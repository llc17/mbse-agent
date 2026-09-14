from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

from week10.experiments.report_projection import build_schedule, summarize
from week10.src.projection_contract import V8_SOURCE_HASHES
from week9.src.hard_gate import REQUIRED_HARD_CHECK_TYPES


ROOT = Path(__file__).resolve().parents[2]


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class ProjectionReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.suite = json.loads((ROOT / "week10/configs/development_diagnostic_suite.json").read_text(encoding="utf-8"))
        self.protocol = json.loads((ROOT / "week10/configs/projection_diagnostic_protocol_v1.json").read_text(encoding="utf-8"))
        self.protocol["prompt_sha256"] = {"A": "a" * 64, "B": "b" * 64}
        self.protocol["offline_acceptance_sha256"] = "c" * 64
        self.protocol["suite_sha256"] = digest(self.suite)

    def _result(self, row: dict, *, accepted: bool, hard: bool | None = None, published: bool = False, usage: dict | None = None) -> dict:
        if hard is None:
            hard = accepted
        return {"schema_version": "v9-integration-result-1", "version": "v9", "case_id": row["case_id"], "stage": "pipeline_complete" if accepted else "generation_blocked", "provider": "deepseek", "prompt_arm": row["arm"], "prompt_sha256": row["prompt_sha256"], "v8_online_generation_sha256": V8_SOURCE_HASHES["week9/src/online_generation.py"], "response_model": "deepseek-v4-flash", "response_valid": True, "usage": usage, "api_call_count": 1, "reason_code": "accepted" if accepted else "local_input_blocked", "parameter_keys": [], "legacy_disposition": "accepted" if accepted else "content_rejection", "projection_accepted": accepted, "local_fallback_used": False, "pipeline_attempted": True if accepted else False, "pipeline_success": hard if accepted else None, "hard_gate_passed": hard if accepted else None, "quality_decision_passed": hard if accepted else None, "published": True if accepted else False, "required_check_statuses": {key: "PASS" for key in REQUIRED_HARD_CHECK_TYPES} if accepted else {}, "run_result_version": "v8", "duration_s": 0.1}

    def _ledger(self, *, mode: str = "online", complete: bool = True) -> dict:
        rows = build_schedule(self.suite, self.protocol["prompt_sha256"])
        for row in rows:
            if complete:
                accepted = row["positive"] and row["arm"] == "B"
                row.update(status="COMPLETED", attempted=True, stop_reason="COMPLETED", result=self._result(row, accepted=accepted, usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}))
        return {"schema_version": "v9-projection-diagnostic-ledger-1", "mode": mode, "protocol_sha256": digest(self.protocol), "suite_sha256": digest(self.suite), "batch_status": "COMPLETED" if complete else "STOPPED", "rows": rows, "budget": {"reserved_tokens": 0, "reserved_cost_usd": 0.0, "known_cost_usd": 0.1, "budget_status": "OK"}, "offline_acceptance_verified": True}

    def test_schedule_is_fixed_and_alternating(self) -> None:
        rows = build_schedule(self.suite, self.protocol["prompt_sha256"])
        self.assertEqual(len(rows), 24)
        self.assertEqual(sum(row["positive"] for row in rows), 18)
        self.assertEqual([row["run_id"] for row in rows], [f"v9pd-{n:03d}" for n in range(1, 25)])
        self.assertEqual(rows[0]["arm"], "A")
        self.assertEqual(rows[2]["arm"], "B")

    def test_full_online_success_and_gate_difference(self) -> None:
        report = summarize(self._ledger(), self.suite, self.protocol)
        self.assertEqual(report["development_gate"], "DEV_GATE_PASS")
        self.assertEqual((report["positive_b_accepted"], report["positive_b_denominator"]), (9, 9))
        self.assertEqual(report["usage_unknown_requests"], 0)
        weak = self._ledger()
        for row in weak["rows"]:
            if row["positive"] and row["arm"] == "A":
                row["result"]["projection_accepted"] = True
                row["result"]["stage"] = "pipeline_complete"
                row["result"]["reason_code"] = "accepted"
                row["result"]["legacy_disposition"] = "accepted"
                row["result"]["pipeline_attempted"] = True
                row["result"]["pipeline_success"] = True
                row["result"]["hard_gate_passed"] = True
                row["result"]["quality_decision_passed"] = True
                row["result"]["published"] = True
                row["result"]["required_check_statuses"] = {key: "PASS" for key in REQUIRED_HARD_CHECK_TYPES}
        self.assertEqual(summarize(weak, self.suite, self.protocol)["development_gate"], "DEV_GATE_FAIL")
        incomplete_hard = self._ledger()
        candidate = next(row for row in incomplete_hard["rows"] if row["positive"] and row["arm"] == "B")
        candidate["result"]["quality_decision_passed"] = False
        candidate["result"]["published"] = False
        candidate["result"]["required_check_statuses"] = {}
        self.assertEqual(summarize(incomplete_hard, self.suite, self.protocol)["development_gate"], "DEV_GATE_FAIL")

    def test_case_threshold_negative_usage_and_modes(self) -> None:
        ledger = self._ledger()
        b_rows = [row for row in ledger["rows"] if row["positive"] and row["arm"] == "B"]
        for first_b in b_rows[:2]:
            first_b["result"].update(stage="generation_blocked", reason_code="local_input_blocked", projection_accepted=False, pipeline_attempted=False, pipeline_success=None, hard_gate_passed=None, quality_decision_passed=None, published=False)
        self.assertEqual(summarize(ledger, self.suite, self.protocol)["development_gate"], "DEV_GATE_FAIL")
        ledger = self._ledger()
        next(row for row in ledger["rows"] if not row["positive"])["result"]["usage"] = None
        report = summarize(ledger, self.suite, self.protocol)
        self.assertEqual(report["usage_unknown_requests"], 1)
        ledger["mode"] = "offline_mock"
        self.assertEqual(summarize(ledger, self.suite, self.protocol)["development_hypothesis_status"], "NOT_RUN")
        dry = self._ledger(mode="dry_run", complete=False)
        for row in dry["rows"]:
            row.update(status="NOT_RUN", stop_reason="DRY_RUN")
        self.assertEqual(summarize(dry, self.suite, self.protocol)["development_gate"], "NOT_RUN")

    def test_fail_closed_for_rows_schema_and_leakage(self) -> None:
        ledger = self._ledger()
        duplicate = copy.deepcopy(ledger)
        duplicate["rows"][1]["arm"] = duplicate["rows"][0]["arm"]
        duplicate["rows"][1]["repetition"] = duplicate["rows"][0]["repetition"]
        with self.assertRaisesRegex(ValueError, "ROW_METADATA_INVALID"):
            summarize(duplicate, self.suite, self.protocol)
        missing = copy.deepcopy(ledger)
        missing["rows"].pop()
        with self.assertRaisesRegex(ValueError, "ROW_COUNT_INVALID"):
            summarize(missing, self.suite, self.protocol)
        leaked = copy.deepcopy(ledger)
        leaked["rows"][0]["result"]["raw_response"] = "secret prompt body"
        with self.assertRaisesRegex(ValueError, "RESULT_SCHEMA_INVALID"):
            summarize(leaked, self.suite, self.protocol)
        drift = copy.deepcopy(ledger)
        drift["rows"][0]["result"]["prompt_sha256"] = "e" * 64
        with self.assertRaisesRegex(ValueError, "RESULT_METADATA_DRIFT"):
            summarize(drift, self.suite, self.protocol)

    def test_negative_false_release_fails_gate(self) -> None:
        ledger = self._ledger()
        negative = next(row for row in ledger["rows"] if not row["positive"])
        negative["result"].update(stage="pipeline_complete", projection_accepted=True, pipeline_attempted=True, pipeline_success=True, hard_gate_passed=True, quality_decision_passed=True, published=True, reason_code="accepted", legacy_disposition="accepted", required_check_statuses={key: "PASS" for key in REQUIRED_HARD_CHECK_TYPES})
        report = summarize(ledger, self.suite, self.protocol)
        self.assertEqual((report["negative_published"], report["development_gate"]), (1, "DEV_GATE_FAIL"))

    def test_metadata_cheat_and_interrupted_attempt_are_fail_closed_or_counted(self) -> None:
        ledger = self._ledger()
        for field, bad in (("positive", False), ("requirement_sha256", "e" * 64), ("run_id", "v9pd-001")):
            altered = copy.deepcopy(ledger)
            altered["rows"][2][field] = bad
            with self.assertRaisesRegex(ValueError, "ROW_METADATA_INVALID"):
                summarize(altered, self.suite, self.protocol)
        interrupted = self._ledger(complete=False)
        row = interrupted["rows"][0]
        row.update(status="INTERRUPTED", attempted=True, stop_reason="INTERRUPTED")
        report = summarize(interrupted, self.suite, self.protocol)
        self.assertEqual((report["api_call_count"], report["usage_unknown_requests"], report["development_gate"]), (1, 1, "INCOMPLETE"))
        swapped = self._ledger()
        swapped["rows"][0], swapped["rows"][1] = swapped["rows"][1], swapped["rows"][0]
        with self.assertRaisesRegex(ValueError, "ROW_METADATA_INVALID"):
            summarize(swapped, self.suite, self.protocol)


if __name__ == "__main__":
    unittest.main()
