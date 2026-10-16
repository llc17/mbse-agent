"""Targeted ordinary CLI/stdio MCP/TEST_ONLY Worker checks; zero HTTP/OMC."""
import asyncio,io,json,sqlite3,tempfile,unittest,uuid
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from week12.src.car_domain.artifact_cli import async_main
from week12.src.car_domain.artifact_continuous import FLOW_PROMPT,projection,compare_candidate,resumable_generation
from week12.src.car_domain.model_artifact import save
from week12.src.car_domain.artifact_session import configure_run,digest

OUT=Path(__file__).resolve().parents[1]/'results/s5-continuous-cli-20261012-01'
TEXT='TEST_ONLY 普通CLI流程夹具：质量1kg；1秒时末值 signal >= 0。不是实际车辆或物理证据。'
FACTS=[]

def spec(source):
    value=dict(schema_version='v11-model-artifact-1',backend='model-artifact',artifact=dict(model_name='ContinuousFixture',modelica_source='model ContinuousFixture\n parameter Real mass=1;\n parameter Real level=0.6;\n Real signal(start=0,fixed=true);\nequation\n der(signal)=level;\nend ContinuousFixture;\n'),sources=[source],requirements=[dict(text='TEST_ONLY finite process check',source_id=source['id'],source_text=source['text'])],design=dict(summary='TEST_ONLY 流程信号模型；非实际车',assumptions=[dict(text='TEST_ONLY Worker合成结果；没有OMC',origin='model')],parameters=[dict(name='level',value=.6,unit='1/s',origin='model',basis='TEST_ONLY chosen value')]),libraries=[],settings=dict(stop_time=1,intervals=20),outputs=dict(signal='signal'),checks=[dict(kind='final_ge',output='signal',threshold=0,source_id=source['id'],source_text=source['text'])])
    fields=dict(value['requirements'][0],model_name='ContinuousFixture')
    value['artifact']['sysml_source']='package ContinuousTest { part def System { attribute mass : Real = 1; attribute level : Real = 0.6; } part system : System; requirement req_1 { '+''.join('attribute '+k+' : String = '+json.dumps(v,ensure_ascii=False)+'; ' for k,v in fields.items())+'} }'
    return value

def commitment(source):
    result=projection(spec(source));result['literal_bindings']=[dict(name='mass',value=1,unit='kg',origin='user',source_id=source['id'],source_text='质量1kg'),dict(name='level',value=.6,unit='1/s',origin='model',source_id=None,source_text=None)];return result

class ControlledProvider:
    """Finite controlled provider; no HTTP client and no API key lookup."""
    def __init__(self,root,*,clarify=False,drift=None,cap=2):
        self.ledger_path=root/'runtime.sqlite';self.calls=[];self.cache={};self.clarify=clarify;self.drift=drift
        self.config=dict(max_requests=cap,max_total_tokens=16000,max_usd=.02)
    def complete(self,messages,tools,rid):
        if rid in self.cache:return deepcopy(self.cache[rid])
        if len(self.calls)>=self.config['max_requests']:raise RuntimeError('TEST_ONLY finite request budget exhausted')
        self.calls.append(rid);source=json.loads(messages[1]['content'])['trusted_sources'][-1]
        if messages[0]['content'].startswith(FLOW_PROMPT):
            value=dict(schema='v11-continuous-design-1',state='CLARIFY' if self.clarify else 'READY',body='TEST_ONLY 请补充缺少的任务事实。' if self.clarify else 'TEST_ONLY 具体方案：1kg，信号模型；末值≥0，运行1秒；结构及物理验收未知。',commitment=None if self.clarify else commitment(source))
            response=dict(content=json.dumps(value,ensure_ascii=False),tool_calls=[])
        else:
            value=spec(source)
            if self.drift=='settings':value['settings']['stop_time']=2
            if self.drift=='comment':value['artifact']['modelica_source']=value['artifact']['modelica_source'].replace('parameter Real mass=1;','// parameter Real mass=1;\n parameter Real mass=2;')
            response=dict(content='TEST_ONLY 按已确认承诺生成；非在线输出。',tool_calls=[dict(id='TEST_ONLY_call',function=dict(name='proposal_prepare',arguments=json.dumps(dict(spec=value))))])
        self.cache[rid]=deepcopy(response);return response

