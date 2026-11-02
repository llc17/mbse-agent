"""New project/session and imported reference over inherited project/version storage."""
from copy import deepcopy
import json,math,uuid
from .case_paths import REPO,ROOT,RUN,PACKAGE,authorization,authorization_sha,read_json,save_json,sha_file,sha_value,require_time
from .case_backend import service,module
from .case_scope import modelica_parameters

ORIGINAL=ROOT/'6轮上下楼仿真/运动/6lun_zhenggeliucheng.mo'
OUTPUTS={name:name for name in ('x','z','v','vz','psi','omega_psi','x_n','z_n','x_f','z_f','x_r','z_r','h_n','h_f','h_r','Fz_n','Fz_f','Fz_r')}

def create():
    require_time();a=authorization();path=RUN/'run.json'
    if path.exists():return dict(reopened=True,run=read_json(path),status=status(open_service(read_only=True)))
    if sha_file(ORIGINAL)!=a['baseline']['original_modelica_sha256']:raise ValueError('selected source binding drift')
    raw=ORIGINAL.read_bytes();RUN.mkdir(parents=True,exist_ok=True)
    target=RUN/'baseline-original.mo'
    if target.exists() and target.read_bytes()!=raw:raise ValueError('existing imported source differs')
    target.write_bytes(raw)
    context=dict(schema='v11-artifact-execution-context-1',context_id='case_'+authorization_sha()[:24],status='ACTIVE',mode='LIVE',allow_execution=True,max_tasks=a['caps']['simulation'],worker_timeout_s=a['timeouts_s']['worker'],simulation_timeout_s=a['timeouts_s']['simulation'])
    save_json(RUN/'context.json',context,immutable=True)
    sid='case_'+authorization_sha()[:24];svc=service(RUN/'projects.sqlite',sid)
    if svc.store.current_project(sid) is not None:raise ValueError('partial initialization exists; read back rather than recreate')
    project=svc.project_create('V12 success-case constrained modification')['project']
    case_text='用户指定的历史成功案例：'+ORIGINAL.relative_to(REPO).as_posix()+'；原SHA256='+a['baseline']['original_modelica_sha256']+'。原全程85kg、I_pitch20、150×280mm、20上20下与2m平台。历史简化模型成功；本次尚未运行，非硬件认证。\n'+raw.decode('utf-8-sig')
    approved=(REPO/a['approved_plan']['path']).read_text(encoding='utf-8-sig')
    trusted=[svc.record_source(case_text),svc.record_source(approved)]
    record=dict(schema='v12-success-case-run-1',session_id=sid,project_id=project['project_id'],authorization_sha256=authorization_sha(),context_sha256=sha_file(RUN/'context.json'),original_source_sha256=sha_file(target),trusted_sources=[dict(id=s['source_id'],text=s['text']) for s in trusted],import_status='IMPORTED_REFERENCE_NOT_RUN',history_success_scope='原全程简化模型已有历史成功证据；本次不重跑基准。',requested_change=dict(parameter='m',before=a['baseline']['mass_kg_before'],after=a['baseline']['mass_kg_after'],added_mass_assumption=a['baseline']['added_mass_assumption']),allowed_parameters=dict(T_main_max=a['baseline']['main_torque_range'],T_nose_max=a['baseline']['nose_torque_range']),frozen_parameters='继承原物理、几何、楼梯、接触、控制形式与目标速度。')
    save_json(path,record,immutable=True)
    return dict(run=record,status=status(svc),product_http=0,compile=0,simulation=0)

def open_service(*,read_only=False,with_worker=False):
    from .case_paths import TASK_CONTEXT
    if TASK_CONTEXT is not None:
        from .case_taskflow import TaskFlow
        return TaskFlow.load(TASK_CONTEXT['request_id']).open(read_only=read_only,with_worker=with_worker)
    record=read_json(RUN/'run.json')
    if record['authorization_sha256']!=authorization_sha() or record['context_sha256']!=sha_file(RUN/'context.json') or record['original_source_sha256']!=sha_file(RUN/'baseline-original.mo'):raise ValueError('run/reference/context binding drift')
    svc=service(RUN/'projects.sqlite',record['session_id'],read_only=read_only)
    if svc._current_project()['project_id']!=record['project_id']:raise ValueError('selected project differs')
    if with_worker:
        from .case_worker import CaseWorkerManager
        svc.worker_manager=CaseWorkerManager(svc.store,svc.context)
    return svc

