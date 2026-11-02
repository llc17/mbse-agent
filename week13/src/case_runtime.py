"""Current-scope product runtime copied/thin-adapted from ConsultRuntime.

Reuses V10's input estimate/credential loader and reservation/cache/no-resend
protocol. Archived modules are not imported or run. All modes share this class.
"""
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import MappingProxyType
import json
import os
import re
import subprocess
import sys
import time
import threading
from week11.src.runtime import DeepSeekRuntime, _api_key, _estimate_input
from .case_paths import CONFIG, BUDGET_DB, RUN, REPO, TASK_CONTEXT, authorization, authorization_sha, read_json, save_json, sha_file, sha_value, remaining, require_time, utc_now
from .case_requests import load_request, authority_for, require_planning, SELECTOR
from .case_budget import CaseBudget
from .case_stream import validate_usage
from .case_progress import emit_progress
from .case_strict_tools import transport_profile, prepare_tools, validate_calls, decode_calls, strict_json, generation_suffix_calls, STRICT_PROFILE, FLAT_PLAN_PROFILE, EMPTY_GUIDE, plan_transport_guide

_MODELS = {'deepseek-flash', 'DeepSeek-V4.1-Flash', 'deepseek-v4.1-flash', 'DeepSeek-V4.1-Flash-1017', 'deepseek-v4.1-flash-1017'}


class ProductHTTPError(RuntimeError):
    def __init__(self,details):
        self.transport_failure=details
        super().__init__('product HTTP interrupted/uncertain; '+details['classification']+'; '+details['message']+'; reservation retained, no automatic resend')


def transport_failure(directory,request_id,exc):
    """Safe local fields only; never copy provider text, credentials or reasoning."""
    directory=Path(directory)
    def saved(name):
        try:return read_json(directory/name)
        except (OSError,ValueError):return {}
    error=saved('transport-error.json');timing=saved('timing.json');exit=saved('process-exit.json')
    kind=error.get('error_type',type(exc).__name__)
    if not isinstance(kind,str) or not kind.isidentifier() or len(kind)>96:kind='TransportError'
    chunks=timing.get('receive_chunk_count',0)
    observed=type(chunks) is int and chunks>0
    classification=exit.get('timeout_classification') or {
        'ConnectTimeout':'CONNECT_TIMEOUT','ConnectError':'CONNECT_ERROR',
        'ReadTimeout':'READ_IDLE_TIMEOUT','ReadError':'STREAM_INTERRUPTED','RemoteProtocolError':'STREAM_INTERRUPTED',
    }.get(kind,'HTTP_TRANSPORT_ERROR')
    if str(exc) in ('HARD_WALL_TIMEOUT','WHOLE_REQUEST_TIMEOUT','NO_SUBSTANTIVE_PROGRESS_TIMEOUT'):classification=str(exc)
    message={
        'CONNECT_TIMEOUT':'连接服务超时', 'CONNECT_ERROR':'连接服务失败',
        'READ_IDLE_TIMEOUT':'等待下一响应数据块超时', 'STREAM_INTERRUPTED':'响应流中途断开',
        'HARD_WALL_TIMEOUT':'原总截止已到','WHOLE_REQUEST_TIMEOUT':'单次HTTP总时限已到',
        'NO_SUBSTANTIVE_PROGRESS_TIMEOUT':'无实质进展时限已到，心跳不计进展',
    }.get(classification,'HTTP通信未完成')
    return dict(schema='v12-safe-http-failure-1',request_id=request_id,error_type=kind,classification=classification,message=message,
        phase='READING_RESPONSE' if observed else 'WAITING_FOR_RESPONSE' if kind=='ReadTimeout' else 'CONNECTING' if kind in ('ConnectError','ConnectTimeout') else 'HTTP_TRANSPORT',
        saved_complete_response=(directory/'protocol-response.json').is_file(),receive_chunk_count=chunks if type(chunks) is int else None,
        remote_delivery_and_charge='UNKNOWN',automatic_resends=0,
        affected_stage='plan' if request_id.startswith('case_plan_') else 'product_http',
        recovery_entry='/recover response；无完整回复时 /recover plan（须明确批准一次替换，可能重复计费）' if request_id.startswith('case_plan_') else '/recover response',
        model_generation_or_simulation_started_by_failed_plan=False if request_id.startswith('case_plan_') else None)


