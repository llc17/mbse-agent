from __future__ import annotations

from dataclasses import asdict, replace
import json
import unittest

from week9.experiments.cases import get_case
from week9.src.generation import DeterministicNaturalLanguageProvider
from week9.src.hard_gate import REQUIRED_HARD_CHECK_TYPES
from week9.src.modelica_templates import representative_parameters
from week9.src.pipeline import (
    AdaptivePreparationError,
    CertifiedTemplateRepairPipeline,
)
from week9.src.parameter_normalization import (
    ParameterNormalizationError,
    ParameterObservation,
)
from week9.src.repair_loop import ArtifactBundle, ValidationSnapshot
from week9.src.routing import CERTIFIED_HARD_GATE_PROFILE
from week9.src.schemas import (
    BudgetDimension,
    BudgetEvent,
    BudgetEvaluationStatus,
    CheckStatus,
    FailureKind,
    QualityCheckResult,
    RepairStopReason,
    RoutingPath,
    RoutingInputKind,
    RoutingState,
    RoutingTrace,
    RunResult,
    TraceLinkStatus,
    TraceabilityReport,
    budget_event_sha256,
    sha256_text,
    traceability_report_sha256,
)
from week9.src.traceability import (
    EXPLICIT_BINDING_BASIS,
    TEMPLATE_TRACE_REGISTRY,
    RequirementItem,
    RequirementTargetBinding,
    TraceabilityInput,
)


class _VersionOnlyTool:
    version = "synthetic-no-execution"


class _VersionOnlyParser:
    version = "synthetic-no-parse"


def _passing_check(
    check_type: str, pipeline: CertifiedTemplateRepairPipeline
) -> QualityCheckResult:
    evidence = {"fixture": "stage4-pipeline", "check": check_type}
    if check_type == "certified_template_integrity":
        evidence.update(
            {
                "template_id": pipeline.context.template_id,
                "template_sha256": pipeline.canonical_render.template_sha256,
                "rendered_sha256": pipeline.canonical_render.rendered_sha256,
            }
        )
    elif check_type == "template_certification_anchor":
        evidence["template_sha256"] = pipeline.canonical_render.template_sha256
    return QualityCheckResult(
        check_type=check_type,
        status=CheckStatus.PASS,
        required=True,
        evidence=evidence,
        tool_name="synthetic-stage4-validator",
        tool_version="1",
    )


def _failing_check(check_type: str, *, owner: str) -> QualityCheckResult:
    category = f"injected_{check_type}_failure"
    return QualityCheckResult(
        check_type=check_type,
        status=CheckStatus.FAIL,
        required=True,
        issues=[category],
        evidence={
            "fixture": "stage4-pipeline",
            "reason": category,
            "owner_layer": owner,
        },
        tool_name="synthetic-stage4-validator",
        tool_version="1",
        failure_kind=FailureKind.CONTENT,
        failure_category=category,
    )


def _checks(
    pipeline: CertifiedTemplateRepairPipeline, *, failing: str = ""
) -> tuple[QualityCheckResult, ...]:
    return tuple(
        _failing_check(name, owner="modelica")
        if name == failing
        else _passing_check(name, pipeline)
        for name in CERTIFIED_HARD_GATE_PROFILE
    )


def _trace_input(template_id: str) -> tuple[str, TraceabilityInput]:
    registry = TEMPLATE_TRACE_REGISTRY[template_id]
    requirement_parts: list[str] = []
    requirements: list[RequirementItem] = []
    bindings: list[RequirementTargetBinding] = []
    offset = 0
    for index, target_id in enumerate(registry.expected_target_ids, start=1):
        text = f"Requirement for {target_id}"
        requirement_id = f"REQ-{index:03d}"
        requirements.append(
            RequirementItem(
                requirement_id=requirement_id,
                text=text,
                source_start=offset,
                source_end=offset + len(text),
                critical=True,
            )
        )
        bindings.append(
            RequirementTargetBinding(
                requirement_id=requirement_id,
                target_id=target_id,
                basis=EXPLICIT_BINDING_BASIS,
            )
        )
        requirement_parts.append(text)
        offset += len(text)
        if index != len(registry.expected_target_ids):
            requirement_parts.append("\n")
            offset += 1
    requirement = "".join(requirement_parts)
    return requirement, TraceabilityInput(
        requirement_artifact_sha256=sha256_text(requirement),
        requirements=tuple(requirements),
        bindings=tuple(bindings),
    )