def prepare_design(packet,*,revision=False):
    """Persist product output; never generate a mechanical design in development code."""
    require_time();run=read_json(RUN/'run.json');a=authorization()
    expected={'baseline_requirements','baseline_design','baseline_sysml_source','candidate_requirements','candidate_design','proposed_parameters','rationale','defaults','unknowns'}
    if not isinstance(packet,dict) or set(packet)!=expected:raise ValueError('product design packet fields differ')
    packet=deepcopy(packet);proposed=packet['proposed_parameters']
    if isinstance(proposed,list):
        # A complete typed metadata list is unambiguous. Preserve the original
        # product reply and audit only the data shape; never supply missing values.
        fields={'name','value','unit','origin','basis'};names={'m','I_pitch','T_main_max','T_nose_max'}
        if len(proposed)!=4 or any(not isinstance(row,dict) or set(row)!=fields or row.get('name') not in names or row.get('origin')!='model' or type(row.get('value')) not in (int,float) or not math.isfinite(row['value']) or any(not isinstance(row.get(k),str) or not row[k].strip() for k in ('unit','basis')) for row in proposed) or {row['name'] for row in proposed}!=names:raise ValueError('nonunique/incomplete proposed parameter records cannot be normalized')
        for row in proposed:
            matching=[p for p in packet['candidate_design']['parameters'] if p.get('name')==row['name']]
            if len(matching)!=1 or matching[0]!=row:raise ValueError('proposed parameter metadata does not exactly match candidate design')
        before=sha_value(packet);packet['proposed_parameters']={row['name']:row['value'] for row in proposed}
        save_json(RUN/'design-normalizations'/(before+'.json'),dict(kind='COMPLETE_PARAMETER_METADATA_LIST_TO_NAME_VALUE_MAP',original_packet_sha256=before,normalized_packet_sha256=sha_value(packet),original_records=proposed,normalized_values=packet['proposed_parameters'],source_or_physics_changed=False),immutable=True)
    p=packet['proposed_parameters']
    if not isinstance(p,dict) or set(p)!={'m','I_pitch','T_main_max','T_nose_max'} or any(type(v) not in (int,float) or not math.isfinite(v) for v in p.values()):raise ValueError('invalid product parameter commitment')
    if p['m']!=a['baseline']['mass_kg_after'] or p['I_pitch']!=a['baseline']['pitch_inertia']:raise ValueError('mass/inertia exceeds approved ideal-point assumption')
    for name,key in [('T_main_max','main_torque_range'),('T_nose_max','nose_torque_range')]:
        low,high=a['baseline'][key]
        if not low<=p[name]<=high:raise ValueError('product torque outside approved range')
    if not isinstance(packet['rationale'],str) or not packet['rationale'].strip() or not all(isinstance(packet[k],list) for k in ('defaults','unknowns')):raise ValueError('design explanation/defaults/unknowns missing')
    euler_notes=[line for line in packet['defaults'] if isinstance(line,str) and 'euler' in line.lower()]
    if euler_notes:
        # Keep the previously diagnosed original conflict intact. Later product
        # corrections are independent immutable notes; the word Euler alone is
        # not evidence of a false solver claim.
        explained=all('dassl' in line.lower() and ('注解' in line or 'annotation' in line.lower()) for line in euler_notes)
        save_json(RUN/'design-factual-notes'/(sha_value(packet)+'.json'),dict(status='PRODUCT_EXPLICIT_ANNOTATION_ACTUAL_SOLVER_DISTINCTION' if explained else 'PRODUCT_SOLVER_NOTE_FOR_CONTEXT_REVIEW',packet_sha256=sha_value(packet),original_product_defaults=euler_notes,authoritative_execution_solver='dassl',source_annotation_preserved=True,product_original_text_modified=False),immutable=True)
    source=(RUN/'baseline-original.mo').read_bytes().decode('utf-8-sig')
    baseline=dict(schema_version='v11-model-artifact-1',backend='model-artifact',artifact=dict(modelica_source=source,model_name=a['baseline']['model_name'],sysml_source=packet['baseline_sysml_source']),sources=run['trusted_sources'],requirements=packet['baseline_requirements'],design=packet['baseline_design'],libraries=[],settings=dict(stop_time=120,intervals=2400),outputs=OUTPUTS,checks=[])
    normalized,_=module('artifact_sysml').normalize_package_wrapper(baseline['artifact']['sysml_source']);baseline['artifact']['sysml_source']=normalized
    checked=module('model_artifact').validate_artifact(baseline,require_sysml=True)
    if not checked['ready']:raise ValueError('product baseline SysML/metadata rejected: '+json.dumps(checked['issues'],ensure_ascii=False))
    # Source-bound candidate metadata is validated using an unexecuted temporary
    # schema probe. Its Modelica is not generated or modified by this helper.
    declared={s['id']:s['text'] for s in run['trusted_sources']}
    for row in packet['candidate_requirements']:
        if not isinstance(row,dict) or set(row)!={'text','source_id','source_text'} or row['source_id'] not in declared or not row['source_text'] or row['source_text'] not in declared[row['source_id']]:raise ValueError('candidate requirement lacks trusted exact source')
    if not packet['candidate_requirements']:raise ValueError('candidate requirements missing')
    design=packet['candidate_design']
    if not isinstance(design,dict) or set(design)!={'summary','assumptions','parameters'} or not isinstance(design['summary'],str) or not design['summary'].strip():raise ValueError('candidate design missing')
    for name in p:
        matches=[x for x in design['parameters'] if x.get('name')==name]
        if len(matches)!=1 or matches[0].get('value')!=p[name]:raise ValueError('candidate design omits parameter commitment: '+name)
    manifest=dict(schema='v12-imported-reference-manifest-1',record_kind='IMPORTED_REFERENCE',project_id=run['project_id'],source_sha256=run['original_source_sha256'],spec_digest=checked['spec_digest'],original_path=ORIGINAL.relative_to(REPO).as_posix(),task_status='NOT_RUN',historical_success_scope=run['history_success_scope'],sysml_origin='PRODUCT_MODEL_GENERATED_NOW_SYNTAX_AND_SHARED_EXPRESSIONS_ONLY')
    save_json(RUN/'imported-manifest.json',manifest,immutable=True)
    imported=dict(record_kind='IMPORTED_REFERENCE',project_id=run['project_id'],task_id='reference_'+run['original_source_sha256'][:24],version_id='v000000',spec_digest=checked['spec_digest'],source_sha256=checked['source_sha256'],manifest_sha256=sha_file(RUN/'imported-manifest.json'),frozen_spec=baseline,evidence_status='IMPORTED_REFERENCE',state='not_run',tool_status='NOT_RUN',chain_status='NOT_RUN',task_status='NOT_RUN',vehicle_task_status='UNKNOWN',physical_acceptance_status='NOT_VERIFIED',recorded_conclusions_verified=False,published=False,read_only=True,source_files=dict(Modelica=str(RUN/'baseline-original.mo'),SysML=str(RUN/'baseline-generated.sysml')),data_files=dict(CSV=None),outcome=dict(historical_success_scope=run['history_success_scope']))
    save_json(RUN/'imported-reference.json',imported,immutable=True)
    (RUN/'baseline-generated.sysml').write_text(normalized,encoding='utf8',newline='\n')
    if revision:
        previous=read_json(RUN/'design.json')
        for key in ('baseline_requirements','baseline_design','baseline_sysml_source'):
            if packet[key]!=previous[key]:raise ValueError('parameter design revision changed imported baseline metadata/SysML')
        save_json(RUN/'design-history'/(sha_value(previous)+'.json'),previous,immutable=True)
        save_json(RUN/'design.json',packet)
    else:save_json(RUN/'design.json',packet,immutable=True)
    syntax,consistency=module('artifact_sysml').inspect_dual_source(baseline)
    save_json(RUN/'baseline-sysml-check.json',dict(syntax=syntax,consistency=consistency),immutable=True)
    svc=open_service();ref=module('artifact_baseline').reference(imported)
    current=module('artifact_baseline').selection(svc)
    if current!=ref:svc.store.select_artifact_baseline(svc.session_id,run['project_id'],ref)
    return dict(design=packet,baseline_reference=ref,baseline_status='IMPORTED_REFERENCE_NOT_RUN',baseline_sysml_status=consistency['status'])

