"""Local product reliability tests. No real HTTP, worker, compiler or dynamics."""
import asyncio
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from week13.src import case_requests,case_taskflow as tf
from week13.src.case_budget import CaseBudget
from week13.src.case_paths import ROOT,read_json,save_json,sha_file
from week13.src.case_semantic_guard import plan_semantic_validation,require_plan_semantics,protect_explanation
from week13.tests.test_case_agent_requests import Harness
from week13.tests.test_case_flat_plan_protocol import flat

OLD=ROOT/'results/shallow-plan-protocol-20261022-03'
OUT=ROOT/'results/agent-reliability-20261023-01'


def saved(name):
    scenario,action=name.split('-',1)
    return read_json(OLD/('action-flat85_'+scenario+'_20261022_03-'+action+'.json'))['result']


def old_facts():
    context=read_json(OLD/'three-scenario-context-review-rule-correction-e02b28ebc10d6334c329273be213a44afd55fb971308693051aeb320aefde24d.json')
    rows=[]
    for scenario in context['current_scenarios']:
        row=deepcopy(scenario['actual']['execution']['current_summary']);row['parameters']=scenario['parameters'];rows.append(row)
    row=deepcopy(context['saved_150x280']['saved_diagnostic'])
    row['current_contact_review']={key:row[key] for key in ('task_status','completion_status','physical_status')};rows.append(row)
    return rows


