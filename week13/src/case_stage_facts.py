"""Request-bound stage facts; no runtime, ledger mutation or result persistence.

``stage_facts`` is pure. ``read_stage_facts`` reads only the selected request's
Store tasks and explicitly bound siblings in its immutable authorization.
Neither function trusts product prose or pipeline aggregation files.
"""
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path


TERMINAL = {'succeeded', 'failed', 'cancelled', 'interrupted'}
RUNNING = {'queued', 'running', 'cancel_requested'}


def _map(value):
    return value if isinstance(value, dict) else {}


def stage_facts(ctx, *, tasks=(), executions=(), failures=(), generation=None,
                proposal_unknown=False, read_errors=()):
    """Summarize already verified structured inputs, without reading or writing.

    Callers supply snapshots verified against the frozen Store proposal/source.
    Foreign request/session/project records cause UNKNOWN, never a ready label.
    """
    rid = ctx['request_id']
    issues = list(read_errors)
    allowed = {'case_execute_' + rid, 'case_execute_repair_1_' + rid}
    own = []
    for task in tasks:
        if (task.get('session_id') != ctx.get('session_id') or
                task.get('project_id') != ctx.get('project_id') or
                task.get('request_id') not in allowed):
            issues.append('任务与当前请求、session或project不一致')
        else:
            own.append(task)
    own.sort(key=lambda x: (x.get('created_at') or '', x.get('task_id') or ''))
    task = own[-1] if own else {}
    snapshots = [row for row in executions if row.get('task_id') == task.get('task_id')]
    if any(row.get('task_id') not in {item.get('task_id') for item in own} for row in executions):
        issues.append('执行快照不属于当前请求任务')
    snapshot = snapshots[0] if len(snapshots) == 1 else {}
    if task and (not snapshot or any(snapshot.get(key) != task.get(key)
                                    for key in ('task_id', 'project_id', 'version_id'))):
        issues.append('执行版本与任务绑定不足或冲突')
    if snapshot and (not snapshot.get('source_sha256') or not snapshot.get('spec_digest')):
        issues.append('执行源码摘要或规格摘要缺失')
    if generation and snapshot and generation.get('spec_digest') != snapshot.get('spec_digest'):
        issues.append('当前生成回执与执行源码规格不一致')
    generated = bool(generation or snapshot)
    planned = bool(ctx.get('plan'))
    history = []
    for failure in failures:
        item = {key: failure.get(key) for key in ('stage', 'attempt', 'error', 'repairable', 'evidence_path')}
        resolved = (item['stage'] == 'plan' and planned) or (item['stage'] == 'generate' and generated)
        item['status'] = 'RESOLVED_WITH_LATER_VALIDATION' if resolved else 'UNRESOLVED'
        history.append(item)
    unresolved = next((row for row in reversed(history) if row['status'] == 'UNRESOLVED'), None)
    phase = (unresolved or {}).get('stage')
    outcome = _map(snapshot.get('outcome'))
    tool = _map(outcome.get('tool_result'))
    mission = _map(outcome.get('mission_assessment'))
    state = str(task.get('state', '')).lower()
    if task and state not in TERMINAL | RUNNING | {'uncertain'}:
        issues.append('任务状态无法识别')
    execution_state = ('UNCERTAIN' if issues or state == 'uncertain' or proposal_unknown else
                       'RUNNING' if state in RUNNING else
                       'TERMINAL' if state in TERMINAL else 'NOT_RUN')
    facts = dict(request_id=rid, project_id=ctx.get('project_id'),
                 request_digest=ctx.get('request_digest'), session_id=ctx.get('session_id'),
                 execution_state=execution_state, state=state or None,
                 plan='PASS' if planned else 'FAIL' if phase == 'plan' else 'NOT_RUN',
                 generate='PASS' if generated else 'FAIL' if phase == 'generate' else 'NOT_RUN',
                 compile=_map(tool.get('compile')).get('status', 'UNKNOWN' if task else 'NOT_RUN'),
                 simulation=_map(tool.get('simulation')).get('status', 'UNKNOWN' if task else 'NOT_RUN'),
                 tool_status=snapshot.get('tool_status', 'UNKNOWN' if task else 'NOT_RUN'),
                 chain_status=snapshot.get('chain_status', 'UNKNOWN' if task else 'NOT_RUN'),
                 task_status=snapshot.get('task_status', 'UNKNOWN'),
                 completion_status=mission.get('completion_status', 'UNKNOWN'),
                 physical_status='UNKNOWN', failure_history=history,
                 unresolved_failure=unresolved, issues=issues,
                 binding_basis='EXACT_REQUEST_SESSION_PROJECT_AND_VERIFIED_FROZEN_SNAPSHOT')
    for key in ('task_id', 'version_id', 'source_sha256', 'spec_digest'):
        facts[key] = snapshot.get(key) or task.get(key)
    if issues:
        for key in ('generate', 'compile', 'simulation', 'tool_status', 'chain_status', 'task_status', 'completion_status'):
            facts[key] = 'UNKNOWN'
    if execution_state == 'UNCERTAIN':
        summary = '执行状态UNKNOWN；绑定或结果尚未确定，不能显示待执行或自动重发。'
    elif execution_state == 'RUNNING':
        summary = '验证进行中；尚无终态结果，不能重复执行。'
    elif execution_state == 'TERMINAL':
        summary = ('执行已结束；编译 ' + str(facts['compile']) + '，仿真 ' + str(facts['simulation']) +
                   '，工具/链路 ' + str(facts['tool_status']) + '/' + str(facts['chain_status']) + '。')
        if facts['completion_status'] == 'OBSERVATION_INTERVAL_INCOMPLETE':
            summary += '观察区间内任务未完成（OBSERVATION_INTERVAL_INCOMPLETE），物理能力UNKNOWN。'
    elif phase == 'plan':
        error = str(unresolved.get('error', '')).lower()
        summary = ('方案JSON解析失败' if 'json' in error else '方案校验失败') + '；双源码生成/编译/仿真均未运行（NOT_RUN）。'
    elif phase == 'generate':
        summary = '双源码生成阶段失败；编译/仿真未运行（NOT_RUN）。'
    elif generated:
        summary = '双源码已生成；尚无执行任务，编译/仿真未运行（NOT_RUN）。'
    else:
        summary = '本请求尚无执行任务；双源码生成/编译/仿真未运行（NOT_RUN）。'
    facts['summary'] = summary
    return facts


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _verified_snapshot(svc, task, ctx):
    """Verify small frozen records; never rescan CSV or rerun its checker."""
    from .case_requests import digest
    from week11.src.store import safe_id
    for key in ('task_id', 'project_id', 'version_id', 'session_id', 'proposal_id'):
        safe_id(task[key], key)
    proposal = svc.store.proposal(task['proposal_id'])
    spec, display = proposal['spec'], proposal['display']
    source = spec['artifact']['modelica_source']
    source_sha = hashlib.sha256(source.encode('utf8')).hexdigest()
    if (any(proposal.get(key) != task.get(key) for key in ('project_id', 'session_id')) or
            digest(spec) != proposal['spec_digest'] or digest(display) != proposal['displayed_digest'] or
            display.get('source_sha256') != source_sha or display.get('artifact') != spec['artifact']):
        raise ValueError('Store任务/提案/源码绑定不一致')
    result = task.get('result') or {}
    snapshot = {key: task.get(key) for key in ('task_id', 'project_id', 'version_id', 'state')}
    snapshot.update(source_sha256=source_sha, spec_digest=proposal['spec_digest'], outcome={})
    manifest_path = result.get('outcome_manifest_path')
    manifest_sha = result.get('outcome_manifest_sha256')
    if manifest_path or manifest_sha:
        base = (Path(ctx['project_root']) / 'projects' / task['project_id'] / 'versions' / task['version_id']).resolve()
        path = Path(manifest_path)
        path = (path if path.is_absolute() else base / path).resolve()
        if path != base / 'artifact-manifest.json' or hashlib.sha256(path.read_bytes()).hexdigest() != manifest_sha:
            raise ValueError('执行manifest路径或摘要不一致')
        manifest = _read(path)
        expected = dict(task_id=task['task_id'], project_id=task['project_id'],
                        spec_digest=proposal['spec_digest'], source_sha256=source_sha,
                        owner=task.get('worker_owner'), context_id=display.get('execution_context_id'),
                        context_sha256=display.get('execution_context_sha256'))
        if proposal.get('operation') == 'modify':
            expected['artifact_baseline'] = spec.get('baseline_ref')
        if manifest.get('schema') != 'v11-artifact-manifest-1' or manifest.get('binding') != expected:
            raise ValueError('执行manifest与当前版本源码绑定不一致')
        names = ['spec.json', 'frozen-model.mo', 'outcome.json']
        if spec['artifact'].get('sysml_source'):
            names.append('frozen-model.sysml')
        for name in names:
            if hashlib.sha256((base / name).read_bytes()).hexdigest() != _map(manifest.get('files', {}).get(name)).get('sha256'):
                raise ValueError('冻结源码或结果摘要不一致：' + name)
        if _read(base / 'spec.json') != spec or (base / 'frozen-model.mo').read_text(encoding='utf8') != source:
            raise ValueError('冻结源码/规格与Store不一致')
        outcome = _read(base / 'outcome.json')
        committed = {key: value for key, value in result.items()
                     if key not in {'outcome_manifest_path', 'outcome_manifest_sha256', 'worker_exit_code', 'worker_pid'}}
        if outcome != manifest.get('outcome') or outcome != committed:
            raise ValueError('Store/outcome/manifest结果不一致')
        snapshot['outcome'] = outcome
    elif task.get('state') == 'succeeded':
        raise ValueError('成功终态缺少manifest结果绑定')
    snapshot.update({key: snapshot['outcome'].get(key, result.get(key, 'UNKNOWN'))
                     for key in ('tool_status', 'chain_status', 'task_status')})
    return snapshot


