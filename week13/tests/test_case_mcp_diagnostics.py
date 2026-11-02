"""No API/MCP process/OMC: bounded exception and UNKNOWN mutation fixtures."""
import asyncio
from contextlib import asynccontextmanager, contextmanager, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from week13.src import case_diagnostics as diagnostics, case_requests
from week13.src import case_taskflow as taskflow, case_cli, case_mcp

SECRET='TEST_ONLY_secret_do_not_log_headers_payload_or_model_context'


class SafeExceptionRecords(unittest.TestCase):
    def test_nested_group_cause_context_and_messages_are_bounded(self):
        try:
            try:raise TimeoutError(SECRET)
            except TimeoutError as cause:raise ValueError(SECRET) from cause
        except ValueError as value:
            grouped=ExceptionGroup(SECRET,[ExceptionGroup('unhandled errors in a TaskGroup',[value])])
        record=diagnostics.exception_record(grouped)
        self.assertEqual(record['details']['leaf_types'],['ValueError','TimeoutError'])
        self.assertEqual(record['details']['exception']['exceptions'][0]['exceptions'][0]['cause']['type'],'TimeoutError')
        self.assertNotIn(SECRET,json.dumps(record))
        self.assertFalse(record['details']['retry_authorized'])

    def test_cancelled_leaf_in_base_exception_group_is_not_lost(self):
        error=BaseExceptionGroup(SECRET,[asyncio.CancelledError(SECRET),TimeoutError(SECRET)])
        record=diagnostics.exception_record(error)
        self.assertEqual(record['details']['leaf_types'],['CancelledError','TimeoutError'])
        self.assertNotIn(SECRET,json.dumps(record))

    def test_phase_timings_have_exact_names_and_safe_error_without_arguments(self):
        stderr=io.StringIO()
        with redirect_stderr(stderr),self.assertRaises(ValueError):
            with diagnostics.phase('BASELINE_RESOLVE',payload=SECRET):raise ValueError(SECRET)
        events=[json.loads(line) for line in stderr.getvalue().splitlines()]
        self.assertEqual([x['mcp_phase'] for x in events],['BASELINE_RESOLVE_ENTER','BASELINE_RESOLVE_ERROR'])
        self.assertGreaterEqual(events[1]['monotonic'],events[0]['monotonic'])
        self.assertGreaterEqual(events[1]['elapsed_s'],0)
        self.assertNotIn(SECRET,stderr.getvalue())

    def test_server_method_records_causal_types_without_exception_payload(self):
        def fail():raise ExceptionGroup(SECRET,[TimeoutError(SECRET)])
        service=SimpleNamespace(proposal_prepare=fail)
        case_mcp.trace_method(service,'proposal_prepare');stderr=io.StringIO()
        with redirect_stderr(stderr),self.assertRaises(ExceptionGroup):service.proposal_prepare()
        terminal=json.loads(stderr.getvalue().splitlines()[-1])
        self.assertEqual(terminal['mcp_phase'],'proposal_prepare_ERROR')
        self.assertEqual(terminal['details']['leaf_types'],['TimeoutError'])
        self.assertNotIn(SECRET,stderr.getvalue())

    def test_existing_explicit_cap_is_used_without_increasing_default(self):
        self.assertEqual(diagnostics.timeout_binding({},300)['actual_cap_s'],120)
        binding=diagnostics.timeout_binding({'timeouts_s':{'mcp_request':45}},80,authority_sha256='a'*64)
        self.assertEqual(binding['actual_cap_s'],45)
        self.assertEqual(binding['cap_source'],'runtime_authority.timeouts_s.mcp_request')
        self.assertEqual(diagnostics.timeout_binding({'timeouts_s':{'mcp_request':600}},7)['actual_cap_s'],7)
        for invalid in (0,-1,True,float('inf'),float('nan')):
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):diagnostics.timeout_binding({'timeouts_s':{'mcp_request':invalid}},10)
        with self.assertRaises(TimeoutError):diagnostics.timeout_binding({},0)


