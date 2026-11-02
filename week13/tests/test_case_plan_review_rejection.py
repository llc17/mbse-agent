"""Isolated trusted review rollback; no credentials, real HTTP, or dynamics."""
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

from week13.src import case_requests,case_taskflow as tf
from week13.src.case_paths import ROOT,read_json,save_json,sha_file,sha_value
from week13.src.case_budget import CaseBudget
from week13.tests.test_case_agent_requests import Harness,write
from week13.tests.test_case_flat_plan_protocol import flat

PREP=ROOT/'results/shallow-plan-protocol-20261022-03'


class CurrentValidationReadback(unittest.TestCase):
    def test_current_digest_head_warnings_and_stale_head_refused(self):
        from week13.src.case_console_data import plan_review_warnings
        with tempfile.TemporaryDirectory(prefix='review-head-fixture-',dir=PREP) as folder:
            root=Path(folder);packet=dict(summary='TEST_ONLY no source-gap claim')
            ctx=dict(root=str(root),request_id='TEST_ONLY_head',request_digest='TEST_ONLY_request_digest',displayed_digest='f'*64,plan=packet)
            facts=dict(baseline_version_id='v000002',baseline_ref=dict(version_id='v000002'),selected_baseline_sysml_present=True,selected_baseline_sysml_sha256='TEST_ONLY_SYSML',generation_source_rule='CANDIDATE_ONLY')
            fixed=root/'plan-validation.json';write(fixed,dict(packet_digest='TEST_ONLY_OLD',review_warnings=['TEST_ONLY old warning']))
            original=fixed.read_bytes();current=root/'plan-validations'/('f'*64+'.json')
            write(current,dict(schema='v12-displayed-plan-validation-1',request_id=ctx['request_id'],request_digest=ctx['request_digest'],displayed_digest=ctx['displayed_digest'],attempt=1,validation=dict(packet_digest=sha_value(packet),verified_source_facts=facts,review_warnings=['TEST_ONLY current warning'])))
            head=dict(displayed_digest=ctx['displayed_digest'],path=str(current),sha256=sha_file(current));write(root/'plan-validation-head.json',head)
            self.assertEqual(plan_review_warnings(ctx,facts),['TEST_ONLY current warning'])
            write(root/'plan-validation-head.json',dict(head,displayed_digest='0'*64))
            self.assertEqual(plan_review_warnings(ctx,facts),[])
            self.assertEqual(fixed.read_bytes(),original)


