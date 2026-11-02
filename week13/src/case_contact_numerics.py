"""Trusted, narrow continuation of the failed contact source; no model synthesis."""
from copy import deepcopy
from pathlib import Path
import re

from .case_paths import ROOT, read_json, save_json, sha_file, sha_value

PACKAGE_PATH = ROOT/'results/contact-numerics-20261022-01/execution-approval.json'
PACKAGE_SHA = '27195b3d8b5c2cd49338cd690b6d152890c0e8cd058f673f1bd77e2c393d4d84'
REFERENCE_SHA = 'cfabd69f58616d0d4c7b1e6012e12a7596b961d9ce620a1081fd949af5d85c69'

PLAN_PROMPT = '''你是原MBSE产品Agent。本次已批准范围仅为已失败v4接触源码的切向力分配数值修复，原v0仍为车辆/任务物理基线。只调用case_request_plan一次。不得重新设计接触几何、法向、动力学或车辆。说明保留原零/近零截断意图、非零域分配与扭矩边界，使连续求解试值/事件迭代中的除法始终有定义；具体表达式由产品提出。proposed_parameters恰为m/I_pitch/T_main_max/T_nose_max/stepH/stepD，前四固定85/20/60/25，后两为本请求工况。scope恰为{change_kind:"contact_model_repair",frozen:"original_vehicle_parameters_geometry_control_route_observation",family:"source_review_bound_stair_contact_v1"}。requirements每项{text,source_id,source_text}精确引用可信来源且至少一项引用本请求原文；design={summary,assumptions:[{text,origin:"model"}],parameters:[{name,value,unit,origin:"model",basis}]}并包含一致六参数。intent=CHANGE，支持范围内support=SUPPORTED，missing/unsupported为空。defaults/unknowns/missing/unsupported为字符串列表，rationale完整中文。已验证原基准SysML侧证通过可信引用复用，仅生成候选双源码，不再要求重造原侧证。三工况只改H/D和原派生量，任务/参数/控制/120秒/DASSL冻结，不预设300mm结果，工具失败不能当车辆失败。产品不能自批或注册源码。'''

GEN_PROMPT = '''你是同一MBSE产品Agent，只调用proposal_prepare一次，arguments={"spec":{完整候选}}。confirmed_plan与trusted_generation_contract保持。以contact_repair_authority.repair_reference.modelica_source给出的实际v4失败源为唯一修复起点；原v0是参数/任务基线，不能从旧v0重写接触实现。仅允许更改FtC[i,b]赋值的RHS：保留原Fn>1e-9零/近零截断意图，保留非零有效域分配、摩擦及扭矩界，使所有连续试值/事件迭代均不会零除。不要仅改变阈值，不改接触几何/法向力/参考系/控制/动力学。除此RHS和本工况stepH/stepD外，完整Modelica逐token继承参考源（包括参数、声明、输出和数值设置）。具体数值公式由你写，不由开发代写。完整SysML同步表达本候选与原参数/依赖，引用可信来源；原基准SysML侧证已验证，不返回baseline_sysml_source。spec字段与可信合同一致，sources只按顺序{id}引用，不返回text；源码包含真实换行。若reviewed_contact_family存在，逐token继承其新源码，仅替换stepH/stepD。第一份修后源须保存为REVIEW_REQUIRED等待可信精确hash审查，此状态不重发HTTP、不自称物理PASS。'''


PLAN_PROMPT += '\n范围硬锁：仅改现有FtC[i,b]赋值的RHS；禁止增加Ft_inv或其他中间变量、声明、函数和方程。数值保护须内联于现有RHS，具体表达式由产品决定。返回唯一完整有效JSON对象，字段间逗号齐全，字符串完整转义。'
GEN_PROMPT += '\n完整返回协议：spec必须含schema_version/backend/baseline_ref/artifact/sources/requirements/design/libraries/settings/outputs/checks全部11项；仅若基线有trajectory_contract才原样加入第12项。artifact恰有model_name/modelica_source/sysml_source，均非空；Modelica与SysML均输出完整全文，不能仅sources、仅修改行、差异、占位、TODO、省略号或口头沿用。schema_version="v11-model-artifact-1"，backend="model-artifact"；完整其余合同从trusted_generation_contract逐字段带回。'


