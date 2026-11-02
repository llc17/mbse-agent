"""Ordinary constrained success-case CLI; importing this module dispatches nothing."""
import argparse,asyncio,json,os,sys,time,uuid,re
# Existing request commands select their context before importing runtime/paths.
# Bootstrap deliberately starts without a selector because its context does not
# exist yet. Explicit TaskFlow runtime context is a second binding check.
if __name__=='__main__' and len(sys.argv)>1 and sys.argv[1] in ('plan-request','revalidate-request','revalidate-generation','inspect-saved-generation','execute-request','confirm-request','authorize-request','repair-request','repair-execution','recover-local-proposal','recover-plan-connect','request-plan-recovery-info','request-plan-recover-timeout','request-recover-response','run-request','request-result','request-status','request-ask','request-reopen','request-recover'):
    if '--request-id' in sys.argv:
        offset=sys.argv.index('--request-id')
        if offset+1<len(sys.argv):os.environ['MBSE_V12_REQUEST_ID']=sys.argv[offset+1]
from datetime import timedelta
from pathlib import Path
from .case_paths import ROOT,REPO,RUN,PACKAGE,CONFIG,BUDGET_DB,authorization,authorization_sha,read_json,save_json,sha_value,remaining,require_time
from .case_runtime import CaseRuntime
from .case_session import create,open_service,prepare_design,confirm_approved,status,OUTPUTS,check_execution_policy
from .case_scope import check_delta,modelica_parameters
from .case_backend import module
from .case_diagnostics import phase,exception_record,failure_record,attach_calls,timeout_binding,tool_name

DESIGN_PROMPT='''你是既有MBSE产品Agent。所有面向人的说明、summary、rationale、defaults、unknowns、basis使用中文，代码标识符和必要技术名称保留。根据明确成功案例、新需求、默认与获批可改范围，提出有据的受约束联动方案，而非另造车辆。先比较继承模型有效参数及其方程依赖；不要把等效扭矩称真实电机额定，不因较慢调车。缺失基准SysML由你生成并反映原85kg代码，改型设计单列新质量和允许扭矩。保留原几何、接触、控制形式和任务。新增质量位于原参考重心的理想点质量，I_pitch保持20只在该近似内成立。继承简化层级，限制与未知要说明。历史案例成功不是新V12运行PASS。原嵌套Euler注解保留，但本次批准的真实求解器为DASSL，不能把嵌套注解当实际求解器。
只调用case_design_prepare一次，返回完整packet。packet只含baseline_requirements、baseline_design、baseline_sysml_source、candidate_requirements、candidate_design、proposed_parameters、rationale、defaults、unknowns。requirements每项严格{text,source_id,source_text}，source_text必须是trusted_sources中的精确非空子串。design严格{summary,assumptions,parameters}，assumptions每项{text,origin:"model"}；parameters每项{name,value,unit,origin:"model",basis}，值是有效方程参数数值。candidate_design.parameters至少含m/I_pitch/T_main_max/T_nose_max，proposed_parameters恰含这四个参数且与candidate_design一致。baseline_design.parameters仅写原85kg实际参数，不混入新设计。rationale为非空字符串，defaults/unknowns为列表。不要执行或编造结果。JSON只编码一次，解码后source字符串必须含真正换行和引号；截断或缺项不可猜补。'''

GENERATION_PROMPT='''你是既有MBSE产品Agent，落实已经保存的受约束设计。人类可读解释用中文，已存元数据原样继承。只调用proposal_prepare一次，完整spec。真正继承给出的原Modelica全部物理、地形floor差异、控制、初值和有效表达式；除批准的质量、两扭矩与当前运行模式明确要求的StopTime/Tolerance/Interval观察注解外保持token一致。不得用新简化模型替换，不改a/b、鼻轮/轮径、楼梯/接触/摩擦/目标速度/控制，不增删物理。保留表达式a+L_nose_ext等，不改成字面量凑校验。模型class名称保持原名。原嵌套Euler注解保留，但本次显式运行DASSL；产品此前把注解写成实际Euler是保留的事实冲突，不是执行依据。
spec恰含schema_version:"v11-model-artifact-1",backend:"model-artifact",baseline_ref,artifact,sources,requirements,design,libraries,settings,outputs,checks。artifact恰含model_name,modelica_source,sysml_source。sources必须按合同顺序仅返回[{"id":"来源ID"}]，禁止text；可信服务按ID读取原文，不能复述改写历史资料。requirements/design使用保存产品设计原样；settings/outputs/libraries/checks按trusted_generation_contract原样。不要trajectory_contract，不加自行性能阈值。两个源码一致，SysML中的全部参数及依赖表达式对应新Modelica；req_N映射精确对应新requirements及model_name。基准SysML只是原85kg映射，改型SysML须同步改质量及有据选择的扭矩。JSON单层编码，参数解码一次得到源码原文，不含字面\\n或\\"，不要markdown包裹。函数arguments只一个{"spec":{...}}对象，checks必须在spec内。不得执行、声称结果或授予权限。'''

_requirement_schema=dict(type='object',properties={k:dict(type='string') for k in ('text','source_id','source_text')},required=['text','source_id','source_text'],additionalProperties=False)
_design_schema=dict(type='object',properties=dict(summary=dict(type='string'),assumptions=dict(type='array',items=dict(type='object')),parameters=dict(type='array',items=dict(type='object'))),required=['summary','assumptions','parameters'],additionalProperties=False)
_packet_properties=dict(baseline_requirements=dict(type='array',items=_requirement_schema),baseline_design=_design_schema,baseline_sysml_source=dict(type='string',description='COMPLETE SysML package { ... } with System, system usage and ALL req_N exact requirement/source/model mappings.'),candidate_requirements=dict(type='array',items=_requirement_schema),candidate_design=_design_schema,proposed_parameters=dict(type='object',properties={k:dict(type='number') for k in ('m','I_pitch','T_main_max','T_nose_max')},required=['m','I_pitch','T_main_max','T_nose_max'],additionalProperties=False),rationale=dict(type='string'),defaults=dict(type='array',items=dict(type='string')),unknowns=dict(type='array',items=dict(type='string')))
DESIGN_TOOL=dict(type='function',function=dict(name='case_design_prepare',description='Save a source-bound constrained design and generate the complete missing baseline SysML; no execution or approval.',parameters=dict(type='object',properties=dict(packet=dict(type='object',properties=_packet_properties,required=list(_packet_properties),additionalProperties=False)),required=['packet'],additionalProperties=False)))

