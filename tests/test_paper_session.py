import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import io
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from trader_bot.agents import AgentOrchestrator, AnalysisPolicy, local_agents
from trader_bot.analysis_io import write_analysis
from trader_bot.config import PaperCredentials, RuntimeConfig
from trader_bot.contracts import AccountSnapshot, AgentRole, RuntimeMode
from trader_bot.market_data import CsvMarketDataProvider, MarketDataClient, UsagePolicy
from trader_bot.notifications import NotificationError
from trader_bot.paper_session import (
    PaperSessionController, PaperSessionInput, PaperSessionStatus,
    PaperSessionSupervisor,
)
from trader_bot.paper_broker import PaperBrokerError, PaperOrder
from trader_bot.pipeline import ResearchPipeline
from trader_bot.risk import CircuitState, DailyCircuitBreaker, RiskWarden
from trader_bot.strategy import BenchmarkStrategy
from trader_bot.cli import main
from tests.agent_support import DIGEST, FixtureModel, NOW, completion, evidence
from tests.support import (
    FIXTURE, INSTRUMENT, OPEN, POLICY, SESSION, quote_at,
)


class SimulatedBroker:
    def __init__(self, *, positions=(), orders=()):
        self._positions = positions
        self._orders = orders
        self.reconcile_calls = 0
        self.cancel_expired_calls = []
        self.submit_calls = []
        self.reconcile_error = None
        self.reconcile_result = ()
        self.circuit_states = {}
        self.account_equity = Decimal("10000")
        self.halted = False
        self.halt_reason = ""

    def reconcile(self, *, cancel_expired=False):
        self.reconcile_calls += 1
        self.cancel_expired_calls.append(cancel_expired)
        if self.reconcile_error is not None:
            raise self.reconcile_error
        return self.reconcile_result

    def safety_state(self):
        return self.halted, self.halt_reason

    def account(self):
        return AccountSnapshot(
            "simulated-paper-account", RuntimeMode.PAPER, self.account_equity,
            Decimal("10000"), "USD", NOW,
        )

    def load_circuit_state(self, account_id, session):
        return self.circuit_states.get((account_id, session.calendar, session.opens_at))

    def save_circuit_state(self, account_id, session, state, at):
        key = (account_id, session.calendar, session.opens_at)
        previous = self.circuit_states.get(key)
        if previous is not None:
            state = replace(state, tripped=state.tripped or previous.tripped)
        self.circuit_states[key] = state
        return state

    def positions(self):
        return self._positions

    def open_orders(self):
        return self._orders

    def submit_approved(self, outcome, instrument):
        self.submit_calls.append((outcome, instrument))
        return {"simulated": True, "client_order_id": outcome.intent.idempotency_key}

    def submit_lifecycle_test(self, outcome, instrument, *, max_notional):
        self.submit_calls.append((outcome, instrument))
        return PaperOrder(
            "simulated-order", outcome.intent.idempotency_key, instrument.symbol,
            "buy", "accepted", "oto", outcome.intent.quantity, Decimal("0"),
            outcome.intent.limit_price, None, NOW, NOW, (),
        )

    def cancel_lifecycle_test(self, submitted, *, max_notional):
        return replace(submitted, status="canceled")


class RecordingNotifier:
    def __init__(self, *, error=None):
        self.messages = []
        self.error = error

    def notify(self, title, message):
        self.messages.append((title, message))
        if self.error is not None:
            raise self.error


class PaperSessionControllerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = RuntimeConfig(
            mode=RuntimeMode.PAPER, watchlist=("DEMO",),
            paper_credentials=PaperCredentials("fake-key", "fake-secret"),
            paper_order_submission_enabled=True,
            paper_execution_capabilities_verified=True,
        )
        provider = CsvMarketDataProvider(FIXTURE, quote_at())
        client = MarketDataClient(provider, UsagePolicy(10))
        self.pipeline = ResearchPipeline(
            client, BenchmarkStrategy(), self.runtime, POLICY, (SESSION,),
            clock=lambda: NOW,
        )
        self.model = FixtureModel()
        self.analysts = AgentOrchestrator(
            local_agents(self.model), AnalysisPolicy(), clock=lambda: NOW,
        )
        self.broker = SimulatedBroker()
        self.controller = PaperSessionController(
            self.pipeline, self.analysts, RiskWarden(), self.broker,
        )
        self.circuit = DailyCircuitBreaker(CircuitState(
            SESSION.opens_at, SESSION.closes_at, Decimal("10000"),
        ))

    async def run_session(self):
        return await self.controller.run(
            INSTRUMENT, OPEN, NOW, "paper-session-test", evidence(), self.circuit,
        )

    async def test_simulated_end_to_end_flow_submits_only_risk_approved_candidate(self):
        result = await self.run_session()

        self.assertEqual(result.status, PaperSessionStatus.SUBMITTED)
        self.assertEqual(result.code, "ORDER_SUBMITTED")
        self.assertTrue(result.analysis.can_continue)
        self.assertTrue(result.risk.decision.allowed)
        self.assertEqual(result.risk.intent.quantity, Decimal("4"))
        self.assertEqual(self.broker.reconcile_calls, 1)
        self.assertEqual(len(self.broker.submit_calls), 1)
        self.assertTrue(result.broker_result["simulated"])

    async def test_preview_runs_risk_checks_without_submission_or_write_gate(self):
        preview_runtime = replace(
            self.runtime, paper_order_submission_enabled=False,
            paper_execution_capabilities_verified=True,
        )
        preview_pipeline = replace(self.pipeline, runtime=preview_runtime)
        controller = PaperSessionController(
            preview_pipeline, self.analysts, RiskWarden(), self.broker,
        )

        result = await controller.preview(
            INSTRUMENT, OPEN, NOW, "paper-preview-test", evidence(), self.circuit,
        )

        self.assertEqual(result.status, PaperSessionStatus.PREVIEWED)
        self.assertEqual(result.code, "ORDER_PREVIEW_ONLY")
        self.assertTrue(result.risk.decision.allowed)
        self.assertIsNone(result.broker_result)
        self.assertEqual(self.broker.submit_calls, [])
        self.assertEqual(self.broker.reconcile_calls, 1)

    async def test_lifecycle_test_risk_enforces_notional_cap_before_submission(self):
        controller = PaperSessionController(
            self.pipeline, self.analysts, RiskWarden(), self.broker,
        )

        result = await controller.run_lifecycle_test(
            INSTRUMENT, OPEN, NOW, "paper-lifecycle-cap-test",
            evidence(), self.circuit, max_notional=Decimal("100"),
        )

        self.assertEqual(result.status, PaperSessionStatus.HELD)
        self.assertEqual(result.code, "RISK_WARDEN_HOLD")
        self.assertFalse(self.broker.submit_calls)

    def test_lifecycle_cli_rejects_missing_typed_confirmation_before_broker_access(self):
        arguments = [
            "paper-lifecycle-test", "--bars", "/missing/bars.csv",
            "--symbol", "HAS", "--venue", "NYSE", "--calendar", "XNYS",
            "--session-open", NOW.isoformat(), "--session-close", (NOW + timedelta(hours=6)).isoformat(),
            "--as-of", NOW.isoformat(), "--bid", "50", "--ask", "50.01",
            "--quote-at", NOW.isoformat(), "--min-price", "5",
            "--min-volume", "1000", "--max-spread-bps", "20",
            "--max-quote-age-seconds", "30", "--max-bar-age-seconds", "60",
            "--verified", "--evidence", "/missing/evidence.json",
            "--max-notional", "100", "--confirm-submit", "wrong",
        ]
        errors = io.StringIO()
        with (
            patch.dict("os.environ", {
                "TRADER_BOT_MODE": "paper",
                "TRADER_BOT_PAPER_API_KEY": "test-key",
                "TRADER_BOT_PAPER_API_SECRET": "test-secret",
                "TRADER_BOT_PAPER_ORDER_SUBMISSION_ENABLED": "true",
                "TRADER_BOT_LOCAL_MODEL_ENABLED": "true",
            }, clear=True),
            patch("trader_bot.cli.AlpacaPaperBroker") as broker_factory,
            redirect_stderr(errors),
        ):
            result = main(arguments)

        self.assertEqual(result, 2)
        self.assertIn("Type exactly 'SUBMIT HAS UP TO 100 USD'", errors.getvalue())
        broker_factory.assert_not_called()

    def test_paper_preview_cli_never_submits_or_cancels(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            evidence_path = folder / "analysis.json"
            write_analysis(
                evidence_path, "paper-preview-cli", INSTRUMENT.instrument_id,
                evidence(), AnalysisPolicy(),
            )
            arguments = [
                "paper-preview", "--bars", str(FIXTURE), "--symbol", "DEMO",
                "--venue", "NASDAQ", "--calendar", "XNAS",
                "--session-open", OPEN.isoformat(),
                "--session-close", SESSION.closes_at.isoformat(),
                "--as-of", NOW.isoformat(), "--bid", "110", "--ask", "110.02",
                "--quote-at", NOW.isoformat(), "--min-price", "5",
                "--min-volume", "1000", "--max-spread-bps", "20",
                "--max-quote-age-seconds", "30", "--max-bar-age-seconds", "60",
                "--verified", "--evidence", str(evidence_path),
                "--journal", str(folder / "paper.sqlite3"),
                "--model", "fixture", "--model-version", DIGEST,
            ]
            output = io.StringIO()
            with (
                patch.dict("os.environ", {
                    "TRADER_BOT_MODE": "paper",
                    "TRADER_BOT_PAPER_API_KEY": "test-key",
                    "TRADER_BOT_PAPER_API_SECRET": "test-secret",
                    "TRADER_BOT_WATCHLIST": "DEMO",
                    "TRADER_BOT_LOCAL_MODEL_ENABLED": "true",
                    "TRADER_BOT_PAPER_ORDER_SUBMISSION_ENABLED": "true",
                    "TRADER_BOT_PAPER_EXECUTION_CAPABILITIES_VERIFIED": "true",
                }, clear=True),
                patch("trader_bot.cli.AlpacaPaperBroker", return_value=self.broker),
                patch("trader_bot.cli.OllamaLocalModel", return_value=self.model),
                redirect_stdout(output),
            ):
                result = main(arguments)

        self.assertEqual(result, 0)
        self.assertIn("previewed: ORDER_PREVIEW_ONLY", output.getvalue())
        self.assertIn("Preview only", output.getvalue())
        self.assertEqual(self.broker.submit_calls, [])
        self.assertEqual(self.broker.cancel_expired_calls, [False])
        self.assertEqual(self.broker.reconcile_calls, 1)

    async def test_successful_submission_sends_local_status_alert(self):
        notifier = RecordingNotifier()
        controller = PaperSessionController(
            self.pipeline, self.analysts, RiskWarden(), self.broker, notifier,
        )

        result = await controller.run(
            INSTRUMENT, OPEN, NOW, "paper-alert-test", evidence(), self.circuit,
        )

        self.assertEqual(result.status, PaperSessionStatus.SUBMITTED)
        self.assertEqual(notifier.messages[0][0], "Paper order submitted")
        self.assertIn("ORDER_SUBMITTED", notifier.messages[0][1])

    async def test_notification_failure_is_reported_without_changing_safety_result(self):
        notifier = RecordingNotifier(error=NotificationError("desktop unavailable"))
        controller = PaperSessionController(
            self.pipeline, self.analysts, RiskWarden(), self.broker, notifier,
        )

        result = await controller.run(
            INSTRUMENT, OPEN, NOW, "paper-alert-failure-test", evidence(), self.circuit,
        )

        self.assertEqual(result.status, PaperSessionStatus.SUBMITTED)
        self.assertEqual(result.alert_error, "desktop unavailable")
        self.assertEqual(len(self.broker.submit_calls), 1)

    async def test_broker_failure_is_not_masked_by_notification_failure(self):
        self.broker.reconcile_error = PaperBrokerError(
            "BROKER_UNAVAILABLE", "paper broker unavailable",
        )
        notifier = RecordingNotifier(error=NotificationError("desktop unavailable"))
        controller = PaperSessionController(
            self.pipeline, self.analysts, RiskWarden(), self.broker, notifier,
        )

        with self.assertRaises(PaperBrokerError) as error:
            await controller.run(
                INSTRUMENT, OPEN, NOW, "paper-alert-broker-failure", evidence(),
                self.circuit,
            )

        self.assertEqual(error.exception.code, "BROKER_UNAVAILABLE")
        self.assertIn("Desktop alert delivery also failed", error.exception.__notes__[0])
        self.assertFalse(self.broker.submit_calls)

    async def test_required_analyst_veto_holds_before_account_or_submission(self):
        self.model.outputs[AgentRole.NEWS_SENTIMENT.value] = completion(
            "news-1", veto=True,
        )

        result = await self.run_session()

        self.assertEqual(result.status, PaperSessionStatus.HELD)
        self.assertEqual(result.code, "ANALYSIS_POLICY_HOLD")
        self.assertFalse(self.broker.submit_calls)

    async def test_nonflat_account_holds_before_risk_or_order_submission(self):
        self.broker._positions = (object(),)

        result = await self.run_session()

        self.assertEqual(result.status, PaperSessionStatus.HELD)
        self.assertEqual(result.code, "ACCOUNT_NOT_FLAT")
        self.assertIsNone(result.risk)
        self.assertFalse(self.broker.submit_calls)

    async def test_operator_pause_is_observed_before_analysis_or_order_submission(self):
        self.broker.halted = True
        self.broker.halt_reason = "OPERATOR_PAUSE: New paper entries paused by operator."

        result = await self.run_session()

        self.assertEqual(result.status, PaperSessionStatus.HELD)
        self.assertEqual(result.code, "PAPER_ENTRIES_HALTED")
        self.assertEqual(self.model.calls, [])
        self.assertFalse(self.broker.submit_calls)

    async def test_unverified_or_disabled_write_gates_never_submit(self):
        for changes, expected in (
            ({"paper_order_submission_enabled": False}, "PAPER_SUBMISSION_DISABLED"),
            ({"paper_execution_capabilities_verified": False}, "PAPER_CAPABILITIES_UNVERIFIED"),
        ):
            with self.subTest(changes=changes):
                self.pipeline = replace(
                    self.pipeline, runtime=replace(self.runtime, **changes),
                )
                self.controller = PaperSessionController(
                    self.pipeline, self.analysts, RiskWarden(), self.broker,
                )
                result = await self.run_session()
                self.assertEqual(result.code, expected)
                self.assertFalse(self.broker.submit_calls)

    async def test_evidence_for_another_instrument_is_rejected(self):
        bad_evidence = tuple(
            replace(item, instrument_id="US:OTHER") for item in evidence()
        )
        result = await self.controller.run(
            INSTRUMENT, OPEN, NOW, "paper-session-test", bad_evidence, self.circuit,
        )

        self.assertEqual(result.status, PaperSessionStatus.REJECTED)
        self.assertEqual(result.code, "ANALYSIS_EVIDENCE_MISMATCH")
        self.assertFalse(self.broker.submit_calls)

    async def test_supervision_requests_expiry_cancellation_through_broker_gate(self):
        result = self.controller.supervise_expired_orders()

        self.assertEqual(result.orders, ())
        self.assertEqual(self.broker.reconcile_calls, 1)
        self.assertEqual(self.broker.cancel_expired_calls, [True])

    async def test_reconciliation_alerts_once_for_partial_fill_and_reports_alert_failure(self):
        partial_fill = PaperOrder(
            "broker-id", "client-id", "DEMO", "buy", "partially_filled", "oto",
            Decimal("5"), Decimal("2"), Decimal("100"), None,
            NOW, NOW, (),
        )
        self.broker.reconcile_result = (partial_fill,)
        notifier = RecordingNotifier(error=NotificationError("desktop unavailable"))
        controller = PaperSessionController(
            self.pipeline, self.analysts, RiskWarden(), self.broker, notifier,
        )

        first = controller.supervise_expired_orders()
        second = controller.supervise_expired_orders()

        self.assertEqual(first.orders, (partial_fill,))
        self.assertEqual(first.alert_error, "desktop unavailable")
        self.assertIsNone(second.alert_error)
        self.assertEqual(len(notifier.messages), 1)
        self.assertEqual(notifier.messages[0][0], "Paper order partially filled")

    async def test_supervisor_runs_reconciliation_cadence_then_stops_cleanly(self):
        supervisor = PaperSessionSupervisor(self.controller)
        stop_event = asyncio.Event()
        calls = 0
        cycles = []

        async def inputs():
            nonlocal calls
            calls += 1
            stop_event.set()
            return None

        await supervisor.run_forever(
            inputs, stop_event, cycles.append, interval_seconds=0.01,
        )

        self.assertEqual(calls, 1)
        self.assertEqual(self.broker.reconcile_calls, 1)
        self.assertEqual(self.broker.cancel_expired_calls, [True])
        self.assertFalse(self.broker.submit_calls)
        self.assertEqual(cycles[0].session, None)
        self.assertEqual(cycles[0].supervision.orders, ())

    async def test_supervisor_cycle_runs_session_after_expiry_supervision(self):
        supervisor = PaperSessionSupervisor(self.controller)
        session_input = PaperSessionInput(
            INSTRUMENT, OPEN, NOW, "supervised-session", evidence(), self.circuit,
        )

        cycle = await supervisor.run_once(session_input)

        self.assertEqual(cycle.session.status, PaperSessionStatus.SUBMITTED)
        self.assertEqual(cycle.supervision.orders, ())
        self.assertEqual(self.broker.cancel_expired_calls, [True, False])
        self.assertEqual(len(self.broker.submit_calls), 1)

    async def test_supervisor_rejects_cadence_longer_than_one_bar(self):
        supervisor = PaperSessionSupervisor(self.controller)
        with self.assertRaises(ValueError):
            await supervisor.run_forever(
                lambda: asyncio.sleep(0, result=None), asyncio.Event(), lambda cycle: None,
                interval_seconds=301,
            )
        self.assertEqual(self.broker.reconcile_calls, 0)

    async def test_supervisor_surfaces_broker_failure_without_requesting_session_input(self):
        supervisor = PaperSessionSupervisor(self.controller)
        stop_event = asyncio.Event()
        factory_calls = 0
        self.broker.reconcile_error = PaperBrokerError(
            "BROKER_UNAVAILABLE", "paper broker unavailable",
        )

        async def inputs():
            nonlocal factory_calls
            factory_calls += 1
            return None

        with self.assertRaises(PaperBrokerError) as error:
            await supervisor.run_forever(
                inputs, stop_event, lambda cycle: None, interval_seconds=0.01,
            )

        self.assertEqual(error.exception.code, "BROKER_UNAVAILABLE")
        self.assertEqual(factory_calls, 0)

    async def test_stopped_supervisor_does_not_start_a_cycle(self):
        supervisor = PaperSessionSupervisor(self.controller)
        stop_event = asyncio.Event()
        stop_event.set()
        calls = 0

        async def inputs():
            nonlocal calls
            calls += 1
            return None

        await supervisor.run_forever(inputs, stop_event, lambda cycle: None)

        self.assertEqual(calls, 0)
        self.assertEqual(self.broker.reconcile_calls, 0)

    async def test_persisted_circuit_trip_survives_a_new_controller_instance(self):
        self.broker.circuit_states[
            ("simulated-paper-account", SESSION.calendar, SESSION.opens_at)
        ] = CircuitState(
            SESSION.opens_at, SESSION.closes_at, Decimal("10000"), True,
        )
        self.broker.account_equity = Decimal("9900")
        restarted_controller = PaperSessionController(
            self.pipeline, self.analysts, RiskWarden(), self.broker,
        )

        result = await restarted_controller.run(
            INSTRUMENT, OPEN, NOW, "paper-session-restart", evidence(),
            DailyCircuitBreaker(CircuitState(
                SESSION.opens_at, SESSION.closes_at, Decimal("10000"),
            )),
        )

        self.assertEqual(result.status, PaperSessionStatus.HELD)
        self.assertEqual(result.code, "RISK_WARDEN_HOLD")
        self.assertEqual(result.risk.decision.reason_codes, ("DAILY_DRAWDOWN",))
        self.assertTrue(self.broker.circuit_states[
            ("simulated-paper-account", SESSION.calendar, SESSION.opens_at)
        ].tripped)
        self.assertFalse(self.broker.submit_calls)


if __name__ == "__main__":
    unittest.main()