def _read_current(ctx):
    root = Path(ctx['root'])
    failures, errors, tasks, snapshots = [], [], [], []
    for path in sorted((root / 'failures').glob('*.json'), key=lambda p: p.stat().st_mtime_ns):
        try:
            failure = _read(path)
            failures.append(dict(failure, evidence_path=str(path)))
        except (OSError, ValueError, TypeError) as exc:
            errors.append('失败记录无法读回：' + str(exc))
    generation = None
    try:
        if (root / 'generation.json').is_file():
            generation = _read(root / 'generation.json')
        db_path = Path(ctx['db_path'])
        if not db_path.is_file():
            raise FileNotFoundError('请求绑定的Store不存在')
        if ctx.get('access_mode') != 'RESULT_REVIEW_ONLY':
            # Restrict the SQL before opening snapshots: do not enumerate other
            # sessions or use the project's latest failure as this request's.
            with closing(sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                ids = [row[0] for row in db.execute(
                    'SELECT task_id FROM tasks WHERE session_id=? AND project_id=? ORDER BY created_at, task_id',
                    (ctx['session_id'], ctx['project_id']))]
            if ids or generation:
                from .case_backend import service
                svc = service(db_path, ctx['session_id'], read_only=True, task_context=ctx)
                if svc._current_project()['project_id'] != ctx['project_id']:
                    raise ValueError('当前session的project选择与请求不一致')
                if generation:
                    proposal = svc.store.proposal(generation['proposal_id'])
                    if (any(proposal.get(key) != ctx.get(key) for key in ('project_id', 'session_id')) or
                            proposal.get('spec_digest') != generation.get('spec_digest')):
                        raise ValueError('生成回执与当前请求Store提案不一致')
                for task_id in ids:
                    task = svc.store.task(task_id)
                    tasks.append(task)
                    snapshots.append(_verified_snapshot(svc, task, ctx))
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        errors.append('只读任务/版本核验未通过：' + str(exc))
    facts = stage_facts(ctx, tasks=tasks, executions=snapshots, failures=failures,
                        generation=generation, read_errors=errors,
                        proposal_unknown=(root / 'proposal-unknown.json').exists() and not generation)
    facts['aggregate_receipt_present'] = any((root / name).is_file() for name in ('pipeline.json', 'pipeline-partial.json'))
    return facts


def read_stage_facts(ctx, *, include_approved_requests=True):
    """Read immutable, explicitly bound inputs only; never create any receipt.

    ``approved_requests`` includes only context_bindings from the current
    validated authority. Ad-hoc history/registry globbing is intentionally absent.
    """
    from .case_requests import authority_for, load_request
    current = _read_current(ctx)
    result = dict(schema='v12-request-stage-facts-1', request_id=ctx['request_id'],
                  current=current, approved_requests=[], authority_sha256=None,
                  read_only=True, product_prose_authoritative=False)
    if not include_approved_requests or not ctx.get('runtime_authority'):
        return result
    try:
        authority = authority_for(ctx)
        result['authority_sha256'] = ctx['runtime_authority']['sha256']
        for rid, expected_digest in authority.get('context_bindings', {}).items():
            if rid == ctx['request_id']:
                continue
            other = load_request(rid)
            if (other['request_digest'] != expected_digest or other['project_id'] != ctx['project_id'] or
                    _map(other.get('runtime_authority')).get('sha256') != result['authority_sha256'] or
                    authority_for(other) != authority):
                raise ValueError('同预算请求绑定不一致：' + rid)
            result['approved_requests'].append(_read_current(other))
    except (OSError, ValueError, KeyError, TypeError, PermissionError) as exc:
        # Do not show a partially verified sibling set as a complete package.
        result['approved_requests'] = []
        result['approved_requests_error'] = str(exc)
    return result


def stage_lines(stage_context, *, request_id=None):
    """Display trusted caller-supplied facts before product interpretation."""
    value = _map(stage_context)
    current = _map(value.get('current'))
    if (value.get('schema') != 'v12-request-stage-facts-1' or
            value.get('request_id') != current.get('request_id') or
            request_id is not None and current.get('request_id') != request_id):
        return []
    lines = ['【系统阶段事实】' + str(current.get('summary', 'UNKNOWN'))]
    if value.get('authority_sha256'):
        lines += ['同预算请求 ' + row['request_id'] + '：' + row['summary']
                  for row in value.get('approved_requests', [])]
    return lines
