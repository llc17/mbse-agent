from __future__ import annotations

import unittest

from week9.src.diagnostics import classify_modelica_diagnostics


class DiagnosticClassificationTests(unittest.TestCase):
    def test_exact_modelica_class_path_is_unknown_class(self):
        result = classify_modelica_diagnostics(
            "Error: Class Modelica.Thermal.HeatTransfer.Components.FixedTemperature "
            "not found in scope BrokenThermal."
        )
        self.assertEqual(result.subcategory, "unknown_class_or_library")
        self.assertEqual(
            result.symbols,
            ("Modelica.Thermal.HeatTransfer.Components.FixedTemperature",),
        )
        self.assertTrue(result.retrievable)

    def test_invalid_connector_is_connector_mismatch(self):
        result = classify_modelica_diagnostics(
            "Error: resistor.R is not a valid connector."
        )
        self.assertEqual(result.subcategory, "connector_type_mismatch")
        self.assertEqual(result.symbols, ("resistor.R",))

    def test_unknown_parameter_and_port_are_separate(self):
        parameter = classify_modelica_diagnostics(
            "Error: Component source has no parameter named freqHz"
        )
        port = classify_modelica_diagnostics(
            "Error: Variable source.positive not found in scope Probe"
        )
        self.assertEqual(parameter.subcategory, "unknown_component_parameter")
        self.assertEqual(parameter.symbols, ("freqHz",))
        self.assertEqual(port.subcategory, "unknown_port_or_member")
        self.assertEqual(port.symbols, ("source.positive",))

    def test_syntax_and_equation_balance_are_not_retrievable(self):
        syntax = classify_modelica_diagnostics("Parser error: syntax error near equation")
        balance = classify_modelica_diagnostics(
            "Error: Too many equations, over-determined system"
        )
        self.assertEqual(syntax.subcategory, "syntax_error")
        self.assertEqual(balance.subcategory, "equation_balance_error")
        self.assertFalse(syntax.retrievable)
        self.assertFalse(balance.retrievable)

    def test_ambiguous_or_success_output_fails_closed(self):
        ambiguous = classify_modelica_diagnostics(
            "Error: Failed to load package Probe using MODELICAPATH C:/libraries"
        )
        success = classify_modelica_diagnostics("Check of Probe completed successfully")
        self.assertEqual(ambiguous.subcategory, "unknown_diagnostic")
        self.assertEqual(success.subcategory, "none")
        self.assertFalse(ambiguous.retrievable)
        self.assertFalse(success.retrievable)


if __name__ == "__main__":
    unittest.main()
