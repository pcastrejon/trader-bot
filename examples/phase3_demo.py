"""Create synthetic analyst evidence and, optionally, a clearly labeled fixture report."""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path

from trader_bot.agents import (
    AgentOrchestrator, AnalysisPolicy, Evidence, Metric, ModelIdentity, local_agents,
)
from trader_bot.analysis_io import write_analysis
from trader_bot.contracts import AgentRole, InputReference


AS_OF = datetime(2026, 1, 2, 19, 0, tzinfo=timezone.utc)


class SyntheticModel:
    """Test double, not an AI model or a fallback for unavailable inference."""

    identity = ModelIdentity("fixture", "synthetic-demo-not-ai", "1")

    async def complete(self, system: str, user: str, timeout_seconds: float) -> str:
        await asyncio.sleep(0)
        item = json.loads(user)
        return json.dumps({
            "summary": f"Synthetic {item['role']} fixture; no AI inference performed.",
            "facts": ["All supplied observations are synthetic."],
            "interpretation": "This demonstrates software contracts, not market expectations.",
            "uncertainty": "unknown", "veto": False, "warnings": ["Test double, not AI analysis."],
            "source_refs": [entry["reference"]["reference"] for entry in item["untrusted_evidence"]],
        })


def sample_evidence() -> tuple[Evidence, ...]:
    return (
        Evidence(
            AgentRole.TECHNICAL_CONTEXT, "US:DEMO",
            InputReference("synthetic", "technical-demo", AS_OF), AS_OF, True,
            tuple(Metric(name, Decimal(value), unit) for name, value, unit in (
                ("close", "110", "USD"), ("volume", "10000", "shares"),
                ("ema9", "101", "USD"), ("ema21", "100", "USD"),
                ("ema50", "99", "USD"), ("atr14", "2", "USD"),
            )),
        ),
        Evidence(
            AgentRole.NEWS_SENTIMENT, "US:DEMO",
            InputReference("synthetic-permitted", "news-demo", AS_OF), AS_OF, True,
            content="Synthetic company announces an upcoming earnings release.",
        ),
        Evidence(
            AgentRole.FUNDAMENTAL_CONTEXT, "US:DEMO",
            InputReference("synthetic-permitted", "fundamental-demo", AS_OF), AS_OF, True,
            (Metric("revenue", Decimal("1000000"), "USD"),),
            period_ends_at=AS_OF - timedelta(days=30),
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixture-report", action="store_true",
                        help="record synthetic test-double outputs, not real model analysis")
    args = parser.parse_args()
    evidence = sample_evidence()
    policy = AnalysisPolicy()
    decision = None
    if args.fixture_report:
        decision = asyncio.run(AgentOrchestrator(
            local_agents(SyntheticModel()), policy, clock=lambda: AS_OF,
        ).analyze("phase3-synthetic-demo", "US:DEMO", evidence))
    write_analysis(args.output, "phase3-synthetic-demo", "US:DEMO", evidence, policy, decision)
    print(f"Synthetic local analysis bundle: {args.output}")
    print(f"Snapshot as-of: {AS_OF.isoformat()}; no model or broker connection.")


if __name__ == "__main__":
    main()
