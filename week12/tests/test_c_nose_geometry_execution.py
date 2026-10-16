"""Exactly eight local preflight methods. No real parser, compiler or simulation."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from week12.src.integration import nose_geometry_execution as runner
from week12.src.integration.review import check_review_receipt
from week12.src.composition.schema import digest, seal_design


def runtime_fixture():
    packet = runner.read(runner.REVIEW/'review-packet.json')
    fixed,inputs,readonly = runner._fixed()
    packet.update(fixed_implementation_manifest=fixed,fixed_inputs=inputs,readonly_manifest=readonly,
        review_evidence=runner._review_rows(),implementation_manifest=[*fixed,*[
            {'path':str((runner.ROOT/p).resolve()),'sha256':runner.sha(runner.ROOT/p)} for p in runner.NEW_PATHS]])
    return packet


def usage_fixture():
    return {'scene':0,'local':0,'sysml':0,'probe':0,'process_seconds':0,'unresolved_processes':0}


class NoseGeometryExecutionChecks(unittest.TestCase):
    def test_approval_absent_stale_and_closed_execution(self):
        packet = runtime_fixture()
        for bad in ({}, {'origin':'user_message','human_confirmed':True,
                'message_reference':'negative test fixture; never execution approval','user_text':'fixture',
                'scope':runner.SCOPE,'specification_digest':'0'*64}):
            with self.assertRaises(ValueError): check_review_receipt(bad,packet)
        with self.assertRaises(ValueError): runner.initialize('relative.json')
        with patch.object(runner,'inputs',return_value=(packet,{},{})), \
                patch.object(runner,'read',return_value={}),patch.object(runner,'guard_sources') as guard:
            with self.assertRaises(ValueError): runner.initialize(Path(runner.__file__).resolve())
            guard.assert_not_called()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); runner.save(root/'final-result.json',{'status':'closed test fixture'})
            with patch.object(runner,'OUT',root),patch.object(runner,'inputs') as inputs:
                with self.assertRaises(ValueError): runner.approved_inputs()
                inputs.assert_not_called()

    def test_budget_requires_entire_2100_second_four_scene_round(self):
        packet = runtime_fixture(); runner.validate_budget(packet)
        for key,value in (('maximum_scene_attempts',13),('complete_round_reserve_s',2000),
                          ('product_online_calls',1),('maximum_rounds',True)):
            bad = deepcopy(packet); bad['budget'][key] = value
            with self.assertRaises(ValueError): runner.validate_budget(bad)
        batch = object.__new__(runner.Batch); batch.budget = deepcopy(runner.BUDGET)
        fresh = usage_fixture()
        with patch.object(batch,'usage',return_value={**fresh,'process_seconds':4200}): runner.require_complete_round(batch)
        for key,value in (('scene',9),('local',17),('sysml',3),('probe',1),
                          ('process_seconds',4200.001),('unresolved_processes',1)):
            with patch.object(batch,'usage',return_value={**fresh,key:value}):
                with self.assertRaises(ValueError): runner.require_complete_round(batch)
        self.assertEqual(8*10+60+4*(180+180+90),1940)
        self.assertLess(1940,runner.BUDGET['complete_round_reserve_s'])

    def test_source_input_review_guard_and_new_file_only_rebind(self):
        packet = runtime_fixture(); manifest = packet['implementation_manifest']
        runner.guard_sources(packet,manifest)
        self.assertEqual(len(manifest),39)
        for transform in ('old','missing','input','readonly','review'):
            bad,rows = deepcopy(packet),deepcopy(manifest)
            if transform == 'old': rows[0]['sha256'] = '0'*64
            elif transform == 'missing': rows.pop()
            elif transform == 'input': bad['fixed_inputs'][0]['sha256'] = '0'*64
            elif transform == 'readonly': bad['readonly_manifest'][0]['sha256'] = '0'*64
            else: bad['review_evidence'][0]['sha256'] = '0'*64
            with self.assertRaises((ValueError,RuntimeError)): runner.guard_sources(bad,rows)
        result = {'status':'STOPPED','retry_eligible':True,'failure_code':'NEW_GENERATOR_INTERFACE',
            'specification_digest':runner.SPEC,'physical_digest':packet['physical_digest'],
            'proposal_digest':packet['bundle_digest']}
        before,after = deepcopy(manifest),deepcopy(manifest); after[-1]['sha256'] = '0'*64
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); (root/'error.log').write_text('specific local argument mismatch fixture',encoding='utf-8')
            diagnosis = {**result,'classification':'implementation','reason':'new wrapper argument mismatch',
                'same_physical_task_verification':True,'evidence_files':[{'path':'error.log','sha256':runner.sha(root/'error.log')}],
                'rebound_implementation_manifest':after,'changed_paths':[before[-1]['path']]}
            with patch.object(runner,'OUT',root),patch.object(runner,'guard_sources'):
                self.assertEqual(runner.validate_repair(packet,before,result,diagnosis),after)
                with self.assertRaises(ValueError): runner.validate_repair(packet,before,result,
                    {**diagnosis,'rebound_implementation_manifest':before,'changed_paths':[]})
                old = deepcopy(after); old[0]['sha256'] = '0'*64
                with self.assertRaises(ValueError): runner.validate_repair(packet,before,result,
                    {**diagnosis,'rebound_implementation_manifest':old,'changed_paths':sorted([before[0]['path'],before[-1]['path']])})

    def test_schema_mass_inertia_and_ten_frame_connections(self):
        packet,bundle,contract = runner.reviewed_inputs()
        actual = runner.validate_candidate(bundle,contract,packet)
        self.assertEqual(actual['mass_kg'],85)
        self.assertEqual(actual['frame_connections'],10)
        self.assertAlmostEqual(actual['locked_inertia_kg_m2'],8.256807571108505,places=12)
        self.assertAlmostEqual(actual['COM_xz_m'][1],.25576470588235295,places=12)
        bad = deepcopy(bundle)
        bad['design']['bodies'][0]['mass']['value'] += 1
        bad['design'] = seal_design(bad['design'])
        changed = {**packet,'bundle_digest':digest(bad),'design_digest':bad['design']['design_digest']}
        with patch.object(runner,'require_valid'):
            with self.assertRaisesRegex(ValueError,'MASS_COM_INERTIA'): runner.validate_candidate(bad,contract,changed)
        bad = deepcopy(bundle)
        bad['design']['joints'][0]['ports']['a']['pose']['x']['value'] += .01
        bad['design'] = seal_design(bad['design'])
        changed = {**packet,'bundle_digest':digest(bad),'design_digest':bad['design']['design_digest']}
        with patch.object(runner,'require_valid'):
            with self.assertRaisesRegex(ValueError,'FRAME_CLOSURE'): runner.validate_candidate(bad,contract,changed)
        self.assertEqual(min(.02,.15*next(w for w in bundle['design']['wheels'] if w['id']=='nose')['radius']['value']),.015)

    def test_generation_matches_frozen_exact_bytes_and_nested_globals(self):
        packet,bundle,contract = runner.reviewed_inputs()
        original = runner.dimension_revision.original_generator
        old_spec = original.SPECIFICATION_DIGEST
        for case in runner.CASE_ORDER:
            generated = runner.generate(bundle,contract,case,runner.SPEC)
            preview = runner.REVIEW/(case+'-preview')
            self.assertEqual(generated['modelica'].encode('utf-8'),(preview/'V11CGenerated.mo').read_bytes())
            self.assertEqual(generated['sysml'].encode('utf-8'),(preview/'vehicle.sysml').read_bytes())
            self.assertEqual(generated['trace']['execution_binding']['specification_digest'],runner.SPEC)
            self.assertEqual(runner.sha(preview/'V11ContactC2.mo'),runner.vehicle.C2_SHA)
        self.assertEqual(original.SPECIFICATION_DIGEST,old_spec)
        with self.assertRaises(ValueError): runner.generate(bundle,contract,'fixed_nose_three_down',runner.SPEC)
        corrupted = deepcopy(generated); corrupted['modelica'] += '\nchanged model'
        with patch.object(runner.dimension_revision,'_isolated_generator',return_value=lambda *_:corrupted):
            with self.assertRaisesRegex(ValueError,'GENERATION_MISMATCH'):
                runner.generate(bundle,contract,runner.CASE_ORDER[1],runner.SPEC)

    def test_actual_solver_xml_and_argv_and_shared_postprocessing(self):
        for refined in (False,True):
            source = runner.build_script('V11CGenerated.Example',20.,'x',refined)
            self.assertIn(f'tolerance={1e-8:.17g}',source)
            self.assertIn('numberOfIntervals=20000',source)
            self.assertIn('method="dassl"',source)
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                xml = '<root><DefaultExperiment startTime="0" stopTime="20" stepSize="0.001" tolerance="1e-8" solver="dassl" outputFormat="csv"/></root>'
                (root/'model_init.xml').write_text(xml,encoding='utf-8')
                step = runner.NUMERICS['refined' if refined else 'base']['max_step_s']
                command = ['model.exe','-s=dassl',f'-maxStepSize={step}','-r=result.csv']
                actual = runner.check_actual_numerics(root,command,{'stop_time_s':20},refined)
                self.assertEqual(actual['launch_command'],[*command,'-lv=LOG_STATS'])
                with self.assertRaises(ValueError): runner.check_actual_numerics(root,
                    ['model.exe','-s=dassl','-maxStepSize=0.001','-r=result.csv'],{'stop_time_s':20},refined)
                (root/'model_init.xml').write_text(xml.replace('1e-8','1e-6'),encoding='utf-8')
                with self.assertRaises(ValueError): runner.check_actual_numerics(root,command,{'stop_time_s':20},refined)
        # Existing verifier and comparator share one bounded child process;
        # there is no second unmetered or separately timed comparison launch.
        source = Path(runner.previous.__file__).read_text(encoding='utf-8')
        self.assertIn("if base is not None: command += ['--base'",source)
        self.assertEqual(source.count("folder, 'verification')"),1)
        self.assertEqual(runner.BUDGET['postprocess_timeout_s'],90)

    def test_physical_numeric_unknown_terminal_and_proven_syntax_only_repair(self):
        for row in ({'status':'FAIL','physical':'FAIL','numeric':'UNVERIFIED'},
                    {'status':'FAIL','physical':'PASS','numeric':'FAIL'},
                    {'status':'FAIL','compile':'PASS','simulation':'FAILED_OR_TIMED_OUT'},
                    {'status':'PASS','physical':'PASS','variant':'refined','numeric':'UNVERIFIED'}):
            with self.assertRaises(runner.TerminalFailure): runner.classify_scene(row,None)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); folder = root/'scene'; folder.mkdir()
            (folder/'build.stdout.log').write_text('V11CGenerated.mo syntax error',encoding='utf-8')
            runner.save(root/'process-001.start.json',{'label':'build','cwd':str(folder)})
            syntax = {'status':'FAIL','compile':'NOT_RUN','error':'RuntimeError: BUILD_TOOL_FAILURE'}
            with patch.object(runner,'OUT',root):
                with self.assertRaises(runner.TerminalFailure): runner.classify_scene(syntax,folder)
            runner.save(root/'process-001.result.json',{'label':'build','returncode':1,
                'timed_out':False,'interrupted':False,'uncertain':False})
            with patch.object(runner,'OUT',root):
                with self.assertRaises(runner.ImplementationFailure): runner.classify_scene(syntax,folder)

    def test_interrupted_ledger_blocks_resume_preserves_and_retires_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); runner.save(root/'policy.json',{'budget':deepcopy(runner.BUDGET)})
            runner.save(root/'process-001.start.json',{'label':'simulation','command':['fixture'],'cwd':str(root)})
            batch = runner.Batch(root,runner.SPEC)
            self.assertEqual(batch.usage()['unresolved_processes'],1)
            with self.assertRaises(ValueError): runner.require_complete_round(batch)
            with self.assertRaises(RuntimeError): batch.reserve('scene','never launched',2)
            self.assertTrue((root/'process-001.start.json').is_file())
        class FakeBatch:
            def __init__(self,*_): self.budget = deepcopy(runner.BUDGET); self.counts = usage_fixture()
            def usage(self): return dict(self.counts)
            def reserve(self,kind,*_): self.counts[kind] += 1; return {'kind':kind}
            def run(self,*_): raise KeyboardInterrupt('mock interruption; no actual child')
        packet = runtime_fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(runner,'OUT',root),patch.object(runner,'approved_inputs',return_value=(packet,{},{})), \
                    patch.object(runner,'round_manifest',return_value=[]),patch.object(runner,'Batch',FakeBatch), \
                    patch.object(runner,'guard_sources'),patch.object(runner.previous,'snapshot_inputs'), \
                    patch.object(runner,'snapshot_readonly'),patch.object(runner,'dependency_audit'), \
                    patch.object(runner,'local_targets',return_value=['fixture']*8):
                result = runner.run_round(1)
            self.assertEqual(result['status'],'STOPPED')
            self.assertFalse(result['retry_eligible'])
            self.assertEqual(result['usage']['local'],1)
            final = runner.read(root/'final-result.json')
            self.assertEqual(final['unused_budget_disposition'],'RETIRED')
            self.assertFalse(final['automatic_additional_scene_permitted'])
            self.assertEqual(len(final['not_run']),4)
            self.assertFalse((root/'.execution.lock').exists())


if __name__ == '__main__': unittest.main()
