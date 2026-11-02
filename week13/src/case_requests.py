"""Thin request context and trusted-local confirmation over the existing Agent.

Creating a request/proposal never creates a budget authorization. Product tools
may propose data, but only the separate local confirm entry records approval.
"""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import uuid

ROOT = Path(__file__).resolve().parents[1]
LEGACY_PROJECT_ROOT = ROOT / 'runs/success-case-20261019-01'
REQUESTS_ROOT = LEGACY_PROJECT_ROOT / 'requests'
SELECTOR = 'MBSE_V12_REQUEST_ID'
APPROVAL_WORDS = {'确认','批准','同意','CONFIRM'}


def _approval(text):
    if not isinstance(text,str) or text.strip() not in APPROVAL_WORDS:
        raise PermissionError('trusted approval requires explicit 确认/批准/同意/CONFIRM command word')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf8')).hexdigest()


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _path(path):
    value = Path(path).resolve()
    if not value.is_relative_to(ROOT.resolve()) or value.is_relative_to((ROOT/'V12_old').resolve()):
        raise ValueError('request resource must stay within the new week13 tree')
    return value


def _write(path, value, immutable=False):
    path = _path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n'
    if immutable:
        try:
            with path.open('x', encoding='utf8') as handle:
                handle.write(body)
        except FileExistsError:
            if _read(path) != value:
                raise ValueError('immutable request evidence differs') from None
    else:
        temporary = path.with_name(path.name+'.tmp')
        temporary.write_text(body, encoding='utf8')
        temporary.replace(path)
    return value


def request_root(request_id):
    if not isinstance(request_id, str) or '..' in request_id or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', request_id):
        raise ValueError('invalid request_id')
    registry=ROOT/'runs/context-registry'/(request_id+'.json')
    if registry.exists():
        value=_read(registry);target=_path(value['root'])
        if value.get('request_id')!=request_id or target.name!=request_id or not target.is_relative_to((ROOT/'runs').resolve()) or target.is_relative_to(LEGACY_PROJECT_ROOT.resolve()):raise ValueError('invalid independent request registry')
        if value.get('kind')=='CASE_PROJECT_REQUEST':
            from .case_projects import resolve_case,verify_case
            case=verify_case(resolve_case(value['case_id']))
            if value.get('project_id')!=case['project_id'] or target!=(Path(case['project_root'])/'requests'/request_id).resolve():raise ValueError('case request registry/project binding differs')
        elif value.get('kind') is not None:raise ValueError('unknown independent request registry kind')
        return target
    return _path(REQUESTS_ROOT/request_id)


def load_request(request_id):
    root = request_root(request_id)
    ctx = _read(root/'task-context.json')
    original = _read(root/'request.json')
    if ctx.get('schema') != 'v12-request-context-1' or ctx.get('request_id') != request_id or Path(ctx['root']).resolve() != root:
        raise ValueError('request context identity/root differs')
    if digest(original) != ctx.get('request_digest') or any(ctx.get(key) != original.get(key) for key in ('project_id','baseline_version_id','baseline_ref','raw_request','db_path','project_root','access_mode','review_binding','step_id')):
        raise ValueError('original request/baseline/store binding drift')
    registry=ROOT/'runs/context-registry'/(request_id+'.json')
    if registry.exists():
        registration=_read(registry)
        if registration.get('kind')=='CASE_PROJECT_REQUEST':
            if ctx.get('access_mode') is not None or ctx.get('review_binding') is not None:raise ValueError('ordinary catalog request cannot claim readonly review privileges')
        elif ctx.get('access_mode')!='RESULT_REVIEW_ONLY' or not isinstance(ctx.get('review_binding'),dict) or ctx.get('step_id') not in ('explain','reopen','negative_mass','negative_geometry'):raise ValueError('readonly registry cannot grant ordinary project request permissions')
    snapshot = _read(root/'baseline-snapshot.json')
    if digest(snapshot) != original.get('baseline_snapshot_sha256') or any(snapshot.get(key) != value for key,value in original['baseline_ref'].items()):
        raise ValueError('baseline snapshot content/reference drift')
    ctx['baseline_snapshot'] = snapshot
    if ctx.get('proposal_path'):
        plan = _read(_path(ctx['proposal_path']))
        if digest(plan) != ctx.get('displayed_digest') or plan.get('request_digest') != ctx['request_digest']:
            raise ValueError('saved proposal digest/request drift')
        ctx['plan'] = plan['packet']
    return ctx


