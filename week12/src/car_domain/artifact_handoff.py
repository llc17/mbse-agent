"""Local one-shot recovery of a cached proposal after proven server shutdown.

No model/HTTP call, approval, task launch, or deletion of an old dispatch receipt.
Call prepare_cached_proposal_recovery before starting a fresh MCP server.
"""
import ctypes, hashlib, json, os, sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from .contracts import inside_week12

def receipt_path(root,request_id,kind):
    return root/'cli-turns'/(hashlib.sha256(request_id.encode()).hexdigest()+'-'+kind+'.json')

def file_sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def stopped_pid(pid):
    if type(pid) is not int or pid<=0:raise ValueError('invalid stopped process identity')
    if os.name=='nt':
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.restype=ctypes.c_void_p
        handle=kernel.OpenProcess(0x100000,False,pid) # SYNCHRONIZE; read-only
        if not handle:
            if ctypes.get_last_error()==87:return True # PID no longer exists
            raise RuntimeError('cannot determine old process state')
        try:return kernel.WaitForSingleObject(ctypes.c_void_p(handle),0)==0
        finally:kernel.CloseHandle(ctypes.c_void_p(handle))
    try:os.kill(pid,0)
    except ProcessLookupError:return True
    return False

def recovery_receipt(root,request_id,session):
    root=inside_week12(root);path=receipt_path(root,request_id,'proposal-recovery')
    if not path.is_file():return None
    value=json.loads(path.read_text(encoding='utf8'))
    if value.get('schema')!='v11-cached-proposal-recovery-1' or value.get('request_id')!=request_id or value.get('session_id')!=session or value.get('db')!=str(root/'projects.sqlite'):raise ValueError('cached recovery binding changed')
    for kind,sha in value['receipt_sha256'].items():
        if file_sha(receipt_path(root,request_id,kind))!=sha:raise ValueError('cached recovery source/dispatch changed')
    if file_sha(Path(value['stopped_process_evidence']))!=value['stopped_process_evidence_sha256']:raise ValueError('stopped process evidence changed')
    return value

def prepare_cached_proposal_recovery(root,request_id,session,stopped_process_evidence):
    root=inside_week12(root);evidence=inside_week12(stopped_process_evidence)
    from .mcp_adapter import exclusive_server_lock
    with exclusive_server_lock(root/'projects.sqlite'):
        identities=json.loads(evidence.read_text(encoding='utf-8-sig'))
        if not isinstance(identities,list) or not identities or not all(stopped_pid(p['ProcessId']) for p in identities):raise RuntimeError('old process stop not proven; no recovery')
        if not any('week12.src.car_domain.mcp_adapter' in p.get('CommandLine','') and str(root/'projects.sqlite').lower() in p['CommandLine'].lower() for p in identities):raise ValueError('stopped service evidence belongs to another database')
        dispatched=json.loads(receipt_path(root,request_id,'proposal-dispatch').read_text(encoding='utf8'))
        if receipt_path(root,request_id,'tool-results').exists() or receipt_path(root,request_id,'proposal-recovery-attempt').exists():raise RuntimeError('handoff completed or recovery already attempted; use read-only lookup')
        with closing(sqlite3.connect((root/'projects.sqlite').as_uri()+'?mode=ro',uri=True)) as db:
            row=db.execute('SELECT current_project_id FROM sessions WHERE session_id=?',(session,)).fetchone()
            if not row or not row[0]:raise ValueError('no selected recovery project')
            project=row[0]
            proposals=db.execute('SELECT proposal_id FROM proposals WHERE session_id=? AND project_id=? AND spec_digest=?',(session,project,dispatched['spec_digest'])).fetchall()
            if proposals or db.execute('SELECT 1 FROM tasks WHERE session_id=? AND project_id=?',(session,project)).fetchone():raise RuntimeError('proposal/task exists; recover by read-only lookup only')
        value=dict(schema='v11-cached-proposal-recovery-1',request_id=request_id,session_id=session,project_id=project,db=str(root/'projects.sqlite'),spec_digest=dispatched['spec_digest'],created_at=datetime.now(timezone.utc).isoformat(),receipt_sha256={kind:file_sha(receipt_path(root,request_id,kind)) for kind in ('request','response','proposal-dispatch')},stopped_process_evidence=str(evidence),stopped_process_evidence_sha256=file_sha(evidence),stopped_pids=[p['ProcessId'] for p in identities],exclusive_server_lock_verified=True,matching_proposals=0,project_tasks=0,max_recovery_dispatches=1,automatic_resend=False)
        target=receipt_path(root,request_id,'proposal-recovery')
        if target.exists():
            prior=recovery_receipt(root,request_id,session)
            if prior['spec_digest']!=value['spec_digest'] or prior['project_id']!=project:raise ValueError('recovery changed')
            return prior
        with target.open('x',encoding='utf8') as handle:json.dump(value,handle,ensure_ascii=False,indent=2)
        return value
