"""Local daily startup; reuse one named run and its finite V10 ledger."""
import json,re,sqlite3,uuid,hashlib
from datetime import datetime,timezone
from .contracts import WEEK12_ROOT,inside_week12
from .model_artifact import save
from .artifact_runtime import ArtifactRuntime

CAPS=dict(max_http_requests=2,max_total_tokens=16000,max_usd=.02,max_actual_tasks=1,worker_timeout_s=300,simulation_timeout_s=120)
THINKING_CAPS=dict(CAPS,max_total_tokens=2000000,max_usd=.10,max_input_tokens=64000,max_output_tokens=16384,http_timeout_s=120)
INTERACTIVE_PROFILE='interactive-20261013'
INTERACTIVE_CAPS=dict(CAPS,max_http_requests=12,max_total_tokens=10000000,max_usd=2.,max_input_tokens=128000,max_output_tokens=65536,http_timeout_s=60,http_read_timeout_s=600)
def now():return datetime.now(timezone.utc).isoformat()
def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def run_path(name):
    if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',name):raise ValueError('run名称仅支持字母、数字、下划线和短横线，最多64字符')
    return inside_week12(WEEK12_ROOT/'runs'/name)
def ledger_state(path):
    if not path.exists():return dict(frozen=False,requests=0,tokens=0,cost=0)
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        row=db.execute('SELECT count(*),coalesce(sum(coalesce(actual_tokens,reserve_tokens)),0),coalesce(sum(coalesce(actual_cost,reserve_cost)),0) FROM requests').fetchone()
        frozen=db.execute("SELECT value FROM meta WHERE key='frozen'").fetchone()
    return dict(frozen=bool(frozen),requests=row[0],tokens=row[1],cost=row[2])

def transport_recovery_ready(path):
    """Startup allowance for one bound request or its exact succeeded cache."""
    from contextlib import closing
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        if db.execute("SELECT 1 FROM meta WHERE key='source_correction'").fetchone():
            return ArtifactRuntime(path.parent/'runtime.json',path).source_correction_resume_ready()
        state=db.execute("SELECT value FROM meta WHERE key='generation_length_recovery_state'").fetchone()
        bound=db.execute("SELECT value FROM meta WHERE key='generation_length_recovery'").fetchone()
        if bound and state==('submitted',):
            return ArtifactRuntime(path.parent/'runtime.json',path).generation_cached_resume_ready()
        if not bound:
            state=db.execute("SELECT value FROM meta WHERE key='transport_recovery_state'").fetchone()
            bound=db.execute("SELECT value FROM meta WHERE key='transport_recovery_authorization'").fetchone()
        if not bound:
            state=db.execute("SELECT value FROM meta WHERE key='design_length_recovery_state'").fetchone()
            bound=db.execute("SELECT value FROM meta WHERE key='design_length_recovery'").fetchone()
        if state!=('pending',) or not bound:return False
        record=json.loads(bound[0])
        return not db.execute('SELECT 1 FROM requests WHERE request_id=?',(record['new_request_id'],)).fetchone()
