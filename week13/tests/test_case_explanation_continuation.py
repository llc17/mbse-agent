"""Narrow offline checks for existing failed-result review and two-step policy."""
import copy,io,json,sqlite3,unittest
from contextlib import contextmanager,redirect_stdout
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import Mock,patch
from week13.src import case_requests as cr,case_taskflow as tf,case_cli,case_budget as cb
from week13.src.case_runtime import CaseRuntime

ORIGIN='console_20261025T132242_d15ca20bf6'
PACKAGE=Path(__file__).resolve().parents[1]/'results/explanation-continuation-20261025-01'

class ExplanationContinuation(unittest.TestCase):
    def authority(self,steps,cap=None):
        caps=dict(http_requests=cap or len(steps),product_cny=round(5.24288*(cap or len(steps)),8),total_tokens=1000000*(cap or len(steps)),total_output_tokens=393216*(cap or len(steps)),compile=0,simulation=0,candidates=0,software_repair_rounds=0,parameter_revision_rounds=0,observation_extensions=0)
        controls=json.loads((cr.ROOT/'configs/case_runtime.json').read_text(encoding='utf8'))
        controls.update(max_requests=caps['http_requests'],max_cny=caps['product_cny'],max_total_tokens=caps['total_tokens'],max_total_output_tokens=caps['total_output_tokens'])
        now=datetime.now(timezone.utc)
        return dict(started_at_utc=now.isoformat(),hard_deadline_utc=(now+timedelta(minutes=30)).isoformat(),caps=caps,model_controls=controls,permissions=dict(planning_http=True,execution=False),operation_policy=dict(mode='READONLY_VALIDATION',steps={s:'TEST_ONLY_'+s for s in steps},origin_reference={}))

    @contextmanager
    def ledger(self,steps,cap=None):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        db.executescript('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE requests(request_id TEXT PRIMARY KEY,content_hash TEXT,category TEXT,status TEXT,reserve_tokens INTEGER,reserve_output INTEGER,reserve_cost REAL,actual_tokens INTEGER,actual_output INTEGER,actual_cost REAL,response TEXT,reason TEXT,reserved_at TEXT,launched_at TEXT,finished_at TEXT); CREATE TABLE connect_retries(new_request_id TEXT,content_hash TEXT,launched_at TEXT); CREATE TABLE counters(kind TEXT,event_id TEXT); CREATE TABLE readonly_attempts(request_id TEXT PRIMARY KEY,context_id TEXT,step_id TEXT);')
        @contextmanager
        def connection(_):
            with db:yield db
        b=cb.CaseBudget.__new__(cb.CaseBudget);b.path=Path('TEST_ONLY_IN_MEMORY');b.authority=self.authority(steps,cap);b.task_context=dict(request_id='TEST_ONLY_explain',step_id='explain');b._require_time=lambda:None
        with patch.object(cb,'connection',connection),patch.object(cb,'require_planning',return_value={}):
            yield b,db
        db.close()

    def test_complete_terminal_failure_review_and_inflight_refusal(self):
        origin=dict(db_path='unused',session_id='session',project_id='project',request_digest='digest',root='unused',project_root='unused')
        snapshot=dict(record_kind='ARTIFACT_EXECUTION_SNAPSHOT',evidence_status='COMPLETE',state='failed',recorded_conclusions_verified=True,project_id='project',task_id='task',task_status='UNKNOWN',chain_status='BLOCKED_INVALID_MODEL',frozen_spec=dict(artifact=dict(modelica_source='model F parameter Real m=90; end F;')))
        svc=Mock();svc.store.task.return_value=dict(session_id='session')
        for state,chain in [('failed','BLOCKED_INVALID_MODEL'),('failed','PASS'),('succeeded','PASS')]:
            current=dict(snapshot,state=state,chain_status=chain);svc._artifact_version.return_value=current
            with patch.object(cr,'load_request',return_value=origin),patch.object(tf,'service',return_value=svc),patch.object(tf,'module') as module,patch.object(cr,'create_request',return_value={'ok':True}) as create:
                module.return_value.reference.return_value=dict(project_id='project',spec_digest='x')
                self.assertEqual(tf.bootstrap_review('TEST_ONLY','origin','v000002','review','unused','explain'),{'ok':True});self.assertEqual(create.call_args.kwargs['baseline_snapshot']['chain_status'],chain)
        for changed in [dict(state='running'),dict(state='uncertain'),dict(evidence_status='STAGED'),dict(record_kind='IMPORTED_REFERENCE'),dict(recorded_conclusions_verified=False)]:
            svc._artifact_version.return_value=dict(snapshot,**changed)
            with patch.object(cr,'load_request',return_value=origin),patch.object(tf,'service',return_value=svc),patch.object(tf,'module') as module,patch.object(cr,'create_request') as create:
                module.return_value.reference.return_value={}
                with self.assertRaises(ValueError):tf.bootstrap_review('TEST_ONLY','origin','v2','review','unused','explain')
                create.assert_not_called()
        svc._artifact_version.return_value=snapshot;svc.store.task.return_value=dict(session_id='different_origin')
        with patch.object(cr,'load_request',return_value=origin),patch.object(tf,'service',return_value=svc),patch.object(tf,'module') as module:
            module.return_value.reference.return_value={}
            with self.assertRaisesRegex(ValueError,'original request'):tf.bootstrap_review('TEST_ONLY','origin','v2','review','unused','explain')

    def test_actual_v2_public_review_create_without_any_write(self):
        captured={}
        def no_write(request_id,project_id,version,question,**kwargs):
            captured.update(kwargs)
            return dict(request_id=request_id,access_mode='RESULT_REVIEW_ONLY',step_id=kwargs['step_id'],review_binding=kwargs['review_binding'])
        output=io.StringIO()
        with patch.object(cr,'create_request',side_effect=no_write),redirect_stdout(output):
            code=case_cli.main(['review-create','--request-id','TEST_ONLY_no_write_review','--origin-request-id',ORIGIN,'--baseline-version','v000002','--requirement','TEST_ONLY readonly evidence review','--context-root',str(PACKAGE/'TEST_ONLY_no_write_review'),'--step-id','explain'])
        self.assertEqual(code,0,output.getvalue());snapshot=captured['baseline_snapshot']
        self.assertEqual(snapshot['task_id'],'task_0784771026604009b022b71b5fa8c805')
        self.assertEqual(snapshot['source_sha256'],'e063e4ab261595cb3562d606eda13256e20c83dba4fd36d2748ba2275fec304d')
        self.assertEqual(snapshot['chain_status'],'BLOCKED_INVALID_MODEL');self.assertEqual(snapshot['state'],'failed')
        self.assertFalse((PACKAGE/'TEST_ONLY_no_write_review').exists())

    def test_two_steps_cap_two_preserves_second_slot(self):
        with self.ledger(('explain','reopen')) as (b,db):
            cr._validate_authority(b.authority)
            b.reserve_http('first','payload','explanation',b.authority['model_controls']);b.launch_http('first',fixture=True);b.finish_http('first',response={},tokens=50,output=20,cost=.001)
            b.task_context=dict(request_id='TEST_ONLY_reopen',step_id='reopen')
            b.reserve_http('second','payload2','reopen',b.authority['model_controls'])
            self.assertEqual(db.execute('SELECT count(*) FROM requests').fetchone()[0],2)
        with self.ledger(('explain','reopen'),1) as (b,db):
            with self.assertRaisesRegex(RuntimeError,'protects required'):b.reserve_http('blocked','payload','explanation',b.authority['model_controls'])
            self.assertEqual(db.execute('SELECT count(*) FROM requests').fetchone()[0],0)

    def test_original_four_step_reservations_unchanged(self):
        steps=('explain','reopen','negative_mass','negative_geometry')
        for cap in (4,3):
            with self.ledger(steps,cap) as (b,db):
                cr._validate_authority(b.authority)
                if cap==4:b.reserve_http('first','payload','explanation',b.authority['model_controls'])
                else:
                    with self.assertRaisesRegex(RuntimeError,'protects required'):b.reserve_http('first','payload','explanation',b.authority['model_controls'])

    def test_invalid_steps_writing_and_tools_remain_forbidden(self):
        for steps in [('explain',),('explain','generate'),('explain','reopen','negative_mass')]:
            with self.assertRaises(PermissionError):cr._validate_authority(self.authority(steps))
        for change in ('execution','compile'):
            authority=self.authority(('explain','reopen'))
            if change=='execution':authority['permissions']['execution']=True
            else:authority['caps']['compile']=1
            with self.assertRaises(PermissionError):cr._validate_authority(authority)
        authority=self.authority(('explain','reopen'));runtime=CaseRuntime.__new__(CaseRuntime);runtime.authorization=authority;runtime.config=authority['model_controls'];runtime.task_context=dict(access_mode='RESULT_REVIEW_ONLY',step_id='explain')
        tool=dict(type='function',function=dict(name='proposal_prepare'))
        with self.assertRaises(PermissionError):runtime.build_payload([dict(role='user',content='review')],[tool])
        payload,_=runtime.build_payload([dict(role='user',content='review')],[])
        self.assertNotIn('tools',payload);self.assertNotIn('tool_choice',payload)

    def test_unknown_stops_further_package_calls_and_preserves_reserve(self):
        with self.ledger(('explain','reopen')) as (b,db):
            b.reserve_http('first','payload','explanation',b.authority['model_controls']);b.launch_http('first',fixture=True)
            b._local_exit_proven=lambda *_:False
            b.finish_http('first',error='TEST_ONLY unknown',uncertain=True)
            row=dict(db.execute('SELECT * FROM requests').fetchone())
            self.assertEqual((row['actual_tokens'],row['actual_output'],row['actual_cost']),(None,None,None))
            self.assertEqual((row['reserve_tokens'],row['reserve_output'],row['reserve_cost']),(1000000,393216,5.24288))
            b.task_context=dict(request_id='TEST_ONLY_reopen',step_id='reopen')
            with self.assertRaisesRegex(RuntimeError,'frozen'):b.reserve_http('second','payload2','reopen',b.authority['model_controls'])

