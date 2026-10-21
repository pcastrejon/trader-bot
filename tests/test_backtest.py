import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, ROUND_DOWN, localcontext
from unittest.mock import patch

from trader_bot.backtest import (
    BacktestInput, BacktestRunner, FillSimulator, SimulationPolicy, canonical_json,
)
from trader_bot.contracts import Quote, RuntimeMode, TradeDirection
from trader_bot.market_data import DataError
from trader_bot.risk import OrderIntent
from tests.support import INSTRUMENT, NOW, POLICY, SESSION, bars_from_prices, snapshot


D = Decimal


def research_data(*, stop: bool = True, full_session: bool = False) -> BacktestInput:
    original = snapshot().bars
    extra = bars_from_prices((110, 100 if stop else 110))
    following = tuple(
        replace(bar, starts_at=NOW + timedelta(minutes=5 * i),
                ends_at=NOW + timedelta(minutes=5 * (i + 1)))
        for i, bar in enumerate(extra)
    )
    bars = original + following
    if full_session:
        count = int((SESSION.closes_at - bars[-1].ends_at).total_seconds() // 300)
        bars += tuple(
            replace(bars[-1], starts_at=bars[-1].ends_at + timedelta(minutes=5 * i),
                    ends_at=bars[-1].ends_at + timedelta(minutes=5 * (i + 1)))
            for i in range(count)
        )
    quotes = tuple(
        Quote(INSTRUMENT.instrument_id, bar.close, bar.close + D("0.02"),
              bar.ends_at, bar.ends_at, "local_csv")
        for bar in bars
    )
    return BacktestInput(
        INSTRUMENT, bars, quotes, (SESSION,), POLICY,
        dataset_label="synthetic-only-not-market-history",
    )


def intent(direction=TradeDirection.LONG) -> OrderIntent:
    return OrderIntent(
        "sim-test", INSTRUMENT.instrument_id, RuntimeMode.BACKTEST, direction,
        D(10), D(100), D(98) if direction is TradeDirection.LONG else D(102),
        NOW, NOW + timedelta(minutes=5), "stable-key", "test-1", D(20),
    )


class FillSimulatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.simulator = FillSimulator(SimulationPolicy(
            fee_per_share=D("0.01"), slippage_per_share=D("0.02"),
            volume_participation=D("0.01"),
        ))
        self.bar = replace(
            bars_from_prices((100,))[0], starts_at=NOW, ends_at=NOW + timedelta(minutes=5),
        )

    def test_causal_limit_fill_with_costs_and_partial_remainder(self) -> None:
        bar = replace(self.bar, open=D("99"), volume=D(250))
        fill = self.simulator.entry(intent(), bar, D(1), available_volume=D(250))
        assert fill is not None
        self.assertEqual(fill.at, NOW)
        self.assertEqual(fill.quantity, D(2))
        self.assertEqual(fill.price, D("99.02"))
        self.assertEqual(fill.fee, D("0.02"))

    def test_no_same_signal_bar_or_expired_fill(self) -> None:
        before = replace(self.bar, starts_at=NOW - timedelta(minutes=5), ends_at=NOW)
        expired = replace(self.bar, starts_at=NOW + timedelta(minutes=5),
                          ends_at=NOW + timedelta(minutes=10))
        self.assertIsNone(self.simulator.entry(intent(), before, D(1), available_volume=D(10000)))
        self.assertIsNone(self.simulator.entry(intent(), expired, D(1), available_volume=D(10000)))

    def test_limit_not_filled_at_worse_price_or_zero_volume(self) -> None:
        self.assertIsNone(self.simulator.entry(intent(), self.bar, D(1), available_volume=D(10000)))
        self.assertIsNone(self.simulator.entry(
            intent(), replace(self.bar, open=D(99)), D(1), available_volume=D(0),
        ))

    def test_long_gap_stop_exceeds_planned_risk(self) -> None:
        fill = self.simulator.exit(intent(), D(2), replace(
            self.bar, open=D(90), high=D(91), low=D(89), close=D(90),
        ))
        assert fill is not None
        self.assertEqual(fill.price, D("89.98"))
        self.assertEqual(fill.at, NOW)

    def test_intrabar_stop_uses_stop_and_bar_end_timestamp(self) -> None:
        fill = self.simulator.exit(intent(), D(2), replace(self.bar, low=D(97)))
        assert fill is not None
        self.assertEqual(fill.price, D("97.98"))
        self.assertEqual(fill.at, self.bar.ends_at)

    def test_short_fill_and_gap_stop_are_adverse(self) -> None:
        short = intent(TradeDirection.SHORT)
        entry = self.simulator.entry(
            short, replace(self.bar, open=D(101)), D(1), available_volume=D(10000),
        )
        assert entry is not None
        self.assertEqual(entry.price, D("100.98"))
        exit_fill = self.simulator.exit(short, D(2), replace(
            self.bar, open=D(110), high=D(111), low=D(109), close=D(110),
        ))
        assert exit_fill is not None
        self.assertEqual(exit_fill.price, D("110.02"))

    def test_session_flatten_is_at_available_open(self) -> None:
        fill = self.simulator.exit(intent(), D(3), self.bar, flatten=True)
        assert fill is not None
        self.assertEqual(fill.price, D("99.98"))
        self.assertEqual(fill.reason, "session_exit")

    def test_gap_through_entry_stop_does_not_open_unprotected_trade(self) -> None:
        self.assertIsNone(self.simulator.entry(
            intent(), replace(self.bar, open=D(97), high=D(99), low=D(96), close=D(98)),
            D(1), available_volume=D(10000),
        ))


class BacktestRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = BacktestRunner()
        self.data = research_data()

    def test_fixture_trade_equity_and_fees_are_exact(self) -> None:
        data = replace(
            self.data, simulation=SimulationPolicy(D("0.01"), D("0.02")),
            risk=replace(self.data.risk, round_trip_cost_per_share=D("0.06")),
        )
        report = self.runner.run(data)
        fills = [event for event in report.events if event.kind == "fill"]
        self.assertEqual(len(fills), 2)
        self.assertEqual(fills[0].at, NOW)
        self.assertEqual(fills[0].price, D("110.02"))
        self.assertEqual(fills[0].quantity, D(4))
        self.assertEqual(fills[1].price, D("99.98"))
        self.assertEqual(report.total_fees, D("0.08"))
        self.assertEqual(report.realized_pnl, D("-40.24"))
        self.assertEqual(report.final_equity, D("9959.76"))
        self.assertTrue(report.complete)
        self.assertEqual(report.open_quantity, 0)

    def test_repeatability_and_decimal_context_isolation(self) -> None:
        first = self.runner.run(self.data)
        with localcontext() as context:
            context.prec = 6
            context.rounding = ROUND_DOWN
            second = self.runner.run(self.data)
        self.assertEqual(first, second)
        self.assertEqual(canonical_json(first), canonical_json(second))

    def test_future_prices_never_change_earlier_signals_or_entry(self) -> None:
        changed = replace(self.data.bars[-1], open=D(80), high=D(81), low=D(79), close=D(80))
        alternative = replace(self.data, bars=self.data.bars[:-1] + (changed,))
        first = self.runner.run(self.data)
        second = self.runner.run(alternative)
        self.assertEqual(
            [e for e in first.events if e.at <= NOW],
            [e for e in second.events if e.at <= NOW],
        )
        self.assertNotEqual(first.final_equity, second.final_equity)

    def test_no_archived_quotes_means_no_trade_not_fabricated_quotes(self) -> None:
        report = self.runner.run(replace(self.data, quotes=()))
        self.assertFalse(any(event.kind == "fill" for event in report.events))
        self.assertEqual(report.final_equity, report.initial_equity)
        self.assertTrue(any(event.code == "DATA_MISSING" for event in report.events))
        self.assertFalse(report.complete)

    def test_future_received_quote_cannot_leak_backwards(self) -> None:
        delayed = replace(
            self.data.quotes[53], received_at=self.data.quotes[53].received_at + timedelta(seconds=1),
        )
        data = replace(self.data, quotes=(delayed,))
        report = self.runner.run(data)
        self.assertFalse(any(event.kind == "fill" for event in report.events))

    def test_end_of_data_does_not_invent_entry_or_exit(self) -> None:
        data = replace(self.data, bars=self.data.bars[:54], quotes=self.data.quotes[:54])
        report = self.runner.run(data)
        self.assertFalse(any(event.kind == "fill" for event in report.events))
        self.assertTrue(any(event.code == "END_OF_DATA" for event in report.events))
        self.assertFalse(report.complete)
        open_data = research_data(stop=False)
        report = self.runner.run(open_data)
        self.assertFalse(report.complete)
        self.assertEqual(report.open_quantity, D(4))
        self.assertTrue(any(event.code == "OPEN_POSITION" for event in report.events))

    def test_final_session_window_flattens_without_overnight(self) -> None:
        data = research_data(stop=False, full_session=True)
        report = self.runner.run(data)
        exit_events = [event for event in report.events if event.code == "SESSION_EXIT"]
        self.assertEqual(len(exit_events), 1)
        self.assertEqual(exit_events[0].at, SESSION.closes_at - timedelta(minutes=5))
        self.assertTrue(report.complete)

    def test_partial_fill_remainder_canceled_and_only_fill_is_exited(self) -> None:
        data = replace(self.data, simulation=SimulationPolicy(volume_participation=D("0.0001")))
        report = self.runner.run(data)
        fills = [event for event in report.events if event.kind == "fill"]
        self.assertEqual([event.quantity for event in fills], [D(1), D(1)])
        self.assertTrue(any(event.code == "PARTIAL_REMAINDER" for event in report.events))

    def test_recheck_blocks_gap_quote_changed_at_entry_time(self) -> None:
        # A later receipt just before entry cannot be used at signal creation.
        quotes = tuple(
            q for q in self.data.quotes if q.received_at < NOW
        ) + (replace(self.data.quotes[53], bid=D(150), ask=D("150.02")),)
        report = self.runner.run(replace(self.data, quotes=quotes))
        self.assertFalse(any(event.kind == "fill" for event in report.events))

    def test_drawdown_latch_is_recorded_and_no_entry_after_breach(self) -> None:
        changed = replace(self.data.bars[-1], open=D(2), high=D(3), low=D(1), close=D(2))
        data = replace(self.data, bars=self.data.bars[:-1] + (changed,))
        report = self.runner.run(data)
        self.assertTrue(report.circuit_states[-1].tripped)
        self.assertLess(report.final_equity, D(9900))

    def test_missing_bars_invalid_identity_currency_and_cost_assumptions_rejected(self) -> None:
        for change in (
            {"bars": ()}, {"bars": self.data.bars[:20] + self.data.bars[21:]},
            {"instrument": replace(INSTRUMENT, currency="MXN")},
            {"simulation": SimulationPolicy(fee_per_share=D("0.01"))},
            {"quotes": (replace(self.data.quotes[0], instrument_id="OTHER"),)},
        ):
            with self.subTest(change=change), self.assertRaises(DataError):
                replace(self.data, **change)

    def test_no_network_or_model_calls(self) -> None:
        with patch("urllib.request.OpenerDirector.open") as network:
            self.runner.run(self.data)
        network.assert_not_called()

    def test_input_parameters_change_fingerprint(self) -> None:
        first = self.runner.run(self.data)
        second = self.runner.run(replace(self.data, initial_equity=D(20000)))
        self.assertNotEqual(first.input_fingerprint, second.input_fingerprint)

    def test_future_bar_volume_cannot_change_open_fill_quantity(self) -> None:
        altered_bar = replace(self.data.bars[54], volume=D(0))
        altered = replace(self.data, bars=self.data.bars[:54] + (altered_bar,) + self.data.bars[55:])
        first = self.runner.run(self.data)
        second = self.runner.run(altered)
        first_entry = next(event for event in first.events if event.code == "ENTRY")
        second_entry = next(event for event in second.events if event.code == "ENTRY")
        self.assertEqual(first_entry, second_entry)

    def test_entry_bar_protective_exit_is_not_delayed_to_next_bar(self) -> None:
        altered_bar = replace(self.data.bars[54], low=D(100))
        altered = replace(self.data, bars=self.data.bars[:54] + (altered_bar,) + self.data.bars[55:])
        report = self.runner.run(altered)
        fills = [event for event in report.events if event.kind == "fill"]
        self.assertEqual(len(fills), 2)
        self.assertEqual(fills[1].code, "PROTECTIVE_STOP")
        self.assertEqual(fills[1].at, altered_bar.ends_at)

    def test_breaker_resets_only_on_next_explicit_session_and_realized_equity(self) -> None:
        data = research_data(stop=True, full_session=True)
        next_session = replace(
            SESSION, opens_at=SESSION.opens_at + timedelta(days=3),
            closes_at=SESSION.closes_at + timedelta(days=3),
        )
        next_bar = replace(
            data.bars[-1], starts_at=next_session.opens_at,
            ends_at=next_session.opens_at + timedelta(minutes=5),
        )
        next_quote = replace(
            data.quotes[-1], observed_at=next_bar.ends_at, received_at=next_bar.ends_at,
        )
        report = self.runner.run(replace(
            data, bars=data.bars + (next_bar,), quotes=data.quotes + (next_quote,),
            sessions=(SESSION, next_session),
        ))
        self.assertEqual(len(report.circuit_states), 2)
        self.assertEqual(report.circuit_states[1].start_equity, D(9960))
        self.assertEqual(report.circuit_states[1].session_open, next_session.opens_at)

    def test_early_close_flatten_uses_supplied_calendar(self) -> None:
        full_data = research_data(stop=False, full_session=True)
        early_close = NOW + timedelta(minutes=45)
        data = replace(
            full_data,
            bars=tuple(bar for bar in full_data.bars if bar.ends_at <= early_close),
            quotes=tuple(q for q in full_data.quotes if q.received_at <= early_close),
            sessions=(replace(SESSION, closes_at=early_close),),
        )
        report = self.runner.run(data)
        exits = [event for event in report.events if event.code == "SESSION_EXIT"]
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0].at, early_close - timedelta(minutes=5))
        self.assertTrue(report.complete)

    def test_early_close_entry_cutoff_is_enforced(self) -> None:
        data = research_data(stop=False)
        session = replace(SESSION, closes_at=NOW + timedelta(minutes=10))
        # This puts the crossover inside the entry cutoff; it must not open a trade.
        report = self.runner.run(replace(data, sessions=(session,)))
        self.assertFalse(any(event.kind == "fill" for event in report.events))
        self.assertTrue(any(event.code == "SESSION_CUTOFF" for event in report.events))
