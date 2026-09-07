from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import patch

from week9.experiments.repair_regression import REPAIR_SCENARIOS
from week9.src.hard_gate import SysMLParserAdapter, _validate_rc, _validate_rlc
from week9.src.modelica_templates import (
    TEMPLATE_REGISTRY,
    render_template,
    representative_parameters,
    verify_rendered_template,
)
from week9.src.modelica_tool import CsvData, resolve_omc_path
from week9.src.pipeline import (
    CertifiedTemplateRepairPipeline,
    run_offline_repair_pipeline,
)
from week9.src.repair_loop import (
    ArtifactBundle,
    RepairLoopConfig,
    RepairProposal,
    RepairProviderStop,
    RepairRequest,
    ValidationSnapshot,
    failure_fingerprint,
)
from week9.src.schemas import (
    CheckStatus,
    FailureKind,
    QualityCheckResult,
    RepairAction,
    RepairLayer,
    RepairStopReason,
    sha256_text,
)


def _pass(name: str = "modelica_compile") -> QualityCheckResult:
    return QualityCheckResult(name, CheckStatus.PASS, True)


def _fail(name: str = "modelica_compile") -> QualityCheckResult:
    return QualityCheckResult(
        name,
        CheckStatus.FAIL,
        True,
        issues=["injected content failure"],
        evidence={"reason": "injected_content_failure"},
        failure_kind=FailureKind.CONTENT,
        failure_category="injected_content_failure",
    )


class Stage2PipelineUnitTests(unittest.TestCase):
    def test_unavailable_physical_observations_are_strict_json_nulls(self):
        rc_rendered = render_template(
            "rc_filter",
            representative_parameters("rc_filter"),
            model_name="JsonSafeRc",
        )
        rc = _validate_rc(
            TEMPLATE_REGISTRY["rc_filter"],
            rc_rendered.parameter_map,
            CsvData(
                columns={"time": (0.0, 1.0), "output_voltage": (0.0, 0.0)},
                row_count=2,
                raw_row_count=2,
            ),
        )
        self.assertIsNone(rc.evidence["tau_relative_error"])
        json.dumps(rc.evidence, allow_nan=False)

        rlc_rendered = render_template(
            "rlc_filter",
            representative_parameters("rlc_filter"),
            model_name="JsonSafeRlc",
        )
        rlc = _validate_rlc(
            TEMPLATE_REGISTRY["rlc_filter"],
            rlc_rendered.parameter_map,
            CsvData(
                columns={
                    "time": (0.0, 0.5, 1.0),
                    "input_signal": (5.0, 5.0, 5.0),
                    "output_voltage": (0.0, 0.0, 0.0),
                },
                row_count=3,
                raw_row_count=3,
            ),
        )
        self.assertIsNone(rlc.evidence["observed_frequency_hz"])
        self.assertIsNone(rlc.evidence["frequency_relative_error"])
        json.dumps(rlc.evidence, allow_nan=False)

    def test_repair_regression_scenarios_are_distinct_offline_injections(self):
        self.assertEqual(
            {item.scenario_id for item in REPAIR_SCENARIOS},
            {
                "modelica_syntax_error",
                "wrong_component_path",
                "port_mismatch",
                "compile_success_wrong_stimulus",
            },
        )
        for scenario in REPAIR_SCENARIOS:
            parameters = representative_parameters(scenario.template_id)
            canonical = render_template(
                scenario.template_id,
                parameters,
                model_name=scenario.model_name,
            )
            injected = scenario.injection(canonical.code, scenario.model_name)
            self.assertNotEqual(injected, canonical.code)
            self.assertIn(f"model {scenario.model_name}", injected)
        physical = next(
            item for item in REPAIR_SCENARIOS
            if item.scenario_id == "compile_success_wrong_stimulus"
        )
        canonical = render_template(
            physical.template_id,
            representative_parameters(physical.template_id),
            model_name=physical.model_name,
        )
        injected = physical.injection(canonical.code, physical.model_name)
        self.assertIn("Sources.StepVoltage", injected)
        self.assertNotIn("Sources.SineVoltage", injected)

    def test_generic_pipeline_binds_loop_evidence_into_run_result(self):
        initial = ArtifactBundle("requirement", "sysml", "bad")

        def validator(artifacts):
            return ValidationSnapshot(
                (_pass(),) if artifacts.modelica == "fixed" else (_fail(),)
            )

        result = run_offline_repair_pipeline(
            case_id="offline_case",
            run_id="offline_run",
            initial_artifacts=initial,
            parameters=(),
            validator=validator,
            repair_provider=lambda _: RepairProposal("fixed"),
        )
        self.assertTrue(result.overall_success)
        self.assertEqual(result.repair_stop_reason, RepairStopReason.ALL_CHECKS_PASS)
        self.assertEqual(result.artifacts["modelica"].content, "fixed")
        self.assertEqual(result.artifacts["modelica"].sha256, sha256_text("fixed"))
        self.assertEqual(result.counters.content_repair_count, 1)
        self.assertEqual(result.counters.accepted_content_repair_count, 1)
        self.assertFalse(result.raw_metrics["online_api_used"])
        self.assertEqual(result.resolved_by, "deterministic_offline_repair")

    def test_all_five_modelica_repairs_are_exact_certified_rerenders(self):
        with tempfile.TemporaryDirectory(prefix="v8-stage2-provider-") as temporary:
            for template_id in TEMPLATE_REGISTRY:
                with self.subTest(template_id=template_id):
                    parameters = representative_parameters(template_id)
                    pipeline = CertifiedTemplateRepairPipeline(
                        template_id=template_id,
                        model_name=f"Repair_{template_id}",
                        parameters=parameters,
                        workspace_root=temporary,
                    )
                    failed = _fail("certified_template_integrity")
                    artifacts = ArtifactBundle(
                        "requirement", pipeline.canonical_sysml, "tampered"
                    )
                    request = RepairRequest(
                        attempt=1,
                        repair_layer=RepairLayer.MODELICA,
                        action=RepairAction.RESTORE_CERTIFIED_TEMPLATE,
                        failed_check=failed,
                        artifacts=artifacts,
                        validation=ValidationSnapshot((failed,)),
                        allowed_scope=(
                            "certified_template_id",
                            "model_name",
                            "normalized_parameter_slots",
                        ),
                        failure_fingerprint=failure_fingerprint(failed),
                    )
                    proposal = pipeline.repair(request)
                    self.assertEqual(proposal.artifact_after, pipeline.canonical_render.code)
                    valid, issues = verify_rendered_template(pipeline.canonical_render)
                    self.assertTrue(valid, issues)

    def test_canonical_sysml_rebuild_passes_real_antlr_syntax_check(self):
        with tempfile.TemporaryDirectory(prefix="v8-stage2-sysml-") as temporary:
            pipeline = CertifiedTemplateRepairPipeline(
                template_id="rc_filter",
                model_name="RepairRc",
                parameters=representative_parameters("rc_filter"),
                workspace_root=temporary,
            )
            syntax, structure = SysMLParserAdapter().inspect(pipeline.canonical_sysml)
        self.assertEqual(syntax.status, CheckStatus.PASS, syntax.issues)
        self.assertIsNotNone(structure)
        system = structure.part_definitions["System"][0]
        self.assertIn("resistor", system.part_usages)

    def test_requirement_repair_stops_instead_of_guessing(self):
        with tempfile.TemporaryDirectory(prefix="v8-stage2-requirement-") as temporary:
            pipeline = CertifiedTemplateRepairPipeline(
                template_id="rc_filter",
                model_name="RepairRc",
                parameters=representative_parameters("rc_filter"),
                workspace_root=temporary,
            )
            failed = _fail("requirement_normalization")
            request = RepairRequest(
                attempt=1,
                repair_layer=RepairLayer.REQUIREMENT,
                action=RepairAction.UPSTREAM_REQUIREMENT_REDO,
                failed_check=failed,
                artifacts=ArtifactBundle("", pipeline.canonical_sysml, pipeline.canonical_render.code),
                validation=ValidationSnapshot((failed,)),
                allowed_scope=("requirement_text", "normalized_parameters"),
                failure_fingerprint=failure_fingerprint(failed),
            )
            with self.assertRaises(RepairProviderStop) as caught:
                pipeline.repair(request)
        self.assertEqual(caught.exception.reason, RepairStopReason.UPSTREAM_REQUIRED)

    def test_validator_executes_exact_candidate_hash(self):
        class FakeTool:
            version = "fake-omc"

            def __init__(self):
                self.rendered = None

            def execute_rendered(self, rendered, *, workspace_root):
                self.rendered = rendered
                return SimpleNamespace(
                    run_directory=str(Path(workspace_root) / "unique"),
                    source_sha256=rendered.rendered_sha256,
                    csv_sha256="",
                )

        fake_tool = FakeTool()
        with tempfile.TemporaryDirectory(prefix="v8-stage2-exact-") as temporary:
            pipeline = CertifiedTemplateRepairPipeline(
                template_id="rc_filter",
                model_name="ExactCandidate",
                parameters=representative_parameters("rc_filter"),
                workspace_root=temporary,
                tool=fake_tool,
            )
            candidate = "model ExactCandidate end ExactCandidate;"
            artifacts = ArtifactBundle("requirement", pipeline.canonical_sysml, candidate)
            with patch(
                "week9.src.pipeline.run_hard_gate",
                return_value=SimpleNamespace(checks=(_pass(),)),
            ) as gate:
                snapshot = pipeline.validate(artifacts)
        self.assertEqual(fake_tool.rendered.code, candidate)
        self.assertEqual(fake_tool.rendered.rendered_sha256, sha256_text(candidate))
        self.assertEqual(snapshot.payload["source_sha256"], sha256_text(candidate))
        self.assertIs(gate.call_args.kwargs["rendered"], fake_tool.rendered)


