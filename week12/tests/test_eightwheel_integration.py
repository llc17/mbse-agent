"""Software-only integration checks; mocks are never dynamics or admission evidence."""
from copy import deepcopy
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch
from week12.src.car_domain import eightwheel_profile as profile
from week12.src.car_domain import eightwheel_route as route
from week12.src.car_domain.contracts import WEEK12_ROOT, validate_spec, execution_route
from week12.src.car_domain.service_adapter import CarService
from week12.tests.test_car_domain_adapters import NoWorker


class EightwheelProfileTests(unittest.TestCase):
    def test_explicit_profile_and_new_receipt(self):
        spec = profile.make_approved_spec('software_source')
        self.assertTrue(validate_spec(spec)['ready'])
        self.assertIs(execution_route(profile.PROFILE),route)
        self.assertEqual(profile.authorization()['authorization_sha256'],profile.AUTH_SHA)
        self.assertEqual(profile.authorization()['scope_id'],profile.SCOPE_ID)

    def test_legacy_and_physical_or_optional_input_drift_rejected(self):
        for changed in ('profile','physical','extra','preference'):
            spec = profile.make_approved_spec('software_source')
            if changed == 'profile': spec.pop('profile')
            elif changed == 'physical': spec['physical_ref']['sha256'] = '0'*64
            elif changed == 'extra': spec['arbitrary_model'] = 'unapproved'
            else: spec['preferences'] = [{'text':'new preference'}]
            self.assertFalse(validate_spec(spec)['ready'],changed)

    def test_adjacent_units_conflicts_and_exact_source(self):
        for changed in ('unit','conflict','forgery'):
            spec = profile.make_approved_spec('software_source')
            row = next(r for r in spec['requirements'] if r['name']=='step_height')
            if changed == 'unit':
                spec['sources'][0]['text'] = spec['sources'][0]['text'].replace('台阶高度0.15m','台阶高度0.15mm')
                row['source_text'] = '台阶高度0.15mm'
            elif changed == 'conflict': spec['sources'][0]['text'] += '；设备载荷25kg'
            else: row['source_text'] = 'fabricated source'
            self.assertFalse(validate_spec(spec)['ready'],changed)


class EightwheelStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='eightwheel-software-fixture-',dir=WEEK12_ROOT/'results')
        self.service = CarService(Path(self.temp.name)/'store.sqlite','eightwheel_fixture',
                                  worker_manager=NoWorker(),profile=profile.PROFILE)
        self.project = self.service.project_create('TEST_ONLY_eightwheel_software_fixture')['project']
        source = self.service.record_source(profile.SOURCE_TEXT)
        self.spec = profile.make_approved_spec(source['source_id'])

    def tearDown(self):
        self.temp.cleanup()

    def claimed(self):
        public = self.service.proposal_prepare(self.spec)['proposal']
        self.service.confirm_approved_test_only(public['proposal_id'],public['displayed_digest'])
        task = self.service.execution_start(public['proposal_id'],'software-fixture-request')['task']
        store = self.service.store
        store.reserve_launch(task['task_id'],'owner_fixture')
        task = store.claim_task(task['task_id'],'owner_fixture',os.getpid())
        return store.proposal(public['proposal_id']),task

    def test_explicit_service_profile_cannot_cross_dispatch(self):
        from week12.tests.test_reference_core_chain import reference_spec
        checked = validate_spec(reference_spec())
        self.assertTrue(checked['ready'])
        with self.assertRaises(ValueError): self.service.proposal_prepare(checked['spec'])

    def test_source_database_forgery_rejected(self):
        spec = deepcopy(self.spec); spec['sources'][0]['text'] += ' invented'
        with self.assertRaises(ValueError): self.service.proposal_prepare(spec)

    def test_actual_import_binds_combined_contract_and_no_fake_human(self):
        proposal,task = self.claimed()
        confirmation = proposal['confirmation']
        self.assertEqual(confirmation['authorization_sha256'],profile.AUTH_SHA)
        self.assertEqual(confirmation['execution_contract_sha256'],profile.CONTRACT_SHA)
        self.assertEqual(confirmation['generated_body_sha256'],profile.GENERATED_BODY_SHA)
        self.assertIs(confirmation['human_new_approval'],False)
        self.assertNotIn('reference_contract_sha256',confirmation)
        route.verify_confirmation(proposal,task['session_id'])
        proposal['confirmation']['authorization_sha256'] = '0'*64
        with self.assertRaises(route.Stop): route.verify_confirmation(proposal,task['session_id'])

    def test_wrong_owner_and_terminal_overwrite_rejected(self):
        proposal,task = self.claimed(); store = self.service.store
        before = store.task(task['task_id'])
        with self.assertRaises(ValueError): store.claim_task(task['task_id'],'wrong_owner',os.getpid())
        self.assertEqual(before,store.task(task['task_id']))
        store.finish_task(task['task_id'],'failed',owner='owner_fixture',error='fixture unknown')
        with self.assertRaises(ValueError): store.finish_task(task['task_id'],'failed',owner='owner_fixture',error='changed')

    def test_eightwheel_cannot_finish_from_summary_or_trusted_fail(self):
        proposal,task = self.claimed(); store = self.service.store
        for result in (dict(route.SUCCESS),{**route.SUCCESS,'vehicle_task_status':'TRUSTED_FAIL'}):
            with self.assertRaises(ValueError): store.finish_task(task['task_id'],'succeeded',owner='owner_fixture',result=result)
        self.assertIsNone(store.project(task['project_id'])['current_version_id'])
        self.assertEqual(store.versions(task['project_id']),[])

    def test_atomic_publish_invokes_actual_final_evidence_gate(self):
        proposal,task = self.claimed(); store = self.service.store
        directory = 'projects/'+task['project_id']+'/versions/'+task['version_id']
        path = WEEK12_ROOT/directory/'eightwheel-outcome-manifest.json'
        result = {**route.SUCCESS,'published_version_id':None,'outcome_manifest_path':str(path),
                  'outcome_manifest_sha256':'0'*64}
        manifest = {'schema':'v11-eightwheel-manifest-1','project_id':task['project_id'],'task_id':task['task_id']}
        store.stage_result(task['task_id'],owner='owner_fixture',result=result)
        before = store.task(task['task_id'])
        with patch('week12.src.car_domain.service_adapter._verify_manifest',return_value=manifest), \
             patch.object(route,'verify_manifest_binding',side_effect=route.Stop('ACTUAL_WORKER_EXIT_UNPROVEN')) as verifier:
            with self.assertRaises(route.Stop):
                store.publish_version(task['task_id'],'0'*64,directory,owner='owner_fixture',result=result)
            self.assertIs(verifier.call_args.kwargs['require_worker_exit'],True)
        self.assertEqual(before,store.task(task['task_id']))
        self.assertIsNone(store.project(task['project_id'])['current_version_id'])
        self.assertEqual(store.versions(task['project_id']),[])

    def test_fail_unknown_and_forged_published_result_never_publish(self):
        proposal,task = self.claimed(); store = self.service.store
        directory = 'projects/'+task['project_id']+'/versions/'+task['version_id']
        for changed in ('UNKNOWN','TRUSTED_FAIL'):
            result = {**route.SUCCESS,'vehicle_task_status':changed}
            with self.assertRaises(ValueError): store.publish_version(task['task_id'],'0'*64,directory,owner='owner_fixture',result=result)
        self.assertIsNone(store.project(task['project_id'])['current_version_id'])


if __name__ == '__main__':
    unittest.main()
