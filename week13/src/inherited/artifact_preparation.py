"""Private, request-local reuse of a freshly verified proposal baseline.

Only service proposal entry points create this scope. No spec field or public
caller-supplied record can seed it. Nested prepares get their own scope; the
ContextVar token restores the outer scope even when validation raises.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from week11.src.store import digest_json
from .contracts import WEEK12_ROOT, inside_week12

_active = ContextVar('artifact_proposal_preparation', default=None)


def _files(service, ref):
    root = getattr(service, 'project_root', WEEK12_ROOT)
    if ref['version_id'] == 'v000000':
        root = Path(root)
        return root, [root / name for name in ('imported-reference.json', 'imported-manifest.json', 'baseline-original.mo')], None
    task = service.store.task(ref['task_id'])
    base = inside_week12(Path(root) / 'projects' / task['project_id'] / 'versions' / task['version_id'])
    result = task.get('result') or {}
    name = result.get('outcome_manifest_path')
    if not isinstance(name, str):
        raise ValueError('baseline has no immutable manifest')
    manifest_path = inside_week12(Path(name) if Path(name).is_absolute() else base / name)
    if manifest_path != base / 'artifact-manifest.json':
        raise ValueError('manifest escapes this task version root')
    manifest = json.loads(manifest_path.read_bytes())
    paths = [manifest_path]
    for name in manifest.get('files', {}):
        if not isinstance(name, str) or not name or Path(name).is_absolute() or '..' in Path(name).parts:
            raise ValueError('unsafe evidence manifest path')
        path = (base / name).resolve()
        if not path.is_relative_to(base.resolve()):
            raise ValueError('evidence escapes task')
        paths.append(path)
    return base, paths, (manifest_path, result.get('outcome_manifest_sha256'))


def _state(service, ref):
    """Cheap drift guard between uses; final acceptance also rehashes files."""
    base, paths, manifest = _files(service, ref)
    files = []
    for path in paths:
        stat = path.stat()
        files.append((str(path.resolve()), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    if ref['version_id'] == 'v000000':
        # These metadata files are small; bind their contents on every reuse.
        database = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths[:2]]
    else:
        task = service.store.task(ref['task_id'])
        proposal = service.store.proposal(task['proposal_id'])
        source_ids = [row['id'] for row in proposal['spec']['sources']]
        sources = service.store.sources(task['session_id'], task['project_id'], source_ids)
        database = [task, proposal, sources, hashlib.sha256(manifest[0].read_bytes()).hexdigest()]
    return digest_json(dict(database=database, files=files))


class _Preparation:
    def __init__(self, service):
        from .artifact_baseline import selection
        self.service = service
        self.identity = (service.session_id, str(Path(service.store.db_path).resolve()))
        self.project = deepcopy(service._current_project())
        self.selection = selection(service)
        self.records = {}
        self.loading = set()

    def check(self):
        from .artifact_baseline import selection
        identity = (self.service.session_id, str(Path(self.service.store.db_path).resolve()))
        if identity != self.identity or self.service._current_project() != self.project or selection(self.service) != self.selection:
            raise ValueError('proposal preparation project/baseline selection changed')

    def resolve(self, ref, load):
        self.check()
        key = digest_json(ref)
        current = _state(self.service, ref)
        if key in self.records:
            entry = self.records[key]
            if current != entry['state']:
                raise ValueError('proposal preparation baseline evidence changed')
            return deepcopy(entry['record'])
        if key in self.loading:
            raise ValueError('cyclic artifact baseline')
        self.loading.add(key)
        try:
            record = load()
            self.check()
            if _state(self.service, ref) != current:
                raise ValueError('proposal preparation baseline changed during verification')
            self.records[key] = dict(ref=deepcopy(ref), state=current, record=deepcopy(record))
            return record
        finally:
            self.loading.remove(key)

    def verify_before_insert(self):
        """Rehash every bound evidence file, including CSV; never reparse CSV."""
        from .service_adapter import _verify_manifest
        self.check()
        for entry in self.records.values():
            ref = entry['ref']
            if _state(self.service, ref) != entry['state']:
                raise ValueError('proposal preparation baseline evidence changed')
            base, paths, manifest = _files(self.service, ref)
            if manifest:
                _verify_manifest(manifest[0], manifest[1], base)
            elif hashlib.sha256(paths[2].read_bytes()).hexdigest() != ref['source_sha256'] or hashlib.sha256(paths[1].read_bytes()).hexdigest() != ref['manifest_sha256']:
                raise ValueError('imported reference source/manifest drift')
            if _state(self.service, ref) != entry['state']:
                raise ValueError('proposal preparation baseline changed during final hash check')
        self.check()


@contextmanager
def proposal_preparation(service):
    operation = _Preparation(service)
    token = _active.set(operation)
    try:
        yield
    finally:
        operation.records.clear()
        _active.reset(token)


def resolve_for_preparation(service, ref, load):
    operation = _active.get()
    return operation.resolve(ref, load) if operation is not None and operation.service is service else load()


def verify_before_insert(service):
    operation = _active.get()
    if operation is not None and operation.service is service:
        operation.verify_before_insert()
