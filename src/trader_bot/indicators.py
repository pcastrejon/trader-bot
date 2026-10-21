"""Decimal-only indicators with explicit SMA seeding and warm-up."""

from collections.abc import Sequence
from decimal import Decimal, localcontext

from trader_bot.contracts import MarketBar
from trader_bot.market_data import DataError


def ema(values: Sequence[Decimal], period: int) -> tuple[Decimal | None, ...]:
    """Seed with the first period's SMA; preceding outputs are unavailable."""
    _validate_period(period)
    if any(not value.is_finite() for value in values):
        raise DataError("DATA_INVALID", "EMA requires finite decimal observations.")
    with localcontext() as context:
        context.prec = 28
        output: list[Decimal | None] = [None] * len(values)
        if len(values) < period:
            return tuple(output)
        current = sum(values[:period], Decimal(0)) / Decimal(period)
        output[period - 1] = current
        alpha = Decimal(2) / Decimal(period + 1)
        for index in range(period, len(values)):
            current = (values[index] - current) * alpha + current
            output[index] = current
        return tuple(output)


def atr(bars: Sequence[MarketBar], period: int = 14) -> tuple[Decimal | None, ...]:
    """Wilder ATR: first TR is high-low, initial ATR is the mean of period TRs."""
    _validate_period(period)
    with localcontext() as context:
        context.prec = 28
        ranges: list[Decimal] = []
        for index, bar in enumerate(bars):
            if index == 0:
                ranges.append(bar.high - bar.low)
            else:
                previous = bars[index - 1].close
                ranges.append(max(bar.high - bar.low, abs(bar.high - previous),
                                  abs(bar.low - previous)))
        output: list[Decimal | None] = [None] * len(bars)
        if len(ranges) < period:
            return tuple(output)
        current = sum(ranges[:period], Decimal(0)) / Decimal(period)
        output[period - 1] = current
        for index in range(period, len(ranges)):
            current = (current * Decimal(period - 1) + ranges[index]) / Decimal(period)
            output[index] = current
        return tuple(output)


def _validate_period(period: int) -> None:
    if isinstance(period, bool) or not isinstance(period, int) or period <= 0:
        raise DataError("DATA_INVALID", "Indicator period must be a positive integer.")
