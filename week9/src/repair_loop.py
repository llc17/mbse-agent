"""Bounded, auditable repair orchestration for V8.

The loop in this module is deliberately provider-neutral.  Callers supply a
validator and a repair provider; this module classifies failures, enforces
budgets, records complete evidence, and decides whether a proposal is committed
or rolled back.  It performs no network or LLM calls itself.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import difflib
import json
import re
from typing import Any, Callable, Mapping, Sequence

from week9.src.quality_gate import QualityGateDecision, decide
from week9.src.schemas import (
    CheckStatus,
    FailureKind,
    InfrastructureRetryOutcome,
    InfrastructureRetryRecord,
    QualityCheckResult,
    RepairAction,
    RepairLayer,
    RepairOutcome,
    RepairRecord,
    RepairStopReason,
    RunCounters,
    sha256_text,
)


@dataclass(frozen=True, slots=True)
class ArtifactBundle:
    """The complete editable artifact snapshot used by one validation run."""

    requirement: str
    sysml: str
    modelica: str

    def __post_init__(self) -> None:
        for name in ("requirement", "sysml", "modelica"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"{name} artifact must be text")

    def artifact(self, layer: RepairLayer) -> str:
        return getattr(self, RepairLayer(layer).value)

    def with_artifact(self, layer: RepairLayer, content: str) -> "ArtifactBundle":
        if not isinstance(content, str):
            raise TypeError("replacement artifact must be text")
        return replace(self, **{RepairLayer(layer).value: content})

    def to_dict(self) -> dict[str, str]:
        return {
            "requirement": self.requirement,
            "sysml": self.sysml,
            "modelica": self.modelica,
        }

    @property
    def sha256(self) -> str:
        return artifact_bundle_sha256(self)


@dataclass(frozen=True, slots=True)
class ValidationSnapshot:
    """One complete validation result plus provider-specific payload."""

    checks: tuple[QualityCheckResult, ...] | Sequence[QualityCheckResult]
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        checks = tuple(self.checks)
        if any(not isinstance(check, QualityCheckResult) for check in checks):
            raise TypeError("validation checks must be QualityCheckResult objects")
        if not isinstance(self.payload, Mapping):
            raise TypeError("validation payload must be a mapping")
        # ``decide`` also rejects ambiguous duplicate check names.
        decide(checks)
        object.__setattr__(self, "checks", checks)
        object.__setattr__(self, "payload", dict(self.payload))

    @property
    def decision(self) -> QualityGateDecision:
        return decide(self.checks)

    @property
    def required_blocker_count(self) -> int:
        return len(self.decision.blocking_checks)


@dataclass(frozen=True, slots=True)
class RepairRequest:
    """Provider input for one deterministically routed content repair."""

    attempt: int
    repair_layer: RepairLayer
    action: RepairAction
    failed_check: QualityCheckResult
    artifacts: ArtifactBundle
    validation: ValidationSnapshot
    allowed_scope: tuple[str, ...]
    failure_fingerprint: str
    previous_actions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "repair_layer", RepairLayer(self.repair_layer))
        object.__setattr__(self, "action", RepairAction(self.action))
        if self.attempt < 1:
            raise ValueError("repair attempts start at 1")
        if not isinstance(self.failed_check, QualityCheckResult):
            raise TypeError("failed_check must be a QualityCheckResult")
        if not isinstance(self.artifacts, ArtifactBundle):
            raise TypeError("artifacts must be an ArtifactBundle")
        if not isinstance(self.validation, ValidationSnapshot):
            raise TypeError("validation must be a ValidationSnapshot")
        if self.failed_check.failure_kind is not FailureKind.CONTENT:
            raise ValueError("repair providers receive content failures only")
        allowed_scope = tuple(self.allowed_scope)
        if not allowed_scope or any(
            not isinstance(item, str) or not item.strip() for item in allowed_scope
        ):
            raise ValueError("allowed_scope must contain explicit non-empty entries")
        if len(allowed_scope) != len(set(allowed_scope)):
            raise ValueError("allowed_scope entries must be unique")
        if not isinstance(self.failure_fingerprint, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.failure_fingerprint
        ):
            raise ValueError("failure_fingerprint must be a SHA-256 digest")
        previous_actions = tuple(self.previous_actions)
        if any(
            not isinstance(item, str) or not item.strip() for item in previous_actions
        ):
            raise ValueError("previous_actions entries must be non-empty strings")
        object.__setattr__(self, "allowed_scope", allowed_scope)
        object.__setattr__(self, "previous_actions", previous_actions)

    @property
    def artifact_before(self) -> str:
        return self.artifacts.artifact(self.repair_layer)


@dataclass(frozen=True, slots=True)
class RepairProposal:
    """A provider proposal for the single artifact named by its request."""

    artifact_after: str
    rationale: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.artifact_after, str):
            raise TypeError("artifact_after must be text")
        if not isinstance(self.rationale, str):
            raise TypeError("proposal rationale must be text")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("proposal metadata must be a mapping")
        object.__setattr__(self, "metadata", dict(self.metadata))


class RepairProviderStop(RuntimeError):
    """A provider's explicit, non-exceptional refusal to guess an artifact."""

    def __init__(self, reason: RepairStopReason, message: str):
        selected_reason = RepairStopReason(reason)
        if selected_reason not in {
            RepairStopReason.UPSTREAM_REQUIRED,
            RepairStopReason.UNREPAIRABLE,
        }:
            raise ValueError("provider stop reason must require upstream work or be unrepairable")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("provider stop message must not be empty")
        self.reason = selected_reason
        self.message = message.strip()
        super().__init__(self.message)


