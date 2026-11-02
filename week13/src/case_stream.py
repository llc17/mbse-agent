"""One SSE attempt, adapted from V12_old stairs_stream without archive imports.

Adds tool-call fragment assembly and full reasoning protocol data. Progress
receipts contain counters only; reasoning is never presented as user prose.
"""
import json
import re
import time
import codecs
import threading
import hashlib
from pathlib import Path
from .case_paths import save_json, utc_now, sha_value


class StreamProtocolError(ValueError):
    pass


def require(ok, code):
    if not ok:
        raise StreamProtocolError(code)


def validate_usage(value):
    require(isinstance(value, dict), 'MISSING_USAGE')
    keys = ('prompt_tokens', 'completion_tokens', 'total_tokens')
    require(all(type(value.get(k)) is int and value[k] >= 0 for k in keys), 'INVALID_USAGE')
    require(value['total_tokens'] == value['prompt_tokens'] + value['completion_tokens'], 'USAGE_NOT_CONSERVED')
    hit, miss = value.get('prompt_cache_hit_tokens'), value.get('prompt_cache_miss_tokens')
    if hit is not None or miss is not None:
        require(type(hit) is int and type(miss) is int and min(hit, miss) >= 0 and hit + miss == value['prompt_tokens'], 'INVALID_CACHE_USAGE')
    details = value.get('completion_tokens_details', {})
    require(isinstance(details, dict), 'INVALID_COMPLETION_DETAILS')
    reasoning = details.get('reasoning_tokens', value.get('reasoning_tokens'))
    require(reasoning is None or type(reasoning) is int and 0 <= reasoning <= value['completion_tokens'], 'INVALID_REASONING_USAGE')
    return value


