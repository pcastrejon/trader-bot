"""Phase 0 configuration-check command."""

from __future__ import annotations

import argparse
import sys
from decimal import InvalidOperation

from trader_bot.config import ConfigurationError, RuntimeConfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trader-bot")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("config-check", help="validate local runtime configuration")
    args = parser.parse_args(argv)

    if args.command == "config-check":
        try:
            config = RuntimeConfig.from_env()
        except (ConfigurationError, InvalidOperation) as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
        print(f"Configuration valid (mode={config.mode.value}).")
        print(f"Watchlist instruments: {len(config.watchlist)}")
        print(f"Monthly cost cap: {config.monthly_cost_cap}")
        print(f"Local model enabled: {config.local_model_enabled}")
        if config.mode.value == "paper":
            print("Paper credentials: configured (values redacted)")
        return 0

    parser.error("Unsupported command.")
    return 2
