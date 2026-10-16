"""Current dual-source checks; generated test artifacts are never product designs."""
from copy import deepcopy
from pathlib import Path
import contextlib,io,json,time,unittest,uuid
from unittest.mock import patch
from week12.src.car_domain.model_artifact import validate_artifact,save,ExecutionContext,sha
from week12.src.car_domain.artifact_sysml import inspect_dual_source
from week12.src.car_domain.artifact_service import ArtifactService
from week12.src.car_domain.artifact_cli import ArtifactCLI
from week12.src.car_domain.artifact_history import snapshot

OUT=Path(__file__).resolve().parents[1]/'results/s5-sysml-integration-20261013-01'
TEXT='TEST_ONLY equation interface; final signal >= 0.5 at 1 second.'
SOURCE='model DualFixture\n parameter Real level=0.6;\n Real signal(start=0,fixed=true);\nequation\n der(signal)=level;\nend DualFixture;\n'

def sysml_for(spec):
    requirements=[]
    for i,item in enumerate(spec['requirements'],1):
        values=dict(item,model_name=spec['artifact']['model_name'])
        requirements.append('requirement req_'+str(i)+' { '+''.join('attribute '+key+' : String = '+json.dumps(value,ensure_ascii=False)+'; ' for key,value in values.items())+'}')
    return 'package DualTest { part def System { attribute level : Real = 0.6; } part system : System; '+' '.join(requirements)+' }'

def fixture(source_id='src_fixture'):
    spec=dict(schema_version='v11-model-artifact-1',backend='model-artifact',artifact=dict(model_name='DualFixture',modelica_source=SOURCE),sources=[dict(id=source_id,text=TEXT)],requirements=[dict(text='TEST_ONLY interface contract',source_id=source_id,source_text=TEXT)],design=dict(summary='TEST_ONLY signal equation, no vehicle',assumptions=[],parameters=[dict(name='level',value=.6,unit='1/s',origin='model',basis='TEST_ONLY fixture')]),libraries=[],settings=dict(stop_time=1,intervals=20),outputs=dict(signal='signal'),checks=[dict(kind='final_ge',output='signal',threshold=.5,source_id=source_id,source_text=TEXT)])
    spec['artifact']['sysml_source']=sysml_for(spec);return spec

def service_fixture(mode='TEST_ONLY',label='unit'):
    folder=OUT/(label+'-'+uuid.uuid4().hex);folder.mkdir(parents=True)
    save(folder/'context.json',dict(schema='v11-artifact-execution-context-1',context_id=folder.name,status='ACTIVE',mode=mode,allow_execution=True,max_tasks=1,worker_timeout_s=300,simulation_timeout_s=120))
    ctx=ExecutionContext.load(folder/'context.json');service=ArtifactService(folder/'projects.sqlite','sysml_test',context=ctx)
    project=service.project_create('TEST_ONLY_dual_'+label)['project'];source=service.record_source(TEXT)
    return service,fixture(source['source_id']),folder

def run_fixture(service,spec,request='sysml_smoke'):
    prepared=service.proposal_prepare(spec)
    if not prepared.get('ok'):raise AssertionError(prepared)
    proposal=prepared['proposal'];service.confirm_local(proposal['proposal_id'],proposal['displayed_digest'])
    task=service.execution_start(proposal['proposal_id'],request)['task'];deadline=time.monotonic()+305
    while time.monotonic()<deadline:
        task=service.store.task(task['task_id'])
        if task['state'] in {'failed','succeeded','uncertain','cancelled'}:break
        time.sleep(.1)
    else:raise AssertionError('bounded Worker failed to finish')
    return task,service.execution_status(task['task_id'])['snapshot']

