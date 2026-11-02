"""Small lifecycle/index projections. These are never evidence verification.

Only exact task metadata and bounded, already saved process receipts are read.
No CSV is opened and no physics checker, baseline resolver or snapshot runs.
"""
import json
from pathlib import Path

from week11.src.store import safe_id

INDEX_KIND = 'ARTIFACT_EXECUTION_INDEX'
ACTIVE_STATES = ('queued', 'running', 'cancel_requested')


def _exit_status(receipt, state):
    """An absent exit observation says nothing about whether a PID is alive."""
    if receipt and receipt.get('timed_out'):
        return 'TIMED_OUT'
    if receipt and receipt.get('uncertain'):
        return 'UNCERTAIN'
    code = receipt.get('returncode') if receipt else None
    if code is None:
        return 'WAITING_EXIT_RECEIPT' if state in ACTIVE_STATES else 'UNKNOWN'
    return 'FINISHED_EXIT_0' if code == 0 else 'FINISHED_NONZERO_EXIT'


def _small_json(path, limit=262144):
    path = Path(path)
    if not path.is_file():
        return None
    for parent in (path, *path.parents):
        if parent.is_symlink() or getattr(parent, 'is_junction', lambda: False)():
            raise ValueError('lifecycle receipt links are refused')
    if path.stat().st_size > limit:
        raise ValueError('lifecycle receipt exceeds small-file limit')
    return json.loads(path.read_text(encoding='utf-8-sig'))


def execution_index(service):
    project = service._current_project()
    # Extract only bounded scalar fields; loading result_json/spec_json would
    # unnecessarily deserialize full independent reports and source envelopes.
    with service.store.reading() as db:
        rows = db.execute('''SELECT t.task_id,t.project_id,t.version_id,t.session_id,
            t.state,t.progress,t.error,t.created_at,t.updated_at,p.spec_digest,
            json_extract(p.display_json,'$.source_sha256') AS source_sha256,
            json_extract(p.spec_json,'$.design.parameters') AS parameters_json,
            length(json_extract(p.spec_json,'$.artifact.sysml_source')) AS sysml_length,
            json_extract(t.result_json,'$.outcome_manifest_sha256') AS manifest_sha256,
            json_extract(t.result_json,'$.tool_status') AS recorded_tool_status,
            json_extract(t.result_json,'$.chain_status') AS recorded_chain_status,
            json_extract(t.result_json,'$.task_status') AS recorded_task_status
            FROM tasks t JOIN proposals p ON p.proposal_id=t.proposal_id
            WHERE t.project_id=? AND json_extract(p.spec_json,'$.backend')='model-artifact'
            ORDER BY t.created_at,t.rowid''', (project['project_id'],)).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        parameters=json.loads(item.pop('parameters_json') or '[]')
        item['parameters']={p['name']:p['value'] for p in parameters if isinstance(p,dict) and 'name' in p and 'value' in p}
        item['source_body_has_sysml']=bool(item.pop('sysml_length'))
        item['recorded_requires_sysml']=not item['source_body_has_sysml']
        for key in ('tool_status', 'chain_status', 'task_status'):
            item['recorded_' + key] = item.get('recorded_' + key) or 'UNKNOWN'
            item[key] = 'UNKNOWN'
        item.update(record_kind=INDEX_KIND, published=False, read_only=True,
                    evidence_status='NOT_RECHECKED', verification_status='NOT_RECHECKED',
                    recorded_conclusions_verified=False, physical_acceptance_status='NOT_VERIFIED')
        items.append(item)
    return dict(current_version_id=project['current_version_id'],
                selected_version_id=project['selected_version_id'], record_kind=INDEX_KIND,
                published=False, read_only=True, items=items)


