"""Validate the final offline-frozen protocol for the blinded V8 formal batch."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping

from week9.experiments.blinded_formal_execution import (
    AUTHORIZATION_SCHEMA,
    DEFAULT_FINAL_PROTOCOL,
    DEFAULT_PRIVATE_RESULTS_ROOT,
    FINAL_PROTOCOL_DIGEST,
    FINAL_PROTOCOL_SCHEMA,
    FORMAL_ARM_IDS,
    FORMAL_DEADLINE_S,
    FORMAL_REPETITIONS,
    PINNED_DEEPSEEK_MODEL,
    PINNED_DEEPSEEK_THINKING_MODE,
    PLANNED_RUN_COUNT,
    SEALED_CASE_REFS,
    build_blinded_schedule,
)
from week9.experiments.formal_recovery import recovery_policy_manifest
from week9.experiments.freeze_formal_recovery_protocol import (
    DEFAULT_PROTOCOL as RECOVERY_PROTOCOL,
    PROTOCOL_DIGEST as RECOVERY_DIGEST,
    validate_recovery_protocol,
)
from week9.experiments.freeze_independent_holdout_binding import (
    DEFAULT_BINDING,
    BINDING_DIGEST,
    canonical_sha256,
    file_sha256,
    validate_binding_protocol,
)
from week9.experiments.freeze_protocol import (
    DEFAULT_PROTOCOL as BASELINE_PROTOCOL,
    PROTOCOL_DIGEST as BASELINE_DIGEST,
    validate_protocol,
)
from week9.experiments.run_blinded_execution_preflight import PASS_VERDICT


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SUPERSEDED_PROTOCOL = PROJECT_ROOT / "week9" / "configs" / "blinded_formal_protocol_v1.json"
SUPERSEDED_DIGEST = SUPERSEDED_PROTOCOL.with_suffix(".sha256")
IMPLEMENTATION_FILES = (
    "experiments/blinded_formal_execution.py",
    "experiments/blinded_arm_worker.py",
    "experiments/run_blinded_formal_comparison.py",
    "experiments/run_blinded_execution_preflight.py",
    "experiments/freeze_blinded_formal_protocol.py",
    "tests/test_blinded_formal_execution.py",
)
EXPECTED_RELEASE_THRESHOLDS = {
    "known_hard_error_false_release_rate_max": 0.0,
    "development_success_improvement_min": 0.1111111111111111,
    "holdout_noninferiority_margin": 0.0,
    "simple_case_token_budget": 33198,
    "simple_case_duration_budget_s": 75.1,
    "max_content_repairs": 1,
    "freeze_note": (
        "Frozen from the verified contemporaneous V4/V6 Pilot proposal before "
        "any formal V8 result or holdout execution."
    ),
}
EXPECTED_BASELINE_GENERATOR_PROFILE = {
    "provider": "deepseek",
    "api_key_env_var": "DEEPSEEK_API_KEY",
    "api_base_env_var": "DEEPSEEK_API_BASE",
    "model_env_var": "DEEPSEEK_MODEL",
    "temperature": 0.3,
    "max_tokens": 8192,
    "v8_projection_schema": "v8-certified-requirement-projection-1",
    "v8_prompt_sha256": "b6b4abed2e928d23f7aa54988f205097f0c270231d1cff2c04a51a3b2be91695",
    "v8_deterministic_verifier_required": True,
}
EXPECTED_GENERATOR_PROFILE = {
    **EXPECTED_BASELINE_GENERATOR_PROFILE,
    "model_id": PINNED_DEEPSEEK_MODEL,
    "api_base_host": "api.deepseek.com",
    "thinking_mode": PINNED_DEEPSEEK_THINKING_MODE,
    "compatibility_migration": {
        "superseded_model_id": "deepseek-chat",
        "retired_after_utc": "2026-07-24T15:59:00Z",
        "successor_model_id": PINNED_DEEPSEEK_MODEL,
        "successor_mode": "non-thinking",
        "official_reference": "https://api-docs.deepseek.com/zh-cn/updates/",
        "reason": "retired_alias_replaced_before_any_v2_authorization_or_execution",
    },
}
EXPECTED_PREFLIGHT_GUARD = {
    "synthetic_content_only": True,
    "independent_holdout_loaded": False,
    "old_holdout_loaded": False,
    "credentials_read": False,
    "network_used": False,
    "deepseek_called": False,
    "kimi_called": False,
    "private_content_emitted": False,
    "case_ids_emitted": False,
    "requirements_emitted": False,
    "parameters_emitted": False,
}
EXPECTED_GATES = {
    "baseline_protocol_frozen": True,
    "recovery_protocol_frozen": True,
    "independent_holdout_manifest_bound": True,
    "non_disclosing_holdout_validation_passed": True,
    "blinded_schedule_frozen": True,
    "private_worker_implemented": True,
    "synthetic_execution_preflight_passed": True,
    "retired_model_alias_replaced_and_pinned": True,
    "new_user_confirmation_recorded": False,
}


def validate_blinded_formal_protocol(protocol: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    baseline = _read_object(BASELINE_PROTOCOL, errors, "baseline protocol")
    recovery = _read_object(RECOVERY_PROTOCOL, errors, "recovery protocol")
    binding = _read_object(DEFAULT_BINDING, errors, "holdout binding")
    baseline_digest = _read_digest(BASELINE_DIGEST, errors, "baseline digest")
    recovery_digest = _read_digest(RECOVERY_DIGEST, errors, "recovery digest")
    binding_digest = _read_digest(BINDING_DIGEST, errors, "binding digest")
    if baseline and validate_protocol(baseline):
        errors.append("baseline protocol is no longer valid")
    if recovery and validate_recovery_protocol(recovery):
        errors.append("recovery protocol is no longer valid")
    if binding and validate_binding_protocol(binding):
        errors.append("independent holdout binding is no longer valid")

    if protocol.get("schema_version") != FINAL_PROTOCOL_SCHEMA:
        errors.append("final blinded protocol schema differs")
    if protocol.get("status") != "READY_AWAITING_USER_AUTHORIZATION":
        errors.append("final blinded protocol status differs")
    if protocol.get("formal_execution_allowed") is not False:
        errors.append("final protocol cannot self-authorize execution")
    if protocol.get("formal_judge_allowed") is not False:
        errors.append("final protocol cannot enable the formal Judge")
    expected_digest = _read_digest(FINAL_PROTOCOL_DIGEST, errors, "final protocol digest")
    if expected_digest and canonical_sha256(protocol) != expected_digest:
        errors.append("final blinded protocol differs from its frozen digest")

    superseded = _read_object(SUPERSEDED_PROTOCOL, errors, "superseded v1 protocol")
    superseded_digest = _read_digest(SUPERSEDED_DIGEST, errors, "superseded v1 digest")
    expected_supersession = {
        "path": "week9/configs/blinded_formal_protocol_v1.json",
        "sha256": superseded_digest,
        "status": "SUPERSEDED_BEFORE_AUTHORIZATION",
        "paid_calls_made": False,
        "holdout_plaintext_loaded_by_formal_execution": False,
        "reason": "deepseek-chat_alias_retired",
    }
    if (
        not superseded
        or canonical_sha256(superseded) != superseded_digest
        or protocol.get("supersedes") != expected_supersession
    ):
        errors.append("v1 supersession evidence differs")

    expected_chain = {
        "experiment_protocol": {
            "path": "week9/configs/experiment_protocol.json",
            "sha256": baseline_digest,
            "unchanged": True,
        },
        "formal_recovery_protocol": {
            "path": "week9/configs/formal_recovery_protocol_v2.json",
            "sha256": recovery_digest,
            "unchanged": True,
        },
        "independent_holdout_binding": {
            "path": "week9/configs/independent_holdout_binding_v1.json",
            "sha256": binding_digest,
            "unchanged": True,
        },
    }
    if protocol.get("baseline_chain") != expected_chain:
        errors.append("immutable baseline chain differs")

    source = binding.get("source_submission", {}) if isinstance(binding, Mapping) else {}
    expected_holdout = {
        "path": "week9/configs/independent_holdout_binding_v1.json",
        "sha256": binding_digest,
        "manifest_sha256": source.get("manifest_sha256"),
        "private_artifact_sha256": source.get("private_artifact_sha256"),
        "case_count": 2,
        "public_case_refs": list(SEALED_CASE_REFS),
        "developer_content_access_allowed": False,
    }
    if protocol.get("holdout_binding") != expected_holdout:
        errors.append("final holdout binding differs")

    expected_design = {
        "comparison_arms": list(FORMAL_ARM_IDS),
        "development_case_count": 3,
        "independent_holdout_case_count": 2,
        "repetitions": FORMAL_REPETITIONS,
        "planned_run_count": PLANNED_RUN_COUNT,
        "deadline_s_per_attempt": FORMAL_DEADLINE_S,
        "same_outcome_policy_for_all_arms": True,
        "old_holdout_included": False,
        "historical_formal_records_reused": False,
        "stop_immediately_on_invalid_infrastructure": True,
        "deepseek_model_id": PINNED_DEEPSEEK_MODEL,
        "deepseek_thinking_mode": PINNED_DEEPSEEK_THINKING_MODE,
    }
    if protocol.get("planned_design") != expected_design:
        errors.append("final formal design differs")
    if protocol.get("outcome_policy_sha256") != canonical_sha256(
        recovery_policy_manifest()
    ):
        errors.append("recovery outcome policy digest differs")

    manifest_digest = str(source.get("manifest_sha256") or "")
    try:
        schedule = build_blinded_schedule(
            FORMAL_REPETITIONS,
            manifest_sha256=manifest_digest,
        )
    except ValueError:
        schedule = ()
    serialized_schedule = json.dumps(schedule, ensure_ascii=False)
    if (
        len(schedule) != PLANNED_RUN_COUNT
        or "raw_input" in serialized_schedule
        or "oracle" in serialized_schedule
        or "case_id" in serialized_schedule
    ):
        errors.append("public blinded schedule is invalid or disclosing")

    expected_private = {
        "root": str(DEFAULT_PRIVATE_RESULTS_ROOT),
        "plaintext_loaded_only_after_authorization": True,
        "plaintext_in_os_command_line": False,
        "raw_candidates_stored_outside_project": True,
        "worker_stdout_stderr_private": True,
        "public_reports_content_free": True,
        "development_session_may_read_plaintext": False,
        "formal_execution_process_may_read_plaintext": True,
    }
    if protocol.get("private_execution_boundary") != expected_private:
        errors.append("private execution boundary differs")
    expected_public = {
        "case_refs_only": True,
        "case_ids_emitted": False,
        "requirements_emitted": False,
        "selected_families_emitted": False,
        "parameters_emitted": False,
        "raw_artifacts_emitted": False,
        "candidate_and_evaluator_hashes_emitted": True,
        "aggregate_statistics_emitted": True,
    }
    if protocol.get("public_output_policy") != expected_public:
        errors.append("public output policy differs")

    if protocol.get("release_thresholds") != EXPECTED_RELEASE_THRESHOLDS:
        errors.append("release thresholds differ")
    if baseline.get("release_thresholds") != EXPECTED_RELEASE_THRESHOLDS:
        errors.append("baseline release thresholds differ")
    if protocol.get("generator_profile") != EXPECTED_GENERATOR_PROFILE:
        errors.append("generator profile differs")
    if baseline.get("generator") != EXPECTED_BASELINE_GENERATOR_PROFILE:
        errors.append("baseline generator profile differs")

    expected_authorization = {
        "additive_receipt_required": True,
        "receipt_schema": AUTHORIZATION_SCHEMA,
        "current_receipt_path": None,
        "current_receipt_sha256": None,
        "explicit_confirmation_required": True,
        "one_batch_only": True,
        "planned_run_limit": PLANNED_RUN_COUNT,
        "consume_before_first_model_call": True,
        "credentials_read_after_validation_only": True,
        "existing_confirmation_ids_reusable": False,
    }
    if protocol.get("authorization_policy") != expected_authorization:
        errors.append("authorization policy differs")

    gate = protocol.get("online_execution_gate")
    expected_gate = {
        "gate_status": EXPECTED_GATES,
        "credentials_read": False,
        "user_confirmation_id": None,
        "formal_execution_allowed": False,
        "formal_judge_allowed": False,
        "next_permitted_action": "obtain_new_paid_authorization_for_exact_protocol_digest",
    }
    if gate != expected_gate:
        errors.append("final online gate differs")

    _validate_preflight(protocol.get("preflight_evidence"), errors)
    _validate_implementation(protocol.get("execution_implementation"), errors)
    return errors


def _validate_preflight(value: Any, errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append("blinded preflight evidence must be an object")
        return
    path = _safe_project_path(value.get("report_path"), errors, "preflight report")
    if path is None or not path.is_file():
        errors.append("blinded preflight report is unavailable")
        return
    digest = file_sha256(path)
    if value.get("report_sha256") != digest:
        errors.append("blinded preflight report hash differs")
    if _read_digest(path.with_suffix(".sha256"), errors, "preflight sidecar") != digest:
        errors.append("blinded preflight sidecar differs")
    report = _read_object(path, errors, "blinded preflight")
    pass_conditions = report.get("pass_conditions")
    if (
        value.get("verdict") != PASS_VERDICT
        or value.get("scenario_count") != 9
        or report.get("schema_version") != "v8-blinded-formal-preflight-2"
        or report.get("verdict") != PASS_VERDICT
        or report.get("planned_run_count") != PLANNED_RUN_COUNT
        or report.get("scenario_count") != 9
        or not isinstance(pass_conditions, Mapping)
        or len(pass_conditions) != 9
        or any(item is not True for item in pass_conditions.values())
        or report.get("guard") != EXPECTED_PREFLIGHT_GUARD
    ):
        errors.append("blinded preflight evidence differs or does not pass")


def _validate_implementation(value: Any, errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append("execution implementation must be an object")
        return
    files = value.get("files")
    if (
        value.get("status") != "BLINDED_EXECUTION_STACK_READY"
        or not isinstance(files, Mapping)
        or set(files) != set(IMPLEMENTATION_FILES)
    ):
        errors.append("execution implementation scope differs")
        return
    for relative in IMPLEMENTATION_FILES:
        path = PROJECT_ROOT / "week9" / relative
        if not path.is_file() or files.get(relative) != file_sha256(path):
            errors.append(f"execution implementation hash differs: {relative}")


def _safe_project_path(value: Any, errors: list[str], label: str) -> Path | None:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        errors.append(f"{label} must be project-relative")
        return None
    path = (PROJECT_ROOT / value).resolve()
    if not path.is_relative_to(PROJECT_ROOT):
        errors.append(f"{label} escapes the project")
        return None
    return path


def _read_object(path: Path, errors: list[str], label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        errors.append(f"{label} is unavailable or invalid")
        return {}
    if not isinstance(value, dict):
        errors.append(f"{label} root must be an object")
        return {}
    return value


def _read_digest(path: Path, errors: list[str], label: str) -> str:
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        errors.append(f"{label} is unavailable")
        return ""
    if len(value) != 64 or any(item not in "0123456789abcdef" for item in value):
        errors.append(f"{label} is not a lowercase SHA-256")
        return ""
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", default=str(DEFAULT_FINAL_PROTOCOL))
    parser.add_argument("--print-protocol-digest", action="store_true")
    args = parser.parse_args(argv)
    protocol = _read_object(Path(args.protocol), [], "final blinded protocol")
    if not protocol:
        print("ERROR: final blinded protocol is unavailable", file=sys.stderr)
        return 1
    if args.print_protocol_digest:
        print(canonical_sha256(protocol))
        return 0
    errors = validate_blinded_formal_protocol(protocol)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("V8 blinded formal protocol v2 is frozen and awaits a new one-batch authorization")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "IMPLEMENTATION_FILES",
    "main",
    "validate_blinded_formal_protocol",
]