def wait_for_http_child(process, request_text, directory, *, request_id, whole_timeout_s,
                        no_progress_timeout_s, remaining_s, clock=time.monotonic,
                        now=utc_now, poll_s=1.0, launched_monotonic=None, launched_at_utc=None):
    """Parent-side watchdog; a blocked body read cannot suspend the deadline.

    Monotonic values are recorded by local Python processes on the same host.
    No stdout/stderr/provider body is copied to watchdog diagnostics.
    """
    directory = Path(directory)
    launched = clock() if launched_monotonic is None else launched_monotonic
    launched_utc, launch_observed_utc = launched_at_utc or now(), now()
    last_substantive = launched
    classification, requested, output = None, False, None
    termination_requested_at_utc = None
    original_error = None
    receipt_read_conflicts = 0
    completed, communication = threading.Event(), {}
    def communicate():
        try:
            communication['output'], _ = process.communicate(request_text)
        except BaseException as exc:
            communication['error'] = exc
        finally:
            completed.set()
    worker = threading.Thread(target=communicate, name='v12-http-child-io', daemon=True)
    try:
        worker.start()
        while True:
            timing_path = directory / 'timing.json'
            if timing_path.is_file():
                try:
                    timing = read_json(timing_path)
                except (PermissionError, FileNotFoundError):
                    # An atomic replace may briefly deny opens on Windows.
                    # Keep the previous clock; a read conflict never renews it.
                    timing = None
                    receipt_read_conflicts += 1
                if timing is not None:
                    observed = timing.get('last_substantive_monotonic')
                    if timing.get('schema') != 'v12-http-stream-timing-1' or timing.get('request_id') != request_id:
                        raise ValueError('HTTP timing receipt identity differs')
                    if observed is not None:
                        if type(observed) not in (int, float) or not launched <= observed <= clock():
                            raise ValueError('HTTP substantive progress clock differs')
                        last_substantive = max(last_substantive, observed)
            current = clock()
            wall_left = remaining_s()
            whole_left = whole_timeout_s - (current - launched)
            progress_left = no_progress_timeout_s - (current - last_substantive)
            if wall_left <= 0:
                classification = 'HARD_WALL_TIMEOUT'
            elif whole_left <= 0:
                classification = 'WHOLE_REQUEST_TIMEOUT'
            elif progress_left <= 0:
                classification = 'NO_SUBSTANTIVE_PROGRESS_TIMEOUT'
            if classification:
                raise TimeoutError(classification)
            # communicate's stdin write may itself block on Windows. Keeping it
            # in a separate thread leaves the parent watchdog independent of I/O.
            if completed.wait(min(poll_s, wall_left, whole_left, progress_left)):
                if 'error' in communication:
                    raise communication['error']
                output = communication.get('output')
                break
    except BaseException as exc:
        original_error = exc
        classification = classification or 'PARENT_INTERRUPTED'
        if process.poll() is None:
            requested = True
            termination_requested_at_utc = now()
            process.kill()
        try:
            # Reap the controlled child before any unrelated step may proceed.
            process.wait(timeout=5)
            worker.join(timeout=5)
        except BaseException:
            pass
    finally:
        returncode = process.poll()
        child_error_type = None
        error_path = directory / 'transport-error.json'
        if error_path.is_file():
            saved_error = read_json(error_path).get('error_type')
            if isinstance(saved_error, str) and saved_error.isidentifier() and len(saved_error) <= 96:
                child_error_type = saved_error
                classification = classification or {'ReadTimeout':'READ_IDLE_TIMEOUT', 'ConnectTimeout':'CONNECT_TIMEOUT'}.get(saved_error)
        timing_path = directory / 'timing.json'
        if classification is None and timing_path.is_file():
            if read_json(timing_path).get('classification') == 'NO_SUBSTANTIVE_PROGRESS_TIMEOUT':
                classification = 'NO_SUBSTANTIVE_PROGRESS_TIMEOUT'
        status_observed_at_utc = now()
        proof = dict(schema='v12-local-http-child-exit-1', request_id=request_id,
            pid=process.pid, returncode=returncode,
            process_launched=True, fixture_transport=False,
            local_process_terminated=type(returncode) is int,
            termination_requested=requested, timeout_classification=classification,
            termination_requested_at_utc=termination_requested_at_utc,
            child_error_type=child_error_type,
            receipt_read_conflicts=receipt_read_conflicts,
            local_spawn_started_at_utc=launched_utc,
            local_launch_observed_at_utc=launch_observed_utc,
            process_status_observed_at_utc=status_observed_at_utc,
            observed_exit_at_utc=status_observed_at_utc if type(returncode) is int else None,
            elapsed_s=max(0, clock()-launched), automatic_resends=0)
        save_json(directory / 'process-exit.json', proof, immutable=True)
    if original_error is not None:
        raise original_error
    if type(returncode) is not int:
        raise RuntimeError('HTTP_CHILD_EXIT_UNCONFIRMED; no following step permitted')
    return output


