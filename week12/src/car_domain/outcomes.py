"""Conservative vehicle results. Failed checks never imply trusted vehicle failure."""
from pathlib import Path
import hashlib
import json

SCENE_ORDER = ('active_joint_energy','one_twin_module_load_cycle','flat_energy_and_brake',
               'three_up','three_forward_down','twenty_up','twenty_forward_down')
REQUIRED_SEQUENCE = tuple((name,variant) for name in SCENE_ORDER for variant in ('base','refined'))


def scene_classification(result, report):
    physical=report.get('physical',{})
    checks=physical.get('checks',[])
    if result.get('compile')!='PASS' or result.get('simulation')!='COMPLETED':
        return 'TOOL_FAIL'
    if (result.get('verification_returncode') not in (0,2)
            or result.get('verification_timed_out') is not False
            or result.get('verification_uncertain') is not False):
        return 'TOOL_FAIL'
    if not checks or any(c.get('status') not in {'PASS','FAIL'} for c in checks):
        return 'EVIDENCE_INCOMPLETE'
    # Keep the raw failed predicates. No substring whitelist can establish trust.
    if any(c.get('status')=='FAIL' for c in checks):
        return 'CHECK_FAILED_VEHICLE_CONCLUSION_UNKNOWN'
    if physical.get('status')!='PASS':return 'EVIDENCE_INCOMPLETE'
    if result.get('variant')=='refined' and report.get('numeric',{}).get('status')!='PASS':
        return 'NUMERIC_UNTRUSTED'
    if result.get('verification_returncode')!=0:return 'TOOL_FAIL'
    return 'PASS'


def complete_scene_set(results):
    if tuple((r.get('scene'),r.get('variant')) for r in results)!=REQUIRED_SEQUENCE:return False
    for r in results:
        if (any(r.get(k)!=v for k,v in dict(status='PASS',compile='PASS',simulation='COMPLETED',physical='PASS',verification_returncode=0).items())
                or r.get('verification_timed_out') is not False
                or r.get('verification_uncertain') is not False
                or (r['variant']=='refined' and r.get('numeric')!='PASS')):
            return False
    return True


def make_outcome(results, *, failure_audit=None, error=None):
    # This is a scene summary, not authorization or permission to publish.
    # The Store must separately verify actual worker exit, budget and artifacts.
    failed=next((r for r in results if r.get('status')!='PASS'),None)
    all_pass=complete_scene_set(results) and not error
    chain='TRUSTED_PHYSICAL_PASS' if all_pass else 'BLOCKED_TOOL_OR_NUMERIC'
    vehicle='PASS' if all_pass else 'UNKNOWN'
    return {'schema_version':'v11-car-outcome-1','agent_execution_status':'COMPLETED' if all_pass else 'BLOCKED',
        'chain_status':chain,'vehicle_task_status':vehicle,
        'tool_status':'PASS' if all_pass else (failed or {}).get('classification','UNKNOWN'),
        'numerical_status':'PASS' if all_pass else (failed or {}).get('numeric','NOT_RUN'),
        'evidence_status':'COMPLETE' if all_pass else 'INCOMPLETE_OR_UNTRUSTED',
        'results':results,'failure_audit':failure_audit,'failure_audit_promotes_result':False,
        'error':error or (failed or {}).get('error'),'published_version_id':None,'whole_V11_complete':False,
        'component_failure_is_whole_vehicle_chain':False}


def write_outcome(path, outcome):
    path=Path(path)
    path.write_text(json.dumps(outcome,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf8')
    return hashlib.sha256(path.read_bytes()).hexdigest()
