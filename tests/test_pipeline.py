import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from decimal import Decimal
from datetime import timedelta
from unittest.mock import Mock, patch

from trader_bot.cli import main
from trader_bot.market_data import DataError, MarketDataClient, UsagePolicy
from trader_bot.pipeline import ResearchPipeline
from trader_bot.strategy import BenchmarkStrategy, EvaluationStatus
from tests.support import FIXTURE, INSTRUMENT, NOW, OPEN, POLICY, RUNTIME, SESSION, snapshot


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = Mock()
        self.provider.fetch.return_value = snapshot()
        self.client = MarketDataClient(self.provider, UsagePolicy(2))
        self.pipeline = ResearchPipeline(
            self.client, BenchmarkStrategy(), RUNTIME, POLICY, (SESSION,),
        )

    def test_end_to_end_fixture_to_candidate(self) -> None:
        result = self.pipeline.evaluate(INSTRUMENT, OPEN, NOW, "case-1")
        self.assertEqual(result.status, EvaluationStatus.SIGNAL)
        self.assertEqual(self.provider.fetch.call_count, 1)

    def test_unsupported_instrument_never_calls_provider(self) -> None:
        result = self.pipeline.evaluate(replace(INSTRUMENT, venue="OTC"), OPEN, NOW, "case-1")
        self.assertEqual(result.code, "INSTRUMENT_UNSUPPORTED")
        self.provider.fetch.assert_not_called()

    def test_provider_error_is_explicit_rejection(self) -> None:
        self.provider.fetch.side_effect = DataError("PROVIDER_UNAVAILABLE", "offline")
        result = self.pipeline.evaluate(INSTRUMENT, OPEN, NOW, "case-1")
        self.assertEqual(result.status, EvaluationStatus.REJECTED)
        self.assertEqual(result.code, "PROVIDER_UNAVAILABLE")

    def test_paid_budget_cannot_override_zero_runtime_cap(self) -> None:
        client = MarketDataClient(self.provider, UsagePolicy(2, monthly_cost_cap=Decimal(1)))
        result = replace(self.pipeline, client=client).evaluate(INSTRUMENT, OPEN, NOW, "case-1")
        self.assertEqual(result.code, "COST_CAP")
        self.provider.fetch.assert_not_called()

    def test_cached_data_is_revalidated_not_assumed_fresh(self) -> None:
        result = self.pipeline.evaluate(INSTRUMENT, OPEN, NOW, "first")
        self.assertEqual(result.status, EvaluationStatus.SIGNAL)
        # Cache returns the same snapshot, but strategy must recheck freshness.
        later = NOW + timedelta(seconds=31)
        with patch.object(self.client, "fetch", return_value=snapshot()):
            stale = self.pipeline.evaluate(INSTRUMENT, OPEN, later, "later")
        self.assertEqual(stale.code, "DATA_STALE")

    def test_network_receipt_time_uses_explicit_evaluation_clock(self) -> None:
        later = NOW + timedelta(seconds=1)
        self.provider.fetch.return_value = replace(
            snapshot(), received_at=later, quote=replace(snapshot().quote, received_at=later)
        )
        pipeline = replace(self.pipeline, clock=lambda: later)
        result = pipeline.evaluate(INSTRUMENT, OPEN, NOW, "network")
        self.assertEqual(result.status, EvaluationStatus.SIGNAL)

    def test_provider_bars_outside_query_are_rejected(self) -> None:
        result = self.pipeline.evaluate(INSTRUMENT, OPEN + timedelta(minutes=5), NOW, "range")
        self.assertEqual(result.code, "DATA_INVALID")
        self.assertIsNone(result.candidate)


class ResearchCliTests(unittest.TestCase):
    def arguments(self) -> list[str]:
        return [
            "analyze-csv", "--bars", str(FIXTURE), "--symbol", "DEMO",
            "--venue", "NASDAQ", "--calendar", "XNAS",
            "--session-open", OPEN.isoformat(),
            "--session-close", SESSION.closes_at.isoformat(),
            "--as-of", NOW.isoformat(), "--quote-at", NOW.isoformat(),
            "--bid", "110", "--ask", "110.02", "--min-price", "5",
            "--min-volume", "1000", "--max-spread-bps", "20",
            "--max-quote-age-seconds", "30", "--max-bar-age-seconds", "60",
            "--verified",
        ]

    def test_offline_command_reports_candidate_without_network(self) -> None:
        output = io.StringIO()
        with patch.dict("os.environ", {"TRADER_BOT_WATCHLIST": "DEMO"}, clear=True):
            with redirect_stdout(output), patch("urllib.request.OpenerDirector.open") as network:
                result = main(self.arguments())
        self.assertEqual(result, 0)
        self.assertIn("signal: CROSSOVER", output.getvalue())
        self.assertIn("Research only", output.getvalue())
        network.assert_not_called()

    def test_missing_watchlist_reports_rejection_and_nonzero_exit(self) -> None:
        output = io.StringIO()
        with patch.dict("os.environ", {}, clear=True), redirect_stdout(output):
            result = main(self.arguments())
        self.assertEqual(result, 2)
        self.assertIn("NOT_WATCHLISTED", output.getvalue())

    def test_bad_input_is_visible_without_traceback(self) -> None:
        args = self.arguments()
        args[args.index("--bid") + 1] = "NaN"
        output = io.StringIO()
        with patch.dict("os.environ", {}, clear=True), redirect_stderr(output):
            result = main(args)
        self.assertEqual(result, 2)
        self.assertIn("Research configuration/data error", output.getvalue())