_DEFAULT_ALLOWED_SCOPES: Mapping[RepairLayer, tuple[str, ...]] = {
    RepairLayer.REQUIREMENT: ("requirement_text", "normalized_parameters"),
    RepairLayer.SYSML: ("sysml_source",),
    RepairLayer.MODELICA: (
        "certified_template_id",
        "model_name",
        "normalized_parameter_slots",
    ),
}


@dataclass(frozen=True, slots=True)
class RepairLoopConfig:
    """Independent budgets and deterministic routing overrides."""

    max_repairs: int = 5
    max_content_repairs: int = 3
    max_upstream_redos: int = 2
    max_infrastructure_retries: int = 2
    allowed_scopes: Mapping[RepairLayer | str, Sequence[str]] = field(
        default_factory=lambda: dict(_DEFAULT_ALLOWED_SCOPES)
    )
    check_layer_overrides: Mapping[str, RepairLayer | str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "max_repairs",
            "max_content_repairs",
            "max_upstream_redos",
            "max_infrastructure_retries",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

        if not isinstance(self.allowed_scopes, Mapping):
            raise TypeError("allowed_scopes must be a mapping")
        scopes = dict(_DEFAULT_ALLOWED_SCOPES)
        for raw_layer, raw_values in self.allowed_scopes.items():
            layer = RepairLayer(raw_layer)
            if isinstance(raw_values, (str, bytes)) or not isinstance(
                raw_values, Sequence
            ):
                raise TypeError(f"allowed scope for {layer.value} must be a sequence")
            values = tuple(raw_values)
            if not values or any(not isinstance(item, str) or not item.strip() for item in values):
                raise ValueError(f"allowed scope for {layer.value} must be explicit")
            if len(values) != len(set(values)):
                raise ValueError(f"allowed scope for {layer.value} contains duplicates")
            scopes[layer] = values

        if not isinstance(self.check_layer_overrides, Mapping):
            raise TypeError("check_layer_overrides must be a mapping")
        overrides: dict[str, RepairLayer] = {}
        for check_type, raw_layer in self.check_layer_overrides.items():
            if not isinstance(check_type, str) or not check_type.strip():
                raise ValueError("check-layer override names must be non-empty")
            overrides[check_type] = RepairLayer(raw_layer)
        object.__setattr__(self, "allowed_scopes", scopes)
        object.__setattr__(self, "check_layer_overrides", overrides)


@dataclass(frozen=True, slots=True)
class RepairLoopResult:
    """Final state and every audit record produced by the bounded loop."""

    final_artifacts: ArtifactBundle
    final_validation: ValidationSnapshot
    decision: QualityGateDecision
    repairs: tuple[RepairRecord, ...]
    infrastructure_retries: tuple[InfrastructureRetryRecord, ...]
    validation_history: tuple[ValidationSnapshot, ...]
    previous_actions: tuple[str, ...]
    counters: RunCounters
    stop_reason: RepairStopReason
    provider_error: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "stop_reason", RepairStopReason(self.stop_reason))
        if self.decision != self.final_validation.decision:
            raise ValueError("final decision must be computed from final validation checks")
        if self.counters.repair_attempt_count != len(self.repairs):
            raise ValueError("repair_attempt_count must match repair records")
        if self.counters.infrastructure_retry_count != len(self.infrastructure_retries):
            raise ValueError("infrastructure_retry_count must match retry records")

    @property
    def overall_success(self) -> bool:
        return self.decision.overall_success

    @property
    def artifacts(self) -> ArtifactBundle:
        return self.final_artifacts

    @property
    def validation(self) -> ValidationSnapshot:
        return self.final_validation


ValidationProvider = Callable[[ArtifactBundle], ValidationSnapshot]
RepairProvider = Callable[[RepairRequest], RepairProposal]


