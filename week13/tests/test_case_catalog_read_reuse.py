"""Small actual Store/manifest fixtures; no RPC, HTTP or dynamics."""
from contextlib import ExitStack,redirect_stderr
from copy import deepcopy
import io,json
from pathlib import Path
import unittest
from unittest.mock import patch
from week13.src import case_projects as projects
from week13.src.inherited import artifact_history as history,artifact_delivery as delivery,artifact_preparation as preparation,service_adapter
from week13.src import case_contact_review as review
from week13.tests.test_case_baseline_reuse import BaselineReuseTests
from week13.tests.test_case_agent_requests import finish_fixture,synthetic_spec

class CatalogReadReuseTests(unittest.TestCase):
    def setUp(self):
        self.fixture=BaselineReuseTests('test_prepare_has_one_snapshot_one_csv_parse_and_final_hash')
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.svc=self.fixture.svc
        with redirect_stderr(io.StringIO()):
            for mass in (90,95):
                candidate=synthetic_spec(self.fixture.source,mass);candidate['baseline_ref']=self.fixture.ref
                result=self.svc.proposal_prepare(candidate)
                proposal=self.svc.store.proposal(result['proposal']['proposal_id'])
                self.svc.confirm_local(proposal['proposal_id'],proposal['displayed_digest'])
                task=self.svc.execution_start(proposal['proposal_id'],'fixture_catalog_'+str(mass))['task']
                finish_fixture(self.svc,task,self.fixture.root)
        self.case=dict(case_id='TEST_ONLY_catalog',project_id=self.fixture.project['project_id'],project_root=str(self.fixture.root),name='TEST_ONLY catalog',status='TEST_ONLY',kind='TEST_ONLY')
        self.imported=dict(self.fixture.record,version_id='v000000',task_id='reference_TEST_ONLY',record_kind='IMPORTED_REFERENCE',task_status='NOT_RUN')
        self.version_calls=[];self.reviews=[];self.listed=[]
        original_list=self.svc.versions_list
        def listed():
            result=original_list();self.listed=result['versions']['items'];return result
        def version(name):
            self.version_calls.append(name)
            if name!='v000000':raise AssertionError('latest must reuse the freshly listed snapshot')
            return deepcopy(self.imported)
        controls={key:'TEST_ONLY' for key in ('model','thinking','reasoning_effort','context_window_tokens','max_tokens','maximum_request_reserve_cny')}
        (self.fixture.root/'configs').mkdir();(self.fixture.root/'configs/case_runtime.json').write_text(json.dumps(controls),encoding='utf8')
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        for obj,name,value in ((projects,'ROOT',self.fixture.root),(projects,'list_cases',lambda:[self.case]),(projects,'verify_case',lambda case:case),(projects,'_service',lambda case:self.svc),(projects,'support_for_source',lambda source:dict(status='SUPPORTED')),(self.svc,'_artifact_version',version),(self.svc,'versions_list',listed)):
            self.stack.enter_context(patch.object(obj,name,value))
    def discover(self,callback=None):
        def checked(row,root):
            self.reviews.append(row)
            if callback:callback(row)
            return dict(row)
        with patch.object(review,'with_review',side_effect=checked),redirect_stderr(io.StringIO()):return projects.discover_case()
    def test_one_review_uses_latest_listed_snapshot_no_second_version_lookup(self):
        with patch.object(history,'snapshot',wraps=history.snapshot) as snapshots,patch.object(delivery,'read_signals',wraps=delivery.read_signals) as reads,patch.object(service_adapter,'_verify_manifest',wraps=service_adapter._verify_manifest) as final:
            result=self.discover()
        self.assertEqual(self.version_calls,['v000000'])
        self.assertEqual(len(self.reviews),1);self.assertIs(self.reviews[0],self.listed[-1])
        self.assertEqual((snapshots.call_count,reads.call_count,final.call_count),(4,4,1))
        self.assertEqual(result['default_baseline']['version_id'],'v000000')
        self.assertIsNone(preparation._active.get())
    def test_next_catalog_operation_verifies_again_no_persistent_scope(self):
        with patch.object(history,'snapshot',wraps=history.snapshot) as snapshots:
            self.discover();self.discover()
        self.assertEqual(snapshots.call_count,8);self.assertIsNone(preparation._active.get())
    def test_csv_drift_before_return_is_rejected_by_final_guard(self):
        csv=self.fixture.base/'tool/model_res.csv';raw=csv.read_bytes()
        original=history.snapshot
        def change(service,task):
            result=original(service,task)
            if task['version_id']=='v000003':csv.write_bytes(raw.replace(b'120,1',b'120,2'))
            return result
        with patch.object(history,'snapshot',side_effect=change),self.assertRaisesRegex(ValueError,'evidence changed|hash mismatch'):self.svc.versions_list()
        self.assertIsNone(preparation._active.get())
    def test_old_recorded_pass_and_test_only_latest_are_not_promoted(self):
        historical=deepcopy(self.fixture.record);historical['task_status']='PASS';historical['outcome']['test_only']=False;historical.pop('current_contact_review',None)
        projected=review.current_summary(historical)
        self.assertEqual(projected['recorded_task_status'],'PASS')
        self.assertEqual(projected['task_status'],'UNKNOWN')
        self.assertEqual(projected['current_review_state'],'NOT_REVIEWED')
        result=self.discover();self.assertFalse(result['baselines'][-1]['automatic_default_eligible'])
        self.assertEqual(result['default_baseline']['version_id'],'v000000')
    def test_missing_new_review_is_not_current_proof(self):
        result=self.discover();latest=result['baselines'][-1]
        self.assertFalse(latest['automatic_default_eligible'])
        self.assertNotEqual(latest['task_status'],'PASS')
        self.assertNotEqual(latest['current_contact_review'].get('review_state'),'CURRENT_BOUND_REVIEW')
    def test_direct_versions_list_entry_reuses_ancestor_without_catalog(self):
        with patch.object(history,'snapshot',wraps=history.snapshot) as snapshots,patch.object(delivery,'read_signals',wraps=delivery.read_signals) as reads,patch.object(service_adapter,'_verify_manifest',wraps=service_adapter._verify_manifest) as final,redirect_stderr(io.StringIO()):
            result=self.svc.versions_list()
        self.assertEqual(len(result['versions']['items']),3)
        self.assertEqual((snapshots.call_count,reads.call_count,final.call_count),(4,4,1))
        self.assertFalse(self.reviews)
        self.assertIsNone(preparation._active.get())
    def test_manifest_drift_inside_direct_listing_is_rejected(self):
        path=self.fixture.base/'artifact-manifest.json';raw=path.read_bytes();original=history.snapshot
        def change(service,task):
            result=original(service,task)
            if task['version_id']=='v000003':path.write_bytes(raw+b' ')
            return result
        with patch.object(history,'snapshot',side_effect=change),self.assertRaises(ValueError):self.svc.versions_list()
        self.assertIsNone(preparation._active.get())

if __name__=='__main__':unittest.main()
