"""Public failure guard and read-only historical EOF regression fixtures."""
import asyncio
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from week13.src import case_taskflow as tf,case_cli
from week13.src.case_paths import ROOT,read_json,save_json
from week13.src.case_protocol_failures import failure_fingerprint,RepeatedProtocolFailure
from week13.src.case_strict_tools import FLAT_PLAN_PROFILE

PREP=ROOT/'results/shallow-plan-protocol-20261022-03'


def response(text):return dict(finish_reason='tool_calls',tool_calls=[dict(type='function',function=dict(name='case_request_plan',arguments=text))])


class ProtocolFingerprint(unittest.TestCase):
    def setUp(self):
        PREP.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='fingerprint-',dir=PREP);self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
    def flow(self):
        flow=object.__new__(tf.TaskFlow);flow.root=self.root
        flow.ctx=dict(request_id='fixture',request_digest='fixture',runtime_authority=dict(ledger_path=str(self.root/'never-real.sqlite')))
        return flow
    def test_actual_three_replies_remain_invalid_and_same_EOF_fingerprint_without_writes(self):
        r=ROOT/'results/strict-plan-protocol-preparation-20261022-02';report=read_json(r/'a-plan-failure-forensics.json')
        signatures=[]
        for entry in report['calls']:
            path=Path(entry['evidence']['protocol-response.json']['path']);before=path.read_bytes();raw=json.loads(before)
            view=dict(finish_reason='tool_calls',tool_calls=raw['choices'][0]['message']['tool_calls'])
            text=view['tool_calls'][0]['function']['arguments']
            with self.assertRaises(json.JSONDecodeError):json.loads(text)
            signatures.append(failure_fingerprint('plan',entry['ledger_reason'],view)['sha256'])
            self.assertEqual(path.read_bytes(),before)
        self.assertEqual(len(set(signatures)),1)
    def test_distinct_shapes_paths_stages_and_interior_unknown_are_not_merged(self):
        a=failure_fingerprint('plan','different offset',response('{"packet":{}'))
        b=failure_fingerprint('plan','different offset',response('{"packet":['))
        self.assertNotEqual(a['sha256'],b['sha256'])
        self.assertNotEqual(a['sha256'],failure_fingerprint('generate','x',response('{"packet":{}'))['sha256'])
        clean=response('{}')
        one=failure_fingerprint('plan','tool object fields missing: $.packet.scope',clean)
        two=failure_fingerprint('plan','tool object fields missing: $.packet.rationale',clean)
        self.assertNotEqual(one['sha256'],two['sha256'])
        self.assertIsNone(failure_fingerprint('plan','business disagreement with values 1',clean))
        self.assertIsNone(failure_fingerprint('plan','x',response('{"a": bad}')))
    def old_failures(self):
        flow=self.flow()
        for index,text in enumerate(('{"packet":{}','{"packet":{   }')):
            try:json.loads(text)
            except ValueError as exc:error=type(exc).__name__+': '+str(exc)
            save_json(self.root/'failures'/('plan-'+str(index)+'.json'),dict(stage='plan',attempt=index,error=error,repairable=True,response=response(text),confirmed_plan=None,physical_scope_expansion_allowed=False),immutable=True)
        async def plan(attempt=0):return flow.feedback('plan',attempt)
        flow.plan=plan
        return flow
    def test_public_bounded_manual_repair_and_reopen_stop_before_budget_consume(self):
        flow=self.old_failures();before={p:p.read_bytes() for p in (self.root/'failures').iterdir()}
        budget=Mock()
        with patch('week13.src.case_requests.require_planning',return_value=flow.ctx),patch('week13.src.case_budget.CaseBudget',return_value=budget) as constructor:
            for current in (flow,self.flow()):
                async def plan(attempt=0):return current.feedback('plan',attempt)
                current.plan=plan
                with self.assertRaises(RepeatedProtocolFailure):asyncio.run(current.repair('plan',2))
                with self.assertRaises(RepeatedProtocolFailure):asyncio.run(current.bounded('plan',current.plan))
            constructor.assert_not_called();budget.consume.assert_not_called()
        self.assertEqual(before,{p:p.read_bytes() for p in before})
    def test_actual_CLI_routes_report_repeat_reason_without_false_budget_exhaustion(self):
        flow=self.old_failures();budget=Mock()
        with patch('week13.src.case_requests.require_planning',return_value=flow.ctx),patch('week13.src.case_budget.CaseBudget',return_value=budget):
            for argv in (['plan-request','--request-id','fixture'],['repair-request','--request-id','fixture','--stage','plan','--attempt','2']):
                output=io.StringIO()
                with redirect_stdout(output):code=case_cli.main(argv,flow_factory=lambda rid:flow)
                self.assertEqual(code,1);message=json.loads(output.getvalue())['error']
                self.assertIn('同类协议错误重复',message);self.assertNotIn('exhaust',message.lower())
        budget.consume.assert_not_called()
    def test_console_first_line_classifies_protocol_repeat_distinct_from_budget(self):
        from week13.src.case_console import classify_error
        kind,message=classify_error('RepeatedProtocolFailure: 同类协议错误重复，已停止自动修复。')
        self.assertEqual(kind,'protocol_repeat');self.assertIn('同类协议错误重复',message)
        self.assertNotIn('预算耗尽',message)
    def test_first_feedback_consumes_once_new_failure_records_fingerprint_and_old_not_rewritten(self):
        flow=self.flow();raw=response('{"packet":{}')
        try:json.loads(raw['tool_calls'][0]['function']['arguments'])
        except ValueError as exc:
            first=flow.failure('plan',0,exc,raw);self.assertIsNotNone(first['protocol_failure_fingerprint'])
        budget=Mock();config=read_json(ROOT/'configs/case_runtime.json')
        with patch('week13.src.case_requests.require_planning',return_value=flow.ctx),patch('week13.src.case_requests.authority_for',return_value=dict(model_controls=config)),patch('week13.src.case_budget.CaseBudget',return_value=budget),patch.object(tf,'emit_progress'):
            feedback=flow.feedback('plan',1)
        budget.consume.assert_called_once();self.assertIn('design_state',feedback['required_action']);self.assertNotIn('packet JSON对象',feedback['required_action'])


if __name__=='__main__':unittest.main()