def runtime():return CaseRuntime(CONFIG,BUDGET_DB,reopen=True)

def factual_notes():
    original=RUN/'design-factual-notes.json'
    return dict(original_known_conflict=read_json(original) if original.exists() else None,later_product_notes=[read_json(p) for p in sorted((RUN/'design-factual-notes').glob('*.json'),key=lambda p:p.stat().st_mtime_ns)])

def _journal(rid,kind,value):
    return save_json(RUN/'cli-turns'/(sha_value(rid)+'-'+kind+'.json'),value,immutable=True)

def _parse_one(response,name,key):
    calls=response.get('tool_calls') or []
    if response.get('finish_reason')=='length':raise ValueError('truncated product output; no semantic repair')
    if len(calls)!=1 or calls[0].get('function',{}).get('name')!=name:raise ValueError('product must return exactly one '+name)
    args=calls[0]['function']['arguments'];args=json.loads(args) if isinstance(args,str) else args
    if not isinstance(args,dict) or set(args)!={key} or not isinstance(args[key],dict):raise ValueError('complete single-layer '+key+' object required')
    return args[key]

def _failure_feedback(action,index):
    if index<=0:return None
    failure_path=RUN/(action+'-'+str(index-1)+'-failure.json')
    if not failure_path.exists():raise ValueError('repair needs recorded prior '+action+' failure at previous index')
    failure=read_json(failure_path)
    prior_rid=failure.get('request_id')
    response=None
    if prior_rid:
        local=RUN/'cli-turns'/(sha_value(prior_rid)+'-response.json')
        protocol=RUN/'http'/sha_value(prior_rid)/'protocol-response.json'
        if local.exists():response=read_json(local)
        elif protocol.exists():response=read_json(protocol)
    # Feed the original tool/content protocol, never invent missing syntax or equations.
    validation_path=RUN/(action+'-'+str(index-1)+'-validation.json')
    inventory_path=RUN/(action+'-'+str(index-1)+'-source-inventory.json')
    return dict(recorded_failure=failure,additional_local_validation=read_json(validation_path) if validation_path.exists() else None,source_inventory=read_json(inventory_path) if inventory_path.exists() else None,prior_response_tool_calls=response.get('tool_calls') if response else None,prior_response_content=response.get('content') if response else None,required_action='Correct the identified output/interface issue without expanding the approved physical scope. Preserve the previous complete candidate parameter commitments; do not tune parameters to solve schema issues. Generate a COMPLETE SysML package with opening/closing braces, every shared arithmetic parameter and ALL req_N source/model traces; this is missing content, not permission for development code to guess brackets or requirement mappings. proposed_parameters must be a four-name numeric value object. Human-readable explanations in Chinese. Actual execution DASSL; original nested Euler annotation is not actual solver evidence. Emit complete valid tool arguments.')

def design_request(index=0,mode='initial',previous_version=None):
    run=read_json(RUN/'run.json');a=authorization();source=(RUN/'baseline-original.mo').read_bytes().decode('utf-8')
    values,expressions,_=modelica_parameters(source)
    context=dict(case=run,original_modelica_source=source,baseline_parameters=values,baseline_expressions=expressions,model_name=a['baseline']['model_name'],approved_scope=a['baseline'],task=dict(stair_height=.15,stair_depth=.28,up_steps=20,down_steps=20,platform_length=2.,exit_x=15.2,complete_wheel_edges=True,continue_forward_after_exit=True,business_completion_deadline=None),sysml_contract=module('artifact_sysml').CONTRACT_PROMPT,baseline_settings=dict(stop_time=120,intervals=2400),outputs=OUTPUTS,baseline_libraries=[],baseline_checks=[])
    context['mode']=mode
    if mode=='repair':context['bounded_software_repair']=_failure_feedback('design',index)
    if mode=='revision':
        if not previous_version:raise ValueError('parameter revision requires previous executed version evidence')
        previous=open_service(read_only=True)._artifact_version(previous_version)
        if previous['evidence_status']!='COMPLETE' or previous['task_status']!='FAIL':raise ValueError('parameter revision requires complete unsuccessful task evidence, not slower completion')
        context.update(previous_design=read_json(RUN/'design.json'),previous_execution=previous,revision_contract='At most one justified torque-domain design revision. Preserve baseline_requirements/baseline_design/baseline_sysml_source exactly. Regenerate candidate design and requirements coherently; no geometry/control/source-physics changes or invented hardware rating. Slower completion alone does not justify tuning.')
    return [dict(role='system',content=DESIGN_PROMPT+'\n'+module('artifact_sysml').CONTRACT_PROMPT),dict(role='user',content=json.dumps(context,ensure_ascii=False))],[DESIGN_TOOL]

async def _product_call(rid,messages,tools):
    response_path=RUN/'cli-turns'/(sha_value(rid)+'-response.json')
    if response_path.exists():return read_json(response_path)
    rt=runtime();rt.require_credentials()
    # Runtime owns exact request deduplication and UNKNOWN readback. A saved
    # request ID is never silently replaced to retry a transport failure.
    _journal(rid,'request',dict(messages=messages,tools=tools))
    response=await asyncio.to_thread(rt.complete,messages,tools,rid)
    _journal(rid,'response',response)
    return response

