"""Source-reviewed contact repair within one explicit, immutable user approval.

This is an adapter, not a contact implementation. Only the product supplies
Modelica/SysML. Registration is a trusted local development action and is never
exposed as a product tool or inferred from model claims.
"""
from copy import deepcopy
from datetime import datetime, timezone
import ast
import hashlib
from pathlib import Path
import re

from .case_paths import ROOT, read_json, save_json, sha_file, sha_value

CONTACT_SCOPE_PACKET = dict(change_kind='contact_model_repair',
    frozen='original_vehicle_parameters_geometry_control_route_observation',
    family='source_review_bound_stair_contact_v1')
PACKAGE_PATH = ROOT/'results/contact-physics-20261022-01/execution-approval.json'
PACKAGE_SHA = 'bfbcad939d3c4e6984cb275cb56c49329e745c2dde0d4894f2c0eb4f8ea351e3'
PROJECT_ID = 'prj_c200bb1e9f2c4d4abc188d043aac75b2'
BASELINE_SOURCE_SHA = 'b9ca3375266cb263a759a5153d5cbeb6320647c265e23cc69295468168332bf6'
SCENARIOS = ((.15,.28),(.18,.26),(.3,.26))
# Ordinary exact-source diagnostics, separate from the immutable contact-repair
# contract above. Input recognition grants neither execution nor physical PASS.
STAIR_DIAGNOSTIC_SCENARIOS = SCENARIOS + ((.15,.27),(.25,.26))
REGISTRY_ROOT = ROOT/'configs/contact-families'

PLAN_PROMPT = '''你是同一MBSE产品Agent，本次有唯一已批准contact_model_repair范围。只调用case_request_plan一次，packet字段与合同完全一致。用户批准的是修正原85kg六轮等效模型共同楼梯实体的圆轮踏面/立面/棱线接触，法向/切向、必要接触点速度/力矩及二维参考系一致性；具体算法由你提出，写在design.summary、assumptions和rationale，不要求开发代写接触方程。不是调车保证爬楼。全部原车辆参数/质量分布/I20/轮径/轴位/扭矩60和25/速度控制意图/20上+2m+20正向下+完全驶出续行/120s/DASSL/数值与输出设置继承；k_ground40000、c_ground3000、mu_s0.85不变。只能使用本请求指定的已批准楼梯工况。proposed_parameters恰为m/I_pitch/T_main_max/T_nose_max/stepH/stepD六项；前四为85/20/60/25。scope恰为{change_kind:"contact_model_repair",frozen:"original_vehicle_parameters_geometry_control_route_observation",family:"source_review_bound_stair_contact_v1"}。requirements每项{text,source_id,source_text}精确引用可信来源，至少一项引用本请求原文。design={summary,assumptions:[{text,origin:"model"}],parameters:[{name,value,unit,origin:"model",basis}]}，包含且一致声明六参数。intent=CHANGE，support在明确可做范围内为SUPPORTED，missing/unsupported为空；关键事实真缺则NEEDS_INPUT，不猜造。defaults/unknowns/missing/unsupported为字符串列表，rationale为完整中文字符串。原v0缺SysML则defaults明确补齐原基准侧证及候选SysML。三轮必须使用相同实体几何，不能保留旧轮组错一级floor差异来换取PASS；原方程历史PASS不证明物理有效。第一份新物理源码必须经可信开发审查精确哈希后才能进入MCP，模型不能自批或自称注册家族。后两工况只改H/D及原派生公式，保持同一已审接触算法。允许可信任务失败或120秒未完成，工具失败不能当车辆失败，有限采样无硬矛盾不是实物或完整物理认证。不能改质量/几何/控制或接触数值凑PASS，不授予权限、不编造运行结果。'''

