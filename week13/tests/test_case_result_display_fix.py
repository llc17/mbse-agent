"""Result display and failed-question readback; isolated fixtures, no transport."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from week13.src.case_console import ActionError,Console
from week13.src.case_console_result import prepare_result
from week13.tests.test_case_console_result_headline import saved_result

OUT=Path(__file__).resolve().parents[1]/'results/result-display-fix-20261028-01/test-tmp'


def current_result():
    value=saved_result()
    value['execution'].update(state='succeeded',parameters=dict(m=110.,stepH=.15,stepD=.28))
    review=value['execution']['current_contact_review']
    review['current_assessment']['completion_time']=34.65
    review['contact_validity']=dict(counters=dict(full_circle_inside_solid_sample_count=0))
    value['state']=dict(request_id='TEST_ONLY_request',project_id='TEST_ONLY_project',latest_request_execution=deepcopy(value['execution']))
    return value


class ResultDisplayFixTests(unittest.TestCase):
    def setUp(self):
        OUT.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=OUT)
        self.output=[]
        self.ui=Console(console_root=Path(self.temp.name),output=self.output.append)
        self.ui.ctx=dict(request_id='TEST_ONLY_request',project_id='TEST_ONLY_project',root=self.temp.name)
        self.ui.selection['request_id']='TEST_ONLY_request'
        self.ui.catalog=dict(project_id='TEST_ONLY_project')

    def tearDown(self):self.temp.cleanup()

    def readback_runner(self,value,*,question_error=None,question_status=None):
        calls=[]
        class Runner:
            def call(inner,action,**kwargs):
                calls.append((action,kwargs))
                if action=='request-result':return deepcopy(value)
                if action=='request-ask':
                    if question_error:raise question_error
                    return dict(status=question_status)
                raise AssertionError('unexpected action '+action)
        self.ui.runner=Runner()
        return calls

    def test_default_success_summary_is_unambiguous_and_full_keeps_original(self):
        value=current_result();before=deepcopy(value);raw=value['explanation']['answer']
        self.ui.show_result(value)
        text='\n'.join(self.output)
        self.assertEqual(self.output[0],'【任务结论】任务成功（当前简化模型内）。')
        for expected in ('110 kg','150 mm','280 mm','34.65 s','工具 PASS','路线 PASS','接触诊断 PASS','整圆入楼梯实体异常：0','本请求证据：'):
            self.assertIn(expected,text)
        self.assertNotIn(raw,text)
        self.assertEqual(sum('真实车辆能力尚未验证' in line for line in self.output),1)
        self.output.clear();self.ui.show_result(value,full=True)
        self.assertIn(raw,'\n'.join(self.output));self.assertEqual(before,value)

    def test_diagnostic_failure_stays_unknown_even_when_route_passes(self):
        value=current_result();review=value['execution']['current_contact_review']
        review.update(status='FAIL',validity_status='FAIL',task_status='UNKNOWN',diagnostic_status='FAIL',completion_status='CONTACT_ANOMALY_ROUTE_COMPLETE')
        review['current_assessment'].update(validity_status='FAIL',task_status='UNKNOWN',completion_status='CONTACT_ANOMALY_ROUTE_COMPLETE')
        review['contact_validity']['counters']['full_circle_inside_solid_sample_count']=248
        value['state']['latest_request_execution']=deepcopy(value['execution'])
        self.ui.show_result(value);text='\n'.join(self.output)
        self.assertIn('轨迹已完成，但模型接触异常，任务结论UNKNOWN',self.output[0])
        self.assertNotIn('任务成功',text);self.assertNotIn('完成情况：',text)
        self.assertIn('轨迹覆盖观测：',text);self.assertIn('248 个采样',text)

    def test_no_execution_incomplete_and_conflict_never_report_success(self):
        missing=dict(state=dict(request_id='TEST_ONLY_request'))
        incomplete=saved_result(task_status='FAIL',mission=dict(validity_status='PASS',task_status='FAIL',completion_status='OBSERVATION_INTERVAL_INCOMPLETE'))
        conflict=current_result();conflict['state']['latest_request_execution']['task_id']='different'
        for value in (missing,incomplete,conflict):
            with self.subTest(value=value.get('state')):
                self.output.clear();self.ui.show_result(value);text='\n'.join(self.output)
                self.assertNotIn('任务成功',text);self.assertNotIn('完成情况：',text)

    def test_full_still_blocks_conflicting_model_claim_and_preserves_raw(self):
        value=current_result();raw='本版本physical_status=PASS，已验证真实车辆能力。'
        value['explanation']['answer']=raw
        self.ui.show_result(value,full=True)
        self.assertNotIn(raw,'\n'.join(self.output))
        view=prepare_result(value,request_id='TEST_ONLY_request')[0]
        self.assertEqual(view['explanation']['raw_answer'],raw)
        self.assertFalse(view['explanation']['explanation_validation']['body_displayed'])
        self.assertFalse(view['explanation']['explanation_validation']['full_semantic_validation'])

    def test_result_usage_typo_never_dispatches_or_refreshes(self):
        def forbidden(*args,**kwargs):raise AssertionError('must not dispatch')
        self.ui.refresh=forbidden
        self.ui.handle('/result ful')
        self.assertIn('用法：/result','\n'.join(self.output))

    def test_result_full_dispatches_only_readonly_current_result(self):
        value=current_result();calls=self.readback_runner(value)
        self.ui.refresh=lambda:None
        self.ui.handle('/result full')
        self.assertEqual([row[0] for row in calls],['request-result'])
        self.assertIn(value['explanation']['answer'],'\n'.join(self.output))

    def test_question_unknown_reads_same_request_pass_without_resend(self):
        calls=self.readback_runner(current_result(),question_error=ActionError('HTTP interrupted/uncertain',kind='unknown'))
        self.ui.budget_guard=lambda:True;self.ui.refresh=lambda:None
        self.ui.ask('请解释本次结果')
        text='\n'.join(self.output)
        self.assertIn('本次追问回复未取得；已有仿真结果不受影响',text)
        self.assertIn('【任务结论】任务成功（当前简化模型内）。',text);self.assertIn('34.65 s',text)
        self.assertEqual([row[0] for row in calls],['request-ask','request-result'])
        self.assertTrue(all(row[1]['request_id']=='TEST_ONLY_request' for row in calls))

    def test_budget_refusal_only_reads_result_and_never_asks(self):
        calls=self.readback_runner(current_result());self.ui.budget_guard=lambda:False
        self.ui.ask('再次解释')
        self.assertEqual([row[0] for row in calls],['request-result'])
        self.assertIn('已有仿真结果不受影响','\n'.join(self.output))

    def test_unknown_response_uses_readback_without_second_question(self):
        calls=self.readback_runner(current_result(),question_status='UNKNOWN');self.ui.budget_guard=lambda:True
        self.ui.ask('解释')
        self.assertEqual([row[0] for row in calls],['request-ask','request-result'])
        self.assertIn('已有仿真结果不受影响','\n'.join(self.output))

    def test_fallback_rejects_cross_request_case_history_and_missing_execution(self):
        values=[]
        wrong_request=current_result();wrong_request['state']['request_id']='other';values.append(wrong_request)
        wrong_case=current_result();wrong_case['state']['project_id']='other';values.append(wrong_case)
        history=current_result();history['state']['reviewed_execution']=history['state'].pop('latest_request_execution');values.append(history)
        values.append(dict(state=dict(request_id='TEST_ONLY_request',stage_facts=dict(task_status='PASS'))))
        conflict=current_result();conflict['state']['latest_request_execution']['task_id']='other';values.append(conflict)
        for value in values:
            with self.subTest(state=value['state']):
                self.output.clear();calls=self.readback_runner(value)
                self.assertFalse(self.ui.show_ask_failure_result('TEST_ONLY_request'))
                text='\n'.join(self.output)
                self.assertNotIn('任务成功',text);self.assertNotIn('已有仿真结果不受影响',text)
                self.assertEqual([row[0] for row in calls],['request-result'])
        self.output.clear();calls=self.readback_runner(current_result());self.ui.selection['request_id']='other'
        self.assertFalse(self.ui.show_ask_failure_result('TEST_ONLY_request'));self.assertEqual(calls,[])


if __name__=='__main__':unittest.main()
