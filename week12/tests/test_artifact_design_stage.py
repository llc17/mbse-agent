"""TEST_ONLY design/generation boundary checks; no HTTP, Worker or OMC."""
import io,json,tempfile,unittest,uuid
from pathlib import Path
from contextlib import redirect_stdout
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from week12.src.car_domain.artifact_cli import ArtifactCLI
from week12.src.car_domain.artifact_runtime import ArtifactRuntime,DESIGN_PROMPT
from week12.tests import test_artifact_cli_queries as cli_fixtures
from week12.tests import test_model_artifact as model_fixtures

OUT=Path(__file__).resolve().parents[1]/'results/s5-fivefloor-proposal-20261012-01/design-stage-test-only'
BODY='TEST_ONLY 方案：来源约束；候选构型/参数；上行与正向下行；简单模型/检查；假设未知。非实际车辆。'

class TextRuntime:
    def __init__(self,root,response=None):
        self.ledger_path=root/'TEST_ONLY-runtime.sqlite';self.calls=[]
        self.response=response or dict(content=BODY,tool_calls=[],response_id='TEST_ONLY-response')
    def complete(self,messages,tools,request_id):
        self.calls.append(dict(messages=messages,tools=tools,request_id=request_id))
        return deepcopy(self.response)

class DesignStageChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        OUT.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=OUT);self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.ctx=model_fixtures.context(self.root/'context.json',allow=False)
        self.service=cli_fixtures.ArtifactService(self.root/'projects.sqlite','TEST_ONLY_design',context=self.ctx,worker_manager=object())
        self.service.project_create('TEST_ONLY_design')
        self.session=cli_fixtures.FakeSession(self.service);self.runtime=TextRuntime(self.root)
        self.cli=ArtifactCLI(self.session,self.service.store,'TEST_ONLY_design',self.runtime,self.ctx)
        self.cli._tools=self.session.offered
    async def design(self):
        with redirect_stdout(io.StringIO()):result=await self.cli._natural_language('TEST_ONLY 软件流程需求，不是实际车辆参数')
        return result['design']
    async def test_design_text_saved_with_sources_and_no_dispatch(self):
        result=await self.design();request=self.runtime.calls[0]
        self.assertEqual(request['tools'],[]);self.assertEqual(request['messages'][0]['content'],DESIGN_PROMPT)
        self.assertNotIn('spec exactly',request['messages'][0]['content'])
        self.assertEqual(result['body'],BODY);self.assertFalse(result['approved']);self.assertEqual(result['physical_acceptance'],'UNKNOWN')
        self.assertEqual(result['model_response']['response_id'],'TEST_ONLY-response')
        self.assertEqual(self.cli._load_design(result['design_id']),result)
        self.assertTrue(result['trusted_sources']);self.assertEqual([n for n,_ in self.session.calls],['project_status','versions_list'])
        with self.service.store.reading() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM proposals').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0],0)
    async def test_unapproved_rejected_then_approved_generation_carries_body(self):
        design=await self.design();did=design['design_id']
        with self.assertRaisesRegex(ValueError,'not confirmed'):await self.cli.build_request_for_design(did)
        with self.assertRaisesRegex(ValueError,'specific displayed'):self.cli.confirm_design_local(did,design['displayed_digest'],'流程已确认')
        with patch('builtins.input',return_value='CONFIRM'),redirect_stdout(io.StringIO()):approval=await self.cli._command('/design-confirm')
        self.assertEqual(approval['design_id'],did)
        messages,tools=await self.cli.build_request_for_design(did)
        data=json.loads(messages[1]['content']);self.assertEqual(data['approved_design']['body'],BODY)
        self.assertEqual(data['approved_design']['displayed_digest'],design['displayed_digest'])
        self.assertEqual(data['workflow_stage'],'GENERATE_APPROVED_DESIGN');self.assertIn('proposal_prepare',[t['function']['name'] for t in tools])
        self.assertNotIn('execution_start',[n for n,_ in self.session.calls]);self.assertEqual(len(self.runtime.calls),1)
    async def test_design_tool_overreach_rejected_without_dispatch_or_retry(self):
        for name in ('proposal_prepare','execution_start','project_status'):
            self.runtime.response=dict(content=BODY,tool_calls=[dict(function=dict(name=name,arguments='{}'))])
            before=len(self.runtime.calls)
            with self.assertRaisesRegex(ValueError,'design stage forbids tool'):await self.cli._natural_language('TEST_ONLY '+name)
            self.assertEqual(len(self.runtime.calls),before+1)
        self.assertEqual(set(n for n,_ in self.session.calls),{'project_status','versions_list'})
        self.assertFalse((self.root/'artifact-designs').exists())
    async def test_changed_requirements_or_design_bytes_reject_approval(self):
        design=await self.design();did=design['design_id']
        path=self.cli._design_path(did);raw=path.read_text(encoding='utf8');changed=json.loads(raw);changed['body']='changed'
        path.write_text(json.dumps(changed),encoding='utf8')
        with self.assertRaisesRegex(ValueError,'digest changed'):self.cli.confirm_design_local(did,design['displayed_digest'],'CONFIRM DESIGN '+did)
        path.write_text(raw,encoding='utf8');self.service.store.record_source('TEST_ONLY_design','TEST_ONLY changed task')
        with self.assertRaisesRegex(ValueError,'requirements changed'):self.cli.confirm_design_local(did,design['displayed_digest'],'CONFIRM DESIGN '+did)
    def test_actual_text_payload_omits_tool_fields(self):
        runtime=object.__new__(ArtifactRuntime);runtime.config=dict(model='deepseek-flash',thinking='enabled',max_tokens=32768,timeout_s=120)
        seen=[];runtime.transport=lambda payload,key,timeout:seen.append(payload) or {}
        with patch('week12.src.car_domain.artifact_runtime._api_key',return_value='TEST_ONLY'):
            runtime._send([dict(role='user',content='TEST_ONLY')],[])
            runtime._send([dict(role='user',content='TEST_ONLY')],[dict(type='function',function=dict(name='TEST_ONLY'))])
        self.assertNotIn('tools',seen[0]);self.assertNotIn('tool_choice',seen[0])
        self.assertEqual(seen[0]['thinking'],dict(type='enabled'));self.assertEqual(seen[1]['tool_choice'],'auto')

if __name__=='__main__':unittest.main()
