from __future__ import annotations

import json
from pathlib import Path
import unittest

from week9.experiments.report_adaptive_regression import (
    CLAIM_STATUS,
    build_adaptive_regression_report,
)
from week9.src.schemas import sha256_text


_SUITE_PATH = (
    Path(__file__).resolve().parents[1]
    / "configs"
    / "stage4_adaptive_regression_suite.json"
)


def _suite():
    text = _SUITE_PATH.read_text(encoding="utf-8")
    return json.loads(text), sha256_text(text)


def _guard():
    return {
        "online_api_used": False,
        "external_network_used": False,
        "holdout_accessed": False,
        "legacy_adapters_used": False,
        "cross_version_comparison_performed": False,
        "cases_module_imported": False,
        "model_provider": "deterministic_offline",
    }


class AdaptiveRegressionReportTests(unittest.TestCase):
    def test_exact_frozen_expectations_produce_mechanism_pass_only(self) -> None:
        suite, digest = _suite()
        cases = [
            {
                **item,
                "actual": dict(item["expectations"]),
                "evidence": {},
                "run_result_file": "",
            }
            for item in suite["cases"]
        ]
        report = build_adaptive_regression_report(
            suite_snapshot=suite,
            suite_sha256=digest,
            cases=cases,
            offline_guard=_guard(),
        )

        self.assertEqual(report["verdict"], "MECHANISM_REGRESSION_PASS")
        self.assertEqual(report["summary"]["pass_count"], len(cases))
        self.assertFalse(report["summary"]["cost_claims_evaluated"])
        self.assertEqual(CLAIM_STATUS["v4_v6_v8_fair_comparison"], "NOT_RUN")
        self.assertEqual(CLAIM_STATUS["simple_case_token_budget"], "NOT_FROZEN")
        self.assertNotIn("RELEASE_PASS", json.dumps(report))

    def test_one_mismatch_fails_without_reclassifying_expected_rejection(self) -> None:
        suite, digest = _suite()
        cases = [
            {
                **item,
                "actual": dict(item["expectations"]),
                "evidence": {},
                "run_result_file": "",
            }
            for item in suite["cases"]
        ]
        cases[0]["actual"]["validation_count"] = 2
        report = build_adaptive_regression_report(
            suite_snapshot=suite,
            suite_sha256=digest,
            cases=cases,
            offline_guard=_guard(),
        )

        self.assertEqual(report["verdict"], "MECHANISM_REGRESSION_FAIL")
        self.assertEqual(report["summary"]["fail_count"], 1)
        rejected = next(
            item
            for item in report["cases"]
            if item["case_id"] == "T01-critical-not-implemented"
        )
        self.assertEqual(rejected["mechanism_assertion_status"], "PASS")
        self.assertFalse(rejected["actual"]["overall_success"])

    def test_offline_guard_is_fail_closed(self) -> None:
        suite, digest = _suite()
        guard = _guard()
        guard["holdout_accessed"] = True
        with self.assertRaisesRegex(ValueError, "offline regression guard"):
            build_adaptive_regression_report(
                suite_snapshot=suite,
                suite_sha256=digest,
                cases=[
                    {
                        **item,
                        "actual": dict(item["expectations"]),
                        "evidence": {},
                    }
                    for item in suite["cases"]
                ],
                offline_guard=guard,
            )


if __name__ == "__main__":
    unittest.main()