def selected_request():
    request_id = os.environ.get(SELECTOR)
    return load_request(request_id) if request_id else None


def create_request(request_id, project_id, baseline_version_id, raw_request, *, baseline_ref=None, baseline_snapshot=None, sources=None, db_path=None, project_root=None,context_root=None,review_binding=None,step_id=None,case_id=None):
    if context_root is not None:
        request_root(request_id);target=_path(context_root)
        registry=dict(request_id=request_id,root=str(target))
        if case_id is not None:
            from .case_projects import resolve_case,verify_case
            case=verify_case(resolve_case(case_id))
            if review_binding is not None or step_id is not None or case['kind']=='LEGACY_READ_ONLY_CATALOG' or case['project_id']!=project_id or Path(project_root).resolve()!=Path(case['project_root']).resolve() or Path(db_path).resolve()!=Path(case['db_path']).resolve() or target!=(Path(case['project_root'])/'requests'/request_id).resolve():raise ValueError('ordinary case request requires exact catalog project/store/root binding')
            registry.update(kind='CASE_PROJECT_REQUEST',case_id=case_id,project_id=project_id)
        elif not isinstance(review_binding,dict) or step_id not in ('explain','reopen','negative_mass','negative_geometry'):raise ValueError('independent readonly context requires explicit supported binding/step')
        if target.name!=request_id or not target.is_relative_to((ROOT/'runs').resolve()) or target.is_relative_to(LEGACY_PROJECT_ROOT.resolve()):raise ValueError('invalid independent request root')
        _write(ROOT/'runs/context-registry'/(request_id+'.json'),registry,immutable=True)
    root = request_root(request_id)
    if not isinstance(raw_request,str) or not raw_request.strip():
        raise ValueError('new request requires actual nonempty user text')
    if not project_id or not baseline_version_id:
        raise ValueError('explicit project and baseline version required')
    if baseline_ref is None or baseline_snapshot is None:
        # Ordinary existing service supplies real snapshot identity; no model
        # generation, source modifications or baseline rerun occurs here.
        from .case_session import open_service
        from .case_backend import module
        service = open_service(read_only=True)
        if service._current_project()['project_id'] != project_id:
            raise ValueError('selected project does not match baseline service')
        baseline_snapshot = service._artifact_version(baseline_version_id)
        baseline_ref = module('artifact_baseline').reference(baseline_snapshot)
    if baseline_snapshot.get('project_id') != project_id or baseline_snapshot.get('version_id') != baseline_version_id or baseline_ref.get('project_id') != project_id or baseline_ref.get('version_id') != baseline_version_id:
        raise ValueError('selected real baseline identity differs')
    project_root = _path(project_root or LEGACY_PROJECT_ROOT)
    db_path = _path(db_path or project_root/'projects.sqlite')
    if any(baseline_snapshot.get(key) != value for key,value in baseline_ref.items()):
        raise ValueError('baseline snapshot and reference content differ')
    original = dict(request_id=request_id,project_id=project_id,baseline_version_id=baseline_version_id,baseline_ref=baseline_ref,baseline_snapshot_sha256=digest(baseline_snapshot),raw_request=raw_request,db_path=str(db_path),project_root=str(project_root))
    if review_binding is not None:original.update(access_mode='RESULT_REVIEW_ONLY',review_binding=review_binding,step_id=step_id)
    if (root/'task-context.json').exists():
        ctx = load_request(request_id)
        if ctx['request_digest'] != digest(original):
            raise ValueError('request_id already owns different input; do not reset it')
        return ctx
    _write(root/'request.json', original, immutable=True)
    _write(root/'baseline-snapshot.json', baseline_snapshot, immutable=True)
    ctx = dict(original,schema='v12-request-context-1',root=str(root),request_digest=digest(original),
        session_id='request_'+uuid.uuid4().hex,sources=sources or [],stage='DRAFT',approved_design=None,
        allowed_delta=None,assumptions=[],displayed_digest=None,confirmation_path=None,
        execution_context_path=str(root/'execution-context.json'),runtime_authority=None,allow_http=False,allow_execution=False,
        created_at_utc=datetime.now(timezone.utc).isoformat())
    _write(root/'task-context.json',ctx)
    return load_request(request_id)


