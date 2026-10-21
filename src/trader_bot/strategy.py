"""Fail-closed eligibility and the experimental five-minute EMA/ATR benchmark."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum
from zoneinfo import ZoneInfo

from trader_bot.config import RuntimeConfig
from trader_bot.contracts import (
    AssetType, CandidateSignal, Instrument, MarketBar, RuntimeMode, TradeDirection,
)
from trader_bot.indicators import atr, ema
from trader_bot.market_data import DataError, MarketSnapshot, utc_time


@dataclass(frozen=True, slots=True)
class ExchangeSession:
    """An explicitly supplied session; never infer holidays or early closes."""

    calendar: str
    opens_at: datetime
    closes_at: datetime
    source: str
    timezone: str = "America/New_York"

    def __post_init__(self) -> None:
        if utc_time(self.closes_at) <= utc_time(self.opens_at):
            raise DataError("DATA_INVALID", "Session close must follow open.")
        if not self.source.strip() or not self.calendar.strip():
            raise DataError("DATA_INVALID", "Session calendar and provenance are required.")
        ZoneInfo(self.timezone)


@dataclass(frozen=True, slots=True)
class EligibilityPolicy:
    verified_instrument_ids: tuple[str, ...]
    min_price: Decimal
    min_bar_volume: Decimal
    max_spread_bps: Decimal
    max_quote_age: timedelta
    max_bar_age: timedelta

    def __post_init__(self) -> None:
        if not self.verified_instrument_ids:
            raise DataError("DATA_INVALID", "A verified instrument allowlist is required.")
        for amount in (self.min_price, self.min_bar_volume, self.max_spread_bps):
            if not isinstance(amount, Decimal) or not amount.is_finite() or amount <= 0:
                raise DataError("DATA_INVALID", "Liquidity thresholds must be positive decimals.")
        if self.max_quote_age <= timedelta(0) or self.max_bar_age <= timedelta(0):
            raise DataError("DATA_INVALID", "Freshness limits must be positive.")


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    fast_period: int = 9
    slow_period: int = 21
    trend_period: int = 50
    atr_period: int = 14
    atr_multiplier: Decimal = Decimal("2")
    entry_cutoff: timedelta = timedelta(minutes=30)

    def __post_init__(self) -> None:
        periods = (self.fast_period, self.slow_period, self.trend_period, self.atr_period)
        if any(isinstance(p, bool) or not isinstance(p, int) or p <= 0 for p in periods):
            raise DataError("DATA_INVALID", "Strategy periods must be positive integers.")
        if not self.fast_period < self.slow_period <= self.trend_period:
            raise DataError("DATA_INVALID", "Expected fast < slow <= trend period.")
        if (
            not isinstance(self.atr_multiplier, Decimal)
            or not self.atr_multiplier.is_finite() or self.atr_multiplier <= 0
        ):
            raise DataError("DATA_INVALID", "ATR multiplier must be a positive decimal.")
        if self.entry_cutoff < timedelta(minutes=30):
            raise DataError("DATA_INVALID", "Entry cutoff must be at least thirty minutes.")

    @property
    def version(self) -> str:
        return (
            f"1:ema-{self.fast_period}-{self.slow_period}-{self.trend_period}"
            f":atr-{self.atr_period}x{self.atr_multiplier}"
            f":cutoff-{self.entry_cutoff.total_seconds():g}s"
        )


class EvaluationStatus(StrEnum):
    SIGNAL = "signal"
    NO_SIGNAL = "no_signal"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class Evaluation:
    instrument_id: str
    status: EvaluationStatus
    code: str
    explanation: str
    candidate: CandidateSignal | None = None


class BenchmarkStrategy:
    """Emits research candidates, never risk approvals, quantities, or orders."""

    def __init__(self, config: BenchmarkConfig | None = None) -> None:
        self.config = config if config is not None else BenchmarkConfig()

    def evaluate(
        self, instrument: Instrument, snapshot: MarketSnapshot,
        sessions: tuple[ExchangeSession, ...], runtime: RuntimeConfig,
        policy: EligibilityPolicy, as_of: datetime, correlation_id: str,
    ) -> Evaluation:
        try:
            self._validate(instrument, snapshot, sessions, runtime, policy, as_of)
            bars = snapshot.bars
            minimum = max(self.config.trend_period, self.config.slow_period + 1,
                          self.config.atr_period)
            if len(bars) < minimum:
                raise DataError("INSUFFICIENT_HISTORY", f"Need at least {minimum} completed bars.")
            prices = tuple(bar.close for bar in bars)
            fast = ema(prices, self.config.fast_period)
            slow = ema(prices, self.config.slow_period)
            trend = ema(prices, self.config.trend_period)
            ranges = atr(bars, self.config.atr_period)
            previous_fast, previous_slow = fast[-2], slow[-2]
            current_fast, current_slow = fast[-1], slow[-1]
            current_trend, current_atr = trend[-1], ranges[-1]
            if (
                previous_fast is None or previous_slow is None
                or current_fast is None or current_slow is None
                or current_trend is None or current_atr is None
            ):
                raise DataError("INSUFFICIENT_HISTORY", "Indicator warm-up incomplete.")
            direction = None
            if (
                previous_fast <= previous_slow and current_fast > current_slow
                and prices[-1] > current_trend
            ):
                direction = TradeDirection.LONG
            elif (
                previous_fast >= previous_slow and current_fast < current_slow
                and prices[-1] < current_trend
            ):
                if runtime.mode is not RuntimeMode.PAPER or not runtime.paper_allow_shorting:
                    raise DataError("MODE_FORBIDDEN", "Short candidates require paper shorting enabled.")
                direction = TradeDirection.SHORT
            if direction is None:
                return Evaluation(instrument.instrument_id, EvaluationStatus.NO_SIGNAL,
                                  "NO_CROSSOVER", "No qualifying EMA crossover on the last bar.")
            with localcontext() as context:
                context.prec = 28
                distance = self.config.atr_multiplier * current_atr
                if distance <= 0:
                    raise DataError("DATA_INVALID", "ATR stop distance must be positive.")
                stop = prices[-1] - distance if direction is TradeDirection.LONG else prices[-1] + distance
            if stop <= 0:
                raise DataError("DATA_INVALID", "Protective stop must be a positive price.")
            last = bars[-1]
            candidate = CandidateSignal(
                correlation_id=correlation_id, instrument_id=instrument.instrument_id,
                strategy_id="experimental-ema-atr", strategy_version=self.config.version,
                direction=direction, signal_bar_ends_at=last.ends_at,
                rule_evidence=(
                    f"source={snapshot.source}; adjustment={last.adjustment_basis}",
                    f"EMA_fast={current_fast}; EMA_slow={current_slow}; EMA_trend={current_trend}",
                    f"ATR={current_atr}; multiplier={self.config.atr_multiplier}",
                    f"close={last.close}; quote_at={snapshot.quote.observed_at.isoformat()}",
                    "Research candidate only; not risk-approved or eligible for execution.",
                ),
                protective_stop_price=stop,
                expires_at=last.ends_at + timedelta(minutes=5),
            )
            return Evaluation(instrument.instrument_id, EvaluationStatus.SIGNAL,
                              "CROSSOVER", "Experimental signal; risk checks not applied by this strategy.",
                              candidate)
        except DataError as exc:
            return Evaluation(instrument.instrument_id, EvaluationStatus.REJECTED,
                              exc.code, str(exc))

    def _validate(
        self, instrument: Instrument, snapshot: MarketSnapshot,
        sessions: tuple[ExchangeSession, ...], runtime: RuntimeConfig,
        policy: EligibilityPolicy, as_of: datetime,
    ) -> None:
        now = utc_time(as_of)
        validate_instrument(instrument, runtime, policy)
        if snapshot.quality_flags:
            raise DataError("DATA_INVALID", "Provider reported data-quality warnings.")
        if not sessions or any(
            item.calendar != instrument.market_calendar for item in sessions
        ):
            raise DataError("CALENDAR_UNKNOWN", "Verified sessions for this calendar are required.")
        if any(
            following.opens_at <= previous.closes_at
            for previous, following in zip(sessions, sessions[1:])
        ):
            raise DataError("CALENDAR_UNKNOWN", "Sessions must be sorted and non-overlapping.")
        current_session = next(
            (item for item in sessions if item.opens_at <= now < item.closes_at), None
        )
        if current_session is None:
            raise DataError("MARKET_CLOSED", "No supplied exchange session covers evaluation time.")
        if now >= current_session.closes_at - self.config.entry_cutoff:
            raise DataError("SESSION_CUTOFF", "No new entries during the final thirty minutes.")
        if snapshot.received_at > now:
            raise DataError("DATA_FUTURE", "Snapshot was received after evaluation time.")
        if not snapshot.bars:
            raise DataError("DATA_MISSING", "No completed market bars.")
        quote = snapshot.quote
        if quote.instrument_id != instrument.instrument_id:
            raise DataError("DATA_INVALID", "Quote instrument mismatch.")
        if quote.source != snapshot.source:
            raise DataError("DATA_INVALID", "Quote and snapshot source mismatch.")
        if (
            quote.observed_at > quote.received_at or quote.received_at > now
            or quote.received_at > snapshot.received_at
        ):
            raise DataError("DATA_FUTURE", "Quote timestamps are inconsistent with evaluation time.")
        if now - quote.observed_at > policy.max_quote_age:
            raise DataError("DATA_STALE", "Quote exceeds configured freshness threshold.")
        validate_bars(snapshot.bars, instrument, sessions, now, snapshot.source)
        last = snapshot.bars[-1]
        if last.ends_at > snapshot.received_at:
            raise DataError("DATA_FUTURE", "Snapshot predates its completed bars.")
        if now - last.ends_at > policy.max_bar_age or now >= last.ends_at + timedelta(minutes=5):
            raise DataError("DATA_STALE", "Last completed bar is stale or signal has expired.")
        if min(last.close, quote.bid) < policy.min_price:
            raise DataError("LIQUIDITY_FILTER", "Price is below configured minimum.")
        if last.volume < policy.min_bar_volume:
            raise DataError("LIQUIDITY_FILTER", "Last-bar volume is below configured minimum.")
        with localcontext() as context:
            context.prec = 28
            spread = (quote.ask - quote.bid) / ((quote.ask + quote.bid) / Decimal(2)) * Decimal(10000)
        if spread > policy.max_spread_bps:
            raise DataError("LIQUIDITY_FILTER", "Quote spread exceeds configured maximum.")


def validate_instrument(
    instrument: Instrument, runtime: RuntimeConfig, policy: EligibilityPolicy,
) -> None:
    if runtime.mode is RuntimeMode.LIVE:
        raise DataError("MODE_FORBIDDEN", "Live mode is unavailable.")
    if instrument.symbol not in runtime.watchlist:
        raise DataError("NOT_WATCHLISTED", "Instrument is not in the configured watchlist.")
    if (
        instrument.instrument_id not in policy.verified_instrument_ids
        or runtime.mode.value not in instrument.supported_modes
        or instrument.asset_type not in {AssetType.STOCK, AssetType.ETF}
        or instrument.currency != "USD"
        or instrument.venue not in {"NYSE", "NASDAQ", "ARCA", "AMEX"}
        or instrument.market_calendar not in {"XNYS", "XNAS"}
    ):
        raise DataError("INSTRUMENT_UNSUPPORTED", "Instrument/market support is not verified.")


def validate_bars(
    bars: tuple[MarketBar, ...], instrument: Instrument,
    sessions: tuple[ExchangeSession, ...], as_of: datetime, source: str,
) -> None:
    previous: MarketBar | None = None
    previous_session: int | None = None
    basis = bars[0].adjustment_basis
    for bar in bars:
        if (
            bar.instrument_id != instrument.instrument_id or bar.source != source
            or bar.adjustment_basis != basis or basis != "unadjusted"
            or bar.interval_seconds != 300
            or bar.ends_at - bar.starts_at != timedelta(minutes=5)
        ):
            raise DataError("DATA_INVALID", "Inconsistent bar identity/source/interval/adjustment.")
        if bar.ends_at > as_of:
            raise DataError("DATA_FUTURE", "Strategy cannot consume a forming/future bar.")
        index = next((i for i, session in enumerate(sessions)
                      if session.opens_at <= bar.starts_at and bar.ends_at <= session.closes_at), None)
        if index is None:
            raise DataError("CALENDAR_UNKNOWN", "A bar falls outside supplied regular sessions.")
        if (bar.starts_at - sessions[index].opens_at).total_seconds() % 300:
            raise DataError("DATA_INVALID", "Bar is not aligned to the session's five-minute grid.")
        if previous is not None and bar.starts_at != previous.ends_at:
            if (
                previous_session is None or index != previous_session + 1
                or previous.ends_at != sessions[previous_session].closes_at
                or bar.starts_at != sessions[index].opens_at
            ):
                raise DataError("DATA_MISSING", "Bars are duplicate, out-of-order, or have a gap.")
        previous, previous_session = bar, index
