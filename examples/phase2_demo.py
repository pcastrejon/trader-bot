"""Create a synthetic offline input bundle; no market-data or broker connection."""

import argparse
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trader_bot.backtest import BacktestInput, SimulationPolicy
from trader_bot.contracts import AssetType, Instrument, MarketBar, Quote
from trader_bot.market_data import DataError
from trader_bot.replay import write_bundle
from trader_bot.risk import RiskPolicy
from trader_bot.strategy import EligibilityPolicy, ExchangeSession


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    start = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
    instrument = Instrument(
        "US:DEMO", "DEMO", AssetType.STOCK, "NASDAQ", "USD",
        ("backtest",), "XNAS",
    )
    prices = (100,) * 50 + (99, 98, 97, 110, 110, 100)
    bars = tuple(
        MarketBar(
            instrument.instrument_id, 300, start + timedelta(minutes=5 * index),
            start + timedelta(minutes=5 * (index + 1)),
            Decimal(price), Decimal(price + 1), Decimal(price - 1),
            Decimal(price), Decimal(10000), "synthetic", "unadjusted",
        )
        for index, price in enumerate(prices)
    )
    quotes = tuple(
        Quote(instrument.instrument_id, bar.close, bar.close + Decimal("0.02"),
              bar.ends_at, bar.ends_at, "synthetic")
        for bar in bars
    )
    session = ExchangeSession(
        "XNAS", start, start.replace(hour=21, minute=0), "synthetic-demonstration-only",
    )
    eligibility = EligibilityPolicy(
        ("US:DEMO",), Decimal(5), Decimal(1000), Decimal(20),
        timedelta(seconds=30), timedelta(seconds=60),
    )
    data = BacktestInput(
        instrument, bars, quotes, (session,), eligibility,
        risk=RiskPolicy(round_trip_cost_per_share=Decimal("0.06")),
        simulation=SimulationPolicy(Decimal("0.01"), Decimal("0.02")),
        dataset_label="Synthetic software fixture, not historical market data",
    )
    try:
        write_bundle(args.output, data)
    except DataError as exc:
        parser.exit(2, f"Cannot create demo input: {exc}\n")
    print(f"Synthetic input written to {args.output}; DEMO is not a verified tradable instrument.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