GEN_PROMPT = '''你是同一MBSE产品Agent。按confirmed_plan及唯一contact_model_repair授权生成完整Modelica与SysML，只调用proposal_prepare一次，arguments={"spec":{...}}。本次允许且仅允许原共同楼梯实体的圆轮踏面/立面/棱线接触、法向/切向及必要接触点速度/力矩/二维参考系修正。三轮共同使用相同实体地形，不继承旧鼻轮与主轮错一级的floor差异。由你实际设计接触算法并写出完整源码；开发代理不代写车辆/Modelica。所有原参数声明/表达式保持，只有stepH/stepD采用确认工况；四派生量保持原依赖公式，原85kg/I20/轮径轴位/60和25扭矩/速度控制意图/k40000/c3000/mu0.85/20上2m20正向下/出口续行/120秒/DASSL与数值设置不变。不得调车、增真实电机或供电/车架/3D，不保证任务PASS。类名、settings/outputs/checks/libraries及可选trajectory_contract保持。SysML由你同步生成，所有有效共享参数/依赖表达式、req_N来源映射与Modelica一致。spec恰含schema_version/backend/baseline_ref/artifact/sources/requirements/design/libraries/settings/outputs/checks；基线有trajectory_contract才原样带入。spec.sources按合同顺序仅返回[{"id":"来源ID"}]，不可返回text；可信Store读取原文。requirements/design及其他合同原样，requirements.source_text精确引用可信原文。源码解码后含真实换行，spec为JSON对象，不再次编码。第一份物理源码保存后进入REVIEW_REQUIRED等待可信开发精确哈希审查，此状态不是协议失败，不应重发。若reviewed_contact_family存在，须逐token继承其完整Modelica，仅替换stepH/stepD数值；其他方程、算法、参数和声明不变，不另写一个近似版本。当前复核可给INVALID_MODEL/UNKNOWN，不能自称PASS或注册家族。'''


class ContactSourceReviewRequired(Exception):
    def __init__(self, spec):
        super().__init__('new contact source requires trusted exact-hash review; no HTTP retry')
        self.spec=deepcopy(spec)
        self.completion=None


def is_contact_scope(scope):
    return scope==CONTACT_SCOPE_PACKET


def _local(path):
    value=Path(path).resolve()
    if not value.is_relative_to(ROOT.resolve()) or value.is_relative_to((ROOT/'V12_old').resolve()):
        raise ValueError('contact review resource must stay in current week13')
    return value


def contact_authority(ctx, *, active=True):
    from .case_requests import authority_for
    if not isinstance(ctx,dict):raise PermissionError('contact repair requires a bound trusted request authority')
    authority=authority_for(ctx);binding=authority.get('contact_repair_package')
    from . import case_contact_numerics as numeric
    allowed={PACKAGE_SHA:PACKAGE_PATH,numeric.PACKAGE_SHA:numeric.PACKAGE_PATH}
    if not isinstance(binding,dict) or set(binding)!={'path','sha256'} or binding.get('sha256') not in allowed or _local(binding['path'])!=allowed[binding['sha256']].resolve() or sha_file(allowed[binding['sha256']])!=binding['sha256']:
        raise PermissionError('contact repair lacks the exact approved package binding')
    package=read_json(binding['path'])
    if package.get('schema') not in ('v12-contact-physics-package-approval-1','v12-contact-numerics-package-approval-1') or package.get('approval_channel')!='USER_CHAT_APPROVAL' or not package.get('approval_quote'):
        raise PermissionError('contact package lacks actual user approval')
    if ctx.get('project_id')!=PROJECT_ID or ctx.get('baseline_ref',{}).get('version_id')!='v000000' or ctx.get('baseline_ref',{}).get('source_sha256')!=BASELINE_SOURCE_SHA:
        raise PermissionError('contact repair belongs only to the approved original85 v0 project')
    if binding['sha256']==numeric.PACKAGE_SHA and ctx['request_id'] not in package['request_ids']:
        numeric.source_revision(ctx,require=True)
    for key in ('started_at_utc','hard_deadline_utc','caps'):
        if authority.get(key)!=package.get(key):raise PermissionError('contact authority differs from approved interval/cumulative caps: '+key)
    if authority.get('permissions',{}).get('execution') is not True:raise PermissionError('contact repair requires actual execution authority')
    if active:
        now=datetime.now(timezone.utc)
        if not datetime.fromisoformat(package['started_at_utc'])<=now<datetime.fromisoformat(package['hard_deadline_utc']):raise PermissionError('contact repair package interval expired or not active')
    return authority,package


def _binding(ctx):
    authority,_=contact_authority(ctx,active=False)
    return authority['contact_repair_package']


def numerical_scope(ctx):
    from .case_contact_numerics import PACKAGE_SHA as numeric_sha
    return _binding(ctx)['sha256']==numeric_sha


