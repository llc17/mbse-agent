"""Run the V8-local development dry-run for the formal recovery adapter."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from week9.experiments.cases import DEVELOPMENT_REGRESSION_CASES
from week9.experiments.formal_recovery import recovery_policy_manifest
from week9.experiments.formal_recovery_adapters import run_development_recovery_arm
from week9.experiments.freeze_formal_recovery_protocol import (
    DEFAULT_PROTOCOL,
    validate_recovery_protocol,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = PROJECT_ROOT / "week9" / "results"
DEVELOPMENT_CASE_IDS = (
    "rc_lowpass",
    "rlc_lowpass",
    "dual_room_thermal",
)


def run_adapter_dry_run(
    *, output_root: Path | None = None
) -> tuple[dict[str, Any], Path]:
    protocol = _read_object(DEFAULT_PROTOCOL)
    protocol_errors = validate_recovery_protocol(protocol)
    if protocol_errors:
        raise ValueError(
            "recovery protocol is invalid: " + "; ".join(protocol_errors)
        )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = (output_root or RESULTS_ROOT).resolve()
    result_dir = root / f"frd-{timestamp}"
    result_dir.mkdir(parents=True, exist_ok=False)
    adapter_root = result_dir / "a"
    records: list[dict[str, Any]] = []
    for sequence, case_id in enumerate(DEVELOPMENT_CASE_IDS, start=1):
        case = DEVELOPMENT_REGRESSION_CASES[case_id]
        adapter_run = run_development_recovery_arm(
            "v8_default_local",
            case,
            adapter_root,
        )
        relative = Path("runs") / f"{sequence:03d}-{case_id}.json"
        path = result_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(path, adapter_run.to_dict())
        result = adapter_run.externally_validated_result
        records.append(
            {
                "sequence": sequence,
                "case_id": case_id,
                "case_sha256": case.sha256,
                "arm_id": adapter_run.arm_id,
                "run_id": adapter_run.harness_result.observation.attempt_id,
                "record_path": relative.as_posix(),
                "record_sha256": _raw_sha256(path),
                "disposition": adapter_run.harness_result.decision.disposition.value,
                "counts_as_completed_run": (
                    adapter_run.harness_result.decision.counts_as_completed_run
                ),
                "overall_success": adapter_run.harness_result.decision.overall_success,
                "infrastructure_failure": (
                    adapter_run.harness_result.decision.infrastructure_failure
                ),
                "failure_category": (
                    adapter_run.harness_result.decision.failure_category
                ),
                "external_result_available": result is not None,
                "external_evaluator": (
                    result.raw_metrics.get("external_evaluator") if result else None
                ),
                "candidate_online_api_used": (
                    _candidate_online_api_used(result) if result else None
                ),
            }
        )
        print(
            f"[{sequence:02d}/{len(DEVELOPMENT_CASE_IDS)}] {case_id} "
            f"disposition={records[-1]['disposition']} "
            f"success={records[-1]['overall_success']}",
            flush=True,
        )

    pass_conditions = {
        "protocol_v2_valid_before_dry_run": not protocol_errors,
        "development_scope_exact": [item["case_id"] for item in records]
        == list(DEVELOPMENT_CASE_IDS),
        "all_runs_count_as_completed": all(
            item["counts_as_completed_run"] is True for item in records
        ),
        "no_infrastructure_failures": all(
            item["infrastructure_failure"] is False for item in records
        ),
        "all_external_results_available": all(
            item["external_result_available"] is True for item in records
        ),
        "external_evaluator_exact": all(
            item["external_evaluator"] == "week9.cross_version_validation.v1"
            for item in records
        ),
        "all_development_cases_pass": all(
            item["overall_success"] is True for item in records
        ),
        "candidate_online_api_never_used": all(
            item["candidate_online_api_used"] is False for item in records
        ),
    }
    report = {
        "schema_version": "v8-formal-recovery-adapter-dry-run-1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "verdict": (
            "RECOVERY_ADAPTER_DRY_RUN_PASS"
            if all(pass_conditions.values())
            else "RECOVERY_ADAPTER_DRY_RUN_FAIL"
        ),
        "guard": {
            "development_cases_only": True,
            "old_holdout_accessed": False,
            "new_holdout_accessed": False,
            "deepseek_called": False,
            "kimi_called": False,
            "external_network_allowed": False,
            "credentials_forwarded_to_child": False,
            "v8_default_local_only": True,
        },
        "outcome_policy_sha256": _canonical_sha256(recovery_policy_manifest()),
        "planned_run_count": len(DEVELOPMENT_CASE_IDS),
        "completed_run_count": len(records),
        "pass_conditions": pass_conditions,
        "records": records,
    }
    report_path = result_dir / "formal-recovery-adapter-dry-run-report.json"
    _write_json(report_path, report)
    report_path.with_suffix(".sha256").write_text(
        f"{_raw_sha256(report_path)}  {report_path.name}\n", encoding="ascii"
    )
    (result_dir / "formal-recovery-adapter-dry-run-summary.md").write_text(
        "# V8 formal recovery adapter dry-run\n\n"
        f"- Verdict: `{report['verdict']}`\n"
        f"- V8 local development runs: {len(records)}/{len(DEVELOPMENT_CASE_IDS)}\n"
        "- Paid/model calls: 0\n"
        "- Holdout access: 0\n",
        encoding="utf-8",
    )
    return report, result_dir


def _candidate_online_api_used(result: Any) -> bool | None:
    candidate = result.raw_metrics.get("candidate_raw_metrics")
    if isinstance(candidate, Mapping):
        value = candidate.get("online_api_used")
        if type(value) is bool:
            return value
    value = result.raw_metrics.get("online_api_used")
    return value if type(value) is bool else None


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _raw_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    report, result_dir = run_adapter_dry_run()
    print(f"V8 recovery adapter dry-run: {report['verdict']} -> {result_dir}")
    return 0 if report["verdict"] == "RECOVERY_ADAPTER_DRY_RUN_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["DEVELOPMENT_CASE_IDS", "main", "run_adapter_dry_run"]
