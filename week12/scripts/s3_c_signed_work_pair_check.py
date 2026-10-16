"""One new authorized check of retained actual CSVs; never republishes an old task."""
from week12.src.car_domain import eightwheel_route as a
from week12.src.active_cluster.verify import check_csv,compare_numerics,SIGNED_WORK_ADDENDUM

def main():
    a.authorization()
    inputs=a.retained_pair_inputs(); manifest=a.read(inputs['manifest']); graph=a.read(inputs['graph'])
    spec=a.read(a.SPEC);scene=inputs['scene'];contract=manifest['output_contract']
    result=dict(schema='v11-retained-pair-posthoc-check-1',proposal_sha256=a.CONTRACT_SHA,
        authorization_sha256=a.AUTH_SHA,addendum=SIGNED_WORK_ADDENDUM,
        inputs_sha256=a.sha(a.RUN/'retained-pair-inputs.json'),old_task_and_reports_unchanged=True,
        base_physical=check_csv(inputs['base'],spec,graph,contract,scene),
        refined_physical=check_csv(inputs['refined'],spec,graph,contract,scene),
        numeric=compare_numerics(inputs['base'],inputs['refined'],contract,scene,numerical_addendum=SIGNED_WORK_ADDENDUM))
    result['status']='PASS' if all(result[k]['status']=='PASS' for k in ('base_physical','refined_physical','numeric')) else 'FAIL'
    a.save(a.RUN/'retained-pair-02-check.json',result)
    return 0 if result['status']=='PASS' else 2
if __name__=='__main__':raise SystemExit(main())
