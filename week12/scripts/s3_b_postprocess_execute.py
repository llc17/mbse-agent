"""Approved bounded reprocessing of B01, followed by ten remaining scene attempts."""
import argparse
import json
from pathlib import Path
import sys
import time

from week12.src.component_physics.contract import ROOT, canonical, digest, require_dependency_hashes
from week12.src.component_physics.ledger import Ledger
from week12.src.component_physics.runner import execute_attempt
from week12.src.toolchain import _run

REPAIR = ROOT/'results/s3-b-postprocess-repair-20260923-01'
RUNS = ROOT/'results/s3-b-postprocess-validation-20260923-01'
PRIOR = ROOT/'results/s3-b-script-repair-validation-20260923-01'
CHECKS = ['R02_stream_preserves_csv_guards_and_events', 'R03_single_read_and_fail_closed_pair']


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write_new(path, value):
    with path.open('xb') as handle:
        handle.write(canonical(value))
        handle.flush()
        __import__('os').fsync(handle.fileno())


def process(command, name, timeout):
    result = _run(command, ROOT.parent, timeout)
    for stream in ('stdout', 'stderr'):
        (RUNS/(name+'.'+stream+'.log')).write_text(result[stream], encoding='utf-8')
    write_new(RUNS/(name+'-process.json'), {k: v for k, v in result.items() if k not in ('stdout', 'stderr')})
    return result['returncode'] == 0 and not result['timed_out'] and not result['uncertain']


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
        raise ValueError('Stale proposal approval')
    for key in ('purpose', 'protocol_file_sha256', 'implementation_manifest_digest', 'descriptor_digests',
                'run_order', 'attempt_limit', 'historical_attempts', 'proposed_cumulative_attempt_ceiling',
                'local_additional_limit', 'local_proposed_cumulative_ceiling', 'reprocessing_limit',
                'online_product_calls', 'retry_slots', 'reuse_input_digest'):
        if approval.get(key) != proposal[key]:
            raise ValueError('Approval binding mismatch: '+key)
    frozen = read(REPAIR/'implementation-manifest.json')
    reuse = read(REPAIR/'reuse-input-manifest.json')
    if digest(frozen) != proposal['implementation_manifest_digest'] or digest(reuse) != proposal['reuse_input_digest']:
        raise ValueError('Manifest drift')
    require_dependency_hashes(frozen)
    require_dependency_hashes(reuse)
    require_dependency_hashes(read(REPAIR/'historical-manifest.json'))
    if read(PRIOR/'batch-result.json')['cumulative_attempts'] != 4:
        raise ValueError('Unexpected cumulative history')
    ledger = Ledger(RUNS)
    ledger.prepare(historical_attempts=4)
    write_new(RUNS/'authorization-record.json', approval)
    summary = {'status': 'RUNNING', 'local_cases': [], 'local_prior_consumed': 13,
               'reprocessing_attempts': 0, 'B01_numeric': 'UNVERIFIED', 'completed': [],
               'not_run': list(proposal['run_order']), 'online_calls': 0, 'physical_status': 'UNVERIFIED'}
    started = time.monotonic()
    try:
        for index, name in enumerate(CHECKS, start=1):
            write_new(RUNS/(name+'-reservation.json'), {'status': 'RESERVED', 'local_cumulative_slot': 13+index})
            item = {'case': name, 'status': 'RESERVED'}
            summary['local_cases'].append(item)
            command = [sys.executable, '-B', '-m', 'unittest', '-v',
                       'week12.tests.test_component_physics_postprocess.PostprocessChecks.test_'+name]
            passed = process(command, name, 10)
            item['status'] = 'PASS' if passed else 'FAIL'
            if not passed:
                summary['status'] = 'STOPPED_LOCAL_FAILURE'
                return 1
        write_new(RUNS/'reprocessing-reservation.json', {'status': 'RESERVED', 'slot': 1,
                  'limit': 1, 'timeout_seconds': 30, 'reuse_input_digest': digest(reuse), 'Modelica_calls': 0})
        summary['reprocessing_attempts'] = 1
        output = RUNS/'B01-reprocessed'
        command = [sys.executable, '-B', str(ROOT/'scripts/s3_b_verify.py'), 'B01',
                   str(PRIOR/'B01-refined/result.csv'), '--base', str(PRIOR/'B01-base/result.csv'),
                   '--output-directory', str(output)]
        if not process(command, 'B01-reprocessing', 30):
            summary['status'] = 'STOPPED_REPROCESSING_FAILURE'
            return 1
        # Performance refactoring must preserve the already observed physical reports.
        equal = (read(output/'physical.json') == read(PRIOR/'B01-refined/physical.json') and
                 read(output/'base_physical.json') == read(PRIOR/'B01-base/physical.json'))
        write_new(RUNS/'B01-report-equivalence.json', {'exact_physical_report_equality': equal})
        numeric = read(output/'numerical.json')
        if not equal or numeric['status'] != 'PASS':
            summary['status'] = 'STOPPED_REPORT_OR_NUMERICAL_FAILURE'
            return 1
        summary['B01_numeric'] = 'PASS_WITH_PRESERVED_SIMULATION_CSVS'
        print('B01 reprocessing PASS; no new B01 simulation.', flush=True)
        for scenario in proposal['run_order']:
            ident, level = scenario.split('-')
            print('START '+scenario, flush=True)
            report = execute_attempt(ledger, ident, refined=level=='refined', approval=approval,
                                     frozen_sources=frozen, batch_started=started)
            item = {key: report.get(key) for key in
                    ('scenario', 'status', 'compile', 'simulation', 'physical', 'numeric', 'error')}
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
        summary['cumulative_attempts'] = 4+summary['new_reserved_attempts']
        summary['local_cumulative_consumed'] = 13+len(summary['local_cases'])
        write_new(RUNS/'batch-result.json', summary)


if __name__ == '__main__':
    raise SystemExit(main())
