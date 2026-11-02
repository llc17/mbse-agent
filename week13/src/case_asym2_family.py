"""Pinned Asym2 source+wrapper adapter; no generated vehicle or authority."""
from copy import deepcopy
import ast
import hashlib
import json
import math
from pathlib import Path
import re

from .case_paths import ROOT

FAMILY = 'sixwheel_full_mission_asym2_v1'
MODEL_NAME = 'ZLTECH_Full_Mission_Asym2'
ORIGINAL_SOURCE_PATH = ROOT/'sixwheel_fix/tail80_tuning_20261030/builds/92892efa7cf4e794/source.mo'
ORIGINAL_WRAPPER_PATH = ORIGINAL_SOURCE_PATH.with_name('variant.mo')
SOURCE_PATH = ROOT/'src/asym2/reference-source.mo'
WRAPPER_PATH = ROOT/'src/asym2/reference-variant.mo'
SOURCE_SHA = '4828ea546e61355c40fee44d001e6b3fc010e8d2fae59c8e762ee4cb67aab5a0'
WRAPPER_SHA = '4e5454825c029fc473884c7a5695b2ba736ef624fc43518f09e5f20065e5943c'
PLAN_PARAMETERS = ('m','I_pitch','T_main_max','T_nose_max','stepH','stepD')
VARIABLE_PARAMETERS = ('m','stepH','stepD')
OVERRIDES = dict(n_steps=20,stepH=.08,stepD=.28,h19=.0775,h20=.065,d19=.22,d20=.08)
DERIVED_NAMES = ('x_last_up','x_end_up','x_start_down','x_last_down','x_end_down','H_peak','nReach','nLocal','nContact')
OUTPUT_NAMES = ('x','z','v','vz','psi','omega_psi','x_n','z_n','x_f','z_f','x_r','z_r',
                'maxCompression','Ekin','Epot','Ucontact','Etotal','Pdrive','Pdiss','Wdrive','Wdiss','energyResidual')


def _sha(raw): return hashlib.sha256(raw).hexdigest()


def _replace(source,name,value):
    pattern=r'(\bparameter\s+(?:Real|Integer)\s+'+re.escape(name)+r'\s*=\s*)([^;\"\r\n]+)'
    source,count=re.subn(pattern,lambda m:m[1]+str(value),source)
    if count!=1:raise ValueError('Asym2 scalar declaration is not unique: '+name)
    return source


def effective_source():
    """Expand the exact approved wrapper, preserving equations and all originals."""
    raw=SOURCE_PATH.read_bytes();wrapper=WRAPPER_PATH.read_bytes()
    if _sha(raw)!=SOURCE_SHA or _sha(wrapper)!=WRAPPER_SHA:raise ValueError('pinned Asym2 source/wrapper drift')
    source=raw.decode('utf-8-sig').replace('\r\n','\n')
    for name,value in OVERRIDES.items():source=_replace(source,name,value)
    return source


def _shape(source):
    from .case_scope import strip_comments
    value=strip_comments(source)
    for name in VARIABLE_PARAMETERS:value=_replace(value,name,'APPROVED_VALUE')
    # Tokenization retains whitespace INSIDE strings and refuses token merging.
    return re.findall(r'"(?:\\.|[^"\\])*"|[A-Za-z_]\w*|\d+(?:\.\d*)?(?:[eE][-+]?\d+)?|[^\s]',value)


def is_asym2_source(source):
    if not isinstance(source,str) or not re.search(r'\bmodel\s+'+MODEL_NAME+r'\b',source):return False
    try:return _shape(source)==_shape(effective_source())
    except (ValueError,OSError):return False


