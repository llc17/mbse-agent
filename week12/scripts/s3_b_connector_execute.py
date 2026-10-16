"""Bounded connector repair validation; reuses already accepted B01 evidence."""
import argparse
import json
from pathlib import Path
import sys
import time

from week12.src.component_physics.contract import ROOT, canonical, digest, require_dependency_hashes
from week12.src.component_physics.ledger import Ledger
from week12.src.component_physics.runner import execute_attempt
from week12.src.toolchain import _run

REPAIR = ROOT/'results/s3-b-connector-repair-20260923-01'
RUNS = ROOT/'results/s3-b-connector-validation-20260923-01'
PRIOR = ROOT/'results/s3-b-postprocess-validation-20260923-01'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write_new(path, value):
    with path.open('xb') as handle:
        handle.write(canonical(value))
        handle.flush()
        __import__('os').fsync(handle.fileno())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--authorization', required=True)
    args = parser.parse_args()
    receipt_path = Path(args.authorization).resolve()
    if not receipt_path.is_relative_to(ROOT/'results'):
        raise ValueError('Approval path outside V11')
    proposal = read(REPAIR/'execution-proposal.json')
    approval = read(receipt_path)
    if approval.get('receipt_kind') != 'user_conversation' or not approval.get('user_message'):
        raise ValueError('Actual external user instruction required')
    if approval.get('repair_proposal_digest') != digest(proposal):
        raise ValueError('Stale connector proposal approval')
    for key in ('purpose', 'protocol_file_sha256', 'implementation_manifest_digest', 'descriptor_digests',
                'run_order', 'attempt_limit', 'historical_attempts', 'proposed_cumulative_attempt_ceiling',
                'local_additional_limit', 'local_proposed_cumulative_ceiling', 'reuse_input_digest',
                'online_product_calls', 'retry_slots'):
        if approval.get(key) != proposal[key]:
            raise ValueError('Approval binding mismatch: '+key)
    frozen = read(REPAIR/'implementation-manifest.json')
    reuse = read(REPAIR/'reuse-input-manifest.json')
    if digest(frozen) != proposal['implementation_manifest_digest'] or digest(reuse) != proposal['reuse_input_digest']:
        raise ValueError('Manifest drift')
    require_dependency_hashes(frozen)
    require_dependency_hashes(reuse)
    require_dependency_hashes(read(REPAIR/'historical-manifest.json'))
    batch = read(PRIOR/'batch-result.json')
    if batch['cumulative_attempts'] != 5 or batch['B01_numeric'] != 'PASS_WITH_PRESERVED_SIMULATION_CSVS':
        raise ValueError('Unexpected prior count or B01 acceptance')
    if read(PRIOR/'B01-reprocessed/numerical.json')['status'] != 'PASS':
        raise ValueError('B01 numerical gate not satisfied')
    ledger = Ledger(RUNS)
    ledger.prepare(historical_attempts=5)
    write_new(RUNS/'authorization-record.json', approval)
    write_new(RUNS/'local-reservation.json', {'status': 'RESERVED', 'local_cumulative_slot': 16,
                                           'case': 'R04_empty_check_result_with_error_is_failure'})
    summary = {'status': 'RUNNING', 'local_prior_consumed': 15, 'local_additional_consumed': 1,
               'B01_evidence': str(PRIOR/'B01-reprocessed'), 'completed': [],
               'not_run': list(proposal['run_order']), 'online_calls': 0, 'physical_status': 'UNVERIFIED'}
    started = time.monotonic()
    try:
        command = [sys.executable, '-B', '-m', 'unittest', '-v',
                   'week12.tests.test_component_physics_connector_report.ConnectorReportCheck.test_R04_empty_check_result_with_error_is_failure']
        result = _run(command, ROOT.parent, 10)
        for stream in ('stdout', 'stderr'):
            (RUNS/('local.'+stream+'.log')).write_text(result[stream], encoding='utf-8')
        passed = result['returncode'] == 0 and not result['timed_out'] and not result['uncertain']
        write_new(RUNS/'local-result.json', {'status': 'PASS' if passed else 'FAIL',
                  'process': {k: v for k, v in result.items() if k not in ('stdout', 'stderr')}})
        if not passed:
            summary['status'] = 'STOPPED_LOCAL_FAILURE'
            return 1
        for scenario in proposal['run_order']:
            ident, level = scenario.split('-')
            print('START '+scenario, flush=True)
            report = execute_attempt(ledger, ident, refined=level=='refined', approval=approval,
                                     frozen_sources=frozen, batch_started=started)
            item = {key: report.get(key) for key in
                    ('scenario', 'status', 'load_file', 'model_check', 'compile', 'simulation', 'physical', 'numeric', 'error')}
            summary['completed'].append(item)
            summary['not_run'].remove(scenario)
            print(json.dumps(item), flush=True)
            if report['status'] != 'PASS':
                summary['status'] = 'STOPPED_FIRST_FAILURE'
                return 1
        summary.update(status='PASS', physical_status='PASS_FOR_SIX_FROZEN_FIXTURES_ONLY')
        return 0
    except BaseException as exc:
        summary.update(status='INTERRUPTED_OR_BOUNDARY_FAILURE', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        summary['elapsed_seconds'] = time.monotonic()-started
        summary['new_reserved_attempts'] = len(list(RUNS.glob('attempt-[0-9][0-9].json')))
        summary['cumulative_attempts'] = 5+summary['new_reserved_attempts']
        write_new(RUNS/'batch-result.json', summary)


if __name__ == '__main__':
    raise SystemExit(main())
