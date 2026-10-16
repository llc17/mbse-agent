from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from week12.src.s2_runner import S2Runner, S2RunnerError
from week12.src.specification import build_s1_spec, confirm_spec, load_mission


ROOT = Path(__file__).resolve().parents[1]
RESULTS_ROOT = ROOT / "results"


class FakeGenerator:
    def __init__(self):
        self.calls = []
        self.states = []

    def generate(self, spec, scenario, scenario_dir):
        self.calls.append(scenario)
        self.states.append(json.loads((scenario_dir.parent / "run-state.json").read_text(encoding="utf-8")))
        return {"scenario": scenario, "model": "fake"}


class FakeExecutor:
    def __init__(self):
        self.calls = []
        self.states = []

    def execute(self, scenario_input, generated, scenario_dir):
        scenario = scenario_input["scenario"]
        self.calls.append(scenario)
        self.states.append(json.loads((scenario_dir.parent / "run-state.json").read_text(encoding="utf-8")))
        return {"tool_status": "PASS", "power": {"peak_w": 100}, "continuous_capability": {"duration_s": 10}}


def confirmed_spec() -> dict:
    mission_path = ROOT / "configs" / "patrol_mission_v1.json"
    spec = build_s1_spec(load_mission(mission_path), mission_path=mission_path, project_id="synthetic-s2-project", baseline_version_id="synthetic-s2-baseline")
    return confirm_spec(spec, {
        "human_confirmed": True,
        "origin": "user",
        "project_id": "synthetic-s2-project",
        "baseline_version_id": "synthetic-s2-baseline",
        "spec_digest": spec["spec_digest"],
        "confirmed_at": "2026-09-18T00:00:00Z",
        "synthetic_confirmation": True,
    })


class S2RunnerTests(unittest.TestCase):
    def test_three_scenarios_persist_evidence_and_keep_physical_unverified(self):
        with tempfile.TemporaryDirectory(dir=RESULTS_ROOT) as tmp:
            spec_path = Path(tmp) / "spec.json"
            spec_path.write_text(json.dumps(confirmed_spec(), ensure_ascii=False), encoding="utf-8")
            generator, executor = FakeGenerator(), FakeExecutor()
            result = S2Runner(generator, executor).run(spec_path, Path(tmp) / "run")
            self.assertEqual(generator.calls, ["ascent", "descent", "flat"])
            self.assertEqual(executor.calls, ["ascent", "descent", "flat"])
            self.assertEqual(result["usage"], {"generator_calls": 3, "executor_calls": 3})
            self.assertEqual([state["usage"]["generator_calls"] for state in generator.states], [1, 2, 3])
            self.assertEqual([state["usage"]["executor_calls"] for state in executor.states], [1, 2, 3])
            self.assertTrue(all(item["physical_status"] == "UNVERIFIED" for item in result["scenarios"].values()))
            root = Path(tmp) / "run" / result["candidate_id"]
            self.assertTrue((root / "manifest.json").is_file())
            self.assertTrue((root / "descent" / "input.json").is_file())
            self.assertEqual(json.loads((root / "flat" / "record.json").read_text())["tool_status"], "PASS")

    def test_unconfirmed_spec_is_rejected_before_generator(self):
        with tempfile.TemporaryDirectory(dir=RESULTS_ROOT) as tmp:
            spec = confirmed_spec()
            spec["status"] = "pending_user_confirmation"
            path = Path(tmp) / "spec.json"
            path.write_text(json.dumps(spec), encoding="utf-8")
            generator, executor = FakeGenerator(), FakeExecutor()
            with self.assertRaises(Exception):
                S2Runner(generator, executor).run(path, Path(tmp) / "run")
            self.assertEqual(generator.calls, [])

    def test_budget_cannot_silently_skip_a_scenario(self):
        from week12.src.s2_runner import S2Budget
        with tempfile.TemporaryDirectory(dir=RESULTS_ROOT) as tmp:
            path = Path(tmp) / "spec.json"
            path.write_text(json.dumps(confirmed_spec()), encoding="utf-8")
            with self.assertRaises(S2RunnerError):
                S2Runner(FakeGenerator(), FakeExecutor(), budget=S2Budget(max_executor_calls=2)).run(path, Path(tmp) / "run")

    def test_explicit_physical_criteria_are_recorded_separately(self):
        class PhysicalExecutor(FakeExecutor):
            def execute(self, scenario_input, generated, scenario_dir):
                return {"tool_status": "PASS", "physical_criteria": [{"id": "contact", "status": "PASS"}], "power_and_continuous_capability": {"peak_w": 50}}

        with tempfile.TemporaryDirectory(dir=RESULTS_ROOT) as tmp:
            path = Path(tmp) / "spec.json"
            path.write_text(json.dumps(confirmed_spec()), encoding="utf-8")
            result = S2Runner(FakeGenerator(), PhysicalExecutor()).run(path, Path(tmp) / "run")
            self.assertTrue(all(item["physical_status"] == "PASS" for item in result["scenarios"].values()))
            self.assertEqual(result["power_continuous_capability"]["scenarios"]["flat"]["power_and_continuous_capability"]["peak_w"], 50)

    def test_non_pass_tool_blocks_explicit_physical_pass(self):
        class FailedToolExecutor(FakeExecutor):
            def execute(self, scenario_input, generated, scenario_dir):
                return {"tool_status": "FAIL", "physical_status": "PASS"}

        with tempfile.TemporaryDirectory(dir=RESULTS_ROOT) as tmp:
            path = Path(tmp) / "spec.json"
            path.write_text(json.dumps(confirmed_spec()), encoding="utf-8")
            result = S2Runner(FakeGenerator(), FailedToolExecutor()).run(path, Path(tmp) / "run")
            self.assertTrue(all(item["physical_status"] == "UNVERIFIED" for item in result["scenarios"].values()))

    def test_existing_or_outside_output_is_rejected_before_writes(self):
        with tempfile.TemporaryDirectory(dir=RESULTS_ROOT) as tmp:
            path = Path(tmp) / "spec.json"
            path.write_text(json.dumps(confirmed_spec()), encoding="utf-8")
            existing = Path(tmp) / "existing"
            existing.mkdir()
            generator = FakeGenerator()
            with self.assertRaises(FileExistsError):
                S2Runner(generator, FakeExecutor()).run(path, existing)
            self.assertEqual(generator.calls, [])
            outside = Path(tempfile.gettempdir()) / "s2-outside-output"
            with self.assertRaises(S2RunnerError):
                S2Runner(generator, FakeExecutor()).run(path, outside)
            self.assertEqual(generator.calls, [])

    def test_keyboard_interrupt_stops_and_keeps_manifest(self):
        class InterruptingExecutor(FakeExecutor):
            def execute(self, scenario_input, generated, scenario_dir):
                super().execute(scenario_input, generated, scenario_dir)
                raise KeyboardInterrupt()

        with tempfile.TemporaryDirectory(dir=RESULTS_ROOT) as tmp:
            path = Path(tmp) / "spec.json"
            path.write_text(json.dumps(confirmed_spec()), encoding="utf-8")
            result = S2Runner(FakeGenerator(), InterruptingExecutor()).run(path, Path(tmp) / "run")
            root = Path(tmp) / "run" / result["candidate_id"]
            self.assertTrue(result["interrupted"])
            self.assertTrue((root / "manifest.json").is_file())
            self.assertEqual(result["scenarios"]["ascent"]["interrupted"], True)
            self.assertEqual(result["scenarios"]["descent"]["skipped"], "run_interrupted")


if __name__ == "__main__":
    unittest.main()
