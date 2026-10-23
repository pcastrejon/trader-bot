from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
import io
from urllib.error import HTTPError
from urllib.request import ProxyHandler
from unittest.mock import MagicMock, call, patch

from trader_bot.config import PaperCredentials, RuntimeConfig
from trader_bot.contracts import AccountSnapshot, CandidateSignal, RuntimeMode, TradeDirection
from trader_bot.paper_broker import (
    PAPER_API_BASE_URL, AlpacaPaperBroker, PaperBrokerError, PaperJsonTransport,
    PaperOrderJournal, PaperOrderUnknownError,
)
from trader_bot.cli import main
from trader_bot.risk import CircuitState, DailyCircuitBreaker, RiskWarden
from tests.support import INSTRUMENT, NOW, POLICY, SESSION, quote_at


D = Decimal


def paper_config(*, enabled: bool = True, capabilities_verified: bool = True) -> RuntimeConfig:
    return replace(
        RuntimeConfig(
            mode=RuntimeMode.PAPER, watchlist=("DEMO",),
            paper_credentials=PaperCredentials("paper-key", "paper-secret"),
            paper_execution_capabilities_verified=capabilities_verified,
        ),
        paper_order_submission_enabled=enabled,
    )


def approved_outcome(
    *, bid: Decimal = Decimal("110"), ask: Decimal = Decimal("110.02"),
    entry_limit: Decimal | None = None, notional_cap: Decimal | None = None,
):
    signal = CandidateSignal(
        "paper-correlation", INSTRUMENT.instrument_id, "experimental-ema-atr",
        "benchmark-v1", TradeDirection.LONG, NOW - timedelta(minutes=1),
        ("test evidence",), D("98"), NOW + timedelta(minutes=4),
    )
    account = AccountSnapshot(
        "paper-account", RuntimeMode.PAPER, D("10000"), D("10000"), "USD", NOW,
    )
    runtime = paper_config()
    return RiskWarden().evaluate(
        signal, INSTRUMENT,
        replace(quote_at(NOW), bid=bid, ask=ask), account, (),
        DailyCircuitBreaker(CircuitState(
            SESSION.opens_at, SESSION.closes_at, D("10000"),
        )),
        SESSION, runtime, POLICY, NOW,
        reconciled=True, protective_exit_supported=True,
        entry_limit=entry_limit, entry_notional_cap=notional_cap,
    )


