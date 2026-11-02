"""Deterministic checks for a finite set of observed product-text conflicts.

This is not a natural-language verifier. It neither writes a product plan nor
changes a checker verdict. Raw provider replies remain untouched.
"""
from copy import deepcopy
import hashlib
import json
import re


def _digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf8')).hexdigest()


def _plain(text):
    return re.sub(r'[`*]','',text)


_ACTION=re.compile(r'修改|改动|调整|调到|修复|替换|改写|重写|新增|增加|引入|延长|增大|减小|提高|降低|校正|改变|改为|改成|移动|移到|偏移|change|replace|modify|repair|extend',re.I)
_NEGATIVE=re.compile(r'(?:(?:不(?!仅|只)|未|无须|无需|禁止|不得|不能|没有|避免)[^，,。；;]{0,16}|\b(?:not|never|without|no)\s*(?:to\s*)?)$',re.I)
_HISTORY=re.compile(r'历史|此前|曾经|原先|基线已|基准已|已经修复|已完成的|historical|previously',re.I)
_FROZEN_COMMON=r'FtC|Fn_sum|FnC|切向(?:力|分配|预算)|接触(?:方程|模型|参数|刚度|阻尼|几何|数值|力)|控制(?:律|器|参数)?|轮径|半径|轴位|轴距|几何|重心|惯量|I_pitch|n_steps|len_flat|台阶数|级数|平台(?:长度)?|观察(?:区间|时间|设置)?|StopTime|Tolerance|容差|任务(?:条件|路线|定义)?'


def _plan_action_target(clause,action,target,local_start,kind):
    """Bind a small action/object grammar, not arbitrary nearby risk words.

    Questions and possible future needs do not commit an edit. A subsequent
    current-action marker starts a new commitment even in an unknowns field.
    Structured parameters and actual source differences have separate gates.
    """
    before=clause[local_start:action.start()]
    questions=list(re.finditer(r'是否|能否|会否|可否|需不需要|要不要|有无必要|\bwhether\b',before,re.I))
    commitment=r'本次(?:将|拟|计划|决定|要)?|此次(?:将|拟|计划|决定|要)?|将|拟|计划|决定|this request|will\s+(?:now\s+)?'
    if questions and not re.search(commitment,before[questions[-1].end():],re.I):return None
    conditionals=list(re.finditer(r'如果|假如|假设|若(?:是)?|\bif\b',before,re.I))
    if conditionals and not re.search(commitment,before[conditionals[-1].end():],re.I):return None
    if re.search(r'可能|也许|或许|尚待确定|尚不清楚|未知|\bmight\b|\bmay\b',before,re.I) and not re.search(commitment,before,re.I):return None
    # A second event, question, result or inherited object ends the relation
    # between an action and an object. This prevents "mass increases, whether
    # inertia/task changes is unknown" from being read as an inertia/task edit.
    barrier=re.compile(r'是否|能否|会否|可否|之后|以后|后|与|和|及|影响|导致|未知|不变|不改|冻结|继承|保持|'+_ACTION.pattern,re.I)
    point_addition=kind=='center_of_mass_point_load' and action.group() in ('新增','增加')
    for found in target.finditer(clause,local_start):
        if found.group().strip()=='m' and re.search(r'[0-9.]\s*$',clause[:found.start()]):continue
        if found.end()<=action.start():
            bridge=clause[found.end():action.start()]
            if len(bridge)>18 or barrier.search(bridge):continue
            # In "at the original CoM, add mass", CoM is a prepositional
            # location, not the object of add. Keep scanning: a later inertia
            # object or a separate move/modify action must still be checked.
            if point_addition and found.group()=='重心':
                location=clause[local_start:found.start()]
                if re.search(r'(?:在|于)(?:原(?:参考)?|参考)?\s*$',location):continue
                # Chinese also permits a bare location topic: "original CoM,
                # add ideal point mass". Require an actual mass/load object.
                if re.search(r'原(?:参考)?\s*$',location) and re.fullmatch(r'(?:位置)?(?:处)?\s*',bridge) and re.match(r'(?:理想)?(?:点)?质量|载荷|\d+(?:\.\d+)?\s*(?:kg|千克)',clause[action.end():],re.I):continue
            # A named physical effect can be the subject of a risk statement:
            # "CoM displacement and inertia variation are not modeled".
            # Keep explicit "this request will move ..." commitments active.
            if not re.search(commitment,before,re.I) and re.match(r'与|和|及|对|所|会|可能|风险|尚|未|后|的(?:风险|影响|结果|物理)',clause[action.end():]):continue
        elif found.start()>=action.end():
            bridge=clause[action.end():found.start()]
            if len(bridge)>28 or barrier.search(bridge):continue
            if point_addition and found.group()=='重心' and re.search(r'(?:在|于|以|按|作为)(?:原(?:参考)?|参考)?\s*$',bridge):continue
            if re.match(r'[^，,]{0,18}(?:不变|不改|冻结|继承|保持)',clause[found.end():]):continue
        else:continue
        return found
    return None


