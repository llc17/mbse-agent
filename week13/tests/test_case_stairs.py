"""Isolated stair-scope fixtures; no product HTTP/MCP/compiler/dynamics.

Synthetic packets, SysML and CSV below exercise contracts only. They are never
installed as a product design or evidence of a real vehicle passing a mission.
"""
import copy
import csv
import json
import math
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from week13.src import case_scope as scope
from week13.src.case_checks import REQUIRED,assess,assess_rows,terrain_binding
from week13.src.case_taskflow import TaskFlow,PLAN_TOOL,PLAN_PROMPT,GEN_PROMPT
from week13.src.case_taskflow import imported_obligations,validate_import_plan
from week13.src.case_toolchain import parameter_readback,_simulation_command,_record_parameter_output
from week13.src.inherited import artifact_baseline
from week13.src.case_backend import module

ROOT=Path(__file__).resolve().parents[1]
TEMP_ROOT=ROOT/'results'/'stairs-extension-20261021-01'
ORIGINAL=ROOT/'src/inherited/reference-full-mission-legacy.mo'

def design(parameters):
    return dict(summary='TEST_ONLY injected product contract fixture',assumptions=[dict(text='TEST_ONLY no real product request or dynamics',origin='model')],parameters=[dict(name=k,value=v,unit='kg' if k=='m' else 'm' if k in scope.STAIR_PARAMETERS else '1',origin='model',basis='TEST_ONLY finite contract') for k,v in parameters.items()])

def sysml_for(spec):
    values,expressions,kinds=scope.modelica_parameters(spec['artifact']['modelica_source'])
    attrs=''.join('attribute '+name+' : '+('Integer' if kinds[name]=='Integer' else 'Real')+' = '+expressions[name]+';\n' for name in values)
    reqs=''
    for index,row in enumerate(spec['requirements'],1):
        attrs_req=''.join('attribute '+key+' : String = '+json.dumps(value,ensure_ascii=False)+';' for key,value in dict(row,model_name=spec['artifact']['model_name']).items())
        reqs+='requirement req_'+str(index)+' {'+attrs_req+'}\n'
    return 'package SyntheticStairFixture { part def System {\n'+attrs+'} part system : System;\n'+reqs+'}'

def baseline():
    # Read only the user-authorized source; no source files are changed.
    source=ORIGINAL.read_text(encoding='utf8')
    old=dict(id='src_history',text='TEST_ONLY baseline source context')
    req=dict(text=old['text'],source_id=old['id'],source_text=old['text'])
    values=scope.modelica_parameters(source)[0]
    return dict(schema_version='v11-model-artifact-1',backend='model-artifact',artifact=dict(model_name='ZLTECH_Full_Mission',modelica_source=source),sources=[old],requirements=[req],design=design({k:values[k] for k in scope.REQUEST_PARAMETERS}),libraries=['Modelica'],settings=dict(stop_time=120,intervals=2400),outputs=dict(x='x'),checks=[])

def packet(base,height=.18,depth=.26,mass_scope=False):
    values=scope.modelica_parameters(base['artifact']['modelica_source'])[0]
    parameters={k:values[k] for k in scope.REQUEST_PARAMETERS}
    if mass_scope:parameters['m']+=5
    else:parameters.update(stepH=height,stepD=depth)
    return dict(intent='CHANGE',support='SUPPORTED',missing=[],unsupported=[],requirements=[dict(text='TEST_ONLY injected request',source_id='src_new',source_text='TEST_ONLY injected request')],design=design(parameters),proposed_parameters=parameters,defaults=['TEST_ONLY inherit selected baseline, DASSL and observation; complete baseline and candidate SysML'],unknowns=['No real execution evidence'],rationale='TEST_ONLY finite scope fixture',scope=copy.deepcopy(scope.MASS_SCOPE_PACKET if mass_scope else scope.STAIR_SCOPE_PACKET))

def source_with(source,**changes):
    import re
    for name,value in changes.items():
        source,count=re.subn(r'(\bparameter\s+(?:Real|Integer)\s+'+name+r'\s*=\s*)([^;]+)(;)',lambda m:m[1]+str(value)+m[3],source)
        if count!=1:raise AssertionError('fixture replacement count '+name)
    return source

