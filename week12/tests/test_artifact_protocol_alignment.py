"""Finite offline protocol boundaries; synthetic data never proves a vehicle."""
import asyncio, ast, copy, hashlib, io, json, tempfile, unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from week12.src.car_domain.artifact_cli import ArtifactCLI
from week12.src.car_domain.artifact_continuous import FLOW_PROMPT,DesignProtocolError,SourceBindingError,validate_commitment,projection
from week12.src.car_domain.artifact_trajectory import CONTRACT_PROMPT,ProtocolValueError,validate_contract
from week12.src.car_domain.artifact_runtime import SYSTEM_PROMPT
from week12.src.car_domain.model_artifact import validate_artifact
from week12.tests.test_artifact_trajectory import fixture
from week12.tests.test_artifact_continuous_cli import commitment

OUT=Path(__file__).resolve().parents[1]/'results/s5-protocol-alignment-20261014-01'

class Checks(unittest.TestCase):
    def rejection(self,mutate,path,actual=None):
        spec=fixture();mutate(spec);before=copy.deepcopy(spec)
        result=validate_artifact(spec)
        self.assertFalse(result['ready']);self.assertEqual(spec,before)
        issue=result['issues'][0];self.assertEqual(issue['path'],path,issue)
        self.assertIn('expected',issue);self.assertIn('actual',issue)
        if actual is not None:self.assertEqual(issue['actual'],actual)
        json.dumps(issue,allow_nan=False)
        return issue

    def test_valid_shared_schema_design_and_generation(self):
        spec=fixture();before=copy.deepcopy(spec)
        self.assertTrue(validate_artifact(spec)['ready']);self.assertEqual(spec,before)
        self.assertIn(CONTRACT_PROMPT,SYSTEM_PROMPT)
        self.assertIn(CONTRACT_PROMPT,ArtifactCLI._flow_generation_schema())
        self.assertIn('current.x_min > previous.x_max',CONTRACT_PROMPT)
        self.assertIn('touching edges',CONTRACT_PROMPT)
        value=projection(spec);value['literal_bindings']=[dict(name=p['name'],value=p['value'],unit=p['unit'],origin='model',source_id=None,source_text=None) for p in spec['design']['parameters']]
        self.assertEqual(validate_commitment(value,spec['sources']),value)

    def test_label_raw_variable_missing_and_nonstring_mapping(self):
        p='$.trajectory_contract.entities["front"].x'
        for value in ('fx','missing',[],{},None,1):
            with self.subTest(value=value):
                self.rejection(lambda s:s['trajectory_contract']['entities']['front'].update(x=value),p,value)
        self.rejection(lambda s:s['outputs'].pop('front_x'),p,'front_x')
        self.rejection(lambda s:s['outputs'].update(front_x=[]),'$.outputs["front_x"]',[])

    def test_duplicate_labels_and_csv_aliases(self):
        p='$.trajectory_contract.entities["rear"].x'
        self.rejection(lambda s:s['trajectory_contract']['entities']['rear'].update(x='front_x'),p,'front_x')
        self.rejection(lambda s:s['outputs'].update(rear_x=s['artifact']['model_name']+'.fx'),p,'rear_x')
        self.rejection(lambda s:s['outputs'].update(rear_x='fx'),p,'rear_x')

    def test_overlap_touch_reverse_and_strict_gap(self):
        p='$.trajectory_contract.milestones[1].targets["front"].x_min'
        for minimum in (.2,.25,-.2):
            with self.subTest(minimum=minimum):
                issue=self.rejection(lambda s:s['trajectory_contract']['milestones'][1]['targets']['front'].update(x_min=minimum),p,minimum)
                self.assertEqual(issue['previous_x_max'],.25)
                self.assertEqual(issue['previous_path'],'$.trajectory_contract.milestones[0].targets["front"].x_max')
        spec=fixture();spec['trajectory_contract']['milestones'][1]['targets']['front']['x_min']=.250001
        self.assertTrue(validate_artifact(spec)['ready'])

    def test_width_tolerance_finite_and_height(self):
        p='$.trajectory_contract.milestones[0].targets["front"]'
        for maximum in (.002,.001):
            issue=self.rejection(lambda s:s['trajectory_contract']['milestones'][0]['targets']['front'].update(x_max=maximum),p)
            self.assertEqual(issue['minimum_exclusive_width'],.002)
        self.rejection(lambda s:s['trajectory_contract']['milestones'][0]['targets']['front'].update(z_min=2),p+'.z_min',2)
        for bad in (float('nan'),float('inf'),True):
            spec=fixture();spec['trajectory_contract']['max_speed']=bad
            result=validate_artifact(spec);self.assertFalse(result['ready'])
            self.assertEqual(result['issues'][0]['path'],'$.trajectory_contract.max_speed')
            json.dumps(result,allow_nan=False)
            if isinstance(bad,float):self.assertIs(spec['trajectory_contract']['max_speed'],bad)

    def test_coverage_horizon_and_source_bindings(self):
        self.rejection(lambda s:s['trajectory_contract']['milestones'][0]['targets'].pop('rear'),'$.trajectory_contract.milestones[0].targets')
        self.rejection(lambda s:s['trajectory_contract'].update(end_time=3),'$.trajectory_contract.end_time',3)
        self.rejection(lambda s:s['trajectory_contract'].update(time_tolerance=4),'$.trajectory_contract.time_tolerance',4)
        self.rejection(lambda s:s['trajectory_contract']['requirement_refs'][0].update(source_id='unknown'),'$.trajectory_contract.requirement_refs[0].source_id','unknown')
        self.rejection(lambda s:s['trajectory_contract']['requirement_refs'][0].update(source_id=[]),'$.trajectory_contract.requirement_refs[0].source_id',[])
        self.rejection(lambda s:s['trajectory_contract']['requirement_refs'][0].update(source_text='DRIFT'),'$.trajectory_contract.requirement_refs[0].source_text','DRIFT')
        self.rejection(lambda s:s['trajectory_contract'].update(model_basis='absent source text'),'$.trajectory_contract.model_basis','absent source text')
        self.rejection(lambda s:s['requirements'][0].update(source_id='unknown'),'$.requirements[0].source_id','unknown')
        self.rejection(lambda s:s['requirements'][0].update(source_text='DRIFT'),'$.requirements[0].source_text','DRIFT')

    def harness(self,root,sources,context,pointer):
        cli=ArtifactCLI.__new__(ArtifactCLI);cli.session_id='TEST_ONLY_protocol'
        cli.store=SimpleNamespace(db_path=root/'projects.sqlite',current_project=lambda _:context['project'])
        cli.context=SimpleNamespace(assert_current=lambda:None)
        cli.read_only=False;cli.continuous=True;cli.responses=[];cli.tool_results=[]
        cli._flow_sources=lambda:copy.deepcopy(sources)
        cli._flow_budget=lambda:dict(runtime_config={},runtime_ledger=str(root/'absent.sqlite'),max_tasks=1,worker_timeout_s=1,simulation_timeout_s=1)
        cli._flow_pointer().write_text(json.dumps(pointer),encoding='utf8')
        cli.dispatched=[]
        async def reject_call(*args,**kwargs):
            cli.dispatched.append(args);raise AssertionError('unexpected tool/Worker')
        cli._call=reject_call
        return cli

    def test_one_design_correction_gets_precise_error_then_stops(self):
        source=dict(id='src_test',text='TEST_ONLY 质量1kg；1秒末值 signal >= 0。')
        value=commitment(source);value['outputs']['height']='height'
        contract=dict(copy.deepcopy(fixture()['trajectory_contract']),entities=dict(car=dict(x='signal',z='height')),end_time=1,model_basis='unused')
        contract.pop('model_basis');contract['requirement_refs']=[dict(source_id=source['id'],source_text=source['text'])]
        contract['milestones']=[dict(id='a',phase='flat',targets=dict(car=dict(x_min=.1,x_max=.3,z_min=0,z_max=0))),dict(id='b',phase='flat',targets=dict(car=dict(x_min=.2,x_max=.4,z_min=0,z_max=0)))]
        value['trajectory_contract']=contract
        bad=dict(schema='v11-continuous-design-1',state='READY',body='TEST_ONLY invalid overlapping route',commitment=value)
        response=dict(content=json.dumps(bad),tool_calls=[],finish_reason='stop')
        context=dict(project=dict(project_id='TEST_ONLY_project'),trusted_sources=[source])
        messages=[dict(role='system',content=FLOW_PROMPT+ArtifactCLI._flow_generation_schema()),dict(role='user',content=json.dumps(context))]
        rid='TEST_ONLY_design';pointer=dict(project_id=context['project']['project_id'],sources=[source],messages=messages,request_id=rid)
        with tempfile.TemporaryDirectory(dir=OUT) as td,redirect_stdout(io.StringIO()):
            root=Path(td);cli=self.harness(root,[source],context,pointer)
            class Provider:
                ledger_path=root/'absent.sqlite'
                def __init__(self):self.calls=[];self.cache={}
                def complete(self,sent,tools,request_id):
                    if request_id in self.cache:return copy.deepcopy(self.cache[request_id])
                    self.calls.append((request_id,copy.deepcopy(sent)));self.cache[request_id]=response
                    return copy.deepcopy(response)
            cli.runtime=Provider()
            for _ in range(2):
                with self.assertRaisesRegex(ValueError,'有限纠正'):asyncio.run(cli.submit_prepared_request(messages,[],rid))
            self.assertEqual([r for r,_ in cli.runtime.calls],[rid,rid+'_format1'])
            corrected=cli.runtime.calls[1][1][0]['content']
            self.assertIn('milestones[1].targets',corrected);self.assertIn('previous_x_max',corrected)
            self.assertIn('"actual":0.2',corrected);self.assertIn('"expected"',corrected)
            self.assertEqual(cli.dispatched,[]);self.assertEqual(list(root.rglob('*-approval.json')),[])
            self.assertIsNone(cli._flow_current().get('design_id'))

    def test_flow_prefix_unchanged_and_source_review_remains(self):
        tree=ast.parse((OUT/'backup/artifact_continuous.py').read_text(encoding='utf8'))
        old=next(ast.literal_eval(node.value) for node in tree.body if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='FLOW_PROMPT' for t in node.targets))
        self.assertEqual(FLOW_PROMPT,old)
        source=dict(id='src_test',text='TEST_ONLY 质量1kg；1秒末值 signal >= 0。')
        value=commitment(source);value['literal_bindings'][0]['source_text']='mass unknown'
        with self.assertRaises(SourceBindingError) as caught:validate_commitment(value,[source])
        self.assertEqual(caught.exception.issue['path'],'$.literal_bindings[0]')
        context=dict(project=dict(project_id='TEST_ONLY_project'),trusted_sources=[source])
        pointer=dict(project_id=context['project']['project_id'],sources=[source],request_id='TEST_ONLY_review',messages=[])
        with tempfile.TemporaryDirectory(dir=OUT) as td,redirect_stdout(io.StringIO()):
            cli=self.harness(Path(td),[source],context,pointer)
            record=cli._save_flow_design('TEST_ONLY_review',context,dict(content=json.dumps(dict(schema='v11-continuous-design-1',state='READY',body='TEST_ONLY unsupported source',commitment=value))))
            self.assertEqual(record['state'],'SOURCE_REVIEW')
            self.assertEqual(record['source_review']['path'],'$.literal_bindings[0]')
            with self.assertRaisesRegex(ValueError,'待澄清'):asyncio.run(cli._flow_continue('确认'))
            self.assertEqual(cli.dispatched,[]);self.assertEqual(list(Path(td).rglob('*-approval.json')),[])
        source=dict(id='src_test',text='TEST_ONLY 静止起步；质量1kg；1秒末值 signal >= 0。')
        value=commitment(source);value['literal_bindings'].append(dict(name='initial_speed',value=0,unit='m/s',origin='user',source_id=source['id'],source_text='静止起步'))
        self.assertEqual(validate_commitment(value,[source]),value)
        value['literal_bindings'][-1].update(name='drag',value=2,unit='N*s/m',source_text='阻力F=-2*v，系数单位N*s/m')
        source['text']+='阻力F=-2*v，系数单位N*s/m'
        self.assertEqual(validate_commitment(value,[source]),value)
