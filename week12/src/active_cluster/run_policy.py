"""Pure, versioned execution policy. No launch or vehicle physics here."""
from datetime import datetime, timezone
import math
from .execution import Stop, ORDER

EXPECTED_CLASSES = dict(zip(ORDER, ('V11GeneratedActive.Probe',
    'V11GeneratedActive.Module','V11GeneratedActive.Flat','V11GeneratedActive.ThreeUp',
    'V11GeneratedActive.ThreeForwardDown','V11GeneratedActive.TwentyUp',
    'V11GeneratedActive.TwentyForwardDown')))
OLD_CAPS = dict(future_cap_s=23400, implementation_analysis_cap_s=12600,
               process_cap_s=10800, other_check_cap=17, repair_rounds_cap=3,
               source_revision_round_cap=4, scene_cap=42, parse_cap=3)

def finite(value):
    value=float(value)
    if not math.isfinite(value) or value<0:
        raise Stop('INVALID_ACCOUNTING_VALUE')
    return value

def active_seconds(intervals, now):
    total=0.; previous=None
    for row in intervals:
        begin=datetime.fromisoformat(row['started_utc'].replace('Z','+00:00'))
        end=datetime.fromisoformat(row['ended_utc'].replace('Z','+00:00')) if row.get('ended_utc') else now
        if begin.tzinfo is None or end.tzinfo is None or end<begin or end>now or (previous and begin<previous):
            raise Stop('INVALID_ACTIVE_INTERVAL')
        seconds=(end-begin).total_seconds()
        if row.get('excluded_wait'):
            if not row.get('independent_wait_evidence'):
                raise Stop('UNPROVEN_WAIT_EXCLUSION')
        else: total+=seconds
        previous=end
    return total

def validate_history(base,data):
    for key,value in OLD_CAPS.items():
        if data.get(key)!=value or base.get(key)!=value:
            raise Stop('ORIGINAL_CAP_HISTORY_DRIFT')
    for key in ('scene_attempts','parse_attempts','other_checks','repairs','source_bindings','active_intervals'):
        if data[key][:len(base[key])]!=base[key]:
            raise Stop('HISTORICAL_PREFIX_DRIFT:'+key)
    for key in ('started_utc','prepare_reserved_s','prepare_actual_s','prior_C_local',
                'approval_sha256','proposal_sha256','generator_readiness_diagnostic','vehicle_recovery'):
        if data.get(key)!=base.get(key):
            raise Stop('HISTORICAL_FIELD_DRIFT:'+key)
    for key in ('other_checks','parse_attempts','repairs'):
        if len(data[key])!=len(base[key]):
            raise Stop('NO_REOPENED_OLD_SCOPE:'+key)

def validate_approval(proposal,receipt,proposal_sha,base_sha):
    if (receipt.get('status')!='APPROVED_BY_USER' or receipt.get('user_exact')!='批准'
        or receipt.get('proposal_sha256')!=proposal_sha
        or receipt.get('baseline_ledger_sha256')!=base_sha
        or receipt.get('allocation')!=proposal['allocation']):
        raise Stop('COMPLETE_SCOPE_APPROVAL_INVALID')
    changes=receipt['allocation']['prospective_changes']
    if (changes['implementation_analysis_cumulative_cap_s']!={'from':12600,'to':16400,'delta':3800}
        or changes['external_process_cumulative_cap_s']!={'from':10800,'to':7000,'delta':-3800}):
        raise Stop('ALLOCATION_NOT_APPROVED')

def cumulative_process(base_process,scene_durations,launchers,live=None,frontend=0):
    total=finite(base_process)+sum(finite(x) for x in scene_durations)+finite(frontend)
    for row in launchers:
        if row.get('status')=='CLAIMED':
            if live is None or live.get('nonce')!=row['nonce']:
                raise Stop('UNKNOWN_LIVE_LAUNCHER')
            total+=max(0,finite(live['elapsed_s'])-finite(live['child_s']))
        elif row.get('actual_external_overhead_s') is not None:
            total+=finite(row['actual_external_overhead_s'])
        elif row.get('status') in ('RETURNED','FAILED_START','UNCERTAIN'):
            # Unreconciled wrapper duration cannot be treated as zero.
            total+=finite(row['reserved_parent_s'])
    return total

def next_scene(rows,round_no,gates,label):
    expected=[name+'-'+variant for name in ORDER for variant in ('base','refined')]
    current=[r for r in rows if r['round']==round_no]
    if len(current)>=14 or label!=expected[len(current)] or any(r['status']!='PASS' for r in current):
        raise Stop('FIXED_ORDER_OR_RETRY_VIOLATION')
    if label.startswith('twenty') and any(gates.get(n)!='PASS' for n in ORDER[:5]):
        raise Stop('BOTH_SMALL_DIRECTIONS_REQUIRED')

def validate_frontend(proof,model_sha,class_map,result_sha,actual):
    if not isinstance(proof,dict) or proof.get('model_sha256')!=model_sha or proof.get('result_sha256')!=result_sha:
        raise Stop('FRONTEND_PROOF_INVALID')
    if class_map!=EXPECTED_CLASSES or proof.get('classes')!=EXPECTED_CLASSES:
        raise Stop('FRONTEND_CLASS_MAP_DRIFT')
    front=actual.get('frontend',{})
    classes=front.get('classes',[])
    if (actual.get('status')!='PASS_FRONTEND_ONLY' or front.get('status')!='PASS_FRONTEND_ONLY'
        or not front.get('pinned_identity_verified')
        or front.get('dependency_loads')!=[True]*7
        or [r.get('class') for r in classes]!=list(EXPECTED_CLASSES.values())
        or any(r.get('status')!='PASS' for r in classes)):
        raise Stop('ACTUAL_FRONTEND_EVIDENCE_INVALID')
    return True

def reserve_whole_sets(used_process,active,scope_process,sets,helper_used=0):
    # Spent helpers are already in scope_process; reserve only the unspent part.
    reserve=sets*3180+max(0,240-finite(helper_used))
    if 7000-used_process<reserve or 23400-active<reserve or 6600-scope_process<reserve:
        raise Stop('NO_COMPLETE_AFFECTED_SET_AND_HELPER_RESERVE')

def allow_repair(records,proof):
    if records or proof.get('classification')!='PROVEN_IMPLEMENTATION_DEFECT' or proof.get('root_review')!='ACCEPTED' or proof.get('physical_control_task_unchanged') is not True:
        raise Stop('NO_PROVEN_SINGLE_IMPLEMENTATION_REPAIR')

def validate_admission(admission,binding_sha,readiness_sha,review_sha):
    if (admission.get('status')!='ACCEPTED_WITHIN_USER_SCOPE'
        or admission.get('binding_sha256')!=binding_sha
        or admission.get('readiness_result_sha256')!=readiness_sha
        or admission.get('independent_review_sha256')!=review_sha):
        raise Stop('NO_SOURCE_BOUND_INDEPENDENT_ADMISSION')