def context(base,plan):
    ref=dict(project_id='fixture_project',task_id='fixture_reference',version_id='v000000',spec_digest='a'*64,source_sha256='b'*64,manifest_sha256='c'*64)
    return dict(request_id='fixture_stairs',request_digest='fixture_request_digest',project_id=ref['project_id'],baseline_ref=ref,baseline_snapshot=dict(frozen_spec=base),plan=plan,sources=[dict(id='src_new',text='TEST_ONLY injected request'),*base['sources']],displayed_digest='fixture_display_digest',root=str(TEMP_ROOT))

def candidate(base,ctx):
    spec=copy.deepcopy(base);p=ctx['plan']['proposed_parameters'];names=scope.STAIR_PARAMETERS if ctx['plan']['scope']==scope.STAIR_SCOPE_PACKET else ('m','T_main_max','T_nose_max')
    spec['artifact']['modelica_source']=source_with(base['artifact']['modelica_source'],**{k:p[k] for k in names})
    spec.update(baseline_ref=ctx['baseline_ref'],sources=ctx['sources'],requirements=ctx['plan']['requirements'],design=ctx['plan']['design'])
    spec['artifact']['sysml_source']=sysml_for(spec)
    return spec

def delta(spec,ctx):
    service=SimpleNamespace(_current_project=lambda:dict(project_id=ctx['project_id']))
    with patch.object(artifact_baseline,'selection',return_value=ctx['baseline_ref']),patch.object(artifact_baseline,'resolve',return_value=ctx['baseline_snapshot']):
        return scope.check_request_delta(service,spec,ctx)

def trajectory(values,end=None):
    """Synthetic arithmetic route with the source's distinct floor conventions."""
    end=values['x_end_down']+1.5 if end is None else end
    rows=[]
    for i in range(int(end/.02)+1):
        x=i*.02;row={k:0. for k in REQUIRED}
        row.update(time=i*.05,x=x,z=.127,v=.4,psi=-.1 if values['x_start_down']<x<values['x_end_down'] else 0.,x_n=x+.54,x_f=x+.22,x_r=x-.23,z_n=.07,z_f=.127,z_r=.127)
        for entity in ('n','f','r'):
            pos=row['x_'+entity];offset=1 if entity=='n' else 0;epsilon=0 if entity=='n' else 1e-3
            if pos<values['x_start_up']:height=0.
            elif pos<values['x_end_up']:height=(math.floor((pos-values['x_start_up'])/values['stepD']+epsilon)+offset)*values['stepH']
            elif pos<values['x_start_down']:height=values['H_peak']
            elif pos<values['x_end_down']:
                height=values['H_peak']-(math.floor((pos-values['x_start_down'])/values['stepD']+epsilon)+offset)*values['stepH']
                if entity!='n':height=max(0.,height)
            else:height=0.
            row['h_'+entity]=height
        rows.append(row)
    return rows


