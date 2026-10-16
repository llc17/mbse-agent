"""Bounded reference backend inside the existing project Agent, never vehicle PASS."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import ast, copy, hashlib, json, math, os, re, time, uuid
from .reference_three_profile import (REPO_ROOT as PROJECT, WEEK12_ROOT as V11, 
    PROFILE, SOURCE_PATH, SOURCE_SHA, CONTRACT_SHA, AUTH_SHA, OLD_LEDGER_SHA, authorization,
    validate_reference_spec, derived_values, MODEL_NAME)

from .reference_three_execution import EXECUTION, CONFIG as EXECUTION_CONFIG, OUT as DEFAULT_OUT
KEY=EXECUTION['key']
LEDGER=DEFAULT_OUT/'ledger.json'
CURRENT_PARENT=None
CAPS=EXECUTION['caps']
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
    auth=authorization(); old=DEFAULT_OUT/EXECUTION['snapshot_file']
    if sha(old)!=OLD_LEDGER_SHA or {k:v for k,v in data.items() if k!=KEY}!=read(old):
        raise Stop('CLOSED_HISTORICAL_LEDGER_MODIFIED')
    s=data.get(KEY,{})
    if (s.get('authorization_sha256')!=AUTH_SHA or s.get('proposal_sha256')!=CONTRACT_SHA
        or s.get('scope_id')!=auth['scope_id'] or s.get('caps')!=CAPS):
        raise Stop('REFERENCE_AUTHORITY_OR_CAP_DRIFT')
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
    if e>=EXECUTION['csv_deadline_s'] and not s.get('actual_reference_dynamics'):
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
        assert_scope(s,30)
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

def verify_confirmation(proposal,session_id):
    from week11.src.store import digest_json
    auth=authorization();c=proposal.get('confirmation') or {}
    expected=dict(channel='EXISTING_USER_AUTHORIZATION_IMPORT',human_new_approval=False,profile=PROFILE,
        authorization_sha256=AUTH_SHA,reference_contract_sha256=CONTRACT_SHA,
        reference_source_sha256=SOURCE_SHA,execution_scope_id=auth['scope_id'])
    if proposal['session_id']!=session_id or proposal['spec'].get('profile')!=PROFILE:
        raise Stop('REFERENCE_SESSION_OR_PROFILE_MISMATCH')
    if any(c.get(k)!=v for k,v in expected.items()):raise Stop('REFERENCE_CONFIRMATION_MISMATCH')
    if digest_json(proposal['spec'])!=proposal['spec_digest'] or digest_json(proposal['display'])!=proposal['displayed_digest']:
        raise Stop('REFERENCE_SPEC_OR_DISPLAY_DRIFT')
    if c.get('spec_digest')!=proposal['spec_digest'] or c.get('displayed_digest')!=proposal['displayed_digest']:
        raise Stop('REFERENCE_CONFIRMATION_DIGEST_DRIFT')


def source_files():
    # Traverse only public local Python modules and package initializers. No historical results scan.
    entries=['week12/src/car_domain/'+n for n in ('contracts.py','store_adapter.py','service_adapter.py',
        'worker_adapter.py','runtime_adapter.py','cli_adapter.py','mcp_adapter.py','reference_three_route.py',
        'reference_three_profile.py','reference_three_generator.py','reference_three_verify.py','reference_three_execution.py')]
    entries+=['week12/src/toolchain.py','week12/tests/test_reference_three_step.py']
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
        if not name.startswith(('week9/src/','week10/src/','week11/src/','week12/src/','week12/scripts/','week12/tests/')) and not name.endswith('/__init__.py'):
            raise Stop('UNAPPROVED_LOCAL_DEPENDENCY:'+name)
        if 'experiments' in name.split('/') or Path(name).name=='.env':raise Stop('FORBIDDEN_DEPENDENCY')
        seen.add(name)
        if len(seen)>200:raise Stop('UNKNOWN_LOCAL_CLOSURE_SIZE')
        path=PROJECT/name
        tree=ast.parse(path.read_text(encoding='utf-8-sig'),filename=name)
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
    fixed=['week12/configs/car_domain_runtime.json',SOURCE_PATH,
]
    fixed=[x for x in fixed if (PROJECT/x).is_file()]
    fixed += [(DEFAULT_OUT/n).relative_to(PROJECT).as_posix() for n in
        (EXECUTION['proposal_file'],EXECUTION['authorization_file'],EXECUTION['snapshot_file'])]
    if EXECUTION_CONFIG.exists():fixed.append(EXECUTION_CONFIG.relative_to(PROJECT).as_posix())
    return [dict(path=p,sha256=sha(PROJECT/p)) for p in sorted(seen|set(fixed))]

def create_implementation_binding(path):
    import importlib.metadata as md
    rows=source_files()
    versions={name:md.version(name) for name in ('mcp','sysmlpy','jsonschema')}
    try:versions['psutil']=md.version('psutil')
    except md.PackageNotFoundError:pass
    save(path,dict(schema='v11-reference-source-closure-1',files=rows,source_file_set=[r['path'] for r in rows],
        third_party_versions=versions,profile=PROFILE,authorization_sha256=AUTH_SHA,created_utc=stamp(),
        explicit_dynamic_dependencies=['week9.src.hard_gate.SysMLParserAdapter','week9.src.modelica_tool._terminate_process_tree']))
    return sha(path)

def assert_binding(binding):
    rows=source_files()
    if binding.get('files')!=rows or binding.get('source_file_set')!=[r['path'] for r in rows]:
        raise Stop('SOURCE_CLOSURE_ADDED_REMOVED_RENAMED_OR_BYTES_DRIFT')
    import importlib.metadata as md
    if any(md.version(n)!=v for n,v in binding['third_party_versions'].items()):raise Stop('DEPENDENCY_VERSION_DRIFT')

def bind_checked_implementation(binding_path,checks_path):
    binding=read(binding_path);checks=read(checks_path)
    assert_binding(binding)
    if checks.get('status')!='PASS' or checks.get('failures') or checks.get('errors'):
        raise Stop('FOCUSED_CHECKS_NOT_PASS')
    with transaction() as (_,s):
        assert_scope(s)
        s['admission']=dict(binding_path=str(binding_path),binding_sha256=sha(binding_path),checks_path=str(checks_path),checks_sha256=sha(checks_path),kind='AUTHORIZED_FOCUSED_CHECKS_NO_INDEPENDENT_REVIEW')
        s['gates'].update(admission='PASS',implementation='PASS',source_closure='PASS')

def generated_sha():
    from .reference_generator import render_model
    from .reference_three_profile import parameter_values
    return hashlib.sha256(render_model(parameter_values())).hexdigest()

SUCCESS=dict(schema_version='v11-reference-outcome-1',profile=PROFILE,agent_execution_status='COMPLETED_REFERENCE_VARIANT',
    reference_chain_status='REFERENCE_VARIANT_CHAIN_PASS',vehicle_task_status='UNKNOWN',
    physical_acceptance_status='NOT_ACCEPTED_KNOWN_SIMPLIFICATION',numeric_convergence='NOT_VERIFIED',
    tool_status='PASS',reference_replay_status='NOT_APPLICABLE',source_algebra_status='PASS_IN_REFERENCE_SCOPE',
    evidence_status='COMPLETE_REFERENCE_ONLY',published_version_id=None,whole_V11_complete=False)

def verify_manifest_binding(manifest,result,task,proposal,owner,require_worker_exit=True):
    verify_confirmation(proposal,task['session_id'])
    expected=dict(task_id=task['task_id'],project_id=task['project_id'],proposal_id=proposal['proposal_id'],
        session_id=task['session_id'],spec_digest=proposal['spec_digest'],displayed_digest=proposal['displayed_digest'],
        reference_source_sha256=SOURCE_SHA,reference_contract_sha256=CONTRACT_SHA,authorization_sha256=AUTH_SHA,
        profile=PROFILE,owner=owner)
    if any(manifest.get('binding',{}).get(k)!=v for k,v in expected.items()):raise Stop('REFERENCE_MANIFEST_BINDING_MISMATCH')
    payload=manifest.get('outcome',{})
    if any(payload.get(k)!=result.get(k) for k in SUCCESS):raise Stop('IMMUTABLE_REFERENCE_RESULT_MISMATCH')
    if result.get('agent_execution_status')=='COMPLETED_REFERENCE_VARIANT' and any(result.get(k)!=v for k,v in SUCCESS.items()):
        raise Stop('REFERENCE_COMPLETION_TUPLE_INVALID')
    if result.get('agent_execution_status')=='COMPLETED_REFERENCE_VARIANT':
        verify_completed_evidence(manifest,result,task,require_worker_exit=require_worker_exit)

def assert_completion_scope(s,task,require_worker_exit=True):
    """Validate this task's completion using the existing execution account.

    CLI/MCP may remain open for queries. Only this worker and its descendants
    must be finished; a live interactive session is not a task failure.
    """
    assert_scope(s)
    if s.get('stop_reason'):
        raise Stop('REFERENCE_COMPLETION_STOP_REASON')
    for name,limit in CAPS.items():
        if name in s.get('counts',{}) and finite(s['counts'][name])>limit:
            raise Stop('REFERENCE_COMPLETION_COUNT_LIMIT')
    if (finite(s['product_tokens'])>CAPS['product_tokens']
            or finite(s['product_usd'])>CAPS['product_usd']):
        raise Stop('REFERENCE_COMPLETION_PRODUCT_LIMIT')
    claim=s.get('execution_claim',{})
    if (claim.get('task_id')!=task['task_id']
            or claim.get('owner')!=task['worker_owner']
            or claim.get('pid')!=task['worker_pid']):
        raise Stop('REFERENCE_COMPLETION_OWNER_MISMATCH')
    if not require_worker_exit:
        return
    parents=s.get('parents',[])
    worker=next((p for p in parents if p.get('label')=='car-worker:'+task['task_id']),None)
    if (not worker or worker.get('actual_exit_code')!=0
            or worker.get('pid')!=task['worker_pid'] or worker.get('status')!='RETURNED'
            or finite(worker.get('own_s',float('inf')))>CAPS['parent_own_s']):
        raise Stop('REFERENCE_WORKER_ACTUAL_EXIT_OR_FINAL_ACCOUNT_NOT_PASS')
    for row in s.get('operations',[])+s.get('product_requests',[])+parents:
        if row is worker:
            continue
        token=row.get('parent_token')
        if token and _descends(token,worker['token'],parents) and row.get('status')!='RETURNED':
            raise Stop('REFERENCE_WORKER_DESCENDANT_NOT_COMPLETE')


def read_initialized_parameters(stdout, expected):
    """Read actual OMC LOG_INIT_V values, not parenthesized start values."""
    if ('LOG_INIT_V' not in stdout or 'parameter values' not in stdout
            or 'The initialization finished successfully' not in stdout):
        raise Stop('ACTUAL_INITIALIZATION_PARAMETER_LOG_MISSING')
    pattern=re.compile(r'\bparameter (Real|Integer) ([A-Za-z_][A-Za-z0-9_]*)\(start=[^\r\n]*?, fixed=(?:true|false)\) = ([^\s]+)\s*$',re.MULTILINE)
    values={}
    for match in pattern.finditer(stdout):
        kind,name,text=match.groups()
        if name not in expected:continue
        if name in values:raise Stop('ACTUAL_RUNTIME_PARAMETER_DUPLICATE:'+name)
        try:value=float(text)
        except ValueError:raise Stop('ACTUAL_RUNTIME_PARAMETER_NONNUMERIC:'+name) from None
        if not math.isfinite(value):raise Stop('ACTUAL_RUNTIME_PARAMETER_NONFINITE:'+name)
        if kind=='Integer' and not value.is_integer():raise Stop('ACTUAL_RUNTIME_INTEGER_INVALID:'+name)
        if not math.isclose(value,expected[name],abs_tol=1e-9,rel_tol=1e-9):
            raise Stop('ACTUAL_RUNTIME_PARAMETER_DIFFERS:'+name)
        values[name]=value
    missing=set(expected)-set(values)
    if missing:raise Stop('ACTUAL_RUNTIME_PARAMETER_LOG_MISSING:'+','.join(sorted(missing)))
    return values

def verify_completed_evidence(manifest,result,task,require_worker_exit=True):
    """Completion requires real owned scope and independently bound actual artifacts."""
    # Called by Store inside its existing success transaction, including re-query.
    with transaction() as (_,s):
        assert_completion_scope(s,task,require_worker_exit)
    base=V11/'projects'/task['project_id']/'versions'/task['version_id']
    files=manifest['files']
    required=['generated/candidate.mo','generated/candidate.sysml','generated/parameter-source-map.json',
        'generated/generation-provenance.json','sysml/result.json','frontend/frontend-process.json',
        'scene-01/compile-process.json','scene-01/simulation-process.json','scene-01/verification-process.json',
        'scene-01/actual-settings-and-parameters.json','scene-01/result.csv','scene-01/independent-result.json',
        'scene-01/simulation.stdout.log','scene-01/model_init.xml']
    if any(name not in files for name in required):raise Stop('REFERENCE_COMPLETION_MISSING_ACTUAL_ARTIFACT')
    if sha(base/'generated/candidate.mo')!=generated_sha():raise Stop('REFERENCE_COMPLETION_SOURCE_MISMATCH')
    for name,cap in [('frontend/frontend-process.json',60),('scene-01/compile-process.json',120),
                     ('scene-01/simulation-process.json',300),('scene-01/verification-process.json',60)]:
        r=read(base/name)
        if (r.get('returncode')!=0 or any(r.get(k) is not False for k in ('timed_out','uncertain','interrupted'))
            or not isinstance(r.get('duration_s'),(int,float)) or r['duration_s']>cap):
            raise Stop('REFERENCE_COMPLETION_TOOL_OR_CLEANUP_NOT_PASS')
    if read(base/'sysml/result.json').get('status')!='PASS':raise Stop('REFERENCE_COMPLETION_SYSML_NOT_PASS')
    checked=read(base/'scene-01/independent-result.json')
    if (checked.get('status')!='PASS_IN_REFERENCE_SCOPE' or checked.get('reference_replay_status')!='NOT_APPLICABLE'
        or checked.get('source_algebra_status')!='PASS_IN_REFERENCE_SCOPE'
        or checked.get('binding',{}).get('spec_digest')!=manifest['binding']['spec_digest']
        or checked.get('binding',{}).get('new_csv_sha256')!=sha(base/'scene-01/result.csv')):
        raise Stop('REFERENCE_COMPLETION_INDEPENDENT_REPORT_MISMATCH')
    actual=read(base/'scene-01/actual-settings-and-parameters.json')
    expected_parameters=derived_values(read(base/'spec.json')['parameters'])
    runtime_parameters=read_initialized_parameters(
        (base/'scene-01/simulation.stdout.log').read_text(encoding='utf8'),expected_parameters)
    if (actual.get('model_sha256')!=generated_sha() or actual.get('parameters')!=runtime_parameters
        or actual.get('runtime_parameter_log_sha256')!=sha(base/'scene-01/simulation.stdout.log')
        or actual.get('xml_sha256')!=sha(base/'scene-01/model_init.xml')
        or actual.get('csv_sha256')!=sha(base/'scene-01/result.csv')):
        raise Stop('REFERENCE_COMPLETION_ACTUAL_PARAMETER_READBACK_MISSING')
    with transaction() as (_,s):
        assert_completion_scope(s,task,require_worker_exit)
        c=s.get('execution_claim',{})
        if (c.get('task_id')!=task['task_id'] or c.get('owner')!=task['worker_owner'] or c.get('pid')!=task['worker_pid']
            or s.get('actual_reference_dynamics',{}).get('csv_sha256')!=sha(base/'scene-01/result.csv')
            or s['gates'].get('admission')!='PASS' or s['gates'].get('verification')!='PASS_REFERENCE_ONLY'):
            raise Stop('REFERENCE_COMPLETION_ACTUAL_SCOPE_OWNER_OR_GATES_MISMATCH')
        if require_worker_exit:
            p=next((p for p in s['parents'] if p['label']=='car-worker:'+task['task_id']),{})
            if p.get('actual_exit_code')!=0 or p.get('pid')!=task['worker_pid'] or p.get('status')!='RETURNED':
                raise Stop('REFERENCE_WORKER_ACTUAL_EXIT_OR_FINAL_ACCOUNT_NOT_PASS')
        if not s.get('admission'):raise Stop('REFERENCE_COMPLETION_NO_ADMISSION')
        assert_binding(read(s['admission']['binding_path']))

class ReferenceRoute:
    def __init__(self,task,proposal,directory,owner):
        self.task=task;self.proposal=proposal;self.owner=owner;self.directory=Path(directory).resolve()
        self.directory.relative_to(V11.resolve());self.claim_token=None
        checked=validate_reference_spec(proposal['spec'])
        if not checked.get('ready'):raise Stop('UNSUPPORTED_REFERENCE_SPEC')
        verify_confirmation(proposal,task['session_id']);self.spec=checked['spec']
    def _owned(self,s):
        r=s.get('execution_claim',{})
        if not self.claim_token or (r.get('token'),r.get('task_id'),r.get('owner'),r.get('pid'))!=(self.claim_token,self.task['task_id'],self.owner,os.getpid()):
            raise Stop('REFERENCE_SCENE_NOT_OWNER')
    def claim(self):
        with transaction() as (_,s):
            assert_scope(s,480)
            a=s.get('admission',{})
            for label in ('binding','checks'):
                if not a.get(label+'_path') or sha(a[label+'_path'])!=a.get(label+'_sha256'):raise Stop('NO_CURRENT_REVIEW_ADMISSION')
            self.binding=read(a['binding_path']);assert_binding(self.binding)
            if s.get('execution_claim'):raise Stop('REFERENCE_SCOPE_ALREADY_CONSUMED')
            self.claim_token=uuid.uuid4().hex
            s['execution_claim']=dict(token=self.claim_token,task_id=self.task['task_id'],owner=self.owner,pid=os.getpid(),status='CLAIMED',utc=stamp())
            s['status']='EXECUTING_REFERENCE'
    def child(self,kind,label,command,cwd,cap,logs=None):
        from week12.src.toolchain import _run
        assert_binding(self.binding)
        with transaction() as (_,s):
            self._owned(s);assert_scope(s,cap)
            key={'frontend':'frontend','parse':'parse','scene':'scene_children'}[kind]
            limit=CAPS[key]
            if s['counts'][key]>=limit:raise Stop('REFERENCE_OPERATION_CAP')
            if any(r['status']=='RUNNING' for r in s.get('operations',[])):raise Stop('UNRESOLVED_REFERENCE_OPERATION')
            s['counts'][key]+=1;token=uuid.uuid4().hex
            row=dict(token=token,kind=kind,label=label,status='RUNNING',parent_token=CURRENT_PARENT,
                started_utc=stamp(),cap_s=cap,command=list(map(str,command)),cwd=str(cwd))
            s.setdefault('operations',[]).append(row)
        r=bounded_run(list(map(str,command)),Path(cwd),cap)
        logs=Path(logs or cwd);logs.mkdir(parents=True,exist_ok=True)
        for stream in ('stdout','stderr'):(logs/(label+'.'+stream+'.log')).write_text(r[stream],encoding='utf8')
        with transaction() as (_,s):
            self._owned(s);row=next(x for x in s['operations'] if x.get('token')==token)
            row.update({k:v for k,v in r.items() if k not in ('stdout','stderr')},status='RETURNED',ended_utc=stamp())
            s['external_s']+=finite(r['duration_s'])
            bad=r['returncode']!=0 or r['timed_out'] or r['uncertain'] or r['interrupted'] or r['duration_s']>cap
            if bad:s.update(status='STOPPED_REFERENCE_TOOL',stop_reason=label+' actual tool/cleanup/cap failed')
        save(logs/(label+'-process.json'),{k:v for k,v in r.items() if k not in ('stdout','stderr')})
        if bad:raise Stop('REFERENCE_TOOL_FAILED:'+label)
        assert_binding(self.binding)
        return r

    def run(self):
        from .reference_three_generator import generate_reference
        from week12.src.toolchain import _marker,_check_model_succeeded
        import xml.etree.ElementTree as ET
        self.claim()
        generated=self.directory/'generated';manifest=generate_reference(self.spec,generated)
        model=Path(manifest['model_path']);sysml=Path(manifest['sysml_path'])
        if sha(model)!=generated_sha():raise Stop('REFERENCE_GENERATED_ORIGINAL_SHA_MISMATCH')
        parse_dir=self.directory/'sysml';parse_dir.mkdir()
        command=[os.sys.executable,'-X','utf8','-B','-m','week12.src.car_domain.reference_three_verify','parse-child',
            '--source',str(sysml),'--report',str(parse_dir/'result.json')]
        self.child('parse','sysml',command,PROJECT,30,logs=parse_dir)
        parsed=read(parse_dir/'result.json')
        if parsed.get('status')!='PASS':raise Stop('REFERENCE_SYSML_SYNTAX_NOT_PASS')
        omc=Path('D:/openmodelica/bin/omc.exe')
        front=self.directory/'frontend';front.mkdir()
        quoted=str(model.resolve()).replace('\\','/').replace('"','\\"')
        head=f'print("__REF_VERSION__="+getVersion()+"\\n");\nloadFile("{quoted}");\nprint("__REF_SOURCE__="+getSourceFile({MODEL_NAME})+"\\n");\nprint("__REF_CHECK__="+checkModel({MODEL_NAME})+"\\n");\n'
        (front/'frontend.mos').write_text(head,encoding='utf8')
        r=self.child('frontend','frontend',[omc,front/'frontend.mos'],front,60)
        if ('1.25.7' not in _marker(r['stdout'],'__REF_VERSION__') or
            Path(_marker(r['stdout'],'__REF_SOURCE__').strip('"')).resolve()!=model.resolve() or
            not _check_model_succeeded(_marker(r['stdout'],'__REF_CHECK__'))):raise Stop('REFERENCE_FRONTEND_SOURCE_OR_VERSION_FAILED')
        with transaction() as (_,s):
            self._owned(s);assert_scope(s,480)
            if s['counts']['scenes']>=CAPS['scenes']:raise Stop('REFERENCE_SCENE_CAP')
            s['counts']['scenes']+=1
            scene=dict(index=s['counts']['scenes'],status='RUNNING',parent_token=CURRENT_PARENT,started_utc=stamp())
            s['scenes'].append(scene)
        folder=self.directory/'scene-01';folder.mkdir()
        mos=head+f'build := buildModel({MODEL_NAME},startTime=0,stopTime=120,numberOfIntervals=2400,tolerance=0.01,method="dassl",outputFormat="csv",fileNamePrefix="model");\nprint("__REF_EXE__="+build[1]+"\\n");\nprint(getErrorString());\n'
        (folder/'build.mos').write_text(mos,encoding='utf8')
        build=self.child('scene','compile',[omc,folder/'build.mos'],folder,120)
        exe=folder/'model.exe';xml=folder/'model_init.xml'
        if not exe.is_file() or not xml.is_file() or not _marker(build['stdout'],'__REF_EXE__'):raise Stop('NEW_REFERENCE_EXECUTABLE_MISSING')
        if Path(_marker(build['stdout'],'__REF_SOURCE__').strip('"')).resolve()!=model.resolve():raise Stop('COMPILE_SOURCE_MISMATCH')
        tree=ET.parse(xml); settings=tree.find('DefaultExperiment')
        if settings is None:raise Stop('ACTUAL_XML_SETTINGS_MISSING')
        actual=settings.attrib
        expected={'startTime':0,'stopTime':120,'stepSize':.05,'tolerance':.01}
        if any(k not in actual or not math.isclose(float(actual[k]),v,rel_tol=0,abs_tol=1e-12) for k,v in expected.items()):raise Stop('ACTUAL_XML_NUMERIC_SETTINGS_DIFFER')
        if actual.get('solver')!='dassl' or actual.get('outputFormat')!='csv':raise Stop('ACTUAL_XML_SOLVER_FORMAT_DIFFER')
        values={};derived_missing=[];frozen=derived_values(self.spec['parameters'])
        for var in tree.iter('ScalarVariable'):
            name=var.get('name')
            if name in frozen:
                if var.get('variability')!='parameter':raise Stop('ACTUAL_XML_PARAMETER_VARIABILITY_DIFFERS:'+name)
                child=next(iter(var),None)
                if child is None:raise Stop('PARAMETER_XML_TYPE_MISSING')
                if 'start' in child.attrib:values[name]=float(child.get('start'))
                elif name not in self.spec['parameters'] and var.get('causality')=='calculatedParameter':derived_missing.append(name)
                else:raise Stop('LITERAL_XML_PARAMETER_VALUE_MISSING:'+name)
        if set(values)|set(derived_missing)!=set(frozen) or any(not math.isclose(values[k],frozen[k],abs_tol=1e-9,rel_tol=1e-9) for k in values):
            raise Stop('ACTUAL_XML_PARAMETER_READBACK_FAILED')
        csv_path=folder/'result.csv'
        simulation=self.child('scene','simulation',[exe,'-s=dassl','-lv=LOG_INIT_V,LOG_SUCCESS','-r='+str(csv_path)],folder,300)
        # A dynamics milestone is actual finite new CSV with full coverage, never an old artifact.
        import csv
        with csv_path.open('r',encoding='utf-8-sig',newline='') as f:
            reader=csv.DictReader(f);rows=list(reader)
        if len(rows)<2 or abs(float(rows[0]['time']))>1e-9 or abs(float(rows[-1]['time'])-120)>1e-7:
            raise Stop('NEW_REFERENCE_FULL_CSV_NOT_COVERED')
        if any(not math.isfinite(float(v)) for row in rows for v in row.values()):raise Stop('NEW_REFERENCE_CSV_NONFINITE')
        runtime_values=read_initialized_parameters(simulation['stdout'],frozen)
        save(folder/'actual-settings-and-parameters.json',dict(settings=actual,parameters=runtime_values,
            literal_xml_values=values,calculated_xml_without_start=derived_missing,
            actual_readback_method='XML_LITERAL_VALUES_AND_PARAMETER_METADATA_PLUS_ACTUAL_LOG_INIT_V_VALUES',
            runtime_parameter_log_sha256=sha(folder/'simulation.stdout.log'),
            constancy_basis='SOURCE_PARAMETER_DECLARATIONS_AND_COMPILED_PARAMETER_VARIABILITY_NOT_SAMPLED_CSV',
            model_sha256=sha(model),xml_sha256=sha(xml),executable_sha256=sha(exe),csv_sha256=sha(csv_path)))
        with transaction() as (_,s):
            self._owned(s);s['actual_reference_dynamics']=dict(csv_path=str(csv_path),csv_sha256=sha(csv_path),rows=len(rows),utc=stamp())
            s['gates']['reference_dynamics']='PASS_NEW_FULL_CSV_ONLY'
        report=folder/'independent-result.json'
        self.child('scene','verification',[os.sys.executable,'-X','utf8','-B','-m','week12.src.car_domain.reference_three_verify','verify-child',
            '--source',str(csv_path),'--model',str(model),'--spec',str(self.directory/'spec.json'),'--report',str(report)],PROJECT,60,logs=folder)
        checked=read(report)
        if checked.get('reference_replay_status')!='NOT_APPLICABLE' or checked.get('source_algebra_status')!='PASS_IN_REFERENCE_SCOPE':raise Stop('REFERENCE_CHECKS_NOT_PASS')
        with transaction() as (_,s):
            self._owned(s);s['scenes'][-1].update(status='PASS_REFERENCE_ONLY',ended_utc=stamp())
            s['gates'].update(generation='PASS_REFERENCE_TEMPLATE',sysml='PASS_SYNTAX_ONLY',frontend='PASS',compile='PASS',simulation='PASS',verification='PASS_REFERENCE_ONLY')
        return {**copy.deepcopy(SUCCESS),'reference_task_status':checked.get('reference_task_status','UNKNOWN'),'reference_events_status':checked.get('reference_events',{}).get('status','UNKNOWN')}

def execute_reference_task(task,proposal,directory,owner):
    route=ReferenceRoute(task,proposal,directory,owner)
    result=None
    try:result=route.run()
    except BaseException as exc:
        result={**SUCCESS,'agent_execution_status':'BLOCKED_REFERENCE','reference_chain_status':'UNKNOWN',
            'tool_status':'TOOL_FAIL','reference_replay_status':'UNKNOWN','source_algebra_status':'UNKNOWN',
            'evidence_status':'INCOMPLETE_REFERENCE','error_type':type(exc).__name__,'error':str(exc)[:1000]}
        if route.claim_token:
            with transaction() as (_,s):
                route._owned(s);s.update(status='STOPPED_REFERENCE_EXECUTION',stop_reason=str(exc)[:1000])
    binding=dict(task_id=task['task_id'],project_id=task['project_id'],proposal_id=proposal['proposal_id'],
        session_id=task['session_id'],spec_digest=proposal['spec_digest'],displayed_digest=proposal['displayed_digest'],
        reference_source_sha256=SOURCE_SHA,reference_contract_sha256=CONTRACT_SHA,authorization_sha256=AUTH_SHA,
        profile=PROFILE,owner=owner)
    directory=Path(directory);files={}
    for path in sorted(directory.rglob('*')):
        if path.is_file():files[path.relative_to(directory).as_posix()]=dict(sha256=sha(path),bytes=path.stat().st_size)
    outcome=directory/'reference-outcome-manifest.json'
    if outcome.exists():raise Stop('REFERENCE_OUTCOME_OVERWRITE_FORBIDDEN')
    save(outcome,dict(schema='v11-reference-manifest-1',binding=binding,outcome=result,files=files))
    return {**result,'outcome_manifest_path':str(outcome),'outcome_manifest_sha256':sha(outcome)}
