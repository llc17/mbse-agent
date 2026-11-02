"""Finite planning schema/rationale compatibility, injected wire only."""
import asyncio
from contextlib import closing
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from week13.src import case_paths,case_requests,case_taskflow
from week13.tests.test_case_agent_requests import Harness,write

EVIDENCE=case_paths.ROOT/'results/context-growth-20261022-01'
RATIONALE={'summary':'完整依据，含"引号"和\n换行','cited_source':'已引用原始需求','not_cited':'没有将历史目标当新要求'}


class RationaleSyntax(unittest.TestCase):
    def test_plan_schema_defines_every_field_and_requires_string_rationale(self):
        tool=case_taskflow.PLAN_TOOL['function']['parameters']['properties']['packet'];props=tool['properties']
        self.assertEqual(set(tool['required']),set(case_taskflow.PLAN_FIELDS))
        self.assertTrue(all(props[name] for name in case_taskflow.PLAN_FIELDS))
        self.assertEqual(props['rationale']['type'],'string');self.assertEqual(props['requirements']['type'],'array')
        self.assertFalse(tool['additionalProperties']);self.assertNotIn('strict',case_taskflow.PLAN_TOOL['function'])
        self.assertIn('rationale必须是完整、非空的中文字符串',case_taskflow.PLAN_PROMPT)

    def test_flat_map_is_complete_reversible_and_changes_no_other_field(self):
        original={'rationale':deepcopy(RATIONALE),'unknowns':['保留全部未知'],'other':{'key':'unchanged'}};before=deepcopy(original)
        value,audit=case_taskflow.normalize_plan_rationale(original)
        self.assertEqual(json.loads(value['rationale']),RATIONALE);self.assertEqual(original,before)
        self.assertEqual({k:v for k,v in value.items() if k!='rationale'},{k:v for k,v in original.items() if k!='rationale'})
        self.assertEqual(audit['field_changes']['rationale']['before'],RATIONALE);self.assertFalse(audit['missing_fields_added'])

    def test_empty_nested_numeric_bool_or_empty_value_map_is_refused(self):
        for value in ({},{'summary':{}},{'summary':[]},{'summary':1},{'summary':True},{'summary':None},{'summary':'  '}):
            with self.subTest(value=value),self.assertRaisesRegex(ValueError,'only nonempty string'):
                case_taskflow.normalize_plan_rationale({'rationale':value})

    def test_duplicate_json_keys_are_rejected_before_normalization(self):
        response=dict(finish_reason='tool_calls',tool_calls=[dict(function=dict(name='case_request_plan',arguments='{"packet":{"rationale":{"summary":"first","summary":"second"}}}'))])
        with self.assertRaisesRegex(ValueError,'duplicate JSON key'):case_taskflow.parse_tool(response,'case_request_plan','packet')


