"""TEST_ONLY finite startup/protocol fixtures, no HTTP/OMC/Worker launch."""
import unittest,json,tempfile,sqlite3,uuid,io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import redirect_stdout
from week12.src.car_domain.artifact_cli import ArtifactCLI
from week12.src.car_domain.artifact_runtime import ArtifactRuntime
from week12.src.car_domain.artifact_session import configure_run,run_path,prepare_run
from week12.src.car_domain.artifact_dialogue import semantic_locks,preserve_locks
from week12.src.car_domain.model_artifact import save
from week12.tests.test_artifact_cli_queries import FakeSession,ReadStore,tools

OUT=Path(__file__).resolve().parents[1]/'results/s5-agent-usability-20261012-01'
def call(name='execution_status',args=None):return dict(content='',tool_calls=[dict(function=dict(name=name,arguments=json.dumps(args or {'task_id':'task_fixture'})))])
def args(name,**kw):return SimpleNamespace(run=name,prepare=False,offline=False,read_only=False,baseline_db=None,baseline_session=None,**kw)

class StartupChecks(unittest.TestCase):
    def setUp(self):
        OUT.mkdir(parents=True,exist_ok=True);self.root=Path(tempfile.mkdtemp(prefix='TEST_ONLY_start_',dir=OUT))
        self.patch=patch('week12.src.car_domain.artifact_session.run_path',lambda name:self.root/name);self.patch.start()
    def tearDown(self):self.patch.stop()
    def test_prepare_decline_and_offline_never_activate_live(self):
        a=args('prepared');a.prepare=True;r=configure_run(a);root=self.root/a.run
        self.assertFalse(r['activated']);self.assertFalse((root/'projects.sqlite').exists());self.assertFalse((root/'runtime.sqlite').exists())
        a.prepare=False;r=configure_run(a,input_fn=lambda _: 'NO');self.assertFalse(r['activated']);self.assertFalse((root/'projects.sqlite').exists())
        a.offline=True;configure_run(a);value=json.loads((root/'context.json').read_text());self.assertFalse(value['allow_execution']);self.assertEqual(value['mode'],'TEST_ONLY');self.assertFalse((root/'runtime.sqlite').exists())
        with self.assertRaisesRegex(ValueError,'LIVE/offline'):configure_run(args('prepared'))
    def test_closed_activating_and_missing_ledger_refused(self):
        a=args('state');a.offline=True;configure_run(a);root=self.root/a.run
        manifest=json.loads((root/'run.json').read_text())
        for state in ('CLOSED','ACTIVATING'):
            save(root/'run.json',{**manifest,'status':state})
            with self.assertRaisesRegex(ValueError,'closed run'):configure_run(a)
        save(root/'run.json',{**manifest,'mode':'LIVE'});a.offline=False
        with self.assertRaisesRegex(ValueError,'账本缺失'):configure_run(a)
        self.assertFalse((root/'runtime.sqlite').exists())
    def test_same_run_retains_context_ledger_and_config(self):
        a=args('resume');configure_run(a,input_fn=lambda _:'ALLOW resume',test_only=True);root=self.root/a.run
        config=root/'runtime.json';ledger=root/'runtime.sqlite';ArtifactRuntime(config,ledger)
        value=json.loads((root/'run.json').read_text());value['mode']='LIVE';save(root/'run.json',value)
        before={p.name:p.read_bytes() for p in (config,ledger,root/'context.json')}
        configure_run(args('resume'))
        self.assertEqual(before,{p.name:p.read_bytes() for p in (config,ledger,root/'context.json')})
        with sqlite3.connect(ledger) as db:db.execute("INSERT INTO meta VALUES('frozen','TEST_ONLY')")
        with self.assertRaisesRegex(ValueError,'冻结'):configure_run(args('resume'))
        self.assertTrue(ledger.exists())

class TestOnlyProvider(ArtifactRuntime):
    def __init__(self,root,responses,max_requests=2):
        self.sent=[];self.captured=responses
        config=dict(schema_version='v10-runtime-1',model='deepseek-flash',api_base='https://api.deepseek.com',thinking='disabled',temperature=0,max_tokens=32,timeout_s=60,max_requests=max_requests,max_total_tokens=16000,max_usd=.02,max_input_tokens=13952,input_usd_per_million=.3,output_usd_per_million=1.2,max_consecutive_transport_errors=1,automatic_retries=0)
        save(root/'runtime.json',config);super().__init__(root/'runtime.json',root/'runtime.sqlite')
    def _send(self,messages,tools):
        index=len(self.sent);self.sent.append(dict(messages=messages,tools=tools));response=self.captured[index]
        for index,tool in enumerate(response.get('tool_calls',[])):tool.update(id='TEST_ONLY_'+str(index),type='function')
        return dict(model='deepseek-flash',choices=[dict(message=response,finish_reason='tool_calls' if response.get('tool_calls') else 'stop')],usage=dict(prompt_tokens=1,completion_tokens=1,total_tokens=2))

class DialogueChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        OUT.mkdir(parents=True,exist_ok=True);self.root=Path(tempfile.mkdtemp(prefix='TEST_ONLY_dialogue_',dir=OUT))
    async def cli(self,responses,max_requests=2):
        runtime=TestOnlyProvider(self.root,responses,max_requests);session=FakeSession();cli=ArtifactCLI(session,ReadStore(),'test',runtime,read_only=True);await cli.initialize()
        offered=[dict(type='function',function=dict(name=t.name,parameters=t.inputSchema)) for t in cli._tools.values()]
        return cli,session,runtime,offered
    async def test_one_packaging_fix_and_atomic_batch(self):
        bad=call(args={'arguments':{'task_id':'task_fixture'}});good=call()
        cli,session,runtime,offered=await self.cli([bad,good])
        with redirect_stdout(io.StringIO()):result=await cli.submit_prepared_request([dict(role='user',content='TEST_ONLY')],offered,'same')
        self.assertEqual(result['format_corrections'],1);self.assertEqual(len(runtime.sent),2);self.assertEqual(len(session.calls),1)
        self.assertEqual(len(list((self.root/'cli-turns').glob('*response.json'))),2)
        with redirect_stdout(io.StringIO()):replayed=await cli.submit_prepared_request([dict(role='user',content='TEST_ONLY')],offered,'same')
        self.assertTrue(replayed['delivery_replayed']);self.assertEqual(len(runtime.sent),2);self.assertEqual(len(session.calls),1)
    async def test_second_format_failure_stops_without_dispatch(self):
        bad=call(args={'arguments':{'task_id':'task_fixture'}});cli,session,runtime,offered=await self.cli([bad,bad])
        with self.assertRaisesRegex(ValueError,'one format correction'):await cli.submit_prepared_request([],offered)
        self.assertEqual(len(runtime.sent),2);self.assertEqual(session.calls,[])
    async def test_budget_exhaustion_has_no_second_provider_send(self):
        cli,session,runtime,offered=await self.cli([call(args={'arguments':{'task_id':'task_fixture'}})],1)
        with self.assertRaisesRegex(Exception,'budget exhausted'):await cli.submit_prepared_request([],offered)
        self.assertEqual(len(runtime.sent),1);self.assertEqual(session.calls,[])
    async def test_forbidden_tool_and_partial_batch_never_dispatch(self):
        response=call();response['tool_calls']+=call('execution_start',{'proposal_id':'bad'})['tool_calls']
        cli,session,runtime,offered=await self.cli([response])
        with self.assertRaisesRegex(ValueError,'未开放'):await cli.submit_prepared_request([],offered)
        self.assertEqual(len(runtime.sent),1);self.assertEqual(session.calls,[])
    async def test_damaged_json_and_changed_semantics_not_repaired(self):
        damaged=call();damaged['tool_calls'][0]['function']['arguments']='{"spec":{"artifact":{"modelica_source":"source"},"threshold":'
        cli,session,runtime,offered=await self.cli([damaged])
        with self.assertRaisesRegex(ValueError,'不可恢复'):await cli.submit_prepared_request([],offered)
        self.assertEqual(len(runtime.sent),1);self.assertEqual(session.calls,[])
        original=call(args={'arguments':{'task_id':'task_original','threshold':4}})
        with self.assertRaisesRegex(ValueError,'语义值'):preserve_locks(semantic_locks(original),[('execution_status',dict(task_id='task_original',threshold=5))])
    async def test_normal_clarification_no_retry(self):
        cli,session,runtime,offered=await self.cli([dict(content='TEST_ONLY clarification',tool_calls=[])])
        with redirect_stdout(io.StringIO()):await cli.submit_prepared_request([],offered)
        self.assertEqual(len(runtime.sent),1);self.assertEqual(session.calls,[])
    async def test_existing_task_confirm_is_query_only(self):
        cli,session,runtime,offered=await self.cli([call()]);cli.read_only=False;cli.context=SimpleNamespace(allow_execution=True)
        cli._existing_proposal_task=lambda _: 'task_fixture'
        with patch('builtins.input',side_effect=AssertionError('must not reconfirm')),redirect_stdout(io.StringIO()):result=await cli._command('/confirm prop_legacy_random_request')
        self.assertTrue(result['ok']);self.assertEqual(session.calls,[('execution_status',{'task_id':'task_fixture'})]);self.assertIsNone(cli._owned_task_id)

if __name__=='__main__':unittest.main()
