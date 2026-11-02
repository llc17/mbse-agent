"""Offline Asym2 contract fixtures only: no HTTP/MCP/compiler/simulation."""
from copy import deepcopy
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from week13.src import case_asym2_family as family
from week13.src import case_scope as scope
from week13.src.inherited.model_artifact import validate_artifact
from week13.src.inherited.artifact_sysml import inspect_dual_source


def design(values):
    return dict(summary='TEST_ONLY contract fixture',assumptions=[],parameters=[dict(name=k,value=v,unit='1',origin='model',basis='TEST_ONLY') for k,v in values.items()])


def baseline():
    source=family.effective_source();values=scope.modelica_parameters(source)[0]
    return dict(schema_version='v11-model-artifact-1',backend='model-artifact',artifact=dict(model_name=family.MODEL_NAME,modelica_source=source),sources=[dict(id='src_fixture',text='TEST_ONLY')],requirements=[dict(text='TEST_ONLY',source_id='src_fixture',source_text='TEST_ONLY')],design=design({k:values[k] for k in family.PLAN_PARAMETERS}),libraries=[],settings=dict(stop_time=300,intervals=30000),outputs={k:k for k in family.OUTPUT_NAMES},checks=[])


def plan(base,*,mass=None,height=None,depth=None):
    p={k:scope.modelica_parameters(base['artifact']['modelica_source'])[0][k] for k in family.PLAN_PARAMETERS}
    p.update({k:v for k,v in dict(m=mass,stepH=height,stepD=depth).items() if v is not None})
    return dict(intent='CHANGE',support='SUPPORTED',missing=[],unsupported=[],requirements=base['requirements'],design=design(p),proposed_parameters=p,defaults=['补齐基准和候选SysML'],unknowns=['TEST_ONLY'],rationale='TEST_ONLY中文方案',scope=deepcopy(scope.STAIR_SCOPE_PACKET if height is not None or depth is not None else scope.MASS_SCOPE_PACKET))


def sysml_for(spec):
    values,expressions,kinds=family.sysml_parameters(spec['artifact']['modelica_source'])
    attrs=''.join('attribute '+key+' : '+kinds[key]+' = '+expressions[key]+';\n' for key in values)
    req=''.join('requirement req_'+str(i)+' {'+''.join('attribute '+k+' : String = '+json.dumps(v)+';' for k,v in dict(row,model_name=spec['artifact']['model_name']).items())+'}' for i,row in enumerate(spec['requirements'],1))
    return 'package Asym2Fixture { part def System {'+attrs+'} part system : System;'+req+'}'


