import asyncio
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import unittest
from unittest.mock import MagicMock, patch

from trader_bot.agents import ModelUnavailable
from trader_bot.contracts import ContractError
from trader_bot.local_model import LocalModelConfig, OllamaLocalModel
from tests.agent_support import DIGEST, completion


def response(payload: object, status: int = 200) -> MagicMock:
    result = MagicMock()
    result.status = status
    result.read.return_value = json.dumps(payload).encode()
    return result


def installed(**changes: object) -> MagicMock:
    model = {"name": "demo:latest", "digest": DIGEST}
    model.update(changes)
    return response({"models": [model]})


def generated(**changes: object) -> MagicMock:
    payload = {
        "model": "demo:latest", "done": True,
        "message": {"role": "assistant", "content": completion()},
    }
    payload.update(changes)
    return response(payload)


class LocalModelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.model = OllamaLocalModel(LocalModelConfig("demo:latest", DIGEST, enabled=True))

    async def test_disabled_never_opens_connection(self) -> None:
        model = OllamaLocalModel(LocalModelConfig("demo:latest", DIGEST))
        with patch("trader_bot.local_model.http.client.HTTPConnection") as connection:
            with self.assertRaises(ModelUnavailable):
                await model.complete("system", "user", 1)
            connection.assert_not_called()

    async def test_fixed_loopback_no_credentials_tools_or_streaming(self) -> None:
        with patch("trader_bot.local_model.http.client.HTTPConnection") as factory:
            connection = factory.return_value
            connection.getresponse.side_effect = [installed(), generated()]
            result = await self.model.complete("system", "user", 2)
            self.assertEqual(result, completion())
            factory.assert_called_once()
            self.assertEqual(factory.call_args.args, ("127.0.0.1", 11434))
            self.assertGreater(factory.call_args.kwargs["timeout"], 0)
            self.assertLessEqual(factory.call_args.kwargs["timeout"], 2)
            calls = connection.request.call_args_list
            self.assertEqual(calls[0].args, ("GET", "/api/tags"))
            self.assertEqual(calls[1].args[:2], ("POST", "/api/chat"))
            payload = json.loads(calls[1].args[2])
            self.assertFalse(payload["stream"])
            self.assertNotIn("tools", payload)
            self.assertEqual(payload["options"]["temperature"], 0)
            self.assertEqual(calls[1].args[3], {"Content-Type": "application/json"})
            connection.close.assert_called_once()

    async def test_ollama_bare_manifest_digest_is_accepted(self) -> None:
        with patch("trader_bot.local_model.http.client.HTTPConnection") as factory:
            factory.return_value.getresponse.side_effect = [
                installed(digest=DIGEST.removeprefix("sha256:")), generated(),
            ]
            self.assertEqual(await self.model.complete("system", "user", 1), completion())

    async def test_response_schema_is_sent_as_format_and_grounded_in_system_prompt(self) -> None:
        schema = {
            "type": "object", "properties": {"summary": {"type": "string"}},
            "required": ["summary"], "additionalProperties": False,
        }
        with patch("trader_bot.local_model.http.client.HTTPConnection") as factory:
            connection = factory.return_value
            connection.getresponse.side_effect = [installed(), generated()]
            await self.model.complete("system", "user", 2, response_schema=schema)
            payload = json.loads(connection.request.call_args_list[1].args[2])
            self.assertEqual(payload["format"], schema)
            self.assertIn(
                json.dumps(schema, sort_keys=True), payload["messages"][0]["content"],
            )
            self.assertEqual(payload["messages"][1]["content"], "user")
            self.assertEqual(payload["options"]["num_predict"], 256)

    async def test_schema_rejected_by_daemon_does_not_retry_without_constraints(self) -> None:
        with patch("trader_bot.local_model.http.client.HTTPConnection") as factory:
            connection = factory.return_value
            connection.getresponse.side_effect = [
                installed(), response({"error": "schema unsupported"}, 400),
            ]
            with self.assertRaises(ModelUnavailable):
                await self.model.complete(
                    "system", "user", 1, response_schema={"type": "object"},
                )
            self.assertEqual(connection.request.call_count, 2)
            connection.close.assert_called_once()

    def test_remote_names_unpinned_versions_and_bad_ports_rejected(self) -> None:
        for model, digest, port in (
            ("demo:cloud", DIGEST, 11434), ("https://cloud-host/x", "unversioned", 11434),
            ("model\nheader", DIGEST, 11434), ("demo", "v1", 11434),
            ("demo", DIGEST, 0), ("demo", DIGEST, True), ("demo", DIGEST, 65536),
        ):
            with self.subTest(model=model, port=port), self.assertRaises(ContractError):
                LocalModelConfig(model, digest, port, True)

    async def test_missing_changed_and_remote_installed_model_blocks_chat(self) -> None:
        for listing in (
            response({"models": []}), installed(digest="sha256:" + "b" * 64),
            installed(remote_host="https://remote.example"),
            installed(remote_model="hosted"), response({"models": "invalid"}),
        ):
            with self.subTest(listing=listing), patch(
                "trader_bot.local_model.http.client.HTTPConnection",
            ) as factory:
                connection = factory.return_value
                connection.getresponse.return_value = listing
                with self.assertRaises((ModelUnavailable, ContractError)):
                    await self.model.complete("system", "user", 1)
                self.assertEqual(connection.request.call_count, 1)
                connection.close.assert_called_once()

    async def test_redirect_and_http_errors_never_followed_or_leaked(self) -> None:
        for status in (302, 401, 429, 500):
            with self.subTest(status=status), patch(
                "trader_bot.local_model.http.client.HTTPConnection",
            ) as factory:
                factory.return_value.getresponse.return_value = response(
                    {"error": "SECRET_FROM_SERVER"}, status,
                )
                with self.assertRaises(ModelUnavailable) as error:
                    await self.model.complete("system", "user", 1)
                self.assertNotIn("SECRET", str(error.exception))
                self.assertEqual(factory.call_count, 1)

    async def test_malformed_and_incomplete_response_rejected(self) -> None:
        for output in (
            generated(model="other"), generated(done=False),
            generated(done_reason="length"),
            generated(message={"role": "assistant", "content": completion(), "tool_calls": []}),
            generated(message={"role": "user", "content": completion()}),
            response([]),
        ):
            with self.subTest(output=output), patch(
                "trader_bot.local_model.http.client.HTTPConnection",
            ) as factory:
                factory.return_value.getresponse.side_effect = [installed(), output]
                with self.assertRaises(ContractError):
                    await self.model.complete("system", "user", 1)

    async def test_oversized_invalid_json_and_encoding_rejected(self) -> None:
        for raw in (b"x" * 65537, b"{", b"\xff", b'{"done":true,"done":false}'):
            with self.subTest(raw=raw[:30]), patch(
                "trader_bot.local_model.http.client.HTTPConnection",
            ) as factory:
                output = generated()
                output.read.return_value = raw
                factory.return_value.getresponse.side_effect = [installed(), output]
                with self.assertRaises(ContractError):
                    await self.model.complete("system", "user", 1)

    async def test_transport_errors_are_sanitized(self) -> None:
        for exception in (OSError("SECRET"), http.client.BadStatusLine("SECRET")):
            with self.subTest(exception=exception), patch(
                "trader_bot.local_model.http.client.HTTPConnection",
            ) as factory:
                factory.return_value.request.side_effect = exception
                with self.assertRaises(ModelUnavailable) as error:
                    await self.model.complete("system", "user", 1)
                self.assertNotIn("SECRET", str(error.exception))
                factory.return_value.close.assert_called_once()

    async def test_socket_timeout_remains_timeout(self) -> None:
        with patch("trader_bot.local_model.http.client.HTTPConnection") as factory:
            factory.return_value.request.side_effect = socket.timeout("SECRET")
            with self.assertRaises(TimeoutError):
                await self.model.complete("system", "user", 1)

    async def test_total_async_deadline(self) -> None:
        async def slow(*_args: object) -> str:
            await asyncio.sleep(1)
            return completion()
        with patch("trader_bot.local_model.asyncio.to_thread", side_effect=slow):
            with self.assertRaises(TimeoutError):
                await self.model.complete("system", "user", 0.01)

    async def test_invalid_request_bounds_never_open_socket(self) -> None:
        with patch("trader_bot.local_model.http.client.HTTPConnection") as factory:
            for timeout in (0, -1, 31, float("nan"), float("inf")):
                with self.subTest(timeout=timeout), self.assertRaises(ContractError):
                    await self.model.complete("system", "user", timeout)
            with self.assertRaises(ContractError):
                await self.model.complete("x" * 400001, "user", 1)
            factory.assert_not_called()

    async def test_queue_timeout_does_not_start_another_generation_after_cancellation(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def generate(*_args: object) -> str:
            entered.set()
            try:
                if not release.wait(2):
                    raise AssertionError("Test did not release worker.")
                return completion()
            finally:
                finished.set()

        with patch.object(self.model, "_generate", side_effect=generate) as worker:
            first = asyncio.create_task(self.model.complete("system", "user", 0.1))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                with self.assertRaises(TimeoutError):
                    await first
                with self.assertRaises(TimeoutError):
                    await self.model.complete("system", "queued", 0.01)
                self.assertEqual(worker.call_count, 1)
            finally:
                release.set()
                self.assertTrue(await asyncio.to_thread(finished.wait, 1))
                if not first.done():
                    first.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await first


class LoopbackIntegrationTests(unittest.TestCase):
    def test_real_loopback_transport_and_parallel_orchestration(self) -> None:
        from trader_bot.agents import AgentOrchestrator, local_agents
        from tests.agent_support import NOW, evidence

        requests: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def respond(self, payload: object) -> None:
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                requests.append(self.path)
                self.respond({"models": [{"name": "demo:latest", "digest": DIGEST}]})

            def do_POST(self) -> None:
                requests.append(self.path)
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                data = json.loads(payload["messages"][1]["content"])
                reference = data["untrusted_evidence"][0]["reference"]["reference"]
                self.respond({
                    "model": "demo:latest", "done": True,
                    "message": {"role": "assistant", "content": completion(reference)},
                })

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            model = OllamaLocalModel(LocalModelConfig(
                "demo:latest", DIGEST, server.server_port, enabled=True,
            ))
            decision = asyncio.run(AgentOrchestrator(
                local_agents(model), clock=lambda: NOW,
            ).analyze("corr", "US:DEMO", evidence()))
            self.assertTrue(decision.can_continue)
            self.assertTrue(all(r.model_version == DIGEST for r in decision.report.results))
            self.assertEqual(requests.count("/api/tags"), 3)
            self.assertEqual(requests.count("/api/chat"), 3)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
