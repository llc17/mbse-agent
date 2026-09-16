import copy
import unittest

from week11.src.spec import observations_for_spec, spec_digest, validate_spec


def rc_draft():
    text = "Build RC. R=1 kOhm; fc=1 kHz; Vin=5 V; derive C. Use a step."
    return {
        "schema_version": "v10-spec-1", "template_id": "rc_filter",
        "sources": [{"id": "u1", "text": text}],
        "parameters": [
            {"name": "resistance", "value": 1, "unit": "kOhm", "source_id": "u1", "source_text": "R=1 kOhm", "origin": "direct"},
            {"name": "cutoff_frequency", "value": 1, "unit": "kHz", "source_id": "u1", "source_text": "fc=1 kHz", "origin": "direct"},
            {"name": "input_voltage", "value": 5, "unit": "V", "source_id": "u1", "source_text": "Vin=5 V", "origin": "user"},
        ],
        "stimulus": {"kind": "step_voltage", "source_id": "u1", "source_text": "step"},
        "derivations": [{"target": "capacitance", "formula": "C = 1 / (2 * pi * R * fc)", "input_names": ["resistance", "cutoff_frequency"], "source_id": "u1", "source_text": "derive C"}],
        "constraints": ["fixed_certified_template"], "assumptions": [],
    }


