"""Tamper rejection without OMC, subprocesses, API calls or physical fixtures."""
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from unittest import TestCase
from unittest.mock import patch
import copy
import hashlib
import json
import sys
import tempfile

from week12.src.car_domain import eightwheel_evidence as evidence
from week12.src.car_domain.outcomes import REQUIRED_SEQUENCE, make_outcome


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf8'))


class EightwheelEvidenceTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root/'projects/project/versions/v1'
        self.base.mkdir(parents=True)
        self.route = ModuleType('week12.src.car_domain.eightwheel_route')
        self.route.RUN = self.root/'run'
        self.route.PROJECT = self.root
        self.route.PROPOSAL_PATH=self.root/'numerical-contract.json'
        self.route.CONTRACT_SHA='c'*64
        self.route.AUTH_SHA='a'*64
        self.route.SPEC = self.root/'physical.json'
        self.route.read, self.route.sha = read, sha
        self.route.verify_confirmation = lambda proposal,session: None
        self.route.scene_limits = lambda name:(180,60)
        self.route.assert_binding = lambda binding: None
        self.scope = {'operations': [], 'scene_attempts': [], 'execution_claim': {'token':'claim'},
                      'parents':[{'token':'worker','label':'car-worker:task','pid':123}]}
        self.exited = True
        def scope_check(scope, task, require_worker_exit=True):
            if require_worker_exit and not self.exited:
                raise ValueError('ACTUAL_WORKER_EXIT_NOT_PASS')
        self.route.assert_completion_scope = scope_check
        @contextmanager
        def transaction():
            yield {}, self.scope
        self.route.transaction = transaction
        self.profile = ModuleType('week12.src.car_domain.eightwheel_profile')
        self.profile.PROFILE = 'active_eightwheel_v1'
        self.profile.PHYSICAL_SHA, self.profile.CONTRACT_SHA, self.profile.AUTH_SHA = 'p'*64,'c'*64,'a'*64
        self.profile.WEEK12_ROOT = self.root
        self.runner = ModuleType('week12.src.component_physics.runner')
        self.runner.OMC = self.root/'omc.exe'
        import week12.src.car_domain as package
        self.addCleanup(patch.stopall)
        patch.dict(sys.modules, {self.route.__name__:self.route, self.profile.__name__:self.profile,
                                self.runner.__name__:self.runner}).start()
        patch.object(package,'eightwheel_route',self.route,create=True).start()
        self.task = dict(task_id='task',project_id='project',version_id='v1',proposal_id='proposal',
                         session_id='session',worker_owner='owner',worker_pid=123,state='staged')
        self.proposal = dict(proposal_id='proposal',project_id='project',session_id='session',
                             spec_digest='spec',displayed_digest='display')
        self.manifest = dict(schema='v11-eightwheel-manifest-1',binding=dict(
            task_id='task',project_id='project',proposal_id='proposal',session_id='session',
            spec_digest='spec',displayed_digest='display',physical_spec_sha256='p'*64,
            scope_proposal_sha256='c'*64,authorization_sha256='a'*64,
            profile='active_eightwheel_v1',owner='owner'),files={})
        self.result = None

    def write(self,path,value):
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(value,allow_nan=False) if not isinstance(value,str) else value,encoding='utf8')
        return path

    def finish_manifest(self,rows,error=None):
        outcome=make_outcome(rows,error=error);outcome['profile']=self.profile.PROFILE
        self.manifest['outcome']=outcome
        self.manifest['files']={p.relative_to(self.base).as_posix():dict(sha256=sha(p),bytes=p.stat().st_size)
                                for p in self.base.rglob('*') if p.is_file() and p.name!='manifest.json'}
        self.write(self.base/'manifest.json',self.manifest)
        self.result=dict(outcome,outcome_manifest_path=str(self.base/'manifest.json'),
                         outcome_manifest_sha256=sha(self.base/'manifest.json'))

    def verify(self):
        return evidence.verify_manifest_binding(self.manifest,self.result,self.task,self.proposal,'owner')

    def process(self,command,cwd,label,kind='scene',scene_label=None):
        token='op'+str(len(self.scope['operations']))
        record=dict(command=list(map(str,command)),cwd=str(cwd),returncode=0,duration_s=.25,pid=321,
                    actual_started_utc='2026-10-10T01:45:00+00:00',
                    actual_ended_utc='2026-10-10T01:45:00.250000+00:00',absolute_cap_s=20,
                    timed_out=False,uncertain=False,interrupted=False,ledger_operation_token=token)
        row=dict(record,token=token,status='RETURNED',kind=kind,label=label,scene_label=scene_label,parent_token='worker')
        self.scope['operations'].append(row)
        return record,row

    def complete_fixture(self):
        from week12.src.car_domain.eightwheel_preparation import verification_command
        from week12.src.active_cluster.verify import SIGNED_WORK_ADDENDUM
        self.write(self.route.PROPOSAL_PATH,{"contract":"fixture"})
        original=self.route.RUN/'generated/task'
        package=self.write(original/'package.mo','frozen model fixture')
        patch.object(evidence,'BODY_SHA',sha(package)).start()
        graph=self.write(original/'graph.json',{})
        sysml=self.write(original/'model.sysml','package Example;')
        gm=self.write(original/'generation-manifest.json',dict(package_path=str(package),sysml_path=str(sysml)))
        self.write(self.route.SPEC,{'execution_contract_proposal':{'scene_caps':[
            {'name':name,'wall_each_s':60} for name,variant in REQUIRED_SEQUENCE if variant=='base']}})
        dependencies=[package]
        self.route.physical_dependencies=lambda package: dependencies
        mos=self.write(self.base/'frontend/frontend.mos','frontend fixture')
        inputs=[dict(path=str(package),sha256=sha(package))]
        proof=dict(status='PASS_FRONTEND_ONLY',script_sha256=sha(mos),actual_source_paths=[str(package)],input_readset=inputs)
        proof_path=self.write(self.base/'frontend/result.json',proof)
        self.write(self.base/'frontend/inputs-before.json',inputs)
        binding=dict(proposal_sha256=self.profile.PHYSICAL_SHA,scope_proposal_sha256=self.profile.CONTRACT_SHA,
            generated=dict(directory=str(original),manifest=str(gm),graph=str(graph)),
            generated_files=[dict(path=str(p),sha256=sha(p)) for p in (package,graph,sysml,gm)],
            frontend_reuse=dict(proof_path=str(proof_path),proof_sha256=sha(proof_path),inputs=inputs,model_sha256=sha(package)))
        binding_path=self.write(original/'source-binding.json',binding)
        mapping=[]
        for p in (package,graph,sysml,gm,binding_path):
            self.write(self.base/'generated-evidence'/p.name,read(p) if p.suffix=='.json' else p.read_text(encoding='utf8'))
            mapping.append(dict(original_source_path=str(p),copy=p.name,sha256=sha(p)))
        self.write(self.base/'generated-evidence/copy-source-map.json',mapping)
        front,_=self.process([self.runner.OMC,mos],mos.parent,'frontend','frontend')
        self.write(mos.parent/'frontend-process.json',front)
        parse_dir=self.base/'parse-round-01';self.write(parse_dir/'parse.json',{'status':'PASS'})
        pc=[sys.executable,'-B','-m','week12.scripts.s3_c_active_cluster','parse-child','--source',str(sysml),
            '--report',str(parse_dir/'parse.json')]
        process,_=self.process(pc,self.root,'parse','parse');self.write(parse_dir/'parse-process.json',process)
        rows=[]
        for name,variant in REQUIRED_SEQUENCE:
            label=name+'-'+variant;folder=self.base/'round-01'/label
            self.write(folder/'result.csv','time,x\n0,0\n1,1\n')
            report=dict(physical={'status':'PASS','checks':[{'code':'real_check','status':'PASS'}]},
                numeric={'status':'PASS','checks':[{'code':'whole_curve_1percent_energy_shaft_energy','status':'PASS'},{'code':'final_energy_1percent_energy_shaft_energy','status':'PASS'}]}
                if variant=='refined' else {'status':'NOT_RUN_BASE_VARIANT'})
            report['numerical_addendum']=dict(id=SIGNED_WORK_ADDENDUM,proposal_sha256=self.route.CONTRACT_SHA,authorization_sha256=self.route.AUTH_SHA)
            self.write(folder/'independent-result.json',report)
            for artifact in ('build.mos','model_init.xml','model.exe'):
                self.write(folder/artifact,'artifact fixture')
            base=self.base/'round-01'/(name+'-base') if variant=='refined' else None
            cmds={'build':([str(self.runner.OMC),'build.mos'],folder),
                  'simulation':([str(folder/'model.exe'),'-s=dassl','-r=result.csv'],folder),
                  'verification':(verification_command(folder,gm,graph,name,base,numerical_contract=self.route.PROPOSAL_PATH),self.root)}
            procs=[]
            for stage,(command,cwd) in cmds.items():
                rec,op=self.process(command,cwd,stage,scene_label=label)
                if stage=='verification':
                    paths=[folder/'result.csv',folder/'independent-result.json',gm,graph,self.route.PROPOSAL_PATH]
                    if base:paths.append(base/'result.csv')
                    rec['evidence_files']={str(p):dict(sha256=sha(p),bytes=p.stat().st_size) for p in paths}
                    op['evidence_files']=copy.deepcopy(rec['evidence_files'])
                self.write(folder/(stage+'-process.json'),rec);procs.append(op)
                for stream in ('stdout','stderr'):self.write(folder/(stage+'.'+stream+'.log'),'')
            row=dict(scene=name,variant=variant,status='PASS',compile='PASS',simulation='COMPLETED',physical='PASS',
                     numeric=report['numeric']['status'],verification_returncode=0,verification_timed_out=False,
                     verification_uncertain=False,scene_active_wall_s=1,source_binding=binding)
            self.write(folder/'scene-result.json',row);rows.append(row)
            self.scope['scene_attempts'].append(dict(label=label,status='PASS',round=1,owner_token='claim',
                processes=procs,scene_result=str(folder/'scene-result.json'),scene_active_wall_s=1))
        self.finish_manifest(rows)

    def test_full_actual_fixture_is_accepted(self):
        self.complete_fixture();self.assertTrue(self.verify())

    def test_worker_must_have_really_exited(self):
        self.complete_fixture();self.exited=False
        with self.assertRaisesRegex(ValueError,'ACTUAL_WORKER_EXIT'):self.verify()

    def test_wrong_owner_is_rejected(self):
        self.write(self.base/'evidence.json',{});self.finish_manifest([],error='blocked')
        self.task['worker_owner']='another'
        with self.assertRaisesRegex(ValueError,'WRONG_WORKER_OWNER'):self.verify()

    def test_process_from_another_worker_is_rejected(self):
        self.complete_fixture();self.scope['operations'][0]['parent_token']='other-worker'
        with self.assertRaisesRegex(ValueError,'PROCESS_NOT_OWNED_BY_WORKER'):self.verify()

    def test_symbolic_links_are_rejected(self):
        from types import SimpleNamespace
        import stat
        with patch.object(Path,'lstat',return_value=SimpleNamespace(st_mode=stat.S_IFLNK,st_file_attributes=0)):
            with self.assertRaisesRegex(ValueError,'LINK_OR_REPARSE_PATH'):
                evidence._unlinked(self.base)

    def test_unknown_failure_retains_binding_without_becoming_trusted_fail(self):
        self.write(self.base/'evidence.json',{});self.finish_manifest([],error='physical check failed')
        self.assertTrue(self.verify());self.assertEqual(self.result['vehicle_task_status'],'UNKNOWN')
        self.result['chain_status']='TRUSTED_PHYSICAL_FAIL'
        with self.assertRaisesRegex(ValueError,'IMMUTABLE_OUTCOME'):self.verify()

    def test_artifact_bytes_tamper_is_rejected(self):
        self.complete_fixture();self.write(self.base/'round-01/three_up-base/result.csv','tampered')
        with self.assertRaisesRegex(ValueError,'ARTIFACT_BYTES_DRIFT'):self.verify()

    def test_rehashed_stale_pass_report_is_rejected_by_provenance(self):
        self.complete_fixture()
        path=self.base/'round-01/three_up-refined/independent-result.json'
        report=read(path);report['physical']['diagnostics']={'stale':True};self.write(path,report)
        self.finish_manifest(self.result['results'])
        with self.assertRaisesRegex(ValueError,'VERIFIER_INPUT_OR_REPORT_DRIFT'):self.verify()

    def test_refined_numeric_checks_must_pass(self):
        with self.assertRaisesRegex(ValueError,'NUMERIC_CHECKS_NOT_PASS'):
            evidence._passed_report({'numeric':{'status':'PASS','checks':[{'code':'x','status':'FAIL'}]}},'numeric')

    def test_reordered_scene_results_are_rejected(self):
        self.complete_fixture();rows=self.result['results'];rows[0],rows[1]=rows[1],rows[0]
        self.finish_manifest(rows)
        with self.assertRaisesRegex(ValueError,'SCENE_SEQUENCE'):self.verify()

    def test_nonfinite_process_duration_is_rejected(self):
        rec,op=self.process(['tool'],self.root,'test');rec['duration_s']=float('nan');op['duration_s']=float('nan')
        with self.assertRaisesRegex(ValueError,'NONFINITE_OR_INVALID_TIME'):
            evidence._process(rec,[op],30,['tool'],self.root,set())

    def test_process_nonzero_exit_and_command_tamper_are_rejected(self):
        for change in ({'returncode':2},{'command':['other']},{'timed_out':True}):
            rec,op=self.process(['tool'],self.root,'test');rec.update(change);op.update(change)
            with self.assertRaises(ValueError):evidence._process(rec,[op],30,['tool'],self.root,set())

    def test_process_cannot_claim_a_different_ledger_return(self):
        rec,op=self.process(['tool'],self.root,'test');op['pid']=987
        with self.assertRaisesRegex(ValueError,'ACTUAL_PROCESS_LEDGER_MISMATCH'):
            evidence._process(rec,[op],30,['tool'],self.root,set())

    def test_path_escape_is_rejected_before_reading(self):
        for name in ('../outside','/outside','C:/outside','a\\outside','a/../outside'):
            with self.assertRaises(ValueError):evidence._relative_path(self.base,name)

    def test_published_version_requires_succeeded_task(self):
        self.write(self.base/'evidence.json',{});self.finish_manifest([],error='blocked')
        self.result['published_version_id']='v1'
        with self.assertRaisesRegex(ValueError,'INVALID_PUBLISHED_VERSION'):self.verify()

    def test_compile_spare_cannot_expand_remaining_phase(self):
        records={'build':{'duration_s':1},'simulation':{'duration_s':20,'actual_started_utc':'2026-10-10T01:00:00+00:00'},
                 'verification':{'duration_s':11,'actual_ended_utc':'2026-10-10T01:00:31+00:00'}}
        with self.assertRaisesRegex(ValueError,'SIMULATION_VERIFICATION_AGGREGATE_OVER_CAP'):
            evidence._phase_time(records,180,30)