def plan_semantic_validation(packet,baseline):
    """Diagnose lexical action claims; structured/source gates are authoritative.

    Only supported ordinary mass/stair plans are covered. Historical/negative
    statements are distinguished where possible. A lexical hit is a visible
    warning, never proof that the confirmed parameters or source were changed.
    """
    scope=packet.get('scope') or {};kind=scope.get('change_kind')
    source=baseline.get('artifact',{}).get('modelica_source','')
    result=dict(schema='v12-known-plan-semantic-check-1',status='NOT_APPLICABLE',full_semantic_validation=False,
        coverage='KNOWN_CURRENT_FROZEN_ACTION_CLAIMS_ONLY',issues=[],scope=deepcopy(scope),baseline_source_sha256=hashlib.sha256(source.encode('utf8')).hexdigest(),
        baseline_facts=dict(ftc_positive_denominator_guard_present=bool(re.search(r'max\s*\(\s*Fn_sum\s*\[\s*i\s*\]\s*,\s*1(?:\.0)?e-9\s*\)',source,re.I))))
    if packet.get('intent')!='CHANGE' or packet.get('support')!='SUPPORTED' or kind not in ('stair_dimensions','center_of_mass_point_load'):return result
    targets=_FROZEN_COMMON
    from .case_asym2_family import is_asym2_source
    asym2=is_asym2_source(source)
    if asym2:targets+=r'|h19|h20|d19|d20|wheel_J|尾阶|末两级|nEndSteps|fixedTopTread'
    if asym2 and kind=='center_of_mass_point_load':targets+=r'|T_main_max|T_nose_max|扭矩|stepH|stepD|台阶(?:高|深)|主体(?:高|深)'
    if kind=='stair_dimensions':targets+=r'|车辆(?:参数|质量|结构)|(?<![A-Za-z0-9_.])m\s*(?==|由|从|改|调)|T_main_max|T_nose_max|扭矩'
    # The original-CoM ideal point load and inherited inertia are allowed mass
    # assumptions, not geometry edits. These words require an actual action.
    target=re.compile(targets,re.I)
    design=packet.get('design') or {}
    fields=[('design.summary',design.get('summary','')),('rationale',packet.get('rationale',''))]
    fields += [('design.assumptions['+str(i)+']',x.get('text','') if isinstance(x,dict) else '') for i,x in enumerate(design.get('assumptions',[]))]
    for name in ('defaults','unknowns'):
        fields += [(name+'['+str(i)+']',x) for i,x in enumerate(packet.get(name,[]))]
    for path,text in fields:
        for clause in re.split(r'[，,。；;\n]',_plain(text)):
            for action in _ACTION.finditer(clause):
                # A topic before a colon, or a separate contrast, cannot make
                # a later inherited fact into a current edit (or vice versa).
                boundaries=list(re.finditer(r'但是|但|然而|不过|而是|却|因此|所以|随后|然后|[：:]',clause[:action.start()]))
                local_start=boundaries[-1].end() if boundaries else 0
                before=clause[local_start:action.start()]
                prefix=before[-22:]
                # An earlier negative condition cannot negate a fresh explicit
                # "this time will ..." commitment in the same short clause.
                fresh=list(re.finditer(r'(?:本次|此次)(?:将|拟|计划|决定|要)',before))
                if fresh:prefix=before[fresh[-1].end():][-22:]
                if _NEGATIVE.search(prefix) and not re.search(r'不能不|不得不|并非不|不是不',prefix):continue
                # Past fixes may be described accurately. A current marker
                # prevents the inherited "本次唯一改动" bug being excused.
                current=bool(re.search(r'本次|此次|当前(?:修改|修复|变更)|将|拟|计划|本工况|this request',before,re.I))
                current_edit=bool(re.search(r'(?:本次|此次|当前)(?:唯一)?(?:修改|修复|改动|变更)|再次|重新|将|拟|计划',before,re.I))
                inherited=bool(re.search(r'继承|沿用|保持|保留',before))
                known_past=bool(re.search(r'(?:基线|基准)(?:已含|已包含|已有)|此前已审',before))
                if _HISTORY.search(clause[local_start:action.end()]) and not current_edit and (not current or inherited or known_past):continue
                if inherited and not current_edit and re.search(r'已(?:经)?$',prefix):continue
                found=_plan_action_target(clause,action,target,local_start,kind)
                if not found:continue
                result['issues'].append(dict(code='CURRENT_ACTION_CHANGES_FROZEN_SCOPE',field=path,target=found.group(),claim=clause.strip()[:420],allowed_direct_changes=['stepH','stepD'] if kind=='stair_dimensions' else ['m','T_main_max','T_nose_max']))
                break
    result['status']='KNOWN_FACT_CONFLICT' if result['issues'] else 'NO_KNOWN_FACT_CONFLICT'
    return result