def confirm_approved(svc,proposal_id):
    require_time();proposal=svc.store.proposal(proposal_id)
    from .case_scope import check_delta
    from .case_session import check_execution_policy
    evidence=check_delta(svc,proposal['spec']);check_execution_policy(proposal['spec'])
    svc.confirm_local(proposal_id,proposal['displayed_digest'])
    with svc.store.transaction() as db:
        row=db.execute('SELECT confirmation_json FROM proposals WHERE proposal_id=?',(proposal_id,)).fetchone();value=json.loads(row[0])
        value.update(channel='CURRENT_USER_APPROVED_CONSTRAINED_DESIGN_DELEGATION',source_reviewed_by_human=False,human_new_approval=False,approval_verbatim=authorization()['approval_quote'],authorization_sha256=authorization_sha(),approved_plan=authorization()['approved_plan'],actual_delta_check=evidence)
        db.execute('UPDATE proposals SET confirmation_json=? WHERE proposal_id=?',(json.dumps(value,ensure_ascii=False),proposal_id))
    return svc.store.proposal(proposal_id)

def check_execution_policy(spec):
    from .case_scope import strip_comments,NUMBER
    import re
    digest=module('model_artifact').validate_artifact(spec)['spec_digest'];policy=read_json(RUN/'candidate-policies'/(digest+'.json'))
    if policy['spec_digest']!=digest or policy['authorization_sha256']!=authorization_sha():raise ValueError('candidate execution policy binding differs')
    tolerance=float(re.findall(r'\bTolerance\s*=\s*('+NUMBER+')',strip_comments(spec['artifact']['modelica_source']))[0])
    mode=policy['mode'];stop=spec['settings']['stop_time']
    if mode in ('initial','repair','revision') and (stop!=120 or tolerance!=.01):raise ValueError('ordinary candidate must use original 120s/tol0.01 observation')
    if mode in ('refine','extend'):
        svc=open_service(read_only=True);previous=svc._artifact_version(policy['previous_version_id'])
        if previous['evidence_status']!='COMPLETE' or previous['chain_status']!='PASS':raise ValueError('refine/extend lacks complete prior runtime evidence')
        old=previous['frozen_spec'];check=previous['outcome'].get('mission_assessment',{})
        if mode=='refine' and (previous['task_status']!='PASS' or tolerance!=1e-4 or stop!=old['settings']['stop_time']):raise ValueError('numerical refinement requires achieved prior candidate, same horizon and 1e-4')
        if mode=='extend':
            if previous['task_status']!='FAIL' or check.get('validity_status')!='PASS' or check.get('completion_status')!='OBSERVATION_INTERVAL_INCOMPLETE' or not old['settings']['stop_time']<stop<=600 or tolerance!=.01:raise ValueError('extension lacks valid incomplete forward trajectory')
            if not policy.get('trajectory_basis'):raise ValueError('extension requires product trajectory estimate/basis')
        for key in ('design','requirements','outputs','checks','sources','libraries'):
            if spec[key]!=old[key]:raise ValueError('observation/numerical-only run changes '+key)
        oldvalues=modelica_parameters(old['artifact']['modelica_source'])[0];newvalues=modelica_parameters(spec['artifact']['modelica_source'])[0]
        if oldvalues!=newvalues:raise ValueError('observation/numerical-only run changes parameters')
    elif mode not in ('initial','repair','revision'):raise ValueError('unknown execution mode')
    return policy

