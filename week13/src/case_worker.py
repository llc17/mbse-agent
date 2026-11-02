"""V10 owned subprocess lifecycle with one Modelica source-artifact backend."""
import argparse,json,os,subprocess,sys,threading
from pathlib import Path
from .case_paths import REPO,remaining,require_time,process_attempt,mark_process_launched,finish_process,save_json,utc_now
from week11.src.worker import WorkerManager,_verify_execution_binding
from week11.src.store import digest_json,safe_id
from .inherited.contracts import WEEK12_ROOT
from .inherited.model_artifact import ExecutionContext,validate_artifact,save,sha,summarize_execution
from .inherited.artifact_service import ArtifactStore

class ArtifactWorkerManager(WorkerManager):
    def __init__(self,store,context):
        self.context=context
        super().__init__(store,timeout_s=context.worker_timeout_s)
        self._exit_lock=threading.RLock()
        self._launcher_pids={}
    def start(self,task_id):
        require_time()
        self.timeout_s=min(self.context.worker_timeout_s,remaining())
        task=self.store.reserve_launch(task_id,self.owner)
        attempt=process_attempt('worker',dict(task_id=task_id,owner=self.owner,timeout_s=self.timeout_s))
        folder=WEEK12_ROOT/'projects'/task['project_id']/'task-logs';folder.mkdir(parents=True,exist_ok=True)
        command=[sys.executable,'-X','utf8','-B','-m','week13.src.case_worker','--db',str(self.store.db_path),'--task',task_id,'--owner',self.owner,'--context',str(self.context.path)]
        options=dict(cwd=str(REPO),stdin=subprocess.DEVNULL)
        if os.name=='nt':options['creationflags']=subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP
        else:options['start_new_session']=True
        try:
            with (folder/(task_id+'.log')).open('ab') as log:process=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,**options)
        except OSError as exc:
            finish_process(attempt,dict(error=str(exc)))
            self.store.finish_task(task_id,'failed',owner=self.owner,error=str(exc));return
        mark_process_launched(attempt,process.pid)
        self._attempts=getattr(self,'_attempts',{});self._attempts[task_id]=attempt
        self._exit_receipts=getattr(self,'_exit_receipts',{})
        self._exit_receipts[task_id]=folder/(task_id+'.process-exit.json')
        self._launcher_pids[task_id]=process.pid
        with self._lock:self._processes[task_id]=process
        # Shutdown explicitly joins owned monitors. Keep a bounded escape if a
        # dependency is stuck: a pending receipt is better than hanging stdio.
        thread=threading.Thread(target=self._monitor,args=(task_id,process),daemon=True)
        self._monitors=getattr(self,'_monitors',{});self._monitors[task_id]=thread
        thread.start()
    def _record_process_exit(self,task_id,process,code,*,timed_out=False,cleanup_confirmed=None,observation='OWNED_POPEN_WAIT_RETURN'):
        """Commit the actual process fact before making a task terminal visible.

        Task/outcome validation is separate. A status reader can fail and close
        MCP immediately after task publication; its daemon monitor must not be
        responsible for a still-pending process receipt at that point.
        """
        receipt=dict(schema='v12-owned-worker-process-exit-1',attempt_id=self._attempts[task_id],
            task_id=task_id,owner=self.owner,pid=getattr(process,'pid',None),returncode=code,
            timed_out=timed_out,uncertain=code is None or cleanup_confirmed is False,
            cleanup_confirmed=cleanup_confirmed,exit_observation=observation,observed_at_utc=utc_now())
        path=getattr(self,'_exit_receipts',{}).get(task_id)
        if path is not None:
            if Path(path).is_file():
                recorded=json.loads(Path(path).read_text(encoding='utf8'))
                if any(recorded.get(k)!=receipt.get(k) for k in ('attempt_id','task_id','owner','pid','returncode')):
                    raise ValueError('durable owned worker exit conflicts with observation')
                receipt=recorded
            else:save_json(path,receipt,immutable=True)
        finish_process(self._attempts[task_id],receipt)
        return receipt

    def _finalize_exit(self,task_id,receipt):
        """Finalize only an attempt launched by this exact manager instance."""
        if task_id not in self._attempts or receipt.get('attempt_id')!=self._attempts[task_id] or receipt.get('owner')!=self.owner or receipt.get('task_id')!=task_id:
            raise ValueError('exit reconciliation is not owned by this manager')
        expected_pid=getattr(self,'_launcher_pids',{}).get(task_id)
        if expected_pid is not None and receipt.get('pid')!=expected_pid:raise ValueError('owned launcher PID differs')
        task=self.store.task(task_id)
        if task.get('worker_owner',self.owner)!=self.owner:raise ValueError('task exit owner differs')
        if task['state'] not in ('queued','running','cancel_requested'):return task
        if receipt.get('timed_out'):
            return self.store.finish_task(task_id,'uncertain' if receipt.get('uncertain') else 'failed',owner=self.owner,
                error='owned artifact worker timeout; task conclusion UNKNOWN; cleanup='+str(receipt.get('cleanup_confirmed')))
        if task['state']=='cancel_requested':
            cleanup_unknown=receipt.get('cleanup_confirmed') is not True and receipt.get('returncode')!=0
            return self.store.finish_task(task_id,'uncertain' if receipt.get('uncertain') or cleanup_unknown else 'cancelled',owner=self.owner,error='accepted cancellation preserved after worker exit')
        result=task.get('result')
        if receipt.get('returncode')==0 and isinstance(result,dict):
            from .inherited.service_adapter import _verify_manifest
            base=WEEK12_ROOT/'projects'/task['project_id']/'versions'/task['version_id']
            manifest=_verify_manifest(Path(result['outcome_manifest_path']),result['outcome_manifest_sha256'],base)
            if manifest['binding']['task_id']!=task_id or manifest['binding']['owner']!=self.owner:raise ValueError('worker result owner mismatch')
            result={**result,'worker_exit_code':receipt['returncode'],'worker_pid':task['worker_pid']}
            return self.store.finish_task(task_id,'succeeded' if result['chain_status']=='PASS' else 'failed',owner=self.owner,result=result)
        return self.store.finish_task(task_id,'uncertain',owner=self.owner,error='worker exited without valid staged evidence: '+str(receipt.get('returncode')))

    def reconcile_owned_exit(self,task_id):
        """Explicit idempotent local repair; never scan/recover historical rows."""
        if task_id not in getattr(self,'_attempts',{}):raise ValueError('only this manager newly launched attempts may reconcile')
        path=self._exit_receipts[task_id]
        receipt=json.loads(Path(path).read_text(encoding='utf8'))
        if receipt.get('attempt_id')!=self._attempts[task_id] or receipt.get('owner')!=self.owner or receipt.get('task_id')!=task_id:
            raise ValueError('durable exit receipt identity differs')
        expected_pid=getattr(self,'_launcher_pids',{}).get(task_id)
        if expected_pid is not None and receipt.get('pid')!=expected_pid:raise ValueError('durable launcher PID differs')
        finish_process(self._attempts[task_id],receipt)
        return self._finalize_exit(task_id,receipt)

    def cancel(self,task_id,*,lock_timeout_s=15):
        # Serialize cancellation observation against the normal monitor's exit
        # publication. Never inspect or terminate a historical PID by number.
        lock=getattr(self,'_exit_lock',None)
        if lock is None:self._exit_lock=lock=threading.RLock()
        if not lock.acquire(timeout=lock_timeout_s):
            raise RuntimeError('owned exit finalization lock busy; cancellation remains pending')
        try:
            task=self.store.request_cancel(task_id)
            if task['state']!='cancel_requested':return task
            with self._lock:process=self._processes.get(task_id)
            if process is None or task.get('worker_owner')!=self.owner:
                raise ValueError('cancellation has no owned Popen handle; lifecycle remains pending')
            if process.poll() is None:cleaned=self._terminate_owned(task_id,process)
            else:cleaned=None
            receipt=self._record_process_exit(task_id,process,process.poll(),cleanup_confirmed=cleaned,
                observation='OWNED_CANCEL_CLEANUP_AND_POLL')
            return self._finalize_exit(task_id,receipt)
        finally:lock.release()
    def _monitor(self,task_id,process):
        receipt_attempted=False
        try:
            code=process.wait(timeout=self.timeout_s)
            receipt_attempted=True
            lock=getattr(self,'_exit_lock',None)
            if lock is None:self._exit_lock=lock=threading.RLock()
            with lock:
                receipt=self._record_process_exit(task_id,process,code)
                self._finalize_exit(task_id,receipt)
        except subprocess.TimeoutExpired:
            lock=getattr(self,'_exit_lock',None)
            if lock is None:self._exit_lock=lock=threading.RLock()
            with lock:
                self.store.request_cancel(task_id);cleaned=self._terminate_owned(task_id,process)
                receipt_attempted=True
                receipt=self._record_process_exit(task_id,process,process.poll(),timed_out=True,
                    cleanup_confirmed=cleaned,observation='OWNED_TIMEOUT_CLEANUP_AND_POLL')
                self._finalize_exit(task_id,receipt)
        except (OSError,ValueError,KeyError) as exc:
            if not receipt_attempted:
                receipt_attempted=True
                self._record_process_exit(task_id,process,process.poll(),observation='MONITOR_ERROR_POLL_NOT_TASK_STATE')
            self.store.finish_task(task_id,'failed',owner=self.owner,error='artifact evidence rejected: '+str(exc))
        finally:
            try:
                if not receipt_attempted:
                    code=process.poll()
                    self._record_process_exit(task_id,process,code,observation='MONITOR_FINAL_POLL_NOT_TASK_STATE')
            finally:
                with self._lock:self._processes.pop(task_id,None)

    def shutdown(self):
        # The inherited shutdown cancels owned processes. Join their monitors
        # so durable receipt/ledger/task completion is not daemon-dependent.
        with self._lock:task_ids=list(self._processes)
        cancellation_pending=set()
        for task_id in task_ids:
            try:self.cancel(task_id)
            except (OSError,ValueError,RuntimeError,KeyError) as exc:
                cancellation_pending.add(task_id)
                path=getattr(self,'_exit_receipts',{}).get(task_id)
                if path:save_json(Path(path).with_suffix('.finalization-pending.json'),dict(task_id=task_id,
                    attempt_id=self._attempts[task_id],error=type(exc).__name__+': '+str(exc),task_conclusion='UNKNOWN'))
        still_monitoring=set()
        for task_id,thread in list(getattr(self,'_monitors',{}).items()):
            if thread is not threading.current_thread():thread.join(timeout=15)
            if thread.is_alive():
                still_monitoring.add(task_id)
                path=getattr(self,'_exit_receipts',{}).get(task_id)
                if path:save_json(Path(path).with_suffix('.finalization-pending.json'),dict(task_id=task_id,
                    attempt_id=self._attempts[task_id],error='owned monitor still alive after bounded shutdown join',
                    task_conclusion='UNKNOWN',automatic_recovery=False))
        for task_id,path in list(getattr(self,'_exit_receipts',{}).items()):
            if task_id in still_monitoring or task_id in cancellation_pending:continue
            if Path(path).is_file():
                try:self.reconcile_owned_exit(task_id)
                except (OSError,ValueError,RuntimeError,KeyError) as exc:
                    save_json(Path(path).with_suffix('.finalization-pending.json'),dict(task_id=task_id,
                        attempt_id=self._attempts[task_id],error=type(exc).__name__+': '+str(exc),
                        original_exit_receipt=str(path),task_conclusion='UNKNOWN'))