def broker_order(outcome=None):
    intent = outcome.intent if outcome is not None else approved_outcome().intent
    assert intent is not None
    return {
        "id": "broker-order-1", "client_order_id": intent.idempotency_key,
        "symbol": INSTRUMENT.symbol, "side": "buy", "status": "accepted",
        "order_class": "oto", "qty": str(intent.quantity), "filled_qty": "0",
        "limit_price": str(intent.limit_price), "stop_price": None,
        "created_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
        "legs": [{
            "id": "protective-stop-1", "type": "stop", "side": "sell",
            "status": "held", "qty": str(intent.quantity),
            "stop_price": str(intent.stop_price),
        }],
    }


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, path, payload=None):
        self.requests.append((method, path, payload))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class PaperBrokerTests(unittest.TestCase):
    def setUp(self):
        self.outcome = approved_outcome()
        self.account_response = {
            "id": "paper-account", "status": "ACTIVE",
            "account_blocked": False, "trading_blocked": False,
            "currency": "USD", "equity": "10000", "buying_power": "10000",
        }
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.journal = PaperOrderJournal(Path(self.tempdir.name) / "paper.sqlite3")

    def broker(self, transport, *, enabled=True):
        return AlpacaPaperBroker(
            paper_config(enabled=enabled), self.journal,
            transport=transport, clock=lambda: NOW,
        )

    def test_paper_account_is_normalized_and_rejects_live_or_blocked_account(self):
        broker = self.broker(FakeTransport([self.account_response]))
        account = broker.account()
        self.assertEqual(account.mode, RuntimeMode.PAPER)
        self.assertEqual(account.account_reference, "paper-account")
        self.assertEqual(account.equity, D("10000"))

        for changes in (
            {"status": "ACCOUNT_CLOSED"},
            {"trading_blocked": True},
            {"account_blocked": True},
            {"currency": "MXN"},
        ):
            response = dict(self.account_response, **changes)
            with self.subTest(changes=changes), self.assertRaises(PaperBrokerError):
                self.broker(FakeTransport([response])).account()

    def test_open_orders_rejects_a_full_limit_response(self):
        broker = self.broker(FakeTransport([[{}] * 500]))
        with self.assertRaises(PaperBrokerError) as error:
            broker.open_orders()
        self.assertEqual(error.exception.code, "BROKER_RESPONSE_INVALID")

    def test_submit_sends_only_approved_oto_limit_with_attached_stop(self):
        transport = FakeTransport([
            PaperBrokerError("BROKER_NOT_FOUND", "not found", status=404),
            self.account_response, [], [], broker_order(self.outcome),
        ])
        result = self.broker(transport).submit_approved(self.outcome, INSTRUMENT)
        self.assertEqual(result.order_class, "oto")
        self.assertEqual(transport.requests[-1][0:2], ("POST", "/v2/orders"))
        payload = transport.requests[-1][2]
        self.assertEqual(payload["side"], "buy")
        self.assertEqual(payload["type"], "limit")
        self.assertEqual(payload["order_class"], "oto")
        self.assertEqual(payload["time_in_force"], "day")
        self.assertFalse(payload["extended_hours"])
        self.assertEqual(payload["client_order_id"], self.outcome.intent.idempotency_key)
        self.assertEqual(payload["stop_loss"]["stop_price"], str(self.outcome.intent.stop_price))
        self.assertEqual(self.journal.unresolved()[0]["status"], "accepted")

    def test_lifecycle_test_submits_capped_passive_order_and_cancels_unfilled(self):
        outcome = approved_outcome(
            bid=D("99.99"), ask=D("100"), entry_limit=D("99.98"),
            notional_cap=D("100"),
        )
        self.assertTrue(outcome.decision.allowed)
        self.assertLess(outcome.intent.limit_price, D("99.99"))
        self.assertLessEqual(
            outcome.intent.quantity * outcome.intent.limit_price, D("100"),
        )
        accepted = broker_order(outcome)
        accepted["legs"][0]["status"] = "new"
        canceled = dict(accepted, status="canceled")
        transport = FakeTransport([
            PaperBrokerError("BROKER_NOT_FOUND", "not found", status=404),
            self.account_response, [], [], accepted,
            accepted, None, canceled,
        ])
        broker = AlpacaPaperBroker(
            paper_config(capabilities_verified=False), self.journal,
            transport=transport, clock=lambda: NOW,
        )

        submitted = broker.submit_lifecycle_test(
            outcome, INSTRUMENT, max_notional=D("100"),
        )
        result = broker.cancel_lifecycle_test(
            submitted, max_notional=D("100"),
        )

        self.assertEqual(result.status, "canceled")
        self.assertEqual(result.filled_quantity, D("0"))
        self.assertEqual(
            [request[0] for request in transport.requests],
            ["GET", "GET", "GET", "GET", "POST", "GET", "DELETE", "GET"],
        )
        self.assertEqual(self.journal.unresolved(), ())
        self.assertFalse(self.journal.safety_state()[0])

    def test_lifecycle_test_refuses_existing_client_id_and_over_cap_order(self):
        outcome = approved_outcome(
            bid=D("99.99"), ask=D("100"), entry_limit=D("99.98"),
            notional_cap=D("100"),
        )
        transport = FakeTransport([
            PaperBrokerError("BROKER_NOT_FOUND", "not found", status=404),
            self.account_response, [], [], broker_order(outcome),
        ])
        broker = AlpacaPaperBroker(
            paper_config(capabilities_verified=False), self.journal,
            transport=transport, clock=lambda: NOW,
        )
        with self.assertRaises(PaperBrokerError) as error:
            broker.submit_lifecycle_test(
                outcome, INSTRUMENT, max_notional=D("100.01"),
            )
        self.assertEqual(error.exception.code, "TEST_ORDER_LIMIT_INVALID")
        self.assertEqual(transport.requests, [])

        broker.submit_lifecycle_test(
            outcome, INSTRUMENT, max_notional=D("100"),
        )
        with self.assertRaises(PaperBrokerError) as duplicate:
            broker.submit_lifecycle_test(
                outcome, INSTRUMENT, max_notional=D("100"),
            )
        self.assertEqual(duplicate.exception.code, "TEST_ORDER_ALREADY_EXISTS")

    def test_lifecycle_test_stops_and_pauses_if_order_fills_before_cancel(self):
        outcome = approved_outcome(
            bid=D("99.99"), ask=D("100"), entry_limit=D("99.98"),
            notional_cap=D("100"),
        )
        accepted = broker_order(outcome)
        accepted["legs"][0]["status"] = "new"
        filled = dict(accepted, status="filled", filled_qty="1")
        filled["legs"][0].update(status="new", qty="1")
        transport = FakeTransport([
            PaperBrokerError("BROKER_NOT_FOUND", "not found", status=404),
            self.account_response, [], [], accepted, filled,
        ])
        broker = AlpacaPaperBroker(
            paper_config(capabilities_verified=False), self.journal,
            transport=transport, clock=lambda: NOW,
        )
        submitted = broker.submit_lifecycle_test(
            outcome, INSTRUMENT, max_notional=D("100"),
        )

        with self.assertRaises(PaperOrderUnknownError) as error:
            broker.cancel_lifecycle_test(
                submitted, max_notional=D("100"),
            )

        self.assertEqual(error.exception.code, "TEST_ORDER_FILLED")
        self.assertEqual(transport.requests[-1][0], "GET")
        self.assertEqual(self.journal.unresolved()[0]["status"], "protected")
        self.assertTrue(self.journal.safety_state()[0])

    def test_lifecycle_test_halts_when_cancellation_is_uncertain(self):
        outcome = approved_outcome(
            bid=D("99.99"), ask=D("100"), entry_limit=D("99.98"),
            notional_cap=D("100"),
        )
        accepted = broker_order(outcome)
        accepted["legs"][0]["status"] = "new"
        transport = FakeTransport([
            PaperBrokerError("BROKER_NOT_FOUND", "not found", status=404),
            self.account_response, [], [], accepted, accepted, None, accepted,
        ])
        broker = AlpacaPaperBroker(
            paper_config(capabilities_verified=False), self.journal,
            transport=transport, clock=lambda: NOW,
        )
        submitted = broker.submit_lifecycle_test(
            outcome, INSTRUMENT, max_notional=D("100"),
        )

        with self.assertRaises(PaperOrderUnknownError) as error:
            broker.cancel_lifecycle_test(
                submitted, max_notional=D("100"),
            )

        self.assertEqual(error.exception.code, "ORDER_CANCEL_UNCONFIRMED")
        self.assertEqual(self.journal.unresolved()[0]["status"], "unknown")
        self.assertTrue(self.journal.safety_state()[0])

    def test_submission_is_off_by_default_and_never_connects(self):
        transport = FakeTransport([])
        broker = self.broker(transport, enabled=False)
        with self.assertRaises(PaperBrokerError) as error:
            broker.submit_approved(self.outcome, INSTRUMENT)
        self.assertEqual(error.exception.code, "PAPER_SUBMISSION_DISABLED")
        self.assertEqual(transport.requests, [])

    def test_submission_requires_explicit_account_capability_verification(self):
        transport = FakeTransport([])
        config = replace(
            paper_config(), paper_execution_capabilities_verified=False,
        )
        broker = AlpacaPaperBroker(
            config, self.journal, transport=transport, clock=lambda: NOW,
        )
        with self.assertRaises(PaperBrokerError) as error:
            broker.submit_approved(self.outcome, INSTRUMENT)
        self.assertEqual(error.exception.code, "PAPER_CAPABILITIES_UNVERIFIED")
        self.assertEqual(transport.requests, [])

    def test_invalid_or_expired_risk_approval_never_reaches_broker(self):
        transport = FakeTransport([])
        broker = self.broker(transport)
        rejected = replace(
            self.outcome,
            decision=replace(
                self.outcome.decision, allowed=False, reason_codes=("TEST_REJECT",),
            ),
        )
        with self.assertRaises(PaperBrokerError) as error:
            broker.submit_approved(rejected, INSTRUMENT)
        self.assertEqual(error.exception.code, "RISK_NOT_APPROVED")
        expired_intent = replace(
            self.outcome.intent, expires_at=NOW + timedelta(milliseconds=500),
        )
        expired = replace(self.outcome, intent=expired_intent)
        expired_broker = AlpacaPaperBroker(
            paper_config(), self.journal, transport=transport,
            clock=lambda: NOW + timedelta(seconds=1),
        )
        with self.assertRaises(PaperBrokerError) as error:
            expired_broker.submit_approved(expired, INSTRUMENT)
        self.assertEqual(error.exception.code, "SIGNAL_EXPIRED")
        self.assertEqual(transport.requests, [])

    def test_short_mode_mismatch_and_changed_equity_are_rejected(self):
        transport = FakeTransport([])
        broker = self.broker(transport)
        short_intent = replace(
            self.outcome.intent, direction=TradeDirection.SHORT,
            stop_price=self.outcome.intent.limit_price + D(1),
        )
        with self.assertRaises(PaperBrokerError) as error:
            broker.submit_approved(replace(self.outcome, intent=short_intent), INSTRUMENT)
        self.assertEqual(error.exception.code, "RISK_APPROVAL_INVALID")
        backtest_intent = replace(self.outcome.intent, mode=RuntimeMode.BACKTEST)
        with self.assertRaises(PaperBrokerError) as error:
            broker.submit_approved(replace(self.outcome, intent=backtest_intent), INSTRUMENT)
        self.assertEqual(error.exception.code, "RISK_APPROVAL_INVALID")
        self.assertEqual(transport.requests, [])

    def test_existing_position_or_order_blocks_submission(self):
        position = {
            "symbol": "OTHER", "qty": "1", "side": "long", "market_value": "100",
        }
        for positions, orders in (([position], []), ([], [broker_order(self.outcome)])):
            transport = FakeTransport([
                PaperBrokerError("BROKER_NOT_FOUND", "not found", status=404),
                self.account_response, positions, orders,
            ])
            with self.subTest(positions=positions, orders=orders), self.assertRaises(
                PaperBrokerError,
            ) as error:
                self.broker(transport).submit_approved(self.outcome, INSTRUMENT)
            self.assertEqual(error.exception.code, "BROKER_STATE_UNKNOWN")
            self.assertNotIn("POST", [request[0] for request in transport.requests])

    def test_ambiguous_post_persists_halt_and_retry_does_not_resubmit(self):
        transport = FakeTransport([
            PaperBrokerError("BROKER_NOT_FOUND", "not found", status=404),
            self.account_response, [], [],
            PaperOrderUnknownError("BROKER_STATE_UNKNOWN", "timeout"),
            PaperBrokerError("BROKER_NOT_FOUND", "not found", status=404),
        ])
        broker = self.broker(transport)
        with self.assertRaises(PaperOrderUnknownError):
            broker.submit_approved(self.outcome, INSTRUMENT)
        self.assertEqual(self.journal.unresolved()[0]["status"], "unknown")
        restarted_journal = PaperOrderJournal(self.journal.path)
        self.assertEqual(restarted_journal.unresolved()[0]["status"], "unknown")
        with self.assertRaises(PaperOrderUnknownError):
            broker.submit_approved(self.outcome, INSTRUMENT)
        self.assertEqual(
            [request[0] for request in transport.requests].count("POST"), 1,
        )

    def test_concurrent_journal_claim_prevents_second_post(self):
        journal = self.journal

        class RacingTransport(FakeTransport):
            def request(inner_self, method, path, payload=None):
                inner_self.requests.append((method, path, payload))
                if path.startswith("/v2/orders:by_client_order_id"):
                    raise PaperBrokerError("BROKER_NOT_FOUND", "not found", status=404)
                if path == "/v2/account":
                    return self.account_response
                if path == "/v2/positions":
                    return []
                if path.startswith("/v2/orders?status=open"):
                    journal.prepare(
                        "paper-account", INSTRUMENT, self.outcome.intent, NOW,
                    )
                    return []
                raise AssertionError(f"Unexpected request: {method} {path}")

        transport = RacingTransport([])
        with self.assertRaises(PaperOrderUnknownError) as error:
            self.broker(transport).submit_approved(self.outcome, INSTRUMENT)
        self.assertEqual(error.exception.code, "BROKER_STATE_UNKNOWN")
        self.assertNotIn("POST", [request[0] for request in transport.requests])
        self.assertEqual(self.journal.unresolved()[0]["status"], "unknown")

    def test_unconfirmed_protective_stop_halts_entries(self):
        no_stop = dict(broker_order(self.outcome), legs=[])
        transport = FakeTransport([
            PaperBrokerError("BROKER_NOT_FOUND", "not found", status=404),
            self.account_response, [], [], no_stop, no_stop,
        ])
        broker = self.broker(transport)
        with self.assertRaises(PaperOrderUnknownError) as error:
            broker.submit_approved(self.outcome, INSTRUMENT)
        self.assertEqual(error.exception.code, "PROTECTIVE_EXIT_UNCONFIRMED")
        self.assertEqual(self.journal.unresolved()[0]["status"], "unknown")

    def test_filled_entry_remains_journaled_while_protective_stop_is_open(self):
        filled_order = broker_order(self.outcome)
        filled_order["status"] = "filled"
        filled_order["filled_qty"] = str(self.outcome.intent.quantity)
        filled_order["legs"][0]["status"] = "new"
        transport = FakeTransport([
            PaperBrokerError("BROKER_NOT_FOUND", "not found", status=404),
            self.account_response, [], [], filled_order,
        ])
        result = self.broker(transport).submit_approved(self.outcome, INSTRUMENT)
        self.assertEqual(result.status, "filled")
        self.assertEqual(self.journal.unresolved()[0]["status"], "protected")

    def test_filled_protective_stop_closes_journal_and_reconcile_is_idempotent(self):
        closed_order = broker_order(self.outcome)
        closed_order["status"] = "filled"
        closed_order["filled_qty"] = str(self.outcome.intent.quantity)
        closed_order["legs"][0].update(
            status="filled", filled_qty=str(self.outcome.intent.quantity),
        )
        self.journal.prepare(
            "paper-account", INSTRUMENT, self.outcome.intent, NOW,
        )
        self.journal.update(
            self.outcome.intent, "protected", "broker-order-1", NOW,
            "Test filled entry with open stop",
        )
        transport = FakeTransport([closed_order])
        broker = self.broker(transport)

        result = broker.reconcile()
        repeated = broker.reconcile()

        self.assertEqual(result[0].status, "filled")
        self.assertEqual(repeated, ())
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(self.journal.unresolved(), ())

    def test_filled_stop_does_not_close_an_entry_that_can_still_fill(self):
        active_entry = broker_order(self.outcome)
        active_entry["status"] = "partially_filled"
        active_entry["filled_qty"] = "1"
        active_entry["legs"][0].update(status="filled", filled_qty="1")
        self.journal.prepare(
            "paper-account", INSTRUMENT, self.outcome.intent, NOW,
        )
        self.journal.update(
            self.outcome.intent, "partially_filled", "broker-order-1", NOW,
            "Test entry and stop fills",
        )

        with self.assertRaises(PaperOrderUnknownError) as error:
            self.broker(FakeTransport([active_entry])).reconcile()

        self.assertEqual(error.exception.code, "PROTECTIVE_EXIT_UNCONFIRMED")
        self.assertEqual(self.journal.unresolved()[0]["status"], "unknown")

    def test_rejected_partial_entry_remains_journaled_when_stop_is_open(self):
        rejected_entry = broker_order(self.outcome)
        rejected_entry["status"] = "rejected"
        rejected_entry["filled_qty"] = "1"
        rejected_entry["legs"][0].update(status="new", filled_qty="0")
        self.journal.prepare(
            "paper-account", INSTRUMENT, self.outcome.intent, NOW,
        )
        self.journal.update(
            self.outcome.intent, "partially_filled", "broker-order-1", NOW,
            "Test rejected partial entry",
        )

        self.broker(FakeTransport([rejected_entry])).reconcile()

        self.assertEqual(self.journal.unresolved()[0]["status"], "protected")

    def test_expired_partial_fill_cancels_remainder_and_verifies_stop_coverage(self):
        partial_order = broker_order(self.outcome)
        partial_order["status"] = "partially_filled"
        partial_order["filled_qty"] = "1"
        partial_order["legs"][0]["qty"] = "1"
        partial_order["legs"][0]["status"] = "new"
        canceled_order = dict(partial_order, status="canceled")
        self.journal.prepare(
            "paper-account", INSTRUMENT, self.outcome.intent, NOW,
        )
        self.journal.update(
            self.outcome.intent, "partially_filled", "broker-order-1", NOW,
            "Test partial fill",
        )
        transport = FakeTransport([partial_order, None, canceled_order])
        broker = AlpacaPaperBroker(
            paper_config(), self.journal, transport=transport,
            clock=lambda: NOW + timedelta(minutes=5),
        )
        result = broker.reconcile(cancel_expired=True)
        self.assertEqual(result[0].status, "canceled")
        self.assertEqual(transport.requests[1][0], "DELETE")
        self.assertEqual(self.journal.unresolved()[0]["status"], "protected")

    def test_uncertain_expired_order_cancel_can_be_recovered_after_restart(self):
        partial_order = broker_order(self.outcome)
        partial_order["status"] = "partially_filled"
        partial_order["filled_qty"] = "1"
        partial_order["legs"][0]["qty"] = "1"
        partial_order["legs"][0]["status"] = "new"
        canceled_order = dict(partial_order, status="canceled")
        self.journal.prepare(
            "paper-account", INSTRUMENT, self.outcome.intent, NOW,
        )
        self.journal.update(
            self.outcome.intent, "partially_filled", "broker-order-1", NOW,
            "Test interrupted cancellation",
        )
        clock = lambda: NOW + timedelta(minutes=5)
        interrupted_transport = FakeTransport([
            partial_order,
            PaperBrokerError("BROKER_UNAVAILABLE", "cancel response lost"),
        ])
        broker = AlpacaPaperBroker(
            paper_config(), self.journal, transport=interrupted_transport,
            clock=clock,
        )

        with self.assertRaises(PaperOrderUnknownError) as error:
            broker.reconcile(cancel_expired=True)

        self.assertEqual(error.exception.code, "BROKER_STATE_UNKNOWN")
        self.assertEqual(self.journal.unresolved()[0]["status"], "unknown")
        self.assertTrue(self.journal.safety_state()[0])

        restarted_journal = PaperOrderJournal(self.journal.path)
        recovered = AlpacaPaperBroker(
            paper_config(), restarted_journal,
            transport=FakeTransport([canceled_order]), clock=clock,
        )
        result = recovered.reconcile(acknowledge_reconciled=True)

        self.assertEqual(result[0].status, "canceled")
        self.assertEqual(restarted_journal.unresolved()[0]["status"], "protected")
        self.assertFalse(restarted_journal.safety_state()[0])

    def test_expired_cancel_still_active_keeps_entries_halted(self):
        partial_order = broker_order(self.outcome)
        partial_order["status"] = "partially_filled"
        partial_order["filled_qty"] = "1"
        partial_order["legs"][0]["qty"] = "1"
        partial_order["legs"][0]["status"] = "new"
        self.journal.prepare(
            "paper-account", INSTRUMENT, self.outcome.intent, NOW,
        )
        self.journal.update(
            self.outcome.intent, "partially_filled", "broker-order-1", NOW,
            "Test unconfirmed cancellation",
        )
        transport = FakeTransport([partial_order, None, partial_order])
        broker = AlpacaPaperBroker(
            paper_config(), self.journal, transport=transport,
            clock=lambda: NOW + timedelta(minutes=5),
        )

        with self.assertRaises(PaperOrderUnknownError) as error:
            broker.reconcile(cancel_expired=True)

        self.assertEqual(error.exception.code, "ORDER_CANCEL_UNCONFIRMED")
        self.assertEqual(self.journal.unresolved()[0]["status"], "unknown")
        self.assertTrue(self.journal.safety_state()[0])

    def test_reconciliation_requires_explicit_acknowledgment_to_clear_halt(self):
        self.journal.prepare("paper-account", INSTRUMENT, self.outcome.intent, NOW)
        self.journal.update(
            self.outcome.intent, "unknown", None, NOW, "Test unknown state",
        )
        broker = self.broker(FakeTransport([broker_order(self.outcome)]))
        broker.reconcile()
        self.assertTrue(self.journal.safety_state()[0])

        broker = self.broker(FakeTransport([broker_order(self.outcome)]))
        broker.reconcile(acknowledge_reconciled=True)
        self.assertFalse(self.journal.safety_state()[0])

    def test_restart_reconciles_persisted_unknown_order_before_clearing_halt(self):
        self.journal.prepare("paper-account", INSTRUMENT, self.outcome.intent, NOW)
        self.journal.update(
            self.outcome.intent, "unknown", None, NOW, "Interrupted submission",
        )
        restarted_journal = PaperOrderJournal(self.journal.path)
        broker = AlpacaPaperBroker(
            paper_config(), restarted_journal,
            transport=FakeTransport([broker_order(self.outcome)]),
            clock=lambda: NOW,
        )

        broker.reconcile(acknowledge_reconciled=True)

        self.assertFalse(restarted_journal.safety_state()[0])
        self.assertEqual(restarted_journal.unresolved()[0]["status"], "accepted")

    def test_journal_serializes_entries_and_sets_private_permissions(self):
        journal_path = Path(self.tempdir.name) / "mode.sqlite3"
        journal = PaperOrderJournal(journal_path)
        self.assertEqual(journal_path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(journal.prepare("paper-account", INSTRUMENT, self.outcome.intent, NOW))
        second_intent = replace(
            self.outcome.intent, correlation_id="different-correlation",
            idempotency_key="another-client-id",
        )
        with self.assertRaises(PaperBrokerError) as error:
            journal.prepare("paper-account", INSTRUMENT, second_intent, NOW)
        self.assertEqual(error.exception.code, "PAPER_ORDER_UNRESOLVED")

    def test_journal_prunes_operational_events_but_keeps_order_state(self):
        self.journal.prepare("paper-account", INSTRUMENT, self.outcome.intent, NOW)
        with sqlite3.connect(self.journal.path) as db:
            db.execute(
                """INSERT INTO audit_events(
                    occurred_at, event_type, correlation_id, client_order_id, detail
                ) VALUES (?, ?, ?, ?, ?)""",
                (
                    (NOW - timedelta(days=8)).isoformat(), "old_event",
                    "old-correlation", "old-client-id", "old detail",
                ),
            )
        self.journal.set_status(
            self.outcome.intent.idempotency_key, "accepted", "broker-order-1", NOW,
        )
        with sqlite3.connect(self.journal.path) as db:
            events = db.execute("SELECT event_type FROM audit_events").fetchall()
            order_count = db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        self.assertNotIn(("old_event",), events)
        self.assertEqual(order_count, 1)

    def test_journal_startup_prunes_expired_operational_events(self):
        with sqlite3.connect(self.journal.path) as db:
            db.execute(
                """INSERT INTO audit_events(
                    occurred_at, event_type, correlation_id, client_order_id, detail
                ) VALUES (?, ?, ?, ?, ?)""",
                (
                    "2026-01-01T00:00:00+00:00", "stale", "old-correlation",
                    "old-order", "stale event",
                ),
            )
        PaperOrderJournal(self.journal.path)
        with sqlite3.connect(self.journal.path) as db:
            events = db.execute("SELECT event_type FROM audit_events").fetchall()
        self.assertNotIn(("stale",), events)

    def test_circuit_baseline_and_latch_persist_across_journal_restart(self):
        initial = CircuitState(
            SESSION.opens_at, SESSION.closes_at, D("10000"),
        )
        self.journal.save_circuit_state(
            "paper-account", SESSION, initial, NOW,
        )
        restarted = PaperOrderJournal(self.journal.path)
        self.assertEqual(
            restarted.load_circuit_state("paper-account", SESSION), initial,
        )

        latched = replace(initial, tripped=True)
        restarted.save_circuit_state("paper-account", SESSION, latched, NOW)
        saved = restarted.save_circuit_state(
            "paper-account", SESSION, initial, NOW,
        )
        self.assertTrue(saved.tripped)
        self.assertTrue(
            PaperOrderJournal(self.journal.path)
            .load_circuit_state("paper-account", SESSION).tripped,
        )
        with self.assertRaises(PaperBrokerError) as error:
            restarted.save_circuit_state(
                "paper-account", SESSION, replace(initial, start_equity=D("11000")), NOW,
            )
        self.assertEqual(error.exception.code, "PAPER_CIRCUIT_CONFLICT")

    def test_operator_pause_persists_and_does_not_overwrite_existing_halt(self):
        self.journal.prepare(
            "paper-account", INSTRUMENT, self.outcome.intent, NOW,
        )
        paused = self.journal.pause_entries()
        self.assertEqual(paused[0], True)
        self.assertIn("OPERATOR_PAUSE", paused[1])
        self.assertEqual(
            PaperOrderJournal(self.journal.path).safety_state(), paused,
        )
        self.journal.update(
            self.outcome.intent, "unknown", None, NOW, "Broker unknown.",
        )
        original_reason = self.journal.safety_state()[1]
        self.assertEqual(
            PaperOrderJournal(self.journal.path).pause_entries()[1],
            original_reason,
        )

    def test_pause_and_resume_cli_use_persistent_journal_and_explicit_reconcile(self):
        output = io.StringIO()
        journal_path = self.journal.path
        with redirect_stdout(output):
            pause_code = main(["paper-pause", "--journal", str(journal_path)])
        self.assertEqual(pause_code, 0)
        self.assertTrue(self.journal.safety_state()[0])
        self.assertIn("Existing positions and orders are unchanged.", output.getvalue())

        output = io.StringIO()
        with (
            patch.dict(
                "os.environ",
                {
                    "TRADER_BOT_MODE": "paper",
                    "TRADER_BOT_PAPER_API_KEY": "fake-key",
                    "TRADER_BOT_PAPER_API_SECRET": "fake-secret",
                },
                clear=True,
            ),
            patch("trader_bot.cli.AlpacaPaperBroker") as broker_type,
            redirect_stdout(output),
        ):
            broker_type.return_value.reconcile.side_effect = (
                lambda **kwargs: self.journal.clear_halt_after_reconciliation()
            )
            broker_type.return_value.account.return_value = AccountSnapshot(
                "paper-account", RuntimeMode.PAPER, D("10000"), D("10000"),
                "USD", NOW,
            )
            broker_type.return_value.positions.return_value = ()
            broker_type.return_value.open_orders.return_value = ()
            resume_code = main(["paper-resume", "--journal", str(journal_path)])
        self.assertEqual(resume_code, 0)
        self.assertFalse(self.journal.safety_state()[0])
        self.assertIn("resumed after successful broker reconciliation", output.getvalue())
        self.assertEqual(
            broker_type.return_value.reconcile.call_args_list,
            [call(), call(acknowledge_reconciled=True)],
        )
        with sqlite3.connect(journal_path) as db:
            events = db.execute(
                "SELECT event_type FROM audit_events ORDER BY id",
            ).fetchall()
        self.assertIn(("operator_pause",), events)
        self.assertIn(("operator_resume",), events)

    def test_paper_resume_keeps_entry_pause_when_broker_is_not_flat(self):
        self.journal.pause_entries()
        with (
            patch.dict(
                "os.environ",
                {
                    "TRADER_BOT_MODE": "paper",
                    "TRADER_BOT_PAPER_API_KEY": "fake-key",
                    "TRADER_BOT_PAPER_API_SECRET": "fake-secret",
                },
                clear=True,
            ),
            patch("trader_bot.cli.AlpacaPaperBroker") as broker_type,
        ):
            broker_type.return_value.positions.return_value = (object(),)
            with redirect_stdout(io.StringIO()):
                exit_code = main(["paper-resume", "--journal", str(self.journal.path)])
        self.assertEqual(exit_code, 2)
        self.assertTrue(self.journal.safety_state()[0])
        self.assertEqual(
            broker_type.return_value.reconcile.call_args_list, [call()],
        )

    def test_fixed_origin_and_no_redirect_are_used_by_transport(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"ok":true}'
        opener = MagicMock()
        opener.open.return_value = response
        config = paper_config()
        with patch(
            "trader_bot.paper_broker.build_opener", return_value=opener,
        ) as build_opener:
            result = PaperJsonTransport(config).request("GET", "/v2/account")
        self.assertEqual(result, {"ok": True})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, PAPER_API_BASE_URL + "/v2/account")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Apca-api-key-id"), "paper-key")
        self.assertEqual(build_opener.call_args.args[0].proxies, {})
        self.assertEqual(
            opener.open.call_args.kwargs["timeout"], 10,
        )
        self.assertEqual(response.read.call_args.args[0], 1_000_001)

    def test_transport_sanitizes_http_errors_and_does_not_follow_redirects(self):
        opener = MagicMock()
        opener.open.side_effect = HTTPError(
            PAPER_API_BASE_URL + "/v2/orders", 302, "redirect", {}, None,
        )
        with patch("trader_bot.paper_broker.build_opener", return_value=opener):
            with self.assertRaises(PaperBrokerError) as error:
                PaperJsonTransport(paper_config()).request("POST", "/v2/orders", {})
        self.assertNotIn("paper-key", str(error.exception))
        self.assertNotIn("paper-secret", str(error.exception))
        self.assertEqual(opener.open.call_count, 1)


if __name__ == "__main__":
    unittest.main()
