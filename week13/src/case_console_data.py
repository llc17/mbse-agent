"""Read-only discovery for the terminal, executed in a fresh Python process.

Never instantiate CaseBudget here: even its constructor creates ledger tables.
No HTTP, confirmation, recovery, generation or execution is available here.
"""
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT / 'runs/success-case-20261019-01'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def parameters(snapshot):
    from .case_scope import modelica_parameters
    values = modelica_parameters(snapshot['frozen_spec']['artifact']['modelica_source'])[0]
    return {key: values.get(key) for key in ('m', 'I_pitch', 'T_main_max', 'T_nose_max', 'stepH', 'stepD')}


def _legacy_discover():
    from .case_backend import service
    run = read(PROJECT_ROOT / 'run.json')
    svc = service(PROJECT_ROOT / 'projects.sqlite', run['session_id'], read_only=True)
    svc.project_root = PROJECT_ROOT
    versions = svc.versions_list()['versions']['items']
    baselines = []
    for row in versions:
        if all(row.get(k) == v for k, v in (('evidence_status', 'COMPLETE'), ('chain_status', 'PASS'), ('task_status', 'PASS'))):
            snapshot = svc._artifact_version(row['version_id'])
            baselines.append(dict(version_id=row['version_id'], task_id=row['task_id'], parameters=parameters(snapshot), task_status='PASS'))
    requests = []
    paths = list((PROJECT_ROOT / 'requests').glob('*/task-context.json'))
    for registry in (ROOT / 'runs/context-registry').glob('*.json'):
        value = read(registry)
        # Use the existing registry validation, never trust an arbitrary path.
        from .case_requests import request_root
        paths.append(request_root(value['request_id']) / 'task-context.json')
    for path in paths:
        value = read(path)
        requests.append({key: value.get(key) for key in ('request_id', 'project_id', 'baseline_version_id', 'stage', 'raw_request', 'created_at_utc', 'access_mode')})
    requests.sort(key=lambda x: (x.get('created_at_utc') or '', x['request_id']))
    return dict(project_id=run['project_id'], project_label='继承成功案例：六轮上下楼', baselines=baselines, default_baseline=baselines[-1] if baselines else None, requests=requests,
                controls={key: read(ROOT / 'configs/case_runtime.json')[key] for key in ('model', 'thinking', 'reasoning_effort', 'context_window_tokens', 'max_tokens', 'maximum_request_reserve_cny')})


def discover(case_id=None):
    from .case_projects import discover_case
    value = discover_case(case_id)
    # Read the already-discovered canonical sources only. No Store or ledger
    # construction is needed to display input limits before the first request.
    project_root = Path((value.get('case') or {}).get('project_root', ''))
    project_id = value.get('project_id')
    if project_root.is_absolute() and project_root.resolve().is_relative_to((ROOT/'runs').resolve()) and isinstance(project_id,str) and re.fullmatch(r'[A-Za-z0-9_-]+',project_id):
        from .case_paths import sha_file
        from .case_contact_family import reviewed_stair_capability
        for row in value.get('baselines',[]):
            version = row.get('version_id','')
            if not re.fullmatch(r'v\d+',version):continue
            path = project_root/'baseline-original.mo' if version=='v000000' else project_root/'projects'/project_id/'versions'/version/'frozen-model.mo'
            try:
                if path.is_file() and sha_file(path)==row.get('source_sha256'):
                    row['input_capability']=reviewed_stair_capability(path.read_text(encoding='utf-8-sig'))
            except (OSError,ValueError,KeyError,TypeError) as exc:
                row['input_capability']=dict(status='UNKNOWN',message='支持范围尚未安全核实：'+str(exc))
    return value