def save_plan(request_id, packet, baseline_ref=None, sources=None, **metadata):
    ctx = load_request(request_id)
    if not isinstance(packet,dict) or not packet:
        raise ValueError('complete product proposal packet required')
    if baseline_ref is not None and baseline_ref != ctx['baseline_ref']:
        raise ValueError('proposal changed the selected real baseline')
    source_records = sources if sources is not None else ctx['sources']
    delta = metadata.get('allowed_delta',dict(scope=packet.get('scope'),proposed_parameters=packet.get('proposed_parameters')))
    assumptions = metadata.get('assumptions',packet.get('assumptions',packet.get('design',packet.get('candidate_design',{})).get('assumptions',[])))
    plan = dict(request_id=request_id,request_digest=ctx['request_digest'],baseline_ref=ctx['baseline_ref'],packet=packet,sources=source_records,allowed_delta=delta,assumptions=assumptions)
    displayed = digest(plan)
    _write(Path(ctx['root'])/'plans'/(displayed+'.json'),plan,immutable=True)
    # Any changed proposal invalidates physical confirmation without deleting
    # previous receipts. A model-supplied "approval" field has no effect.
    if ctx.get('displayed_digest') != displayed:
        ctx.update(stage='PROPOSED',approved_design=None,confirmation_path=None,allow_execution=False)
    ctx.update(displayed_digest=displayed,proposal_path=str(Path(ctx['root'])/'plans'/(displayed+'.json')),sources=source_records,allowed_delta=delta,assumptions=assumptions)
    return _write(Path(ctx['root'])/'task-context.json',ctx)


def _executable(packet):
    """Structural approval guard; physical/source validation remains in scope."""
    fields={'intent','support','missing','unsupported','requirements','design','proposed_parameters','defaults','unknowns','rationale','scope'}
    if not isinstance(packet,dict) or not fields <= set(packet) or packet['intent'] != 'CHANGE' or packet['support'] != 'SUPPORTED':
        raise PermissionError('only a complete supported CHANGE proposal can be confirmed/executed')
    if packet['missing'] or packet['unsupported'] or not packet['requirements'] or not isinstance(packet['requirements'],list):
        raise PermissionError('proposal has unresolved facts or lacks complete requirements')
    if any(not isinstance(row,dict) or not all(isinstance(row.get(k),str) and row[k].strip() for k in ('text','source_id','source_text')) for row in packet['requirements']):
        raise PermissionError('complete source-bound requirements required before confirmation')
    design=packet['design']; parameters=packet['proposed_parameters']
    if not isinstance(design,dict) or not isinstance(design.get('summary'),str) or not design['summary'].strip() or not isinstance(design.get('assumptions'),list) or not isinstance(design.get('parameters'),list) or not design['parameters'] or not isinstance(parameters,dict) or not parameters or not isinstance(packet['scope'],dict) or not packet['scope'] or not isinstance(packet['rationale'],str) or not packet['rationale'].strip():
        raise PermissionError('complete product design, scope and parameter commitment required')
    for name,value in parameters.items():
        matches=[row for row in design['parameters'] if isinstance(row,dict) and row.get('name')==name]
        if len(matches)!=1 or matches[0].get('value')!=value:
            raise PermissionError('design and parameter commitment differ')


