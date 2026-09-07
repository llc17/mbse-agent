from __future__ import annotations

from dataclasses import replace
import unittest

from week9.src.modelica_templates import (
    TEMPLATE_REGISTRY,
    TemplateInputError,
    canonicalize_parameters,
    render_template,
    representative_parameters,
    verify_rendered_template,
)
from week9.src.schemas import NormalizedParameter, ParameterStatus


class CertifiedTemplateTests(unittest.TestCase):
    def test_exactly_five_stage1_templates_are_registered(self):
        self.assertEqual(
            set(TEMPLATE_REGISTRY),
            {
                "rc_filter",
                "rlc_filter",
                "opamp_inverting",
                "thermal_single_room",
                "thermal_dual_room",
            },
        )

    def test_representative_matrix_renders_reproducibly(self):
        for template_id in TEMPLATE_REGISTRY:
            with self.subTest(template_id=template_id):
                rendered = render_template(
                    template_id,
                    representative_parameters(template_id),
                    model_name=f"Certified_{template_id}",
                )
                valid, issues = verify_rendered_template(rendered)
                self.assertTrue(valid, issues)
                self.assertNotIn("$MODEL_NAME", rendered.code)
                self.assertNotRegex(rendered.code, r"\$[A-Za-z_]")

    def test_missing_parameter_is_rejected_instead_of_defaulted(self):
        parameters = representative_parameters("rc_filter")[:-1]
        with self.assertRaisesRegex(TemplateInputError, "missing parameter: input_voltage"):
            render_template("rc_filter", parameters, model_name="MissingValue")

    def test_unknown_parameter_is_rejected(self):
        parameters = representative_parameters("rc_filter")
        parameters.append(
            NormalizedParameter(
                canonical_name="invented_default",
                value=1.0,
                unit="V",
                source_text="test injection",
                status=ParameterStatus.KNOWN,
            )
        )
        with self.assertRaisesRegex(TemplateInputError, "unknown template parameter"):
            render_template("rc_filter", parameters, model_name="UnknownValue")

    def test_non_known_status_is_rejected(self):
        parameters = representative_parameters("rc_filter")
        parameters[0].status = ParameterStatus.CONFLICT
        with self.assertRaisesRegex(TemplateInputError, "non-known status"):
            canonicalize_parameters("rc_filter", parameters)

    def test_audited_unit_conversion_is_applied(self):
        parameters = representative_parameters("rc_filter")
        parameters[0].value = 1.0
        parameters[0].unit = "kΩ"
        canonical = canonicalize_parameters("rc_filter", parameters)
        values = {item.name: item.value for item in canonical}
        self.assertEqual(values["resistance"], 1000.0)

    def test_incompatible_or_unknown_unit_is_rejected(self):
        parameters = representative_parameters("rc_filter")
        parameters[0].unit = "V"
        with self.assertRaisesRegex(TemplateInputError, "incompatible"):
            canonicalize_parameters("rc_filter", parameters)

    def test_inconsistent_rc_formula_is_rejected(self):
        parameters = representative_parameters("rc_filter")
        parameters[1].value *= 2.0
        with self.assertRaisesRegex(TemplateInputError, "RC cutoff frequency mismatch"):
            render_template("rc_filter", parameters, model_name="BadPhysics")

    def test_model_name_injection_is_rejected(self):
        with self.assertRaisesRegex(TemplateInputError, "invalid Modelica model name"):
            render_template(
                "rc_filter",
                representative_parameters("rc_filter"),
                model_name="Bad; end Evil",
            )

    def test_full_file_rewrite_breaks_certified_integrity(self):
        rendered = render_template(
            "rc_filter", representative_parameters("rc_filter"), model_name="Original"
        )
        rewritten = replace(rendered, code=rendered.code.replace("StepVoltage", "SineVoltage"))
        valid, issues = verify_rendered_template(rewritten)
        self.assertFalse(valid)
        self.assertTrue(issues)


if __name__ == "__main__":
    unittest.main()

