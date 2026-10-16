"""Read committed reference evidence without admitting or reopening execution."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
from week11.src.store import digest_json
from .contracts import RESULT_ROOT, inside_week12

PROFILE = 'REFERENCE_TEMPLATE_GENERATION_ONLY'


def verify_historical_reference(manifest, result, task, proposal, ledger_path=None):
    # The caller has already rehashed the manifest and every listed artifact.
    if task.get('state') not in {'succeeded','failed','cancelled','interrupted','uncertain'}:
        raise ValueError('historical evidence requires a terminal task')
    c = proposal.get('confirmation') or {}
    if (proposal.get('session_id') != task.get('session_id')
            or proposal.get('project_id') != task.get('project_id')
            or proposal.get('proposal_id') != task.get('proposal_id')
            or proposal.get('spec', {}).get('profile') != PROFILE
            or digest_json(proposal['spec']) != proposal.get('spec_digest')
            or digest_json(proposal['display']) != proposal.get('displayed_digest')):
        raise ValueError('historical proposal identity or digest mismatch')
    expected_confirmation = dict(proposal_id=proposal['proposal_id'],project_id=task['project_id'],
        spec_digest=proposal['spec_digest'],displayed_digest=proposal['displayed_digest'],
        baseline_version_id=proposal['baseline_version_id'],operation=proposal['operation'],
        project_revision=proposal['project_revision'],profile=PROFILE,
        channel='EXISTING_USER_AUTHORIZATION_IMPORT',human_new_approval=False)
    if any(c.get(k) != v for k,v in expected_confirmation.items()):
        raise ValueError('historical confirmation mismatch')
    display = proposal['display']
    if (not c.get('execution_scope_id') or not c.get('authorization_sha256')
            or c.get('execution_scope_id') != display.get('execution_scope_id')
            or c.get('reference_contract_sha256') != display.get('reference_contract_sha256')
            or c.get('reference_source_sha256') != proposal['spec'].get('reference_ref',{}).get('sha256')):
        raise ValueError('historical approval scope mismatch')
    expected = dict(task_id=task['task_id'],project_id=task['project_id'],proposal_id=proposal['proposal_id'],
        session_id=task['session_id'],spec_digest=proposal['spec_digest'],displayed_digest=proposal['displayed_digest'],
        profile=PROFILE,owner=task['worker_owner'],reference_source_sha256=c['reference_source_sha256'],
        reference_contract_sha256=c['reference_contract_sha256'],authorization_sha256=c['authorization_sha256'])
    if any(manifest.get('binding',{}).get(k) != v for k,v in expected.items()):
        raise ValueError('historical manifest binding mismatch')
    payload = {k:v for k,v in result.items() if k not in {'outcome_manifest_path','outcome_manifest_sha256'}}
    if manifest.get('outcome') != payload:
        raise ValueError('historical immutable outcome mismatch')
    if task['state'] != 'succeeded':
        if (result.get('reference_chain_status') == 'REFERENCE_CHAIN_PASS'
                or result.get('agent_execution_status') == 'COMPLETED_REFERENCE'
                or result.get('vehicle_task_status') in {'PASS','TRUSTED_FAIL'}):
            raise ValueError('failed task cannot claim successful evidence')
        return
    required = dict(agent_execution_status='COMPLETED_REFERENCE',reference_chain_status='REFERENCE_CHAIN_PASS',
        vehicle_task_status='UNKNOWN',physical_acceptance_status='NOT_ACCEPTED_KNOWN_SIMPLIFICATION',
        numeric_convergence='NOT_VERIFIED',tool_status='PASS',reference_replay_status='PASS',
        source_algebra_status='PASS_IN_REFERENCE_SCOPE',evidence_status='COMPLETE_REFERENCE_ONLY',
        published_version_id=None,whole_V11_complete=False,profile=PROFILE)
    if any(k not in result or result[k] != v for k,v in required.items()):
        raise ValueError('historical success tuple invalid')
    ledger = json.loads(Path(ledger_path or RESULT_ROOT/'ledger.json').read_bytes())
    scopes = [v for v in ledger.values() if isinstance(v,dict) and v.get('scope_id') == c['execution_scope_id']]
    if len(scopes) != 1:
        raise ValueError('historical execution account missing or ambiguous')
    s = scopes[0]
    if (s.get('status') not in {'EXECUTING_REFERENCE','CLOSED_REFERENCE_CHAIN_PASS'} or s.get('stop_reason')
            or s.get('authorization_sha256') != c['authorization_sha256']
            or s.get('proposal_sha256') != c['reference_contract_sha256']):
        raise ValueError('historical execution account not successful')
    claim = s.get('execution_claim',{})
    if any(claim.get(k) != v for k,v in dict(task_id=task['task_id'],owner=task['worker_owner'],pid=task['worker_pid']).items()):
        raise ValueError('historical worker owner mismatch')
    workers = [p for p in s.get('parents',[]) if p.get('label') == 'car-worker:'+task['task_id']]
    if len(workers) != 1:
        raise ValueError('historical worker exit missing')
    worker = workers[0]
    if (worker.get('status') != 'RETURNED' or worker.get('actual_exit_code') != 0
            or worker.get('pid') != task['worker_pid'] or worker.get('own_s',float('inf')) > s['caps']['parent_own_s']):
        raise ValueError('historical worker did not exit successfully')
    # Use the recorded completion time, never today's clock or current scope.
    completed = datetime.fromisoformat(task['updated_at'])
    began = datetime.fromisoformat(s['started_utc'].replace('Z','+00:00'))
    if not 0 <= (completed-began).total_seconds() <= s['caps']['active_s']:
        raise ValueError('historical task completion outside its budget')
    if any(s['counts'].get(k,0) > v for k,v in s['caps'].items() if k in s['counts']):
        raise ValueError('historical execution count exceeded')
    if (s['product_tokens'] > s['caps']['product_tokens'] or s['product_usd'] > s['caps']['product_usd']
            or s['external_s'] > s['caps']['external_s']):
        raise ValueError('historical execution cost exceeded')
    if s.get('gates',{}).get('verification') != 'PASS_REFERENCE_ONLY':
        raise ValueError('historical verification missing')
    if s.get('actual_reference_dynamics',{}).get('csv_sha256') != manifest['files'].get('scene-01/result.csv',{}).get('sha256'):
        raise ValueError('historical dynamics mismatch')
    # Preserve archived admission integrity without comparing old code to newer code.
    admission = s.get('admission',{})
    for name in ('binding','readiness','review'):
        path = inside_week12(admission.get(name+'_path',''))
        if hashlib.sha256(path.read_bytes()).hexdigest() != admission.get(name+'_sha256'):
            raise ValueError('historical admission evidence changed')
