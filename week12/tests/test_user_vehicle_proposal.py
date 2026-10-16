from copy import deepcopy
import unittest
from week12.src.integration.user_vehicle_proposal import proposal, validate, verification_contract
from week12.src.integration.review import review_identity, check_review_receipt


class UserVehicleChecks(unittest.TestCase):
    def test_inertial_and_geometric_preflight(self):
        b=proposal(); r=validate(b)
        self.assertEqual(r['mass_check']['total_kg'],85)
        self.assertAlmostEqual(r['mass_check']['initial_com_xz_m'][1],.248)
        self.assertTrue(4.80<r['mass_check']['initial_inertia_about_com_kg_m2']<4.81)
        self.assertEqual(r['source_geometry'],'USER_WHEEL_RADII_AND_RELATIVE_AXLE_GEOMETRY_PRESERVED')
        self.assertEqual(r['static_envelope_and_ratings']['shared_supply_power_margin_W'],530)
        self.assertFalse(b['design']['confirmation']['human_confirmed'])
        self.assertEqual(b['physical']['initialization']['release_clearance_m'],.01)
        self.assertNotIn('global_vertical_lift_m',b['physical']['initialization'])
        self.assertNotIn('passive_damping_implementation',b['physical'])
        for wheel in b['design']['wheels']:
            self.assertEqual(wheel['radius']['source'],'user_supplied_full_mission_source')
        for case in b['physical']['cases']:
            self.assertNotIn('initial_vertical_offset_m',case)
            expected=case['steps']*.15+.01 if case['direction']=='descent' else .01
            self.assertAlmostEqual(case['initial_world_translation_z_m'],expected)

    def test_changed_physics_geometry_or_test_receipt_rejected(self):
        b=proposal()
        for part,key,value in [('physical','mass_kg',105),('physical','source_model_sha256','different')]:
            changed=deepcopy(b); changed[part][key]=value
            with self.assertRaises(ValueError): validate(changed)
        changed=deepcopy(b); changed['binding']['root']['pitch_lock']=True
        with self.assertRaises(ValueError): validate(changed)
        identity=review_identity([b],verification_contract())
        with self.assertRaises(ValueError): check_review_receipt({'origin':'development_test','human_confirmed':True},identity)
        altered=verification_contract(); altered['budget_proposal']['maximum_scene_attempts']=55
        self.assertNotEqual(identity['specification_digest'],review_identity([b],altered)['specification_digest'])

    def test_serial_contract_and_budget_cover_whole_affected_round(self):
        b=proposal(); c=verification_contract()
        self.assertEqual(len(b['physical']['cases']),7)
        self.assertEqual(len(c['probes']),2)
        per_round=(7+2)*2
        self.assertEqual(c['budget_proposal']['scene_attempts_per_complete_round'],per_round)
        self.assertEqual(c['budget_proposal']['maximum_scene_attempts'],per_round*3)
        self.assertEqual(c['budget_proposal']['sysml_parse_limit'],3)
        full=b['physical']['cases'][-1]
        self.assertAlmostEqual(full['first_down_edge_x_m']-full['up_end_x_m'],2)
        self.assertAlmostEqual(full['exit_plane_x_m'],15.2)
        self.assertEqual(full['stop_time_s'],100)
        self.assertEqual(full['requires'],['fixed_nose_twenty_up','fixed_nose_twenty_down','fixed_nose_flat_energy'])
        self.assertIn('battery_energy',c['physical']['serial_mission_rule']['no_reset'])
        self.assertIn('do not assume nose airborne',c['physical']['serial_mission_rule']['upper_support'])
        self.assertIn('even before axle crosses',c['physical']['serial_mission_rule']['descent_event_reference'])
        self.assertIn('INSUFFICIENT',c['physical']['serial_mission_rule']['support_reference_window'])
        self.assertNotIn('rocker_bogie',c['physical']['front_first_rule']['edge_crossing_order'])
        self.assertNotIn('passive_joint_relative_angle_limit_deg',c['physical'])
