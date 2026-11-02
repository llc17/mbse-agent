"""Synthetic Store for the real v12.cmd smoke; no model or worker transport.

The child hook changes only fixture storage paths and its catalog label. It
retains the actual launcher, Console, CliRunner, readback and compare code.
"""
import hashlib
import importlib.abc
import importlib.machinery
import os
from pathlib import Path
import sys
from unittest.mock import patch


def seed(root):
    from week13.src import case_backend, case_requests
    from week13.tests import test_case_agent_requests as fixtures
    saved = case_requests.ROOT, case_requests.REQUESTS_ROOT, case_requests.LEGACY_PROJECT_ROOT
    original_finish, original_write = fixtures.finish_fixture, fixtures.write

    def timed_write(path, value):
        if Path(path).name == 'outcome.json':
            value['mission_assessment']['completion_time'] = 17.25
        original_write(path, value)

    def finish(svc, task, folder, task_status='PASS'):
        return original_finish(svc, task, folder, task_status)

    try:
        with patch.object(fixtures, 'finish_fixture', finish), patch.object(fixtures, 'write', timed_write):
            harness = fixtures.Harness(root)
            svc = case_backend.module('artifact_service').ArtifactService(harness.db, 'fixture_seed', context=root / 'seed-context.json', worker_manager=fixtures.NoLaunch())
            svc.project_root = harness.projectroot
            source = svc.record_source('TEST_ONLY deterministic CMD fixture')
            spec = fixtures.synthetic_spec(dict(id=source['source_id'], text=source['text']), 92)
            proposal = svc.proposal_prepare(spec)['proposal']
            stored = svc.store.proposal(proposal['proposal_id'])
            svc.confirm_local(stored['proposal_id'], stored['displayed_digest'])
            task = svc.execution_start(stored['proposal_id'], 'fixture_cmd_v3')['task']
            fixtures.finish_fixture(svc, task, harness.projectroot)
    finally:
        case_requests.ROOT, case_requests.REQUESTS_ROOT, case_requests.LEGACY_PROJECT_ROOT = saved


def file_hashes(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in root.rglob('*') if path.is_file()}


def configure_child(env, root, hook):
    hook.mkdir(parents=True, exist_ok=True)
    # PYTHONPATH startup is test-scoped. No production launcher/config change.
    (hook / 'sitecustomize.py').write_text(
        "import os\nif os.environ.get('V12_CLOSEOUT_BASE'): import isolation\n"
        "from week13.tests.case_console_cmd_fixture import install\ninstall()\n", encoding='utf8')
    return dict(env, V12_TEST_CMD_ROOT=str(root), V12_CLOSEOUT_CMD_ROOT=str(root),
                PYTHONPATH=os.pathsep.join([str(hook), str(Path(__file__).resolve().parents[2]), env.get('PYTHONPATH', '')]))


def install():
    root = Path(os.environ['V12_TEST_CMD_ROOT']).resolve()
    targets = {'week13.src.case_projects', 'week13.src.case_console_data', 'week13.src.case_requests'}

    def guard(event, args):
        if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
            raw = os.fsdecode(args[0])
            if raw.upper() in ('NUL', r'\\.\NUL'): return
            path = Path(raw).resolve()
            if path.name == '.env': raise AssertionError('CMD fixture prohibits credentials')
            if args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                if not path.is_relative_to(root.parent): raise AssertionError('CMD fixture write escaped isolation')
        elif event == 'socket.connect':
            address = args[1]
            if not (isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1')):
                raise AssertionError('CMD fixture prohibits real network')
        elif event == 'subprocess.Popen' and 'omc' in str(args[1]).lower():
            raise AssertionError('CMD fixture prohibits compiler')

    sys.addaudithook(guard)

    def configure(module):
        module.ROOT = root
        if module.__name__ == 'week13.src.case_projects':
            module.LEGACY_ROOT, module.LEGACY_REPLAY_INPUT = root / 'shared-project', root / 'original-input'
            original = module._legacy
            def catalog():
                value = original()
                return dict(value, case_id='TEST_ONLY_cmd_case', name='TEST_ONLY CMD isolated case') if value else None
            module._legacy = catalog
        elif module.__name__ == 'week13.src.case_console_data':
            module.PROJECT_ROOT = root / 'shared-project'
        else:
            module.REQUESTS_ROOT, module.LEGACY_PROJECT_ROOT = root / 'shared-project/requests', root / 'shared-project'

    class Loader(importlib.abc.Loader):
        def __init__(self, wrapped): self.wrapped = wrapped
        def create_module(self, spec): return self.wrapped.create_module(spec)
        def exec_module(self, module):
            self.wrapped.exec_module(module)
            configure(module)
        def get_code(self, fullname):
            if fullname == 'week13.src.case_console_data':
                source = self.wrapped.get_source(fullname)
                insertion = 'ROOT = __import__("pathlib").Path(__import__("os").environ["V12_TEST_CMD_ROOT"])\nPROJECT_ROOT = ROOT / "shared-project"\n\n'
                source = source.replace("if __name__ == '__main__':", insertion + "if __name__ == '__main__':")
                return compile(source, self.wrapped.get_filename(fullname), 'exec')
            return self.wrapped.get_code(fullname)
        def __getattr__(self, name): return getattr(self.wrapped, name)

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == 'week13.src.case_cli':
                raise AssertionError('CMD smoke permits read-only data/project commands only')
            if fullname in targets:
                spec = importlib.machinery.PathFinder.find_spec(fullname, path)
                if spec and spec.loader: spec.loader = Loader(spec.loader)
                return spec

    sys.meta_path.insert(0, Finder())
