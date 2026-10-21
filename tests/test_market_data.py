import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

from trader_bot.market_data import (
    CsvMarketDataProvider, DataError, DataRequest, MarketDataClient, UsagePolicy,
    parse_decimal, parse_time,
)
from tests.support import FIXTURE, INSTRUMENT, NOW, OPEN, quote_at, snapshot


class CsvProviderTests(unittest.TestCase):
    def test_recorded_fixture_normalizes_decimal_and_utc(self) -> None:
        result = snapshot()
        self.assertEqual(len(result.bars), 54)
        self.assertEqual(result.bars[-1].close, Decimal(110))
        self.assertEqual(result.bars[-1].ends_at, NOW)
        self.assertEqual(result.bars[-1].interval_seconds, 300)
        self.assertEqual(result.source, "local_csv")

    def test_range_never_exposes_future_bar(self) -> None:
        end = NOW - timedelta(minutes=5)
        result = CsvMarketDataProvider(FIXTURE, quote_at(end)).fetch(
            DataRequest(INSTRUMENT, OPEN, end), end
        )
        self.assertEqual(len(result.bars), 53)
        self.assertEqual(result.bars[-1].close, Decimal(97))

    def test_missing_file_is_explicit_provider_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = CsvMarketDataProvider(Path(directory) / "missing.csv", quote_at())
            with self.assertRaises(DataError) as error:
                provider.fetch(DataRequest(INSTRUMENT, OPEN, NOW), NOW)
            self.assertEqual(error.exception.code, "PROVIDER_UNAVAILABLE")

    def test_malformed_missing_non_finite_and_wrong_identity_rejected(self) -> None:
        cases = (
            "bad,header\n1,2\n",
            "instrument_id,starts_at,open,high,low,close,volume\nUS:DEMO,2026-01-02T14:30:00Z,100\n",
            "instrument_id,starts_at,open,high,low,close,volume\nUS:DEMO,2026-01-02T14:30:00Z,NaN,101,99,100,100\n",
            "instrument_id,starts_at,open,high,low,close,volume\nUS:OTHER,2026-01-02T14:30:00Z,100,101,99,100,100\n",
            "instrument_id,starts_at,open,high,low,close,volume\nUS:DEMO,2026-01-02T14:30:00,100,101,99,100,100\n",
            "instrument_id,starts_at,open,high,low,close,volume\nUS:DEMO,2026-01-02T14:30:00Z,100,99,99,100,100\n",
            "instrument_id,starts_at,open,high,low,close,volume,open\nUS:DEMO,2026-01-02T14:30:00Z,100,101,99,100,100,100\n",
        )
        for data in cases:
            with self.subTest(data=data), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "bars.csv"
                path.write_text(data)
                with self.assertRaises(DataError) as error:
                    CsvMarketDataProvider(path, quote_at()).fetch(
                        DataRequest(INSTRUMENT, OPEN, NOW), NOW
                    )
                self.assertEqual(error.exception.code, "DATA_INVALID")

    def test_timezone_normalization_and_numeric_errors(self) -> None:
        self.assertEqual(parse_time("2026-01-02T09:30:00-05:00"), OPEN)
        for text in ("", "NaN", "Infinity", "not-a-number"):
            with self.subTest(text=text), self.assertRaises(DataError):
                parse_decimal(text)


class MarketDataClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = Mock()
        self.provider.fetch.return_value = snapshot()
        self.request = DataRequest(INSTRUMENT, OPEN, NOW)

    def test_cache_hit_and_expiry_consume_quota_correctly(self) -> None:
        client = MarketDataClient(self.provider, UsagePolicy(2))
        first = client.fetch(self.request, NOW)
        self.assertIs(client.fetch(self.request, NOW + timedelta(seconds=1)), first)
        self.assertEqual(client.cache_hits, 1)
        self.assertEqual(self.provider.fetch.call_count, 1)
        client.fetch(self.request, NOW + timedelta(seconds=30))
        self.assertEqual(self.provider.fetch.call_count, 2)
        with self.assertRaises(DataError) as error:
            client.fetch(self.request, NOW + timedelta(seconds=60))
        self.assertEqual(error.exception.code, "PROVIDER_QUOTA")

    def test_zero_budget_blocks_paid_provider_before_fetch(self) -> None:
        client = MarketDataClient(
            self.provider, UsagePolicy(2, cost_per_fetch=Decimal("0.01"))
        )
        with self.assertRaises(DataError) as error:
            client.fetch(self.request, NOW)
        self.assertEqual(error.exception.code, "COST_CAP")
        self.provider.fetch.assert_not_called()

    def test_failed_fetch_consumes_quota_and_is_not_cached(self) -> None:
        self.provider.fetch.side_effect = DataError("PROVIDER_UNAVAILABLE", "offline")
        client = MarketDataClient(self.provider, UsagePolicy(1))
        with self.assertRaises(DataError):
            client.fetch(self.request, NOW)
        with self.assertRaises(DataError) as error:
            client.fetch(self.request, NOW)
        self.assertEqual(error.exception.code, "PROVIDER_QUOTA")

    def test_bounded_cache_and_request_key_identity(self) -> None:
        client = MarketDataClient(self.provider, UsagePolicy(5, cache_size=1))
        client.fetch(self.request, NOW)
        second = replace(self.request, starts_at=OPEN + timedelta(minutes=5))
        client.fetch(second, NOW)
        client.fetch(self.request, NOW)
        self.assertEqual(self.provider.fetch.call_count, 3)

    def test_future_request_rejected_without_fetch(self) -> None:
        client = MarketDataClient(self.provider, UsagePolicy(1))
        with self.assertRaises(DataError):
            client.fetch(replace(self.request, ends_at=NOW + timedelta(seconds=1)), NOW)
        self.provider.fetch.assert_not_called()

    def test_cache_clock_reversal_does_not_reuse_future_snapshot(self) -> None:
        client = MarketDataClient(self.provider, UsagePolicy(2))
        client.fetch(self.request, NOW + timedelta(seconds=20))
        client.fetch(self.request, NOW)
        self.assertEqual(client.cache_hits, 0)
        self.assertEqual(self.provider.fetch.call_count, 2)

    def test_monthly_cost_and_daily_quota_reset_use_utc_dates(self) -> None:
        client = MarketDataClient(self.provider, UsagePolicy(
            requests_per_day=1, monthly_cost_cap=Decimal("0.02"),
            cost_per_fetch=Decimal("0.01"), cache_ttl=timedelta(0),
        ))
        client.fetch(self.request, NOW)
        client.fetch(self.request, NOW + timedelta(days=1))
        with self.assertRaises(DataError) as error:
            client.fetch(self.request, NOW + timedelta(days=2))
        self.assertEqual(error.exception.code, "COST_CAP")
        self.assertEqual(client.costs["2026-01"], Decimal("0.02"))
        client.fetch(self.request, NOW.replace(month=2))
        self.assertEqual(client.costs["2026-02"], Decimal("0.01"))
