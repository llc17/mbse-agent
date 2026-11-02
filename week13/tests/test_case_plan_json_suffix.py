"""Only complete rejected planning replies may lose one redundant outer brace."""
import asyncio,json,sqlite3,tempfile,unittest
from contextlib import closing
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import AsyncMock,patch

from week13.src import case_taskflow as flowmod,case_requests,case_paths,case_cli
from week13.tests.test_case_agent_requests import Harness,write

EVIDENCE=case_paths.ROOT/'results/plan-json-suffix-20261021-01'


def reply(arguments,name='case_request_plan',finish='tool_calls'):
    return dict(content='',finish_reason=finish,tool_calls=[dict(id='TEST_ONLY_call',type='function',function=dict(name=name,arguments=arguments))])


class PlanSuffixSyntax(unittest.TestCase):
    def test_single_complete_packet_then_one_brace_only(self):
        packet=dict(text='literal } in text and escaped "quote"',parameters=dict(m=105))
        raw=json.dumps(dict(packet=packet))+'}'
        for text in (raw,' \t'+raw[:-1]+' \n}\r\n'):
            original=reply(text);before=deepcopy(original)
            parsed,audit=flowmod.plan_suffix_arguments(original)
            self.assertEqual(parsed,packet);self.assertEqual(original,before)
            self.assertFalse(audit['business_values_changed']);self.assertEqual(audit['packet_digest'],case_paths.sha_value(packet))
    def test_missing_duplicate_nonfinite_multiple_encoding_second_object_or_text_refused(self):
        invalid=['{"packet":{', '{"packet":{}}', '{"packet":{}}}}', '{"packet":{}} {}', '{"packet":{}}garbage', '{"packet":{"x":1,"x":2}}}', '{"packet":{},"packet":{}}}', '{"packet":{"x":NaN}}}', '{"packet":{"x":Infinity}}}', '{"packet":{"x":1e309}}}', '{"packet":"{}"}}', '{"packet":[]}}', '{"packet":{},"other":{}}}', '[]}', '"text"}', '{"spec":{}}}']
        for raw in invalid:
            with self.subTest(raw=raw):
                with self.assertRaises((ValueError,TypeError)):flowmod.plan_suffix_arguments(reply(raw))
        with self.assertRaises(ValueError):flowmod.plan_suffix_arguments(reply('{"packet":{}}}',name='proposal_prepare'))
        with self.assertRaises(ValueError):flowmod.plan_suffix_arguments(reply('{"packet":{}}}',finish='length'))
        multi=reply('{"packet":{}}}');multi['tool_calls']*=2
        with self.assertRaises(ValueError):flowmod.plan_suffix_arguments(multi)


