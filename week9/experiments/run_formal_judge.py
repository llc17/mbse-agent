"""Run auxiliary blinded Kimi scoring on a completed formal comparison.

The deterministic formal verdict is never changed by this report.  The
number of repeated scores follows the already measured Judge stability policy.
Import and the default CLI path are offline.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import statistics
import sys
import time
from typing import Any, Iterable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from week9.experiments.freeze_protocol import (  # noqa: E402
    DEFAULT_PROTOCOL,
    PROTOCOL_DIGEST,
    validate_protocol,
)
from week9.experiments.online_policy import assert_online_allowed  # noqa: E402
from week9.experiments.run_formal_comparison import FORMAL_ARMS  # noqa: E402
from week9.experiments.run_judge_stability import blind_payload, validate_score  # noqa: E402
from week9.experiments.subprocess_control import run_controlled  # noqa: E402
from week9.src.schemas import RunResult  # noqa: E402


RESULTS_ROOT = PROJECT_ROOT / "week9" / "results"
JUDGE_RUNNER = PROJECT_ROOT / "week8" / "src" / "judge.py"
FORMAL_ARM_IDS = tuple(item["arm_id"] for item in FORMAL_ARMS)
_BLIND_MARKERS = re.compile(r"(?i)(?<![A-Za-z0-9_])(?:v4|v6|v8|agent)(?![A-Za-z0-9_])")
_WINDOWS_PATH = re.compile(r"[A-Za-z]:\\[^\r\n\"']+")
_IMPLEMENTATION_FILES = (
    "experiments/run_formal_judge.py",
    "experiments/run_judge_stability.py",
    "experiments/subprocess_control.py",
)


def required_repeats(stability_policy: str, minimum_repeats: int) -> int:
    if stability_policy == "SINGLE_SCORE_AUXILIARY_ALLOWED":
        return 1
    if stability_policy == "REPEATED_MEDIAN_AUXILIARY_REQUIRED":
        return max(3, minimum_repeats)
    if stability_policy == "QUALITATIVE_AUXILIARY_ONLY":
        return 1
    raise ValueError("Judge stability policy is unavailable or unsupported")


def select_formal_records(formal_report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Select one preregistered repetition for every case and experiment arm."""

    records = formal_report.get("records")
    if not isinstance(records, list):
        raise ValueError("formal report records are missing")
    selected = [
        dict(item)
        for item in records
        if item.get("repetition") == 1 and item.get("arm_id") in FORMAL_ARM_IDS
    ]
    keys = [(item.get("dataset"), item.get("case_id"), item.get("arm_id")) for item in selected]
    if len(keys) != len(set(keys)) or not selected:
        raise ValueError("formal Judge selection is incomplete or duplicated")
    case_keys = {(dataset, case_id) for dataset, case_id, _ in keys}
    if any(
        {arm_id for dataset, case_id, arm_id in keys if (dataset, case_id) == key}
        != set(FORMAL_ARM_IDS)
        for key in case_keys
    ):
        raise ValueError("formal Judge selection lacks a version for a case")
    return sorted(
        selected,
        key=lambda item: (item["dataset"], item["case_id"], item["arm_id"]),
    )


