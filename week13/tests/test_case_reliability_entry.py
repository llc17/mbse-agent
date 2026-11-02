"""Reliability regressions: pinned historical evidence and isolated mock CLI.

No new online request, MCP process, OMC or simulation. Only the old negative
uses real historical dynamics; the positive lifecycle fixtures are TEST_ONLY.
"""
import copy
import io
import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

# Load the installed parser before denying subprocess creation. Its dependency
# queries Windows version on first import; parser checks themselves stay real.
import sysmlpy

from week13.src import case_console as console, case_console_data as data
from week13.src import case_contact_family as family, case_requests, case_scope
from week13.src.case_contact_review import review_snapshot, current_summary
from week13.src.case_console_result import prepare_result
from week13.src.case_paths import ROOT, sha_file, sha_value
from week13.tests import test_case_agent_requests as lifecycle
from week13.tests.test_case_console import FixtureRunner
from week13.tests.test_case_console_result_headline import saved_result
from week13.tests.test_case_stairs import baseline, packet, source_with

OUT=ROOT/'results/agent-reliability-20261023-01'
FIXTURE=ROOT/'tests/fixtures/agent_reliability_original300.json'
ORIGINAL_EVIDENCE={}
CURRENT85_EVIDENCE={}


def original300_readback():
    """Use pinned canonical artifacts directly; never open a Store or budget."""
    fixture=json.loads(FIXTURE.read_text(encoding='utf8'))
    project=ROOT/fixture['project_root']
    base=project/'projects'/fixture['project_id']/'versions'/fixture['version_id']
    protected=[base/name for name in fixture['files']]
    protected += [project/'projects.sqlite',ROOT/'runs/console/selection.json']
    protected += [project/'requests'/fixture['request_id']/name for name in ('task-context.json','dialogue.json','pipeline.json')]
    old_readback=ROOT/'results/contact-validity-fix-20261022-01/integration-v9-readback.json'
    # Saved old evidence supplies extra protection paths; no old ledger is
    # instantiated and the selection's historical value is never restored.
    previous=json.loads(old_readback.read_text(encoding='utf-8-sig'))
    protected += [Path(path) for path in previous['protected_before'] if Path(path).is_file()]
    protected=list(dict.fromkeys(path for path in protected if path.is_file()))
    before={str(path):sha_file(path) for path in protected}
    for name,expected in fixture['files'].items():
        if sha_file(base/name)!=expected:raise AssertionError('original300 pinned artifact drift: '+name)
    manifest=json.loads((base/'artifact-manifest.json').read_text(encoding='utf8'))
    spec=json.loads((base/'spec.json').read_text(encoding='utf8'));outcome=manifest['outcome']
    values=case_scope.modelica_parameters(spec['artifact']['modelica_source'])[0]
    assert {key:values[key] for key in fixture['historical_parameters']}==fixture['historical_parameters']
    assert sha_value(spec)==fixture['spec_digest']
    assert all(manifest['binding'][key]==fixture[key] for key in ('project_id','task_id','spec_digest','source_sha256'))
    snapshot={key:fixture[key] for key in ('project_id','task_id','version_id','spec_digest','source_sha256','manifest_sha256')}
    snapshot.update(frozen_spec=spec,outcome=outcome,record_kind='ARTIFACT_EXECUTION_SNAPSHOT',evidence_status='COMPLETE',recorded_conclusions_verified=True,state='succeeded',**{key:outcome[key] for key in ('tool_status','chain_status','task_status')})
    # The guards enforce that this regression never acquires runtime authority.
    with patch('week13.src.case_budget.CaseBudget.__init__',side_effect=AssertionError('historical budget construction forbidden')),patch('subprocess.Popen',side_effect=AssertionError('process forbidden')),patch('httpx.Client.send',side_effect=AssertionError('HTTP forbidden')),patch('httpx.AsyncClient.send',side_effect=AssertionError('HTTP forbidden')):
        review=review_snapshot(snapshot,project)
    assert outcome['task_status']==fixture['historical_task_status']=='PASS'
    assert review['route_assessment']['task_status']=='PASS'
    assert all(review[key]==value for key,value in fixture['expected_current'].items())
    assert review['contact_validity']['hard_contradictions']
    after={str(path):sha_file(path) for path in protected};assert before==after
    result=dict(request_id=fixture['request_id'],execution=snapshot,state=dict(latest_request_execution=current_summary(dict(snapshot,current_contact_review=review))))
    _,execution,_,lines=prepare_result(result,request_id=fixture['request_id'])
    assert 'INVALID_MODEL' in lines[0] and execution['task_status']=='UNKNOWN'
    report=dict(schema='v12-original300-regression-result-1',scope='READ_ONLY_REAL_OLD_SOURCE_CSV_NEGATIVE',fixture_path=str(FIXTURE),fixture_sha256=sha_file(FIXTURE),binding=review['binding'],historical_parameters=fixture['historical_parameters'],historical_task_status=outcome['task_status'],historical_route_task_status=review['route_assessment']['task_status'],current={key:review[key] for key in fixture['expected_current']},headline=lines[0],hard_contradictions=review['contact_validity']['hard_contradictions'],counters=review['contact_validity']['counters'],protected_file_count=len(before),protected_hashes_unchanged=before==after,protected_before=before,protected_after=after,new_HTTP=0,new_MCP=0,new_compile=0,new_simulation=0,new_budget=0,limits=fixture['limits'])
    ORIGINAL_EVIDENCE.update(report)
    return result,report


