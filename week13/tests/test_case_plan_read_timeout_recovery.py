"""TEST_ONLY same-ledger recovery; local fixtures never contact a provider."""
import asyncio
from contextlib import closing, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import httpx
from week13.src import case_requests,case_taskflow,case_console,case_console_data,case_cli,case_http,case_plan_recovery
from week13.src.case_budget import connection
from week13.src.case_paths import ROOT,sha_file,sha_value
from week13.src.case_runtime import ProductHTTPError,transport_failure
from week13.src.case_stream import StreamAssembler,StreamProtocolError
from week13.tests.test_case_agent_requests import Harness,write

OUT=ROOT/'results/http-timeout-recovery-20261029-01'

class PlanReadTimeoutRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.saved=(case_requests.ROOT,case_requests.REQUESTS_ROOT,case_requests.LEGACY_PROJECT_ROOT)
        self.temp=tempfile.TemporaryDirectory(prefix='TEST_ONLY-read-timeout-',dir=OUT);self.folder=Path(self.temp.name)
        self.h=Harness(self.folder/'fixture')
        config=json.loads((self.h.root/'configs/case_runtime.json').read_text());config['read_timeout_s']=180
        write(self.h.root/'configs/case_runtime.json',config)
        self.rid='TEST_ONLY_read_timeout_plan';self.h.bootstrap(self.rid,self.h.project_id,'v000002','把总质量改为92kg，增加部分在原重心，几何控制不变。');self.h.authorize(self.rid)
        self.sent=[];transport=self.h.transport
        def isolated(ctx,payload):
            self.sent.append(deepcopy(payload))
            if len(self.sent)==1:raise httpx.ReadTimeout('PRIVATE_PROVIDER_MUST_NOT_LOG')
            if not payload.get('tools'):
                return dict(id='TEST_ONLY_followup',model='deepseek-flash',choices=[dict(message=dict(content='TEST_ONLY read-only consultation',reasoning_content='TEST_ONLY protocol'),finish_reason='stop')],usage=dict(prompt_tokens=20,completion_tokens=10,total_tokens=30))
            result=transport(ctx,payload)
            call=result['choices'][0]['message'].get('tool_calls',[None])[0]
            if call:
                args=json.loads(call['function']['arguments'])
                if 'packet' in args:
                    packet=args['packet'];design=packet.pop('design');packet.update(design_state='OBJECT',design_summary=design['summary'],design_assumptions=design['assumptions'],design_parameters=design['parameters']);call['function']['arguments']=json.dumps(packet,ensure_ascii=False)
                elif 'spec' in args:
                    args['spec']['sources']=[dict(id=source['id']) for source in args['spec']['sources']];call['function']['arguments']=json.dumps(args,ensure_ascii=False)
            return result
        self.h.transport=isolated;self.flow=self.h.flow(self.rid)
        with self.assertRaises(ProductHTTPError) as caught:asyncio.run(self.flow.plan())
        self.failure=caught.exception;self.ctx=case_requests.load_request(self.rid);self.root=Path(self.ctx['root']);self.old='case_plan_'+self.rid;self.new=self.old+'_read_timeout_replacement_1';self.http=self.root/'http'/sha_value(self.old)
        write(self.http/'transport-error.json',dict(error_type='ReadTimeout'))
        write(self.http/'timing.json',dict(schema='v12-http-stream-timing-1',request_id=self.old,error_type='ReadTimeout',receive_chunk_count=0,received_bytes=0,event_count=0,content_bytes=0,reasoning_bytes=0,tool_argument_bytes=0,usage_known=False,first_receive_at_utc=None,finish_reason=None))
        write(self.http/'process-exit.json',dict(request_id=self.old,fixture_transport=True,fixture_call_returned=True,process_launched=False))
        self.authority=case_requests.authority_for(self.ctx);self.ledger=Path(self.authority['ledger_path']);self.old_row=self.rows()[0]
        self.approval_path=self.folder/'TEST_ONLY-approval.json';self.approve()
    def tearDown(self):
        case_requests.ROOT,case_requests.REQUESTS_ROOT,case_requests.LEGACY_PROJECT_ROOT=self.saved;self.temp.cleanup()
    def rows(self):
        with connection(self.ledger) as db:return [dict(r) for r in db.execute('SELECT * FROM requests ORDER BY reserved_at')]
    def approve(self):
        self.approval=dict(self.flow.plan_recovery_info()['approval_template'],status='APPROVED',approval_quote='TEST_ONLY explicit user one-time replacement',approved_at_utc=datetime.now(timezone.utc).isoformat());write(self.approval_path,self.approval)
    def recover(self):return asyncio.run(self.flow.recover_plan_read_timeout(self.approval_path))
    def test_same_payload_success_preserves_unknown_freeze_and_allows_only_same_request_after_plan(self):
        before=sha_file(self.ledger);info=self.flow.plan_recovery_info();self.assertEqual(sha_file(self.ledger),before);self.assertEqual(info['product_http_added'],0)
        with patch.object(case_taskflow,'project_messages',side_effect=AssertionError('original saved messages required')):result=self.recover()
        self.assertEqual(result['plan']['proposed_parameters']['m'],92);self.assertEqual(self.sent[0],self.sent[1]);self.assertEqual(self.rows()[0],self.old_row)
        self.assertEqual([r['status'] for r in self.rows()],['unknown','succeeded']);self.assertEqual(self.rows()[0]['reserve_cost'],5.24288)
        with connection(self.ledger) as db:
            self.assertEqual(dict(db.execute('SELECT key,value FROM meta'))['frozen'],case_plan_recovery.FREEZE)
            self.assertTrue(case_plan_recovery.registered(db,case_requests.load_request(self.rid),self.authority,require_plan=True))
            self.assertIsNone(case_plan_recovery.registered(db,self.ctx,self.authority,'case_ask_1_other'))
        summary=case_console_data.ledger_summary(self.authority);self.assertIsNone(summary['blocked']);self.assertEqual(summary['actual_cost_cny'],'UNKNOWN');self.assertEqual(summary['retained_unknown_after_explicit_read_timeout_replacement'][0]['request_id'],self.old)
        rt=self.h.runtime(case_requests.load_request(self.rid));self.assertEqual(rt.config['read_timeout_s'],180)
        follow=rt.complete([dict(role='user',content='TEST_ONLY consultation')],[],'case_ask_1_'+self.rid);self.assertTrue(follow);self.assertEqual(len(self.sent),3)
        with self.assertRaises(PermissionError):self.recover()
        self.assertFalse((self.root/'generation.json').exists())
    def test_reserve_launch_transitions_exact_600_child_validation_and_other_rid_block(self):
        info,proof=case_plan_recovery.approved(self.flow,self.approval_path);rt=self.h.runtime(self.ctx);rt.budget.authorize_plan_read_timeout(proof)
        effective,override=rt.budget.effective_http_transport(self.new);self.assertEqual(effective['read_timeout_s'],600);self.assertEqual(rt.config['read_timeout_s'],180)
        request=dict(request_id=self.new,timeout_override=override,**effective);self.assertEqual(case_http.effective_timeouts(request,rt.budget)[0],effective)
        for k,v in [('read_timeout_s',180),('connect_timeout_s',31),('whole_request_deadline_s',5401),('timeout_override',None)]:
            with self.assertRaises(PermissionError):case_http.effective_timeouts(dict(request,**{k:v}),rt.budget)
        with self.assertRaisesRegex(RuntimeError,'frozen'):rt.budget.reserve_http('case_ask_1_'+self.rid,'other','consultation',rt.config)
        rt.budget.reserve_http(self.new,sha_value(info['payload']),'design',rt.config)
        self.assertEqual(rt.budget.effective_http_transport(self.new)[0]['read_timeout_s'],600)
        rt.budget.launch_http(self.new,fixture=True);self.assertEqual(rt.budget.request(self.new)['status'],'dispatched')
        with self.assertRaises(RuntimeError):rt.budget.launch_http(self.new,fixture=True)
        rt.budget.finish_http(self.new,error='TEST_ONLY stopped',uncertain=True)
        with self.assertRaises(PermissionError):self.recover()
    def test_absent_approval_wrong_binding_expiry_or_insufficient_budget_never_dispatch(self):
        for key,value in [('status','PENDING_USER_APPROVAL'),('approval_quote',''),('maximum_new_http_requests',2),('payload_sha256','bad'),('baseline_ref',{}),('hard_deadline_utc','2099-01-01T00:00:00Z'),('original_request_row_sha256','bad')]:
            write(self.approval_path,dict(self.approval,**{key:value}));before=sha_file(self.ledger)
            with self.assertRaises((PermissionError,ValueError)):self.recover()
            self.assertEqual(sha_file(self.ledger),before)
        write(self.approval_path,self.approval)
        expired=dict(self.authority,hard_deadline_utc=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat())
        with patch.object(case_plan_recovery,'authority_for',return_value=expired):
            with self.assertRaises(PermissionError):self.flow.plan_recovery_info()
        limited=deepcopy(self.authority);limited['caps']['http_requests']=3
        with patch.object(case_plan_recovery,'authority_for',return_value=limited):
            with self.assertRaises(PermissionError):self.flow.plan_recovery_info()
        self.assertEqual(len(self.sent),1)
    def test_other_unknown_inflight_or_binding_drift_blocks(self):
        with connection(self.ledger) as db:
            cols=','.join(self.old_row);q=','.join('?' for _ in self.old_row);other=dict(self.old_row,request_id='TEST_ONLY_other_unknown');db.execute('INSERT INTO requests('+cols+') VALUES('+q+')',tuple(other.values()))
        with self.assertRaises(PermissionError):self.recover()
        with connection(self.ledger) as db:db.execute('DELETE FROM requests WHERE request_id=?',('TEST_ONLY_other_unknown',));db.execute("INSERT INTO meta VALUES('closed','TEST_ONLY closed')")
        with self.assertRaises(PermissionError):self.recover()
        self.assertEqual(len(self.sent),1)
    def test_second_timeout_keeps_two_unknowns_and_no_automatic_repair_or_third_send(self):
        self.h.transport=lambda *a:(_ for _ in ()).throw(httpx.ReadTimeout('PRIVATE'))
        with self.assertRaises(ProductHTTPError):self.recover()
        self.assertEqual([r['status'] for r in self.rows()],['unknown','unknown']);self.assertEqual(sum(r['reserve_cost'] for r in self.rows()),10.48576)
        self.assertIsNotNone(case_console_data.ledger_summary(self.authority)['blocked'])
        with self.assertRaises(PermissionError):self.recover()
        with self.assertRaises(PermissionError):asyncio.run(self.flow.plan())
        with connection(self.ledger) as db:self.assertEqual(db.execute("SELECT count(*) FROM counters WHERE kind='software_repair'").fetchone()[0],0)
    def test_complete_saved_original_reply_is_zero_http_priority_and_partial_refused(self):
        wire=json.loads((self.http/'request.json').read_text());reply=self.h.transport(self.ctx,wire['payload']);self.sent.pop()
        write(self.http/'protocol-response.json',reply)
        with self.assertRaisesRegex(PermissionError,'zero-HTTP'):self.recover()
        before_count=len(self.sent)
        with patch.object(self.h,'transport',side_effect=AssertionError('zero HTTP required')):result=asyncio.run(self.flow.recover_response())
        self.assertEqual(result['product_http_added'],0);self.assertEqual(len(self.sent),before_count);self.assertEqual(self.rows()[0]['status'],'succeeded');self.assertEqual(result['plan']['proposed_parameters']['m'],92)
    def test_missing_complete_reply_never_reconstructs_partial_text(self):
        write(self.http/'partial.json',dict(content='half a plan',finish_reason='tool_calls'))
        before=sha_file(self.ledger)
        with self.assertRaisesRegex(RuntimeError,'no complete saved'):asyncio.run(self.flow.recover_response())
        self.assertEqual(sha_file(self.ledger),before);self.assertEqual(len(self.sent),1)
    def test_safe_specific_failure_fields_exclude_provider_text(self):
        self.assertEqual(self.failure.transport_failure['classification'],'READ_IDLE_TIMEOUT');self.assertNotIn('PRIVATE',str(self.failure));self.assertFalse(self.failure.transport_failure['saved_complete_response'])
        for kind,classification in [('ConnectError','CONNECT_ERROR'),('ConnectTimeout','CONNECT_TIMEOUT'),('ReadError','STREAM_INTERRUPTED'),('RemoteProtocolError','STREAM_INTERRUPTED')]:
            write(self.http/'transport-error.json',dict(error_type=kind));result=transport_failure(self.http,self.old,RuntimeError('PRIVATE_KEY_OR_REASONING'));self.assertEqual(result['classification'],classification);self.assertNotIn('PRIVATE',json.dumps(result))
        self.assertEqual(case_console.classify_error(str(self.failure))[0],'read_timeout')
    def test_public_cli_requires_explicit_approval_and_never_runs_execution(self):
        code,result=self.h.cli(['request-plan-recover-timeout','--request-id',self.rid]);self.assertEqual(code,1);self.assertEqual(len(self.sent),1)
        code,result=self.h.cli(['request-plan-recovery-info','--request-id',self.rid]);self.assertEqual(code,0,result);self.assertEqual(result['product_http_added'],0)
        code,result=self.h.cli(['request-plan-recover-timeout','--request-id',self.rid,'--approval-evidence',str(self.approval_path)]);self.assertEqual(code,0,result);self.assertFalse(result['generation_compile_simulation_started']);self.assertEqual(len(self.sent),2)
    def test_console_shows_concrete_risk_then_exact_confirmation_once(self):
        info=self.flow.plan_recovery_info();calls=[];ctx=dict(request_id=self.rid,project_id=self.ctx['project_id'],plan=None)
        class Runner:
            def data(s,*a,**kw):return deepcopy(ctx)
            def call(s,action,**kw):
                calls.append((action,kw))
                return deepcopy(info) if action=='request-plan-recovery-info' else dict(plan={})
        lines=[];ui=case_console.Console(Runner(),console_root=self.folder/'console',output=lines.append);ui.selection['request_id']=self.rid;ui.catalog=dict(case_id='TEST_ONLY',project_id=self.ctx['project_id'])
        with patch.object(ui,'show_plan'),patch.object(case_console,'ROOT',self.folder):
            ui.handle('/recover plan');self.assertEqual([x[0] for x in calls],['request-plan-recovery-info']);self.assertIn('可能重复计费','\n'.join(lines));ui.handle('确认恢复方案')
            self.assertEqual([x[0] for x in calls],['request-plan-recovery-info','request-plan-recovery-info','request-plan-recover-timeout']);ui.handle('确认恢复方案');self.assertEqual(len(calls),3)
        path=Path(calls[-1][1]['approval_evidence']);approval=json.loads(path.read_text());self.assertEqual(approval['approval_quote'],'确认恢复方案');self.assertEqual(approval['approval_channel'],'TRUSTED_LOCAL_CLI')

    def test_complete_bad_replacement_reply_never_repairs_or_sends_again(self):
        transport=self.h.transport
        def bad(ctx,payload):
            response=transport(ctx,payload);response['choices'][0]['message']['tool_calls'][0]['function']['arguments']='{"broken":';return response
        self.h.transport=bad
        with self.assertRaises(ValueError):self.recover()
        self.assertEqual(len(self.sent),2);self.assertEqual([r['status'] for r in self.rows()],['unknown','rejected'])
        with self.assertRaises(PermissionError):asyncio.run(self.flow.plan())
        self.assertEqual(len(self.sent),2)
        with connection(self.ledger) as db:self.assertEqual(db.execute("SELECT count(*) FROM counters WHERE kind='software_repair'").fetchone()[0],0)

    def test_latest_saved_ask_reply_recovers_instead_of_old_plan_and_keeps_semantic_guard(self):
        self.recover();self.flow.refresh();rt=self.h.runtime(self.flow.ctx)
        stage=self.root/'turns/ask_0';rid='case_ask_0_'+self.rid
        context=dict(binding=self.flow.state(),raw_request=self.ctx['raw_request'],baseline_snapshot=self.ctx['baseline_snapshot'])
        messages=case_taskflow.project_messages(self.flow.ctx,[dict(role='system',content='TEST_ONLY readonly'),dict(role='user',content=json.dumps(context,ensure_ascii=False)),dict(role='user',content='TEST_ONLY original question')])
        saved=dict(request_id=rid,messages=messages,tools=[],request_digest=self.ctx['request_digest']);write(stage/'request.json',saved)
        payload,estimate=rt.build_payload(messages,[]);rt.budget.reserve_http(rid,sha_value(payload),'consultation',rt.config);rt.budget.launch_http(rid,fixture=True);rt.budget.finish_http(rid,error='TEST_ONLY interrupted parent',uncertain=True)
        folder=self.root/'http'/sha_value(rid)
        from week13.src.case_strict_tools import transport_profile
        write(folder/'request.json',dict(request_id=rid,payload=payload,payload_sha256=sha_value(payload),authorization_sha256=self.ctx['runtime_authority']['sha256'],internal_tools=[],transport_profile=transport_profile(rt.config)))
        write(folder/'protocol-response.json',dict(id='TEST_ONLY_saved_ask',model='deepseek-flash',choices=[dict(message=dict(content='任务UNKNOWN，尚未执行。',reasoning_content='PRIVATE_REASONING_MUST_NOT_DISPLAY'),finish_reason='stop')],usage=dict(prompt_tokens=20,completion_tokens=10,total_tokens=30)))
        before=len(self.sent)
        with patch.object(self.h,'transport',side_effect=AssertionError('zero HTTP required')):result=asyncio.run(self.flow.recover_response())
        self.assertEqual(result['product_http_added'],0);self.assertEqual(result['recovered_stage'],'ask_0');self.assertEqual(len(self.sent),before);self.assertNotIn('PRIVATE_REASONING',json.dumps(result));self.assertTrue((stage/'response.json').is_file());self.assertIn('explanation_validation',result)
        history=json.loads((self.root/'dialogue.json').read_text());self.assertEqual(len(history),1);self.assertEqual(history[0]['question'],'TEST_ONLY original question');self.assertEqual(history[0]['recovered_http_request_id'],rid)

    def test_recovered_succeeded_stage_without_dialogue_is_idempotent_and_next_question_advances(self):
        # First materialize the interrupted ask with runtime success and the
        # stage response already saved, exactly before ordinary dialogue append.
        self.recover();self.flow.refresh();rt=self.h.runtime(self.flow.ctx)
        stage=self.root/'turns/ask_0';rid='case_ask_0_'+self.rid
        context=dict(binding=self.flow.state(),raw_request=self.ctx['raw_request'],baseline_snapshot=self.ctx['baseline_snapshot'])
        messages=case_taskflow.project_messages(self.flow.ctx,[dict(role='system',content='TEST_ONLY readonly'),dict(role='user',content=json.dumps(context,ensure_ascii=False)),dict(role='user',content='TEST_ONLY recovered original question')])
        saved=dict(request_id=rid,messages=messages,tools=[],request_digest=self.ctx['request_digest']);write(stage/'request.json',saved)
        response=rt.complete(messages,[],rid);write(stage/'response.json',response)
        # Fixture transports normally do not materialize HTTP protocol files.
        folder=self.root/'http'/sha_value(rid)
        write(folder/'protocol-response.json',dict(id=response['response_id'],model=response['response_model'],choices=[dict(message=response['assistant_message'],finish_reason=response['finish_reason'])],usage=response['usage']))
        before=len(self.sent)
        first=asyncio.run(self.flow.recover_response());self.assertEqual(first['recovered_stage'],'ask_0');self.assertEqual(first['product_http_added'],0)
        path=self.root/'dialogue.json';one=path.read_bytes();again=asyncio.run(self.flow.recover_response());self.assertTrue(again['reused']);self.assertEqual(path.read_bytes(),one);self.assertEqual(len(self.sent),before)
        asyncio.run(self.flow.ask('TEST_ONLY next ordinary question'))
        self.assertEqual(len(self.sent),before+1);self.assertTrue((self.root/'turns/ask_1/request.json').is_file());self.assertEqual(len(json.loads(path.read_text())),2)
        next_response=json.loads((self.root/'turns/ask_1/response.json').read_text())
        write(self.root/'http'/sha_value('case_ask_1_'+self.rid)/'protocol-response.json',dict(id=next_response['response_id'],model=next_response['response_model'],choices=[dict(message=next_response['assistant_message'],finish_reason=next_response['finish_reason'])],usage=next_response['usage']))
        # An existing conflicting dialogue entry cannot be silently replaced.
        latest=json.loads(path.read_text());latest[1]['question']='TEST_ONLY forged question';write(path,latest)
        with self.assertRaisesRegex(ValueError,'dialogue turn differs'):asyncio.run(self.flow.recover_response())
        self.assertEqual(len(self.sent),before+1)

    def test_same_request_following_generation_uses_original_authority_and_unknown_retained(self):
        planned=self.recover();self.flow.confirm(planned['displayed_digest'],'CONFIRM')
        generated=asyncio.run(self.flow.generate());self.assertIn('proposal_id',generated);self.assertEqual(self.rows()[0],self.old_row)
        self.assertTrue((self.root/'generation.json').is_file());self.assertEqual(self.h.runtime(case_requests.load_request(self.rid)).config['read_timeout_s'],180)

    def test_post_success_other_unknown_and_approval_or_source_binding_drift_block(self):
        self.recover()
        with connection(self.ledger) as db:
            cols=','.join(self.old_row);q=','.join('?' for _ in self.old_row);other=dict(self.old_row,request_id='TEST_ONLY_other_unknown');db.execute('INSERT INTO requests('+cols+') VALUES('+q+')',tuple(other.values()))
        self.assertIsNotNone(case_console_data.ledger_summary(self.authority)['blocked'])
        with connection(self.ledger) as db:db.execute('DELETE FROM requests WHERE request_id=?',('TEST_ONLY_other_unknown',))
        write(self.approval_path,dict(self.approval,payload_sha256='drift'))
        self.assertIsNotNone(case_console_data.ledger_summary(self.authority)['blocked'])