def build_formal_judge_report(
    *,
    artifacts: Iterable[Mapping[str, Any]],
    trials: Iterable[Mapping[str, Any]],
    repeats_per_artifact: int,
    stability_policy: str,
    protocol_sha256: str,
    formal_report_sha256: str,
    stability_report_sha256: str,
    formal_hard_verdict: str,
    confirmation_id: str,
) -> dict[str, Any]:
    artifact_records = [dict(item) for item in artifacts]
    trial_records = [dict(item) for item in trials]
    artifact_ids = [item.get("artifact_id") for item in artifact_records]
    judge_input_ids = {
        str(item.get("judge_input_id") or item.get("artifact_id") or "")
        for item in artifact_records
    }
    judge_input_ids.discard("")
    trial_input_ids = [
        str(item.get("judge_input_id") or item.get("artifact_id") or "")
        for item in trial_records
    ]
    expected_repeats_set = set(range(1, repeats_per_artifact + 1))
    trial_schedule_complete = bool(judge_input_ids) and all(
        {
            int(trial["repeat"])
            for trial, trial_input_id in zip(trial_records, trial_input_ids)
            if trial_input_id == judge_input_id
            and type(trial.get("repeat")) is int
        }
        == expected_repeats_set
        and sum(trial_input_id == judge_input_id for trial_input_id in trial_input_ids)
        == repeats_per_artifact
        for judge_input_id in judge_input_ids
    )
    complete = (
        bool(artifact_records)
        and len(artifact_ids) == len(set(artifact_ids))
        and all(isinstance(value, str) and bool(value) for value in artifact_ids)
        and set(trial_input_ids) == judge_input_ids
        and trial_schedule_complete
    )
    valid_complete = complete and all(item.get("valid") is True for item in trial_records)
    blinded = bool(artifact_records) and all(
        item.get("blind_marker_count") == 0
        and isinstance(item.get("blind_input_sha256"), str)
        and len(item["blind_input_sha256"]) == 64
        and isinstance(item.get("blind_content_sha256"), str)
        and len(item["blind_content_sha256"]) == 64
        for item in artifact_records
    )
    content_to_input: dict[str, set[str]] = {}
    input_to_content: dict[str, set[str]] = {}
    input_to_file_hash: dict[str, set[tuple[str, str]]] = {}
    for artifact in artifact_records:
        judge_input_id = str(
            artifact.get("judge_input_id") or artifact.get("artifact_id") or ""
        )
        content_sha256 = str(artifact.get("blind_content_sha256") or "")
        content_to_input.setdefault(content_sha256, set()).add(judge_input_id)
        input_to_content.setdefault(judge_input_id, set()).add(content_sha256)
        input_to_file_hash.setdefault(judge_input_id, set()).add(
            (
                str(artifact.get("blind_input_file") or ""),
                str(artifact.get("blind_input_sha256") or ""),
            )
        )
    deduplication_integrity = blinded and all(
        len(values) == 1 for values in content_to_input.values()
    ) and all(
        len(input_to_content[judge_input_id]) == 1
        and len(input_to_file_hash[judge_input_id]) == 1
        for judge_input_id in judge_input_ids
    )
    configurations = {
        (str(item.get("provider")), str(item.get("model")))
        for item in trial_records
        if item.get("valid") is True
    }
    single_configuration = len(configurations) == 1 and all(
        provider == "kimi" and bool(model) for provider, model in configurations
    )
    expected_repeats = required_repeats(stability_policy, 3)
    policy_followed = repeats_per_artifact == expected_repeats
    numeric_allowed = stability_policy != "QUALITATIVE_AUXILIARY_ONLY"

    summary: dict[str, dict[str, Any]] = {}
    for dataset in ("development", "holdout"):
        summary[dataset] = {}
        for arm_id in FORMAL_ARM_IDS:
            selected_artifacts = [
                item
                for item in artifact_records
                if item.get("dataset") == dataset and item.get("arm_id") == arm_id
            ]
            values_by_artifact = [
                [
                    int(trial["total"])
                    for trial in trial_records
                    if (trial.get("judge_input_id") or trial.get("artifact_id"))
                    == (artifact.get("judge_input_id") or artifact["artifact_id"])
                    and trial.get("valid") is True
                ]
                for artifact in selected_artifacts
            ]
            aggregate = [
                (
                    float(statistics.median(values))
                    if repeats_per_artifact > 1
                    else float(values[0])
                )
                for values in values_by_artifact
                if values
            ]
            summary[dataset][arm_id] = {
                "artifact_count": len(selected_artifacts),
                "unique_judge_input_count": len(
                    {
                        item.get("judge_input_id") or item["artifact_id"]
                        for item in selected_artifacts
                    }
                ),
                "valid_artifact_score_count": len(aggregate),
                "aggregation": (
                    "per_artifact_repeated_median"
                    if repeats_per_artifact > 1
                    else "per_artifact_single_score"
                ),
                "mean": statistics.fmean(aggregate) if aggregate else None,
                "median": statistics.median(aggregate) if aggregate else None,
                "std": statistics.stdev(aggregate) if len(aggregate) >= 2 else None,
                "numeric_comparison_allowed": numeric_allowed,
            }

    pass_conditions = {
        "artifact_and_trial_schedule_complete": complete,
        "all_judge_responses_valid": valid_complete,
        "blind_input_integrity": blinded,
        "content_hash_deduplication_integrity": deduplication_integrity,
        "single_judge_configuration": single_configuration,
        "stability_policy_followed": policy_followed,
    }
    passed = all(pass_conditions.values())
    return {
        "schema_version": "v8-formal-judge-1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "verdict": "FORMAL_JUDGE_COMPLETE" if passed else "FORMAL_JUDGE_INVALID",
        "experiment_scope": "auxiliary_blinded_formal_artifact_scoring",
        "formal_hard_verdict_unchanged": formal_hard_verdict,
        "protocol_sha256": protocol_sha256,
        "formal_report_sha256": formal_report_sha256,
        "stability_report_sha256": stability_report_sha256,
        "implementation_sha256": {
            name: _raw_sha256(PROJECT_ROOT / "week9" / name)
            for name in _IMPLEMENTATION_FILES
        },
        "authorization": {
            "confirmation_recorded": bool(confirmation_id),
            "confirmation_id_sha256": (
                hashlib.sha256(confirmation_id.encode("utf-8")).hexdigest()
                if confirmation_id
                else ""
            ),
            "credential_values_recorded": False,
        },
        "guard": {
            "generator_called": False,
            "judge_provider": "Kimi",
            "source_arms": list(FORMAL_ARM_IDS),
            "holdout_source_is_completed_formal_report": True,
            "blind_payload_excludes_version_and_paths": True,
            "temperature_parameter_sent": False,
            "hard_verdict_override_allowed": False,
        },
        "stability_policy": stability_policy,
        "numeric_comparison_allowed": numeric_allowed,
        "repeats_per_artifact": repeats_per_artifact,
        "artifact_count": len(artifact_records),
        "unique_judge_input_count": len(judge_input_ids),
        "deduplicated_artifact_count": len(artifact_records) - len(judge_input_ids),
        "planned_trial_count": len(judge_input_ids) * repeats_per_artifact,
        "completed_trial_count": len(trial_records),
        "valid_trial_count": sum(item.get("valid") is True for item in trial_records),
        "pass_conditions": pass_conditions,
        "judge_configurations": [
            {"provider": provider, "model": model}
            for provider, model in sorted(configurations)
        ],
        "summary_by_dataset_and_arm": summary,
        "artifacts": artifact_records,
        "trials": trial_records,
        "limitations": [
            "This report is auxiliary and cannot change the deterministic formal verdict.",
            "Only repetition 1 from each case/arm is scored to avoid outcome-based selection.",
            "Canonical-equivalent blinded substantive content is scored once and mapped to every originating arm.",
            "When stability is qualitative-only, numeric version comparisons are disabled.",
        ],
    }


