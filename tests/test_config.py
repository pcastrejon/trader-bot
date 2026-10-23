from decimal import Decimal
import unittest

from trader_bot.config import (
    ConfigurationError,
    LiveModeDisabledError,
    RuntimeConfig,
    RuntimeMode,
)


class RuntimeConfigTests(unittest.TestCase):
    def test_defaults_to_backtest_without_credentials(self) -> None:
        config = RuntimeConfig.from_env({})

        self.assertEqual(config.mode, RuntimeMode.BACKTEST)
        self.assertEqual(config.monthly_cost_cap, Decimal("0"))
        self.assertIsNone(config.paper_credentials)

    def test_backtest_ignores_broker_credentials(self) -> None:
        config = RuntimeConfig.from_env(
            {
                "TRADER_BOT_MODE": "backtest",
                "TRADER_BOT_PAPER_API_KEY": "paper-key",
                "TRADER_BOT_PAPER_API_SECRET": "paper-secret",
                "APCA_API_KEY_ID": "live-key-must-not-be-read",
                "APCA_API_SECRET_KEY": "live-secret-must-not-be-read",
            }
        )

        self.assertIsNone(config.paper_credentials)

    def test_live_mode_is_refused_before_credentials_are_read(self) -> None:
        with self.assertRaises(LiveModeDisabledError):
            RuntimeConfig.from_env(
                {
                    "TRADER_BOT_MODE": "live",
                    "TRADER_BOT_PAPER_API_KEY": "should-not-be-read",
                    "TRADER_BOT_PAPER_API_SECRET": "should-not-be-read",
                }
            )

    def test_paper_mode_requires_paper_credentials(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "TRADER_BOT_PAPER_API_KEY"):
            RuntimeConfig.from_env({"TRADER_BOT_MODE": "paper"})

    def test_paper_mode_loads_only_namespaced_paper_credentials(self) -> None:
        config = RuntimeConfig.from_env(
            {
                "TRADER_BOT_MODE": "paper",
                "TRADER_BOT_PAPER_API_KEY": "paper-key",
                "TRADER_BOT_PAPER_API_SECRET": "paper-secret",
                "APCA_API_KEY_ID": "unrecognized-key",
                "APCA_API_SECRET_KEY": "unrecognized-secret",
            }
        )

        self.assertEqual(config.mode, RuntimeMode.PAPER)
        self.assertNotIn("paper-key", repr(config))
        self.assertNotIn("paper-secret", repr(config))
        self.assertNotIn("unrecognized", repr(config))

    def test_watchlist_is_normalized(self) -> None:
        config = RuntimeConfig.from_env(
            {
                "TRADER_BOT_WATCHLIST": "aapl, msft",
            }
        )

        self.assertEqual(config.watchlist, ("AAPL", "MSFT"))

    def test_rejects_invalid_cost_and_boolean_values(self) -> None:
        with self.assertRaises(ConfigurationError):
            RuntimeConfig.from_env({"TRADER_BOT_MONTHLY_COST_CAP": "-1"})
        with self.assertRaises(ConfigurationError):
            RuntimeConfig.from_env({"TRADER_BOT_LOCAL_MODEL_ENABLED": "yes"})

    def test_paper_only_features_cannot_be_enabled_in_backtest(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "require paper mode"):
            RuntimeConfig.from_env(
                {"TRADER_BOT_PAPER_ALLOW_SHORTING": "true"}
            )

    def test_paper_order_submission_is_opt_in_and_paper_only(self) -> None:
        self.assertFalse(RuntimeConfig.from_env({}).paper_order_submission_enabled)
        config = RuntimeConfig.from_env(
            {
                "TRADER_BOT_MODE": "paper",
                "TRADER_BOT_PAPER_API_KEY": "paper-key",
                "TRADER_BOT_PAPER_API_SECRET": "paper-secret",
                "TRADER_BOT_PAPER_ORDER_SUBMISSION_ENABLED": "true",
                "TRADER_BOT_PAPER_EXECUTION_CAPABILITIES_VERIFIED": "true",
            }
        )
        self.assertTrue(config.paper_order_submission_enabled)
        self.assertTrue(config.paper_execution_capabilities_verified)
        with self.assertRaisesRegex(ConfigurationError, "require paper mode"):
            RuntimeConfig.from_env(
                {"TRADER_BOT_PAPER_ORDER_SUBMISSION_ENABLED": "true"}
            )


if __name__ == "__main__":
    unittest.main()
