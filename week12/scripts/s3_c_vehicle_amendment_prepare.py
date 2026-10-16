"""Freeze a concrete review packet and generated source, without execution."""
import ast
from copy import deepcopy
from pathlib import Path
from week12.src.integration import vehicle_amendment as a
from week12.src.integration.execution import read,save,sha,approved_inputs
from week12.src.composition.schema import digest


def differences(left,right,path=''):
    if isinstance(left,dict) and isinstance(right,dict):
        result=[]
        for key in sorted(set(left)|set(right)):
            name=path+'/'+key
            if key not in left: result.append({'path':name,'old':'<absent>','new':right[key]})
            elif key not in right: result.append({'path':name,'old':left[key],'new':'<absent>'})
            else: result.extend(differences(left[key],right[key],name))
        return result
    if left!=right: return [{'path':path,'old':left,'new':right}]
    return []


def main():
    bundle,contract=a.build_draft(); oldpacket,_,_=approved_inputs()
    _,old_bundle,old_contract=a.previous_inputs()
    new_sources=['src/integration/vehicle_amendment.py','src/integration/vehicle_amendment_execution.py',
        'scripts/s3_c_vehicle_amendment.py','scripts/s3_c_vehicle_amendment_prepare.py',
        'tests/test_c_vehicle_amendment.py','tests/test_c_vehicle_amendment_execution.py']
    syntax=[]
    for name in new_sources:
        tree=ast.parse((a.ROOT/name).read_text(encoding='utf-8'),filename=name)
        syntax.append({'path':name,'status':'AST_ONLY',
            'test_methods':sum(isinstance(n,ast.FunctionDef) and n.name.startswith('test_') for n in ast.walk(tree))})
    reused_sources=['src/integration/execution.py','src/integration/review.py','src/integration/timing_amendment.py',
        'src/integration/model_generator.py','src/integration/output_contract.py','src/integration/vehicle_verify.py',
        'src/integration/contact_repair_execution.py','src/composition/schema.py','src/toolchain.py',
        'src/component_physics/contract.py','src/component_physics/runner.py','src/component_physics/event_codegen.py',
        'src/component_physics/ledger.py','scripts/s3_c_verify.py','scripts/s3_c_execute.py']
    manifest=[{'path':str(a.ROOT/p),'sha256':sha(a.ROOT/p)} for p in sorted(new_sources+reused_sources)]
    fixed=dict(oldpacket['protected_inputs'])
    for row in oldpacket['files']: fixed[row['path']]=row['sha256']
    for p in reused_sources: fixed[p]=sha(a.ROOT/p)
    names=[a.CANDIDATE,a.B_SOURCE,a.CONTACT_EVIDENCE/'final-result.json',
        a.CONTACT_EVIDENCE/'closeout-manifest.json',a.CONTACT_EVIDENCE/'round-02/source-manifest.json',
        a.ROOT/'results/s3-b-auto-repair-20260923-02/round-01/source-manifest.json',
        a.ROOT/'results/s3-c-user-vehicle-execution-20260924-01/round-03/source-manifest.json',
        a.ROOT/'results/s3-c-user-vehicle-execution-20260924-01/round-03/verification-contract.json',
        a.ROOT/'results/s3-c-user-vehicle-execution-20260924-01/timing-amendment-authorization.json',
        a.ROOT/'results/s3-c-user-vehicle-execution-20260924-01/timing-amendment-draft/manifest.json',
        a.ROOT/'results/s3-c-user-vehicle-execution-20260924-01/timing-amendment-draft/proposal.json',
        a.ROOT/'results/s3-c-user-vehicle-execution-20260924-01/timing-amendment-draft/verification-contract.json']
    for p in names: fixed[str(p.relative_to(a.ROOT))]=sha(p)
    for reuse in contract['reused_probe_evidence']:
        for row in reuse['files']: fixed[row['path']]=row['sha256']
    for name,value in fixed.items():
        if sha(a.ROOT/name)!=value: raise ValueError('PROTECTED_INPUT_DRIFT')
    a.REVIEW.mkdir(exist_ok=False)
    save(a.REVIEW/'proposal.json',bundle); save(a.REVIEW/'verification-contract.json',contract)
    save(a.REVIEW/'input-differences.json',{'bundle':differences(old_bundle,bundle),
        'contract':differences(old_contract,contract),'previous_specification_digest':a.PREVIOUS_SPEC,
        'same_physical_and_numerical_acceptance_criteria':all(contract[k]==old_contract[k] for k in ('physical','numerical','numerical_comparison')),
        'dynamics_changes':'only the explicitly bound accepted contact library; housing has no reaction forces'})
    for name in ('proposal.json','verification-contract.json','input-differences.json'):
        fixed[str((a.REVIEW/name).relative_to(a.ROOT))]=sha(a.REVIEW/name)
    packet={'scope':'v11_s3_c_single_vehicle_c2_circular_housing_amendment',
        'status':'READY_FOR_SPECIFIC_USER_CONFIRMATION_SOURCE_ONLY',
        'previous_specification_digest':a.PREVIOUS_SPEC,'bundle_digest':digest(bundle),
        'proposal_digest':digest(bundle),'verification_digest':digest(contract),
        'fixed_inputs':[{'path':p,'sha256':fixed[p]} for p in sorted(fixed)],
        'implementation_manifest':manifest,'budget':contract['budget_proposal'],
        'changes':['accepted C2 via isolated B runtime copy','nose-drive AI square to axle-centred circleR50mm'],
        'unchanged':['85kg total including20kg payload','all axle poses and wheel radii','component masses/inertias','drive/control/supply ratings',
            '150x280mm stair task/full mission','settling/ramp timing','physical/numerical tolerances'],
        'confirmation_boundary':'no execution, no new physical approval inferred from prepare request; bind a subsequent actual user message',
        'stop':'first failure pauses; physical/task/numerical hard failure stops without tuning; only small implementation repairs within3 complete rounds',
        'scope_limits':['AI packaging assumption, not selected purchasable part','no 3D fit/thermal validation','no guarantee of ascent after contact correction',
            'no second topology, D/E, scans or V12','unused budgets never carried over']}
    packet['specification_digest']=digest(packet)
    save(a.REVIEW/'execution-review-packet.json',packet)
    artifacts=[]
    for case in a.CASE_ORDER:
        generated=a.generate(bundle,case); folder=a.REVIEW/'generated'/case; folder.mkdir(parents=True)
        for key,name in (('modelica','V11CGenerated.mo'),('sysml','vehicle.sysml')):
            (folder/name).write_text(generated[key],encoding='utf-8',newline='\n'); artifacts.append(folder/name)
        save(folder/'trace.json',generated['trace']); artifacts.append(folder/'trace.json')
    (a.REVIEW/'V11PhysicalB1-runtime.mo').write_text(a.isolated_b(),encoding='utf-8',newline='\n')
    artifacts.append(a.REVIEW/'V11PhysicalB1-runtime.mo')
    for row in manifest:
        src=Path(row['path']); dst=a.REVIEW/'source-snapshot'/src.relative_to(a.ROOT)
        dst.parent.mkdir(parents=True,exist_ok=True)
        with dst.open('xb') as stream: stream.write(src.read_bytes())
    save(a.REVIEW/'generated-artifacts.json',{'specification_digest':packet['specification_digest'],
        'status':'SOURCE_ONLY_NOT_COMPILED_OR_PARSED','files':[{'path':str(p.relative_to(a.ROOT)),'sha256':sha(p)} for p in artifacts]})
    save(a.REVIEW/'readiness.json',{'specification_digest':packet['specification_digest'],
        'new_sources_ast':syntax,'local_acceptance_runs':0,'Modelica_runs':0,'SysML_runs':0,'online_calls':0,
        'generated_vehicle_cases':7,'reused_old_probe_runs':4,'new_execution_directory_exists':a.OUT.exists(),
        'note':'8 preflight methods prepared but not executed; existing circle algorithm reused; new real physics pending approval'})
    print(packet['specification_digest']); print('SOURCE_ONLY_NO_APPROVAL_NO_EXECUTION')


if __name__=='__main__': main()
