"""Repeated natural-language requests on the existing ArtifactService lifecycle.

Planning, trusted local confirmation, product source generation and ordinary
MCP execution are separate stages. No import starts a model or a process.
"""
import asyncio,json,os,sys,time,sqlite3
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from pathlib import Path
from contextlib import asynccontextmanager,closing
from .case_paths import ROOT,PROJECT_ROOT,PROJECT_DB,REPO,CONFIG,read_json,save_json,sha_value,sha_file
from .case_backend import service,module
from .case_scope import modelica_parameters,validate_request_plan,check_delta,UnsupportedRequestDesign,REQUEST_PARAMETERS,STAIR_PLAN_PARAMETERS,MASS_SCOPE_PACKET,STAIR_SCOPE_PACKET
from .case_progress import emit_progress
from .case_diagnostics import phase,exception_record,attach_calls,timeout_binding,tool_name
from .case_product_context import bootstrap_source,project_messages,projected_context_matches,outcome_summary
from .case_contact_review import current_summary,mission_summary,with_review,RULE as CONTACT_REVIEW_RULE
from .case_contact_family import CONTACT_SCOPE_PACKET,ContactSourceReviewRequired,is_contact_scope,planning_context,save_pending,pending_response
from .case_strict_tools import transport_profile, generation_tool, STRICT_PROFILE, STRICT_PROFILES, FLAT_PLAN_PROFILE, plan_transport_guide
from .case_protocol_failures import failure_fingerprint, reject_repeated_failure
from .case_semantic_guard import require_plan_semantics,protect_explanation,plan_diagnostic_warnings,baseline_source_references

PLAN_FIELDS=('intent','support','missing','unsupported','requirements','design','proposed_parameters','defaults','unknowns','rationale','scope')
PLAN_TOOL=dict(type='function',function=dict(name='case_request_plan',description='提出供用户审查的新需求方案；不能确认、生成源码或执行。',parameters=dict(type='object',properties=dict(packet=dict(type='object',properties={k:{} for k in PLAN_FIELDS},required=list(PLAN_FIELDS),additionalProperties=False)),required=['packet'],additionalProperties=False)))
for _field in ('missing','unsupported','defaults','unknowns'):
    PLAN_TOOL['function']['parameters']['properties']['packet']['properties'][_field]=dict(type='array',items=dict(type='string'))
_packet_properties=PLAN_TOOL['function']['parameters']['properties']['packet']['properties']
_packet_properties['intent']=dict(type='string',enum=['CHANGE','QUESTION'])
_packet_properties['support']=dict(type='string',enum=['SUPPORTED','NEEDS_INPUT','UNSUPPORTED'])
_packet_properties['rationale']=dict(type='string',minLength=1,description='完整中文依据说明，必须为非空字符串，不得返回对象。')
_packet_properties['requirements']=dict(type='array',items=dict(type='object',properties={k:dict(type='string',minLength=1) for k in ('text','source_id','source_text')},required=['text','source_id','source_text'],additionalProperties=False))
_packet_properties['design']=dict(oneOf=[dict(type='object',properties=dict(summary=dict(type='string',minLength=1),assumptions=dict(type='array',items=dict(type='object',properties=dict(text=dict(type='string'),origin=dict(type='string',enum=['model'])),required=['text','origin'],additionalProperties=False)),parameters=dict(type='array',items=dict(type='object',properties=dict(name=dict(type='string',minLength=1),value=dict(type='number'),unit=dict(type='string'),origin=dict(type='string',enum=['model']),basis=dict(type='string')),required=['name','value','unit','origin','basis'],additionalProperties=False))),required=['summary','assumptions','parameters'],additionalProperties=False),dict(type='object',maxProperties=0)])
_packet_properties['scope']=dict(oneOf=[dict(type='object',properties={k:dict(type='string',enum=[v]) for k,v in scope.items()},required=list(scope),additionalProperties=False) for scope in (MASS_SCOPE_PACKET,STAIR_SCOPE_PACKET,CONTACT_SCOPE_PACKET)]+[dict(type='object',maxProperties=0)])
_packet_properties['proposed_parameters']=dict(oneOf=[dict(type='object',properties={k:dict(type='number') for k in names},required=list(names),additionalProperties=False) for names in (REQUEST_PARAMETERS,STAIR_PLAN_PARAMETERS)]+[dict(type='object',maxProperties=0)])
PLAN_STRUCTURE_GUIDE=('工具参数结构：arguments顶层仅有packet。packet的'+str(len(PLAN_TOOL['function']['parameters']['properties']['packet']['required']))+'个必需字段全部同级：'+', '.join(PLAN_TOOL['function']['parameters']['properties']['packet']['required'])+'。packet.scope与packet.design同级；非空packet.design仅含'+', '.join(_packet_properties['design']['oneOf'][0]['properties'])+'，不能将scope或其余packet字段嵌入design。完整返回工具对象，不将字段放进reasoning_content；NEEDS_INPUT/UNSUPPORTED仍按原schema与运输编码返回，不伪造可执行方案。')
PLAN_PROMPT='''你是已有MBSE受约束改型Agent。完整理解用户原文，不把它当固定改质量数字的模板。输出中文说明，只调用case_request_plan一次。packet恰含intent(CHANGE/QUESTION)、support(SUPPORTED/NEEDS_INPUT/UNSUPPORTED)、missing、unsupported、requirements、design、proposed_parameters、defaults、unknowns、rationale、scope。
先按用户选择的实际baseline_snapshot理解相对变化；原成功案例85kg不是每个新需求的基线。支持两种独立范围，不可混合：A同结构家族原重心增加理想点质量、I_pitch继承，以及有据的主/鼻轮等效扭矩调整；B只改楼梯级高stepH和/或踏步深stepD，车辆质量/惯量/扭矩/全部几何/接触/控制、台阶数、平台、任务、观察区间/容差全部继承。B单改高度时深度必须明确继承所选基线，单改深度时高度同理；数值换算为米、正有限，不能从105kg等历史示例误取质量。A质量应明确为总质量或从基线无歧义推得；设备载荷、总质量、增加质量不明确时NEEDS_INPUT。几何、轮径/轴位、接触、其他地形、控制、目标速度、惯量分布变化及A+B混合UNSUPPORTED，不猜测新机械设计。A扭矩可保持基线，也可在[基线扭矩,基线扭矩*新质量/基线质量]中有据选择，不代表真实电机额定。
SUPPORTED CHANGE时：requirements每项{text,source_id,source_text}，引用trusted_sources精确非空子串，至少一项引用本次原始请求源；不要把历史目标当新要求。design={summary,assumptions:[{text,origin:"model"}],parameters:[{name,value,unit,origin:"model",basis}]}；design必须逐项包含proposed_parameters全部数值并一致。A proposed_parameters恰为m/I_pitch/T_main_max/T_nose_max四项，scope恰为{change_kind:"center_of_mass_point_load",mass_location:"original_center_of_mass",inertia_assumption:"ideal_point_mass",frozen:"geometry_contact_control_terrain_task"}，defaults披露点质量/I不变近似。B proposed_parameters恰为m/I_pitch/T_main_max/T_nose_max/stepH/stepD六项，前四项逐值继承基线，scope恰为{change_kind:"stair_dimensions",frozen:"vehicle_geometry_contact_control_stair_count_platform_task_observation"}；defaults披露未改楼梯维度、车辆/台阶数/平台/任务继承，楼梯变化不保证通过。B只允许原公式H_peak=n_steps*stepH、x_end_up=x_start_up+n_steps*stepD、x_start_down=x_end_up+len_flat、x_end_down=x_start_down+n_steps*stepD自然派生，不把派生量作为独立输入。两种范围均明确继承仿真区间/容差、DASSL及简化模型限制。missing/unsupported为空。其他情况requirements=[]、design={}、proposed_parameters={}、scope={}，明确缺项或不支持内容，不提出可执行假方案。QUESTION只读问题不得伪装变更。
missing/unsupported/defaults/unknowns必须是字符串列表，空值用[]，不得用对象{}；defaults不是键值对象。不能授予权限、确认、编造运行结果。历史成功/当前简化模型结果与实物能力必须分开。原源码的Euler嵌套注解不代表实际执行，继承链显式DASSL。'''
PLAN_PROMPT+='\nrationale必须是完整、非空的中文字符串；所有packet字段必须齐全，不能放入reasoning_content代替工具参数。'
PLAN_PROMPT+='\nbaseline_snapshot.frozen_spec.design及其assumptions只记录历史方案，不是本次变更指令。必须按当前raw_request、所选源码和当前scope独立重写design.assumptions，不能复制历史“本次唯一改动”等表述冒充当前操作。stair_dimensions的当前变更只有H/D及原公式派生量，基线已有接触数值保护是冻结继承事实，不能说本次修改或再次修复它。'
PLAN_PROMPT+='\nmissing仅列尚缺且不能从可信资料确定、确实阻止提出方案的关键用户事实，例如总质量/增加质量含义未明确；不能静默补用户事实。用户未明确的原重心理想点载必须列为模型假设并随方案确认，不能声称用户已指定。Agent确认后待补的基准/候选SysML属于required_source_completion并须在defaults披露，不属于missing用户事实；历史导入缺项不自动等于当前缺项。方案待确认、预算/执行待批准由程序状态管理，不能列为missing或据此将本来支持的方案判为NEEDS_INPUT。风险、近似和未知结果列unknowns，不把未知写成已经决定修改冻结参数。'
PLAN_PROMPT+='\n明确要求按当前所选baseline原样重新仿真也受支持，使用现有CHANGE路径和范围A，不因参数无变化而UNSUPPORTED，不另造intent；m/I_pitch/T_main_max/T_nose_max全部逐值继承当前所选基线，零新增点质量，不杜撰物理变化。原样是相对于所选baseline，不代表恢复任意历史质量。新的运行请求仍需新的需求来源、方案确认及程序执行授权，生成新候选并真实编译仿真，不能把历史结果或旧请求终态充当本次运行。单纯查旧结果/解释仍为QUESTION只读。'
PLAN_PROMPT+='\n'+PLAN_STRUCTURE_GUIDE
GEN_PROMPT='''你是同一MBSE产品Agent。按已确认的新需求方案生成完整双源码，只调用proposal_prepare一次，arguments={"spec":{...}}。严格按confirmed_plan.scope和source_delta_contract：center_of_mass_point_load仅改m/T_main_max/T_nose_max，I_pitch继承且楼梯不变；stair_dimensions仅改stepH/stepD，其余参数/车辆质量/惯量/扭矩不变。保留全部原依赖表达式，楼梯四派生量仅由原公式重算，不写成常量；方程、全部地形floor差异、几何、接触、控制及类名原样。保留选中基线StopTime/Tolerance/Interval及settings/outputs/checks/libraries。不得补造新简化模型。SysML由你同步生成，全部有效共享参数/依赖表达式、req_N来源映射与Modelica一致，实际ANTLR解析校验。spec恰含schema_version/backend/baseline_ref/artifact/sources/requirements/design/libraries/settings/outputs/checks；若基线有trajectory_contract则原样带入。spec.sources必须按合同顺序仅返回[{"id":"来源ID"}]引用，不得返回text，不复述、改写或截短历史资料；真实原文由可信Store按ID读取。requirements/design及其余合同原样，requirements.source_text仍须精确引用可信原文。中文说明；JSON单层编码，源码解码后含真实换行。不能执行、确认、宣称PASS或扩大范围。'''
GEN_TOOL=dict(type='function',function=dict(name='proposal_prepare',description='保存已确认方案生成的完整双源码提案，执行仍须已有授权。',parameters=dict(type='object',properties=dict(spec=dict(type='object',properties=dict(sources=dict(type='array',minItems=1,items=dict(type='object',properties=dict(id=dict(type='string',minLength=1)),required=['id'],additionalProperties=False))),required=['sources'])),required=['spec'],additionalProperties=False)))
GEN_SPEC_FIELDS=('schema_version','backend','baseline_ref','artifact','sources','requirements','design','libraries','settings','outputs','checks')
_spec_schema=GEN_TOOL['function']['parameters']['properties']['spec']
_spec_schema['properties'].update(
    schema_version=dict(type='string',enum=['v11-model-artifact-1']),backend=dict(type='string',enum=['model-artifact']),
    baseline_ref=dict(type='object',properties={key:dict(type='string',minLength=1) for key in ('project_id','task_id','version_id','spec_digest','source_sha256','manifest_sha256')},required=['project_id','task_id','version_id','spec_digest','source_sha256','manifest_sha256'],additionalProperties=False),
    artifact=dict(type='object',properties={key:dict(type='string',minLength=1) for key in ('model_name','modelica_source','sysml_source')},required=['model_name','modelica_source','sysml_source'],additionalProperties=False),
    requirements=deepcopy(_packet_properties['requirements']),design=deepcopy(_packet_properties['design']['oneOf'][0]),
    libraries=dict(type='array',items=dict(type='string',enum=['Modelica']),maxItems=1,uniqueItems=True),
    settings=dict(type='object',properties=dict(stop_time=dict(type='number',exclusiveMinimum=0,maximum=600),intervals=dict(type='integer',minimum=2,maximum=12000)),required=['stop_time','intervals'],additionalProperties=False),
    outputs=dict(type='object',minProperties=1,maxProperties=64,additionalProperties=dict(type='string',minLength=1)),
    checks=dict(type='array',maxItems=16,items=dict(type='object',properties=dict(kind=dict(type='string',enum=['final_ge','final_le']),output=dict(type='string',minLength=1),threshold=dict(type='number'),source_id=dict(type='string',minLength=1),source_text=dict(type='string',minLength=1)),required=['kind','output','threshold','source_id','source_text'],additionalProperties=False)))
_spec_schema.update(required=list(GEN_SPEC_FIELDS),additionalProperties=False)

GEN_PROMPT+='\nspec必须直接是JSON对象，不能是再次JSON编码的字符串；原基准缺SysML时baseline_sysml_source与spec在arguments顶层同级，不能放进spec。'
PLAN_PROMPT+='\n当前继承接触模型尚未完整修复，受支持改型仅为原方程/路线诊断，不能验收物理越障能力。current_contact_review的FAIL/UNKNOWN必须在方案依据与unknowns中明确，不得把历史PASS当新物理保证；不能借本次接入放开几何/接触/控制源码锁。'
GEN_PROMPT+='\n当前仅生成受原源码锁约束的旧方程诊断候选；接触模型缺陷没有因此修复，不能承诺可信物理越障PASS或擅改物理方程。'


def flat_plan_prompt(prompt):
    """Render existing business rules in the selected shallow wire vocabulary."""
    import re
    result=prompt.replace(PLAN_STRUCTURE_GUIDE,'')
    result=re.sub(r'packet恰含[^\n]*?scope。','浅层字段与工具schema逐项一致。',result)
    result=result.replace('packet字段与合同完全一致','浅层字段与运输schema完全一致')
    result=result.replace('其他情况requirements=[]、design={}、proposed_parameters={}、scope={}',
        '其他情况requirements=[]、design_state=EMPTY、design_summary=""、design_assumptions=[]、design_parameters=[]，proposed_parameters和scope采用声明的空对象运输标记')
    nested='design={summary,assumptions:[{text,origin:"model"}],parameters:[{name,value,unit,origin:"model",basis}]}'
    result=result.replace(nested,'design_state=OBJECT，design_summary为摘要、design_assumptions为原assumptions数组、design_parameters为原parameters数组')
    result=result.replace('design.summary','design_summary').replace('design必须','design_parameters必须').replace('所有packet字段','所有浅层字段')
    if 'design={' in result or 'packet恰含' in result:
        raise ValueError('unrecognized nested PLAN prompt for flat profile')
    return result.rstrip()+'\n'+plan_transport_guide(PLAN_TOOL)


def _strict_generation_json(value,label):
    """Decode one complete JSON value, rejecting ambiguous keys and numbers."""
    def unique(pairs):
        result={}
        for key,item in pairs:
            if key in result:raise ValueError(label+' contains duplicate JSON key: '+key)
            result[key]=item
        return result
    def nonfinite(token):raise ValueError(label+' contains non-finite JSON number: '+token)
    try:
        decoded=json.loads(value,object_pairs_hook=unique,parse_constant=nonfinite)
        json.dumps(decoded,allow_nan=False)
    except (ValueError,TypeError,OverflowError) as exc:
        raise ValueError(label+' must be one complete finite JSON value: '+str(exc)) from exc
    return decoded


def generation_arguments(response,*,needs_baseline):
    """Only unwrap a complete spec JSON string once; never edit source text."""
    calls=response.get('tool_calls') or []
    if response.get('finish_reason') not in ('tool_calls','stop') or len(calls)!=1 or calls[0].get('function',{}).get('name')!='proposal_prepare':raise ValueError('complete single dual-source generation response required')
    raw=calls[0]['function']['arguments']
    args=_strict_generation_json(raw,'generation arguments') if isinstance(raw,str) else deepcopy(raw)
    if not isinstance(args,dict):raise ValueError('generation arguments must be a JSON object')
    expected={'spec','baseline_sysml_source'} if needs_baseline else {'spec'}
    if set(args)!=expected:raise ValueError('generation argument fields differ; missing='+','.join(sorted(expected-set(args)))+'; extra='+','.join(sorted(set(args)-expected)))
    if needs_baseline and not isinstance(args['baseline_sysml_source'],str):raise ValueError('complete original baseline_sysml_source string required at arguments top level')
    normalization=None
    if isinstance(args['spec'],str):
        if response.get('request_tool_output_profile') in STRICT_PROFILES:
            raise ValueError('strict generation requires object spec; encoded JSON recovery forbidden')
        encoded=args['spec'];decoded=_strict_generation_json(encoded,'generation spec')
        if not isinstance(decoded,dict):raise ValueError('generation spec must decode exactly once to a JSON object; nested encoding is not supported')
        normalization=dict(schema='v12-generation-spec-json-normalization-1',source_response_sha256=sha_value(response),field='arguments.spec',transformation='STRICT_SINGLE_JSON_STRING_TO_OBJECT',additional_json_decodes=1,original_spec_string_sha256=sha_value(encoded),decoded_spec_value_sha256=sha_value(decoded),other_argument_values_unchanged=True,source_text_values_unchanged=True,business_values_changed=False)
        args['spec']=decoded
    if not isinstance(args['spec'],dict):raise ValueError('generation spec must be a JSON object, not an array or scalar')
    try:json.dumps(args,allow_nan=False)
    except (ValueError,TypeError,OverflowError) as exc:raise ValueError('generation arguments must contain only finite JSON values') from exc
    return args,normalization


def plan_suffix_arguments(response):
    """Recognize one redundant final outer brace for the planning tool only."""
    calls=response.get('tool_calls') or []
    if response.get('finish_reason') not in ('stop','tool_calls') or len(calls)!=1 or calls[0].get('type')!='function' or calls[0].get('function',{}).get('name')!='case_request_plan':raise ValueError('complete single case_request_plan response required for suffix normalization')
    raw=calls[0]['function'].get('arguments')
    if not isinstance(raw,str):raise ValueError('original planning arguments string required')
    def unique(pairs):
        value={}
        for key,item in pairs:
            if key in value:raise ValueError('duplicate plan JSON key: '+key)
            value[key]=item
        return value
    def nonfinite(token):raise ValueError('non-finite plan JSON number: '+token)
    start=len(raw)-len(raw.lstrip(' \t\r\n'))
    args,end=json.JSONDecoder(object_pairs_hook=unique,parse_constant=nonfinite).raw_decode(raw,start)
    if raw[end:].strip(' \t\r\n')!='}':raise ValueError('only one redundant final outer planning brace is eligible')
    if not isinstance(args,dict) or set(args)!={'packet'} or not isinstance(args['packet'],dict):raise ValueError('complete single-layer packet object required for suffix normalization')
    try:json.dumps(args,allow_nan=False)
    except (ValueError,TypeError,OverflowError) as exc:raise ValueError('only finite plan JSON values are eligible') from exc
    # Reproduce the original strict failure; ordinary valid JSON is not migrated.
    try:json.loads(raw)
    except json.JSONDecodeError as exc:
        if exc.msg!='Extra data' or exc.pos!=end+len(raw[end:])-len(raw[end:].lstrip(' \t\r\n')):raise ValueError('original error is not the identified final planning brace') from exc
        original_error=type(exc).__name__+': '+str(exc)
    else:raise ValueError('planning response has no redundant final brace')
    audit=dict(schema='v12-plan-json-suffix-normalization-1',tool_name='case_request_plan',source_response_sha256=sha_value(response),original_arguments_sha256=sha_value(raw),strict_prefix_start=start,strict_prefix_end=end,ignored_suffix=raw[end:],transformation='ONE_REDUNDANT_OUTER_CLOSE_AFTER_COMPLETE_PACKET_OBJECT',parsed_arguments_sha256=sha_value(args),packet_digest=sha_value(args['packet']),business_values_changed=False,source_text_values_changed=False,original_error=original_error)
    return args['packet'],audit


def imported_obligations(ctx):
    """Check current selected-source capability; retain import claims as history."""
    marker=Path(ctx['project_root'])/'case.json'
    if not marker.exists():return {}
    from .case_projects import resolve_case,verify_case,support_for_source
    case=verify_case(resolve_case(read_json(marker)['case_id']))
    current=support_for_source(ctx['baseline_snapshot']['frozen_spec']['artifact']['modelica_source'])
    historical_missing=[item for item in case['missing'] if item!='请提供用户确认的原案例历史成功范围；导入不构成成功证明。']
    selected_sysml=baseline_sysml_available(ctx)
    # These two records are emitted by the existing importer, not arbitrary
    # user omissions mentioning SysML. Preserve them as history and classify
    # their program-owned work separately; every other missing item remains.
    importer_source_work={
        '提供的SysML尚未验证与所选Modelica的关联；确认方案后由产品模型核验并补齐。',
        '尚无基准SysML文件；确认方案后由产品模型补齐并独立验证。',
    }
    critical_missing=[item for item in historical_missing if item not in importer_source_work]
    return dict(case_id=case['case_id'],support=current,historical_import_support=deepcopy(case['support']),historical_success_statement=case.get('historical_success_statement',''),historical_success_verified=False,reference_origin='USER_PROVIDED_REFERENCE_HISTORY_NOT_INDEPENDENTLY_VERIFIED',historical_missing=historical_missing,selected_baseline_sysml_present=selected_sysml,missing=critical_missing,required_source_completion=[] if selected_sysml else ['在已确认范围内补齐原始基准SysML及候选SysML；原基准只映射原资料，本次新要求只映射候选。'])

def validate_import_plan(ctx,packet):
    obligations=imported_obligations(ctx)
    if packet.get('intent')=='CHANGE' and packet.get('support')=='SUPPORTED' and obligations:
        if obligations['support']['status']!='SUPPORTED':raise UnsupportedRequestDesign('imported source family is unsupported; existing mission checker cannot be applied')
        if obligations['required_source_completion'] and not any('sysml' in text.casefold() for text in packet.get('defaults',[])):
            raise ValueError('displayed plan must explicitly disclose original-baseline and candidate SysML completion in defaults')
    return obligations

def baseline_sysml_available(ctx):
    if ctx['baseline_snapshot']['frozen_spec']['artifact'].get('sysml_source'):return True
    path=Path(ctx['root'])/'baseline-sysml-reference.json'
    if path.exists():
        from .case_contact_numerics import referenced_sysml
        return bool(referenced_sysml(ctx))
    return False


def baseline_completion_status(ctx):
    available=bool(ctx['baseline_snapshot']['frozen_spec']['artifact'].get('sysml_source')) or any((Path(ctx['root'])/name).exists() for name in ('baseline-sysml-reference.json','baseline-sysml-completion.json'))
    if not available:return dict(status='MISSING_NOT_COMPLETED',baseline_ref=ctx['baseline_ref'],original_v0_unchanged=True)
    source=baseline_sysml_for_context(ctx)
    return dict(status='VERIFIED',baseline_ref=ctx['baseline_ref'],sysml_source_sha256=__import__('hashlib').sha256(source.encode('utf8')).hexdigest(),original_v0_unchanged=True,verification_basis='TRUSTED_COMPLETION_REFERENCE' if (Path(ctx['root'])/'baseline-sysml-reference.json').exists() else 'SOURCE_BOUND_COMPLETION' if (Path(ctx['root'])/'baseline-sysml-completion.json').exists() else 'PRESENT_IN_BASELINE')