class SysMLChecks(unittest.TestCase):
    def test_real_parser_and_exact_contract(self):
        checked=validate_artifact(fixture(),require_sysml=True);self.assertTrue(checked['ready'],checked)
        syntax,result=inspect_dual_source(fixture());self.assertEqual(syntax['tool_version'],'0.34.1');self.assertEqual(result['status'],'PASS');self.assertEqual(result['equation_and_structure_semantic_equivalence'],'UNKNOWN')
    def test_missing_historical_compatibility_and_empty_package(self):
        for value in [None,'','package Empty {}','package Test { part def System {} part system : System; }']:
            spec=fixture()
            if value is None:spec['artifact'].pop('sysml_source')
            else:spec['artifact']['sysml_source']=value
            self.assertFalse(validate_artifact(spec,require_sysml=True)['ready'])
        old=fixture();old['artifact'].pop('sysml_source');self.assertTrue(validate_artifact(old)['ready'])
    def test_bad_syntax_parameter_and_requirement_mismatch(self):
        valid=fixture()['artifact']['sysml_source']
        for source in [valid.replace('Real = 0.6','Real = ;'),valid.replace('Real = 0.6','Real = 0.7'),valid.replace('TEST_ONLY interface contract','fake task'),valid.replace('src_fixture','invented_source'),valid.replace('DualFixture','WrongModel'),valid.replace('part system : System;','part system : Other;')]:
            spec=fixture();spec['artifact']['sysml_source']=source;self.assertFalse(validate_artifact(spec,require_sysml=True)['ready'])
    def test_comments_strings_duplicates_and_unsupported_mapping(self):
        valid=fixture()
        variants=[]
        spec=deepcopy(valid);spec['artifact']['sysml_source']=spec['artifact']['sysml_source'].replace('attribute level : Real = 0.6;','/* attribute level : Real = 0.6; */');variants.append(spec)
        spec=deepcopy(valid);spec['artifact']['sysml_source']=spec['artifact']['sysml_source'].replace('attribute level : Real = 0.6;','attribute level : Real = 0.6; attribute level : Real = 0.6;');variants.append(spec)
        spec=deepcopy(valid);spec['artifact']['modelica_source']=SOURCE.replace('parameter Real level=0.6;','parameter Real level=0.6; parameter Real level=0.6;');variants.append(spec)
        spec=deepcopy(valid);spec['artifact']['modelica_source']=SOURCE.replace('parameter Real level=0.6;','parameter Real level=0.6; parameter Real extra=level*2;');variants.append(spec)
        spec=deepcopy(valid);spec['artifact']['sysml_source']=spec['artifact']['sysml_source'].replace('requirement req_1 {','/* requirement req_1 {} */ requirement req_2 {');variants.append(spec)
        for spec in variants:self.assertFalse(validate_artifact(spec,require_sysml=True)['ready'])
    def test_new_service_and_legacy_pending_cannot_bypass(self):
        service,spec,folder=service_fixture(label='guard');old=deepcopy(spec);old['artifact'].pop('sysml_source')
        self.assertFalse(service.proposal_prepare(old)['ok'])
        proposal=service.proposal_prepare(spec)['proposal']
        # Simulate an old, internally coherent pending receipt, without changing
        # any real historical run. Even this formerly optional source cannot run.
        from week11.src.store import digest_json,canonical_json
        display=deepcopy(proposal['display']);display['artifact']=old['artifact'];display['spec_digest']=digest_json(old)
        with service.store.transaction() as db:db.execute('UPDATE proposals SET spec_json=?,spec_digest=?,display_json=?,displayed_digest=? WHERE proposal_id=?',(canonical_json(old),digest_json(old),canonical_json(display),digest_json(display),proposal['proposal_id']))
        with self.assertRaisesRegex(ValueError,'requires SysML'):service.confirm_local(proposal['proposal_id'],digest_json(display))
        with self.assertRaisesRegex(ValueError,'frozen artifact'):service.execution_start(proposal['proposal_id'],'legacy_pending')
    def test_worker_history_and_cli_readback(self):
        service,spec,folder=service_fixture(label='offline-chain');task,record=run_fixture(service,spec)
        self.assertEqual(task['state'],'succeeded',task);self.assertEqual(record['dual_model_status']['sysml'],'PASS');self.assertEqual(record['dual_model_status']['consistency'],'PASS')
        self.assertTrue(Path(record['source_files']['SysML']).is_file());self.assertTrue(Path(record['source_files']['Modelica']).is_file())
        evidence=service.evidence_query('task:'+task['task_id'])['evidence'];self.assertIn('frozen-model.sysml',evidence['manifest']['files'])
        output=io.StringIO()
        with contextlib.redirect_stdout(output):ArtifactCLI._print_snapshot(record)
        self.assertIn('SysML PASS',output.getvalue());self.assertIn('Modelica源码',output.getvalue());self.assertIn('SysML源码',output.getvalue())
        save(folder/'readback.json',dict(task=task,snapshot=record));(folder/'cli.txt').write_text(output.getvalue(),encoding='utf8')
    def test_worker_sysml_failure_preserves_manifest_and_skips_modelica(self):
        service,spec,folder=service_fixture(label='worker-failure');service.worker_manager=object()
        proposal=service.proposal_prepare(spec)['proposal'];service.confirm_local(proposal['proposal_id'],proposal['displayed_digest'])
        task,_=service.store.create_task(proposal['proposal_id'],'sysml_test','fail_direct');owner='owner_sysml_failure';service.store.reserve_launch(task['task_id'],owner)
        from week12.src.car_domain.artifact_worker import execute_task
        # Real parser rejects malformed source during the worker recheck. The
        # frozen proposal stays intact; this injection tests failure staging only.
        syntax,report=inspect_dual_source(spec);syntax['status']='FAIL';report.update(status='FAIL',issues=['TEST_ONLY injected worker parser failure'])
        with patch('week12.src.car_domain.artifact_sysml.inspect_dual_source',return_value=(syntax,report)):
            self.assertEqual(execute_task(service.store.db_path,task['task_id'],owner,service.context.path),0)
        staged=service.store.task(task['task_id'])['result'];self.assertEqual(staged['chain_status'],'BLOCKED_SYSML');self.assertEqual(staged['toolchain_invocations'],0)
        self.assertEqual(staged['modelica_status']['compile'],'NOT_RUN')
        base=Path(staged['outcome_manifest_path']).parent;self.assertTrue((base/'sysml-result.json').is_file());self.assertTrue((base/'dual-source-result.json').is_file())
        # Finish as monitor would; failure evidence remains independently queryable.
        service.store.finish_task(task['task_id'],'failed',owner=owner,result=dict(staged,worker_exit_code=0))
        self.assertEqual(service.execution_status(task['task_id'])['snapshot']['dual_model_status']['sysml'],'FAIL')

if __name__=='__main__':unittest.main()