class Asym2FamilyTests(unittest.TestCase):
    def test_cached_assessment_rechecks_all_evidence_and_checker_fingerprint(self):
        from week13.src import case_contact_review as review
        review._ASYM2_REVIEW_CACHE.clear()
        spec=baseline();execution=dict(task_status='PASS',mission_assessment=dict(classification='PASS'))
        base=family.ROOT/'runs/synthetic-cache-test'
        with patch('week13.src.inherited.service_adapter._verify_manifest') as full_check,patch('week13.src.case_asym2_execution.verify_run_evidence',return_value=({}, {},dict(status='PASS'))) as runtime_check,patch('week13.src.inherited.model_artifact.summarize_execution',return_value=execution) as compute:
            first,a=review._asym2_checked_execution(spec,{},base,'a'*64)
            first['task_status']='MUTATED_FIXTURE'
            second,b=review._asym2_checked_execution(spec,{},base,'a'*64)
            self.assertEqual(second['task_status'],'PASS');self.assertEqual(compute.call_count,1)
            self.assertIn('MISS',a['status']);self.assertIn('HIT',b['status'])
            self.assertEqual(full_check.call_count,2);self.assertEqual(runtime_check.call_count,2)
            runtime_check.side_effect=ValueError('synthetic runtime dependency hash changed')
            with self.assertRaisesRegex(ValueError,'dependency hash changed'):review._asym2_checked_execution(spec,{},base,'a'*64)
            self.assertEqual(compute.call_count,1)
            runtime_check.side_effect=None
            with patch.object(review,'ASYM2_CHECKER_FILES',()):
                _,changed=review._asym2_checked_execution(spec,{},base,'a'*64)
            self.assertIn('MISS',changed['status']);self.assertEqual(compute.call_count,2)
            full_check.side_effect=ValueError('synthetic CSV hash changed')
            with self.assertRaisesRegex(ValueError,'CSV hash changed'):review._asym2_checked_execution(spec,{},base,'a'*64)
            self.assertEqual(compute.call_count,2)
        review._ASYM2_REVIEW_CACHE.clear()

    def test_simulated_task_field_is_separate_from_acceptance_and_publication(self):
        from week13.src.inherited.artifact_history import _check_outcome_acceptance
        spec=baseline();good=dict(task_status='PASS',vehicle_task_status='PASS',physical_acceptance_status='NOT_VERIFIED',published_version_id=None)
        self.assertEqual(_check_outcome_acceptance(spec,good),(True,'PASS'))
        for task in ('FAIL','UNKNOWN'):
            result=dict(good,task_status=task,vehicle_task_status=task)
            self.assertEqual(_check_outcome_acceptance(spec,result),(True,task))
        for changes in (dict(task_status='FAIL'),dict(vehicle_task_status='ARBITRARY'),dict(physical_acceptance_status='PASS'),dict(published_version_id='v000001')):
            with self.subTest(changes=changes),self.assertRaises(ValueError):_check_outcome_acceptance(spec,dict(good,**changes))
        altered=deepcopy(spec);altered['artifact']['modelica_source']=family._replace(altered['artifact']['modelica_source'],'I_pitch',21)
        with self.assertRaises(ValueError):_check_outcome_acceptance(altered,good)
        legacy=dict(artifact=dict(modelica_source='model Fixture end Fixture;'))
        with self.assertRaises(ValueError):_check_outcome_acceptance(legacy,good)
        self.assertEqual(_check_outcome_acceptance(legacy,dict(good,vehicle_task_status='UNKNOWN')),(False,'UNKNOWN'))

    def test_exact_wrapper_effective_parameters_and_functions(self):
        source=family.effective_source();p,expr,kinds=scope.modelica_parameters(source)
        self.assertTrue(family.is_asym2_source(source))
        self.assertEqual(p['wheel_J'],[.02205,.12852225,.12852225])
        self.assertEqual(p['stepH'],.08);self.assertEqual(p['h19'],.0775)
        self.assertEqual(p['nReach'],2);self.assertEqual(p['nContact'],18)
        self.assertAlmostEqual(p['H_peak'],1.5825);self.assertAlmostEqual(p['x_end_down'],14.68)
        self.assertNotAlmostEqual(p['H_peak'],20*p['stepH'])

    def test_supported_scopes_and_actual_nonuniform_dependencies(self):
        base=baseline()
        for update in ({},{'mass':90},{'mass':105},{'height':.075},{'height':.085},{'depth':.271},{'depth':.270}):
            packet=plan(base,**update)
            evidence=scope.validate_request_plan(packet,base,base['sources'])
            self.assertTrue(evidence['executable'])
            if 'height' in update:self.assertAlmostEqual(evidence['derived_after']['H_peak'],18*update['height']+.0775+.065)
            if 'depth' in update:self.assertAlmostEqual(evidence['derived_after']['x_last_up'],2+17*update['depth']+.22+.08)

    def test_refuse_torque_mixed_scope_and_frozen_tail(self):
        base=baseline();packet=plan(base,mass=90)
        packet['proposed_parameters']['T_main_max']=41
        with self.assertRaises(scope.UnsupportedRequestDesign):scope.validate_request_plan(packet,base,base['sources'])
        packet=plan(base,mass=90,height=.075)
        with self.assertRaises(scope.UnsupportedRequestDesign):scope.validate_request_plan(packet,base,base['sources'])
        for key,value in [('h19',.08),('I_pitch',21),('T_main_max',41),('R_main',.12)]:
            self.assertFalse(family.is_asym2_source(family._replace(base['artifact']['modelica_source'],key,value)))
        self.assertFalse(family.is_asym2_source(base['artifact']['modelica_source'].replace('x_last_up+fixedTopTread','x_start_up+n_steps*stepD')))

    def test_expected_values_are_confirmed_metadata_not_actual(self):
        base=baseline();base['design']=plan(base,mass=105)['design']
        expected=family.expected_parameters(base)
        self.assertEqual(expected['m'],105)
        self.assertEqual(scope.modelica_parameters(base['artifact']['modelica_source'])[0]['m'],85)
        self.assertEqual(expected['T_main_max'],40);self.assertEqual(expected['I_pitch'],20)

    def test_new_family_artifact_and_real_sysml_parse(self):
        spec=baseline();spec['artifact']['sysml_source']=sysml_for(spec)
        check=validate_artifact(spec,require_sysml=True)
        self.assertTrue(check['ready'],check['issues'])
        spec['artifact']['sysml_source']=spec['artifact']['sysml_source'].replace('attribute wheel_J_1 : Real = 0.02205;','')
        self.assertFalse(validate_artifact(spec,require_sysml=True)['ready'])

    def test_new_interval_limit_does_not_relax_old_family(self):
        spec=baseline();spec['artifact']['modelica_source']=family._replace(spec['artifact']['modelica_source'],'h19',.08)
        checked=validate_artifact(spec)
        self.assertFalse(checked['ready'])

    def test_prepared_import_preserves_exact_originals(self):
        root=family.ROOT/'results/updated-model-agent-experiments-20261031-01'
        with tempfile.TemporaryDirectory(prefix='offline-asym2-',dir=root) as parent:
            path=Path(parent)/'bundle';value=family.prepare_input_bundle(path)
            self.assertEqual((path/'originals/source.mo').read_bytes(),family.SOURCE_PATH.read_bytes())
            from week13.src.case_projects import inspect_case
            inspected=inspect_case(path,value['model'])
            self.assertEqual(inspected['support']['family'],family.FAMILY)
            self.assertEqual(inspected['selected_model']['model_name'],family.MODEL_NAME)
            self.assertEqual(inspected['source_facts']['model_parameters']['m'],85)

    def test_sysml_candidate_context_and_baseline_are_separate(self):
        ctx=family.context_contract(family.effective_source(),dict(m=105,stepH=.08,stepD=.28))
        self.assertEqual(next(x['expression'] for x in ctx['sysml_scalar_mirrors'] if x['name']=='m'),'105')
        self.assertEqual(next(x['expression'] for x in ctx['baseline_sysml_scalar_mirrors'] if x['name']=='m'),'85.0')
        self.assertNotIn('stepH',ctx['frozen_tail'])

    def test_bound_current_review_and_console_keep_new_family_pass(self):
        from week13.src import case_contact_review as review
        from week13.src.case_console_result import prepare_result
        spec=baseline();spec['artifact']['sysml_source']=sysml_for(spec)
        identity=dict(project_id='test_asym2_project',task_id='test_asym2_task',version_id='v000001',spec_digest=review._value(spec),source_sha256=review._sha(spec['artifact']['modelica_source'].encode()))
        mission=dict(validity_status='PASS',task_status='PASS',route_status='PASS',diagnostic_status='PASS',assessment_evidence_status='PASS',completion_status=review.COMPLETE,overall_summary='TASK_COMPLETED',classification='PASS',completion_time=98.79,issues=[],stage_report=dict(progress=dict(last_both_main_supported_clearance_step=20)))
        tool=dict(compile=dict(status='PASS'),simulation=dict(status='PASS'),runtime_parameter_readback=dict(status='PASS'))
        outcome=dict(tool_status='PASS',chain_status='PASS',task_status='PASS',tool_result=tool,mission_assessment=mission,completion_status=review.COMPLETE,runtime_parameter_readback=dict(status='PASS'),signal_check_status='PASS')
        root=family.ROOT/'runs'
        with tempfile.TemporaryDirectory(prefix='offline-asym2-review-',dir=root) as parent:
            base=Path(parent)/'projects'/identity['project_id']/'versions'/identity['version_id'];(base/'tool').mkdir(parents=True)
            files={'frozen-model.mo':spec['artifact']['modelica_source'].encode(),'spec.json':json.dumps(spec).encode(),'outcome.json':json.dumps(outcome).encode(),'tool/model_res.csv':b'time,x\n0,0\n300,30\n'}
            for name,raw in files.items():(base/name).write_bytes(raw)
            manifest=dict(schema='v11-artifact-manifest-1',binding={k:v for k,v in identity.items() if k!='version_id'},outcome=outcome,files={name:dict(sha256=review._sha(raw),bytes=len(raw)) for name,raw in files.items()})
            raw=json.dumps(manifest).encode();(base/'artifact-manifest.json').write_bytes(raw)
            snapshot=dict(identity,manifest_sha256=review._sha(raw),record_kind='MODEL_ARTIFACT',state='succeeded',evidence_status='COMPLETE',recorded_conclusions_verified=True,frozen_spec=spec,outcome=outcome,tool_status='PASS',chain_status='PASS',task_status='PASS')
            with patch('week13.src.case_contact_review._asym2_checked_execution',return_value=(outcome,dict(status='MISS',key='a'*64,checker_source_sha256={})) ) as cached:
                current=review.with_review(snapshot,Path(parent))
                cached.return_value=(outcome,dict(status='HIT',key='a'*64,checker_source_sha256={}))
                repeated=review.with_review(snapshot,Path(parent))
                self.assertEqual(current,repeated)
                summary=review.current_summary(current)
                self.assertEqual(summary['current_review_state'],'CURRENT_BOUND_REVIEW')
                self.assertEqual(summary['task_status'],'PASS')
                self.assertEqual(summary['current_contact_review']['rule_version'],review.ASYM2_RULE_VERSION)
                _,execution,_,lines=prepare_result(dict(state=dict(latest_request_execution=current)))
                self.assertEqual(execution['task_status'],'PASS');self.assertIn('任务成功',lines[0])
                self.assertFalse(any('未验证轮转' in line for line in lines))
                (base/'tool/model_res.csv').write_bytes(b'changed')
                invalid=review.review_snapshot(snapshot,Path(parent))
                self.assertEqual(invalid['status'],'UNKNOWN')


if __name__=='__main__':unittest.main()
