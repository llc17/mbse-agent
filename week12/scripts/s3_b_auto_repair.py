"""Finite user-authorized B repair rounds; one cumulative append-only budget."""
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import time

from week12.src.component_physics.contract import ROOT, canonical, digest, require_dependency_hashes
from week12.src.component_physics.ledger import Ledger
from week12.src.component_physics.runner import execute_attempt
from week12.src.toolchain import _run

BASE = ROOT/'results/s3-b-auto-repair-20260923-01'
PRIOR = ROOT/'results/s3-b-connector-repair-20260923-01'
POLICY_DOCUMENT = ROOT/'任务规划/B-自动修复执行补充.md'
PACKAGE = ROOT/'models/components/V11PhysicalB1/package.mo'
HARNESS = ROOT/'tests/modelica_scalar_harness.py'
TEST = ROOT/'tests/test_component_physics_event_range.py'
ORDER = [f'B{i:02d}-{level}' for i in range(2, 7) for level in ('base', 'refined')]


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path, value):
    with path.open('xb') as handle:
        handle.write(canonical(value))
        handle.flush()
        __import__('os').fsync(handle.fileno())


def row(path):
    return {'path': str(path.resolve()), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'bytes': path.stat().st_size}


def initialize():
    path = BASE/'policy.json'
    if not path.exists():
        write(path, {'user_message': '修改为自动修复，这种小错误，不要每次都让我来继续',
                    'receipt_kind': 'user_conversation', 'authorization_scope': 'Bounded automatic repair of minor implementation/tool compatibility errors, without repeated confirmation',
                    'limits_set_by': 'Agent within the user-authorized scope; not numbers quoted from user',
                    'round_limit': 3, 'new_attempt_limit': 16, 'new_local_limit': 8,
                    'historical_attempts': 6, 'historical_local': 16, 'historical_simulations': 2,
                    'wall_seconds': 2700, 'online_product_calls': 0,
                    'document': row(POLICY_DOCUMENT), 'executor': row(Path(__file__))})
    policy = read(path)
    require_dependency_hashes([policy['document'], policy['executor']])
    return policy


def spent():
    return len(list(BASE.glob('scene-reservation-*.json'))), len(list(BASE.glob('local-reservation-*.json')))


def remaining_time(policy):
    stamp = BASE/'execution-start.json'
    if not stamp.exists():
        write(stamp, {'utc': datetime.now(timezone.utc).isoformat()})
    elapsed = (datetime.now(timezone.utc)-datetime.fromisoformat(read(stamp)['utc'])).total_seconds()
    return policy['wall_seconds']-elapsed


def seal(number, tests, reason):
    policy = initialize()
    directory = BASE/f'round-{number:02d}'
    if number < 1 or number > policy['round_limit'] or not reason or not tests:
        raise ValueError('Missing concrete repair review or round outside limit')
    if number != len(list(BASE.glob('round-[0-9][0-9]')))+1:
        raise ValueError('Rounds must be sequential and never overwritten')
    if number > 1 and not (BASE/f'round-{number-1:02d}'/'batch-result.json').exists():
        raise ValueError('Previous round incomplete')
    previous = read(PRIOR/'implementation-manifest.json')
    changed = {PACKAGE.resolve(), HARNESS.resolve()}
    unchanged = [item for item in previous if Path(item['path']).resolve() not in changed]
    require_dependency_hashes(unchanged)
    for path in (PACKAGE, HARNESS):
        original = next(item for item in previous if Path(item['path']).resolve() == path.resolve())
        if row(BASE/'before'/path.name)['sha256'] != original['sha256']:
            raise ValueError('Backup is not the previously executed source')
    require_dependency_hashes(read(PRIOR/'historical-manifest.json'))
    reuse = read(PRIOR/'reuse-input-manifest.json')
    require_dependency_hashes(reuse)
    old = (BASE/'before/package.mo').read_text(encoding='utf-8')
    new = PACKAGE.read_text(encoding='utf-8')
    if number > 1:
        last_manifest = read(BASE/f'round-{number-1:02d}'/'source-manifest.json')
        previous_patch = {item['path']: item['sha256'] for item in last_manifest
                          if Path(item['path']).resolve() in {PACKAGE.resolve(), HARNESS.resolve(), TEST.resolve()}}
        if all(previous_patch.get(str(path.resolve())) == row(path)['sha256'] for path in (PACKAGE, HARNESS, TEST)):
            raise ValueError('No-change automatic retry is forbidden')
    strip_contact = lambda value: re.sub(r'  model PolylineWheelContact\b.*?  end PolylineWheelContact;', '', value, flags=re.S)
    if strip_contact(old) != strip_contact(new):
        raise ValueError('Repair changed more than contact algorithm; wider review required')
    # Declarations, parameters, equations and assertions must remain byte-equivalent.
    def outside_algorithm(value):
        contact = re.search(r'  model PolylineWheelContact\b.*?  end PolylineWheelContact;', value, re.S).group()
        return re.sub(r'  algorithm\b.*?  equation\b', '  algorithm\n  equation', contact, flags=re.S)
    if outside_algorithm(old) != outside_algorithm(new):
        raise ValueError('Contact declarations/equations/parameters changed')
    for path in (HARNESS, TEST, Path(__file__)):
        ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    if any(not test.startswith('week12.tests.test_component_physics_event_range.') for test in tests):
        raise ValueError('Unreviewed local test scope')
    attempts, locals_used = spent()
    if locals_used+len(tests) > policy['new_local_limit'] or attempts >= policy['new_attempt_limit']:
        raise ValueError('Budget exhausted')
    if (BASE/'execution-start.json').exists() and remaining_time(policy) <= 0:
        raise ValueError('Repair package wall limit exhausted')
    frozen = unchanged+[row(p) for p in (PACKAGE, HARNESS, TEST, Path(__file__), POLICY_DOCUMENT, BASE/'policy.json')]
    approval = read(PRIOR/'authorization-record.json')
    approval.update(user_message=policy['user_message'], authorization_context='Delegated minor-repair execution under this user policy; agent reviewed this specific patch, not a new per-patch user approval.',
                    implementation_manifest_digest=digest(frozen), historical_attempts=6+attempts,
                    attempt_limit=min(10, policy['new_attempt_limit']-attempts),
                    proposed_cumulative_attempt_ceiling=22, local_proposed_cumulative_ceiling=24,
                    run_order=ORDER, execution_directory=str(directory/'runs'),
                    automatic_repair_policy_digest=digest(policy), repair_reason=reason,
                    status='AUTHORIZED_UNDER_USER_AUTOMATIC_REPAIR_POLICY')
    approval.pop('repair_proposal_digest', None)
    directory.mkdir(exist_ok=False)
    write(directory/'source-manifest.json', frozen)
    write(directory/'reuse-manifest.json', reuse)
    write(directory/'authorization-record.json', approval)
    write(directory/'plan.json', {'tests': tests, 'reason': reason, 'policy_digest': digest(policy),
                                  'B01_dependency_closure_unchanged': True,
                                  'run_order': ORDER, 'attempt_limit': approval['attempt_limit']})
    print(f'Sealed round {number}; existing scene/local usage {attempts}/{locals_used}; no execution.')