class StreamRecorder:
    def __init__(self, directory, *, clock=time.monotonic, now=utc_now, request_id=None, payload_sha256=None):
        self.directory = Path(directory)
        self.clock, self.now = clock, now
        self.last_saved = None
        self.lock = threading.RLock()
        self.http_status = None
        self.tool_evidence = None
        self.tool_sequence, self.tool_previous, self.tool_terminal = 0, None, False
        if payload_sha256 is not None:
            require(isinstance(request_id,str) and bool(request_id) and
                    isinstance(payload_sha256,str) and re.fullmatch(r'[0-9a-f]{64}',payload_sha256), 'INVALID_TOOL_EVIDENCE_BINDING')
            self.tool_evidence = self.directory / 'tool-call-deltas.jsonl'
            with self.tool_evidence.open('x',encoding='utf8'):
                pass
            self._append_tool_evidence(dict(kind='HEADER',schema='v12-parsed-tool-deltas-1',
                request_id=request_id,payload_sha256=payload_sha256,
                representation='PARSED_SSE_TOOL_CALL_DELTAS_NOT_RAW_NETWORK_BYTES',
                reasoning_or_headers_recorded=False))
        self.timing = dict(schema='v12-http-stream-timing-1', request_id=request_id,
            first_receive_at_utc=None, last_receive_at_utc=None,
            first_substantive_progress_at_utc=None, last_substantive_progress_at_utc=None,
            last_substantive_monotonic=None, receive_chunk_count=0, received_bytes=0,
            heartbeat_count=0, event_count=0, content_bytes=0, reasoning_bytes=0,
            tool_argument_bytes=0, usage_known=False, finish_reason=None,
            terminated_at_utc=None, receive_clock_basis='application body-chunk observation')

    def _append_tool_evidence(self, fields):
        record=dict(sequence=self.tool_sequence,previous_sha256=self.tool_previous,**fields)
        record['record_sha256']=sha_value(record)
        with self.tool_evidence.open('r+',encoding='utf8',newline='\n') as stream:
            stream.seek(0,2)
            stream.write(json.dumps(record,ensure_ascii=False,separators=(',',':'),allow_nan=False)+'\n')
            stream.flush()
        self.tool_previous=record['record_sha256'];self.tool_sequence+=1

    def record_tool_delta(self, event_index, tool_calls):
        """Persist the parsed delta before production assembly changes it."""
        if self.tool_evidence is None:return
        require(not self.tool_terminal,'TOOL_EVIDENCE_AFTER_TERMINAL')
        self._append_tool_evidence(dict(kind='TOOL_DELTA',event_index=event_index,tool_calls=tool_calls))

    def _finish_tool_evidence(self, phase, fields):
        if self.tool_evidence is None or self.tool_terminal:return
        self._append_tool_evidence(dict(kind='TERMINAL',complete=phase=='STREAM_COMPLETE',
            finish_reason=self.timing['finish_reason'],usage_known=self.timing['usage_known'],
            event_count=self.timing['event_count'],classification=fields.get('classification')))
        self.tool_terminal=True

    def _save(self, name, value, *, immutable=False):
        # Windows readers briefly deny replacement while their file handle is
        # open. A bounded local retry preserves atomic receipts without failing
        # an otherwise healthy provider stream because the watchdog read it.
        for attempt in range(20):
            try:
                return save_json(self.directory / name, value, immutable=immutable)
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(.005)

    def __call__(self, phase, **fields):
        # No headers, exceptions, reasoning or model text enter progress files.
        allowed = {'event_count', 'content_bytes', 'reasoning_bytes', 'tool_argument_bytes', 'finish_reason', 'usage_known', 'http_status', 'provider_request_id', 'classification', 'error_type', 'received_bytes', 'substantive'}
        require(set(fields) <= allowed, 'UNSAFE_PROGRESS_FIELD')
        require(phase in {'HTTP_STATUS', 'STREAM_RECEIVE', 'STREAM_HEARTBEAT', 'STREAM_PROGRESS', 'STREAM_COMPLETE', 'STREAM_TERMINATED'}, 'UNSAFE_PROGRESS_PHASE')
        if 'provider_request_id' in fields:
            require(isinstance(fields['provider_request_id'], str) and re.fullmatch(r'[A-Za-z0-9._:-]{1,256}', fields['provider_request_id']), 'UNSAFE_REQUEST_ID')
        for key in ('classification', 'error_type'):
            if key in fields:
                require(isinstance(fields[key], str) and re.fullmatch(r'[A-Za-z0-9_]{1,96}', fields[key]), 'UNSAFE_ERROR_CLASS')
        with self.lock:
            monotonic, stamp = self.clock(), self.now()
            if phase == 'HTTP_STATUS':
                self.http_status = fields['http_status']
                # This immutable receipt survives all later stream progress.
                self._save('http-status.json', dict(recorded_at_utc=stamp, **fields), immutable=True)
            if phase == 'STREAM_RECEIVE':
                require(type(fields.get('received_bytes')) is int and fields['received_bytes'] > 0, 'INVALID_RECEIVE_COUNT')
                self.timing['first_receive_at_utc'] = self.timing['first_receive_at_utc'] or stamp
                self.timing['last_receive_at_utc'] = stamp
                self.timing['receive_chunk_count'] += 1
                self.timing['received_bytes'] += fields['received_bytes']
            if phase == 'STREAM_HEARTBEAT':
                self.timing['heartbeat_count'] += 1
            if fields.get('substantive') is True:
                self.timing['first_substantive_progress_at_utc'] = self.timing['first_substantive_progress_at_utc'] or stamp
                self.timing['last_substantive_progress_at_utc'] = stamp
                self.timing['last_substantive_monotonic'] = monotonic
            for key in ('event_count', 'content_bytes', 'reasoning_bytes', 'tool_argument_bytes', 'finish_reason', 'usage_known'):
                if key in fields:
                    self.timing[key] = fields[key]
            if phase in ('STREAM_COMPLETE', 'STREAM_TERMINATED'):
                self.timing.update(terminated_at_utc=stamp, termination_phase=phase)
                if 'classification' in fields:
                    self.timing['classification'] = fields['classification']
                if 'error_type' in fields:
                    self.timing['error_type'] = fields['error_type']
                self._finish_tool_evidence(phase, fields)
            self._save('timing.json', self.timing)
            if phase in ('STREAM_RECEIVE', 'STREAM_HEARTBEAT'):
                return
            if phase == 'STREAM_PROGRESS' and self.last_saved is not None and monotonic - self.last_saved < 10:
                return
            self.last_saved = monotonic
            safe = {key: value for key, value in fields.items() if key != 'substantive'}
            self._save('progress.json', dict(phase=phase, recorded_at=stamp, **safe))
            from .case_progress import emit_progress
            messages={'HTTP_STATUS':'产品HTTP已返回状态，正在接收回复。','STREAM_PROGRESS':'正在接收产品模型回复。','STREAM_COMPLETE':'产品回复流已结束，正在保存通信回执。','STREAM_TERMINATED':'产品通信已终止，正在记录实际结束状态。'}
            if phase in messages:emit_progress('http_'+phase.lower(),messages[phase],http_request_id=self.timing['request_id'],http_phase=phase,**safe)

    def record_http_error(self, value):
        """Separate diagnostic evidence; never an executable provider response."""
        require(value.get('schema') == 'v12-http-error-body-1' and
                value.get('request_id') == self.timing['request_id'] and
                value.get('http_status') == self.http_status and
                value.get('product_response') is False, 'HTTP_ERROR_BINDING_DIFFERS')
        self._save('http-error.json', value, immutable=True)


