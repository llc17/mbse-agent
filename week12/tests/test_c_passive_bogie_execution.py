"""Twelve bounded preflight methods; no compiler, parser or simulation calls."""
from copy import deepcopy
import csv
import math
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from week12.src.integration import passive_bogie_execution as runner
from week12.src.integration import passive_bogie_verify as verify


def fixture(): return runner.runtime_packet()


def usage():
    return {'scene':0,'local':0,'sysml':0,'probe':0,'process_seconds':0.,'unresolved_processes':0}


def analytical_probe(path,defect=None):
    frequency = math.sqrt(320); crossing = math.pi/(2*frequency)
    times = sorted([i*.0001 for i in range(2001)]+[crossing,crossing])
    with Path(path).open('w',encoding='utf-8',newline='') as stream:
        writer = csv.DictWriter(stream,fieldnames=runner.PROBE_OUTPUTS); writer.writeheader()
        for index,t in enumerate(times):
            q = .1*math.cos(frequency*t) if t<=crossing else -.1*frequency*(t-crossing)
            w = -.1*frequency*math.sin(frequency*t) if t<=crossing else -.1*frequency
            row = {'time':t,'q':q,'w':w,'alpha':-320*max(q,0),'springTorque':320*max(q,0),
                'elasticEnergy':160*max(q,0)**2,'totalEnergy':.5*w*w+160*max(q,0)**2,
                'negativeTorque':0.,'zeroTorque':0.,'positiveTorque':32.}
            if defect: defect(index,row)
            writer.writerow(row)


