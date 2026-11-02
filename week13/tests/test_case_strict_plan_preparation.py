"""Isolated protocol fixtures: no product calls, credentials or real budget."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from week13.src import case_taskflow as tf
from week13.src.case_paths import ROOT, save_json, read_json, sha_value
from week13.src.case_strict_tools import decode_calls, prepare_tools, validate_value, EMPTY_MARKER

PREP=ROOT/'results/strict-plan-protocol-preparation-20261022-02'


def arguments(support='SUPPORTED'):
    packet=dict(intent='CHANGE',support=support,missing=[],unsupported=[],requirements=[],
                design={},proposed_parameters={},defaults=[],unknowns=[],rationale='fixture',scope={})
    if support=='SUPPORTED':
        packet.update(design=dict(summary='fixture',assumptions=[],parameters=[]),
                      proposed_parameters=dict(m=85,I_pitch=20,T_main_max=60,T_nose_max=25),
                      scope=deepcopy(tf.MASS_SCOPE_PACKET))
    else:
        packet['missing' if support=='NEEDS_INPUT' else 'unsupported']=['fixture']
        for key in ('design','proposed_parameters','scope'):packet[key]=deepcopy(EMPTY_MARKER)
    return dict(packet=packet)


def calls(args):
    return [dict(id='fixture_call',type='function',function=dict(name='case_request_plan',arguments=json.dumps(args,ensure_ascii=False)))]


class StrictPlanPreparation(unittest.TestCase):
    def test_observed_scope_shape_rejected_with_both_paths_and_no_value_leak(self):
        malformed=arguments();malformed['packet']['design']['scope']=malformed['packet'].pop('scope')
        malformed['packet']['design']['scope']['frozen']='SECRET_VALUE_NOT_A_FIELD'
        before=deepcopy(malformed)
        with self.assertRaises(ValueError) as caught:decode_calls(calls(malformed),[tf.PLAN_TOOL])
        message=str(caught.exception)
        self.assertIn('missing: $.packet.scope',message)
        self.assertIn('unexpected: $.packet.design.scope',message)
        self.assertNotIn('SECRET_VALUE_NOT_A_FIELD',message)
        self.assertEqual(malformed,before)
        unknown=arguments();unknown['packet']['design']['SECRET_PROVIDER_KEY_VALUE']='hidden'
        with self.assertRaises(ValueError) as caught:decode_calls(calls(unknown),[tf.PLAN_TOOL])
        self.assertNotIn('SECRET_PROVIDER_KEY_VALUE',str(caught.exception))

    def test_all_support_branches_and_schema_derived_guide_remain_valid(self):
        for support in ('SUPPORTED','NEEDS_INPUT','UNSUPPORTED'):
            value=arguments(support)
            validate_value(value,prepare_tools([tf.PLAN_TOOL])[0][0]['function']['parameters'])
            decoded,_=decode_calls(calls(value),[tf.PLAN_TOOL])
            self.assertEqual(json.loads(decoded[0]['function']['arguments'])['packet']['support'],support)
        self.assertIn(tf.PLAN_STRUCTURE_GUIDE,tf.PLAN_PROMPT)
        self.assertIn('packet.scope与packet.design同级',tf.PLAN_STRUCTURE_GUIDE)
        for name in tf.PLAN_FIELDS:self.assertIn(name,tf.PLAN_STRUCTURE_GUIDE)


class KnownRejectedFeedback(unittest.TestCase):
    def setUp(self):
        PREP.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='fixture-',dir=PREP);self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.flow=object.__new__(tf.TaskFlow);self.flow.root=self.root
        self.flow.ctx=dict(request_id='fixture',request_digest='fixture_digest',runtime_authority=dict(ledger_path=str(self.root/'fake.sqlite'),sha256='fixture_authority'))
        self.flow.refresh=lambda:self.flow.ctx
        self.budget=Mock();self.row=dict(status='rejected',actual_tokens=30,actual_output=10,actual_cost=.001)
        self.budget.request=lambda rid:self.row
        self.runtime=Mock();self.runtime.config=read_json(ROOT/'configs/case_runtime.json');self.runtime.budget=self.budget
        self.runtime.require_credentials=lambda:None
        self.flow._runtime=lambda:self.runtime
        self.patches=[patch('week13.src.case_requests.require_planning',return_value=self.flow.ctx),
                      patch('week13.src.case_requests.authority_for',return_value={}),
                      patch('week13.src.case_budget.CaseBudget',return_value=self.budget),
                      patch.object(tf,'project_messages',side_effect=lambda ctx,messages:messages),
                      patch.object(tf,'emit_progress')]
        for item in self.patches:item.start();self.addCleanup(item.stop)
        self.args=arguments();self.args['packet']['design']['scope']=self.args['packet'].pop('scope')
        self.raw=dict(id='fixture_response',model='deepseek-flash',choices=[dict(index=0,finish_reason='tool_calls',message=dict(role='assistant',tool_calls=calls(self.args)))],usage=dict(prompt_tokens=20,completion_tokens=10,total_tokens=30))
        self.usage_known=True
        def rejected(messages,tools,rid):
            save_json(self.root/'http'/sha_value(rid)/'rejected-response.json',dict(usage_known=self.usage_known,response=self.raw),immutable=True)
            decode_calls(calls(self.args),[tf.PLAN_TOOL])
        self.runtime.complete.side_effect=rejected
        self.messages=[dict(role='system',content=tf.PLAN_PROMPT),dict(role='user',content='fixture')]
    async def initial(self):return await self.flow.call('plan',self.messages,[tf.PLAN_TOOL])
    def test_known_complete_rejected_reply_reaches_existing_bounded_feedback_once(self):
        feedback=[]
        async def repair(stage,attempt):
            feedback.append(self.flow.feedback(stage,attempt))
            return decode_calls(calls(arguments('NEEDS_INPUT')),[tf.PLAN_TOOL])[0]
        self.flow.repair=repair
        result=asyncio.run(self.flow.bounded('plan',self.initial))
        self.assertEqual(len(result),1);self.assertEqual(self.runtime.complete.call_count,1)
        self.budget.consume.assert_called_once()
        self.assertIn(tf.PLAN_STRUCTURE_GUIDE,feedback[0]['required_action'])
        prior=feedback[0]['known_failure']
        self.assertIn('$.packet.design.scope',prior['error'])
        self.assertEqual(prior['response']['tool_calls'],calls(self.args))
        self.assertFalse((self.root/'proposal.json').exists())
    def test_unknown_fee_unknown_or_incomplete_has_no_failure_and_no_bounded_retry(self):
        cases=[('unknown',True,.001,'tool_calls',True),('rejected',False,.001,'tool_calls',True),
               ('rejected',True,None,'tool_calls',True),('rejected',True,.001,'length',True),
               ('rejected',True,.001,'tool_calls',False)]
        for index,(status,known,cost,finish,has_usage) in enumerate(cases):
            with self.subTest(index=index):
                self.flow.root=self.root/str(index);self.flow.root.mkdir()
                self.root_for_save=self.flow.root
                self.row.update(status=status,actual_cost=cost);self.usage_known=known
                self.raw['choices'][0]['finish_reason']=finish
                self.raw['usage']=dict(prompt_tokens=20,completion_tokens=10,total_tokens=30) if has_usage else None
                # Rebind the fixture sink to this isolated case's root.
                def reject(messages,tools,rid):
                    save_json(self.flow.root/'http'/sha_value(rid)/'rejected-response.json',dict(usage_known=self.usage_known,response=self.raw),immutable=True)
                    raise ValueError('TEST_ONLY known structural failure')
                self.runtime.complete.side_effect=reject
                self.flow.repair=Mock(side_effect=AssertionError('no retry'))
                with self.assertRaises(ValueError):asyncio.run(self.flow.bounded('plan',self.initial))
                self.assertFalse((self.flow.root/'failures').exists());self.flow.repair.assert_not_called()
        self.budget.consume.assert_not_called()
    def test_closed_or_frozen_gate_still_blocks_feedback_without_resend(self):
        self.budget.consume.side_effect=RuntimeError('TEST_ONLY closed/frozen authority')
        async def repair(stage,attempt):return self.flow.feedback(stage,attempt)
        self.flow.repair=repair
        with self.assertRaisesRegex(RuntimeError,'closed/frozen'):asyncio.run(self.flow.bounded('plan',self.initial))
        self.assertEqual(self.runtime.complete.call_count,1)


if __name__=='__main__':unittest.main()
