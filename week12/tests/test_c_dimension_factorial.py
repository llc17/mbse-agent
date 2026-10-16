"""Four offline fixture/mock checks; no OMC, parser or online tools launched."""
from copy import deepcopy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from week12.src.integration import dimension_factorial as d


class DimensionFactorialChecks(unittest.TestCase):
    def setUp(self):
        self.started_at = time.monotonic()

    def tearDown(self):
        print(f'DEVELOPMENT_TEST_SECONDS {self._testMethodName} {time.monotonic()-self.started_at:.6f}')

    def test_two_fixed_corners_original_model_build_and_true_launch(self):
        _,packet,bundle,original = d._original_inputs()
        d._check_reuse()
        before = deepcopy(original)
        self.assertEqual(d.CORNER_ORDER,('tolerance_only','step_only'))
        self.assertIs(d.compare_failure,d.prior.compare_failure)
        for corner in d.CORNER_ORDER:
            effective = d.effective_contract(original,corner)
            self.assertEqual(original,before)
            restored = deepcopy(effective); restored['numerical']['refined'] = deepcopy(d.prior.NUMERICS)
            self.assertEqual(restored,original)
            changed = {k for k,v in d.prior.NUMERICS.items() if v != d.NUMERICS[corner][k]}
            self.assertEqual(changed,{'tolerance'} if corner == 'tolerance_only' else {'max_step_s'})
            # Prior inputs were really guarded above. Reuse that immutable fixture
            # for both generation branches instead of rehashing it four more times.
            with patch.object(d,'_original_inputs',return_value=(None,packet,bundle,original)):
                generated,build = d._generated(packet,bundle,effective,corner)
            self.assertEqual(generated['modelica'].encode('utf-8'),(d.BASE/'V11CGenerated.mo').read_bytes())
            old = (d.BASE/'build.mos').read_text(encoding='utf-8')
            if corner == 'tolerance_only':
                self.assertEqual(build.replace(f'tolerance={1e-8:.17g}',f'tolerance={1e-7:.17g}',1),old)
            else: self.assertEqual(build,old)
            bad = deepcopy(effective); bad['physical'] = {}
            with patch.object(d,'_original_inputs',return_value=(None,packet,bundle,original)):
                with self.assertRaises(ValueError): d._generated(packet,bundle,bad,corner)
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); n = d.NUMERICS[corner]
                xml = root/'model_init.xml'
                xml.write_text('<root><DefaultExperiment startTime="0" stopTime="35" stepSize="0.001" '
                    f'tolerance="{n["tolerance"]}" solver="dassl" outputFormat="csv"/></root>',encoding='utf-8')
                raw = ['model','-s=dassl',f'-maxStepSize={n["max_step_s"]}','-r=result.csv']
                batch = Mock()
                def run(command,cwd,timeout,log_dir,label):
                    self.assertEqual(command,[*raw,d.STATS_FLAG])
                    (root/'simulation.stdout.log').write_text('OMC_LOG_STATS | info | steps: 123',encoding='utf-8')
                    return {'returncode':0,'timed_out':False}
                batch.run.side_effect = run
                proxy = d._NumericsBatch(batch,corner)
                with self.assertRaises(ValueError): proxy.run([*raw,'-lv=LOG_SOLVER'],root,180,root,'simulation')
                batch.run.assert_not_called()
                proxy.run(raw,root,180,root,'simulation')
                actual = d.read(root/'actual-numerics.json')
                self.assertEqual(actual['launch_command'],[*raw,d.STATS_FLAG])
                self.assertEqual(actual['numerics'],n)
                d._check_telemetry(root,corner)
                xml.write_text(xml.read_text().replace('stopTime="35"','stopTime="36"'),encoding='utf-8')
                batch.run.reset_mock()
                with self.assertRaises(ValueError): proxy.run(raw,root,180,root,'simulation')
                batch.run.assert_not_called()
        with self.assertRaises(ValueError): d.effective_contract(original,'next_precision')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); n = d.NUMERICS['step_only']
            (root/'model_init.xml').write_text('<root><DefaultExperiment startTime="0" stopTime="35" '
                'stepSize="0.001" tolerance="1e-7" solver="dassl" outputFormat="csv"/></root>',encoding='utf-8')
            batch = Mock()
            def no_stats(*args):
                (root/'simulation.stdout.log').write_text('simulation finished without telemetry',encoding='utf-8')
                return {'returncode':0,'timed_out':False}
            batch.run.side_effect = no_stats
            with self.assertRaisesRegex(RuntimeError,'TELEMETRY_INCOMPLETE'):
                d._NumericsBatch(batch,'step_only').run(
                    ['model','-s=dassl',f'-maxStepSize={n["max_step_s"]}','-r=result.csv'],root,180,root,'simulation')
            self.assertEqual(d.read(root/'solver-statistics.json')['status'],'TELEMETRY_INCOMPLETE')

    def test_exact_previous_thirty_three_and_new_three_evidenced_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); files = [root/f'source-{i}.py' for i in range(36)]
            for i,path in enumerate(files): path.write_text(f'x={i}\n',encoding='utf-8')
            rows = [{'path':str(p),'sha256':d.sha(p)} for p in files]
            original = {'numerical':{'refined':deepcopy(d.prior.NUMERICS)}}
            packet = {'fixed_inputs':[],'readonly_manifest':[]}
            old_identity = {'implementation_manifest':rows[:33]}
            identity = {'specification_digest':'diag','fixed_implementation_manifest':rows[:33],
                'implementation_manifest':rows,'fixed_inputs':[],'readonly_manifest':[],
                'vehicle_verification_digest':d.digest(original),
                'effective_verification_digests':{c:d.digest(d.effective_contract(original,c)) for c in d.CORNER_ORDER},
                'reused_evidence':[],'review_evidence':[]}
            with patch.object(d,'ROOT',root), patch.object(d.previous,'ROOT',root), \
                    patch.object(d,'NEW_PATHS',tuple(p.name for p in files[33:])), \
                    patch.object(d,'_original_inputs',return_value=(old_identity,packet,{},original)), \
                    patch.object(d,'_fixed_sources',return_value=rows[:33]), \
                    patch.object(d.reference,'guard_sources') as old_guard, patch.object(d,'_check_reuse'):
                d.guard_sources(identity,packet,rows)
                self.assertTrue(all(call.args[2] == rows[:33] for call in old_guard.call_args_list))
                with self.assertRaises(ValueError): d.guard_sources(identity,packet,rows[:-1])
                files[33].write_text('x=99\n',encoding='utf-8')
                changed = deepcopy(rows); changed[33]['sha256'] = d.sha(files[33])
                with self.assertRaises(RuntimeError): d.guard_sources(identity,packet,rows)
                d.guard_sources(identity,packet,changed)
                evidence = root/'failure.log'; evidence.write_text('new assertion evidence',encoding='utf-8')
                result = {'status':'STOPPED','retry_eligible':True,'failure_code':'NEW_LOCAL_CHECK_FAILURE',
                    'diagnostic_digest':'diag','physical_specification_digest':d.PHYSICAL_SPEC,'physical_digest':d.PHYSICAL_DIGEST}
                diagnosis = {**result,'classification':'implementation','same_physical_task_verification':True,
                    'reason':'Repair new adapter assertion, retain original physics and factors.',
                    'evidence_files':[{'path':'failure.log','sha256':d.sha(evidence)}],
                    'rebound_implementation_manifest':changed,'changed_paths':[str(files[33].resolve())]}
                with patch.object(d,'OUT',root):
                    self.assertEqual(d.validate_repair(identity,packet,rows,result,diagnosis),changed)
                    bad = deepcopy(diagnosis); bad['evidence_files'][0]['sha256'] = 'bad'
                    with self.assertRaises(ValueError): d.validate_repair(identity,packet,rows,result,bad)
                    with self.assertRaises(ValueError):
                        d.validate_repair(identity,packet,rows,{**result,'failure_code':'TERMINAL'},diagnosis)
                    files[0].write_text('altered frozen source\n',encoding='utf-8')
                    with self.assertRaises(RuntimeError): d.guard_sources(identity,packet,changed)
                    changed[0]['sha256'] = d.sha(files[0])
                    diagnosis['changed_paths'] = [str(files[0].resolve()),str(files[33].resolve())]
                    with self.assertRaises(ValueError): d.validate_repair(identity,packet,rows,result,diagnosis)

    def test_external_receipt_exact_budget_order_and_closed_batch(self):
        identity = {'scope':d.SCOPE,'specification_digest':'diag','case_order':[d.CASE],
            'variant':'refined','stop_time_s':35,'corner_order':list(d.CORNER_ORDER),
            'numerics':deepcopy(d.NUMERICS),'endpoint_numerics':deepcopy(d.ENDPOINT_NUMERICS),
            'edge_factors':deepcopy(d.EDGE_FACTORS),'stats_flag':d.STATS_FLAG,
            'budget':deepcopy(d.BUDGET),'implementation_manifest':[]}
        self.assertEqual(d.validate_budget(identity)['execution_total_wall_s'],3000)
        for field,value in (('corner_order',list(reversed(d.CORNER_ORDER))),('stats_flag','-lv=LOG_SOLVER'),
                            ('budget',{**d.BUDGET,'maximum_scene_attempts':7})):
            bad = deepcopy(identity); bad[field] = value
            with self.assertRaises(ValueError): d.validate_budget(bad)
        bad = deepcopy(identity); bad['numerics']['step_only']['tolerance'] = 1e-8
        with self.assertRaises(ValueError): d.validate_budget(bad)
        used = {'scene':0,'local':0,'sysml':0,'probe':0,'process_seconds':0,'unresolved_processes':0}
        batch = Mock(budget=deepcopy(d.BUDGET)); batch.usage.return_value = used
        d.require_complete_round(batch)
        for change in ({'scene':5},{'local':9},{'process_seconds':2001},{'process_seconds':float('nan')},
                       {'sysml':1},{'probe':1},{'unresolved_processes':1}):
            batch.usage.return_value = {**used,**change}
            with self.assertRaises(ValueError): d.require_complete_round(batch)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); out = root/'execution'; review = root/'review'
            bad = root/'ai.json'; d.save(bad,{'origin':'AI','human_confirmed':True})
            with patch.object(d,'OUT',out), patch.object(d,'REVIEW',review), \
                    patch.object(d,'inputs',return_value=(identity,{}, {}, {})), \
                    patch.object(d,'guard_sources'), patch.object(d,'Batch') as new_batch:
                with self.assertRaises(ValueError): d.initialize(str(bad))
                self.assertFalse(out.exists())
                receipt = root/'user-message.json'
                d.save(receipt,{'origin':'user_message','human_confirmed':True,
                    'message_reference':'fixture external user message','user_text':'确认',
                    'scope':d.SCOPE,'specification_digest':'diag'})
                d.initialize(str(receipt))
                self.assertEqual(d.read(out/'authorization.json'),d.read(receipt))
                new_batch.assert_not_called()
                with self.assertRaises(FileExistsError): d.initialize(str(receipt))
                d.save(out/'final-result.json',{'status':'STOPPED'})
                with self.assertRaisesRegex(ValueError,'CLOSED_DIAGNOSTIC'): d.approved_inputs()

    def test_two_edges_each_exact_metered_child_hard_failure_stop_and_pair_closure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root/'result.csv').write_text('fixture',encoding='utf-8')
            names = [*sorted(d.EXPECTED_FAILED),*[f'passing_{i}' for i in range(63)]]
            report = {'status':'FAIL','checks':[{'name':name,'pass':name not in d.EXPECTED_FAILED} for name in names],
                'diagnostics':[],'issues':[],'metrics':{'case_id':d.CASE,'csv_sha256':d.sha(root/'result.csv')}}
            d.save(root/'physical.json',report)
            with patch.object(d,'BASE',root):
                self.assertEqual(d._check_refined(root),report)
                subset = deepcopy(report); subset['checks'][0]['pass'] = True
                with patch.object(d,'read',side_effect=[subset,report]): d._check_refined(root)
                added = deepcopy(report); added['checks'][5]['pass'] = False
                with patch.object(d,'read',side_effect=[added,report]):
                    with self.assertRaisesRegex(ValueError,'NEW_PHYSICAL'): d._check_refined(root)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp); directory = out/'round-01'; target = directory/'tolerance_only'
            folder = target/(d.CASE+'-refined'); folder.mkdir(parents=True)
            original = {'numerical':{'refined':deepcopy(d.prior.NUMERICS)}}
            identity = {'specification_digest':'diag','existing_diagonal':'old-diagonal.json'}
            d.save(target/'verification-contract.json',d.effective_contract(original,'tolerance_only'))
            d.save(directory/'source-manifest.json',[])
            lock = out/'.execution.lock'; lock.write_text('fixture-parent',encoding='ascii')
            command = [d.sys.executable,'-B','-m',d.CLI,'--compare',str(folder.resolve())]
            start = out/'process-001.start.json'
            d.save(start,{'label':'factor-comparison-tolerance_only','command':command,
                'cwd':str(d.ROOT.parent),'timeout_s':87.5})
            compare = Mock(return_value={'classification':'NUMERICAL_SENSITIVITY','vehicle_acceptance':'NOT_GRANTED'})
            with patch.object(d,'OUT',out), patch.object(d,'approved_inputs',return_value=(identity,{}, {},original)), \
                    patch.object(d,'guard_sources'), patch.object(d,'_check_refined',return_value={}), \
                    patch.object(d,'_check_telemetry'), patch.object(d,'compare_failure',compare):
                result = d.compare_folder(folder)
                self.assertEqual(compare.call_count,2)
                self.assertEqual([call.args[1] for call in compare.call_args_list],
                                 [d.BASE/'result.csv',d.STRICT/'result.csv'])
                self.assertEqual([e['isolated_factor'] for e in result['edges']],['tolerance','max_step_s'])
                d.save(out/'process-001.result.json',{'duration_s':1})
                with self.assertRaisesRegex(ValueError,'EXACT_ACTIVE_METERED'): d.compare_folder(folder)
            lock.unlink()
        for additional_failure in (False,True):
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp); identity = {'specification_digest':'diag','implementation_manifest':[]}
                original = {'numerical':{'refined':deepcopy(d.prior.NUMERICS)}}
                used = {'scene':2,'local':4,'sysml':0,'probe':0,'process_seconds':100.,'unresolved_processes':0}
                batch = Mock(root=out,budget=deepcopy(d.BUDGET)); batch.usage.return_value = used
                def scene(batch,target,bundle,contract,number,manifest,identity,packet,corner):
                    folder = target/(d.CASE+'-refined'); folder.mkdir()
                    return {'compile':'PASS','simulation':'COMPLETED','physical':'FAIL','corner':corner},folder
                def run(command,cwd,timeout,log_dir,label):
                    if label.startswith('factor-comparison-'):
                        self.assertEqual(timeout,87.5)
                        corner = log_dir.parent.name
                        d.save(log_dir/'factor-comparisons.json',{'corner':corner,'vehicle_acceptance':'NOT_GRANTED',
                            'edges':[{'base':r,'corner':corner,'isolated_factor':d.EDGE_FACTORS[corner][r],
                                'classification':'NUMERICAL_SENSITIVITY','vehicle_acceptance':'NOT_GRANTED'}
                                for r in ('A_existing','D_existing')]})
                    return {'returncode':0,'timed_out':False}
                batch.run.side_effect = run
                check = Mock(side_effect=ValueError('NEW_PHYSICAL_TASK_HARD_FAILURE') if additional_failure else None)
                with patch.object(d,'OUT',out), patch.object(d,'approved_inputs',return_value=(identity,{}, {},original)), \
                        patch.object(d,'round_manifest',return_value=[]), patch.object(d,'Batch',return_value=batch), \
                        patch.object(d,'guard_sources'), patch.object(d.previous,'snapshot_inputs'), \
                        patch.object(d.execution,'snapshot_readonly'), patch.object(d,'dependency_audit'), \
                        patch.object(d,'local_targets',return_value=['t1','t2','t3','t4']), \
                        patch.object(d,'_generated'), patch.object(d,'execute_scene',side_effect=scene) as execute, \
                        patch.object(d,'_completed_verification',return_value=2.5), \
                        patch.object(d,'_check_refined',check), patch.object(d,'_check_telemetry'):
                    result = d.run_round(1)
                    self.assertEqual(execute.call_count,1 if additional_failure else 2)
                    self.assertEqual(len(result['edges']),0 if additional_failure else 4)
                    self.assertEqual(result['status'],'STOPPED' if additional_failure else
                                     'DIAGNOSTIC_COMPLETED_VEHICLE_NOT_ACCEPTED')
                    self.assertFalse(result['retry_eligible'])
                    self.assertEqual(d.read(out/'final-result.json')['unused_budget_disposition'],'RETIRED')
                    self.assertFalse((out/'.execution.lock').exists())


if __name__ == '__main__': unittest.main()
