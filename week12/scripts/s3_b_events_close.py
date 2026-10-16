"""Collect existing B evidence after bounded repair; never rerun verification."""
import hashlib
import json
from pathlib import Path

from week12.src.component_physics.contract import ROOT, canonical, require_dependency_hashes


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def row(path):
    sha = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024*1024), b''):
            sha.update(block)
    return {'path': str(path), 'sha256': sha.hexdigest(), 'bytes': path.stat().st_size}


def main():
    root = ROOT/'results/s3-b-auto-repair-20260923-02'
    rounds = sorted(root.glob('round-[0-9][0-9]'))
    last = rounds[-1]
    batch = read(last/'batch-result.json')
    if batch['status'] != 'PASS_FROZEN_B02_B06_FIXTURES':
        raise ValueError('Latest bounded batch is not accepted; B remains incomplete')
    require_dependency_hashes(read(last/'source-manifest.json'))
    require_dependency_hashes(read(last/'reuse-manifest.json'))
    evidence = []
    scenarios = []
    b01 = ROOT/'results/s3-b-postprocess-validation-20260923-01/B01-reprocessed'
    paths = {'B01': [b01/'base_physical.json', b01/'physical.json', b01/'numerical.json']}
    for number in range(2, 7):
        ident = f'B{number:02d}'
        base, refined = last/'runs'/(ident+'-base'), last/'runs'/(ident+'-refined')
        for directory in (base, refined):
            report = read(directory/'result.json')
            if any(report[field] != 'PASS' for field in ('status', 'compile', 'event_codegen', 'simulation', 'physical')):
                raise ValueError('Nonaccepted run: '+str(directory))
            evidence += [row(directory/name) for name in ('result.json', 'result.csv', 'descriptor.json', 'V11PhysicalB1.mo', 'event-codegen.json')]
        paths[ident] = [base/'physical.json', refined/'physical.json', refined/'numerical.json']
    for ident, reports in paths.items():
        values = [read(path) for path in reports]
        if any(value['status'] != 'PASS' for value in values):
            raise ValueError('Physics/numeric failure '+ident)
        if any(not check['pass'] for value in values for check in value['checks']):
            raise ValueError('A failed check contradicts report status '+ident)
        scenarios.append({'scenario': ident, 'base_physical': 'PASS', 'refined_physical': 'PASS',
                          'numerical': 'PASS', 'evidence': [str(path) for path in reports]})
        evidence += [row(path) for path in reports]
    new_simulations = 0
    local_count = 0
    for directory in rounds:
        info = read(directory/'batch-result.json')
        local_count += len(info['local'])
        evidence.append(row(directory/'batch-result.json'))
        for ledger in (directory/'runs').glob('attempt-[0-9][0-9].json'):
            record = read(ledger)
            if record['simulation_started'] is None:
                raise ValueError('Uncertain launch remains')
            new_simulations += int(record['simulation_started'])
            evidence.append(row(ledger))
    attempts = len(list(root.glob('scene-reservation-*.json')))
    if local_count != len(list(root.glob('local-reservation-*.json'))):
        raise ValueError('Local budget/result mismatch')
    summary = {'status': 'PASS_S3_B_FROZEN_COMPONENT_FIXTURES', 'S3_status': 'INCOMPLETE_C_D_E_NOT_ENTERED',
               'scenarios': scenarios, 'B01_reused_unchanged_dependency_closure': True,
               'automatic_repair_rounds': len(rounds), 'budget': {
                   'new_scene_attempts': attempts, 'new_scene_limit': 30, 'cumulative_scene_attempts': 16+attempts,
                   'new_local': local_count, 'new_local_limit': 12, 'cumulative_local': 21+local_count,
                   'new_simulations': new_simulations, 'cumulative_simulations_started': 12+new_simulations, 'cumulative_simulations_completed': 11+sum(int(read(d/'runs'/s['scenario']/'result.json').get('simulation_process',{}).get('returncode') == 0) for d in rounds for s in read(d/'batch-result.json')['completed']),
                   'CSV_reprocessings': 1, 'tool_probes': 2, 'online_product_calls': 0},
               'scope': 'Six fixed development fixtures based on user-model adaptation; not whole-car integration or independent AI design',
               'limitations': ['A-to-physical generation and product CLI/MCP integration not delivered',
                               'No full vehicle/load/20-step/ability-scan validation',
                               'No C-E, A/R7 rerun or V12 work',
                               'Prior failed/timeout runs retained, not relabeled'],
               'source_manifest': str(last/'source-manifest.json')}
    for name, value in [('accepted-evidence-manifest.json', evidence), ('final-result.json', summary)]:
        with (root/name).open('xb') as handle:
            handle.write(canonical(value))
    print(json.dumps({'status': summary['status'], 'budget': summary['budget']}))


if __name__ == '__main__':
    main()
