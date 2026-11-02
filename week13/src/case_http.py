"""One terminable HTTP child; credentials are program-internal, never logged."""
import json
import sys
from pathlib import Path
from week11.src.runtime import _api_key
from .case_budget import CaseBudget
from .case_paths import RUN, inside_write, save_json, read_json, sha_value, utc_now, authorization
from .case_stream import StreamRecorder, complete_stream
from .case_strict_tools import transport_profile, prepare_tools, FLAT_PLAN_PROFILE


def effective_transport(request, controls):
    """Resolve only the exact authority-bound endpoint; no env/base override."""
    profile = transport_profile(controls)
    supplied = request.get('transport_profile')
    if supplied is None:
        if profile['strict_tools']:
            raise ValueError('strict HTTP child requires explicit transport profile')
    elif supplied != profile:
        raise ValueError('HTTP child transport profile differs from authorization')
    tools = request['payload'].get('tools', [])
    if profile['strict_tools']:
        if profile['profile'] == FLAT_PLAN_PROFILE:
            internal=request.get('internal_tools')
            if not isinstance(internal,list):raise ValueError('flat child requires original internal tools')
            prepared,_=prepare_tools(internal,profile=profile['profile'])
        else:prepared, _ = prepare_tools(tools)
        if prepared != tools or any(tool['function'].get('strict') is not True for tool in tools):
            raise ValueError('HTTP child strict tools/schema differ from prepared payload')
    elif any(tool.get('function', {}).get('strict') for tool in tools):
        raise ValueError('strict tools cannot use legacy endpoint')
    return profile


def effective_timeouts(request,budget):
    controls,override=budget.effective_http_transport(request['request_id'])
    if any(request.get(name)!=value for name,value in controls.items()) or request.get('timeout_override')!=override:
        raise PermissionError('HTTP child timeout controls differ from original authority or one-time approved override')
    return controls,override


def main():
    request = json.load(sys.stdin)
    directory = inside_write(request['receipt_directory'])
    if not directory.is_relative_to((RUN / 'http').resolve()):
        raise ValueError('HTTP receipt directory is outside this execution run')
    budget = CaseBudget()
    recorder = StreamRecorder(directory, request_id=request['request_id'], payload_sha256=sha_value(request['payload']))
    try:
        row = budget.request(request['request_id'])
        payload = request['payload']
        if not row or row['content_hash'] != sha_value(payload) or row['status'] != 'reserved':
            raise ValueError('HTTP child payload does not match original reservation')
        if payload.get('model') != 'deepseek-flash' or payload.get('thinking') != {'type':'enabled'} or payload.get('reasoning_effort') != 'high' or payload.get('max_tokens') != 393216 or payload.get('tool_choice', 'auto') != 'auto':
            raise ValueError('HTTP child effective model controls differ')
        profile = effective_transport(request, authorization()['model_controls'])
        timeouts,override=effective_timeouts(request,budget)
        if profile['profile']==FLAT_PLAN_PROFILE:
            original=read_json(directory/'request.json')
            if original.get('request_id')!=request['request_id'] or original.get('payload')!=payload or original.get('internal_tools')!=request.get('internal_tools') or original.get('transport_profile')!=profile:
                raise ValueError('flat child original request/schema binding differs')
        save_json(directory / 'transport-profile.json', dict(schema='v12-http-child-transport-profile-1',
            request_id=request['request_id'], profile=profile, payload_sha256=sha_value(payload),
            automatic_retries=0, redirects=False, credential_source='PROGRAM_INTERNAL_LOADER_NOT_RECORDED',
            observation='CHILD_SELECTED_ENDPOINT_BEFORE_DISPATCH_NOT_SERVER_ECHO'), immutable=True)
        save_json(directory/'effective-timeouts.json',dict(schema='v12-http-effective-timeouts-1',request_id=request['request_id'],effective_transport=timeouts,
            timeout_override=override,hard_deadline_utc=budget.authority['hard_deadline_utc'],original_authorization_unchanged=True),immutable=True)
        key = _api_key()
        import httpx
        timeout = httpx.Timeout(request['read_timeout_s'], connect=request['connect_timeout_s'], read=request['read_timeout_s'])
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            response = complete_stream(client, url=profile['endpoint'],
                headers={'Authorization': 'Bearer ' + key}, payload=request['payload'], emit=recorder,
                request_id=request['request_id'], secret_values=(key,), on_http_error=recorder.record_http_error,
                no_progress_s=request['no_progress_timeout_s'], before_post=lambda: budget.launch_http(request['request_id']))
        response['transport_completed_at_utc'] = utc_now()
        save_json(directory / 'protocol-response.json', response, immutable=True)
        print(json.dumps(response, ensure_ascii=False))
        return 0
    except Exception as exc:
        # Keep only the exception class; arbitrary provider/credential strings
        # must never enter stdout or progress receipts.
        code = type(exc).__name__
        classification = 'NO_SUBSTANTIVE_PROGRESS_TIMEOUT' if str(exc) == 'NO_SUBSTANTIVE_PROGRESS_TIMEOUT' else 'TRANSPORT_ERROR'
        try:
            recorder('STREAM_TERMINATED', classification=classification, error_type=code)
        except Exception:
            pass  # A diagnostic write error must not print the original exception.
        try:
            save_json(directory / 'transport-error.json', dict(error_type=code), immutable=True)
        except Exception:
            pass
        print(json.dumps(dict(transport_error=code)))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
