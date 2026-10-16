"""Four preflight groups for the explicit C2/housing amendment; not auto-run."""
from copy import deepcopy
import unittest
from week12.src.integration import vehicle_amendment as a
from week12.src.integration import vehicle_verify as v
from week12.src.integration.timing_amendment import inputs as old_inputs
from week12.src.integration import model_generator as old


class VehicleAmendmentChecks(unittest.TestCase):
    def test_exact_change_scope_and_original_dynamics_preserved(self):
        packet,bundle,contract=a.inputs(); _,original,old_contract=old_inputs()
        normalized=deepcopy(bundle)
        normalized['physical'].pop('contact_implementation'); normalized['physical'].pop('housing_amendment')
        for key in ('status','physical_digest'): normalized['physical'][key]=original['physical'][key]
        normalized['physical']['envelopes_local_xz']['nose_drive']=original['physical']['envelopes_local_xz']['nose_drive']
        self.assertEqual(normalized,original)
        for key in ('physical','numerical','numerical_comparison'): self.assertEqual(contract[key],old_contract[key])
        for case in a.CASE_ORDER:
            generated=a.generate(bundle,case); prior=old.generate(original,case)
            for key in ('modelica','sysml'):
                self.assertEqual(generated[key].replace(packet['specification_digest'],a.PREVIOUS_SPEC),prior[key])

    def test_circle_oracle_and_inertia_compatibility(self):
        _,bundle,_=a.inputs()
        shape=bundle['physical']['envelopes_local_xz']['nose_drive'][0]
        terrain=v._terrain([[-1,0],[0,0],[0,.15],[1,.15]])
        # Independent distances to horizontal face, vertical face and corner.
        self.assertAlmostEqual(v._shape_clearance(shape,(-.5,.2,0),terrain),.15,places=12)
        self.assertAlmostEqual(v._shape_clearance(shape,(-.06,.075,.7),terrain),.01,places=12)
        self.assertAlmostEqual(v._shape_clearance(shape,(-.06,.23,1.4),terrain),.05,places=12)
        self.assertLess(v._shape_clearance(shape,(.5,.16,0),terrain),-.0001)
        drive=next(d for d in bundle['design']['drives'] if d['id']=='nose_drive')
        wheel=next(w for w in bundle['design']['wheels'] if w['id']=='nose')
        self.assertEqual([drive['pose'][k]['value'] for k in ('x','z','pitch')],
                         [wheel['pose'][k]['value'] for k in ('x','z','pitch')])
        self.assertLessEqual(drive['inertia']['value'],drive['mass']['value']*.05**2)
        self.assertAlmostEqual(.07-.05-.15*.07,.0095,places=12)
        self.assertEqual(shape['start'],shape['end'])

    def test_changed_input_and_old_bundle_are_rejected(self):
        _,bundle,_=a.inputs(); _,original,_=old_inputs()
        with self.assertRaises(ValueError): a.generate(original,a.CASE_ORDER[0])
        bad=deepcopy(bundle); bad['design']['drives'][0]['torque']['value']+=1
        with self.assertRaises(ValueError): a.generate(bad,a.CASE_ORDER[0])
        bad=deepcopy(bundle); bad['physical']['envelopes_local_xz']['nose_drive'][0]['radius']=.04
        with self.assertRaises(ValueError): a.generate(bad,a.CASE_ORDER[0])
        with self.assertRaises(ValueError): a.generate(bundle,'shared_supply_probe')

    def test_full_flow_trace_and_contact_binding(self):
        packet,bundle,contract=a.inputs(); generated=a.generate(bundle,'fixed_nose_full_mission')
        self.assertEqual(contract['case_order'],a.CASE_ORDER)
        self.assertEqual(len(contract['reused_probe_evidence']),4)
        for row in contract['reused_probe_evidence']:
            for f in row['files']: self.assertEqual(a.sha(a.ROOT/f['path']),f['sha256'])
        trace=generated['trace']
        self.assertEqual(trace['frozen']['physical_digest'],bundle['physical']['physical_digest'])
        self.assertEqual(trace['frozen']['contact_source_sha256'],a.C2_SHA)
        self.assertTrue(trace['one_solve'])
        self.assertEqual(set(trace['state_continuity']['no_reset']),set(contract['physical']['serial_mission_rule']['no_reset']))
        self.assertIn('model PolylineWheelContact = V11ContactC2.PolylineWheelContact;',a.isolated_b())
        self.assertEqual(contract['budget_proposal']['maximum_scene_attempts'],42)


if __name__=='__main__': unittest.main()
