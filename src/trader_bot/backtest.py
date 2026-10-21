"""Causal, single-instrument archived-quote backtesting. Never connects to a broker."""

from __future__ import annotations

import json
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime, timedelta
from decimal import Context, Decimal, localcontext
from enum import Enum
from hashlib import sha256
from pathlib import Path

from trader_bot.config import PaperCredentials, RuntimeConfig
from trader_bot.contracts import AccountSnapshot, Instrument, MarketBar, Quote, RuntimeMode, TradeDirection
from trader_bot.market_data import DataError, MarketSnapshot, utc_time
from trader_bot.risk import (
    CircuitState, DailyCircuitBreaker, Exposure, OrderIntent, RiskPolicy, RiskWarden,
    decimal_amount, round_step,
)
from trader_bot.strategy import (
    BenchmarkConfig, BenchmarkStrategy, EligibilityPolicy, ExchangeSession, validate_bars,
    validate_instrument,
)


def json_value(value: object) -> object:
    """Canonical public research records only; never serialize RuntimeConfig/secrets."""
    if isinstance(value, (RuntimeConfig, PaperCredentials)):
        raise TypeError("Runtime configuration may contain secrets and cannot be serialized.")
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return utc_time(value).isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported report value: {type(value).__name__}.")


def canonical_json(value: object) -> str:
    return json.dumps(json_value(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value: object) -> str:
    return sha256(canonical_json(value).encode()).hexdigest()


def source_version() -> str:
    """Fingerprint actual implementation, including uncommitted source changes."""
    source = {path.name: path.read_text(encoding="utf-8")
              for path in sorted(Path(__file__).parent.glob("*.py"))}
    return fingerprint(source)


@dataclass(frozen=True, slots=True)
class SimulationPolicy:
    fee_per_share: Decimal = Decimal("0")
    slippage_per_share: Decimal = Decimal("0")
    volume_participation: Decimal = Decimal("0.01")

    def __post_init__(self) -> None:
        decimal_amount(self.fee_per_share, "fee_per_share")
        decimal_amount(self.slippage_per_share, "slippage_per_share")
        decimal_amount(self.volume_participation, "volume_participation", positive=True)
        if self.volume_participation > 1:
            raise DataError("DATA_INVALID", "Volume participation must not exceed one.")


@dataclass(frozen=True, slots=True)
class SimulatedFill:
    at: datetime
    quantity: Decimal
    price: Decimal
    fee: Decimal
    reason: str


class FillSimulator:
    """Open-only limit fills; stops prioritize adverse prices, not guaranteed caps."""

    def __init__(self, policy: SimulationPolicy) -> None:
        self.policy = policy

    def entry(
        self, intent: OrderIntent, bar: MarketBar, quantity_step: Decimal,
        *, available_volume: Decimal,
    ) -> SimulatedFill | None:
        if bar.instrument_id != intent.instrument_id:
            raise DataError("DATA_INVALID", "Order/bar instrument mismatch.")
        if not intent.created_at <= bar.starts_at < intent.expires_at:
            return None
        decimal_amount(available_volume, "known entry liquidity")
        with localcontext(Context(prec=28)):
            long = intent.direction is TradeDirection.LONG
            price = bar.open + self.policy.slippage_per_share if long else (
                bar.open - self.policy.slippage_per_share
            )
            if price <= 0 or (long and price > intent.limit_price) or (
                not long and price < intent.limit_price
            ):
                return None
            if (long and price <= intent.stop_price) or (not long and price >= intent.stop_price):
                return None
            quantity = min(
                intent.quantity,
                round_step(available_volume * self.policy.volume_participation, quantity_step),
            )
            if quantity <= 0:
                return None
            return SimulatedFill(
                bar.starts_at, quantity, price, quantity * self.policy.fee_per_share, "entry",
            )

    def exit(
        self, intent: OrderIntent, quantity: Decimal, bar: MarketBar, *, flatten: bool = False,
    ) -> SimulatedFill | None:
        if bar.instrument_id != intent.instrument_id:
            raise DataError("DATA_INVALID", "Order/bar instrument mismatch.")
        decimal_amount(quantity, "exit quantity", positive=True)
        if quantity > intent.quantity or bar.starts_at < intent.created_at:
            raise DataError("DATA_INVALID", "Cannot exit excess quantity or before intent creation.")
        long = intent.direction is TradeDirection.LONG
        if flatten:
            reference, at, reason = bar.open, bar.starts_at, "session_exit"
        elif long and bar.low <= intent.stop_price:
            reference = min(bar.open, intent.stop_price)
            at = bar.starts_at if bar.open <= intent.stop_price else bar.ends_at
            reason = "protective_stop"
        elif not long and bar.high >= intent.stop_price:
            reference = max(bar.open, intent.stop_price)
            at = bar.starts_at if bar.open >= intent.stop_price else bar.ends_at
            reason = "protective_stop"
        else:
            return None
        with localcontext(Context(prec=28)):
            price = reference - self.policy.slippage_per_share if long else (
                reference + self.policy.slippage_per_share
            )
            if price <= 0:
                raise DataError("DATA_INVALID", "Simulation would produce a nonpositive exit price.")
            return SimulatedFill(
                at, quantity, price, quantity * self.policy.fee_per_share, reason,
            )


@dataclass(frozen=True, slots=True)
class BacktestInput:
    instrument: Instrument
    bars: tuple[MarketBar, ...]
    quotes: tuple[Quote, ...]
    sessions: tuple[ExchangeSession, ...]
    eligibility: EligibilityPolicy
    benchmark: BenchmarkConfig = BenchmarkConfig()
    risk: RiskPolicy = RiskPolicy()
    simulation: SimulationPolicy = SimulationPolicy()
    initial_equity: Decimal = Decimal("10000")
    dataset_label: str = "operator-supplied"

    def __post_init__(self) -> None:
        decimal_amount(self.initial_equity, "initial equity", positive=True)
        if not self.dataset_label.strip():
            raise DataError("DATA_INVALID", "Dataset label is required.")
        if not self.bars or not self.sessions:
            raise DataError("DATA_MISSING", "Backtest requires bars and verified sessions.")
        if any(
            session.calendar != self.instrument.market_calendar for session in self.sessions
        ) or any(
            right.opens_at <= left.closes_at
            for left, right in zip(self.sessions, self.sessions[1:])
        ):
            raise DataError("CALENDAR_UNKNOWN", "Invalid or overlapping exchange sessions.")
        validate_instrument(
            self.instrument, RuntimeConfig(watchlist=(self.instrument.symbol,)), self.eligibility,
        )
        validate_bars(
            self.bars, self.instrument, self.sessions, self.bars[-1].ends_at, self.bars[0].source,
        )
        for quote in self.quotes:
            if (
                quote.instrument_id != self.instrument.instrument_id
                or quote.source != self.bars[0].source
                or quote.observed_at > quote.received_at
            ):
                raise DataError("DATA_INVALID", "Archived quote identity/source/time mismatch.")
        if any(
            right.received_at <= left.received_at
            or right.observed_at < left.observed_at
            for left, right in zip(self.quotes, self.quotes[1:])
        ):
            raise DataError("DATA_INVALID", "Archived quotes must be ordered and nonduplicated.")
        with localcontext(Context(prec=28)):
            minimum_cost = 2 * (self.simulation.fee_per_share + self.simulation.slippage_per_share)
        if self.risk.round_trip_cost_per_share < minimum_cost:
            raise DataError("COST_ASSUMPTIONS", "Risk allowance must cover simulated fees/slippage.")


@dataclass(frozen=True, slots=True)
class ReplayEvent:
    at: datetime
    kind: str
    code: str
    detail: str
    correlation_id: str = ""
    quantity: Decimal = Decimal("0")
    price: Decimal = Decimal("0")
    fee: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class EquityPoint:
    at: datetime
    equity: Decimal


@dataclass(frozen=True, slots=True)
class BacktestReport:
    schema_version: str
    input_fingerprint: str
    source_fingerprint: str
    strategy_version: str
    risk_version: str
    order_intents: tuple[OrderIntent, ...]
    events: tuple[ReplayEvent, ...]
    equity_curve: tuple[EquityPoint, ...]
    initial_equity: Decimal
    final_equity: Decimal
    realized_pnl: Decimal
    total_fees: Decimal
    max_drawdown: Decimal
    complete: bool
    open_quantity: Decimal
    circuit_states: tuple[CircuitState, ...]
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Position:
    intent: OrderIntent
    quantity: Decimal
    entry_price: Decimal
    entry_fee: Decimal


class BacktestRunner:
    """Single-symbol, USD, unleveraged long-only research using Phase 1 signals."""

    def run(self, data: BacktestInput) -> BacktestReport:
        with localcontext(Context(prec=28)):
            return self._run(data)

    def _run(self, data: BacktestInput) -> BacktestReport:
        runtime = RuntimeConfig(watchlist=(data.instrument.symbol,))
        strategy = BenchmarkStrategy(data.benchmark)
        warden = RiskWarden(data.risk)
        simulator = FillSimulator(data.simulation)
        events: list[ReplayEvent] = []
        intents: list[OrderIntent] = []
        curve: list[EquityPoint] = []
        states: list[CircuitState] = []
        cash = data.initial_equity
        realized = Decimal(0)
        fees = Decimal(0)
        peak = cash
        max_drawdown = Decimal(0)
        position: _Position | None = None
        pending: OrderIntent | None = None
        pending_signal = None
        circuit: DailyCircuitBreaker | None = None
        active_session: ExchangeSession | None = None
        quote_index = 0
        quote: Quote | None = None
        seen_intents: set[str] = set()

        def emit(at: datetime, kind: str, code: str, detail: str, intent: OrderIntent | None = None,
                 fill: SimulatedFill | None = None, correlation_id: str = "") -> None:
            events.append(ReplayEvent(
                at, kind, code, detail, intent.correlation_id if intent else correlation_id,
                fill.quantity if fill else Decimal(0), fill.price if fill else Decimal(0),
                fill.fee if fill else Decimal(0),
            ))

        def latest_quote(at: datetime) -> Quote | None:
            nonlocal quote_index, quote
            while quote_index < len(data.quotes) and data.quotes[quote_index].received_at <= at:
                quote = data.quotes[quote_index]
                quote_index += 1
            return quote

        for index, bar in enumerate(data.bars):
            session = next(s for s in data.sessions
                           if s.opens_at <= bar.starts_at and bar.ends_at <= s.closes_at)
            if active_session != session:
                if position is not None:
                    raise DataError("OVERNIGHT_POSITION", "Prior session did not confirm flattening.")
                if circuit is not None:
                    states.append(circuit.state)
                active_session = session
                circuit = DailyCircuitBreaker(CircuitState(
                    session.opens_at, session.closes_at, cash,
                ))
            assert circuit is not None
            open_equity = cash + (position.quantity * bar.open if position else Decimal(0))
            if open_equity <= 0:
                raise DataError("ACCOUNT_INSOLVENT", "Simulation equity is nonpositive.")
            was_tripped = circuit.state.tripped
            tripped = circuit.observe(open_equity, bar.starts_at, data.risk.daily_drawdown)
            if tripped and not was_tripped:
                emit(bar.starts_at, "halted", "DAILY_DRAWDOWN",
                     "Entry halt latched at open; protective exits preserved.")
            opening_quote = latest_quote(bar.starts_at)
            if pending is not None:
                if tripped:
                    emit(bar.starts_at, "canceled", "DAILY_DRAWDOWN", "Cancel pending entry.", pending)
                elif opening_quote is None or pending_signal is None:
                    emit(bar.starts_at, "canceled", "DATA_MISSING", "No archived entry-time quote.", pending)
                else:
                    account = AccountSnapshot(
                        "simulation", RuntimeMode.BACKTEST, open_equity, max(cash, Decimal(0)),
                        "USD", bar.starts_at,
                    )
                    recheck = warden.evaluate(
                        pending_signal, data.instrument, opening_quote, account, (), circuit,
                        session, runtime, data.eligibility, bar.starts_at,
                        entry_limit=pending.limit_price, reconciled=True,
                        protective_exit_supported=True,
                    )
                    if recheck.intent is None:
                        emit(bar.starts_at, "canceled", recheck.decision.reason_codes[0],
                             recheck.explanation, pending)
                    else:
                        intent = replace(pending, quantity=min(pending.quantity, recheck.intent.quantity))
                        fill = simulator.entry(
                            intent, bar, data.risk.quantity_step,
                            available_volume=data.bars[index - 1].volume,
                        )
                        if fill is None:
                            emit(bar.ends_at, "expired", "LIMIT_UNFILLED",
                                 "Open-only limit not marketable or insufficient modeled volume.", intent)
                        else:
                            cash -= fill.quantity * fill.price + fill.fee
                            fees += fill.fee
                            position = _Position(intent, fill.quantity, fill.price, fill.fee)
                            emit(fill.at, "fill", "ENTRY", "Simulated bounded entry.", intent, fill)
                            if fill.quantity < intent.quantity:
                                emit(bar.ends_at, "canceled", "PARTIAL_REMAINDER",
                                     "Unfilled remainder canceled; filled quantity remains protected.", intent)
                pending, pending_signal = None, None
            if position is not None:
                exit_fill = simulator.exit(
                    position.intent, position.quantity, bar,
                    flatten=bar.starts_at >= session.closes_at - timedelta(minutes=5),
                )
                if exit_fill is not None:
                    cash += exit_fill.quantity * exit_fill.price - exit_fill.fee
                    realized += (
                        exit_fill.quantity * (exit_fill.price - position.entry_price)
                        - position.entry_fee - exit_fill.fee
                    )
                    fees += exit_fill.fee
                    emit(exit_fill.at, "fill", exit_fill.reason.upper(),
                         "Simulated exit; gap/slippage may exceed planned risk.",
                         position.intent, exit_fill)
                    position = None
            equity = cash + (position.quantity * bar.close if position else Decimal(0))
            if equity <= 0:
                raise DataError("ACCOUNT_INSOLVENT", "Simulation equity is nonpositive.")
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
            curve.append(EquityPoint(bar.ends_at, equity))
            if bar.ends_at < session.closes_at:
                was_tripped = circuit.state.tripped
                circuit.observe(equity, bar.ends_at, data.risk.daily_drawdown)
                if circuit.state.tripped and not was_tripped:
                    emit(bar.ends_at, "halted", "DAILY_DRAWDOWN",
                         "Entry halt latched; protective exits preserved.")
            current_quote = latest_quote(bar.ends_at)
            if current_quote is None:
                emit(bar.ends_at, "rejected", "DATA_MISSING", "No archived quote available.")
                continue
            snapshot = MarketSnapshot(
                data.bars[:index + 1], current_quote, bar.ends_at, bar.source,
            )
            correlation = f"backtest:{data.instrument.instrument_id}:{bar.ends_at.isoformat()}"
            evaluation = strategy.evaluate(
                data.instrument, snapshot, data.sessions, runtime, data.eligibility,
                bar.ends_at, correlation,
            )
            if evaluation.candidate is None:
                emit(bar.ends_at, evaluation.status.value, evaluation.code, evaluation.explanation)
                continue
            account = AccountSnapshot(
                "simulation", RuntimeMode.BACKTEST, equity, max(cash, Decimal(0)), "USD", bar.ends_at,
            )
            exposure = () if position is None else (Exposure(
                position.intent.instrument_id, TradeDirection.LONG,
                position.quantity * bar.close,
                position.quantity * (
                    max(position.entry_price - position.intent.stop_price, Decimal(0))
                    + data.risk.round_trip_cost_per_share
                ),
            ),)
            outcome = warden.evaluate(
                evaluation.candidate, data.instrument, current_quote, account, exposure,
                circuit, session, runtime, data.eligibility, bar.ends_at,
                reconciled=True, protective_exit_supported=True,
            )
            emit(
                bar.ends_at, "risk", "APPROVED" if outcome.intent else outcome.decision.reason_codes[0],
                outcome.explanation + " " + "; ".join(outcome.checks), outcome.intent,
                correlation_id=correlation,
            )
            if outcome.intent is not None:
                if outcome.intent.idempotency_key in seen_intents:
                    raise DataError("DUPLICATE_INTENT", "Duplicate simulated order intent.")
                seen_intents.add(outcome.intent.idempotency_key)
                intents.append(outcome.intent)
                pending, pending_signal = outcome.intent, evaluation.candidate
        if pending is not None:
            emit(data.bars[-1].ends_at, "canceled", "END_OF_DATA", "No next bar to simulate entry.", pending)
        if position is not None:
            emit(data.bars[-1].ends_at, "unresolved", "OPEN_POSITION",
                 "Dataset ended before a confirmed exit; equity includes unrealized P&L.",
                 position.intent)
        if circuit is not None:
            states.append(circuit.state)
        data_failures = {"DATA_MISSING", "DATA_STALE", "DATA_INVALID", "DATA_FUTURE", "CALENDAR_UNKNOWN"}
        complete = (
            position is None and pending is None
            and not any(event.code in data_failures for event in events)
        )
        return BacktestReport(
            "1", fingerprint(data), source_version(), data.benchmark.version, data.risk.version,
            tuple(intents), tuple(sorted(events, key=lambda event: event.at)),
            tuple(curve), data.initial_equity, curve[-1].equity,
            realized, fees, max_drawdown, complete,
            position.quantity if position else Decimal(0), tuple(states),
            (
                "Experimental, single-symbol, USD, unleveraged long-only simulation; not predictive.",
                "Archived quotes required; no artificial bid/ask from close prices.",
                "Open-only limit fills; prior completed bar volume is a liquidity proxy, not queue simulation.",
                "Unfilled remainder canceled after one bar; stops cover only filled quantity.",
                "Intrabar stop timestamp is recorded at bar end; path/queue position is unknown.",
                "Stop exits assume full liquidity; gaps/slippage can exceed planned loss.",
                "Calendar sessions, corporate actions and data quality must be verified by the operator.",
                "No live/paper broker connection, AI calls, dividends, borrow, or FX simulation.",
            ),
        )
