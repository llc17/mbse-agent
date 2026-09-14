"""Public-manifest tests for recorded RC arithmetic dependency tracing."""

from __future__ import annotations

import hashlib
import json
from io import StringIO
import unittest
from contextlib import redirect_stderr, redirect_stdout

from week10.src import dependency_cli
from week10.src.dependency_trace import DependencyTraceGraph
from week10.src.trace_cli import DEFAULT_MANIFEST, load_runs


class DependencyTraceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = DependencyTraceGraph(load_runs(DEFAULT_MANIFEST))

    def test_cutoff_reaches_only_recorded_capacitance_derivation(self) -> None:
        result = self.graph.impact("trace_rc_before", "parameter:cutoff_frequency")
        self.assertEqual(
            result["affected_target_ids"],
            ["parameter:cutoff_frequency", "parameter:capacitance"],
        )
        self.assertEqual(len(result["dependency_paths"]), 1)
        self.assertEqual(result["dependency_paths"][0]["target_ids"][-1], "parameter:capacitance")
        self.assertNotIn("parameter:resistance", result["affected_target_ids"])
        self.assertNotIn("parameter:input_voltage", result["affected_target_ids"])

    def test_resistance_reaches_only_recorded_capacitance_derivation(self) -> None:
        result = self.graph.impact("trace_rc_before", "parameter:resistance")
        self.assertEqual(
            result["affected_target_ids"],
            ["parameter:resistance", "parameter:capacitance"],
        )

    def test_capacitance_does_not_reverse_to_inputs(self) -> None:
        result = self.graph.impact("trace_rc_before", "parameter:capacitance")
        self.assertEqual(result["affected_target_ids"], ["parameter:capacitance"])
        self.assertEqual(result["dependency_paths"], [{"target_ids": ["parameter:capacitance"], "edge_ids": []}])

    def test_original_evidence_and_dependency_hashes_are_exposed(self) -> None:
        payload = self.graph.to_dict()
        edge = next(item for item in payload["dependency_edges"] if item["run_id"] == "trace_rc_before")
        self.assertEqual(edge["source_text"], "电容根据截止频率计算")
        self.assertEqual(len(edge["generation_evidence_sha256"]), 64)
        self.assertEqual(len(edge["run_snapshot_sha256"]), 64)
        self.assertTrue(any(item["type"] == "target" for item in payload["nodes"]))

    def test_dual_run_without_derivations_does_not_expand(self) -> None:
        result = self.graph.impact("trace_dual", "parameter:heat_capacity_a")
        self.assertEqual(result["affected_target_ids"], ["parameter:heat_capacity_a"])

    def test_return_values_are_defensive_copies(self) -> None:
        payload = self.graph.to_dict()
        payload["nodes"].clear()
        payload["dependency_edges"].clear()
        impact = self.graph.impact("trace_rc_before", "parameter:cutoff_frequency")
        impact["affected_target_ids"].clear()
        impact["target_impacts"][0]["checks"].clear()
        self.assertTrue(self.graph.to_dict()["nodes"])
        repeated = self.graph.impact("trace_rc_before", "parameter:cutoff_frequency")
        self.assertEqual(repeated["affected_target_ids"], ["parameter:cutoff_frequency", "parameter:capacitance"])
        self.assertTrue(repeated["target_impacts"][0]["checks"])

    def test_unknown_run_and_target_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.graph.impact("missing", "parameter:capacitance")
        with self.assertRaises(ValueError):
            self.graph.impact("trace_rc_before", "parameter:missing")

    def test_tampered_generation_hash_is_rejected(self) -> None:
        run = next(item for item in load_runs(DEFAULT_MANIFEST) if item.run_id == "trace_rc_before")
        run.raw_metrics = dict(run.raw_metrics)
        run.raw_metrics["natural_language_generation_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            DependencyTraceGraph([run])

    def test_tampered_recorded_derivation_fields_are_rejected(self) -> None:
        mutations = (
            lambda item: item.__setitem__("formula", "C=eval(R)"),
            lambda item: item.__setitem__("value", 2.0),
            lambda item: item.__setitem__("unit", "uF"),
            lambda item: item.__setitem__("source_end", item["source_end"] - 1),
            lambda item: item.__setitem__("source_end", 9999),
            lambda item: item.__setitem__("input_names", ["resistance"]),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                run = next(item for item in load_runs(DEFAULT_MANIFEST) if item.run_id == "trace_rc_before")
                evidence = dict(run.raw_metrics["natural_language_generation"])
                derivation = dict(evidence["derivations"][0])
                mutate(derivation)
                evidence["derivations"] = [derivation]
                digest = hashlib.sha256(json.dumps(
                    evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
                ).encode("utf-8")).hexdigest()
                run.raw_metrics = dict(run.raw_metrics)
                run.raw_metrics["natural_language_generation"] = evidence
                run.raw_metrics["natural_language_generation_sha256"] = digest
                run.model_configuration.parameters = dict(run.model_configuration.parameters)
                run.model_configuration.parameters["natural_language_generation"] = evidence
                run.model_configuration.parameters["natural_language_generation_sha256"] = digest
                with self.assertRaises(ValueError):
                    DependencyTraceGraph([run])

    def test_tampered_generation_schema_and_api_count_are_rejected(self) -> None:
        for field, value in (("schema_version", "untrusted"), ("api_call_count", True), ("api_call_count", 1)):
            with self.subTest(field=field, value=value):
                run = next(item for item in load_runs(DEFAULT_MANIFEST) if item.run_id == "trace_rc_before")
                evidence = dict(run.raw_metrics["natural_language_generation"])
                evidence[field] = value
                digest = hashlib.sha256(json.dumps(
                    evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
                ).encode("utf-8")).hexdigest()
                run.raw_metrics = dict(run.raw_metrics)
                run.raw_metrics["natural_language_generation"] = evidence
                run.raw_metrics["natural_language_generation_sha256"] = digest
                run.model_configuration.parameters = dict(run.model_configuration.parameters)
                run.model_configuration.parameters["natural_language_generation"] = evidence
                run.model_configuration.parameters["natural_language_generation_sha256"] = digest
                with self.assertRaises(ValueError):
                    DependencyTraceGraph([run])

    def test_cli_graph_and_unknown_target_exit_codes(self) -> None:
        output, errors = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(dependency_cli.main(["graph"]), 0)
        self.assertIn('"dependency_edges"', output.getvalue())
        output, errors = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(
                dependency_cli.main([
                    "impact", "--run-id", "trace_rc_before", "--target-id", "parameter:missing"
                ]),
                2,
            )
        self.assertIn("unknown target_id", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
