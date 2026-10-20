"""Validated runtime configuration with fail-closed mode separation."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Mapping

from trader_bot.contracts import RuntimeMode


class ConfigurationError(ValueError):
    """Raised when runtime configuration is invalid or incomplete."""


class LiveModeDisabledError(ConfigurationError):
    """Raised when the unimplemented live execution mode is requested."""


@dataclass(frozen=True, slots=True)
class PaperCredentials:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.api_key.strip() or not self.api_secret.strip():
            raise ConfigurationError("Paper credentials must not be empty.")

    def __repr__(self) -> str:
        return "PaperCredentials(api_key=<redacted>, api_secret=<redacted>)"


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    mode: RuntimeMode = RuntimeMode.BACKTEST
    watchlist: tuple[str, ...] = ()
    monthly_cost_cap: Decimal = Decimal("0")
    local_model_enabled: bool = False
    paper_allow_shorting: bool = False
    paper_allow_leverage: bool = False
    paper_credentials: PaperCredentials | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.mode, RuntimeMode):
            raise ConfigurationError("mode must be a RuntimeMode value.")
        if self.mode is RuntimeMode.LIVE:
            raise LiveModeDisabledError(
                "Live mode is not implemented. Use backtest or paper mode."
            )
        if not isinstance(self.monthly_cost_cap, Decimal):
            raise ConfigurationError("Monthly cost cap must be a Decimal amount.")
        if not self.monthly_cost_cap.is_finite() or self.monthly_cost_cap < 0:
            raise ConfigurationError(
                "Monthly cost cap must be a finite, non-negative amount."
            )
        if not isinstance(self.watchlist, tuple) or any(
            not isinstance(symbol, str) for symbol in self.watchlist
        ):
            raise ConfigurationError("Watchlist must be a tuple of symbols.")
        if not isinstance(self.local_model_enabled, bool):
            raise ConfigurationError("local_model_enabled must be a boolean.")
        if not isinstance(self.paper_allow_shorting, bool) or not isinstance(
            self.paper_allow_leverage, bool
        ):
            raise ConfigurationError("Paper trading feature flags must be booleans.")
        if len(set(self.watchlist)) != len(self.watchlist):
            raise ConfigurationError("Watchlist entries must be unique.")
        if any(not symbol or symbol != symbol.strip() for symbol in self.watchlist):
            raise ConfigurationError(
                "Watchlist symbols must be non-empty and have no surrounding whitespace."
            )
        if self.mode is RuntimeMode.PAPER and self.paper_credentials is None:
            raise ConfigurationError(
                "Paper mode requires TRADER_BOT_PAPER_API_KEY and "
                "TRADER_BOT_PAPER_API_SECRET."
            )
        if self.mode is RuntimeMode.BACKTEST and self.paper_credentials is not None:
            raise ConfigurationError(
                "Backtest mode must not be configured with broker credentials."
            )
        if self.paper_credentials is not None and not isinstance(
            self.paper_credentials, PaperCredentials
        ):
            raise ConfigurationError(
                "paper_credentials must be a PaperCredentials value."
            )
        if self.mode is not RuntimeMode.PAPER and (
            self.paper_allow_shorting or self.paper_allow_leverage
        ):
            raise ConfigurationError(
                "Paper-only shorting and leverage flags require paper mode."
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RuntimeConfig:
        """Build configuration from environment variables without reading live secrets."""
        values = os.environ if env is None else env
        raw_mode = values.get("TRADER_BOT_MODE", RuntimeMode.BACKTEST.value).strip().lower()
        try:
            mode = RuntimeMode(raw_mode)
        except ValueError as exc:
            supported = ", ".join(item.value for item in RuntimeMode)
            raise ConfigurationError(
                f"Unsupported mode {raw_mode!r}; choose one of: {supported}."
            ) from exc

        if mode is RuntimeMode.LIVE:
            raise LiveModeDisabledError(
                "Live mode is not implemented. Live credentials are not read."
            )

        try:
            cost_cap = Decimal(values.get("TRADER_BOT_MONTHLY_COST_CAP", "0"))
        except InvalidOperation as exc:
            raise ConfigurationError(
                "TRADER_BOT_MONTHLY_COST_CAP must be a decimal amount."
            ) from exc

        symbols = tuple(
            symbol.strip().upper()
            for symbol in values.get("TRADER_BOT_WATCHLIST", "").split(",")
            if symbol.strip()
        )
        local_model_enabled = _parse_bool(
            values.get("TRADER_BOT_LOCAL_MODEL_ENABLED", "false"),
            "TRADER_BOT_LOCAL_MODEL_ENABLED",
        )
        allow_shorting = _parse_bool(
            values.get("TRADER_BOT_PAPER_ALLOW_SHORTING", "false"),
            "TRADER_BOT_PAPER_ALLOW_SHORTING",
        )
        allow_leverage = _parse_bool(
            values.get("TRADER_BOT_PAPER_ALLOW_LEVERAGE", "false"),
            "TRADER_BOT_PAPER_ALLOW_LEVERAGE",
        )

        credentials = None
        if mode is RuntimeMode.PAPER:
            api_key = values.get("TRADER_BOT_PAPER_API_KEY", "")
            api_secret = values.get("TRADER_BOT_PAPER_API_SECRET", "")
            if not api_key.strip() or not api_secret.strip():
                raise ConfigurationError(
                    "Paper mode requires non-empty TRADER_BOT_PAPER_API_KEY and "
                    "TRADER_BOT_PAPER_API_SECRET."
                )
            credentials = PaperCredentials(api_key=api_key, api_secret=api_secret)

        return cls(
            mode=mode,
            watchlist=symbols,
            monthly_cost_cap=cost_cap,
            local_model_enabled=local_model_enabled,
            paper_allow_shorting=allow_shorting,
            paper_allow_leverage=allow_leverage,
            paper_credentials=credentials,
        )


def _parse_bool(value: str, name: str) -> bool:
    normalized = value.lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ConfigurationError(f"{name} must be 'true' or 'false'.")
