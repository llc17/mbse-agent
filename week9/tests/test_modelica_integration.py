from __future__ import annotations

import math
import tempfile
import unittest

from week9.src.hard_gate import run_hard_gate
from week9.src.modelica_templates import (
    TEMPLATE_REGISTRY,
    get_template_spec,
    render_template,
    representative_parameters,
)
from week9.src.modelica_tool import OpenModelicaTool, resolve_omc_path
from week9.src.schemas import CheckStatus, FailureKind


def _certification_sysml(template_id, parameters) -> str:
    spec = get_template_spec(template_id)
    attributes = " ".join(
        f"attribute {item.canonical_name} : Real = {item.value};"
        for item in parameters
    )
    parts = " ".join(f"part {name};" for name in spec.sysml_components)
    connections = " ".join(
        f"connect {left} to {right};" for left, right in spec.sysml_connections
    )
    return (
        f"package Certification_{template_id} {{ part def System {{ "
        f"{attributes} {parts} {connections} }} part system : System; }}"
    )


@unittest.skipUnless(resolve_omc_path(), "OpenModelica is not installed")
class OpenModelicaCertificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temporary = tempfile.TemporaryDirectory(prefix="v8-omc-cert-")
        cls.tool = OpenModelicaTool()
        cls.executions = {}
        cls.rendered = {}
        cls.parameters = {}
        for template_id in TEMPLATE_REGISTRY:
            parameters = representative_parameters(template_id)
            rendered = render_template(
                template_id,
                parameters,
                model_name=f"Certified_{template_id}",
            )
            execution = cls.tool.execute_rendered(
                rendered, workspace_root=cls._temporary.name
            )
            cls.parameters[template_id] = parameters
            cls.rendered[template_id] = rendered
            cls.executions[template_id] = execution

    @classmethod
    def tearDownClass(cls):
        cls._temporary.cleanup()

    def test_all_five_templates_compile_and_simulate(self):
        for template_id, execution in self.executions.items():
            with self.subTest(template_id=template_id):
                self.assertEqual(
                    execution.compile_check.status,
                    CheckStatus.PASS,
                    execution.compile_check.issues,
                )
                self.assertEqual(
                    execution.simulation_check.status,
                    CheckStatus.PASS,
                    execution.simulation_check.issues,
                )
                self.assertIsNotNone(execution.csv_data)

    def test_all_five_correct_samples_pass_complete_gate(self):
        required_checks = {
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
        }
        for template_id in TEMPLATE_REGISTRY:
            with self.subTest(template_id=template_id):
                execution = self.executions[template_id]
                rendered = self.rendered[template_id]
                result = run_hard_gate(
                    template_id=template_id,
                    parameters=self.parameters[template_id],
                    sysml_source=_certification_sysml(
                        template_id, self.parameters[template_id]
                    ),
                    rendered=rendered,
                    execution=execution,
                )
                self.assertTrue(result.overall_success, result.decision.failure_reasons)
                self.assertEqual(
                    {check.check_type for check in result.checks}, required_checks
                )
                self.assertTrue(
                    all(check.status is CheckStatus.PASS for check in result.checks)
                )
                self.assertEqual(execution.template_id, template_id)
                self.assertEqual(execution.model_name, rendered.model_name)
                self.assertEqual(execution.source_sha256, rendered.rendered_sha256)
                self.assertEqual(
                    execution.compile_check.evidence["source_sha256"],
                    rendered.rendered_sha256,
                )
                self.assertEqual(
                    execution.simulation_check.evidence["csv_sha256"],
                    execution.csv_sha256,
                )

    def test_known_hard_compile_errors_are_never_released(self):
        injected = []

        rc = self.rendered["rc_filter"]
        syntax_source = rc.code.replace(f"end {rc.model_name};", f"end {rc.model_name}")
        injected.append(
            (
                "rc_filter",
                self.tool.execute_source(
                syntax_source,
                model_name=rc.model_name,
                template_spec=get_template_spec("rc_filter"),
                workspace_root=self._temporary.name,
                ),
            )
        )

        thermal = self.rendered["thermal_single_room"]
        wrong_path_source = thermal.code.replace(
            "HeatTransfer.Sources.FixedTemperature",
            "HeatTransfer.Components.FixedTemperature",
        )
        injected.append(
            (
                "thermal_single_room",
                self.tool.execute_source(
                wrong_path_source,
                model_name=thermal.model_name,
                template_spec=get_template_spec("thermal_single_room"),
                workspace_root=self._temporary.name,
                ),
            )
        )

        port_model_name = "InjectedPortMismatch"
        port_source = self.rendered["rc_filter"].code.replace(
            self.rendered["rc_filter"].model_name, port_model_name
        ).replace("connect(source.p, resistor.p);", "connect(source.p, resistor.R);")
        injected.append(
            (
                "rc_filter",
                self.tool.execute_source(
                port_source,
                model_name=port_model_name,
                template_spec=get_template_spec("rc_filter"),
                workspace_root=self._temporary.name,
                ),
            )
        )

        false_releases = 0
        for template_id, execution in injected:
            if execution.compile_check.status is CheckStatus.PASS:
                false_releases += 1
            self.assertEqual(execution.compile_check.status, CheckStatus.FAIL)
            self.assertEqual(execution.compile_check.failure_kind, FailureKind.CONTENT)
            self.assertNotEqual(execution.simulation_check.status, CheckStatus.PASS)
            gate = run_hard_gate(
                template_id=template_id,
                parameters=self.parameters[template_id],
                sysml_source=_certification_sysml(
                    template_id, self.parameters[template_id]
                ),
                rendered=self.rendered[template_id],
                execution=execution,
            )
            self.assertFalse(gate.overall_success)
        self.assertEqual(false_releases, 0)

    def test_low_frequency_rc_uses_dynamic_window_and_passes_full_gate(self):
        template_id = "rc_filter"
        parameters = representative_parameters(template_id)
        values = {item.canonical_name: item for item in parameters}
        values["cutoff_frequency"].value = 1.0
        values["capacitance"].value = 1.0 / (
            2.0 * math.pi * values["resistance"].value
        )
        rendered = render_template(
            template_id, parameters, model_name="Certified_rc_filter_1Hz"
        )
        execution = self.tool.execute_rendered(
            rendered, workspace_root=self._temporary.name
        )
        self.assertGreater(execution.simulation_stop_time, 1.0)
        result = run_hard_gate(
            template_id=template_id,
            parameters=parameters,
            sysml_source=_certification_sysml(template_id, parameters),
            rendered=rendered,
            execution=execution,
        )
        self.assertTrue(result.overall_success, result.decision.failure_reasons)

    def test_compile_successful_rlc_step_source_is_rejected_by_physics(self):
        template_id = "rlc_filter"
        rendered = self.rendered[template_id]
        wrong_source = rendered.code.replace(
            """Modelica.Electrical.Analog.Sources.SineVoltage source(
    V=input_voltage,
    f=cutoff_frequency,
    phase=0,
    offset=0,
    startTime=0);""",
            """Modelica.Electrical.Analog.Sources.StepVoltage source(
    V=input_voltage,
    startTime=0);""",
        )
        self.assertNotEqual(wrong_source, rendered.code)
        execution = self.tool.execute_source(
            wrong_source,
            model_name=rendered.model_name,
            template_spec=get_template_spec(template_id),
            workspace_root=self._temporary.name,
        )
        self.assertEqual(execution.compile_check.status, CheckStatus.PASS)
        self.assertEqual(execution.simulation_check.status, CheckStatus.PASS)
        result = run_hard_gate(
            template_id=template_id,
            parameters=self.parameters[template_id],
            sysml_source=_certification_sysml(
                template_id, self.parameters[template_id]
            ),
            rendered=rendered,
            execution=execution,
        )
        physics = next(
            check for check in result.checks
            if check.check_type == "physical_behavior"
        )
        self.assertEqual(physics.status, CheckStatus.FAIL)
        self.assertFalse(result.overall_success)


if __name__ == "__main__":
    unittest.main()