def prepare_run(name,*,baseline_db=None,baseline_session=None,runtime_profile=None):
    root=run_path(name);manifest=root/'run.json'
    if manifest.exists():
        value=json.loads(manifest.read_text(encoding='utf8'))
        if baseline_db or baseline_session:raise ValueError('已有run不允许换基线；请使用该run原DB和账本')
        if runtime_profile is not None and runtime_profile!=value.get('runtime_profile','daily'):raise ValueError('已有run不允许改变额度或思考配置')
        return value
    profile=runtime_profile or 'daily'
    if profile not in ('daily','thinking-task-20261012',INTERACTIVE_PROFILE):raise ValueError('unknown runtime profile')
    if bool(baseline_db)!=bool(baseline_session):raise ValueError('基线必须同时显式提供DB与session')
    if root.exists():raise ValueError('run目录已存在但缺少manifest；拒绝覆盖，请使用新的run名称')
    root.mkdir(parents=True,exist_ok=True)
    config=json.loads((WEEK12_ROOT/'configs/car_domain_runtime.json').read_text(encoding='utf-8-sig'))
    config.update(max_requests=2,max_total_tokens=16000,max_usd=.02,max_tokens=2048,max_input_tokens=13952,timeout_s=60,automatic_retries=0)
    caps=dict(CAPS)
    if profile=='thinking-task-20261012':
        config.update(thinking='enabled',budget_profile=profile,max_total_tokens=2000000,max_usd=.10,max_tokens=16384,max_input_tokens=64000,timeout_s=120)
        caps=dict(THINKING_CAPS)
    elif profile==INTERACTIVE_PROFILE:
        config.update(thinking='enabled',budget_profile=profile,max_requests=12,max_total_tokens=10000000,max_usd=2.,max_tokens=65536,max_input_tokens=128000,read_timeout_s=600)
        caps=dict(INTERACTIVE_CAPS)
    save(root/'runtime.json',config)
    context=dict(schema='v11-artifact-execution-context-1',context_id='daily_'+uuid.uuid4().hex,status='ACTIVE',mode='LIVE',allow_execution=False,max_tasks=1,worker_timeout_s=300,simulation_timeout_s=120)
    save(root/'context.json',context)
    baseline=None
    if baseline_db:
        source=inside_week12(baseline_db)
        if not source.is_file():raise ValueError('基线DB不存在')
        with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as db:
            if db.execute('SELECT 1 FROM sessions WHERE session_id=?',(baseline_session,)).fetchone() is None:raise ValueError('基线session不存在')
        baseline=dict(db=str(source),session=baseline_session,sha256=digest(source))
    value=dict(schema='v11-artifact-daily-run-1',name=name,status='PREPARED',mode=None,created_at=now(),runtime_profile=profile,caps=caps,session=baseline_session or 'daily',baseline=baseline,runtime_config_sha256=digest(root/'runtime.json'),context_sha256=digest(root/'context.json'))
    save(manifest,value);return value