def package():
    if sha_file(PACKAGE_PATH)!=PACKAGE_SHA:raise ValueError('numerical approval drift')
    value=read_json(PACKAGE_PATH)
    if value.get('schema')!='v12-contact-numerics-package-approval-1' or value.get('approval_channel')!='USER_CHAT_APPROVAL' or value.get('approval_quote')!='开始执行':raise PermissionError('numerical scope lacks actual user approval')
    return value


def reference(ctx):
    """Read the immutable failed source through its original request/version."""
    from .case_requests import load_request
    from .case_backend import service,module
    p=package();binding=p['repair_reference'];origin=load_request(binding['request_id'])
    if origin['project_id']!=ctx['project_id']:raise ValueError('repair reference project drift')
    svc=service(origin['db_path'],origin['session_id'],read_only=True,task_context=origin)
    # Trust bindings must read the manifest-verified historical snapshot without
    # entering CaseService's high-level current-contact review overlay. That
    # overlay itself reads this registration and would recurse into reference().
    snap=module('artifact_service').ArtifactService._artifact_version(svc,binding['version_id'])
    if snap.get('evidence_status')!='COMPLETE' or snap.get('recorded_conclusions_verified') is not True:raise ValueError('failed reference lacks complete manifest-verified historical evidence')
    if any(snap.get(k)!=binding[v] for k,v in (('task_id','task_id'),('source_sha256','modelica_source_sha256'),('manifest_sha256','manifest_sha256'))):raise ValueError('failed contact reference binding drift')
    source=snap['frozen_spec']['artifact']['modelica_source']
    from .case_contact_family import source_sha
    if source_sha(source)!=REFERENCE_SHA or sha_file(binding['failure_review_path'])!=binding['failure_review_sha256']:raise ValueError('failed contact source/evidence drift')
    return dict(binding,modelica_source=source,sysml_source=snap['frozen_spec']['artifact']['sysml_source'],frozen_spec=deepcopy(snap['frozen_spec']))


def ftc_identity(source):
    """Mask exactly one array-allocation RHS, plus the two approved dimensions."""
    from .case_scope import strip_comments
    from .case_contact_family import _source_identity
    stripped=strip_comments(source)
    pattern=r'(\bFtC\s*\[\s*i\s*,\s*b\s*\]\s*=)([^;]+)(;)'
    value,count=re.subn(pattern,lambda m:m[1]+' NUMERICAL_ALLOCATION_RHS '+m[3],stripped)
    if count!=1:raise ValueError('numerical repair requires exactly one FtC[i,b] assignment')
    return _source_identity(value)


def verify_numerical_delta(ctx,source):
    ref=reference(ctx)
    if ftc_identity(source)!=ftc_identity(ref['modelica_source']):raise ValueError('numerical repair changed source outside FtC RHS and approved H/D')
    from .case_contact_family import _source_identity
    if _source_identity(source)==_source_identity(ref['modelica_source']):raise ValueError('numerical repair must change the existing FtC RHS, not only comments or H/D')
    return dict(schema='v12-reviewed-contact-domain-1',ground_pad_extension_m=100.,corner_regularization_distance_m=1e-9,
                reviewed_source_sha256=__import__('hashlib').sha256(source.encode('utf8')).hexdigest(),
                family_shape_sha256=__import__('week13.src.case_contact_family',fromlist=['_source_identity'])._source_identity(source),
                geometry_unchanged=True,reference_source_sha256=REFERENCE_SHA)


def completion_reference(ctx):
    """Reuse verified source provenance; never copy or alter the old receipt."""
    from .case_requests import load_request
    from .case_taskflow import baseline_sysml_for_context
    p=package();origin=load_request(p['repair_reference']['request_id'])
    if ctx['baseline_ref']!=origin['baseline_ref'] or ctx['baseline_snapshot']!=origin['baseline_snapshot']:raise ValueError('reused baseline sidecar belongs to another exact baseline')
    source=baseline_sysml_for_context(origin)
    path=Path(origin['root'])/'baseline-sysml-completion.json'
    return dict(schema='v12-baseline-sysml-reference-1',origin_request_id=origin['request_id'],
                request_id=ctx['request_id'],request_digest=ctx['request_digest'],baseline_ref=ctx['baseline_ref'],
                baseline_snapshot_sha256=sha_value(ctx['baseline_snapshot']),completion_path=str(path),completion_sha256=sha_file(path),
                sysml_source_sha256=__import__('hashlib').sha256(source.encode('utf8')).hexdigest(),
                approved_package_sha256=PACKAGE_SHA,original_v0_unchanged=True)


