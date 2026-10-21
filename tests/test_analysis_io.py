import asyncio
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from trader_bot.agents import (
    AgentOrchestrator, AnalysisPolicy, DisabledModel, fingerprint, local_agents,
)
from trader_bot.analysis_io import read_analysis, replay_analysis, write_analysis
from trader_bot.cli import main
from trader_bot.config import RuntimeConfig
from trader_bot.contracts import ContractError
from trader_bot.market_data import DataError
from tests.agent_support import DIGEST, NOW, FixtureModel, evidence


class AnalysisBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = FixtureModel()
        self.evidence = evidence()
        self.policy = AnalysisPolicy()
        self.decision = asyncio.run(AgentOrchestrator(
            local_agents(self.model), self.policy, clock=lambda: NOW,
        ).analyze("corr", "US:DEMO", self.evidence))
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "analysis.json"

    def write(self) -> None:
        write_analysis(self.path, "corr", "US:DEMO", self.evidence, self.policy, self.decision)

    def test_round_trip_and_model_free_replay(self) -> None:
        self.write()
        correlation, instrument, records, policy, _ = read_analysis(self.path)
        self.assertEqual((correlation, instrument), ("corr", "US:DEMO"))
        self.assertEqual(records, self.evidence)
        self.assertEqual(policy, self.policy)
        with patch.object(self.model, "complete", side_effect=AssertionError("No inference")):
            self.assertEqual(replay_analysis(self.path), self.decision)

    def test_input_only_bundle_cannot_claim_replay(self) -> None:
        write_analysis(self.path, "corr", "US:DEMO", self.evidence, self.policy)
        self.assertIsNone(read_analysis(self.path)[4])
        with self.assertRaises(DataError):
            replay_analysis(self.path)

    def test_all_failure_outcomes_can_replay(self) -> None:
        records = self.evidence[:2]
        decision = asyncio.run(AgentOrchestrator(
            local_agents(DisabledModel()), clock=lambda: NOW,
        ).analyze("corr", "US:DEMO", records))
        write_analysis(self.path, "corr", "US:DEMO", records, self.policy, decision)
        self.assertEqual(replay_analysis(self.path), decision)
        self.assertFalse(decision.can_continue)

    def test_tampering_with_inputs_policy_outputs_or_metadata_is_detected(self) -> None:
        self.write()
        original = self.path.read_text()
        for field in ("evidence", "policy", "summary", "correlation", "prompt", "source", "continue"):
            with self.subTest(field=field):
                payload = json.loads(original)
                if field == "evidence":
                    payload["evidence"][0]["metrics"][0]["value"] = "200"
                elif field == "policy":
                    payload["policy"]["required_roles"] = []
                elif field == "summary":
                    payload["decision"]["report"]["results"][0]["summary"] = "Tampered."
                elif field == "correlation":
                    payload["correlation_id"] = "other"
                elif field == "prompt":
                    payload["decision"]["prompt_version"] = "other"
                elif field == "source":
                    payload["decision"]["source_fingerprint"] = "other"
                else:
                    payload["decision"]["can_continue"] = False
                self.path.write_text(json.dumps(payload))
                with self.assertRaises(DataError):
                    replay_analysis(self.path)

    def test_source_changes_invalidate_replay(self) -> None:
        self.write()
        with patch("trader_bot.agents.source_version", return_value="changed"):
            with self.assertRaises(DataError):
                replay_analysis(self.path)

    def test_fingerprint_is_not_a_substitute_for_semantic_validation(self) -> None:
        self.write()
        payload = json.loads(self.path.read_text())
        payload["decision"]["report"]["results"][0]["source_refs"] = ["unknown"]
        payload["decision_fingerprint"] = fingerprint(payload["decision"])
        self.path.write_text(json.dumps(payload))
        with self.assertRaises(DataError):
            replay_analysis(self.path)

    def test_recorded_success_with_invalid_evidence_is_rejected(self) -> None:
        self.write()
        payload = json.loads(self.path.read_text())
        payload["evidence"][0]["permission_verified"] = False
        payload["decision"]["input_fingerprint"] = fingerprint(payload["evidence"])
        payload["decision_fingerprint"] = fingerprint(payload["decision"])
        self.path.write_text(json.dumps(payload))
        with self.assertRaises(DataError):
            replay_analysis(self.path)

    def test_existing_file_and_symlink_not_overwritten(self) -> None:
        self.path.write_text("keep")
        with self.assertRaises(DataError):
            self.write()
        link = self.path.parent / "link.json"
        link.symlink_to(self.path)
        with self.assertRaises(DataError):
            write_analysis(link, "corr", "US:DEMO", self.evidence, self.policy)
        self.assertEqual(self.path.read_text(), "keep")

    def test_strict_schemas_types_duplicate_keys_and_limits(self) -> None:
        for raw in (
            "{", "[]", '{"schema_version":"1","schema_version":"1"}',
            '{"unexpected":true}', '{"value":NaN}', "\x00", "x" * 1000001,
        ):
            with self.subTest(raw=raw[:50]):
                self.path.write_text(raw)
                with self.assertRaises(DataError):
                    read_analysis(self.path)

    def test_invalid_evidence_fields_and_policies_rejected(self) -> None:
        self.write()
        original = json.loads(self.path.read_text())
        for field in ("role", "decimal", "permission", "duration", "version", "unknown"):
            with self.subTest(field=field):
                payload = json.loads(json.dumps(original))
                if field == "role":
                    payload["evidence"][0]["role"] = "execution"
                elif field == "decimal":
                    payload["evidence"][0]["metrics"][0]["value"] = "NaN"
                elif field == "permission":
                    payload["evidence"][0]["permission_verified"] = "true"
                elif field == "duration":
                    payload["policy"]["timeout_seconds"] = True
                elif field == "version":
                    payload["schema_version"] = "2"
                else:
                    payload["evidence"][0]["order"] = "BUY"
                self.path.write_text(json.dumps(payload))
                with self.assertRaises(DataError):
                    read_analysis(self.path)

    def test_missing_input_error_is_explicit(self) -> None:
        with self.assertRaises(DataError) as error:
            read_analysis(self.path)
        self.assertEqual(error.exception.code, "ANALYSIS_IO")

    def test_invalid_decimal_literal_is_explicit_data_error(self) -> None:
        self.write()
        payload = json.loads(self.path.read_text())
        payload["evidence"][0]["metrics"][0]["value"] = "not-a-number"
        self.path.write_text(json.dumps(payload))
        with self.assertRaises(DataError) as error:
            read_analysis(self.path)
        self.assertEqual(error.exception.code, "ANALYSIS_INVALID")

    def test_writer_refuses_mismatched_decision_before_creating_file(self) -> None:
        with self.assertRaises(ContractError):
            write_analysis(self.path, "corr", "US:DEMO", self.evidence, self.policy,
                           replace(self.decision, can_continue=False))
        self.assertFalse(self.path.exists())

    def test_disabled_cli_records_hold_and_replays_without_network(self) -> None:
        write_analysis(self.path, "corr", "US:DEMO", self.evidence, self.policy)
        output = self.path.parent / "result.json"
        with (
            patch("trader_bot.cli.RuntimeConfig.from_env", return_value=RuntimeConfig()),
            patch("trader_bot.local_model.http.client.HTTPConnection") as connection,
            redirect_stdout(io.StringIO()) as stdout,
            redirect_stderr(io.StringIO()),
        ):
            code = main(["analyze-agents", "--input", str(self.path), "--output", str(output),
                         "--as-of", NOW.isoformat()])
            self.assertEqual(code, 2)
            self.assertEqual(main(["replay-analysis", "--input", str(output)]), 2)
            connection.assert_not_called()
        self.assertIn("AGENT_UNAVAILABLE", stdout.getvalue())
        self.assertTrue(output.exists())

    def test_enabled_cli_with_fixture_model_and_model_free_replay(self) -> None:
        write_analysis(self.path, "corr", "US:DEMO", self.evidence, self.policy)
        output = self.path.parent / "result.json"
        with (
            patch("trader_bot.cli.RuntimeConfig.from_env",
                  return_value=RuntimeConfig(local_model_enabled=True)),
            patch("trader_bot.cli.OllamaLocalModel", return_value=self.model) as model,
            redirect_stdout(io.StringIO()) as stdout,
            redirect_stderr(io.StringIO()),
        ):
            args = ["analyze-agents", "--input", str(self.path), "--output", str(output),
                    "--as-of", NOW.isoformat(), "--model", "demo:latest", "--model-version", DIGEST]
            self.assertEqual(main(args), 0)
            self.assertEqual(main(["replay-analysis", "--input", str(output)]), 0)
            self.assertEqual(model.call_count, 1)
            self.assertEqual(main(args), 2)
        self.assertIn("not a signal", stdout.getvalue())

    def test_enabled_cli_requires_model_and_pinned_version(self) -> None:
        write_analysis(self.path, "corr", "US:DEMO", self.evidence, self.policy)
        with (
            patch("trader_bot.cli.RuntimeConfig.from_env",
                  return_value=RuntimeConfig(local_model_enabled=True)),
            redirect_stderr(io.StringIO()) as stderr,
        ):
            self.assertEqual(main([
                "analyze-agents", "--input", str(self.path),
                "--output", str(self.path.parent / "out.json"), "--as-of", NOW.isoformat(),
            ]), 2)
        self.assertIn("--model", stderr.getvalue())

    def test_cli_refuses_live_mode_before_inference(self) -> None:
        with (
            patch.dict("os.environ", {"TRADER_BOT_MODE": "live"}, clear=True),
            patch("trader_bot.local_model.http.client.HTTPConnection") as connection,
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(["replay-analysis", "--input", str(self.path)]), 2)
            connection.assert_not_called()
