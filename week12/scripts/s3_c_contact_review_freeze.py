"""Freeze a reviewable proposal only; never create approval or run tests/tools."""
import ast
from pathlib import Path
from week12.src.integration.execution import ROOT, read, save, sha, approved_inputs
from week12.src.integration.timing_amendment import inputs
from week12.src.composition.schema import digest


def main():
    directory=ROOT/'results/s3-c-evidence-review-20260926-01'
    if (directory/'execution-review-packet.json').exists():
        raise FileExistsError('Review already frozen; preserve it before any revision')
    inputs()
    previous,_,_=approved_inputs()
    proposal=read(directory/'contact-repair-proposal.json')
    python_files=[
        'src/integration/contact_repair_execution.py', 'src/integration/contact_probe_verify.py',
        'scripts/s3_c_contact_repair.py', 'scripts/s3_c_contact_probe_verify.py',
        'tests/test_c_contact_repair.py', 'tests/test_c_contact_probe.py',
        'scripts/s3_c_contact_candidate_prepare.py', 'scripts/s3_c_contact_review_freeze.py']
    syntax=[]
    for relative in python_files:
        tree=ast.parse((ROOT/relative).read_text(encoding='utf-8'),filename=relative)
        syntax.append({'path':relative,'status':'AST_PARSED_ONLY',
            'test_methods':sum(isinstance(n,ast.FunctionDef) and n.name.startswith('test_') for n in ast.walk(tree))})
    reused=[
        'src/integration/execution.py','src/integration/review.py','src/integration/timing_amendment.py',
        'src/toolchain.py','src/component_physics/contract.py','src/component_physics/runner.py',
        'src/component_physics/verify.py','src/component_physics/ledger.py',
        'src/component_physics/event_codegen.py','scripts/s3_b_verify.py','src/composition/schema.py']
    code=sorted(set(python_files+reused+['models/components/V11ContactC2/package.mo']))
    manifest=[{'path':str(ROOT/p),'sha256':sha(ROOT/p)} for p in code]
    fixed={name:value for name,value in previous['protected_inputs'].items()}
    for row in previous['files']: fixed[row['path']]=row['sha256']
    named=[
        'results/s3-c-user-vehicle-review-20260924-01/review-packet.json',
        'results/s3-c-user-vehicle-execution-20260924-01/final-result.json',
        'results/s3-c-user-vehicle-execution-20260924-01/timing-amendment-authorization.json',
        'results/s3-c-user-vehicle-execution-20260924-01/timing-amendment-draft/manifest.json',
        'results/s3-b-auto-repair-20260923-02/round-01/source-manifest.json']
    named+=reused
    named += [str((directory/p).relative_to(ROOT)) for p in (
        'contact-repair-proposal.json','candidate-provenance.json','V11ContactC2Probes.mo',
        'mechanics-review.json','source-evidence.json','read-only-policy.json')]
    for relative in named: fixed[relative]=sha(ROOT/relative)
    for row in read(directory/'source-evidence.json')['files']:
        if sha(ROOT/row['path'])!=row['sha256']: raise ValueError('REVIEW_SOURCE_DRIFT')
        fixed[row['path']]=row['sha256']
    for name,value in fixed.items():
        if sha(ROOT/name)!=value: raise ValueError('FIXED_INPUT_DRIFT: '+name)
    if sha(ROOT/'models/components/V11ContactC2/package.mo')!=proposal['candidate_sha256']:
        raise ValueError('INITIAL_CANDIDATE_DRIFT')
    if sha(directory/'V11ContactC2Probes.mo')!=proposal['fixture_sha256']:
        raise ValueError('FIXTURE_DRIFT')
    packet={
        'scope':'v11_s3_c_contact_repair_development',
        'status':'SOURCE_READY_PENDING_NEW_FINITE_EXECUTION_BUDGET',
        'proposal_digest':proposal['proposal_digest'],
        'fixed_inputs':[{'path':p,'sha256':fixed[p]} for p in sorted(fixed)],
        'implementation_manifest':manifest,
        'budget':proposal['budget_proposal'],
        'readiness':'All four pending implementation items in initial proposal now have source; no tests or Modelica execution yet',
        'probe_gate_details':{
            'raw_csv':'16 named numeric columns, every cell finite; no extra cells; all nominal outputs and all event rows retained',
            'time_bounds_absolute_s':1e-9,'nominal_time_grid_absolute_s':2e-9,
            'velocity_absolute_m_s':5e-7,'omega_absolute_rad_s':5e-7,'slip_absolute_m_s':5e-7,
            'elastic_energy_absolute_J':2e-4,'loss_power_absolute_W':.04,
            'energy_loss_relative':1e-5,'event_vs_prescribed_absolute_s':2e-6,
            'derived_energy_loss_note':'Conservative Fn*d(delta) and dF*abs(v) floors for these new local fixtures only; original B/C tolerances unchanged'},
        'implementation_repair_boundary':'At most two repairs after first round, same full 18-scene/6-local set; preserve physical inputs/force and energy laws/acceptance criteria; explicitly record diagnosis and rebound manifest; shared helpers are immutable fixed inputs',
        'limitations':['Generated Boolean ownership guard awaits real compiler output; unrecognized lowering stops before simulation',
                      'New probe success and isolated B regression success do not authorize vehicle rebinding or establish climb success',
                      'No car/envelope/torque/mass changes; no whole-car execution, D/E, scans or V12'],
        'prior_batch':'Original 3 rounds/17 scenes/24 local/3 SysML remain closed; 37 unused scene slots not carried over',
        'approval':'Must bind a subsequent real user message to this specification; preparation is not approval'}
    packet['specification_digest']=digest(packet)
    save(directory/'execution-review-packet.json',packet)
    for row in manifest:
        source=Path(row['path']); destination=directory/'source-snapshot'/source.relative_to(ROOT)
        destination.parent.mkdir(parents=True,exist_ok=True)
        with destination.open('xb') as stream: stream.write(source.read_bytes())
    save(directory/'implementation-readiness.json',{
        'status':'SOURCE_REVIEWED_NOT_EXECUTED','specification_digest':packet['specification_digest'],
        'syntax_only':syntax,'source_files':len(manifest),'fixed_inputs':len(fixed),
        'new_local_acceptance_runs':0,'Modelica_runs':0,'SysML_runs':0,'product_online_calls':0,
        'new_execution_directory_exists':(ROOT/'results/s3-c-contact-repair-execution-20260926-01').exists(),
        'protected_inputs':'UNCHANGED_BY_HASH_READ_ONLY_NOT_A_NEW_ACCEPTANCE',
        'subagent':{'task':'c_contact_review_20260926','requested_model':'GPT-5.6 Sol','requested_reasoning':'High',
            'client_independent_model_identity':'unavailable','delivery':'3 source files; AST only, no tests',
            'main_review':'Inspected source/API/fixture geometry/force law/CLI and added extra-cell rejection; no formal PASS claim'}})
    print(packet['specification_digest'])
    print('SOURCE_FROZEN_NO_TESTS_NO_SIMULATION_NO_APPROVAL')


if __name__=='__main__': main()