def require_plan_semantics(packet,baseline):
    report=plan_semantic_validation(packet,baseline)
    report['warnings']=[deepcopy(item) for item in report['issues'] if item['code']=='CURRENT_ACTION_CHANGES_FROZEN_SCOPE']
    report['blocking_issues']=[deepcopy(item) for item in report['issues'] if item['code']!='CURRENT_ACTION_CHANGES_FROZEN_SCOPE']
    report['decision']='BLOCK' if report['blocking_issues'] else 'ALLOW_WITH_DIAGNOSTIC_WARNINGS' if report['warnings'] else 'ALLOW'
    report['lexical_diagnostics_authoritative']=False
    report['lexical_classification']=report['status']
    if report['warnings'] and not report['blocking_issues']:report['status']='DIAGNOSTIC_WARNINGS'
    if report['blocking_issues']:
        raise ValueError('known plan semantic conflict; keep structured parameter commitments and independently rewrite current action claims: '+json.dumps(report,ensure_ascii=False,separators=(',',':')))
    return report


def plan_diagnostic_warnings(report):
    return ['方案文字诊断告警（不授予改模权限）：'+item['field']+'；目标 '+item['target']+'；原文「'+item['claim']+'」。词法提示可能误报；仅以已确认结构化参数/范围及实际双源码检查决定允许差异。'
        for item in report.get('warnings',[])]


def baseline_source_references(ctx):
    """Project a verified request baseline into source identity, never results.

    load_request already verifies the immutable original and snapshot. Recheck
    their content here so callers cannot turn a foreign/stale v0 into a source.
    Incomplete legacy/test contexts grant no source-reference privilege.
    """
    ref=ctx.get('baseline_ref') or {};fields={'project_id','task_id','version_id','spec_digest','source_sha256','manifest_sha256'}
    if not fields<=set(ref):return []
    snapshot=ctx.get('baseline_snapshot') or {}
    if set(ref)!=fields or ref['project_id']!=ctx.get('project_id') or ref['version_id']!=ctx.get('baseline_version_id') or any(snapshot.get(k)!=v for k,v in ref.items()):
        raise ValueError('baseline source reference project/version/content binding differs')
    if _digest(snapshot)!=ctx.get('baseline_snapshot_sha256'):
        raise ValueError('baseline source snapshot digest differs')
    spec=snapshot.get('frozen_spec') or {};source=(spec.get('artifact') or {}).get('modelica_source')
    if not isinstance(source,str) or _digest(spec)!=ref['spec_digest'] or hashlib.sha256(source.encode('utf8')).hexdigest()!=ref['source_sha256']:
        raise ValueError('baseline source spec/source digest differs')
    return [dict(kind='BASELINE_SOURCE',project_id=ref['project_id'],version_id=ref['version_id'],record_kind=snapshot.get('record_kind'),
        baseline_ref=deepcopy(ref),baseline_snapshot_sha256=ctx['baseline_snapshot_sha256'],spec_digest=ref['spec_digest'],
        source_sha256=ref['source_sha256'],manifest_sha256=ref['manifest_sha256'],execution_evidence_present=False)]


def explanation_facts(binding,extra_executions=()):
    """Project only structured current facts, keeping their evidence identity."""
    from .case_contact_validity import verification_disclosure
    from .case_contact_review import current_summary
    rows=[]
    for record in (binding.get('latest_request_execution'),binding.get('reviewed_execution'),*(binding.get('explicitly_reviewed_executions') or [])[:8],*extra_executions):
        if not isinstance(record,dict) or not record.get('version_id'):continue
        if record.get('record_kind')=='IMPORTED_REFERENCE':continue
        original=record
        record=current_summary(record)
        if binding.get('project_id') is not None and original.get('project_id') is not None and original['project_id']!=binding['project_id']:
            # Preserve both identities as evidence of a conflict; never repair
            # a foreign record by overwriting its project to match the caller.
            from .case_contact_review import SCHEMA,RULE_VERSION
            record=current_summary(dict(record,current_contact_review=dict(schema=SCHEMA,rule_version=RULE_VERSION,status='UNKNOWN',validity_status='UNKNOWN',task_status='UNKNOWN',completion_status='CONTACT_EVIDENCE_UNKNOWN',review_state='BINDING_CONFLICT',issues=['外层请求项目与执行项目绑定冲突。'])))
        review=record.get('current_contact_review') or {};assessment=review.get('current_assessment') or {}
        contact=review.get('contact_validity') or {}
        identity=review.get('binding') or {}
        hard=contact.get('hard_contradictions',review.get('hard_contradictions'))
        row=dict(version_id=record['version_id'],task_id=record.get('task_id'),project_id=record.get('project_id',binding.get('project_id')),
            tool_status=record.get('tool_status','UNKNOWN'),chain_status=record.get('chain_status','UNKNOWN'),current_review_state=record['current_review_state'],
            validity_status=review.get('validity_status',record.get('validity_status','UNKNOWN')),
            task_status=review.get('task_status',record.get('task_status','UNKNOWN')),
            route_status=record.get('route_status','UNKNOWN'),diagnostic_status=record.get('diagnostic_status','UNKNOWN'),
            overall_summary=record.get('overall_summary','EVIDENCE_UNKNOWN'),
            completion_status=review.get('completion_status',assessment.get('completion_status','UNKNOWN')),
            physical_status=review.get('physical_status',assessment.get('physical_status','UNKNOWN')),
            hard_contradictions=deepcopy(hard),hard_contradictions_explicit=isinstance(hard,list),
            source_binding={key:identity[key] for key in ('spec_digest','source_sha256','manifest_sha256','csv_sha256') if key in identity},
            state=record.get('state'),recorded_state=record.get('recorded_state'),
            current_contact_review={k:deepcopy(review[k]) for k in ('rule_version','legacy_center_failure_reclassified','execution_evidence_status','issues') if k in review},
            recorded_tool_status=record.get('recorded_tool_status'),recorded_chain_status=record.get('recorded_chain_status'),recorded_task_status=record.get('recorded_task_status'),
            test_only=record.get('test_only',False),**verification_disclosure())
        from .case_contact_review import ASYM2_RULE_VERSION
        if review.get('rule_version')==ASYM2_RULE_VERSION:
            row.update(verification_scope='SOURCE_BOUND_ASYM2_STAGE_AND_VALIDITY',classification=assessment.get('classification'),stage_report=deepcopy(assessment.get('stage_report')),capabilities=deepcopy(review.get('capabilities',{})),limitations=['有限简化模型、原判据检查，不构成硬件认证。'])
        parameters=original.get('parameters') or {}
        if 'stepH' in parameters and 'stepD' in parameters:row['stair_dimensions']={k:parameters[k] for k in ('stepH','stepD')}
        if not any(all(x.get(k)==row.get(k) for k in ('project_id','version_id','task_id')) for x in rows):rows.append(row)
    return rows