async def design(index=0,mode='initial',previous_version=None):
    if mode not in ('initial','repair','revision','connect-retry'):raise ValueError('design mode must be initial/repair/revision/connect-retry')
    require_time();create();rid='case_design_'+authorization_sha()[:20]+'_'+str(index)
    receipt_path=RUN/('design-'+str(index)+'.json')
    if receipt_path.exists():return read_json(receipt_path)
    if mode=='initial' and index!=0:raise ValueError('initial design index must be zero; failures use the bounded repair entry')
    if (RUN/'design.json').exists() and mode!='revision':return dict(reused=True,design=read_json(RUN/'design.json'),status=status())
    from .case_budget import CaseBudget
    if mode=='initial':CaseBudget().consume('candidate','initial_product_design',dict(stage='initial_product_design'))
    if mode=='repair':CaseBudget().consume('software_repair',rid,dict(stage='design_protocol_repair'))
    if mode=='revision':
        CaseBudget().consume('parameter_revision','product_parameter_revision',dict(stage='bounded_product_design_revision',previous_version_id=previous_version));CaseBudget().consume('candidate','revised_product_design',dict(stage='bounded_product_design_revision'))
    messages,tools=design_request(index,'initial' if mode=='connect-retry' else mode,previous_version)
    if mode=='connect-retry':
        old_rid='case_design_'+authorization_sha()[:20]+'_'+str(index-1)
        if not (RUN/'cli-turns'/(sha_value(rid)+'-response.json')).exists():runtime().authorize_connect_retry(old_rid,rid,messages,tools)
    response=await _product_call(rid,messages,tools)
    packet=_parse_one(response,'case_design_prepare','packet');result=prepare_design(packet,revision=mode=='revision')
    _journal(rid,'tool-result',result)
    receipt=dict(request_id=rid,index=index,mode=mode,design=result,usage=runtime().usage(),proposal_transport='LOCAL_DESIGN_PERSISTENCE_NO_EXECUTION',model_response_id=response.get('response_id'))
    save_json(receipt_path,receipt,immutable=True);return receipt

def generation_context(mode='initial',previous_version=None,trajectory_basis=None):
    if mode not in ('initial','repair','revision','refine','extend'):raise ValueError('unknown generation mode')
    svc=open_service();record=read_json(RUN/'run.json');a=authorization();design=read_json(RUN/'design.json')
    stop=120;tolerance=.01;previous=None
    if previous_version:
        previous=svc._artifact_version(previous_version)
        if mode=='refine':stop=previous['frozen_spec']['settings']['stop_time'];tolerance=.0001
    if mode=='extend':
        if not previous or not trajectory_basis:raise ValueError('extension needs prior version and product-estimated basis JSON')
        if not isinstance(trajectory_basis,dict) or set(trajectory_basis)!={'stop_time','reason'} or not isinstance(trajectory_basis['reason'],str) or not trajectory_basis['reason'].strip():raise ValueError('extension basis must contain stop_time/reason')
        stop=trajectory_basis['stop_time']
    contract=dict(schema_version='v11-model-artifact-1',backend='model-artifact',baseline_ref=svc.baseline_context()['reference'],model_name=a['baseline']['model_name'],sources=[dict(id=s['id']) for s in record['trusted_sources']],requirements=design['candidate_requirements'],design=design['candidate_design'],libraries=[],settings=dict(stop_time=stop,intervals=round(stop/.05)),outputs=OUTPUTS,checks=[],source_annotation=dict(StopTime=stop,Tolerance=tolerance,Interval=.05))
    return dict(selected_baseline=svc.baseline_context(),product_design=design,product_factual_notes=factual_notes(),execution_solver='dassl',trusted_generation_contract=contract,mode=mode,previous_execution=previous,trajectory_basis=trajectory_basis,authorization_sha256=authorization_sha())

async def _mcp_context_call(name,args,*,session=None):
    if session is None:raise ValueError('MCP session required')
    binding=_mcp_timeout();observed=dict(tool=tool_name(name),transport='REAL_STDIO_MCP',rpc_dispatched=None,call_invoked=False,rpc_dispatch_status='NOT_INVOKED',remote_cancellation_proven=None,**binding)
    started=time.monotonic()
    try:
        with phase('MCP_CALL',**observed) as progress:
            observed.update(call_invoked=True,rpc_dispatch_status='CLIENT_CALL_INVOKED_WIRE_SEND_UNPROVEN');progress.update(observed)
            response=await session.call_tool(name,args,read_timeout_seconds=timedelta(seconds=binding['actual_cap_s']))
            observed.update(rpc_dispatched=True,rpc_dispatch_status='RESPONSE_RECEIVED');progress.update(observed)
            if response.isError:raise ValueError('MCP tool error; payload omitted')
            texts=[c.text for c in response.content if getattr(c,'type',None)=='text']
            if len(texts)!=1:raise ValueError('MCP response must contain one JSON text block')
            result=json.loads(texts[0])
            if not result.get('ok'):raise ValueError('MCP response rejected; payload omitted')
            observed['outcome']='RESPONSE_RECEIVED';progress['outcome']='RESPONSE_RECEIVED'
            return result
    except BaseException as exc:
        observed.update(outcome='UNKNOWN_AFTER_CALL_INVOCATION',elapsed_s=time.monotonic()-started)
        attach_calls(exc,[observed]);raise

def _mcp_timeout():
    return timeout_binding(authorization(),remaining(),authority_sha256=authorization_sha(),authority_source='legacy_authorization')

async def _mcp_initialize(session):
    binding=_mcp_timeout();observed=dict(tool='initialize',transport='REAL_STDIO_MCP',rpc_dispatched=None,call_invoked=False,rpc_dispatch_status='NOT_INVOKED',**binding)
    try:
        with phase('MCP_INITIALIZE',**observed) as progress:
            observed.update(call_invoked=True,rpc_dispatch_status='CLIENT_CALL_INVOKED_WIRE_SEND_UNPROVEN');progress.update(observed)
            await session.initialize();progress.update(outcome='RESPONSE_RECEIVED',rpc_dispatched=True,rpc_dispatch_status='RESPONSE_RECEIVED')
    except BaseException as exc:
        observed['outcome']='UNKNOWN_AFTER_CALL_INVOCATION';attach_calls(exc,[observed]);raise