def baseline_sysml_for_context(ctx):
    """Read source-bound completion without changing the frozen v0/request."""
    original=ctx['baseline_snapshot']['frozen_spec']
    if original['artifact'].get('sysml_source'):return original['artifact']['sysml_source']
    if (Path(ctx['root'])/'baseline-sysml-reference.json').exists():
        from .case_contact_numerics import referenced_sysml
        return referenced_sysml(ctx)
    path=Path(ctx['root'])/'baseline-sysml-completion.json';saved=read_json(path)
    if saved.get('schema')!='v12-baseline-sysml-completion-1' or saved.get('request_digest')!=ctx['request_digest'] or saved.get('baseline_ref')!=ctx['baseline_ref'] or saved.get('baseline_snapshot_sha256')!=sha_value(ctx['baseline_snapshot']):raise ValueError('baseline SysML sidecar/request/source binding drift')
    completed=deepcopy(saved['completed_baseline_spec']);source=completed['artifact'].pop('sysml_source',None)
    if completed!=original or not source or saved.get('completed_spec_sha256')!=sha_value(saved['completed_baseline_spec']):raise ValueError('completed baseline changed original facts or lacks SysML')
    response_path=Path(saved.get('source_response_path','')).resolve()
    if not response_path.is_relative_to((Path(ctx['root'])/'turns').resolve()) or response_path.name!='response.json':raise ValueError('baseline SysML sidecar response path differs')
    response=read_json(response_path)
    if sha_value(response)!=saved.get('source_response_sha256'):raise ValueError('baseline SysML source response binding drift')
    args,_=generation_arguments(response,needs_baseline=True)
    if module('artifact_sysml').normalize_package_wrapper(args['baseline_sysml_source'])[0]!=source:raise ValueError('baseline SysML differs from original product output')
    generation=Path(ctx['root'])/'generation.json'
    if generation.exists() and read_json(generation).get('baseline_completion_sha256')!=sha_value(saved):raise ValueError('baseline SysML completion receipt binding drift')
    checked=module('model_artifact').validate_artifact(saved['completed_baseline_spec'],require_sysml=True)
    if not checked['ready'] or checked['spec_digest']!=saved['completed_spec_sha256']:raise ValueError('baseline SysML completion no longer validates')
    return source


def selected_source_facts(ctx,obligations=None):
    """One current selected-source projection for planning, questions and UI.

    Import-time omissions remain separate historical records. A validated
    source-bound completion is usable without changing the imported original.
    """
    obligations=obligations or {};snapshot=ctx['baseline_snapshot']
    original=snapshot['frozen_spec']['artifact'].get('sysml_source')
    present=isinstance(original,str) and bool(original.strip())
    completion=baseline_completion_status(ctx);available=completion['status']=='VERIFIED'
    source=baseline_sysml_for_context(ctx) if available else None
    return dict(schema='v12-selected-source-facts-2',rule_version='SELECTED_SOURCE_PRECEDENCE_20261101',
        baseline_version_id=ctx['baseline_version_id'],baseline_ref=deepcopy(ctx['baseline_ref']),
        baseline_snapshot_sha256=sha_value(snapshot),selected_baseline_sysml_present=present,
        selected_baseline_sysml_sha256=sha_value(source) if available else None,
        baseline_sysml_completion=completion,baseline_sysml_available=available,
        validated_baseline_sysml_available=available,baseline_sysml_reference_reused=available and not present,
        required_source_completion=[] if available else ['原基准SysML及候选SysML'],
        generation_source_rule='CANDIDATE_ONLY' if available else 'BASELINE_AND_CANDIDATE',
        critical_user_facts_missing=deepcopy(obligations.get('missing',[])),
        missing_classification='ONLY_UNRESOLVED_CRITICAL_USER_FACTS',
        confirmation_and_execution_authority='PROGRAM_OWNED_LIFECYCLE_NOT_MISSING_USER_FACTS',
        verification_basis='REQUEST_FROZEN_BASELINE_AND_BOUND_COMPLETION',
        unchanged_parameter_rerun=dict(supported=True,intent='CHANGE',relative_to_selected_baseline=True,requires_new_request_confirmation_and_execution=True,reuse_historical_terminal_result=False))

def parse_tool(response,name,key):
    calls=response.get('tool_calls') or []
    if response.get('finish_reason')=='length' or len(calls)!=1 or calls[0].get('function',{}).get('name')!=name:raise ValueError('complete single '+name+' tool response required')
    args=calls[0]['function']['arguments'];args=_strict_generation_json(args,'tool arguments') if isinstance(args,str) else args
    if not isinstance(args,dict) or set(args)!={key} or not isinstance(args[key],dict):raise ValueError('single-layer complete '+key+' required; no guessed JSON repair')
    return args[key]

def normalize_empty_nonexecutable_defaults(packet):
    """One lossless protocol shape adaptation; never invent explanatory data."""
    value=deepcopy(packet)
    if value.get('defaults')=={} and isinstance(value.get('defaults'),dict):
        if value.get('support') not in ('NEEDS_INPUT','UNSUPPORTED') or any(value.get(k) for k in ('requirements','design','proposed_parameters','scope')):
            raise ValueError('empty defaults normalization is restricted to nonexecutable clarification/refusal')
        value['defaults']=[]
    return value


def normalize_plan_rationale(packet):
    """A flat string map can be represented reversibly as complete JSON text."""
    value=deepcopy(packet);rationale=value.get('rationale')
    if not isinstance(rationale,dict):return value,None
    if not rationale or any(not isinstance(k,str) or not isinstance(v,str) or not v.strip() for k,v in rationale.items()):raise ValueError('rationale object must be nonempty with only nonempty string values')
    encoded=json.dumps(rationale,ensure_ascii=False,separators=(',',':'),allow_nan=False)
    if _strict_generation_json(encoded,'normalized rationale')!=rationale:raise ValueError('rationale JSON representation is not reversible')
    value['rationale']=encoded
    audit=dict(schema='v12-plan-rationale-json-normalization-1',source_packet_sha256=sha_value(packet),normalized_packet_sha256=sha_value(value),field_changes=dict(rationale=dict(before=deepcopy(rationale),after=encoded)),transformation='COMPLETE_FLAT_STRING_MAP_TO_REVERSIBLE_JSON_TEXT',all_keys_and_values_preserved=True,other_packet_fields_unchanged=True,missing_fields_added=False,reasoning_used_as_source=False,business_semantics_changed=False)
    return value,audit

def save_recovered_response(flow,rid,response):
    """Only attach an offline runtime readback to its original stage journal."""
    matches=[]
    for path in (flow.root/'turns').glob('*/request.json'):
        if read_json(path).get('request_id')==rid:matches.append(path.parent)
    if not matches:
        journal=flow.root/'turns/plan/read-timeout-replacement.json'
        if journal.is_file() and read_json(journal).get('replacement_http_request_id')==rid:matches=[journal.parent]
    if len(matches)!=1:raise ValueError('recovered HTTP request has no unique original stage')
    save_json(matches[0]/'response.json',response,immutable=True)

def bootstrap(request_id,project_id,baseline_version_id,raw_request):
    """Bind immutable input and an actually verified existing version, no HTTP."""
    from .case_requests import create_request,load_request
    # Bootstrap is read-only until the input/baseline identity has been checked.
    project_root=PROJECT_ROOT;db_path=PROJECT_DB;case=None
    original=read_json(project_root/'run.json') if (project_root/'run.json').exists() else {}
    if project_id!=original.get('project_id'):
        from .case_projects import resolve_case,verify_case
        case=verify_case(resolve_case(project_id));project_root=Path(case['project_root']);db_path=Path(case['db_path']);original=read_json(project_root/'run.json')
    else:
        marker=project_root/'case.json'
        if marker.exists():
            from .case_projects import resolve_case,verify_case
            case=verify_case(resolve_case(read_json(marker)['case_id']))
    legacy=service(db_path,original['session_id'],read_only=True,project_root=project_root)
    snapshot=legacy._artifact_version(baseline_version_id)
    from .inherited.artifact_baseline import reference
    ref=reference(snapshot)
    if baseline_version_id!='v000000' and (snapshot.get('evidence_status')!='COMPLETE' or snapshot.get('chain_status')!='PASS'):raise ValueError('baseline requires complete verified source/tool evidence')
    kwargs=dict(context_root=project_root/'requests'/request_id,case_id=case['case_id']) if case and case['kind']!='LEGACY_READ_ONLY_CATALOG' else {}
    ctx=create_request(request_id,project_id,baseline_version_id,raw_request,baseline_ref=ref,baseline_snapshot=snapshot,db_path=str(db_path),project_root=str(project_root),**kwargs)
    if ctx.get('sources'):return ctx
    # Reuse Store and its session/project provenance. Source identities are new;
    # historical requirements are evidence, not new user instructions.
    store=module('artifact_service').ArtifactStore(db_path)
    store.ensure_session(ctx['session_id']);store.open_project(ctx['session_id'],project_id)
    texts=[raw_request,bootstrap_source(snapshot,ref)]
    sources=[]
    for text in texts:
        with store.reading() as db:rows=db.execute('SELECT source_id,text FROM sources WHERE session_id=? AND project_id=? AND text=?',(ctx['session_id'],project_id,text)).fetchall()
        if len(rows)>1:raise ValueError('ambiguous partial initialization; inspect sources')
        row=dict(rows[0]) if rows else store.record_source(ctx['session_id'],text)
        sources.append(dict(id=row['source_id'],text=row['text']))
    from .case_requests import save_sources
    save_sources(request_id,sources)
    if module('artifact_baseline').selection(type('Selection',(),dict(store=store,session_id=ctx['session_id'],_current_project=lambda self:store.current_project(ctx['session_id'])))())!=ref:
        store.select_artifact_baseline(ctx['session_id'],project_id,ref)
    return load_request(request_id)

def bootstrap_review(request_id,origin_request_id,version_id,raw_request,context_root,step_id):
    """Independent metadata only; the origin Store and all models stay readonly."""
    from .case_requests import load_request,create_request
    origin=load_request(origin_request_id)
    svc=service(origin['db_path'],origin['session_id'],read_only=True,task_context=origin)
    snapshot=svc._artifact_version(version_id);ref=module('artifact_baseline').reference(snapshot)
    if snapshot.get('record_kind')!='ARTIFACT_EXECUTION_SNAPSHOT' or snapshot.get('evidence_status')!='COMPLETE' or snapshot.get('state') not in ('succeeded','failed') or not snapshot.get('recorded_conclusions_verified'):raise ValueError('review requires an actually complete terminal source/manifest-bound execution')
    task=svc.store.task(snapshot['task_id'])
    if snapshot['project_id']!=origin['project_id'] or task['session_id']!=origin['session_id']:raise ValueError('review execution does not belong to the bound original request')
    binding=dict(origin_request_id=origin_request_id,origin_session_id=origin['session_id'],origin_request_digest=origin['request_digest'],reference=ref,original_partial_path=str(Path(origin['root'])/'pipeline-partial.json'),original_unknown_request_id='case_explain_0_'+origin_request_id,original_http_root=str(Path(origin['root'])/'http'))
    facts=dict(kind='EXISTING_EXECUTION_EVIDENCE_NOT_NEW_USER_REQUIREMENTS',reference=ref,parameters=modelica_parameters(snapshot['frozen_spec']['artifact']['modelica_source'])[0],task_status=snapshot['task_status'],scope='INHERITED_SIMPLIFIED_MODEL_NO_HARDWARE_CERTIFICATION')
    sources=[dict(id='input_'+sha_value(request_id)[:24],text=raw_request),dict(id='evidence_'+ref['spec_digest'][:24],text=json.dumps(facts,ensure_ascii=False))]
    return create_request(request_id,ref['project_id'],version_id,raw_request,baseline_ref=ref,baseline_snapshot=snapshot,sources=sources,db_path=origin['db_path'],project_root=origin['project_root'],context_root=context_root,review_binding=binding,step_id=step_id)

