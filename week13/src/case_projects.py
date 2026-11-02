"""Local case catalog and immutable original-input imports; no execution authority.

Only an explicitly submitted directory is inspected. A catalog entry and a user
success statement are provenance, never an execution result or a budget grant.
"""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import uuid
from xml.etree import ElementTree
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
LEGACY_ROOT = ROOT / 'runs/success-case-20261019-01'
# Recognition is pinned to the pre-release public bytes, not a mutable entry.
REFERENCE_MODEL = ROOT / 'src/inherited/reference-full-mission-legacy.mo'
# Legacy replay is the 2026-10-19 a9... snapshot, distinct from the 04db...
# pre-release public variant. This directory contains only that original model.
LEGACY_REPLAY_INPUT = ROOT / 'src/inherited/legacy-six-wheel-input'
SUFFIXES = {'.mo','.sysml','.docx','.txt','.md','.pdf','.png','.jpg','.jpeg'}
SKIP = {'.git','.codex','.agents','v12_old','__pycache__','node_modules','v8_holdout_private','v8_formal_private'}
MAX_FILES, MAX_BYTES, MAX_TOTAL, MAX_XML = 100, 32*1024*1024, 128*1024*1024, 2*1024*1024
OUTPUT_NAMES = ('x','z','v','vz','psi','omega_psi','x_n','z_n','x_f','z_f','x_r','z_r','h_n','h_f','h_r','Fz_n','Fz_f','Fz_r')


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _digest(value):
    from .case_requests import digest
    return digest(value)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write(path, value):
    from .case_paths import save_json
    return save_json(path, value, immutable=True)


def _catalog():
    return ROOT/'runs/case-catalog'


def _local(path):
    path=Path(path).resolve()
    if not path.is_relative_to((ROOT/'runs').resolve()) or path.is_relative_to((ROOT/'V12_old').resolve()):
        raise ValueError('case project must stay within current week13/runs')
    return path


def _safe_submitted(path):
    # Check names before resolving or accessing a submitted directory. No banned
    # path is enumerated, hashed or probed as part of input validation.
    path=Path(path).absolute()
    lowered=str(path).replace('\\','/').lower()
    if any(part.lower() in SKIP or part.lower().startswith('.env') for part in path.parts) or any(token in lowered for token in ('v8_holdout_private','v8_formal_private','week9/experiments/cases.py')):
        raise ValueError('forbidden original-input path')
    if path==Path(path.anchor) or path in (ROOT.absolute(),ROOT.parent.absolute()) or ROOT.parent.absolute().is_relative_to(path):raise ValueError('submitted directory is too broad; select the concrete original case directory')
    for parent in (path,*path.parents):
        if parent.is_symlink() or getattr(parent,'is_junction',lambda:False)():
            raise ValueError('symbolic links/junctions are not accepted as original inputs')
    if not path.is_dir():raise ValueError('explicit existing case directory required')
    return path.resolve()


def _files(path):
    root=_safe_submitted(path);found=[];excluded=[];total=0;visited=0
    def walk(directory,depth):
        nonlocal total,visited
        if depth>5:raise ValueError('original directory nesting exceeds finite import limit')
        with os.scandir(directory) as iterator:
            entries=sorted(iterator,key=lambda x:x.name.casefold())
        visited+=len(entries)
        if visited>500:raise ValueError('too many directory entries in submitted input')
        for entry in entries:
            child=Path(entry.path);relative=child.relative_to(root).as_posix();name=entry.name.casefold()
            if name in SKIP or name.startswith(('.env','~$','.~')) or name.endswith(('.lock','.lck')):
                excluded.append(dict(path=relative,reason='EXCLUDED_NAME'));continue
            if entry.is_symlink() or getattr(child,'is_junction',lambda:False)():
                excluded.append(dict(path=relative,reason='SYMLINK_OR_JUNCTION'));continue
            resolved=child.resolve()
            if not resolved.is_relative_to(root):raise ValueError('original path escapes submitted directory')
            if entry.is_dir(follow_symlinks=False):walk(child,depth+1);continue
            if not entry.is_file(follow_symlinks=False) or child.suffix.lower() not in SUFFIXES:
                excluded.append(dict(path=relative,reason='NOT_ALLOWLISTED'));continue
            size=entry.stat(follow_symlinks=False).st_size
            if size>MAX_BYTES:raise ValueError('original file exceeds finite size limit: '+relative)
            total+=size
            if len(found)>=MAX_FILES or total>MAX_TOTAL:raise ValueError('original import exceeds finite aggregate limit')
            found.append(dict(path=relative,bytes=size,sha256=_sha(child),kind=child.suffix.lower()[1:]))
    walk(root,0)
    if not found:raise ValueError('no allowlisted case input files')
    return root,found,excluded


