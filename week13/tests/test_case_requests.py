"""Isolated request/budget fixtures. No real HTTP, MCP, OMC or project data."""
import copy
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from week13.src import case_requests as requests
from week13.src import case_paths as paths
from week13.src.case_runtime import CaseRuntime
from week13.src.case_budget import CaseBudget


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'week13';self.root.mkdir()
        self.project=self.root/'runs/shared-project'
        self.controls=json.loads((paths.ROOT/'configs/case_runtime.json').read_text(encoding='utf8'))
        (self.root/'configs').mkdir();(self.root/'configs/case_runtime.json').write_text(json.dumps(self.controls),encoding='utf8')
        for obj,name,value in ((requests,'ROOT',self.root),(requests,'LEGACY_PROJECT_ROOT',self.project),(requests,'REQUESTS_ROOT',self.project/'requests'),(paths,'ROOT',self.root)):
            p=patch.object(obj,name,value);p.start();self.addCleanup(p.stop)
        self.ref=dict(project_id='fixture_project',task_id='fixture_task_v2',version_id='v000002',spec_digest='fixture_spec',source_sha256='fixture_source',manifest_sha256='fixture_manifest')
        # Confirmation now rechecks ordinary scope and current source facts.
        # Keep this lifecycle fixture executable under the same product guard.
        source='model Fixture parameter Real m=90; parameter Real I_pitch=20; parameter Real T_main_max=60; parameter Real T_nose_max=25; end Fixture;'
        self.snapshot=dict(self.ref,frozen_spec={'fixture':True,'mass':90,'artifact':dict(modelica_source=source)})
        self.caps=dict(http_requests=4,product_cny=25,total_tokens=4000000,total_output_tokens=4*393216,compile=2,simulation=2,candidates=2,software_repair_rounds=1,parameter_revision_rounds=1,observation_extensions=0)
        self.ctx=self.create('fixture_a')

    def create(self,rid):
        return requests.create_request(rid,'fixture_project','v000002','基于实际v2继续提出一项新需求（隔离fixture）',baseline_ref=self.ref,baseline_snapshot=self.snapshot,project_root=self.project,db_path=self.project/'projects.sqlite',sources=[dict(id='fixture_source',text='新需求')])

    def budget(self,rid='fixture_a',budget='fixture_budget',reuse=False):
        args={} if reuse else dict(caps=self.caps,hard_deadline_utc=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat())
        return requests.authorize_budget(rid,budget,'CONFIRM',test_only=True,reuse=reuse,**args)

    def packet(self):
        params=dict(m=95,I_pitch=20,T_main_max=60,T_nose_max=25)
        source=self.ctx['sources'][0]
        return dict(intent='CHANGE',support='SUPPORTED',missing=[],unsupported=[],requirements=[dict(text='新需求',source_id=source['id'],source_text=source['text'])],design=dict(summary='fixture product proposal',assumptions=['fixture'],parameters=[dict(name=k,value=v) for k,v in params.items()]),proposed_parameters=params,defaults=[],unknowns=[],rationale='fixture evidence',scope=dict(change_kind='center_of_mass_point_load',mass_location='original_center_of_mass',inertia_assumption='ideal_point_mass',frozen='geometry_contact_control_terrain_task'))

    def confirm(self):
        ctx=requests.save_plan('fixture_a',self.packet())
        return requests.confirm_request('fixture_a',ctx['displayed_digest'],'CONFIRM')

    def test_context_keeps_real_v2_and_same_project_without_permission(self):
        second=self.create('fixture_b')
        self.assertEqual(second['db_path'],self.ctx['db_path']);self.assertNotEqual(second['session_id'],self.ctx['session_id'])
        self.assertEqual(second['baseline_snapshot']['frozen_spec']['mass'],90)
        with self.assertRaises(PermissionError):requests.require_planning('fixture_b')
        with self.assertRaises(PermissionError):requests.require_execution('fixture_b')
        self.assertFalse((self.project/'budget.sqlite').exists())

    def test_baseline_snapshot_and_input_cannot_be_replaced(self):
        path=Path(self.ctx['root'])/'baseline-snapshot.json'
        changed=copy.deepcopy(self.snapshot);changed['frozen_spec']['mass']=85
        path.write_text(json.dumps(changed),encoding='utf8')
        with self.assertRaisesRegex(ValueError,'snapshot'):requests.load_request('fixture_a')

    def test_confirmation_rejects_incomplete_question_and_negative_words(self):
        for intent,support in [('QUESTION','SUPPORTED'),('CHANGE','NEEDS_INPUT'),('CHANGE','UNSUPPORTED')]:
            packet=self.packet();packet.update(intent=intent,support=support)
            ctx=requests.save_plan('fixture_a',packet)
            with self.assertRaises(PermissionError):requests.confirm_request('fixture_a',ctx['displayed_digest'],'CONFIRM')
        ctx=requests.save_plan('fixture_a',self.packet())
        with self.assertRaises(PermissionError):requests.confirm_request('fixture_a',ctx['displayed_digest'],'不批准')
        packet=self.packet();packet['design']={};ctx=requests.save_plan('fixture_a',packet)
        with self.assertRaises(PermissionError):requests.confirm_request('fixture_a',ctx['displayed_digest'],'CONFIRM')

    def test_confirm_is_not_budget_and_budget_is_not_plan_confirmation(self):
        self.confirm()
        with self.assertRaises(PermissionError):requests.require_execution('fixture_a')
        ctx=self.budget();requests.require_execution('fixture_a')
        self.assertTrue(Path(ctx['execution_context_path']).is_file())
        packet=self.packet();packet['rationale']='new material proposal';requests.save_plan('fixture_a',packet)
        with self.assertRaises(PermissionError):requests.require_execution('fixture_a')

    def test_explicit_budget_reuse_retains_one_ledger_cumulative_caps(self):
        first=self.budget();self.create('fixture_b');second=self.budget('fixture_b',reuse=True)
        self.assertEqual(first['runtime_authority']['ledger_path'],second['runtime_authority']['ledger_path'])
        b1=CaseBudget(task_context=first);b1.consume('candidate','fixture_candidate_a')
        b2=CaseBudget(task_context=second);b2.consume('candidate','fixture_candidate_b')
        self.assertEqual(b1.usage()['counters']['candidate'],2)
        with self.assertRaises(RuntimeError):b2.consume('candidate','fixture_overcap')
        with self.assertRaises(ValueError):requests.authorize_budget('fixture_b','fixture_budget','CONFIRM',reuse=True,caps=self.caps,test_only=True)
        with self.assertRaises(PermissionError):requests.authorize_budget('fixture_b','fresh_budget','CONFIRM',caps=self.caps,hard_deadline_utc=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),test_only=True)
        self.assertFalse((self.root/'runs/request-budgets/fresh_budget').exists())

    def test_dynamic_runtime_after_old_paths_import_writes_only_request_root(self):
        ctx=self.budget();sent=[]
        def transport(payload,key,timeout):
            sent.append(payload)
            return dict(id='fixture_reply',model='deepseek-flash',choices=[dict(message=dict(content='fixture',reasoning_content='fixture protocol'),finish_reason='stop')],usage=dict(prompt_tokens=20,completion_tokens=10,total_tokens=30))
        rt=CaseRuntime(Path(ctx['root'])/'runtime.json',task_context=ctx,transport=transport)
        rt.complete([dict(role='user',content='fixture request')],[],'fixture_design')
        self.assertEqual(rt.run,Path(ctx['root']));self.assertEqual(rt.usage()['requests'],1)
        receipts=list((Path(ctx['root'])/'http').glob('*/request.json'));self.assertEqual(len(receipts),1)
        self.assertTrue(json.loads(receipts[0].read_text())['outbound_settings_observed'])
        self.assertEqual(sent[0]['max_tokens'],393216)
        with self.assertRaises(PermissionError):CaseRuntime(Path(ctx['root'])/'runtime.json',task_context=ctx)

    def test_no_budget_can_be_created_by_new_request_or_negative_approval(self):
        with self.assertRaises(PermissionError):requests.authorize_budget('fixture_a','fixture_budget','不批准',caps=self.caps,hard_deadline_utc='2099-01-01T00:00:00Z',test_only=True)
        self.assertFalse((self.root/'runs/request-budgets').exists())

    def test_process_requires_approved_complete_plan_even_with_budget(self):
        ctx=self.budget();budget=CaseBudget(task_context=ctx)
        with self.assertRaises(PermissionError):budget.reserve_process('compile',{'fixture':True})
        self.confirm();attempt=budget.reserve_process('compile',{'fixture':True})
        self.assertEqual(budget.usage()['processes'],{})
        budget.finish_process(attempt,dict(reason='fixture no Popen'))
        self.assertEqual(budget.usage()['processes'],{})

    def test_execution_context_loads_actual_inherited_schema_and_assumptions(self):
        ctx=self.budget();self.confirm();ctx=requests.require_execution('fixture_a')
        from week13.src.inherited.model_artifact import ExecutionContext
        actual=ExecutionContext.load(ctx['execution_context_path'])
        self.assertEqual(actual.mode,'TEST_ONLY');self.assertEqual(actual.max_tasks,2)
        binding=json.loads((Path(ctx['root'])/'execution-binding.json').read_text(encoding='utf8'))
        self.assertEqual(binding['assumptions'],['fixture'])
        self.assertEqual(binding['request_digest'],ctx['request_digest'])

    def test_unknown_request_freezes_shared_budget_in_another_context(self):
        ctx=self.budget()
        def interrupted(*args):raise TimeoutError('isolated fixture')
        rt=CaseRuntime(Path(ctx['root'])/'runtime.json',task_context=ctx,transport=interrupted)
        with self.assertRaisesRegex(RuntimeError,'uncertain'):rt.complete([dict(role='user',content='fixture')],[],'fixture_design_a')
        self.create('fixture_b');second=self.budget('fixture_b',reuse=True)
        other=CaseRuntime(Path(second['root'])/'runtime.json',task_context=second,transport=interrupted)
        with self.assertRaisesRegex(RuntimeError,'frozen'):other.complete([dict(role='user',content='fixture')],[],'fixture_design_b')
        self.assertEqual(other.usage()['requests'],1)
        self.assertEqual(other.usage()['actual_cost_cny'],'UNKNOWN')
        self.assertAlmostEqual(other.usage()['cost_cny_reserved_or_actual'],5.24288)

    def test_delegated_confirmation_records_prior_basis_without_claiming_human_saw_plan(self):
        ctx=requests.save_plan('fixture_a',self.packet())
        with self.assertRaises(PermissionError):requests.confirm_request('fixture_a',ctx['displayed_digest'],'CONFIRM',human_reviewed_displayed_plan=False)
        receipt=requests.confirm_request('fixture_a',ctx['displayed_digest'],'CONFIRM',approval_basis='FIXTURE_ONLY: previously displayed finite scope approval',human_reviewed_displayed_plan=False)
        self.assertFalse(receipt['human_reviewed_displayed_plan'])
        self.assertIn('FIXTURE_ONLY',receipt['approval_basis'])
        self.assertFalse(receipt['budget_authorization_created'])

    def test_test_only_budget_blocks_direct_real_http_launch(self):
        ctx=self.budget();budget=CaseBudget(task_context=ctx)
        budget.reserve_http('fixture_http','fixture_hash','design',requests.authority_for(ctx)['model_controls'])
        with self.assertRaises(PermissionError):budget.launch_http('fixture_http')
        self.assertEqual(budget.usage()['requests'],0)
        budget.finish_http('fixture_http',error='fixture never dispatched')

    def test_fractional_or_boolean_count_caps_rejected_before_any_approval_write(self):
        for invalid in (1.5,True):
            caps=dict(self.caps,candidates=invalid)
            with self.assertRaises(ValueError):
                requests.authorize_budget('fixture_a','invalid_budget','CONFIRM',caps=caps,hard_deadline_utc=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),test_only=True)
            self.assertFalse((self.root/'runs/request-budgets/invalid_budget').exists())

    def test_complete_bad_json_saves_known_rejected_reply_for_bounded_repair(self):
        ctx=self.budget()
        def transport(*args):
            return dict(id='fixture_rejected',model='deepseek-flash',choices=[dict(message=dict(content='',reasoning_content='fixture protocol',tool_calls=[dict(id='fixture_call',type='function',function=dict(name='proposal_prepare',arguments='{"spec":'))]),finish_reason='tool_calls')],usage=dict(prompt_tokens=20,completion_tokens=10,total_tokens=30))
        rt=CaseRuntime(Path(ctx['root'])/'runtime.json',task_context=ctx,transport=transport)
        tool=dict(type='function',function=dict(name='proposal_prepare',description='TEST_ONLY',parameters=dict(type='object',properties=dict(spec=dict(type='object',properties={},additionalProperties=False)),required=['spec'],additionalProperties=False)))
        with self.assertRaises(json.JSONDecodeError):rt.complete([dict(role='user',content='fixture')],[tool],'fixture_generate')
        self.assertEqual(rt.budget.request('fixture_generate')['status'],'rejected')
        path=Path(ctx['root'])/'http'/paths.sha_value('fixture_generate')/'rejected-response.json'
        saved=json.loads(path.read_text(encoding='utf8'))
        self.assertTrue(saved['usage_known']);self.assertTrue(saved['fixture_transport'])
        self.assertEqual(saved['response']['choices'][0]['message']['tool_calls'][0]['function']['arguments'],'{"spec":')

    def test_request_and_budget_dotdot_ids_rejected_before_directory_creation(self):
        with self.assertRaisesRegex(ValueError,'invalid request_id'):self.create('fixture..request')
        self.assertFalse((requests.REQUESTS_ROOT/'fixture..request').exists())
        with self.assertRaisesRegex(ValueError,'invalid budget_id'):
            requests.authorize_budget('fixture_a','fixture..budget','CONFIRM',caps=self.caps,hard_deadline_utc=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),test_only=True)
        self.assertFalse((self.root/'runs/request-budgets/fixture..budget').exists())


if __name__=='__main__':unittest.main()
