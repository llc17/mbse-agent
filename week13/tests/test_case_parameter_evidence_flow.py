"""TEST_ONLY normal-gate integration: real local persistence, no simulation.

The isolated context uses LIVE solely to avoid the worker's TEST_ONLY runtime
evidence exemption. Every executor/process/model/MCP boundary is mocked or
forbidden; synthetic PASS receipts are never real execution evidence.
"""
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
import platform
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

from week13.src import case_backend, case_checks, case_console, case_console_data
from week13.src import case_contact_family, case_contact_review, case_paths
from week13.src import case_requests, case_taskflow, case_toolchain, case_worker
from week13.src.inherited import artifact_baseline, artifact_delivery, artifact_service, model_artifact


EVIDENCE = Path(__file__).resolve().parents[1] / 'results/parameter-evidence-flow-20261026-01'


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf8')


def synthetic_spec(source):
    values = dict(m=85, I_pitch=20, T_main_max=60, T_nose_max=25, stepH=.15, stepD=.27)
    name = 'TEST_ONLY_ParameterEvidence'
    requirement = dict(text='TEST_ONLY synthetic signal final >=1; no simulation occurred',
                       source_id=source['id'], source_text=source['text'])
    model = 'model ' + name + ' ' + ' '.join('parameter Real ' + key + '=' + str(value) + ';' for key, value in values.items())
    model += ' Real signal; equation signal=2; annotation(experiment(StopTime=2,Tolerance=.01,Interval=1)); end ' + name + ';'
    attributes = ''.join('attribute ' + key + ' : Real = ' + str(value) + ';' for key, value in values.items())
    traces = ''.join('attribute ' + key + ' : String = ' + json.dumps(value) + ';'
                     for key, value in dict(requirement, model_name=name).items())
    sysml = 'package TEST_ONLY_Package { part def System {' + attributes + '} part system : System; requirement req_1 {' + traces + '} }'
    return dict(schema_version='v11-model-artifact-1', backend='model-artifact',
                artifact=dict(model_name=name, modelica_source=model, sysml_source=sysml),
                sources=[source], requirements=[requirement],
                design=dict(summary='TEST_ONLY injection fixture; no vehicle or simulation', assumptions=[],
                            parameters=[dict(name=key, value=value, unit='1', origin='model', basis='TEST_ONLY synthetic metadata') for key, value in values.items()]),
                libraries=[], settings=dict(stop_time=2, intervals=2), outputs=dict(signal='signal'),
                checks=[dict(kind='final_ge', output='signal', threshold=1, source_id=source['id'], source_text=source['text'])])


class NoLaunch:
    def __init__(self):
        self.requests = []

    def start(self, task_id):
        self.requests.append(task_id)


class LocalReadRunner:
    """Console routing to real local projections; no writable action available."""
    def __init__(self):
        self.calls = []

    def data(self, action, **options):
        if action == 'discover':
            value = case_console_data._legacy_discover()
            value['case_id'] = value['project_id']
            return value
        if action == 'context':
            return case_console_data.context(options['request_id'])
        raise AssertionError('TEST_ONLY unexpected read ' + action)

    def call(self, action, **options):
        self.calls.append((action, deepcopy(options)))
        flow = case_taskflow.TaskFlow.load(options['request_id'])
        if action == 'request-result':
            return flow.terminal_readback()
        if action == 'request-status':
            return flow.state()
        raise AssertionError('TEST_ONLY writable/product action forbidden: ' + action)


