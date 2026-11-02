"""RID stage classification only; no runtime initialization or network."""
import unittest
from week13.src.case_runtime import CaseRuntime


class PlanCategory(unittest.TestCase):
    def test_plan_stage_remains_design_for_diagnostic_and_correction_rids(self):
        runtime = object.__new__(CaseRuntime)
        original = 'case_plan_strict85_180_260_20261022_01'
        for request_id in (original, original + '_http400_diagnostic_1',
                           original + '_empty_object_correction_1'):
            with self.subTest(request_id=request_id):
                self.assertEqual(runtime._category(request_id), 'design')
        self.assertEqual(runtime._category('case_generate_fixture'), 'generation')
        self.assertEqual(runtime._category('case_source_repair_fixture'), 'generation')
        self.assertEqual(runtime._category('case_reopen_fixture'), 'reopen')


if __name__ == '__main__':
    unittest.main()
