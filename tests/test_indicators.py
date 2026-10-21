import unittest
from dataclasses import replace
from decimal import Decimal, localcontext

from trader_bot.indicators import atr, ema
from trader_bot.market_data import DataError
from tests.support import bars_from_prices


class IndicatorTests(unittest.TestCase):
    def test_ema_sma_seed_and_recursive_values_exactly(self) -> None:
        result = ema(tuple(map(Decimal, ("1", "2", "3", "4", "5"))), 3)
        self.assertEqual(result, (None, None, Decimal("2"), Decimal("3"), Decimal("4")))

    def test_empty_and_insufficient_warmup_are_explicit(self) -> None:
        self.assertEqual(ema((), 9), ())
        self.assertEqual(ema((Decimal(1), Decimal(2)), 3), (None, None))
        self.assertEqual(atr(bars_from_prices((10, 11)), 3), (None, None))

    def test_wilder_atr_includes_gaps_and_sma_seed(self) -> None:
        # TR = 2, 5, 3, 2. Wilder(3): 10/3 then ((10/3)*2+2)/3.
        bars = bars_from_prices((10, 14, 12, 13))
        result = atr(bars, 3)
        with localcontext() as context:
            context.prec = 28
            seed = Decimal(10) / Decimal(3)
            expected = (seed * 2 + 2) / 3
        self.assertEqual(result, (None, None, seed, expected))

    def test_period_one(self) -> None:
        self.assertEqual(ema((Decimal(2), Decimal(4)), 1), (Decimal(2), Decimal(4)))
        self.assertEqual(atr(bars_from_prices((10, 12)), 1), (Decimal(2), Decimal(3)))

    def test_invalid_periods_and_non_finite_values(self) -> None:
        for period in (0, -1, True):
            with self.subTest(period=period), self.assertRaises(DataError):
                ema((Decimal(1),), period)
            with self.assertRaises(DataError):
                atr((), period)
        with self.assertRaises(DataError):
            ema((Decimal("NaN"),), 1)

    def test_decimal_context_does_not_change_results(self) -> None:
        prices = tuple(Decimal(i) / 10 for i in range(1, 12))
        expected = ema(prices, 3)
        with localcontext() as context:
            context.prec = 6
            self.assertEqual(ema(prices, 3), expected)

    def test_flat_market_atr_is_zero_not_fabricated(self) -> None:
        bar = bars_from_prices((10,))[0]
        flat = replace(bar, high=Decimal(10), low=Decimal(10))
        self.assertEqual(atr((flat,), 1), (Decimal(0),))