def _mcp_params():
    from mcp import StdioServerParameters
    return StdioServerParameters(command=sys.executable,args=['-X','utf8','-B','-m','week13.src.case_mcp'],cwd=str(REPO),env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))

async def generate(index=0,mode='initial',previous_version=None,trajectory_basis=None):
    require_time();receipt_path=RUN/('generation-'+str(index)+'.json')
    if receipt_path.exists():return read_json(receipt_path)
    if mode=='initial' and index!=0:raise ValueError('initial generation index must be zero; use a bound repair/refine/revision mode')
    effective_mode=mode;feedback=None
    if mode=='repair':
        feedback=_failure_feedback('generation',index);failed=feedback['recorded_failure']
        if failed.get('mode') in ('refine','extend','revision'):
            effective_mode=failed['mode'];previous_version=previous_version or failed.get('previous_version_id');trajectory_basis=trajectory_basis or failed.get('trajectory_basis')
    context=generation_context(effective_mode,previous_version,trajectory_basis)
    if feedback:context['bounded_software_repair']=feedback
    from week11.src.mcp_server import create_mcp
    svc=open_service();available={t.name:t for t in await create_mcp(svc).list_tools()};t=available['proposal_prepare']
    tools=[dict(type='function',function=dict(name=t.name,description=t.description,parameters=t.inputSchema))]
    from copy import deepcopy
    from .case_taskflow import GEN_TOOL
    tools[0]['function']['parameters']=deepcopy(tools[0]['function']['parameters'])
    tools[0]['function']['parameters']['properties']['spec'].update(properties=deepcopy(GEN_TOOL['function']['parameters']['properties']['spec']['properties']))
    messages=[dict(role='system',content=GENERATION_PROMPT+'\n'+module('artifact_sysml').CONTRACT_PROMPT),dict(role='user',content=json.dumps(context,ensure_ascii=False))]
    rid='case_generate_'+authorization_sha()[:20]+'_'+str(index)
    from .case_budget import CaseBudget
    if mode=='repair':CaseBudget().consume('software_repair',rid,dict(mode=mode))
    if mode=='revision':
        if not (RUN/'design-history').exists():raise ValueError('revision generation requires a newly saved product design revision first')
    if mode=='extend':CaseBudget().consume('observation_extension',rid,dict(previous_version_id=previous_version,trajectory_basis=trajectory_basis))
    response=await _product_call(rid,messages,tools);spec=_parse_one(response,'proposal_prepare','spec')
    spec,_=svc.normalize_candidate(spec);delta=check_delta(svc,spec)
    checked=module('model_artifact').validate_artifact(spec,require_sysml=True)
    if not checked['ready']:raise ValueError('product double-source candidate rejected: '+json.dumps(checked['issues'],ensure_ascii=False))
    policy=dict(schema='v12-candidate-policy-1',mode=effective_mode,request_id=rid,spec_digest=checked['spec_digest'],authorization_sha256=authorization_sha(),previous_version_id=previous_version,trajectory_basis=trajectory_basis,model_response_id=response.get('response_id'))
    save_json(RUN/'candidate-policies'/(checked['spec_digest']+'.json'),policy,immutable=True);check_execution_policy(spec)
    # The actual proposal mutation happens through a real stdio MCP session.
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client
    uncertain_marker=RUN/('generation-'+str(index)+'-mcp-unknown.json')
    recovered_proposal_id=None
    if uncertain_marker.exists():
        marker=read_json(uncertain_marker)
        if marker.get('spec_digest')!=checked['spec_digest']:raise ValueError('prior MCP unknown spec binding differs')
        with svc.store.reading() as db:
            matches=db.execute('SELECT proposal_id,spec_json FROM proposals WHERE session_id=? AND project_id=? AND spec_digest=?',(svc.session_id,svc._current_project()['project_id'],checked['spec_digest'])).fetchall()
        matches=[row['proposal_id'] for row in matches if json.loads(row['spec_json'])==spec]
        if len(matches)==1:recovered_proposal_id=matches[0]
        elif not matches:
            proof_path=RUN/('generation-'+str(index)+'-not-committed-proof.json')
            if not proof_path.exists():raise ValueError('prior MCP mutation UNKNOWN and no terminal noncommit proof; no proposal resend')
            proof=read_json(proof_path)
            if proof.get('conclusion')!='NOT_COMMITTED_AFTER_SERVER_EXIT' or proof.get('spec_digest')!=checked['spec_digest'] or proof.get('authorization_sha256')!=authorization_sha() or proof.get('matching_mcp_processes')!=[] or proof.get('proposal_count')!=0 or proof.get('task_count')!=0:raise ValueError('MCP noncommit proof differs; no proposal resend')
        else:raise ValueError('prior MCP mutation matches multiple proposals; readback ambiguous')
    try:
        async with stdio_client(_mcp_params()) as (read,write):
            async with ClientSession(read,write,read_timeout_seconds=timedelta(seconds=_mcp_timeout()['actual_cap_s'])) as session:
                await _mcp_initialize(session)
                result=await _mcp_context_call('proposal_get',dict(proposal_id=recovered_proposal_id),session=session) if recovered_proposal_id else await _mcp_context_call('proposal_prepare',dict(spec=spec),session=session)
                proposal=result['proposal'];readback=await _mcp_context_call('proposal_get',dict(proposal_id=proposal['proposal_id']),session=session)
                _journal(rid,'mcp-tool-result',result);_journal(rid,'mcp-proposal-readback',readback)
    except BaseException as exc:
        if not uncertain_marker.exists():save_json(uncertain_marker,dict(request_id=rid,spec_digest=checked['spec_digest'],status='UNKNOWN_READ_DB_BEFORE_ANY_RESEND',**exception_record(exc)),immutable=True)
        raise
    receipt=dict(request_id=rid,index=index,mode=mode,proposal_id=proposal['proposal_id'],spec_digest=checked['spec_digest'],delta_check=delta,usage=runtime().usage(),real_mcp_proposal_pass=True,proposal_transport='REAL_STDIO_MCP',model_response_id=response.get('response_id'))
    save_json(receipt_path,receipt,immutable=True);return receipt