class StreamAssembler:
    def __init__(self, emit=lambda *a, **k: None, *, clock=time.monotonic, no_progress_s=600, on_tool_delta=lambda *a: None):
        self.emit = emit
        self.clock = clock
        self.no_progress_s = no_progress_s
        self.on_tool_delta = on_tool_delta
        self.last_substantive = clock()
        self.lines = []
        self.content = []
        self.reasoning = []
        self.reasoning_present = False
        self.calls = {}
        self.identity = None
        self.finish_reason = None
        self.usage = None
        self.done = False
        self.event_count = self.content_bytes = self.reasoning_bytes = self.tool_argument_bytes = 0

    def progress(self):
        return dict(event_count=self.event_count, content_bytes=self.content_bytes,
                    reasoning_bytes=self.reasoning_bytes, tool_argument_bytes=self.tool_argument_bytes,
                    usage_known=self.usage is not None, finish_reason=self.finish_reason)

    def check_progress(self):
        require(self.clock() - self.last_substantive < self.no_progress_s, 'NO_SUBSTANTIVE_PROGRESS_TIMEOUT')

    def feed_line(self, line):
        self.check_progress()
        require(isinstance(line, str), 'INVALID_SSE_LINE')
        if line.startswith(':'):
            self.emit('STREAM_HEARTBEAT')
            return  # heartbeat is not model progress
        if not line:
            if self.lines:
                body = '\n'.join(self.lines)
                self.lines.clear()
                self.consume(body)
            return
        field, separator, value = line.partition(':')
        require(separator and field in ('data', 'event', 'id', 'retry'), 'INVALID_SSE_FIELD')
        if field == 'data':
            self.lines.append(value[1:] if value.startswith(' ') else value)

    def consume(self, body):
        require(not self.done, 'DATA_AFTER_DONE')
        self.event_count += 1
        if body == '[DONE]':
            self.done = True
            return
        try:
            value = json.loads(body)
        except (ValueError, TypeError):
            raise StreamProtocolError('INVALID_EVENT_JSON') from None
        require(isinstance(value, dict) and not value.get('error') and value.get('object') == 'chat.completion.chunk', 'INVALID_PROVIDER_EVENT')
        identity = (value.get('id'), value.get('model'), value.get('created'))
        require(isinstance(identity[0], str) and bool(identity[0]) and isinstance(identity[1], str), 'INVALID_STREAM_IDENTITY')
        if self.identity is None:
            self.identity = identity
        require(identity == self.identity, 'CHUNK_IDENTITY_CHANGED')
        choices = value.get('choices')
        require(isinstance(choices, list) and len(choices) <= 1, 'INVALID_CHOICES')
        usage = value.get('usage')
        if not choices:
            # Compatibility only: current provider normally combines usage with
            # the final content block. A usage-only block is never required.
            require(self.finish_reason is not None and self.usage is None and usage is not None, 'INVALID_USAGE_ONLY_CHUNK')
            self.usage = validate_usage(usage)
            self.last_substantive = self.clock()
            self.emit('STREAM_PROGRESS', substantive=True, **self.progress())
            return
        require(self.finish_reason is None, 'CONTENT_AFTER_TERMINAL')
        choice = choices[0]
        require(isinstance(choice, dict) and choice.get('index') == 0 and isinstance(choice.get('delta'), dict), 'INVALID_CHOICE')
        delta = choice['delta']
        require(delta.get('role') in (None, 'assistant') and not delta.get('function_call'), 'INVALID_ASSISTANT_DELTA')
        if 'tool_calls' in delta:
            self.on_tool_delta(self.event_count,delta['tool_calls'])
        substantive = False
        for key, parts, count in (('content', self.content, 'content_bytes'), ('reasoning_content', self.reasoning, 'reasoning_bytes')):
            fragment = delta.get(key)
            require(fragment is None or isinstance(fragment, str), 'INVALID_TEXT_DELTA')
            if key == 'reasoning_content' and key in delta:
                self.reasoning_present = True
            if fragment:
                parts.append(fragment)
                setattr(self, count, getattr(self, count) + len(fragment.encode('utf8')))
                substantive = substantive or bool(fragment.strip())
        calls = delta.get('tool_calls', [])
        require(isinstance(calls, list), 'INVALID_TOOL_DELTAS')
        for fragment in calls:
            require(isinstance(fragment, dict) and type(fragment.get('index')) is int and fragment['index'] >= 0, 'INVALID_TOOL_INDEX')
            call = self.calls.setdefault(fragment['index'], dict(id='', type='function', function=dict(name='', arguments='')))
            require(fragment.get('type') in (None, 'function'), 'INVALID_TOOL_TYPE')
            if fragment.get('id'):
                require(isinstance(fragment['id'], str), 'INVALID_TOOL_ID')
                call['id'] += fragment['id']
                substantive = substantive or bool(fragment['id'].strip())
            function = fragment.get('function', {})
            require(isinstance(function, dict), 'INVALID_FUNCTION_DELTA')
            for key in ('name', 'arguments'):
                text = function.get(key)
                require(text is None or isinstance(text, str), 'INVALID_FUNCTION_FRAGMENT')
                if text:
                    call['function'][key] += text
                    if key == 'arguments':
                        self.tool_argument_bytes += len(text.encode('utf8'))
                    substantive = substantive or bool(text.strip())
        finish = choice.get('finish_reason')
        if finish is not None:
            require(finish in ('stop', 'tool_calls', 'length', 'content_filter', 'insufficient_system_resource', 'aborted'), 'UNKNOWN_FINISH_REASON')
            self.finish_reason = finish
            substantive = True
        if usage is not None:
            require(finish is not None and self.usage is None, 'USAGE_WITHOUT_TERMINAL')
            self.usage = validate_usage(usage)
        if substantive:
            self.last_substantive = self.clock()
        self.emit('STREAM_PROGRESS', substantive=substantive, **self.progress())

    def finalize(self):
        require(not self.lines and self.done and self.finish_reason is not None and self.usage is not None and self.identity is not None, 'INCOMPLETE_STREAM')
        calls = [self.calls[index] for index in sorted(self.calls)]
        require(all(call['id'] and call['function']['name'] for call in calls), 'INCOMPLETE_TOOL_IDENTITY')
        require(len({call['id'] for call in calls}) == len(calls), 'DUPLICATE_TOOL_IDS')
        message = dict(role='assistant', content=''.join(self.content), reasoning_content=''.join(self.reasoning))
        if calls:
            message['tool_calls'] = calls
        return dict(id=self.identity[0], object='chat.completion', model=self.identity[1], created=self.identity[2],
                    choices=[dict(index=0, finish_reason=self.finish_reason, message=message)], usage=self.usage,
                    reasoning_field_present=self.reasoning_present)