class TaskFlow:
    def __init__(self,ctx,*,runtime_factory=None,rpc=None):
        self.ctx=ctx;self.root=Path(ctx['root']);self.runtime_factory=runtime_factory;self.rpc=rpc
        self._verified_snapshots={}
    @classmethod
    def load(cls,rid,**kwargs):
        from .case_requests import load_request
        return cls(load_request(rid),**kwargs)
    def refresh(self):
        from .case_requests import load_request
        self.ctx=load_request(self.ctx['request_id']);return self.ctx
    def http_state(self):
        """Read the existing ledger without creating or mutating a budget."""
        ref=self.ctx.get('runtime_authority')
        if not ref or not Path(ref['ledger_path']).exists():return None
        with closing(sqlite3.connect(Path(ref['ledger_path']).resolve().as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row
            rows=[dict(row) for row in db.execute("SELECT request_id,category,status,reserve_tokens,reserve_output,reserve_cost,actual_tokens,actual_output,actual_cost,reason,launched_at,finished_at FROM requests WHERE status<>'not_dispatched'")]
            frozen=db.execute("SELECT value FROM meta WHERE key='frozen'").fetchone()
        unknown=[row for row in rows if row['status'] in ('unknown','reserved','dispatched')]
        return dict(ledger_path=ref['ledger_path'],frozen=frozen[0] if frozen else None,unknown_requests=unknown,actual_cost_cny='UNKNOWN' if unknown else sum(row['actual_cost'] or 0 for row in rows),cost_cny_reserved_or_actual=sum(row['actual_cost'] if row['actual_cost'] is not None else row['reserve_cost'] for row in rows),known_actual_cost_cny=sum(row['actual_cost'] or 0 for row in rows))
    def open(self,*,read_only=False,with_worker=False):
        self.refresh()
        review=self.ctx.get('access_mode')=='RESULT_REVIEW_ONLY'
        if review and (not read_only or with_worker):raise PermissionError('result-review/plan-only context cannot open a writable or worker service')
        if not read_only:
            from .case_requests import require_execution
            self.ctx=require_execution(self.ctx['request_id'])
        sid=self.ctx['review_binding']['origin_session_id'] if review else self.ctx['session_id']
        svc=service(self.ctx['db_path'],sid,read_only=read_only,task_context=self.ctx)
        if svc._current_project()['project_id']!=self.ctx['project_id']:raise ValueError('request project selection changed')
        if with_worker:
            from .case_worker import CaseWorkerManager
            svc.worker_manager=CaseWorkerManager(svc.store,svc.context)
        return svc
    def _runtime(self):
        if self.runtime_factory:return self.runtime_factory(self.ctx)
        from .case_runtime import CaseRuntime
        authority=self.ctx.get('runtime_authority') or {}
        db=authority.get('ledger_path',str(self.root/'budget.sqlite'))
        return CaseRuntime(self.root/'runtime.json',db,reopen=True,task_context=self.ctx)
    def effective_stage_request(self,location):
        """Read the append-only request projection without hiding its original."""
        location=Path(location);original=read_json(location/'request.json')
        projected=location/'request-projection.json';receipt=location/'request-projection-receipt.json'
        if not projected.exists() and not receipt.exists():return original
        if not projected.exists() or not receipt.exists():raise ValueError('incomplete stage projection journal; inspect evidence before retry')
        value=read_json(projected);audit=read_json(receipt)
        expected=dict(original,messages=project_messages(self.ctx,original['messages']))
        if value!=expected or audit!=self._projection_receipt(original,value):raise ValueError('stage projection journal/request/baseline/authority binding drift')
        return value
    def _projection_receipt(self,original,projected):
        from .case_runtime import _estimate_input
        context=json.loads(original['messages'][1]['content']);confirmed='confirmed_plan' in context
        return dict(schema='v12-unsent-stage-context-projection-1',request_id=original['request_id'],request_digest=self.ctx['request_digest'],
                    baseline_ref=self.ctx['baseline_ref'],baseline_snapshot_sha256=sha_value(self.ctx['baseline_snapshot']),
                    sources_sha256=sha_value(self.ctx['sources']),runtime_authority=deepcopy(self.ctx['runtime_authority']),
                    displayed_digest=self.ctx.get('displayed_digest') if confirmed else None,confirmation_sha256=sha_file(self.ctx['confirmation_path']) if confirmed and self.ctx.get('confirmation_path') else None,
                    original_stage_request_sha256=sha_value(original),projected_stage_request_sha256=sha_value(projected),
                    original_input_estimate=_estimate_input(original['messages'],original['tools']),projected_input_estimate=_estimate_input(projected['messages'],projected['tools']),
                    ledger_row_absent_at_projection=True,http_artifacts_absent_at_projection=True,original_retained=True,
                    same_http_request_id=True,budget_reissued=False,product_http_added=0,
                    transformation='STRUCTURED_CURRENT_MODEL_AND_VERIFIED_HISTORICAL_PROVENANCE_NO_TEXT_TRUNCATION')
    def _append_preflight_projection(self,location,original,request):
        """Only a provably unsent over-window input may retain its HTTP identity."""
        from .case_requests import authority_for
        from .case_runtime import _estimate_input
        if (location/'request-projection.json').exists() or (location/'request-projection-receipt.json').exists():
            if self.effective_stage_request(location)!=request:raise ValueError('stage request changed; do not reuse HTTP identity')
            return
        authority=authority_for(self.ctx);now=datetime.now(timezone.utc)
        if not datetime.fromisoformat(authority['started_at_utc'].replace('Z','+00:00'))<=now<datetime.fromisoformat(authority['hard_deadline_utc'].replace('Z','+00:00')):raise PermissionError('preflight projection requires active original budget interval')
        expected=dict(original,messages=project_messages(self.ctx,original.get('messages',[])))
        if original.get('request_id')!=request['request_id'] or original.get('request_digest')!=self.ctx['request_digest'] or expected!=request or original.get('tools')!=request['tools']:raise ValueError('stage request changed outside structured projection; do not reuse HTTP identity')
        context=json.loads(original['messages'][1]['content'])
        if context.get('raw_request')!=self.ctx['raw_request'] or context.get('baseline_snapshot')!=self.ctx['baseline_snapshot'] or ('trusted_sources' in context and context['trusted_sources']!=self.ctx['sources']):raise ValueError('unsent original request/source/baseline binding differs')
        if 'confirmed_plan' in context and context['confirmed_plan']!=self.ctx.get('plan'):raise ValueError('unsent original confirmed plan differs')
        if _estimate_input(original['messages'],original['tools'])<1000000:raise ValueError('only the identified shared-window local preflight rejection may be projected')
        if _estimate_input(request['messages'],request['tools'])>=1000000:raise ValueError('conservative full-input estimate reaches the nominal shared 1M window; no HTTP, no truncation')
        if set(p.name for p in location.iterdir())!={'request.json'} or (self.root/'http'/sha_value(request['request_id'])).exists():raise ValueError('stage has response/protocol/HTTP evidence; automatic projection resend forbidden')
        ledger=Path(self.ctx['runtime_authority']['ledger_path'])
        with closing(sqlite3.connect(ledger.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            row=db.execute('SELECT 1 FROM requests WHERE request_id=?',(request['request_id'],)).fetchone()
            uncertain=db.execute("SELECT 1 FROM requests WHERE status IN ('unknown','reserved','dispatched') LIMIT 1").fetchone()
            meta=dict(db.execute('SELECT key,value FROM meta'))
        if row or uncertain or meta.get('frozen') or meta.get('authorization_sha256')!=self.ctx['runtime_authority']['sha256']:raise ValueError('HTTP ledger has a row, uncertainty, freeze or different authority; automatic projection resend forbidden')
        save_json(location/'request-projection.json',request,immutable=True)
        save_json(location/'request-projection-receipt.json',self._projection_receipt(original,request),immutable=True)
    async def call(self,stage,messages,tools):
        from .case_requests import require_planning,require_execution
        self.refresh()
        (require_execution if stage.startswith('generate') else require_planning)(self.ctx['request_id'])
        rid='case_'+stage+'_'+self.ctx['request_id']
        location=self.root/'turns'/stage
        connection=location/'connect-replacement.json'
        timeout_replacement=location/'read-timeout-replacement.json'
        if timeout_replacement.exists():
            if stage!='plan':raise PermissionError('read-timeout replacement is limited to initial planning')
            journal=read_json(timeout_replacement)
            request=self.effective_stage_request(location);messages,tools=request['messages'],request['tools']
            rid=journal['replacement_http_request_id']
            from .case_requests import authority_for
            from .case_plan_recovery import registered
            with closing(sqlite3.connect(Path(self.ctx['runtime_authority']['ledger_path']).resolve().as_uri()+'?mode=ro',uri=True)) as db:
                db.row_factory=sqlite3.Row
                proof=registered(db,self.ctx,authority_for(self.ctx),rid)
            if not proof or not (location/'response.json').exists():raise PermissionError('explicit replacement lacks a complete saved successful response; no automatic dispatch')
        elif connection.exists():
            if stage!='plan':raise PermissionError('connection replacement is limited to the initial plan')
            # A rebuilt context can contain newer failure facts. The replacement
            # must instead reuse the exact original saved stage input.
            binding=self._plan_connect_binding()
            request=self.effective_stage_request(location)
            messages,tools=request['messages'],request['tools']
        else:
            messages=project_messages(self.ctx,messages)
            request=dict(request_id=rid,messages=messages,tools=tools,request_digest=self.ctx['request_digest'])
        existing=location/'request.json'
        if existing.exists():
            if self.effective_stage_request(location)!=request:self._append_preflight_projection(location,read_json(existing),request)
        else:save_json(existing,request,immutable=True)
        if (location/'response.json').exists():
            if connection.exists():self._plan_connect_binding(require_success=True)
            emit_progress('response_saved','正在读取已保存的完整产品回复。',request_id=self.ctx['request_id'],http_request_id=rid)
            return read_json(location/'response.json')
        rt=self._runtime()
        if connection.exists():
            rid=binding['new_request_id']
            row=rt.budget.request(rid)
            if (not row or row['status']!='succeeded') and getattr(self,'_explicit_plan_connect_dispatch',None)!=rid:
                raise PermissionError('connection replacement requires the explicit approved recovery action; no automatic dispatch')
        diagnostic=location/'http400-diagnostic-replacement.json'
        correction=location/'http400-empty-object-correction.json'
        if correction.exists():
            journal=read_json(correction);proof_path=Path(journal['proof_path']);proof=read_json(proof_path)
            if journal.get('request_digest')!=self.ctx['request_digest'] or journal.get('original_request_id')!=rid or journal.get('proof_sha256')!=sha_file(proof_path) or proof.get('authority_sha256')!=self.ctx['runtime_authority']['sha256'] or proof.get('correction_request_id')!=journal.get('correction_request_id') or sha_value(rt.build_payload(messages,tools)[0])!=proof.get('new_payload_sha256'):raise ValueError('known schema correction journal/payload differs')
            rid=journal['correction_request_id'];diagnostic=correction
        elif diagnostic.exists():
            journal=read_json(diagnostic);proof_path=Path(journal['proof_path']);proof=read_json(proof_path)
            if journal.get('schema')!='v12-stage-http400-diagnostic-replacement-1' or journal.get('original_request_id')!=rid or journal.get('request_digest')!=self.ctx['request_digest'] or journal.get('original_stage_request_sha256')!=sha_file(existing) or journal.get('proof_sha256')!=sha_file(proof_path) or proof.get('diagnostic_request_id')!=journal.get('diagnostic_request_id') or proof.get('authority_sha256')!=self.ctx['runtime_authority']['sha256']:raise ValueError('HTTP400 replacement journal binding differs')
            actual=journal['diagnostic_request_id'];row=rt.budget.request(actual)
            # A separately authorized diagnostic may only be read back here.
            # This ordinary plan entry can never launch the diagnostic lane.
            if not row or row['status']!='succeeded' or not (self.root/'http'/sha_value(actual)/'protocol-response.json').is_file():raise PermissionError('HTTP400 diagnostic has no complete succeeded response; readback only, no dispatch')
            rid=actual
        rt.require_credentials()
        try:response=await asyncio.to_thread(rt.complete,messages,tools,rid)
        except ValueError as exc:
            # Runtime can reject a complete, charged reply before returning its
            # normal view (e.g. tool arguments are malformed JSON). Only this
            # explicit terminal local receipt permits a new bounded attempt.
            rejected=self.root/'http'/sha_value(rid)/'rejected-response.json'
            row=rt.budget.request(rid)
            from .case_runtime import _MODELS
            strict_output=transport_profile(rt.config)['strict_tools']
            if stage.startswith(('plan','generate')) and rejected.exists() and row and row['status']=='rejected' and (strict_output or isinstance(exc,json.JSONDecodeError) or str(exc)=='tool arguments must decode once to an object'):
                known=read_json(rejected);raw=known.get('response',{});choices=raw.get('choices',[])
                from .case_stream import validate_usage
                try:validate_usage(raw.get('usage'))
                except ValueError:raw={};choices=[]
                charged=all(type(row.get(key)) in (int,float) and __import__('math').isfinite(row[key]) and row[key]>=0 for key in ('actual_tokens','actual_output','actual_cost'))
                if charged and known.get('usage_known') is True and raw.get('model') in _MODELS and isinstance(raw.get('id'),str) and len(choices)==1 and choices[0].get('finish_reason') in ('stop','tool_calls'):
                    message=choices[0].get('message',{});view=dict(tool_calls=message.get('tool_calls',[]),content=message.get('content',''),finish_reason=choices[0]['finish_reason'],raw_rejected_protocol=raw,request_tool_output_profile=transport_profile(rt.config)['profile'])
                    base='generate' if stage.startswith('generate') else 'plan';attempt=int(stage.rsplit('_',1)[-1]) if '_repair_' in stage else 0
                    self.failure(base,attempt,exc,view)
            raise
        emit_progress('save_response','产品通信已结束，正在保存完整回复。',request_id=self.ctx['request_id'],http_request_id=rid)
        save_json(location/'response.json',response,immutable=True)
        if connection.exists():save_json(location/'response-connect-binding.json',dict(original_request_id=request['request_id'],actual_request_id=rid,journal_sha256=sha_file(connection),response_sha256=sha_value(response),original_unknown_preserved=True),immutable=True)
        if diagnostic.exists():save_json(location/'response-http400-binding.json',dict(original_request_id=request['request_id'],actual_request_id=rid,diagnostic_journal_sha256=sha_file(diagnostic),response_sha256=sha_value(response),HTTP_added_by_stage_readback=0),immutable=True)
        emit_progress('response_saved','完整产品回复已保存，正在进行本地处理。',request_id=self.ctx['request_id'],http_request_id=rid)
        return response

    def plan_recovery_info(self):
        from .case_plan_recovery import inspect
        info=inspect(self)
        return {key:info[key] for key in ('approval_template','used','ledger_path','product_http_added')}

    async def recover_plan_read_timeout(self,approval_path):
        """Exactly one explicit HTTP; never bounded repairs, generation or execution."""
        from .case_plan_recovery import approved
        info,proof=approved(self,approval_path)
        rt=self._runtime();rt.budget.authorize_plan_read_timeout(proof)
        journal=dict(schema='v12-stage-plan-read-timeout-replacement-1',request_id=self.ctx['request_id'],request_digest=self.ctx['request_digest'],
            original_http_request_id=proof['original_http_request_id'],replacement_http_request_id=proof['replacement_http_request_id'],
            proof_sha256=sha_value(proof),approval_path=proof['approval_path'],approval_sha256=proof['approval_sha256'])
        save_json(Path(info['stage'])/'read-timeout-replacement.json',journal,immutable=True)
        response=await asyncio.to_thread(rt.complete,info['request']['messages'],info['request']['tools'],proof['replacement_http_request_id'])
        save_recovered_response(self,proof['replacement_http_request_id'],response)
        result=self._accept_plan_response(response)
        result.update(product_http_added=1,original_unknown_preserved=True,requires_user_plan_confirmation=True,generation_compile_simulation_started=False)
        return result

    async def recover_response(self):
        """Recover only complete saved protocol replies; zero network operations."""
        self.refresh();stages={}
        for path in (self.root/'turns').glob('*/request.json'):
            saved=self.effective_stage_request(path.parent)
            if saved.get('request_digest')!=self.ctx['request_digest']:raise ValueError('saved response stage belongs to a different business request')
            stages[saved['request_id']]=(path.parent,saved)
        journal=self.root/'turns/plan/read-timeout-replacement.json';replaced=set()
        if journal.is_file():
            value=read_json(journal);original=value['original_http_request_id'];stages[value['replacement_http_request_id']]=stages[original];replaced.add(original)
        with closing(sqlite3.connect(Path(self.ctx['runtime_authority']['ledger_path']).resolve().as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row;rows=[dict(r) for r in db.execute('SELECT * FROM requests ORDER BY reserved_at DESC')]
        history=read_json(self.root/'dialogue.json') if (self.root/'dialogue.json').is_file() else []
        def answer_stage(location):return location.name.startswith(('ask_','explain_','reopen_','result_followup_'))
        def missing_answer(location):
            if not answer_stage(location):return False
            if self.ctx.get('access_mode')=='RESULT_REVIEW_ONLY':return not (self.root/'review-answer.json').exists()
            return int(location.name.rsplit('_',1)[-1])>=len(history)
        eligible=[row for row in rows if row['request_id'] in stages and row['request_id'] not in replaced and (row['status'] in ('unknown','reserved','dispatched') or not (stages[row['request_id']][0]/'response.json').is_file() or missing_answer(stages[row['request_id']][0]))]
        if not eligible:
            # Idempotent readback of an answer already inserted into dialogue.
            eligible=[row for row in rows if row['request_id'] in stages and answer_stage(stages[row['request_id']][0]) and row['status']=='succeeded'][:1]
        if not eligible:raise RuntimeError('no unattached complete response for this request; zero HTTP, saved stages unchanged')
        row=eligible[0];rid=row['request_id'];location,saved=stages[rid]
        if not (self.root/'http'/sha_value(rid)/'protocol-response.json').is_file():raise RuntimeError('latest interrupted stage has no complete saved request/response; remain UNKNOWN, zero HTTP; partial fragments cannot be recovered')
        if location.name!='plan' and not location.name.startswith(('ask_','explain_','reopen_','result_followup_')):raise PermissionError('automatic response recovery supports initial plan and read-only answers only; use an explicit stage-specific recovery for generated source')
        rt=self._runtime();wire=read_json(self.root/'http'/sha_value(rid)/'request.json')
        if rt.build_payload(saved['messages'],saved['tools'])[0]!=wire.get('payload') or wire.get('internal_tools') not in (None,saved['tools']):raise ValueError('saved stage and original complete HTTP request differ; no recovery attachment')
        recovery_context=self._readonly_recovery_context(saved) if location.name!='plan' else None
        response=rt.recover_saved_response(rid);save_recovered_response(self,rid,response)
        if location.name=='plan':result=self._accept_plan_response(response)
        else:
            if saved.get('tools') or response.get('tool_calls') or not response.get('content','').strip():raise ValueError('saved read-only reply must be plain nonempty text')
            result=self._save_recovered_answer(location,saved,response,rid,recovery_context)
        result.update(product_http_added=0,generation_compile_simulation_started=False)
        return result

    def _readonly_recovery_context(self,saved):
        """Validate the actual old question and business state before attachment."""
        messages=saved.get('messages',[])
        if len(messages)<3 or messages[-1].get('role')!='user' or not messages[-1].get('content','').strip():raise ValueError('original read-only question is absent')
        context=json.loads(messages[1]['content']);review=self.ctx.get('access_mode')=='RESULT_REVIEW_ONLY'
        before=context.get('new_review_state' if review else 'binding');current=self.state(verify_current=True)
        if not isinstance(before,dict) or before.get('request_id')!=self.ctx['request_id'] or before.get('project_id')!=self.ctx['project_id']:raise ValueError('saved answer request/project binding differs')
        strip=lambda state:{k:v for k,v in state.items() if k!='http_state'}
        if strip(before)!=strip(current):raise ValueError('saved answer model/task state binding differs; no dialogue recovery')
        if not review and (context.get('raw_request')!=self.ctx['raw_request'] or not projected_context_matches(self.ctx,context)):raise ValueError('saved answer original requirement/baseline/source binding differs')
        return dict(before=before,current=current,question=messages[-1]['content'],review=review)

    def _save_recovered_answer(self,location,saved,response,rid,context):
        protected=self._protect_explanation(response['content'],context['current'])
        result=dict(**protected,binding=context['before'],recovered_stage=location.name,recovered_http_request_id=rid,read_only_status_unchanged=True)
        if context['review']:
            path=self.root/'review-answer.json'
            if not path.exists():save_json(path,result,immutable=True)
            return result
        index=int(location.name.rsplit('_',1)[-1]);path=self.root/'dialogue.json';history=read_json(path) if path.exists() else []
        if index>len(history):raise ValueError('earlier dialogue turns are absent; no guessed recovery ordering')
        if index<len(history):
            old=history[index];strip=lambda state:{k:v for k,v in state.items() if k!='http_state'}
            if old.get('question')!=context['question'] or old.get('assistant_message')!=response.get('assistant_message') or strip(old.get('binding',{}))!=strip(context['before']) or old.get('recovered_http_request_id',rid)!=rid:raise ValueError('existing dialogue turn differs from the recovered original stage')
            result['reused']=True
        else:
            history.append(dict(question=context['question'],assistant_message=response['assistant_message'],binding=context['before'],reopen=location.name.startswith('reopen_'),**protected,recovered_http_request_id=rid,recovered_stage=location.name,stage_request_sha256=sha_value(saved)))
            save_json(path,history)
        return result

    def _plan_connect_evidence(self,approval_path,*,initial=True):
        """Read-only validation; never construct CaseBudget, unfreeze or send."""
        from .case_requests import authority_for
        from .case_runtime import CaseRuntime
        from types import SimpleNamespace
        self.refresh();ctx=self.ctx;authority=authority_for(ctx)
        now=datetime.now(timezone.utc)
        if not datetime.fromisoformat(authority['started_at_utc'].replace('Z','+00:00'))<=now<datetime.fromisoformat(authority['hard_deadline_utc'].replace('Z','+00:00')):raise PermissionError('original connection-recovery budget is expired or inactive')
        if authority.get('operation_policy') or ctx.get('access_mode')=='RESULT_REVIEW_ONLY':raise PermissionError('only ordinary initial planning supports this explicit connection exception')
        if initial and (ctx.get('stage')!='DRAFT' or ctx.get('plan') or ctx.get('confirmation_path') or any((self.root/name).exists() for name in ('generation.json','proposal-unknown.json','pipeline.json','pipeline-partial.json'))):raise PermissionError('connection recovery requires an unconfirmed, ungenerated initial plan')
        path=Path(approval_path).resolve()
        if not path.is_relative_to((ROOT/'results').resolve()) or path.is_relative_to((ROOT/'V12_old').resolve()):raise PermissionError('connection exception must be a current trusted result approval file')
        approval=read_json(path);original='case_plan_'+ctx['request_id'];replacement=original+'_connect_retry_1'
        stage=self.root/'turns/plan';stage_request=self.effective_stage_request(stage)
        if stage_request.get('request_id')!=original or stage_request.get('request_digest')!=ctx['request_digest']:raise ValueError('original saved plan request binding differs')
        folder=self.root/'http'/sha_value(original);wire=read_json(folder/'request.json')
        pure=SimpleNamespace(config=authority['model_controls'],authorization=authority,task_context=ctx)
        payload=CaseRuntime.build_payload(pure,stage_request['messages'],stage_request['tools'])[0]
        if wire.get('request_id')!=original or wire.get('authorization_sha256')!=ctx['runtime_authority']['sha256'] or wire.get('payload')!=payload or wire.get('payload_sha256')!=sha_value(payload) or wire.get('internal_tools') not in (None,stage_request['tools']):raise ValueError('original saved messages/tools and complete outbound payload differ')
        if any(wire.get('transport_source_sha256',{}).get(name)!=sha_file(REPO/name) for name in ('week13/src/case_http.py','week13/src/case_stream.py')):raise ValueError('original HTTP dependency source drift')
        expected=dict(schema='v12-plan-connect-replacement-approval-1',status='APPROVED',approval_channel='USER_CHAT_APPROVAL',
            request_id=ctx['request_id'],request_digest=ctx['request_digest'],project_id=ctx['project_id'],
            runtime_authority=ctx['runtime_authority'],original_http_request_id=original,replacement_http_request_id=replacement,
            payload_sha256=sha_value(payload),original_stage_request_sha256=sha_file(stage/'request.json'),
            original_http_request_file_sha256=sha_file(folder/'request.json'),
            original_error_file_sha256=sha_file(folder/'transport-error.json'),original_timing_sha256=sha_file(folder/'timing.json'),
            original_process_exit_sha256=sha_file(folder/'process-exit.json'),caps=authority['caps'],
            hard_deadline_utc=authority['hard_deadline_utc'],maximum_new_http_requests=1,
            original_unknown_and_full_reservation_preserved=True,test_only=authority.get('test_only',False))
        if any(approval.get(k)!=v for k,v in expected.items()) or not isinstance(approval.get('approval_quote'),str) or not approval['approval_quote'].strip():raise PermissionError('explicit user connection exception is absent, unapproved or bound to different scope/payload/budget')
        error=read_json(folder/'transport-error.json');timing=read_json(folder/'timing.json');exit=read_json(folder/'process-exit.json')
        if error.get('error_type') not in ('ConnectError','ConnectTimeout'):raise PermissionError('read/stream interruption cannot use connection recovery')
        if timing.get('schema')!='v12-http-stream-timing-1' or timing.get('request_id')!=original or timing.get('error_type')!=error['error_type'] or any(type(timing.get(k)) is not int or timing[k]!=0 for k in ('receive_chunk_count','received_bytes','event_count','content_bytes','reasoning_bytes','tool_argument_bytes')) or timing.get('usage_known') is not False or timing.get('first_receive_at_utc') is not None or timing.get('finish_reason') is not None:raise PermissionError('zero-response connection-stage timing proof required')
        fixture=authority.get('test_only') is True and exit.get('fixture_transport') is True and exit.get('fixture_call_returned') is True and exit.get('process_launched') is False
        real=exit.get('local_process_terminated') is True and type(exit.get('returncode')) is int and exit.get('process_launched') is True and exit.get('fixture_transport') is False
        if exit.get('request_id')!=original or not (fixture or real) or real and exit.get('child_error_type')!=error['error_type']:raise PermissionError('original local HTTP process exit is unproven')
        if any((folder/name).exists() for name in ('protocol-response.json','wire-evidence.json','rejected-response.json','readback-recovery.json')):raise PermissionError('response evidence forbids connection-only replacement')
        progress=read_json(folder/'progress.json') if (folder/'progress.json').exists() else {}
        if 'http_status' in progress or any(progress.get(k,0) for k in ('event_count','content_bytes','reasoning_bytes','tool_argument_bytes')):raise PermissionError('response progress forbids connection-only replacement')
        ledger=Path(ctx['runtime_authority']['ledger_path'])
        with closing(sqlite3.connect(ledger.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row
            old=db.execute('SELECT * FROM requests WHERE request_id=?',(original,)).fetchone()
            meta=dict(db.execute('SELECT key,value FROM meta'))
            unknowns={r[0] for r in db.execute("SELECT request_id FROM requests WHERE status IN ('unknown','reserved','dispatched')")}
            retry=db.execute('SELECT * FROM connect_retries WHERE old_request_id=?',(original,)).fetchone()
        if not old or old['status']!='unknown' or not old['launched_at'] or old['content_hash']!=sha_value(payload) or any(old[k] is not None for k in ('actual_tokens','actual_output','actual_cost')) or sha_value(dict(old))!=approval.get('original_request_row_sha256'):raise PermissionError('original UNKNOWN row or full reservation binding differs')
        if meta.get('authorization_sha256')!=ctx['runtime_authority']['sha256'] or any('closed' in k.lower() or 'retired' in k.lower() for k in meta):raise PermissionError('original ledger is closed, retired or bound elsewhere')
        if initial and (unknowns!={original} or retry or meta.get('connect_retry_required') or meta.get('frozen')!='unknown product HTTP; usage reservation retained' or (stage/'connect-replacement.json').exists()):raise PermissionError('only one original UNKNOWN permits one explicit initial connection replacement')
        return dict(approval=approval,approval_path=str(path),original=original,replacement=replacement,stage=stage,
                    request=stage_request,payload=payload,wire=wire,old=dict(old),meta=meta,unknowns=unknowns,
                    retry=dict(retry) if retry else None)

    def _plan_connect_binding(self,*,require_success=False):
        """Validate an already registered single connection exception read-only."""
        stage=self.root/'turns/plan';journal=read_json(stage/'connect-replacement.json')
        evidence=self._plan_connect_evidence(journal['approval_path'],initial=False)
        proof_path=self.root/'http'/sha_value(evidence['original'])/'explicit-connect-retry.json'
        proof=read_json(proof_path);retry=evidence['retry'];authority_sha=self.ctx['runtime_authority']['sha256']
        expected=dict(schema='v12-stage-plan-connect-replacement-1',request_id=self.ctx['request_id'],request_digest=self.ctx['request_digest'],
            original_request_id=evidence['original'],new_request_id=evidence['replacement'],
            original_stage_request_sha256=sha_file(stage/'request.json'),effective_stage_request_sha256=sha_value(evidence['request']),
            payload_sha256=sha_value(evidence['payload']),authority_sha256=authority_sha,
            approval_path=evidence['approval_path'],approval_sha256=sha_file(evidence['approval_path']),
            proof_path=str(proof_path),proof_sha256=sha_file(proof_path))
        if journal!=expected or not retry or json.loads(retry['proof_json'])!=proof or retry['new_request_id']!=evidence['replacement'] or retry['content_hash']!=sha_value(evidence['payload']) or proof.get('original_request_row_sha256')!=sha_value(evidence['old']) or proof.get('authorization_sha256')!=authority_sha or proof.get('payload_sha256')!=sha_value(evidence['payload']) or proof.get('old_request_id')!=evidence['original'] or proof.get('new_request_id')!=evidence['replacement']:raise PermissionError('connection stage/proof/ledger binding drift')
        if require_success:
            with closing(sqlite3.connect(Path(self.ctx['runtime_authority']['ledger_path']).resolve().as_uri()+'?mode=ro',uri=True)) as db:
                db.row_factory=sqlite3.Row;row=db.execute('SELECT * FROM requests WHERE request_id=?',(evidence['replacement'],)).fetchone()
            if not row or row['status']!='succeeded' or row['content_hash']!=sha_value(evidence['payload']) or any(row[k] is None for k in ('actual_tokens','actual_output','actual_cost','launched_at','finished_at')) or any(type(row[k]) not in (int,float) or not __import__('math').isfinite(row[k]) or row[k]<0 for k in ('actual_tokens','actual_output','actual_cost')) or evidence['unknowns']!={evidence['original']} or evidence['meta'].get('frozen') or evidence['meta'].get('connect_retry_required'):raise PermissionError('explicit replacement is not completely successful or another uncertainty/freeze exists')
            actual_wire=read_json(self.root/'http'/sha_value(evidence['replacement'])/'request.json')
            if actual_wire.get('request_id')!=evidence['replacement'] or actual_wire.get('authorization_sha256')!=authority_sha or actual_wire.get('payload')!=evidence['payload'] or actual_wire.get('payload_sha256')!=sha_value(evidence['payload']):raise PermissionError('replacement outbound payload/authority differs')
            response=read_json(stage/'response.json')
            if json.loads(row['response'])!=response:raise PermissionError('successful connection replacement response differs')
        return expected

    async def recover_plan_connect(self,approval_path):
        """Explicit approved action only: register once, then one same-payload call."""
        evidence=self._plan_connect_evidence(approval_path)
        rt=self._runtime()
        rt.authorize_connect_retry(evidence['original'],evidence['replacement'],evidence['request']['messages'],evidence['request']['tools'])
        proof_path=self.root/'http'/sha_value(evidence['original'])/'explicit-connect-retry.json'
        journal=dict(schema='v12-stage-plan-connect-replacement-1',request_id=self.ctx['request_id'],request_digest=self.ctx['request_digest'],
            original_request_id=evidence['original'],new_request_id=evidence['replacement'],
            original_stage_request_sha256=sha_file(evidence['stage']/'request.json'),effective_stage_request_sha256=sha_value(evidence['request']),
            payload_sha256=sha_value(evidence['payload']),authority_sha256=self.ctx['runtime_authority']['sha256'],
            approval_path=evidence['approval_path'],approval_sha256=sha_file(evidence['approval_path']),proof_path=str(proof_path),proof_sha256=sha_file(proof_path))
        save_json(evidence['stage']/'connect-replacement.json',journal,immutable=True)
        self._explicit_plan_connect_dispatch=evidence['replacement']
        try:return await self.plan()
        finally:self._explicit_plan_connect_dispatch=None

    def bind_http_rejection_diagnostic(self, proof_path):
        """Explicit append-only stage journal; does not call a product/tool."""
        self.refresh();path=Path(proof_path).resolve();proof=read_json(path)
        original='case_plan_'+self.ctx['request_id'];stage=self.root/'turns/plan'
        expected=self.root/'http'/sha_value(original)/'http400-diagnostic-authorization.json'
        if path!=expected.resolve() or proof.get('schema')!='v12-http400-diagnostic-proof-1' or proof.get('request_context_id')!=self.ctx['request_id'] or proof.get('authority_sha256')!=self.ctx['runtime_authority']['sha256'] or proof.get('original_request_id')!=original or proof.get('original_stage_request_sha256')!=sha_file(stage/'request.json'):raise ValueError('diagnostic stage proof identity differs')
        ledger=Path(self.ctx['runtime_authority']['ledger_path'])
        with closing(sqlite3.connect(ledger.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            row=db.execute('SELECT proof_json FROM http_rejection_proofs WHERE original_request_id=?',(original,)).fetchone()
        if not row or json.loads(row[0])!=proof:raise ValueError('diagnostic proof not atomically registered')
        journal=dict(schema='v12-stage-http400-diagnostic-replacement-1',original_request_id=original,
            diagnostic_request_id=proof['diagnostic_request_id'],request_digest=self.ctx['request_digest'],
            original_stage_request_sha256=sha_file(stage/'request.json'),proof_path=str(path),proof_sha256=sha_file(path),
            purpose='DIAGNOSTIC_ONLY_THEN_ZERO_HTTP_READBACK_NO_AUTOMATIC_PLAN_CONFIRMATION_OR_TOOLS')
        save_json(stage/'http400-diagnostic-replacement.json',journal,immutable=True)
        return journal

    def bind_http_empty_object_correction(self,proof_path):
        self.refresh();path=Path(proof_path).resolve();proof=read_json(path)
        original='case_plan_'+self.ctx['request_id'];expected=self.root/'http'/sha_value(original)/'empty-object-correction-authorization.json'
        if path!=expected.resolve() or proof.get('request_digest')!=self.ctx['request_digest'] or proof.get('authority_sha256')!=self.ctx['runtime_authority']['sha256']:raise ValueError('correction stage proof differs')
        with closing(sqlite3.connect(Path(self.ctx['runtime_authority']['ledger_path']).resolve().as_uri()+'?mode=ro',uri=True)) as db:
            row=db.execute('SELECT proof_json FROM http_schema_correction_proofs WHERE correction_request_id=?',(proof['correction_request_id'],)).fetchone()
        if not row or json.loads(row[0])!=proof:raise ValueError('correction not atomically registered')
        journal=dict(schema='v12-stage-empty-object-correction-1',original_request_id=original,correction_request_id=proof['correction_request_id'],request_digest=self.ctx['request_digest'],proof_path=str(path),proof_sha256=sha_file(path))
        save_json(self.root/'turns/plan/http400-empty-object-correction.json',journal,immutable=True)
        return journal
    def failure(self,stage,attempt,exc,response):
        path=self.root/'failures'/(stage+'-'+str(attempt)+'.json')
        error=type(exc).__name__+': '+str(exc)
        if path.is_file():
            previous=read_json(path)
            if any(previous.get(key)!=value for key,value in dict(stage=stage,attempt=attempt,error=error,response=response).items()):raise ValueError('original protocol failure receipt differs')
            return previous  # Old records remain byte-for-byte unchanged.
        return save_json(path,dict(stage=stage,attempt=attempt,error=error,repairable=not isinstance(exc,UnsupportedRequestDesign),response=response,confirmed_plan=self.ctx.get('plan'),physical_scope_expansion_allowed=False,protocol_failure_fingerprint=failure_fingerprint(stage,exc,response)),immutable=True)
    def reject_plan(self,expected_displayed_digest,reason,*,attempt=0):
        """Trusted local review only; retain the product reply for bounded repair.

        This is deliberately not a product/MCP tool and creates no permission,
        HTTP, or repair allowance. feedback() remains the sole repair charge.
        """
        from .case_requests import require_planning,authority_for
        self.ctx=require_planning(self.ctx['request_id']);authority=authority_for(self.ctx)
        if type(attempt) is not int or attempt<0 or not isinstance(reason,str) or not reason.strip():raise ValueError('explicit plan attempt and nonempty trusted review reason required')
        if not isinstance(expected_displayed_digest,str) or len(expected_displayed_digest)!=64 or any(c not in '0123456789abcdef' for c in expected_displayed_digest):raise ValueError('expected displayed plan digest required')
        now=datetime.now(timezone.utc)
        if not datetime.fromisoformat(authority['started_at_utc'].replace('Z','+00:00'))<=now<datetime.fromisoformat(authority['hard_deadline_utc'].replace('Z','+00:00')):raise PermissionError('plan review requires active original budget interval')
        with closing(sqlite3.connect(Path(self.ctx['runtime_authority']['ledger_path']).resolve().as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row;meta=dict(db.execute('SELECT key,value FROM meta'))
            uncertain=db.execute("SELECT 1 FROM requests WHERE status IN ('unknown','reserved','dispatched') LIMIT 1").fetchone()
            rows=[dict(x) for x in db.execute('SELECT * FROM requests')]
            processes=[dict(x) for x in db.execute('SELECT * FROM process_attempts')]
        if uncertain or any(meta.get(k) for k in ('frozen','closed','retired','connect_retry_required')) or meta.get('authorization_sha256')!=self.ctx['runtime_authority']['sha256']:raise PermissionError('plan review cannot recover UNKNOWN or closed/frozen budget')
        if self.ctx.get('access_mode')=='RESULT_REVIEW_ONLY' or self.ctx.get('confirmation_path') or self.ctx.get('allow_execution') or self.ctx.get('stage') not in ('PROPOSED','PLAN_REJECTED'):raise PermissionError('only an unconfirmed, ungenerated, unexecuted plan may be reviewed back')
        if any((self.root/name).exists() for name in ('generation.json','generation-revalidated.json','proposal.json','execution-binding.json','pipeline.json','pipeline-partial.json')) or any((self.root/'turns').glob('generate*')) or any(p.name.startswith('execution-') for p in self.root.glob('*.json')):raise PermissionError('generated or executed request cannot revise its plan')
        if any(self.ctx['request_id'] in x['request_id'] and not x['request_id'].startswith(('case_plan_','case_plan_repair_')) for x in rows) or any(self.ctx['request_id'] in x['detail_json'] for x in processes):raise PermissionError('post-plan HTTP/process history prevents review rollback')
        review_path=self.root/'plan-reviews'/(expected_displayed_digest+'.json')
        previous=read_json(review_path) if review_path.exists() else None
        if previous and any(previous.get(k)!=v for k,v in dict(request_id=self.ctx['request_id'],request_digest=self.ctx['request_digest'],displayed_digest=expected_displayed_digest,reason=reason,attempt=attempt,channel='TRUSTED_LOCAL_REVIEW').items()):raise ValueError('immutable plan review identity/reason differs')
        if self.ctx.get('displayed_digest')!=expected_displayed_digest:
            if previous and self.ctx.get('stage')=='PLAN_REJECTED' and not self.ctx.get('displayed_digest') and self.ctx.get('last_plan_review_path')==str(review_path):
                failure=read_json(self.root/'failures'/('plan-'+str(attempt)+'.json'))
                if failure.get('review_rejection')!=dict(path=str(review_path),sha256=sha_file(review_path)):raise ValueError('plan rejection journal/failure differs')
                return previous
            raise ValueError('trusted plan review digest is stale')
        saved=read_json(self.ctx['proposal_path']);packet=saved['packet']
        if sha_value(saved)!=expected_displayed_digest or saved['request_digest']!=self.ctx['request_digest'] or saved['sources']!=self.ctx['sources'] or saved['baseline_ref']!=self.ctx['baseline_ref']:raise ValueError('displayed plan/request/source binding differs')
        stage='plan' if not attempt else 'plan_repair_'+str(attempt);location=self.root/'turns'/stage
        request=self.effective_stage_request(location);response=read_json(location/'response.json')
        rid='case_'+stage+'_'+self.ctx['request_id'];directory=self.root/'http'/sha_value(rid)
        wire=read_json(directory/'request.json');protocol=read_json(directory/'protocol-response.json');exit_receipt=read_json(directory/'process-exit.json');status=read_json(directory/'http-status.json')
        from .case_strict_tools import decode_calls
        from .case_runtime import CaseRuntime
        from types import SimpleNamespace
        selected=transport_profile(authority['model_controls'])['profile'];payload=wire['payload']
        expected_payload=CaseRuntime.build_payload(SimpleNamespace(config=authority['model_controls'],authorization=authority,task_context=self.ctx),request['messages'],request['tools'])[0]
        if request.get('request_id')!=rid or request.get('request_digest')!=self.ctx['request_digest'] or wire.get('request_id')!=rid or wire.get('authorization_sha256')!=self.ctx['runtime_authority']['sha256'] or wire.get('payload_sha256')!=sha_value(payload) or payload!=expected_payload:raise ValueError('complete planning wire/request/authority binding required')
        choices=protocol.get('choices',[])
        if len(choices)!=1 or choices[0].get('finish_reason') not in ('tool_calls','stop') or response.get('finish_reason')!=choices[0]['finish_reason'] or response.get('response_id')!=protocol.get('id') or response.get('response_model')!=protocol.get('model'):raise ValueError('known complete planning response required')
        decoded,_=decode_calls(choices[0]['message'].get('tool_calls',[]),request['tools'],profile=selected)
        if decoded!=response.get('tool_calls') or parse_tool(response,'case_request_plan','packet')!=packet:raise ValueError('original decoded product plan differs from displayed plan')
        fixture=wire.get('fixture_transport') is True
        terminal=(exit_receipt.get('fixture_call_returned') is True and exit_receipt.get('process_launched') is False) if fixture else (exit_receipt.get('process_launched') is True and exit_receipt.get('local_process_terminated') is True and exit_receipt.get('termination_requested') is False)
        if status.get('http_status')!=200 or exit_receipt.get('request_id')!=rid or exit_receipt.get('returncode')!=0 or not terminal or exit_receipt.get('timeout_classification') is not None or exit_receipt.get('child_error_type') is not None or exit_receipt.get('fixture_transport')!=wire.get('fixture_transport') or (directory/'rejected-response.json').exists():raise ValueError('known successful planning process exit required')
        row=next((r for r in rows if r['request_id']==rid),None);usage=protocol.get('usage',{})
        if not row or row['status']!='succeeded' or row['content_hash']!=sha_value(payload) or any(row[k] is None for k in ('actual_tokens','actual_output','actual_cost','launched_at','finished_at','response')) or json.loads(row['response'])!=response or row['actual_tokens']!=usage.get('total_tokens') or row['actual_output']!=usage.get('completion_tokens'):raise ValueError('known complete charged planning response required; no UNKNOWN recovery')
        journal=previous or dict(schema='v12-trusted-plan-review-rejection-1',channel='TRUSTED_LOCAL_REVIEW',request_id=self.ctx['request_id'],request_digest=self.ctx['request_digest'],displayed_digest=expected_displayed_digest,reason=reason,attempt=attempt,recorded_at_utc=now.isoformat(),proposal_path=self.ctx['proposal_path'],proposal_sha256=sha_file(self.ctx['proposal_path']),packet_sha256=sha_value(packet),response_path=str(location/'response.json'),response_sha256=sha_file(location/'response.json'),source_http_request_id=rid,source_payload_sha256=sha_value(payload),process_exit_sha256=sha_file(directory/'process-exit.json'),authority_sha256=self.ctx['runtime_authority']['sha256'],product_packet_changed=False,product_http_added=0,software_repair_counter_changed=False,physical_scope_expansion_allowed=False)
        # Journal, failure, then active head: a partial write can resume with
        # this exact digest/reason, without deleting or overwriting evidence.
        save_json(review_path,journal,immutable=True)
        failure=dict(stage='plan',attempt=attempt,error='ValueError: trusted local plan review rejected: '+reason,repairable=True,response=response,confirmed_plan=None,physical_scope_expansion_allowed=False,protocol_failure_fingerprint=None,review_rejection=dict(path=str(review_path),sha256=sha_file(review_path)))
        save_json(self.root/'failures'/('plan-'+str(attempt)+'.json'),failure,immutable=True)
        self.ctx.update(stage='PLAN_REJECTED',displayed_digest=None,proposal_path=None,approved_design=None,confirmation_path=None,allow_execution=False,allowed_delta=None,assumptions=[],last_plan_review_path=str(review_path))
        self.ctx.pop('plan',None);save_json(self.root/'task-context.json',self.ctx)
        return journal
    def feedback(self,stage,attempt):
        if type(attempt) is not int or attempt<1:raise ValueError('explicit positive repair attempt required')
        from .case_requests import require_planning,require_execution
        (require_execution if stage=='generate' else require_planning)(self.ctx['request_id'])
        path=self.root/'failures'/(stage+'-'+str(attempt-1)+'.json')
        if not path.exists():raise ValueError('no known complete product validation failure for this repair; UNKNOWN/execution failure cannot be retried')
        prior=read_json(path)
        if not prior.get('repairable') or not isinstance(prior.get('response'),dict) or prior['response'].get('finish_reason')=='length':raise ValueError('repair requires known complete protocol/output failure; unsupported physics/UNKNOWN is not repairable')
        older=[read_json(self.root/'failures'/(stage+'-'+str(index)+'.json')) for index in range(attempt-1) if (self.root/'failures'/(stage+'-'+str(index)+'.json')).is_file()]
        reject_repeated_failure(stage,prior,older)
        from .case_budget import CaseBudget
        event='case_'+stage+'_repair_'+str(attempt)+'_'+self.ctx['request_id']
        CaseBudget(self.ctx['runtime_authority']['ledger_path'],task_context=self.ctx).consume('software_repair',event,dict(stage=stage,attempt=attempt,prior_error=prior['error']))
        emit_progress('repair','正在纠正已知的完整回复格式错误。',operation=stage,attempt=attempt)
        action='仅纠正指出的协议/需求映射/双源码接口错误；保留已确认方案参数、范围、来源、任务。spec.sources仅按原顺序返回{id}引用，不能返回text；真实原文由Store解析。不能改物理调车、编造源码缺字或复用失败request_id。返回完整有效对象。'
        if stage=='plan':
            from .case_requests import authority_for
            selected=transport_profile(authority_for(self.ctx)['model_controls'])['profile']
            action+=' '+(plan_transport_guide(PLAN_TOOL) if selected==FLAT_PLAN_PROFILE else PLAN_STRUCTURE_GUIDE)
            if prior.get('review_rejection'):action+=' 这是可信本地审查对当前已保存但未确认方案的退回。严格解决known_failure.error指出的矛盾：历史baseline.design/assumptions只作来源，按本次raw_request与scope重新表达全部当前design.assumptions，不能复制历史修复指令；保持先前已承诺的参数、来源、scope与冻结源码，仅纠正当前需求映射和说明。'
            if 'known plan semantic conflict' in prior.get('error',''):action+=' 已知方案事实矛盾详见known_failure.error中的字段与原文；按本次scope独立重写当前操作说明，历史数值修复只能作为已存在的继承事实，不可再次声称修改冻结源码。保持先前完整数值参数、范围与来源承诺；不得由纠错扩大物理范围。'
        from .case_contact_family import numerical_scope
        if is_contact_scope(self.ctx.get('plan',{}).get('scope',{})) or 'contact_repair_package' in __import__('week13.src.case_requests',fromlist=['authority_for']).authority_for(self.ctx):
            if numerical_scope(self.ctx):
                action+=' 本数值包还要求：仅改现有FtC[i,b]的RHS，禁止新增Ft_inv或其他中间变量/声明/函数/方程；将保护内联于该RHS，公式由产品决定。字段之间逗号齐全、中文解释均为正确转义字符串，禁止Markdown和JSON后的额外文字。本请求保留完整原来源引用与六项固定参数，不能沿用超范围的新变量方案。'
                if stage=='plan':action+=(' case_request_plan只返回一个完整浅层JSON对象，不含packet/design包装。' if selected==FLAT_PLAN_PROFILE else ' case_request_plan只返回一个完整packet JSON对象。')
                if stage=='generate':action+=' 本轮proposal_prepare必须返回完整spec的schema_version/backend/baseline_ref/artifact/sources/requirements/design/libraries/settings/outputs/checks全部11字段，trajectory_contract仅基线存在时原样带入。artifact必须有完整非空model_name/modelica_source/sysml_source。仅sources的对象、差异、占位、TODO、省略号和声称沿用而不返回完整源码均不合格。'
        return dict(known_failure=prior,required_action=action)
    async def repair(self,stage,attempt):
        if stage not in ('plan','generate'):raise ValueError('repair stage must be plan or generate')
        if stage=='generate' and (self.root/'generation.json').exists():raise ValueError('successful source generation is not a protocol repair; physical task failure cannot trigger this entry')
        if stage=='plan' and self.ctx.get('plan'):raise ValueError('valid saved plan cannot be revised by protocol repair')
        return await (self.plan(attempt=attempt) if stage=='plan' else self.generate(attempt=attempt))
    async def bounded(self,stage,initial):
        """Automatically repair only locally classified complete output failures."""
        if (stage=='plan' and self.ctx.get('plan')) or (stage=='generate' and ((self.root/'generation.json').exists() or (self.root/'generation-revalidated.json').exists())):return await initial()
        paths=list((self.root/'failures').glob(stage+'-*.json'))
        if not paths:
            try:return await initial()
            except (ValueError,TypeError,KeyError):
                paths=list((self.root/'failures').glob(stage+'-*.json'))
                if not paths:raise
        while paths:
            attempt=max(int(p.stem.rsplit('-',1)[-1]) for p in paths)+1
            try:return await self.repair(stage,attempt)
            except (ValueError,TypeError,KeyError):
                path=self.root/'failures'/(stage+'-'+str(attempt)+'.json')
                if not path.exists():raise
                # The shared software counter in feedback is the hard bound;
                # no new attempts are possible once that authority is spent.
                paths.append(path)
    def _plan_source_facts(self,obligations=None):
        obligations=imported_obligations(self.ctx) if obligations is None else obligations
        return selected_source_facts(self.ctx,obligations)
    def _validate_plan_packet(self,packet):
        obligations=validate_import_plan(self.ctx,packet)
        validation=validate_request_plan(packet,self.ctx['baseline_snapshot']['frozen_spec'],self.ctx['sources'],task_context=self.ctx)
        validation['known_semantic_validation']=require_plan_semantics(packet,self.ctx['baseline_snapshot']['frozen_spec'])
        if validation['executable'] and not any(x['source_id']==self.ctx['sources'][0]['id'] for x in packet['requirements']):raise ValueError('product omitted original new request provenance')
        facts=self._plan_source_facts(obligations)
        warnings=plan_diagnostic_warnings(validation['known_semantic_validation'])
        if facts['baseline_sysml_available'] and any('sysml' in item.casefold() for item in obligations.get('historical_missing',[])):
            warnings.append('已核实所选基线'+facts['baseline_version_id']+'已有SysML；原v0导入缺项属于历史。原方案文字保持不变，其中若称当前基准缺少SysML或需重新补齐，该说明与已核实事实不符；确认后仅同步生成候选双源码。')
        validation.update(packet_digest=sha_value(packet),verified_source_facts=facts,review_warnings=warnings)
        return validation
    def _check_prior_plan_parameters(self,packet,attempt):
        if not attempt:return
        prior=read_json(self.root/'failures'/('plan-'+str(attempt-1)+'.json'))
        try:old=parse_tool(prior['response'],'case_request_plan','packet').get('proposed_parameters')
        except (ValueError,TypeError,KeyError):
            old=None
            if prior['response'].get('request_tool_output_profile')==FLAT_PLAN_PROFILE:
                # Comparison only: never recover a rejected packet for execution.
                from .case_strict_tools import strict_json
                try:
                    calls=prior['response']['tool_calls']
                    if len(calls)==1 and calls[0]['function']['name']=='case_request_plan':
                        raw=strict_json(calls[0]['function']['arguments'])
                        candidate=raw.get('proposed_parameters') if isinstance(raw,dict) else None
                        if isinstance(candidate,dict) and set(candidate) in (set(REQUEST_PARAMETERS),set(STAIR_PLAN_PARAMETERS)) and all(type(value) in (int,float) and __import__('math').isfinite(value) for value in candidate.values()):old=candidate
                except (ValueError,TypeError,KeyError):pass
        if isinstance(old,dict) and old and packet.get('proposed_parameters')!=old:raise ValueError('protocol repair changed prior complete parameter commitments')
    async def plan(self,attempt=0):
        self.refresh()
        if self.ctx.get('plan'):
            validation=self._validate_plan_packet(self.ctx['plan'])
            return dict(reused=True,plan=self.ctx['plan'],displayed_digest=self.ctx.get('displayed_digest'),validation=validation)
        baseline=self.ctx['baseline_snapshot']['frozen_spec']
        obligations=imported_obligations(self.ctx);facts=self._plan_source_facts(obligations)
        completion_rule='原v0本体与已验证SysML侧证分别保存；已有可信侧证或本体SysML时只同步生成候选双源码，不重新生成原侧证。' if facts['validated_baseline_sysml_available'] else '缺少原基准SysML时，SUPPORTED方案defaults必须明确披露确认后补齐原始基准和候选两份SysML，并分别绑定原资料与新需求；不能跳过，不要求用户手写。'
        current_import={key:deepcopy(value) for key,value in obligations.items() if not key.startswith('historical_')}
        historical_import={key:deepcopy(value) for key,value in obligations.items() if key.startswith('historical_')}
        context=dict(raw_request=self.ctx['raw_request'],request_source_id=self.ctx['sources'][0]['id'],trusted_sources=self.ctx['sources'],baseline_snapshot=self.ctx['baseline_snapshot'],baseline_parameters=modelica_parameters(baseline['artifact']['modelica_source'])[0],sysml_contract=module('artifact_sysml').CONTRACT_PROMPT,imported_case=current_import,historical_import=dict(role='HISTORY_ONLY_NOT_CURRENT_MISSING_ITEMS',**historical_import),verified_source_facts=facts,source_completion_rule=completion_rule)
        from .case_contact_family import reviewed_stair_capability
        capability=reviewed_stair_capability(baseline['artifact']['modelica_source'])
        if capability:context['reviewed_stair_capability']=capability
        if self.ctx.get('access_mode')=='RESULT_REVIEW_ONLY':
            if self.ctx['step_id'] not in ('negative_mass','negative_geometry'):raise PermissionError('this readonly step is an explanation, not a planning stage')
            context['baseline_snapshot']=self.review_evidence()
            context['plan_only_no_confirmation_generation_or_execution']=True
        contact=planning_context(self.ctx);prompt=PLAN_PROMPT
        from .case_asym2_family import is_asym2_source,context_contract,SYSML_GUIDE,PLAN_PROMPT as ASYM2_PLAN_PROMPT
        if is_asym2_source(baseline['artifact']['modelica_source']):
            prompt=ASYM2_PLAN_PROMPT
            context['asym2_family_contract']=context_contract(baseline['artifact']['modelica_source'])
            context['sysml_contract']=module('artifact_sysml').CONTRACT_PROMPT+'\n'+SYSML_GUIDE
        if contact:
            from .case_contact_family import PLAN_PROMPT as CONTACT_PLAN_PROMPT
            context['contact_repair_authority']=contact;prompt=CONTACT_PLAN_PROMPT
            if contact.get('numerical_only'):
                from .case_contact_numerics import PLAN_PROMPT as NUMERIC_PLAN_PROMPT
                prompt=NUMERIC_PLAN_PROMPT
        if PLAN_STRUCTURE_GUIDE not in prompt:prompt+='\n'+PLAN_STRUCTURE_GUIDE
        prompt+='\n当前来源事实以verified_source_facts为准：baseline_sysml_available=true即本次已具备可用基准SysML，按generation_source_rule只生成候选；historical_import及baseline旧design/defaults是历史原文，不得据此重报当前缺项或重新补齐已具备的基准SysML。'
        from .case_requests import authority_for
        selected=transport_profile(authority_for(self.ctx)['model_controls'])['profile']
        if selected==FLAT_PLAN_PROFILE:prompt=flat_plan_prompt(prompt)
        if attempt:context['bounded_protocol_repair']=self.feedback('plan',attempt)
        emit_progress('plan','正在等待产品模型整理需求与提出方案。',attempt=attempt)
        try:response=await self.call('plan' if not attempt else 'plan_repair_'+str(attempt),[dict(role='system',content=prompt),dict(role='user',content=json.dumps(context,ensure_ascii=False))],[PLAN_TOOL])
        except json.JSONDecodeError as exc:
            if exc.msg!='Extra data' or transport_profile(__import__('week13.src.case_requests',fromlist=['authority_for']).authority_for(self.ctx)['model_controls'])['strict_tools']:raise
            # Runtime remains strict/rejected; only this known planning suffix
            # may become a source-bound proposal pending human review.
            return self._revalidate_plan_suffix(attempt)
        return self._accept_plan_response(response,attempt=attempt)

    def _accept_plan_response(self,response,attempt=0):
        """Validate and save one existing reply; no runtime or HTTP operations."""
        try:
            emit_progress('validation','正在检查方案、来源和支持范围。')
            packet=parse_tool(response,'case_request_plan','packet');normalized=normalize_empty_nonexecutable_defaults(packet)
            if normalized!=packet:
                save_json(self.root/'normalizations'/('plan-'+str(attempt)+'-empty-defaults.json'),dict(schema='v12-lossless-plan-normalization-1',source_response_sha256=sha_value(response),source_packet_sha256=sha_value(packet),field_changes={'defaults':dict(before={},after=[])},business_values_changed=False,normalized_packet_sha256=sha_value(normalized)),immutable=True)
                packet=normalized
            packet,rationale_audit=normalize_plan_rationale(packet)
            validation=self._validate_plan_packet(packet)
            self._check_prior_plan_parameters(packet,attempt)
            if rationale_audit:
                rationale_audit.update(source_response_sha256=sha_value(response),source_response_path=str(self.root/'turns'/('plan' if not attempt else 'plan_repair_'+str(attempt))/'response.json'))
                save_json(self.root/'normalizations'/('plan-'+str(attempt)+'-rationale-object.json'),rationale_audit,immutable=True)
        except (ValueError,TypeError,KeyError) as exc:self.failure('plan',attempt,exc,response);raise
        from .case_requests import save_plan
        self.ctx=save_plan(self.ctx['request_id'],packet)
        validation_path=self._save_plan_validation(validation,attempt=attempt)
        return dict(plan=packet,displayed_digest=self.ctx.get('displayed_digest'),requires_local_confirmation=validation['executable'],validation=validation,validation_path=str(validation_path))
    def _save_plan_validation(self,validation,*,attempt):
        """Bind each validation to its plan; retain the first fixed receipt."""
        path=self.root/'plan-validations'/(self.ctx['displayed_digest']+'.json')
        save_json(path,dict(schema='v12-displayed-plan-validation-1',request_id=self.ctx['request_id'],request_digest=self.ctx['request_digest'],displayed_digest=self.ctx['displayed_digest'],attempt=attempt,validation=validation),immutable=True)
        fixed=self.root/'plan-validation.json'
        if not fixed.exists():save_json(fixed,validation,immutable=True)
        save_json(self.root/'plan-validation-head.json',dict(displayed_digest=self.ctx['displayed_digest'],path=str(path),sha256=sha_file(path)))
        return path
    def _plan_normalization_evidence(self,attempt,*,rationale_object=False,semantic_only=False):
        """Bind one known complete planning reply to its original wire/ledger."""
        from .case_requests import require_planning,authority_for
        if transport_profile(authority_for(self.ctx)['model_controls'])['strict_tools'] and not semantic_only:
            raise PermissionError('strict output cannot use legacy plan shape recovery')
        from .case_runtime import _MODELS
        if type(attempt) is not int or attempt<0:raise ValueError('explicit saved nonnegative plan attempt required')
        self.ctx=require_planning(self.ctx['request_id']);authority=authority_for(self.ctx)
        now=datetime.now(timezone.utc)
        if not datetime.fromisoformat(authority['started_at_utc'].replace('Z','+00:00'))<=now<datetime.fromisoformat(authority['hard_deadline_utc'].replace('Z','+00:00')):raise PermissionError('plan revalidation requires the active original budget interval')
        if self.ctx.get('access_mode')=='RESULT_REVIEW_ONLY' or self.ctx.get('stage') not in ('DRAFT','PROPOSED') or self.ctx.get('confirmation_path'):raise PermissionError('ordinary plan suffix revalidation cannot confirm or revise a confirmed request')
        stage='plan' if attempt==0 else 'plan_repair_'+str(attempt);folder=self.root/'turns'/stage
        request=self.effective_stage_request(folder);failure_path=self.root/'failures'/('plan-'+str(attempt)+'.json');failure=read_json(failure_path)
        response=failure.get('response',{})
        successful=rationale_object or semantic_only
        if successful:
            if read_json(folder/'response.json')!=response:raise ValueError('saved successful planning response/failure binding differs')
            original=parse_tool(response,'case_request_plan','packet')
            if semantic_only:
                if not failure.get('error','').startswith('ValueError: known plan semantic conflict;'):raise ValueError('semantic-only revalidation requires the original known-semantic rejection')
                packet=deepcopy(original)
                normalization=dict(schema='v12-complete-plan-semantic-revalidation-1',transformation='NONE_ORIGINAL_COMPLETE_PACKET_UNCHANGED',
                    source_packet_sha256=sha_value(original),revalidated_packet_sha256=sha_value(packet),field_changes={},
                    business_values_changed=False,source_strings_changed=False,original_error=failure['error'])
            else:
                packet,normalization=normalize_plan_rationale(original)
                if normalization is None:raise ValueError('saved planning response has no flat rationale object normalization')
                normalization['original_error']='ValueError: plan rationale required'
            normalization.update(source_response_sha256=sha_value(response),source_response_path=str(folder/'response.json'))
        else:packet,normalization=plan_suffix_arguments(response)
        rid='case_'+stage+'_'+self.ctx['request_id']
        if request.get('request_id')!=rid or request.get('request_digest')!=self.ctx['request_digest'] or failure.get('stage')!='plan' or failure.get('attempt')!=attempt or failure.get('repairable') is not True or failure.get('confirmed_plan') is not None or failure.get('error')!=normalization['original_error']:raise ValueError('original plan failure/request/error binding differs')
        context=json.loads(request['messages'][1]['content'])
        if context.get('raw_request')!=self.ctx['raw_request'] or context.get('request_source_id')!=self.ctx['sources'][0]['id'] or not projected_context_matches(self.ctx,context):raise ValueError('original planning sources/baseline/request binding differs')
        if attempt and context.get('bounded_protocol_repair',{}).get('known_failure')!=read_json(self.root/'failures'/('plan-'+str(attempt-1)+'.json')):raise ValueError('original prior planning failure binding differs')
        directory=self.root/'http'/sha_value(rid);wire=read_json(directory/'request.json');protocol=read_json(directory/'protocol-response.json');exit_receipt=read_json(directory/'process-exit.json');status=read_json(directory/'http-status.json')
        rejected=None if successful else read_json(directory/'rejected-response.json')
        payload=wire.get('payload');choices=protocol.get('choices',[])
        if semantic_only:
            # Reuse the existing deterministic transport builder without any
            # runtime/CaseBudget constructor, credentials or HTTP operation.
            from types import SimpleNamespace
            from .case_runtime import CaseRuntime
            pure=SimpleNamespace(authorization=authority,task_context=self.ctx,config=authority['model_controls'])
            expected_payload,_=CaseRuntime.build_payload(pure,request['messages'],request['tools'])
            same_transport=payload==expected_payload and wire.get('internal_tools')==request['tools'] and wire.get('transport_profile')==transport_profile(authority['model_controls'])
        else:same_transport=isinstance(payload,dict) and payload.get('messages')==request['messages'] and payload.get('tools')==request['tools']
        if not isinstance(payload,dict) or wire.get('request_id')!=rid or wire.get('authorization_sha256')!=self.ctx['runtime_authority']['sha256'] or wire.get('payload_sha256')!=sha_value(payload) or not same_transport or len(request.get('tools',[]))!=1 or request['tools'][0].get('function',{}).get('name')!='case_request_plan':raise ValueError('original planning wire payload/authority binding differs')
        if protocol.get('model') not in _MODELS or not isinstance(protocol.get('id'),str) or len(choices)!=1 or choices[0].get('finish_reason') not in ('tool_calls','stop'):raise ValueError('known complete planning protocol required')
        if successful:
            if (directory/'rejected-response.json').exists() or response.get('response_model')!=protocol['model'] or response.get('response_id')!=protocol['id']:raise ValueError('known complete successful planning protocol required')
        elif rejected.get('usage_known') is not True or rejected.get('error_type')!='JSONDecodeError' or rejected.get('response')!=protocol or protocol!=response.get('raw_rejected_protocol'):raise ValueError('known complete rejected planning protocol required')
        message=choices[0].get('message',{})
        if semantic_only:
            from .case_strict_tools import decode_calls
            decoded,mapping=decode_calls(message.get('tool_calls',[]),request['tools'],profile=transport_profile(authority['model_controls'])['profile'])
            calls_match=(decoded==response.get('tool_calls') and message.get('tool_calls')==response.get('wire_tool_calls') and mapping==response.get('wire_structure_mapping'))
        else:calls_match=message.get('tool_calls')==response.get('tool_calls')
        fixture=wire.get('fixture_transport') is True
        terminal=(exit_receipt.get('fixture_call_returned') is True and exit_receipt.get('process_launched') is False) if fixture else (exit_receipt.get('process_launched') is True and exit_receipt.get('local_process_terminated') is True and exit_receipt.get('termination_requested') is False)
        if not calls_match or (message.get('content') or '')!=response.get('content') or choices[0]['finish_reason']!=response.get('finish_reason') or status.get('http_status')!=200 or exit_receipt.get('request_id')!=rid or exit_receipt.get('returncode')!=0 or not terminal or exit_receipt.get('timeout_classification') is not None or exit_receipt.get('child_error_type') is not None or exit_receipt.get('fixture_transport')!=wire.get('fixture_transport') or (rejected is not None and wire.get('fixture_transport')!=rejected.get('fixture_transport')):raise ValueError('terminal planning response/local exit binding differs')
        if successful:
            assistant=dict(role='assistant',content=message.get('content') or '',reasoning_content=message.get('reasoning_content') or '')
            if message.get('tool_calls'):assistant['tool_calls']=message['tool_calls']
            if response.get('assistant_message')!=assistant or response.get('reasoning_content')!=assistant['reasoning_content'] or any(payload.get(k)!=v for k,v in dict(model='deepseek-flash',thinking=dict(type='enabled'),reasoning_effort='high',max_tokens=393216).items()):raise ValueError('saved successful planning protocol/settings binding differs')
        with closing(sqlite3.connect(Path(self.ctx['runtime_authority']['ledger_path']).resolve().as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row
            row=db.execute('SELECT * FROM requests WHERE request_id=?',(rid,)).fetchone();meta=dict(db.execute('SELECT key,value FROM meta'))
            uncertain=db.execute("SELECT 1 FROM requests WHERE status IN ('unknown','reserved','dispatched') LIMIT 1").fetchone()
        usage=protocol.get('usage',{})
        expected_status='succeeded' if successful else 'rejected'
        reason_matches=row and (json.loads(row['response'])==response if successful and row['response'] else row['reason']==normalization['original_error'] if not successful else False)
        if not row or row['status']!=expected_status or not reason_matches or row['content_hash']!=sha_value(payload) or any(row[k] is None for k in ('actual_tokens','actual_output','actual_cost','launched_at','finished_at')) or row['actual_tokens']!=usage.get('total_tokens') or row['actual_output']!=usage.get('completion_tokens') or uncertain or meta.get('frozen') or (semantic_only and (meta.get('closed') or meta.get('retired'))) or meta.get('authorization_sha256')!=self.ctx['runtime_authority']['sha256']:raise ValueError('known complete charged '+expected_status+' planning ledger required; no UNKNOWN/closed recovery')
        svc=self.open(read_only=True);baseline=module('artifact_baseline')
        if baseline.selection(svc)!=self.ctx['baseline_ref'] or baseline.resolve(svc,self.ctx['baseline_ref'])['frozen_spec']!=self.ctx['baseline_snapshot']['frozen_spec']:raise ValueError('selected real planning baseline differs')
        normalization.update(request_id=self.ctx['request_id'],request_digest=self.ctx['request_digest'],baseline_ref=self.ctx['baseline_ref'],baseline_snapshot_sha256=sha_value(self.ctx['baseline_snapshot']),authority_sha256=self.ctx['runtime_authority']['sha256'],source_ids=[x['id'] for x in self.ctx['sources']],source_http_request_id=rid,source_stage_request_sha256=sha_value(request),source_payload_sha256=sha_value(payload),source_protocol_sha256=sha_value(protocol),source_process_exit_sha256=sha_value(exit_receipt),source_http_status_sha256=sha_value(status),original_failure_path=str(failure_path),original_failure_sha256=sha_value(failure),original_http_status=expected_status,product_http_added=0,software_repair_counter_changed=False,confirmation_added=False,generation_added=False,compile_added=0,simulation_added=0)
        if rejected is not None:normalization['source_rejected_receipt_sha256']=sha_value(rejected)
        if semantic_only:
            normalization.update(revalidation_source_sha256={str(path):sha_file(path) for path in (Path(__file__),Path(__file__).with_name('case_semantic_guard.py'))},
                original_error_sha256={str(path):sha_file(path) for path in sorted((self.root/'failures').glob('plan-*.json'))})
        return packet,normalization
    def _plan_suffix_evidence(self,attempt):
        return self._plan_normalization_evidence(attempt)
    def _plan_rationale_evidence(self,attempt):
        return self._plan_normalization_evidence(attempt,rationale_object=True)
    def _revalidate_plan_suffix(self,attempt=0,*,expected_packet_digest=None):
        packet,audit=self._plan_suffix_evidence(attempt)
        return self._save_revalidated_plan(packet,audit,attempt,'json-suffix',expected_packet_digest)
    def revalidate_plan_rationale(self,attempt=0,*,expected_packet_digest=None):
        """Explicit zero-HTTP recovery of one complete flat-rationale reply."""
        packet,audit=self._plan_rationale_evidence(attempt)
        return self._save_revalidated_plan(packet,audit,attempt,'rationale-object',expected_packet_digest)
    def _save_revalidated_plan(self,packet,audit,attempt,label,expected_packet_digest):
        from .case_requests import save_plan
        validation=self._validate_plan_packet(packet)
        self._check_prior_plan_parameters(packet,attempt)
        if expected_packet_digest is not None and sha_value(packet)!=expected_packet_digest:raise ValueError('revalidated plan differs from expected packet digest')
        audit['validation']=validation
        audit_path=self.root/'normalizations'/('plan-'+str(attempt)+'-'+label+'.json');validation_path=self.root/'plan-validation.json'
        for path,value in ((audit_path,audit),(validation_path,validation)):
            if path.exists() and read_json(path)!=value:raise ValueError('existing planning normalization evidence differs; no overwrite')
        if self.ctx.get('plan'):
            if self.ctx['plan']!=packet or not audit_path.exists() or not validation_path.exists():raise ValueError('saved plan does not match this original '+label+' normalization')
        else:
            save_json(audit_path,audit,immutable=True)
            self.ctx=save_plan(self.ctx['request_id'],packet)
            save_json(validation_path,validation,immutable=True)
        return dict(plan=packet,displayed_digest=self.ctx.get('displayed_digest'),validation=validation,requires_local_confirmation=validation['executable'],product_http_added=0,normalization_audit=str(audit_path),original_http_status=audit['original_http_status'])
    def revalidate_plan(self,attempt=0,*,expected_packet_digest=None):
        """Offline, evidence-bound shape correction of a known complete reply."""
        from .case_requests import save_plan
        from .case_budget import CaseBudget
        self.refresh()
        if self.ctx.get('access_mode')!='RESULT_REVIEW_ONLY':
            failure=self.root/'failures'/('plan-'+str(attempt)+'.json')
            if failure.exists() and read_json(failure).get('error','').startswith('ValueError: known plan semantic conflict;'):
                if expected_packet_digest is None:raise ValueError('semantic-only revalidation requires expected original packet digest')
                packet,audit=self._plan_normalization_evidence(attempt,semantic_only=True)
                return self._save_revalidated_plan(packet,audit,attempt,'semantic-only',expected_packet_digest)
            if failure.exists() and read_json(failure).get('error')=='ValueError: plan rationale required':return self.revalidate_plan_rationale(attempt,expected_packet_digest=expected_packet_digest)
            return self._revalidate_plan_suffix(attempt,expected_packet_digest=expected_packet_digest)
        if self.ctx.get('access_mode')!='RESULT_REVIEW_ONLY' or self.ctx.get('step_id') not in ('negative_mass','negative_geometry'):raise PermissionError('offline normalization is restricted to plan-only closeout contexts')
        if self.ctx.get('plan'):return dict(reused=True,plan=self.ctx['plan'],product_http_added=0)
        if type(attempt) is not int or attempt<0:raise ValueError('explicit saved nonnegative attempt required')
        stage='plan' if attempt==0 else 'plan_repair_'+str(attempt);folder=self.root/'turns'/stage
        request=self.effective_stage_request(folder);response=read_json(folder/'response.json');failure=read_json(self.root/'failures'/('plan-'+str(attempt)+'.json'))
        budget=CaseBudget(task_context=self.ctx);row=budget.request(request['request_id'])
        if request['request_digest']!=self.ctx['request_digest'] or failure['response']!=response or failure['error']!='ValueError: invalid defaults' or not row or row['status']!='succeeded' or any(row[k] is None for k in ('actual_tokens','actual_output','actual_cost')) or json.loads(row['response'])!=response:raise ValueError('known complete reply/failure/ledger binding required; no UNKNOWN recovery')
        original=parse_tool(response,'case_request_plan','packet');packet=normalize_empty_nonexecutable_defaults(original)
        if packet==original:raise ValueError('no unique empty-defaults shape correction available')
        validation=validate_request_plan(packet,self.ctx['baseline_snapshot']['frozen_spec'],self.ctx['sources'],task_context=self.ctx)
        if validation['executable']:raise PermissionError('offline reply correction cannot create an executable design')
        audit=dict(schema='v12-lossless-plan-normalization-1',source_response_path=str(folder/'response.json'),source_response_sha256=sha_value(response),source_packet_sha256=sha_value(original),original_failure_path=str(self.root/'failures'/('plan-'+str(attempt)+'.json')),field_changes={'defaults':dict(before={},after=[])},business_values_changed=False,normalized_packet_sha256=sha_value(packet),product_http_added=0,validation=validation)
        path=self.root/'normalizations'/('plan-'+str(attempt)+'-empty-defaults.json');save_json(path,audit,immutable=True)
        self.ctx=save_plan(self.ctx['request_id'],packet);save_json(self.root/'plan-validation.json',validation,immutable=True)
        return dict(plan=packet,displayed_digest=self.ctx.get('displayed_digest'),validation=validation,requires_local_confirmation=False,product_http_added=0,normalization_audit=str(path))
    def confirm(self,displayed_digest,text,*,approval_basis=None,human_reviewed_displayed_plan=True):
        from .case_requests import confirm_request
        self.refresh()
        if not self.ctx.get('plan') or not displayed_digest or displayed_digest!=self.ctx.get('displayed_digest'):raise ValueError('trusted confirmation must bind the current displayed plan; rejected/stale plan cannot be confirmed')
        evidence=self._validate_plan_packet(self.ctx['plan'])
        if not evidence['executable']:raise ValueError('question/incomplete/unsupported plan cannot be confirmed for execution')
        return confirm_request(self.ctx['request_id'],displayed_digest,text,approval_basis=approval_basis,human_reviewed_displayed_plan=human_reviewed_displayed_plan)
    @asynccontextmanager
    async def channel(self):
        if self.rpc:
            yield;return
        from mcp import ClientSession,StdioServerParameters
        from mcp.client.stdio import stdio_client
        from .case_requests import remaining_request,authority_for
        binding=timeout_binding(authority_for(self.ctx),remaining_request(self.ctx['request_id']),authority_sha256=self.ctx['runtime_authority']['sha256'])
        params=StdioServerParameters(command=sys.executable,args=['-X','utf8','-B','-m','week13.src.case_mcp'],cwd=str(REPO),env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',MBSE_V12_REQUEST_ID=self.ctx['request_id']))
        self._mcp_observations=[];initializing=None
        try:
            async with stdio_client(params) as (read,write):
                async with ClientSession(read,write,read_timeout_seconds=timedelta(seconds=binding['actual_cap_s'])) as session:
                    with phase('MCP_INITIALIZE',tool='initialize',transport='REAL_STDIO_MCP',rpc_dispatched=None,call_invoked=False,rpc_dispatch_status='NOT_INVOKED',**binding) as observed:
                        initializing=observed
                        observed.update(call_invoked=True,rpc_dispatch_status='CLIENT_CALL_INVOKED_WIRE_SEND_UNPROVEN')
                        await session.initialize();observed.update(outcome='RESPONSE_RECEIVED',rpc_dispatched=True,rpc_dispatch_status='RESPONSE_RECEIVED')
                    self._session=session
                    try:yield
                    finally:self._session=None
        except BaseException as exc:
            if initializing is not None and initializing.get('outcome')!='RESPONSE_RECEIVED':self._mcp_observations.append(dict(initializing))
            if not self._mcp_observations:self._mcp_observations.append(dict(tool='initialize',transport='REAL_STDIO_MCP',call_invoked=False,rpc_dispatched=False,rpc_dispatch_status='NOT_INVOKED',outcome='NOT_DISPATCHED',**binding))
            attach_calls(exc,self._mcp_observations);raise
    async def mcp(self,name,args):
        if not self.rpc and not getattr(self,'_session',None):
            async with self.channel():return await self.mcp(name,args)
        binding={}
        if not self.rpc:
            from .case_requests import remaining_request,authority_for
            binding=timeout_binding(authority_for(self.ctx),remaining_request(self.ctx['request_id']),authority_sha256=self.ctx['runtime_authority']['sha256'])
        observed=dict(tool=tool_name(name),transport='ISOLATED_FIXTURE' if self.rpc else 'REAL_STDIO_MCP',rpc_dispatched=None,call_invoked=False,rpc_dispatch_status='NOT_INVOKED',remote_cancellation_proven=None,**binding)
        started=time.monotonic();observed['call_started_monotonic']=started
        try:
            with phase('MCP_CALL',**observed) as progress:
                observed.update(call_invoked=True,rpc_dispatch_status='CLIENT_CALL_INVOKED_WIRE_SEND_UNPROVEN');progress.update(observed)
                if self.rpc:result=await self.rpc(self,name,args)
                else:
                    response=await self._session.call_tool(name,args,read_timeout_seconds=timedelta(seconds=binding['actual_cap_s']))
                    observed.update(rpc_dispatched=True,rpc_dispatch_status='RESPONSE_RECEIVED');progress.update(observed)
                    texts=[x.text for x in response.content if getattr(x,'type',None)=='text']
                    if response.isError or len(texts)!=1:raise ValueError('MCP response error or invalid JSON block')
                    result=json.loads(texts[0])
                    if not result.get('ok'):raise ValueError('MCP response rejected; payload omitted')
                observed.update(outcome='RESPONSE_RECEIVED',rpc_dispatched=True,rpc_dispatch_status='RESPONSE_RECEIVED');progress.update(observed)
                observed.update(monotonic=time.monotonic(),elapsed_s=time.monotonic()-started)
                history=getattr(self,'_mcp_observations',[]);history.append(observed);self._mcp_observations=history[-8:]
                return result
        except BaseException as exc:
            observed.update(outcome='UNKNOWN_AFTER_CALL_INVOCATION' if observed['call_invoked'] else 'NOT_DISPATCHED',elapsed_s=time.monotonic()-started)
            history=getattr(self,'_mcp_observations',[]);history.append(observed);self._mcp_observations=history[-8:]
            attach_calls(exc,[observed]);raise
    def _validate_generation(self,response,response_stage,*,source_references=False,needs_baseline=None,local_spec=None):
        """One validation path for fresh generation and explicit saved migration."""
        base=self.ctx['baseline_snapshot']['frozen_spec'];completion=None
        if needs_baseline is None:needs_baseline=not baseline_sysml_available(self.ctx)
        if local_spec is None:args,_=generation_arguments(response,needs_baseline=needs_baseline)
        else:
            if needs_baseline:raise ValueError('local parameter tool cannot complete a missing baseline SysML')
            args=dict(spec=deepcopy(local_spec))
        if needs_baseline:
            completed=deepcopy(base);completed['artifact']['sysml_source']=module('artifact_sysml').normalize_package_wrapper(args['baseline_sysml_source'])[0]
            completed_check=module('model_artifact').validate_artifact(completed,require_sysml=True)
            if not completed_check['ready']:raise ValueError('original-baseline SysML validation failed: '+json.dumps(completed_check['issues'],ensure_ascii=False))
            completion=dict(schema='v12-baseline-sysml-completion-1',request_digest=self.ctx['request_digest'],baseline_ref=self.ctx['baseline_ref'],baseline_snapshot_sha256=sha_value(self.ctx['baseline_snapshot']),completed_baseline_spec=completed,completed_spec_sha256=completed_check['spec_digest'],source_response_path=str(self.root/'turns'/response_stage/'response.json'),source_response_sha256=sha_value(response),origin='CONFIRMED_PRODUCT_GENERATION',original_modelica_and_metadata_unchanged=True)
        spec=deepcopy(args['spec']);declared=spec.get('sources');expected=[x['id'] for x in self.ctx['sources']]
        if not isinstance(declared,list) or any(not isinstance(x,dict) or set(x) not in ({'id'},{'id','text'}) for x in declared) or [x.get('id') for x in declared]!=expected:raise ValueError('generation source IDs must match trusted request completely and in order')
        if source_references:spec['sources']=[dict(id=x['id']) for x in declared]
        emit_progress('validation','正在解析双源码并检查已确认方案、来源和允许差异。')
        svc=self.open(read_only=True)
        spec,_=svc.normalize_candidate(spec)
        contact=is_contact_scope(self.ctx['plan']['scope'])
        if not contact:delta=check_delta(svc,spec,task_context=self.ctx)
        checked=module('model_artifact').validate_artifact(spec,require_sysml=True)
        if not checked['ready']:raise ValueError('new request dual-source validation failed: '+json.dumps(checked['issues'],ensure_ascii=False))
        try:
            if contact:delta=check_delta(svc,spec,task_context=self.ctx)
        except ContactSourceReviewRequired as exc:
            exc.completion=completion
            raise
        if contact:
            from .case_contact_family import numerical_scope
            if numerical_scope(self.ctx):
                from .case_budget import CaseBudget
                CaseBudget(task_context=self.ctx).mark_valid_generated_source(checked['spec_digest'])
        return spec,delta,checked,completion
    def _revalidation_evidence(self,attempt):
        """Read only: current confirmation, original wire payload and charged reply."""
        from .case_requests import authority_for
        if type(attempt) is not int or attempt<0:raise ValueError('explicit saved nonnegative attempt required')
        self.refresh();authority=authority_for(self.ctx)
        now=datetime.now(timezone.utc)
        if not datetime.fromisoformat(authority['started_at_utc'].replace('Z','+00:00'))<=now<datetime.fromisoformat(authority['hard_deadline_utc'].replace('Z','+00:00')):raise PermissionError('generation revalidation requires active original budget interval; no implicit recovery')
        if self.ctx.get('access_mode')=='RESULT_REVIEW_ONLY' or authority.get('permissions',{}).get('execution') is not True or self.ctx.get('stage')!='CONFIRMED' or not self.ctx.get('confirmation_path'):raise PermissionError('generation revalidation requires trusted confirmed execution authority')
        confirmation=read_json(self.ctx['confirmation_path']);plan=read_json(self.ctx['proposal_path'])
        if confirmation.get('request_id')!=self.ctx['request_id'] or confirmation.get('request_digest')!=self.ctx['request_digest'] or confirmation.get('channel')!='TRUSTED_LOCAL_CLI' or confirmation.get('product_self_approval') is not False or confirmation.get('displayed_digest')!=self.ctx['displayed_digest'] or sha_value(plan)!=self.ctx['displayed_digest'] or plan.get('packet')!=self.ctx['plan'] or plan.get('sources')!=self.ctx['sources'] or plan.get('baseline_ref')!=self.ctx['baseline_ref']:raise ValueError('generation revalidation confirmation/plan/source binding differs')
        validate_import_plan(self.ctx,self.ctx['plan'])
        if not validate_request_plan(self.ctx['plan'],self.ctx['baseline_snapshot']['frozen_spec'],self.ctx['sources'],task_context=self.ctx)['executable']:raise PermissionError('confirmed plan is not executable')
        stage='generate' if attempt==0 else 'generate_repair_'+str(attempt);folder=self.root/'turns'/stage
        request=self.effective_stage_request(folder);response=read_json(folder/'response.json');failure=read_json(self.root/'failures'/('generate-'+str(attempt)+'.json'))
        rid='case_'+stage+'_'+self.ctx['request_id']
        if request.get('request_id')!=rid or request.get('request_digest')!=self.ctx['request_digest'] or failure.get('stage')!='generate' or failure.get('attempt')!=attempt or failure.get('repairable') is not True or failure.get('response')!=response or failure.get('confirmed_plan')!=self.ctx['plan']:raise ValueError('saved generation failure/request/confirmed-plan binding differs')
        normalization=None
        if failure.get('error')=='ValueError: generation must include exactly candidate spec and required original baseline_sysml_source':
            _,normalization=generation_arguments(response,needs_baseline=not self.ctx['baseline_snapshot']['frozen_spec']['artifact'].get('sysml_source'))
            if normalization is None:raise ValueError('saved generation failure has no single spec JSON string normalization')
        elif failure.get('error')=='ValueError: spec source text differs from trusted CLI input':
            _,extra=generation_arguments(response,needs_baseline=not self.ctx['baseline_snapshot']['frozen_spec']['artifact'].get('sysml_source'))
            if extra is not None:raise ValueError('source-reference migration cannot also normalize spec JSON encoding')
        else:raise ValueError('saved generation failure is not an eligible source-reference or spec JSON protocol failure')
        context=json.loads(request['messages'][1]['content']);contract=context['trusted_generation_contract']
        if not projected_context_matches(self.ctx,context) or context.get('confirmed_plan')!=self.ctx['plan'] or context.get('raw_request')!=self.ctx['raw_request'] or contract.get('baseline_ref')!=self.ctx['baseline_ref'] or contract.get('requirements')!=self.ctx['plan']['requirements'] or contract.get('design')!=self.ctx['plan']['design'] or contract.get('sources') not in (self.ctx['sources'],[dict(id=x['id']) for x in self.ctx['sources']]):raise ValueError('saved generation context differs from current trusted request')
        ledger=Path(self.ctx['runtime_authority']['ledger_path'])
        with closing(sqlite3.connect(ledger.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row
            row=db.execute('SELECT * FROM requests WHERE request_id=?',(rid,)).fetchone();meta=dict(db.execute('SELECT key,value FROM meta'))
            uncertain=db.execute("SELECT 1 FROM requests WHERE status IN ('unknown','reserved','dispatched') LIMIT 1").fetchone()
            candidate=db.execute("SELECT detail_json FROM counters WHERE kind='candidate' AND event_id=?",('request_candidate_'+self.ctx['request_id'],)).fetchone()
        if not candidate or json.loads(candidate['detail_json'])!=dict(request_id=self.ctx['request_id'],displayed_digest=self.ctx['displayed_digest']):raise ValueError('original charged candidate event/request confirmation binding required')
        if not row or row['status']!='succeeded' or any(row[k] is None for k in ('actual_tokens','actual_output','actual_cost','launched_at','finished_at')) or uncertain or meta.get('frozen') or meta.get('authorization_sha256')!=self.ctx['runtime_authority']['sha256'] or json.loads(row['response'])!=response:raise ValueError('successful known complete HTTP ledger/response binding required; no UNKNOWN recovery')
        wire=read_json(self.root/'http'/sha_value(rid)/'request.json');payload=wire.get('payload')
        if not isinstance(payload,dict) or wire.get('request_id')!=rid or wire.get('authorization_sha256')!=self.ctx['runtime_authority']['sha256'] or payload.get('messages')!=request['messages'] or payload.get('tools')!=request['tools'] or wire.get('payload_sha256')!=sha_value(payload) or row['content_hash']!=sha_value(payload):raise ValueError('original HTTP payload/authority hash binding differs')
        binding=dict(request_id=self.ctx['request_id'],request_digest=self.ctx['request_digest'],project_id=self.ctx['project_id'],baseline_ref=self.ctx['baseline_ref'],baseline_snapshot_sha256=sha_value(self.ctx['baseline_snapshot']),displayed_digest=self.ctx['displayed_digest'],authority_sha256=self.ctx['runtime_authority']['sha256'],confirmation_sha256=sha_value(confirmation),source_ids=[x['id'] for x in self.ctx['sources']],source_response_path=str(folder/'response.json'),source_response_sha256=sha_value(response),source_stage_request_sha256=sha_value(request),source_http_request_id=rid,source_payload_sha256=sha_value(payload),original_failure_path=str(self.root/'failures'/('generate-'+str(attempt)+'.json')),original_failure_sha256=sha_value(failure))
        if normalization is not None:binding['spec_json_normalization']=normalization
        return stage,response,binding
    def _generation_revalidation_values(self,attempt,stage,response,binding):
        spec_json='spec_json_normalization' in binding
        spec,delta,checked,completion=self._validate_generation(response,stage,source_references=not spec_json)
        audit=dict(schema='v12-generation-source-reference-migration-1',**binding,attempt=attempt,projection='ONLY_SPEC_SOURCES_TO_ORDERED_ID_REFERENCES',source_text_lossless_normalization=False,non_source_reply_fields_changed=False,normalized_spec_sha256=checked['spec_digest'],baseline_completion_sha256=sha_value(completion) if completion else None,product_http_added=0,mcp_calls_added=0,compile_added=0,simulation_added=0,software_repair_counter_changed=False)
        suffix='source-references'
        if spec_json:
            audit.pop('non_source_reply_fields_changed')
            audit.update(schema='v12-generation-spec-json-migration-1',projection='ONLY_SPEC_JSON_STRING_TO_OBJECT',source_reference_projection=False,other_argument_values_unchanged=True,source_text_values_unchanged=True,product_output_repair_added=0)
            suffix='spec-json'
        return spec,delta,checked,completion,audit,self.root/'revalidations'/('generate-'+str(attempt)+'-'+suffix+'.json')
    def revalidate_generation(self,attempt=0,*,expected_spec_digest=None,expected_baseline_digest=None):
        """Explicit protocol migration of one complete known reply; zero HTTP/MCP."""
        emit_progress('validation','正在离线复验已知生成回复；不调用模型或执行工具。')
        stage,response,binding=self._revalidation_evidence(attempt)
        spec,delta,checked,completion,audit,audit_path=self._generation_revalidation_values(attempt,stage,response,binding)
        if expected_spec_digest is not None and checked['spec_digest']!=expected_spec_digest:raise ValueError('revalidated candidate differs from expected spec digest')
        if expected_baseline_digest is not None and (completion or {}).get('completed_spec_sha256')!=expected_baseline_digest:raise ValueError('revalidated original baseline differs from expected completed spec digest')
        saved=dict(schema='v12-revalidated-generation-candidate-1',stage='VALIDATED_CANDIDATE_PENDING_PROPOSAL',request_id=self.ctx['request_id'],request_digest=self.ctx['request_digest'],displayed_digest=self.ctx['displayed_digest'],attempt=attempt,spec=spec,spec_digest=checked['spec_digest'],delta=delta,baseline_completion_sha256=audit['baseline_completion_sha256'],audit_path=str(audit_path),audit_sha256=sha_value(audit),product_generation_success=False,proposal_prepared=False,product_http_added=0)
        # All evidence is checked before append-only writes; originals stay intact.
        for path,value in [(audit_path,audit),(self.root/'generation-revalidated.json',saved)]+([(self.root/'baseline-sysml-completion.json',completion)] if completion else []):
            if path.exists() and read_json(path)!=value:raise ValueError('existing revalidation evidence differs; no overwrite')
        if completion:save_json(self.root/'baseline-sysml-completion.json',completion,immutable=True)
        save_json(audit_path,audit,immutable=True);save_json(self.root/'generation-revalidated.json',saved,immutable=True)
        return saved
    def _read_revalidated_generation(self):
        saved=read_json(self.root/'generation-revalidated.json');stage,response,binding=self._revalidation_evidence(saved['attempt'])
        spec,delta,checked,completion,expected_audit,audit_path=self._generation_revalidation_values(saved['attempt'],stage,response,binding)
        if saved.get('audit_path')!=str(audit_path):raise ValueError('revalidation audit path differs')
        audit=read_json(audit_path)
        if audit!=expected_audit or saved.get('audit_sha256')!=sha_value(audit):raise ValueError('revalidation audit binding differs')
        if saved.get('schema')!='v12-revalidated-generation-candidate-1' or any(saved.get(k)!=self.ctx[k] for k in ('request_id','request_digest','displayed_digest')) or saved.get('spec')!=spec or saved.get('spec_digest')!=checked['spec_digest'] or audit.get('normalized_spec_sha256')!=checked['spec_digest'] or saved.get('delta')!=delta or saved.get('baseline_completion_sha256')!=(sha_value(completion) if completion else None):raise ValueError('revalidated candidate changed outside source reference or spec JSON projection')
        if completion and read_json(self.root/'baseline-sysml-completion.json')!=completion:raise ValueError('revalidated baseline SysML sidecar binding differs')
        return spec,delta,checked,completion
    async def generate(self,attempt=0):
        from .case_requests import require_execution
        self.refresh();require_execution(self.ctx['request_id'])
        from .case_local_proposal_recovery import preflight,cached
        preflight(self)
        receipt=self.root/'generation.json'
        if receipt.exists():
            baseline_sysml_for_context(self.ctx)
            saved=read_json(receipt);proposal=self.open(read_only=True).store.proposal(saved['proposal_id'])
            if proposal['spec_digest']!=saved['spec_digest']:raise ValueError('stored proposal binding changed')
            audit=self._parameter_edit_provenance(proposal['spec'])
            if saved.get('origin')=='TOOL_APPLIED_CONFIRMED_PRODUCT_PLAN' and not audit:raise ValueError('stored parameter tool provenance missing')
            if audit and saved.get('parameter_edit_provenance_sha256')!=sha_value(audit):raise ValueError('stored parameter tool provenance binding changed')
            return saved
        if (self.root/'validated-generation-candidate.json').exists():
            return await self._prepare_generation(*cached(self))
        if (self.root/'generation-revalidated.json').exists():
            spec,delta,checked,completion=self._read_revalidated_generation()
            return await self._prepare_generation(spec,delta,checked,completion)
        if (self.root/'contact-source-review-pending.json').exists():
            pending,response=pending_response(self.ctx)
            try:spec,delta,checked,completion=self._validate_generation(response,pending['stage'])
            except ContactSourceReviewRequired as exc:return save_pending(self.ctx,response,pending['stage'],exc.spec,exc.completion)
            if spec!=pending['spec'] or completion!=pending['baseline_completion']:raise ValueError('reviewed candidate changed during zero-HTTP readback')
            return await self._prepare_generation(spec,delta,checked,completion)
        from .case_budget import CaseBudget
        CaseBudget(self.ctx['runtime_authority']['ledger_path'],task_context=self.ctx).consume('candidate','request_candidate_'+self.ctx['request_id'],dict(request_id=self.ctx['request_id'],displayed_digest=self.ctx['displayed_digest']))
        base=self.ctx['baseline_snapshot']['frozen_spec'];plan=self.ctx['plan'];validate_import_plan(self.ctx,plan)
        from .case_parameter_edit import edit_mode,apply_confirmed_plan
        if edit_mode(self.ctx) is not None:
            emit_progress('generate','正在由工具应用已确认产品方案，同步完整SysML与Modelica。',attempt=attempt,origin='TOOL_APPLIED_CONFIRMED_PRODUCT_PLAN')
            spec,audit=apply_confirmed_plan(self.ctx)
            spec,delta,checked,completion=self._validate_generation(None,'parameter_tool',needs_baseline=False,local_spec=spec)
            if checked['spec_digest']!=audit['candidate_spec_digest']:raise ValueError('parameter tool candidate changed during ordinary validation')
            save_json(self.root/'parameter-edit-provenance.json',audit,immutable=True)
            return await self._prepare_generation(spec,delta,checked,completion)
        contract={k:deepcopy(base[k]) for k in ('schema_version','backend','libraries','settings','outputs','checks')}
        contract.update(baseline_ref=self.ctx['baseline_ref'],sources=[dict(id=x['id']) for x in self.ctx['sources']],requirements=plan['requirements'],design=plan['design'],model_name=base['artifact']['model_name'])
        if 'trajectory_contract' in base:contract['trajectory_contract']=base['trajectory_contract']
        context=dict(raw_request=self.ctx['raw_request'],baseline_snapshot=self.ctx['baseline_snapshot'],confirmed_plan=plan,trusted_generation_contract=contract,source_delta_contract=validate_request_plan(plan,base,self.ctx['sources'],task_context=self.ctx),sysml_contract=module('artifact_sysml').CONTRACT_PROMPT,verified_source_facts=self._plan_source_facts())
        needs_baseline=not baseline_sysml_available(self.ctx);tool=deepcopy(GEN_TOOL);prompt=GEN_PROMPT
        from .case_asym2_family import is_asym2_source,context_contract,SYSML_GUIDE,GEN_PROMPT as ASYM2_GEN_PROMPT
        if is_asym2_source(base['artifact']['modelica_source']):
            prompt=ASYM2_GEN_PROMPT
            context['asym2_family_contract']=context_contract(base['artifact']['modelica_source'],plan['proposed_parameters'])
            context['sysml_contract']=module('artifact_sysml').CONTRACT_PROMPT+'\n'+SYSML_GUIDE
            tool['function']['parameters']['properties']['spec']['properties']['settings']['properties']['intervals']['maximum']=30000
        if 'trajectory_contract' in base:
            tool['function']['parameters']['properties']['spec']['properties']['trajectory_contract']=dict(type='object',enum=[deepcopy(base['trajectory_contract'])])
            tool['function']['parameters']['properties']['spec']['required'].append('trajectory_contract')
        if is_contact_scope(plan['scope']):
            from .case_contact_family import GEN_PROMPT as CONTACT_GEN_PROMPT
            prompt=CONTACT_GEN_PROMPT;context['contact_repair_authority']=planning_context(self.ctx)
            if context['contact_repair_authority'].get('numerical_only'):
                from .case_contact_numerics import GEN_PROMPT as NUMERIC_GEN_PROMPT
                prompt=NUMERIC_GEN_PROMPT
        if needs_baseline:
            tool['function']['parameters']['properties']['baseline_sysml_source']=dict(type='string')
            tool['function']['parameters']['required'].append('baseline_sysml_source')
            prompt+='\n原基准缺少SysML，本次同一调用必须返回arguments={"spec":{完整候选},"baseline_sysml_source":"完整原基准SysML"}。原基准SysML必须与baseline_snapshot.frozen_spec原始Modelica/全部共享参数/requirements原文及来源映射一致；不能把本次新要求写进原基准，不能改原基准Modelica或元数据。候选SysML独立映射已确认新要求。'
            context['original_baseline_completion']=dict(required=True,requirements=base['requirements'],sources=base['sources'],design=base['design'],original_snapshot_must_remain_immutable=True)
        else:
            prompt+='\n已核实原基准已有有效SysML本体或可信来源绑定侧证。侧证复用不表示v0原件本来包含SysML。已保存方案原文若仍称需重新补齐，该说明属于历史缺项，不改变已确认参数、requirements或design原文。本次只同步生成候选双源码，arguments恰为{"spec":{完整候选}}，不得额外返回baseline_sysml_source。'
            context['original_baseline_completion']=dict(required=False,selected_baseline_sysml_present=bool(base['artifact'].get('sysml_source')),validated_sidecar_reference=(str(self.root/'baseline-sysml-reference.json') if (self.root/'baseline-sysml-reference.json').exists() else None),original_snapshot_must_remain_immutable=True)
        if attempt:context['bounded_protocol_repair']=self.feedback('generate',attempt)
        if transport_profile(__import__('week13.src.case_requests',fromlist=['authority_for']).authority_for(self.ctx)['model_controls'])['strict_tools']:
            tool=generation_tool(tool,contract,needs_baseline=needs_baseline)
        emit_progress('generate','正在等待产品模型生成完整双源码。',attempt=attempt)
        response=await self.call('generate' if not attempt else 'generate_repair_'+str(attempt),[dict(role='system',content=prompt),dict(role='user',content=json.dumps(context,ensure_ascii=False))],[tool])
        try:
            spec,delta,checked,completion=self._validate_generation(response,'generate' if not attempt else 'generate_repair_'+str(attempt))
        except ContactSourceReviewRequired as exc:
            return save_pending(self.ctx,response,'generate' if not attempt else 'generate_repair_'+str(attempt),exc.spec,exc.completion)
        except (ValueError,TypeError,KeyError) as exc:self.failure('generate',attempt,exc,response);raise
        _,normalization=generation_arguments(response,needs_baseline=needs_baseline)
        if normalization is not None:save_json(self.root/'normalizations'/('generate-'+str(attempt)+'-spec-json.json'),normalization,immutable=True)
        return await self._prepare_generation(spec,delta,checked,completion)
    async def recover_local_proposal(self,approval_path):
        from .case_local_proposal_recovery import recover
        return await recover(self,approval_path)
    def _parameter_edit_provenance(self,spec):
        path=self.root/'parameter-edit-provenance.json'
        if not path.exists():return None
        from . import case_parameter_edit as tool
        audit=read_json(path)
        expected=dict(schema='v12-confirmed-parameter-tool-1',origin=tool.ORIGIN,tool_source_sha256=sha_file(tool.__file__),request_id=self.ctx['request_id'],request_digest=self.ctx['request_digest'],displayed_digest=self.ctx['displayed_digest'],baseline_ref=self.ctx['baseline_ref'],baseline_snapshot_sha256=sha_value(self.ctx['baseline_snapshot']),confirmed_product_plan_sha256=sha_value(self.ctx['plan']),candidate_spec_digest=sha_value(spec),candidate_source_sha256=__import__('hashlib').sha256(spec['artifact']['modelica_source'].encode()).hexdigest(),candidate_sysml_sha256=__import__('hashlib').sha256(spec['artifact']['sysml_source'].encode()).hexdigest(),product_generated_source=False,product_http_added=0,execution_added=0)
        if any(audit.get(key)!=value for key,value in expected.items()):raise ValueError('parameter tool provenance/current candidate binding drift')
        return audit
    async def _prepare_generation(self,spec,delta,checked,completion,*,local_recovery=None):
        from .case_local_proposal_recovery import cache,preflight
        audit=self._parameter_edit_provenance(spec)
        cache(self,spec,delta,checked,completion)
        if completion:save_json(self.root/'baseline-sysml-completion.json',completion,immutable=True)
        baseline_sysml_for_context(self.ctx)
        from .case_budget import CaseBudget
        CaseBudget(self.ctx['runtime_authority']['ledger_path'],task_context=self.ctx).mark_valid_generated_source(checked['spec_digest'])
        preflight(self)
        emit_progress('proposal','双源码检查完成，正在通过MCP保存提案并读回。')
        svc=self.open();receipt=self.root/'generation.json'
        # Read persisted state before any resend after a lost RPC. A matching
        # proposal is read back; absence alone never clears UNKNOWN.
        marker=self.root/'proposal-unknown.json'
        with svc.store.reading() as db:rows=db.execute('SELECT proposal_id,spec_json FROM proposals WHERE session_id=? AND project_id=? AND spec_digest=?',(svc.session_id,self.ctx['project_id'],checked['spec_digest'])).fetchall()
        exact=[x['proposal_id'] for x in rows if json.loads(x['spec_json'])==spec]
        if len(exact)>1:raise ValueError('ambiguous persisted proposal readback')
        if marker.exists() and not exact:
            if local_recovery is None:raise ValueError('proposal mutation UNKNOWN; no blind resend')
            intent=self.root/'local-proposal-recovery-intent.json';dispatch=self.root/'local-proposal-recovery-dispatch.json'
            if not intent.exists() or read_json(intent)!=local_recovery or local_recovery.get('spec_digest')!=checked['spec_digest'] or local_recovery.get('original_marker_sha256')!=sha_file(marker):raise ValueError('local recovery intent/candidate/marker differs')
            if dispatch.exists():raise ValueError('local recovery dispatch already attempted; no blind resend')
            try:
                with dispatch.open('x',encoding='utf8') as handle:
                    json.dump(dict(intent_sha256=sha_value(local_recovery),spec_digest=checked['spec_digest'],status='DISPATCH_ATTEMPT_RESERVED_ONCE'),handle,ensure_ascii=False,indent=2)
                    handle.write('\n')
            except FileExistsError:raise ValueError('local recovery dispatch already claimed; no blind resend') from None
        try:
            result=await self.mcp('proposal_get',dict(proposal_id=exact[0])) if exact else await self.mcp('proposal_prepare',dict(spec=spec))
            proposal=result['proposal'];readback=await self.mcp('proposal_get',dict(proposal_id=proposal['proposal_id']))
        except BaseException as exc:
            if not marker.exists():save_json(marker,dict(spec_digest=checked['spec_digest'],status='UNKNOWN',**exception_record(exc)),immutable=True)
            if local_recovery is not None:save_json(self.root/'local-proposal-recovery-failure.json',dict(spec_digest=checked['spec_digest'],status='MUTATION_UNKNOWN_NO_BLIND_RESEND',**exception_record(exc),intent_sha256=sha_value(local_recovery),original_marker_preserved=True),immutable=True)
            raise
        result=dict(proposal_id=proposal['proposal_id'],spec_digest=checked['spec_digest'],delta=delta,transport='REAL_STDIO_MCP' if not self.rpc else 'ISOLATED_FIXTURE',readback=readback)
        if audit:result.update(origin=audit['origin'],product_generated_source=False,product_http_added=0,parameter_edit_provenance_path=str(self.root/'parameter-edit-provenance.json'),parameter_edit_provenance_sha256=sha_value(audit))
        if completion:result['baseline_completion_sha256']=sha_value(completion)
        save_json(receipt,result,immutable=True);return result
    def state(self,*,verify_current=False):
        emit_progress('readback','正在读取当前请求的版本索引。',request_id=self.ctx['request_id'])
        svc=self.open(read_only=True);items=svc.versions_index()['versions']['items']
        def brief(row):
            value=current_summary(row)
            if row.get('record_kind')=='ARTIFACT_EXECUTION_INDEX':
                value.update(verification_status='NOT_RECHECKED',recorded_conclusions_verified=False,
                    parameters=deepcopy(row.get('parameters',{})),updated_at=row.get('updated_at'),
                    **{key:row.get(key,'UNKNOWN') for key in ('recorded_tool_status','recorded_chain_status','recorded_task_status')})
            return value
        def short(row):
            value=brief(row);value.pop('current_contact_review',None);return value
        review=self.ctx.get('access_mode')=='RESULT_REVIEW_ONLY'
        own=[] if review else [x for x in items if x.get('session_id')==self.ctx['session_id']]
        current={}
        for row in ([own[-1]] if own else [])+([next((x for x in items if x['version_id']==self.ctx['baseline_version_id']),None)] if review else []):
            if row:current[row['version_id']]=self._selected_snapshot(row['version_id'],svc=svc,index=row) if verify_current and row.get('state') in ('succeeded','failed','cancelled','uncertain') else row
        items=[current.get(x['version_id'],x) for x in items]
        own=[current.get(x['version_id'],x) for x in own]
        valid=[x for x in current.values() if brief(x).get('chain_status')=='PASS' and brief(x).get('task_status')=='PASS']
        failures=sorted((self.root/'failures').glob('*.json'),key=lambda p:p.stat().st_mtime_ns)
        last_failure=None
        if failures:
            failure=read_json(failures[-1]);resolved=(failure['stage']=='plan' and self.ctx.get('plan') is not None) or (failure['stage']=='generate' and (self.root/'generation.json').exists())
            last_failure={k:failure.get(k) for k in ('stage','attempt','error','repairable')};last_failure.update(evidence_path=str(failures[-1]),status='RESOLVED_WITH_LATER_VALIDATION' if resolved else 'UNRESOLVED')
        marker=self.root/'proposal-unknown.json';unknown=None
        if marker.exists():
            value=read_json(marker);unknown=dict(stage='proposal_prepare',error=value.get('error'),spec_digest=value.get('spec_digest'),evidence_path=str(marker),status='RECOVERED_BY_EXACT_READBACK' if (self.root/'generation.json').exists() else 'UNKNOWN_NO_BLIND_RESEND')
            if (self.root/'local-proposal-recovery-failure.json').exists() and not (self.root/'generation.json').exists():
                failed=read_json(self.root/'local-proposal-recovery-failure.json');unknown.update(error=failed.get('error'),status=failed['status'],recovery_evidence_path=str(self.root/'local-proposal-recovery-failure.json'))
        recorded_passes=[x for x in items if x.get('recorded_task_status',x.get('task_status'))=='PASS']
        return dict(request_id=self.ctx['request_id'],project_id=self.ctx['project_id'],baseline_ref=self.ctx['baseline_ref'],baseline_sysml_completion=baseline_completion_status(self.ctx),stage=self.ctx.get('stage'),plan=self.ctx.get('plan'),displayed_digest=self.ctx.get('displayed_digest'),latest_request_execution=brief(own[-1]) if own else None,reviewed_execution=next((brief(x) for x in items if x['version_id']==self.ctx['baseline_version_id']),None) if review else None,origin_request_id=self.ctx.get('review_binding',{}).get('origin_request_id'),latest_project_execution=short(items[-1]) if items else None,last_task_pass=short(valid[-1]) if valid else None,last_task_pass_scope='CURRENT_OR_SELECTED_REVIEWED_VERSIONS_ONLY',historical_last_task_pass_record=short(recorded_passes[-1]) if recorded_passes else None,versions=[short(x) for x in items],last_failure=last_failure,unknown_marker=unknown,http_state=self.http_state())

    def _selected_snapshot(self,version_id,*,svc=None,index=None,snapshot=None):
        """Reuse one verified immutable version during this command only.

        A lightweight index is never a verified result. Its state, identity and
        update marker invalidate this operation's copy when the task changes.
        No snapshot survives a new TaskFlow/CLI operation.
        """
        cache=getattr(self,'_verified_snapshots',{})
        existing=cache.get(version_id)
        keys=('project_id','task_id','version_id','state','updated_at','spec_digest','source_sha256','manifest_sha256')
        if snapshot is None and existing is not None and (index is None or all(index.get(k) is None or index.get(k)==existing.get(k) for k in keys)):
            return deepcopy(existing)
        svc=svc or self.open(read_only=True)
        actual=deepcopy(snapshot) if snapshot is not None else svc._artifact_version(version_id)
        if actual.get('version_id')!=version_id or actual.get('project_id')!=self.ctx['project_id']:raise ValueError('selected snapshot project/version differs')
        if index and any(index.get(k) is not None and index.get(k)!=actual.get(k) for k in keys):raise ValueError('selected snapshot changed after index read')
        if not actual.get('current_contact_review'):actual=with_review(actual,svc.project_root)
        cache[version_id]=deepcopy(actual);self._verified_snapshots=cache
        return actual
    def review_evidence(self):
        self.refresh();binding=self.ctx['review_binding'];svc=self.open(read_only=True);snapshot=self._selected_snapshot(self.ctx['baseline_version_id'],svc=svc)
        if module('artifact_baseline').reference(snapshot)!=binding['reference'] or snapshot['frozen_spec']!=self.ctx['baseline_snapshot']['frozen_spec']:raise ValueError('existing result review binding drift')
        from .case_requests import load_request
        origin=load_request(binding['origin_request_id'])
        if origin['request_digest']!=binding['origin_request_digest']:raise ValueError('original request binding drift')
        outcome=snapshot.get('outcome',{});mission=outcome.get('mission_assessment',{});prior=origin['baseline_snapshot']['frozen_spec'];current=snapshot['frozen_spec'];before=modelica_parameters(prior['artifact']['modelica_source'])[0];after=modelica_parameters(current['artifact']['modelica_source'])[0]
        old=read_json(binding['original_partial_path'])
        summary=current_summary(with_review(snapshot,svc.project_root))
        return dict(**summary,origin_request_id=binding['origin_request_id'],reference=binding['reference'],review_scope='READ_EXISTING_BOUND_EXECUTION_NOT_A_NEW_PHYSICAL_EXECUTION',original_requirement=origin['raw_request'],requirements=current['requirements'],design=current['design'],parameter_changes={key:dict(before=before.get(key),after=value) for key,value in after.items() if before.get(key)!=value},actual_parameters=after,mission=summary['current_contact_review'].get('current_assessment',{}),historical_mission=mission_summary(mission),spatial_event_count=len(mission.get('events',[])),terrain_event_count=len(mission.get('terrain_events',[])),runtime_parameter_readback=outcome.get('runtime_parameter_readback'),original_explanation=dict(role='HISTORICAL_INTERPRETATION_NOT_CURRENT_REVIEW',record=old.get('explanation')),source_files=snapshot.get('source_files'),data_files=snapshot.get('data_files'),curve_files=snapshot.get('curve_files'),physical_acceptance_status=snapshot.get('physical_acceptance_status'),historical_baseline_task_status=origin['baseline_snapshot']['task_status'])
    def _explanation_source_references(self,binding,raw_answer=''):
        """Include a readonly review's verified origin baseline as source only."""
        references=baseline_source_references(self.ctx)
        # The imported v0 is a source identity, not an execution. Resolve only
        # an explicitly mentioned original of this selected case/project.
        if __import__('re').search(r'(?<![A-Za-z0-9_-])v0+(?![A-Za-z0-9_-])',raw_answer) and self.ctx.get('baseline_version_id')!='v000000' and (Path(self.ctx['project_root'])/'case.json').is_file():
            original=self._selected_snapshot('v000000')
            if original.get('record_kind')!='IMPORTED_REFERENCE':raise ValueError('original source reference is not an imported source')
            source_ctx=dict(self.ctx,baseline_version_id='v000000',baseline_snapshot=original,
                baseline_ref=module('artifact_baseline').reference(original),baseline_snapshot_sha256=sha_value(original))
            for source in baseline_source_references(source_ctx):
                if source not in references:references.append(source)
        if self.ctx.get('access_mode')!='RESULT_REVIEW_ONLY':return references
        from .case_requests import load_request
        review=self.ctx['review_binding'];reference=review['reference']
        row=binding.get('reviewed_execution') or {}
        if not references or reference!=self.ctx['baseline_ref'] or binding.get('project_id')!=self.ctx['project_id'] or binding.get('request_id')!=self.ctx['request_id'] or any(row.get(key)!=value for key,value in reference.items()):
            raise ValueError('review source reference/current execution binding differs')
        origin=load_request(review['origin_request_id'])
        if origin['request_id']!=review['origin_request_id'] or origin['request_digest']!=review['origin_request_digest'] or origin['session_id']!=review['origin_session_id']:
            raise ValueError('review origin request digest/session binding differs')
        if origin['project_id']!=self.ctx['project_id'] or any(Path(origin[key]).resolve()!=Path(self.ctx[key]).resolve() for key in ('db_path','project_root')):
            raise ValueError('review origin project/store binding differs')
        original_references=baseline_source_references(origin)
        if not original_references:raise ValueError('review origin baseline source binding missing')
        for source in original_references:
            if source not in references:references.append(source)
        return references
    def _protect_explanation(self,raw_answer,binding):
        """Read only explicitly named project versions, never trust body claims.

        Current request facts are already in state. Other named versions are
        looked up through the existing bound ArtifactService, not user text or
        copied historical narration. At most eight versions are examined.
        """
        binding=deepcopy(binding);extra=[]
        try:source_references=self._explanation_source_references(binding,raw_answer)
        except (OSError,ValueError,KeyError,TypeError) as exc:
            source_references=[];binding['source_reference_binding_error']=str(exc)
        for key in ('latest_request_execution','reviewed_execution'):
            row=binding.get(key)
            if not isinstance(row,dict) or not row.get('version_id'):continue
            try:
                snapshot=self._selected_snapshot(row['version_id'])
                if snapshot.get('project_id')!=binding.get('project_id'):raise ValueError('explanation project identity differs')
                if snapshot.get('record_kind')=='IMPORTED_REFERENCE':
                    binding[key]=None
                    continue
                actual=current_summary(dict(snapshot,current_contact_review=row.get('current_contact_review')))
                actual['parameters']=modelica_parameters(snapshot['frozen_spec']['artifact']['modelica_source'])[0]
                binding[key]=actual
            except (OSError,ValueError,KeyError,TypeError,AttributeError):pass
        known={row.get('version_id') for row in (binding.get('latest_request_execution'),binding.get('reviewed_execution')) if isinstance(row,dict)}
        named_versions={'v'+str(int(token[1:])).zfill(6) for token in __import__('re').findall(r'(?<![A-Za-z0-9_-])v\d+(?![A-Za-z0-9_-])',raw_answer)}
        for row in binding.get('versions',[])[:64]:
            version=row.get('version_id')
            if not version or version in known or version not in named_versions:continue
            try:
                snapshot=self._selected_snapshot(version)
                if snapshot.get('project_id')!=binding.get('project_id'):continue
                if snapshot.get('record_kind')=='IMPORTED_REFERENCE':continue
                actual=current_summary(snapshot)
                actual['parameters']=modelica_parameters(snapshot['frozen_spec']['artifact']['modelica_source'])[0]
                extra.append(actual)
            except (OSError,ValueError,KeyError,TypeError,AttributeError):continue
            if len(extra)>=8:break
        fact_binding={key:deepcopy(binding.get(key)) for key in ('request_id','project_id','latest_request_execution','reviewed_execution','source_reference_binding_error')}
        fact_binding['explicitly_reviewed_executions']=extra
        return dict(**protect_explanation(raw_answer,fact_binding,source_references=source_references),current_fact_binding=fact_binding,current_source_references=source_references)
    async def ask_review(self,question,*,reopen=False):
        self.refresh();expected='reopen' if reopen else 'explain'
        if self.ctx['step_id']!=expected:raise PermissionError('readonly review command/step differs')
        evidence=self.review_evidence();before=self.state(verify_current=True);context=dict(bound_existing_execution=evidence,new_review_state=before,review_is_not_original_reply_recovery=True)
        from .case_stage_facts import read_stage_facts
        context['stage_facts']=read_stage_facts(self.ctx)
        context['verified_source_facts']=selected_source_facts(self.ctx)
        if reopen:
            from .case_requests import authority_for,load_request
            authority=authority_for(self.ctx);first=load_request(authority['operation_policy']['steps']['explain']);response=Path(first['root'])/'turns/explain_0/response.json'
            context['prior_new_explanation']=dict(status='COMPLETE_SAVED_PRODUCT_RESPONSE',answer=read_json(response)['content']) if response.exists() else dict(status='NO_COMPLETE_NEW_EXPLANATION_READ_EXISTING_BOUND_EXECUTION_INDEPENDENTLY',http_state=TaskFlow.load(first['request_id']).http_state())
        messages=[dict(role='system',content='用中文对明确绑定的既有结果解释或复核。你没有改模、生成、确认或执行工具；不是新物理执行，也不是恢复原超时回复。逐项区分历史基线、绑定版本实际任务、原解释状态、新解释状态与实物未知。保留简化模型、软接触压缩、有限采样及无真实机电/碰撞认证等限制；时间只报告，不造业务时限。用所给实际参数、checker/CSV/源码/manifest引用，不能编造结果。明确阶段事实stage_facts优先于历史解释；plan失败不能混称双源码生成/编译/仿真失败，已解决旧失败仅作历史。'),dict(role='user',content=json.dumps(context,ensure_ascii=False)),dict(role='user',content=question)]
        from .case_contact_review import rule_for_spec
        messages[0]['content']+='\n'+rule_for_spec(self.ctx['baseline_snapshot']['frozen_spec'])
        emit_progress('explain','正在等待产品模型说明已绑定的既有结果。',request_id=self.ctx['request_id'])
        stage='reopen_0' if reopen else 'explain_0';response=await self.call(stage,messages,[])
        if response.get('tool_calls') or not response.get('content','').strip():raise ValueError('readonly result review requires nonempty text without tool calls')
        after=self.state(verify_current=True);binding=lambda x:{key:value for key,value in x.items() if key!='http_state'}
        if binding(before)!=binding(after):raise ValueError('readonly result review changed model/task state')
        emit_progress('fact_validation','正在核对既有结果说明的来源和事实。',request_id=self.ctx['request_id'])
        result=dict(**self._protect_explanation(response['content'],before),binding=before,current_contact_review=(before.get('reviewed_execution') or {}).get('current_contact_review'),origin_request_id=self.ctx['review_binding']['origin_request_id'],reopened=reopen,read_only_status_unchanged=True,new_physical_execution=False,request_id='case_'+stage+'_'+self.ctx['request_id'])
        save_json(self.root/'review-answer.json',result,immutable=True);return result
    def terminal_readback(self,*,error=None,bound_state=None,concise=False):
        """Return already obtained execution independently of product narration.

        This never calls a model, generates sources or starts an execution.
        A persisted partial receipt is deliberately not resumed automatically.
        """
        if concise:
            from .case_saved_assessment import concise_result
            return concise_result(self)
        repaired=self._repaired_pipeline()
        if repaired is not None:return self._terminal_explanation_overlay(dict(reused=True,pipeline=repaired,state=self.state(verify_current=True),product_http_added=0,execution_added=0))
        completed=self.root/'pipeline.json'
        if completed.exists():return self._terminal_explanation_overlay(dict(reused=True,pipeline=read_json(completed),state=self.state(verify_current=True),product_http_added=0,execution_added=0))
        path=self.root/'pipeline-partial.json'
        if path.exists():return self._terminal_explanation_overlay(dict(reused=True,partial=read_json(path),state=self.state(verify_current=True),product_http_added=0,execution_added=0),error=error)
        state=bound_state if bound_state is not None else self.state(verify_current=True)
        if state.get('request_id')!=self.ctx['request_id'] or state.get('project_id')!=self.ctx['project_id']:raise ValueError('terminal readback current request/project differs')
        latest=state['latest_request_execution']
        if not latest or latest['state'] not in ('succeeded','failed','cancelled','uncertain'):return None
        svc=self.open(read_only=True);task=svc.store.task(latest['task_id']);snapshot=self._selected_snapshot(latest['version_id'],svc=svc);outcome=snapshot.get('outcome',{})
        candidates=[]
        for request in (self.root/'turns').glob('explain_*/request.json'):
            record=read_json(request);rid=record['request_id'];receipt=None
            http=state.get('http_state') or {}
            for row in http.get('unknown_requests',[]):
                if row['request_id']==rid:receipt=row
            location=self.root/'http'/sha_value(rid)
            transport_error=read_json(location/'transport-error.json').get('error_type') if (location/'transport-error.json').exists() else None
            status='UNKNOWN' if receipt else 'FAILED' if error else 'NOT_COMPLETED'
            if (request.parent/'response.json').exists():status='SAVED_RESPONSE_READBACK_NOT_PIPELINE_COMPLETION'
            candidates.append(dict(request_id=rid,status=status,error=error or (receipt or {}).get('reason'),transport_error=transport_error,request_evidence=str(request),transport_error_evidence=str(location/'transport-error.json') if (location/'transport-error.json').exists() else None,progress_evidence=str(location/'progress.json') if (location/'progress.json').exists() else None,usage_known=False if receipt else None))
        explanation=candidates[-1] if candidates else dict(status='NOT_RUN',error=error)
        mission=outcome.get('mission_assessment',{})
        execution=dict(latest,parameters=modelica_parameters(snapshot['frozen_spec']['artifact']['modelica_source'])[0],mission=deepcopy(latest.get('current_contact_review',{}).get('current_assessment') or mission_summary(mission)),historical_mission=mission_summary(mission),runtime_parameter_readback=outcome.get('runtime_parameter_readback'),test_only=outcome.get('test_only'),physical_acceptance_status=snapshot.get('physical_acceptance_status'),source_files=snapshot.get('source_files'),data_files=snapshot.get('data_files'),curve_files=snapshot.get('curve_files'),outcome_manifest_path=(task.get('result') or {}).get('outcome_manifest_path'))
        result=dict(schema='v12-request-partial-result-1',stage='EXECUTION_TERMINAL_EXPLANATION_INCOMPLETE',request_id=self.ctx['request_id'],execution=execution,explanation=explanation,execution_evidence=str(self.root/('execution-'+latest['task_id']+'.json')),state=state,product_explanation_success=False,product_http_added_by_readback=0,execution_added_by_readback=0)
        save_json(path,result,immutable=True);return self._terminal_explanation_overlay(result)

    def _terminal_explanation_overlay(self,value,*,error=None):
        """Derive narration availability without overwriting any saved pipeline."""
        result=deepcopy(value);state=result.get('state') or self.state(verify_current=True);latest=state.get('latest_request_execution') or {}
        if not latest:return result
        candidates=[];attempts=[]
        for path in sorted((self.root/'turns').glob('explain_*/request.json'),key=lambda p:p.stat().st_mtime_ns):
            request=read_json(path)
            if request.get('request_digest')==self.ctx['request_digest']:attempts.append((path,request))
            response_path=path.parent/'response.json'
            if not response_path.exists():continue
            response=read_json(response_path)
            if request.get('request_digest')!=self.ctx['request_digest'] or response.get('finish_reason')=='length' or response.get('tool_calls') or not response.get('content','').strip():continue
            try:
                context=json.loads(request['messages'][1]['content']);actual=context['actual_request_execution']
                if any(actual.get(k)!=latest.get(k) for k in ('version_id','task_id')):continue
                with closing(sqlite3.connect(Path(self.ctx['runtime_authority']['ledger_path']).resolve().as_uri()+'?mode=ro',uri=True)) as db:
                    db.row_factory=sqlite3.Row;row=db.execute('SELECT * FROM requests WHERE request_id=?',(request['request_id'],)).fetchone()
                if not row or row['status']!='succeeded' or json.loads(row['response'])!=response or any(row[k] is None for k in ('actual_tokens','actual_output','actual_cost','finished_at')):continue
            except (OSError,ValueError,KeyError,TypeError):continue
            candidates.append(dict(status='COMPLETE_SAVED_PRODUCT_RESPONSE',**self._protect_explanation(response['content'],state),request_id=request['request_id'],request_evidence=str(path),response_evidence=str(response_path),response_sha256=sha_file(response_path),binding=dict(version_id=latest['version_id'],task_id=latest['task_id']),usage_known=True,pipeline_completion_claimed=False))
        if candidates:
            head=result.get('partial',result.get('pipeline',result));head['historical_explanation']=deepcopy(head.get('explanation'));head['explanation']=candidates[-1]
            head['current_explanation_success']=True;head['read_only_explanation_overlay']=True
            if head.get('stage')=='EXECUTION_TERMINAL_EXPLANATION_INCOMPLETE':
                head['historical_saved_stage']=head['stage'];head['stage']='EXECUTION_TERMINAL_EXPLANATION_AVAILABLE'
        elif attempts:
            # The early immutable checkpoint can precede the narration call.
            # Its NOT_RUN is history once a real attempt exists; derive the
            # current status without rewriting that checkpoint or resending.
            path,request=attempts[-1];rid=request['request_id']
            unknown=next((row for row in (state.get('http_state') or {}).get('unknown_requests',[]) if row.get('request_id')==rid),None)
            head=result.get('partial',result.get('pipeline',result))
            head['historical_explanation']=deepcopy(head.get('explanation'))
            head['explanation']=dict(status='UNKNOWN' if unknown else 'FAILED' if error else 'NOT_COMPLETED',request_id=rid,error=error or (unknown or {}).get('reason'),request_evidence=str(path),usage_known=False if unknown else None)
            head['current_explanation_success']=False
        head=result.get('partial',result.get('pipeline',result));explanation=head.get('explanation')
        if isinstance(explanation,dict) and isinstance(explanation.get('answer'),str):
            explanation.update(self._protect_explanation(explanation.get('raw_answer',explanation['answer']),state))
            result['current_fact_binding']=explanation['current_fact_binding']
            result['current_source_references']=explanation['current_source_references']
            head['explanation_body_displayed']=explanation['explanation_validation']['body_displayed']
            if not head['explanation_body_displayed']:head['current_explanation_success']=False
        return result

    def _repaired_pipeline(self):
        """Only an explicitly completed local repair becomes the readback head."""
        pointer=self.root/'pipeline-repair-latest.json'
        if not pointer.exists():return None
        head=read_json(pointer);path=self.root/'pipeline-repair-1.json'
        receipt=read_json(self.root/'repair-execution-1.json');result=read_json(path)
        if head.get('schema')!='v12-execution-repair-readback-head-1' or head.get('request_id')!=self.ctx['request_id'] or head.get('pipeline_sha256')!=sha_file(path) or head.get('repair_receipt_sha256')!=sha_value(receipt) or result.get('repair_receipt_sha256')!=sha_value(receipt) or receipt.get('request_id')!=self.ctx['request_id']:
            raise ValueError('completed execution repair readback binding drift')
        task=(result.get('execution') or {}).get('task',{})
        if task.get('task_id')!=receipt.get('new_task_id') or task.get('version_id')!=receipt.get('new_version_id'):
            raise ValueError('completed execution repair task differs')
        snapshot=self.open(read_only=True)._artifact_version(receipt['new_version_id'])
        if snapshot['spec_digest']!=receipt['spec_digest'] or snapshot['source_sha256']!=receipt['source_sha256']:
            raise ValueError('completed execution repair source binding drift')
        return result

    @staticmethod
    def _repair_terminal_eligible(task,snapshot):
        result=task.get('result') or {};outcome=snapshot.get('outcome') or {}
        mission=outcome.get('mission_assessment') or result.get('mission_assessment') or {}
        if task.get('state')!='failed' or result.get('worker_exit_code')!=0 or result.get('chain_status')!='BLOCKED_PARAMETER_EVIDENCE' or result.get('tool_status')!='PASS' or mission.get('task_status')!='PASS' or mission.get('validity_status')!='PASS':
            raise PermissionError('repair-execution only accepts exited known parameter-evidence failure with tool/mission PASS')

    @staticmethod
    def _same_repair_candidate(proposal,spec,spec_digest):
        if proposal.get('spec')!=spec or proposal.get('spec_digest')!=spec_digest or sha_value(spec)!=spec_digest:
            raise ValueError('execution repair must preserve the identical confirmed product candidate')

    def _execution_repair_evidence(self,approval_path):
        """Read the known failure and existing authority; create no new budget."""
        from .case_requests import authority_for,require_execution,remaining_request
        self.refresh();ctx=self.ctx
        path=Path(approval_path).resolve()
        if not path.is_relative_to((ROOT/'results').resolve()) or path.is_relative_to((ROOT/'V12_old').resolve()):raise ValueError('repair approval must be a current week13 result record')
        approval=read_json(path);authority=authority_for(ctx)
        for key,expected in [('schema','v12-known-parameter-evidence-repair-1'),('request_id',ctx['request_id']),('project_id',ctx['project_id']),('runtime_authority',ctx['runtime_authority']),('baseline_ref',ctx['baseline_ref']),('displayed_digest',ctx['displayed_digest']),('confirmation_path',ctx['confirmation_path']),('hard_deadline_utc',authority['hard_deadline_utc']),('maximum_new_compile',1),('maximum_new_simulation',1),('maximum_new_physical_candidates',0),('model_cannot_authorize',True),('old_result_must_remain',True)]:
            if approval.get(key)!=expected:raise PermissionError('execution repair approval binding differs: '+key)
        package_path=Path(approval['package_authorization_path']).resolve()
        if not package_path.is_relative_to((ROOT/'results').resolve()) or sha_file(package_path)!=approval['package_authorization_sha256']:raise ValueError('repair package authorization drift')
        package=read_json(package_path)
        if package.get('approval_channel')!='USER_CHAT_APPROVAL' or package.get('approval_quote')!=approval.get('approval_quote') or package.get('hard_deadline_utc')!=approval['hard_deadline_utc']:raise PermissionError('repair lacks original user package authorization')
        if remaining_request(ctx['request_id'])<=0:raise PermissionError('execution repair shared deadline reached')
        require_execution(ctx['request_id'])
        confirmation=read_json(ctx['confirmation_path']);plan=read_json(ctx['proposal_path'])
        if plan.get('packet')!=ctx['plan'] or plan.get('sources')!=ctx['sources'] or plan.get('baseline_ref')!=ctx['baseline_ref'] or sha_value(plan)!=ctx['displayed_digest'] or confirmation.get('request_id')!=ctx['request_id']:
            raise ValueError('repair original confirmation/plan/source binding drift')
        protected=approval.get('protected_hashes')
        if not isinstance(protected,dict) or not protected:raise ValueError('repair requires protected original evidence hashes')
        for filename,expected in protected.items():
            original=Path(filename).resolve()
            if not original.is_relative_to(Path(ctx['project_root']).resolve()) or sha_file(original)!=expected:raise ValueError('repair protected original evidence drift: '+original.name)
        svc=self.open(read_only=True);task=svc.store.task(approval['previous_task_id']);snapshot=svc._artifact_version(approval['previous_version_id'])
        if task.get('session_id')!=ctx['session_id'] or task.get('project_id')!=ctx['project_id'] or task.get('proposal_id')!=approval['original_proposal_id'] or task.get('version_id')!=snapshot.get('version_id') or any(snapshot.get(key)!=approval[field] for key,field in [('task_id','previous_task_id'),('spec_digest','previous_spec_digest'),('source_sha256','previous_source_sha256'),('manifest_sha256','previous_manifest_sha256')]):raise ValueError('repair previous task/version/source binding differs')
        self._repair_terminal_eligible(task,snapshot)
        generated=read_json(self.root/'generation.json');proposal=svc.store.proposal(approval['original_proposal_id']);spec=proposal['spec'];spec_digest=approval['previous_spec_digest']
        self._same_repair_candidate(proposal,snapshot['frozen_spec'],spec_digest)
        if proposal.get('state')!='executed' or generated.get('proposal_id')!=proposal['proposal_id'] or generated.get('spec_digest')!=spec_digest or spec.get('sources')!=ctx['sources']:raise ValueError('repair original generation/candidate/source differs')
        original_confirmation=proposal.get('confirmation') or {}
        if original_confirmation.get('request_id')!=ctx['request_id'] or original_confirmation.get('request_displayed_digest')!=ctx['displayed_digest'] or original_confirmation.get('approval')!=confirmation:raise ValueError('repair original proposal confirmation differs')
        baseline_sysml_for_context(ctx);normalized,_=svc.normalize_candidate(spec)
        if normalized!=spec:raise ValueError('repair would normalize or change original candidate')
        check_delta(svc,spec,task_context=ctx)
        checked=module('model_artifact').validate_artifact(spec,require_sysml=True)
        if not checked['ready'] or checked['spec_digest']!=spec_digest:raise ValueError('repair saved double source no longer validates')
        ledger=Path(ctx['runtime_authority']['ledger_path'])
        with closing(sqlite3.connect(ledger.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row;meta=dict(db.execute('SELECT key,value FROM meta'))
            uncertain=db.execute("SELECT 1 FROM requests WHERE status IN ('unknown','reserved','dispatched') LIMIT 1").fetchone()
            processes=[dict(row) for row in db.execute('SELECT * FROM process_attempts')]
            candidate=db.execute("SELECT detail_json FROM counters WHERE kind='candidate' AND event_id=?",('request_candidate_'+ctx['request_id'],)).fetchone()
            repair=db.execute("SELECT 1 FROM counters WHERE kind='software_repair' AND event_id=?",(approval['software_repair_event'],)).fetchone()
        if uncertain or meta.get('frozen') or meta.get('connect_retry_required') or meta.get('authorization_sha256')!=ctx['runtime_authority']['sha256']:raise PermissionError('execution repair cannot recover UNKNOWN/in-flight/frozen activity')
        if not candidate or json.loads(candidate['detail_json'])!=dict(request_id=ctx['request_id'],displayed_digest=ctx['displayed_digest']) or not repair:raise ValueError('repair lacks original charged candidate or already-accounted software event')
        if any(row['state']!='FINISHED' for row in processes):raise PermissionError('prior process has not exited; execution repair stopped')
        workers=[row for row in processes if row['kind']=='worker' and json.loads(row['detail_json']).get('task_id')==task['task_id']]
        if len(workers)!=1 or not workers[0].get('finished_at'):raise PermissionError('known failed worker exit evidence required')
        used={kind:sum(row['kind']==kind for row in processes) for kind in ('compile','simulation')}
        if any(authority['caps'][kind]-used[kind]<1 for kind in used):raise PermissionError('execution repair has no cumulative compile/simulation allowance')
        binding=dict(schema='v12-known-execution-repair-intent-1',request_id=ctx['request_id'],request_digest=ctx['request_digest'],project_id=ctx['project_id'],baseline_ref=ctx['baseline_ref'],displayed_digest=ctx['displayed_digest'],runtime_authority=ctx['runtime_authority'],approval_path=str(path),approval_sha256=sha_file(path),original_proposal_id=proposal['proposal_id'],previous_task_id=task['task_id'],previous_version_id=snapshot['version_id'],spec_digest=spec_digest,source_sha256=snapshot['source_sha256'],confirmation_sha256=sha_value(confirmation),protected_hashes=protected,process_counts_before=used,execution_request_id='case_execute_repair_1_'+ctx['request_id'],physical_candidate_reused=True,source_code_sha256={name:sha_file(ROOT/'src'/name) for name in ('case_taskflow.py','case_cli.py','case_toolchain.py')})
        return dict(binding=binding,spec=spec,generated=generated,confirmation=confirmation)

    async def repair_execution(self,approval_path):
        """One explicit local recheck of a known parameter-readback interface failure."""
        completed=self._repaired_pipeline()
        if completed is not None:return dict(reused=True,pipeline=completed,state=self.state(verify_current=True),product_http_added=0,execution_added=0)
        intent_path=self.root/'repair-execution-1.intent.json'
        if intent_path.exists():raise PermissionError('execution repair already claimed; read saved task/proposal evidence, never resend')
        evidence=self._execution_repair_evidence(approval_path);binding=evidence['binding'];spec=evidence['spec']
        # Exclusive claim precedes the first mutation. A lost RPC remains stopped,
        # rather than becoming an implicit new-identity recovery.
        with intent_path.open('x',encoding='utf8') as handle:json.dump(binding,handle,ensure_ascii=False,indent=2,allow_nan=False)
        try:
            async with self.channel():
                prepared=await self.mcp('proposal_prepare',dict(spec=spec));new_id=prepared['proposal']['proposal_id']
                readback=await self.mcp('proposal_get',dict(proposal_id=new_id));svc=self.open();proposal=svc.store.proposal(new_id)
                self._same_repair_candidate(proposal,spec,binding['spec_digest'])
                if new_id==binding['original_proposal_id'] or proposal['state']!='awaiting_confirmation':raise ValueError('repair requires a new unexecuted identical proposal')
                check_delta(svc,proposal['spec'],task_context=self.ctx)
                save_json(self.root/'repair-execution-1-proposal.json',dict(binding=binding,proposal_id=new_id,readback=readback),immutable=True)
                svc.confirm_local(new_id,proposal['displayed_digest'])
                with svc.store.transaction() as db:
                    row=db.execute('SELECT confirmation_json FROM proposals WHERE proposal_id=?',(new_id,)).fetchone();confirmation=json.loads(row[0]);confirmation.update(channel='trusted_local_request_design_confirmation',request_id=self.ctx['request_id'],request_displayed_digest=self.ctx['displayed_digest'],approval=evidence['confirmation'],source_reviewed_by_human=False,execution_repair=dict(intent_path=str(intent_path),intent_sha256=sha_value(binding),previous_task_id=binding['previous_task_id'],same_product_candidate=True))
                    db.execute('UPDATE proposals SET confirmation_json=? WHERE proposal_id=?',(json.dumps(confirmation,ensure_ascii=False),new_id))
                current=await self.mcp('execution_start',dict(proposal_id=new_id,request_id=binding['execution_request_id']));task_id=current['task']['task_id']
                save_json(self.root/'repair-execution-1-start.json',current,immutable=True)
                from .case_requests import remaining_request
                while current['task']['state'] in ('queued','running','cancel_requested'):
                    if remaining_request(self.ctx['request_id'])<=0:raise RuntimeError('execution repair UNKNOWN at shared deadline; no relaunch')
                    await asyncio.sleep(.5);current=await self.mcp('execution_status',dict(task_id=task_id))
            save_json(self.root/'repair-execution-1-execution.json',current,immutable=True)
            snapshot=self.open(read_only=True)._artifact_version(current['task']['version_id'])
            if snapshot['spec_digest']!=binding['spec_digest'] or snapshot['frozen_spec']!=spec:raise ValueError('rechecked execution candidate differs')
            for filename,expected in binding['protected_hashes'].items():
                if sha_file(Path(filename))!=expected:raise ValueError('execution repair changed protected original evidence')
            executed=dict(current,parameters=modelica_parameters(spec['artifact']['modelica_source'])[0],reference=module('artifact_baseline').reference(snapshot))
            receipt=dict(schema='v12-known-execution-repair-receipt-1',**{key:value for key,value in binding.items() if key!='schema'},new_proposal_id=new_id,new_task_id=task_id,new_version_id=snapshot['version_id'],execution_sha256=sha_value(current),terminal_state=current['task']['state'],candidate_counter_added=0,physical_parameters_changed=False)
            save_json(self.root/'repair-execution-1.json',receipt,immutable=True)
            try:explanation=await self.ask('请解释最新参数证据修复重验的实际版本、任务、双源码及基准SysML侧证；保留旧参数证据失败，不把它改成通过。',explain=True)
            except Exception as exc:explanation=dict(status='INCOMPLETE',error=type(exc).__name__+': '+str(exc),old_explanation_preserved=True)
            result=dict(schema='v12-execution-repair-pipeline-1',request_id=self.ctx['request_id'],stage='REPAIR_RESULT_READ_BACK',execution=executed,explanation=explanation,state=self.state(verify_current=True),repair_receipt_sha256=sha_value(receipt),previous_result_path=str(self.root/'pipeline.json'),same_product_candidate=True)
            save_json(self.root/'pipeline-repair-1.json',result,immutable=True)
            save_json(self.root/'pipeline-repair-latest.json',dict(schema='v12-execution-repair-readback-head-1',request_id=self.ctx['request_id'],pipeline_sha256=sha_file(self.root/'pipeline-repair-1.json'),repair_receipt_sha256=sha_value(receipt)),immutable=True)
            return result
        except BaseException as exc:
            save_json(self.root/'repair-execution-1-stopped.json',dict(error=type(exc).__name__+': '+str(exc),binding=binding,status='STOPPED_NO_AUTOMATIC_RETRY'),immutable=True)
            raise
    async def execute(self):
        from .case_requests import require_execution,remaining_request
        self.refresh();require_execution(self.ctx['request_id']);generated=await self.generate();svc=self.open()
        proposal=svc.store.proposal(generated['proposal_id']);check_delta(svc,proposal['spec'],task_context=self.ctx)
        # Local trusted user approved the displayed design, not a product tool.
        # Its digest, exact new sources and concrete double source commitments
        # are rechecked before the ordinary Store confirmation.
        if proposal['state']=='awaiting_confirmation':
            svc.confirm_local(proposal['proposal_id'],proposal['displayed_digest'])
            with svc.store.transaction() as db:
                row=db.execute('SELECT confirmation_json FROM proposals WHERE proposal_id=?',(proposal['proposal_id'],)).fetchone();confirmation=json.loads(row[0]);confirmation.update(channel='trusted_local_request_design_confirmation',request_id=self.ctx['request_id'],request_displayed_digest=self.ctx['displayed_digest'],approval=read_json(self.ctx['confirmation_path']),source_reviewed_by_human=False)
                db.execute('UPDATE proposals SET confirmation_json=? WHERE proposal_id=?',(json.dumps(confirmation,ensure_ascii=False),proposal['proposal_id']))
        rid='case_execute_'+self.ctx['request_id']
        async with self.channel():
            emit_progress('execution_wait','正在提交已确认提案，等待MCP执行流水线。')
            result=await self.mcp('execution_start',dict(proposal_id=proposal['proposal_id'],request_id=rid));task_id=result['task']['task_id']
            limit=time.monotonic()+remaining_request(self.ctx['request_id'])
            while True:
                current=await self.mcp('execution_progress',dict(task_id=task_id))
                progress=current.get('progress_snapshot') or {}
                facts=progress.get('stage_facts') or {}
                compiled=(facts.get('compile') or {}).get('status')=='FINISHED_EXIT_0'
                simulated=(facts.get('simulation') or {}).get('status')=='FINISHED_EXIT_0'
                message='编译与仿真进程已结束，正在等待独立检查及终态回执。' if simulated else '编译进程已结束，正在等待仿真阶段完成回执。' if compiled else '正在等待编译阶段完成回执。'
                emit_progress('independent_check' if simulated else 'simulation' if compiled else 'compile',message,request_id=self.ctx['request_id'],task_id=task_id,state=current['task']['state'],stage_facts=facts)
                if current['task']['state'] not in ('queued','running','cancel_requested'):break
                if time.monotonic()>=limit:raise ValueError('execution UNKNOWN after bounded polling; read task without relaunch')
                await asyncio.sleep(.5)
            emit_progress('readback','执行进程已结束，正在核对本次完整结果。',request_id=self.ctx['request_id'],task_id=task_id)
            current=await self.mcp('execution_status',dict(task_id=task_id))
        if isinstance(current.get('snapshot'),dict):self._selected_snapshot(current['task']['version_id'],snapshot=current['snapshot'])
        emit_progress('execution_complete','MCP执行已返回终态，正在保存结果。',task_id=task_id,state=current['task']['state'])
        save_json(self.root/('execution-'+task_id+'.json'),current,immutable=True);return current
    def _result_followup_evidence(self,binding,snapshot):
        """Identify this request's terminal result without granting HTTP authority.

        The historical budget category ``reopen`` reserves a result-review exit.
        An ordinary user followup can fulfil that exit; it is still not a cold
        reopen. Budget time, uncertainty and full reservations remain enforced
        by the existing runtime, independently of this result identity check.
        """
        latest=binding.get('latest_request_execution') or {}
        if self.ctx.get('access_mode')=='RESULT_REVIEW_ONLY' or not isinstance(snapshot,dict):return None
        if binding.get('request_id')!=self.ctx.get('request_id') or binding.get('project_id')!=self.ctx.get('project_id'):return None
        if snapshot.get('record_kind')!='ARTIFACT_EXECUTION_SNAPSHOT':return None
        for key in ('project_id','task_id','version_id'):
            if not latest.get(key) or latest[key]!=snapshot.get(key):return None
        if latest['project_id']!=self.ctx.get('project_id'):return None
        task=self.open(read_only=True).store.task(latest['task_id'])
        if any(task.get(key)!=latest[key] for key in ('project_id','task_id','version_id')) or not self.ctx.get('session_id') or task.get('session_id')!=self.ctx['session_id']:return None
        if task.get('state') not in ('succeeded','failed','cancelled') or snapshot.get('state')!=task['state'] or latest.get('state')!=task['state']:return None
        return dict(request_id=self.ctx['request_id'],project_id=latest['project_id'],task_id=latest['task_id'],version_id=latest['version_id'],terminal_state=task['state'],session_id=task['session_id'],source='READONLY_STORE_TASK_AND_VERSION_SNAPSHOT')

    async def ask(self,question,*,reopen=False,explain=False):
        if self.ctx.get('access_mode')=='RESULT_REVIEW_ONLY':return await self.ask_review(question,reopen=reopen)
        if not isinstance(question,str) or not question.strip():raise ValueError('nonempty question required')
        before=self.state(verify_current=True);history_path=self.root/'dialogue.json';history=read_json(history_path) if history_path.exists() else []
        latest=before['latest_request_execution'];snapshot=self._selected_snapshot(latest['version_id']) if latest else None
        actual=None
        if snapshot:
            outcome=snapshot.get('outcome',{});tool=outcome.get('tool_result',{})
            actual=dict(reference=module('artifact_baseline').reference(snapshot) if snapshot.get('manifest_sha256') else None,version_id=snapshot['version_id'],task_id=snapshot['task_id'],evidence_status=snapshot['evidence_status'],parameters=modelica_parameters(snapshot['frozen_spec']['artifact']['modelica_source'])[0],outcome={k:outcome[k] for k in ('tool_status','chain_status','task_status','mission_assessment','runtime_parameter_evidence','execution_mode','test_only','modelica_status','dual_source_issues','sysml_status','consistency_status') if k in outcome},tool_result={k:tool[k] for k in ('compile','simulation','parameter_readback','solver') if k in tool},result_delivery=snapshot.get('result_delivery'),source_files=snapshot.get('source_files'),curve_files=snapshot.get('curve_files'),data_files=snapshot.get('data_files'))
            actual.update(state=snapshot.get('state'),task_status=snapshot.get('task_status'),error=snapshot.get('error'),runtime_parameter_readback=outcome.get('runtime_parameter_readback'))
            actual['historical_recorded_outcome']=outcome_summary(actual.pop('outcome'))
            actual.update(current_summary(dict(snapshot,current_contact_review=latest.get('current_contact_review'))))
            actual['outcome']=actual['current_contact_review'].get('current_assessment',{})
            if not outcome:actual['incomplete_task_receipt']=self.open(read_only=True).store.task(latest['task_id']).get('result')
        original=self.ctx['baseline_snapshot']['frozen_spec']['artifact'];sidecar=self.root/'baseline-sysml-completion.json'
        baseline_sysml=dict(status='MISSING_NOT_COMPLETED',original_v0_unchanged=True)
        reference=self.root/'baseline-sysml-reference.json'
        if original.get('sysml_source') or sidecar.exists() or reference.exists():
            source=baseline_sysml_for_context(self.ctx)
            chosen=reference if reference.exists() else sidecar
            baseline_sysml=dict(status='VALIDATED_SOURCE_BOUND_SIDECAR_REFERENCE' if reference.exists() else 'VALIDATED_SOURCE_BOUND_SIDECAR' if sidecar.exists() else 'PRESENT_IN_SELECTED_BASELINE',path=str(chosen) if chosen.exists() else None,sha256=sha_file(chosen) if chosen.exists() else None,sysml_source_sha256=module('model_artifact').sha(source.encode('utf8')),original_v0_unchanged=True)
        from .case_stage_facts import read_stage_facts
        stage_facts=read_stage_facts(self.ctx)
        messages=[dict(role='system',content='中文只读咨询。解释所绑定新需求、所选基线、当前最新结果和最近达标版。不得修改、生成、执行或授予权限；新要求请使用独立request入口。没有本请求结果就是未执行，历史PASS不能冒充新请求PASS；实物能力未知，保留简化模型限制。基准SysML侧证与冻结v0分开：VALIDATED_SOURCE_BOUND_SIDECAR表示已绑定原资料并通过验证，不能因v0原件无SysML声称尚未补齐。切向力、法向量和限幅须按当前绑定源码及actual_request_execution事实解释，不套用其他家族公式，不臆造额外摩擦圆；世界Fz不能当所有接触面的法向力，无轮自转模型不能声称已验证滑移；历史问答中的楼梯尺寸或PASS只能作为历史叙述，本次尺寸和结果以actual_request_execution为准。明确阶段事实stage_facts优先于历史解释：plan失败表示方案阶段失败，不能混称双源码生成/编译/仿真失败；已解决的failure_history只作历史，不可当作其他请求当前失败。'),dict(role='user',content=json.dumps(dict(raw_request=self.ctx['raw_request'],binding=before,baseline_snapshot=self.ctx['baseline_snapshot'],baseline_sysml_sidecar=baseline_sysml,actual_request_execution=actual,stage_facts=stage_facts),ensure_ascii=False))]
        from .case_contact_review import rule_for_spec
        source_facts=selected_source_facts(self.ctx)
        supplied=json.loads(messages[1]['content']);supplied['verified_source_facts']=source_facts
        messages[1]['content']=json.dumps(supplied,ensure_ascii=False)
        messages[0]['content']+=' 当前基准可用性以verified_source_facts为准，历史导入缺项不能覆盖该事实。'
        messages[0]['content']+='\n'+rule_for_spec(self.ctx['baseline_snapshot']['frozen_spec'])+' 历史对话只作历史解释原文，不能推翻当前复核。'
        for turn in history:messages.extend([dict(role='user',content='【历史对话，仅供追溯，不代表当前复核】'+turn['question']),turn['assistant_message']])
        if actual:messages.append(dict(role='user',content='【当前只读复核，覆盖上文历史解释】'+json.dumps(actual['current_contact_review'],ensure_ascii=False)))
        followup=self._result_followup_evidence(before,snapshot) if not reopen and not explain else None
        messages.append(dict(role='user',content=question));stage=('reopen_' if reopen else 'explain_' if explain else 'result_followup_' if followup else 'ask_')+str(len(history))
        emit_progress('explain','正在等待产品模型解释绑定结果。')
        response=await self.call(stage,messages,[])
        if response.get('tool_calls') or not response.get('content','').strip():raise ValueError('read-only answer must be plain nonempty text')
        emit_progress('readback','产品通信已结束，正在核对当前请求绑定。',request_id=self.ctx['request_id'])
        after=self.state(verify_current=True)
        binding=lambda value:{key:item for key,item in value.items() if key!='http_state'}
        if binding(after)!=binding(before):raise ValueError('read-only answer changed project/task binding')
        emit_progress('fact_validation','正在核对说明文字的版本、来源和已知事实。',request_id=self.ctx['request_id'])
        protected=self._protect_explanation(response['content'],before)
        budget_purpose=dict(category='reopen',purpose='EXISTING_RESULT_REVIEW_EXIT',historical_category_name=True,cold_reopen=False,evidence=followup) if followup else None
        emit_progress('save_result','说明核对已结束，正在保存本次问答。',request_id=self.ctx['request_id'],body_displayed=protected['explanation_validation']['body_displayed'])
        history.append(dict(question=question,assistant_message=response['assistant_message'],binding=before,reopen=reopen,**protected,**(dict(budget_purpose=budget_purpose) if followup else {})));save_json(history_path,history)
        emit_progress('complete','本次问答处理完成。',request_id=self.ctx['request_id'])
        return dict(**protected,binding=before,current_contact_review=(before.get('latest_request_execution') or {}).get('current_contact_review'),reopened=reopen,read_only_status_unchanged=True,**(dict(budget_purpose=budget_purpose) if followup else {}))
    async def run(self):
        if self.ctx.get('access_mode')=='RESULT_REVIEW_ONLY':raise PermissionError('readonly context has no generation or physical execution flow; use review/plan commands')
        if (self.root/'generation.json').exists():baseline_sysml_for_context(self.ctx)
        repaired=self._repaired_pipeline()
        if repaired is not None:return self._terminal_explanation_overlay(dict(reused=True,pipeline=repaired,state=self.state(verify_current=True),product_http_added=0,execution_added=0))
        completed=self.root/'pipeline.json'
        if completed.exists():return self._terminal_explanation_overlay(dict(reused=True,pipeline=read_json(completed),state=self.state(verify_current=True)))
        terminal=self.terminal_readback()
        if terminal is not None:return terminal
        planned=await self.bounded('plan',self.plan);self.refresh();packet=self.ctx['plan']
        if packet['intent']=='QUESTION' or packet['support']!='SUPPORTED':return dict(stage='NO_EXECUTABLE_PLAN',proposal=planned)
        from .case_requests import require_execution
        try:require_execution(self.ctx['request_id'])
        except (ValueError,RuntimeError,PermissionError) as exc:return dict(stage='AWAITING_CONFIRMATION_OR_EXECUTION_AUTHORITY',proposal=planned,reason=str(exc))
        generated=await self.bounded('generate',self.generate)
        if generated.get('status')=='REVIEW_REQUIRED':return dict(stage='CONTACT_SOURCE_REVIEW_REQUIRED',proposal=planned,generation=generated)
        executed=await self.execute()
        # Enrich the pipeline view from the actual frozen version; the original
        # MCP execution receipt remains unchanged in execution-<task>.json.
        snapshot=self._selected_snapshot(executed['task']['version_id'])
        executed=dict(executed,parameters=modelica_parameters(snapshot['frozen_spec']['artifact']['modelica_source'])[0],reference=module('artifact_baseline').reference(snapshot) if snapshot.get('manifest_sha256') else None)
        current=self.state(verify_current=True)
        terminal=self.terminal_readback(bound_state=current)
        if terminal is not None:
            path=self.root/'pipeline-partial.json';latest=current.get('latest_request_execution') or {}
            if path.is_file():
                emit_progress('result_available','本次任务结果已核对并保存，正在处理可选结果说明。',request_id=self.ctx['request_id'],result_ready=dict(path=str(path.resolve()),sha256=sha_file(path),request_id=self.ctx['request_id'],project_id=self.ctx['project_id'],task_id=latest.get('task_id'),version_id=latest.get('version_id')))
        try:explained=await self.ask('请解释本请求的参数修改、真实工具/任务结果、最近有效版本及模型限制。',explain=True)
        except Exception as exc:
            partial=self.terminal_readback(error=type(exc).__name__+': '+str(exc))
            if partial is None:raise
            return partial
        result=dict(stage='RESULT_READ_BACK',proposal=planned,generation=generated,execution=executed,explanation=explained,state=self.state(verify_current=True),
            current_fact_binding=explained['current_fact_binding'],current_source_references=explained['current_source_references'])
        save_json(completed,result,immutable=True);return result