async def execute(index=0):
    require_time();receipt=read_json(RUN/('generation-'+str(index)+'.json'));svc=open_service();proposal=confirm_approved(svc,receipt['proposal_id'])
    rid='case_execute_'+sha_value(dict(proposal_id=proposal['proposal_id'],authorization_sha256=authorization_sha()))[:40]
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client
    async with stdio_client(_mcp_params()) as (read,write):
        async with ClientSession(read,write,read_timeout_seconds=timedelta(seconds=_mcp_timeout()['actual_cap_s'])) as session:
            await _mcp_initialize(session)
            result=await _mcp_context_call('execution_start',dict(proposal_id=proposal['proposal_id'],request_id=rid),session=session)
            _journal(rid,'mcp-start',result);task_id=result['task']['task_id'];deadline=time.monotonic()+min(authorization()['timeouts_s']['worker']+15,remaining())
            while True:
                current=await _mcp_context_call('execution_status',dict(task_id=task_id),session=session)
                if current['task']['state'] not in ('queued','running','cancel_requested'):break
                if time.monotonic()>=deadline:raise ValueError('execution status UNKNOWN after bounded polling; read DB, never relaunch')
                await asyncio.sleep(.5)
            _journal(rid,'mcp-final-status',current)
    result=dict(task=current,status=status(),real_mcp_execution_status_readback=True)
    save_json(RUN/('execution-'+task_id+'.json'),result,immutable=True);return result

async def recover_proposal(index=0):
    """Resolve a terminated local MCP mutation, retaining its original UNKNOWN."""
    require_time();marker=read_json(RUN/('generation-'+str(index)+'-mcp-unknown.json'))
    import subprocess,sqlite3
    from contextlib import closing
    command="Get-CimInstance Win32_Process -Filter \"Name = 'python.exe'\" -ErrorAction Stop | Where-Object { $_.CommandLine -match 'week13\\.src\\.case_mcp' } | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    options=dict(stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf8',errors='replace',timeout=15)
    if os.name=='nt':options['creationflags']=subprocess.CREATE_NO_WINDOW
    result=subprocess.run(['powershell','-NoProfile','-Command',command],**options)
    if result.returncode or result.stderr.strip():raise ValueError('cannot establish prior MCP process exit; retain UNKNOWN; process-query error: '+result.stderr[:600])
    processes=json.loads(result.stdout) if result.stdout.strip() else []
    if isinstance(processes,dict):processes=[processes]
    if processes:raise ValueError('matching MCP server remains active; no mutation recovery')
    svc=open_service();project=svc._current_project()['project_id']
    with closing(sqlite3.connect(RUN/'projects.sqlite',timeout=5)) as db:
        db.execute('BEGIN EXCLUSIVE')
        proposals=db.execute('SELECT count(*) FROM proposals WHERE project_id=?',(project,)).fetchone()[0]
        tasks=db.execute('SELECT count(*) FROM tasks WHERE project_id=?',(project,)).fetchone()[0]
        db.rollback()
    if proposals or tasks:raise ValueError('project has persisted state; use exact proposal/task readback instead of noncommit recovery')
    from .case_paths import sha_file,utc_now
    proof=dict(schema='v12-mcp-terminal-noncommit-proof-1',conclusion='NOT_COMMITTED_AFTER_SERVER_EXIT',recorded_at_utc=utc_now(),original_rpc_status='UNKNOWN_RETAINED',request_send_status='UNKNOWN_NOT_INFERRED_FROM_DB',authorization_sha256=authorization_sha(),spec_digest=marker['spec_digest'],matching_mcp_processes=processes,process_query_exit_code=result.returncode,exclusive_transaction_succeeded=True,proposal_count=proposals,task_count=tasks,side_effect_basis='proposal_prepare only persists through atomic Store.insert_proposal; execution_start was never reached',store_source_sha256=sha_file(REPO/'week11/src/store.py'),original_cli_context_returned=True,stdio_cleanup_basis='Installed MCP stdio_client finally closes stdin, awaits process exit or terminates its tree before returning; original CLI exited, current process inventory empty',product_http_repeated=False,initialization_fix='Same parser fully initialized on main thread before serving; same candidate insert-disabled RPC validation now passed')
    save_json(RUN/('generation-'+str(index)+'-not-committed-proof.json'),proof,immutable=True)
    return await generate(index=index)

def compare():
    svc=open_service(read_only=True);state=status(svc);latest=state['latest_execution']
    if latest is None:return dict(status=state,comparison=None)
    comparison=svc.versions_compare('v000000',latest['version_id'])['comparison']
    result=dict(status=state,comparison=comparison,scope='IMPORTED_REFERENCE_VERSUS_CURRENT_SIMPLIFIED_MODEL_EXECUTION_NOT_HARDWARE_CERTIFICATION')
    policies=RUN/'candidate-policies';right=comparison['after'];policy=read_json(policies/(right['spec_digest']+'.json'))
    if policy['mode']=='refine':
        from .case_checks import compare_numerics
        previous=svc._artifact_version(policy['previous_version_id'])
        result['numerical_refinement']=compare_numerics(previous['outcome']['mission_assessment'],right['outcome']['mission_assessment'])
    return result

