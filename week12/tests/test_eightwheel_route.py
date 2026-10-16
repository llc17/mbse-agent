"""Budget/order safety of the new scope, using synthetic in-memory state only."""
import copy, unittest
from unittest.mock import patch
from contextlib import contextmanager
from week12.src.car_domain import eightwheel_route as a

class EightwheelRoutePolicy(unittest.TestCase):
    def state(self):
        return dict(status='IMPLEMENTING',started_utc=a.stamp(),external_s=0,counts={k:0 for k in ('scenes','scene_children','repairs','parents','frontend','parse','checks','final_checks','product_requests')},
            parents=[],operations=[],scene_attempts=[],parse_attempts=[],frontend_attempts=[],gates={},product_tokens=0,product_usd=0)
    def test_cap_matches_combined_proposal_and_retired_prefix(self):
        value=a.read(a.RUN/'deadline-recovery-activation.json')
        self.assertEqual(value['caps'],a.CAPS)
        data=a.read(a.LEDGER);a.validate_authority(data)
        data['arbitrary_old_history']=True
        with self.assertRaises(a.Stop):a.validate_authority(data)
    def test_milestone_and_stopped_scope_never_resume(self):
        s=self.state()
        with patch.object(a,'elapsed',return_value=a.RECOVERY_DEADLINE+1):
            with self.assertRaises(a.Stop):a.assert_scope(s)
        s['status']='STOPPED_TEST'
        with self.assertRaises(a.Stop):a.assert_scope(s,allow_affected=True)
    def test_zero_product_and_no_unknown_budget(self):
        s=self.state()
        @contextmanager
        def tx():yield {},s
        with patch.object(a,'transaction',tx):
            with self.assertRaises(a.Stop):a.reserve_product_request('forbidden')
        self.assertEqual(s['counts']['product_requests'],0)
    def test_scene_sequence_single_set_and_no_retry(self):
        s=self.state();route=object.__new__(a.EightwheelRoute)
        route.claim_token='claim';route.task_id='task';route.owner='owner';route.pid=1;route.spec=a.read(a.SPEC)
        s['execution_claim']=dict(token='claim',task_id='task',owner='owner',pid=1)
        @contextmanager
        def tx():yield {},s
        with patch.object(a,'transaction',tx):
            with self.assertRaises(a.Stop):route.reserve('scene','three_up-base',180)
            with self.assertRaises(a.Stop):route.reserve('scene','active_joint_energy-base',210,2)
            slot=route.reserve('scene','active_joint_energy-base',210)
            route.finish(slot,'FAIL')
            with self.assertRaises(a.Stop):route.reserve('scene','active_joint_energy-refined',210)
            with self.assertRaises(a.Stop):route.reserve('scene','active_joint_energy-base',30)
    def test_source_closure_contains_cleanup_and_physical_inputs(self):
        paths=[r['path'].replace('\\','/') for r in a.source_files()]
        self.assertIn('week9/src/modelica_tool.py',paths)
        self.assertIn('week9/src/hard_gate.py',paths)
        self.assertIn('week12/src/active_cluster/verify.py',paths)
        self.assertTrue(any(p.endswith('V11ActiveClusterC3/package.mo') for p in paths))
    def test_confirmation_bound_to_actual_approved_scope(self):
        from week12.src.car_domain.eightwheel_profile import make_approved_spec
        from week11.src.store import digest_json
        spec=make_approved_spec('source');display={}
        c=dict(channel='EXISTING_USER_AUTHORIZATION_IMPORT',human_new_approval=False,profile=a.PROFILE,
            authorization_sha256=a.AUTH_SHA,execution_contract_sha256=a.CONTRACT_SHA,
            physical_spec_sha256=a.PHYSICAL_SHA,execution_scope_id=a.authorization()['scope_id'],
            generated_body_sha256=a.GENERATED_BODY_SHA,
            spec_digest=digest_json(spec),displayed_digest=digest_json(display))
        p=dict(session_id='s',spec=spec,display=display,spec_digest=digest_json(spec),displayed_digest=digest_json(display),confirmation=c)
        a.verify_confirmation(p,'s');c['execution_scope_id']='old'
        with self.assertRaises(a.Stop):a.verify_confirmation(p,'s')

    def complete_scope(self):
        s=self.state();s['counts'].update(scenes=15,scene_children=43,frontend=2,parse=2)
        s['gates'].update({n:'PASS' for n in a.ORDER});s['gates']['admission']='PASS'
        s['admission']={k+suffix:'fixture' for k in ('binding','readiness','review') for suffix in ('_path','_sha256')}
        s['execution_claim']=dict(task_id='task',owner='owner',pid=42)
        s['parents']=[dict(label='car-worker:task',token='worker',parent_token=None,status='RETURNED',pid=42,actual_exit_code=0,own_s=0)]
        return s,dict(task_id='task',worker_owner='owner',worker_pid=42)
    def test_completion_requires_real_exit_owner_and_all_counts(self):
        s,task=self.complete_scope()
        with patch.object(a,'sha',return_value='fixture'),patch.object(a,'read',return_value={}),patch.object(a,'assert_binding'),patch.object(a,'assert_archived_fail_preserved'),patch.object(a,'assert_retained_pair_pass'):
            a.assert_completion_scope(s,task)
            for key,value in [('scene_children',41),('scenes',13)]:
                changed=copy.deepcopy(s);changed['counts'][key]=value
                with self.assertRaises(a.Stop):a.assert_completion_scope(changed,task)
            changed=copy.deepcopy(s);changed['parents'][0]['actual_exit_code']=2
            with self.assertRaises(a.Stop):a.assert_completion_scope(changed,task)
            changed=copy.deepcopy(s);changed['execution_claim']['owner']='other'
            with self.assertRaises(a.Stop):a.assert_completion_scope(changed,task)
    def test_completion_rejects_account_gap_or_unfinished_descendant(self):
        s,task=self.complete_scope()
        with patch.object(a,'sha',return_value='fixture'),patch.object(a,'read',return_value={}),patch.object(a,'assert_binding'),patch.object(a,'assert_archived_fail_preserved'),patch.object(a,'assert_retained_pair_pass'):
            changed=copy.deepcopy(s);changed['external_s']=1
            with self.assertRaises(a.Stop):a.assert_completion_scope(changed,task)
            changed=copy.deepcopy(s);changed['operations']=[dict(parent_token='worker',status='RUNNING')]
            with self.assertRaises(a.Stop):a.assert_completion_scope(changed,task)

    def test_separate_compile_budget_and_durable_instrumentation(self):
        route=object.__new__(a.EightwheelRoute)
        self.assertEqual(route.scene_budget('active_joint_energy'),(180,30))
        self.assertEqual(route.scene_wall('active_joint_energy',30),210)
        text=route.instrument_build('ok := loadFile("x");\ncheck := checkModel(X);\nbuild := buildModel(X);\n')
        for phase in ('LOAD','CHECK_MODEL','BUILD_MODEL'):
            self.assertIn(phase+'_ENTERED',text)
            self.assertIn(phase+'_RETURNED',text)
        self.assertIn('writeFile("build-progress.log"',text)

    def test_actual_launched_timeout_keeps_phase_and_tool_failure(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from week12.src.active_cluster.execution import execute_scene
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            def child(slot,command,cwd,wall,logs,label):
                a.save(Path(logs)/'build-process.json',{'timed_out':True})
                (Path(logs)/'build-progress.log').write_text('BUILD_MODEL_ENTERED\n',encoding='utf8')
                raise a.Stop('CHILD_TOOL_OR_CLEANUP_NOT_TRUSTED')
            route=SimpleNamespace(spec=a.read(a.SPEC),assert_sources=lambda binding:None,
                reserve=lambda *args:('scene_attempts',0),scene_wall=lambda name,old:210,
                scene_budget=lambda name:(180,30),scene_directory=lambda *args:root/'scene',
                child=child,finish=lambda *args,**kwargs:None)
            manifest={'package_path':str(root/'package.mo'),'output_contract':{},
                      'variable_filter':'^(time|o_.*)$','model_class_by_scene':{a.ORDER[0]:'Example.Probe'}}
            result,_=execute_scene(route,manifest,{}, {},a.ORDER[0])
            self.assertEqual(result['compile'],'TIMEOUT')
            self.assertEqual(result['classification'],'TOOL_FAIL')
            self.assertEqual(result['simulation'],'NOT_RUN')
            self.assertEqual(result['compile_phase_progress'],['BUILD_MODEL_ENTERED'])

    def test_all_seven_actual_generated_contracts_normalize_canonical_units(self):
        from week12.src.active_cluster.verify import _normalize_scene
        inputs=a.archived_inputs(); manifest=a.read(inputs['manifest']); graph=a.read(inputs['graph'])
        contract=manifest['output_contract']
        self.assertEqual(set(contract['scenes']),set(a.ORDER))
        for name in a.ORDER:
            with self.subTest(scene=name):
                local,info=_normalize_scene(contract,name,a.read(a.SPEC),graph)
                self.assertEqual(local['columns']['elastic_energy'],'o_elastic_energy')
                self.assertEqual(local['columns']['failure_code'],'o_failure_code')
                self.assertEqual(local['columns']['mechanical_energy'],'o_mechanical_energy')
                self.assertEqual(local['definitions']['power'],'tau*wrel')
                self.assertEqual({b['id'] for b in local['bodies']},set(contract['scenes'][name]['metadata']['body_ids']))
                self.assertTrue(all(w['radius']>0 for w in local['wheels']))
                self.assertEqual(local['_gravity'],0 if name=='active_joint_energy' else 9.81)

    def test_missing_canonical_energy_does_not_fallback_to_unrelated_alias(self):
        from week12.src.active_cluster.verify import _normalize_scene,EvidenceMissing
        inputs=a.archived_inputs(); manifest=a.read(inputs['manifest']); contract=copy.deepcopy(manifest['output_contract'])
        columns=contract['scenes']['active_joint_energy']['columns']
        columns['elastic']=columns.pop('elastic_energy')
        with self.assertRaises(EvidenceMissing):
            _normalize_scene(contract,'active_joint_energy',a.read(a.SPEC),a.read(inputs['graph']))

    def test_archived_failure_is_preserved_not_promoted_to_pass(self):
        a.assert_archived_fail_preserved(self.state())
        for status in ('PASS','REQUIRES_EVIDENCE'):
            report={'physical':{'status':status,'checks':[{'code':'actual_task_completion','status':'FAIL'}]}}
            with patch.object(a,'archived_inputs',return_value={'original_failed_check':'fixture'}),patch.object(a,'read',return_value=report):
                with self.assertRaises(a.Stop):a.assert_archived_fail_preserved(self.state())
        # New check is explicitly authorized once, but admission is mandatory.
        with self.assertRaises(a.Stop):a.assert_retained_pair_pass(self.state())

    def test_fresh_probe_failure_blocks_all_later_scenes(self):
        # Original actual_task_completion gate is unchanged. reserve() requires
        # first fresh Probe PASS; preserved old FAIL is never a new scene PASS.
        s=self.state();route=object.__new__(a.EightwheelRoute)
        route.claim_token='claim';route.task_id='task';route.owner='owner';route.pid=1;route.spec=a.read(a.SPEC)
        s['execution_claim']=dict(token='claim',task_id='task',owner='owner',pid=1)
        @contextmanager
        def tx():yield {},s
        with patch.object(a,'transaction',tx):
            slot=route.reserve('scene','active_joint_energy-base',210)
            route.finish(slot,'FAIL')
            self.assertEqual(s['counts']['scenes'],1)
            with self.assertRaises(a.Stop):route.reserve('scene','active_joint_energy-refined',210)
            with self.assertRaises(a.Stop):route.reserve('scene','one_twin_module_load_cycle-base',210)

    def test_retained_pair_missing_gate_blocks_claim_before_OMC(self):
        s=self.state();s['counts']['archived_checks']=0
        with self.assertRaises(a.Stop):a.assert_retained_pair_pass(s)

    def test_explicit_addendum_command_and_final_provenance_are_required(self):
        from week12.src.car_domain.eightwheel_preparation import verification_command
        old=verification_command('scene','manifest','graph','active_joint_energy')
        new=verification_command('scene','manifest','graph','active_joint_energy',numerical_contract=a.PROPOSAL_PATH)
        self.assertNotIn('--numerical-contract',old)
        self.assertEqual(new[-2:],['--numerical-contract',str(a.PROPOSAL_PATH)])

    def test_retained_pair_rehashed_nonzero_process_is_rejected(self):
        report=dict(status='PASS',proposal_sha256=a.BASE_CONTRACT_SHA,authorization_sha256=a.BASE_AUTH_SHA,
            inputs_sha256='hash',addendum='signed-shaft-work-cycle-1percent-v1')
        for k in ('base_physical','refined_physical','numeric'):
            report[k]=dict(status='PASS',checks=[dict(code='x',status='PASS')])
        proc=dict(command=a.retained_pair_command(),cwd=str(a.PROJECT.resolve()),returncode=2,
            timed_out=False,uncertain=False,interrupted=False,duration_s=1)
        s=self.state();s['retained_pair_check']=dict(status='PASS',report_sha256='hash',process_sha256='hash',inputs_sha256='hash')
        def read(p):return proc if str(p).endswith('process.json') else report
        with patch.object(a,'retained_pair_inputs',return_value={}),patch.object(a,'sha',return_value='hash'),patch.object(a,'read',side_effect=read):
            with self.assertRaises(a.Stop):a.assert_retained_pair_pass(s)

    def test_amendment_retains_prior_usage_and_rejects_rewritten_operations(self):
        data=a.read(a.LEDGER)
        a.validate_authority(data)
        changed=copy.deepcopy(data)
        changed[a.KEY]['operations'][0]['returncode']=99
        with self.assertRaises(a.Stop):a.validate_authority(changed)
        changed=copy.deepcopy(data)
        changed[a.KEY]['counts']['archived_checks']=0
        with self.assertRaises(a.Stop):a.validate_authority(changed)
