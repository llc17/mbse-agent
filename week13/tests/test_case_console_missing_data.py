"""TEST_ONLY missing-data UI routes; isolated storage, no product or OMC calls."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import tempfile
import unittest

from week13.src import case_console as console


EVIDENCE = Path(__file__).resolve().parents[1] / 'results/no-data-continuation-20261026-01'


def fixture():
    """Synthetic values exercise UI routing, never a vehicle result."""
    plan = dict(intent='CHANGE', support='NEEDS_INPUT', missing=['TEST_ONLY轮转惯量缺少来源'],
                unsupported=[], requirements=[], design={}, proposed_parameters={}, scope={},
                defaults=[], unknowns=['TEST_ONLY物理未知'], rationale='TEST_ONLY缺项')
    deadline = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    return dict(request_id='TEST_ONLY_missing', project_id='TEST_ONLY_project',
                baseline_version_id='v000000', raw_request='TEST_ONLY有待补充事实的请求',
                stage='PROPOSED', plan=plan, displayed_digest='TEST_ONLY_plan_digest',
                root='TEST_ONLY_NO_BACKEND', execution_state='NOT_RUN', terminal_saved=False,
                baseline_parameters=dict(m=85, stepH=.15, stepD=.27),
                verified_source_facts=dict(selected_baseline_sysml_present=True),
                budget=dict(budget_id='TEST_ONLY_budget', hard_deadline_utc=deadline,
                            ledger_path='TEST_ONLY_NO_LEDGER', approval_quote='确认',
                            blocked=None, http_blocked=None, permissions=dict(execution=True),
                            caps=dict(http_requests=6), used=dict(http_requests=1)))


class Runner:
    def __init__(self, ctx):
        self.contexts = {ctx['request_id']: deepcopy(ctx)}
        self.calls = []
        self.reads = []
        self.catalog = dict(case_id='TEST_ONLY_case', project_id=ctx['project_id'],
                            project_label='TEST_ONLY案例', controls={}, missing=[], requests=[],
                            support=dict(status='SUPPORTED'),
                            baselines=[dict(version_id='v000000', selectable=True, parameters=dict(m=85))],
                            default_baseline=dict(version_id='v000000', parameters=dict(m=85)))
        self.current = ctx['request_id']

    def data(self, action, **options):
        self.reads.append((action, options))
        if action == 'discover':
            return deepcopy(self.catalog)
        if action == 'context':
            self.current = options['request_id']
            return deepcopy(self.contexts[self.current])
        raise AssertionError('unexpected local read: ' + action)

    def call(self, action, **options):
        self.calls.append((action, deepcopy(options)))
        if action == 'request-ask':
            return dict(answer='TEST_ONLY只读回答', binding=dict(latest_request_execution=None))
        if action == 'request-status':
            return dict(latest_request_execution=None)
        if action == 'request':
            child = deepcopy(self.contexts[self.current])
            child.update(request_id=options['request_id'], raw_request=options['requirement'],
                         baseline_version_id=options['baseline_version'], plan=None,
                         displayed_digest=None, stage='DRAFT', budget=None)
            self.contexts[child['request_id']] = child
            return deepcopy(child)
        if action == 'authorize-request':
            if not options.get('reuse_budget'):
                raise AssertionError('new budget approval forbidden in this fixture')
            child = self.contexts[options['request_id']]
            parent = self.contexts[self.current]
            if options['budget_id'] != parent['budget']['budget_id']:
                raise AssertionError('budget changed')
            child['budget'] = deepcopy(parent['budget'])
            return {}
        if action == 'plan-request':
            child = self.contexts[options['request_id']]
            child.update(plan=deepcopy(fixture()['plan']), displayed_digest='TEST_ONLY_child_plan', stage='PROPOSED')
            child['budget']['used']['http_requests'] += 1
            return dict(plan=deepcopy(child['plan']))
        if action in ('confirm-request', 'run-request'):
            return {}
        raise AssertionError('unexpected operation: ' + action)


class MissingDataConsoleTests(unittest.TestCase):
    def setUp(self):
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='isolated-', dir=EVIDENCE)
        self.root = Path(self.temp.name) / 'console'
        self.out = []
        self.ctx = fixture()
        self.runner = Runner(self.ctx)
        self.ui = self.console()
        self.ui.catalog = deepcopy(self.runner.catalog)
        self.ui.selection.update(request_id=self.ctx['request_id'], baseline_version_id='v000000')
        self.ui.refresh()

    def tearDown(self):
        self.temp.cleanup()

    def console(self):
        return console.Console(self.runner, console_root=self.root, input_fn=lambda _: '', output=self.out.append)

    def histories(self):
        return [console.read(path) for path in (self.root / 'history').glob('*.json')]

    def test_explicit_ask_preserves_identity_missing_plan_budget_at_all_stages(self):
        for support, terminal in (('NEEDS_INPUT', False), ('SUPPORTED', False), ('SUPPORTED', True)):
            with self.subTest(support=support, terminal=terminal):
                current = self.runner.contexts[self.ctx['request_id']]
                current['plan']['support'] = support
                current['terminal_saved'] = terminal
                current['execution_state'] = 'TERMINAL' if terminal else 'NOT_RUN'
                self.ui.refresh()
                before = deepcopy(self.ui.ctx)
                count = len(self.runner.calls)
                self.ui.handle('/ask 暂时没有资料会影响哪些结论？')
                self.assertEqual([action for action, _ in self.runner.calls[count:]], ['request-ask'])
                self.assertEqual(self.runner.calls[-1][1]['request_id'], before['request_id'])
                self.assertEqual(self.ui.ctx, before)
                self.assertEqual(self.runner.contexts[before['request_id']], before)

    def test_defer_is_local_bound_idempotent_and_never_resolves_missing(self):
        before = deepcopy(self.runner.contexts)
        self.ui.handle('/defer 现在无法取得原始资料')
        self.ui.handle('/defer 现在无法取得原始资料')
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.runner.contexts, before)
        record = self.ui.selection['deferred_input']
        self.assertEqual(record['missing'], self.ctx['plan']['missing'])
        self.assertEqual(record['binding']['project_id'], self.ctx['project_id'])
        self.assertEqual(record['binding']['request_id'], self.ctx['request_id'])
        self.assertEqual(record['binding']['baseline_version_id'], 'v000000')
        self.assertEqual(record['binding']['plan_digest'], self.ctx['displayed_digest'])
        self.assertIsNotNone(datetime.fromisoformat(record['at_utc']).tzinfo)
        self.assertEqual(len(self.histories()), 1)
        self.assertIn('缺项仍未解决，方案不可执行', '\n'.join(self.out))
        self.ui.handle('确认执行')
        self.assertEqual(self.runner.calls, [])
        self.assertIn('没有完整受支持', '\n'.join(self.out))

    def test_restart_and_open_redisplay_only_same_binding_without_execution(self):
        self.ui.handle('我现在提供不了数据。')
        self.ui.handle('/quit')
        self.out.clear()
        reopened = self.console()
        reopened.start()
        self.assertIsNone(reopened.catalog)
        reopened.handle('/resume')
        self.assertIn('已记录资料暂缺：我现在提供不了数据。', '\n'.join(self.out))
        self.assertEqual(self.runner.calls, [])
        self.out.clear()
        reopened.handle('/open ' + self.ctx['request_id'])
        self.assertIn('已记录资料暂缺', '\n'.join(self.out))
        self.assertEqual(self.runner.calls, [])

    def test_terminal_stale_needs_input_plan_keeps_plain_text_readonly(self):
        self.ui.handle('/defer TEST_ONLY先前缺资料')
        current = self.runner.contexts[self.ctx['request_id']]
        current.update(terminal_saved=True, execution_state='TERMINAL')
        self.ui.refresh()
        self.out.clear()
        self.ui.show_context()
        self.ui.show_plan(self.ui.ctx['plan'])
        rendered = '\n'.join(self.out)
        self.assertNotIn('已记录资料暂缺', rendered)
        self.assertNotIn('可直接补充事实', rendered)
        self.ui.handle('我现在提供不了数据')
        self.assertEqual([action for action, _ in self.runner.calls], ['request-ask'])
        self.assertEqual(current['plan']['support'], 'NEEDS_INPUT')
        self.assertTrue(current['terminal_saved'])

    def test_deferral_never_leaks_across_request_project_baseline_or_changed_plan(self):
        self.ui.handle('/defer TEST_ONLY缺资料')
        original = deepcopy(self.ui.ctx)
        for key, value in (('request_id', 'TEST_ONLY_other'), ('project_id', 'TEST_ONLY_other_project'),
                           ('baseline_version_id', 'v000001'), ('displayed_digest', 'TEST_ONLY_new_digest')):
            with self.subTest(key=key):
                self.ui.ctx = dict(deepcopy(original), **{key: value})
                self.out.clear()
                self.ui.show_missing_options()
                self.assertNotIn('已记录资料暂缺', '\n'.join(self.out))
        self.ui.ctx = deepcopy(original)
        self.ui.ctx['plan']['missing'].append('TEST_ONLY新增缺项')
        self.out.clear()
        self.ui.show_missing_options()
        self.assertNotIn('已记录资料暂缺', '\n'.join(self.out))
        self.ui.ctx = deepcopy(original)
        self.ui.ctx['displayed_digest'] = None
        self.runner.contexts[self.ctx['request_id']] = deepcopy(self.ui.ctx)
        self.ui.handle('/defer TEST_ONLY无显示摘要时绑定完整方案')
        self.assertEqual(self.ui.selection['deferred_input']['binding']['plan_digest'], self.ui.defer_binding()['plan_sha256'])

    def test_only_complete_no_data_statements_defer_other_facts_are_not_truncated(self):
        for phrase in sorted(console.NO_DATA_STATEMENTS):
            for punctuation in ('', '。', '！'):
                with self.subTest(phrase=phrase, punctuation=punctuation):
                    self.ui.handle('  ' + phrase + punctuation + '  ')
                    self.assertEqual(self.ui.selection['deferred_input']['raw_text'], phrase + punctuation)
        self.assertEqual(self.runner.calls, [])
        for text in ('我没有轮子数据，但总质量为90kg', '暂时没有数据，但总质量为90kg',
                     '请问暂时没有数据会怎样？', '暂时没有数据?', '暂时没有数据.', '我现在提供不了数据，还没测量'):
            with self.subTest(text=text):
                seen = []
                original = self.ui.supplement
                try:
                    self.ui.supplement = seen.append
                    self.ui.handle(text)
                finally:
                    self.ui.supplement = original
                self.assertEqual(seen, [text])
        self.assertEqual(self.runner.calls, [])

    def test_empty_ask_and_no_request_do_not_call(self):
        self.ui.handle('/ask')
        self.assertIn('用法：/ask', '\n'.join(self.out))
        self.ui.selection['request_id'] = None
        self.ui.ctx = None
        self.ui.handle('/ask 缺项影响？')
        self.ui.handle('/defer 无数据')
        self.assertEqual(self.runner.calls, [])
        self.assertIn('请先选择请求', '\n'.join(self.out))

    def test_expired_unknown_and_exhausted_budget_guard_ask_but_local_defer_needs_no_budget(self):
        current = self.runner.contexts[self.ctx['request_id']]
        for blocked, http_blocked in (('TEST_ONLY截止已过', None), ('TEST_ONLY用量UNKNOWN', None),
                                      (None, 'TEST_ONLY产品HTTP用尽')):
            with self.subTest(blocked=blocked, http_blocked=http_blocked):
                current['budget'].update(blocked=blocked, http_blocked=http_blocked)
                current['budget']['hard_deadline_utc'] = '2000-01-01T00:00:00+00:00'
                self.ui.handle('/ask 缺项影响？')
                self.ui.handle('/defer TEST_ONLY预算停止也能本地记录')
                self.assertEqual(self.runner.calls, [])
                self.assertIn('deferred_input', self.ui.selection)
        current['budget'] = None
        self.ui.handle('/defer TEST_ONLY无预算也能记录')
        self.assertEqual(self.runner.calls, [])

    def test_real_fact_supplement_uses_same_budget_deadline_and_keeps_unknown_missing(self):
        self.ui.handle('/defer TEST_ONLY暂缺轮转数据')
        parent = deepcopy(self.ui.ctx)
        text = '我没有轮子数据，但总质量为90kg'
        self.ui.handle(text)
        self.assertEqual([action for action, _ in self.runner.calls], ['request', 'authorize-request', 'plan-request'])
        self.assertNotEqual(self.ui.ctx['request_id'], parent['request_id'])
        self.assertEqual(self.ui.ctx['raw_request'], parent['raw_request'] + '\n\n补充事实原文：\n' + text)
        for key in ('budget_id', 'hard_deadline_utc', 'ledger_path'):
            self.assertEqual(self.ui.ctx['budget'][key], parent['budget'][key])
        self.assertTrue(self.runner.calls[1][1]['reuse_budget'])
        self.assertEqual(self.ui.ctx['plan']['missing'], parent['plan']['missing'])
        self.out.clear()
        self.ui.show_missing_options()
        self.assertNotIn('已记录资料暂缺', '\n'.join(self.out))
        count = len(self.runner.calls)
        self.ui.handle('确认执行')
        self.assertEqual(len(self.runner.calls), count)

    def test_defer_rejects_supported_readonly_terminal_running_uncertain_and_empty_missing(self):
        for changes in (dict(plan=dict(self.ctx['plan'], support='SUPPORTED')),
                        dict(access_mode='RESULT_REVIEW_ONLY'), dict(terminal_saved=True),
                        dict(execution_state='RUNNING'), dict(execution_state='UNCERTAIN'),
                        dict(plan=dict(self.ctx['plan'], missing=[]))):
            with self.subTest(changes=changes):
                self.runner.contexts[self.ctx['request_id']] = dict(deepcopy(self.ctx), **changes)
                self.ui.handle('/defer 无数据')
                self.assertNotIn('deferred_input', self.ui.selection)
                self.assertEqual(self.runner.calls, [])
        self.runner.contexts[self.ctx['request_id']] = deepcopy(self.ctx)
        self.ui.handle('/defer')
        self.assertNotIn('deferred_input', self.ui.selection)
        self.assertIn('用法：/defer', '\n'.join(self.out))

    def test_supported_plan_without_wheel_inertia_keeps_existing_confirmation_path(self):
        current = self.runner.contexts[self.ctx['request_id']]
        current['plan'].update(support='SUPPORTED', missing=[], requirements=[dict(text='TEST_ONLY原样诊断')],
                               proposed_parameters=dict(m=85, I_pitch=20, T_main_max=60, T_nose_max=25))
        self.ui.refresh()
        self.assertNotIn('J', self.ui.ctx['plan']['proposed_parameters'])
        self.ui.show_plan(self.ui.ctx['plan'])
        self.ui.handle('/defer 未提供轮转惯量')
        self.ui.handle('确认执行')
        self.assertEqual([action for action, _ in self.runner.calls], ['confirm-request', 'run-request'])
        self.assertNotIn('deferred_input', self.ui.selection)

    def test_help_and_missing_context_plan_disclose_consultation_limits(self):
        self.ui.handle('/defer 暂缺资料')
        self.out.clear()
        self.ui.show_context()
        self.ui.show_plan(self.ui.ctx['plan'])
        self.ui.help()
        rendered = '\n'.join(self.out)
        for phrase in ('/ask', '/defer', '/new', '原预算HTTP', '原截止', '可以咨询假设及其影响',
                       '缺项仍未解决', '不会自动填值', '不代表已批准按假设执行', '已知缺陷仍会如实报告'):
            self.assertIn(phrase, rendered)
        self.assertNotIn('轮转新家族', rendered)
        self.assertEqual(self.runner.calls, [])


if __name__ == '__main__':
    unittest.main()