def expression_value(expression,values):
    """Finite arithmetic/1D arrays and the four pinned Modelica functions."""
    def walk(node):
        if isinstance(node,ast.Constant) and type(node.value) in (int,float):return float(node.value)
        if isinstance(node,ast.Name) and node.id in values:return values[node.id]
        if isinstance(node,(ast.List,ast.Tuple)):return [walk(x) for x in node.elts]
        if isinstance(node,ast.UnaryOp) and isinstance(node.op,(ast.UAdd,ast.USub)):
            value=walk(node.operand);return value if isinstance(node.op,ast.UAdd) else -value
        if isinstance(node,ast.BinOp) and isinstance(node.op,(ast.Add,ast.Sub,ast.Mult,ast.Div)):
            a,b=walk(node.left),walk(node.right)
            if isinstance(a,list) or isinstance(b,list):raise ValueError('array arithmetic is not supported')
            return {ast.Add:lambda:a+b,ast.Sub:lambda:a-b,ast.Mult:lambda:a*b,ast.Div:lambda:a/b}[type(node.op)]()
        if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and not node.keywords:
            args=[walk(x) for x in node.args];name=node.func.id
            if name in ('min','max'):
                args=args[0] if len(args)==1 and isinstance(args[0],list) else args
                if not args or any(isinstance(x,list) for x in args):raise ValueError('invalid min/max arguments')
                return (min if name=='min' else max)(args)
            if name in ('ceil','integer') and len(args)==1 and not isinstance(args[0],list):
                return float(math.ceil(args[0]) if name=='ceil' else math.trunc(args[0]))
        raise ValueError('unsupported Asym2 parameter expression: '+expression)
    if not isinstance(expression,str) or len(expression)>512:raise ValueError('invalid Asym2 expression')
    try:result=walk(ast.parse(expression.replace('{','[').replace('}',']'),mode='eval').body)
    except (SyntaxError,ZeroDivisionError,OverflowError,TypeError) as exc:raise ValueError('invalid finite Asym2 expression') from exc
    if any(not math.isfinite(x) for x in (result if isinstance(result,list) else [result])):raise ValueError('nonfinite Asym2 parameter')
    return result


def modelica_parameters(source):
    from .case_scope import strip_comments
    pending={};kinds={};sizes={}
    for statement in re.findall(r'\bparameter\b(?:[^;\"]|"(?:\\.|[^"\\])*")*;',strip_comments(source)):
        match=re.fullmatch(r'parameter\s+(Real|Integer)\s+([A-Za-z_]\w*)(?:\[(\d+)\])?\s*=\s*(.*?)\s*(?:"(?:\\.|[^"\\])*"\s*)?;',statement,re.S)
        if not match:raise ValueError('unsupported Asym2 parameter declaration: '+statement[:120])
        kind,name,size,expr=match.groups()
        if name in pending:raise ValueError('duplicate Asym2 parameter: '+name)
        pending[name]=expr.strip();kinds[name]=kind;sizes[name]=int(size) if size else None
    expressions=dict(pending);values={}
    while pending:
        progress=False
        for name,expr in list(pending.items()):
            try:value=expression_value(expr,values)
            except ValueError:continue
            if (isinstance(value,list) and sizes[name]!=len(value)) or (not isinstance(value,list) and sizes[name] is not None):raise ValueError('Asym2 array dimension mismatch: '+name)
            if kinds[name]=='Integer' and (isinstance(value,list) or not float(value).is_integer()):raise ValueError('invalid Asym2 Integer: '+name)
            values[name]=value;del pending[name];progress=True
        if not progress:raise ValueError('cyclic/unsupported Asym2 expressions: '+','.join(pending))
    return values,expressions,kinds


def support_for_source(source):
    if not is_asym2_source(source):return None
    values=modelica_parameters(source)[0]
    if any(values[name]<=0 for name in VARIABLE_PARAMETERS):return None
    return dict(status='SUPPORTED',family=FAMILY,parameters=values,checker='case_asym2_execution',
        scope='固定非均匀20级Asym2模型；仅独立原重心点载或主体H/D改型，固定尾阶及全部车辆/接触/控制。',
        execution_authorized=False,physical_acceptance='NOT_VERIFIED',execution_scope='SOURCE_BOUND_ASYM2_DIAGNOSTIC_ONLY',
        physical_model_status='SOURCE_BOUND_FINITE_CONTACT_DIAGNOSTIC')


