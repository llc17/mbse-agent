"""One fixed regression case for the stage-reporting defect; not yet executed."""
import unittest

from week12.src.component_physics.contract import ROOT
from week12.src.component_physics.runner import build_observations


class ScriptRepairCheck(unittest.TestCase):
    def test_R01_stage_evidence_survives_process_failure(self):
        # Reuse the actual failed output; do not fabricate a successful build.
        stdout = (ROOT/'results/s3-b-execution-20260923-01/B01-base/build.stdout.log').read_text(encoding='utf-8')
        observed = build_observations(stdout)
        self.assertEqual(observed['dependency_load'], dict.fromkeys(('COMPLEX', 'SERVICES', 'MSL'), 'PASS'))
        self.assertEqual((observed['load_file'], observed['model_check']), ('PASS', 'PASS'))
        self.assertEqual(observed['compile'], 'NOT_OBSERVED')
        self.assertEqual(observed['reported_executable'], '')
        # Lost output is unknown, never an invented success or proof of no run.
        missing = build_observations('')
        self.assertEqual((missing['load_file'], missing['model_check'], missing['compile']),
                         ('NOT_OBSERVED', 'NOT_OBSERVED', 'NOT_OBSERVED'))
        rejected = build_observations('__B_LOAD_FILE__=false\n__B_LOAD_ERROR__=synthetic parse error\n')
        self.assertEqual(rejected['load_file'], 'FAIL')
        self.assertEqual(rejected['model_check'], 'NOT_OBSERVED')
        # A compiler-reported path alone never constitutes compilation PASS.
        reported = build_observations('__B_BUILD_STARTED__=true\n__B_BUILD_EXE__=model.exe\n')
        self.assertEqual(reported['compile'], 'ATTEMPTED')
        self.assertEqual(reported['reported_executable'], 'model.exe')