class ReviewOriginSourceBinding(unittest.TestCase):
    """Real persisted request/snapshot bindings; no model or execution calls."""
    @classmethod
    def setUpClass(cls):
        cls.ctx=cr.load_request('explanation_20261025_01_explain')
        cls.binding=json.loads((PACKAGE/'explain-stdout.json').read_text(encoding='utf-8-sig'))['binding']

    def source_references(self,ctx=None,binding=None):
        return tf.TaskFlow(copy.deepcopy(self.ctx if ctx is None else ctx))._explanation_source_references(copy.deepcopy(self.binding if binding is None else binding))

    def protect(self,text):
        return tf.protect_explanation(text,self.binding,source_references=self.source_references())['explanation_validation']

    def test_real_origin_v0_is_source_only_and_not_run_is_displayed(self):
        refs=self.source_references()
        self.assertEqual({x['version_id'] for x in refs},{'v000000','v000002'})
        self.assertTrue(all(x['execution_evidence_present'] is False for x in refs))
        report=self.protect('v000000 是85kg基线来源，task_status=NOT_RUN。')
        self.assertTrue(report['body_displayed'])
        self.assertNotIn('v000000',report['known_execution_versions'])

    def test_source_task_pass_and_unknown_version_still_rejected(self):
        for text,code in [('v000000 task_status=PASS。','SOURCE_REFERENCE_HAS_NO_BOUND_EXECUTION'),('v999999 task_status=NOT_RUN。','UNBOUND_VERSION_REFERENCE'),('v000002 task_status=PASS。','STRUCTURED_STATUS_CONTRADICTION')]:
            with self.subTest(text=text):
                report=self.protect(text)
                self.assertFalse(report['body_displayed'])
                self.assertIn(code,{x['code'] for x in report['issues']})

    def test_origin_digest_session_project_and_store_drift_rejected(self):
        for field,value in [('origin_request_digest','0'*64),('origin_session_id','FOREIGN_SESSION'),('origin_request_id','nonexistent_review_origin')]:
            with self.subTest(field=field):
                ctx=copy.deepcopy(self.ctx);ctx['review_binding'][field]=value
                with self.assertRaises((ValueError,OSError)):self.source_references(ctx)
        for field,value in [('project_id','FOREIGN_PROJECT'),('db_path',str(PACKAGE/'FOREIGN_STORE.sqlite')),('project_root',str(PACKAGE/'FOREIGN_PROJECT'))]:
            with self.subTest(field=field):
                ctx=copy.deepcopy(self.ctx);binding=copy.deepcopy(self.binding);ctx[field]=value
                if field=='project_id':
                    # Keep the review side internally consistent; the real
                    # loaded origin must still fail the cross-project check.
                    ctx['baseline_ref'][field]=value;ctx['baseline_snapshot'][field]=value
                    ctx['baseline_snapshot_sha256']=cr.digest(ctx['baseline_snapshot'])
                    ctx['review_binding']['reference'][field]=value
                    binding[field]=value;binding['reviewed_execution'][field]=value
                with self.assertRaises(ValueError):self.source_references(ctx,binding)

    def test_review_reference_snapshot_and_missing_binding_rejected(self):
        for change in ('reference','snapshot_hash','source_hash','missing_origin'):
            with self.subTest(change=change):
                ctx=copy.deepcopy(self.ctx)
                if change=='reference':ctx['review_binding']['reference']['manifest_sha256']='0'*64
                elif change=='snapshot_hash':ctx['baseline_snapshot_sha256']='0'*64
                elif change=='source_hash':ctx['baseline_snapshot']['frozen_spec']['artifact']['modelica_source']+='\n'
                else:ctx['review_binding'].pop('origin_request_digest')
                with self.assertRaises((ValueError,KeyError)):self.source_references(ctx)
        binding=copy.deepcopy(self.binding);binding['reviewed_execution']['source_sha256']='0'*64
        with self.assertRaises(ValueError):self.source_references(binding=binding)

    def test_ordinary_request_source_projection_is_unchanged(self):
        origin=cr.load_request(ORIGIN)
        self.assertEqual(tf.TaskFlow(origin)._explanation_source_references({}),tf.baseline_source_references(origin))

if __name__=='__main__':unittest.main()