def _received_lines(response, emit):
    """Observe actual body chunks, then decode SSE lines without retaining raw data."""
    decoder = codecs.getincrementaldecoder('utf-8')()
    pending, after_cr = [], False
    for raw in response.iter_bytes():
        if not raw:
            continue
        emit('STREAM_RECEIVE', received_bytes=len(raw))
        text = decoder.decode(raw)
        if not text:
            continue
        if after_cr and text.startswith('\n'):
            text = text[1:]
        after_cr = text.endswith('\r')
        parts = re.split(r'\r\n|\r|\n', text)
        pending.append(parts[0])
        if len(parts) > 1:
            yield ''.join(pending)
            yield from parts[1:-1]
            pending = [parts[-1]]
    pending.append(decoder.decode(b'', final=True))
    pending = ''.join(pending)
    if pending:
        yield pending


def _redact_error_text(text, secrets, *, partial=False):
    for secret in sorted({item for item in secrets if isinstance(item, str) and item}, key=len, reverse=True):
        for encoded in (secret, json.dumps(secret, ensure_ascii=False)[1:-1], json.dumps(secret)[1:-1]):
            text = text.replace(encoded, '[REDACTED]')
        if partial:
            for length in range(len(secret)-1, 0, -1):
                if text.endswith(secret[:length]):
                    text = text[:-length] + '[REDACTED_PARTIAL_SECRET]'
                    break
    text = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [REDACTED]', text)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]{6,}', '[REDACTED_KEY]', text)
    text = re.sub(r'(?i)(https?://)[^\s/:@]+:[^\s/@]+@', r'\1[REDACTED]@', text)
    text = re.sub(r'(?im)^.*\b(?:request[_ -]?|response[_ -]?)?headers?\s*[:=].*$', '[REDACTED_HEADER_LINE]', text)
    text = re.sub(r'(?i)("(?:request_|response_)?headers"\s*:\s*)\{[^\r\n]*', r'\1"[REDACTED_HEADERS]"', text)
    text = re.sub(r'''(?ix)(\b(?:authorization|proxy-authorization|x-api-key|api[_-]?key|cookie|set-cookie|password|client[_-]?secret|access[_-]?token)\b\s*[:=]\s*)("[^"]*"|'[^']*'|[^\r\n,;}]+)''', r'\1[REDACTED]', text)
    return text