def _pipeline() -> CertifiedTemplateRepairPipeline:
    return CertifiedTemplateRepairPipeline(
        template_id="rc_filter",
        model_name="Stage4IntegrationRC",
        parameters=representative_parameters("rc_filter"),
        workspace_root="week9/results/stage4-synthetic-no-execution",
        tool=_VersionOnlyTool(),
        sysml_parser=_VersionOnlyParser(),
    )


def _canonical_artifacts(
    pipeline: CertifiedTemplateRepairPipeline, requirement: str
) -> ArtifactBundle:
    return ArtifactBundle(
        requirement=requirement,
        sysml=pipeline.canonical_sysml,
        modelica=pipeline.canonical_render.code,
    )


def _fast_result(*, case_id: str = "stage4-fast-fixture") -> RunResult:
    pipeline = _pipeline()
    requirement, trace_input = _trace_input("rc_filter")

    def validate(artifacts: ArtifactBundle) -> ValidationSnapshot:
        return ValidationSnapshot(
            checks=_checks(pipeline),
            payload={"artifact_bundle_sha256": artifacts.sha256},
        )

    pipeline.validate = validate  # type: ignore[method-assign]
    return pipeline.run(
        case_id=case_id,
        run_id="synthetic-1",
        initial_artifacts=_canonical_artifacts(pipeline, requirement),
        traceability_input=trace_input,
    )


def _rebind_trace_report(payload: dict) -> None:
    report = TraceabilityReport.from_dict(payload["traceability"])
    trace_check = next(
        item
        for item in payload["checks"]
        if item["check_type"] == "traceability_integrity"
    )
    trace_check["evidence"]["report_sha256"] = traceability_report_sha256(report)


def _trace_blocked_result() -> RunResult:
    pipeline = _pipeline()
    requirement, trace_input = _trace_input("rc_filter")
    requirements = list(trace_input.requirements)
    requirements[0] = replace(
        requirements[0], not_implemented_reason="explicit upstream gap"
    )
    blocked_trace = TraceabilityInput(
        requirement_artifact_sha256=trace_input.requirement_artifact_sha256,
        requirements=tuple(requirements),
        bindings=trace_input.bindings,
    )

    def validate(artifacts: ArtifactBundle) -> ValidationSnapshot:
        return ValidationSnapshot(
            checks=_checks(pipeline),
            payload={"artifact_bundle_sha256": artifacts.sha256},
        )

    pipeline.validate = validate  # type: ignore[method-assign]
    return pipeline.run(
        case_id="stage4-blocked-fixture",
        run_id="synthetic-1",
        initial_artifacts=_canonical_artifacts(pipeline, requirement),
        traceability_input=blocked_trace,
    )


