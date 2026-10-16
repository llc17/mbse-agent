"""One reporting regression for the observed B02 checkModel error."""
import unittest

from week12.src.component_physics.contract import ROOT
from week12.src.component_physics.runner import build_observations


class ConnectorReportCheck(unittest.TestCase):
    def test_R04_empty_check_result_with_error_is_failure(self):
        log = ROOT/'results/s3-b-postprocess-validation-20260923-01/B02-base/build.stdout.log'
        result = build_observations(log.read_text(encoding='utf-8'))
        self.assertEqual(result['load_file'], 'PASS')
        self.assertEqual(result['model_check'], 'FAIL')
        self.assertEqual(result['reported_executable'], '')
        self.assertNotEqual(result['compile'], 'PASS')
        # No check marker is different from an observed empty check result.
        self.assertEqual(build_observations('')['model_check'], 'NOT_OBSERVED')
        old = ROOT/'results/s3-b-script-repair-validation-20260923-01/B01-base/build.stdout.log'
        self.assertEqual(build_observations(old.read_text(encoding='utf-8'))['model_check'], 'PASS')
