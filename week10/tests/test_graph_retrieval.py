from __future__ import annotations

import json
import unittest

from week10.src.graph_retrieval import EvidenceIndex
from week10.src.trace_cli import DEFAULT_MANIFEST, load_runs


class EvidenceIndexTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runs = load_runs(DEFAULT_MANIFEST)
        cls.index = EvidenceIndex(cls.runs)
        cls.ids = [run.run_id for run in cls.runs]

    def test_versions_filters_and_determinism(self):
        one = [self.ids[0]]
        a = self.index.retrieve("input_voltage 参数", one, "lexical")
        self.assertEqual(a, self.index.retrieve("input_voltage 参数", one, "lexical"))
        self.assertTrue(a["cards"])
        self.assertEqual({card["run_id"] for card in a["cards"]}, set(one))
        self.assertTrue(all(one[0] in card["text"] for card in a["cards"]))
        with self.assertRaisesRegex(ValueError, "unknown run_id"):
            self.index.retrieve("input_voltage", ["missing"])

    def test_budget_uses_exact_context_encoding(self):
        answer = self.index.retrieve("input_voltage", [self.ids[0]], max_cards=1, max_chars=80)
        encoded = json.dumps(answer["context"], ensure_ascii=False, separators=(",", ":"))
        self.assertEqual(answer["context_chars"], len(encoded))
        self.assertLessEqual(answer["context_chars"], 80)
        self.assertTrue(answer["truncated"])
        self.assertEqual(answer["context"], [{"id": item["id"], "text": item["text"]} for item in answer["cards"]])
        self.assertLessEqual(len(answer["cards"]), 1)
        zero = self.index.retrieve("input_voltage", [self.ids[0]], max_cards=0)
        self.assertEqual(zero["context"], [])
        self.assertEqual(zero["cards"], [])
        self.assertEqual(zero["selected_graph_ids"], [])

    def test_graph_connectivity_and_edge_provenance(self):
        answer = self.index.retrieve("input_voltage", [self.ids[0]], "graph")
        self.assertTrue(answer["seed_node_ids"])
        self.assertTrue(answer["selected_graph_ids"])
        edges = [card for card in answer["cards"] if card["kind"] == "edge"]
        self.assertTrue(edges)
        for card in edges:
            self.assertEqual(card["data"]["provenance"]["source"], "TraceabilityReport.links")
            self.assertIn(card["data"]["source"], answer["selected_graph_ids"])
            self.assertIn(card["data"]["target"], answer["selected_graph_ids"])
        self.assertTrue(set(answer["selected_graph_ids"]).issubset(answer["candidate_graph_ids"]))

    def test_element_seed_can_reach_its_actual_edge_without_shared_fanout(self):
        element = next(card for card in self.index.cards if card["kind"] == "node" and card["data"].get("element_id") == "modelica.parameter.input_voltage")
        answer = self.index.retrieve("modelica.parameter.input_voltage", [element["run_id"]], "graph")
        self.assertIn(element["graph_id"], answer["seed_node_ids"])
        incident = [card for card in answer["cards"] if card["kind"] == "edge" and element["graph_id"] in {card["data"]["source"], card["data"]["target"]}]
        self.assertTrue(incident)
        artifacts = {card["graph_id"] for card in self.index.cards if card["kind"] == "node" and card["data"].get("type") == "artifact"}
        by_graph_id = {card["graph_id"]: card for card in self.index.cards}
        seeded_elements = {
            graph_id for graph_id in answer["seed_node_ids"]
            if by_graph_id[graph_id]["data"]["type"] == "element"
        }
        artifact_edges = [
            card for card in answer["cards"] if card["kind"] == "edge"
            and {card["data"]["source"], card["data"]["target"]} & artifacts
        ]
        self.assertTrue(artifact_edges)
        self.assertTrue(all(
            {item["data"]["source"], item["data"]["target"]} & seeded_elements
            for item in artifact_edges
        ))
        artifact = next(card for card in self.index.cards if card["kind"] == "node" and card["run_id"] == element["run_id"] and card["data"].get("type") == "artifact")
        leaf = self.index.retrieve(artifact["data"]["sha256"], [artifact["run_id"]], "graph")
        self.assertIn(artifact["graph_id"], leaf["seed_node_ids"])
        self.assertFalse(any(card["kind"] == "edge" for card in leaf["cards"]))

    def test_unrelated_question_is_empty(self):
        for mode in ("graph", "lexical"):
            answer = self.index.retrieve("量子香蕉星云", self.ids, mode)
            self.assertEqual(answer["cards"], [])
            self.assertEqual(answer["context"], [])


if __name__ == "__main__":
    unittest.main()