class Stage4CertifiedPipelineIntegrationTests(unittest.TestCase):
    def test_local_natural_language_generation_can_use_auditable_fast_path(self) -> None:
        generation = DeterministicNaturalLanguageProvider().generate(
            get_case("rc_lowpass").raw_input
        )
        pipeline = CertifiedTemplateRepairPipeline.from_natural_language_generation(
            generation=generation,
            workspace_root="week9/results/stage4-synthetic-no-execution",
            tool=_VersionOnlyTool(),
            sysml_parser=_VersionOnlyParser(),
        )

        def validate(artifacts: ArtifactBundle) -> ValidationSnapshot:
            return ValidationSnapshot(
                checks=_checks(pipeline),
                payload={"artifact_bundle_sha256": artifacts.sha256},
            )

        pipeline.validate = validate  # type: ignore[method-assign]
        result = pipeline.run(
            case_id="raw-natural-language-rc",
            run_id="synthetic-natural-language",
            initial_artifacts=_canonical_artifacts(
                pipeline, generation.requirement_text
            ),
        )

        self.assertTrue(result.overall_success)
        self.assertEqual(result.routing.input_kind, RoutingInputKind.RAW_NATURAL_LANGUAGE)
        self.assertEqual(result.routing.path, RoutingPath.FAST_PATH)
        self.assertTrue(result.routing.natural_language_interpretation_performed)
        self.assertEqual(result.counters.generation_count, 1)
        self.assertEqual(result.token_usage.total_tokens, 0)
        self.assertEqual(
            result.resolved_by,
            "initial_hard_gate",
        )
        self.assertTrue(result.traceability.integrity_passed)

        forged = result.to_dict()
        forged["raw_metrics"]["natural_language_generation"]["template_id"] = (
            "rlc_filter"
        )
        with self.assertRaisesRegex(ValueError, "generation evidence"):
            RunResult.from_dict(forged)

    def test_natural_language_requirement_or_trace_cannot_be_swapped(self) -> None:
        generation = DeterministicNaturalLanguageProvider().generate(
            get_case("rc_lowpass").raw_input
        )
        pipeline = CertifiedTemplateRepairPipeline.from_natural_language_generation(
            generation=generation,
            workspace_root="week9/results/stage4-synthetic-no-execution",
            tool=_VersionOnlyTool(),
            sysml_parser=_VersionOnlyParser(),
        )
        with self.assertRaisesRegex(ValueError, "initial requirement"):
            pipeline.run(
                case_id="raw-natural-language-tamper",
                run_id="synthetic-natural-language-tamper",
                initial_artifacts=_canonical_artifacts(pipeline, "different requirement"),
            )

    def test_router_profile_is_the_public_hard_gate_profile(self) -> None:
        self.assertEqual(CERTIFIED_HARD_GATE_PROFILE, REQUIRED_HARD_CHECK_TYPES)

    def test_complete_initial_gate_uses_one_validation_fast_path(self) -> None:
        pipeline = _pipeline()
        requirement, trace_input = _trace_input("rc_filter")
        initial = _canonical_artifacts(pipeline, requirement)

        def validate(artifacts: ArtifactBundle) -> ValidationSnapshot:
            return ValidationSnapshot(
                checks=_checks(pipeline),
                payload={"artifact_bundle_sha256": artifacts.sha256},
            )

        pipeline.validate = validate  # type: ignore[method-assign]
        result = pipeline.run(
            case_id="stage4-fast",
            run_id="synthetic-1",
            initial_artifacts=initial,
            traceability_input=trace_input,
        )

        self.assertTrue(result.overall_success)
        self.assertEqual(result.routing.path, RoutingPath.FAST_PATH)
        self.assertEqual(result.routing.final_state, RoutingState.SUCCEEDED)
        self.assertEqual(result.counters.validation_count, 1)
        self.assertEqual(result.resolved_by, "initial_hard_gate")
        self.assertEqual(len(result.checks), 11)
        self.assertTrue(result.traceability and result.traceability.integrity_passed)
        self.assertEqual(
            result.budget_ledger.token_budget_status,
            BudgetEvaluationStatus.NOT_FROZEN,
        )
        self.assertEqual(
            result.budget_ledger.duration_budget_status,
            BudgetEvaluationStatus.NOT_FROZEN,
        )
        self.assertEqual(
            result.budget_ledger.usage(BudgetDimension.VALIDATION), 1
        )
        self.assertEqual(RunResult.from_dict(result.to_dict()).to_dict(), result.to_dict())

    def test_structured_observations_are_hashed_and_can_use_fast_path(self) -> None:
        parameters = representative_parameters("rc_filter")
        observations = tuple(
            ParameterObservation(
                name=item.canonical_name,
                value=item.value,
                unit=item.unit,
                source_text=f"{item.canonical_name}={item.value} {item.unit}",
            )
            for item in parameters
        )
        pipeline = CertifiedTemplateRepairPipeline.from_observations(
            template_id="rc_filter",
            model_name="Stage4StructuredRC",
            observations=observations,
            workspace_root="week9/results/stage4-synthetic-no-execution",
            tool=_VersionOnlyTool(),
            sysml_parser=_VersionOnlyParser(),
        )
        requirement, trace_input = _trace_input("rc_filter")

        def validate(artifacts: ArtifactBundle) -> ValidationSnapshot:
            return ValidationSnapshot(
                checks=_checks(pipeline),
                payload={"artifact_bundle_sha256": artifacts.sha256},
            )

        pipeline.validate = validate  # type: ignore[method-assign]
        result = pipeline.run(
            case_id="stage4-structured-fast",
            run_id="synthetic-1",
            initial_artifacts=_canonical_artifacts(pipeline, requirement),
            traceability_input=trace_input,
        )

        self.assertEqual(result.routing.input_kind, RoutingInputKind.STRUCTURED_OBSERVATIONS)
        self.assertEqual(result.routing.path, RoutingPath.FAST_PATH)
        self.assertTrue(result.model_configuration.parameters["parameter_normalization"]["ready"])

    def test_unresolved_structured_input_carries_preflight_escalation(self) -> None:
        with self.assertRaises(ParameterNormalizationError) as caught:
            CertifiedTemplateRepairPipeline.from_observations(
                template_id="rc_filter",
                model_name="Stage4MissingParameters",
                observations=(
                    ParameterObservation(
                        name="R",
                        value=1000.0,
                        unit="Ohm",
                        source_text="R=1000 Ohm",
                    ),
                ),
                workspace_root="week9/results/stage4-synthetic-no-execution",
                tool=_VersionOnlyTool(),
                sysml_parser=_VersionOnlyParser(),
            )

        error = caught.exception
        self.assertTrue(error.input_snapshot_sha256)
        self.assertIn("missing_parameter", error.upgrade_reason_codes)
        self.assertEqual(error.routing.path, RoutingPath.ESCALATION_PATH)
        self.assertEqual(error.routing.final_state, RoutingState.FAILED)
        self.assertFalse(error.routing.initial_checks)
        self.assertTrue(error.evidence()["upgrade_required"])

    def test_missing_certified_template_has_explicit_preflight_route(self) -> None:
        with self.assertRaises(AdaptivePreparationError) as caught:
            CertifiedTemplateRepairPipeline.from_observations(
                template_id="not_a_certified_template",
                model_name="Stage4UnknownTemplate",
                observations=(
                    ParameterObservation(
                        name="x",
                        value=1.0,
                        unit="1",
                        source_text="x=1",
                    ),
                ),
                workspace_root="week9/results/stage4-synthetic-no-execution",
                tool=_VersionOnlyTool(),
                sysml_parser=_VersionOnlyParser(),
            )

        error = caught.exception
        self.assertEqual(error.upgrade_reason_codes, ("no_certified_template",))
        self.assertEqual(error.routing.path, RoutingPath.ESCALATION_PATH)
        self.assertIn("no_certified_template", error.routing.escalation_reasons)
        self.assertEqual(
            error.routing.events[-1].action.value,
            "stop_recertification_required",
        )

    def test_adaptive_entry_snapshot_cannot_change_before_run(self) -> None:
        pipeline = _pipeline()
        requirement, trace_input = _trace_input("rc_filter")
        pipeline.input_snapshot_sha256 = "f" * 64

        with self.assertRaisesRegex(ValueError, "adaptive entry contract changed"):
            pipeline.run(
                case_id="stage4-mutated-entry",
                run_id="synthetic-1",
                initial_artifacts=_canonical_artifacts(pipeline, requirement),
                traceability_input=trace_input,
            )

    def test_modelica_failure_uses_bounded_certified_rerender_path(self) -> None:
        pipeline = _pipeline()
        requirement, trace_input = _trace_input("rc_filter")
        initial = replace(
            _canonical_artifacts(pipeline, requirement),
            modelica="model Broken\nend Broken;",
        )

        def validate(artifacts: ArtifactBundle) -> ValidationSnapshot:
            failing = (
                ""
                if artifacts.modelica == pipeline.canonical_render.code
                else "modelica_compile"
            )
            return ValidationSnapshot(
                checks=_checks(pipeline, failing=failing),
                payload={"artifact_bundle_sha256": artifacts.sha256},
            )

        pipeline.validate = validate  # type: ignore[method-assign]
        result = pipeline.run(
            case_id="stage4-escalation",
            run_id="synthetic-1",
            initial_artifacts=initial,
            traceability_input=trace_input,
        )

        self.assertTrue(result.overall_success)
        self.assertEqual(result.routing.path, RoutingPath.ESCALATION_PATH)
        self.assertEqual(result.routing.final_state, RoutingState.SUCCEEDED)
        self.assertEqual(result.resolved_by, "deterministic_offline_repair")
        self.assertEqual(result.counters.validation_count, 2)
        self.assertEqual(len(result.repairs), 1)
        self.assertTrue(result.repairs[0].accepted)
        self.assertEqual(
            result.budget_ledger.usage(BudgetDimension.REPAIR_ATTEMPT), 1
        )
        self.assertEqual(
            result.budget_ledger.usage(BudgetDimension.MODELICA_CHANGED_REPAIR),
            1,
        )

    def test_critical_not_implemented_trace_blocks_release(self) -> None:
        pipeline = _pipeline()
        requirement, trace_input = _trace_input("rc_filter")
        requirements = list(trace_input.requirements)
        requirements[0] = replace(
            requirements[0],
            not_implemented_reason="upstream semantic interpretation unavailable",
        )
        blocked_trace = TraceabilityInput(
            requirement_artifact_sha256=trace_input.requirement_artifact_sha256,
            requirements=tuple(requirements),
            bindings=trace_input.bindings,
        )
        initial = _canonical_artifacts(pipeline, requirement)

        def validate(artifacts: ArtifactBundle) -> ValidationSnapshot:
            return ValidationSnapshot(
                checks=_checks(pipeline),
                payload={"artifact_bundle_sha256": artifacts.sha256},
            )

        pipeline.validate = validate  # type: ignore[method-assign]
        result = pipeline.run(
            case_id="stage4-trace-blocked",
            run_id="synthetic-1",
            initial_artifacts=initial,
            traceability_input=blocked_trace,
        )

        self.assertFalse(result.overall_success)
        self.assertEqual(result.routing.path, RoutingPath.ESCALATION_PATH)
        self.assertEqual(result.routing.final_state, RoutingState.FAILED)
        self.assertEqual(result.repair_stop_reason, RepairStopReason.UPSTREAM_REQUIRED)
        self.assertEqual(result.resolved_by, "")
        self.assertFalse(result.traceability.integrity_passed)
        self.assertEqual(
            result.traceability.links[0].status, TraceLinkStatus.NOT_IMPLEMENTED
        )
        trace_check = next(
            item
            for item in result.checks
            if item.check_type == "traceability_integrity"
        )
        self.assertEqual(trace_check.status, CheckStatus.FAIL)

    def test_serialized_trace_cannot_claim_a_different_parameter_hash(self) -> None:
        pipeline = _pipeline()
        requirement, trace_input = _trace_input("rc_filter")

        def validate(artifacts: ArtifactBundle) -> ValidationSnapshot:
            return ValidationSnapshot(
                checks=_checks(pipeline),
                payload={"artifact_bundle_sha256": artifacts.sha256},
            )

        pipeline.validate = validate  # type: ignore[method-assign]
        result = pipeline.run(
            case_id="stage4-hash-binding",
            run_id="synthetic-1",
            initial_artifacts=_canonical_artifacts(pipeline, requirement),
            traceability_input=trace_input,
        )
        payload = result.to_dict()
        parameter_link = next(
            item
            for item in payload["traceability"]["links"]
            if item["normalized_parameter_sha256"]
        )
        parameter_link["normalized_parameter_sha256"] = "0" * 64
        forged_report_hash = traceability_report_sha256(
            TraceabilityReport.from_dict(payload["traceability"])
        )
        trace_check = next(
            item
            for item in payload["checks"]
            if item["check_type"] == "traceability_integrity"
        )
        trace_check["evidence"]["report_sha256"] = forged_report_hash

        with self.assertRaisesRegex(
            ValueError, "not bound to its final normalized parameter"
        ):
            RunResult.from_dict(payload)

    def test_recomputed_nonexistent_selector_is_rejected(self) -> None:
        payload = _fast_result(case_id="stage4-selector-forgery").to_dict()
        link = next(
            item
            for item in payload["traceability"]["links"]
            if item["sysml_elements"]
        )
        link["sysml_elements"][0]["exact_selector"] = "not present in SysML"
        _rebind_trace_report(payload)

        with self.assertRaisesRegex(ValueError, "selector cardinality"):
            RunResult.from_dict(payload)

    def test_adaptive_budget_limits_cannot_be_removed(self) -> None:
        payload = _fast_result(case_id="stage4-budget-limit-forgery").to_dict()
        payload["budget_ledger"]["limits"] = []

        with self.assertRaisesRegex(ValueError, "budget limits"):
            RunResult.from_dict(payload)

    def test_budget_validation_subject_binds_initial_snapshot(self) -> None:
        payload = _fast_result(case_id="stage4-budget-subject-forgery").to_dict()
        event = next(
            item
            for item in payload["budget_ledger"]["events"]
            if item["dimension"] == BudgetDimension.VALIDATION.value
        )
        event["subject_sha256"] = "0" * 64
        draft = object.__new__(BudgetEvent)
        for name, value in event.items():
            object.__setattr__(
                draft,
                name,
                BudgetDimension(value) if name == "dimension" else value,
            )
        object.__setattr__(draft, "event_sha256", "")
        event["event_sha256"] = budget_event_sha256(draft)

        with self.assertRaisesRegex(ValueError, "initial routing evidence"):
            RunResult.from_dict(payload)

    def test_adaptive_contract_cannot_be_downgraded_to_strip_evidence(self) -> None:
        payload = _fast_result(case_id="stage4-evidence-strip").to_dict()
        payload["model_configuration"]["model"] = "bounded_repair_loop"
        payload["routing"] = json.loads(json.dumps(asdict(RoutingTrace())))
        payload["budget_ledger"] = None
        payload["traceability"] = None
        payload["checks"] = [
            item
            for item in payload["checks"]
            if item["check_type"] != "traceability_integrity"
        ]

        with self.assertRaisesRegex(ValueError, "adaptive execution contract"):
            RunResult.from_dict(payload)

    def test_failed_route_cannot_claim_initial_gate_resolution(self) -> None:
        payload = _trace_blocked_result().to_dict()
        payload["resolved_by"] = "initial_hard_gate"

        with self.assertRaisesRegex(ValueError, "cannot claim a resolution"):
            RunResult.from_dict(payload)

    def test_nested_artifact_mutation_cannot_be_serialized(self) -> None:
        result = _fast_result(case_id="stage4-artifact-mutation")
        result.artifacts["sysml"].content += " "

        with self.assertRaisesRegex(ValueError, "content changed"):
            result.to_dict()


if __name__ == "__main__":
    unittest.main()
