"""Repeated ordinary CLI fixtures: actual Store/schema/runtime, injected wire/RPC.

No real HTTP, MCP process, compiler, worker or dynamics is launched. Synthetic
terminal records are explicitly TEST_ONLY and never prove a vehicle result.
"""
import asyncio,copy,io,json,os,subprocess,sys,tempfile,unittest,uuid
from contextlib import redirect_stdout
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
from week13.src import case_paths,case_requests,case_taskflow,case_cli
from week13.src.case_backend import module,service
from week13.src.case_runtime import CaseRuntime
from week13.src.case_budget import CaseBudget

def write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf8')

def synthetic_spec(source,mass):
    params=dict(m=mass,I_pitch=20,T_main_max=60,T_nose_max=25)
    requirement=dict(text='隔离fixture要求',source_id=source['id'],source_text=source['text'])
    model='model Fixture '+''.join('parameter Real '+k+'='+str(v)+'; ' for k,v in params.items())+'Real signal(start=0); equation der(signal)=0; annotation(experiment(StopTime=120,Tolerance=.0001,Interval=.05)); end Fixture;'
    traces=''.join('attribute '+key+' : String = '+json.dumps(value,ensure_ascii=False)+';' for key,value in dict(requirement,model_name='Fixture').items())
    sysml='package FixturePackage { part def System {'+''.join('attribute '+key+' : Real = '+str(value)+';' for key,value in params.items())+'} part system : System; requirement req_1 {'+traces+'} }'
    return dict(schema_version='v11-model-artifact-1',backend='model-artifact',artifact=dict(model_name='Fixture',modelica_source=model,sysml_source=sysml),sources=[source],requirements=[requirement],design=dict(summary='TEST_ONLY synthetic product design',assumptions=[dict(text='TEST_ONLY',origin='model')],parameters=[dict(name=k,value=v,unit='kg' if k=='m' else '1',origin='model',basis='isolated fixture') for k,v in params.items()]),libraries=[],settings=dict(stop_time=120,intervals=2400),outputs=dict(signal='signal'),checks=[])

def finish_fixture(svc,task,root,task_status='FAIL'):
    """No launch: materialize a hash-bound TEST_ONLY terminal receipt."""
    task=svc.store.reserve_launch(task['task_id'],'fixture_owner');task=svc.store.claim_task(task['task_id'],'fixture_owner',99999)
    proposal=svc.store.proposal(task['proposal_id']);spec=proposal['spec'];base=Path(root)/'projects'/task['project_id']/'versions'/task['version_id'];base.mkdir(parents=True)
    for name,value in [('spec.json',spec),('confirmation.json',proposal['confirmation'])]:write(base/name,value)
    if 'baseline_ref' in spec:write(base/'baseline-binding.json',spec['baseline_ref'])
    (base/'frozen-model.mo').write_text(spec['artifact']['modelica_source'],encoding='utf8');(base/'frozen-model.sysml').write_text(spec['artifact']['sysml_source'],encoding='utf8')
    (base/'tool').mkdir();(base/'tool/model_res.csv').write_text('time,signal\n0,0\n120,1\n',encoding='utf8')
    outcome=dict(schema_version='v11-artifact-outcome-1',backend='model-artifact',tool_status='PASS',chain_status='PASS',task_status=task_status,vehicle_task_status='UNKNOWN',physical_acceptance_status='NOT_VERIFIED',published_version_id=None,test_only=True,execution_mode='TEST_ONLY',mission_assessment=dict(task_status=task_status,validity_status='UNKNOWN',fixture_unique_metric=100+int(task['version_id'][1:]),scope='SYNTHETIC_NO_DYNAMICS'),tool_result=dict(compile=dict(status='TEST_ONLY'),simulation=dict(status='TEST_ONLY')))
    write(base/'outcome.json',outcome)
    files={p.relative_to(base).as_posix():dict(sha256=case_paths.sha_file(p),bytes=p.stat().st_size) for p in base.rglob('*') if p.is_file()}
    binding=dict(task_id=task['task_id'],project_id=task['project_id'],spec_digest=proposal['spec_digest'],source_sha256=proposal['display']['source_sha256'],owner='fixture_owner',context_id=svc.context.context_id,context_sha256=svc.context.digest)
    if 'baseline_ref' in spec:binding['artifact_baseline']=spec['baseline_ref']
    write(base/'artifact-manifest.json',dict(schema='v11-artifact-manifest-1',binding=binding,outcome=outcome,files=files))
    result=dict(outcome,outcome_manifest_path=str(base/'artifact-manifest.json'),outcome_manifest_sha256=case_paths.sha_file(base/'artifact-manifest.json'),worker_exit_code=0,worker_pid=99999)
    return svc.store.finish_task(task['task_id'],'succeeded',owner='fixture_owner',result=result)

