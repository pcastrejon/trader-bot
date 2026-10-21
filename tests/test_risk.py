import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, ROUND_DOWN, localcontext

from trader_bot.config import PaperCredentials
from trader_bot.contracts import AccountSnapshot, CandidateSignal, RuntimeMode, TradeDirection
from trader_bot.market_data import DataError
from trader_bot.risk import (
    CircuitState, DailyCircuitBreaker, Exposure, RiskOutcome, RiskPolicy, RiskWarden,
)
from tests.support import INSTRUMENT, NOW, POLICY, RUNTIME, SESSION, quote_at


D = Decimal


def candidate() -> CandidateSignal:
    return CandidateSignal(
        "risk-test", INSTRUMENT.instrument_id, "experimental-ema-atr", "test-1",
        TradeDirection.LONG, NOW, ("fixture",), D(98), NOW + timedelta(minutes=5),
    )


class RiskWardenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = RiskPolicy()
        self.warden = RiskWarden(self.policy)
        self.signal = candidate()
        self.quote = replace(quote_at(), bid=D("99.99"), ask=D(100))
        self.account = AccountSnapshot("test", RuntimeMode.BACKTEST, D(10000), D(10000), "USD", NOW)
        self.circuit = DailyCircuitBreaker(CircuitState(
            SESSION.opens_at, SESSION.closes_at, D(10000),
        ))
        self.runtime = RUNTIME

    def evaluate(
        self, *, exposures: tuple[Exposure, ...] = (), at=NOW, entry_limit=None,
        reconciled=True, protective_exit_supported=True, borrow_available=False,
        borrow_cost_per_share=None,
    ) -> RiskOutcome:
        return self.warden.evaluate(
            self.signal, INSTRUMENT, self.quote, self.account, exposures,
            self.circuit, SESSION, self.runtime, POLICY, at,
            entry_limit=entry_limit, reconciled=reconciled,
            protective_exit_supported=protective_exit_supported,
            borrow_available=borrow_available, borrow_cost_per_share=borrow_cost_per_share,
        )

    def paper(self, *, shorting=False, leverage=False) -> None:
        self.runtime = replace(
            RUNTIME, mode=RuntimeMode.PAPER, paper_credentials=PaperCredentials("fake", "fake"),
            paper_allow_shorting=shorting, paper_allow_leverage=leverage,
        )
        self.account = replace(self.account, mode=RuntimeMode.PAPER)

    def test_default_position_cap_and_intent_shape(self) -> None:
        result = self.evaluate()
        self.assertTrue(result.decision.allowed)
        assert result.intent is not None
        self.assertEqual(result.intent.quantity, D(5))
        self.assertEqual(result.intent.limit_price, D(100))
        self.assertEqual(result.intent.stop_price, D(98))
        self.assertEqual(result.intent.planned_risk, D(10))
        self.assertEqual(result.intent.entry_type, "limit")
        self.assertEqual(result.intent.protective_type, "stop_market")
        self.assertEqual(result.intent.expires_at, self.signal.expires_at)
        self.assertTrue(result.checks)

    def test_risk_budget_and_rounding_never_exceeded(self) -> None:
        self.warden = RiskWarden(replace(self.policy, position_notional=D("1")))
        for stop in ("99.99", "99", "98", "97.31", "90", "1"):
            self.signal = replace(candidate(), protective_stop_price=D(stop))
            result = self.evaluate()
            if result.intent is not None:
                self.assertLessEqual(result.intent.planned_risk, D(25))
                self.assertLessEqual(result.intent.quantity * result.intent.limit_price, D(10000))
                self.assertEqual(result.intent.quantity % self.policy.quantity_step, 0)
            else:
                self.assertIn("RISK_PER_TRADE", result.decision.reason_codes)

    def test_cost_allowance_reduces_quantity(self) -> None:
        self.warden = RiskWarden(replace(
            self.policy, position_notional=D("1"), round_trip_cost_per_share=D("0.5"),
        ))
        result = self.evaluate()
        assert result.intent is not None
        self.assertEqual(result.intent.quantity, D(10))
        self.assertEqual(result.intent.planned_risk, D(25))

    def test_fractional_increment_and_adverse_stop_rounding(self) -> None:
        self.signal = replace(self.signal, protective_stop_price=D("98.009"))
        self.quote = replace(self.quote, ask=D("100.009"))
        self.warden = RiskWarden(replace(self.policy, quantity_step=D("0.1")))
        result = self.evaluate()
        assert result.intent is not None
        self.assertEqual(result.intent.limit_price, D("100.00"))
        self.assertEqual(result.intent.stop_price, D("98.00"))
        self.assertLessEqual(result.intent.quantity * result.intent.limit_price, D(500))

    def test_buying_power_includes_cost_allowance(self) -> None:
        self.account = replace(self.account, buying_power=D("100.05"))
        self.warden = RiskWarden(replace(self.policy, round_trip_cost_per_share=D("0.10")))
        self.assertEqual(self.evaluate().decision.reason_codes, ("BUYING_POWER",))

    def test_open_and_reserved_risk_cap_inclusive(self) -> None:
        for risk in ("99", "100", "101"):
            exposure = Exposure("OTHER", TradeDirection.LONG, D(1000), D(risk))
            self.assertEqual(self.evaluate(exposures=(exposure,)).decision.reason_codes, ("OPEN_RISK",))
        result = self.evaluate(exposures=(Exposure("OTHER", TradeDirection.LONG, D(1000), D(98)),))
        assert result.intent is not None
        self.assertEqual(result.intent.quantity, D(1))

    def test_duplicate_instrument_reservation_rejected(self) -> None:
        exposure = Exposure(INSTRUMENT.instrument_id, TradeDirection.LONG, D(100), D(2))
        self.assertEqual(self.evaluate(exposures=(exposure,)).decision.reason_codes, ("POSITION_EXISTS",))

    def test_gross_cap_without_leverage_and_paper_only_override(self) -> None:
        exposure = Exposure("OTHER", TradeDirection.LONG, D(10000), D(0))
        self.assertEqual(self.evaluate(exposures=(exposure,)).decision.reason_codes, ("GROSS_EXPOSURE",))
        self.paper()
        self.assertFalse(self.evaluate(exposures=(exposure,)).decision.allowed)
        self.paper(leverage=True)
        self.assertTrue(self.evaluate(exposures=(exposure,)).decision.allowed)
        exposure = replace(exposure, notional=D(12500))
        self.assertEqual(self.evaluate(exposures=(exposure,)).decision.reason_codes, ("GROSS_EXPOSURE",))

    def test_short_requires_paper_opt_in_and_explicit_borrow(self) -> None:
        self.signal = replace(self.signal, direction=TradeDirection.SHORT, protective_stop_price=D(102))
        self.assertEqual(self.evaluate().decision.reason_codes, ("MODE_FORBIDDEN",))
        self.paper()
        self.assertEqual(self.evaluate().decision.reason_codes, ("MODE_FORBIDDEN",))
        self.paper(shorting=True)
        self.assertEqual(self.evaluate().decision.reason_codes, ("BORROW_UNAVAILABLE",))
        self.assertEqual(
            self.evaluate(borrow_available=True).decision.reason_codes, ("BORROW_UNAVAILABLE",),
        )
        result = self.evaluate(borrow_available=True, borrow_cost_per_share=D("0.01"))
        assert result.intent is not None
        self.assertLessEqual(result.intent.quantity * result.intent.limit_price, D(250))
        self.assertGreater(result.intent.stop_price, result.intent.limit_price)

    def test_aggregate_short_cap_is_not_net_exposure(self) -> None:
        self.paper(shorting=True, leverage=True)
        self.signal = replace(self.signal, direction=TradeDirection.SHORT, protective_stop_price=D(102))
        exposure = Exposure("OTHER", TradeDirection.SHORT, D(1000), D(0))
        self.assertEqual(self.evaluate(
            exposures=(exposure,), borrow_available=True, borrow_cost_per_share=D(0),
        ).decision.reason_codes, ("SHORT_CAP",))

    def test_existing_short_breach_also_blocks_new_long(self) -> None:
        self.paper(shorting=True)
        exposure = Exposure("OTHER", TradeDirection.SHORT, D(1001), D(0))
        self.assertEqual(self.evaluate(exposures=(exposure,)).decision.reason_codes, ("SHORT_CAP",))

    def test_daily_breaker_exact_threshold_latches_after_recovery(self) -> None:
        self.account = replace(self.account, equity=D("9900.01"))
        self.assertTrue(self.evaluate().decision.allowed)
        self.account = replace(self.account, equity=D(9900))
        self.assertEqual(self.evaluate().decision.reason_codes, ("DAILY_DRAWDOWN",))
        self.account = replace(self.account, equity=D(11000))
        self.assertEqual(self.evaluate().decision.reason_codes, ("DAILY_DRAWDOWN",))
        restored = DailyCircuitBreaker(self.circuit.state)
        self.assertTrue(restored.observe(D(11000), NOW, D("0.01")))

    def test_breaker_requires_explicit_new_session_baseline(self) -> None:
        self.circuit = DailyCircuitBreaker(replace(
            self.circuit.state, session_open=SESSION.opens_at - timedelta(days=1),
        ))
        self.assertEqual(self.evaluate().decision.reason_codes, ("SESSION_BASELINE_UNKNOWN",))

    def test_freshness_future_expiry_and_cutoff_boundaries(self) -> None:
        self.quote = replace(self.quote, observed_at=NOW - POLICY.max_quote_age)
        self.account = replace(self.account, as_of=NOW - self.policy.max_account_age)
        self.assertTrue(self.evaluate().decision.allowed)
        self.account = replace(self.account, as_of=self.account.as_of - timedelta(microseconds=1))
        self.assertEqual(self.evaluate().decision.reason_codes, ("DATA_STALE",))
        self.setUp()
        self.assertEqual(self.evaluate(at=self.signal.expires_at).decision.reason_codes, ("SIGNAL_EXPIRED",))
        self.signal = replace(self.signal, signal_bar_ends_at=NOW + timedelta(seconds=1))
        self.assertEqual(self.evaluate().decision.reason_codes, ("SIGNAL_EXPIRED",))

    def test_session_cutoff_cannot_be_overridden_by_long_signal_expiry(self) -> None:
        cutoff = SESSION.closes_at - timedelta(minutes=30)
        self.signal = replace(self.signal, signal_bar_ends_at=cutoff,
                              expires_at=cutoff + timedelta(minutes=5))
        self.quote = replace(self.quote, observed_at=cutoff, received_at=cutoff)
        self.account = replace(self.account, as_of=cutoff)
        self.assertEqual(self.evaluate(at=cutoff).decision.reason_codes, ("SESSION_CUTOFF",))

    def test_invalid_price_tick_does_not_divide_by_zero(self) -> None:
        self.warden = RiskWarden(replace(self.policy, price_tick=D(1000)))
        self.assertEqual(self.evaluate().decision.reason_codes, ("PRICE_BOUND",))

    def test_quote_received_in_future_is_rejected(self) -> None:
        self.quote = replace(self.quote, received_at=NOW + timedelta(microseconds=1))
        self.assertEqual(self.evaluate().decision.reason_codes, ("DATA_STALE",))

    def test_unknown_state_unsupported_stop_and_fx_fail_closed(self) -> None:
        self.assertEqual(self.evaluate(reconciled=False).decision.reason_codes, ("BROKER_STATE_UNKNOWN",))
        self.assertEqual(self.evaluate(protective_exit_supported=False).decision.reason_codes,
                         ("PROTECTIVE_EXIT_UNAVAILABLE",))
        self.account = replace(self.account, currency="MXN")
        self.assertEqual(self.evaluate().decision.reason_codes, ("CURRENCY_UNSUPPORTED",))

    def test_wrong_mode_identity_stop_and_price_bound(self) -> None:
        self.account = replace(self.account, mode=RuntimeMode.PAPER)
        self.assertEqual(self.evaluate().decision.reason_codes, ("MODE_FORBIDDEN",))
        self.setUp()
        self.quote = replace(self.quote, instrument_id="OTHER")
        self.assertEqual(self.evaluate().decision.reason_codes, ("DATA_INVALID",))
        self.setUp()
        self.signal = replace(self.signal, protective_stop_price=D(101))
        self.assertEqual(self.evaluate().decision.reason_codes, ("STOP_INVALID",))
        self.setUp()
        self.assertEqual(self.evaluate(entry_limit=D(110)).decision.reason_codes, ("PRICE_BOUND",))

    def test_idempotency_key_and_precision_are_repeatable(self) -> None:
        first = self.evaluate()
        with localcontext() as context:
            context.prec = 6
            context.rounding = ROUND_DOWN
            second = self.evaluate()
        self.assertEqual(first, second)
        assert first.intent is not None
        self.assertEqual(len(first.intent.idempotency_key), 64)

    def test_policy_and_exposure_validation(self) -> None:
        for change in (
            {"open_risk": D("NaN")}, {"price_tick": D(0)},
            {"paper_gross": D(-1)}, {"daily_drawdown": D(2)},
            {"max_account_age": timedelta(0)},
        ):
            with self.subTest(change=change), self.assertRaises(DataError):
                replace(self.policy, **change)
        with self.assertRaises(DataError):
            Exposure("OTHER", TradeDirection.LONG, D(-1), D(0))

    def test_invalid_or_live_intents_rejected(self) -> None:
        intent = self.evaluate().intent
        assert intent is not None
        for change in (
            {"mode": RuntimeMode.LIVE}, {"quantity": D(0)},
            {"stop_price": D(101)}, {"expires_at": NOW},
            {"entry_type": "market"},
        ):
            with self.subTest(change=change), self.assertRaises(DataError):
                replace(intent, **change)