async def ask(question,*,reopen=False):
    require_time()
    if not isinstance(question,str) or not question.strip():raise ValueError('nonempty question required')
    state=compare();before=status();history_path=RUN/'dialogue.json';history=read_json(history_path) if history_path.exists() else []
    # Questions have no tools or write/execute capability. Persistent product
    # answer records retain full assistant protocol data, not user-visible reasoning.
    compact=dict(status=state['status'],numerical_refinement=state.get('numerical_refinement'),comparison=state.get('comparison'),design=read_json(RUN/'design.json') if (RUN/'design.json').exists() else None,product_factual_notes=factual_notes(),authoritative_execution_solver='dassl')
    from .case_contact_review import RULE as CONTACT_REVIEW_RULE
    from .case_console_result import prepare_result
    current=before.get('latest_execution')
    compact['current_contact_review']=(current or {}).get('current_contact_review')
    messages=[dict(role='system',content='用中文解释当前已绑定案例、产品设计和实际证据。只读咨询，不能修改、运行或授予权限。分清历史案例成功/本次最新执行/本次最近达标版（None就是不存在）及实物能力未知；v0仅导入引用且本次未运行。工具、仿真、任务分别报告；失败不覆盖最近有效版。数值复核比较任务结论与事件顺序，时间差只报告，不发明业务时限。新增参数或拓扑超范围只能说明需要集中确认。引用原简化模型限制。'),dict(role='user',content=json.dumps(compact,ensure_ascii=False))]
    messages[0]['content']+='\n'+CONTACT_REVIEW_RULE
    for turn in history:
        if 'assistant_message' in turn:messages.extend([dict(role='user',content='【历史对话，不代表当前接触复核】'+turn['question']),turn['assistant_message']])
    if current:messages.append(dict(role='user',content='【当前只读接触复核，覆盖历史解释】'+json.dumps(current.get('current_contact_review') or dict(status='UNKNOWN',task_status='UNKNOWN'),ensure_ascii=False)))
    messages.append(dict(role='user',content=question))
    rid=('case_reopen_' if reopen else 'case_explain_')+uuid.uuid4().hex
    response=await _product_call(rid,messages,[])
    if response.get('tool_calls') or not response.get('content','').strip():raise ValueError('read-only response must be nonempty text only')
    after=status()
    if after!=before:raise ValueError('read-only dialogue changed execution/project status')
    history.append(dict(question=question,request_id=rid,binding=before,assistant_message=response['assistant_message'],reopen=reopen))
    save_json(history_path,history)
    fixed=prepare_result(dict(execution=current) if current else {})[3][0]
    return dict(system_conclusion=fixed,current_contact_review=(current or {}).get('current_contact_review'),answer=response['content'],binding=before,request_id=rid,reopened=reopen,read_only_status_unchanged=True,usage=runtime().usage())

def _record_failure(action,index,mode,exc,*,previous_version=None,trajectory_basis=None):
    rid=('case_generate_' if action=='generation' else 'case_design_')+authorization_sha()[:20]+'_'+str(index)
    path=RUN/(action+'-'+str(index)+'-failure.json')
    record=dict(action=action,index=index,mode=mode,request_id=rid,previous_version_id=previous_version,trajectory_basis=trajectory_basis,**failure_record(exc))
    if not path.exists():save_json(path,record,immutable=True)
    elif read_json(path)!=record:
        validation_path=RUN/(action+'-'+str(index)+'-validation.json');history=read_json(validation_path) if validation_path.exists() else []
        if record not in history:history.append(record);save_json(validation_path,history)

async def run():
    """One ordinary product entry for the approved complete initial business path."""
    summary_path=RUN/'pipeline-initial.json'
    if summary_path.exists():return dict(reused=True,pipeline=read_json(summary_path),current_status=status())
    stage='design';index=0;mode='initial';previous=None
    try:
        create();designed=await design();stage='generation'
        receipts=[];occupied=[]
        for path in RUN.glob('generation-*'):
            match=re.fullmatch(r'generation-(\d+)(?:\.json|-failure\.json|-mcp-unknown\.json)',path.name)
            if not match:continue
            occupied.append(int(match[1]))
            if path.name=='generation-'+match[1]+'.json':receipts.append((int(match[1]),read_json(path)))
        if receipts:
            index,generated=max(receipts,key=lambda pair:pair[0])
            proposal=open_service().store.proposal(generated['proposal_id'])
            if proposal['spec_digest']!=generated['spec_digest']:raise ValueError('stored generation/proposal binding changed')
            policy=check_execution_policy(proposal['spec']);mode=policy['mode']
        else:
            index=0;mode='initial';generated=await generate()
        stage='execution';executed=await execute(index=index)
        state=status();refined=None
        if mode=='refine':refined=dict(generation=generated,execution=executed,numerics=compare().get('numerical_refinement'))
        elif state['latest_execution']['task_status']=='PASS':
            previous=state['latest_execution']['version_id'];mode='refine';stage='generation'
            for failure_path in RUN.glob('generation-*-failure.json'):
                failure=read_json(failure_path)
                if failure.get('mode')=='refine' and failure.get('previous_version_id')==previous:raise ValueError('prior refinement failed; use its bound repair/readback entry before resuming run')
            index=max(occupied,default=-1)+1
            refined_generation=await generate(index=index,mode='refine',previous_version=previous)
            stage='execution';refined_execution=await execute(index=index)
            refined=dict(generation=refined_generation,execution=refined_execution,numerics=compare().get('numerical_refinement'))
        stage='explanation';explanation=await ask('请根据当前绑定版本解释：这次改型落实了哪些参数，实际编译/仿真/20上20下/完整驶出是否完成，收紧容差结论是否一致，最近达标版本是什么，哪些实物能力和模型限制仍未验证。')
        result=dict(schema='v12-case-pipeline-result-1',design=designed,generation=generated,execution=executed,numerical_refinement=refined,explanation=explanation,status=status(),stage_acceptance='REQUIRES_PRIMARY_REVIEW_NO_AUTOMATIC_S0_S3_PASS')
        save_json(summary_path,result,immutable=True);return result
    except Exception as exc:
        if stage in ('design','generation'):_record_failure(stage,index,mode,exc,previous_version=previous)
        save_json(RUN/'pipeline-initial-error.json',dict(stage=stage,index=index,mode=mode,previous_version_id=previous,**failure_record(exc),status='STOP_AFFECTED_ACTION_NO_BLIND_RETRY'))
        raise

