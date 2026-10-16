"""Real reference-profile acceptance through inherited V10 CLI/MCP/Store/Worker."""
from pathlib import Path
import argparse, asyncio, json, os, sys, time, unittest
from week12.src.car_domain.reference_route import DEFAULT_OUT, PROJECT, read, save, stamp
from week12.src.car_domain.reference_execution import EXECUTION


def http_child(args):
    from week12.src.car_domain import reference_route as a
    from week11.src.runtime import DeepSeekRuntime,_load_config,_safe_transport_diagnostic
    job=(DEFAULT_OUT/(EXECUTION['artifact_prefix']+'-http')/args.request_token).resolve()
    if args.source.resolve()!=job/'request.json' or args.report.resolve()!=job/'response.json':raise a.Stop('HTTP_PATH_NOT_RESERVED')
    phase='admission'
    try:
        with a.transaction() as (_,s):
            a.assert_scope(s)
            row=next((r for r in s.get('product_requests',[]) if r['token']==args.request_token),None)
            if not row or row['status']!='RESERVED' or row.get('http_pid') or row.get('input_sha256')!=a.sha(args.source):raise a.Stop('HTTP_CHILD_NOT_EXCLUSIVELY_OWNED')
            a.assert_binding(read(s['admission']['binding_path']));row['http_pid']=os.getpid()
        phase='input_and_configuration'
        payload=read(args.source)
        runtime=object.__new__(DeepSeekRuntime);runtime.config=_load_config(PROJECT/'week12/configs/car_domain_runtime.json');runtime.transport=None
        phase='transport'
        response=DeepSeekRuntime._send(runtime,payload['messages'],payload['tools'])
        phase='response_persistence'
        if args.report.exists():raise a.Stop('HTTP_RESPONSE_OVERWRITE')
        save(args.report,response);return 0
    except Exception as exc:
        # Reuse V10's type/code-only sanitizer. No exception message, URL,
        # request/response body, headers, credential or raw stderr is recorded.
        diagnostic=dict(schema='v11-safe-http-diagnostic-1',phase=phase,
            request_token=args.request_token,input_sha256=a.sha(args.source),pid=os.getpid(),
            **json.loads(_safe_transport_diagnostic(exc)))
        try:
            with (job/'error.json').open('x',encoding='utf8') as stream:
                json.dump(diagnostic,stream,ensure_ascii=False,allow_nan=False)
                stream.write('\n')
        except FileExistsError:
            pass  # Never replace an earlier failure for this reserved request.
        return 2


def verify_child(args):
    from week12.src.car_domain.reference_verify import check_reference_csv
    r=check_reference_csv(args.source,read(args.spec),args.model,args.report)
    return 0 if r.get('status')=='PASS_IN_REFERENCE_SCOPE' else 2

def parse_child(args):
    from week12.src.toolchain import check_sysml
    r=check_sysml(args.source.read_text(encoding='utf8'))
    save(args.report,r)
    return 0 if r.get('status')=='PASS' else 2

def readiness(args):
    from week12.src.car_domain.reference_route import create_implementation_binding
    began=time.monotonic();folder=DEFAULT_OUT/(EXECUTION['artifact_prefix']+'-readiness');folder.mkdir(exist_ok=True)
    label='initial' if args.round==1 else 'repair'
    with (folder/(label+'-tests.log')).open('w',encoding='utf8') as stream:
        suite=unittest.TestLoader().loadTestsFromNames(['week12.tests.test_reference_core_chain',
            'week12.tests.test_car_domain_adapters','week12.tests.test_car_domain_route'])
        tested=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    report=dict(status='PASS_IMPLEMENTATION_ONLY' if tested.wasSuccessful() else 'FAIL_IMPLEMENTATION',
        tests_run=tested.testsRun,failures=len(tested.failures),errors=len(tested.errors),duration_s=time.monotonic()-began,
        not_physics_evidence=True,not_online_evidence=True,round=args.round)
    if tested.wasSuccessful():
        binding=folder/(label+'-source-binding.json')
        report.update(binding_sha256=create_implementation_binding(binding),binding_path=str(binding))
    save(folder/(label+'-result.json'),report)
    print(json.dumps(report,ensure_ascii=False),flush=True)
    return 0 if tested.wasSuccessful() else 2

