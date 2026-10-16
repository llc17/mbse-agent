"""V10 owned subprocess lifecycle with one Modelica source-artifact backend."""
import argparse,json,os,subprocess,sys,threading
from pathlib import Path
from week11.src.worker import WorkerManager,_verify_execution_binding
from week11.src.store import digest_json,safe_id
from .contracts import WEEK12_ROOT
from .model_artifact import ExecutionContext,validate_artifact,save,sha,summarize_execution
from .artifact_service import ArtifactStore

class ArtifactWorkerManager(WorkerManager):
    def __init__(self,store,context):
        self.context=context
        super().__init__(store,timeout_s=context.worker_timeout_s)
    def start(self,task_id):
        task=self.store.reserve_launch(task_id,self.owner)
        folder=WEEK12_ROOT/'projects'/task['project_id']/'task-logs';folder.mkdir(parents=True,exist_ok=True)
        command=[sys.executable,'-X','utf8','-B','-m','week12.src.car_domain.artifact_worker','--db',str(self.store.db_path),'--task',task_id,'--owner',self.owner,'--context',str(self.context.path)]
        options=dict(cwd=str(WEEK12_ROOT.parent),stdin=subprocess.DEVNULL)
        if os.name=='nt':options['creationflags']=subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP
        else:options['start_new_session']=True
        try:
            with (folder/(task_id+'.log')).open('ab') as log:process=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,**options)
        except OSError as exc:
            self.store.finish_task(task_id,'failed',owner=self.owner,error=str(exc));return
        with self._lock:self._processes[task_id]=process
        threading.Thread(target=self._monitor,args=(task_id,process),daemon=True).start()
    def _monitor(self,task_id,process):
        try:
            code=process.wait(timeout=self.timeout_s);task=self.store.task(task_id)
            if task['state'] in ('queued','running','cancel_requested'):
                if task['state']=='cancel_requested':
                    self.store.finish_task(task_id,'cancelled',owner=self.owner,error='accepted cancellation preserved after worker exit')
                    return
                result=task.get('result')
                if code==0 and isinstance(result,dict):
                    # Validate frozen artifacts before trusting the staged outcome.
                    from .service_adapter import _verify_manifest
                    base=WEEK12_ROOT/'projects'/task['project_id']/'versions'/task['version_id']
                    manifest=_verify_manifest(Path(result['outcome_manifest_path']),result['outcome_manifest_sha256'],base)
                    if manifest['binding']['task_id']!=task_id or manifest['binding']['owner']!=self.owner:raise ValueError('worker result owner mismatch')
                    result={**result,'worker_exit_code':code,'worker_pid':task['worker_pid']}
                    self.store.finish_task(task_id,'succeeded' if result['chain_status']=='PASS' else 'failed',owner=self.owner,result=result)
                else:self.store.finish_task(task_id,'uncertain',owner=self.owner,error='worker exited without valid staged evidence: '+str(code))
        except subprocess.TimeoutExpired:
            self.store.request_cancel(task_id);cleaned=self._terminate_owned(task_id,process)
            self.store.finish_task(task_id,'failed' if cleaned else 'uncertain',owner=self.owner,error='owned artifact worker timeout; cleanup='+str(cleaned))
        except (OSError,ValueError,KeyError) as exc:
            self.store.finish_task(task_id,'failed',owner=self.owner,error='artifact evidence rejected: '+str(exc))
        finally:
            with self._lock:self._processes.pop(task_id,None)