class Original300Regression(unittest.TestCase):
    def test_real_old_false_success_is_invalid_without_rerun_or_mutation(self):
        result,report=original300_readback()
        self.assertEqual(report['historical_parameters']['m'],105)
        self.assertEqual(report['current']['task_status'],'UNKNOWN')
        self.assertNotEqual(result['execution']['task_id'],'task_R03')


class InputBoundaryAndDisplay(unittest.TestCase):
    def setUp(self):
        OUT.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(prefix='entry-',dir=OUT)
        self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)

    def capability(self):
        support=dict(family='source-review-bound-stair-contact-v1',modelica_source_sha256='a'*64,family_shape_sha256='b'*64)
        with patch.object(family,'support_for_reviewed_source',return_value=support):return family.reviewed_stair_capability('TEST_ONLY source')

    def test_capability_is_readonly_serializable_and_does_not_claim_success(self):
        value=self.capability();json.dumps(value,allow_nan=False)
        self.assertEqual(value['allowed_scenarios'],[dict(stepH=h,stepD=d) for h,d in family.STAIR_DIAGNOSTIC_SCENARIOS])
        self.assertFalse(value['execution_authorized']);self.assertEqual(value['task_success'],'NOT_GUARANTEED')
        with patch.object(family,'support_for_reviewed_source',return_value=None):self.assertIsNone(family.reviewed_stair_capability('unrecognized'))

    def test_reviewed_baseline_allows_each_diagnostic_point_but_not_arbitrary_positive(self):
        base=baseline();sources=[dict(id='src_new',text='TEST_ONLY injected request'),*base['sources']]
        with patch.object(family,'reviewed_stair_capability',return_value=self.capability()):
            for h,d in family.STAIR_DIAGNOSTIC_SCENARIOS:
                evidence=case_scope.validate_request_plan(packet(base,h,d),base,sources)
                self.assertTrue(evidence['executable'])
            for h,d in ((.2,.26),(.3,.28),(.18,.260001)):
                with self.assertRaisesRegex(case_scope.UnsupportedRequestDesign,'五个离散'):
                    case_scope.validate_request_plan(packet(base,h,d),base,sources)

    def test_actual_reviewed85_baseline_rejects_outside_the_same_recognition_domain(self):
        base=ROOT/'runs/case-projects/case_3b0265f496f84ae9b800/projects/prj_c200bb1e9f2c4d4abc188d043aac75b2/versions/v000005'
        source_path=base/'frozen-model.mo';manifest_path=base/'artifact-manifest.json'
        self.assertEqual(sha_file(source_path),'e615e0ecb55db09ec8e2a8af314d9d9a6b13871f407356218d4e578366a2fa20')
        self.assertEqual(sha_file(manifest_path),'acf5d6efb0ac4f138a494bf25c709d8faf5821f6cc4a9fffc129bfbe20855d42')
        source=source_path.read_text(encoding='utf8');capability=family.reviewed_stair_capability(source)
        self.assertIsNotNone(capability);self.assertEqual(capability['allowed_scenarios'],[dict(stepH=h,stepD=d) for h,d in family.STAIR_DIAGNOSTIC_SCENARIOS])
        spec=json.loads((base/'spec.json').read_text(encoding='utf8'));sources=[dict(id='src_new',text='TEST_ONLY injected request'),*spec['sources']]
        for h,d in family.STAIR_DIAGNOSTIC_SCENARIOS:
            self.assertIsNotNone(family.support_for_reviewed_source(source_with(source,stepH=h,stepD=d)))
            self.assertTrue(case_scope.validate_request_plan(packet(spec,h,d),spec,sources)['executable'])
        self.assertIsNone(family.support_for_reviewed_source(source_with(source,stepH=.2,stepD=.26)))
        with self.assertRaises(case_scope.UnsupportedRequestDesign):case_scope.validate_request_plan(packet(spec,.2,.26),spec,sources)
        CURRENT85_EVIDENCE.update(version_root=str(base),source_sha256=sha_file(source_path),manifest_sha256=sha_file(manifest_path),input_capability=capability)

    def test_old_family_diagnostic_scope_keeps_its_existing_positive_dimension_rule(self):
        base=baseline();sources=[dict(id='src_new',text='TEST_ONLY injected request'),*base['sources']]
        with patch.object(family,'reviewed_stair_capability',return_value=None):
            self.assertTrue(case_scope.validate_request_plan(packet(base,.2,.31),base,sources)['executable'])

    def test_selected_baseline_displays_range_before_new_request(self):
        output=[];ui=console.Console(console_root=self.root/'ui',output=output.append)
        row=dict(version_id='v000005',parameters=dict(m=85,stepH=.15,stepD=.28),input_capability=self.capability())
        ui.catalog=dict(default_baseline=row,baselines=[row]);ui.selection['baseline_version_id']='v000005'
        ui.show_baseline();text='\n'.join(output)
        self.assertIn('150×280、180×260、300×260、150×270、250×260',text);self.assertIn('不代表任何工况能爬过',text)

    def test_new_diagnostic_points_do_not_expand_old_contact_repair_contract(self):
        self.assertEqual(family.SCENARIOS,((.15,.28),(.18,.26),(.3,.26)))
        for path in family.REGISTRY_ROOT.glob('*.json'):
            receipt=family._read_registration(path)
            self.assertEqual(receipt['allowed_scenarios'],[dict(stepH=h,stepD=d) for h,d in family.SCENARIOS])
        base=baseline()
        for h,d in ((.15,.27),(.25,.26)):
            p=packet(base,h,d);p['scope']=copy.deepcopy(family.CONTACT_SCOPE_PACKET)
            with patch.object(family,'contact_authority'),self.assertRaisesRegex(case_scope.UnsupportedRequestDesign,'three approved'):
                family.validate_contact_plan(p,base,{})

    def test_new_diagnostic_points_reject_frozen_vehicle_and_contact_changes(self):
        base=ROOT/'runs/case-projects/case_3b0265f496f84ae9b800/projects/prj_c200bb1e9f2c4d4abc188d043aac75b2/versions/v000005'
        source=(base/'frozen-model.mo').read_text(encoding='utf8')
        spec=json.loads((base/'spec.json').read_text(encoding='utf8'))
        sources=[dict(id='src_new',text='TEST_ONLY injected request'),*spec['sources']]
        for h,d in ((.15,.27),(.25,.26)):
            changed=source_with(source,stepH=h,stepD=d)
            self.assertIsNotNone(family.support_for_reviewed_source(changed))
            for name,value in [('m',86),('I_pitch',21),('T_main_max',61),('T_nose_max',26),('R_main',.128),('k_ground',40001)]:
                with self.subTest(h=h,d=d,name=name):
                    self.assertIsNone(family.support_for_reviewed_source(source_with(changed,**{name:value})))
            for old,new in [('target_speed = 0.4','target_speed = 0.5'),('else 0.0);','else 0.01);')]:
                altered=changed.replace(old,new)
                self.assertNotEqual(altered,changed)
                self.assertIsNone(family.support_for_reviewed_source(altered))
            p=packet(spec,h,d);p['proposed_parameters']['m']=86
            with self.assertRaisesRegex(case_scope.UnsupportedRequestDesign,'freeze'):
                case_scope.validate_request_plan(p,spec,sources)
        for h,d in ((.15,.270001),(.25,.260001),(.2,.26)):
            self.assertIsNone(family.support_for_reviewed_source(source_with(source,stepH=h,stepD=d)))
            with self.assertRaises(case_scope.UnsupportedRequestDesign):
                case_scope.validate_request_plan(packet(spec,h,d),spec,sources)

    def test_classification_keeps_positive_observation_invalid_tool_and_unknown_distinct(self):
        examples=[(saved_result(),'路线已完成'),(saved_result(task_status='FAIL',mission=dict(validity_status='PASS',task_status='FAIL',completion_status='OBSERVATION_INTERVAL_INCOMPLETE')),'观察区间内任务未完成'),(saved_result(task_status='UNKNOWN',mission=dict(validity_status='FAIL',task_status='UNKNOWN',completion_status='INVALID_MODEL')),'INVALID_MODEL'),(saved_result(tool_status='TOOL_FAIL',chain_status='TOOL_FAIL',task_status='UNKNOWN',mission={}),'验证受阻'),(saved_result(mission=dict(validity_status='UNKNOWN',task_status='UNKNOWN')),'结论未确定')]
        for value,expected in examples:
            original=copy.deepcopy(value);self.assertIn(expected,prepare_result(value)[3][0]);self.assertEqual(value,original)
        synthetic=saved_result(test_only=True)
        self.assertIn('TEST_ONLY',prepare_result(synthetic)[3][0])
        history=saved_result();history['state']=dict(reviewed_execution=history.pop('execution'))
        self.assertIn('本次仅只读查看',prepare_result(history)[3][0])

    def test_safe_answer_is_displayed_raw_conflict_and_unverified_history_are_hidden(self):
        output=[];ui=console.Console(console_root=self.root/'ui',output=output.append)
        raw='错误原文：300mm物理爬楼成功。';safe='【事实】模型有效性失败，任务UNKNOWN。解释未通过事实核对。'
        value=saved_result(task_status='UNKNOWN',mission=dict(validity_status='FAIL',task_status='UNKNOWN',completion_status='INVALID_MODEL'))
        value['explanation']=dict(status='PASS',answer=safe,raw_answer=raw,explanation_validation=dict(status='KNOWN_FACT_CONFLICT',body_displayed=False,full_semantic_validation=False,issues=['TEST_ONLY']))
        before=copy.deepcopy(value);ui.show_result(value);text='\n'.join(output)
        self.assertTrue(output[0].startswith('【任务结论】'));self.assertIn('INVALID_MODEL',output[0]);self.assertIn(safe,text);self.assertNotIn(raw,text);self.assertEqual(value,before)
        output.clear();value['explanation']=dict(status='PASS',answer=raw);ui.show_result(value)
        self.assertNotIn(raw,'\n'.join(output));self.assertIn('尚未核对当前事实','\n'.join(output))