def planning_context(ctx):
    """No implicit authority: ordinary requests retain their existing prompt."""
    if not (ctx.get('runtime_authority') or {}):return None
    from .case_requests import authority_for
    if 'contact_repair_package' not in authority_for(ctx):return None
    authority,package=contact_authority(ctx)
    from .case_contact_numerics import source_revision
    binding=authority['contact_repair_package'];family=reviewed_family(package_sha=binding['sha256'],authority_sha=ctx['runtime_authority']['sha256'],revision=source_revision(ctx))
    proposal=package.get('approved_proposal',package)
    result=dict(scope=deepcopy(CONTACT_SCOPE_PACKET),approved_package_path=binding['path'],approved_package_sha256=binding['sha256'],
                allowed_scenarios=[dict(stepH=h,stepD=d) for h,d in SCENARIOS],frozen=proposal['frozen'],
                allowed_changes=proposal['allowed_changes'],reviewed_contact_family=family)
    if numerical_scope(ctx):
        from .case_contact_numerics import reference
        result.update(numerical_only=True,repair_reference=reference(ctx))
        if source_revision(ctx):
            from .case_contact_numerics import revision_context
            result['source_revision']=revision_context(ctx)
    return result


def validate_contact_plan(packet, baseline, ctx):
    contact_authority(ctx)
    from .case_scope import modelica_parameters,UnsupportedRequestDesign
    p=packet['proposed_parameters'];before=modelica_parameters(baseline['artifact']['modelica_source'])[0]
    if not isinstance(p,dict) or (p.get('stepH'),p.get('stepD')) not in SCENARIOS:raise UnsupportedRequestDesign('contact repair stair scenario is outside the three approved points')
    if any(before[k]!=v or p.get(k)!=v for k,v in dict(m=85.,I_pitch=20.,T_main_max=60.,T_nose_max=25.).items()):raise UnsupportedRequestDesign('contact repair freezes original85 mass/inertia/torques')


def source_sha(source):
    return hashlib.sha256(source.encode('utf8')).hexdigest()


def _source_identity(source):
    from .case_scope import strip_comments,STAIR_PARAMETERS
    # Unlike the historical family helper, do not mask observation settings.
    value=strip_comments(source)
    for name in STAIR_PARAMETERS:
        pattern=r'(\bparameter\s+(?:Real|Integer|Modelica\.Units\.SI\.\w+)\s+'+name+r'\s*=\s*)([^;]+)(;)'
        value,count=re.subn(pattern,lambda m:m[1]+'APPROVED_VALUE'+m[3],value)
        if count!=1:raise ValueError('contact stair parameter must be uniquely declared: '+name)
    return sha_value(''.join(re.findall(r'"(?:\\.|[^"\\])*"|\S',value)))


def _read_registration(path):
    index=read_json(path)
    if index.get('schema')!='v12-contact-family-registration-1':raise ValueError('unknown contact family registration')
    receipt_path=_local(index['source_review_path']);receipt=read_json(receipt_path)
    if sha_file(receipt_path)!=index.get('source_review_sha256'):raise ValueError('contact source review receipt drift')
    from .case_contact_numerics import PACKAGE_SHA as numeric_sha,PACKAGE_PATH as numeric_path
    approved={PACKAGE_SHA:PACKAGE_PATH,numeric_sha:numeric_path};package_path=approved.get(receipt.get('approved_package_sha256'))
    if receipt.get('schema')!='v12-contact-source-review-1' or receipt.get('channel')!='TRUSTED_LOCAL_DEVELOPMENT_REVIEW' or receipt.get('product_self_approval') is not False or package_path is None or not receipt.get('review_basis'):
        raise ValueError('untrusted contact source review receipt')
    if receipt.get('allowed_scenarios')!=[dict(stepH=h,stepD=d) for h,d in SCENARIOS]:raise ValueError('contact source review must bind exactly the three approved scenarios')
    if sha_file(package_path)!=receipt['approved_package_sha256'] or _local(receipt['approved_package_path'])!=package_path.resolve():raise ValueError('contact reviewed package drift')
    source=receipt['reviewed_modelica_source']
    if source_sha(source)!=receipt.get('modelica_source_sha256') or _source_identity(source)!=receipt.get('family_shape_sha256'):raise ValueError('contact reviewed source/shape drift')
    from .case_requests import load_request,authority_for
    ctx=load_request(receipt['request_id']);authority_for(ctx)
    if any(receipt.get(key)!=ctx.get(key) for key in ('request_digest','displayed_digest','baseline_ref','project_id')) or receipt['runtime_authority_sha256']!=ctx['runtime_authority']['sha256']:
        raise ValueError('contact source review request/authority drift')
    contact_authority(ctx,active=False)
    from .case_contact_numerics import source_revision,verify_numerical_delta
    if receipt.get('source_revision',0)!=source_revision(ctx):raise ValueError('contact source revision binding drift')
    if numerical_scope(ctx) and receipt.get('contact_domain')!=verify_numerical_delta(ctx,source):raise ValueError('numerical contact domain or token-lock drift')
    if not ctx.get('confirmation_path') or sha_file(_local(ctx['confirmation_path']))!=receipt.get('confirmation_sha256'):raise ValueError('contact source review confirmation drift')
    pending=read_json(Path(ctx['root'])/'contact-source-review-pending.json')
    if sha_value(pending)!=receipt['pending_sha256'] or pending['spec']['artifact']['modelica_source']!=source:raise ValueError('contact review candidate drift')
    response=read_json(_local(pending['source_response_path']))
    if sha_value(response)!=pending['source_response_sha256']:raise ValueError('contact review product response drift')
    _check_pending_product_source(ctx,pending,response)
    return receipt