def ledger_summary(authority):
    caps = authority['caps']
    used = {key: 0 for key in caps}
    pending = []
    frozen = None
    path = Path(authority['ledger_path'])
    if path.exists():
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'requests' in tables:
                for row in db.execute("SELECT * FROM requests WHERE status<>'not_dispatched'"):
                    used['http_requests'] += 1
                    used['total_tokens'] += row['actual_tokens'] if row['actual_tokens'] is not None else row['reserve_tokens']
                    used['total_output_tokens'] += row['actual_output'] if row['actual_output'] is not None else row['reserve_output']
                    used['product_cny'] += row['actual_cost'] if row['actual_cost'] is not None else row['reserve_cost']
                    if row['status'] in ('unknown', 'reserved', 'dispatched'):
                        pending.append(dict(request_id=row['request_id'], status=row['status']))
            if 'process_attempts' in tables:
                for row in db.execute('SELECT kind,count(*) FROM process_attempts GROUP BY kind'):
                    if row[0] in used:
                        used[row[0]] = row[1]
            if 'counters' in tables:
                names = dict(candidate='candidates', software_repair='software_repair_rounds', parameter_revision='parameter_revision_rounds', observation_extension='observation_extensions')
                for row in db.execute('SELECT kind,count(*) FROM counters GROUP BY kind'):
                    if row[0] in names:
                        used[names[row[0]]] = row[1]
            if 'meta' in tables:
                item = db.execute("SELECT value FROM meta WHERE key IN ('frozen','connect_retry_required') LIMIT 1").fetchone()
                frozen = item[0] if item else None
    now = datetime.now(timezone.utc)
    expired = now >= datetime.fromisoformat(authority['hard_deadline_utc'].replace('Z', '+00:00'))
    retained = []
    retained_timeout=[];approved_timeout=None
    if pending and frozen and not expired and len(authority.get('context_bindings',{}))==1:
        try:
            from .case_requests import load_request
            from .case_plan_recovery import registered
            ctx=load_request(next(iter(authority['context_bindings'])))
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
                db.row_factory=sqlite3.Row;proof=registered(db,ctx,authority,require_plan=True)
            if proof and pending==[dict(request_id=proof['original_http_request_id'],status='unknown')]:
                retained_timeout=pending;pending=[];approved_timeout=dict(original_request_id=proof['original_http_request_id'],replacement_request_id=proof['replacement_http_request_id'],approval_path=proof['approval_path'],original_freeze_preserved=True)
        except (OSError,ValueError,KeyError,TypeError,PermissionError,sqlite3.Error):pass
    if pending and not frozen and not expired and len(authority.get('context_bindings',{}))==1:
        # Preserve every original row and its full token/CNY reservation. Only
        # the exactly bound, successful explicit connection exception removes
        # that old UNKNOWN from the current-action blocking classification.
        try:
            from .case_taskflow import TaskFlow
            from .case_requests import load_request
            rid=next(iter(authority['context_bindings']))
            flow=TaskFlow(load_request(rid))
            journal=flow.root/'turns/plan/connect-replacement.json'
            if journal.is_file():
                binding=flow._plan_connect_binding(require_success=True)
                if pending==[dict(request_id=binding['original_request_id'],status='unknown')]:
                    retained=pending;pending=[]
        except (OSError,ValueError,KeyError,TypeError,PermissionError,sqlite3.Error):
            pass  # Missing/drifted proof never creates permission.
    blocked = 'UNKNOWN：有未确定或在途请求，禁止重发或换ID续借' if pending else '预算已冻结，禁止自动重试：' + str(frozen) if frozen and not approved_timeout else '硬总截止已到' if expired else None
    labels = dict(http_requests='产品HTTP次数', product_cny='人民币费用', total_tokens='累计总tokens', total_output_tokens='累计输出tokens')
    http_limits = [label + '额度已满' for key, label in labels.items() if used.get(key, 0) >= caps.get(key, float('inf'))]
    exhausted = {key: dict(used=used.get(key, 0), cap=cap) for key, cap in caps.items() if used.get(key, 0) >= cap}
    return dict(budget_id=authority['budget_id'], hard_deadline_utc=authority['hard_deadline_utc'], started_at_utc=authority['started_at_utc'], caps=caps, used=used, unknown_requests=pending, retained_unknown_after_explicit_connection_replacement=retained,retained_unknown_after_explicit_read_timeout_replacement=retained_timeout,approved_read_timeout_replacement=approved_timeout,actual_cost_cny='UNKNOWN' if pending or retained or retained_timeout else used.get('product_cny',0), frozen=frozen, blocked=blocked, http_blocked='；'.join(http_limits) or None, exhausted=exhausted, permissions=authority['permissions'], approval_quote=authority['approval_quote'], ledger_path=authority['ledger_path'])


