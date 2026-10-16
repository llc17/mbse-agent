"""Regression of terminal completion and preservation of approved scene contracts.

Software-only: checks generated conditions and whole-package/contract drift;
this does not execute Modelica or certify endpoint export behavior.
"""
import hashlib
import json
import re
import unittest
from pathlib import Path

from week12.src.active_cluster.design import build_design
from week12.src.active_cluster.modelica import lower

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "week12/results/s3-c-open-configuration-proposal-20261009-01/proposal-final.json"
ARCHIVE = ROOT / "week12/results/s3-c-active-eightwheel-execution-20261009-01/eightwheel-compile-continuation-20261010/generated/task_d7c63d5fd3b943d4be8e0e39f5b23fbe"


class TerminalCompletionRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raw = SPEC.read_bytes()
        cls.model, cls.contract = lower(build_design(json.loads(raw), spec_sha=hashlib.sha256(raw).hexdigest()))
        cls.original = (ARCHIVE / "V11GeneratedActive.mo").read_text(encoding="utf-8")
        cls.manifest = json.loads((ARCHIVE / "manifest.json").read_text(encoding="utf-8"))

    def test_only_terminal_fix_and_approved_inline_adapter_change_whole_package(self):
        expected = self.original
        for end in (12, 40):
            old = f"taskDone=time>={end};"
            new = f"taskDone=if terminal() then noEvent(time>={end}) else time>={end};"
            self.assertEqual(expected.count(old), 1)
            expected = expected.replace(old, new)
        wrapper='  function referenceGeometryInline\n    extends V11ActiveClusterC3.referenceGeometry;\n    annotation(Inline=true);\n  end referenceGeometryInline;'
        self.assertEqual(self.model.count(wrapper), 1)
        self.assertEqual(self.model.count('ref=referenceGeometryInline('), 5)
        restored=self.model.replace(wrapper+'\n\n','',1).replace('ref=referenceGeometryInline(', 'ref=C.referenceGeometry(')
        self.assertEqual(restored, expected)

    def test_all_seven_raw_contracts_remain_identical(self):
        self.assertEqual(json.loads(json.dumps(self.contract)), self.manifest["output_contract"])

    def test_terminal_branch_is_exact_and_cannot_complete_before_deadline(self):
        # Evaluate the literal terminal branch extracted from actual generated text,
        # not a second completion predicate. Event scheduling remains untested.
        lines = re.findall(r"taskDone=if terminal\(\) then noEvent\((time>=[0-9]+)\) else (time>=[0-9]+);", self.model)
        self.assertEqual(len(lines), 2)
        for terminal_branch, ordinary_branch in lines:
            self.assertEqual(terminal_branch, ordinary_branch)
            end = float(terminal_branch.split(">=")[1])
            for actual_time, expected in ((0., False), (end-.02, False), (end-1e-9, False), (end, True), (end+1e-9, True)):
                self.assertEqual(eval(terminal_branch, {"__builtins__": {}}, {"time": actual_time}), expected)

    def test_task_completion_event_and_mission_termination_remain_present(self):
        self.assertEqual(self.model.count("elsewhen taskDone then completionTime:=time;"), 7)
        self.assertEqual(self.model.count('terminate("ACTUAL_COMPLETE_CRUISING")'), 4)
        self.assertNotIn("noEvent(time>=120)", self.model)
        self.assertNotIn("taskDone=terminal();", self.model)


if __name__ == "__main__":
    unittest.main()