class IsolatedOrdinaryEntry(unittest.TestCase):
    def setUp(self):
        self.saved=(case_requests.ROOT,case_requests.REQUESTS_ROOT,case_requests.LEGACY_PROJECT_ROOT)
        # Short temporary workspace stays inside week13; no production state.
        self.tmp=tempfile.TemporaryDirectory(prefix='rel-',dir=ROOT)
        self.addCleanup(self.tmp.cleanup);self.addCleanup(self.restore)
        self.root=Path(self.tmp.name);self.output=[]
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        for target in ('subprocess.Popen','httpx.Client.send','httpx.AsyncClient.send'):
            self.stack.enter_context(patch(target,side_effect=AssertionError('real process/HTTP forbidden in local fixture')))
        original_init=lifecycle.CaseBudget.__init__
        def guarded_init(budget,ledger_path=None,*,task_context=None):
            if not task_context or not Path(task_context['root']).resolve().is_relative_to(self.root.resolve()):raise AssertionError('real/old CaseBudget forbidden')
            return original_init(budget,ledger_path,task_context=task_context)
        self.stack.enter_context(patch.object(lifecycle.CaseBudget,'__init__',guarded_init))
        finish=lifecycle.finish_fixture
        with patch.object(lifecycle,'finish_fixture',lambda svc,task,root,task_status='PASS':finish(svc,task,root,task_status)):
            self.h=lifecycle.Harness(self.root/'fixture')
        original_transport=self.h.transport
        def current_profile_transport(ctx,payload):
            if not payload.get('tools'):
                return dict(id='local_mock_reply',model='deepseek-flash',choices=[dict(message=dict(content='TEST_ONLY 本次仅合成执行，任务FAIL；不能证明真实车辆或物理能力。',reasoning_content='isolated mock transport'),finish_reason='stop')],usage=dict(prompt_tokens=20,completion_tokens=10,total_tokens=30))
            response=original_transport(ctx,payload)
            tool=(payload.get('tools') or [{}])[0].get('function',{})
            if tool.get('name')=='case_request_plan' and 'design_state' in tool.get('parameters',{}).get('properties',{}):
                from week13.tests.test_case_flat_plan_protocol import flat
                call=response['choices'][0]['message']['tool_calls'][0]
                call['function']['arguments']=json.dumps(flat(json.loads(call['function']['arguments'])),ensure_ascii=False)
            elif tool.get('name')=='proposal_prepare':
                call=response['choices'][0]['message']['tool_calls'][0]
                args=json.loads(call['function']['arguments'])
                args['spec']['sources']=[dict(id=row['id']) for row in args['spec']['sources']]
                call['function']['arguments']=json.dumps(args,ensure_ascii=False)
            return response
        self.h.transport=current_profile_transport
        self.runner=FixtureRunner(self.h)

    def restore(self):
        case_requests.ROOT,case_requests.REQUESTS_ROOT,case_requests.LEGACY_PROJECT_ROOT=self.saved

    def test_ordinary_console_autoplans_confirms_runs_results_asks_and_readonly_reopens(self):
        ui=console.Console(self.runner,console_root=self.root/'ui',input_fn=lambda _: '确认预算',output=self.output.append)
        ui.start();ui.handle('把总质量改为92kg，增加部分在原重心，几何控制不变。')
        self.assertEqual(ui.ctx['stage'],'PROPOSED')
        self.assertIn('plan-request',[action for action,_ in self.runner.calls])
        self.assertNotIn('rejectplan',[action for action,_ in self.runner.calls])
        self.assertNotIn('run-request',[action for action,_ in self.runner.calls])
        ui.handle('确认执行');self.assertTrue(ui.ctx['terminal_saved'])
        count=len(self.runner.calls);ui.handle('/result');self.assertEqual(self.runner.calls[-1][0],'request-result')
        self.assertFalse(any(action=='run-request' for action,_ in self.runner.calls[count:]))
        ui.handle('为何未完成？');self.assertEqual(self.runner.calls[-1][0],'request-ask')
        rid=ui.ctx['request_id'];ctx=case_requests.load_request(rid)
        before={str(p):sha_file(p) for p in (Path(ctx['project_root'])/'projects').rglob('*') if p.is_file()}
        current=console.Console(self.runner,console_root=self.root/'ui',output=self.output.append)
        calls=len(self.runner.calls);current.start()
        self.assertEqual(current.ctx['request_id'],rid);self.assertEqual(len(self.runner.calls),calls)
        self.assertEqual(before,{str(Path(p)):sha_file(Path(p)) for p in before})
        self.assertIn('TEST_ONLY','\n'.join(self.output))
        self.assertFalse(any('物理爬楼成功' in line for line in self.output))

    def test_cli_api_lifecycle_has_real_local_store_with_mock_product_and_no_launch(self):
        rid='entry_api';raw='把总质量改为92kg，增加部分在原重心，几何控制不变。'
        code,value=self.h.cli(['request','--request-id',rid,'--project-id',self.h.project_id,'--baseline-version','v000002','--requirement',raw]);self.assertEqual(code,0,value)
        code,value=self.h.cli(['run-request','--request-id',rid]);self.assertEqual(code,1)
        self.h.authorize(rid);code,plan=self.h.cli(['plan-request','--request-id',rid]);self.assertEqual(code,0,plan)
        code,waiting=self.h.cli(['run-request','--request-id',rid]);self.assertEqual(waiting['stage'],'AWAITING_CONFIRMATION_OR_EXECUTION_AUTHORITY')
        code,value=self.h.cli(['confirm-request','--request-id',rid,'--displayed-digest',plan['displayed_digest'],'--approval-text','CONFIRM']);self.assertEqual(code,0,value)
        code,value=self.h.cli(['run-request','--request-id',rid]);self.assertEqual(code,0,value)
        execution=value['state']['latest_request_execution'];self.assertTrue(execution.get('test_only'))
        ctx=case_requests.load_request(rid);version_root=Path(ctx['project_root'])/'projects'/ctx['project_id']/'versions'/execution['version_id']
        protected={str(p):sha_file(p) for p in version_root.rglob('*') if p.is_file()}
        for action in ('request-result','request-ask','request-reopen'):
            args=[action,'--request-id',rid]+(['--question','TEST_ONLY 解释当前证据'] if action=='request-ask' else [])
            code,value=self.h.cli(args);self.assertEqual(code,0,value)
            if action!='request-result':
                self.assertIn('explanation_validation',value);self.assertIn('raw_answer',value)
        self.assertEqual(protected,{p:sha_file(Path(p)) for p in protected})


