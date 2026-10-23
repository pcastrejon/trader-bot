"""Local configuration validation and offline deterministic signal inspection."""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
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
from trader_bot.agents import (
    AgentOrchestrator, DisabledModel, local_agents,
)
from trader_bot.analysis_io import read_analysis, replay_analysis, write_analysis
from trader_bot.local_model import LocalModelConfig, OllamaLocalModel
from trader_bot.paper_broker import (
    AlpacaPaperBroker, PaperBrokerError, PaperOrder, PaperOrderJournal,
)
from trader_bot.paper_session import PaperSessionController, PaperSessionStatus
from trader_bot.risk import CircuitState, DailyCircuitBreaker, RiskWarden


def _add_paper_session_arguments(parser: argparse.ArgumentParser) -> None:
    for name in (
        "bars", "symbol", "venue", "calendar", "session-open", "session-close",
        "as-of", "bid", "ask", "quote-at", "min-price", "min-volume", "max-spread-bps",
    ):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--max-quote-age-seconds", type=int, required=True)
    parser.add_argument("--max-bar-age-seconds", type=int, required=True)
    parser.add_argument(
        "--verified", action="store_true", required=True,
        help="acknowledge verification of the supplied instrument, data and session",
    )
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--journal", type=Path, default=Path("local-data/paper-orders.sqlite3"))
    parser.add_argument("--model")
    parser.add_argument("--model-version")
    parser.add_argument("--port", type=int, default=11434)


