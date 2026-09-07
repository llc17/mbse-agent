"""Offline Stage-2 pipeline for repairing existing certified artifacts.

This module does not generate requirements or infer missing parameters.  It
accepts an explicit template identity and normalized parameter set, validates
the exact SysML/Modelica snapshot with the Stage-1 hard gate, and permits only
deterministic reconstruction of SysML or a slot-only certified Modelica render.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
import time
from typing import Iterable

from week9.src.generation import NaturalLanguageGenerationResult
from week9.src.hard_gate import SysMLParserAdapter, run_hard_gate
from week9.src.modelica_templates import (
    RenderedTemplate,
    TemplateInputError,
    get_template_spec,
    render_template,
)
from week9.src.modelica_tool import OpenModelicaTool
from week9.src.parameter_normalization import (
    ParameterNormalizationError,
    ParameterNormalizationResult,
    ParameterObservation,
    normalize_parameters,
    normalize_parameters_with_retrieval,
    parameter_observations_sha256,
)
from week9.src.quality_gate import decide
from week9.src.retrieval import RetrievalPolicy, RetrievalResult
from week9.src.repair_loop import (
    ArtifactBundle,
    RepairLoopConfig,
    RepairLoopResult,
    RepairProposal,
    RepairProvider,
    RepairProviderStop,
    RepairRequest,
    ValidationProvider,
    ValidationSnapshot,
    run_repair_loop,
)
from week9.src.schemas import (
    ArtifactRecord,
    CheckStatus,
    FailureKind,
    ModelConfiguration,
    NormalizedParameter,
    RepairAction,
    RepairLayer,
    RepairStopReason,
    ReproductionManifest,
    RoutingInputKind,
    RoutingTrace,
    RunResult,
    TokenUsage,
    sha256_text,
)
from week9.src.routing import (
    build_adaptive_routing_evidence,
    build_preflight_failure_trace,
    resolution_from_route,
)
from week9.src.traceability import TraceabilityInput, build_traceability


@dataclass(frozen=True, slots=True)
class CertifiedPipelineContext:
    template_id: str
    model_name: str
    parameters: tuple[NormalizedParameter, ...]
    workspace_root: Path


@dataclass(frozen=True, slots=True)
class _BoundRetrievalApplication:
    result: RetrievalResult
    parameter_contract_sha256: str
    normalization_evidence_sha256: str


class AdaptivePreparationError(ValueError):
    """A structured input that cannot reach artifact generation."""

    def __init__(
        self,
        message: str,
        *,
        routing: RoutingTrace,
        input_snapshot_sha256: str,
        upgrade_reason_codes: tuple[str, ...],
    ) -> None:
        self.routing = routing
        self.input_snapshot_sha256 = input_snapshot_sha256
        self.upgrade_reason_codes = upgrade_reason_codes
        super().__init__(message)


class CertifiedTemplateRepairPipeline:
    """Concrete validator/provider pair for one immutable template identity."""

    def __init__(
        self,
        *,
        template_id: str,
        model_name: str,
        parameters: Iterable[NormalizedParameter],
        workspace_root: str | Path,
        tool: OpenModelicaTool | None = None,
        sysml_parser: SysMLParserAdapter | None = None,
    ) -> None:
        parameter_tuple = tuple(parameters)
        canonical = render_template(
            template_id, parameter_tuple, model_name=model_name
        )
        self.context = CertifiedPipelineContext(
            template_id=template_id,
            model_name=model_name,
            parameters=parameter_tuple,
            workspace_root=Path(workspace_root).resolve(),
        )
        self.canonical_render = canonical
        self.canonical_sysml = render_canonical_sysml(canonical)
        self.tool = tool or OpenModelicaTool()
        self.sysml_parser = sysml_parser or SysMLParserAdapter()
        self.normalization_result: ParameterNormalizationResult | None = None
        self.retrieval_result: RetrievalResult | None = None
        self.generation_result: NaturalLanguageGenerationResult | None = None
        self._retrieval_application: _BoundRetrievalApplication | None = None
        self.routing_input_kind = RoutingInputKind.PRENORMALIZED_PARAMETERS
        self.input_snapshot_sha256 = _normalized_parameters_sha256(parameter_tuple)
        self.normalization_mode = "pre_normalized_input"
        self._initial_parameter_contract_sha256 = self.input_snapshot_sha256
        self._adaptive_entry_contract_sha256 = (
            self._current_adaptive_entry_contract_sha256()
        )

    @classmethod
    def from_observations(
        cls,
        *,
        template_id: str,
        model_name: str,
        observations: Iterable[ParameterObservation],
        workspace_root: str | Path,
        retrieval_result: RetrievalResult | None = None,
        tool: OpenModelicaTool | None = None,
        sysml_parser: SysMLParserAdapter | None = None,
    ) -> "CertifiedTemplateRepairPipeline":
        observation_tuple = tuple(observations)
        input_snapshot_sha256 = parameter_observations_sha256(
            template_id, observation_tuple
        )
        try:
            normalized = (
                normalize_parameters_with_retrieval(
                    template_id, observation_tuple, retrieval_result
                )
                if retrieval_result is not None and retrieval_result.triggered
                else normalize_parameters(template_id, observation_tuple)
            )
        except TemplateInputError as exc:
            routing = build_preflight_failure_trace(
                input_kind=RoutingInputKind.STRUCTURED_OBSERVATIONS,
                input_snapshot_sha256=input_snapshot_sha256,
                reason_codes=("no_certified_template",),
                normalization_mode="local_structured_normalization",
                retrieval_evidence=(
                    retrieval_result.evidence()
                    if retrieval_result is not None
                    else None
                ),
            )
            raise AdaptivePreparationError(
                str(exc),
                routing=routing,
                input_snapshot_sha256=input_snapshot_sha256,
                upgrade_reason_codes=("no_certified_template",),
            ) from exc
        if not normalized.ready:
            reason_codes = tuple(
                dict.fromkeys(item.code for item in normalized.issues)
            ) or ("parameter_normalization_not_ready",)
            routing = build_preflight_failure_trace(
                input_kind=RoutingInputKind.STRUCTURED_OBSERVATIONS,
                input_snapshot_sha256=input_snapshot_sha256,
                reason_codes=reason_codes,
                certified_template_id=template_id,
                normalization_mode=(
                    "retrieval_assisted_structured_normalization"
                    if retrieval_result is not None and retrieval_result.triggered
                    else "local_structured_normalization"
                ),
                retrieval_evidence=(
                    retrieval_result.evidence()
                    if retrieval_result is not None
                    else None
                ),
            )
            raise ParameterNormalizationError(
                normalized,
                routing=routing,
                input_snapshot_sha256=input_snapshot_sha256,
            )
        pipeline = cls(
            template_id=template_id,
            model_name=model_name,
            parameters=normalized.parameters,
            workspace_root=workspace_root,
            tool=tool,
            sysml_parser=sysml_parser,
        )
        pipeline.normalization_result = normalized
        pipeline.retrieval_result = retrieval_result
        pipeline.routing_input_kind = RoutingInputKind.STRUCTURED_OBSERVATIONS
        pipeline.input_snapshot_sha256 = input_snapshot_sha256
        pipeline.normalization_mode = (
            "retrieval_assisted_structured_normalization"
            if retrieval_result is not None and retrieval_result.triggered
            else "local_structured_normalization"
        )
        if retrieval_result is not None:
            pipeline._retrieval_application = _BoundRetrievalApplication(
                result=retrieval_result,
                parameter_contract_sha256=_normalized_parameters_sha256(
                    normalized.parameters
                ),
                normalization_evidence_sha256=_json_payload_sha256(
                    normalized.evidence()
                ),
            )
        pipeline._adaptive_entry_contract_sha256 = (
            pipeline._current_adaptive_entry_contract_sha256()
        )
        return pipeline

    @classmethod
    def from_natural_language_generation(
        cls,
        *,
        generation: NaturalLanguageGenerationResult,
        workspace_root: str | Path,
        tool: OpenModelicaTool | None = None,
        sysml_parser: SysMLParserAdapter | None = None,
    ) -> "CertifiedTemplateRepairPipeline":
        """Project one validated natural-language result into the certified path.

        The generation provider is upstream of the normalizer.  It may identify
        a certified template and explicit observations, but it cannot bypass
        normalization, template rendering, hard checks, routing, or traceability.
        """

        if not isinstance(generation, NaturalLanguageGenerationResult):
            raise TypeError("generation must be a NaturalLanguageGenerationResult")
        if not generation.ready or generation.traceability_input is None:
            raise ValueError("natural-language generation is not ready")
        pipeline = cls.from_observations(
            template_id=generation.template_id,
            model_name=generation.model_name,
            observations=generation.observations,
            workspace_root=workspace_root,
            tool=tool,
            sysml_parser=sysml_parser,
        )
        pipeline.generation_result = generation
        pipeline.routing_input_kind = RoutingInputKind.RAW_NATURAL_LANGUAGE
        pipeline.input_snapshot_sha256 = generation.requirement_sha256
        pipeline.normalization_mode = generation.normalization_mode
        pipeline._adaptive_entry_contract_sha256 = (
            pipeline._current_adaptive_entry_contract_sha256()
        )
        return pipeline

    def _current_adaptive_entry_contract_sha256(self) -> str:
        return _json_payload_sha256(
            {
                "input_kind": self.routing_input_kind.value,
                "input_snapshot_sha256": self.input_snapshot_sha256,
                "normalization_mode": self.normalization_mode,
                "normalization_evidence": (
                    self.normalization_result.evidence()
                    if self.normalization_result is not None
                    else {"ready": True, "issues": [], "mode": "pre_normalized_input"}
                ),
                "retrieval_evidence": (
                    self.retrieval_result.evidence()
                    if self.retrieval_result is not None
                    else {
                        "attempted": False,
                        "triggered": False,
                        "reason": "not_requested",
                        "estimated_tokens": 0,
                    }
                ),
                "natural_language_generation": (
                    self.generation_result.evidence()
                    if self.generation_result is not None
                    else None
                ),
            }
        )

    def validate(self, artifacts: ArtifactBundle) -> ValidationSnapshot:
        """Execute and gate the exact candidate; no cached success is reused."""

        candidate_render = replace(
            self.canonical_render,
            code=artifacts.modelica,
            rendered_sha256=sha256_text(artifacts.modelica),
        )
        execution = self.tool.execute_rendered(
            candidate_render,
            workspace_root=self.context.workspace_root,
        )
        gate = run_hard_gate(
            template_id=self.context.template_id,
            parameters=self.context.parameters,
            sysml_source=artifacts.sysml if artifacts.sysml.strip() else None,
            rendered=candidate_render,
            execution=execution,
            sysml_parser=self.sysml_parser,
        )
        checks = tuple(_annotate_owner(check) for check in gate.checks)
        return ValidationSnapshot(
            checks=checks,
            payload={
                "template_id": self.context.template_id,
                "model_name": self.context.model_name,
                "artifact_bundle_sha256": artifacts.sha256,
                "run_directory": execution.run_directory,
                "source_sha256": execution.source_sha256,
                "csv_sha256": execution.csv_sha256,
                "online_api_used": False,
            },
        )

    def repair(self, request: RepairRequest) -> RepairProposal:
        """Return only deterministic, layer-owned replacements."""

        if request.repair_layer is RepairLayer.REQUIREMENT:
            raise RepairProviderStop(
                RepairStopReason.UPSTREAM_REQUIRED,
                "normalized requirement parameters are incomplete; offline repair will not guess values",
            )
        if request.repair_layer is RepairLayer.SYSML:
            if request.action is not RepairAction.REBUILD_SYSML:
                raise RepairProviderStop(
                    RepairStopReason.UNREPAIRABLE,
                    "SysML repair action is outside the certified pipeline policy",
                )
            return RepairProposal(
                artifact_after=self.canonical_sysml,
                rationale="rebuild the scoped System topology from certified metadata",
                metadata={
                    "provider": "deterministic_offline",
                    "template_id": self.context.template_id,
                    "policy": "canonical_sysml_rebuild",
                },
            )
        if request.repair_layer is RepairLayer.MODELICA:
            if request.action is not RepairAction.RESTORE_CERTIFIED_TEMPLATE:
                raise RepairProviderStop(
                    RepairStopReason.UNREPAIRABLE,
                    "full-file Modelica patching is forbidden for certified templates",
                )
            return RepairProposal(
                artifact_after=self.canonical_render.code,
                rationale="re-render the immutable certified skeleton from declared slots",
                metadata={
                    "provider": "deterministic_offline",
                    "template_id": self.context.template_id,
                    "template_sha256": self.canonical_render.template_sha256,
                    "rendered_sha256": self.canonical_render.rendered_sha256,
                    "policy": "slot_only_no_unconstrained_full_rewrite",
                },
            )
        raise RepairProviderStop(
            RepairStopReason.UNREPAIRABLE,
            f"unsupported repair layer: {request.repair_layer}",
        )

    def run(
        self,
        *,
        case_id: str,
        run_id: str,
        initial_artifacts: ArtifactBundle,
        config: RepairLoopConfig | None = None,
        traceability_input: TraceabilityInput | None = None,
    ) -> RunResult:
        generation = self.generation_result
        effective_traceability_input = traceability_input
        if generation is not None:
            if initial_artifacts.requirement != generation.requirement_text:
                raise ValueError(
                    "initial requirement artifact differs from natural-language input"
                )
            if effective_traceability_input is None:
                effective_traceability_input = generation.traceability_input
            if effective_traceability_input != generation.traceability_input:
                raise ValueError(
                    "traceability input differs from natural-language generation evidence"
                )
        if (
            self._current_adaptive_entry_contract_sha256()
            != self._adaptive_entry_contract_sha256
        ):
            raise ValueError("adaptive entry contract changed after construction")
        if (
            _normalized_parameters_sha256(self.context.parameters)
            != self._initial_parameter_contract_sha256
        ):
            raise ValueError("certified parameter contract changed after construction")
        retrieval_application = self._retrieval_application
        if retrieval_application is not None:
            if self.retrieval_result is not retrieval_application.result:
                raise ValueError("bound retrieval result was replaced after normalization")
            if (
                _normalized_parameters_sha256(self.context.parameters)
                != retrieval_application.parameter_contract_sha256
            ):
                raise ValueError("normalized parameters changed after retrieval projection")
            if (
                self.normalization_result is None
                or self.normalization_result.template_id != self.context.template_id
                or _normalized_parameters_sha256(
                    self.normalization_result.parameters
                )
                != retrieval_application.parameter_contract_sha256
                or _json_payload_sha256(self.normalization_result.evidence())
                != retrieval_application.normalization_evidence_sha256
            ):
                raise ValueError(
                    "retrieval normalization evidence changed after projection"
                )
        selected_config = config or RepairLoopConfig()
        started = time.monotonic()
        initial_validation = self.validate(initial_artifacts)
        loop = run_repair_loop(
            initial_artifacts,
            self.validate,
            self.repair,
            selected_config,
            initial_validation=initial_validation,
        )
        generation_duration_s = generation.duration_s if generation is not None else 0.0
        duration_s = generation_duration_s + time.monotonic() - started
        return _build_run_result_from_loop(
            case_id=case_id,
            run_id=run_id,
            parameters=self.context.parameters,
            loop=loop,
            duration_s=duration_s,
            config=selected_config,
            model_configuration=ModelConfiguration(
                provider=(
                    generation.provider
                    if generation is not None
                    else "deterministic_offline"
                ),
                model="certified_template_adaptive_path",
                parameters={
                    "generator_model": (
                        generation.model if generation is not None else ""
                    ),
                    "temperature": (
                        generation.temperature if generation is not None else None
                    ),
                    "max_tokens": (
                        generation.max_tokens if generation is not None else 0
                    ),
                    "api_call_count": (
                        generation.api_call_count if generation is not None else 0
                    ),
                    "template_id": self.context.template_id,
                    "template_sha256": self.canonical_render.template_sha256,
                    "model_name": self.context.model_name,
                    "post_render_full_rewrite_allowed": False,
                    "traceability_required": effective_traceability_input is not None,
                    "natural_language_generation": (
                        generation.evidence() if generation is not None else None
                    ),
                    "natural_language_generation_sha256": (
                        generation.evidence_sha256
                        if generation is not None
                        else ""
                    ),
                    "parameter_normalization": (
                        self.normalization_result.evidence()
                        if self.normalization_result is not None
                        else {"mode": "pre_normalized_input"}
                    ),
                },
            ),
            tool_versions={
                "openmodelica": self.tool.version,
                "sysmlpy": self.sysml_parser.version,
            },
            retrieval_application=retrieval_application,
            adaptive_initial_artifacts=initial_artifacts,
            adaptive_initial_validation=initial_validation,
            adaptive_input_kind=self.routing_input_kind,
            adaptive_input_snapshot_sha256=self.input_snapshot_sha256,
            adaptive_normalization_mode=self.normalization_mode,
            adaptive_normalization_evidence=(
                self.normalization_result.evidence()
                if self.normalization_result is not None
                else {"ready": True, "issues": [], "mode": "pre_normalized_input"}
            ),
            canonical_sysml=self.canonical_sysml,
            canonical_render=self.canonical_render,
            traceability_input=effective_traceability_input,
            generation_result=generation,
            duration_started_at=started,
            duration_offset_s=generation_duration_s,
        )


def run_offline_repair_pipeline(
    *,
    case_id: str,
    run_id: str,
    initial_artifacts: ArtifactBundle,
    parameters: Iterable[NormalizedParameter],
    validator: ValidationProvider,
    repair_provider: RepairProvider,
    config: RepairLoopConfig | None = None,
    model_configuration: ModelConfiguration | None = None,
    tool_versions: dict[str, str] | None = None,
) -> RunResult:
    """Run the generic bounded loop without claiming retrieval attribution.

    Retrieval evidence is accepted only through
    :meth:`CertifiedTemplateRepairPipeline.from_observations`, where it is
    bound to the exact structured observation snapshot and normalized
    parameter contract.
    """

    return _run_offline_repair_pipeline(
        case_id=case_id,
        run_id=run_id,
        initial_artifacts=initial_artifacts,
        parameters=parameters,
        validator=validator,
        repair_provider=repair_provider,
        config=config,
        model_configuration=model_configuration,
        tool_versions=tool_versions,
        retrieval_application=None,
    )


def _run_offline_repair_pipeline(
    *,
    case_id: str,
    run_id: str,
    initial_artifacts: ArtifactBundle,
    parameters: Iterable[NormalizedParameter],
    validator: ValidationProvider,
    repair_provider: RepairProvider,
    config: RepairLoopConfig | None,
    model_configuration: ModelConfiguration | None,
    tool_versions: dict[str, str] | None,
    retrieval_application: _BoundRetrievalApplication | None,
) -> RunResult:
    """Internal runner carrying already-bound retrieval application evidence."""

    started = time.monotonic()
    selected_config = config or RepairLoopConfig()
    parameter_list = list(parameters)
    loop = run_repair_loop(
        initial_artifacts,
        validator,
        repair_provider,
        selected_config,
    )
    return _build_run_result_from_loop(
        case_id=case_id,
        run_id=run_id,
        parameters=parameter_list,
        loop=loop,
        duration_s=time.monotonic() - started,
        config=selected_config,
        model_configuration=model_configuration,
        tool_versions=tool_versions,
        retrieval_application=retrieval_application,
    )


def build_run_result_from_loop(
    *,
    case_id: str,
    run_id: str,
    parameters: Iterable[NormalizedParameter],
    loop: RepairLoopResult,
    duration_s: float,
    config: RepairLoopConfig,
    model_configuration: ModelConfiguration | None = None,
    tool_versions: dict[str, str] | None = None,
) -> RunResult:
    """Build a generic non-retrieval result from an already completed loop."""

    return _build_run_result_from_loop(
        case_id=case_id,
        run_id=run_id,
        parameters=parameters,
        loop=loop,
        duration_s=duration_s,
        config=config,
        model_configuration=model_configuration,
        tool_versions=tool_versions,
        retrieval_application=None,
    )


def _build_run_result_from_loop(
    *,
    case_id: str,
    run_id: str,
    parameters: Iterable[NormalizedParameter],
    loop: RepairLoopResult,
    duration_s: float,
    config: RepairLoopConfig,
    model_configuration: ModelConfiguration | None,
    tool_versions: dict[str, str] | None,
    retrieval_application: _BoundRetrievalApplication | None,
    adaptive_initial_artifacts: ArtifactBundle | None = None,
    adaptive_initial_validation: ValidationSnapshot | None = None,
    adaptive_input_kind: RoutingInputKind | None = None,
    adaptive_input_snapshot_sha256: str = "",
    adaptive_normalization_mode: str = "",
    adaptive_normalization_evidence: dict | None = None,
    canonical_sysml: str = "",
    canonical_render: RenderedTemplate | None = None,
    traceability_input: TraceabilityInput | None = None,
    generation_result: NaturalLanguageGenerationResult | None = None,
    duration_started_at: float | None = None,
    duration_offset_s: float = 0.0,
) -> RunResult:
    final = loop.final_artifacts
    parameter_list = list(parameters)
    final_checks = list(loop.final_validation.checks)
    traceability_report = None
    traceability_check = None
    if traceability_input is not None:
        if canonical_render is None or not canonical_sysml:
            raise ValueError(
                "traceability requires the exact certified canonical snapshots"
            )
        traceability_report, traceability_check = build_traceability(
            trace_input=traceability_input,
            final_requirement=final.requirement,
            final_sysml=final.sysml,
            final_modelica=final.modelica,
            canonical_sysml=canonical_sysml,
            canonical_render=canonical_render,
            parameters=parameter_list,
            final_base_checks=final_checks,
        )
        final_checks.append(traceability_check)
    decision = decide(final_checks)
    effective_stop_reason = loop.stop_reason
    if loop.overall_success and not decision.overall_success:
        effective_stop_reason = RepairStopReason.UPSTREAM_REQUIRED
    infrastructure_errors = [
        f"{check.check_type}:{check.failure_category or check.status.value}"
        for check in loop.final_validation.checks
        if check.required
        and check.status is not CheckStatus.PASS
        and check.failure_kind is FailureKind.INFRASTRUCTURE
    ]
    retrieval_result = (
        retrieval_application.result if retrieval_application is not None else None
    )
    retrieval_triggered = bool(retrieval_result is not None and retrieval_result.triggered)
    retrieval_evidence = (
        retrieval_result.evidence()
        if retrieval_result is not None
        else {
            "attempted": False,
            "triggered": False,
            "reason": "not_requested",
            "estimated_tokens": 0,
        }
    )
    adaptive_evidence = None
    if adaptive_input_kind is not None:
        if (
            adaptive_initial_artifacts is None
            or adaptive_initial_validation is None
            or not adaptive_input_snapshot_sha256
            or canonical_render is None
        ):
            raise ValueError("adaptive routing context is incomplete")
        retrieval_policy = (
            retrieval_result.policy
            if retrieval_result is not None
            else RetrievalPolicy()
        )
        adaptive_evidence = build_adaptive_routing_evidence(
            loop=loop,
            config=config,
            initial_artifacts=adaptive_initial_artifacts,
            initial_validation=adaptive_initial_validation,
            input_kind=adaptive_input_kind,
            input_snapshot_sha256=adaptive_input_snapshot_sha256,
            certified_template_id=canonical_render.template_id,
            normalization_mode=adaptive_normalization_mode,
            normalization_evidence=adaptive_normalization_evidence or {},
            retrieval_evidence=retrieval_evidence,
            post_gate_check=traceability_check,
            max_retrieval_events=retrieval_policy.max_retrieval_events_per_run,
            max_retrieval_tokens=retrieval_policy.max_total_retrieval_tokens,
            observed_model_tokens=(
                generation_result.total_tokens
                if generation_result is not None
                else 0
            ),
            observed_duration_s=duration_s,
            simple_case_token_budget=None,
            simple_case_duration_budget_s=None,
            online_api_used=(
                generation_result.online_api_used
                if generation_result is not None
                else False
            ),
        )
        if duration_started_at is not None:
            duration_s = duration_offset_s + time.monotonic() - duration_started_at
            adaptive_evidence = replace(
                adaptive_evidence,
                budget_ledger=replace(
                    adaptive_evidence.budget_ledger,
                    observed_duration_s=duration_s,
                ),
            )
        resolved_by = resolution_from_route(adaptive_evidence.routing)
    elif decision.overall_success and retrieval_triggered:
        resolved_by = (
            "retrieval_assisted_deterministic_offline_repair"
            if any(item.accepted for item in loop.repairs)
            else (
                "retrieval_assisted_infrastructure_retry"
                if loop.infrastructure_retries
                else "retrieval_assisted_parameter_normalization"
            )
        )
    elif decision.overall_success and any(item.accepted for item in loop.repairs):
        resolved_by = "deterministic_offline_repair"
    elif decision.overall_success and loop.infrastructure_retries:
        resolved_by = "infrastructure_retry"
    elif decision.overall_success:
        resolved_by = "initial_hard_gate"
    else:
        resolved_by = ""
    reproduction_budgets: dict[str, int | float] = {
        "max_repairs": config.max_repairs,
        "max_content_repairs": config.max_content_repairs,
        "max_upstream_redos": config.max_upstream_redos,
        "max_infrastructure_retries": config.max_infrastructure_retries,
    }
    if adaptive_evidence is not None:
        reproduction_budgets.update(
            {
                item.dimension.value: item.limit
                for item in adaptive_evidence.budget_ledger.limits
            }
        )

    result_counters = replace(
        loop.counters,
        generation_count=(1 if generation_result is not None else 0),
    )
    return RunResult(
        version="v8",
        case_id=case_id,
        run_id=run_id,
        requirement_text=final.requirement,
        parameters=parameter_list,
        artifacts={
            "requirement": ArtifactRecord(
                "requirement",
                content=final.requirement,
                sha256=sha256_text(final.requirement),
            ),
            "sysml": ArtifactRecord(
                "sysml",
                content=final.sysml,
                sha256=sha256_text(final.sysml),
                media_type="text/x-sysml",
            ),
            "modelica": ArtifactRecord(
                "modelica",
                content=final.modelica,
                sha256=sha256_text(final.modelica),
                media_type="text/x-modelica",
            ),
        },
        checks=final_checks,
        repairs=list(loop.repairs),
        infrastructure_retries=list(loop.infrastructure_retries),
        repair_stop_reason=effective_stop_reason,
        overall_success=decision.overall_success,
        primary_failure_category=decision.primary_failure_category,
        primary_failure_evidence=dict(decision.primary_failure_evidence),
        resolved_by=resolved_by,
        counters=result_counters,
        token_usage=TokenUsage(
            prompt_tokens=(
                generation_result.prompt_tokens
                if generation_result is not None
                else 0
            ),
            completion_tokens=(
                generation_result.completion_tokens
                if generation_result is not None
                else 0
            ),
            total_tokens=(
                generation_result.total_tokens
                if generation_result is not None
                else 0
            ),
            retrieval_tokens=(
                retrieval_result.estimated_tokens if retrieval_triggered else 0
            )
        ),
        duration_s=duration_s,
        model_configuration=model_configuration
        or ModelConfiguration(
            provider="deterministic_offline", model="bounded_repair_loop"
        ),
        reproduction=ReproductionManifest(
            prompt_sha256=(
                generation_result.prompt_sha256
                or generation_result.evidence().get("grammar_sha256", "")
                if generation_result is not None
                else ""
            ),
            corpus_sha256=(
                retrieval_result.corpus_sha256 if retrieval_triggered else ""
            ),
            tool_versions=dict(tool_versions or {}),
            budgets=reproduction_budgets,
        ),
        infrastructure_errors=infrastructure_errors,
        retrieval_triggered=retrieval_triggered,
        retrieval_fragment_ids=(
            list(retrieval_result.fragment_ids) if retrieval_triggered else []
        ),
        routing=(
            adaptive_evidence.routing
            if adaptive_evidence is not None
            else RoutingTrace()
        ),
        budget_ledger=(
            adaptive_evidence.budget_ledger
            if adaptive_evidence is not None
            else None
        ),
        traceability=traceability_report,
        execution_contract=(
            "certified_template_adaptive_v1"
            if adaptive_evidence is not None
            else "bounded_repair_compatibility_v1"
        ),
        raw_metrics={
            "online_api_used": (
                generation_result.online_api_used
                if generation_result is not None
                else False
            ),
            "natural_language_interpretation_performed": (
                generation_result is not None
            ),
            "natural_language_generation": (
                generation_result.evidence()
                if generation_result is not None
                else None
            ),
            "natural_language_generation_sha256": (
                generation_result.evidence_sha256
                if generation_result is not None
                else ""
            ),
            "repair_stop_reason": effective_stop_reason.value,
            "validation_history_count": len(loop.validation_history),
            "artifact_bundle_sha256": final.sha256,
            "previous_actions": list(loop.previous_actions),
            "repair_provider_error": loop.provider_error,
            "retrieval_decision": retrieval_evidence,
            "traceability_input_sha256": (
                traceability_input.sha256 if traceability_input is not None else ""
            ),
        },
    )


def render_canonical_sysml(rendered: RenderedTemplate) -> str:
    """Render the exact Stage-1 scoped topology using canonical SI values."""

    spec = get_template_spec(rendered.template_id)
    attributes = " ".join(
        f"attribute {item.name} : Real = {_real_literal(item.value)};"
        for item in rendered.parameters
    )
    parts = " ".join(f"part {name};" for name in spec.sysml_components)
    connections = " ".join(
        f"connect {left} to {right};" for left, right in spec.sysml_connections
    )
    return (
        f"package V8_{rendered.model_name} {{ part def System {{ "
        f"{attributes} {parts} {connections} }} part system : System; }}"
    )


def _real_literal(value: float) -> str:
    text = format(float(value), ".17g")
    return text if any(character in text for character in ".eE") else f"{text}.0"


def _annotate_owner(check):
    if check.status is CheckStatus.PASS:
        return check
    owner = None
    if check.check_type in {"sysml_syntax", "cross_model_consistency"}:
        owner = RepairLayer.SYSML
    elif check.check_type in {
        "certified_template_integrity",
        "modelica_compile",
        "simulation",
        "physical_behavior",
    }:
        owner = RepairLayer.MODELICA
    elif check.check_type in {
        "requirement_normalization",
        "parameter_unit_consistency",
    }:
        owner = RepairLayer.REQUIREMENT
    if owner is None:
        return check
    return replace(check, evidence={**check.evidence, "owner_layer": owner.value})


def _normalized_parameters_sha256(
    parameters: Iterable[NormalizedParameter],
) -> str:
    payload = []
    for item in parameters:
        serialized = asdict(item)
        serialized["status"] = item.status.value
        payload.append(serialized)
    return _json_payload_sha256(payload)


def _json_payload_sha256(payload) -> str:
    return sha256_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


__all__ = [
    "CertifiedPipelineContext",
    "CertifiedTemplateRepairPipeline",
    "build_run_result_from_loop",
    "render_canonical_sysml",
    "run_offline_repair_pipeline",
]
