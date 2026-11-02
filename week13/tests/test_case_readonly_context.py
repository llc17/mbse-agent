"""Read-only closeout guards on isolated real Store and budget fixtures."""
import asyncio,copy,json,hashlib,tempfile,unittest
from pathlib import Path
from week13.tests.test_case_agent_requests import Harness
from week13.src import case_requests,case_paths
from week13.src.case_taskflow import bootstrap_review,TaskFlow,normalize_empty_nonexecutable_defaults
from week13.src.case_budget import CaseBudget,connection

class ReadonlyContext(unittest.TestCase):
    def setUp(self):
        self.saved=(case_requests.ROOT,case_requests.REQUESTS_ROOT,case_requests.LEGACY_PROJECT_ROOT)
        self.temp=tempfile.TemporaryDirectory(dir=case_paths.ROOT/'results');self.h=Harness(Path(self.temp.name)/'fixture');h=self.h
        h.bootstrap('origin_fixture',h.project_id,'v000002','把总质量改为92kg，增加部分在原重心，几何控制不变。');h.authorize('origin_fixture')
        flow=h.flow('origin_fixture');p=asyncio.run(flow.plan());flow.confirm(p['displayed_digest'],'CONFIRM');asyncio.run(flow.run())
        origin=case_requests.load_request('origin_fixture');(Path(origin['root'])/'pipeline-partial.json').write_text(json.dumps(dict(explanation=dict(status='UNKNOWN',request_id='fixture_old_unknown'))),encoding='utf8')
        self.steps={key:'review_'+key for key in ('explain','reopen','negative_mass','negative_geometry')}
        for step,rid in self.steps.items():bootstrap_review(rid,'origin_fixture','v000003','TEST_ONLY readonly request',h.root/'runs/review/contexts'/rid,step)
        caps=dict(http_requests=6,product_cny=35,total_tokens=6000000,total_output_tokens=2359296,compile=0,simulation=0,candidates=0,software_repair_rounds=2,parameter_revision_rounds=0,observation_extensions=0)
        controls=json.loads((h.root/'configs/case_runtime.json').read_text());controls['read_timeout_s']=600
        from datetime import datetime,timezone,timedelta
        for i,rid in enumerate(self.steps.values()):case_requests.authorize_budget(rid,'review_budget','CONFIRM',execution=False,test_only=True,reuse=i>0,**({} if i else dict(caps=caps,hard_deadline_utc=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),model_controls=controls,readonly_steps=self.steps)))
    def tearDown(self):
        case_requests.ROOT,case_requests.REQUESTS_ROOT,case_requests.LEGACY_PROJECT_ROOT=self.saved;self.temp.cleanup()
    def budget(self,step):return CaseBudget(task_context=case_requests.load_request(self.steps[step]))
    def dispatch(self,step,rid,unknown=False,proof=True,rejected=False,partial=False):
        b=self.budget(step);b.reserve_http(rid,'fixturehash','explanation',b.authority['model_controls']);b.launch_http(rid,fixture=True)
        if unknown:
            if proof:
                ctx=case_requests.load_request(self.steps[step]);p=Path(ctx['root'])/'http'/case_paths.sha_value(rid)/'process-exit.json';p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(dict(request_id=rid,fixture_transport=True,fixture_call_returned=True,process_launched=False,local_process_terminated=False)),encoding='utf8')
            b.finish_http(rid,error='ReadTimeout',uncertain=True,**(dict(tokens=17,output=7,cost=.001) if partial else {}))
        else:b.finish_http(rid,response=dict(fixture=True),tokens=50,output=20,cost=.0001,error='known schema error' if rejected else None)
        return b
    def test_unknown_steps_keep_reserve_but_leave_all_four_and_two_known_corrections(self):
        first=self.dispatch('explain','explain_first',unknown=True,partial=True);self.dispatch('reopen','reopen_first',unknown=True)
        with connection(first.path) as db:
            row=db.execute('SELECT actual_tokens,actual_output,actual_cost FROM requests WHERE request_id=?',('explain_first',)).fetchone()
        self.assertEqual(tuple(row),(None,None,None))
        ctx=case_requests.load_request(self.steps['explain']);self.assertTrue((Path(ctx['root'])/'http'/case_paths.sha_value('explain_first')/'partial-usage-observation.json').exists())
        with self.assertRaises(RuntimeError):self.budget('explain').reserve_http('different_id','fixturehash','explanation',self.budget('explain').authority['model_controls'])
        self.dispatch('negative_mass','negative_first',rejected=True);self.dispatch('negative_geometry','geometry_first')
        b=self.budget('negative_mass');b.consume('software_repair','negative_fix');self.dispatch('negative_mass','negative_fix')
        ctx=case_requests.load_request(self.steps['negative_geometry']);p=Path(ctx['root'])/'failures/plan-0.json';p.parent.mkdir();p.write_text(json.dumps(dict(repairable=True,response=dict(fixture=True))),encoding='utf8')
        b=self.budget('negative_geometry');b.consume('software_repair','geometry_fix');self.dispatch('negative_geometry','geometry_fix')
        usage=b.usage();self.assertEqual(usage['requests'],6);self.assertEqual(usage['unknown_requests'],2);self.assertIsNone(usage['frozen']);self.assertGreaterEqual(usage['cost_cny_reserved_or_actual'],2*5.24288)
    def test_missing_exit_stops_package_and_readonly_cannot_confirm_or_execute(self):
        self.dispatch('explain','unknown_no_exit',unknown=True,proof=False)
        with self.assertRaises(RuntimeError):self.budget('reopen').reserve_http('reopen','fixturehash','reopen',self.budget('reopen').authority['model_controls'])
        rid=self.steps['negative_mass']
        with self.assertRaises(PermissionError):case_requests.confirm_request(rid,'fake','CONFIRM')
        with self.assertRaises(PermissionError):case_requests.require_execution(rid)
        authority=copy.deepcopy(self.budget('explain').authority);authority['caps']['compile']=1
        with self.assertRaises(PermissionError):case_requests._validate_authority(authority)
    def test_compact_review_reads_bound_existing_execution_and_keeps_original_store(self):
        before=hashlib.sha256(self.h.db.read_bytes()).hexdigest();seen=[]
        def transport(ctx,payload):
            seen.append(payload)
            return dict(id='fixture_review',model='deepseek-flash',choices=[dict(message=dict(content='TEST_ONLY bound result review',reasoning_content='fixture protocol'),finish_reason='stop')],usage=dict(prompt_tokens=20,completion_tokens=10,total_tokens=30))
        original=self.h.transport;self.h.transport=transport
        flow=TaskFlow.load(self.steps['explain'],runtime_factory=self.h.runtime);answer=asyncio.run(flow.ask('解释已有结果'))
        context=json.loads(seen[0]['messages'][1]['content']);self.assertEqual(context['bound_existing_execution']['reference']['version_id'],'v000003');self.assertNotIn('baseline_snapshot',context);self.assertNotIn('modelica_source',json.dumps(context));self.assertFalse(answer['new_physical_execution']);self.assertEqual(before,hashlib.sha256(self.h.db.read_bytes()).hexdigest())
        ctx=case_requests.load_request(self.steps['reopen']);path=Path(ctx['root'])/'baseline-snapshot.json';changed=json.loads(path.read_text());changed['spec_digest']='bad';path.write_text(json.dumps(changed),encoding='utf8')
        with self.assertRaises(ValueError):case_requests.load_request(self.steps['reopen'])
    def test_known_empty_defaults_revalidation_is_lossless_and_zero_http(self):
        ctx=case_requests.load_request(self.steps['negative_mass']);root=Path(ctx['root']);rid='case_plan_'+ctx['request_id']
        packet=dict(intent='CHANGE',support='NEEDS_INPUT',missing=['设备载荷构成未知'],unsupported=[],requirements=[],design={},proposed_parameters={},defaults={},unknowns=['新总质量未知'],rationale='总质量不等于设备载荷',scope={})
        response=dict(tool_calls=[dict(function=dict(name='case_request_plan',arguments=json.dumps(dict(packet=packet),ensure_ascii=False)))],content='',finish_reason='tool_calls')
        b=self.budget('negative_mass');b.reserve_http(rid,'fixturehash','planning',b.authority['model_controls']);b.launch_http(rid,fixture=True);b.finish_http(rid,response=response,tokens=50,output=20,cost=.0001)
        folder=root/'turns/plan';folder.mkdir(parents=True)
        (folder/'request.json').write_text(json.dumps(dict(request_id=rid,request_digest=ctx['request_digest'])),encoding='utf8');(folder/'response.json').write_text(json.dumps(response),encoding='utf8')
        path=root/'failures/plan-0.json';path.parent.mkdir();path.write_text(json.dumps(dict(stage='plan',attempt=0,error='ValueError: invalid defaults',repairable=True,response=response)),encoding='utf8')
        before=hashlib.sha256(self.h.db.read_bytes()).hexdigest();saved=(folder/'response.json').read_bytes();usage=b.usage()
        result=TaskFlow.load(ctx['request_id']).revalidate_plan(0)
        expected=copy.deepcopy(packet);expected['defaults']=[]
        self.assertEqual(result['plan'],expected);self.assertEqual(result['product_http_added'],0);self.assertFalse(result['validation']['executable']);self.assertEqual(usage,b.usage());self.assertEqual(saved,(folder/'response.json').read_bytes());self.assertEqual(before,hashlib.sha256(self.h.db.read_bytes()).hexdigest())
        nonempty=copy.deepcopy(packet);nonempty['defaults']={'solver':'DASSL'};self.assertEqual(normalize_empty_nonexecutable_defaults(nonempty),nonempty)
        executable=copy.deepcopy(packet);executable['proposed_parameters']={'m':123}
        with self.assertRaises(ValueError):normalize_empty_nonexecutable_defaults(executable)

if __name__=='__main__':unittest.main()
