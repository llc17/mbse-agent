"""Isolated flat-profile and child-entrypoint fixtures; no real network/key."""
import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
import httpx

from week13.src import case_runtime as runtime,case_http,case_taskflow as tf
from week13.src.case_paths import ROOT,read_json,sha_value,save_json
from week13.src.case_strict_tools import (FLAT_PLAN_PROFILE,STRICT_PROFILE,LEGACY_PROFILE,EMPTY_MARKER,
    transport_profile,prepare_tools,decode_calls,flat_plan_schema)
from week13.src.case_stream import reconstruct_tool_delta_evidence
from week13.tests.test_case_strict_tools import candidate

PREP=ROOT/'results/shallow-plan-protocol-20261022-03'


def internal(support='SUPPORTED'):
    packet=dict(intent='CHANGE',support=support,missing=[],unsupported=[],requirements=[],design={},
        proposed_parameters={},defaults=[],unknowns=[],rationale='fixture',scope={})
    if support=='SUPPORTED':
        params=dict(m=85,I_pitch=20,T_main_max=60,T_nose_max=25,stepH=.18,stepD=.26)
        packet.update(requirements=[dict(text='fixture',source_id='fixture_source',source_text='轮 " / \\n 原文')],
            design=dict(summary='轮 " / \\n',assumptions=[dict(text='fixture',origin='model')],parameters=[dict(name=k,value=v,unit='1',origin='model',basis='fixture') for k,v in params.items()]),
            proposed_parameters=params,scope=deepcopy(tf.STAIR_SCOPE_PACKET))
    else:packet['missing' if support=='NEEDS_INPUT' else 'unsupported']=['fixture']
    return dict(packet=packet)


def flat(args):
    packet=deepcopy(args['packet']);design=packet.pop('design')
    packet.update(design_state='OBJECT' if design else 'EMPTY',design_summary=design.get('summary',''),
                  design_assumptions=design.get('assumptions',[]),design_parameters=design.get('parameters',[]))
    for field in ('proposed_parameters','scope'):
        if packet[field]=={}:packet[field]=deepcopy(EMPTY_MARKER)
    return packet


def call(args,name='case_request_plan'):
    return dict(id='fixture_call',type='function',function=dict(name=name,arguments=json.dumps(args,ensure_ascii=False)))


def reply(args,name='case_request_plan'):
    return dict(id='fixture_response',model='deepseek-flash',choices=[dict(index=0,finish_reason='tool_calls',
        message=dict(role='assistant',content='',reasoning_content='fixture protocol',tool_calls=[call(args,name)]))],
        usage=dict(prompt_tokens=20,completion_tokens=10,total_tokens=30))


class MemoryBudget:
    def __init__(self,authority=None):self.rows={};self.launches=0;self.authority=deepcopy(authority or {})
    def effective_http_transport(self,rid):
        names=('connect_timeout_s','read_timeout_s','no_progress_timeout_s','whole_request_deadline_s')
        return {name:self.authority['model_controls'][name] for name in names},None
    def request(self,rid):return self.rows.get(rid)
    def reserve_http(self,rid,digest,category,config):self.rows[rid]=dict(content_hash=digest,status='reserved',launched_at=None)
    def launch_http(self,rid,**kwargs):self.launches+=1;self.rows[rid].update(status='dispatched',launched_at=datetime.now(timezone.utc).isoformat())
    def finish_http(self,rid,**kw):self.rows[rid].update(status='unknown' if kw.get('uncertain') else 'rejected' if kw.get('error') else 'succeeded',response=json.dumps(kw.get('response')),actual_tokens=kw.get('tokens'),actual_output=kw.get('output'),actual_cost=kw.get('cost'))
    def recover_http(self,rid,digest,result,tokens,output,cost,proof):self.finish_http(rid,response=result,tokens=tokens,output=output,cost=cost)


