"""Bounded C preview checks and non-executable artifacts. Never starts OMC."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

from week12.src.component_physics.contract import ROOT, canonical
from week12.src.composition.examples import make_example
from week12.src.integration.preview import binding_preview

OUT = ROOT/'results/s3-c-preparation-20260923-01'
TESTS = ['test_mass_and_offset_accounting', 'test_bearings_and_support_are_not_duplicated',
         'test_tree_topology_and_determinism', 'test_shared_supply_and_no_approval',
         'test_changed_design_cannot_reuse_binding', 'test_source_drift_rejected']


def save(path, data):
    with path.open('xb') as f:
        f.write(canonical(data))


def main():
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--round', type=int, default=1)
    round_no=parser.parse_args().round
    if round_no not in (1,2,3):
        raise ValueError('Round budget exhausted')
    if round_no==1:
        OUT.mkdir(exist_ok=False)
        save(OUT/'policy.json', {'user_instruction': '按照这个计划继续', 'scope': 'C preparation only',
         'local_initial': 6, 'local_limit_total': 18, 'repair_round_limit': 3, 'timeout_each_seconds': 10,
         'modelica_compile_or_simulation_limit': 0, 'product_online_limit': 0,
         'confirmation': 'No concrete vehicle approval yet; do not emit executable car models',
         'history': 'B closed independently:26 attempts,25 local; no reuse of remaining B budget'})
    policy=json.loads((OUT/'policy.json').read_text(encoding='utf-8'))
    if round_no>policy['repair_round_limit']:
        raise ValueError('Round budget exhausted')
    directory=OUT if round_no==1 else OUT/f'round-{round_no:02d}'
    if round_no>1:
        previous=OUT if round_no==2 else OUT/f'round-{round_no-1:02d}'
        if json.loads((previous/'result.json').read_text(encoding='utf-8'))['status']!='PAUSED_LOCAL_FAILURE':
            raise ValueError('Only a failed previous round may be resumed')
        directory.mkdir(exist_ok=False)
    used=len(list(OUT.glob('local-reservation-*.json')))
    if used+len(TESTS)>policy['local_limit_total']:
        raise ValueError('Complete repair set exceeds remaining budget')
    source_files=['src/integration/preview.py','tests/test_integration_preview.py','scripts/s3_c_prepare.py']
    save(directory/'source-manifest.json', [{'path': 'week12/'+p,'sha256':hashlib.sha256((ROOT/p).read_bytes()).hexdigest()} for p in source_files])
    rows=[]
    for n, name in enumerate(TESTS,1):
        target='week12.tests.test_integration_preview.BindingChecks.'+name
        save(OUT/f'local-reservation-{used+n:02d}.json', {'ordinal': used+n, 'test': target, 'round': round_no})
        start=time.monotonic()
        try:
            p=subprocess.run([sys.executable,'-B','-m','unittest',target],cwd=ROOT.parent,
                             capture_output=True, timeout=10)
            (directory/f'local-{n:02d}.stdout').write_bytes(p.stdout)
            (directory/f'local-{n:02d}.stderr').write_bytes(p.stderr)
            status='PASS' if p.returncode==0 else 'FAIL'
        except subprocess.TimeoutExpired as exc:
            (directory/f'local-{n:02d}.stderr').write_bytes(exc.stderr or b'')
            status='TIMEOUT'
        rows.append({'test': target, 'status': status, 'seconds': time.monotonic()-start})
        print(name, status, flush=True)
        if status!='PASS':
            save(directory/'result.json',{'status':'PAUSED_LOCAL_FAILURE','local':rows,'modelica_calls':0,'online_calls':0})
            return
    for topology in ('fixed_nose','rocker_bogie'):
        save(directory/(topology+'-synthetic-preview.json'), binding_preview(make_example(topology)))
    save(directory/'result.json', {'status':'PASS_NONEXECUTABLE_BINDING_PREVIEW', 'local':rows,
         'cumulative_local': used+len(rows),
         'modelica_calls':0,'online_calls':0,'vehicle_confirmation':'PENDING',
         'limits':'Synthetic A examples only; not user numeric baseline or whole car verification'})


if __name__=='__main__':
    main()
