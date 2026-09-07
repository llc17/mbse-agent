"""Deterministic Stage-4 routing and budget evidence.

This module does not generate artifacts, call retrieval, or run validators.  It
turns the evidence already produced by the structured/certified pipeline into
the immutable contracts declared in :mod:`week9.src.schemas`.

The routing trace is deliberately stricter than the generic quality gate.  A
successful certified route must contain the complete ten-check hard-gate
profile; a single passing required check is never enough to claim this path.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping, Sequence

from week9.src.repair_loop import (
    ArtifactBundle,
    RepairLoopConfig,
    RepairLoopResult,
    ValidationSnapshot,
)
from week9.src.schemas import (
    BudgetDimension,
    BudgetEvaluationStatus,
    BudgetEvent,
    BudgetLedger,
    BudgetLimit,
    CheckStatus,
    FailureKind,
    QualityCheckResult,
    RepairAction,
    RepairLayer,
    RepairStopReason,
    RoutingAction,
    RoutingCheckSnapshot,
    RoutingEvent,
    RoutingInputKind,
    RoutingPath,
    RoutingState,
    RoutingTrace,
    budget_event_sha256,
    quality_check_semantic_sha256,
    routing_event_sha256,
    routing_validation_sha256,
)


CERTIFIED_HARD_GATE_PROFILE = (
    "requirement_normalization",
    "sysml_syntax",
    "certified_template_integrity",
    "template_certification_anchor",
    "execution_provenance",
    "modelica_compile",
    "parameter_unit_consistency",
    "simulation",
    "physical_behavior",
    "cross_model_consistency",
)

_CHECK_PRIORITY = {
    name: index for index, name in enumerate(CERTIFIED_HARD_GATE_PROFILE)
}
_STATUS_PRIORITY = {
    CheckStatus.FAIL: 0,
    CheckStatus.ERROR: 1,
    CheckStatus.UNVERIFIED: 2,
    CheckStatus.NOT_APPLICABLE: 3,
    CheckStatus.PASS: 4,
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
_TERMINAL_CHECKS = frozenset({"template_certification_anchor"})
_TERMINAL_CATEGORIES = frozenset({"process_tree_cleanup_failed"})
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class AdaptiveRoutingEvidence:
    """The two Stage-4 records constructed from one completed repair loop."""

    routing: RoutingTrace
    budget_ledger: BudgetLedger


def is_complete_hard_gate_profile(
    checks: Sequence[QualityCheckResult],
) -> bool:
    """Return whether ``checks`` are exactly the certified ten-check profile.

    Validator output ordering is intentionally irrelevant.  Optional or missing
    checks prevent a fast-path claim because they change the frozen profile.
    """

    items = tuple(checks)
    return (
        len(items) == len(CERTIFIED_HARD_GATE_PROFILE)
        and {item.check_type for item in items} == set(CERTIFIED_HARD_GATE_PROFILE)
        and all(item.required for item in items)
    )


def build_preflight_failure_trace(
    *,
    input_kind: RoutingInputKind | str,
    input_snapshot_sha256: str,
    reason_codes: Sequence[str],
    certified_template_id: str = "",
    normalization_mode: str = "local_structured_normalization",
    retrieval_evidence: Mapping[str, Any] | Any | None = None,
) -> RoutingTrace:
    """Record an explicit structured-input failure before artifacts exist.

    This contract is used for missing certified templates and unresolved
    parameter/unit conflicts.  It intentionally carries no fabricated hard-gate
    or artifact evidence because validation never started.
    """

    selected_input_kind = RoutingInputKind(input_kind)
    if selected_input_kind not in {
        RoutingInputKind.STRUCTURED_OBSERVATIONS,
        RoutingInputKind.PRENORMALIZED_PARAMETERS,
    }:
        raise ValueError("preflight failures require a structured input kind")
    _require_sha256(input_snapshot_sha256, "input_snapshot_sha256")
    reasons = tuple(dict.fromkeys(reason_codes))
    if not reasons or any(
        not isinstance(item, str) or not item.strip() for item in reasons
    ):
        raise ValueError("preflight failure reasons must be unique non-empty codes")
    if not isinstance(normalization_mode, str) or not normalization_mode.strip():
        raise ValueError("normalization_mode must not be empty")

    retrieval = _evidence_mapping(retrieval_evidence, "retrieval_evidence")
    attempted, triggered, retrieval_reason, _ = _retrieval_summary(
        retrieval, input_snapshot_sha256
    )
    events: list[RoutingEvent] = []
    state = _append_routing_event(
        events,
        from_state=RoutingState.RECEIVED,
        to_state=RoutingState.NORMALIZING,
        action=(
            RoutingAction.ACCEPT_STRUCTURED_INPUT
            if selected_input_kind is RoutingInputKind.STRUCTURED_OBSERVATIONS
            else RoutingAction.ACCEPT_PRENORMALIZED_INPUT
        ),
        reason_code="structured_input_declared",
        input_snapshot_sha256=input_snapshot_sha256,
    )
    if attempted:
        state = _append_routing_event(
            events,
            from_state=state,
            to_state=RoutingState.RETRIEVING,
            action=RoutingAction.RETRIEVE_CERTIFIED_ALIAS,
            reason_code=f"retrieval:{retrieval_reason}",
            input_snapshot_sha256=input_snapshot_sha256,
        )
    missing_template = any(
        item in {"no_certified_template", "unknown_template"} for item in reasons
    )
    _append_routing_event(
        events,
        from_state=state,
        to_state=RoutingState.FAILED,
        action=(
            RoutingAction.STOP_RECERTIFICATION_REQUIRED
            if missing_template
            else RoutingAction.STOP_UPSTREAM_REQUIRED
        ),
        reason_code=f"preflight:{reasons[0]}",
        input_snapshot_sha256=input_snapshot_sha256,
    )
    return RoutingTrace(
        input_kind=selected_input_kind,
        path=RoutingPath.ESCALATION_PATH,
        final_state=RoutingState.FAILED,
        certified_template_id=certified_template_id,
        normalization_mode=normalization_mode,
        events=tuple(events),
        full_hard_gate_profile=False,
        escalation_reasons=tuple(
            dict.fromkeys(
                (*reasons, *((f"retrieval:{retrieval_reason}",) if attempted else ()))
            )
        ),
        retrieval_attempted=attempted,
        retrieval_triggered=triggered,
        natural_language_interpretation_performed=False,
        online_api_used=False,
    )


def build_routing_trace(
    *,
    loop: RepairLoopResult,
    initial_artifacts: ArtifactBundle,
    initial_validation: ValidationSnapshot,
    input_kind: RoutingInputKind | str,
    input_snapshot_sha256: str,
    certified_template_id: str = "",
    normalization_mode: str | None = None,
    normalization_evidence: Mapping[str, Any] | Any | None = None,
    retrieval_evidence: Mapping[str, Any] | Any | None = None,
    post_gate_check: QualityCheckResult | None = None,
    online_api_used: bool = False,
) -> RoutingTrace:
    """Build the deterministic route for an already completed loop.

    ``input_snapshot_sha256`` is supplied by the structured entry point: it is
    the ordered observation hash for structured observations and the canonical
    normalized-parameter hash for pre-normalized input.  Retrieval evidence, if
    present, must be bound to this same hash.
    """

    if not isinstance(loop, RepairLoopResult):
        raise TypeError("loop must be a RepairLoopResult")
    if not isinstance(initial_artifacts, ArtifactBundle):
        raise TypeError("initial_artifacts must be an ArtifactBundle")
    if not isinstance(initial_validation, ValidationSnapshot):
        raise TypeError("initial_validation must be a ValidationSnapshot")
    selected_input_kind = RoutingInputKind(input_kind)
    _require_sha256(input_snapshot_sha256, "input_snapshot_sha256")
    if type(online_api_used) is not bool:
        raise TypeError("online_api_used must be a bool")
    if post_gate_check is not None and (
        not isinstance(post_gate_check, QualityCheckResult)
        or post_gate_check.check_type != "traceability_integrity"
        or not post_gate_check.required
    ):
        raise ValueError(
            "post_gate_check must be the required traceability_integrity check"
        )

    if not loop.validation_history:
        raise ValueError("routing requires the loop's initial validation history")
    if loop.counters.validation_count != len(loop.validation_history):
        raise ValueError("validation_count must match validation history")
    if routing_validation_sha256(loop.validation_history[0].checks) != (
        routing_validation_sha256(initial_validation.checks)
    ):
        raise ValueError("initial validation differs from the loop's first snapshot")
    payload_bundle_hash = initial_validation.payload.get("artifact_bundle_sha256")
    if payload_bundle_hash and payload_bundle_hash != initial_artifacts.sha256:
        raise ValueError("initial validation is bound to a different artifact bundle")

    normalization = _evidence_mapping(
        normalization_evidence, "normalization_evidence"
    )
    retrieval = _evidence_mapping(retrieval_evidence, "retrieval_evidence")
    attempted, triggered, retrieval_reason, retrieval_tokens = (
        _retrieval_summary(retrieval, input_snapshot_sha256)
    )
    selected_normalization_mode = normalization_mode or _normalization_mode(
        selected_input_kind, normalization, triggered
    )
    if not isinstance(selected_normalization_mode, str) or not (
        selected_normalization_mode.strip()
    ):
        raise ValueError("normalization_mode must be a non-empty string")

    initial_checks = tuple(
        _routing_check_snapshot(item)
        for item in sorted(initial_validation.checks, key=lambda item: item.check_type)
    )
    initial_validation_hash = routing_validation_sha256(initial_checks)
    full_profile = is_complete_hard_gate_profile(initial_validation.checks)

    if (
        selected_input_kind is RoutingInputKind.RAW_NATURAL_LANGUAGE
        and selected_normalization_mode == "not_implemented"
    ):
        if loop.overall_success or loop.repairs or loop.infrastructure_retries:
            raise ValueError("the raw natural-language entry must fail before escalation")
        event = _make_routing_event(
            sequence=1,
            from_state=RoutingState.RECEIVED,
            to_state=RoutingState.FAILED,
            action=RoutingAction.STOP_NOT_IMPLEMENTED,
            reason_code="natural_language_generator_not_implemented",
            input_snapshot_sha256=input_snapshot_sha256,
            artifact_bundle_sha256=initial_artifacts.sha256,
            validation_sha256=initial_validation_hash,
        )
        return RoutingTrace(
            input_kind=selected_input_kind,
            path=RoutingPath.UNAVAILABLE_ENTRY,
            final_state=RoutingState.FAILED,
            normalization_mode=selected_normalization_mode,
            events=(event,),
            initial_checks=initial_checks,
            initial_artifact_bundle_sha256=initial_artifacts.sha256,
            initial_validation_sha256=initial_validation_hash,
            full_hard_gate_profile=False,
            escalation_reasons=("natural_language_generator_not_implemented",),
            natural_language_interpretation_performed=False,
            online_api_used=online_api_used,
        )

    if selected_input_kind not in {
        RoutingInputKind.RAW_NATURAL_LANGUAGE,
        RoutingInputKind.STRUCTURED_OBSERVATIONS,
        RoutingInputKind.PRENORMALIZED_PARAMETERS,
    }:
        raise ValueError("routing input kind is not an evaluated Stage-4 entry")
    if not isinstance(certified_template_id, str) or not certified_template_id.strip():
        raise ValueError("a structured route requires a declared certified template")

    events: list[RoutingEvent] = []
    state = RoutingState.RECEIVED
    if selected_input_kind is RoutingInputKind.RAW_NATURAL_LANGUAGE:
        accept_action = RoutingAction.INTERPRET_NATURAL_LANGUAGE
        accept_reason = "natural_language_interpreted_to_structured_contract"
    elif selected_input_kind is RoutingInputKind.STRUCTURED_OBSERVATIONS:
        accept_action = RoutingAction.ACCEPT_STRUCTURED_INPUT
        accept_reason = "structured_input_declared"
    else:
        accept_action = RoutingAction.ACCEPT_PRENORMALIZED_INPUT
        accept_reason = "structured_input_declared"
    state = _append_routing_event(
        events,
        from_state=state,
        to_state=RoutingState.NORMALIZING,
        action=accept_action,
        reason_code=accept_reason,
        input_snapshot_sha256=input_snapshot_sha256,
    )

    if attempted:
        state = _append_routing_event(
            events,
            from_state=state,
            to_state=RoutingState.RETRIEVING,
            action=RoutingAction.RETRIEVE_CERTIFIED_ALIAS,
            reason_code=f"retrieval:{retrieval_reason}",
            input_snapshot_sha256=input_snapshot_sha256,
        )
        validation_from = RoutingState.RETRIEVING
    else:
        validation_from = RoutingState.NORMALIZING
    state = _append_routing_event(
        events,
        from_state=validation_from,
        to_state=RoutingState.CERTIFIED_GATE,
        action=RoutingAction.VALIDATE_CERTIFIED_SNAPSHOT,
        reason_code="validate_exact_certified_snapshot",
        input_snapshot_sha256=input_snapshot_sha256,
        artifact_bundle_sha256=initial_artifacts.sha256,
        validation_sha256=initial_validation_hash,
    )

    initial_all_pass = full_profile and all(
        item.status is CheckStatus.PASS for item in initial_validation.checks
    )
    normalization_issue_codes = _normalization_issue_codes(normalization)
    normalization_ready = (
        selected_input_kind is RoutingInputKind.PRENORMALIZED_PARAMETERS
        and normalization.get("ready", True) is not False
        and not normalization_issue_codes
    ) or (
        selected_input_kind in {
            RoutingInputKind.STRUCTURED_OBSERVATIONS,
            RoutingInputKind.RAW_NATURAL_LANGUAGE,
        }
        and normalization.get("ready") is True
        and not normalization_issue_codes
    )
    retrieval_not_requested = not retrieval or retrieval_reason == "not_requested"
    no_loop_escalation = not loop.repairs and not loop.infrastructure_retries
    unchanged = loop.final_artifacts.sha256 == initial_artifacts.sha256
    fast_path = (
        initial_all_pass
        and normalization_ready
        and loop.overall_success
        and no_loop_escalation
        and not attempted
        and retrieval_not_requested
        and loop.counters.validation_count == 1
        and unchanged
        and not online_api_used
        and (
            post_gate_check is None
            or post_gate_check.status is CheckStatus.PASS
        )
    )

    if fast_path:
        _append_routing_event(
            events,
            from_state=state,
            to_state=RoutingState.SUCCEEDED,
            action=RoutingAction.RELEASE_INITIAL_GATE,
            reason_code="initial_hard_gate_passed",
            input_snapshot_sha256=input_snapshot_sha256,
            artifact_bundle_sha256=initial_artifacts.sha256,
            validation_sha256=initial_validation_hash,
        )
        return RoutingTrace(
            input_kind=selected_input_kind,
            path=RoutingPath.FAST_PATH,
            final_state=RoutingState.SUCCEEDED,
            certified_template_id=certified_template_id,
            normalization_mode=selected_normalization_mode,
            events=tuple(events),
            initial_checks=initial_checks,
            initial_artifact_bundle_sha256=initial_artifacts.sha256,
            initial_validation_sha256=initial_validation_hash,
            full_hard_gate_profile=True,
            retrieval_attempted=False,
            retrieval_triggered=False,
            repair_attempted=False,
            infrastructure_retry_attempted=False,
            natural_language_interpretation_performed=(
                selected_input_kind is RoutingInputKind.RAW_NATURAL_LANGUAGE
            ),
            online_api_used=False,
        )

    post_gate_failed = (
        post_gate_check is not None
        and post_gate_check.status is not CheckStatus.PASS
    )
    if initial_all_pass and not attempted and not post_gate_failed:
        raise ValueError("passing initial hard gate is inconsistent with loop escalation")
    if loop.overall_success and not full_profile:
        raise ValueError("successful certified routing requires the complete hard-gate profile")

    escalation_reasons = _escalation_reasons(
        normalization,
        retrieval,
        initial_validation,
        loop,
    )
    if selected_input_kind is RoutingInputKind.RAW_NATURAL_LANGUAGE:
        escalation_reasons = tuple(
            dict.fromkeys(
                ("natural_language_interpretation_performed", *escalation_reasons)
            )
        )
    if post_gate_failed:
        escalation_reasons = tuple(
            dict.fromkeys(
                (*escalation_reasons, "traceability_integrity_failed")
            )
        )
    primary = _select_primary_blocker(initial_validation)
    has_bounded_activity = bool(loop.repairs or loop.infrastructure_retries)
    if has_bounded_activity:
        state = _append_routing_event(
            events,
            from_state=state,
            to_state=RoutingState.BOUNDED_REPAIR,
            action=RoutingAction.ENTER_BOUNDED_REPAIR,
            reason_code=_blocker_reason(primary),
            input_snapshot_sha256=input_snapshot_sha256,
            artifact_bundle_sha256=initial_artifacts.sha256,
            validation_sha256=initial_validation_hash,
        )
        action_groups = (
            ("infrastructure", "repair")
            if primary is not None and _is_infrastructure_or_terminal(primary)
            else ("repair", "infrastructure")
        )
        for group in action_groups:
            if group == "infrastructure":
                for retry in loop.infrastructure_retries:
                    state = _append_routing_event(
                        events,
                        from_state=state,
                        to_state=RoutingState.BOUNDED_REPAIR,
                        action=RoutingAction.RETRY_IDENTICAL_SNAPSHOT,
                        reason_code=(
                            f"infrastructure_retry:{retry.failed_check}:"
                            f"{retry.error_category}"
                        ),
                        input_snapshot_sha256=input_snapshot_sha256,
                        artifact_bundle_sha256=retry.artifact_bundle_sha256,
                        validation_sha256=routing_validation_sha256(
                            retry.revalidation_results
                        ),
                    )
            else:
                for repair in loop.repairs:
                    state = _append_routing_event(
                        events,
                        from_state=state,
                        to_state=RoutingState.BOUNDED_REPAIR,
                        action=_routing_action_for_repair(repair.repair_layer, repair.action),
                        reason_code=(
                            f"repair:{repair.failed_check}:{repair.error_category}"
                        ),
                        input_snapshot_sha256=input_snapshot_sha256,
                        artifact_bundle_sha256=_repair_baseline_bundle_sha256(repair),
                        validation_sha256=(
                            routing_validation_sha256(repair.revalidation_results)
                            if repair.revalidation_results
                            else ""
                        ),
                    )

    post_gate_is_only_blocker = post_gate_failed and loop.overall_success
    final_state = (
        RoutingState.SUCCEEDED
        if loop.overall_success and not post_gate_failed
        else RoutingState.FAILED
    )
    final_action = (
        RoutingAction.STOP_UPSTREAM_REQUIRED
        if post_gate_is_only_blocker
        else _terminal_action(loop, primary)
    )
    final_reason = (
        "post_gate_required_check_failed:traceability_integrity"
        if post_gate_is_only_blocker
        else (
            "all_hard_checks_pass_after_escalation"
            if loop.overall_success
            else f"repair_stop:{loop.stop_reason.value or 'unrepairable'}"
        )
    )
    _append_routing_event(
        events,
        from_state=state,
        to_state=final_state,
        action=final_action,
        reason_code=final_reason,
        input_snapshot_sha256=input_snapshot_sha256,
        artifact_bundle_sha256=loop.final_artifacts.sha256,
        validation_sha256=routing_validation_sha256(
            (
                *loop.final_validation.checks,
                *((post_gate_check,) if post_gate_check is not None else ()),
            )
        ),
    )
    return RoutingTrace(
        input_kind=selected_input_kind,
        path=RoutingPath.ESCALATION_PATH,
        final_state=final_state,
        certified_template_id=certified_template_id,
        normalization_mode=selected_normalization_mode,
        events=tuple(events),
        initial_checks=initial_checks,
        initial_artifact_bundle_sha256=initial_artifacts.sha256,
        initial_validation_sha256=initial_validation_hash,
        full_hard_gate_profile=full_profile,
        escalation_reasons=escalation_reasons,
        retrieval_attempted=attempted,
        retrieval_triggered=triggered,
        repair_attempted=bool(loop.repairs),
        infrastructure_retry_attempted=bool(loop.infrastructure_retries),
        reviewer_rounds=0,
        natural_language_interpretation_performed=(
            selected_input_kind is RoutingInputKind.RAW_NATURAL_LANGUAGE
        ),
        online_api_used=online_api_used,
    )


def build_budget_ledger(
    *,
    loop: RepairLoopResult,
    config: RepairLoopConfig,
    initial_artifacts: ArtifactBundle,
    input_snapshot_sha256: str,
    route: RoutingTrace,
    retrieval_evidence: Mapping[str, Any] | Any | None,
    max_retrieval_events: int,
    max_retrieval_tokens: int,
    observed_model_tokens: int = 0,
    observed_duration_s: float = 0.0,
    simple_case_token_budget: int | None = None,
    simple_case_duration_budget_s: float | None = None,
) -> BudgetLedger:
    """Construct the exact record-derived hard-budget ledger.

    Event order is canonical rather than wall-clock based: retrieval, validation,
    repair records, then infrastructure records.  The underlying loop does not
    retain a unified cross-kind timestamp order, so this stable ordering avoids
    inventing one while preserving every exact charge.
    """

    if not isinstance(loop, RepairLoopResult):
        raise TypeError("loop must be a RepairLoopResult")
    if not isinstance(config, RepairLoopConfig):
        raise TypeError("config must be a RepairLoopConfig")
    if not isinstance(initial_artifacts, ArtifactBundle):
        raise TypeError("initial_artifacts must be an ArtifactBundle")
    if not isinstance(route, RoutingTrace) or route.path is RoutingPath.NOT_EVALUATED:
        raise TypeError("route must be an evaluated RoutingTrace")
    _require_sha256(input_snapshot_sha256, "input_snapshot_sha256")
    if route.initial_artifact_bundle_sha256 != initial_artifacts.sha256:
        raise ValueError("route and budget ledger use different initial artifacts")
    if any(
        event.input_snapshot_sha256 != input_snapshot_sha256
        for event in route.events
    ):
        raise ValueError("route and budget ledger use different input snapshots")
    for name, value in (
        ("max_retrieval_events", max_retrieval_events),
        ("max_retrieval_tokens", max_retrieval_tokens),
        ("observed_model_tokens", observed_model_tokens),
    ):
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    if loop.counters.validation_count != len(loop.validation_history):
        raise ValueError("validation_count must match validation history")
    changed_modelica = sum(
        repair.repair_layer is RepairLayer.MODELICA and not repair.is_noop
        for repair in loop.repairs
    )
    changed_upstream = sum(
        repair.repair_layer is not RepairLayer.MODELICA and not repair.is_noop
        for repair in loop.repairs
    )
    if (
        loop.counters.repair_attempt_count != len(loop.repairs)
        or loop.counters.content_repair_count != changed_modelica
        or loop.counters.upstream_redo_count != changed_upstream
        or loop.counters.infrastructure_retry_count
        != len(loop.infrastructure_retries)
    ):
        raise ValueError("loop counters do not match auditable budget records")

    retrieval = _evidence_mapping(retrieval_evidence, "retrieval_evidence")
    attempted, triggered, retrieval_reason, retrieval_tokens = _retrieval_summary(
        retrieval, input_snapshot_sha256
    )
    if attempted is not route.retrieval_attempted or triggered is not route.retrieval_triggered:
        raise ValueError("retrieval evidence differs from the routing trace")

    limits_by_dimension = {
        BudgetDimension.VALIDATION: (
            1 + config.max_repairs + config.max_infrastructure_retries
        ),
        BudgetDimension.REPAIR_ATTEMPT: config.max_repairs,
        BudgetDimension.MODELICA_CHANGED_REPAIR: config.max_content_repairs,
        BudgetDimension.UPSTREAM_CHANGED_REPAIR: config.max_upstream_redos,
        BudgetDimension.INFRASTRUCTURE_RETRY: config.max_infrastructure_retries,
        BudgetDimension.RETRIEVAL_EVENT: max_retrieval_events,
        BudgetDimension.RETRIEVAL_CONTEXT_TOKEN: max_retrieval_tokens,
    }
    limits = tuple(
        BudgetLimit(dimension=dimension, limit=limits_by_dimension[dimension])
        for dimension in BudgetDimension
    )

    events: list[BudgetEvent] = []
    usage = {dimension: 0 for dimension in BudgetDimension}
    if attempted:
        _append_budget_event(
            events,
            usage,
            dimension=BudgetDimension.RETRIEVAL_EVENT,
            amount=1,
            action=f"retrieval:{retrieval_reason}",
            subject_sha256=input_snapshot_sha256,
        )
    if retrieval_tokens:
        token_subject = retrieval.get("corpus_sha256", input_snapshot_sha256)
        if not isinstance(token_subject, str) or not _SHA256_RE.fullmatch(token_subject):
            token_subject = input_snapshot_sha256
        _append_budget_event(
            events,
            usage,
            dimension=BudgetDimension.RETRIEVAL_CONTEXT_TOKEN,
            amount=retrieval_tokens,
            action="retrieval_context_selected",
            subject_sha256=token_subject,
        )

    for index, snapshot in enumerate(loop.validation_history, start=1):
        subject = routing_validation_sha256(snapshot.checks)
        _append_budget_event(
            events,
            usage,
            dimension=BudgetDimension.VALIDATION,
            amount=1,
            action=f"validation:{index}",
            subject_sha256=subject,
        )

    for repair in loop.repairs:
        _append_budget_event(
            events,
            usage,
            dimension=BudgetDimension.REPAIR_ATTEMPT,
            amount=1,
            action=f"repair_attempt:{repair.action.value}",
            subject_sha256=repair.failure_fingerprint_before,
        )
        if repair.is_noop:
            continue
        changed_dimension = (
            BudgetDimension.MODELICA_CHANGED_REPAIR
            if repair.repair_layer is RepairLayer.MODELICA
            else BudgetDimension.UPSTREAM_CHANGED_REPAIR
        )
        _append_budget_event(
            events,
            usage,
            dimension=changed_dimension,
            amount=1,
            action=f"changed_repair:{repair.action.value}",
            subject_sha256=repair.after_sha256,
        )

    for retry in loop.infrastructure_retries:
        _append_budget_event(
            events,
            usage,
            dimension=BudgetDimension.INFRASTRUCTURE_RETRY,
            amount=1,
            action=f"infrastructure_retry:{retry.failed_check}",
            subject_sha256=retry.artifact_bundle_sha256,
        )

    token_status = _simple_budget_status(
        route.path,
        simple_case_token_budget,
        observed_model_tokens,
    )
    duration_status = _simple_budget_status(
        route.path,
        simple_case_duration_budget_s,
        observed_duration_s,
    )
    return BudgetLedger(
        limits=limits,
        events=tuple(events),
        simple_case_token_budget=simple_case_token_budget,
        simple_case_duration_budget_s=simple_case_duration_budget_s,
        token_budget_status=token_status,
        duration_budget_status=duration_status,
        observed_model_tokens=observed_model_tokens,
        observed_duration_s=observed_duration_s,
    )


def build_adaptive_routing_evidence(
    *,
    loop: RepairLoopResult,
    config: RepairLoopConfig,
    initial_artifacts: ArtifactBundle,
    initial_validation: ValidationSnapshot,
    input_kind: RoutingInputKind | str,
    input_snapshot_sha256: str,
    certified_template_id: str = "",
    normalization_mode: str | None = None,
    normalization_evidence: Mapping[str, Any] | Any | None = None,
    retrieval_evidence: Mapping[str, Any] | Any | None = None,
    post_gate_check: QualityCheckResult | None = None,
    max_retrieval_events: int,
    max_retrieval_tokens: int,
    observed_model_tokens: int = 0,
    observed_duration_s: float = 0.0,
    simple_case_token_budget: int | None = None,
    simple_case_duration_budget_s: float | None = None,
    online_api_used: bool = False,
) -> AdaptiveRoutingEvidence:
    """Build a mutually bound routing trace and budget ledger."""

    route = build_routing_trace(
        loop=loop,
        initial_artifacts=initial_artifacts,
        initial_validation=initial_validation,
        input_kind=input_kind,
        input_snapshot_sha256=input_snapshot_sha256,
        certified_template_id=certified_template_id,
        normalization_mode=normalization_mode,
        normalization_evidence=normalization_evidence,
        retrieval_evidence=retrieval_evidence,
        post_gate_check=post_gate_check,
        online_api_used=online_api_used,
    )
    ledger = build_budget_ledger(
        loop=loop,
        config=config,
        initial_artifacts=initial_artifacts,
        input_snapshot_sha256=input_snapshot_sha256,
        route=route,
        retrieval_evidence=retrieval_evidence,
        max_retrieval_events=max_retrieval_events,
        max_retrieval_tokens=max_retrieval_tokens,
        observed_model_tokens=observed_model_tokens,
        observed_duration_s=observed_duration_s,
        simple_case_token_budget=simple_case_token_budget,
        simple_case_duration_budget_s=simple_case_duration_budget_s,
    )
    return AdaptiveRoutingEvidence(routing=route, budget_ledger=ledger)


def resolution_from_route(route: RoutingTrace) -> str:
    """Return the compatibility ``RunResult.resolved_by`` value."""

    if not isinstance(route, RoutingTrace):
        raise TypeError("route must be a RoutingTrace")
    if route.final_state is not RoutingState.SUCCEEDED:
        return ""
    if route.path is RoutingPath.FAST_PATH:
        return "initial_hard_gate"
    if route.path is not RoutingPath.ESCALATION_PATH:
        raise ValueError("a successful route must be fast or escalated")
    if route.retrieval_triggered and route.repair_attempted:
        return "retrieval_assisted_deterministic_offline_repair"
    if route.retrieval_triggered and route.infrastructure_retry_attempted:
        return "retrieval_assisted_infrastructure_retry"
    if route.retrieval_triggered:
        return "retrieval_assisted_parameter_normalization"
    if route.repair_attempted:
        return "deterministic_offline_repair"
    if route.infrastructure_retry_attempted:
        return "infrastructure_retry"
    if route.retrieval_attempted:
        return "initial_hard_gate_after_retrieval_no_match"
    if route.natural_language_interpretation_performed:
        return "initial_hard_gate_after_natural_language_interpretation"
    raise ValueError("successful escalation has no resolution evidence")


def _routing_check_snapshot(check: QualityCheckResult) -> RoutingCheckSnapshot:
    return RoutingCheckSnapshot(
        check_type=check.check_type,
        status=check.status,
        required=check.required,
        failure_kind=check.failure_kind,
        failure_category=check.failure_category,
        semantic_evidence_sha256=quality_check_semantic_sha256(check),
    )


def _make_routing_event(
    *,
    sequence: int,
    from_state: RoutingState,
    to_state: RoutingState,
    action: RoutingAction,
    reason_code: str,
    input_snapshot_sha256: str,
    artifact_bundle_sha256: str = "",
    validation_sha256: str = "",
    previous_event_sha256: str = "",
) -> RoutingEvent:
    draft = object.__new__(RoutingEvent)
    for name, value in (
        ("sequence", sequence),
        ("from_state", from_state),
        ("to_state", to_state),
        ("action", action),
        ("reason_code", reason_code),
        ("input_snapshot_sha256", input_snapshot_sha256),
        ("artifact_bundle_sha256", artifact_bundle_sha256),
        ("validation_sha256", validation_sha256),
        ("previous_event_sha256", previous_event_sha256),
        ("event_sha256", ""),
    ):
        object.__setattr__(draft, name, value)
    digest = routing_event_sha256(draft)
    return RoutingEvent(
        sequence=sequence,
        from_state=from_state,
        to_state=to_state,
        action=action,
        reason_code=reason_code,
        input_snapshot_sha256=input_snapshot_sha256,
        artifact_bundle_sha256=artifact_bundle_sha256,
        validation_sha256=validation_sha256,
        previous_event_sha256=previous_event_sha256,
        event_sha256=digest,
    )


def _append_routing_event(
    events: list[RoutingEvent],
    *,
    from_state: RoutingState,
    to_state: RoutingState,
    action: RoutingAction,
    reason_code: str,
    input_snapshot_sha256: str,
    artifact_bundle_sha256: str = "",
    validation_sha256: str = "",
) -> RoutingState:
    previous = events[-1].event_sha256 if events else ""
    events.append(
        _make_routing_event(
            sequence=len(events) + 1,
            from_state=from_state,
            to_state=to_state,
            action=action,
            reason_code=reason_code,
            input_snapshot_sha256=input_snapshot_sha256,
            artifact_bundle_sha256=artifact_bundle_sha256,
            validation_sha256=validation_sha256,
            previous_event_sha256=previous,
        )
    )
    return to_state


def _make_budget_event(
    *,
    sequence: int,
    dimension: BudgetDimension,
    amount: int,
    action: str,
    subject_sha256: str,
    before: int,
    after: int,
    previous_event_sha256: str,
) -> BudgetEvent:
    draft = object.__new__(BudgetEvent)
    for name, value in (
        ("sequence", sequence),
        ("dimension", dimension),
        ("amount", amount),
        ("action", action),
        ("subject_sha256", subject_sha256),
        ("before", before),
        ("after", after),
        ("previous_event_sha256", previous_event_sha256),
        ("event_sha256", ""),
    ):
        object.__setattr__(draft, name, value)
    digest = budget_event_sha256(draft)
    return BudgetEvent(
        sequence=sequence,
        dimension=dimension,
        amount=amount,
        action=action,
        subject_sha256=subject_sha256,
        before=before,
        after=after,
        previous_event_sha256=previous_event_sha256,
        event_sha256=digest,
    )


def _append_budget_event(
    events: list[BudgetEvent],
    usage: dict[BudgetDimension, int],
    *,
    dimension: BudgetDimension,
    amount: int,
    action: str,
    subject_sha256: str,
) -> None:
    before = usage[dimension]
    after = before + amount
    previous = events[-1].event_sha256 if events else ""
    event = _make_budget_event(
        sequence=len(events) + 1,
        dimension=dimension,
        amount=amount,
        action=action,
        subject_sha256=subject_sha256,
        before=before,
        after=after,
        previous_event_sha256=previous,
    )
    events.append(event)
    usage[dimension] = after


def _evidence_mapping(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    evidence = getattr(value, "evidence", None)
    if callable(evidence):
        rendered = evidence()
        if isinstance(rendered, Mapping):
            return dict(rendered)
    raise TypeError(f"{label} must be a mapping or expose evidence()")


def _retrieval_summary(
    evidence: Mapping[str, Any], input_snapshot_sha256: str
) -> tuple[bool, bool, str, int]:
    attempted = evidence.get("attempted", False)
    triggered = evidence.get("triggered", False)
    if type(attempted) is not bool or type(triggered) is not bool:
        raise TypeError("retrieval attempted/triggered evidence must be boolean")
    if triggered and not attempted:
        raise ValueError("triggered retrieval requires an attempted retrieval")
    raw_tokens = evidence.get("estimated_tokens", 0)
    if type(raw_tokens) is not int or raw_tokens < 0:
        raise ValueError("retrieval estimated_tokens must be a non-negative integer")
    if not attempted and (triggered or raw_tokens):
        raise ValueError("unattempted retrieval cannot claim context or tokens")
    reason = evidence.get("reason", "not_requested")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("retrieval evidence requires a non-empty reason")
    failed_snapshot = evidence.get("failed_snapshot_sha256", "")
    if failed_snapshot:
        _require_sha256(failed_snapshot, "retrieval failed_snapshot_sha256")
        if failed_snapshot != input_snapshot_sha256:
            raise ValueError("retrieval evidence is bound to a different input snapshot")
    return attempted, triggered, reason, raw_tokens


def _normalization_mode(
    input_kind: RoutingInputKind,
    normalization: Mapping[str, Any],
    retrieval_triggered: bool,
) -> str:
    if input_kind is RoutingInputKind.RAW_NATURAL_LANGUAGE:
        return "not_implemented"
    if input_kind is RoutingInputKind.PRENORMALIZED_PARAMETERS:
        return "pre_normalized_input"
    if retrieval_triggered:
        return "retrieval_assisted_structured_normalization"
    if normalization:
        return "local_structured_normalization"
    return "structured_normalization_evidence_not_recorded"


def _normalization_issue_codes(evidence: Mapping[str, Any]) -> tuple[str, ...]:
    raw_issues = evidence.get("issues", ())
    if raw_issues is None:
        return ()
    if isinstance(raw_issues, (str, bytes)) or not isinstance(raw_issues, Sequence):
        raise TypeError("normalization issues must be a sequence")
    codes: list[str] = []
    for item in raw_issues:
        code = item.get("code") if isinstance(item, Mapping) else getattr(item, "code", "")
        if not isinstance(code, str) or not code.strip():
            raise ValueError("normalization issues require non-empty codes")
        codes.append(code)
    return tuple(sorted(set(codes)))


def _escalation_reasons(
    normalization: Mapping[str, Any],
    retrieval: Mapping[str, Any],
    initial_validation: ValidationSnapshot,
    loop: RepairLoopResult,
) -> tuple[str, ...]:
    reasons: list[str] = list(_normalization_issue_codes(normalization))
    if retrieval.get("attempted") is True:
        reasons.append(f"retrieval:{retrieval.get('reason', 'attempted')}")
    primary = _select_primary_blocker(initial_validation)
    if primary is not None:
        reasons.append(_blocker_reason(primary))
    elif not initial_validation.decision.overall_success:
        reasons.append("quality_gate_configuration")
    if not loop.overall_success:
        reasons.append(f"repair_stop:{loop.stop_reason.value or 'unrepairable'}")
    return tuple(dict.fromkeys(reason for reason in reasons if reason))


def _select_primary_blocker(
    snapshot: ValidationSnapshot,
) -> QualityCheckResult | None:
    blockers = [
        item
        for item in snapshot.checks
        if item.required and item.status is not CheckStatus.PASS
    ]
    if not blockers:
        return None
    blockers.sort(
        key=lambda item: (
            0 if _is_infrastructure_or_terminal(item) else 1,
            _STATUS_PRIORITY[item.status],
            _CHECK_PRIORITY.get(item.check_type, 1000),
            item.check_type,
        )
    )
    return blockers[0]


def _is_infrastructure_or_terminal(check: QualityCheckResult) -> bool:
    category = (
        check.failure_category or str(check.evidence.get("reason", ""))
    ).strip().lower()
    return (
        check.failure_kind is FailureKind.INFRASTRUCTURE
        or check.failure_kind in {FailureKind.NOT_IMPLEMENTED, FailureKind.UNKNOWN}
        or check.check_type in _RERUN_ONLY_CHECKS
        or check.check_type in _TERMINAL_CHECKS
        or category in _RERUN_ONLY_CATEGORIES
        or category in _TERMINAL_CATEGORIES
    )


def _blocker_reason(check: QualityCheckResult | None) -> str:
    if check is None:
        return "quality_gate_configuration"
    category = (
        check.failure_category
        or str(check.evidence.get("reason", ""))
        or check.status.value.lower()
    )
    return f"required_check:{check.check_type}:{category}"


def _routing_action_for_repair(
    layer: RepairLayer, action: RepairAction
) -> RoutingAction:
    if (
        layer is RepairLayer.SYSML
        and action is RepairAction.REBUILD_SYSML
    ):
        return RoutingAction.REBUILD_CANONICAL_SYSML
    if (
        layer is RepairLayer.MODELICA
        and action is RepairAction.RESTORE_CERTIFIED_TEMPLATE
    ):
        return RoutingAction.RERENDER_CERTIFIED_MODELICA
    return RoutingAction.ENTER_BOUNDED_REPAIR


def _repair_baseline_bundle_sha256(repair: Any) -> str:
    raw = repair.original_evidence.get("artifact_bundle_before_sha256", "")
    if isinstance(raw, str) and _SHA256_RE.fullmatch(raw):
        return raw
    return ""


def _terminal_action(
    loop: RepairLoopResult, primary: QualityCheckResult | None
) -> RoutingAction:
    if loop.overall_success:
        return RoutingAction.RELEASE_AFTER_ESCALATION
    if loop.stop_reason is RepairStopReason.UPSTREAM_REQUIRED:
        return RoutingAction.STOP_UPSTREAM_REQUIRED
    if loop.stop_reason in {
        RepairStopReason.MAX_REPAIRS,
        RepairStopReason.INFRASTRUCTURE_RETRY_EXHAUSTED,
    }:
        return RoutingAction.STOP_BUDGET_EXHAUSTED
    if primary is not None and primary.check_type == "template_certification_anchor":
        return RoutingAction.STOP_RECERTIFICATION_REQUIRED
    if primary is not None and primary.failure_kind is FailureKind.NOT_IMPLEMENTED:
        return RoutingAction.STOP_NOT_IMPLEMENTED
    return RoutingAction.STOP_UNREPAIRABLE


def _simple_budget_status(
    path: RoutingPath,
    configured: int | float | None,
    observed: int | float,
) -> BudgetEvaluationStatus:
    if path is not RoutingPath.FAST_PATH:
        return BudgetEvaluationStatus.NOT_APPLICABLE
    if configured is None:
        return BudgetEvaluationStatus.NOT_FROZEN
    return (
        BudgetEvaluationStatus.PASS
        if observed <= configured
        else BudgetEvaluationStatus.FAIL
    )


def _require_sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


__all__ = [
    "AdaptiveRoutingEvidence",
    "CERTIFIED_HARD_GATE_PROFILE",
    "build_adaptive_routing_evidence",
    "build_budget_ledger",
    "build_preflight_failure_trace",
    "build_routing_trace",
    "is_complete_hard_gate_profile",
    "resolution_from_route",
]
