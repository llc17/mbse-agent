"""Finite success-case family support; authorization is data, never model output."""
import ast
import math
import re
from .case_paths import authorization, authorization_sha, RUN, read_json, sha_value

NUMBER = r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?'

def strip_comments(source):
    return re.sub(r'"(?:\\.|[^"\\])*"|/\*[\s\S]*?\*/|//[^\r\n]*', lambda m: m[0] if m[0].startswith('"') else ' ' * len(m[0]), source)

def expression_value(expression, values):
    """Only finite arithmetic and earlier/declared shared parameters, no eval."""
    def walk(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float): return float(node.value)
        if isinstance(node, ast.Name) and node.id in values: return values[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = walk(node.operand); return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            left, right = walk(node.left), walk(node.right)
            return {ast.Add: lambda: left + right, ast.Sub: lambda: left - right, ast.Mult: lambda: left * right, ast.Div: lambda: left / right}[type(node.op)]()
        raise ValueError('unsupported or unresolved shared parameter expression: ' + expression)
    if not isinstance(expression, str) or len(expression) > 512: raise ValueError('invalid expression')
    try: value = walk(ast.parse(expression.strip(), mode='eval').body)
    except (SyntaxError, ZeroDivisionError, OverflowError) as exc: raise ValueError('invalid arithmetic expression') from exc
    if not math.isfinite(value): raise ValueError('nonfinite shared parameter')
    return value

def modelica_parameters(source):
    from .case_asym2_family import MODEL_NAME,modelica_parameters as asym2_parameters
    if re.search(r'\bmodel\s+'+MODEL_NAME+r'\b',strip_comments(source)):
        return asym2_parameters(source)
    clean = strip_comments(source)
    pending = {}; kinds = {}
    for statement in re.findall(r'\bparameter\b[^;]*;', clean):
        match = re.fullmatch(r'parameter\s+(Real|Integer|Modelica\.Units\.SI\.\w+)\s+([A-Za-z_]\w*)\s*=\s*(.*?)\s*(?:"(?:\\.|[^"\\])*"\s*)?;', statement, re.S)
        if not match: raise ValueError('unsupported shared parameter declaration: ' + statement[:180])
        kind, name, expr = match.groups()
        if name in pending: raise ValueError('duplicate shared parameter: ' + name)
        pending[name] = expr.strip(); kinds[name] = kind
    expressions = dict(pending); values = {}
    while pending:
        progress = False
        for name, expression in list(pending.items()):
            try: value = expression_value(expression, values)
            except ValueError: continue
            if kinds[name] == 'Integer' and not value.is_integer(): raise ValueError('nonintegral Integer parameter')
            values[name] = value; del pending[name]; progress = True
        if not progress: raise ValueError('cyclic/unsupported shared expressions: ' + ', '.join(pending))
    return values, expressions, kinds

def model_shape(source, variable_names):
    source = strip_comments(source)
    # Comments may differ; strings, equation tokens and dependency expressions may not.
    for name in variable_names:
        pattern = r'(\bparameter\s+(?:Real|Integer|Modelica\.Units\.SI\.\w+)\s+' + re.escape(name) + r'\s*=\s*)([^;]+)(;)'
        source, count = re.subn(pattern, lambda m: m[1] + 'APPROVED_VALUE' + m[3], source)
        if count != 1: raise ValueError('approved parameter is not uniquely declared: ' + name)
    source = re.sub(r'\b(StopTime|Tolerance|Interval)\s*=\s*' + NUMBER, lambda m: m[1] + '=APPROVED_SETTING', source)
    return re.sub(r'\s+', '', source)

def check_delta(service, spec, *, task_context=None):
    if task_context is not None:return check_request_delta(service,spec,task_context)
    a = authorization(); imported = read_json(RUN / 'imported-reference.json')
    baseline = imported['frozen_spec']; raw = baseline['artifact']['modelica_source']
    if service._current_project()['project_id'] != imported['project_id']: raise ValueError('wrong imported case project')
    if spec.get('baseline_ref') != {k: imported[k] for k in ('project_id', 'task_id', 'version_id', 'spec_digest', 'source_sha256', 'manifest_sha256')}: raise ValueError('candidate baseline binding differs')
    if spec['artifact']['model_name'] != baseline['artifact']['model_name']: raise ValueError('model class changed beyond inherited family')
    changed = ('m', 'T_main_max', 'T_nose_max')
    if model_shape(spec['artifact']['modelica_source'], changed) != model_shape(raw, changed): raise ValueError('Modelica physics/control/geometry changed beyond allowed scope')
    values, expressions, _ = modelica_parameters(spec['artifact']['modelica_source'])
    before, _, _ = modelica_parameters(raw)
    if values['m'] != a['baseline']['mass_kg_after'] or values['I_pitch'] != a['baseline']['pitch_inertia']: raise ValueError('approved mass/inertia assumption differs')
    for name, key in [('T_main_max', 'main_torque_range'), ('T_nose_max', 'nose_torque_range')]:
        low, high = a['baseline'][key]
        if not low <= values[name] <= high: raise ValueError('equivalent torque outside approved range: ' + name)
    for name in set(before) - set(changed):
        if values.get(name) != before[name]: raise ValueError('frozen parameter changed: ' + name)
    if spec['libraries'] != baseline['libraries'] or spec['outputs'] != baseline['outputs'] or spec['checks'] != [] or spec.get('trajectory_contract') is not None: raise ValueError('outputs/libraries/task conditions changed')
    stop = spec['settings']['stop_time']; interval = a['observation']['output_interval']
    if stop < a['observation']['initial_stop_time'] or stop > a['observation']['maximum_stop_time'] or spec['settings']['intervals'] != round(stop / interval): raise ValueError('unsupported observation interval')
    # Annotation is a product-source setting too, even though the runner overrides it explicitly.
    for field, expected in [('StopTime', stop), ('Interval', interval)]:
        matches = re.findall(r'\b' + field + r'\s*=\s*(' + NUMBER + ')', strip_comments(spec['artifact']['modelica_source']))
        if len(matches) != 1 or not math.isclose(float(matches[0]), expected, rel_tol=1e-12, abs_tol=1e-12): raise ValueError('source observation setting differs: ' + field)
    tolerances = re.findall(r'\bTolerance\s*=\s*(' + NUMBER + ')', strip_comments(spec['artifact']['modelica_source']))
    if len(tolerances) != 1 or float(tolerances[0]) not in (.01, .0001): raise ValueError('unsupported source tolerance')
    design = read_json(RUN / 'design.json')
    for name in ('T_main_max', 'T_nose_max'):
        if not math.isclose(values[name], design['proposed_parameters'][name], rel_tol=1e-12, abs_tol=1e-12): raise ValueError('generated source differs from product design: ' + name)
    if spec['design'] != design['candidate_design'] or spec['requirements'] != design['candidate_requirements']: raise ValueError('generated design/requirements differ from saved product design')
    expected_sources = read_json(RUN / 'run.json')['trusted_sources']
    if spec['sources'] != expected_sources: raise ValueError('source snapshot differs')
    return dict(status='PASS_APPROVED_FAMILY_DELTA',before={n: before[n] for n in changed},after={n:values[n] for n in changed},parameters=values,expressions=expressions,equation_control_geometry_token_lock='MATCH',scope='INHERITED_SIMPLIFIED_MODEL_ONLY',authorization_sha256=authorization_sha(),source_tolerance=float(tolerances[0]))

REQUEST_PARAMETERS=('m','I_pitch','T_main_max','T_nose_max')
REQUEST_SCOPE='center_of_mass_point_load'
STAIR_PARAMETERS=('stepH','stepD')
STAIR_SCOPE='stair_dimensions'
STAIR_PLAN_PARAMETERS=REQUEST_PARAMETERS+STAIR_PARAMETERS
# Family recognition only: an individual request never receives this union as
# its source-edit authority. Mass and stair scopes remain strictly separate.
FAMILY_VARIABLE_PARAMETERS=('m','T_main_max','T_nose_max')+STAIR_PARAMETERS
MASS_SCOPE_PACKET=dict(change_kind=REQUEST_SCOPE,mass_location='original_center_of_mass',inertia_assumption='ideal_point_mass',frozen='geometry_contact_control_terrain_task')
STAIR_SCOPE_PACKET=dict(change_kind=STAIR_SCOPE,frozen='vehicle_geometry_contact_control_stair_count_platform_task_observation')
STAIR_DERIVED_EXPRESSIONS={
    'x_end_up':'x_start_up + n_steps * stepD',
    'x_start_down':'x_end_up + len_flat',
    'x_end_down':'x_start_down + n_steps * stepD',
    'H_peak':'n_steps * stepH',
}
STAIR_DERIVED_PARAMETERS=tuple(STAIR_DERIVED_EXPRESSIONS)
COMPILED_PARAMETER_NAMES=STAIR_PLAN_PARAMETERS+('n_steps','x_start_up','len_flat')+STAIR_DERIVED_PARAMETERS
class UnsupportedRequestDesign(ValueError):pass

def stair_derived_values(values,expressions,proposed):
    """Recompute only the original four dependencies; never infer a new route."""
    current=dict(values);current.update(proposed);derived={}
    for name,formula in STAIR_DERIVED_EXPRESSIONS.items():
        if name not in expressions or ast.dump(ast.parse(expressions[name],mode='eval').body)!=ast.dump(ast.parse(formula,mode='eval').body):
            raise UnsupportedRequestDesign('inherited stair dependency must preserve the original formula: '+name)
        derived[name]=expression_value(expressions[name],current);current[name]=derived[name]
    if any(current[name]<=0 for name in ('stepH','stepD','n_steps','len_flat')) or not current['x_start_up']<current['x_end_up']<current['x_start_down']<current['x_end_down']:
        raise UnsupportedRequestDesign('stair dimensions must form a positive finite inherited route')
    return derived

def validate_request_plan(packet,baseline,sources,*,task_context=None):
    """Check product interpretation against the finite inherited family.

    This checks the model's structured answer; it never parses a user's mass
    from natural language or supplies a missing design value.
    """
    fields={'intent','support','missing','unsupported','requirements','design','proposed_parameters','defaults','unknowns','rationale','scope'}
    if not isinstance(packet,dict) or set(packet)!=fields:raise ValueError('request plan fields differ')
    if packet['intent'] not in ('CHANGE','QUESTION') or packet['support'] not in ('SUPPORTED','NEEDS_INPUT','UNSUPPORTED'):raise ValueError('invalid product intent/support')
    for name in ('missing','unsupported','defaults','unknowns'):
        if not isinstance(packet[name],list) or any(not isinstance(x,str) or not x.strip() for x in packet[name]):raise ValueError('invalid '+name)
    if not isinstance(packet['rationale'],str) or not packet['rationale'].strip():raise ValueError('plan rationale required')
    if packet['support']=='NEEDS_INPUT' and not packet['missing']:raise ValueError('needs-input must identify missing facts')
    if packet['support']=='UNSUPPORTED' and not packet['unsupported']:raise ValueError('unsupported must identify the unsupported change')
    if packet['intent']=='QUESTION' or packet['support']!='SUPPORTED':
        if packet['proposed_parameters'] or packet['design'] or packet['requirements'] or packet['scope']:raise ValueError('question/incomplete/unsupported plan cannot contain an executable design')
        return dict(executable=False,intent=packet['intent'],support=packet['support'])
    if packet['missing'] or packet['unsupported']:raise ValueError('supported plan still has unresolved critical changes')
    p=packet['proposed_parameters'];before,expressions,_=modelica_parameters(baseline['artifact']['modelica_source'])
    from .case_contact_family import is_contact_scope,validate_contact_plan
    from .case_asym2_family import is_asym2_source,PLAN_PARAMETERS as ASYM2_PLAN_PARAMETERS,derived_values
    asym2=is_asym2_source(baseline['artifact']['modelica_source'])
    contact=is_contact_scope(packet['scope']);stairs=packet['scope']==STAIR_SCOPE_PACKET or contact
    if not stairs and packet['scope']!=MASS_SCOPE_PACKET:raise UnsupportedRequestDesign('unsupported or incomplete declared physical scope')
    names=ASYM2_PLAN_PARAMETERS if asym2 else STAIR_PLAN_PARAMETERS if stairs else REQUEST_PARAMETERS
    if not isinstance(p,dict) or set(p)!=set(names) or any(type(x) not in (int,float) or not math.isfinite(x) for x in p.values()):raise ValueError('complete finite product parameters required')
    if contact:validate_contact_plan(packet,baseline,task_context)
    torque_ranges={};derived={}
    if stairs:
        if any(p[k]!=before[k] for k in REQUEST_PARAMETERS):raise UnsupportedRequestDesign('stair request must freeze every inherited vehicle mass/inertia/torque parameter')
        if any(p[k]<=0 for k in STAIR_PARAMETERS):raise UnsupportedRequestDesign('stair height and depth must be positive finite numbers')
        if not contact and not asym2:
            from .case_contact_family import reviewed_stair_capability
            capability=reviewed_stair_capability(baseline['artifact']['modelica_source'])
            if capability and {key:p[key] for key in STAIR_PARAMETERS} not in capability['allowed_scenarios']:
                raise UnsupportedRequestDesign(capability['message'])
        derived=(derived_values if asym2 else stair_derived_values)(before,expressions,{k:p[k] for k in STAIR_PARAMETERS})
    else:
        if p['m']<before['m'] or p['m']<=0 or p['I_pitch']!=before['I_pitch']:raise UnsupportedRequestDesign('only added ideal point mass at inherited CoM with inherited inertia is supported')
        if asym2 and any(p[k]!=before[k] for k in STAIR_PARAMETERS):raise UnsupportedRequestDesign('Asym2 mass request freezes both body stair dimensions')
        torque_ranges={name:[before[name],before[name] if asym2 else before[name]*p['m']/before['m']] for name in ('T_main_max','T_nose_max')}
        for name,(low,high) in torque_ranges.items():
            if not 0<low<=p[name]<=high:raise UnsupportedRequestDesign('torque is outside baseline-to-mass-ratio family envelope: '+name)
    declared={s['id']:s['text'] for s in sources}
    if not packet['requirements']:raise ValueError('new source-bound requirements required')
    for row in packet['requirements']:
        if not isinstance(row,dict) or set(row)!={'text','source_id','source_text'} or not isinstance(row['text'],str) or not row['text'].strip() or row['source_id'] not in declared or not isinstance(row['source_text'],str) or not row['source_text'] or row['source_text'] not in declared[row['source_id']]:raise ValueError('request requirement is not bound to a trusted source')
    design=packet['design']
    if not isinstance(design,dict) or set(design)!={'summary','assumptions','parameters'} or not isinstance(design['summary'],str) or not design['summary'].strip() or not isinstance(design['assumptions'],list) or not isinstance(design['parameters'],list):raise ValueError('complete product design required')
    for name,value in p.items():
        rows=[x for x in design['parameters'] if isinstance(x,dict) and x.get('name')==name]
        if len(rows)!=1 or rows[0].get('value')!=value:raise ValueError('design and parameter commitment differ: '+name)
    result=dict(executable=True,before={k:before[k] for k in names},after=p,torque_ranges=torque_ranges,scope=packet['scope'],direct_parameters=list(STAIR_PARAMETERS if stairs else ('m',) if asym2 else ('m','T_main_max','T_nose_max')))
    if stairs:result.update(derived_before={k:before[k] for k in derived},derived_after=derived,derived_expressions={k:expressions[k] for k in derived})
    return result

def check_request_delta(service,spec,ctx):
    baseline=ctx['baseline_snapshot']['frozen_spec'];plan=ctx['plan'];raw=baseline['artifact']['modelica_source']
    evidence=validate_request_plan(plan,baseline,ctx['sources'],task_context=ctx)
    if not evidence['executable']:raise ValueError('request plan is not executable')
    if service._current_project()['project_id']!=ctx['project_id'] or spec.get('baseline_ref')!=ctx['baseline_ref']:raise ValueError('new request project/baseline differs')
    from .inherited.artifact_baseline import selection,resolve
    if selection(service)!=ctx['baseline_ref']:raise ValueError('request baseline selection changed')
    actual=resolve(service,ctx['baseline_ref'])
    if actual['frozen_spec']!=baseline:raise ValueError('request baseline snapshot differs from actual version evidence')
    changed=evidence['direct_parameters']
    from .case_contact_family import is_contact_scope,check_contact_source
    contact=is_contact_scope(plan['scope'])
    from .case_asym2_family import is_asym2_source
    asym2=is_asym2_source(raw)
    if spec['artifact']['model_name']!=baseline['artifact']['model_name']:raise ValueError('new request changes inherited model class')
    if asym2 and not is_asym2_source(spec['artifact']['modelica_source']):raise ValueError('Asym2 source changed beyond pinned mass/body-stair family')
    if not contact and model_shape(spec['artifact']['modelica_source'],changed)!=model_shape(raw,changed):raise ValueError('new request changes frozen physics/geometry/control/dependencies')
    values,expressions,_=modelica_parameters(spec['artifact']['modelica_source']);old=modelica_parameters(raw)[0]
    for key,value in old.items():
        expected=evidence.get('derived_after',{}).get(key,plan['proposed_parameters'].get(key,value))
        if values.get(key)!=expected:raise ValueError('actual source parameter differs from confirmed design: '+key)
    if spec['sources']!=ctx['sources'] or spec['requirements']!=plan['requirements'] or spec['design']!=plan['design']:raise ValueError('generated metadata differs from request plan')
    for key in ('settings','outputs','libraries','checks'):
        if spec[key]!=baseline[key]:raise ValueError('new request changed inherited execution/task contract: '+key)
    if spec.get('trajectory_contract')!=baseline.get('trajectory_contract'):raise ValueError('new request changed task checks')
    # No observation-extension or numerical-refinement authority is inferred
    # from the first case. New requests preserve their selected baseline setup.
    for key in ('StopTime','Tolerance','Interval'):
        pattern=r'\b'+key+r'\s*=\s*('+NUMBER+')'
        first=re.findall(pattern,strip_comments(raw));second=re.findall(pattern,strip_comments(spec['artifact']['modelica_source']))
        if len(first)!=1 or len(second)!=1 or float(first[0])!=float(second[0]):raise ValueError('new request changed observation setting: '+key)
    review=check_contact_source(spec,ctx) if contact else None
    result=dict(status='PASS_REQUEST_FAMILY_DELTA',parameters=values,expressions=expressions,baseline_ref=ctx['baseline_ref'],request_id=ctx['request_id'],displayed_digest=ctx['displayed_digest'],scope='SOURCE_REVIEW_BOUND_CONTACT_DIAGNOSTIC' if contact else 'INHERITED_SIMPLIFIED_MODEL_ONLY',change_scope=plan['scope'],direct_parameters=changed,derived_parameters=evidence.get('derived_after',{}),equation_control_geometry_dependency_token_lock='REVIEWED_CONTACT_FAMILY_MATCH' if contact else 'MATCH')
    if review:result['contact_source_review']=review
    return result
