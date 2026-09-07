"""Content-safe contracts for the independently sealed V8 formal batch.

This module owns only sealed-case loading, public scheduling, authorization
validation, and content-free reporting.  It never invokes a model provider.
Private case fields are deliberately excluded from reprs and public records.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
from typing import Any, Iterable, Mapping

from week9.experiments.formal_recovery_harness import RecoveryHarnessResult
from week9.experiments.freeze_independent_holdout_binding import (
    DEFAULT_BINDING,
    canonical_sha256,
    file_sha256,
    validate_binding_protocol,
)
from week9.src.schemas import CheckStatus, FailureKind, RunResult


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FINAL_PROTOCOL = (
    PROJECT_ROOT / "week9" / "configs" / "blinded_formal_protocol_v2.json"
)
FINAL_PROTOCOL_DIGEST = DEFAULT_FINAL_PROTOCOL.with_suffix(".sha256")
DEFAULT_PRIVATE_RESULTS_ROOT = Path(r"D:\v8_formal_private")
AUTHORIZATION_SCHEMA = "v8-blinded-formal-authorization-2"
FINAL_PROTOCOL_SCHEMA = "v8-blinded-formal-protocol-2"
PUBLIC_REPORT_SCHEMA = "v8-blinded-formal-report-2"
INCOMPLETE_REPORT_SCHEMA = "v8-blinded-formal-incomplete-2"
PASS_VERDICT = "FORMAL_PASS"
FAIL_VERDICT = "FORMAL_FAIL"
INCOMPLETE_VERDICT = "INCOMPLETE"
FORMAL_REPETITIONS = 5
PLANNED_RUN_COUNT = 100
FORMAL_DEADLINE_S = 900.0
PINNED_DEEPSEEK_MODEL = "deepseek-v4-flash"
PINNED_DEEPSEEK_THINKING_MODE = "disabled"

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
FORMAL_ARM_IDS = tuple(item["arm_id"] for item in FORMAL_ARMS)
CONTROLLED_ARM_IDS = (
    "v4_default_deepseek",
    "v6_default_deepseek",
    "v8_controlled_deepseek",
)
DEFAULT_LOCAL_ARM_ID = "v8_default_local"
DEVELOPMENT_CASE_REFS = (
    "dual_room_thermal",
    "rc_lowpass",
    "rlc_lowpass",
)
SEALED_CASE_REFS = ("sealed_h01", "sealed_h02")
SIMPLE_CASE_REFS = ("rc_lowpass",)
SAFE_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
LOWER_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class BlindedCaseDefinition:
    """A case-like object whose repr cannot disclose sealed fields."""

    public_ref: str
    case_id: str = field(repr=False)
    dataset: str = field(repr=False)
    domain: str = field(repr=False)
    raw_input: str = field(repr=False)
    required_checks: tuple[str, ...] = field(repr=False)
    expected_physics: Mapping[str, Any] = field(repr=False)
    tags: tuple[str, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if self.public_ref not in SEALED_CASE_REFS:
            raise ValueError("sealed case public reference is invalid")
        if not isinstance(self.case_id, str) or not self.case_id:
            raise ValueError("sealed case identifier is unavailable")
        if self.dataset != "holdout":
            raise ValueError("sealed case dataset differs")
        if not isinstance(self.raw_input, str) or not self.raw_input.strip():
            raise ValueError("sealed requirement is unavailable")
        object.__setattr__(self, "required_checks", tuple(self.required_checks))
        object.__setattr__(self, "expected_physics", dict(self.expected_physics))
        object.__setattr__(self, "tags", tuple(self.tags))

    @property
    def sha256(self) -> str:
        payload = {
            "case_id": self.case_id,
            "dataset": self.dataset,
            "domain": self.domain,
            "raw_input": self.raw_input,
            "required_checks": list(self.required_checks),
            "expected_physics": dict(self.expected_physics),
            "tags": list(self.tags),
        }
        return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class SealedHoldoutBundle:
    manifest_sha256: str
    private_artifact_sha256: str
    cases: tuple[BlindedCaseDefinition, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if LOWER_SHA256.fullmatch(self.manifest_sha256) is None:
            raise ValueError("manifest digest is invalid")
        if LOWER_SHA256.fullmatch(self.private_artifact_sha256) is None:
            raise ValueError("private artifact digest is invalid")
        if len(self.cases) != 2:
            raise ValueError("sealed bundle must contain exactly two cases")
        if tuple(case.public_ref for case in self.cases) != SEALED_CASE_REFS:
            raise ValueError("sealed case slots differ")

    def case_for_ref(self, public_ref: str) -> BlindedCaseDefinition:
        for case in self.cases:
            if case.public_ref == public_ref:
                return case
        raise KeyError("unknown sealed case reference")


def load_sealed_holdout(
    binding_path: Path = DEFAULT_BINDING,
) -> SealedHoldoutBundle:
    """Load the already validated private artifact without returning raw mappings."""

    binding = _read_object(binding_path)
    if validate_binding_protocol(binding):
        raise PermissionError("independent holdout binding is invalid")
    source = binding.get("source_submission")
    if not isinstance(source, Mapping):
        raise PermissionError("independent holdout source binding is missing")
    manifest_path = Path(str(source.get("manifest_path") or "")).resolve()
    private_path = manifest_path.with_name(str(source.get("private_artifact_file_name") or ""))
    if (
        private_path.name != "independent_holdout.private.json"
        or not private_path.is_file()
        or file_sha256(private_path) != source.get("private_artifact_sha256")
    ):
        raise PermissionError("sealed private artifact integrity differs")
    private = _read_object(private_path)
    records = private.get("cases")
    required = private.get("required_checks")
    if not isinstance(records, list) or len(records) != 2 or not isinstance(required, list):
        raise PermissionError("sealed private contract differs")
    cases: list[BlindedCaseDefinition] = []
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise PermissionError("sealed private case contract differs")
        try:
            cases.append(
                BlindedCaseDefinition(
                    public_ref=SEALED_CASE_REFS[index],
                    case_id=str(record["case_id"]),
                    dataset=str(record["dataset"]),
                    domain=str(record["domain"]),
                    raw_input=str(record["raw_input"]),
                    required_checks=tuple(str(item) for item in required),
                    expected_physics=dict(record["expected_physics"]),
                    tags=tuple(str(item) for item in record["tags"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            raise PermissionError("sealed private case contract differs") from None
    return SealedHoldoutBundle(
        manifest_sha256=str(source["manifest_sha256"]),
        private_artifact_sha256=str(source["private_artifact_sha256"]),
        cases=tuple(cases),
    )


def build_blinded_schedule(
    repetitions: int = FORMAL_REPETITIONS,
    *,
    manifest_sha256: str,
) -> tuple[dict[str, Any], ...]:
    """Build 100 public schedule rows without loading sealed case content."""

    if type(repetitions) is not int or repetitions < 1:
        raise ValueError("repetitions must be a positive integer")
    if LOWER_SHA256.fullmatch(manifest_sha256) is None:
        raise ValueError("manifest_sha256 must be a lowercase SHA-256")
    scoped = (
        ("development", DEVELOPMENT_CASE_REFS),
        ("holdout", SEALED_CASE_REFS),
    )
    rows: list[dict[str, Any]] = []
    sequence = 0
    case_position = 0
    for repetition in range(1, repetitions + 1):
        for dataset, refs in scoped:
            rotated = refs[(repetition - 1) % len(refs) :] + refs[: (repetition - 1) % len(refs)]
            for case_ref in rotated:
                offset = (repetition - 1 + case_position) % len(FORMAL_ARMS)
                arm_order = FORMAL_ARMS[offset:] + FORMAL_ARMS[:offset]
                group_ref = f"{dataset}-r{repetition:02d}-{case_ref}"
                commitment = _case_commitment(dataset, case_ref, manifest_sha256)
                for order_in_group, arm in enumerate(arm_order, start=1):
                    sequence += 1
                    rows.append(
                        {
                            "sequence": sequence,
                            "group_ref": group_ref,
                            "dataset": dataset,
                            "repetition": repetition,
                            "case_ref": case_ref,
                            "case_commitment_sha256": commitment,
                            **dict(arm),
                            "order_in_group": order_in_group,
                        }
                    )
                case_position += 1
    return tuple(rows)


def validate_execution_authorization(
    protocol_path: Path,
    authorization_path: Path,
    *,
    confirmation_id: str | None,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Validate an additive one-batch receipt; never reads credentials or holdout."""

    errors: list[str] = []
    try:
        protocol = _read_object(protocol_path)
        authorization = _read_object(authorization_path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return {}, {}, ["authorization_artifact_unavailable"]
    sidecar = protocol_path.with_suffix(".sha256")
    try:
        protocol_digest = sidecar.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        protocol_digest = ""
    if (
        LOWER_SHA256.fullmatch(protocol_digest) is None
        or canonical_sha256(protocol) != protocol_digest
    ):
        errors.append("final_protocol_digest_invalid")
    if (
        protocol.get("schema_version") != FINAL_PROTOCOL_SCHEMA
        or protocol.get("status") != "READY_AWAITING_USER_AUTHORIZATION"
        or protocol.get("formal_execution_allowed") is not False
        or protocol.get("authorization_policy", {}).get("additive_receipt_required") is not True
    ):
        errors.append("final_protocol_not_authorizable")

    receipt_digest = file_sha256(authorization_path)
    receipt_sidecar = authorization_path.with_suffix(".sha256")
    try:
        recorded_receipt_digest = receipt_sidecar.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        recorded_receipt_digest = ""
    if receipt_digest != recorded_receipt_digest:
        errors.append("authorization_receipt_digest_invalid")
    expected_confirmation_hash = (
        hashlib.sha256(confirmation_id.encode("utf-8")).hexdigest()
        if confirmation_id
        else None
    )
    expected_binding_digest = protocol.get("holdout_binding", {}).get("sha256")
    if (
        authorization.get("schema_version") != AUTHORIZATION_SCHEMA
        or authorization.get("status") != "AUTHORIZED_FOR_ONE_BATCH"
        or authorization.get("blinded_protocol_sha256") != protocol_digest
        or authorization.get("holdout_binding_sha256") != expected_binding_digest
        or authorization.get("planned_run_count") != PLANNED_RUN_COUNT
        or authorization.get("user_explicitly_authorized") is not True
        or authorization.get("one_batch_only") is not True
        or authorization.get("credentials_read_after_validation_only") is not True
        or authorization.get("execution_consumed") is not False
        or not isinstance(authorization.get("confirmation_id_sha256"), str)
        or LOWER_SHA256.fullmatch(str(authorization.get("confirmation_id_sha256"))) is None
    ):
        errors.append("authorization_receipt_semantics_invalid")
    if (
        expected_confirmation_hash is not None
        and authorization.get("confirmation_id_sha256") != expected_confirmation_hash
    ):
        errors.append("confirmation_id_mismatch")
    return protocol, authorization, sorted(set(errors))


def authorization_consumption_path(authorization_path: Path) -> Path:
    digest = file_sha256(authorization_path)
    return (
        PROJECT_ROOT
        / "week9"
        / "results"
        / "formal-authorization-consumption"
        / f"{digest}.consumed.json"
    )


def consume_execution_authorization(
    authorization_path: Path,
    *,
    protocol_sha256: str,
    batch_id: str,
) -> Path:
    """Atomically consume a one-batch receipt immediately before execution."""

    if SAFE_REFERENCE.fullmatch(batch_id) is None or ".." in batch_id:
        raise ValueError("unsafe batch_id")
    path = authorization_consumption_path(authorization_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "schema_version": "v8-blinded-formal-authorization-consumption-2",
        "authorization_receipt_sha256": file_sha256(authorization_path),
        "blinded_protocol_sha256": protocol_sha256,
        "batch_id": batch_id,
        "consumed_utc": datetime.now(timezone.utc).isoformat(),
    }
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
    path.with_suffix(".sha256").write_text(file_sha256(path) + "\n", encoding="ascii")
    return path


def public_result_record(
    schedule_item: Mapping[str, Any],
    harness: RecoveryHarnessResult,
    result: RunResult | None,
    *,
    evaluated_result_sha256: str,
    thresholds: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one public row containing no private case or artifact content."""

    simple = schedule_item.get("case_ref") in SIMPLE_CASE_REFS
    if result is None:
        generator = None
        online_used = None
        total_tokens = 0
        duration_s = float(harness.observation.duration_s)
        repairs = 0
        required_status: dict[str, str] = {}
        external_evaluator = None
    else:
        generator = generator_configuration(result)
        online_used = candidate_online_api_used(result)
        total_tokens = result.token_usage.total_tokens
        duration_s = result.duration_s
        repairs = result.counters.content_repair_count
        required_status = {
            check.check_type: check.status.value
            for check in result.checks
            if check.required
        }
        external_evaluator = result.raw_metrics.get("external_evaluator")
    budget_rules = {
        "content_repairs_within_limit": repairs <= int(thresholds["max_content_repairs"]),
    }
    if simple:
        budget_rules.update(
            {
                "simple_tokens_within_limit": total_tokens
                <= int(thresholds["simple_case_token_budget"]),
                "simple_duration_within_limit": duration_s
                <= float(thresholds["simple_case_duration_budget_s"]),
            }
        )
    decision = harness.decision
    return {
        **dict(schedule_item),
        "attempt_id": harness.observation.attempt_id,
        "candidate_contract_sha256": harness.candidate_contract_sha256,
        "evaluated_result_sha256": evaluated_result_sha256,
        "disposition": decision.disposition.value,
        "counts_as_completed_run": decision.counts_as_completed_run,
        "overall_success": decision.overall_success,
        "infrastructure_failure": decision.infrastructure_failure,
        "failure_category": decision.failure_category,
        "external_evaluator": external_evaluator,
        "generator_configuration": generator,
        "candidate_online_api_used": online_used,
        "total_tokens": total_tokens,
        "duration_s": duration_s,
        "content_repair_count": repairs,
        "required_check_status": required_status,
        "budget_rules": budget_rules,
        "equal_budget_success": decision.overall_success and all(budget_rules.values()),
    }


def build_public_formal_report(
    *,
    schedule: Iterable[Mapping[str, Any]],
    records: Iterable[Mapping[str, Any]],
    protocol: Mapping[str, Any],
    protocol_sha256: str,
    binding_sha256: str,
    authorization_sha256: str,
    aborted: bool,
) -> dict[str, Any]:
    expected = [dict(item) for item in schedule]
    actual = [dict(item) for item in records]
    if aborted:
        return {
            "schema_version": INCOMPLETE_REPORT_SCHEMA,
            "verdict": INCOMPLETE_VERDICT,
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "protocol_sha256": protocol_sha256,
            "holdout_binding_sha256": binding_sha256,
            "authorization_receipt_sha256": authorization_sha256,
            "planned_run_count": len(expected),
            "completed_run_count": sum(
                item.get("counts_as_completed_run") is True for item in actual
            ),
            "record_count": len(actual),
            "aborted_on_infrastructure": True,
            "records": actual,
            "privacy_guard": _public_privacy_guard(holdout_accessed=bool(actual)),
        }

    thresholds = dict(protocol["release_thresholds"])
    summaries = _summaries(actual)
    dev_v8 = _rate(summaries["development"]["v8_controlled_deepseek"])
    dev_v6 = _rate(summaries["development"]["v6_default_deepseek"])
    holdout_v8 = _rate(summaries["holdout"]["v8_controlled_deepseek"])
    holdout_v6 = _rate(summaries["holdout"]["v6_default_deepseek"])
    dev_local = _rate(summaries["development"][DEFAULT_LOCAL_ARM_ID])
    holdout_local = _rate(summaries["holdout"][DEFAULT_LOCAL_ARM_ID])
    simple = [
        item
        for item in actual
        if item.get("dataset") == "development"
        and item.get("arm_id") == DEFAULT_LOCAL_ARM_ID
        and item.get("case_ref") in SIMPLE_CASE_REFS
    ]
    local = [item for item in actual if item.get("arm_id") == DEFAULT_LOCAL_ARM_ID]
    release_conditions = {
        "controlled_development_improvement_met": dev_v8 - dev_v6
        >= float(thresholds["development_success_improvement_min"]),
        "controlled_holdout_noninferiority_met": holdout_v8
        + float(thresholds["holdout_noninferiority_margin"])
        >= holdout_v6,
        "default_development_improvement_met": dev_local - dev_v6
        >= float(thresholds["development_success_improvement_min"]),
        "default_holdout_noninferiority_met": holdout_local
        + float(thresholds["holdout_noninferiority_margin"])
        >= holdout_v6,
        "simple_token_budget_met": bool(simple)
        and statistics.median(int(item["total_tokens"]) for item in simple)
        <= float(thresholds["simple_case_token_budget"]),
        "simple_duration_budget_met": bool(simple)
        and statistics.median(float(item["duration_s"]) for item in simple)
        <= float(thresholds["simple_case_duration_budget_s"]),
        "content_repair_budget_met": bool(local)
        and max(int(item["content_repair_count"]) for item in local)
        <= int(thresholds["max_content_repairs"]),
    }
    fairness = _group_fairness(actual)
    schedule_fields = (
        "sequence",
        "group_ref",
        "dataset",
        "repetition",
        "case_ref",
        "case_commitment_sha256",
        "arm_id",
        "version",
        "configuration_view",
        "generation_mode",
        "online_generation",
        "order_in_group",
    )
    pass_conditions = {
        "schedule_complete": [tuple(item.get(key) for key in schedule_fields) for item in actual]
        == [tuple(item.get(key) for key in schedule_fields) for item in expected],
        "all_attempts_count_as_completed": len(actual) == len(expected)
        and all(item.get("counts_as_completed_run") is True for item in actual),
        "no_infrastructure_failures": bool(actual)
        and all(item.get("infrastructure_failure") is False for item in actual),
        "external_evaluator_exact": bool(actual)
        and all(
            item.get("external_evaluator") == "week9.cross_version_validation.v1"
            for item in actual
            if item.get("candidate_contract_sha256")
        ),
        "paired_generator_fairness": bool(fairness)
        and all(item["same_requirement_and_generator_configuration"] for item in fairness),
        "v8_default_local_is_offline": bool(local)
        and all(
            item.get("online_generation") is False
            and item.get("candidate_online_api_used") is False
            and (item.get("generator_configuration") or {}).get("provider")
            == "v8_deterministic_local_grammar"
            for item in local
        ),
        "release_conditions_met": all(release_conditions.values()),
    }
    passed = all(pass_conditions.values())
    return {
        "schema_version": PUBLIC_REPORT_SCHEMA,
        "verdict": PASS_VERDICT if passed else FAIL_VERDICT,
        "conclusion_strength": "FORMAL",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "protocol_sha256": protocol_sha256,
        "holdout_binding_sha256": binding_sha256,
        "authorization_receipt_sha256": authorization_sha256,
        "planned_run_count": len(expected),
        "completed_run_count": len(actual),
        "frozen_thresholds": thresholds,
        "pass_conditions": pass_conditions,
        "release_conditions": release_conditions,
        "group_fairness": fairness,
        "summary_by_dataset_and_arm": summaries,
        "records": actual,
        "privacy_guard": _public_privacy_guard(holdout_accessed=True),
        "limitations": [
            "The two independent holdout cases test only the preregistered certified input scope.",
            "Kimi review is auxiliary and cannot override deterministic external checks.",
            "Private requirements and raw artifacts remain outside the project report.",
        ],
    }


def generator_configuration(result: RunResult) -> dict[str, Any]:
    parameters = dict(result.model_configuration.parameters)
    model = (
        str(parameters.get("generator_model") or "")
        if result.version.lower() == "v8"
        else result.model_configuration.model
    )
    return {
        "provider": result.model_configuration.provider.lower(),
        "model": str(model),
        "temperature": parameters.get("temperature"),
        "max_tokens": parameters.get("max_tokens"),
        "thinking_mode": parameters.get("thinking_mode"),
    }


def candidate_online_api_used(result: RunResult) -> bool | None:
    candidate = result.raw_metrics.get("candidate_raw_metrics")
    if isinstance(candidate, Mapping):
        value = candidate.get("online_api_used")
        if type(value) is bool:
            return value
    value = result.raw_metrics.get("online_api_used")
    return value if type(value) is bool else None


def _case_commitment(dataset: str, case_ref: str, manifest_sha256: str) -> str:
    basis = (
        f"sealed:{manifest_sha256}:{case_ref}"
        if dataset == "holdout"
        else f"development:{case_ref}"
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _summaries(records: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for dataset in ("development", "holdout"):
        output[dataset] = {}
        for arm_id in FORMAL_ARM_IDS:
            selected = [
                item
                for item in records
                if item.get("dataset") == dataset and item.get("arm_id") == arm_id
            ]
            count = len(selected)
            success = sum(item.get("overall_success") is True for item in selected)
            equal = sum(item.get("equal_budget_success") is True for item in selected)
            output[dataset][arm_id] = {
                "run_count": count,
                "default_success_count": success,
                "default_success_rate": success / count if count else None,
                "default_success_wilson_95": list(_wilson(success, count)),
                "equal_budget_success_count": equal,
                "equal_budget_success_rate": equal / count if count else None,
                "infrastructure_failure_count": sum(
                    item.get("infrastructure_failure") is True for item in selected
                ),
                "total_tokens": _distribution(
                    [int(item["total_tokens"]) for item in selected]
                ),
                "duration_s": _distribution(
                    [float(item["duration_s"]) for item in selected]
                ),
                "content_repair_count": _distribution(
                    [int(item["content_repair_count"]) for item in selected]
                ),
            }
    return output


def _group_fairness(records: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for group_ref in sorted({str(item.get("group_ref")) for item in records}):
        group = [item for item in records if item.get("group_ref") == group_ref]
        selected = [item for item in group if item.get("arm_id") in CONTROLLED_ARM_IDS]
        configurations = [item.get("generator_configuration") for item in selected]
        exact = (
            len(selected) == len(CONTROLLED_ARM_IDS)
            and {item.get("arm_id") for item in selected} == set(CONTROLLED_ARM_IDS)
            and {item.get("version") for item in selected} == {"v4", "v6", "v8"}
            and all(_valid_generator_configuration(value) for value in configurations)
            and len({canonical_sha256(value) for value in configurations}) == 1
            and len({item.get("case_commitment_sha256") for item in selected}) == 1
        )
        output.append(
            {
                "group_ref": group_ref,
                "same_requirement_and_generator_configuration": exact,
                "generator_configuration_sha256": (
                    canonical_sha256(configurations[0]) if exact else ""
                ),
            }
        )
    return output


def _valid_generator_configuration(value: Any) -> bool:
    return isinstance(value, Mapping) and (
        value.get("provider") == "deepseek"
        and value.get("model") == PINNED_DEEPSEEK_MODEL
        and value.get("thinking_mode") == PINNED_DEEPSEEK_THINKING_MODE
        and type(value.get("temperature")) in {int, float}
        and math.isfinite(float(value["temperature"]))
        and type(value.get("max_tokens")) is int
        and int(value["max_tokens"]) > 0
    )


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
    half = z * math.sqrt(
        proportion * (1.0 - proportion) / count + z * z / (4.0 * count * count)
    ) / denominator
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


def _public_privacy_guard(*, holdout_accessed: bool) -> dict[str, Any]:
    return {
        "holdout_accessed_by_execution_process": holdout_accessed,
        "private_content_in_public_report": False,
        "case_ids_in_public_report": False,
        "selected_families_in_public_report": False,
        "requirements_in_public_report": False,
        "raw_artifacts_in_public_report": False,
        "private_results_stored_outside_project": True,
        "old_holdout_used": False,
        "historical_formal_records_reused": False,
        "judge_called_during_comparison": False,
    }


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


__all__ = [
    "AUTHORIZATION_SCHEMA",
    "BlindedCaseDefinition",
    "CONTROLLED_ARM_IDS",
    "DEFAULT_FINAL_PROTOCOL",
    "DEFAULT_LOCAL_ARM_ID",
    "DEFAULT_PRIVATE_RESULTS_ROOT",
    "DEVELOPMENT_CASE_REFS",
    "FAIL_VERDICT",
    "FINAL_PROTOCOL_DIGEST",
    "FINAL_PROTOCOL_SCHEMA",
    "FORMAL_ARM_IDS",
    "FORMAL_ARMS",
    "FORMAL_DEADLINE_S",
    "FORMAL_REPETITIONS",
    "INCOMPLETE_VERDICT",
    "PASS_VERDICT",
    "PLANNED_RUN_COUNT",
    "PINNED_DEEPSEEK_MODEL",
    "PINNED_DEEPSEEK_THINKING_MODE",
    "PUBLIC_REPORT_SCHEMA",
    "SEALED_CASE_REFS",
    "SealedHoldoutBundle",
    "authorization_consumption_path",
    "build_blinded_schedule",
    "build_public_formal_report",
    "candidate_online_api_used",
    "consume_execution_authorization",
    "generator_configuration",
    "load_sealed_holdout",
    "public_result_record",
    "validate_execution_authorization",
]
