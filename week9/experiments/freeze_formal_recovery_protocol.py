"""Validate the offline-only V8 formal recovery protocol v2."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping

from week9.experiments.formal_recovery import recovery_policy_manifest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PROTOCOL = PROJECT_ROOT / "week9" / "configs" / "experiment_protocol.json"
BASELINE_DIGEST = PROJECT_ROOT / "week9" / "configs" / "experiment_protocol.sha256"
DEFAULT_PROTOCOL = (
    PROJECT_ROOT / "week9" / "configs" / "formal_recovery_protocol_v2.json"
)
PROTOCOL_DIGEST = DEFAULT_PROTOCOL.with_suffix(".sha256")
RECOVERY_IMPLEMENTATION_FILES = (
    "experiments/formal_recovery.py",
    "experiments/formal_recovery_harness.py",
    "experiments/formal_recovery_adapters.py",
    "experiments/run_formal_recovery_preflight.py",
    "experiments/run_formal_recovery_adapter_dry_run.py",
    "experiments/freeze_formal_recovery_protocol.py",
    "tests/test_formal_recovery.py",
    "tests/test_formal_recovery_adapters.py",
)
EXPECTED_GATES = {
    "offline_preflight_passed": True,
    "outer_harness_implemented": True,
    "formal_adapter_integration_complete": True,
    "independent_holdout_manifest_bound": False,
    "development_dry_run_passed": True,
    "new_user_confirmation_recorded": False,
}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_recovery_protocol(protocol: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    baseline = _read_object(BASELINE_PROTOCOL, errors, "baseline protocol")
    baseline_digest = _read_ascii_digest(BASELINE_DIGEST, errors, "baseline digest")
    if baseline and baseline_digest and canonical_sha256(baseline) != baseline_digest:
        errors.append("baseline V8 protocol no longer matches its frozen digest")

    if protocol.get("schema_version") != "v8-formal-recovery-protocol-2":
        errors.append("recovery protocol schema differs")
    if protocol.get("status") != "offline_recovery_complete_new_holdout_required":
        errors.append("recovery protocol must remain offline and holdout-blocked")
    if protocol.get("formal_online_runs_allowed") is not False:
        errors.append("recovery protocol cannot enable paid formal runs")
    if protocol.get("formal_judge_allowed") is not False:
        errors.append("recovery protocol cannot enable the formal Judge")

    try:
        expected_digest = PROTOCOL_DIGEST.read_text(encoding="ascii").strip()
    except OSError as exc:
        errors.append(f"recovery protocol digest sidecar is unavailable: {exc}")
    else:
        if canonical_sha256(protocol) != expected_digest:
            errors.append("complete recovery protocol differs from its frozen digest")

    baseline_boundary = protocol.get("baseline_boundary", {})
    if not isinstance(baseline_boundary, Mapping):
        errors.append("baseline_boundary must be an object")
        baseline_boundary = {}
    expected_boundary = {
        "protocol_path": "week9/configs/experiment_protocol.json",
        "protocol_sha256": baseline_digest,
        "old_formal_result_status": "INCOMPLETE",
        "old_holdout_retired": True,
        "old_holdout_access_allowed": False,
        "old_attempt_reclassification_allowed": False,
        "historical_records_reusable": False,
        "current_frozen_v8_files_mutated": False,
    }
    if dict(baseline_boundary) != expected_boundary:
        errors.append("baseline immutability boundary differs")

    if protocol.get("outcome_policy") != recovery_policy_manifest():
        errors.append("formal recovery outcome policy differs from implementation")

    planned = protocol.get("planned_design", {})
    expected_planned = {
        "comparison_arms": recovery_policy_manifest()["applies_to_all_arms"],
        "development_case_count": 3,
        "independent_holdout_case_count_required": 2,
        "repetitions": 5,
        "planned_run_count": 100,
        "same_outcome_policy_for_all_arms": True,
        "reuse_historical_records": False,
        "old_holdout_included": False,
    }
    if planned != expected_planned:
        errors.append("future formal design differs from the offline recovery plan")

    holdout = protocol.get("new_holdout_gate", {})
    expected_holdout = {
        "status": "NOT_PROVIDED",
        "independent_source_required": True,
        "developer_content_access_allowed": False,
        "manifest_path": None,
        "manifest_sha256": None,
        "case_count": None,
        "sealed_before_final_protocol": True,
        "access_enabled": False,
    }
    if holdout != expected_holdout:
        errors.append("new independent holdout gate is not sealed in blocked state")

    online = protocol.get("online_execution_gate", {})
    if not isinstance(online, Mapping):
        errors.append("online_execution_gate must be an object")
        online = {}
    if online.get("user_confirmation_id") is not None:
        errors.append("offline recovery cannot record or reuse a confirmation ID")
    if online.get("credentials_read") is not False:
        errors.append("offline recovery cannot read model credentials")
    if online.get("next_permitted_action") != (
        "obtain_independently_sealed_holdout_manifest"
    ):
        errors.append("next permitted action differs")
    if online.get("gate_status") != EXPECTED_GATES:
        errors.append("online recovery gates differ")
    required = online.get("all_gates_required_before_new_authorization")
    if required != list(EXPECTED_GATES):
        errors.append("required online-gate order differs")
    if all(EXPECTED_GATES.values()):
        errors.append("offline recovery unexpectedly satisfies every online gate")

    implementation = protocol.get("recovery_implementation", {})
    if not isinstance(implementation, Mapping):
        errors.append("recovery_implementation must be an object")
        implementation = {}
    if implementation.get("status") != "OFFLINE_RECOVERY_STACK_READY":
        errors.append("recovery implementation status differs")
    if implementation.get("formal_adapter_integration_complete") is not True:
        errors.append("formal adapter integration evidence is missing")
    bound_files = implementation.get("files", {})
    if not isinstance(bound_files, Mapping) or set(bound_files) != set(
        RECOVERY_IMPLEMENTATION_FILES
    ):
        errors.append("recovery implementation file scope differs")
    else:
        for relative in RECOVERY_IMPLEMENTATION_FILES:
            path = PROJECT_ROOT / "week9" / relative
            if not path.is_file() or bound_files.get(relative) != file_sha256(path):
                errors.append(f"recovery implementation hash differs: {relative}")

    preflight = protocol.get("preflight_evidence", {})
    _validate_preflight(preflight, errors)
    _validate_adapter_dry_run(protocol.get("adapter_dry_run_evidence", {}), errors)
    _validate_retired_markers(protocol.get("retired_attempt_markers", {}), baseline, errors)
    return errors


def _validate_preflight(value: Any, errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append("preflight_evidence must be an object")
        return
    path = _safe_project_path(value.get("report_path"), errors, "preflight report")
    expected_sha = value.get("report_sha256")
    if path is None or not path.is_file():
        errors.append("bound recovery preflight report is unavailable")
        return
    actual_sha = file_sha256(path)
    if expected_sha != actual_sha:
        errors.append("bound recovery preflight report hash differs")
    sidecar = path.with_suffix(".sha256")
    if _read_ascii_digest(sidecar, errors, "preflight sidecar") != actual_sha:
        errors.append("recovery preflight sidecar differs")
    report = _read_object(path, errors, "preflight report")
    expected_guard = {
        "external_network_allowed": False,
        "deepseek_called": False,
        "kimi_called": False,
        "dataset_content_read": False,
        "old_holdout_accessed": False,
        "new_holdout_accessed": False,
        "historical_run_results_read": False,
        "synthetic_fault_injection_only": True,
    }
    if (
        report.get("schema_version") != "v8-formal-recovery-preflight-1"
        or report.get("verdict") != "RECOVERY_PREFLIGHT_PASS"
        or report.get("guard") != expected_guard
        or report.get("formal_policy") != recovery_policy_manifest()
        or report.get("planned_scenario_count") != 11
        or report.get("completed_scenario_count") != 11
        or not isinstance(report.get("pass_conditions"), dict)
        or not report["pass_conditions"]
        or any(item is not True for item in report["pass_conditions"].values())
        or not isinstance(report.get("records"), list)
        or len(report["records"]) != 11
    ):
        errors.append("recovery preflight semantics differ or do not pass")
    if value.get("verdict") != "RECOVERY_PREFLIGHT_PASS":
        errors.append("preflight evidence verdict binding differs")
    if value.get("scenario_count") != 11:
        errors.append("preflight evidence scenario count differs")


def _validate_retired_markers(
    value: Any, baseline: Mapping[str, Any], errors: list[str]
) -> None:
    if not isinstance(value, Mapping):
        errors.append("retired_attempt_markers must be an object")
        return
    restart = baseline.get("formal_restart_preflight", {})
    latest = baseline.get("latest_formal_attempt", {})
    expected = {
        "first_attempt_path": restart.get("aborted_attempt_marker_path"),
        "first_attempt_sha256": restart.get("aborted_attempt_marker_sha256"),
        "replacement_attempt_path": latest.get("incomplete_marker_path"),
        "replacement_attempt_sha256": latest.get("incomplete_marker_sha256"),
        "records_reusable": False,
        "old_holdout_reusable": False,
    }
    if dict(value) != expected:
        errors.append("retired attempt marker binding differs")
        return
    for prefix in ("first_attempt", "replacement_attempt"):
        path = _safe_project_path(value.get(f"{prefix}_path"), errors, prefix)
        if path is None or not path.is_file():
            errors.append(f"{prefix} marker is unavailable")
        elif file_sha256(path) != value.get(f"{prefix}_sha256"):
            errors.append(f"{prefix} marker hash differs")


def _validate_adapter_dry_run(value: Any, errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append("adapter_dry_run_evidence must be an object")
        return
    path = _safe_project_path(value.get("report_path"), errors, "adapter dry-run")
    expected_sha = value.get("report_sha256")
    if path is None or not path.is_file():
        errors.append("bound adapter dry-run report is unavailable")
        return
    actual_sha = file_sha256(path)
    if expected_sha != actual_sha:
        errors.append("bound adapter dry-run report hash differs")
    if _read_ascii_digest(path.with_suffix(".sha256"), errors, "adapter dry-run sidecar") != actual_sha:
        errors.append("adapter dry-run sidecar differs")
    report = _read_object(path, errors, "adapter dry-run report")
    expected_guard = {
        "development_cases_only": True,
        "old_holdout_accessed": False,
        "new_holdout_accessed": False,
        "deepseek_called": False,
        "kimi_called": False,
        "external_network_allowed": False,
        "credentials_forwarded_to_child": False,
        "v8_default_local_only": True,
    }
    records = report.get("records")
    if (
        report.get("schema_version") != "v8-formal-recovery-adapter-dry-run-1"
        or report.get("verdict") != "RECOVERY_ADAPTER_DRY_RUN_PASS"
        or report.get("guard") != expected_guard
        or report.get("outcome_policy_sha256")
        != canonical_sha256(recovery_policy_manifest())
        or report.get("planned_run_count") != 3
        or report.get("completed_run_count") != 3
        or not isinstance(report.get("pass_conditions"), dict)
        or not report["pass_conditions"]
        or any(item is not True for item in report["pass_conditions"].values())
        or not isinstance(records, list)
        or len(records) != 3
        or [item.get("case_id") for item in records]
        != ["rc_lowpass", "rlc_lowpass", "dual_room_thermal"]
        or any(item.get("arm_id") != "v8_default_local" for item in records)
        or any(item.get("overall_success") is not True for item in records)
        or any(item.get("infrastructure_failure") is not False for item in records)
        or any(item.get("candidate_online_api_used") is not False for item in records)
    ):
        errors.append("adapter dry-run semantics differ or do not pass")
    if value.get("verdict") != "RECOVERY_ADAPTER_DRY_RUN_PASS":
        errors.append("adapter dry-run verdict binding differs")
    if value.get("run_count") != 3:
        errors.append("adapter dry-run count binding differs")


def _safe_project_path(
    relative: Any, errors: list[str], label: str
) -> Path | None:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        errors.append(f"{label} path must be a non-empty project-relative path")
        return None
    path = (PROJECT_ROOT / relative).resolve()
    if not path.is_relative_to(PROJECT_ROOT):
        errors.append(f"{label} path escapes the project root")
        return None
    return path


def _read_object(
    path: Path, errors: list[str], label: str
) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{label} is unavailable or invalid: {exc}")
        return {}
    if not isinstance(value, dict):
        errors.append(f"{label} root must be an object")
        return {}
    return value


def _read_ascii_digest(path: Path, errors: list[str], label: str) -> str:
    try:
        value = path.read_text(encoding="ascii").strip().split()[0]
    except (OSError, IndexError) as exc:
        errors.append(f"{label} is unavailable: {exc}")
        return ""
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        errors.append(f"{label} is not a lowercase SHA-256")
        return ""
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", default=str(DEFAULT_PROTOCOL))
    parser.add_argument("--print-protocol-digest", action="store_true")
    args = parser.parse_args(argv)
    path = Path(args.protocol)
    protocol = json.loads(path.read_text(encoding="utf-8"))
    if args.print_protocol_digest:
        print(canonical_sha256(protocol))
        return 0
    errors = validate_recovery_protocol(protocol)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("V8 formal recovery protocol v2 is frozen and offline-only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_PROTOCOL",
    "PROTOCOL_DIGEST",
    "RECOVERY_IMPLEMENTATION_FILES",
    "canonical_sha256",
    "file_sha256",
    "main",
    "validate_recovery_protocol",
]
