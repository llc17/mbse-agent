"""Prepared front-end evidence negative cases; no Modelica process execution."""
from pathlib import Path
import unittest

from week12.src.active_cluster.frontend import frontend_script, frontend_evidence
from week12.src.active_cluster.execution import ORDER, Stop
from week12.src.component_physics.runner import MSL


class FrontendEvidence(unittest.TestCase):
    def setUp(self):
        self.manifest = {'model_class_by_scene': {n: 'V11GeneratedActive.C'+str(i)
                         for i, n in enumerate(ORDER)},
                         'package_path': 'V11GeneratedActive.mo'}
        self.result = {'returncode': 0, 'timed_out': False, 'uncertain': False,
                       'stdout': '\n'.join(
                           [f'__GF_LOAD_{i}__=true' for i in range(7)] +
                           ['__GF_OMC__=OpenModelica 1.25.7', '__GF_MSL__=4.0.0',
                            '__GF_MSL_SOURCE__='+MSL.as_posix()] +
                           [line for i in range(7) for line in
                            [f'__GF_CHECK_{i}__=Check completed successfully.',
                             f'__GF_ERROR_{i}_BEGIN__', '', f'__GF_ERROR_{i}_END__']] +
                           ['__GF_COMPLETE__=true'])}

    def test_frontend_receipt_cannot_claim_compile_or_task_pass(self):
        result = frontend_evidence(self.manifest, self.result)
        self.assertEqual(result['status'], 'PASS_FRONTEND_ONLY')
        self.assertEqual(result['compile'], 'NOT_RUN')
        self.assertEqual(result['vehicle_task'], 'NOT_RUN')

    def test_all_classes_checked_once_without_build_or_simulation(self):
        text = frontend_script(self.manifest)
        self.assertEqual(text.count('checkModel('), 7)
        self.assertNotIn('buildModel(', text)
        self.assertNotIn('simulate(', text)

    def test_directory_package_name_mismatch_rejected_before_script(self):
        self.manifest['package_path'] = str(Path('wrong')/'package.mo')
        with self.assertRaisesRegex(Stop, 'FILENAME_ROOT_MISMATCH'):
            frontend_script(self.manifest)

    def test_missing_duplicate_or_untyped_registered_class_is_rejected(self):
        original = dict(self.manifest['model_class_by_scene'])
        for change in ('missing', 'duplicate', 'untyped'):
            changed = dict(original)
            if change == 'missing':
                del changed[ORDER[-1]]
            elif change == 'duplicate':
                changed[ORDER[-1]] = changed[ORDER[0]]
            else:
                changed[ORDER[-1]] = {}
            self.manifest['model_class_by_scene'] = changed
            with self.assertRaises(Stop):
                frontend_script(self.manifest)

    def test_missing_or_duplicate_completion_marker_is_failure(self):
        for suffix in ('', '\n__GF_COMPLETE__=true'):
            changed = dict(self.result)
            changed['stdout'] = (changed['stdout'].replace('__GF_COMPLETE__=true', '')
                                  if not suffix else changed['stdout']+suffix)
            self.assertEqual(frontend_evidence(self.manifest, changed)['status'], 'FAIL')

    def test_success_text_cannot_hide_one_class_type_error(self):
        self.result['stdout'] = self.result['stdout'].replace(
            '__GF_ERROR_3_BEGIN__\n', '__GF_ERROR_3_BEGIN__\nError: Unknown component\n')
        self.assertEqual(frontend_evidence(self.manifest, self.result)['status'], 'FAIL')

    def test_wrong_version_or_uncertain_process_never_passes(self):
        for key, value in [('uncertain', True), ('timed_out', True), ('returncode', 1)]:
            changed = dict(self.result); changed[key] = value
            self.assertEqual(frontend_evidence(self.manifest, changed)['status'], 'FAIL')
        changed = dict(self.result)
        changed['stdout'] = changed['stdout'].replace('1.25.7', '1.26.0')
        self.assertEqual(frontend_evidence(self.manifest, changed)['status'], 'FAIL')


if __name__ == '__main__':
    unittest.main()