class StairScopeContracts(unittest.TestCase):
    def setUp(self):self.base=baseline();self.before=scope.modelica_parameters(self.base['artifact']['modelica_source'])[0]
    def validate(self,p):return scope.validate_request_plan(p,self.base,context(self.base,p)['sources'])
    def test_height_only_depth_only_and_both_inherit_other_dimension(self):
        for height,depth in ((.18,self.before['stepD']),(self.before['stepH'],.26),(.18,.26)):
            with self.subTest(height=height,depth=depth):
                p=packet(self.base,height,depth);proof=self.validate(p);ctx=context(self.base,p);actual=delta(candidate(self.base,ctx),ctx)
                self.assertEqual(proof['direct_parameters'],list(scope.STAIR_PARAMETERS));self.assertEqual(actual['parameters']['stepH'],height);self.assertEqual(actual['parameters']['stepD'],depth)
                self.assertAlmostEqual(actual['parameters']['H_peak'],20*height);self.assertAlmostEqual(actual['parameters']['x_end_down'],4+40*depth)
                self.assertEqual(actual['parameters']['m'],85)
    def test_missing_inherited_dimension_is_rejected(self):
        p=packet(self.base);del p['proposed_parameters']['stepD']
        with self.assertRaisesRegex(ValueError,'complete finite'):self.validate(p)
    def test_zero_negative_nonfinite_and_bool_rejected(self):
        for name in scope.STAIR_PARAMETERS:
            for value in (0,-.01,float('nan'),float('inf'),-float('inf'),True):
                with self.subTest(name=name,value=value),self.assertRaises(ValueError):
                    p=packet(self.base);p['proposed_parameters'][name]=value;self.validate(p)
    def test_mass_inertia_torques_and_extra_parameters_cannot_piggyback(self):
        for name in scope.REQUEST_PARAMETERS:
            with self.subTest(name=name),self.assertRaisesRegex(scope.UnsupportedRequestDesign,'freeze'):
                p=packet(self.base);p['proposed_parameters'][name]+=1;self.validate(p)
        for name in ('R_main','n_steps','target_speed','x_end_up'):
            with self.subTest(extra=name),self.assertRaisesRegex(ValueError,'complete finite'):
                p=packet(self.base);p['proposed_parameters'][name]=1;self.validate(p)
    def test_old_mass_scope_and_strict_separation_remain(self):
        p=packet(self.base,mass_scope=True);proof=self.validate(p);ctx=context(self.base,p);actual=delta(candidate(self.base,ctx),ctx)
        self.assertTrue(proof['executable']);self.assertEqual(actual['parameters']['m'],90);self.assertEqual(actual['parameters']['stepH'],.15)
        spec=candidate(self.base,ctx);spec['artifact']['modelica_source']=source_with(spec['artifact']['modelica_source'],stepH=.18)
        with self.assertRaisesRegex(ValueError,'frozen'):delta(spec,ctx)
        p['proposed_parameters'].update(stepH=.18,stepD=.26)
        with self.assertRaises(ValueError):self.validate(p)
    def test_vehicle_geometry_contact_control_task_and_observation_locked(self):
        p=packet(self.base);ctx=context(self.base,p);good=candidate(self.base,ctx)
        variants=[source_with(good['artifact']['modelica_source'],**{name:self.before[name]+.01}) for name in ('m','I_pitch','T_main_max','T_nose_max','R_main','R_nose','wheelbase','k_ground','n_steps','len_flat','x_start_up')]
        variants.extend([good['artifact']['modelica_source'].replace('target_speed = 0.4','target_speed = 0.5'),good['artifact']['modelica_source'].replace('StopTime=120','StopTime=121').replace('StopTime = 120','StopTime = 121')])
        for i,source in enumerate(variants):
            spec=copy.deepcopy(good);spec['artifact']['modelica_source']=source
            with self.subTest(variant=i),self.assertRaises(ValueError):delta(spec,ctx)
        for field,value in [('settings',dict(stop_time=121,intervals=2420)),('checks',[dict(kind='fake')])]:
            spec=copy.deepcopy(good);spec[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):delta(spec,ctx)
    def test_four_dependency_tampering_or_literalization_rejected(self):
        p=packet(self.base);ctx=context(self.base,p);good=candidate(self.base,ctx);values=scope.modelica_parameters(good['artifact']['modelica_source'])[0]
        for name in scope.STAIR_DERIVED_PARAMETERS:
            for replacement in (str(values[name]),scope.STAIR_DERIVED_EXPRESSIONS[name]+'+0'):
                spec=copy.deepcopy(good);spec['artifact']['modelica_source']=source_with(spec['artifact']['modelica_source'],**{name:replacement})
                with self.subTest(name=name,replacement=replacement),self.assertRaisesRegex(ValueError,'frozen'):delta(spec,ctx)
        bad=copy.deepcopy(self.base);bad['artifact']['modelica_source']=source_with(bad['artifact']['modelica_source'],H_peak=3)
        with self.assertRaisesRegex(ValueError,'formula'):scope.validate_request_plan(packet(bad),bad,ctx['sources'])
    def test_family_union_is_not_an_individual_scope_authority(self):
        self.assertEqual(set(scope.FAMILY_VARIABLE_PARAMETERS),{'m','T_main_max','T_nose_max','stepH','stepD'})
        p=packet(self.base);ctx=context(self.base,p);spec=candidate(self.base,ctx);source=spec['artifact']['modelica_source'];tampered=source_with(source,m=86)
        self.assertEqual(scope.model_shape(source,scope.FAMILY_VARIABLE_PARAMETERS),scope.model_shape(tampered,scope.FAMILY_VARIABLE_PARAMETERS))
        spec['artifact']['modelica_source']=tampered
        with self.assertRaisesRegex(ValueError,'frozen'):delta(spec,ctx)
    def test_design_values_and_declared_scope_must_match(self):
        p=packet(self.base);p['design']['parameters'][-1]['value']=.5
        with self.assertRaisesRegex(ValueError,'commitment'):self.validate(p)
        p=packet(self.base);p['scope']['frozen']='anything'
        with self.assertRaises(scope.UnsupportedRequestDesign):self.validate(p)
    def test_plan_tool_and_prompts_expose_two_scopes(self):
        properties=PLAN_TOOL['function']['parameters']['properties']['packet']['properties']
        self.assertEqual(len(properties['scope']['oneOf']),4);self.assertEqual(len(properties['proposed_parameters']['oneOf']),3)
        self.assertIn('stair_dimensions',PLAN_PROMPT);self.assertIn('stair_dimensions',GEN_PROMPT);self.assertIn('source_delta_contract',GEN_PROMPT)


