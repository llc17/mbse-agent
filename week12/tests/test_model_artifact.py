"""TEST_ONLY protocol fixtures, never product vehicle designs."""
from copy import deepcopy
from pathlib import Path
import json,tempfile,unittest
from week12.src.car_domain.model_artifact import validate_artifact,summarize_execution,ExecutionContext,save
from week12.src.car_domain.artifact_service import ArtifactService

RESULTS=Path(__file__).resolve().parents[1]/'results/s3-de-model-artifact-20261011-01'
SOURCE_TEXT='TEST_ONLY equation interface fixture; final signal >= 0.5 at 1 second. This is not a vehicle design or a human physical approval.'
SOURCE='model ArtifactContractFixture\n  parameter Real level = 0.6;\n  Real signal(start=0,fixed=true);\nequation\n  der(signal)=level;\nend ArtifactContractFixture;\n'
def fixture(source_id='src_fixture',text=SOURCE_TEXT):
    return dict(schema_version='v11-model-artifact-1',backend='model-artifact',artifact=dict(model_name='ArtifactContractFixture',modelica_source=SOURCE),sources=[dict(id=source_id,text=text)],requirements=[dict(text='TEST_ONLY interface contract',source_id=source_id,source_text=text)],design=dict(summary='TEST_ONLY single equation fixture; no vehicle',assumptions=[dict(text='TEST_ONLY simple signal only',origin='model')],parameters=[dict(name='level',value=.6,unit='1/s',origin='model',basis='TEST_ONLY fixture value')]),libraries=[],settings=dict(stop_time=1,intervals=20),outputs=dict(signal='signal'),checks=[dict(kind='final_ge',output='signal',threshold=.5,source_id=source_id,source_text=text)])
def context(path,mode='TEST_ONLY',failure=False,allow=True):
    save(path,dict(schema='v11-artifact-execution-context-1',context_id=path.stem,status='ACTIVE',mode=mode,allow_execution=allow,max_tasks=1,worker_timeout_s=300,simulation_timeout_s=120,test_tool_failure=failure))
    return ExecutionContext.load(path)

