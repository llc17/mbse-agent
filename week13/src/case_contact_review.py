"""Current, read-only contact review over one manifest-bound execution.

No Store, ledger, process, API or write is opened here. Recorded outcomes remain
historical facts; this overlay is derived again from the selected version files.
"""
from copy import deepcopy
from collections import OrderedDict
import hashlib
import json
from pathlib import Path
import re
from .case_contact_validity import RULE_VERSION, verification_disclosure

ROOT=Path(__file__).resolve().parents[1]
SCHEMA='v12-current-contact-review-2'
IDENTITY_FIELDS=('project_id','version_id','task_id','spec_digest','source_sha256','manifest_sha256')
COMPLETE='COMPLETE_FORWARD_UP20_DOWN20_EXIT_AND_CONTINUE'
RULE='当前只读复核按sampled-full-circle-interior-20261027-v2绑定实际源码、CSV及检查器，不覆盖历史state/outcome/账本。工具执行、route_status路线覆盖、diagnostic_status有限接触诊断和physical_status分别报告。普通轮圆重叠或轮心入统一实体仅为观测；只有轮心在内且到有限踏面/立面/角点最近边界距离d>R（扣除纯浮点epsilon）才报告整圆入实体采样异常，给出d-R、时刻、坐标和来源。异常不抹去路线完成，但不能用于能力证明；未见异常也不等于物理PASS。浮点epsilon不是求解误差阈值。height/姿态只报告，120s为观察区间，非业务时限。tool失败、参数/轨迹/CSV绑定冲突不能报告可信任务成功。旧失败仅在明确属于旧轮心规则且当前完整证据重验有效时允许独立路线复核，recorded_*原记录保留。'
ASYM2_RULE_VERSION='asym2-source-bound-stage-and-validity-20261031-v1'
ASYM2_RULE='当前Asym2只读复核绑定本版本源码、已确认参数、完整CSV与专用有效性/逐级受力检查器。保留原有判据，不套旧均匀楼梯或旧接触检查器。分别报告工具、模型有效性、任务、逐级清阶/受阻、完成时间及实物UNKNOWN；300s是观察区间，未完成不等于缺证据，有效受阻不等于工具失败。旧结果不代替本次运行。'
ASYM2_CHECKER_FILES=('case_asym2_family.py','case_asym2_execution.py','case_contact_review.py','case_scope.py',
    'inherited/model_artifact.py','inherited/artifact_delivery.py','inherited/artifact_history.py',
    'inherited/service_adapter.py','inherited/toolchain_original.py',
    'asym2/physics_check.py','asym2/mixed_check.py','asym2/stage_check.py','asym2/acceptance.json',
    'asym2/reference-source.mo','asym2/reference-variant.mo')
_ASYM2_REVIEW_CACHE=OrderedDict()


def _asym2_checked_execution(spec,tool,base,manifest_sha256):
    """Reuse arithmetic only AFTER fresh complete file/runtime validation.

    This cache is process-local, bounded and never persisted. Any source,
    acceptance rule, spec, manifest or bound file change prevents reuse.
    """
    from .inherited.service_adapter import _verify_manifest
    from .case_asym2_execution import verify_run_evidence,confirmed_expectations
    from .inherited.model_artifact import summarize_execution
    _verify_manifest(base/'artifact-manifest.json',manifest_sha256,base)
    _,_,binding=verify_run_evidence(base/'tool',expected=confirmed_expectations(spec))
    if binding.get('status')!='PASS':raise ValueError('Asym2 current runtime evidence failed: '+str(binding.get('issues',[])))
    fingerprints={name:_sha((ROOT/'src'/name).read_bytes()) for name in ASYM2_CHECKER_FILES}
    key=_value(dict(root=str(base),spec=spec,manifest_sha256=manifest_sha256,checker_source_sha256=fingerprints,rule_version=ASYM2_RULE_VERSION))
    if key in _ASYM2_REVIEW_CACHE:
        _ASYM2_REVIEW_CACHE.move_to_end(key)
        return deepcopy(_ASYM2_REVIEW_CACHE[key]),dict(status='HIT_AFTER_FRESH_FULL_EVIDENCE_CHECK',key=key,checker_source_sha256=fingerprints)
    execution=summarize_execution(spec,tool,base/'tool/model_res.csv')
    _ASYM2_REVIEW_CACHE[key]=deepcopy(execution)
    while len(_ASYM2_REVIEW_CACHE)>3:_ASYM2_REVIEW_CACHE.popitem(last=False)
    return execution,dict(status='MISS_COMPUTED_AFTER_FRESH_FULL_EVIDENCE_CHECK',key=key,checker_source_sha256=fingerprints)


