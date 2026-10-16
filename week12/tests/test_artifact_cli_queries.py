"""TEST_ONLY response injection; actual CLI/MCP verification is separate."""
import unittest,json,io,tempfile
from copy import deepcopy
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from week12.src.car_domain.artifact_cli import ArtifactCLI
from week12.src.car_domain.artifact_runtime import READ_TOOLS
from week12.src.car_domain.artifact_service import ArtifactService
from week12.tests.test_model_artifact import fixture,context,SOURCE_TEXT

OUT=Path(__file__).resolve().parents[1]/'results/s4-artifact-cli-query-20261011-01'
TASK='task_fixture';VERSION='v000001'
SNAP=dict(task_id=TASK,version_id=VERSION,state='succeeded',published=False,evidence_status='COMPLETE',tool_status='PASS',chain_status='PASS',task_status='FAIL',vehicle_task_status='UNKNOWN',outcome=dict(observations=[dict(output='y',value=.86,status='FAIL',criterion=dict(kind='final_ge',threshold=.9))]))
PROJECT=dict(project_id='prj_fixture',name='TEST_ONLY_fixture',current_version_id=None,selected_version_id=None)

def tools(readonly=True):
    schema={
        'project_status':{},'versions_list':{},'execution_status':{'task_id':{'type':'string'}},
        'evidence_query':{'question':{'type':'string'},'version_id':{'type':['string','null']}},
        'versions_compare':{'before_version_id':{'type':'string'},'after_version_id':{'type':'string'}},
        'proposal_prepare':{'spec':{'type':'object'}},'proposal_get':{'proposal_id':{'type':'string'}}}
    names=set(READ_TOOLS) if readonly else set(schema)
    return {n:SimpleNamespace(name=n,description='TEST_ONLY schema',inputSchema=dict(type='object',properties=schema[n],required=[k for k in schema[n] if k!='version_id'])) for n in names}

class FakeSession:
    def __init__(self,service=None):self.calls=[];self.service=service;self.offered=tools(service is None)
    async def initialize(self):return None
    async def list_tools(self):return SimpleNamespace(tools=list(self.offered.values()))
    async def call_tool(self,name,arguments):
        self.calls.append((name,arguments))
        if self.service:result=getattr(self.service,name)(**arguments)
        elif name=='project_status':result=dict(ok=True,status=dict(project=PROJECT,snapshot_count=1,latest_task=SNAP))
        elif name=='versions_list':result=dict(ok=True,versions=dict(items=[SNAP]))
        elif name=='execution_status':result=dict(ok=True,task=dict(task_id=TASK,state='succeeded'),snapshot=SNAP)
        elif name=='evidence_query':result=dict(ok=True,evidence=dict(snapshot=SNAP))
        elif name=='versions_compare':result=dict(ok=True,comparison=dict(before=SNAP,after=SNAP,differences={'settings':{'same':True}}))
        else:raise AssertionError('unexpected tool '+name)
        return SimpleNamespace(structuredContent=result,isError=False,content=[])

class ReadStore:
    def current_project(self,session):return PROJECT
    def transaction(self):raise AssertionError('readonly dialogue/source write')
    def record_source(self,*args):raise AssertionError('readonly trusted-source write')

class TestOnlyResponseRuntime:
    def __init__(self,name='evidence_query',arguments=None,generate=False):self.calls=[];self.name=name;self.arguments=arguments or dict(question='task:'+TASK);self.generate=generate
    def complete(self,messages,tools,request_id):
        self.calls.append(dict(messages=messages,tools=tools,request_id=request_id))
        arguments=self.arguments;name=self.name
        if self.generate:
            source=json.loads(messages[1]['content'])['trusted_sources'][-1];name='proposal_prepare';arguments=dict(spec=fixture(source['id'],source['text']))
        return dict(content='TEST_ONLY injected tool selection; no online request',tool_calls=[dict(id='test_only',function=dict(name=name,arguments=json.dumps(arguments)))])

