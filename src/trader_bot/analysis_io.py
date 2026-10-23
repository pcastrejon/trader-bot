"""Strict local analysis snapshots and model-free replay of recorded policy decisions."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path

from trader_bot.agents import (
    AnalysisDecision, AnalysisPolicy, Evidence, Metric, ModelIdentity, PROMPT_VERSION,
    aggregate, evidence_issue, fingerprint, object_fields, strict_json,
    string_list, text, validate_evidence, validate_result, validate_numeric_claims,
)
from trader_bot.backtest import json_value
from trader_bot.contracts import (
    AgentAnalysisReport, AgentRequest, AgentResult, AgentRole, AgentStatus,
    ContractError, InputReference, Uncertainty,
)
from trader_bot.market_data import DataError, parse_time


def sequence(value: object) -> list[object]:
    if not isinstance(value, list) or len(value) > 64:
        raise ContractError("Expected a bounded analysis list.")
    return value


def boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise ContractError("Expected an analysis boolean.")
    return value


def load_reference(value: object) -> InputReference:
    item = object_fields(value, {"source", "reference", "observed_at"})
    return InputReference(
        text(item["source"], 120), text(item["reference"], 200),
        parse_time(text(item["observed_at"], 100)),
    )


def load_evidence(value: object) -> Evidence:
    item = object_fields(value, {
        "role", "instrument_id", "reference", "received_at", "permission_verified",
        "metrics", "content", "period_ends_at",
    })
    metrics: list[Metric] = []
    for value in sequence(item["metrics"]):
        metric = object_fields(value, {"name", "value", "unit"})
        metrics.append(Metric(
            text(metric["name"], 80), Decimal(text(metric["value"], 80)),
            text(metric["unit"], 40),
        ))
    if not isinstance(item["content"], str):
        raise ContractError("Evidence content must be text.")
    return Evidence(
        AgentRole(text(item["role"])), text(item["instrument_id"], 120),
        load_reference(item["reference"]), parse_time(text(item["received_at"], 100)),
        boolean(item["permission_verified"]), tuple(metrics), item["content"],
        parse_time(text(item["period_ends_at"], 100)) if item["period_ends_at"] is not None else None,
    )


def load_policy(value: object) -> AnalysisPolicy:
    item = object_fields(value, {
        "required_roles", "veto_roles", "timeout_seconds", "technical_max_age_seconds",
        "news_max_age_seconds", "fundamental_max_age_seconds",
    })
    durations: dict[str, int] = {}
    for key in (
        "timeout_seconds", "technical_max_age_seconds",
        "news_max_age_seconds", "fundamental_max_age_seconds",
    ):
        duration = item[key]
        if isinstance(duration, bool) or not isinstance(duration, int):
            raise ContractError("Analysis policy durations must be integer seconds.")
        durations[key] = duration
    return AnalysisPolicy(
        tuple(AgentRole(text(role)) for role in sequence(item["required_roles"])),
        tuple(AgentRole(text(role)) for role in sequence(item["veto_roles"])), **durations,
    )


def read_analysis(path: Path) -> tuple[str, str, tuple[Evidence, ...], AnalysisPolicy, object]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ContractError("Analysis bundle exceeds 1 MB.")
        item = object_fields(strict_json(raw.decode("utf-8")), {
            "schema_version", "correlation_id", "instrument_id", "evidence", "policy",
            "decision", "decision_fingerprint",
        })
        if item["schema_version"] != "1":
            raise ContractError("Unsupported analysis schema version.")
        correlation = text(item["correlation_id"], 200)
        instrument = text(item["instrument_id"], 120)
        evidence = tuple(load_evidence(value) for value in sequence(item["evidence"]))
        validate_evidence(evidence, instrument)
        if item["decision_fingerprint"] != (
            fingerprint(item["decision"]) if item["decision"] is not None else None
        ):
            raise ContractError("Analysis decision fingerprint mismatch.")
        return correlation, instrument, evidence, load_policy(item["policy"]), item["decision"]
    except OSError as exc:
        raise DataError("ANALYSIS_IO", f"Cannot read local analysis: {exc.strerror}.") from exc
    except (ValueError, TypeError, OverflowError, RecursionError, InvalidOperation) as exc:
        raise DataError("ANALYSIS_INVALID", "Malformed local analysis bundle.") from exc


def write_analysis(
    path: Path, correlation_id: str, instrument_id: str, evidence: tuple[Evidence, ...],
    policy: AnalysisPolicy, decision: AnalysisDecision | None = None,
) -> None:
    text(correlation_id, 200)
    text(instrument_id, 120)
    validate_evidence(evidence, instrument_id)
    if decision is not None:
        if (
            decision.report.correlation_id != correlation_id
            or decision.report.instrument_id != instrument_id
            or rebuild_decision(decision.report, evidence, policy) != decision
        ):
            raise ContractError("Cannot archive a mismatched analysis decision.")
    payload = {
        "schema_version": "1", "correlation_id": correlation_id,
        "instrument_id": instrument_id, "evidence": json_value(evidence),
        "policy": json_value(policy), "decision": json_value(decision),
        "decision_fingerprint": fingerprint(decision) if decision is not None else None,
    }
    raw = json.dumps(payload, sort_keys=True, indent=2, allow_nan=False) + "\n"
    if len(raw.encode("utf-8")) > 1_000_000:
        raise DataError("ANALYSIS_INVALID", "Analysis bundle exceeds 1 MB.")
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(raw)
    except OSError as exc:
        raise DataError("ANALYSIS_IO", f"Cannot create local analysis: {exc.strerror}.") from exc


def load_result(value: object) -> AgentResult:
    item = object_fields(value, {
        "schema_version", "correlation_id", "role", "status", "instrument_id", "created_at",
        "model_provider", "model_name", "model_version", "input_refs", "summary",
        "uncertainty", "veto", "insufficient_data", "warnings", "source_refs",
    })
    identity = ModelIdentity(
        text(item["model_provider"], 200), text(item["model_name"], 200),
        text(item["model_version"], 200),
    )
    return AgentResult(
        text(item["schema_version"]), text(item["correlation_id"]),
        AgentRole(text(item["role"])), AgentStatus(text(item["status"])),
        text(item["instrument_id"]), parse_time(text(item["created_at"], 100)),
        identity.provider, identity.name, identity.version,
        tuple(load_reference(value) for value in sequence(item["input_refs"])),
        text(item["summary"], 12000), Uncertainty(text(item["uncertainty"])),
        boolean(item["veto"]), boolean(item["insufficient_data"]),
        string_list(item["warnings"], 12, 300), string_list(item["source_refs"], 32, 200),
    )


def rebuild_decision(
    report: AgentAnalysisReport, evidence: tuple[Evidence, ...], policy: AnalysisPolicy,
) -> AnalysisDecision:
    for result in report.results:
        selected = tuple(value for value in evidence if value.role is result.role)
        request = AgentRequest(
            "1", report.correlation_id, result.role, report.instrument_id, report.created_at,
            report.created_at + timedelta(seconds=policy.timeout_seconds),
            tuple(value.reference for value in selected),
        )
        validate_result(result, request)
        validate_numeric_claims(result, selected)
        issue = evidence_issue(selected, request, policy)
        if issue is not None and (result.status, result.summary) != issue:
            raise ContractError("Recorded outcome contradicts evidence validation.")
    return aggregate(report, policy, fingerprint(evidence))


def replay_analysis(path: Path) -> AnalysisDecision:
    correlation, instrument, evidence, policy, expected = read_analysis(path)
    try:
        item = object_fields(expected, {
            "report", "policy", "can_continue", "reason_codes", "input_fingerprint",
            "source_fingerprint", "prompt_version",
        })
        report_data = object_fields(item["report"], {
            "correlation_id", "instrument_id", "created_at", "results",
        })
        created = parse_time(text(report_data["created_at"], 100))
        if report_data["correlation_id"] != correlation or report_data["instrument_id"] != instrument:
            raise ContractError("Recorded report identity does not match inputs.")
        report = AgentAnalysisReport(
            correlation, instrument, created,
            tuple(load_result(value) for value in sequence(report_data["results"])),
        )
        actual = rebuild_decision(report, evidence, policy)
        if item["prompt_version"] != PROMPT_VERSION or json_value(actual) != expected:
            raise ContractError("Recorded policy decision does not match replay.")
        return actual
    except (ValueError, TypeError, OverflowError, InvalidOperation) as exc:
        raise DataError("ANALYSIS_REPLAY_MISMATCH", "Recorded analysis failed validation/replay.") from exc
