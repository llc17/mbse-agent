"""Finite eightwheel backend inside the V10-derived Agent.
Accounting and owned process cleanup adapted from reference_route; no legacy Route.
"""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import ast,copy,hashlib,json,math,os,re,time,uuid
from .eightwheel_profile import (REPO_ROOT as PROJECT,WEEK12_ROOT as V11,RESULT_ROOT as DEFAULT_OUT,
    PROFILE,PHYSICAL_SHA,CONTRACT_SHA,AUTH_SHA,PROPOSAL_PATH,authorization,validate_eightwheel_spec,
    GENERATED_BODY_SHA,BASE_CONTRACT_SHA,BASE_AUTH_SHA,BASE_BODY_SHA,MODULE_BEFORE_SHA,PROBE_REUSE_SHA)
from week12.src.active_cluster.execution import ORDER,SPEC,execute_scene
RUN=DEFAULT_OUT/'eightwheel-signed-work-execution-20261010'
KEY='eightwheel_signed_work_execution'
LEDGER=DEFAULT_OUT/'ledger.json'
ACTIVATION=json.loads((RUN/'activation-amendment-01.json').read_bytes())
CAPS=dict(active_s=10800,analysis_s=4020,external_s=6780,scenes=14,scene_children=42,
    repairs=2,parents=12,parent_own_s=40,frontend=1,frontend_s=60,parse=2,parse_s=30,
    checks=4,check_s=60,final_checks=2,final_check_s=60,archived_checks=2,archived_check_s=60,product_requests=0,product_tokens=0,product_usd=0)
CAPS=authorization()['caps']
RECOVERY_DEADLINE=authorization()['first_whole_vehicle_deadline_elapsed_s']
CURRENT_PARENT=None
class Stop(RuntimeError): pass