def prepare_input_bundle(destination):
    """Trusted import preparation only; never writes a product candidate."""
    destination=Path(destination).resolve()
    if not destination.is_relative_to(ROOT.resolve()) or destination.is_relative_to((ROOT/'V12_old').resolve()):raise ValueError('Asym2 import bundle must stay in current week13')
    source=effective_source();destination.mkdir(parents=True,exist_ok=False)
    original=destination/'originals';original.mkdir()
    (original/'source.mo').write_bytes(SOURCE_PATH.read_bytes());(original/'variant.mo').write_bytes(WRAPPER_PATH.read_bytes())
    (destination/'effective-baseline.mo').write_text(source,encoding='utf8',newline='')
    provenance=dict(schema='v12-asym2-input-provenance-1',family=FAMILY,source=str(ORIGINAL_SOURCE_PATH),source_sha256=SOURCE_SHA,wrapper=str(ORIGINAL_WRAPPER_PATH),wrapper_sha256=WRAPPER_SHA,pinned_source=str(SOURCE_PATH),pinned_wrapper=str(WRAPPER_PATH),effective_source_sha256=_sha(source.encode()),overrides=OVERRIDES,parameters=modelica_parameters(source)[0],method='EXACT_WRAPPER_SCALAR_EXPANSION_NO_EQUATION_CHANGE',execution_authorized=False)
    (destination/'provenance.txt').write_text(json.dumps(provenance,ensure_ascii=False,indent=2),encoding='utf8')
    return dict(path=str(destination),model='effective-baseline.mo',model_name=MODEL_NAME,provenance=provenance)


def expected_parameters(spec):
    """Expected values originate from the committed plan, never actual source."""
    rows=spec['design']['parameters'];proposed={}
    for name in PLAN_PARAMETERS:
        matches=[r for r in rows if r.get('name')==name]
        if len(matches)!=1 or type(matches[0].get('value')) not in (int,float) or not math.isfinite(matches[0]['value']):raise ValueError('confirmed Asym2 parameter missing/ambiguous: '+name)
        proposed[name]=matches[0]['value']
    if any(proposed[k]!=v for k,v in dict(I_pitch=20.,T_main_max=40.,T_nose_max=17.).items()):raise ValueError('confirmed Asym2 frozen inertia/torque differs')
    source=effective_source()
    for name in VARIABLE_PARAMETERS:source=_replace(source,name,proposed[name])
    return modelica_parameters(source)[0]


def derived_values(values,expressions,proposed):
    current=dict(values);current.update(proposed);derived={}
    pending={name:expressions[name] for name in DERIVED_NAMES}
    while pending:
        for name,expr in list(pending.items()):
            value=expression_value(expr,current);derived[name]=value;current[name]=value;del pending[name]
    return derived


def sysml_parameters(source):
    """Scalar mirrors for finite arrays; nReach is a checked evaluated function."""
    values,expressions,kinds=modelica_parameters(source);flat={};expr={};types={}
    for name,value in values.items():
        if isinstance(value,list):
            elements=expressions[name].strip()[1:-1].split(',')
            for i,item in enumerate(value,1):
                key=name+'_'+str(i);flat[key]=item;expr[key]=elements[i-1].strip();types[key]=kinds[name]
        else:
            flat[name]=value;expr[name]=str(int(value)) if name=='nReach' else expressions[name];types[name]=kinds[name]
    return flat,expr,types


def context_contract(source,proposed=None):
    baseline_mirrors=sysml_parameters(source)
    if proposed:
        for name in VARIABLE_PARAMETERS:source=_replace(source,name,proposed[name])
    values,expressions,kinds=sysml_parameters(source)
    return dict(family=FAMILY,model_name=MODEL_NAME,source_sha256=SOURCE_SHA,wrapper_sha256=WRAPPER_SHA,
        allowed_direct_parameters=list(VARIABLE_PARAMETERS),frozen_tail={k:v for k,v in OVERRIDES.items() if k not in ('stepH','stepD')},
        derived_expressions={k:modelica_parameters(source)[1][k] for k in DERIVED_NAMES},
        sysml_scalar_mirrors=[dict(name=k,value=values[k],expression=expressions[k],kind=kinds[k]) for k in values],
        baseline_sysml_scalar_mirrors=[dict(name=k,value=baseline_mirrors[0][k],expression=baseline_mirrors[1][k],kind=baseline_mirrors[2][k]) for k in baseline_mirrors[0]],
        observation=dict(stop_time=300,intervals=30000,solver='dassl',tolerance=1e-5,max_step_s=.002))