def _aliases(row):
    aliases=[row['version_id']]
    if re.fullmatch(r'v\d+',row['version_id']):aliases.append('v'+str(int(row['version_id'][1:])))
    dimensions=row.get('stair_dimensions')
    if dimensions:
        h,d=dimensions['stepH'],dimensions['stepD']
        aliases += [f'{h:g}×{d:g}',f'{h:.2f}×{d:.2f}',f'{h*1000:g}×{d*1000:g}']
    return aliases


def _equivalence_negation(sentence,match,claim_group):
    """Locate only the target of a finite negative equivalence construction.

    Return (negated target, fresh local prefix start). Completing one target
    ends its negation; later assertions must be checked using their own prefix.
    """
    start=match.start(claim_group)
    completed_target=r'任务(?:已经|已)?(?:完成|成功|通过)|已完成\s*20\s*级|成功(?:爬|通过|完成)|(?:task_status|validity_status|physical_status|completion_status)\s*[:=]\s*[A-Z_]+'
    markers=list(re.finditer(r'(不能|不得|不应)\s*(不\s*)?(?:把|将)',sentence[:start]))
    for marker in reversed(markers):
        relation=re.search(r'当作|视为|等同于',sentence[marker.end():start])
        if not relation:continue
        relation_start=marker.end()+relation.start();target_start=marker.end()+relation.end()
        if re.search(r'但是|但|然而|不过|而是|却|[，,。；;\n]',sentence[marker.end():relation_start]):continue
        target_prefix=sentence[target_start:start]
        # These modifiers belong to the equivalence target. A second subject,
        # verb or claim is not part of this small grammar.
        target_modifier=r'[\s“”"\'（(]*(?:(?:本次|此次|本请求|当前)(?:的)?\s*)?(?:(?:车辆|小车|这台车|本车)(?:的)?\s*)?(?:\d+(?:\.\d+)?\s*(?:mm|毫米|[×x]\s*\d+(?:\.\d+)?)\s*)?'
        if re.fullmatch(target_modifier,target_prefix):
            # The existing vehicle-success pattern can begin at the first
            # target and greedily finish in a later assertion. Such a match
            # crosses the target's end and must never inherit its negation.
            claim=match.group(claim_group)
            first_target=re.search(completed_target,claim,re.I)
            if re.search(r'但是|但|然而|不过|而是|却',claim) or (first_target and first_target.end()<len(claim.rstrip())):
                return False,target_start
            doubled=bool(marker.group(2) or re.search(r'(?:不是|并非|并不是)\s*$',sentence[:marker.start()]))
            return (False,target_start) if doubled else (True,None)
        # Match earlier completed targets even when the current checker uses a
        # different claim pattern (e.g. task_status=PASS after 任务成功).
        completed=list(re.finditer(completed_target,target_prefix,re.I))
        ends=[target_start+x.end() for x in completed]
        ends += [x.end() for x in match.re.finditer(sentence,target_start,start)]
        if ends:return False,max(ends)
    return False,None


