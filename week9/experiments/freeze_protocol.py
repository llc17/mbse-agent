"""Verify that frozen V8 inputs still match the experiment protocol."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from week9.experiments.cases import frozen_dataset_manifest  # noqa: E402


DEFAULT_PROTOCOL = PROJECT_ROOT / "week9" / "configs" / "experiment_protocol.json"
PROTOCOL_DIGEST = PROJECT_ROOT / "week9" / "configs" / "experiment_protocol.sha256"
SOURCE_FILES = {
    "v8_start_prompt": PROJECT_ROOT / "任务规划" / "V8-启动prompt-完整版.md",
    "v8_task_plan": PROJECT_ROOT / "任务规划" / "任务规划-2026-08-28.md",
    "v7_summary": (
        PROJECT_ROOT
        / "week9"
        / "configs"
        / "source_snapshots"
        / "V7总结-pre-pilot-20260831T081903Z.md"
    ),
}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def current_snapshot() -> dict[str, Any]:
    return {
        "datasets": frozen_dataset_manifest(),
        "source_hashes": {name: file_sha256(path) for name, path in SOURCE_FILES.items()},
    }


def canonical_protocol_sha256(protocol: dict[str, Any]) -> str:
    payload = json.dumps(
        protocol,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_protocol(protocol: dict[str, Any]) -> list[str]:
    current = current_snapshot()
    errors: list[str] = []
    if protocol.get("datasets") != current["datasets"]:
        errors.append("dataset hashes differ from frozen case definitions")
    if protocol.get("source_hashes") != current["source_hashes"]:
        errors.append("planning/source document hashes differ from the frozen protocol")
    if protocol.get("source_snapshot_policy") != {
        "v7_summary_source_path": "week8/V7总结.md",
        "v7_summary_frozen_path": (
            "week9/configs/source_snapshots/"
            "V7总结-pre-pilot-20260831T081903Z.md"
        ),
        "captured_before_complete_pilot": True,
        "reason": (
            "The historical source document was being edited externally during the "
            "first incomplete paid pilot attempt; the exact current bytes were copied "
            "so a single experiment batch cannot change its own source baseline."
        ),
    }:
        errors.append("V7 source snapshot policy differs from the frozen pilot boundary")

    try:
        expected_digest = PROTOCOL_DIGEST.read_text(encoding="ascii").strip()
    except OSError as exc:
        errors.append(f"protocol digest sidecar is unavailable: {exc}")
    else:
        if canonical_protocol_sha256(protocol) != expected_digest:
            errors.append("the complete experiment protocol differs from its frozen digest")

    if type(protocol.get("formal_online_runs_allowed")) is not bool:
        errors.append("formal_online_runs_allowed must be a bool")

    if protocol.get("comparison_versions") != ["v4", "v6", "v8"]:
        errors.append("comparison versions differ from V4/V6/V8")
    expected_arms = [
        {
            "arm_id": "v4_default_deepseek",
            "version": "v4",
            "configuration_view": "controlled_and_default",
            "generation_mode": "deepseek",
            "online_generation": True,
        },
        {
            "arm_id": "v6_default_deepseek",
            "version": "v6",
            "configuration_view": "controlled_and_default",
            "generation_mode": "deepseek",
            "online_generation": True,
        },
        {
            "arm_id": "v8_controlled_deepseek",
            "version": "v8",
            "configuration_view": "controlled",
            "generation_mode": "deepseek",
            "online_generation": True,
        },
        {
            "arm_id": "v8_default_local",
            "version": "v8",
            "configuration_view": "default",
            "generation_mode": "deterministic_local",
            "online_generation": False,
        },
    ]
    if protocol.get("comparison_arms") != expected_arms:
        errors.append("controlled/default comparison arms differ")
    if protocol.get("controlled_comparison_arms") != [
        "v4_default_deepseek",
        "v6_default_deepseek",
        "v8_controlled_deepseek",
    ]:
        errors.append("controlled comparison arm scope differs")
    if protocol.get("default_configuration_arms") != [
        "v4_default_deepseek",
        "v6_default_deepseek",
        "v8_default_local",
    ]:
        errors.append("default configuration arm scope differs")
    development = protocol.get("development_provenance", {})
    if (
        development.get("assistant_family") != "GPT/Codex"
        or development.get("experimental_runtime_role") is not False
    ):
        errors.append("development provenance is missing or treated as a runtime arm")

    repetitions = protocol.get("repetitions", {})
    for name, minimum in (("smoke_or_exploratory_min", 3), ("formal_recommended", 5)):
        value = repetitions.get(name)
        if type(value) is not int or value < minimum:
            errors.append(f"invalid repetition policy: {name}")

    fairness = protocol.get("fairness", {})
    for name in (
        "same_raw_requirement",
        "same_generator_model_and_parameters",
        "interleaved_paired_execution",
        "isolated_subprocess_per_version",
        "unique_run_directory",
        "external_evaluator_is_source_of_truth",
        "infrastructure_failures_reported_separately",
        "equal_budget_and_default_configuration_views",
    ):
        if fairness.get(name) is not True:
            errors.append(f"required fairness rule is not enabled: {name}")

    generator = protocol.get("generator", {})
    if generator.get("provider") != "deepseek":
        errors.append("formal generator provider must be deepseek")
    for name, expected in (
        ("api_key_env_var", "DEEPSEEK_API_KEY"),
        ("api_base_env_var", "DEEPSEEK_API_BASE"),
        ("model_env_var", "DEEPSEEK_MODEL"),
        ("v8_projection_schema", "v8-certified-requirement-projection-1"),
    ):
        if generator.get(name) != expected:
            errors.append(f"formal generator setting differs: {name}")
    if generator.get("temperature") != 0.3 or generator.get("max_tokens") != 8192:
        errors.append("formal generator request parameters differ from the frozen values")
    if generator.get("v8_deterministic_verifier_required") is not True:
        errors.append("V8 online projection must require deterministic verification")
    try:
        from week9.src.online_generation import ONLINE_PROMPT_SHA256
    except ImportError as exc:
        errors.append(f"V8 online generation contract is unavailable: {exc}")
    else:
        if generator.get("v8_prompt_sha256") != ONLINE_PROMPT_SHA256:
            errors.append("V8 online projection prompt hash differs")

    formal_implementation = protocol.get("formal_execution_implementation", {})
    if formal_implementation.get("ready") is not True:
        errors.append("formal execution implementation is not marked ready")
    if formal_implementation.get("offline_mock_and_integration_tested") is not True:
        errors.append("formal execution implementation lacks offline test readiness")
    implementation_files = formal_implementation.get("files")
    required_implementation_files = {
        "src/generation.py",
        "src/online_generation.py",
        "src/runner.py",
        "src/pipeline.py",
        "experiments/run_formal_comparison.py",
        "experiments/run_formal_judge.py",
        "experiments/version_adapters.py",
        "experiments/cross_version_validation.py",
        "experiments/external_evaluator.py",
        "experiments/online_policy.py",
        "experiments/subprocess_control.py",
    }
    if not isinstance(implementation_files, dict) or set(implementation_files) != required_implementation_files:
        errors.append("formal execution implementation file set differs")
    else:
        week9_root = (PROJECT_ROOT / "week9").resolve()
        for relative, expected_sha in implementation_files.items():
            path = (week9_root / relative).resolve()
            if not path.is_relative_to(week9_root) or not path.is_file():
                errors.append(f"formal implementation file is unavailable: {relative}")
            elif file_sha256(path) != expected_sha:
                errors.append(f"formal implementation file hash differs: {relative}")

    judge = protocol.get("judge", {})
    if judge.get("hard_gate") is not False:
        errors.append("Judge must not be configured as a hard gate")
    if judge.get("content_hash_deduplication") is not True:
        errors.append("Judge must deduplicate canonical-equivalent blinded content")
    if not _finite_number_at_least(judge.get("max_tokens_min"), 16000):
        errors.append("Judge max_tokens_min must be at least 16000")

    online = protocol.get("online_execution_policy", {})
    for name in ("current_milestone_allows_online_calls", "process_tree_cleanup_ready"):
        if type(online.get(name)) is not bool:
            errors.append(f"online execution policy field must be bool: {name}")
    if online.get("process_tree_cleanup_ready"):
        certification = online.get("process_tree_cleanup_certification")
        if not isinstance(certification, dict):
            errors.append("process-tree cleanup is ready without a certification record")
        else:
            relative = certification.get("report_path")
            expected_sha = certification.get("sha256")
            if not isinstance(relative, str) or not relative:
                errors.append("process-tree cleanup certification path is missing")
            else:
                report_path = (PROJECT_ROOT / relative).resolve()
                if not report_path.is_relative_to(PROJECT_ROOT.resolve()):
                    errors.append("process-tree cleanup certification escapes project root")
                elif not report_path.is_file():
                    errors.append("process-tree cleanup certification report is unavailable")
                else:
                    actual_sha = file_sha256(report_path)
                    if actual_sha != expected_sha:
                        errors.append("process-tree cleanup certification hash differs")
                    try:
                        cleanup_report = json.loads(report_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError) as exc:
                        errors.append(f"invalid process-tree cleanup certification: {exc}")
                    else:
                        implementation = (
                            PROJECT_ROOT / "week9" / "experiments" / "subprocess_control.py"
                        )
                        if (
                            cleanup_report.get("verdict")
                            != "PROCESS_TREE_CLEANUP_PASS"
                            or cleanup_report.get("timeout_trial_count", 0) < 3
                            or cleanup_report.get("timeout_pass_count")
                            != cleanup_report.get("timeout_trial_count")
                            or cleanup_report.get("implementation_sha256")
                            != file_sha256(implementation)
                            or cleanup_report.get("online_api_used") is not False
                            or cleanup_report.get("holdout_accessed") is not False
                        ):
                            errors.append(
                                "process-tree cleanup certification semantics do not pass"
                            )
    if online.get("current_milestone_allows_online_calls") and not online.get("user_confirmation_id"):
        errors.append("online calls enabled without a user confirmation record")

    holdout = protocol.get("holdout_policy", {})
    if holdout.get("access_enabled") and not protocol.get("formal_online_runs_allowed"):
        errors.append("holdout access enabled before formal online runs")

    restart = protocol.get("formal_restart_preflight")
    replacement_status = str(protocol.get("status") or "").startswith("replacement_formal_")
    if replacement_status and not isinstance(restart, dict):
        errors.append("replacement formal run lacks a frozen restart preflight")
    if isinstance(restart, dict):
        expected_restart_fields = {
            "aborted_completed_run_count": 9,
            "aborted_paid_deepseek_call_count": 7,
            "aborted_holdout_accessed": False,
            "reuse_aborted_runs": False,
            "replacement_planned_run_count": 100,
        }
        for name, expected in expected_restart_fields.items():
            if restart.get(name) != expected:
                errors.append(f"formal restart boundary differs: {name}")

        restart_report = None
        for label, path_key, hash_key in (
            ("preflight", "report_path", "report_sha256"),
            (
                "aborted_attempt_marker",
                "aborted_attempt_marker_path",
                "aborted_attempt_marker_sha256",
            ),
        ):
            relative = restart.get(path_key)
            expected_sha = restart.get(hash_key)
            if not isinstance(relative, str) or not relative:
                errors.append(f"formal restart evidence path is missing: {label}")
                continue
            evidence_path = (PROJECT_ROOT / relative).resolve()
            if not evidence_path.is_relative_to(PROJECT_ROOT.resolve()):
                errors.append(f"formal restart evidence escapes project root: {label}")
                continue
            if not evidence_path.is_file():
                errors.append(f"formal restart evidence is unavailable: {label}")
                continue
            if not isinstance(expected_sha, str) or file_sha256(evidence_path) != expected_sha:
                errors.append(f"formal restart evidence hash differs: {label}")
                continue
            if label == "preflight":
                try:
                    value = json.loads(evidence_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    errors.append(f"formal restart preflight is invalid: {exc}")
                else:
                    if isinstance(value, dict):
                        restart_report = value
                    else:
                        errors.append("formal restart preflight is not an object")

        if restart_report is not None:
            guard = restart_report.get("guard", {})
            pass_conditions = restart_report.get("pass_conditions", {})
            if (
                restart_report.get("schema_version")
                != "v8-formal-local-preflight-1"
                or restart_report.get("verdict") != "FORMAL_LOCAL_PREFLIGHT_PASS"
                or restart_report.get("planned_run_count") != 3
                or restart_report.get("completed_run_count") != 3
                or guard.get("development_cases_only") is not True
                or guard.get("holdout_accessed") is not False
                or guard.get("online_generator_called") is not False
                or guard.get("external_evaluator")
                != "week9.cross_version_validation.v1"
                or not isinstance(pass_conditions, dict)
                or not pass_conditions
                or any(value is not True for value in pass_conditions.values())
            ):
                errors.append("formal restart preflight semantics do not pass")

    latest_attempt = protocol.get("latest_formal_attempt")
    aborted_after_holdout = "aborted_22_of_100_holdout_timeout" in str(
        protocol.get("status") or ""
    )
    if aborted_after_holdout and not isinstance(latest_attempt, dict):
        errors.append("aborted holdout attempt lacks a frozen audit record")
    if isinstance(latest_attempt, dict):
        expected_attempt_fields = {
            "completed_run_count": 22,
            "paid_deepseek_call_count": 16,
            "deterministic_local_run_count": 6,
            "development_run_count": 14,
            "holdout_run_count": 8,
            "holdout_accessed": True,
            "formal_report_produced": False,
            "formal_judge_called": False,
            "infrastructure_failure_count_min": 1,
            "process_tree_cleanup_succeeded": True,
            "post_holdout_tuning_allowed": False,
        }
        for name, expected in expected_attempt_fields.items():
            if latest_attempt.get(name) != expected:
                errors.append(f"latest formal attempt boundary differs: {name}")

        timeout_result = None
        for label, path_key, hash_key in (
            (
                "incomplete_marker",
                "incomplete_marker_path",
                "incomplete_marker_sha256",
            ),
            ("timeout_result", "timeout_result_path", "timeout_result_sha256"),
        ):
            relative = latest_attempt.get(path_key)
            expected_sha = latest_attempt.get(hash_key)
            if not isinstance(relative, str) or not relative:
                errors.append(f"latest formal attempt evidence path is missing: {label}")
                continue
            evidence_path = (PROJECT_ROOT / relative).resolve()
            if not evidence_path.is_relative_to(PROJECT_ROOT.resolve()):
                errors.append(f"latest formal attempt evidence escapes project root: {label}")
                continue
            if not evidence_path.is_file():
                errors.append(f"latest formal attempt evidence is unavailable: {label}")
                continue
            if not isinstance(expected_sha, str) or file_sha256(evidence_path) != expected_sha:
                errors.append(f"latest formal attempt evidence hash differs: {label}")
                continue
            if label == "timeout_result":
                try:
                    value = json.loads(evidence_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    errors.append(f"latest formal timeout result is invalid: {exc}")
                else:
                    if isinstance(value, dict):
                        timeout_result = value
                    else:
                        errors.append("latest formal timeout result is not an object")

        if timeout_result is not None:
            adapter_checks = [
                item
                for item in timeout_result.get("checks", [])
                if isinstance(item, dict) and item.get("check_type") == "adapter.execution"
            ]
            raw_metrics = timeout_result.get("raw_metrics", {})
            if (
                timeout_result.get("case_id") != "three_room_thermal_chain"
                or timeout_result.get("version") != "v6"
                or timeout_result.get("overall_success") is not False
                or timeout_result.get("primary_failure_category") != "legacy_runner_error"
                or "adapter timeout after 900.0s"
                not in timeout_result.get("infrastructure_errors", [])
                or len(adapter_checks) != 1
                or adapter_checks[0].get("status") != "ERROR"
                or adapter_checks[0].get("failure_kind") != "infrastructure"
                or raw_metrics.get("process_tree_cleanup_attempted") is not True
                or raw_metrics.get("process_tree_cleanup_succeeded") is not True
            ):
                errors.append("latest formal timeout evidence semantics differ")

        attempt_directory = latest_attempt.get("attempt_directory")
        if not isinstance(attempt_directory, str) or not attempt_directory:
            errors.append("latest formal attempt directory is missing")
        else:
            directory_path = (PROJECT_ROOT / attempt_directory).resolve()
            if (
                not directory_path.is_relative_to(PROJECT_ROOT.resolve())
                or not directory_path.is_dir()
            ):
                errors.append("latest formal attempt directory is unavailable or unsafe")

    thresholds = protocol.get("release_thresholds", {})
    threshold_freeze = protocol.get("release_threshold_freeze")
    if not isinstance(threshold_freeze, dict):
        errors.append("release thresholds lack a frozen evidence record")
    else:
        evidence_objects: dict[str, dict[str, Any]] = {}
        for label, path_key, hash_key in (
            ("proposal", "proposal_path", "proposal_sha256"),
            ("pilot", "pilot_report_path", "pilot_report_sha256"),
            (
                "judge_stability",
                "judge_stability_report_path",
                "judge_stability_report_sha256",
            ),
        ):
            relative = threshold_freeze.get(path_key)
            expected_sha = threshold_freeze.get(hash_key)
            if not isinstance(relative, str) or not relative:
                errors.append(f"threshold freeze evidence path is missing: {label}")
                continue
            evidence_path = (PROJECT_ROOT / relative).resolve()
            if not evidence_path.is_relative_to(PROJECT_ROOT.resolve()):
                errors.append(f"threshold freeze evidence escapes project root: {label}")
                continue
            if not evidence_path.is_file():
                errors.append(f"threshold freeze evidence is unavailable: {label}")
                continue
            if not isinstance(expected_sha, str) or file_sha256(evidence_path) != expected_sha:
                errors.append(f"threshold freeze evidence hash differs: {label}")
                continue
            try:
                value = json.loads(evidence_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"threshold freeze evidence is invalid: {label}: {exc}")
                continue
            if not isinstance(value, dict):
                errors.append(f"threshold freeze evidence is not an object: {label}")
                continue
            evidence_objects[label] = value

        proposal = evidence_objects.get("proposal", {})
        pilot_evidence = evidence_objects.get("pilot", {})
        judge_evidence = evidence_objects.get("judge_stability", {})
        proposed_thresholds = proposal.get("thresholds", {})
        frozen_numeric = {
            name: thresholds.get(name)
            for name in (
                "development_success_improvement_min",
                "holdout_noninferiority_margin",
                "simple_case_token_budget",
                "simple_case_duration_budget_s",
                "max_content_repairs",
            )
        }
        if (
            proposal.get("schema_version") != "v8-release-threshold-proposal-1"
            or proposal.get("verdict") != "THRESHOLD_PROPOSAL_READY"
            or proposal.get("guard", {}).get("holdout_accessed") is not False
            or proposal.get("guard", {}).get("v8_formal_result_read") is not False
            or proposed_thresholds != frozen_numeric
        ):
            errors.append("release threshold proposal semantics or values differ")
        if (
            pilot_evidence.get("schema_version") != "v8-contemporaneous-pilot-1"
            or pilot_evidence.get("verdict") != "PILOT_PASS"
            or pilot_evidence.get("guard", {}).get("holdout_accessed") is not False
        ):
            errors.append("release threshold Pilot evidence does not pass")
        if (
            judge_evidence.get("schema_version") != "v8-judge-stability-1"
            or judge_evidence.get("verdict") != "JUDGE_STABILITY_PASS"
            or judge_evidence.get("guard", {}).get("holdout_accessed") is not False
        ):
            errors.append("release threshold Judge evidence does not pass")
        if (
            threshold_freeze.get("frozen_before_formal_v8_results") is not True
            or threshold_freeze.get("holdout_accessed") is not False
        ):
            errors.append("release threshold freeze boundary differs")

    if protocol.get("formal_online_runs_allowed", False):
        required_numeric = (
            "development_success_improvement_min",
            "holdout_noninferiority_margin",
            "simple_case_token_budget",
            "simple_case_duration_budget_s",
            "max_content_repairs",
        )
        for name in required_numeric:
            if not _finite_number(thresholds.get(name)):
                errors.append(f"formal run enabled before numeric threshold was frozen: {name}")
        max_repairs = thresholds.get("max_content_repairs")
        if _finite_number(max_repairs) and float(max_repairs) < 0:
            errors.append("max_content_repairs must be non-negative")
        for name in (
            "development_success_improvement_min",
            "holdout_noninferiority_margin",
            "simple_case_token_budget",
            "simple_case_duration_budget_s",
        ):
            value = thresholds.get(name)
            if _finite_number(value) and float(value) < 0:
                errors.append(f"formal threshold must be non-negative: {name}")
    return errors


def _finite_number(value: Any) -> bool:
    return type(value) in {int, float} and math.isfinite(float(value))


def _finite_number_at_least(value: Any, minimum: float) -> bool:
    return _finite_number(value) and float(value) >= minimum


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check V8 frozen protocol hashes")
    parser.add_argument("--protocol", default=str(DEFAULT_PROTOCOL))
    parser.add_argument("--print-snapshot", action="store_true")
    parser.add_argument("--print-protocol-digest", action="store_true")
    args = parser.parse_args(argv)

    if args.print_snapshot:
        print(json.dumps(current_snapshot(), ensure_ascii=False, indent=2))
        return 0

    path = Path(args.protocol)
    protocol = json.loads(path.read_text(encoding="utf-8"))
    if args.print_protocol_digest:
        print(canonical_protocol_sha256(protocol))
        return 0
    errors = validate_protocol(protocol)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("V8 experiment protocol hashes are unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
