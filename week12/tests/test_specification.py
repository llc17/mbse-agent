from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from week12.src.specification import (
    SpecificationError,
    assert_generation_ready,
    build_s1_spec,
    confirm_spec,
    load_mission,
    load_spec,
    write_s1_artifact,
)


ROOT = Path(__file__).resolve().parents[1]
MISSION = ROOT / "configs" / "patrol_mission_v1.json"


class SpecificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mission = load_mission(MISSION)
        self.spec = build_s1_spec(self.mission, mission_path=MISSION, project_id="test-project", baseline_version_id="baseline-1")

    def test_payload_and_transition_fields_are_projected(self) -> None:
        self.assertEqual(self.spec["mass"]["payload_kg"], 20.0)
        self.assertGreater(self.spec["mass"]["total_loaded_mass_kg"], 20.0)
        self.assertEqual(self.spec["scenarios"]["descent"]["mode"], "forward_front_wheels_first")
        self.assertIn("last_wheel_leaves_steps", self.spec["scenarios"]["descent"]["exit"])

    def test_pending_spec_is_rejected_by_generation_gate(self) -> None:
        with self.assertRaises(SpecificationError):
            assert_generation_ready(self.spec)

    def test_explicit_user_confirmation_binds_project_baseline_and_digest(self) -> None:
        with self.assertRaises(SpecificationError):
            confirm_spec(self.spec, {"human_confirmed": True, "origin": "user", "project_id": "other-project", "baseline_version_id": "baseline-1", "spec_digest": self.spec["spec_digest"]})
        confirmed = confirm_spec(self.spec, {"human_confirmed": True, "origin": "user", "project_id": "test-project", "baseline_version_id": "baseline-1", "spec_digest": self.spec["spec_digest"]})
        assert_generation_ready(confirmed)
        self.assertEqual(confirmed["status"], "confirmed_for_generation")

    def test_changed_content_invalidates_confirmation(self) -> None:
        confirmed = confirm_spec(self.spec, {"human_confirmed": True, "origin": "user", "project_id": "test-project", "baseline_version_id": "baseline-1", "spec_digest": self.spec["spec_digest"]})
        changed = copy.deepcopy(confirmed)
        changed["stair"]["riser_m"] += 0.001
        with self.assertRaises(SpecificationError):
            assert_generation_ready(changed)

    def test_invalid_descent_mode_is_rejected(self) -> None:
        invalid = copy.deepcopy(self.mission)
        invalid["user_requirements"]["descent_mode"]["value"] = "reverse"
        with self.assertRaises(SpecificationError):
            build_s1_spec(invalid, mission_path=MISSION, project_id="test-project", baseline_version_id="baseline-1")

    def test_artifact_roundtrip_has_no_simulation_or_online_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_s1_artifact(MISSION, output, project_id="test-project", baseline_version_id="baseline-1")
            loaded = load_spec(output / "spec.json")
            evidence = json.loads((output / "validation.json").read_text(encoding="utf-8"))
            self.assertEqual(loaded["s1_budget"]["openmodelica_simulations"], 0)
            self.assertEqual(evidence["simulation_count"], 0)
            self.assertEqual(evidence["online_request_count"], 0)


if __name__ == "__main__":
    unittest.main()
