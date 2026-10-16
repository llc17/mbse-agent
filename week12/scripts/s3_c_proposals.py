"""Prepare reviewable C proposals under a bounded local-only budget."""
import argparse
import hashlib
import json
import subprocess
import sys
import time
from week12.src.component_physics.contract import ROOT, canonical
from week12.src.integration.proposals import proposal, USER_FACTS
from week12.src.integration.proposal_checks import validate_proposal, verification_contract

OUT=ROOT/'results/s3-c-proposals-20260924-01'
TESTS=['test_user_facts_and_original_geometry','test_mass_power_clearance_and_passive_stability',
       'test_stale_or_inconsistent_proposal_rejected','test_cases_and_probe_independent_arithmetic']


def save(path,value):
    with path.open('xb') as stream:
        stream.write(canonical(value))


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--round',type=int,default=1)
    round_no=parser.parse_args().round
    if round_no not in (1,2,3):
        raise ValueError('Round budget exhausted')
    if round_no==1:
        OUT.mkdir(exist_ok=False)
        save(OUT/'policy.json',{'user_facts':USER_FACTS,'authorization':'C preparation continues; facts are not complete vehicle approval',
             'local_limit':12,'round_limit':3,'timeout_seconds':10,'prior_C_local_attempts':9,
             'cumulative_C_local_limit':21,'old_unused_local_slots_retired':9,
             'modelica_calls_limit':0,'product_online_calls_limit':0})
    policy=json.loads((OUT/'policy.json').read_text(encoding='utf-8'))
    used=len(list(OUT.glob('local-reservation-*.json')))
    if used+4>policy['local_limit']:
        raise ValueError('Insufficient budget for complete check set')
    directory=OUT/f'round-{round_no:02d}'
    if round_no>1:
        previous=json.loads((OUT/f'round-{round_no-1:02d}'/'result.json').read_text(encoding='utf-8'))
        if previous['status']!='PAUSED_LOCAL_FAILURE' and not (OUT/f'round-{round_no-1:02d}'/'review-findings.json').is_file():
            raise ValueError('No failed check or documented review finding to repair')
    directory.mkdir(exist_ok=False)
    sources=['src/integration/proposals.py','src/integration/proposal_checks.py','src/integration/preview.py','src/integration/review.py',
             'tests/test_integration_proposals.py','scripts/s3_c_proposals.py']
    save(directory/'source-manifest.json',[{'path':'week12/'+p,'sha256':hashlib.sha256((ROOT/p).read_bytes()).hexdigest()} for p in sources])
    results=[]
    for n,name in enumerate(TESTS,1):
        target='week12.tests.test_integration_proposals.ProposalChecks.'+name
        save(OUT/f'local-reservation-{used+n:02d}.json',{'ordinal':used+n,'test':target,'round':round_no})
        start=time.monotonic()
        try:
            p=subprocess.run([sys.executable,'-B','-m','unittest',target],cwd=ROOT.parent,capture_output=True,timeout=10)
            (directory/f'local-{n:02d}.stdout').write_bytes(p.stdout)
            (directory/f'local-{n:02d}.stderr').write_bytes(p.stderr)
            status='PASS' if p.returncode==0 else 'FAIL'
        except subprocess.TimeoutExpired as exc:
            (directory/f'local-{n:02d}.stderr').write_bytes(exc.stderr or b'')
            status='TIMEOUT'
        results.append({'test':target,'status':status,'seconds':time.monotonic()-start})
        print(name,status,flush=True)
        if status!='PASS':
            save(directory/'result.json',{'status':'PAUSED_LOCAL_FAILURE','local':results,'modelica_calls':0,'online_calls':0})
            return
    for name in ('fixed_nose','rocker_bogie'):
        b=proposal(name)
        save(directory/(name+'-proposal.json'),b)
        save(directory/(name+'-static.json'),validate_proposal(b))
    save(directory/'verification-contract.json',verification_contract())
    save(directory/'result.json',{'status':'PASS_STATIC_PROPOSALS_PENDING_CONFIRMATION','local':results,
         'new_local_attempts':used+4,'cumulative_C_local_attempts':9+used+4,'modelica_calls':0,'online_calls':0,
         'scope':'No executable generation, no runtime validation; all concrete design assumptions await user review'})


if __name__=='__main__':
    main()
