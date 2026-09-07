"""Run the independently sealed, four-arm, 100-attempt V8 comparison.

Importing this module is offline.  The CLI refuses to load the sealed bundle,
inspect credentials, or launch a child unless an additive one-batch receipt is
valid and ``--execute-formal`` is explicit.  Raw candidates and evaluator
artifacts remain under the private results root; only content-free records are
written into the project.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any, Mapping
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from week9.experiments.blinded_formal_execution import (  # noqa: E402
    DEFAULT_FINAL_PROTOCOL,
    DEFAULT_PRIVATE_RESULTS_ROOT,
    FAIL_VERDICT,
    FORMAL_DEADLINE_S,
    FORMAL_REPETITIONS,
    INCOMPLETE_VERDICT,
    PASS_VERDICT,
    PINNED_DEEPSEEK_MODEL,
    PINNED_DEEPSEEK_THINKING_MODE,
    build_blinded_schedule,
    build_public_formal_report,
    consume_execution_authorization,
    file_sha256,
    load_sealed_holdout,
    public_result_record,
    validate_execution_authorization,
)
from week9.experiments.formal_recovery_harness import (  # noqa: E402
    RecoveryAttemptSpec,
    execute_recovery_attempt,
)
from week9.experiments.freeze_independent_holdout_binding import (  # noqa: E402
    DEFAULT_BINDING,
)
from week9.src.quality_gate import apply_to_run_result  # noqa: E402
from week9.src.schemas import RunResult  # noqa: E402


PUBLIC_RESULTS_ROOT = PROJECT_ROOT / "week9" / "results"
WORKER = PROJECT_ROOT / "week9" / "experiments" / "blinded_arm_worker.py"
SYSTEM_ENVIRONMENT_KEYS = {
    "COMSPEC",
    "LANG",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "PYTHONIOENCODING",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "WINDIR",
}
PROVIDER_ENVIRONMENT_KEYS = {
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_API_BASE",
    "DEEPSEEK_MODEL",
}


def build_worker_command(
    schedule_item: Mapping[str, Any],
    *,
    protocol_path: Path,
    authorization_path: Path,
    consumption_marker: Path,
    binding_path: Path,
    run_id: str,
    work_root: Path,
    candidate_path: Path,
) -> tuple[str, ...]:
    """Build one content-free operating-system command line."""

    return (
        sys.executable,
        "-B",
        str(WORKER),
        "--protocol",
        str(protocol_path),
        "--authorization",
        str(authorization_path),
        "--consumption-marker",
        str(consumption_marker),
        "--binding",
        str(binding_path),
        "--dataset",
        str(schedule_item["dataset"]),
        "--case-ref",
        str(schedule_item["case_ref"]),
        "--arm-id",
        str(schedule_item["arm_id"]),
        "--run-id",
        run_id,
        "--work-root",
        str(work_root),
        "--out",
        str(candidate_path),
    )


def run_blinded_formal_comparison(
    *,
    confirmation_id: str,
    authorization_path: Path,
    protocol_path: Path = DEFAULT_FINAL_PROTOCOL,
    binding_path: Path = DEFAULT_BINDING,
    public_output_root: Path = PUBLIC_RESULTS_ROOT,
) -> tuple[dict[str, Any], Path]:
    """Execute the one authorized batch; this is the only paid entry point."""

    protocol_path = protocol_path.resolve()
    authorization_path = authorization_path.resolve()
    binding_path = binding_path.resolve()
    protocol, _authorization, errors = validate_execution_authorization(
        protocol_path,
        authorization_path,
        confirmation_id=confirmation_id,
    )
    if errors:
        raise PermissionError("one-batch execution authorization is invalid")
    try:
        from week9.experiments.freeze_blinded_formal_protocol import (
            validate_blinded_formal_protocol,
        )
    except ImportError as exc:
        raise PermissionError("final blinded protocol verifier is unavailable") from exc
    protocol_errors = validate_blinded_formal_protocol(protocol)
    if protocol_errors:
        raise PermissionError("final blinded protocol is not frozen")
    if binding_path != (PROJECT_ROOT / str(protocol["holdout_binding"]["path"])).resolve():
        raise PermissionError("holdout binding path differs from final protocol")

    private_root = Path(str(protocol["private_execution_boundary"]["root"])).resolve()
    if private_root != DEFAULT_PRIVATE_RESULTS_ROOT.resolve():
        raise PermissionError("private execution root differs from final protocol")
    _prepare_private_root(private_root)
    environment = controlled_worker_environment(os.environ)
    if not environment.get("DEEPSEEK_API_KEY"):
        raise PermissionError("DeepSeek credential is unavailable after authorization")
    _validate_pinned_provider_environment(environment, protocol)
    bundle = load_sealed_holdout(binding_path)
    if bundle.manifest_sha256 != protocol["holdout_binding"]["manifest_sha256"]:
        raise PermissionError("sealed bundle manifest differs from final protocol")
    schedule = build_blinded_schedule(
        FORMAL_REPETITIONS,
        manifest_sha256=bundle.manifest_sha256,
    )
    if len(schedule) != 100:
        raise PermissionError("formal schedule does not contain exactly 100 attempts")
    from week9.experiments.cross_version_validation import externally_validate

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    batch_id = f"v8bf_{timestamp}"
    private_batch = (private_root / batch_id).resolve()
    public_root = public_output_root.resolve()
    if not public_root.is_relative_to(PROJECT_ROOT):
        raise ValueError("public output root must remain inside the project")
    public_batch = public_root / f"blinded-formal-{timestamp}"
    private_batch.mkdir(parents=True, exist_ok=False)
    public_batch.mkdir(parents=True, exist_ok=False)
    (private_batch / "attempts").mkdir()
    protocol_sha256 = protocol_path.with_suffix(".sha256").read_text(
        encoding="ascii"
    ).strip()
    consumption = consume_execution_authorization(
        authorization_path,
        protocol_sha256=protocol_sha256,
        batch_id=batch_id,
    )

    records: list[dict[str, Any]] = []
    aborted = False
    for item in schedule:
        sequence = int(item["sequence"])
        attempt_id = f"{batch_id}_s{sequence:03d}_{item['arm_id']}"
        attempt_root = private_batch / "attempts" / f"{sequence:03d}"
        candidate_path = attempt_root / "candidate.run-result.json"
        work_root = attempt_root / "work"
        command = build_worker_command(
            item,
            protocol_path=protocol_path,
            authorization_path=authorization_path,
            consumption_marker=consumption,
            binding_path=binding_path,
            run_id=attempt_id,
            work_root=work_root,
            candidate_path=candidate_path,
        )
        case = _case_for_schedule_item(item, bundle)
        captured: dict[str, Any] = {}

        def common_external_evaluator(payload: Mapping[str, Any]) -> bool:
            candidate = apply_to_run_result(RunResult.from_dict(payload))
            if (
                candidate.version.lower() != str(item["version"])
                or candidate.case_id != case.case_id
                or candidate.run_id != attempt_id
            ):
                raise ValueError("candidate identity differs from blinded schedule")
            external = externally_validate(
                candidate,
                case,
                workspace_root=attempt_root / "external-evaluation",
            )
            evaluated_path = attempt_root / "externally-validated.run-result.json"
            evaluated_path.write_text(
                json.dumps(
                    external.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                    allow_nan=False,
                )
                + "\n",
                encoding="utf-8",
            )
            captured["result"] = external
            captured["sha256"] = file_sha256(evaluated_path)
            return external.overall_success

        harness = execute_recovery_attempt(
            RecoveryAttemptSpec.build(
                attempt_id=attempt_id,
                command=command,
                cwd=private_batch,
                workspace_root=private_batch,
                candidate_contract_path=candidate_path,
            ),
            deadline_s=FORMAL_DEADLINE_S,
            external_evaluator=common_external_evaluator,
            environment=environment,
        )
        record = public_result_record(
            item,
            harness,
            captured.get("result"),
            evaluated_result_sha256=str(captured.get("sha256") or ""),
            thresholds=protocol["release_thresholds"],
        )
        records.append(record)
        _write_public_progress(public_batch, schedule, records, protocol_sha256)
        print(
            f"[{sequence:03d}/{len(schedule)}] case_ref={item['case_ref']} "
            f"arm_id={item['arm_id']} disposition={record['disposition']}",
            flush=True,
        )
        if harness.decision.infrastructure_failure:
            aborted = True
            break

    report = build_public_formal_report(
        schedule=schedule,
        records=records,
        protocol=protocol,
        protocol_sha256=protocol_sha256,
        binding_sha256=str(protocol["holdout_binding"]["sha256"]),
        authorization_sha256=file_sha256(authorization_path),
        aborted=aborted,
    )
    report_path = public_batch / "blinded-formal-report.json"
    _write_json(report_path, report)
    report_path.with_suffix(".sha256").write_text(
        file_sha256(report_path) + "\n", encoding="ascii"
    )
    private_receipt = {
        "schema_version": "v8-blinded-private-batch-receipt-2",
        "batch_id": batch_id,
        "protocol_sha256": protocol_sha256,
        "authorization_receipt_sha256": file_sha256(authorization_path),
        "public_report_path": str(report_path),
        "public_report_sha256": file_sha256(report_path),
        "planned_run_count": len(schedule),
        "record_count": len(records),
        "aborted": aborted,
    }
    _write_json(private_batch / "private-batch-receipt.json", private_receipt)
    return report, public_batch


def controlled_worker_environment(source: Mapping[str, str]) -> dict[str, str]:
    """Forward only operating-system essentials and the frozen DeepSeek profile."""

    allowed = SYSTEM_ENVIRONMENT_KEYS | PROVIDER_ENVIRONMENT_KEYS
    environment = {key: value for key, value in source.items() if key.upper() in allowed}
    environment["PYTHONPATH"] = str(PROJECT_ROOT)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["V8_BLINDED_FORMAL_EXECUTION"] = "1"
    return environment


def _validate_pinned_provider_environment(
    environment: Mapping[str, str], protocol: Mapping[str, Any]
) -> None:
    profile = protocol.get("generator_profile")
    if not isinstance(profile, Mapping):
        raise PermissionError("frozen generator profile is unavailable")
    if (
        environment.get("DEEPSEEK_MODEL") != PINNED_DEEPSEEK_MODEL
        or profile.get("model_id") != PINNED_DEEPSEEK_MODEL
        or profile.get("thinking_mode") != PINNED_DEEPSEEK_THINKING_MODE
    ):
        raise PermissionError("DeepSeek runtime profile differs from the frozen protocol")
    api_base = environment.get("DEEPSEEK_API_BASE", "https://api.deepseek.com")
    if urlparse(api_base).hostname != "api.deepseek.com":
        raise PermissionError("DeepSeek runtime host differs from the frozen protocol")


def _case_for_schedule_item(item: Mapping[str, Any], bundle: Any) -> Any:
    if item["dataset"] == "holdout":
        return bundle.case_for_ref(str(item["case_ref"]))
    from week9.experiments.cases import DEVELOPMENT_REGRESSION_CASES

    return DEVELOPMENT_REGRESSION_CASES[str(item["case_ref"])]


def _prepare_private_root(path: Path) -> None:
    if path != DEFAULT_PRIVATE_RESULTS_ROOT.resolve():
        raise ValueError("private root differs from frozen location")
    path.mkdir(parents=True, exist_ok=True)
    item = path.lstat()
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if path.is_symlink() or bool(marker and getattr(item, "st_file_attributes", 0) & marker):
        raise ValueError("private root must not be a reparse point")


def _write_public_progress(
    public_batch: Path,
    schedule: tuple[dict[str, Any], ...],
    records: list[dict[str, Any]],
    protocol_sha256: str,
) -> None:
    progress = {
        "schema_version": "v8-blinded-formal-progress-2",
        "protocol_sha256": protocol_sha256,
        "planned_run_count": len(schedule),
        "record_count": len(records),
        "records": records,
        "privacy_guard": {
            "private_content_in_progress": False,
            "case_ids_in_progress": False,
            "requirements_in_progress": False,
            "raw_artifacts_in_progress": False,
        },
    }
    path = public_batch / "blinded-formal-progress.json"
    _write_json(path, progress)
    path.with_suffix(".sha256").write_text(file_sha256(path) + "\n", encoding="ascii")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-formal", action="store_true")
    parser.add_argument("--confirmation-id", default="")
    parser.add_argument("--authorization")
    parser.add_argument("--protocol", default=str(DEFAULT_FINAL_PROTOCOL))
    parser.add_argument("--binding", default=str(DEFAULT_BINDING))
    parser.add_argument("--out-root", default=str(PUBLIC_RESULTS_ROOT))
    args = parser.parse_args(argv)
    if not args.execute_formal:
        print(
            "BLOCKED: blinded formal comparison requires a new one-batch authorization.",
            file=sys.stderr,
        )
        return 2
    if not args.confirmation_id or not args.authorization:
        print("BLOCKED: confirmation and authorization receipt are required", file=sys.stderr)
        return 2
    try:
        report, output = run_blinded_formal_comparison(
            confirmation_id=args.confirmation_id,
            authorization_path=Path(args.authorization),
            protocol_path=Path(args.protocol),
            binding_path=Path(args.binding),
            public_output_root=Path(args.out_root),
        )
    except Exception:
        print("BLOCKED: blinded formal execution gate failed", file=sys.stderr)
        return 2
    print(f"formal={report['verdict']} output={output}")
    if report["verdict"] == PASS_VERDICT:
        return 0
    if report["verdict"] in {FAIL_VERDICT, INCOMPLETE_VERDICT}:
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "build_worker_command",
    "controlled_worker_environment",
    "main",
    "run_blinded_formal_comparison",
]
