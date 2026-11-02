"""Frozen-baseline plan review fixtures; no real request, HTTP or execution."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from week13.src import case_console as console
from week13.src import case_console_data as data
from week13.src import case_requests


OUT = Path(__file__).resolve().parents[1] / 'results/plan-json-suffix-20261021-01'
ORIGINAL_CLAIM = '所选原基准缺少SysML，确认后补齐原基准和候选两份SysML。'


class ReadOnlyRunner:
    def __init__(self, ctx):
        self.ctx = ctx
        self.calls = []
        self.reads = []

    def data(self, action, **options):
        self.reads.append((action, options))
        if action != 'context':
            raise AssertionError(action)
        return deepcopy(self.ctx)

    def call(self, action, **options):
        self.calls.append((action, options))
        if action not in ('request-ask', 'confirm-request', 'run-request'):
            raise AssertionError(action)
        return {'answer': 'TEST_ONLY只读回答'} if action == 'request-ask' else {}


class PlanReviewConsoleTests(unittest.TestCase):
    def setUp(self):
        OUT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='ui-fixture-', dir=OUT)
        self.root = Path(self.temp.name)
        self.plan = dict(intent='CHANGE', support='SUPPORTED', missing=[], unsupported=[],
                         requirements=[dict(text='保持105kg，仅调整楼梯尺寸。', source_text='TEST_ONLY原文')],
                         defaults=[ORIGINAL_CLAIM], unknowns=[],
                         proposed_parameters=dict(stepH=.18, stepD=.26),
                         design=dict(summary='TEST_ONLY原方案设计', parameters=[], assumptions=[]),
                         rationale='TEST_ONLY依据', scope=dict(geometry_control='继承'))
        self.ctx = dict(request_id='TEST_ONLY_plan_review', project_id='TEST_ONLY_project',
                        baseline_version_id='v000001', baseline_ref=dict(version_id='v000001'),
                        baseline_snapshot=dict(version_id='v000001', frozen_spec=dict(artifact=dict(
                            modelica_source='TEST_ONLY_MODEL', sysml_source='TEST_ONLY_COMPLETE_SYSML'))),
                        baseline_requires_sysml=True, raw_request='保持105kg，楼梯改180×260mm。',
                        root=str(self.root), plan=self.plan, stage='PROPOSED', displayed_digest='TEST_ONLY_displayed',
                        runtime_authority=None)
        self.out = []

    def tearDown(self):
        self.temp.cleanup()

    def projected(self):
        with patch.object(case_requests, 'load_request', return_value=self.ctx), \
                patch.object(data, 'parameters', return_value=dict(m=105, stepH=.15, stepD=.28)), \
                patch.object(data, 'ledger_summary', side_effect=AssertionError('no real ledger')):
            return data.context(self.ctx['request_id'])

    def ui(self, ctx=None):
        ctx = self.projected() if ctx is None else ctx
        runner = ReadOnlyRunner(ctx)
        ui = console.Console(runner, console_root=self.root / 'console', output=self.out.append)
        ui.catalog = dict(case_id='TEST_ONLY_case', project_id='TEST_ONLY_project', project_label='TEST_ONLY案例',
                          case_status='IMPORTED_REFERENCE_NOT_RUN', baselines=[
                              dict(version_id='v000000', requires_sysml=True, parameters=dict(m=85)),
                              dict(version_id='v000001', requires_sysml=False, parameters=dict(m=105))],
                          missing=['原基准缺少SysML'], generation_obligations=['原v0须补齐SysML'])
        ui.selection.update(request_id=ctx['request_id'], baseline_version_id='v000001')
        ui.ctx = deepcopy(ctx)
        return ui, runner

    def test_context_uses_frozen_snapshot_and_preserves_history_and_packet(self):
        original = deepcopy(self.ctx)
        projected = self.projected()
        facts = projected['verified_source_facts']
        self.assertTrue(facts['selected_baseline_sysml_present'])
        self.assertEqual(facts['selected_baseline_sysml_sha256'], case_requests.digest('TEST_ONLY_COMPLETE_SYSML'))
        self.assertEqual(facts['generation_source_rule'], 'CANDIDATE_ONLY')
        self.assertEqual(facts['required_source_completion'], [])
        self.assertFalse(projected['baseline_requires_sysml'])
        self.assertTrue(projected['plan_review_warnings'])
        self.assertEqual(self.ctx, original)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_empty_or_blank_sysml_is_not_claimed_present(self):
        for source in (None, '', '   '):
            with self.subTest(source=source):
                self.ctx['baseline_snapshot']['frozen_spec']['artifact']['sysml_source'] = source
                self.ctx['baseline_requires_sysml'] = False
                projected = self.projected()
                self.assertTrue(projected['baseline_requires_sysml'])
                self.assertFalse(projected['verified_source_facts']['selected_baseline_sysml_present'])
                self.assertIsNone(projected['verified_source_facts']['selected_baseline_sysml_sha256'])
                self.assertEqual(projected['plan_review_warnings'], [])
                ui, _ = self.ui(projected)
                ui.show_plan(self.plan, review=False)
                self.assertIn('拟补齐：', '\n'.join(self.out))
                self.assertNotIn('已核事实更正：', '\n'.join(self.out))
                self.out.clear()

    def test_validation_warnings_require_exact_packet_and_frozen_source_binding(self):
        facts = data.verified_source_facts(self.ctx)
        bound = dict(packet_digest=case_requests.digest(self.plan), verified_source_facts=facts,
                     review_warnings=['TEST_ONLY已绑定警告'])
        path = self.root / 'plan-validation.json'
        for variant, expected in (('valid', True), ('stale_packet', False), ('wrong_baseline', False), ('wrong_sysml', False)):
            with self.subTest(variant=variant):
                value = deepcopy(bound)
                if variant == 'stale_packet':
                    value['packet_digest'] = 'TEST_ONLY_OLD_PACKET'
                elif variant == 'wrong_baseline':
                    value['verified_source_facts']['baseline_version_id'] = 'v000000'
                elif variant == 'wrong_sysml':
                    value['verified_source_facts']['selected_baseline_sysml_sha256'] = 'TEST_ONLY_OTHER_SOURCE'
                path.write_text(json.dumps(value), encoding='utf8')
                warnings = self.projected()['plan_review_warnings']
                self.assertEqual('TEST_ONLY已绑定警告' in warnings, expected)
        path.write_text('[]', encoding='utf8')
        self.assertTrue(self.projected()['plan_review_warnings'])  # local frozen-source correction still works

    def test_conditional_completion_rule_does_not_create_false_conflict(self):
        for text in ('若原基准缺少SysML，应补齐。', '原基准缺少SysML时应补齐。', '确认后同步生成候选SysML与Modelica。'):
            self.ctx['plan']['defaults'] = [text]
            self.assertEqual(self.projected()['plan_review_warnings'], [])

    def test_original_claim_is_followed_by_clear_fact_before_explicit_approval(self):
        projected = self.projected()
        projected['budget'] = dict(blocked=None, permissions=dict(execution=True))
        ui, runner = self.ui(projected)
        original = deepcopy(self.plan)
        ui.show_plan(self.plan)
        text = '\n'.join(self.out)
        self.assertIn(ORIGINAL_CLAIM, text)
        self.assertIn('当前冻结基线 v000001 已包含SysML，无需再补原基准', text)
        self.assertIn('仅同步生成候选SysML与Modelica双源码', text)
        self.assertNotIn('拟补齐：', text)
        self.assertLess(text.index(ORIGINAL_CLAIM), text.index('已核事实更正：'))
        self.assertLess(text.index('已核事实更正：'), text.index('明确同意后单独输入“确认执行”'))
        self.assertEqual(self.plan, original)
        self.assertEqual(runner.calls, [])
        self.assertFalse((self.root / 'confirmation.json').exists())

    def test_old_context_respects_current_selected_catalog_version(self):
        projected = self.projected()
        projected.pop('verified_source_facts')
        projected.pop('plan_review_warnings')
        projected['baseline_requires_sysml'] = True  # stale old import flag
        ui, _ = self.ui(projected)
        ui.show_plan(self.plan, review=False)
        self.assertNotIn('拟补齐：', '\n'.join(self.out))
        self.assertNotIn('已核基准事实：', '\n'.join(self.out))

    def test_banner_marks_v0_gap_as_history_and_context_shows_current_fact(self):
        ui, _ = self.ui()
        ui.show_case()
        ui.show_context()
        self.assertIn('历史导入缺项（原v0）：原基准缺少SysML', self.out)
        self.assertFalse(any(line.startswith('缺项：原基准缺少SysML') for line in self.out))
        self.assertIn('当前请求冻结基线 v000001 已包含SysML', '\n'.join(self.out))
        self.assertIn('导入原始参考，未重新运行', '\n'.join(self.out))

    def test_saved_plan_is_read_only_even_when_http_or_execution_blocked(self):
        projected = self.projected()
        projected['budget'] = dict(blocked='硬总截止已到', http_blocked='HTTP额度已满', permissions=dict(execution=False))
        ui, runner = self.ui(projected)
        ui.handle('/plan')
        self.assertEqual(runner.calls, [])
        self.assertIn(ORIGINAL_CLAIM, '\n'.join(self.out))
        self.assertIn('已核事实更正：', '\n'.join(self.out))
        self.assertIn('当前预算未准许新增执行', '\n'.join(self.out))
        ui.handle('确认执行')
        self.assertEqual(runner.calls, [])

    def test_showing_plan_never_confirms_until_exact_explicit_command(self):
        projected = self.projected()
        # This isolated fixture has no on-disk task/request to inspect. State
        # inspection correctly returns UNCERTAIN for that absence; explicitly
        # model the NOT_RUN branch this confirmation test is exercising.
        projected['execution_state'] = 'NOT_RUN'
        projected['budget'] = dict(blocked=None, permissions=dict(execution=True))
        ui, runner = self.ui(projected)
        ui.handle('/plan')
        self.assertEqual(runner.calls, [])
        ui.handle('请先解释，暂不确认执行。')
        self.assertEqual([row[0] for row in runner.calls], ['request-ask'])
        ui.handle('确认执行')
        self.assertEqual([row[0] for row in runner.calls], ['request-ask', 'confirm-request', 'run-request'])
        self.assertEqual(runner.calls[-2][1]['displayed_digest'], 'TEST_ONLY_displayed')


if __name__ == '__main__':
    unittest.main()