_CHECK_LAYER_MAP: Mapping[str, RepairLayer] = {
    "requirement_normalization": RepairLayer.REQUIREMENT,
    "parameter_unit_consistency": RepairLayer.REQUIREMENT,
    "sysml_syntax": RepairLayer.SYSML,
    "cross_model_consistency": RepairLayer.SYSML,
    "certified_template_integrity": RepairLayer.MODELICA,
    "template_certification_anchor": RepairLayer.MODELICA,
    "execution_provenance": RepairLayer.MODELICA,
    "modelica_compile": RepairLayer.MODELICA,
    "simulation": RepairLayer.MODELICA,
    "physical_behavior": RepairLayer.MODELICA,
}

_CHECK_PRIORITY: Mapping[str, int] = {
    "requirement_normalization": 0,
    "sysml_syntax": 10,
    "certified_template_integrity": 20,
    "template_certification_anchor": 30,
    "execution_provenance": 40,
    "modelica_compile": 50,
    "parameter_unit_consistency": 60,
    "simulation": 70,
    "physical_behavior": 80,
    "cross_model_consistency": 90,
}

_RERUN_ONLY_CHECKS = frozenset({"execution_provenance"})
_RERUN_ONLY_CATEGORIES = frozenset(
    {
        "execution_provenance_missing",
        "modelica_execution_missing",
        "simulation_csv_missing",
        "simulation_execution_missing",
        "csv_missing",
        "missing_csv",
    }
)
_TERMINAL_INFRASTRUCTURE_CATEGORIES = frozenset(
    {"process_tree_cleanup_failed"}
)
_RECERTIFICATION_CHECKS = frozenset({"template_certification_anchor"})

_STATUS_PRIORITY = {
    CheckStatus.FAIL: 0,
    CheckStatus.ERROR: 1,
    CheckStatus.UNVERIFIED: 2,
    CheckStatus.NOT_APPLICABLE: 3,
    CheckStatus.PASS: 4,
}

_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_SHA256_RE = re.compile(r"\b[0-9a-f]{64}\b", re.IGNORECASE)
_ISO_TIME_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?\b",
    re.IGNORECASE,
)
_ATTEMPT_RE = re.compile(r"(?i)(?:attempt|retry|#)\s*\d+")
_HEX_UUID_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])", re.IGNORECASE)
_OMC_SOURCE_LOCATION_RE = re.compile(
    r"\[(?:[a-z]:[\\/]|/)[^\]\r\n]+?\.(?:mo|mos)(?::[^\]]*)?\]",
    re.IGNORECASE,
)
_WINDOWS_PATH_RE = re.compile(
    r"(?<![\w])(?:[a-z]:[\\/])(?:[^\s\"'<>|:]+[\\/])*([^\\/\s\"'<>|:]+)",
    re.IGNORECASE,
)
_POSIX_PATH_RE = re.compile(
    r"(?<![\w])/(?:[^/\s\"'<>|]+/)+([^/\s\"'<>|:]+)",
    re.IGNORECASE,
)


