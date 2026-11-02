"""Narrow, evidence-bound recovery of a proven local MCP import failure."""
import importlib
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import uuid
from .case_paths import ROOT, REPO, read_json, save_json, sha_file, sha_value


def preflight(flow):
    if flow.rpc:
        return dict(status='ISOLATED_FIXTURE_RPC')
    try:
        mcp=importlib.import_module('mcp')
        stdio=importlib.import_module('mcp.client.stdio')
        for owner,name in ((mcp,'ClientSession'),(mcp,'StdioServerParameters'),(stdio,'stdio_client')):
            if not hasattr(owner,name):raise ImportError('MCP dependency lacks '+name)
    except (ImportError,ModuleNotFoundError) as exc:
        save_json(flow.root/'local-mcp-preflight-failures'/(uuid.uuid4().hex+'.json'),dict(schema='v12-local-mcp-dependency-preflight-1',request_id=flow.ctx['request_id'],executable=sys.executable,status='LOCAL_FAILURE_NOT_DISPATCHED',error=type(exc).__name__+': '+str(exc),mcp_process_started=False,rpc_dispatched=False,product_http_added=0),immutable=True)
        raise RuntimeError('MCP_DEPENDENCY_PREFLIGHT_NOT_DISPATCHED: '+str(exc)) from exc
    return dict(status='PASS_LOCAL_DEPENDENCIES_ONLY',executable=sys.executable,module_path=str(mcp.__file__))


def binding(flow):
    ctx=flow.ctx
    return dict(request_id=ctx['request_id'],request_digest=ctx['request_digest'],project_id=ctx['project_id'],displayed_digest=ctx['displayed_digest'],baseline_ref=ctx['baseline_ref'],authority_sha256=ctx['runtime_authority']['sha256'])


def cache(flow,spec,delta,checked,completion):
    value=dict(schema='v12-validated-generation-candidate-1',**binding(flow),spec=spec,spec_digest=checked['spec_digest'],delta=delta,baseline_completion=completion,proposal_prepared=False)
    save_json(flow.root/'validated-generation-candidate.json',value,immutable=True)
    return value


def cached(flow):
    from .case_backend import module
    from .case_scope import check_delta
    value=read_json(flow.root/'validated-generation-candidate.json')
    if value.get('schema')!='v12-validated-generation-candidate-1' or any(value.get(k)!=v for k,v in binding(flow).items()):raise ValueError('cached generation request/plan/authority differs')
    checked=module('model_artifact').validate_artifact(value['spec'],require_sysml=True)
    delta=check_delta(flow.open(read_only=True),value['spec'],task_context=flow.ctx)
    if not checked['ready'] or checked['spec_digest']!=value['spec_digest'] or delta!=value['delta']:raise ValueError('cached validated candidate changed')
    return value['spec'],delta,checked,value['baseline_completion']


def _evidence(approval,key):
    ref=approval['evidence'][key];path=Path(ref['path']).resolve()
    if not path.is_relative_to(ROOT.resolve()) or path.is_relative_to((ROOT/'V12_old').resolve()) or sha_file(path)!=ref['sha256']:raise ValueError('local recovery evidence differs: '+key)
    return path


