from __future__ import annotations

import unittest

from week9.experiments.run_formal_comparison import (
    build_formal_report,
    build_schedule,
    main,
)


CONFIGURATION = {
    "provider": "deepseek",
    "model": "frozen-generator",
    "temperature": 0.3,
    "max_tokens": 8192,
}


def _protocol():
    return {
        "repetitions": {
            "smoke_or_exploratory_min": 3,
            "formal_recommended": 5,
        },
        "release_thresholds": {
            "known_hard_error_false_release_rate_max": 0.0,
            "development_success_improvement_min": 0.1,
            "holdout_noninferiority_margin": 0.0,
            "simple_case_token_budget": 100,
            "simple_case_duration_budget_s": 10.0,
            "max_content_repairs": 1,
        },
    }


def _records(schedule):
    records = []
    for item in schedule:
        success = item["version"] == "v8" or (
            item["dataset"] == "holdout" and item["version"] == "v6"
        )
        budget_rules = {"content_repairs_within_limit": True}
        if item["case_id"] == "rc_lowpass":
            budget_rules.update(
                {
                    "simple_tokens_within_limit": True,
                    "simple_duration_within_limit": True,
                }
            )
        records.append(
            {
                **item,
                "run_id": f"run-{item['sequence']}",
                "result_file": f"runs/{item['sequence']}.json",
                "result_sha256": "a" * 64,
                "contract_valid": True,
                "external_evaluator": "week9.cross_version_validation.v1",
                "overall_success": success,
                "infrastructure_failure": False,
                "infrastructure_error_count": 0,
                "prompt_tokens": 30,
                "completion_tokens": 20,
                "total_tokens": 50,
                "duration_s": 2.0,
                "content_repair_count": 0,
                "generator_configuration": (
                    {
                        "provider": "v8_deterministic_local_grammar",
                        "model": "certified_requirement_grammar_v1",
                        "temperature": None,
                        "max_tokens": 0,
                    }
                    if item["arm_id"] == "v8_default_local"
                    else dict(CONFIGURATION)
                ),
                "candidate_online_api_used": (
                    False if item["arm_id"] == "v8_default_local" else None
                ),
                "budget_rules": budget_rules,
                "equal_budget_success": success,
                "required_check_status": {},
            }
        )
    return records


class FormalComparisonTests(unittest.TestCase):
    def test_schedule_covers_both_datasets_and_rotates_four_arms(self):
        schedule = build_schedule(3)
        self.assertEqual(len(schedule), 60)
        self.assertEqual({item["dataset"] for item in schedule}, {"development", "holdout"})
        groups = {item["group_id"] for item in schedule}
        self.assertEqual(len(groups), 15)
        for group in groups:
            selected = [item for item in schedule if item["group_id"] == group]
            self.assertEqual({item["version"] for item in selected}, {"v4", "v6", "v8"})
            self.assertEqual(
                {item["arm_id"] for item in selected},
                {
                    "v4_default_deepseek",
                    "v6_default_deepseek",
                    "v8_controlled_deepseek",
                    "v8_default_local",
                },
            )
            self.assertEqual({item["order_in_group"] for item in selected}, {1, 2, 3, 4})

    def test_complete_fair_hard_clean_report_passes_as_preliminary_at_three_repeats(self):
        schedule = build_schedule(3)
        report = build_formal_report(
            schedule=schedule,
            records=_records(schedule),
            repetitions=3,
            protocol=_protocol(),
            protocol_sha256="b" * 64,
            pilot_sha256="c" * 64,
            judge_sha256="d" * 64,
            certification_sha256="e" * 64,
            confirmation_id="confirmed",
        )
        self.assertEqual(report["verdict"], "FORMAL_PASS")
        self.assertEqual(report["conclusion_strength"], "PRELIMINARY")
        self.assertTrue(all(report["pass_conditions"].values()))
        self.assertTrue(all(report["release_conditions"].values()))
        self.assertEqual(
            report["summary_by_dataset_and_arm"]["development"][
                "v8_controlled_deepseek"
            ][
                "default_success_rate"
            ],
            1.0,
        )

    def test_generator_mismatch_or_infrastructure_invalidates_formal_report(self):
        schedule = build_schedule(3)
        records = _records(schedule)
        records[0] = {
            **records[0],
            "infrastructure_failure": True,
            "generator_configuration": {
                **records[0]["generator_configuration"],
                "model": "different-model",
            },
        }
        report = build_formal_report(
            schedule=schedule,
            records=records,
            repetitions=3,
            protocol=_protocol(),
            protocol_sha256="b" * 64,
            pilot_sha256="c" * 64,
            judge_sha256="d" * 64,
            certification_sha256="e" * 64,
            confirmation_id="confirmed",
        )
        self.assertEqual(report["verdict"], "FORMAL_FAIL")
        self.assertFalse(report["pass_conditions"]["no_infrastructure_failures"])
        self.assertFalse(report["pass_conditions"]["paired_generator_fairness"])

    def test_default_local_view_has_its_own_quality_floor(self):
        schedule = build_schedule(3)
        records = _records(schedule)
        for record in records:
            if record["arm_id"] == "v8_default_local":
                record["overall_success"] = False
                record["equal_budget_success"] = False
        report = build_formal_report(
            schedule=schedule,
            records=records,
            repetitions=3,
            protocol=_protocol(),
            protocol_sha256="b" * 64,
            pilot_sha256="c" * 64,
            judge_sha256="d" * 64,
            certification_sha256="e" * 64,
            confirmation_id="confirmed",
        )
        self.assertTrue(
            report["release_conditions"][
                "controlled_development_improvement_met"
            ]
        )
        self.assertFalse(
            report["release_conditions"]["default_development_improvement_met"]
        )
        self.assertEqual(report["verdict"], "FORMAL_FAIL")

    def test_default_local_view_must_prove_no_online_api_use(self):
        schedule = build_schedule(3)
        records = _records(schedule)
        local = next(
            item for item in records if item["arm_id"] == "v8_default_local"
        )
        local["candidate_online_api_used"] = True
        report = build_formal_report(
            schedule=schedule,
            records=records,
            repetitions=3,
            protocol=_protocol(),
            protocol_sha256="b" * 64,
            pilot_sha256="c" * 64,
            judge_sha256="d" * 64,
            certification_sha256="e" * 64,
            confirmation_id="confirmed",
        )
        self.assertFalse(report["pass_conditions"]["v8_default_local_is_offline"])
        self.assertEqual(report["verdict"], "FORMAL_FAIL")

    def test_cli_without_execute_online_is_blocked(self):
        self.assertEqual(main([]), 2)


if __name__ == "__main__":
    unittest.main()
