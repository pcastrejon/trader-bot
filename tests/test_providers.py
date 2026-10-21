import unittest
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from trader_bot.config import PaperCredentials
from trader_bot.market_data import DataError, DataRequest
from trader_bot.providers import AlpacaIexProvider, HttpJsonTransport, _NoRedirect
from tests.support import INSTRUMENT, NOW, OPEN


BAR = {"t": OPEN.isoformat(), "o": 100, "h": 101, "l": 99, "c": 100, "v": 1000}
QUOTE = {"symbol": "DEMO", "quote": {
    "bp": "110", "ap": "110.02", "bs": 100, "as": 200, "t": NOW.isoformat(),
}}


class AlpacaProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = Mock()
        self.provider = AlpacaIexProvider(
            PaperCredentials("fake-paper-key", "fake-paper-secret"),
            free_feed_verified=True, transport=self.transport, clock=lambda: NOW,
        )
        self.request = DataRequest(INSTRUMENT, OPEN, NOW)

    def test_pagination_normalization_and_read_only_feed(self) -> None:
        next_bar = dict(BAR, t=(OPEN + timedelta(minutes=5)).isoformat())
        self.transport.get.side_effect = [
            {"symbol": "DEMO", "bars": [BAR], "next_page_token": "next"},
            {"symbol": "DEMO", "bars": [next_bar], "next_page_token": None},
            QUOTE,
        ]
        result = self.provider.fetch(self.request, NOW)
        self.assertEqual(len(result.bars), 2)
        self.assertEqual(result.quote.ask, Decimal("110.02"))
        self.assertEqual(result.source, "alpaca:iex")
        self.assertEqual(self.provider.http_requests, 3)
        urls = [call.args[0] for call in self.transport.get.call_args_list]
        self.assertTrue(all(url.startswith("https://data.alpaca.markets/v2/stocks/") for url in urls))
        self.assertIn("feed=iex", urls[0])
        self.assertIn("adjustment=raw", urls[0])
        self.assertIn("timeframe=5Min", urls[0])
        self.assertIn("page_token=next", urls[1])
        self.assertTrue(urls[-1].endswith("quotes/latest?feed=iex"))

    def test_forming_inclusive_end_bar_is_not_exposed(self) -> None:
        self.transport.get.side_effect = [
            {"bars": [BAR, dict(BAR, t=NOW.isoformat())]}, QUOTE,
        ]
        result = self.provider.fetch(self.request, NOW)
        self.assertEqual(len(result.bars), 1)

    def test_pagination_limit_and_repeated_tokens_are_errors(self) -> None:
        self.transport.get.return_value = {"bars": [BAR], "next_page_token": "same"}
        with self.assertRaises(DataError) as error:
            self.provider.fetch(self.request, NOW)
        self.assertEqual(error.exception.code, "DATA_INVALID")
        self.assertEqual(self.transport.get.call_count, 2)

    def test_malformed_or_missing_data_is_never_success(self) -> None:
        for payload in ({"bars": None}, {"bars": ["bad"]}, {"bars": [dict(BAR, c="NaN")]}):
            with self.subTest(payload=payload):
                self.transport.get.return_value = payload
                with self.assertRaises(DataError):
                    self.provider.fetch(self.request, NOW)

    def test_quote_symbol_mismatch(self) -> None:
        self.transport.get.side_effect = [{"bars": [BAR]}, dict(QUOTE, symbol="OTHER")]
        with self.assertRaises(DataError) as error:
            self.provider.fetch(self.request, NOW)
        self.assertEqual(error.exception.code, "DATA_INVALID")

    def test_rate_limit_covers_every_http_call(self) -> None:
        provider = AlpacaIexProvider(
            PaperCredentials("fake", "fake"), free_feed_verified=True,
            transport=self.transport, requests_per_minute=1, clock=lambda: NOW,
        )
        self.transport.get.return_value = {"bars": [BAR]}
        with self.assertRaises(DataError) as error:
            provider.fetch(self.request, NOW)
        self.assertEqual(error.exception.code, "PROVIDER_QUOTA")
        self.assertEqual(self.transport.get.call_count, 1)

    def test_free_access_must_be_verified(self) -> None:
        with self.assertRaises(DataError):
            AlpacaIexProvider(PaperCredentials("fake", "fake"), free_feed_verified=False)

    def test_http_errors_are_sanitized_and_not_retried(self) -> None:
        for failure, code in (
            (HTTPError("https://secret", 429, "secret-body", {}, None), "PROVIDER_QUOTA"),
            (URLError("secret-url"), "PROVIDER_UNAVAILABLE"),
        ):
            opener = Mock()
            opener.open.side_effect = failure
            with patch("trader_bot.providers.build_opener", return_value=opener):
                with self.assertRaises(DataError) as error:
                    HttpJsonTransport().get("https://data.alpaca.markets", {"key": "secret"})
            self.assertEqual(error.exception.code, code)
            self.assertNotIn("secret", str(error.exception))
            self.assertEqual(opener.open.call_count, 1)

    def test_credentials_are_not_forwarded_on_redirect(self) -> None:
        self.assertIsNone(_NoRedirect().redirect_request(Mock(), None, 302, "", None, "https://other"))