def _recheck_plan(ctx,packet):
    """Saved confirmations cannot bypass current deterministic plan guards."""
    from .case_scope import validate_request_plan
    from .case_semantic_guard import require_plan_semantics
    baseline=ctx['baseline_snapshot']['frozen_spec']
    if not validate_request_plan(packet,baseline,ctx['sources'],task_context=ctx)['executable']:
        raise PermissionError('saved plan is not executable under current scope')
    return require_plan_semantics(packet,baseline)


def confirm_request(request_id, displayed_digest, approval_text, *, approval_basis=None, human_reviewed_displayed_plan=True):
    ctx = load_request(request_id)
    if ctx.get('access_mode')=='RESULT_REVIEW_ONLY':raise PermissionError('readonly/plan-only context cannot confirm a physical design')
    _approval(approval_text)
    if type(human_reviewed_displayed_plan) is not bool or approval_basis is not None and (not isinstance(approval_basis,str) or not approval_basis.strip()):
        raise ValueError('explicit truthful human review flag/approval basis required')
    if not human_reviewed_displayed_plan and not approval_basis:
        raise PermissionError('delegated in-scope confirmation requires the actual prior approval basis')
    if ctx.get('displayed_digest') != displayed_digest or not displayed_digest or not isinstance(approval_text,str) or not approval_text.strip():
        raise ValueError('trusted confirmation must bind the displayed proposal and actual approval text')
    plan = _read(ctx['proposal_path'])
    if digest(plan) != displayed_digest or plan['request_digest'] != ctx['request_digest']:
        raise ValueError('displayed plan/request changed')
    _executable(plan['packet'])
    _recheck_plan(ctx,plan['packet'])
    receipt = dict(schema='v12-trusted-request-confirmation-1',request_id=request_id,request_digest=ctx['request_digest'],displayed_digest=displayed_digest,
        approval_text=approval_text,approval_basis=approval_basis,human_reviewed_displayed_plan=human_reviewed_displayed_plan,channel='TRUSTED_LOCAL_CLI',product_self_approval=False,budget_authorization_created=False)
    path = Path(ctx['root'])/'confirmations'/(displayed_digest+'.json')
    _write(path,receipt,immutable=True)
    ctx.update(stage='CONFIRMED',approved_design=plan['packet'],confirmation_path=str(path))
    _write(Path(ctx['root'])/'task-context.json',ctx)
    return receipt


def authority_for(ctx):
    ref = ctx.get('runtime_authority')
    if not isinstance(ref,dict):
        raise PermissionError('request has no newly approved runtime budget; legacy balance is not permission')
    path = _path(ref['path'])
    if hashlib.sha256(path.read_bytes()).hexdigest() != ref.get('sha256'):
        raise ValueError('request runtime authorization digest drift')
    authority = _read(path)
    if authority.get('schema') != 'v12-request-budget-authorization-1':
        raise ValueError('runtime budget schema differs')
    if authority.get('context_bindings',{}).get(ctx['request_id']) != ctx['request_digest']:
        binding_path=ref.get('binding_path')
        if not binding_path or hashlib.sha256(_path(binding_path).read_bytes()).hexdigest()!=ref.get('binding_sha256'):
            raise ValueError('runtime budget is not bound to this request; cannot borrow old authorization')
        binding=_read(binding_path)
        if binding.get('schema')!='v12-trusted-budget-context-binding-1' or binding.get('authorization_sha256')!=ref['sha256'] or binding.get('request_digest')!=ctx['request_digest'] or binding.get('request_id')!=ctx['request_id'] or binding.get('project_id')!=ctx['project_id'] or binding.get('channel')!='TRUSTED_LOCAL_CLI' or not binding.get('approval_text'):
            raise ValueError('trusted shared budget binding differs')
    if not authority.get('approval_quote') or authority.get('approval_channel') not in ('TRUSTED_LOCAL_CLI','USER_CHAT_APPROVAL'):
        raise ValueError('runtime authorization lacks a trusted actual user approval record')
    ledger = _path(authority['ledger_path'])
    if str(ledger) != str(_path(ref['ledger_path'])):
        raise ValueError('one authorization must retain its unique ledger path')
    if ledger == (LEGACY_PROJECT_ROOT/'budget.sqlite').resolve():
        raise PermissionError('new request cannot borrow or replace the first-case ledger')
    _validate_authority(authority)
    policy=authority.get('operation_policy')
    if policy:
        if ctx.get('access_mode')!='RESULT_REVIEW_ONLY' or policy['steps'].get(ctx.get('step_id'))!=ctx['request_id'] or ctx.get('review_binding',{}).get('reference')!=policy['origin_reference']:raise PermissionError('readonly budget requires exact immutable origin and step binding')
    elif ctx.get('access_mode')=='RESULT_REVIEW_ONLY':raise PermissionError('readonly context requires an explicitly restricted authority')
    return authority


