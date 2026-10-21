"""Strict, local JSON replay bundles containing inputs and reproducible results."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

from trader_bot.backtest import BacktestInput, BacktestReport, BacktestRunner, SimulationPolicy, json_value
from trader_bot.contracts import AssetType, Instrument, MarketBar, Quote
from trader_bot.market_data import DataError, parse_decimal, parse_time
from trader_bot.risk import RiskPolicy
from trader_bot.strategy import BenchmarkConfig, EligibilityPolicy, ExchangeSession


def _record(value: object, keys: tuple[str, ...]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise DataError("REPLAY_INVALID", "Replay object has missing or unsupported fields.")
    return {str(key): item for key, item in value.items()}


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DataError("REPLAY_INVALID", "Expected a nonempty replay string.")
    return value


def _sequence(value: object) -> tuple[object, ...]:
    if not isinstance(value, list):
        raise DataError("REPLAY_INVALID", "Expected a replay list.")
    return tuple(value)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DataError("REPLAY_INVALID", "Expected a replay integer.")
    return value


def _duration(value: object) -> timedelta:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataError("REPLAY_INVALID", "Expected duration in seconds.")
    return timedelta(seconds=value)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DataError("REPLAY_INVALID", "Duplicate JSON object field.")
        result[key] = value
    return result


def load_input(value: object) -> BacktestInput:
    data = _record(value, (
        "instrument", "bars", "quotes", "sessions", "eligibility", "benchmark",
        "risk", "simulation", "initial_equity", "dataset_label",
    ))
    item = _record(data["instrument"], (
        "instrument_id", "symbol", "asset_type", "venue", "currency",
        "supported_modes", "market_calendar",
    ))
    instrument = Instrument(
        _text(item["instrument_id"]), _text(item["symbol"]),
        AssetType(_text(item["asset_type"])), _text(item["venue"]),
        _text(item["currency"]), tuple(_text(x) for x in _sequence(item["supported_modes"])),
        _text(item["market_calendar"]),
    )
    bars: list[MarketBar] = []
    for raw in _sequence(data["bars"]):
        item = _record(raw, (
            "instrument_id", "interval_seconds", "starts_at", "ends_at",
            "open", "high", "low", "close", "volume", "source", "adjustment_basis",
        ))
        bars.append(MarketBar(
            _text(item["instrument_id"]), _integer(item["interval_seconds"]),
            parse_time(_text(item["starts_at"])), parse_time(_text(item["ends_at"])),
            *(parse_decimal(_text(item[name])) for name in ("open", "high", "low", "close", "volume")),
            _text(item["source"]), _text(item["adjustment_basis"]),
        ))
    quotes: list[Quote] = []
    for raw in _sequence(data["quotes"]):
        item = _record(raw, (
            "instrument_id", "bid", "ask", "observed_at", "received_at", "source",
            "bid_size", "ask_size",
        ))
        quotes.append(Quote(
            _text(item["instrument_id"]), parse_decimal(_text(item["bid"])),
            parse_decimal(_text(item["ask"])), parse_time(_text(item["observed_at"])),
            parse_time(_text(item["received_at"])), _text(item["source"]),
            parse_decimal(_text(item["bid_size"])) if item["bid_size"] is not None else None,
            parse_decimal(_text(item["ask_size"])) if item["ask_size"] is not None else None,
        ))
    sessions: list[ExchangeSession] = []
    for raw in _sequence(data["sessions"]):
        item = _record(raw, ("calendar", "opens_at", "closes_at", "source", "timezone"))
        sessions.append(ExchangeSession(
            _text(item["calendar"]), parse_time(_text(item["opens_at"])),
            parse_time(_text(item["closes_at"])), _text(item["source"]), _text(item["timezone"]),
        ))
    item = _record(data["eligibility"], (
        "verified_instrument_ids", "min_price", "min_bar_volume", "max_spread_bps",
        "max_quote_age", "max_bar_age",
    ))
    eligibility = EligibilityPolicy(
        tuple(_text(x) for x in _sequence(item["verified_instrument_ids"])),
        parse_decimal(_text(item["min_price"])), parse_decimal(_text(item["min_bar_volume"])),
        parse_decimal(_text(item["max_spread_bps"])),
        _duration(item["max_quote_age"]), _duration(item["max_bar_age"]),
    )
    item = _record(data["benchmark"], (
        "fast_period", "slow_period", "trend_period", "atr_period", "atr_multiplier", "entry_cutoff",
    ))
    benchmark = BenchmarkConfig(
        _integer(item["fast_period"]), _integer(item["slow_period"]),
        _integer(item["trend_period"]), _integer(item["atr_period"]),
        parse_decimal(_text(item["atr_multiplier"])), _duration(item["entry_cutoff"]),
    )
    risk_names = (
        "per_trade_risk", "position_notional", "open_risk", "daily_drawdown",
        "paper_gross", "short_total", "short_position", "quantity_step", "price_tick",
        "round_trip_cost_per_share", "max_entry_deviation_bps",
    )
    item = _record(data["risk"], (*risk_names, "max_account_age"))
    risk = RiskPolicy(
        **{name: parse_decimal(_text(item[name])) for name in risk_names},
        max_account_age=_duration(item["max_account_age"]),
    )
    item = _record(data["simulation"], ("fee_per_share", "slippage_per_share", "volume_participation"))
    simulation = SimulationPolicy(
        parse_decimal(_text(item["fee_per_share"])),
        parse_decimal(_text(item["slippage_per_share"])),
        parse_decimal(_text(item["volume_participation"])),
    )
    return BacktestInput(
        instrument, tuple(bars), tuple(quotes), tuple(sessions), eligibility, benchmark,
        risk, simulation, parse_decimal(_text(data["initial_equity"])), _text(data["dataset_label"]),
    )


def read_bundle(path: Path) -> tuple[BacktestInput, object]:
    try:
        if path.stat().st_size > 50_000_000:
            raise DataError("REPLAY_INVALID", "Replay bundle exceeds 50 MB limit.")
        payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        bundle = _record(payload, ("schema_version", "input", "report"))
        if bundle["schema_version"] != "1":
            raise DataError("REPLAY_INVALID", "Unsupported replay bundle version.")
        return load_input(bundle["input"]), bundle["report"]
    except OSError as exc:
        raise DataError("REPLAY_IO", f"Cannot read local replay bundle: {exc.strerror}.") from exc
    except (ValueError, TypeError, OverflowError, ZoneInfoNotFoundError) as exc:
        if isinstance(exc, DataError):
            raise
        raise DataError("REPLAY_INVALID", "Malformed replay bundle or domain records.") from exc


def write_bundle(path: Path, data: BacktestInput, report: BacktestReport | None = None) -> None:
    payload = {"schema_version": "1", "input": json_value(data), "report": json_value(report)}
    try:
        # Never overwrite an existing dataset/report or accidentally follow an existing file link.
        with path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, sort_keys=True, indent=2, allow_nan=False) + "\n")
    except OSError as exc:
        raise DataError("REPLAY_IO", f"Cannot create local replay bundle: {exc.strerror}.") from exc


def replay_bundle(path: Path) -> BacktestReport:
    data, expected = read_bundle(path)
    if expected is None:
        raise DataError("REPLAY_INVALID", "Replay verification needs a previously recorded report.")
    actual = BacktestRunner().run(data)
    if json_value(actual) != expected:
        raise DataError("REPLAY_MISMATCH", "Inputs, implementation or recorded results differ.")
    return actual
