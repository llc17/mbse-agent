"""Four offline checks for the new receipt-bound stricter numerical reference."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from week12.src.integration import dimension_reference as d


class DimensionReferenceChecks(unittest.TestCase):
    def test_original_contract_model_and_only_build_tolerance_delta(self):
        _,packet,bundle,original = d.prior.inputs()
        before = deepcopy(original); effective = d.effective_contract(original)
        self.assertEqual(original,before)
        self.assertEqual(effective['numerical']['refined'],d.NUMERICS)
        restored = deepcopy(effective); restored['numerical']['refined'] = deepcopy(d.prior.NUMERICS)
        self.assertEqual(restored,original)
        generated,build = d._generated(packet,bundle,effective)
        self.assertEqual(generated['modelica'].encode('utf-8'),(d.BASE/'V11CGenerated.mo').read_bytes())
        old = (d.BASE/'build.mos').read_text(encoding='utf-8')
        self.assertEqual(build.replace(f'tolerance={d.NUMERICS["tolerance"]:.17g}',
            f'tolerance={d.prior.NUMERICS["tolerance"]:.17g}',1),old)
        bad = deepcopy(effective); bad['physical'] = {}
        with self.assertRaises(ValueError): d._generated(packet,bundle,bad)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); xml = root/'model_init.xml'
            xml.write_text('<root><DefaultExperiment startTime="0" stopTime="35" stepSize="0.001" '
                'tolerance="1e-8" solver="dassl" outputFormat="csv"/></root>',encoding='utf-8')
            batch = Mock(); proxy = d._NumericsBatch(batch)
            command = ['model','-s=dassl','-maxStepSize=0.00025','-r=result.csv']
            proxy.run(command,root,180,root,'simulation')
            self.assertEqual(d.read(root/'actual-numerics.json')['numerics'],d.NUMERICS)
            batch.run.assert_called_once_with(command,root,180,root,'simulation')
            batch.run.reset_mock(); xml.write_text(xml.read_text().replace('1e-8','1e-7'),encoding='utf-8')
            with self.assertRaises(ValueError): proxy.run(command,root,180,root,'simulation')
            batch.run.assert_not_called()

    def test_fixed_thirty_guard_and_exact_new_three_repair_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); files = [root/f'source-{i}.py' for i in range(33)]
            for i,path in enumerate(files): path.write_text(f'x={i}\n',encoding='utf-8')
            rows = [{'path':str(p),'sha256':d.sha(p)} for p in files]
            original = {'numerical':{'refined':deepcopy(d.prior.NUMERICS)}}
            packet = {'fixed_inputs':[],'readonly_manifest':[]}
            old_identity = {'implementation_manifest':rows[:30]}
            identity = {'specification_digest':'diag','fixed_implementation_manifest':rows[:30],
                'implementation_manifest':rows,'fixed_inputs':[],'readonly_manifest':[],
                'vehicle_verification_digest':d.digest(original),
                'effective_verification_digest':d.digest(d.effective_contract(original)),
                'reused_evidence':[],'review_evidence':[]}
            with patch.object(d,'ROOT',root), patch.object(d.previous,'ROOT',root), \
                    patch.object(d,'NEW_PATHS',tuple(p.name for p in files[30:])), \
                    patch.object(d.prior,'inputs',return_value=(old_identity,packet,{},original)), \
                    patch.object(d,'_fixed_sources',return_value=rows[:30]), \
                    patch.object(d.prior,'guard_sources') as old_guard, patch.object(d,'_check_reuse'):
                d.guard_sources(identity,packet,rows)
                self.assertTrue(all(call.args[2] == rows[:30] for call in old_guard.call_args_list))
                with self.assertRaises(ValueError): d.guard_sources(identity,packet,rows[:-1])
                files[30].write_text('x=99\n',encoding='utf-8')
                changed = deepcopy(rows); changed[30]['sha256'] = d.sha(files[30])
                with self.assertRaises(RuntimeError): d.guard_sources(identity,packet,rows)
                d.guard_sources(identity,packet,changed)
                evidence = root/'failure.log'; evidence.write_text('new assertion evidence',encoding='utf-8')
                result = {'status':'STOPPED','retry_eligible':True,'failure_code':'NEW_LOCAL_CHECK_FAILURE',
                    'diagnostic_digest':'diag','physical_specification_digest':d.PHYSICAL_SPEC,'physical_digest':d.PHYSICAL_DIGEST}
                diagnosis = {**result,'classification':'implementation','same_physical_task_verification':True,
                    'reason':'Repair only the new adapter assertion with original physics unchanged.',
                    'evidence_files':[{'path':'failure.log','sha256':d.sha(evidence)}],
                    'rebound_implementation_manifest':changed,'changed_paths':[str(files[30].resolve())]}
                with patch.object(d,'OUT',root):
                    self.assertEqual(d.validate_repair(identity,packet,rows,result,diagnosis),changed)
                    with self.assertRaises(ValueError):
                        d.validate_repair(identity,packet,rows,{**result,'failure_code':'TERMINAL'},diagnosis)
                    bad = deepcopy(diagnosis); bad['evidence_files'][0]['sha256'] = 'bad'
                    with self.assertRaises(ValueError): d.validate_repair(identity,packet,rows,result,bad)
                files[0].write_text('altered frozen source\n',encoding='utf-8')
                with self.assertRaises(RuntimeError): d.guard_sources(identity,packet,changed)
                altered = deepcopy(changed); altered[0]['sha256'] = d.sha(files[0])
                with self.assertRaises(ValueError): d.guard_sources(identity,packet,altered)

    def test_external_approval_complete_round_budget_and_closed_batch(self):
        identity = {'scope':d.SCOPE,'specification_digest':'diag','case_order':[d.CASE],
            'variant':'refined','stop_time_s':35,'numerics':deepcopy(d.NUMERICS),
            'budget':deepcopy(d.BUDGET),'implementation_manifest':[]}
        self.assertEqual(d.validate_budget(identity)['execution_total_wall_s'],1500)
        bad = deepcopy(identity); bad['numerics'] = deepcopy(d.prior.NUMERICS)
        with self.assertRaises(ValueError): d.validate_budget(bad)
        used = {'scene':0,'local':0,'sysml':0,'probe':0,'process_seconds':0,'unresolved_processes':0}
        batch = Mock(budget=deepcopy(d.BUDGET)); batch.usage.return_value = used
        d.require_complete_round(batch)
        for change in ({'scene':3},{'local':9},{'process_seconds':1001},{'sysml':1},{'probe':1},{'unresolved_processes':1}):
            batch.usage.return_value = {**used,**change}
            with self.assertRaises(ValueError): d.require_complete_round(batch)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); out = root/'execution'; receipt = root/'external.json'
            d.save(receipt,{'origin':'AI','human_confirmed':True})
            with patch.object(d,'OUT',out), patch.object(d,'REVIEW',root/'review'), \
                    patch.object(d,'inputs',return_value=(identity,{}, {}, {})), patch.object(d,'guard_sources'), \
                    patch.object(d,'Batch') as factory:
                with self.assertRaises(ValueError): d.initialize(str(receipt))
                self.assertFalse(out.exists())
                good = root/'actual-message.json'
                d.save(good,{'origin':'user_message','human_confirmed':True,'message_reference':'external reference',
                    'user_text':'确认','scope':d.SCOPE,'specification_digest':'diag'})
                d.initialize(str(good)); factory.assert_not_called()
                self.assertEqual(d.read(out/'policy.json')['budget'],d.BUDGET)
                with self.assertRaises(FileExistsError): d.initialize(str(good))
                d.save(out/'final-result.json',{'status':'STOPPED'})
                with self.assertRaisesRegex(ValueError,'CLOSED_DIAGNOSTIC'): d.approved_inputs()

    def test_shared_comparison_budget_known_failures_and_terminal_closure(self):
        self.assertIs(d.compare_failure,d.prior.compare_failure)
        self.assertIs(d._completed_verification,d.prior._completed_verification)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); out = root/'execution'; out.mkdir(); review = root/'review'; review.mkdir()
            d.save(review/'original-contract.json',{})
            identity = {'specification_digest':'diag','implementation_manifest':[]}
            used = {'scene':1,'local':4,'sysml':0,'probe':0,'process_seconds':100.,'unresolved_processes':0}
            batch = Mock(root=out,budget=deepcopy(d.BUDGET)); batch.usage.return_value = used
            folder = out/'round-01'/(d.CASE+'-refined')
            def scene(*args):
                folder.mkdir(); return {'compile':'PASS','simulation':'COMPLETED','physical':'FAIL'},folder
            def run(command,cwd,timeout,log_dir,label):
                if label == 'failure-comparison':
                    self.assertEqual(timeout,57.5)
                    self.assertEqual(command[3],'week12.scripts.s3_c_dimension_reference')
                    d.save(log_dir/'failure-diagnostic.json',{'classification':'NUMERICAL_SENSITIVITY','vehicle_acceptance':'NOT_GRANTED'})
                return {'returncode':0,'timed_out':False}
            batch.run.side_effect = run
            with patch.object(d,'OUT',out), patch.object(d,'REVIEW',review), \
                    patch.object(d,'approved_inputs',return_value=(identity,{}, {}, {})), \
                    patch.object(d,'round_manifest',return_value=[]), patch.object(d,'Batch',return_value=batch), \
                    patch.object(d,'guard_sources'), patch.object(d.previous,'snapshot_inputs'), \
                    patch.object(d.execution,'snapshot_readonly'), patch.object(d,'dependency_audit'), \
                    patch.object(d,'local_targets',return_value=['t1','t2','t3','t4']), patch.object(d,'_generated'), \
                    patch.object(d,'execute_scene',side_effect=scene), patch.object(d,'_completed_verification',return_value=2.5), \
                    patch.object(d,'_check_refined'):
                result = d.run_round(1)
                self.assertEqual(result['status'],'DIAGNOSTIC_COMPLETED_VEHICLE_NOT_ACCEPTED')
                self.assertEqual(d.read(out/'final-result.json')['unused_budget_disposition'],'RETIRED')
                self.assertFalse((out/'.execution.lock').exists())
            base = root/'base'; base.mkdir(); d.save(base/'result.csv',{})
            names = [*sorted(d.EXPECTED_FAILED),*[f'passing_{i}' for i in range(63)]]
            report = {'status':'FAIL','checks':[{'name':n,'pass':n not in d.EXPECTED_FAILED} for n in names],
                'diagnostics':[],'issues':[],'metrics':{'case_id':d.CASE,'csv_sha256':d.sha(base/'result.csv')}}
            d.save(base/'physical.json',report)
            with patch.object(d,'BASE',base):
                self.assertEqual(d._check_refined(base),report)
                bad = deepcopy(report); bad['checks'][-1]['pass'] = False
                with patch.object(d,'read',return_value=bad):
                    with self.assertRaisesRegex(ValueError,'NEW_PHYSICAL'): d._check_refined(base)
            with patch.object(d,'OUT',out), patch.object(d,'approved_inputs',return_value=(identity,{}, {}, {})):
                with self.assertRaisesRegex(ValueError,'ACTIVE_PARENT'): d.compare_folder(folder)
                (out/'.execution.lock').write_text('other-owner',encoding='ascii')
                with self.assertRaisesRegex(ValueError,'METERED_COMPARISON'): d.compare_folder(folder)


if __name__ == '__main__': unittest.main()