def reviewed_family(*,package_sha=PACKAGE_SHA,authority_sha=None,revision=0):
    matches=[]
    for path in sorted(REGISTRY_ROOT.glob('*.json')):
        receipt=_read_registration(path)
        if (package_sha is None or receipt['approved_package_sha256']==package_sha) and (authority_sha is None or receipt['runtime_authority_sha256']==authority_sha) and (revision is None or receipt.get('source_revision',0)==revision):matches.append(receipt)
    if len(matches)>1:raise ValueError('multiple contact formulations registered for one approved design')
    return matches[0] if matches else None


def check_contact_source(spec,ctx):
    contact_authority(ctx)
    from .case_scope import modelica_parameters,UnsupportedRequestDesign,STAIR_PARAMETERS
    old,expressions,kinds=modelica_parameters(ctx['baseline_snapshot']['frozen_spec']['artifact']['modelica_source'])
    values,after,after_kinds=modelica_parameters(spec['artifact']['modelica_source'])
    for name in old:
        unchanged=name in after and ast.dump(ast.parse(after[name],mode='eval').body)==ast.dump(ast.parse(expressions[name],mode='eval').body)
        if after_kinds.get(name)!=kinds[name] or (name not in STAIR_PARAMETERS and not unchanged):raise UnsupportedRequestDesign('contact repair changed a frozen parameter declaration: '+name)
    binding=_binding(ctx)
    if numerical_scope(ctx):
        from .case_contact_numerics import verify_numerical_delta
        verify_numerical_delta(ctx,spec['artifact']['modelica_source'])
    from .case_contact_numerics import source_revision
    family=reviewed_family(package_sha=binding['sha256'],authority_sha=ctx['runtime_authority']['sha256'],revision=source_revision(ctx))
    if family is None:raise ContactSourceReviewRequired(spec)
    if _source_identity(spec['artifact']['modelica_source'])!=family['family_shape_sha256']:raise UnsupportedRequestDesign('contact source differs from the one exact reviewed family beyond stair dimensions')
    from .case_requests import request_root
    return dict(source_review_path=str(request_root(family['request_id'])/'contact-source-review.json'),
                source_review_sha256=sha_value(family),approved_package_sha256=binding['sha256'],family_shape_sha256=family['family_shape_sha256'])


def save_pending(ctx,response,stage,spec,completion):
    path=Path(ctx['root'])/'contact-source-review-pending.json'
    value=dict(schema='v12-contact-source-review-pending-1',status='REVIEW_REQUIRED',request_id=ctx['request_id'],request_digest=ctx['request_digest'],
               displayed_digest=ctx['displayed_digest'],baseline_ref=ctx['baseline_ref'],runtime_authority_sha256=ctx['runtime_authority']['sha256'],
               approved_package_sha256=_binding(ctx)['sha256'],source_response_path=str(Path(ctx['root'])/'turns'/stage/'response.json'),
               source_response_sha256=sha_value(response),stage=stage,spec=spec,spec_digest=sha_value(spec),
               modelica_source_sha256=source_sha(spec['artifact']['modelica_source']),sysml_source_sha256=source_sha(spec['artifact']['sysml_source']),
               baseline_completion=completion,software_repair_added=False,product_http_retry_required=False,proposal_prepared=False)
    save_json(path,value,immutable=True)
    return dict(status='REVIEW_REQUIRED',pending_path=str(path),modelica_source_sha256=value['modelica_source_sha256'],spec_digest=value['spec_digest'],software_repair_added=False,product_http_added=0,proposal_prepared=False)


