"""One bounded, offline S3-A acceptance run. Never imports a physics executor."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import unittest

from week12.src.composition.schema import ROOT, canonical

OUTPUT = ROOT / "results/s3-a-composition-20260922-01"


def write(name, value):
    (OUTPUT / name).write_bytes(canonical(value))


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []

    def startTest(self, test):
        if self.testsRun >= 12:
            raise RuntimeError("local case execution budget exhausted")
        self.records.append({"id": test.id(), "status": "RUNNING"})
        super().startTest(test)
        self.persist()

    def persist(self):
        write("test-state.json", {"local_case_executions": self.testsRun, "cases": self.records,
                                   "simulations": 0, "online_model_calls": 0})

    def addSuccess(self, test):
        super().addSuccess(test)
        self.records[-1]["status"] = "PASS"
        self.persist()

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.records[-1]["status"] = "FAIL"
        self.persist()

    def addError(self, test, err):
        super().addError(test, err)
        self.records[-1]["status"] = "ERROR"
        self.persist()


def main():
    from week12.tests import test_composition
    suite = unittest.defaultTestLoader.loadTestsFromModule(test_composition)
    if not 1 <= suite.countTestCases() <= 12:
        raise RuntimeError("test suite exceeds A local case budget")
    OUTPUT.mkdir(exist_ok=False)
    write("protocol.json", {
        "window": "V11-W12-S3-组件化建模首闭环-20260922", "work_package": "S3-A",
        "local_case_execution_limit": 12, "planned_cases": suite.countTestCases(),
        "simulations_limit": 0, "online_model_calls_limit": 0, "automatic_retries": 0,
        "stop_conditions": ["first failing case", "case budget exhausted", "interruption"],
        "claims": "structural contract only; no solver, physical or product acceptance",
    })
    protected = [ROOT / "src" / name for name in ("physics.py", "s2_real.py", "s2_runner.py", "specification.py")]
    before = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    write("protected-code-before.json", before)
    test_composition.EVIDENCE_DIR = OUTPUT
    with (OUTPUT / "tests.log").open("w", encoding="utf-8", newline="\n") as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2, failfast=True, resultclass=RecordedResult).run(suite)
    after = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    report = {
        "status": "PASS" if result.wasSuccessful() and before == after else "FAIL",
        "stage": "S3_INCOMPLETE", "work_package": "A", "local_case_executions": result.testsRun,
        "cases": result.records, "simulations": 0, "online_model_calls": 0,
        "physical_backend": "UNSUPPORTED", "tool_status": "NOT_RUN", "physical_status": "UNVERIFIED",
        "product_acceptance": "not_claimed", "protected_code_unchanged": before == after,
        "protected_code_after": after,
    }
    write("result.json", report)
    sources = [*sorted((ROOT / "src/composition").glob("*.py")), *sorted((ROOT / "configs/composition").glob("*.json")),
               ROOT / "models/components/contract-library-v1.json", ROOT / "tests/test_composition.py", Path(__file__).resolve()]
    write("source-manifest.json", {"files": {str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}})
    write("manifest.json", {"files": {str(p.relative_to(OUTPUT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
                                      for p in sorted(OUTPUT.rglob("*")) if p.is_file() and p.name != "manifest.json"}})
    print(json.dumps({k: report[k] for k in ("status", "local_case_executions", "simulations", "online_model_calls", "protected_code_unchanged")}, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