class PassiveBogiePreflightChecks(unittest.TestCase):
    def test_01_exact_receipt_and_fresh_output_closed_guard(self):
        packet,_,_ = fixture()
        receipt = runner.read(runner.ROOT/'results/s3-c-passive-bogie-authorization-20261006.json')
        runner.check_receipt(receipt,packet)
        for key in ('scope','specification_digest','source_review_packet_sha256','verification_contract_sha256'):
            bad = deepcopy(receipt); bad[key] = 'stale fixture'
            with self.assertRaises(ValueError): runner.check_receipt(bad,packet)
        with self.assertRaises(ValueError): runner.check_receipt({},packet)
        with self.assertRaises(ValueError): runner.initialize('relative.json')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); runner.save(root/'final-result.json',{'status':'closed fixture'})
            with patch.object(runner,'OUT',root),patch.object(runner,'inputs') as inputs:
                with self.assertRaises(ValueError): runner.approved_inputs()
                inputs.assert_not_called()
        with patch.object(runner,'OUT',runner.REVIEW),patch.object(runner,'inputs',return_value=(packet,{},{})), \
                patch.object(runner,'guard_sources'):
            with self.assertRaises(FileExistsError): runner.initialize((runner.ROOT/'results/s3-c-passive-bogie-authorization-20261006.json').resolve())

    def test_02_fixed_sources_inputs_contact_and_18_probe_hashes(self):
        packet,_,contract = fixture(); manifest = packet['implementation_manifest']
        runner.guard_sources(packet,manifest)
        self.assertEqual(len(manifest),41)
        self.assertEqual(sum(len(p['files']) for p in contract['reused_probe_evidence']),18)
        for mutation in ('source','input','readonly','preview','missing'):
            bad,rows = deepcopy(packet),deepcopy(manifest)
            if mutation == 'source': rows[0]['sha256'] = '0'*64
            elif mutation == 'input': bad['fixed_inputs'][0]['sha256'] = '0'*64
            elif mutation == 'readonly': bad['readonly_manifest'][0]['sha256'] = '0'*64
            elif mutation == 'preview': bad['review_evidence'][0]['sha256'] = '0'*64
            else: rows.pop()
            with self.assertRaises((ValueError,RuntimeError)): runner.guard_sources(bad,rows)
        preview = runner.REVIEW/'passive_bogie_flat_brake-preview'
        self.assertEqual(runner.sha(preview/'V11ContactC2.mo'),runner.vehicle.C2_SHA)
        self.assertEqual((preview/'V11PhysicalB1.mo').read_text(encoding='utf-8'),
                         runner.vehicle.isolated_b().replace('\r\n','\n'))

    def test_03_schema_passive_topology_and_closed_frames(self):
        packet,bundle,contract = fixture(); actual = runner.validate_candidate(bundle,contract,packet)
        self.assertGreater(actual['frame_connections'],10)
        joints = [j for j in bundle['design']['joints'] if j['kind'] == 'passive_revolute']
        self.assertEqual([j['id'] for j in joints],['bogie_hinge'])
        bad = deepcopy(bundle); bad['design']['joints'][0]['ports']['a']['pose']['x']['value'] += .01
        with patch.object(runner,'digest',return_value=runner.SPEC),patch.object(runner,'require_valid'), \
                patch.object(runner,'validate_budget'):
            changed = {**packet,'verification_digest':runner.SPEC}
            with self.assertRaisesRegex(ValueError,'FRAME_CLOSURE'): runner.validate_candidate(bad,contract,changed)

    def test_04_exact_mass_com_and_separate_inertia(self):
        packet,bundle,contract = fixture(); actual = runner.validate_candidate(bundle,contract,packet)
        self.assertEqual(actual['mass_kg'],85)
        self.assertAlmostEqual(actual['COM_xz_m'][0],.43481361616147285,places=12)
        self.assertAlmostEqual(actual['COM_xz_m'][1],.254,places=12)
        self.assertAlmostEqual(actual['locked_inertia_kg_m2'],5.9513054693682985,places=12)
        for key in ('mass','inertia'):
            bad = deepcopy(bundle); bad['design']['bodies'][0][key]['value'] += 1
            with patch.object(runner,'digest',return_value=runner.SPEC),patch.object(runner,'require_valid'):
                with self.assertRaisesRegex(ValueError,'MASS_COM_SEPARATE_INERTIA'):
                    runner.validate_candidate(bad,contract,{**packet,'verification_digest':runner.SPEC})

    def test_05_real_revolute_spring_sign_and_original_bytes(self):
        _,bundle,contract = fixture()
        for case in runner.CASE_ORDER:
            generated = runner.generate(bundle,contract,case,runner.SPEC)
            preview = runner.REVIEW/(generated['trace']['execution_binding']['preview_case']+'-preview')
            self.assertEqual(generated['modelica'].encode('utf-8'),(preview/'V11CGenerated.mo').read_bytes())
            self.assertEqual(generated['sysml'].encode('utf-8'),(preview/'vehicle.sysml').read_bytes())
            self.assertEqual(generated['modelica'].count('MB.Joints.Revolute bogieJoint('),1)
            self.assertIn('connect(bogieSpring.flange_b,bogieJoint.axis)',generated['modelica'].replace(' ',''))
            self.assertIn('connect(bogieSpring.flange_a,bogieJoint.support)',generated['modelica'].replace(' ',''))
            spring = (preview/'V11PassiveBogieDraft.mo').read_text(encoding='utf-8')
            self.assertIn('tau = c*compression;',spring)
            self.assertIn('compression = max(phi_rel, 0);',spring)
            self.assertNotIn('initial equation',spring)
            self.assertNotIn('ElastoBacklash',spring)

    def test_06_probe_analytical_oracle_initialization_and_branch_defects(self):
        _,bundle,contract = fixture()
        runtime = {'status':'PASS_BEFORE_SIMULATION','numerics':verify.expected_numerics(contract,runner.CASE_ORDER[0],'base')}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'probe.csv'; analytical_probe(path)
            result = verify.verify(bundle,contract,runner.CASE_ORDER[0],path,variant='base',runtime_evidence=runtime)
            self.assertEqual(result['status'],'PASS')
            for defect in (lambda i,r:r.update(alpha=32.) if i==0 else None,
                           lambda i,r:r.update(negativeTorque=-32.),lambda i,r:r.update(zeroTorque=-106.667)):
                analytical_probe(path,defect)
                result = verify.verify(bundle,contract,runner.CASE_ORDER[0],path,variant='base',runtime_evidence=runtime)
                self.assertEqual(result['status'],'FAIL')
            analytical_probe(path)
            absent = verify.verify(bundle,contract,runner.CASE_ORDER[0],path,variant='base',runtime_evidence=None)
            self.assertNotEqual(absent['status'],'PASS')

    def test_07_filter_every_body_and_actual_wheel_carrier(self):
        _,bundle,contract = fixture()
        for case in runner.CASE_ORDER:
            generated = runner.generate(bundle,contract,case,runner.SPEC)
            expected = verify.required_columns(bundle,case)
            observed = set(generated['variable_filter'][2:-2].split('|'))
            self.assertEqual(observed,expected)
            self.assertIsNone(re.fullmatch(generated['variable_filter'],'inventedPassFlag'))
        self.assertEqual(verify.CARRIERS,{'rear':'chassis','front':'bogie','nose':'bogie'})
        data = verify.old._Data({'body_chassis_pitch':(0.,.01),'body_bogie_pitch':(.1,.2),
            'body_chassis_pitchRate':(.2,.3),'body_bogie_pitchRate':(.5,.9)},2)
        q,w,energy,torque = verify._joint_series(data)
        self.assertEqual(q,(.1,.19)); self.assertAlmostEqual(w[1],.6)
        self.assertAlmostEqual(energy[1],160*.19**2); self.assertAlmostEqual(torque[1],-320*.19)

    def test_08_internal_spring_energy_added_once_no_external_work(self):
        _,bundle,_ = fixture()
        data = verify.old._Data({'body_chassis_pitch':(0.,0.),'body_bogie_pitch':(.1,.2),
            'body_chassis_pitchRate':(0.,0.),'body_bogie_pitchRate':(0.,0.)},2)
        original = {'mechanical_scale_J':123.,'shaft_work_J':7.}
        with patch.object(verify.old,'_vehicle_energy',return_value=(dict(original),[0.,-4.8])):
            energy,residual = verify._vehicle_energy(bundle,data)
        self.assertAlmostEqual(residual[1],0.,places=12)
        self.assertEqual(energy['shaft_work_J'],7.)
        self.assertEqual(energy['mechanical_scale_J'],123.)
        self.assertAlmostEqual(energy['spring_change_J'],4.8)

    def test_09_own_envelope_and_original_physical_task_thresholds(self):
        _,bundle,contract = fixture()
        parent = runner.read(runner.ROOT/contract['inheritance']['path'])
        for name in ('physical','numerical','numerical_comparison','reused_probe_evidence'):
            self.assertEqual(contract[name],parent[name])
        self.assertEqual(contract['articulated_checking']['joint_domain_rad'],[-.65,.35])
        self.assertEqual(bundle['design']['requirements']['descent_mode'],'forward_front_wheels_first')
        self.assertEqual([case['id'] for case in bundle['physical']['cases']],list(runner.CASE_ORDER[1:]))
        self.assertIs(verify.old._geometry,verify._vehicle_case.__globals__['old']._geometry)
        self.assertEqual(verify._vehicle_sanity.__globals__['_vehicle_energy'],verify._vehicle_energy)
        self.assertNotEqual(verify._vehicle_sanity.__globals__,vars(verify.old))

    def test_10_new_case_order_gates_and_real_vehicle_sysml(self):
        packet,bundle,contract = fixture(); attempted = []
        class FakeBatch:
            def __init__(self,*_): self.budget = deepcopy(runner.BUDGET); self.counts = usage()
            def usage(self): return dict(self.counts)
            def reserve(self,kind,*_): self.counts[kind] += 1; return {'kind':kind}
            def run(self,command,cwd,timeout,log,label):
                if label == 'sysml':
                    runner.save(Path(log)/'sysml-result.json',{'status':'PASS','tool_version':'0.34.1'})
                return {'returncode':0,'timed_out':False}
        def scene(batch,directory,b,c,case,refined,*args):
            attempted.append((case,refined)); batch.reserve('scene',case,1)
            return {'case':case,'variant':'refined' if refined else 'base','status':'FAIL',
                    'physical':'PASS' if not refined else 'FAIL','numeric':'UNVERIFIED'},None
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(runner,'OUT',root),patch.object(runner,'approved_inputs',return_value=(packet,bundle,contract)), \
                patch.object(runner,'round_manifest',return_value=[]),patch.object(runner,'Batch',FakeBatch), \
                patch.object(runner,'guard_sources'),patch.object(runner.previous,'snapshot_inputs'), \
                patch.object(runner,'snapshot_readonly'),patch.object(runner,'dependency_audit'), \
                patch.object(runner,'local_targets',return_value=['fixture']*12),patch.object(runner,'execute_scene',side_effect=scene):
                result = runner.run_round(1)
            # Base failure prevents even refined; no later vehicle may launch.
            self.assertEqual(attempted,[(runner.CASE_ORDER[0],False)])
            self.assertFalse(result['retry_eligible'])
            generated = runner.generate(bundle,contract,runner.CASE_ORDER[0],runner.SPEC)
            self.assertEqual((root/'round-01/vehicle.sysml').read_bytes(),generated['sysml'].encode('utf-8'))
            self.assertTrue((root/'final-result.json').is_file())
            self.assertFalse((root/'.execution.lock').exists())

    def test_11_durable_budget_full_round_repair_and_unresolved_guard(self):
        packet,_,_ = fixture(); runner.validate_budget(packet)
        for key,value in (('maximum_scene_attempts',19),('maximum_rounds',True),('product_online_calls',1),
                          ('complete_round_reserve_s',2999),('new_local_execution_checks_limit',37)):
            bad = deepcopy(packet); bad['budget'][key] = value
            with self.assertRaises(ValueError): runner.validate_budget(bad)
        batch = object.__new__(runner.Batch); batch.budget = deepcopy(runner.BUDGET)
        with patch.object(batch,'usage',return_value={**usage(),'process_seconds':6000.}): runner.require_complete_round(batch)
        for key,value in (('scene',13),('local',25),('sysml',3),('probe',1),('process_seconds',6000.001),('unresolved_processes',1)):
            with patch.object(batch,'usage',return_value={**usage(),key:value}):
                with self.assertRaises(ValueError): runner.require_complete_round(batch)
        self.assertEqual(6*(180+180+90)+12*15+60,2940)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); runner.save(root/'policy.json',{'budget':deepcopy(runner.BUDGET)})
            runner.save(root/'process-001.start.json',{'label':'simulation','command':['fixture'],'cwd':str(root)})
            batch = runner.Batch(root,runner.SPEC)
            self.assertEqual(batch.usage()['unresolved_processes'],1)
            with self.assertRaises(RuntimeError): batch.reserve('scene','unlaunched',2)
            self.assertTrue((root/'process-001.start.json').is_file())
        manifest = packet['implementation_manifest']; changed = deepcopy(manifest); changed[-1]['sha256'] = '0'*64
        result = {'status':'STOPPED','retry_eligible':True,'failure_code':'NEW_POSTPROCESS_INTERFACE',
            'specification_digest':runner.SPEC,'physical_digest':packet['physical_digest'],'proposal_digest':runner.SPEC}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); (root/'error.log').write_text('local traceback fixture',encoding='utf-8')
            diagnosis = {**result,'classification':'implementation','same_physical_task_verification':True,'reason':'specific checker argument mismatch',
                'rebound_implementation_manifest':changed,'changed_paths':[manifest[-1]['path']],
                'evidence_files':[{'path':'error.log','sha256':runner.sha(root/'error.log')}]}
            with patch.object(runner,'OUT',root),patch.object(runner,'guard_sources'):
                self.assertEqual(runner.validate_repair(packet,manifest,result,diagnosis),changed)
                with self.assertRaises(ValueError): runner.validate_repair(packet,manifest,result,
                    {**diagnosis,'rebound_implementation_manifest':manifest,'changed_paths':[]})
                old = deepcopy(changed); old[0]['sha256'] = '0'*64
                with self.assertRaises(ValueError): runner.validate_repair(packet,manifest,result,
                    {**diagnosis,'rebound_implementation_manifest':old,'changed_paths':sorted([manifest[0]['path'],manifest[-1]['path']])})

    def test_12_xml_argv_event_rows_and_proven_checker_failure_only(self):
        _,bundle,contract = fixture()
        for case_id in runner.CASE_ORDER:
            case = runner.case_definition(bundle,contract,case_id)
            for refined in (False,True):
                gen = runner.generate(bundle,contract,case_id,runner.SPEC)
                script = runner.build_script(gen['model_class'],case['stop_time_s'],gen['variable_filter'],refined)
                interval = .0001 if case_id == runner.CASE_ORDER[0] else .001
                self.assertIn('loadFile("V11PassiveBogieDraft.mo")',script)
                self.assertIn('numberOfIntervals='+str(round(case['stop_time_s']/interval)),script)
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary); xml = f'<root><DefaultExperiment startTime="0" stopTime="{case["stop_time_s"]}" stepSize="{interval}" tolerance="1e-8" solver="dassl" outputFormat="csv"/></root>'
                    (root/'model_init.xml').write_text(xml,encoding='utf-8')
                    command = ['model.exe','-s=dassl',f'-maxStepSize={.00025 if refined else .0005}','-r=result.csv']
                    actual = runner.check_actual_numerics(root,command,case,refined)
                    self.assertTrue(actual['numerics']['keep_event_rows'])
                    self.assertEqual(actual['launch_command'],[*command,'-lv=LOG_STATS'])
                    with self.assertRaises(ValueError): runner.check_actual_numerics(root,[*command,'-noEventEmit'],case,refined)
                    (root/'model_init.xml').write_text(xml.replace('1e-8','1e-6'),encoding='utf-8')
                    with self.assertRaises(ValueError): runner.check_actual_numerics(root,command,case,refined)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); folder = root/'scene'; folder.mkdir()
            (folder/'verification.stderr.log').write_text('Traceback (most recent call last):\n  File "'+str(runner.ROOT/runner.NEW_PATHS[1])+'", line 1\nKeyError: missing_adapter_argument\n',encoding='utf-8')
            command = ['python','--verify-scene',str(folder)]
            runner.save(root/'process-001.start.json',{'label':'verification','cwd':str(runner.ROOT.parent),'command':command})
            row = {'status':'FAIL','physical':'UNVERIFIED','numeric':'UNVERIFIED',
                   'error':'RuntimeError: PHYSICAL_NUMERIC_OR_POSTPROCESS_FAILURE'}
            with patch.object(runner,'OUT',root):
                with self.assertRaises(runner.TerminalFailure): runner.classify_scene(row,folder)
            runner.save(root/'process-001.result.json',{'label':'verification','returncode':1,'timed_out':False,'interrupted':False,'uncertain':False})
            with patch.object(runner,'OUT',root):
                with self.assertRaises(runner.ImplementationFailure): runner.classify_scene(row,folder)
                for failure in ('physical','numeric'):
                    with self.assertRaises(runner.TerminalFailure): runner.classify_scene({**row,failure:'FAIL'},folder)
            # A clean process in another scene cannot prove this scene's adapter error.
            with patch.object(runner,'OUT',root),patch.object(runner,'read',side_effect=lambda p:
                {'label':'verification','cwd':str(runner.ROOT.parent),'command':['python','--verify-scene',str(root/'other')]} if str(p).endswith('start.json') else
                {'returncode':1,'timed_out':False,'interrupted':False,'uncertain':False}):
                with self.assertRaises(runner.TerminalFailure): runner.classify_scene(row,folder)


if __name__ == '__main__': unittest.main()
