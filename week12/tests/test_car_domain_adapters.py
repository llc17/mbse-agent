"""Reference adapter software fixtures; no mock is online or physics evidence."""
from copy import deepcopy
from pathlib import Path
import hashlib, json, os, shutil, tempfile, unittest
from week12.tests.test_reference_core_chain import reference_spec
from week12.src.car_domain.contracts import WEEK12_ROOT, validate_spec
from week12.src.car_domain.service_adapter import CarService
from week12.src.car_domain.store_adapter import CarStore
from week12.src.car_domain import reference_route as a
from week11.src.store import digest_json

class NoWorker:
    def start(self,task_id):pass
    def cancel(self,task_id):raise AssertionError('fixture launches no process')

class ReferenceAdapters(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='reference-adapter-fixture-',dir=WEEK12_ROOT/'results')
        self.service=CarService(Path(self.temp.name)/'store.sqlite','fixture',worker_manager=NoWorker())
        self.project=self.service.project_create('TEST_ONLY_reference_software_fixture')['project']
        source=self.service.record_source(reference_spec()['sources'][0]['text'])
        self.spec=reference_spec();self.spec['sources'][0]['id']=source['source_id']
        for row in self.spec['requirements']+[self.spec['design_permission']]:row['source_id']=source['source_id']
        self.task_directory=None
    def tearDown(self):
        if self.task_directory is not None:
            p=self.task_directory.parents[1].resolve()
            if p.parent!=(WEEK12_ROOT/'projects').resolve() or p.name!=self.project['project_id'] or p.is_symlink():
                raise AssertionError('unsafe fixture cleanup')
            shutil.rmtree(p)
        p=Path(self.temp.name).resolve();p.relative_to((WEEK12_ROOT/'results').resolve())
        if p.is_symlink():raise AssertionError('unsafe temporary fixture cleanup')
        self.temp.cleanup()
    def queued(self):
        p=self.service.proposal_prepare(self.spec)['proposal']
        self.service.confirm_approved_test_only(p['proposal_id'],p['displayed_digest'])
        t=self.service.execution_start(p['proposal_id'],'fixture-request')['task']
        return self.service.store.proposal(p['proposal_id']),t
    def claimed(self):
        p,t=self.queued();self.service.store.reserve_launch(t['task_id'],'owner_fixture')
        t=self.service.store.claim_task(t['task_id'],'owner_fixture',os.getpid())
        return p,t
    def fixture_result(self,p,t):
        base=WEEK12_ROOT/'projects'/t['project_id']/'versions'/t['version_id'];base.mkdir(parents=True)
        self.task_directory=base
        artifact=base/'fixture.txt';artifact.write_text('SOFTWARE_FIXTURE_NOT_ACTUAL_EVIDENCE',encoding='utf8')
        binding=dict(task_id=t['task_id'],project_id=t['project_id'],proposal_id=p['proposal_id'],session_id=t['session_id'],
            spec_digest=p['spec_digest'],displayed_digest=p['displayed_digest'],reference_source_sha256=a.SOURCE_SHA,
            reference_contract_sha256=a.CONTRACT_SHA,authorization_sha256=a.AUTH_SHA,profile=a.PROFILE,owner='owner_fixture')
        manifest=base/'fixture-manifest.json';a.save(manifest,dict(binding=binding,outcome=a.SUCCESS,
            files={'fixture.txt':dict(sha256=a.sha(artifact),bytes=artifact.stat().st_size)}))
        return {**a.SUCCESS,'outcome_manifest_path':str(manifest),'outcome_manifest_sha256':a.sha(manifest)}
    def test_source_forgery_rejected(self):
        s=deepcopy(self.spec);s['sources'][0]['text']+=' fabricated'
        with self.assertRaises(ValueError):self.service.proposal_prepare(s)
    def test_legacy_vehicle_input_closed(self):
        self.assertFalse(validate_spec({'schema_version':'v11-car-spec-1','design_mode':'REUSE_APPROVED_DESIGN'})['ready'])
    def test_different_parameters_require_new_scope(self):
        s=deepcopy(self.spec);s['parameters']['m']=90
        self.assertFalse(self.service.proposal_prepare(s)['ok'])
    def test_import_binds_actual_new_receipt(self):
        p,t=self.queued();c=p['confirmation']
        self.assertEqual(c['authorization_sha256'],a.AUTH_SHA)
        self.assertEqual(c['channel'],'EXISTING_USER_AUTHORIZATION_IMPORT')
        self.assertIs(c['human_new_approval'],False)
        self.assertEqual(c['profile'],a.PROFILE)
    def test_stale_display_cannot_confirm(self):
        p=self.service.proposal_prepare(self.spec)['proposal']
        with self.assertRaises(ValueError):self.service.confirm_approved_test_only(p['proposal_id'],'0'*64)
    def test_legacy_or_forged_receipt_rejected(self):
        p,t=self.queued();p['confirmation']['authorization_sha256']='e5eabe60008eaacb941b19032d3b658c7db35a6b44b3fdb46fea29734324f5e5'
        with self.assertRaises(a.Stop):a.verify_confirmation(p,'fixture')
    def test_duplicate_and_wrong_owner_claim_are_read_only(self):
        p,t=self.claimed();before=self.service.store.task(t['task_id'])
        for owner in ('owner_fixture','different_owner'):
            with self.assertRaises(ValueError):self.service.store.claim_task(t['task_id'],owner,os.getpid())
        self.assertEqual(before,self.service.store.task(t['task_id']))
    def test_reference_cannot_claim_vehicle_pass_or_trusted_fail(self):
        p,t=self.claimed()
        for status in ('PASS','TRUSTED_FAIL'):
            with self.assertRaises(ValueError):self.service.store.finish_task(t['task_id'],'succeeded',owner='owner_fixture',result={**a.SUCCESS,'vehicle_task_status':status})
    def test_reference_cannot_publish_even_forged_vehicle_tuple(self):
        p,t=self.claimed()
        forged=dict(vehicle_task_status='PASS',agent_execution_status='COMPLETED',tool_status='PASS',numerical_status='PASS',evidence_status='COMPLETE')
        with self.assertRaises(ValueError):self.service.store.publish_version(t['task_id'],'0'*64,
            f"projects/{t['project_id']}/versions/{t['version_id']}",owner='owner_fixture',result=forged)
        self.assertIsNone(self.service.store.project(t['project_id'])['current_version_id'])
    def test_complete_reference_fixture_stays_unpublished(self):
        p,t=self.claimed();result=self.fixture_result(p,t)
        from unittest.mock import patch
        with patch.object(a,'verify_completed_evidence'), patch('week12.src.car_domain.reference_history.verify_historical_reference'):
            self.service.store.finish_task(t['task_id'],'succeeded',owner='owner_fixture',result=result)
            self.assertTrue(self.service.evidence_query('task:'+t['task_id'])['ok'])
        self.assertEqual(self.service.store.versions(t['project_id']),[])
    def test_manifest_profile_and_evidence_drift_rejected(self):
        p,t=self.claimed();result=self.fixture_result(p,t)
        manifest=Path(result['outcome_manifest_path']);v=a.read(manifest);v['binding']['profile']='VEHICLE';a.save(manifest,v)
        result['outcome_manifest_sha256']=a.sha(manifest)
        with self.assertRaises(a.Stop):self.service.store.finish_task(t['task_id'],'succeeded',owner='owner_fixture',result=result)
    def test_cross_session_confirmation_rejected(self):
        p,t=self.queued()
        with self.assertRaises(a.Stop):a.verify_confirmation(p,'other_session')
    def test_terminal_fixture_immutable(self):
        p,t=self.claimed();self.service.store.finish_task(t['task_id'],'failed',owner='owner_fixture',error='fixture')
        with self.assertRaises(ValueError):self.service.store.finish_task(t['task_id'],'failed',owner='owner_fixture',error='changed')

    def test_actual_store_transaction_rejects_stopped_or_over_budget_success(self):
        from unittest.mock import patch
        from week12.tests.test_car_domain_route import completion_fixture, fake_transaction
        p,t=self.claimed();result=self.fixture_result(p,t)
        for case in ('scope_stopped','worker_over_budget','global_over_budget'):
            scope=completion_fixture(t)
            if case=='scope_stopped':scope['status']='STOPPED_PARENT_OR_CLEANUP'
            if case=='worker_over_budget':scope['parents'][1]['own_s']=31
            elapsed=10801 if case=='global_over_budget' else 10
            before=self.service.store.task(t['task_id'])
            with self.subTest(case=case),patch.object(a,'transaction',lambda:fake_transaction(scope)),patch.object(a,'elapsed',return_value=elapsed):
                with self.assertRaises(a.Stop):
                    self.service.store.finish_task(t['task_id'],'succeeded',owner='owner_fixture',result=result)
            self.assertEqual(before,self.service.store.task(t['task_id']))
            self.assertEqual(self.service.store.versions(t['project_id']),[])
