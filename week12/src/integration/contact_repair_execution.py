"""Isolated contact-development batch; no vehicle execution or automatic approval."""
import os
from pathlib import Path
import re
import sys
import unittest
from .execution import ROOT,read,save,sha,Batch,dependency_audit,require_sources
from .review import check_review_receipt
from ..composition.schema import digest
from ..component_physics.contract import fixture
from ..component_physics.runner import build_script as b_script,build_observations,OMC,MSL

REVIEW=ROOT/'results/s3-c-evidence-review-20260926-01'
OUT=ROOT/'results/s3-c-contact-repair-execution-20260926-01'
CANDIDATE=ROOT/'models/components/V11ContactC2/package.mo'
B_SOURCE=ROOT/'models/components/V11PhysicalB1/package.mo'
CASE_ORDER=['T01','T02','T03','T04','B02','B03','B04','B05','B06']


def review_inputs():
    from .timing_amendment import inputs as protected_vehicle_inputs
    protected_vehicle_inputs()  # Read-only protection of A/B/user originals and old approvals.
    packet=read(REVIEW/'execution-review-packet.json')
    data=dict(packet); specification=data.pop('specification_digest')
    if digest(data)!=specification: raise ValueError('CONTACT_REVIEW_PACKET_DRIFT')
    for entry in packet['fixed_inputs']:
        if sha(ROOT/entry['path'])!=entry['sha256']: raise ValueError('FIXED_INPUT_DRIFT')
    protocol=read(REVIEW/'contact-repair-proposal.json')
    raw=dict(protocol); key=raw.pop('proposal_digest')
    if digest(raw)!=key or key!=packet['proposal_digest']: raise ValueError('PROPOSAL_DRIFT')
    return packet,protocol


def initialize(receipt_path):
    packet,protocol=review_inputs(); receipt=read(receipt_path)
    check_review_receipt(receipt,packet)
    require_sources(packet['implementation_manifest'])
    OUT.mkdir(exist_ok=False)
    save(OUT/'authorization.json',receipt); save(OUT/'approved-packet.json',packet)
    proposed=protocol['budget_proposal']
    save(OUT/'policy.json',{'budget':{'maximum_rounds':3,'maximum_scene_attempts':54,
        'new_local_execution_checks_limit':18,'sysml_parse_limit':0,'identity_probe_limit':0,
        'execution_total_wall_s':1800},'approved_budget_proposal':proposed,
        'scope':'contact development only; never execute any car model','prior_C_local_attempts':54,
        'no_budget_carry_over':True})


def local_targets():
    def flatten(suite):
        for v in suite:
            if isinstance(v,unittest.TestSuite): yield from flatten(v)
            else: yield v.id()
    names=[]
    for mod in ('week12.tests.test_c_contact_probe','week12.tests.test_c_contact_repair'):
        names+=list(flatten(unittest.defaultTestLoader.loadTestsFromName(mod)))
    if len(names)!=6 or any('_FailedTest' in n for n in names): raise ValueError('SIX_PREFLIGHT_GROUPS_REQUIRED')
    return names


def isolated_b():
    source=B_SOURCE.read_text(encoding='utf-8')
    start=source.index('  model PolylineWheelContact\n'); end=source.index('  end PolylineWheelContact;',start)+len('  end PolylineWheelContact;')
    return source[:start]+'  model PolylineWheelContact = V11ContactC2.PolylineWheelContact;'+source[end:]


def build_script(ident,refined):
    insertion='c2 := loadFile("V11ContactC2.mo");\nprint(getErrorString()+"\\n");\nif not c2 then exit(1); end if;\n'
    if ident.startswith('B'):
        return b_script(ident,refined=refined).replace('loadedFile :=',insertion+'loadedFile :=',1)
    from .execution import build_script as c_script
    script=c_script('V11ContactC2Probes.'+ident,.01,'^(time|x|z|vx|vz|omega|Fx|Fz|normal|penetration|slip|elasticEnergy|normalLoss|frictionLoss|featureId|contactCount)$',refined)
    script=script.replace('ok := loadFile("V11PhysicalB1.mo");',insertion+'ok := loadFile("V11PhysicalB1.mo");',1)
    return script.replace('numberOfIntervals=10,','numberOfIntervals=1000,')