def failure_summary(ctx, budget):
    """Read saved validation facts without instantiating a runtime or ledger."""
    root = Path(ctx['root'])
    paths = sorted((root / 'failures').glob('*.json'), key=lambda path: path.stat().st_mtime_ns)
    if not paths:
        return None
    path = paths[-1]
    failure = read(path)
    stage = failure.get('stage')
    resolved = bool(ctx.get('plan')) if stage == 'plan' else (root / 'generation.json').exists() if stage == 'generate' else False
    summary = {key: failure.get(key) for key in ('stage', 'attempt', 'error', 'repairable')}
    summary.update(evidence_path=str(path), status='RESOLVED_WITH_LATER_VALIDATION' if resolved else 'UNRESOLVED')
    revalidated = root / 'generation-revalidated.json'
    if not resolved and stage == 'generate' and revalidated.exists():
        try:
            from .case_paths import sha_value
            saved = read(revalidated)
            audit_path = root / 'revalidations' / ('generate-' + str(saved['attempt']) + '-source-references.json')
            audit = read(audit_path)
            if saved.get('schema') != 'v12-revalidated-generation-candidate-1' or saved.get('stage') != 'VALIDATED_CANDIDATE_PENDING_PROPOSAL' or any(saved.get(key) != ctx[key] for key in ('request_id', 'request_digest', 'displayed_digest')) or saved.get('audit_path') != str(audit_path) or saved.get('audit_sha256') != sha_value(audit) or saved.get('spec_digest') != sha_value(saved.get('spec')):
                raise ValueError('离线复验回执与当前请求、方案或摘要绑定不一致')
            summary.update(status='RESOLVED_BY_OFFLINE_REVALIDATION', revalidation_path=str(revalidated), validation_scope='RECEIPT_BINDING_READ_ONLY_BACKEND_REVALIDATES_BEFORE_RUN')
            resolved = True
        except (OSError, ValueError, KeyError, TypeError) as exc:
            summary['revalidation_error'] = '离线复验回执不能安全读回：' + str(exc)
    if not resolved and stage in ('plan', 'generate') and budget:
        used = budget['used'].get('software_repair_rounds', 0)
        cap = budget['caps'].get('software_repair_rounds', 0)
        if used >= cap:
            summary['stopped_action'] = '自动纠错 ' + str(used) + '/' + str(cap) + ' 已耗尽；' + ('双源码生成' if stage == 'generate' else '方案生成') + '停止，尚未进入后续执行。'
    return summary


def verified_source_facts(ctx):
    """Describe the request's verified frozen source, not import-time gaps."""
    from .case_taskflow import selected_source_facts
    return selected_source_facts(ctx)


