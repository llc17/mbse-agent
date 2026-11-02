"""Stair-family discovery and terminal evidence display; no real execution.

All catalogs, request paths and terminal state are temporary. Fake read-only
version services expose stored evidence; these checks never use HTTP/MCP/OMC.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from week13.src import case_console as console
from week13.src import case_console_data as console_data
from week13.src import case_projects as projects
from week13.src.case_scope import FAMILY_VARIABLE_PARAMETERS, modelica_parameters


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'results/stairs-extension-20261021-01'


def replace_parameter(source, name, value):
    pattern = r'(\bparameter\s+(?:Real|Integer)\s+' + re.escape(name) + r'\s*=\s*)[^;]+;'
    result, count = re.subn(pattern, lambda match: match[1] + str(value) + ';', source)
    if count != 1:
        raise AssertionError(name)
    return result


class Versions:
    def __init__(self, records):
        self.records = {record['version_id']: record for record in records}

    def _artifact_version(self, version):
        return self.records[version]

    def versions_list(self):
        return {'versions': {'items': [{'version_id': key} for key in self.records if key != 'v000000']}}


class StairsConsoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = projects.REFERENCE_MODEL.read_text(encoding='utf-8-sig')
        EVIDENCE.mkdir(parents=True, exist_ok=True)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ui-local-', dir=EVIDENCE)
        self.root = Path(self.temp.name)
        self.output = []
        self.ui = console.Console(console_root=self.root / 'terminal', output=self.output.append)
        self.case = dict(case_id='TEST_ONLY_stairs', project_id='TEST_ONLY_project', name='隔离楼梯案例',
                         project_root=str(self.root), kind='IMPORTED_CASE', status='IMPORTED_REFERENCE_NOT_RUN',
                         support={'status': 'SUPPORTED'}, missing=[], input_sha256='TEST_ONLY_input')
        self.old = self.record('v000000', self.source, complete=False, sysml=False)
        candidate = replace_parameter(replace_parameter(self.source, 'stepH', '.18'), 'stepD', '.26')
        self.new = self.record('v000001', candidate)
        self.records = Versions([self.old, self.new])
        self.ui.catalog = dict(case_id=self.case['case_id'], project_id=self.case['project_id'],
                               project_label=self.case['name'], case_status=self.case['status'],
                               baselines=[], generation_obligations=['确认后补齐原基准SysML'])
        self.ui.ctx = dict(request_id='TEST_ONLY_request', baseline_version_id='v000000',
                           baseline_parameters=console_data.parameters(self.old), baseline_requires_sysml=True,
                           access_mode='CHANGE', root=str(self.root))

    def tearDown(self):
        self.temp.cleanup()

    def record(self, version, source, *, complete=True, sysml=True):
        artifact = dict(model_name='ZLTECH_Full_Mission', modelica_source=source)
        if sysml:
            artifact['sysml_source'] = 'TEST_ONLY_STORED_SYSML'
        spec = dict(artifact=artifact, settings=dict(stop_time=120, intervals=2400), outputs={}, checks=[], libraries=[])
        return dict(version_id=version, task_id='TEST_ONLY_task_' + version, record_kind='EXECUTION' if complete else 'IMPORTED_REFERENCE',
                    frozen_spec=spec, spec_digest='TEST_ONLY_spec_' + version,
                    source_sha256=hashlib.sha256(source.encode('utf8')).hexdigest(), manifest_sha256='TEST_ONLY_manifest_' + version,
                    tool_status='PASS' if complete else 'NOT_RUN', chain_status='PASS' if complete else 'NOT_RUN',
                    task_status='PASS' if complete else 'NOT_RUN', evidence_status='COMPLETE' if complete else 'IMPORTED_REFERENCE',
                    outcome=dict(mission_assessment=dict(completion_time=34, task_status='PASS'), tool_result=dict(actual_run_settings={'solver': 'dassl'})) if complete else {})

    def project_patches(self):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch.object(projects, 'ROOT', self.root))
        stack.enter_context(patch.object(projects, 'list_cases', return_value=[self.case]))
        stack.enter_context(patch.object(projects, 'resolve_case', return_value=self.case))
        stack.enter_context(patch.object(projects, 'verify_case', side_effect=lambda case: case))
        stack.enter_context(patch.object(projects, '_service', return_value=self.records))
        return stack

    def plan(self):
        parameters = modelica_parameters(self.new['frozen_spec']['artifact']['modelica_source'])[0]
        return dict(intent='CHANGE', support='SUPPORTED', missing=[], unsupported=[], requirements=[],
                    proposed_parameters={key: parameters[key] for key in ('m', 'I_pitch', 'T_main_max', 'T_nose_max', 'stepH', 'stepD')},
                    design=dict(parameters=[dict(name=key, value=parameters[key], unit='m', basis='TEST_ONLY') for key in ('stepH', 'stepD')]))

    def test_source_family_accepts_height_depth_and_old_mass_torque(self):
        self.assertEqual(FAMILY_VARIABLE_PARAMETERS, ('m', 'T_main_max', 'T_nose_max', 'stepH', 'stepD'))
        for name, value in (('stepH', '.18'), ('stepD', '.26'), ('m', '90'), ('T_main_max', '61'), ('T_nose_max', '26')):
            with self.subTest(name=name):
                support = projects.support_for_source(replace_parameter(self.source, name, value))
                self.assertEqual(support['status'], 'SUPPORTED')
                self.assertFalse(support['execution_authorized'])
                self.assertEqual(support['physical_acceptance'], 'NOT_VERIFIED')
        self.assertEqual(projects.support_for_source(self.new['frozen_spec']['artifact']['modelica_source'])['status'], 'SUPPORTED')

    def test_invalid_stairs_are_rejected(self):
        for name in ('stepH', 'stepD'):
            for value in ('0', '-.1', '1e309', 'NaN', '1/0'):
                with self.subTest(name=name, value=value):
                    self.assertEqual(projects.support_for_source(replace_parameter(self.source, name, value))['status'], 'UNSUPPORTED')

    def test_dependency_formula_geometry_and_control_remain_locked(self):
        for name, value in (('x_end_up', '7.2'), ('x_start_down', '9.2'), ('x_end_down', '14.4'), ('H_peak', '3.6'), ('R_main', '.14'), ('I_pitch', '21'), ('len_flat', '3'), ('n_steps', '19')):
            with self.subTest(name=name):
                self.assertEqual(projects.support_for_source(replace_parameter(self.source, name, value))['status'], 'UNSUPPORTED')
        changed = self.source.replace('Real target_speed = 0.4;', 'Real target_speed = 0.5;')
        self.assertNotEqual(changed, self.source)
        self.assertEqual(projects.support_for_source(changed)['status'], 'UNSUPPORTED')

    def test_discovery_includes_actual_version_stairs_and_sysml_need(self):
        (self.root / 'configs').mkdir()
        (self.root / 'configs/case_runtime.json').write_bytes((ROOT / 'configs/case_runtime.json').read_bytes())
        with self.project_patches():
            value = projects.discover_case(self.case['case_id'])
        a, b = value['baselines']
        self.assertEqual((a['parameters']['m'], a['parameters']['stepH'], a['parameters']['stepD']), (85, .15, .28))
        self.assertEqual((b['parameters']['stepH'], b['parameters']['stepD']), (.18, .26))
        self.assertTrue(a['requires_sysml'])
        self.assertFalse(b['requires_sysml'])
        self.assertEqual(value['default_baseline']['version_id'], 'v000001')
        self.assertEqual(console_data.parameters(self.new)['stepH'], .18)

    def test_current_support_is_derived_without_rewriting_import_history(self):
        self.case['support'] = dict(status='UNSUPPORTED', scope='HISTORICAL_ONLY 原楼梯质量扭矩范围')
        original_case = deepcopy(self.case)
        record_path = self.root / 'case.json'
        record_path.write_text(json.dumps(self.case, ensure_ascii=False), encoding='utf8')
        original_bytes = record_path.read_bytes()
        (self.root / 'configs').mkdir()
        (self.root / 'configs/case_runtime.json').write_bytes((ROOT / 'configs/case_runtime.json').read_bytes())
        with self.project_patches():
            value = projects.discover_case(self.case['case_id'])
        self.assertEqual(value['support']['status'], 'SUPPORTED')
        self.assertIn('楼梯高度/深度', value['support']['scope'])
        self.assertFalse(value['support']['execution_authorized'])
        self.assertTrue(value['baselines'][0]['selectable'])
        self.assertEqual(value['historical_import_support'], original_case['support'])
        self.assertEqual(self.case, original_case)
        self.assertEqual(record_path.read_bytes(), original_bytes)

    def test_compare_same_equation_family_but_different_stair_conditions(self):
        self.old = self.record('v000000', self.source)
        self.records = Versions([self.old, self.new])
        with self.project_patches():
            result = projects.compare_versions(self.case['case_id'], 'v000000', 'v000001')
        self.assertTrue(result['task_conditions']['geometry_control_terrain_equations'])
        self.assertFalse(result['task_conditions']['terrain'])
        self.assertFalse(result['conditions_match'])
        self.assertFalse(result['directly_comparable'])
        self.assertFalse(result['automatic_performance_ranking'])
        self.assertEqual(result['parameter_changes']['stepH'], dict(before=.15, after=.18))
        self.assertAlmostEqual(result['right']['task_conditions']['terrain']['x_end_down'], 14.4)
        self.assertIn('条件不一致：terrain', result['incomparable_reasons'])

    def test_compare_imported_history_cannot_count_as_new_run(self):
        with self.project_patches():
            result = projects.compare_versions(self.case['case_id'], 'v000000', 'v000001')
        self.assertFalse(result['directly_comparable'])
        self.assertIsNone(result['left']['completion_time'])
        self.assertEqual(result['left']['task_status'], 'NOT_RUN')
        self.assertTrue(any('历史成功声明不能补作运行结果' in item for item in result['incomparable_reasons']))

    def test_terminal_baseline_and_plan_show_version_stair_binding(self):
        rows = [dict(version_id='v000000', parameters=console_data.parameters(self.old), task_status='NOT_RUN', source_sha256=self.old['source_sha256'])]
        self.ui.catalog.update(baselines=rows, default_baseline=rows[0])
        self.ui.selection['baseline_version_id'] = 'v000000'
        self.ui.show_baseline()
        self.ui.show_plan(self.plan(), review=False)
        text = '\n'.join(self.output)
        for expected in ('新需求基线：v000000', '高度 0.15 m × 深度 0.28 m', '方案绑定基线：v000000', '180 mm（0.18 m）', '260 mm（0.26 m）', self.old['source_sha256'], '拟补齐：'):
            self.assertIn(expected, text)
        self.assertFalse(self.ui.selection_path.exists())

    def test_sysml_work_uses_selected_baseline_and_only_executable_plan(self):
        self.ui.ctx.update(baseline_version_id='v000001', baseline_parameters=console_data.parameters(self.new), baseline_requires_sysml=False)
        self.ui.show_plan(self.plan(), review=False)
        self.assertNotIn('拟补齐', '\n'.join(self.output))
        self.output.clear()
        self.ui.ctx['baseline_requires_sysml'] = True
        self.ui.show_plan(dict(intent='CHANGE', support='UNSUPPORTED', missing=[], unsupported=['超域']), review=False)
        self.assertNotIn('拟补齐', '\n'.join(self.output))

    def test_result_uses_execution_stairs_never_plan_stairs(self):
        self.ui.ctx['plan'] = self.plan()
        result = dict(reused=True, state=dict(reviewed_execution=dict(version_id='v000000', task_id='TEST_ONLY_old',
                      parameters=console_data.parameters(self.old), source_sha256=self.old['source_sha256'], tool_status='NOT_RUN')))
        self.ui.show_result(result)
        text = '\n'.join(self.output)
        self.assertIn('不是新运行', text)
        self.assertIn('执行版本：v000000', text)
        self.assertIn('高度 0.15 m × 深度 0.28 m', text)
        self.assertNotIn('高度 0.18 m', text)
        self.assertIn(self.old['source_sha256'], text)
        self.output.clear()
        self.ui.show_result(dict(execution=dict(version_id='v000001', task_id='TEST_ONLY_unknown')))
        self.assertIn('楼梯高度 未知 m × 深度 未知 m', '\n'.join(self.output))

    def test_compare_console_marks_different_conditions_without_ranking(self):
        with self.project_patches():
            result = projects.compare_versions(self.case['case_id'], 'v000000', 'v000001')
        class Runner:
            def projects(self, action, **options):
                if action != 'compare':
                    raise AssertionError(action)
                return result
        self.ui.runner = Runner()
        self.ui.compare('v000000 v000001')
        text = '\n'.join(self.output)
        self.assertIn('stepH', text)
        self.assertIn('terrain', text)
        self.assertIn('不可直接排名', text)

    def test_result_preserves_pipeline_binding_when_unwrapping_mcp_task(self):
        self.ui.show_result(dict(execution=dict(parameters=console_data.parameters(self.new),
            reference=dict(source_sha256=self.new['source_sha256']),
            task=dict(version_id='v000001', task_id='TEST_ONLY_mcp_task', result=dict(tool_status='PASS')))))
        text = '\n'.join(self.output)
        self.assertIn('执行版本：v000001', text)
        self.assertIn('高度 0.18 m × 深度 0.26 m', text)
        self.assertIn(self.new['source_sha256'], text)

    def test_stairs_question_calls_only_readonly_ask(self):
        calls = []
        class Runner:
            def call(self, action, **options):
                calls.append(action)
                return dict(answer='TEST_ONLY 只读回答')
        self.ui.runner = Runner()
        self.ui.budget_guard = lambda: True
        self.ui.refresh = lambda: self.ui.ctx
        self.ui.ask('180mm高、260mm深为何更困难？')
        self.assertEqual(calls, ['request-ask'])
        self.assertFalse(self.ui.selection_path.exists())


if __name__ == '__main__':
    unittest.main()
