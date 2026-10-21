import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from trader_bot.config import PaperCredentials, RuntimeConfig
from trader_bot.contracts import Instrument, RuntimeMode, TradeDirection
from trader_bot.indicators import atr
from trader_bot.market_data import DataError, MarketSnapshot
from trader_bot.strategy import (
    BenchmarkConfig, BenchmarkStrategy, EligibilityPolicy, Evaluation, EvaluationStatus,
    ExchangeSession,
)
from tests.support import (
    INSTRUMENT, NOW, OPEN, POLICY, RUNTIME, SESSION, bars_from_prices, quote_at, snapshot,
)


class BenchmarkStrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.strategy = BenchmarkStrategy()
        self.data = snapshot()

    def evaluate(
        self, *, instrument: Instrument = INSTRUMENT, snapshot: MarketSnapshot | None = None,
        sessions: tuple[ExchangeSession, ...] = (SESSION,), runtime: RuntimeConfig = RUNTIME,
        policy: EligibilityPolicy = POLICY, as_of: datetime = NOW,
    ) -> Evaluation:
        return self.strategy.evaluate(
            instrument, snapshot if snapshot is not None else self.data, sessions,
            runtime, policy, as_of, "case-1",
        )

    def test_recorded_long_signal_and_exact_stop_shape(self) -> None:
        result = self.evaluate()
        self.assertEqual(result.status, EvaluationStatus.SIGNAL)
        signal = result.candidate
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.direction, TradeDirection.LONG)
        self.assertEqual(signal.signal_bar_ends_at, NOW)
        self.assertEqual(signal.expires_at, NOW + timedelta(minutes=5))
        last_atr = atr(self.data.bars)[-1]
        assert last_atr is not None
        self.assertEqual(signal.protective_stop_price, Decimal(110) - 2 * last_atr)
        self.assertIn("ema-9-21-50", signal.strategy_version)
        self.assertFalse(hasattr(signal, "quantity"))
        self.assertEqual(self.evaluate(), result)

    def test_no_lookahead_before_last_crossover_bar(self) -> None:
        now = NOW - timedelta(minutes=5)
        data = replace(self.data, bars=self.data.bars[:-1], quote=quote_at(now), received_at=now)
        result = self.evaluate(snapshot=data, as_of=now)
        self.assertIsNone(result.candidate)
        self.assertNotEqual(result.status, EvaluationStatus.SIGNAL)

    def test_flat_market_reports_no_signal_not_rejection(self) -> None:
        data = replace(self.data, bars=bars_from_prices((100,) * 54))
        result = self.evaluate(snapshot=data)
        self.assertEqual(result.status, EvaluationStatus.NO_SIGNAL)
        self.assertEqual(result.code, "NO_CROSSOVER")

    def test_short_signal_is_paper_only_and_requires_opt_in(self) -> None:
        bars = bars_from_prices((100,) * 50 + (101, 102, 103, 90))
        data = replace(self.data, bars=bars)
        self.assertEqual(self.evaluate(snapshot=data).code, "MODE_FORBIDDEN")
        runtime = RuntimeConfig(
            mode=RuntimeMode.PAPER, watchlist=("DEMO",),
            paper_credentials=PaperCredentials("fake", "fake"),
        )
        self.assertEqual(self.evaluate(snapshot=data, runtime=runtime).code, "MODE_FORBIDDEN")
        result = self.evaluate(
            snapshot=data, runtime=replace(runtime, paper_allow_shorting=True)
        )
        self.assertEqual(result.status, EvaluationStatus.SIGNAL)
        assert result.candidate is not None
        self.assertEqual(result.candidate.direction, TradeDirection.SHORT)
        self.assertGreater(result.candidate.protective_stop_price, Decimal(90))

    def test_stale_quote_boundary(self) -> None:
        quote = replace(self.data.quote, observed_at=NOW - POLICY.max_quote_age)
        self.assertEqual(self.evaluate(snapshot=replace(self.data, quote=quote)).status,
                         EvaluationStatus.SIGNAL)
        quote = replace(quote, observed_at=quote.observed_at - timedelta(microseconds=1))
        self.assertEqual(self.evaluate(snapshot=replace(self.data, quote=quote)).code, "DATA_STALE")

    def test_stale_bar_boundary_and_expired_signal(self) -> None:
        now = NOW + POLICY.max_bar_age
        data = replace(self.data, quote=quote_at(now), received_at=now)
        self.assertEqual(self.evaluate(snapshot=data, as_of=now).status, EvaluationStatus.SIGNAL)
        now += timedelta(microseconds=1)
        data = replace(data, quote=quote_at(now), received_at=now)
        self.assertEqual(self.evaluate(snapshot=data, as_of=now).code, "DATA_STALE")
        now = NOW + timedelta(minutes=5)
        self.assertEqual(self.evaluate(
            snapshot=replace(data, quote=quote_at(now), received_at=now), as_of=now,
            policy=replace(POLICY, max_bar_age=timedelta(minutes=20)),
        ).code, "DATA_STALE")

    def test_missing_forming_duplicate_out_of_order_and_gap_bars_rejected(self) -> None:
        cases = (
            ((), "DATA_MISSING"),
            (self.data.bars[:-2] + (self.data.bars[-1],), "DATA_MISSING"),
            (self.data.bars[:-1] + (self.data.bars[-2],), "DATA_MISSING"),
            (tuple(reversed(self.data.bars)), "DATA_MISSING"),
            (self.data.bars + (replace(self.data.bars[-1], starts_at=NOW,
                                     ends_at=NOW + timedelta(minutes=5)),), "DATA_FUTURE"),
        )
        for bars, code in cases:
            with self.subTest(code=code, length=len(bars)):
                result = self.evaluate(snapshot=replace(self.data, bars=bars))
                self.assertEqual(result.status, EvaluationStatus.REJECTED)
                self.assertEqual(result.code, code)
                self.assertIsNone(result.candidate)

    def test_invalid_interval_source_adjustment_and_instrument(self) -> None:
        for change in (
            {"interval_seconds": 60}, {"instrument_id": "OTHER"},
            {"source": "other_feed"}, {"adjustment_basis": "split_adjusted"},
            {"ends_at": self.data.bars[0].ends_at + timedelta(seconds=1)},
        ):
            with self.subTest(change=change):
                bars = (replace(self.data.bars[0], **change),) + self.data.bars[1:]
                self.assertEqual(self.evaluate(snapshot=replace(self.data, bars=bars)).code,
                                 "DATA_INVALID")

    def test_unverified_watchlist_international_and_closed_session(self) -> None:
        for result, code in (
            (self.evaluate(runtime=replace(RUNTIME, watchlist=())), "NOT_WATCHLISTED"),
            (self.evaluate(instrument=replace(INSTRUMENT, currency="MXN")), "INSTRUMENT_UNSUPPORTED"),
            (self.evaluate(instrument=replace(INSTRUMENT, venue="OTC")), "INSTRUMENT_UNSUPPORTED"),
            (self.evaluate(policy=replace(POLICY, verified_instrument_ids=("OTHER",))), "INSTRUMENT_UNSUPPORTED"),
            (self.evaluate(sessions=()), "CALENDAR_UNKNOWN"),
            (self.evaluate(as_of=OPEN - timedelta(seconds=1)), "MARKET_CLOSED"),
        ):
            with self.subTest(code=code):
                self.assertEqual(result.code, code)

    def test_early_close_and_cutoff_boundaries(self) -> None:
        session = replace(SESSION, closes_at=NOW + timedelta(minutes=30))
        self.assertEqual(self.evaluate(sessions=(session,)).code, "SESSION_CUTOFF")
        session = replace(session, closes_at=session.closes_at + timedelta(microseconds=1))
        self.assertEqual(self.evaluate(sessions=(session,)).status, EvaluationStatus.SIGNAL)

    def test_liquidity_price_volume_and_spread_thresholds(self) -> None:
        for policy in (
            replace(POLICY, min_price=Decimal("110.01")),
            replace(POLICY, min_bar_volume=Decimal("10001")),
            replace(POLICY, max_spread_bps=Decimal("1")),
        ):
            self.assertEqual(self.evaluate(policy=policy).code, "LIQUIDITY_FILTER")
        self.assertEqual(self.evaluate(policy=replace(
            POLICY, min_price=Decimal("110"), min_bar_volume=Decimal("10000"),
        )).status, EvaluationStatus.SIGNAL)

    def test_future_quote_snapshot_and_quality_warnings_rejected(self) -> None:
        for data, code in (
            (replace(self.data, received_at=NOW + timedelta(seconds=1)), "DATA_FUTURE"),
            (replace(self.data, quote=replace(self.data.quote,
                     observed_at=NOW + timedelta(seconds=1))), "DATA_FUTURE"),
            (replace(self.data, quality_flags=("missing_volume",)), "DATA_INVALID"),
        ):
            self.assertEqual(self.evaluate(snapshot=data).code, code)

    def test_insufficient_warmup_is_not_filled_with_defaults(self) -> None:
        data = replace(self.data, bars=self.data.bars[-10:])
        self.assertEqual(self.evaluate(snapshot=data).code, "INSUFFICIENT_HISTORY")

    def test_custom_atr_multiplier_changes_version_and_stop(self) -> None:
        original = self.evaluate().candidate
        self.strategy = BenchmarkStrategy(BenchmarkConfig(atr_multiplier=Decimal(3)))
        changed = self.evaluate().candidate
        assert original is not None and changed is not None
        self.assertNotEqual(original.strategy_version, changed.strategy_version)
        self.assertLess(changed.protective_stop_price, original.protective_stop_price)

    def test_multiple_sessions_allow_only_explicit_overnight_gap(self) -> None:
        previous = replace(
            SESSION, opens_at=SESSION.opens_at - timedelta(days=1),
            closes_at=SESSION.closes_at - timedelta(days=1),
        )
        older_bars = tuple(
            replace(self.data.bars[0],
                    starts_at=previous.closes_at - timedelta(minutes=5 * (54 - index)),
                    ends_at=previous.closes_at - timedelta(minutes=5 * (53 - index)))
            for index in range(54)
        )
        data = replace(self.data, bars=older_bars + self.data.bars)
        self.assertEqual(self.evaluate(
            snapshot=data, sessions=(previous, SESSION),
        ).status, EvaluationStatus.SIGNAL)
        self.assertEqual(self.evaluate(
            snapshot=replace(data, bars=older_bars[:-1] + self.data.bars),
            sessions=(previous, SESSION),
        ).code, "DATA_MISSING")

    def test_nonpositive_stop_cannot_generate_candidate(self) -> None:
        self.strategy = BenchmarkStrategy(BenchmarkConfig(atr_multiplier=Decimal("1000")))
        self.assertEqual(self.evaluate().code, "DATA_INVALID")

    def test_zero_atr_crossover_is_rejected(self) -> None:
        with patch("trader_bot.strategy.atr", return_value=(Decimal(0),) * 54):
            self.assertEqual(self.evaluate().code, "DATA_INVALID")

    def test_invalid_strategy_parameters_cannot_start(self) -> None:
        for changes in (
            {"fast_period": 21}, {"slow_period": 60},
            {"atr_period": 0}, {"atr_multiplier": Decimal("NaN")},
            {"entry_cutoff": timedelta(minutes=29)},
        ):
            with self.subTest(changes=changes), self.assertRaises(DataError):
                replace(BenchmarkConfig(), **changes)

    def test_invalid_thresholds_and_sessions_cannot_start(self) -> None:
        for changes in (
            {"min_price": Decimal("-1")}, {"min_bar_volume": Decimal("NaN")},
            {"max_spread_bps": Decimal(0)}, {"max_quote_age": timedelta(0)},
            {"verified_instrument_ids": ()},
        ):
            with self.subTest(changes=changes), self.assertRaises(DataError):
                replace(POLICY, **changes)
        with self.assertRaises(DataError):
            replace(SESSION, closes_at=SESSION.opens_at)
