"""Bounded readonly-interface regressions: saved text and TEST_ONLY local Store.

Fixture transports return synthetic replies. No HTTP/MCP/compiler/simulation.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from week13.src import case_console, case_requests, case_taskflow
from week13.src.case_budget import CaseBudget, connection
from week13.src.case_paths import ROOT, read_json, save_json
from week13.src.case_runtime import CaseRuntime
from week13.src.case_semantic_guard import protect_explanation
from week13.tests.test_case_agent_requests import Harness, NoLaunch, finish_fixture, synthetic_spec
from week13.src.case_backend import module

OUT=ROOT/'results/readonly-interface-repair-20261028-01'
FINDINGS=ROOT/'results/two-round-agent-test-20261027-01/package/closeout-readonly-findings.json'

def old_namespace(name):
    result=dict(__name__='week13.src._frozen_before',__package__='week13.src')
    exec(compile((OUT/'before'/name).read_text(encoding='utf8'),'SAVED_BEFORE_REPAIR/'+name,'exec'),result)
    return result

def saved():
    f=read_json(FINDINGS);response=read_json(f['explanation']['raw_answer_path'])
    turns=read_json(Path(f['explanation']['raw_answer_path']).parents[2]/'dialogue.json')
    turn=next(x for x in turns if x['raw_answer']==response['content'])
    return f,response,turn

def record(name,value):
    (OUT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf8')

class VersionReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.finding,cls.response,cls.turn=saved()

    def checked(self,text,binding=None,references=None):
        return protect_explanation(text,binding if binding is not None else self.turn['binding'],source_references=self.turn['current_source_references'] if references is None else references)['explanation_validation']

    def test_exact_saved_reply_old_rejects_new_displays_without_any_character_change(self):
        raw=self.response['content'];original=old_namespace('case_semantic_guard.py')['protect_explanation'](raw,self.turn['binding'],source_references=self.turn['current_source_references'])
        current=self.checked(raw)
        self.assertFalse(original['explanation_validation']['body_displayed']);self.assertTrue(current['body_displayed'])
        self.assertFalse(current['full_semantic_validation']);self.assertEqual(current['raw_answer_sha256'],self.finding['explanation']['raw_answer_sha256'])
        record('exact-saved-response-revalidation.json',dict(original=original['explanation_validation'],current=current,original_response_sha256=hashlib.sha256(Path(self.finding['explanation']['raw_answer_path']).read_bytes()).hexdigest(),product_http_added=0))

    def test_trusted_shorthand_and_product_version_name(self):
        for text in ('v0 tool_status=NOT_RUN；v1 tool_status=PASS。','v0 tool_status=NOT_RUN。v1 task_status=PASS。','## v0\ntool_status=NOT_RUN\n## v1\ntool_status=PASS','V12 Agent的只读结果说明；当前 tool_status=PASS。','不能把v0当作task_status=PASS。','sampled-full-circle-interior-20261027-v2，当前 task_status=PASS。'):
            with self.subTest(text=text):self.assertTrue(self.checked(text)['body_displayed'],self.checked(text)['issues'])

    def test_bound_baseline_and_current_contradictions_still_block_at_correct_version(self):
        for text,version in [('v0 tool_status=PASS。','v000000'),('v1 tool_status=NOT_RUN。','v000001'),('## v1\nv0 task_status=PASS。','v000000'),('## v0\nv1 tool_status=NOT_RUN。','v000001'),('v0 tool_status=PASS 而 v1 tool_status=PASS。','v000000'),('v0 tool_status=NOT_RUN；v1 tool_status=NOT_RUN。','v000001'),('v1 tool_status=PASS；v0 tool_status=PASS。','v000000'),('v0和v1 tool_status=PASS。','v000000'),('v0 tool_status=NOT_RUN，task_status=PASS。','v000000')]:
            with self.subTest(text=text):
                result=self.checked(text);self.assertFalse(result['body_displayed']);self.assertIn(version,{i.get('version_id') for i in result['issues']})

    def test_unknown_short_full_and_noncanonical_aliases_never_fall_back(self):
        for text in ('v10 tool_status=PASS。','v000010 tool_status=PASS。','v01 task_status=PASS。','## v2\ntool_status=PASS。'):
            with self.subTest(text=text):
                result=self.checked(text);self.assertFalse(result['body_displayed']);self.assertIn('UNBOUND_VERSION_REFERENCE',{i['code'] for i in result['issues']})
        result=self.checked('v0 tool_status=NOT_RUN。',references=[])
        self.assertFalse(result['body_displayed']);self.assertIn('UNBOUND_VERSION_REFERENCE',{i['code'] for i in result['issues']})

    def test_v1_does_not_match_v10_and_ambiguous_numeric_alias_remains_untrusted(self):
        other=deepcopy(self.turn['current_source_references'][0]);other['version_id']=other['baseline_ref']['version_id']='v000010'
        refs=self.turn['current_source_references']+[other]
        self.assertTrue(self.checked('v10 tool_status=NOT_RUN；v1 tool_status=PASS。',references=refs)['body_displayed'])
        self.assertFalse(self.checked('v10 tool_status=PASS；v1 tool_status=PASS。',references=refs)['body_displayed'])
        self.assertFalse(self.checked('v10 tool_status=NOT_RUN；v1 tool_status=NOT_RUN。',references=refs)['body_displayed'])
        other['version_id']=other['baseline_ref']['version_id']='v0000001'
        result=self.checked('v1 tool_status=PASS。',references=self.turn['current_source_references']+[other])
        self.assertFalse(result['body_displayed']);self.assertIn('AMBIGUOUS_VERSION_REFERENCE',{i['code'] for i in result['issues']})
        self.assertFalse(self.checked('## v0 与 v1\ntool_status=PASS。')['body_displayed'])

class ResultFollowupTests(unittest.TestCase):
    def setUp(self):
        self.old=(case_requests.ROOT,case_requests.REQUESTS_ROOT,case_requests.LEGACY_PROJECT_ROOT)
        self.tmp=tempfile.TemporaryDirectory(prefix='test-only-',dir=OUT)
        self.h=Harness(Path(self.tmp.name)/'fixture');self.rid='TEST_ONLY_followup_reopen_in_request_name'
        ctx=self.h.bootstrap(self.rid,self.h.project_id,'v000002','TEST_ONLY 只读结果出口维护测试')
        caps=dict(http_requests=5,product_cny=35,total_tokens=5000000,total_output_tokens=5*393216,compile=1,simulation=1,candidates=1,software_repair_rounds=0,parameter_revision_rounds=0,observation_extensions=0)
        case_requests.authorize_budget(self.rid,'TEST_ONLY_followup_budget','CONFIRM',test_only=True,caps=caps,hard_deadline_utc=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat())
        self.flow=self.h.flow(self.rid);self.ctx=self.flow.ctx;self.sent=[]
        def transport(ctx,payload):
            self.sent.append(deepcopy(payload));self.assertFalse(payload.get('tools'))
            return dict(id='TEST_ONLY_LOCAL_NO_NETWORK',model='deepseek-flash',choices=[dict(message=dict(content='TEST_ONLY 仅只读讨论已有终态结果，实物能力未知。',reasoning_content='fixture'),finish_reason='stop')],usage=dict(prompt_tokens=20,completion_tokens=10,total_tokens=30))
        self.h.transport=transport
        self.budget=CaseBudget(task_context=self.ctx)

    def tearDown(self):
        case_requests.ROOT,case_requests.REQUESTS_ROOT,case_requests.LEGACY_PROJECT_ROOT=self.old
        self.tmp.cleanup()

    def terminal(self):
        # Fixture-only Store seeding: this does not invoke TaskFlow.execute or
        # grant a production request execution permission.
        context=self.h.root/'TEST_ONLY_seed_execution_context.json'
        save_json(context,dict(schema='v11-artifact-execution-context-1',context_id='TEST_ONLY_seed',status='ACTIVE',mode='TEST_ONLY',allow_execution=True,max_tasks=1,worker_timeout_s=120,simulation_timeout_s=120))
        svc=module('artifact_service').ArtifactService(self.ctx['db_path'],self.ctx['session_id'],context=context,worker_manager=NoLaunch());svc.project_root=Path(self.ctx['project_root'])
        item=svc.record_source('TEST_ONLY synthetic terminal evidence, not a model run')
        spec=synthetic_spec(dict(id=item['source_id'],text=item['text']),95)
        spec['baseline_ref']=self.ctx['baseline_ref']
        proposal=svc.proposal_prepare(spec)['proposal'];row=svc.store.proposal(proposal['proposal_id']);svc.confirm_local(row['proposal_id'],row['displayed_digest'])
        task=svc.execution_start(row['proposal_id'],'TEST_ONLY_result_task')['task'];finish_fixture(svc,task,self.ctx['project_root'],task_status='FAIL')
        return task['task_id']

    def charge(self,rid,category):
        self.budget.reserve_http(rid,'TEST_ONLY_hash_'+rid,category,self.budget.authority['model_controls'])
        self.budget.launch_http(rid,fixture=True)
        self.budget.finish_http(rid,response=dict(test_only=True),tokens=30,output=10,cost=.001)

    def four_rows(self,*,explanation=True,unknown=True):
        if unknown:
            self.budget.reserve_http('TEST_ONLY_original','TEST_ONLY_original_hash','design',self.budget.authority['model_controls'])
            self.budget.launch_http('TEST_ONLY_original',fixture=True);self.budget.finish_http('TEST_ONLY_original',error='TEST_ONLY connection-phase failure',uncertain=True)
            original=self.budget.request('TEST_ONLY_original')
            self.budget.authorize_connect_retry('TEST_ONLY_original','TEST_ONLY_replacement','TEST_ONLY_original_hash',dict(test_only=True,explicit_fixture_exception=True))
            self.budget.reserve_http('TEST_ONLY_replacement','TEST_ONLY_original_hash','design',self.budget.authority['model_controls'])
            self.budget.launch_http('TEST_ONLY_replacement',fixture=True);self.budget.finish_http('TEST_ONLY_replacement',response=dict(test_only=True),tokens=30,output=10,cost=.001)
        else:
            self.charge('TEST_ONLY_plan','design');self.charge('TEST_ONLY_replacement','design');original=None
        self.charge('TEST_ONLY_generate','generation')
        self.charge('TEST_ONLY_explain' if explanation else 'TEST_ONLY_design','explanation' if explanation else 'design')
        return original

    def test_real_console_flow_runtime_and_sqlite_use_last_review_exit_preserving_unknown(self):
        self.terminal();original=self.four_rows()
        old=old_namespace('case_runtime.py')['CaseRuntime']._category(None,'case_ask_0_plain')
        with self.assertRaisesRegex(RuntimeError,'protects'):
            self.budget.reserve_http('TEST_ONLY_before_repair','before',old,self.budget.authority['model_controls'])
        calls=[];answers=[]
        class Runner:
            def call(inner,action,**kwargs):
                calls.append(action);answer=asyncio.run(self.flow.ask(kwargs['question']));answers.append(answer);return answer
        ui=case_console.Console.__new__(case_console.Console);ui.selection=dict(request_id=self.rid);ui.ctx=self.ctx;ui.runner=Runner();ui.output=lambda _:None;ui.budget_guard=lambda:True;ui.refresh=lambda:None
        ui.handle('/ask TEST_ONLY 解释已有结果，不执行')
        self.assertEqual(calls,['request-ask']);self.assertEqual(len(self.sent),1)
        answer=answers[0];self.assertFalse(answer['reopened']);self.assertTrue(answer['read_only_status_unchanged'])
        self.assertEqual(answer['budget_purpose']['purpose'],'EXISTING_RESULT_REVIEW_EXIT');self.assertFalse(answer['budget_purpose']['cold_reopen'])
        self.assertEqual(self.budget.usage()['requests'],5);self.assertEqual(self.budget.request('TEST_ONLY_original'),original)
        rid='case_result_followup_0_'+self.rid;self.assertEqual(self.budget.request(rid)['category'],'reopen')
        self.assertFalse(read_json(self.flow.root/'dialogue.json')[-1]['reopen'])
        self.assertEqual(original['status'],'unknown');self.assertIsNone(original['actual_cost']);self.assertEqual(original['reserve_tokens'],1000000)
        with self.assertRaisesRegex(RuntimeError,'quota|protects'):
            asyncio.run(self.flow.ask('TEST_ONLY 不允许第六次'))
        record('isolated-console-followup.json',dict(test_only=True,transport='FIXTURE_NO_NETWORK',original_category=old,before_refusal='4+1+1>5',after_category='reopen',budget_purpose=answer['budget_purpose'],reopened=answer['reopened'],http_rows_count=5,original_unknown_row_unchanged=self.budget.request('TEST_ONLY_original')==original,original_unknown=original,fixture_transports=1,product_http_added=0))

    def test_preexecution_ordinary_ask_cannot_consume_reserved_exit_even_with_keywords_in_request_id(self):
        self.four_rows()
        self.assertEqual(CaseRuntime._category(None,'case_ask_0_'+self.rid),'consultation')
        with self.assertRaisesRegex(RuntimeError,'protects'):
            asyncio.run(self.flow.ask('TEST_ONLY 未执行前一般咨询'))
        self.assertEqual(len(self.sent),0);self.assertEqual(self.budget.usage()['requests'],4)

    def test_missing_explanation_still_keeps_its_slot(self):
        self.terminal()
        # Three design/generation rows leave two required exits; a fourth
        # design cannot fit, even before trying the result-followup category.
        self.charge('TEST_ONLY_a','design');self.charge('TEST_ONLY_b','design');self.charge('TEST_ONLY_c','generation')
        with self.assertRaisesRegex(RuntimeError,'protects'):
            self.charge('TEST_ONLY_d','design')
        result=asyncio.run(self.flow.ask('TEST_ONLY 复核已有结果'))
        self.assertFalse(result['reopened']);self.assertEqual(self.budget.usage()['requests'],4)
        with self.assertRaisesRegex(RuntimeError,'protects'):
            asyncio.run(self.flow.ask('TEST_ONLY 仍须预留explanation'))

    def test_unknown_and_inflight_still_block_before_fixture_transport(self):
        self.terminal()
        self.budget.reserve_http('TEST_ONLY_unknown','h','design',self.budget.authority['model_controls']);self.budget.launch_http('TEST_ONLY_unknown',fixture=True);self.budget.finish_http('TEST_ONLY_unknown',uncertain=True)
        with self.assertRaisesRegex(RuntimeError,'frozen'):
            asyncio.run(self.flow.ask('TEST_ONLY 不绕UNKNOWN'))
        self.assertEqual(self.sent,[])

    def test_inflight_still_blocks_and_exact_stage_prefix_only(self):
        self.terminal();self.budget.reserve_http('TEST_ONLY_inflight','h','design',self.budget.authority['model_controls'])
        with self.assertRaisesRegex(RuntimeError,'reserved or in flight'):
            asyncio.run(self.flow.ask('TEST_ONLY 不绕在途'))
        self.assertEqual(self.sent,[])
        self.assertEqual(CaseRuntime._category(None,'case_ask_8_result_followup_0_reopen'),'consultation')
        self.assertNotEqual(CaseRuntime._category(None,'xcase_result_followup_0_plain'),'reopen')

    def test_terminal_failure_and_cancelled_identity_may_review_but_foreign_or_inflight_cannot(self):
        base=dict(project_id='TEST_ONLY_project',task_id='TEST_ONLY_task',version_id='v000001',state='failed')
        ctx=dict(request_id='TEST_ONLY_r',project_id=base['project_id'],session_id='TEST_ONLY_session')
        flow=case_taskflow.TaskFlow.__new__(case_taskflow.TaskFlow);flow.ctx=ctx
        snapshot=dict(base,record_kind='ARTIFACT_EXECUTION_SNAPSHOT');task=dict(base,session_id=ctx['session_id'])
        flow.open=lambda **_:SimpleNamespace(store=SimpleNamespace(task=lambda _:task))
        binding=dict(request_id=ctx['request_id'],project_id=ctx['project_id'],latest_request_execution=dict(base))
        for state in ('succeeded','failed','cancelled'):
            task['state']=snapshot['state']=binding['latest_request_execution']['state']=state
            self.assertIsNotNone(flow._result_followup_evidence(binding,snapshot))
        for state in ('queued','running','cancel_requested','uncertain','UNKNOWN'):
            task['state']=snapshot['state']=binding['latest_request_execution']['state']=state
            self.assertIsNone(flow._result_followup_evidence(binding,snapshot))
        task['state']=snapshot['state']=binding['latest_request_execution']['state']='failed'
        for field in ('project_id','task_id','version_id','session_id'):
            old=task[field];task[field]='foreign';self.assertIsNone(flow._result_followup_evidence(binding,snapshot));task[field]=old
        flow.ctx=dict(ctx,access_mode='RESULT_REVIEW_ONLY');self.assertIsNone(flow._result_followup_evidence(binding,snapshot))

    def test_expired_closed_and_money_or_token_limits_remain_effective(self):
        self.terminal();self.four_rows()
        # Mutations below touch only this test's private SQLite. No real
        # authorization/ledger is edited or refreshed by this maintenance.
        with connection(self.budget.path) as db:db.execute("INSERT INTO meta VALUES('frozen','TEST_ONLY CLOSED_RETIRED')")
        with self.assertRaisesRegex(RuntimeError,'frozen'):
            asyncio.run(self.flow.ask('TEST_ONLY closed'))
        with connection(self.budget.path) as db:
            db.execute("DELETE FROM meta WHERE key='frozen'")
            db.execute("UPDATE requests SET actual_cost=35 WHERE request_id='TEST_ONLY_explain'")
        with self.assertRaisesRegex(RuntimeError,'quota'):
            self.budget.reserve_http('case_result_followup_1_TEST_ONLY_cost','h','reopen',self.budget.authority['model_controls'])
        with connection(self.budget.path) as db:
            db.execute("UPDATE requests SET actual_cost=.001,actual_tokens=5000000 WHERE request_id='TEST_ONLY_explain'")
        with self.assertRaisesRegex(RuntimeError,'quota'):
            self.budget.reserve_http('case_result_followup_1_TEST_ONLY_tokens','h','reopen',self.budget.authority['model_controls'])
        future=datetime.now(timezone.utc)+timedelta(hours=2)
        with patch('week13.src.case_budget.datetime') as clock:
            clock.now.return_value=future;clock.fromisoformat.side_effect=datetime.fromisoformat
            with self.assertRaisesRegex(RuntimeError,'interval is not active'):
                self.budget.reserve_http('case_result_followup_1_TEST_ONLY_expired','h','reopen',self.budget.authority['model_controls'])
        self.assertEqual(self.sent,[])

if __name__=='__main__':unittest.main()