class TrustedPlanReview(unittest.TestCase):
    def setUp(self):
        self.saved=(case_requests.ROOT,case_requests.REQUESTS_ROOT,case_requests.LEGACY_PROJECT_ROOT)
        self.temp=tempfile.TemporaryDirectory(prefix='review-fixture-',dir=PREP)
        self.h=Harness(Path(self.temp.name)/'fixture');self.rid='TEST_ONLY_review'
        self.h.bootstrap(self.rid,self.h.project_id,'v000002','把总质量改为92kg，增加部分在原重心，几何控制不变。')
        self.flow=self.h.flow(self.rid);self.root=self.flow.root;self.calls=[]
        original=self.h.transport
        def transport(ctx,payload):
            self.calls.append(deepcopy(payload));result=original(ctx,payload)
            function=result['choices'][0]['message']['tool_calls'][0]['function']
            args=json.loads(function['arguments']);context=json.loads(payload['messages'][1]['content'])
            if 'bounded_protocol_repair' in context:
                args['packet']['design']['assumptions']=[dict(text='TEST_ONLY 当前假设已独立重写',origin='model')]
            function['arguments']=json.dumps(flat(args),ensure_ascii=False)
            stage='plan_repair_1' if 'bounded_protocol_repair' in context else 'plan'
            rid='case_'+stage+'_'+ctx['request_id'];directory=self.root/'http'/sha_value(rid)
            write(directory/'protocol-response.json',result)
            write(directory/'http-status.json',dict(http_status=200,fixture_transport=True))
            write(directory/'process-exit.json',dict(request_id=rid,returncode=0,fixture_transport=True,fixture_call_returned=True,process_launched=False,local_process_terminated=False,timeout_classification=None,child_error_type=None))
            return result
        self.h.transport=transport
        case_requests.authorize_budget(self.rid,'fixture_review','CONFIRM',test_only=True,
            caps=dict(http_requests=9,product_cny=50,total_tokens=9000000,total_output_tokens=3538944,compile=4,simulation=4,candidates=4,software_repair_rounds=3,parameter_revision_rounds=0,observation_extensions=0),
            hard_deadline_utc=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
            repair_allocation=dict(maximum_extra_rounds=3,maximum_before_first_valid_generated_source=1,reserved_for_post_generation_or_runtime=2,maximum_complete_affected_scenario_rerun_rounds=1))
        self.result=asyncio.run(self.flow.plan());self.digest=self.result['displayed_digest']
        self.reason='TEST_ONLY 历史变更叙述与当前需求矛盾，请保持参数并重写当前假设。'
    def tearDown(self):
        case_requests.ROOT,case_requests.REQUESTS_ROOT,case_requests.LEGACY_PROJECT_ROOT=self.saved
        self.temp.cleanup()
    def budget(self):return CaseBudget(task_context=case_requests.load_request(self.rid))
    def reject(self):return self.flow.reject_plan(self.digest,self.reason)
    def sql(self,statement):
        with closing(sqlite3.connect(self.flow.ctx['runtime_authority']['ledger_path'])) as db:
            with db:db.execute(statement)
    def test_reject_stale_confirmation_repair_once_and_append_validation(self):
        protected=[self.root/'plans'/(self.digest+'.json'),self.root/'plan-validation.json',self.root/'turns/plan/response.json']
        protected+=list((self.root/'http').glob('*/*.json'))
        before={str(p):sha_file(p) for p in protected};used=self.budget().usage()
        journal=self.reject();self.assertEqual(self.reject(),journal)
        ctx=case_requests.load_request(self.rid)
        self.assertEqual(ctx['stage'],'PLAN_REJECTED');self.assertIsNone(ctx['proposal_path']);self.assertNotIn('plan',ctx)
        self.assertEqual(self.budget().usage(),used)
        with self.assertRaises(ValueError):self.flow.confirm(self.digest,'CONFIRM')
        repaired=asyncio.run(self.flow.repair('plan',1));new=repaired['displayed_digest']
        self.assertNotEqual(new,self.digest);self.assertEqual(repaired['plan']['proposed_parameters'],self.result['plan']['proposed_parameters'])
        self.assertEqual(self.budget().usage()['counters']['software_repair'],1);self.assertEqual(len(self.calls),2)
        self.assertEqual(read_json(repaired['validation_path'])['displayed_digest'],new)
        self.assertEqual(read_json(self.root/'plan-validation-head.json')['displayed_digest'],new)
        for p,h in before.items():self.assertEqual(sha_file(p),h,p)
        with self.assertRaises(ValueError):self.flow.confirm(self.digest,'CONFIRM')
        with self.assertRaises(ValueError):self.reject()
        with self.assertRaises(ValueError):asyncio.run(self.flow.repair('plan',1))
        self.assertEqual(len(self.calls),2)
        context=json.loads(self.calls[1]['messages'][1]['content'])
        self.assertEqual(context['bounded_protocol_repair']['known_failure']['review_rejection']['path'],str(self.root/'plan-reviews'/(self.digest+'.json')))
        self.assertIn('独立重写',self.calls[1]['messages'][0]['content'])
        self.assertIn('当前design.assumptions',context['bounded_protocol_repair']['required_action'])
        self.flow.refresh();self.flow.reject_plan(new,self.reason,attempt=1)
        with self.assertRaisesRegex(RuntimeError,'reserves at least two'):asyncio.run(self.flow.repair('plan',2))
        self.assertEqual(self.budget().usage()['counters']['software_repair'],1);self.assertEqual(len(self.calls),2)
    def test_partial_journal_write_resumes_idempotently(self):
        original=tf.save_json
        def interrupted(path,value,**kwargs):
            if Path(path)==self.root/'task-context.json' and value.get('stage')=='PLAN_REJECTED':raise RuntimeError('TEST_ONLY crash before active head')
            return original(path,value,**kwargs)
        with patch.object(tf,'save_json',side_effect=interrupted),self.assertRaisesRegex(RuntimeError,'crash'):self.reject()
        self.assertEqual(case_requests.load_request(self.rid)['displayed_digest'],self.digest)
        journal=read_json(self.root/'plan-reviews'/(self.digest+'.json'))
        self.assertEqual(self.reject(),journal);self.assertEqual(self.reject(),journal)
        self.assertEqual(self.budget().usage()['counters'],{});self.assertEqual(len(self.calls),1)
    def test_stale_confirmed_generated_unknown_closed_and_exit_rejected_without_journal(self):
        with self.assertRaisesRegex(ValueError,'stale'):self.flow.reject_plan('0'*64,self.reason)
        context=self.root/'task-context.json';original=context.read_bytes()
        self.flow.confirm(self.digest,'CONFIRM')
        with self.assertRaisesRegex(PermissionError,'unconfirmed'):self.reject()
        context.write_bytes(original);self.flow.refresh()
        save_json(self.root/'generation.json',dict(test_only=True))
        with self.assertRaisesRegex(PermissionError,'generated'):self.reject()
        (self.root/'generation.json').unlink()
        self.sql("UPDATE requests SET status='unknown'")
        with self.assertRaisesRegex(PermissionError,'UNKNOWN'):self.reject()
        self.sql("UPDATE requests SET status='succeeded'")
        self.sql("INSERT INTO meta VALUES('closed','true')")
        with self.assertRaisesRegex(PermissionError,'closed'):self.reject()
        self.sql("DELETE FROM meta WHERE key='closed'")
        exit_path=next((self.root/'http').glob('*/process-exit.json'));exit_receipt=read_json(exit_path)
        write(exit_path,dict(exit_receipt,fixture_call_returned=False))
        with self.assertRaisesRegex(ValueError,'process exit'):self.reject()
        self.assertFalse((self.root/'plan-reviews').exists());self.assertFalse((self.root/'failures').exists())


if __name__=='__main__':unittest.main()
