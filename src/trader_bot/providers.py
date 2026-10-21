"""Read-only Alpaca IEX historical bars and quote adapter; no trading endpoints."""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from trader_bot.config import PaperCredentials
from trader_bot.contracts import ContractError, MarketBar, Quote
from trader_bot.market_data import (
    DataError, DataRequest, MarketSnapshot, parse_decimal, parse_time, utc_time,
)


class JsonTransport(Protocol):
    def get(self, url: str, headers: Mapping[str, str]) -> object: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str,
        headers: object, newurl: str,
    ) -> None:
        return None


class HttpJsonTransport:
    def get(self, url: str, headers: Mapping[str, str]) -> object:
        request = Request(url, headers=dict(headers), method="GET")
        try:
            # Never forward credentials to an untrusted redirect destination.
            with build_opener(_NoRedirect()).open(request, timeout=10) as response:
                payload = response.read(5_000_001)
                if len(payload) > 5_000_000:
                    raise DataError("DATA_INVALID", "Provider response exceeds size limit.")
                return json.loads(payload, parse_float=Decimal)
        except HTTPError as exc:
            code = "PROVIDER_QUOTA" if exc.code == 429 else "PROVIDER_UNAVAILABLE"
            raise DataError(code, f"Market-data HTTP request failed (status {exc.code}).") from None
        except (URLError, TimeoutError, OSError):
            raise DataError("PROVIDER_UNAVAILABLE", "Market-data connection failed.") from None
        except (json.JSONDecodeError, UnicodeError):
            raise DataError("DATA_INVALID", "Provider returned invalid JSON.") from None


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise DataError("DATA_INVALID", "Expected a provider JSON object.")
    return {str(key): item for key, item in value.items()}


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DataError("DATA_INVALID", "Missing/invalid provider text field.")
    return value


def _number(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise DataError("DATA_INVALID", "Missing/invalid provider numeric field.")
    return parse_decimal(str(value))


class AlpacaIexProvider:
    """Uses only data.alpaca.markets/v2/stocks, explicitly IEX/unadjusted/5Min.

    The caller must verify free-feed entitlements before construction. Pagination
    is bounded; incomplete/repeating pagination is an error, never partial success.
    """

    def __init__(
        self, credentials: PaperCredentials, *, free_feed_verified: bool,
        transport: JsonTransport | None = None, max_pages: int = 5,
        requests_per_minute: int = 30,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if free_feed_verified is not True:
            raise DataError("INSTRUMENT_UNSUPPORTED", "Verify free IEX access before use.")
        if max_pages <= 0 or requests_per_minute <= 0:
            raise DataError("DATA_INVALID", "Page and request limits must be positive.")
        self._credentials = credentials
        self._transport = transport if transport is not None else HttpJsonTransport()
        self.max_pages = max_pages
        self.requests_per_minute = requests_per_minute
        self._clock = clock
        self._request_times: deque[datetime] = deque()
        self.http_requests = 0

    def _get(self, url: str, headers: Mapping[str, str]) -> object:
        now = utc_time(self._clock())
        if self._request_times and now < self._request_times[-1]:
            raise DataError("PROVIDER_QUOTA", "Clock moved backwards; request quota uncertain.")
        while self._request_times and now - self._request_times[0] >= timedelta(minutes=1):
            self._request_times.popleft()
        if len(self._request_times) >= self.requests_per_minute:
            raise DataError("PROVIDER_QUOTA", "Configured HTTP requests/minute exhausted.")
        self._request_times.append(now)
        self.http_requests += 1
        return self._transport.get(url, headers)

    def fetch(self, request: DataRequest, as_of: datetime) -> MarketSnapshot:
        if request.ends_at > utc_time(as_of):
            raise DataError("DATA_FUTURE", "Data request extends beyond evaluation time.")
        instrument = request.instrument
        if (
            instrument.currency != "USD"
            or instrument.venue not in {"NYSE", "NASDAQ", "ARCA", "AMEX"}
            or instrument.market_calendar not in {"XNYS", "XNAS"}
        ):
            raise DataError("INSTRUMENT_UNSUPPORTED", "Only verified US USD stocks/ETFs supported.")
        symbol = quote(instrument.symbol, safe="")
        base = f"https://data.alpaca.markets/v2/stocks/{symbol}"
        headers = {
            "APCA-API-KEY-ID": self._credentials.api_key,
            "APCA-API-SECRET-KEY": self._credentials.api_secret,
        }
        params = {
            "start": utc_time(request.starts_at).isoformat(),
            "end": utc_time(request.ends_at).isoformat(),
            "timeframe": "5Min", "adjustment": "raw", "feed": "iex",
            "limit": "10000", "sort": "asc",
        }
        bars: list[MarketBar] = []
        tokens: set[str] = set()
        try:
            for page in range(self.max_pages):
                payload = _object(self._get(f"{base}/bars?{urlencode(params)}", headers))
                raw_bars = payload.get("bars")
                if not isinstance(raw_bars, list):
                    raise DataError("DATA_MISSING", "Provider bars are missing or invalid.")
                if "symbol" in payload and payload["symbol"] != instrument.symbol:
                    raise DataError("DATA_INVALID", "Provider returned another symbol.")
                for raw_bar in raw_bars:
                    item = _object(raw_bar)
                    starts_at = parse_time(_text(item.get("t")))
                    ends_at = starts_at + timedelta(minutes=5)
                    if not request.starts_at <= starts_at <= request.ends_at:
                        raise DataError("DATA_INVALID", "Provider bar outside requested range.")
                    # Alpaca end is inclusive on bar start: omit the forming bar.
                    if ends_at > request.ends_at:
                        continue
                    bars.append(MarketBar(
                        instrument_id=instrument.instrument_id, interval_seconds=300,
                        starts_at=starts_at, ends_at=ends_at,
                        open=_number(item.get("o")), high=_number(item.get("h")),
                        low=_number(item.get("l")), close=_number(item.get("c")),
                        volume=_number(item.get("v")), source="alpaca:iex",
                        adjustment_basis="unadjusted",
                    ))
                token = payload.get("next_page_token")
                if token is None:
                    break
                next_token = _text(token)
                if next_token in tokens or page + 1 == self.max_pages:
                    raise DataError("DATA_INVALID", "Incomplete or repeating bar pagination.")
                tokens.add(next_token)
                params["page_token"] = next_token
            latest = _object(self._get(f"{base}/quotes/latest?feed=iex", headers))
            if latest.get("symbol") != instrument.symbol:
                raise DataError("DATA_INVALID", "Provider quote symbol mismatch.")
            item = _object(latest.get("quote"))
            received_at = utc_time(self._clock())
            observed_quote = Quote(
                instrument_id=instrument.instrument_id,
                bid=_number(item.get("bp")), ask=_number(item.get("ap")),
                observed_at=parse_time(_text(item.get("t"))),
                received_at=received_at, source="alpaca:iex",
                bid_size=_number(item.get("bs")), ask_size=_number(item.get("as")),
            )
        except ContractError as exc:
            raise DataError("DATA_INVALID", f"Invalid provider market data: {exc}") from exc
        return MarketSnapshot(tuple(bars), observed_quote, received_at, "alpaca:iex")
