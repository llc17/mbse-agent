"""One approved frozen eightwheel through the existing CLI/MCP Agent, zero API."""
from __future__ import annotations
import argparse,asyncio,json,os,sys,time,unittest
from pathlib import Path
from types import SimpleNamespace
from week12.src.car_domain import eightwheel_route as a

TESTS=['week12.tests.test_reference_core_chain','week12.tests.test_car_domain_adapters',
 'week12.tests.test_car_domain_route','week12.tests.test_reference_history.HistoricalEvidence',
 'week12.tests.test_eightwheel_handoff','week12.tests.test_eightwheel_integration',
 'week12.tests.test_eightwheel_evidence','week12.tests.test_eightwheel_route',
 'week12.tests.test_active_cluster_completion','week12.tests.test_active_cluster_generation',
 'week12.tests.test_active_cluster_verification','week12.tests.test_signed_work_numerics','week12.tests.test_eightwheel_deadline_path']

def checks_child(label):
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(n) for n in TESTS)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    value=dict(status='PASS' if result.wasSuccessful() else 'FAIL',tests=result.testsRun,
        failures=len(result.failures),errors=len(result.errors),new_model_calls=0,new_simulations=0)
    a.save(a.RUN/(label+'-tests.json'),value)
    return 0 if result.wasSuccessful() else 2

async def actual_case(cli):
    from week12.src.car_domain.eightwheel_profile import SOURCE_TEXT,make_approved_spec
    from week11.src.store import digest_json
    evidence=dict(real_stdio_mcp=True,new_model_calls=0,real_online_model=False,
        proposal_origin='EXISTING_APPROVED_FROZEN_SPEC_IMPORT',human_new_approval=False,
        authorization_sha256=a.AUTH_SHA,started_utc=a.stamp())
    created=await cli._show_call('project_create',{'name':'TEST_ONLY_V11_approved_eightwheel_signed_work_execution'})
    evidence['project_create']=created
    if not created.get('ok'):raise RuntimeError('MCP project create failed')
    source=cli.store.record_source(cli.session_id,SOURCE_TEXT)
    spec=make_approved_spec(source['source_id'])
    prepared=await cli._show_call('proposal_prepare',{'spec':spec});evidence['prepared']=prepared
    if not prepared.get('ok'):raise RuntimeError('MCP proposal prepare failed')
    proposal=prepared['proposal']
    if proposal['spec_digest']!=digest_json(spec):raise RuntimeError('spec changed')
    confirmed=await cli.confirm_approved_test_only(proposal['proposal_id']);evidence['confirmation']=confirmed
    if not confirmed.get('ok'):raise RuntimeError('existing approval import failed')
    started=await cli._show_call('execution_start',{'proposal_id':proposal['proposal_id'],'request_id':'eightwheel_signed_work_execution_once'})
    evidence['execution_start']=started
    if not started.get('ok'):raise RuntimeError('actual execution start rejected')
    task_id=started['task']['task_id'];watched=await cli._watch_task(task_id);evidence['terminal_task']=watched
    evidence['evidence_query']=await cli._call('evidence_query',{'question':'task:'+task_id})
    evidence['versions']=await cli._call('versions_list',{})
    task=watched.get('task',{});result=task.get('result') or {}
    evidence.update(status='COMPLETED_FIXED_EIGHTWHEEL' if task.get('state')=='succeeded' else 'STOPPED_WITH_EVIDENCE',
        vehicle_task_status=result.get('vehicle_task_status','UNKNOWN'),whole_V11_complete=False,ended_utc=a.stamp())
    a.save(a.RUN/'deadline-recovery-actual-agent-result.json',evidence)
    return evidence

async def run_actual():
    from week12.src.car_domain.cli_adapter import async_main
    db=a.RUN/'deadline-recovery-agent-store.sqlite'
    if db.exists():raise a.Stop('NO_DATABASE_RESTART_OR_SECOND_RUN')
    args=SimpleNamespace(db=db,session='eightwheel_signed_work_execution',debug_offline=True,profile=a.PROFILE)
    return await async_main(args,case=actual_case)

def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['checks','checks-child','admit','archive-check','run','final'])
    parser.add_argument('--label',default='software-01');args=parser.parse_args(argv)
    if args.command=='checks-child':return checks_child(args.label)
    token=a.reserve_parent('eightwheel-dispatch:'+args.command+':'+args.label,pid=os.getpid())
    a.set_parent_context(token);started=time.monotonic();status='UNKNOWN';code=0
    try:
        if args.command=='checks':
            r=a.run_check([sys.executable,'-X','utf8','-B','-m','week12.scripts.s3_c_eightwheel_first_run','checks-child','--label',args.label],a.PROJECT,args.label)
            code=0 if r['returncode']==0 and not any(r[k] for k in ('timed_out','uncertain','interrupted')) else 2
            if code==0:
                binding=a.RUN/'deadline-recovery-binding.json';digest=a.create_implementation_binding(binding)
                a.save(a.RUN/'deadline-recovery-readiness.json',dict(status='PASS_IMPLEMENTATION_ONLY',binding_sha256=digest,
                    test_result=str(a.RUN/(args.label+'-tests.json')),new_simulations=0,new_model_calls=0))
        elif args.command=='admit':
            a.admit(a.RUN/'deadline-recovery-binding.json',a.RUN/'deadline-recovery-readiness.json',a.RUN/'deadline-recovery-independent-admission.json')
        elif args.command=='archive-check':
            raise a.Stop('MODULE_ARCHIVED_CHECK_FORBIDDEN')
        elif args.command=='run':
            result=asyncio.run(run_actual());code=0 if result['status']=='COMPLETED_FIXED_EIGHTWHEEL' else 2
        elif args.command=='final':
            with a.transaction() as (_,s):
                a.save(a.RUN/'deadline-recovery-final-scope-checkpoint.json',s)
        status='RETURNED'
    except Exception as exc:
        print(type(exc).__name__+': '+str(exc),file=sys.stderr,flush=True)
        code=2;status='RETURNED'
    finally:
        a.finish_parent(token,time.monotonic()-started,status=status)
    return code

if __name__=='__main__':raise SystemExit(main())
