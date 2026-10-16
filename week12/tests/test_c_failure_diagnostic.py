"""Four offline checks for the isolated diagnostic adapter, not vehicle tests."""
from copy import deepcopy
import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from week12.src.integration import failure_diagnostic as d


class FailureDiagnosticChecks(unittest.TestCase):
    def test_external_approval_no_autorun_and_exact_finite_one_case(self):
        identity = {'scope':'diagnostic', 'specification_digest':'abc',
                    'case_order':[d.CASE], 'variant':'refined', 'stop_time_s':35,
                    'numerics':deepcopy(d.NUMERICS), 'budget':deepcopy(d.BUDGET)}
        self.assertEqual(d.validate_budget(identity)['maximum_scene_attempts'], 3)
        for key, value in (('case_order',[d.CASE,'other']), ('variant','base'),
                           ('budget',{**d.BUDGET,'maximum_rounds':4})):
            bad = deepcopy(identity); bad[key] = value
            with self.assertRaises(ValueError): d.validate_budget(bad)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); out = root/'execution'
            with patch.object(d, 'OUT', out), patch.object(d, 'inputs', return_value=(identity, {}, {}, {})), \
                    patch.object(d, 'guard_sources') as guards, patch.object(d, 'Batch') as batch:
                rejected = root/'rejected.json'
                d.save(rejected, {'origin':'AI', 'human_confirmed':True})
                with self.assertRaises(ValueError): d.initialize(str(rejected))
                self.assertFalse(out.exists())
                identity['implementation_manifest'] = []
                receipt = root/'external.json'
                d.save(receipt, {'origin':'user_message','human_confirmed':True,
                       'message_reference':'actual external message reference', 'user_text':'确认',
                       'scope':'diagnostic','specification_digest':'abc'})
                d.initialize(str(receipt))
                self.assertTrue((out/'authorization.json').is_file())
                self.assertEqual(d.read(out/'authorization.json'), d.read(receipt))
                batch.assert_not_called()
                with self.assertRaises(FileExistsError): d.initialize(str(receipt))
            fake = Mock(); fake.usage.return_value = {'scene':0,'local':0,'sysml':0,
                'probe':0,'process_seconds':0,'unresolved_processes':0}
            d.require_complete_round(fake)
            for change in ({'scene':3}, {'local':9}, {'process_seconds':1001},
                           {'unresolved_processes':1}, {'sysml':1}):
                fake.usage.return_value = {**{'scene':0,'local':0,'sysml':0,'probe':0,
                    'process_seconds':0,'unresolved_processes':0}, **change}
                with self.assertRaises(ValueError): d.require_complete_round(fake)

    def test_failed_report_comparison_never_accepts_vehicle_and_detects_changes(self):
        rules = {'position_abs_m':.001,'pitch_abs_rad':.0017453292519943296,
                 'velocity_abs_m_s':.001,'wheel_speed_abs_rad_s':.01,'state_relative':.01,
                 'peak_contact_force_relative':.05,'peak_contact_force_abs_N':1.,
                 'event_time_abs_s':.002,'energy_relative':.01,'energy_abs_J':.1}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); paths = [root/'base.csv', root/'refined.csv']
            def write(path, shift):
                with path.open('w', encoding='utf-8', newline='') as stream:
                    writer = csv.writer(stream); writer.writerow(['time','body_chassis_x'])
                    for i in range(35001): writer.writerow([i/1000, 1.+shift])
            write(paths[0], 0.); write(paths[1], 0.)
            def report(path):
                return {'status':'FAIL','checks':[{'name':'task_exit','pass':False}],
                    'diagnostics':[],'issues':[], 'metrics':{'case_id':d.CASE,
                    'csv_sha256':d.sha(path),'signals':{'body_chassis_x':{'peak_abs':1.,'terminal':1.}},
                    'event_times':{'front_riser_1':10.},'cumulative':{'shaft_work':500.}}}
            a, b = (report(p) for p in paths)
            def compare(): return d.compare_failure({'numerical_comparison':rules}, *paths, a, b)
            result = compare()
            self.assertEqual(result['classification'], 'FAILURE_REPRODUCED_NUMERICALLY')
            self.assertEqual(result['vehicle_acceptance'], 'NOT_GRANTED')
            self.assertEqual(a['status'], 'FAIL'); self.assertEqual(b['status'], 'FAIL')
            write(paths[1], .1); b['metrics']['csv_sha256'] = d.sha(paths[1])
            self.assertEqual(compare()['classification'], 'NUMERICAL_SENSITIVITY')
            b['status'] = 'PASS'; b['checks'][0]['pass'] = True
            self.assertEqual(compare()['classification'], 'TASK_OUTCOME_CHANGED')
            b['metrics']['csv_sha256'] = 'bad'
            self.assertEqual(compare()['classification'], 'INCOMPLETE')
            self.assertEqual(a['status'], 'FAIL')

    def test_frozen_source_and_base_reuse_guards(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); fixed = root/'fixed.py'; new = root/'new.py'; base = root/'base.csv'
            fixed.write_text('x=1',encoding='utf-8'); new.write_text('x=2',encoding='utf-8')
            base.write_text('immutable base',encoding='utf-8')
            candidate = root/'candidate.mo'; candidate.write_text('accepted C2',encoding='utf-8')
            rows = [{'path':str(p),'sha256':d.sha(p)} for p in (fixed,new)]
            identity = {'implementation_manifest':rows,'fixed_implementation_manifest':[rows[0]],
                        'fixed_inputs':[], 'reused_evidence':[{'path':'base.csv','sha256':d.sha(base)}]}
            with patch.object(d,'ROOT',root), patch.object(d.execution,'ROOT',root), \
                    patch.object(d.vehicle,'CANDIDATE',candidate), \
                    patch.object(d.vehicle,'C2_SHA',d.sha(candidate)), patch.object(d,'_check_reuse'):
                d.guard_sources(identity, {'fixed_inputs':[]}, rows)
                repaired = deepcopy(rows); new.write_text('x=3',encoding='utf-8')
                repaired[1]['sha256'] = d.sha(new)
                d.guard_sources(identity, {'fixed_inputs':[]}, repaired)
                with self.assertRaises(RuntimeError): d.guard_sources(identity, {'fixed_inputs':[]}, rows)
                illegal = deepcopy(repaired); illegal[0]['sha256'] = 'f'*64
                with self.assertRaises(ValueError): d.guard_sources(identity, {'fixed_inputs':[]}, illegal)
                with self.assertRaises(ValueError): d.guard_sources(identity, {'fixed_inputs':[]}, repaired[:1])
                base.write_text('modified',encoding='utf-8')
                with self.assertRaisesRegex(ValueError,'REUSED_BASE'):
                    d.guard_sources(identity, {'fixed_inputs':[]}, repaired)

    def test_stop_closure_no_overwrite_and_owned_lock_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            identity = {'specification_digest':'diag','implementation_manifest':[]}
            usage = {'scene':0,'local':0,'sysml':0,'probe':0,'process_seconds':0,'unresolved_processes':0}
            batch = Mock(root=out); batch.usage.return_value = usage
            batch.run.return_value = {'returncode':1,'timed_out':False}
            patches = [patch.object(d,'OUT',out),
                patch.object(d,'approved_inputs',return_value=(identity,{}, {}, {})),
                patch.object(d,'round_manifest',return_value=[]), patch.object(d,'Batch',return_value=batch),
                patch.object(d,'guard_sources'), patch.object(d.execution,'snapshot_inputs'),
                patch.object(d,'dependency_audit'), patch.object(d,'local_targets',return_value=['test1','test2','test3','test4']),
                patch.object(d.execution,'execute_scene')]
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8] as scene:
                first = d.run_round(1)
                self.assertEqual(first['status'],'STOPPED')
                self.assertEqual(len(first['local']),1); scene.assert_not_called()
                self.assertFalse((out/'.execution.lock').exists())
                self.assertFalse((out/'final-result.json').exists())
                with self.assertRaises(ValueError): d.run_round(1)
                # A diagnostic completion closes unused budget even if vehicle FAIL.
                batch.run.return_value = {'returncode':0,'timed_out':False}
                def compare_run(command, cwd, timeout, log_dir, label):
                    if label == 'failure-comparison':
                        d.save(log_dir/'failure-diagnostic.json', {'classification':'FAILURE_REPRODUCED_NUMERICALLY'})
                    return {'returncode':0,'timed_out':False}
                batch.run.side_effect = compare_run
                def failed_scene(*args, **kwargs):
                    folder = out/'round-02'/(d.CASE+'-refined'); folder.mkdir()
                    return {'case':d.CASE,'variant':'refined','status':'FAIL','physical':'FAIL'}, folder
                scene.side_effect = failed_scene
                with patch.object(d,'_completed_verification',return_value=1.):
                    result = d.run_round(2)
                self.assertEqual(result['scenes'][0]['status'],'FAIL')
                self.assertEqual(result['status'],'DIAGNOSTIC_COMPLETED_VEHICLE_NOT_ACCEPTED')
                self.assertEqual(d.read(out/'final-result.json')['unused_budget_disposition'],'RETIRED')
            # Actual approval loader checks closure before reading anything else.
            with patch.object(d,'OUT',out), patch.object(d,'inputs') as source:
                with self.assertRaisesRegex(ValueError,'CLOSED_DIAGNOSTIC'): d.approved_inputs()
                source.assert_not_called()
            lock = out/'.execution.lock'; lock.write_text('somebody-else',encoding='ascii')
            with patch.object(d,'OUT',out), patch.object(d,'approved_inputs',return_value=(identity,{}, {}, {})), \
                    patch.object(d,'round_manifest',return_value=[]), patch.object(d,'Batch',return_value=batch):
                with self.assertRaises(FileExistsError): d.run_round(3)
                self.assertEqual(lock.read_text(encoding='ascii'),'somebody-else')


if __name__ == '__main__': unittest.main()
