from __future__ import annotations

from dataclasses import replace
import math
import unittest

from week9.experiments.cases import DEVELOPMENT_REGRESSION_CASES
from week9.src.generation import (
    DeterministicNaturalLanguageProvider,
    LOCAL_GRAMMAR_SHA256,
)
from week9.src.parameter_normalization import normalize_parameters
from week9.src.traceability import get_trace_registry


class DeterministicNaturalLanguageGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = DeterministicNaturalLanguageProvider()

    def test_all_frozen_development_requirements_prepare_without_tokens(self):
        expected = {
            "rc_lowpass": "rc_filter",
            "rlc_lowpass": "rlc_filter",
            "dual_room_thermal": "thermal_dual_room",
        }
        for case_id, template_id in expected.items():
            with self.subTest(case_id=case_id):
                case = DEVELOPMENT_REGRESSION_CASES[case_id]
                result = self.provider.generate(case.raw_input)
                self.assertTrue(result.ready)
                self.assertEqual(result.template_id, template_id)
                self.assertEqual(result.total_tokens, 0)
                self.assertFalse(result.online_api_used)
                self.assertEqual(result.evidence()["grammar_sha256"], LOCAL_GRAMMAR_SHA256)
                self.assertTrue(
                    normalize_parameters(result.template_id, result.observations).ready
                )

    def test_trace_spans_are_exact_and_cover_every_certified_target(self):
        for case in DEVELOPMENT_REGRESSION_CASES.values():
            with self.subTest(case_id=case.case_id):
                result = self.provider.generate(case.raw_input)
                trace = result.traceability_input
                self.assertIsNotNone(trace)
                for item in trace.requirements:
                    self.assertEqual(
                        case.raw_input[item.source_start:item.source_end], item.text
                    )
                self.assertEqual(
                    {item.target_id for item in trace.bindings},
                    set(get_trace_registry(result.template_id).expected_target_ids),
                )

    def test_rc_and_rlc_capacitance_use_only_declared_formulas(self):
        rc = self.provider.generate(
            DEVELOPMENT_REGRESSION_CASES["rc_lowpass"].raw_input
        )
        rlc = self.provider.generate(
            DEVELOPMENT_REGRESSION_CASES["rlc_lowpass"].raw_input
        )
        rc_c = next(item for item in rc.observations if item.name == "capacitance")
        rlc_c = next(item for item in rlc.observations if item.name == "capacitance")
        self.assertTrue(
            math.isclose(rc_c.value, 1 / (2 * math.pi * 1000 * 1000), rel_tol=1e-12)
        )
        self.assertTrue(
            math.isclose(
                rlc_c.value,
                1 / (((2 * math.pi * 10000) ** 2) * 0.01),
                rel_tol=1e-12,
            )
        )
        self.assertEqual(rc.derivations[0].formula, "C=1/(2*pi*R*fc)")
        self.assertEqual(rlc.derivations[0].formula, "C=1/((2*pi*f0)^2*L)")

    def test_other_two_certified_templates_have_explicit_local_grammar(self):
        opamp = self.provider.generate(
            "设计反相运算放大器，输入电压 0.5V，输入电阻 1kΩ，反馈电阻 10kΩ，"
            "正电源 15V，负电源 -15V"
        )
        room = self.provider.generate(
            "设计单房间热传导系统，房间热容 500kJ/K，墙体热阻 0.02 K/W，"
            "初始温度 20°C，室外温度 35°C"
        )
        self.assertTrue(opamp.ready)
        self.assertEqual(opamp.template_id, "opamp_inverting")
        self.assertTrue(room.ready)
        self.assertEqual(room.template_id, "thermal_single_room")

    def test_conflicting_duplicate_value_is_not_selected(self):
        result = self.provider.generate(
            "设计一个 RC 低通滤波器，截止频率 1kHz，电阻 1kΩ，"
            "电容根据截止频率计算，输入电压 5V，输入电压 3V 阶跃信号"
        )
        self.assertFalse(result.ready)
        self.assertIn("ambiguous_parameter", {item.code for item in result.issues})

    def test_missing_or_wrong_stimulus_is_blocking(self):
        missing = self.provider.generate(
            "设计一个 RC 低通滤波器，截止频率 1kHz，电阻 1kΩ，"
            "电容根据截止频率计算，输入电压 5V"
        )
        wrong = self.provider.generate(
            "设计一个 RC 低通滤波器，截止频率 1kHz，电阻 1kΩ，"
            "电容根据截止频率计算，输入电压 5V 正弦信号"
        )
        self.assertIn("missing_stimulus", {item.code for item in missing.issues})
        self.assertIn("stimulus_conflict", {item.code for item in wrong.issues})

    def test_unknown_topology_fails_without_partial_artifacts(self):
        result = self.provider.generate("设计三个房间组成的任意热网络，参数稍后再给")
        self.assertFalse(result.ready)
        self.assertEqual(result.issues[0].code, "no_certified_template")
        self.assertEqual(result.observations, ())
        self.assertIsNone(result.traceability_input)

    def test_requirement_or_trace_hash_tampering_is_rejected(self):
        result = self.provider.generate(
            DEVELOPMENT_REGRESSION_CASES["rc_lowpass"].raw_input
        )
        with self.assertRaisesRegex(ValueError, "requirement hash"):
            replace(result, requirement_sha256="0" * 64)


if __name__ == "__main__":
    unittest.main()