def _validate_authority(authority):
    start=datetime.fromisoformat(authority['started_at_utc'].replace('Z','+00:00'))
    end=datetime.fromisoformat(authority['hard_deadline_utc'].replace('Z','+00:00'))
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError('explicit finite UTC authorization interval required')
    cap_names=('http_requests','product_cny','total_tokens','total_output_tokens','compile','simulation','candidates','software_repair_rounds','parameter_revision_rounds','observation_extensions')
    caps=authority.get('caps',{})
    if any(type(caps.get(k)) not in (int,float) or not 0 <= caps[k] < float('inf') for k in cap_names):
        raise ValueError('all cumulative runtime/process/repair budgets must be explicit and finite')
    if any(type(caps[k]) is not int for k in cap_names if k!='product_cny'):
        raise ValueError('request/token/process/candidate/repair/extension caps must be integers, not bool or fractional')
    if not isinstance(authority.get('model_controls'),dict) or not isinstance(authority.get('permissions'),dict):
        raise ValueError('runtime controls and planning/execution permissions required')
    controls=authority['model_controls']
    if controls.get('tool_output_profile')=='deepseek-strict-beta-plan-flat-v1':
        from .case_strict_tools import transport_profile
        transport_profile(controls)
    if controls.get('model')!='deepseek-flash' or controls.get('thinking')!='enabled' or controls.get('reasoning_effort')!='high' or controls.get('max_tokens')!=393216 or controls.get('context_window_tokens')!=1000000 or controls.get('automatic_retries')!=0:
        raise ValueError('new budget cannot silently lower the shared product model controls')
    if any(type(authority['permissions'].get(key)) is not bool for key in ('planning_http','execution')):
        raise ValueError('planning and execution permissions must be explicit booleans')
    for control,cap in (('max_requests','http_requests'),('max_total_tokens','total_tokens'),('max_total_output_tokens','total_output_tokens'),('max_cny','product_cny')):
        if controls.get(control)!=caps[cap]: raise ValueError('runtime controls and cumulative budget caps differ')
    policy=authority.get('operation_policy')
    allocation=authority.get('repair_allocation')
    if allocation is not None:
        before=allocation.get('maximum_before_first_valid_generated_source')
        reserved=allocation.get('reserved_for_post_generation_or_runtime')
        if allocation.get('maximum_extra_rounds')!=caps['software_repair_rounds'] or type(before) is not int or type(reserved) is not int or before<0 or reserved<2 or before+reserved!=caps['software_repair_rounds'] or allocation.get('maximum_complete_affected_scenario_rerun_rounds')!=1:
            raise PermissionError('repair allocation must match finite total and preserve two post-generation/runtime repair rounds')
    package_binding=authority.get('execution_package')
    if package_binding is not None:
        if not isinstance(package_binding,dict) or set(package_binding)!={'path','sha256'} or hashlib.sha256(_path(package_binding['path']).read_bytes()).hexdigest()!=package_binding['sha256']:
            raise PermissionError('execution package approval binding differs')
        package=_read(_path(package_binding['path']))
        if package.get('schema')!='v12-strict-stair-followup-approval-1' or package.get('approval_channel')!='USER_CHAT_APPROVAL' or not package.get('approval_quote'):
            raise PermissionError('execution package lacks actual user approval')
        for key in ('project_id','started_at_utc','hard_deadline_utc','caps','model_controls','timeouts_s','repair_allocation'):
            if authority.get(key)!=package.get(key):raise PermissionError('execution package authority differs: '+key)
        if set(authority.get('context_bindings',{}))!=set(package.get('request_ids',[])):
            raise PermissionError('execution package requests differ from approved set')
    if policy is not None:
        if not isinstance(policy,dict) or set(policy)!={'mode','steps','origin_reference'} or policy['mode']!='READONLY_VALIDATION' or not isinstance(policy['steps'],dict) or set(policy['steps']) not in ({'explain','reopen'},{'explain','reopen','negative_mass','negative_geometry'}) or any(not isinstance(value,str) or not value for value in policy['steps'].values()) or len(set(policy['steps'].values()))!=len(policy['steps']):raise PermissionError('readonly isolation requires distinct explicitly bound explain/reopen steps or the complete four-step validation')
        if authority['permissions']!={'planning_http':True,'execution':False} or any(caps[key]!=0 for key in ('compile','simulation','candidates','parameter_revision_rounds','observation_extensions')):raise PermissionError('readonly isolation cannot carry physical/generation execution permissions')


