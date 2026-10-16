"""Developer execution entry for the explicitly approved active-cluster C route.

This is not product CLI/MCP E acceptance and has no online-model capability.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import sys
import time
import unittest


def main():
    from week12.src.active_cluster.execution import (
        Route, SPEC, DEFAULT_OUT, Stop, read, save, prepare, run_bound_round)
    p = argparse.ArgumentParser()
    p.add_argument('mode', choices=['prepare', 'run', 'workflow', 'parse-child', 'verify-child'])
    p.add_argument('--output', type=Path, default=DEFAULT_OUT)
    p.add_argument('--round', type=int, default=1)
    p.add_argument('--maximum-gate')
    p.add_argument('--source', type=Path)
    p.add_argument('--report', type=Path)
    p.add_argument('--manifest', type=Path)
    p.add_argument('--graph', type=Path)
    p.add_argument('--scene')
    p.add_argument('--base', type=Path)
    p.add_argument('--numerical-contract', type=Path)
    p.add_argument('--skip-unit-tests', action='store_true')
    p.add_argument('--inprocess-unit-tests', action='store_true')
    p.add_argument('--repair-evidence', type=Path)
    args = p.parse_args()
    dispatch=None
    code=2
    try:
        if args.mode == 'parse-child':
            from week12.src.toolchain import check_sysml
            args.report.parent.mkdir(parents=True, exist_ok=True)
            report = check_sysml(args.source.read_text(encoding='utf-8'))
            save(args.report, report)
            return 0 if report['status'] == 'PASS' else 2
        if args.mode == 'verify-child':
            from week12.src.active_cluster.verify import check_csv, compare_numerics
            manifest, graph = read(args.manifest), read(args.graph)
            physical = check_csv(args.source, read(SPEC), graph, manifest['output_contract'], args.scene)
            addendum = None
            provenance = None
            if args.numerical_contract is not None:
                from week12.src.car_domain.eightwheel_profile import authorization,PROPOSAL_PATH,CONTRACT_SHA,AUTH_SHA
                from week12.src.active_cluster.verify import SIGNED_WORK_ADDENDUM
                authority=authorization()
                if args.numerical_contract.resolve()!=PROPOSAL_PATH.resolve():
                    raise Stop('UNAPPROVED_NUMERICAL_CONTRACT')
                addendum=SIGNED_WORK_ADDENDUM
                provenance=dict(id=addendum,proposal_sha256=CONTRACT_SHA,authorization_sha256=AUTH_SHA)
            numeric = (compare_numerics(args.base, args.source, manifest['output_contract'], args.scene, numerical_addendum=addendum)
                       if args.base else {'status':'NOT_RUN_BASE_VARIANT'})
            save(args.report, {'physical':physical, 'numeric':numeric, 'numerical_addendum':provenance})
            return 0 if physical['status']=='PASS' and (not args.base or numeric['status']=='PASS') else 2
        route = Route(args.output)
        if args.repair_evidence:
            repair=route.accept_implementation_repair(args.repair_evidence)
            if repair['round'] != args.round:
                raise Stop('REPAIR_ROUND_ARGUMENT_MISMATCH')
        dispatch=route.begin_dispatch('developer-'+args.mode,args.round)
        modules=['week12.tests.test_active_cluster_execution',
                 'week12.tests.test_active_cluster_generation',
                 'week12.tests.test_active_cluster_verification']
        if args.mode == 'workflow' and args.inprocess_unit_tests:
            # This is one external dispatcher process; no extra child launches
            # or unrecorded time. All unit time consumes its 60s overhead cap.
            began=time.monotonic()
            report_dir=route.directory/f'unit-round-{args.round:02d}'
            report_dir.mkdir(exist_ok=False)
            with (report_dir/'unittest.log').open('w',encoding='utf-8') as stream:
                suite=unittest.TestLoader().loadTestsFromNames(modules)
                tested=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
            passed=tested.wasSuccessful()
            save(report_dir/'result.json',{'status':'PASS' if passed else 'FAIL',
                'tests_run':tested.testsRun,'duration_s':time.monotonic()-began,
                'execution':'in this accounted dispatcher process; no subprocess',
                'failures':len(tested.failures),'errors':len(tested.errors)})
            if not passed:
                route.terminal('NEW_UNIT_CHECKS_FAILED_NOT_PHYSICAL_PASS','STOPPED_UNIT_CHECK_FAILURE')
                raise Stop('NEW_UNIT_CHECKS_FAILED_NOT_PHYSICAL_PASS')
        elif args.mode == 'workflow' and not args.skip_unit_tests:
            slot=route.reserve('other','new active-cluster combined unit checks',180,args.round)
            result=route.child(slot,[sys.executable,'-B','-m','unittest',
                *modules,'-v'],
                SPEC.parents[3],180,route.directory/f'unit-round-{args.round:02d}','unittest')
            passed=result['returncode']==0 and not result['timed_out']
            route.finish(slot,'PASS' if passed else 'FAIL')
            if not passed:
                route.terminal('NEW_UNIT_CHECKS_FAILED_NOT_PHYSICAL_PASS','STOPPED_UNIT_CHECK_FAILURE')
                raise Stop('NEW_UNIT_CHECKS_FAILED_NOT_PHYSICAL_PASS')
        if args.mode in ('prepare','workflow'):
            manifest, _, _ = prepare(route, args.round)
            print('GENERATION_VERIFIED_NOT_PHYSICAL_PASS', manifest['package_path'], flush=True)
            if args.mode=='prepare':code=0;return code
        result = run_bound_round(route, args.round, args.maximum_gate)
        code=0 if result and all(x['status']=='PASS' for x in result) else 2
        return code
    except Exception as exc:
        print(type(exc).__name__ + ': ' + str(exc), flush=True)
        return 2
    finally:
        if dispatch:
            route.end_dispatch(dispatch,'PASS' if code==0 else 'FAIL')


if __name__ == '__main__':
    raise SystemExit(main())