def _affirmative(sentence,match,claim_group=0):
    """Negation/history applies near the claim, never to an entire answer."""
    start=match.start(claim_group);claim=match.group(claim_group)
    prefix=sentence[max(0,start-30):start]
    # A dotted object path can exceed the general short prose-negation window.
    # Recognize only explicit structured-state targets, and finish that scope
    # at the first completed state so a later contradictory claim stays visible.
    structured_start=None
    if {'label','value'}<=set(match.re.groupindex):
        markers=list(re.finditer(r'不代表|并非|不是',sentence[:start]))
        for marker in reversed(markers):
            target_prefix=sentence[marker.end():start]
            if re.fullmatch(r'\s*(?:[A-Za-z_]\w*\s*\.\s*)*',target_prefix):
                leading=re.search(r'(?:(?:不代表|并非|不是)\s*)+$',sentence[:marker.start()])
                count=1+len(re.findall(r'不代表|并非|不是',leading[0])) if leading else 1
                if count%2:return False
                structured_start=marker.end();break
            completed=list(_STATUS_CLAIM.finditer(sentence,marker.end(),start))
            if completed:structured_start=completed[-1].end();break
    negated,local_start=_equivalence_negation(sentence,match,claim_group)
    if negated:return False
    if structured_start is not None:local_start=max(local_start or 0,structured_start)
    if local_start is not None:prefix=sentence[max(local_start,start-30):start]
    prefix='' if claim.startswith('而是') else re.split(r'但是|但|然而|不过|而是|却',prefix)[-1]
    negative_prefix=prefix
    if re.search(r'(?:不能|不得|不应)\s*不(?:承认|认为|声称|认定)(?:\s|本次|此次|本请求|当前|车辆|小车|这台车|本车|的)*$',negative_prefix):negative_prefix=''
    if _NEGATIVE.search(negative_prefix):return False
    if re.search(r'历史|此前|旧版本|曾经|如果|假设|原(?:错误)?回复|上文错误|错误(?:地)?(?:声称|解释|认为)|曾错误',prefix):return False
    return True


_STATUS_LABELS={
    'tool_status':'tool_status','工具状态':'tool_status','工具':'tool_status',
    'chain_status':'chain_status','链路状态':'chain_status','链路':'chain_status',
    'task_status':'task_status','任务状态':'task_status','任务结果':'task_status','任务完成':'task_status',
    '任务完成性':'task_status','任务完成状态':'task_status','任务完成情况':'task_status','完成性':'task_status',
    'validity_status':'validity_status','模型有效性':'validity_status',
    'route_status':'route_status','路线状态':'route_status',
    'diagnostic_status':'diagnostic_status','接触诊断':'diagnostic_status',
    'physical_status':'physical_status','物理状态':'physical_status',
    'completion_status':'completion_status',
}
_STATUS_VALUES={
    '未知':'UNKNOWN','不能判定':'UNKNOWN','无法判定':'UNKNOWN','无法判断':'UNKNOWN','不能确定':'UNKNOWN',
    '无法确定':'UNKNOWN','尚不确定':'UNKNOWN','不可判定':'UNKNOWN','不能确认':'UNKNOWN','无法确认':'UNKNOWN',
    '未运行':'NOT_RUN','尚未运行':'NOT_RUN','未完成':'FAIL','失败':'FAIL','成功':'PASS','已完成':'PASS','完成':'PASS','通过':'PASS',
}
_STATUS_VALUE=r'(?:[A-Z][A-Z_]*|'+ '|'.join(sorted(_STATUS_VALUES,key=len,reverse=True))+r')'
_STATUS_CLAIM=re.compile(r'(?<![A-Za-z0-9_])(?P<label>'+ '|'.join(sorted(_STATUS_LABELS,key=len,reverse=True))+r')'
    r'(?:(?:\s*[:：=]\s*(?:当前\s*)?(?:为|是)?\s*)|(?:\s*(?:当前\s*)?(?:为|是)\s*)|(?:\s*(?=不能判定|无法判定|无法判断|不能确定|无法确定|尚不确定|不可判定|不能确认|无法确认)))'
    r'(?P<value>'+_STATUS_VALUE+r')(?![A-Za-z0-9_])')
_TASK_SUCCESS=(r'任务(?:已|已经)?(?:完成|成功|通过)|(?:已经|已)完成(?:了)?(?:全部|本次|本)?任务|已完成\s*\d+\s*级|成功(?:爬|通过|完成)|'
    r'(?:已|已经|能够|可以|能)爬\s*\d+(?:\.\d+)?\s*(?:mm|毫米|m|米)|(?:车辆|小车|这台车|本车)[^，,。；\n]{0,12}(?:已|已经)(?:完成|成功|通过)|(?:而是|是)(?:已经|已)?通过')


def _route_task_label(sentence, match):
    """A nested route task label names route_status, never the global verdict.

    Require a directly attached object path or the observed finite Chinese
    qualifier. A later final/current claim does not inherit that qualifier.
    Each matched field still needs its own bound version and route evidence.
    """
    if _STATUS_LABELS[match.group('label')] != 'task_status':return False
    prefix=sentence[:match.start()]
    return bool(re.search(r'(?<![A-Za-z0-9_])route_assessment\s*\.\s*$|'
        r'(?:路线子评估|路线覆盖子评估)(?:内部|里面|里|内|中)?(?:的)?(?:带有|包含|记录的|字段为|结论为)?\s*$',prefix,re.I))