def bind_runtime_authority(request_id, authorization_path, authorization_sha256=None):
    """Attach an existing real authorization; never fabricate/issue approval."""
    ctx = load_request(request_id)
    path = _path(authorization_path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    if authorization_sha256 is not None and sha != authorization_sha256:
        raise ValueError('supplied runtime authorization hash differs')
    value = _read(path)
    new_ref = dict(path=str(path),sha256=sha,ledger_path=str(_path(value['ledger_path'])))
    if ctx.get('runtime_authority') and ctx['runtime_authority'] != new_ref:
        raise PermissionError('request budget already bound; no new context/authorization may refresh it')
    ctx['runtime_authority'] = new_ref
    authority = authority_for(ctx)
    ctx['allow_http'] = authority.get('permissions',{}).get('planning_http') is True
    ctx['allow_execution'] = False
    return _write(Path(ctx['root'])/'task-context.json',ctx)


def require_planning(request_id):
    ctx = load_request(request_id)
    authority = authority_for(ctx)
    if authority.get('permissions',{}).get('planning_http') is not True:
        raise PermissionError('planning HTTP not authorized for this request')
    return ctx


def remaining_request(request_id):
    authority=authority_for(load_request(request_id))
    return (datetime.fromisoformat(authority['hard_deadline_utc'].replace('Z','+00:00'))-datetime.now(timezone.utc)).total_seconds()


def save_sources(request_id,sources):
    ctx=load_request(request_id)
    if not isinstance(sources,list) or not sources:
        raise ValueError('nonempty source records required')
    if ctx['sources'] == sources: return ctx
    if ctx['stage']!='DRAFT' or ctx['sources']:
        raise ValueError('request sources are immutable after initial registration')
    ctx['sources']=sources
    _write(Path(ctx['root'])/'task-context.json',ctx)
    return ctx


def authorize_budget(request_id,budget_id,approval_text,*,caps=None,hard_deadline_utc=None,planning_http=True,execution=True,reuse=False,test_only=False,model_controls=None,readonly_steps=None,started_at_utc=None,timeouts_s=None,repair_allocation=None,execution_package=None,bound_request_ids=None):
    """Trusted local CLI action, never registered as a product/MCP tool.

    Creating requests has no effect on budgets. Reuse appends an immutable
    context binding to the existing authorization and keeps its exact ledger,
    original deadline and cumulative counters. No historical ledger is edited.
    """
    ctx=load_request(request_id)
    _approval(approval_text)
    if not isinstance(budget_id,str) or '..' in budget_id or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}',budget_id):
        raise ValueError('invalid budget_id')
    folder=_path(ROOT/'runs/request-budgets'/budget_id)
    path=folder/'authorization.json'
    if ctx.get('runtime_authority') and Path(ctx['runtime_authority']['path']).resolve()!=path:
        raise PermissionError('request already owns a budget; cannot replace/reset it')
    if reuse:
        value=_read(path)
        if value.get('project_id')!=ctx['project_id'] or value.get('test_only',False)!=test_only:
            raise PermissionError('existing budget belongs to a different project or fixture mode')
        if any(item is not None for item in (caps,hard_deadline_utc,model_controls,started_at_utc,timeouts_s,repair_allocation,execution_package,bound_request_ids)):
            raise ValueError('reuse retains original caps/deadline/controls; no reset or increase')
    else:
        if path.exists():
            raise ValueError('budget_id already exists; explicit reuse required, never reset')
        if not isinstance(caps,dict) or hard_deadline_utc is None:
            raise ValueError('new budget requires explicit finite caps and UTC deadline')
        controls=dict(model_controls or _read(ROOT/'configs/case_runtime.json'))
        controls.update(max_requests=caps.get('http_requests'),max_total_tokens=caps.get('total_tokens'),max_total_output_tokens=caps.get('total_output_tokens'),max_cny=caps.get('product_cny'))
        value=dict(schema='v12-request-budget-authorization-1',budget_id=budget_id,project_id=ctx['project_id'],approval_quote=approval_text,
            approval_channel='TRUSTED_LOCAL_CLI',context_bindings={request_id:ctx['request_digest']},started_at_utc=started_at_utc or datetime.now(timezone.utc).isoformat(),hard_deadline_utc=hard_deadline_utc,
            ledger_path=str(folder/'budget.sqlite'),caps=caps,permissions=dict(planning_http=planning_http,execution=execution),model_controls=controls,
            timeouts_s=dict(compile=900,simulation=900,worker=1900,http_whole=controls['whole_request_deadline_s'],connect=controls['connect_timeout_s'],read_idle=controls['read_timeout_s'],no_substantive_progress=controls['no_progress_timeout_s'],mcp_request=120),
            test_only=test_only,old_scopes_reopened=False,old_balances_transferred=False)
        if timeouts_s is not None:
            expected={'compile','simulation','worker','http_whole','connect','read_idle','no_substantive_progress','mcp_request'}
            if set(timeouts_s)!=expected or any(type(v) not in (int,float) or not 0<v<float('inf') for v in timeouts_s.values()):raise ValueError('complete finite process timeouts required')
            value['timeouts_s']=dict(timeouts_s)
        if repair_allocation is not None:value['repair_allocation']=dict(repair_allocation)
        if execution_package is not None:value['execution_package']=dict(execution_package)
        if bound_request_ids is not None:
            if not isinstance(bound_request_ids,list) or request_id not in bound_request_ids or len(set(bound_request_ids))!=len(bound_request_ids):raise ValueError('explicit distinct bound requests required')
            for rid in bound_request_ids:
                other=load_request(rid)
                if other['project_id']!=ctx['project_id'] or other.get('runtime_authority'):raise PermissionError('new package requests must belong to same project and have no prior budget')
                value['context_bindings'][rid]=other['request_digest']
        if readonly_steps is not None:
            if ctx.get('access_mode')!='RESULT_REVIEW_ONLY':raise PermissionError('readonly authority requires readonly context')
            value['operation_policy']=dict(mode='READONLY_VALIDATION',steps=readonly_steps,origin_reference=ctx['review_binding']['reference'])
    if ctx.get('access_mode')=='RESULT_REVIEW_ONLY' and not value.get('operation_policy'):raise PermissionError('readonly context cannot obtain an unrestricted budget')
    _validate_authority(value)
    if value.get('operation_policy'):
        policy=value['operation_policy']
        if policy['steps'].get(ctx.get('step_id'))!=request_id or policy['origin_reference']!=ctx['review_binding']['reference']:raise PermissionError('budget step/origin differs')
    if not reuse:
        # Validate the complete candidate before writing an approval file.
        _validate_authority(value)
        _write(path,value,immutable=True)
        _write(folder/'runtime.json',controls,immutable=True)
    sha=hashlib.sha256(path.read_bytes()).hexdigest()
    binding=dict(schema='v12-trusted-budget-context-binding-1',budget_id=budget_id,authorization_sha256=sha,request_id=request_id,request_digest=ctx['request_digest'],project_id=ctx['project_id'],approval_text=approval_text,channel='TRUSTED_LOCAL_CLI',test_only=test_only)
    binding_path=folder/'context-bindings'/(digest(binding)+'.json')
    _write(binding_path,binding,immutable=True)
    ref=dict(path=str(path),sha256=sha,ledger_path=value['ledger_path'],binding_path=str(binding_path),binding_sha256=hashlib.sha256(binding_path.read_bytes()).hexdigest())
    if ctx.get('runtime_authority') and ctx['runtime_authority']!=ref:
        # An idempotent repeat may supply the original actual approval text.
        if {k:ctx['runtime_authority'].get(k) for k in ('path','sha256','ledger_path')}!={k:ref[k] for k in ('path','sha256','ledger_path')}:
            raise PermissionError('request already owns a budget; cannot replace/reset it')
        return ctx
    ctx['runtime_authority']=ref
    authority_for(ctx)
    ctx.update(allow_http=value['permissions'].get('planning_http') is True,allow_execution=False)
    _write(Path(ctx['root'])/'runtime.json',value['model_controls'],immutable=True)
    _write(Path(ctx['root'])/'task-context.json',ctx)
    return load_request(request_id)


