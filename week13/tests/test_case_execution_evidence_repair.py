"""Known terminal parameter-evidence repair: local fixtures, no real transports."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from week13.src import case_taskflow as taskflow
from week13.src import case_cli
from week13.src.case_paths import ROOT, sha_file, sha_value, save_json

EVIDENCE = ROOT / 'results/stairs-extension-20261021-01'


class FakeStore:
    def __init__(self, root, proposal):
        self.path = root / 'fixture.sqlite'
        self.proposal_record = proposal
        with self.transaction() as db:
            db.execute('CREATE TABLE proposals(proposal_id TEXT,confirmation_json TEXT)')
            db.execute('INSERT INTO proposals VALUES(?,?)', (proposal['proposal_id'], '{}'))

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def proposal(self, proposal_id):
        if proposal_id != self.proposal_record['proposal_id']:
            raise AssertionError(proposal_id)
        return self.proposal_record


class ExecutionEvidenceRepairTests(unittest.TestCase):
    def setUp(self):
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='repair-fixture-', dir=EVIDENCE)
        self.root = Path(self.temp.name)
        self.source = (ROOT / 'src/inherited/reference-full-mission-legacy.mo').read_text(encoding='utf-8-sig')
        self.spec = dict(artifact=dict(model_name='ZLTECH_Full_Mission', modelica_source=self.source, sysml_source='TEST_ONLY'))
        self.spec_digest = sha_value(self.spec)
        self.source_hash = hashlib.sha256(self.source.encode('utf8')).hexdigest()
        for name, value in [('pipeline.json', {'stage': 'ORIGINAL_KNOWN_PARAMETER_EVIDENCE_FAILURE'}), ('generation.json', {'proposal_id': 'TEST_ONLY_original', 'spec_digest': self.spec_digest}), ('old-task.json', {'state': 'failed'})]:
            save_json(self.root / name, value, immutable=True)
        self.protected = {str(path): sha_file(path) for path in self.root.glob('*.json')}
        self.ctx = dict(root=str(self.root), request_id='TEST_ONLY_repair', project_id='TEST_ONLY_project', request_digest='TEST_ONLY_request_digest', displayed_digest='TEST_ONLY_displayed_digest')
        self.snapshot = dict(project_id=self.ctx['project_id'], version_id='v000003', task_id='TEST_ONLY_new_task', spec_digest=self.spec_digest,
                             source_sha256=self.source_hash, manifest_sha256='TEST_ONLY_manifest', frozen_spec=self.spec)
        self.proposal = dict(proposal_id='TEST_ONLY_new_proposal', spec=deepcopy(self.spec), spec_digest=self.spec_digest, state='awaiting_confirmation', displayed_digest='TEST_ONLY_display')
        self.store = FakeStore(self.root, self.proposal)
        self.confirmed = []
        fixture = self
        class Service:
            store = fixture.store
            def _artifact_version(self, version):
                if version != fixture.snapshot['version_id']:
                    raise AssertionError(version)
                return fixture.snapshot
            def confirm_local(self, proposal_id, digest):
                fixture.confirmed.append((proposal_id, digest))
                fixture.proposal['state'] = 'approved'
        self.service = Service()
        self.calls = []
        async def rpc(flow, name, arguments):
            self.calls.append((name, deepcopy(arguments)))
            if name in ('proposal_prepare', 'proposal_get'):
                return dict(proposal=dict(proposal_id=self.proposal['proposal_id']))
            if name == 'execution_start':
                return dict(task=dict(task_id=self.snapshot['task_id'], version_id=self.snapshot['version_id'], state='succeeded', result=dict(worker_exit_code=0, tool_status='PASS', chain_status='PASS', task_status='PASS')))
            raise AssertionError(name)
        self.flow = taskflow.TaskFlow(self.ctx, rpc=rpc)
        self.flow.refresh = lambda: self.ctx
        self.flow.open = lambda **_: self.service
        self.flow.state = lambda: dict(latest_request_execution=dict(task_id=self.snapshot['task_id'], version_id='v000003'))
        self.flow.ask = AsyncMock(return_value=dict(answer='TEST_ONLY new result explanation'))
        self.evidence = dict(binding=dict(schema='v12-known-execution-repair-intent-1', request_id=self.ctx['request_id'],
            original_proposal_id='TEST_ONLY_original', previous_task_id='TEST_ONLY_old_task', previous_version_id='v000002',
            spec_digest=self.spec_digest, source_sha256=self.source_hash, protected_hashes=self.protected,
            execution_request_id='TEST_ONLY_single_repair_execution'), spec=self.spec, generated={}, confirmation={'TEST_ONLY_original_approval': True})
        self.flow._execution_repair_evidence = lambda _: deepcopy(self.evidence)

    def tearDown(self):
        self.temp.cleanup()

    def run_repair(self):
        with patch.object(taskflow, 'check_delta', return_value={'status': 'TEST_ONLY'}):
            return asyncio.run(self.flow.repair_execution('TEST_ONLY_approval_not_real'))

    def test_only_exited_known_interface_failure_is_eligible(self):
        task = dict(state='failed', result=dict(worker_exit_code=0, chain_status='BLOCKED_PARAMETER_EVIDENCE', tool_status='PASS'))
        snapshot = dict(outcome=dict(mission_assessment=dict(task_status='PASS', validity_status='PASS')))
        taskflow.TaskFlow._repair_terminal_eligible(task, snapshot)
        for field, value in [('state', 'uncertain'), ('worker_exit_code', None), ('chain_status', 'BLOCKED_TOOL'), ('tool_status', 'TOOL_FAIL'), ('task_status', 'FAIL'), ('validity_status', 'FAIL')]:
            with self.subTest(field=field):
                a, b = deepcopy(task), deepcopy(snapshot)
                if field == 'state':
                    a[field] = value
                elif field in ('task_status', 'validity_status'):
                    b['outcome']['mission_assessment'][field] = value
                else:
                    a['result'][field] = value
                with self.assertRaises(PermissionError):
                    taskflow.TaskFlow._repair_terminal_eligible(a, b)

    def test_same_candidate_guard_rejects_changed_source_or_digest(self):
        taskflow.TaskFlow._same_repair_candidate(self.proposal, self.spec, self.spec_digest)
        candidate = deepcopy(self.proposal)
        candidate['spec']['artifact']['modelica_source'] += '\nparameter Real sneaky=1;'
        with self.assertRaises(ValueError):
            taskflow.TaskFlow._same_repair_candidate(candidate, self.spec, self.spec_digest)
        with self.assertRaises(ValueError):
            taskflow.TaskFlow._same_repair_candidate(self.proposal, self.spec, 'different_digest')

    def test_same_candidate_new_task_old_receipts_immutable_and_repeat_is_readonly(self):
        result = self.run_repair()
        self.assertEqual(result['execution']['task']['version_id'], 'v000003')
        self.assertEqual(len(self.confirmed), 1)
        self.assertEqual([name for name, _ in self.calls], ['proposal_prepare', 'proposal_get', 'execution_start'])
        self.assertEqual(self.calls[0][1]['spec'], self.spec)
        self.assertEqual(self.calls[-1][1]['request_id'], self.evidence['binding']['execution_request_id'])
        self.assertEqual(self.flow.ask.await_count, 1)
        for filename, expected in self.protected.items():
            self.assertEqual(sha_file(Path(filename)), expected)
        repeated = self.run_repair()
        self.assertTrue(repeated['reused'])
        self.assertEqual(repeated['execution_added'], 0)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.flow.ask.await_count, 1)
        with patch.object(taskflow, 'baseline_sysml_for_context', return_value='TEST_ONLY'):
            ordinary = asyncio.run(self.flow.run())
        self.assertEqual(ordinary['pipeline']['execution']['task']['task_id'], self.snapshot['task_id'])
        self.assertEqual(self.flow.terminal_readback()['pipeline']['execution']['task']['task_id'], self.snapshot['task_id'])
        self.assertEqual(len(self.calls), 3)

    def test_changed_mcp_candidate_stops_before_confirmation_or_execution(self):
        self.proposal['spec'] = deepcopy(self.spec)
        self.proposal['spec']['artifact']['modelica_source'] += '\n// CHANGED_BYTES'
        with self.assertRaises(ValueError):
            self.run_repair()
        self.assertEqual(self.confirmed, [])
        self.assertEqual([name for name, _ in self.calls], ['proposal_prepare', 'proposal_get'])
        self.assertTrue((self.root / 'repair-execution-1-stopped.json').exists())
        with self.assertRaises(PermissionError):
            self.run_repair()
        self.assertEqual(len(self.calls), 2)

    def test_claimed_interrupted_repair_never_sends_again(self):
        (self.root / 'repair-execution-1.intent.json').write_text('{}', encoding='utf8')
        with self.assertRaises(PermissionError):
            self.run_repair()
        self.assertEqual(self.calls, [])

    def test_default_run_does_not_retry_original_terminal_pipeline(self):
        self.flow._execution_repair_evidence = lambda _: self.fail('ordinary run must not authorize repair')
        with patch.object(taskflow, 'baseline_sysml_for_context', return_value='TEST_ONLY'):
            result = asyncio.run(self.flow.run())
        self.assertEqual(result['pipeline']['stage'], 'ORIGINAL_KNOWN_PARAMETER_EVIDENCE_FAILURE')
        self.assertEqual(self.calls, [])

    def test_cli_repair_needs_explicit_approval_record(self):
        output = io.StringIO()
        with patch('sys.stdout', output):
            code = case_cli.main(['repair-execution', '--request-id', self.ctx['request_id']], flow_factory=lambda _: self.flow)
        self.assertEqual(code, 1)
        self.assertIn('--approval-evidence', json.loads(output.getvalue())['error'])
        self.assertEqual(self.calls, [])

    def test_ask_context_contains_verified_sidecar_and_actual_dual_source_status(self):
        flow = taskflow.TaskFlow(self.ctx)
        self.ctx.update(raw_request='TEST_ONLY 180/260', baseline_snapshot=dict(frozen_spec=dict(artifact={'modelica_source': self.source})))
        flow.refresh = lambda: self.ctx
        flow.state = lambda: dict(latest_request_execution=dict(version_id='v000003', task_id=self.snapshot['task_id']))
        flow.open = lambda **_: self.service
        self.store.task = lambda _: dict(result={})
        self.snapshot.update(evidence_status='COMPLETE', source_files={'SysML': 'TEST_ONLY_current.sysml'}, outcome=dict(sysml_status='PASS', consistency_status='PASS'))
        save_json(self.root / 'baseline-sysml-completion.json', {'TEST_ONLY_verified_by_patched_existing_validator': True}, immutable=True)
        captured = []
        async def call(stage, messages, tools):
            captured.extend(messages)
            return dict(content='TEST_ONLY correct explanation', assistant_message=dict(role='assistant', content='TEST_ONLY'))
        flow.call = call
        with patch.object(taskflow, 'baseline_sysml_for_context', return_value='TEST_ONLY_VALIDATED_SIDECAR'):
            asyncio.run(flow.ask('解释本次双源码', explain=True))
        context = json.loads(captured[1]['content'])
        self.assertEqual(context['baseline_sysml_sidecar']['status'], 'VALIDATED_SOURCE_BOUND_SIDECAR')
        self.assertEqual(context['actual_request_execution']['outcome']['sysml_status'], 'PASS')
        self.assertEqual(context['actual_request_execution']['outcome']['consistency_status'], 'PASS')
        self.assertEqual(context['actual_request_execution']['source_files']['SysML'], 'TEST_ONLY_current.sysml')
        self.assertIn('min(T/R,mu_s*Fz)', captured[0]['content'])


if __name__ == '__main__':
    unittest.main()
