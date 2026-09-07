from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest

from week9.experiments.experiment_readiness import build_readiness_report
from week9.experiments.freeze_protocol import DEFAULT_PROTOCOL
import json


class ExperimentReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.protocol = json.loads(DEFAULT_PROTOCOL.read_text(encoding="utf-8"))

    def test_current_protocol_is_honestly_blocked(self):
        report = build_readiness_report(
            protocol=self.protocol,
            protocol_errors=[],
            environment={
                "DEEPSEEK_API_KEY": "secret-generator",
                "KIMI_API_KEY": "secret-judge",
            },
        )
        self.assertEqual(report["formal_experiment"]["status"], "BLOCKED")
        self.assertEqual(report["release_thresholds"]["status"], "FROZEN")
        self.assertEqual(report["online_execution"]["status"], "DISABLED")
        self.assertEqual(report["holdout"]["status"], "LOCKED_AFTER_ACCESS")
        self.assertTrue(report["holdout"]["historical_formal_access_recorded"])
        self.assertEqual(report["holdout"]["historical_holdout_run_count"], 8)
        self.assertEqual(report["formal_result"]["status"], "INCOMPLETE")
        self.assertEqual(
            report["formal_result"]["semantics"],
            "aborted_before_formal_report",
        )
        self.assertEqual(
            report["formal_result"]["incomplete_attempt"]["completed_run_count"],
            22,
        )
        self.assertEqual(
            report["formal_result"]["incomplete_attempt"]["planned_run_count"],
            100,
        )
        self.assertTrue(
            report["formal_result"]["incomplete_attempt"]["holdout_accessed"]
        )
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("secret-generator", serialized)
        self.assertNotIn("secret-judge", serialized)
        self.assertFalse(
            report["online_execution"]["credential_values_recorded"]
        )

    def test_online_flags_cannot_override_missing_evidence(self):
        protocol = deepcopy(self.protocol)
        protocol["formal_online_runs_allowed"] = True
        protocol["holdout_policy"]["access_enabled"] = True
        protocol["online_execution_policy"].update(
            {
                "current_milestone_allows_online_calls": True,
                "user_confirmation_id": "confirmation-test",
                "process_tree_cleanup_ready": True,
            }
        )
        for key in (
            "development_success_improvement_min",
            "holdout_noninferiority_margin",
            "simple_case_token_budget",
            "simple_case_duration_budget_s",
            "max_content_repairs",
        ):
            protocol["release_thresholds"][key] = 1
        report = build_readiness_report(
            protocol=protocol,
            protocol_errors=[],
            environment={},
        )
        self.assertEqual(report["online_execution"]["status"], "AUTHORIZED")
        self.assertFalse(report["formal_experiment"]["ready"])
        self.assertEqual(
            report["natural_language_development_regression"]["status"],
            "NOT_RUN_OR_INVALID",
        )

    def test_hash_valid_but_failed_pilot_is_rejected_semantically(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "contemporaneous-pilot-report.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": "v8-contemporaneous-pilot-1",
                        "verdict": "PILOT_FAIL",
                        "guard": {
                            "development_cases_only": True,
                            "holdout_accessed": False,
                            "judge_called": False,
                            "external_evaluator": "week9.cross_version_validation.v1",
                        },
                    }
                ),
                encoding="utf-8",
            )
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            path.with_suffix(".sha256").write_text(
                f"{digest}  {path.name}\n", encoding="ascii"
            )
            report = build_readiness_report(
                protocol=self.protocol,
                protocol_errors=[],
                pilot_report=path,
            )
        self.assertEqual(report["contemporaneous_v4_v6_pilot"]["status"], "INVALID")
        self.assertEqual(
            report["contemporaneous_v4_v6_pilot"]["semantics"],
            "verdict_not_pass",
        )


if __name__ == "__main__":
    unittest.main()
