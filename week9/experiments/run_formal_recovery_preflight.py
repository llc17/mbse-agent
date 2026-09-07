"""Run the offline fault-injection preflight for the V8 recovery policy."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from typing import Any

from week9.experiments.formal_recovery import (
    AttemptDisposition,
    AttemptObservation,
    CandidateContractStatus,
    ExternalEvaluationStatus,
    TerminationReason,
    classify_attempt,
    observation_from_process_result,
    recovery_policy_manifest,
)
from week9.experiments.formal_recovery_harness import (
    RecoveryAttemptSpec,
    execute_recovery_attempt,
)
from week9.experiments.subprocess_control import ControlledProcessResult


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = PROJECT_ROOT / "week9" / "results"
PREFLIGHT_DEADLINE_S = 0.5


def execute_synthetic_scenarios() -> list[dict[str, Any]]:
    """Exercise real process capture plus structured failure injection."""

    with tempfile.TemporaryDirectory(prefix="v8-recovery-preflight-") as temp_dir:
        root = Path(temp_dir).resolve()

        def spec(attempt_id: str, command: list[str]) -> RecoveryAttemptSpec:
            return RecoveryAttemptSpec.build(
                attempt_id=attempt_id,
                command=command,
                cwd=root,
                workspace_root=root,
                candidate_contract_path=root / attempt_id / "candidate.json",
            )

        def writer_command(payload: str, *, returncode: int = 0) -> list[str]:
            script = (
                "from pathlib import Path;import sys;"
                "Path(sys.argv[1]).write_text(sys.argv[2],encoding='utf-8');"
                "raise SystemExit(int(sys.argv[3]))"
            )
            return [sys.executable, "-c", script, "PLACEHOLDER", payload, str(returncode)]

        def run_writer(
            attempt_id: str, payload: str, *, returncode: int = 0
        ) -> dict[str, Any]:
            attempt_spec = spec(attempt_id, writer_command(payload, returncode=returncode))
            command = list(attempt_spec.command)
            command[3] = str(attempt_spec.candidate_contract_path)
            attempt_spec = spec(attempt_id, command)
            return execute_recovery_attempt(
                attempt_spec,
                deadline_s=10.0,
                external_evaluator=_synthetic_external_evaluator,
            ).to_dict()

        records = [
            run_writer("normal-success", '{"external_success":true}'),
            run_writer("external-hard-fail", '{"external_success":false}'),
            run_writer(
                "nonzero-valid-external-pass",
                '{"external_success":true}',
                returncode=2,
            ),
            execute_recovery_attempt(
                spec(
                    "missing-candidate-contract",
                    [sys.executable, "-c", "print('no candidate contract')"],
                ),
                deadline_s=10.0,
                external_evaluator=_synthetic_external_evaluator,
            ).to_dict(),
            run_writer("invalid-candidate-contract", "not-json"),
            execute_recovery_attempt(
                spec(
                    "nonzero-arm-exit",
                    [sys.executable, "-c", "raise SystemExit(2)"],
                ),
                deadline_s=10.0,
                external_evaluator=_synthetic_external_evaluator,
            ).to_dict(),
            execute_recovery_attempt(
                spec(
                    "real-deadline-cleanup-success",
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                ),
                deadline_s=PREFLIGHT_DEADLINE_S,
                external_evaluator=_synthetic_external_evaluator,
            ).to_dict(),
            execute_recovery_attempt(
                spec(
                    "injected-process-launch-failure",
                    ["v8-recovery-definitely-missing-executable"],
                ),
                deadline_s=10.0,
                external_evaluator=_synthetic_external_evaluator,
            ).to_dict(),
            run_writer("injected-evaluator-error", '{"raise_evaluator":true}'),
        ]

    cleanup_failure_process = ControlledProcessResult(
        returncode=None,
        stdout="",
        stderr="",
        timed_out=True,
        cleanup_attempted=True,
        cleanup_succeeded=False,
        cleanup_detail="injected cleanup failure",
    )
    cleanup_failure = observation_from_process_result(
        attempt_id="injected-deadline-cleanup-failure",
        process_result=cleanup_failure_process,
        candidate_contract_status=CandidateContractStatus.NOT_APPLICABLE,
        external_evaluation_status=ExternalEvaluationStatus.NOT_RUN,
        duration_s=PREFLIGHT_DEADLINE_S,
        deadline_s=PREFLIGHT_DEADLINE_S,
    )
    harness_error = AttemptObservation(
        attempt_id="injected-harness-error",
        termination_reason=TerminationReason.HARNESS_ERROR,
        process_started=True,
        process_returncode=None,
        candidate_contract_status=CandidateContractStatus.NOT_APPLICABLE,
        external_evaluation_status=ExternalEvaluationStatus.ERROR,
        cleanup_attempted=False,
        cleanup_succeeded=None,
        duration_s=0.01,
        detail="injected outer harness error",
    )
    records.extend(
        [
            {
                "observation": cleanup_failure.to_dict(),
                "decision": classify_attempt(cleanup_failure).to_dict(),
                "candidate_contract_sha256": "",
            },
            {
                "observation": harness_error.to_dict(),
                "decision": classify_attempt(harness_error).to_dict(),
                "candidate_contract_sha256": "",
            },
        ]
    )
    return records


def _synthetic_external_evaluator(payload: dict[str, Any] | Any) -> bool:
    if payload.get("raise_evaluator") is True:
        raise RuntimeError("injected evaluator failure")
    value = payload.get("external_success")
    if type(value) is not bool:
        raise ValueError("synthetic external_success must be a bool")
    return value


def build_preflight_report(records: list[dict[str, Any]]) -> dict[str, Any]:
    expected = {
        "normal-success": (
            AttemptDisposition.VALID_SUCCESS.value,
            "",
        ),
        "external-hard-fail": (
            AttemptDisposition.VALID_ARM_FAILURE.value,
            "external_evaluation_failed",
        ),
        "nonzero-valid-external-pass": (
            AttemptDisposition.VALID_SUCCESS.value,
            "",
        ),
        "missing-candidate-contract": (
            AttemptDisposition.VALID_ARM_FAILURE.value,
            "candidate_contract_missing",
        ),
        "invalid-candidate-contract": (
            AttemptDisposition.VALID_ARM_FAILURE.value,
            "candidate_contract_invalid",
        ),
        "nonzero-arm-exit": (
            AttemptDisposition.VALID_ARM_FAILURE.value,
            "arm_process_nonzero_exit",
        ),
        "real-deadline-cleanup-success": (
            AttemptDisposition.VALID_ARM_FAILURE.value,
            "arm_deadline_exceeded",
        ),
        "injected-deadline-cleanup-failure": (
            AttemptDisposition.INVALID_INFRASTRUCTURE.value,
            "process_tree_cleanup_failed",
        ),
        "injected-process-launch-failure": (
            AttemptDisposition.INVALID_INFRASTRUCTURE.value,
            "process_launch_failed",
        ),
        "injected-evaluator-error": (
            AttemptDisposition.INVALID_INFRASTRUCTURE.value,
            "external_evaluator_error",
        ),
        "injected-harness-error": (
            AttemptDisposition.INVALID_INFRASTRUCTURE.value,
            "experiment_harness_error",
        ),
    }
    actual = {
        record["observation"]["attempt_id"]: (
            record["decision"]["disposition"],
            record["decision"]["failure_category"],
        )
        for record in records
    }
    deadline_record = next(
        (
            record
            for record in records
            if record["observation"]["attempt_id"]
            == "real-deadline-cleanup-success"
        ),
        None,
    )
    pass_conditions = {
        "scenario_scope_exact": set(actual) == set(expected),
        "all_classifications_match": actual == expected,
        "real_deadline_observed": bool(deadline_record)
        and deadline_record["observation"]["termination_reason"]
        == TerminationReason.DEADLINE_EXCEEDED.value,
        "real_deadline_cleanup_succeeded": bool(deadline_record)
        and deadline_record["observation"]["cleanup_attempted"] is True
        and deadline_record["observation"]["cleanup_succeeded"] is True,
        "deadline_is_completed_arm_failure": bool(deadline_record)
        and deadline_record["decision"]["counts_as_completed_run"] is True
        and deadline_record["decision"]["overall_success"] is False
        and deadline_record["decision"]["infrastructure_failure"] is False,
        "infrastructure_faults_never_count_as_completed": all(
            record["decision"]["counts_as_completed_run"] is False
            for record in records
            if record["decision"]["disposition"]
            == AttemptDisposition.INVALID_INFRASTRUCTURE.value
        ),
    }
    return {
        "schema_version": "v8-formal-recovery-preflight-1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "verdict": (
            "RECOVERY_PREFLIGHT_PASS"
            if all(pass_conditions.values())
            else "RECOVERY_PREFLIGHT_FAIL"
        ),
        "guard": {
            "external_network_allowed": False,
            "deepseek_called": False,
            "kimi_called": False,
            "dataset_content_read": False,
            "old_holdout_accessed": False,
            "new_holdout_accessed": False,
            "historical_run_results_read": False,
            "synthetic_fault_injection_only": True,
        },
        "preflight_deadline_s": PREFLIGHT_DEADLINE_S,
        "formal_policy": recovery_policy_manifest(),
        "planned_scenario_count": len(expected),
        "completed_scenario_count": len(records),
        "pass_conditions": pass_conditions,
        "records": records,
    }


def run_preflight(*, output_root: Path | None = None) -> tuple[dict[str, Any], Path]:
    records = execute_synthetic_scenarios()
    report = build_preflight_report(records)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    root = (output_root or RESULTS_ROOT).resolve()
    result_dir = root / f"formal-recovery-preflight-{timestamp}"
    result_dir.mkdir(parents=True, exist_ok=False)
    report_path = result_dir / "formal-recovery-preflight-report.json"
    _write_json(report_path, report)
    digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
    report_path.with_suffix(".sha256").write_text(
        f"{digest}  {report_path.name}\n", encoding="ascii"
    )
    (result_dir / "formal-recovery-preflight-summary.md").write_text(
        "# V8 formal recovery offline preflight\n\n"
        f"- Verdict: `{report['verdict']}`\n"
        f"- Synthetic scenarios: {report['completed_scenario_count']}/"
        f"{report['planned_scenario_count']}\n"
        "- Network/model calls: 0\n"
        "- Dataset and historical run-result access: 0\n",
        encoding="utf-8",
    )
    return report, result_dir


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    report, result_dir = run_preflight()
    print(f"V8 formal recovery preflight: {report['verdict']} -> {result_dir}")
    return 0 if report["verdict"] == "RECOVERY_PREFLIGHT_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PREFLIGHT_DEADLINE_S",
    "build_preflight_report",
    "execute_synthetic_scenarios",
    "main",
    "run_preflight",
]
