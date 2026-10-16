"""Offline task routing through actual CLI/Store/Service; no HTTP/Worker/OMC."""
import io,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from contextlib import redirect_stdout
from copy import deepcopy
from unittest.mock import patch,AsyncMock
from week12.src.car_domain.artifact_cli import ArtifactCLI
from week12.src.car_domain.artifact_continuous import FLOW_PROMPT
from week12.src.car_domain.artifact_intent import task_intent
from week12.src.car_domain.artifact_runtime import READ_TOOLS
from week12.src.car_domain.artifact_service import ArtifactService
from week12.tests.test_artifact_cli_queries import FakeSession,ReadStore,TestOnlyResponseRuntime
from week12.tests.test_artifact_continuous_cli import commitment,spec,TEXT
from week12.tests.test_model_artifact import context

OUT=Path(__file__).resolve().parents[1]/'results/s5-task-dialogue-20261014-01'

class Provider:
    def __init__(self,root):
        self.calls=[];self.config=dict(max_requests=20,max_total_tokens=100000,max_usd=.1)
        self.ledger_path=root/'runtime.sqlite';self.state='READY';self.malicious=False
    def complete(self,messages,tools,rid):
        self.calls.append(dict(messages=deepcopy(messages),tools=deepcopy(tools),rid=rid))
        local=json.loads(messages[1]['content'])
        if messages[0]['content'].startswith(FLOW_PROMPT):
            source=local['trusted_sources'][0]
            comm=commitment(source) if self.state!='CLARIFY' else None
            if self.state=='SOURCE_REVIEW':comm['literal_bindings'][0]['value']=9
            return dict(content=json.dumps(dict(schema='v11-continuous-design-1',state='CLARIFY' if self.state=='CLARIFY' else 'READY',body='TEST_ONLY 当前方案正文；请补充任务事实。',commitment=comm),ensure_ascii=False),tool_calls=[])
        if local.get('approved_design'):
            return dict(content='TEST_ONLY approved generation',tool_calls=[dict(id='generate',function=dict(name='proposal_prepare',arguments=json.dumps(dict(spec=spec(local['approved_design']['trusted_sources'][0])))))])
        calls=[]
        if self.malicious:
            calls=[dict(id='read',function=dict(name='project_status',arguments='{}')),dict(id='write',function=dict(name='proposal_prepare',arguments='{}'))]
        return dict(content='TEST_ONLY 根据当前方案解释；物理未验证。',tool_calls=calls)

