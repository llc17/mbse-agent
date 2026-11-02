"""New TEST_ONLY fixtures: fixed assessment reuse and worker-local baselines."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json
import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from week11.src.store import digest_json
from week13.src import case_saved_assessment as saved, case_readback
from week13.src.case_paths import ROOT
from week13.src.inherited.service_adapter import file_sha256
from week13.src.inherited import artifact_baseline as baseline
from week13.tests import test_case_baseline_reuse as fixture

AREA=ROOT/'results/two-simple-capabilities-20261102-01/B'
SNAPSHOT=ROOT/'runs/case-projects/case_380e7315172241efa971/requests/console_20261101T150324_6b18efafeb/baseline-snapshot.json'

def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf8')


class SavedAssessmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_spec=json.loads(SNAPSHOT.read_bytes())['frozen_spec']

    def setUp(self):
        AREA.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='TEST_ONLY-saved-',dir=AREA)
        self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.spec=deepcopy(self.original_spec)
        self.ctx=dict(request_id='request_fixture',request_digest='1'*64,displayed_digest='2'*64,
                      baseline_ref=deepcopy(self.spec.get('baseline_ref')),project_id='project_fixture',session_id='session_fixture')
        self.task=dict(task_id='task_fixture',project_id='project_fixture',version_id='v000002',session_id='session_fixture',
                       proposal_id='proposal_fixture',request_id='case_execute_request_fixture',request_sha256='3'*64,
                       worker_owner='owner_fixture',state='succeeded')
        self.base=self.root/'projects/project_fixture/versions/v000002';self.base.mkdir(parents=True)
        self.proposal=dict(spec=self.spec,spec_digest=digest_json(self.spec),project_id='project_fixture',session_id='session_fixture',
                           operation='modify',confirmation=dict(request_id='request_fixture',request_displayed_digest='2'*64))
        import hashlib
        self.proposal['display']=dict(source_sha256=hashlib.sha256(self.spec['artifact']['modelica_source'].encode()).hexdigest(),
                                      execution_context_id='context_fixture',execution_context_sha256='4'*64)
        self.proposal['displayed_digest']=digest_json(self.proposal['display'])
        self.svc=SimpleNamespace(project_root=self.root,_require_current_project=lambda p:None,
            store=SimpleNamespace(proposal=lambda p:deepcopy(self.proposal),task=lambda t:deepcopy(self.task),
                sources=lambda sid,pid,ids:[dict(source_id=s['id'],text=s['text'],text_sha256=hashlib.sha256(s['text'].encode()).hexdigest()) for s in self.spec['sources']]))
        self.save_outcome()

    def save_outcome(self,classification='TASK_PASS'):
        good=classification in ('TASK_PASS','VALID_STALL')
        mission=dict(validity_status='PASS' if good else 'UNKNOWN',task_status='PASS' if classification=='TASK_PASS' else 'FAIL' if classification=='VALID_STALL' else 'UNKNOWN',
            route_status='PASS' if classification=='TASK_PASS' else 'FAIL',diagnostic_status='PASS' if good else 'UNKNOWN',
            completion_status='COMPLETE_FORWARD_UP20_DOWN20_EXIT_AND_CONTINUE' if classification=='TASK_PASS' else 'OBSERVATION_INTERVAL_INCOMPLETE',
            classification=classification,overall_summary=classification,issues=[])
        outcome=dict(tool_status='PASS' if good else 'TOOL_FAIL',chain_status='PASS' if good else 'BLOCKED_TOOL',
            task_status=mission['task_status'],completion_status=mission['completion_status'],mission_assessment=mission,
            vehicle_task_status=mission['task_status'],physical_acceptance_status='NOT_VERIFIED',published_version_id=None,
            runtime_parameter_readback=dict(status='PASS' if good else 'UNKNOWN',parameters={f'p{x}':x for x in range(48)}))
        for p in (self.base/'outcome.json',self.base/'artifact-manifest.json'):
            if p.exists():p.unlink()
        write(self.base/'spec.json',self.spec);write(self.base/'confirmation.json',self.proposal['confirmation'])
        (self.base/'frozen-model.mo').write_text(self.spec['artifact']['modelica_source'],encoding='utf8',newline='\n')
        (self.base/'frozen-model.sysml').write_text(self.spec['artifact'].get('sysml_source',''),encoding='utf8',newline='\n')
        (self.base/'tool').mkdir(exist_ok=True);(self.base/'tool/model_res.csv').write_bytes(b'time,x\n0,1\n1,2\n')
        outcome['saved_assessment']=saved.create_receipt(self.task,self.proposal,self.spec,outcome,self.base,self.ctx,checked_inputs=saved.assessment_inputs(self.spec,self.base))
        write(self.base/'outcome.json',outcome)
        display=self.proposal['display']
        binding=dict(task_id=self.task['task_id'],project_id=self.task['project_id'],spec_digest=self.proposal['spec_digest'],source_sha256=display['source_sha256'],
                     owner=self.task['worker_owner'],context_id=display['execution_context_id'],context_sha256=display['execution_context_sha256'],artifact_baseline=self.spec['baseline_ref'])
        files={p.relative_to(self.base).as_posix():dict(sha256=file_sha256(p),bytes=p.stat().st_size) for p in self.base.rglob('*') if p.is_file()}
        path=self.base/'artifact-manifest.json';write(path,dict(schema='v11-artifact-manifest-1',binding=binding,outcome=outcome,files=files))
        self.task['result']=dict(outcome,outcome_manifest_path=str(path),outcome_manifest_sha256=file_sha256(path),worker_exit_code=0)

    def flow(self):
        task=self.task;display=self.proposal['display']
        latest=dict(project_id=task['project_id'],task_id=task['task_id'],version_id=task['version_id'],state=task['state'],
            spec_digest=self.proposal['spec_digest'],source_sha256=display['source_sha256'],manifest_sha256=task['result']['outcome_manifest_sha256'],
            tool_status='UNKNOWN',chain_status='NOT_REVIEWED',task_status='UNKNOWN')
        state=dict(request_id=self.ctx['request_id'],project_id=self.ctx['project_id'],latest_request_execution=latest)
        return SimpleNamespace(ctx=self.ctx,refresh=lambda:None,state=lambda:deepcopy(state),open=lambda **kw:self.svc,
                               _selected_snapshot=lambda *a,**k:self.fail('full snapshot called'),
                               _protect_explanation=lambda *a,**k:self.fail('prose check called'))

    def test_success_and_valid_stall_reuse_without_any_physics_or_prose(self):
        from week13.src.case_console_result import prepare_result,concise_result_lines
        for kind,status in [('TASK_PASS','PASS'),('VALID_STALL','FAIL')]:
            with self.subTest(kind=kind):
                self.save_outcome(kind)
                with patch('week13.src.inherited.model_artifact.summarize_execution',side_effect=AssertionError('checker called')),patch('week13.src.inherited.artifact_history.snapshot',side_effect=AssertionError('snapshot called')),patch('week13.src.case_semantic_guard.protect_explanation',side_effect=AssertionError('prose called')):
                    value=saved.concise_result(self.flow());view,execution,reviewed,lines=prepare_result(value,request_id=self.ctx['request_id'])
                    shown=concise_result_lines(view,execution,reviewed,lines)
                self.assertEqual(value['saved_assessment_status'],'CONTENT_AND_CHECKER_VERIFIED_REUSED')
                self.assertEqual(execution['task_status'],status);self.assertFalse(view['result_binding_conflict'])
                self.assertEqual(len(execution['runtime_parameter_readback']['parameters']),48)
                self.assertIn('简化模型',' '.join(shown))
                self.assertNotIn('answer',value['explanation'])

    def test_tool_failure_and_interruption_never_become_valid_task_stall(self):
        self.save_outcome('TOOL_FAILURE');self.task['state']='failed'
        value=saved.concise_result(self.flow());self.assertEqual(value['execution']['tool_status'],'TOOL_FAIL')
        self.assertEqual(value['execution']['task_status'],'UNKNOWN')
        self.task['state']='uncertain';self.task['result'].pop('worker_exit_code')
        value=saved.concise_result(self.flow());self.assertEqual(value['execution']['task_status'],'UNKNOWN')
        self.assertEqual(value['execution']['state'],'uncertain')

    def test_same_size_same_mtime_csv_or_source_tamper_needs_recheck(self):
        for name in ('tool/model_res.csv','frozen-model.mo'):
            with self.subTest(name=name):
                p=self.base/name;raw=p.read_bytes();st=p.stat()
                p.write_bytes(raw[:-1]+(b'X' if raw[-1:]!=b'X' else b'Y'));os.utime(p,ns=(st.st_atime_ns,st.st_mtime_ns))
                try:
                    value=saved.concise_result(self.flow());self.assertEqual(value['saved_assessment_status'],'NEEDS_RECHECK')
                    self.assertEqual(value['execution']['task_status'],'UNKNOWN')
                    self.assertEqual(p.stat().st_size,len(raw));self.assertEqual(p.stat().st_mtime_ns,st.st_mtime_ns)
                finally:p.write_bytes(raw)

    def test_checker_change_identity_source_and_database_outcome_conflicts_refuse(self):
        original=saved.checker_binding
        with patch.object(saved,'checker_binding',side_effect=lambda spec:dict(original(spec),rule_version='changed')):
            self.assertEqual(saved.concise_result(self.flow())['saved_assessment_status'],'NEEDS_RECHECK')
        self.task['result']['mission_assessment']['task_status']='FAIL'
        self.assertIn('outcome/result',saved.concise_result(self.flow())['recheck_reason'])
        self.save_outcome();self.ctx['request_digest']='9'*64
        self.assertIn('request/task',saved.concise_result(self.flow())['recheck_reason'])

    def test_evidence_or_checker_drift_during_assessment_cannot_get_receipt(self):
        checked=saved.assessment_inputs(self.spec,self.base)
        path=self.base/'tool/model_res.csv';raw=path.read_bytes()
        path.write_bytes(raw.replace(b'0,1',b'0,9'))
        with self.assertRaisesRegex(ValueError,'during assessment'):
            saved.create_receipt(self.task,self.proposal,self.spec,{},self.base,self.ctx,checked_inputs=checked)
        path.write_bytes(raw)
        original=saved.checker_binding
        with patch.object(saved,'checker_binding',side_effect=lambda spec:dict(original(spec),rule_version='changed')):
            with self.assertRaisesRegex(ValueError,'during assessment'):
                saved.create_receipt(self.task,self.proposal,self.spec,{},self.base,self.ctx,checked_inputs=checked)

    def test_unknown_receipt_and_active_task_do_not_trigger_fallback_checker(self):
        with patch.object(saved,'verify_saved',side_effect=ValueError('no saved fixed-assessment binding')):
            self.assertEqual(saved.concise_result(self.flow())['saved_assessment_status'],'NEEDS_RECHECK')
        self.task['state']='running'
        value=saved.concise_result(self.flow());self.assertIn('still active',value['recheck_reason'])


class WorkerBaselineReuseTests(unittest.TestCase):
    def setUp(self):
        self.patch=patch.object(fixture,'EVIDENCE',AREA);self.patch.start();self.addCleanup(self.patch.stop)
        fixture.BaselineReuseTests.setUp(self)
    add_delivery=fixture.BaselineReuseTests.add_delivery

    def test_same_worker_delta_and_confirmation_share_one_verified_snapshot(self):
        from week13.src.case_scope import check_delta
        from week13.src.inherited import artifact_history as history
        proposal=dict(spec=self.candidate,operation='modify',display=dict(artifact_baseline=self.ref),confirmation=dict(artifact_baseline=self.ref))
        with patch.object(history,'snapshot',wraps=history.snapshot) as snapshots:
            with baseline.execution_baselines(self.case,task_context=self.ctx) as scope:
                check_delta(self.case,self.candidate,task_context=self.ctx)
                baseline.verify_proposal(self.case,proposal,require_selection=True,require_confirmation=True)
                scope.before_launch()
            self.assertEqual(snapshots.call_count,1)
            baseline.resolve(self.case,self.ref)
            self.assertEqual(snapshots.call_count,2)
        self.assertIsNone(baseline._execution.get())

    def test_launch_guard_catches_same_size_restored_mtime_tamper(self):
        p=self.base/'tool/model_res.csv';raw=p.read_bytes();st=p.stat()
        with baseline.execution_baselines(self.case) as scope:
            baseline.resolve(self.case,self.ref)
            p.write_bytes(raw.replace(b'120,1',b'120,2'));os.utime(p,ns=(st.st_atime_ns,st.st_mtime_ns))
            self.assertEqual(p.stat().st_size,len(raw));self.assertEqual(p.stat().st_mtime_ns,st.st_mtime_ns)
            with self.assertRaisesRegex(ValueError,'content drift'):scope.before_launch()

    def test_selection_changed_or_other_service_cannot_supply_shared_record(self):
        from week13.src.inherited import artifact_history as history
        with patch.object(history,'snapshot',wraps=history.snapshot) as snapshots:
            with baseline.execution_baselines(self.case) as scope:
                baseline.resolve(self.case,self.ref);baseline.resolve(self.svc,self.ref)
                self.assertEqual(snapshots.call_count,2)
                self.case.store.select_artifact_baseline(self.case.session_id,self.project['project_id'],None)
                with self.assertRaisesRegex(ValueError,'selection changed'):scope.before_launch()

    def test_request_mutation_cannot_reuse_otherwise_identical_baseline(self):
        with baseline.execution_baselines(self.case,task_context=self.ctx) as scope:
            baseline.resolve(self.case,self.ref)
            self.ctx['request_id']='another_request'
            with self.assertRaisesRegex(ValueError,'request context changed'):scope.before_launch()


class ExitStatusTests(unittest.TestCase):
    def test_all_null_exit_and_known_exit_categories(self):
        for state in ('queued','running','cancel_requested'):
            for receipt in (None,dict(returncode=None)):
                self.assertEqual(case_readback._exit_status(receipt,state),'WAITING_EXIT_RECEIPT')
        for state in ('succeeded','failed','cancelled','uncertain'):
            self.assertEqual(case_readback._exit_status(dict(returncode=None),state),'UNKNOWN')
        for receipt,want in [(dict(returncode=0),'FINISHED_EXIT_0'),(dict(returncode=7),'FINISHED_NONZERO_EXIT'),
            (dict(returncode=0,timed_out=True),'TIMED_OUT'),(dict(returncode=None,uncertain=True),'UNCERTAIN')]:
            self.assertEqual(case_readback._exit_status(receipt,'running'),want)

    def test_progress_null_receipt_keeps_identity_and_reports_no_liveness_claim(self):
        from week13.tests import test_case_readback_lifecycle as lifecycle
        AREA.mkdir(parents=True,exist_ok=True)
        holder=lifecycle.ReadbackLifecycleTests('test_index_and_completed_progress_do_not_open_csv_or_snapshot_or_full_result')
        with patch.object(lifecycle,'AREA',AREA):holder.setUp()
        self.addCleanup(holder.doCleanups);svc=holder.service()
        path=holder.root/'fixture.sqlite'
        with sqlite3.connect(path) as db:db.execute("UPDATE tasks SET state='running' WHERE task_id='task_7'")
        folder=holder.root/'projects/project/versions/v000007/tool';folder.mkdir(parents=True)
        receipt=folder/'compile.command.json'
        write(receipt,dict(task_id='task_7',cwd=str(folder),returncode=None))
        _,progress=case_readback.execution_progress(svc,'task_7')
        self.assertEqual(progress['stage_facts']['compile']['status'],'WAITING_EXIT_RECEIPT')
        self.assertEqual(progress['stage_facts']['worker_exit']['status'],'WAITING_EXIT_RECEIPT')
        self.assertEqual(progress['stage_facts']['compile']['process_alive'],'NOT_INFERRED')
        with sqlite3.connect(path) as db:db.execute("UPDATE tasks SET state='failed' WHERE task_id='task_7'")
        _,progress=case_readback.execution_progress(svc,'task_7')
        self.assertEqual(progress['stage_facts']['compile']['status'],'UNKNOWN')
        self.assertEqual(progress['stage_facts']['worker_exit']['status'],'UNKNOWN')
        write(receipt,dict(task_id='another_task',cwd=str(folder),returncode=None))
        with self.assertRaisesRegex(ValueError,'identity differs'):case_readback.execution_progress(svc,'task_7')


class EntryRouteTests(unittest.TestCase):
    def test_cli_concise_flag_does_not_change_full_default(self):
        from week13.src import case_cli
        calls=[]
        flow=SimpleNamespace(terminal_readback=lambda **kw:calls.append(kw) or dict(request_id='test_cli'))
        for flags in ([],['--concise']):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(case_cli.main(['request-result','--request-id','test_cli',*flags],flow_factory=lambda rid:flow),0)
        self.assertEqual(calls,[{},dict(concise=True)])

    def test_console_result_selects_concise_and_full_route(self):
        from week13.src.case_console import Console
        calls=[]
        ui=Console.__new__(Console);ui.ctx=dict(request_id='test_console');ui.catalog={}
        ui.runner=SimpleNamespace(call=lambda action,**kw:calls.append((action,kw)) or {})
        ui.refresh=lambda:None;ui.output=lambda *a:None;ui.show_result=lambda *a,**kw:None
        ui.handle('/result');ui.handle('/result full')
        self.assertEqual(calls,[('request-result',dict(request_id='test_console',concise=True)),('request-result',dict(request_id='test_console',concise=False))])


if __name__=='__main__':unittest.main()