def run_repair_loop(
    initial_artifacts: ArtifactBundle,
    validator: ValidationProvider,
    repair_provider: RepairProvider,
    config: RepairLoopConfig | None = None,
    *,
    initial_validation: ValidationSnapshot | None = None,
) -> RepairLoopResult:
    """Validate and repair until success or a deterministic stop condition.

    Infrastructure failures never reach ``repair_provider``.  They are retried
    against the exact same :class:`ArtifactBundle` and recorded separately.
    """

    if not isinstance(initial_artifacts, ArtifactBundle):
        raise TypeError("initial_artifacts must be an ArtifactBundle")
    if not callable(validator) or not callable(repair_provider):
        raise TypeError("validator and repair_provider must be callable")
    selected_config = config or RepairLoopConfig()
    if not isinstance(selected_config, RepairLoopConfig):
        raise TypeError("config must be a RepairLoopConfig")

    counters = RunCounters()
    repairs: list[RepairRecord] = []
    infrastructure_retries: list[InfrastructureRetryRecord] = []
    history: list[ValidationSnapshot] = []
    previous_actions: list[str] = []
    artifacts = initial_artifacts
    ineffective_fingerprint = ""
    ineffective_count = 0

    if initial_validation is None:
        current = _invoke_validator(validator, artifacts)
    elif isinstance(initial_validation, ValidationSnapshot):
        current = initial_validation
    else:
        raise TypeError("initial_validation must be a ValidationSnapshot")
    counters.validation_count += 1
    history.append(current)

    def finish(reason: RepairStopReason, provider_error: str = "") -> RepairLoopResult:
        return RepairLoopResult(
            final_artifacts=artifacts,
            final_validation=current,
            decision=decide(current.checks),
            repairs=tuple(repairs),
            infrastructure_retries=tuple(infrastructure_retries),
            validation_history=tuple(history),
            previous_actions=tuple(previous_actions),
            counters=replace(counters),
            stop_reason=reason,
            provider_error=provider_error,
        )

    def validate_once(bundle: ArtifactBundle, baseline: ValidationSnapshot) -> ValidationSnapshot:
        revalidated = _ensure_required_coverage(
            baseline, _invoke_validator(validator, bundle)
        )
        counters.validation_count += 1
        history.append(revalidated)
        return revalidated

    def retry_infrastructure_once(
        bundle: ArtifactBundle,
        baseline: ValidationSnapshot,
        failure: QualityCheckResult,
    ) -> tuple[ValidationSnapshot, bool, bool]:
        """Revalidate an identical bundle and record whether this failure recovered."""

        retry_attempt = len(infrastructure_retries) + 1
        revalidated = validate_once(bundle, baseline)
        resolved = _infrastructure_failure_resolved(failure, revalidated)
        exhausted = (
            not resolved
            and retry_attempt >= selected_config.max_infrastructure_retries
        )
        outcome = (
            InfrastructureRetryOutcome.RESOLVED
            if resolved
            else (
                InfrastructureRetryOutcome.EXHAUSTED
                if exhausted
                else InfrastructureRetryOutcome.RETRY_FAILED
            )
        )
        infrastructure_retries.append(
            InfrastructureRetryRecord(
                attempt=retry_attempt,
                failed_check=failure.check_type,
                error_category=_error_category(failure),
                failure_fingerprint=failure_fingerprint(failure),
                artifact_bundle_sha256=bundle.sha256,
                original_evidence=_original_evidence(baseline, failure, bundle),
                revalidation_results=list(revalidated.checks),
                outcome=outcome,
            )
        )
        counters.infrastructure_retry_count += 1
        return revalidated, resolved, exhausted

    def append_changed_record(
        *,
        attempt: int,
        failed_check: QualityCheckResult,
        layer: RepairLayer,
        action: RepairAction,
        allowed_scope: tuple[str, ...],
        proposal: RepairProposal,
        baseline_artifacts: ArtifactBundle,
        baseline_validation: ValidationSnapshot,
        candidate_validation: ValidationSnapshot,
        fingerprint_before: str,
        fingerprint_after: str,
        accepted: bool,
        stop_reason: RepairStopReason,
        outcome: RepairOutcome,
    ) -> None:
        artifact_before = baseline_artifacts.artifact(layer)
        artifact_after = proposal.artifact_after
        before_sha256 = sha256_text(artifact_before)
        after_sha256 = sha256_text(artifact_after)
        rolled_back = not accepted
        repairs.append(
            RepairRecord(
                attempt=attempt,
                failed_check=failed_check.check_type,
                failure_kind=FailureKind.CONTENT,
                error_category=_error_category(failed_check),
                repair_layer=layer,
                action=action,
                artifact_type=layer.value,
                failure_fingerprint_before=fingerprint_before,
                previous_actions=list(previous_actions),
                original_evidence=_original_evidence(
                    baseline_validation,
                    failed_check,
                    baseline_artifacts,
                    proposal=proposal,
                ),
                allowed_scope=list(allowed_scope),
                artifact_before=artifact_before,
                artifact_after=artifact_after,
                before_sha256=before_sha256,
                after_sha256=after_sha256,
                diff=unified_artifact_diff(artifact_before, artifact_after, layer.value),
                required_revalidation_checks=list(
                    _required_check_names(baseline_validation)
                ),
                revalidation_results=list(candidate_validation.checks),
                failure_fingerprint_after=fingerprint_after,
                accepted=accepted,
                rolled_back=rolled_back,
                effective_after_sha256=after_sha256 if accepted else before_sha256,
                outcome=outcome,
                stop_reason=stop_reason,
            )
        )
        counters.repair_attempt_count += 1
        if layer is RepairLayer.MODELICA:
            counters.content_repair_count += 1
            if accepted:
                counters.accepted_content_repair_count += 1
        else:
            counters.upstream_redo_count += 1
        if rolled_back:
            counters.rollback_count += 1

    while True:
        if decide(current.checks).overall_success:
            return finish(RepairStopReason.ALL_CHECKS_PASS)

        failed_check = _select_failure(current)
        if failed_check is None:
            return finish(RepairStopReason.UNREPAIRABLE)
        if _is_terminal_failure(failed_check):
            return finish(RepairStopReason.UNREPAIRABLE)

        if _requires_infrastructure_retry(failed_check):
            if len(infrastructure_retries) >= selected_config.max_infrastructure_retries:
                return finish(RepairStopReason.INFRASTRUCTURE_RETRY_EXHAUSTED)
            current, _, exhausted = retry_infrastructure_once(
                artifacts, current, failed_check
            )
            if exhausted:
                return finish(RepairStopReason.INFRASTRUCTURE_RETRY_EXHAUSTED)
            continue

        if failed_check.failure_kind is not FailureKind.CONTENT:
            return finish(RepairStopReason.UNREPAIRABLE)

        layer = repair_layer_for_check(failed_check, selected_config)
        if layer is None:
            return finish(RepairStopReason.UNREPAIRABLE)
        if len(repairs) >= selected_config.max_repairs:
            return finish(RepairStopReason.MAX_REPAIRS)
        if (
            layer is RepairLayer.MODELICA
            and counters.content_repair_count >= selected_config.max_content_repairs
        ):
            return finish(RepairStopReason.MAX_REPAIRS)
        if (
            layer in {RepairLayer.REQUIREMENT, RepairLayer.SYSML}
            and counters.upstream_redo_count >= selected_config.max_upstream_redos
        ):
            return finish(RepairStopReason.MAX_REPAIRS)

        action = _action_for_failure(failed_check, layer)
        attempt = len(repairs) + 1
        allowed_scope = tuple(selected_config.allowed_scopes[layer])
        fingerprint_before = failure_fingerprint(failed_check)
        request = RepairRequest(
            attempt=attempt,
            repair_layer=layer,
            action=action,
            failed_check=failed_check,
            artifacts=artifacts,
            validation=current,
            allowed_scope=allowed_scope,
            failure_fingerprint=fingerprint_before,
            previous_actions=tuple(previous_actions),
        )
        try:
            proposal = repair_provider(request)
            if not isinstance(proposal, RepairProposal):
                raise TypeError("repair provider must return RepairProposal")
        except RepairProviderStop as exc:
            return finish(exc.reason, exc.message)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"[:2000]
            return finish(RepairStopReason.REPAIR_PROVIDER_ERROR, message)

        artifact_before = artifacts.artifact(layer)
        artifact_after = proposal.artifact_after
        before_sha256 = sha256_text(artifact_before)
        after_sha256 = sha256_text(artifact_after)
        action_evidence = _action_evidence(attempt, layer, action, after_sha256)

        if before_sha256 == after_sha256:
            repairs.append(
                RepairRecord(
                    attempt=attempt,
                    failed_check=failed_check.check_type,
                    failure_kind=FailureKind.CONTENT,
                    error_category=_error_category(failed_check),
                    repair_layer=layer,
                    action=action,
                    artifact_type=layer.value,
                    failure_fingerprint_before=fingerprint_before,
                    previous_actions=list(previous_actions),
                    original_evidence=_original_evidence(
                        current, failed_check, artifacts, proposal=proposal
                    ),
                    allowed_scope=list(allowed_scope),
                    artifact_before=artifact_before,
                    artifact_after=artifact_after,
                    before_sha256=before_sha256,
                    after_sha256=after_sha256,
                    diff="",
                    required_revalidation_checks=list(_required_check_names(current)),
                    failure_fingerprint_after=fingerprint_before,
                    accepted=False,
                    rolled_back=False,
                    effective_after_sha256=before_sha256,
                    outcome=RepairOutcome.NO_OP,
                    stop_reason=RepairStopReason.NO_OP,
                )
            )
            previous_actions.append(action_evidence)
            counters.repair_attempt_count += 1
            counters.no_op_count += 1
            return finish(RepairStopReason.NO_OP)

        baseline_artifacts = artifacts
        baseline_validation = current
        candidate_artifacts = artifacts.with_artifact(layer, artifact_after)
        candidate_validation = validate_once(candidate_artifacts, current)

        candidate_stop = RepairStopReason.NONE
        while True:
            candidate_failure = _select_failure(candidate_validation)
            if candidate_failure is None:
                break
            if _is_terminal_failure(candidate_failure):
                candidate_stop = RepairStopReason.UNREPAIRABLE
                break
            if not _requires_infrastructure_retry(candidate_failure):
                break
            if len(infrastructure_retries) >= selected_config.max_infrastructure_retries:
                candidate_stop = RepairStopReason.INFRASTRUCTURE_RETRY_EXHAUSTED
                break
            candidate_validation, _, exhausted = retry_infrastructure_once(
                candidate_artifacts, candidate_validation, candidate_failure
            )
            if exhausted:
                candidate_stop = RepairStopReason.INFRASTRUCTURE_RETRY_EXHAUSTED
                break

        after_failure = _select_failure(candidate_validation)
        fingerprint_after = (
            failure_fingerprint(after_failure) if after_failure is not None else ""
        )
        if candidate_stop is not RepairStopReason.NONE:
            append_changed_record(
                attempt=attempt,
                failed_check=failed_check,
                layer=layer,
                action=action,
                allowed_scope=allowed_scope,
                proposal=proposal,
                baseline_artifacts=baseline_artifacts,
                baseline_validation=baseline_validation,
                candidate_validation=candidate_validation,
                fingerprint_before=fingerprint_before,
                fingerprint_after=fingerprint_after,
                accepted=False,
                stop_reason=candidate_stop,
                outcome=RepairOutcome.ROLLED_BACK,
            )
            previous_actions.append(action_evidence)
            return finish(candidate_stop)

        all_passed = decide(candidate_validation.checks).overall_success
        regression = _has_regression(baseline_validation, candidate_validation)
        improved = _has_objective_improvement(
            baseline_validation, candidate_validation
        )

        if regression:
            stop_reason = RepairStopReason.REGRESSION
            outcome = RepairOutcome.ROLLED_BACK
            accepted = False
        elif all_passed:
            stop_reason = RepairStopReason.ALL_CHECKS_PASS
            outcome = RepairOutcome.PASSED
            accepted = True
        elif improved:
            stop_reason = RepairStopReason.NONE
            outcome = RepairOutcome.ACCEPTED
            accepted = True
        else:
            accepted = False
            outcome = RepairOutcome.ROLLED_BACK
            if fingerprint_after and fingerprint_after == ineffective_fingerprint:
                ineffective_count += 1
            else:
                ineffective_fingerprint = fingerprint_after
                ineffective_count = 1
            stop_reason = (
                RepairStopReason.REPEATED_ERROR
                if ineffective_count >= 2
                else RepairStopReason.NONE
            )

        append_changed_record(
            attempt=attempt,
            failed_check=failed_check,
            layer=layer,
            action=action,
            allowed_scope=allowed_scope,
            proposal=proposal,
            baseline_artifacts=baseline_artifacts,
            baseline_validation=baseline_validation,
            candidate_validation=candidate_validation,
            fingerprint_before=fingerprint_before,
            fingerprint_after=fingerprint_after,
            accepted=accepted,
            stop_reason=stop_reason,
            outcome=outcome,
        )
        previous_actions.append(action_evidence)

        if stop_reason is RepairStopReason.REGRESSION:
            return finish(RepairStopReason.REGRESSION)
        if stop_reason is RepairStopReason.REPEATED_ERROR:
            counters.repeated_error_count += 1
            return finish(RepairStopReason.REPEATED_ERROR)
        if accepted:
            artifacts = candidate_artifacts
            current = candidate_validation
            ineffective_fingerprint = ""
            ineffective_count = 0
            if all_passed:
                return finish(RepairStopReason.ALL_CHECKS_PASS)


