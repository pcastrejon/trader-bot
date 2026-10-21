from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trader_bot.config import RuntimeConfig
from trader_bot.contracts import AssetType, Instrument, MarketBar, Quote
from trader_bot.market_data import CsvMarketDataProvider, DataRequest, MarketSnapshot
from trader_bot.strategy import EligibilityPolicy, ExchangeSession


OPEN = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
NOW = datetime(2026, 1, 2, 19, 0, tzinfo=UTC)
INSTRUMENT = Instrument(
    "US:DEMO", "DEMO", AssetType.STOCK, "NASDAQ", "USD",
    ("backtest", "paper"), "XNAS",
)
SESSION = ExchangeSession("XNAS", OPEN, OPEN.replace(hour=21, minute=0), "test-fixture")
POLICY = EligibilityPolicy(
    ("US:DEMO",), Decimal("5"), Decimal("1000"), Decimal("20"),
    timedelta(seconds=30), timedelta(seconds=60),
)
RUNTIME = RuntimeConfig(watchlist=("DEMO",))
FIXTURE = Path(__file__).parent / "fixtures" / "benchmark_long.csv"


def quote_at(now: datetime = NOW) -> Quote:
    return Quote("US:DEMO", Decimal("110"), Decimal("110.02"), now, now, "local_csv")


def snapshot() -> MarketSnapshot:
    return CsvMarketDataProvider(FIXTURE, quote_at()).fetch(
        DataRequest(INSTRUMENT, OPEN, NOW), NOW
    )


def bars_from_prices(prices: tuple[int, ...]) -> tuple[MarketBar, ...]:
    return tuple(
        MarketBar(
            "US:DEMO", 300, OPEN + timedelta(minutes=5 * index),
            OPEN + timedelta(minutes=5 * (index + 1)),
            Decimal(price), Decimal(price + 1), Decimal(price - 1),
            Decimal(price), Decimal(10000), "local_csv", "unadjusted",
        )
        for index, price in enumerate(prices)
    )
