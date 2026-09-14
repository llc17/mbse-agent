from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import unittest

from week10.src.graph_evidence_cli import main
from week10.src.graph_evidence_view import build_evidence_answer, render_markdown
from week10.src.graph_retrieval_v2 import EvidenceIndexV2
from week10.src.trace_cli import DEFAULT_MANIFEST, load_runs


class GraphEvidenceViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runs = load_runs(DEFAULT_MANIFEST)
        cls.index = EvidenceIndexV2(cls.runs)
        cls.before, cls.after, cls.dual = [run.run_id for run in cls.runs]

    def test_declarations_keep_requirement_and_literal_selector(self):
        result = build_evidence_answer(self.index, "输入电压对应哪些 SysML Modelica 元素", [self.before])
        items = result["evidence_answer"]["items"]
        self.assertEqual(result["api_call_count"], 0)
        self.assertEqual({item["layer"] for item in items}, {"sysml", "modelica"})
        self.assertTrue(all(item["requirement_id"] and item["requirement_text"] and item["exact_selector"].endswith(";") for item in items))
        self.assertIn("final parameter Modelica.Units.SI.Voltage input_voltage = 5;", render_markdown(result))

    def test_compare_changed_and_unchanged_targets(self):
        changed = build_evidence_answer(self.index, "比较 input_voltage 对应模型", [self.before, self.after])
        comparison = changed["evidence_answer"]["comparison"]
        self.assertTrue(comparison["normalized_parameter_changed"])
        self.assertEqual(comparison["change_record"]["before"], {"value": 5.0, "unit": "V"})
        unchanged = build_evidence_answer(self.index, "比较 resistance 对应模型", [self.before, self.after])
        self.assertFalse(unchanged["evidence_answer"]["comparison"]["normalized_parameter_changed"])

    def test_reverse_and_impact_are_explicit_structure(self):
        check = self.runs[0].checks[0].check_type
        reverse = build_evidence_answer(self.index, f"需求如何追溯到 {check}", [self.before])
        self.assertTrue(all(len(x["node_ids"]) == 3 and x["check_type"] == check for x in reverse["evidence_answer"]["items"]))
        impact = build_evidence_answer(self.index, "输入电压影响哪些模型元素和检查", [self.before])
        items = impact["evidence_answer"]["items"]
        self.assertEqual({x["layer"] for x in items if "layer" in x}, {"sysml", "modelica"})
        self.assertTrue(all("check_status" in x for x in items if "check_type" in x))

    def test_missing_budget_and_broken_path_abstain(self):
        budget = build_evidence_answer(self.index, "输入电压对应模型", [self.before], max_cards=0)
        self.assertEqual(budget["evidence_answer"]["reason_code"], "context_budget_exceeded")
        retrieval = self.index.retrieve("输入电压对应模型", [self.before])
        retrieval["cards"][0]["edge_ids"] = retrieval["cards"][0]["edge_ids"][:1]
        class Broken:
            graph = self.index.graph
            def retrieve(self, *args, **kwargs): return deepcopy(retrieval)
        broken = build_evidence_answer(Broken(), "unused", [self.before])
        self.assertEqual(broken["evidence_answer"]["status"], "abstained")

    def test_rejects_empty_or_off_question_mocked_cards(self):
        retrieval = self.index.retrieve("输入电压对应模型", [self.before])
        class Controlled:
            graph = self.index.graph
            def __init__(self, packet): self.packet = packet
            def retrieve(self, *args, **kwargs): return deepcopy(self.packet)
        empty = deepcopy(retrieval)
        empty["cards"] = []
        empty["required_evidence_ids"] = []
        self.assertEqual(build_evidence_answer(Controlled(empty), "unused", [self.before])["evidence_answer"]["status"], "abstained")
        wrong_run = deepcopy(retrieval)
        wrong_run["cards"][0]["run_id"] = self.after
        self.assertEqual(build_evidence_answer(Controlled(wrong_run), "unused", [self.before])["evidence_answer"]["status"], "abstained")
        wrong_target = deepcopy(retrieval)
        wrong_target["plan"]["target_ids"] = ["parameter:resistance"]
        self.assertEqual(build_evidence_answer(Controlled(wrong_target), "unused", [self.before])["evidence_answer"]["status"], "abstained")

    def test_rejects_missing_graph_fields_and_bad_comparison_contract(self):
        retrieval = self.index.retrieve("输入电压对应模型", [self.before])
        node_id = retrieval["cards"][0]["node_ids"][-1]
        graph_data = self.index.graph.to_dict()
        for node in graph_data["nodes"]:
            if node["id"] == node_id:
                del node["exact_selector"]
        class DamagedGraph:
            def to_dict(self): return deepcopy(graph_data)
        class DamagedIndex:
            graph = DamagedGraph()
            def retrieve(self, *args, **kwargs): return deepcopy(retrieval)
        self.assertEqual(build_evidence_answer(DamagedIndex(), "unused", [self.before])["evidence_answer"]["status"], "abstained")
        compare = self.index.retrieve("比较 input_voltage 对应模型", [self.before, self.after])
        class BadGraph:
            def to_dict(self): return self_outer.index.graph.to_dict()
            def compare_runs(self, *args): return {"parameter_changes": "bad"}
        self_outer = self
        class BadCompare:
            graph = BadGraph()
            def retrieve(self, *args, **kwargs): return deepcopy(compare)
        self.assertEqual(build_evidence_answer(BadCompare(), "unused", [self.before, self.after])["evidence_answer"]["status"], "abstained")

    def test_cli_json_markdown_and_literal_backticks(self):
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--question", "输入电压对应模型", "--run-id", self.before, "--format", "json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["api_call_count"], 0)
        rendered = render_markdown({"evidence_answer": {"status": "ready", "origin": "deterministic_graph_evidence", "reason_code": None,
                                    "comparison": None, "items": [{"path_id": "p", "run_id": "r", "target_id": "t", "node_ids": ["n"], "edge_ids": ["e"],
                                    "layer": "sysml", "element_id": "x", "exact_selector": "a ``` b;"}]}, "api_call_count": 0})
        self.assertIn("````\na ``` b;\n````", rendered)


if __name__ == "__main__":
    unittest.main()
