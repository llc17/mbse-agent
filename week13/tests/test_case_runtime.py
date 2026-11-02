"""Targeted fixtures only: no credentials, HTTP, MCP, OMC or simulation."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from week13.src.case_runtime import CaseRuntime
from week13.src.case_paths import CONFIG, read_json, authorization_sha, sha_value
from week13.src.case_stream import StreamAssembler, StreamProtocolError


TOOLS = [dict(type='function', function=dict(name='proposal_prepare', parameters=dict(type='object')))]


def response(*, calls=None, content='说明', finish=None, reasoning='protocol reasoning', usage=None):
    message = dict(role='assistant', content=content, reasoning_content=reasoning)
    if calls:
        message['tool_calls'] = calls
    return dict(id='fixture_response', model='deepseek-flash', choices=[dict(index=0, finish_reason=finish or ('tool_calls' if calls else 'stop'), message=message)], usage=usage or dict(prompt_tokens=20, completion_tokens=10, total_tokens=30, prompt_cache_hit_tokens=5, prompt_cache_miss_tokens=15))


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.sent = []
        self.provider = response()
        def transport(payload, key, timeout):
            self.sent.append(copy.deepcopy(payload))
            if isinstance(self.provider, BaseException):
                raise self.provider
            return copy.deepcopy(self.provider)
        self.rt = CaseRuntime(CONFIG, Path(self.folder.name) / 'fixtures.sqlite', transport=transport)

    def test_effective_controls_cache_and_no_live_ledger(self):
        value = self.rt.complete([dict(role='user', content='明确需求')], [], 'case_design_1')
        self.assertEqual(self.sent[0]['thinking'], dict(type='enabled'))
        self.assertEqual(self.sent[0]['reasoning_effort'], 'high')
        self.assertEqual(self.sent[0]['max_tokens'], 393216)
        self.assertEqual(value['reasoning_content'], 'protocol reasoning')
        self.assertEqual(self.rt.complete([dict(role='user', content='明确需求')], [], 'case_design_1'), value)
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn('V12_old', str(self.rt.ledger_path))
        self.assertEqual(self.rt.usage()['requests'], 1)  # isolated fixture ledger only

    def test_no_call_tool_round_retains_reasoning_on_next_payload(self):
        first = self.rt.complete([dict(role='user', content='read')], TOOLS, 'case_design_1')
        self.rt.complete([dict(role='user', content='read'), first['assistant_message'], dict(role='user', content='next')], TOOLS, 'case_design_2')
        self.assertEqual(self.sent[-1]['messages'][1]['reasoning_content'], 'protocol reasoning')
        self.assertEqual(self.sent[-1]['tool_choice'], 'auto')
        with self.assertRaisesRegex(ValueError, 'reasoning_content'):
            self.rt.complete([dict(role='assistant', content='lost reasoning')], TOOLS, 'case_design_3')
        self.assertEqual(len(self.sent), 2)

    def test_actual_shared_window_is_not_old_input_estimate_cap(self):
        self.provider = response(usage=dict(prompt_tokens=999990, completion_tokens=10, total_tokens=1000000))
        result = self.rt.complete([dict(role='user', content='short fixture context')], [], 'case_design_1')
        self.assertEqual(result['usage']['total_tokens'], 1000000)
        self.assertEqual(self.sent[0]['max_tokens'], 393216)
        with self.assertRaises(ValueError):
            self.rt.build_payload([dict(role='user', content='x' * 1000000)], [])

    def test_known_malformed_json_is_rejected_without_guessing_or_retry(self):
        self.provider = response(content='', calls=[dict(id='tool_1', type='function', function=dict(name='proposal_prepare', arguments='{"spec":'))])
        with self.assertRaises(json.JSONDecodeError):
            self.rt.complete([dict(role='user', content='generate')], TOOLS, 'case_generate_1')
        row = self.rt.budget.request('case_generate_1')
        self.assertEqual(row['status'], 'rejected')
        self.assertEqual(row['actual_tokens'], 30)
        with self.assertRaisesRegex(RuntimeError, 'resend'):
            self.rt.complete([dict(role='user', content='generate')], TOOLS, 'case_generate_1')
        self.assertEqual(len(self.sent), 1)
        self.provider = response()
        self.rt.complete([dict(role='user', content='bounded correction')], [], 'case_correct_1')
        self.assertEqual(len(self.sent), 2)

    def test_truncation_never_becomes_accepted_source(self):
        self.provider = response(finish='length')
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            self.rt.complete([dict(role='user', content='generate')], [], 'case_generate_1')
        self.assertEqual(self.rt.budget.request('case_generate_1')['status'], 'rejected')

    def test_transport_unknown_retains_full_reservation_and_stops_new_send(self):
        self.provider = TimeoutError()
        with self.assertRaisesRegex(RuntimeError, 'uncertain'):
            self.rt.complete([dict(role='user', content='request')], [], 'case_design_1')
        usage = self.rt.usage()
        self.assertEqual(usage['actual_cost_cny'], 'UNKNOWN')
        self.assertAlmostEqual(usage['cost_cny_reserved_or_actual'], 5.24288)
        with self.assertRaisesRegex(RuntimeError, 'frozen'):
            self.rt.complete([dict(role='user', content='do not retry')], [], 'case_design_2')
        self.assertEqual(len(self.sent), 1)

    def test_cny_cached_usage_no_reasoning_double_count(self):
        result = self.rt.complete([dict(role='user', content='cost')], [], 'case_design_1')
        self.assertEqual(result['usage']['total_tokens'], 30)
        # This approved execution window is wholly off-peak Beijing time.
        self.assertAlmostEqual(self.rt.usage()['known_actual_cost_cny'], (15*2 + 5*.04 + 10*8)/1000000/2)


def chunk(delta, finish=None, usage=None):
    value = dict(id='stream_fixture', model='deepseek-flash', created=1, object='chat.completion.chunk', choices=[dict(index=0, delta=delta, finish_reason=finish)])
    if usage is not None:
        value['usage'] = usage
    return json.dumps(value)


class StreamTests(unittest.TestCase):
    def test_tool_fragments_reasoning_and_combined_terminal_usage(self):
        stream = StreamAssembler()
        stream.consume(chunk(dict(reasoning_content='one ')))
        stream.consume(chunk(dict(reasoning_content='two', tool_calls=[dict(index=0, id='call_1', type='function', function=dict(name='proposal_', arguments='{"spec":'))])))
        stream.consume(chunk(dict(tool_calls=[dict(index=0, function=dict(name='prepare', arguments='{"value":"line\\nquote\\\""}}'))])))
        stream.consume(chunk(dict(content=''), finish='tool_calls', usage=dict(prompt_tokens=5, completion_tokens=8, total_tokens=13)))
        stream.consume('[DONE]')
        result = stream.finalize()
        message = result['choices'][0]['message']
        self.assertEqual(message['reasoning_content'], 'one two')
        self.assertEqual(message['tool_calls'][0]['function']['name'], 'proposal_prepare')
        self.assertEqual(json.loads(message['tool_calls'][0]['function']['arguments'])['spec']['value'], 'line\nquote"')

    def test_final_content_and_usage_same_block(self):
        stream = StreamAssembler()
        stream.consume(chunk(dict(content='done'), finish='stop', usage=dict(prompt_tokens=5, completion_tokens=2, total_tokens=7)))
        stream.consume('[DONE]')
        self.assertEqual(stream.finalize()['choices'][0]['message']['content'], 'done')

    def test_heartbeat_cannot_extend_substantive_progress(self):
        elapsed = [0.0]
        stream = StreamAssembler(clock=lambda: elapsed[0], no_progress_s=600)
        elapsed[0] = 599
        stream.feed_line(': heartbeat')
        elapsed[0] = 600
        with self.assertRaisesRegex(StreamProtocolError, 'NO_SUBSTANTIVE'):
            stream.feed_line(': heartbeat')

    def test_missing_done_usage_or_bad_identity_rejected(self):
        stream = StreamAssembler()
        stream.consume(chunk(dict(content='text'), finish='stop'))
        with self.assertRaisesRegex(StreamProtocolError, 'INCOMPLETE'):
            stream.finalize()
        value = json.loads(chunk(dict(content='wrong')))
        value['id'] = 'changed'
        with self.assertRaises(StreamProtocolError):
            stream.consume(json.dumps(value))


class SavedResponseRecoveryTests(unittest.TestCase):
    setUp = RuntimeTests.setUp

    def test_legacy_direct_sender_cannot_bypass_enabled_budgeted_transport(self):
        with self.assertRaisesRegex(RuntimeError, 'use CaseRuntime.complete'):
            self.rt._send([dict(role='user', content='bypass')], [])
        self.assertEqual(len(self.sent), 0)

    def saved(self, rid='case_design_interrupted', *, unknown=True):
        payload, estimate = self.rt.build_payload([dict(role='user', content='original request')], [])
        content_hash = sha_value(payload)
        self.rt.budget.reserve_http(rid, content_hash, 'design', self.rt.config)
        self.rt.budget.launch_http(rid)
        if unknown:
            self.rt.budget.finish_http(rid, error='parent interrupted after child saved response', uncertain=True)
        directory = self.rt.ledger_path.parent / 'fixture-http' / sha_value(rid)
        directory.mkdir(parents=True)
        request = dict(request_id=rid, payload=payload, payload_sha256=content_hash, authorization_sha256=authorization_sha())
        (directory / 'request.json').write_text(json.dumps(request), encoding='utf8')
        (directory / 'protocol-response.json').write_text(json.dumps(response()), encoding='utf8')
        return directory

    def test_reopen_reconciles_complete_unknown_reply_without_http(self):
        directory = self.saved()
        self.assertEqual(self.rt.usage()['actual_cost_cny'], 'UNKNOWN')
        reopened = CaseRuntime(CONFIG, self.rt.ledger_path, transport=lambda *a: self.fail('recovery must not dispatch HTTP'))
        result = reopened.recover_saved_response('case_design_interrupted')
        self.assertEqual(result['content'], '说明')
        self.assertEqual(result['reasoning_content'], 'protocol reasoning')
        self.assertEqual(reopened.usage()['requests'], 1)
        self.assertEqual(reopened.usage()['unknown_requests'], 0)
        self.assertIsNone(reopened.usage()['frozen'])
        self.assertTrue((directory / 'readback-recovery.json').is_file())
        self.assertEqual(reopened.recover_saved_response('case_design_interrupted'), result)
        self.assertEqual(len(self.sent), 0)

    def test_complete_inflight_reply_can_be_read_back(self):
        self.saved(unknown=False)
        result = self.rt.recover_saved_response('case_design_interrupted')
        self.assertEqual(result['finish_reason'], 'stop')
        self.assertEqual(self.rt.budget.request('case_design_interrupted')['status'], 'succeeded')
        self.assertEqual(len(self.sent), 0)

    def test_missing_incomplete_or_mismatched_evidence_keeps_unknown(self):
        directory = self.saved()
        (directory / 'protocol-response.json').unlink()
        with self.assertRaisesRegex(RuntimeError, 'absent'):
            self.rt.recover_saved_response('case_design_interrupted')
        (directory / 'protocol-response.json').write_text(json.dumps(response(finish='length')), encoding='utf8')
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            self.rt.recover_saved_response('case_design_interrupted')
        (directory / 'protocol-response.json').write_text(json.dumps(response()), encoding='utf8')
        saved = json.loads((directory / 'request.json').read_text())
        saved['payload']['thinking'] = dict(type='disabled')
        (directory / 'request.json').write_text(json.dumps(saved), encoding='utf8')
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            self.rt.recover_saved_response('case_design_interrupted')
        self.assertEqual(self.rt.usage()['actual_cost_cny'], 'UNKNOWN')
        self.assertEqual(len(self.sent), 0)


class ConnectionRetryTests(unittest.TestCase):
    setUp = RuntimeTests.setUp
    saved = SavedResponseRecoveryTests.saved

    def failed(self, error='ConnectError'):
        directory = self.saved('case_design_failed')
        (directory / 'protocol-response.json').unlink()
        (directory / 'transport-error.json').write_text(json.dumps(dict(error_type=error)), encoding='utf8')
        return directory

    def test_explicit_same_payload_retry_keeps_original_unknown_and_reservation(self):
        self.failed()
        messages = [dict(role='user', content='original request')]
        before = self.rt.budget.request('case_design_failed')
        proof = self.rt.authorize_connect_retry('case_design_failed', 'case_design_retry', messages, [])
        self.assertFalse(proof['origin_request_body_sent'])
        self.assertEqual(self.rt.usage()['requests'], 1)
        self.assertEqual(self.rt.usage()['connect_retry_pending'], 'case_design_retry')
        with self.assertRaisesRegex(RuntimeError, 'explicitly bound'):
            self.rt.complete(messages, [], 'unbound_request')
        self.rt.complete(messages, [], 'case_design_retry')
        after = self.rt.budget.request('case_design_failed')
        self.assertEqual(before, after)
        self.assertEqual(after['status'], 'unknown')
        self.assertIsNone(after['actual_cost'])
        self.assertEqual(after['reserve_cost'], 5.24288)
        self.assertEqual(self.rt.usage()['requests'], 2)  # isolated fixtures only
        self.assertEqual(self.rt.usage()['actual_cost_cny'], 'UNKNOWN')
        self.assertIsNone(self.rt.usage()['connect_retry_pending'])
        self.assertEqual(len(self.sent), 1)

    def test_read_timeout_and_stream_failure_never_unfreeze(self):
        directory = self.failed('ReadTimeout')
        for error in ('ReadTimeout', 'ReadError', 'RemoteProtocolError'):
            (directory / 'transport-error.json').write_text(json.dumps(dict(error_type=error)), encoding='utf8')
            with self.assertRaisesRegex(ValueError, 'only verified connection'):
                self.rt.authorize_connect_retry('case_design_failed', 'case_design_retry', [dict(role='user', content='original request')], [])
            self.assertIsNotNone(self.rt.usage()['frozen'])
        self.assertEqual(len(self.sent), 0)

    def test_mismatch_or_response_progress_cannot_be_called_unsent(self):
        directory = self.failed()
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            self.rt.authorize_connect_retry('case_design_failed', 'case_design_retry', [dict(role='user', content='changed')], [])
        (directory / 'progress.json').write_text(json.dumps(dict(http_status=200, event_count=1)), encoding='utf8')
        with self.assertRaisesRegex(ValueError, 'response/stream progress'):
            self.rt.authorize_connect_retry('case_design_failed', 'case_design_retry', [dict(role='user', content='original request')], [])
        self.assertEqual(len(self.sent), 0)


if __name__ == '__main__':
    unittest.main()