def stamp(): return datetime.now(timezone.utc).isoformat()
def read(p): return json.loads(Path(p).read_bytes())
def save(p,v):
    p=Path(p); tmp=p.with_name(p.name+'.'+uuid.uuid4().hex+'.tmp')
    tmp.write_text(json.dumps(v,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf8')
    os.replace(tmp,p)
def sha(p):
    with Path(p).open('rb') as f: return hashlib.file_digest(f,'sha256').hexdigest()
def finite(v):
    v=float(v)
    if not math.isfinite(v) or v<0: raise Stop('INVALID_TIME_OR_USAGE')
    return v

def validate_authority(data):
    auth=authorization(); old=RUN/'ledger-before.json'
    if sha(old)!=ACTIVATION['snapshot_sha256'] or {k:v for k,v in data.items() if k!=KEY}!=read(old):
        raise Stop('CLOSED_HISTORICAL_LEDGER_MODIFIED')
    s=data.get(KEY,{})
    if (s.get('authorization_sha256')!=AUTH_SHA or s.get('proposal_sha256')!=CONTRACT_SHA
        or s.get('scope_id')!=auth['scope_id'] or s.get('caps')!=CAPS
        or s.get('started_utc')!=auth['started_utc'] or auth.get('caps')!=CAPS):
        raise Stop('REFERENCE_AUTHORITY_OR_CAP_DRIFT')
    if sha(RUN/'ledger-before-module-amendment.json')!=MODULE_BEFORE_SHA:
        raise Stop('MODULE_BEFORE_SNAPSHOT_DRIFT')
    before=read(RUN/'ledger-before-module-amendment.json')[KEY]
    if (s['operations'][:len(before['operations'])]!=before['operations']
        or s['parents'][:len(before['parents'])]!=before['parents']
        or any(s['counts'].get(k,0)<v for k,v in before['counts'].items())
        or s['started_utc']!=before['started_utc']):
        raise Stop('AMENDMENT_REWROTE_PRIOR_USAGE')
    recovery=authorization()
    recovery_before=read(RUN/'module-recovery-before.json')[KEY]
    if (s['operations'][:len(recovery_before['operations'])]!=recovery_before['operations']
        or s['parents'][:len(recovery_before['parents'])]!=recovery_before['parents']
        or s['scene_attempts'][:len(recovery_before['scene_attempts'])]!=recovery_before['scene_attempts']
        or any(s['counts'].get(k,0)<v for k,v in recovery_before['counts'].items())):
        raise Stop('RECOVERY_REWROTE_PRIOR_USAGE')
    newest=read(RUN/'deadline-recovery-before.json')[KEY]
    if (s['operations'][:len(newest['operations'])]!=newest['operations']
        or s['parents'][:len(newest['parents'])]!=newest['parents']
        or s['scene_attempts'][:len(newest['scene_attempts'])]!=newest['scene_attempts']
        or any(s['counts'].get(k,0)<v for k,v in newest['counts'].items())):
        raise Stop('DEADLINE_RECOVERY_REWROTE_PRIOR_USAGE')
    return s

@contextmanager
def transaction():
    lock=DEFAULT_OUT/'car-increment-ledger.lock'
    with lock.open('a+b') as f:
        f.seek(0)
        if not f.read(1): f.write(b'0'); f.flush()
        f.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(f.fileno(),msvcrt.LK_LOCK,1)
        else:
            import fcntl
            fcntl.flock(f.fileno(),fcntl.LOCK_EX)
        try:
            data=read(LEDGER);s=validate_authority(data);before=copy.deepcopy(data)
            yield data,s
            if data!=before:
                s['active_s']=elapsed(s);s['analysis_s']=max(0,s['active_s']-s['external_s'])
                save(LEDGER,data)
        finally:
            f.seek(0)
            if os.name=='nt': msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
            else: fcntl.flock(f.fileno(),fcntl.LOCK_UN)

def elapsed(s):
    return (datetime.now(timezone.utc)-datetime.fromisoformat(s['started_utc'].replace('Z','+00:00'))).total_seconds()
def assert_scope(s,reserve=0,allow_affected=False):
    if str(s['status']).startswith(('CLOSED','STOPPED','UNCERTAIN')):
        raise Stop('REFERENCE_STOPPED_NO_REOPEN')
    e=elapsed(s); ext=finite(s['external_s'])
    if e+reserve>CAPS['active_s'] or max(0,e-ext)>CAPS['analysis_s'] or ext+reserve>CAPS['external_s']:
        raise Stop('REFERENCE_FINITE_BUDGET_EXHAUSTED')
    if e>=RECOVERY_DEADLINE and not s.get('actual_whole_vehicle_dynamics'):
        raise Stop('NEW_FULL_CSV_MILESTONE_MISSED')

def set_parent_context(token):
    global CURRENT_PARENT
    with transaction() as (_,s):
        row=next((p for p in s['parents'] if p['token']==token),None)
        if not row or row['status']!='RESERVED' or row.get('pid')!=os.getpid():
            raise Stop('PARENT_CONTEXT_NOT_CURRENT_PROCESS')
    CURRENT_PARENT=token

def reserve_parent(label,pid=None):
    with transaction() as (_,s):
        assert_scope(s,40)
        if s['counts']['parents']>=CAPS['parents']: raise Stop('PARENT_CAP')
        token=uuid.uuid4().hex;s['counts']['parents']+=1
        s['parents'].append(dict(token=token,label=label,pid=pid,status='RESERVED',
            parent_token=CURRENT_PARENT,started_utc=stamp()))
        return token

def claim_parent(token,label):
    with transaction() as (_,s):
        assert_scope(s)
        p=next((p for p in s['parents'] if p['token']==token),None)
        if not p or p['label']!=label or p['status']!='RESERVED' or p.get('pid') is not None:
            raise Stop('PARENT_CLAIM_REJECTED')
        p['pid']=os.getpid()
    set_parent_context(token)

def _descends(token,ancestor,parents):
    seen=set()
    while token:
        if token==ancestor:return True
        if token in seen:raise Stop('PARENT_LINEAGE_CYCLE')
        seen.add(token); row=next((p for p in parents if p['token']==token),None)
        if not row:raise Stop('UNKNOWN_PARENT_LINEAGE')
        token=row.get('parent_token')
    return False

def finish_parent(token,duration_s,child_s=0,status='RETURNED'):
    with transaction() as (_,s):
        p=next((p for p in s['parents'] if p['token']==token),None)
        if not p or p['status']!='RESERVED':raise Stop('PARENT_ALREADY_FINISHED_OR_NOT_OWNED')
        end=datetime.now(timezone.utc).timestamp(); duration=finite(duration_s);begin=end-duration
        intervals=[]
        for row in s.get('operations',[])+s.get('product_requests',[])+s['parents']:
            if row is p:continue
            pt=row.get('parent_token')
            if not pt or not _descends(pt,token,s['parents']):continue
            if row.get('status') in ('RESERVED','RUNNING'):raise Stop('UNFINISHED_DESCENDANT_CLEANUP_UNKNOWN')
            if 'ended_utc' in row:
                b=datetime.fromisoformat(row.get('actual_ended_utc',row['ended_utc'])).timestamp()
                if 'duration_s' in row:a=b-finite(row['duration_s'])
                elif 'actual_wall_s' in row:a=b-finite(row['actual_wall_s'])
                else:a=datetime.fromisoformat(row['started_utc']).timestamp()
                intervals.append((max(begin,a),min(end,b)))
        covered=0;tail=begin
        for a,b in sorted(intervals):
            if b>a:covered+=max(0,b-max(tail,a));tail=max(tail,b)
        own=max(0,duration-covered)
        p.update(status=status,ended_utc=stamp(),actual_wall_s=duration,nested_union_s=covered,own_s=own)
        s['external_s']+=own
        if own>CAPS['parent_own_s'] or status not in ('RETURNED','FAILED_START'):
            if own>CAPS['parent_own_s']:
                p['status']='OVER_BUDGET'
            s.update(status='STOPPED_PARENT_OR_CLEANUP',stop_reason='parent own time/cleanup unknown or over cap')

def reserve_product_request(request_id,input_reserved=16000,output_reserved=4096):
    with transaction() as (_,s):
        assert_scope(s,120)
        rows=s.setdefault('product_requests',[])
        reserved=sum(x['reserved_tokens'] for x in rows if x['status']=='RESERVED')
        reserve_cost=sum(x['reserved_usd'] for x in rows if x['status']=='RESERVED')
        if input_reserved>16000 or output_reserved>4096 or s['counts']['product_requests']>=CAPS['product_requests']:
            raise Stop('PRODUCT_REQUEST_CAP')
        if any(x['request_id']==request_id for x in rows):raise Stop('PRODUCT_REQUEST_DUPLICATE')
        if s['product_tokens']+reserved+input_reserved+output_reserved>CAPS['product_tokens'] or s['product_usd']+reserve_cost+.0097152>CAPS['product_usd']:
            raise Stop('PRODUCT_TOKEN_COST_CAP')
        token=uuid.uuid4().hex;s['counts']['product_requests']+=1
        rows.append(dict(token=token,request_id=request_id,status='RESERVED',parent_token=CURRENT_PARENT,
            reserved_tokens=input_reserved+output_reserved,reserved_usd=.0097152,started_utc=stamp()))
        return token

def finish_product_request(token,duration_s,tokens=None,cost_usd=None,status='RETURNED'):
    with transaction() as (_,s):
        r=next((r for r in s.get('product_requests',[]) if r['token']==token),None)
        if not r or r['status']!='RESERVED':raise Stop('REQUEST_NOT_OWNED')
        d=finite(duration_s);known=type(tokens) is int and tokens>=0 and cost_usd is not None
        s['external_s']+=d;s['product_tokens']+=tokens if known else r['reserved_tokens']
        s['product_usd']+=finite(cost_usd) if known else r['reserved_usd']
        r.update(status=status if known else 'UNKNOWN_USAGE',duration_s=d,tokens=tokens,cost_usd=cost_usd,ended_utc=stamp())
        if not known or status!='RETURNED' or d>120 or s['product_tokens']>CAPS['product_tokens'] or s['product_usd']>CAPS['product_usd']:
            s.update(status='STOPPED_PRODUCT_UNKNOWN_OR_LIMIT',stop_reason='API unknown/failed/over cap; reserve retained')

def bounded_run(command,cwd,cap):
    """Reuse existing tool result/owned tree cleanup, with one absolute deadline."""
    import subprocess
    from week12.src.toolchain import _run_result,_terminate_process_tree,_OMC
    started=time.monotonic();started_utc=stamp();deadline=started+cap
    env=os.environ.copy();env['PATH']=str(_OMC.parent)+os.pathsep+env.get('PATH','')
    options=dict(cwd=str(cwd),stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
    if os.name=='nt':options['creationflags']=subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.CREATE_NO_WINDOW
    else:options['start_new_session']=True
    try:process=subprocess.Popen(list(map(str,command)),**options)
    except OSError as exc:
        r=_run_result(None,b'',b'',False,False,type(exc).__name__,started)
    else:
        try:
            remaining=deadline-time.monotonic()-5
            if remaining<=0:raise subprocess.TimeoutExpired(command,0)
            out,err=process.communicate(timeout=remaining)
            r=_run_result(process.returncode,out,err,False,False,'',started)
        except (subprocess.TimeoutExpired,KeyboardInterrupt) as exc:
            interrupted=isinstance(exc,KeyboardInterrupt)
            share=max(.001,(deadline-time.monotonic())/6)
            clean,error=_terminate_process_tree(process,cleanup_timeout_s=share)
            out=err=b''
            if clean:
                remaining=deadline-time.monotonic()
                if remaining<=0:clean=False;error+=';deadline exhausted'
                else:
                    try:out,err=process.communicate(timeout=remaining)
                    except subprocess.TimeoutExpired:clean=False;error+=';post cleanup deadline'
            r=_run_result(process.poll(),out,err,not interrupted,not clean,'interrupted' if interrupted else 'timeout',started,interrupted,clean,error)
        r['pid']=process.pid
    r.update(actual_started_utc=started_utc,actual_ended_utc=stamp(),absolute_cap_s=cap)
    if r['duration_s']>cap:r.update(uncertain=True,error=(r.get('error','')+';absolute deadline exceeded'))
    return r


def scene_limits(name):
    rows=read(PROPOSAL_PATH)['remaining_scenes']
    if name==ORDER[0]:return 180,30
    row=next(r for r in rows if r['scene']==name and r['variant']=='base')
    return row['compiler_cap_s'],row['simulation_and_verification_cap_s']

def verify_confirmation(proposal,session_id):
    from week11.src.store import digest_json
    auth=authorization(); c=proposal.get('confirmation') or {}
    expected=dict(channel='EXISTING_USER_AUTHORIZATION_IMPORT',human_new_approval=False,profile=PROFILE,
        authorization_sha256=AUTH_SHA,execution_contract_sha256=CONTRACT_SHA,
        generated_body_sha256=GENERATED_BODY_SHA,
        physical_spec_sha256=PHYSICAL_SHA,execution_scope_id=auth['scope_id'])
    if proposal['session_id']!=session_id or not validate_eightwheel_spec(proposal['spec']).get('ready'):
        raise Stop('EIGHTWHEEL_SESSION_OR_SPEC_MISMATCH')
    if any(c.get(k)!=v for k,v in expected.items()):raise Stop('EIGHTWHEEL_CONFIRMATION_MISMATCH')
    if digest_json(proposal['spec'])!=proposal['spec_digest'] or digest_json(proposal['display'])!=proposal['displayed_digest']:
        raise Stop('SPEC_OR_DISPLAY_DRIFT')
    if c.get('spec_digest')!=proposal['spec_digest'] or c.get('displayed_digest')!=proposal['displayed_digest']:
        raise Stop('CONFIRMATION_DIGEST_DRIFT')

def source_files():
    # Targeted AST closure of actual public local imports, never history/private scan.
    entries=['week12/src/car_domain/'+n for n in ('contracts.py','store_adapter.py','service_adapter.py',
        'worker_adapter.py','cli_adapter.py','mcp_adapter.py','eightwheel_route.py','eightwheel_profile.py',
        'eightwheel_evidence.py','eightwheel_preparation.py','outcomes.py')]
    entries+=['week12/scripts/s3_c_active_cluster.py','week12/scripts/s3_c_eightwheel_first_run.py','week12/scripts/s3_c_signed_work_pair_check.py',
        'week12/src/toolchain.py','week9/src/hard_gate.py','week9/src/modelica_tool.py']
    pending=list(entries);seen=set()
    def add_module(name):
        bits=name.split('.')
        if not bits or bits[0] not in ('week9','week10','week11','week12'):return
        for i in range(1,len(bits)+1):
            init=PROJECT.joinpath(*bits[:i],'__init__.py')
            if init.is_file():pending.append(init.relative_to(PROJECT).as_posix())
        mod=PROJECT.joinpath(*bits).with_suffix('.py')
        if mod.is_file():pending.append(mod.relative_to(PROJECT).as_posix())
    while pending:
        name=pending.pop()
        if name in seen:continue
        if not name.startswith(('week9/src/','week10/src/','week11/src/','week12/src/','week12/scripts/')) and not name.endswith('/__init__.py'):
            raise Stop('UNAPPROVED_LOCAL_DEPENDENCY:'+name)
        if 'experiments' in name.split('/') or Path(name).name=='.env':raise Stop('FORBIDDEN_DEPENDENCY')
        seen.add(name)
        if len(seen)>200:raise Stop('UNKNOWN_LOCAL_CLOSURE_SIZE')
        tree=ast.parse((PROJECT/name).read_text(encoding='utf-8-sig'),filename=name)
        parts=list(Path(name).with_suffix('').parts);package=parts[:-1]
        for node in ast.walk(tree):
            if isinstance(node,ast.Import):
                for alias in node.names:add_module(alias.name)
            elif isinstance(node,ast.ImportFrom):
                base=package[:len(package)-node.level+1] if node.level else []
                module=base+(node.module.split('.') if node.module else [])
                add_module('.'.join(module))
                for alias in node.names:
                    if alias.name!='*':add_module('.'.join(module+[alias.name]))
            elif isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='__import__':
                # Existing component ledger uses literal standard-library os.fsync.
                # No computed name or local dynamic dependency is admitted here.
                if not (len(node.args)==1 and not node.keywords and isinstance(node.args[0],ast.Constant)
                        and node.args[0].value=='os'):
                    raise Stop('UNPROVEN_DYNAMIC_LOCAL_IMPORT:'+name)
        add_module('.'.join(parts))
    fixed=[SPEC,RUN/'authorization.json',RUN/'activation.json',RUN/'ledger-before.json',
        RUN/'authorization-amendment-01.json',RUN/'activation-amendment-01.json',RUN/'ledger-before-amendment-01.json',
        RUN/'retained-pair-check.json',RUN/'retained-pair-process.json',
        RUN/'implementation-binding.json',RUN/'readiness.json',RUN/'independent-admission.json',
        PROPOSAL_PATH,
        V11/'configs/car_domain_runtime.json',V11/'configs/reference_execution.json']
    reference_config=read(V11/'configs/reference_execution.json')
    for key in ('proposal_file','authorization_file','snapshot_file'):
        name=reference_config[key]
        if Path(name).name!=name:raise Stop('REFERENCE_DEPENDENCY_PATH_ESCAPE')
        fixed.append(DEFAULT_OUT/name)
    fixed += [V11/'tests'/name for name in ('test_reference_core_chain.py','test_car_domain_adapters.py',
        'test_car_domain_route.py','test_reference_history.py','test_eightwheel_handoff.py',
        'test_eightwheel_integration.py','test_eightwheel_evidence.py','test_eightwheel_route.py',
        'test_active_cluster_completion.py','test_active_cluster_generation.py','test_active_cluster_verification.py','test_signed_work_numerics.py')]
    fixed+=[RUN/'archived-inputs.json',RUN/'retained-pair-inputs.json',
            RUN/'module-continuation-proposal.json',RUN/'module-authorization.json',
            RUN/'module-activation.json',RUN/'ledger-before-module-amendment.json',RUN/'module-probe-reuse.json']
    fixed += [Path(r['path']) for r in read(RUN/'module-probe-reuse.json')['passed_inputs']]
    fixed+=[Path(r['path']) for r in read(RUN/'retained-pair-inputs.json')['files']]
    fixed+=[Path(r['path']) for r in read(RUN/'archived-inputs.json')['files']]
    fixed += [RUN/n for n in ('module-recovery-proposal.json','module-recovery-authorization.json','module-recovery-activation.json','module-recovery-before.json')]
    fixed += [RUN/'module-recovery-supplement-authorization.json',RUN/'module-recovery-supplement-before.json']
    fixed += [RUN/n for n in ('deadline-recovery-proposal.json','deadline-recovery-authorization.json','deadline-recovery-activation.json','deadline-recovery-before.json')]
    fixed += [V11/'tests/test_eightwheel_deadline_path.py']
    fixed+=physical_dependencies(None)
    rows=[dict(path=p,sha256=sha(PROJECT/p)) for p in sorted(seen)]
    rows += [dict(path=str(p.resolve()),sha256=sha(p)) for p in fixed]
    return sorted(rows,key=lambda r:r['path'])

def physical_dependencies(package):
    from week12.src.component_physics.runner import COMPLEX,SERVICES,MSL
    result=[COMPLEX,SERVICES,MSL,V11/'models/components/V11PhysicalB1/package.mo',
        V11/'models/components/V11ContactC2/package.mo',V11/'models/components/V11ActiveClusterC3/package.mo']
    return result+([Path(package)] if package else [])

def create_implementation_binding(path):
    import importlib.metadata as md
    rows=source_files()
    save(path,dict(schema='v11-eightwheel-source-closure-1',files=rows,source_file_set=[r['path'] for r in rows],
        third_party_versions={n:md.version(n) for n in ('mcp','sysmlpy','jsonschema','psutil')},
        profile=PROFILE,authorization_sha256=AUTH_SHA,created_utc=stamp()))
    return sha(path)

def assert_binding(binding):
    rows=source_files()
    if binding.get('files')!=rows or binding.get('source_file_set')!=[r['path'] for r in rows]:
        raise Stop('SOURCE_CLOSURE_ADDED_REMOVED_RENAMED_OR_BYTES_DRIFT')
    import importlib.metadata as md
    if any(md.version(n)!=v for n,v in binding['third_party_versions'].items()):raise Stop('DEPENDENCY_VERSION_DRIFT')

def admit(binding_path,readiness_path,review_path):
    b=read(binding_path);r=read(readiness_path);v=read(review_path)
    assert_binding(b)
    if r.get('status')!='PASS_IMPLEMENTATION_ONLY' or v.get('allow_execution') is not True:
        raise Stop('INDEPENDENT_ADMISSION_BLOCK')
    if r.get('binding_sha256')!=sha(binding_path) or v.get('binding_sha256')!=sha(binding_path):
        raise Stop('REVIEW_BINDING_MISMATCH')
    with transaction() as (_,s):
        assert_scope(s)
        s['admission']={k+'_path':str(p) for k,p in [('binding',binding_path),('readiness',readiness_path),('review',review_path)]}
        s['admission'].update({k+'_sha256':sha(p) for k,p in [('binding',binding_path),('readiness',readiness_path),('review',review_path)]})
        s['gates']['admission']='PASS';s['status']='READY_EIGHTWHEEL_EXECUTION'

def assert_completion_scope(s,task,require_worker_exit=True):
    assert_scope(s)
    assert_archived_fail_preserved(s)
    assert_retained_pair_pass(s)
    if s.get('stop_reason'):raise Stop('COMPLETION_STOP_REASON')
    for name,limit in CAPS.items():
        if name in s.get('counts',{}) and finite(s['counts'][name])>limit:raise Stop('COMPLETION_COUNT_LIMIT')
    if finite(s['product_tokens'])!=0 or finite(s['product_usd'])!=0:raise Stop('PRODUCT_FORBIDDEN')
    if s['counts'].get('scenes')!=15 or s['counts'].get('scene_children')!=43 or s['counts'].get('frontend')!=2 or s['counts'].get('parse')!=2:
        raise Stop('ACTUAL_COMPLETE_SET_COUNTS_MISMATCH')
    accounted=sum(finite(r.get('duration_s',0)) for r in s['operations'])+sum(finite(p.get('own_s',0)) for p in s['parents'])
    if not math.isclose(accounted,finite(s['external_s']),rel_tol=0,abs_tol=1e-6):raise Stop('EXTERNAL_ACCOUNT_TOTAL_MISMATCH')
    c=s.get('execution_claim',{})
    if (c.get('task_id'),c.get('owner'),c.get('pid'))!=(task['task_id'],task['worker_owner'],task['worker_pid']):
        raise Stop('COMPLETION_OWNER_MISMATCH')
    if s['gates'].get('admission')!='PASS' or any(s['gates'].get(n)!='PASS' for n in ORDER):raise Stop('COMPLETION_GATES_MISSING')
    a=s.get('admission',{})
    for k in ('binding','readiness','review'):
        if sha(a[k+'_path'])!=a[k+'_sha256']:raise Stop('ADMISSION_DRIFT')
    assert_binding(read(a['binding_path']))
    if not require_worker_exit:return
    parents=s['parents'];worker=next((p for p in parents if p['label']=='car-worker:'+task['task_id']),None)
    if (not worker or worker.get('actual_exit_code')!=0 or worker.get('pid')!=task['worker_pid']
        or worker.get('status')!='RETURNED' or finite(worker.get('own_s',math.inf))>CAPS['parent_own_s']):
        raise Stop('ACTUAL_WORKER_EXIT_NOT_PASS')
    for row in s['operations']+parents:
        if row is worker:continue
        if row.get('parent_token') and _descends(row['parent_token'],worker['token'],parents) and row['status']!='RETURNED':
            raise Stop('UNFINISHED_WORKER_DESCENDANT')

def verify_manifest_binding(*args,**kwargs):
    from .eightwheel_evidence import verify_manifest_binding as verify
    return verify(*args,**kwargs)

def run_check(command,cwd,label,kind='checks'):
    if kind not in ('checks','final_checks','archived_checks'):raise Stop('UNSUPPORTED_CHECK_KIND')
    with transaction() as (_,s):
        assert_scope(s,60)
        if s['counts'][kind]>=CAPS[kind]:raise Stop('CHECK_COUNT_LIMIT')
        s['counts'][kind]+=1;token=uuid.uuid4().hex
        s['operations'].append(dict(token=token,kind=kind,label=label,status='RUNNING',parent_token=CURRENT_PARENT,started_utc=stamp(),cap_s=60))
    r=bounded_run(command,cwd,60)
    r.update(command=list(map(str,command)),cwd=str(Path(cwd).resolve()),ledger_operation_token=token)
    save(RUN/(label+'-process.json'),r)
    with transaction() as (_,s):
        row=next(rw for rw in s['operations'] if rw['token']==token)
        row.update({k:v for k,v in r.items() if k not in ('stdout','stderr')},status='RETURNED',ended_utc=stamp())
        s['external_s']+=finite(r['duration_s'])
        if r['uncertain'] or r['interrupted'] or r['timed_out']:s.update(status='STOPPED_CHECK_TOOL',stop_reason=label)
    return r


def archived_inputs():
    inputs=read(RUN/'archived-inputs.json')
    for row in inputs['files']:
        if sha(row['path'])!=row['sha256']:raise Stop('ARCHIVED_INPUT_DRIFT')
    return inputs

def assert_archived_fail_preserved(s):
    # This is a history-integrity gate, never a physics PASS for the new scene.
    inputs=archived_inputs()
    report=read(inputs['original_failed_check'])
    checks=report.get('physical',{}).get('checks',[])
    failed=[c.get('code') for c in checks if c.get('status')!='PASS']
    if report.get('physical',{}).get('status')!='FAIL' or len(checks)!=326 or failed!=['actual_task_completion']:
        raise Stop('ARCHIVED_ORIGINAL_FAILURE_NOT_PRESERVED')

PROBE_BASE=V11/'projects/prj_6b0f1534bed846b68842aabeade9df7e/versions/v000001/round-01/active_joint_energy-base'

def probe_reuse(s):
    from .eightwheel_profile import _bound_json
    proof=_bound_json(RUN/'module-probe-reuse.json',PROBE_REUSE_SHA)
    if (proof.get('probe_class_byte_equal') is not True
            or proof.get('no_call_to_modified_function_in_probe') is not True
            or proof.get('new_package_sha256')!=GENERATED_BODY_SHA
            or sha(proof['old_generated_package'])!=BASE_BODY_SHA
            or sha(proof['new_package_candidate'])!=GENERATED_BODY_SHA):
        raise Stop('PROBE_REUSE_SEMANTICS_DRIFT')
    def probe(path):
        text=Path(path).read_text(encoding='utf8')
        match=re.search(r'  model Probe\b.*?  end Probe;',text,re.S)
        if not match:raise Stop('MISSING_PROBE_CLASS')
        return match.group(0)
    if probe(proof['old_generated_package'])!=probe(proof['new_package_candidate']):
        raise Stop('PROBE_CLASS_CHANGED')
    for row in proof['components_unchanged']+proof['passed_inputs']:
        path=Path(row['path']);path=path if path.is_absolute() else PROJECT/path
        if sha(path)!=row['sha256']:raise Stop('PROBE_REUSE_INPUT_DRIFT')
    before=read(RUN/'ledger-before-module-amendment.json')[KEY]
    attempts=before['scene_attempts']
    if (len(attempts)!=3 or [(r['label'],r['status']) for r in attempts]!=
            [(ORDER[0]+'-base','PASS'),(ORDER[0]+'-refined','PASS'),(ORDER[1]+'-base','FAIL')]
            or s['scene_attempts'][:3]!=attempts):raise Stop('PROBE_REUSE_ATTEMPTS_DRIFT')
    results=[]
    for variant in ('base','refined'):
        folder=PROBE_BASE.parent/(ORDER[0]+'-'+variant)
        result=read(folder/'scene-result.json');report=read(folder/'independent-result.json')
        if (result.get('status')!='PASS' or result.get('scene')!=ORDER[0]
                or result.get('variant')!=variant or report.get('physical',{}).get('status')!='PASS'
                or (variant=='refined' and report.get('numeric',{}).get('status')!='PASS')):
            raise Stop('PROBE_REUSE_NOT_PASS')
        results.append(result)
    return results


def retained_pair_inputs():
    inputs=read(RUN/'retained-pair-inputs.json')
    for row in inputs['files']:
        if sha(row['path'])!=row['sha256']:raise Stop('RETAINED_PAIR_INPUT_DRIFT')
    old=read(inputs['old_numeric_fail'])
    failed=[c.get('code') for c in old.get('numeric',{}).get('checks',[]) if c.get('status')!='PASS']
    if old.get('numeric',{}).get('status')!='FAIL' or failed!=['final_energy_1percent_energy_shaft_energy']:
        raise Stop('ORIGINAL_NUMERIC_FAILURE_NOT_PRESERVED')
    if sha(inputs['package'])!='eb80da656b5598f9708136c7469e8cfe0e75aea6aa8a4528af2e81c02d802066':
        raise Stop('RETAINED_MODEL_BODY_DRIFT')
    return inputs

def retained_pair_command():
    import sys
    return [sys.executable,'-X','utf8','-B','-m','week12.scripts.s3_c_signed_work_pair_check']

def assert_retained_pair_pass(s):
    inputs=retained_pair_inputs()
    gate=s.get('retained_pair_check',{})
    report_path=RUN/'retained-pair-02-check.json';process_path=RUN/'retained-pair-02-process.json'
    if (gate.get('status')!='PASS' or gate.get('report_sha256')!=sha(report_path)
        or gate.get('process_sha256')!=sha(process_path)
        or gate.get('inputs_sha256')!=sha(RUN/'retained-pair-inputs.json')):
        raise Stop('RETAINED_PAIR_CURRENT_PROOF_REQUIRED')
    report=read(report_path);process=read(process_path)
    from week12.src.active_cluster.verify import SIGNED_WORK_ADDENDUM
    if (report.get('status')!='PASS' or report.get('proposal_sha256')!=BASE_CONTRACT_SHA
        or report.get('authorization_sha256')!=BASE_AUTH_SHA or report.get('addendum')!=SIGNED_WORK_ADDENDUM
        or report.get('inputs_sha256')!=sha(RUN/'retained-pair-inputs.json')
        or process.get('command')!=retained_pair_command() or process.get('cwd')!=str(PROJECT.resolve())
        or process.get('returncode')!=0 or any(process.get(k) is not False for k in ('timed_out','uncertain','interrupted'))
        or not 0<=finite(process.get('duration_s',math.inf))<=60):
        raise Stop('RETAINED_PAIR_REPORT_OR_PROCESS_NOT_PASS')
    for section in ('base_physical','refined_physical','numeric'):
        block=report.get(section,{})
        if block.get('status')!='PASS' or not block.get('checks') or any(c.get('status')!='PASS' for c in block['checks']):
            raise Stop('RETAINED_PAIR_FAILED_SECTION')
    from .eightwheel_evidence import _process
    _process({k:v for k,v in process.items() if k not in ('stdout','stderr')},s['operations'],60,retained_pair_command(),PROJECT,set())
    codes={c.get('code') for c in report['numeric']['checks']}
    if not {'whole_curve_1percent_energy_shaft_energy','final_energy_1percent_energy_shaft_energy'}.issubset(codes):
        raise Stop('RETAINED_PAIR_WHOLE_CURVE_GATE_MISSING')
    matches=[r for r in s['operations'] if r.get('token')==process.get('ledger_operation_token')]
    if (len(matches)!=1 or matches[0].get('kind')!='archived_checks' or matches[0].get('status')!='RETURNED'
        or any(matches[0].get(k)!=v for k,v in process.items() if k not in ('stdout','stderr'))
        or s['counts'].get('archived_checks')!=2):raise Stop('RETAINED_PAIR_ACCOUNT_PROOF_MISMATCH')

def check_archived_csv():
    with transaction() as (_,s):
        assert_scope(s,60)
        admission=s.get('admission',{})
        for k in ('binding','readiness','review'):
            if not admission.get(k+'_path') or sha(admission[k+'_path'])!=admission.get(k+'_sha256'):
                raise Stop('NO_CURRENT_INDEPENDENT_ADMISSION')
        assert_binding(read(admission['binding_path']))
        if s.get('execution_claim'):raise Stop('PAIR_CHECK_MUST_PRECEDE_OMC')
        retained_pair_inputs()
    result=run_check(retained_pair_command(),PROJECT,'retained-pair-02',kind='archived_checks')
    report_path=RUN/'retained-pair-02-check.json'
    passed=result.get('returncode')==0 and not any(result[k] for k in ('timed_out','uncertain','interrupted')) and report_path.is_file()
    with transaction() as (_,s):
        if passed:
            s['retained_pair_check']=dict(status='PASS',report_sha256=sha(report_path),process_sha256=sha(RUN/'retained-pair-02-process.json'),inputs_sha256=sha(RUN/'retained-pair-inputs.json'))
            assert_retained_pair_pass(s)
        else:s.update(status='STOPPED_RETAINED_PAIR_CHECK',stop_reason='Retained actual pair failed new explicitly approved numerical contract')
    return passed

SUCCESS=dict(schema_version='v11-car-outcome-1',published_version_id=None,profile=PROFILE,agent_execution_status='COMPLETED',chain_status='TRUSTED_PHYSICAL_PASS',
    vehicle_task_status='PASS',tool_status='PASS',numerical_status='PASS',evidence_status='COMPLETE')

class EightwheelRoute:
    def __init__(self,task,proposal,directory,owner):
        self.task=task;self.task_id=task['task_id'];self.proposal=proposal;self.owner=owner;self.pid=os.getpid()
        self.directory=Path(directory).resolve();self.directory.relative_to(V11.resolve())
        self.spec=read(SPEC);self.claim_token=None
        if not validate_eightwheel_spec(proposal['spec']).get('ready'):raise Stop('UNSUPPORTED_EIGHTWHEEL_SPEC')
        verify_confirmation(proposal,task['session_id'])
    def _owned(self,s):
        c=s.get('execution_claim',{})
        if not self.claim_token or (c.get('token'),c.get('task_id'),c.get('owner'),c.get('pid'))!=(self.claim_token,self.task_id,self.owner,self.pid):
            raise Stop('NOT_SCENE_OWNER')
    def claim(self):
        with transaction() as (_,s):
            assert_scope(s,5370)
            assert_archived_fail_preserved(s)
            assert_retained_pair_pass(s)
            a=s.get('admission',{})
            for label in ('binding','readiness','review'):
                if not a.get(label+'_path') or sha(a[label+'_path'])!=a.get(label+'_sha256'):raise Stop('NO_CURRENT_REVIEW_ADMISSION')
            self.binding=read(a['binding_path']);assert_binding(self.binding)
            before=read(RUN/'ledger-before-module-amendment.json')[KEY]
            old=before.get('execution_claim',{})
            if (s.get('execution_claim')!=old or self.task_id==old.get('task_id')
                    or self.directory==Path(PROBE_BASE).parent.parent
                    or s.get('continuation_claim')):raise Stop('SCOPE_ALREADY_CONSUMED')
            probe_reuse(s)
            self.claim_token=uuid.uuid4().hex
            s['continuation_claim']=dict(previous_claim=copy.deepcopy(old),probe_reuse_sha256=PROBE_REUSE_SHA)
            s['execution_claim']=dict(token=self.claim_token,task_id=self.task_id,owner=self.owner,pid=self.pid,status='CLAIMED',utc=stamp())
            s['status']='EXECUTING_EIGHTWHEEL'
    def assert_sources(self,binding):
        assert_binding(self.binding)
        for row in binding.get('generated_files',[]):
            if sha(row['path'])!=row['sha256']:raise Stop('GENERATED_SOURCE_DRIFT')
        with transaction() as (_,s):self._owned(s);assert_scope(s)
    def scene_budget(self,name):
        return scene_limits(name)
    def scene_wall(self,name,old):
        return sum(self.scene_budget(name))
    def instrument_build(self,text):
        lines=[]
        for line in text.splitlines():
            phase=('LOAD' if ' := loadFile(' in line else
                   'CHECK_MODEL' if ' := checkModel(' in line else
                   'BUILD_MODEL' if ' := buildModel(' in line else None)
            if phase:
                lines.append('writeFile("build-progress.log","'+phase+'_ENTERED\\n",true);')
            lines.append(line)
            if phase:
                lines.append('writeFile("build-progress.log","'+phase+'_RETURNED\\n",true);')
        return '\n'.join(lines)+'\n'
    def reserve(self,kind,label,wall,round_no=1):
        with transaction() as (_,s):
            self._owned(s);assert_scope(s,wall)
            if round_no!=1 or kind not in ('scene','parse','frontend'):raise Stop('NO_RETRY_OR_NEW_KIND')
            count={'scene':'scenes','parse':'parse','frontend':'frontend'}[kind]
            if s['counts'][count]>=CAPS[count]:raise Stop('OPERATION_COUNT_LIMIT')
            if any(r['status']=='RUNNING' for k in ('scene_attempts','parse_attempts','frontend_attempts') for r in s[k]):
                raise Stop('UNCERTAIN_PREVIOUS_OPERATION')
            if kind=='scene':
                expected=[n+'-'+v for n in ORDER for v in ('base','refined')]
                previous=s['scene_attempts']
                if s.get('continuation_claim'):
                    old=read(RUN/'ledger-before-module-amendment.json')[KEY]['scene_attempts']
                    if previous[:len(old)]!=old:raise Stop('OLD_ATTEMPT_REWRITTEN')
                    previous=previous[:2]+previous[len(old):]
                if label!=expected[len(previous)] or any(r['status']!='PASS' for r in previous):raise Stop('SCENE_ORDER_OR_PRIOR_FAIL')
                name=label.removesuffix('-base').removesuffix('-refined')
                limit=self.scene_wall(name,0)
                if wall!=limit:raise Stop('SCENE_TIME_CAP_DRIFT')
                if name.startswith('twenty') and any(s['gates'].get(n)!='PASS' for n in ORDER[:5]):raise Stop('SMALL_BOTH_DIRECTIONS_REQUIRED')
            elif wall>CAPS[count+'_s']:raise Stop('AUX_TIME_CAP')
            key={'scene':'scene_attempts','parse':'parse_attempts','frontend':'frontend_attempts'}[kind]
            s['counts'][count]+=1
            s[key].append(dict(kind=kind,label=label,round=1,status='RUNNING',reserved_wall_s=wall,started_utc=stamp(),processes=[],owner_token=self.claim_token))
            return key,len(s[key])-1
    def child(self,slot,command,cwd,wall,logs,label):
        key,index=slot
        assert_binding(self.binding)
        with transaction() as (_,s):
            self._owned(s);assert_scope(s)
            row=s[key][index]
            if row['status']!='RUNNING' or row['owner_token']!=self.claim_token:raise Stop('CHILD_NO_OWNED_RESERVATION')
            used=sum(p.get('duration_s',0) for p in row['processes'])
            phase_allowed=math.inf
            if key=='scene_attempts':
                name=row['label'].removesuffix('-base').removesuffix('-refined')
                compile_cap,sim_cap=self.scene_budget(name)
                if label not in ('build','simulation','verification') or any(p['label']==label for p in row['processes']):
                    raise Stop('DUPLICATE_OR_UNKNOWN_SCENE_CHILD')
                phase_used=sum(p.get('duration_s',0) for p in row['processes'] if p['label']!='build')
                phase_allowed=compile_cap if label=='build' else sim_cap-phase_used
            allowed=min(phase_allowed,finite(wall),row['reserved_wall_s']-used,CAPS['external_s']-s['external_s'],CAPS['active_s']-elapsed(s),
                (RECOVERY_DEADLINE-elapsed(s)) if not s.get('actual_whole_vehicle_dynamics') else math.inf)
            if allowed<=5:raise Stop('NO_CHILD_AND_CLEANUP_RESERVE')
            if key=='scene_attempts':
                if s['counts']['scene_children']>=CAPS['scene_children']:raise Stop('SCENE_CHILD_COUNT_LIMIT')
                s['counts']['scene_children']+=1
            token=uuid.uuid4().hex
            op=dict(token=token,ledger_operation_token=token,kind=row['kind'],scene_label=row['label'],label=label,
                status='RUNNING',parent_token=CURRENT_PARENT,started_utc=stamp(),cap_s=allowed,
                command=list(map(str,command)),cwd=str(Path(cwd).resolve()))
            s['operations'].append(op);row['processes'].append(copy.deepcopy(op))
        result=bounded_run(command,cwd,allowed)
        metadata={k:v for k,v in result.items() if k not in ('stdout','stderr')}
        metadata.update(command=list(map(str,command)),cwd=str(Path(cwd).resolve()),ledger_operation_token=token)
        if label=='verification':
            command=list(map(str,command));evidence={}
            for flag in ('--source','--manifest','--graph','--report','--base','--numerical-contract'):
                if flag in command:
                    path=Path(command[command.index(flag)+1]).resolve()
                    if path.is_file():evidence[str(path)]=dict(sha256=sha(path),bytes=path.stat().st_size)
            metadata['evidence_files']=evidence
        logs=Path(logs);logs.mkdir(parents=True,exist_ok=True)
        for stream in ('stdout','stderr'):(logs/(label+'.'+stream+'.log')).write_text(result[stream],encoding='utf8')
        save(logs/(label+'-process.json'),metadata)
        with transaction() as (_,s):
            self._owned(s)
            for entry in [next(p for p in s['operations'] if p['token']==token),next(p for p in s[key][index]['processes'] if p['token']==token)]:
                entry.update(metadata,status='RETURNED',ended_utc=stamp())
            s['external_s']+=finite(result['duration_s'])
            uncertain=result['uncertain'] or result['interrupted'] or result['timed_out'] or result['duration_s']>allowed
            if uncertain:s.update(status='STOPPED_TOOL_OR_CLEANUP',stop_reason=label+' actual process timeout/cleanup/cap')
        if uncertain:raise Stop('CHILD_TOOL_OR_CLEANUP_NOT_TRUSTED')
        # Nonzero verifier returns are retained for exact failed predicates; no next scene.
        assert_binding(self.binding)
        return result
    def finish(self,slot,status,**evidence):
        with transaction() as (_,s):
            self._owned(s);key,index=slot;r=s[key][index]
            if r['owner_token']!=self.claim_token:raise Stop('NONOWNER_FINISH')
            if r['status']!='RUNNING':
                if status=='FAIL' and r['status']=='FAIL':return
                raise Stop('DUPLICATE_FINISH')
            r.update(status=status,ended_utc=stamp(),**evidence)
    def terminal(self,reason,status='STOPPED_EIGHTWHEEL_EXECUTION'):
        with transaction() as (_,s):self._owned(s);s.update(status=status,stop_reason=reason)
    def scene_directory(self,name,refined,round_no):
        return self.directory/'round-01'/(name+('-refined' if refined else '-base'))
    def verification_command(self,directory,manifest_path,graph_path,name,base):
        from .eightwheel_preparation import verification_command
        return verification_command(directory,manifest_path,graph_path,name,base,numerical_contract=PROPOSAL_PATH)
    def prepare(self):
        from week12.src.active_cluster.generator import generate
        from week12.src.active_cluster.verify import verify_generated
        out=RUN/'generated'/self.task_id;out.parent.mkdir(parents=True,exist_ok=True)
        if out.exists():raise Stop('GENERATED_TASK_EXISTS')
        manifest=generate(SPEC,out)
        design=Path(manifest['design_path']);design=design if design.is_absolute() else out/design
        graph=read(design);review=verify_generated(graph,manifest,self.spec)
        save(out/'independent-generation-review.json',review)
        if review.get('status')!='PASS':raise Stop('GENERATION_NOT_VERIFIED')
        if sha(manifest['package_path'])!=GENERATED_BODY_SHA:raise Stop('PHYSICAL_BODY_DRIFT')
        binding=dict(self.binding,generated_files=[dict(path=str(p),sha256=sha(p)) for p in out.iterdir() if p.is_file()],
            generated=dict(directory=str(out),manifest=str(out/'manifest.json'),graph=str(design)),
            proposal_sha256=PHYSICAL_SHA,scope_proposal_sha256=CONTRACT_SHA)
        save(out/'source-binding.json',binding)
        self.copy_generated_evidence(out)
        return manifest,graph,binding
    def prove_frontend(self,manifest,binding):
        from week12.src.active_cluster.frontend import frontend_script,frontend_evidence
        from week12.src.component_physics.runner import OMC
        # Historical proof does not bind its actual readset. Do one explicitly
        # authorized NEW scope check and bind its inputs before/after.
        folder=self.directory/'frontend';folder.mkdir(exist_ok=False)
        mos=folder/'frontend.mos'
        classes=('Complex','ModelicaServices','Modelica','V11PhysicalB1','V11ContactC2','V11ActiveClusterC3','V11GeneratedActive')
        text=frontend_script(manifest)
        for i,cls in enumerate(classes):
            text+=f'print("__CAR_SOURCE_{i}__="+getSourceFile({cls})+"\\n");\n'
        mos.write_text(text,encoding='utf8')
        dependencies=physical_dependencies(Path(manifest['package_path']))
        before=[{'path':str(p),'sha256':sha(p)} for p in dependencies]
        save(folder/'inputs-before.json',before)
        slot=self.reserve('frontend','new-source-bound-seven-class-front-end',60)
        r=self.child(slot,[str(OMC),str(mos)],folder,60,folder,'frontend')
        proof=frontend_evidence(manifest,r)
        from week12.src.toolchain import _marker
        actual_sources=[_marker(r['stdout'],f'__CAR_SOURCE_{i}__').strip(chr(34)) for i in range(7)]
        proof['actual_source_paths']=actual_sources
        if any(not actual or Path(actual).resolve()!=expected.resolve() for actual,expected in zip(actual_sources,dependencies)):
            proof['status']='FAIL_SOURCE_MAPPING'
        proof['input_readset']=before;proof['script_sha256']=sha(mos)
        if any(sha(row['path'])!=row['sha256'] for row in before): raise Stop('FRONTEND_INPUT_DRIFT')
        save(folder/'result.json',proof)
        self.finish(slot,'PASS' if proof['status']=='PASS_FRONTEND_ONLY' else 'FAIL')
        if proof['status']!='PASS_FRONTEND_ONLY': raise Stop('NEW_BOUND_FRONTEND_FAILED')
        binding['frontend_reuse']={'proof_path':str(folder/'result.json'),'proof_sha256':sha(folder/'result.json'),
            'inputs':before,'model_sha256':sha(manifest['package_path'])}
        save(Path(binding['generated']['directory'])/'source-binding.json',binding)
        self.copy_generated_evidence(Path(binding['generated']['directory']))
    def copy_generated_evidence(self,out):
        import shutil
        target=self.directory/'generated-evidence';target.mkdir(exist_ok=True)
        rows=[]
        for path in out.iterdir():
            if path.is_file():
                shutil.copyfile(path,target/path.name)
                rows.append({'original_source_path':str(path),'copy':path.name,'sha256':sha(path)})
        save(target/'copy-source-map.json',rows)
    def frontend_reusable(self,binding,manifest):
        self.assert_sources(binding);p=binding['frontend_reuse']
        if sha(p['proof_path'])!=p['proof_sha256'] or sha(manifest['package_path'])!=p['model_sha256']:
            raise Stop('FRONTEND_PROOF_DRIFT')
        if any(sha(x['path'])!=x['sha256'] for x in p['inputs']): raise Stop('BOUND_FRONTEND_READSET_DRIFT')
        return read(p['proof_path']).get('status')=='PASS_FRONTEND_ONLY'

    def run(self):
        self.claim()
        manifest,graph,binding=self.prepare()
        self.prove_frontend(manifest,binding)
        from week12.src.active_cluster.execution import parse_current_sysml
        sysml=Path(manifest['sysml_path'])
        if not sysml.is_absolute():sysml=Path(binding['generated']['directory'])/sysml
        parse_current_sysml(self,sysml)
        with transaction() as (_,s):self.results=probe_reuse(s)
        for name in ORDER[1:]:
            base=None
            for refined in (False,True):
                result,folder=execute_scene(self,manifest,graph,binding,name,refined,1,base)
                self.results.append(result)
                if name in ORDER[2:] and result.get('simulation')=='COMPLETED':
                    import csv
                    path=folder/'result.csv'
                    with path.open(encoding='utf-8-sig',newline='') as stream:
                        rows=csv.DictReader(stream);n=0;first=last=None
                        for row in rows:
                            v=float(row['time'])
                            if not math.isfinite(v):raise Stop('CSV_TIME_NONFINITE')
                            if first is None:first=v
                            last=v;n+=1
                    if n>1 and last>first:
                        with transaction() as (_,s):
                            self._owned(s);s['actual_whole_vehicle_dynamics']=dict(csv_path=str(path),csv_sha256=sha(path),rows=n,utc=stamp())
                if result['status']!='PASS':
                    self.terminal(result.get('error','FAILED_SCENE'))
                    break
                if not refined:base=folder
            if self.results[-1]['status']!='PASS':break
            with transaction() as (_,s):self._owned(s);s['gates'][name]='PASS'
        from .outcomes import make_outcome
        return make_outcome(self.results)

def execute_eightwheel_task(task,proposal,directory,owner):
    from .outcomes import make_outcome
    route=EightwheelRoute(task,proposal,directory,owner)
    # No fallback may write another owner's evidence on a failed claim.
    route.results=[]
    try:result=route.run()
    except Exception as exc:
        if not route.claim_token:raise
        route.terminal(str(exc))
        result=make_outcome(route.results,error=str(exc))
    result['profile']=PROFILE
    binding=dict(task_id=task['task_id'],project_id=task['project_id'],proposal_id=proposal['proposal_id'],
        session_id=task['session_id'],spec_digest=proposal['spec_digest'],displayed_digest=proposal['displayed_digest'],
        physical_spec_sha256=PHYSICAL_SHA,scope_proposal_sha256=CONTRACT_SHA,authorization_sha256=AUTH_SHA,
        profile=PROFILE,owner=owner)
    directory=Path(directory);files={}
    for path in sorted(directory.rglob('*')):
        if path.is_file() and path.suffix in ('.json','.csv','.mo','.sysml','.mos','.log','.xml','.exe'):
            files[path.relative_to(directory).as_posix()]=dict(sha256=sha(path),bytes=path.stat().st_size)
    outcome=directory/'eightwheel-outcome-manifest.json'
    if outcome.exists():raise Stop('OUTCOME_OVERWRITE_FORBIDDEN')
    save(outcome,dict(schema='v11-eightwheel-manifest-1',project_id=task['project_id'],task_id=task['task_id'],profile=PROFILE,binding=binding,outcome=result,files=files))
    return {**result,'outcome_manifest_path':str(outcome),'outcome_manifest_sha256':sha(outcome)}