def repair_layer_for_check(
    check: QualityCheckResult,
    config: RepairLoopConfig | None = None,
) -> RepairLayer | None:
    """Return the deterministic owner for a content failure."""

    selected_config = config or RepairLoopConfig()
    override = selected_config.check_layer_overrides.get(check.check_type)
    if override is not None:
        return RepairLayer(override)
    for key in ("owner_layer", "repair_layer", "artifact_owner"):
        evidence_owner = check.evidence.get(key)
        if evidence_owner is not None:
            try:
                return RepairLayer(str(evidence_owner).lower())
            except ValueError:
                return None
    mapped = _CHECK_LAYER_MAP.get(check.check_type)
    if mapped is not None:
        return mapped

    name = check.check_type.lower()
    category = check.failure_category.lower()
    joined = f"{name} {category}"
    if any(token in joined for token in ("requirement", "parameter", "unit")):
        return RepairLayer.REQUIREMENT
    if any(token in joined for token in ("sysml", "cross_model")):
        return RepairLayer.SYSML
    if any(
        token in joined
        for token in (
            "modelica",
            "simulation",
            "physical",
            "template",
            "execution_provenance",
        )
    ):
        return RepairLayer.MODELICA
    return None


def failure_fingerprint(check: QualityCheckResult) -> str:
    """Hash stable error identity while excluding run-specific timing and paths."""

    payload = {
        "check_type": check.check_type,
        "status": check.status.value,
        "failure_kind": check.failure_kind.value,
        "failure_category": _stable_text(check.failure_category),
        "reason": _stable_text(str(check.evidence.get("reason", ""))),
        "issues": sorted(_stable_text(issue) for issue in check.issues),
    }
    return sha256_text(_canonical_json(payload))


