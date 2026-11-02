import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from week13.src.case_backend import module
from week13.src.case_cli import _parse_one
from week13.src.case_paths import PACKAGE
from week13.src.case_toolchain import parameter_readback,_build_mos

def fixture():
    source='model Fixture parameter Real a=.2; parameter Real extension=.3; parameter Real nose=a+extension; Real x(start=0); equation der(x)=0; annotation(experiment(StopTime=120,Tolerance=.01,Interval=.05)); end Fixture;'
    requirement=dict(text='fixture "quoted" requirement',source_id='src_fixture',source_text='fixture evidence')
    traces=''.join('attribute '+key+' : String = '+json.dumps(value)+';' for key,value in dict(requirement,model_name='Fixture').items())
    sysml='package Test { part def System { attribute a : Real = .2; attribute extension : Real = .3; attribute nose : Real = a + extension; } part system : System; requirement req_1 {'+traces+'} }'
    return dict(schema_version='v11-model-artifact-1',backend='model-artifact',artifact=dict(model_name='Fixture',modelica_source=source,sysml_source=sysml),sources=[dict(id='src_fixture',text='fixture evidence')],requirements=[requirement],design=dict(summary='synthetic schema fixture, not vehicle product output',assumptions=[],parameters=[dict(name='nose',value=.5,unit='m',origin='model',basis='finite dependency fixture')]),libraries=[],settings=dict(stop_time=120,intervals=2400),outputs=dict(x='x'),checks=[])

class AgentContract(unittest.TestCase):
    def test_finite_expressions_and_requirement_trace_parse(self):
        result=module('model_artifact').validate_artifact(fixture(),require_sysml=True)
        self.assertTrue(result['ready'],result)
    def test_flattened_sysml_expression_rejected(self):
        spec=fixture();spec['artifact']['sysml_source']=spec['artifact']['sysml_source'].replace('a + extension','.5')
        result=module('model_artifact').validate_artifact(spec,require_sysml=True)
        self.assertFalse(result['ready']);self.assertIn('dependency',result['issues'][0]['message'])
    def test_task_trace_source_cannot_be_fabricated(self):
        spec=fixture();spec['requirements'][0]['source_text']='invented'
        self.assertFalse(module('model_artifact').validate_artifact(spec,require_sysml=True)['ready'])
    def test_parameter_metadata_uses_effective_value(self):
        spec=fixture();spec['design']['parameters'][0]['value']=.6
        self.assertFalse(module('model_artifact').validate_artifact(spec)['ready'])
    def test_extended_observation_supported_but_not_unbounded(self):
        spec=fixture();spec['settings']=dict(stop_time=600,intervals=12000)
        self.assertTrue(module('model_artifact').validate_artifact(spec)['ready'])
        spec['settings']=dict(stop_time=601,intervals=12020)
        self.assertFalse(module('model_artifact').validate_artifact(spec)['ready'])
    def test_one_json_layer_and_truncated_rejection(self):
        packet=dict(source='line1\n"line2"')
        response=dict(tool_calls=[dict(function=dict(name='test',arguments=json.dumps(dict(packet=packet))))],finish_reason='tool_calls')
        self.assertEqual(_parse_one(response,'test','packet'),packet)
        response['tool_calls'][0]['function']['arguments']=json.dumps(json.dumps(dict(packet=packet)))
        with self.assertRaises(ValueError):_parse_one(response,'test','packet')
        response['finish_reason']='length'
        with self.assertRaises(ValueError):_parse_one(response,'test','packet')
    def test_explicit_solver_and_runtime_parameter_xml(self):
        self.assertIn('method="dassl"',_build_mos('Fixture',120,2400))
        source='model X parameter Real m=1; parameter Real I_pitch=2; parameter Real T_main_max=3; parameter Real T_nose_max=4; end X;'
        with tempfile.TemporaryDirectory(dir=PACKAGE) as tmp:
            path=Path(tmp)/'model_init.xml'
            path.write_text('<fmiModelDescription><DefaultExperiment tolerance="0.01"/><ModelVariables>'+''.join('<ScalarVariable name="'+name+'"><Real start="'+str(value)+'"/></ScalarVariable>' for name,value in [('m',1),('I_pitch',2),('T_main_max',3),('T_nose_max',4)])+'</ModelVariables></fmiModelDescription>',encoding='utf8')
            # This synthetic XML fixture intentionally covers only four names.
            result=parameter_readback(source,tmp,parameter_names=('m','I_pitch','T_main_max','T_nose_max'))
            self.assertEqual(result['status'],'PASS');self.assertEqual(result['actual']['m'],1)
    def test_worker_context_finite_new_caps(self):
        with tempfile.TemporaryDirectory(dir=PACKAGE) as tmp:
            path=Path(tmp)/'context.json';value=dict(schema='v11-artifact-execution-context-1',context_id='fixture',status='ACTIVE',mode='TEST_ONLY',allow_execution=False,max_tasks=6,worker_timeout_s=1900,simulation_timeout_s=900)
            path.write_text(json.dumps(value),encoding='utf8')
            context=module('model_artifact').ExecutionContext.load(path)
            self.assertEqual(context.worker_timeout_s,1900)
            value['worker_timeout_s']=1901;path.write_text(json.dumps(value),encoding='utf8')
            with self.assertRaises(ValueError):module('model_artifact').ExecutionContext.load(path)

if __name__=='__main__':unittest.main()