def protect_explanation(raw_answer,binding,*,extra_executions=(),source_references=()):
    """Return safe display text and a bounded, honest validation report."""
    facts=explanation_facts(binding,extra_executions);issues=[];text=_plain(raw_answer)
    def add(code,claim,row=None):
        issue=dict(code=code,claim=claim.strip()[:420])
        if row:issue['version_id']=row['version_id']
        if issue not in issues:issues.append(issue)
    references=[]
    for reference in source_references:
        ref=reference.get('baseline_ref') or {} if isinstance(reference,dict) else {}
        if not isinstance(reference,dict) or reference.get('kind')!='BASELINE_SOURCE' or reference.get('project_id')!=binding.get('project_id') or not ref.get('version_id') or any(reference.get(k)!=ref.get(k) for k in ('project_id','version_id','spec_digest','source_sha256','manifest_sha256')) or reference.get('execution_evidence_present') is not False or not re.fullmatch(r'[0-9a-f]{64}',reference.get('baseline_snapshot_sha256','')):
            add('CURRENT_SOURCE_BINDING_CONFLICT','基线来源引用的项目、版本、摘要或来源类别未绑定。')
            continue
        references.append(deepcopy(reference))
    if binding.get('source_reference_binding_error'):add('CURRENT_SOURCE_BINDING_CONFLICT',binding['source_reference_binding_error'])
    known_execution_versions={row['version_id'] for row in facts}
    known_source_versions={row['version_id'] for row in references}
    source_only=[dict(row,reference_only=True) for row in references if row['version_id'] not in known_execution_versions]
    rows=facts+source_only
    # A shorthand is an alias only of an already bound row. Token boundaries
    # prevent v1 from naming v10 (or an identifier such as original_v1).
    alias_rows={}
    for row in rows:
        for alias in set(_aliases(row)):alias_rows.setdefault(alias.lower(),[]).append(row)
    dimensions=[alias for alias in alias_rows if not re.fullmatch(r'v\d+',alias)]
    mention_pattern=re.compile(r'(?<![A-Za-z0-9_-])v\d+(?![A-Za-z0-9_-])'+('|'+'|'.join(r'(?<![\d.])'+re.escape(alias)+r'(?![\d.])' for alias in sorted(dimensions,key=len,reverse=True)) if dimensions else ''))
    def mentions(fragment):
        return [(match,alias_rows.get(match.group().lower(),[])) for match in mention_pattern.finditer(fragment)]
    def targets_at(sentence,position,inherited):
        named=mentions(sentence);prior=[item for item in named if item[0].end()<=position]
        if not prior:
            if named:return []
            if inherited is False:
                add('AMBIGUOUS_VERSION_SCOPE',sentence)
            return [inherited] if isinstance(inherited,dict) else []
        group=[prior[-1]]
        for item in reversed(prior[:-1]):
            bridge=sentence[item[0].end():group[0][0].start()]
            if not re.fullmatch(r'[\s/、]*(?:(?:与|和|及|以及|and)[\s/、]*)?',bridge,re.I):break
            group.insert(0,item)
        if any(len(candidates)!=1 for _,candidates in group):return []
        return list({(row['version_id'],row.get('task_id')):row for _,candidates in group for row in candidates}.values())
    active=None;paragraph_scope=False;block_scopes=[]
    for row in facts:
        if row.get('current_review_state')=='BINDING_CONFLICT':add('CURRENT_FACT_BINDING_CONFLICT','当前事实的项目/任务/版本或证据绑定冲突。',row)
    for match,candidates in mentions(text):
        if re.fullmatch(r'v\d+',match.group()) and len(candidates)!=1:
            add('UNBOUND_VERSION_REFERENCE' if not candidates else 'AMBIGUOUS_VERSION_REFERENCE','正文版本未唯一绑定当前项目事实或来源：'+match.group())
    valid_task_fail=any(x['validity_status']=='PASS' and x['task_status']=='FAIL' for x in facts)
    for line in text.splitlines():
        line_mentions=mentions(line)
        named=list({(row['version_id'],row.get('task_id')):row for _,candidates in line_mentions for row in candidates}.values())
        unresolved=any(len(candidates)!=1 for _,candidates in line_mentions)
        # A version-labelled parent owns only its Markdown children. Pop a
        # sibling before resolving its scope, so an old version cannot leak
        # into the next list item or section. This is a finite block grammar,
        # not a natural-language inference from incidental version mentions.
        heading=re.match(r'^\s*(#{1,6})\s+',line)
        bullet=re.match(r'^(\s*)(?:[-+*]|\d+[.)])\s+',line)
        if heading:
            level=len(heading.group(1))
            while block_scopes and (block_scopes[-1][0]=='list' or block_scopes[-1][1]>=level):block_scopes.pop()
            inherited=block_scopes[-1][2] if block_scopes else None
            active=named[0] if len(named)==1 and not unresolved else False if line_mentions else inherited
            block_scopes.append(('heading',level,active));paragraph_scope=False
        elif bullet:
            indent=len(bullet.group(1).expandtabs(4))
            while block_scopes and block_scopes[-1][0]=='list' and block_scopes[-1][1]>=indent:block_scopes.pop()
            inherited=block_scopes[-1][2] if block_scopes else None
            active=named[0] if len(named)==1 and not unresolved else False if line_mentions else inherited
            block_scopes.append(('list',indent,active));paragraph_scope=False
        elif not line.strip() and paragraph_scope:
            active=block_scopes[-1][2] if block_scopes else None;paragraph_scope=False
        elif line.strip():
            indent=len(line)-len(line.lstrip())
            while block_scopes and block_scopes[-1][0]=='list' and indent<=block_scopes[-1][1]:block_scopes.pop()
            if not paragraph_scope:active=block_scopes[-1][2] if block_scopes else None
        if not heading and not bullet and (unresolved or len(named)>1 or paragraph_scope and named and isinstance(active,dict) and named[0]['version_id']!=active['version_id']):
            active=False;paragraph_scope=False
        # Observed saved explanations identify the actual execution in one
        # narrative line, followed by status bullets. Bind only that explicit
        # single-version paragraph; incidental mentions never establish scope.
        anchor=re.search(r'(?:本次|本请求)(?:的)?\s*(?:实际)?执行版本\s*(?:为|是|[:：])\s*(v\d+)(?![A-Za-z0-9_])',line)
        if anchor and len(named)==1 and not unresolved and named[0] in alias_rows.get(anchor.group(1).lower(),[]) and _affirmative(line,anchor):
            active=named[0];paragraph_scope=True
        local=named[0] if len(named)==1 and not unresolved else active if not named and not unresolved else None
        if local is None and active is not False and len(facts)==1 and not line_mentions:local=facts[0]
        for sentence in re.split(r'[，,。；;\n]',line):
            if not sentence.strip():continue
            if not facts:
                for match in re.finditer(r'(?:本次|本请求|当前)(?:任务|仿真|运行)[^，,。；\n]{0,8}(?:已|已经)(?:完成|成功|通过)|(?:本次|本请求|当前)已(?:仿真|运行)(?:完成|成功|通过)',sentence):
                    if _affirmative(sentence,match):add('CURRENT_COMPLETION_WITHOUT_BOUND_EXECUTION',sentence)
            # These patterns intentionally target affirmative misclassification,
            # not quoted prior errors or statements saying it cannot be inferred.
            if valid_task_fail and re.search(r'task_status\s*=\s*FAIL|任务\s*FAIL|此\s*FAIL',sentence,re.I):
                for match in re.finditer(r'表示|解释为|意味着|推导|→|=>',sentence):
                    if _affirmative(sentence,match) and re.search(r'硬几何矛盾|INVALID_MODEL|模型无效',sentence[match.end():]) and not re.search(r'错误|纠正',sentence[match.end():]):
                        add('TASK_FAILURE_MISREPRESENTED_AS_INVALID_MODEL',sentence,local)
            # Bind each claim to its preceding explicit version, including a
            # coordinated v0/v1 subject. A later inline version cannot overwrite
            # an earlier claim's subject. Preserve the complete negation span.
            statuses=list(_STATUS_CLAIM.finditer(sentence))
            for match in re.finditer(r'(?:已验证|已证明|证实|确认)[^，,。；\n]{0,12}(?:车辆能力|物理能力|实物能力)|(?:车辆能力|物理能力|实物能力)[^，,。；\n]{0,8}(?:已通过|PASS|已验证|可用)',sentence,re.I):
                if not _affirmative(sentence,match):continue
                for target in targets_at(sentence,match.start(),local):
                    if target.get('diagnostic_status')=='FAIL':add('UNSUPPORTED_PHYSICAL_CAPABILITY',sentence,target)
            for match in statuses:
                field=_STATUS_LABELS[match.group('label')];value=_STATUS_VALUES.get(match.group('value'),match.group('value'))
                if not _affirmative(sentence,match) or re.search(r'历史|旧记录|此前|假设|如果',sentence[:match.start()]):continue
                route_label=_route_task_label(sentence,match)
                if route_label:field='route_status'
                targets=targets_at(sentence,match.start(),local)
                if not targets and value not in ('UNKNOWN','NOT_RUN') and (route_label or not facts):
                    add('ROUTE_STATUS_WITHOUT_BOUND_EXECUTION' if route_label else 'CURRENT_STATUS_WITHOUT_BOUND_EXECUTION',sentence)
                for target in targets:
                    if target.get('reference_only'):
                        if value not in ('UNKNOWN','NOT_RUN'):add('SOURCE_REFERENCE_HAS_NO_BOUND_EXECUTION',sentence,target)
                    elif field in target and value!=target[field]:add('STRUCTURED_STATUS_CONTRADICTION',sentence,target)
            for match in re.finditer(_TASK_SUCCESS,sentence,re.I):
                if any(label.start()<=match.start() and match.end()<=label.end() for label in statuses):continue
                if not _affirmative(sentence,match):continue
                for target in targets_at(sentence,match.start(),local):
                    if target.get('reference_only') or target['task_status'] in ('FAIL','UNKNOWN','NOT_RUN'):add('SOURCE_REFERENCE_HAS_NO_BOUND_EXECUTION' if target.get('reference_only') else 'UNSUPPORTED_TASK_SUCCESS',sentence,target)
            for match in re.finditer(r'(?:编译|仿真|运行|工具|链路)(?:已经|已)?(?:成功|通过)|(?:编译|仿真|运行|工具|链路)\s*[:：=]?\s*PASS',sentence,re.I):
                if not _affirmative(sentence,match):continue
                for target in targets_at(sentence,match.start(),local):
                    if target.get('reference_only'):add('SOURCE_REFERENCE_HAS_NO_BOUND_EXECUTION',sentence,target)
            dimension_claim=r'\d+(?:\.\d+)?\s*(?:mm|毫米|[×x]\s*\d+(?:\.\d+)?)'
            for match in re.finditer(dimension_claim+r'[^，,。；\n]{0,12}?((?:已经|已)?通过)',sentence,re.I):
                if not _affirmative(sentence,match,1):continue
                for target in targets_at(sentence,match.start(1),local):
                    if target.get('reference_only') or target['task_status'] in ('FAIL','UNKNOWN','NOT_RUN'):add('UNSUPPORTED_TASK_SUCCESS',sentence,target)
            hard=re.search('hard_contradictions',sentence)
            if hard:
                for target in targets_at(sentence,hard.start(),local):
                    if target.get('hard_contradictions_explicit') and target['hard_contradictions']==[]:
                        if re.search(r'未见显式|无该字段|没有(?:显式)?[^，,。]{0,15}字段|未包含|缺少|非空|不为空|存在硬(?:几何)?矛盾',sentence) and not re.search(r'不能|不得|不声称|不是非空|并非非空',sentence):add('EXPLICIT_EMPTY_HARD_CONTRADICTIONS_MISREAD',sentence,target)
            if mentions(sentence):
                tail=targets_at(sentence,len(sentence),local)
                local=tail[0] if len(tail)==1 else False
            if len([row for row in facts if row['physical_status']=='UNKNOWN'])>1 and re.search(r'只有|仅有|仅\s*\d',sentence) and 'UNKNOWN' in sentence:
                claim=re.search(r'只有|仅有|仅\s*\d',sentence)
                if _affirmative(sentence,claim) and not re.search(r'旧错误|历史错误|错误(?:说法|解释)|是错误|已纠正',sentence) and re.search(r'physical|物理|为\s*UNKNOWN',sentence,re.I):add('EXCLUSIVE_UNKNOWN_CLAIM_CONTRADICTS_FACTS',sentence)
    report=dict(schema='v12-known-explanation-fact-check-1',rule_version='BOUND_SOURCE_AND_MARKDOWN_SCOPE_20261101',status='KNOWN_FACT_CONFLICT' if issues else 'NO_KNOWN_FACT_CONFLICT',
        body_displayed=not bool(issues),full_semantic_validation=False,coverage='STRUCTURED_STATUS_AND_OBSERVED_CONTRADICTION_PATTERNS_ONLY',issues=issues,
        raw_answer_sha256=hashlib.sha256(raw_answer.encode('utf8')).hexdigest(),facts_sha256=_digest(facts),facts=facts,
        known_execution_versions=sorted(known_execution_versions),known_source_versions=sorted(known_source_versions),
        source_references=references,source_references_sha256=_digest(references),
        limitation='只检查已知字段冲突与有限表达模式；未发现冲突不等于全文语义、物理推理或任意自然语言均正确。')
    from .case_contact_review import task_conclusion
    primary=next((row for row in facts if row['version_id']==(binding.get('latest_request_execution') or binding.get('reviewed_execution') or {}).get('version_id')),None)
    description=task_conclusion(primary)[0] if primary else '尚未运行：本请求没有绑定执行结果。'
    header=['【任务结论】'+description,'程序核对事实：请求 '+str(binding.get('request_id','UNKNOWN'))+'；项目 '+str(binding.get('project_id','UNKNOWN'))+'。']
    if not facts:header.append('本请求尚无绑定执行结果；不作任务完成或物理通过声明。')
    for row in source_only:
        header.append('已绑定基线来源 '+row['version_id']+'：仅为源码/参数来源；没有因此获得本版本执行或任务PASS证据。')
    if facts:
        if all(row.get('verification_scope')=='SOURCE_BOUND_ASYM2_STAGE_AND_VALIDITY' for row in facts):
            header.append('能力限制：Asym2固定简化模型内的有效性、逐级受力和路线检查；真实车辆能力尚未验证。')
        else:
            from .case_contact_validity import CAPABILITY_LIMITATION
            header.append(CAPABILITY_LIMITATION)
    for row in facts:
        header.append(row['version_id']+' / '+str(row['task_id'])+'：工具 '+row['tool_status']+'；链路 '+row['chain_status']+'；有限采样检查 '+row['validity_status']+'；任务 '+row['task_status']+'；完成状态 '+row['completion_status']+'；物理 '+row['physical_status']+'。')
        if row['hard_contradictions_explicit']:header.append('hard_contradictions='+json.dumps(row['hard_contradictions'],ensure_ascii=False,separators=(',',':'))+'；仅代表所绑定的有限检查。')
        if row['source_binding']:header.append('来源绑定：'+json.dumps(row['source_binding'],ensure_ascii=False,separators=(',',':')))
    if issues:body='解释未通过事实核对；模型原文保留供追溯。'
    else:body='【结果说明（模型生成）】已核对已知关键字段，未发现事实冲突；进一步推断不作额外验证，全文语义及推理未验证。\n'+raw_answer
    return dict(answer='\n'.join(header)+'\n\n'+body,raw_answer=raw_answer,program_fact_lines=header,supplemental_analysis=body,explanation_validation=report)
