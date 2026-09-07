from __future__ import annotations

from dataclasses import replace
import unittest

from week9.src.modelica_templates import render_template, representative_parameters
from week9.src.pipeline import render_canonical_sysml
from week9.src.schemas import (
    CheckStatus,
    FailureKind,
    QualityCheckResult,
    TraceLinkStatus,
    quality_check_semantic_sha256,
    sha256_text,
    traceability_report_sha256,
)
from week9.src.traceability import (
    EXPLICIT_BINDING_BASIS,
    TEMPLATE_TRACE_REGISTRY,
    TOPOLOGY_TARGET_ID,
    TRACEABILITY_CHECK_TYPE,
    TRACEABILITY_SCOPE,
    RequirementItem,
    RequirementTargetBinding,
    TraceabilityInput,
    build_traceability,
    parameter_target_id,
)


_BASE_CHECK_NAMES = (
    "requirement_normalization",
    "parameter_unit_consistency",
    "sysml_syntax",
    "cross_model_consistency",
    "certified_template_integrity",
    "modelica_compile",
)


def _check(name: str, status: CheckStatus = CheckStatus.PASS) -> QualityCheckResult:
    if status is CheckStatus.PASS:
        return QualityCheckResult(
            check_type=name,
            status=status,
            required=True,
            evidence={"fixture": name},
            tool_name="traceability fixture",
        )
    return QualityCheckResult(
        check_type=name,
        status=status,
        required=True,
        issues=[f"injected {name} failure"],
        evidence={"reason": f"injected_{name}_failure"},
        tool_name="traceability fixture",
        failure_kind=FailureKind.CONTENT,
        failure_category=f"injected_{name}_failure",
    )


def _trace_input(template_id: str):
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


def _fixture(template_id: str, *, checks=None, trace_input=None):
    parameters = representative_parameters(template_id)
    rendered = render_template(
        template_id,
        parameters,
        model_name=f"Trace_{template_id}",
    )
    sysml = render_canonical_sysml(rendered)
    requirement, default_input = _trace_input(template_id)
    report, trace_check = build_traceability(
        trace_input=trace_input or default_input,
        final_requirement=requirement,
        final_sysml=sysml,
        final_modelica=rendered.code,
        canonical_sysml=sysml,
        canonical_render=rendered,
        parameters=parameters,
        final_base_checks=checks
        if checks is not None
        else tuple(_check(name) for name in _BASE_CHECK_NAMES),
    )
    return requirement, default_input, rendered, sysml, report, trace_check


class TraceabilityInputContractTests(unittest.TestCase):
    def test_binding_basis_forbids_inferred_name_matching(self):
        with self.assertRaisesRegex(ValueError, "inferred name matching is forbidden"):
            RequirementTargetBinding(
                requirement_id="REQ-1",
                target_id="parameter:resistance",
                basis="similar_name",
            )

    def test_requirement_span_length_is_exact(self):
        with self.assertRaisesRegex(ValueError, "span length"):
            RequirementItem("REQ-1", "abc", 0, 2)

    def test_every_requirement_requires_exactly_one_binding(self):
        item = RequirementItem("REQ-1", "abc", 0, 3)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            TraceabilityInput(
                requirement_artifact_sha256=sha256_text("abc"),
                requirements=(item,),
                bindings=(),
            )


class CertifiedTraceRegistryTests(unittest.TestCase):
    def test_all_five_templates_have_exact_parameter_and_topology_targets(self):
        self.assertEqual(
            set(TEMPLATE_TRACE_REGISTRY),
            {
                "rc_filter",
                "rlc_filter",
                "opamp_inverting",
                "thermal_single_room",
                "thermal_dual_room",
            },
        )
        for template_id, registry in TEMPLATE_TRACE_REGISTRY.items():
            with self.subTest(template_id=template_id):
                expected = {
                    *(parameter_target_id(name) for name in registry.parameter_names),
                    TOPOLOGY_TARGET_ID,
                }
                self.assertEqual(set(registry.expected_target_ids), expected)
                self.assertTrue(registry.structural_correspondence_only)
                self.assertTrue(registry.sysml_parts)
                self.assertTrue(registry.sysml_connections)
                self.assertTrue(registry.modelica_connections)

    def test_all_five_canonical_snapshots_build_verified_reports(self):
        for template_id, registry in TEMPLATE_TRACE_REGISTRY.items():
            with self.subTest(template_id=template_id):
                _, _, _, _, report, trace_check = _fixture(template_id)
                self.assertTrue(report.integrity_passed, report.issues)
                self.assertEqual(report.scope, TRACEABILITY_SCOPE)
                self.assertEqual(trace_check.status, CheckStatus.PASS)
                self.assertTrue(trace_check.required)
                self.assertEqual(
                    trace_check.evidence["report_sha256"],
                    traceability_report_sha256(report),
                )
                self.assertEqual(
                    len(report.links), len(registry.parameter_names) + 1
                )
                self.assertTrue(
                    all(link.status is TraceLinkStatus.VERIFIED for link in report.links)
                )
                topology = next(
                    link for link in report.links if link.target_id == TOPOLOGY_TARGET_ID
                )
                self.assertEqual(
                    len(topology.sysml_elements),
                    len(registry.sysml_parts) + len(registry.sysml_connections),
                )
                self.assertEqual(
                    len(topology.modelica_elements),
                    len(registry.modelica_connections),
                )
                self.assertTrue(
                    all(item.cardinality == 1 for item in topology.sysml_elements)
                )
                self.assertTrue(
                    all(item.cardinality == 1 for item in topology.modelica_elements)
                )
                self.assertTrue(
                    all(
                        "structural_only" in item.kind
                        for item in topology.modelica_elements
                    )
                )
                self.assertNotIn(
                    TRACEABILITY_CHECK_TYPE,
                    {
                        reference.check_type
                        for link in report.links
                        for reference in link.check_references
                    },
                )


