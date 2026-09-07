"""Run the preregistered contemporaneous V4/V6 development pilot.

The pilot is the only allowed source for the still-null release thresholds.
It never accesses holdout cases and it refuses to start unless the frozen
protocol, explicit confirmation record, process cleanup certification, and
generator credential presence all agree.  Importing this module is offline.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
from typing import Any, Iterable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from week9.experiments.cases import DEVELOPMENT_REGRESSION_CASES, CaseDefinition  # noqa: E402
from week9.experiments.freeze_protocol import (  # noqa: E402
    DEFAULT_PROTOCOL,
    PROTOCOL_DIGEST,
    validate_protocol,
)
from week9.experiments.version_adapters import get_adapter  # noqa: E402
from week9.src.schemas import CheckStatus, FailureKind, RunResult  # noqa: E402


RESULTS_ROOT = PROJECT_ROOT / "week9" / "results"
PILOT_VERSIONS = ("v4", "v6")
SIMPLE_CASE_IDS = ("rc_lowpass",)
_IMPLEMENTATION_FILES = (
    "experiments/run_contemporaneous_pilot.py",
    "experiments/version_adapters.py",
    "experiments/cross_version_validation.py",
    "experiments/external_evaluator.py",
    "experiments/subprocess_control.py",
)


def build_schedule(
    repetitions: int,
    *,
    case_ids: Iterable[str] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Return a deterministic, order-balanced paired schedule."""

    if type(repetitions) is not int or repetitions < 1:
        raise ValueError("repetitions must be a positive integer")
    selected = tuple(case_ids or sorted(DEVELOPMENT_REGRESSION_CASES))
    if not selected or len(selected) != len(set(selected)):
        raise ValueError("pilot case ids must be non-empty and unique")
    if any(case_id not in DEVELOPMENT_REGRESSION_CASES for case_id in selected):
        raise ValueError("pilot may use only frozen development cases")
    schedule: list[dict[str, Any]] = []
    sequence = 0
    for repetition in range(1, repetitions + 1):
        rotated_cases = selected[(repetition - 1) % len(selected) :] + selected[
            : (repetition - 1) % len(selected)
        ]
        for case_position, case_id in enumerate(rotated_cases):
            pair_id = f"r{repetition:02d}-{case_id}"
            version_order = (
                PILOT_VERSIONS
                if (repetition + case_position) % 2 == 1
                else tuple(reversed(PILOT_VERSIONS))
            )
            for order_in_pair, version in enumerate(version_order, start=1):
                sequence += 1
                schedule.append(
                    {
                        "sequence": sequence,
                        "pair_id": pair_id,
                        "repetition": repetition,
                        "case_id": case_id,
                        "case_sha256": DEVELOPMENT_REGRESSION_CASES[case_id].sha256,
                        "version": version,
                        "order_in_pair": order_in_pair,
                    }
                )
    return tuple(schedule)


def result_record(
    schedule_item: Mapping[str, Any],
    result: RunResult,
    *,
    result_file: str,
    result_sha256: str,
) -> dict[str, Any]:
    external_marker = result.raw_metrics.get("external_evaluator")
    infrastructure_failure = bool(
        result.infrastructure_errors
        or any(
            check.status is CheckStatus.ERROR
            or check.failure_kind is FailureKind.INFRASTRUCTURE
            for check in result.checks
        )
    )
    return {
        **dict(schedule_item),
        "run_id": result.run_id,
        "result_file": result_file,
        "result_sha256": result_sha256,
        "contract_valid": True,
        "external_evaluator": external_marker,
        "overall_success": result.overall_success,
        "infrastructure_failure": infrastructure_failure,
        "infrastructure_error_count": len(result.infrastructure_errors),
        "prompt_tokens": result.token_usage.prompt_tokens,
        "completion_tokens": result.token_usage.completion_tokens,
        "total_tokens": result.token_usage.total_tokens,
        "duration_s": result.duration_s,
        "content_repair_count": result.counters.content_repair_count,
        "model_configuration": {
            "provider": result.model_configuration.provider,
            "model": result.model_configuration.model,
            "parameters": dict(result.model_configuration.parameters),
        },
        "required_check_status": {
            check.check_type: check.status.value
            for check in result.checks
            if check.required
        },
    }


