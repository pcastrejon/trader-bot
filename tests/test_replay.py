import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from trader_bot.backtest import BacktestRunner, json_value
from trader_bot.cli import main
from trader_bot.config import PaperCredentials
from trader_bot.market_data import DataError
from trader_bot.replay import read_bundle, replay_bundle, write_bundle
from tests.support import RUNTIME
from tests.test_backtest import research_data


class ReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = research_data()
        self.report = BacktestRunner().run(self.data)

    def test_bundle_round_trip_and_exact_replay(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "recorded.json"
            write_bundle(path, self.data, self.report)
            recovered, expected = read_bundle(path)
            self.assertEqual(recovered, self.data)
            self.assertEqual(expected, json_value(self.report))
            self.assertEqual(replay_bundle(path), self.report)
            self.assertEqual(len(self.report.order_intents), 1)

    def test_input_only_bundle_runs_but_cannot_verify_without_report(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "input.json"
            write_bundle(path, self.data)
            recovered, expected = read_bundle(path)
            self.assertEqual(recovered, self.data)
            self.assertIsNone(expected)
            with self.assertRaises(DataError) as error:
                replay_bundle(path)
            self.assertEqual(error.exception.code, "REPLAY_INVALID")

    def test_replay_detects_tampered_metrics_and_data(self) -> None:
        for field in ("report", "input"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "recorded.json"
                write_bundle(path, self.data, self.report)
                payload = json.loads(path.read_text())
                if field == "report":
                    payload["report"]["final_equity"] = "999999"
                else:
                    payload["input"]["initial_equity"] = "20000"
                path.write_text(json.dumps(payload))
                with self.assertRaises(DataError) as error:
                    replay_bundle(path)
                self.assertEqual(error.exception.code, "REPLAY_MISMATCH")

    def test_source_change_prevents_claim_of_identical_replay(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "recorded.json"
            write_bundle(path, self.data, self.report)
            with patch("trader_bot.backtest.source_version", return_value="different-source"):
                with self.assertRaises(DataError) as error:
                    replay_bundle(path)
            self.assertEqual(error.exception.code, "REPLAY_MISMATCH")

    def test_existing_file_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "recorded.json"
            path.write_text("keep this")
            with self.assertRaises(DataError) as error:
                write_bundle(path, self.data, self.report)
            self.assertEqual(error.exception.code, "REPLAY_IO")
            self.assertEqual(path.read_text(), "keep this")

    def test_schema_unknown_fields_duplicates_and_bad_json_rejected(self) -> None:
        for content in (
            "{", "[]",
            '{"schema_version":"1","schema_version":"2","input":{},"report":null}',
            '{"schema_version":"999","input":{},"report":null}',
            '{"schema_version":"1","input":{},"report":null,"execute":true}',
        ):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "bad.json"
                path.write_text(content)
                with self.assertRaises(DataError):
                    read_bundle(path)

    def test_domain_validation_still_applies_after_deserialization(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad-data.json"
            write_bundle(path, self.data)
            payload = json.loads(path.read_text())
            payload["input"]["bars"][0]["close"] = "NaN"
            path.write_text(json.dumps(payload))
            with self.assertRaises(DataError):
                read_bundle(path)

    def test_secret_bearing_objects_cannot_be_serialized(self) -> None:
        for secret in (
            replace(RUNTIME, local_model_enabled=True), PaperCredentials("private-key", "private-secret"),
        ):
            with self.subTest(value=type(secret).__name__), self.assertRaises(TypeError):
                json_value(secret)

    def test_missing_path_is_explicit_io_error(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(DataError) as error:
                read_bundle(Path(folder) / "absent.json")
            self.assertEqual(error.exception.code, "REPLAY_IO")

    def test_offline_cli_backtest_then_replay_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            original = Path(folder) / "input.json"
            recorded = Path(folder) / "result.json"
            write_bundle(original, self.data)
            output = io.StringIO()
            with patch.dict("os.environ", {}, clear=True), redirect_stdout(output):
                with patch("urllib.request.OpenerDirector.open") as network:
                    self.assertEqual(main([
                        "backtest", "--input", str(original), "--output", str(recorded),
                    ]), 0)
                    self.assertEqual(main(["replay", "--input", str(recorded)]), 0)
            self.assertIn("Complete: True", output.getvalue())
            self.assertIn("Research simulation only", output.getvalue())
            network.assert_not_called()

    def test_cli_returns_nonzero_for_unresolved_position_and_records_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            original = Path(folder) / "input.json"
            recorded = Path(folder) / "result.json"
            write_bundle(original, research_data(stop=False))
            with patch.dict("os.environ", {}, clear=True), redirect_stdout(io.StringIO()):
                self.assertEqual(main([
                    "backtest", "--input", str(original), "--output", str(recorded),
                ]), 2)
            self.assertTrue(recorded.exists())
            _, result = read_bundle(recorded)
            self.assertIsInstance(result, dict)

    def test_cli_refuses_live_mode_and_missing_files(self) -> None:
        output = io.StringIO()
        with patch.dict("os.environ", {"TRADER_BOT_MODE": "live"}, clear=True):
            with redirect_stderr(output):
                self.assertEqual(main(["replay", "--input", "never-read.json"]), 2)
        self.assertIn("Live mode", output.getvalue())
        with patch.dict("os.environ", {}, clear=True), redirect_stderr(output):
            self.assertEqual(main(["replay", "--input", "never-read.json"]), 2)

    def test_report_decimal_and_time_values_are_json_safe(self) -> None:
        value = json_value(self.report)
        encoded = json.dumps(value, allow_nan=False)
        self.assertIn('"final_equity": "9960', encoded)
        self.assertIn("source_fingerprint", encoded)
