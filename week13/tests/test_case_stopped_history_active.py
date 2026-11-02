"""Isolated transactional fixtures; no live Store writes, worker, API or CSV."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import hashlib,json
from pathlib import Path
import sqlite3,tempfile,threading
import unittest
from unittest.mock import patch

from week11.src.store import Store,canonical_json,digest_json
from week13.src.case_paths import ROOT
from week13.src.inherited.artifact_service import ArtifactStore
from week13.src.inherited import artifact_active as active

AREA=ROOT/'results/agent-readback-lifecycle-repair-20261101-01/repair2'


class StoppedHistoryActiveTests(unittest.TestCase):
    def write(self,path,value):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value),encoding='utf8')
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def setUp(self):
        AREA.mkdir(parents=True,exist_ok=True);temp=tempfile.TemporaryDirectory(prefix='TEST_ONLY-active-',dir=AREA)
        self.addCleanup(temp.cleanup);self.root=Path(temp.name);self.store=ArtifactStore(self.root/'projects.sqlite')
        self.old_session='old_session';self.new_session='new_session';self.old_rid='old_request'
        self.project=self.store.create_project(self.old_session,'TEST_ONLY history active')
        self.store.open_project(self.new_session,self.project['project_id'])
        self.folder=self.root/'requests'/self.old_rid
        self.context_path=self.folder/'execution-context.json'
        context_sha=self.write(self.context_path,dict(context_id='old_context'))
        self.old_proposal=self.proposal('old_proposal',self.old_session,'old_context',context_sha)
        self.old_task,_=Store.create_task(self.store,self.old_proposal['proposal_id'],self.old_session,'case_execute_'+self.old_rid)
        now=datetime.now(timezone.utc);self.launch=(now-timedelta(minutes=20)).isoformat();self.ended=(now-timedelta(minutes=10)).isoformat()
        self.authority_path=self.root/'budget/authorization.json'
        caps={key:2 for key in ('http_requests','product_cny','total_tokens','total_output_tokens','compile','simulation','candidates','software_repair_rounds','parameter_revision_rounds','observation_extensions')}
        controls=dict(model='deepseek-flash',thinking='enabled',reasoning_effort='high',max_tokens=393216,
            context_window_tokens=1000000,automatic_retries=0,max_requests=2,max_total_tokens=2,max_total_output_tokens=2,max_cny=2)
        self.authority=dict(schema='v12-request-budget-authorization-1',project_id=self.project['project_id'],
            approval_quote='确认',approval_channel='TRUSTED_LOCAL_CLI',context_bindings={},caps=caps,
            model_controls=controls,permissions=dict(planning_http=True,execution=True),ledger_path=str(self.root/'budget/budget.sqlite'),
            started_at_utc=(now-timedelta(hours=2)).isoformat(),hard_deadline_utc=(now-timedelta(hours=1)).isoformat())
        authority_sha=self.write(self.authority_path,self.authority)
        original=dict(request_id=self.old_rid,project_id=self.project['project_id'])
        self.write(self.folder/'request.json',original)
        self.binding_path=self.root/'budget/context-bindings/binding.json'
        self.binding=dict(schema='v12-trusted-budget-context-binding-1',authorization_sha256=authority_sha,
            request_id=self.old_rid,request_digest=digest_json(original),project_id=self.project['project_id'],
            channel='TRUSTED_LOCAL_CLI',approval_text='确认')
        binding_sha=self.write(self.binding_path,self.binding)
        self.ctx=dict(request_id=self.old_rid,session_id=self.old_session,project_id=self.project['project_id'],
            project_root=str(self.root),db_path=str(self.store.db_path),root=str(self.folder),request_digest=digest_json(original),
            execution_context_path=str(self.context_path),runtime_authority=dict(path=str(self.authority_path),sha256=authority_sha,
                ledger_path=str(self.root/'budget/budget.sqlite'),binding_path=str(self.binding_path),binding_sha256=binding_sha))
        self.approval=dict(schema='v12-trusted-request-confirmation-1',request_id=self.old_rid,
            request_digest=digest_json(original),displayed_digest='request_display_digest',approval_text='确认',
            channel='TRUSTED_LOCAL_CLI',product_self_approval=False)
        self.approval_path=self.folder/'confirmations/request_display_digest.json';self.write(self.approval_path,self.approval)
        self.ctx.update(stage='CONFIRMED',displayed_digest='request_display_digest',confirmation_path=str(self.approval_path))
        self.write(self.folder/'task-context.json',self.ctx)
        self.receipt=dict(schema='v12-owned-worker-process-exit-1',attempt_id='worker_attempt',task_id=self.old_task['task_id'],
            owner='old_owner',pid=999,returncode=1,timed_out=True,uncertain=False,cleanup_confirmed=True,
            exit_observation='OWNED_TIMEOUT_CLEANUP_AND_POLL',observed_at_utc=self.ended)
        self.exit_path=self.root/'projects'/self.project['project_id']/'task-logs'/(self.old_task['task_id']+'.process-exit.json')
        self.write(self.exit_path,self.receipt)
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET state='cancel_requested',worker_owner='old_owner',worker_pid=998 WHERE task_id=?",(self.old_task['task_id'],))
            db.execute('INSERT INTO car_launch_intents VALUES(?,?,?)',(self.old_task['task_id'],'old_owner',self.launch))
            confirmation=dict(self.old_proposal['confirmation'],channel='trusted_local_request_design_confirmation',
                request_id=self.old_rid,request_displayed_digest='request_display_digest',approval=self.approval)
            db.execute('UPDATE proposals SET confirmation_json=? WHERE proposal_id=?',(canonical_json(confirmation),self.old_proposal['proposal_id']))
        with closing(sqlite3.connect(self.root/'budget/budget.sqlite')) as db:
            db.execute('CREATE TABLE process_attempts(attempt_id TEXT,kind TEXT,state TEXT,pid INTEGER,launched_at TEXT,detail_json TEXT,result_json TEXT)')
            db.execute('INSERT INTO process_attempts VALUES(?,?,?,?,?,?,?)',('worker_attempt','worker','STARTED',999,self.launch,
                json.dumps(dict(task_id=self.old_task['task_id'],owner='old_owner')),None))
            db.execute('CREATE TABLE meta(key TEXT,value TEXT)')
            db.execute('INSERT INTO meta VALUES(?,?)',('authorization_sha256',authority_sha));db.commit()
        self.new_proposal=self.proposal('new_proposal',self.new_session,'new_context','new_context_hash')

    def proposal(self,name,session,context,context_sha):
        project=self.store.project(self.project['project_id']);spec=dict(backend='model-artifact',sources=[],value=name)
        display=dict(execution_context_id=context,execution_context_sha256=context_sha)
        result=self.store.insert_proposal(dict(proposal_id=name,session_id=session,project_id=project['project_id'],
            operation='create',baseline_version_id=project['current_version_id'],project_revision=project['revision'],
            spec=spec,spec_digest=digest_json(spec),display=display,displayed_digest=digest_json(display)))
        return self.store.confirm(name,result['displayed_digest'],session)

    def create(self,request='new_execution'):
        return self.store.create_task(self.new_proposal['proposal_id'],self.new_session,request)

    def task_count(self):
        with self.store.reading() as db:return db.execute('SELECT count(*) FROM tasks').fetchone()[0]

    def test_positive_owned_exit_allows_new_task_keeps_old_task_and_recovery_exact(self):
        with self.store.reading() as db:
            old=dict(db.execute('SELECT * FROM tasks WHERE task_id=?',(self.old_task['task_id'],)).fetchone())
            recovery=dict(db.execute('SELECT * FROM recovery_context WHERE task_id=?',(self.old_task['task_id'],)).fetchone())
        task,created=self.create();self.assertTrue(created);self.assertEqual(task['state'],'queued')
        with self.store.reading() as db:
            self.assertEqual(dict(db.execute('SELECT * FROM tasks WHERE task_id=?',(self.old_task['task_id'],)).fetchone()),old)
            self.assertEqual(dict(db.execute('SELECT * FROM recovery_context WHERE task_id=?',(self.old_task['task_id'],)).fetchone()),recovery)
            self.assertIsNotNone(db.execute('SELECT * FROM recovery_context WHERE task_id=?',(task['task_id'],)).fetchone())
            proof=json.loads(db.execute('SELECT proof_json FROM artifact_stopped_history_exclusions WHERE task_id=?',(task['task_id'],)).fetchone()[0])
        self.assertTrue(proof['authority_expired']);self.assertFalse(proof['original_task_or_budget_modified'])

    def test_missing_exit_and_wrong_owner_pid_attempt_or_uncertain_cleanup_block(self):
        for field,value in [('owner','wrong'),('pid',123),('attempt_id','wrong'),('uncertain',True),('cleanup_confirmed',False),('returncode',None)]:
            with self.subTest(field=field):
                self.write(self.exit_path,dict(self.receipt,**{field:value}))
                with self.assertRaisesRegex(ValueError,'another execution is active'):self.create()
                self.assertEqual(self.task_count(),1)
        self.exit_path.unlink()
        with self.assertRaisesRegex(ValueError,'another execution is active'):self.create()

    def test_queued_running_or_unknown_budget_state_remain_blocking(self):
        for state in ('queued','running'):
            with self.store.transaction() as db:db.execute('UPDATE tasks SET state=? WHERE task_id=?',(state,self.old_task['task_id']))
            with self.assertRaisesRegex(ValueError,'another execution is active'):self.create()
        with self.store.transaction() as db:db.execute("UPDATE tasks SET state='uncertain' WHERE task_id=?",(self.old_task['task_id'],))
        with closing(sqlite3.connect(self.root/'budget/budget.sqlite')) as db:
            db.execute("UPDATE process_attempts SET state='UNKNOWN'");db.commit()
        with self.assertRaisesRegex(ValueError,'another execution is active'):self.create()

    def test_context_authority_binding_content_changes_are_not_trusted(self):
        for path in (self.context_path,self.authority_path,self.binding_path,self.folder/'request.json'):
            with self.subTest(path=path.name):
                raw=path.read_bytes();path.write_bytes(raw+b' ')
                # request JSON semantic whitespace is allowed by the original
                # request digest; an actual semantic mutation must be rejected.
                if path.name=='request.json':self.write(path,dict(request_id='wrong'))
                try:
                    with self.assertRaisesRegex(ValueError,'another execution is active'):self.create()
                finally:path.write_bytes(raw)

    def test_direct_context_binding_does_not_require_a_shared_binding_file(self):
        self.authority['context_bindings']={self.old_rid:self.ctx['request_digest']}
        self.ctx['runtime_authority']['sha256']=self.write(self.authority_path,self.authority)
        self.ctx['runtime_authority'].pop('binding_path');self.ctx['runtime_authority'].pop('binding_sha256')
        self.write(self.folder/'task-context.json',self.ctx);self.binding_path.unlink()
        with closing(sqlite3.connect(self.root/'budget/budget.sqlite')) as db:
            db.execute("UPDATE meta SET value=? WHERE key='authorization_sha256'",(self.ctx['runtime_authority']['sha256'],));db.commit()
        task,created=self.create();self.assertTrue(created)
        with self.store.reading() as db:
            proof=json.loads(db.execute('SELECT proof_json FROM artifact_stopped_history_exclusions WHERE task_id=?',(task['task_id'],)).fetchone()[0])
        self.assertEqual(proof['authority_binding'],'DIRECT_CONTEXT')

    def test_wrong_ledger_authority_and_untrusted_approval_are_rejected(self):
        with closing(sqlite3.connect(self.root/'budget/budget.sqlite')) as db:
            db.execute("UPDATE meta SET value='wrong'");db.commit()
        with self.assertRaisesRegex(ValueError,'another execution is active'):self.create()
        with closing(sqlite3.connect(self.root/'budget/budget.sqlite')) as db:
            db.execute('UPDATE meta SET value=?',(self.ctx['runtime_authority']['sha256'],));db.commit()
        approval=dict(self.approval,channel='PRODUCT_SELF_AUTHORIZED');self.write(self.approval_path,approval)
        with self.assertRaisesRegex(ValueError,'another execution is active'):self.create()

    def test_rebound_untrusted_authority_schema_channel_or_project_is_rejected(self):
        original_ctx=deepcopy(self.ctx);original_binding=deepcopy(self.binding)
        for change in (dict(schema='invented'),dict(approval_channel='PRODUCT'),dict(approval_quote=''),dict(project_id='foreign')):
            with self.subTest(change=change):
                authority=dict(self.authority,**change);ctx=deepcopy(original_ctx);binding=deepcopy(original_binding)
                ctx['runtime_authority']['sha256']=self.write(self.authority_path,authority)
                binding['authorization_sha256']=ctx['runtime_authority']['sha256']
                ctx['runtime_authority']['binding_sha256']=self.write(self.binding_path,binding)
                self.write(self.folder/'task-context.json',ctx)
                with closing(sqlite3.connect(self.root/'budget/budget.sqlite')) as db:
                    db.execute('UPDATE meta SET value=?',(ctx['runtime_authority']['sha256'],));db.commit()
                with self.assertRaisesRegex(ValueError,'another execution is active'):self.create()
    def test_nonexpired_authority_even_when_hashes_rebound_still_blocks(self):
        changed=dict(self.authority,hard_deadline_utc=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat())
        self.ctx['runtime_authority']['sha256']=self.write(self.authority_path,changed)
        self.binding['authorization_sha256']=self.ctx['runtime_authority']['sha256']
        self.ctx['runtime_authority']['binding_sha256']=self.write(self.binding_path,self.binding)
        self.write(self.folder/'task-context.json',self.ctx)
        with self.assertRaisesRegex(ValueError,'another execution is active'):self.create()

    def test_same_session_or_context_does_not_become_an_independent_new_run(self):
        with self.store.reading() as db:
            old=dict(db.execute('SELECT * FROM tasks WHERE task_id=?',(self.old_task['task_id'],)).fetchone())
            new=dict(db.execute('SELECT * FROM proposals WHERE proposal_id=?',(self.new_proposal['proposal_id'],)).fetchone())
            with self.assertRaisesRegex(ValueError,'same session'):active.stopped_history_evidence(self.store,db,old,dict(new,session_id=self.old_session))
            display=json.loads(new['display_json']);display['execution_context_id']='old_context'
            with self.assertRaisesRegex(ValueError,'not independent'):active.stopped_history_evidence(self.store,db,old,dict(new,display_json=json.dumps(display)))

    def test_same_request_is_idempotent_and_different_proposal_content_refused(self):
        first,created=self.create();again,recreated=self.create()
        self.assertTrue(created);self.assertFalse(recreated);self.assertEqual(first['task_id'],again['task_id'])
        another=self.proposal('different_proposal',self.new_session,'third_context','third_context_hash')
        with self.assertRaisesRegex(ValueError,'different content'):
            self.store.create_task(another['proposal_id'],self.new_session,'new_execution')

    def test_second_real_active_task_blocks_even_with_one_proven_historical_exit(self):
        self.create()
        second=self.proposal('second_proposal',self.new_session,'third_context','third_context_hash')
        with self.assertRaisesRegex(ValueError,'another execution is active'):
            self.store.create_task(second['proposal_id'],self.new_session,'another_execution')

    def test_two_concurrent_creates_admit_only_one_new_active_task(self):
        other_session='other_session';self.store.open_project(other_session,self.project['project_id'])
        other=self.proposal('other_proposal',other_session,'other_context','other_context_hash')
        barrier=threading.Barrier(2);results=[]
        def create(proposal,session,request):
            barrier.wait()
            try:results.append(('created',self.store.create_task(proposal,session,request)[0]['task_id']))
            except ValueError as exc:results.append(('blocked',str(exc)))
        threads=[threading.Thread(target=create,args=(self.new_proposal['proposal_id'],self.new_session,'parallel_a')),
                 threading.Thread(target=create,args=(other['proposal_id'],other_session,'parallel_b'))]
        for thread in threads:thread.start()
        for thread in threads:thread.join(timeout=5);self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(x[0] for x in results),['blocked','created']);self.assertEqual(self.task_count(),2)

    def test_confirmation_revision_selection_and_frozen_content_guards_are_preserved(self):
        mutations=[("UPDATE proposals SET state='awaiting_confirmation' WHERE proposal_id=?",(self.new_proposal['proposal_id'],)),
            ("UPDATE sessions SET current_project_id=NULL WHERE session_id=?",(self.new_session,)),
            ("UPDATE projects SET revision=revision+1 WHERE project_id=?",(self.project['project_id'],)),
            ("UPDATE proposals SET confirmation_json='{}' WHERE proposal_id=?",(self.new_proposal['proposal_id'],)),
            ("UPDATE proposals SET spec_json='{}' WHERE proposal_id=?",(self.new_proposal['proposal_id'],))]
        for sql,args in mutations:
            with self.subTest(sql=sql):
                # Keep each intentional corruption within a rolled-back fixture
                # transaction; create_task on a second connection must not run
                # against a locked writer, so snapshot/restore affected tables.
                with self.store.reading() as db:
                    tables={t:[dict(r) for r in db.execute('SELECT * FROM '+t)] for t in ('proposals','sessions','projects')}
                with self.store.transaction() as db:db.execute(sql,args)
                try:
                    with self.assertRaises(ValueError):self.create()
                    self.assertEqual(self.task_count(),1)
                finally:
                    with self.store.transaction() as db:
                        for table,rows in tables.items():
                            for row in rows:
                                key='proposal_id' if table=='proposals' else 'session_id' if table=='sessions' else 'project_id'
                                columns=[name for name in row if name!=key]
                                db.execute('UPDATE '+table+' SET '+','.join(name+'=?' for name in columns)+' WHERE '+key+'=?',tuple(row[name] for name in columns)+(row[key],))


if __name__=='__main__':unittest.main()
