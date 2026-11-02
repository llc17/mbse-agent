"""Case terminal integration: isolated real catalog/Store, no real transport.

The original Modelica bytes are read as submitted input. All projects, requests,
terminal selection and TEST_ONLY authorities stay inside a temporary directory.
"""
import contextlib
import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from week13.src import case_console as console
from week13.src import case_console_data as data
from week13.src import case_paths, case_projects as projects, case_projects_cli
from week13.src import case_requests, case_taskflow, case_cli,case_contact_family

EVIDENCE = case_paths.ROOT / 'results/result-verdict-fix-20261024-01/ui-projects'
STATEMENT = '用户原文：原始85kg模型已完成全程上20级、前向下20级并从出口继续行驶。'


class LocalRunner:
    def __init__(self):
        self.calls = []
        self.project_calls = []

    def data(self, action, **options):
        return data.discover(options.get('case_id')) if action == 'discover' else data.context(options['request_id'])

    def projects(self, action, **options):
        self.project_calls.append((action, options))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = case_projects_cli.main([action] + console.CliRunner._options(options))
        result = json.loads(output.getvalue())
        if code:
            raise console.ActionError(result.get('error', 'case CLI failed'))
        return result

    def call(self, action, **options):
        self.calls.append((action, options))
        if action == 'authorize-request':
            raise AssertionError('this case isolation suite never approves a product budget')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = case_cli.main([action] + console.CliRunner._options(options))
        result = json.loads(output.getvalue())
        if code:
            raise console.ActionError(result.get('error', 'core CLI failed'))
        return result