def ownership_audit(directory,ident):
    """Require observable shared Boolean consumers in generated equations.

    Unrecognized compiler lowering fails closed for manual diagnosis; cached
    relation uniqueness alone is insufficient to prove endpoint ownership.
    """
    if ident in ('B02','B03'):
        return {'status':'NOT_APPLICABLE_NO_CONVEX_CORNER','scope':'flat fixtures'}
    blocks=[]
    for path in directory.glob('model*.c'):
        source=path.read_text(encoding='utf-8')
        blocks+=re.findall(r'/\*\s*equation index:.*?\*/\s*void\s+\w+\([^)]*\)\s*\{.*?TRACE_POP\s*\}',source,re.S)
    vertex=[b for b in blocks if re.search(r'\b\S*vertexActive\[\d+\] =',b) and 'distanceVertex' in b]
    witnessed=[]
    for block in vertex:
        body=block.split('*/',1)[1]
        owned=('afterStart[' in body and 'beforeEnd[' in body and 'booleanVars' in body)
        old_geometry='vertexNx[' in body or 'vertexNz[' in body
        witnessed.append(owned and not old_geometry)
    return {'status':'PASS' if witnessed and all(witnessed) else 'INSUFFICIENT_SHARED_OWNER_EVIDENCE',
            'convex_equations_observed':len(vertex),'all_share_boolean_owner':bool(witnessed) and all(witnessed),
            'scope':'generated vertex consumes shared endpoint Booleans; source and local checks require face complements; no full compiler proof'}


def one_scene(batch,directory,ident,refined,round_no,manifest):
    from ..toolchain import _resolve_generated
    from ..component_physics.event_codegen import audit_source
    require_sources(manifest)
    name=ident+('-refined' if refined else '-base')
    batch.reserve('scene',name,round_no)
    folder=directory/name; folder.mkdir(exist_ok=False)
    result={'case':name,'status':'FAIL','compile':'NOT_RUN','simulation':'NOT_RUN','physical':'UNVERIFIED','numeric':'UNVERIFIED'}
    try:
        (folder/'V11ContactC2.mo').write_bytes(CANDIDATE.read_bytes())
        (folder/'V11PhysicalB1.mo').write_text(isolated_b(),encoding='utf-8',newline='\n')
        (folder/'V11CGenerated.mo').write_bytes((REVIEW/'V11ContactC2Probes.mo').read_bytes())
        save(folder/'binding.json',{'candidate_sha256':sha(CANDIDATE),'original_B_sha256':sha(B_SOURCE),
            'isolated_adapted_B_sha256':sha(folder/'V11PhysicalB1.mo'),'not_original_B_acceptance':True})
        if ident.startswith('B'): save(folder/'descriptor.json',fixture(ident))
        (folder/'build.mos').write_text(build_script(ident,refined),encoding='utf-8')
        proc=batch.run([str(OMC),'build.mos'],folder,180,folder,'build'); result.update(build_observations(proc['stdout']))
        if proc['returncode']!=0 or proc['timed_out'] or result['model_check']!='PASS': raise RuntimeError('BUILD_FAILURE')
        if result['observed_msl_version']!='4.0.0' or Path(result['observed_msl_source']).resolve()!=MSL.resolve(): raise RuntimeError('DEPENDENCY_DRIFT')
        executable=_resolve_generated(folder,result['reported_executable'])
        if executable is None: raise RuntimeError('NO_EXECUTABLE')
        result['compile']='PASS'
        event=audit_source((folder/'model_05evt.c').read_text(encoding='utf-8'))
        owner=ownership_audit(folder,ident)
        save(folder/'event-cache-audit.json',event); save(folder/'owner-audit.json',owner)
        if event['status']!='PASS' or owner['status'] not in ('PASS','NOT_APPLICABLE_NO_CONVEX_CORNER'):
            raise RuntimeError('GENERATED_EVENT_OWNER_UNVERIFIED')
        step=(5e-6 if refined else 1e-5) if ident.startswith('T') else (.0005 if refined else .001)
        result['simulation']='LAUNCH_REQUESTED'
        save(folder/'launch.json',{'executable':str(executable),'max_step_s':step})
        proc=batch.run([str(executable),'-s=dassl',f'-maxStepSize={step}','-r=result.csv'],folder,180,folder,'simulation')
        if proc['returncode']!=0 or proc['timed_out']: raise RuntimeError('SIMULATION_FAILURE')
        result['simulation']='COMPLETED'
        module='week12.scripts.s3_c_contact_probe_verify' if ident.startswith('T') else 'week12.scripts.s3_b_verify'
        command=[sys.executable,'-B','-m',module,ident,str(folder/'result.csv')]
        if refined: command+=['--base',str(directory/(ident+'-base')/'result.csv')]
        proc=batch.run(command,ROOT.parent,60,folder,'verification')
        for key,file in [('physical','physical.json'),('numeric','numerical.json')]:
            if (folder/file).exists(): result[key]=read(folder/file)['status']
        if proc['returncode'] or proc['timed_out'] or result['physical']!='PASS' or refined and result['numeric']!='PASS':
            raise RuntimeError('PHYSICAL_NUMERIC_OR_POSTPROCESS_FAILURE')
        result['status']='PASS'
    except Exception as exc: result['error']=str(exc)
    save(folder/'result.json',result); print(name,result['status'],result.get('error',''),flush=True)
    return result


