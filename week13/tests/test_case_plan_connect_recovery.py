"""TEST_ONLY isolated request/ledger/transport; never real HTTP/MCP/OMC."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, closing
from unittest.mock import patch

import httpx
from week13.src import case_requests, case_taskflow, case_console, case_console_data, case_cli
from week13.src.case_paths import ROOT, sha_file, sha_value
from week13.tests.test_case_agent_requests import Harness, write

OUT = ROOT / 'results/two-round-agent-test-20261027-01'


class PlanConnectRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.saved = (case_requests.ROOT, case_requests.REQUESTS_ROOT, case_requests.LEGACY_PROJECT_ROOT)
        self.temp = tempfile.TemporaryDirectory(prefix='connect-TEST_ONLY-', dir=OUT)
        self.folder = Path(self.temp.name)
        self.h = Harness(self.folder / 'fixture')
        self.rid = 'TEST_ONLY_connect_plan'
        self.h.bootstrap(self.rid, self.h.project_id, 'v000002', '把总质量改为92kg，增加部分在原重心，几何控制不变。')
        self.h.authorize(self.rid)
        self.sent = []
        transport = self.h.transport

        def isolated(ctx, payload):
            self.sent.append(deepcopy(payload))
            if len(self.sent) == 1:
                raise httpx.ConnectError('TEST_ONLY connection setup failure')
            result = transport(ctx, payload)
            # Current configured strict flat wire protocol, preserving the
            # existing test harness's semantic response values.
            if payload.get('tools'):
                call = result['choices'][0]['message']['tool_calls'][0]
                args = json.loads(call['function']['arguments'])
                if 'packet' in args:
                    packet = args['packet']; design = packet.pop('design')
                    packet.update(design_state='OBJECT', design_summary=design['summary'],
                                  design_assumptions=design['assumptions'], design_parameters=design['parameters'])
                    call['function']['arguments'] = json.dumps(packet, ensure_ascii=False)
            return result

        self.h.transport = isolated
        self.flow = self.h.flow(self.rid)
        with self.assertRaisesRegex(RuntimeError, 'uncertain'):
            asyncio.run(self.flow.plan())
        self.ctx = case_requests.load_request(self.rid)
        self.root = Path(self.ctx['root']); self.old = 'case_plan_' + self.rid
        self.new = self.old + '_connect_retry_1'; self.http = self.root / 'http' / sha_value(self.old)
        write(self.http / 'transport-error.json', dict(error_type='ConnectError'))
        write(self.http / 'timing.json', dict(schema='v12-http-stream-timing-1', request_id=self.old,
              error_type='ConnectError', receive_chunk_count=0, received_bytes=0, event_count=0,
              content_bytes=0, reasoning_bytes=0, tool_argument_bytes=0, usage_known=False,
              first_receive_at_utc=None, finish_reason=None))
        write(self.http / 'process-exit.json', dict(request_id=self.old, fixture_transport=True,
              fixture_call_returned=True, process_launched=False))
        self.approval_path = self.folder / 'TEST_ONLY-approval.json'
        self.authority = case_requests.authority_for(self.ctx)
        self.ledger = Path(self.ctx['runtime_authority']['ledger_path'])
        self.old_row = self.rows()[0]
        self.approval = dict(schema='v12-plan-connect-replacement-approval-1', status='APPROVED',
            approval_channel='USER_CHAT_APPROVAL', approval_quote='TEST_ONLY isolated explicit exception',
            request_id=self.rid, request_digest=self.ctx['request_digest'], project_id=self.ctx['project_id'],
            runtime_authority=self.ctx['runtime_authority'], original_http_request_id=self.old,
            replacement_http_request_id=self.new, payload_sha256=self.old_row['content_hash'],
            original_request_row_sha256=sha_value(self.old_row),
            original_stage_request_sha256=sha_file(self.root / 'turns/plan/request.json'),
            original_http_request_file_sha256=sha_file(self.http / 'request.json'),
            original_error_file_sha256=sha_file(self.http / 'transport-error.json'),
            original_timing_sha256=sha_file(self.http / 'timing.json'),
            original_process_exit_sha256=sha_file(self.http / 'process-exit.json'),
            caps=self.authority['caps'], hard_deadline_utc=self.authority['hard_deadline_utc'],
            maximum_new_http_requests=1, original_unknown_and_full_reservation_preserved=True, test_only=True)
        write(self.approval_path, self.approval)

    def tearDown(self):
        case_requests.ROOT, case_requests.REQUESTS_ROOT, case_requests.LEGACY_PROJECT_ROOT = self.saved
        self.assertTrue(self.folder.resolve().is_relative_to(OUT.resolve()))
        self.temp.cleanup()

    def rows(self):
        ledger = getattr(self, 'ledger', Path(case_requests.load_request(self.rid)['runtime_authority']['ledger_path']))
        with closing(sqlite3.connect(ledger)) as db, db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute('SELECT * FROM requests ORDER BY reserved_at')]

    def amend_hash(self, key, path):
        self.approval[key] = sha_file(path); write(self.approval_path, self.approval)

    def recover(self):
        return asyncio.run(self.flow.recover_plan_connect(self.approval_path))

    def test_exact_saved_payload_success_original_unknown_and_full_reservation_remain(self):
        stage = (self.root / 'turns/plan/request.json').read_bytes()
        unknown = deepcopy(self.old_row)
        # Prove the current plan builder need not reproduce the old prompt:
        # rebuilding with changed context must not affect the replacement wire.
        with patch.object(case_taskflow, 'project_messages', side_effect=AssertionError('must use saved original messages')):
            planned = self.recover()
        self.assertEqual(planned['plan']['proposed_parameters']['m'], 92)
        self.assertEqual(self.sent[0], self.sent[1])
        self.assertEqual((self.root / 'turns/plan/request.json').read_bytes(), stage)
        self.assertEqual(self.rows()[0], unknown)
        self.assertEqual([r['status'] for r in self.rows()], ['unknown', 'succeeded'])
        self.assertTrue((self.root / 'turns/plan/response-connect-binding.json').exists())
        self.assertTrue(self.flow._plan_connect_binding(require_success=True))
        summary = case_console_data.ledger_summary(self.authority)
        self.assertIsNone(summary['blocked'])
        self.assertEqual(summary['retained_unknown_after_explicit_connection_replacement'][0]['request_id'], self.old)
        self.assertEqual(summary['actual_cost_cny'], 'UNKNOWN')
        self.assertEqual(summary['used']['http_requests'], 2)
        self.assertGreaterEqual(summary['used']['product_cny'], 5.24288)
        with self.assertRaises(PermissionError):self.recover()
        self.assertEqual(len(self.sent), 2)

    def test_missing_or_proposed_or_wrong_scope_permission_never_registers_or_dispatches(self):
        for field, value in [('status', 'PROPOSED'), ('approval_quote', ''), ('maximum_new_http_requests', 2),
                             ('payload_sha256', 'bad'), ('hard_deadline_utc', '2099-01-01T00:00:00Z'),
                             ('test_only', False), ('original_request_row_sha256', 'bad')]:
            with self.subTest(field=field):
                changed = dict(self.approval, **{field: value}); write(self.approval_path, changed)
                before = sha_file(self.ledger)
                with self.assertRaises((PermissionError, ValueError)):self.recover()
                self.assertEqual(sha_file(self.ledger), before)
        self.assertEqual(len(self.sent), 1)

    def test_read_or_stream_error_and_received_bytes_are_not_connection_exception(self):
        for error in ('ReadTimeout', 'ReadError', 'RemoteProtocolError'):
            with self.subTest(error=error):
                write(self.http / 'transport-error.json', dict(error_type=error))
                self.amend_hash('original_error_file_sha256', self.http / 'transport-error.json')
                with self.assertRaises(PermissionError):self.recover()
        write(self.http / 'transport-error.json', dict(error_type='ConnectError'))
        self.amend_hash('original_error_file_sha256', self.http / 'transport-error.json')
        timing = json.loads((self.http / 'timing.json').read_text()); timing['received_bytes'] = 1
        write(self.http / 'timing.json', timing); self.amend_hash('original_timing_sha256', self.http / 'timing.json')
        with self.assertRaises(PermissionError):self.recover()
        self.assertEqual(len(self.sent), 1)

    def test_unproven_exit_response_artifact_and_payload_or_dependency_drift_rejected(self):
        exit_path=self.http/'process-exit.json'; original_exit=exit_path.read_bytes()
        write(exit_path,dict(request_id=self.old,process_launched=True,local_process_terminated=False,fixture_transport=False))
        self.amend_hash('original_process_exit_sha256',exit_path)
        with self.assertRaises(PermissionError):self.recover()
        exit_path.write_bytes(original_exit);self.amend_hash('original_process_exit_sha256',exit_path)
        response_path=self.http/'protocol-response.json';write(response_path,dict(test_only=True))
        with self.assertRaises(PermissionError):self.recover()
        response_path.unlink()
        wire_path=self.http/'request.json';original_wire=wire_path.read_bytes();wire=json.loads(original_wire)
        wire['payload']['max_tokens']=1;write(wire_path,wire)
        self.amend_hash('original_http_request_file_sha256',wire_path)
        with self.assertRaises(ValueError):self.recover()
        wire_path.write_bytes(original_wire);self.amend_hash('original_http_request_file_sha256',wire_path)
        wire=json.loads(original_wire);wire['transport_source_sha256']['week13/src/case_http.py']='drift';write(wire_path,wire)
        self.amend_hash('original_http_request_file_sha256',wire_path)
        with self.assertRaises(ValueError):self.recover()
        self.assertEqual(len(self.sent),1)

    def test_other_unknown_or_closed_ledger_never_registers(self):
        with closing(sqlite3.connect(self.ledger)) as db, db:
            columns=','.join(self.old_row); placeholders=','.join('?' for _ in self.old_row)
            second=dict(self.old_row,request_id='TEST_ONLY_other_unknown')
            db.execute('INSERT INTO requests('+columns+') VALUES('+placeholders+')',tuple(second.values()))
        with self.assertRaises(PermissionError):self.recover()
        with closing(sqlite3.connect(self.ledger)) as db, db:
            db.execute('DELETE FROM requests WHERE request_id=?',('TEST_ONLY_other_unknown',))
            db.execute("INSERT INTO meta VALUES('closed','TEST_ONLY closed')")
        with self.assertRaises(PermissionError):self.recover()
        self.assertEqual(len(self.sent),1)

    def test_projection_rejects_extra_unknown_forged_proof_or_failed_replacement(self):
        self.recover()
        with closing(sqlite3.connect(self.ledger)) as db, db:
            columns=','.join(self.old_row);placeholders=','.join('?' for _ in self.old_row)
            db.execute('INSERT INTO requests('+columns+') VALUES('+placeholders+')',tuple(dict(self.old_row,request_id='TEST_ONLY_extra').values()))
        self.assertIsNotNone(case_console_data.ledger_summary(self.authority)['blocked'])
        with closing(sqlite3.connect(self.ledger)) as db, db:db.execute('DELETE FROM requests WHERE request_id=?',('TEST_ONLY_extra',))
        proof=self.http/'explicit-connect-retry.json';saved=proof.read_bytes();value=json.loads(saved);value['payload_sha256']='forged';write(proof,value)
        self.assertIsNotNone(case_console_data.ledger_summary(self.authority)['blocked'])
        proof.write_bytes(saved)
        for status in ('unknown','rejected','dispatched'):
            with self.subTest(status=status):
                with closing(sqlite3.connect(self.ledger)) as db, db:db.execute('UPDATE requests SET status=? WHERE request_id=?',(status,self.new))
                self.assertIsNotNone(case_console_data.ledger_summary(self.authority)['blocked'])
        self.assertEqual(len(self.sent),2)

    def test_projection_other_freeze_missing_usage_changed_wire_and_expiry_block(self):
        self.recover()
        with closing(sqlite3.connect(self.ledger)) as db, db:db.execute("INSERT INTO meta VALUES('frozen','TEST_ONLY different freeze')")
        self.assertIsNotNone(case_console_data.ledger_summary(self.authority)['blocked'])
        with closing(sqlite3.connect(self.ledger)) as db, db:db.execute("DELETE FROM meta WHERE key='frozen'")
        wire_path=self.root/'http'/sha_value(self.new)/'request.json';original=wire_path.read_bytes();wire=json.loads(original);wire['payload_sha256']='changed';write(wire_path,wire)
        self.assertIsNotNone(case_console_data.ledger_summary(self.authority)['blocked'])
        wire_path.write_bytes(original)
        with closing(sqlite3.connect(self.ledger)) as db, db:db.execute('UPDATE requests SET actual_cost=NULL WHERE request_id=?',(self.new,))
        self.assertIsNotNone(case_console_data.ledger_summary(self.authority)['blocked'])
        expired=dict(self.authority,hard_deadline_utc=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat())
        self.assertIsNotNone(case_console_data.ledger_summary(expired)['blocked'])

    def test_console_continuation_retains_unknown_message_and_never_confirms_on_recovery(self):
        self.recover();summary=case_console_data.ledger_summary(self.authority)
        ctx=dict(request_id=self.rid,budget=summary,execution_state='NOT_RUN',proposal_unknown=False)
        class Runner:
            def __init__(self):self.calls=[]
            def data(self,action,**kw):return deepcopy(ctx)
            def call(self,action,**kw):self.calls.append((action,kw));return {'plan':{'intent':'CHANGE'}}
        runner=Runner();lines=[];ui=case_console.Console(runner,console_root=self.folder/'console',output=lines.append)
        ui.selection['request_id']=self.rid
        self.assertTrue(ui.budget_guard(execute=True))
        ui.show_budget(summary,compact=True)
        self.assertIn('实际费用UNKNOWN','\n'.join(lines))
        with patch.object(ui,'show_plan') as shown:
            ui.handle('/recover connect '+str(self.approval_path))
            shown.assert_called_once()
        self.assertEqual([x[0] for x in runner.calls],['recover-plan-connect'])

    def test_public_cli_requires_permission_and_dispatches_only_explicit_recovery_action(self):
        out=io.StringIO()
        with redirect_stdout(out):code=case_cli.main(['recover-plan-connect','--request-id',self.rid],flow_factory=self.h.flow)
        self.assertEqual(code,1);self.assertEqual(len(self.sent),1)
        out=io.StringIO()
        with redirect_stdout(out):code=case_cli.main(['recover-plan-connect','--request-id',self.rid,'--approval-evidence',str(self.approval_path)],flow_factory=self.h.flow)
        self.assertEqual(code,0,out.getvalue());self.assertEqual(len(self.sent),2)

    def test_independent_cli_selects_target_before_context_import_without_http(self):
        # A deliberately nonexistent TEST_ONLY context stops import before any
        # runtime/budget/transport construction. The missing path proves which
        # selector the actual -m entry installed, overriding a hostile old env.
        target='TEST_ONLY_connect_selector_before_import'
        env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',MBSE_V12_REQUEST_ID='TEST_ONLY_wrong_inherited_selector')
        child=subprocess.run([sys.executable,'-X','utf8','-B','-m','week13.src.case_cli',
            'recover-plan-connect','--request-id',target,'--approval-evidence','TEST_ONLY_missing'],
            cwd=ROOT.parent,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf8',timeout=20)
        self.assertNotEqual(child.returncode,0)
        self.assertIn(target,child.stderr)
        self.assertNotIn('TEST_ONLY_wrong_inherited_selector',child.stderr)
        self.assertIn('FileNotFoundError',child.stderr)
        self.assertEqual(len(self.sent),1)


if __name__=='__main__':unittest.main()
