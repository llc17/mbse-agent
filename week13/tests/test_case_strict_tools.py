"""Local wire/schema tests only: no real account, ledger, MCP, OMC or solver."""
from copy import deepcopy
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from week13.src import case_taskflow
from week13.src.case_http import effective_transport
from week13.src.case_paths import ROOT, read_json, save_json, sha_value
from week13.src.case_runtime import CaseRuntime
from week13.src.case_stream import StreamAssembler
from week13.src.case_strict_tools import (STRICT_PROFILE, LEGACY_PROFILE, STRICT_BASE, LEGACY_BASE,
    transport_profile, prepare_tools, generation_tool, strict_json, validate_value, validate_calls)

EVIDENCE = ROOT / 'results/strict-output-contact-followup-20261022-01'


def reply(arguments=None, *, name='case_request_plan', finish=None):
    message = dict(role='assistant', content='说明', reasoning_content='保留完整思考协议')
    if arguments is not None:
        message['tool_calls'] = [dict(id='fixture_call', type='function', function=dict(name=name, arguments=arguments))]
    return dict(id='strict_local_fixture', model='deepseek-flash', choices=[dict(message=message,
        finish_reason=finish or ('tool_calls' if arguments is not None else 'stop'))],
        usage=dict(prompt_tokens=20, completion_tokens=10, total_tokens=30))


def plan():
    return dict(packet=dict(intent='CHANGE', support='NEEDS_INPUT', missing=['需要质量事实'], unsupported=[],
        requirements=[], design={}, proposed_parameters={}, defaults=[], unknowns=[], rationale='缺少关键事实', scope={}))


def candidate():
    contract = dict(outputs={'trajectory_x': 'x'})
    design = dict(summary='完整合成fixture', assumptions=[], parameters=[])
    source = 'model Fixture\n  // 引号 " / literal \\n\n' + '  Real x;\n' * 5000 + 'equation x=0;\nend Fixture;'
    spec = dict(schema_version='v11-model-artifact-1', backend='model-artifact', baseline_ref={key: key + '_fixture'
        for key in ('project_id', 'task_id', 'version_id', 'spec_digest', 'source_sha256', 'manifest_sha256')},
        artifact=dict(model_name='Fixture', modelica_source=source, sysml_source='package Fixture {\n' + '  // 字符 \\n "\n' * 5000 + '}'),
        sources=[dict(id='fixture_source')], requirements=[dict(text='合成要求', source_id='fixture_source', source_text='合成原文')],
        design=design, libraries=[], settings=dict(stop_time=120, intervals=2400), outputs=contract['outputs'], checks=[])
    return contract, spec


class MemoryBudget:
    def __init__(self): self.rows = {}
    def request(self, rid): return self.rows.get(rid)
    def reserve_http(self, rid, digest, category, config):
        self.rows[rid] = dict(content_hash=digest, status='reserved', response=None, launched_at=None)
    def launch_http(self, rid, **kwargs): self.rows[rid].update(status='dispatched', launched_at=datetime.now(timezone.utc).isoformat())
    def finish_http(self, rid, **kwargs):
        self.rows[rid].update(status='unknown' if kwargs.get('uncertain') else 'rejected' if kwargs.get('error') else 'succeeded',
                              response=json.dumps(kwargs.get('response')), **{k:v for k,v in kwargs.items() if k != 'response'})
    def recover_http(self, rid, digest, result, tokens, output, cost, proof):
        self.rows[rid].update(status='succeeded', response=json.dumps(result), recovery_proof=proof)