class CaseConsoleProjectsTests(unittest.TestCase):
    def setUp(self):
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='ui-catalog-', dir=EVIDENCE)
        self.root = Path(self.temp.name)
        self.patches = contextlib.ExitStack()
        self.patches.enter_context(patch.object(projects, 'ROOT', self.root))
        self.patches.enter_context(patch.object(projects, 'LEGACY_ROOT', self.root / 'no-legacy'))
        self.patches.enter_context(patch.object(case_requests, 'ROOT', self.root))
        self.patches.enter_context(patch.object(case_requests, 'LEGACY_PROJECT_ROOT', self.root / 'no-legacy'))
        self.patches.enter_context(patch.object(case_requests, 'REQUESTS_ROOT', self.root / 'no-legacy/requests'))
        # Contact-family discovery must share this fixture's registry; real
        # project requests cannot be remapped into an isolated no-legacy tree.
        self.patches.enter_context(patch.object(case_contact_family,'REGISTRY_ROOT',self.root/'configs/contact-families'))
        (self.root / 'configs').mkdir()
        (self.root / 'configs/case_runtime.json').write_bytes((case_paths.ROOT / 'configs/case_runtime.json').read_bytes())
        self.original = self.root / 'submitted originals'
        self.original.mkdir()
        (self.original / 'route.mo').write_bytes(projects.REFERENCE_MODEL.read_bytes())
        (self.original / 'success.txt').write_text(STATEMENT, encoding='utf8')
        self.runner, self.out = LocalRunner(), []
        self.console_root = self.root / 'console'
        self.ui = console.Console(self.runner, console_root=self.console_root, input_fn=lambda _: '', output=self.out.append)
        self.ui.start()

    def tearDown(self):
        self.patches.close()
        self.temp.cleanup()

    def inputs(self, *values):
        values = iter(values)
        self.ui.input = lambda _: next(values)

    def import_a(self):
        self.inputs('TEST_ONLY 案例A')
        self.ui.handle('/case import ' + str(self.original))
        return self.ui.catalog['case_id']

    def test_fresh_import_v0_request_without_pass_and_no_approval(self):
        case_id = self.import_a()
        self.assertTrue(case_id)
        self.assertEqual(self.ui.selection['baseline_version_id'], 'v000000')
        self.assertEqual(self.ui.catalog['baselines'][0]['status'], 'IMPORTED_REFERENCE_NOT_RUN')
        self.assertNotIn('最近完整任务PASS', '\n'.join(self.out))
        self.assertEqual(self.ui.catalog['case']['historical_success_statement'], '')
        self.assertIn('这次希望在原案例上做什么改变？', '\n'.join(self.out))
        self.inputs('')
        self.ui.handle('/new 把总质量改为90kg，新增质量在原重心，几何控制不变。')
        ctx = self.ui.ctx
        self.assertEqual(ctx['baseline_version_id'], 'v000000')
        self.assertIsNone(ctx['budget'])
        self.assertEqual([action for action, _ in self.runner.calls], ['request'])
        self.assertNotIn('sysml_source', case_requests.load_request(ctx['request_id'])['baseline_snapshot']['frozen_spec']['artifact'])
        self.ui.handle('确认执行')
        self.assertEqual([action for action, _ in self.runner.calls], ['request'])
        self.assertIn('尚无明确批准', '\n'.join(self.out))

    def test_duplicate_blank_open_and_independent_replay_original_85(self):
        case_a = self.import_a()
        self.inputs('')
        self.ui.handle('/case import ' + str(self.original))
        self.assertEqual(len(projects.list_cases()), 1)
        self.inputs('打开 1')
        self.ui.handle('/case import ' + str(self.original))
        self.assertEqual(self.ui.catalog['case_id'], case_a)
        self.inputs('独立测试', '案例B')
        self.ui.handle('/case import ' + str(self.original))
        case_b = self.ui.catalog['case_id']
        self.assertNotEqual(case_a, case_b)
        self.assertIsNone(self.ui.ctx)
        self.assertIsNone(self.ui.selection.get('request_id'))
        self.assertEqual(self.ui.catalog['baselines'][0]['parameters']['m'], 85)
        self.inputs('案例A独立复测')
        self.ui.handle('/case replay ' + case_a)
        replay = self.ui.catalog['case']
        self.assertEqual(replay['origin_case_id'], case_a)
        self.assertEqual(self.ui.catalog['baselines'][0]['parameters']['m'], 85)
        self.assertNotIn('sysml_source', projects._read(Path(replay['project_root']) / 'imported-reference.json')['frozen_spec']['artifact'])
        self.assertEqual(self.ui.catalog['requests'], [])
        self.assertFalse(list(Path(replay['project_root']).rglob('budget.sqlite')))
        self.assertEqual(self.runner.calls, [])

    def test_multimodel_empty_cancel_and_explicit_path_selection(self):
        (self.original / 'another.mo').write_bytes((self.original / 'route.mo').read_bytes())
        self.inputs('')
        self.ui.handle('/case import ' + str(self.original))
        self.assertEqual(projects.list_cases(), [])
        self.inputs('route.mo', '明确完整流程')
        self.ui.handle('/case import ' + str(self.original))
        self.assertEqual(self.ui.catalog['case']['selected_model'], 'route.mo')
        self.assertEqual(self.runner.calls, [])

    def test_supported_choice_is_clear_and_accepts_number_or_unique_model_name(self):
        source = (self.original / 'route.mo').read_text(encoding='utf8')
        (self.original / 'part.mo').write_text(source + '\nparameter Real unsupportedGeometry=999;\n', encoding='utf8')
        prompts = []
        words = iter(['1', '无额外确认的案例'])
        self.ui.input = lambda prompt: (prompts.append(prompt), next(words))[1]
        self.out.clear()
        self.ui.handle('/case import ' + str(self.original))
        self.assertEqual(self.ui.catalog['case']['selected_model'], 'route.mo')
        self.assertEqual(len(prompts), 2)
        text = '\n'.join(self.out)
        self.assertIn('可用于当前改型流程的模型：\n  1. route.mo', text)
        self.assertIn('仅作资料：part.mo', text)
        self.assertNotIn('sha256', text)
        self.assertNotIn('确认导入', text)
        self.assertIn('85.0 kg', text)
        name = self.ui.catalog['case']['name']
        self.inputs('ZLTECH_Full_Mission')
        self.ui.handle('/case import ' + str(self.original))
        self.assertEqual(self.ui.catalog['case']['name'], name)  # ambiguous model name cancels
        (self.original / 'part.mo').unlink()
        (self.original / 'part.mo').write_text('model Other end Other;', encoding='utf8')
        self.inputs('ZLTECH_Full_Mission', '独立测试', '唯一名称选择')
        self.ui.handle('/case import ' + str(self.original))
        self.assertEqual(self.ui.catalog['case']['selected_model'], 'route.mo')
        self.assertEqual(self.runner.calls, [])

    def test_a_b_a_and_reopen_restore_own_request_baseline_mode(self):
        case_a = self.import_a()
        self.inputs('')
        self.ui.handle('/new A的独立需求原文')
        rid_a = self.ui.ctx['request_id']
        self.ui.selection['interrupted_requests'] = [rid_a]
        self.ui.save()
        self.inputs('案例B')
        self.ui.handle('/case replay ' + case_a)
        case_b = self.ui.catalog['case_id']
        self.inputs('')
        self.ui.handle('/new B的独立需求原文')
        rid_b = self.ui.ctx['request_id']
        self.assertNotEqual(rid_a, rid_b)
        self.assertNotIn(rid_a, self.ui.selection.get('interrupted_requests', []))
        self.ui.handle('/case open ' + case_a)
        self.assertEqual(self.ui.ctx['request_id'], rid_a)
        self.assertEqual(self.ui.ctx['raw_request'], 'A的独立需求原文')
        self.assertIn(rid_a, self.ui.selection['interrupted_requests'])
        self.ui.handle('/case open ' + case_b)
        before = len(self.runner.calls)
        reopened = console.Console(self.runner, console_root=self.console_root, input_fn=lambda _: '', output=self.out.append)
        reopened.start()
        self.assertIsNone(reopened.catalog)
        reopened.handle('/resume')
        self.assertEqual(reopened.catalog['case_id'], case_b)
        self.assertEqual(reopened.ctx['request_id'], rid_b)
        self.assertEqual(len(self.runner.calls), before)
        reopened.handle('/open ' + rid_a)
        self.assertEqual(reopened.catalog['case_id'], case_a)
        self.assertEqual(reopened.ctx['request_id'], rid_a)
        self.assertIn('明确只读切换到其所属项目', '\n'.join(self.out))
        self.assertEqual(len(self.runner.calls), before)

    def test_legacy_flat_selection_migrates_only_console_state(self):
        case_a = self.import_a()
        self.inputs('')
        self.ui.handle('/new 迁移时保持原文')
        rid = self.ui.ctx['request_id']
        original_ctx = Path(self.ui.ctx['root']) / 'task-context.json'
        before = original_ctx.read_bytes()
        console.write(self.console_root / 'selection.json', dict(request_id=rid, baseline_version_id='v000000', baseline_explicit=True, interrupted_requests=[rid]))
        restored = console.Console(self.runner, console_root=self.console_root, input_fn=lambda _: '', output=self.out.append)
        restored.start()
        self.assertIsNone(restored.catalog)
        self.assertNotIn('schema_version',console.read(self.console_root/'selection.json'))
        restored.handle('/resume')
        self.assertEqual(restored.catalog['case_id'], case_a)
        self.assertEqual(restored.ctx['request_id'], rid)
        self.assertEqual(restored.selection['interrupted_requests'], [rid])
        saved = console.read(self.console_root / 'selection.json')
        self.assertEqual(saved['schema_version'], 2)
        self.assertEqual(saved['cases'][case_a]['request_id'], rid)
        self.assertEqual(original_ctx.read_bytes(), before)

    def test_selected_old_version_is_remembered_per_case(self):
        from week13.src.case_backend import module
        from week13.tests.test_case_agent_requests import synthetic_spec, finish_fixture, NoLaunch
        case_a = self.import_a()
        row = self.ui.catalog['case']
        root = Path(row['project_root'])
        context = self.root / 'TEST_ONLY-context.json'
        console.write(context, dict(schema='v11-artifact-execution-context-1', context_id='TEST_ONLY_console_versions', status='ACTIVE', mode='TEST_ONLY', allow_execution=True, max_tasks=2, worker_timeout_s=1900, simulation_timeout_s=900))
        svc = module('artifact_service').ArtifactService(row['db_path'], row['session_id'], context=context, worker_manager=NoLaunch())
        svc.project_root = root
        source = svc.record_source('TEST_ONLY synthetic version for selection memory')
        source = dict(id=source['source_id'], text=source['text'])
        proposal = svc.proposal_prepare(synthetic_spec(source, 90))['proposal']
        svc.confirm_local(proposal['proposal_id'], proposal['displayed_digest'])
        task = svc.execution_start(proposal['proposal_id'], 'TEST_ONLY_version_selector')['task']
        finish_fixture(svc, task, root, 'PASS')
        self.ui.catalog = self.runner.data('discover', case_id=case_a)
        self.ui.select_baseline()
        # This is a TEST_ONLY receipt, so it is selectable but cannot become
        # an automatically verified default. Memory is tested after selection.
        candidate=next(row for row in self.ui.catalog['baselines'] if row['version_id']=='v000001')
        self.assertEqual(candidate['current_contact_review']['status'],'TEST_ONLY')
        self.assertFalse(candidate['automatic_default_eligible'])
        self.assertEqual(self.ui.selection['baseline_version_id'], 'v000000')
        self.ui.handle('/baseline v000001')
        self.assertEqual(self.ui.selection['baseline_version_id'], 'v000001')
        self.inputs('案例B')
        self.ui.handle('/case replay ' + case_a)
        self.assertEqual(self.ui.selection['baseline_version_id'], 'v000000')
        self.ui.handle('/case open ' + case_a)
        self.assertEqual(self.ui.selection['baseline_version_id'], 'v000001')
        self.ui.handle('/baseline v000000')
        self.assertEqual(self.ui.selection['baseline_version_id'], 'v000000')
        self.assertEqual(self.runner.calls, [])

    def test_compare_import_reference_readonly_and_cancelled_replay(self):
        case_a = self.import_a()
        before = case_paths.sha_file(Path(self.ui.catalog['case']['db_path']))
        self.ui.handle('/compare v000000 v000000')
        self.assertIn('NOT_RUN', '\n'.join(self.out))
        self.assertIn('时间仅供展示', '\n'.join(self.out))
        self.assertIn('不可直接排名', '\n'.join(self.out))
        self.assertEqual(case_paths.sha_file(Path(self.ui.catalog['case']['db_path'])), before)
        self.inputs('')
        self.ui.handle('/case replay ' + case_a)
        self.assertEqual(len(projects.list_cases()), 1)
        self.assertEqual(self.runner.calls, [])
        self.ui.handle('/case list')
        text = '\n'.join(self.out)
        self.assertIn('正式', text)
        self.assertIn('所选 v000000', text)

    def test_unsupported_input_cannot_start_request_or_budget(self):
        source = (self.original / 'route.mo').read_text(encoding='utf-8-sig')
        (self.original / 'route.mo').write_text(source + '\n// unsupported family alteration\nparameter Real unsupportedGeometry = 999;\n', encoding='utf8')
        self.import_a()
        self.assertEqual(self.ui.catalog['support']['status'], 'UNSUPPORTED')
        self.ui.handle('/new 给此超域模型做改型')
        self.assertEqual(self.runner.calls, [])
        self.assertIsNone(self.ui.ctx)

    def test_comparison_displays_conclusions_and_event_counts_without_arrays(self):
        self.import_a()
        original = self.runner.projects
        result = original('compare', case_id=self.ui.catalog['case_id'], left='v000000', right='v000000')
        mission = dict(task_status='PASS', validity_status='PASS', completion_status='COMPLETE', completion_time=34.45, descent_orientation='FORWARD_FRONT_FIRST', exit_x=15.2, rear_edge_at_completion=15.216422, post_exit_distinct_times=1711, issues=[], limitations=['TEST_ONLY no hardware certification'], events=[dict(payload='DONT_DUMP_EVENT_BODY')] * 240, terrain_events=[dict(payload='DONT_DUMP_TERRAIN_BODY')] * 240, event_order_signature=['DONT_DUMP_SIGNATURE'] * 240)
        for side in ('left', 'right'):
            result[side]['mission_assessment'] = mission
        def compare_response(action, **options):
            return result if action == 'compare' else original(action, **options)
        self.runner.projects = compare_response
        self.out.clear()
        self.ui.handle('/compare v000000 v000000')
        rendered = '\n'.join(self.out)
        self.assertIn('34.45', rendered)
        self.assertIn('FORWARD_FRONT_FIRST', rendered)
        self.assertIn('events_count', rendered)
        self.assertIn('240', rendered)
        self.assertIn('TEST_ONLY no hardware certification', rendered)
        self.assertNotIn('DONT_DUMP_', rendered)
        self.assertEqual(len(mission['events']), 240)
        self.assertEqual(self.runner.calls, [])


if __name__ == '__main__':
    unittest.main()
