import unittest
from unittest.mock import patch
from copy import deepcopy

from week12.src.integration.execution import approved_inputs, Batch, build_script, APPROVED_SPEC
from week12.src.integration.review import check_review_receipt


class ExecutionChecks(unittest.TestCase):
    def test_approved_identity_requires_exact_scope_and_live_inputs(self):
        packet,bundle,contract=approved_inputs()
        self.assertEqual(packet['confirmation_identity']['specification_digest'],APPROVED_SPEC)
        self.assertEqual(contract['budget_proposal']['maximum_scene_attempts'],54)
        receipt={'origin':'user_message','human_confirmed':True,'message_reference':'synthetic boundary negative',
                 'user_text':'test only','scope':packet['confirmation_identity']['scope'],'specification_digest':'old'}
        with self.assertRaises(ValueError): check_review_receipt(receipt,packet['confirmation_identity'])
        fake=deepcopy(packet); fake['confirmation_identity']['specification_digest']='different'
        with patch('week12.src.integration.execution.read',return_value=fake):
            with self.assertRaises(ValueError): approved_inputs()
        source=build_script('V11CGenerated.Scene',2,'^(time|probeOmega)$',False)
        self.assertIn('numberOfIntervals=2000',source)
        self.assertIn('Modelica 4.0.0+maint.om',source)
        self.assertNotIn('loadModel(',source)
        from week12.src.integration.timing_amendment import inputs, SPEC
        amended,vehicle,rules=inputs()
        self.assertEqual(amended['confirmation_identity']['specification_digest'],SPEC)
        self.assertEqual(vehicle['physical']['controller']['settle_until_s'],4)
        self.assertEqual(rules['physical']['settled_window_s'],[3,4])
        old_receipt={**receipt,'specification_digest':APPROVED_SPEC}
        with self.assertRaises(ValueError): check_review_receipt(old_receipt,amended['confirmation_identity'])

    def test_budget_exhaustion_and_unresolved_launch_never_reserve(self):
        batch=object.__new__(Batch)
        batch.budget={'maximum_scene_attempts':54,'new_local_execution_checks_limit':24,
                      'sysml_parse_limit':3,'identity_probe_limit':2,'maximum_rounds':3,'execution_total_wall_s':7200}
        for usage in ({'scene':54,'local':0,'sysml':0,'probe':0,'process_seconds':0,'unresolved_processes':0},
                      {'scene':0,'local':0,'sysml':0,'probe':0,'process_seconds':7200,'unresolved_processes':0},
                      {'scene':0,'local':0,'sysml':0,'probe':0,'process_seconds':0,'unresolved_processes':1}):
            with patch.object(batch,'usage',return_value=usage):
                with self.assertRaises(RuntimeError): batch.reserve('scene','must_not_write',1)