def artifact_bundle_sha256(bundle: ArtifactBundle) -> str:
    """Hash all three complete artifacts with unambiguous JSON framing."""

    return sha256_text(_canonical_json(bundle.to_dict()))


def unified_artifact_diff(before: str, after: str, artifact_type: str) -> str:
    """Return a complete, deterministic unified diff for two text snapshots."""

    if before == after:
        return ""
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"{artifact_type}.before",
        tofile=f"{artifact_type}.after",
        lineterm="",
    )
    diff = "\n".join(lines)
    # ``splitlines`` still produces a diff for newline-only changes, but keep a
    # deterministic fallback to satisfy the evidence contract on all runtimes.
    if not diff.strip():
        diff = (
            f"--- {artifact_type}.before\n"
            f"+++ {artifact_type}.after\n"
            "@@ complete snapshots differ in line termination @@"
        )
    return diff


def _invoke_validator(
    validator: ValidationProvider, artifacts: ArtifactBundle
) -> ValidationSnapshot:
    try:
        snapshot = validator(artifacts)
        if not isinstance(snapshot, ValidationSnapshot):
            raise TypeError("validator must return ValidationSnapshot")
        return snapshot
    except Exception as exc:
        message = f"validation provider raised {type(exc).__name__}: {exc}"[:2000]
        return ValidationSnapshot(
            checks=(
                QualityCheckResult(
                    check_type="repair_loop_validation",
                    status=CheckStatus.ERROR,
                    required=True,
                    issues=[message],
                    evidence={"reason": "validation_provider_error"},
                    tool_name="V8 repair-loop validation callback",
                    failure_kind=FailureKind.INFRASTRUCTURE,
                    failure_category="validation_provider_error",
                ),
            ),
            payload={"validation_provider_error": message},
        )