def referenced_sysml(ctx):
    path=Path(ctx['root'])/'baseline-sysml-reference.json'
    if not path.exists():return None
    expected=completion_reference(ctx)
    if read_json(path)!=expected:raise ValueError('reused baseline SysML reference drift')
    from .case_requests import load_request
    from .case_taskflow import baseline_sysml_for_context
    return baseline_sysml_for_context(load_request(expected['origin_request_id']))


def bind_completion(ctx):
    save_json(Path(ctx['root'])/'baseline-sysml-reference.json',completion_reference(ctx),immutable=True)
    return referenced_sysml(ctx)


def source_revision(ctx,*,require=False):
    """Read one immutable same-ledger revision binding, never infer permission."""
    path=Path(ctx['root'])/'contact-source-revision-binding.json'
    if not path.exists():
        if require:raise PermissionError('unapproved numerical request lacks the one source-revision binding')
        return 0
    binding=read_json(path);intent_path=Path(binding['intent_path']).resolve()
    expected=Path(ctx['runtime_authority']['ledger_path']).resolve().parent/'source-revision-1.json'
    if intent_path!=expected or sha_file(intent_path)!=binding.get('intent_sha256'):raise ValueError('source revision intent drift')
    intent=read_json(intent_path)
    if intent.get('schema')!='v12-known-contact-source-revision-1' or intent.get('channel')!='TRUSTED_LOCAL_DEVELOPMENT_REVIEW' or intent.get('approved_package_sha256')!=PACKAGE_SHA or intent.get('authority_sha256')!=ctx['runtime_authority']['sha256'] or intent.get('revision')!=1 or ctx['request_id'] not in intent.get('revision_requests',{}).values():raise PermissionError('source revision lacks exact approved authority/child identity')
    return 1


def revision_context(ctx):
    if source_revision(ctx)!=1:return None
    binding=read_json(Path(ctx['root'])/'contact-source-revision-binding.json');intent=read_json(binding['intent_path'])
    from .case_contact_family import _read_registration,REGISTRY_ROOT
    prior=_read_registration(REGISTRY_ROOT/(ctx['runtime_authority']['sha256']+'.json'))
    if sha_value(prior)!=intent['previous_source_review_sha256']:raise ValueError('prior source review drift for bounded revision')
    from .case_requests import load_request
    origin_id=next(key for key,value in intent['revision_requests'].items() if value==ctx['request_id'])
    origin=load_request(origin_id)
    return dict(revision=1,review_basis=intent['review_basis'],known_failures=intent['known_failures'],previous_product_plan=origin.get('plan'),previous_plan_role='HISTORICAL_EXACT_EXPRESSION_NOT_NEW_APPROVAL',previous_reviewed_modelica_source=prior['reviewed_modelica_source'],previous_reviewed_source_sha256=prior['modelica_source_sha256'],allowed_changes='ONLY_EXISTING_FtC_RHS_KEEP_ALL_OTHER_TOKENS',full_affected_set_rerun_limit=1)


