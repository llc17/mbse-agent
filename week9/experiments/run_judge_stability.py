"""Measure Kimi Judge repeatability on fixed, blinded pilot artifacts.

No generator is called by this script.  It selects fixed development artifacts
from a verified contemporaneous pilot, removes version/path/agent markers from
the Judge payload, and evaluates each artifact at least three times.  Online
execution remains blocked until the frozen protocol records explicit approval.
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
from week9.experiments.subprocess_control import run_controlled  # noqa: E402
from week9.src.schemas import RunResult  # noqa: E402


RESULTS_ROOT = PROJECT_ROOT / "week9" / "results"
JUDGE_RUNNER = PROJECT_ROOT / "week8" / "src" / "judge.py"
_BLIND_MARKERS = re.compile(r"(?i)(?<![A-Za-z0-9_])(?:v4|v6|v8|agent)(?![A-Za-z0-9_])")
_WINDOWS_PATH = re.compile(r"[A-Za-z]:\\[^\r\n\"']+")
_SCORE_KEYS = ("syntax", "consistency", "topology", "traceability")
_IMPLEMENTATION_FILES = (
    "experiments/run_judge_stability.py",
    "experiments/subprocess_control.py",
    "experiments/experiment_readiness.py",
)


def blind_payload(result: RunResult, *, artifact_id: str) -> dict[str, Any]:
    """Build the exact anonymous object sent to the frozen Judge runner."""

    if not re.fullmatch(r"artifact-[0-9]{3}", artifact_id):
        raise ValueError("artifact_id must be an anonymous sequential id")
    sysml = _scrub(_artifact_content(result, "sysml"))
    modelica = _scrub(_artifact_content(result, "modelica"))
    if not sysml.strip() or not modelica.strip():
        raise ValueError("Judge representative artifact requires SysML and Modelica")
    template_id = str(result.raw_metrics.get("template_id") or "")
    return {
        "case_id": artifact_id,
        "component_type": template_id,
        "parameters": {
            item.canonical_name: {"value": item.value, "unit": item.unit}
            for item in result.parameters
        },
        "sysml_code": sysml,
        "modelica_code": modelica,
        "summary_text": _scrub(
            "Requirement: " + result.requirement_text + "\n"
            "External hard-check statuses: "
            + json.dumps(
                {
                    item.check_type: item.status.value
                    for item in result.checks
                    if item.required
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        ),
    }


def validate_score(payload: Any) -> tuple[bool, dict[str, Any], str]:
    if not isinstance(payload, dict):
        return False, {}, "score root is not an object"
    scores = payload.get("scores")
    if not isinstance(scores, dict) or set(scores) != set(_SCORE_KEYS):
        return False, {}, "score dimensions are incomplete or unexpected"
    normalized: dict[str, int] = {}
    for key in _SCORE_KEYS:
        value = scores.get(key)
        if type(value) is not int or not 0 <= value <= 25:
            return False, {}, f"invalid {key} score"
        normalized[key] = value
    total = payload.get("total")
    if type(total) is not int or total != sum(normalized.values()):
        return False, {}, "total does not equal the four score dimensions"
    provider = str(payload.get("provider") or "")
    model = str(payload.get("model") or "")
    if provider.lower() != "kimi" or not model:
        return False, {}, "Judge provider/model provenance is missing"
    return True, {
        "scores": normalized,
        "total": total,
        "comment": str(payload.get("comment") or ""),
        "provider": provider.lower(),
        "model": model,
    }, ""


def build_judge_stability_report(
    *,
    artifacts: Iterable[Mapping[str, Any]],
    trials: Iterable[Mapping[str, Any]],
    repeats_per_artifact: int,
    protocol_sha256: str,
    confirmation_id: str,
) -> dict[str, Any]:
    artifact_records = [dict(item) for item in artifacts]
    trial_records = [dict(item) for item in trials]
    artifact_ids = [item.get("artifact_id") for item in artifact_records]
    unique_artifacts = len(artifact_ids) == len(set(artifact_ids))
    statistics_by_artifact: list[dict[str, Any]] = []
    model_configurations: set[tuple[str, str]] = set()
    for artifact in artifact_records:
        artifact_id = artifact["artifact_id"]
        selected = [item for item in trial_records if item.get("artifact_id") == artifact_id]
        valid = [item for item in selected if item.get("valid") is True]
        totals = [int(item["total"]) for item in valid]
        for item in valid:
            model_configurations.add((str(item.get("provider")), str(item.get("model"))))
        statistics_by_artifact.append(
            {
                "artifact_id": artifact_id,
                "planned_trials": repeats_per_artifact,
                "completed_trials": len(selected),
                "valid_trials": len(valid),
                "valid_response_rate": len(valid) / len(selected) if selected else 0.0,
                "total_mean": statistics.fmean(totals) if totals else None,
                "total_median": statistics.median(totals) if totals else None,
                "total_std": statistics.stdev(totals) if len(totals) >= 2 else None,
                "totals": totals,
            }
        )

    minimums_met = bool(artifact_records) and len(artifact_records) >= 3 and all(
        item["completed_trials"] == repeats_per_artifact
        and item["valid_trials"] >= 3
        for item in statistics_by_artifact
    )
    model_consistent = len(model_configurations) == 1 and all(
        provider == "kimi" and bool(model)
        for provider, model in model_configurations
    )
    blind_integrity = unique_artifacts and all(
        item.get("blind_marker_count") == 0
        and isinstance(item.get("blind_input_sha256"), str)
        and len(item["blind_input_sha256"]) == 64
        for item in artifact_records
    )
    std_values = [
        float(item["total_std"])
        for item in statistics_by_artifact
        if item["total_std"] is not None
    ]
    max_std = max(std_values) if std_values else None
    if max_std is None:
        evidence_policy = "UNAVAILABLE"
    elif max_std < 2.0:
        evidence_policy = "SINGLE_SCORE_AUXILIARY_ALLOWED"
    elif max_std < 5.0:
        evidence_policy = "REPEATED_MEDIAN_AUXILIARY_REQUIRED"
    else:
        evidence_policy = "QUALITATIVE_AUXILIARY_ONLY"
    pass_conditions = {
        "representative_artifact_minimum_met": len(artifact_records) >= 3,
        "repetition_minimum_met": repeats_per_artifact >= 3,
        "all_trials_complete_with_three_valid": minimums_met,
        "blind_input_integrity": blind_integrity,
        "single_judge_configuration": model_consistent,
    }
    passed = all(pass_conditions.values())
    return {
        "schema_version": "v8-judge-stability-1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "verdict": "JUDGE_STABILITY_PASS" if passed else "JUDGE_STABILITY_FAIL",
        "experiment_scope": "fixed_development_artifact_judge_resampling",
        "protocol_sha256": protocol_sha256,
        "implementation_sha256": {
            name: _raw_sha256(PROJECT_ROOT / "week9" / name)
            for name in _IMPLEMENTATION_FILES
        },
        "authorization": {
            "confirmation_recorded": bool(confirmation_id),
            "confirmation_id_sha256": hashlib.sha256(
                confirmation_id.encode("utf-8")
            ).hexdigest()
            if confirmation_id
            else "",
            "credential_values_recorded": False,
        },
        "guard": {
            "generator_called": False,
            "judge_provider": "Kimi",
            "holdout_accessed": False,
            "blind_payload_excludes_version_and_paths": True,
            "temperature_parameter_sent": False,
            "max_tokens_min": 16000,
        },
        "artifact_count": len(artifact_records),
        "repeats_per_artifact": repeats_per_artifact,
        "planned_trial_count": len(artifact_records) * repeats_per_artifact,
        "completed_trial_count": len(trial_records),
        "valid_trial_count": sum(item.get("valid") is True for item in trial_records),
        "pass_conditions": pass_conditions,
        "judge_configurations": [
            {"provider": provider, "model": model}
            for provider, model in sorted(model_configurations)
        ],
        "max_artifact_total_std": max_std,
        "formal_evidence_policy": evidence_policy,
        "artifacts": artifact_records,
        "statistics_by_artifact": statistics_by_artifact,
        "trials": trial_records,
        "limitations": [
            "Judge is auxiliary evidence and never overrides deterministic hard checks.",
            "This measures repeatability on fixed development artifacts, not generalization.",
            "A high standard deviation downgrades Judge use instead of invalidating hard evidence.",
        ],
    }


def select_pilot_artifacts(
    pilot_report_path: Path, *, minimum: int
) -> list[dict[str, Any]]:
    report = _verified_report(pilot_report_path, expected_verdict="PILOT_PASS")
    if report.get("guard", {}).get("holdout_accessed") is not False:
        raise ValueError("pilot report does not prove holdout isolation")
    result_dir = pilot_report_path.parent
    selected: list[dict[str, Any]] = []
    seen_cases: set[str] = set()
    for record in report.get("records", []):
        case_id = record.get("case_id")
        if case_id in seen_cases:
            continue
        result_path = (result_dir / str(record.get("result_file"))).resolve()
        if not result_path.is_relative_to(result_dir.resolve()):
            raise ValueError("pilot run result path escapes its result directory")
        if _raw_sha256(result_path) != record.get("result_sha256"):
            raise ValueError("pilot run result hash mismatch")
        result = RunResult.from_dict(_read_object(result_path))
        if not _artifact_content(result, "sysml") or not _artifact_content(result, "modelica"):
            continue
        selected.append(
            {
                "source_case_id": case_id,
                "source_version": record.get("version"),
                "source_run_id": record.get("run_id"),
                "source_result_sha256": record.get("result_sha256"),
                "result": result,
            }
        )
        seen_cases.add(str(case_id))
        if len(selected) >= minimum:
            break
    if len(selected) < minimum:
        raise ValueError(f"pilot contains fewer than {minimum} usable fixed artifacts")
    return selected


def run_judge_stability(
    *,
    pilot_report_path: Path,
    repeats_per_artifact: int,
    confirmation_id: str,
    output_root: Path | None = None,
) -> tuple[dict[str, Any], Path]:
    protocol = _read_object(DEFAULT_PROTOCOL)
    errors = validate_protocol(protocol)
    if errors:
        raise PermissionError("frozen protocol is invalid: " + "; ".join(errors))
    judge_policy = protocol["judge"]
    minimum_artifacts = int(judge_policy["representative_artifacts_min"])
    minimum_repeats = int(judge_policy["repeats_per_artifact_min"])
    if repeats_per_artifact < minimum_repeats:
        raise PermissionError(f"Judge stability requires at least {minimum_repeats} repeats")
    online = protocol["online_execution_policy"]
    if not online.get("current_milestone_allows_online_calls"):
        raise PermissionError("the frozen protocol currently disables online calls")
    if not online.get("process_tree_cleanup_ready"):
        raise PermissionError("process-tree cleanup is not certified")
    recorded_confirmation = str(online.get("user_confirmation_id") or "")
    if not confirmation_id or confirmation_id != recorded_confirmation:
        raise PermissionError("confirmation id does not match the frozen protocol")
    if not os.environ.get("KIMI_API_KEY"):
        raise PermissionError("KIMI_API_KEY is not present in the process environment")

    sources = select_pilot_artifacts(pilot_report_path.resolve(), minimum=minimum_artifacts)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = (output_root or RESULTS_ROOT).resolve()
    result_dir = root / f"judge-stability-{timestamp}"
    result_dir.mkdir(parents=True, exist_ok=False)
    inputs_dir = result_dir / "blind-inputs"
    outputs_dir = result_dir / "judge-results"
    inputs_dir.mkdir()
    outputs_dir.mkdir()
    artifacts: list[dict[str, Any]] = []
    input_paths: dict[str, Path] = {}
    for index, source in enumerate(sources, start=1):
        artifact_id = f"artifact-{index:03d}"
        payload = blind_payload(source["result"], artifact_id=artifact_id)
        input_path = inputs_dir / f"{artifact_id}.json"
        _write_json(input_path, payload)
        serialized = input_path.read_text(encoding="utf-8")
        artifacts.append(
            {
                "artifact_id": artifact_id,
                "source_case_id": source["source_case_id"],
                "source_version": source["source_version"],
                "source_run_id": source["source_run_id"],
                "source_result_sha256": source["source_result_sha256"],
                "blind_input_file": input_path.relative_to(result_dir).as_posix(),
                "blind_input_sha256": _raw_sha256(input_path),
                "blind_marker_count": len(_BLIND_MARKERS.findall(serialized))
                + len(_WINDOWS_PATH.findall(serialized)),
            }
        )
        input_paths[artifact_id] = input_path

    environment = dict(os.environ)
    for key in tuple(environment):
        if key.startswith("DEEPSEEK_") or key.startswith("ZHIPU_"):
            environment.pop(key, None)
    trials: list[dict[str, Any]] = []
    sequence = 0
    for repeat in range(1, repeats_per_artifact + 1):
        for artifact in artifacts:
            sequence += 1
            artifact_id = artifact["artifact_id"]
            output_path = outputs_dir / f"{sequence:02d}-{artifact_id}-r{repeat:02d}.json"
            if sequence > 1:
                time.sleep(22.0)
            process = run_controlled(
                (
                    sys.executable,
                    str(JUDGE_RUNNER),
                    "--result",
                    str(input_paths[artifact_id]),
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
                    "artifact_id": artifact_id,
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
                    "stdout_tail": process.stdout[-500:],
                    "stderr_tail": process.stderr[-500:],
                    "process_tree_cleanup_succeeded": process.cleanup_succeeded,
                }
            )
            print(
                f"[{sequence:02d}/{len(artifacts) * repeats_per_artifact}] "
                f"{artifact_id} repeat={repeat} valid={valid}",
                flush=True,
            )

    report = build_judge_stability_report(
        artifacts=artifacts,
        trials=trials,
        repeats_per_artifact=repeats_per_artifact,
        protocol_sha256=PROTOCOL_DIGEST.read_text(encoding="ascii").strip(),
        confirmation_id=confirmation_id,
    )
    report_path = result_dir / "judge-stability-report.json"
    _write_json(report_path, report)
    (result_dir / "judge-stability-report.sha256").write_text(
        f"{_raw_sha256(report_path)}  {report_path.name}\n", encoding="ascii"
    )
    (result_dir / "judge-stability-summary.md").write_text(
        "# V8 Judge 稳定性测量\n\n"
        f"- 结论：`{report['verdict']}`\n"
        f"- 有效评分：{report['valid_trial_count']}/{report['planned_trial_count']}\n"
        f"- 正式证据策略：`{report['formal_evidence_policy']}`\n"
        "- Judge 仅为辅助证据，不覆盖确定性硬检查。\n",
        encoding="utf-8",
    )
    return report, result_dir


def _verified_report(path: Path, *, expected_verdict: str) -> dict[str, Any]:
    report = _read_object(path)
    sidecar = path.with_suffix(".sha256")
    expected = sidecar.read_text(encoding="ascii").strip().split()[0]
    if expected != _raw_sha256(path):
        raise ValueError(f"report hash mismatch: {path}")
    if report.get("verdict") != expected_verdict:
        raise ValueError(f"report verdict is not {expected_verdict}")
    return report


def _artifact_content(result: RunResult, name: str) -> str:
    artifact = result.artifacts.get(name)
    if artifact is None:
        return ""
    if artifact.content:
        return artifact.content
    if artifact.path:
        try:
            return Path(artifact.path).read_text(encoding="utf-8")
        except OSError:
            return ""
    return ""


def _scrub(text: str) -> str:
    return _WINDOWS_PATH.sub("[path-redacted]", _BLIND_MARKERS.sub("[blind]", text))


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


def _raw_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot-report", required=True)
    parser.add_argument("--execute-online", action="store_true")
    parser.add_argument("--confirmation-id", default="")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--out-root", default=None)
    args = parser.parse_args(argv)
    if not args.execute_online:
        print(
            "BLOCKED: Judge resampling is an online paid batch. Pass --execute-online "
            "only after the protocol records explicit user confirmation.",
            file=sys.stderr,
        )
        return 2
    try:
        report, result_dir = run_judge_stability(
            pilot_report_path=Path(args.pilot_report),
            repeats_per_artifact=args.repeats,
            confirmation_id=args.confirmation_id,
            output_root=Path(args.out_root) if args.out_root else None,
        )
    except (PermissionError, ValueError, OSError) as exc:
        print(f"BLOCKED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(f"judge_stability={report['verdict']} -> {result_dir}")
    return 0 if report["verdict"] == "JUDGE_STABILITY_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "blind_payload",
    "build_judge_stability_report",
    "run_judge_stability",
    "select_pilot_artifacts",
    "validate_score",
]