def run_round(number):
    packet,protocol=review_inputs(); check_review_receipt(read(OUT/'authorization.json'),packet)
    if not 1<=number<=3: raise ValueError('ROUND_LIMIT')
    manifest=packet['implementation_manifest']
    if number>1:
        previous=read(OUT/f'round-{number-1:02d}/batch-result.json')
        if previous['status']!='STOPPED': raise ValueError('NO_RETRY_AFTER_SUCCESS')
        repair=read(OUT/f'round-{number-1:02d}/repair.json')
        if repair.get('classification')!='implementation' or repair.get('proposal_digest')!=protocol['proposal_digest'] or not repair.get('reason'):
            raise ValueError('BOUNDED_IMPLEMENTATION_REPAIR_REQUIRED')
        manifest=repair['rebound_implementation_manifest']
        if manifest==read(OUT/f'round-{number-1:02d}/source-manifest.json'): raise ValueError('NO_BLIND_RETRY')
        if {r['path'] for r in manifest}!={r['path'] for r in packet['implementation_manifest']}: raise ValueError('INCOMPLETE_REPAIR_MANIFEST')
    require_sources(manifest)
    batch=Batch(OUT,packet['specification_digest']); usage=batch.usage()
    if usage['scene']+18>54 or usage['local']+6>18: raise ValueError('NO_COMPLETE_ROUND_BUDGET')
    lock=OUT/'.execution.lock'; fd=os.open(lock,os.O_EXCL|os.O_CREAT|os.O_WRONLY); os.write(fd,str(os.getpid()).encode()); os.close(fd)
    directory=OUT/f'round-{number:02d}'; owns=False
    result={'round':number,'status':'PENDING','local':[],'scenes':[],'scope':'contact development only'}
    try:
        directory.mkdir(exist_ok=False); owns=True
        save(directory/'source-manifest.json',manifest)
        for row in manifest:
            src=Path(row['path']); dest=directory/'source-snapshot'/src.relative_to(ROOT)
            dest.parent.mkdir(parents=True,exist_ok=True); dest.write_bytes(src.read_bytes())
        dependency_audit(directory)
        for i,target in enumerate(local_targets(),1):
            batch.reserve('local',target,number)
            proc=batch.run([sys.executable,'-B','-m','unittest',target],ROOT.parent,10,directory,f'local-{i}')
            passed=proc['returncode']==0 and not proc['timed_out']; result['local'].append({'test':target,'pass':passed})
            if not passed: raise RuntimeError('PREFLIGHT_FAILURE')
        for ident in CASE_ORDER:
            for refined in (False,True):
                row=one_scene(batch,directory,ident,refined,number,manifest); result['scenes'].append(row)
                if row['status']!='PASS': raise RuntimeError('FIRST_FAILURE_REQUIRES_DIAGNOSIS')
        result['status']='PASS_CONTACT_VARIANT_NOT_VEHICLE_ACCEPTANCE'
    except BaseException as exc: result.update(status='STOPPED',error=str(exc))
    finally:
        result['usage']=batch.usage()
        if owns: save(directory/'batch-result.json',result)
        if lock.is_file() and lock.read_text()==str(os.getpid()): lock.unlink()
    return result