class PlanSuffixFlow(unittest.TestCase):
    def setUp(self):
        EVIDENCE.mkdir(parents=True,exist_ok=True)
        self.saved=(case_requests.ROOT,case_requests.REQUESTS_ROOT,case_requests.LEGACY_PROJECT_ROOT)
        self.temp=tempfile.TemporaryDirectory(prefix='plan-suffix-',dir=EVIDENCE)
        self.h=Harness(Path(self.temp.name)/'fixture');self.rid='TEST_ONLY_plan_suffix'
        self.h.bootstrap(self.rid,self.h.project_id,'v000002','把总质量改为92kg，增加部分在原重心，几何控制不变。');self.h.authorize(self.rid)
        self.flow=self.h.flow(self.rid);self.root=Path(self.flow.ctx['root']);self.original=self.h.transport
        self.mode='extra';self.captured=[];self.h.transport=self.transport
    def tearDown(self):
        case_requests.ROOT,case_requests.REQUESTS_ROOT,case_requests.LEGACY_PROJECT_ROOT=self.saved
        self.temp.cleanup()
    def transport(self,ctx,payload):
        self.captured.append(deepcopy(payload));result=self.original(ctx,payload)
        context=json.loads(payload['messages'][1]['content']);tool=(payload.get('tools') or [{}])[0].get('function',{}).get('name')
        if tool!='case_request_plan':return result
        attempt=1 if 'bounded_protocol_repair' in context else 0
        function=result['choices'][0]['message']['tool_calls'][0]['function'];args=json.loads(function['arguments'])
        if self.mode=='prior_parameters' and attempt==0:args['packet']['defaults']={}
        if self.mode=='prior_parameters' and attempt:
            args['packet']['proposed_parameters']['m']=94
            next(p for p in args['packet']['design']['parameters'] if p['name']=='m')['value']=94
        raw=json.dumps(args,ensure_ascii=False)
        if self.mode=='missing_then_extra' and not attempt:raw=raw[:-1]
        elif not (self.mode=='prior_parameters' and not attempt):raw+='}'
        function['arguments']=raw
        stage='plan_repair_1' if attempt else 'plan';rid='case_'+stage+'_'+ctx['request_id'];directory=Path(ctx['root'])/'http'/case_paths.sha_value(rid)
        write(directory/'protocol-response.json',result);write(directory/'http-status.json',dict(http_status=200,fixture_transport=True))
        write(directory/'process-exit.json',dict(request_id=rid,returncode=0,fixture_transport=True,fixture_call_returned=True,process_launched=False,local_process_terminated=False,timeout_classification=None,child_error_type=None))
        return result
    def known_failure(self,repair=False):
        self.mode='missing_then_extra' if repair else self.mode
        with patch.object(self.flow,'_revalidate_plan_suffix',side_effect=ValueError('TEST_ONLY preserve charged failure')):
            with self.assertRaises((ValueError,TypeError)):asyncio.run(self.flow.plan())
            if repair:
                with self.assertRaises((ValueError,TypeError)):asyncio.run(self.flow.repair('plan',1))
    def hashes(self):return {str(p.relative_to(self.root)):case_paths.sha_file(p) for p in self.root.rglob('*') if p.is_file()}
    def ledger_bytes(self):return Path(self.flow.ctx['runtime_authority']['ledger_path']).read_bytes()
    def test_automatic_single_suffix_uses_one_http_no_repair_and_preserves_rejected(self):
        result=asyncio.run(self.flow.plan());self.assertTrue(result['requires_local_confirmation'])
        self.assertIsNone(self.flow.ctx['confirmation_path']);self.assertFalse(self.flow.ctx['allow_execution'])
        with closing(sqlite3.connect(self.flow.ctx['runtime_authority']['ledger_path'])) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM requests').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT status FROM requests').fetchone()[0],'rejected')
            self.assertEqual(db.execute('SELECT count(*) FROM counters').fetchone()[0],0)
        self.assertTrue((self.root/'failures/plan-0.json').exists());self.assertFalse((self.root/'turns/plan/response.json').exists())
        self.assertEqual(result['validation']['packet_digest'],case_paths.sha_value(result['plan']))
        context=json.loads(self.captured[0]['messages'][1]['content']);self.assertEqual(context['verified_source_facts']['generation_source_rule'],'CANDIDATE_ONLY')
    def test_offline_attempt1_preserves_originals_store_ledger_and_requires_review(self):
        self.known_failure(repair=True);before=self.hashes();ledger=self.ledger_bytes();store=Path(self.flow.ctx['db_path']).read_bytes()
        failure=case_paths.read_json(self.root/'failures/plan-1.json');packet,_=flowmod.plan_suffix_arguments(failure['response'])
        with patch.object(self.flow,'_runtime',side_effect=AssertionError('no product')),patch.object(self.flow,'mcp',side_effect=AssertionError('no MCP')):
            result=self.flow.revalidate_plan(1,expected_packet_digest=case_paths.sha_value(packet))
        self.assertEqual(result['plan'],packet);self.assertTrue(result['requires_local_confirmation']);self.assertEqual(result['original_http_status'],'rejected')
        self.assertEqual(self.ledger_bytes(),ledger);self.assertEqual(Path(self.flow.ctx['db_path']).read_bytes(),store)
        for name,digest in before.items():
            if name!='task-context.json':self.assertEqual(case_paths.sha_file(self.root/name),digest,name)
        self.assertFalse((self.root/'generation.json').exists());self.assertIsNone(self.flow.ctx['confirmation_path'])
        self.assertEqual(self.flow.revalidate_plan(1),result)
    def test_expected_digest_and_unknown_rejected_before_any_write(self):
        self.known_failure();before=self.hashes()
        with self.assertRaisesRegex(ValueError,'expected packet'):self.flow.revalidate_plan(expected_packet_digest='0'*64)
        self.assertEqual(self.hashes(),before)
        with closing(sqlite3.connect(self.flow.ctx['runtime_authority']['ledger_path'])) as db:
            with db:db.execute("UPDATE requests SET status='unknown'")
        with self.assertRaisesRegex(ValueError,'no UNKNOWN'):self.flow.revalidate_plan()
        self.assertFalse((self.root/'plans').exists())
    def test_missing_first_reply_cannot_be_revalidated(self):
        self.mode='missing_then_extra';self.known_failure()
        with self.assertRaises(json.JSONDecodeError):self.flow.revalidate_plan()
        self.assertFalse((self.root/'plans').exists())
    def test_payload_protocol_source_and_local_exit_drift_rejected(self):
        self.known_failure();rid='case_plan_'+self.rid;folder=self.root/'http'/case_paths.sha_value(rid)
        changes=[(folder/'request.json',lambda v:v.update(payload_sha256='0'*64)),(folder/'protocol-response.json',lambda v:v.update(id='drift')),(folder/'process-exit.json',lambda v:v.update(fixture_call_returned=False))]
        for path,change in changes:
            with self.subTest(path=path.name):
                original=path.read_bytes();value=json.loads(original);change(value);write(path,value)
                with self.assertRaises(ValueError):self.flow.revalidate_plan()
                path.write_bytes(original)
        self.assertFalse((self.root/'plans').exists())
    def test_expired_or_confirmed_context_rejected(self):
        self.known_failure()
        class Future(datetime):
            @classmethod
            def now(cls,tz=None):return datetime.now(timezone.utc)+timedelta(hours=2)
        with patch.object(flowmod,'datetime',Future),self.assertRaisesRegex(PermissionError,'active original'):self.flow.revalidate_plan()
        path=self.root/'task-context.json';ctx=case_paths.read_json(path);ctx['stage']='CONFIRMED';ctx['confirmation_path']='TEST_ONLY';write(path,ctx)
        with self.assertRaisesRegex(PermissionError,'cannot confirm'):self.flow.revalidate_plan()
    def test_prior_complete_parameter_commitments_still_enforced(self):
        self.mode='prior_parameters';self.known_failure()
        with self.assertRaisesRegex(ValueError,'prior complete parameter'):asyncio.run(self.flow.repair('plan',1))
        self.assertFalse((self.root/'plans').exists())
    def test_warning_facts_bound_to_original_packet_without_rewriting_text(self):
        self.known_failure();failure=case_paths.read_json(self.root/'failures/plan-0.json');packet,_=flowmod.plan_suffix_arguments(failure['response']);original=deepcopy(packet)
        obligations=dict(support=dict(status='SUPPORTED'),missing=[],historical_missing=['原v0缺少SysML'],required_source_completion=[])
        with patch.object(flowmod,'imported_obligations',return_value=obligations):validation=self.flow._validate_plan_packet(packet)
        self.assertEqual(packet,original);self.assertEqual(validation['packet_digest'],case_paths.sha_value(original))
        self.assertTrue(validation['verified_source_facts']['selected_baseline_sysml_present']);self.assertEqual(validation['verified_source_facts']['generation_source_rule'],'CANDIDATE_ONLY');self.assertTrue(validation['review_warnings'])
    def test_generation_existing_sysml_payload_only_requests_candidate(self):
        result=asyncio.run(self.flow.plan());self.flow.confirm(result['displayed_digest'],'CONFIRM');before=deepcopy(self.flow.ctx['plan'])
        with patch.object(self.flow,'_prepare_generation',new=AsyncMock(return_value=dict(test_only=True))):asyncio.run(self.flow.generate())
        payload=self.captured[-1];context=json.loads(payload['messages'][1]['content'])
        self.assertFalse(context['original_baseline_completion']['required']);self.assertTrue(context['verified_source_facts']['selected_baseline_sysml_present'])
        self.assertEqual(payload['tools'][0]['function']['parameters']['required'],['spec']);self.assertNotIn('baseline_sysml_source',payload['tools'][0]['function']['parameters']['properties'])
        self.assertEqual(context['confirmed_plan'],before)
    def test_cli_expected_packet_guard(self):
        self.known_failure();code,result=self.h.cli(['revalidate-request','--request-id',self.rid,'--attempt','0','--expected-packet-digest','0'*64])
        self.assertEqual(code,1);self.assertIn('expected packet digest',result['error']);self.assertFalse((self.root/'plans').exists())


