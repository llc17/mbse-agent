from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import unittest

from week9.src.schemas import RunResult
from week10.src.trace_graph import SCOPE, TraceGraph


PUBLIC_ROOT = (
    Path(__file__).resolve().parents[1]
    / "results"
    / "trace-stage2-20260909"
)


class TraceGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        manifest_path = PUBLIC_ROOT / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("public_data_only") is not True:
            raise AssertionError("trace graph fixtures must be public")
        cls.payloads = {}
        project_root = Path(__file__).resolve().parents[2]
        for entry in manifest["runs"]:
            path = project_root / entry["path"]
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
                raise AssertionError("trace graph fixture hash mismatch")
            cls.payloads[entry["run_id"]] = json.loads(raw.decode("utf-8"))

    def fixture(self, run_id: str) -> RunResult:
        return RunResult.from_dict(json.loads(json.dumps(self.payloads[run_id])))

    def graph_with_public_runs(self) -> TraceGraph:
        graph = TraceGraph()
        graph.add_run(self.fixture("trace_rc_before"))
        graph.add_run(self.fixture("trace_rc_after"))
        graph.add_run(self.fixture("trace_dual"))
        return graph

    def test_add_revalidates_stale_artifact_trace_and_check_evidence(self):
        stale_artifact = self.fixture("trace_rc_before")
        stale_artifact.artifacts["modelica"].content += "\n// changed"
        with self.assertRaisesRegex(ValueError, "invalid RunResult snapshot"):
            TraceGraph().add_run(stale_artifact)

        tampered_report = self.fixture("trace_rc_before")
        tampered_report.traceability = replace(
            tampered_report.traceability, scope="tampered scope"
        )
        with self.assertRaisesRegex(ValueError, "invalid RunResult snapshot"):
            TraceGraph().add_run(tampered_report)

        tampered_check = self.fixture("trace_rc_before")
        compile_check = next(
            item for item in tampered_check.checks
            if item.check_type == "modelica_compile"
        )
        compile_check.evidence["tampered"] = True
        with self.assertRaisesRegex(ValueError, "invalid RunResult snapshot"):
            TraceGraph().add_run(tampered_check)

    def test_duplicate_rejection_defensive_copy_and_run_id_isolation(self):
        original = self.fixture("trace_rc_before")
        graph = TraceGraph()
        self.assertEqual(graph.add_run(original), "trace_rc_before")
        with self.assertRaisesRegex(ValueError, "duplicate run_id"):
            graph.add_run(self.fixture("trace_rc_before"))

        original.requirement_text = "mutated after add"
        self.assertEqual(
            graph.query_requirement("trace_rc_before", "REQ-NL-004")["requirements"][0]["text"],
            self.payloads["trace_rc_before"]["traceability"]["links"][3]["requirement_text"],
        )

        copy = self.fixture("trace_rc_before")
        copy.run_id = "trace_rc_before_copy"
        copy.created_at = "2099-01-01T00:00:00+00:00"
        graph.add_run(copy)
        unchanged = graph.compare_runs("trace_rc_before", "trace_rc_before_copy")
        self.assertEqual(unchanged["parameter_changes"], [])
        self.assertEqual(unchanged["changed_target_ids"], [])
        by_run = {}
        for node in graph.to_dict()["nodes"]:
            by_run.setdefault(node["run_id"], set()).add(node["id"])
        self.assertTrue(by_run["trace_rc_before"].isdisjoint(by_run["trace_rc_before_copy"]))

    def test_forward_query_is_only_the_selected_explicit_link(self):
        graph = self.graph_with_public_runs()
        result = graph.query_requirement("trace_rc_before", "REQ-NL-004")
        self.assertEqual(result["target_ids"], ["parameter:input_voltage"])
        self.assertEqual(
            {item["element_id"] for item in result["elements"]},
            {"sysml.attribute.input_voltage", "modelica.parameter.input_voltage"},
        )
        self.assertEqual(
            {item["check_type"] for item in result["checks"]},
            {
                "requirement_normalization",
                "parameter_unit_consistency",
                "cross_model_consistency",
                "certified_template_integrity",
                "modelica_compile",
            },
        )
        self.assertNotIn("sysml.part.source", {item["element_id"] for item in result["elements"]})
        self.assertEqual(len(result["evidence_sha256"]), 64)

    def test_reverse_query_does_not_expand_through_shared_nodes(self):
        graph = self.graph_with_public_runs()
        result = graph.reverse_check("trace_rc_before", "sysml_syntax")
        self.assertEqual([item["id"] for item in result["requirements"]], ["REQ-NL-005"])
        self.assertEqual(result["target_ids"], ["constraint:topology"])
        self.assertEqual(result["coverage_status"], "explicit_verified_links")
        self.assertIn("not proof", result["scope_warning"])

        unlinked = graph.reverse_check("trace_rc_before", "physical_behavior")
        self.assertEqual(unlinked["requirements"], [])
        self.assertEqual(unlinked["target_ids"], [])
        self.assertEqual(
            unlinked["coverage_status"],
            "check_present_without_explicit_trace_links",
        )
        absent = graph.reverse_check("trace_rc_before", "not_a_run_check")
        self.assertEqual(absent["requirements"], [])
        self.assertEqual(absent["coverage_status"], "check_absent_from_run")

    def test_impact_separates_direct_evidence_from_conservative_rerun_checks(self):
        graph = self.graph_with_public_runs()
        result = graph.impact("trace_rc_before", "parameter:input_voltage")
        direct = {item["check_type"] for item in result["checks"]}
        rerun = {item["check_type"] for item in result["rerun_required_checks"]}
        self.assertNotIn("physical_behavior", direct)
        self.assertIn("physical_behavior", rerun)
        self.assertIn("traceability_integrity", rerun)
        self.assertEqual(len(rerun), 11)
        self.assertEqual(
            {item["artifact_type"] for item in result["artifacts"]},
            {"sysml", "modelica"},
        )
        self.assertEqual(result["scope"], SCOPE)
        self.assertFalse(result["numerical_behavior_guarantee"])

    def test_unknown_run_requirement_and_target_fail_closed(self):
        graph = self.graph_with_public_runs()
        with self.assertRaisesRegex(ValueError, "unknown run_id"):
            graph.query_requirement("missing", "REQ-NL-004")
        with self.assertRaisesRegex(ValueError, "unknown requirement_id"):
            graph.query_requirement("trace_rc_before", "missing")
        with self.assertRaisesRegex(ValueError, "unknown target_id"):
            graph.impact("trace_rc_before", "parameter:missing")

    def test_comparison_detects_physical_change_and_actual_artifact_changes(self):
        graph = self.graph_with_public_runs()
        result = graph.compare_runs("trace_rc_before", "trace_rc_after")
        self.assertEqual(result["changed_target_ids"], ["parameter:input_voltage"])
        self.assertEqual(
            result["changed_artifact_types"], ["modelica", "requirement", "sysml"]
        )
        self.assertEqual(result["parameter_changes"][0]["before"]["value"], 5.0)
        self.assertEqual(result["parameter_changes"][0]["after"]["value"], 6.0)
        self.assertEqual(result["impacts"][0]["target_id"], "parameter:input_voltage")
        self.assertIsNotNone(result["impacts"][0]["before"])
        self.assertIsNotNone(result["impacts"][0]["after"])

        unchanged = graph.compare_runs("trace_rc_before", "trace_rc_before")
        self.assertEqual(unchanged["parameter_changes"], [])
        self.assertEqual(unchanged["changed_target_ids"], [])
        self.assertEqual(unchanged["changed_artifact_types"], [])
        self.assertEqual(unchanged["impacts"], [])

    def test_comparison_rejects_mismatched_case(self):
        graph = TraceGraph()
        before = self.fixture("trace_rc_before")
        after = self.fixture("trace_rc_after")
        after.run_id = "different-case"
        after.case_id = "other-case"
        graph.add_run(before)
        graph.add_run(after)
        with self.assertRaisesRegex(ValueError, "different case_id"):
            graph.compare_runs(before.run_id, after.run_id)

        template_graph = TraceGraph()
        rc = self.fixture("trace_rc_before")
        dual = self.fixture("trace_dual")
        dual.case_id = rc.case_id
        template_graph.add_run(rc)
        template_graph.add_run(dual)
        with self.assertRaisesRegex(ValueError, "different template_id"):
            template_graph.compare_runs(rc.run_id, dual.run_id)

    def test_graph_serialization_is_stable_and_edges_carry_provenance(self):
        graph = self.graph_with_public_runs()
        first = graph.to_dict()
        self.assertEqual(first, graph.to_dict())
        json.dumps(first, ensure_ascii=False, allow_nan=False, sort_keys=True)
        self.assertTrue(first["nodes"])
        self.assertTrue(first["edges"])
        for edge in first["edges"]:
            self.assertIn("relation", edge)
            self.assertEqual(edge["provenance"]["source"], "TraceabilityReport.links")
            self.assertEqual(len(edge["evidence_sha256"]), 64)
            self.assertEqual(len(edge["run_snapshot_sha256"]), 64)
            self.assertEqual(len(edge["link_evidence_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
