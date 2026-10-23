"""Operator-invoked paper session flow; execution remains behind broker gates."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
import math
from enum import StrEnum
from typing import Protocol

from trader_bot.agents import AgentOrchestrator, AnalysisDecision, Evidence
from trader_bot.config import RuntimeConfig
from trader_bot.contracts import AccountSnapshot, Instrument, RuntimeMode
from trader_bot.pipeline import ResearchPipeline
from trader_bot.notifications import NotificationError
from trader_bot.paper_broker import PaperBrokerError, PaperOrder
from trader_bot.risk import (
    CircuitState, DailyCircuitBreaker, RiskOutcome, RiskWarden,
)
from trader_bot.strategy import Evaluation, EvaluationStatus, ExchangeSession


class PaperSessionBroker(Protocol):
    def reconcile(self, *, cancel_expired: bool = False) -> object: ...

    def safety_state(self) -> tuple[bool, str]: ...

    def account(self) -> AccountSnapshot: ...

    def load_circuit_state(
        self, account_id: str, session: ExchangeSession,
    ) -> CircuitState | None: ...

    def save_circuit_state(
        self, account_id: str, session: ExchangeSession, state: CircuitState,
        at: datetime,
    ) -> CircuitState: ...

    def positions(self) -> Sequence[object]: ...

    def open_orders(self) -> Sequence[object]: ...

    def submit_approved(self, outcome: RiskOutcome, instrument: Instrument) -> object: ...

    def submit_lifecycle_test(
        self, outcome: RiskOutcome, instrument: Instrument, *, max_notional: Decimal,
    ) -> PaperOrder: ...

    def cancel_lifecycle_test(
        self, submitted: PaperOrder, *, max_notional: Decimal,
    ) -> PaperOrder: ...


class PaperSessionNotifier(Protocol):
    def notify(self, title: str, message: str) -> None: ...


class PaperSessionStatus(StrEnum):
    SUBMITTED = "submitted"
    LIFECYCLE_TESTED = "lifecycle_tested"
    PREVIEWED = "previewed"
    NO_SIGNAL = "no_signal"
    HELD = "held"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class PaperSessionResult:
    status: PaperSessionStatus
    code: str
    explanation: str
    research: Evaluation | None = None
    analysis: AnalysisDecision | None = None
    risk: RiskOutcome | None = None
    broker_result: object | None = None
    alert_error: str | None = None


@dataclass(frozen=True, slots=True)
class PaperSupervisionResult:
    orders: tuple[PaperOrder, ...]
    alert_error: str | None = None


@dataclass(frozen=True, slots=True)
class PaperSessionInput:
    instrument: Instrument
    starts_at: datetime
    as_of: datetime
    correlation_id: str
    evidence: tuple[Evidence, ...]
    circuit: DailyCircuitBreaker


@dataclass(frozen=True, slots=True)
class PaperSessionCycle:
    supervision: PaperSupervisionResult
    session: PaperSessionResult | None


class PaperSessionController:
    """Connect research, required analyst policy, risk approval and paper submission.

    The caller supplies curated Phase 3 evidence and a restorable session circuit
    baseline. A non-flat account or any open order is held because account-wide
    exposure is not yet incorporated in broker sizing.
    """

    def __init__(
        self, pipeline: ResearchPipeline, analysts: AgentOrchestrator,
        risk_warden: RiskWarden, broker: PaperSessionBroker,
        notifier: PaperSessionNotifier | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._analysts = analysts
        self._risk_warden = risk_warden
        self._broker = broker
        self._notifier = notifier
        self._notified_order_states: set[tuple[str, str, str]] = set()

    async def run(
        self, instrument: Instrument, starts_at: datetime, as_of: datetime,
        correlation_id: str, evidence: tuple[Evidence, ...],
        circuit: DailyCircuitBreaker,
    ) -> PaperSessionResult:
        return await self._execute(
            instrument, starts_at, as_of, correlation_id, evidence, circuit,
            preview_only=False,
        )

    async def preview(
        self, instrument: Instrument, starts_at: datetime, as_of: datetime,
        correlation_id: str, evidence: tuple[Evidence, ...],
        circuit: DailyCircuitBreaker,
    ) -> PaperSessionResult:
        """Evaluate a paper session without invoking any order-write operation."""
        return await self._execute(
            instrument, starts_at, as_of, correlation_id, evidence, circuit,
            preview_only=True,
        )

    async def run_lifecycle_test(
        self, instrument: Instrument, starts_at: datetime, as_of: datetime,
        correlation_id: str, evidence: tuple[Evidence, ...],
        circuit: DailyCircuitBreaker, *, max_notional: Decimal,
    ) -> PaperSessionResult:
        if (
            not isinstance(max_notional, Decimal) or not max_notional.is_finite()
            or max_notional <= 0 or max_notional > Decimal("100")
        ):
            raise PaperBrokerError(
                "TEST_ORDER_LIMIT_INVALID",
                "Lifecycle-test notional cap must be positive and no greater than USD 100.",
            )
        try:
            result = await self._run(
                instrument, starts_at, as_of, correlation_id, evidence, circuit,
                lifecycle_test=True, max_notional=max_notional,
            )
            if result.status is PaperSessionStatus.SUBMITTED:
                if not isinstance(result.broker_result, PaperOrder):
                    raise PaperBrokerError(
                        "BROKER_RESPONSE_INVALID",
                        "Lifecycle-test submission returned an invalid broker order.",
                    )
                order = self._broker.cancel_lifecycle_test(
                    result.broker_result, max_notional=max_notional,
                )
                result = replace(
                    result, status=PaperSessionStatus.LIFECYCLE_TESTED,
                    code="TEST_ORDER_RECONCILED",
                    explanation=(
                        f"Paper test order reconciled as {order.status}; "
                        "review account state before any further action."
                    ),
                    broker_result=order,
                )
        except PaperBrokerError as exc:
            try:
                self._notify("Paper lifecycle test safety event", f"{exc.code}: {exc}")
            except NotificationError as alert_exc:
                exc.add_note(f"Desktop alert delivery also failed: {alert_exc}")
            raise
        return self._notify_result(result)

    async def _execute(
        self, instrument: Instrument, starts_at: datetime, as_of: datetime,
        correlation_id: str, evidence: tuple[Evidence, ...],
        circuit: DailyCircuitBreaker, *, preview_only: bool,
    ) -> PaperSessionResult:
        try:
            result = await self._run(
                instrument, starts_at, as_of, correlation_id, evidence, circuit,
                preview_only=preview_only,
            )
        except PaperBrokerError as exc:
            try:
                self._notify("Paper broker safety event", f"{exc.code}: {exc}")
            except NotificationError as alert_exc:
                exc.add_note(f"Desktop alert delivery also failed: {alert_exc}")
            raise
        return self._notify_result(result)

    async def _run(
        self, instrument: Instrument, starts_at: datetime, as_of: datetime,
        correlation_id: str, evidence: tuple[Evidence, ...],
        circuit: DailyCircuitBreaker, *, preview_only: bool = False,
        lifecycle_test: bool = False, max_notional: Decimal | None = None,
    ) -> PaperSessionResult:
        runtime: RuntimeConfig = self._pipeline.runtime
        if runtime.mode is not RuntimeMode.PAPER:
            return PaperSessionResult(
                PaperSessionStatus.REJECTED, "MODE_FORBIDDEN",
                "Paper session requires paper runtime mode.",
            )
        self._broker.reconcile()
        halted, halt_reason = self._broker.safety_state()
        if halted:
            return PaperSessionResult(
                PaperSessionStatus.HELD, "PAPER_ENTRIES_HALTED",
                halt_reason or "Paper entries are halted pending operator reconciliation.",
            )
        if not preview_only and not runtime.paper_order_submission_enabled:
            return PaperSessionResult(
                PaperSessionStatus.HELD, "PAPER_SUBMISSION_DISABLED",
                "Paper order submission is disabled by configuration.",
            )
        if not lifecycle_test and not preview_only and not runtime.paper_execution_capabilities_verified:
            return PaperSessionResult(
                PaperSessionStatus.HELD, "PAPER_CAPABILITIES_UNVERIFIED",
                "Paper execution capabilities have not been acknowledged as verified.",
            )

        research = self._pipeline.evaluate(
            instrument, starts_at, as_of, correlation_id,
        )
        if research.status is EvaluationStatus.REJECTED:
            return PaperSessionResult(
                PaperSessionStatus.REJECTED, research.code, research.explanation,
                research=research,
            )
        if research.status is EvaluationStatus.NO_SIGNAL:
            return PaperSessionResult(
                PaperSessionStatus.NO_SIGNAL, research.code, research.explanation,
                research=research,
            )
        candidate = research.candidate
        if candidate is None or research.quote is None:
            return PaperSessionResult(
                PaperSessionStatus.REJECTED, "RESEARCH_RESULT_INVALID",
                "Signal evaluation did not include its candidate and source quote.",
                research=research,
            )
        entry_limit = None
        if lifecycle_test:
            if max_notional is None:
                raise PaperBrokerError(
                    "TEST_ORDER_LIMIT_INVALID", "A lifecycle-test notional cap is required.",
                )
            entry_limit = research.quote.bid - self._risk_warden.policy.price_tick
            if entry_limit <= 0 or entry_limit >= research.quote.bid:
                return PaperSessionResult(
                    PaperSessionStatus.HELD, "TEST_LIMIT_UNAVAILABLE",
                    "A positive, below-bid lifecycle-test limit could not be calculated.",
                    research=research,
                )
        if any(item.instrument_id != instrument.instrument_id for item in evidence):
            return PaperSessionResult(
                PaperSessionStatus.REJECTED, "ANALYSIS_EVIDENCE_MISMATCH",
                "Analyst evidence must refer to the selected instrument.",
                research=research,
            )

        analysis = await self._analysts.analyze(
            correlation_id, instrument.instrument_id, evidence,
        )
        if not analysis.can_continue:
            return PaperSessionResult(
                PaperSessionStatus.HELD, "ANALYSIS_POLICY_HOLD",
                "; ".join(analysis.reason_codes) or "Required analyst policy did not pass.",
                research, analysis,
            )

        account = self._broker.account()
        positions = self._broker.positions()
        open_orders = self._broker.open_orders()
        if positions or open_orders:
            return PaperSessionResult(
                PaperSessionStatus.HELD, "ACCOUNT_NOT_FLAT",
                "Session requires a reconciled flat account with no open orders.",
                research, analysis,
            )
        session = next(
            (
                item for item in self._pipeline.sessions
                if item.calendar == instrument.market_calendar
                and item.opens_at <= as_of < item.closes_at
            ),
            None,
        )
        if session is None:
            return PaperSessionResult(
                PaperSessionStatus.REJECTED, "SESSION_UNKNOWN",
                "No configured exchange session covers this evaluation time.",
                research, analysis,
            )
        if (
            circuit.state.session_open != session.opens_at
            or circuit.state.session_close != session.closes_at
        ):
            return PaperSessionResult(
                PaperSessionStatus.REJECTED, "SESSION_BASELINE_UNKNOWN",
                "Circuit-breaker baseline does not match the active exchange session.",
                research, analysis,
            )

        persisted_circuit = self._broker.load_circuit_state(
            account.account_reference, session,
        )
        if persisted_circuit is not None and (
            persisted_circuit.start_equity != circuit.state.start_equity
            or persisted_circuit.session_open != circuit.state.session_open
            or persisted_circuit.session_close != circuit.state.session_close
        ):
            return PaperSessionResult(
                PaperSessionStatus.REJECTED, "PAPER_CIRCUIT_CONFLICT",
                "Supplied circuit baseline conflicts with durable session state.",
                research, analysis,
            )
        initial_state = persisted_circuit or circuit.state
        if circuit.state.tripped and not initial_state.tripped:
            initial_state = CircuitState(
                initial_state.session_open, initial_state.session_close,
                initial_state.start_equity, True,
            )
        initial_state = self._broker.save_circuit_state(
            account.account_reference, session, initial_state, as_of,
        )
        durable_circuit = DailyCircuitBreaker(initial_state)
        risk = self._risk_warden.evaluate(
            candidate, instrument, research.quote, account, (), durable_circuit, session,
            runtime, self._pipeline.policy, as_of, reconciled=True,
            protective_exit_supported=(
                lifecycle_test or runtime.paper_execution_capabilities_verified
            ),
            entry_limit=entry_limit, entry_notional_cap=max_notional,
        )
        self._broker.save_circuit_state(
            account.account_reference, session, durable_circuit.state, as_of,
        )
        if not risk.decision.allowed:
            return PaperSessionResult(
                PaperSessionStatus.HELD, "RISK_WARDEN_HOLD", risk.explanation,
                research, analysis, risk,
            )
        if (
            lifecycle_test and risk.intent is not None
            and risk.intent.limit_price >= research.quote.bid
        ):
            return PaperSessionResult(
                PaperSessionStatus.HELD, "TEST_LIMIT_NOT_PASSIVE",
                "Risk-adjusted test limit is not below the observed bid.",
                research, analysis, risk,
            )
        if preview_only:
            return PaperSessionResult(
                PaperSessionStatus.PREVIEWED, "ORDER_PREVIEW_ONLY",
                "Risk checks passed; this preview did not submit an order.",
                research, analysis, risk,
            )
        broker_result = (
            self._broker.submit_lifecycle_test(
                risk, instrument, max_notional=max_notional,
            )
            if lifecycle_test and max_notional is not None
            else self._broker.submit_approved(risk, instrument)
        )
        return PaperSessionResult(
            PaperSessionStatus.SUBMITTED, "ORDER_SUBMITTED",
            "Risk-approved paper order was accepted by the configured broker boundary.",
            research, analysis, risk, broker_result,
        )

    def supervise_expired_orders(self) -> PaperSupervisionResult:
        """Reconcile and request cancellation of expired entry orders.

        Call this at least once per five-minute bar while supervising a paper
        session. The broker still requires both paper-write gates before it can
        issue a cancellation.
        """
        runtime = self._pipeline.runtime
        if runtime.mode is not RuntimeMode.PAPER:
            raise PaperBrokerError(
                "MODE_FORBIDDEN", "Paper order supervision requires paper mode.",
            )
        if not runtime.paper_order_submission_enabled:
            raise PaperBrokerError(
                "PAPER_SUBMISSION_DISABLED",
                "Paper writes must be enabled to cancel expired entries.",
            )
        if not runtime.paper_execution_capabilities_verified:
            raise PaperBrokerError(
                "PAPER_CAPABILITIES_UNVERIFIED",
                "Paper execution capabilities must be verified before cancellation.",
            )
        try:
            result = self._broker.reconcile(cancel_expired=True)
        except PaperBrokerError as exc:
            try:
                self._notify("Paper order supervision failed", f"{exc.code}: {exc}")
            except NotificationError as alert_exc:
                exc.add_note(f"Desktop alert delivery also failed: {alert_exc}")
            raise
        if not isinstance(result, tuple) or any(
            not isinstance(item, PaperOrder) for item in result
        ):
            raise PaperBrokerError(
                "BROKER_RESPONSE_INVALID",
                "Paper broker returned invalid reconciliation results.",
            )
        return PaperSupervisionResult(result, self._notify_order_events(result))

    def _notify_result(self, result: PaperSessionResult) -> PaperSessionResult:
        if self._notifier is None:
            return result
        titles = {
            PaperSessionStatus.SUBMITTED: "Paper order submitted",
            PaperSessionStatus.LIFECYCLE_TESTED: "Paper lifecycle test reconciled",
            PaperSessionStatus.PREVIEWED: "Paper order preview",
            PaperSessionStatus.NO_SIGNAL: "No paper trade proposal",
            PaperSessionStatus.HELD: "Paper trade held",
            PaperSessionStatus.REJECTED: "Paper trade rejected",
        }
        if (
            result.risk is not None
            and "DAILY_DRAWDOWN" in result.risk.decision.reason_codes
        ):
            titles[PaperSessionStatus.HELD] = "Paper circuit breaker activated"
        message = f"{result.code}: {result.explanation}"
        if result.research is not None and result.research.candidate is not None:
            candidate = result.research.candidate
            message += (
                f" Instrument={candidate.instrument_id}; "
                f"direction={candidate.direction.value}."
            )
        if result.risk is not None and result.risk.intent is not None:
            intent = result.risk.intent
            message += (
                f" Quantity={intent.quantity}; limit={intent.limit_price}; "
                f"stop={intent.stop_price}; expires={intent.expires_at.isoformat()}."
            )
        try:
            self._notify(titles[result.status], message)
        except NotificationError as exc:
            return replace(result, alert_error=str(exc))
        return result

    def _notify(self, title: str, message: str) -> None:
        if self._notifier is not None:
            self._notifier.notify(title[:120], message[:500])

    def _notify_order_events(self, result: tuple[PaperOrder, ...]) -> str | None:
        if self._notifier is None:
            return None
        errors: list[str] = []
        for item in result:
            key = (
                item.client_order_id, item.status,
                str(item.filled_quantity),
            )
            if key in self._notified_order_states:
                continue
            self._notified_order_states.add(key)
            titles = {
                "partially_filled": "Paper order partially filled",
                "filled": "Paper order filled; verify protective exit",
                "canceled": "Paper order canceled",
                "expired": "Paper order expired",
                "rejected": "Paper order rejected",
            }
            title = titles.get(item.status, "Paper order status changed")
            try:
                self._notify(
                    title,
                    f"{item.symbol} {item.status}: "
                    f"filled {item.filled_quantity} of {item.ordered_quantity}.",
                )
            except NotificationError as exc:
                errors.append(str(exc))
        return "; ".join(dict.fromkeys(errors)) or None


class PaperSessionSupervisor:
    """Explicitly started async cadence runner; never starts itself in the background."""

    def __init__(self, controller: PaperSessionController) -> None:
        self._controller = controller

    async def run_once(
        self, session_input: PaperSessionInput | None,
    ) -> PaperSessionCycle:
        supervision = self._controller.supervise_expired_orders()
        result = await self._run_session(session_input)
        return PaperSessionCycle(supervision, result)

    async def _run_session(
        self, session_input: PaperSessionInput | None,
    ) -> PaperSessionResult | None:
        result = None
        if session_input is not None:
            result = await self._controller.run(
                session_input.instrument, session_input.starts_at,
                session_input.as_of, session_input.correlation_id,
                session_input.evidence, session_input.circuit,
            )
        return result

    async def run_forever(
        self, session_input_factory: Callable[[], Awaitable[PaperSessionInput | None]],
        stop_event: asyncio.Event, on_cycle: Callable[[PaperSessionCycle], None],
        *, interval_seconds: float = 300,
    ) -> None:
        if (
            isinstance(interval_seconds, bool)
            or not isinstance(interval_seconds, (int, float))
            or not math.isfinite(interval_seconds)
            or interval_seconds <= 0
            or interval_seconds > 300
        ):
            raise ValueError("Paper supervision interval must be greater than zero and at most 300 seconds.")
        if not isinstance(stop_event, asyncio.Event):
            raise TypeError("stop_event must be an asyncio.Event.")

        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        while not stop_event.is_set():
            delay = next_tick - loop.time()
            if delay > 0:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=delay)
                except TimeoutError:
                    pass
                if stop_event.is_set():
                    return
            supervision = self._controller.supervise_expired_orders()
            session_input = await session_input_factory()
            if stop_event.is_set():
                on_cycle(PaperSessionCycle(supervision, None))
                return
            session = await self._run_session(session_input)
            on_cycle(PaperSessionCycle(supervision, session))
            next_tick = max(next_tick + interval_seconds, loop.time())