def _paper_session_inputs(args, runtime):
    as_of = parse_time(args.as_of)
    session = ExchangeSession(
        args.calendar, parse_time(args.session_open),
        parse_time(args.session_close), "operator-supplied",
    )
    symbol = args.symbol.upper()
    instrument = Instrument(
        instrument_id=f"US:{symbol}", symbol=symbol,
        asset_type=AssetType.STOCK, venue=args.venue, currency="USD",
        supported_modes=("backtest", "paper"), market_calendar=args.calendar,
    )
    quote = Quote(
        instrument_id=instrument.instrument_id,
        bid=parse_decimal(args.bid), ask=parse_decimal(args.ask),
        observed_at=parse_time(args.quote_at), received_at=as_of,
        source="local_csv",
    )
    eligibility = EligibilityPolicy(
        verified_instrument_ids=(instrument.instrument_id,),
        min_price=parse_decimal(args.min_price),
        min_bar_volume=parse_decimal(args.min_volume),
        max_spread_bps=parse_decimal(args.max_spread_bps),
        max_quote_age=timedelta(seconds=args.max_quote_age_seconds),
        max_bar_age=timedelta(seconds=args.max_bar_age_seconds),
    )
    pipeline = ResearchPipeline(
        MarketDataClient(
            CsvMarketDataProvider(Path(args.bars), quote),
            UsagePolicy(requests_per_day=1),
        ),
        BenchmarkStrategy(), runtime, eligibility, (session,),
    )
    correlation, evidence_instrument, evidence, policy, _ = read_analysis(args.evidence)
    if evidence_instrument != instrument.instrument_id:
        raise ContractError("Evidence bundle instrument does not match the selected symbol.")
    return as_of, session, instrument, pipeline, correlation, evidence, policy


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
    paper_account = subparsers.add_parser(
        "paper-account", help="read the connected Alpaca paper-account status",
    )
    paper_orders = subparsers.add_parser(
        "paper-orders", help="list currently open Alpaca paper orders",
    )
    paper_orders.add_argument("--journal", type=Path, default=Path("local-data/paper-orders.sqlite3"))
    paper_reconcile = subparsers.add_parser(
        "paper-reconcile", help="reconcile journaled orders; optionally cancel expired entries",
    )
    paper_reconcile.add_argument("--journal", type=Path, default=Path("local-data/paper-orders.sqlite3"))
    paper_reconcile.add_argument(
        "--cancel-expired", action="store_true",
        help=(
            "cancel unfilled expired entries (requires paper writes and verified "
            "paper execution capabilities)"
        ),
    )
    paper_reconcile.add_argument(
        "--acknowledge-reconciled", action="store_true",
        help="clear the persistent entry halt after successful reconciliation",
    )
    paper_pause = subparsers.add_parser(
        "paper-pause", help="persistently block new paper entries (does not cancel orders)",
    )
    paper_pause.add_argument("--journal", type=Path, default=Path("local-data/paper-orders.sqlite3"))
    paper_resume = subparsers.add_parser(
        "paper-resume", help="reconcile paper orders, then explicitly clear the entry pause",
    )
    paper_resume.add_argument("--journal", type=Path, default=Path("local-data/paper-orders.sqlite3"))
    paper_preview = subparsers.add_parser(
        "paper-preview",
        help="preview a paper-session decision using local evidence; never submit an order",
    )
    _add_paper_session_arguments(paper_preview)
    paper_lifecycle = subparsers.add_parser(
        "paper-lifecycle-test",
        help="submit one risk-approved paper test order up to $100, then cancel if unfilled",
    )
    _add_paper_session_arguments(paper_lifecycle)
    paper_lifecycle.add_argument("--max-notional", required=True)
    paper_lifecycle.add_argument(
        "--confirm-submit", required=True,
        help="type exactly: SUBMIT <SYMBOL> UP TO <MAX_NOTIONAL> USD",
    )
    args = parser.parse_args(argv)

    if args.command == "paper-pause":
        try:
            journal = PaperOrderJournal(args.journal)
            halted, reason = journal.pause_entries()
            print(f"Paper entries halted: {halted} ({reason})")
            print("Existing positions and orders are unchanged.")
            return 0
        except (PaperBrokerError, sqlite3.Error, OSError) as exc:
            print(f"Paper pause error: {exc}", file=sys.stderr)
            return 2

    if args.command == "paper-resume":
        try:
            runtime = RuntimeConfig.from_env()
            journal = PaperOrderJournal(args.journal)
            broker = AlpacaPaperBroker(runtime, journal)
            broker.reconcile()
            broker.account()
            if broker.positions() or broker.open_orders():
                raise PaperBrokerError(
                    "PAPER_ACCOUNT_NOT_FLAT",
                    "Cannot resume entries while paper positions or open orders remain.",
                )
            broker.reconcile(acknowledge_reconciled=True)
            halted, reason = journal.safety_state()
            print(f"Paper entries halted: {halted}")
            if halted:
                print(f"Paper entries remain halted: {reason}", file=sys.stderr)
                return 2
            print("Paper entries resumed after successful broker reconciliation.")
            return 0
        except (
            ConfigurationError, PaperBrokerError, ContractError, sqlite3.Error, OSError,
        ) as exc:
            print(f"Paper resume error: {exc}", file=sys.stderr)
            return 2

    if args.command in ("paper-account", "paper-orders", "paper-reconcile"):
        try:
            runtime = RuntimeConfig.from_env()
            journal = (
                PaperOrderJournal(args.journal)
                if args.command != "paper-account" else None
            )
            broker = AlpacaPaperBroker(runtime, journal)
            if args.command == "paper-account":
                account = broker.account()
                print(
                    f"Paper account ready: currency={account.currency} "
                    f"equity={account.equity} buying_power={account.buying_power}"
                )
                return 0
            if args.command == "paper-orders":
                orders = broker.open_orders()
            else:
                orders = broker.reconcile(
                    cancel_expired=args.cancel_expired,
                    acknowledge_reconciled=args.acknowledge_reconciled,
                )
            for order in orders:
                print(
                    f"{order.symbol} {order.side} {order.status}: "
                    f"filled={order.filled_quantity}/{order.ordered_quantity} "
                    f"client_order_id={order.client_order_id}"
                )
            print(f"Paper orders returned: {len(orders)}")
            if journal is not None:
                halted, reason = journal.safety_state()
                if halted:
                    print(f"Paper entries remain halted: {reason}", file=sys.stderr)
                    return 2
            return 0
        except (
            ConfigurationError, PaperBrokerError, ContractError, sqlite3.Error, OSError,
        ) as exc:
            print(f"Paper broker error: {exc}", file=sys.stderr)
            return 2

    if args.command in {"paper-preview", "paper-lifecycle-test"}:
        lifecycle_test = args.command == "paper-lifecycle-test"
        try:
            runtime = RuntimeConfig.from_env()
            if runtime.mode is not RuntimeMode.PAPER:
                raise ConfigurationError("Paper session requires paper runtime mode.")
            max_notional = None
            if lifecycle_test:
                if not runtime.paper_order_submission_enabled:
                    raise ConfigurationError(
                        "Set TRADER_BOT_PAPER_ORDER_SUBMISSION_ENABLED=true for the explicit test order."
                    )
                max_notional = parse_decimal(args.max_notional)
                if max_notional <= 0 or max_notional > 100:
                    raise ConfigurationError("Lifecycle-test notional must be positive and at most USD 100.")
                expected_confirmation = (
                    f"SUBMIT {args.symbol.upper()} UP TO "
                    f"{format(max_notional.normalize(), 'f')} USD"
                )
                if args.confirm_submit != expected_confirmation:
                    raise ConfigurationError(
                        f"Type exactly {expected_confirmation!r} to authorize this paper test order."
                    )
                if not runtime.local_model_enabled:
                    raise ConfigurationError(
                        "The lifecycle test requires enabled local analyst inference."
                    )
            (
                as_of, session, instrument, pipeline, correlation, evidence, policy,
            ) = _paper_session_inputs(args, runtime)
            model = DisabledModel()
            if runtime.local_model_enabled:
                if args.model is None or args.model_version is None:
                    raise ConfigurationError(
                        "Enabled local inference requires --model and --model-version."
                    )
                local_model = OllamaLocalModel(LocalModelConfig(
                    args.model, args.model_version, args.port, enabled=True,
                ))
                model = local_model
            analysts = AgentOrchestrator(
                local_agents(model), policy, clock=lambda: as_of,
            )
            journal = PaperOrderJournal(args.journal)
            broker = AlpacaPaperBroker(runtime, journal)
            account = broker.account()
            circuit_state = broker.load_circuit_state(
                account.account_reference, session,
            ) or CircuitState(
                session.opens_at, session.closes_at, account.equity,
            )
            controller = PaperSessionController(
                pipeline, analysts, RiskWarden(), broker,
            )
            if lifecycle_test:
                if max_notional is None:
                    raise ConfigurationError("Lifecycle-test notional is required.")
                result = asyncio.run(controller.run_lifecycle_test(
                    instrument, session.opens_at, as_of, correlation, evidence,
                    DailyCircuitBreaker(circuit_state),
                    max_notional=max_notional,
                ))
            else:
                result = asyncio.run(controller.preview(
                    instrument, session.opens_at, as_of, correlation, evidence,
                    DailyCircuitBreaker(circuit_state),
                ))
        except (
            ConfigurationError, DataError, ContractError, PaperBrokerError,
            InvalidOperation, sqlite3.Error, OSError, ValueError,
        ) as exc:
            label = "Paper lifecycle test" if lifecycle_test else "Paper preview"
            print(f"{label} error: {exc}", file=sys.stderr)
            return 2

        print(f"{result.status.value}: {result.code}: {result.explanation}")
        if result.analysis is not None:
            print(f"Analyst policy passed: {result.analysis.can_continue}")
            for code in result.analysis.reason_codes:
                print(code)
        if result.risk is not None:
            print(f"Risk approved: {result.risk.decision.allowed}")
            for code in result.risk.decision.reason_codes:
                print(code)
            if result.risk.intent is not None:
                intent = result.risk.intent
                print(
                    f"{'Test' if lifecycle_test else 'Preview'} intent: "
                    f"{intent.direction.value} {intent.quantity} "
                    f"{instrument.symbol} limit={intent.limit_price} stop={intent.stop_price}"
                )
        if lifecycle_test:
            order = result.broker_result
            if isinstance(order, PaperOrder):
                print(
                    f"Test order state: {order.status}; "
                    f"filled={order.filled_quantity}/{order.ordered_quantity}; "
                    f"client_order_id={order.client_order_id}"
                )
            if result.status is PaperSessionStatus.LIFECYCLE_TESTED:
                print(
                    "One paper order was submitted and reconciled. "
                    "This command never retries or automatically liquidates a fill."
                )
            else:
                print("No order was submitted because the lifecycle test did not pass its gates.")
            return 0 if (
                result.status is PaperSessionStatus.LIFECYCLE_TESTED
                and isinstance(order, PaperOrder)
                and order.status == "canceled"
                and order.filled_quantity == 0
            ) else 2
        print("Preview only: this command does not submit, cancel, or modify broker orders.")
        return 2 if result.status is PaperSessionStatus.REJECTED else 0

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
            print(f"Paper order submission enabled: {config.paper_order_submission_enabled}")
            print(
                "Paper execution capabilities verified: "
                f"{config.paper_execution_capabilities_verified}"
            )
        return 0

    parser.error("Unsupported command.")
    return 2
