"""Read-only market data boundaries, normalization, caching, and usage limits."""

from __future__ import annotations

import csv
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from trader_bot.contracts import ContractError, Instrument, MarketBar, Quote


class DataError(ValueError):
    """An explicit, machine-readable market-data failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def utc_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DataError("DATA_INVALID", "Timestamps must include a timezone.")
    return value.astimezone(UTC)


def parse_time(value: str) -> datetime:
    try:
        return utc_time(datetime.fromisoformat(value))
    except ValueError as exc:
        raise DataError("DATA_INVALID", "Invalid timezone-aware timestamp.") from exc


def parse_decimal(value: str) -> Decimal:
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise DataError("DATA_INVALID", "Invalid decimal market value.") from exc
    if not result.is_finite():
        raise DataError("DATA_INVALID", "Market values must be finite.")
    return result


@dataclass(frozen=True, slots=True)
class DataRequest:
    instrument: Instrument
    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        if utc_time(self.ends_at) <= utc_time(self.starts_at):
            raise DataError("DATA_INVALID", "Data request end must follow start.")


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    bars: tuple[MarketBar, ...]
    quote: Quote
    received_at: datetime
    source: str
    quality_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        utc_time(self.received_at)
        if not self.source.strip():
            raise DataError("DATA_INVALID", "Snapshot source must be non-empty.")


class MarketDataProvider(Protocol):
    """No orders, credentials, AI calls, or risk decisions cross this interface."""

    def fetch(self, request: DataRequest, as_of: datetime) -> MarketSnapshot: ...


class CsvMarketDataProvider:
    """Local, source-tagged five-minute OHLCV with an explicit quote observation."""

    def __init__(self, path: Path, quote: Quote, source: str = "local_csv") -> None:
        self.path = path
        self.quote = quote
        self.source = source

    def fetch(self, request: DataRequest, as_of: datetime) -> MarketSnapshot:
        bars: list[MarketBar] = []
        required = {
            "instrument_id", "starts_at", "open", "high", "low", "close", "volume"
        }
        try:
            with self.path.open(newline="", encoding="utf-8") as stream:
                reader = csv.DictReader(stream)
                if (
                    reader.fieldnames is None or set(reader.fieldnames) != required
                    or len(reader.fieldnames) != len(required)
                ):
                    raise DataError("DATA_INVALID", "CSV columns do not match OHLCV schema.")
                for row in reader:
                    if None in row or any(value is None for value in row.values()):
                        raise DataError("DATA_INVALID", "CSV row has missing/extra values.")
                    start = parse_time(row["starts_at"])
                    bar = MarketBar(
                        instrument_id=row["instrument_id"],
                        interval_seconds=300,
                        starts_at=start,
                        ends_at=start + timedelta(minutes=5),
                        open=parse_decimal(row["open"]),
                        high=parse_decimal(row["high"]),
                        low=parse_decimal(row["low"]),
                        close=parse_decimal(row["close"]),
                        volume=parse_decimal(row["volume"]),
                        source=self.source,
                        adjustment_basis="unadjusted",
                    )
                    if bar.instrument_id != request.instrument.instrument_id:
                        raise DataError("DATA_INVALID", "CSV contains another instrument.")
                    if request.starts_at <= start and bar.ends_at <= request.ends_at:
                        bars.append(bar)
        except (OSError, csv.Error, UnicodeError) as exc:
            raise DataError("PROVIDER_UNAVAILABLE", "Cannot read OHLCV CSV file.") from exc
        except ContractError as exc:
            raise DataError("DATA_INVALID", f"Invalid CSV market bar: {exc}") from exc
        return MarketSnapshot(tuple(bars), self.quote, utc_time(as_of), self.source)


@dataclass(frozen=True, slots=True)
class UsagePolicy:
    requests_per_day: int
    monthly_cost_cap: Decimal = Decimal("0")
    cost_per_fetch: Decimal = Decimal("0")
    cache_ttl: timedelta = timedelta(seconds=30)
    cache_size: int = 128

    def __post_init__(self) -> None:
        if self.requests_per_day <= 0 or self.cache_size <= 0:
            raise DataError("DATA_INVALID", "Quota and cache size must be positive.")
        for amount in (self.monthly_cost_cap, self.cost_per_fetch):
            if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
                raise DataError("DATA_INVALID", "Costs must be finite non-negative decimals.")
        if self.cache_ttl < timedelta(0):
            raise DataError("DATA_INVALID", "Cache TTL must not be negative.")


class MarketDataClient:
    """A bounded cache and conservative per-fetch budget, including failed fetches.

    Usage is in-memory for this offline/research phase, not a persistent billing
    ledger. Concrete HTTP adapters additionally bound the requests in each fetch.
    """

    def __init__(self, provider: MarketDataProvider, policy: UsagePolicy) -> None:
        self.provider = provider
        self.policy = policy
        self.requests: dict[str, int] = {}
        self.costs: dict[str, Decimal] = {}
        self.cache_hits = 0
        self._cache: OrderedDict[
            DataRequest, tuple[datetime, MarketSnapshot]
        ] = OrderedDict()

    def fetch(self, request: DataRequest, as_of: datetime) -> MarketSnapshot:
        now = utc_time(as_of)
        if request.ends_at > now:
            raise DataError("DATA_FUTURE", "Requested data cannot extend into the future.")
        cached = self._cache.get(request)
        if cached is not None:
            fetched_at, snapshot = cached
            if timedelta(0) <= now - fetched_at < self.policy.cache_ttl:
                self._cache.move_to_end(request)
                self.cache_hits += 1
                return snapshot
            del self._cache[request]
        day = now.date().isoformat()
        month = now.strftime("%Y-%m")
        count = self.requests.get(day, 0)
        spent = self.costs.get(month, Decimal("0"))
        if count >= self.policy.requests_per_day:
            raise DataError("PROVIDER_QUOTA", "Daily provider fetch quota exhausted.")
        if spent + self.policy.cost_per_fetch > self.policy.monthly_cost_cap:
            raise DataError("COST_CAP", "Provider fetch would exceed monthly cost cap.")
        self.requests[day] = count + 1
        self.costs[month] = spent + self.policy.cost_per_fetch
        snapshot = self.provider.fetch(request, now)
        self._cache[request] = (now, snapshot)
        if len(self._cache) > self.policy.cache_size:
            self._cache.popitem(last=False)
        return snapshot
