"""Read-only derivation from a rejected, complete generation protocol reply.

This does not reconcile HTTP, change a request, issue authority or prepare a
proposal. All output is an explicitly new, nonexecutable review artifact.
"""
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json,sqlite3
from .case_paths import ROOT,REPO,read_json,save_json,sha_file,sha_value
from .case_requests import authority_for
from .case_runtime import CaseRuntime
from .case_stream import validate_usage
from .case_strict_tools import decode_calls,generation_suffix_calls,transport_profile
from .case_product_context import projected_context_matches
from .case_taskflow import validate_import_plan,validate_request_plan,is_contact_scope


def inspect_saved_generation(flow,attempt,output_dir,*,expected_spec_digest=None,expected_baseline_digest=None):
    """Validate originals first, then export only a derived pending candidate."""
    if type(attempt) is not int or attempt<0:raise ValueError('explicit nonnegative saved attempt required')
    target=Path(output_dir).resolve()
    if not target.is_relative_to((ROOT/'results').resolve()) or target.is_relative_to((ROOT/'V12_old').resolve()):raise PermissionError('saved generation inspection writes only new current results')
    flow.refresh();ctx=flow.ctx;root=flow.root
    if target.is_relative_to(root.resolve()):raise PermissionError('inspection cannot write original request')
    authority=authority_for(ctx)  # validates immutable identity, never its clock
    if ctx.get('stage')!='CONFIRMED' or not ctx.get('confirmation_path') or is_contact_scope(ctx['plan']['scope']):raise PermissionError('confirmed noncontact generation review required')
    confirmation=read_json(ctx['confirmation_path']);plan=read_json(ctx['proposal_path'])
    if confirmation.get('request_id')!=ctx['request_id'] or confirmation.get('request_digest')!=ctx['request_digest'] or confirmation.get('channel')!='TRUSTED_LOCAL_CLI' or confirmation.get('product_self_approval') is not False or confirmation.get('displayed_digest')!=ctx['displayed_digest'] or sha_value(plan)!=ctx['displayed_digest'] or plan.get('packet')!=ctx['plan'] or plan.get('sources')!=ctx['sources'] or plan.get('baseline_ref')!=ctx['baseline_ref']:raise ValueError('original confirmation/plan/source binding drift')
    validate_import_plan(ctx,ctx['plan'])
    if not validate_request_plan(ctx['plan'],ctx['baseline_snapshot']['frozen_spec'],ctx['sources'],task_context=ctx)['executable']:raise ValueError('original confirmed plan not executable')
    stage='generate' if attempt==0 else 'generate_repair_'+str(attempt)
    request=flow.effective_stage_request(root/'turns'/stage);rid='case_'+stage+'_'+ctx['request_id']
    directory=root/'http'/sha_value(rid)
    files={'request':directory/'request.json','protocol':directory/'protocol-response.json','rejected':directory/'rejected-response.json','exit':directory/'process-exit.json','status':directory/'http-status.json','failure':root/'failures'/('generate-'+str(attempt)+'.json')}
    wire,protocol,rejected,exit_receipt,http_status,failure=[read_json(files[k]) for k in ('request','protocol','rejected','exit','status','failure')]
    if request.get('request_id')!=rid or request.get('request_digest')!=ctx['request_digest'] or failure.get('stage')!='generate' or failure.get('attempt')!=attempt or failure.get('repairable') is not True or failure.get('confirmed_plan')!=ctx['plan']:raise ValueError('original generation failure/stage binding drift')
    payload=wire.get('payload');pure=SimpleNamespace(config=authority['model_controls'],authorization=authority,task_context=ctx)
    rebuilt=CaseRuntime.build_payload(pure,request['messages'],request['tools'])[0]
    profile=transport_profile(authority['model_controls'])
    if wire.get('request_id')!=rid or wire.get('authorization_sha256')!=ctx['runtime_authority']['sha256'] or payload!=rebuilt or wire.get('payload_sha256')!=sha_value(payload) or wire.get('transport_profile')!=profile or wire.get('internal_tools')!=request['tools']:raise ValueError('original HTTP payload/internal schema/authority binding drift')
    if any(wire.get('transport_source_sha256',{}).get(name)!=sha_file(REPO/name) for name in ('week13/src/case_http.py','week13/src/case_stream.py')):raise ValueError('original HTTP dependency source drift')
    context=json.loads(request['messages'][1]['content']);contract=context['trusted_generation_contract']
    if not projected_context_matches(ctx,context) or context.get('confirmed_plan')!=ctx['plan'] or context.get('raw_request')!=ctx['raw_request'] or contract.get('baseline_ref')!=ctx['baseline_ref'] or contract.get('requirements')!=ctx['plan']['requirements'] or contract.get('design')!=ctx['plan']['design'] or contract.get('sources')!=[dict(id=x['id']) for x in ctx['sources']]:raise ValueError('original generation context/source contract drift')
    if rejected.get('response')!=protocol or rejected.get('usage_known') is not True or rejected.get('error_type')!='JSONDecodeError':raise ValueError('original known rejected protocol binding required')
    if wire.get('fixture_transport') is not False or http_status.get('http_status')!=200 or exit_receipt.get('request_id')!=rid or exit_receipt.get('returncode')!=0 or exit_receipt.get('process_launched') is not True or exit_receipt.get('local_process_terminated') is not True or exit_receipt.get('termination_requested') is not False or exit_receipt.get('timeout_classification') is not None or exit_receipt.get('child_error_type') is not None:raise ValueError('complete terminal real HTTP process required')
    with closing(sqlite3.connect(Path(ctx['runtime_authority']['ledger_path']).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        row=db.execute('SELECT * FROM requests WHERE request_id=?',(rid,)).fetchone();meta=dict(db.execute('SELECT key,value FROM meta'))
        pending=db.execute("SELECT 1 FROM requests WHERE status IN ('unknown','reserved','dispatched') LIMIT 1").fetchone()
        charged=db.execute("SELECT detail_json FROM counters WHERE kind='candidate' AND event_id=?",('request_candidate_'+ctx['request_id'],)).fetchone()
    if not row or row['status']!='rejected' or pending or any(row[k] is None for k in ('actual_tokens','actual_output','actual_cost','launched_at','finished_at')) or row['content_hash']!=sha_value(payload) or meta.get('authorization_sha256')!=ctx['runtime_authority']['sha256'] or not charged or json.loads(charged[0])!=dict(request_id=ctx['request_id'],displayed_digest=ctx['displayed_digest']):raise ValueError('known rejected charged HTTP/candidate identity required; no UNKNOWN recovery')
    usage=validate_usage(protocol.get('usage'))
    if row['actual_tokens']!=usage['total_tokens'] or row['actual_output']!=usage['completion_tokens']:raise ValueError('known protocol/ledger usage drift')
    choices=protocol.get('choices',[])
    if len(choices)!=1:raise ValueError('one complete protocol choice required')
    original_calls=choices[0].get('message',{}).get('tool_calls',[])
    _,suffix=generation_suffix_calls(original_calls,choices[0].get('finish_reason'))
    if suffix is None or failure.get('error')!=suffix['original_error'] or row['reason']!=suffix['original_error']:raise ValueError('original failure is not the exact eligible generation suffix error')
    old_reply=failure.get('response',{})
    if old_reply.get('tool_calls')!=original_calls or old_reply.get('finish_reason')!=choices[0]['finish_reason']:raise ValueError('original failure reply/protocol differs')
    parser=CaseRuntime.__new__(CaseRuntime);parser.config=authority['model_controls']
    parser._usage_cost=lambda response:(usage,row['actual_cost'])
    response,_,_=parser._parse(protocol)
    calls,mapping=decode_calls(original_calls,request['tools'],profile=profile['profile'],finish_reason=choices[0]['finish_reason'])
    response.update(tool_calls=calls,wire_tool_calls=original_calls,request_tool_output_profile=profile['profile'],request_endpoint=profile['endpoint'])
    # Same production dual-source/source-delta validation as fresh generation.
    spec,delta,checked,completion=flow._validate_generation(response,stage)
    if expected_spec_digest is not None and expected_spec_digest!=checked['spec_digest']:raise ValueError('expected candidate digest differs')
    if expected_baseline_digest is not None and expected_baseline_digest!=(completion or {}).get('completed_spec_sha256'):raise ValueError('expected baseline digest differs')
    if completion:
        completion.update(source_response_path=str(files['protocol']),source_response_sha256=sha_value(protocol),origin='DERIVED_READONLY_SAVED_PROTOCOL_REVIEW_NOT_OLD_REQUEST_SUCCESS')
    sources={str(path):sha_file(path) for path in (Path(__file__),REPO/'week13/src/case_runtime.py',REPO/'week13/src/case_strict_tools.py',REPO/'week13/src/case_taskflow.py',REPO/'week13/src/inherited/artifact_sysml.py',REPO/'week13/src/case_scope.py')}
    candidate=dict(schema='v12-detached-generation-candidate-1',stage='VALIDATED_PENDING_NEW_EXECUTION_AUTHORIZATION',origin_request_id=ctx['request_id'],origin_request_digest=ctx['request_digest'],displayed_digest=ctx['displayed_digest'],attempt=attempt,spec=spec,spec_digest=checked['spec_digest'],delta=delta,baseline_completion=completion,baseline_completion_sha256=sha_value(completion) if completion else None,source_protocol_path=str(files['protocol']),source_protocol_sha256=sha_file(files['protocol']),normalization=suffix,validation_source_sha256=sources,executable=False,old_http_status='rejected',old_request_changed=False,original_budget_restored=False)
    report=dict(schema='v12-saved-generation-readonly-review-1',status='PASS_DERIVED_CANDIDATE_PENDING_NEW_AUTHORIZATION',request_id=ctx['request_id'],request_digest=ctx['request_digest'],displayed_digest=ctx['displayed_digest'],spec_digest=checked['spec_digest'],baseline_digest=(completion or {}).get('completed_spec_sha256'),checks=checked,delta=delta,normalization=suffix,strict_schema_mapping=mapping,original_evidence_sha256={str(p):sha_file(p) for p in files.values()},original_authority_sha256=ctx['runtime_authority']['sha256'],known_original_usage=usage,known_original_cost_cny=row['actual_cost'],validation_source_sha256=sources,candidate_path=str(target/'candidate.json'),candidate_sha256=sha_value(candidate),old_http_status_preserved='rejected',original_request_and_ledger_changed=False,product_http_added=0,mcp_added=0,compile_added=0,simulation_added=0,execution_authorized=False)
    save_json(target/'candidate.json',candidate,immutable=True);save_json(target/'review.json',report,immutable=True)
    return report
