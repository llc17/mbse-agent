from __future__ import annotations

from dataclasses import replace
import csv
import hashlib
import math
from pathlib import Path
import tempfile
import unittest

from week9.src.hard_gate import (
    PhysicalValidation,
    SysMLParserAdapter,
    check_physical_behavior,
    run_hard_gate,
)
from week9.src.modelica_templates import (
    get_template_spec,
    render_template,
    representative_parameters,
    simulation_plan,
)
from week9.src.modelica_tool import CsvData, ModelicaExecution, read_simulation_csv
from week9.src.schemas import (
    CheckStatus,
    FailureKind,
    ParameterStatus,
    QualityCheckResult,
)


_FIXTURE_ROOT: tempfile.TemporaryDirectory[str] | None = None


def setUpModule():
    global _FIXTURE_ROOT
    _FIXTURE_ROOT = tempfile.TemporaryDirectory(prefix="v8-hard-gate-fixtures-")


def tearDownModule():
    global _FIXTURE_ROOT
    if _FIXTURE_ROOT is not None:
        _FIXTURE_ROOT.cleanup()
        _FIXTURE_ROOT = None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pass_check(name: str, evidence=None) -> QualityCheckResult:
    return QualityCheckResult(
        check_type=name,
        status=CheckStatus.PASS,
        required=True,
        evidence=dict(evidence or {}),
        tool_name="test fixture",
    )


def _rc_fixture():
    if _FIXTURE_ROOT is None:
        raise RuntimeError("hard-gate fixture module was not initialized")
    template_id = "rc_filter"
    parameters = representative_parameters(template_id)
    rendered = render_template(template_id, parameters, model_name="PureGateRC")
    spec = get_template_spec(template_id)
    times = tuple(index * spec.simulation_stop_time / 1000 for index in range(1001))
    tau = rendered.parameter_map["resistance"] * rendered.parameter_map["capacitance"]
    voltage = rendered.parameter_map["input_voltage"]
    outputs = tuple(voltage * (1.0 - math.exp(-time / tau)) for time in times)
    run_dir = Path(tempfile.mkdtemp(prefix="rc-", dir=_FIXTURE_ROOT.name))
    source_path = run_dir / "fixture.mo"
    source_path.write_text(rendered.code, encoding="utf-8", newline="\n")
    executable_path = run_dir / "fixture.exe"
    executable_path.write_bytes(b"V8 deterministic fixture executable")
    csv_path = run_dir / "fixture.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("time", "output_voltage"))
        writer.writerows(zip(times, outputs))
    data = read_simulation_csv(csv_path, ("time", "output_voltage"))
    plan = simulation_plan(template_id, rendered.parameter_map)
    execution = ModelicaExecution(
        run_directory=str(run_dir),
        source_path=str(source_path),
        executable_path=str(executable_path),
        csv_path=str(csv_path),
        compile_check=_pass_check(
            "modelica_compile",
            {
                "source_sha256": _sha256(source_path),
                "executable_sha256": _sha256(executable_path),
            },
        ),
        simulation_check=_pass_check(
            "simulation", {"csv_sha256": _sha256(csv_path)}
        ),
        csv_data=data,
        template_id=template_id,
        model_name=rendered.model_name,
        source_sha256=_sha256(source_path),
        csv_sha256=_sha256(csv_path),
        simulation_start_time=plan.start_time,
        simulation_stop_time=plan.stop_time,
        simulation_intervals=plan.intervals,
    )
    attributes = " ".join(
        f"attribute {item.canonical_name} : Real = {item.value};"
        for item in parameters
    )
    parts = " ".join(f"part {name};" for name in spec.sysml_components)
    connections = " ".join(
        f"connect {left} to {right};" for left, right in spec.sysml_connections
    )
    sysml = (
        f"package PureGate {{ part def System {{ {attributes} {parts} {connections} }} "
        "part system : System; }"
    )
    return template_id, parameters, rendered, execution, sysml


class SysMLHardSyntaxTests(unittest.TestCase):
    def test_real_antlr_parser_accepts_valid_minimum_structure(self):
        check, structure = SysMLParserAdapter().inspect(
            "package P { part def A; part a : A; }"
        )
        self.assertEqual(check.status, CheckStatus.PASS)
        self.assertIsNotNone(structure)
        self.assertEqual(check.evidence["semantic_claim"], "not_made")

    def test_unknown_import_is_not_misreported_as_semantically_valid(self):
        check, _ = SysMLParserAdapter().inspect(
            "package P { private import DefinitelyMissing::*; part def A; }"
        )
        self.assertEqual(check.status, CheckStatus.PASS)
        self.assertEqual(check.evidence["semantic_claim"], "not_made")

    def test_real_antlr_parser_rejects_syntax_error(self):
        check, structure = SysMLParserAdapter().inspect(
            "package P { part def MissingSemicolon }"
        )
        self.assertEqual(check.status, CheckStatus.FAIL)
        self.assertEqual(check.failure_kind, FailureKind.CONTENT)
        self.assertIsNone(structure)

    def test_missing_sysml_is_unverified(self):
        check, _ = SysMLParserAdapter().inspect("   ")
        self.assertEqual(check.status, CheckStatus.UNVERIFIED)

    def test_parser_self_test_failure_is_infrastructure_error(self):
        adapter = SysMLParserAdapter(parser=lambda source: object())
        check, _ = adapter.inspect("package P { part def A; }")
        self.assertEqual(check.status, CheckStatus.ERROR)
        self.assertEqual(check.failure_kind, FailureKind.INFRASTRUCTURE)