def plan_review_warnings(ctx, facts):
    """Only read validation warnings bound to this exact saved packet/source."""
    from .case_requests import digest
    plan = ctx.get('plan')
    if not plan:
        return []
    warnings = []
    from .case_semantic_guard import require_plan_semantics,plan_diagnostic_warnings
    # Derive current diagnostics from the exact trusted saved packet/source.
    # This also keeps warnings visible when reading a legacy validation report.
    warnings.extend(plan_diagnostic_warnings(require_plan_semantics(plan,ctx['baseline_snapshot']['frozen_spec'])))
    root = Path(ctx['root']);path = root / 'plan-validation.json'
    head = root / 'plan-validation-head.json'
    if path.exists():
        try:
            validation = read(path)
            if head.exists():
                from .case_paths import sha_file
                pointer=read(head);current=root/'plan-validations'/(ctx['displayed_digest']+'.json')
                if pointer.get('displayed_digest')!=ctx['displayed_digest'] or Path(pointer.get('path','')).resolve()!=current.resolve() or pointer.get('sha256')!=sha_file(current):raise ValueError('current plan validation head differs')
                bound=read(current)
                if any(bound.get(key)!=ctx.get(key) for key in ('request_id','request_digest','displayed_digest')):raise ValueError('current displayed validation request binding differs')
                validation=bound['validation']
            saved_facts = validation.get('verified_source_facts') or {}
            keys = ('baseline_version_id', 'baseline_ref', 'selected_baseline_sysml_present',
                    'selected_baseline_sysml_sha256', 'generation_source_rule')
            if validation.get('packet_digest') == digest(plan) and all(saved_facts.get(key) == facts[key] for key in keys):
                warnings.extend(item for item in validation.get('review_warnings', []) if isinstance(item, str) and item)
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            # A stale/unreadable report cannot replace the frozen-source facts.
            pass
    if facts['selected_baseline_sysml_present']:
        def texts(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for item in value.values():
                    yield from texts(item)
            elif isinstance(value, list):
                for item in value:
                    yield from texts(item)
        # This fallback uses only the current packet; conditional completion
        # rules ("若...缺..."/"...缺...时") do not assert a current gap.
        claims = []
        for value in texts(plan):
            for sentence in re.split(r'[。；;\n]', value):
                if 'sysml' not in sentence.lower() or not any(word in sentence for word in ('基准', '基线')):
                    continue
                if re.search(r'缺(?:少|失)?|未(?:提供|包含|具备)|不存在', sentence) and not re.search(r'若|如果|缺[^，,]{0,24}时', sentence):
                    claims.append(sentence)
        if claims and not warnings:
            warnings.append('原方案文字保留；其中将原基准SysML列为缺失或待重补的说法与已核源码不符。当前冻结基线 '
                            + facts['baseline_version_id'] + ' 已包含SysML，无需再补原基准；确认后仅同步生成本请求候选SysML与Modelica双源码。')
    return list(dict.fromkeys(warnings))


def context(request_id):
    from .case_requests import load_request, authority_for
    ctx = load_request(request_id)
    result = {key: ctx.get(key) for key in ('request_id', 'project_id', 'case_id', 'baseline_version_id', 'raw_request', 'stage', 'displayed_digest', 'confirmation_path', 'root', 'plan', 'access_mode', 'runtime_authority', 'baseline_requires_sysml')}
    result['baseline_parameters'] = parameters(ctx['baseline_snapshot'])
    from .case_contact_family import reviewed_stair_capability
    try:
        result['input_capability']=reviewed_stair_capability(ctx['baseline_snapshot']['frozen_spec']['artifact']['modelica_source'])
    except (OSError,ValueError,KeyError,TypeError) as exc:
        result['input_capability']=dict(status='UNKNOWN',message='支持范围尚未安全核实：'+str(exc))
    result['verified_source_facts'] = verified_source_facts(ctx)
    result['baseline_requires_sysml'] = not result['verified_source_facts']['baseline_sysml_available']
    result['plan_review_warnings'] = plan_review_warnings(ctx, result['verified_source_facts'])
    result['budget'] = ledger_summary(authority_for(ctx)) if ctx.get('runtime_authority') else None
    result['last_failure'] = failure_summary(ctx, result['budget'])
    from .case_stage_facts import read_stage_facts
    result['stage_facts'] = read_stage_facts(ctx)
    result['execution_state'] = result['stage_facts']['current']['execution_state']
    result['terminal_saved'] = result['execution_state'] == 'TERMINAL'
    result['proposal_unknown'] = (Path(ctx['root']) / 'proposal-unknown.json').exists() and not (Path(ctx['root']) / 'generation.json').exists()
    return result


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='终端内部只读发现接口')
    parser.add_argument('action', choices=['discover', 'context'])
    parser.add_argument('--request-id')
    parser.add_argument('--case-id')
    args = parser.parse_args(argv)
    try:
        value = discover(args.case_id) if args.action == 'discover' else context(args.request_id)
        print(json.dumps(value, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception as exc:
        print(json.dumps(dict(error=type(exc).__name__ + ': ' + str(exc)), ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
