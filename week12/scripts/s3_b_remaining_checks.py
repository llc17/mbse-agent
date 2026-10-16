"""Consume only L05/L06/L07/L08/L10 from the original 12-case local budget."""
import hashlib
import json
from pathlib import Path
import sys
import time

from week12.src.component_physics.contract import ROOT, canonical
from week12.src.toolchain import _run

NAMES = ['L05_convex_unique_feature', 'L06_concave_and_endpoint',
         'L07_no_contact_no_tension_dissipation', 'L08_drive_brake_energy',
         'L10_unsupported_and_inside_terrain']
DIRECTORY = ROOT/'results/s3-b-local-remaining-20260923-01'


def save(state):
    temporary = DIRECTORY/'result.pending'
    with temporary.open('xb') as handle:
        handle.write(canonical(state))
        handle.flush()
        __import__('os').fsync(handle.fileno())
    temporary.replace(DIRECTORY/'result.json')


def main():
    prior_path = ROOT/'results/s3-b-local-20260922-01/result.json'
    prior = json.loads(prior_path.read_text(encoding='utf-8'))
    if prior['consumed'] != 7 or prior['status'] != 'PARTIAL_LOCAL_PASS':
        raise ValueError('Unexpected prior budget; do not execute')
    DIRECTORY.mkdir(exist_ok=False)
    state = {'kind': 'actual_source_scalar_checks_not_Modelica_simulation',
             'limit': 12, 'prior_consumed': 7, 'consumed': 7, 'cases': [],
             'prior_result_sha256': hashlib.sha256(prior_path.read_bytes()).hexdigest(),
             'modelica_attempts': 0, 'online_calls': 0, 'physical_status': 'UNVERIFIED'}
    files = [ROOT/'tests/modelica_scalar_harness.py', ROOT/'tests/test_component_physics_remaining.py',
             ROOT/'models/components/V11PhysicalB1/package.mo', Path(__file__)]
    (DIRECTORY/'source-manifest.json').write_bytes(canonical([
        {'path': str(path.resolve()), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()} for path in files]))
    started = time.monotonic()
    for name in NAMES:
        remaining_seconds = 120-prior['elapsed_seconds']-(time.monotonic()-started)
        if remaining_seconds <= 0:
            state['stop_reason'] = 'local_wall_budget'
            break
        item = {'case': name, 'status': 'RESERVED', 'slot': state['consumed']+1}
        state['cases'].append(item)
        state['consumed'] += 1
        save(state)
        command = [sys.executable, '-B', '-m', 'unittest', '-v',
                   'week12.tests.test_component_physics_remaining.BRemainingChecks.test_'+name]
        result = _run(command, ROOT.parent, remaining_seconds)
        for stream in ('stdout', 'stderr'):
            (DIRECTORY/(name+'.'+stream+'.log')).write_text(result[stream], encoding='utf-8')
        item['process'] = {k: v for k, v in result.items() if k not in ('stdout', 'stderr')}
        item['status'] = 'PASS' if result['returncode'] == 0 and not result['timed_out'] and not result['uncertain'] else 'FAIL'
        save(state)
        if item['status'] != 'PASS':
            state['stop_reason'] = 'first_failure_no_retry'
            break
    passed = len(state['cases']) == 5 and all(item['status']=='PASS' for item in state['cases'])
    state.update(status='LOCAL_PASS' if passed else 'STOPPED',
                 elapsed_seconds=time.monotonic()-started,
                 total_local_passed=7+sum(item['status']=='PASS' for item in state['cases']))
    state['not_run'] = NAMES[len(state['cases']):]
    save(state)
    print(json.dumps(state, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
