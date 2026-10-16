"""Readiness returns before independent admission; vehicle never waits in-process."""
import argparse
from pathlib import Path
import unittest
from week12.src.active_cluster.execution import (DEFAULT_OUT,PROJECT,ORDER,
    Stop,sha,read,save,stamp,prepare,run_bound_round)
from week12.src.active_cluster.run_scope import RunScope,KEY,reconcile_start_failure
from week12.src.active_cluster import run_policy as policy

def readiness(route):
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name)
        for name in ('week12.tests.test_active_cluster_run_policy',
                     'week12.tests.test_active_cluster_run_scope'))
    tests=unittest.TextTestRunner(verbosity=2).run(suite)
    report={'ran':tests.testsRun,'failures':len(tests.failures),'errors':len(tests.errors),
            'status':'PASS' if tests.wasSuccessful() else 'FAIL'}
    if not tests.wasSuccessful():raise Stop('NEW_AND_AFFECTED_CONTRACT_CHECK_FAILED')
    for row in route.proposal['protected_sources']:
        if sha(PROJECT/row['path'])!=row['expected']:raise Stop('PROTECTED_SOURCE_DRIFT')
    mutable=set(route.proposal['allowed_initial_runtime_edits'])
    for row in route.proposal['current_source_snapshot']:
        if row['path'] not in mutable and sha(PROJECT/row['path'])!=row['sha256']:
            raise Stop('FROZEN_SOURCE_DRIFT:'+row['path'])
    sets=2-len(route.data[KEY]['repairs'])
    used=route.process_used()
    scope_used=used-route.base['external_process_accounted_s']
    policy.reserve_whole_sets(used,route.elapsed(),scope_used,sets,
                             helper_used=scope_used-route.scene_leaf())
    round_no=4+len(route.data[KEY]['repairs'])
    manifest,graph,binding=prepare(route,round_no)
    package=Path(manifest['package_path'])
    if not package.is_absolute():package=Path(binding['generated']['directory'])/package
    binding['frontend_reuse']={'model_sha256':sha(package),
        'result_sha256':route.proposal['baseline_before_this_preparation']['readiness_result_sha256'],
        'classes':manifest['model_class_by_scene'],'scope':'PASS_FRONTEND_ONLY'}
    route.frontend_reusable(binding,manifest)
    sysml=Path(manifest['sysml_path'])
    if not sysml.is_absolute():sysml=Path(binding['generated']['directory'])/sysml
    route.reuse_sysml(sysml,binding)
    route.refresh()
    route.data['source_bindings'][-1]=binding
    route.data[KEY]['binding']=binding
    route.data[KEY]['status']='READY_FOR_INDEPENDENT_REVIEW'
    route.persist()
    save(Path(binding['generated']['directory'])/'source-binding.json',binding)
    return {'status':'READY_EVIDENCE_ONLY','units':report,'binding':binding,
            'generation_review':read(Path(binding['generated']['directory'])/'independent-generation-review.json'),
            'frontend':'REUSED_EXACT_SOURCE_AND_CLASS_PROOF',
            'sysml':'REUSED_EXACT_BYTES_PRIOR_ACTUAL_PARSE','vehicle_children':0,
            'compile':'NOT_RUN','initialization':'NOT_RUN','simulation':'NOT_RUN','vehicle_task':'NOT_RUN'}

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('mode',choices=('readiness','vehicle'))
    parser.add_argument('--nonce',required=True)
    args=parser.parse_args()
    route=None
    result={'status':'FAIL','mode':args.mode,'compile':'NOT_RUN','simulation':'NOT_RUN','vehicle_task':'NOT_RUN'}
    try:
        route=RunScope(args.nonce,args.mode)
        route.limits()
        if args.mode=='readiness':result=readiness(route)
        else:
            route.validate_admission()
            route.data[KEY]['status']='VEHICLE_RUNNING';route.persist()
            scenes=run_bound_round(route,4+len(route.data[KEY]['repairs']))
            result={'status':'PASS_CANDIDATE_GATES_ONLY' if len(scenes)==14 and all(r['status']=='PASS' for r in scenes) else 'STOPPED',
                    'scenes':scenes,'whole_V11_complete':False}
    except Exception as exc:
        result.update(status='FAIL',error=str(exc))
        if route is not None:route.terminal(str(exc))
        else:reconcile_start_failure(DEFAULT_OUT,args.nonce,exc)
    finally:
        if route is not None:route.end_parent(result['status'],result.get('error',''))
        result['ended_utc']=stamp()
        save(DEFAULT_OUT/('execution-remediation-'+args.mode+'-'+args.nonce+'.json'),result)
    return 0 if result['status'] in ('READY_EVIDENCE_ONLY','PASS_CANDIDATE_GATES_ONLY') else 1

if __name__=='__main__':raise SystemExit(main())