class StructuredSpecTests(unittest.TestCase):
    def test_rc_derivation_and_revalidation(self):
        result = validate_spec(rc_draft())
        self.assertTrue(result["ready"], result["issues"])
        self.assertAlmostEqual(result["spec"]["normalized_parameters"][0]["value"], 1000.0)
        self.assertEqual(len(observations_for_spec(result["spec"])), 4)
        tampered = copy.deepcopy(result["spec"])
        tampered["normalized_parameters"][0]["value"] = 2
        self.assertEqual(spec_digest(tampered), spec_digest(result["spec"]))

    def test_bad_provenance_and_boolean_are_blocking(self):
        draft = rc_draft(); draft["parameters"][0]["value"] = True; draft["parameters"][1]["source_text"] = "invented"
        result = validate_spec(draft)
        self.assertFalse(result["ready"])
        self.assertEqual(result["spec"], None)
        self.assertIn("numeric_value", {item["code"] for item in result["issues"]})
        self.assertIn("source_text", {item["code"] for item in result["issues"]})

    def test_closed_template_formula_and_stimulus_rejected(self):
        draft = rc_draft(); draft["template_id"] = "opamp_inverting"
        self.assertIn("template_not_open", {item["code"] for item in validate_spec(draft)["issues"]})
        draft = rc_draft(); draft["stimulus"]["kind"] = "sine_voltage"; draft["derivations"][0]["formula"] = "C = 4"
        codes = {item["code"] for item in validate_spec(draft)["issues"]}
        self.assertTrue({"stimulus_mismatch", "unsupported_formula"} <= codes)

    def test_duplicate_unknown_and_non_finite_rejected(self):
        draft = rc_draft(); draft["parameters"].append(copy.deepcopy(draft["parameters"][0])); draft["parameters"][1]["value"] = float("nan")
        draft["parameters"].append({"name": "made_up", "value": 1, "unit": "V", "source_id": "u1", "source_text": "Vin=5 V", "origin": "direct"})
        codes = {item["code"] for item in validate_spec(draft)["issues"]}
        self.assertTrue({"duplicate_parameter", "numeric_value", "unknown_parameter"} <= codes)

    def test_rlc_and_dual_room_are_open(self):
        source = "R 100 Ohm, L 10 mH, fc 10 kHz, Vin 5 V, calculate C, sine."
        rlc = rc_draft()
        rlc.update({"template_id": "rlc_filter", "sources": [{"id": "s", "text": source}], "parameters": [
            {"name": "resistance", "value": 100, "unit": "Ohm", "source_id": "s", "source_text": "R 100 Ohm", "origin": "direct"},
            {"name": "inductance", "value": 10, "unit": "mH", "source_id": "s", "source_text": "L 10 mH", "origin": "direct"},
            {"name": "cutoff_frequency", "value": 10, "unit": "kHz", "source_id": "s", "source_text": "fc 10 kHz", "origin": "direct"},
            {"name": "input_voltage", "value": 5, "unit": "V", "source_id": "s", "source_text": "Vin 5 V", "origin": "direct"},
        ], "stimulus": {"kind": "sine_voltage", "source_id": "s", "source_text": "sine"},
        "derivations": [{"target": "capacitance", "formula": "C = 1 / ((2 * pi * fc)^2 * L)", "input_names": ["inductance", "cutoff_frequency"], "source_id": "s", "source_text": "calculate C"}]})
        self.assertTrue(validate_spec(rlc)["ready"])
        text = "Room A heat capacity 300000 J/K; Room B heat capacity 200000 J/K; shared wall 0.05 K/W; A outer wall 0.03 K/W; B outer wall 0.04 K/W; A begins 293.15 K; B begins 293.15 K; outdoors stay at 308.15 K."
        names = [("heat_capacity_a", 300000, "J/K"), ("heat_capacity_b", 200000, "J/K"), ("shared_resistance", .05, "K/W"), ("outdoor_resistance_a", .03, "K/W"), ("outdoor_resistance_b", .04, "K/W"), ("initial_temperature_a", 293.15, "K"), ("initial_temperature_b", 293.15, "K"), ("outdoor_temperature", 308.15, "K")]
        snippets = ["300000 J/K", "200000 J/K", "0.05 K/W", "0.03 K/W", "0.04 K/W", "293.15 K", "293.15 K", "308.15 K"]
        dual = {"schema_version": "v10-spec-1", "template_id": "thermal_dual_room", "sources": [{"id": "t", "text": text}], "parameters": [{"name": name, "value": value, "unit": unit, "source_id": "t", "source_text": snippet, "origin": "direct"} for (name, value, unit), snippet in zip(names, snippets)], "stimulus": {"kind": "fixed_temperature_boundary", "source_id": "t", "source_text": "outdoors stay"}, "derivations": [], "constraints": [], "assumptions": []}
        self.assertTrue(validate_spec(dual)["ready"])

    def test_local_numeric_provenance_and_other_blocking_paths(self):
        draft = rc_draft(); draft["parameters"][0]["value"] = 2
        self.assertIn("source_value_unit", {x["code"] for x in validate_spec(draft)["issues"]})
        draft = rc_draft(); draft["parameters"][0]["source_text"] = "resistance supplied"
        self.assertIn("source_value_unit", {x["code"] for x in validate_spec(draft)["issues"]})
        draft = rc_draft(); draft["parameters"][0]["source_text"] = "R=1 V"
        self.assertIn("source_value_unit", {x["code"] for x in validate_spec(draft)["issues"]})
        draft = rc_draft(); draft["parameters"][0]["unit"] = "parsec"
        self.assertIn("unit", {x["code"] for x in validate_spec(draft)["issues"]})
        draft = rc_draft(); draft["parameters"] = draft["parameters"][:-1]
        self.assertIn("missing_parameter", {x["code"] for x in validate_spec(draft)["issues"]})
        draft = rc_draft(); draft["parameters"].append({"name": "capacitance", "value": 1, "unit": "uF", "source_id": "u1", "source_text": "C=1 uF", "origin": "direct"})
        self.assertIn("derivation_conflict", {x["code"] for x in validate_spec(draft)["issues"]})
        draft = rc_draft(); draft["constraints"] = ["invent a controller"]
        self.assertIn("unsupported_constraint", {x["code"] for x in validate_spec(draft)["issues"]})

    def test_natural_synonyms_need_no_provider_or_label_grammar(self):
        source = "For the low pass, use one kilo-ohm? Actually use 1 kOhm, with corner at 1 kHz and feed it 5 V; make C from the stated formula; apply a step."
        draft = rc_draft(); draft["sources"] = [{"id": "n", "text": source}]
        for parameter in draft["parameters"]:
            parameter["source_id"] = "n"
        draft["parameters"][0]["source_text"] = "1 kOhm"; draft["parameters"][1]["source_text"] = "1 kHz"; draft["parameters"][2]["source_text"] = "5 V"
        draft["stimulus"] = {"kind": "step_voltage", "source_id": "n", "source_text": "step"}; draft["derivations"][0].update({"source_id": "n", "source_text": "stated formula"})
        self.assertTrue(validate_spec(draft)["ready"])
        chinese = "阻值取 1 kOhm，转折点设为 1 kHz，以 5 V 突变驱动，电容按公式求得。"
        draft = rc_draft(); draft["sources"] = [{"id": "cn", "text": chinese}]
        for parameter, snippet in zip(draft["parameters"], ["1 kOhm", "1 kHz", "5 V"]):
            parameter.update({"source_id": "cn", "source_text": snippet})
        draft["stimulus"] = {"kind": "step_voltage", "source_id": "cn", "source_text": "突变"}; draft["derivations"][0].update({"source_id": "cn", "source_text": "按公式"})
        self.assertTrue(validate_spec(draft)["ready"])

    def test_malformed_derivation_and_extreme_arithmetic_block_without_crash(self):
        draft = rc_draft(); draft["derivations"][0]["target"] = []
        self.assertIn("derivation", {x["code"] for x in validate_spec(draft)["issues"]})
        draft = rc_draft(); draft["sources"][0]["text"] = "R=1e308 Ohm; fc=1e308 Hz; Vin=5 V; derive C. Use a step."
        draft["parameters"][0].update({"value": 1e308, "unit": "Ohm", "source_text": "R=1e308 Ohm"})
        draft["parameters"][1].update({"value": 1e308, "unit": "Hz", "source_text": "fc=1e308 Hz"})
        result = validate_spec(draft)
        self.assertFalse(result["ready"])
        self.assertIn("derivation_value", {x["code"] for x in result["issues"]})

    def test_celsius_source_provenance_converts_to_kelvin(self):
        text = "A is 20°C, B is 20°C, outside is 35°C; capacities are 300000 J/K and 200000 J/K; resistances are 0.05 K/W, 0.03 K/W, and 0.04 K/W. Fixed outside."
        values = [("heat_capacity_a", 300000, "J/K", "300000 J/K"), ("heat_capacity_b", 200000, "J/K", "200000 J/K"), ("shared_resistance", .05, "K/W", "0.05 K/W"), ("outdoor_resistance_a", .03, "K/W", "0.03 K/W"), ("outdoor_resistance_b", .04, "K/W", "0.04 K/W"), ("initial_temperature_a", 20, "degC", "20°C"), ("initial_temperature_b", 20, "degC", "20°C"), ("outdoor_temperature", 35, "degC", "35°C")]
        draft = {"schema_version": "v10-spec-1", "template_id": "thermal_dual_room", "sources": [{"id": "t", "text": text}], "parameters": [{"name": name, "value": value, "unit": unit, "source_id": "t", "source_text": snippet, "origin": "direct"} for name, value, unit, snippet in values], "stimulus": {"kind": "fixed_temperature_boundary", "source_id": "t", "source_text": "Fixed outside"}, "derivations": [], "constraints": [], "assumptions": []}
        result = validate_spec(draft)
        self.assertTrue(result["ready"], result["issues"])
        self.assertAlmostEqual(result["spec"]["normalized_parameters"][5]["value"], 293.15)


if __name__ == "__main__":
    unittest.main()
