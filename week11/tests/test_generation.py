from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from week9.src.hard_gate import REQUIRED_HARD_CHECK_TYPES
from week9.src.modelica_templates import render_template
from week9.src.parameter_normalization import normalize_parameters
from week9.src.pipeline import render_canonical_sysml
from week9.src.schemas import (
    ArtifactRecord,
    CheckStatus,
    QualityCheckResult,
    RunResult,
)
from week11.src import generation
from week11.src.spec import spec_digest, validate_spec


WEEK11_ROOT = Path(__file__).resolve().parents[1]


def _rc_spec(*, derived: bool = False) -> dict:
    capacitance = 1.0 / (2.0 * math.pi * 1000.0 * 1000.0)
    if derived:
        text = (
            "请搭一个一阶无源平滑网络。阻值取1 kΩ，目标拐点1 kHz，"
            "电容按 C = 1 / (2 * pi * R * fc) 算；端口喂1 V，零时刻给阶跃。"
        )
    else:
        text = (
            "请搭一个一阶无源平滑网络。阻值取1 kΩ，容量取"
            f"{capacitance} F，目标拐点1 kHz；端口喂1 V，零时刻给阶跃。"
        )
    parameters = [
        {
            "name": "resistance",
            "value": 1.0,
            "unit": "kOhm",
            "source_id": "request",
            "source_text": "阻值取1 kΩ",
            "origin": "direct",
        },
        {
            "name": "cutoff_frequency",
            "value": 1.0,
            "unit": "kHz",
            "source_id": "request",
            "source_text": "目标拐点1 kHz",
            "origin": "direct",
        },
        {
            "name": "input_voltage",
            "value": 1.0,
            "unit": "V",
            "source_id": "request",
            "source_text": "端口喂1 V",
            "origin": "direct",
        },
    ]
    derivations = []
    if derived:
        derivations.append(
            {
                "target": "capacitance",
                "formula": "C = 1 / (2 * pi * R * fc)",
                "input_names": ["resistance", "cutoff_frequency"],
                "source_id": "request",
                "source_text": "电容按 C = 1 / (2 * pi * R * fc) 算",
            }
        )
    else:
        parameters.insert(
            1,
            {
                "name": "capacitance",
                "value": capacitance,
                "unit": "F",
                "source_id": "request",
                "source_text": f"容量取{capacitance} F",
                "origin": "direct",
            },
        )
    return {
        "schema_version": "v10-spec-1",
        "template_id": "rc_filter",
        "sources": [{"id": "request", "text": text}],
        "parameters": parameters,
        "stimulus": {
            "kind": "step_voltage",
            "source_id": "request",
            "source_text": "零时刻给阶跃",
        },
        "derivations": derivations,
        "constraints": ["fixed_certified_template", "rc_time_constant_check"],
        "assumptions": ["使用认证模板固定的零初始电容电压"],
    }


class _FakeTraceGraph:
    def __init__(self) -> None:
        self.run_id = ""

    def add_run(self, run: RunResult) -> str:
        self.run_id = run.run_id
        return self.run_id

    def to_dict(self) -> dict:
        return {
            "schema_version": "v9-evidenced-trace-graph-1",
            "scope": "explicit_structural_correspondence_only",
            "runs": [{"run_id": self.run_id}],
            "nodes": [],
            "edges": [],
        }


