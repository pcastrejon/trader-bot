"""Opt-in Ollama chat adapter; fixed IPv4 loopback, no tools or remote endpoints."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import http.client
import json
import math
import re
import socket
import threading
import time

from trader_bot.agents import (
    ContractError, ModelIdentity, ModelUnavailable, object_fields, strict_json, text,
)


@dataclass(frozen=True, slots=True)
class LocalModelConfig:
    model: str
    version: str
    port: int = 11434
    enabled: bool = False

    def __post_init__(self) -> None:
        text(self.model, 200)
        text(self.version, 200)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", self.model):
            raise ContractError("Local model identifier contains unsupported characters.")
        if "cloud" in self.model.lower() or not re.fullmatch(r"sha256:[a-f0-9]{64}", self.version):
            raise ContractError("Use a local model and its pinned sha256 manifest digest.")
        if (
            isinstance(self.port, bool) or not isinstance(self.port, int)
            or not 1024 <= self.port <= 65535
            or not isinstance(self.enabled, bool)
        ):
            raise ContractError("Local inference requires a valid unprivileged port and boolean opt-in.")


class OllamaLocalModel:
    def __init__(self, config: LocalModelConfig) -> None:
        self.config = config
        self._generation_lock = threading.Lock()

    @property
    def identity(self) -> ModelIdentity:
        return ModelIdentity("local", self.config.model, self.config.version)

    async def complete(
        self, system: str, user: str, timeout_seconds: float, *,
        response_schema: dict[str, object] | None = None,
    ) -> str:
        if not self.config.enabled:
            raise ModelUnavailable("Local inference is disabled.")
        if response_schema is not None:
            system += "\nRequired response JSON schema:\n" + json.dumps(
                response_schema, sort_keys=True, allow_nan=False,
            )
        if (
            not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30
            or len(system) + len(user) > 400000
        ):
            raise ContractError("Local inference request exceeds deadline or prompt bounds.")
        # The synchronous socket also has a timeout; cancelling the await does not kill a thread.
        return await asyncio.wait_for(
            asyncio.to_thread(
                self._complete, system, user, timeout_seconds, response_schema,
            ), timeout_seconds,
        )

    def _complete(
        self, system: str, user: str, timeout_seconds: float,
        response_schema: dict[str, object] | None,
    ) -> str:
        deadline = time.monotonic() + timeout_seconds
        # Keep the lock in the worker: cancelling the await does not stop socket work.
        if not self._generation_lock.acquire(timeout=timeout_seconds):
            raise TimeoutError("Local inference deadline expired while queued.")
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Local inference deadline expired while queued.")
            return self._generate(system, user, remaining, response_schema)
        finally:
            self._generation_lock.release()

    def _generate(
        self, system: str, user: str, timeout_seconds: float,
        response_schema: dict[str, object] | None,
    ) -> str:
        payload = json.dumps({
            "model": self.config.model, "stream": False,
            "format": response_schema if response_schema is not None else "json",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "options": {"temperature": 0, "seed": 0, "num_predict": 256},
        }, allow_nan=False).encode("utf-8")
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.config.port, timeout=timeout_seconds,
        )
        try:
            connection.request("GET", "/api/tags")
            tags_response = connection.getresponse()
            if tags_response.status != 200:
                raise ModelUnavailable("Cannot verify installed local model.")
            tags_raw = tags_response.read(65537)
            if len(tags_raw) > 65536:
                raise ContractError("Local model listing exceeds bounds.")
            tags = strict_json(tags_raw.decode("utf-8"))
            if not isinstance(tags, dict) or not isinstance(tags.get("models"), list):
                raise ContractError("Malformed local model listing.")
            installed = [
                model for model in tags["models"]
                if isinstance(model, dict) and model.get("name") == self.config.model
            ]
            if (
                len(installed) != 1 or installed[0].get("digest") not in (
                    self.config.version, self.config.version.removeprefix("sha256:"),
                )
                or installed[0].get("remote_host") or installed[0].get("remote_model")
            ):
                raise ModelUnavailable("Pinned local model is missing, changed or remote.")
            connection.request("POST", "/api/chat", payload, {"Content-Type": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                raise ModelUnavailable("Local inference returned a non-success status.")
            raw = response.read(65537)
            if len(raw) > 65536:
                raise ContractError("Local response exceeds 64 KB.")
            item = strict_json(raw.decode("utf-8"))
            if not isinstance(item, dict):
                raise ContractError("Local response must be an object.")
            if item.get("model") != self.config.model or item.get("done") is not True:
                raise ContractError("Local response model mismatch or incomplete generation.")
            if item.get("done_reason") == "length":
                raise ContractError("Local response exhausted its generation token limit.")
            message = object_fields(item.get("message"), {"role", "content"})
            if message["role"] != "assistant":
                raise ContractError("Local response has unexpected role.")
            return text(message["content"], 32000)
        except (socket.timeout, TimeoutError) as exc:
            raise TimeoutError("Local inference timed out.") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise ModelUnavailable("Local inference transport unavailable.") from exc
        except UnicodeError as exc:
            raise ContractError("Local response is not UTF-8.") from exc
        finally:
            connection.close()
