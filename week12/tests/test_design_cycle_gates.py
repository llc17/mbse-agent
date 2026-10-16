"""Independent analytic anchors and applicability regressions; no physics run."""
from copy import deepcopy
import json
import math
import unittest

from week12.src.design_preparation.cycle_gates import evaluate_cycle_constraints


def fixed(**updates):
    section = {"main_radius_m": .16, "riser_m": .15, "tread_m": .28,
               "front_tread_index": 2, "main_center_distance_m": None,
               "assumptions": {"ideal_disks": True, "fixed_main_center_distance": True,
                               "front_stays_same_tread_full_rear_arc": True}}
    section.update(updates)
    return {"fixed_main_same_tread": section}


def nose(**updates):
    section = {"nose_radius_m": .1, "riser_m": .15, "tread_m": .28,
               "nose_center_x_m": 2.83, "nose_center_z_m": .55,
               "tread_start_x_m": 2.56, "tread_height_m": .45, "has_next_riser": True,
               "assumptions": {"ideal_disk": True, "nose_remains_on_current_tread": True}}
    section.update(updates)
    return {"nose_next_riser": section}


def corner(**updates):
    section = {"beta_rad": math.asin(.0625), "friction_coefficient": .85,
               "normal_load_N": 100,
               "assumptions": {"single_corner_contact": True, "coulomb_friction": True}}
    section.update(updates)
    return {"corner_traction": section}


