"""Positive owned-exit proof for a narrow historical active-row exception.

No task, budget, result or old receipt is updated. Missing/uncertain evidence
continues to block. A PID's absence or presence is never used as exit proof.
"""
from contextlib import closing
from datetime import datetime,timezone
import hashlib,json,sqlite3
from pathlib import Path

from week11.src.store import digest_json,safe_id
CURRENT_ROOT=Path(__file__).resolve().parents[2]


def _case_paths(store):
    return Path(store.db_path).parent,Path(store.db_path)


def _path(value):
    path=Path(value).absolute()
    if path.suffix.casefold()=='.csv' or any(p.casefold()=='v12_old' or p.casefold().startswith('.env') or p.casefold() in ('v8_holdout_private','v8_formal_private') for p in path.parts):
        raise ValueError('unsafe historical proof path')
    if not path.is_relative_to(CURRENT_ROOT.absolute()):raise ValueError('historical proof escapes current storage')
    for parent in (path,*path.parents):
        if parent.is_symlink() or getattr(parent,'is_junction',lambda:False)():raise ValueError('historical proof links refused')
    return path.resolve()


def _read(path,files):
    path=_path(path)
    if path.suffix.casefold()!='.json' or path.stat().st_size>2*1024*1024:raise ValueError('historical proof must be bounded JSON')
    raw=path.read_bytes();files[str(path)]=hashlib.sha256(raw).hexdigest()
    return json.loads(raw.decode('utf-8-sig'))


def _time(value):
    result=datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None:raise ValueError('historical proof time is not UTC-bound')
    return result