class StrictToolsTests(unittest.TestCase):
    def setUp(self):
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='a-local-', dir=EVIDENCE)
        self.folder = Path(self.temp.name)
        self.config = read_json(ROOT / 'configs/case_runtime.json')
        self.sent = []
    def tearDown(self):
        self.assertTrue(self.folder.resolve().is_relative_to(EVIDENCE.resolve()))
        self.temp.cleanup()
    def runtime(self, config=None):
        rt = object.__new__(CaseRuntime)
        rt.config = deepcopy(config or self.config)
        rt.task_context = None
        rt.run = self.folder
        rt.ledger_path = self.folder / 'memory-only-never-created.sqlite'
        now = datetime.now(timezone.utc)
        rt.authorization = dict(started_at_utc=(now-timedelta(hours=1)).isoformat(), hard_deadline_utc=(now+timedelta(hours=1)).isoformat())
        rt.budget = MemoryBudget()
        rt._require_time = lambda: None
        rt._remaining = lambda: 1000
        rt._authorization_sha = lambda: 'fixture_authority'
        def transport(payload, *unused):
            self.sent.append(deepcopy(payload))
            return self.response
        rt.transport = transport
        self.response = reply(json.dumps(plan(), ensure_ascii=False))
        return rt
    def test_plan_wire_is_closed_and_internal_schema_is_not_mutated(self):
        original = deepcopy(case_taskflow.PLAN_TOOL)
        tools, audit = prepare_tools([original])
        self.assertEqual(original, case_taskflow.PLAN_TOOL)
        self.assertTrue(tools[0]['function']['strict'])
        self.assertIn('oneOf', original['function']['parameters']['properties']['packet']['properties']['design'])
        self.assertNotIn('oneOf', json.dumps(tools))
        self.assertNotIn('minLength', json.dumps(tools))
        self.assertTrue(audit[0]['local_only_constraints'])
        validate_calls([dict(function=dict(name='case_request_plan', arguments=json.dumps(plan())))], [original])
    def test_unsupported_or_ambiguous_schema_is_rejected_locally(self):
        for schema in (dict(type='object'), dict(allOf=[dict(type='string')]),
                       dict(oneOf=[dict(type='string'), dict(type='string')]),
                       dict(type='object', properties=dict(optional=dict(type='string')), required=[], additionalProperties=False)):
            tool = dict(type='function', function=dict(name='fixture', parameters=schema))
            with self.subTest(schema=schema), self.assertRaises(ValueError): prepare_tools([tool])
    def test_generation_known_optional_fields_and_frozen_outputs(self):
        contract, spec = candidate()
        for needs in (False, True):
            for trajectory in (False, True):
                local = deepcopy(contract)
                if trajectory: local['trajectory_contract'] = {'samples': [], 'kind': 'frozen'}
                tool = generation_tool(case_taskflow.GEN_TOOL, local, needs_baseline=needs)
                prepared, _ = prepare_tools([tool])
                arguments = prepared[0]['function']['parameters']
                self.assertEqual('baseline_sysml_source' in arguments['properties'], needs)
                fields = arguments['properties']['spec']
                self.assertEqual('trajectory_contract' in fields['properties'], trajectory)
                self.assertEqual(set(fields['properties']['outputs']['properties']), {'trajectory_x'})
                self.assertFalse(fields['properties']['outputs']['additionalProperties'])
        tool = generation_tool(case_taskflow.GEN_TOOL, contract, needs_baseline=False)
        validate_value(dict(spec=spec), tool['function']['parameters'])
        spec['outputs']['extra'] = 'invented'
        with self.assertRaises(ValueError): validate_value(dict(spec=spec), tool['function']['parameters'])
    def test_effective_payload_endpoint_history_estimate_and_no_tool_explanation(self):
        rt = self.runtime()
        messages = [dict(role='user', content='需求'), dict(role='assistant', content='历史', reasoning_content='完整思考')]
        payload, estimate = rt.build_payload(messages, [case_taskflow.PLAN_TOOL])
        self.assertEqual(payload['messages'], messages)
        self.assertEqual(payload['thinking'], {'type':'enabled'})
        self.assertEqual(payload['max_tokens'], 393216)
        self.assertEqual(payload['reasoning_effort'], 'high')
        self.assertEqual(payload['tool_choice'], 'auto')
        from week11.src.runtime import _estimate_input
        self.assertEqual(estimate, _estimate_input(messages, payload['tools']))
        profile = transport_profile(rt.config)
        self.assertEqual(effective_transport(dict(payload=payload, transport_profile=profile), rt.config)['endpoint'], STRICT_BASE+'/chat/completions')
        with self.assertRaises(ValueError): effective_transport(dict(payload=payload, transport_profile=dict(profile, api_base=LEGACY_BASE)), rt.config)
        with self.assertRaises(ValueError): effective_transport(dict(payload=payload), rt.config)
        plain = rt.build_payload(messages, [])[0]
        self.assertNotIn('tools', plain); self.assertNotIn('tool_choice', plain)
    def test_saved_actual_second_bad_plan_is_rejected_without_changing_response(self):
        location = ROOT/'runs/case-projects/case_3b0265f496f84ae9b800/requests/contact_numeric85_180_260_20261022_01/http/7d7f8a1c7becc0685bcedd4458d650730e671a00c5b51a63e662bc7528d09ca9/protocol-response.json'
        saved = location.read_bytes()
        raw = json.loads(saved)
        rt = self.runtime(); self.response = raw
        with self.assertRaises(json.JSONDecodeError): rt.complete([dict(role='user', content='fixture')], [case_taskflow.PLAN_TOOL], 'fixture_bad')
        self.assertEqual(rt.budget.request('fixture_bad')['status'], 'rejected')
        self.assertEqual(rt.budget.request('fixture_bad')['tokens'], raw['usage']['total_tokens'])
        with self.assertRaises(RuntimeError): rt.complete([dict(role='user', content='fixture')], [case_taskflow.PLAN_TOOL], 'fixture_bad')
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(location.read_bytes(), saved)
    def test_local_business_limits_duplicates_wrong_depth_and_unoffered_calls_rejected(self):
        for value in (dict(packet=dict(plan()['packet'], rationale='')), {'packet': {'design': plan()['packet']}}, {'packet': plan()['packet'], 'extra': True}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_value(value, case_taskflow.PLAN_TOOL['function']['parameters'])
        for text in ('{"packet":{},"packet":{}}', '{"value":NaN}', '{"value":1e999}', '{"packet":{}} trailing'):
            with self.subTest(text=text), self.assertRaises(ValueError): strict_json(text)
        rt=self.runtime(); self.response=reply(json.dumps(plan()), name='unoffered')
        with self.assertRaisesRegex(ValueError,'unoffered'): rt.complete([dict(role='user',content='fixture')], [case_taskflow.PLAN_TOOL], 'fixture_unoffered')
        self.assertEqual(rt.budget.request('fixture_unoffered')['status'],'rejected')
    def test_complete_long_double_source_survives_stream_fragments_exactly(self):
        contract, spec = candidate(); text = json.dumps(dict(spec=spec), ensure_ascii=False)
        stream = StreamAssembler()
        def emit(delta, finish=None, usage=None):
            chunk=dict(id='strict_long',model='deepseek-flash',object='chat.completion.chunk',created=1,choices=[dict(index=0,delta=delta,finish_reason=finish)])
            if usage is not None: chunk['usage']=usage
            stream.consume(json.dumps(chunk,ensure_ascii=False))
        emit(dict(reasoning_content='思考',tool_calls=[dict(index=0,id='long_call',type='function',function=dict(name='proposal_prepare',arguments=''))]))
        for offset in range(0,len(text),337): emit(dict(tool_calls=[dict(index=0,function=dict(arguments=text[offset:offset+337]))]))
        emit({},'tool_calls',dict(prompt_tokens=20,completion_tokens=30000,total_tokens=30020));stream.consume('[DONE]')
        result=stream.finalize(); rt=self.runtime(); self.response=result
        tool=generation_tool(case_taskflow.GEN_TOOL,contract,needs_baseline=False)
        parsed=rt.complete([dict(role='user',content='fixture')],[tool],'fixture_long')
        args,audit=case_taskflow.generation_arguments(parsed,needs_baseline=False)
        self.assertEqual(args['spec'],spec);self.assertIsNone(audit)
        self.assertEqual(parsed['request_tool_output_profile'],STRICT_PROFILE)
        invalid=dict(parsed,tool_calls=[dict(id='bad',type='function',function=dict(name='proposal_prepare',arguments=json.dumps(dict(spec=json.dumps(spec)))) )])
        with self.assertRaisesRegex(ValueError,'encoded JSON recovery forbidden'):case_taskflow.generation_arguments(invalid,needs_baseline=False)
    def test_incomplete_finish_keeps_known_usage_and_never_succeeds(self):
        rt=self.runtime();self.response=reply(json.dumps(plan()),finish='length')
        with self.assertRaisesRegex(ValueError,'incomplete'):rt.complete([dict(role='user',content='fixture')],[case_taskflow.PLAN_TOOL],'fixture_length')
        self.assertEqual(rt.budget.request('fixture_length')['status'],'rejected')
    def test_legacy_payload_recovery_and_profile_mismatch_are_not_migrated(self):
        config=deepcopy(self.config);config['api_base']=LEGACY_BASE;config.pop('tool_output_profile')
        rt=self.runtime(config);messages=[dict(role='user',content='legacy')]
        tools=[dict(type='function',function=dict(name='legacy',parameters=dict(type='object')))]
        payload,_=rt.build_payload(messages,tools);self.assertEqual(payload['tools'],tools)
        rid='legacy_saved';folder=self.folder/'fixture-http'/sha_value(rid)
        digest=sha_value(payload);rt.budget.rows[rid]=dict(content_hash=digest,status='unknown',launched_at=None)
        saved=dict(request_id=rid,payload=payload,payload_sha256=digest,authorization_sha256='fixture_authority')
        save_json(folder/'request.json',saved);save_json(folder/'protocol-response.json',reply())
        original=(folder/'request.json').read_bytes();result=rt.recover_saved_response(rid)
        self.assertEqual(result['content'],'说明');self.assertEqual(self.sent,[])
        self.assertEqual((folder/'request.json').read_bytes(),original)
        strict_rt=self.runtime()
        with self.assertRaisesRegex(ValueError,'cannot migrate'):strict_rt._validate_saved_profile(saved)
        with self.assertRaisesRegex(ValueError,'mismatch'):rt._validate_saved_profile(dict(saved,transport_profile=transport_profile(self.config)))
    def test_strict_saved_recovery_uses_original_internal_constraints(self):
        rt=self.runtime();rid='strict_saved';tools=[case_taskflow.PLAN_TOOL]
        payload,_=rt.build_payload([dict(role='user',content='strict')],tools);digest=sha_value(payload)
        folder=self.folder/'fixture-http'/sha_value(rid)
        rt.budget.rows[rid]=dict(content_hash=digest,status='unknown',launched_at=None)
        save_json(folder/'request.json',dict(request_id=rid,payload=payload,payload_sha256=digest,
            authorization_sha256='fixture_authority',transport_profile=transport_profile(self.config),internal_tools=tools))
        save_json(folder/'protocol-response.json',reply(json.dumps(dict(packet=dict(plan()['packet'],rationale='')))))
        with self.assertRaisesRegex(ValueError,'size constraint'):rt.recover_saved_response(rid)
        self.assertEqual(rt.budget.request(rid)['status'],'unknown');self.assertEqual(self.sent,[])
    def test_actual_saved_generation_contract_has_supported_closed_wire_shape(self):
        path=ROOT/'runs/case-projects/case_3b0265f496f84ae9b800/requests/contact_numeric85_150_280_20261022_01/turns/generate_repair_1/request.json'
        original=path.read_bytes();request=json.loads(original)
        context=json.loads(request['messages'][1]['content'])
        contract=context['trusted_generation_contract']
        needs=context['original_baseline_completion']['required']
        tool=generation_tool(case_taskflow.GEN_TOOL,contract,needs_baseline=needs)
        prepared,audit=prepare_tools([tool])
        outputs=prepared[0]['function']['parameters']['properties']['spec']['properties']['outputs']
        self.assertEqual(set(outputs['properties']),set(contract['outputs']))
        self.assertTrue(prepared[0]['function']['strict']);self.assertEqual(path.read_bytes(),original)
    def test_ask_and_independent_review_reopen_receive_canonical_stage_facts(self):
        facts=dict(schema='v12-request-stage-facts-1',request_id='fixture',current=dict(request_id='fixture',plan='FAIL',
                   generate='NOT_RUN',compile='NOT_RUN',simulation='NOT_RUN',summary='方案JSON解析失败'),approved_requests=[])
        captured=[]
        async def call(stage,messages,tools):
            captured.append((stage,deepcopy(messages),tools))
            return dict(content='TEST_ONLY 方案解析失败，后续未运行',assistant_message=dict(role='assistant',content='TEST_ONLY',reasoning_content='fixture'))
        flow=object.__new__(case_taskflow.TaskFlow)
        flow.root=self.folder/'ordinary';flow.root.mkdir()
        flow.ctx=dict(request_id='fixture',root=str(flow.root),raw_request='解释当前状态',
                      baseline_snapshot=dict(frozen_spec=dict(artifact=dict(sysml_source='package Fixture {}'))))
        flow.state=lambda:dict(latest_request_execution=None,last_failure=dict(stage='plan',error='JSONDecodeError'))
        flow.call=call
        with patch('week13.src.case_stage_facts.read_stage_facts',return_value=facts):
            for reopen in (False,True): asyncio.run(flow.ask('按实际阶段解释',reopen=reopen,explain=not reopen))
            for reopen in (False,True):
                flow.root=self.folder/('review-reopen' if reopen else 'review-explain');flow.root.mkdir()
                flow.ctx=dict(request_id='fixture',root=str(flow.root),access_mode='RESULT_REVIEW_ONLY',
                              step_id='reopen' if reopen else 'explain',review_binding=dict(origin_request_id='origin'))
                flow.refresh=lambda:None
                flow.review_evidence=lambda:dict(task_status='FAIL')
                flow.state=lambda:dict(reviewed_execution=dict(current_contact_review=dict(status='UNKNOWN')))
                save_json(flow.root/'turns/explain_0/response.json',dict(content='TEST_ONLY 旧解释'))
                with patch('week13.src.case_requests.authority_for',return_value=dict(operation_policy=dict(steps=dict(explain='explain_fixture')))), \
                     patch('week13.src.case_requests.load_request',return_value=dict(request_id='explain_fixture',root=str(flow.root))):
                    asyncio.run(flow.ask_review('按实际阶段解释',reopen=reopen))
        self.assertEqual(len(captured),4)
        for stage,messages,tools in captured:
            self.assertEqual(tools,[])
            self.assertIn('stage_facts优先',messages[0]['content'])
            self.assertEqual(json.loads(messages[1]['content'])['stage_facts'],facts)
            self.assertNotIn('min(T/R,mu_s*Fz)',messages[0]['content'])
        self.assertIn('当前绑定源码及actual_request_execution事实',captured[0][1][0]['content'])


if __name__ == '__main__': unittest.main()
