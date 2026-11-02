"""Current authorization-bound paths; adapted from archived modification_paths.

No archived module is executed. The immutable plan and authorization witness are
checked on every authorization read; one UTC deadline covers every process.
"""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
from .case_requests import selected_request, load_request, authority_for, require_planning, require_execution

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
PACKAGE = ROOT / 'results' / 's0-case-agent-20261019-01'
RUN = ROOT / 'runs' / 'success-case-20261019-01'
AUTH = PACKAGE / 'authorization.json'
AUTH_WITNESS = PACKAGE / 'authorization.sha256'
CONFIG = ROOT / 'configs' / 'case_runtime.json'
BUDGET_DB = RUN / 'budget.sqlite'
TASK_CONTEXT = selected_request()
PROJECT_ROOT = RUN
PROJECT_DB = RUN / 'projects.sqlite'
if TASK_CONTEXT is not None:
    PROJECT_ROOT = Path(TASK_CONTEXT['project_root'])
    PROJECT_DB = Path(TASK_CONTEXT['db_path'])
    RUN = Path(TASK_CONTEXT['root'])
    PACKAGE = RUN / 'evidence'
    CONFIG = RUN / 'runtime.json' if (RUN/'runtime.json').is_file() else ROOT/'configs/case_runtime.json'
    if TASK_CONTEXT.get('runtime_authority'):
        AUTH = Path(TASK_CONTEXT['runtime_authority']['path'])
        BUDGET_DB = Path(TASK_CONTEXT['runtime_authority']['ledger_path'])
    else:
        AUTH = None
        BUDGET_DB = None


def now():
    return datetime.now(timezone.utc)


def utc_now():
    return now().isoformat()


def sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sha_value(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode('utf8')).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def inside_write(path):
    target = Path(path).resolve()
    if not target.is_relative_to(ROOT.resolve()) or target.is_relative_to((ROOT / 'V12_old').resolve()):
        raise ValueError('V12 write escapes the new version root')
    return target


def save_json(path, value, immutable=False):
    target = inside_write(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if immutable:
        try:
            with target.open('x', encoding='utf8') as handle:
                handle.write(body)
        except FileExistsError:
            if read_json(target) != value:
                raise ValueError('immutable V12 receipt differs: ' + target.name) from None
    else:
        temporary = target.with_name(target.name + '.tmp')
        temporary.write_text(body, encoding='utf8')
        temporary.replace(target)
    return value


def authorization():
    if TASK_CONTEXT is not None:
        return authority_for(load_request(TASK_CONTEXT['request_id']))
    expected = AUTH_WITNESS.read_text(encoding='ascii').strip()
    if len(expected) != 64 or sha_file(AUTH) != expected:
        raise ValueError('current V12 authorization digest drift')
    value = read_json(AUTH)
    if value.get('schema') != 'v12-success-case-authorization-1' or value.get('approval_quote') != '按照本次正确的顺序执行':
        raise ValueError('current V12 approval identity differs')
    if sha_file(REPO / value['approved_plan']['path']) != value['approved_plan']['sha256']:
        raise ValueError('approved V12 plan snapshot drift')
    if value['started_at_utc'] != '2026-10-19T10:40:38Z' or value['hard_deadline_utc'] != '2026-10-19T16:40:38Z':
        raise ValueError('V12 shared hard deadline differs')
    return value


def authorization_sha():
    authorization()
    return sha_file(AUTH)


def remaining():
    deadline = datetime.fromisoformat(authorization()['hard_deadline_utc'].replace('Z', '+00:00'))
    return (deadline - now()).total_seconds()


def require_time():
    if remaining() <= 0:
        raise RuntimeError('V12 shared six-hour wall-clock deadline reached')


def process_attempt(kind, detail):
    """Reserve only; B must mark_process_launched immediately after Popen."""
    from .case_budget import CaseBudget
    if TASK_CONTEXT is not None:
        require_execution(TASK_CONTEXT['request_id'])
    return CaseBudget().reserve_process(kind, detail)


def mark_process_launched(attempt_id, pid=None):
    from .case_budget import CaseBudget
    return CaseBudget().launch_process(attempt_id, pid)


def finish_process(attempt_id, result):
    from .case_budget import CaseBudget
    return CaseBudget().finish_process(attempt_id, result)


finish_stage = finish_process
