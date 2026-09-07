"""Offline readiness audit for the remaining Stage-0/Stage-4 experiments."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_PATH = PROJECT_ROOT / "week9" / "configs" / "experiment_protocol.json"
RESULTS_ROOT = PROJECT_ROOT / "week9" / "results"


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _latest_report(pattern: str, filename: str) -> Path | None:
    candidates = sorted(RESULTS_ROOT.glob(f"{pattern}/{filename}"))
    return candidates[-1] if candidates else None


def _report_integrity(path: Path | None) -> tuple[bool, str, str]:
    if path is None:
        return False, "", "report_not_found"
    sidecar = path.with_suffix(".sha256")
    try:
        expected = sidecar.read_text(encoding="ascii").strip().split()[0]
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, IndexError) as exc:
        return False, str(path), f"sidecar_error:{type(exc).__name__}"
    if expected != actual:
        return False, str(path), "sha256_mismatch"
    return True, str(path), "sha256_verified"


def _semantic_report_status(
    path: Path | None,
    *,
    schema_version: str,
    verdict: str,
    guard_expectations: Mapping[str, Any],
) -> tuple[bool, str]:
    if path is None:
        return False, "report_not_found"
    try:
        report = _read_object(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return False, f"report_parse_error:{type(exc).__name__}"
    if report.get("schema_version") != schema_version:
        return False, "schema_mismatch"
    if report.get("verdict") != verdict:
        return False, "verdict_not_pass"
    guard = report.get("guard", report.get("offline_guard", {}))
    if not isinstance(guard, dict) or any(
        guard.get(key) != value for key, value in guard_expectations.items()
    ):
        return False, "guard_mismatch"
    conditions = report.get("pass_conditions")
    if conditions is not None and (
        not isinstance(conditions, dict)
        or not conditions
        or any(value is not True for value in conditions.values())
    ):
        return False, "pass_conditions_not_met"
    if schema_version == "v8-contemporaneous-pilot-1":
        records = report.get("records")
        repetitions = report.get("repetitions")
        planned = report.get("planned_run_count")
        completed = report.get("completed_run_count")
        if (
            type(repetitions) is not int
            or repetitions < 3
            or not isinstance(records, list)
            or planned != completed
            or completed != len(records)
            or guard.get("versions") != ["v4", "v6"]
            or any(item.get("version") not in {"v4", "v6"} for item in records)
        ):
            return False, "pilot_scope_or_count_mismatch"
    if schema_version == "v8-judge-stability-1":
        artifacts = report.get("artifacts")
        trials = report.get("trials")
        repeats = report.get("repeats_per_artifact")
        if (
            type(repeats) is not int
            or repeats < 3
            or not isinstance(artifacts, list)
            or len(artifacts) < 3
            or not isinstance(trials, list)
            or report.get("planned_trial_count") != len(artifacts) * repeats
            or report.get("completed_trial_count") != len(trials)
            or report.get("valid_trial_count", 0) < len(artifacts) * 3
        ):
            return False, "judge_scope_or_count_mismatch"
    return True, "semantic_pass"


def build_readiness_report(
    *,
    protocol: Mapping[str, Any],
    protocol_errors: list[str],
    environment: Mapping[str, str] | None = None,
    natural_language_report: Path | None = None,
    pilot_report: Path | None = None,
    judge_report: Path | None = None,
    formal_report: Path | None = None,
    formal_judge_report: Path | None = None,
) -> dict[str, Any]:
    """Return statuses without exposing credential values or opening holdout data."""

    env = dict(environment or {})
    thresholds = protocol.get("release_thresholds", {})
    online = protocol.get("online_execution_policy", {})
    holdout = protocol.get("holdout_policy", {})
    restart_preflight = protocol.get("formal_restart_preflight", {})
    latest_attempt_value = protocol.get("latest_formal_attempt", {})
    latest_attempt = (
        latest_attempt_value
        if isinstance(latest_attempt_value, Mapping)
        else {}
    )
    historical_holdout_access = latest_attempt.get("holdout_accessed") is True
    required_thresholds = (
        "development_success_improvement_min",
        "holdout_noninferiority_margin",
        "simple_case_token_budget",
        "simple_case_duration_budget_s",
        "max_content_repairs",
    )
    missing_thresholds = [
        name for name in required_thresholds if thresholds.get(name) is None
    ]
    nl_ok, nl_path, nl_integrity = _report_integrity(natural_language_report)
    pilot_ok, pilot_path, pilot_integrity = _report_integrity(pilot_report)
    judge_ok, judge_path, judge_integrity = _report_integrity(judge_report)
    formal_integrity_ok, formal_path, formal_integrity = _report_integrity(formal_report)
    formal_judge_integrity_ok, formal_judge_path, formal_judge_integrity = _report_integrity(
        formal_judge_report
    )
    nl_semantic, nl_semantic_detail = _semantic_report_status(
        natural_language_report,
        schema_version="v8-stage0-natural-language-regression-1",
        verdict="MECHANISM_REGRESSION_PASS",
        guard_expectations={
            "external_network_allowed": False,
            "holdout_access_allowed": False,
            "online_model_allowed": False,
        },
    )
    pilot_semantic, pilot_semantic_detail = _semantic_report_status(
        pilot_report,
        schema_version="v8-contemporaneous-pilot-1",
        verdict="PILOT_PASS",
        guard_expectations={
            "development_cases_only": True,
            "holdout_accessed": False,
            "judge_called": False,
            "external_evaluator": "week9.cross_version_validation.v1",
        },
    )
    judge_semantic, judge_semantic_detail = _semantic_report_status(
        judge_report,
        schema_version="v8-judge-stability-1",
        verdict="JUDGE_STABILITY_PASS",
        guard_expectations={
            "generator_called": False,
            "holdout_accessed": False,
            "temperature_parameter_sent": False,
        },
    )
    nl_ok = nl_ok and nl_semantic
    pilot_ok = pilot_ok and pilot_semantic
    judge_ok = judge_ok and judge_semantic
    generator_credentials = bool(env.get("DEEPSEEK_API_KEY"))
    judge_credentials = bool(env.get("KIMI_API_KEY"))
    online_authorized = bool(
        online.get("current_milestone_allows_online_calls")
        and online.get("user_confirmation_id")
        and online.get("process_tree_cleanup_ready")
    )
    formal_implementation = protocol.get("formal_execution_implementation", {})
    formal_implementation_ready = bool(
        formal_implementation.get("ready") is True
        and formal_implementation.get("offline_mock_and_integration_tested") is True
        and isinstance(formal_implementation.get("files"), dict)
        and formal_implementation.get("files")
    )
    formal_ready = bool(
        not protocol_errors
        and nl_ok
        and pilot_ok
        and judge_ok
        and not missing_thresholds
        and protocol.get("formal_online_runs_allowed") is True
        and online_authorized
        and holdout.get("access_enabled") is True
        and formal_implementation_ready
    )
    formal_result_status, formal_result_detail = _formal_result_status(
        formal_report, integrity_ok=formal_integrity_ok
    )
    incomplete_attempt = bool(
        formal_report is None
        and latest_attempt
        and latest_attempt.get("formal_report_produced") is False
    )
    if incomplete_attempt:
        formal_result_status = "INCOMPLETE"
        formal_result_detail = "aborted_before_formal_report"
    formal_judge_status, formal_judge_detail = _formal_judge_result_status(
        formal_judge_report, integrity_ok=formal_judge_integrity_ok
    )
    return {
        "schema_version": "v8-experiment-readiness-1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "offline_audit_only": True,
        "protocol": {
            "status": "PASS" if not protocol_errors else "FAIL",
            "errors": list(protocol_errors),
        },
        "roles": {
            "development": "GPT/Codex",
            "controlled_runtime_generator": "DeepSeek",
            "default_v8_runtime_generator": "deterministic_local",
            "auxiliary_judge": "Kimi",
            "development_is_experimental_arm": False,
        },
        "natural_language_development_regression": {
            "status": "PASS" if nl_ok else "NOT_RUN_OR_INVALID",
            "report_path": nl_path,
            "integrity": nl_integrity,
            "semantics": nl_semantic_detail,
        },
        "contemporaneous_v4_v6_pilot": {
            "status": "PASS" if pilot_ok else (
                "NOT_RUN" if pilot_report is None else "INVALID"
            ),
            "report_path": pilot_path,
            "integrity": pilot_integrity,
            "semantics": pilot_semantic_detail,
        },
        "judge_stability": {
            "status": "PASS" if judge_ok else (
                "NOT_RUN" if judge_report is None else "INVALID"
            ),
            "report_path": judge_path,
            "integrity": judge_integrity,
            "semantics": judge_semantic_detail,
        },
        "release_thresholds": {
            "status": "FROZEN" if not missing_thresholds else "NOT_FROZEN",
            "missing": missing_thresholds,
        },
        "formal_implementation": {
            "status": "READY" if formal_implementation_ready else "NOT_READY",
            "ready": formal_implementation_ready,
            "hash_bound_file_count": (
                len(formal_implementation.get("files", {}))
                if isinstance(formal_implementation.get("files"), dict)
                else 0
            ),
        },
        "online_execution": {
            "status": "AUTHORIZED" if online_authorized else "DISABLED",
            "generator_credentials_present": generator_credentials,
            "judge_credentials_present": judge_credentials,
            "user_confirmation_recorded": bool(online.get("user_confirmation_id")),
            "process_tree_cleanup_ready": bool(
                online.get("process_tree_cleanup_ready")
            ),
            "process_tree_cleanup_certification": bool(
                online.get("process_tree_cleanup_certification")
            ),
            "credential_values_recorded": False,
        },
        "holdout": {
            "status": (
                "ENABLED"
                if holdout.get("access_enabled")
                else "LOCKED_AFTER_ACCESS"
                if historical_holdout_access
                else "LOCKED"
            ),
            "accessed_by_audit": False,
            "historical_formal_access_recorded": historical_holdout_access,
            "historical_holdout_run_count": (
                latest_attempt.get("holdout_run_count", 0)
                if historical_holdout_access
                else 0
            ),
        },
        "formal_experiment": {
            "status": "READY" if formal_ready else "BLOCKED",
            "ready": formal_ready,
        },
        "formal_result": {
            "status": formal_result_status,
            "report_path": formal_path,
            "integrity": formal_integrity,
            "semantics": formal_result_detail,
            "incomplete_attempt": {
                "recorded": incomplete_attempt,
                "attempt_directory": latest_attempt.get("attempt_directory", ""),
                "completed_run_count": latest_attempt.get("completed_run_count", 0),
                "planned_run_count": (
                    restart_preflight.get("replacement_planned_run_count", 0)
                    if isinstance(restart_preflight, Mapping)
                    else 0
                ),
                "infrastructure_failure_count_min": latest_attempt.get(
                    "infrastructure_failure_count_min", 0
                ),
                "holdout_accessed": historical_holdout_access,
                "formal_judge_called": latest_attempt.get(
                    "formal_judge_called", False
                ),
            },
        },
        "formal_judge_result": {
            "status": formal_judge_status,
            "report_path": formal_judge_path,
            "integrity": formal_judge_integrity,
            "semantics": formal_judge_detail,
        },
    }


def _formal_result_status(
    path: Path | None, *, integrity_ok: bool
) -> tuple[str, str]:
    if path is None:
        return "NOT_RUN", "report_not_found"
    if not integrity_ok:
        return "INVALID", "sha256_not_verified"
    try:
        report = _read_object(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return "INVALID", f"report_parse_error:{type(exc).__name__}"
    if report.get("schema_version") != "v8-formal-comparison-1":
        return "INVALID", "schema_mismatch"
    verdict = report.get("verdict")
    if verdict not in {"FORMAL_PASS", "FORMAL_FAIL"}:
        return "INVALID", "verdict_invalid"
    records = report.get("records")
    if (
        not isinstance(records, list)
        or report.get("completed_run_count") != len(records)
        or report.get("planned_run_count") != len(records)
        or report.get("guard", {}).get("versions") != ["v4", "v6", "v8"]
        or report.get("guard", {}).get("arms")
        != [
            "v4_default_deepseek",
            "v6_default_deepseek",
            "v8_controlled_deepseek",
            "v8_default_local",
        ]
        or any(
            item.get("arm_id")
            not in {
                "v4_default_deepseek",
                "v6_default_deepseek",
                "v8_controlled_deepseek",
                "v8_default_local",
            }
            for item in records
        )
        or report.get("guard", {}).get("external_evaluator")
        != "week9.cross_version_validation.v1"
    ):
        return "INVALID", "scope_or_count_mismatch"
    return ("PASS" if verdict == "FORMAL_PASS" else "FAIL"), "semantic_complete"


def _formal_judge_result_status(
    path: Path | None, *, integrity_ok: bool
) -> tuple[str, str]:
    if path is None:
        return "NOT_RUN", "report_not_found"
    if not integrity_ok:
        return "INVALID", "sha256_not_verified"
    try:
        report = _read_object(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return "INVALID", f"report_parse_error:{type(exc).__name__}"
    if (
        report.get("schema_version") != "v8-formal-judge-1"
        or report.get("verdict") != "FORMAL_JUDGE_COMPLETE"
        or report.get("guard", {}).get("hard_verdict_override_allowed") is not False
        or report.get("guard", {}).get("source_arms")
        != [
            "v4_default_deepseek",
            "v6_default_deepseek",
            "v8_controlled_deepseek",
            "v8_default_local",
        ]
        or report.get("completed_trial_count") != report.get("planned_trial_count")
    ):
        return "INVALID", "semantic_mismatch"
    conditions = report.get("pass_conditions")
    artifact_count = report.get("artifact_count")
    unique_count = report.get("unique_judge_input_count")
    deduplicated_count = report.get("deduplicated_artifact_count")
    summaries = report.get("summary_by_dataset_and_arm")
    if (
        not isinstance(conditions, dict)
        or not conditions
        or any(value is not True for value in conditions.values())
        or type(artifact_count) is not int
        or type(unique_count) is not int
        or type(deduplicated_count) is not int
        or not 0 < unique_count <= artifact_count
        or deduplicated_count != artifact_count - unique_count
        or not isinstance(summaries, dict)
        or set(summaries) != {"development", "holdout"}
        or any(
            not isinstance(summaries[dataset], dict)
            or set(summaries[dataset])
            != {
                "v4_default_deepseek",
                "v6_default_deepseek",
                "v8_controlled_deepseek",
                "v8_default_local",
            }
            for dataset in ("development", "holdout")
        )
    ):
        return "INVALID", "scope_or_deduplication_mismatch"
    return "COMPLETE", "semantic_complete"


def run_audit(*, output_root: Path | None = None) -> tuple[dict[str, Any], Path]:
    from week9.experiments.freeze_protocol import validate_protocol

    protocol = _read_object(PROTOCOL_PATH)
    natural_language_report = _latest_report(
        "stage0-natural-language-offline-*", "natural-language-regression-report.json"
    )
    pilot_report = _latest_report(
        "contemporaneous-pilot-*", "contemporaneous-pilot-report.json"
    )
    judge_report = _latest_report(
        "judge-stability-*", "judge-stability-report.json"
    )
    formal_report = _latest_report(
        "formal-comparison-*", "formal-comparison-report.json"
    )
    formal_judge_report = _latest_report(
        "formal-judge-*", "formal-judge-report.json"
    )
    report = build_readiness_report(
        protocol=protocol,
        protocol_errors=validate_protocol(protocol),
        environment=os.environ,
        natural_language_report=natural_language_report,
        pilot_report=pilot_report,
        judge_report=judge_report,
        formal_report=formal_report,
        formal_judge_report=formal_judge_report,
    )
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = (output_root or RESULTS_ROOT).resolve()
    result_dir = root / f"experiment-readiness-{timestamp}"
    result_dir.mkdir(parents=True, exist_ok=False)
    report_path = result_dir / "experiment-readiness-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
    report_path.with_suffix(".sha256").write_text(
        f"{digest}  {report_path.name}\n", encoding="ascii"
    )
    return report, result_dir


def main() -> int:
    report, result_dir = run_audit()
    print(
        f"V8 experiment readiness: {report['formal_experiment']['status']} "
        f"-> {result_dir}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
