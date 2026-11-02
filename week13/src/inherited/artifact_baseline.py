"""Explicit verified artifact baseline; separate from published V10 versions."""
from copy import deepcopy
import json
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from week11.src.store import digest_json
REF_FIELDS={'project_id','task_id','version_id','spec_digest','source_sha256','manifest_sha256'}
_execution = ContextVar('artifact_worker_baseline_verification', default=None)
def reference(record):return {k:record[k] for k in REF_FIELDS}
def selection(service):
    with service.store.reading() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifact_baseline_selections'").fetchone():return None
        row=db.execute('SELECT binding_json FROM artifact_baseline_selections WHERE session_id=? AND project_id=?',(service.session_id,service._current_project()['project_id'])).fetchone()
    return json.loads(row[0]) if row else None
def resolve(service,ref):
    if not isinstance(ref,dict) or set(ref)!=REF_FIELDS:raise ValueError('invalid artifact baseline reference')
    operation = _execution.get()
    if operation is not None and operation.service is service:
        return operation.resolve(ref)
    from .artifact_preparation import resolve_for_preparation
    return resolve_for_preparation(service,ref,lambda:_resolve_fresh(service,ref))


class _ExecutionBaselines:
    """Private one-worker reuse; every guard reads content, never mtime/size."""
    def __init__(self, service, task_context=None):
        self.service = service
        self.identity = (service.session_id, str(Path(service.store.db_path).resolve()))
        self.project = deepcopy(service._current_project())
        self.selected = selection(service)
        self.request_context = task_context
        self.request_digest = digest_json(task_context)
        self.records = {}
        self.loading = set()

    def check(self):
        current = (self.service.session_id, str(Path(self.service.store.db_path).resolve()))
        if current != self.identity or self.service._current_project() != self.project or selection(self.service) != self.selected:
            raise ValueError('worker baseline project/selection changed')
        if digest_json(self.request_context) != self.request_digest:
            raise ValueError('worker baseline request context changed')
        if self.request_context is not None and (self.request_context.get('project_id') != self.project['project_id'] or self.request_context.get('baseline_ref') != self.selected):
            raise ValueError('worker baseline request project/reference differs')

    def content(self, ref):
        from .artifact_preparation import _files
        from .service_adapter import file_sha256
        _, paths, _ = _files(self.service, ref)
        files = [(str(p.resolve()), file_sha256(p)) for p in paths]
        if ref['version_id'] == 'v000000':
            database = None
        else:
            task = self.service.store.task(ref['task_id'])
            proposal = self.service.store.proposal(task['proposal_id'])
            ids = [s['id'] for s in proposal['spec']['sources']]
            database = [task, proposal, self.service.store.sources(task['session_id'], task['project_id'], ids)]
        return digest_json(dict(files=files, database=database))

    def resolve(self, ref):
        self.check(); key = digest_json(ref); current = self.content(ref)
        if key in self.records:
            entry = self.records[key]
            if current != entry['content']:raise ValueError('worker baseline content drift')
            return deepcopy(entry['record'])
        if key in self.loading:raise ValueError('cyclic worker artifact baseline')
        self.loading.add(key)
        try:
            record = _resolve_fresh(self.service, ref)
            self.check()
            if self.content(ref) != current:raise ValueError('worker baseline changed during verification')
            self.records[key] = dict(ref=deepcopy(ref), content=current, record=deepcopy(record))
            return record
        finally:self.loading.remove(key)

    def before_launch(self):
        self.check()
        for entry in self.records.values():
            if self.content(entry['ref']) != entry['content']:raise ValueError('worker baseline content drift before launch')
        self.check()


@contextmanager
def execution_baselines(service, *, task_context=None):
    """Only worker code opens this scope; no caller-supplied record seeds it."""
    operation = _ExecutionBaselines(service, task_context);token = _execution.set(operation)
    try:yield operation
    finally:
        operation.records.clear();_execution.reset(token)
def _resolve_fresh(service,ref):
    if ref.get('version_id')=='v000000':
        # A user-supplied successful case has no invented execution task. Its
        # imported manifest/source and product-generated SysML are independently bound.
        from ..case_paths import PROJECT_ROOT,read_json,sha_file
        PROJECT_ROOT=getattr(service,'project_root',PROJECT_ROOT)
        record=read_json(PROJECT_ROOT/'imported-reference.json')
        manifest=read_json(PROJECT_ROOT/'imported-manifest.json')
        if record.get('record_kind')!='IMPORTED_REFERENCE' or record.get('task_status')!='NOT_RUN' or reference(record)!=ref:raise ValueError('invalid imported reference identity')
        service._require_current_project(record['project_id'])
        if sha_file(PROJECT_ROOT/'baseline-original.mo')!=record['source_sha256'] or sha_file(PROJECT_ROOT/'imported-manifest.json')!=record['manifest_sha256'] or digest_json(record['frozen_spec'])!=record['spec_digest']:raise ValueError('imported reference source/spec/manifest drift')
        if manifest['source_sha256']!=record['source_sha256'] or manifest['spec_digest']!=record['spec_digest']:raise ValueError('imported manifest/spec binding differs')
        return record
    task=service.store.task(ref['task_id']);service._require_current_project(task['project_id'])
    if task['project_id']!=ref['project_id'] or task['version_id']!=ref['version_id']:raise ValueError('artifact baseline task/version/project mismatch')
    from .artifact_history import snapshot
    from ..case_diagnostics import phase
    with phase('BASELINE_SNAPSHOT'):record,_=snapshot(service,task)
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
