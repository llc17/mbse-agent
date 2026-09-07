"""Run the preregistered V4/V6/V8 formal comparison.

Importing this module and invoking the CLI without ``--execute-online`` are
offline.  Execution is permitted only after the contemporaneous baseline
pilot, Judge stability study, release thresholds, online authorization, and
holdout switch are all frozen and verifiable.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import sys
from typing import Any, Iterable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from week9.experiments.cases import (  # noqa: E402
    DEVELOPMENT_REGRESSION_CASES,
    HOLDOUT_CASES,
    CaseDefinition,
)
from week9.experiments.freeze_protocol import (  # noqa: E402
    DEFAULT_PROTOCOL,
    PROTOCOL_DIGEST,
    validate_protocol,
)
from week9.experiments.online_policy import assert_online_allowed  # noqa: E402
from week9.experiments.version_adapters import get_adapter  # noqa: E402
from week9.src.schemas import CheckStatus, FailureKind, RunResult  # noqa: E402


RESULTS_ROOT = PROJECT_ROOT / "week9" / "results"
FORMAL_VERSIONS = ("v4", "v6", "v8")
FORMAL_ARMS = (
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
)
CONTROLLED_ARM_IDS = (
    "v4_default_deepseek",
    "v6_default_deepseek",
    "v8_controlled_deepseek",
)
DEFAULT_LOCAL_ARM_ID = "v8_default_local"
SIMPLE_CASE_IDS = ("rc_lowpass",)
_IMPLEMENTATION_FILES = (
    "src/generation.py",
    "src/online_generation.py",
    "src/runner.py",
    "src/pipeline.py",
    "experiments/run_formal_comparison.py",
    "experiments/version_adapters.py",
    "experiments/cross_version_validation.py",
    "experiments/external_evaluator.py",
    "experiments/online_policy.py",
    "experiments/subprocess_control.py",
)


def build_schedule(repetitions: int) -> tuple[dict[str, Any], ...]:
    """Build a deterministic four-arm rotation for both comparison views."""

    if type(repetitions) is not int or repetitions < 1:
        raise ValueError("repetitions must be a positive integer")
    scoped_cases = (
        ("development", tuple(sorted(DEVELOPMENT_REGRESSION_CASES))),
        ("holdout", tuple(sorted(HOLDOUT_CASES))),
    )
    schedule: list[dict[str, Any]] = []
    sequence = 0
    case_position = 0
    for repetition in range(1, repetitions + 1):
        for dataset, case_ids in scoped_cases:
            rotated = case_ids[(repetition - 1) % len(case_ids) :] + case_ids[
                : (repetition - 1) % len(case_ids)
            ]
            source = (
                DEVELOPMENT_REGRESSION_CASES
                if dataset == "development"
                else HOLDOUT_CASES
            )
            for case_id in rotated:
                case = source[case_id]
                offset = (repetition - 1 + case_position) % len(FORMAL_ARMS)
                arm_order = FORMAL_ARMS[offset:] + FORMAL_ARMS[:offset]
                group_id = f"{dataset}-r{repetition:02d}-{case_id}"
                for order_in_group, arm in enumerate(arm_order, start=1):
                    sequence += 1
                    schedule.append(
                        {
                            "sequence": sequence,
                            "group_id": group_id,
                            "dataset": dataset,
                            "repetition": repetition,
                            "case_id": case_id,
                            "case_sha256": case.sha256,
                            **dict(arm),
                            "order_in_group": order_in_group,
                        }
                    )
                case_position += 1
    return tuple(schedule)


def generator_configuration(result: RunResult) -> dict[str, Any]:
    """Extract only the comparable generator controls, not pipeline metadata."""

    parameters = dict(result.model_configuration.parameters)
    if result.version.lower() == "v8":
        model = str(parameters.get("generator_model") or "")
    else:
        model = result.model_configuration.model
    temperature = parameters.get("temperature")
    max_tokens = parameters.get("max_tokens")
    return {
        "provider": result.model_configuration.provider.lower(),
        "model": str(model),
        "temperature": temperature,
        "max_tokens": max_tokens,
    }


def result_record(
    schedule_item: Mapping[str, Any],
    result: RunResult,
    *,
    result_file: str,
    result_sha256: str,
    thresholds: Mapping[str, Any],
) -> dict[str, Any]:
    infrastructure_failure = bool(
        result.infrastructure_errors
        or any(
            check.status is CheckStatus.ERROR
            or check.failure_kind is FailureKind.INFRASTRUCTURE
            for check in result.checks
        )
    )
    repair_limit = int(thresholds["max_content_repairs"])
    budget_rules = {
        "content_repairs_within_limit": (
            result.counters.content_repair_count <= repair_limit
        ),
    }
    if schedule_item["case_id"] in SIMPLE_CASE_IDS:
        budget_rules.update(
            {
                "simple_tokens_within_limit": (
                    result.token_usage.total_tokens
                    <= int(thresholds["simple_case_token_budget"])
                ),
                "simple_duration_within_limit": (
                    result.duration_s
                    <= float(thresholds["simple_case_duration_budget_s"])
                ),
            }
        )
    return {
        **dict(schedule_item),
        "run_id": result.run_id,
        "result_file": result_file,
        "result_sha256": result_sha256,
        "contract_valid": True,
        "external_evaluator": result.raw_metrics.get("external_evaluator"),
        "overall_success": result.overall_success,
        "infrastructure_failure": infrastructure_failure,
        "infrastructure_error_count": len(result.infrastructure_errors),
        "prompt_tokens": result.token_usage.prompt_tokens,
        "completion_tokens": result.token_usage.completion_tokens,
        "total_tokens": result.token_usage.total_tokens,
        "duration_s": result.duration_s,
        "content_repair_count": result.counters.content_repair_count,
        "generator_configuration": generator_configuration(result),
        "candidate_online_api_used": _candidate_online_api_used(result),
        "budget_rules": budget_rules,
        "equal_budget_success": result.overall_success and all(budget_rules.values()),
        "required_check_status": {
            check.check_type: check.status.value
            for check in result.checks
            if check.required
        },
    }


def _candidate_online_api_used(result: RunResult) -> bool | None:
    candidate = result.raw_metrics.get("candidate_raw_metrics")
    if isinstance(candidate, Mapping):
        value = candidate.get("online_api_used")
        if type(value) is bool:
            return value
    value = result.raw_metrics.get("online_api_used")
    return value if type(value) is bool else None


def build_formal_report(
    *,
    schedule: Iterable[Mapping[str, Any]],
    records: Iterable[Mapping[str, Any]],
    repetitions: int,
    protocol: Mapping[str, Any],
    protocol_sha256: str,
    pilot_sha256: str,
    judge_sha256: str,
    certification_sha256: str,
    confirmation_id: str,
) -> dict[str, Any]:
    expected = [dict(item) for item in schedule]
    actual = [dict(item) for item in records]
    schedule_fields = (
        "sequence",
        "group_id",
        "dataset",
        "repetition",
        "case_id",
        "case_sha256",
        "arm_id",
        "version",
        "configuration_view",
        "generation_mode",
        "online_generation",
        "order_in_group",
    )
    expected_keys = [tuple(item.get(name) for name in schedule_fields) for item in expected]
    actual_keys = [tuple(item.get(name) for name in schedule_fields) for item in actual]
    groups = sorted({item["group_id"] for item in expected})
    group_fairness: list[dict[str, Any]] = []
    for group_id in groups:
        group = [item for item in actual if item.get("group_id") == group_id]
        selected = [
            item for item in group if item.get("arm_id") in CONTROLLED_ARM_IDS
        ]
        versions = {item.get("version") for item in selected}
        configurations = [item.get("generator_configuration") for item in selected]
        exact = (
            len(selected) == len(CONTROLLED_ARM_IDS)
            and {item.get("arm_id") for item in selected} == set(CONTROLLED_ARM_IDS)
            and versions == set(FORMAL_VERSIONS)
            and all(_valid_generator_configuration(item) for item in configurations)
            and len({_canonical_sha256(item) for item in configurations}) == 1
            and len({item.get("case_sha256") for item in selected}) == 1
        )
        group_fairness.append(
            {
                "group_id": group_id,
                "controlled_arms": list(CONTROLLED_ARM_IDS),
                "versions": sorted(value for value in versions if value),
                "same_requirement_and_generator_configuration": exact,
                "generator_configuration_sha256": (
                    _canonical_sha256(configurations[0]) if exact else ""
                ),
            }
        )

    summaries: dict[str, dict[str, Any]] = {}
    for dataset in ("development", "holdout"):
        summaries[dataset] = {}
        for arm in FORMAL_ARMS:
            arm_id = arm["arm_id"]
            selected = [
                item
                for item in actual
                if item.get("dataset") == dataset and item.get("arm_id") == arm_id
            ]
            default_successes = sum(item.get("overall_success") is True for item in selected)
            budget_successes = sum(
                item.get("equal_budget_success") is True for item in selected
            )
            summaries[dataset][arm_id] = _version_summary(
                selected,
                default_successes=default_successes,
                equal_budget_successes=budget_successes,
            )

    thresholds = dict(protocol["release_thresholds"])
    dev_v8 = _rate(summaries["development"]["v8_controlled_deepseek"])
    dev_v6 = _rate(summaries["development"]["v6_default_deepseek"])
    holdout_v8 = _rate(summaries["holdout"]["v8_controlled_deepseek"])
    holdout_v6 = _rate(summaries["holdout"]["v6_default_deepseek"])
    dev_v8_default = _rate(summaries["development"][DEFAULT_LOCAL_ARM_ID])
    holdout_v8_default = _rate(summaries["holdout"][DEFAULT_LOCAL_ARM_ID])
    simple_v8 = [
        item
        for item in actual
        if item.get("dataset") == "development"
        and item.get("arm_id") == DEFAULT_LOCAL_ARM_ID
        and item.get("case_id") in SIMPLE_CASE_IDS
    ]
    simple_tokens = [int(item["total_tokens"]) for item in simple_v8]
    simple_durations = [float(item["duration_s"]) for item in simple_v8]
    v8_repairs = [
        int(item["content_repair_count"])
        for item in actual
        if item.get("arm_id") == DEFAULT_LOCAL_ARM_ID
    ]
    local_records = [
        item for item in actual if item.get("arm_id") == DEFAULT_LOCAL_ARM_ID
    ]
    local_default_offline = bool(local_records) and all(
        item.get("online_generation") is False
        and item.get("candidate_online_api_used") is False
        and item.get("generator_configuration", {}).get("provider")
        == "v8_deterministic_local_grammar"
        for item in local_records
    )
    release_conditions = {
        "controlled_development_improvement_met": (
            dev_v8 - dev_v6
            >= float(thresholds["development_success_improvement_min"])
        ),
        "controlled_holdout_noninferiority_met": (
            holdout_v8 + float(thresholds["holdout_noninferiority_margin"])
            >= holdout_v6
        ),
        "default_development_improvement_met": (
            dev_v8_default - dev_v6
            >= float(thresholds["development_success_improvement_min"])
        ),
        "default_holdout_noninferiority_met": (
            holdout_v8_default
            + float(thresholds["holdout_noninferiority_margin"])
            >= holdout_v6
        ),
        "simple_token_budget_met": bool(simple_tokens)
        and statistics.median(simple_tokens)
        <= float(thresholds["simple_case_token_budget"]),
        "simple_duration_budget_met": bool(simple_durations)
        and statistics.median(simple_durations)
        <= float(thresholds["simple_case_duration_budget_s"]),
        "content_repair_budget_met": bool(v8_repairs)
        and max(v8_repairs) <= int(thresholds["max_content_repairs"]),
    }
    pass_conditions = {
        "schedule_complete": actual_keys == expected_keys,
        "run_ids_unique": len({item.get("run_id") for item in actual}) == len(actual),
        "external_evaluator_exact": bool(actual)
        and all(
            item.get("external_evaluator") == "week9.cross_version_validation.v1"
            for item in actual
        ),
        "no_infrastructure_failures": bool(actual)
        and all(item.get("infrastructure_failure") is False for item in actual),
        "paired_generator_fairness": bool(group_fairness)
        and all(
            item["same_requirement_and_generator_configuration"]
            for item in group_fairness
        ),
        "v8_default_local_is_offline": local_default_offline,
        "minimum_repetitions_met": repetitions
        >= int(protocol["repetitions"]["smoke_or_exploratory_min"]),
        "release_conditions_met": all(release_conditions.values()),
    }
    passed = all(pass_conditions.values())
    recommended = int(protocol["repetitions"]["formal_recommended"])
    conclusion_strength = "FORMAL" if repetitions >= recommended else "PRELIMINARY"
    return {
        "schema_version": "v8-formal-comparison-1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "verdict": "FORMAL_PASS" if passed else "FORMAL_FAIL",
        "conclusion_strength": conclusion_strength,
        "experiment_scope": "preregistered_controlled_and_default_v8_views",
        "protocol_sha256": protocol_sha256,
        "input_evidence_sha256": {
            "contemporaneous_pilot": pilot_sha256,
            "judge_stability": judge_sha256,
            "template_certification": certification_sha256,
        },
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
            "versions": list(FORMAL_VERSIONS),
            "arms": [item["arm_id"] for item in FORMAL_ARMS],
            "controlled_arms": list(CONTROLLED_ARM_IDS),
            "default_local_arm": DEFAULT_LOCAL_ARM_ID,
            "development_and_holdout": True,
            "holdout_accessed": True,
            "online_generator_called": True,
            "judge_called_during_comparison": False,
            "judge_is_auxiliary_precondition_only": True,
            "external_evaluator": "week9.cross_version_validation.v1",
        },
        "repetitions": repetitions,
        "planned_run_count": len(expected),
        "completed_run_count": len(actual),
        "frozen_thresholds": thresholds,
        "pass_conditions": pass_conditions,
        "release_conditions": release_conditions,
        "group_fairness": group_fairness,
        "summary_by_dataset_and_arm": summaries,
        "budget_views": {
            "controlled_same_generator": list(CONTROLLED_ARM_IDS),
            "default_configuration": [
                "v4_default_deepseek",
                "v6_default_deepseek",
                "v8_default_local",
            ],
            "equal_frozen_budget": "overall_success plus the same preregistered applicable token, duration, and repair ceilings",
        },
        "records": actual,
        "limitations": [
            "Judge stability is a prerequisite and auxiliary evidence; it never overrides hard checks.",
            "The equal-budget view applies only preregistered dimensions available across the frozen adapters.",
            "V8 local-default cost results are system-level evidence and are not presented as a same-generator causal comparison.",
            "A three-repeat run is labeled preliminary even if all release conditions pass.",
            "Unknown evaluator topologies fail closed instead of receiving inferred validation.",
        ],
    }


def run_formal_comparison(
    *,
    repetitions: int,
    confirmation_id: str,
    pilot_report_path: Path,
    judge_report_path: Path,
    output_root: Path | None = None,
) -> tuple[dict[str, Any], Path]:
    protocol = _read_object(DEFAULT_PROTOCOL)
    errors = validate_protocol(protocol)
    if errors:
        raise PermissionError("frozen protocol is invalid: " + "; ".join(errors))
    assert_online_allowed(allow_online=True, run_kind="formal")
    if protocol.get("holdout_policy", {}).get("access_enabled") is not True:
        raise PermissionError("formal holdout access is not enabled")
    minimum = int(protocol["repetitions"]["smoke_or_exploratory_min"])
    if repetitions < minimum:
        raise PermissionError(f"formal comparison requires at least {minimum} repetitions")
    recorded = str(protocol["online_execution_policy"].get("user_confirmation_id") or "")
    if not confirmation_id or confirmation_id != recorded:
        raise PermissionError("confirmation id does not match the frozen protocol")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise PermissionError("DEEPSEEK_API_KEY is not present in the process environment")
    thresholds = protocol.get("release_thresholds", {})
    if any(
        type(thresholds.get(name)) not in {int, float}
        or not math.isfinite(float(thresholds[name]))
        for name in (
            "development_success_improvement_min",
            "holdout_noninferiority_margin",
            "simple_case_token_budget",
            "simple_case_duration_budget_s",
            "max_content_repairs",
        )
    ):
        raise PermissionError("formal release thresholds are not completely frozen")
    pilot = _verified_report(
        pilot_report_path.resolve(),
        schema_version="v8-contemporaneous-pilot-1",
        verdict="PILOT_PASS",
    )
    judge = _verified_report(
        judge_report_path.resolve(),
        schema_version="v8-judge-stability-1",
        verdict="JUDGE_STABILITY_PASS",
    )
    if pilot.get("guard", {}).get("holdout_accessed") is not False:
        raise PermissionError("pilot report does not prove holdout isolation")
    if judge.get("guard", {}).get("holdout_accessed") is not False:
        raise PermissionError("Judge stability report does not prove holdout isolation")
    certification_path = _latest_template_certification()
    certification = _read_object(certification_path)
    summary = certification.get("summary", {})
    if (
        certification.get("schema_version") != "v8-template-certification-1"
        or summary.get("stage1_gate_passed") is not True
        or summary.get("known_hard_error_false_release_count") != 0
        or summary.get("error_injection_expectations_matched") is not True
    ):
        raise PermissionError("latest template certification does not pass")

    schedule = build_schedule(repetitions)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = (output_root or RESULTS_ROOT).resolve()
    result_dir = root / f"formal-comparison-{timestamp}"
    result_dir.mkdir(parents=True, exist_ok=False)
    runs_dir = result_dir / "runs"
    adapter_root = result_dir / "adapter-work"
    runs_dir.mkdir()
    environment = _controlled_generator_environment(os.environ)
    records: list[dict[str, Any]] = []
    for item in schedule:
        source = (
            DEVELOPMENT_REGRESSION_CASES
            if item["dataset"] == "development"
            else HOLDOUT_CASES
        )
        case: CaseDefinition = source[item["case_id"]]
        result = get_adapter(item["version"]).run(
            case,
            adapter_root,
            timeout_s=900.0,
            allow_online=bool(item["online_generation"]),
            run_kind="formal",
            environment=environment,
        )
        relative = Path("runs") / (
            f"{item['sequence']:03d}-{item['group_id']}-{item['arm_id']}.run-result.json"
        )
        result_path = result_dir / relative
        _write_json(result_path, result.to_dict())
        records.append(
            result_record(
                item,
                result,
                result_file=relative.as_posix(),
                result_sha256=_raw_sha256(result_path),
                thresholds=thresholds,
            )
        )
        print(
            f"[{item['sequence']:03d}/{len(schedule)}] {item['group_id']} "
            f"{item['arm_id']} success={result.overall_success}",
            flush=True,
        )

    report = build_formal_report(
        schedule=schedule,
        records=records,
        repetitions=repetitions,
        protocol=protocol,
        protocol_sha256=PROTOCOL_DIGEST.read_text(encoding="ascii").strip(),
        pilot_sha256=_raw_sha256(pilot_report_path),
        judge_sha256=_raw_sha256(judge_report_path),
        certification_sha256=_raw_sha256(certification_path),
        confirmation_id=confirmation_id,
    )
    report_path = result_dir / "formal-comparison-report.json"
    _write_json(report_path, report)
    report_path.with_suffix(".sha256").write_text(
        f"{_raw_sha256(report_path)}  {report_path.name}\n", encoding="ascii"
    )
    (result_dir / "formal-comparison-summary.md").write_text(
        "# V8 正式 V4/V6/V8 比较\n\n"
        f"- 结论：`{report['verdict']}`\n"
        f"- 证据强度：`{report['conclusion_strength']}`\n"
        f"- 完成：{report['completed_run_count']}/{report['planned_run_count']}\n"
        "- 主判据：版本外确定性 evaluator；Judge 仅为前置辅助证据。\n",
        encoding="utf-8",
    )
    return report, result_dir


def _version_summary(
    records: list[Mapping[str, Any]],
    *,
    default_successes: int,
    equal_budget_successes: int,
) -> dict[str, Any]:
    count = len(records)
    return {
        "run_count": count,
        "default_success_count": default_successes,
        "default_success_rate": default_successes / count if count else None,
        "default_success_wilson_95": list(_wilson(default_successes, count)),
        "equal_budget_success_count": equal_budget_successes,
        "equal_budget_success_rate": equal_budget_successes / count if count else None,
        "equal_budget_success_wilson_95": list(_wilson(equal_budget_successes, count)),
        "infrastructure_failure_count": sum(
            item.get("infrastructure_failure") is True for item in records
        ),
        "total_tokens": _distribution([int(item["total_tokens"]) for item in records]),
        "duration_s": _distribution([float(item["duration_s"]) for item in records]),
        "content_repair_count": _distribution(
            [int(item["content_repair_count"]) for item in records]
        ),
    }


def _rate(summary: Mapping[str, Any]) -> float:
    value = summary.get("default_success_rate")
    return float(value) if value is not None else 0.0


def _wilson(successes: int, count: int) -> tuple[float | None, float | None]:
    if count <= 0:
        return None, None
    z = 1.959963984540054
    proportion = successes / count
    denominator = 1.0 + z * z / count
    center = (proportion + z * z / (2.0 * count)) / denominator
    half = (
        z
        * math.sqrt(
            proportion * (1.0 - proportion) / count
            + z * z / (4.0 * count * count)
        )
        / denominator
    )
    return max(0.0, center - half), min(1.0, center + half)


def _distribution(values: list[int] | list[float]) -> dict[str, Any]:
    if not values:
        return {
            "count": 0,
            "mean": None,
            "median": None,
            "std": None,
            "minimum": None,
            "maximum": None,
        }
    return {
        "count": len(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "std": statistics.stdev(values) if len(values) >= 2 else 0.0,
        "minimum": min(values),
        "maximum": max(values),
    }


def _valid_generator_configuration(value: Any) -> bool:
    return isinstance(value, dict) and (
        value.get("provider") == "deepseek"
        and isinstance(value.get("model"), str)
        and bool(value["model"])
        and type(value.get("temperature")) in {int, float}
        and math.isfinite(float(value["temperature"]))
        and type(value.get("max_tokens")) is int
        and value["max_tokens"] > 0
    )


def _verified_report(path: Path, *, schema_version: str, verdict: str) -> dict[str, Any]:
    report = _read_object(path)
    sidecar = path.with_suffix(".sha256")
    try:
        expected = sidecar.read_text(encoding="ascii").strip().split()[0]
    except (OSError, IndexError) as exc:
        raise ValueError(f"report sidecar unavailable: {path}") from exc
    if expected != _raw_sha256(path):
        raise ValueError(f"report SHA-256 mismatch: {path}")
    if report.get("schema_version") != schema_version or report.get("verdict") != verdict:
        raise ValueError(f"report semantics do not pass: {path}")
    conditions = report.get("pass_conditions")
    if not isinstance(conditions, dict) or not conditions or not all(
        value is True for value in conditions.values()
    ):
        raise ValueError(f"report pass conditions do not pass: {path}")
    return report


def _latest_template_certification() -> Path:
    candidates = sorted(
        RESULTS_ROOT.glob("template-certification-*/certification.json")
    )
    if not candidates:
        raise PermissionError("template certification report is unavailable")
    return candidates[-1]


def _controlled_generator_environment(source: Mapping[str, str]) -> dict[str, str]:
    environment = dict(source)
    environment["LLM_PROVIDER"] = "deepseek"
    for key in tuple(environment):
        if key.startswith("AGENT_") and key.endswith("_PROVIDER"):
            environment.pop(key, None)
        if key.startswith("KIMI_") or key.startswith("ZHIPU_"):
            environment.pop(key, None)
    return environment


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-online", action="store_true")
    parser.add_argument("--confirmation-id", default="")
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--pilot-report", required=False)
    parser.add_argument("--judge-report", required=False)
    parser.add_argument("--out-root", default=None)
    args = parser.parse_args(argv)
    if not args.execute_online:
        print(
            "BLOCKED: formal comparison is an online paid batch with holdout access.",
            file=sys.stderr,
        )
        return 2
    if not args.pilot_report or not args.judge_report:
        print("BLOCKED: verified Pilot and Judge report paths are required", file=sys.stderr)
        return 2
    try:
        report, result_dir = run_formal_comparison(
            repetitions=args.repetitions,
            confirmation_id=args.confirmation_id,
            pilot_report_path=Path(args.pilot_report),
            judge_report_path=Path(args.judge_report),
            output_root=Path(args.out_root) if args.out_root else None,
        )
    except (PermissionError, ValueError, OSError) as exc:
        print(f"BLOCKED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(f"formal={report['verdict']} -> {result_dir}")
    return 0 if report["verdict"] == "FORMAL_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CONTROLLED_ARM_IDS",
    "DEFAULT_LOCAL_ARM_ID",
    "FORMAL_ARMS",
    "FORMAL_VERSIONS",
    "build_formal_report",
    "build_schedule",
    "generator_configuration",
    "main",
    "result_record",
    "run_formal_comparison",
]