class CycleGateTests(unittest.TestCase):
    def evaluate(self, intent):
        before = deepcopy(intent)
        report = evaluate_cycle_constraints(intent)
        json.dumps(report, allow_nan=False)
        # NaN does not compare equal; immutability is separately tested below.
        if not any(isinstance(v, float) and math.isnan(v) for s in intent.values()
                   if isinstance(s, dict) for v in s.values()):
            self.assertEqual(intent, before)
        return report

    def test_historical_fixed_distance_anchor(self):
        report = self.evaluate(fixed())
        self.assertEqual(report["status"], "CONFLICT")
        d = report["checks"][0]["derived"]
        self.assertAlmostEqual(d["start_L_interval_m"][0], .5322826587133563, places=12)
        self.assertAlmostEqual(d["end_L_interval_m"][1], .427493090547767, places=12)
        self.assertIsNone(d["endpoint_L_intersection_m"])
        self.assertAlmostEqual(d["necessary_effective_center_distance_variation_lower_bound_m"],
                               .10478956816558932, places=12)

    def test_long_tread_intersects_both_complete_intervals(self):
        report = self.evaluate(fixed(tread_m=.6))
        self.assertEqual(report["status"], "NECESSARY_CONDITIONS_ONLY")
        d = report["checks"][0]["derived"]
        self.assertEqual(d["endpoint_L_intersection_m"],
                         [max(d["start_L_interval_m"][0], d["end_L_interval_m"][0]),
                          min(d["start_L_interval_m"][1], d["end_L_interval_m"][1])])
        self.assertAlmostEqual(d["start_L_interval_m"][1], math.hypot(1.2, .3))
        self.assertAlmostEqual(d["end_L_interval_m"][0], math.hypot(.6, .15))
        lo, hi = d["endpoint_L_intersection_m"]
        for value in (lo, hi, (lo + hi) / 2):
            self.assertEqual(self.evaluate(fixed(tread_m=.6, main_center_distance_m=value))["status"],
                             "NECESSARY_CONDITIONS_ONLY")
        for value in (lo - .001, hi + .001):
            self.assertEqual(self.evaluate(fixed(tread_m=.6, main_center_distance_m=value))["status"],
                             "CONFLICT")

    def test_no_fixed_refutation_of_activity_or_partial_staggering(self):
        for name in ("fixed_main_center_distance", "front_stays_same_tread_full_rear_arc", "ideal_disks"):
            intent = fixed()
            intent["fixed_main_same_tread"]["assumptions"][name] = False
            report = self.evaluate(intent)
            self.assertEqual(report["status"], "REQUIRES_ANALYSIS")
            self.assertNotIn("endpoint_L_intersection_m", report["checks"][0]["derived"])

    def test_fixed_geometry_domain_and_empty_window(self):
        self.assertEqual(self.evaluate(fixed(riser_m=.17))["status"], "INVALID")
        self.assertEqual(self.evaluate(fixed(tread_m=.1))["status"], "CONFLICT")
        for value in (True, 1.0, 0, -1):
            self.assertEqual(self.evaluate(fixed(front_tread_index=value))["status"], "INVALID")
        self.assertEqual(self.evaluate(fixed(riser_m=.16, tread_m=.6))["status"],
                         "NECESSARY_CONDITIONS_ONLY")

    def test_next_riser_historical_90mm_anchor(self):
        report = self.evaluate(nose())
        self.assertEqual(report["status"], "CONFLICT")
        d = report["checks"][0]["derived"]
        self.assertEqual(d["next_obstruction_mode"], "vertical_riser")
        self.assertAlmostEqual(d["next_riser_center_limit_x_m"], 2.74)
        self.assertAlmostEqual(d["next_riser_bound_violation_m"], .09)
        self.assertAlmostEqual(d["disk_distance_to_next_riser_segment_m"], .01)
        self.assertFalse(d["disk_clear_of_next_step"])

    def test_small_and_large_nose_use_different_obstructions(self):
        self.assertEqual(self.evaluate(nose(nose_center_x_m=2.74))["status"],
                         "NECESSARY_CONDITIONS_ONLY")
        report = self.evaluate(nose(nose_radius_m=.2, nose_center_z_m=.65, nose_center_x_m=2.6))
        d = report["checks"][0]["derived"]
        self.assertEqual(d["next_obstruction_mode"], "upper_corner")
        self.assertAlmostEqual(d["next_riser_center_limit_x_m"], 2.84 - math.sqrt(.0375))
        self.assertAlmostEqual(d["disk_distance_to_next_riser_segment_m"], math.hypot(.24, .05))
        self.assertEqual(report["status"], "NECESSARY_CONDITIONS_ONLY")
        self.assertEqual(self.evaluate(nose(nose_radius_m=.15, nose_center_z_m=.6,
                                             nose_center_x_m=2.69))["checks"][0]["derived"]["next_obstruction_mode"],
                         "vertical_riser")

    def test_platform_and_lost_contact_do_not_prove_cycle(self):
        report = self.evaluate(nose(has_next_riser=False))
        self.assertEqual(report["status"], "REQUIRES_ANALYSIS")
        self.assertNotIn("next_riser_center_limit_x_m", report["checks"][0]["derived"])
        report = self.evaluate(nose(nose_center_z_m=.8))
        self.assertEqual(report["status"], "REQUIRES_ANALYSIS")
        self.assertNotIn("next_riser_center_limit_x_m", report["checks"][0]["derived"])
        self.assertEqual(self.evaluate(nose(nose_center_x_m=2.5))["status"], "CONFLICT")
        self.assertEqual(self.evaluate(nose(nose_center_x_m=3.1))["status"], "CONFLICT")

    def test_corner_force_and_zero_friction_anchors(self):
        report = self.evaluate(corner())
        d = report["checks"][0]["derived"]
        self.assertEqual(report["status"], "REQUIRES_ANALYSIS")
        self.assertAlmostEqual(d["maximum_forward_force_N"], -94.4919963916957)
        self.assertAlmostEqual(math.degrees(d["nonnegative_forward_cone_beta_rad"]), 49.635463426902646)
        report = self.evaluate(corner(beta_rad=1.2))
        self.assertEqual(report["status"], "NECESSARY_CONDITIONS_ONLY")
        self.assertTrue(report["checks"][0]["derived"]["positive_forward_force_possible_at_positive_normal"])
        report = self.evaluate(corner(beta_rad=0, friction_coefficient=0))
        self.assertEqual(report["checks"][0]["derived"]["maximum_forward_force_per_normal_load"], -1)
        self.assertEqual(report["checks"][0]["derived"]["nonnegative_forward_cone_beta_rad"], math.pi / 2)
        report = self.evaluate(corner(beta_rad=math.pi / 2, friction_coefficient=0, normal_load_N=0))
        self.assertEqual(report["status"], "NECESSARY_CONDITIONS_ONLY")
        self.assertEqual(report["checks"][0]["derived"]["maximum_forward_force_N"], 0)
        self.assertFalse(report["checks"][0]["derived"]["positive_forward_force_possible_at_positive_normal"])

    def test_missing_and_malformed_inputs_are_strict_json_invalid(self):
        self.assertEqual(self.evaluate({})["status"], "REQUIRES_ANALYSIS")
        for intent in (None, [], {"unknown": {}}, {"fixed_main_same_tread": {}},
                       {"corner_traction": {"beta_rad": 0, "friction_coefficient": .8}}):
            report = evaluate_cycle_constraints(intent)
            json.dumps(report, allow_nan=False)
            self.assertEqual(report["status"], "INVALID")
        for value in (float("nan"), float("inf"), -float("inf"), True, "0.16", 0, -1):
            self.assertEqual(self.evaluate(fixed(main_radius_m=value))["status"], "INVALID")
        for intent in (corner(beta_rad=-.1), corner(beta_rad=math.pi), corner(friction_coefficient=-1),
                       corner(normal_load_N=True), nose(has_next_riser=1), fixed(main_center_distance_m=True)):
            self.assertEqual(self.evaluate(intent)["status"], "INVALID")
        intent = fixed()
        intent["fixed_main_same_tread"]["assumptions"]["fixed_main_center_distance"] = 1
        self.assertEqual(self.evaluate(intent)["status"], "INVALID")
        intent = fixed()
        del intent["fixed_main_same_tread"]["assumptions"]["fixed_main_center_distance"]
        self.assertEqual(self.evaluate(intent)["status"], "INVALID")
        self.assertEqual(self.evaluate(fixed(main_radius_m=10 ** 1000))["status"], "INVALID")
        self.assertEqual(self.evaluate(fixed(tread_m=1e308, front_tread_index=100))["status"], "INVALID")

    def test_aggregate_precedence_and_no_pass(self):
        report = self.evaluate({**fixed(tread_m=.6), **corner(beta_rad=1.2)})
        self.assertEqual(report["status"], "NECESSARY_CONDITIONS_ONLY")
        self.assertNotIn("PASS", json.dumps(report))
        self.assertEqual(self.evaluate({**fixed(tread_m=.6), **nose(has_next_riser=False)})["status"],
                         "REQUIRES_ANALYSIS")
        self.assertEqual(self.evaluate({**fixed(), **nose(has_next_riser=False)})["status"], "CONFLICT")
        self.assertEqual(self.evaluate({**fixed(), **corner(beta_rad=True)})["status"], "INVALID")


if __name__ == "__main__":
    unittest.main()