class Checks(unittest.IsolatedAsyncioTestCase):
    def setup(self,*,allow=False,project=True,continuous=True):
        OUT.mkdir(exist_ok=True);root=Path(tempfile.mkdtemp(prefix='TEST_ONLY_',dir=OUT))
        ctx=context(root/'context.json',allow=allow)
        service=ArtifactService(root/'projects.sqlite','test',context=ctx,worker_manager=object())
        if project:service.project_create('TEST_ONLY_dialogue')
        session=FakeSession(service);provider=Provider(root)
        session.offered['project_create']=SimpleNamespace(name='project_create',description='TEST_ONLY project entry',inputSchema=dict(type='object',properties=dict(name=dict(type='string')),required=['name']))
        cli=ArtifactCLI(session,service.store,'test',provider,ctx,continuous=continuous);cli._tools=session.offered
        self.addCleanup(lambda: None)
        return root,cli,service,session,provider
    def sources(self,service):
        with service.store.reading() as db:return [dict(r) for r in db.execute('SELECT * FROM sources ORDER BY rowid')]
    async def call(self,cli,line):
        log=io.StringIO()
        with redirect_stdout(log):result=await cli.handle_line(line)
        return result,log.getvalue()
    async def ready(self,cli):return await self.call(cli,TEXT)
    async def test_consult_variants_preserve_sources_pointer_and_approval(self):
        root,cli,service,session,p=self.setup();await self.ready(cli)
        pointer=cli._flow_pointer();before=pointer.read_bytes();sources=self.sources(service)
        # An existing approval is immutable context, never consultation authority.
        approval=cli._flow_root()/(cli.last_design_id+'-approval.json');approval.write_text('{"TEST_ONLY":"preserve"}',encoding='utf8')
        approval_before=approval.read_bytes()
        for text in ('最后面这个artifact我能直接对话交互吗？','解释一下为什么选择这个方案','如果把质量改成4kg会怎样？','确认后会做什么？'):
            await self.call(cli,text)
            self.assertEqual(self.sources(service),sources);self.assertEqual(pointer.read_bytes(),before);self.assertEqual(approval.read_bytes(),approval_before)
            request=p.calls[-1];local=json.loads(request['messages'][1]['content'])
            self.assertEqual(local['current_flow']['body'],'TEST_ONLY 当前方案正文；请补充任务事实。')
            self.assertEqual(local['current_flow']['state'],'READY');self.assertTrue(local['current_flow']['approval_recorded'])
            self.assertEqual({t['function']['name'] for t in request['tools']},set(READ_TOOLS))
        self.assertNotIn('proposal_prepare',[n for n,a in session.calls]);self.assertNotIn('execution_start',[n for n,a in session.calls])
    async def test_local_status_help_and_no_project_consult(self):
        root,cli,service,session,p=self.setup(project=False)
        for text in ('现在是什么情况？','状态','/status','/help','这里可以对话吗'):
            result,output=await self.call(cli,text);self.assertTrue(output)
        self.assertEqual(p.calls,[]);self.assertIsNone(service.store.current_project('test'))
        self.assertFalse((root/'cli-continuations').exists())
        await self.call(cli,'最后面这个artifact我能直接对话交互吗？')
        self.assertEqual(p.calls[-1]['tools'],[]);self.assertIsNone(service.store.current_project('test'));self.assertEqual(self.sources(service),[])
    async def test_explicit_modification_preserves_source_and_invalidates_old_confirmation(self):
        root,cli,service,session,p=self.setup();await self.ready(cli)
        old=cli._flow_read(cli._flow_root()/(cli.last_design_id+'.json'));original=self.sources(service)
        await self.call(cli,'把质量改为4kg，其余条件不变')
        self.assertEqual(self.sources(service)[:len(original)],original);self.assertEqual(len(self.sources(service)),2)
        with self.assertRaisesRegex(ValueError,'来源/项目已变化'):cli._flow_bind_current(old)
        self.assertEqual(p.calls[-1]['messages'][-1]['content'],'把质量改为4kg，其余条件不变')
    async def test_clarify_and_source_review_accept_facts_but_not_confirmation(self):
        for state in ('CLARIFY','SOURCE_REVIEW'):
            root,cli,service,session,p=self.setup();p.state=state;await self.ready(cli)
            with self.assertRaisesRegex(ValueError,'澄清'):await cli.handle_line('确认')
            before=self.sources(service);p.state='READY';await self.call(cli,'正确数值是1kg')
            self.assertEqual(self.sources(service)[:len(before)],before);self.assertEqual(len(self.sources(service)),2)
            self.assertEqual(cli._flow_read(cli._flow_root()/(cli.last_design_id+'.json'))['state'],'READY')
            self.assertNotIn('execution_start',[n for n,a in session.calls])
    async def test_unknown_and_mixed_intent_do_not_record_sources(self):
        root,cli,service,session,p=self.setup();await self.ready(cli);before=self.sources(service);pointer=cli._flow_pointer().read_bytes();count=len(p.calls)
        for line in ('嗯这个那个','把质量改成4kg可以吗？'):
            result,output=await self.call(cli,line);self.assertEqual(result['state'],'INTENT_CLARIFY');self.assertIn('/ask',output);self.assertIn('/require',output)
        self.assertEqual(self.sources(service),before);self.assertEqual(cli._flow_pointer().read_bytes(),pointer);self.assertEqual(len(p.calls),count)
        await self.call(cli,'/ask 把质量改成4kg可以吗？');self.assertEqual(self.sources(service),before)
        await self.call(cli,'/require 把质量改成4kg可以吗？');self.assertEqual(len(self.sources(service)),2)
    async def test_malicious_consult_batch_rejected_before_any_dispatch(self):
        root,cli,service,session,p=self.setup();await self.ready(cli);p.malicious=True
        before=self.sources(service)
        with self.assertRaisesRegex(ValueError,'未开放工具'):await self.call(cli,'解释一下当前方案')
        # The builder's two reads are the only calls; no model batch read is sent.
        self.assertEqual([n for n,a in session.calls[-2:]],['project_status','versions_list'])
        self.assertEqual(self.sources(service),before);self.assertEqual(len(p.calls),2)
    async def test_confirmation_decline_and_exit(self):
        root,cli,service,session,p=self.setup()
        with self.assertRaisesRegex(ValueError,'没有当前'):await cli.handle_line('确认')
        await self.ready(cli)
        with self.assertRaisesRegex(ValueError,'未授权执行'):await cli.handle_line('确认')
        await self.call(cli,'拒绝')
        with self.assertRaisesRegex(ValueError,'已拒绝'):await cli.handle_line('确认')
        result,_=await self.call(cli,'/exit 继续最初需求');self.assertFalse(result['exit'])
        result,output=await self.call(cli,'/exit继续最初需求，质量1kg');self.assertFalse(result['exit']);self.assertIn('分两次',output)
        self.assertEqual(len(self.sources(service)),1);self.assertEqual(len(p.calls),1)
        with patch('builtins.input',side_effect=['/exit 继续最初需求','/exit']),redirect_stdout(io.StringIO()):self.assertEqual(await cli.run(),0)
    async def test_read_only_and_advanced_entry_preserved(self):
        runtime=TestOnlyResponseRuntime();session=FakeSession();cli=ArtifactCLI(session,ReadStore(),'read',runtime,read_only=True,continuous=True);await cli.initialize()
        await self.call(cli,'解释一下为什么选择这个方案')
        with self.assertRaisesRegex(ValueError,'只读CLI'):await cli.handle_line('/require 质量4kg')
        with self.assertRaisesRegex(ValueError,'只读CLI'):await cli.handle_line('确认')
        with self.assertRaisesRegex(ValueError,'只读CLI'):await cli.handle_line('/retry')
        root,advanced,service,s,p=self.setup(continuous=False);await self.call(advanced,TEXT)
        self.assertEqual(len(self.sources(service)),1);self.assertIsNotNone(advanced.last_design_id)
        self.assertEqual(p.calls[0]['tools'],[])
    async def test_valid_ready_confirmation_and_repeat_only_queries(self):
        root,cli,service,session,p=self.setup(allow=True,project=False)
        await self.ready(cli);self.assertIsNotNone(service.store.current_project('test'))
        original=cli._call;blocked=[]
        async def bounded_call(name,args):
            if name=='execution_start':
                blocked.append((name,args));return dict(ok=False,reason='TEST_ONLY no Worker is permitted in this check')
            return await original(name,args)
        cli._call=bounded_call
        await self.call(cli,'确认')
        self.assertEqual(len(p.calls),2);self.assertEqual(len(blocked),1)
        proposal=service.store.proposal(cli.last_proposal_id);self.assertEqual(proposal['state'],'approved')
        approval=cli._flow_root()/(cli.last_design_id+'-approval.json');before=approval.read_bytes()
        query=AsyncMock(return_value=dict(ok=True,test_only=True))
        with patch.object(cli,'_existing_proposal_task',return_value='task_TEST_ONLY_existing'),patch.object(cli,'_show_call',query):
            await self.call(cli,'批准')
        query.assert_awaited_once_with('execution_status',dict(task_id='task_TEST_ONLY_existing'))
        self.assertEqual(len(p.calls),2);self.assertEqual(len(blocked),1);self.assertEqual(approval.read_bytes(),before)
    async def test_question_marks_are_consultation_not_decisions(self):
        root,cli,service,session,p=self.setup(allow=True);await self.ready(cli)
        pointer=cli._flow_pointer().read_bytes();before=self.sources(service)
        for line in ('确认？','拒绝？','批准吗？'):
            await self.call(cli,line)
            self.assertEqual(json.loads(p.calls[-1]['messages'][1]['content'])['dialogue_purpose'],'TASK_CONSULTATION')
        self.assertEqual(self.sources(service),before);self.assertEqual(cli._flow_pointer().read_bytes(),pointer)
        self.assertFalse((cli._flow_root()/(cli.last_design_id+'-declined.json')).exists())
        self.assertFalse((cli._flow_root()/(cli.last_design_id+'-approval.json')).exists())
    def test_new_request_polite_suffix_and_exact_confirm(self):
        self.assertEqual(task_intent('我要一个质量1kg的小车，运行3秒，请先整理方案，可以吗？'),'require')
        self.assertEqual(task_intent('确认后会做什么？'),'consult')
        self.assertEqual(task_intent('把质量改为4kg，其余条件不变'),'require')
        self.assertEqual(task_intent('请设计一个质量1kg的小车，如果失败，请保留证据并解释结果'),'require')
        self.assertEqual(task_intent('请设计一个质量1kg的小车，最后解释结果'),'require')

if __name__=='__main__':unittest.main(verbosity=2)
