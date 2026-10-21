import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

from trader_bot.agents import Evidence, Metric, ModelIdentity
from trader_bot.contracts import AgentRole, InputReference


NOW = datetime(2026, 1, 2, 19, 0, tzinfo=timezone.utc)
DIGEST = "sha256:" + "a" * 64


def evidence() -> tuple[Evidence, ...]:
    return (
        Evidence(
            AgentRole.TECHNICAL_CONTEXT, "US:DEMO",
            InputReference("synthetic", "technical-1", NOW), NOW, True,
            tuple(Metric(name, Decimal(value), unit) for name, value, unit in (
                ("close", "110", "USD"), ("volume", "10000", "shares"),
                ("ema9", "101", "USD"), ("ema21", "100", "USD"),
                ("ema50", "99", "USD"), ("atr14", "2", "USD"),
            )),
        ),
        Evidence(
            AgentRole.NEWS_SENTIMENT, "US:DEMO",
            InputReference("synthetic-permitted", "news-1", NOW - timedelta(hours=1)),
            NOW, True, content="Synthetic company announces a scheduled earnings release.",
        ),
        Evidence(
            AgentRole.FUNDAMENTAL_CONTEXT, "US:DEMO",
            InputReference("synthetic-permitted", "fundamental-1", NOW - timedelta(days=10)),
            NOW - timedelta(days=9), True,
            (Metric("revenue", Decimal("1000000"), "USD"),),
            period_ends_at=NOW - timedelta(days=30),
        ),
    )


def completion(reference: str = "technical-1", **changes: object) -> str:
    payload: dict[str, object] = {
        "summary": "Synthetic context only.", "facts": ["The supplied observation is synthetic."],
        "interpretation": "No conclusion about future returns.",
        "uncertainty": "high", "veto": False, "warnings": [],
        "source_refs": [reference],
    }
    payload.update(changes)
    return json.dumps(payload)


class FixtureModel:
    identity = ModelIdentity("fixture", "synthetic", "1")

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.outputs: dict[str, str] = {}
        self.delay = 0.0
        self.started = 0
        self.concurrent = 0

    async def complete(self, system: str, user: str, timeout_seconds: float) -> str:
        self.calls.append((system, user))
        self.started += 1
        self.concurrent = max(self.concurrent, self.started)
        try:
            await asyncio.sleep(self.delay)
            item = json.loads(user)
            return self.outputs.get(
                item["role"], completion(item["untrusted_evidence"][0]["reference"]["reference"]),
            )
        finally:
            self.started -= 1
