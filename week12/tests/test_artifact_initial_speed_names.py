"""Conservative initial-speed naming compatibility; no network or result writes."""
import copy
import json
import unittest
from pathlib import Path
from week12.src.car_domain.artifact_continuous import initial_rest_evidence, validate_commitment, SourceBindingError

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'week12/results/s5-frozen-acceptance-20261014-01'

def original():
    commitment = json.loads((OUT / 'platform-recovered-commitment.json').read_text(encoding='utf8'))
    pointer = json.loads((OUT / 'platform-current.before-initial-speed-cache-replay.json').read_text(encoding='utf8'))
    return commitment, pointer['sources']

class Checks(unittest.TestCase):
    def item(self, **changes):
        item = dict(name='v_initial', value=0, unit='m/s', source_id='src_test', source_text='从平台前静止起步')
        return dict(item, **changes)

    def test_explicit_initial_speed_names(self):
        for name in ('v_initial', 'initial_v', 'speed_initial', 'velocity_initial', 'initial_speed', 'initial_velocity', 'speed_start', 'velocity_start', 'v_start', 'start_v', 'start_speed', 'start_velocity', 'v0', 'V_INITIAL'):
            with self.subTest(name=name):
                self.assertIsNotNone(initial_rest_evidence(self.item(name=name), '从平台前静止起步，向前行驶。'))

    def test_non_initial_and_angular_names_rejected(self):
        for name in ('v', 'speed', 'velocity', 'v_final', 'final_v', 'speed_end', 'velocity_initial_target', 'initial_position', 'initial_angular_velocity', 'angular_velocity_initial', 'omega_initial', 'w0', 'initial_speed_limit', 'drive_force'):
            with self.subTest(name=name):
                self.assertIsNone(initial_rest_evidence(self.item(name=name), '从平台前静止起步'))

    def test_nonzero_wrong_units_and_missing_source_rejected(self):
        for changes in ({'value': 1}, {'value': -0.1}, {'unit': 'rad/s'}, {'unit': 'kg'}, {'unit': 'm'}, {'source_text': '静止'}):
            with self.subTest(changes=changes):
                self.assertIsNone(initial_rest_evidence(self.item(**changes), '从平台前静止起步'))
        self.assertIsNone(initial_rest_evidence(self.item(), '无此引用'))

    def test_negated_hypothetical_final_and_repeated_context_rejected(self):
        for source in ('不是静止起步', '不要求静止起步', '可能静止起步', '如果静止起步', '是否静止起步？', '最终静止起步', '例如静止起步', '“静止起步”只是例子', '静止起步或继续滑行', '静止起步。不是静止起步'):
            with self.subTest(source=source):
                self.assertIsNone(initial_rest_evidence(self.item(source_text='静止起步'), source))

    def test_unchanged_real_commitment_and_trusted_source(self):
        commitment, sources = original()
        evidence = []
        validate_commitment(commitment, sources, source_evidence=evidence)
        self.assertEqual([(e['field'], e['kind']) for e in evidence], [('v_initial', 'initial_rest_zero_speed')])
        bad = copy.deepcopy(commitment)
        next(b for b in bad['literal_bindings'] if b['name'] == 'v_initial')['source_text'] += ' UNTRUSTED'
        with self.assertRaises(SourceBindingError):
            validate_commitment(bad, sources)

if __name__ == '__main__':
    unittest.main()
