"""New C preparation checks only; no A/B suite, physical solver or product call."""
from copy import deepcopy
from pathlib import Path
import math
import unittest
from unittest.mock import patch

from week12.src.composition.examples import make_example, synthetic_receipt
from week12.src.composition.schema import ROOT, seal_design
from week12.src.design_preparation import prepare_design, write_preparation


def task_for(design):
    scenario = design["scenario"]["ascent"]
    return {"wheel_count": sum(w["count"] for w in design["wheels"]), "payload_mass_kg": 20,
            "stair_riser_m": scenario["riser"]["value"], "stair_tread_m": scenario["tread"]["value"],
            "stair_steps": scenario["steps"]["value"], "mission": ["ascent", "descent", "flat"],
            "descent_mode": "forward_front_wheels_first"}


class PreparationChecks(unittest.TestCase):
    def test_missing_facts_concentrated_before_mapping(self):
        with patch("week12.src.design_preparation.pipeline.binding_preview") as mapping:
            result = prepare_design({"wheel_count": 6}, None)
        mapping.assert_not_called()
        self.assertEqual(result["status"], "NEEDS_TASK_FACTS")
        self.assertEqual(len(result["diagnostics"]), 6)
        self.assertFalse(result["executable"])

    def test_task_only_needs_agent_design_not_user_mechanical_answers(self):
        d = make_example("fixed_nose")
        r = prepare_design(task_for(d), None)
        self.assertEqual(r["status"], "NEEDS_AGENT_DESIGN")
        self.assertNotIn("design", r)

    def test_parameters_and_topology_flow_through_existing_mapping(self):
        a, b = (make_example(n) for n in ("fixed_nose", "rocker_bogie"))
        rb = prepare_design(task_for(b), b)
        a["design_id"] = "unseen_pending_input"
        for name in ("ascent", "descent"):
            a["scenario"][name]["tread"]["value"] = .31
        a = seal_design(a)
        ra = prepare_design(task_for(a), a)
        self.assertEqual(ra["status"], "NEEDS_DESIGN_ANALYSIS")
        self.assertNotEqual(ra["structure_ir"]["topology_digest"], rb["structure_ir"]["topology_digest"])
        self.assertEqual(ra["structure_ir"]["scenario"]["ascent"]["tread"]["value"], .31)
        self.assertEqual(len(rb["physical_descriptor_contract"]["passive_joints"]), 2)
        self.assertFalse(ra["executable"])

    def test_hard_mass_stairs_and_count_are_not_silently_changed(self):
        d = make_example("fixed_nose")
        for key, value in (("wheel_count", 4), ("stair_tread_m", .29), ("total_loaded_mass_kg", 85)):
            task = task_for(d)
            task[key] = value
            with self.subTest(key=key):
                r = prepare_design(task, d)
                self.assertEqual(r["status"], "TASK_DESIGN_MISMATCH")
                self.assertNotIn("component_binding_preview", r)

    def test_approval_is_not_inherited_and_identity_is_deterministic(self):
        d = synthetic_receipt(make_example("fixed_nose"))
        original = deepcopy(d)
        r = prepare_design(task_for(d), d)
        self.assertEqual(d, original)
        self.assertEqual(r["design"]["confirmation"], {"origin": "pending", "human_confirmed": False})
        self.assertEqual(r, prepare_design(task_for(d), d))
        self.assertEqual(r["execution"], {"simulations": 0, "SysML": 0, "product_online": 0})

    def test_actual_passive_topology_does_not_receive_fixed_distance_rejection(self):
        d = make_example("rocker_bogie")
        intent = {"fixed_main_same_tread": {"rear_wheel_id": "rear", "front_wheel_id": "front", "front_tread_index": 2,
                  "assumptions": {"ideal_disks": True, "fixed_main_center_distance": True,
                                  "front_stays_same_tread_full_rear_arc": True}}}
        r = prepare_design(task_for(d), d, intent)
        self.assertFalse(r["bound_cycle_inputs"]["fixed_main_same_tread"]["assumptions"]["fixed_main_center_distance"])
        self.assertEqual(r["cycle_checks"]["status"], "REQUIRES_ANALYSIS")
        self.assertEqual(r["status"], "NEEDS_DESIGN_ANALYSIS")

    def test_fixed_topology_cannot_hide_conflict_by_claiming_variable_distance(self):
        d = make_example("fixed_nose")
        intent = {"fixed_main_same_tread": {"rear_wheel_id": "rear", "front_wheel_id": "front", "front_tread_index": 2,
                  "assumptions": {"ideal_disks": True, "fixed_main_center_distance": False,
                                  "front_stays_same_tread_full_rear_arc": True}}}
        r = prepare_design(task_for(d), d, intent)
        self.assertTrue(r["bound_cycle_inputs"]["fixed_main_same_tread"]["assumptions"]["fixed_main_center_distance"])
        self.assertEqual(r["status"], "BLOCKED_CONTACT_MODE")

    def test_partial_cycle_and_backwards_corner_never_claim_vehicle_success(self):
        d = make_example("fixed_nose")
        intent = {"corner_traction": {"wheel_id": "rear", "scenario": "ascent", "beta_rad": math.asin(.0625),
                  "normal_load_N": 100, "assumptions": {"single_corner_contact": True, "coulomb_friction": True}}}
        r = prepare_design(task_for(d), d, intent)
        self.assertEqual(r["status"], "NEEDS_DESIGN_ANALYSIS")
        self.assertNotEqual(r["cycle_checks"]["status"], "CONFLICT")
        self.assertNotIn("physical_status", r)

    def test_cycle_evidence_bound_to_actual_stairs_and_wheel(self):
        d = make_example("fixed_nose")
        intent = {"nose_next_riser": {"wheel_id": "nose", "tread_index": 3, "nose_center_x_m": .77,
                  "nose_center_z_m": .57, "coordinate_frame": "relative_first_upper_edge",
                  "assumptions": {"ideal_disk": True, "nose_remains_on_current_tread": True}}}
        r = prepare_design(task_for(d), d, intent)
        bound = r["bound_cycle_inputs"]["nose_next_riser"]
        self.assertTrue(bound["has_next_riser"])
        self.assertEqual(bound["nose_radius_m"], .12)
        self.assertAlmostEqual(bound["tread_height_m"], .45)
        self.assertEqual(r["status"], "BLOCKED_CONTACT_MODE")

    def test_invalid_types_and_stale_drafts_are_reports(self):
        d = make_example("fixed_nose")
        task = task_for(d)
        for key, value in (("wheel_count", True), ("stair_steps", 2.5), ("stair_tread_m", float("nan")), ("mission", ["ascent", "ascent"])):
            t = deepcopy(task)
            t[key] = value
            with self.subTest(key=key):
                self.assertEqual(prepare_design(t, d)["status"], "INVALID_INPUT")
        d["wheels"][0]["radius"]["value"] += .001
        self.assertEqual(prepare_design(task, d)["status"], "INVALID_DESIGN")

    def test_bad_references_are_diagnostics_not_exceptions(self):
        d = make_example("fixed_nose")
        intent = {"fixed_main_same_tread": {"rear_wheel_id": [], "front_wheel_id": "front", "front_tread_index": 2,
                  "assumptions": {"ideal_disks": True}}}
        self.assertEqual(prepare_design(task_for(d), d, intent)["status"], "INVALID_CYCLE_INTENT")

    def test_export_rejects_changed_report_and_external_target_before_writing(self):
        d = make_example("fixed_nose")
        r = prepare_design(task_for(d), d)
        with self.assertRaisesRegex(ValueError, "inside week12/results"):
            write_preparation(r, ROOT.parent/"week11"/"must_not_create")
        changed = deepcopy(r)
        changed["task"]["stair_tread_m"] = .9
        with self.assertRaisesRegex(ValueError, "STALE"):
            write_preparation(changed, ROOT/"results"/"must_not_create")


if __name__ == "__main__":
    unittest.main()
