"""Local configuration validation and offline deterministic signal inspection."""

from __future__ import annotations

import argparse
import asyncio
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
from trader_bot.backtest import BacktestRunner
from trader_bot.contracts import RuntimeMode
from trader_bot.replay import read_bundle, replay_bundle, write_bundle
from trader_bot.agents import AgentOrchestrator, DisabledModel, local_agents
from trader_bot.analysis_io import read_analysis, replay_analysis, write_analysis
from trader_bot.local_model import LocalModelConfig, OllamaLocalModel


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trader-bot")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("config-check", help="validate local runtime configuration")
    analysis = subparsers.add_parser(
        "analyze-agents", help="analyze a local evidence snapshot; never approve or submit orders",
    )
    analysis.add_argument("--input", type=Path, required=True)
    analysis.add_argument("--output", type=Path, required=True)
    analysis.add_argument("--as-of", required=True)
    analysis.add_argument("--model")
    analysis.add_argument("--model-version")
    analysis.add_argument("--port", type=int, default=11434)
    analysis_replay = subparsers.add_parser(
        "replay-analysis", help="revalidate recorded analyst outcomes and replay policy, without inference",
    )
    analysis_replay.add_argument("--input", type=Path, required=True)
    backtest = subparsers.add_parser("backtest", help="run an offline JSON research bundle")
    backtest.add_argument("--input", type=Path, required=True)
    backtest.add_argument("--output", type=Path, required=True)
    replay = subparsers.add_parser("replay", help="verify a recorded local research report")
    replay.add_argument("--input", type=Path, required=True)
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

    if args.command in ("analyze-agents", "replay-analysis"):
        try:
            runtime = RuntimeConfig.from_env()
            if args.command == "replay-analysis":
                decision = replay_analysis(args.input)
            else:
                correlation, instrument, evidence, policy, _ = read_analysis(args.input)
                as_of = parse_time(args.as_of)
                model = DisabledModel()
                if runtime.local_model_enabled:
                    if args.model is None or args.model_version is None:
                        raise ConfigurationError("Enabled inference requires --model and --model-version.")
                    local_model = OllamaLocalModel(LocalModelConfig(
                        args.model, args.model_version, args.port, enabled=True,
                    ))
                    agents = local_agents(local_model)
                else:
                    agents = local_agents(model)
                decision = asyncio.run(AgentOrchestrator(
                    agents, policy, clock=lambda: as_of,
                ).analyze(correlation, instrument, evidence))
                write_analysis(args.output, correlation, instrument, evidence, policy, decision)
            for result in decision.report.results:
                print(f"{result.role.value}: {result.status.value}: {result.summary}")
            print(f"Analysis can continue: {decision.can_continue}")
            for code in decision.reason_codes:
                print(code)
            print("Context only; not a signal, risk approval or broker order.")
            return 0 if decision.can_continue else 2
        except (ConfigurationError, DataError, ContractError, InvalidOperation) as exc:
            print(f"Analysis error: {exc}", file=sys.stderr)
            return 2

    if args.command in ("backtest", "replay"):
        try:
            runtime = RuntimeConfig.from_env()
            if runtime.mode is not RuntimeMode.BACKTEST:
                raise ConfigurationError("Offline backtest/replay commands require backtest mode.")
            if args.command == "backtest":
                data, _ = read_bundle(args.input)
                report = BacktestRunner().run(data)
                write_bundle(args.output, data, report)
            else:
                report = replay_bundle(args.input)
            print(
                f"Offline {args.command}: initial={report.initial_equity} "
                f"final={report.final_equity} realized_pnl={report.realized_pnl} "
                f"fees={report.total_fees} max_drawdown={report.max_drawdown}"
            )
            print(f"Complete: {report.complete}; unresolved quantity: {report.open_quantity}")
            print(f"Input fingerprint: {report.input_fingerprint}")
            print("Research simulation only; no AI, broker connection or order submission.")
            return 0 if report.complete else 2
        except (ConfigurationError, DataError, ContractError) as exc:
            print(f"Backtest/replay error: {exc}", file=sys.stderr)
            return 2

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