def build_pilot_report(
    *,
    schedule: Iterable[Mapping[str, Any]],
    records: Iterable[Mapping[str, Any]],
    repetitions: int,
    protocol_sha256: str,
    confirmation_id: str,
) -> dict[str, Any]:
    expected = [dict(item) for item in schedule]
    actual = [dict(item) for item in records]
    expected_keys = [
        (item["sequence"], item["pair_id"], item["version"]) for item in expected
    ]
    actual_keys = [
        (item.get("sequence"), item.get("pair_id"), item.get("version"))
        for item in actual
    ]
    schedule_complete = actual_keys == expected_keys
    unique_run_ids = len({item.get("run_id") for item in actual}) == len(actual)
    external_evaluator_exact = all(
        item.get("external_evaluator") == "week9.cross_version_validation.v1"
        for item in actual
    )
    no_infrastructure_failures = all(
        item.get("infrastructure_failure") is False for item in actual
    )

    pair_fairness: list[dict[str, Any]] = []
    for pair_id in sorted({item["pair_id"] for item in expected}):
        pair = [item for item in actual if item.get("pair_id") == pair_id]
        configs = [item.get("model_configuration") for item in pair]
        versions = {item.get("version") for item in pair}
        exact = (
            versions == set(PILOT_VERSIONS)
            and len(pair) == 2
            and all(isinstance(config, dict) for config in configs)
            and bool(configs[0].get("provider"))
            and bool(configs[0].get("model"))
            and configs[0] == configs[1]
        )
        pair_fairness.append(
            {
                "pair_id": pair_id,
                "same_generator_configuration": exact,
                "versions": sorted(version for version in versions if version),
                "configuration_sha256": (
                    _canonical_sha256(configs[0]) if exact else ""
                ),
            }
        )
    fairness_passed = bool(pair_fairness) and all(
        item["same_generator_configuration"] for item in pair_fairness
    )

    by_version: dict[str, dict[str, Any]] = {}
    for version in PILOT_VERSIONS:
        version_records = [item for item in actual if item.get("version") == version]
        tokens = [int(item.get("total_tokens", 0)) for item in version_records]
        durations = [float(item.get("duration_s", 0.0)) for item in version_records]
        simple = [item for item in version_records if item.get("case_id") in SIMPLE_CASE_IDS]
        simple_tokens = [int(item.get("total_tokens", 0)) for item in simple]
        simple_durations = [float(item.get("duration_s", 0.0)) for item in simple]
        repairs = [int(item.get("content_repair_count", 0)) for item in version_records]
        successes = sum(item.get("overall_success") is True for item in version_records)
        by_version[version] = {
            "run_count": len(version_records),
            "success_count": successes,
            "success_rate": successes / len(version_records) if version_records else None,
            "infrastructure_failure_count": sum(
                item.get("infrastructure_failure") is True for item in version_records
            ),
            "total_tokens": _distribution(tokens),
            "duration_s": _distribution(durations),
            "content_repair_count": _distribution(repairs),
            "simple_case_ids": list(SIMPLE_CASE_IDS),
            "simple_case_total_tokens": _distribution(simple_tokens),
            "simple_case_duration_s": _distribution(simple_durations),
        }

    pass_conditions = {
        "schedule_complete": schedule_complete,
        "run_ids_unique": unique_run_ids,
        "external_evaluator_exact": external_evaluator_exact,
        "no_infrastructure_failures": no_infrastructure_failures,
        "paired_generator_fairness": fairness_passed,
        "minimum_repetitions_met": repetitions >= 3,
    }
    passed = all(pass_conditions.values())
    return {
        "schema_version": "v8-contemporaneous-pilot-1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "verdict": "PILOT_PASS" if passed else "PILOT_FAIL",
        "experiment_scope": "contemporaneous_v4_v6_development_pilot",
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
            "versions": list(PILOT_VERSIONS),
            "development_cases_only": True,
            "holdout_accessed": False,
            "judge_called": False,
            "online_generator_called": True,
            "external_evaluator": "week9.cross_version_validation.v1",
        },
        "repetitions": repetitions,
        "planned_run_count": len(expected),
        "completed_run_count": len(actual),
        "pass_conditions": pass_conditions,
        "pair_fairness": pair_fairness,
        "summary_by_version": by_version,
        "records": actual,
        "claim_status": {
            "baseline_pilot": "RUN",
            "thresholds_frozen": "NOT_YET",
            "v8_formal_comparison": "NOT_RUN",
            "holdout_evaluation": "NOT_RUN",
            "judge_evaluation": "NOT_RUN",
        },
        "limitations": [
            "This pilot contains V4 and V6 development runs only; it is not a V8 result.",
            "Content failures are measured outcomes; infrastructure failures invalidate the pilot.",
            "The pilot may inform preregistered thresholds but cannot establish V8 superiority.",
        ],
    }


