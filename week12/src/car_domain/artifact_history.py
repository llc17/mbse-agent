"""Read-only execution snapshots over V10 tasks, not vehicle publications."""
from pathlib import Path
import json
from week11.src.store import digest_json,safe_id
from .contracts import WEEK12_ROOT,inside_week12
from .model_artifact import validate_artifact,sha
from .service_adapter import _verify_manifest

def snapshot(service,task):
    service._require_current_project(task['project_id'])
    for key in ('task_id','project_id','proposal_id','version_id','session_id'):
        safe_id(task[key],key)
    proposal=service.store.proposal(task['proposal_id']);spec=proposal['spec'];display=proposal['display']
    checked=validate_artifact(spec)
    if not checked['ready'] or checked['spec_digest']!=proposal['spec_digest'] or digest_json(display)!=proposal['displayed_digest']:
        raise ValueError('historical frozen proposal digest mismatch')
    if proposal['project_id']!=task['project_id'] or proposal['session_id']!=task['session_id']:
        raise ValueError('historical task/proposal identity mismatch')
    display_fields=dict(project_id=task['project_id'],spec_digest=proposal['spec_digest'],source_sha256=checked['source_sha256'],requirements=spec['requirements'],sources=spec['sources'],model_design=spec['design'],artifact=spec['artifact'],libraries=spec['libraries'],settings=spec['settings'],outputs=spec['outputs'],checks=spec['checks'])
    if 'trajectory_contract' in spec:display_fields['trajectory_contract']=spec['trajectory_contract']
    if any(display.get(k)!=v for k,v in display_fields.items()):raise ValueError('historical display/spec/source mismatch')
    if display.get('operation')!=proposal['operation'] or display.get('baseline_version_id')!=proposal['baseline_version_id']:raise ValueError('historical operation/published baseline mismatch')
    if proposal['operation']=='modify':
        from .artifact_baseline import verify_proposal
        ref=spec.get('baseline_ref')
        if not ref or int(ref['version_id'][1:])>=int(task['version_id'][1:]):raise ValueError('artifact baseline must precede this execution version')
        verify_proposal(service,proposal,require_confirmation=True)
    declared={s['id']:s['text'] for s in spec['sources']}
    stored=service.store.sources(task['session_id'],task['project_id'],list(declared))
    if {s['source_id']:s['text'] for s in stored}!=declared or any(s['text_sha256']!=sha(s['text'].encode()) for s in stored):
        raise ValueError('historical trusted source drift')
    result=task.get('result') or {}
    record=dict(record_kind='ARTIFACT_EXECUTION_SNAPSHOT',published=False,read_only=True,project_id=task['project_id'],version_id=task['version_id'],task_id=task['task_id'],state=task['state'],error=task.get('error',''),created_at=task['created_at'],updated_at=task['updated_at'],evidence_status='INCOMPLETE',spec_digest=proposal['spec_digest'],source_sha256=checked['source_sha256'],frozen_spec=spec,tool_status=result.get('tool_status','UNKNOWN'),chain_status=result.get('chain_status','UNKNOWN'),task_status=result.get('task_status','UNKNOWN'),vehicle_task_status='UNKNOWN',physical_acceptance_status='NOT_VERIFIED',recorded_conclusions_verified=False)
    record['dual_model_status']=dict(sysml='HISTORICAL_NOT_CHECKED' if spec['artifact'].get('sysml_source') else 'NOT_GENERATED',modelica=result.get('tool_status','UNKNOWN'),consistency='HISTORICAL_NOT_CHECKED',semantic_equivalence='UNKNOWN')
    record['source_files']=dict(Modelica=None,SysML=None)
    record['data_files']=dict(CSV=None)
    record['curve_files']=[]
    record['result_delivery']=dict(status='HISTORICAL_NOT_GENERATED',plot_status='NOT_GENERATED')
    path=result.get('outcome_manifest_path');digest=result.get('outcome_manifest_sha256')
    if not path and not digest:return record,None
    if not isinstance(path,str) or not isinstance(digest,str):raise ValueError('incomplete manifest reference')
    base=inside_week12(WEEK12_ROOT/'projects'/task['project_id']/'versions'/task['version_id'])
    candidate=inside_week12(Path(path) if Path(path).is_absolute() else base/path)
    if candidate!=base/'artifact-manifest.json':raise ValueError('manifest escapes this task version root')
    manifest=_verify_manifest(candidate,digest,base)
    expected=dict(task_id=task['task_id'],project_id=task['project_id'],spec_digest=proposal['spec_digest'],source_sha256=checked['source_sha256'],owner=task['worker_owner'],context_id=display['execution_context_id'],context_sha256=display['execution_context_sha256'])
    if proposal['operation']=='modify':expected['artifact_baseline']=spec['baseline_ref']
    if manifest.get('schema')!='v11-artifact-manifest-1' or manifest.get('binding')!=expected:raise ValueError('artifact manifest identity/spec/source mismatch')
    required={'spec.json','confirmation.json','frozen-model.mo','outcome.json'}
    if proposal['operation']=='modify':
        required.add('baseline-binding.json')
        if json.loads((base/'baseline-binding.json').read_bytes())!=spec['baseline_ref']:raise ValueError('frozen artifact baseline binding differs')
    if not required<=set(manifest['files']):raise ValueError('manifest omits required frozen evidence')
    record['source_files']['Modelica']=str(base/'frozen-model.mo')
    if json.loads((base/'spec.json').read_bytes())!=spec or json.loads((base/'confirmation.json').read_bytes())!=proposal['confirmation']:
        raise ValueError('frozen file/proposal mismatch')
    if sha((base/'frozen-model.mo').read_bytes())!=checked['source_sha256']:
        raise ValueError('frozen Modelica/source mismatch')
    outcome=manifest.get('outcome')
    committed={k:v for k,v in result.items() if k not in {'outcome_manifest_path','outcome_manifest_sha256','worker_exit_code','worker_pid'}}
    if committed!=outcome or json.loads((base/'outcome.json').read_bytes())!=outcome:
        raise ValueError('artifact outcome/result semantic drift')
    if outcome.get('vehicle_task_status')!='UNKNOWN' or outcome.get('physical_acceptance_status')!='NOT_VERIFIED' or outcome.get('published_version_id') is not None:
        raise ValueError('artifact snapshot cannot imply vehicle acceptance/publication')
    if outcome.get('dual_source_contract')=='v11-dual-source-check-1':
        if not {'frozen-model.sysml','sysml-result.json','dual-source-result.json'}<=set(manifest['files']):raise ValueError('dual-source manifest omits required evidence')
        if sha((base/'frozen-model.sysml').read_bytes())!=sha(spec['artifact'].get('sysml_source','').encode()):raise ValueError('frozen SysML/source mismatch')
        syntax=json.loads((base/'sysml-result.json').read_bytes());consistency=json.loads((base/'dual-source-result.json').read_bytes())
        if syntax.get('status')!=outcome.get('sysml_status') or consistency.get('status')!=outcome.get('consistency_status') or consistency.get('modelica_source_sha256')!=checked['source_sha256'] or consistency.get('sysml_source_sha256')!=sha(spec['artifact'].get('sysml_source','').encode()):raise ValueError('dual-source outcome/evidence differs')
        if outcome.get('chain_status')=='PASS' and (syntax.get('status')!='PASS' or consistency.get('status')!='PASS'):raise ValueError('chain PASS lacks required dual-source checks')
        record['source_files']['SysML']=str(base/'frozen-model.sysml')
        record['dual_model_status']=dict(sysml=syntax.get('status'),modelica=outcome.get('modelica_status'),consistency=consistency.get('status'),semantic_equivalence='UNKNOWN')
    if task['state']=='succeeded' and (result.get('worker_exit_code')!=0 or outcome.get('chain_status')!='PASS'):
        raise ValueError('artifact completion lacks actual successful worker exit')
    final=task['state'] in {'succeeded','failed'} and result.get('worker_exit_code')==0
    record.update(evidence_status='COMPLETE' if final else 'STAGED',recorded_conclusions_verified=final,outcome=result,manifest_sha256=digest,conclusion_stage='FINAL' if final else 'STAGED_NOT_FINAL')
    if 'tool/model_res.csv' in manifest['files']:
        record['data_files']['CSV']=str(base/'tool/model_res.csv')
    elif outcome.get('chain_status')=='PASS' or outcome.get('task_status')=='PASS':
        raise ValueError('claimed PASS lacks manifest-bound original CSV')
    from .artifact_delivery import verify_delivery,read_signals
    if outcome.get('chain_status')=='PASS' or outcome.get('task_status')=='PASS':read_signals(spec,base/'tool/model_res.csv')
    record['result_delivery'],record['curve_files']=verify_delivery(spec,outcome,manifest,base,dict(expected,version_id=task['version_id']))
    if not final:record.update(chain_status='PENDING',task_status='UNKNOWN')
    return record,manifest