class Checks(unittest.IsolatedAsyncioTestCase):
    def setup(self,**kwargs):
        OUT.mkdir(exist_ok=True);root=Path(tempfile.mkdtemp(prefix='TEST_ONLY_',dir=OUT));session='test_'+uuid.uuid4().hex
        save(root/'context.json',dict(schema='v11-artifact-execution-context-1',context_id=session,status='ACTIVE',mode='TEST_ONLY',allow_execution=True,max_tasks=1,worker_timeout_s=30,simulation_timeout_s=10))
        args=SimpleNamespace(db=root/'projects.sqlite',session=session,execution_context=root/'context.json',read_only=False,debug_offline=True,runtime_config=None,runtime_ledger=None,advanced_workflow=False)
        return root,args,ControlledProvider(root,**kwargs)
    async def run_case(self,args,runtime,case,name):
        log=io.StringIO()
        with redirect_stdout(log):result=await async_main(args,runtime=runtime,case=case)
        (Path(args.db).parent/(name+'-console.txt')).write_text(log.getvalue(),encoding='utf8');return result
    def count(self,root,table):
        with sqlite3.connect(root/'projects.sqlite') as db:return db.execute('SELECT count(*) FROM '+table).fetchone()[0]
    async def test_ordinary_interactive_entry_and_repeated_confirmation(self):
        root,args,provider=self.setup()
        async def case(cli):
            with patch('builtins.input',side_effect=[TEXT,'批准','结果','确认','/exit']):return await cli.run()
        await self.run_case(args,provider,case,'ordinary')
        console=(root/'ordinary-console.txt').read_text(encoding='utf8')
        for visible in ('冻结设计：','需求：','假设：','末值判据：signal ≥ 0','正在接续执行'):self.assertIn(visible,console)
        self.assertNotIn('modelica_source',console);self.assertNotIn('runtime_config_path',console)
        self.assertEqual(len(provider.calls),2);self.assertEqual(self.count(root,'tasks'),1);self.assertEqual(self.count(root,'sources'),1)
        with sqlite3.connect(root/'projects.sqlite') as db:
            task=json.loads(db.execute('SELECT result_json FROM tasks').fetchone()[0]);confirmation=json.loads(db.execute('SELECT confirmation_json FROM proposals').fetchone()[0])
        self.assertEqual(task['task_status'],'PASS');self.assertTrue(task['test_only']);self.assertEqual(task['toolchain_invocations'],0)
        self.assertEqual(confirmation['channel'],'TEST_ONLY_DESIGN_DELEGATION');self.assertFalse(confirmation['source_reviewed_by_human']);self.assertEqual(confirmation['design_approval']['approval_text'],'批准')
        FACTS.append(dict(case='ordinary_interactive_natural_input_approval_generation_stdio_mcp_test_worker_result_repeat',root=str(root),controlled_provider_calls=2,tasks=1,task_status=task['task_status'],actual_http=0,actual_omc=0))
    async def test_clarification_and_decline_have_no_generation(self):
        for clarify in (True,False):
            root,args,provider=self.setup(clarify=clarify)
            async def case(cli):
                await cli.handle_line(TEXT)
                if clarify:
                    with self.assertRaisesRegex(ValueError,'澄清'):await cli.handle_line('确认')
                else:
                    await cli.handle_line('拒绝')
                    with self.assertRaisesRegex(ValueError,'拒绝'):await cli.handle_line('确认')
                await cli.handle_line(TEXT)
            await self.run_case(args,provider,case,'decline-or-clarify');self.assertEqual(len(provider.calls),1);self.assertEqual(self.count(root,'tasks'),0)
        FACTS.append(dict(case='clarify_decline_and_repeated_request',actual_http=0,tasks=0))
    async def test_source_budget_and_candidate_drift_stop_before_worker(self):
        for kind in ('source','budget','settings','comment','exhausted'):
            root,args,provider=self.setup(drift=kind,cap=1 if kind=='exhausted' else 2)
            async def case(cli):
                await cli.handle_line(TEXT)
                if kind=='source':cli.store.record_source(cli.session_id,'TEST_ONLY changed requirement')
                if kind=='budget':provider.config['max_usd']=.03
                with self.assertRaises((ValueError,RuntimeError)):await cli.handle_line('确认')
            await self.run_case(args,provider,case,'drift-'+kind);self.assertEqual(self.count(root,'tasks'),0)
        FACTS.append(dict(case='source_budget_settings_comment_literal_and_exhaustion',tasks=0,actual_http=0))
    def test_strings_duplicate_and_real_literal_negatives(self):
        source=dict(id='src_test',text=TEXT);comm=commitment(source)
        for replace in ('parameter Real mass=2;\n String note="parameter Real mass=1;";','parameter Real mass=1;\n parameter Real mass=1;','parameter Real mass=2; // parameter Real mass=1;'):
            candidate=spec(source);candidate['artifact']['modelica_source']=candidate['artifact']['modelica_source'].replace('parameter Real mass=1;',replace)
            with self.assertRaisesRegex(ValueError,'字面参数'):compare_candidate(comm,candidate)
    def test_confirmed_trajectory_ranges_and_tolerances_cannot_change(self):
        source=dict(id='src_test',text=TEXT);candidate=spec(source);comm=commitment(source)
        trajectory=dict(schema='v11-artifact-trajectory-1',route_semantics='ordered_forward_waypoints',entities=dict(car=dict(x='signal',z='height')),milestones=[dict(id='first',phase='flat',targets=dict(car=dict(x_min=.5,x_max=1,z_min=0,z_max=0)))],start_time=0,end_time=1,max_sample_gap=.1,max_speed=2,position_tolerance=.001,time_tolerance=.01,coverage_note='TEST_ONLY controlled route condition',requirement_refs=[dict(source_id=source['id'],source_text=source['text'])])
        comm['trajectory_contract']=trajectory;candidate['trajectory_contract']=dict(deepcopy(trajectory),model_basis='der(signal)=level;')
        compare_candidate(comm,candidate)
        for key,value in (('position_tolerance',.1),('end_time',2)):
            changed=deepcopy(candidate);changed['trajectory_contract'][key]=value
            with self.assertRaisesRegex(ValueError,'成功判据'):compare_candidate(comm,changed)
        changed=deepcopy(candidate);changed['trajectory_contract']['milestones'][0]['targets']['car']['x_max']=4
        with self.assertRaisesRegex(ValueError,'成功判据'):compare_candidate(comm,changed)
    async def test_uncertain_dispatch_does_not_start_second_worker(self):
        root,args,provider=self.setup()
        async def first(cli):
            await cli.handle_line(TEXT);original=cli._call
            async def uncertain(name,arguments):
                if name=='execution_start':raise RuntimeError('TEST_ONLY unknown dispatch before observed task')
                return await original(name,arguments)
            cli._call=uncertain
            with self.assertRaises(RuntimeError):await cli.handle_line('确认')
        await self.run_case(args,provider,first,'uncertain-first')
        async def second(cli):
            with self.assertRaisesRegex(RuntimeError,'不重发'):await cli.handle_line('同意')
        await self.run_case(args,provider,second,'uncertain-reentry');self.assertEqual(len(provider.calls),2);self.assertEqual(self.count(root,'tasks'),0)
        FACTS.append(dict(case='uncertain_dispatch_reentry_no_resend',root=str(root),controlled_provider_calls=2,tasks=0,actual_http=0,actual_omc=0))
    async def test_reconnect_after_generation_before_first_worker_and_read_only(self):
        root,args,provider=self.setup()
        async def first(cli):
            await cli.handle_line(TEXT)
            with patch('week12.src.car_domain.artifact_service.ArtifactService.confirm_design_delegated_local',side_effect=OSError('TEST_ONLY interruption before first execution')):
                with self.assertRaises(OSError):await cli.handle_line('确认')
        await self.run_case(args,provider,first,'before-reconnect');self.assertTrue(resumable_generation(root,args.session));self.assertEqual(self.count(root,'tasks'),0)
        # Ordinary startup must permit deterministic cached generation delivery
        # despite two spent request slots. This is a synthetic LIVE manifest only;
        # the actual context remains TEST_ONLY and no model transport exists.
        save(root/'runtime.json',dict(provider.config));save(root/'run.json',dict(schema='v11-artifact-daily-run-1',name='TEST_ONLY_resume',status='ACTIVE',mode='LIVE',session=args.session,baseline=None,runtime_profile='daily',caps=dict(max_http_requests=2,max_total_tokens=16000,max_usd=.02),runtime_config_sha256=digest(root/'runtime.json'),context_sha256=digest(root/'context.json')))
        with sqlite3.connect(root/'runtime.sqlite') as db:
            db.execute('CREATE TABLE requests(actual_tokens INTEGER,reserve_tokens INTEGER,actual_cost REAL,reserve_cost REAL)');db.execute('CREATE TABLE meta(key TEXT,value TEXT)');db.executemany('INSERT INTO requests VALUES(1,1,0,0)',[( ),( )])
        startup=SimpleNamespace(run='TEST_ONLY_resume',baseline_db=None,baseline_session=None,runtime_profile=None,prepare=False,read_only=False,offline=False,advanced_workflow=False)
        with patch('week12.src.car_domain.artifact_session.run_path',return_value=root),redirect_stdout(io.StringIO()):configure_run(startup)
        self.assertFalse(startup.read_only)
        async def second(cli):return await cli.handle_line('同意')
        await self.run_case(args,provider,second,'reconnected');self.assertEqual(len(provider.calls),2);self.assertEqual(self.count(root,'tasks'),1)
        with patch('week12.src.car_domain.artifact_session.run_path',return_value=root),redirect_stdout(io.StringIO()):configure_run(startup)
        self.assertTrue(startup.read_only)
        before=(root/'projects.sqlite').read_bytes();args.read_only=True;args.execution_context=None
        async def read(cli):
            await cli.handle_line('结果')
            with self.assertRaises(ValueError):await cli.handle_line('确认')
        await self.run_case(args,None,read,'readonly');self.assertEqual(before,(root/'projects.sqlite').read_bytes())
        FACTS.append(dict(case='real_stdio_reconnect_cached_delivery_first_worker_then_readonly',root=str(root),controlled_provider_calls=2,tasks=1,db_bytes_unchanged_on_readonly=True,actual_http=0,actual_omc=0))

if __name__=='__main__':
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(Checks);result=unittest.TextTestRunner(verbosity=2).run(suite)
    OUT.mkdir(exist_ok=True);save(OUT/'verification.json',dict(test_only=True,tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),actual_http=0,actual_omc=0,facts=FACTS))
    raise SystemExit(0 if result.wasSuccessful() else 1)