def _select_failure(snapshot: ValidationSnapshot) -> QualityCheckResult | None:
    blockers = [
        check
        for check in snapshot.checks
        if check.required and check.status is not CheckStatus.PASS
    ]
    if not blockers:
        return None
    # Never mutate artifacts while a required tool/infrastructure result is
    # uncertain, even if another content FAIL is also present.  Dependency
    # ordering also keeps a blocked physics check from hiding its simulation or
    # compilation prerequisite.
    blockers.sort(
        key=lambda check: (
            0
            if (
                check.failure_kind is FailureKind.INFRASTRUCTURE
                or _requires_infrastructure_retry(check)
                or _is_terminal_failure(check)
            )
            else 1,
            _STATUS_PRIORITY[check.status],
            _CHECK_PRIORITY.get(check.check_type, 1000),
            check.check_type,
        )
    )
    return blockers[0]


def _requires_infrastructure_retry(check: QualityCheckResult) -> bool:
    # Rerun-only identifies how a *blocking* result can be recovered. Once the
    # exact check is PASS it must not remain classified as an infrastructure
    # failure merely because its check_type belongs to the rerun-only set.
    if check.status is CheckStatus.PASS:
        return False
    if _is_terminal_failure(check):
        return False
    category = _error_category(check).strip().lower()
    return (
        check.failure_kind is FailureKind.INFRASTRUCTURE
        or check.check_type in _RERUN_ONLY_CHECKS
        or category in _RERUN_ONLY_CATEGORIES
    )


def _is_terminal_failure(check: QualityCheckResult) -> bool:
    category = _error_category(check).strip().lower()
    return (
        check.check_type in _RECERTIFICATION_CHECKS
        or category in _TERMINAL_INFRASTRUCTURE_CATEGORIES
        or check.failure_kind in {FailureKind.NOT_IMPLEMENTED, FailureKind.UNKNOWN}
    )


def _infrastructure_failure_resolved(
    original: QualityCheckResult, candidate: ValidationSnapshot
) -> bool:
    matching = next(
        (check for check in candidate.checks if check.check_type == original.check_type),
        None,
    )
    return matching is not None and not _requires_infrastructure_retry(matching)


def _required_checks_by_name(
    snapshot: ValidationSnapshot,
) -> dict[str, QualityCheckResult]:
    return {check.check_type: check for check in snapshot.checks if check.required}


def _has_regression(
    baseline: ValidationSnapshot, candidate: ValidationSnapshot
) -> bool:
    before = _required_checks_by_name(baseline)
    after = _required_checks_by_name(candidate)
    before_blockers = {
        name for name, check in before.items() if check.status is not CheckStatus.PASS
    }
    after_blockers = {
        name for name, check in after.items() if check.status is not CheckStatus.PASS
    }
    if after_blockers - before_blockers:
        return True
    for name, previous in before.items():
        current = after.get(name)
        if current is None:
            return True
        if previous.status is CheckStatus.PASS and current.status is not CheckStatus.PASS:
            return True
        if name in before_blockers:
            if _hard_status_severity(current.status) > _hard_status_severity(
                previous.status
            ):
                return True
            if len(current.issues) > len(previous.issues):
                return True
    return False


def _has_objective_improvement(
    baseline: ValidationSnapshot, candidate: ValidationSnapshot
) -> bool:
    before = _required_checks_by_name(baseline)
    after = _required_checks_by_name(candidate)
    before_blockers = {
        name for name, check in before.items() if check.status is not CheckStatus.PASS
    }
    after_blockers = {
        name for name, check in after.items() if check.status is not CheckStatus.PASS
    }
    if after_blockers < before_blockers:
        return True
    for name in before_blockers & after_blockers:
        previous = before[name]
        current = after[name]
        if _hard_status_severity(current.status) < _hard_status_severity(
            previous.status
        ):
            return True
        if len(current.issues) < len(previous.issues):
            return True
    return False