def stopped_history_evidence(store,db,task,new_proposal):
    """Return a fully bound positive proof or raise; no blanket state bypass."""
    task=dict(task);files={}
    if task['state'] not in ('cancel_requested','uncertain'):raise ValueError('task lifecycle is not a stopped-history candidate')
    for key in ('task_id','project_id','proposal_id','session_id','worker_owner'):
        safe_id(task.get(key),key)
    if task['session_id']==new_proposal['session_id']:raise ValueError('same session cannot exempt its own unfinished execution')
    old_row=db.execute('SELECT * FROM proposals WHERE proposal_id=?',(task['proposal_id'],)).fetchone()
    if old_row is None:raise ValueError('historical proposal absent')
    old=dict(old_row);old_spec=json.loads(old['spec_json']);old_display=json.loads(old['display_json'])
    new_display=json.loads(new_proposal['display_json'])
    if (old['project_id']!=task['project_id'] or old['session_id']!=task['session_id']
            or old_spec.get('backend')!='model-artifact' or digest_json(old_spec)!=old['spec_digest']
            or task['request_sha256']!=digest_json(dict(proposal_id=old['proposal_id']))
            or digest_json(old_display)!=old['displayed_digest']):raise ValueError('historical proposal identity/content differs')
    confirmation=json.loads(old['confirmation_json'] or 'null')
    if (not isinstance(confirmation,dict) or confirmation.get('spec_digest')!=old['spec_digest']
            or confirmation.get('displayed_digest')!=old['displayed_digest']):raise ValueError('historical frozen proposal confirmation differs')
    if (json.loads(new_proposal['spec_json']).get('backend')!='model-artifact'
            or not new_display.get('execution_context_id') or not old_display.get('execution_context_id')
            or new_display['execution_context_id']==old_display['execution_context_id']):raise ValueError('execution contexts are not independent')
    intent=db.execute('SELECT owner FROM car_launch_intents WHERE task_id=?',(task['task_id'],)).fetchone()
    if intent is None or intent['owner']!=task['worker_owner']:raise ValueError('historical launch intent owner differs')
    root,source_db=_case_paths(store);root=_path(root);source_db=_path(source_db)
    exit_path=root/'projects'/task['project_id']/'task-logs'/(task['task_id']+'.process-exit.json')
    receipt=_read(exit_path,files)
    if (receipt.get('schema')!='v12-owned-worker-process-exit-1' or receipt.get('task_id')!=task['task_id']
            or receipt.get('owner')!=task['worker_owner'] or type(receipt.get('pid')) is not int or receipt['pid']<=0
            or type(receipt.get('returncode')) is not int or receipt.get('uncertain') is not False
            or receipt.get('cleanup_confirmed') is not True
            or receipt.get('exit_observation') not in ('OWNED_TIMEOUT_CLEANUP_AND_POLL','OWNED_CANCEL_CLEANUP_AND_POLL','OWNED_POPEN_WAIT_RETURN')):
        raise ValueError('no certain owned exit and complete tree cleanup proof')
    safe_id(receipt.get('attempt_id'),'worker_attempt')
    prefix='case_execute_'
    if not task['request_id'].startswith(prefix):raise ValueError('historical request binding is unsupported')
    rid=safe_id(task['request_id'][len(prefix):],'historical_request')
    folder=root/'requests'/rid
    ctx=_read(folder/'task-context.json',files);request=_read(folder/'request.json',files)
    if (ctx.get('request_id')!=rid or ctx.get('session_id')!=task['session_id'] or ctx.get('project_id')!=task['project_id']
            or _path(ctx.get('project_root',''))!=root or _path(ctx.get('db_path',''))!=source_db
            or _path(ctx.get('root',''))!=_path(folder) or digest_json(request)!=ctx.get('request_digest')):
        raise ValueError('historical request/context/Store binding differs')
    approval_path=_path(ctx['confirmation_path']);approval=_read(approval_path,files)
    if (ctx.get('stage')!='CONFIRMED' or confirmation.get('channel')!='trusted_local_request_design_confirmation'
            or approval_path.parent!=_path(folder/'confirmations')
            or confirmation.get('request_id')!=rid or confirmation.get('request_displayed_digest')!=ctx.get('displayed_digest')
            or confirmation.get('approval')!=approval or approval.get('schema')!='v12-trusted-request-confirmation-1'
            or approval.get('request_id')!=rid or approval.get('request_digest')!=ctx['request_digest']
            or approval.get('displayed_digest')!=ctx.get('displayed_digest') or approval.get('channel')!='TRUSTED_LOCAL_CLI'
            or approval.get('product_self_approval') is not False or not approval.get('approval_text')):
        raise ValueError('historical request approval/confirmation digest differs')
    context_path=_path(ctx['execution_context_path']);context=_read(context_path,files)
    if (context_path!=_path(folder/'execution-context.json') or context.get('context_id')!=old_display['execution_context_id']
            or files[str(context_path)]!=old_display.get('execution_context_sha256')):
        raise ValueError('historical execution context differs from proposal')
    ref=ctx['runtime_authority'];authority_path=_path(ref['path']);authority=_read(authority_path,files)
    ledger=_path(ref['ledger_path'])
    if files[str(authority_path)]!=ref.get('sha256') or ledger!=authority_path.parent/'budget.sqlite':
        raise ValueError('historical authority/ledger binding differs')
    binding_kind='DIRECT_CONTEXT'
    if authority.get('context_bindings',{}).get(rid)!=ctx['request_digest']:
        binding_kind='SHARED_CONTEXT'
        binding_path=_path(ref['binding_path']);_read(binding_path,files)
        if files[str(binding_path)]!=ref.get('binding_sha256') or binding_path.parent!=authority_path.parent/'context-bindings':
            raise ValueError('historical shared binding digest/path differs')
    # Reuse both existing direct/shared binding branches, schema, trusted user
    # approval, finite controls/caps and unique-ledger validation. This check
    # does not require active time and does not issue/recover any authorization.
    from ..case_requests import authority_for
    if (authority_for(ctx)!=authority or authority.get('project_id')!=task['project_id']
            or authority.get('permissions',{}).get('execution') is not True):
        raise ValueError('historical trusted authority differs')
    now=datetime.now(timezone.utc);ended=_time(receipt['observed_at_utc'])
    if not _time(authority['started_at_utc'])<=ended<=now or not _time(authority['hard_deadline_utc'])<now:
        raise ValueError('historical authority is not expired or exit time is invalid')
    with closing(sqlite3.connect(ledger.as_uri()+'?mode=ro',uri=True,timeout=5)) as budget:
        budget.row_factory=sqlite3.Row;budget.execute('PRAGMA query_only=ON');budget.execute('BEGIN')
        meta=budget.execute("SELECT value FROM meta WHERE key='authorization_sha256'").fetchone()
        if meta is None or meta[0]!=ref['sha256']:raise ValueError('historical ledger belongs to a different authority')
        row=budget.execute('SELECT * FROM process_attempts WHERE attempt_id=?',(receipt['attempt_id'],)).fetchone()
        if row is None:raise ValueError('owned worker reservation absent')
        row=dict(row);detail=json.loads(row['detail_json'])
        if (row['kind']!='worker' or row['state'] not in ('STARTED','FINISHED') or row['pid']!=receipt['pid']
                or detail.get('task_id')!=task['task_id'] or detail.get('owner')!=task['worker_owner']
                or not row['launched_at'] or not _time(row['launched_at'])<=ended):
            raise ValueError('owned exit does not match its launched reservation')
        if row['state']=='FINISHED' and json.loads(row['result_json'] or 'null')!=receipt:
            raise ValueError('worker final budget receipt conflicts with owned exit')
        children=[dict(r) for r in budget.execute("SELECT kind,state,result_json FROM process_attempts WHERE kind IN ('compile','simulation') AND json_extract(detail_json,'$.task_id')=?",(task['task_id'],))]
        for child in children:
            result=json.loads(child['result_json'] or 'null')
            if (child['state']!='FINISHED' or not isinstance(result,dict) or result.get('uncertain') is not False
                    or result.get('cleanup_confirmed') is not True or type(result.get('returncode')) is not int):
                raise ValueError('historical child process remains unproven or active')
    return dict(schema='v12-stopped-history-positive-exit-1',task_id=task['task_id'],
        project_id=task['project_id'],old_state=task['state'],owner=task['worker_owner'],
        attempt_id=receipt['attempt_id'],launcher_pid=receipt['pid'],worker_pid=task['worker_pid'],
        exit_receipt_path=str(_path(exit_path)),exit_receipt_sha256=files[str(_path(exit_path))],
        authority_sha256=ref['sha256'],authority_binding=binding_kind,authority_expired=True,source_files_sha256=files,
        original_task_or_budget_modified=False,basis='POSITIVE_OWNED_EXIT_AND_CLEANUP_NOT_PID_ABSENCE')


def active_exclusions(store,db,new_proposal):
    proofs=[]
    for task in db.execute("SELECT * FROM tasks WHERE state IN ('queued','running','cancel_requested','uncertain')").fetchall():
        try:proofs.append(stopped_history_evidence(store,db,task,new_proposal))
        except (OSError,ValueError,KeyError,TypeError,sqlite3.Error) as exc:
            raise ValueError('another execution is active') from exc
    return proofs
