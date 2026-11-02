"""Isolated contact-adapter contracts; no paid calls, MCP, OMC or vehicle run."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch,AsyncMock

from week13.src import case_contact_family as contact,case_scope as scope
from week13.src.case_paths import read_json,save_json,sha_file,sha_value
from week13.src.case_taskflow import TaskFlow,baseline_sysml_for_context
from week13.tests.test_case_stairs import baseline,packet,context,candidate,sysml_for,source_with,delta

RESULTS=Path(__file__).resolve().parents[1]/'results/contact-physics-20261022-01'


class ContactAdapterContracts(unittest.TestCase):
    def setUp(self):
        RESULTS.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='adapter-fixture-',dir=RESULTS)
        self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.base=baseline();self.plan=packet(self.base,.15,.28)
        self.plan['scope']=deepcopy(contact.CONTACT_SCOPE_PACKET)
        self.ctx=context(self.base,self.plan);self.ctx.update(root=str(self.root),project_id=contact.PROJECT_ID,request_id='contact_fixture')
        self.ctx['baseline_ref'].update(project_id=contact.PROJECT_ID,source_sha256=contact.BASELINE_SOURCE_SHA)
        self.ctx['runtime_authority']=dict(sha256='TEST_ONLY_authority')
        self.authority=read_json(RESULTS/'execution-approval.json')
        self.authority.update(permissions=dict(execution=True),contact_repair_package=dict(path=str(contact.PACKAGE_PATH),sha256=contact.PACKAGE_SHA))
        self.authpatch=patch('week13.src.case_requests.authority_for',return_value=self.authority)
        self.authpatch.start();self.addCleanup(self.authpatch.stop)
        self.registry=self.root/'registration'
        p=patch.object(contact,'REGISTRY_ROOT',self.registry);p.start();self.addCleanup(p.stop)

    def contact_spec(self,height=.15,depth=.28):
        spec=deepcopy(self.base)
        source=source_with(spec['artifact']['modelica_source'],stepH=height,stepD=depth)
        # TEST_ONLY neutral algebraic rewrite to exercise a different equation
        # family without introducing a development-authored contact algorithm.
        spec['artifact']['modelica_source']=source.replace('c_ground * vz','c_ground * (vz + 0)')
        spec.update(baseline_ref=self.ctx['baseline_ref'],sources=self.ctx['sources'],requirements=self.plan['requirements'],design=self.plan['design'])
        spec['artifact']['sysml_source']=sysml_for(spec)
        return spec

    def test_contact_plan_requires_the_exact_trusted_package(self):
        with self.assertRaisesRegex(PermissionError,'bound trusted'):scope.validate_request_plan(self.plan,self.base,self.ctx['sources'])
        self.authority['contact_repair_package']['sha256']='0'*64
        with self.assertRaisesRegex(PermissionError,'exact approved'):scope.validate_request_plan(self.plan,self.base,self.ctx['sources'],task_context=self.ctx)

    def test_three_approved_scenarios_freeze_four_vehicle_parameters(self):
        for h,d in contact.SCENARIOS:
            p=deepcopy(self.plan);p['proposed_parameters'].update(stepH=h,stepD=d)
            for row in p['design']['parameters']:
                if row['name'] in ('stepH','stepD'):row['value']=p['proposed_parameters'][row['name']]
            proof=scope.validate_request_plan(p,self.base,self.ctx['sources'],task_context=self.ctx)
            self.assertEqual(proof['direct_parameters'],['stepH','stepD'])
        p=deepcopy(self.plan);p['proposed_parameters']['stepH']=.22
        with self.assertRaisesRegex(scope.UnsupportedRequestDesign,'three approved'):scope.validate_request_plan(p,self.base,self.ctx['sources'],task_context=self.ctx)
        p=deepcopy(self.plan);p['proposed_parameters']['m']=105
        with self.assertRaisesRegex(scope.UnsupportedRequestDesign,'mass/inertia'):scope.validate_request_plan(p,self.base,self.ctx['sources'],task_context=self.ctx)

    def test_ordinary_scope_does_not_receive_contact_source_permissions(self):
        old=packet(self.base,.15,.28);ctx=context(self.base,old);spec=candidate(self.base,ctx)
        spec['artifact']['modelica_source']=spec['artifact']['modelica_source'].replace('c_ground * vz','c_ground * (vz + 0)')
        with self.assertRaisesRegex(ValueError,'frozen physics'):delta(spec,ctx)

    def test_unregistered_source_requires_review_without_self_registration(self):
        spec=self.contact_spec()
        with self.assertRaises(contact.ContactSourceReviewRequired) as raised:contact.check_contact_source(spec,self.ctx)
        self.assertEqual(raised.exception.spec,spec)
        self.assertIsNone(contact.support_for_reviewed_source(spec['artifact']['modelica_source']))
        self.assertFalse(self.registry.exists())

    def test_approved_package_does_not_unlock_frozen_parameters(self):
        for name,value in [('R_main',.2),('k_ground',90000),('T_main_max',61)]:
            spec=self.contact_spec();spec['artifact']['modelica_source']=source_with(spec['artifact']['modelica_source'],**{name:value})
            with self.subTest(name=name),self.assertRaisesRegex(scope.UnsupportedRequestDesign,'frozen parameter'):contact.check_contact_source(spec,self.ctx)

    def test_reviewed_shape_allows_only_stair_values_not_settings_or_algorithm(self):
        spec=self.contact_spec();source=spec['artifact']['modelica_source']
        family=dict(family_shape_sha256=contact._source_identity(source),request_id='contact_fixture',approved_package_sha256=contact.PACKAGE_SHA)
        with patch.object(contact,'reviewed_family',return_value=family),patch('week13.src.case_requests.request_root',return_value=self.root):
            for h,d in contact.SCENARIOS:
                changed=deepcopy(spec);changed['artifact']['modelica_source']=source_with(source,stepH=h,stepD=d)
                self.assertIn('family_shape_sha256',contact.check_contact_source(changed,self.ctx))
            for changed_source in (source.replace('Tolerance=1e-2','Tolerance=1e-4'),source.replace('(vz + 0)','(vz + 1)'),source.replace('target_speed = 0.4','target_speed = 0.5')):
                self.assertNotEqual(changed_source,source)
                changed=deepcopy(spec);changed['artifact']['modelica_source']=changed_source
                with self.assertRaises(scope.UnsupportedRequestDesign):contact.check_contact_source(changed,self.ctx)

    def response(self,spec,baseline_sysml=False):
        args=dict(spec=deepcopy(spec))
        if baseline_sysml:args['baseline_sysml_source']=sysml_for(self.base)
        return dict(finish_reason='tool_calls',tool_calls=[dict(type='function',function=dict(name='proposal_prepare',arguments=json.dumps(args,ensure_ascii=False)))])

    def test_pending_reply_is_source_bound_and_not_a_protocol_failure(self):
        spec=self.contact_spec();response=self.response(spec,True)
        save_json(self.root/'turns/generate/response.json',response,immutable=True)
        receipt=contact.save_pending(self.ctx,response,'generate',spec,None)
        self.assertEqual(receipt['status'],'REVIEW_REQUIRED');self.assertFalse(receipt['software_repair_added'])
        self.assertFalse((self.root/'failures').exists());self.assertFalse((self.root/'generation.json').exists())
        pending,again=contact.pending_response(self.ctx);self.assertEqual(again,response)
        pending['spec']['artifact']['modelica_source']+='\n// TEST_ONLY tamper'
        pending['spec_digest']=sha_value(pending['spec']);save_json(self.root/'contact-source-review-pending.json',pending)
        with self.assertRaisesRegex(ValueError,'original product output'):contact.pending_response(self.ctx)

    def test_pending_resume_uses_saved_response_and_zero_http(self):
        spec=self.contact_spec();response=self.response(spec,True)
        save_json(self.root/'turns/generate/response.json',response,immutable=True)
        contact.save_pending(self.ctx,response,'generate',spec,None)
        flow=TaskFlow(self.ctx);flow.refresh=lambda:self.ctx
        flow.call=AsyncMock(side_effect=AssertionError('must not use HTTP'))
        flow._validate_generation=lambda r,s:(spec,{'status':'TEST_ONLY'}, {'ready':True,'spec_digest':sha_value(spec)},None)
        flow._prepare_generation=AsyncMock(return_value={'status':'ISOLATED_PREPARED'})
        with patch('week13.src.case_requests.require_execution',return_value=self.ctx):result=asyncio.run(flow.generate())
        self.assertEqual(result['status'],'ISOLATED_PREPARED');flow.call.assert_not_called();flow._prepare_generation.assert_awaited_once()

    def test_ordinary_run_returns_review_status_before_execute(self):
        self.ctx['plan']=self.plan;flow=TaskFlow(self.ctx);flow.refresh=lambda:self.ctx
        flow._repaired_pipeline=lambda:None;flow.terminal_readback=lambda:None
        flow.bounded=AsyncMock(side_effect=[{'status':'TEST_ONLY_PLAN'},{'status':'REVIEW_REQUIRED'}])
        flow.execute=AsyncMock(side_effect=AssertionError('must not execute before source review'))
        with patch('week13.src.case_requests.require_execution',return_value=self.ctx):result=asyncio.run(flow.run())
        self.assertEqual(result['stage'],'CONTACT_SOURCE_REVIEW_REQUIRED');flow.execute.assert_not_called()

    def test_missing_baseline_sysml_is_completed_as_a_sidecar(self):
        spec=self.contact_spec();response=self.response(spec,True);flow=TaskFlow(self.ctx)
        svc=SimpleNamespace(normalize_candidate=lambda value:(value,{}))
        flow.open=lambda **kwargs:svc
        save_json(self.root/'turns/generate/response.json',response,immutable=True)
        with patch('week13.src.case_taskflow.check_delta',side_effect=contact.ContactSourceReviewRequired(spec)):
            with self.assertRaises(contact.ContactSourceReviewRequired) as raised:flow._validate_generation(response,'generate')
        completion=raised.exception.completion
        self.assertTrue(completion['original_modelica_and_metadata_unchanged'])
        save_json(self.root/'baseline-sysml-completion.json',completion,immutable=True)
        self.assertEqual(baseline_sysml_for_context(self.ctx),completion['completed_baseline_spec']['artifact']['sysml_source'])
        self.assertNotIn('sysml_source',self.base['artifact'])

    def test_no_model_claim_or_wrong_hash_can_register(self):
        self.ctx['plan']=self.plan;spec=self.contact_spec();response=self.response(spec,True)
        save_json(self.root/'turns/generate/response.json',response,immutable=True)
        contact.save_pending(self.ctx,response,'generate',spec,None)
        with patch('week13.src.case_requests.require_execution',return_value=self.ctx):
            with self.assertRaisesRegex(ValueError,'exact source hash'):contact.register_source_review('contact_fixture','0'*64,'TEST_ONLY deliberate hash mismatch')
        self.assertFalse(self.registry.exists())

    def test_registry_requires_immutable_receipt_not_family_name_claim(self):
        self.registry.mkdir();save_json(self.registry/'fake.json',dict(schema='v12-contact-family-registration-1',source_review_path=str(self.root/'model-claim.json'),source_review_sha256='0'*64))
        save_json(self.root/'model-claim.json',dict(schema='v12-contact-source-review-1',channel='PRODUCT_MODEL',family='source_review_bound_stair_contact_v1'))
        with self.assertRaisesRegex(ValueError,'receipt drift'):contact.support_for_reviewed_source(self.contact_spec()['artifact']['modelica_source'])

    def register_fixture(self):
        self.ctx['plan']=self.plan;self.ctx['confirmation_path']=str(self.root/'confirmation.json')
        save_json(self.ctx['confirmation_path'],dict(TEST_ONLY='synthetic trusted confirmation'))
        spec=self.contact_spec();response=self.response(spec,True)
        save_json(self.root/'turns/generate/response.json',response,immutable=True)
        contact.save_pending(self.ctx,response,'generate',spec,None)
        reviewed=contact.ContactSourceReviewRequired(spec)
        flow=SimpleNamespace(_validate_generation=lambda *args:(_ for _ in ()).throw(reviewed))
        with patch('week13.src.case_requests.require_execution',return_value=self.ctx),patch('week13.src.case_requests.load_request',return_value=self.ctx),patch.object(TaskFlow,'load',return_value=flow):
            receipt=contact.register_source_review(self.ctx['request_id'],contact.source_sha(spec['artifact']['modelica_source']),'TEST_ONLY local exact-hash registration fixture; no physical certification')
        return spec,receipt

    def test_trusted_registration_reads_back_only_same_source_family(self):
        spec,receipt=self.register_fixture()
        self.assertFalse(receipt['formal_physical_acceptance']);self.assertFalse(receipt['product_self_approval'])
        with patch('week13.src.case_requests.load_request',return_value=self.ctx):
            proof=contact.support_for_reviewed_source(source_with(spec['artifact']['modelica_source'],stepH=.18,stepD=.26))
            self.assertEqual(proof['family'],'source-review-bound-stair-contact-v1')
            self.assertEqual(proof['parameters']['m'],85);self.assertEqual(proof['physical_acceptance'],'NOT_VERIFIED')
            self.assertIsNone(contact.support_for_reviewed_source(spec['artifact']['modelica_source'].replace('(vz + 0)','(vz + 1)')))
            self.assertIsNone(contact.support_for_reviewed_source(self.base['artifact']['modelica_source']))

    def test_registered_source_confirmation_or_receipt_drift_is_rejected(self):
        self.register_fixture()
        with patch('week13.src.case_requests.load_request',return_value=self.ctx):
            save_json(self.ctx['confirmation_path'],dict(TEST_ONLY='tampered confirmation'))
            with self.assertRaisesRegex(ValueError,'confirmation drift'):contact.reviewed_family()
        path=self.root/'contact-source-review.json';saved=read_json(path);saved['review_basis']='TEST_ONLY changed';save_json(path,saved)
        with self.assertRaisesRegex(ValueError,'receipt drift'):contact.reviewed_family()


if __name__=='__main__':unittest.main()
