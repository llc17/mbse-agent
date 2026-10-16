"""Two fixed postprocessor regression cases; require separately approved slots."""
from copy import deepcopy
import csv
import json
from pathlib import Path
import unittest
from unittest.mock import patch, call

from week12.src.component_physics.contract import ROOT
from week12.src.component_physics import verify

DIRECTORY = ROOT/'results/s3-b-postprocess-validation-20260923-01/local-fixtures'
PRIOR = ROOT/'results/s3-b-script-repair-validation-20260923-01'


class PostprocessChecks(unittest.TestCase):
    def test_R02_stream_preserves_csv_guards_and_events(self):
        DIRECTORY.mkdir(exist_ok=False)
        headers = ['time']+sorted(verify._B01)+['normal', 'unused_internal']
        path = DIRECTORY/'valid-events.csv'
        with path.open('x', encoding='utf-8', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            for index in range(40001):
                values = [index/10000]+[0.0]*(len(headers)-1)
                writer.writerow(values)
                if index == 100:
                    values[headers.index('theta')] = .01
                    writer.writerow(values)
        data = verify._read_csv(path, 'B01')
        self.assertEqual(data.row_count, 40002)
        self.assertEqual(data.time[100:102], (.01, .01))
        self.assertEqual(data.col('theta')[100:102], (0, .01))
        self.assertNotIn('unused_internal', data.columns)
        self.assertIn('normal', data.columns)

        def malformed(name, header, times, expected, *, unused='0', short=False):
            target = DIRECTORY/(name+'.csv')
            with target.open('x', encoding='utf-8', newline='') as handle:
                writer = csv.writer(handle)
                writer.writerow(header)
                for time in times:
                    values = [time]+['0']*(len(header)-2)+[unused]
                    writer.writerow(values[:-1] if short else values)
            with self.assertRaisesRegex(verify._CsvProblem, expected):
                verify._read_csv(target, 'B01')

        malformed('nan-unused', headers, [0, .0001], 'csv_nonfinite', unused='nan')
        malformed('text-unused', headers, [0, .0001], 'csv_nonnumeric', unused='bad')
        malformed('width', headers, [0, .0001], 'csv_row_width', short=True)
        malformed('duplicate-header', headers+['time'], [0, .0001], 'csv_duplicate_header')
        malformed('missing-header', [h for h in headers if h != 'alpha'], [0, .0001], 'csv_missing_columns')
        malformed('time-backwards', headers, [0, .0002, .0001, 4], 'csv_time_decreases')
        malformed('gap', headers, [0, 4], 'csv_sampling_gap')
        malformed('truncated', headers, [0, .0001], 'csv_time_coverage_incomplete')

    def test_R03_single_read_and_fail_closed_pair(self):
        base = json.loads((PRIOR/'B01-base/physical.json').read_text(encoding='utf-8'))
        refined = json.loads((PRIOR/'B01-refined/physical.json').read_text(encoding='utf-8'))
        paths = [PRIOR/'B01-base/result.csv', PRIOR/'B01-refined/result.csv']
        # Test the orchestration in isolation, not another read of 290 MB CSVs.
        with patch.object(verify, 'verify_case', side_effect=[refined, base]) as read:
            output = list(verify.verification_outputs('B01', paths[1], paths[0]))
            self.assertEqual(read.call_args_list, [call('B01', paths[1]), call('B01', paths[0])])
        self.assertEqual([name for name, _ in output], ['physical', 'base_physical', 'numerical'])
        self.assertEqual(output[0][1], refined)
        self.assertEqual(output[1][1], base)
        self.assertEqual(output[2][1]['status'], 'PASS')
        failed = deepcopy(refined)
        failed['status'] = 'FAIL'
        failed['issues'] = ['synthetic_unit_test_only']
        with patch.object(verify, 'verify_case', return_value=failed) as read:
            output = list(verify.verification_outputs('B01', paths[1], paths[0]))
            self.assertEqual(read.call_count, 1)
            self.assertEqual(output, [('physical', failed)])
        bad_base = deepcopy(base)
        bad_base['status'] = 'FAIL'
        with patch.object(verify, 'verify_case', side_effect=[refined, bad_base]):
            output = list(verify.verification_outputs('B01', paths[1], paths[0]))
        self.assertEqual(output[-1][1]['status'], 'FAIL')