def rule_for_spec(spec):
    from .case_asym2_family import is_asym2_source
    return ASYM2_RULE if is_asym2_source((spec or {}).get('artifact',{}).get('modelica_source','')) else RULE


def _sha(raw):return hashlib.sha256(raw).hexdigest()
def _value(value):return _sha(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf8'))
def _read(path,limit):
    if path.stat().st_size>limit:raise ValueError('contact review evidence exceeds finite byte limit')
    return path.read_bytes()


def mission_summary(assessment):
    keys=('schema','scope','task_status','route_status','diagnostic_status','assessment_evidence_status','overall_summary','validity_status','completion_status','completion_time','completion_row','time_start','time_end','sample_count','descent_orientation','exit_x','rear_edge_at_completion','post_exit_distinct_times','issues','limitations','physical_status','contact_source_domain','body_motion_observations','classification','stage_assessment','stage_report','blocking_stage','progress','source_family','parameter_binding')
    result={k:deepcopy(assessment[k]) for k in keys if k in assessment}
    for key in ('events','terrain_events'):
        if key in assessment:result[key+'_count']=len(assessment[key])
    return result


def _base(snapshot,project_root):
    root=Path(project_root).absolute()
    test_root=ROOT/'results/contact-validity-fix-20261022-01'
    allowed=root.is_relative_to(ROOT/'runs') or root.is_relative_to(test_root) and root.relative_to(test_root).parts and root.relative_to(test_root).parts[0].startswith('integration-')
    if not allowed:raise ValueError('contact review project root is outside current project storage')
    for name in ('project_id','task_id','version_id'):
        if not isinstance(snapshot.get(name),str) or not re.fullmatch(r'[A-Za-z0-9_-]+',snapshot[name]):raise ValueError('invalid bound contact review identity')
    base=root/'projects'/snapshot['project_id']/'versions'/snapshot['version_id']
    for parent in (base,*base.parents):
        if parent.is_symlink() or getattr(parent,'is_junction',lambda:False)():raise ValueError('contact review refuses symbolic links/junctions')
    if base.resolve()!=base:raise ValueError('contact review path escapes bound version')
    return base


def _dual_evidence_pass(outcome):
    if outcome.get('dual_source_contract')!='v11-dual-source-check-1':return True
    modelica=outcome.get('modelica_status')
    modelica_ok=(modelica=='PASS' or isinstance(modelica,dict) and all(modelica.get(k)=='PASS' for k in ('check','compile','simulation')))
    return modelica_ok and outcome.get('sysml_status')=='PASS' and outcome.get('consistency_status')=='PASS'


def _legacy_center_failure_only(snapshot, outcome):
    """Narrow attribution of a historical failed terminal, never a blanket retry."""
    mission=outcome.get('mission_assessment') or {};contact=mission.get('contact_validity') or {}
    route=mission.get('route_assessment') or {};tool=outcome.get('tool_result') or {}
    return (snapshot.get('state')=='failed' and not snapshot.get('error')
            and outcome.get('worker_exit_code')==0
            and outcome.get('tool_status')=='PASS' and outcome.get('chain_status')=='BLOCKED_INVALID_MODEL'
            and outcome.get('task_status')=='UNKNOWN'
            and mission.get('completion_status')=='INVALID_MODEL' and mission.get('validity_status')=='FAIL'
            and route.get('validity_status')=='PASS' and route.get('task_status') in ('PASS','FAIL')
            and contact.get('schema')=='v12-contact-validity-1'
            and contact.get('conclusion')=='HARD_GEOMETRIC_CONTRADICTION'
            and contact.get('counters',{}).get('center_inside_solid_sample_count',0)>0
            and mission.get('terrain_binding',{}).get('status')=='PASS'
            and mission.get('observation_binding',{}).get('status')=='PASS'
            and all(tool.get(k,{}).get('status')=='PASS' for k in ('compile','simulation'))
            and (outcome.get('runtime_parameter_readback') or tool.get('runtime_parameter_readback') or {}).get('status')=='PASS'
            and _dual_evidence_pass(outcome)
            and mission.get('issues')==contact.get('issues'))


def review_snapshot(snapshot,project_root,*,detailed=False):
    """Recheck only canonical paths of this snapshot/project/version."""
    historical={k:snapshot.get(k) for k in ('tool_status','chain_status','task_status','evidence_status','state')}
    result=dict(schema=SCHEMA,rule_version=RULE_VERSION,read_only=True,status='UNKNOWN',validity_status='UNKNOWN',task_status='UNKNOWN',route_status='UNKNOWN',diagnostic_status='UNKNOWN',completion_status='CONTACT_EVIDENCE_UNKNOWN',physical_status='UNKNOWN',historical_recorded_status=historical,historical_explanation_role='HISTORICAL_INTERPRETATION_NOT_CURRENT_REVIEW',precedence_rule=RULE,issues=[],**verification_disclosure())
    if snapshot.get('outcome',{}).get('test_only') is True:
        result.update(status='TEST_ONLY',completion_status='TEST_ONLY',issues=['注入测试数据不能证明真实物理任务。']);return result
    if snapshot.get('record_kind')=='IMPORTED_REFERENCE':
        result.update(status='NOT_RUN',task_status='NOT_RUN',completion_status='NOT_RUN',issues=['导入参考没有本版本独立运行CSV。']);return result
    try:
        base=_base(snapshot,project_root)
        if snapshot.get('evidence_status')!='COMPLETE' or snapshot.get('recorded_conclusions_verified') is not True:raise ValueError('complete final snapshot evidence required')
        spec=snapshot['frozen_spec'];source=spec['artifact']['modelica_source']
        from .case_asym2_family import is_asym2_source
        asym2=is_asym2_source(source)
        if asym2:result.update(rule_version=ASYM2_RULE_VERSION,precedence_rule=ASYM2_RULE,scope='SOURCE_BOUND_ASYM2_STAGE_AND_VALIDITY',verification_scope='SOURCE_BOUND_ASYM2_CONTACT_ENERGY_AND_STAGES',capabilities=dict(wheel_rotation='SOURCE_BOUND_CHECK',slip='SOURCE_BOUND_CHECK',multi_contact_force_closure='SOURCE_BOUND_CHECK'),limitations=['有限简化模型诊断，不构成硬件认证。'])
        if _value(spec)!=snapshot.get('spec_digest') or _sha(source.encode('utf8'))!=snapshot.get('source_sha256'):raise ValueError('contact review frozen spec/source binding drift')
        manifest_path=base/'artifact-manifest.json';manifest_raw=_read(manifest_path,32*1024*1024)
        if _sha(manifest_raw)!=snapshot.get('manifest_sha256'):raise ValueError('contact review manifest digest drift')
        manifest=json.loads(manifest_raw);binding=manifest['binding']
        if manifest.get('schema')!='v11-artifact-manifest-1' or any(binding.get(k)!=snapshot[k] for k in ('project_id','task_id','spec_digest','source_sha256')):raise ValueError('contact review manifest identity differs')
        envelope_outcome=snapshot.get('outcome')
        if not isinstance(envelope_outcome,dict) or {k:v for k,v in envelope_outcome.items() if k not in ('outcome_manifest_path','outcome_manifest_sha256','worker_exit_code','worker_pid')}!=manifest.get('outcome'):
            raise ValueError('contact review snapshot outcome differs from bound manifest')
        paths={'frozen-model.mo':131072,'spec.json':32*1024*1024,'outcome.json':32*1024*1024,'tool/model_res.csv':(512 if asym2 else 64)*1024*1024}
        hashes={}
        for name,limit in paths.items():
            path=base/name
            for parent in (path,*path.parents):
                if parent.is_symlink() or getattr(parent,'is_junction',lambda:False)():raise ValueError('contact review evidence link refused')
            raw=_read(path,limit);entry=manifest['files'][name]
            if entry.get('sha256')!=_sha(raw) or entry.get('bytes')!=len(raw):raise ValueError('contact review manifest file mismatch: '+name)
            hashes[name]=_sha(raw)
            if name=='frozen-model.mo' and raw!=source.encode('utf8'):raise ValueError('contact review actual source differs from snapshot')
            if name=='spec.json' and json.loads(raw)!=spec:raise ValueError('contact review actual spec differs from snapshot')
            if name=='outcome.json' and json.loads(raw)!=manifest.get('outcome'):raise ValueError('contact review actual outcome differs from manifest')
        for name,expected in (('Modelica',base/'frozen-model.mo'),('CSV',base/'tool/model_res.csv')):
            declared=snapshot.get('source_files' if name=='Modelica' else 'data_files',{}).get(name)
            if declared is not None and Path(declared).absolute()!=expected:raise ValueError('contact review declared evidence path differs from bound version')
        from .inherited.model_artifact import summarize_execution
        recorded=manifest['outcome'];tool=recorded.get('tool_result') or {}
        # Reapply the complete existing output/parameter/signal/trajectory checks,
        # rather than using the new geometry rule to bypass another old failure.
        if asym2:
            execution,cache=_asym2_checked_execution(spec,tool,base,snapshot['manifest_sha256'])
            # Binding snapshots must remain identical across MISS/HIT so the
            # ordinary read-only before/after equality guard remains valid.
            result['assessment_binding']={k:cache[k] for k in ('key','checker_source_sha256')}
        else:execution=summarize_execution(spec,tool,base/'tool/model_res.csv')
        assessment=execution.get('mission_assessment') or dict(task_status='UNKNOWN',validity_status='UNKNOWN',issues=execution.get('evidence_issues',[]))
        assessment=dict(assessment,task_status=execution.get('task_status','UNKNOWN'),completion_status=execution.get('completion_status',assessment.get('completion_status','UNKNOWN')))
        if assessment.get('task_status')=='FAIL' and assessment.get('completion_status')==COMPLETE:
            assessment.update(completion_status='TASK_CHECK_FAILED',overall_summary='TASK_CHECK_FAILED')
        legacy=_legacy_center_failure_only(snapshot,snapshot.get('outcome') or recorded)
        state=str(snapshot.get('state','')).lower()
        terminal_ok=state=='succeeded' or legacy
        dual_ok=_dual_evidence_pass(recorded)
        execution_ok=(snapshot.get('tool_status')=='PASS' and execution.get('tool_status')=='PASS'
                      and execution.get('chain_status')=='PASS' and terminal_ok and dual_ok
                      and (snapshot.get('chain_status')=='PASS' or legacy))
        if not execution_ok:
            assessment=dict(assessment,task_status='UNKNOWN',issues=list(assessment.get('issues',[]))+['历史执行或当前完整检查未满足可信任务证据要求；路线观测不覆盖工具、参数、轨迹或其他检查失败。'])
            assessment['completion_status']='TOOL_EXECUTION_INCOMPLETE' if snapshot.get('tool_status')!='PASS' or state in ('cancelled','uncertain') else 'EXECUTION_EVIDENCE_INCOMPLETE'
        contact=assessment.get('contact_validity') or dict(status='UNKNOWN',issues=assessment.get('issues',[]),physical_status='UNKNOWN')
        compact={k:deepcopy(contact[k]) for k in ('schema','rule_version','status','conclusion','physical_status','scope','verification_scope','capabilities','limitations','issues','counters','per_entity','observations') if k in contact}
        compact['hard_contradictions']=deepcopy(contact.get('hard_contradictions',[])[:3])
        compact['full_diagnostic_sha256']=_value(contact)
        # Source/terrain binding is part of the current assessment. A geometry
        # PASS must not mask an UNKNOWN registration or an invalid h binding.
        checker_names=ASYM2_CHECKER_FILES if asym2 else ('case_checks.py','case_contact_validity.py','case_contact_review.py','case_contact_family.py','inherited/model_artifact.py')
        result.update(status=assessment.get('validity_status','UNKNOWN'),validity_status=assessment.get('validity_status','UNKNOWN'),task_status=assessment.get('task_status','UNKNOWN'),route_status=assessment.get('route_status','UNKNOWN'),diagnostic_status=assessment.get('diagnostic_status','UNKNOWN'),overall_summary=assessment.get('overall_summary','EVIDENCE_UNKNOWN') if execution_ok else 'EVIDENCE_UNKNOWN',completion_status=assessment.get('completion_status','CONTACT_EVIDENCE_UNKNOWN'),contact_validity=compact,route_assessment=mission_summary(assessment.get('route_assessment') or {}),current_assessment=mission_summary(assessment),execution_checks=dict(tool_status=execution.get('tool_status'),chain_status=execution.get('chain_status'),signal_check_status=execution.get('signal_check_status'),trajectory_assessment=execution.get('trajectory_assessment'),runtime_parameter_readback=execution.get('runtime_parameter_readback')),execution_evidence_status='PASS' if execution_ok else 'UNKNOWN',legacy_center_failure_reclassified=bool(legacy and execution_ok),current_chain_status=execution.get('chain_status') if execution_ok else snapshot.get('chain_status','BLOCKED_EVIDENCE'),issues=assessment.get('issues',[]),binding=dict(project_id=snapshot['project_id'],task_id=snapshot['task_id'],version_id=snapshot['version_id'],spec_digest=snapshot['spec_digest'],source_sha256=snapshot['source_sha256'],manifest_sha256=snapshot['manifest_sha256'],csv_sha256=hashes['tool/model_res.csv'],rule_version=ASYM2_RULE_VERSION if asym2 else RULE_VERSION,canonical_version_root=str(base),checker_source_sha256={name:_sha((ROOT/'src'/name).read_bytes()) for name in checker_names}))
        if detailed:result['detailed_assessment']=assessment
    except (OSError,ValueError,KeyError,TypeError,AttributeError,OverflowError) as exc:
        result['issues']=[str(exc)]
    return result


def with_review(snapshot,project_root):
    result=dict(snapshot)
    result['current_contact_review']=review_snapshot(snapshot,project_root)
    return result


def current_summary(snapshot,*,include_review=True):
    """One fail-closed projection of recorded data and a version-bound review.

    This function never opens files. Callers obtain a review with ``with_review``
    only for their current/selected version. An untouched history row therefore
    says NOT_REVIEWED, rather than inventing a new failure for that old run.
    """
    result={k:deepcopy(snapshot.get(k)) for k in (*IDENTITY_FIELDS,'record_kind','state','evidence_status','tool_status','chain_status','task_status')}
    for key in ('state','tool_status','chain_status','task_status'):
        result['recorded_'+key]=snapshot.get('recorded_'+key,snapshot.get(key))
    if snapshot.get('record_kind')=='ARTIFACT_EXECUTION_INDEX':
        # An index has no current family/physics review. Do not attach the old
        # contact rule or its capability disclosure to an unreviewed Asym2 row.
        result.update(verification_status='NOT_RECHECKED',recorded_conclusions_verified=False,
            current_review_state='NOT_REVIEWED',tool_status='UNKNOWN',chain_status='NOT_REVIEWED',
            task_status='UNKNOWN',route_status='UNKNOWN',diagnostic_status='UNKNOWN',
            overall_summary='RECORDED_RESULT_NOT_RECHECKED',validity_status='UNKNOWN',
            completion_status='NOT_REVIEWED',physical_status='UNKNOWN')
        if include_review:result['current_contact_review']=dict(status='NOT_REVIEWED',
            review_state='NOT_REVIEWED',verification_status='NOT_RECHECKED',task_status='UNKNOWN',
            issues=['轻量目录仅列保存记录，本次未重核原始证据。'])
        return result
    raw=snapshot.get('current_contact_review');review=deepcopy(raw) if isinstance(raw,dict) else None
    test_only=snapshot.get('test_only') is True or snapshot.get('outcome',{}).get('test_only') is True
    review_state='NOT_REVIEWED' if review is None else 'EVIDENCE_INSUFFICIENT'
    issues=[]
    if test_only:
        review_state='TEST_ONLY';review=dict(schema=SCHEMA,status='TEST_ONLY',validity_status='UNKNOWN',task_status='UNKNOWN',completion_status='TEST_ONLY',issues=['注入测试数据不能证明真实物理任务。'])
        result['test_only']=True
    elif snapshot.get('record_kind')=='IMPORTED_REFERENCE':
        review_state='NOT_RUN';review=dict(schema=SCHEMA,status='NOT_RUN',validity_status='UNKNOWN',task_status='NOT_RUN',completion_status='NOT_RUN',issues=['导入参考没有本版本独立运行CSV。'])
    elif review is not None and review.get('status')=='UNKNOWN' and not review.get('binding') and review.get('review_state') in ('NOT_REVIEWED','BINDING_CONFLICT','EVIDENCE_INSUFFICIENT'):
        # Re-projecting an already fail-closed summary never upgrades it and
        # keeps its original missing-file/conflict explanation stable.
        review_state=review['review_state']
    elif review is not None and (review.get('schema')!=SCHEMA or review.get('rule_version') not in (RULE_VERSION,ASYM2_RULE_VERSION)):
        review_state='NOT_REVIEWED';review=None
    elif review is not None:
        identity=review.get('binding') or {}
        if review.get('schema')!=SCHEMA:issues.append('当前复核schema不受支持。')
        if identity.get('rule_version')!=review.get('rule_version'):issues.append('当前复核规则版本绑定不足。')
        for key in ('project_id','version_id','task_id'):
            if not isinstance(snapshot.get(key),str) or not snapshot[key]:issues.append('执行封套缺少身份：'+key)
        # Every accepted review names a project, task, version, source, spec,
        # manifest and CSV. Compare all identities available on the envelope.
        for key in (*IDENTITY_FIELDS,'csv_sha256'):
            value=identity.get(key)
            if not isinstance(value,str) or not value:issues.append('当前复核缺少绑定：'+key)
            elif key.endswith(('_digest','_sha256')) and not re.fullmatch(r'[0-9a-f]{64}',value):issues.append('当前复核摘要非法：'+key)
            if snapshot.get(key) is not None and value is not None and snapshot.get(key)!=value:issues.append('当前复核绑定冲突：'+key)
        if not issues:
            review_state='CURRENT_BOUND_REVIEW'
            if review.get('status') not in ('PASS','FAIL') or review.get('validity_status')!=review.get('status'):
                issues.append('当前复核有效性状态不足或冲突。')
            else:
                task=review.get('task_status','UNKNOWN');completion=review.get('completion_status','UNKNOWN')
                assessment=review.get('current_assessment') or {}
                if any(assessment.get(k) is not None and assessment[k]!=review.get(k) for k in ('validity_status','task_status','completion_status')):issues.append('当前复核与当前检查摘要冲突。')
                if task=='PASS' and completion!=COMPLETE or task!='PASS' and completion==COMPLETE:issues.append('当前任务状态与完成状态冲突。')
                if task=='PASS' and review['status']=='FAIL':issues.append('严重接触异常不能归为整体任务PASS。')
                if task=='PASS' and str(result.get('state','')).lower()=='failed' and not (review.get('legacy_center_failure_reclassified') is True and review.get('execution_evidence_status')=='PASS' and result['recorded_chain_status']=='BLOCKED_INVALID_MODEL'):
                    issues.append('当前任务通过与执行失败终态冲突。')
                if task=='PASS' and review.get('execution_evidence_status')!='PASS':issues.append('当前任务缺少完整执行检查证据。')
                if result.get('tool_status')!='PASS' or str(result.get('state','')).lower() in ('cancelled','uncertain','queued','running','cancel_requested'):
                    review.update(task_status='UNKNOWN',completion_status='TOOL_EXECUTION_INCOMPLETE')
        if issues:review_state='BINDING_CONFLICT' if any('绑定冲突' in issue for issue in issues) else 'EVIDENCE_INSUFFICIENT'
    if review is None or issues:
        prior_issues=list(review.get('issues') or []) if review else []
        if raw is not None and raw.get('review_state')=='NOT_REVIEWED' and not raw.get('binding'):review_state='NOT_REVIEWED'
        review=dict(schema=SCHEMA,rule_version=RULE_VERSION,status='UNKNOWN',validity_status='UNKNOWN',task_status='UNKNOWN',completion_status='NOT_REVIEWED' if review_state=='NOT_REVIEWED' else 'CONTACT_EVIDENCE_UNKNOWN',issues=list(dict.fromkeys(prior_issues+issues)) or ['本历史版本尚未按当前规则/源码/CSV绑定复核；原记录单列保留。'])
    review.update(physical_status='UNKNOWN',review_state=review_state,precedence_rule=ASYM2_RULE if review.get('rule_version')==ASYM2_RULE_VERSION else RULE)
    if review.get('rule_version')!=ASYM2_RULE_VERSION:review.update(verification_disclosure())
    result.update(current_contact_review=review,current_review_state=review_state,task_status=review.get('task_status','UNKNOWN'),route_status=review.get('route_status','UNKNOWN'),diagnostic_status=review.get('diagnostic_status',review.get('validity_status','UNKNOWN')),overall_summary=review.get('overall_summary','EVIDENCE_UNKNOWN'),validity_status=review.get('validity_status','UNKNOWN'),completion_status=review.get('completion_status','UNKNOWN'),physical_status='UNKNOWN')
    if review_state=='CURRENT_BOUND_REVIEW' and review.get('execution_evidence_status')=='PASS':result['chain_status']=review.get('current_chain_status',result.get('chain_status'))
    if review_state=='NOT_REVIEWED':result['chain_status']='NOT_REVIEWED'
    elif review['status']=='UNKNOWN' and result.get('tool_status')=='PASS':result['chain_status']='BLOCKED_CONTACT_EVIDENCE'
    if not include_review:result.pop('current_contact_review',None)
    return result


def task_conclusion(summary,*,binding_conflict=False):
    """Program-owned task wording, shared by results and read-only answers."""
    if summary.get('test_only') or summary.get('current_review_state')=='TEST_ONLY':return '仅测试数据（TEST_ONLY），不能认定真实任务完成。',False
    if binding_conflict or summary.get('current_review_state')=='BINDING_CONFLICT':return '结论未确定：结构化执行证据存在冲突，不能认定任务完成。',False
    state=str(summary.get('state','')).upper()
    if state in ('QUEUED','RUNNING','CANCEL_REQUESTED'):return '验证进行中，任务是否完成尚未确定。',False
    if state in ('FAILED','CANCELLED','UNCERTAIN') and summary.get('recorded_task_status')=='PASS' and summary.get('recorded_tool_status')=='PASS' and summary.get('recorded_chain_status')=='PASS':return '结论未确定：结构化执行证据存在冲突，不能认定任务完成。',False
    if state in ('CANCELLED','UNCERTAIN'):return '验证已取消或结果未确定，不能认定任务完成。',False
    tool=summary.get('tool_status','UNKNOWN');chain=summary.get('chain_status','UNKNOWN')
    if tool not in ('PASS','UNKNOWN','NOT_RUN','NOT_VERIFIED',None):return '验证受阻，任务是否完成尚未确定。',False
    if summary.get('current_review_state')=='NOT_REVIEWED':return '结论未确定：本历史版本尚未复核（NOT_REVIEWED），历史任务记录不代表当前结论。',False
    review=summary.get('current_contact_review') or {}
    if any('冲突' in x for x in review.get('issues',[])):return '结论未确定：结构化执行证据存在冲突，不能认定任务完成。',False
    if summary.get('current_review_state')!='CURRENT_BOUND_REVIEW' or summary.get('validity_status') not in ('PASS','FAIL'):return '结论未确定：当前源码与CSV绑定的复核证据不足，任务UNKNOWN。',False
    if tool!='PASS' or chain!='PASS':return '验证受阻，任务是否完成尚未确定。',False
    if summary.get('validity_status')=='FAIL':
        if summary.get('route_status')=='PASS':return '轨迹已完成，但模型接触异常，任务结论UNKNOWN。',False
        return '模型接触异常；路线完成情况另列，任务结论UNKNOWN。',False
    if summary.get('task_status')=='FAIL':
        return ('观察区间内任务未完成（简化模型仿真）。' if summary.get('completion_status')=='OBSERVATION_INTERVAL_INCOMPLETE' else '任务未完成（独立任务检查未通过；简化模型仿真）。'),False
    if summary.get('task_status')=='PASS' and summary.get('completion_status')==COMPLETE:return '任务成功（当前简化模型内）。',True
    return '结论未确定：现有证据不足以判定任务是否完成。',False
