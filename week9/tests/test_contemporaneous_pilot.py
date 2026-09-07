from __future__ import annotations

import unittest

from week9.experiments.run_contemporaneous_pilot import (
    build_pilot_report,
    build_schedule,
    main,
)


def _records(schedule):
    configuration = {
        "provider": "deepseek",
        "model": "frozen-generator",
        "parameters": {
            "temperature": 0.3,
            "max_tokens": 8192,
            "configuration_source": "frozen_legacy_runner",
        },
    }
    return [
        {
            **item,
            "run_id": f"run-{item['sequence']}",
            "result_file": f"runs/{item['sequence']}.json",
            "result_sha256": "a" * 64,
            "contract_valid": True,
            "external_evaluator": "week9.cross_version_validation.v1",
            "overall_success": item["version"] == "v6",
            "infrastructure_failure": False,
            "infrastructure_error_count": 0,
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "duration_s": 2.0,
            "model_configuration": configuration,
            "required_check_status": {},
        }
        for item in schedule
    ]


class ContemporaneousPilotTests(unittest.TestCase):
    def test_schedule_is_complete_paired_and_order_balanced(self):
        schedule = build_schedule(3)
        self.assertEqual(len(schedule), 18)
        pairs = {item["pair_id"] for item in schedule}
        self.assertEqual(len(pairs), 9)
        for pair_id in pairs:
            pair = [item for item in schedule if item["pair_id"] == pair_id]
            self.assertEqual({item["version"] for item in pair}, {"v4", "v6"})
            self.assertEqual({item["order_in_pair"] for item in pair}, {1, 2})
        first_positions = {
            version: sum(
                item["version"] == version and item["order_in_pair"] == 1
                for item in schedule
            )
            for version in ("v4", "v6")
        }
        self.assertLessEqual(abs(first_positions["v4"] - first_positions["v6"]), 1)

    def test_report_passes_only_complete_fair_infrastructure_clean_pilot(self):
        schedule = build_schedule(3)
        report = build_pilot_report(
            schedule=schedule,
            records=_records(schedule),
            repetitions=3,
            protocol_sha256="b" * 64,
            confirmation_id="confirmed-batch",
        )
        self.assertEqual(report["verdict"], "PILOT_PASS")
        self.assertTrue(all(report["pass_conditions"].values()))
        self.assertEqual(report["summary_by_version"]["v4"]["success_rate"], 0.0)
        self.assertEqual(report["summary_by_version"]["v6"]["success_rate"], 1.0)
        self.assertFalse(report["guard"]["holdout_accessed"])

    def test_report_fails_closed_on_provider_mismatch_or_infrastructure(self):
        schedule = build_schedule(3)
        records = _records(schedule)
        records[0] = {
            **records[0],
            "infrastructure_failure": True,
            "model_configuration": {
                **records[0]["model_configuration"],
                "model": "different-model",
            },
        }
        report = build_pilot_report(
            schedule=schedule,
            records=records,
            repetitions=3,
            protocol_sha256="b" * 64,
            confirmation_id="confirmed-batch",
        )
        self.assertEqual(report["verdict"], "PILOT_FAIL")
        self.assertFalse(report["pass_conditions"]["no_infrastructure_failures"])
        self.assertFalse(report["pass_conditions"]["paired_generator_fairness"])

    def test_cli_without_execute_online_is_blocked(self):
        self.assertEqual(main([]), 2)


if __name__ == "__main__":
    unittest.main()