def require_execution(request_id):
    ctx = load_request(request_id)
    if ctx.get('access_mode')=='RESULT_REVIEW_ONLY':raise PermissionError('readonly/plan-only context has zero physical execution permission')
    authority = authority_for(ctx)
    if authority.get('permissions',{}).get('execution') is not True or ctx.get('stage') != 'CONFIRMED' or not ctx.get('confirmation_path'):
        raise PermissionError('physical execution requires both trusted plan confirmation and runtime budget')
    receipt = _read(ctx['confirmation_path'])
    plan = _read(ctx['proposal_path'])
    if receipt.get('channel') != 'TRUSTED_LOCAL_CLI' or receipt.get('product_self_approval') is not False or receipt.get('displayed_digest') != ctx.get('displayed_digest') or digest(plan) != ctx['displayed_digest'] or receipt.get('request_digest') != ctx['request_digest']:
        raise ValueError('trusted physical confirmation is stale or mismatched')
    _executable(plan['packet'])
    _recheck_plan(ctx,plan['packet'])
    binding=dict(schema='v12-request-execution-binding-1',request_id=request_id,request_digest=ctx['request_digest'],project_id=ctx['project_id'],baseline_ref=ctx['baseline_ref'],displayed_digest=ctx['displayed_digest'],runtime_authority=ctx['runtime_authority'],confirmation_path=ctx['confirmation_path'],allowed_delta=plan['allowed_delta'],assumptions=plan['assumptions'])
    _write(Path(ctx['root'])/'execution-binding.json',binding)
    tasks=authority['caps']['simulation']
    if type(tasks) is not int or not 0 < tasks <= 8 or authority['caps']['compile']<=0:
        raise PermissionError('physical execution requires positive finite supported process caps (simulation <=8)')
    timeouts=authority.get('timeouts_s',{})
    execution=dict(schema='v11-artifact-execution-context-1',context_id='request_'+ctx['request_digest'][:24],status='ACTIVE',mode='TEST_ONLY' if authority.get('test_only') else 'LIVE',allow_execution=True,max_tasks=tasks,worker_timeout_s=timeouts.get('worker',1900),simulation_timeout_s=timeouts.get('simulation',900))
    _write(ctx['execution_context_path'],execution)
    ctx['allow_execution']=True
    return ctx