class NoLaunch:
    def start(self,task_id):pass

class Harness:
    def __init__(self,root,seed=True):
        self.root=Path(root);self.projectroot=self.root/'shared-project';self.db=self.projectroot/'projects.sqlite'
        case_requests.ROOT=self.root;case_requests.REQUESTS_ROOT=self.projectroot/'requests';case_requests.LEGACY_PROJECT_ROOT=self.projectroot
        if seed:
            (self.root/'configs').mkdir(parents=True);(self.root/'configs/case_runtime.json').write_bytes((case_paths.ROOT/'configs/case_runtime.json').read_bytes())
            context=self.root/'seed-context.json';write(context,dict(schema='v11-artifact-execution-context-1',context_id='fixture_seed',status='ACTIVE',mode='TEST_ONLY',allow_execution=True,max_tasks=6,worker_timeout_s=1900,simulation_timeout_s=900))
            svc=module('artifact_service').ArtifactService(self.db,'fixture_seed',context=context,worker_manager=NoLaunch());svc.project_root=self.projectroot
            project=svc.project_create('TEST_ONLY repeated request fixture')['project'];self.project_id=project['project_id']
            source=svc.record_source('TEST_ONLY 历史基线事实');source=dict(id=source['source_id'],text=source['text']);base=synthetic_spec(source,85)
            checked=module('model_artifact').validate_artifact(base,require_sysml=True)
            if not checked['ready']:raise AssertionError(checked)
            (self.projectroot/'baseline-original.mo').write_text(base['artifact']['modelica_source'],encoding='utf8');write(self.projectroot/'imported-manifest.json',dict(source_sha256=checked['source_sha256'],spec_digest=checked['spec_digest']))
            record=dict(project_id=self.project_id,task_id='reference_fixture',version_id='v000000',spec_digest=checked['spec_digest'],source_sha256=checked['source_sha256'],manifest_sha256=case_paths.sha_file(self.projectroot/'imported-manifest.json'),frozen_spec=base,record_kind='IMPORTED_REFERENCE',task_status='NOT_RUN')
            write(self.projectroot/'imported-reference.json',record);write(self.projectroot/'run.json',dict(project_id=self.project_id,session_id='fixture_seed'))
            for mass in (85,90):
                prepared=svc.proposal_prepare(synthetic_spec(source,mass));p=svc.store.proposal(prepared['proposal']['proposal_id']);svc.confirm_local(p['proposal_id'],p['displayed_digest']);task=svc.execution_start(p['proposal_id'],'fixture_seed_'+str(mass))['task'];finish_fixture(svc,task,self.projectroot)
        else:self.project_id=json.loads((self.projectroot/'run.json').read_text())['project_id']
    def bootstrap(self,*args):
        with patch.object(case_taskflow,'PROJECT_ROOT',self.projectroot),patch.object(case_taskflow,'PROJECT_DB',self.db):return case_taskflow.bootstrap(*args)
    def authorize(self,rid,reuse=False):
        return case_requests.authorize_budget(rid,'fixture_budget','CONFIRM',reuse=reuse,test_only=True,**({} if reuse else dict(caps=dict(http_requests=20,product_cny=150,total_tokens=30000000,total_output_tokens=20*393216,compile=4,simulation=4,candidates=2,software_repair_rounds=1,parameter_revision_rounds=0,observation_extensions=0),hard_deadline_utc=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat())))
    def flow(self,rid):return case_taskflow.TaskFlow.load(rid,runtime_factory=self.runtime,rpc=self.rpc)
    def runtime(self,ctx):return CaseRuntime(Path(ctx['root'])/'runtime.json',task_context=ctx,transport=lambda payload,*args:self.transport(ctx,payload))
    def transport(self,ctx,payload):
        user=json.loads(payload['messages'][1]['content']);tool=(payload.get('tools') or [{}])[0].get('function',{}).get('name')
        if tool=='case_request_plan':
            assert user['raw_request']==ctx['raw_request']
            lookup={'把总质量改为92kg，增加部分在原重心，几何控制不变。':92,'在刚通过的版本上再增加2kg理想点载。':94}
            if ctx['raw_request'] in lookup:
                p=dict(m=lookup[ctx['raw_request']],I_pitch=20,T_main_max=60,T_nose_max=25);req=dict(text=ctx['raw_request'],source_id=ctx['sources'][0]['id'],source_text=ctx['raw_request'])
                packet=dict(intent='CHANGE',support='SUPPORTED',missing=[],unsupported=[],requirements=[req],design=dict(summary='TEST_ONLY 产品方案',assumptions=[dict(text='新增质量在原重心理想点质量，I继承',origin='model')],parameters=[dict(name=k,value=v,unit='kg' if k=='m' else '1',origin='model',basis='TEST_ONLY product stub interpretation') for k,v in p.items()]),proposed_parameters=p,defaults=['DASSL，继承观察区间和容差'],unknowns=['实物未知'],rationale='TEST_ONLY',scope=dict(change_kind='center_of_mass_point_load',mass_location='original_center_of_mass',inertia_assumption='ideal_point_mass',frozen='geometry_contact_control_terrain_task'))
            else:
                unsupported='轮径' in ctx['raw_request'];packet=dict(intent='CHANGE',support='UNSUPPORTED' if unsupported else 'NEEDS_INPUT',missing=[] if unsupported else ['总质量或增加质量不明确'],unsupported=['几何变化未支持'] if unsupported else [],requirements=[],design={},proposed_parameters={},defaults=[],unknowns=[],rationale='TEST_ONLY honest support boundary',scope={})
            args=dict(packet=packet)
        elif tool=='proposal_prepare':
            spec=synthetic_spec(ctx['sources'][0],ctx['plan']['proposed_parameters']['m']);spec['baseline_ref']=ctx['baseline_ref'];spec['sources']=ctx['sources'];spec['requirements']=ctx['plan']['requirements'];spec['design']=ctx['plan']['design'];req=spec['requirements'][0]
            traces=''.join('attribute '+key+' : String = '+json.dumps(value,ensure_ascii=False)+';' for key,value in dict(req,model_name='Fixture').items())
            head=spec['artifact']['sysml_source'].split('requirement req_1')[0];spec['artifact']['sysml_source']=head+'requirement req_1 {'+traces+'} }';args=dict(spec=spec)
        else:
            actual=user.get('actual_request_execution')
            if actual:
                assert actual['historical_recorded_outcome']['mission_assessment']['fixture_unique_metric']>=103
                assert actual['task_status']=='UNKNOWN'
                assert actual['current_contact_review']['status']=='TEST_ONLY'
            message=dict(content='TEST_ONLY 新结果已绑定；任务FAIL，模型和实物能力分开。',reasoning_content='fixture protocol only')
            return dict(id='fixture_reply',model='deepseek-flash',choices=[dict(message=message,finish_reason='stop')],usage=dict(prompt_tokens=20,completion_tokens=10,total_tokens=30))
        call=dict(id='fixture_call',type='function',function=dict(name=tool,arguments=json.dumps(args,ensure_ascii=False)))
        return dict(id='fixture_reply',model='deepseek-flash',choices=[dict(message=dict(content='',reasoning_content='fixture protocol',tool_calls=[call]),finish_reason='tool_calls')],usage=dict(prompt_tokens=20,completion_tokens=10,total_tokens=30))
    async def rpc(self,flow,name,args):
        svc=flow.open();svc.worker_manager=NoLaunch()
        result=getattr(svc,name)(**args)
        if name=='execution_start' and result['task']['state']=='queued':finish_fixture(svc,result['task'],self.projectroot,getattr(self,'outcome_status','FAIL'))
        return result
    def cli(self,args):
        output=io.StringIO()
        with redirect_stdout(output):code=case_cli.main(args,flow_factory=self.flow,bootstrap_factory=self.bootstrap)
        result=json.loads(output.getvalue());return code,result

