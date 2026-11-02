"""Bound stage display regressions; pure fixtures, no HTTP/MCP/dynamics."""
import copy
import unittest

from week13.src.case_console import Console
from week13.src.case_console_result import prepare_result
from week13.src.case_stage_facts import stage_facts, stage_lines


class StageFactsTests(unittest.TestCase):
    def setUp(self):
        self.ctx = dict(request_id='r1', project_id='p1', session_id='s1',
                        request_digest='d1', stage='CONFIRMED', plan={'support': 'SUPPORTED'})
        self.task = dict(task_id='t1', version_id='v1', project_id='p1', session_id='s1',
                         request_id='case_execute_r1', state='succeeded', created_at='2026-10-22')
        self.snapshot = dict(task_id='t1', version_id='v1', project_id='p1',
                             source_sha256='source', spec_digest='spec', tool_status='PASS',
                             chain_status='PASS', task_status='FAIL', outcome=dict(
                                 tool_result=dict(compile={'status': 'PASS'}, simulation={'status': 'PASS'}),
                                 mission_assessment={'completion_status': 'OBSERVATION_INTERVAL_INCOMPLETE'}))

    def facts(self, **kwargs):
        return stage_facts(self.ctx, tasks=[self.task], executions=[self.snapshot], **kwargs)

    def envelope(self, facts):
        return dict(schema='v12-request-stage-facts-1', request_id='r1', current=facts,
                    approved_requests=[], authority_sha256='authority')

    def test_terminal_is_independent_of_aggregate_file(self):
        facts = self.facts()
        self.assertEqual(facts['execution_state'], 'TERMINAL')
        self.assertEqual(facts['compile'], 'PASS')
        self.assertIn('观察区间内任务未完成', facts['summary'])
        self.assertNotIn('待执行', facts['summary'])

    def test_foreign_session_cannot_supply_current_terminal(self):
        self.task['session_id'] = 'foreign'
        facts = self.facts()
        self.assertEqual(facts['execution_state'], 'UNCERTAIN')
        self.assertEqual(facts['simulation'], 'UNKNOWN')

    def test_foreign_project_or_request_cannot_supply_terminal(self):
        for key in ('project_id', 'request_id'):
            with self.subTest(key=key):
                task = dict(self.task, **{key: 'foreign'})
                facts = stage_facts(self.ctx, tasks=[task], executions=[self.snapshot])
                self.assertEqual(facts['execution_state'], 'UNCERTAIN')

    def test_wrong_version_or_source_digest_is_unknown(self):
        self.snapshot['version_id'] = 'foreign'
        self.assertEqual(self.facts()['execution_state'], 'UNCERTAIN')
        self.snapshot['version_id'] = 'v1'
        self.snapshot['source_sha256'] = None
        self.assertEqual(self.facts()['execution_state'], 'UNCERTAIN')

    def test_generation_spec_drift_is_unknown(self):
        self.assertEqual(self.facts(generation={'spec_digest': 'foreign'})['compile'], 'UNKNOWN')

    def test_running_and_uncertain_override_confirmation(self):
        for state, mode in [('running', '验证进行中（只读）'), ('uncertain', '执行状态UNKNOWN（只读核实）')]:
            self.task['state'] = state
            facts = self.facts()
            ui = Console.__new__(Console)
            ui.ctx = dict(self.ctx, stage_facts=self.envelope(facts), confirmation_path='confirmed')
            self.assertEqual(ui.mode(), mode)

    def test_terminal_mode_overrides_confirmation(self):
        ui = Console.__new__(Console)
        ui.ctx = dict(self.ctx, terminal_saved=True, execution_state='TERMINAL', confirmation_path='confirmed')
        self.assertEqual(ui.mode(), '只读追问')

    def test_no_budget_new_request_remains_not_run(self):
        ctx = dict(self.ctx, stage='DRAFT', plan=None)
        facts = stage_facts(ctx)
        self.assertEqual(facts['execution_state'], 'NOT_RUN')
        self.assertEqual(facts['plan'], 'NOT_RUN')
        self.assertEqual(facts['compile'], 'NOT_RUN')

    def test_plan_json_failure_does_not_become_source_generation_failure(self):
        facts = stage_facts(dict(self.ctx, plan=None), failures=[
            dict(stage='plan', attempt=0, error='JSONDecodeError: missing close')])
        self.assertEqual(facts['plan'], 'FAIL')
        self.assertEqual([facts[k] for k in ('generate', 'compile', 'simulation')], ['NOT_RUN'] * 3)
        self.assertTrue(facts['summary'].startswith('方案JSON解析失败'))

    def test_failure_history_keeps_resolved_and_unresolved_stages(self):
        failures = [dict(stage='plan', attempt=0, error='JSON'),
                    dict(stage='generate', attempt=0, error='source')]
        facts = stage_facts(self.ctx, failures=failures)
        self.assertEqual(facts['failure_history'][0]['status'], 'RESOLVED_WITH_LATER_VALIDATION')
        self.assertEqual(facts['unresolved_failure']['stage'], 'generate')
        self.assertEqual(facts['compile'], 'NOT_RUN')
        resolved = self.facts(failures=failures)
        self.assertTrue(all(row['status'] == 'RESOLVED_WITH_LATER_VALIDATION' for row in resolved['failure_history']))
        self.assertIsNone(resolved['unresolved_failure'])

    def test_missing_store_read_and_source_drift_cannot_claim_not_run(self):
        self.assertEqual(stage_facts(self.ctx, read_errors=['store missing'])['execution_state'], 'UNCERTAIN')
        self.assertEqual(stage_facts(self.ctx, proposal_unknown=True)['execution_state'], 'UNCERTAIN')

    def test_product_prose_cannot_replace_system_facts(self):
        envelope = self.envelope(self.facts())
        result = prepare_result({'explanation': {'answer': '已经完成，第二项双源码失败'}},
                                request_id='r1', stage_context=envelope)
        self.assertTrue(result[3][0].startswith('【系统阶段事实】'))
        self.assertIn('观察区间内任务未完成', result[3][0])
        self.assertEqual(stage_lines(envelope, request_id='foreign'), [])

    def test_pure_summary_does_not_modify_input(self):
        before = copy.deepcopy((self.ctx, self.task, self.snapshot))
        self.facts()
        self.assertEqual(before, (self.ctx, self.task, self.snapshot))


if __name__ == '__main__':
    unittest.main()