def prepare_source_revision(affected_request_ids,review_basis):
    """Trusted local setup after known failure; no model/API/MCP execution.

    Only the primary calls this after accepting a new numerical repair basis.
    All child requests retain the same immutable authority and shared ledger.
    """
    from .case_requests import load_request,authorize_budget
    from .case_budget import CaseBudget,connection
    from .case_taskflow import bootstrap,TaskFlow
    from .case_contact_family import contact_authority,reviewed_family
    p=package()
    if not isinstance(affected_request_ids,list) or not affected_request_ids or len(set(affected_request_ids))!=len(affected_request_ids) or any(rid not in p['request_ids'] for rid in affected_request_ids):raise ValueError('one affected subset of approved initial scenarios required')
    if not isinstance(review_basis,str) or not review_basis.strip():raise ValueError('new testable numerical correction basis required')
    origins=[load_request(rid) for rid in affected_request_ids];ctx=origins[0];a,_=contact_authority(ctx);budget=CaseBudget(task_context=ctx)
    with connection(budget.path) as db:
        budget._blocked(db)
        if db.execute("SELECT 1 FROM requests WHERE status IN ('reserved','dispatched','unknown')").fetchone() or db.execute("SELECT 1 FROM process_attempts WHERE state NOT IN ('FINISHED','NOT_LAUNCHED')").fetchone():raise PermissionError('source revision cannot retry UNKNOWN/inflight activity')
    prior=reviewed_family(package_sha=PACKAGE_SHA,authority_sha=ctx['runtime_authority']['sha256'])
    if prior is None:raise PermissionError('source revision requires a previously valid reviewed source')
    failures=[];known_defect=False
    for origin in origins:
        if origin['runtime_authority']!=ctx['runtime_authority']:raise ValueError('affected set must share the exact authority')
        flow=TaskFlow(origin);latest=flow.state().get('latest_request_execution')
        if not latest:
            failures.append(dict(request_id=origin['request_id'],status='NOT_RUN_SHARED_ALGORITHM_AFFECTED'));continue
        if latest['state'] not in ('succeeded','failed','cancelled'):raise PermissionError('affected revision requires known terminal activity; no in-flight recovery')
        if latest['tool_status']=='TOOL_FAIL' or latest.get('validity_status')=='FAIL':known_defect=True
        if latest['tool_status'] not in ('PASS','TOOL_FAIL'):raise PermissionError('unsupported or uncertain terminal state')
        failures.append(dict(request_id=origin['request_id'],version_id=latest['version_id'],task_id=latest['task_id'],tool_status=latest['tool_status'],task_status=latest['task_status'],current_contact_review=latest.get('current_contact_review')))
    if not known_defect:raise PermissionError('credible task failure or unrun scenarios alone do not authorize numerical source revision')
    path=Path(a['ledger_path']).parent/'source-revision-1.json'
    children={origin['request_id']:origin['request_id']+'_r1' for origin in origins}
    intent=dict(schema='v12-known-contact-source-revision-1',channel='TRUSTED_LOCAL_DEVELOPMENT_REVIEW',approved_package_sha256=PACKAGE_SHA,authority_sha256=ctx['runtime_authority']['sha256'],revision=1,revision_requests=children,previous_source_review_sha256=sha_value(prior),known_failures=failures,review_basis=review_basis,physical_parameter_revision=False)
    if path.exists() and read_json(path)!=intent:raise PermissionError('the one source revision is already bound; no second revision')
    detail=dict(stage='source_revision',intent_sha256=sha_value(intent),known_terminal=True,review_basis=review_basis)
    budget.consume('software_repair','contact-source-revision-1',detail);budget.consume('source_revision','contact-source-revision-1',detail)
    save_json(path,intent,immutable=True)
    result=[]
    for origin in origins:
        rid=children[origin['request_id']];child=bootstrap(rid,origin['project_id'],'v000000',origin['raw_request'])
        child=authorize_budget(rid,p['budget_id'],'确认',reuse=True)
        save_json(Path(child['root'])/'contact-source-revision-binding.json',dict(intent_path=str(path),intent_sha256=sha_file(path)),immutable=True)
        bind_completion(child)
        remap={row['id']:next(new['id'] for new in child['sources'] if new['text']==row['text']) for row in origin['sources']}
        if origin.get('plan'):
            # The prior plan states its exact RHS expression. A genuine source
            # revision must get a product-supplied revised expression/dual-source
            # description, rather than silently confirming that obsolete text.
            save_json(Path(child['root'])/'inherited-numerical-plan.json',dict(origin_request_id=origin['request_id'],origin_displayed_digest=origin['displayed_digest'],origin_packet_sha256=sha_value(origin['plan']),source_id_projection=remap,role='PRIOR_PRODUCT_PLAN_CONTEXT_ONLY_NOT_EXECUTABLE',requires_product_numerical_revision_plan=True,confirmation_created=False),immutable=True)
        result.append(dict(request_id=rid,displayed_digest=child['displayed_digest']))
    return dict(status='ONE_AFFECTED_SET_REVISION_PREPARED_PRODUCT_NUMERICAL_PLAN_REQUIRED',requests=result,intent_path=str(path),intent_sha256=sha_file(path),new_HTTP=0,new_compile=0,new_simulation=0)