def run_formal_judge(
    *,
    formal_report_path: Path,
    stability_report_path: Path,
    confirmation_id: str,
    output_root: Path | None = None,
) -> tuple[dict[str, Any], Path]:
    protocol = _read_object(DEFAULT_PROTOCOL)
    errors = validate_protocol(protocol)
    if errors:
        raise PermissionError("frozen protocol is invalid: " + "; ".join(errors))
    assert_online_allowed(allow_online=True, run_kind="formal")
    recorded = str(protocol["online_execution_policy"].get("user_confirmation_id") or "")
    if not confirmation_id or confirmation_id != recorded:
        raise PermissionError("confirmation id does not match the frozen protocol")
    if not os.environ.get("KIMI_API_KEY"):
        raise PermissionError("KIMI_API_KEY is not present in the process environment")
    formal = _verified_formal_report(formal_report_path.resolve())
    stability = _verified_report(
        stability_report_path.resolve(),
        schema_version="v8-judge-stability-1",
        verdict="JUDGE_STABILITY_PASS",
    )
    stability_policy = str(stability.get("formal_evidence_policy") or "")
    repeats = required_repeats(
        stability_policy,
        int(protocol["judge"]["repeats_per_artifact_min"]),
    )
    selected = select_formal_records(formal)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = (output_root or RESULTS_ROOT).resolve()
    result_dir = root / f"formal-judge-{timestamp}"
    result_dir.mkdir(parents=True, exist_ok=False)
    inputs_dir = result_dir / "blind-inputs"
    outputs_dir = result_dir / "judge-results"
    inputs_dir.mkdir()
    outputs_dir.mkdir()
    artifacts: list[dict[str, Any]] = []
    input_paths: dict[str, Path] = {}
    content_to_input_id: dict[str, str] = {}
    input_metadata: dict[str, tuple[Path, str, int]] = {}
    formal_root = formal_report_path.resolve().parent
    for index, record in enumerate(selected, start=1):
        source_path = (formal_root / str(record["result_file"])).resolve()
        if not source_path.is_relative_to(formal_root):
            raise ValueError("formal result path escapes its report directory")
        if _raw_sha256(source_path) != record.get("result_sha256"):
            raise ValueError("formal run result hash mismatch")
        result = RunResult.from_dict(_read_object(source_path))
        artifact_id = f"artifact-{index:03d}"
        payload = blind_payload(result, artifact_id=artifact_id)
        content_sha256 = _blind_content_sha256(payload)
        judge_input_id = content_to_input_id.get(content_sha256)
        if judge_input_id is None:
            judge_input_id = artifact_id
            content_to_input_id[content_sha256] = judge_input_id
            input_path = inputs_dir / f"{judge_input_id}.json"
            _write_json(input_path, payload)
            serialized = input_path.read_text(encoding="utf-8")
            input_metadata[judge_input_id] = (
                input_path,
                _raw_sha256(input_path),
                len(_BLIND_MARKERS.findall(serialized))
                + len(_WINDOWS_PATH.findall(serialized)),
            )
        input_path, input_sha256, marker_count = input_metadata[judge_input_id]
        artifacts.append(
            {
                "artifact_id": artifact_id,
                "judge_input_id": judge_input_id,
                "dataset": record["dataset"],
                "case_id": record["case_id"],
                "version": record["version"],
                "arm_id": record["arm_id"],
                "source_run_id": record["run_id"],
                "source_result_sha256": record["result_sha256"],
                "blind_content_sha256": content_sha256,
                "blind_input_file": input_path.relative_to(result_dir).as_posix(),
                "blind_input_sha256": input_sha256,
                "blind_marker_count": marker_count,
            }
        )
        input_paths[judge_input_id] = input_path

    environment = dict(os.environ)
    for key in tuple(environment):
        if key.startswith("DEEPSEEK_") or key.startswith("ZHIPU_"):
            environment.pop(key, None)
    trials: list[dict[str, Any]] = []
    sequence = 0
    judge_input_ids = tuple(sorted(input_paths))
    for repeat in range(1, repeats + 1):
        for judge_input_id in judge_input_ids:
            sequence += 1
            output_path = (
                outputs_dir / f"{sequence:03d}-{judge_input_id}-r{repeat:02d}.json"
            )
            if sequence > 1:
                time.sleep(22.0)
            process = run_controlled(
                (
                    sys.executable,
                    str(JUDGE_RUNNER),
                    "--result",
                    str(input_paths[judge_input_id]),
                    "--out",
                    str(output_path),
                ),
                cwd=PROJECT_ROOT / "week8",
                timeout_s=300.0,
                env=environment,
            )
            parsed: dict[str, Any] = {}
            parse_error = ""
            try:
                parsed = _read_object(output_path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                parse_error = f"{type(exc).__name__}: {exc}"
            valid, normalized, validation_error = validate_score(parsed)
            error = parse_error or validation_error
            if process.timed_out:
                valid = False
                error = "judge timeout; " + process.cleanup_detail
            elif process.returncode != 0:
                valid = False
                error = error or f"judge process exited {process.returncode}"
            trials.append(
                {
                    "sequence": sequence,
                    "artifact_id": judge_input_id,
                    "judge_input_id": judge_input_id,
                    "repeat": repeat,
                    "valid": valid,
                    **normalized,
                    "error": error,
                    "result_file": (
                        output_path.relative_to(result_dir).as_posix()
                        if output_path.is_file()
                        else ""
                    ),
                    "result_sha256": _raw_sha256(output_path) if output_path.is_file() else "",
                    "process_tree_cleanup_succeeded": process.cleanup_succeeded,
                }
            )
            print(
                f"[{sequence:03d}/{len(judge_input_ids) * repeats}] "
                f"{judge_input_id} repeat={repeat} valid={valid}",
                flush=True,
            )

    report = build_formal_judge_report(
        artifacts=artifacts,
        trials=trials,
        repeats_per_artifact=repeats,
        stability_policy=stability_policy,
        protocol_sha256=PROTOCOL_DIGEST.read_text(encoding="ascii").strip(),
        formal_report_sha256=_raw_sha256(formal_report_path),
        stability_report_sha256=_raw_sha256(stability_report_path),
        formal_hard_verdict=str(formal["verdict"]),
        confirmation_id=confirmation_id,
    )
    report_path = result_dir / "formal-judge-report.json"
    _write_json(report_path, report)
    report_path.with_suffix(".sha256").write_text(
        f"{_raw_sha256(report_path)}  {report_path.name}\n", encoding="ascii"
    )
    return report, result_dir


def _verified_formal_report(path: Path) -> dict[str, Any]:
    report = _verified_report(
        path,
        schema_version="v8-formal-comparison-1",
        verdict=None,
    )
    if report.get("verdict") not in {"FORMAL_PASS", "FORMAL_FAIL"}:
        raise ValueError("formal report verdict is invalid")
    conditions = report.get("pass_conditions", {})
    for name in (
        "schedule_complete",
        "run_ids_unique",
        "external_evaluator_exact",
        "no_infrastructure_failures",
        "paired_generator_fairness",
        "v8_default_local_is_offline",
        "minimum_repetitions_met",
    ):
        if conditions.get(name) is not True:
            raise ValueError(f"formal report infrastructure/fairness condition failed: {name}")
    return report


def _verified_report(
    path: Path, *, schema_version: str, verdict: str | None
) -> dict[str, Any]:
    report = _read_object(path)
    try:
        expected = path.with_suffix(".sha256").read_text(encoding="ascii").strip().split()[0]
    except (OSError, IndexError) as exc:
        raise ValueError(f"report sidecar unavailable: {path}") from exc
    if expected != _raw_sha256(path):
        raise ValueError(f"report SHA-256 mismatch: {path}")
    if report.get("schema_version") != schema_version:
        raise ValueError(f"report schema mismatch: {path}")
    if verdict is not None and report.get("verdict") != verdict:
        raise ValueError(f"report verdict does not pass: {path}")
    return report


def _raw_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _blind_content_sha256(payload: Mapping[str, Any]) -> str:
    substantive = dict(payload)
    substantive.pop("case_id", None)
    return hashlib.sha256(
        json.dumps(
            substantive,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-online", action="store_true")
    parser.add_argument("--confirmation-id", default="")
    parser.add_argument("--formal-report")
    parser.add_argument("--stability-report")
    parser.add_argument("--out-root", default=None)
    args = parser.parse_args(argv)
    if not args.execute_online:
        print("BLOCKED: formal Judge is an online paid auxiliary batch.", file=sys.stderr)
        return 2
    if not args.formal_report or not args.stability_report:
        print("BLOCKED: formal and Judge stability report paths are required", file=sys.stderr)
        return 2
    try:
        report, result_dir = run_formal_judge(
            formal_report_path=Path(args.formal_report),
            stability_report_path=Path(args.stability_report),
            confirmation_id=args.confirmation_id,
            output_root=Path(args.out_root) if args.out_root else None,
        )
    except (PermissionError, ValueError, OSError) as exc:
        print(f"BLOCKED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(f"formal_judge={report['verdict']} -> {result_dir}")
    return 0 if report["verdict"] == "FORMAL_JUDGE_COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "build_formal_judge_report",
    "main",
    "required_repeats",
    "run_formal_judge",
    "select_formal_records",
]
