import tempfile
from pathlib import Path
import unittest
from week12.src.active_cluster.execution import Stop,save,read,execute_scene,ORDER
from week12.src.active_cluster.run_scope import claim_once,reconcile_start_failure,KEY
from week12.src.active_cluster import run_policy as policy

class ScopeTests(unittest.TestCase):
    def test_real_scene_entry_refuses_bare_frontend_flag_before_reserve(self):
        class LegacyRoute:
            def assert_sources(self,binding):pass
            def reserve(self,*args):raise AssertionError('must not reserve')
        with self.assertRaisesRegex(Stop,'UNVERIFIED_FRONTEND_REUSE'):
            execute_scene(LegacyRoute(),{}, {},{'frontend_reuse':True},ORDER[0])
    def test_fully_mocked_approved_order_and_terminal(self):
        rows=[];gates={}
        with tempfile.TemporaryDirectory() as tmp:
            claim_once(tmp,'approved',1)
            policy.validate_admission({'status':'ACCEPTED_WITHIN_USER_SCOPE',
                'binding_sha256':'binding','readiness_result_sha256':'actualtests',
                'independent_review_sha256':'actualreview'},'binding','actualtests','actualreview')
            for name in ORDER:
                for variant in ('base','refined'):
                    label=name+'-'+variant
                    policy.next_scene(rows,4,gates,label)
                    rows.append({'round':4,'label':label,'status':'PASS'})
                gates[name]='PASS'
            self.assertEqual(len(rows),14)
            with self.assertRaises(Stop):policy.next_scene(rows,4,gates,ORDER[0]+'-base')
    def test_duplicate_claim_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            claim_once(tmp,'first',1)
            with self.assertRaises(Stop):claim_once(tmp,'second',1)
            self.assertEqual(read(Path(tmp)/'remediation-launch-01.claim.json')['nonce'],'first')
    def test_constructor_failure_closes_reserved_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            save(Path(tmp)/'ledger.json',{KEY:{'launchers':[{'nonce':'n','status':'LAUNCH_REQUESTED'}]}})
            reconcile_start_failure(tmp,'n','badreceipt')
            d=read(Path(tmp)/'ledger.json')
            self.assertEqual(d[KEY]['launchers'][0]['status'],'FAILED_START')
            self.assertEqual(d[KEY]['status'],'STOPPED_IMPLEMENTATION')
    def test_wrong_nonce_cannot_rewrite_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            original={KEY:{'launchers':[{'nonce':'n','status':'LAUNCH_REQUESTED'}]}}
            save(Path(tmp)/'ledger.json',original)
            reconcile_start_failure(tmp,'wrong','bad')
            self.assertEqual(read(Path(tmp)/'ledger.json'),original)
    def test_returned_intent_not_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            original={KEY:{'launchers':[{'nonce':'n','status':'RETURNED'}]}}
            save(Path(tmp)/'ledger.json',original)
            reconcile_start_failure(tmp,'n','bad')
            self.assertEqual(read(Path(tmp)/'ledger.json'),original)
