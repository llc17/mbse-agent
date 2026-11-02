"""TEST_ONLY plan layout and review gates; no HTTP or production writes."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from week13.src import case_console as console
from week13.src import case_console_format as formatting
from week13.tests.test_case_plan_review_console import ReadOnlyRunner


OUT = Path(__file__).resolve().parents[1] / 'results/plan-layout-20261022-01'


class PlanLayoutTests(unittest.TestCase):
    def setUp(self):
        OUT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='layout-', dir=OUT)
        self.out = []
        self.plan = dict(intent='CHANGE', support='SUPPORTED', missing=[], unsupported=[],
                         requirements=[dict(text='仅将楼梯高度改为190mm。', source_text='TEST_ONLY来源逐字保留')],
                         proposed_parameters=dict(stepH=.19, stepD=.25),
                         design=dict(summary='TEST_ONLY设计概要', parameters=[dict(name='stepH', value=.19, unit='m', basis='TEST_ONLY高度依据', origin='model')],
                                     assumptions=[dict(text='TEST_ONLY重心假设', origin='model')]),
                         defaults=['TEST_ONLY保持车辆及控制', '所选原基准缺少SysML。'],
                         unknowns=['TEST_ONLY未知结果'], rationale='TEST_ONLY完整理由',
                         scope=dict(geometry_control='TEST_ONLY继承原控制', nested=dict(allowed=['stepH', 'stepD']), TEST_ONLY_hash='a' * 64))
        self.ctx = dict(request_id='TEST_ONLY_layout', project_id='TEST_ONLY_project',
                        baseline_version_id='v000003', baseline_parameters=dict(m=85, stepH=.18, stepD=.25),
                        raw_request='TEST_ONLY用户原文：改190mm，深度250mm不变。',
                        stage='PROPOSED', plan=self.plan, displayed_digest='TEST_ONLY_original_digest',
                        root=self.temp.name, budget=dict(blocked=None, permissions=dict(execution=True)),
                        verified_source_facts=dict(selected_baseline_sysml_present=True, selected_baseline_sysml_sha256='a' * 64),
                        plan_review_warnings=['TEST_ONLY更正：当前冻结基线已有SysML。'])
        self.runner = ReadOnlyRunner(self.ctx)
        self.ui = console.Console(self.runner, console_root=Path(self.temp.name) / 'console', output=self.out.append)
        self.ui.catalog = dict(project_id='TEST_ONLY_project', baselines=[dict(version_id='v000000', parameters=dict(m=85, stepH=.15, stepD=.28))])
        self.ui.ctx = deepcopy(self.ctx)
        self.ui.selection.update(request_id=self.ctx['request_id'], baseline_version_id='v000000')

    def tearDown(self):
        self.temp.cleanup()

    def render(self, *, brief=False, columns=100):
        self.out.clear()
        with patch.object(console, 'terminal_columns', return_value=columns):
            self.ui.show_plan(self.plan, brief=brief)
        return '\n'.join(self.out)

    def test_current_frozen_baseline_and_changed_inherited_groups(self):
        text = self.render()
        changed = text.split('一、本次变更参数', 1)[1].split('二、保持不变的参数', 1)[0]
        kept = text.split('二、保持不变的参数', 1)[1].split('三、需求与来源原文', 1)[0]
        self.assertIn('楼梯高度（stepH）：180 mm（0.18 m） → 190 mm（0.19 m）', changed)
        self.assertNotIn('stepD', changed)
        self.assertIn('楼梯深度（stepD）：250 mm（0.25 m）（保持不变）', kept)
        self.assertIn('总质量（m）：85 kg', kept)
        self.assertIn('v000003（当前请求冻结基线）', text)
        self.assertNotIn('150 mm', text)
        self.assertNotIn('280 mm', text)

    def test_decimal_mm_display_is_exact_and_not_magnified_or_rounded(self):
        for metres, millimetres in ((.19, '190'), (.190123456789123, '190.123456789123'),
                                   (.00000001, '0.00001'), (1.001, '1001')):
            with self.subTest(metres=metres):
                self.assertEqual(formatting.parameter_value('stepH', metres), millimetres + ' mm（' + str(metres) + ' m）')
        self.assertEqual(formatting.parameter_value('m', 85), '85 kg')

    def test_narrow_cjk_wrapping_preserves_all_content_and_plan(self):
        long_text = 'TEST_ONLY长段落中文依据，保留全部字符与空格 abcdefghijklmnopqrstuvwxyz；' * 12
        self.plan['design']['parameters'][0]['basis'] = long_text
        self.plan['defaults'].append(long_text)
        original = deepcopy(self.plan)
        text = self.render(columns=34)
        self.assertTrue(all(formatting.display_width(line) <= 34 for line in self.out))
        recovered = ''.join(line.lstrip() for line in self.out)
        self.assertEqual(recovered.count(long_text), 2)
        self.assertIn('a' * 64, recovered)
        self.assertNotIn('…', text)
        self.assertEqual(self.plan, original)
        paragraph = '中文窄屏 ABC exact   spaces；尾部不丢失。' * 5
        lines = list(formatting.wrapped_lines(paragraph, 19, continuation=''))
        self.assertEqual(''.join(lines), paragraph)
        self.assertTrue(all(formatting.display_width(line) <= 19 for line in lines))

    def test_full_retains_sources_assumptions_defaults_unknown_and_raw_scope(self):
        original = deepcopy(self.plan)
        text = self.render()
        for token in ('TEST_ONLY用户原文', 'TEST_ONLY来源逐字保留', 'TEST_ONLY设计概要', 'TEST_ONLY高度依据',
                      'TEST_ONLY重心假设', 'TEST_ONLY保持车辆及控制', 'TEST_ONLY未知结果',
                      'TEST_ONLY完整理由', 'geometry_control', 'nested', 'allowed', 'stepD'):
            self.assertIn(token, text)
        self.assertIn('1：TEST_ONLY重心假设', text)
        self.assertNotIn('origin：', text)
        self.assertNotIn('text：', text)
        self.assertLess(text.index('所选原基准缺少SysML。'), text.index('已核事实更正：'))
        self.assertLess(text.index('已核事实更正：'), text.index('明确同意后单独输入“确认执行”'))
        self.assertEqual(self.plan, original)

    def test_needs_input_and_unsupported_remain_visible_without_review_gate(self):
        self.plan.update(support='NEEDS_INPUT', missing=['TEST_ONLY必须补充楼梯级数'], unsupported=['TEST_ONLY不得改变控制'])
        self.ui.ctx['plan'] = deepcopy(self.plan)
        self.ui.selection.update(displayed_digest='OLD', displayed_request_id='OLD')
        text = self.render()
        self.assertIn('TEST_ONLY必须补充楼梯级数', text)
        self.assertIn('TEST_ONLY不得改变控制', text)
        self.assertIn('NO_EXECUTABLE_PLAN', text)
        self.assertNotIn('单独输入“确认执行”', text)
        self.assertNotIn('displayed_digest', self.ui.selection)

    def test_no_executable_target_does_not_claim_inherited_design(self):
        for changes in (dict(proposed_parameters={}), dict(intent='QUESTION'), dict(support='UNSUPPORTED')):
            with self.subTest(changes=changes):
                self.plan.update(intent='CHANGE', support='SUPPORTED', proposed_parameters=dict(stepH=.19, stepD=.25))
                self.plan.update(changes)
                self.ui.ctx['plan'] = deepcopy(self.plan)
                text = self.render()
                self.assertIn('基线参考值（尚无可执行目标）', text)
                self.assertNotIn('（保持不变）', text)

    def test_brief_uses_saved_plan_without_http_saving_or_new_review_marker(self):
        before = deepcopy(self.ui.selection)
        with patch.object(self.ui, 'save', side_effect=AssertionError('brief must not save')):
            self.ui.handle('/plan brief')
        text = '\n'.join(self.out)
        for token in ('方案摘要', '190 mm', '250 mm', 'TEST_ONLY未知结果', '已核事实更正：', '完整审阅请用 /plan'):
            self.assertIn(token, text)
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.ui.selection, before)
        self.ui.handle('确认执行')
        self.assertEqual(self.runner.calls, [])

    def test_brief_preserves_existing_full_review_markers(self):
        self.ui.handle('/plan')
        before = deepcopy(self.ui.selection)
        self.ui.handle('/plan brief')
        self.assertEqual(self.ui.selection, before)
        self.assertEqual(before['displayed_digest'], 'TEST_ONLY_original_digest')
        self.ui.handle('确认执行')
        self.assertEqual([item[0] for item in self.runner.calls], ['confirm-request', 'run-request'])
        self.assertEqual(self.runner.calls[0][1]['displayed_digest'], 'TEST_ONLY_original_digest')

    def test_brief_no_saved_plan_never_calls_plan_request_or_sets_review(self):
        self.runner.ctx['plan'] = None
        self.ui.handle('/plan brief')
        self.assertIn('请先用 /plan', '\n'.join(self.out))
        self.assertEqual(self.runner.calls, [])
        self.assertNotIn('displayed_digest', self.ui.selection)

    def test_terminal_brief_marks_historical_plan_without_http_or_review_mutation(self):
        self.runner.ctx['terminal_saved'] = dict(TEST_ONLY='saved outcome')
        before = deepcopy(self.ui.selection)
        self.ui.handle('/plan brief')
        text = ''.join(line.lstrip() for line in self.out)
        self.assertIn('以下为当时保存的方案；当前请求已有结果，实际运行结论请用 /result。', text)
        self.assertIn('TEST_ONLY未知结果', text)
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.ui.selection, before)

    def test_help_distinguishes_full_review_and_readonly_brief(self):
        self.ui.handle('/help')
        self.assertIn('/plan 完整审阅已保存方案', '\n'.join(self.out))
        self.assertIn('/plan brief 只读摘要', '\n'.join(self.out))


if __name__ == '__main__':
    unittest.main()
