from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from week9.experiments.cases import DEVELOPMENT_REGRESSION_CASES
from week9.experiments.cross_version_validation import externally_validate
from week9.src.generation import DeterministicNaturalLanguageProvider
from week9.src.modelica_templates import render_template
from week9.src.modelica_tool import CsvData, ModelicaExecution, resolve_omc_path
from week9.src.parameter_normalization import normalize_parameters
from week9.src.pipeline import render_canonical_sysml
from week9.src.schemas import (
    ArtifactRecord,
    CheckStatus,
    FailureKind,
    ModelConfiguration,
    QualityCheckResult,
    RepairAction,
    RepairLayer,
    RepairOutcome,
    RepairRecord,
    RepairStopReason,
    RunCounters,
    RunResult,
)


def _pass(check_type: str) -> QualityCheckResult:
    return QualityCheckResult(
        check_type=check_type,
        status=CheckStatus.PASS,
        required=True,
        evidence={"executed": True},
    )


class _FakeModelicaTool:
    version = "fake-omc"

    def __init__(self, csv_path: Path):
        self.csv_path = csv_path

    def execute_source(self, source, *, model_name, template_spec, workspace_root, parameter_values):
        return ModelicaExecution(
            run_directory=str(Path(workspace_root)),
            source_path=str(Path(workspace_root) / "model.mo"),
            executable_path=str(Path(workspace_root) / "model.exe"),
            csv_path=str(self.csv_path),
            compile_check=_pass("modelica_compile"),
            simulation_check=_pass("simulation"),
            csv_data=CsvData(
                columns={
                    "time": (0.0, 0.001, 0.01),
                    "capacitor.v": (0.0, 4.2, 5.0),
                },
                row_count=3,
                raw_row_count=3,
            ),
            template_id=template_spec.template_id,
            model_name=model_name,
        )