def main(argv=None,*,flow_factory=None,bootstrap_factory=None):
    os.environ['PYTHONDONTWRITEBYTECODE']='1'
    p=argparse.ArgumentParser(description='V12 inherited successful-case constrained modification Agent')
    p.add_argument('action',choices=['run','init','import','design','generate','execute','status','compare','ask','reopen','prepare','recover-request','recover-proposal','request','review-create','plan-request','revalidate-request','revalidate-generation','inspect-saved-generation','execute-request','confirm-request','authorize-request','repair-request','repair-execution','recover-local-proposal','recover-plan-connect','request-plan-recovery-info','request-plan-recover-timeout','request-recover-response','run-request','request-result','request-status','request-ask','request-reopen','request-recover'])
    p.add_argument('--index',type=int,default=0);p.add_argument('--mode',choices=['initial','repair','revision','refine','extend','connect-retry'],default='initial');p.add_argument('--previous-version');p.add_argument('--trajectory-basis');p.add_argument('--question',default='请解释当前设计、真实结果、最近有效版本与继承模型限制。');p.add_argument('--stage',choices=['design','plan','generate'],default='design');p.add_argument('--request-id');p.add_argument('--attempt',type=int)
    p.add_argument('--project-id');p.add_argument('--baseline-version');p.add_argument('--requirement');p.add_argument('--displayed-digest');p.add_argument('--approval-text',help='可信本地批准命令词：确认/批准/同意/CONFIRM');p.add_argument('--http-request-id')
    p.add_argument('--budget-id');p.add_argument('--caps',help='完整有限额度JSON对象，新预算必填');p.add_argument('--hard-deadline-utc');p.add_argument('--reuse-budget',action='store_true');p.add_argument('--planning-only',action='store_true')
    p.add_argument('--timeouts-s', help='本请求完整进程超时JSON；仅新预算可设，沿用既有严格校验')
    p.add_argument('--expected-spec-digest',help='离线生成复验的预期候选摘要；不匹配时禁止写入回执')
    p.add_argument('--expected-baseline-digest',help='离线生成复验的预期完整原基准摘要；不匹配时禁止写入回执')
    p.add_argument('--output-dir',help='只读保存回复复验的新results目录；不写原请求或恢复原预算')
    p.add_argument('--expected-packet-digest',help='离线方案复验的预期原packet摘要；不匹配时禁止写入方案')
    p.add_argument('--approval-basis',help='实际已存在的集中授权记录/范围依据；不产生新授权');p.add_argument('--delegated-confirmation',action='store_true',help='如实记录用户未亲自审阅后生成的plan digest，必须同时给approval-basis')
    p.add_argument('--approval-evidence',help='已存在且绑定原候选与已知参数证据失败的本地修复依据；不授予新范围')
    p.add_argument('--origin-request-id');p.add_argument('--context-root');p.add_argument('--step-id',choices=['explain','reopen','negative_mass','negative_geometry']);p.add_argument('--readonly-steps',help='只读解释/重开两步，或原完整四步验收step到context ID的JSON映射');p.add_argument('--read-timeout',type=int)
    p.add_argument('--concise',action='store_true',help='简洁结果：只读核对已保存结论及内容绑定，不核查解释正文。')
    args=p.parse_args(argv)
    try:
        if args.action in ('request','review-create','plan-request','revalidate-request','revalidate-generation','inspect-saved-generation','execute-request','confirm-request','authorize-request','repair-request','repair-execution','recover-local-proposal','recover-plan-connect','request-plan-recovery-info','request-plan-recover-timeout','request-recover-response','run-request','request-result','request-status','request-ask','request-reopen','request-recover'):
            from .case_taskflow import bootstrap,bootstrap_review,TaskFlow
            if not args.request_id:raise ValueError('new request commands require --request-id')
            if args.action=='review-create':
                if not all((args.origin_request_id,args.baseline_version,args.requirement,args.context_root,args.step_id)):raise ValueError('review-create requires origin-request-id/baseline-version/requirement/context-root/step-id')
                ctx=bootstrap_review(args.request_id,args.origin_request_id,args.baseline_version,args.requirement,args.context_root,args.step_id)
                result=dict(request_id=ctx['request_id'],access_mode=ctx['access_mode'],step_id=ctx['step_id'],origin=ctx['review_binding'],execution_authorized=False)
            elif args.action=='request':
                if not args.project_id or not args.baseline_version or not args.requirement:raise ValueError('request requires --project-id, --baseline-version and original --requirement')
                ctx=(bootstrap_factory or bootstrap)(args.request_id,args.project_id,args.baseline_version,args.requirement)
                result=dict(request_id=ctx['request_id'],project_id=ctx['project_id'],baseline_ref=ctx['baseline_ref'],stage=ctx['stage'],planning_http_authorized=ctx.get('allow_http',False),execution_authorized=False)
            else:
                flow=(flow_factory or TaskFlow.load)(args.request_id)
                if args.action=='authorize-request':
                    from .case_requests import authorize_budget
                    controls=None
                    if args.read_timeout is not None:
                        controls=read_json(ROOT/'configs/case_runtime.json');controls['read_timeout_s']=args.read_timeout
                    result=authorize_budget(args.request_id,args.budget_id,args.approval_text,caps=json.loads(args.caps) if args.caps else None,hard_deadline_utc=args.hard_deadline_utc,reuse=args.reuse_budget,planning_http=True,execution=not (args.planning_only or args.readonly_steps),model_controls=controls,readonly_steps=json.loads(args.readonly_steps) if args.readonly_steps else None,timeouts_s=json.loads(args.timeouts_s) if args.timeouts_s is not None else None)
                elif args.action=='plan-request':result=asyncio.run(flow.bounded('plan',flow.plan))
                elif args.action=='request-plan-recovery-info':result=flow.plan_recovery_info()
                elif args.action=='request-recover-response':result=asyncio.run(flow.recover_response())
                elif args.action=='request-plan-recover-timeout':
                    if not args.approval_evidence:raise ValueError('request-plan-recover-timeout requires a separately user-approved --approval-evidence')
                    result=asyncio.run(flow.recover_plan_read_timeout(args.approval_evidence))
                elif args.action=='recover-plan-connect':
                    if not args.approval_evidence:raise ValueError('recover-plan-connect requires a separately approved, exactly bound --approval-evidence')
                    result=asyncio.run(flow.recover_plan_connect(args.approval_evidence))
                elif args.action=='revalidate-request':result=flow.revalidate_plan(args.attempt if args.attempt is not None else 0,**({'expected_packet_digest':args.expected_packet_digest} if args.expected_packet_digest is not None else {}))
                elif args.action=='revalidate-generation':result=flow.revalidate_generation(args.attempt if args.attempt is not None else 0,**({key:value for key,value in [('expected_spec_digest',args.expected_spec_digest),('expected_baseline_digest',args.expected_baseline_digest)] if value is not None}))
                elif args.action=='inspect-saved-generation':
                    if not args.output_dir or args.attempt is None:raise ValueError('inspect-saved-generation requires explicit --output-dir and --attempt')
                    from .case_saved_generation import inspect_saved_generation
                    inspected=inspect_saved_generation(flow,args.attempt,args.output_dir,expected_spec_digest=args.expected_spec_digest,expected_baseline_digest=args.expected_baseline_digest)
                    result={key:inspected[key] for key in ('status','request_id','request_digest','displayed_digest','spec_digest','baseline_digest','candidate_path','candidate_sha256','old_http_status_preserved','product_http_added','mcp_added','compile_added','simulation_added','execution_authorized')}
                    result['review_path']=str(Path(args.output_dir).resolve()/'review.json')
                elif args.action=='repair-request':result=asyncio.run(flow.repair(args.stage,args.attempt))
                elif args.action=='repair-execution':
                    if not args.approval_evidence:raise ValueError('repair-execution requires --approval-evidence; default run never retries a terminal task')
                    result=asyncio.run(flow.repair_execution(args.approval_evidence))
                elif args.action=='run-request':result=asyncio.run(flow.run())
                elif args.action=='execute-request':
                    if not any((flow.root/name).is_file() for name in ('generation.json','validated-generation-candidate.json')):raise ValueError('execute-request requires an already validated saved candidate; no product generation is started')
                    result=asyncio.run(flow.execute())
                elif args.action=='recover-local-proposal':
                    if not args.approval_evidence:raise ValueError('recover-local-proposal requires existing --approval-evidence')
                    result=asyncio.run(flow.recover_local_proposal(args.approval_evidence))
                elif args.action=='request-result':result=flow.terminal_readback(**({'concise':True} if args.concise else {})) or dict(stage='NO_TERMINAL_EXECUTION',state=flow.state(),product_http_added=0,execution_added=0)
                elif args.action=='confirm-request':result=flow.confirm(args.displayed_digest,args.approval_text,approval_basis=args.approval_basis,human_reviewed_displayed_plan=not args.delegated_confirmation)
                elif args.action=='request-status':result=flow.state()
                elif args.action in ('request-ask','request-reopen'):result=asyncio.run(flow.ask(args.question,reopen=args.action=='request-reopen'))
                else:
                    if not args.http_request_id:raise ValueError('request-recover requires --http-request-id; it never resends HTTP')
                    response=flow._runtime().recover_saved_response(args.http_request_id)
                    result=dict(request_id=args.request_id,http_request_id=args.http_request_id,recovered=True,product_http_added=0)
                    from .case_taskflow import save_recovered_response
                    save_recovered_response(flow,args.http_request_id,response)
        elif args.action in ('init','import'):result=create()
        elif args.action=='run':result=asyncio.run(run())
        elif args.action=='design':result=asyncio.run(design(args.index,args.mode,args.previous_version))
        elif args.action=='generate':result=asyncio.run(generate(args.index,args.mode,args.previous_version,json.loads(args.trajectory_basis) if args.trajectory_basis else None))
        elif args.action=='execute':result=asyncio.run(execute(args.index))
        elif args.action=='recover-proposal':result=asyncio.run(recover_proposal(args.index))
        elif args.action=='status':result=status()
        elif args.action=='compare':result=compare()
        elif args.action in ('ask','reopen'):result=asyncio.run(ask(args.question,reopen=args.action=='reopen'))
        elif args.action=='recover-request':
            if not args.request_id:raise ValueError('recover-request requires original --request-id')
            response=runtime().recover_saved_response(args.request_id);_journal(args.request_id,'response',response)
            result=dict(request_id=args.request_id,recovered=True,product_http_added=0,usage=runtime().usage())
        else:
            result=dict(stage=args.stage,messages=design_request(args.index,args.mode,args.previous_version)[0],tools=design_request(args.index,args.mode,args.previous_version)[1]) if args.stage=='design' else generation_context(args.mode,args.previous_version,json.loads(args.trajectory_basis) if args.trajectory_basis else None)
        print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False));return 0
    except Exception as exc:
        if args.action in ('design','generate'):
            action='generation' if args.action=='generate' else 'design'
            _record_failure(action,args.index,args.mode,exc,previous_version=args.previous_version,trajectory_basis=json.loads(args.trajectory_basis) if args.trajectory_basis else None)
        result=dict(**failure_record(exc),state='STOP_AFFECTED_ACTION_READ_BACK_BEFORE_RETRY')
        if getattr(exc,'transport_failure',None):result['transport_failure']=exc.transport_failure
        print(json.dumps(result,ensure_ascii=False));return 1

if __name__=='__main__':raise SystemExit(main())
