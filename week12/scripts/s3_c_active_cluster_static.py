"""One reserved post-stop static diagnostic; never binds or runs a vehicle.

Only the two separately approved remaining diagnostics can call this entry.
The second implementation repair is already consumed: no retry, checkModel,
buildModel, simulation, force assignment or changed physical specification here.
"""
from pathlib import Path
import sys
import time
import unittest

from week12.src.active_cluster.execution import Route, SPEC, save, sha, stamp
from week12.src.active_cluster.design import load_design
from week12.src.active_cluster.modelica import lower


def modelica_load_only():
    """Final approved syntax diagnostic: loadFile only, no model instantiation."""
    from week12.src.toolchain import _run, _marker
    from week12.src.component_physics.runner import OMC, COMPLEX, SERVICES, MSL
    route=Route();slot=len(route.data['other_checks'])-1;row=route.data['other_checks'][slot]
    if (route.auxiliary_limit()!=15 or slot!=14 or
            row['label']!='post-stop Modelica syntax load only' or row['status']!='RUNNING' or
            len(route.data['repairs'])!=2 or not route.data['status'].startswith('STOPPED')):
        raise RuntimeError('NO_EXPLICIT_RESERVED_STATIC_DIAGNOSTIC')
    began=time.monotonic();folder=route.directory/'diagnostic-syntax-preview'
    paths=[COMPLEX,SERVICES,MSL,
        route.directory.parents[1]/'models/components/V11PhysicalB1/package.mo',
        route.directory.parents[1]/'models/components/V11ContactC2/package.mo',
        route.directory.parents[1]/'models/components/V11ActiveClusterC3/package.mo',folder/'package.mo']
    lines=[]
    for i,path in enumerate(paths):
        lines += [f'ok := loadFile("{path.as_posix()}");',
                  f'print("__STATIC_LOAD_{i}__="+String(ok)+"\\n");',
                  'print(getErrorString()+"\\n");','if not ok then exit(1); end if;']
    lines += ['print("__STATIC_OMC__="+getVersion()+"\\n");',
              'print("__STATIC_MSL__="+getVersion(Modelica)+"\\n");',
              'print("__STATIC_MSL_SOURCE__="+getSourceFile(Modelica)+"\\n");']
    (folder/'load-only.mos').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    result=_run([str(OMC),'load-only.mos'],folder,20)
    for stream in ('stdout','stderr'):
        (folder/f'load-only.{stream}.log').write_text(result[stream],encoding='utf-8')
    loaded=(result['returncode']==0 and not result['timed_out'] and not result['uncertain'] and
            _marker(result['stdout'],'__STATIC_LOAD_6__')=='true')
    report={'status':'PASS_SYNTAX_LOAD_ONLY' if loaded else 'FAIL',
            'method':'loadFile only: no checkModel/buildModel/simulation',
            'loaded':loaded,'actual_OMC':_marker(result['stdout'],'__STATIC_OMC__'),
            'actual_MSL':_marker(result['stdout'],'__STATIC_MSL__'),
            'actual_MSL_source':_marker(result['stdout'],'__STATIC_MSL_SOURCE__'),
            'returncode':result['returncode'],'timed_out':result['timed_out'],
            'uncertain':result['uncertain'],'physical_status':'NOT_RUN',
            'component_compile_status':'NOT_VERIFIED','preview_sha256':sha(folder/'package.mo')}
    save(folder/'load-only-result.json',report)
    route.refresh();row=route.data['other_checks'][slot]
    row['processes'].append({k:v for k,v in result.items() if k not in ('stdout','stderr')})
    overhead=max(0,time.monotonic()-began-result['duration_s'])
    row['processes'].append({'duration_s':overhead,'dispatcher_overhead':True,
                            'label':'syntax diagnostic parent excluding OMC child'})
    row.update(status='PASS' if loaded else 'FAIL',ended_utc=stamp())
    route.persist()
    print(report['status'],flush=True)
    return 0 if loaded else 2


def main():
    route=Route()
    slot=len(route.data['other_checks'])-1
    row=route.data['other_checks'][slot]
    if (route.auxiliary_limit()!=15 or slot!=13 or
            row['label']!='post-stop Python static diagnostic' or row['status']!='RUNNING' or
            len(route.data['repairs'])!=2 or not route.data['status'].startswith('STOPPED')):
        raise RuntimeError('NO_EXPLICIT_RESERVED_STATIC_DIAGNOSTIC')
    began=time.monotonic()
    folder=route.directory/'diagnostic-syntax-preview'
    folder.mkdir(exist_ok=False)
    outcome={'status':'FAIL','scope':'post-stop static preparation only',
             'no_vehicle_source_binding':True,'no_omc_or_simulation':True}
    try:
        with (folder/'unittest.log').open('w',encoding='utf-8') as stream:
            suite=unittest.TestLoader().loadTestsFromNames([
                'week12.tests.test_active_cluster_execution',
                'week12.tests.test_active_cluster_generation',
                'week12.tests.test_active_cluster_verification'])
            result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
        outcome.update(tests_run=result.testsRun,failures=len(result.failures),errors=len(result.errors))
        if not result.wasSuccessful():
            raise RuntimeError('STATIC_UNIT_FAILURE')
        design=load_design(SPEC)
        text,_=lower(design)
        (folder/'package.mo').write_text(text,encoding='utf-8')
        outcome.update(status='PASS_PYTHON_STATIC_ONLY',
                       preview_sha256=sha(folder/'package.mo'),
                       modelica_load_status='NOT_RUN',physical_status='NOT_RUN')
        print('PASS_PYTHON_STATIC_ONLY',result.testsRun,flush=True)
        return 0
    finally:
        elapsed=time.monotonic()-began
        outcome['duration_s']=elapsed
        save(folder/'result.json',outcome)
        route.refresh()
        row=route.data['other_checks'][slot]
        row['processes'].append({'label':'single Python static diagnostic, no child processes',
                                 'duration_s':elapsed,'returncode':0 if outcome['status'].startswith('PASS') else 2})
        row.update(status='PASS' if outcome['status'].startswith('PASS') else 'FAIL',ended_utc=stamp())
        route.persist()


if __name__=='__main__':
    raise SystemExit(modelica_load_only() if sys.argv[1:]==['modelica-load'] else main())
