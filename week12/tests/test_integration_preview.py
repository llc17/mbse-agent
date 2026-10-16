"""C preparation checks: no A regression suite or Modelica execution."""
from copy import deepcopy
import math
import unittest
from unittest.mock import patch

from week12.src.composition.examples import make_example, pose
from week12.src.composition.schema import seal_design
from week12.src.integration.preview import binding_preview, transform


class BindingChecks(unittest.TestCase):
    def test_mass_and_offset_accounting(self):
        d = make_example('fixed_nose')
        r = binding_preview(d)
        expected = sum(c['count']*c['mass']['value'] for c in [*d['bodies'], *d['wheels'], *d['drives'], d['power_supply']])
        self.assertAlmostEqual(r['mass_check']['total_kg'], expected)
        self.assertAlmostEqual(expected, 40.3)
        self.assertEqual(len(r['hardware_mass_mounts']), 3)
        self.assertGreater(r['mass_check']['initial_inertia_about_com_kg_m2'], 1)
        t = transform((2,3,math.pi/2), (2,4,math.pi))
        self.assertAlmostEqual(t['r_m'][0], 1)
        self.assertAlmostEqual(t['r_m'][2], 0)
        self.assertAlmostEqual(t['angle_deg'], 90)

    def test_bearings_and_support_are_not_duplicated(self):
        r = binding_preview(make_example('fixed_nose'))
        self.assertEqual(len(r['wheels']), 3)
        self.assertTrue(all(w['mount_and_bearing']['additional_revolutes'] == 0 for w in r['wheels']))
        self.assertEqual(sum(j['kind'] == 'passive_revolute' for j in r['joints']), 0)
        for d in r['drives']:
            self.assertEqual(d['support_connect'], d['wheel_id']+'.support')
        self.assertFalse(r['root']['pitch_lock'])
        self.assertEqual(next(w for w in r['wheels'] if w['input_id']=='nose')['shaft_boundary'], 'free_zero_torque')

    def test_tree_topology_and_determinism(self):
        a = binding_preview(make_example('fixed_nose'))
        design = make_example('rocker_bogie')
        b = binding_preview(design)
        self.assertEqual(sum(j['kind'] == 'passive_revolute' for j in b['joints']), 2)
        self.assertEqual(next(w for w in b['wheels'] if w['input_id']=='rear')['mount_and_bearing']['carrier'], 'bogie')
        self.assertEqual(next(w for w in b['wheels'] if w['input_id']=='front')['mount_and_bearing']['carrier'], 'rocker')
        self.assertNotEqual(a['structure_topology_digest'], b['structure_topology_digest'])
        reordered = deepcopy(design)
        for key in ('bodies','wheels','joints','drives','connections'):
            reordered[key].reverse()
        reordered = seal_design(reordered)
        rb = binding_preview(reordered)
        self.assertEqual(b['bodies'], rb['bodies'])
        self.assertEqual(b['joints'], rb['joints'])
        self.assertEqual(b, binding_preview(design))

    def test_shared_supply_and_no_approval(self):
        r = binding_preview(make_example('rocker_bogie'))
        self.assertEqual(len(r['drives']), 3)
        self.assertEqual(len([r['shared_supply']]), 1)
        self.assertEqual(r['shared_supply']['rated_power_W'], 800)
        self.assertTrue(all(d['branch_energy_is_not_shared_battery_energy'] for d in r['drives']))
        self.assertFalse(r['executable'])
        self.assertEqual(r['approval_status'], 'PENDING_CONCRETE_C_CONFIRMATION')
        self.assertNotIn('modelica_source', r)

    def test_changed_design_cannot_reuse_binding(self):
        d = make_example('fixed_nose')
        old = binding_preview(d)
        d['drives'][0]['torque']['value'] += 1
        with self.assertRaises(ValueError):
            binding_preview(d)
        new = binding_preview(seal_design(d))
        self.assertNotEqual(old['preview_digest'], new['preview_digest'])
        self.assertNotEqual(old['design_digest'], new['design_digest'])

    def test_source_drift_rejected(self):
        from week12.src.integration import preview
        original = preview.B_SOURCE.read_bytes
        with patch.object(type(preview.B_SOURCE), 'read_bytes', lambda p: original()+b' ' if p == preview.B_SOURCE else original()):
            with self.assertRaisesRegex(ValueError, 'B_SOURCE_DRIFT'):
                binding_preview(make_example('fixed_nose'))


if __name__ == '__main__':
    unittest.main()
