import unittest
from week12.src.integration.user_baseline import SOURCE, baseline_mapping, numeric_parameters, mission_terrain


class UserBaselineChecks(unittest.TestCase):
    def test_source_mapping_does_not_double_mass_torque_or_nose_height(self):
        result = baseline_mapping()
        self.assertFalse(result['executable'])
        self.assertFalse(result['human_confirmed'])
        self.assertEqual(result['user_confirmed_facts']['loaded_mass_kg'], 85)
        self.assertEqual(result['source_inertial_reference']['pitch_inertia_kg_m2'], 20)
        rear, front, nose = result['wheels']
        self.assertEqual(sum(w['physical_count'] for w in result['wheels']), 6)
        self.assertTrue(all(w['driven'] for w in result['wheels']))
        self.assertAlmostEqual(front['local_axle_xz_m'][0]-rear['local_axle_xz_m'][0], .45)
        self.assertAlmostEqual(nose['local_axle_xz_m'][0], .54)
        self.assertAlmostEqual(.127+nose['local_axle_xz_m'][1]-nose['radius_m'], .12)
        self.assertEqual([w['proposed_pair_torque_cap_Nm'] for w in result['wheels']], [60,60,25])
        self.assertEqual([w['proposed_single_wheel_torque_cap_Nm'] for w in result['wheels']], [30,30,12.5])

    def test_restricted_extractor_rejects_unsafe_or_ambiguous_values(self):
        original = SOURCE.read_text(encoding='utf-8-sig')
        for replacement in ('unknown', 'f()', '1e999', '[85][0]'):
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                numeric_parameters(original.replace('m = 85.0;', 'm = '+replacement+';'))
        with self.assertRaises(ValueError):
            numeric_parameters(original+'\nparameter Real m = 85;')
        with self.assertRaises(ValueError):
            numeric_parameters(original.replace('n_steps = 20;', 'n_steps = 2.5;'))

    def test_serial_mission_has_both_flights_and_full_platform(self):
        terrain = mission_terrain(steps=20, rise=.15, tread=.28, entry=2, platform=2)
        self.assertAlmostEqual(terrain['up_end_x_m'], 7.6)
        self.assertAlmostEqual(terrain['down_start_x_m'], 9.6)
        self.assertAlmostEqual(terrain['down_end_x_m'], 15.2)
        points = terrain['points_xz_m']
        vertical = [b[1]-a[1] for a,b in zip(points,points[1:]) if a[0] == b[0]]
        self.assertEqual(sum(v>0 for v in vertical), 20)
        self.assertEqual(sum(v<0 for v in vertical), 20)
        self.assertTrue(all(abs(abs(v)-.15)<1e-12 for v in vertical))
        self.assertEqual(max(p[1] for p in points), 3)
        self.assertEqual(points[-1][1], 0)
        self.assertTrue(all(b[0]>=a[0] for a,b in zip(points,points[1:])))
        short = mission_terrain(steps=3, rise=.15, tread=.28, entry=2, platform=2)
        self.assertEqual(len(short['points_xz_m']), 16)
        for kw in ({'steps':0}, {'steps':True}, {'rise':-1}, {'platform':0}, {'tread':float('nan')}):
            args = dict(steps=3, rise=.15, tread=.28, entry=2, platform=2)
            args.update(kw)
            with self.assertRaises(ValueError):
                mission_terrain(**args)
