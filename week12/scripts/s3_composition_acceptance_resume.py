"""Explicit one-time fixture correction validation, sharing the original budget.

The initial run's eight executions and failure remain untouched. Four remaining
slots are frozen here. This is not automatic retry or a fresh acceptance budget.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import unittest

from week12.src.composition.schema import ROOT
from week12.scripts.s3_composition_acceptance import OUTPUT, RecordedResult, write

NAMES = (
    "test_rocker_bogie_preview_has_distinct_connection_topology",
    "test_same_version_library_content_change_is_rejected",
    "test_seal_and_receipt_binding_invalidate_changed_design_content",
    "test_unknown_port_reference_is_rejected",
)


class ResumeResult(RecordedResult):
    def startTest(self, test):
        if self.testsRun + 8 >= 12:
            raise RuntimeError("shared local case execution budget exhausted")
        super().startTest(test)

    def persist(self):
        write("resume-test-state.json", {"local_case_executions_this_batch": self.testsRun,
                                         "local_case_executions_total": self.testsRun + 8,
                                         "cases": self.records, "simulations": 0, "online_model_calls": 0})


def main():
    from week12.tests import test_composition
    prior = json.loads((OUTPUT / "result.json").read_text(encoding="utf-8"))
    if prior["local_case_executions"] != 8 or prior["status"] != "FAIL" or prior["cases"][-1]["id"].split(".")[-1] != NAMES[0]:
        raise RuntimeError("unexpected prior evidence; no execution permitted")
    protocol = OUTPUT / "resume-protocol.json"
    with protocol.open("x", encoding="utf-8") as stream:
        json.dump({"prior_executions": 8, "remaining_slots": 4, "cases": NAMES,
                   "reason": "correct rocker fixture port(-0.30,-0.08) to port(x=-0.30,z=-0.08); validator unchanged",
                   "not_run": ["test_strict_units_are_rejected_before_stale_digest"],
                   "simulations": 0, "online_model_calls": 0, "automatic_retries": 0,
                   "stop": "first failure or total executions = 12"}, stream, ensure_ascii=False, indent=2)
    test_composition.EVIDENCE_DIR = OUTPUT
    suite = unittest.TestSuite(test_composition.CompositionContractTests(name) for name in NAMES)
    with (OUTPUT / "resume-tests.log").open("x", encoding="utf-8") as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2, failfast=True, resultclass=ResumeResult).run(suite)
    passed_ids = {item["id"] for item in prior["cases"] + result.records if item["status"] == "PASS"}
    after = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in prior["protected_code_after"]}
    unchanged = after == prior["protected_code_after"]
    write("final-result.json", {
        "status": "PASS" if result.wasSuccessful() and unchanged else "FAIL",
        "work_package": "A", "stage": "S3_INCOMPLETE", "local_case_executions_total": 8 + result.testsRun,
        "unique_cases_passed": len(passed_ids), "historical_errors_preserved": 1,
        "not_run": ["test_strict_units_are_rejected_before_stale_digest"],
        "cases_this_batch": result.records, "simulations": 0, "online_model_calls": 0,
        "physical_backend": "UNSUPPORTED", "physical_status": "UNVERIFIED", "tool_status": "NOT_RUN",
        "product_acceptance": "not_claimed", "protected_code_unchanged": unchanged,
    })
    sources = [*sorted((ROOT / "src/composition").glob("*.py")), *sorted((ROOT / "configs/composition").glob("*.json")),
               ROOT / "models/components/contract-library-v1.json", ROOT / "tests/test_composition.py",
               Path(__file__).resolve(), ROOT / "scripts/s3_composition_acceptance.py"]
    write("final-source-manifest.json", {"files": {str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}})
    write("final-manifest.json", {"files": {str(p.relative_to(OUTPUT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
                                            for p in sorted(OUTPUT.rglob("*")) if p.is_file() and p.name != "final-manifest.json"}})
    print(json.dumps({"success": result.wasSuccessful() and unchanged, "total_executions": 8 + result.testsRun,
                      "unique_cases_passed": len(passed_ids), "historical_errors_preserved": 1}))
    return 0 if result.wasSuccessful() and unchanged else 1


if __name__ == "__main__":
    sys.exit(main())
