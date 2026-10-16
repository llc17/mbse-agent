"""Single approved C batch: immutable reservations, bounded tools, no silent retry."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import sys

from week12.src.component_physics.contract import ROOT, require_dependency_hashes
from week12.src.composition.schema import digest
from week12.src.integration.review import check_review_receipt, review_identity

REVIEW = ROOT/'results/s3-c-user-vehicle-review-20260924-01'
OUT = ROOT/'results/s3-c-user-vehicle-execution-20260924-01'
APPROVED_SPEC = '9d218678a4eb7976e11775d0efe159d3f1ec6a857d8af0eaa6ed198ccd32c409'
MESSAGE = '确实上述方案，继续生成和验证'


def read(path): return json.loads(Path(path).read_text(encoding='utf-8'))


def save(path, value):
    with Path(path).open('x',encoding='utf-8',newline='\n') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False)
        stream.write('\n')


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def approved_inputs():
    packet = read(REVIEW/'review-packet.json')
    raw = dict(packet); checksum = raw.pop('packet_digest')
    if digest(raw)!=checksum or packet['confirmation_identity']['specification_digest']!=APPROVED_SPEC:
        raise ValueError('REVIEW_IDENTITY_DRIFT')
    for row in packet['files']:
        if sha(ROOT/row['path'])!=row['sha256']: raise ValueError('REVIEW_FILE_DRIFT: '+row['path'])
    for name, expected in packet['protected_inputs'].items():
        if sha(ROOT/name)!=expected: raise ValueError('PROTECTED_FILE_DRIFT: '+name)
    bundle = read(REVIEW/'round-02/proposal.json')
    contract = read(REVIEW/'round-02/verification-contract.json')
    if review_identity([bundle],contract)!=packet['confirmation_identity']:
        raise ValueError('ACTUAL_SPECIFICATION_DRIFT')
    return packet,bundle,contract


def initialize():
    packet,bundle,contract = approved_inputs()
    OUT.mkdir(exist_ok=False)
    receipt={'origin':'user_message','human_confirmed':True,
        'message_reference':'current conversation user message on2026-09-24, immediately after discussion of C reuse',
        'user_text':MESSAGE,'scope':packet['confirmation_identity']['scope'],
        'specification_digest':APPROVED_SPEC,
        'interpretation':'Explicit approval of the immediately preceding single-vehicle completion and finite execution proposal; original user wording retained'}
    check_review_receipt(receipt,packet['confirmation_identity'])
    save(OUT/'authorization.json',receipt)
    save(OUT/'approved-packet.json',packet)
    save(OUT/'proposal.json',bundle); save(OUT/'verification-contract.json',contract)
    save(OUT/'policy.json',{'budget':contract['budget_proposal'],'prior_C_local_attempts':30,
        'ordinary_implementation_repair':'automatic within same physical specification and finite totals',
        'stop':['physical/task/numerical hard failure','specification/dependency drift','budget exhausted','uncertain process cleanup'],
        'scope':'one fixed-nose vehicle plus2 extension probes; no rocker/D/E/scans/V12',
        'execution_time_accounting':'sum actual bounded child-process wall time including failed tools; preparation separately excluded; unresolved launches block resume',
        'rounds_include_local_repairs':True,'no_credit_from_closed_preparation':True})
    save(OUT/'initial-state.json',{'status':'APPROVED_IMPLEMENTATION_IN_PROGRESS',
         'scene_attempts':0,'local_checks':0,'sysml':0,'identity_probes':0,'online':0})
    return receipt


class Batch:
    def __init__(self,directory=OUT,specification_digest=APPROVED_SPEC):
        self.root=Path(directory)
        self.specification_digest=specification_digest
        self.policy=read(self.root/'policy.json')
        self.budget=self.policy['budget']

    def usage(self):
        rows=[read(p) for p in sorted(self.root.glob('reservation-*.json'))]
        kinds={k:sum(r['kind']==k for r in rows) for k in ('scene','local','sysml','probe')}
        finished=list(self.root.glob('process-*.result.json'))
        started=list(self.root.glob('process-*.start.json'))
        kinds['process_seconds']=sum(read(p)['duration_s'] for p in finished)
        # A returned process record does not prove its process tree was cleaned.
        kinds['unresolved_processes']=len(started)-len(finished)+sum(bool(read(p).get('uncertain')) for p in finished)
        return kinds

    def reserve(self,kind,label,round_no):
        usage=self.usage()
        if usage['unresolved_processes']: raise RuntimeError('UNKNOWN_PREVIOUS_PROCESS_BLOCKS_RESUME')
        limits={'scene':self.budget['maximum_scene_attempts'], 'local':self.budget['new_local_execution_checks_limit'],
                'sysml':self.budget['sysml_parse_limit'],'probe':self.budget['identity_probe_limit']}
        if kind not in limits or usage[kind]>=limits[kind] or not 1<=round_no<=self.budget['maximum_rounds']:
            raise RuntimeError('FINITE_BUDGET_EXHAUSTED')
        if usage['process_seconds']>=self.budget['execution_total_wall_s']:
            raise RuntimeError('TOTAL_EXECUTION_TIME_EXHAUSTED')
        number=len(list(self.root.glob('reservation-*.json')))+1
        value={'kind':kind,'label':label,'round':round_no,'ordinal':number,'specification_digest':self.specification_digest}
        save(self.root/f'reservation-{number:03d}.json',value)
        return value

    def run(self,command,cwd,timeout,log_dir,label):
        from week12.src.toolchain import _run
        usage=self.usage()
        if usage['unresolved_processes']: raise RuntimeError('UNRESOLVED_PROCESS')
        remaining=self.budget['execution_total_wall_s']-usage['process_seconds']
        if remaining<=0: raise RuntimeError('TOTAL_EXECUTION_TIME_EXHAUSTED')
        number=len(list(self.root.glob('process-*.start.json')))+1
        save(self.root/f'process-{number:03d}.start.json',{'command':command,'cwd':str(cwd),'timeout_s':min(timeout,remaining),'label':label})
        result=_run(command,Path(cwd),min(timeout,remaining))
        for stream in ('stdout','stderr'):
            with (Path(log_dir)/(label+'.'+stream+'.log')).open('x',encoding='utf-8') as handle: handle.write(result[stream])
        summary={k:v for k,v in result.items() if k not in ('stdout','stderr')}
        summary.update(label=label,command=command)
        save(self.root/f'process-{number:03d}.result.json',summary)
        if result['uncertain'] or result['interrupted']: raise RuntimeError('PROCESS_INTERRUPTED_OR_CLEANUP_UNCERTAIN')
        return result


def build_script(model_class,stop,variable_filter,refined):
    from week12.src.component_physics.runner import COMPLEX,SERVICES,MSL
    lines=[]
    for label,path in [('COMPLEX',COMPLEX),('SERVICES',SERVICES),('MSL',MSL),('B_LIBRARY',Path('V11PhysicalB1.mo')),('LOAD_FILE',Path('V11CGenerated.mo'))]:
        lines += [f'ok := loadFile("{path.as_posix()}");', f'print("__B_{label}__="+String(ok)+"\\n");',
                  'print(getErrorString()+"\\n");','if not ok then exit(1); end if;']
    lines += ['print("__B_MSL_VERSION__="+getVersion(Modelica)+"\\n");',
              'print("__B_MSL_SOURCE__="+getSourceFile(Modelica)+"\\n");',
              f'check := checkModel({model_class});','print("__B_CHECK__="+check+"\\n");',
              'print(getErrorString()+"\\n");','if not regexBool(check,"completed successfully") then exit(1); end if;',
              'print("__B_BUILD_STARTED__=true\\n");',
              f'build := buildModel({model_class},startTime=0,stopTime={stop:.17g},numberOfIntervals={round(stop/.001)},tolerance={1e-7 if refined else 1e-6:.17g},method="dassl",outputFormat="csv",fileNamePrefix="model",variableFilter="{variable_filter}");',
              'print("__B_BUILD_EXE__="+build[1]+"\\n");','print(getErrorString()+"\\n");']
    return '\n'.join(lines)+'\n'


def freeze_sources(directory):
    paths=[*sorted((ROOT/'src/integration').glob('*.py')),ROOT/'scripts/s3_c_execute.py',
           ROOT/'scripts/s3_c_verify.py',ROOT/'src/toolchain.py',ROOT/'src/component_physics/event_codegen.py',
           ROOT/'src/component_physics/contract.py',ROOT/'src/component_physics/runner.py',
           ROOT/'tests/test_c_execution.py',ROOT/'tests/test_c_model_generator.py',ROOT/'tests/test_c_vehicle_verify.py']
    manifest=[]
    snapshots=directory/'source-snapshot'; snapshots.mkdir()
    for path in paths:
        relative=path.relative_to(ROOT)
        dest=snapshots/relative; dest.parent.mkdir(parents=True,exist_ok=True)
        with dest.open('xb') as stream: stream.write(path.read_bytes())
        manifest.append({'path':str(path),'sha256':sha(path)})
    save(directory/'source-manifest.json',manifest)
    return manifest


def require_sources(manifest):
    for row in manifest:
        if sha(row['path'])!=row['sha256']: raise RuntimeError('IMPLEMENTATION_DRIFT_DURING_ROUND')


def dependency_audit(directory):
    from week12.src.component_physics.runner import LIBRARIES,OMC
    previous=read(ROOT/'results/s3-b-auto-repair-20260923-02/round-01/source-manifest.json')
    # Read only previously enumerated installed-library/compiler paths.
    prefix=str(LIBRARIES).replace('\\','/').lower()+'/'
    unique={}
    for row in previous:
        name=row['path'].replace('\\','/')
        if name.lower().startswith(prefix) or name.lower()==str(OMC).replace('\\','/').lower():
            unique[name]=row
    if len(unique)<4000: raise RuntimeError('INCOMPLETE_PINNED_DEPENDENCY_MANIFEST')
    require_dependency_hashes(list(unique.values()))
    save(directory/'dependency-audit.json',{'status':'UNCHANGED_PINNED_B_DEPENDENCIES','files':len(unique),
         'manifest_sha256':sha(ROOT/'results/s3-b-auto-repair-20260923-02/round-01/source-manifest.json'),
         'identity_tool_probes_used':0,'not_a_new_B_test':True})


def execute_scene(batch,directory,bundle,contract,case_id,refined,round_no,manifest,base=None):
    from week12.src.integration.model_generator import generate
    from week12.src.component_physics.runner import PACKAGE,OMC,MSL,build_observations
    from week12.src.component_physics.event_codegen import audit_source
    from week12.src.toolchain import _resolve_generated
    require_sources(manifest)
    case=next((c for c in bundle['physical']['cases'] if c['id']==case_id),None)
    stop=case['stop_time_s'] if case else next(c['stop_time_s'] for c in contract['probes'] if c['id']==case_id)
    name=case_id+('-refined' if refined else '-base')
    reservation=batch.reserve('scene',name,round_no)
    run_dir=directory/name; run_dir.mkdir(exist_ok=False)
    result={'case':case_id,'variant':'refined' if refined else 'base','status':'FAIL',
            'reservation':reservation,'compile':'NOT_RUN','simulation':'NOT_RUN','physical':'UNVERIFIED','numeric':'UNVERIFIED'}
    try:
        generated=generate(bundle,case_id)
        (run_dir/'V11CGenerated.mo').write_text(generated['modelica'],encoding='utf-8',newline='\n')
        (run_dir/'V11PhysicalB1.mo').write_bytes(PACKAGE.read_bytes())
        save(run_dir/'trace.json',generated['trace'])
        save(run_dir/'input-identity.json',{'specification_digest':batch.specification_digest,'implementation_digest':digest(manifest),
            'design_digest':bundle['design']['design_digest'],'physical_digest':bundle['physical']['physical_digest'],
            'modelica_sha256':sha(run_dir/'V11CGenerated.mo')})
        (run_dir/'build.mos').write_text(build_script(generated['model_class'],stop,generated['variable_filter'],refined),encoding='utf-8',newline='\n')
        build=batch.run([str(OMC),'build.mos'],run_dir,180,run_dir,'build')
        result.update(build_observations(build['stdout']))
        if build['returncode']!=0 or build['timed_out']: raise RuntimeError('BUILD_TOOL_FAILURE')
        if result['model_check']!='PASS': raise RuntimeError('MODEL_CHECK_FAILED')
        if result['observed_msl_version']!='4.0.0' or Path(result['observed_msl_source']).resolve()!=MSL.resolve():
            raise RuntimeError('MSL_IDENTITY_DRIFT')
        exe=_resolve_generated(run_dir,result['reported_executable'])
        if exe is None: raise RuntimeError('NO_GENERATED_EXECUTABLE')
        result['compile']='PASS'
        event=audit_source((run_dir/'model_05evt.c').read_text(encoding='utf-8'))
        if case is None and event['issues']==['No cached zero-crossing relations observed']:
            event['status']='NOT_APPLICABLE_NO_RELATIONS_IN_ANALYTIC_PROBE'
        save(run_dir/'event-codegen.json',event)
        result['event_codegen']=event['status']
        if not (event['status']=='PASS' or event['status'].startswith('NOT_APPLICABLE')):
            raise RuntimeError('EVENT_CACHE_GUARD_FAILURE')
        result['simulation']='LAUNCH_REQUESTED'
        save(run_dir/'simulation-launch.json',{'executable':str(exe),'specification_digest':batch.specification_digest})
        simulation=batch.run([str(exe),'-s=dassl',f'-maxStepSize={.0005 if refined else .001}','-r=result.csv'],run_dir,180,run_dir,'simulation')
        result['simulation']='COMPLETED' if simulation['returncode']==0 and not simulation['timed_out'] else 'FAILED_OR_TIMED_OUT'
        if result['simulation']!='COMPLETED': raise RuntimeError('SIMULATION_FAILURE_REQUIRES_DIAGNOSIS')
        command=[sys.executable,'-B','-m','week12.scripts.s3_c_verify','--proposal',str(directory/'proposal.json'),
                 '--contract',str(directory/'verification-contract.json'),'--case',case_id,'--csv',str(run_dir/'result.csv')]
        if base: command+=['--base',str(base/'result.csv')]
        post=batch.run(command,ROOT.parent,60,run_dir,'verification')
        for label,file in [('physical','physical.json'),('numeric','numerical.json')]:
            if (run_dir/file).exists(): result[label]=read(run_dir/file)['status']
        if post['returncode']!=0 or post['timed_out']: raise RuntimeError('PHYSICAL_NUMERIC_OR_POSTPROCESS_FAILURE')
        if result['physical']!='PASS' or (refined and result['numeric']!='PASS'):
            raise RuntimeError('INCOMPLETE_PHYSICAL_OR_NUMERIC_EVIDENCE')
        result['status']='PASS'
    except Exception as exc:
        result['error']=type(exc).__name__+': '+str(exc)
    finally:
        save(run_dir/'result.json',result)
    print(name,result['status'],result.get('error',''),flush=True)
    return result,run_dir