class CLIQueryChecks(unittest.IsolatedAsyncioTestCase):
    def test_evidence_manifest_location_and_digest_display(self):
        snap=deepcopy(SNAP);snap['outcome'].update(outcome_manifest_path='TEST_ONLY/artifact-manifest.json',outcome_manifest_sha256='a'*64)
        printed=io.StringIO()
        with redirect_stdout(printed):ArtifactCLI._print_result(dict(ok=True,evidence=dict(snapshot=snap)))
        self.assertIn('TEST_ONLY/artifact-manifest.json',printed.getvalue());self.assertIn('a'*64,printed.getvalue())
    def test_comparison_actual_values_and_source_digest_display(self):
        left=deepcopy(SNAP);right=deepcopy(SNAP);left['source_sha256']='source_before';right['source_sha256']='source_after'
        left['frozen_spec']=dict(requirements=['TEST_ONLY before'],settings={'stop_time':1},checks=[])
        right['frozen_spec']=dict(requirements=['TEST_ONLY after'],settings={'stop_time':2},checks=[])
        compared=dict(before=left,after=right,differences=dict(settings=dict(same=False,before={'stop_time':1},after={'stop_time':2}),source=dict(same=False,before=dict(model_name='Before',modelica_source='SECRET_SOURCE'),after=dict(model_name='After',modelica_source='SECRET_SOURCE'))))
        printed=io.StringIO()
        with redirect_stdout(printed):ArtifactCLI._print_result(dict(ok=True,comparison=compared))
        for expected in ('"stop_time": 1','"stop_time": 2','source_before','source_after','TEST_ONLY before','TEST_ONLY after'):self.assertIn(expected,printed.getvalue())
        self.assertNotIn('SECRET_SOURCE',printed.getvalue())
    async def reader(self,runtime=None):
        session=FakeSession();cli=ArtifactCLI(session,ReadStore(),'test',runtime,read_only=True);await cli.initialize();return cli,session
    async def test_no_writes_no_runtime_no_cancel(self):
        cli,session=await self.reader();self.assertEqual(set(cli._tools),set(READ_TOOLS))
        cli.last_task_id=TASK;await cli._cancel_latest_on_interrupt();self.assertEqual(session.calls,[])
        with self.assertRaisesRegex(ValueError,'未发请求'):await cli._natural_language('查询结果')
    async def test_injected_nl_query_context_and_semantics(self):
        runtime=TestOnlyResponseRuntime();cli,session=await self.reader(runtime)
        printed=io.StringIO()
        with redirect_stdout(printed):await cli._natural_language('给我上次的结果')
        self.assertEqual(len(runtime.calls),1);request=runtime.calls[0]
        self.assertEqual({t['function']['name'] for t in request['tools']},set(READ_TOOLS))
        local=json.loads(request['messages'][1]['content']);self.assertIsNone(local['project']['current_version_id'])
        self.assertEqual(local['execution_snapshots'][0]['task_id'],TASK)
        self.assertEqual(session.calls[-1],('evidence_query',dict(question='task:'+TASK)))
        self.assertIn('观测任务 FAIL',printed.getvalue());self.assertIn('车辆未验收',printed.getvalue());self.assertIsNone(cli._owned_task_id)
    async def test_injected_write_refused(self):
        runtime=TestOnlyResponseRuntime('execution_start',dict(proposal_id='prop_x',request_id='x'));cli,session=await self.reader(runtime)
        with self.assertRaisesRegex(ValueError,'未开放工具'):await cli._natural_language('运行')
        self.assertNotIn('execution_start',[n for n,a in session.calls])
    async def test_injected_bad_parameters_refused(self):
        runtime=TestOnlyResponseRuntime('execution_status',dict(task_id=123));cli,session=await self.reader(runtime)
        with self.assertRaisesRegex(ValueError,'参数不匹配'):await cli._natural_language('查询')
        self.assertNotIn('execution_status',[n for n,a in session.calls])
    async def test_explicit_commands_and_write_refusal(self):
        cli,session=await self.reader()
        commands=['/status','/task '+TASK,'/history','/compare '+VERSION+' '+VERSION,'/evidence task:'+TASK+' '+VERSION,'/query '+TASK]
        with redirect_stdout(io.StringIO()):
            for command in commands:self.assertTrue((await cli._command(command))['ok'])
        self.assertEqual(session.calls[-1],('evidence_query',dict(question='task:'+TASK)))
        self.assertEqual(session.calls[-2][1]['version_id'],VERSION)
        for command in ('/confirm prop_x','/new X','/source X','/select v000001','/cancel '+TASK,'/recover '+TASK):
            with self.assertRaisesRegex(ValueError,'只读CLI'):await cli._command(command)
        with self.assertRaisesRegex(ValueError,'查询用法'):await cli._command('/compare '+VERSION)
    async def test_writable_proposal_and_confirmation_refusal_preserved(self):
        OUT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=OUT) as tmp:
            root=Path(tmp);ctx=context(root/'context.json',allow=False);service=ArtifactService(root/'db.sqlite','test',context=ctx,worker_manager=object());service.project_create('TEST_ONLY_cli')
            session=FakeSession(service);runtime=TestOnlyResponseRuntime(generate=True);cli=ArtifactCLI(session,service.store,'test',runtime,ctx);cli._tools=session.offered
            messages,_=await cli.build_request_for_text(SOURCE_TEXT)
            design=cli._save_design('TEST_ONLY_design',json.loads(messages[1]['content']),dict(content='TEST_ONLY reviewed interface fixture',tool_calls=[]))
            cli.confirm_design_local(design['design_id'],design['displayed_digest'],'CONFIRM DESIGN '+design['design_id'])
            messages,offered=await cli.build_request_for_design(design['design_id'])
            with redirect_stdout(io.StringIO()):await cli.submit_prepared_request(messages,offered)
            proposal=service.store.proposal(cli.last_proposal_id);self.assertEqual(proposal['state'],'awaiting_confirmation')
            self.assertIn('proposal_prepare',{t['function']['name'] for t in runtime.calls[0]['tools']})
            with patch('builtins.input',return_value='NO'),redirect_stdout(io.StringIO()):refusal=await cli._command('/confirm '+cli.last_proposal_id)
            self.assertFalse(refusal['confirmed']);self.assertEqual(service.store.proposal(cli.last_proposal_id)['state'],'awaiting_confirmation')
            self.assertNotIn('execution_start',[n for n,a in session.calls]);self.assertIsNone(cli._owned_task_id)
            cli._remember(dict(task=dict(task_id=TASK,state='succeeded')))
            await cli._cancel_latest_on_interrupt();self.assertNotIn('execution_cancel',[n for n,a in session.calls])

if __name__=='__main__':unittest.main()
