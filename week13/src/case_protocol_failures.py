"""Value-free classification of known protocol failures, never JSON repair."""
import json
import re
from .case_paths import sha_value
from .case_strict_tools import strict_json


class RepeatedProtocolFailure(RuntimeError):
    """A repeated known structural failure, distinct from budget exhaustion."""


def _json_shape(text, end):
    stack=[];quoted=False;escaped=False;mismatch=False
    for char in text[:end]:
        if quoted:
            if escaped:escaped=False
            elif char=='\\':escaped=True
            elif char=='"':quoted=False
        elif char=='"':quoted=True
        elif char in '{[':stack.append(char)
        elif char in '}]':
            if stack and char=={'{':'}','[':']'}[stack[-1]]:stack.pop()
            else:mismatch=True
    return dict(open_containers=stack,quoted=quoted,escape_pending=escaped,closing_type_mismatch=mismatch)


def failure_fingerprint(stage, error, response):
    """Return None for unclassified errors rather than merge arbitrary text.

    HTTP IDs, contents, costs and numeric character/line offsets are excluded.
    Old failure records can be classified read-only from their original reply.
    """
    if not isinstance(response,dict):return None
    calls=response.get('tool_calls',[])
    if not isinstance(calls,list):return None
    tools=[call.get('function',{}).get('name') for call in calls if isinstance(call,dict)]
    detail=None
    for call in calls:
        if not isinstance(call,dict):continue
        text=call.get('function',{}).get('arguments')
        if not isinstance(text,str):continue
        try:strict_json(text)
        except json.JSONDecodeError as exc:
            # Only the observed EOF family has a supported structural proof.
            # Interior syntax errors need a safe field path; do not over-merge.
            if exc.pos != len(text):return None
            char=text[exc.pos:exc.pos+1]
            token='EOF' if not char else char if char in '{}[],:"' else 'OTHER'
            detail=dict(family='JSON_SYNTAX',message=exc.msg,at_EOF=exc.pos==len(text),
                        encountered_token=token,shape=_json_shape(text,exc.pos))
            break
        except ValueError as exc:
            message=str(exc)
            if message.startswith('duplicate JSON key: '):
                detail=dict(family='JSON_DUPLICATE_KEY',key_sha256=sha_value(message.split(': ',1)[1]))
            elif message.startswith('nonfinite JSON number: '):detail=dict(family='JSON_NONFINITE')
            break
    if detail is None:
        message=str(error)
        recognized=('tool object fields missing:','tool object fields unexpected:','tool value type differs:',
                    'tool value enum differs:','tool value const differs:','tool local size constraint differs:',
                    'tool numeric bound differs:','tool numeric multiple differs:','tool string pattern differs:')
        families=sorted(prefix[:-1] for prefix in recognized if prefix in message)
        paths=sorted(set(re.sub(r'\[\d+\]','[]',path) for path in re.findall(r'\$(?:\.[A-Za-z_<>][A-Za-z0-9_<>-]*|\[\d+\])*',message)))
        if families and paths:detail=dict(family='SCHEMA',constraints=families,paths=paths)
    if detail is None:return None
    value=dict(stage=stage,tools=tools,detail=detail)
    return dict(schema='v12-protocol-fingerprint-1',signature=value,sha256=sha_value(value))


def reject_repeated_failure(stage, prior, previous):
    current=failure_fingerprint(stage,prior.get('error',''),prior.get('response'))
    if current is None:return
    for older in previous:
        old=failure_fingerprint(stage,older.get('error',''),older.get('response'))
        if old is not None and current['sha256']==old['sha256']:
            raise RepeatedProtocolFailure('同类协议错误重复，已停止自动修复；未新增修复扣数或产品调用。fingerprint='+current['sha256'])
