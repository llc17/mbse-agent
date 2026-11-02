"""TEST_ONLY Store/manifest/CSV fixtures; no RPC, HTTP, compiler or dynamics."""
from copy import deepcopy
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from week11.src.store import canonical_json
from week13.src import case_backend, case_requests
from week13.src.case_paths import ROOT, sha_file
from week13.src.inherited import artifact_baseline as baseline
from week13.src.inherited import artifact_delivery as delivery
from week13.src.inherited import artifact_history as history
from week13.src.inherited import artifact_preparation as preparation
from week13.src.inherited import service_adapter
from week13.src.inherited.artifact_service import ArtifactService
from week13.tests.test_case_agent_requests import NoLaunch, finish_fixture, synthetic_spec, write

EVIDENCE = ROOT / 'results/mcp-baseline-reuse-20261031-01'


class BaselineReuseTests(unittest.TestCase):
    def setUp(self):
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='TEST_ONLY-baseline-', dir=EVIDENCE)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.context = self.root / 'context.json'
        write(self.context, dict(schema='v11-artifact-execution-context-1', context_id='fixture_baseline', status='ACTIVE', mode='TEST_ONLY', allow_execution=True, max_tasks=6, worker_timeout_s=1900, simulation_timeout_s=900))
        self.svc = ArtifactService(self.root / 'projects.sqlite', 'fixture_seed', context=self.context, worker_manager=NoLaunch())
        self.svc.project_root = self.root
        self.project = self.svc.project_create('TEST_ONLY baseline reuse')['project']
        source = self.svc.record_source('TEST_ONLY ideal point mass request')
        self.source = dict(id=source['source_id'], text=source['text'])
        with redirect_stderr(io.StringIO()):
            result = self.svc.proposal_prepare(synthetic_spec(self.source, 85))
            proposal = self.svc.store.proposal(result['proposal']['proposal_id'])
            self.svc.confirm_local(proposal['proposal_id'], proposal['displayed_digest'])
            task = self.svc.execution_start(proposal['proposal_id'], 'fixture_seed_task')['task']
        self.task = finish_fixture(self.svc, task, self.root)
        self.base = self.root / 'projects' / self.project['project_id'] / 'versions' / self.task['version_id']
        self.add_delivery()
        with redirect_stderr(io.StringIO()):
            self.svc.versions_select(self.task['version_id'])
            self.record = baseline.selected_record(self.svc)
        self.ref = baseline.reference(self.record)
        self.candidate = synthetic_spec(self.source, 90)
        self.candidate['baseline_ref'] = self.ref
        params = dict(m=90, I_pitch=20, T_main_max=60, T_nose_max=25)
        self.ctx = dict(request_id='fixture_prepare', execution_context_path=str(self.context), project_root=str(self.root), project_id=self.project['project_id'], baseline_ref=self.ref, baseline_snapshot=self.record, sources=[self.source], displayed_digest='TEST_ONLY', plan=dict(intent='CHANGE', support='SUPPORTED', missing=[], unsupported=[], requirements=self.candidate['requirements'], design=self.candidate['design'], proposed_parameters=params, defaults=[], unknowns=[], rationale='TEST_ONLY', scope=dict(change_kind='center_of_mass_point_load', mass_location='original_center_of_mass', inertia_assumption='ideal_point_mass', frozen='geometry_contact_control_terrain_task')))
        self.case = case_backend.service(self.svc.store.db_path, self.svc.session_id, task_context=self.ctx, worker_manager=NoLaunch())

    def add_delivery(self):
        """Construct a bound delivery in the synthetic receipt, before tests."""
        task = self.svc.store.task(self.task['task_id'])
        proposal = self.svc.store.proposal(task['proposal_id'])
        outcome = json.loads((self.base / 'outcome.json').read_bytes())
        samples = delivery.read_signals(proposal['spec'], self.base / delivery.CSV_NAME)
        summary = dict(contract=delivery.SCHEMA, report='delivery.json', status='INCOMPLETE', data_status='PASS', plot_status='NOT_RUN', scope='TASK_RESULT')
        identity = dict(task_id=task['task_id'], project_id=task['project_id'], version_id=task['version_id'], spec_digest=proposal['spec_digest'], source_sha256=proposal['display']['source_sha256'])
        write(self.base / 'delivery.json', dict(schema=delivery.SCHEMA, binding=identity, scope='TASK_RESULT', test_only=True, status='INCOMPLETE', data_status='PASS', plot_status='NOT_RUN', plots=[], reasons=[], csv=dict(path=delivery.CSV_NAME, **delivery.file_record(self.base / delivery.CSV_NAME)), output_mapping=samples['mapping'], samples=samples['summary']))
        outcome['result_delivery'] = summary
        write(self.base / 'outcome.json', outcome)
        manifest_path = self.base / 'artifact-manifest.json'
        manifest = json.loads(manifest_path.read_bytes())
        manifest.update(outcome=outcome, files={p.relative_to(self.base).as_posix(): delivery.file_record(p) for p in self.base.rglob('*') if p.is_file() and p != manifest_path})
        write(manifest_path, manifest)
        result = dict(outcome, outcome_manifest_path=str(manifest_path), outcome_manifest_sha256=sha_file(manifest_path), worker_exit_code=0, worker_pid=99999)
        # Explicit TEST_ONLY fixture construction, never a production recovery.
        with self.svc.store.transaction() as db:
            db.execute('UPDATE tasks SET result_json=? WHERE task_id=?', (canonical_json(result), task['task_id']))
        self.task = self.svc.store.task(task['task_id'])

    def prepare(self, candidate=None):
        with patch.object(case_requests, 'require_execution', return_value=self.ctx), redirect_stderr(io.StringIO()):
            return self.case.proposal_prepare(self.candidate if candidate is None else candidate)

    def test_prepare_has_one_snapshot_one_csv_parse_and_final_hash(self):
        with patch.object(history, 'snapshot', wraps=history.snapshot) as snapshots, patch.object(delivery, 'read_signals', wraps=delivery.read_signals) as reads, patch.object(history, '_verify_manifest', wraps=history._verify_manifest) as initial_hashes, patch.object(service_adapter, '_verify_manifest', wraps=service_adapter._verify_manifest) as final_hashes:
            result = self.prepare()
        self.assertTrue(result['ok'])
        stored = self.case.store.proposal(result['proposal']['proposal_id'])
        self.assertEqual(stored['spec'], self.candidate)
        self.assertEqual(stored['display']['artifact_baseline'], self.ref)
        self.assertEqual((snapshots.call_count, reads.call_count, initial_hashes.call_count, final_hashes.call_count), (1, 1, 1, 1))
        self.assertIsNone(preparation._active.get())

    def test_each_prepare_and_outside_resolve_verify_again(self):
        with patch.object(history, 'snapshot', wraps=history.snapshot) as snapshots, patch.object(delivery, 'read_signals', wraps=delivery.read_signals) as reads:
            self.prepare(); self.prepare()
            baseline.resolve(self.case, self.ref); baseline.resolve(self.case, self.ref)
        self.assertEqual((snapshots.call_count, reads.call_count), (4, 4))
        self.assertIsNone(preparation._active.get())

    def test_nested_scopes_restore_outer_and_other_service_does_not_share(self):
        with patch.object(history, 'snapshot', wraps=history.snapshot) as snapshots, redirect_stderr(io.StringIO()):
            with preparation.proposal_preparation(self.case):
                baseline.resolve(self.case, self.ref)
                outer = preparation._active.get()
                with preparation.proposal_preparation(self.case):
                    baseline.resolve(self.case, self.ref)
                    self.assertIsNot(preparation._active.get(), outer)
                self.assertIs(preparation._active.get(), outer)
                baseline.resolve(self.case, self.ref)
                baseline.resolve(self.svc, self.ref)
            self.assertEqual(snapshots.call_count, 3)
        self.assertIsNone(preparation._active.get())

    def test_input_cannot_supply_prevalidated_record_or_csv_samples(self):
        for key in ('verified_baseline', 'baseline_snapshot', 'samples'):
            candidate = deepcopy(self.candidate); candidate[key] = {'status': 'PASS'}
            self.assertFalse(self.prepare(candidate)['ok'])
        with self.assertRaises(TypeError):
            delivery.verify_delivery({}, {}, {}, self.base, {}, samples={'summary': {'status': 'PASS'}})
        self.assertIsNone(preparation._active.get())

    def test_selection_change_during_prepare_refuses_and_clears_scope(self):
        normalize = self.case.normalize_candidate
        def change(spec):
            result = normalize(spec)
            self.case.store.select_artifact_baseline(self.case.session_id, self.project['project_id'], None)
            return result
        with patch.object(self.case, 'normalize_candidate', side_effect=change), self.assertRaisesRegex(ValueError, 'selection changed|selection changed|baseline selection'):
            self.prepare()
        self.assertIsNone(preparation._active.get())

    def test_manifest_source_csv_drift_before_prepare_refuses(self):
        for name in ('artifact-manifest.json', 'frozen-model.mo', delivery.CSV_NAME):
            with self.subTest(name=name):
                path = self.base / name; original = path.read_bytes()
                path.write_bytes(original + b' ')
                try:
                    with self.assertRaises(ValueError):self.prepare()
                finally:path.write_bytes(original)
        self.assertIsNone(preparation._active.get())

    def test_manifest_and_source_drift_during_prepare_refuses(self):
        normalize = self.case.normalize_candidate
        for name in ('artifact-manifest.json', 'frozen-model.mo'):
            with self.subTest(name=name):
                path = self.base / name; original = path.read_bytes(); calls = []
                def change(spec):
                    result = normalize(spec); calls.append(1)
                    if len(calls) == 1:path.write_bytes(original + b' ')
                    return result
                try:
                    with patch.object(self.case, 'normalize_candidate', side_effect=change), self.assertRaises(ValueError):self.prepare()
                finally:path.write_bytes(original)
        self.assertIsNone(preparation._active.get())

    def test_candidate_cannot_change_task_version_or_reference_digests(self):
        for key, value in (('version_id', 'v000002'), ('task_id', 'task_invented'), ('source_sha256', '0' * 64), ('manifest_sha256', '0' * 64), ('spec_digest', '0' * 64)):
            with self.subTest(key=key):
                candidate = deepcopy(self.candidate); candidate['baseline_ref'][key] = value
                with self.assertRaises(ValueError):self.prepare(candidate)
        self.assertIsNone(preparation._active.get())

    def test_same_size_csv_change_restored_mtime_rejected_before_insert(self):
        path = self.base / delivery.CSV_NAME
        original = path.read_bytes(); stat = path.stat()
        normalize = self.case.normalize_candidate; calls = []
        def change(spec):
            result = normalize(spec); calls.append(1)
            if len(calls) == 2:
                path.write_bytes(original.replace(b'120,1', b'120,2'))
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            return result
        with patch.object(self.case, 'normalize_candidate', side_effect=change), self.assertRaisesRegex(ValueError, 'evidence changed|hash mismatch'):
            self.prepare()
        self.assertEqual(path.stat().st_size, len(original))
        self.assertEqual(path.stat().st_mtime_ns, stat.st_mtime_ns)
        self.assertIsNone(preparation._active.get())

    def test_final_hash_rejects_content_change_even_when_stat_guard_is_blind(self):
        path = self.base / delivery.CSV_NAME
        original = path.read_bytes(); normalize = self.case.normalize_candidate; calls = []
        def change(spec):
            result = normalize(spec); calls.append(1)
            if len(calls) == 2:path.write_bytes(original.replace(b'120,1', b'120,2'))
            return result
        # Deliberately neutralize only the cheap guard: final cryptographic
        # verification must still prevent accepting a mutable CSV.
        with patch.object(preparation, '_state', return_value='TEST_ONLY_same_stat'), patch.object(self.case, 'normalize_candidate', side_effect=change), self.assertRaisesRegex(ValueError, 'hash mismatch'):
            self.prepare()
        self.assertIsNone(preparation._active.get())

    def test_trusted_source_row_drift_during_prepare_refuses(self):
        normalize = self.case.normalize_candidate
        def change(spec):
            result = normalize(spec)
            with self.case.store.transaction() as db:
                db.execute('UPDATE sources SET text=? WHERE source_id=?', ('TEST_ONLY tampered text', self.source['id']))
            return result
        with patch.object(self.case, 'normalize_candidate', side_effect=change), self.assertRaises(ValueError):self.prepare()
        self.assertIsNone(preparation._active.get())

    def test_delivery_samples_mapping_and_invalid_numeric_rows_still_refuse(self):
        for category in ('summary', 'mapping', 'nonfinite', 'decreasing'):
            with self.subTest(category=category):
                csv = self.base / delivery.CSV_NAME
                original_csv = csv.read_bytes()
                report_path = self.base / 'delivery.json'; original_report = report_path.read_bytes()
                if category in ('summary', 'mapping'):
                    report = json.loads(original_report)
                    if category == 'summary':report['samples']['rows'] += 1
                    else:report['output_mapping']['signal']['resolved'] = 'invented'
                    write(report_path, report)
                else:csv.write_text('time,signal\n0,0\n'+('120,nan\n' if category == 'nonfinite' else '-1,1\n'), encoding='utf8')
                manifest = json.loads((self.base / 'artifact-manifest.json').read_bytes())
                manifest['files'][delivery.CSV_NAME] = delivery.file_record(csv)
                outcome = self.task['result']
                identity = dict(task_id=self.task['task_id'], project_id=self.project['project_id'], version_id=self.task['version_id'], spec_digest=self.record['spec_digest'], source_sha256=self.record['source_sha256'])
                try:
                    with self.assertRaises(ValueError):delivery.verify_delivery(self.record['frozen_spec'], outcome, manifest, self.base, identity, require_signals=True)
                finally:csv.write_bytes(original_csv);report_path.write_bytes(original_report)

    def test_repeated_event_times_and_prefixed_mapping_keep_original_summary(self):
        path = self.root / 'TEST_ONLY-events.csv'
        path.write_text('time,signal\n0,0\n1,1\n1,2\n2,3\n', encoding='utf8')
        spec = deepcopy(self.record['frozen_spec']); spec['outputs']['signal'] = 'Fixture.signal'
        samples = delivery.read_signals(spec, path)
        self.assertEqual(samples['times'], [0, 1, 1, 2])
        self.assertEqual(samples['series']['signal'], [0, 1, 2, 3])
        self.assertEqual(samples['summary'], dict(rows=4, time_start=0, time_end=2, duplicate_times=1, final_values={'signal': 3}))
        self.assertEqual(samples['mapping']['signal'], dict(requested='Fixture.signal', resolved='signal'))


if __name__ == '__main__':unittest.main()