@unittest.skipUnless(resolve_omc_path(), "OpenModelica is not installed")
class Stage2PipelineOpenModelicaTests(unittest.TestCase):
    def test_real_syntax_injection_is_restored_and_revalidated(self):
        with tempfile.TemporaryDirectory(prefix="v8-stage2-real-") as temporary:
            pipeline = CertifiedTemplateRepairPipeline(
                template_id="rc_filter",
                model_name="Stage2RealRc",
                parameters=representative_parameters("rc_filter"),
                workspace_root=temporary,
            )
            broken = pipeline.canonical_render.code.replace(
                "end Stage2RealRc;", "end Stage2RealRc"
            )
            self.assertNotEqual(broken, pipeline.canonical_render.code)
            result = pipeline.run(
                case_id="stage2_syntax_injection",
                run_id="offline_real",
                initial_artifacts=ArtifactBundle(
                    "repair an injected syntax error",
                    pipeline.canonical_sysml,
                    broken,
                ),
                config=RepairLoopConfig(
                    max_repairs=2,
                    max_content_repairs=2,
                    max_upstream_redos=1,
                    max_infrastructure_retries=1,
                ),
            )
        self.assertTrue(result.overall_success, result.primary_failure_evidence)
        self.assertEqual(result.artifacts["modelica"].content, pipeline.canonical_render.code)
        self.assertEqual(result.counters.content_repair_count, 1)
        self.assertEqual(result.counters.accepted_content_repair_count, 1)
        self.assertTrue(all(check.status is CheckStatus.PASS for check in result.checks))


if __name__ == "__main__":
    unittest.main()
