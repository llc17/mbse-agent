"""Bounded local regressions; saved public answers contain no provider reasoning."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from week13.src.case_console import ActionError, Console
from week13.src.case_semantic_guard import protect_explanation

EVIDENCE = Path(__file__).resolve().parents[1] / 'results/agent-bug-sweep-20261028-01'


def saved(name):
    return json.loads((EVIDENCE / name).read_text(encoding='utf8'))


class ScopeRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = saved('stairs300-answer-1.json')

    def report(self, answer, binding=None):
        return protect_explanation(answer, binding or self.original['current_fact_binding'])['explanation_validation']

    def test_all_four_saved_public_answers_remain_exact_and_visible(self):
        for scenario in ('stairs300', 'stairs160'):
            for index in range(2):
                row = saved(f'{scenario}-answer-{index}.json')
                with self.subTest(scenario=scenario, index=index):
                    result = protect_explanation(row['raw_answer'], row['current_fact_binding'], source_references=row['current_source_references'])
                    self.assertTrue(result['explanation_validation']['body_displayed'], result['explanation_validation']['issues'])
                    self.assertEqual(result['raw_answer'], row['raw_answer'])
                    self.assertIn(row['raw_answer'], result['answer'])
                    self.assertFalse(result['explanation_validation']['full_semantic_validation'])

    def test_explicit_local_route_labels_map_only_their_own_status(self):
        good = [
            'v000001 的 route_assessment.task_status=PASS；最终task_status=UNKNOWN。',
            'v1 路线子评估里的task_status=PASS，但当前最终task_status=UNKNOWN。',
            'v1 虽然路线子评估内部带有 task_status=PASS，但最终任务状态为UNKNOWN。',
            'v1 路线子评估的任务状态=PASS；本请求task_status=UNKNOWN。',
        ]
        for answer in good:
            with self.subTest(answer=answer):
                self.assertTrue(self.report(answer)['body_displayed'], self.report(answer)['issues'])

    def test_global_success_is_not_exempted_by_route_words(self):
        bad = [
            'v1 route_assessment.task_status=PASS，但本请求task_status=PASS。',
            'v1 路线子评估里的task_status=PASS，最终任务状态=PASS。',
            'v1 路线子评估内部带有task_status=PASS，因此任务通过。',
            'v1 路线已完成，当前task_status=PASS。',
            'v1 路线子评估最终本请求task_status=PASS。',
            'v1 route_assessment.task_status=UNKNOWN；最终task_status=UNKNOWN。',
            'v99 route_assessment.task_status=PASS。',
        ]
        for answer in bad:
            with self.subTest(answer=answer):
                self.assertFalse(self.report(answer)['body_displayed'])

    def test_route_evidence_is_required_and_unknown_is_not_pass(self):
        for status in (None, 'UNKNOWN', 'FAIL'):
            binding = deepcopy(self.original['current_fact_binding'])
            row = binding['latest_request_execution']
            for holder in (row, row['current_contact_review']):
                if status is None:
                    holder.pop('route_status', None)
                else:
                    holder['route_status'] = status
            assessment = row['current_contact_review'].get('current_assessment', {})
            assessment.pop('route_status', None)
            with self.subTest(route_status=status):
                self.assertFalse(self.report('v1 route_assessment.task_status=PASS。', binding)['body_displayed'])
        empty = dict(request_id='TEST_ONLY_no_execution', project_id='TEST_ONLY_project')
        self.assertFalse(self.report('route_assessment.task_status=PASS。', empty)['body_displayed'])

    def test_no_execution_cannot_invent_structured_statuses(self):
        empty = dict(request_id='TEST_ONLY_no_execution', project_id='TEST_ONLY_project')
        for claim in ('task_status=PASS。', 'physical_status=PASS。', 'tool_status=PASS。',
                      'diagnostic_status=FAIL。', '任务状态=成功。'):
            with self.subTest(claim=claim):
                self.assertFalse(self.report(claim, empty)['body_displayed'])
        for claim in ('task_status=UNKNOWN。', 'tool_status=NOT_RUN。', 'physical_status=UNKNOWN。',
                      '不能声称task_status=PASS。', '历史task_status=PASS。', '如果task_status=PASS。'):
            with self.subTest(claim=claim):
                self.assertTrue(self.report(claim, empty)['body_displayed'])


class ConsoleRegressions(unittest.TestCase):
    def setUp(self):
        self.output = []
        self.prompts = []
        self.ui = Console(console_root=EVIDENCE / 'unused-console', output=self.output.append,
            input_fn=lambda prompt: self.prompts.append(prompt) or '')
        self.rows = [dict(case_id='case_one', name='中文 含空格名称', project_id='p_one'),
                     dict(case_id='case_two', name='其他案例', project_id='p_two')]

    def test_balanced_quotes_preserve_exact_case_name_and_numeric_selection(self):
        for name in ('中文 含空格名称', '"中文 含空格名称"', "'中文 含空格名称'", '"case_one"', '1', '  "1"  '):
            with self.subTest(name=name):
                self.assertEqual(self.ui.resolve_case(name, self.rows)['case_id'], 'case_one')
        self.assertEqual(self.ui.resolve_case('2', self.rows)['case_id'], 'case_two')

    def test_unbalanced_quotes_unknown_ids_and_ambiguous_names_never_resolve(self):
        for token in ('"中文 含空格名称', '中文 含空格名称"', "'中文 含空格名称\"", '3', '', 'case_missing'):
            with self.subTest(token=token), self.assertRaises(ActionError):
                self.ui.resolve_case(token, self.rows)
        with self.assertRaises(ActionError):
            self.ui.resolve_case('"中文 含空格名称"', self.rows + [dict(case_id='duplicate',name='中文 含空格名称')])

    def test_multiple_originals_one_supported_still_requires_explicit_choice(self):
        candidates = [dict(path='full.mo',model_name='Full',support=dict(status='SUPPORTED'))]
        candidates += [dict(path=f'local{i}.mo',model_name=f'Local{i}',support=dict(status='UNSUPPORTED')) for i in range(3)]
        self.assertIsNone(self.ui.choose_model(dict(model_candidates=candidates)))
        self.assertEqual(len(self.prompts), 1)
        self.assertIn('4', self.prompts[0])
        self.assertIn('1', self.prompts[0])
        self.assertIn('明确选择', self.prompts[0])
        self.ui.input = lambda _: '1'
        self.assertEqual(self.ui.choose_model(dict(model_candidates=candidates)), 'full.mo')


if __name__ == '__main__':
    unittest.main()
