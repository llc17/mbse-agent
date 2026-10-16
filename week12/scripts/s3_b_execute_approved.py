"""Execute the exact B batch authorized in the 2026-09-23 conversation.

The recorded message is a provenance record of that external instruction, not
an identity authentication mechanism or an API for arbitrary approvals.
"""
import ast
import hashlib
import json
from pathlib import Path
import time

from week12.src.component_physics.contract import ROOT, canonical, digest, require_dependency_hashes
from week12.src.component_physics.ledger import Ledger
from week12.src.component_physics.runner import execute_attempt, OMC

READY = ROOT/'results/s3-b-ready-20260923-01'
RUNS = ROOT/'results/s3-b-execution-20260923-01'
USER_MESSAGE = '按照你给出的顺序开始：先补齐执行前检查 → 确认新增有限预算 → 按顺序验证六组场景，首个失败立即停下定位'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def row(path):
    return {'path': str(path.resolve()), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def main():
    development = ROOT/'results/s3-b-development-20260922-01'
    preflight = ROOT/'results/s3-b-preflight-20260923-01'
    local_path = ROOT/'results/s3-b-local-remaining-20260923-01/result.json'
    local = read(local_path)
    preflight_status = read(preflight/'status.json')
    if local['status'] != 'LOCAL_PASS' or local['consumed'] != 12:
        raise ValueError('Local checks incomplete')
    if preflight_status['status'] != 'PREFLIGHT_PASS_PENDING_EXECUTION_APPROVAL':
        raise ValueError('Preflight not passed')
    original = read(development/'implementation-manifest.json')
    require_dependency_hashes(original)
    historical = read(ROOT/'results/s3-b-recovery-20260922-01/before-restoration.json')['historical_execution_files']
    require_dependency_hashes(historical)
    libraries = read(preflight/'installed-library-manifest.json')
    require_dependency_hashes(libraries)
    require_dependency_hashes([read(preflight/'tool-identity.json')['omc']])
    extras = [ROOT/'tests/modelica_scalar_harness.py', ROOT/'tests/test_component_physics_remaining.py',
              ROOT/'scripts/s3_b_remaining_checks.py', ROOT/'scripts/s3_b_preflight.py', Path(__file__),
              local_path, preflight/'status.json', preflight/'probe-result.json',
              preflight/'help.stdout.log', preflight/'tool-identity.json', OMC]
    for path in extras:
        if path.suffix == '.py':
            ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    unique = {}
    for item in original+libraries+[row(path) for path in extras]:
        unique[str(Path(item['path']).resolve())] = item
    frozen = list(unique.values())
    if READY.exists() or RUNS.exists():
        raise FileExistsError('Fixed batch already exists; no resume, retry or alternate directory')
    READY.mkdir()
    proposal = read(development/'execution-proposal.json')
    approval = dict(proposal)
    approval.update(status='AUTHORIZED_BY_USER_CONVERSATION', receipt_kind='user_conversation',
                    user_message=USER_MESSAGE, local_consumed=12, local_cases_remaining=[],
                    implementation_manifest_digest=digest(frozen),
                    authorization_context='User instructed the stated sequence after the explicit new-12/cumulative-13 proposal; physical specification and thresholds unchanged.',
                    note='Post-preflight evidence revision; preserves original proposal and all historical failures.')
    (READY/'implementation-manifest.json').write_bytes(canonical(frozen))
    (READY/'authorization-record.json').write_bytes(canonical(approval))
    ledger = Ledger(RUNS)
    ledger.prepare(historical_attempts=1)
    summary = {'status': 'RUNNING', 'new_attempt_limit': 12, 'historical_attempts': 1,
               'cumulative_ceiling': 13, 'online_calls': 0, 'completed': [],
               'not_run': list(approval['run_order']), 'physical_status': 'UNVERIFIED'}
    started = time.monotonic()
    try:
        for scenario in approval['run_order']:
            ident, level = scenario.split('-')
            print('START '+scenario, flush=True)
            result = execute_attempt(ledger, ident, refined=level=='refined', approval=approval,
                                     frozen_sources=frozen, batch_started=started)
            summary['completed'].append({'scenario': scenario, 'status': result['status'],
                                         'compile': result['compile'], 'simulation': result['simulation'],
                                         'physical': result['physical'], 'numeric': result['numeric'],
                                         'error': result.get('error')})
            summary['not_run'].remove(scenario)
            print(json.dumps(summary['completed'][-1]), flush=True)
            if result['status'] != 'PASS':
                summary['status'] = 'STOPPED_FIRST_FAILURE'
                break
        else:
            summary.update(status='PASS', physical_status='PASS_FOR_SIX_FROZEN_FIXTURES_ONLY')
    except BaseException as exc:
        summary.update(status='INTERRUPTED_OR_BOUNDARY_FAILURE', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        summary['elapsed_seconds'] = time.monotonic()-started
        summary['new_reserved_attempts'] = len(list(RUNS.glob('attempt-*.json')))
        summary['cumulative_attempts'] = 1+summary['new_reserved_attempts']
        (RUNS/'batch-result.json').write_bytes(canonical(summary))
    return 0 if summary['status']=='PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
