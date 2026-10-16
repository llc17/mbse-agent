"""Four offline adapter checks; no model, parser or online execution."""
import ast
from copy import deepcopy
import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from week12.src.integration import dimension_failure_diagnostic as d


class DimensionFailureDiagnosticChecks(unittest.TestCase):
    def test_external_receipt_and_exact_complete_round_budget(self):
        identity = {'scope':d.SCOPE,'specification_digest':'diagnostic',
            'case_order':[d.CASE],'variant':'refined','stop_time_s':35,
            'numerics':deepcopy(d.NUMERICS),'budget':deepcopy(d.BUDGET),'implementation_manifest':[]}
        self.assertEqual(d.validate_budget(identity)['execution_total_wall_s'],1500)
        for key,value in (('case_order',[d.CASE,'flat']),('variant','base'),
                          ('budget',{**d.BUDGET,'maximum_rounds':4})):
            bad = deepcopy(identity); bad[key] = value
            with self.assertRaises(ValueError): d.validate_budget(bad)
        used = {'scene':0,'local':0,'sysml':0,'probe':0,'process_seconds':0,'unresolved_processes':0}
        batch = Mock(budget=deepcopy(d.BUDGET)); batch.usage.return_value = used
        d.require_complete_round(batch)
        for change in ({'scene':3},{'local':9},{'process_seconds':1001},{'sysml':1},{'probe':1},{'unresolved_processes':1}):
            batch.usage.return_value = {**used,**change}
            with self.assertRaises(ValueError): d.require_complete_round(batch)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); out = root/'execution'
            receipt = root/'external.json'
            d.save(receipt,{'origin':'AI','human_confirmed':True})
            with patch.object(d,'OUT',out), patch.object(d,'inputs',return_value=(identity,{}, {}, {})), \
                    patch.object(d,'guard_sources'), patch.object(d,'Batch') as new_batch:
                with self.assertRaises(ValueError): d.initialize(str(receipt))
                self.assertFalse(out.exists())
                good = root/'actual-message.json'
                d.save(good,{'origin':'user_message','human_confirmed':True,
                    'message_reference':'test external reference','user_text':'确认',
                    'scope':d.SCOPE,'specification_digest':'diagnostic'})
                d.initialize(str(good))
                self.assertEqual(d.read(out/'authorization.json'),d.read(good))
                self.assertEqual(d.read(out/'policy.json')['budget'],d.BUDGET)
                new_batch.assert_not_called()
                with self.assertRaises(FileExistsError): d.initialize(str(good))
                d.save(out/'final-result.json',{'status':'STOPPED'})
                with self.assertRaisesRegex(ValueError,'CLOSED_DIAGNOSTIC'): d.approved_inputs()

    def test_fixed_26_proxy_comparator_and_repair_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); files = []
            for index in range(30):
                path = root/f'source-{index}.py'; path.write_text(f'x={index}\n',encoding='utf-8'); files.append(path)
            rows = [{'path':str(p),'sha256':d.sha(p)} for p in files]
            identity = {'specification_digest':'diag','fixed_implementation_manifest':rows[:27],
                'implementation_manifest':rows,'fixed_inputs':[],'readonly_manifest':[],
                'reused_evidence':[],'review_evidence':[]}
            packet = {'fixed_inputs':[],'readonly_manifest':[]}
            new_names = tuple(p.name for p in files[27:])
            with patch.object(d,'ROOT',root), patch.object(d.previous,'ROOT',root), \
                    patch.object(d,'NEW_PATHS',new_names), patch.object(d,'COMPARATOR_PATH',files[26]), \
                    patch.object(d,'COMPARATOR_SHA',rows[26]['sha256']), \
                    patch.object(d,'_fixed_sources',return_value=rows[:26]), \
                    patch.object(d.execution,'guard_sources') as old_guard:
                d.guard_sources(identity,packet,rows)
                self.assertTrue(all(call.args[1] == rows[:26] for call in old_guard.call_args_list))
                with self.assertRaises(ValueError): d.guard_sources(identity,packet,rows[:29])
                changed = deepcopy(rows); files[27].write_text('x=99\n',encoding='utf-8')
                changed[27]['sha256'] = d.sha(files[27])
                with self.assertRaises(RuntimeError): d.guard_sources(identity,packet,rows)
                d.guard_sources(identity,packet,changed)
                evidence = root/'failure.log'; evidence.write_text('concrete adapter assertion',encoding='utf-8')
                result = {'status':'STOPPED','retry_eligible':True,'failure_code':'NEW_LOCAL_CHECK_FAILURE',
                    'diagnostic_digest':'diag','physical_specification_digest':d.PHYSICAL_SPEC,
                    'physical_digest':d.PHYSICAL_DIGEST}
                diagnosis = {**result,'classification':'implementation','same_physical_task_verification':True,
                    'reason':'Fix the new local adapter assertion, preserving physical inputs.',
                    'evidence_files':[{'path':'failure.log','sha256':d.sha(evidence)}],
                    'rebound_implementation_manifest':changed,'changed_paths':[str(files[27].resolve())]}
                with patch.object(d,'OUT',root):
                    self.assertEqual(d.validate_repair(identity,packet,rows,result,diagnosis),changed)
                    bad = deepcopy(diagnosis); bad['changed_paths'] = []
                    with self.assertRaises(ValueError): d.validate_repair(identity,packet,rows,result,bad)
                    bad = deepcopy(diagnosis); bad['evidence_files'][0]['sha256'] = 'bad'
                    with self.assertRaises(ValueError): d.validate_repair(identity,packet,rows,result,bad)
                    bad = {**result,'failure_code':'TERMINAL'}
                    with self.assertRaises(ValueError): d.validate_repair(identity,packet,rows,bad,diagnosis)
                files[26].write_text('altered comparator',encoding='utf-8')
                with self.assertRaises(RuntimeError): d.guard_sources(identity,packet,changed)

    def test_reused_real_failure_comparator_and_known_failure_guard(self):
        self.assertIs(d.compare_failure,d.comparator.compare_failure)
        self.assertIs(d._completed_verification,d.comparator._completed_verification)
        rules = {'position_abs_m':.001,'pitch_abs_rad':.0017453292519943296,
            'velocity_abs_m_s':.001,'wheel_speed_abs_rad_s':.01,'state_relative':.01,
            'peak_contact_force_relative':.05,'peak_contact_force_abs_N':1.,
            'event_time_abs_s':.002,'energy_relative':.01,'energy_abs_J':.1}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); paths = [root/'base.csv',root/'refined.csv']
            def write(path,shift):
                with path.open('w',encoding='utf-8',newline='') as stream:
                    writer = csv.writer(stream); writer.writerow(['time','body_chassis_x'])
                    for i in range(35001): writer.writerow([i/1000,1.+shift])
            for path in paths: write(path,0.)
            names = [*sorted(d.EXPECTED_FAILED),*[f'passing_{i}' for i in range(63)]]
            def report(path):
                return {'status':'FAIL','checks':[{'name':name,'pass':name not in d.EXPECTED_FAILED} for name in names],
                    'diagnostics':[],'issues':[],'metrics':{'case_id':d.CASE,'csv_sha256':d.sha(path),
                    'signals':{'body_chassis_x':{'peak_abs':1.,'terminal':1.}},
                    'event_times':{'front_riser_1':10.},'cumulative':{'shaft_work':500.}}}
            base, refined = [report(p) for p in paths]
            def compare(): return d.compare_failure({'numerical_comparison':rules},*paths,base,refined)
            self.assertEqual(compare()['classification'],'FAILURE_REPRODUCED_NUMERICALLY')
            self.assertEqual(compare()['vehicle_acceptance'],'NOT_GRANTED')
            write(paths[1],.1); refined['metrics']['csv_sha256'] = d.sha(paths[1])
            self.assertEqual(compare()['classification'],'NUMERICAL_SENSITIVITY')
            self.assertEqual(d._failed_checks(base),d.EXPECTED_FAILED)
            refined['checks'][0]['pass'] = True
            self.assertEqual(compare()['classification'],'TASK_OUTCOME_CHANGED')
            self.assertEqual(base['status'],'FAIL')
            refined['checks'][5]['pass'] = False
            d.save(root/'physical.json',refined)
            with patch.object(d,'BASE',root), patch.object(d,'sha',return_value=refined['metrics']['csv_sha256']):
                with self.assertRaisesRegex(ValueError,'NEW_PHYSICAL'): d._check_refined(root)

    def test_shared_postprocess_terminal_closure_and_owned_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp); identity = {'specification_digest':'diag','implementation_manifest':[]}
            used = {'scene':1,'local':4,'sysml':0,'probe':0,'process_seconds':100.,'unresolved_processes':0}
            batch = Mock(root=out,budget=deepcopy(d.BUDGET)); batch.usage.return_value = used
            batch.run.return_value = {'returncode':0,'timed_out':False}
            folder = out/'round-01'/(d.CASE+'-refined')
            def scene(*args):
                folder.mkdir()
                return {'compile':'PASS','simulation':'COMPLETED','physical':'FAIL'},folder
            def run(command,cwd,timeout,log_dir,label):
                if label == 'failure-comparison':
                    self.assertEqual(timeout,57.5)
                    d.save(log_dir/'failure-diagnostic.json',{'classification':'NUMERICAL_SENSITIVITY',
                        'vehicle_acceptance':'NOT_GRANTED'})
                return {'returncode':0,'timed_out':False}
            batch.run.side_effect = run
            with patch.object(d,'OUT',out), patch.object(d,'approved_inputs',return_value=(identity,{}, {}, {})), \
                    patch.object(d,'round_manifest',return_value=[]), patch.object(d,'Batch',return_value=batch), \
                    patch.object(d,'guard_sources'), patch.object(d.previous,'snapshot_inputs'), \
                    patch.object(d.execution,'snapshot_readonly'), patch.object(d,'dependency_audit'), \
                    patch.object(d,'local_targets',return_value=['t1','t2','t3','t4']), \
                    patch.object(d,'_generated'), patch.object(d,'execute_scene',side_effect=scene), \
                    patch.object(d,'_completed_verification',return_value=2.5), patch.object(d,'_check_refined'):
                result = d.run_round(1)
                self.assertEqual(result['classification'],'NUMERICAL_SENSITIVITY')
                self.assertEqual(result['status'],'DIAGNOSTIC_COMPLETED_VEHICLE_NOT_ACCEPTED')
                self.assertEqual(d.read(out/'final-result.json')['unused_budget_disposition'],'RETIRED')
                self.assertFalse((out/'.execution.lock').exists())
                with self.assertRaises(ValueError): d.run_round(1)
            lock = out/'.execution.lock'; lock.write_text('other-owner',encoding='ascii')
            with patch.object(d,'OUT',out), patch.object(d,'approved_inputs',return_value=(identity,{}, {}, {})), \
                    patch.object(d,'round_manifest',return_value=[]), patch.object(d,'Batch',return_value=batch):
                with self.assertRaises(FileExistsError): d.run_round(2)
                self.assertEqual(lock.read_text(encoding='ascii'),'other-owner')
            source = Path(d.__file__).read_text(encoding='utf-8')
            tree = ast.parse(source)
            self.assertFalse(any(isinstance(node,ast.Expr) and isinstance(node.value,ast.Call)
                and isinstance(node.value.func,ast.Name) and node.value.func.id in ('run_round','initialize','prepare')
                for node in tree.body))


if __name__ == '__main__': unittest.main()