def _docx(path):
    with ZipFile(path) as archive:
        if len(archive.infolist())>10000:raise ValueError('DOCX archive exceeds finite member count')
        info=archive.getinfo('word/document.xml')
        if info.file_size>MAX_XML or info.compress_size>MAX_BYTES:raise ValueError('DOCX XML exceeds import limit')
        raw=archive.read(info)
        if len(raw)>MAX_XML or b'<!DOCTYPE' in raw or b'<!ENTITY' in raw:raise ValueError('unsupported DOCX XML')
        document=ElementTree.fromstring(raw)
        text='\n'.join(''.join(node.itertext()) for node in document.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p'))
        has_images=any(name.startswith('word/media/') for name in archive.namelist())
    return dict(text=text,embedded_images='NOT_AUTOMATICALLY_INTERPRETED' if has_images else 'NONE',ocr_performed=False)


def support_for_source(source):
    """Conservative inherited-equation family, not parameter-name detection."""
    from .case_asym2_family import support_for_source as asym2_support
    asym2=asym2_support(source)
    if asym2 is not None:return asym2
    from .case_contact_family import support_for_reviewed_source
    reviewed=support_for_reviewed_source(source)
    if reviewed is not None:return reviewed
    from .case_scope import FAMILY_VARIABLE_PARAMETERS, model_shape, modelica_parameters
    try:
        before=REFERENCE_MODEL.read_text(encoding='utf-8-sig')
        if model_shape(source,FAMILY_VARIABLE_PARAMETERS)!=model_shape(before,FAMILY_VARIABLE_PARAMETERS):
            return dict(status='UNSUPPORTED',family=None,reasons=['Modelica方程、车辆几何、楼梯依赖公式或控制不属于当前全程继承家族；可管理资料，不能套用既有检查器。'])
        parameters=modelica_parameters(source)[0]
        for key in FAMILY_VARIABLE_PARAMETERS:
            if not math.isfinite(parameters[key]) or parameters[key]<=0:raise ValueError('family parameter must be positive and finite: '+key)
        from .case_scope import NUMBER,strip_comments
        for key,allowed in [('StopTime',(120.0,)),('Interval',(.05,)),('Tolerance',(.01,.0001))]:
            values=re.findall(r'\b'+key+r'\s*=\s*('+NUMBER+')',strip_comments(source))
            if len(values)!=1 or float(values[0]) not in allowed:raise ValueError('UNSUPPORTED_OBSERVATION_SETTING: '+key+' differs from the tested family execution settings')
        return dict(status='SUPPORTED',family='inherited-three-contact-full-route',scope='可管理原继承模型，并在另有授权时诊断原方程/路线；允许的点载/等效扭矩或独立楼梯高度/深度改型仍受原源码范围锁约束。接触模型尚未完整修复，诊断不能验收物理越障能力；当前几何复核优先于历史PASS。',parameters=parameters,checker='case_checks',execution_authorized=False,physical_acceptance='NOT_VERIFIED',execution_scope='INHERITED_EQUATION_DIAGNOSTIC_ONLY',physical_model_status='CONTACT_MODEL_NOT_REPAIRED')
    except (ValueError,KeyError) as exc:
        return dict(status='UNSUPPORTED',family=None,reasons=[str(exc)])


def _legacy():
    path=LEGACY_ROOT/'run.json'
    if not path.is_file():return None
    run=_read(path)
    return dict(case_id='legacy-six-wheel',name='六轮上下楼（既有项目）',project_id=run['project_id'],project_root=str(LEGACY_ROOT.resolve()),db_path=str((LEGACY_ROOT/'projects.sqlite').resolve()),session_id=run['session_id'],kind='LEGACY_READ_ONLY_CATALOG',origin_case_id=None,status='EXISTING_PROJECT',original_input_root=str(LEGACY_REPLAY_INPUT.resolve()),selected_model='运动/6lun_zhenggeliucheng.mo',original_model_sha256=run.get('original_source_sha256'),historical_success_statement=run.get('history_success_scope',''),catalog_read_only=True)


def list_cases():
    cases=[];legacy=_legacy()
    if legacy:cases.append(legacy)
    for path in sorted(_catalog().glob('*.json')):
        item=_read(path);root=_local(item['project_root'])
        if item.get('schema')!='v12-case-catalog-entry-1' or path.stem!=item.get('case_id') or root.name!=item['case_id'] or _sha(root/'case.json')!=item.get('case_record_sha256'):
            raise ValueError('case catalog identity/source drift')
        case=_read(root/'case.json')
        if any(case.get(k)!=item.get(k) for k in ('case_id','project_id','project_root','db_path')):raise ValueError('case catalog/project binding drift')
        cases.append(case)
    return cases


def resolve_case(case_id):
    matches=[case for case in list_cases() if case_id in (case['case_id'],case['name'],case['project_id'])]
    if len(matches)!=1:raise ValueError('case is missing or ambiguous; use stable case_id')
    return matches[0]


def verify_case(case):
    """Verify immutable stored originals; user originals may later be moved."""
    if case['kind']=='LEGACY_READ_ONLY_CATALOG':return case
    root=_local(case['project_root']);actual=_read(root/'case.json')
    if actual!=case:raise ValueError('case record drift')
    run=_read(root/'run.json')
    if any(run.get(key)!=case[key] for key in ('case_id','project_id','session_id')):raise ValueError('case run/session binding drift')
    manifest=_read(root/'original-manifest.json')
    if _sha(root/'original-manifest.json')!=case['original_manifest_sha256']:raise ValueError('original manifest drift')
    original_root=_safe_submitted(root/'original')
    for item in manifest['files']:
        relative=item['path']
        if not isinstance(relative,str) or not relative or '\\' in relative or Path(relative).is_absolute() or any(part in ('','..','.') or part.casefold() in SKIP or part.casefold().startswith('.env') for part in relative.split('/')):raise ValueError('invalid original manifest relative path')
        unresolved=original_root/relative
        if unresolved.is_symlink() or getattr(unresolved,'is_junction',lambda:False)():raise ValueError('original snapshot source drift: symbolic link/junction')
        _safe_submitted(unresolved.parent)
        path=unresolved.resolve()
        if not path.is_relative_to(original_root) or _sha(path)!=item['sha256']:raise ValueError('original snapshot source drift')
    imported=_read(root/'imported-reference.json')
    if _sha(root/'imported-reference.json')!=case['imported_reference_sha256'] or _sha(root/'baseline-original.mo')!=imported['source_sha256'] or _sha(root/'imported-manifest.json')!=imported['manifest_sha256'] or _digest(imported['frozen_spec'])!=imported['spec_digest']:raise ValueError('imported reference source/spec/manifest drift')
    return case


def inspect_case(path, model=None):
    root,files,excluded=_files(path)
    models=[];documents=[]
    for item in files:
        if item['kind']=='mo':
            if item['bytes']>131072:raise ValueError('Modelica source exceeds supported parser limit')
            source=(root/item['path']).read_text(encoding='utf-8-sig')
            names=re.findall(r'^\s*model\s+([A-Za-z_]\w*)',source,re.M)
            models.append(dict(path=item['path'],model_name=names[0] if len(names)==1 else None,sha256=item['sha256'],support=support_for_source(source)))
        elif item['kind']=='docx':documents.append(dict(path=item['path'],**_docx(root/item['path'])))
        elif item['kind'] in ('txt','md','sysml'):
            if item['bytes']>MAX_XML:raise ValueError('input text exceeds finite context limit')
            documents.append(dict(path=item['path'],text=(root/item['path']).read_text(encoding='utf-8-sig'),status='UNVERIFIED_SOURCE_PAIR' if item['kind']=='sysml' else 'TEXT_EXTRACTED',ocr_performed=False))
        else:documents.append(dict(path=item['path'],status='SOURCE_PRESERVED_NOT_AUTOMATICALLY_INTERPRETED',ocr_performed=False))
    selected=next((x for x in models if x['path']==model),None) if model else models[0] if len(models)==1 else None
    if model and selected is None:raise ValueError('selected Modelica path is not an inspected candidate')
    input_hash=_digest([dict(path=x['path'],sha256=x['sha256']) for x in files])
    duplicates=[]
    for case in list_cases():
        same_bundle=case.get('input_sha256')==input_hash
        same_model=selected is not None and case.get('original_model_sha256')==selected['sha256']
        if same_bundle or same_model:duplicates.append(dict(**{k:case.get(k) for k in ('case_id','name','project_id','project_root')},match='SAME_INPUT_BUNDLE' if same_bundle else 'SAME_MODEL_SOURCE',whole_bundle_equal=same_bundle,model_source_equal=same_model))
    missing=[]
    if not models:missing.append('缺少Modelica模型文件。')
    elif selected is None:missing.append('目录含多个Modelica模型，请明确选择完整流程模型；不会静默合并上楼/下楼/功率文件。')
    # Raw SysML files are preserved; selecting a pair is not inferred from names.
    missing.append('提供的SysML尚未验证与所选Modelica的关联；确认方案后由产品模型核验并补齐。' if any(item['kind']=='sysml' for item in files) else '尚无基准SysML文件；确认方案后由产品模型补齐并独立验证。')
    return dict(path=str(root),files=files,excluded=excluded,model_candidates=models,selected_model=selected,documents=documents,duplicates=duplicates,input_sha256=input_hash,support=selected['support'] if selected else dict(status='NEEDS_INPUT',reasons=['必须明确完整流程模型。']),missing=missing,source_facts=dict(kind='ORIGINAL_INPUT_ONLY',model_parameters=selected['support'].get('parameters',{}) if selected else {},documents_interpreted='TEXT_ONLY_NO_OCR'),historical_success=dict(status='USER_PROVIDED_REFERENCE_HISTORY_NOT_INDEPENDENTLY_VERIFIED',execution_verified=False))


def import_case(path,name,model=None,success_statement='',duplicate_mode=None,*,origin_case_id=None):
    if not isinstance(name,str) or not name.strip() or len(name)>120:raise ValueError('nonempty case name <=120 characters required')
    if duplicate_mode not in (None,'independent'):raise ValueError('duplicate mode must be independent')
    if not isinstance(success_statement,str):raise ValueError('success statement must be user text')
    inspected=inspect_case(path,model)
    if inspected['duplicates'] and duplicate_mode!='independent':return dict(created=False,duplicates=inspected['duplicates'],inspection=inspected,requires_duplicate_mode='independent')
    if not inspected['selected_model']:return dict(created=False,inspection=inspected,missing=inspected['missing'])
    if origin_case_id is None and duplicate_mode=='independent' and inspected['duplicates']:origin_case_id=inspected['duplicates'][0]['case_id']
    from .case_backend import module
    cid='case_'+uuid.uuid4().hex[:20];root=_local(ROOT/'runs/case-projects'/cid)
    root.mkdir(parents=True,exist_ok=False)
    source_root=Path(inspected['path'])
    for item in inspected['files']:
        source=source_root/item['path'];target=root/'original'/item['path'];target.parent.mkdir(parents=True,exist_ok=True)
        # Recheck while copying to catch input changes after inspection. Do not
        # follow a newly inserted link or accept bytes different from inspection.
        if source.is_symlink() or not source.resolve().is_relative_to(source_root):raise ValueError('input path changed during import')
        raw=source.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=item['sha256']:raise ValueError('input source drift during import')
        with target.open('xb') as handle:handle.write(raw)
    original_manifest=dict(schema='v12-case-original-inputs-1',files=inspected['files'],input_sha256=inspected['input_sha256'],original_input_root=inspected['path'],selected_model=inspected['selected_model']['path'],excluded=inspected['excluded'])
    _write(root/'original-manifest.json',original_manifest)
    source=(root/'original'/inspected['selected_model']['path']).read_text(encoding='utf-8-sig')
    (root/'baseline-original.mo').write_text(source,encoding='utf8',newline='')
    db=root/'projects.sqlite';sid='import_'+uuid.uuid4().hex
    store=module('artifact_service').ArtifactStore(db);store.ensure_session(sid)
    project=store.create_project(sid,name.strip())
    facts='原始用户案例来源（不是本次改型要求）：'+json.dumps(dict(input_sha256=inspected['input_sha256'],selected_model=inspected['selected_model'],historical_success_statement=success_statement,historical_success_verified=False,documents=inspected['documents']),ensure_ascii=False)+'\n原样Modelica：\n'+source
    row=store.record_source(sid,facts);sources=[dict(id=row['source_id'],text=row['text'])]
    requirements=[dict(text='用户提供的原始参考案例；历史范围未独立核实，本次尚未执行。',source_id=row['source_id'],source_text='原始用户案例来源')]
    from .case_scope import NUMBER,strip_comments
    clean=strip_comments(source);settings={}
    for key in ('StopTime','Interval'):
        values=re.findall(r'\b'+key+r'\s*=\s*('+NUMBER+')',clean)
        if len(values)==1:settings[key]=float(values[0])
    from .case_asym2_family import FAMILY as ASYM2_FAMILY,OUTPUT_NAMES as ASYM2_OUTPUT_NAMES
    output_names=ASYM2_OUTPUT_NAMES if inspected['support'].get('family')==ASYM2_FAMILY else OUTPUT_NAMES
    spec=dict(schema_version='v11-model-artifact-1',backend='model-artifact',artifact=dict(model_name=inspected['selected_model']['model_name'] or 'UnknownModel',modelica_source=source),sources=sources,requirements=requirements,design=dict(summary='原样导入；设计结论和缺失SysML待产品模型补齐。',assumptions=[],parameters=[]),libraries=[],settings=dict(stop_time=settings.get('StopTime',0),intervals=round(settings['StopTime']/settings['Interval']) if settings.get('Interval',0)>0 and 'StopTime' in settings else 0),outputs={key:key for key in output_names} if inspected['support']['status']=='SUPPORTED' else {},checks=[])
    source_hash=hashlib.sha256(source.encode('utf8')).hexdigest()
    manifest=dict(schema='v12-imported-reference-manifest-1',record_kind='IMPORTED_REFERENCE',project_id=project['project_id'],source_sha256=source_hash,spec_digest=_digest(spec),original_input_sha256=inspected['input_sha256'],historical_success_scope=success_statement,task_status='NOT_RUN',sysml_origin='MISSING_PENDING_CONFIRMED_PRODUCT_COMPLETION')
    _write(root/'imported-manifest.json',manifest)
    imported=dict(record_kind='IMPORTED_REFERENCE',project_id=project['project_id'],task_id='reference_'+source_hash[:24],version_id='v000000',spec_digest=_digest(spec),source_sha256=source_hash,manifest_sha256=_sha(root/'imported-manifest.json'),frozen_spec=spec,evidence_status='IMPORTED_REFERENCE',state='not_run',tool_status='NOT_RUN',chain_status='NOT_RUN',task_status='NOT_RUN',vehicle_task_status='UNKNOWN',physical_acceptance_status='NOT_VERIFIED',recorded_conclusions_verified=False,published=False,read_only=True,status='IMPORTED_REFERENCE_NOT_RUN',source_files=dict(Modelica=str(root/'baseline-original.mo'),SysML=None),data_files=dict(CSV=None),outcome=dict(historical_success_scope=success_statement,historical_success_verified=False),support=inspected['support'],missing=inspected['missing'])
    _write(root/'imported-reference.json',imported)
    missing=list(inspected['missing'])
    case=dict(schema='v12-case-project-1',case_id=cid,name=name.strip(),project_id=project['project_id'],project_root=str(root),db_path=str(db),session_id=sid,kind='INDEPENDENT_REPLAY' if origin_case_id else 'IMPORTED_CASE',origin_case_id=origin_case_id,status='IMPORTED_REFERENCE_NOT_RUN',created_at_utc=datetime.now(timezone.utc).isoformat(),original_input_root=inspected['path'],selected_model=inspected['selected_model']['path'],input_sha256=inspected['input_sha256'],original_model_sha256=inspected['selected_model']['sha256'],normalized_model_sha256=source_hash,normalization=dict(original_bytes_preserved_in='original/'+inspected['selected_model']['path'],baseline_text='UTF8_WITHOUT_BOM_UNIVERSAL_NEWLINES',source_bytes_equal=inspected['selected_model']['sha256']==source_hash),original_manifest_sha256=_sha(root/'original-manifest.json'),imported_reference_sha256=_sha(root/'imported-reference.json'),historical_success_statement=success_statement,historical_success_verified=False,support=inspected['support'],missing=missing,authority_inherited=False)
    if duplicate_mode=='independent' and origin_case_id is None:case['kind']='INDEPENDENT_TEST'
    _write(root/'run.json',dict(schema='v12-case-import-run-1',case_id=cid,session_id=sid,project_id=project['project_id'],trusted_sources=sources,import_status='IMPORTED_REFERENCE_NOT_RUN',history_success_scope=success_statement))
    _write(root/'case.json',case)
    _write(_catalog()/(cid+'.json'),dict(schema='v12-case-catalog-entry-1',**{k:case[k] for k in ('case_id','project_id','project_root','db_path')},case_record_sha256=_sha(root/'case.json')))
    return dict(created=True,case=case,imported_reference=imported,duplicates=inspected['duplicates'],planning_http_authorized=False,execution_authorized=False,old_budget_inherited=False)


def replay_case(case_id,name=None,model=None,success_statement=None):
    case=verify_case(resolve_case(case_id))
    # A replay starts from the immutable ORIGINAL snapshot, never a generated
    # latest version, old request, old answer, confirmation or ledger.
    root=Path(case['original_input_root']) if case['kind']=='LEGACY_READ_ONLY_CATALOG' else Path(case['project_root'])/'original'
    selected=model or case.get('selected_model')
    if case['kind']=='LEGACY_READ_ONLY_CATALOG':
        if selected!=case['selected_model']:raise ValueError('legacy replay only supports its immutable original model')
        expected=case['original_model_sha256']
        if _sha(LEGACY_ROOT/'baseline-original.mo')!=expected or _sha(root/selected)!=expected:
            raise ValueError('legacy original replay snapshot drift')
    return import_case(root,name or case['name']+'（独立测试）',selected,case.get('historical_success_statement','') if success_statement is None else success_statement,'independent',origin_case_id=case['case_id'])


def _service(case):
    from .case_backend import service
    return service(case['db_path'],case['session_id'],read_only=True,project_root=case['project_root'])


def compare_versions(case_id,left,right):
    case=verify_case(resolve_case(case_id));svc=_service(case)
    a=svc._artifact_version(left);b=svc._artifact_version(right)
    from .case_scope import modelica_parameters
    def brief(record):
        spec=record['frozen_spec'];mission=record.get('outcome',{}).get('mission_assessment')
        from .case_contact_review import current_summary
        current=current_summary(record)
        from .case_scope import NUMBER,strip_comments
        tolerances=re.findall(r'\bTolerance\s*=\s*('+NUMBER+')',strip_comments(spec['artifact']['modelica_source']))
        settings=record.get('outcome',{}).get('tool_result',{}).get('actual_run_settings',{})
        try:parameters=modelica_parameters(spec['artifact']['modelica_source'])[0];parameter_status='EXTRACTED'
        except (ValueError,KeyError,TypeError):parameters={};parameter_status='UNSUPPORTED_PARAMETER_SYNTAX'
        terrain={key:parameters.get(key) for key in ('stepH','stepD','n_steps','len_flat','x_start_up','x_end_up','x_start_down','x_end_down','H_peak')}
        conditions=dict(terrain=terrain,settings=spec['settings'],source_tolerance=float(tolerances[0]) if len(tolerances)==1 else None,actual_solver=settings.get('solver','TEST_ONLY_NOT_RUN' if record.get('outcome',{}).get('test_only') else 'NOT_RUN' if record.get('evidence_status')!='COMPLETE' else 'UNKNOWN'),outputs=spec['outputs'],checks=spec['checks'],trajectory_contract=spec.get('trajectory_contract'),libraries=spec['libraries'])
        return dict(version_id=record['version_id'],task_id=record['task_id'],record_kind=record['record_kind'],parameters=parameters,parameter_status=parameter_status,baseline_source=spec.get('baseline_ref',dict(kind='ORIGINAL_IMPORTED_REFERENCE',input_sha256=case.get('input_sha256'),source_sha256=record['source_sha256'])),spec_digest=record['spec_digest'],source_sha256=record['source_sha256'],manifest_sha256=record.get('manifest_sha256'),tool_status=current.get('tool_status','NOT_RUN'),chain_status=current.get('chain_status','NOT_RUN'),task_status=current['task_status'],recorded_task_status=record.get('task_status'),current_contact_review=current['current_contact_review'],evidence_status=record.get('evidence_status'),source_files=record.get('source_files'),data_files=record.get('data_files'),historical_mission_assessment=mission,mission_assessment=current['current_contact_review'].get('current_assessment',{}),completion_time=mission.get('completion_time') if isinstance(mission,dict) else None,task_conditions=conditions,physical_acceptance_status='NOT_VERIFIED')
    aa,bb=brief(a),brief(b)
    keys=set(aa['parameters'])|set(bb['parameters'])
    from .case_scope import FAMILY_VARIABLE_PARAMETERS,model_shape
    try:shape_equal=model_shape(a['frozen_spec']['artifact']['modelica_source'],FAMILY_VARIABLE_PARAMETERS)==model_shape(b['frozen_spec']['artifact']['modelica_source'],FAMILY_VARIABLE_PARAMETERS)
    except (ValueError,KeyError,TypeError):shape_equal=False
    condition_matches={key:aa['task_conditions'][key]==bb['task_conditions'][key] for key in aa['task_conditions']}
    condition_matches['geometry_control_terrain_equations']=shape_equal
    missing=[]
    for label,record in [('left',aa),('right',bb)]:
        if record.get('evidence_status')!='COMPLETE' or record.get('chain_status')!='PASS':missing.append(label+'没有本次完整可信执行证据；历史成功声明不能补作运行结果。')
    for key,same in condition_matches.items():
        if not same:missing.append('条件不一致：'+key)
    return dict(case_id=case['case_id'],project_id=case['project_id'],left=aa,right=bb,parameter_changes={key:dict(before=aa['parameters'].get(key),after=bb['parameters'].get(key)) for key in sorted(keys) if aa['parameters'].get(key)!=bb['parameters'].get(key)},source_equal=aa['source_sha256']==bb['source_sha256'],task_conditions=condition_matches,conditions_match=all(condition_matches.values()),directly_comparable=not missing,incomparable_reasons=missing,comparison_status='AVAILABLE' if not missing else 'INCOMPLETE_OR_DIFFERENT_CONDITIONS',automatic_performance_ranking=False,scope='VERSION_BOUND_EVIDENCE_NO_HARDWARE_CERTIFICATION')


def discover_case(case_id=None):
    cases=list_cases()
    if not cases:return dict(cases=[],case_id=None,project_id=None,project_label=None,baselines=[],default_baseline=None,requests=[],controls={})
    # Catalog discovery is an index read. Original-input and selected execution
    # evidence are fully verified when a request actually adopts that baseline.
    case=resolve_case(case_id) if case_id else cases[0];svc=_service(case)
    from .case_scope import modelica_parameters
    baseline=svc._artifact_version('v000000')
    # Import-time capability describes the code that existed at import. Derive
    # current support from immutable source without rewriting that history.
    support=support_for_source(baseline['frozen_spec']['artifact']['modelica_source'])
    def brief(record):
        indexed=record.get('record_kind')=='ARTIFACT_EXECUTION_INDEX'
        if indexed:
            recorded=record.get('recorded_task_status','UNKNOWN')
            ready=record.get('state')=='succeeded' and record.get('recorded_chain_status')=='PASS' and support['status']=='SUPPORTED'
            return dict(version_id=record['version_id'],task_id=record['task_id'],
                parameters={key:record.get('parameters',{}).get(key) for key in ('m','I_pitch','T_main_max','T_nose_max','stepH','stepD') if key in record.get('parameters',{})},
                parameter_status='RECORDED_METADATA_NOT_RECHECKED',task_status='UNKNOWN',recorded_task_status=recorded,
                current_contact_review=dict(status='NOT_REVIEWED',task_status='UNKNOWN'),
                route_status='UNKNOWN',diagnostic_status='UNKNOWN',status='HISTORICAL_EXECUTION_NOT_RECHECKED',
                selectable=ready,automatic_default_eligible=ready and recorded=='PASS',
                source_sha256=record['source_sha256'],requires_sysml=None,
                source_body_has_sysml=record.get('source_body_has_sysml'),
                recorded_requires_sysml=record.get('recorded_requires_sysml'),
                selection_requires_verification=True,verification_status='NOT_RECHECKED')
        ready=(record['version_id']=='v000000' or record.get('evidence_status')=='COMPLETE' and record.get('chain_status')=='PASS') and support['status']=='SUPPORTED'
        try:values=modelica_parameters(record['frozen_spec']['artifact']['modelica_source'])[0];parameter_status='EXTRACTED'
        except (ValueError,KeyError,TypeError):values={};parameter_status='UNSUPPORTED_PARAMETER_SYNTAX'
        requires_sysml=not bool(record['frozen_spec']['artifact'].get('sysml_source'))
        from .case_contact_review import current_summary
        current=current_summary(record)
        return dict(version_id=record['version_id'],task_id=record['task_id'],parameters={key:values[key] for key in ('m','I_pitch','T_main_max','T_nose_max','stepH','stepD') if key in values},parameter_status=parameter_status,task_status=current['task_status'] if record['version_id']!='v000000' else 'NOT_RUN',recorded_task_status=record.get('task_status','NOT_RUN'),current_contact_review=current['current_contact_review'],route_status=current.get('route_status','UNKNOWN'),diagnostic_status=current.get('diagnostic_status','UNKNOWN'),status='IMPORTED_REFERENCE_NOT_RUN' if record['version_id']=='v000000' else 'CONTACT_ANOMALY' if current.get('diagnostic_status')=='FAIL' else 'CURRENT_LIMITED_TASK_COMPLETE' if current.get('task_status')=='PASS' else 'HISTORICAL_ROUTE_RECORD_CONTACT_NOT_ACCEPTED',selectable=ready,automatic_default_eligible=(record['version_id']=='v000000' or current.get('current_review_state')=='CURRENT_BOUND_REVIEW' and current.get('task_status')=='PASS' and current.get('diagnostic_status')=='PASS' and current.get('chain_status')=='PASS'),source_sha256=record['source_sha256'],requires_sysml=requires_sysml)
    baselines=[brief(baseline)]
    versions=svc.versions_index()['versions']['items']
    for row in versions:
        baselines.append(brief(row))
    requests=[]
    from .case_requests import request_root
    paths=list((Path(case['project_root'])/'requests').glob('*/task-context.json'))
    for path in (ROOT/'runs/context-registry').glob('*.json'):
        record=_read(path);paths.append(request_root(record['request_id'])/'task-context.json')
    seen=set()
    for path in paths:
        if path in seen:continue
        seen.add(path);value=_read(path)
        if value.get('project_id')!=case['project_id']:continue
        requests.append({key:value.get(key) for key in ('request_id','project_id','baseline_version_id','stage','raw_request','created_at_utc','access_mode')})
    requests.sort(key=lambda row:(row.get('created_at_utc') or '',row['request_id']))
    selectable=[row for row in baselines if row['selectable'] and row.get('automatic_default_eligible',True)];passes=[row for row in selectable if row.get('recorded_task_status',row['task_status'])=='PASS']
    return dict(cases=cases,case_id=case['case_id'],case_status=case['status'],case_kind=case['kind'],case=case,project_id=case['project_id'],project_label=case['name'],baselines=baselines,default_baseline=passes[-1] if passes else selectable[-1] if selectable else None,requests=requests,support=support,historical_import_support=deepcopy(case.get('support')),missing=case.get('missing',[]),generation_obligations=['由产品核验并补齐原基准SysML及候选双源码，分别绑定原资料和本次新要求；不覆盖v0。'] if not baseline['frozen_spec']['artifact'].get('sysml_source') else [],controls={key:_read(ROOT/'configs/case_runtime.json')[key] for key in ('model','thinking','reasoning_effort','context_window_tokens','max_tokens','maximum_request_reserve_cny')})


# Stable Python spelling for callers which use a generic inspect API.
inspect = inspect_case


def catalog_summaries():
    """Catalog identity plus recorded Store scalars, without per-case discovery.

    The suggested default is historical metadata only. Opening/adopting a
    baseline still uses the existing selected-case evidence validation path.
    """
    summaries=[]
    for case in list_cases():
        summary=dict(case,latest_version_id=None,default_baseline_version_id=None,
            verification_status='NOT_RECHECKED',selection_requires_verification=True,
            summary_scope='CATALOG_AND_RECORDED_STORE_METADATA_ONLY')
        db=None
        try:
            path=_local(case['db_path'])
            db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=5)
            db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON')
            project=db.execute('SELECT project_id FROM projects WHERE project_id=?',(case['project_id'],)).fetchone()
            if project is None:raise ValueError('catalog project is absent from its Store')
            base='''SELECT t.version_id,t.state,
                json_extract(t.result_json,'$.tool_status') AS recorded_tool_status,
                json_extract(t.result_json,'$.chain_status') AS recorded_chain_status,
                json_extract(t.result_json,'$.task_status') AS recorded_task_status
                FROM tasks t JOIN proposals p ON p.proposal_id=t.proposal_id
                WHERE t.project_id=? AND json_extract(p.spec_json,'$.backend')='model-artifact' '''
            latest=db.execute(base+'ORDER BY t.created_at DESC,t.rowid DESC LIMIT 1',(case['project_id'],)).fetchone()
            eligible="AND t.state='succeeded' AND json_extract(t.result_json,'$.chain_status')='PASS' "
            passing=db.execute(base+eligible+"AND json_extract(t.result_json,'$.task_status')='PASS' ORDER BY t.created_at DESC,t.rowid DESC LIMIT 1",(case['project_id'],)).fetchone()
            reusable=db.execute(base+eligible+'ORDER BY t.created_at DESC,t.rowid DESC LIMIT 1',(case['project_id'],)).fetchone()
            suggested=passing or reusable
            # Preserve the imported v0 and legacy entry without opening the
            # imported source, resolving its family or traversing its requests.
            supported=case.get('support',{}).get('status')!='UNSUPPORTED'
            summary.update(latest_version_id=latest['version_id'] if latest else 'v000000',
                default_baseline_version_id=(suggested['version_id'] if suggested else 'v000000') if supported else None,
                latest_recorded_execution=dict(latest) if latest else None,
                recorded_support_status=case.get('support',{}).get('status','UNKNOWN'),
                store_metadata_status='AVAILABLE')
        except (OSError,ValueError,sqlite3.Error) as exc:
            summary.update(store_metadata_status='UNAVAILABLE',store_metadata_error=type(exc).__name__+': '+str(exc))
        finally:
            if db is not None:db.close()
        summaries.append(summary)
    return summaries
