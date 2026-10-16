"""Six local recovery checks. Mocked provider; no HTTP/MCP/Worker/OMC."""
import asyncio,gc,hashlib,io,json,sqlite3,tempfile,unittest
from contextlib import closing,contextmanager,redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from week12.src.car_domain.artifact_runtime import ArtifactRuntime,RuntimeError,_digest
from week12.src.car_domain.artifact_session import configure_run,digest,ledger_state,now
from week12.src.car_domain.artifact_continuous import FLOW_PROMPT
from week12.src.car_domain.artifact_cli import ArtifactCLI
from week12.src.car_domain.model_artifact import save

ROOT=Path(__file__).resolve().parents[1]/'results/s5-my-run-timeout-20261013-01/recovery-test-only'

class LocalStore:
    def __init__(self,path):self.db_path=path
    def current_project(self,session):return dict(project_id='TEST_ONLY_project')
    @contextmanager
    def reading(self):
        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory=sqlite3.Row;yield db

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        ROOT.mkdir(parents=True,exist_ok=True);self.temp=tempfile.TemporaryDirectory(dir=ROOT);self.addCleanup(self.temp.cleanup);self.addCleanup(gc.collect);self.root=Path(self.temp.name)
        self.config=dict(schema_version='v10-runtime-1',model='deepseek-flash',api_base='https://api.deepseek.com',thinking='disabled',temperature=0,max_tokens=2048,timeout_s=60,max_requests=2,max_total_tokens=16000,max_usd=.02,max_input_tokens=13952,input_usd_per_million=.3,output_usd_per_million=1.2,max_consecutive_transport_errors=1,automatic_retries=0)
        save(self.root/'runtime.json',self.config)
        save(self.root/'context.json',dict(schema='v11-artifact-execution-context-1',context_id='TEST_ONLY_context',status='ACTIVE',mode='LIVE',allow_execution=True,max_tasks=1,worker_timeout_s=300,simulation_timeout_s=120))
        save(self.root/'run.json',dict(schema='v11-artifact-daily-run-1',name='TEST_ONLY',status='ACTIVE',mode='LIVE',session='daily',caps=dict(max_http_requests=2,max_total_tokens=16000,max_usd=.02,max_actual_tasks=1,worker_timeout_s=300,simulation_timeout_s=120),runtime_config_sha256=digest(self.root/'runtime.json'),context_sha256=digest(self.root/'context.json')))
        with closing(sqlite3.connect(self.root/'projects.sqlite')) as db:
            db.execute('CREATE TABLE proposals(proposal_id TEXT,display_json TEXT)');db.execute('CREATE TABLE tasks(proposal_id TEXT)');db.execute('CREATE TABLE sources(source_id TEXT,session_id TEXT,project_id TEXT,text TEXT)')
            db.execute("INSERT INTO sources VALUES('TEST_ONLY_source','daily','TEST_ONLY_project','TEST_ONLY original requirement')");db.commit()
        runtime=self.runtime()
        runtime.authorize_active_budget_adjustment(self.root/'run.json',approval_verbatim='TEST_ONLY original quota approval',limits=dict(max_requests=6,max_total_tokens=2000000,max_usd=.5,max_tokens=32768,thinking='enabled'),approval_scope='TEST_ONLY quota adjustment')
        self.budget_bytes=(self.root/'budget-adjustment-authorization.json').read_bytes()
        sources=[dict(id='TEST_ONLY_source',text='TEST_ONLY original requirement')]
        self.messages=[dict(role='system',content=FLOW_PROMPT),dict(role='user',content=json.dumps(dict(project=dict(project_id='TEST_ONLY_project'),trusted_sources=sources)))]
        runtime._reserve('TEST_ONLY_prior_reserved',_digest({}),42562,.0422598)
        runtime._reserve('TEST_ONLY_prior_failed',_digest(dict(messages=self.messages,tools=[])),43611,.0425745)
        runtime._transport_error('TEST_ONLY_prior_failed','TEST_ONLY ReadTimeout')
        folder=self.root/'cli-continuations';folder.mkdir()
        self.pointer_path=folder/('current-'+hashlib.sha256(b'daily').hexdigest()+'.json')
        save(self.pointer_path,dict(project_id='TEST_ONLY_project',text='TEST_ONLY original requirement',sources=sources,messages=self.messages,request_id='TEST_ONLY_prior_failed'))
        self.prior=self.rows();self.runtime_object=runtime;self.calls=[]
        self.key=patch('week12.src.car_domain.artifact_runtime._api_key',return_value='TEST_ONLY');self.key.start();self.addCleanup(self.key.stop)

    def runtime(self,transport=None):return ArtifactRuntime(self.root/'runtime.json',self.root/'runtime.sqlite',transport=transport)
    def rows(self):
        with closing(sqlite3.connect(self.root/'runtime.sqlite')) as db:return ArtifactRuntime._recovery_request_rows(db)
    def recover(self):
        return self.runtime_object.authorize_transport_recovery(self.root/'run.json',approval_verbatim='TEST_ONLY 按这个方案修改',approval_scope='TEST_ONLY preserve unknown usage; one manual new attempt; stop on failure',idle_process_evidence=dict(checked_at=now(),matching_processes=[],method='TEST_ONLY synthetic idle'))
    def state(self,key):
        with closing(sqlite3.connect(self.root/'runtime.sqlite')) as db:return db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
    def startup(self):
        args=SimpleNamespace(run='TEST_ONLY',baseline_db=None,baseline_session=None,runtime_profile=None,prepare=False,read_only=False,offline=False,advanced_workflow=False)
        with patch('week12.src.car_domain.artifact_session.run_path',return_value=self.root),redirect_stdout(io.StringIO()):configure_run(args)
        return args
    def provider(self,content):
        def send(payload,key,timeout):
            self.calls.append(payload)
            if isinstance(content,Exception):raise content
            return dict(id='TEST_ONLY_response',model='DeepSeek-V4.1-Flash',choices=[dict(finish_reason='stop',message=dict(content=content))],usage=dict(prompt_tokens=4,completion_tokens=8,total_tokens=12))
        return send
    def cli(self,runtime):
        cli=ArtifactCLI.__new__(ArtifactCLI)
        cli.runtime=runtime;cli.store=LocalStore(self.root/'projects.sqlite');cli.session_id='daily';cli.continuous=True;cli.read_only=False;cli.responses=[];cli.tool_results=[];cli._tools={};cli.last_design_id=None
        cli.context=SimpleNamespace(context_id='TEST_ONLY_context',digest=digest(self.root/'context.json'),mode='LIVE',max_tasks=1,worker_timeout_s=300,simulation_timeout_s=120,assert_current=lambda:None)
        cli._journal=lambda *args:None;cli._save_dialogue=lambda *args:None
        return cli

    def test_old_rows_budget_receipt_and_caps_preserved_startup(self):
        record=self.recover();runtime=self.runtime()
        self.assertEqual(self.rows(),self.prior);self.assertEqual((self.root/'budget-adjustment-authorization.json').read_bytes(),self.budget_bytes)
        self.assertEqual(record['original_totals'],dict(requests=2,tokens=86173,cost=.0848343));self.assertEqual(self.state('frozen'),('transport_errors',));self.assertEqual(self.state('transport_recovery_state'),('pending',))
        for key in ('max_requests','max_total_tokens','max_usd','max_tokens','timeout_s','max_input_tokens','automatic_retries'):self.assertEqual(runtime.config[key],record['old_config'][key])
        self.assertFalse(self.startup().read_only)
    def test_receipt_hash_chain_drift_rejected(self):
        self.recover();path=self.root/'transport-recovery-authorization.json';record=json.loads(path.read_text(encoding='utf8'));record['old_config_hash']='TEST_ONLY tamper';save(path,record)
        with self.assertRaisesRegex(RuntimeError,'chain differs'):self.runtime()
        self.assertEqual(self.rows(),self.prior)
    def test_only_single_new_id_old_ids_still_refused(self):
        record=self.recover();runtime=self.runtime()
        with self.assertRaisesRegex(RuntimeError,'single explicitly'):runtime._reserve('TEST_ONLY_other',record['request_content_hash'],1,.000001)
        for row in self.prior:
            with self.assertRaises(RuntimeError):runtime.complete(self.messages,[],row['request_id'])
        runtime._reserve(record['new_request_id'],record['request_content_hash'],43611,.0425745)
        with self.assertRaisesRegex(RuntimeError,'no further retry'):runtime._reserve('TEST_ONLY_fourth',record['request_content_hash'],1,.000001)
        self.assertEqual(self.rows()[:2],self.prior)
    def test_second_transport_failure_stops_without_resend(self):
        record=self.recover();runtime=self.runtime(self.provider(TimeoutError('TEST_ONLY timeout')))
        with self.assertRaisesRegex(RuntimeError,'transport failed'):runtime.complete(self.messages,[],record['new_request_id'])
        self.assertEqual(self.rows()[-1]['status'],'failed');self.assertEqual(self.state('frozen'),('transport_errors',))
        with self.assertRaisesRegex(ValueError,'账本冻结'):self.startup()
        with self.assertRaises(RuntimeError):runtime.complete(self.messages,[],'TEST_ONLY_fourth')
        with self.assertRaises(RuntimeError):runtime.authorize_transport_recovery(self.root/'run.json',approval_verbatim='TEST_ONLY again',approval_scope='TEST_ONLY',idle_process_evidence=dict(checked_at=now(),matching_processes=[]))
        self.assertEqual(len(self.calls),1);self.assertEqual(self.rows()[:2],self.prior)
    def test_retry_valid_design_then_normal_continuation_no_duplicate_source(self):
        record=self.recover();runtime=self.runtime(self.provider(json.dumps(dict(schema='v11-continuous-design-1',state='CLARIFY',body='TEST_ONLY valid clarification',commitment=None))))
        with redirect_stdout(io.StringIO()):asyncio.run(self.cli(runtime).handle_line('/retry'))
        self.assertEqual(self.state('transport_recovery_state'),('completed',));self.assertIsNone(self.state('frozen'))
        self.assertFalse(self.startup().read_only)
        runtime._reserve('TEST_ONLY_normal_continuation',_digest({}),100,.001)
        self.assertEqual(self.rows()[:2],self.prior)
        with closing(sqlite3.connect(self.root/'projects.sqlite')) as db:self.assertEqual(db.execute('SELECT count(*) FROM sources').fetchone()[0],1)
        self.assertEqual(len(self.calls),1)
    def test_valid_http_invalid_design_stops_even_same_text(self):
        self.recover();runtime=self.runtime(self.provider('TEST_ONLY invalid design'))
        with self.assertRaisesRegex(ValueError,'结构化方案'):asyncio.run(self.cli(runtime).handle_line('TEST_ONLY original requirement'))
        self.assertEqual(self.rows()[-1]['status'],'succeeded');self.assertEqual(self.state('transport_recovery_state'),('submitted',));self.assertEqual(self.state('frozen'),('transport_errors',))
        with self.assertRaisesRegex(RuntimeError,'no further retry'):runtime.complete(self.messages,[],'TEST_ONLY_fourth')
        with self.assertRaisesRegex(ValueError,'账本冻结'):self.startup()
        with closing(sqlite3.connect(self.root/'projects.sqlite')) as db:self.assertEqual(db.execute('SELECT count(*) FROM sources').fetchone()[0],1)
        self.assertEqual(len(self.calls),1)

if __name__=='__main__':unittest.main()
