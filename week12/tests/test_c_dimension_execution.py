"""Four isolated boundary checks; no compiler, simulation, parser or online use."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from week12.src.integration import dimension_execution as runner
from week12.src.integration.review import check_review_receipt


def packet_fixture():
    return {'scope':runner.SCOPE, 'specification_digest':'a'*64, 'physical_digest':'b'*64,
            'bundle_digest':'c'*64, 'case_order':list(runner.CASE_ORDER),
            'variants':['base', 'refined'], 'budget':deepcopy(runner.BUDGET),
            'implementation_manifest':[], 'fixed_inputs':[], 'readonly_manifest':[]}


class DimensionExecutionChecks(unittest.TestCase):
    def test_external_exact_receipt_closed_batch_and_budget_reservation(self):
        packet = packet_fixture()
        stale = {'origin':'user_message', 'human_confirmed':True,
            'message_reference':'negative boundary fixture; never approval', 'user_text':'test only',
            'scope':runner.SCOPE, 'specification_digest':'old'}
        with self.assertRaises(ValueError): check_review_receipt(stale, packet)
        with patch.object(runner, 'inputs', return_value=(packet, {}, {})), \
                patch.object(runner, 'read', return_value=stale), patch.object(runner, 'guard_sources') as guard:
            with self.assertRaises(ValueError): runner.initialize(Path(runner.__file__).resolve())
            guard.assert_not_called()
        with self.assertRaises(ValueError): runner.initialize('relative-receipt.json')
        self.assertEqual(runner.validate_budget(packet)['maximum_scene_attempts'], 12)
        bad = deepcopy(packet); bad['budget']['maximum_rounds'] = 4
        with self.assertRaises(ValueError): runner.validate_budget(bad)
        batch = object.__new__(runner.Batch); batch.budget = deepcopy(runner.BUDGET)
        fresh = {'scene':0, 'local':0, 'sysml':0, 'probe':0,
                 'process_seconds':0, 'unresolved_processes':0}
        with patch.object(batch, 'usage', return_value=fresh): runner.require_complete_round(batch)
        for key,value in (('scene',9), ('local',17), ('sysml',3), ('probe',1),
                          ('process_seconds',4001), ('unresolved_processes',1)):
            with patch.object(batch, 'usage', return_value={**fresh,key:value}):
                with self.assertRaises(ValueError): runner.require_complete_round(batch)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); runner.save(root/'final-result.json', {'status':'closed fixture'})
            with patch.object(runner, 'OUT', root), patch.object(runner, 'inputs') as inputs:
                with self.assertRaises(ValueError): runner.approved_inputs()
                inputs.assert_not_called()

    def test_source_guard_exact_old21_new5_and_readonly_helper(self):
        fixed, fixed_inputs = runner._prior()
        manifest = [*fixed, *[{'path':str((runner.ROOT/p).resolve()), 'sha256':runner.sha(runner.ROOT/p)}
                             for p in runner.NEW_PATHS]]
        packet = packet_fixture()
        packet.update(fixed_implementation_manifest=fixed, fixed_inputs=fixed_inputs,
            implementation_manifest=manifest, readonly_manifest=[
                {'path':str((runner.ROOT/p).resolve()), 'sha256':runner.sha(runner.ROOT/p)}
                for p in runner.READONLY_PATHS])
        runner.guard_sources(packet, manifest)
        self.assertEqual(len(manifest), 26)
        changed = deepcopy(manifest); changed[0]['sha256'] = '0'*64
        with self.assertRaises(ValueError): runner.guard_sources(packet, changed)
        with self.assertRaises(ValueError): runner.guard_sources(packet, manifest[:-1])
        bad = deepcopy(packet); bad['readonly_manifest'][0]['sha256'] = '0'*64
        with self.assertRaises(ValueError): runner.guard_sources(bad, manifest)
        bad = deepcopy(packet); bad['fixed_inputs'][0]['sha256'] = '0'*64
        with self.assertRaises(ValueError): runner.guard_sources(bad, manifest)
        old = runner.previous.execute_scene
        self.assertIs(old.__globals__['amendment'], runner.vehicle)
        with patch.object(runner.previous, 'execute_scene', lambda *a, **k: ('isolated fixture', None)):
            self.assertEqual(runner.execute_scene(None, None, {}, {}, runner.CASE_ORDER[0],
                False, 1, [], packet), ('isolated fixture', None))
        self.assertIs(old.__globals__['amendment'], runner.vehicle)

    def test_concrete_diagnosis_changed_new_source_only_and_no_blind_retry(self):
        packet = packet_fixture()
        result = {'status':'STOPPED', 'retry_eligible':True, 'failure_code':'NEW_GENERATOR_INTERFACE',
            'specification_digest':packet['specification_digest'], 'physical_digest':packet['physical_digest'],
            'proposal_digest':packet['bundle_digest']}
        before = [{'path':str((runner.ROOT/p).resolve()),'sha256':'a'*64} for p in runner.NEW_PATHS]
        after = deepcopy(before); after[0]['sha256'] = 'b'*64
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'error.log').write_text('specific local adapter error fixture', encoding='utf-8')
            diagnosis = {**result, 'classification':'implementation', 'reason':'exact wrapper argument mismatch',
                'same_physical_task_verification':True,
                'evidence_files':[{'path':'error.log','sha256':runner.sha(root/'error.log')}],
                'rebound_implementation_manifest':after, 'changed_paths':[before[0]['path']]}
            with patch.object(runner, 'OUT', root), patch.object(runner, 'guard_sources'):
                self.assertEqual(runner.validate_repair(packet, before, result, diagnosis), after)
                blind = {**diagnosis,'rebound_implementation_manifest':before,'changed_paths':[]}
                with self.assertRaises(ValueError): runner.validate_repair(packet, before, result, blind)
                for bad in ({**diagnosis,'physical_digest':'changed'},
                            {**diagnosis,'evidence_files':[]}, {**diagnosis,'classification':'physical'}):
                    with self.assertRaises(ValueError): runner.validate_repair(packet, before, result, bad)
                with self.assertRaises(ValueError): runner.validate_repair(packet, before,
                    {**result,'retry_eligible':False}, diagnosis)
                with self.assertRaises(ValueError): runner.validate_repair(packet, before,
                    {**result,'failure_code':'TERMINAL'}, diagnosis)

    def test_first_known_task_fail_closes_without_refined_or_automatic_retry(self):
        for row in ({'status':'FAIL','physical':'FAIL','numeric':'UNVERIFIED'},
                    {'status':'FAIL','physical':'PASS','numeric':'FAIL'},
                    {'status':'FAIL','compile':'PASS','simulation':'FAILED_OR_TIMED_OUT'}):
            with self.assertRaises(runner.TerminalFailure): runner.classify_scene(row, None)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); folder = root/'scene'; folder.mkdir()
            (folder/'build.stdout.log').write_text('V11CGenerated.mo syntax error', encoding='utf-8')
            runner.save(root/'process-001.start.json', {'label':'build','cwd':str(folder)})
            completed = {'label':'build','returncode':1,'timed_out':False,'interrupted':False,'uncertain':False}
            syntax = {'status':'FAIL','compile':'NOT_RUN','error':'RuntimeError: BUILD_TOOL_FAILURE'}
            with patch.object(runner, 'OUT', root), patch.object(runner, 'read', side_effect=[
                    {'label':'build','cwd':str(folder)}, completed]):
                # Matching completed result must exist; missing result is terminal.
                with self.assertRaises(runner.TerminalFailure): runner.classify_scene(syntax, folder)
            runner.save(root/'process-001.result.json', completed)
            with patch.object(runner, 'OUT', root):
                with self.assertRaises(runner.ImplementationFailure): runner.classify_scene(syntax, folder)
            with patch.object(runner, 'OUT', root), patch.object(runner, 'read', side_effect=[
                    {'label':'build','cwd':str(folder)}, {**completed,'timed_out':True}]):
                with self.assertRaises(runner.TerminalFailure): runner.classify_scene(syntax, folder)
        class FakeBatch:
            def __init__(self, *_):
                self.budget = deepcopy(runner.BUDGET)
                self.counts = {'scene':0,'local':0,'sysml':0,'probe':0,
                               'process_seconds':0,'unresolved_processes':0}
            def usage(self): return dict(self.counts)
            def reserve(self, kind, *_): self.counts[kind] += 1; return {'kind':kind}
            def run(self, command, cwd, timeout, log_dir, label):
                self.counts['process_seconds'] += 1
                if label == 'sysml': runner.save(Path(command[-1]), {'status':'PASS','tool_version':'0.34.1'})
                return {'returncode':0,'timed_out':False}
        packet = packet_fixture(); seen = []
        def fail_scene(batch, directory, bundle, contract, case, refined, number, sources, identity, base):
            seen.append((case,refined)); batch.reserve('scene',case,number)
            return ({'case':case,'variant':'base','status':'FAIL','compile':'PASS',
                'simulation':'COMPLETED','physical':'FAIL','numeric':'UNVERIFIED',
                'failed_checks':['complete_geometry_beyond_exit']}, directory/'mock')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(runner, 'OUT', root), \
                    patch.object(runner, 'approved_inputs', return_value=(packet,{},{})), \
                    patch.object(runner, 'round_manifest', return_value=[]), \
                    patch.object(runner, 'Batch', FakeBatch), patch.object(runner, 'guard_sources'), \
                    patch.object(runner.previous, 'snapshot_inputs'), patch.object(runner, 'snapshot_readonly'), \
                    patch.object(runner, 'dependency_audit'), \
                    patch.object(runner, 'local_targets', return_value=['local_'+str(i) for i in range(8)]), \
                    patch.object(runner.revision, 'generate', return_value={'sysml':'mock source','trace':{}}), \
                    patch.object(runner, 'execute_scene', side_effect=fail_scene):
                report = runner.run_round(1)
            self.assertEqual(seen, [(runner.CASE_ORDER[0], False)])
            self.assertEqual(len(report['not_run']), 3)
            self.assertEqual(report['usage']['local'], 8)
            self.assertEqual(report['usage']['sysml'], 1)
            self.assertFalse(report['retry_eligible'])
            final = runner.read(root/'final-result.json')
            self.assertEqual(final['unused_budget_disposition'], 'RETIRED')
            self.assertFalse(final['automatic_additional_scene_permitted'])
            self.assertFalse(final['C_complete'])
            self.assertFalse((root/'.execution.lock').exists())


if __name__ == '__main__': unittest.main()
