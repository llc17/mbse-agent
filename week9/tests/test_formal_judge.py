from __future__ import annotations

import unittest

from week9.experiments.run_formal_judge import (
    build_formal_judge_report,
    main,
    required_repeats,
    select_formal_records,
)


def _artifacts():
    values = []
    index = 0
    for dataset in ("development", "holdout"):
        case_ids = ("case-a", "case-b") if dataset == "development" else ("case-c",)
        for case_id in case_ids:
            for arm_id, version in (
                ("v4_default_deepseek", "v4"),
                ("v6_default_deepseek", "v6"),
                ("v8_controlled_deepseek", "v8"),
                ("v8_default_local", "v8"),
            ):
                index += 1
                values.append(
                    {
                        "artifact_id": f"artifact-{index:03d}",
                        "dataset": dataset,
                        "case_id": case_id,
                        "version": version,
                        "arm_id": arm_id,
                        "blind_input_file": f"blind-inputs/artifact-{index:03d}.json",
                        "blind_input_sha256": "a" * 64,
                        "blind_content_sha256": f"{index:064x}",
                        "blind_marker_count": 0,
                    }
                )
    return values


def _trials(artifacts, repeats):
    values = []
    sequence = 0
    judge_input_ids = list(
        dict.fromkeys(
            artifact.get("judge_input_id") or artifact["artifact_id"]
            for artifact in artifacts
        )
    )
    for repeat in range(1, repeats + 1):
        for judge_input_id in judge_input_ids:
            sequence += 1
            values.append(
                {
                    "sequence": sequence,
                    "artifact_id": judge_input_id,
                    "judge_input_id": judge_input_id,
                    "repeat": repeat,
                    "valid": True,
                    "provider": "kimi",
                    "model": "frozen-judge",
                    "total": 80 + repeat,
                }
            )
    return values


class FormalJudgeTests(unittest.TestCase):
    def test_repeat_count_follows_stability_policy(self):
        self.assertEqual(required_repeats("SINGLE_SCORE_AUXILIARY_ALLOWED", 3), 1)
        self.assertEqual(required_repeats("REPEATED_MEDIAN_AUXILIARY_REQUIRED", 3), 3)
        self.assertEqual(required_repeats("QUALITATIVE_AUXILIARY_ONLY", 3), 1)

    def test_complete_blinded_report_is_auxiliary_and_cannot_change_hard_verdict(self):
        artifacts = _artifacts()
        report = build_formal_judge_report(
            artifacts=artifacts,
            trials=_trials(artifacts, 3),
            repeats_per_artifact=3,
            stability_policy="REPEATED_MEDIAN_AUXILIARY_REQUIRED",
            protocol_sha256="b" * 64,
            formal_report_sha256="c" * 64,
            stability_report_sha256="d" * 64,
            formal_hard_verdict="FORMAL_FAIL",
            confirmation_id="confirmed",
        )
        self.assertEqual(report["verdict"], "FORMAL_JUDGE_COMPLETE")
        self.assertEqual(report["formal_hard_verdict_unchanged"], "FORMAL_FAIL")
        self.assertTrue(report["numeric_comparison_allowed"])
        self.assertFalse(report["guard"]["hard_verdict_override_allowed"])

    def test_identical_blinded_content_is_scored_once_and_mapped_to_both_arms(self):
        artifacts = _artifacts()
        first = artifacts[0]
        duplicate = artifacts[1]
        duplicate["judge_input_id"] = first["artifact_id"]
        duplicate["blind_content_sha256"] = first["blind_content_sha256"]
        duplicate["blind_input_file"] = first["blind_input_file"]
        duplicate["blind_input_sha256"] = first["blind_input_sha256"]
        report = build_formal_judge_report(
            artifacts=artifacts,
            trials=_trials(artifacts, 3),
            repeats_per_artifact=3,
            stability_policy="REPEATED_MEDIAN_AUXILIARY_REQUIRED",
            protocol_sha256="b" * 64,
            formal_report_sha256="c" * 64,
            stability_report_sha256="d" * 64,
            formal_hard_verdict="FORMAL_PASS",
            confirmation_id="confirmed",
        )
        self.assertEqual(report["verdict"], "FORMAL_JUDGE_COMPLETE")
        self.assertEqual(report["artifact_count"], len(artifacts))
        self.assertEqual(report["unique_judge_input_count"], len(artifacts) - 1)
        self.assertEqual(report["deduplicated_artifact_count"], 1)
        self.assertEqual(report["planned_trial_count"], (len(artifacts) - 1) * 3)
        self.assertTrue(
            report["pass_conditions"]["content_hash_deduplication_integrity"]
        )
        self.assertEqual(
            report["summary_by_dataset_and_arm"]["development"]
            ["v6_default_deepseek"]["artifact_count"],
            2,
        )

    def test_qualitative_policy_disables_numeric_version_comparison(self):
        artifacts = _artifacts()
        report = build_formal_judge_report(
            artifacts=artifacts,
            trials=_trials(artifacts, 1),
            repeats_per_artifact=1,
            stability_policy="QUALITATIVE_AUXILIARY_ONLY",
            protocol_sha256="b" * 64,
            formal_report_sha256="c" * 64,
            stability_report_sha256="d" * 64,
            formal_hard_verdict="FORMAL_PASS",
            confirmation_id="confirmed",
        )
        self.assertEqual(report["verdict"], "FORMAL_JUDGE_COMPLETE")
        self.assertFalse(report["numeric_comparison_allowed"])

    def test_selection_is_fixed_to_first_repetition_and_all_versions(self):
        records = []
        sequence = 0
        for repetition in (1, 2):
            for arm_id, version in (
                ("v4_default_deepseek", "v4"),
                ("v6_default_deepseek", "v6"),
                ("v8_controlled_deepseek", "v8"),
                ("v8_default_local", "v8"),
            ):
                sequence += 1
                records.append(
                    {
                        "sequence": sequence,
                        "dataset": "development",
                        "case_id": "case-a",
                        "version": version,
                        "arm_id": arm_id,
                        "repetition": repetition,
                    }
                )
        selected = select_formal_records({"records": records})
        self.assertEqual(len(selected), 4)
        self.assertTrue(all(item["repetition"] == 1 for item in selected))

    def test_cli_without_execute_online_is_blocked(self):
        self.assertEqual(main([]), 2)


if __name__ == "__main__":
    unittest.main()
