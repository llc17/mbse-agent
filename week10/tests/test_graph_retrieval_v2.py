from __future__ import annotations

import unittest

from week10.src.graph_retrieval_v2 import EvidenceIndexV2
from week10.src.trace_cli import DEFAULT_MANIFEST, load_runs


class EvidenceIndexV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runs = load_runs(DEFAULT_MANIFEST)
        cls.index = EvidenceIndexV2(cls.runs)
        cls.ids = [run.run_id for run in cls.runs]

    def test_declaration_paths_are_continuous_and_layered(self):
        answer = self.index.retrieve("输入电压对应哪些 SysML Modelica 元素", [self.ids[0]])
        self.assertEqual(answer["status"], "ready")
        self.assertEqual(answer["plan"]["target_ids"], ["parameter:input_voltage"])
        self.assertEqual({c["data"]["layer"] for c in answer["cards"]}, {"sysml", "modelica"})
        self.assertTrue(all(len(c["node_ids"]) == 3 and len(c["edge_ids"]) == 2 for c in answer["cards"]))
        self.assertEqual(answer["required_evidence_ids"], [c["id"] for c in answer["cards"]])
        impact = self.index.retrieve("输入电压影响哪些模型元素和检查", [self.ids[0]])
        self.assertEqual(impact["status"], "ready")
        self.assertTrue(any("layer sysml" in c["text"] and "element_id" in c["text"] for c in impact["cards"]))
        self.assertTrue(any("check_type" in c["text"] and "status" in c["text"] for c in impact["cards"]))
        causal = self.index.retrieve("输入电压对应哪些模型元素，以及它导致故障的原因是什么", [self.ids[0]])
        self.assertEqual(causal["reason_code"], "unsupported_question")

    def test_aliases_boundaries_and_ambiguity(self):
        dual = self.ids[2]
        self.assertEqual(self.index.retrieve("室外温度对应模型", [dual])["plan"]["target_ids"], ["parameter:outdoor_temperature"])
        self.assertEqual(self.index.retrieve("房间A热容对应模型", [dual])["plan"]["target_ids"], ["parameter:heat_capacity_a"])
        self.assertEqual(self.index.retrieve("initial_temperature_b 对应模型", [dual])["plan"]["target_ids"], ["parameter:initial_temperature_b"])
        self.assertEqual(self.index.retrieve("外墙热阻对应模型", [dual])["reason_code"], "ambiguous_target")
        self.assertEqual(self.index.retrieve("shared_resistance 对应模型", [dual])["plan"]["target_ids"], ["parameter:shared_resistance"])
        self.assertEqual(self.index.retrieve("wind_speed 对应模型", [dual])["reason_code"], "unknown_target")
        shared = self.index.retrieve("shared resistance 对应模型", [dual])
        self.assertEqual(shared["plan"]["target_ids"], ["parameter:shared_resistance"])
        outdoor = self.index.retrieve("outdoor resistance a 对应模型", [dual])
        self.assertEqual(outdoor["plan"]["target_ids"], ["parameter:outdoor_resistance_a"])
        self.assertEqual(self.index.retrieve("对应模型", [dual])["reason_code"], "unknown_target")

    def test_compare_order_and_budget_are_atomic(self):
        answer = self.index.retrieve("比较 input_voltage 对应模型", [self.ids[1], self.ids[0]])
        self.assertEqual(answer["status"], "ready")
        self.assertEqual(answer["run_ids"], [self.ids[1], self.ids[0]])
        limited = self.index.retrieve("输入电压对应模型", [self.ids[0]], max_cards=0)
        self.assertEqual(limited["reason_code"], "context_budget_exceeded")
        self.assertEqual(limited["cards"], [])
        self.assertTrue(limited["truncated"])
        self.assertEqual(limited["context_chars"], 2)
        incompatible = self.index.retrieve("比较 input_voltage 对应模型", [self.ids[0], self.ids[2]])
        self.assertEqual(incompatible["reason_code"], "incompatible_runs")

    def test_reverse_and_missing_layer_are_evidence_bounded(self):
        check_type = self.runs[0].checks[0].check_type
        answer = self.index.retrieve(f"需求如何追溯到 {check_type}", [self.ids[0]])
        self.assertEqual(answer["status"], "ready")
        self.assertTrue(all(len(card["node_ids"]) == 3 and len(card["edge_ids"]) == 2 for card in answer["cards"]))
        certified = self.index.retrieve("需求如何追溯到 certified_template_integrity", [self.ids[0]])
        self.assertEqual(certified["status"], "ready")
        self.assertEqual(self.index.retrieve("需求如何追溯到 certified_template_integrity 并证明量产认证", [self.ids[0]])["reason_code"], "unsupported_question")
        self.assertEqual(self.index.retrieve("输入电压需求如何追溯到 certified_template_integrity", [self.ids[0]])["reason_code"], "unsupported_question")
        self.assertEqual(self.index.retrieve("初始温度需求如何追溯到 certified_template_integrity", [self.ids[2]])["reason_code"], "ambiguous_target")
        index = EvidenceIndexV2(self.runs)
        edge_ids = [edge_id for edge_id, edge in index._edges.items()
                    if edge["run_id"] == self.ids[0] and edge["relation"] == "structurally_represented_by"
                    and edge["provenance"]["target_id"] == "parameter:resistance"
                    and index._nodes[edge["target"]]["layer"] == "modelica"]
        for edge_id in edge_ids:
            del index._edges[edge_id]
        missing = index.retrieve("电阻对应模型", [self.ids[0]])
        self.assertEqual(missing["reason_code"], "missing_evidence")
        missing_impact = index.retrieve("电阻影响哪些模型元素和检查", [self.ids[0]])
        self.assertEqual(missing_impact["reason_code"], "missing_evidence")

    def test_validation_and_defensive_result(self):
        with self.assertRaisesRegex(ValueError, "duplicates"):
            self.index.retrieve("输入电压对应模型", [self.ids[0], self.ids[0]])
        with self.assertRaisesRegex(ValueError, "unknown"):
            self.index.retrieve("输入电压对应模型", ["nope"])
        with self.assertRaisesRegex(ValueError, "non-empty"):
            self.index.retrieve("   ", [self.ids[0]])
        with self.assertRaisesRegex(ValueError, "at least 2"):
            self.index.retrieve("输入电压对应模型", [self.ids[0]], max_chars=1)
        answer = self.index.retrieve("输入电压对应模型", [self.ids[0]])
        answer["cards"].clear()
        self.assertTrue(self.index.retrieve("输入电压对应模型", [self.ids[0]])["cards"])


if __name__ == "__main__":
    unittest.main()