class DualSourceAndRuntimeContracts(unittest.TestCase):
    def setUp(self):self.base=baseline();self.plan=packet(self.base);self.ctx=context(self.base,self.plan);self.spec=candidate(self.base,self.ctx)
    def test_candidate_sysml_uses_new_terrain_and_old_baseline_sidecar_stays_old(self):
        svc=SimpleNamespace(_current_project=lambda:dict(project_id=self.ctx['project_id']),normalize_candidate=lambda spec:(spec,{}))
        flow=object.__new__(TaskFlow);flow.ctx=self.ctx;flow.root=TEMP_ROOT;flow.open=lambda **kwargs:svc
        response=dict(finish_reason='tool_calls',tool_calls=[dict(function=dict(name='proposal_prepare',arguments=dict(spec=self.spec,baseline_sysml_source=sysml_for(self.base))))])
        with patch.object(artifact_baseline,'selection',return_value=self.ctx['baseline_ref']),patch.object(artifact_baseline,'resolve',return_value=self.ctx['baseline_snapshot']):
            spec,proof,checked,completion=flow._validate_generation(response,'generate')
        self.assertTrue(checked['ready']);self.assertEqual(proof['parameters']['stepH'],.18)
        before=module('artifact_sysml').inspect_dual_source(completion['completed_baseline_spec'])[1]
        after=module('artifact_sysml').inspect_dual_source(spec)[1]
        self.assertEqual(before['status'],'PASS');self.assertEqual(after['status'],'PASS')
        self.assertEqual(before['shared_parameters']['stepH'],.15);self.assertEqual(before['shared_parameters']['stepD'],.28)
        self.assertEqual(after['shared_parameters']['stepH'],.18);self.assertEqual(after['shared_parameters']['stepD'],.26)
        self.assertNotIn('sysml_source',self.base['artifact'])
    def test_sysml_old_terrain_or_flattened_dependency_rejected(self):
        for old,new in [('stepH : Real = 0.18','stepH : Real = 0.15'),('x_end_up : Real = x_start_up + n_steps * stepD','x_end_up : Real = 7.2')]:
            spec=copy.deepcopy(self.spec);self.assertIn(old,spec['artifact']['sysml_source']);spec['artifact']['sysml_source']=spec['artifact']['sysml_source'].replace(old,new)
            checked=module('model_artifact').validate_artifact(spec,require_sysml=True)
            self.assertFalse(checked['ready'],checked)
    def test_compiled_xml_reads_stairs_integer_count_platform_and_dependencies(self):
        values,_,kinds=scope.modelica_parameters(self.spec['artifact']['modelica_source']);expected={k:values[k] for k in scope.COMPILED_PARAMETER_NAMES}
        with tempfile.TemporaryDirectory(prefix='stairs-xml-',dir=TEMP_ROOT) as tmp:
            path=Path(tmp)/'model_init.xml'
            def write(changes=None,missing=None):
                p=dict(expected);p.update(changes or {});p.pop(missing,None)
                path.write_text('<fmiModelDescription><DefaultExperiment tolerance="0.01"/><ModelVariables>'+''.join('<ScalarVariable name="'+name+'"><'+('Integer' if kinds[name]=='Integer' else 'Real')+' start="'+str(value)+'"/></ScalarVariable>' for name,value in p.items())+'</ModelVariables></fmiModelDescription>',encoding='utf8')
            write();proof=parameter_readback(self.spec['artifact']['modelica_source'],tmp)
            self.assertEqual(proof['status'],'PASS');self.assertEqual(proof['actual']['n_steps'],20);self.assertEqual(proof['actual']['stepH'],.18);self.assertEqual(len(proof['actual']),13)
            for name,value in [('stepH',.15),('stepD',.28),('m',105),('len_flat',3),('n_steps',19),('x_end_down',15.2)]:
                with self.subTest(name=name):write({name:value});self.assertEqual(parameter_readback(self.spec['artifact']['modelica_source'],tmp)['status'],'FAIL')
            write(missing='H_peak');self.assertEqual(parameter_readback(self.spec['artifact']['modelica_source'],tmp)['status'],'UNKNOWN')