def status(svc=None):
    svc=svc or open_service(read_only=True);items=svc.versions_list()['versions']['items']
    from .case_contact_review import current_summary,with_review
    brief=current_summary
    passes=[x for x in items if x['evidence_status']=='COMPLETE' and x['chain_status']=='PASS' and x['task_status']=='PASS']
    reviewed={}
    for row in ([items[-1]] if items else [])+([passes[-1]] if passes else []):
        if row['version_id'] not in reviewed:reviewed[row['version_id']]=with_review(row,svc.project_root)
    candidate=brief(reviewed[passes[-1]['version_id']]) if passes else None
    accepted=candidate if candidate and candidate['task_status']=='PASS' and candidate['validity_status']=='PASS' else None
    historical={k:passes[-1].get(k) for k in ('version_id','task_id','task_status','chain_status')} if passes else None
    if historical:historical['role']='HISTORICAL_ROUTE_RECORD_NOT_CURRENT_CONTACT_REVIEW'
    return dict(project=svc._current_project(),imported_reference_status='IMPORTED_REFERENCE_NOT_RUN',historical_case_success=read_json(RUN/'run.json')['history_success_scope'],latest_execution=brief(reviewed[items[-1]['version_id']]) if items else None,last_task_pass_snapshot=accepted,historical_last_task_pass_record=historical,versions=[current_summary(reviewed.get(x['version_id'],x),include_review=False) for x in items],current_product_design_saved=(RUN/'design.json').is_file(),execution_scope='INHERITED_EQUATION_DIAGNOSTIC_ONLY',physical_status='UNKNOWN')