def _hard_status_severity(status: CheckStatus) -> int:
    return {
        CheckStatus.PASS: 0,
        CheckStatus.FAIL: 1,
        CheckStatus.UNVERIFIED: 2,
        CheckStatus.ERROR: 3,
        CheckStatus.NOT_APPLICABLE: 4,
    }[status]


def _ensure_required_coverage(
    previous: ValidationSnapshot, candidate: ValidationSnapshot
) -> ValidationSnapshot:
    required = {
        check.check_type for check in previous.checks if check.required
    }
    if not required:
        return candidate
    normalized: list[QualityCheckResult] = []
    seen: set[str] = set()
    for check in candidate.checks:
        if check.check_type in required and not check.required:
            normalized.append(_missing_revalidation_check(check.check_type))
        else:
            normalized.append(check)
        seen.add(check.check_type)
    for missing in sorted(required - seen):
        normalized.append(_missing_revalidation_check(missing))
    return ValidationSnapshot(checks=tuple(normalized), payload=candidate.payload)


def _missing_revalidation_check(check_type: str) -> QualityCheckResult:
    return QualityCheckResult(
        check_type=check_type,
        status=CheckStatus.ERROR,
        required=True,
        issues=[f"revalidation omitted required check {check_type}"],
        evidence={"reason": "required_revalidation_check_missing"},
        tool_name="V8 repair-loop coverage validator",
        failure_kind=FailureKind.INFRASTRUCTURE,
        failure_category="required_revalidation_check_missing",
    )


def _required_check_names(snapshot: ValidationSnapshot) -> tuple[str, ...]:
    return tuple(check.check_type for check in snapshot.checks if check.required)


def _action_for_failure(
    check: QualityCheckResult, layer: RepairLayer
) -> RepairAction:
    if layer is RepairLayer.REQUIREMENT:
        return RepairAction.UPSTREAM_REQUIREMENT_REDO
    if layer is RepairLayer.SYSML:
        return RepairAction.REBUILD_SYSML
    # Stage 2 only repairs Modelica by reproducing an immutable certified
    # skeleton from its declared slots.  It never authorizes a full-file patch.
    return RepairAction.RESTORE_CERTIFIED_TEMPLATE


def _error_category(check: QualityCheckResult) -> str:
    return (
        check.failure_category
        or str(check.evidence.get("reason", ""))
        or check.check_type
    )


def _original_evidence(
    snapshot: ValidationSnapshot,
    failed_check: QualityCheckResult,
    artifacts: ArtifactBundle,
    *,
    proposal: RepairProposal | None = None,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "failed_check": _check_to_dict(failed_check),
        "validation_snapshot": {
            "checks": [_check_to_dict(check) for check in snapshot.checks],
            "payload": dict(snapshot.payload),
        },
        "artifact_bundle_before": artifacts.to_dict(),
        "artifact_bundle_before_sha256": artifacts.sha256,
    }
    if proposal is not None:
        evidence["proposal"] = {
            "rationale": proposal.rationale,
            "metadata": dict(proposal.metadata),
        }
    return evidence


def _check_to_dict(check: QualityCheckResult) -> dict[str, Any]:
    values = asdict(check)
    values["status"] = check.status.value
    values["failure_kind"] = check.failure_kind.value
    return values


def _action_evidence(
    attempt: int,
    layer: RepairLayer,
    action: RepairAction,
    after_sha256: str,
) -> str:
    return (
        f"attempt={attempt};layer={layer.value};action={action.value};"
        f"after_sha256={after_sha256}"
    )


def _stable_text(value: str) -> str:
    text = " ".join(value.strip().lower().split())
    # OpenModelica prefixes diagnostics with the absolute, per-run ``.mo`` path.
    # Removing the whole source location keeps identical compiler errors stable
    # when a fresh UUID-named working directory (and shifted source lines) is used.
    text = _OMC_SOURCE_LOCATION_RE.sub("[<source>]", text)
    text = _WINDOWS_PATH_RE.sub(lambda match: f"<path>/{match.group(1)}", text)
    text = _POSIX_PATH_RE.sub(lambda match: f"<path>/{match.group(1)}", text)
    text = _UUID_RE.sub("<uuid>", text)
    text = _HEX_UUID_RE.sub("<uuid>", text)
    text = _SHA256_RE.sub("<sha256>", text)
    text = _ISO_TIME_RE.sub("<timestamp>", text)
    return _ATTEMPT_RE.sub("<attempt>", text)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = [
    "ArtifactBundle",
    "RepairLoopConfig",
    "RepairLoopResult",
    "RepairProposal",
    "RepairProvider",
    "RepairProviderStop",
    "RepairRequest",
    "ValidationProvider",
    "ValidationSnapshot",
    "artifact_bundle_sha256",
    "failure_fingerprint",
    "repair_layer_for_check",
    "run_repair_loop",
    "unified_artifact_diff",
]
