from __future__ import annotations

from dataclasses import asdict
import json
import unittest
from unittest.mock import patch

from week9.src.generation import DeterministicNaturalLanguageProvider
from week9.src.online_generation import (
    ONLINE_PROJECTION_SCHEMA,
    _parse_projection,
    _verify_projection,
)
from week10.src.projection_diagnostics import diagnose_projection


def _content(local, mutate=None):
    projection = {
        "schema_version": ONLINE_PROJECTION_SCHEMA,
        "status": "ready" if local.ready else "blocked",
        "template_id": local.template_id,
        "observations": [asdict(item) for item in local.observations],
        "issues": [] if local.ready else [asdict(item) for item in local.issues],
    }
    if mutate:
        mutate(projection)
    return json.dumps(projection, ensure_ascii=False, separators=(",", ":"))


class ProjectionDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.provider = DeterministicNaturalLanguageProvider()
        self.requirements = (
            "设计一个 RC 低通滤波器，截止频率 1kHz，电阻 1kΩ，电容根据截止频率计算，输入电压 5V 阶跃信号",
            "设计一个 RLC 串联低通滤波器，截止频率 10kHz，电阻 100Ω，电感 10mH，电容根据谐振频率计算，输入电压 5V 正弦波",
            "设计反相运算放大器，输入电压 0.5V，输入电阻 1kΩ，反馈电阻 10kΩ，正电源 15V，负电源 -15V",
            "设计单房间热传导系统，房间热容 500kJ/K，墙体热阻 0.02 K/W，初始温度 20°C，室外温度 35°C",
            "设计双房间热传导系统：房间A热容 300kJ/K，房间B热容 200kJ/K，两房间之间墙体热阻 0.05 K/W，房间A外墙热阻 0.03 K/W 连接到室外 35°C，房间B外墙热阻 0.04 K/W 连接到室外 35°C，两房间初始温度均为 20°C",
        )

    def test_all_five_templates_accept_the_exact_local_projection(self):
        for requirement in self.requirements:
            with self.subTest(requirement=requirement[:12]):
                local = self.provider.generate(requirement)
                self.assertTrue(local.ready)
                diagnostic = diagnose_projection(_content(local), local)
                self.assertTrue(diagnostic.projection_accepted)
                self.assertEqual(diagnostic.reason_code, "accepted")
                self.assertEqual(diagnostic.legacy_disposition, "accepted")

    def test_parse_failures_are_infrastructure_with_distinct_reasons(self):
        local = self.provider.generate(self.requirements[0])
        self.assertEqual(diagnose_projection("{", local).reason_code, "response_json_invalid")
        schema = diagnose_projection("{}", local)
        self.assertEqual(schema.reason_code, "projection_schema_invalid")
        self.assertEqual(schema.legacy_disposition, "infrastructure_error")

    def test_ordered_content_rejections(self):
        local = self.provider.generate(self.requirements[0])
        cases = []
        cases.append(("ready_mismatch", lambda p: (p.update(status="blocked"), p.update(observations=[]), p.update(issues=[{"code":"x","message":"x"}]))))
        cases.append(("template_mismatch", lambda p: p.update(template_id="rlc_filter")))
        cases.append(("parameter_count_mismatch", lambda p: p["observations"].pop()))
        def duplicate_without_changing_count(p):
            p["observations"][1]["name"] = p["observations"][0]["name"]
        cases.append(("duplicate_parameter", duplicate_without_changing_count))
        cases.append(("parameter_name_mismatch", lambda p: p["observations"][0].update(name="malicious-secret-name")))
        cases.append(("unit_mismatch", lambda p: p["observations"][0].update(unit="V")))
        cases.append(("source_span_mismatch", lambda p: p["observations"][0].update(source_text="provider secret")))
        cases.append(("value_mismatch", lambda p: p["observations"][0].update(value=p["observations"][0]["value"] + 1)))
        for expected, mutate in cases:
            with self.subTest(expected=expected):
                result = diagnose_projection(_content(local, mutate), local)
                self.assertEqual(result.reason_code, expected)
                self.assertFalse(result.projection_accepted)
                self.assertEqual(result.legacy_disposition, "content_rejection")

    def test_local_blocked_is_not_a_projection_acceptance(self):
        local = self.provider.generate(
            "RC 低通滤波器，截止频率 2kHz，电阻 2kΩ，输入电压 4V 阶跃信号"
        )
        self.assertFalse(local.ready)
        result = diagnose_projection(_content(local), local)
        self.assertEqual(result.reason_code, "local_input_blocked")
        self.assertFalse(result.projection_accepted)

    def test_priority_and_public_output_do_not_leak_provider_text(self):
        local = self.provider.generate(self.requirements[0])
        def mutate(p):
            p["observations"].append(dict(p["observations"][0]))
            p["template_id"] = "malicious-template"
            p["observations"][0]["source_text"] = "TOP-SECRET"
        result = diagnose_projection(_content(local, mutate), local)
        self.assertEqual(result.reason_code, "template_mismatch")
        public = result.public_dict()
        self.assertEqual(set(public), {"reason_code", "parameter_keys", "projection_accepted", "legacy_disposition"})
        self.assertNotIn("TOP-SECRET", json.dumps(public))
        self.assertNotIn("malicious-template", json.dumps(public))

    def test_nonfinite_bool_and_outer_whitespace_are_schema_errors(self):
        local = self.provider.generate(self.requirements[0])
        for mutate in (
            lambda p: p["observations"][0].update(value=True),
            lambda p: p["observations"][0].update(value=float("inf")),
        ):
            with self.subTest(mutate=mutate):
                self.assertEqual(diagnose_projection(_content(local, mutate), local).reason_code, "projection_schema_invalid")
        self.assertEqual(diagnose_projection(" " + _content(local), local).reason_code, "projection_schema_invalid")

    def test_v8_verifier_differential_and_value_tolerance(self):
        local = self.provider.generate(self.requirements[0])
        accepted_content = _content(local)
        accepted_projection = _parse_projection(accepted_content)
        self.assertEqual(_verify_projection(accepted_projection, local), ())
        self.assertTrue(diagnose_projection(accepted_content, local).projection_accepted)

        def within_tolerance(p):
            p["observations"][0]["value"] *= 1 + 5e-13
        within_content = _content(local, within_tolerance)
        self.assertEqual(_verify_projection(_parse_projection(within_content), local), ())
        self.assertTrue(diagnose_projection(within_content, local).projection_accepted)

        def outside_tolerance(p):
            p["observations"][0]["value"] *= 1 + 2e-12
        outside_content = _content(local, outside_tolerance)
        self.assertTrue(_verify_projection(_parse_projection(outside_content), local))
        self.assertEqual(diagnose_projection(outside_content, local).reason_code, "value_mismatch")

    def test_diagnosis_never_opens_the_network(self):
        local = self.provider.generate(self.requirements[0])
        with patch("week9.src.online_generation.urlopen", side_effect=AssertionError("network used")):
            result = diagnose_projection(_content(local), local)
        self.assertTrue(result.projection_accepted)


if __name__ == "__main__":
    unittest.main()