class ArtifactChecks(unittest.TestCase):
    def setUp(self):
        RESULTS.mkdir(parents=True,exist_ok=True);self.tmp=tempfile.TemporaryDirectory(dir=RESULTS);self.path=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def service(self):
        ctx=context(self.path/'context.json');service=ArtifactService(self.path/'db.sqlite','test',context=ctx,worker_manager=object())
        service.project_create('TEST_ONLY_protocol');source=service.record_source(SOURCE_TEXT)
        spec=fixture(source['source_id']);return service,spec
    def test_unicode_logical_output_label(self):
        spec=fixture();spec['outputs']={'末值':'signal'};spec['checks'][0]['output']='末值'
        self.assertTrue(validate_artifact(spec)['ready'])
        for label in ('', '   ', 'x'*129, 'bad\nlabel'):
            spec['outputs']={label:'signal'};spec['checks'][0]['output']=label
            self.assertFalse(validate_artifact(spec)['ready'])
        spec['outputs']={'末值':'not a CSV name'}
        self.assertFalse(validate_artifact(spec)['ready'])
    def test_exact_model_root_csv_mapping(self):
        spec=fixture();spec['outputs']={'末值':'ArtifactContractFixture.signal'};spec['checks'][0]['output']='末值'
        csv=self.path/'result.csv';csv.write_text('time,signal\n0,0\n1,.6\n')
        result=summarize_execution(spec,dict(compile=dict(status='PASS'),simulation=dict(status='PASS')),csv)
        self.assertEqual(result['output_mapping']['末值'],dict(requested='ArtifactContractFixture.signal',resolved='signal'))
        self.assertEqual(result['task_status'],'PASS')
    def test_mapping_does_not_guess_suffix(self):
        spec=fixture();spec['outputs']['signal']='OtherRoot.signal'
        csv=self.path/'result.csv';csv.write_text('time,signal\n0,0\n1,.6\n')
        result=summarize_execution(spec,dict(compile=dict(status='PASS'),simulation=dict(status='PASS')),csv)
        self.assertEqual(result['chain_status'],'OUTPUT_CONTRACT_MISMATCH')
        self.assertIsNone(result['output_mapping']['signal']['resolved'])
    def test_supported_artifact_and_parameter_readback(self):
        self.assertTrue(validate_artifact(fixture())['ready'])
        spec=fixture();spec['design']['parameters'][0]['value']=.7
        self.assertFalse(validate_artifact(spec)['ready'])
    def test_unsafe_source_and_model_permissions_rejected(self):
        for text in ('external "C";','Modelica.Utilities.System.command("x");','import OtherLibrary;'):
            spec=fixture();spec['artifact']['modelica_source']+=text
            self.assertFalse(validate_artifact(spec)['ready'])
        spec=fixture();spec['authorization']={'approved':True}
        self.assertFalse(validate_artifact(spec)['ready'])
    def test_model_assumptions_are_not_user_sources(self):
        spec=fixture();spec['design']['assumptions'][0]['origin']='user'
        self.assertFalse(validate_artifact(spec)['ready'])
        spec=fixture();spec['requirements'][0]['source_text']='invented user text'
        self.assertFalse(validate_artifact(spec)['ready'])
    def test_no_check_does_not_skip_output_contract(self):
        spec=fixture();spec['checks']=[];spec['outputs']['signal']='absent'
        csv=self.path/'result.csv';csv.write_text('time,signal\n0,0\n1,0.6\n')
        result=summarize_execution(spec,dict(compile=dict(status='PASS'),simulation=dict(status='PASS')),csv)
        self.assertEqual(result['chain_status'],'OUTPUT_CONTRACT_MISMATCH');self.assertEqual(result['task_status'],'UNKNOWN')
    def test_task_failure_different_from_tool_failure(self):
        csv=self.path/'result.csv';csv.write_text('time,signal\n0,0\n1,0\n')
        result=summarize_execution(fixture(),dict(compile=dict(status='PASS'),simulation=dict(status='PASS')),csv)
        self.assertEqual((result['tool_status'],result['chain_status'],result['task_status']),('PASS','PASS','FAIL'))
        result=summarize_execution(fixture(),dict(compile=dict(status='FAIL'),simulation=dict(status='UNVERIFIED')),csv)
        self.assertEqual((result['tool_status'],result['chain_status'],result['task_status']),('TOOL_FAIL','BLOCKED_TOOL','UNKNOWN'))
    def test_no_independent_criteria_stays_unknown(self):
        spec=fixture();spec['checks']=[];csv=self.path/'result.csv';csv.write_text('time,signal\n0,0\n1,.6\n')
        result=summarize_execution(spec,dict(compile=dict(status='PASS'),simulation=dict(status='PASS')),csv)
        self.assertEqual(result['task_status'],'UNKNOWN');self.assertEqual(result['vehicle_task_status'],'UNKNOWN')
    def test_unconfirmed_and_changed_digest_rejected(self):
        service,spec=self.service();proposal=service.proposal_prepare(spec)['proposal']
        with self.assertRaisesRegex(ValueError,'not approved'):service.execution_start(proposal['proposal_id'],'test_unconfirmed')
        service.confirm_local(proposal['proposal_id'],proposal['displayed_digest'])
        spec['artifact']['modelica_source']=SOURCE.replace('0.6','0.7')
        with service.store.transaction() as db:db.execute('UPDATE proposals SET spec_json=? WHERE proposal_id=?',(json.dumps(spec),proposal['proposal_id']))
        with self.assertRaisesRegex(ValueError,'changed'):service.execution_start(proposal['proposal_id'],'test_changed')
    def test_actual_confirmation_rejects_wrong_display(self):
        service,spec=self.service();proposal=service.proposal_prepare(spec)['proposal']
        with self.assertRaises(ValueError):service.confirm_local(proposal['proposal_id'],'0'*64)
        self.assertEqual(service.store.proposal(proposal['proposal_id'])['state'],'awaiting_confirmation')
        service.confirm_local(proposal['proposal_id'],proposal['displayed_digest'])
        confirmation=service.store.proposal(proposal['proposal_id'])['confirmation']
        self.assertEqual(confirmation['channel'],'TEST_ONLY_CLI_CONFIRMATION');self.assertFalse(confirmation['human_new_approval'])
    def test_closed_context_and_proposal_only(self):
        path=self.path/'context.json';ctx=context(path,allow=False)
        service=ArtifactService(self.path/'db.sqlite','test',context=ctx,worker_manager=object());service.project_create('TEST_ONLY_protocol');source=service.record_source(SOURCE_TEXT)
        proposal=service.proposal_prepare(fixture(source['source_id']))['proposal']
        with self.assertRaisesRegex(ValueError,'proposal only'):service.execution_start(proposal['proposal_id'],'no_execution')
        value=json.loads(path.read_bytes());value['status']='CLOSED';save(path,value)
        with self.assertRaisesRegex(ValueError,'closed'):ExecutionContext.load(path)
    def test_cancel_cannot_become_success(self):
        service,spec=self.service();proposal=service.proposal_prepare(spec)['proposal'];service.confirm_local(proposal['proposal_id'],proposal['displayed_digest'])
        task,_=service.store.create_task(proposal['proposal_id'],'test','cancel_test');service.store.reserve_launch(task['task_id'],'owner_test');service.store.claim_task(task['task_id'],'owner_test',1);service.store.request_cancel(task['task_id'])
        with self.assertRaisesRegex(ValueError,'cancellation'):service.store.finish_task(task['task_id'],'succeeded',owner='owner_test',result=dict(chain_status='PASS',worker_exit_code=0))

if __name__=='__main__':unittest.main()