class CaseRuntime(DeepSeekRuntime):
    def __init__(self, config_path=None, ledger_path=None, *, transport=None, reopen=True, task_context=None):
        supplied=task_context or TASK_CONTEXT
        self.task_context=load_request(supplied['request_id']) if supplied else None
        self.authorization = authority_for(self.task_context) if self.task_context else authorization()
        self.run=Path(self.task_context['root']) if self.task_context else RUN
        canonical=Path(self.authorization['ledger_path']) if self.task_context else BUDGET_DB
        self.config_path = Path(config_path or CONFIG)
        self.ledger_path = Path(ledger_path or canonical)
        config = read_json(self.config_path)
        if config != self.authorization['model_controls']:
            raise ValueError('V12 runtime config differs from the actual authorized controls')
        if config['thinking'] != 'enabled' or config['max_tokens'] != 393216 or config['model'] != 'deepseek-flash' or config['reasoning_effort'] != 'high':
            raise ValueError('all V12 product entries require enabled/high/393216')
        if transport is None and self.authorization.get('test_only'):
            raise PermissionError('test-only authorization cannot dispatch real product HTTP')
        if transport is None and self.ledger_path.resolve() != canonical.resolve():
            raise ValueError('production product entries must use the single shared budget ledger')
        if transport is not None and self.ledger_path.resolve() == canonical.resolve() and not (self.task_context and self.authorization.get('test_only') is True):
            raise ValueError('fixture transport must use an isolated test ledger, never the product ledger')
        self.config = MappingProxyType(config)
        self.profile = transport_profile(config)
        self.transport = transport
        self.budget = CaseBudget(self.ledger_path,task_context=self.task_context)

    def _authorization_sha(self):
        if self.task_context: authority_for(load_request(self.task_context['request_id']))
        return sha_file(self.task_context['runtime_authority']['path']) if self.task_context else authorization_sha()

    def _remaining(self):
        if not self.task_context: return remaining()
        value=authority_for(load_request(self.task_context['request_id']))
        return (datetime.fromisoformat(value['hard_deadline_utc'].replace('Z','+00:00'))-datetime.now(timezone.utc)).total_seconds()

    def _require_time(self):
        self.budget._require_time()

    def require_credentials(self):
        if self.transport is not None:
            return
        try:
            _api_key()
        except Exception:
            raise RuntimeError('MISSING_CREDENTIAL: program-internal loader found no usable product key; no HTTP') from None

    def _send(self, *args, **kwargs):
        # The inherited V10 sender hard-coded disabled. Never permit a caller
        # to accidentally bypass this runtime's enabled, budgeted transport.
        raise RuntimeError('legacy direct transport is unavailable; use CaseRuntime.complete')

    def _category(self, request_id):
        lowered = request_id.lower()
        if lowered.startswith('case_plan_'): return 'design'
        # TaskFlow alone emits this stage after binding the ordinary user's
        # question to this request's terminal Store result. The historical
        # reopen reservation is a result-review exit, not proof of cold reopen.
        if re.match(r'^case_result_followup_\d+_',lowered):return 'reopen'
        if re.match(r'^case_ask_\d+_',lowered):return 'consultation'
        if any(word in lowered for word in ('reopen', 'restart')):
            return 'reopen'
        if any(word in lowered for word in ('explain', 'explanation', 'synthesis')):
            return 'explanation'
        if any(word in lowered for word in ('consult', 'ask', 'question', 'followup')):
            return 'consultation'
        return 'generation' if any(word in lowered for word in ('generat', 'source', 'repair', 'correct')) else 'design'

    def build_payload(self, messages, tools):
        if not isinstance(messages, list) or not messages or not isinstance(tools, list):
            raise ValueError('messages/tools must be valid lists')
        if self.authorization.get('operation_policy', {}).get('mode') == 'READONLY_VALIDATION':
            context = self.task_context or {}
            if context.get('access_mode') != 'RESULT_REVIEW_ONLY':
                raise PermissionError('readonly authority requires a result review context')
            step = context.get('step_id')
            if step in ('explain', 'reopen'):
                if tools:
                    raise PermissionError('result explanation and reopen cannot offer tools')
            elif step in ('negative_mass', 'negative_geometry'):
                names = [tool.get('function', {}).get('name') for tool in tools if isinstance(tool, dict)]
                if len(tools) != 1 or names != ['case_request_plan'] or tools[0].get('type') != 'function':
                    raise PermissionError('readonly negative checks permit only case_request_plan')
            else:
                raise PermissionError('unrecognized readonly validation step')
        if tools:
            for message in messages:
                if message.get('role') == 'assistant' and not isinstance(message.get('reasoning_content'), str):
                    raise ValueError('thinking tool history must retain full assistant reasoning_content, including no-call rounds')
        profile = transport_profile(self.config)
        effective_tools, adapter = prepare_tools(tools,profile=profile['profile']) if profile['strict_tools'] else (tools, [])
        if profile['profile']==FLAT_PLAN_PROFILE:
            plans=[tool for tool in tools if tool['function']['name']=='case_request_plan']
            if plans:
                from copy import deepcopy
                messages=deepcopy(messages)
                guide=plan_transport_guide(plans[0])
                for message in messages:
                    if message.get('role')=='system' and ('packet恰含' in message.get('content','') or 'design={' in message.get('content','')):
                        raise ValueError('nested PLAN prompt cannot be sent with flat profile')
                if messages[0].get('role')!='system':messages.insert(0,dict(role='system',content=guide))
                elif guide not in messages[0]['content']:messages[0]['content']+='\n'+guide
        if profile['strict_tools'] and tools and any('DECLARED_EMPTY_OBJECT_TO_CLOSED_TRANSPORT_MARKER' in str(item) for item in adapter):
            from copy import deepcopy
            messages=deepcopy(messages)
            if messages and messages[0].get('role')=='system':
                if not messages[0]['content'].endswith(EMPTY_GUIDE):messages[0]['content']+=EMPTY_GUIDE
            else:messages.insert(0,dict(role='system',content=EMPTY_GUIDE.lstrip()))
        estimate = _estimate_input(messages, effective_tools)
        if estimate >= self.config['context_window_tokens']:
            raise ValueError('conservative full-input estimate reaches the nominal shared 1M window; no HTTP, no truncation')
        # No extra 16/32/64K input cap. Provider enforces the actual shared
        # window; known actual prompt+completion is checked again below.
        payload = dict(model='deepseek-flash', messages=messages, thinking=dict(type='enabled'), reasoning_effort='high',
                       max_tokens=393216, stream=True, stream_options=dict(include_usage=True))
        if tools:
            payload.update(tools=effective_tools, tool_choice='auto')
        return payload, estimate

    def _send_payload(self, payload, request_id, directory):
        if self.transport is not None:
            self.budget.launch_http(request_id,fixture=True)
            try:
                return self.transport(payload, None, min(self.config['whole_request_deadline_s'], self._remaining()))
            finally:
                if self.task_context and self.authorization.get('operation_policy', {}).get('mode') == 'READONLY_VALIDATION':
                    save_json(directory / 'process-exit.json', dict(schema='v12-local-http-child-exit-1',
                        request_id=request_id, pid=None, returncode=None, process_launched=False,
                        fixture_transport=True, fixture_call_returned=True,
                        local_process_terminated=False, termination_requested=False,
                        timeout_classification=None, observed_exit_at_utc=None,
                        fixture_call_returned_at_utc=utc_now(), automatic_resends=0), immutable=True)
        effective,override=self.budget.effective_http_transport(request_id)
        request = dict(payload=payload, request_id=request_id, receipt_directory=str(directory),
                       transport_profile=transport_profile(self.config),
                       **effective,timeout_override=override)
        if self.config.get('tool_output_profile') == FLAT_PLAN_PROFILE:
            request['internal_tools'] = read_json(directory/'request.json')['internal_tools']
        command = [sys.executable, '-X', 'utf8', '-B', '-m', 'week13.src.case_http']
        environment=dict(os.environ,PYTHONDONTWRITEBYTECODE='1')
        if self.task_context: environment[SELECTOR]=self.task_context['request_id']
        launched_monotonic, launched_at_utc = time.monotonic(), utc_now()
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf8', cwd=REPO, env=environment)
        output = wait_for_http_child(process, json.dumps(request, ensure_ascii=False), directory,
            request_id=request_id, whole_timeout_s=effective['whole_request_deadline_s'],
            no_progress_timeout_s=effective['no_progress_timeout_s'], remaining_s=self._remaining,
            launched_monotonic=launched_monotonic, launched_at_utc=launched_at_utc)
        # A completed child receipt is a readback, never a repeated request.
        response_path = directory / 'protocol-response.json'
        if response_path.is_file():
            return read_json(response_path)
        try:
            response = json.loads(output)
        except (ValueError, TypeError):
            raise RuntimeError('HTTP_CHILD_INCOMPLETE; readback only, no resend') from None
        if process.returncode or not isinstance(response, dict) or response.get('transport_error'):
            raise RuntimeError('HTTP_CHILD_TRANSPORT_ERROR; reservation retained')
        return response

    def _usage_cost(self, response):
        usage = validate_usage(response.get('usage'))
        prompt, output = usage['prompt_tokens'], usage['completion_tokens']
        hit = usage.get('prompt_cache_hit_tokens')
        if hit is None:
            hit = usage.get('prompt_tokens_details', {}).get('cached_tokens', 0)
        if type(hit) is not int or not 0 <= hit <= prompt:
            raise ValueError('invalid cached prompt usage')
        # The approved live window is 18:40..00:40 Beijing, wholly off-peak.
        # General weekday daytime holiday status/cross-band rules are unknown:
        # use a labelled peak upper bound rather than invent holiday calendars.
        factor, band, basis = 1.0, 'peak_upper_bound', 'published_tariff_upper_bound'
        started = getattr(self, '_active_started_at', None)
        completed_at = response.get('transport_completed_at_utc')
        ended = datetime.fromisoformat(completed_at.replace('Z', '+00:00')) if isinstance(completed_at, str) else datetime.now(timezone.utc)
        if started:
            start = datetime.fromisoformat(started.replace('Z', '+00:00'))
            auth_start = datetime.fromisoformat(self.authorization['started_at_utc'].replace('Z', '+00:00'))
            auth_end = datetime.fromisoformat(self.authorization['hard_deadline_utc'].replace('Z', '+00:00'))
            # Off-peak is certain only if every Beijing minute is outside
            # weekday daytime bands. Holidays can only lower the upper bound.
            cursor=start.astimezone(timezone(timedelta(hours=8)))
            finish=ended.astimezone(timezone(timedelta(hours=8)))
            peak=False
            while cursor<=finish:
                hour=cursor.hour+cursor.minute/60
                if cursor.weekday()<5 and (9<=hour<12 or 14<=hour<18): peak=True; break
                cursor+=timedelta(minutes=1)
            if auth_start <= start <= ended <= auth_end and not peak:
                factor, band, basis = 0.5, 'offpeak', 'published_tariff_calculation'
        self._active_tariff = dict(band=band, factor=factor, cost_basis=basis,
            official_checked_date='2026-10-19', official_source='https://api-docs.deepseek.com/zh-cn/quick_start/pricing/',
            billing_invoice_claimed=False, request_started_at_utc=started, usage_received_at_utc=ended.isoformat())
        cost = factor * ((prompt - hit) * self.config['peak_input_cny_per_million'] + hit * self.config['peak_cached_input_cny_per_million'] + output * self.config['peak_output_cny_per_million']) / 1_000_000
        return usage, cost

    def _parse(self, response):
        if not isinstance(response, dict) or response.get('model') not in _MODELS or not isinstance(response.get('id'), str):
            raise ValueError('invalid provider model/response identity')
        usage, cost = self._usage_cost(response)
        if usage['total_tokens'] > self.config['context_window_tokens'] or usage['completion_tokens'] > self.config['max_tokens']:
            raise ValueError('provider actual usage exceeds shared window or output reservation')
        choices = response.get('choices')
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0].get('message'), dict):
            raise ValueError('invalid provider choices')
        choice, message = choices[0], response['choices'][0]['message']
        calls, content = message.get('tool_calls', []), message.get('content')
        if choice.get('finish_reason') not in ('stop', 'tool_calls'):
            raise ValueError('provider response incomplete/rejected: ' + str(choice.get('finish_reason')))
        if not isinstance(calls, list) or content is not None and not isinstance(content, str) or not (content or calls):
            raise ValueError('invalid/empty provider message')
        if (choice['finish_reason'] == 'tool_calls') != bool(calls):
            raise ValueError('finish_reason and tool calls disagree')
        parsed_calls,suffix=generation_suffix_calls(calls,choice['finish_reason'])
        identifiers = set()
        for call in parsed_calls:
            function = call.get('function', {}) if isinstance(call, dict) else {}
            if not isinstance(call, dict) or call.get('type') != 'function' or not isinstance(call.get('id'), str) or not call['id'] or call['id'] in identifiers or not isinstance(function, dict) or not isinstance(function.get('name'), str) or not isinstance(function.get('arguments'), str):
                raise ValueError('invalid provider tool call')
            identifiers.add(call['id'])
            # A known single surplus envelope brace may have been removed;
            # complete finite JSON and business schema/semantics still apply.
            decoded = strict_json(function['arguments'])
            if not isinstance(decoded, dict):
                raise ValueError('tool arguments must decode once to an object')
        reasoning = message.get('reasoning_content')
        if reasoning is not None and not isinstance(reasoning, str):
            raise ValueError('invalid reasoning protocol text')
        assistant = dict(role='assistant', content=content or '', reasoning_content=reasoning or '')
        if calls:
            assistant['tool_calls'] = calls
        result = dict(content=content or '', tool_calls=parsed_calls, usage=usage, response_model=response['model'],
                      response_id=response['id'], finish_reason=choice['finish_reason'], request_thinking='enabled',
                      request_reasoning_effort='high', reasoning_content=reasoning or '', assistant_message=assistant,
                      reasoning_field_present=response.get('reasoning_field_present', 'reasoning_content' in message))
        if suffix:result.update(wire_tool_calls=calls,generation_arguments_normalization=suffix)
        return result, usage, cost

    def complete(self, messages, tools, request_id):
        if not isinstance(request_id, str) or not request_id:
            raise ValueError('nonempty request_id required')
        payload, estimate = self.build_payload(messages, tools)
        content_hash = sha_value(payload)
        old = self.budget.request(request_id)
        if old:
            if old['content_hash'] != content_hash:
                raise ValueError('request_id content/settings conflict')
            if old['status'] == 'succeeded':
                if transport_profile(self.config)['strict_tools'] and (self.transport is None or self.task_context):
                    saved = read_json(self.run / 'http' / sha_value(request_id) / 'request.json')
                    self._validate_saved_profile(saved)
                    if saved.get('internal_tools') != tools:
                        raise ValueError('cached strict request internal schema conflict')
                return json.loads(old['response'])
            raise RuntimeError('saved failed/unknown/in-flight request; readback only, automatic resend forbidden')
        self._require_time()
        if self.task_context: require_planning(self.task_context['request_id'])
        self.require_credentials()
        category = self._category(request_id)
        self.budget.reserve_http(request_id, content_hash, category, self.config)
        directory = self.run / 'http' / sha_value(request_id)
        if self.transport is not None and not self.task_context:
            directory = self.ledger_path.parent / 'fixture-http' / sha_value(request_id)
        # Test fixtures use isolated ledgers/receipts, never live HTTP counters.
        if self.transport is None or self.task_context:
            save_json(directory / 'request.json', dict(request_id=request_id, payload=payload, payload_sha256=content_hash,
                transport_profile=transport_profile(self.config),
                internal_tools=tools if transport_profile(self.config)['strict_tools'] else None,
                tool_schema_adapter=prepare_tools(tools,profile=transport_profile(self.config)['profile'])[1] if transport_profile(self.config)['strict_tools'] else [],
                authorization_sha256=self._authorization_sha(), estimated_input=estimate,
                transport_source_sha256={name:sha_file(REPO/name) for name in ('week13/src/case_http.py','week13/src/case_stream.py')},
                fixture_transport=self.transport is not None,
                outbound_settings_observed=True, provider_echo_of_settings_claimed=False, recorded_at_utc=utc_now()), immutable=True)
        try:
            response = self._send_payload(payload, request_id, directory)
        except BaseException as exc:
            self.budget.finish_http(request_id, error=type(exc).__name__, uncertain=True)
            details=transport_failure(directory,request_id,exc)
            if self.transport is None or self.task_context:save_json(directory/'failure-summary.json',details,immutable=True)
            raise ProductHTTPError(details) from None
        known_usage = known_cost = None
        emit_progress('protocol_validation','产品HTTP通信已结束，正在解析协议与用量回执。',http_request_id=request_id)
        self._active_started_at = self.budget.request(request_id)['launched_at']
        try:
            known_usage, known_cost = self._usage_cost(response)
            result, usage, cost = self._parse(response)
            if transport_profile(self.config)['strict_tools']:
                actual_profile=transport_profile(self.config)['profile']
                raw_calls=result['tool_calls'];result['tool_calls'],mapping=decode_calls(raw_calls,tools,profile=actual_profile)
                if mapping:
                    mapping_key='wire_structure_mapping' if actual_profile==FLAT_PLAN_PROFILE else 'wire_empty_object_mapping'
                    result.setdefault('wire_tool_calls',raw_calls);result.update(**{mapping_key:mapping})
                    name='wire-plan-structure-mapping.json' if actual_profile==FLAT_PLAN_PROFILE else 'wire-empty-object-normalization.json'
                    audit_schema='v12-strict-plan-structure-mapping-1' if actual_profile==FLAT_PLAN_PROFILE else 'v12-strict-empty-object-normalization-1'
                    if self.transport is None or self.task_context:save_json(directory/name,dict(schema=audit_schema,request_id=request_id,payload_sha256=content_hash,mapping=mapping,original_protocol_response_preserved=True),immutable=True)
                result.update(request_tool_output_profile=actual_profile,
                              request_endpoint=transport_profile(self.config)['endpoint'])
        except Exception as exc:
            self.budget.finish_http(request_id, tokens=known_usage['total_tokens'] if known_usage else None,
                output=known_usage['completion_tokens'] if known_usage else None, cost=known_cost,
                error=type(exc).__name__ + ': ' + str(exc), uncertain=known_usage is None)
            if self.transport is None or self.task_context:
                save_json(directory / 'rejected-response.json', dict(response=response, error_type=type(exc).__name__,
                    usage_known=known_usage is not None, fixture_transport=self.transport is not None, reasoning_is_protocol_data=True), immutable=True)
            raise
        if not tools and result['tool_calls']:
            self.budget.finish_http(request_id, tokens=usage['total_tokens'], output=usage['completion_tokens'], cost=cost, error='unoffered tools')
            raise ValueError('provider returned unoffered tools; no dispatch')
        emit_progress('save_response','产品回复协议已解析，正在保存调用回执。',http_request_id=request_id)
        self.budget.finish_http(request_id, response=result, tokens=usage['total_tokens'], output=usage['completion_tokens'], cost=cost)
        if self.transport is None or self.task_context:
            save_json(directory / 'wire-evidence.json', dict(request_id=request_id, model=response['model'],
                transport_profile=transport_profile(self.config),
                finish_reason=result['finish_reason'], usage=usage, reasoning_field_present=result['reasoning_field_present'],
                reasoning_content_sha256=sha_value(result['reasoning_content']), provider_echo_of_settings_claimed=False,
                cost_cny=cost, tariff=self._active_tariff, received_at_utc=utc_now()), immutable=True)
        return result

    def recover_saved_response(self, request_id):
        """Explicit zero-HTTP reconciliation after parent interruption.

        Only a complete immutable request plus terminal protocol response can
        establish the result. SSE fragments/partial text are never guessed.
        Readback remains possible after the hard deadline; no new execution is
        authorized by reconciliation itself.
        """
        current_sha = self._authorization_sha()
        old = self.budget.request(request_id)
        if old is None:
            raise ValueError('no original request to recover')
        if old['status'] == 'succeeded':
            return json.loads(old['response'])
        directory = self.run / 'http' / sha_value(request_id)
        if self.transport is not None and not self.task_context:
            directory = self.ledger_path.parent / 'fixture-http' / sha_value(request_id)
        request_path, response_path = directory / 'request.json', directory / 'protocol-response.json'
        if not request_path.is_file() or not response_path.is_file():
            raise RuntimeError('complete saved request/response absent; remain UNKNOWN, zero HTTP')
        saved = read_json(request_path)
        payload = saved.get('payload')
        if saved.get('request_id') != request_id or saved.get('authorization_sha256') != current_sha or not isinstance(payload, dict):
            raise ValueError('saved request authorization/identity mismatch')
        self._validate_saved_profile(saved)
        internal_tools = saved.get('internal_tools') if transport_profile(self.config)['strict_tools'] else payload.get('tools', [])
        if internal_tools is None:
            raise ValueError('saved strict request lacks original internal schema')
        rebuilt, _ = self.build_payload(payload['messages'], internal_tools)
        content_hash = sha_value(payload)
        if rebuilt != payload or saved.get('payload_sha256') != content_hash or old['content_hash'] != content_hash:
            raise ValueError('saved request payload/settings hash mismatch')
        self._active_started_at = old['launched_at']
        response = read_json(response_path)
        result, usage, cost = self._parse(response)
        if transport_profile(self.config)['strict_tools']:
            actual_profile=transport_profile(self.config)['profile']
            raw_calls=result['tool_calls'];result['tool_calls'],mapping=decode_calls(raw_calls,internal_tools,profile=actual_profile)
            if mapping:
                result.setdefault('wire_tool_calls',raw_calls);result.update(**{'wire_structure_mapping' if actual_profile==FLAT_PLAN_PROFILE else 'wire_empty_object_mapping':mapping})
            result.update(request_tool_output_profile=actual_profile,
                          request_endpoint=transport_profile(self.config)['endpoint'])
        if not payload.get('tools') and result['tool_calls']:
            raise ValueError('saved reply includes unoffered tools')
        proof = dict(schema='v12-complete-response-readback-1', request_id=request_id,
            authorization_sha256=current_sha, original_payload_sha256=content_hash,
            request_file_sha256=sha_file(request_path), response_file_sha256=sha_file(response_path),
            original_response_id=result['response_id'], model=result['response_model'],
            usage=usage, finish_reason=result['finish_reason'], tariff=self._active_tariff,
            new_http_requests=0, source='complete saved request and protocol response, not SSE fragments')
        self.budget.recover_http(request_id, content_hash, result, usage['total_tokens'], usage['completion_tokens'], cost, proof)
        if self.transport is None:
            save_json(directory / 'readback-recovery.json', proof, immutable=True)
        else:
            (directory / 'readback-recovery.json').write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding='utf8')
        return result

    def _validate_saved_profile(self, saved):
        """No rebuilding an old request under today's different wire profile."""
        from .case_strict_tools import LEGACY_PROFILE
        current = transport_profile(self.config)
        previous = saved.get('transport_profile')
        if previous is None:
            if current['profile'] != LEGACY_PROFILE:
                raise ValueError('saved legacy request cannot migrate to strict profile')
        elif previous != current:
            raise ValueError('saved request transport profile/endpoint mismatch')

    def authorize_http_rejection_diagnostic(self, original_request_id, diagnostic_request_id, primary_review_path, independent_review_path):
        """Append one proof/repair event; no HTTP and no global unfreeze."""
        self._require_time()
        if not self.task_context:raise PermissionError('bound current request required')
        directory=self.run/'http'/sha_value(original_request_id)
        saved=read_json(directory/'request.json');self._validate_saved_profile(saved)
        rebuilt,_=self.build_payload(saved['payload']['messages'],saved.get('internal_tools',[]))
        old=self.budget.request(original_request_id)
        if old is None:raise ValueError('original HTTP row absent')
        from .case_http_rejection import prepare_rejection_proof
        proof=prepare_rejection_proof(self.task_context,self.authorization,original_request_id,diagnostic_request_id,
            primary_review_path,independent_review_path,old,rebuilt)
        proof=self.budget.authorize_http_rejection_diagnostic(original_request_id,diagnostic_request_id,sha_value(rebuilt),proof)
        path=directory/'http400-diagnostic-authorization.json'
        save_json(path,proof,immutable=True)
        return dict(proof_path=str(path),proof_sha256=sha_file(path),diagnostic_request_id=diagnostic_request_id,
                    original_row_unchanged=True,original_freeze_preserved=True,HTTP_launched=False)

    def authorize_http_empty_object_correction(self,primary_review_path):
        self._require_time()
        if not self.task_context:raise PermissionError('bound current request required')
        original='case_plan_'+self.task_context['request_id'];diagnostic=original+'_http400_diagnostic_1'
        saved=read_json(self.run/'http'/sha_value(original)/'request.json');self._validate_saved_profile(saved)
        payload,_=self.build_payload(saved['payload']['messages'],saved['internal_tools'])
        from .case_http_rejection import prepare_empty_object_correction
        proof=prepare_empty_object_correction(self.task_context,self.authorization,primary_review_path,payload,
            {rid:self.budget.request(rid) for rid in (original,diagnostic)})
        self.budget.authorize_http_schema_correction(proof['correction_request_id'],sha_value(payload),proof)
        path=self.run/'http'/sha_value(original)/'empty-object-correction-authorization.json';save_json(path,proof,immutable=True)
        return dict(proof_path=str(path),proof_sha256=sha_file(path),correction_request_id=proof['correction_request_id'],HTTP_launched=False)

    def authorize_connect_retry(self, old_request_id, new_request_id, messages, tools):
        """Explicit same-payload new RID only for connection setup failures.

        Retains the original UNKNOWN and full conservative reservation. Source
        inspection establishes the no-origin-body boundary; absence of a reply
        alone is never treated as proof that a request was not delivered.
        """
        import inspect
        import httpx
        import httpcore
        from httpcore._sync.connection import HTTPConnection
        from httpcore._sync.http11 import HTTP11Connection
        from httpcore._backends.sync import SyncBackend, SyncStream
        current_sha = self._authorization_sha()
        self._require_time()
        payload, _ = self.build_payload(messages, tools)
        content_hash = sha_value(payload)
        directory = self.run / 'http' / sha_value(old_request_id)
        if self.transport is not None and not self.task_context:
            directory = self.ledger_path.parent / 'fixture-http' / sha_value(old_request_id)
        request_path, error_path = directory / 'request.json', directory / 'transport-error.json'
        if not request_path.is_file() or not error_path.is_file():
            raise ValueError('connection-stage evidence is absent; UNKNOWN remains')
        saved, error = read_json(request_path), read_json(error_path)
        self._validate_saved_profile(saved)
        if transport_profile(self.config)['strict_tools'] and saved.get('internal_tools') != tools:
            raise ValueError('connection retry original internal schema mismatch')
        if saved.get('request_id') != old_request_id or saved.get('authorization_sha256') != current_sha or saved.get('payload') != payload or saved.get('payload_sha256') != content_hash:
            raise ValueError('connection retry original authorization/payload mismatch')
        if error.get('error_type') not in ('ConnectError', 'ConnectTimeout'):
            raise ValueError('only verified connection setup errors permit an explicit replacement; read/stream uncertainty stays blocked')
        if any((directory / name).exists() for name in ('protocol-response.json', 'wire-evidence.json', 'rejected-response.json', 'readback-recovery.json')):
            raise ValueError('response evidence exists; connection-only replacement is forbidden')
        progress_path = directory / 'progress.json'
        if progress_path.is_file():
            progress = read_json(progress_path)
            if 'http_status' in progress or progress.get('event_count', 0) or progress.get('content_bytes', 0) or progress.get('reasoning_bytes', 0) or progress.get('tool_argument_bytes', 0):
                raise ValueError('HTTP response/stream progress exists; delivery may have happened')
        if (httpx.__version__, httpcore.__version__) != ('0.28.1', '1.0.9'):
            raise ValueError('connection-boundary inspection is not verified for these dependency versions')
        boundaries = []
        for function in (SyncBackend.connect_tcp, SyncStream.start_tls, HTTPConnection.handle_request, HTTPConnection._connect, HTTP11Connection.handle_request, HTTP11Connection._send_request_body):
            source = inspect.getsource(function)
            boundaries.append(dict(function=function.__qualname__, path=inspect.getsourcefile(function),
                                   line=inspect.getsourcelines(function)[1], source_sha256=sha_value(source)))
        if self.transport is None and self.task_context is None:
            previous = read_json(RUN.parent.parent / 'results/s0-case-agent-20261019-01/runtime-source-provenance.json')
            for name in ('week13/src/case_http.py', 'week13/src/case_stream.py'):
                expected = next(item['sha256'] for item in previous['implemented_files'] if item['path'] == name)
                if sha_file(REPO / name) != expected:
                    raise ValueError('original HTTP/default-transport source binding drift; no connection retry')
        elif self.transport is None:
            sources=saved.get('transport_source_sha256',{})
            if any(sources.get(name)!=sha_file(REPO/name) for name in ('week13/src/case_http.py','week13/src/case_stream.py')):
                raise ValueError('original request HTTP/default-transport source binding drift; no connection retry')
        proof = dict(schema='v12-explicit-connection-stage-replacement-1', authorization_sha256=current_sha,
            original_request_file_sha256=sha_file(request_path), original_error_file_sha256=sha_file(error_path),
            payload_sha256=content_hash, observed_error_class=error['error_type'],
            dependency_versions=dict(httpx=httpx.__version__, httpcore=httpcore.__version__), inspected_boundaries=boundaries,
            absence_of_response_evidence=True, origin_request_body_sent=False,
            no_body_basis='Installed default transport maps TCP/TLS setup exceptions to ConnectError/ConnectTimeout; HTTPConnection finishes _connect before HTTP11Connection sends headers/body. This is source-based classification, not a retrospective network trace.',
            automatic_retries=0, redirects=False, original_usage_and_charge_remain='UNKNOWN; full count/token/CNY reservation retained',
            network_permission='replacement caller must separately obtain required external network execution permission',
            fixture_transport=self.transport is not None)
        proof = self.budget.authorize_connect_retry(old_request_id, new_request_id, content_hash, proof)
        audit = directory / 'explicit-connect-retry.json'
        if self.transport is None:
            save_json(audit, proof, immutable=True)
        else:
            audit.write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding='utf8')
        return proof

    def usage(self):
        return self.budget.usage()


ModificationRuntime = CaseRuntime
