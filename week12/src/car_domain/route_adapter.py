"""Domain adapter for the existing Agent; inner scene/time accounting only.

The closed historical Route is never instantiated. A new user-approved scope
lives as an appended namespace in the SAME ledger. Owner rejection is read-only.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import copy
import hashlib
import json
import math
import os
import time
import uuid

from week12.src.active_cluster.execution import Stop, ORDER, SPEC, PROJECT, V11, DEFAULT_OUT, sha, read, save, execute_scene

AUTH = DEFAULT_OUT/'v10-car-increment-authorization.json'
CONTRACT = DEFAULT_OUT/'v10-car-increment-proposal-final.json'
AUTH_SHA = 'e5eabe60008eaacb941b19032d3b658c7db35a6b44b3fdb46fea29734324f5e5'
CONTRACT_SHA = '768d7b3f60a5d762477dc22e81e53e8c43477297f0d1d222e8dd78af546cba55'
PHYSICAL_SHA = 'ec45de70bd6ffc114c5a9670c0db5000f5ee131a4144f6949fd5df7215c627ac'
LEDGER = DEFAULT_OUT/'ledger.json'
KEY = 'car_domain_increment'

def stamp():
    return datetime.now(timezone.utc).isoformat()

def finite(x):
    x=float(x)
    if not math.isfinite(x) or x<0:
        raise Stop('INVALID_TIME_OR_USAGE')
    return x

def validate_authority(data):
    if sha(AUTH)!=AUTH_SHA or sha(CONTRACT)!=CONTRACT_SHA or sha(SPEC)!=PHYSICAL_SHA:
        raise Stop('NEW_APPROVAL_CONTRACT_OR_PHYSICAL_DRIFT')
    a=read(AUTH); c=read(CONTRACT); old_path=DEFAULT_OUT/'ledger-before-car-increment.json'
    if (a.get('status')!='APPROVED' or a.get('user_message_verbatim')!='批准'
            or a.get('proposal_sha256')!=CONTRACT_SHA
            or sha(old_path)!=a.get('old_ledger_sha256') or a.get('budget')!=c.get('budget')):
        raise Stop('NEW_SCOPE_AUTHORITY_INVALID')
    base=read(old_path)
    actual={k:v for k,v in data.items() if k!=KEY}
    if actual!=base:
        raise Stop('CLOSED_HISTORICAL_LEDGER_MODIFIED')
    s=data.get(KEY,{})
    if s.get('authorization_sha256')!=AUTH_SHA or s.get('proposal_sha256')!=CONTRACT_SHA or s.get('scope_id')!=a['scope_id']:
        raise Stop('NEW_SCOPE_BINDING_INVALID')
    expected=dict(active_s=27000,analysis_s=19200,external_s=7800,scene_attempts=29,
        scene_child_operations=87,complete_sets=2,implementation_repairs=1,parents=8,
        parent_own_each_s=30,frontend=1,frontend_each_s=60,parse=2,parse_each_s=30,
        other_checks=3,other_each_s=20,product_requests=4,product_tokens=80384,
        product_usd=.1,failure_audit=1,failure_audit_s=540)
    if s.get('caps')!=expected:
        raise Stop('FINITE_CAP_DRIFT')
    return s

@contextmanager
def transaction():
    lock=DEFAULT_OUT/'car-increment-ledger.lock'
    with lock.open('a+b') as fh:
        fh.seek(0)
        if not fh.read(1): fh.write(b'0'); fh.flush()
        fh.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(fh.fileno(),msvcrt.LK_LOCK,1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(),fcntl.LOCK_EX)
        try:
            data=read(LEDGER); validate_authority(data)
            before=copy.deepcopy(data)
            yield data, data[KEY]
            if data!=before: save(LEDGER,data)
        finally:
            fh.seek(0)
            if os.name=='nt': msvcrt.locking(fh.fileno(),msvcrt.LK_UNLCK,1)
            else: fcntl.flock(fh.fileno(),fcntl.LOCK_UN)

def elapsed(s):
    return (datetime.now(timezone.utc)-datetime.fromisoformat(s['started_utc'].replace('Z','+00:00'))).total_seconds()

def assert_scope(s, reserve=0, allow_affected=False):
    if str(s['status']).startswith(('CLOSED','STOPPED','UNCERTAIN')) and not (allow_affected and s['status']=='STOPPED_AFFECTED_EXECUTION'):
        raise Stop('NEW_SCOPE_STOPPED_NO_REOPEN')
    e=elapsed(s); ext=finite(s['external_s'])
    if e+reserve>s['caps']['active_s'] or max(0,e-ext)>s['caps']['analysis_s'] or ext+reserve>s['caps']['external_s']:
        raise Stop('FINITE_SCOPE_BUDGET_EXHAUSTED')
    if e>=14400 and not s.get('actual_whole_vehicle_dynamics',False):
        raise Stop('FOUR_HOUR_WHOLE_VEHICLE_DYNAMICS_MILESTONE_MISSED')

def reserve_parent(label,pid=None):
    with transaction() as (_,s):
        assert_scope(s,30)
        if s['counts']['parents']>=8: raise Stop('PARENT_LAUNCH_CAP')
        token=uuid.uuid4().hex
        s['counts']['parents']+=1
        s.setdefault('parents',[]).append({'token':token,'label':label,'pid':pid,'status':'RESERVED','started_utc':stamp(),'external_before_s':s['external_s']})
        return token

def finish_parent(token,duration_s,child_s=0,status='RETURNED'):
    with transaction() as (_,s):
        p=next((x for x in s.get('parents',[]) if x['token']==token),None)
        if not p or p['status']!='RESERVED': raise Stop('PARENT_NOT_OWNED_OR_ALREADY_FINISHED')
        # Nested leaf/API/parent durations already accounted are subtracted once.
        nested=max(finite(child_s),max(0,s['external_s']-p['external_before_s']))
        own=max(0,finite(duration_s)-nested)
        p.update(status=status,ended_utc=stamp(),actual_wall_s=duration_s,nested_accounted_s=nested,own_s=own)
        s['external_s']+=own
        if own>30 or status in ('UNCERTAIN','UNKNOWN','TIMEOUT_CLEANED'):
            s.update(status='STOPPED_PARENT_OR_CLEANUP',stop_reason='parent overhead or cleanup unknown')

def reserve_product_request(request_id,input_reserved=16000,output_reserved=4096):
    with transaction() as (_,s):
        assert_scope(s,120)
        if input_reserved>16000 or output_reserved>4096 or s['counts']['product_requests']>=4:
            raise Stop('PRODUCT_REQUEST_CAP')
        rows=s.setdefault('product_requests',[])
        if any(x['request_id']==request_id for x in rows): raise Stop('PRODUCT_REQUEST_ALREADY_CONSUMED')
        if s['product_tokens']+input_reserved+output_reserved>80384 or s['product_usd']+.0097152>.1:
            raise Stop('PRODUCT_TOKEN_COST_RESERVE_EXHAUSTED')
        token=uuid.uuid4().hex;s['counts']['product_requests']+=1
        rows.append({'token':token,'request_id':request_id,'status':'RESERVED','reserved_tokens':input_reserved+output_reserved,'reserved_usd':.0097152,'started_utc':stamp()})
        return token

def finish_product_request(token,duration_s,tokens=None,cost_usd=None,status='RETURNED'):
    with transaction() as (_,s):
        row=next((x for x in s.get('product_requests',[]) if x['token']==token),None)
        if not row or row['status']!='RESERVED': raise Stop('PRODUCT_REQUEST_NOT_OWNED')
        s['external_s']+=finite(duration_s)
        known=tokens is not None and cost_usd is not None
        s['product_tokens']+=int(tokens) if known else row['reserved_tokens']
        s['product_usd']+=finite(cost_usd) if known else row['reserved_usd']
        row.update(status=status if known else 'UNKNOWN_USAGE',duration_s=duration_s,tokens=tokens,cost_usd=cost_usd,ended_utc=stamp())
        if not known or status!='RETURNED' or duration_s>120 or s['product_tokens']>80384 or s['product_usd']>.1:
            s.update(status='STOPPED_PRODUCT_UNKNOWN_OR_LIMIT',stop_reason='actual product failure/unknown/limit retained')

def admit(binding_path,readiness_path,review_path):
    # Root final admission follows a real independent review, never AI self-approval of new physical scope.
    b=read(binding_path);r=read(readiness_path);v=read(review_path)
    if r.get('status')!='PASS_IMPLEMENTATION_ONLY' or v.get('allow_execution') is not True:
        raise Stop('INDEPENDENT_ADMISSION_BLOCK')
    if r.get('binding_sha256')!=sha(binding_path) or v.get('binding_sha256')!=sha(binding_path):
        raise Stop('ADMISSION_SOURCE_BINDING_MISMATCH')
    with transaction() as (_,s):
        assert_scope(s)
        s['admission']={'binding_path':str(binding_path),'binding_sha256':sha(binding_path),
            'readiness_path':str(readiness_path),'readiness_sha256':sha(readiness_path),
            'review_path':str(review_path),'review_sha256':sha(review_path)}
        s['gates']['independent']='PASS';s['gates']['implementation']='PASS';s['gates']['source_binding']='PASS'
        s['status']='READY_FOR_FIRST_CASE'

class CarRoute:
    def __init__(self,task,owner,directory):
        self.task_id=task['task_id'];self.owner=owner;self.pid=os.getpid()
        self.directory=Path(directory).resolve()
        self.directory.relative_to(V11.resolve())
        self.spec=read(SPEC);self.audit_mode=False
        self.refresh()
        self.claim_token=None
    def refresh(self):
        with transaction() as (_,s): self.data=copy.deepcopy(s)
        return self.data
    def _owned(self,s):
        c=s.get('execution_claim')
        if not self.claim_token or not c or (c['token'],c['task_id'],c['owner'],c['pid'])!=(self.claim_token,self.task_id,self.owner,self.pid):
            raise Stop('NOT_SCENE_OWNER')
    def claim(self):
        with transaction() as (_,s):
            assert_scope(s)
            a=s.get('admission')
            if not a or sha(a['binding_path'])!=a['binding_sha256'] or sha(a['review_path'])!=a['review_sha256'] or sha(a['readiness_path'])!=a['readiness_sha256']:
                raise Stop('NO_REAL_SOURCE_BOUND_ADMISSION')
            binding=read(a['binding_path']); self._check_binding(binding)
            if s.get('execution_claim'): raise Stop('EXECUTION_SCOPE_SINGLE_CONSUMPTION')
            self.claim_token=uuid.uuid4().hex
            s['execution_claim']={'token':self.claim_token,'task_id':self.task_id,'owner':self.owner,'pid':self.pid,'status':'CLAIMED','utc':stamp()}
            s['status']='EXECUTING_FIRST_CASE'
            for k in ('scene_attempts','parse_attempts','source_bindings'): s.setdefault(k,[])
            for name in ORDER: s['gates'].setdefault(name,'NOT_RUN')
    def _check_binding(self,binding):
        rows=binding.get('files',[])
        current=source_files()
        if binding.get('source_file_set') != [x['path'] for x in current]:
            raise Stop('SOURCE_FILE_SET_ADDED_MISSING_OR_RENAMED')
        if not rows or len({x['path'] for x in rows})!=len(rows): raise Stop('INCOMPLETE_OR_DUPLICATE_BINDING')
        for row in rows:
            p=Path(row['path']);p=p if p.is_absolute() else PROJECT/p
            if p.name.lower()=='.env' or str(p).lower().replace('\\','/').endswith('/week9/experiments/cases.py'):
                raise Stop('FORBIDDEN_SOURCE')
            if sha(p)!=row['sha256']: raise Stop('SOURCE_DRIFT:'+str(p))
    def assert_sources(self,binding):
        self._check_binding(binding)
        with transaction() as (_,s): self._owned(s); assert_scope(s,allow_affected=self.audit_mode)
    def persist(self):
        raise Stop('NO_UNGUARDED_PROXY_PERSIST_USE_OWNER_TRANSACTION')
    def reserve(self,kind,label,wall,round_no=1):
        with transaction() as (_,s):
            self._owned(s);assert_scope(s,wall,allow_affected=self.audit_mode)
            if kind not in ('scene','parse','frontend'): raise Stop('UNSUPPORTED_OPERATION')
            cap={'scene':29,'parse':2,'frontend':1}[kind]
            count_key={'scene':'scene_attempts','parse':'parse','frontend':'frontend'}[kind]
            if s['counts'][count_key]>=cap: raise Stop('FINITE_OPERATION_CAP')
            if kind=='scene':
                current=[r for r in s['scene_attempts'] if r.get('round')==round_no and not r.get('failure_audit')]
                expected=[name+'-'+v for name in ORDER for v in ('base','refined')]
                if not self.audit_mode and (len(current)>=14 or label!=expected[len(current)] or any(r['status']!='PASS' for r in current)):
                    raise Stop('SCENE_ORDER_OR_UNAUTHORIZED_RETRY')
                if label.startswith('twenty') and any(s['gates'].get(n)!='PASS' for n in ORDER[:5]): raise Stop('SMALL_BOTH_DIRECTIONS_REQUIRED')
                if self.audit_mode and s['counts']['failure_audit']!=1: raise Stop('NO_FAILURE_AUDIT_RESERVATION')
            key='scene_attempts' if kind=='scene' else 'parse_attempts' if kind=='parse' else 'frontend_attempts'
            rows=s.setdefault(key,[])
            if any(r.get('status')=='RUNNING' for k in ('scene_attempts','parse_attempts','frontend_attempts') for r in s.get(k,[])):
                raise Stop('UNRESOLVED_OPERATION_BLOCKS_RESUME')
            s['counts'][count_key]+=1
            rows.append({'kind':kind,'label':label,'round':round_no,'status':'RUNNING','reserved_wall_s':wall,'failure_audit':self.audit_mode,'started_utc':stamp(),'processes':[],'owner_token':self.claim_token})
            return key,len(rows)-1
    def child(self,slot,command,cwd,wall,logs,label):
        from week12.src.toolchain import _run
        key,index=slot
        with transaction() as (_,s):
            self._owned(s);assert_scope(s,allow_affected=self.audit_mode)
            row=s[key][index]
            if row['status']!='RUNNING' or row['owner_token']!=self.claim_token: raise Stop('NO_OWNED_RUNNING_OPERATION')
            remaining=row['reserved_wall_s']-sum(p.get('duration_s',0) for p in row['processes'])
            allowed=min(finite(wall),remaining,7800-s['external_s'],27000-elapsed(s))
            if allowed<=5: raise Stop('NO_CHILD_AND_CLEANUP_RESERVE')
            if key=='scene_attempts':
                if s['counts']['scene_child_operations']>=87: raise Stop('SCENE_CHILD_OPERATION_CAP')
                s['counts']['scene_child_operations']+=1
            token=uuid.uuid4().hex
            row['processes'].append({'token':token,'label':label,'status':'REQUESTED','command':list(map(str,command)),'cwd':str(cwd),'timeout_s':allowed-5,'started_utc':stamp()})
        r=_run(list(map(str,command)),Path(cwd),allowed-5)
        Path(logs).mkdir(parents=True,exist_ok=True)
        for stream in ('stdout','stderr'): (Path(logs)/(label+'.'+stream+'.log')).write_text(r[stream],encoding='utf8')
        with transaction() as (_,s):
            self._owned(s);entry=next(p for p in s[key][index]['processes'] if p['token']==token)
            entry.update({k:v for k,v in r.items() if k not in ('stdout','stderr')});entry.update(status='UNCERTAIN' if r['uncertain'] else 'RETURNED',ended_utc=stamp())
            s['external_s']+=finite(r['duration_s'])
            if r['uncertain'] or r['interrupted']: s.update(status='UNCERTAIN',stop_reason='child cleanup uncertain or interrupted')
        if r['uncertain'] or r['interrupted']: raise Stop('CHILD_CLEANUP_UNCERTAIN_OR_INTERRUPTED')
        return r
    def finish(self,slot,status,**evidence):
        with transaction() as (_,s):
            self._owned(s);key,index=slot;r=s[key][index]
            if r['owner_token']!=self.claim_token or r['status']!='RUNNING': raise Stop('NONOWNER_OR_DUPLICATE_FINISH')
            r.update(status=status,ended_utc=stamp(),**evidence)
    def terminal(self,reason,status='STOPPED_AFFECTED_EXECUTION'):
        with transaction() as (_,s): self._owned(s);s.update(status=status,stop_reason=reason)
    def scene_wall(self,name,normal): return 540 if self.audit_mode else normal
    def scene_directory(self,name,refined,round_no):
        if self.audit_mode: return self.directory/'failure-audit'/name
        return self.directory/f'round-{round_no:02d}'/(name+('-refined' if refined else '-base'))
    def verification_command(self,directory,manifest_path,graph_path,name,base):
        command=[os.sys.executable,'-B','-m','week12.scripts.s3_c_car_increment_acceptance','verify-child',
            '--source',str(directory/'result.csv'),'--manifest',str(manifest_path),'--graph',str(graph_path),
            '--scene',name,'--report',str(directory/'independent-result.json')]
        if base: command+=['--base',str(base/'result.csv')]
        if self.audit_mode: command+=['--failure-audit']
        return command
    def prepare(self):
        from week12.src.active_cluster.generator import generate
        from week12.src.active_cluster.verify import verify_generated
        out=DEFAULT_OUT/'car-domain-generated'/self.task_id
        out.parent.mkdir(parents=True,exist_ok=True)
        if out.exists(): raise Stop('GENERATED_TASK_EXISTS_NO_OVERWRITE')
        manifest=generate(SPEC,out)
        design=Path(manifest['design_path']);design=design if design.is_absolute() else out/design
        graph=read(design);review=verify_generated(graph,manifest,self.spec)
        save(out/'independent-generation-review.json',review)
        if review.get('status')!='PASS': raise Stop('GENERATION_CONSISTENCY_NOT_VERIFIED')
        if sha(manifest['package_path'])!='a4af03f09054c8a8c011e2e8ffc268ee09c1d4b1d683b59d56e65783b1495cf9':
            raise Stop('FROZEN_PHYSICAL_MODEL_BODY_DRIFT')
        save(out/'manifest.json',manifest)
        binding={'source_file_set':[x['path'] for x in source_files()],'files':source_files()+[{'path':str(p),'sha256':sha(p)} for p in out.iterdir() if p.is_file()],
            'generated':{'directory':str(out),'manifest':str(out/'manifest.json'),'graph':str(design)},'created_utc':stamp(),
            'proposal_sha256':PHYSICAL_SHA,'scope_proposal_sha256':CONTRACT_SHA}
        save(out/'source-binding.json',binding)
        self.copy_generated_evidence(out)
        with transaction() as (_,s): self._owned(s);s['source_bindings'].append(binding)
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
    def set_gate(self,name,status):
        with transaction() as (_,s): self._owned(s);s['gates'][name]=status


def physical_dependencies(package):
    from week12.src.component_physics.runner import COMPLEX,SERVICES,MSL
    return [COMPLEX,SERVICES,MSL,V11/'models/components/V11PhysicalB1/package.mo',V11/'models/components/V11ContactC2/package.mo',V11/'models/components/V11ActiveClusterC3/package.mo',package]

def source_files():
    # Owned-source directory listing only. No history or private scan.
    paths=list((V11/'src/car_domain').glob('*.py'))+[
        V11/'src/active_cluster/execution.py',V11/'src/active_cluster/generator.py',V11/'src/active_cluster/modelica.py',
        V11/'src/active_cluster/design.py',V11/'src/active_cluster/verify.py',V11/'src/active_cluster/frontend.py',
        V11/'src/toolchain.py',V11/'src/component_physics/runner.py',
        V11/'scripts/s3_c_car_increment_acceptance.py',V11/'configs/car_domain_runtime.json']
    paths+=list((V11/'tests').glob('test_car_domain*.py'))
    paths+=list((PROJECT/'week11/src').glob('*.py'))
    paths+=[SPEC,CONTRACT,AUTH]
    paths+=physical_dependencies(DEFAULT_OUT/'readiness-diagnostic-01/generated/V11GeneratedActive.mo')
    return [{'path':str(p.resolve()),'sha256':sha(p)} for p in sorted(set(paths),key=str)]

def create_implementation_binding(path):
    save(path,{'schema':'v11-car-increment-implementation-binding-1','source_file_set':[x['path'] for x in source_files()],'files':source_files(),
        'scope_proposal_sha256':CONTRACT_SHA,'created_utc':stamp()})
    return sha(path)

def execute_car_task(task,proposal,output_dir,owner):
    from .outcomes import make_outcome,write_outcome
    spec=proposal.get('spec',{})
    ref=spec.get('physical_ref',{})
    if ref.get('sha256')!=PHYSICAL_SHA: raise Stop('TASK_PHYSICAL_SPEC_NOT_APPROVED')
    route=CarRoute(task,owner,output_dir)
    results=[];audit=None;error=None
    # Claim OUTSIDE catch: a rejected non-owner has no failure-writing fallback.
    route.claim()
    try:
        with transaction() as (_,s):
            route._owned(s);assert_scope(s,3180)
            if 7800-s['external_s']<6360+540: raise Stop('NO_FULL_INITIAL_REPAIR_AND_FAILURE_RESERVE')
            s['counts']['complete_sets']+=1
        manifest,graph,binding=route.prepare()
        route.prove_frontend(manifest,binding)
        sysml=Path(manifest['sysml_path'])
        if not sysml.is_absolute(): sysml=Path(binding['generated']['directory'])/sysml
        if sha(sysml)!='2bd46a477f6f5ee4ba94840877471c77f624f3702a1092e2dcebf914deaf60d6':
            from week12.src.active_cluster.execution import parse_current_sysml
            parse_current_sysml(route,sysml)
        for name in ORDER:
            base=None
            for refined in (False,True):
                result,folder=execute_scene(route,manifest,graph,binding,name,refined,1,base)
                results.append(result)
                proof_path=folder/'independent-result.json'
                if name in ORDER[2:] and result.get('simulation')=='COMPLETED' and proof_path.exists():
                    proof=read(proof_path).get('physical',{}).get('diagnostics',{})
                    if proof.get('samples',0)>1 and proof.get('last_time',0)>0:
                        with transaction() as (_,s):
                            route._owned(s);s['actual_whole_vehicle_dynamics']=True
                if result['status']!='PASS':
                    route.terminal(result.get('error','FAILED_SCENE'))
                    # A failure audit needs a specific supported predicate and
                    # trustworthy model, not merely a second FAIL.
                    from .failure_evidence import eligible_failure
                    report=read(folder/'independent-result.json') if (folder/'independent-result.json').exists() else {}
                    if eligible_failure(result,report):
                        with transaction() as (_,s):
                            route._owned(s);assert_scope(s,540,allow_affected=True)
                            if s['counts']['failure_audit']>=1: raise Stop('FAILURE_AUDIT_USED')
                            s['counts']['failure_audit']+=1
                        route.audit_mode=True
                        audited,auditdir=execute_scene(route,manifest,graph,binding,name,True,1,folder)
                        audit=read(auditdir/'independent-result.json').get('failure_evidence') if (auditdir/'independent-result.json').exists() else {'status':'UNKNOWN'}
                        route.audit_mode=False
                    outcome=make_outcome(results,failure_audit=audit)
                    enrich_outcome(outcome,task,proposal,owner,output_dir)
                    digest=write_outcome(Path(output_dir)/'outcome.json',outcome)
                    return dict(outcome,outcome_manifest_path=str(Path(output_dir)/'outcome.json'),outcome_manifest_sha256=digest)
                if not refined: base=folder
            route.set_gate(name,'PASS')
        outcome=make_outcome(results)
        route.terminal('ALL_FIXED_TASK_GATES_VERIFIED','CLOSED_COMPLETED_FIXED_CASE')
    except Exception as exc:
        error=str(exc);route.terminal(error,'STOPPED_AFFECTED_EXECUTION')
        outcome=make_outcome(results,failure_audit=audit,error=error)
    enrich_outcome(outcome,task,proposal,owner,output_dir)
    digest=write_outcome(Path(output_dir)/'outcome.json',outcome)
    return dict(outcome,outcome_manifest_path=str(Path(output_dir)/'outcome.json'),outcome_manifest_sha256=digest)


def enrich_outcome(outcome,task,proposal,owner,output_dir):
    from week11.src.store import digest_json
    folder=Path(output_dir).resolve()
    files={}
    for p in sorted(folder.rglob('*')):
        if p.is_file() and p.name!='outcome.json' and p.suffix in ('.json','.csv','.mo','.sysml','.mos','.log'):
            if p.name.lower()=='.env': raise Stop('CREDENTIAL_ARTIFACT_FORBIDDEN')
            p.resolve().relative_to(folder)
            with p.open('rb') as stream:
                digest=hashlib.file_digest(stream,'sha256').hexdigest()
            files[p.relative_to(folder).as_posix()]={'sha256':digest,'bytes':p.stat().st_size}
    outcome.update(project_id=task['project_id'],task_id=task['task_id'],proposal_id=task['proposal_id'],
        spec_digest=proposal.get('spec_digest',digest_json(proposal['spec'])),physical_spec_sha256=PHYSICAL_SHA,
        owner=owner,files=files,scope_proposal_sha256=CONTRACT_SHA)