class KnownSemanticPatterns(unittest.TestCase):
    def test_real_saved_first_plan_rejected_and_repair_and_next_plan_allowed(self):
        before={str(p):sha_file(p) for p in OLD.glob('action-*.json')}
        first=saved('180_260-plan')['plan'];baseline={'artifact':{'modelica_source':'FtC[i, b] = Ft_budget[i]*FnC[i,b]/max(Fn_sum[i], 1.0e-9);'}}
        report=plan_semantic_validation(first,baseline)
        self.assertEqual(report['status'],'KNOWN_FACT_CONFLICT')
        self.assertTrue(report['baseline_facts']['ftc_positive_denominator_guard_present'])
        self.assertEqual({x['field'] for x in report['issues']},{'design.assumptions[0]'})
        for name in ('180_260-plan-repair-1','300_260-plan'):
            self.assertEqual(require_plan_semantics(saved(name)['plan'],baseline)['status'],'NO_KNOWN_FACT_CONFLICT')
        self.assertEqual({p:sha_file(p) for p in before},before)

    def test_current_frozen_edits_and_negative_and_historical_variants(self):
        plan=saved('180_260-plan-repair-1')['plan']
        bad=['本次修复FtC分母保护。','将接触方程替换为新表达式。','本次调整控制律。','本次延长观察区间。','本次修改车辆质量。','本次提高T_main_max。','不调整质量但修复接触方程。']
        good=['仅修改楼梯高度，车辆与接触保持不变。','基线已修复FtC分母，本次继承，不再次修复。','本次继承基线已修复的FtC分母保护。','本次不修改接触方程。','当前接触模型尚未完整修复。','仅修改stepH/stepD，其余冻结。','接触方程不变，控制律不改。']
        for claim in bad+good:
            packet=deepcopy(plan);packet['design']['summary']=claim
            with self.subTest(claim=claim):self.assertEqual(bool(plan_semantic_validation(packet,{})['issues']),claim in bad)

    def test_real_bad_and_good_explanations_and_residual_correction(self):
        facts=old_facts();before={str(p):sha_file(p) for p in OLD.glob('action-*.json')}
        # The saved v5 diagnostic supplied here has no current source/CSV
        # binding. Its old FAIL cannot be promoted to a current verified fact.
        for name,expected in [('300_260-reopen',False),('300_260-explain',False),('300_260-reopen-review-rule-correction',False)]:
            record=saved(name);result=protect_explanation(record['answer'],record['binding'],extra_executions=facts)
            self.assertEqual(result['raw_answer'],record['answer']);self.assertEqual(result['explanation_validation']['body_displayed'],expected)
            self.assertFalse(result['explanation_validation']['full_semantic_validation'])
            if expected:self.assertIn(record['answer'],result['answer'])
            else:self.assertNotIn(record['answer'],result['answer']);self.assertIn('解释未通过事实核对',result['answer'])
            if 'correction' in name:self.assertTrue({'EXPLICIT_EMPTY_HARD_CONTRADICTIONS_MISREAD','EXCLUSIVE_UNKNOWN_CLAIM_CONTRADICTS_FACTS'}<={x['code'] for x in result['explanation_validation']['issues']})
        self.assertEqual({p:sha_file(p) for p in before},before)

    def test_supported_mass_original_com_load_is_distinct_from_moving_com_or_inertia(self):
        packet=dict(intent='CHANGE',support='SUPPORTED',scope=deepcopy(tf.MASS_SCOPE_PACKET),design=dict(assumptions=[]),defaults=[],unknowns=[],rationale='保持原物理近似')
        good=['新增5kg集中于原参考重心。','新增载荷放在原重心。']
        bad=['本次调整重心位置。','本次增加I_pitch。','本次移动重心。']
        for claim in good+bad:
            packet['design']['summary']=claim
            with self.subTest(claim=claim):self.assertEqual(bool(plan_semantic_validation(packet,{})['issues']),claim in bad)

    def test_core_false_success_and_local_negation(self):
        binding=saved('300_260-explain')['binding']
        bad=['是，300mm通过。','这台车能爬300mm楼梯。','任务不是失败，而是已经通过。','任务不是失败而是已经通过。','本次任务已完成。','task_status=PASS。','physical_status=PASS。']
        good=['不能说这台车能爬300mm楼梯。','300mm不保证通过。','任务未完成，工具通过不能当作任务通过。','不能把task_status=FAIL改写为INVALID_MODEL。','原错误回复声称本次任务已完成；当前任务FAIL。','只解释参数继承与简化限制。']
        for text in bad+good:
            with self.subTest(text=text):self.assertEqual(protect_explanation(text,binding)['explanation_validation']['body_displayed'],text in good)
        explained=protect_explanation('“只有150×280为UNKNOWN”是旧错误，三者物理均UNKNOWN。',binding,extra_executions=old_facts())
        self.assertTrue(explained['explanation_validation']['body_displayed'])

    def test_no_execution_cannot_claim_current_success_but_general_help_is_visible(self):
        binding=dict(request_id='TEST_ONLY_no_result',project_id='p')
        for text in ['本次任务已完成。','本请求仿真已经通过。','当前已仿真通过。']:
            self.assertFalse(protect_explanation(text,binding)['explanation_validation']['body_displayed'])
        for text in ['本次任务尚未执行。','历史本次任务已完成的说法是旧解释。','如果本次任务已完成，则应先读取证据。','楼梯高度会改变地形坐标；请先确认方案。']:
            self.assertTrue(protect_explanation(text,binding)['explanation_validation']['body_displayed'],text)


