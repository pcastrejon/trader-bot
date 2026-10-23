"""Deterministic sizing and order-intent gate. No execution or model access."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Context, Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
from hashlib import sha256

from trader_bot.config import RuntimeConfig
from trader_bot.contracts import (
    AccountSnapshot, CandidateSignal, Instrument, Quote, RiskDecision, RuntimeMode,
    TradeDirection,
)
from trader_bot.market_data import DataError, utc_time
from trader_bot.strategy import EligibilityPolicy, ExchangeSession, validate_instrument


def decimal_amount(value: Decimal, name: str, *, positive: bool = False) -> None:
    if (
        not isinstance(value, Decimal) or not value.is_finite()
        or value < 0 or (positive and value == 0)
    ):
        raise DataError("DATA_INVALID", f"{name} must be a finite {'positive' if positive else 'non-negative'} Decimal.")


def round_step(value: Decimal, step: Decimal, *, up: bool = False) -> Decimal:
    decimal_amount(step, "step", positive=True)
    with localcontext(Context(prec=28)):
        return (value / step).to_integral_value(
            rounding=ROUND_CEILING if up else ROUND_FLOOR
        ) * step


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    per_trade_risk: Decimal = Decimal("0.0025")
    position_notional: Decimal = Decimal("0.05")
    open_risk: Decimal = Decimal("0.01")
    daily_drawdown: Decimal = Decimal("0.01")
    paper_gross: Decimal = Decimal("1.25")
    short_total: Decimal = Decimal("0.10")
    short_position: Decimal = Decimal("0.025")
    quantity_step: Decimal = Decimal("1")
    price_tick: Decimal = Decimal("0.01")
    round_trip_cost_per_share: Decimal = Decimal("0")
    max_entry_deviation_bps: Decimal = Decimal("10")
    max_account_age: timedelta = timedelta(seconds=30)

    def __post_init__(self) -> None:
        for name in (
            "per_trade_risk", "position_notional", "open_risk", "daily_drawdown",
            "paper_gross", "short_total", "short_position", "quantity_step", "price_tick",
            "max_entry_deviation_bps",
        ):
            decimal_amount(getattr(self, name), name, positive=True)
        for name in (
            "per_trade_risk", "position_notional", "open_risk", "daily_drawdown",
            "short_total", "short_position",
        ):
            if getattr(self, name) > 1:
                raise DataError("DATA_INVALID", f"{name} must not exceed one.")
        decimal_amount(self.round_trip_cost_per_share, "round_trip_cost_per_share")
        if self.max_account_age <= timedelta(0):
            raise DataError("DATA_INVALID", "Account freshness must be positive.")

    @property
    def version(self) -> str:
        values = (
            self.per_trade_risk, self.position_notional, self.open_risk,
            self.daily_drawdown, self.paper_gross, self.short_total, self.short_position,
            self.quantity_step, self.price_tick, self.round_trip_cost_per_share,
            self.max_entry_deviation_bps, self.max_account_age.total_seconds(),
        )
        return "risk-v1:" + sha256("|".join(map(str, values)).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Exposure:
    """Normalized marked gross notional and planned loss, including reserved entries."""

    instrument_id: str
    direction: TradeDirection
    notional: Decimal
    planned_risk: Decimal
    currency: str = "USD"

    def __post_init__(self) -> None:
        if not self.instrument_id.strip() or not isinstance(self.direction, TradeDirection):
            raise DataError("DATA_INVALID", "Exposure needs instrument and valid direction.")
        decimal_amount(self.notional, "exposure notional", positive=True)
        decimal_amount(self.planned_risk, "exposure planned risk")


@dataclass(frozen=True, slots=True)
class CircuitState:
    session_open: datetime
    session_close: datetime
    start_equity: Decimal
    tripped: bool = False

    def __post_init__(self) -> None:
        if utc_time(self.session_close) <= utc_time(self.session_open):
            raise DataError("CALENDAR_UNKNOWN", "Invalid circuit-breaker session.")
        decimal_amount(self.start_equity, "start equity", positive=True)
        if not isinstance(self.tripped, bool):
            raise DataError("DATA_INVALID", "Circuit latch must be boolean.")


class DailyCircuitBreaker:
    """Explicit restorable state; a tripped session never automatically resumes."""

    def __init__(self, state: CircuitState) -> None:
        self.state = state

    def observe(self, equity: Decimal, at: datetime, limit: Decimal) -> bool:
        decimal_amount(equity, "equity", positive=True)
        decimal_amount(limit, "drawdown limit", positive=True)
        if not self.state.session_open <= utc_time(at) < self.state.session_close:
            raise DataError("SESSION_BASELINE_UNKNOWN", "Circuit baseline does not cover this session.")
        with localcontext(Context(prec=28)):
            breached = equity <= self.state.start_equity * (1 - limit)
        if breached and not self.state.tripped:
            self.state = CircuitState(
                self.state.session_open, self.state.session_close, self.state.start_equity, True,
            )
        return self.state.tripped


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """Offline limit-entry/stop-market plan; never an executable broker command."""

    correlation_id: str
    instrument_id: str
    mode: RuntimeMode
    direction: TradeDirection
    quantity: Decimal
    limit_price: Decimal
    stop_price: Decimal
    created_at: datetime
    expires_at: datetime
    idempotency_key: str
    risk_rule_version: str
    planned_risk: Decimal
    currency: str = "USD"
    entry_type: str = "limit"
    protective_type: str = "stop_market"

    def __post_init__(self) -> None:
        if not isinstance(self.mode, RuntimeMode) or self.mode is RuntimeMode.LIVE:
            raise DataError("MODE_FORBIDDEN", "Live order intents are unavailable.")
        if not isinstance(self.direction, TradeDirection):
            raise DataError("DATA_INVALID", "Intent direction is invalid.")
        for name in ("correlation_id", "instrument_id", "idempotency_key", "risk_rule_version"):
            if not getattr(self, name).strip():
                raise DataError("DATA_INVALID", f"Intent {name} is required.")
        for name in ("quantity", "limit_price", "stop_price", "planned_risk"):
            decimal_amount(getattr(self, name), name, positive=True)
        if utc_time(self.expires_at) <= utc_time(self.created_at):
            raise DataError("SIGNAL_EXPIRED", "Intent must expire after creation.")
        if (
            self.entry_type != "limit" or self.protective_type != "stop_market"
            or self.currency != "USD"
        ):
            raise DataError("ORDER_UNSUPPORTED", "Only USD limit/stop-market plans supported.")
        if (
            self.direction is TradeDirection.LONG and self.stop_price >= self.limit_price
            or self.direction is TradeDirection.SHORT and self.stop_price <= self.limit_price
        ):
            raise DataError("DATA_INVALID", "Stop must be on the loss side of entry.")


@dataclass(frozen=True, slots=True)
class RiskOutcome:
    decision: RiskDecision
    explanation: str
    checks: tuple[str, ...]
    intent: OrderIntent | None = None


class RiskWarden:
    def __init__(self, policy: RiskPolicy | None = None) -> None:
        self.policy = policy if policy is not None else RiskPolicy()

    def evaluate(
        self, signal: CandidateSignal, instrument: Instrument, quote: Quote,
        account: AccountSnapshot, exposures: tuple[Exposure, ...],
        circuit: DailyCircuitBreaker, session: ExchangeSession, runtime: RuntimeConfig,
        eligibility: EligibilityPolicy, as_of: datetime, *,
        reconciled: bool, protective_exit_supported: bool,
        entry_limit: Decimal | None = None, borrow_available: bool = False,
        borrow_cost_per_share: Decimal | None = None,
        entry_notional_cap: Decimal | None = None,
    ) -> RiskOutcome:
        with localcontext(Context(prec=28)):
            return self._evaluate(
                signal, instrument, quote, account, exposures, circuit, session, runtime,
                eligibility, as_of, entry_limit, reconciled, protective_exit_supported,
                borrow_available, borrow_cost_per_share, entry_notional_cap,
            )

    def _evaluate(
        self, signal: CandidateSignal, instrument: Instrument, quote: Quote,
        account: AccountSnapshot, exposures: tuple[Exposure, ...],
        circuit: DailyCircuitBreaker, session: ExchangeSession, runtime: RuntimeConfig,
        eligibility: EligibilityPolicy, as_of: datetime, entry_limit: Decimal | None,
        reconciled: bool, protective_exit_supported: bool, borrow_available: bool,
        borrow_cost_per_share: Decimal | None, entry_notional_cap: Decimal | None,
    ) -> RiskOutcome:
        now = utc_time(as_of)
        policy = self.policy
        checks: list[str] = []
        try:
            validate_instrument(instrument, runtime, eligibility)
            if account.mode is not runtime.mode:
                raise DataError("MODE_FORBIDDEN", "Account and runtime mode mismatch.")
            if account.currency != "USD" or any(e.currency != "USD" for e in exposures):
                raise DataError("CURRENCY_UNSUPPORTED", "No unverified FX conversion is allowed.")
            if not reconciled:
                raise DataError("BROKER_STATE_UNKNOWN", "Account/orders/positions are not reconciled.")
            if not protective_exit_supported:
                raise DataError("PROTECTIVE_EXIT_UNAVAILABLE", "Stop-market coverage is required.")
            if (
                signal.instrument_id != instrument.instrument_id
                or quote.instrument_id != instrument.instrument_id
            ):
                raise DataError("DATA_INVALID", "Signal, instrument and quote identity must match.")
            if not signal.signal_bar_ends_at <= now < min(
                signal.expires_at, signal.signal_bar_ends_at + timedelta(minutes=5),
            ):
                raise DataError("SIGNAL_EXPIRED", "Signal is future-dated or expired.")
            if (
                account.as_of > now or now - account.as_of > policy.max_account_age
                or quote.observed_at > quote.received_at or quote.received_at > now
                or now - quote.observed_at > eligibility.max_quote_age
            ):
                raise DataError("DATA_STALE", "Account or quote timestamp is invalid/stale.")
            if session.calendar != instrument.market_calendar:
                raise DataError("CALENDAR_UNKNOWN", "Instrument/session calendar mismatch.")
            if not session.opens_at <= now < session.closes_at:
                raise DataError("MARKET_CLOSED", "No active exchange session.")
            if now >= session.closes_at - timedelta(minutes=30):
                raise DataError("SESSION_CUTOFF", "New entries stop thirty minutes before close.")
            if (
                circuit.state.session_open != session.opens_at
                or circuit.state.session_close != session.closes_at
            ):
                raise DataError("SESSION_BASELINE_UNKNOWN", "Circuit baseline/session mismatch.")
            if circuit.observe(account.equity, now, policy.daily_drawdown):
                raise DataError("DAILY_DRAWDOWN", "Daily breaker latched; cancel entries, preserve exits.")
            checks.append("identity/mode/state/freshness/session/circuit passed")
            if any(e.instrument_id == instrument.instrument_id for e in exposures):
                raise DataError("POSITION_EXISTS", "No pyramiding, hedging or duplicate reserved entry.")
            if min(quote.bid, quote.ask) < eligibility.min_price:
                raise DataError("LIQUIDITY_FILTER", "Quote price below configured minimum.")
            spread = (quote.ask - quote.bid) / ((quote.ask + quote.bid) / 2) * 10000
            if spread > eligibility.max_spread_bps:
                raise DataError("LIQUIDITY_FILTER", "Quote spread exceeds configured maximum.")
            is_short = signal.direction is TradeDirection.SHORT
            borrow_cost = Decimal(0)
            if is_short:
                if runtime.mode is not RuntimeMode.PAPER or not runtime.paper_allow_shorting:
                    raise DataError("MODE_FORBIDDEN", "Shorting requires explicit paper-only permission.")
                if not borrow_available or borrow_cost_per_share is None:
                    raise DataError("BORROW_UNAVAILABLE", "Borrow availability and cost must be explicit.")
                decimal_amount(borrow_cost_per_share, "borrow cost")
                borrow_cost = borrow_cost_per_share
            reference = quote.bid if is_short else quote.ask
            limit = reference if entry_limit is None else entry_limit
            decimal_amount(limit, "entry limit", positive=True)
            if entry_notional_cap is not None:
                decimal_amount(entry_notional_cap, "entry notional cap", positive=True)
            if abs(limit - reference) / reference * 10000 > policy.max_entry_deviation_bps:
                raise DataError("PRICE_BOUND", "Entry limit exceeds quote deviation bound.")
            limit = round_step(limit, policy.price_tick, up=is_short)
            if limit <= 0:
                raise DataError("PRICE_BOUND", "Price tick would round the entry limit to zero.")
            stop = round_step(signal.protective_stop_price, policy.price_tick, up=is_short)
            distance = stop - limit if is_short else limit - stop
            if stop <= 0 or distance <= 0:
                raise DataError("STOP_INVALID", "Rounded stop must be on loss side of entry.")
            unit_risk = distance + policy.round_trip_cost_per_share + borrow_cost
            gross = sum((e.notional for e in exposures), Decimal(0))
            open_risk = sum((e.planned_risk for e in exposures), Decimal(0))
            short_gross = sum(
                (e.notional for e in exposures if e.direction is TradeDirection.SHORT), Decimal(0),
            )
            if short_gross > 0 and runtime.mode is not RuntimeMode.PAPER:
                raise DataError("MODE_FORBIDDEN", "Existing short exposure is paper-only.")
            if short_gross > account.equity * policy.short_total:
                raise DataError("SHORT_CAP", "Existing aggregate short exposure exceeds its cap.")
            gross_cap = policy.paper_gross if (
                runtime.mode is RuntimeMode.PAPER and runtime.paper_allow_leverage
            ) else Decimal(1)
            capacities = {
                "RISK_PER_TRADE": account.equity * policy.per_trade_risk / unit_risk,
                "POSITION_NOTIONAL": account.equity * policy.position_notional / limit,
                "OPEN_RISK": (account.equity * policy.open_risk - open_risk) / unit_risk,
                "GROSS_EXPOSURE": (account.equity * gross_cap - gross) / limit,
                "BUYING_POWER": account.buying_power / (limit + policy.round_trip_cost_per_share),
            }
            if entry_notional_cap is not None:
                capacities["ENTRY_NOTIONAL_CAP"] = entry_notional_cap / limit
            if is_short:
                capacities["SHORT_CAP"] = min(
                    account.equity * policy.short_position / limit,
                    (account.equity * policy.short_total - short_gross) / limit,
                )
            binding = min(capacities, key=lambda key: capacities[key])
            quantity = round_step(max(capacities[binding], Decimal(0)), policy.quantity_step)
            if quantity <= 0:
                raise DataError(binding, f"No valid quantity remains under {binding}.")
            planned_risk = quantity * unit_risk
            notional = quantity * limit
            checks.extend((
                f"equity={account.equity}; start_equity={circuit.state.start_equity}; USD",
                f"unit_risk={unit_risk}; binding={binding}; step={policy.quantity_step}",
                f"reserved/open gross={gross}; planned risk={open_risk}; shorts={short_gross}",
                f"quantity={quantity}; notional={notional}; planned_risk={planned_risk}",
            ))
            key = sha256(
                f"{runtime.mode.value}|{signal.correlation_id}|{instrument.instrument_id}|"
                f"{signal.strategy_version}|{signal.signal_bar_ends_at.isoformat()}|"
                f"{signal.direction.value}".encode()
            ).hexdigest()
            intent = OrderIntent(
                signal.correlation_id, instrument.instrument_id, runtime.mode, signal.direction,
                quantity, limit, stop, now,
                min(signal.expires_at, session.closes_at - timedelta(minutes=30)),
                key, policy.version, planned_risk,
            )
            decision = RiskDecision(
                signal.correlation_id, runtime.mode, True, (), policy.version,
                now, account.equity, account.currency, quantity, notional, planned_risk,
            )
            return RiskOutcome(decision, "All deterministic entry checks passed.", tuple(checks), intent)
        except DataError as exc:
            decision = RiskDecision(
                signal.correlation_id, runtime.mode, False, (exc.code,), policy.version,
                now, account.equity, account.currency,
            )
            return RiskOutcome(decision, str(exc), tuple(checks))