class TraceabilityBuildNegativeTests(unittest.TestCase):
    def test_similar_but_unregistered_parameter_target_is_not_inferred(self):
        requirement, trace_input = _trace_input("rc_filter")
        first = trace_input.bindings[0]
        misspelled = replace(first, target_id="parameter:resistnce")
        bad_input = TraceabilityInput(
            requirement_artifact_sha256=trace_input.requirement_artifact_sha256,
            requirements=trace_input.requirements,
            bindings=(misspelled, *trace_input.bindings[1:]),
        )
        parameters = representative_parameters("rc_filter")
        rendered = render_template("rc_filter", parameters, model_name="NoFuzzy")
        sysml = render_canonical_sysml(rendered)
        report, trace_check = build_traceability(
            trace_input=bad_input,
            final_requirement=requirement,
            final_sysml=sysml,
            final_modelica=rendered.code,
            canonical_sysml=sysml,
            canonical_render=rendered,
            parameters=parameters,
            final_base_checks=tuple(_check(name) for name in _BASE_CHECK_NAMES),
        )
        link = next(item for item in report.links if item.requirement_id == first.requirement_id)
        self.assertEqual(link.status, TraceLinkStatus.UNVERIFIED)
        self.assertIn("not registered", " ".join(link.issues))
        self.assertFalse(report.integrity_passed)
        self.assertEqual(trace_check.status, CheckStatus.FAIL)

    def test_stale_requirement_hash_and_wrong_span_are_blocked(self):
        requirement, trace_input = _trace_input("rc_filter")
        first = trace_input.requirements[0]
        wrong_text = ("X" if first.text[0] != "X" else "Y") + first.text[1:]
        bad_item = replace(first, text=wrong_text)
        bad_input = TraceabilityInput(
            requirement_artifact_sha256="0" * 64,
            requirements=(bad_item, *trace_input.requirements[1:]),
            bindings=trace_input.bindings,
        )
        parameters = representative_parameters("rc_filter")
        rendered = render_template("rc_filter", parameters, model_name="BadSource")
        sysml = render_canonical_sysml(rendered)
        report, trace_check = build_traceability(
            trace_input=bad_input,
            final_requirement=requirement,
            final_sysml=sysml,
            final_modelica=rendered.code,
            canonical_sysml=sysml,
            canonical_render=rendered,
            parameters=parameters,
            final_base_checks=tuple(_check(name) for name in _BASE_CHECK_NAMES),
        )
        self.assertFalse(report.integrity_passed)
        self.assertIn("requirement hash", " ".join(report.issues))
        self.assertIn("does not exactly match", " ".join(report.links[0].issues))
        self.assertEqual(trace_check.status, CheckStatus.FAIL)

    def test_duplicate_exact_selector_cardinality_is_unverified(self):
        parameters = representative_parameters("rc_filter")
        rendered = render_template("rc_filter", parameters, model_name="DuplicatePart")
        canonical_sysml = render_canonical_sysml(rendered)
        duplicate_sysml = canonical_sysml.replace(
            "part resistor;", "part resistor; part resistor;", 1
        )
        requirement, trace_input = _trace_input("rc_filter")
        report, trace_check = build_traceability(
            trace_input=trace_input,
            final_requirement=requirement,
            final_sysml=duplicate_sysml,
            final_modelica=rendered.code,
            canonical_sysml=canonical_sysml,
            canonical_render=rendered,
            parameters=parameters,
            final_base_checks=tuple(_check(name) for name in _BASE_CHECK_NAMES),
        )
        topology = next(
            link for link in report.links if link.target_id == TOPOLOGY_TARGET_ID
        )
        resistor = next(
            item for item in topology.sysml_elements if item.element_id == "sysml.part.resistor"
        )
        self.assertEqual(resistor.cardinality, 2)
        self.assertEqual(topology.status, TraceLinkStatus.UNVERIFIED)
        self.assertEqual(trace_check.status, CheckStatus.FAIL)

    def test_critical_not_implemented_is_explicit_and_fails_integrity(self):
        requirement, trace_input = _trace_input("rc_filter")
        first = replace(
            trace_input.requirements[0],
            not_implemented_reason="explicitly unsupported in the current model",
        )
        marked = TraceabilityInput(
            requirement_artifact_sha256=trace_input.requirement_artifact_sha256,
            requirements=(first, *trace_input.requirements[1:]),
            bindings=trace_input.bindings,
        )
        parameters = representative_parameters("rc_filter")
        rendered = render_template("rc_filter", parameters, model_name="NotImplemented")
        sysml = render_canonical_sysml(rendered)
        report, trace_check = build_traceability(
            trace_input=marked,
            final_requirement=requirement,
            final_sysml=sysml,
            final_modelica=rendered.code,
            canonical_sysml=sysml,
            canonical_render=rendered,
            parameters=parameters,
            final_base_checks=tuple(_check(name) for name in _BASE_CHECK_NAMES),
        )
        link = report.links[0]
        self.assertEqual(link.status, TraceLinkStatus.NOT_IMPLEMENTED)
        self.assertFalse(link.sysml_elements)
        self.assertFalse(link.modelica_elements)
        self.assertFalse(link.check_references)
        self.assertFalse(report.integrity_passed)
        self.assertEqual(trace_check.status, CheckStatus.FAIL)

    def test_missing_parameter_or_topology_coverage_is_a_report_failure(self):
        requirement, trace_input = _trace_input("rc_filter")
        shortened_requirement = requirement[
            : trace_input.requirements[-1].source_start
        ].rstrip("\n")
        shortened = TraceabilityInput(
            requirement_artifact_sha256=sha256_text(shortened_requirement),
            requirements=trace_input.requirements[:-1],
            bindings=trace_input.bindings[:-1],
        )
        parameters = representative_parameters("rc_filter")
        rendered = render_template("rc_filter", parameters, model_name="NoTopology")
        sysml = render_canonical_sysml(rendered)
        report, trace_check = build_traceability(
            trace_input=shortened,
            final_requirement=shortened_requirement,
            final_sysml=sysml,
            final_modelica=rendered.code,
            canonical_sysml=sysml,
            canonical_render=rendered,
            parameters=parameters,
            final_base_checks=tuple(_check(name) for name in _BASE_CHECK_NAMES),
        )
        self.assertFalse(report.integrity_passed)
        self.assertIn(TOPOLOGY_TARGET_ID, " ".join(report.issues))
        self.assertEqual(trace_check.status, CheckStatus.FAIL)

    def test_trace_check_cannot_be_used_as_its_own_endpoint(self):
        requirement, trace_input = _trace_input("rc_filter")
        parameters = representative_parameters("rc_filter")
        rendered = render_template("rc_filter", parameters, model_name="NoCycle")
        sysml = render_canonical_sysml(rendered)
        checks = [*(_check(name) for name in _BASE_CHECK_NAMES), _check(TRACEABILITY_CHECK_TYPE)]
        with self.assertRaisesRegex(ValueError, "must not include"):
            build_traceability(
                trace_input=trace_input,
                final_requirement=requirement,
                final_sysml=sysml,
                final_modelica=rendered.code,
                canonical_sysml=sysml,
                canonical_render=rendered,
                parameters=parameters,
                final_base_checks=checks,
            )

    def test_base_check_failure_is_preserved_without_breaking_structure_link(self):
        checks = [
            _check(name, CheckStatus.FAIL if name == "modelica_compile" else CheckStatus.PASS)
            for name in _BASE_CHECK_NAMES
        ]
        _, _, _, _, report, trace_check = _fixture("rc_filter", checks=checks)
        self.assertTrue(report.integrity_passed)
        self.assertEqual(trace_check.status, CheckStatus.PASS)
        for link in report.links:
            compile_ref = next(
                item for item in link.check_references if item.check_type == "modelica_compile"
            )
            self.assertEqual(compile_ref.status, CheckStatus.FAIL)
            failed_check = next(item for item in checks if item.check_type == "modelica_compile")
            self.assertEqual(
                compile_ref.semantic_evidence_sha256,
                quality_check_semantic_sha256(failed_check),
            )
            self.assertEqual(link.status, TraceLinkStatus.VERIFIED)


if __name__ == "__main__":
    unittest.main()