class CompleteHardGateTests(unittest.TestCase):
    def test_correct_sample_is_accepted(self):
        template_id, parameters, rendered, execution, sysml = _rc_fixture()
        result = run_hard_gate(
            template_id=template_id,
            parameters=parameters,
            sysml_source=sysml,
            rendered=rendered,
            execution=execution,
            recertification_mode=True,
        )
        self.assertTrue(result.overall_success)
        self.assertTrue(all(check.status is CheckStatus.PASS for check in result.checks))
        anchor = next(
            check for check in result.checks
            if check.check_type == "template_certification_anchor"
        )
        self.assertEqual(anchor.evidence["mode"], "recertification_candidate")
        self.assertTrue(anchor.evidence["not_release_authorization"])

    def test_missing_execution_cannot_be_released(self):
        template_id, parameters, rendered, _, sysml = _rc_fixture()
        result = run_hard_gate(
            template_id=template_id,
            parameters=parameters,
            sysml_source=sysml,
            rendered=rendered,
            execution=None,
        )
        self.assertFalse(result.overall_success)
        statuses = {check.check_type: check.status for check in result.checks}
        self.assertEqual(statuses["modelica_compile"], CheckStatus.UNVERIFIED)
        self.assertEqual(statuses["simulation"], CheckStatus.UNVERIFIED)
        self.assertEqual(statuses["physical_behavior"], CheckStatus.UNVERIFIED)

    def test_unconstrained_rewrite_is_blocked_even_if_other_evidence_passes(self):
        template_id, parameters, rendered, execution, sysml = _rc_fixture()
        tampered = replace(rendered, code=rendered.code + "// arbitrary rewrite\n")
        result = run_hard_gate(
            template_id=template_id,
            parameters=parameters,
            sysml_source=sysml,
            rendered=tampered,
            execution=execution,
        )
        integrity = next(
            check for check in result.checks if check.check_type == "certified_template_integrity"
        )
        self.assertEqual(integrity.status, CheckStatus.FAIL)
        self.assertFalse(result.overall_success)

    def test_validator_exception_is_error_not_pass(self):
        template_id, parameters, rendered, execution, sysml = _rc_fixture()

        def explode(spec, values, data):
            raise RuntimeError("injected validator crash")

        result = run_hard_gate(
            template_id=template_id,
            parameters=parameters,
            sysml_source=sysml,
            rendered=rendered,
            execution=execution,
            physical_validator=explode,
        )
        physics = next(
            check for check in result.checks if check.check_type == "physical_behavior"
        )
        self.assertEqual(physics.status, CheckStatus.ERROR)
        self.assertEqual(physics.failure_kind, FailureKind.INFRASTRUCTURE)
        self.assertFalse(result.overall_success)

    def test_cross_model_parameter_mismatch_is_blocked(self):
        template_id, parameters, rendered, execution, sysml = _rc_fixture()
        sysml = sysml.replace("resistance : Real = 1000.0", "resistance : Real = 2000.0")
        result = run_hard_gate(
            template_id=template_id,
            parameters=parameters,
            sysml_source=sysml,
            rendered=rendered,
            execution=execution,
        )
        cross = next(
            check for check in result.checks if check.check_type == "cross_model_consistency"
        )
        self.assertEqual(cross.status, CheckStatus.FAIL)
        self.assertFalse(result.overall_success)

    def test_execution_hash_cannot_be_swapped_or_forged(self):
        template_id, parameters, rendered, execution, sysml = _rc_fixture()
        forged = replace(execution, source_sha256="0" * 64)
        result = run_hard_gate(
            template_id=template_id,
            parameters=parameters,
            sysml_source=sysml,
            rendered=rendered,
            execution=forged,
        )
        provenance = next(
            check for check in result.checks if check.check_type == "execution_provenance"
        )
        self.assertEqual(provenance.status, CheckStatus.FAIL)
        self.assertFalse(result.overall_success)

    def test_nonexistent_execution_artifact_cannot_be_released(self):
        template_id, parameters, rendered, execution, sysml = _rc_fixture()
        stale = replace(execution, source_path=str(Path(execution.run_directory) / "gone.mo"))
        result = run_hard_gate(
            template_id=template_id,
            parameters=parameters,
            sysml_source=sysml,
            rendered=rendered,
            execution=stale,
        )
        provenance = next(
            check for check in result.checks if check.check_type == "execution_provenance"
        )
        self.assertEqual(provenance.status, CheckStatus.UNVERIFIED)
        self.assertFalse(result.overall_success)

    def test_missing_component_is_blocked(self):
        template_id, parameters, rendered, execution, sysml = _rc_fixture()
        sysml = sysml.replace("part capacitor;", "")
        result = run_hard_gate(
            template_id=template_id,
            parameters=parameters,
            sysml_source=sysml,
            rendered=rendered,
            execution=execution,
        )
        cross = next(
            check for check in result.checks if check.check_type == "cross_model_consistency"
        )
        self.assertEqual(cross.status, CheckStatus.FAIL)

    def test_wrong_connection_is_blocked(self):
        template_id, parameters, rendered, execution, sysml = _rc_fixture()
        sysml = sysml.replace(
            "connect source to resistor;", "connect source to capacitor;"
        )
        result = run_hard_gate(
            template_id=template_id,
            parameters=parameters,
            sysml_source=sysml,
            rendered=rendered,
            execution=execution,
        )
        cross = next(
            check for check in result.checks if check.check_type == "cross_model_consistency"
        )
        self.assertEqual(cross.status, CheckStatus.FAIL)

    def test_correct_names_in_unrelated_scope_do_not_spoof_cross_check(self):
        template_id, parameters, rendered, execution, sysml = _rc_fixture()
        sysml = sysml.replace("part def System", "part def Decoy").replace(
            "part system : System;",
            "part system : Decoy; part def System { part placeholder; }",
        )
        result = run_hard_gate(
            template_id=template_id,
            parameters=parameters,
            sysml_source=sysml,
            rendered=rendered,
            execution=execution,
        )
        cross = next(
            check for check in result.checks if check.check_type == "cross_model_consistency"
        )
        self.assertNotEqual(cross.status, CheckStatus.PASS)

    def test_non_known_requirement_parameter_is_blocked(self):
        template_id, parameters, rendered, execution, sysml = _rc_fixture()
        parameters[0].status = ParameterStatus.MISSING
        result = run_hard_gate(
            template_id=template_id,
            parameters=parameters,
            sysml_source=sysml,
            rendered=rendered,
            execution=execution,
        )
        requirement = next(
            check for check in result.checks if check.check_type == "requirement_normalization"
        )
        self.assertEqual(requirement.status, CheckStatus.FAIL)
        self.assertFalse(result.overall_success)