class RationaleFlow(unittest.TestCase):
    def setUp(self):
        EVIDENCE.mkdir(parents=True,exist_ok=True);self.temp=tempfile.TemporaryDirectory(prefix='TEST_ONLY-rationale-',dir=EVIDENCE)
        self.saved=(case_requests.ROOT,case_requests.REQUESTS_ROOT,case_requests.LEGACY_PROJECT_ROOT)
        self.h=Harness(Path(self.temp.name)/'fixture');self.rid='TEST_ONLY_rationale'
        self.h.bootstrap(self.rid,self.h.project_id,'v000002','把总质量改为92kg，增加部分在原重心，几何控制不变。');self.h.authorize(self.rid)
        self.flow=self.h.flow(self.rid);self.root=Path(self.flow.ctx['root']);self.original_transport=self.h.transport;self.drop_field=None
        self.h.transport=self.transport
    def tearDown(self):
        case_requests.ROOT,case_requests.REQUESTS_ROOT,case_requests.LEGACY_PROJECT_ROOT=self.saved;self.temp.cleanup()
    def transport(self,ctx,payload):
        result=self.original_transport(ctx,payload);function=result['choices'][0]['message']['tool_calls'][0]['function'];args=json.loads(function['arguments'])
        args['packet']['rationale']=deepcopy(RATIONALE)
        if self.drop_field:args['packet'].pop(self.drop_field)
        function['arguments']=json.dumps(args,ensure_ascii=False)
        stage='plan_repair_1' if 'bounded_protocol_repair' in json.loads(payload['messages'][1]['content']) else 'plan'
        rid='case_'+stage+'_'+ctx['request_id'];directory=Path(ctx['root'])/'http'/case_paths.sha_value(rid)
        write(directory/'protocol-response.json',result);write(directory/'http-status.json',dict(http_status=200,fixture_transport=True))
        write(directory/'process-exit.json',dict(request_id=rid,returncode=0,fixture_transport=True,fixture_call_returned=True,process_launched=False,local_process_terminated=False,timeout_classification=None,child_error_type=None))
        return result
    def known_failure(self):
        with patch.object(case_taskflow,'normalize_plan_rationale',side_effect=lambda packet:(deepcopy(packet),None)):
            with self.assertRaisesRegex(ValueError,'plan rationale required'):asyncio.run(self.flow.plan())
    def folder(self):return self.root/'http'/case_paths.sha_value('case_plan_'+self.rid)

    def test_ordinary_plan_normalizes_all_keys_and_saves_audit(self):
        result=asyncio.run(self.flow.plan());self.assertEqual(json.loads(result['plan']['rationale']),RATIONALE)
        audit=case_paths.read_json(self.root/'normalizations/plan-0-rationale-object.json')
        self.assertTrue(audit['all_keys_and_values_preserved']);self.assertEqual(self.flow.ctx['stage'],'PROPOSED')
        self.assertIsNone(self.flow.ctx['confirmation_path']);self.assertFalse(self.flow.ctx['allow_execution'])

    def test_missing_field_still_fails_and_is_never_filled_from_reasoning(self):
        self.drop_field='unknowns'
        with self.assertRaisesRegex(ValueError,'fields differ'):asyncio.run(self.flow.plan())
        self.assertFalse((self.root/'normalizations/plan-0-rationale-object.json').exists());self.assertFalse(self.flow.ctx.get('plan'))

    def test_zero_http_revalidation_preserves_originals_ledger_store_and_selection(self):
        self.known_failure();before={str(p):case_paths.sha_file(p) for p in self.root.rglob('*') if p.is_file() and p.name!='task-context.json'}
        ledger=Path(self.flow.ctx['runtime_authority']['ledger_path']).read_bytes();store=self.h.db.read_bytes()
        with patch.object(self.flow,'_runtime',side_effect=AssertionError('no HTTP')),patch.object(self.flow,'mcp',side_effect=AssertionError('no MCP')):
            packet,audit=self.flow._plan_rationale_evidence(0)
            result=self.flow.revalidate_plan(0,expected_packet_digest=case_paths.sha_value(packet))
        self.assertEqual(json.loads(result['plan']['rationale']),RATIONALE);self.assertTrue(result['requires_local_confirmation'])
        self.assertEqual(result['original_http_status'],'succeeded');self.assertEqual(result['product_http_added'],0)
        self.assertFalse(self.flow.ctx['allow_execution']);self.assertIsNone(self.flow.ctx['confirmation_path'])
        self.assertEqual(ledger,Path(self.flow.ctx['runtime_authority']['ledger_path']).read_bytes());self.assertEqual(store,self.h.db.read_bytes())
        for p,digest in before.items():self.assertEqual(case_paths.sha_file(p),digest,p)
        self.assertEqual(self.flow.revalidate_plan(0),result)

    def test_unsuccessful_or_unknown_ledger_cannot_be_revalidated(self):
        self.known_failure()
        for status in ('not_dispatched','reserved','dispatched','rejected','unknown'):
            with self.subTest(status=status):
                with closing(sqlite3.connect(self.flow.ctx['runtime_authority']['ledger_path'])) as db,db:
                    db.execute('UPDATE requests SET status=?',(status,))
                with self.assertRaisesRegex(ValueError,'no UNKNOWN'):self.flow._plan_rationale_evidence(0)

    def test_request_response_payload_exit_and_authority_drift_are_refused(self):
        self.known_failure()
        changes=[(self.folder()/'request.json',lambda v:v.update(payload_sha256='0'*64)),
                 (self.folder()/'protocol-response.json',lambda v:v.update(id='drift')),
                 (self.folder()/'process-exit.json',lambda v:v.update(fixture_call_returned=False)),
                 (self.root/'turns/plan/response.json',lambda v:v.update(response_id='drift'))]
        for path,change in changes:
            with self.subTest(path=path.name):
                before=case_paths.read_json(path);value=deepcopy(before);change(value);write(path,value)
                with self.assertRaises(ValueError):self.flow._plan_rationale_evidence(0)
                write(path,before)
        with closing(sqlite3.connect(self.flow.ctx['runtime_authority']['ledger_path'])) as db,db:
            db.execute("UPDATE meta SET value='different authority' WHERE key='authorization_sha256'")
        with self.assertRaisesRegex(ValueError,'no UNKNOWN'):self.flow._plan_rationale_evidence(0)

    def test_unexpected_packet_digest_expired_or_confirmed_context_is_refused(self):
        self.known_failure()
        with self.assertRaisesRegex(ValueError,'expected packet'):self.flow.revalidate_plan_rationale(0,expected_packet_digest='0'*64)
        class Future(datetime):
            @classmethod
            def now(cls,tz=None):return datetime.now(timezone.utc)+timedelta(days=1)
        with patch.object(case_taskflow,'datetime',Future),self.assertRaisesRegex(PermissionError,'active original'):self.flow._plan_rationale_evidence(0)
        ctx=case_requests.load_request(self.rid);ctx['confirmation_path']='TEST_ONLY_different';write(self.root/'task-context.json',ctx)
        with self.assertRaisesRegex(PermissionError,'cannot confirm'):self.flow._plan_rationale_evidence(0)


if __name__=='__main__':unittest.main()
