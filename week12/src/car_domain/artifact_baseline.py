"""Explicit verified artifact baseline; separate from published V10 versions."""
from copy import deepcopy
import json
from week11.src.store import digest_json
REF_FIELDS={'project_id','task_id','version_id','spec_digest','source_sha256','manifest_sha256'}
def reference(record):return {k:record[k] for k in REF_FIELDS}
def selection(service):
    with service.store.reading() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifact_baseline_selections'").fetchone():return None
        row=db.execute('SELECT binding_json FROM artifact_baseline_selections WHERE session_id=? AND project_id=?',(service.session_id,service._current_project()['project_id'])).fetchone()
    return json.loads(row[0]) if row else None
def resolve(service,ref):
    if not isinstance(ref,dict) or set(ref)!=REF_FIELDS:raise ValueError('invalid artifact baseline reference')
    task=service.store.task(ref['task_id']);service._require_current_project(task['project_id'])
    if task['project_id']!=ref['project_id'] or task['version_id']!=ref['version_id']:raise ValueError('artifact baseline task/version/project mismatch')
    from .artifact_history import snapshot
    record,_=snapshot(service,task)
    if record['evidence_status']!='COMPLETE' or record['state']!='succeeded' or record['chain_status']!='PASS' or not record['recorded_conclusions_verified']:raise ValueError('artifact baseline requires complete tool/chain evidence, not a task PASS')
    if reference(record)!=ref:raise ValueError('artifact baseline source/spec/manifest drift')
    return record
def selected_record(service):
    ref=selection(service);return resolve(service,ref) if ref else None
def normalize_candidate(service,spec):
    """Inherit only omitted preserved metadata from an explicit verified baseline."""
    if not isinstance(spec,dict) or 'baseline_ref' not in spec:return spec,{}
    allowed={'schema_version','backend','baseline_ref','artifact','sources','requirements','design','libraries','settings','outputs','checks','trajectory_contract'}
    required={'schema_version','backend','baseline_ref','artifact','sources','requirements'}
    if set(spec)-allowed or not required<=set(spec):return spec,{}
    ref=selection(service)
    if ref is None or spec['baseline_ref']!=ref:raise ValueError('partial candidate baseline is not the explicit selected baseline')
    record=resolve(service,ref);result=deepcopy(spec);inherited={}
    for field in ('design','libraries','settings','outputs','checks'):
        if field not in result:
            result[field]=deepcopy(record['frozen_spec'][field])
            inherited[field]=dict(sha256=digest_json(result[field]),from_artifact_baseline=deepcopy(ref),reason='omitted preserved baseline metadata; no explicit value overwritten')
    if 'trajectory_contract' not in result and 'trajectory_contract' in record['frozen_spec']:
        result['trajectory_contract']=deepcopy(record['frozen_spec']['trajectory_contract'])
        inherited['trajectory_contract']=dict(sha256=digest_json(result['trajectory_contract']),from_artifact_baseline=deepcopy(ref),reason='omitted preserved baseline trajectory metadata')
    return result,inherited
def verify_proposal(service,proposal,*,require_selection=False,require_confirmation=False):
    ref=proposal['spec'].get('baseline_ref')
    if proposal['operation']=='create':
        if ref or proposal['display'].get('artifact_baseline'):raise ValueError('create cannot invent an artifact baseline')
        return None
    if proposal['operation']!='modify' or ref!=proposal['display'].get('artifact_baseline'):raise ValueError('modify baseline not bound to displayed source')
    if require_selection and ref!=selection(service):raise ValueError('selected artifact baseline changed after proposal')
    record=resolve(service,ref)
    if require_confirmation and proposal['confirmation'].get('artifact_baseline')!=ref:raise ValueError('confirmation does not bind artifact baseline')
    return record
def changes(before,after):
    return {f:dict(same=before.get(f)==after.get(f),before_sha256=digest_json(before.get(f)),after_sha256=digest_json(after.get(f)),before=deepcopy(before.get(f)),after=deepcopy(after.get(f))) for f in ('artifact','requirements','design','settings','checks','outputs','libraries','trajectory_contract')}
