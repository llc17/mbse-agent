"""Offline lightweight reads and owned exit recovery; no API/compiler/processes."""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch,Mock

from week13.src import case_readback,case_worker
from week13.src.case_budget import CaseBudget
from week13.src.case_paths import ROOT
from week13.src.inherited.service_adapter import _verify_manifest,file_sha256

AREA=ROOT/'results/agent-readback-lifecycle-repair-20261101-01/A'


class ReadbackLifecycleTests(unittest.TestCase):
    def setUp(self):
        AREA.mkdir(parents=True,exist_ok=True)
        temp=tempfile.TemporaryDirectory(prefix='TEST_ONLY-readback-',dir=AREA)
        self.addCleanup(temp.cleanup);self.root=Path(temp.name)

    def service(self):
        path=self.root/'fixture.sqlite'
        with sqlite3.connect(path) as db:
            db.executescript('''CREATE TABLE tasks(task_id TEXT,session_id TEXT,project_id TEXT,proposal_id TEXT,
              request_id TEXT,request_sha256 TEXT,version_id TEXT,state TEXT,progress TEXT,worker_pid INTEGER,
              worker_owner TEXT,result_json TEXT,error TEXT,created_at TEXT,updated_at TEXT);
              CREATE TABLE proposals(proposal_id TEXT,spec_json TEXT,spec_digest TEXT,display_json TEXT);''')
            for i in range(8):
                result=dict(tool_status='PASS',chain_status='PASS',task_status='PASS' if i%2 else 'FAIL',
                    outcome_manifest_sha256='recorded_manifest',large_report='x'*200000)
                db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (f'task_{i}','session','project',f'proposal_{i}',f'request_{i}','request_sha',f'v{i:06d}',
                     'succeeded','done',100,'owner',json.dumps(result),None,str(i),str(i)))
                spec=dict(backend='model-artifact',design=dict(parameters=[dict(name='m',value=85)]),artifact=dict(sysml_source='body'))
                db.execute('INSERT INTO proposals VALUES(?,?,?,?)',(f'proposal_{i}',json.dumps(spec),'spec_sha',json.dumps(dict(source_sha256='source_sha'))))
        db.close()
        @contextmanager
        def reading():
            db=sqlite3.connect(path);db.row_factory=sqlite3.Row
            try:yield db
            finally:db.close()
        def require(project):
            if project!='project':raise ValueError('wrong project')
        return SimpleNamespace(store=SimpleNamespace(reading=reading,task=lambda _:self.fail('full task deserialized')),
            project_root=self.root,_current_project=lambda:dict(project_id='project',current_version_id='v000000',selected_version_id='v000000'),
            _require_current_project=require)

    def test_index_and_completed_progress_do_not_open_csv_or_snapshot_or_full_result(self):
        svc=self.service();opened=[];original=Path.open
        def guarded(path,*args,**kwargs):
            if path.suffix.lower()=='.csv':raise AssertionError('CSV opened')
            opened.append(path);return original(path,*args,**kwargs)
        with patch.object(Path,'open',guarded),patch('week13.src.inherited.artifact_history.snapshot',side_effect=AssertionError('snapshot called')):
            index=case_readback.execution_index(svc);task,progress=case_readback.execution_progress(svc,'task_7')
        self.assertEqual(len(index['items']),8)
        self.assertEqual(index['items'][-1]['recorded_task_status'],'PASS')
        self.assertEqual(index['items'][-1]['task_status'],'UNKNOWN')
        self.assertFalse(index['items'][-1]['recorded_conclusions_verified'])
        self.assertNotIn('result',task);self.assertNotIn('large_report',json.dumps(progress))
        self.assertEqual(progress['verification_status'],'NOT_RECHECKED')

    def test_progress_refuses_other_project_and_receipt_identity(self):
        svc=self.service()
        with patch.object(svc,'_require_current_project',side_effect=ValueError('wrong project')),self.assertRaises(ValueError):
            case_readback.execution_progress(svc,'task_0')
        folder=self.root/'projects/project/versions/v000000/tool';folder.mkdir(parents=True)
        (folder/'compile.command.json').write_text(json.dumps(dict(task_id='wrong',cwd=str(folder),returncode=0)),encoding='utf8')
        with self.assertRaisesRegex(ValueError,'identity differs'):case_readback.execution_progress(svc,'task_0')

    def test_index_sysml_field_is_body_metadata_not_current_sidecar_claim(self):
        row=case_readback.execution_index(self.service())['items'][0]
        self.assertTrue(row['source_body_has_sysml']);self.assertNotIn('requires_sysml',row)
        self.assertEqual(row['verification_status'],'NOT_RECHECKED')
        from week13.src.case_contact_review import current_summary
        summary=current_summary(row)
        self.assertEqual(summary['recorded_task_status'],'FAIL')
        self.assertNotIn('rule_version',summary['current_contact_review'])
        self.assertNotIn('capabilities',summary['current_contact_review'])

    def test_complete_hash_refuses_same_size_restored_mtime_change(self):
        target=self.root/'data.csv';target.write_bytes(b'0,10\n1,20\n')
        manifest=self.root/'manifest.json'
        manifest.write_text(json.dumps(dict(files={'data.csv':dict(sha256=file_sha256(target),bytes=target.stat().st_size)})),encoding='utf8')
        expected=file_sha256(manifest);_verify_manifest(manifest,expected,self.root)
        stat=target.stat();target.write_bytes(b'0,11\n1,20\n');os.utime(target,ns=(stat.st_atime_ns,stat.st_mtime_ns))
        with self.assertRaisesRegex(ValueError,'hash mismatch'):_verify_manifest(manifest,expected,self.root)

    def test_catalog_lists_recorded_result_without_full_history_or_current_pass(self):
        from week13.src import case_projects as projects
        baseline=dict(record_kind='IMPORTED_REFERENCE',version_id='v000000',task_id='reference',project_id='project',
            source_sha256='source',frozen_spec=dict(artifact=dict(modelica_source='model Fixture end Fixture;')),
            task_status='NOT_RUN',state='not_run',evidence_status='IMPORTED_REFERENCE')
        row=dict(record_kind=case_readback.INDEX_KIND,version_id='v000007',task_id='task',project_id='project',
            source_sha256='new_source',parameters=dict(m=85),state='succeeded',recorded_task_status='PASS',
            recorded_chain_status='PASS',source_body_has_sysml=True,recorded_requires_sysml=False)
        case=dict(case_id='case',project_id='project',project_root=str(self.root),name='Fixture',status='EXISTING',kind='TEST_ONLY')
        svc=SimpleNamespace(_artifact_version=Mock(return_value=baseline),
            versions_index=lambda:dict(versions=dict(items=[row])),versions_list=lambda:self.fail('full history used'))
        config=self.root/'configs';config.mkdir();(config/'case_runtime.json').write_text(json.dumps({key:1 for key in ('model','thinking','reasoning_effort','context_window_tokens','max_tokens','maximum_request_reserve_cny')}),encoding='utf8')
        with patch.object(projects,'ROOT',self.root),patch.object(projects,'list_cases',return_value=[case]),patch.object(projects,'_service',return_value=svc),patch.object(projects,'support_for_source',return_value=dict(status='SUPPORTED')),patch.object(projects,'verify_case',side_effect=AssertionError('deep original scan in catalog')):
            result=projects.discover_case()
        latest=result['baselines'][-1]
        self.assertEqual(latest['task_status'],'UNKNOWN');self.assertEqual(latest['recorded_task_status'],'PASS')
        self.assertTrue(latest['selection_requires_verification']);self.assertIsNone(latest['requires_sysml'])
        svc._artifact_version.assert_called_once_with('v000000')

    def test_arithmetic_cache_requires_fresh_content_binding_and_same_rule(self):
        from collections import OrderedDict
        from week13.src import case_contact_review as review,case_asym2_execution as execution
        from week13.src.inherited import service_adapter,model_artifact
        with patch.object(review,'_ASYM2_REVIEW_CACHE',OrderedDict()),patch.object(service_adapter,'_verify_manifest') as verify,patch.object(execution,'verify_run_evidence',return_value=({}, {}, dict(status='PASS'))),patch.object(execution,'confirmed_expectations',return_value={}),patch.object(model_artifact,'summarize_execution',return_value=dict(task_status='PASS')) as calculate:
            review._asym2_checked_execution({}, {}, self.root,'manifest')
            review._asym2_checked_execution({}, {}, self.root,'manifest')
            self.assertEqual(calculate.call_count,1);self.assertEqual(verify.call_count,2)
            with patch.object(review,'ASYM2_RULE_VERSION','TEST_ONLY_CHANGED_RULE'):
                review._asym2_checked_execution({}, {}, self.root,'manifest')
            self.assertEqual(calculate.call_count,2)
            verify.side_effect=ValueError('TEST_ONLY content binding drift')
            with self.assertRaises(ValueError):review._asym2_checked_execution({}, {}, self.root,'manifest')
            self.assertEqual(calculate.call_count,2)

    def manager(self):
        task=dict(state='running',project_id='project',version_id='v000001',worker_pid=101,worker_owner='owner',result=None)
        publications=[]
        def finish(task_id,state,**kwargs):
            publications.append(state);task['state']=state;return dict(task)
        def cancel(task_id):
            if task['state'] in ('queued','running','cancel_requested'):task['state']='cancel_requested'
            return dict(task)
        manager=case_worker.ArtifactWorkerManager.__new__(case_worker.ArtifactWorkerManager)
        manager.owner='owner';manager.timeout_s=1;manager._attempts={'task':'worker_attempt'}
        manager._exit_receipts={'task':self.root/'task.process-exit.json'};manager._launcher_pids={'task':999}
        manager._processes={};manager._lock=threading.Lock();manager._exit_lock=threading.RLock();manager._monitors={}
        manager.store=SimpleNamespace(task=lambda _:dict(task),request_cancel=cancel,finish_task=finish)
        return manager,task,publications

    def test_exit_file_then_ledger_failure_can_reconcile_only_same_owned_attempt(self):
        manager,task,publications=self.manager()
        process=SimpleNamespace(pid=999,wait=lambda **kw:(_ for _ in ()).throw(subprocess.TimeoutExpired('fixture',1)),poll=lambda:1)
        manager._processes['task']=process;manager._terminate_owned=lambda *_:True
        with patch.object(case_worker,'finish_process',side_effect=RuntimeError('TEST_ONLY ledger interrupted')):
            with self.assertRaises(RuntimeError):manager._monitor('task',process)
        receipt=json.loads(manager._exit_receipts['task'].read_text(encoding='utf8'))
        self.assertEqual(task['state'],'cancel_requested');self.assertEqual(publications,[])
        with patch.object(case_worker,'finish_process') as commit:
            manager.reconcile_owned_exit('task');manager.reconcile_owned_exit('task')
        self.assertEqual(publications,['failed']);self.assertEqual(commit.call_count,2)
        self.assertEqual(json.loads(manager._exit_receipts['task'].read_text(encoding='utf8')),receipt)
        with self.assertRaisesRegex(ValueError,'newly launched'):manager.reconcile_owned_exit('historical_E6')
        manager._launcher_pids['task']=123
        with patch.object(case_worker,'finish_process'),self.assertRaisesRegex(ValueError,'launcher PID'):manager.reconcile_owned_exit('task')

    def test_shutdown_cancel_and_monitor_race_preserves_one_observed_exit(self):
        manager,task,publications=self.manager();exited=threading.Event();state={'code':None};commits=[]
        process=SimpleNamespace(pid=999,poll=lambda:state['code'])
        def wait(**kw):
            if not exited.wait(2):raise subprocess.TimeoutExpired('fixture',2)
            return state['code']
        process.wait=wait
        def terminate(*args):state['code']=1;exited.set();return True
        manager._terminate_owned=terminate;manager._processes['task']=process
        thread=threading.Thread(target=manager._monitor,args=('task',process),daemon=True);manager._monitors['task']=thread
        with patch.object(case_worker,'finish_process',side_effect=lambda attempt,receipt:commits.append(deepcopy(receipt))):
            thread.start();manager.shutdown()
        self.assertFalse(thread.is_alive());self.assertEqual(publications,['cancelled'])
        self.assertTrue(commits);self.assertTrue(all(x==commits[0] for x in commits))
        self.assertEqual(commits[0]['returncode'],1);self.assertTrue(commits[0]['cleanup_confirmed'])

    def test_bounded_shutdown_marks_pending_for_stuck_daemon_monitor(self):
        manager,task,publications=self.manager();joins=[]
        manager._monitors={'task':SimpleNamespace(join=lambda **kw:joins.append(kw),is_alive=lambda:True)}
        manager.shutdown()
        path=manager._exit_receipts['task'].with_suffix('.finalization-pending.json')
        self.assertEqual(json.loads(path.read_text())['task_conclusion'],'UNKNOWN')
        self.assertEqual(joins,[dict(timeout=15)]);self.assertEqual(publications,[])

    def test_timeout_and_cancel_share_exit_lock_and_same_durable_receipt(self):
        manager,task,publications=self.manager();commits=[];waiting=threading.Event();state={'code':None}
        process=SimpleNamespace(pid=999,poll=lambda:state['code'])
        def wait(**kw):waiting.set();raise subprocess.TimeoutExpired('fixture',1)
        process.wait=wait
        def terminate(*args):state['code']=1;return True
        manager._terminate_owned=terminate;manager._processes['task']=process
        thread=threading.Thread(target=manager._monitor,args=('task',process),daemon=True)
        # Hold the same lock so the timeout branch and explicit cancellation
        # both contend for it, rather than testing only normal wait return.
        with patch.object(case_worker,'finish_process',side_effect=lambda attempt,receipt:commits.append(deepcopy(receipt))):
            manager._exit_lock.acquire()
            try:
                thread.start();self.assertTrue(waiting.wait(1))
                manager.cancel('task',lock_timeout_s=.1)
            finally:manager._exit_lock.release()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive());self.assertEqual(len(publications),1)
        self.assertTrue(commits);self.assertTrue(all(x==commits[0] for x in commits))
        self.assertEqual(commits[0]['returncode'],1)

    def test_shutdown_does_not_block_before_join_when_exit_lock_is_busy(self):
        manager,task,publications=self.manager();attempts=[]
        manager._processes['task']=SimpleNamespace(pid=999,poll=lambda:None)
        manager._exit_lock=SimpleNamespace(acquire=lambda **kw:attempts.append(kw) or False,release=lambda:self.fail('unowned lock release'))
        manager.shutdown()
        path=manager._exit_receipts['task'].with_suffix('.finalization-pending.json')
        pending=json.loads(path.read_text(encoding='utf8'))
        self.assertIn('lock busy',pending['error']);self.assertEqual(attempts,[dict(timeout=15)])
        self.assertEqual(task['state'],'running');self.assertEqual(publications,[])

    def test_budget_duplicate_receipt_is_idempotent_and_conflicts_refused(self):
        path=self.root/'budget.sqlite'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE process_attempts(attempt_id TEXT,kind TEXT,detail_json TEXT,state TEXT,reserved_at TEXT,launched_at TEXT,pid INTEGER,finished_at TEXT,result_json TEXT)')
            db.execute('INSERT INTO process_attempts VALUES(?,?,?,?,?,?,?,?,?)',('worker_attempt','worker',json.dumps(dict(task_id='task',owner='owner')),'STARTED','old','launch',999,None,None))
        db.close()
        budget=CaseBudget.__new__(CaseBudget);budget.path=path
        receipt=dict(schema='v12-owned-worker-process-exit-1',attempt_id='worker_attempt',task_id='task',owner='owner',pid=999,returncode=1,timed_out=True,uncertain=False)
        budget.finish_process('worker_attempt',receipt)
        with sqlite3.connect(path) as db:before=db.execute('SELECT * FROM process_attempts').fetchone()
        db.close()
        self.assertTrue(budget.finish_process('worker_attempt',receipt)['reused'])
        with sqlite3.connect(path) as db:self.assertEqual(db.execute('SELECT * FROM process_attempts').fetchone(),before)
        db.close()
        with self.assertRaises(RuntimeError):budget.finish_process('worker_attempt',dict(receipt,returncode=0))


if __name__=='__main__':unittest.main()
