"""Read-only result rendering from one currently bound fact projection.

Saved execution/narration remains historical evidence. Program-owned verdicts
and times use the current review, never provider prose or a saved text check.
"""
from copy import deepcopy

from .case_contact_review import IDENTITY_FIELDS,current_summary,task_conclusion


def _mapping(value):
    return value if isinstance(value,dict) else {}


def _nodes(execution):
    """Known execution envelopes only; never search specs or free text."""
    execution=_mapping(execution);task=_mapping(execution.get('task'))
    for base in (execution,task,_mapping(task.get('result')),_mapping(execution.get('result')),_mapping(execution.get('snapshot'))):
        if base:
            yield base
            if _mapping(base.get('outcome')):yield base['outcome']


def prepare_result(result,*,request_id=None,stage_context=None):
    """Return a display copy, normalized execution, review flag and top lines."""
    layers=[];seen=set();view=_mapping(result)
    while id(view) not in seen:
        seen.add(id(view));layers.append(view)
        child=view.get('partial') or view.get('pipeline')
        if not isinstance(child,dict) or not child:break
        view=child
    view=deepcopy(view)
    for key in ('stage','explanation','proposal'):
        if not view.get(key):view[key]=deepcopy(next((layer[key] for layer in reversed(layers) if layer.get(key)),{}))
    view['reused']=any(layer.get('reused') for layer in layers)
    # A fresh readback wraps the saved pipeline. Its current state must take
    # precedence over the historical state stored inside that pipeline.
    states=[_mapping(layer.get('state')) for layer in layers]
    current_state=next((state for state in states if state),{})
    view['state']=deepcopy(current_state)
    current=_mapping(current_state.get('latest_request_execution')) or None
    reviewed=current is None and bool(_mapping(current_state.get('reviewed_execution')))
    if reviewed:current=current_state['reviewed_execution']
    executions=[layer['execution'] for layer in reversed(layers) if _mapping(layer.get('execution'))]
    raw=executions[0] if executions else current or {}
    nodes=[node for item in executions for node in _nodes(item)]
    execution={}
    for node in _nodes(raw):execution.update(deepcopy(node))
    execution.pop('task',None)
    expected=request_id or next((layer.get('request_id') or _mapping(layer.get('state')).get('request_id') for layer in layers if layer.get('request_id') or _mapping(layer.get('state')).get('request_id')),None)
    requests=[value for layer in layers for value in (layer.get('request_id'),_mapping(layer.get('state')).get('request_id')) if value]
    conflict=bool(expected and any(value!=expected for value in requests))
    candidates=nodes+([current] if current else [])
    # A version number alone is not identity. Reject crossing project/task or
    # source/spec/manifest identities even when a version happens to match.
    for key in IDENTITY_FIELDS:
        values={str(row[key]) for row in candidates if row.get(key) is not None}
        conflict|=len(values)>1
    # The review can revise historical task validity, but never erase a
    # conflicting tool receipt or a TEST_ONLY marker in another envelope.
    for key in ('tool_status','evidence_status'):
        conflict|=len({str(row[key]).upper() for row in candidates if row.get(key) is not None})>1
    state_conflict=len({str(row['state']).upper() for row in candidates if row.get('state') is not None})>1
    test_only=any(row.get('test_only') is True for row in candidates)
    def old_diagnostic_receipt(row):
        review=(current or {}).get('current_contact_review') or {}
        recorded=_mapping(row.get('result')) or _mapping(_mapping(row.get('task')).get('result')) or row
        return (review.get('legacy_center_failure_reclassified') is True
                and review.get('execution_evidence_status')=='PASS'
                and recorded.get('tool_status')=='PASS'
                and recorded.get('chain_status')=='BLOCKED_INVALID_MODEL')
    failed_receipt=any(row.get('ok') is False and not old_diagnostic_receipt(row) for row in candidates)
    projects={str(state['project_id']) for state in states if state.get('project_id') is not None}
    projects.update(str(layer['project_id']) for layer in layers if layer.get('project_id') is not None)
    projects.update(str(row['project_id']) for row in candidates if row.get('project_id') is not None)
    conflict|=len(projects)>1
    if expected and not reviewed:
        conflict|=any(node.get('request_id') not in (None,expected,'case_execute_'+expected,'case_execute_repair_1_'+expected) for node in nodes)
    if current is not None:
        if not current.get('current_contact_review') and execution.get('task_status') is not None and current.get('task_status') is not None:
            conflict|=execution['task_status']!=current['task_status']
        execution.update(deepcopy(current))
    if test_only:execution['test_only']=True
    if failed_receipt:
        if execution.get('tool_status')=='PASS':conflict=True
        else:execution['tool_status']='TOOL_FAIL'
    summary=current_summary(execution) if execution else {}
    # A bound model contradiction remains reportable across a failed/succeeded
    # receipt disagreement; that disagreement cannot manufacture a route PASS.
    conflict|=state_conflict
    if execution:
        execution['historical_mission']=deepcopy(execution.get('mission') or execution.get('mission_assessment') or {})
        execution.update(summary)
        execution['mission']=deepcopy(summary['current_contact_review'].get('current_assessment') or {})
    description,passed=task_conclusion(summary,binding_conflict=conflict) if execution else ('尚未运行：本请求没有执行结果。',False)
    if reviewed:description='历史结果：'+description+' 本次仅只读查看，未新增运行。'
    lines=['【任务结论】'+description]
    if execution:
        from .case_contact_review import ASYM2_RULE_VERSION
        if execution.get('current_contact_review',{}).get('rule_version')==ASYM2_RULE_VERSION:
            lines.append('能力限制：Asym2固定简化模型内的有效性、逐级受力和路线检查；真实车辆能力尚未验证。')
        else:
            from .case_contact_validity import CAPABILITY_LIMITATION
            lines.append(CAPABILITY_LIMITATION)
    from .case_stage_facts import stage_lines
    lines+=stage_lines(stage_context,request_id=expected)
    if not conflict and (passed or (summary.get('route_status')=='PASS' and summary.get('current_review_state')=='CURRENT_BOUND_REVIEW' and summary.get('current_contact_review',{}).get('execution_evidence_status')=='PASS')):
        detail=('完成情况：' if passed else '轨迹覆盖观测：')+'前向上楼20级、前向下楼20级，末轮驶出出口后继续前行。'
        value=execution['mission'].get('completion_time')
        if isinstance(value,(int,float)) and not isinstance(value,bool):
            import math
            if math.isfinite(value):detail+=' 完成时刻：'+str(value)+' s（时间展示）。'
        lines.append(detail)
    explanation=deepcopy(_mapping(view.get('explanation')))
    raw_answer=explanation.get('raw_answer',explanation.get('answer'))
    status=explanation.get('status')
    # An old validation report is an old report, not permission to display its
    # body. Re-evaluate the untouched provider text against this display fact.
    if status=='NOT_CHECKED_USE_RESULT_FULL':
        lines.append('简洁结果未核查解释正文；如需核查说明，请用/result full（只读）')
    elif isinstance(raw_answer,str) and (status in (None,'PASS','COMPLETE_SAVED_PRODUCT_RESPONSE') or explanation.get('explanation_validation')):
        from .case_semantic_guard import protect_explanation
        binding=dict(request_id=expected,project_id=summary.get('project_id'),**({'reviewed_execution':summary} if reviewed else {'latest_request_execution':summary}))
        # Only a current top-level service projection supplies extra versions;
        # old explanation/validation records are never a fact source. A fresh
        # terminal overlay produces this projection after explicitly named,
        # finite lazy reviews, and its main identity must still match here.
        projection=layers[0] if _mapping(layers[0].get('current_fact_binding')) else {}
        fact_binding=projection.get('current_fact_binding') or {}
        source_references=()
        primary=_mapping(fact_binding.get('latest_request_execution') or fact_binding.get('reviewed_execution'))
        if primary and all(primary.get(key)==summary.get(key) for key in IDENTITY_FIELDS) and fact_binding.get('project_id')==summary.get('project_id'):
            binding['explicitly_reviewed_executions']=(fact_binding.get('explicitly_reviewed_executions') or [])[:8]
            if fact_binding.get('request_id')==expected:
                source_references=projection.get('current_source_references') or ()
                if fact_binding.get('source_reference_binding_error'):binding['source_reference_binding_error']=fact_binding['source_reference_binding_error']
        protected=protect_explanation(raw_answer,binding,source_references=source_references)
        if conflict:
            protected['supplemental_analysis']='解释未通过事实核对；当前结果绑定冲突，模型原文保留供追溯。'
            protected['explanation_validation']['body_displayed']=False
            protected['explanation_validation']['status']='KNOWN_FACT_CONFLICT'
            protected['explanation_validation']['issues'].append(dict(code='RESULT_BINDING_CONFLICT',claim='结果与当前执行身份冲突。'))
            protected['answer']='\n'.join(protected['program_fact_lines'])+'\n\n'+protected['supplemental_analysis']
        explanation.update(protected)
        lines.append('结果说明（模型生成）：已保存；已核对已知关键字段，进一步推断不作额外验证。')
    elif raw_answer:
        explanation.pop('supplemental_analysis',None)
        lines.append('产品解释未完成：'+str(status or '尚无完整解释')+'；模型原文仅留证据。')
    else:lines.append('产品解释未完成：'+str(status or '尚无完整解释')+'；已有运行结果与解释分别记录。')
    view['explanation']=explanation
    # Retain the compatibility field without calling any model analysis verified.
    view['verified_current_explanation_available']=False
    view['result_binding_conflict']=bool(conflict or summary.get('current_review_state')=='BINDING_CONFLICT')
    return view,execution,reviewed,lines