def http_error_evidence(response, *, request_id, payload, endpoint, headers, secret_values=(), maximum_bytes=65536):
    """Retain a bounded sanitized body and explicit completeness, never usage."""
    require(type(maximum_bytes) is int and 0 < maximum_bytes <= 65536, 'INVALID_ERROR_BODY_BOUND')
    secrets = list(secret_values)
    for value in headers.values():
        if isinstance(value, str) and value:
            secrets.append(value)
            if value.lower().startswith('bearer '): secrets.append(value[7:])
    chunks, size, truncated, read_error, exhausted = [], 0, False, None, False
    try:
        for chunk in response.iter_bytes(chunk_size=min(8192, maximum_bytes+1)):
            if not chunk: continue
            take = min(len(chunk), maximum_bytes + 1 - size)
            chunks.append(chunk[:take]); size += take
            if size > maximum_bytes:
                truncated = True
                break
        else:
            exhausted = True
    except Exception as exc:
        read_error = type(exc).__name__
    raw = b''.join(chunks)[:maximum_bytes]
    text = _redact_error_text(raw.decode('utf8', errors='replace'), secrets, partial=truncated or bool(read_error))
    parsed, json_error = None, None
    if exhausted and not truncated and not read_error:
        try:
            # Parse before field-level redaction; raw text is separately scrubbed.
            value = json.loads(raw.decode('utf8'))
            sensitive = re.compile(r'(?i)(authorization|cookie|password|secret|token|api[_-]?key|headers)')
            def clean(item):
                if isinstance(item, dict):
                    return {_redact_error_text(str(key), secrets): '[REDACTED]' if sensitive.search(str(key)) else clean(val) for key,val in item.items()}
                if isinstance(item, list): return [clean(val) for val in item]
                return _redact_error_text(item, secrets) if isinstance(item, str) else item
            parsed = clean(value)
            # Regenerate JSON text from the fully sanitized tree, so nested
            # header dictionaries and credential fields cannot leak in text.
            text = json.dumps(parsed, ensure_ascii=False)
        except (ValueError, UnicodeError):
            json_error = 'NOT_COMPLETE_VALID_UTF8_JSON'
    provider_id = response.headers.get('x-request-id', '')
    if not isinstance(provider_id, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,256}', provider_id): provider_id = None
    if provider_id in secrets: provider_id = '[REDACTED]'
    saved_limit = maximum_bytes * 4
    encoded_text = text.encode('utf8')
    saved_truncated = len(encoded_text) > saved_limit
    if saved_truncated:
        text = encoded_text[:saved_limit].decode('utf8', errors='ignore')
        parsed = None
        json_error = 'SANITIZED_TEXT_EXCEEDED_SAVED_BOUND'
    from .case_paths import sha_value
    return dict(schema='v12-http-error-body-1', request_id=request_id, http_status=int(response.status_code),
        provider_request_id=provider_id, endpoint=_redact_error_text(endpoint, secrets), payload_sha256=sha_value(payload),
        maximum_body_bytes=maximum_bytes, captured_original_prefix_bytes=len(raw), observed_bytes_at_least=size,
        body_complete=exhausted and not truncated and not read_error, body_truncated=truncated,
        body_read_error=read_error, body_text=text, error_json=parsed, json_parse_status=json_error or ('PARSED' if parsed is not None else 'NOT_PARSED'),
        maximum_saved_text_bytes=saved_limit, sanitized_text_truncated=saved_truncated,
        sanitized_text_sha256=hashlib.sha256(text.encode('utf8')).hexdigest(),
        redaction='REQUEST_SECRET_VALUES_AND_CREDENTIAL_FIELDS_HEADERS_AND_BEARER_PATTERNS',
        raw_body_persisted=False, response_headers_persisted=False, request_headers_persisted=False,
        product_response=False, billing_usage_authoritative=False, recorded_at_utc=utc_now())


