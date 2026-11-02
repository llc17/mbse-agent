"""Bounded MCP diagnostics: types and code locations, never exception payloads.

These records describe observations only. In particular, a timeout or an empty
Store is not evidence that a dispatched mutation is safe to repeat.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sys
import time

_REPO = Path(__file__).resolve().parents[2]
_TOOLS = {'proposal_prepare', 'proposal_get', 'evidence_query', 'execution_start',
          'execution_status', 'versions_list', 'versions_compare', 'project_get',
          'session_state', 'initialize'}
_MESSAGES = {'MCP response error or invalid JSON block',
             'MCP response must contain one JSON text block',
             'MCP response rejected; payload omitted',
             'MCP tool error; payload omitted',
             'proposal mutation UNKNOWN; no blind resend'}


def _symbol(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9.]{0,159}', value) else 'unknown'


def tool_name(value):
    return value if value in _TOOLS else 'unknown_tool'


def _locations(exc):
    """No source lines, locals, arbitrary filenames or exception text."""
    frames = []
    tb = exc.__traceback__
    while tb is not None:
        path = Path(tb.tb_frame.f_code.co_filename)
        try:
            relative = path.resolve().relative_to(_REPO).as_posix()
        except (ValueError, OSError):
            relative = None
        if relative and relative.startswith('week13/src/'):
            frames.append(dict(path=relative, line=tb.tb_lineno,
                               function=_symbol(tb.tb_frame.f_code.co_name)))
        else:
            normalized = path.as_posix().replace('\\', '/')
            for library in ('mcp', 'anyio', 'asyncio'):
                marker = '/'+library+'/'
                if marker in normalized:
                    suffix = library+'/'+normalized.rsplit(marker, 1)[1]
                    if re.fullmatch(r'[A-Za-z_0-9./-]+\.py', suffix):
                        frames.append(dict(path=suffix, line=tb.tb_lineno,
                                           function=_symbol(tb.tb_frame.f_code.co_name)))
                    break
        tb = tb.tb_next
    return frames[-12:]


def exception_record(exc):
    """Keep outer ``error`` compatibility and add a bounded causal type tree."""
    seen = set()
    leaves = []
    nodes = [0]
    calls = []

    def visit(error, depth=0):
        name = _symbol(type(error).__name__)
        node = dict(type=name, module=_symbol(type(error).__module__),
                    traceback=_locations(error))
        if id(error) in seen or depth >= 8 or nodes[0] >= 32:
            node['truncated'] = True
            return node
        seen.add(id(error)); nodes[0] += 1
        for call in getattr(error, '_case_mcp_diagnostics', [])[-8:]:
            if call not in calls:calls.append(dict(call))
        if isinstance(error, BaseExceptionGroup):
            node['exceptions'] = [visit(child, depth+1) for child in error.exceptions[:16]]
            if len(error.exceptions) > 16:node['truncated'] = True
        else:
            leaves.append(name)
        cause = error.__cause__
        context = error.__context__ if not error.__suppress_context__ else None
        if cause is not None:node['cause'] = visit(cause, depth+1)
        elif context is not None:node['context'] = visit(context, depth+1)
        return node

    tree = visit(exc)
    name = tree['type']
    if isinstance(exc, BaseExceptionGroup):
        message = ('unhandled errors in a TaskGroup' if exc.message == 'unhandled errors in a TaskGroup'
                   else 'grouped failure')+' ('+str(len(exc.exceptions))+' sub-exception'+('s' if len(exc.exceptions) != 1 else '')+')'
    elif isinstance(exc, TimeoutError):message = 'operation timed out'
    elif name == 'CancelledError':message = 'operation cancelled'
    else:
        # Exact fixed program messages only; arbitrary exceptions may contain
        # credentials, HTTP bodies, user text or generated Modelica/SysML.
        args = exc.args
        message = args[0] if len(args) == 1 and isinstance(args[0], str) and args[0] in _MESSAGES else 'message omitted; see exception types and code locations'
    details = dict(schema='v12-safe-exception-details-1', exception=tree,
                   leaf_types=list(dict.fromkeys(leaves)), messages_omitted=True,
                   retry_authorized=False)
    if calls:details['mcp_calls'] = calls[-8:]
    return dict(error=name+': '+message, details=details)


def failure_record(exc):
    """Add details to old CLI failures; redact all newly instrumented MCP errors."""
    record = exception_record(exc)
    if not isinstance(exc, BaseExceptionGroup) and not record['details'].get('mcp_calls'):
        # Preserve unrelated existing CLI validation/repair contracts. New MCP
        # diagnostics never use arbitrary remote error bodies as error text.
        record['error'] = type(exc).__name__+': '+str(exc)
    return record


def attach_calls(exc, calls):
    """Carry observations across AnyIO's outer TaskGroup cleanup exception."""
    existing = list(getattr(exc, '_case_mcp_diagnostics', []))
    for call in calls:
        if call not in existing:existing.append(dict(call))
    try:exc._case_mcp_diagnostics = existing[-8:]
    except (AttributeError, TypeError):pass


