"""Narrow proof for one terminated HTTP400 diagnostic, never a connection retry."""
from pathlib import Path
import sqlite3
from contextlib import closing
from .case_paths import read_json,sha_file,sha_value,REPO


def prepare_rejection_proof(ctx,authority,original_request_id,diagnostic_request_id,primary_review_path,independent_review_path,old_row,rebuilt_payload):
    if original_request_id!='case_plan_'+ctx['request_id'] or diagnostic_request_id!=original_request_id+'_http400_diagnostic_1':raise PermissionError('only this original plan and single diagnostic are eligible')
    folder=Path(ctx['root'])/'http'/sha_value(original_request_id)
    paths={name:folder/name for name in ('request.json','http-status.json','process-exit.json','transport-error.json','timing.json','transport-profile.json')}
    primary_path=Path(primary_review_path).resolve();independent_path=Path(independent_review_path).resolve()
    primary=read_json(primary_path);independent=read_json(independent_path)
    if primary.get('schema')!='v12-http400-diagnostic-primary-review-1' or primary.get('decision')!='APPROVE_ONE_SAME_PAYLOAD_HTTP400_DIAGNOSTIC' or primary.get('channel')!='TRUSTED_LOCAL_DEVELOPMENT_REVIEW':raise PermissionError('explicit primary diagnostic decision required')
    expected=dict(request_id=ctx['request_id'],original_http_request_id=original_request_id,diagnostic_request_id=diagnostic_request_id,authority_sha256=ctx['runtime_authority']['sha256'],execution_approval_sha256=authority['execution_package']['sha256'],independent_review_sha256=sha_file(independent_path))
    if any(primary.get(key)!=value for key,value in expected.items()) or primary.get('same_payload_only') is not True or primary.get('maximum_diagnostic_HTTP')!=1:raise PermissionError('primary diagnostic decision binding differs')
    if independent.get('schema')!='v12-known-http-rejection-recovery-review-1' or independent.get('actual_scope',{}).get('authorization_sha256')!=ctx['runtime_authority']['sha256']:raise PermissionError('independent HTTP400 review binding differs')
    source_hashes=primary.get('implementation_source_hashes',{})
    required={'week13/src/'+name for name in ('case_http.py','case_stream.py','case_runtime.py','case_budget.py','case_taskflow.py','case_http_rejection.py')}
    if set(source_hashes)!=required or any(sha_file(REPO/name)!=expected for name,expected in source_hashes.items()):raise ValueError('reviewed diagnostic implementation source differs')
    for name,expected_sha in independent.get('original_evidence',{}).items():
        if name in paths and sha_file(paths[name])!=expected_sha:raise ValueError('original reviewed HTTP evidence changed: '+name)
    saved=read_json(paths['request.json']);status=read_json(paths['http-status.json']);exit=read_json(paths['process-exit.json']);timing=read_json(paths['timing.json']);error=read_json(paths['transport-error.json']);profile=read_json(paths['transport-profile.json'])
    payload=saved['payload'];digest=sha_value(payload)
    if saved.get('request_id')!=original_request_id or saved.get('authorization_sha256')!=ctx['runtime_authority']['sha256'] or saved.get('payload_sha256')!=digest or rebuilt_payload!=payload or old_row.get('content_hash')!=digest:raise ValueError('original complete payload/internal tools/authority differ')
    if old_row.get('request_id')!=original_request_id or old_row.get('status')!='unknown' or any(old_row.get(key) is not None for key in ('actual_tokens','actual_output','actual_cost')):raise PermissionError('original financial UNKNOWN must remain')
    if status.get('http_status')!=400 or error.get('error_type')!='HTTPStatusError' or exit.get('request_id')!=original_request_id or exit.get('child_error_type')!='HTTPStatusError' or exit.get('local_process_terminated') is not True or exit.get('process_launched') is not True or exit.get('fixture_transport') is not False or type(exit.get('returncode')) is not int or exit.get('returncode')==0 or exit.get('termination_requested') is not False or not exit.get('observed_exit_at_utc'):raise PermissionError('not a proven naturally terminated real HTTP400')
    if timing.get('request_id')!=original_request_id or any(timing.get(key)!=0 for key in ('receive_chunk_count','received_bytes','event_count','content_bytes','reasoning_bytes','tool_argument_bytes')):raise PermissionError('response/SSE/tool bytes or missing timing block diagnostic')
    if profile.get('payload_sha256')!=digest or profile.get('request_id')!=original_request_id or profile.get('profile')!=saved.get('transport_profile') or profile.get('profile',{}).get('endpoint')!='https://api.deepseek.com/beta/chat/completions':raise PermissionError('original strict endpoint/profile differs')
    if not saved.get('internal_tools') or any(tool.get('function',{}).get('strict') is not True for tool in payload.get('tools',[])):raise PermissionError('original strict tools missing')
    for name,expected_sha in saved.get('transport_source_sha256',{}).items():
        if independent.get('source_hashes_at_read',{}).get(name)!=expected_sha:raise ValueError('original transport source lacks independent reviewed binding')
    forbidden=[folder/name for name in ('protocol-response.json','rejected-response.json','wire-evidence.json','readback-recovery.json')]
    forbidden += [Path(ctx['root'])/name for name in ('generation.json','pipeline.json','pipeline-partial.json','proposal-unknown.json','contact-source-review-pending.json')]
    forbidden += [Path(ctx['root'])/'turns/plan/response.json']
    if ctx.get('plan') or ctx.get('confirmation_path') or any(path.exists() for path in forbidden):raise PermissionError('existing plan/response/tool evidence blocks diagnostic')
    with closing(sqlite3.connect(Path(ctx['db_path']).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        for table in ('proposals','tasks'):
            if db.execute('SELECT count(*) FROM '+table+' WHERE session_id=? AND project_id=?',(ctx['session_id'],ctx['project_id'])).fetchone()[0]:raise PermissionError('local proposal/task side effect blocks diagnostic')
    stage=Path(ctx['root'])/'turns/plan/request.json'
    stage_request=read_json(stage)
    if stage_request.get('request_id')!=original_request_id or stage_request.get('request_digest')!=ctx['request_digest'] or stage_request.get('tools')!=saved['internal_tools']:raise ValueError('original stage journal/internal tools differ')
    evidence={str(path.resolve()):sha_file(path) for path in list(paths.values())+[stage,primary_path,independent_path,Path(ctx['runtime_authority']['path']),Path(authority['execution_package']['path'])]}
    evidence.update({str((REPO/name).resolve()):expected for name,expected in source_hashes.items()})
    return dict(schema='v12-http400-diagnostic-proof-1',request_context_id=ctx['request_id'],request_digest=ctx['request_digest'],
        original_request_id=original_request_id,diagnostic_request_id=diagnostic_request_id,authority_sha256=ctx['runtime_authority']['sha256'],
        ledger_path=str(Path(authority['ledger_path']).resolve()),payload_sha256=digest,original_request_row_sha256=sha_value(old_row),
        original_freeze='unknown product HTTP; usage reservation retained',evidence_hashes=evidence,
        original_stage_request_sha256=sha_file(stage),internal_tools_sha256=sha_value(saved['internal_tools']),
        http_status=400,local_process_terminated=True,no_local_tool_side_effects=True,maximum_diagnostic_requests=1,
        original_unknown_and_full_reservation_preserved=True,ordinary_calls_still_blocked=True,recursive_diagnostics_forbidden=True,
        purpose='ONLY_OBSERVE_SAME_PAYLOAD_HTTP_ERROR_BODY_OR_SAVE_COMPLETE_RESPONSE_NO_TOOL_DISPATCH',
        repair_event='http400-protocol-repair-1',primary_review_path=str(primary_path),independent_review_path=str(independent_path))


def prepare_empty_object_correction(ctx,authority,primary_path,new_payload,old_rows):
    """Prove the only payload delta is the observed empty-object wire fix."""
    from copy import deepcopy
    from .case_strict_tools import prepare_tools,EMPTY_GUIDE
    from .case_scope import STAIR_SCOPE_PACKET
    original='case_plan_'+ctx['request_id'];diagnostic=original+'_http400_diagnostic_1';corrected=original+'_empty_object_correction_1'
    primary_path=Path(primary_path).resolve();primary=read_json(primary_path)
    if primary.get('schema')!='v12-http-empty-object-correction-primary-review-1' or primary.get('decision')!='APPROVE_ONE_EMPTY_OBJECT_SCHEMA_CORRECTION_WITHIN_REPAIR1' or primary.get('channel')!='TRUSTED_LOCAL_DEVELOPMENT_REVIEW':raise PermissionError('explicit primary known-correction decision required')
    for key,value in dict(request_id=ctx['request_id'],authority_sha256=ctx['runtime_authority']['sha256'],correction_request_id=corrected,execution_approval_sha256=authority['execution_package']['sha256']).items():
        if primary.get(key)!=value:raise PermissionError('primary known-correction binding differs: '+key)
    source_hashes=primary.get('implementation_source_hashes',{})
    required={'week13/src/'+name for name in ('case_http.py','case_stream.py','case_runtime.py','case_budget.py','case_taskflow.py','case_http_rejection.py','case_strict_tools.py')}
    if set(source_hashes)!=required or any(sha_file(REPO/name)!=expected for name,expected in source_hashes.items()):raise ValueError('reviewed correction implementation differs')
    origin=Path(ctx['root'])/'http'/sha_value(original);diag=Path(ctx['root'])/'http'/sha_value(diagnostic)
    saved=read_json(origin/'request.json');diag_request=read_json(diag/'request.json');error=read_json(diag/'http-error.json');exit=read_json(diag/'process-exit.json')
    if diag_request['payload']!=saved['payload'] or error.get('payload_sha256')!=saved['payload_sha256'] or error.get('request_id')!=diagnostic or error.get('http_status')!=400 or error.get('body_complete') is not True or error.get('body_truncated') is not False or error.get('error_json',{}).get('error',{}).get('code')!='invalid_request_error' or not error['error_json']['error'].get('message','').startswith('An object with no properties is not allowed.'):raise PermissionError('exact observed empty-object rejection required')
    if exit.get('termination_requested') is not False or exit.get('local_process_terminated') is not True or type(exit.get('returncode')) is not int or exit.get('returncode')==0:raise PermissionError('diagnostic did not terminate naturally')
    if set(old_rows)!={original,diagnostic} or any(row.get('status')!='unknown' or any(row.get(key) is not None for key in ('actual_tokens','actual_output','actual_cost')) for row in old_rows.values()):raise PermissionError('both financial UNKNOWN rows must remain')
    expected=deepcopy(saved['payload']);expected['tools'],audit=prepare_tools(saved['internal_tools'])
    expected['messages'][0]['content']+=EMPTY_GUIDE
    if expected!=new_payload or saved['payload']['messages'][0]['content'].endswith(EMPTY_GUIDE):raise ValueError('corrected payload changed beyond unique empty-object adapter and transport guide')
    if not any('DECLARED_EMPTY_OBJECT_TO_CLOSED_TRANSPORT_MARKER' in str(item) for item in audit):raise ValueError('no observed empty-object branch was corrected')
    files=[primary_path,origin/'request.json',origin/'http-status.json',origin/'process-exit.json',origin/'http400-diagnostic-authorization.json',diag/'request.json',diag/'http-status.json',diag/'http-error.json',diag/'process-exit.json',Path(ctx['runtime_authority']['path']),Path(authority['execution_package']['path'])]
    evidence={str(path.resolve()):sha_file(path) for path in files};evidence.update({str((REPO/name).resolve()):value for name,value in source_hashes.items()})
    return dict(schema='v12-http-empty-object-correction-proof-1',request_context_id=ctx['request_id'],request_digest=ctx['request_digest'],
        authority_sha256=ctx['runtime_authority']['sha256'],ledger_path=str(Path(authority['ledger_path']).resolve()),
        original_request_id=original,diagnostic_request_id=diagnostic,correction_request_id=corrected,
        preserved_unknown_rows={rid:sha_value(row) for rid,row in old_rows.items()},original_freeze='unknown product HTTP; usage reservation retained',
        approved_context_bindings=authority['context_bindings'],evidence_hashes=evidence,origin_request_root=str(Path(ctx['root']).resolve()),
        baseline_ref=ctx['baseline_ref'],expected_parameters=dict(m=85,I_pitch=20,T_main_max=60,T_nose_max=25,stepH=.18,stepD=.26),expected_scope=STAIR_SCOPE_PACKET,
        old_payload_sha256=saved['payload_sha256'],new_payload_sha256=sha_value(new_payload),wire_schema_audit=audit,transport_system_guide=EMPTY_GUIDE,
        business_messages_and_sources_preserved=True,repair_round=1,additional_repair_charge=0,ordinary_calls_require_validated_in_scope_plan=True,
        no_recursive_correction=True,primary_review_path=str(primary_path))