def run(number):
    policy = initialize()
    directory = BASE/f'round-{number:02d}'
    plan = read(directory/'plan.json')
    frozen = read(directory/'source-manifest.json')
    approval = read(directory/'authorization-record.json')
    if plan['policy_digest'] != digest(policy) or approval['implementation_manifest_digest'] != digest(frozen):
        raise ValueError('Stale policy/source binding')
    require_dependency_hashes(frozen)
    require_dependency_hashes(read(directory/'reuse-manifest.json'))
    attempts, local_used = spent()
    if approval['historical_attempts'] != 6+attempts:
        raise ValueError('Cumulative ledger changed after seal')
    # Process singleton prevents concurrent callers spending the same budget.
    with (BASE/'active.lock').open('x', encoding='utf-8') as handle:
        handle.write(str(number))
    result = {'status': 'RUNNING', 'round': number, 'local': [], 'completed': [], 'not_run': ORDER.copy(), 'online_calls': 0}
    try:
        if (directory/'batch-result.json').exists():
            raise ValueError('Completed round immutable')
        remaining = remaining_time(policy)
        if remaining <= 0:
            raise TimeoutError('Automatic repair time budget exhausted')
        batch_started = time.monotonic()-(policy['wall_seconds']-remaining)
        ledger = Ledger(directory/'runs')
        ledger.prepare(historical_attempts=6+attempts)
        for test in plan['tests']:
            attempts, local_used = spent()
            if local_used >= policy['new_local_limit'] or remaining_time(policy) <= 0:
                result['status'] = 'STOPPED_BUDGET'
                return 1
            write(BASE/f'local-reservation-{local_used+1:02d}.json', {'round': number, 'test': test, 'cumulative_slot': 17+local_used})
            process = _run([sys.executable, '-B', '-m', 'unittest', '-v', test], ROOT.parent, min(10, remaining_time(policy)))
            passed = process['returncode'] == 0 and not process['timed_out'] and not process['uncertain']
            write(directory/f'local-{local_used+1:02d}.json', {'test': test, 'status': 'PASS' if passed else 'FAIL', 'process': process})
            result['local'].append({'test': test, 'status': 'PASS' if passed else 'FAIL'})
            print('LOCAL '+test+' '+('PASS' if passed else 'FAIL'), flush=True)
            if not passed:
                result['status'] = 'STOPPED_LOCAL_FAILURE_FOR_REPAIR'
                return 1
        for scenario in ORDER:
            attempts, local_used = spent()
            if attempts >= policy['new_attempt_limit'] or remaining_time(policy) <= 0:
                result['status'] = 'STOPPED_BUDGET'
                return 1
            write(BASE/f'scene-reservation-{attempts+1:02d}.json', {'round': number, 'scenario': scenario, 'cumulative_slot': 7+attempts})
            ident, level = scenario.split('-')
            print('START '+scenario, flush=True)
            report = execute_attempt(ledger, ident, refined=level=='refined', approval=approval, frozen_sources=frozen, batch_started=batch_started)
            item = {k: report.get(k) for k in ('scenario', 'status', 'model_check', 'compile', 'simulation', 'physical', 'numeric', 'error')}
            result['completed'].append(item)
            result['not_run'].remove(scenario)
            print(json.dumps(item), flush=True)
            if report['status'] != 'PASS':
                result['status'] = 'PAUSED_FOR_DIAGNOSIS_NO_BLIND_RETRY'
                return 1
        result['status'] = 'PASS_FROZEN_B02_B06_FIXTURES'
        return 0
    except BaseException as exc:
        result.update(status='STOPPED_BOUNDARY_OR_INTERRUPT', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        attempts, local_used = spent()
        result.update(new_attempts_total=attempts, cumulative_attempts=6+attempts,
                      new_local_total=local_used, cumulative_local=16+local_used)
        destination = directory/'batch-result.json'
        if not destination.exists():
            write(destination, result)
        (BASE/'active.lock').unlink()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['seal', 'run'])
    parser.add_argument('--round', type=int, required=True)
    parser.add_argument('--tests', nargs='+')
    parser.add_argument('--reason')
    args = parser.parse_args()
    if args.action == 'seal':
        seal(args.round, args.tests, args.reason)
        return 0
    return run(args.round)


if __name__ == '__main__':
    raise SystemExit(main())