class WaitingBoundaryTests(unittest.TestCase):
    def test_virtual_delayed_response_after_180_before_600_and_heartbeats_do_not_renew_progress(self):
        # Delays are virtual; no actual 240/600-second sleep or network.
        elapsed=[0.0]
        def delayed(read_timeout):
            elapsed[0]=0;stream=StreamAssembler(clock=lambda:elapsed[0],no_progress_s=600);elapsed[0]=240
            if elapsed[0]>read_timeout:raise httpx.ReadTimeout('TEST_ONLY virtual next-block wait')
            chunk=dict(id='TEST_ONLY',model='deepseek-flash',object='chat.completion.chunk',created=1,choices=[dict(index=0,delta=dict(content='done'),finish_reason='stop')],usage=dict(prompt_tokens=1,completion_tokens=1,total_tokens=2))
            stream.consume(json.dumps(chunk));stream.consume('[DONE]');return stream.finalize()
        with self.assertRaises(httpx.ReadTimeout):delayed(180)
        self.assertEqual(delayed(600)['choices'][0]['message']['content'],'done')
        elapsed[0]=0;stream=StreamAssembler(clock=lambda:elapsed[0],no_progress_s=600)
        for seconds in (180,360,599):elapsed[0]=seconds;stream.feed_line(': keepalive')
        elapsed[0]=600
        with self.assertRaisesRegex(StreamProtocolError,'NO_SUBSTANTIVE'):stream.feed_line(': keepalive')

if __name__=='__main__':unittest.main()
