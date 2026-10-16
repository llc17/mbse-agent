"""TEST_ONLY lifecycle facts; no real Modelica or provider calls."""
from pathlib import Path
import json,unittest,subprocess
from unittest.mock import patch
from week12.src.car_domain.artifact_service import ArtifactService
from week12.src.car_domain.artifact_worker import ArtifactWorkerManager
from week12.src.car_domain.model_artifact import ExecutionContext,save
from week11.src.store import canonical_json

OUT=Path(__file__).resolve().parents[1]/'results/s4-artifact-lifecycle-20261011-01'
TEXT='TEST_ONLY lifecycle interface fixture: at 1 second require final signal >= 0.5; not a vehicle or a human physical approval.'
FACTS=[]
def make_context(path):
    save(path,dict(schema='v11-artifact-execution-context-1',context_id='lifecycle_test',status='ACTIVE',mode='TEST_ONLY',allow_execution=True,max_tasks=8,worker_timeout_s=30,simulation_timeout_s=10))
    return ExecutionContext.load(path)
def spec(source,gain=.6):
    return dict(schema_version='v11-model-artifact-1',backend='model-artifact',artifact=dict(model_name='LifecycleFixture',modelica_source=f'model LifecycleFixture\n parameter Real gain = {gain};\n Real signal(start=0,fixed=true);\nequation\n der(signal)=gain;\nend LifecycleFixture;\n'),sources=[dict(id=source['source_id'],text=source['text'])],requirements=[dict(text='TEST_ONLY lifecycle process check',source_id=source['source_id'],source_text=source['text'])],design=dict(summary='TEST_ONLY software process fixture, not product vehicle design',assumptions=[dict(text='TEST_ONLY no real OMC execution',origin='model')],parameters=[dict(name='gain',value=gain,unit='1/s',origin='model',basis='TEST_ONLY fixture difference')]),libraries=[],settings=dict(stop_time=1,intervals=20),outputs=dict(signal='signal'),checks=[dict(kind='final_ge',output='signal',threshold=.5,source_id=source['source_id'],source_text=source['text'])])
def make_service(folder,manager=None):
    folder.mkdir(parents=True,exist_ok=False);ctx=make_context(folder/'context.json');service=ArtifactService(folder/'store.sqlite','lifecycle',context=ctx,worker_manager=manager or object());service.project_create('TEST_ONLY_lifecycle');source=service.record_source(TEXT)
    return service,source,ctx
def proposal(service,source,gain=.6):
    p=service.proposal_prepare(spec(source,gain))['proposal'];service.confirm_local(p['proposal_id'],p['displayed_digest']);return service.store.proposal(p['proposal_id'])
def reserved(service,source,manager,index='one',running=False):
    p=proposal(service,source);t,_=service.store.create_task(p['proposal_id'],'lifecycle','request_'+index);service.store.reserve_launch(t['task_id'],manager.owner)
    if running:service.store.claim_task(t['task_id'],manager.owner,1)
    return service.store.task(t['task_id'])
class CountOnlyManager:
    def __init__(self):self.launches=[]
    def start(self,task_id):self.launches.append(task_id)
class ProcessDouble:
    pid=1
    def __init__(self,code=0,timeout=False):self.code=code;self.timeout=timeout
    def wait(self,timeout=None):
        if self.timeout:raise subprocess.TimeoutExpired('TEST_ONLY_PROCESS_DOUBLE',timeout)
        return self.code
    def poll(self):return self.code