def concise_result_lines(view,execution,reviewed,headlines,*,evidence_root=None):
    """Render only the already prepared projection; never infer from a plan."""
    lines=[headlines[0]]
    if view.get('reused'):lines.append('以下为已保存结果的只读读回。')
    if execution:
        conflict=view.get('result_binding_conflict')
        parameters=execution.get('parameters') or (execution.get('runtime_parameter_evidence') or {}).get('parameters') or {}
        if not parameters:
            source=(execution.get('frozen_spec') or {}).get('artifact',{}).get('modelica_source')
            if source:
                try:
                    from .case_scope import modelica_parameters
                    parameters=modelica_parameters(source)[0]
                except (ValueError,KeyError,TypeError):pass
        def number(key,scale=1):
            import math
            value=parameters.get(key)
            return format(value*scale,'.6g') if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) else '未知'
        review=execution.get('current_contact_review') or {}
        from .case_contact_review import ASYM2_RULE_VERSION
        asym2=review.get('rule_version')==ASYM2_RULE_VERSION
        actual=(not conflict and execution.get('current_review_state')=='CURRENT_BOUND_REVIEW'
                and review.get('execution_evidence_status')=='PASS' and execution.get('tool_status')=='PASS')
        condition_label='执行记录工况（绑定待核）：' if conflict else '实际工况：' if actual else '执行记录工况：'
        lines.append(condition_label+'总质量 '+number('m')+' kg；'+('主体楼梯高度 ' if asym2 else '楼梯高度 ')+number('stepH',1000)+' mm × 深度 '+number('stepD',1000)+' mm。')
        if asym2:
            lines.append('固定尾阶：77.5/65 mm；尾段间距220/80 mm，20级；主体H/D不代表均匀全梯。')
            assessment=review.get('current_assessment') or {}
            if assessment.get('classification'):lines.append('独立分类：'+str(assessment['classification']))
            progress=(assessment.get('stage_report') or {}).get('progress') or {}
            if progress:
                location_labels=dict(entrance='入口',main18_middle='主体中段',main18_late='主体后段',tail19='第19级尾阶',tail20='第20级尾阶',top_platform='顶部平台',platform_before_down_entry='下楼入口前平台',descent='下楼段',route_complete='全程已完成')
                lines.append('逐级进展：双主轮可信受力清阶至第 '+str(progress.get('last_both_main_supported_clearance_step','未知'))+' 级；末段位置 '+location_labels.get(progress.get('terminal_location'),'未知')+'。')
        lines.extend(line for line in headlines if line.startswith(('完成情况：','轨迹覆盖观测：')))
        lines.append(('记录字段（绑定冲突，不能据此认定通过）：' if conflict else '依据：')+'工具 '+str(execution.get('tool_status','UNKNOWN'))+'；路线 '+str(execution.get('route_status','UNKNOWN'))+'；接触诊断 '+str(execution.get('diagnostic_status','UNKNOWN'))+'；有限采样检查 '+str(execution.get('validity_status','UNKNOWN'))+'。')
        contact=review.get('contact_validity') or {}
        count=(contact.get('counters') or {}).get('full_circle_inside_solid_sample_count')
        if count is not None:
            lines.append(('异常记录：' if conflict else '整圆入楼梯实体异常：')+str(count)+' 个采样。')
        for issue in (review.get('issues') or [])[:2]:lines.append('复核说明：'+str(issue))
        if reviewed:lines.append('本次仅只读查看已绑定历史结果，未新增运行。')
        lines.append('执行版本：'+str(execution.get('version_id','未知'))+'；任务：'+str(execution.get('task_id','未知')))
    else:
        lines.extend(line for line in headlines[1:] if not line.startswith('能力限制：'))
    explanation=view.get('explanation') or {}
    validation=explanation.get('explanation_validation') or {}
    if validation.get('body_displayed') is False:
        lines.append('结果说明未通过事实核对；原文保留，任务结论按执行证据单列。')
    elif execution:
        lines.extend(line for line in headlines if line.startswith(('产品解释未完成：','简洁结果未核查解释正文；')))
    if evidence_root:lines.append('本请求证据：'+str(evidence_root))
    lines.append('完整说明与详细字段：/result full（只读）。')
    if execution:
        lines.append('适用范围：当前简化模型仿真；真实车辆能力尚未验证。')
    return lines