def timeout_binding(authority, remaining_s, *, authority_sha256=None, authority_source='runtime_authority'):
    """Use approved MCP cap, preserving 120s when no explicit value exists."""
    configured = authority.get('timeouts_s', {}).get('mcp_request', 120)
    if type(configured) not in (int, float) or not math.isfinite(configured) or configured <= 0:
        raise ValueError('invalid approved MCP request timeout')
    if type(remaining_s) not in (int, float) or not math.isfinite(remaining_s) or remaining_s <= 0:
        raise TimeoutError('request deadline reached before MCP dispatch')
    return dict(configured_cap_s=configured, remaining_request_s=remaining_s,
                actual_cap_s=min(configured, remaining_s),
                cap_source=(_symbol(authority_source)+'.timeouts_s.mcp_request' if 'mcp_request' in authority.get('timeouts_s', {}) else 'default_120s'),
                authority_sha256=authority_sha256)


def _emit(value):
    try:print(json.dumps(value, ensure_ascii=False, allow_nan=False), file=sys.stderr, flush=True)
    except (OSError, ValueError, TypeError):pass  # Logging cannot change a mutation outcome.


@contextmanager
def phase(name, **metadata):
    """Log fixed phase names and timings; callers must pass bounded facts only."""
    name = _symbol(name)
    allowed = {'tool', 'transport', 'configured_cap_s', 'remaining_request_s', 'actual_cap_s',
               'cap_source', 'authority_sha256', 'rpc_dispatched', 'outcome',
               'call_invoked', 'rpc_dispatch_status', 'remote_cancellation_proven', 'mutation_outcome'}
    observed = {key:value for key,value in metadata.items() if key in allowed}
    started = time.monotonic()
    def event(suffix):
        now = time.monotonic()
        return dict(schema='v12-mcp-phase-1', mcp_phase=name+'_'+suffix,
                    updated_at_utc=datetime.now(timezone.utc).isoformat(),
                    monotonic=now, started_monotonic=started,
                    elapsed_s=max(0, now-started), **observed)
    _emit(event('ENTER'))
    try:
        yield observed
    except BaseException as exc:
        if 'rpc_dispatched' in observed:
            observed.setdefault('outcome', 'UNKNOWN_AFTER_CALL_INVOCATION' if observed.get('call_invoked') else 'NOT_DISPATCHED')
            if observed.get('tool') in ('proposal_prepare', 'execution_start'):
                observed['mutation_outcome'] = 'UNKNOWN' if observed.get('call_invoked') else 'NOT_DISPATCHED'
        value=event('ERROR'); value.update(exception_record(exc)); _emit(value)
        raise
    else:_emit(event('RETURN'))