class ParameterEvidenceFlow(unittest.TestCase):
    def setUp(self):
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        # Retain only this new evidence tree for parent readback; no old roots.
        self.root = Path(tempfile.mkdtemp(prefix='TEST_ONLY_', dir=EVIDENCE))
        self.project_root = self.root / 'project'
        self.project_root.mkdir()
        self.db = self.project_root / 'projects.sqlite'
        self.context_path = self.root / 'TEST_ONLY_normal_branch_context.json'
        write(self.root / 'TEST_ONLY_NOT_REAL_EXECUTION.json', dict(
            fixture_only=True, actual_process_launches=0, actual_model_calls=0,
            reason='LIVE selects the normal guard branch only; executor and physical assessor are synthetic mocks'))
        write(self.context_path, dict(schema='v11-artifact-execution-context-1', context_id='TEST_ONLY_normal_gate',
                                     status='ACTIVE', mode='LIVE', allow_execution=True, max_tasks=3,
                                     worker_timeout_s=30, simulation_timeout_s=30))
        config = self.root / 'configs/case_runtime.json'
        write(config, dict(model='TEST_ONLY_NO_PRODUCT', thinking='TEST_ONLY', reasoning_effort='TEST_ONLY',
                           context_window_tokens=1000000, max_tokens=393216, maximum_request_reserve_cny=0))
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # flexcache imports platform.system(); supply fixture metadata rather
        # than allowing Windows' platform helper to spawn cmd for a version.
        self.stack.enter_context(patch.object(platform, 'system', return_value='Windows'))
        self.popen = self.stack.enter_context(patch.object(case_worker.subprocess, 'Popen', side_effect=AssertionError('REAL_LAUNCH_FORBIDDEN')))
        self.product = self.stack.enter_context(patch.object(case_taskflow.TaskFlow, '_runtime', side_effect=AssertionError('PRODUCT_FORBIDDEN')))
        self.mcp = self.stack.enter_context(patch.object(case_taskflow.TaskFlow, 'mcp', side_effect=AssertionError('MCP_FORBIDDEN')))
        self.stack.enter_context(patch.object(case_worker, 'require_time'))
        self.stack.enter_context(patch.object(case_worker, 'finish_process'))
        self.stack.enter_context(patch.object(case_paths, 'TASK_CONTEXT', None))
        self.stack.enter_context(patch.object(case_worker, 'WEEK12_ROOT', self.project_root))
        for target, name, value in (
                (case_requests, 'ROOT', self.root), (case_requests, 'REQUESTS_ROOT', self.project_root / 'requests'),
                (case_requests, 'LEGACY_PROJECT_ROOT', self.project_root),
                (case_console_data, 'ROOT', self.root), (case_console_data, 'PROJECT_ROOT', self.project_root)):
            self.stack.enter_context(patch.object(target, name, value))
        self.stack.enter_context(patch.object(case_taskflow, 'service', side_effect=self.service))
        self.stack.enter_context(patch.object(case_backend, 'service', side_effect=self.service))
        self.stack.enter_context(patch.object(case_contact_family, 'reviewed_stair_capability', return_value=None))
        # Only the fixture path allowance is injected. Real review source/spec/
        # manifest/CSV hashes are still checked; the physical assessor is mocked.
        self.stack.enter_context(patch.object(case_contact_review, '_base', side_effect=self.canonical_base))
        self.assessor = self.stack.enter_context(patch.object(case_checks, 'assess', return_value=dict(
            schema='TEST_ONLY_MOCK_ASSESSMENT', scope='TEST_ONLY_NO_PHYSICS_OR_SIMULATION', task_status='PASS',
            validity_status='PASS', completion_status=case_contact_review.COMPLETE, completion_time=2,
            sample_count=3, time_start=0, time_end=2, issues=[], limitations=['TEST_ONLY injected assessment, no simulation'],
            contact_validity=dict(status='PASS', scope='TEST_ONLY_MOCK', hard_contradictions=[], issues=[]))))
        self.stack.enter_context(patch.object(artifact_delivery, 'render_curves', side_effect=RuntimeError('TEST_ONLY raster disabled')))
        self.no_launch = NoLaunch()
        self.context = model_artifact.ExecutionContext.load(self.context_path)
        setup = artifact_service.ArtifactService(self.db, 'TEST_ONLY_setup', context=self.context, worker_manager=self.no_launch)
        setup.project_root = self.project_root
        self.project_id = setup.project_create('TEST_ONLY parameter evidence integration')['project']['project_id']
        source = setup.record_source('TEST_ONLY baseline synthetic fact >=1; no simulation occurred')
        source = dict(id=source['source_id'], text=source['text'])
        base = synthetic_spec(source)
        checked = model_artifact.validate_artifact(base, require_sysml=True)
        self.assertTrue(checked['ready'], checked)
        write(self.project_root / 'imported-manifest.json', dict(source_sha256=checked['source_sha256'], spec_digest=checked['spec_digest']))
        (self.project_root / 'baseline-original.mo').write_text(base['artifact']['modelica_source'], encoding='utf8', newline='')
        self.baseline = dict(record_kind='IMPORTED_REFERENCE', project_id=self.project_id, version_id='v000000',
                             task_id='TEST_ONLY_reference', spec_digest=checked['spec_digest'], source_sha256=checked['source_sha256'],
                             manifest_sha256=case_paths.sha_file(self.project_root / 'imported-manifest.json'),
                             frozen_spec=base, task_status='NOT_RUN')
        write(self.project_root / 'imported-reference.json', self.baseline)
        write(self.project_root / 'run.json', dict(project_id=self.project_id, session_id='TEST_ONLY_setup'))
        self.injected_calls = []
        self.executions = []

    def tearDown(self):
        self.stack.close()

    def service(self, db_path, session_id, *, read_only=False, **unused):
        self.assertEqual(Path(db_path), self.db)
        svc = artifact_service.ArtifactService(db_path, session_id, read_only=read_only,
                                               context=None if read_only else self.context, worker_manager=self.no_launch)
        svc.project_root = self.project_root
        return svc

    def canonical_base(self, snapshot, project_root):
        self.assertEqual(Path(project_root), self.project_root)
        self.assertEqual(snapshot['project_id'], self.project_id)
        return self.project_root / 'projects' / self.project_id / 'versions' / snapshot['version_id']

    def execute_injected(self, runtime_status):
        rid = 'TEST_ONLY_' + runtime_status
        ctx = case_requests.create_request(rid, self.project_id, 'v000000',
            'TEST_ONLY synthetic ' + runtime_status + ' parameter evidence; no simulation',
            baseline_ref=artifact_baseline.reference(self.baseline), baseline_snapshot=self.baseline,
            sources=[], db_path=self.db, project_root=self.project_root)
        svc = self.service(self.db, ctx['session_id'])
        svc.project_open(self.project_id)
        source = svc.record_source('TEST_ONLY synthetic signal >=1; no simulation occurred')
        spec = synthetic_spec(dict(id=source['source_id'], text=source['text']))
        prepared = svc.proposal_prepare(spec)
        self.assertTrue(prepared['ok'], prepared)
        proposal = svc.store.proposal(prepared['proposal']['proposal_id'])
        svc.confirm_local(proposal['proposal_id'], proposal['displayed_digest'])
        task = svc.execution_start(proposal['proposal_id'], 'case_execute_' + rid)['task']
        owner = 'TEST_ONLY_owner_' + runtime_status
        svc.store.reserve_launch(task['task_id'], owner)

        def executor(source, model_name, out, stop, intervals, simulation_timeout_s):
            self.injected_calls.append(runtime_status)
            Path(out).mkdir()
            (Path(out) / 'model_res.csv').write_text('time,signal\n0,0\n1,1\n2,2\n', encoding='utf8', newline='')
            return dict(source_sha256=hashlib.sha256(source.encode('utf8')).hexdigest(),
                        compile=dict(status='PASS', check_model='TEST_ONLY_INJECTED'), simulation=dict(status='PASS'),
                        runtime_parameter_readback=dict(status=runtime_status, actual={}, scope='TEST_ONLY_INJECTED_NO_OMC'),
                        fixture_only=dict(no_simulation=True, actual_process_launches=0))
        with patch.object(case_toolchain, 'execute_model', side_effect=executor) as injected:
            code = case_worker.execute_task(self.db, task['task_id'], owner, self.context_path)
            self.assertEqual(code, 0, svc.store.task(task['task_id']))
            self.assertEqual(injected.call_count, 1)
        staged = svc.store.task(task['task_id'])['result']
        self.assertNotIn('test_only', staged['tool_result'])
        self.assertFalse(staged['test_only'])
        manager = case_worker.ArtifactWorkerManager.__new__(case_worker.ArtifactWorkerManager)
        manager.store, manager.owner, manager.timeout_s = svc.store, owner, 30
        manager._attempts = {task['task_id']: 'TEST_ONLY_NO_PROCESS_ATTEMPT'}
        manager._processes = {task['task_id']: object()}
        manager._lock = threading.Lock()
        manager._monitor(task['task_id'], SimpleNamespace(wait=lambda **_: 0))
        final = svc.store.task(task['task_id'])
        snapshot = svc._artifact_version(final['version_id'])
        self.assertEqual(snapshot['evidence_status'], 'COMPLETE')
        self.assertTrue(snapshot['recorded_conclusions_verified'])
        self.assertEqual(final['result']['mission_assessment']['task_status'], 'PASS')
        self.assertEqual(final['result']['mission_assessment']['validity_status'], 'PASS')
        self.assertEqual(final['result']['observations'][0]['status'], 'PASS')
        self.assertTrue(Path(snapshot['data_files']['CSV']).is_file())
        self.executions.append(dict(runtime_status=runtime_status, request_id=rid, task=final,
                                    snapshot=snapshot, worker_calls_are_mock=True, actual_simulations=0))
        return ctx, final, snapshot

    def inspect_new_session_and_console(self, ctx, final, snapshot):
        new_session = self.service(self.db, 'TEST_ONLY_reopen_' + final['version_id'])
        new_session.project_open(self.project_id)
        reopened = self.service(self.db, new_session.session_id, read_only=True)._artifact_version(final['version_id'])
        self.assertEqual(reopened, snapshot)
        first = case_taskflow.TaskFlow.load(ctx['request_id']).terminal_readback()
        second = case_taskflow.TaskFlow.load(ctx['request_id']).terminal_readback()
        from week13.src.case_console_result import prepare_result
        _, execution, _, lines = prepare_result(second, request_id=ctx['request_id'])
        self.assertEqual(execution['runtime_parameter_readback']['status'], final['result']['runtime_parameter_readback']['status'])
        self.assertEqual(first['state']['latest_request_execution']['task_id'], final['task_id'])
        self.assertEqual(second['state']['latest_request_execution']['task_id'], final['task_id'])
        before_calls = list(self.injected_calls)
        runner, output = LocalReadRunner(), []
        ui = case_console.Console(runner, console_root=self.root / ('console_' + ctx['request_id']), output=output.append)
        ui.catalog = runner.data('discover')
        ui.selection.update(request_id=ctx['request_id'], baseline_version_id='v000000')
        ui.refresh()
        ui.save()
        ui.handle('/result')
        ui.handle('/status')
        ui.handle('/quit')
        fresh = case_console.Console(runner, console_root=ui.root, output=output.append)
        fresh.start()
        fresh.handle('/open ' + ctx['request_id'])
        fresh.handle('/result')
        self.assertTrue(all(action in ('request-result', 'request-status') for action, _ in runner.calls))
        self.assertEqual(self.injected_calls, before_calls)
        self.assertEqual(fresh.ctx['request_id'], ctx['request_id'])
        self.popen.assert_not_called()
        self.product.assert_not_called()
        self.mcp.assert_not_called()
        return second, execution, lines, output

    def test_unknown_and_fail_normal_gate_survive_store_results_and_cold_console_reopen(self):
        for runtime_status in ('UNKNOWN', 'FAIL'):
            with self.subTest(runtime_status=runtime_status):
                ctx, final, snapshot = self.execute_injected(runtime_status)
                self.assertEqual(final['state'], 'failed')
                self.assertEqual(final['result']['task_status'], 'UNKNOWN')
                self.assertEqual(final['result']['chain_status'], 'BLOCKED_PARAMETER_EVIDENCE')
                self.assertEqual(snapshot['task_status'], 'UNKNOWN')
                with self.assertRaisesRegex(ValueError, 'complete tool/chain'):
                    artifact_baseline.resolve(self.service(self.db, ctx['session_id'], read_only=True), artifact_baseline.reference(snapshot))
                result, execution, lines, output = self.inspect_new_session_and_console(ctx, final, snapshot)
                self.assertNotEqual(execution['task_status'], 'PASS')
                self.assertEqual(execution['recorded_chain_status'], 'BLOCKED_PARAMETER_EVIDENCE')
                self.assertIsNone(result['state']['last_task_pass'])
                self.assertIsNone(result['state']['historical_last_task_pass_record'])
                self.assertEqual(case_console_data._legacy_discover()['baselines'], [])
                self.assertNotIn('路线已完成', '\n'.join(lines))
                self.assertNotIn('路线已完成', '\n'.join(output))
        write(self.root / 'TEST_ONLY_flow-summary.json', dict(actual_simulations=0, runtime_gate_exemption=False,
              executions=self.executions, fixture_root=str(self.root), new_session_and_console_reopen=True))

    def test_parameter_pass_normal_path_remains_available_as_synthetic_control(self):
        ctx, final, snapshot = self.execute_injected('PASS')
        self.assertEqual(final['state'], 'succeeded')
        self.assertEqual(final['result']['task_status'], 'PASS')
        self.assertEqual(final['result']['chain_status'], 'PASS')
        result, execution, lines, _ = self.inspect_new_session_and_console(ctx, final, snapshot)
        self.assertEqual(execution['task_status'], 'PASS')
        self.assertEqual(result['state']['last_task_pass']['version_id'], final['version_id'])
        self.assertEqual(case_console_data._legacy_discover()['default_baseline']['version_id'], final['version_id'])
        self.assertEqual(artifact_baseline.resolve(self.service(self.db, ctx['session_id'], read_only=True), artifact_baseline.reference(snapshot))['version_id'], final['version_id'])
        write(self.root / 'TEST_ONLY_flow-summary.json', dict(actual_simulations=0, runtime_gate_exemption=False,
              executions=self.executions, fixture_root=str(self.root), control_only_not_real_PASS=True))


if __name__ == '__main__':
    unittest.main()
