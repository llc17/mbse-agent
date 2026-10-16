"""TEST_ONLY delivery fixtures. No provider, Worker, OMC, or physical acceptance."""
from pathlib import Path
from copy import deepcopy
import contextlib,io,json,tempfile,unittest
from unittest.mock import patch
from types import SimpleNamespace
from week11.src.store import digest_json
from week12.src.car_domain.model_artifact import save,sha,summarize_execution
from week12.src.car_domain.artifact_delivery import create_delivery,file_record
from week12.src.car_domain.artifact_history import snapshot
from week12.src.car_domain.artifact_cli import ArtifactCLI
from week12.tests.test_model_artifact import fixture,SOURCE_TEXT
OUT=Path(__file__).resolve().parents[1]/'results'
TOOL=dict(compile=dict(status='PASS'),simulation=dict(status='PASS'),test_only=True,toolchain_invocations=0)

class Checks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='delivery-',dir=OUT);self.root=Path(self.temp.name)
        self.spec=fixture();self.csv=self.root/'tool/model_res.csv';self.csv.parent.mkdir(exist_ok=True);self.csv.write_text('time,signal\n0,0\n0.5,.3\n0.5,.4\n1,.6\n',encoding='utf8')
        self.binding=dict(task_id='task_test',project_id='prj_test',version_id='v000001',spec_digest=digest_json(self.spec),source_sha256=sha(self.spec['artifact']['modelica_source'].encode()))
    def tearDown(self):self.temp.cleanup()
    def test_event_samples_mapping_and_pagination(self):
        self.spec['outputs']={('输出'+str(i)):'ArtifactContractFixture.signal' for i in range(9)};self.spec['checks']=[];self.binding['spec_digest']=digest_json(self.spec)
        result=summarize_execution(self.spec,TOOL,self.csv);result['test_only']=True
        summary=create_delivery(self.spec,self.csv,self.root,self.binding,result)
        report=json.loads((self.root/'delivery.json').read_bytes());self.assertEqual(summary['status'],'PASS');self.assertEqual(len(report['plots']),3)
        self.assertEqual(report['samples']['rows'],4);self.assertEqual(report['samples']['duplicate_times'],1)
        self.assertEqual(sum((p['outputs'] for p in report['plots']),[]),list(self.spec['outputs']))
        self.assertEqual(result['task_status'],'UNKNOWN');self.assertEqual(result['vehicle_task_status'],'UNKNOWN')
    def test_missing_bad_nonfinite_data_never_pass(self):
        cases={'missing':None,'empty':'time,signal\n','column':'time,other\n0,0\n1,.6\n','nonfinite':'time,signal\n0,nan\n1,.6\n','bad':'time,signal\n0,text\n1,.6\n','time':'time,signal\n0,0\n-1,.6\n','row':'time,signal\n0,0,9\n1,.6\n'}
        for name,text in cases.items():
            with self.subTest(name=name):
                path=self.root/(name+'.csv')
                if text is not None:path.write_text(text,encoding='utf8')
                result=summarize_execution(self.spec,TOOL,path);self.assertEqual(result['tool_status'],'PASS');self.assertNotEqual(result['chain_status'],'PASS');self.assertEqual(result['task_status'],'UNKNOWN')
                target=self.root/name;target.mkdir();summary=create_delivery(self.spec,path,target,self.binding,result,source_root=self.root)
                self.assertEqual(summary['data_status'],'INCOMPLETE');self.assertFalse(list(target.glob('curves/*.png')))
    def test_render_failure_keeps_tool_and_observation(self):
        result=summarize_execution(self.spec,TOOL,self.csv);before=deepcopy(result)
        with patch('week12.src.car_domain.artifact_delivery.render_curves',side_effect=ImportError('TEST_ONLY absent graph library')):summary=create_delivery(self.spec,self.csv,self.root,self.binding,result)
        self.assertEqual(summary['plot_status'],'FAILED');self.assertEqual(summary['data_status'],'PASS');self.assertEqual(result,before);self.assertEqual(result['task_status'],'PASS')
    def make_snapshot(self,*,delivery=True,missing=False,blocked=False):
        spec=self.spec;task=dict(task_id='task_test',project_id='prj_test',proposal_id='prop_test',version_id='v000001',session_id='session_test',state='failed' if blocked else 'succeeded',worker_owner='owner_test',created_at='TEST_ONLY',updated_at='TEST_ONLY')
        base=self.root/'projects/prj_test/versions/v000001';base.mkdir(parents=True);(base/'tool').mkdir();csv_path=base/'tool/model_res.csv'
        if not missing:csv_path.write_bytes(self.csv.read_bytes())
        outcome=summarize_execution(spec,TOOL,csv_path);outcome.update(test_only=True,toolchain_invocations=0)
        display=dict(project_id=task['project_id'],spec_digest=digest_json(spec),source_sha256=self.binding['source_sha256'],requirements=spec['requirements'],sources=spec['sources'],model_design=spec['design'],artifact=spec['artifact'],libraries=spec['libraries'],settings=spec['settings'],outputs=spec['outputs'],checks=spec['checks'],operation='create',baseline_version_id=None,execution_context_id='context_test',execution_context_sha256='1'*64)
        proposal=dict(project_id=task['project_id'],session_id=task['session_id'],spec=spec,spec_digest=digest_json(spec),display=display,displayed_digest=digest_json(display),operation='create',baseline_version_id=None,confirmation={'test_only':True})
        if delivery:outcome['result_delivery']=create_delivery(spec,csv_path,base,self.binding,outcome)
        save(base/'spec.json',spec);save(base/'confirmation.json',proposal['confirmation']);save(base/'outcome.json',outcome);(base/'frozen-model.mo').write_text(spec['artifact']['modelica_source'],encoding='utf8',newline='\n')
        binding=dict(self.binding,owner=task['worker_owner'],context_id=display['execution_context_id'],context_sha256=display['execution_context_sha256']);binding.pop('version_id')
        manifest=dict(schema='v11-artifact-manifest-1',binding=binding,outcome=outcome,files={p.relative_to(base).as_posix():file_record(p) for p in base.rglob('*') if p.is_file()})
        path=base/'artifact-manifest.json';save(path,manifest);task['result']=dict(outcome,outcome_manifest_path=str(path),outcome_manifest_sha256=sha(path.read_bytes()),worker_exit_code=0,worker_pid=1)
        store=SimpleNamespace(proposal=lambda _:proposal,sources=lambda *_:[dict(source_id='src_fixture',text=SOURCE_TEXT,text_sha256=sha(SOURCE_TEXT.encode()))]);service=SimpleNamespace(store=store,_require_current_project=lambda _:None)
        return service,task,base,manifest
    def call_snapshot(self,service,task):
        with patch('week12.src.car_domain.artifact_history.WEEK12_ROOT',self.root):return snapshot(service,task)[0]
    def test_manifest_snapshot_cli_new_delivery(self):
        service,task,base,manifest=self.make_snapshot();record=self.call_snapshot(service,task)
        self.assertEqual(record['result_delivery']['status'],'PASS');self.assertEqual(len(record['curve_files']),1);self.assertEqual(record['data_files']['CSV'],str(base/'tool/model_res.csv'))
        captured=io.StringIO()
        with contextlib.redirect_stdout(captured):ArtifactCLI._print_snapshot(record)
        self.assertIn('基本曲线：',captured.getvalue());self.assertIn('UNKNOWN',captured.getvalue());self.assertIn('原始数据CSV',captured.getvalue())
    def test_historical_no_plot_is_explicit(self):
        service,task,base,manifest=self.make_snapshot(delivery=False);record=self.call_snapshot(service,task)
        self.assertEqual(record['result_delivery']['status'],'HISTORICAL_NOT_GENERATED');self.assertEqual(record['curve_files'],[]);self.assertEqual(record['task_status'],'PASS')
    def test_missing_csv_honest_unknown_stays_queryable(self):
        service,task,base,manifest=self.make_snapshot(missing=True,blocked=True);record=self.call_snapshot(service,task)
        self.assertEqual(record['tool_status'],'PASS');self.assertEqual(record['task_status'],'UNKNOWN');self.assertEqual(record['chain_status'],'BLOCKED_EVIDENCE');self.assertIsNone(record['data_files']['CSV'])
    def test_missing_csv_claimed_pass_is_rejected(self):
        service,task,base,manifest=self.make_snapshot(delivery=False,missing=True,blocked=True)
        manifest['outcome'].update(chain_status='PASS',task_status='PASS');save(base/'outcome.json',manifest['outcome']);manifest['files']['outcome.json']=file_record(base/'outcome.json');save(base/'artifact-manifest.json',manifest)
        task['result'].update(manifest['outcome'],outcome_manifest_sha256=sha((base/'artifact-manifest.json').read_bytes()))
        with self.assertRaisesRegex(ValueError,'original CSV'):self.call_snapshot(service,task)
    def test_delivery_hash_or_identity_tamper_is_rejected(self):
        service,task,base,manifest=self.make_snapshot();report_path=base/'delivery.json';report=json.loads(report_path.read_bytes());report['binding']['task_id']='task_other';save(report_path,report)
        with self.assertRaisesRegex(ValueError,'hash mismatch'):self.call_snapshot(service,task)
        manifest['files']['delivery.json']=file_record(report_path);save(base/'artifact-manifest.json',manifest);task['result']['outcome_manifest_sha256']=sha((base/'artifact-manifest.json').read_bytes())
        with self.assertRaisesRegex(ValueError,'identity mismatch'):self.call_snapshot(service,task)
    def test_curve_bytes_tamper_is_rejected(self):
        service,task,base,manifest=self.make_snapshot();(base/'curves/signals-01.png').write_bytes(b'TEST_ONLY forged')
        with self.assertRaisesRegex(ValueError,'hash mismatch'):self.call_snapshot(service,task)

if __name__=='__main__':unittest.main()
