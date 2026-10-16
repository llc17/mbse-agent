"""TEST_ONLY synthetic baseline/software tests. No API/OMC/worker process."""
import asyncio,json,unittest,tempfile,copy
from pathlib import Path
from unittest.mock import patch
from mcp.types import CallToolResult,TextContent,ListToolsResult
from week11.src.mcp_server import create_mcp
from week12.src.car_domain.artifact_service import ArtifactService
from week12.src.car_domain.artifact_baseline import reference,verify_proposal
from week12.src.car_domain.artifact_cli import ArtifactCLI
from week12.src.car_domain.model_artifact import save,sha,summarize_execution,ExecutionContext
from week12.tests.test_model_artifact import context,fixture,SOURCE_TEXT
OUT=Path(__file__).resolve().parents[1]/'results/s4-artifact-modify-20261012-01'
FACTS=[]
class CountOnly:
    def __init__(self):self.starts=[]
    def start(self,tid):self.starts.append(tid)
class ModifyChecks(unittest.TestCase):
    def setUp(self):
        OUT.mkdir(exist_ok=True);self.root=Path(tempfile.mkdtemp(prefix='TEST_ONLY_',dir=OUT));self.patch=patch('week12.src.car_domain.artifact_history.WEEK12_ROOT',self.root);self.patch.start()
        self.ctx=context(self.root/'context.json',allow=False);self.manager=CountOnly()
        self.service=ArtifactService(self.root/'store.sqlite','modify_test',context=self.ctx,worker_manager=self.manager)
        self.project=self.service.project_create('TEST_ONLY_modify')['project'];self.source=self.service.record_source(SOURCE_TEXT)
        self.left=self.record(fixture(self.source['source_id']),1)
        self.base=self.root/'projects'/self.project['project_id']/'versions'/self.left['version_id']
        self.old_hashes={p.name:sha(p.read_bytes()) for p in self.base.iterdir()}
    def tearDown(self):self.patch.stop() # Retain all isolated fixture facts.
    def record(self,spec,index):
        p=self.service.proposal_prepare(spec)['proposal'];self.service.confirm_local(p['proposal_id'],p['displayed_digest'])
        p=self.service.store.proposal(p['proposal_id']);t,_=self.service.store.create_task(p['proposal_id'],'modify_test','record_'+str(index));owner='test_owner_'+str(index)
        self.service.store.reserve_launch(t['task_id'],owner);self.service.store.claim_task(t['task_id'],owner,1)
        base=self.root/'projects'/t['project_id']/'versions'/t['version_id'];base.mkdir(parents=True)
        save(base/'spec.json',spec);save(base/'confirmation.json',p['confirmation']);(base/'frozen-model.mo').write_text(spec['artifact']['modelica_source'],encoding='utf8',newline='\n')
        if p['operation']=='modify':save(base/'baseline-binding.json',spec['baseline_ref'])
        (base/'fake.csv').write_text('time,signal\n0,0\n1,0\n',encoding='utf8')
        result=summarize_execution(spec,dict(compile=dict(status='PASS'),simulation=dict(status='PASS'),test_only=True,toolchain_invocations=0),base/'fake.csv');result.update(test_only=True,toolchain_invocations=0)
        save(base/'outcome.json',result);files={f.name:dict(sha256=sha(f.read_bytes()),bytes=f.stat().st_size) for f in base.iterdir()}
        binding=dict(task_id=t['task_id'],project_id=t['project_id'],spec_digest=p['spec_digest'],source_sha256=p['display']['source_sha256'],owner=owner,context_id=p['display']['execution_context_id'],context_sha256=p['display']['execution_context_sha256'])
        if p['operation']=='modify':binding['artifact_baseline']=spec['baseline_ref']
        save(base/'artifact-manifest.json',dict(schema='v11-artifact-manifest-1',binding=binding,outcome=result,files=files))
        self.service.store.finish_task(t['task_id'],'succeeded',owner=owner,result={**result,'worker_exit_code':0,'worker_pid':1,'outcome_manifest_path':str(base/'artifact-manifest.json'),'outcome_manifest_sha256':sha((base/'artifact-manifest.json').read_bytes())})
        return self.service.store.task(t['task_id'])
    def selected_spec(self):
        selected=self.service.versions_select(self.left['version_id'])
        spec=fixture(self.source['source_id']);spec['artifact']['modelica_source']=spec['artifact']['modelica_source'].replace('0.6','0.7');spec['design']['parameters'][0]['value']=.7
        spec['baseline_ref']=selected['artifact_baseline'];return spec
    def test_create_preserved_and_goal_fail_baseline_can_modify(self):
        self.assertEqual(self.left['result']['task_status'],'FAIL')
        self.assertEqual(self.service.store.proposal(self.left['proposal_id'])['operation'],'create')
        spec=self.selected_spec();p=self.service.proposal_prepare(spec)['proposal'];self.assertEqual(p['operation'],'modify');self.assertIsNone(p['baseline_version_id'])
        self.service.confirm_local(p['proposal_id'],p['displayed_digest']);p=self.service.store.proposal(p['proposal_id'])
        self.assertEqual(p['confirmation']['artifact_baseline'],spec['baseline_ref']);self.assertFalse(p['confirmation']['human_new_approval'])
        verify_proposal(self.service,p,require_selection=True,require_confirmation=True)
        self.assertIsNone(self.service._current_project()['current_version_id']);FACTS.append(dict(case='create_and_unpublished_taskFAIL_baseline',status='PASS',test_only=True))
    def test_wrong_and_unselected_baselines_rejected(self):
        with self.assertRaises(ValueError):self.service.versions_select('v999999')
        spec=self.selected_spec();wrong=copy.deepcopy(spec);wrong['baseline_ref']['source_sha256']='0'*64
        with self.assertRaisesRegex(ValueError,'different artifact baseline'):self.service.proposal_prepare(wrong)
        self.service.versions_select('none')
        with self.assertRaisesRegex(ValueError,'explicitly selected'):self.service.proposal_prepare(spec)
        self.service.project_create('TEST_ONLY_other')
        with self.assertRaises(ValueError):self.service.versions_select(self.left['version_id'])
    def test_selection_change_invalidates_confirmation_execution(self):
        cfg=json.loads(self.ctx.path.read_bytes());cfg.update(context_id='stale_selection_test',allow_execution=True);save(self.root/'stale-context.json',cfg)
        self.service=ArtifactService(self.service.store.db_path,'modify_test',context=ExecutionContext.load(self.root/'stale-context.json'),worker_manager=self.manager)
        spec=self.selected_spec();p=self.service.proposal_prepare(spec)['proposal'];self.service.confirm_local(p['proposal_id'],p['displayed_digest'])
        self.service.versions_select('none')
        with self.assertRaises(ValueError):self.service.confirm_local(p['proposal_id'],p['displayed_digest'])
        with self.assertRaisesRegex(ValueError,'selected artifact baseline changed'):self.service.execution_start(p['proposal_id'],'stale')
        self.assertFalse(self.manager.starts)
        created=self.service.proposal_prepare(fixture(self.source['source_id']))['proposal'];self.assertEqual(created['operation'],'create')
    def test_cli_full_context_without_model_selection_authority(self):
        self.selected_spec();service=self.service
        class Session:
            async def initialize(self):return None
            async def list_tools(self):return ListToolsResult(tools=await create_mcp(service).list_tools())
            async def call_tool(self,name,arguments):
                result=getattr(service,name)(**arguments)
                return CallToolResult(content=[TextContent(type='text',text=json.dumps(result))],structuredContent=result)
        async def check():
            cli=ArtifactCLI(Session(),service.store,'modify_test',None,self.ctx);await cli.initialize()
            messages,tools=await cli.build_request_for_text('TEST_ONLY change gain to 0.7')
            data=json.loads(messages[1]['content']);base=data['selected_artifact_baseline']
            self.assertEqual(base['spec']['artifact']['modelica_source'],fixture(self.source['source_id'])['artifact']['modelica_source']);self.assertEqual(base['reference']['task_id'],self.left['task_id']);self.assertFalse(base['published'])
            self.assertEqual(base['spec']['sources'][0]['id'],self.source['source_id']);self.assertNotIn(self.source['source_id'],[s['id'] for s in data['trusted_sources']]);self.assertTrue(any(s['text']=='TEST_ONLY change gain to 0.7' for s in data['trusted_sources']))
            self.assertNotIn('versions_select',[t['function']['name'] for t in tools]);self.assertIn('proposal_prepare',[t['function']['name'] for t in tools]);self.assertNotIn('outcome',base)
        asyncio.run(check());FACTS.append(dict(case='normal_cli_full_baseline_context',status='PASS',provider='NOT_CALLED',test_only=True))
    def test_modified_manifest_binding_compare_and_old_files_unchanged(self):
        spec=self.selected_spec();right=self.record(spec,2)
        reader=ArtifactService(self.service.store.db_path,'modify_test',read_only=True)
        compared=reader.versions_compare(self.left['version_id'],right['version_id'])['comparison'];self.assertFalse(compared['differences']['source']['same']);self.assertEqual(compared['combined_vehicle_task_status'],'UNKNOWN')
        self.assertEqual(self.old_hashes,{p.name:sha(p.read_bytes()) for p in self.base.iterdir()})
        p=self.service.store.proposal(right['proposal_id']);p['confirmation']['artifact_baseline']={**p['spec']['baseline_ref'],'source_sha256':'0'*64}
        with self.assertRaisesRegex(ValueError,'confirmation'):verify_proposal(self.service,p,require_confirmation=True)
        self.assertEqual(reader.evidence_query('task:'+right['task_id'])['evidence']['evidence_status'],'COMPLETE')
    def test_new_context_budget_excludes_imported_task_and_covers_projects(self):
        cfg=json.loads(self.ctx.path.read_bytes());cfg.update(context_id='new_context_budget',allow_execution=True,max_tasks=1);save(self.root/'new-context.json',cfg)
        self.service=ArtifactService(self.service.store.db_path,'modify_test',context=ExecutionContext.load(self.root/'new-context.json'),worker_manager=self.manager)
        spec=self.selected_spec();p=self.service.proposal_prepare(spec)['proposal'];self.service.confirm_local(p['proposal_id'],p['displayed_digest']);first=self.service.execution_start(p['proposal_id'],'new_scope_one')
        self.assertEqual(first['task']['version_id'],'v000002');self.assertEqual(len(self.manager.starts),1)
        self.service.project_create('TEST_ONLY_budget_other');src=self.service.record_source(SOURCE_TEXT);p=self.service.proposal_prepare(fixture(src['source_id']))['proposal'];self.service.confirm_local(p['proposal_id'],p['displayed_digest'])
        with self.assertRaisesRegex(ValueError,'finite artifact task cap'):self.service.execution_start(p['proposal_id'],'new_scope_two')
        self.assertEqual(len(self.manager.starts),1);FACTS.append(dict(case='context_budget_cross_project',status='PASS',process_launches=0,count_only_launch_calls=1))
if __name__=='__main__':unittest.main()
