"""Local terminal tests; synthetic wire/RPC, no product HTTP or dynamics.

Fixtures use their own Store/ledger. The real CMD launcher smoke reads a
synthetic isolated project and saves UI history, never a product approval.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from week13.src import case_console as console
from week13.src import case_console_data as data
from week13.src import case_requests, case_paths
from week13.tests import test_case_agent_requests as fixtures

EVIDENCE = Path(os.environ.get('MBSE_V12_TEST_EVIDENCE', str(case_paths.ROOT / 'results/test-case-console' / ('run-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:12]))))
REQUIREMENT = '把总质量改为92kg，增加部分在原重心，几何控制不变。'


class FixtureRunner:
    def __init__(self, harness):
        self.h = harness
        self.calls = []

    def data(self, action, **options):
        with patch.object(data, 'ROOT', self.h.root), patch.object(data, 'PROJECT_ROOT', self.h.projectroot):
            value = data._legacy_discover() if action == 'discover' else data.context(options['request_id'])
            if action == 'discover':
                value['case_id'] = 'fixture-case'
            return value

    def projects(self, action, **options):
        if action != 'list':
            raise AssertionError('unexpected fixture project action: '+action)
        found=self.data('discover')
        return dict(cases=[dict(case_id='fixture-case',project_id=found['project_id'],name='TEST_ONLY案例')])

    def call(self, action, **options):
        self.calls.append((action, options))
        if action == 'authorize-request':
            return case_requests.authorize_budget(options['request_id'], options['budget_id'], options['approval_text'], caps=options.get('caps'), hard_deadline_utc=options.get('hard_deadline_utc'), reuse=options.get('reuse_budget', False), planning_http=True, execution=not options.get('planning_only', False), test_only=True)
        code, result = self.h.cli([action] + console.CliRunner._options(options))
        if code:
            raise console.ActionError('隔离fixture动作失败：' + result.get('error', 'unknown'))
        return result


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        self.saved = (case_requests.ROOT, case_requests.REQUESTS_ROOT, case_requests.LEGACY_PROJECT_ROOT)
        # Keep production-style context-binding paths below Windows MAX_PATH.
        # Only retained logs and smoke records belong under the evidence root.
        self.temp = tempfile.TemporaryDirectory(prefix='ui-', dir=EVIDENCE.parent)
        original = fixtures.finish_fixture
        def seed_pass(svc, task, root, task_status='PASS'):
            return original(svc, task, root, task_status)
        with patch.object(fixtures, 'finish_fixture', seed_pass):
            self.h = fixtures.Harness(Path(self.temp.name) / 'fixture')
        # The shared old wire mock emits nested PLAN arguments; the current
        # configured transport is flat. Adapt only this isolated mock response.
        original_transport=self.h.transport
        def transport(ctx,payload):
            if not payload.get('tools'):
                return dict(id='fixture_reply',model='deepseek-flash',choices=[dict(message=dict(content='TEST_ONLY 仅解释测试结果，不证明真实任务。',reasoning_content='fixture protocol only'),finish_reason='stop')],usage=dict(prompt_tokens=20,completion_tokens=10,total_tokens=30))
            response=original_transport(ctx,payload)
            from week13.tests.test_case_flat_plan_protocol import flat
            for call in response['choices'][0]['message'].get('tool_calls',[]):
                arguments=json.loads(call['function']['arguments'])
                if call['function']['name']=='case_request_plan':arguments=flat(arguments)
                elif call['function']['name']=='proposal_prepare':arguments['spec']['sources']=[dict(id=row['id']) for row in arguments['spec']['sources']]
                call['function']['arguments']=json.dumps(arguments,ensure_ascii=False)
            return response
        self.h.transport=transport
        self.runner = FixtureRunner(self.h)
        self.out = []
        self.console_root = self.h.root / 'runs/console'
        self.ui = console.Console(self.runner, console_root=self.console_root, input_fn=lambda _: '', output=self.out.append)
        self.ui.start()
        self.ui.handle('/case open fixture-case')

    def tearDown(self):
        case_requests.ROOT, case_requests.REQUESTS_ROOT, case_requests.LEGACY_PROJECT_ROOT = self.saved
        self.temp.cleanup()

    def inputs(self, words):
        iterator = iter(words)
        self.ui.input = lambda prompt: next(iterator)

    def approve_new(self, text=REQUIREMENT):
        self.inputs(['确认预算'])
        self.ui.handle(text)
        return self.ui.ctx['request_id']

    def test_budget_declined_and_sentence_confirmation_do_not_authorize(self):
        self.inputs(['我还没有确认预算'])
        self.ui.handle('中文需求 & echo 不应执行 | 中文路径 C:\\有 空格\\测试')
        self.assertIsNone(self.ui.ctx['budget'])
        self.assertEqual([x[0] for x in self.runner.calls], ['request'])
        self.ui.handle('确认执行')
        self.ui.handle('包含确认执行这几个字不批准')
        self.assertEqual([x[0] for x in self.runner.calls], ['request'])
        self.assertEqual(self.ui.ctx['raw_request'], '中文需求 & echo 不应执行 | 中文路径 C:\\有 空格\\测试')
        self.assertIn('预算未批准', '\n'.join(self.out))

    def test_empty_budget_input_and_custom_cancel_are_not_approval(self):
        self.inputs([''])
        self.ui.handle(REQUIREMENT)
        self.assertIsNone(self.ui.ctx['budget'])
        self.inputs(['修改预算', '/cancel'])
        with self.assertRaises(console.ActionError):
            self.ui.handle('/budget')
        self.assertFalse(any(action == 'authorize-request' for action, _ in self.runner.calls))
        self.inputs(['修改预算'] + [''] * 9 + ['确认预算'])
        self.ui.handle('/budget')
        self.assertEqual(self.ui.ctx['budget']['caps']['total_tokens'], 6000000)
        self.assertEqual(self.ui.ctx['stage'], 'PROPOSED')
        self.assertEqual(sum(action == 'authorize-request' for action, _ in self.runner.calls), 1)

    def test_plan_confirm_run_and_readonly_question_actual_taskflow(self):
        rid = self.approve_new()
        self.assertEqual(self.ui.ctx['stage'], 'PROPOSED')
        self.assertEqual(self.ui.mode(), '方案待确认执行')
        self.ui.show_context()
        self.assertIn('普通文字仍为只读追问', '\n'.join(self.out))
        self.assertNotIn('run-request', [x[0] for x in self.runner.calls])
        self.ui.handle('还没确认执行，先解释限制。')
        self.assertEqual(self.runner.calls[-1][0], 'request-ask')
        self.assertFalse(any(x[0] == 'confirm-request' for x in self.runner.calls))
        self.ui.handle('确认执行')
        self.assertEqual([x[0] for x in self.runner.calls][-2:], ['confirm-request', 'run-request'])
        self.assertTrue(self.ui.ctx['terminal_saved'])
        result = self.h.flow(rid).terminal_readback()
        current=result['pipeline']['state']['latest_request_execution']
        self.assertEqual(current['task_status'],'UNKNOWN')
        self.assertEqual(current['recorded_task_status'],'FAIL')
        counts = fixtures.CaseBudget(task_context=case_requests.load_request(rid)).usage()
        self.ui.handle('为何失败？')
        after = fixtures.CaseBudget(task_context=case_requests.load_request(rid)).usage()
        self.assertEqual(after['processes'], counts['processes'])
        self.assertEqual(after['counters'], counts['counters'])
        self.assertIn('TEST_ONLY', '\n'.join(self.out))
        self.assertIn('仍可只读追问', '\n'.join(self.out))

    def test_needs_input_supplement_preserves_original_budget_deadline(self):
        parent = self.approve_new('多加一点载荷。')
        before = self.ui.ctx['budget']
        original_text = case_requests.load_request(parent)['raw_request']
        self.assertEqual(self.ui.mode(), '补充缺项（同账）')
        self.out.clear()
        self.ui.show_plan(self.ui.ctx['plan'])
        self.assertNotIn('单独输入“确认执行”', '\n'.join(self.out))
        self.ui.handle('明确总质量92kg，增加部分位于原重心。')
        child = self.ui.ctx['request_id']
        self.assertNotEqual(parent, child)
        self.assertEqual(case_requests.load_request(parent)['raw_request'], original_text)
        self.assertIn(original_text, self.ui.ctx['raw_request'])
        self.assertIn('明确总质量92kg', self.ui.ctx['raw_request'])
        after = self.ui.ctx['budget']
        self.assertEqual(after['budget_id'], before['budget_id'])
        self.assertEqual(after['hard_deadline_utc'], before['hard_deadline_utc'])
        self.assertEqual(after['ledger_path'], before['ledger_path'])
        self.assertEqual(after['used']['http_requests'], before['used']['http_requests'] + 1)
        links = [console.read(path) for path in (self.console_root / 'history').glob('*.json') if console.read(path)['kind'] == 'supplement_parent']
        self.assertEqual(links[0]['parent_request_id'], parent)

    def test_reopen_correct_request_zero_product_calls_and_no_resume(self):
        rid = self.approve_new()
        before = len(self.runner.calls)
        other = console.Console(self.runner, console_root=self.console_root, input_fn=lambda _: '', output=self.out.append)
        other.start()
        self.assertEqual(other.mode(),'首页')
        other.handle('/resume')
        self.assertEqual(other.ctx['request_id'], rid)
        self.assertEqual(len(self.runner.calls), before)
        self.assertEqual(other.selection['baseline_version_id'], 'v000002')
        other.handle('/help')
        self.assertEqual(len(self.runner.calls), before)
        other.handle('/open ' + rid)
        self.assertEqual(len(self.runner.calls), before)
        other.handle('确认执行')
        self.assertEqual(len(self.runner.calls), before)  # /open invalidates displayed review

    def test_unknown_full_and_expired_budget_never_refresh_or_call(self):
        rid = self.approve_new()
        ctx = case_requests.load_request(rid)
        budget = fixtures.CaseBudget(task_context=ctx)
        authority_before = case_requests.authority_for(ctx)
        real_data = self.runner.data
        for reason in ('UNKNOWN：有未确定请求', 'HTTP次数或金额上限已满', '预算已过期'):
            def blocked(action, **options):
                value = real_data(action, **options)
                if action == 'context':
                    value['budget']['blocked'] = reason
                return value
            self.runner.data = blocked
            before = len(self.runner.calls)
            usage_before = budget.usage()
            self.ui.handle('确认执行')
            self.ui.handle('/plan')
            self.ui.handle('追问当前结果')
            self.ui.handle('/budget')
            # A blocked question performs exactly one same-request saved-result
            # readback. It must never send a model request, plan or execution.
            self.assertEqual(self.runner.calls[before:], [('request-result', dict(request_id=rid, concise=True))])
            self.assertEqual(budget.usage(), usage_before)
            self.assertEqual(case_requests.authority_for(case_requests.load_request(rid)), authority_before)
        self.runner.data = real_data

    def test_repair_exhaustion_reports_saved_cause_without_blocking_readonly_http(self):
        rid = self.approve_new()
        ctx = case_requests.load_request(rid)
        budget = fixtures.CaseBudget(task_context=ctx)
        budget.consume('software_repair', 'TEST_ONLY_repair_1')
        failure_path = Path(ctx['root']) / 'failures/generate-1.json'
        console.write(failure_path, dict(stage='generate', attempt=1, error='ValueError: spec source text differs from trusted CLI input', repairable=True))
        self.ui.refresh()
        self.assertIsNone(self.ui.ctx['budget']['blocked'])
        self.assertIn('自动纠错 1/1 已耗尽', self.ui.ctx['last_failure']['stopped_action'])
        self.out.clear()
        before = len(self.runner.calls)
        self.ui.handle('确认执行')
        self.assertEqual(len(self.runner.calls), before)
        self.ui.handle('/status')
        self.ui.handle('/budget')
        shown = '\n'.join(self.out)
        self.assertIn('source text differs', shown)
        self.assertIn('HTTP 1/6', shown)
        self.assertIn('编译 0/2', shown)
        self.assertIn('仿真 0/2', shown)
        self.ui.handle('只读解释当前方案限制')
        self.assertEqual(self.runner.calls[-1][0], 'request-ask')
        console.write(Path(ctx['root']) / 'generation-revalidated.json', dict(schema='v12-revalidated-generation-candidate-1', attempt=1, request_digest='TEST_ONLY_WRONG_BINDING'))
        self.ui.refresh()
        self.assertIn('stopped_action', self.ui.ctx['last_failure'])
        self.assertIn('不能安全读回', self.ui.ctx['last_failure']['revalidation_error'])
        console.write(Path(ctx['root']) / 'generation.json', dict(TEST_ONLY='accepted generation evidence'))
        self.ui.refresh()
        self.assertNotIn('stopped_action', self.ui.ctx['last_failure'])
        # A filename with an unbound TEST_ONLY object is not a real generation
        # receipt and cannot restore the executable/pending-confirmation mode.
        self.assertEqual(self.ui.mode(), '执行状态UNKNOWN（只读核实）')

    def test_http_full_only_blocks_product_calls_not_all_existing_execution(self):
        self.approve_new()
        authority = case_requests.authority_for(case_requests.load_request(self.ui.ctx['request_id']))
        authority = dict(authority, caps=dict(authority['caps'], http_requests=1))
        summary = data.ledger_summary(authority)
        self.assertIsNone(summary['blocked'])
        self.assertIn('产品HTTP次数额度已满', summary['http_blocked'])
        real_data = self.runner.data
        def status(action, **options):
            value = real_data(action, **options)
            if action == 'context':
                value['budget'] = summary
            return value
        self.runner.data = status
        self.assertTrue(self.ui.budget_guard(execute=True))
        self.assertFalse(self.ui.budget_guard())

    def test_question_and_unsupported_are_not_executable(self):
        self.approve_new('把轮径改成更大。')
        before = len(self.runner.calls)
        self.ui.handle('确认执行')
        self.assertEqual(len(self.runner.calls), before)
        plan = dict(self.ui.ctx['plan'], intent='QUESTION', support='SUPPORTED', unsupported=[])
        self.out.clear()
        self.ui.show_plan(plan)
        self.assertIn('NO_EXECUTABLE_PLAN', '\n'.join(self.out))
        self.assertNotIn('单独输入“确认执行”', '\n'.join(self.out))

    def test_actual_partial_unknown_and_readonly_review_result_shapes(self):
        rid = self.approve_new()
        original = self.h.transport
        self.h.outcome_status = 'PASS'
        def timeout_narration(ctx, payload):
            if not payload.get('tools'):
                import httpx
                raise httpx.ReadTimeout('TEST_ONLY explain UNKNOWN')
            return original(ctx, payload)
        self.h.transport = timeout_narration
        self.ui.handle('确认执行')
        partial = self.h.flow(rid).terminal_readback()
        self.assertEqual(partial['partial']['explanation']['status'], 'UNKNOWN')
        latest = partial['state']['latest_request_execution']
        self.assertEqual(latest['task_status'], 'UNKNOWN')
        self.assertEqual(latest['recorded_task_status'], 'PASS')
        self.assertEqual(latest['current_contact_review']['status'], 'TEST_ONLY')
        saved_partial = Path(case_requests.load_request(rid)['root']) / 'pipeline-partial.json'
        saved_before = saved_partial.read_bytes()
        budget = fixtures.CaseBudget(task_context=case_requests.load_request(rid))
        usage_before = budget.usage()
        concise = self.h.flow(rid).terminal_readback(concise=True)
        self.assertEqual(concise['saved_assessment_status'], 'NEEDS_RECHECK')
        self.assertTrue(concise['execution']['test_only'])
        self.assertEqual(concise['execution']['task_status'], 'UNKNOWN')
        self.assertEqual(concise['execution']['recorded_task_status'], 'PASS')
        self.out.clear()
        before = len(self.runner.calls)
        self.ui.handle('/result')
        text = '\n'.join(self.out)
        self.assertIn('TEST_ONLY', text)
        self.assertIn('执行版本：v000003', text)
        self.assertIn('简洁结果未核查解释正文', text)
        self.assertNotIn('历史记录任务：PASS', text)
        self.assertEqual(self.runner.calls[before:], [('request-result', dict(request_id=rid, concise=True))])
        self.out.clear()
        self.ui.handle('/result full')
        text = '\n'.join(self.out)
        self.assertIn('TEST_ONLY', text)
        self.assertIn('历史记录任务：PASS', text)
        self.assertIn('任务：未知', text)
        self.assertIn('产品解释：未知', text)
        self.assertEqual(self.runner.calls[-1], ('request-result', dict(request_id=rid, concise=False)))
        from week13.src.case_taskflow import bootstrap_review
        review = 'console_review_fixture'
        bootstrap_review(review, rid, 'v000003', '只读查看已有结果', self.h.root / 'runs/review' / review, 'explain')
        self.ui.handle('/open ' + review)
        self.out.clear()
        self.ui.handle('/result')
        self.assertIn('本次仅只读查看已绑定历史结果', '\n'.join(self.out))
        self.assertIn('v000003', '\n'.join(self.out))
        self.assertIn('TEST_ONLY', '\n'.join(self.out))
        self.assertEqual(self.runner.calls[-1], ('request-result', dict(request_id=review, concise=True)))
        self.out.clear()
        self.ui.handle('/result full')
        self.assertIn('本只读请求绑定的已有运行结果', '\n'.join(self.out))
        self.assertIn('v000003', '\n'.join(self.out))
        self.assertIn('任务：未知', '\n'.join(self.out))
        self.assertIn('历史记录任务：PASS', '\n'.join(self.out))
        state = self.h.flow(review).state()
        self.assertIsNone(state['latest_request_execution'])
        self.assertEqual(state['reviewed_execution']['version_id'], 'v000003')
        self.assertEqual(saved_partial.read_bytes(), saved_before)
        self.assertEqual(budget.usage(), usage_before)

    def test_eof_and_ctrlc_at_prompt_submit_nothing(self):
        for error in (EOFError, KeyboardInterrupt):
            before = len(self.runner.calls)
            ui = console.Console(self.runner, console_root=self.console_root, input_fn=lambda _: (_ for _ in ()).throw(error()), output=self.out.append)
            self.assertEqual(ui.loop(), 0)
            self.assertEqual(len(self.runner.calls), before)

    def test_interrupted_action_persists_block_and_does_not_resume(self):
        rid = self.approve_new()
        calls = []
        def interrupt(*args, **kwargs):
            calls.append(args[0])
            raise console.ActionInterrupted('fixture中断：结果UNKNOWN')
        self.runner.call = interrupt
        self.inputs(['确认执行'])
        self.assertEqual(self.ui.loop(), 0)
        self.assertEqual(calls, ['confirm-request'])
        self.assertIn(rid, console.read(self.console_root / 'selection.json')['cases']['fixture-case']['interrupted_requests'])
        ui = console.Console(self.runner, console_root=self.console_root, input_fn=lambda _: '', output=self.out.append)
        ui.start()
        ui.handle('/resume')
        ui.handle('确认执行')
        self.assertEqual(calls, ['confirm-request'])


class LauncherTests(unittest.TestCase):
    def test_error_classification_keeps_transport_protocol_and_reserve_distinct(self):
        self.assertEqual(console.classify_error('RuntimeError: V12 software_repair allowance exhausted')[0], 'software_repair')
        self.assertEqual(console.classify_error('HTTP_CHILD_TRANSPORT_ERROR')[0], 'unknown')
        self.assertEqual(console.classify_error('HTTP child payload differs')[0], 'action_error')
        self.assertEqual(console.classify_error('HTTP receipt binding differs')[0], 'action_error')
        self.assertEqual(console.classify_error('V12 product HTTP/tokens/CNY quota reached')[0], 'product_quota')
        self.assertEqual(console.classify_error('protects required explanation/reopen HTTP slots')[0], 'http_reserved')

    def test_subprocess_progress_changes_and_original_error_preserved(self):
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='ui-progress-', dir=EVIDENCE) as folder:
            output = []
            runner = console.CliRunner(folder, output=output.append)
            result = runner._run('week13.tests.test_case_console', ['--progress-fixture'], 'TEST_ONLY_progress')
            self.assertEqual(result, dict(TEST_ONLY=True))
            self.assertEqual(sum('等待产品模型' in line for line in output), 1)
            self.assertEqual(sum('校验双源码' in line for line in output), 1)
            self.assertTrue(all('已用' in line for line in output))
            self.assertFalse(any('编译' in line for line in output))
            self.assertIn('HTTP 3/6', '\n'.join(output))
            with self.assertRaises(console.ActionError) as raised:
                runner._run('week13.tests.test_case_console', ['--error-fixture'], 'TEST_ONLY_error')
            self.assertEqual(raised.exception.kind, 'software_repair')
            self.assertIn('RuntimeError: V12 software_repair allowance exhausted', str(raised.exception))
            self.assertNotIn('预算到限', str(raised.exception))
            records = [console.read(path) for path in (Path(folder) / 'operations').glob('*.json') if not path.name.endswith('.progress.json')]
            self.assertEqual(len(records), 2)
            self.assertTrue(all(record['ownership_established_before_module_dispatch'] for record in records))
    @unittest.skipUnless(os.name == 'nt', 'CMD launcher')
    def test_actual_cmd_help_start_status_help_exit(self):
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
        env.pop('MBSE_V12_REQUEST_ID', None)
        help_result = subprocess.run(['cmd.exe', '/d', '/c', 'v12 --help'], cwd=case_paths.ROOT, env=env, input='', stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf8', timeout=30)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn('中文持续对话终端', help_result.stdout)
        with tempfile.TemporaryDirectory(prefix='console-cmd-', dir=EVIDENCE) as folder:
            from week13.tests.case_console_cmd_fixture import configure_child, seed, file_hashes
            fixture_root = Path(folder) / 'fixture'
            seed(fixture_root)
            before = file_hashes(fixture_root)
            env = configure_child(env, fixture_root, Path(folder) / 'hook')
            commands = '/case list\n/case open TEST_ONLY_cmd_case\n/compare v000002 v000003\n/status\n/help\n/quit\n'
            started = subprocess.run(['cmd.exe', '/d', '/c', 'v12', '--console-root', str(Path(folder) / 'console')], cwd=case_paths.ROOT, env=env, input=commands, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf8', timeout=45)
            records = []
            for operation in (Path(folder) / 'console/operations').glob('*.json'):
                value = console.read(operation)
                records.append(value)
                if value.get('stage') == 'case_compare':
                    console.write(EVIDENCE / 'ui-cmd-compare-operation.json', value)
            self.assertTrue(records)
            self.assertLessEqual({row['stage'] for row in records}, {'discover', 'case_list', 'case_compare'})
            comparisons = [json.loads(row['stdout']) for row in records if row['stage'] == 'case_compare']
            self.assertEqual(len(comparisons), 1)
            comparison = comparisons[0]
            self.assertEqual(comparison['case_id'], 'TEST_ONLY_cmd_case')
            self.assertEqual(comparison['left']['version_id'], 'v000002')
            self.assertEqual(comparison['right']['version_id'], 'v000003')
            self.assertEqual(comparison['left']['parameters']['m'], 90)
            self.assertEqual(comparison['right']['parameters']['m'], 92)
            self.assertEqual(comparison['right']['completion_time'], 17.25)
            self.assertEqual(comparison['right']['task_status'], 'UNKNOWN')
            self.assertEqual(comparison['right']['recorded_task_status'], 'PASS')
            self.assertEqual(comparison['right']['current_contact_review']['status'], 'TEST_ONLY')
            self.assertEqual(file_hashes(fixture_root), before)
        self.assertEqual(started.returncode, 0, started.stderr + started.stdout)
        self.assertIn('V12 中文交互终端', started.stdout)
        self.assertIn('TEST_ONLY_cmd_case', started.stdout)
        self.assertIn('"m": 92.0', started.stdout)
        self.assertIn('v000003', started.stdout)
        self.assertIn('尚未选择请求', started.stdout)
        self.assertIn('版本对比（只读）', started.stdout)
        self.assertIn('完成时间：17.25', started.stdout)
        self.assertIn('已恢复该案例的需求、基线和会话记忆', started.stdout)
        self.assertNotIn('Traceback', started.stderr)
        console.write(EVIDENCE / 'ui-cmd-startup-smoke.json', dict(help=dict(returncode=help_result.returncode, stdout=help_result.stdout, stderr=help_result.stderr), startup=dict(returncode=started.returncode, stdin=commands, stdout=started.stdout, stderr=started.stderr), isolated_console_root=True, product_http=0, real_mcp=0, compile=0, simulation=0))

    @unittest.skipUnless(os.name == 'nt', 'Windows process tree')
    def test_gate_prevents_early_spawn_and_job_stops_descendant_on_interrupt(self):
        # The module tries to spawn immediately, while Job assignment is
        # deliberately delayed. No module action may happen before release.
        with tempfile.TemporaryDirectory(prefix='console-process-', dir=EVIDENCE) as folder:
            pid_file = Path(folder) / 'pid.json'
            original_popen, original_job = subprocess.Popen, console.WindowsJob
            seen = {}
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            def delayed_job(proc):
                time.sleep(.3)
                self.assertFalse(pid_file.exists(), 'module dispatched before Job ownership')
                seen['gate_was_closed'] = True
                job = original_job(proc)
                seen['job_ready'] = True
                return job
            def interrupting_popen(*args, **kwargs):
                proc = original_popen(*args, **kwargs)
                original_communicate = proc.communicate
                first = True
                def communicate(*call_args, **call_kwargs):
                    nonlocal first
                    if not first or not seen.get('job_ready'):
                        return original_communicate(*call_args, **call_kwargs)
                    first = False
                    limit = time.monotonic() + 10
                    while not pid_file.exists() and time.monotonic() < limit:
                        time.sleep(.05)
                    self.assertTrue(pid_file.exists())
                    seen['child_pid'] = console.read(pid_file)['pid']
                    seen['child_handle'] = kernel.OpenProcess(0x00100000, False, seen['child_pid'])
                    raise KeyboardInterrupt
                proc.communicate = communicate
                return proc
            runner = console.CliRunner(Path(folder) / 'ui', output=lambda _: None)
            with patch.object(console, 'WindowsJob', delayed_job), patch.object(console.subprocess, 'Popen', interrupting_popen):
                with self.assertRaises(console.ActionInterrupted):
                    runner._run('week13.tests.test_case_console', ['--owned-child', str(pid_file)], 'fixture_gate_interrupt')
            try:
                self.assertTrue(seen['gate_was_closed'])
                self.assertTrue(seen['child_handle'])
                self.assertEqual(kernel.WaitForSingleObject(seen['child_handle'], 10000), 0)
                evidence = console.read(next((Path(folder) / 'ui/operations').glob('*.json')))
                self.assertTrue(evidence['ownership_established_before_module_dispatch'])
                self.assertTrue(evidence['local_cli_exited'])
                self.assertFalse(evidence['remote_cancellation_proven'])
            finally:
                if seen.get('child_handle'):
                    kernel.CloseHandle(seen['child_handle'])


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--owned-child':
        child = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', '-c', 'import time; time.sleep(60)'], creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        console.write(sys.argv[2], dict(pid=child.pid))
        time.sleep(60)
    elif len(sys.argv) > 1 and sys.argv[1] == '--progress-fixture':
        from week13.src.case_progress import emit_progress
        for _ in range(4):
            emit_progress('generate', '等待产品模型', http_requests=3, http_requests_cap=6)
            time.sleep(.15)
        emit_progress('validation', '校验双源码', http_requests=3, http_requests_cap=6)
        time.sleep(.45)
        print(json.dumps(dict(TEST_ONLY=True)))
    elif len(sys.argv) > 1 and sys.argv[1] == '--error-fixture':
        print(json.dumps(dict(error='RuntimeError: V12 software_repair allowance exhausted')))
        raise SystemExit(1)
    else:
        unittest.main()