def run_pilot(
    *, repetitions: int, confirmation_id: str, output_root: Path | None = None
) -> tuple[dict[str, Any], Path]:
    protocol = _read_object(DEFAULT_PROTOCOL)
    errors = validate_protocol(protocol)
    if errors:
        raise PermissionError("frozen protocol is invalid: " + "; ".join(errors))
    minimum = protocol["repetitions"]["smoke_or_exploratory_min"]
    if repetitions < minimum:
        raise PermissionError(f"pilot requires at least {minimum} repetitions")
    policy = protocol["online_execution_policy"]
    if not policy.get("current_milestone_allows_online_calls"):
        raise PermissionError("the frozen protocol currently disables online calls")
    if not policy.get("process_tree_cleanup_ready"):
        raise PermissionError("process-tree cleanup is not certified")
    recorded_confirmation = str(policy.get("user_confirmation_id") or "")
    if not confirmation_id or confirmation_id != recorded_confirmation:
        raise PermissionError("confirmation id does not match the frozen protocol")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise PermissionError("DEEPSEEK_API_KEY is not present in the process environment")

    schedule = build_schedule(repetitions)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = (output_root or RESULTS_ROOT).resolve()
    result_dir = root / f"contemporaneous-pilot-{timestamp}"
    result_dir.mkdir(parents=True, exist_ok=False)
    runs_dir = result_dir / "runs"
    runs_dir.mkdir()
    adapter_root = result_dir / "adapter-work"
    environment = _controlled_generator_environment(os.environ)
    records: list[dict[str, Any]] = []
    for item in schedule:
        case: CaseDefinition = DEVELOPMENT_REGRESSION_CASES[item["case_id"]]
        adapter = get_adapter(item["version"])
        result = adapter.run(
            case,
            adapter_root,
            timeout_s=900.0,
            allow_online=True,
            run_kind="pilot",
            environment=environment,
        )
        relative = Path("runs") / (
            f"{item['sequence']:02d}-{item['pair_id']}-{item['version']}.run-result.json"
        )
        result_path = result_dir / relative
        _write_json(result_path, result.to_dict())
        records.append(
            result_record(
                item,
                result,
                result_file=relative.as_posix(),
                result_sha256=_raw_sha256(result_path),
            )
        )
        print(
            f"[{item['sequence']:02d}/{len(schedule)}] {item['pair_id']} "
            f"{item['version']} success={result.overall_success}",
            flush=True,
        )

    report = build_pilot_report(
        schedule=schedule,
        records=records,
        repetitions=repetitions,
        protocol_sha256=PROTOCOL_DIGEST.read_text(encoding="ascii").strip(),
        confirmation_id=confirmation_id,
    )
    report_path = result_dir / "contemporaneous-pilot-report.json"
    _write_json(report_path, report)
    (result_dir / "contemporaneous-pilot-report.sha256").write_text(
        f"{_raw_sha256(report_path)}  {report_path.name}\n", encoding="ascii"
    )
    (result_dir / "contemporaneous-pilot-summary.md").write_text(
        "# V8 同期 V4/V6 Pilot\n\n"
        f"- 结论：`{report['verdict']}`\n"
        f"- 完成：{report['completed_run_count']}/{report['planned_run_count']}\n"
        "- 范围：冻结开发集、V4/V6 同期成对运行、版本外统一复核。\n"
        "- 未运行：V8 正式比较、Judge、holdout。\n",
        encoding="utf-8",
    )
    return report, result_dir


def _controlled_generator_environment(source: Mapping[str, str]) -> dict[str, str]:
    environment = dict(source)
    environment["LLM_PROVIDER"] = "deepseek"
    for key in tuple(environment):
        if key.startswith("AGENT_") and key.endswith("_PROVIDER"):
            environment.pop(key, None)
        if key.startswith("KIMI_"):
            environment.pop(key, None)
    return environment


def _distribution(values: list[int] | list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "mean": None, "median": None, "minimum": None, "maximum": None}
    return {
        "count": len(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "minimum": min(values),
        "maximum": max(values),
    }


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-online", action="store_true")
    parser.add_argument("--confirmation-id", default="")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--out-root", default=None)
    args = parser.parse_args(argv)
    if not args.execute_online:
        print(
            "BLOCKED: Pilot is an online paid batch. Pass --execute-online only after "
            "the protocol records explicit user confirmation.",
            file=sys.stderr,
        )
        return 2
    try:
        report, result_dir = run_pilot(
            repetitions=args.repetitions,
            confirmation_id=args.confirmation_id,
            output_root=Path(args.out_root) if args.out_root else None,
        )
    except (PermissionError, ValueError, OSError) as exc:
        print(f"BLOCKED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(f"pilot={report['verdict']} -> {result_dir}")
    return 0 if report["verdict"] == "PILOT_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PILOT_VERSIONS",
    "SIMPLE_CASE_IDS",
    "build_pilot_report",
    "build_schedule",
    "result_record",
    "run_pilot",
]
