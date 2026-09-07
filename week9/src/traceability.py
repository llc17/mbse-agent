"""Deterministic, template-scoped traceability for V8 Stage 4.

The module deliberately does not infer trace links from similar names.  A
caller supplies explicit requirement-to-target bindings, and every accepted
target is resolved through an audited registry for one certified template.
Model evidence is exact and is always bound to the final artifact hashes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
import re
from types import MappingProxyType
from typing import Iterable, Mapping

from week9.src.modelica_templates import (
    RenderedTemplate,
    TemplateInputError,
    canonicalize_parameters,
    get_template_spec,
    verify_rendered_template,
)
from week9.src.schemas import (
    CheckStatus,
    FailureKind,
    NormalizedParameter,
    ParameterStatus,
    QualityCheckResult,
    TraceCheckReference,
    TraceElementReference,
    TraceLayer,
    TraceLink,
    TraceLinkStatus,
    TraceabilityReport,
    normalized_parameter_semantic_sha256,
    quality_check_semantic_sha256,
    sha256_text,
    traceability_report_sha256,
)


EXPLICIT_BINDING_BASIS = "explicit_structured_input"
PARAMETER_TARGET_PREFIX = "parameter:"
TOPOLOGY_TARGET_ID = "constraint:topology"
TRACEABILITY_SCOPE = "certified_template_explicit_structural_correspondence_only"
TRACEABILITY_CHECK_TYPE = "traceability_integrity"
TRACEABILITY_TOOL_NAME = "V8 deterministic traceability validator"
TRACEABILITY_TOOL_VERSION = "stage4"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXPLICIT_ID = re.compile(r"^\S+$")

_PARAMETER_CHECKS = (
    "requirement_normalization",
    "parameter_unit_consistency",
    "cross_model_consistency",
    "certified_template_integrity",
    "modelica_compile",
)
_TOPOLOGY_CHECKS = (
    "sysml_syntax",
    "cross_model_consistency",
    "certified_template_integrity",
    "modelica_compile",
)


@dataclass(frozen=True, slots=True)
class RequirementItem:
    """One explicitly identified slice of the final requirement artifact."""

    requirement_id: str
    text: str
    source_start: int
    source_end: int
    critical: bool = True
    not_implemented_reason: str = ""

    def __post_init__(self) -> None:
        if (
            not isinstance(self.requirement_id, str)
            or not _EXPLICIT_ID.fullmatch(self.requirement_id)
        ):
            raise ValueError("requirement_id must be an explicit whitespace-free id")
        if not isinstance(self.text, str) or not self.text:
            raise ValueError("requirement item text must not be empty")
        if (
            type(self.source_start) is not int
            or type(self.source_end) is not int
            or self.source_start < 0
            or self.source_end <= self.source_start
        ):
            raise ValueError("requirement source span must be a positive character range")
        if self.source_end - self.source_start != len(self.text):
            raise ValueError("requirement source span length must equal item text length")
        if type(self.critical) is not bool:
            raise TypeError("requirement critical must be a bool")
        if not isinstance(self.not_implemented_reason, str):
            raise TypeError("not_implemented_reason must be a string")
        if (
            self.not_implemented_reason
            and self.not_implemented_reason != self.not_implemented_reason.strip()
        ):
            raise ValueError("not_implemented_reason must not contain surrounding whitespace")


@dataclass(frozen=True, slots=True)
class RequirementTargetBinding:
    """An explicit requirement-to-certified-target edge supplied upstream."""

    requirement_id: str
    target_id: str
    basis: str = EXPLICIT_BINDING_BASIS

    def __post_init__(self) -> None:
        if (
            not isinstance(self.requirement_id, str)
            or not _EXPLICIT_ID.fullmatch(self.requirement_id)
            or not isinstance(self.target_id, str)
            or not _EXPLICIT_ID.fullmatch(self.target_id)
        ):
            raise ValueError("trace bindings require explicit whitespace-free ids")
        if self.basis != EXPLICIT_BINDING_BASIS:
            raise ValueError(
                "trace binding basis must be explicit_structured_input; inferred name matching is forbidden"
            )


@dataclass(frozen=True, slots=True)
class TraceabilityInput:
    """Immutable Stage-4 input contract for one final requirement artifact."""

    requirement_artifact_sha256: str
    requirements: tuple[RequirementItem, ...]
    bindings: tuple[RequirementTargetBinding, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "requirements", tuple(self.requirements))
        object.__setattr__(self, "bindings", tuple(self.bindings))
        if not _SHA256.fullmatch(self.requirement_artifact_sha256):
            raise ValueError("traceability input requires a requirement SHA-256")
        if not self.requirements:
            raise ValueError("traceability input requires at least one requirement item")
        if any(not isinstance(item, RequirementItem) for item in self.requirements):
            raise TypeError("requirements must contain RequirementItem objects")
        if any(not isinstance(item, RequirementTargetBinding) for item in self.bindings):
            raise TypeError("bindings must contain RequirementTargetBinding objects")

        requirement_ids = [item.requirement_id for item in self.requirements]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("traceability requirement ids must be unique")
        known_ids = set(requirement_ids)
        binding_ids = [item.requirement_id for item in self.bindings]
        if any(item not in known_ids for item in binding_ids):
            raise ValueError("trace binding refers to an unknown requirement id")
        if len(binding_ids) != len(set(binding_ids)):
            raise ValueError("each requirement item must have exactly one target binding")
        if set(binding_ids) != known_ids:
            raise ValueError("each requirement item must have exactly one target binding")

    @property
    def sha256(self) -> str:
        payload = {
            "schema_version": "v8-traceability-input-1",
            "requirement_artifact_sha256": self.requirement_artifact_sha256,
            "requirements": [asdict(item) for item in self.requirements],
            "bindings": [asdict(item) for item in self.bindings],
        }
        return sha256_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )


@dataclass(frozen=True, slots=True)
class CertifiedTraceRegistry:
    """Audited target and topology selectors for one certified template."""

    template_id: str
    parameter_names: tuple[str, ...]
    sysml_parts: tuple[str, ...]
    sysml_connections: tuple[tuple[str, str], ...]
    modelica_connections: tuple[str, ...]
    structural_correspondence_only: bool = True

    def __post_init__(self) -> None:
        for field_name in (
            "parameter_names",
            "sysml_parts",
            "sysml_connections",
            "modelica_connections",
        ):
            object.__setattr__(self, field_name, tuple(getattr(self, field_name)))
        if not self.template_id.strip():
            raise ValueError("certified trace registry requires a template id")
        if not self.parameter_names or not self.sysml_parts or not self.modelica_connections:
            raise ValueError("certified trace registry cannot be empty")
        if len(self.parameter_names) != len(set(self.parameter_names)):
            raise ValueError("certified trace parameter names must be unique")
        if len(self.sysml_parts) != len(set(self.sysml_parts)):
            raise ValueError("certified trace SysML parts must be unique")
        if len(self.sysml_connections) != len(set(self.sysml_connections)):
            raise ValueError("certified trace SysML connections must be unique")
        if len(self.modelica_connections) != len(set(self.modelica_connections)):
            raise ValueError("certified trace Modelica connections must be unique")
        if self.structural_correspondence_only is not True:
            raise ValueError("V8 topology trace may claim structural correspondence only")

    @property
    def expected_target_ids(self) -> tuple[str, ...]:
        return tuple(parameter_target_id(name) for name in self.parameter_names) + (
            TOPOLOGY_TARGET_ID,
        )


def parameter_target_id(canonical_name: str) -> str:
    if not isinstance(canonical_name, str) or not _EXPLICIT_ID.fullmatch(canonical_name):
        raise ValueError("canonical parameter name must be an explicit id")
    return f"{PARAMETER_TARGET_PREFIX}{canonical_name}"


_TRACE_REGISTRY: dict[str, CertifiedTraceRegistry] = {
    "rc_filter": CertifiedTraceRegistry(
        template_id="rc_filter",
        parameter_names=(
            "resistance",
            "capacitance",
            "cutoff_frequency",
            "input_voltage",
        ),
        sysml_parts=("source", "resistor", "capacitor", "ground"),
        sysml_connections=(
            ("source", "resistor"),
            ("resistor", "capacitor"),
            ("capacitor", "ground"),
            ("source", "ground"),
        ),
        modelica_connections=(
            "connect(source.p, resistor.p);",
            "connect(resistor.n, capacitor.p);",
            "connect(capacitor.n, ground.p);",
            "connect(source.n, ground.p);",
        ),
    ),
    "rlc_filter": CertifiedTraceRegistry(
        template_id="rlc_filter",
        parameter_names=(
            "resistance",
            "inductance",
            "capacitance",
            "cutoff_frequency",
            "input_voltage",
        ),
        sysml_parts=("source", "resistor", "inductor", "capacitor", "ground"),
        sysml_connections=(
            ("source", "resistor"),
            ("resistor", "inductor"),
            ("inductor", "capacitor"),
            ("capacitor", "ground"),
            ("source", "ground"),
        ),
        modelica_connections=(
            "connect(source.p, resistor.p);",
            "connect(resistor.n, inductor.p);",
            "connect(inductor.n, capacitor.p);",
            "connect(capacitor.n, ground.p);",
            "connect(source.n, ground.p);",
        ),
    ),
    "opamp_inverting": CertifiedTraceRegistry(
        template_id="opamp_inverting",
        parameter_names=(
            "input_voltage",
            "input_resistance",
            "feedback_resistance",
            "positive_supply",
            "negative_supply",
        ),
        sysml_parts=(
            "source",
            "inputResistor",
            "feedbackResistor",
            "opAmp",
            "outputSensor",
            "ground",
        ),
        sysml_connections=(
            ("source", "inputResistor"),
            ("source", "ground"),
            ("inputResistor", "opAmp"),
            ("feedbackResistor", "opAmp"),
            ("opAmp", "ground"),
            ("opAmp", "outputSensor"),
            ("outputSensor", "ground"),
        ),
        modelica_connections=(
            "connect(source.p, inputResistor.p);",
            "connect(source.n, ground.p);",
            "connect(inputResistor.n, opAmp.in_n);",
            "connect(feedbackResistor.p, opAmp.in_n);",
            "connect(feedbackResistor.n, opAmp.out);",
            "connect(opAmp.in_p, ground.p);",
            "connect(opAmp.out, outputSensor.p);",
            "connect(outputSensor.n, ground.p);",
        ),
    ),
    "thermal_single_room": CertifiedTraceRegistry(
        template_id="thermal_single_room",
        parameter_names=(
            "heat_capacity",
            "thermal_resistance",
            "initial_temperature",
            "outdoor_temperature",
        ),
        sysml_parts=("outdoor", "wall", "room", "roomSensor"),
        sysml_connections=(
            ("outdoor", "wall"),
            ("wall", "room"),
            ("room", "roomSensor"),
        ),
        modelica_connections=(
            "connect(outdoor.port, wall.port_a);",
            "connect(wall.port_b, room.port);",
            "connect(room.port, roomSensor.port);",
        ),
    ),
    "thermal_dual_room": CertifiedTraceRegistry(
        template_id="thermal_dual_room",
        parameter_names=(
            "heat_capacity_a",
            "heat_capacity_b",
            "shared_resistance",
            "outdoor_resistance_a",
            "outdoor_resistance_b",
            "initial_temperature_a",
            "initial_temperature_b",
            "outdoor_temperature",
        ),
        sysml_parts=(
            "outdoor",
            "outdoorWallA",
            "outdoorWallB",
            "sharedWall",
            "roomA",
            "roomB",
            "roomASensor",
            "roomBSensor",
        ),
        sysml_connections=(
            ("outdoor", "outdoorWallA"),
            ("outdoorWallA", "roomA"),
            ("roomA", "sharedWall"),
            ("sharedWall", "roomB"),
            ("roomB", "outdoorWallB"),
            ("outdoorWallB", "outdoor"),
            ("roomA", "roomASensor"),
            ("roomB", "roomBSensor"),
        ),
        modelica_connections=(
            "connect(outdoor.port, outdoorWallA.port_a);",
            "connect(outdoorWallA.port_b, roomA.port);",
            "connect(roomA.port, sharedWall.port_a);",
            "connect(sharedWall.port_b, roomB.port);",
            "connect(roomB.port, outdoorWallB.port_a);",
            "connect(outdoorWallB.port_b, outdoor.port);",
            "connect(roomA.port, roomASensor.port);",
            "connect(roomB.port, roomBSensor.port);",
        ),
    ),
}

TEMPLATE_TRACE_REGISTRY: Mapping[str, CertifiedTraceRegistry] = MappingProxyType(
    _TRACE_REGISTRY
)


def get_trace_registry(template_id: str) -> CertifiedTraceRegistry:
    try:
        return TEMPLATE_TRACE_REGISTRY[template_id]
    except KeyError as exc:
        raise ValueError(f"no certified trace registry for template: {template_id}") from exc


def build_traceability(
    *,
    trace_input: TraceabilityInput,
    final_requirement: str,
    final_sysml: str,
    final_modelica: str,
    canonical_sysml: str,
    canonical_render: RenderedTemplate,
    parameters: Iterable[NormalizedParameter],
    final_base_checks: Iterable[QualityCheckResult],
) -> tuple[TraceabilityReport, QualityCheckResult]:
    """Build and validate the exact final trace snapshot.

    Base-check failure statuses are preserved in :class:`TraceCheckReference`
    and do not by themselves invalidate a structural link.  Missing or
    ambiguous evidence does invalidate the link.  The traceability check is
    deliberately excluded from ``final_base_checks`` to prevent self-reference.
    """

    if not isinstance(trace_input, TraceabilityInput):
        raise TypeError("trace_input must be a TraceabilityInput")
    if not isinstance(canonical_render, RenderedTemplate):
        raise TypeError("canonical_render must be a RenderedTemplate")
    for label, value in (
        ("final_requirement", final_requirement),
        ("final_sysml", final_sysml),
        ("final_modelica", final_modelica),
        ("canonical_sysml", canonical_sysml),
    ):
        if not isinstance(value, str):
            raise TypeError(f"{label} must be a string")

    parameter_tuple = tuple(parameters)
    if any(not isinstance(item, NormalizedParameter) for item in parameter_tuple):
        raise TypeError("parameters must contain NormalizedParameter objects")
    base_checks = tuple(final_base_checks)
    if any(not isinstance(item, QualityCheckResult) for item in base_checks):
        raise TypeError("final_base_checks must contain QualityCheckResult objects")
    check_names = [item.check_type for item in base_checks]
    if TRACEABILITY_CHECK_TYPE in check_names:
        raise ValueError("final_base_checks must not include traceability_integrity")
    if len(check_names) != len(set(check_names)):
        raise ValueError("final_base_checks must have unique check_type values")
    check_index = {item.check_type: item for item in base_checks}

    registry = get_trace_registry(canonical_render.template_id)
    spec = get_template_spec(canonical_render.template_id)
    global_issues = _registry_issues(registry, spec, canonical_render)
    render_valid, render_issues = verify_rendered_template(canonical_render)
    if not render_valid:
        global_issues.extend(
            f"canonical render integrity: {item}" for item in render_issues
        )

    requirement_sha256 = sha256_text(final_requirement)
    sysml_sha256 = sha256_text(final_sysml)
    modelica_sha256 = sha256_text(final_modelica)
    if trace_input.requirement_artifact_sha256 != requirement_sha256:
        global_issues.append(
            "traceability input requirement hash does not match the final requirement artifact"
        )
    artifact_alignment_issues: list[str] = []
    if final_sysml != canonical_sysml:
        issue = "final SysML differs from the supplied canonical SysML snapshot"
        global_issues.append(issue)
        artifact_alignment_issues.append(issue)
    if final_modelica != canonical_render.code:
        issue = "final Modelica differs from the certified canonical render"
        global_issues.append(issue)
        artifact_alignment_issues.append(issue)
    if canonical_render.rendered_sha256 != sha256_text(canonical_render.code):
        issue = "canonical rendered hash does not match canonical Modelica content"
        global_issues.append(issue)
        artifact_alignment_issues.append(issue)

    parameter_index, parameter_issues = _parameter_index(
        parameter_tuple, canonical_render
    )
    global_issues.extend(parameter_issues)

    binding_index = {item.requirement_id: item for item in trace_input.bindings}
    expected_targets = set(registry.expected_target_ids)
    valid_bound_targets = {
        item.target_id for item in trace_input.bindings if item.target_id in expected_targets
    }
    missing_targets = sorted(expected_targets - valid_bound_targets)
    if missing_targets:
        global_issues.append(
            "certified trace targets are not covered by explicit bindings: "
            + ", ".join(missing_targets)
        )

    links: list[TraceLink] = []
    for requirement in trace_input.requirements:
        binding = binding_index[requirement.requirement_id]
        source_issues = _requirement_source_issues(requirement, final_requirement)
        if trace_input.requirement_artifact_sha256 != requirement_sha256:
            source_issues.append(
                "requirement item is bound to a stale requirement artifact hash"
            )
        target_valid = binding.target_id in expected_targets
        if not target_valid:
            source_issues.append(
                f"target {binding.target_id!r} is not registered for template {registry.template_id}"
            )

        if requirement.not_implemented_reason and target_valid:
            links.append(
                TraceLink(
                    requirement_id=requirement.requirement_id,
                    requirement_text=requirement.text,
                    requirement_text_sha256=sha256_text(requirement.text),
                    target_id=binding.target_id,
                    status=TraceLinkStatus.NOT_IMPLEMENTED,
                    requirement_source_start=requirement.source_start,
                    requirement_source_end=requirement.source_end,
                    critical=requirement.critical,
                    not_implemented_reason=requirement.not_implemented_reason,
                    binding_basis=binding.basis,
                    issues=tuple(
                        _deduplicate(
                            [requirement.not_implemented_reason, *source_issues]
                        )
                    ),
                )
            )
            continue

        link_issues = [*source_issues, *artifact_alignment_issues]
        sysml_elements: tuple[TraceElementReference, ...] = ()
        modelica_elements: tuple[TraceElementReference, ...] = ()
        check_references: tuple[TraceCheckReference, ...] = ()
        normalized_parameter_sha256 = ""

        if target_valid and binding.target_id == TOPOLOGY_TARGET_ID:
            sysml_elements, modelica_elements, element_issues = _topology_evidence(
                registry,
                final_sysml=final_sysml,
                final_modelica=final_modelica,
                canonical_sysml=canonical_sysml,
                canonical_modelica=canonical_render.code,
                sysml_sha256=sysml_sha256,
                modelica_sha256=modelica_sha256,
            )
            link_issues.extend(element_issues)
            check_references, check_issues = _check_references(
                _TOPOLOGY_CHECKS, check_index
            )
            link_issues.extend(check_issues)
        elif target_valid:
            canonical_name = binding.target_id[len(PARAMETER_TARGET_PREFIX) :]
            parameter = parameter_index.get(canonical_name)
            if parameter is None:
                link_issues.append(
                    f"normalized parameter {canonical_name!r} is absent or ambiguous"
                )
            else:
                normalized_parameter_sha256 = normalized_parameter_semantic_sha256(
                    parameter
                )
            sysml_elements, modelica_elements, element_issues = _parameter_evidence(
                canonical_name,
                canonical_render,
                final_sysml=final_sysml,
                final_modelica=final_modelica,
                canonical_sysml=canonical_sysml,
                sysml_sha256=sysml_sha256,
                modelica_sha256=modelica_sha256,
            )
            link_issues.extend(element_issues)
            check_references, check_issues = _check_references(
                _PARAMETER_CHECKS, check_index
            )
            link_issues.extend(check_issues)

        link_issues = _deduplicate(link_issues)
        links.append(
            TraceLink(
                requirement_id=requirement.requirement_id,
                requirement_text=requirement.text,
                requirement_text_sha256=sha256_text(requirement.text),
                target_id=binding.target_id,
                status=(
                    TraceLinkStatus.VERIFIED
                    if not link_issues
                    else TraceLinkStatus.UNVERIFIED
                ),
                requirement_source_start=requirement.source_start,
                requirement_source_end=requirement.source_end,
                critical=requirement.critical,
                not_implemented_reason=requirement.not_implemented_reason,
                binding_basis=binding.basis,
                normalized_parameter_sha256=normalized_parameter_sha256,
                sysml_elements=sysml_elements,
                modelica_elements=modelica_elements,
                check_references=check_references,
                issues=tuple(link_issues),
            )
        )

    global_issues = _deduplicate(global_issues)
    critical_ids = tuple(
        item.requirement_id for item in trace_input.requirements if item.critical
    )
    if not critical_ids:
        global_issues.append("traceability input declares no critical requirements")
        global_issues = _deduplicate(global_issues)
    integrity_passed = bool(links) and bool(critical_ids) and not global_issues and all(
        link.status is TraceLinkStatus.VERIFIED
        for link in links
        if link.requirement_id in critical_ids
    )
    report = TraceabilityReport(
        scope=TRACEABILITY_SCOPE,
        template_id=registry.template_id,
        template_sha256=canonical_render.template_sha256,
        requirement_artifact_sha256=requirement_sha256,
        sysml_artifact_sha256=sysml_sha256,
        modelica_artifact_sha256=modelica_sha256,
        trace_input_sha256=trace_input.sha256,
        trace_input_requirement_artifact_sha256=(
            trace_input.requirement_artifact_sha256
        ),
        links=tuple(links),
        critical_requirement_ids=critical_ids,
        integrity_passed=integrity_passed,
        issues=tuple(global_issues),
    )
    return report, _traceability_check(report, trace_input)


def _registry_issues(registry, spec, canonical_render: RenderedTemplate) -> list[str]:
    issues: list[str] = []
    if tuple(registry.parameter_names) != tuple(spec.parameter_names):
        issues.append("certified trace parameter registry differs from TemplateSpec")
    if tuple(registry.sysml_parts) != tuple(spec.sysml_components):
        issues.append("certified trace SysML part registry differs from TemplateSpec")
    if tuple(registry.sysml_connections) != tuple(spec.sysml_connections):
        issues.append("certified trace SysML connection registry differs from TemplateSpec")
    if canonical_render.template_sha256 != spec.source_sha256:
        issues.append("trace template hash differs from the certified template source")
    for selector in registry.modelica_connections:
        if spec.source.count(selector) != 1:
            issues.append(
                f"registered Modelica connection is not unique in certified source: {selector}"
            )
    return issues


def _parameter_index(
    parameters: tuple[NormalizedParameter, ...], canonical_render: RenderedTemplate
) -> tuple[dict[str, NormalizedParameter], list[str]]:
    issues: list[str] = []
    indexed: dict[str, NormalizedParameter] = {}
    duplicates: set[str] = set()
    for parameter in parameters:
        if parameter.canonical_name in indexed:
            duplicates.add(parameter.canonical_name)
        else:
            indexed[parameter.canonical_name] = parameter
        if parameter.status is not ParameterStatus.KNOWN:
            issues.append(
                f"normalized parameter {parameter.canonical_name} is not known"
            )
    if duplicates:
        issues.append(
            "normalized parameter names are duplicated: " + ", ".join(sorted(duplicates))
        )
        for name in duplicates:
            indexed.pop(name, None)
    try:
        canonical = canonicalize_parameters(canonical_render.template_id, parameters)
    except TemplateInputError as exc:
        issues.extend(f"normalized parameter contract: {item}" for item in exc.issues)
    else:
        observed = tuple((item.name, item.value, item.unit) for item in canonical)
        rendered = tuple(
            (item.name, item.value, item.unit) for item in canonical_render.parameters
        )
        if len(observed) != len(rendered) or any(
            left[0] != right[0]
            or left[2] != right[2]
            or not math.isclose(left[1], right[1], rel_tol=1e-12, abs_tol=1e-15)
            for left, right in zip(observed, rendered)
        ):
            issues.append("normalized parameters differ from the canonical render slots")
    return indexed, issues


def _requirement_source_issues(
    requirement: RequirementItem, final_requirement: str
) -> list[str]:
    if requirement.source_end > len(final_requirement):
        return [
            f"requirement span {requirement.source_start}:{requirement.source_end} is outside the final artifact"
        ]
    observed = final_requirement[requirement.source_start : requirement.source_end]
    if observed != requirement.text:
        return ["requirement item text does not exactly match its final artifact span"]
    return []


def _parameter_evidence(
    canonical_name: str,
    canonical_render: RenderedTemplate,
    *,
    final_sysml: str,
    final_modelica: str,
    canonical_sysml: str,
    sysml_sha256: str,
    modelica_sha256: str,
) -> tuple[
    tuple[TraceElementReference, ...],
    tuple[TraceElementReference, ...],
    list[str],
]:
    issues: list[str] = []
    canonical_values = {
        item.name: item for item in canonical_render.parameters
    }
    canonical = canonical_values.get(canonical_name)
    if canonical is None:
        sysml_selector = f"attribute {canonical_name} : Real = <unregistered>;"
        issues.append(
            f"canonical render does not contain parameter {canonical_name!r}"
        )
    else:
        sysml_selector = (
            f"attribute {canonical_name} : Real = {_real_literal(canonical.value)};"
        )
    if canonical_sysml.count(sysml_selector) != 1:
        issues.append(
            f"canonical SysML selector is not exact and unique: {sysml_selector}"
        )
    sysml_cardinality = final_sysml.count(sysml_selector)
    if sysml_cardinality != 1:
        issues.append(
            f"final SysML selector cardinality is {sysml_cardinality}, expected 1: {sysml_selector}"
        )
    sysml_reference = TraceElementReference(
        element_id=f"sysml.attribute.{canonical_name}",
        layer=TraceLayer.SYSML,
        kind="attribute",
        exact_selector=sysml_selector,
        artifact_sha256=sysml_sha256,
        cardinality=sysml_cardinality,
    )

    selector_matches = _modelica_parameter_selectors(
        canonical_render.code, canonical_name
    )
    if len(selector_matches) != 1:
        issues.append(
            f"canonical Modelica parameter selector count is {len(selector_matches)}, expected 1 for {canonical_name}"
        )
        modelica_selector = f"final parameter <exact-type> {canonical_name} = <exact-value>;"
    else:
        modelica_selector = selector_matches[0]
    modelica_cardinality = final_modelica.count(modelica_selector)
    if modelica_cardinality != 1:
        issues.append(
            f"final Modelica selector cardinality is {modelica_cardinality}, expected 1: {modelica_selector}"
        )
    modelica_reference = TraceElementReference(
        element_id=f"modelica.parameter.{canonical_name}",
        layer=TraceLayer.MODELICA,
        kind="parameter",
        exact_selector=modelica_selector,
        artifact_sha256=modelica_sha256,
        cardinality=modelica_cardinality,
    )
    return (sysml_reference,), (modelica_reference,), issues


def _topology_evidence(
    registry: CertifiedTraceRegistry,
    *,
    final_sysml: str,
    final_modelica: str,
    canonical_sysml: str,
    canonical_modelica: str,
    sysml_sha256: str,
    modelica_sha256: str,
) -> tuple[
    tuple[TraceElementReference, ...],
    tuple[TraceElementReference, ...],
    list[str],
]:
    issues: list[str] = []
    sysml_references: list[TraceElementReference] = []
    for name in registry.sysml_parts:
        selector = f"part {name};"
        if canonical_sysml.count(selector) != 1:
            issues.append(
                f"registered SysML part is not exact and unique in canonical source: {selector}"
            )
        cardinality = final_sysml.count(selector)
        if cardinality != 1:
            issues.append(
                f"final SysML part cardinality is {cardinality}, expected 1: {selector}"
            )
        sysml_references.append(
            TraceElementReference(
                element_id=f"sysml.part.{name}",
                layer=TraceLayer.SYSML,
                kind="part",
                exact_selector=selector,
                artifact_sha256=sysml_sha256,
                cardinality=cardinality,
            )
        )
    for left, right in registry.sysml_connections:
        selector = f"connect {left} to {right};"
        if canonical_sysml.count(selector) != 1:
            issues.append(
                f"registered SysML connection is not exact and unique in canonical source: {selector}"
            )
        cardinality = final_sysml.count(selector)
        if cardinality != 1:
            issues.append(
                f"final SysML connection cardinality is {cardinality}, expected 1: {selector}"
            )
        sysml_references.append(
            TraceElementReference(
                element_id=f"sysml.connection.{left}__{right}",
                layer=TraceLayer.SYSML,
                kind="component_level_connection_structural_only",
                exact_selector=selector,
                artifact_sha256=sysml_sha256,
                cardinality=cardinality,
            )
        )

    modelica_references: list[TraceElementReference] = []
    for selector in registry.modelica_connections:
        if canonical_modelica.count(selector) != 1:
            issues.append(
                f"registered Modelica connection is not exact and unique in canonical render: {selector}"
            )
        cardinality = final_modelica.count(selector)
        if cardinality != 1:
            issues.append(
                f"final Modelica connection cardinality is {cardinality}, expected 1: {selector}"
            )
        endpoints = _connection_element_suffix(selector)
        modelica_references.append(
            TraceElementReference(
                element_id=f"modelica.connection.{endpoints}",
                layer=TraceLayer.MODELICA,
                kind="port_level_connection_structural_only",
                exact_selector=selector,
                artifact_sha256=modelica_sha256,
                cardinality=cardinality,
            )
        )
    return tuple(sysml_references), tuple(modelica_references), issues


def _check_references(
    names: tuple[str, ...],
    check_index: Mapping[str, QualityCheckResult],
) -> tuple[tuple[TraceCheckReference, ...], list[str]]:
    references: list[TraceCheckReference] = []
    issues: list[str] = []
    for name in names:
        check = check_index.get(name)
        if check is None:
            issues.append(f"required trace endpoint check is missing: {name}")
            continue
        references.append(
            TraceCheckReference(
                check_type=check.check_type,
                status=check.status,
                required=check.required,
                semantic_evidence_sha256=quality_check_semantic_sha256(check),
            )
        )
        if not check.required:
            issues.append(f"trace endpoint check is no longer required: {name}")
    return tuple(references), issues


def _traceability_check(
    report: TraceabilityReport, trace_input: TraceabilityInput
) -> QualityCheckResult:
    evidence = {
        "report_sha256": traceability_report_sha256(report),
        "trace_input_sha256": trace_input.sha256,
        "scope": report.scope,
        "template_id": report.template_id,
        "critical_requirement_ids": list(report.critical_requirement_ids),
        "verified_link_count": sum(
            item.status is TraceLinkStatus.VERIFIED for item in report.links
        ),
        "semantic_claim": "explicit_structural_correspondence_only",
    }
    if report.integrity_passed:
        return QualityCheckResult(
            check_type=TRACEABILITY_CHECK_TYPE,
            status=CheckStatus.PASS,
            required=True,
            evidence=evidence,
            tool_name=TRACEABILITY_TOOL_NAME,
            tool_version=TRACEABILITY_TOOL_VERSION,
        )
    issues = list(report.issues)
    for link in report.links:
        if link.requirement_id in report.critical_requirement_ids:
            issues.extend(
                f"{link.requirement_id}: {issue}" for issue in link.issues
            )
    issues = _deduplicate(issues) or ["critical traceability coverage is incomplete"]
    return QualityCheckResult(
        check_type=TRACEABILITY_CHECK_TYPE,
        status=CheckStatus.FAIL,
        required=True,
        issues=issues,
        evidence={**evidence, "reason": "traceability_integrity_failed"},
        tool_name=TRACEABILITY_TOOL_NAME,
        tool_version=TRACEABILITY_TOOL_VERSION,
        failure_kind=FailureKind.CONTENT,
        failure_category="traceability_integrity_failed",
    )


def _modelica_parameter_selectors(code: str, canonical_name: str) -> tuple[str, ...]:
    declaration = re.compile(
        rf"^final\s+parameter\s+.+\s+{re.escape(canonical_name)}\s*=\s*[^;]+;$"
    )
    return tuple(
        stripped
        for line in code.splitlines()
        if (stripped := line.strip()) and declaration.fullmatch(stripped)
    )


def _connection_element_suffix(selector: str) -> str:
    match = re.fullmatch(r"connect\(([^,]+),\s*([^\)]+)\);", selector)
    if match is None:
        return sha256_text(selector)[:16]
    return f"{match.group(1).strip()}__{match.group(2).strip()}"


def _real_literal(value: float) -> str:
    text = format(float(value), ".17g")
    return text if any(character in text for character in ".eE") else f"{text}.0"


def _deduplicate(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


__all__ = [
    "CertifiedTraceRegistry",
    "EXPLICIT_BINDING_BASIS",
    "PARAMETER_TARGET_PREFIX",
    "RequirementItem",
    "RequirementTargetBinding",
    "TEMPLATE_TRACE_REGISTRY",
    "TOPOLOGY_TARGET_ID",
    "TRACEABILITY_CHECK_TYPE",
    "TRACEABILITY_SCOPE",
    "TraceabilityInput",
    "build_traceability",
    "get_trace_registry",
    "parameter_target_id",
]
