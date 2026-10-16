"""Exactly four offline guard tests; no Modelica/SysML/online execution."""
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from week12.src.integration import contact_damping_diagnostic as d


class ContactDampingDiagnosticChecks(unittest.TestCase):
    def test_single_parameter_exact_budget_and_isolated_codegen(self):
        original = {'design':{'wheels':[{'id':n,'count':2} for n in ('rear','front','nose')]},
            'physical':{'contact_per_physical_wheel':{'stiffness_N_m':50000,'damping_Ns_m':100,'friction':.85},
                'wheel_pair_lowering':{'stiffness_N_m':100000,'damping_Ns_m':200},
                'status':'original','physical_digest':'original','provenance_notes':{'mass_split':'85kg'},
                'controller':{'kp':12},'CoM/J':'unchanged'}, 'binding':{'mass_kg':85}}
        contract = {'case_order':['original'],'scope':'original','physical':{'penetration':.0105},
                    'budget_proposal':{},'numerical_comparison':{'position_abs_m':.001}}
        unchanged = deepcopy(original)
        bundle, amended = d.build_draft(original,contract)
        self.assertEqual(original,unchanged)
        back = deepcopy(bundle)
        back['physical']['contact_per_physical_wheel']['damping_Ns_m'] = 100
        back['physical']['wheel_pair_lowering']['damping_Ns_m'] = 200
        back['physical']['status'] = 'original'; back['physical']['physical_digest'] = 'original'
        del back['physical']['provenance_notes']['contact_damping_diagnostic']
        self.assertEqual(back,original)
        self.assertEqual(amended['physical'],contract['physical'])
        self.assertEqual(amended['numerical_comparison'],contract['numerical_comparison'])
        packet = {'budget':deepcopy(d.BUDGET),'case_order':list(d.CASE_ORDER),'variants':['base','refined'],
                  'specification_digest':'new','verification_digest':'verify'}
        self.assertEqual(d.validate_budget(packet)['execution_total_wall_s'],5400)
        self.assertEqual(d.BUDGET['maximum_scene_attempts'],12)
        for key, value in (('budget',{**d.BUDGET,'maximum_rounds':4}),('variants',['base']),
                           ('case_order',[d.STAIRS,d.FLAT])):
            bad = deepcopy(packet); bad[key] = value
            with self.assertRaises(ValueError): d.validate_budget(bad)
        source = '\n'.join('    WheelPairWithTerrainPort wheel_'+w['id']+
            '(m=2.0, k=100000.0, c=200.0, mu=0.85);' for w in original['design']['wheels'])
        source += '\n// '+d.PHYSICAL_SPEC
        generated = {'modelica':source,'sysml':'// historical identity '+d.PHYSICAL_SPEC,
                     'trace':{'generator_version':'old','frozen':{}},'model_class':'Scene'}
        with patch.object(d.vehicle,'generate',side_effect=lambda *a:deepcopy(generated)):
            result = d._generate(bundle,d.FLAT,packet,original)
            self.assertEqual(result['modelica'].count('c=3000.0'),3)
            self.assertEqual(result['sysml'],generated['sysml'])
            self.assertEqual(result['modelica'].replace('c=3000.0','c=200.0').replace('new',d.PHYSICAL_SPEC),source)
        def case_generated(bundle,case):
            value = deepcopy(generated)
            value['sysml'] = '// case='+case+' specification_digest='+d.PHYSICAL_SPEC+'\npart structure;'
            return value
        with patch.object(d.vehicle,'generate',side_effect=case_generated):
            stairs = d._generate(bundle,d.STAIRS,packet,original)
            self.assertEqual(stairs['sysml'],case_generated(original,d.FLAT)['sysml'])
        def inconsistent_structure(bundle,case):
            value = case_generated(bundle,case)
            if case == d.STAIRS: value['sysml'] += '\npart extra;'
            return value
        with patch.object(d.vehicle,'generate',side_effect=inconsistent_structure):
            with self.assertRaisesRegex(ValueError,'ONLY_SYSML_CASE_COMMENT'):
                d._generate(bundle,d.STAIRS,packet,original)
        proxy = d._VehicleProxy()
        self.assertIs(proxy.generate,d.generate)
        self.assertIs(proxy.CANDIDATE,d.vehicle.CANDIDATE)
        with self.assertRaises(AttributeError): proxy.generate = Mock()
        fake = Mock(); usage = {'scene':0,'local':0,'sysml':0,'probe':0,'process_seconds':0,'unresolved_processes':0}
        fake.usage.return_value = usage; d.require_complete_round(fake)
        for change in ({'scene':9},{'local':9},{'process_seconds':3601},{'sysml':1},{'unresolved_processes':1}):
            fake.usage.return_value = {**usage,**change}
            with self.assertRaises(ValueError): d.require_complete_round(fake)

    def test_real_receipt_no_overwrite_and_immutable_source_evidence_guards(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); out = root/'out'
            fixed, new, evidence = (root/n for n in ('old.py','new.py','old.csv'))
            for path in (fixed,new,evidence): path.write_text('original',encoding='utf-8')
            rows = [{'path':str(p),'sha256':d.sha(p)} for p in (fixed,new)]
            packet = {'scope':'diagnostic','specification_digest':'new','physical_digest':'phys','bundle_digest':'bundle',
                'fixed_inputs':[],'implementation_manifest':rows,'fixed_implementation_manifest':[rows[0]],
                'reused_evidence':[{'path':'old.csv','sha256':d.sha(evidence)}]}
            with patch.object(d,'ROOT',root), patch.object(d.execution,'ROOT',root), \
                    patch.object(d.execution,'guard_sources',side_effect=lambda p,m:d.require_sources(m)):
                d.guard_sources(packet,rows)
                repaired = deepcopy(rows); new.write_text('repair',encoding='utf-8'); repaired[1]['sha256'] = d.sha(new)
                d.guard_sources(packet,repaired)
                with self.assertRaises(RuntimeError): d.guard_sources(packet,rows)
                illegal = deepcopy(repaired); illegal[0]['sha256'] = 'f'*64
                with self.assertRaisesRegex(ValueError,'IMMUTABLE'): d.guard_sources(packet,illegal)
                with self.assertRaises(ValueError): d.guard_sources(packet,repaired[:1])
                evidence.write_text('drift',encoding='utf-8')
                with self.assertRaisesRegex(ValueError,'REUSED_EVIDENCE'): d.guard_sources(packet,repaired)
            packet['implementation_manifest'] = []
            bad, receipt = root/'bad.json',root/'user.json'
            d.save(bad,{'origin':'AI','human_confirmed':True})
            d.save(receipt,{'origin':'user_message','human_confirmed':True,'scope':'diagnostic',
                'specification_digest':'new','message_reference':'actual user message','user_text':'确认'})
            with patch.object(d,'OUT',out), patch.object(d,'inputs',return_value=(packet,{},{})), \
                    patch.object(d,'guard_sources'), patch.object(d,'Batch') as batch:
                with self.assertRaises(ValueError): d.initialize(str(bad))
                self.assertFalse(out.exists())
                d.initialize(str(receipt)); batch.assert_not_called()
                self.assertEqual(d.read(out/'authorization.json'),d.read(receipt))
                with self.assertRaises(FileExistsError): d.initialize(str(receipt))
                with self.assertRaises(ValueError): d.initialize('relative.json')

    def test_task_failure_subset_hard_failure_and_new_pair_numerical_classification(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv = Path(tmp)/'result.csv'; csv.write_text('measured',encoding='utf-8')
            def report(names,case=d.STAIRS):
                return {'status':'FAIL' if names else 'PASS','diagnostics':[],'issues':[],
                    'metrics':{'case_id':case,'csv_sha256':d.sha(csv)},
                    'checks':[{'name':'energy_closure','pass':True}]+[{'name':n,'pass':False} for n in names]}
            for name in d.KNOWN_TASK_FAILURES:
                self.assertEqual(d.classify_physical(d.STAIRS,report([name]),csv),'KNOWN_STAIRS_TASK_FAIL')
            self.assertEqual(d.classify_physical(d.STAIRS,report(list(d.KNOWN_TASK_FAILURES)),csv),'KNOWN_STAIRS_TASK_FAIL')
            self.assertEqual(d.classify_physical(d.STAIRS,report([]),csv),'PHYSICAL_PASS')
            for names in (['max_penetration'],['energy_failure'],['nonwheel_clearance'],['continuous_high_slip','energy_failure']):
                self.assertEqual(d.classify_physical(d.STAIRS,report(names),csv),'HARD_PHYSICAL_FAILURE')
            self.assertEqual(d.classify_physical(d.FLAT,report(['continuous_high_slip'],d.FLAT),csv),'HARD_PHYSICAL_FAILURE')
            broken = report([]); broken['metrics']['csv_sha256'] = 'wrong'
            with self.assertRaises(ValueError): d.classify_physical(d.STAIRS,broken,csv)
            folder = Path(tmp)
            (folder/'simulation.stderr.log').write_text(
                'Wheel centre entered the terrain solid; fixture is physically invalid',encoding='utf-8')
            with self.assertRaisesRegex(d.TerminalFailure,'PHYSICAL_SIMULATION_ASSERTION'):
                d.scene_tool_failure({'compile':'PASS','simulation':'FAILED_OR_TIMED_OUT'},folder)
            (folder/'simulation.stderr.log').write_text('unclassified failure',encoding='utf-8')
            with self.assertRaisesRegex(d.TerminalFailure,'CLASSIFICATION_UNRESOLVED'):
                d.scene_tool_failure({'compile':'PASS','simulation':'FAILED_OR_TIMED_OUT'},folder)
            result = {'classification':'TASK_OUTCOME_CHANGED','base_failed_checks':[],
                      'refined_failed_checks':[],'checks':[{'pass':True}]}
            a,b = report([]),report([])
            self.assertEqual(d.classify_numerical(result,a,b),'TASK_PASS_NUMERICALLY_CONSISTENT')
            a,b = report(['continuous_high_slip']),report(['continuous_high_slip'])
            result.update(classification='FAILURE_REPRODUCED_NUMERICALLY',base_failed_checks=['continuous_high_slip'],
                          refined_failed_checks=['continuous_high_slip'])
            self.assertEqual(d.classify_numerical(result,a,b),'FAILURE_REPRODUCED_NUMERICALLY')
            result['refined_failed_checks'] = []
            self.assertEqual(d.classify_numerical(result,a,b),'NUMERICAL_UNRESOLVED')
            result['refined_failed_checks'] = ['continuous_high_slip']; result['checks'][0]['pass'] = False
            self.assertEqual(d.classify_numerical(result,a,b),'NUMERICAL_UNRESOLVED')
            result['classification'] = 'INCOMPLETE'
            self.assertEqual(d.classify_numerical(result,a,b),'INCOMPLETE')

    def test_mock_order_flat_gate_stops_closure_no_overwrite_and_owned_lock(self):
        for scenario in ('complete','local','flat_hard','flat_numeric','stairs_hard','stairs_numeric'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                out = Path(tmp); packet = {'specification_digest':'new','physical_digest':'phys','bundle_digest':'bundle'}
                usage = {'scene':0,'local':0,'sysml':0,'probe':0,'process_seconds':0,'unresolved_processes':0}
                batch = Mock(root=out); batch.usage.return_value = usage
                order = []
                def process(command,cwd,timeout,folder,label):
                    if label == 'damping-comparison':
                        self.assertEqual(timeout,59)
                        d.save(folder/'damping-diagnostic.json',{'classification':
                            'NUMERICAL_UNRESOLVED' if scenario == 'stairs_numeric' else 'FAILURE_REPRODUCED_NUMERICALLY'})
                    return {'returncode':1 if scenario == 'local' else 0,'timed_out':False}
                batch.run.side_effect = process
                def scene(batch,directory,bundle,contract,case,refined,number,manifest,packet,base=None):
                    order.append((case,refined))
                    if case == d.FLAT and refined: self.assertEqual(base.name,d.FLAT+'-base')
                    if case == d.STAIRS: self.assertIsNone(base)
                    folder = directory/(case+('-refined' if refined else '-base')); folder.mkdir()
                    csv = folder/'result.csv'; csv.write_text('data',encoding='utf-8')
                    names = (['energy_failure'] if scenario == 'flat_hard' and case == d.FLAT else
                        ['geometry_failure'] if scenario == 'stairs_hard' and case == d.STAIRS else
                        ['continuous_high_slip'] if case == d.STAIRS else [])
                    status = 'FAIL' if names else 'PASS'
                    d.save(folder/'physical.json',{'status':status,'checks':[{'name':'energy','pass':True}]+
                        [{'name':n,'pass':False} for n in names], 'diagnostics':[],'issues':[],
                        'metrics':{'case_id':case,'csv_sha256':d.sha(csv)}})
                    numeric = 'FAIL' if scenario == 'flat_numeric' and case == d.FLAT and refined else 'PASS'
                    return {'case':case,'variant':'refined' if refined else 'base','status':status,
                            'compile':'PASS','simulation':'COMPLETED','physical':status,'numeric':numeric},folder
                for name, value in (('OUT',out),('approved_inputs',Mock(return_value=(packet,{},{}))),
                    ('round_manifest',Mock(return_value=[])),('Batch',Mock(return_value=batch)),
                    ('guard_sources',Mock()),('dependency_audit',Mock()),
                    ('local_targets',Mock(return_value=['one','two','three','four'])),
                    ('execute_scene',Mock(side_effect=scene)),('_verification_time',Mock(return_value=1.))):
                    stack.enter_context(patch.object(d,name,value))
                stack.enter_context(patch.object(d.execution,'snapshot_inputs'))
                result = d.run_round(1)
                self.assertFalse((out/'.execution.lock').exists())
                with self.assertRaises(ValueError): d.run_round(1)
                if scenario == 'local':
                    self.assertEqual(order,[]); self.assertEqual(len(result['local']),1)
                    self.assertTrue(result['retry_eligible']); self.assertFalse((out/'final-result.json').exists())
                else:
                    self.assertTrue((out/'final-result.json').exists())
                    self.assertEqual(d.read(out/'final-result.json')['unused_budget_disposition'],'RETIRED')
                    self.assertEqual(result['vehicle_acceptance'],'NOT_GRANTED')
                    self.assertFalse(result['retry_eligible'])
                    expected = 4 if scenario in ('complete','stairs_numeric') else 3 if scenario == 'stairs_hard' else 2 if scenario == 'flat_numeric' else 1
                    self.assertEqual(order,list(d.SCENES[:expected]))
                    self.assertEqual(result['status'],'DIAGNOSTIC_COMPLETED_VEHICLE_NOT_ACCEPTED' if scenario == 'complete' else 'STOPPED')
                lock = out/'.execution.lock'; lock.write_text('another owner',encoding='ascii')
                with self.assertRaises(FileExistsError): d.run_round(2)
                self.assertEqual(lock.read_text(encoding='ascii'),'another owner')
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp); d.save(out/'final-result.json',{'closed':True})
            with patch.object(d,'OUT',out), patch.object(d,'inputs') as loader:
                with self.assertRaisesRegex(ValueError,'CLOSED_DAMPING'): d.approved_inputs()
                loader.assert_not_called()


if __name__ == '__main__': unittest.main()