def complete_stream(client, *, url, headers, payload, emit, no_progress_s=600, before_post=lambda: None,
                    request_id=None, secret_values=(), on_http_error=lambda value: None):
    effective = dict(payload, stream=True, stream_options=dict(include_usage=True))
    state = StreamAssembler(emit, no_progress_s=no_progress_s,
        on_tool_delta=getattr(emit,'record_tool_delta',lambda *a: None))
    before_post()
    with client.stream('POST', url, headers=headers, json=effective) as response:
        provider_request_id = response.headers.get('x-request-id', '')
        fields = dict(http_status=int(response.status_code))
        id_secrets=tuple(secret_values)+tuple(value for value in headers.values() if isinstance(value,str))
        if re.fullmatch(r'[A-Za-z0-9._:-]{1,256}', provider_request_id) and _redact_error_text(provider_request_id,id_secrets)==provider_request_id:
            fields['provider_request_id'] = provider_request_id
        emit('HTTP_STATUS', **fields)
        if not 200 <= response.status_code < 300:
            try:
                on_http_error(http_error_evidence(response, request_id=request_id, payload=effective,
                    endpoint=url, headers=headers, secret_values=secret_values))
            except Exception:
                emit('STREAM_TERMINATED', classification='HTTP_ERROR_DIAGNOSTIC_FAILED', error_type='DiagnosticError')
            response.raise_for_status()
            raise StreamProtocolError('HTTP_STATUS_NOT_SUCCESS')
        response.raise_for_status()
        require(response.headers.get('content-type', '').split(';')[0].strip().lower() == 'text/event-stream', 'INVALID_STREAM_CONTENT_TYPE')
        for line in _received_lines(response, emit):
            state.feed_line(line)
            if state.done:
                break
        result = state.finalize()
    emit('STREAM_COMPLETE', **state.progress())
    return result


