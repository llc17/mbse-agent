"""Version-external evaluator and hard release decision for V4/V6/V8."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import time
from typing import Any, Callable, Iterable, Sequence

from week9.src.quality_gate import decide
from week9.src.schemas import (
    ArtifactRecord,
    CheckStatus,
    FailureKind,
    ModelConfiguration,
    NormalizedParameter,
    QualityCheckResult,
    ReproductionManifest,
    RepairStopReason,
    RunCounters,
    RunResult,
    TokenUsage,
)


@dataclass(frozen=True, slots=True)
class EvaluationContext:
    version: str
    case_id: str
    run_id: str
    requirement_text: str
    artifacts: dict[str, ArtifactRecord]
    metadata: dict[str, Any] = field(default_factory=dict)


Validator = Callable[[EvaluationContext], QualityCheckResult]


@dataclass(frozen=True, slots=True)
class ValidatorSpec:
    check_type: str
    validator: Validator
    required: bool = True
    tool_name: str = ""
    tool_version: str = ""


def evaluate(
    *,
    context: EvaluationContext,
    required_check_types: Sequence[str],
    parameters: Iterable[NormalizedParameter] = (),
    precomputed_external_checks: Iterable[QualityCheckResult] = (),
    validators: Iterable[ValidatorSpec] = (),
    repairs=(),
    infrastructure_retries=(),
    counters: RunCounters | None = None,
    token_usage: TokenUsage | None = None,
    duration_s: float = 0.0,
    model_configuration: ModelConfiguration | None = None,
    reproduction: ReproductionManifest | None = None,
    infrastructure_errors: Iterable[str] = (),
    raw_metrics: dict[str, Any] | None = None,
) -> RunResult:
    """Evaluate final artifacts without consulting version-internal success.

    Stage 0 supplies artifact-presence checks and an exception-safe validator
    interface. Checks not implemented until later milestones are emitted as
    ``UNVERIFIED`` rather than being silently treated as successful.
    """

    required_check_types = tuple(required_check_types)
    if not required_check_types:
        raise ValueError("at least one core required check must be configured")
    if len(required_check_types) != len(set(required_check_types)):
        raise ValueError("required_check_types must be unique")
    for key, artifact in context.artifacts.items():
        if key != artifact.artifact_type:
            raise ValueError(
                f"artifact mapping key {key!r} does not match type {artifact.artifact_type!r}"
            )

    checks: dict[str, QualityCheckResult] = {}
    for check in _artifact_presence_checks(context):
        checks[check.check_type] = check

    for check in precomputed_external_checks:
        if check.check_type in checks:
            raise ValueError(f"duplicate external check: {check.check_type}")
        checks[check.check_type] = check

    for spec in validators:
        if spec.check_type in checks:
            raise ValueError(f"duplicate validator check: {spec.check_type}")
        checks[spec.check_type] = _execute_validator(spec, context)

    for check_type in required_check_types:
        if check_type not in checks:
            checks[check_type] = QualityCheckResult(
                check_type=check_type,
                status=CheckStatus.UNVERIFIED,
                required=True,
                issues=["该必需检查尚未执行或没有可验证证据"],
                evidence={"reason": "missing_external_check_result"},
                tool_name="week9.external_evaluator",
                failure_kind=FailureKind.CONTENT,
                failure_category="unverified_required_check",
            )
        elif not checks[check_type].required:
            raise ValueError(f"required check was supplied as optional: {check_type}")

    final_checks = list(checks.values())
    decision = decide(final_checks)
    return RunResult(
        version=context.version,
        case_id=context.case_id,
        run_id=context.run_id,
        requirement_text=context.requirement_text,
        parameters=list(parameters),
        artifacts=dict(context.artifacts),
        checks=final_checks,
        repairs=list(repairs),
        infrastructure_retries=list(infrastructure_retries),
        counters=counters or RunCounters(),
        token_usage=token_usage or TokenUsage(),
        duration_s=duration_s,
        model_configuration=model_configuration or ModelConfiguration(),
        reproduction=reproduction or ReproductionManifest(),
        infrastructure_errors=list(infrastructure_errors),
        raw_metrics=dict(raw_metrics or {}),
        overall_success=decision.overall_success,
        repair_stop_reason=(
            RepairStopReason.ALL_CHECKS_PASS
            if decision.overall_success
            else RepairStopReason.NONE
        ),
        primary_failure_category=decision.primary_failure_category,
        primary_failure_evidence=dict(decision.primary_failure_evidence),
    )


def _artifact_presence_checks(context: EvaluationContext) -> list[QualityCheckResult]:
    checks = [
        _text_presence_check(
            "requirement.present", context.requirement_text, "需求原文缺失"
        )
    ]
    for artifact_name in ("sysml", "modelica", "simulation_csv"):
        checks.append(_artifact_presence_check(artifact_name, context.artifacts.get(artifact_name)))
    return checks


def _text_presence_check(check_type: str, value: str, issue: str) -> QualityCheckResult:
    present = bool(value and value.strip())
    return QualityCheckResult(
        check_type=check_type,
        status=CheckStatus.PASS if present else CheckStatus.UNVERIFIED,
        required=True,
        issues=[] if present else [issue],
        evidence={"present": present, "reason": "" if present else "missing_text"},
        tool_name="week9.artifact_presence",
        tool_version="1",
        failure_kind=FailureKind.NONE if present else FailureKind.CONTENT,
        failure_category="" if present else "missing_artifact",
    )


def _artifact_presence_check(
    artifact_name: str, artifact: ArtifactRecord | None
) -> QualityCheckResult:
    content_present = bool(artifact and artifact.content.strip())
    path_present = False
    path_sha256 = ""
    path_error = ""
    if artifact and artifact.path:
        path = Path(artifact.path)
        if path.is_file():
            try:
                payload = path.read_bytes()
                path_present = bool(payload.strip())
                if path_present:
                    path_sha256 = hashlib.sha256(payload).hexdigest()
            except OSError as exc:
                path_error = f"{type(exc).__name__}: {exc}"

    present = content_present or path_present
    evidence = {
        "artifact": artifact_name,
        "content_present": content_present,
        "path": artifact.path if artifact else "",
        "path_present": path_present,
        "content_sha256": artifact.sha256 if artifact else "",
        "path_sha256": path_sha256,
        "path_error": path_error,
        "reason": "" if present else ("artifact_read_error" if path_error else "missing_artifact"),
    }
    status = CheckStatus.PASS if present else (
        CheckStatus.ERROR if path_error else CheckStatus.UNVERIFIED
    )
    return QualityCheckResult(
        check_type=f"artifact.{artifact_name}.present",
        status=status,
        required=True,
        issues=(
            [] if present else
            [f"{artifact_name} 产物读取失败：{path_error}"] if path_error else
            [f"{artifact_name} 产物缺失或为空"]
        ),
        evidence=evidence,
        tool_name="week9.artifact_presence",
        tool_version="1",
        failure_kind=(
            FailureKind.NONE if present else
            FailureKind.INFRASTRUCTURE if path_error else
            FailureKind.CONTENT
        ),
        failure_category=(
            "" if present else "artifact_read_error" if path_error else "missing_artifact"
        ),
    )


def _execute_validator(spec: ValidatorSpec, context: EvaluationContext) -> QualityCheckResult:
    started_at = time.time()
    try:
        result = spec.validator(context)
        if result.check_type != spec.check_type:
            raise ValueError(
                f"validator returned {result.check_type!r}, expected {spec.check_type!r}"
            )
        if result.required != spec.required:
            raise ValueError("validator returned a mismatched required flag")
        return result
    except Exception as exc:  # checker failures must be observable and blocking
        return QualityCheckResult(
            check_type=spec.check_type,
            status=CheckStatus.ERROR,
            required=spec.required,
            issues=[f"验证器异常：{type(exc).__name__}: {exc}"],
            evidence={"reason": "validator_exception", "exception_type": type(exc).__name__},
            tool_name=spec.tool_name or getattr(spec.validator, "__name__", "validator"),
            tool_version=spec.tool_version,
            duration_s=time.time() - started_at,
            failure_kind=FailureKind.INFRASTRUCTURE,
            failure_category="validator_exception",
        )
