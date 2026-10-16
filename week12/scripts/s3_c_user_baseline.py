"""Bounded source-mapping checks. No model generation, simulation or approval."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

from week12.src.component_physics.contract import ROOT
from week12.src.integration.user_baseline import baseline_mapping, SOURCE, ENERGY_SOURCE

OUT = ROOT/'results/s3-c-user-baseline-20260924-01'
TESTS = ['test_source_mapping_does_not_double_mass_torque_or_nose_height',
         'test_restricted_extractor_rejects_unsafe_or_ambiguous_values',
         'test_serial_mission_has_both_flights_and_full_platform']
DOCS = ['V11-CURRENT-STATE.md', 'README.md', '任务规划/S3-启动.md',
        '任务规划/C-两结构集成与验证协议.md', 'WORKLOG.md']


def save(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def protected_files():
    files = [SOURCE, ENERGY_SOURCE, ROOT/'models/components/V11PhysicalB1/package.mo',
             ROOT/'任务规划/B-基础物理组件与验证协议.md',
             ROOT/'results/s3-b-auto-repair-20260923-02/final-result.json',
             ROOT/'results/s3-c-proposals-20260924-01/review-packet.json']
    files += sorted((ROOT/'src/composition').glob('*.py'))
    files += sorted((ROOT/'configs/composition').glob('*.json'))
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--round', type=int, default=1)
    args = parser.parse_args()
    if args.round not in (1,2,3):
        raise ValueError('Round limit exhausted')
    if args.round == 1:
        OUT.mkdir(exist_ok=False)
        save(OUT/'policy.json', {'scope': 'user source mapping and common mission geometry only',
             'prior_C_local_attempts': 21, 'new_limit': 9, 'rounds': 3,
             'per_check_timeout_s': 10, 'total_execution_timeout_s': 120,
             'modelica': 0, 'sysml': 0, 'online': 0,
             'old_unspent_slots_retired': True, 'physical_approval': False})
        backup = OUT/'before'
        backup.mkdir()
        for name in DOCS:
            dest = backup/name
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open('xb') as stream:
                stream.write((ROOT/name).read_bytes())
        save(OUT/'protected-before.json', protected_files())
    if args.round > 1:
        previous = json.loads((OUT/f'round-{args.round-1:02d}'/'result.json').read_text(encoding='utf-8'))
        if previous['status'] != 'PAUSED_MAPPING_FAILURE' and not (OUT/f'round-{args.round-1:02d}'/'review-findings.json').exists():
            raise ValueError('No failure or review finding justifies repeat')
    used = len(list(OUT.glob('reservation-*.json')))
    if used+len(TESTS)>9:
        raise ValueError('Complete check set exceeds budget')
    elapsed = sum(json.loads(p.read_text(encoding='utf-8'))['seconds']
                  for p in OUT.glob('round-*/result.json'))
    if elapsed+len(TESTS)*10>120:
        raise ValueError('Insufficient time for complete round')
    directory = OUT/f'round-{args.round:02d}'
    directory.mkdir(exist_ok=False)
    sources = ['src/integration/user_baseline.py', 'scripts/s3_c_user_baseline.py',
               'tests/test_user_baseline_mapping.py']
    save(directory/'sources.json', {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sources})
    for name in sources:
        dest = directory/Path(name).name
        with dest.open('xb') as stream: stream.write((ROOT/name).read_bytes())
    start = time.monotonic()
    results = []
    for index, name in enumerate(TESTS, 1):
        save(OUT/f'reservation-{used+index:02d}.json', {'round': args.round, 'test': name})
        target = 'week12.tests.test_user_baseline_mapping.UserBaselineChecks.'+name
        try:
            proc = subprocess.run([sys.executable, '-B', '-m', 'unittest', target],
                                  cwd=ROOT.parent, capture_output=True, timeout=10)
            status = 'PASS' if proc.returncode==0 else 'FAIL'
            stdout, stderr = proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            status, stdout, stderr = 'TIMEOUT', exc.stdout or b'', exc.stderr or b''
        (directory/f'{index}.stdout').write_bytes(stdout)
        (directory/f'{index}.stderr').write_bytes(stderr)
        results.append({'name': name, 'status': status})
        print(name, status, flush=True)
        if status != 'PASS': break
    status = 'PASS_MAPPING_NOT_PHYSICAL_APPROVAL' if len(results)==3 and all(r['status']=='PASS' for r in results) else 'PAUSED_MAPPING_FAILURE'
    if status.startswith('PASS'):
        save(directory/'mapping.json', baseline_mapping())
    unchanged = protected_files() == json.loads((OUT/'protected-before.json').read_text(encoding='utf-8'))
    if not unchanged:
        status = 'PROTECTED_INPUT_DRIFT_STOP'
    save(directory/'result.json', {'status': status, 'checks': results, 'seconds': time.monotonic()-start,
         'protected_inputs_unchanged': unchanged, 'new_attempts': used+len(results),
         'cumulative_C_attempts': 21+used+len(results), 'Modelica': 0, 'SysML': 0, 'online': 0})


if __name__ == '__main__':
    main()