if __name__=='__main__':
    if '--report' in __import__('sys').argv:
        suite=unittest.defaultTestLoader.loadTestsFromModule(__import__('sys').modules[__name__])
        stream=io.StringIO();start=datetime.now(timezone.utc)
        result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
        OUT.mkdir(parents=True,exist_ok=True)
        (OUT/'entry-regression-tests.txt').write_text(stream.getvalue(),encoding='utf8')
        (OUT/'entry-regression-original300.json').write_text(json.dumps(ORIGINAL_EVIDENCE,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        report=dict(schema='v12-reliability-entry-regression-report-1',started_at_utc=start.isoformat(),ended_at_utc=datetime.now(timezone.utc).isoformat(),tests_run=result.testsRun,failures=len(result.failures),errors=len(result.errors),local_status='PASS' if result.wasSuccessful() else 'FAIL',test_log=str(OUT/'entry-regression-tests.txt'),original300_report=str(OUT/'entry-regression-original300.json'),current85_readonly_capability=CURRENT85_EVIDENCE,online_HTTP=0,real_MCP=0,OMC=0,new_simulation=0,new_real_budget=0,test_mode='LOCAL_MOCK_PRODUCT_AND_EXECUTION_ONLY',remaining_real_validation=['普通CLI实际HTTP方案/双源码/真实MCP/OMC/仿真/独立检查/解释、追问和独立进程重开尚需新有限执行授权。'],limits=['原300mm负例为真实历史105kg旧源码/CSV；当前85kg车辆冻结，未新增仿真。','有效完成展示只用合成TEST_ONLY分类fixture，不证明新车辆PASS。','只读重开为本地临时Store，非新在线/MCP验证。'])
        (OUT/'entry-regression-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        print(json.dumps(report,ensure_ascii=False,indent=2));raise SystemExit(0 if result.wasSuccessful() else 1)
    unittest.main()