def readiness_wrapper(args):
    from week12.src.car_domain import reference_route as a
    from week12.src.car_domain.reference_route import bounded_run
    label='initial' if args.round==1 else 'repair'
    token=a.reserve_parent('reference-readiness-'+label,pid=os.getpid());a.set_parent_context(token)
    began=time.monotonic();status='UNKNOWN'
    with a.transaction() as (_,s):
        a.assert_scope(s,20)
        if s['counts']['checks']>=a.CAPS['checks']:raise a.Stop('OTHER_CHECK_CAP')
        if args.round==2 and s['counts']['repairs']!=1:raise a.Stop('NO_APPROVED_BOUNDED_IMPLEMENTATION_REPAIR')
        s['counts']['checks']+=1
        op=dict(kind='check',label=label+' readiness',parent_token=token,status='RUNNING',started_utc=stamp())
        s.setdefault('operations',[]).append(op)
    try:
        r=bounded_run([sys.executable,'-X','utf8','-B','-m','week12.scripts.s3_c_car_increment_acceptance','readiness','--round',str(args.round)],PROJECT,20)
        folder=DEFAULT_OUT/(EXECUTION['artifact_prefix']+'-readiness');folder.mkdir(exist_ok=True)
        for stream in ('stdout','stderr'):(folder/(label+'-wrapper.'+stream+'.log')).write_text(r[stream],encoding='utf8')
        ok=r['returncode']==0 and not r['timed_out'] and not r['uncertain'] and not r['interrupted'] and r['duration_s']<=20
        with a.transaction() as (_,s):
            s['external_s']+=r['duration_s'];s['operations'][-1].update(status='RETURNED',ended_utc=stamp(),duration_s=r['duration_s'],actual_started_utc=r['actual_started_utc'],actual_ended_utc=r['actual_ended_utc'],
                result={k:v for k,v in r.items() if k not in ('stdout','stderr')})
            s['gates']['implementation']='PASS' if ok else 'FAIL'
            s['status']='IMPLEMENTATION_READY' if ok else 'IMPLEMENTATION_CHECK_FAILED_NO_EXECUTION'
        status='RETURNED' if not r['uncertain'] and not r['interrupted'] else 'UNKNOWN'
        return 0 if ok else 2
    finally:a.finish_parent(token,time.monotonic()-began,status=status)

async def run_case(cli):
    # This harness imports facts/explicit reuse from the user-approved full contract.
    # It is not a new human utterance or fabricated local approval UX.
    source=('复用用户原成功案例参考模板，先把链路打通。总质量85kg含设备载荷20kg；'
        '台阶高度0.15m；台阶深度0.28m；连续级数20级；'
        'passage_task=ascend_and_forward_head_down_front_first_descend。'
        '按既有批准的原参数模板，整理需求与约束表并调用proposal_prepare。'
        '本参考包仅回放原完整流程，不宣称满足可信轮胎接触或车辆验收。')
    created=await cli._show_call('project_create',{'name':'TEST_ONLY_reference_core_chain'})
    if not created.get('ok'):raise RuntimeError('actual MCP project_create failed')
    prepared=await cli.handle_text(source)
    evidence=dict(real_stdio_mcp=True,real_online_model=bool(cli.responses),human_new_approval=False,
        source_origin='EXISTING_USER_APPROVED_REFERENCE_CONTRACT_ACCEPTANCE_INPUT',source_text=source,
        prepared=prepared,created=created,utc=stamp())
    proposal_id=cli.last_proposal_id
    if not proposal_id:
        return {**evidence,'status':'STOPPED_NO_VALID_PROPOSAL','vehicle_task_status':'NOT_RUN'}
    return await execute_prepared(cli,proposal_id,evidence)


async def execute_prepared(cli,proposal_id,evidence):
    from week12.src.car_domain.reference_route import SUCCESS
    confirmed=await cli.confirm_approved_test_only(proposal_id);evidence['confirmation']=confirmed
    started=await cli._show_call('execution_start',{'proposal_id':proposal_id,'request_id':'approved_reference_'+EXECUTION['key']})
    evidence['execution_start']=started
    if not started.get('ok'):return {**evidence,'status':'STOPPED_EXECUTION_START'}
    task_id=started['task']['task_id'];watched=await cli._watch_task(task_id);evidence['terminal_task']=watched
    queried=await cli._call('evidence_query',{'question':'task:'+task_id});evidence['evidence_query']=queried
    versions=await cli._call('versions_list',{});evidence['versions']=versions
    outcome=(watched.get('task') or {}).get('result') or {}
    passed=(watched.get('task',{}).get('state')=='succeeded' and queried.get('ok') is True
        and versions.get('ok') is True and versions.get('versions',{}).get('items')==[]
        and versions.get('versions',{}).get('current_version_id') is None and evidence.get('real_online_model') is True
        and all(outcome.get(k)==v for k,v in SUCCESS.items()))
    return {**evidence,'status':'REFERENCE_CASE_COMPLETED' if passed else 'STOPPED_REFERENCE_NOT_COMPLETE',
        'reference_chain_status':outcome.get('reference_chain_status','UNKNOWN'),
        'vehicle_task_status':outcome.get('vehicle_task_status','UNKNOWN'),'whole_V11_complete':False}

