from copy import deepcopy
import math
import unittest
from unittest.mock import patch
from week12.src.integration.proposals import proposal
from week12.src.integration.proposal_checks import validate_proposal, verification_contract
from week12.src.composition.schema import digest, seal_design
from week12.src.integration.review import review_identity, check_review_receipt


class ProposalChecks(unittest.TestCase):
    def test_user_facts_and_original_geometry(self):
        for name in ('fixed_nose','rocker_bogie'):
            bundle=proposal(name)
            report=validate_proposal(bundle)
            self.assertEqual(report['loaded_mass_kg'],85)
            self.assertEqual(bundle['physical']['vehicle_without_payload_kg'],65)
            self.assertEqual(bundle['physical']['user_facts']['payload_mass_kg'],20)
            self.assertFalse(bundle['physical']['executable'])
            self.assertEqual(bundle['design']['confirmation']['origin'],'pending')
            self.assertFalse(bundle['design']['confirmation']['human_confirmed'])
        d=proposal('fixed_nose')['design']
        wheels={w['id']:w for w in d['wheels']}
        self.assertEqual(wheels['nose']['radius']['value'],.07)
        self.assertEqual(wheels['nose']['pose']['z']['value'],.19)
        self.assertAlmostEqual(wheels['front']['pose']['x']['value']-wheels['rear']['pose']['x']['value'],.45)
        self.assertTrue(wheels['nose']['driven'])
        self.assertEqual(next(c for c in d['drives'] if c['id']=='nose_drive')['torque']['value']*2,25)

    def test_mass_power_clearance_and_passive_stability(self):
        f=proposal('fixed_nose'); r=proposal('rocker_bogie')
        fr,rr=validate_proposal(f),validate_proposal(r)
        self.assertAlmostEqual(f['binding']['mass_check']['initial_com_xz_m'][0],0)
        self.assertGreater(fr['nonwheel_initial_min_clearance_m'],.05)
        self.assertGreater(rr['nonwheel_initial_min_clearance_m'],.05)
        self.assertAlmostEqual(fr['all_drive_rated_electrical_plus_aux_W'],1630)
        self.assertAlmostEqual(rr['all_drive_rated_electrical_plus_aux_W'],1955)
        self.assertGreater(rr['hanging_margin_m'],.1)
        selected=[c for c in [*r['design']['bodies'],r['design']['power_supply']] if c['id'] in ('chassis','payload','controller','battery')]
        self.assertAlmostEqual(sum(c['mass']['value']*c['pose']['x']['value'] for c in selected),0)
        self.assertEqual(r['physical']['passive_joint_damping']['rocker_pivot']['pair_Nms_rad'],20)
        self.assertEqual(f['physical']['rolling_resistance']['reaction'],'external terrain couple; not wheel-carrier internal brake')

    def test_stale_or_inconsistent_proposal_rejected(self):
        b=proposal('fixed_nose'); bad=deepcopy(b)
        bad['physical']['contact_per_physical_wheel']['friction']=.1
        with self.assertRaisesRegex(ValueError,'STALE'):
            validate_proposal(bad)
        bad=deepcopy(b); bad['design']['power_supply']['mass']['value']+=1
        bad['design']=seal_design(bad['design'])
        bad['physical']['design_digest']=bad['design']['design_digest']
        bad['physical'].pop('physical_digest'); bad['physical']['physical_digest']=digest(bad['physical'])
        with self.assertRaisesRegex(ValueError,'Mass'):
            validate_proposal(bad)
        bad=deepcopy(b); bad['physical']['supply']['continuous_power_W']=100
        bad['physical'].pop('physical_digest'); bad['physical']['physical_digest']=digest(bad['physical'])
        with self.assertRaisesRegex(ValueError,'supply'):
            validate_proposal(bad)
        bad=deepcopy(b); bad['binding']['mass_check']['total_kg']=86
        with self.assertRaisesRegex(ValueError,'binding'):
            validate_proposal(bad)
        bad=deepcopy(b); bad['physical']['wheel_pair_lowering']['stiffness_N_m']=50000
        bad['physical'].pop('physical_digest'); bad['physical']['physical_digest']=digest(bad['physical'])
        with self.assertRaisesRegex(ValueError,'FROZEN_PROPOSAL'):
            validate_proposal(bad)
        both=[b,proposal('rocker_bogie')]; contract=verification_contract()
        identity=review_identity(both,contract)
        with patch('week12.src.integration.review.protocol_hash',return_value='0'*64):
            self.assertNotEqual(identity['specification_digest'],review_identity(both,contract)['specification_digest'])
        contract['physical']['wheel_penetration_limit']='relaxed'
        self.assertNotEqual(identity['specification_digest'],review_identity(both,contract)['specification_digest'])
        with self.assertRaisesRegex(ValueError,'EXTERNAL'):
            check_review_receipt({'origin':'synthetic_test','human_confirmed':False},identity)
        with self.assertRaisesRegex(ValueError,'STALE'):
            check_review_receipt({'origin':'user_message','human_confirmed':True,'message_reference':'test_only',
                'user_text':'test boundary only','scope':identity['scope'],'specification_digest':b['design']['design_digest']},identity)

    def test_cases_and_probe_independent_arithmetic(self):
        contract=verification_contract()
        total=2*len(contract['probes'])
        for name in ('fixed_nose','rocker_bogie'):
            b=proposal(name); total+=sum(len(c['numeric_variants']) for c in b['physical']['cases'])
            for case in b['physical']['cases']:
                self.assertEqual((case['riser_m'],case['tread_m']),(.15,.28))
                points=case['terrain_points']
                vertical=[(a,b) for a,b in zip(points,points[1:]) if a[0]==b[0]]
                self.assertEqual(len(vertical),case['steps'])
                for a,b in vertical:
                    self.assertAlmostEqual(b[1]-a[1],-.15 if case['direction']=='descent' else .15)
                if case['steps']==20:
                    self.assertIn(name+'_three_up',case['requires'])
                    self.assertIn(name+'_three_down',case['requires'])
        self.assertEqual(total,28)
        self.assertEqual(contract['budget_proposal']['maximum_scene_attempts'],total*3)
        self.assertAlmostEqual(100-2*((1+2)*10/.8+5),15)
        # Exact integration of J*domega/dt=-Crr*R*N*tanh(omega/wreg).
        rate=.05*.1*10/(.01*.1)
        self.assertAlmostEqual(rate,50)
        self.assertAlmostEqual(.5*.01*10**2,.5)
        self.assertTrue(contract['physical']['front_first_rule']['edge_crossing_alone_is_not_pass'])


if __name__=='__main__':
    unittest.main()
