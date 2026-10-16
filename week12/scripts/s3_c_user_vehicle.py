"""Prepare the single user-car completion for review. Never authorize execution."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

from week12.src.component_physics.contract import ROOT
from week12.src.integration.user_vehicle_proposal import proposal, validate, verification_contract
from week12.src.integration.review import review_identity
from week12.scripts.s3_c_user_baseline import save, protected_files

OUT = ROOT/'results/s3-c-user-vehicle-review-20260924-01'
TESTS = ['test_inertial_and_geometric_preflight',
         'test_changed_physics_geometry_or_test_receipt_rejected',
         'test_serial_contract_and_budget_cover_whole_affected_round']


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--round',type=int,default=1)
    no=parser.parse_args().round
    if no not in (1,2,3): raise ValueError('Finite round limit')
    if no==1:
        OUT.mkdir(exist_ok=False)
        save(OUT/'policy.json', {'scope':'one user-car completion draft, not execution approval',
             'prior_C_local_attempts':24,'new_local_limit':9,'round_limit':3,
             'local_timeout_s':10,'total_local_execution_s':120,
             'retired_mapping_unused_slots':6,'Modelica':0,'SysML':0,'online':0})
        save(OUT/'protected-before.json',protected_files())
    if no>1:
        prior=json.loads((OUT/f'round-{no-1:02d}'/'result.json').read_text(encoding='utf-8'))
        if prior['status']!='PAUSED_LOCAL_FAILURE' and not (OUT/f'round-{no-1:02d}'/'review-findings.json').exists():
            raise ValueError('Repeat requires failure or concrete review finding')
    used=len(list(OUT.glob('reservation-*.json')))
    if used+3>9: raise ValueError('No complete round left')
    elapsed=sum(json.loads(p.read_text(encoding='utf-8'))['seconds'] for p in OUT.glob('round-*/result.json'))
    if elapsed+30>120: raise ValueError('Insufficient time reserve')
    directory=OUT/f'round-{no:02d}'; directory.mkdir(exist_ok=False)
    paths=['src/integration/user_vehicle_proposal.py','tests/test_user_vehicle_proposal.py',
           'scripts/s3_c_user_vehicle.py','src/integration/user_baseline.py','src/integration/proposals.py',
           'src/integration/proposal_checks.py','src/integration/preview.py','src/integration/review.py']
    save(directory/'source-manifest.json',{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in paths})
    for path in paths:
        with (directory/Path(path).name).open('xb') as stream: stream.write((ROOT/path).read_bytes())
    start=time.monotonic(); results=[]
    for n,name in enumerate(TESTS,1):
        save(OUT/f'reservation-{used+n:02d}.json',{'round':no,'test':name})
        target='week12.tests.test_user_vehicle_proposal.UserVehicleChecks.'+name
        try:
            process=subprocess.run([sys.executable,'-B','-m','unittest',target],cwd=ROOT.parent,capture_output=True,timeout=10)
            status='PASS' if process.returncode==0 else 'FAIL'; stdout,stderr=process.stdout,process.stderr
        except subprocess.TimeoutExpired as exc:
            status,stdout,stderr='TIMEOUT',exc.stdout or b'',exc.stderr or b''
        (directory/f'{n}.stdout').write_bytes(stdout); (directory/f'{n}.stderr').write_bytes(stderr)
        results.append({'test':name,'status':status}); print(name,status,flush=True)
        if status!='PASS': break
    status='PASS_REVIEW_PREPARATION_NOT_APPROVED' if len(results)==3 and all(r['status']=='PASS' for r in results) else 'PAUSED_LOCAL_FAILURE'
    unchanged=protected_files()==json.loads((OUT/'protected-before.json').read_text(encoding='utf-8'))
    if not unchanged: status='PROTECTED_INPUT_DRIFT_STOP'
    if status.startswith('PASS'):
        b=proposal(); contract=verification_contract()
        save(directory/'proposal.json',b); save(directory/'static.json',validate(b))
        save(directory/'verification-contract.json',contract)
        # Identity here is evidence at this point. Final review packet is frozen
        # only AFTER main-window audit and the protocol update.
        save(directory/'preliminary-identity.json',review_identity([b],contract))
    save(directory/'result.json',{'status':status,'checks':results,'seconds':time.monotonic()-start,
         'new_attempts':used+len(results),'cumulative_C_attempts':24+used+len(results),
         'protected_inputs_unchanged':unchanged,'Modelica':0,'SysML':0,'online':0})


if __name__=='__main__': main()