class PhysicalNegativeTests(unittest.TestCase):
    def _check(self, template_id: str, columns) -> QualityCheckResult:
        parameters = representative_parameters(template_id)
        rendered = render_template(
            template_id, parameters, model_name=f"BadPhysics_{template_id}"
        )
        row_count = len(columns["time"])
        execution = ModelicaExecution(
            run_directory="injected",
            source_path="injected.mo",
            executable_path="injected.exe",
            csv_path="injected.csv",
            compile_check=_pass_check("modelica_compile"),
            simulation_check=_pass_check("simulation"),
            csv_data=CsvData(
                columns={name: tuple(values) for name, values in columns.items()},
                row_count=row_count,
                raw_row_count=row_count,
            ),
        )
        return check_physical_behavior(
            get_template_spec(template_id), rendered, execution
        )

    def test_each_template_rejects_wrong_physical_trace(self):
        cases = {
            "rc_filter": {
                "time": (0.0, 0.001, 0.002),
                "output_voltage": (1.0, 5.0, 4.0),
            },
            "rlc_filter": {
                "time": (0.0, 0.001, 0.002, 0.003),
                "input_signal": (0.0, 0.0, 0.0, 0.0),
                "output_voltage": (0.0, 0.0, 0.0, 0.0),
            },
            "opamp_inverting": {
                "time": (0.0, 0.01),
                "output_voltage": (5.0, 5.0),
            },
            "thermal_single_room": {
                "time": (0.0, 100000.0),
                "room_temperature": (308.15, 308.15),
            },
            "thermal_dual_room": {
                "time": (0.0, 100000.0),
                "room_a_temperature": (308.15, 308.15),
                "room_b_temperature": (308.15, 308.15),
            },
        }
        for template_id, columns in cases.items():
            with self.subTest(template_id=template_id):
                check = self._check(template_id, columns)
                self.assertEqual(check.status, CheckStatus.FAIL, check.evidence)


if __name__ == "__main__":
    unittest.main()
