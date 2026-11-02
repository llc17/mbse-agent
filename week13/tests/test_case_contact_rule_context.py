"""Outbound readonly context keeps mission failure distinct from contact failure."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from week13.src import case_taskflow as tf
from week13.src.case_contact_review import current_summary,RULE,SCHEMA,RULE_VERSION
from week13.src.case_paths import ROOT,read_json

OUT=ROOT/'results/shallow-plan-protocol-20261022-03'


class ContactRuleContext(unittest.TestCase):
    def test_actual_readonly_payload_distinguishes_two_FAIL_fields(self):
        for contact_status,task_status,completion in (('PASS','FAIL','OBSERVATION_INTERVAL_INCOMPLETE'),('FAIL','UNKNOWN','CONTACT_ANOMALY_ROUTE_COMPLETE')):
            with self.subTest(contact_status=contact_status),tempfile.TemporaryDirectory(prefix='rule-context-fixture-',dir=OUT) as directory:
                root=Path(directory);source='model M parameter Real m=85; parameter Real I_pitch=20; parameter Real T_main_max=60; parameter Real T_nose_max=25; end M;'
                assessment=dict(task_status=task_status,validity_status=contact_status,completion_status=completion,physical_status='UNKNOWN')
                review=dict(schema=SCHEMA,rule_version=RULE_VERSION,status=contact_status,validity_status=contact_status,task_status=task_status,route_status='PASS' if contact_status=='FAIL' else task_status,diagnostic_status=contact_status,completion_status=completion,execution_evidence_status='PASS',current_chain_status='PASS',physical_status='UNKNOWN',current_assessment=assessment,hard_contradictions=[] if contact_status=='PASS' else [dict(kind='TEST_ONLY_full_circle_inside')],precedence_rule=RULE,binding=dict(project_id='p',version_id='v000006',task_id='t',spec_digest='a'*64,source_sha256='b'*64,manifest_sha256='c'*64,csv_sha256='d'*64,rule_version=RULE_VERSION))
                snapshot=dict(project_id='p',version_id='v000006',task_id='t',state='succeeded',evidence_status='COMPLETE',tool_status='PASS',chain_status='PASS',task_status='FAIL',frozen_spec=dict(artifact=dict(modelica_source=source)),outcome=dict(tool_status='PASS',chain_status='PASS',task_status='FAIL',mission_assessment=assessment,execution_mode='LIVE',test_only=False),current_contact_review=review)
                state=dict(request_id='TEST_ONLY_rule',project_id='p',latest_request_execution=current_summary(snapshot))
                ctx=dict(request_id='TEST_ONLY_rule',root=str(root),raw_request='TEST_ONLY 仅解释已绑定结果',baseline_snapshot=dict(frozen_spec=dict(artifact=dict(modelica_source=source))))
                flow=tf.TaskFlow(deepcopy(ctx));flow.state=Mock(side_effect=lambda:deepcopy(state));flow.open=Mock(return_value=SimpleNamespace(_artifact_version=lambda _:deepcopy(snapshot)))
                before=deepcopy((ctx,snapshot,state));captured=[]
                async def call(stage,messages,tools):
                    captured.append(deepcopy(messages));self.assertEqual(stage,'reopen_0');self.assertEqual(tools,[])
                    context=json.loads(messages[1]['content']);actual=context['actual_request_execution']
                    self.assertEqual(actual['current_contact_review']['status'],contact_status)
                    self.assertEqual(actual['task_status'],task_status)
                    self.assertEqual(actual['outcome']['completion_status'],completion)
                    self.assertIn(RULE_VERSION,messages[0]['content'])
                    self.assertIn('route_status',messages[0]['content']);self.assertIn('diagnostic_status',messages[0]['content'])
                    answer='TEST_ONLY 字段已区分，不改模型与结果。'
                    return dict(content=answer,finish_reason='stop',tool_calls=[],assistant_message=dict(role='assistant',content=answer,reasoning_content='TEST_ONLY'))
                flow.call=call
                with patch('week13.src.case_stage_facts.read_stage_facts',return_value=dict(TEST_ONLY=True)):
                    result=asyncio.run(flow.ask('TEST_ONLY 保留当前字段，解释任务与模型有效性的区别。',reopen=True))
                self.assertTrue(result['read_only_status_unchanged']);self.assertEqual((ctx,snapshot,state),before)
                self.assertEqual(flow.open.call_args.kwargs,dict(read_only=True));self.assertEqual(len(captured),1)
                self.assertEqual([p.name for p in root.iterdir()],['dialogue.json'])
                self.assertTrue(read_json(root/'dialogue.json')[0]['reopen'])


if __name__=='__main__':unittest.main()
