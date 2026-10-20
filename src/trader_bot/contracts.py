"""Typed, validated domain records and analyst-agent contracts.

These records define Phase 0 boundaries only. They do not implement strategy,
risk approval, data access, model inference, or broker execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class ContractError(ValueError):
    """Raised when a domain record violates its contract."""


class AssetType(StrEnum):
    STOCK = "stock"
    ETF = "etf"


class TradeDirection(StrEnum):
    LONG = "long"
    SHORT = "short"


class AgentRole(StrEnum):
    TECHNICAL_CONTEXT = "technical_context"
    FUNDAMENTAL_CONTEXT = "fundamental_context"
    NEWS_SENTIMENT = "news_sentiment"


class AgentStatus(StrEnum):
    OK = "ok"
    INSUFFICIENT_DATA = "insufficient_data"
    TIMED_OUT = "timed_out"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


class Uncertainty(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"


class OrderStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class RuntimeMode(StrEnum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class InputReference:
    source: str
    reference: str
    observed_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.source, "source")
        _require_text(self.reference, "reference")
        _require_aware(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True)
class Instrument:
    instrument_id: str
    symbol: str
    asset_type: AssetType
    venue: str
    currency: str
    supported_modes: tuple[str, ...]
    market_calendar: str

    def __post_init__(self) -> None:
        for name in ("instrument_id", "symbol", "venue", "currency", "market_calendar"):
            _require_text(getattr(self, name), name)
        if not self.currency.isalpha() or len(self.currency) != 3:
            raise ContractError("currency must be a three-letter currency code.")
        if not isinstance(self.supported_modes, tuple):
            raise ContractError("supported_modes must be a tuple.")
        if not self.supported_modes:
            raise ContractError("supported_modes must contain at least one mode.")
        if len(set(self.supported_modes)) != len(self.supported_modes):
            raise ContractError("supported_modes must not contain duplicates.")
        if any(
            mode not in (RuntimeMode.BACKTEST.value, RuntimeMode.PAPER.value)
            for mode in self.supported_modes
        ):
            raise ContractError(
                "supported_modes may contain only backtest and paper in this release."
            )


@dataclass(frozen=True, slots=True)
class MarketBar:
    instrument_id: str
    interval_seconds: int
    starts_at: datetime
    ends_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source: str
    adjustment_basis: str

    def __post_init__(self) -> None:
        _require_text(self.instrument_id, "instrument_id")
        _require_text(self.source, "source")
        _require_text(self.adjustment_basis, "adjustment_basis")
        _require_aware(self.starts_at, "starts_at")
        _require_aware(self.ends_at, "ends_at")
        if self.interval_seconds <= 0:
            raise ContractError("interval_seconds must be positive.")
        if self.ends_at <= self.starts_at:
            raise ContractError("ends_at must be later than starts_at.")
        prices = (self.open, self.high, self.low, self.close)
        if any(not value.is_finite() or value <= 0 for value in prices):
            raise ContractError("OHLC prices must be finite and greater than zero.")
        if self.high < max(self.open, self.close, self.low):
            raise ContractError("high must be at least open, close, and low.")
        if self.low > min(self.open, self.close, self.high):
            raise ContractError("low must be at most open, close, and high.")
        if not self.volume.is_finite() or self.volume < 0:
            raise ContractError("volume must be finite and non-negative.")


@dataclass(frozen=True, slots=True)
class Quote:
    instrument_id: str
    bid: Decimal
    ask: Decimal
    observed_at: datetime
    received_at: datetime
    source: str
    bid_size: Decimal | None = None
    ask_size: Decimal | None = None

    def __post_init__(self) -> None:
        _require_text(self.instrument_id, "instrument_id")
        _require_text(self.source, "source")
        _require_aware(self.observed_at, "observed_at")
        _require_aware(self.received_at, "received_at")
        if not self.bid.is_finite() or not self.ask.is_finite():
            raise ContractError("bid and ask must be finite.")
        if self.bid <= 0 or self.ask <= 0:
            raise ContractError("bid and ask must be greater than zero.")
        if self.bid > self.ask:
            raise ContractError("bid must not exceed ask.")
        for field_name in ("bid_size", "ask_size"):
            size = getattr(self, field_name)
            if size is not None and (not size.is_finite() or size < 0):
                raise ContractError(f"{field_name} must be finite and non-negative.")


@dataclass(frozen=True, slots=True)
class AgentRequest:
    schema_version: str
    correlation_id: str
    role: AgentRole
    instrument_id: str
    created_at: datetime
    deadline: datetime
    input_refs: tuple[InputReference, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.role, AgentRole):
            raise ContractError("role must be an AgentRole.")
        _require_text(self.schema_version, "schema_version")
        _require_text(self.correlation_id, "correlation_id")
        _require_text(self.instrument_id, "instrument_id")
        _require_aware(self.created_at, "created_at")
        _require_aware(self.deadline, "deadline")
        if self.deadline <= self.created_at:
            raise ContractError("deadline must be later than created_at.")


@dataclass(frozen=True, slots=True)
class AgentResult:
    schema_version: str
    correlation_id: str
    role: AgentRole
    status: AgentStatus
    instrument_id: str
    created_at: datetime
    model_provider: str
    model_name: str
    model_version: str
    input_refs: tuple[InputReference, ...]
    summary: str
    uncertainty: Uncertainty
    veto: bool = False
    insufficient_data: bool = False
    warnings: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.role, AgentRole):
            raise ContractError("role must be an AgentRole.")
        if not isinstance(self.status, AgentStatus):
            raise ContractError("status must be an AgentStatus.")
        if not isinstance(self.uncertainty, Uncertainty):
            raise ContractError("uncertainty must be an Uncertainty.")
        if not isinstance(self.veto, bool) or not isinstance(
            self.insufficient_data, bool
        ):
            raise ContractError("veto and insufficient_data must be booleans.")
        for name in (
            "schema_version",
            "correlation_id",
            "instrument_id",
            "model_provider",
            "model_name",
            "model_version",
        ):
            _require_text(getattr(self, name), name)
        _require_aware(self.created_at, "created_at")
        if self.status is AgentStatus.INSUFFICIENT_DATA and not self.insufficient_data:
            raise ContractError(
                "insufficient_data status requires insufficient_data=True."
            )
        if self.status is AgentStatus.OK and self.insufficient_data:
            raise ContractError(
                "An ok agent result cannot set insufficient_data=True."
            )
        if self.status is not AgentStatus.OK and self.veto:
            raise ContractError("Only a valid agent result may set veto=True.")
        if self.status is not AgentStatus.OK and not self.summary:
            raise ContractError("Non-ok agent results must explain their status.")
        if any(not warning.strip() for warning in self.warnings):
            raise ContractError("warnings must not contain empty values.")
        if any(not reference.strip() for reference in self.source_refs):
            raise ContractError("source_refs must not contain empty values.")


@dataclass(frozen=True, slots=True)
class AgentAnalysisReport:
    correlation_id: str
    instrument_id: str
    created_at: datetime
    results: tuple[AgentResult, ...]

    def __post_init__(self) -> None:
        _require_text(self.correlation_id, "correlation_id")
        _require_text(self.instrument_id, "instrument_id")
        _require_aware(self.created_at, "created_at")
        roles = [result.role for result in self.results]
        if len(roles) != len(set(roles)):
            raise ContractError("An analysis report may contain each agent role once.")
        if any(
            result.correlation_id != self.correlation_id
            or result.instrument_id != self.instrument_id
            for result in self.results
        ):
            raise ContractError(
                "Agent results must match the report correlation and instrument."
            )


@dataclass(frozen=True, slots=True)
class CandidateSignal:
    correlation_id: str
    instrument_id: str
    strategy_id: str
    strategy_version: str
    direction: TradeDirection
    signal_bar_ends_at: datetime
    rule_evidence: tuple[str, ...]
    protective_stop_price: Decimal
    expires_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.direction, TradeDirection):
            raise ContractError("direction must be a TradeDirection.")
        for name in (
            "correlation_id",
            "instrument_id",
            "strategy_id",
            "strategy_version",
        ):
            _require_text(getattr(self, name), name)
        _require_aware(self.signal_bar_ends_at, "signal_bar_ends_at")
        _require_aware(self.expires_at, "expires_at")
        if self.expires_at <= self.signal_bar_ends_at:
            raise ContractError("expires_at must be later than the signal bar.")
        if not self.protective_stop_price.is_finite() or self.protective_stop_price <= 0:
            raise ContractError("protective_stop_price must be finite and positive.")
        if not self.rule_evidence or any(not item.strip() for item in self.rule_evidence):
            raise ContractError("rule_evidence must contain non-empty evidence.")


@dataclass(frozen=True, slots=True)
class OrderProposal:
    correlation_id: str
    instrument_id: str
    direction: TradeDirection
    quantity: Decimal
    notional: Decimal
    currency: str
    created_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.direction, TradeDirection):
            raise ContractError("direction must be a TradeDirection.")
        for name in ("correlation_id", "instrument_id", "currency"):
            _require_text(getattr(self, name), name)
        _require_aware(self.created_at, "created_at")
        _require_aware(self.expires_at, "expires_at")
        if self.expires_at <= self.created_at:
            raise ContractError("expires_at must be later than created_at.")
        if not self.quantity.is_finite() or self.quantity <= 0:
            raise ContractError("quantity must be finite and positive.")
        if not self.notional.is_finite() or self.notional <= 0:
            raise ContractError("notional must be finite and positive.")


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    account_reference: str
    mode: RuntimeMode
    equity: Decimal
    buying_power: Decimal
    currency: str
    as_of: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.mode, RuntimeMode):
            raise ContractError("mode must be a RuntimeMode.")
        _require_text(self.account_reference, "account_reference")
        _require_text(self.currency, "currency")
        _require_aware(self.as_of, "as_of")
        if self.mode is RuntimeMode.LIVE:
            raise ContractError("Live account snapshots are not supported in this release.")
        if not self.equity.is_finite() or self.equity <= 0:
            raise ContractError("equity must be finite and positive.")
        if not self.buying_power.is_finite() or self.buying_power < 0:
            raise ContractError("buying_power must be finite and non-negative.")


@dataclass(frozen=True, slots=True)
class RiskDecision:
    correlation_id: str
    mode: RuntimeMode
    allowed: bool
    reason_codes: tuple[str, ...]
    rule_version: str
    evaluated_at: datetime
    account_equity: Decimal
    currency: str
    quantity: Decimal | None = None
    notional: Decimal | None = None
    planned_risk: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, RuntimeMode) or self.mode is RuntimeMode.LIVE:
            raise ContractError("Risk decision mode must be backtest or paper.")
        if not isinstance(self.allowed, bool):
            raise ContractError("allowed must be a boolean.")
        for name in ("correlation_id", "rule_version", "currency"):
            _require_text(getattr(self, name), name)
        _require_aware(self.evaluated_at, "evaluated_at")
        if not self.account_equity.is_finite() or self.account_equity <= 0:
            raise ContractError("account_equity must be finite and positive.")
        if self.allowed and self.reason_codes:
            raise ContractError("Allowed decisions must not have rejection reason codes.")
        if not self.allowed and not self.reason_codes:
            raise ContractError("Rejected decisions require at least one reason code.")
        for name in ("quantity", "notional", "planned_risk"):
            value = getattr(self, name)
            if value is not None and (not value.is_finite() or value < 0):
                raise ContractError(f"{name} must be finite and non-negative.")
        if self.allowed and (
            self.quantity is None
            or self.quantity <= 0
            or self.notional is None
            or self.notional <= 0
            or self.planned_risk is None
        ):
            raise ContractError(
                "Allowed decisions require positive quantity/notional and planned risk."
            )


@dataclass(frozen=True, slots=True)
class OrderState:
    correlation_id: str
    broker_order_id: str | None
    status: OrderStatus
    submitted_at: datetime
    updated_at: datetime
    ordered_quantity: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.status, OrderStatus):
            raise ContractError("status must be an OrderStatus.")
        _require_text(self.correlation_id, "correlation_id")
        _require_aware(self.submitted_at, "submitted_at")
        _require_aware(self.updated_at, "updated_at")
        if self.broker_order_id is not None:
            _require_text(self.broker_order_id, "broker_order_id")
        if self.updated_at < self.submitted_at:
            raise ContractError("updated_at must not precede submitted_at.")
        quantities = (
            self.ordered_quantity,
            self.filled_quantity,
            self.remaining_quantity,
        )
        if any(not value.is_finite() or value < 0 for value in quantities):
            raise ContractError("Order quantities must be finite and non-negative.")
        if self.ordered_quantity <= 0:
            raise ContractError("ordered_quantity must be positive.")
        if self.filled_quantity + self.remaining_quantity > self.ordered_quantity:
            raise ContractError("filled plus remaining quantity cannot exceed ordered.")


@dataclass(frozen=True, slots=True)
class Fill:
    fill_id: str
    correlation_id: str
    broker_order_id: str
    instrument_id: str
    filled_at: datetime
    quantity: Decimal
    price: Decimal
    currency: str

    def __post_init__(self) -> None:
        for name in (
            "fill_id",
            "correlation_id",
            "broker_order_id",
            "instrument_id",
            "currency",
        ):
            _require_text(getattr(self, name), name)
        _require_aware(self.filled_at, "filled_at")
        if not self.quantity.is_finite() or self.quantity <= 0:
            raise ContractError("fill quantity must be finite and positive.")
        if not self.price.is_finite() or self.price <= 0:
            raise ContractError("fill price must be finite and positive.")


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: str
    event_type: str
    occurred_at: datetime
    mode: RuntimeMode
    correlation_id: str
    component: str
    component_version: str
    references: tuple[str, ...] = ()
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.mode, RuntimeMode) or self.mode is RuntimeMode.LIVE:
            raise ContractError("Audit event mode must be backtest or paper.")
        for name in (
            "event_id",
            "event_type",
            "mode",
            "correlation_id",
            "component",
            "component_version",
        ):
            _require_text(getattr(self, name), name)
        _require_aware(self.occurred_at, "occurred_at")
        if any(not reference.strip() for reference in self.references):
            raise ContractError("references must not contain empty values.")


def _require_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{name} must be a non-empty string.")


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ContractError(f"{name} must be timezone-aware.")
