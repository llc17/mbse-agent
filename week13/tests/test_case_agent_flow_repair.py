"""Bounded general-flow regressions; injected HTTP/RPC, no real simulation."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from week13.src import case_requests, case_taskflow as tf, case_projects
from week13.src.case_paths import ROOT, read_json, sha_file
from week13.src.case_scope import validate_request_plan, check_delta, UnsupportedRequestDesign
from week13.src.case_semantic_guard import plan_semantic_validation
from week13.tests.test_case_agent_requests import Harness, synthetic_spec
from week13.tests.test_case_flat_plan_protocol import flat

OUT=ROOT/'results/agent-flow-repair-20261025-01/implementation'


def packet(mass=103.75):
    p=dict(m=mass,I_pitch=20,T_main_max=60,T_nose_max=25)
    return dict(intent='CHANGE',support='SUPPORTED',missing=[],unsupported=[],
        requirements=[dict(text='TEST_ONLY new request',source_id='new',source_text='TEST_ONLY new request')],
        design=dict(summary='仅改变受支持的总质量。',assumptions=[],parameters=[dict(name=k,value=v,unit='1',origin='model',basis='TEST_ONLY') for k,v in p.items()]),
        proposed_parameters=p,defaults=['原重心理想点质量；I继承。'],unknowns=[],rationale='TEST_ONLY',scope=deepcopy(tf.MASS_SCOPE_PACKET))


class RiskAndCommitment(unittest.TestCase):
    def test_all_seven_real_packets_replay_without_rewriting_old_results(self):
        rows=read_json(OUT/'regression-originals.json')
        self.assertEqual(len(rows),7)
        for row in rows:
            with self.subTest(request=row['request_id'],turn=row['turn']):
                self.assertFalse(plan_semantic_validation(row['packet'],row['baseline'])['issues'])
        # A prompt fix is not permission to edit an old refusal or to erase its
        # critical missing list. These original product responses stay invalid.
        old=next(x for x in rows if '044358' in x['request_id'])
        self.assertEqual(old['packet']['support'],'UNSUPPORTED')
        old=next(x for x in rows if '045124' in x['request_id'] and x['turn']=='plan')
        context=read_json(OUT/'originals'/old['request_id']/'task-context.json')
        self.assertTrue(old['packet']['missing'])
        with self.assertRaisesRegex(ValueError,'unresolved critical'):
            validate_request_plan(old['packet'],old['baseline'],context['sources'])

    def test_original_responses_and_failure_files_are_unchanged(self):
        for row in read_json(OUT/'original-files-before.json'):
            with self.subTest(path=row['path']):
                self.assertEqual(sha_file(row['path']),row['sha256'])
                self.assertEqual(sha_file(row['copy']),row['copy_sha256'])

    def test_unseen_numbers_and_risk_synonyms_are_not_action_commitments(self):
        claims=[
            '新增18.75 kg是否完全不影响俯仰惯量分布的物理真实性未知。',
            '质量增加后是否能在既定观察区间完成任务未知。',
            '新增2.125千克可能导致惯量或重心效应，尚未验证。',
            '未知是否需要改变惯量。',
            '是否需要调整I_pitch尚待确认。',
            '尚不清楚是否要改变控制律。',
            '可能需要改变惯量，当前不能据此认定任务成功。',
            '如果修改惯量，结果可能不同；本次仍继承基线。',
            '假设调整控制律，仿真结果可能变化。',
            '真实重心偏移与俯仰惯量变化未建模。',
            '质心偏移与俯仰惯量变化会影响结果。',
            '本次不修改惯量；基线已修复接触方程，本次继承。',
        ]
        for claim in claims:
            for field in ('summary','unknowns'):
                p=packet()
                if field=='summary':p['design']['summary']=claim
                else:p['unknowns']=[claim]
                with self.subTest(claim=claim,field=field):self.assertFalse(plan_semantic_validation(p,{})['issues'])

    def test_unknowns_never_hide_actual_current_edits_or_mixed_clauses(self):
        claims=[
            '为了判断未知，本次将I_pitch改为25。',
            '增加载荷后将StopTime延长。',
            '可能失稳，因此本次将修改惯量。',
            '如果结果不理想，本次将修改惯量。',
            '如果结果不理想本次将修改惯量。',
            '本次不能不修改惯量。',
            '未知是否需要改变惯量，但本次将I_pitch改为25。',
            '质量增加后能否完成任务未知；本次计划调整控制律。',
            '本次增加I_pitch。',
            '本次移动重心。',
            '不调整惯量但本次替换接触方程。',
            '基线已修复FtC，本次再次修复FtC。',
        ]
        for claim in claims:
            p=packet();p['unknowns']=[claim]
            with self.subTest(claim=claim):self.assertTrue(plan_semantic_validation(p,{})['issues'])

    def test_parameters_sources_and_actual_source_gates_remain_hard(self):
        source=dict(id='new',text='TEST_ONLY new request');base=synthetic_spec(source,85)
        p=packet();self.assertTrue(validate_request_plan(p,base,[source])['executable'])
        newer=synthetic_spec(source,90)
        self.assertTrue(validate_request_plan(packet(90),newer,[source])['executable'])
        with self.assertRaises(UnsupportedRequestDesign):validate_request_plan(packet(85),newer,[source])
        for key,value in [('I_pitch',25),('m',84),('T_main_max',999)]:
            bad=deepcopy(p);bad['proposed_parameters'][key]=value
            for x in bad['design']['parameters']:
                if x['name']==key:x['value']=value
            with self.subTest(key=key),self.assertRaises(UnsupportedRequestDesign):validate_request_plan(bad,base,[source])
        bad=deepcopy(p);bad['requirements'][0]['source_text']='not in supplied source'
        with self.assertRaisesRegex(ValueError,'trusted source'):validate_request_plan(bad,base,[source])
        bad=deepcopy(base);bad['artifact']['modelica_source']=bad['artifact']['modelica_source'].replace('der(signal)=0','der(signal)=1')
        ctx=dict(project_id='TEST_ONLY',baseline_ref={},baseline_snapshot=dict(frozen_spec=base),plan=p,sources=[source])
        bad['baseline_ref']={}
        svc=SimpleNamespace(_current_project=lambda:dict(project_id='TEST_ONLY'))
        with patch('week13.src.inherited.artifact_baseline.selection',return_value={}),patch('week13.src.inherited.artifact_baseline.resolve',return_value=ctx['baseline_snapshot']):
            with self.assertRaisesRegex(ValueError,'frozen physics'):check_delta(svc,bad,task_context=ctx)


class InputClassification(unittest.TestCase):
    def test_only_program_owned_importer_work_moves_out_of_missing(self):
        with tempfile.TemporaryDirectory(dir=OUT,prefix='TEST_ONLY-obligations-') as directory:
            root=Path(directory);(root/'case.json').write_text('{"case_id":"TEST_ONLY"}',encoding='utf8')
            history=['尚无基准SysML文件；确认方案后由产品模型补齐并独立验证。',
                     '总质量是否包含设备载荷未明确。',
                     'SysML所引用的实物尺寸来源未知，不能从资料确定。']
            case=dict(case_id='TEST_ONLY',missing=history,support=dict(status='SUPPORTED'))
            ctx=dict(project_root=str(root),root=str(root),baseline_ref=dict(version_id='v000000'),baseline_snapshot=dict(frozen_spec=dict(artifact=dict(modelica_source='TEST_ONLY'))))
            with patch.object(case_projects,'resolve_case',return_value=case),patch.object(case_projects,'verify_case',return_value=case),patch.object(case_projects,'support_for_source',return_value=dict(status='SUPPORTED')),patch.object(tf,'baseline_sysml_available',return_value=False):
                result=tf.imported_obligations(ctx)
                self.assertEqual(result['historical_missing'],history)
                self.assertEqual(result['missing'],history[1:])
                self.assertTrue(result['required_source_completion'])
                facts=tf.TaskFlow(ctx)._plan_source_facts(result)
                self.assertEqual(facts['critical_user_facts_missing'],history[1:])
                self.assertEqual(facts['confirmation_and_execution_authority'],'PROGRAM_OWNED_LIFECYCLE_NOT_MISSING_USER_FACTS')

    def test_real_missing_fact_does_not_become_executable(self):
        source=dict(id='new',text='TEST_ONLY new request');base=synthetic_spec(source,85)
        p=packet();p['missing']=['总质量或增加质量尚不明确。']
        with self.assertRaisesRegex(ValueError,'unresolved critical'):validate_request_plan(p,base,[source])
        p.update(support='NEEDS_INPUT',requirements=[],design={},proposed_parameters={},scope={})
        self.assertFalse(validate_request_plan(p,base,[source])['executable'])

    def test_prompt_and_flat_wire_preserve_missing_and_rerun_rules(self):
        for prompt in (tf.PLAN_PROMPT,tf.flat_plan_prompt(tf.PLAN_PROMPT)):
            self.assertIn('missing仅列',prompt)
            self.assertIn('不能静默补用户事实',prompt)
            self.assertIn('required_source_completion',prompt)
            self.assertIn('相对于所选baseline',prompt)
            self.assertIn('QUESTION只读',prompt)

    def test_readonly_question_cannot_enter_a_new_execution(self):
        source=dict(id='new',text='TEST_ONLY new request');base=synthetic_spec(source,85)
        p=packet();p.update(intent='QUESTION',requirements=[],design={},proposed_parameters={},scope={})
        self.assertFalse(validate_request_plan(p,base,[source])['executable'])
        with self.assertRaises(PermissionError):case_requests._executable(p)
        with tempfile.TemporaryDirectory(dir=OUT,prefix='TEST_ONLY-readonly-') as directory:
            flow=tf.TaskFlow(dict(root=directory,access_mode='RESULT_REVIEW_ONLY'))
            with self.assertRaisesRegex(PermissionError,'readonly'):asyncio.run(flow.run())
            self.assertEqual(list(Path(directory).iterdir()),[])


class FreshUnchangedLifecycle(unittest.TestCase):
    def setUp(self):
        self.saved=(case_requests.ROOT,case_requests.REQUESTS_ROOT,case_requests.LEGACY_PROJECT_ROOT)
        self.temp=tempfile.TemporaryDirectory(dir=OUT,prefix='TEST_ONLY-rerun-')
        self.h=Harness(Path(self.temp.name)/'fixture');self.outbound=[]
        old=self.h.transport
        def transport(ctx,payload):
            tool=(payload.get('tools') or [{}])[0].get('function',{}).get('name')
            if tool is None:
                return dict(id='TEST_ONLY_answer',model='deepseek-flash',choices=[dict(message=dict(content='仅解释绑定的TEST_ONLY合成执行，不作真实车辆结论。',reasoning_content='TEST_ONLY'),finish_reason='stop')],usage=dict(prompt_tokens=20,completion_tokens=10,total_tokens=30))
            if tool!='case_request_plan':
                result=old(ctx,payload)
                if tool=='proposal_prepare':
                    function=result['choices'][0]['message']['tool_calls'][0]['function'];args=json.loads(function['arguments'])
                    args['spec']['sources']=[dict(id=x['id']) for x in ctx['sources']]
                    function['arguments']=json.dumps(args,ensure_ascii=False)
                return result
            self.outbound.append(deepcopy(payload))
            # Inject a model reply, never a production design implementation.
            values=tf.modelica_parameters(ctx['baseline_snapshot']['frozen_spec']['artifact']['modelica_source'])[0]
            p=packet(values['m']);p['proposed_parameters']={k:values[k] for k in tf.REQUEST_PARAMETERS}
            p['design']['parameters']=[dict(name=k,value=v,unit='1',origin='model',basis='TEST_ONLY inherited') for k,v in p['proposed_parameters'].items()]
            p['design']['summary']='同参数原样重新运行，全部车辆参数继承。'
            p['requirements']=[dict(text=ctx['raw_request'],source_id=ctx['sources'][0]['id'],source_text=ctx['raw_request'])]
            return dict(id='TEST_ONLY_reply',model='deepseek-flash',choices=[dict(message=dict(content='',reasoning_content='TEST_ONLY',tool_calls=[dict(id='TEST_ONLY_call',type='function',function=dict(name=tool,arguments=json.dumps(flat(dict(packet=p)),ensure_ascii=False)))]),finish_reason='tool_calls')],usage=dict(prompt_tokens=20,completion_tokens=10,total_tokens=30))
        self.h.transport=transport

    def tearDown(self):
        case_requests.ROOT,case_requests.REQUESTS_ROOT,case_requests.LEGACY_PROJECT_ROOT=self.saved
        self.temp.cleanup()

    def test_two_new_same_parameter_requests_have_distinct_results_and_confirmations(self):
        first=None
        for index in (1,2):
            rid='TEST_ONLY_unchanged_'+str(index)
            self.h.bootstrap(rid,self.h.project_id,'v000002','按当前所选基线原样重新仿真 '+str(index))
            self.h.authorize(rid,reuse=index==2);flow=self.h.flow(rid)
            planned=asyncio.run(flow.plan())
            self.assertEqual(planned['plan']['proposed_parameters']['m'],90)
            with self.assertRaises(PermissionError):case_requests.require_execution(rid)
            case_requests.confirm_request(rid,planned['displayed_digest'],'CONFIRM',approval_basis='TEST_ONLY delegated scope',human_reviewed_displayed_plan=False)
            result=asyncio.run(flow.run());current=result['state']['latest_request_execution']
            self.assertEqual(current['version_id'],'v'+str(index+2).zfill(6))
            if first:
                self.assertNotEqual(first['task_id'],current['task_id'])
                self.assertNotEqual(first['version_id'],current['version_id'])
                self.assertEqual(read_json(first_result_path),first_saved)
            else:
                first=deepcopy(current);first_result_path=flow.root/('execution-'+current['task_id']+'.json');first_saved=read_json(first_result_path)
            self.assertFalse(read_json(case_requests.load_request(rid)['confirmation_path'])['human_reviewed_displayed_plan'])
        context=json.loads(self.outbound[0]['messages'][1]['content'])
        self.assertTrue(context['verified_source_facts']['unchanged_parameter_rerun']['supported'])
        self.assertFalse(context['verified_source_facts']['unchanged_parameter_rerun']['reuse_historical_terminal_result'])


if __name__=='__main__':unittest.main()
