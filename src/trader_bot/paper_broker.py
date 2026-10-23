"""Paper-only Alpaca execution boundary with durable, fail-closed order tracking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from trader_bot.config import ConfigurationError, RuntimeConfig
from trader_bot.contracts import (
    AccountSnapshot, AssetType, Instrument, RuntimeMode, TradeDirection,
)
from trader_bot.market_data import parse_time, utc_time
from trader_bot.risk import CircuitState, OrderIntent, RiskOutcome
from trader_bot.strategy import ExchangeSession


PAPER_API_BASE_URL = "https://paper-api.alpaca.markets"
_MAX_RESPONSE_BYTES = 1_000_000
_ACTIVE_STATUSES = {
    "accepted", "accepted_for_bidding", "new", "partially_filled",
    "pending_new", "pending_cancel", "pending_replace", "held",
}
_TERMINAL_STATUSES = {"canceled", "expired", "filled", "rejected"}
_PROTECTIVE_ACTIVE_STATUSES = {
    "accepted", "accepted_for_bidding", "new", "partially_filled", "pending_new",
}


class PaperBrokerError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        self.code = code
        self.status = status
        super().__init__(message)


class PaperOrderUnknownError(PaperBrokerError):
    """The broker may have accepted a write; reconciliation is required."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str,
        headers: object, newurl: str,
    ) -> None:
        return None


