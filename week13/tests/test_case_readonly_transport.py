"""Offline transport fixtures: no credentials, provider HTTP, OMC or simulation."""
import json
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

from week13.src.case_paths import ROOT, REPO, read_json
from week13.src.case_runtime import CaseRuntime, wait_for_http_child
from week13.src.case_stream import StreamAssembler, StreamProtocolError, StreamRecorder, complete_stream


class ReadonlyTransportTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='readonly-transport-', dir=ROOT/'tests')
        self.addCleanup(self.folder.cleanup)
        self.directory = Path(self.folder.name)

    def child(self, code):
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        environment.pop('MBSE_V12_REQUEST_ID', None)
        child = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', '-c', code, str(self.directory)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf8', cwd=REPO, env=environment)
        self.addCleanup(lambda: child.kill() if child.poll() is None else None)
        return child

    def watch(self, child, *, progress=.2, whole=4, remaining=lambda: 4):
        return wait_for_http_child(child, '{}', self.directory, request_id='fixture',
            whole_timeout_s=whole, no_progress_timeout_s=progress, remaining_s=remaining, poll_s=.01)

    def test_no_line_and_blocked_stdin_are_stopped_by_independent_watchdog(self):
        # The child deliberately never reads stdin. A large write would block
        # communicate itself on Windows if it ran in the watchdog thread.
        child = self.child('import time; time.sleep(30)')
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, 'NO_SUBSTANTIVE_PROGRESS_TIMEOUT'):
            wait_for_http_child(child, 'x'*1_000_000, self.directory, request_id='fixture',
                whole_timeout_s=4, no_progress_timeout_s=.2, remaining_s=lambda: 4, poll_s=.01)
        self.assertLess(time.monotonic()-started, 3)
        proof = read_json(self.directory/'process-exit.json')
        self.assertTrue(proof['local_process_terminated'])
        self.assertTrue(proof['termination_requested'])
        self.assertIs(type(proof['returncode']), int)
        self.assertEqual(proof['timeout_classification'], 'NO_SUBSTANTIVE_PROGRESS_TIMEOUT')

    def test_keepalive_receive_and_useless_fragments_do_not_extend_watchdog(self):
        code = '''import sys,time
from pathlib import Path
from week13.src.case_stream import StreamRecorder
r=StreamRecorder(Path(sys.argv[1]),request_id='fixture')
sys.stdin.read()
while True:
 r('STREAM_RECEIVE',received_bytes=12)
 r('STREAM_HEARTBEAT')
 r('STREAM_PROGRESS',event_count=1,content_bytes=0,reasoning_bytes=0,tool_argument_bytes=0,usage_known=False,finish_reason=None,substantive=False)
 time.sleep(.01)
'''
        child = self.child(code)
        with self.assertRaisesRegex(TimeoutError, 'NO_SUBSTANTIVE_PROGRESS_TIMEOUT'):
            self.watch(child, progress=.8)
        timing = read_json(self.directory/'timing.json')
        self.assertGreater(timing['heartbeat_count'], 1)
        self.assertIsNotNone(timing['first_receive_at_utc'])
        self.assertGreaterEqual(timing['last_receive_at_utc'], timing['first_receive_at_utc'])
        self.assertIsNone(timing['last_substantive_progress_at_utc'])
        self.assertTrue(read_json(self.directory/'process-exit.json')['local_process_terminated'])

    def test_substantive_progress_cannot_extend_whole_deadline(self):
        code = '''import sys,time
from pathlib import Path
from week13.src.case_stream import StreamRecorder
r=StreamRecorder(Path(sys.argv[1]),request_id='fixture')
sys.stdin.read()
while True:
 r('STREAM_PROGRESS',event_count=1,content_bytes=1,reasoning_bytes=0,tool_argument_bytes=0,usage_known=False,finish_reason=None,substantive=True)
 time.sleep(.01)
'''
        child = self.child(code)
        with self.assertRaisesRegex(TimeoutError, 'WHOLE_REQUEST_TIMEOUT'):
            self.watch(child, progress=.7, whole=.8)
        self.assertIsNotNone(read_json(self.directory/'timing.json')['last_substantive_progress_at_utc'])
        self.assertEqual(read_json(self.directory/'process-exit.json')['timeout_classification'], 'WHOLE_REQUEST_TIMEOUT')

    def test_wall_deadline_stops_idle_child(self):
        child = self.child('import sys,time; sys.stdin.read(); time.sleep(30)')
        end = time.monotonic()+.2
        with self.assertRaisesRegex(TimeoutError, 'HARD_WALL_TIMEOUT'):
            self.watch(child, progress=4, remaining=lambda: end-time.monotonic())
        self.assertEqual(read_json(self.directory/'process-exit.json')['timeout_classification'], 'HARD_WALL_TIMEOUT')

    def test_completed_child_has_confirmed_exit_without_requested_termination(self):
        child = self.child("import sys; sys.stdin.read(); print('completed')")
        self.assertEqual(self.watch(child,progress=2).strip(), 'completed')
        proof = read_json(self.directory/'process-exit.json')
        self.assertEqual(proof['returncode'], 0)
        self.assertTrue(proof['local_process_terminated'])
        self.assertFalse(proof['termination_requested'])
        self.assertIsNone(proof['timeout_classification'])
        self.assertIsNotNone(proof['observed_exit_at_utc'])

    def test_status_and_exact_progress_timing_survive_progress_throttling(self):
        elapsed = [10.0]
        recorder = StreamRecorder(self.directory, clock=lambda: elapsed[0], now=lambda: 'utc_'+str(elapsed[0]), request_id='fixture')
        recorder('HTTP_STATUS', http_status=200, provider_request_id='safe-123')
        elapsed[0] = 11
        recorder('STREAM_RECEIVE', received_bytes=40)
        recorder('STREAM_PROGRESS', substantive=True, event_count=1, reasoning_bytes=7)
        elapsed[0] = 11.1
        recorder('STREAM_PROGRESS', substantive=True, event_count=2, reasoning_bytes=8)
        elapsed[0] = 12
        recorder('STREAM_RECEIVE', received_bytes=3)
        recorder('STREAM_HEARTBEAT')
        recorder('STREAM_TERMINATED', classification='TRANSPORT_ERROR', error_type='ReadTimeout')
        status, timing = read_json(self.directory/'http-status.json'), read_json(self.directory/'timing.json')
        self.assertEqual(status['http_status'], 200)
        self.assertEqual(status['provider_request_id'], 'safe-123')
        self.assertEqual(timing['first_receive_at_utc'], 'utc_11')
        self.assertEqual(timing['last_receive_at_utc'], 'utc_12')
        self.assertEqual(timing['last_substantive_progress_at_utc'], 'utc_11.1')
        self.assertEqual(timing['terminated_at_utc'], 'utc_12')
        self.assertEqual(timing['received_bytes'], 43)
        self.assertEqual(timing['event_count'], 2)
        with self.assertRaisesRegex(StreamProtocolError, 'UNSAFE'):
            recorder('STREAM_PROGRESS', reasoning_content='secret')
        with self.assertRaisesRegex(StreamProtocolError, 'UNSAFE'):
            recorder('HTTP_STATUS', provider_request_id='Bearer secret\n')

    def test_empty_role_and_whitespace_preserve_protocol_but_not_progress(self):
        elapsed = [0.0]
        stream = StreamAssembler(clock=lambda: elapsed[0])
        def body(delta):
            return json.dumps(dict(id='fixture',model='deepseek-flash',created=1,object='chat.completion.chunk',choices=[dict(index=0,delta=delta,finish_reason=None)]))
        for delta in (dict(role='assistant'), dict(content=''), dict(reasoning_content=' \n'), dict(tool_calls=[])):
            elapsed[0] += 1
            stream.consume(body(delta))
        self.assertEqual(stream.last_substantive, 0)
        self.assertEqual(''.join(stream.reasoning), ' \n')
        self.assertEqual(stream.reasoning_bytes, 2)

    def test_complete_high_capacity_tool_stream_and_safe_diagnostics(self):
        reasoning, arguments = 'PRIVATE_REASONING_'*5000, json.dumps(dict(text='中'*25000))
        chunk = dict(id='fixture', model='deepseek-flash', created=1, object='chat.completion.chunk', choices=[dict(index=0,
            delta=dict(reasoning_content=reasoning, tool_calls=[dict(index=0,id='call_1',type='function',function=dict(name='case_request_plan',arguments=arguments))]),
            finish_reason='tool_calls')], usage=dict(prompt_tokens=700000,completion_tokens=300000,total_tokens=1000000))
        raw = ('data: '+json.dumps(chunk,ensure_ascii=False)+'\r\n\r\ndata: [DONE]\r\n\r\n').encode('utf8')
        class Response:
            status_code = 200
            headers = {'content-type':'text/event-stream', 'x-request-id':'safe-456', 'authorization':'Bearer PRIVATE_KEY'}
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def raise_for_status(self): pass
            def iter_bytes(self):
                for offset in range(0,len(raw),4093): yield raw[offset:offset+4093]
        class Client:
            def stream(self,method,url,**kwargs):
                self.payload=kwargs['json']
                return Response()
        client = Client()
        result = complete_stream(client,url='fixture://offline',headers={'Authorization':'PRIVATE_KEY'},
            payload=dict(max_tokens=393216,thinking=dict(type='enabled')), emit=StreamRecorder(self.directory,request_id='fixture'))
        message = result['choices'][0]['message']
        self.assertEqual(message['reasoning_content'], reasoning)
        self.assertEqual(message['tool_calls'][0]['function']['arguments'], arguments)
        self.assertEqual(client.payload['max_tokens'],393216)
        self.assertEqual(result['usage']['total_tokens'],1000000)
        diagnostic = ''.join(path.read_text(encoding='utf8') for path in self.directory.glob('*.json'))
        self.assertNotIn('PRIVATE_REASONING',diagnostic)
        self.assertNotIn('PRIVATE_KEY',diagnostic)
        self.assertEqual(read_json(self.directory/'http-status.json')['http_status'],200)
        self.assertEqual(read_json(self.directory/'timing.json')['reasoning_bytes'],len(reasoning.encode('utf8')))

    def test_runtime_readonly_authority_restricts_exact_step_tools(self):
        runtime = CaseRuntime.__new__(CaseRuntime)
        runtime.authorization = dict(operation_policy=dict(mode='READONLY_VALIDATION'))
        runtime.config = dict(context_window_tokens=1000000)
        messages = [dict(role='user',content='review')]
        plan_tool = dict(type='function',function=dict(name='case_request_plan'))
        unsafe_tool = dict(type='function',function=dict(name='proposal_prepare'))
        for step in ('explain','reopen'):
            runtime.task_context = dict(access_mode='RESULT_REVIEW_ONLY',step_id=step)
            self.assertNotIn('tools',runtime.build_payload(messages,[])[0])
            with self.assertRaises(PermissionError): runtime.build_payload(messages,[plan_tool])
        for step in ('negative_mass','negative_geometry'):
            runtime.task_context = dict(access_mode='RESULT_REVIEW_ONLY',step_id=step)
            self.assertEqual(runtime.build_payload(messages,[plan_tool])[0]['tools'],[plan_tool])
            for tools in ([],[unsafe_tool],[plan_tool,unsafe_tool]):
                with self.assertRaises(PermissionError): runtime.build_payload(messages,tools)
        runtime.task_context = dict(access_mode='CHANGE',step_id='explain')
        with self.assertRaises(PermissionError): runtime.build_payload(messages,[])

    def test_child_error_diagnostic_failure_never_prints_provider_or_key_text(self):
        from week13.src import case_http
        from week13.src.case_paths import sha_value
        payload = dict(model='deepseek-flash',thinking=dict(type='enabled'),reasoning_effort='high',max_tokens=393216)
        request = dict(request_id='fixture',payload=payload,receipt_directory=str(self.directory/'http'/'child'),
            connect_timeout_s=30,read_timeout_s=600,no_progress_timeout_s=600)
        budget = Mock()
        budget.request.return_value = dict(content_hash=sha_value(payload),status='reserved')
        broken_recorder = Mock(side_effect=ValueError('PRIVATE_DIAGNOSTIC'))
        stdout = io.StringIO()
        with patch.object(case_http,'RUN',self.directory), patch.object(case_http,'CaseBudget',return_value=budget), \
             patch.object(case_http,'_api_key',return_value='PRIVATE_KEY'), patch.object(case_http,'StreamRecorder',return_value=broken_recorder), \
             patch.object(case_http,'complete_stream',side_effect=RuntimeError('PRIVATE_PROVIDER')), \
             patch('httpx.Client'), patch('sys.stdin',io.StringIO(json.dumps(request))), patch('sys.stdout',stdout):
            self.assertEqual(case_http.main(),1)
        self.assertEqual(json.loads(stdout.getvalue()),dict(transport_error='RuntimeError'))
        self.assertNotIn('PRIVATE',stdout.getvalue())
        self.assertEqual(read_json(self.directory/'http'/'child'/'transport-error.json'),dict(error_type='RuntimeError'))


if __name__ == '__main__':
    unittest.main()