def pending_response(ctx):
    pending=read_json(Path(ctx['root'])/'contact-source-review-pending.json')
    if any(pending.get(k)!=ctx.get(k) for k in ('request_id','request_digest','displayed_digest','baseline_ref')) or pending.get('runtime_authority_sha256')!=ctx['runtime_authority']['sha256'] or pending.get('approved_package_sha256')!=_binding(ctx)['sha256'] or sha_value(pending['spec'])!=pending['spec_digest']:
        raise ValueError('pending contact candidate/request/confirmation drift')
    path=_local(pending['source_response_path'])
    if path!=Path(ctx['root'])/'turns'/pending['stage']/'response.json':raise ValueError('pending product response path drift')
    response=read_json(path)
    if sha_value(response)!=pending['source_response_sha256']:raise ValueError('pending product response drift')
    _check_pending_product_source(ctx,pending,response)
    return pending,response


def _check_pending_product_source(ctx,pending,response):
    from .case_taskflow import generation_arguments
    from .case_backend import module
    from .case_taskflow import baseline_sysml_available
    args,_=generation_arguments(response,needs_baseline=not baseline_sysml_available(ctx))
    product=args['spec']['artifact'];saved=pending['spec']['artifact']
    normalized_sysml=module('artifact_sysml').normalize_package_wrapper(product.get('sysml_source'))[0]
    if product.get('modelica_source')!=saved.get('modelica_source') or normalized_sysml!=saved.get('sysml_source') or source_sha(saved['modelica_source'])!=pending['modelica_source_sha256'] or source_sha(saved['sysml_source'])!=pending['sysml_source_sha256']:
        raise ValueError('pending contact sources differ from the original product output')


def register_source_review(request_id,expected_source_sha256,review_basis):
    """Trusted local entry, never a product tool; no model edits or execution."""
    from .case_requests import load_request,require_execution
    ctx=require_execution(request_id);contact_authority(ctx)
    if not is_contact_scope(ctx.get('plan',{}).get('scope')):raise PermissionError('source review requires confirmed contact scope')
    if not isinstance(review_basis,str) or not review_basis.strip():raise ValueError('actual source review findings required')
    pending,response=pending_response(ctx);source=pending['spec']['artifact']['modelica_source']
    if expected_source_sha256!=source_sha(source):raise ValueError('reviewed exact source hash differs from product candidate')
    from .case_contact_numerics import source_revision
    revision=source_revision(ctx);binding=_binding(ctx);existing=reviewed_family(package_sha=binding['sha256'],authority_sha=ctx['runtime_authority']['sha256'],revision=revision)
    if existing is not None:
        if existing['request_id']!=request_id or existing['modelica_source_sha256']!=expected_source_sha256:raise PermissionError('one approved contact design already registered; no formulation replacement')
        return existing
    # Re-run the same local parsing/source checks before trusting a pending file;
    # this makes registration a review of actual product output, never a way
    # to install arbitrary hand-written Modelica through the pending journal.
    from .case_taskflow import TaskFlow
    flow=TaskFlow.load(request_id)
    try:flow._validate_generation(response,pending['stage'])
    except ContactSourceReviewRequired as exc:
        if exc.spec!=pending['spec'] or exc.completion!=pending['baseline_completion']:raise ValueError('pending candidate differs from revalidated product output')
    else:raise ValueError('source review requires the original unregistered product candidate')
    receipt=dict(schema='v12-contact-source-review-1',channel='TRUSTED_LOCAL_DEVELOPMENT_REVIEW',product_self_approval=False,
        request_id=request_id,request_digest=ctx['request_digest'],displayed_digest=ctx['displayed_digest'],project_id=ctx['project_id'],baseline_ref=ctx['baseline_ref'],
        runtime_authority_sha256=ctx['runtime_authority']['sha256'],confirmation_sha256=sha_file(ctx['confirmation_path']),approved_package_path=binding['path'],approved_package_sha256=binding['sha256'],
        pending_sha256=sha_value(pending),modelica_source_sha256=expected_source_sha256,sysml_source_sha256=pending['sysml_source_sha256'],
        reviewed_modelica_source=source,family_shape_sha256=_source_identity(source),review_basis=review_basis,
        reviewed_scope=deepcopy(CONTACT_SCOPE_PACKET),allowed_variable_parameters=['stepH','stepD'],allowed_scenarios=[dict(stepH=h,stepD=d) for h,d in SCENARIOS],
        formal_physical_acceptance=False,original_parameters_frozen=True,source_revision=revision,registered_at_utc=datetime.now(timezone.utc).isoformat())
    if numerical_scope(ctx):
        from .case_contact_numerics import verify_numerical_delta
        receipt['contact_domain']=verify_numerical_delta(ctx,source)
    path=Path(ctx['root'])/'contact-source-review.json';save_json(path,receipt,immutable=True)
    suffix=('-revision-'+str(revision)) if revision else ''
    save_json(REGISTRY_ROOT/(ctx['runtime_authority']['sha256']+suffix+'.json'),dict(schema='v12-contact-family-registration-1',source_review_path=str(path),source_review_sha256=sha_file(path)),immutable=True)
    return receipt