async def online(args):
    from types import SimpleNamespace
    from week12.src.car_domain.cli_adapter import async_main
    runargs=SimpleNamespace(db=DEFAULT_OUT/(EXECUTION['artifact_prefix']+'-agent-store.sqlite'),session=EXECUTION['key'],debug_offline=False)
    r=await async_main(runargs,case=run_case)
    save(DEFAULT_OUT/(EXECUTION['artifact_prefix']+'-online-candidate.json'),r)
    return 0 if isinstance(r,dict) and r.get('status')=='REFERENCE_CASE_COMPLETED' else 2

def prepare_local_reuse():
    import sqlite3
    from contextlib import closing
    from week11.src.runtime import _parse_response
    from week11.src.store import digest_json
    from week12.src.car_domain import reference_route as a
    from week12.src.car_domain.contracts import validate_spec
    if any(EXECUTION['caps'][k]!=0 for k in ('product_requests','product_tokens','product_usd')):
        raise a.Stop('LOCAL_REUSE_REQUIRES_ZERO_PRODUCT_BUDGET')
    contract=read(DEFAULT_OUT/EXECUTION['proposal_file']);reuse=contract['local_reuse']
    resolved={}
    for name in ('database','response','prior_result'):
        item=reuse[name];path=(DEFAULT_OUT/item['path']).resolve()
        path.relative_to(DEFAULT_OUT.resolve())
        if a.sha(path)!=item['sha256']:raise a.Stop('LOCAL_REUSE_INPUT_HASH_DIFFERS:'+name)
        resolved[name]=path
    prior=read(resolved['prior_result'])
    if (prior.get('proposal_id')!=reuse['proposal_id'] or prior.get('project_id')!=reuse['project_id']
            or prior.get('task_id')!=reuse['task_id'] or not prior.get('actual_progress',{}).get('real_model_response')):
        raise a.Stop('LOCAL_REUSE_PRIOR_RUN_BINDING_DIFFERS')
    response,ninput,noutput=_parse_response(read(resolved['response']))
    calls=response.get('tool_calls',[])
    if len(calls)!=1 or calls[0]['function']['name']!='proposal_prepare':
        raise a.Stop('LOCAL_REUSE_EXPECTS_ACTUAL_PREPARED_PROPOSAL')
    checked=validate_spec(json.loads(calls[0]['function']['arguments'])['spec'])
    if not checked.get('ready'):raise a.Stop('LOCAL_REUSE_RESPONSE_SPEC_INVALID')
    destination=DEFAULT_OUT/(EXECUTION['artifact_prefix']+'-agent-store.sqlite')
    if destination.exists():raise a.Stop('LOCAL_REUSE_DATABASE_ALREADY_EXISTS')
    with closing(sqlite3.connect(resolved['database'].as_uri()+'?mode=ro',uri=True)) as original:
        original.row_factory=sqlite3.Row
        row=original.execute('SELECT spec_json,spec_digest,session_id,project_id FROM proposals WHERE proposal_id=?',(reuse['proposal_id'],)).fetchone()
        task=original.execute('SELECT state,proposal_id FROM tasks WHERE task_id=?',(reuse['task_id'],)).fetchone()
        if (row is None or task is None or task['state']!='failed' or task['proposal_id']!=reuse['proposal_id']
                or row['session_id']!=reuse['session_id'] or row['project_id']!=reuse['project_id']
                or digest_json(checked['spec'])!=row['spec_digest'] or digest_json(json.loads(row['spec_json']))!=row['spec_digest']):
            raise a.Stop('LOCAL_REUSE_DATABASE_SPEC_OR_TASK_DIFFERS')
        with closing(sqlite3.connect(destination)) as target:original.backup(target)
    if a.sha(resolved['database'])!=reuse['database']['sha256']:raise a.Stop('LOCAL_REUSE_ORIGINAL_DATABASE_CHANGED')
    evidence=dict(real_stdio_mcp=True,real_online_model=True,new_model_calls=0,
        model_response_origin='HASH_BOUND_PRIOR_REAL_RESPONSE',human_new_approval=False,
        prior_run=reuse,prior_response_usage=dict(prompt_tokens=ninput,completion_tokens=noutput),
        source_origin='REUSE_PRIOR_USER_APPROVED_REFERENCE_SPEC',utc=stamp())
    return destination,reuse,checked['spec'],evidence