class FlatPlanProtocol(unittest.TestCase):
    def setUp(self):
        PREP.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='fixture-',dir=PREP);self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
    def rt(self,profile=FLAT_PLAN_PROFILE):
        value=object.__new__(runtime.CaseRuntime);value.config=read_json(ROOT/'configs/case_runtime.json')
        value.config['tool_output_profile']=profile
        value.config['api_base']='https://api.deepseek.com' if profile==LEGACY_PROFILE else 'https://api.deepseek.com/beta'
        value.transport=None;value.task_context=None;value.run=self.root;value.ledger_path=self.root/'fixture.sqlite'
        now=datetime.now(timezone.utc)
        value.authorization=dict(started_at_utc=(now-timedelta(hours=1)).isoformat(),hard_deadline_utc=(now+timedelta(hours=1)).isoformat(),model_controls=value.config)
        value.budget=MemoryBudget(value.authorization);value._require_time=lambda:None;value.require_credentials=lambda:None
        value._remaining=lambda:1000;value._authorization_sha=lambda:'fixture_authority'
        return value
    def test_all_business_states_roundtrip_and_wire_is_closed_and_idempotent(self):
        original=deepcopy(tf.PLAN_TOOL)
        tools,audit=prepare_tools([original],profile=FLAT_PLAN_PROFILE)
        self.assertNotIn('packet',tools[0]['function']['parameters']['properties'])
        self.assertNotIn('design',tools[0]['function']['parameters']['properties'])
        self.assertEqual(prepare_tools(tools,profile=FLAT_PLAN_PROFILE)[0],tools)
        for support in ('SUPPORTED','NEEDS_INPUT','UNSUPPORTED'):
            args=internal(support);wire=[call(flat(args))];before=deepcopy(wire)
            decoded,mapping=decode_calls(wire,[original],profile=FLAT_PLAN_PROFILE)
            self.assertEqual(json.loads(decoded[0]['function']['arguments']),args)
            self.assertEqual(wire,before);self.assertTrue(mapping)
        self.assertEqual(original,tf.PLAN_TOOL)
    def test_bad_json_duplicates_nonfinite_extra_fields_and_false_empty_are_rejected(self):
        for text in ('{"intent":','{"intent":"CHANGE","intent":"CHANGE"}','{"x":NaN}','{"x":1e999}'):
            one=call({});one['function']['arguments']=text
            with self.subTest(text=text),self.assertRaises(ValueError):decode_calls([one],[tf.PLAN_TOOL],profile=FLAT_PLAN_PROFILE)
        cases=[internal(),dict(flat(internal()),extra='bad'),dict(flat(internal('NEEDS_INPUT')),design_summary='not empty'),dict(flat(internal()),design_summary='')]
        for args in cases:
            with self.subTest(keys=list(args)),self.assertRaises(ValueError):decode_calls([call(args)],[tf.PLAN_TOOL],profile=FLAT_PLAN_PROFILE)
    def test_generation_schema_and_full_long_sources_are_unchanged(self):
        from week13.src.case_strict_tools import generation_tool
        contract,spec=candidate();tool=generation_tool(tf.GEN_TOOL,contract,needs_baseline=False)
        self.assertEqual(prepare_tools([tool],profile=FLAT_PLAN_PROFILE)[0],prepare_tools([tool],profile=STRICT_PROFILE)[0])
        decoded,_=decode_calls([call(dict(spec=spec),'proposal_prepare')],[tool],profile=FLAT_PLAN_PROFILE)
        self.assertEqual(json.loads(decoded[0]['function']['arguments'])['spec'],spec)
        view=dict(finish_reason='tool_calls',tool_calls=[call(dict(spec=json.dumps(spec)),'proposal_prepare')],request_tool_output_profile=FLAT_PLAN_PROFILE)
        with self.assertRaisesRegex(ValueError,'recovery forbidden'):tf.generation_arguments(view,needs_baseline=False)
    def test_prompt_schema_payload_and_saved_rebuild_agree(self):
        from week13.src.case_contact_family import PLAN_PROMPT as contact
        from week13.src.case_contact_numerics import PLAN_PROMPT as numerical
        rt=self.rt()
        for prompt in (tf.PLAN_PROMPT,contact,numerical):
            rendered=tf.flat_plan_prompt(prompt)
            self.assertNotIn('design={',rendered);self.assertNotIn('packet恰含',rendered)
            payload,_=rt.build_payload([dict(role='system',content=rendered),dict(role='user',content='fixture')],[tf.PLAN_TOOL])
            self.assertEqual(rt.build_payload(payload['messages'],[tf.PLAN_TOOL])[0],payload)
            self.assertIn('design_state',payload['messages'][0]['content'])
        with self.assertRaisesRegex(ValueError,'nested PLAN prompt'):rt.build_payload([dict(role='system',content=tf.PLAN_PROMPT)],[tf.PLAN_TOOL])
    def test_child_entrypoint_fixture_transmits_once_decodes_original_and_recovers_without_resend(self):
        rt=self.rt();args=internal();wire=flat(args);response=reply(wire)
        emitted=[];requests=[]
        chunk=dict(id=response['id'],model=response['model'],created=1,object='chat.completion.chunk',
            choices=[dict(index=0,delta=dict(role='assistant',tool_calls=[dict(index=0,**response['choices'][0]['message']['tool_calls'][0])]),finish_reason='tool_calls')],usage=response['usage'])
        data=('data: '+json.dumps(chunk,ensure_ascii=False)+'\n\ndata: [DONE]\n\n').encode('utf8')
        def handler(request):
            emitted.append(str(request.url))
            self.assertEqual(json.loads(request.content)['tools'],prepare_tools([tf.PLAN_TOOL],profile=FLAT_PLAN_PROFILE)[0])
            return httpx.Response(200,headers={'content-type':'text/event-stream'},content=data)
        client=httpx.Client(transport=httpx.MockTransport(handler));self.addCleanup(client.close)
        process=Mock(returncode=0)
        def wait(proc,text,directory,**kwargs):
            request=json.loads(text);requests.append(request);out=io.StringIO()
            expected,override=rt.budget.effective_http_transport(request['request_id'])
            self.assertEqual({name:request[name] for name in expected},expected)
            self.assertIsNone(override);self.assertIsNone(request['timeout_override'])
            with patch.object(case_http,'RUN',self.root),patch.object(case_http,'CaseBudget',return_value=rt.budget),patch.object(case_http,'authorization',return_value=rt.authorization),patch.object(case_http,'_api_key',return_value='SYNTHETIC_FIXTURE_KEY'),patch('httpx.Client',return_value=client),patch('sys.stdin',io.StringIO(text)),redirect_stdout(out):
                self.assertEqual(case_http.main(),0)
            return out.getvalue()
        with patch.object(runtime.subprocess,'Popen',return_value=process),patch.object(runtime,'wait_for_http_child',side_effect=wait):
            result=rt.complete([dict(role='system',content=tf.flat_plan_prompt(tf.PLAN_PROMPT))],[tf.PLAN_TOOL],'case_plan_fixture')
        self.assertEqual(emitted,['https://api.deepseek.com/beta/chat/completions']);self.assertEqual(rt.budget.launches,1)
        self.assertEqual(requests[0]['internal_tools'],[tf.PLAN_TOOL])
        self.assertEqual(result['request_tool_output_profile'],FLAT_PLAN_PROFILE)
        self.assertEqual(json.loads(result['tool_calls'][0]['function']['arguments']),args)
        self.assertEqual(json.loads(result['assistant_message']['tool_calls'][0]['function']['arguments']),wire)
        self.assertIn('wire_structure_mapping',result);self.assertNotIn('wire_empty_object_mapping',result)
        directory=self.root/'http'/sha_value('case_plan_fixture');self.assertTrue((directory/'wire-plan-structure-mapping.json').is_file())
        request=read_json(directory/'request.json');protocol=read_json(directory/'protocol-response.json')
        checked=reconstruct_tool_delta_evidence(directory/'tool-call-deltas.jsonl',request_id=request['request_id'],payload_sha256=request['payload_sha256'],protocol_response=protocol)
        self.assertTrue(checked['complete'])
        self.assertEqual(rt.complete([dict(role='system',content=tf.flat_plan_prompt(tf.PLAN_PROMPT))],[tf.PLAN_TOOL],'case_plan_fixture')['response_id'],result['response_id'])
        rt.budget.rows['case_plan_fixture']['status']='unknown'
        recovered=rt.recover_saved_response('case_plan_fixture')
        self.assertEqual(recovered['request_tool_output_profile'],FLAT_PLAN_PROFILE);self.assertEqual(rt.budget.launches,1)
        self.assertEqual(json.loads(recovered['tool_calls'][0]['function']['arguments']),args)
    def test_old_profiles_keep_shape_and_cannot_migrate_saved_payload(self):
        old=dict(profile=STRICT_PROFILE,api_base='https://api.deepseek.com/beta',endpoint='https://api.deepseek.com/beta/chat/completions',strict_tools=True,provider_echo_claimed=False)
        self.assertEqual(transport_profile(self.rt(STRICT_PROFILE).config),old)
        legacy=self.rt(LEGACY_PROFILE);self.assertFalse(transport_profile(legacy.config)['strict_tools'])
        self.assertIn('packet',prepare_tools([tf.PLAN_TOOL])[0][0]['function']['parameters']['properties'])
        for profile in (STRICT_PROFILE,LEGACY_PROFILE):
            rt=self.rt(profile);payload,_=rt.build_payload([dict(role='system',content=tf.PLAN_PROMPT)],[tf.PLAN_TOOL])
            self.assertIn('packet',payload['tools'][0]['function']['parameters']['properties'])
            rt._validate_saved_profile(dict(transport_profile=transport_profile(rt.config)))
            with self.assertRaises(ValueError):self.rt()._validate_saved_profile(dict(transport_profile=transport_profile(rt.config)))
            rt.transport=lambda *unused:reply(internal())
            result=rt.complete([dict(role='system',content=tf.PLAN_PROMPT)],[tf.PLAN_TOOL],'old_'+profile)
            self.assertEqual(json.loads(result['tool_calls'][0]['function']['arguments']),internal())
            self.assertEqual(rt.complete([dict(role='system',content=tf.PLAN_PROMPT)],[tf.PLAN_TOOL],'old_'+profile),result)
            self.assertEqual(rt.budget.launches,1)
        wire=prepare_tools([tf.PLAN_TOOL],profile=FLAT_PLAN_PROFILE)[0]
        child=dict(payload=dict(tools=wire),transport_profile=transport_profile(self.rt().config),internal_tools=[tf.PLAN_TOOL])
        self.assertEqual(case_http.effective_transport(child,self.rt().config)['profile'],FLAT_PLAN_PROFILE)
        child['internal_tools']=[]
        with self.assertRaises(ValueError):case_http.effective_transport(child,self.rt().config)
    def test_flat_repair_cannot_change_prior_complete_numeric_commitments(self):
        flow=object.__new__(tf.TaskFlow);flow.root=self.root
        args=flat(internal());args['design_state']='INVALID_SCHEMA_ONLY'
        prior=dict(response=dict(finish_reason='tool_calls',request_tool_output_profile=FLAT_PLAN_PROFILE,tool_calls=[call(args)]))
        save_json(self.root/'failures/plan-0.json',prior,immutable=True)
        flow._check_prior_plan_parameters(internal()['packet'],1)
        changed=internal()['packet'];changed['proposed_parameters']['stepH']=.3
        with self.assertRaisesRegex(ValueError,'parameter commitments'):flow._check_prior_plan_parameters(changed,1)


if __name__=='__main__':unittest.main()