class OrdinaryFlowReliability(unittest.TestCase):
    def setUp(self):
        OUT.mkdir(parents=True,exist_ok=True)
        self.old=(case_requests.ROOT,case_requests.REQUESTS_ROOT,case_requests.LEGACY_PROJECT_ROOT)
        self.tmp=tempfile.TemporaryDirectory(prefix='g-',dir=OUT)
        self.h=Harness(Path(self.tmp.name));self.rid='T_semantic'
        self.h.bootstrap(self.rid,self.h.project_id,'v000002','把总质量改为92kg，增加部分在原重心，几何控制不变。')
        self.h.authorize(self.rid);self.flow=self.h.flow(self.rid);self.sent=[]
        original=self.h.transport
        def transport(ctx,payload):
            self.sent.append(deepcopy(payload));response=original(ctx,payload)
            calls=response['choices'][0]['message'].get('tool_calls',[])
            if calls and calls[0]['function']['name']=='case_request_plan':
                args=json.loads(calls[0]['function']['arguments']);context=json.loads(payload['messages'][1]['content'])
                if 'bounded_protocol_repair' not in context:args['packet']['design']['assumptions'].append(dict(text='本次唯一改动是修复FtC分母。',origin='model'))
                calls[0]['function']['arguments']=json.dumps(flat(args),ensure_ascii=False)
            return response
        self.h.transport=transport
    def tearDown(self):
        case_requests.ROOT,case_requests.REQUESTS_ROOT,case_requests.LEGACY_PROJECT_ROOT=self.old
        self.tmp.cleanup()

    def test_ordinary_lexical_diagnostic_remains_visible_without_spending_repair(self):
        result=asyncio.run(self.flow.bounded('plan',self.flow.plan));ctx=case_requests.load_request(self.rid)
        self.assertEqual(len(self.sent),1);self.assertTrue(result['requires_local_confirmation'])
        self.assertFalse((self.flow.root/'failures').exists())
        report=result['validation']['known_semantic_validation']
        self.assertEqual(report['status'],'DIAGNOSTIC_WARNINGS');self.assertTrue(report['issues'])
        self.assertTrue(result['validation']['review_warnings'])
        self.assertEqual(CaseBudget(task_context=ctx).usage()['counters'].get('software_repair',0),0)
        self.assertEqual(CaseBudget(task_context=ctx).usage()['requests'],1)
        self.assertFalse((self.flow.root/'generation.json').exists());self.assertFalse(ctx['allow_execution'])
        response=read_json(self.flow.root/'turns/plan/response.json')
        self.assertEqual(result['plan'],tf.parse_tool(response,'case_request_plan','packet'))

    def test_saved_invalid_plan_cannot_confirm_or_execute_or_run_as_valid(self):
        planned=asyncio.run(self.flow.plan());packet=deepcopy(planned['plan'])
        # Lexical warnings cannot bypass a real structured frozen-inertia edit.
        packet['proposed_parameters']['I_pitch']=25
        for row in packet['design']['parameters']:
            if row['name']=='I_pitch':row['value']=25
        ctx=case_requests.save_plan(self.rid,packet);digest=ctx['displayed_digest']
        with self.assertRaises(ValueError):case_requests.confirm_request(self.rid,digest,'CONFIRM')
        self.assertFalse((self.flow.root/'confirmations').exists())
        # The old historical confirmation is constructed only in isolated TEST_ONLY
        # storage, to prove current execution rechecks it before any new binding.
        receipt=dict(channel='TRUSTED_LOCAL_CLI',product_self_approval=False,displayed_digest=digest,request_digest=ctx['request_digest'])
        path=self.flow.root/'confirmations'/('old-'+digest+'.json');save_json(path,receipt)
        raw=read_json(self.flow.root/'task-context.json');raw.update(stage='CONFIRMED',confirmation_path=str(path));save_json(self.flow.root/'task-context.json',raw)
        with self.assertRaises(ValueError):case_requests.require_execution(self.rid)
        with self.assertRaises(ValueError):asyncio.run(self.flow.run())
        self.assertFalse((self.flow.root/'execution-binding.json').exists());self.assertEqual(len(self.sent),1)

    def test_unknown_transport_does_not_become_a_semantic_repair(self):
        def failed(*args):raise TimeoutError('TEST_ONLY unknown provider outcome')
        self.h.transport=failed
        with self.assertRaises(RuntimeError):asyncio.run(self.flow.bounded('plan',self.flow.plan))
        self.assertFalse((self.flow.root/'failures').exists())
        self.assertEqual(CaseBudget(task_context=case_requests.load_request(self.rid)).usage()['requests'],1)

    def test_multiple_lexical_warnings_do_not_trigger_repair_or_expand_authority(self):
        current=self.h.transport
        def still_wrong(ctx,payload):
            response=current(ctx,payload);call=response['choices'][0]['message']['tool_calls'][0]
            args=json.loads(call['function']['arguments'])
            args['design_assumptions'].append(dict(text='本次修复接触方程。',origin='model'))
            call['function']['arguments']=json.dumps(args,ensure_ascii=False)
            return response
        self.h.transport=still_wrong
        result=asyncio.run(self.flow.bounded('plan',self.flow.plan))
        budget=CaseBudget(task_context=case_requests.load_request(self.rid))
        self.assertEqual(budget.usage()['counters'].get('software_repair',0),0)
        self.assertEqual(budget.usage()['requests'],1);self.assertEqual(len(self.sent),1)
        self.assertGreaterEqual(len(result['validation']['review_warnings']),2)
        with self.assertRaises(PermissionError):case_requests.require_execution(self.rid)
        self.assertFalse((self.flow.root/'generation.json').exists())


