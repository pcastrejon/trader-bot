"""Local configuration validation and offline deterministic signal inspection."""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from decimal import InvalidOperation
from pathlib import Path

from trader_bot.config import ConfigurationError, RuntimeConfig
from trader_bot.contracts import AssetType, ContractError, Instrument, Quote
from trader_bot.market_data import (
    CsvMarketDataProvider, DataError, MarketDataClient, UsagePolicy, parse_decimal,
    parse_time,
)
from trader_bot.pipeline import ResearchPipeline
from trader_bot.strategy import (
    BenchmarkStrategy, EligibilityPolicy, EvaluationStatus, ExchangeSession,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trader-bot")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("config-check", help="validate local runtime configuration")
    research = subparsers.add_parser(
        "analyze-csv", help="inspect a local five-minute research signal; never submit orders"
    )
    for name in (
        "bars", "symbol", "venue", "calendar", "session-open", "session-close",
        "as-of", "bid", "ask", "quote-at", "min-price", "min-volume", "max-spread-bps",
    ):
        research.add_argument(f"--{name}", required=True)
    research.add_argument("--max-quote-age-seconds", type=int, required=True)
    research.add_argument("--max-bar-age-seconds", type=int, required=True)
    research.add_argument(
        "--verified", action="store_true", required=True,
        help="acknowledge verification of the supplied instrument, data and session",
    )
    args = parser.parse_args(argv)

    if args.command == "analyze-csv":
        try:
            runtime = RuntimeConfig.from_env()
            as_of = parse_time(args.as_of)
            session = ExchangeSession(
                args.calendar, parse_time(args.session_open),
                parse_time(args.session_close), "operator-supplied",
            )
            instrument = Instrument(
                instrument_id=f"US:{args.symbol.upper()}", symbol=args.symbol.upper(),
                asset_type=AssetType.STOCK, venue=args.venue, currency="USD",
                supported_modes=("backtest", "paper"), market_calendar=args.calendar,
            )
            quote = Quote(
                instrument_id=instrument.instrument_id,
                bid=parse_decimal(args.bid), ask=parse_decimal(args.ask),
                observed_at=parse_time(args.quote_at), received_at=as_of,
                source="local_csv",
            )
            policy = EligibilityPolicy(
                verified_instrument_ids=(instrument.instrument_id,),
                min_price=parse_decimal(args.min_price),
                min_bar_volume=parse_decimal(args.min_volume),
                max_spread_bps=parse_decimal(args.max_spread_bps),
                max_quote_age=timedelta(seconds=args.max_quote_age_seconds),
                max_bar_age=timedelta(seconds=args.max_bar_age_seconds),
            )
            client = MarketDataClient(
                CsvMarketDataProvider(Path(args.bars), quote), UsagePolicy(requests_per_day=1),
            )
            pipeline = ResearchPipeline(client, BenchmarkStrategy(), runtime, policy, (session,))
            evaluation = pipeline.evaluate(
                instrument, session.opens_at, as_of, "cli-research",
            )
        except (ConfigurationError, DataError, ContractError, InvalidOperation) as exc:
            print(f"Research configuration/data error: {exc}", file=sys.stderr)
            return 2
        print(f"{evaluation.status.value}: {evaluation.code}: {evaluation.explanation}")
        if evaluation.candidate is not None:
            signal = evaluation.candidate
            print(f"{signal.instrument_id} {signal.direction.value} at {signal.signal_bar_ends_at}")
            print(f"Protective stop reference: {signal.protective_stop_price}")
            print(f"Strategy: {signal.strategy_version}")
            for evidence in signal.rule_evidence:
                print(evidence)
        print("Research only: no risk approval, AI inference, broker connection or order submission.")
        return 2 if evaluation.status is EvaluationStatus.REJECTED else 0

    if args.command == "config-check":
        try:
            config = RuntimeConfig.from_env()
        except (ConfigurationError, InvalidOperation) as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
        print(f"Configuration valid (mode={config.mode.value}).")
        print(f"Watchlist instruments: {len(config.watchlist)}")
        print(f"Monthly cost cap: {config.monthly_cost_cap}")
        print(f"Local model enabled: {config.local_model_enabled}")
        if config.mode.value == "paper":
            print("Paper credentials: configured (values redacted)")
        return 0

    parser.error("Unsupported command.")
    return 2