def use_flat_fixture_transport(harness):
    """Adapt this old wire fixture at the configured transport boundary only."""
    original=harness.transport
    def transport(ctx,payload):
        response=original(ctx,payload)
        from week13.tests.test_case_flat_plan_protocol import flat
        for call in response['choices'][0]['message'].get('tool_calls',[]):
            args=json.loads(call['function']['arguments'])
            if call['function']['name']=='case_request_plan' and 'packet' in args:
                args=flat(args)
            elif call['function']['name']=='proposal_prepare':
                args['spec']['sources']=[dict(id=row['id']) for row in args['spec']['sources']]
            call['function']['arguments']=json.dumps(args,ensure_ascii=False)
        return response
    harness.transport=transport

class RepeatedRequests(unittest.TestCase):
    def setUp(self):
        self.saved=(case_requests.ROOT,case_requests.REQUESTS_ROOT,case_requests.LEGACY_PROJECT_ROOT)
        self.temp=tempfile.TemporaryDirectory(dir=case_paths.PACKAGE);self.h=Harness(Path(self.temp.name)/'fixture')
        use_flat_fixture_transport(self.h)
    def tearDown(self):
        case_requests.ROOT,case_requests.REQUESTS_ROOT,case_requests.LEGACY_PROJECT_ROOT=self.saved;self.temp.cleanup()
    def create(self,rid,version,text):
        code,result=self.h.cli(['request','--request-id',rid,'--project-id',self.h.project_id,'--baseline-version',version,'--requirement',text]);self.assertEqual(code,0,result);return case_requests.load_request(rid)
    def child(self,rid,action):
        env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1');env.pop('MBSE_V12_REQUEST_ID',None)
        command=[sys.executable,'-X','utf8','-B','-m','week13.tests.test_case_agent_requests','--child',str(self.h.root),rid,action]
        child=subprocess.run(command,cwd=case_paths.REPO,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf8',timeout=60)
        self.assertEqual(child.returncode,0,child.stderr+'\n'+child.stdout);return json.loads(child.stdout)
    def test_ordinary_cli_two_new_targets_and_independent_process_reopen(self):
        ctx=self.create('fixture_a','v000002','把总质量改为92kg，增加部分在原重心，几何控制不变。');self.assertEqual(case_taskflow.modelica_parameters(ctx['baseline_snapshot']['frozen_spec']['artifact']['modelica_source'])[0]['m'],90)
        code,blocked=self.h.cli(['run-request','--request-id','fixture_a']);self.assertEqual(code,1);self.assertIn('no newly approved',blocked['error'])
        self.h.authorize('fixture_a');planned=self.child('fixture_a','plan-request');self.assertEqual(planned['plan']['proposed_parameters']['m'],92)
        code,waiting=self.h.cli(['run-request','--request-id','fixture_a']);self.assertEqual(code,0,waiting);self.assertEqual(waiting['stage'],'AWAITING_CONFIRMATION_OR_EXECUTION_AUTHORITY')
        self.assertEqual(self.h.cli(['confirm-request','--request-id','fixture_a','--displayed-digest',planned['displayed_digest'],'--approval-text','CONFIRM'])[0],0)
        first=self.child('fixture_a','run-request');self.assertEqual(first['state']['latest_request_execution']['version_id'],'v000003')
        self.assertEqual(first['generation']['delta']['parameters']['m'],92);self.assertEqual(first['execution']['task']['result']['mission_assessment']['fixture_unique_metric'],103)
        budget=CaseBudget(task_context=case_requests.load_request('fixture_a'));before=budget.usage()['requests'];self.assertTrue(self.h.cli(['run-request','--request-id','fixture_a'])[1]['reused']);self.assertEqual(budget.usage()['requests'],before)
        sources_before=self.h.flow('fixture_a').state();usage_before=budget.usage()['requests'];authority_before=case_requests.authority_for(case_requests.load_request('fixture_a'))
        reopened=self.child('fixture_a','request-reopen');sources_after=self.h.flow('fixture_a').state();authority_after=case_requests.authority_for(case_requests.load_request('fixture_a'))
        self.assertTrue(reopened['read_only_status_unchanged'])
        self.assertEqual({key:value for key,value in sources_after.items() if key!='http_state'},{key:value for key,value in sources_before.items() if key!='http_state'})
        self.assertEqual(budget.usage()['requests'],usage_before+1)
        self.assertGreaterEqual(sources_after['http_state']['cost_cny_reserved_or_actual'],sources_before['http_state']['cost_cny_reserved_or_actual'])
        self.assertEqual({key:authority_after[key] for key in ('budget_id','ledger_path','hard_deadline_utc')},{key:authority_before[key] for key in ('budget_id','ledger_path','hard_deadline_utc')})
        self.create('fixture_b','v000003','在刚通过的版本上再增加2kg理想点载。');self.h.authorize('fixture_b',reuse=True)
        second_plan=self.h.cli(['plan-request','--request-id','fixture_b'])[1];self.assertEqual(second_plan['plan']['proposed_parameters']['m'],94)
        self.h.cli(['confirm-request','--request-id','fixture_b','--displayed-digest',second_plan['displayed_digest'],'--approval-text','CONFIRM']);second=self.h.cli(['run-request','--request-id','fixture_b'])[1]
        self.assertEqual(second['state']['latest_request_execution']['version_id'],'v000004');self.assertEqual(second['generation']['delta']['parameters']['m'],94)
        self.assertEqual(budget.usage()['counters']['candidate'],2)
        self.assertEqual([x['version_id'] for x in second['state']['versions']],['v000001','v000002','v000003','v000004'])
        for rid in ('fixture_a','fixture_b'):
            root=Path(case_requests.load_request(rid)['root']);self.assertTrue(list((root/'http').glob('*/request.json')))
            for path in (root/'http').glob('*/request.json'):self.assertEqual(json.loads(path.read_text())['authorization_sha256'],case_paths.sha_file(case_requests.load_request(rid)['runtime_authority']['path']))
        with self.assertRaises(ValueError):self.h.bootstrap('fixture_a',self.h.project_id,'v000002','不同原文不能复用ID')
    def test_missing_and_unsupported_never_confirm_generate_or_consume_candidate(self):
        self.create('fixture_missing','v000002','多加一点载荷。');self.h.authorize('fixture_missing')
        first=self.h.cli(['run-request','--request-id','fixture_missing'])[1];self.assertEqual(first['proposal']['plan']['support'],'NEEDS_INPUT')
        self.create('fixture_geom','v000002','把轮径改成更大。');self.h.authorize('fixture_geom',reuse=True)
        second=self.h.cli(['run-request','--request-id','fixture_geom'])[1];self.assertEqual(second['proposal']['plan']['support'],'UNSUPPORTED')
        for rid,item in [('fixture_missing',first),('fixture_geom',second)]:
            self.assertEqual(self.h.cli(['confirm-request','--request-id',rid,'--displayed-digest',item['proposal']['displayed_digest'],'--approval-text','CONFIRM'])[0],1)
            self.assertEqual(len(self.h.flow(rid).state()['versions']),2)
        self.assertEqual(CaseBudget(task_context=case_requests.load_request('fixture_missing')).usage()['counters'].get('candidate',0),0)
    def supported(self,rid='fixture_repair'):
        self.create(rid,'v000002','把总质量改为92kg，增加部分在原重心，几何控制不变。');self.h.authorize(rid)
        return rid
    def confirm(self,rid):
        planned=self.h.cli(['plan-request','--request-id',rid])[1]
        code,result=self.h.cli(['confirm-request','--request-id',rid,'--displayed-digest',planned['displayed_digest'],'--approval-text','CONFIRM','--delegated-confirmation','--approval-basis','TEST_ONLY 实际集中批准范围记录'])
        self.assertEqual(code,0,result);self.assertFalse(result['human_reviewed_displayed_plan'])
    def test_run_auto_repairs_complete_bad_json_with_new_id_and_known_usage(self):
        rid=self.supported();original=self.h.transport;sent=[]
        def bad_once(ctx,payload):
            response=original(ctx,payload);tool=(payload.get('tools') or [{}])[0].get('function',{}).get('name');sent.append(tool)
            response['model']='DeepSeek-V4.1-Flash-1017'
            if tool=='case_request_plan' and len(sent)==1:response['choices'][0]['message']['tool_calls'][0]['function']['arguments']='{"packet":'
            return response
        self.h.transport=bad_once
        code,result=self.h.cli(['plan-request','--request-id',rid]);self.assertEqual(code,0,result);self.assertEqual(result['plan']['proposed_parameters']['m'],92)
        ctx=case_requests.load_request(rid);budget=CaseBudget(task_context=ctx);self.assertEqual(budget.usage()['requests'],2);self.assertEqual(budget.usage()['counters']['software_repair'],1)
        self.assertTrue((Path(ctx['root'])/'failures/plan-0.json').exists());self.assertTrue((Path(ctx['root'])/'turns/plan_repair_1/response.json').exists());self.assertEqual(ctx['plan']['proposed_parameters']['m'],92)
        failure=json.loads((Path(ctx['root'])/'failures/plan-0.json').read_text());summary=self.h.flow(rid).state()['last_failure'];self.assertEqual(summary['error'],failure['error']);self.assertEqual(summary['status'],'RESOLVED_WITH_LATER_VALIDATION');self.assertNotIn('response',summary)
    def test_run_auto_repairs_double_source_without_changing_confirmed_design(self):
        rid=self.supported();self.confirm(rid);original=self.h.transport
        def bad_first_generation(ctx,payload):
            response=original(ctx,payload);tool=(payload.get('tools') or [{}])[0].get('function',{}).get('name')
            if tool=='proposal_prepare' and 'bounded_protocol_repair' not in json.loads(payload['messages'][1]['content']):
                call=response['choices'][0]['message']['tool_calls'][0];args=json.loads(call['function']['arguments']);args['spec']['artifact']['sysml_source']=args['spec']['artifact']['sysml_source'].replace('req_1','wrong_req');call['function']['arguments']=json.dumps(args,ensure_ascii=False)
            return response
        self.h.transport=bad_first_generation;before=copy.deepcopy(case_requests.load_request(rid)['plan']);code,result=self.h.cli(['run-request','--request-id',rid]);self.assertEqual(code,0,result)
        self.assertEqual(case_requests.load_request(rid)['plan'],before);budget=CaseBudget(task_context=case_requests.load_request(rid));self.assertEqual(budget.usage()['counters']['candidate'],1);self.assertEqual(budget.usage()['counters']['software_repair'],1)
        latest=result['state']['latest_request_execution']
        self.assertEqual(latest['task_status'],'UNKNOWN')
        self.assertEqual(latest['recorded_task_status'],'FAIL')
        self.assertEqual(latest['current_contact_review']['status'],'TEST_ONLY')
        code,refused=self.h.cli(['repair-request','--request-id',rid,'--stage','generate','--attempt','2']);self.assertEqual(code,1);self.assertIn('physical task failure',refused['error'])
    def test_unknown_mcp_has_readback_guard_and_no_blind_resend(self):
        rid=self.supported();self.confirm(rid);sent=[]
        async def missing_rpc(flow,name,args):
            sent.append(name)
            if name=='proposal_prepare':raise TimeoutError('TEST_ONLY unknown transport')
            return await self.h.rpc(flow,name,args)
        flow=case_taskflow.TaskFlow.load(rid,runtime_factory=self.h.runtime,rpc=missing_rpc)
        with self.assertRaises(TimeoutError):asyncio.run(flow.generate())
        count=CaseBudget(task_context=case_requests.load_request(rid)).usage()['requests']
        with self.assertRaisesRegex(ValueError,'UNKNOWN'):asyncio.run(flow.generate())
        self.assertEqual(sent,['proposal_prepare']);self.assertEqual(CaseBudget(task_context=case_requests.load_request(rid)).usage()['requests'],count)
        budget=CaseBudget(task_context=case_requests.load_request(rid));usage=budget.usage()
        authority=case_requests.authority_for(case_requests.load_request(rid))
        marker=flow.state()['unknown_marker'];self.assertEqual(marker['status'],'UNKNOWN_NO_BLIND_RESEND');self.assertEqual(marker['error'],'TimeoutError: operation timed out');self.assertNotIn('response',marker)
        raw=json.loads((Path(case_requests.load_request(rid)['root'])/'proposal-unknown.json').read_text(encoding='utf8'))
        self.assertEqual(raw['details']['exception']['type'],'TimeoutError')
        self.assertTrue(raw['details']['messages_omitted']);self.assertFalse(raw['details']['retry_authorized'])
        self.assertNotIn('TEST_ONLY unknown transport',json.dumps(raw))
        with self.assertRaisesRegex(ValueError,'UNKNOWN'):asyncio.run(flow.repair('generate',1))
        self.assertEqual(sent,['proposal_prepare']);self.assertEqual(budget.usage(),usage)
        self.assertEqual(case_requests.authority_for(case_requests.load_request(rid)),authority)
    def test_new_process_selector_binds_service_worker_version_root_and_ledger(self):
        rid=self.supported();self.confirm(rid);ctx=case_requests.load_request(rid)
        code="import json,sys;from pathlib import Path;from week13.src import case_requests as r;r.ROOT=Path(sys.argv[1]);r.REQUESTS_ROOT=r.ROOT/'shared-project/requests';from week13.src import case_paths as p;from week13.src.case_session import open_service;from week13.src.case_worker import WEEK12_ROOT;s=open_service();print(json.dumps(dict(run=str(p.RUN),project_root=str(p.PROJECT_ROOT),db=str(p.PROJECT_DB),ledger=str(p.BUDGET_DB),worker_root=str(WEEK12_ROOT),context_mode=s.context.mode)))"
        env=dict(os.environ,MBSE_V12_REQUEST_ID=rid,PYTHONDONTWRITEBYTECODE='1');result=subprocess.run([sys.executable,'-X','utf8','-B','-c',code,str(self.h.root)],cwd=case_paths.REPO,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf8',timeout=60)
        self.assertEqual(result.returncode,0,result.stderr);actual=json.loads(result.stdout);self.assertEqual(actual['run'],ctx['root']);self.assertEqual(actual['project_root'],ctx['project_root']);self.assertEqual(actual['worker_root'],ctx['project_root']);self.assertEqual(actual['db'],ctx['db_path']);self.assertEqual(actual['ledger'],ctx['runtime_authority']['ledger_path']);self.assertEqual(actual['context_mode'],'TEST_ONLY')

if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--child':
        # case_paths/runtime were already imported with no selector at module
        # import. Explicit TaskFlow context must still route all real runtime
        # journals/ledger operations to this isolated request, not legacy RUN.
        h=Harness(Path(sys.argv[2]),seed=False);use_flat_fixture_transport(h)
        code,result=h.cli([sys.argv[4],'--request-id',sys.argv[3]])
        print(json.dumps(result,ensure_ascii=False));raise SystemExit(code)
    unittest.main()
