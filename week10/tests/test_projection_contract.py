from __future__ import annotations

import math
import re
import unittest
import hashlib
from pathlib import Path

from week10.src.projection_contract import (
    ABS_TOL, CAPTURE_RULES, PROJECTION_SCHEMA, PROMPT_SHA256, PROMPT_VERSION,
    REL_TOL, SYSTEM_PROMPT, TEMPLATE_CONTRACTS, V8_SOURCE_HASHES,
)
from week9.src.schemas import sha256_text
from week9.src.traceability import get_trace_registry
from week9.src.generation import DeterministicNaturalLanguageProvider
from week9.src.modelica_templates import get_template_spec


class ProjectionContractTests(unittest.TestCase):
    def test_hash_and_schema_are_frozen(self) -> None:
        self.assertEqual(PROJECTION_SCHEMA, "v8-certified-requirement-projection-1")
        self.assertEqual(PROMPT_SHA256, sha256_text(PROMPT_VERSION + "\n" + SYSTEM_PROMPT))
        self.assertIn('"schema_version":"v8-certified-requirement-projection-1"', SYSTEM_PROMPT)
        self.assertNotIn("cases.py", SYSTEM_PROMPT)

    def test_fields_and_downstream_units_match_trace_registry(self) -> None:
        for template_id, contract in TEMPLATE_CONTRACTS.items():
            self.assertEqual(contract["fields"], get_trace_registry(template_id).parameter_names)
            self.assertEqual(set(contract["units"]), set(contract["fields"]))
            template_units = {item.name: item.unit for item in get_template_spec(template_id).parameters}
            self.assertEqual(contract["units"], template_units)

    def test_single_field_example_has_true_source_value_and_unit(self) -> None:
        source = "电阻 3 kOhm"
        match = re.fullmatch(r"电阻\s+(" + CAPTURE_RULES["number"] + r")\s+(" + CAPTURE_RULES["resistance"] + r")", source, re.IGNORECASE)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(float(match.group(1)), 3.0)
        self.assertEqual(match.group(2), "kOhm")
        self.assertIn('"source_text":"电阻 3 kOhm"', SYSTEM_PROMPT)

    def test_duplicate_joint_and_derivation_rules_are_explicit(self) -> None:
        for text in ("Equivalent repeats use the first occurrence", "Conflicting repeats are blocked", "takes priority over separately listed", "Do not invent any additional rejection rule"):
            self.assertIn(text, SYSTEM_PROMPT)
        self.assertIn("C=1/(2*pi*R*fc)", SYSTEM_PROMPT)
        self.assertIn("C=1/((2*pi*f0)^2*L)", SYSTEM_PROMPT)
        self.assertIn("explicit capacitance has priority", SYSTEM_PROMPT)
        self.assertIn("exactly one certified calculation phrase", SYSTEM_PROMPT)

    def test_prompt_contains_v8_label_spans_and_rc_boundary(self) -> None:
        for text in (
            "(?<![A-Za-z])RC(?![A-Za-z])",
            "(?:输入电压|输入(?:为|是)?|源电压)",
            "房间\\s*A\\s*热容",
            "两房间之间(?:墙体)?热阻",
            "房间\\s*B\\s*外墙热阻",
            "两房间初始温度均(?:为)?\\s*",
            "every matched whitespace",
        ):
            self.assertIn(text, SYSTEM_PROMPT)

    def test_static_contract_does_not_accept_case_data(self) -> None:
        self.assertFalse(hasattr(__import__("week10.src.projection_contract", fromlist=["x"]), "render_prompt"))
        self.assertEqual((REL_TOL, ABS_TOL), (1e-12, 1e-15))
        root = Path(__file__).resolve().parents[2]
        for relative_path, expected_hash in V8_SOURCE_HASHES.items():
            self.assertEqual(hashlib.sha256((root / relative_path).read_bytes()).hexdigest(), expected_hash)
        self.assertTrue(math.isclose(REL_TOL, 1e-12) and math.isclose(ABS_TOL, 1e-15))

    def test_custom_projection_behavior_matches_contract_rules(self) -> None:
        provider = DeterministicNaturalLanguageProvider()
        base = "RC串联低通滤波器，阶跃，截止频率 1 kHz，电阻 2 kOhm，输入 5 V"
        equivalent = provider.generate(base + "，电阻 2000 Ohm，电容 1 uF")
        self.assertTrue(equivalent.ready)
        resistance = next(item for item in equivalent.observations if item.name == "resistance")
        self.assertEqual((resistance.value, resistance.unit, resistance.source_text), (2.0, "kOhm", "电阻 2 kOhm"))
        self.assertFalse(provider.generate(base + "，电阻 3 kOhm，电容 1 uF").ready)

        joint = provider.generate("双房间热传导系统，房间A热容 1 J/K，房间B热容 2 J/K，两房间之间墙体热阻 3 K/W，房间A外墙热阻 4 K/W，房间B外墙热阻 5 K/W，两房间初始温度均为 20 °C，室外 10 °C")
        self.assertTrue(joint.ready)
        joint_initial = {item.name: item for item in joint.observations if item.name.startswith("initial_temperature_")}
        self.assertEqual(joint_initial["initial_temperature_a"].source_text, "两房间初始温度均为 20 °C")
        self.assertEqual(joint_initial["initial_temperature_b"].source_text, "两房间初始温度均为 20 °C")
        separate = provider.generate("双房间热传导系统，房间A热容 1 J/K，房间B热容 2 J/K，两房间之间墙体热阻 3 K/W，房间A外墙热阻 4 K/W，房间B外墙热阻 5 K/W，房间A初始温度 20 °C，房间B初始温度 21 °C，室外 10 °C")
        self.assertTrue(separate.ready)

        explicit = provider.generate(base + "，电容 1 uF，计算所需电容")
        self.assertTrue(explicit.ready)
        capacitance = next(item for item in explicit.observations if item.name == "capacitance")
        self.assertEqual((capacitance.value, capacitance.unit, capacitance.source_text), (1.0, "uF", "电容 1 uF"))
        self.assertFalse(provider.generate(base + "，计算所需电容，计算所需电容").ready)


if __name__ == "__main__":
    unittest.main()
