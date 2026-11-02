"""Explicit, single same-payload planning replacement after a read timeout.

This lane preserves the original UNKNOWN and freeze. It makes no claim about
remote delivery or billing and never accepts partial stream text as a reply.
"""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from .case_paths import ROOT, REPO, read_json, sha_file, sha_value
from .case_requests import authority_for, load_request

SCHEMA='v12-plan-read-timeout-replacement-approval-1'
FREEZE='unknown product HTTP; usage reservation retained'
TRANSPORT=dict(connect_timeout_s=30,read_timeout_s=600,no_progress_timeout_s=600,whole_request_deadline_s=5400)
SOURCES=('week13/src/case_http.py','week13/src/case_stream.py','week13/src/case_plan_recovery.py','week13/src/case_budget.py','week13/src/case_runtime.py','week13/src/case_taskflow.py')

def active(authority):
    now=datetime.now(timezone.utc)
    if not datetime.fromisoformat(authority['started_at_utc'].replace('Z','+00:00'))<=now<datetime.fromisoformat(authority['hard_deadline_utc'].replace('Z','+00:00')):
        raise PermissionError('original shared budget deadline expired or inactive; no replacement')

def inspect(flow, *, initial=True):
    """Only read saved evidence; do not construct runtime/budget or load keys."""
    from .case_runtime import CaseRuntime
    flow.refresh();ctx=flow.ctx;authority=authority_for(ctx);active(authority)
    if authority.get('operation_policy') or ctx.get('access_mode')=='RESULT_REVIEW_ONLY':raise PermissionError('only an ordinary initial plan supports read-timeout replacement')
    if initial and (ctx.get('stage')!='DRAFT' or ctx.get('plan') or ctx.get('confirmation_path') or any((flow.root/name).exists() for name in ('generation.json','proposal-unknown.json','pipeline.json','pipeline-partial.json'))):raise PermissionError('only unconfirmed, ungenerated initial planning may be replaced')
    old_id='case_plan_'+ctx['request_id'];new_id=old_id+'_read_timeout_replacement_1'
    stage=flow.root/'turns/plan';saved=flow.effective_stage_request(stage);folder=flow.root/'http'/sha_value(old_id)
    paths={name:folder/name for name in ('request.json','transport-error.json','timing.json','process-exit.json')}
    wire,error,timing,exit=(read_json(paths[name]) for name in paths)
    if any((folder/name).exists() for name in ('protocol-response.json','wire-evidence.json','rejected-response.json','readback-recovery.json')):raise PermissionError('saved response evidence exists; use zero-HTTP response recovery first')
    if error.get('error_type')!='ReadTimeout' or timing.get('request_id')!=old_id or timing.get('schema')!='v12-http-stream-timing-1' or timing.get('usage_known') is not False:raise PermissionError('this explicit exception requires the saved ReadTimeout with unknown usage')
    fixture=authority.get('test_only') is True and exit.get('fixture_transport') is True and exit.get('fixture_call_returned') is True and exit.get('process_launched') is False
    real=exit.get('local_process_terminated') is True and type(exit.get('returncode')) is int and exit.get('process_launched') is True and exit.get('fixture_transport') is False and exit.get('child_error_type')=='ReadTimeout'
    if exit.get('request_id')!=old_id or not (fixture or real):raise PermissionError('original local HTTP process exit must be proven')
    payload=CaseRuntime.build_payload(SimpleNamespace(config=authority['model_controls'],authorization=authority,task_context=ctx),saved['messages'],saved['tools'])[0]
    if saved.get('request_id')!=old_id or saved.get('request_digest')!=ctx['request_digest'] or wire.get('request_id')!=old_id or wire.get('authorization_sha256')!=ctx['runtime_authority']['sha256'] or wire.get('payload')!=payload or wire.get('payload_sha256')!=sha_value(payload) or wire.get('internal_tools') not in (None,saved['tools']):raise PermissionError('original business request, payload, baseline or authorization binding differs')
    controls=authority['model_controls']
    if any(controls.get(k)!=v for k,v in TRANSPORT.items() if k!='read_timeout_s') or controls.get('read_timeout_s') not in (180,600):raise PermissionError('only the explicitly approved read timeout may change')
    ledger=Path(ctx['runtime_authority']['ledger_path'])
    with closing(sqlite3.connect(ledger.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row;old=db.execute('SELECT * FROM requests WHERE request_id=?',(old_id,)).fetchone();meta=dict(db.execute('SELECT key,value FROM meta'))
        pending={r[0] for r in db.execute("SELECT request_id FROM requests WHERE status IN ('unknown','reserved','dispatched')")}
        used=db.execute("SELECT count(*),coalesce(sum(coalesce(actual_tokens,reserve_tokens)),0),coalesce(sum(coalesce(actual_output,reserve_output)),0),coalesce(sum(coalesce(actual_cost,reserve_cost)),0) FROM requests WHERE status<>'not_dispatched'").fetchone()
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='plan_read_timeout_replacements'").fetchone()
        retry=db.execute('SELECT * FROM plan_read_timeout_replacements').fetchone() if exists else None
    if not old or old['status']!='unknown' or not old['launched_at'] or old['content_hash']!=sha_value(payload) or any(old[k] is not None for k in ('actual_tokens','actual_output','actual_cost')):raise PermissionError('original UNKNOWN and full reservation must remain exact')
    if meta.get('authorization_sha256')!=ctx['runtime_authority']['sha256'] or meta.get('frozen')!=FREEZE or meta.get('connect_retry_required') or any('closed' in k.lower() or 'retired' in k.lower() or 'CLOSED' in str(v).upper() or 'RETIRED' in str(v).upper() for k,v in meta.items()):raise PermissionError('original ledger freeze/binding differs or budget closed/retired')
    if initial and (pending!={old_id} or retry or (stage/'read-timeout-replacement.json').exists()):raise PermissionError('another uncertainty or previous replacement blocks this one-time action')
    caps=authority['caps'];c=controls
    if initial and (used[0]+3>caps['http_requests'] or used[1]+c['context_window_tokens']>caps['total_tokens'] or used[2]+c['max_tokens']>caps['total_output_tokens'] or used[3]+c['maximum_request_reserve_cny']>caps['product_cny']):raise PermissionError('replacement and protected final HTTP slots exceed original budget')
    hashes={str(p.resolve()):sha_file(p) for p in list(paths.values())+[stage/'request.json']}
    template=dict(schema=SCHEMA,status='PENDING_USER_APPROVAL',approval_channel='USER_CHAT_APPROVAL',approval_quote='',approved_at_utc=None,
        request_id=ctx['request_id'],request_digest=ctx['request_digest'],project_id=ctx['project_id'],baseline_ref=ctx['baseline_ref'],runtime_authority=ctx['runtime_authority'],
        original_http_request_id=old_id,replacement_http_request_id=new_id,payload_sha256=sha_value(payload),original_request_row_sha256=sha_value(dict(old)),
        evidence_hashes=hashes,effective_source_hashes={name:sha_file(REPO/name) for name in SOURCES},effective_transport=TRANSPORT,
        caps=caps,hard_deadline_utc=authority['hard_deadline_utc'],maximum_new_http_requests=1,
        original_unknown_and_full_reservation_preserved=True,remote_delivery_and_original_charge='UNKNOWN',duplicate_billing_possible=True,
        automatic_retries=0,generation_compile_simulation_authorized_by_this_approval=False,test_only=authority.get('test_only',False))
    return dict(approval_template=template,request=saved,payload=payload,original=dict(old),stage=str(stage),ledger_path=str(ledger.resolve()),used=list(used),product_http_added=0)

def approved(flow,path):
    info=inspect(flow);path=Path(path).resolve()
    if not path.is_relative_to((ROOT/'results').resolve()):raise PermissionError('approval must be in current trusted results')
    value=read_json(path);expected=info['approval_template']
    if any(value.get(k)!=v for k,v in expected.items() if k not in ('status','approval_quote','approved_at_utc','approval_channel')) or value.get('status')!='APPROVED' or value.get('approval_channel') not in ('USER_CHAT_APPROVAL','TRUSTED_LOCAL_CLI') or not isinstance(value.get('approval_quote'),str) or not value['approval_quote'].strip():raise PermissionError('explicit user-approved read-timeout exception differs from the bound template')
    try:stamp=datetime.fromisoformat(value['approved_at_utc'].replace('Z','+00:00'));assert stamp.tzinfo is not None and stamp<=datetime.now(timezone.utc)
    except (ValueError,TypeError,KeyError,AssertionError):raise PermissionError('actual approval timestamp required') from None
    proof=dict(value,approval_path=str(path),approval_sha256=sha_file(path),ledger_path=info['ledger_path'],original_freeze=FREEZE)
    return info,proof

def registered(db,ctx,authority,request_id=None,*,require_plan=False):
    """Validate the narrow append-only exception using the caller's transaction."""
    if not ctx or not db.execute("SELECT 1 FROM sqlite_master WHERE name='plan_read_timeout_replacements'").fetchone():return None
    item=db.execute('SELECT * FROM plan_read_timeout_replacements WHERE request_context_id=?',(ctx['request_id'],)).fetchone()
    if not item:return None
    proof=json.loads(item['proof_json']);old=db.execute('SELECT * FROM requests WHERE request_id=?',(proof['original_http_request_id'],)).fetchone();new=db.execute('SELECT * FROM requests WHERE request_id=?',(proof['replacement_http_request_id'],)).fetchone();meta=dict(db.execute('SELECT key,value FROM meta'))
    if not old or sha_value(dict(old))!=proof['original_request_row_sha256'] or old['status']!='unknown' or meta.get('frozen')!=FREEZE or meta.get('connect_retry_required') or any('closed' in k.lower() or 'retired' in k.lower() or 'CLOSED' in str(v).upper() or 'RETIRED' in str(v).upper() for k,v in meta.items()):return None
    if proof.get('request_digest')!=ctx['request_digest'] or proof.get('baseline_ref')!=ctx['baseline_ref'] or proof.get('runtime_authority')!=ctx['runtime_authority'] or proof.get('caps')!=authority['caps'] or proof.get('hard_deadline_utc')!=authority['hard_deadline_utc'] or proof.get('effective_transport')!=TRANSPORT:return None
    if db.execute("SELECT 1 FROM requests WHERE (actual_tokens>reserve_tokens OR actual_output>reserve_output OR actual_cost>reserve_cost)").fetchone():return None
    pending={r[0] for r in db.execute("SELECT request_id FROM requests WHERE status IN ('unknown','reserved','dispatched')")}
    if pending-{old['request_id'],request_id}:return None
    try:
        if sha_file(proof['approval_path'])!=proof['approval_sha256']:return None
        if any(sha_file(name)!=expected for name,expected in proof['evidence_hashes'].items()) or any(sha_file(REPO/name)!=expected for name,expected in proof['effective_source_hashes'].items()):return None
        approval=read_json(proof['approval_path'])
        if any(approval.get(k)!=v for k,v in proof.items() if k not in ('approval_path','approval_sha256','ledger_path','original_freeze')):return None
    except (OSError,ValueError,KeyError,TypeError):return None
    if request_id==proof['replacement_http_request_id'] and not require_plan:
        if not new or new['status']=='reserved' and new['launched_at'] is None and new['content_hash']==proof['payload_sha256']:return proof
    if not new or new['status']!='succeeded' or new['content_hash']!=proof['payload_sha256'] or any(new[k] is None for k in ('actual_tokens','actual_output','actual_cost','response','launched_at','finished_at')):return None
    try:
        current=load_request(ctx['request_id']);stage=Path(current['root'])/'turns/plan'
        response=read_json(stage/'response.json');journal=read_json(stage/'read-timeout-replacement.json')
        if json.loads(new['response'])!=response or journal.get('proof_sha256')!=sha_value(proof):return None
        if require_plan or request_id!=proof['replacement_http_request_id']:
            validation=read_json(Path(current['root'])/'plan-validation.json')
            if not current.get('plan') or validation.get('packet_digest')!=sha_value(current['plan']) or validation.get('executable') is not True:return None
    except (OSError,ValueError,KeyError,TypeError):return None
    return proof if request_id is None or request_id==proof['replacement_http_request_id'] or request_id.endswith('_'+ctx['request_id']) else None