def support_for_reviewed_source(source):
    """Read-only recognition of a registered source, never model-name guessing."""
    try:identity=_source_identity(source)
    except ValueError:return None
    matches=[_read_registration(path) for path in sorted(REGISTRY_ROOT.glob('*.json'))]
    matches=[receipt for receipt in matches if receipt['family_shape_sha256']==identity]
    if not matches:return None
    family=matches[0]
    if any(row['modelica_source_sha256']!=family['modelica_source_sha256'] for row in matches):raise ValueError('ambiguous registered contact source identity')
    from .case_scope import modelica_parameters
    parameters=modelica_parameters(source)[0]
    if (parameters['stepH'],parameters['stepD']) not in STAIR_DIAGNOSTIC_SCENARIOS:return None
    domain=family.get('contact_domain')
    from .case_contact_numerics import REFERENCE_SHA
    if domain is None and family['modelica_source_sha256']==REFERENCE_SHA:
        domain=dict(schema='v12-reviewed-contact-domain-1',ground_pad_extension_m=100.,corner_regularization_distance_m=1e-9,reviewed_source_sha256=REFERENCE_SHA,family_shape_sha256=family['family_shape_sha256'],geometry_unchanged=True,reference_source_sha256=REFERENCE_SHA)
    return dict(status='SUPPORTED',family='source-review-bound-stair-contact-v1',parameters=parameters,contact_domain=deepcopy(domain),
        modelica_source_sha256=source_sha(source),reviewed_modelica_source_sha256=family['modelica_source_sha256'],family_shape_sha256=family['family_shape_sha256'],
        checker='case_checks',execution_authorized=False,physical_acceptance='NOT_VERIFIED',physical_model_status='CONTACT_SOURCE_REVIEWED_NOT_PHYSICALLY_CERTIFIED',
        execution_scope='SOURCE_REVIEW_BOUND_CONTACT_DIAGNOSTIC',source_review_sha256=sha_value(family),approved_package_sha256=family['approved_package_sha256'])


def reviewed_stair_capability(modelica_source):
    """Serializable, read-only input limits of this recognized baseline.

    Recognition reuses the existing exact reviewed-family checks. This does
    not register a family, authorize a run, or predict a vehicle result.
    """
    support = support_for_reviewed_source(modelica_source)
    if support is None:
        return None
    return dict(schema='v12-reviewed-stair-capability-1', family=support['family'],
                baseline_source_sha256=support['modelica_source_sha256'],
                family_shape_sha256=support['family_shape_sha256'],
                allowed_scenarios=[dict(stepH=h, stepD=d) for h, d in STAIR_DIAGNOSTIC_SCENARIOS],
                dimension_unit='m',
                frozen=['vehicle_mass_distribution_inertia', 'wheel_geometry_axle_positions',
                        'drive_torque_control', 'contact_equations_parameters',
                        'stair_count_platform_route_task', 'observation_solver_output_checks'],
                execution_scope='SOURCE_REVIEW_BOUND_CONTACT_DIAGNOSTIC',
                task_success='NOT_GUARANTEED', physical_acceptance='NOT_VERIFIED',
                execution_authorized=False,
                message='当前已审接触家族仅支持150×280、180×260、300×260、150×270、250×260 mm五个离散楼梯输入的有限同源诊断；'
                        '不是连续高深范围，不代表任何工况能爬过。其他H/D属于尚不支持的输入域，'
                        '不能当车辆失败或靠协议重试扩大；车辆、接触、控制、任务及仿真设置冻结，实际运行仍需另有有限授权。')