def reconstruct_tool_delta_evidence(path, *, request_id, payload_sha256, protocol_response):
    """Independent join of persisted parsed deltas; never calls StreamAssembler.

    This verifies application-level event evidence, not original network bytes.
    A missing/partial terminal, reordered or missing record cannot be complete.
    """
    records=[];previous=None
    with Path(path).open(encoding='utf8') as stream:
        for line in stream:
            require(line.endswith('\n'),'INCOMPLETE_TOOL_EVIDENCE_LINE')
            record=json.loads(line)
            require(isinstance(record,dict),'INVALID_TOOL_EVIDENCE_RECORD')
            expected=record.get('record_sha256')
            body={key:value for key,value in record.items() if key!='record_sha256'}
            require(type(record.get('sequence')) is int and record.get('sequence')==len(records) and record.get('previous_sha256')==previous and
                    expected==sha_value(body),'TOOL_EVIDENCE_ORDER_OR_HASH_DIFFERS')
            records.append(record);previous=expected
    require(len(records)>=2,'INCOMPLETE_TOOL_EVIDENCE')
    header,terminal=records[0],records[-1]
    require(header.get('kind')=='HEADER' and header.get('schema')=='v12-parsed-tool-deltas-1' and
            header.get('request_id')==request_id and header.get('payload_sha256')==payload_sha256,'TOOL_EVIDENCE_BINDING_DIFFERS')
    require(terminal.get('kind')=='TERMINAL' and terminal.get('complete') is True and
            terminal.get('usage_known') is True and terminal.get('finish_reason') in ('stop','tool_calls') and
            type(terminal.get('event_count')) is int and terminal['event_count']>0,'INCOMPLETE_TOOL_EVIDENCE')
    fragments={};last_event=0
    for record in records[1:-1]:
        event=record.get('event_index')
        require(record.get('kind')=='TOOL_DELTA' and type(event) is int and last_event<event<terminal['event_count'], 'TOOL_EVIDENCE_EVENT_ORDER_DIFFERS')
        last_event=event
        require(isinstance(record.get('tool_calls'),list),'INVALID_TOOL_EVIDENCE_CALLS')
        for part in record['tool_calls']:
            require(isinstance(part,dict) and type(part.get('index')) is int and part['index']>=0,'INVALID_TOOL_EVIDENCE_INDEX')
            require(part.get('type') in (None,'function'),'INVALID_TOOL_EVIDENCE_TYPE')
            target=fragments.setdefault(part['index'],dict(id=[],name=[],arguments=[]))
            function=part.get('function',{})
            require(isinstance(function,dict),'INVALID_TOOL_EVIDENCE_FUNCTION')
            for key,value in (('id',part.get('id')),('name',function.get('name')),('arguments',function.get('arguments'))):
                require(value is None or isinstance(value,str),'INVALID_TOOL_EVIDENCE_FRAGMENT')
                if value:target[key].append(value)
    calls=[dict(id=''.join(fragments[index]['id']),type='function',
                function=dict(name=''.join(fragments[index]['name']),arguments=''.join(fragments[index]['arguments']))) for index in sorted(fragments)]
    require(isinstance(protocol_response,dict),'INVALID_TOOL_EVIDENCE_PROTOCOL')
    choices=protocol_response.get('choices',[])
    require(isinstance(choices,list) and len(choices)==1 and isinstance(choices[0],dict) and
            choices[0].get('finish_reason')==terminal['finish_reason'],'TOOL_EVIDENCE_PROTOCOL_TERMINAL_DIFFERS')
    validate_usage(protocol_response.get('usage'))
    require(calls==choices[0].get('message',{}).get('tool_calls',[]),'TOOL_EVIDENCE_PROTOCOL_ARGUMENTS_DIFFER')
    return dict(schema='v12-independent-tool-delta-readback-1',complete=True,request_id=request_id,
        payload_sha256=payload_sha256,delta_records=len(records)-2,tool_calls=calls,
        evidence_file_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        evidence_kind='PARSED_SSE_TOOL_CALL_DELTAS_NOT_RAW_NETWORK_BYTES',protocol_tool_calls_equal=True)