class RuntimeCalculatedParameterRepair(unittest.TestCase):
    def setUp(self):
        base=baseline();self.source=candidate(base,context(base,packet(base)))['artifact']['modelica_source'];self.values=scope.modelica_parameters(self.source)[0]
        self.expected={k:self.values[k] for k in scope.COMPILED_PARAMETER_NAMES}
    def fixture(self,tmp,changes=None,stdout=None):
        folder=Path(tmp);(folder/'model.mo').write_text(self.source,encoding='utf8',newline='\n')
        nodes=[]
        for name,value in self.expected.items():
            calculated=name in scope.STAIR_DERIVED_PARAMETERS
            kind='Integer' if name=='n_steps' else 'Real';start='' if calculated else ' start="'+str(value)+'"'
            nodes.append('<ScalarVariable name="'+name+'" causality="'+('calculatedParameter' if calculated else 'parameter')+'"><'+kind+start+' fixed="true"/></ScalarVariable>')
        (folder/'model_init.xml').write_text('<fmiModelDescription><DefaultExperiment stopTime="120" tolerance="0.01"/><ModelVariables>'+''.join(nodes)+'</ModelVariables></fmiModelDescription>',encoding='utf8')
        values=dict(self.expected);values.update(changes or {})
        line='time=120,'+','.join(k+'='+str(v) for k,v in values.items())
        text='LOG_SUCCESS | info | The initialization finished successfully.\n'+(line if stdout is None else stdout)+'\nLOG_SUCCESS | info | The simulation finished successfully.\n'
        (folder/'simulation.stdout.log').write_text(text,encoding='utf8')
        _record_parameter_output(_simulation_command(['TEST_ONLY_model.exe','-r=synthetic.csv']),folder,'TEST_ONLY_injected_attempt',99999,'TEST_ONLY_injected_task',dict(stdout=text,returncode=0,timed_out=False,uncertain=False))
        return folder
    def test_output_flag_lists_parameters_without_changing_source_or_solver(self):
        command=_simulation_command(['model.exe','-r=result.csv'])
        self.assertEqual(command[-2],'-s=dassl');self.assertEqual(command[-1],'-output='+','.join(scope.COMPILED_PARAMETER_NAMES))
        for flag in ('-output=m','-override=m=105','-overrideFile=unexpected.txt'):
            with self.subTest(flag=flag),self.assertRaises(ValueError):_simulation_command(['model.exe',flag])
    def test_calculated_parameter_without_xml_start_uses_owned_final_stdout(self):
        with tempfile.TemporaryDirectory(prefix='stairs-final-output-',dir=TEMP_ROOT) as tmp:
            folder=self.fixture(tmp);proof=parameter_readback(self.source,folder)
            self.assertEqual(proof['status'],'PASS',proof);self.assertEqual(len(proof['xml_initial_values']),9);self.assertEqual(len(proof['runtime_final_values']),13)
            self.assertEqual(proof['actual']['stepH'],.18);self.assertAlmostEqual(proof['actual']['H_peak'],3.6)
            self.assertEqual(proof['value_origins']['H_peak'],'SIMULATION_FINAL_STDOUT_EVALUATED_PARAMETER');self.assertEqual(proof['xml_causality']['H_peak'],'calculatedParameter')
    def test_source_stdout_xml_value_conflicts_are_not_pass(self):
        for name,value in [('m',105),('stepH',.15),('H_peak',3),('x_end_down',15.2)]:
            with self.subTest(name=name),tempfile.TemporaryDirectory(prefix='stairs-final-conflict-',dir=TEMP_ROOT) as tmp:
                proof=parameter_readback(self.source,self.fixture(tmp,{name:value}));self.assertEqual(proof['status'],'FAIL');self.assertIn(name,' '.join(proof['issues']))
    def test_missing_nonfinite_duplicate_and_wrong_time_stdout_rejected(self):
        full='time=120,'+','.join(k+'='+str(v) for k,v in self.expected.items())
        variants=[full.replace(',H_peak='+str(self.expected['H_peak']),''),full.replace('H_peak='+str(self.expected['H_peak']),'H_peak=nan'),full+',H_peak=3.6',full+'\n'+full,full.replace('time=120','time=0')]
        for i,text in enumerate(variants):
            with self.subTest(variant=i),tempfile.TemporaryDirectory(prefix='stairs-final-invalid-',dir=TEMP_ROOT) as tmp:
                proof=parameter_readback(self.source,self.fixture(tmp,stdout=text));self.assertNotEqual(proof['status'],'PASS')
    def test_unbound_or_modified_stdout_source_and_process_receipts_rejected(self):
        for mode in ('stdout_tamper','source_tamper','binding_missing','failed_process'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory(prefix='stairs-final-binding-',dir=TEMP_ROOT) as tmp:
                folder=self.fixture(tmp)
                if mode=='stdout_tamper':(folder/'simulation.stdout.log').write_text('time=120,H_peak=3.6\n',encoding='utf8')
                elif mode=='source_tamper':(folder/'model.mo').write_text(self.source+'\n// TEST_ONLY changed bytes',encoding='utf8')
                elif mode=='binding_missing':(folder/'runtime-parameter-output.json').unlink()
                else:
                    path=folder/'runtime-parameter-output.json';receipt=json.loads(path.read_text(encoding='utf8'));receipt['returncode']=1;path.write_text(json.dumps(receipt),encoding='utf8')
                self.assertNotEqual(parameter_readback(self.source,folder)['status'],'PASS')
    def test_existing_real_xml_missing_calculated_starts_remains_unknown_without_run(self):
        tool=ROOT/'runs/case-projects/case_3b0265f496f84ae9b800/projects/prj_c200bb1e9f2c4d4abc188d043aac75b2/versions/v000002/tool'
        source=(tool/'model.mo').read_text(encoding='utf8');before={name:(tool/name).read_bytes() for name in ('model.mo','model_init.xml','simulation.stdout.log')}
        proof=parameter_readback(source,tool)
        self.assertEqual(proof['status'],'UNKNOWN');self.assertEqual(len(proof['xml_initial_values']),9);self.assertEqual(proof['runtime_final_values'],{})
        for name in scope.STAIR_DERIVED_PARAMETERS:self.assertEqual(proof['xml_causality'][name],'calculatedParameter');self.assertNotIn(name,proof['actual'])
        self.assertEqual({name:(tool/name).read_bytes() for name in before},before)


class CurrentImportedCapability(unittest.TestCase):
    def run_import(self,base,callback):
        from week13.src import case_projects
        historical=dict(case_id='fixture_old_import',support=dict(status='SUPPORTED',scope='原楼梯工况冻结，仅质量和扭矩可改'),missing=['原基准缺少SysML'],historical_success_statement='TEST_ONLY history')
        frozen=copy.deepcopy(historical);ctx=context(base,packet(base));snapshot=copy.deepcopy(ctx['baseline_snapshot'])
        with tempfile.TemporaryDirectory(prefix='stairs-import-',dir=TEMP_ROOT) as tmp:
            marker=Path(tmp)/'case.json';marker.write_text(json.dumps(historical,ensure_ascii=False),encoding='utf8');original=marker.read_bytes();ctx['project_root']=tmp
            with patch.object(case_projects,'resolve_case',return_value=historical),patch.object(case_projects,'verify_case',side_effect=lambda record:record):
                callback(ctx)
            self.assertEqual(marker.read_bytes(),original)
        self.assertEqual(historical,frozen);self.assertEqual(ctx['baseline_snapshot'],snapshot)
    def test_old_import_support_is_historical_current_stair_baseline_is_supported(self):
        base=baseline();base['artifact']['modelica_source']=source_with(base['artifact']['modelica_source'],stepH=.18,stepD=.26)
        def check(ctx):
            obligations=validate_import_plan(ctx,ctx['plan'])
            self.assertEqual(obligations['support']['status'],'SUPPORTED');self.assertIn('高度/深度',obligations['support']['scope'])
            self.assertEqual(obligations['support']['parameters']['stepH'],.18);self.assertEqual(obligations['support']['parameters']['stepD'],.26)
            self.assertFalse(obligations['support']['execution_authorized']);self.assertEqual(obligations['historical_import_support']['scope'],'原楼梯工况冻结，仅质量和扭矩可改')
        self.run_import(base,check)
    def test_historically_supported_import_does_not_bypass_current_shape_lock(self):
        base=baseline();base['artifact']['modelica_source']=source_with(base['artifact']['modelica_source'],R_main=.2)
        def check(ctx):
            obligations=imported_obligations(ctx);self.assertEqual(obligations['historical_import_support']['status'],'SUPPORTED');self.assertEqual(obligations['support']['status'],'UNSUPPORTED')
            with self.assertRaisesRegex(scope.UnsupportedRequestDesign,'source family is unsupported'):validate_import_plan(ctx,ctx['plan'])
        self.run_import(base,check)


class TerrainMissionContracts(unittest.TestCase):
    def setUp(self):
        base=baseline();self.ctx=context(base,packet(base));self.spec=candidate(base,self.ctx);self.values=scope.modelica_parameters(self.spec['artifact']['modelica_source'])[0]
    def test_new_dimensions_drive_20_up_20_down_wheel_edge_exit_and_continue(self):
        rows=trajectory(self.values);proof=assess_rows(rows,self.values);binding=terrain_binding(rows,self.values)
        self.assertEqual(proof['task_status'],'PASS');self.assertEqual(binding['status'],'PASS');self.assertEqual(len(proof['events']),120);self.assertEqual(len(proof['terrain_events']),120)
        self.assertAlmostEqual(proof['exit_x'],14.4);self.assertGreater(proof['rear_edge_at_completion'],14.4);self.assertGreater(proof['post_exit_distinct_times'],2)
        self.assertAlmostEqual(binding['parameters']['H_peak'],3.6)
    def test_old_terrain_and_wrong_floor_convention_do_not_pass_new_case(self):
        old=scope.modelica_parameters(baseline()['artifact']['modelica_source'])[0]
        self.assertEqual(terrain_binding(trajectory(old),self.values)['status'],'FAIL')
        rows=trajectory(self.values);index=next(i for i,row in enumerate(rows) if 3<row['x_f']<4);rows[index]['h_f']+=self.values['stepH']
        self.assertEqual(terrain_binding(rows,self.values)['status'],'FAIL')
    def test_center_only_exit_and_nonfinite_or_inverted_state_still_fail(self):
        self.assertEqual(assess_rows(trajectory(self.values,self.values['x_end_down']+.25),self.values)['task_status'],'FAIL')
        rows=trajectory(self.values);rows[5]['v']=float('nan');self.assertEqual(assess_rows(rows,self.values)['task_status'],'UNKNOWN')
        rows=trajectory(self.values);rows[5]['x_f']=rows[5]['x_r'];self.assertEqual(assess_rows(rows,self.values)['validity_status'],'FAIL')
    def test_source_bound_csv_accepts_full_horizon_and_refuses_terrain_mismatch(self):
        rows=trajectory(self.values)
        while rows[-1]['time']<120:
            row=copy.deepcopy(rows[-1]);row['time']=round(min(120,row['time']+.05),8)
            for key in ('x','x_n','x_f','x_r'):row[key]+=.02
            rows.append(row)
        with tempfile.TemporaryDirectory(prefix='stairs-csv-',dir=TEMP_ROOT) as tmp:
            path=Path(tmp)/'synthetic.csv'
            def write():
                with path.open('w',newline='',encoding='utf8') as handle:
                    out=csv.DictWriter(handle,fieldnames=['time',*REQUIRED]);out.writeheader();out.writerows(rows)
            write();proof=assess(self.spec,path);self.assertEqual(proof['task_status'],'PASS',proof);self.assertEqual(proof['terrain_binding']['parameters']['stepH'],.18)
            rows[200]['h_n']+=.01;write();proof=assess(self.spec,path);self.assertEqual(proof['task_status'],'UNKNOWN');self.assertEqual(proof['terrain_binding']['status'],'FAIL')


if __name__=='__main__':unittest.main()