class GenerationAdapterTests(unittest.TestCase):
    def _pipeline_factory(self, capture: dict):
        def factory(*, template_id, model_name, observations, workspace_root):
            observations = tuple(observations)
            normalized = normalize_parameters(template_id, observations)
            self.assertTrue(normalized.ready, normalized.issues)
            rendered = render_template(template_id, normalized.parameters, model_name=model_name)
            sysml = render_canonical_sysml(rendered)
            capture.update(
                template_id=template_id,
                observations=observations,
                workspace_root=workspace_root,
            )

            pipeline = SimpleNamespace(
                context=SimpleNamespace(parameters=normalized.parameters),
                canonical_render=rendered,
                canonical_sysml=sysml,
                input_snapshot_sha256="a" * 64,
            )

            def run(*, case_id, run_id, initial_artifacts, config, traceability_input):
                capture.update(
                    case_id=case_id,
                    run_id=run_id,
                    initial_artifacts=initial_artifacts,
                    config=config,
                    traceability_input=traceability_input,
                )
                checks = [
                    QualityCheckResult(name, CheckStatus.PASS, True)
                    for name in REQUIRED_HARD_CHECK_TYPES
                ]
                checks.append(
                    QualityCheckResult("traceability_integrity", CheckStatus.PASS, True)
                )
                result = MagicMock(spec=RunResult)
                result.version = "v8"
                result.case_id = case_id
                result.run_id = run_id
                result.overall_success = True
                result.checks = checks
                result.repairs = []
                result.traceability = SimpleNamespace(
                    trace_input_sha256=traceability_input.sha256,
                    integrity_passed=True,
                )
                result.artifacts = {
                    "requirement": ArtifactRecord("requirement", content=initial_artifacts.requirement),
                    "sysml": ArtifactRecord("sysml", content=initial_artifacts.sysml),
                    "modelica": ArtifactRecord("modelica", content=initial_artifacts.modelica),
                }
                result.to_dict.return_value = {
                    "schema_version": "8.0-stage4",
                    "version": "v8",
                    "case_id": case_id,
                    "run_id": run_id,
                    "overall_success": True,
                }
                return result

            pipeline.run = run
            return pipeline

        return factory

    def test_synonymous_source_enters_structured_pipeline_without_old_parser(self) -> None:
        draft = _rc_spec()
        self.assertTrue(validate_spec(draft)["ready"])
        capture: dict = {}
        with tempfile.TemporaryDirectory(prefix="wp2-test-", dir=WEEK11_ROOT) as temp_dir:
            workspace = Path(temp_dir) / "version"
            with (
                patch.object(
                    generation.CertifiedTemplateRepairPipeline,
                    "from_observations",
                    side_effect=self._pipeline_factory(capture),
                ) as structured_entry,
                patch.object(generation, "TraceGraph", _FakeTraceGraph),
                patch.object(generation, "traceability_report_sha256", return_value="b" * 64),
                patch(
                    "week9.src.generation.DeterministicNaturalLanguageProvider.generate",
                    side_effect=AssertionError("old full-text parser was called"),
                ) as old_parser,
            ):
                result = generation.execute_spec(
                    draft,
                    workspace,
                    project_id="project-rc",
                    run_id="run-001",
                )

            self.assertEqual("succeeded", result["status"])
            self.assertEqual("v8", result["run_result"]["version"])
            self.assertEqual("structured_observations", result["generation_binding"]["pipeline_input_kind"])
            self.assertFalse(result["generation_binding"]["natural_language_parser_used"])
            self.assertFalse(result["generation_binding"]["certification"]["old_certification_covers_v10_adapter"])
            self.assertTrue(result["hard_gate"]["passed"])
            self.assertEqual(0, capture["config"].max_repairs)
            self.assertEqual(0, capture["config"].max_infrastructure_retries)
            trace_input = capture["traceability_input"]
            requirement_text = capture["initial_artifacts"].requirement
            for item in trace_input.requirements:
                self.assertEqual(
                    item.text,
                    requirement_text[item.source_start:item.source_end],
                )
            structured_entry.assert_called_once()
            old_parser.assert_not_called()

            observed = {item.name: item for item in capture["observations"]}
            self.assertEqual(1.0, observed["resistance"].value)
            self.assertEqual("kOhm", observed["resistance"].unit)
            self.assertIn("一阶无源平滑网络", capture["initial_artifacts"].requirement)
            self.assertTrue((workspace / "model.sysml").is_file())
            self.assertTrue((workspace / "model.mo").is_file())
            manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
            self.assertIn("run-result.json", manifest["files"])
            self.assertIn("generation-binding.json", manifest["files"])

    def test_execution_revalidates_and_ignores_tampered_normalized_values(self) -> None:
        canonical = validate_spec(_rc_spec(derived=True))["spec"]
        self.assertIsNotNone(canonical)
        canonical["normalized_parameters"][0]["value"] = 999999.0
        canonical["derived_values"][0]["value"] = 0.75
        capture: dict = {}
        with tempfile.TemporaryDirectory(prefix="wp2-revalidate-", dir=WEEK11_ROOT) as temp_dir:
            with (
                patch.object(
                    generation.CertifiedTemplateRepairPipeline,
                    "from_observations",
                    side_effect=self._pipeline_factory(capture),
                ),
                patch.object(generation, "TraceGraph", _FakeTraceGraph),
                patch.object(generation, "traceability_report_sha256", return_value="b" * 64),
            ):
                result = generation.execute_spec(
                    canonical,
                    Path(temp_dir) / "version",
                    project_id="project-derived",
                    run_id="run-002",
                )

        observations = {item.name: item for item in capture["observations"]}
        self.assertEqual(1.0, observations["resistance"].value)
        expected_c = 1.0 / (2.0 * math.pi * 1000.0 * 1000.0)
        self.assertAlmostEqual(expected_c, observations["capacitance"].value, places=18)
        self.assertIn("C = 1 / (2 * pi * R * fc)", observations["capacitance"].source_text)
        self.assertEqual(spec_digest(_rc_spec(derived=True)), result["spec_digest"])
        derived_binding = next(
            item
            for item in result["generation_binding"]["trace_bindings"]
            if item["target_id"] == "parameter:capacitance"
        )
        self.assertEqual("deterministically_derived_parameter", derived_binding["source_kind"])
        self.assertEqual("request", derived_binding["source_id"])

    def test_invalid_spec_fails_before_pipeline_or_workspace_creation(self) -> None:
        draft = _rc_spec()
        draft["parameters"] = [
            item for item in draft["parameters"] if item["name"] != "input_voltage"
        ]
        with tempfile.TemporaryDirectory(prefix="wp2-invalid-", dir=WEEK11_ROOT) as temp_dir:
            workspace = Path(temp_dir) / "must-not-exist"
            with patch.object(
                generation.CertifiedTemplateRepairPipeline,
                "from_observations",
            ) as structured_entry:
                with self.assertRaises(generation.SpecExecutionError):
                    generation.execute_spec(
                        draft,
                        workspace,
                        project_id="project-invalid",
                        run_id="run-003",
                    )
            structured_entry.assert_not_called()
            self.assertFalse(workspace.exists())

    def test_workspace_must_be_below_week11(self) -> None:
        with self.assertRaisesRegex(ValueError, "inside week11"):
            generation.execute_spec(
                _rc_spec(),
                Path(tempfile.gettempdir()) / "outside-v10",
                project_id="project-safe",
                run_id="run-safe",
            )


if __name__ == "__main__":
    unittest.main()