def execute_task(db_path,task_id,owner,context_path):
    require_time();os.environ['V12_CASE_TASK_ID']=task_id;os.environ['PYTHONDONTWRITEBYTECODE']='1'
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
        from .inherited.artifact_service import ArtifactService
        from .case_paths import TASK_CONTEXT
        ctx=None
        if TASK_CONTEXT is not None:
            from .case_requests import require_execution
            from .case_backend import service as case_service
            from .case_scope import check_delta
            ctx=require_execution(TASK_CONTEXT['request_id'])
            service=case_service(db_path,task['session_id'],worker_manager=object(),task_context=ctx)
        else:service=ArtifactService(db_path,task['session_id'],context=context,worker_manager=object())
        from .inherited.artifact_baseline import execution_baselines
        with execution_baselines(service,task_context=ctx) as baseline_operation:
            if ctx is not None:check_delta(service,proposal['spec'],task_context=ctx)
            service._verify_sources(task['project_id'],proposal['spec'])
            from .inherited.artifact_baseline import verify_proposal
            baseline=verify_proposal(service,proposal,require_selection=True,require_confirmation=True)
            base.mkdir(parents=True,exist_ok=False);spec=checked['spec']
            save(base/'spec.json',spec);save(base/'confirmation.json',proposal['confirmation'])
            if baseline:save(base/'baseline-binding.json',spec['baseline_ref'])
            (base/'frozen-model.mo').write_text(spec['artifact']['modelica_source'],encoding='utf8',newline='\n')
            if sha((base/'frozen-model.mo').read_bytes())!=proposal['display']['source_sha256']:raise ValueError('frozen source bytes mismatch')
            from .inherited.artifact_sysml import inspect_dual_source
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
                baseline_operation.before_launch()
                from .case_toolchain import execute_model
                from .case_asym2_family import is_asym2_source
                extra=dict(confirmed_spec=spec) if is_asym2_source(spec['artifact']['modelica_source']) else {}
                tool=execute_model(spec['artifact']['modelica_source'],spec['artifact']['model_name'],base/'tool',spec['settings']['stop_time'],spec['settings']['intervals'],simulation_timeout_s=context.simulation_timeout_s,**extra)
                if tool.get('source_sha256')!=checked['source_sha256']:raise ValueError('toolchain actual source hash mismatch')
                csv_path=base/'tool/model_res.csv'
            from .case_progress import emit_progress
            emit_progress('independent_check','正在独立检查实际输出与任务结果。',task_id=task_id)
            from .case_saved_assessment import assessment_inputs,create_receipt
            checked_inputs=assessment_inputs(spec,base) if ctx is not None else None
            result=summarize_execution(spec,tool,csv_path)
            if not dual_ok:result.update(chain_status='BLOCKED_SYSML',tool_status='NOT_RUN')
            result['dual_source_contract']='v11-dual-source-check-1'
            result['sysml_status']=syntax.get('status','UNKNOWN');result['consistency_status']=consistency['status']
            result['modelica_status']=dict(check=tool.get('compile',{}).get('check_model','NOT_RUN' if not dual_ok else 'TEST_ONLY'),compile=tool.get('compile',{}).get('status','UNKNOWN'),simulation=tool.get('simulation',{}).get('status','UNKNOWN'))
            result['dual_source_issues']=consistency['issues']
            result['sysml_contract_check_invocations']=1
            result['execution_mode']=context.mode;result['test_only']=context.mode.startswith('TEST_ONLY')
            result['toolchain_invocations']=0 if context.mode=='TEST_ONLY' or not dual_ok else 1
            from .inherited.artifact_delivery import create_delivery
            delivery_binding=dict(task_id=task_id,project_id=task['project_id'],version_id=task['version_id'],spec_digest=proposal['spec_digest'],source_sha256=checked['source_sha256'])
            result['result_delivery']=create_delivery(spec,csv_path,base,delivery_binding,result)
            saved_assessment=create_receipt(task,proposal,spec,result,base,ctx,checked_inputs=checked_inputs)
            if saved_assessment is not None:result['saved_assessment']=saved_assessment
            save(base/'outcome.json',result)
            from .inherited.service_adapter import file_sha256
            files={p.relative_to(base).as_posix():dict(sha256=file_sha256(p),bytes=p.stat().st_size) for p in base.rglob('*') if p.is_file()}
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

CaseWorkerManager=ArtifactWorkerManager
