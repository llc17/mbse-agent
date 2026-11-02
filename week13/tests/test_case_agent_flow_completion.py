"""Finite flow-completion regressions; local replay and injected lifecycle only."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from week13.src import case_requests,case_taskflow as tf,case_console,case_console_data
from week13.src.case_budget import CaseBudget
from week13.src.case_paths import ROOT,read_json,sha_value,sha_file
from week13.src.case_scope import validate_request_plan,check_delta,UnsupportedRequestDesign
from week13.src.case_console_result import prepare_result
from week13.src.case_semantic_guard import baseline_source_references,protect_explanation,require_plan_semantics,plan_semantic_validation
from week13.tests.test_case_agent_requests import Harness,synthetic_spec
from week13.tests.test_case_flat_plan_protocol import flat

OUT=ROOT/'results/agent-flow-completion-20261025-02/implementation'
OLD=ROOT/'results/agent-flow-repair-20261025-01'
PROJECT=ROOT/'runs/case-projects/case_9937b85ad1f4408e8a97'
A=PROJECT/'requests/console_20261025T052634_0e4b84ca9c'
B=PROJECT/'requests/console_20261025T052707_f468a06923'


def trusted_context():
    ctx=read_json(A/'task-context.json');ctx['baseline_snapshot']=read_json(A/'baseline-snapshot.json')
    return ctx


def actual_explanation():
    return read_json(OLD/'live/run-a-public-01-stdout.json')['explanation']


def binding():
    return actual_explanation()['current_fact_binding']


def packet(mass=102.875):
    values=dict(m=mass,I_pitch=20,T_main_max=60,T_nose_max=25)
    return dict(intent='CHANGE',support='SUPPORTED',missing=[],unsupported=[],
        requirements=[dict(text='TEST_ONLY new requirement',source_id='new',source_text='TEST_ONLY new requirement')],
        design=dict(summary='本次计划调整控制律。',assumptions=[],parameters=[dict(name=k,value=v,unit='1',origin='model',basis='TEST_ONLY') for k,v in values.items()]),
        proposed_parameters=values,defaults=['原重心理想点载，惯量继承。'],unknowns=[],rationale='TEST_ONLY',scope=deepcopy(tf.MASS_SCOPE_PACKET))


class RealReplyReplay(unittest.TestCase):
    def test_B_original_structured_plan_becomes_visible_warning_without_rewrite(self):
        failure=read_json(B/'failures/plan-0.json');p=tf.parse_tool(failure['response'],'case_request_plan','packet')
        ctx=read_json(B/'task-context.json');baseline=read_json(B/'baseline-snapshot.json')['frozen_spec'];original=deepcopy(p)
        self.assertTrue(validate_request_plan(p,baseline,ctx['sources'])['executable'])
        lexical=plan_semantic_validation(p,baseline);report=require_plan_semantics(p,baseline)
        self.assertEqual(len(lexical['issues']),3)
        self.assertEqual(report['issues'],lexical['issues']);self.assertEqual(report['warnings'],lexical['issues'])
        self.assertEqual(report['blocking_issues'],[]);self.assertEqual(report['decision'],'ALLOW_WITH_DIAGNOSTIC_WARNINGS')
        self.assertEqual(p,original);self.assertEqual(p['proposed_parameters'],dict(m=90,I_pitch=20,T_main_max=60,T_nose_max=25))
        self.assertIn('known plan semantic conflict',failure['error'])

    def test_A_automatic_and_both_readonly_original_answers_are_visible(self):
        references=baseline_source_references(trusted_context());records=[actual_explanation(),*read_json(A/'dialogue.json')]
        for record in records:
            with self.subTest(answer_sha=sha_value(record['raw_answer'])):
                result=protect_explanation(record['raw_answer'],record.get('current_fact_binding') or record['binding'],source_references=references)
                self.assertTrue(result['explanation_validation']['body_displayed'],result['explanation_validation']['issues'])
                self.assertEqual(result['raw_answer'],record['raw_answer'])
                self.assertNotIn('v000000',result['explanation_validation']['known_execution_versions'])
                self.assertEqual(result['explanation_validation']['known_source_versions'],['v000000'])
                current=result['explanation_validation']['facts'][0]
                self.assertEqual((current['tool_status'],current['validity_status'],current['task_status']),('PASS','FAIL','UNKNOWN'))


class ReferenceIdentity(unittest.TestCase):
    def test_imported_source_is_not_promoted_to_execution_by_taskflow(self):
        ctx=trusted_context();state=deepcopy(binding());state['versions']=[dict(version_id='v000000')]
        imported=ctx['baseline_snapshot']
        snapshot=lambda version:deepcopy(imported) if version=='v000000' else (_ for _ in ()).throw(KeyError(version))
        flow=tf.TaskFlow(ctx);flow.open=lambda **kw:SimpleNamespace(_artifact_version=snapshot,project_root=PROJECT)
        with patch.object(tf,'with_review',side_effect=AssertionError('imported source must never receive execution review')):
            report=flow._protect_explanation('原始 v000000 是导入参考，未运行。',state)['explanation_validation']
        self.assertTrue(report['body_displayed'],report['issues'])
        self.assertNotIn('v000000',report['known_execution_versions'])
        self.assertEqual(report['known_source_versions'],['v000000'])

    def test_source_identity_and_each_digest_mismatch_are_rejected(self):
        original=trusted_context()
        for kind in ('project','version','snapshot','spec','source','manifest'):
            ctx=deepcopy(original)
            if kind=='project':ctx['baseline_ref']['project_id']='TEST_ONLY_other_project'
            elif kind=='version':ctx['baseline_ref']['version_id']='v000002'
            elif kind=='snapshot':ctx['baseline_snapshot_sha256']='0'*64
            else:
                key={'spec':'spec_digest','source':'source_sha256','manifest':'manifest_sha256'}[kind]
                ctx['baseline_ref'][key]='0'*64
            with self.subTest(kind=kind),self.assertRaises(ValueError):baseline_source_references(ctx)
        ref=baseline_source_references(original)[0];ref['project_id']='TEST_ONLY_other_project';ref['baseline_ref']['project_id']=ref['project_id']
        result=protect_explanation('v000000 是基线来源。',binding(),source_references=[ref])
        self.assertFalse(result['explanation_validation']['body_displayed'])
        self.assertIn('CURRENT_SOURCE_BINDING_CONFLICT',{x['code'] for x in result['explanation_validation']['issues']})

    def test_baseline_source_cannot_inherit_current_PASS_or_invent_results(self):
        references=baseline_source_references(trusted_context())
        bad=['v000000 task_status=PASS。','v000000 tool_status=PASS。','v000000 任务完成：PASS。',
             'v000000 仿真已经通过。','v000000 已经完成任务。','v000000 task_status=FAIL。',
             'v000000 task_status=PASS；v000001 task_status=UNKNOWN。']
        for text in bad:
            with self.subTest(text=text):
                report=protect_explanation(text,binding(),source_references=references)['explanation_validation']
                self.assertFalse(report['body_displayed']);self.assertIn('SOURCE_REFERENCE_HAS_NO_BOUND_EXECUTION',{x['code'] for x in report['issues']})
        for text in ['v000000 是导入源码来源，没有本版本运行CSV。','v000000 task_status=NOT_RUN。','v000000 任务完成性不能判定。']:
            with self.subTest(text=text):self.assertTrue(protect_explanation(text,binding(),source_references=references)['explanation_validation']['body_displayed'])


class FiniteStatusSpans(unittest.TestCase):
    def checked(self,text,allowed):
        report=protect_explanation(text,binding())['explanation_validation']
        self.assertEqual(report['body_displayed'],allowed,(text,report['issues']))
        return report

    def test_unknown_status_labels_are_topics_not_success(self):
        for value in ('UNKNOWN','未知','不能判定','无法判断','不可判定'):
            for label in ('任务完成：','任务完成：当前为 ','任务完成性','任务状态为 '):
                if label=='任务完成性' and value in ('UNKNOWN','未知'):label+='：'
                with self.subTest(label=label,value=value):self.checked(label+value+'。',True)
        for text in ['当前为UNKNOWN。','任务完成：UNKNOWN；工具状态：PASS。','任务完成情况：未知。','task_status = UNKNOWN。']:
            self.checked(text,True)

    def test_non_success_status_values_still_must_match_execution(self):
        row=deepcopy(binding()['latest_request_execution'])
        for value in ('FAIL','NOT_RUN','UNKNOWN'):
            changed=dict(project_id=row['project_id'],latest_request_execution=deepcopy(row));review=changed['latest_request_execution']['current_contact_review']
            review.update(status='PASS',validity_status='PASS',task_status=value,completion_status='OBSERVATION_INTERVAL_INCOMPLETE' if value=='FAIL' else value)
            # Keep the actual binding and prove these labels do not assert success.
            report=protect_explanation('任务完成：'+value+'。',changed)['explanation_validation']
            self.assertNotIn('UNSUPPORTED_TASK_SUCCESS',{x['code'] for x in report['issues']})
        self.checked('任务完成：FAIL。',False)
        self.checked('任务完成：PASS。',False)

    def test_consumption_is_local_and_later_real_success_is_still_rejected(self):
        for tail in ('但已经完成任务','但是任务已完成','不过本车已通过','；task_status=PASS','，车辆已经成功'):
            for head in ('任务完成：UNKNOWN','任务完成性不能判定'):
                with self.subTest(head=head,tail=tail):self.checked(head+tail+'。',False)
        for count in (7,31,124):self.checked('已完成 '+str(count)+' 级。',False)


class ConsoleSourceProjection(unittest.TestCase):
    def protected(self):
        state=binding();refs=baseline_source_references(trusted_context());raw=actual_explanation()['raw_answer']
        protected=protect_explanation(raw,state,source_references=refs)
        return dict(protected,binding=state,current_fact_binding=state,current_source_references=refs)

    def test_result_uses_only_fresh_top_level_source_projection(self):
        protected=self.protected();fresh=dict(state=protected['binding'],explanation=protected,
            current_fact_binding=protected['current_fact_binding'],current_source_references=protected['current_source_references'])
        view=prepare_result(fresh,request_id=protected['binding']['request_id'])[0]
        self.assertTrue(view['explanation']['explanation_validation']['body_displayed'])
        historical=dict(state=protected['binding'],explanation=protected)
        historical['pipeline']=dict(current_fact_binding=protected['current_fact_binding'],current_source_references=protected['current_source_references'],explanation=protected)
        view=prepare_result(historical,request_id=protected['binding']['request_id'])[0]
        self.assertFalse(view['explanation']['explanation_validation']['body_displayed'])
        self.assertIn('UNBOUND_VERSION_REFERENCE',{x['code'] for x in view['explanation']['explanation_validation']['issues']})

    def test_console_ask_carries_fresh_source_projection_and_preserves_UNKNOWN_gate(self):
        protected=self.protected();OUT.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='c-',dir=OUT) as folder:
            ctx=dict(request_id=protected['binding']['request_id'],project_id=protected['binding']['project_id'],
                budget=dict(blocked=None,permissions=dict(execution=True)),plan=packet(),execution_state='UNCERTAIN')
            calls=[];runner=SimpleNamespace(data=lambda *a,**k:deepcopy(ctx),call=lambda action,**k:(calls.append(action) or deepcopy(protected)))
            out=[];ui=case_console.Console(runner,console_root=Path(folder),output=out.append);ui.ctx=ctx;ui.catalog=dict(project_id=ctx['project_id']);ui.selection=dict(request_id=ctx['request_id'])
            ui.ask('TEST_ONLY explain')
            self.assertIn(protected['raw_answer'],'\n'.join(out));self.assertNotIn('解释未通过事实核对','\n'.join(out))
            ui.confirm_execute();self.assertEqual(calls,['request-ask'])
            self.assertIn('任务仍在运行或结果UNKNOWN','\n'.join(out))


class HardGatesAndDisplay(unittest.TestCase):
    def test_diagnostics_never_override_parameters_sources_scope_or_actual_tokens(self):
        src=dict(id='new',text='TEST_ONLY new requirement');base=synthetic_spec(src,85);p=packet()
        self.assertTrue(require_plan_semantics(p,base)['warnings'])
        self.assertTrue(validate_request_plan(p,base,[src])['executable'])
        for key,value in (('m',84),('I_pitch',27),('T_main_max',800)):
            bad=deepcopy(p);bad['proposed_parameters'][key]=value
            for item in bad['design']['parameters']:
                if item['name']==key:item['value']=value
            with self.subTest(key=key),self.assertRaises(UnsupportedRequestDesign):validate_request_plan(bad,base,[src])
        bad=deepcopy(p);bad['requirements'][0]['source_text']='TEST_ONLY missing source substring'
        with self.assertRaises(ValueError):validate_request_plan(bad,base,[src])
        bad=deepcopy(p);bad['scope']['frozen']='NONE'
        with self.assertRaises(UnsupportedRequestDesign):validate_request_plan(bad,base,[src])
        ctx=dict(project_id='TEST_ONLY',baseline_ref={},baseline_snapshot=dict(frozen_spec=base),plan=p,sources=[src]);candidate=deepcopy(base)
        candidate['baseline_ref']={};candidate['artifact']['modelica_source']=candidate['artifact']['modelica_source'].replace('der(signal)=0','der(signal)=7')
        svc=SimpleNamespace(_current_project=lambda:dict(project_id='TEST_ONLY'))
        with patch('week13.src.inherited.artifact_baseline.selection',return_value={}),patch('week13.src.inherited.artifact_baseline.resolve',return_value=ctx['baseline_snapshot']):
            with self.assertRaisesRegex(ValueError,'frozen physics'):check_delta(svc,candidate,task_context=ctx)

    def test_warning_and_structured_difference_are_visible_before_confirmation(self):
        ctx=read_json(B/'task-context.json');ctx['baseline_snapshot']=read_json(B/'baseline-snapshot.json')
        p=tf.parse_tool(read_json(B/'failures/plan-0.json')['response'],'case_request_plan','packet');ctx['plan']=p
        facts=case_console_data.verified_source_facts(ctx);warnings=case_console_data.plan_review_warnings(ctx,facts)
        self.assertEqual(len([x for x in warnings if '文字诊断告警' in x]),3)
        projected=dict(ctx,baseline_parameters=tf.modelica_parameters(ctx['baseline_snapshot']['frozen_spec']['artifact']['modelica_source'])[0],verified_source_facts=facts,plan_review_warnings=warnings,
            budget=dict(blocked=None,permissions=dict(execution=True)))
        output=[];ui=case_console.Console(SimpleNamespace(),console_root=OUT/'TEST_ONLY-no-write-console',output=output.append)
        ui.ctx=projected;ui.selection={};ui.catalog={};ui.show_plan(p)
        body='\n'.join(output)
        self.assertIn('结构化允许差异范围',body);self.assertIn('85',body);self.assertIn('90',body)
        self.assertIn('文字诊断告警',body);self.assertLess(body.index('文字诊断告警'),body.index('确认执行'))


class WarningLifecycle(unittest.TestCase):
    def test_warning_survives_plan_confirmation_generation_and_execution_rechecks(self):
        saved=(case_requests.ROOT,case_requests.REQUESTS_ROOT,case_requests.LEGACY_PROJECT_ROOT)
        OUT.mkdir(parents=True,exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix='t-',dir=OUT) as folder:
                h=Harness(Path(folder));rid='T'
                h.bootstrap(rid,h.project_id,'v000002','把总质量改为92kg，增加部分在原重心，几何控制不变。');h.authorize(rid)
                original=h.transport
                def transport(ctx,payload):
                    result=original(ctx,payload);calls=result['choices'][0]['message'].get('tool_calls',[])
                    if calls:
                        call=calls[0]['function'];args=json.loads(call['arguments'])
                        if call['name']=='case_request_plan':
                            args['packet']['design']['summary']='本次增加质量（冻结控制），并计划修改控制律。'
                            args=flat(args)
                        elif call['name']=='proposal_prepare':args['spec']['sources']=[dict(id=x['id']) for x in ctx['sources']]
                        call['arguments']=json.dumps(args,ensure_ascii=False)
                    return result
                h.transport=transport;flow=h.flow(rid);planned=asyncio.run(flow.plan())
                self.assertTrue(planned['validation']['review_warnings']);self.assertFalse((flow.root/'failures').exists())
                with self.assertRaises(PermissionError):case_requests.require_execution(rid)
                case_requests.confirm_request(rid,planned['displayed_digest'],'CONFIRM',approval_basis='TEST_ONLY explicit fixture approval',human_reviewed_displayed_plan=False)
                self.assertEqual(case_requests.require_execution(rid)['request_id'],rid)
                result=asyncio.run(flow.run());self.assertIsNotNone(result['state']['latest_request_execution'])
                self.assertEqual(CaseBudget(task_context=case_requests.load_request(rid)).usage()['counters'].get('software_repair',0),0)
                self.assertEqual(read_json(flow.root/'turns/plan/response.json')['tool_calls'][0]['function']['name'],'case_request_plan')
        finally:case_requests.ROOT,case_requests.REQUESTS_ROOT,case_requests.LEGACY_PROJECT_ROOT=saved


if __name__=='__main__':unittest.main()
