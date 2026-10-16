"""One launcher for the user-approved full vehicle recovery contract."""
import time
import unittest
from pathlib import Path
from datetime import datetime, timezone
from week12.src.active_cluster.execution import (DEFAULT_OUT, PROJECT, ORDER,
    Stop, sha, read, save, stamp, prepare, run_bound_round)
from week12.src.active_cluster.vehicle_recovery import RecoveryRoute

def main():
    route=None
    started=time.monotonic()
    result={'status':'FAIL','compile':'NOT_RUN','simulation':'NOT_RUN','vehicle_task':'NOT_RUN'}
    try:
        route=RecoveryRoute()
        p=route.proposal
        allowed={str((PROJECT/x).resolve()) for x in p['allowed_runtime_edit_paths']}
        for row in p['source_snapshot']:
            source=Path(row['path'])
            if not source.is_absolute():source=PROJECT/source
            if str(source.resolve()) not in allowed and sha(source)!=row['sha256']:
                raise Stop('DIAGNOSTIC_SOURCE_DRIFT:'+str(source))
        route.data['vehicle_recovery'].update(status='DISPATCHING',actual_started_utc=stamp(),
            old_process_before=super(RecoveryRoute,route).process_used(),
            scene_leaf_before=sum(float(q.get('duration_s',0)) for r in route.data['scene_attempts'] for q in r.get('processes',[])))
        route.persist()
        suite=unittest.defaultTestLoader.loadTestsFromName('week12.tests.test_active_cluster_vehicle_recovery')
        tests=unittest.TextTestRunner(verbosity=2).run(suite)
        result['adapter_checks']={'ran':tests.testsRun,'failures':len(tests.failures),'errors':len(tests.errors)}
        if not tests.wasSuccessful():raise Stop('RECOVERY_ADAPTER_NEGATIVE_CHECK_FAILED')
        for name,key in [('generator-readiness-root-review.json','readiness_root_review'),
                         ('readiness-diagnostic-01/result.json','readiness_result')]:
            if sha(DEFAULT_OUT/name)!=p['immutable_bindings'][key]:raise Stop('FRONTEND_RECEIPT_DRIFT')
        actual=read(DEFAULT_OUT/'readiness-diagnostic-01/result.json')
        if actual['status']!='PASS_FRONTEND_ONLY':raise Stop('NO_ACTUAL_FRONTEND_PASS')
        route.data.update(status='RECOVERY_SOURCE_BIND_PREPARATION',stop_reason='Explicit complete recovery contract approved; old failures retained.')
        route.persist()
        manifest,graph,binding=prepare(route,4)
        package=Path(manifest['package_path'])
        if not package.is_absolute():package=Path(binding['generated']['directory'])/package
        if sha(package)!=p['vehicle_model_sha256']:raise Stop('FROZEN_MODELICA_BYTE_DRIFT')
        binding['frontend_reuse']={'result_sha256':p['immutable_bindings']['readiness_result'],
            'model_sha256':sha(package),'scope':'PASS_FRONTEND_ONLY','classes':list(manifest['model_class_by_scene'].values())}
        if len(binding['frontend_reuse']['classes'])!=7:raise Stop('REGISTERED_CLASSES_DRIFT')
        route.refresh()
        route.data['source_bindings'][-1]=binding
        route.data['vehicle_recovery']['frontend_reuse']=binding['frontend_reuse']
        route.persist()
        save(Path(binding['generated']['directory'])/'source-binding.json',binding)
        route.reuse_sysml(Path(manifest['sysml_path']),binding)
        route.assert_ready()
        if route.process_used()+3180+60>10800 or route.elapsed()+3180+60>23400:
            raise Stop('NO_FULL_SET_RESERVE')
        scenes=run_bound_round(route,4)
        result.update(status='PASS_CANDIDATE_GATES_ONLY' if len(scenes)==14 and all(x['status']=='PASS' for x in scenes) else 'STOPPED',
            scenes=scenes,whole_V11_complete=False)
    except Exception as exc:
        result['error']=str(exc)
    finally:
        if route is not None:
            route.refresh()
            r=route.data['vehicle_recovery']
            child=sum(float(q.get('duration_s',0)) for row in route.data['scene_attempts'][len(route.base['scene_attempts']):] for q in row.get('processes',[]))
            r['parent_overhead_s']=max(0,time.monotonic()-started-child)
            r.update(status='CLOSED_'+result['status'],ended_utc=stamp(),child_process_s=child)
            r['attempts'][0].update(status=result['status'],ended_utc=stamp())
            route.data.update(status='STOPPED_RECOVERY_CLOSED',stop_reason=result.get('error',result['status']))
            route.persist()
        save(DEFAULT_OUT/'vehicle-recovery-result.json',result)
    return 0 if result['status']=='PASS_CANDIDATE_GATES_ONLY' else 1

if __name__=='__main__':
    raise SystemExit(main())