def comparison(left,right):
    if left['project_id']!=right['project_id']:raise ValueError('cross-project snapshot comparison refused')
    fields={'source':lambda s:s['artifact'],'requirements':lambda s:s['requirements'],'sources':lambda s:s['sources'],'design':lambda s:s['design'],'settings':lambda s:s['settings'],'checks':lambda s:s['checks'],'outputs':lambda s:s['outputs'],'libraries':lambda s:s['libraries']}
    fields['trajectory_contract']=lambda s:s.get('trajectory_contract')
    diffs={name:dict(before=get(left['frozen_spec']),after=get(right['frozen_spec']),same=get(left['frozen_spec'])==get(right['frozen_spec']),before_sha256=digest_json(get(left['frozen_spec'])),after_sha256=digest_json(get(right['frozen_spec']))) for name,get in fields.items()}
    return dict(project_id=left['project_id'],record_kind='ARTIFACT_SNAPSHOT_COMPARISON',read_only=True,published=False,before=left,after=right,differences=diffs,same_conditions=all(diffs[k]['same'] for k in ('requirements','sources','design','settings','checks','outputs','libraries','trajectory_contract')),evidence_status='COMPLETE' if all(s['evidence_status']=='COMPLETE' for s in (left,right)) else 'INCOMPLETE',combined_vehicle_task_status='UNKNOWN',scope='FROZEN_EXECUTION_RECORD_COMPARISON_ONLY_NO_COMBINED_CAPABILITY_OR_PHYSICAL_PASS')
