"""Isolated display classification; no HTTP, Store, approval or dynamics."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from week13.src.case_console import Console
from week13.src.case_contact_review import SCHEMA,RULE_VERSION


OUT = Path(__file__).resolve().parents[1] / 'results/result-headline-20261022-01'


def saved_result(**updates):
    mission = dict(validity_status='PASS', task_status='PASS',
                   completion_status='COMPLETE_FORWARD_UP20_DOWN20_EXIT_AND_CONTINUE', completion_time=47.15)
    execution = dict(project_id='TEST_ONLY_project',version_id='vTEST', task_id='TEST_ONLY_task', tool_status='PASS',
                     chain_status='PASS', task_status='PASS', test_only=False, mission=mission,
                     parameters=dict(stepH=.23, stepD=.25), source_sha256='a' * 64,
                     spec_digest='d'*64,manifest_sha256='b'*64,
                     data_files=[dict(path='TEST_ONLY.csv')])
    execution.update(updates)
    # Synthetic display input only; geometry/binding verification itself is
    # covered by test_case_contact_integration with manifest-bound fixtures.
    current=execution.get('mission') or {}
    execution['current_contact_review']=dict(schema=SCHEMA,rule_version=RULE_VERSION,status=current.get('validity_status','UNKNOWN'),validity_status=current.get('validity_status','UNKNOWN'),task_status=execution.get('task_status','UNKNOWN'),route_status=current.get('task_status','UNKNOWN'),diagnostic_status=current.get('validity_status','UNKNOWN'),completion_status=current.get('completion_status','UNKNOWN'),execution_evidence_status='PASS',current_chain_status=execution['chain_status'],current_assessment=current,binding=dict(project_id=execution['project_id'],version_id=execution['version_id'],task_id=execution['task_id'],spec_digest=execution['spec_digest'],source_sha256=execution['source_sha256'],manifest_sha256=execution['manifest_sha256'],csv_sha256='c'*64,rule_version=RULE_VERSION))
    return dict(stage='RESULT_READ_BACK', request_id='TEST_ONLY_request', execution=execution,
                explanation=dict(answer='TEST_ONLY完整产品解释原文\n第二行必须保留。',
                                 explanation_validation=dict(status='NO_KNOWN_FACT_CONFLICT',body_displayed=True,full_semantic_validation=False)))


class ResultHeadlineTests(unittest.TestCase):
    def setUp(self):
        OUT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='headline-', dir=OUT)
        self.out = []
        self.ui = Console(console_root=Path(self.temp.name), output=self.out.append)
        self.ui.ctx = dict(request_id='TEST_ONLY_request', root=self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def render(self, result, *, full=False):
        self.out.clear()
        original = deepcopy(result)
        self.ui.show_result(result,full=full)
        self.assertEqual(original, result, 'display must not mutate saved evidence')
        self.assertTrue(self.out[0].startswith('【任务结论】'))
        self.assertEqual(sum(line.startswith('【任务结论】') for line in self.out), 1)
        return '\n'.join(self.out)

    def test_pass_is_first_and_full_original_information_remains(self):
        result = saved_result()
        text = self.render(result,full=True)
        self.assertEqual(self.out[0], '【任务结论】任务成功（当前简化模型内）。')
        self.assertIn('前向上楼20级、前向下楼20级，末轮驶出出口后继续前行', text)
        self.assertIn('47.15 s', text)
        for value in ('RESULT_READ_BACK', 'vTEST', '0.23 m', '0.25 m', 'a' * 64, 'TEST_ONLY.csv', result['explanation']['answer']):
            self.assertIn(value, text)
        self.assertLess(text.index('结果说明（模型生成）：已保存'), text.index('结果状态'))

    def test_valid_task_failure_reports_failed_check_not_observation_or_timeout(self):
        result = saved_result(state='failed', task_status='FAIL', mission=dict(validity_status='PASS', task_status='FAIL', completion_status='EVENT_ORDER_FAIL'))
        self.render(result)
        self.assertIn('任务未完成（独立任务检查未通过', self.out[0])
        self.assertNotIn('观察区间', self.out[0])
        self.assertNotIn('超时', self.out[0])

    def test_observation_interval_incomplete_does_not_claim_full_route(self):
        result = saved_result(task_status='FAIL', mission=dict(validity_status='PASS', task_status='FAIL', completion_status='OBSERVATION_INTERVAL_INCOMPLETE'))
        self.render(result)
        self.assertIn('观察区间内任务未完成', self.out[0])
        self.assertFalse(any(line.startswith('完成情况：') for line in self.out))

    def test_tool_failure_is_blocked_not_vehicle_failure(self):
        result = saved_result(tool_status='FAIL', chain_status='FAIL', task_status='UNKNOWN', mission={})
        self.render(result)
        self.assertIn('验证受阻，任务是否完成尚未确定', self.out[0])

    def test_invalid_evidence_does_not_claim_task_failure(self):
        result = saved_result(task_status='FAIL', mission=dict(validity_status='FAIL', task_status='FAIL'))
        self.render(result)
        self.assertIn('模型接触异常', self.out[0])
        self.assertNotIn('任务未完成', self.out[0])

    def test_unknown_validity_never_passes(self):
        self.render(saved_result(mission=dict(validity_status='UNKNOWN', task_status='PASS')))
        self.assertIn('结论未确定', self.out[0])

    def test_unknown_task_never_passes(self):
        self.render(saved_result(task_status='UNKNOWN', mission={}))
        self.assertIn('结论未确定', self.out[0])

    def test_history_project_pass_cannot_be_used_as_current_execution(self):
        old = saved_result()['execution']
        self.render(dict(stage='PROPOSED', state=dict(latest_request_execution=None, latest_project_execution=old, last_task_pass=old), explanation=dict(answer='模型说任务已经完成。')))
        self.assertIn('尚未运行', self.out[0])
        self.assertNotIn('任务已完成', self.out[0])

    def test_readonly_review_is_explicitly_historical(self):
        result = saved_result()
        execution = result.pop('execution')
        result['state'] = dict(reviewed_execution=execution, latest_request_execution=None)
        self.render(result)
        self.assertIn('历史结果：任务成功（当前简化模型内）', self.out[0])
        self.assertIn('本次仅只读查看，未新增运行', self.out[0])

    def test_explanation_unknown_keeps_trusted_task_pass_and_unverified_raw_is_hidden(self):
        result = saved_result()
        result['explanation']['status'] = 'UNKNOWN'
        result['explanation'].pop('explanation_validation')
        text = self.render(result)
        self.assertIn('任务成功（当前简化模型内）', self.out[0])
        self.assertIn('产品解释未完成：UNKNOWN', text)
        self.assertNotIn(result['explanation']['answer'], text)
        self.assertIn('原文仅留证据', text)

    def test_missing_explanation_is_separate(self):
        result = saved_result()
        result.pop('explanation')
        text = self.render(result)
        self.assertIn('任务成功（当前简化模型内）', self.out[0])
        self.assertIn('产品解释未完成', text)

    def test_nested_execution_task_result_and_duplicate_wrappers(self):
        flat = saved_result()
        evidence = flat.pop('execution')
        flat['execution'] = dict(task=dict(version_id='vTEST', task_id='TEST_ONLY_task', state='succeeded',
                                          request_id='case_execute_TEST_ONLY_request', result=evidence))
        result = dict(reused=True, pipeline=dict(reused=True, partial=flat))
        self.render(result)
        self.assertIn('任务成功（当前简化模型内）', self.out[0])
        self.assertEqual(self.out.count('以下为已保存结果的只读读回。'), 1)

    def test_latest_request_execution_is_supported(self):
        result = saved_result()
        result['state'] = dict(latest_request_execution=result.pop('execution'))
        self.render(result)
        self.assertIn('任务成功（当前简化模型内）', self.out[0])

    def test_conflicting_execution_and_current_state_do_not_pass(self):
        result = saved_result()
        result['state'] = dict(latest_request_execution=dict(version_id='vTEST', task_id='TEST_ONLY_task', task_status='FAIL'))
        self.render(result)
        self.assertIn('证据存在冲突', self.out[0])

    def test_conflicting_mission_or_task_binding_do_not_pass(self):
        for update in (dict(mission=dict(validity_status='PASS', task_status='FAIL')),
                       dict(request_id='another-request')):
            with self.subTest(update=update):
                self.render(saved_result(**update))
                self.assertIn('证据存在冲突', self.out[0])

    def test_test_only_at_execution_or_outcome_never_claims_real_pass(self):
        for update in (dict(test_only=True), dict(outcome=dict(test_only=True))):
            with self.subTest(update=update):
                self.render(saved_result(**update))
                self.assertIn('仅测试数据（TEST_ONLY）', self.out[0])
                self.assertNotIn('任务已完成', self.out[0])

    def test_running_execution_is_in_progress(self):
        self.render(dict(state=dict(latest_request_execution=dict(state='running', task_id='TEST_ONLY_task'))))
        self.assertIn('验证进行中', self.out[0])

    def test_legal_repair_request_binding_does_not_look_like_conflict(self):
        self.render(saved_result(request_id='case_execute_repair_1_TEST_ONLY_request'))
        self.assertIn('任务成功（当前简化模型内）', self.out[0])
        self.render(saved_result(request_id='case_execute_repair_1_other_request'))
        self.assertIn('证据存在冲突', self.out[0])

    def test_failed_cancelled_or_uncertain_state_cannot_display_all_pass(self):
        for state in ('failed', 'cancelled', 'uncertain'):
            with self.subTest(state=state):
                self.render(saved_result(state=state))
                self.assertIn('证据存在冲突', self.out[0])


if __name__ == '__main__':
    unittest.main()