class SelectedBaselineFacts(unittest.TestCase):
    def test_current_sysml_clears_only_historical_missing_without_mutating_case(self):
        from week13.src import case_projects
        EVIDENCE.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='plan-facts-',dir=EVIDENCE) as folder:
            original=dict(case_id='TEST_ONLY_case',support=dict(status='SUPPORTED'),missing=['原基准缺少SysML','其他必要事实仍缺失'])
            marker=Path(folder)/'case.json';write(marker,original);before=marker.read_bytes()
            ctx=dict(project_root=folder,baseline_snapshot=dict(frozen_spec=dict(artifact=dict(modelica_source='TEST_ONLY',sysml_source='package Existing {}'))))
            with patch.object(case_projects,'resolve_case',return_value=original),patch.object(case_projects,'verify_case',side_effect=lambda value:value),patch.object(case_projects,'support_for_source',return_value=dict(status='SUPPORTED')):facts=flowmod.imported_obligations(ctx)
            self.assertTrue(facts['selected_baseline_sysml_present']);self.assertEqual(facts['missing'],['其他必要事实仍缺失']);self.assertEqual(facts['historical_missing'],original['missing']);self.assertEqual(facts['required_source_completion'],[]);self.assertEqual(marker.read_bytes(),before)


if __name__=='__main__':unittest.main()