class MCPObservations(unittest.IsolatedAsyncioTestCase):
    async def test_channel_startup_failure_is_observed_without_claiming_tool_dispatch(self):
        import mcp.client.stdio
        flow=taskflow.TaskFlow.__new__(taskflow.TaskFlow)
        flow.ctx=dict(request_id='TEST_ONLY_request',runtime_authority={'sha256':'b'*64});flow.rpc=None
        @asynccontextmanager
        async def broken_stdio(params):
            raise ValueError(SECRET)
            yield  # pragma: no cover
        with patch.object(mcp.client.stdio,'stdio_client',broken_stdio),patch.object(case_requests,'authority_for',return_value={}),patch.object(case_requests,'remaining_request',return_value=20):
            try:await flow.mcp('proposal_prepare',{'spec':SECRET})
            except ValueError as exc:record=diagnostics.failure_record(exc)
            else:self.fail('fixture should fail')
        self.assertFalse(record['details']['mcp_calls'][0]['rpc_dispatched'])
        self.assertEqual(record['details']['mcp_calls'][0]['outcome'],'NOT_DISPATCHED')
        self.assertNotIn(SECRET,json.dumps(record))

    async def test_taskgroup_cleanup_wrapper_keeps_exact_failed_call_and_timeout_cap(self):
        import mcp
        import mcp.client.stdio
        flow=taskflow.TaskFlow.__new__(taskflow.TaskFlow)
        flow.ctx=dict(request_id='TEST_ONLY_request',runtime_authority={'sha256':'b'*64});flow.rpc=None
        @asynccontextmanager
        async def isolated_stdio(params):
            try:yield object(),object()
            except Exception as exc:raise ExceptionGroup('unhandled errors in a TaskGroup',[exc]) from None
        class Session:
            def __init__(self,*args,**kwargs):pass
            async def __aenter__(self):return self
            async def __aexit__(self,*args):return False
            async def initialize(self):pass
            async def call_tool(self,*args,**kwargs):raise TimeoutError(SECRET)
        with patch.object(mcp,'ClientSession',Session),patch.object(mcp.client.stdio,'stdio_client',isolated_stdio),patch.object(case_requests,'authority_for',return_value={'timeouts_s':{'mcp_request':12}}),patch.object(case_requests,'remaining_request',return_value=20),redirect_stderr(io.StringIO()):
            try:await flow.mcp('proposal_prepare',{'spec':SECRET})
            except ExceptionGroup as exc:record=diagnostics.failure_record(exc)
            else:self.fail('fixture should fail')
        self.assertEqual(record['details']['leaf_types'],['TimeoutError'])
        self.assertEqual(record['details']['mcp_calls'][-1]['tool'],'proposal_prepare')
        self.assertEqual(record['details']['mcp_calls'][-1]['actual_cap_s'],12)
        self.assertNotIn(SECRET,json.dumps(record))
        self.assertIsNone(flow._session)

    async def test_taskflow_call_uses_approved_cap_and_exposes_timeout_leaf(self):
        flow=taskflow.TaskFlow.__new__(taskflow.TaskFlow)
        flow.ctx=dict(request_id='TEST_ONLY_request',runtime_authority={'sha256':'b'*64});flow.rpc=None
        caps=[]
        async def fail(name,args,**kwargs):
            caps.append(kwargs['read_timeout_seconds'].total_seconds())
            raise ExceptionGroup(SECRET,[TimeoutError(SECRET)])
        flow._session=SimpleNamespace(call_tool=fail)
        stderr=io.StringIO()
        with patch.object(case_requests,'authority_for',return_value={'timeouts_s':{'mcp_request':40}}),patch.object(case_requests,'remaining_request',return_value=23),redirect_stderr(stderr):
            try:await flow.mcp('proposal_prepare',{'spec':SECRET})
            except ExceptionGroup as exc:record=diagnostics.failure_record(exc)
            else:self.fail('fixture should fail')
        self.assertEqual(caps,[23])
        observed=record['details']['mcp_calls'][0]
        self.assertEqual(observed['tool'],'proposal_prepare')
        self.assertIsNone(observed['rpc_dispatched'])
        self.assertTrue(observed['call_invoked'])
        self.assertEqual(observed['outcome'],'UNKNOWN_AFTER_CALL_INVOCATION')
        self.assertEqual(observed['actual_cap_s'],23)
        self.assertIsNone(observed['remote_cancellation_proven'])
        self.assertNotIn(SECRET,json.dumps(record)+stderr.getvalue())
        self.assertIn('week13/src/case_taskflow.py',json.dumps(record))

    async def test_expired_budget_stops_before_call(self):
        flow=taskflow.TaskFlow.__new__(taskflow.TaskFlow)
        flow.ctx=dict(request_id='TEST_ONLY_request',runtime_authority={'sha256':'b'*64});flow.rpc=None
        calls=[]
        async def unexpected(*args,**kwargs):calls.append(True)
        flow._session=SimpleNamespace(call_tool=unexpected)
        with patch.object(case_requests,'authority_for',return_value={}),patch.object(case_requests,'remaining_request',return_value=-1):
            with self.assertRaises(TimeoutError):await flow.mcp('proposal_prepare',{'spec':SECRET})
        self.assertEqual(calls,[])

    async def test_cli_call_rejection_redacts_payload_and_uses_configured_cap(self):
        caps=[]
        async def rejected(name,args,**kwargs):
            caps.append(kwargs['read_timeout_seconds'].total_seconds())
            return SimpleNamespace(isError=True,content=[SimpleNamespace(type='text',text=SECRET)])
        with patch.object(case_cli,'authorization',return_value={'timeouts_s':{'mcp_request':31}}),patch.object(case_cli,'authorization_sha',return_value='c'*64),patch.object(case_cli,'remaining',return_value=50),redirect_stderr(io.StringIO()):
            with self.assertRaises(ValueError) as captured:await case_cli._mcp_context_call('proposal_prepare',{'spec':SECRET},session=SimpleNamespace(call_tool=rejected))
        self.assertEqual(caps,[31])
        self.assertNotIn(SECRET,json.dumps(diagnostics.failure_record(captured.exception)))

    async def test_unknown_markers_keep_nested_timeout_valueerror_and_cancellation_nonretryable(self):
        @contextmanager
        def reading():
            yield SimpleNamespace(execute=lambda *args:SimpleNamespace(fetchall=lambda:[]))
        for kind in ('timeout','value','cancel'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory(prefix='TEST_ONLY_mcp_diagnostics_',dir=taskflow.ROOT/'runs') as temp:
                calls=[];flow=taskflow.TaskFlow.__new__(taskflow.TaskFlow)
                flow.ctx=dict(request_id='TEST_ONLY_request',project_id='TEST_ONLY_project',runtime_authority={'ledger_path':str(Path(temp)/'budget.sqlite')});flow.root=Path(temp)
                flow.open=lambda **kwargs:SimpleNamespace(session_id='TEST_ONLY_session',store=SimpleNamespace(reading=reading))
                async def rpc(*args):
                    calls.append(True)
                    if kind=='cancel':raise asyncio.CancelledError(SECRET)
                    leaf=TimeoutError(SECRET) if kind=='timeout' else ValueError(SECRET)
                    raise ExceptionGroup(SECRET,[ExceptionGroup('unhandled errors in a TaskGroup',[leaf])])
                flow.rpc=rpc
                from week13.src import case_local_proposal_recovery as recovery,case_budget
                checked={'spec_digest':'d'*64}
                with patch.object(recovery,'cache'),patch.object(recovery,'preflight'),patch.object(taskflow,'baseline_sysml_for_context'),patch.object(case_budget,'CaseBudget'),redirect_stderr(io.StringIO()):
                    with self.assertRaises(asyncio.CancelledError if kind=='cancel' else ExceptionGroup):await flow._prepare_generation({}, {},checked,None)
                    marker=flow.root/'proposal-unknown.json';before=marker.read_bytes();saved=json.loads(before)
                    self.assertEqual(saved['status'],'UNKNOWN')
                    self.assertEqual(saved['details']['leaf_types'],[{'timeout':'TimeoutError','value':'ValueError','cancel':'CancelledError'}[kind]])
                    self.assertFalse(saved['details']['retry_authorized'])
                    self.assertNotIn(SECRET,before.decode())
                    with self.assertRaisesRegex(ValueError,'no blind resend'):await flow._prepare_generation({}, {},checked,None)
                    self.assertEqual(marker.read_bytes(),before)
                    self.assertEqual(calls,[True])

    async def test_cli_main_keeps_error_field_and_leaf_details_for_grouped_failure(self):
        class Flow:
            async def execute(self):raise ExceptionGroup(SECRET,[ValueError(SECRET)])
        # Main executes asyncio.run(), so invoke the synchronous CLI off this loop.
        with tempfile.TemporaryDirectory(prefix='TEST_ONLY_cli_diagnostics_') as temp:
            (Path(temp)/'generation.json').write_text('{}')
            flow=Flow();flow.root=Path(temp)
            def call():
                stdout=io.StringIO()
                with redirect_stdout(stdout):code=case_cli.main(['execute-request','--request-id','TEST_ONLY_request'],flow_factory=lambda _:flow)
                return code,json.loads(stdout.getvalue())
            code,result=await asyncio.to_thread(call)
        self.assertEqual(code,1)
        self.assertTrue(result['error'].startswith('ExceptionGroup:'))
        self.assertEqual(result['details']['leaf_types'],['ValueError'])
        self.assertNotIn(SECRET,json.dumps(result))


if __name__=='__main__':unittest.main()