class SafeReadOnlyPaths(unittest.TestCase):
    def test_ask_review_and_saved_overlay_keep_raw_and_never_show_known_bad_body(self):
        OUT.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='local-semantic-readonly-',dir=OUT) as folder:
            root=Path(folder);record=saved('300_260-reopen');state=deepcopy(record['binding'])
            source='model M parameter Real m=85; parameter Real I_pitch=20; parameter Real T_main_max=60; parameter Real T_nose_max=25; parameter Real stepH=.3; parameter Real stepD=.26; end M;'
            snapshot=dict(deepcopy(state['latest_request_execution']),frozen_spec=dict(artifact=dict(modelica_source=source)),outcome=dict(tool_status='PASS'))
            ctx=dict(request_id='TEST_ONLY_readonly',root=str(root),raw_request='只读解释',baseline_snapshot=dict(frozen_spec=dict(artifact=dict(modelica_source=source))))
            flow=tf.TaskFlow(ctx);flow.state=Mock(side_effect=lambda:deepcopy(state));flow.open=Mock(return_value=SimpleNamespace(_artifact_version=lambda _:deepcopy(snapshot)))
            assistant=dict(role='assistant',content=record['answer'],reasoning_content='TEST_ONLY raw reasoning')
            async def call(*args):return dict(content=record['answer'],tool_calls=[],assistant_message=assistant)
            flow.call=call
            self.assertNotIn('parameters',state['latest_request_execution'])
            dimensions=flow._protect_explanation('300×260已经通过。',state)
            self.assertFalse(dimensions['explanation_validation']['body_displayed'])
            self.assertEqual(dimensions['explanation_validation']['facts'][0]['stair_dimensions'],dict(stepH=.3,stepD=.26))
            self.assertTrue(flow._protect_explanation('300×260尚未通过。',state)['explanation_validation']['body_displayed'])
            with patch('week13.src.case_stage_facts.read_stage_facts',return_value={}):result=asyncio.run(flow.ask('解释当前任务'))
            self.assertFalse(result['explanation_validation']['body_displayed']);self.assertEqual(result['raw_answer'],record['answer'])
            history=read_json(root/'dialogue.json');self.assertEqual(history[0]['assistant_message'],assistant)
            self.assertEqual(history[0]['raw_answer'],record['answer']);self.assertEqual(history[0]['binding'],state)
            original=dict(pipeline=dict(explanation=dict(answer=record['answer'])),state=state)
            result=flow._terminal_explanation_overlay(original)
            self.assertEqual(original['pipeline']['explanation']['answer'],record['answer'])
            self.assertFalse(result['pipeline']['explanation']['explanation_validation']['body_displayed'])
            flow.ctx.update(access_mode='RESULT_REVIEW_ONLY',step_id='explain',review_binding=dict(origin_request_id='TEST_ONLY_origin'))
            flow.refresh=Mock();flow.review_evidence=Mock(return_value={})
            with patch('week13.src.case_stage_facts.read_stage_facts',return_value={}):reviewed=asyncio.run(flow.ask_review('解释当前结果'))
            self.assertFalse(reviewed['explanation_validation']['body_displayed']);self.assertEqual(reviewed['raw_answer'],record['answer'])
            self.assertEqual(read_json(root/'review-answer.json')['raw_answer'],record['answer'])


if __name__=='__main__':unittest.main()