class PaperJsonTransport:
    """Authenticated JSON transport pinned to the official paper API origin."""

    def __init__(self, config: RuntimeConfig) -> None:
        if config.mode is not RuntimeMode.PAPER or config.paper_credentials is None:
            raise ConfigurationError("Alpaca Paper API requires paper mode and paper credentials.")
        self._credentials = config.paper_credentials

    def request(self, method: str, path: str, payload: object | None = None) -> object:
        if (
            method not in {"GET", "POST", "DELETE"}
            or not path.startswith("/v2/")
            or path.startswith("//")
            or any(character in path for character in "\r\n\\#")
            or "/../" in path
        ):
            raise PaperBrokerError("REQUEST_INVALID", "Unsupported paper API request.")
        body = None if payload is None else json.dumps(
            payload, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        request = Request(
            f"{PAPER_API_BASE_URL}{path}", data=body,
            headers={
                "APCA-API-KEY-ID": self._credentials.api_key,
                "APCA-API-SECRET-KEY": self._credentials.api_secret,
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
            method=method,
        )
        try:
            with build_opener(ProxyHandler({}), _NoRedirect()).open(
                request, timeout=10,
            ) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(raw) > _MAX_RESPONSE_BYTES:
                    raise PaperBrokerError("BROKER_RESPONSE_INVALID", "Paper API response exceeds size limit.")
                return _decode_json(raw)
        except HTTPError as exc:
            if method == "POST" and 400 <= exc.code < 500:
                raise PaperBrokerError(
                    "BROKER_REJECTED", f"Paper broker rejected the request (HTTP {exc.code}).",
                    status=exc.code,
                ) from None
            if method == "GET" and exc.code == 404:
                raise PaperBrokerError("BROKER_NOT_FOUND", "Paper broker record was not found.", status=404) from None
            if method in {"POST", "DELETE"}:
                raise PaperOrderUnknownError(
                    "BROKER_STATE_UNKNOWN", "Paper broker write outcome is unknown; reconcile before retrying.",
                    status=exc.code,
                ) from None
            raise PaperBrokerError(
                "BROKER_UNAVAILABLE", f"Paper broker request failed (HTTP {exc.code}).",
                status=exc.code,
            ) from None
        except (URLError, TimeoutError, OSError):
            if method in {"POST", "DELETE"}:
                raise PaperOrderUnknownError(
                    "BROKER_STATE_UNKNOWN", "Paper broker write outcome is unknown; reconcile before retrying.",
                ) from None
            raise PaperBrokerError("BROKER_UNAVAILABLE", "Paper broker connection failed.") from None
        except (json.JSONDecodeError, UnicodeError):
            if method in {"POST", "DELETE"}:
                raise PaperOrderUnknownError(
                    "BROKER_STATE_UNKNOWN", "Paper broker write outcome is unknown; reconcile before retrying.",
                ) from None
            raise PaperBrokerError("BROKER_RESPONSE_INVALID", "Paper broker returned invalid JSON.") from None


def _decode_json(raw: bytes) -> object:
    if not raw:
        return None

    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        return json.loads(
            raw.decode("utf-8"), parse_float=Decimal, object_pairs_hook=object_pairs,
        )
    except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
        raise PaperBrokerError("BROKER_RESPONSE_INVALID", "Paper broker returned invalid JSON.") from exc


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise PaperBrokerError("BROKER_RESPONSE_INVALID", "Paper broker returned an invalid object.")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PaperBrokerError("BROKER_RESPONSE_INVALID", f"Paper broker omitted {field}.")
    return value


def _amount(value: object, field: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise PaperBrokerError("BROKER_RESPONSE_INVALID", f"Paper broker returned invalid {field}.")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise PaperBrokerError("BROKER_RESPONSE_INVALID", f"Paper broker returned invalid {field}.") from exc
    if not amount.is_finite() or amount < 0 or (positive and amount == 0):
        raise PaperBrokerError("BROKER_RESPONSE_INVALID", f"Paper broker returned invalid {field}.")
    return amount


def _signed_amount(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise PaperBrokerError("BROKER_RESPONSE_INVALID", f"Paper broker returned invalid {field}.")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise PaperBrokerError("BROKER_RESPONSE_INVALID", f"Paper broker returned invalid {field}.") from exc
    if not amount.is_finite():
        raise PaperBrokerError("BROKER_RESPONSE_INVALID", f"Paper broker returned invalid {field}.")
    return amount


@dataclass(frozen=True, slots=True)
class PaperOrder:
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: str
    status: str
    order_class: str
    ordered_quantity: Decimal
    filled_quantity: Decimal
    limit_price: Decimal
    stop_price: Decimal | None
    created_at: datetime
    updated_at: datetime
    legs: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class PaperPosition:
    symbol: str
    quantity: Decimal
    side: str
    market_value: Decimal


def _order(value: object) -> PaperOrder:
    item = _object(value)
    raw_legs = item.get("legs")
    if raw_legs is None:
        legs: tuple[dict[str, object], ...] = ()
    elif isinstance(raw_legs, list):
        legs = tuple(_object(leg) for leg in raw_legs)
    else:
        raise PaperBrokerError("BROKER_RESPONSE_INVALID", "Paper broker returned invalid order legs.")
    status = _text(item.get("status"), "order status").lower()
    if status not in _ACTIVE_STATUSES | _TERMINAL_STATUSES:
        status = "unknown"
    try:
        created = parse_time(_text(item.get("created_at"), "order creation time"))
        updated = parse_time(_text(item.get("updated_at", item.get("created_at")), "order update time"))
    except ValueError as exc:
        raise PaperBrokerError("BROKER_RESPONSE_INVALID", "Paper broker returned invalid order timestamps.") from exc
    limit = _amount(item.get("limit_price"), "limit price", positive=True)
    stop_value = item.get("stop_price")
    if stop_value is None or str(stop_value) == "0":
        stop_value = next(
            (
                leg.get("stop_price") for leg in legs
                if str(leg.get("type", "")).lower() == "stop"
            ),
            None,
        )
    stop = (
        _amount(stop_value, "protective stop price", positive=True)
        if stop_value is not None else None
    )
    ordered_quantity = _amount(item.get("qty"), "order quantity", positive=True)
    filled_quantity = _amount(item.get("filled_qty", "0"), "filled quantity")
    if filled_quantity > ordered_quantity:
        raise PaperBrokerError(
            "BROKER_RESPONSE_INVALID", "Paper broker reported more filled shares than ordered.",
        )
    return PaperOrder(
        _text(item.get("id"), "order id"),
        _text(item.get("client_order_id"), "client order id"),
        _text(item.get("symbol"), "symbol"),
        _text(item.get("side"), "side").lower(),
        status,
        _text(item.get("order_class"), "order class").lower(),
        ordered_quantity,
        filled_quantity,
        limit, stop, created, updated, legs,
    )


class PaperOrderJournal:
    """Durable single-account order journal; operational event rows retain seven days."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    client_order_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    limit_price TEXT NOT NULL,
                    stop_price TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    broker_order_id TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY,
                    occurred_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    client_order_id TEXT NOT NULL,
                    detail TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS safety (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    halted INTEGER NOT NULL,
                    reason TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS circuit_states (
                    account_id TEXT NOT NULL,
                    calendar TEXT NOT NULL,
                    session_open TEXT NOT NULL,
                    session_close TEXT NOT NULL,
                    start_equity TEXT NOT NULL,
                    tripped INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, calendar, session_open)
                );
                INSERT OR IGNORE INTO safety(id, halted, reason) VALUES (1, 0, '');
                """
            )
            self._prune_events(db, datetime.now(UTC))
        os.chmod(path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level="IMMEDIATE")
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA synchronous = FULL")
        return db

    def prepare(
        self, account_id: str, instrument: Instrument, intent: OrderIntent, now: datetime,
    ) -> bool:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM orders WHERE client_order_id = ?", (intent.idempotency_key,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["account_id"] != account_id
                    or existing["symbol"] != instrument.symbol
                    or existing["quantity"] != str(intent.quantity)
                    or existing["limit_price"] != str(intent.limit_price)
                    or existing["stop_price"] != str(intent.stop_price)
                ):
                    raise PaperBrokerError(
                        "IDEMPOTENCY_CONFLICT", "Client order key already belongs to different order data.",
                    )
                return False
            safety = db.execute("SELECT halted, reason FROM safety WHERE id = 1").fetchone()
            if safety["halted"]:
                raise PaperBrokerError(
                    "PAPER_ENTRIES_HALTED",
                    "Paper entries are halted pending reconciliation: " + safety["reason"],
                )
            if db.execute(
                "SELECT 1 FROM orders WHERE account_id != ? LIMIT 1", (account_id,),
            ).fetchone():
                raise PaperBrokerError(
                    "JOURNAL_ACCOUNT_MISMATCH", "Journal belongs to a different paper account.",
                )
            active = db.execute(
                "SELECT 1 FROM orders WHERE status NOT IN ('canceled','expired','filled','rejected') LIMIT 1",
            ).fetchone()
            if active:
                raise PaperBrokerError(
                    "PAPER_ORDER_UNRESOLVED", "Reconcile the existing paper order before submitting another.",
                )
            db.execute(
                """INSERT INTO orders (
                    client_order_id, account_id, correlation_id, instrument_id, symbol,
                    quantity, limit_price, stop_price, expires_at, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (
                    intent.idempotency_key, account_id, intent.correlation_id,
                    instrument.instrument_id, instrument.symbol, str(intent.quantity),
                    str(intent.limit_price), str(intent.stop_price),
                    utc_time(intent.expires_at).isoformat(), utc_time(now).isoformat(),
                ),
            )
            self._event(db, now, "submission_prepared", intent, "Risk outcome approved.")
            return True

    def get(self, client_order_id: str) -> sqlite3.Row | None:
        with self._connect() as db:
            return db.execute(
                "SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,),
            ).fetchone()

    def update(self, intent: OrderIntent, status: str, broker_order_id: str | None, at: datetime, detail: str) -> None:
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE orders SET status = ?, broker_order_id = COALESCE(?, broker_order_id),
                    updated_at = ? WHERE client_order_id = ?""",
                (
                    status, broker_order_id, utc_time(at).isoformat(),
                    intent.idempotency_key,
                ),
            )
            if cursor.rowcount != 1:
                raise PaperBrokerError("PAPER_JOURNAL_MISSING", "Prepared paper order is missing from the journal.")
            if status == "unknown":
                db.execute("UPDATE safety SET halted = 1, reason = ? WHERE id = 1", (detail,))
            self._event(db, at, "order_" + status, intent, detail)

    def set_status(self, client_order_id: str, status: str, broker_order_id: str | None, at: datetime) -> None:
        with self._connect() as db:
            row = db.execute(
                "SELECT correlation_id FROM orders WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
            db.execute(
                """UPDATE orders SET status = ?, broker_order_id = COALESCE(?, broker_order_id),
                    updated_at = ? WHERE client_order_id = ?""",
                (status, broker_order_id, utc_time(at).isoformat(), client_order_id),
            )
            if row is None:
                raise PaperBrokerError(
                    "PAPER_JOURNAL_MISSING", "Order state is missing from the local journal.",
                )
            db.execute(
                """INSERT INTO audit_events(
                    occurred_at, event_type, correlation_id, client_order_id, detail
                ) VALUES (?, ?, ?, ?, ?)""",
                (
                    utc_time(at).isoformat(), "order_" + status,
                    row["correlation_id"], client_order_id,
                    "Broker order status reconciled.",
                ),
            )
            if status == "unknown":
                db.execute(
                    "UPDATE safety SET halted = 1, reason = ? WHERE id = 1",
                    ("Broker order state could not be reconciled.",),
                )
            self._prune_events(db, at)

    def unresolved(self) -> tuple[sqlite3.Row, ...]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM orders WHERE status NOT IN
                    ('canceled','expired','filled','rejected') ORDER BY updated_at""",
            ).fetchall()
            return tuple(rows)

    def clear_halt_after_reconciliation(self) -> None:
        now = datetime.now(UTC)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM orders WHERE status = 'unknown' LIMIT 1",
            ).fetchone():
                raise PaperBrokerError(
                    "PAPER_STATE_UNKNOWN", "Unknown paper orders require operator investigation.",
                )
            previous = db.execute(
                "SELECT halted FROM safety WHERE id = 1",
            ).fetchone()
            db.execute("UPDATE safety SET halted = 0, reason = '' WHERE id = 1")
            if previous["halted"]:
                db.execute(
                    """INSERT INTO audit_events(
                        occurred_at, event_type, correlation_id, client_order_id, detail
                    ) VALUES (?, ?, ?, ?, ?)""",
                    (
                        utc_time(now).isoformat(), "operator_resume", "", "",
                        "Entry halt cleared after explicit broker reconciliation.",
                    ),
                )
                self._prune_events(db, now)

    def pause_entries(self) -> tuple[bool, str]:
        now = datetime.now(UTC)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT halted, reason FROM safety WHERE id = 1").fetchone()
            if not row["halted"]:
                db.execute(
                    "UPDATE safety SET halted = 1, reason = ? WHERE id = 1",
                    ("OPERATOR_PAUSE: New paper entries paused by operator.",),
                )
                db.execute(
                    """INSERT INTO audit_events(
                        occurred_at, event_type, correlation_id, client_order_id, detail
                    ) VALUES (?, ?, ?, ?, ?)""",
                    (
                        utc_time(now).isoformat(), "operator_pause", "", "",
                        "Operator paused new paper entries.",
                    ),
                )
                self._prune_events(db, now)
            current = db.execute(
                "SELECT halted, reason FROM safety WHERE id = 1",
            ).fetchone()
            return bool(current["halted"]), str(current["reason"])

    def safety_state(self) -> tuple[bool, str]:
        with self._connect() as db:
            row = db.execute("SELECT halted, reason FROM safety WHERE id = 1").fetchone()
            return bool(row["halted"]), str(row["reason"])

    def load_circuit_state(
        self, account_id: str, session: ExchangeSession,
    ) -> CircuitState | None:
        session_open = utc_time(session.opens_at).isoformat()
        with self._connect() as db:
            row = db.execute(
                """SELECT * FROM circuit_states
                    WHERE account_id = ? AND calendar = ? AND session_open = ?""",
                (account_id, session.calendar, session_open),
            ).fetchone()
        if row is None:
            return None
        if row["session_close"] != utc_time(session.closes_at).isoformat():
            raise PaperBrokerError(
                "PAPER_CIRCUIT_CONFLICT",
                "Persisted circuit session close does not match the configured exchange session.",
            )
        return CircuitState(
            parse_time(row["session_open"]), parse_time(row["session_close"]),
            Decimal(row["start_equity"]), bool(row["tripped"]),
        )

    def save_circuit_state(
        self, account_id: str, session: ExchangeSession, state: CircuitState,
        at: datetime,
    ) -> CircuitState:
        if (
            not account_id.strip()
            or utc_time(state.session_open) != utc_time(session.opens_at)
            or utc_time(state.session_close) != utc_time(session.closes_at)
        ):
            raise PaperBrokerError(
                "PAPER_CIRCUIT_CONFLICT",
                "Circuit state must match its account and configured session.",
            )
        session_open = utc_time(session.opens_at).isoformat()
        session_close = utc_time(session.closes_at).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT * FROM circuit_states
                    WHERE account_id = ? AND calendar = ? AND session_open = ?""",
                (account_id, session.calendar, session_open),
            ).fetchone()
            tripped = state.tripped
            if row is not None:
                if (
                    row["session_close"] != session_close
                    or Decimal(row["start_equity"]) != state.start_equity
                ):
                    raise PaperBrokerError(
                        "PAPER_CIRCUIT_CONFLICT",
                        "Persisted circuit baseline cannot be changed during a session.",
                    )
                tripped = tripped or bool(row["tripped"])
            db.execute(
                """INSERT INTO circuit_states(
                    account_id, calendar, session_open, session_close, start_equity,
                    tripped, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, calendar, session_open) DO UPDATE SET
                    tripped = excluded.tripped, updated_at = excluded.updated_at""",
                (
                    account_id, session.calendar, session_open, session_close,
                    str(state.start_equity), int(tripped), utc_time(at).isoformat(),
                ),
            )
        return CircuitState(
            state.session_open, state.session_close, state.start_equity, tripped,
        )

    @staticmethod
    def _event(
        db: sqlite3.Connection, at: datetime, event_type: str,
        intent: OrderIntent, detail: str,
    ) -> None:
        db.execute(
            """INSERT INTO audit_events(
                occurred_at, event_type, correlation_id, client_order_id, detail
            ) VALUES (?, ?, ?, ?, ?)""",
            (
                utc_time(at).isoformat(), event_type, intent.correlation_id,
                intent.idempotency_key, detail[:500],
            ),
        )
        PaperOrderJournal._prune_events(db, at)

    @staticmethod
    def _prune_events(db: sqlite3.Connection, at: datetime) -> None:
        cutoff = (utc_time(at) - timedelta(days=7)).isoformat()
        db.execute("DELETE FROM audit_events WHERE occurred_at < ?", (cutoff,))


class AlpacaPaperBroker:
    """Paper account client; order writes require opt-in and a fresh Risk Warden approval."""

    def __init__(
        self, config: RuntimeConfig, journal: PaperOrderJournal | None = None,
        *, transport: PaperJsonTransport | None = None,
        clock=lambda: datetime.now(UTC),
    ) -> None:
        if config.mode is not RuntimeMode.PAPER or config.paper_credentials is None:
            raise ConfigurationError("Alpaca Paper API requires paper mode and paper credentials.")
        self._config = config
        self._transport = transport if transport is not None else PaperJsonTransport(config)
        self._journal = journal
        self._clock = clock

    def account(self) -> AccountSnapshot:
        item = _object(self._transport.request("GET", "/v2/account"))
        if (
            _text(item.get("status"), "account status").lower() != "active"
            or item.get("account_blocked") is not False
            or item.get("trading_blocked") is not False
        ):
            raise PaperBrokerError("PAPER_ACCOUNT_BLOCKED", "Paper account is inactive or trading-blocked.")
        currency = _text(item.get("currency"), "account currency").upper()
        if currency != "USD":
            raise PaperBrokerError("CURRENCY_UNSUPPORTED", "Only USD paper accounts are supported.")
        return AccountSnapshot(
            _text(item.get("id"), "account id"), RuntimeMode.PAPER,
            _amount(item.get("equity"), "account equity", positive=True),
            _amount(item.get("buying_power"), "buying power"),
            currency, utc_time(self._clock()),
        )

    def positions(self) -> tuple[PaperPosition, ...]:
        value = self._transport.request("GET", "/v2/positions")
        if not isinstance(value, list) or len(value) > 500:
            raise PaperBrokerError("BROKER_RESPONSE_INVALID", "Paper broker returned invalid positions.")
        positions: list[PaperPosition] = []
        for raw in value:
            item = _object(raw)
            positions.append(PaperPosition(
                _text(item.get("symbol"), "position symbol"),
                _amount(item.get("qty"), "position quantity", positive=True),
                _text(item.get("side"), "position side").lower(),
                _signed_amount(item.get("market_value"), "position market value"),
            ))
        return tuple(positions)

    def open_orders(self) -> tuple[PaperOrder, ...]:
        value = self._transport.request(
            "GET", "/v2/orders?status=open&nested=true&limit=500",
        )
        if not isinstance(value, list) or len(value) >= 500:
            raise PaperBrokerError("BROKER_RESPONSE_INVALID", "Paper broker returned invalid open orders.")
        return tuple(_order(item) for item in value)

    def order_by_client_id(self, client_order_id: str) -> PaperOrder:
        value = self._transport.request(
            "GET", f"/v2/orders:by_client_order_id?client_order_id={quote(client_order_id, safe='')}&nested=true",
        )
        return _order(value)

    def cancel_order(self, broker_order_id: str) -> None:
        self._transport.request("DELETE", f"/v2/orders/{quote(broker_order_id, safe='')}")

    def load_circuit_state(
        self, account_id: str, session: ExchangeSession,
    ) -> CircuitState | None:
        if self._journal is None:
            raise PaperBrokerError(
                "PAPER_JOURNAL_REQUIRED", "A durable journal is required for circuit state.",
            )
        return self._journal.load_circuit_state(account_id, session)

    def save_circuit_state(
        self, account_id: str, session: ExchangeSession, state: CircuitState,
        at: datetime,
    ) -> CircuitState:
        if self._journal is None:
            raise PaperBrokerError(
                "PAPER_JOURNAL_REQUIRED", "A durable journal is required for circuit state.",
            )
        return self._journal.save_circuit_state(account_id, session, state, at)

    def safety_state(self) -> tuple[bool, str]:
        if self._journal is None:
            raise PaperBrokerError(
                "PAPER_JOURNAL_REQUIRED", "A durable journal is required for entry safety state.",
            )
        return self._journal.safety_state()

    def submit_approved(
        self, outcome: RiskOutcome, instrument: Instrument,
    ) -> PaperOrder:
        return self._submit_approved(outcome, instrument)

    def submit_lifecycle_test(
        self, outcome: RiskOutcome, instrument: Instrument, *, max_notional: Decimal,
    ) -> PaperOrder:
        if (
            not isinstance(max_notional, Decimal) or not max_notional.is_finite()
            or max_notional <= 0 or max_notional > Decimal("100")
        ):
            raise PaperBrokerError(
                "TEST_ORDER_LIMIT_INVALID",
                "Lifecycle-test notional cap must be positive and no greater than USD 100.",
            )
        return self._submit_approved(
            outcome, instrument, capability_test=True, max_notional=max_notional,
        )

    def _submit_approved(
        self, outcome: RiskOutcome, instrument: Instrument, *,
        capability_test: bool = False, max_notional: Decimal | None = None,
    ) -> PaperOrder:
        if not self._config.paper_order_submission_enabled:
            raise PaperBrokerError(
                "PAPER_SUBMISSION_DISABLED",
                "Set TRADER_BOT_PAPER_ORDER_SUBMISSION_ENABLED=true to enable paper order submission.",
            )
        if not capability_test and not self._config.paper_execution_capabilities_verified:
            raise PaperBrokerError(
                "PAPER_CAPABILITIES_UNVERIFIED",
                "Verify paper OTO, partial-fill protection, and cancellation behavior before enabling orders.",
            )
        if self._journal is None:
            raise PaperBrokerError(
                "PAPER_JOURNAL_REQUIRED", "Durable order journaling is required before paper submission.",
            )
        intent = self._validate_approval(outcome, instrument)
        if max_notional is not None and intent.quantity * intent.limit_price > max_notional:
            raise PaperBrokerError(
                "TEST_ORDER_LIMIT_EXCEEDED",
                "Risk-approved order exceeds the lifecycle-test notional cap.",
            )
        now = utc_time(self._clock())
        journal_entry = self._journal.get(intent.idempotency_key)
        if capability_test and journal_entry is not None:
            raise PaperBrokerError(
                "TEST_ORDER_ALREADY_EXISTS",
                "Lifecycle-test client order ID already exists in the local journal.",
            )

        try:
            previous = self.order_by_client_id(intent.idempotency_key)
        except PaperBrokerError as exc:
            if exc.code != "BROKER_NOT_FOUND":
                if journal_entry is not None:
                    self._journal.set_status(intent.idempotency_key, "unknown", None, now)
                    raise PaperOrderUnknownError(
                        "BROKER_STATE_UNKNOWN",
                        "Previously journaled order cannot be checked; entries halted.",
                    ) from exc
                raise
            if journal_entry is not None:
                self._journal.set_status(intent.idempotency_key, "unknown", None, now)
                raise PaperOrderUnknownError(
                    "BROKER_STATE_UNKNOWN",
                    "Previously journaled order is not visible at the broker; manual investigation is required.",
                ) from None
        else:
            if capability_test:
                raise PaperBrokerError(
                    "TEST_ORDER_ALREADY_EXISTS",
                    "Lifecycle-test client order ID already exists at the broker.",
                )
            self._assert_same_order(previous, instrument, intent)
            self._journal.prepare(self._account_id(), instrument, intent, now)
            try:
                status = self._journal_status(previous, intent.stop_price)
            except PaperBrokerError as exc:
                self._journal.set_status(intent.idempotency_key, "unknown", previous.broker_order_id, now)
                raise PaperOrderUnknownError(
                    "PROTECTIVE_EXIT_UNCONFIRMED",
                    "Existing paper order has no verified protective-stop state.",
                ) from exc
            self._journal.set_status(
                intent.idempotency_key, status, previous.broker_order_id, now,
            )
            return previous

        account = self.account()
        account_age = account.as_of - outcome.decision.evaluated_at
        if account_age < timedelta(0) or account_age > timedelta(seconds=30):
            raise PaperBrokerError("RISK_DECISION_STALE", "Risk approval does not match current account state.")
        if account.equity != outcome.decision.account_equity:
            raise PaperBrokerError("ACCOUNT_CHANGED", "Paper account equity changed after risk approval.")
        if intent.quantity * intent.limit_price > account.buying_power:
            raise PaperBrokerError("BUYING_POWER", "Paper buying power is below the approved entry notional.")
        if self.positions() or self.open_orders():
            raise PaperBrokerError(
                "BROKER_STATE_UNKNOWN",
                "Initial paper integration requires a flat account with no open orders.",
            )

        if not self._journal.prepare(account.account_reference, instrument, intent, now):
            self._journal.set_status(intent.idempotency_key, "unknown", None, now)
            raise PaperOrderUnknownError(
                "BROKER_STATE_UNKNOWN",
                "A concurrent or previous submission owns this client order key; reconcile before retrying.",
            )
        payload = {
            "symbol": instrument.symbol,
            "qty": str(intent.quantity),
            "side": "buy",
            "type": "limit",
            "time_in_force": "day",
            "limit_price": str(intent.limit_price),
            "order_class": "oto",
            "stop_loss": {"stop_price": str(intent.stop_price)},
            "client_order_id": intent.idempotency_key,
            "extended_hours": False,
        }
        try:
            submitted = _order(self._transport.request("POST", "/v2/orders", payload))
            self._assert_same_order(submitted, instrument, intent)
            if (
                submitted.order_class != "oto"
                or not self._has_protective_stop(
                    submitted, intent.stop_price,
                    submitted.filled_quantity or intent.quantity,
                )
            ):
                submitted = self.order_by_client_id(intent.idempotency_key)
                self._assert_same_order(submitted, instrument, intent)
            if submitted.order_class != "oto":
                raise PaperOrderUnknownError(
                    "PROTECTIVE_EXIT_UNCONFIRMED",
                    "The broker has not confirmed the attached protective stop; entries halted.",
                )
            journal_status = self._journal_status(submitted, intent.stop_price)
        except PaperOrderUnknownError as exc:
            self._journal.update(intent, "unknown", None, now, exc.code)
            raise
        except PaperBrokerError as exc:
            if exc.code == "BROKER_REJECTED":
                self._journal.update(intent, "rejected", None, now, exc.code)
                raise
            self._journal.update(intent, "unknown", None, now, exc.code)
            if isinstance(exc, PaperOrderUnknownError):
                raise
            raise PaperOrderUnknownError(
                "BROKER_STATE_UNKNOWN",
                "Paper order response could not be reconciled; entries halted.",
            ) from exc
        self._journal.update(
            intent, journal_status, submitted.broker_order_id, now, "Broker response reconciled.",
        )
        if journal_status == "rejected":
            raise PaperBrokerError("BROKER_REJECTED", "Paper broker rejected the approved order.")
        return submitted

    def cancel_lifecycle_test(
        self, submitted: PaperOrder, *, max_notional: Decimal,
    ) -> PaperOrder:
        if (
            not self._config.paper_order_submission_enabled
            or not isinstance(max_notional, Decimal)
            or not max_notional.is_finite()
            or max_notional <= 0 or max_notional > Decimal("100")
        ):
            raise PaperBrokerError(
                "TEST_ORDER_LIMIT_INVALID",
                "Lifecycle-test cancellation requires enabled paper writes and a cap at or below USD 100.",
            )
        if self._journal is None:
            raise PaperBrokerError(
                "PAPER_JOURNAL_REQUIRED", "Durable order journaling is required for test cancellation.",
            )
        intent_row = self._journal.get(submitted.client_order_id)
        if intent_row is None:
            raise PaperBrokerError(
                "PAPER_JOURNAL_MISSING", "Lifecycle-test order is absent from the local journal.",
            )
        if Decimal(intent_row["quantity"]) * Decimal(intent_row["limit_price"]) > max_notional:
            raise PaperBrokerError(
                "TEST_ORDER_LIMIT_EXCEEDED",
                "Journaled order exceeds the lifecycle-test notional cap.",
            )
        try:
            current = self.order_by_client_id(submitted.client_order_id)
        except PaperBrokerError as exc:
            self._journal.set_status(
                submitted.client_order_id, "unknown", submitted.broker_order_id,
                utc_time(self._clock()),
            )
            raise PaperOrderUnknownError(
                "BROKER_STATE_UNKNOWN", "Lifecycle-test order cannot be inspected before cancellation.",
            ) from exc

        if current.broker_order_id != submitted.broker_order_id:
            self._journal.set_status(
                submitted.client_order_id, "unknown", current.broker_order_id,
                utc_time(self._clock()),
            )
            raise PaperOrderUnknownError(
                "BROKER_STATE_UNKNOWN", "Lifecycle-test order identity changed during verification.",
            )
        if current.filled_quantity > 0:
            self._record_lifecycle_fill(current, Decimal(intent_row["stop_price"]))
        if current.status not in _ACTIVE_STATUSES:
            status = self._journal_status(current, Decimal(intent_row["stop_price"]))
            self._journal.set_status(
                current.client_order_id, status, current.broker_order_id, utc_time(self._clock()),
            )
            return current

        try:
            self.cancel_order(current.broker_order_id)
            current = self.order_by_client_id(current.client_order_id)
        except PaperBrokerError as exc:
            self._journal.set_status(
                submitted.client_order_id, "unknown", submitted.broker_order_id,
                utc_time(self._clock()),
            )
            raise PaperOrderUnknownError(
                "BROKER_STATE_UNKNOWN", "Lifecycle-test cancellation could not be confirmed.",
            ) from exc
        if current.filled_quantity > 0:
            self._record_lifecycle_fill(current, Decimal(intent_row["stop_price"]))
        if current.status in _ACTIVE_STATUSES:
            self._journal.set_status(
                current.client_order_id, "unknown", current.broker_order_id,
                utc_time(self._clock()),
            )
            raise PaperOrderUnknownError(
                "ORDER_CANCEL_UNCONFIRMED",
                "Lifecycle-test cancellation remains active; manual broker review is required.",
            )
        status = self._journal_status(current, Decimal(intent_row["stop_price"]))
        self._journal.set_status(
            current.client_order_id, status, current.broker_order_id, utc_time(self._clock()),
        )
        return current

    def _record_lifecycle_fill(self, order: PaperOrder, expected_stop: Decimal) -> None:
        if self._journal is None:
            raise PaperBrokerError(
                "PAPER_JOURNAL_REQUIRED", "Durable order journaling is required for fill handling.",
            )
        try:
            status = self._journal_status(order, expected_stop)
            self._journal.set_status(
                order.client_order_id, status, order.broker_order_id, utc_time(self._clock()),
            )
        except PaperBrokerError:
            self._journal.set_status(
                order.client_order_id, "unknown", order.broker_order_id, utc_time(self._clock()),
            )
        self._journal.pause_entries()
        raise PaperOrderUnknownError(
            "TEST_ORDER_FILLED",
            "The lifecycle-test order filled during cancellation; entries are paused. "
            "Review the paper position and protective stop manually.",
        )

    def reconcile(
        self, *, cancel_expired: bool = False, acknowledge_reconciled: bool = False,
    ) -> tuple[PaperOrder, ...]:
        if not isinstance(cancel_expired, bool) or not isinstance(acknowledge_reconciled, bool):
            raise PaperBrokerError("REQUEST_INVALID", "Reconciliation options must be booleans.")
        if self._journal is None:
            raise PaperBrokerError("PAPER_JOURNAL_REQUIRED", "A durable order journal is required.")
        reconciled: list[PaperOrder] = []
        for row in self._journal.unresolved():
            client_id = row["client_order_id"]
            now = utc_time(self._clock())
            try:
                order = self.order_by_client_id(client_id)
            except PaperBrokerError as exc:
                self._journal.set_status(client_id, "unknown", None, now)
                message = (
                    "Journaled order is not visible at the broker; manual investigation is required."
                    if exc.code == "BROKER_NOT_FOUND"
                    else "Journaled order cannot be checked at the broker; entries halted."
                )
                raise PaperOrderUnknownError("BROKER_STATE_UNKNOWN", message) from exc
            if order.status == "unknown":
                self._journal.set_status(client_id, "unknown", order.broker_order_id, now)
                raise PaperOrderUnknownError(
                    "BROKER_STATE_UNKNOWN", "Broker returned an unknown order status.",
                )
            try:
                journal_status = self._journal_status(
                    order, Decimal(row["stop_price"]),
                )
            except PaperBrokerError as exc:
                self._journal.set_status(client_id, "unknown", order.broker_order_id, now)
                raise PaperOrderUnknownError(
                    "PROTECTIVE_EXIT_UNCONFIRMED",
                    "Broker order state does not confirm protective coverage.",
                ) from exc
            expiry = parse_time(row["expires_at"])
            if cancel_expired and order.status in _ACTIVE_STATUSES and expiry <= now:
                if not self._config.paper_order_submission_enabled:
                    raise PaperBrokerError(
                        "PAPER_SUBMISSION_DISABLED",
                        "Enable paper order writes before canceling an expired entry.",
                    )
                if not self._config.paper_execution_capabilities_verified:
                    raise PaperBrokerError(
                        "PAPER_CAPABILITIES_UNVERIFIED",
                        "Verify paper cancellation and partial-fill behavior before canceling entries.",
                    )
                try:
                    self.cancel_order(order.broker_order_id)
                    order = self.order_by_client_id(client_id)
                except PaperBrokerError as exc:
                    self._journal.set_status(client_id, "unknown", order.broker_order_id, now)
                    raise PaperOrderUnknownError(
                        "BROKER_STATE_UNKNOWN",
                        "Expired paper entry cancellation could not be reconciled.",
                    ) from exc
                if order.status in _ACTIVE_STATUSES:
                    self._journal.set_status(client_id, "unknown", order.broker_order_id, now)
                    raise PaperOrderUnknownError(
                        "ORDER_CANCEL_UNCONFIRMED",
                        "Expired entry cancellation is not yet confirmed.",
                    )
                try:
                    journal_status = self._journal_status(
                        order, Decimal(row["stop_price"]),
                    )
                except PaperBrokerError as exc:
                    self._journal.set_status(client_id, "unknown", order.broker_order_id, now)
                    raise PaperOrderUnknownError(
                        "PROTECTIVE_EXIT_UNCONFIRMED",
                        "Expired partial fill has no confirmed protective stop; entries halted.",
                    ) from exc
            self._journal.set_status(client_id, journal_status, order.broker_order_id, now)
            reconciled.append(order)
        if acknowledge_reconciled:
            self._journal.clear_halt_after_reconciliation()
        return tuple(reconciled)

    def _validate_approval(self, outcome: RiskOutcome, instrument: Instrument) -> OrderIntent:
        decision = outcome.decision
        intent = outcome.intent
        if not decision.allowed or intent is None:
            raise PaperBrokerError("RISK_NOT_APPROVED", "A risk-approved order intent is required.")
        now = utc_time(self._clock())
        if (
            decision.mode is not RuntimeMode.PAPER or intent.mode is not RuntimeMode.PAPER
            or intent.direction is not TradeDirection.LONG
            or intent.instrument_id != instrument.instrument_id
            or intent.correlation_id != decision.correlation_id
            or intent.risk_rule_version != decision.rule_version
            or decision.quantity != intent.quantity
            or decision.notional != intent.quantity * intent.limit_price
            or decision.planned_risk != intent.planned_risk
            or intent.created_at != decision.evaluated_at
            or decision.currency != "USD" or instrument.currency != "USD"
            or instrument.asset_type not in {AssetType.STOCK, AssetType.ETF}
            or instrument.venue not in {"NYSE", "NASDAQ", "ARCA", "AMEX"}
            or instrument.market_calendar not in {"XNYS", "XNAS"}
            or "paper" not in instrument.supported_modes
            or re.fullmatch(r"[A-Z0-9]+(?:[.-][A-Z0-9]+)*", instrument.symbol) is None
        ):
            raise PaperBrokerError("RISK_APPROVAL_INVALID", "Risk approval does not match supported paper order.")
        evaluated = utc_time(decision.evaluated_at)
        if evaluated > now or now - evaluated > timedelta(seconds=30):
            raise PaperBrokerError("RISK_DECISION_STALE", "Risk approval is future-dated or older than 30 seconds.")
        if utc_time(intent.created_at) > now or utc_time(intent.expires_at) <= now:
            raise PaperBrokerError("SIGNAL_EXPIRED", "Paper order intent is expired or future-dated.")
        return intent

    def _account_id(self) -> str:
        value = _object(self._transport.request("GET", "/v2/account"))
        return _text(value.get("id"), "account id")

    @staticmethod
    def _assert_same_order(order: PaperOrder, instrument: Instrument, intent: OrderIntent) -> None:
        if (
            order.client_order_id != intent.idempotency_key
            or order.symbol != instrument.symbol
            or order.side != "buy"
            or order.ordered_quantity != intent.quantity
            or order.limit_price != intent.limit_price
            or (
                order.stop_price is not None
                and order.stop_price != intent.stop_price
            )
        ):
            raise PaperBrokerError(
                "IDEMPOTENCY_CONFLICT",
                "Broker order does not match the approved paper intent.",
            )

    def _journal_status(self, order: PaperOrder, expected_stop: Decimal) -> str:
        if order.status == "unknown":
            raise PaperOrderUnknownError("BROKER_STATE_UNKNOWN", "Broker order status is unknown.")
        leg = next(
            (
                leg for leg in order.legs
                if str(leg.get("type", "")).lower() == "stop"
                and str(leg.get("side", "")).lower() == "sell"
                and _amount(leg.get("stop_price"), "protective stop", positive=True) == expected_stop
            ),
            None,
        )
        if order.filled_quantity == 0:
            if order.status in _ACTIVE_STATUSES and not self._has_protective_stop(
                order, expected_stop, order.ordered_quantity,
            ):
                raise PaperOrderUnknownError(
                    "PROTECTIVE_EXIT_UNCONFIRMED", "Open entry has no attached protective stop.",
                )
            return order.status
        if leg is None:
            raise PaperOrderUnknownError(
                "PROTECTIVE_EXIT_UNCONFIRMED", "Filled entry has no protective stop leg.",
            )
        leg_status = str(leg.get("status", "")).lower()
        leg_quantity = _amount(leg.get("qty"), "protective stop quantity", positive=True)
        leg_filled = _amount(leg.get("filled_qty", "0"), "protective stop fill quantity")
        if leg_filled > leg_quantity or leg_filled > order.filled_quantity:
            raise PaperOrderUnknownError(
                "PROTECTIVE_EXIT_UNCONFIRMED",
                "Protective stop fills exceed the confirmed open-entry quantity.",
            )
        if leg_status == "filled":
            if (
                leg_filled < order.filled_quantity
                or order.status in _ACTIVE_STATUSES
            ):
                raise PaperOrderUnknownError(
                    "PROTECTIVE_EXIT_UNCONFIRMED",
                    "Protective stop does not close the entry or the entry remains active.",
                )
            return "filled"
        if (
            leg_status not in _PROTECTIVE_ACTIVE_STATUSES
            or leg_quantity < order.filled_quantity
        ):
            raise PaperOrderUnknownError(
                "PROTECTIVE_EXIT_UNCONFIRMED", "Protective stop is inactive or undersized.",
            )
        if order.status in _TERMINAL_STATUSES:
            return "protected"
        return order.status

    @staticmethod
    def _has_protective_stop(
        order: PaperOrder, expected_stop: Decimal, required_quantity: Decimal,
    ) -> bool:
        return any(
            isinstance(leg.get("type"), str)
            and leg["type"].lower() == "stop"
            and str(leg.get("side", "")).lower() == "sell"
            and str(leg.get("status", "")).lower() in _ACTIVE_STATUSES
            and _amount(leg.get("stop_price"), "protective stop", positive=True) == expected_stop
            and _amount(leg.get("qty"), "protective stop quantity", positive=True) >= required_quantity
            for leg in order.legs
        )