SYSML_GUIDE='''Asym2专用共享参数映射：沿用合法package/part def System/part system:System/requirement req_N结构。数组不使用SysML数组语法；每个一维Modelica数组逐元素展平为attribute wheel_J_1、wheel_J_2、wheel_J_3等，R/Tmax/bodyX/bodyZ/u0同理，保留各元素原算术表达式。nReach涉及integer/ceil/max/min，唯一例外为其已独立计算的整数常量；nLocal/nContact及其他全部标量保持原依赖表达式。必须逐项使用asym2_family_contract.sysml_scalar_mirrors的名字、expression和kind；不漏数组、不另写旧均匀楼梯公式。此映射只核有限参数/表达式与来源，不宣称方程等价或硬件认证。'''
PLAN_PROMPT='''你是已有MBSE产品Agent，当前为独立新家族sixwheel_full_mission_asym2_v1。按用户原文和所选baseline提出新方案，只调用case_request_plan一次。packet字段与现工具schema一致。允许两个独立范围：原重心理想点载仅改m（I_pitch20及扭矩40/17不变），使用center_of_mass_point_load原scope；或仅主体stepH/stepD，使用stair_dimensions原scope。原样复算走CHANGE点载范围，零新增质量。两种范围的proposed_parameters均恰为m/I_pitch/T_main_max/T_nose_max/stepH/stepD六项，design.parameters逐项一致，不混改质量与楼梯。scope必须用现有完整packet值。尾阶h19=.0775/h20=.065/d19=.22/d20=.08、n_steps20/nEndSteps2/fixedTopTread.28/len_flat2及全部车/数组wheel_J/函数/接触/控制冻结。主体H仅前18级，主体D仅前17段间距；派生严格使用所选Asym2源码表达式，不用旧均匀n_steps*H/D公式。300s只是观察区间，DASSL/tol1e-5/maxStep.002/30000输出间隔冻结。质量严格从所选baseline_snapshot的实际源码/参数及可信用户来源读取，区分所选版本总质量、空车质量与本次新增载荷；不得固定空车质量或用旧示例替代当前来源。所选基线已带载时，不能自动将其总质量当作空车质量；缺少决定目标总质量的关键事实才集中澄清。本次新增原重心理想点载应在所选基线总质量上相加，并披露已有载荷与新增载荷，不能把旧版本结果转记为新质量结果；I与轮J冻结。要求缺关键事实才NEEDS_INPUT；基准缺SysML由产品在确认后补齐并在defaults披露，不索要用户手写。requirements必须精确绑定trusted_sources至少一项引用本次请求，来源/结构与通用工具协议相同；design含summary/assumptions/parameters，所有metadata origin=model。defaults/unknowns/missing/unsupported为字符串列表，rationale非空中文。已有新接触方程完整继承，不能套旧家族接触缺陷说明，也不能声称已有物理通过。真实运行才确定PASS/有效受阻/观察未完/模型无效/工具故障，不能改车调参凑PASS或虚构实物认证。'''
GEN_PROMPT='''你是同一MBSE产品Agent，按confirmed_plan生成完整Asym2候选Modelica及SysML，只调用proposal_prepare一次。arguments={"spec":{完整对象}}；原基准缺SysML时按程序追加baseline_sysml_source。spec严格沿trusted_generation_contract，字段恰为schema_version/backend/baseline_ref/artifact/sources/requirements/design/libraries/settings/outputs/checks（已有trajectory_contract才原样继承）。sources按顺序只返回{id}引用，可信原文由Store读取；requirements/design原样。Modelica从baseline_snapshot完整复制，只替换confirmed_plan允许的m或主体stepH/stepD数值，不能改I/扭矩/轮J/尾阶/函数/方程/控制/初始化/类名/注解。保留所有Asym2非均匀阶梯派生表达式，不写旧均匀公式或写成常量。settings固定300/30000，完整新数组由专用checker读全CSV，outputs原样标量映射。候选SysML同步映射全部标量/数组；参考asym2_family_contract及sysml_contract，不漏参数。源码解码后真实换行，JSON仅一层；不得修改元数据合同、确认、执行、注册家族或宣称PASS。'''