def validate(flow,approval_path):
    """No mutation. Original UNKNOWN alone or empty Store never permits recovery."""
    from .case_requests import authority_for, require_execution
    from .case_taskflow import projected_context_matches
    flow.ctx=require_execution(flow.ctx['request_id']);authority=authority_for(flow.ctx)
    path=Path(approval_path).resolve()
    if not path.is_relative_to((ROOT/'results').resolve()):raise PermissionError('local recovery requires an existing trusted results approval')
    approval=read_json(path)
    if approval.get('schema')!='v12-local-mcp-predispatch-repair-supplement-1' or approval.get('approval_quote') not in ('确认','批准','同意','CONFIRM') or approval.get('approval_channel')!='USER_CHAT_APPROVAL_RELAYED_BY_PRIMARY' or approval.get('additional_local_repair_rounds')!=1 or approval.get('whole_package_local_repair_total')!=2 or approval.get('product_automatic_repair_remaining')!=0 or approval.get('budget_caps_or_deadline_increased') is not False:raise PermissionError('explicit narrow local-repair supplement required')
    for key in ('request_id','request_digest','project_id','displayed_digest','authority_sha256'):
        if approval.get(key)!=binding(flow)[key]:raise ValueError('local recovery approval binding differs: '+key)
    if approval.get('hard_deadline_utc')!=authority['hard_deadline_utc']:raise ValueError('local recovery cannot refresh deadline')
    marker_path=_evidence(approval,'original_marker');marker=read_json(marker_path)
    if marker_path!=(flow.root/'proposal-unknown.json').resolve() or marker!={'spec_digest':approval['spec_digest'],'status':'UNKNOWN','error':"ModuleNotFoundError: No module named 'mcp'"}:raise ValueError('only the proven original MCP import failure is eligible')
    operation=read_json(_evidence(approval,'original_operation'))
    if operation.get('returncode')!=1 or operation.get('local_cli_exited') is not True or operation.get('setup_error') is not None or operation.get('interrupted') is not False or operation.get('last_progress',{}).get('stage')!='proposal' or json.loads(operation.get('stdout','{}')).get('error')!=marker['error'] or operation.get('stderr')!='':raise ValueError('original local pre-dispatch operation proof differs')
    first=read_json(_evidence(approval,'original_console_process'))
    formal=(REPO/'week11/.venv/Scripts/python.exe').resolve()
    probes=read_json(_evidence(approval,'dependency_environment'))
    parsed=[json.loads(row['stdout']) for row in probes.values() if row.get('returncode')==0 and not row.get('stderr')]
    old_exe=Path(first['command'][0]).resolve()
    if first.get('exit_code')!=0 or old_exe==formal or not any(Path(row['executable']).resolve()==old_exe and row.get('mcp_available') is False for row in parsed) or not any(Path(row['executable']).resolve()==formal and row.get('mcp_available') is True for row in parsed):raise ValueError('original interpreter/dependency proof differs')
    if not flow.rpc and Path(sys.executable).resolve()!=formal:raise PermissionError('recover with the formal v12.cmd interpreter only')
    old_source=_evidence(approval,'original_taskflow_source').read_text(encoding='utf-8-sig')
    channel=old_source.split('async def channel(self):',1)[1].split('async def mcp(',1)[0]
    if not channel.index('from mcp import ClientSession,StdioServerParameters')<channel.index('async with stdio_client(params)'):raise ValueError('original import-before-dispatch proof differs')
    stage=flow.effective_stage_request(flow.root/'turns/generate');response=read_json(flow.root/'turns/generate/response.json')
    context=json.loads(stage['messages'][1]['content']);rid='case_generate_'+flow.ctx['request_id']
    if stage.get('request_id')!=rid or stage.get('request_digest')!=flow.ctx['request_digest'] or not projected_context_matches(flow.ctx,context) or context.get('confirmed_plan')!=flow.ctx['plan']:raise ValueError('original complete generation request binding differs')
    with closing(sqlite3.connect(Path(flow.ctx['runtime_authority']['ledger_path']).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        row=db.execute('SELECT * FROM requests WHERE request_id=?',(rid,)).fetchone();meta=dict(db.execute('SELECT key,value FROM meta'))
        uncertain=db.execute("SELECT 1 FROM requests WHERE status IN ('unknown','reserved','dispatched') LIMIT 1").fetchone()
        attempts=db.execute('SELECT count(*) FROM process_attempts').fetchone()[0]
    if not row or row['status']!='succeeded' or any(row[k] is None for k in ('actual_tokens','actual_output','actual_cost','launched_at','finished_at')) or json.loads(row['response'])!=response or uncertain or any(k in meta for k in ('frozen','closed','retired')) or meta.get('authorization_sha256')!=binding(flow)['authority_sha256']:raise ValueError('complete known original HTTP and active original budget required')
    wire=read_json(flow.root/'http'/sha_value(rid)/'request.json')
    if wire.get('authorization_sha256')!=binding(flow)['authority_sha256'] or wire.get('request_id')!=rid or wire.get('payload_sha256')!=sha_value(wire.get('payload')) or row['content_hash']!=wire['payload_sha256']:raise ValueError('original HTTP wire/ledger binding differs')
    required=stage['tools'][0]['function']['parameters']['required']
    spec,delta,checked,completion=flow._validate_generation(response,'generate',needs_baseline='baseline_sysml_source' in required)
    if checked['spec_digest']!=approval['spec_digest']:raise ValueError('same original validated candidate required')
    proof=dict(schema='v12-local-mcp-predispatch-recovery-intent-1',**binding(flow),spec_digest=checked['spec_digest'],approval_path=str(path),approval_sha256=sha_file(path),original_marker_sha256=sha_file(marker_path),original_operation_sha256=approval['evidence']['original_operation']['sha256'],original_generation_response_sha256=sha_value(response),original_http_row_sha256=sha_value(dict(row)),original_unknown_retained=True,product_http_added=0)
    return proof,spec,delta,checked,completion,attempts


async def recover(flow,approval_path):
    proof,spec,delta,checked,completion,attempts=validate(flow,approval_path)
    intent=flow.root/'local-proposal-recovery-intent.json'
    if intent.exists() and read_json(intent)!=proof:raise ValueError('existing local recovery intent differs')
    if (flow.root/'generation.json').exists():
        saved=read_json(flow.root/'generation.json');proposal=flow.open(read_only=True).store.proposal(saved['proposal_id'])
        if proposal['spec_digest']!=checked['spec_digest'] or proposal['spec']!=spec:raise ValueError('completed recovery exact proposal differs')
        return dict(reused=True,generation=saved,product_http_added=0,mutation_added=0)
    if attempts:raise PermissionError('local proposal recovery cannot follow a process attempt')
    with flow.open(read_only=True).store.reading() as db:
        proposals=db.execute('SELECT count(*) FROM proposals WHERE project_id=?',(flow.ctx['project_id'],)).fetchone()[0]
        tasks=db.execute('SELECT count(*) FROM tasks WHERE project_id=?',(flow.ctx['project_id'],)).fetchone()[0]
    if not intent.exists() and (proposals or tasks):raise PermissionError('first local recovery requires no actual proposal or task')
    preflight(flow)
    save_json(intent,proof,immutable=True)
    result=await flow._prepare_generation(spec,delta,checked,completion,local_recovery=proof)
    save_json(flow.root/'local-proposal-recovery-result.json',dict(schema='v12-local-mcp-predispatch-recovery-result-1',intent_sha256=sha_value(proof),generation_sha256=sha_value(result),original_marker_sha256=sha_file(flow.root/'proposal-unknown.json'),original_unknown_retained=True,product_http_added=0),immutable=True)
    return dict(reused=False,generation=result,product_http_added=0)