class CrossVersionValidationTests(unittest.TestCase):
    def setUp(self):
        self.case = DEVELOPMENT_REGRESSION_CASES["rc_lowpass"]
        generation = DeterministicNaturalLanguageProvider().generate(self.case.raw_input)
        parameters = normalize_parameters(
            generation.template_id, generation.observations
        ).parameters
        self.rendered = render_template(
            generation.template_id, parameters, model_name="ExternalRC"
        )
        self.sysml = render_canonical_sysml(self.rendered)

    def _candidate(self, modelica: str, *, internal_success: bool = False) -> RunResult:
        checks = [_pass("adapter.execution")]
        if internal_success:
            checks.append(_pass("legacy.claimed_success"))
        return RunResult(
            version="v6",
            case_id=self.case.case_id,
            run_id="external-test",
            requirement_text=self.case.raw_input,
            artifacts={
                "sysml": ArtifactRecord("sysml", content=self.sysml),
                "modelica": ArtifactRecord("modelica", content=modelica),
            },
            checks=checks,
            counters=RunCounters(generation_count=1),
            model_configuration=ModelConfiguration(
                provider="deepseek", model="test-model"
            ),
            overall_success=internal_success,
        )

    def test_canonical_artifacts_pass_external_rules(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "simulation.csv"
            csv_path.write_text("time,capacitor.v\n0,0\n0.01,5\n", encoding="utf-8")
            result = externally_validate(
                self._candidate(self.rendered.code),
                self.case,
                workspace_root=Path(temp_dir) / "work",
                modelica_tool=_FakeModelicaTool(csv_path),
            )
        self.assertTrue(
            result.overall_success,
            [(item.check_type, item.status.value, item.issues) for item in result.checks],
        )
        self.assertEqual(result.raw_metrics["candidate_internal_overall_success"], False)
        required = {item.check_type: item.status for item in result.checks if item.required}
        for name in self.case.required_checks:
            self.assertEqual(required[name], CheckStatus.PASS)

    def test_wrong_stimulus_is_rejected_even_if_candidate_claimed_success(self):
        broken = self.rendered.code.replace("StepVoltage", "SineVoltage")
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "simulation.csv"
            csv_path.write_text("time,capacitor.v\n0,0\n0.01,5\n", encoding="utf-8")
            result = externally_validate(
                self._candidate(broken, internal_success=True),
                self.case,
                workspace_root=Path(temp_dir) / "work",
                modelica_tool=_FakeModelicaTool(csv_path),
            )
        self.assertFalse(result.overall_success)
        checks = {item.check_type: item for item in result.checks}
        self.assertEqual(
            checks["parameter_unit_consistency"].status, CheckStatus.FAIL
        )

    def test_v8_audit_records_are_preserved_during_external_rebuild(self):
        modelica = ArtifactRecord("modelica", content=self.rendered.code)
        fingerprint = "a" * 64
        repair = RepairRecord(
            attempt=1,
            failed_check="modelica_compile",
            failure_kind=FailureKind.CONTENT,
            error_category="modelica_build_failed",
            repair_layer=RepairLayer.MODELICA,
            action=RepairAction.RESTORE_CERTIFIED_TEMPLATE,
            artifact_type="modelica",
            failure_fingerprint_before=fingerprint,
            previous_actions=[],
            original_evidence={"injected": True},
            allowed_scope=["certified_template_rerender"],
            artifact_before=self.rendered.code,
            artifact_after=self.rendered.code,
            before_sha256=modelica.sha256,
            after_sha256=modelica.sha256,
            diff="",
            required_revalidation_checks=[],
            failure_fingerprint_after=fingerprint,
            effective_after_sha256=modelica.sha256,
            outcome=RepairOutcome.NO_OP,
            stop_reason=RepairStopReason.NO_OP,
        )
        candidate = RunResult(
            version="v8",
            case_id=self.case.case_id,
            run_id="v8-audit-forwarding",
            requirement_text=self.case.raw_input,
            artifacts={
                "requirement": ArtifactRecord(
                    "requirement", content=self.case.raw_input
                ),
                "sysml": ArtifactRecord("sysml", content=self.sysml),
                "modelica": modelica,
            },
            checks=[
                _pass("adapter.execution"),
                QualityCheckResult(
                    check_type="modelica_compile",
                    status=CheckStatus.FAIL,
                    required=True,
                    issues=["injected transient build failure"],
                    evidence={"injected": True},
                    failure_kind=FailureKind.CONTENT,
                    failure_category="modelica_build_failed",
                ),
            ],
            repairs=[repair],
            repair_stop_reason=RepairStopReason.NO_OP,
            counters=RunCounters(
                generation_count=1,
                validation_count=1,
                repair_attempt_count=1,
                no_op_count=1,
            ),
            model_configuration=ModelConfiguration(
                provider="deterministic_local", model="test-model"
            ),
            overall_success=False,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "simulation.csv"
            csv_path.write_text("time,capacitor.v\n0,0\n0.01,5\n", encoding="utf-8")
            result = externally_validate(
                candidate,
                self.case,
                workspace_root=Path(temp_dir) / "work",
                modelica_tool=_FakeModelicaTool(csv_path),
            )
        self.assertTrue(result.overall_success)
        self.assertEqual(result.counters.repair_attempt_count, 1)
        self.assertEqual(result.counters.no_op_count, 1)
        self.assertEqual(result.repairs, [repair])

    @unittest.skipUnless(resolve_omc_path() is not None, "OpenModelica is unavailable")
    def test_all_development_topologies_pass_real_external_reexecution(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            for case_id in ("rc_lowpass", "rlc_lowpass", "dual_room_thermal"):
                with self.subTest(case_id=case_id):
                    case = DEVELOPMENT_REGRESSION_CASES[case_id]
                    generation = DeterministicNaturalLanguageProvider().generate(
                        case.raw_input
                    )
                    parameters = normalize_parameters(
                        generation.template_id, generation.observations
                    ).parameters
                    rendered = render_template(
                        generation.template_id,
                        parameters,
                        model_name="External_" + case_id,
                    )
                    candidate = RunResult(
                        version="v6",
                        case_id=case.case_id,
                        run_id="real-" + case_id,
                        requirement_text=case.raw_input,
                        artifacts={
                            "sysml": ArtifactRecord(
                                "sysml", content=render_canonical_sysml(rendered)
                            ),
                            "modelica": ArtifactRecord(
                                "modelica", content=rendered.code
                            ),
                        },
                        checks=[_pass("adapter.execution")],
                        counters=RunCounters(generation_count=1),
                        model_configuration=ModelConfiguration(
                            provider="deepseek", model="test-model"
                        ),
                        raw_metrics={"template_id": generation.template_id},
                    )
                    result = externally_validate(
                        candidate,
                        case,
                        workspace_root=Path(temp_dir) / case_id,
                    )
                    self.assertTrue(
                        result.overall_success,
                        [
                            (item.check_type, item.status.value, item.issues)
                            for item in result.checks
                            if item.required
                        ],
                    )


if __name__ == "__main__":
    unittest.main()