def configure_run(args,*,input_fn=input,test_only=False,authorization_channel='trusted_local_cli'):
    """No Runtime/API or execution permission before an explicit local quota action."""
    value=prepare_run(args.run,baseline_db=args.baseline_db,baseline_session=args.baseline_session,runtime_profile=getattr(args,'runtime_profile',None));root=run_path(args.run)
    print('本run有限额度：'+json.dumps(value['caps'],ensure_ascii=False)+'；具体方案仍须本地确认，之后自动生成和执行。',flush=True)
    if args.prepare:return dict(prepared=True,manifest=str(root/'run.json'),activated=False)
    if digest(root/'runtime.json')!=value['runtime_config_sha256'] or digest(root/'context.json')!=value['context_sha256']:raise ValueError('run配置摘要已改变；拒绝激活或重置账本')
    if args.read_only:
        if not (root/'projects.sqlite').exists():raise ValueError('run尚无DB，可使用 --prepare 或 --offline')
    elif value['status']=='PREPARED':
        offline=bool(args.offline)
        if not offline:
            if input_fn('允许本run上述有限费用和执行额度请输入 ALLOW '+args.run+': ').strip()!='ALLOW '+args.run:
                return dict(prepared=True,activated=False,reason='LOCAL_QUOTA_DECLINED')
        context=json.loads((root/'context.json').read_text(encoding='utf8'))
        context.update(mode='TEST_ONLY' if test_only or offline else 'LIVE',allow_execution=not (test_only or offline))
        save(root/'context.json',context)
        value.update(mode='TEST_ONLY' if test_only else ('OFFLINE' if offline else 'LIVE'),status='ACTIVATING',activated_at=now(),quota_confirmation=dict(channel=authorization_channel if not test_only else 'TEST_ONLY',human_new_model_approval=False,offline=offline),context_sha256=digest(root/'context.json'))
        # Persist activation before any database/runtime creation; failures cannot renew it.
        save(root/'run.json',value)
        if value['baseline']:
            src=inside_week12(value['baseline']['db'])
            if digest(src)!=value['baseline']['sha256']:raise ValueError('基线DB已变；未复制')
            with sqlite3.connect(src.as_uri()+'?mode=ro',uri=True) as old,sqlite3.connect(root/'projects.sqlite') as new:
                old.backup(new)
                new.execute('DELETE FROM artifact_baseline_selections')
                new.execute('UPDATE projects SET selected_version_id=current_version_id')
        if value['mode']=='LIVE':ArtifactRuntime(root/'runtime.json',root/'runtime.sqlite')
        from .artifact_service import ArtifactStore
        store=ArtifactStore(root/'projects.sqlite');store.ensure_session(value['session'])
        value['status']='ACTIVE';save(root/'run.json',value)
    elif value['status']!='ACTIVE':raise ValueError('closed run不重开；可加 --read-only 查询')
    elif bool(args.offline)!=(value['mode'] in ('OFFLINE','TEST_ONLY')):raise ValueError('同run不允许切换LIVE/offline；原账本和权限保持')
    if not args.read_only:
        if not (root/'projects.sqlite').is_file():raise ValueError('ACTIVE run原DB缺失；拒绝补建或重置')
        if value['mode']=='LIVE':
            if not (root/'runtime.sqlite').is_file():raise ValueError('ACTIVE run原账本缺失；拒绝补建或重置')
            state=ledger_state(root/'runtime.sqlite')
            caps=value['caps']
            exhausted=state['requests']>=caps['max_http_requests'] or state['tokens']>=caps['max_total_tokens'] or state['cost']>=caps['max_usd']
            from .artifact_continuous import resumable_generation
            if state['frozen']:
                if not transport_recovery_ready(root/'runtime.sqlite'):
                    runtime=ArtifactRuntime(root/'runtime.json',root/'runtime.sqlite')
                    pointer_path=root/'cli-continuations'/('current-'+hashlib.sha256(value['session'].encode()).hexdigest()+'.json')
                    pointer=json.loads(pointer_path.read_text(encoding='utf8')) if pointer_path.is_file() else {}
                    if value.get('runtime_profile')==INTERACTIVE_PROFILE and pointer.get('design_id') and not runtime._generation_length_recovery:
                        try:runtime.prepare_known_generation_length_recovery(root/'run.json',authorization_text='Existing ALLOW and approved design bounded implementation recovery; one explicit low effort generation, thinking and caps unchanged')
                        except (RuntimeError,OSError,ValueError,TypeError,KeyError) as exc:raise ValueError('run账本冻结；已确认生成恢复未准入，仅只读核对。原因：'+str(exc)) from None
                    elif value.get('runtime_profile')==INTERACTIVE_PROFILE and not runtime._design_length_recovery:
                        try:runtime.prepare_known_design_length_recovery(root/'run.json',authorization_text='Existing run quota ALLOW and bounded implementation recovery; no physical approval or cap increase',idle_process_evidence=None)
                        except (RuntimeError,OSError,ValueError,TypeError,KeyError) as exc:raise ValueError('run账本冻结；未取得可恢复的已知方案截断证据或原MCP仍占用，请先退出旧CLI；仅只读核对。原因：'+str(exc)) from None
                    else:raise ValueError('run账本冻结；仅 --read-only 查询，不创建新账本')
                ArtifactRuntime(root/'runtime.json',root/'runtime.sqlite')
                print('已留证同账一次恢复；旧失败及实际用量/未知预留保持。输入 /retry 接续对应阶段的指定请求；已确认方案沿原批准生成执行。若再次失败停止。读取等待600秒不是整调用时限。',flush=True)
            if exhausted and not (not getattr(args,'advanced_workflow',False) and resumable_generation(root,value['session'])):
                # Exhaustion grants no requests. A completed task can be observed
                # using this same ordinary startup command, with read-only tools.
                with sqlite3.connect((root/'projects.sqlite').as_uri()+'?mode=ro',uri=True) as db:count=db.execute('SELECT count(*) FROM tasks').fetchone()[0]
                if count and not getattr(args,'advanced_workflow',False):
                    args.read_only=True;print('模型额度已用完，已有任务自动进入只读结果查询。',flush=True)
                else:raise ValueError('run额度耗尽且无确定生成可接续；仅 --read-only 查询，不创建新账本')
        if (root/'projects.sqlite').exists():
            with sqlite3.connect((root/'projects.sqlite').as_uri()+'?mode=ro',uri=True) as db:
                rows=db.execute('SELECT p.display_json FROM tasks t JOIN proposals p ON t.proposal_id=p.proposal_id').fetchall()
            context=json.loads((root/'context.json').read_text(encoding='utf8'))
            if sum(json.loads(r[0]).get('execution_context_id')==context['context_id'] for r in rows)>=context['max_tasks']:
                if not getattr(args,'advanced_workflow',False):args.read_only=True;print('当前任务额度已用，自动进入只读结果查询。',flush=True)
                else:raise ValueError('run任务额度已用；仅 --read-only 查询')
    args.db=root/'projects.sqlite';args.session=value['session'];args.debug_offline=args.read_only or value['mode']!='LIVE'
    args.execution_context=None if args.read_only else root/'context.json'
    args.runtime_config=None if args.debug_offline else root/'runtime.json';args.runtime_ledger=None if args.debug_offline else root/'runtime.sqlite'
    return None
