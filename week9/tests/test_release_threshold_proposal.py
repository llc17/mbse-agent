from __future__ import annotations

from copy import deepcopy
import unittest

from week9.experiments.propose_release_thresholds import derive_thresholds


def _pilot():
    return {
        "schema_version": "v8-contemporaneous-pilot-1",
        "verdict": "PILOT_PASS",
        "guard": {
            "versions": ["v4", "v6"],
            "development_cases_only": True,
            "holdout_accessed": False,
            "external_evaluator": "week9.cross_version_validation.v1",
        },
        "pass_conditions": {
            "schedule_complete": True,
            "paired_generator_fairness": True,
        },
        "summary_by_version": {
            "v6": {
                "run_count": 9,
                "success_rate": 5 / 9,
                "simple_case_total_tokens": {"median": 1200},
                "simple_case_duration_s": {"median": 30.5},
                "content_repair_count": {"maximum": 0},
            }
        },
    }


class ReleaseThresholdProposalTests(unittest.TestCase):
    def test_thresholds_are_derived_only_from_baseline_statistics(self):
        result = derive_thresholds(_pilot())
        thresholds = result["thresholds"]
        self.assertAlmostEqual(
            thresholds["development_success_improvement_min"], 1 / 9
        )
        self.assertEqual(thresholds["holdout_noninferiority_margin"], 0.0)
        self.assertEqual(thresholds["simple_case_token_budget"], 1200)
        self.assertEqual(thresholds["simple_case_duration_budget_s"], 30.5)
        self.assertEqual(thresholds["max_content_repairs"], 1)

    def test_failed_or_holdout_tainted_pilot_is_rejected(self):
        failed = deepcopy(_pilot())
        failed["verdict"] = "PILOT_FAIL"
        with self.assertRaises(ValueError):
            derive_thresholds(failed)
        tainted = deepcopy(_pilot())
        tainted["guard"]["holdout_accessed"] = True
        with self.assertRaises(ValueError):
            derive_thresholds(tainted)

    def test_zero_or_missing_cost_baseline_is_rejected(self):
        invalid = deepcopy(_pilot())
        invalid["summary_by_version"]["v6"]["simple_case_total_tokens"]["median"] = 0
        with self.assertRaises(ValueError):
            derive_thresholds(invalid)


if __name__ == "__main__":
    unittest.main()