class LifecycleChecks(unittest.TestCase):
    def setUp(self):
        self.folder=OUT/'software-fixtures'/self._testMethodName;self.folder.parent.mkdir(parents=True,exist_ok=True)
    def test_duplicate_request_and_different_spec_reuse(self):
        manager=CountOnlyManager();service,source,ctx=make_service(self.folder,manager);p=proposal(service,source)
        first=service.execution_start(p['proposal_id'],'same_request');repeat=service.execution_start(p['proposal_id'],'same_request')
        self.assertTrue(repeat['reused']);self.assertEqual(first['task']['task_id'],repeat['task']['task_id']);self.assertEqual(len(manager.launches),1)
        changed=proposal(service,source,.7)
        with self.assertRaisesRegex(ValueError,'different content'):service.execution_start(changed['proposal_id'],'same_request')
        self.assertEqual(len(manager.launches),1);FACTS.append(dict(case='duplicate_and_spec_mismatch',test_only=True,process_kind='NO_PROCESS_COUNT_ONLY_DOUBLE',launch_invocations=1))
    def test_confirmation_and_changed_source_refuse_before_launch(self):
        manager=CountOnlyManager();service,source,ctx=make_service(self.folder,manager);p=service.proposal_prepare(spec(source))['proposal']
        with self.assertRaisesRegex(ValueError,'not approved'):service.execution_start(p['proposal_id'],'no_confirmation')
        service.confirm_local(p['proposal_id'],p['displayed_digest']);changed=spec(source,.7)
        with service.store.transaction() as db:db.execute('UPDATE proposals SET spec_json=? WHERE proposal_id=?',(canonical_json(changed),p['proposal_id']))
        with self.assertRaisesRegex(ValueError,'changed'):service.execution_start(p['proposal_id'],'changed')
        self.assertEqual(manager.launches,[]);FACTS.append(dict(case='confirmation_source_binding',test_only=True,launch_invocations=0))
    def test_wrong_owner_reclaim_and_cancelled_success_refused(self):
        service,source,ctx=make_service(self.folder);manager=ArtifactWorkerManager(service.store,ctx);t=reserved(service,source,manager,running=True)
        with self.assertRaisesRegex(ValueError,'owner'):service.store.finish_task(t['task_id'],'failed',owner='wrong_owner')
        with self.assertRaisesRegex(ValueError,'owner'):service.store.claim_task(t['task_id'],manager.owner,2)
        service.store.request_cancel(t['task_id'])
        with self.assertRaisesRegex(ValueError,'cancellation'):service.store.finish_task(t['task_id'],'succeeded',owner=manager.owner,result=dict(chain_status='PASS',worker_exit_code=0))
        service.store.finish_task(t['task_id'],'cancelled',owner=manager.owner)
        with self.assertRaisesRegex(ValueError,'immutable'):service.store.finish_task(t['task_id'],'succeeded',owner=manager.owner,result=dict(chain_status='PASS',worker_exit_code=0))
        FACTS.append(dict(case='owner_reclaim_and_cancel_precedence',test_only=True,actual_process_launches=0))
    def test_queued_and_running_cancel_request_precedes_exit0(self):
        states=[]
        for running in (False,True):
            service,source,ctx=make_service(self.folder/str(running));manager=ArtifactWorkerManager(service.store,ctx);t=reserved(service,source,manager,running=running)
            service.store.request_cancel(t['task_id']);process=ProcessDouble();manager._processes[t['task_id']]=process;manager._monitor(t['task_id'],process)
            states.append(service.store.task(t['task_id'])['state'])
        self.assertEqual(states,['cancelled','cancelled']);FACTS.append(dict(case='queued_running_cancel_before_exit0',process_kind='TEST_ONLY_PROCESS_DOUBLE',states=states,actual_process_launches=0))
    def test_exit_without_evidence_nonzero_and_timeout_never_pass(self):
        outcomes=[]
        for name,code,timeout,cleaned in [('no_evidence',0,False,True),('nonzero',7,False,True),('timeout_clean',0,True,True),('timeout_unknown',0,True,False)]:
            service,source,ctx=make_service(self.folder/name);manager=ArtifactWorkerManager(service.store,ctx);t=reserved(service,source,manager,running=True);process=ProcessDouble(code,timeout);manager._processes[t['task_id']]=process
            with patch.object(manager,'_terminate_owned',return_value=cleaned):manager._monitor(t['task_id'],process)
            ended=service.store.task(t['task_id']);self.assertNotEqual(ended['state'],'succeeded');outcomes.append(dict(case=name,state=ended['state'],error=ended['error']))
        self.assertEqual([o['state'] for o in outcomes],['uncertain','uncertain','failed','uncertain']);FACTS.append(dict(case='exit_timeout_gate',process_kind='TEST_ONLY_PROCESS_DOUBLE',actual_process_launches=0,outcomes=outcomes))
    def test_unknown_owner_recovery_blocks_and_other_manager_does_not_kill(self):
        service,source,ctx=make_service(self.folder);old=ArtifactWorkerManager(service.store,ctx);t=reserved(service,source,old,running=True);new=ArtifactWorkerManager(service.store,ctx)
        with patch.object(new,'_terminate_owned',side_effect=AssertionError('foreign process kill')):
            ended=new.cancel(t['task_id'])
        self.assertEqual(ended['state'],'uncertain');p=proposal(service,source,.7)
        with self.assertRaisesRegex(ValueError,'active'):service.execution_start(p['proposal_id'],'blocked_by_uncertain')
        # Explicit dead old-owned recovery, no manager process ownership assumed.
        with service.store.transaction() as db:db.execute("UPDATE tasks SET state='running' WHERE task_id=?",(t['task_id'],))
        with patch('week12.src.car_domain.store_adapter._pid_alive',return_value=False):affected=service.store.recover_orphans()
        self.assertIn(t['task_id'],affected);self.assertEqual(service.store.task(t['task_id'])['state'],'uncertain')
        with self.assertRaisesRegex(ValueError,'owner'):service.store.claim_task(t['task_id'],new.owner,2)
        FACTS.append(dict(case='unknown_owner_recovery',test_only=True,state='uncertain',blocked_new_execution=True,foreign_cleanup_attempted=False,actual_process_launches=0))

if __name__=='__main__':unittest.main()