async def local_resume(args):
    from types import SimpleNamespace
    from week11.src.store import digest_json
    from week12.src.car_domain.cli_adapter import async_main
    db,reuse,spec,evidence=prepare_local_reuse()
    async def resume_case(cli):
        status=await cli._call('project_status',{})
        if status.get('status',{}).get('project',{}).get('project_id')!=reuse['project_id']:
            raise RuntimeError('local reuse project is not selected')
        prepared=await cli._show_call('proposal_prepare',{'spec':spec})
        if not prepared.get('ok'):raise RuntimeError('local reuse proposal preparation failed')
        proposal=prepared['proposal']
        if proposal.get('spec_digest')!=digest_json(spec):raise RuntimeError('local reuse changed specification')
        evidence['prepared']=prepared
        return await execute_prepared(cli,proposal['proposal_id'],evidence)
    runargs=SimpleNamespace(db=db,session=reuse['session_id'],debug_offline=True)
    result=await async_main(runargs,case=resume_case)
    save(DEFAULT_OUT/(EXECUTION['artifact_prefix']+'-online-candidate.json'),result)
    return 0 if result.get('status')=='REFERENCE_CASE_COMPLETED' else 2


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('mode',choices=['readiness','readiness-wrapper','online','local-resume','verify-child','parse-child','http-child'])
    parser.add_argument('--round',type=int,choices=[1,2],default=1)
    for field in ('source','model','spec','report'):parser.add_argument('--'+field,type=Path)
    parser.add_argument('--request-token')
    args=parser.parse_args()
    if args.mode=='http-child':return http_child(args)
    if args.mode=='verify-child':return verify_child(args)
    if args.mode=='parse-child':return parse_child(args)
    if args.mode=='readiness':return readiness(args)
    if args.mode=='readiness-wrapper':return readiness_wrapper(args)
    from week12.src.car_domain import reference_route as a
    with a.transaction() as (_,s):
        a.assert_scope(s)
        if not s.get('admission'):raise a.Stop('NO_INDEPENDENT_ADMISSION_FOR_ONLINE')
        adm=s['admission'];a.assert_binding(read(adm['binding_path']))
    token=a.reserve_parent('reference-acceptance-host',pid=os.getpid());a.set_parent_context(token)
    began=time.monotonic();status='UNKNOWN'
    try:
        code=asyncio.run(local_resume(args) if args.mode=='local-resume' else online(args));status='RETURNED'
    finally:a.finish_parent(token,time.monotonic()-began,status=status)
    # The task completes through V10's worker/store path; the acceptance host
    # only checks its own completed session/account before reporting the run.
    candidate=read(DEFAULT_OUT/(EXECUTION['artifact_prefix']+'-online-candidate.json'))
    if code==0:
        try:
            with a.transaction() as (_,scope):
                a.assert_scope(scope)
                current_parents=[p for p in scope['parents'] if p.get('token')==token or
                    (p.get('parent_token') and a._descends(p['parent_token'],token,scope['parents']))]
                if not current_parents or any(p['status']!='RETURNED' for p in current_parents):
                    raise a.Stop('ACCEPTANCE_PARENT_NOT_RETURNED')
        except a.Stop as exc:
            candidate.update(status='STOPPED_ACCEPTANCE_FINAL_ACCOUNT',reference_chain_status='UNKNOWN',
                final_account_error=str(exc),vehicle_task_status='UNKNOWN')
            code=2
    candidate['acceptance_exit_status']=code
    save(DEFAULT_OUT/(EXECUTION['artifact_prefix']+'-online-result.json'),candidate)
    return code

if __name__=='__main__':raise SystemExit(main())