def execute_task(db_path,task_id,owner,context_path):
    context=ExecutionContext.load(context_path);store=ArtifactStore(db_path)
    task=store.claim_task(task_id,owner,os.getpid())
    base=WEEK12_ROOT/'projects'/task['project_id']/'versions'/task['version_id']
    try:
        proposal=store.proposal(task['proposal_id']);project=store.project(task['project_id'])
        _verify_execution_binding(task,proposal,project)
        context.assert_current()
        if not context.allow_execution or proposal['display']['execution_context_sha256']!=context.digest:raise ValueError('unapproved or changed caller execution context')
        checked=validate_artifact(proposal['spec'])
        if not checked['ready'] or checked['spec_digest']!=proposal['spec_digest']:raise ValueError('source changed after confirmation')
        from .artifact_service import ArtifactService
        ArtifactService(db_path,task['session_id'],context=context,worker_manager=object())._verify_sources(task['project_id'],proposal['spec'])
        service=ArtifactService(db_path,task['session_id'],context=context,worker_manager=object())
        from .artifact_baseline import verify_proposal
        baseline=verify_proposal(service,proposal,require_selection=True,require_confirmation=True)
        base.mkdir(parents=True,exist_ok=False);spec=checked['spec']
        save(base/'spec.json',spec);save(base/'confirmation.json',proposal['confirmation'])
        if baseline:save(base/'baseline-binding.json',spec['baseline_ref'])
        (base/'frozen-model.mo').write_text(spec['artifact']['modelica_source'],encoding='utf8',newline='\n')
        if sha((base/'frozen-model.mo').read_bytes())!=proposal['display']['source_sha256']:raise ValueError('frozen source bytes mismatch')
        from .artifact_sysml import inspect_dual_source
        sysml=spec['artifact'].get('sysml_source','')
        (base/'frozen-model.sysml').write_text(sysml,encoding='utf8',newline='\n')
        if sha((base/'frozen-model.sysml').read_bytes())!=sha(sysml.encode()):raise ValueError('frozen SysML source bytes mismatch')
        syntax,consistency=inspect_dual_source(spec)
        save(base/'sysml-result.json',syntax);save(base/'dual-source-result.json',consistency)
        dual_ok=syntax.get('status')=='PASS' and consistency['status']=='PASS'
        if not dual_ok:
            tool=dict(compile=dict(status='NOT_RUN'),simulation=dict(status='NOT_RUN'),toolchain_invocations=0)
            csv_path=None
        elif context.mode=='TEST_ONLY':
            # Explicit injection for failure semantics. No tool or simulation ran.
            folder=base/'tool';folder.mkdir();csv_path=folder/'model_res.csv'
            csv_path.write_text('time,signal\n0,0\n1,0\n',encoding='utf8')
            status='FAIL' if context.test_tool_failure else 'PASS'
            tool=dict(compile=dict(status=status),simulation=dict(status=status),test_only=True,toolchain_invocations=0)
        else:
            from week12.src.toolchain import execute_model
            tool=execute_model(spec['artifact']['modelica_source'],spec['artifact']['model_name'],base/'tool',spec['settings']['stop_time'],spec['settings']['intervals'],simulation_timeout_s=context.simulation_timeout_s)
            if tool.get('source_sha256')!=checked['source_sha256']:raise ValueError('toolchain actual source hash mismatch')
            csv_path=base/'tool/model_res.csv'
        result=summarize_execution(spec,tool,csv_path)
        if not dual_ok:result.update(chain_status='BLOCKED_SYSML',tool_status='NOT_RUN')
        result['dual_source_contract']='v11-dual-source-check-1'
        result['sysml_status']=syntax.get('status','UNKNOWN');result['consistency_status']=consistency['status']
        result['modelica_status']=dict(check=tool.get('compile',{}).get('check_model','NOT_RUN' if not dual_ok else 'TEST_ONLY'),compile=tool.get('compile',{}).get('status','UNKNOWN'),simulation=tool.get('simulation',{}).get('status','UNKNOWN'))
        result['dual_source_issues']=consistency['issues']
        result['sysml_contract_check_invocations']=1
        result['execution_mode']=context.mode;result['test_only']=context.mode.startswith('TEST_ONLY')
        result['toolchain_invocations']=0 if context.mode=='TEST_ONLY' or not dual_ok else 1
        from .artifact_delivery import create_delivery
        delivery_binding=dict(task_id=task_id,project_id=task['project_id'],version_id=task['version_id'],spec_digest=proposal['spec_digest'],source_sha256=checked['source_sha256'])
        result['result_delivery']=create_delivery(spec,csv_path,base,delivery_binding,result)
        save(base/'outcome.json',result)
        files={p.relative_to(base).as_posix():dict(sha256=sha(p.read_bytes()),bytes=p.stat().st_size) for p in base.rglob('*') if p.is_file()}
        path=base/'artifact-manifest.json'
        binding=dict(task_id=task_id,project_id=task['project_id'],spec_digest=proposal['spec_digest'],source_sha256=checked['source_sha256'],owner=owner,context_id=context.context_id,context_sha256=context.digest)
        if baseline:binding['artifact_baseline']=spec['baseline_ref']
        save(path,dict(schema='v11-artifact-manifest-1',binding=binding,outcome=result,files=files))
        store.stage_artifact(task_id,owner,{**result,'outcome_manifest_path':str(path),'outcome_manifest_sha256':sha(path.read_bytes())})
        return 0
    except BaseException as exc:
        store.finish_task(task_id,'failed',owner=owner,result=dict(tool_status='TOOL_FAIL',chain_status='BLOCKED_TOOL',task_status='UNKNOWN',vehicle_task_status='UNKNOWN',test_only=context.mode.startswith('TEST_ONLY')),error=type(exc).__name__+': '+str(exc))
        return 1

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--db',required=True);parser.add_argument('--task',required=True);parser.add_argument('--owner',required=True);parser.add_argument('--context',required=True)
    args=parser.parse_args();raise SystemExit(execute_task(args.db,args.task,args.owner,args.context))