def execution_progress(service, task_id):
    safe_id(task_id, 'task_id')
    with service.store.reading() as db:
        row=db.execute('''SELECT task_id,session_id,project_id,proposal_id,request_id,
            request_sha256,version_id,state,progress,worker_pid,worker_owner,error,created_at,updated_at,
            json_extract(result_json,'$.tool_status') AS recorded_tool_status,
            json_extract(result_json,'$.chain_status') AS recorded_chain_status,
            json_extract(result_json,'$.task_status') AS recorded_task_status,
            json_extract(result_json,'$.outcome_manifest_sha256') AS recorded_manifest_sha256
            FROM tasks WHERE task_id=?''',(task_id,)).fetchone()
    if row is None:raise ValueError('unknown task')
    task=dict(row)
    service._require_current_project(task['project_id'])
    # Return metadata only, not the potentially large staged independent report.
    # The existing execution_status tool remains the full task/result contract.
    result={key:task.pop('recorded_'+key) or 'UNKNOWN' for key in ('tool_status','chain_status','task_status')}
    manifest_sha=task.pop('recorded_manifest_sha256')
    task['result_projection']='LIFECYCLE_ONLY_USE_EXECUTION_STATUS_FOR_FULL_EVIDENCE'
    from .inherited.contracts import inside_week12, WEEK12_ROOT
    root = Path(getattr(service, 'project_root', WEEK12_ROOT))
    base = inside_week12(root / 'projects' / task['project_id'] / 'versions' / task['version_id'])
    stages = {}
    for kind, name in (('compile', 'compile.command.json'), ('simulation', 'simulation.command.json')):
        receipt = _small_json(base / 'tool' / name)
        stages[kind] = dict(status=_exit_status(None, task['state']), process_alive='NOT_INFERRED')
        if receipt:
            if receipt.get('task_id') != task_id or Path(receipt.get('cwd', '')).resolve() != (base / 'tool').resolve():
                raise ValueError('lifecycle process receipt identity differs')
            stages[kind] = dict(status=_exit_status(receipt, task['state']), process_alive='NOT_INFERRED',
                returncode=receipt.get('returncode'), timed_out=receipt.get('timed_out'),
                uncertain=receipt.get('uncertain'), elapsed_s=receipt.get('elapsed_s'),
                evidence_path=str(base / 'tool' / name), content_verified=False)
    exit_path = root / 'projects' / task['project_id'] / 'task-logs' / (task_id + '.process-exit.json')
    exit_receipt = _small_json(exit_path)
    if exit_receipt:
        if exit_receipt.get('task_id') != task_id or exit_receipt.get('owner') != task.get('worker_owner'):
            raise ValueError('lifecycle worker receipt identity differs')
        stages['worker_exit'] = dict(exit_receipt, status=_exit_status(exit_receipt, task['state']), process_alive='NOT_INFERRED', evidence_path=str(exit_path), content_verified=False)
    else:
        stages['worker_exit'] = dict(status=_exit_status(None, task['state']), process_alive='NOT_INFERRED', content_verified=False)
    pending_path=exit_path.with_suffix('.finalization-pending.json')
    pending=_small_json(pending_path)
    if pending:
        if pending.get('task_id')!=task_id:raise ValueError('finalization receipt identity differs')
        stages['finalization']=dict(status='PENDING_RECORDED' if task['state'] in ('queued','running','cancel_requested') else 'HISTORICAL_PENDING_RECORD',
            error=pending.get('error'),evidence_path=str(pending_path),content_verified=False)
    stages['independent_check'] = dict(status='RECORDED_OUTCOME_AVAILABLE' if manifest_sha else 'NO_COMPLETE_SAVED_OUTCOME', content_verified=False)
    summary = {key: task.get(key) for key in ('project_id', 'version_id', 'task_id', 'session_id', 'state', 'progress', 'updated_at')}
    summary.update(record_kind='ARTIFACT_EXECUTION_PROGRESS', read_only=True,
                   evidence_status='NOT_RECHECKED' if manifest_sha else 'INCOMPLETE',
                   verification_status='NOT_RECHECKED', recorded_conclusions_verified=False,
                   stage_facts=stages)
    for key in ('tool_status', 'chain_status', 'task_status'):
        summary['recorded_' + key] = result.get(key, 'UNKNOWN')
    return task, summary
