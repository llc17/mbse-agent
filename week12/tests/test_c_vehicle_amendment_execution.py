"""Four isolated runner checks; no OMC, simulator, parser or online calls."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from week12.src.integration import vehicle_amendment_execution as runner
from week12.src.integration import vehicle_amendment as amendment
from week12.src.integration.review import check_review_receipt


class VehicleAmendmentExecutionChecks(unittest.TestCase):
    def test_review_identity_stale_approval_and_finite_budget(self):
        packet, _, contract = amendment.inputs()
        self.assertEqual(runner.validate_budget(contract)['maximum_scene_attempts'], 42)
        stale = {'origin':'user_message', 'human_confirmed':True,
                 'message_reference':'negative boundary fixture only', 'user_text':'test only',
                 'scope':packet['scope'], 'specification_digest':'old specification'}
        with self.assertRaises(ValueError): check_review_receipt(stale, packet)
        # A real existing path is used; no synthetic receipt file is written.
        with patch.object(runner, 'read', return_value=stale):
            with self.assertRaises(ValueError):
                runner.initialize((amendment.REVIEW/'execution-review-packet.json').resolve())
        changed = deepcopy(contract)
        changed['budget_proposal']['maximum_scene_attempts'] = 43
        with self.assertRaises(ValueError): runner.validate_budget(changed)
        batch = object.__new__(runner.Batch)
        batch.budget = runner.validate_budget(contract)
        for usage in ({'scene':42,'local':0,'sysml':0,'probe':0,'process_seconds':0,'unresolved_processes':0},
                      {'scene':0,'local':24,'sysml':0,'probe':0,'process_seconds':0,'unresolved_processes':0},
                      {'scene':0,'local':0,'sysml':3,'probe':0,'process_seconds':0,'unresolved_processes':0},
                      {'scene':0,'local':0,'sysml':0,'probe':0,'process_seconds':3600,'unresolved_processes':0},
                      {'scene':0,'local':0,'sysml':0,'probe':0,'process_seconds':0,'unresolved_processes':1}):
            with patch.object(batch, 'usage', return_value=usage):
                with self.assertRaises(ValueError): runner.require_complete_round(batch)
        with patch.object(batch, 'usage', return_value={
                'scene':0,'local':0,'sysml':0,'probe':0,'process_seconds':0,'unresolved_processes':0}):
            with self.assertRaises(RuntimeError): batch.reserve('probe', 'forbidden', 1)

    def test_build_order_isolated_contact_alias_and_protected_original(self):
        _, bundle, _ = amendment.inputs()
        before = runner.sha(amendment.B_SOURCE)
        source = amendment.isolated_b()
        binding = bundle['physical']['contact_implementation']
        import hashlib
        self.assertEqual(hashlib.sha256(source.encode('utf-8')).hexdigest(),
                         binding['runtime_B_copy_sha256'])
        self.assertIn('model PolylineWheelContact = V11ContactC2.PolylineWheelContact;', source)
        self.assertEqual(runner.sha(amendment.CANDIDATE), amendment.C2_SHA)
        self.assertEqual(runner.sha(amendment.B_SOURCE), before)
        self.assertNotIn('model PolylineWheelContact = V11ContactC2.PolylineWheelContact;',
                         amendment.B_SOURCE.read_text(encoding='utf-8'))
        script = runner.build_script('V11CGenerated.Scene', 2, '^(time|x)$', False)
        self.assertLess(script.index('loadFile("V11ContactC2.mo")'),
                        script.index('loadFile("V11PhysicalB1.mo")'))
        self.assertLess(script.index('loadFile("V11PhysicalB1.mo")'),
                        script.index('loadFile("V11CGenerated.mo")'))
        self.assertIn('if not c2 then exit(1); end if;', script)
        self.assertNotIn('loadModel(', script)

    def test_exact_preflight_set_no_overwrite_closed_batch_and_diagnosis_guard(self):
        packet, bundle, contract = amendment.inputs()
        targets = runner.local_targets()
        self.assertEqual(len(targets), 8)
        self.assertEqual(len(set(targets)), 8)
        for module in ('test_c_vehicle_amendment.', 'test_c_vehicle_amendment_execution.'):
            self.assertEqual(sum(module in target for target in targets), 4)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root/'round-01'
            directory.mkdir()
            sentinel = directory/'keep.txt'
            sentinel.write_text('existing evidence', encoding='utf-8')
            with patch.object(runner, 'OUT', root), \
                    patch.object(runner, 'approved_inputs', return_value=(packet,bundle,contract)), \
                    patch.object(runner, 'round_manifest', return_value=packet['implementation_manifest']), \
                    patch.object(runner, 'Batch'), patch.object(runner, 'require_complete_round'):
                with self.assertRaises(ValueError): runner.run_round(1)
            self.assertEqual(sentinel.read_text(encoding='utf-8'), 'existing evidence')
            self.assertFalse((root/'.execution.lock').exists())
            # The historical protected manifest may use both slash styles.
            # Preserve its rows while snapshotting identical resolved files once.
            snapshot = root/'aliases'; snapshot.mkdir()
            rel = 'src/composition/schema.py'
            alias_packet = {'fixed_inputs':[
                {'path':rel,'sha256':runner.sha(amendment.ROOT/rel)},
                {'path':rel.replace('/',chr(92)),'sha256':runner.sha(amendment.ROOT/rel)}]}
            runner.snapshot_inputs(snapshot,alias_packet,{}, {}, [])
            self.assertEqual((snapshot/'input-snapshot'/rel).read_bytes(),
                             (amendment.ROOT/rel).read_bytes())
            runner.save(root/'final-result.json', {'status':'closed fixture'})
            with patch.object(runner, 'OUT', root), patch.object(amendment, 'inputs') as inputs:
                with self.assertRaises(ValueError): runner.approved_inputs()
                inputs.assert_not_called()
        previous = {'status':'STOPPED','proposal_digest':packet['bundle_digest'],
                    'specification_digest':packet['specification_digest']}
        hard = {'classification':'physical', 'reason':'physical failure',
                'proposal_digest':packet['bundle_digest'],
                'specification_digest':packet['specification_digest']}
        with patch.object(runner, 'read', side_effect=[previous,hard]):
            with self.assertRaises(ValueError): runner.round_manifest(2, packet)
        repair = {**hard, 'classification':'implementation',
                  'rebound_implementation_manifest':packet['implementation_manifest']}
        with patch.object(runner, 'read', side_effect=[previous,repair,packet['implementation_manifest']]):
            with self.assertRaises(ValueError): runner.round_manifest(2, packet)

    def test_first_scene_failure_stops_and_keeps_usage_not_run_and_owned_lock_cleanup(self):
        class FakeBatch:
            def __init__(self, *_):
                self.budget = dict(runner.EXPECTED_BUDGET)
                self.counts = {'scene':0,'local':0,'sysml':0,'probe':0,
                               'process_seconds':0,'unresolved_processes':0}
            def usage(self): return dict(self.counts)
            def reserve(self, kind, *_):
                self.counts[kind] += 1
                return {'kind':kind}
            def run(self, command, cwd, timeout, log_dir, label):
                self.counts['process_seconds'] += 1
                if label == 'sysml':
                    runner.save(Path(command[-1]), {'status':'PASS','tool_version':'0.34.1'})
                return {'returncode':0,'timed_out':False}
        source = Path(runner.__file__).resolve()
        manifest = [{'path':str(source), 'sha256':runner.sha(source)}]
        packet = {'scope':'mock isolated orchestration', 'specification_digest':'mock spec',
                  'bundle_digest':'mock proposal', 'fixed_inputs':[], 'implementation_manifest':manifest}
        seen = []
        def fail_scene(batch, directory, bundle, contract, case, refined, number, sources, identity, base):
            seen.append((case,refined))
            batch.reserve('scene', case, number)
            return ({'case':case,'variant':'base','status':'FAIL','compile':'PASS',
                     'simulation':'COMPLETED','physical':'FAIL','numeric':'UNVERIFIED'}, directory/'mock')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(runner, 'OUT', root), \
                    patch.object(runner, 'approved_inputs', return_value=(packet,{},{})), \
                    patch.object(runner, 'round_manifest', return_value=manifest), \
                    patch.object(runner, 'Batch', FakeBatch), \
                    patch.object(runner, 'guard_sources'), patch.object(runner, 'snapshot_inputs'), \
                    patch.object(runner, 'dependency_audit'), \
                    patch.object(runner, 'local_targets', return_value=['local_'+str(i) for i in range(8)]), \
                    patch.object(amendment, 'generate', return_value={'sysml':'mock source','trace':{}}), \
                    patch.object(runner, 'execute_scene', side_effect=fail_scene):
                report = runner.run_round(1)
            self.assertEqual(seen, [(runner.CASE_ORDER[0],False)])
            self.assertEqual(report['status'], 'STOPPED')
            self.assertEqual(len(report['not_run']), 13)
            self.assertEqual(report['usage']['scene'], 1)
            self.assertEqual(report['usage']['local'], 8)
            self.assertEqual(report['usage']['sysml'], 1)
            self.assertEqual(report['scenes'][0]['physical'], 'FAIL')
            self.assertEqual(runner.read(root/'round-01/batch-result.json'), report)
            self.assertFalse((root/'.execution.lock').exists())


if __name__ == '__main__': unittest.main()
