"""Narrow numerical-continuation contracts; no transport/compiler/model edits."""
from copy import deepcopy
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock,patch

from week13.src import case_contact_family as family,case_contact_numerics as numeric
from week13.src.case_budget import CaseBudget,connection
from week13.src.case_paths import ROOT,sha_file,read_json
from week13.src.case_requests import load_request
from week13.src.case_taskflow import TaskFlow,baseline_completion_status,baseline_sysml_available,baseline_sysml_for_context,GEN_TOOL,GEN_SPEC_FIELDS

OUT=ROOT/'results/contact-numerics-20261022-01'


class NumericContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx=load_request(numeric.package()['request_ids'][0]);cls.ref=numeric.reference(cls.ctx);cls.source=cls.ref['modelica_source']

    def test_actual_reference_and_sidecar_reused_without_v0_edit(self):
        self.assertEqual(family.source_sha(self.source),numeric.REFERENCE_SHA)
        before=deepcopy(self.ctx['baseline_snapshot'])
        self.assertTrue(baseline_sysml_available(self.ctx));self.assertTrue(baseline_sysml_for_context(self.ctx))
        self.assertEqual(baseline_completion_status(self.ctx)['verification_basis'],'TRUSTED_COMPLETION_REFERENCE')
        self.assertEqual(self.ctx['baseline_snapshot'],before);self.assertFalse(before['frozen_spec']['artifact'].get('sysml_source'))

    def test_reference_does_not_enter_higher_level_contact_overlay(self):
        with patch('week13.src.case_contact_review.with_review',side_effect=AssertionError('lower trust binding entered higher current review')):
            actual=numeric.reference(self.ctx)
        self.assertEqual(actual['modelica_source_sha256'],numeric.REFERENCE_SHA)
        self.assertEqual(family.source_sha(actual['modelica_source']),numeric.REFERENCE_SHA)

    def test_real_registered_new_and_old_source_coexist(self):
        path=family.REGISTRY_ROOT/(self.ctx['runtime_authority']['sha256']+'.json')
        if not path.exists():self.skipTest('actual new product source is not yet registered')
        before=sha_file(path);new=family._read_registration(path)
        supported_old=family.support_for_reviewed_source(self.source)
        supported_new=family.support_for_reviewed_source(new['reviewed_modelica_source'])
        self.assertEqual(sha_file(path),before)
        self.assertEqual(supported_old['reviewed_modelica_source_sha256'],numeric.REFERENCE_SHA)
        self.assertEqual(supported_new['reviewed_modelica_source_sha256'],new['modelica_source_sha256'])
        self.assertNotEqual(supported_old['approved_package_sha256'],supported_new['approved_package_sha256'])
        self.assertEqual(supported_new['contact_domain']['reference_source_sha256'],numeric.REFERENCE_SHA)

    def test_generation_tool_requires_full_dual_source_contract(self):
        schema=GEN_TOOL['function']['parameters']['properties']['spec']
        self.assertEqual(set(schema['required']),set(GEN_SPEC_FIELDS));self.assertEqual(len(GEN_SPEC_FIELDS),11)
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(set(schema['properties']['artifact']['required']),{'model_name','modelica_source','sysml_source'})
        for key in schema['properties']['artifact']['required']:
            self.assertEqual(schema['properties']['artifact']['properties'][key]['minLength'],1)

    def test_ftc_only_lock_and_domain(self):
        # Purely syntactic fixture, never a candidate or a numerical solution.
        source=self.source.replace('Ft_budget[i]*FnC[i, b]','(Ft_budget[i])*FnC[i, b]')
        self.assertNotEqual(source,self.source)
        domain=numeric.verify_numerical_delta(self.ctx,source)
        self.assertEqual(domain['schema'],'v12-reviewed-contact-domain-1');self.assertTrue(domain['geometry_unchanged'])
        self.assertEqual(domain['reference_source_sha256'],numeric.REFERENCE_SHA)
        for name,value in [('stepH','0.18'),('stepD','0.26')]:
            import re
            source=re.sub(r'(\bparameter\s+Real\s+'+name+r'\s*=\s*)[^;]+',lambda m:m[1]+value,source)
        numeric.verify_numerical_delta(self.ctx,source)

    def test_non_ftc_equation_and_setting_changes_rejected(self):
        for old,new in [('FnC[i, b] =','FnC[i, b] = 0 +'),('Tolerance=1e-2','Tolerance=1e-4'),('4.0e4','4.1e4')]:
            changed=self.source.replace(old,new)
            self.assertNotEqual(changed,self.source)
            with self.subTest(old=old),self.assertRaises(ValueError):numeric.verify_numerical_delta(self.ctx,changed)

    def test_no_effective_allocation_change_rejected(self):
        with self.assertRaisesRegex(ValueError,'must change'):numeric.verify_numerical_delta(self.ctx,self.source)

    def test_actual_unreviewed_source_does_not_self_register(self):
        source=self.source.replace('Ft_budget[i]*FnC[i, b]','(Ft_budget[i])*FnC[i, b]')
        spec=deepcopy(self.ref['frozen_spec']);spec['artifact']['modelica_source']=source
        with patch.object(family,'reviewed_family',return_value=None),self.assertRaises(family.ContactSourceReviewRequired):family.check_contact_source(spec,self.ctx)

    def test_old_registered_source_stays_readable(self):
        support=family.support_for_reviewed_source(self.source)
        self.assertEqual(support['reviewed_modelica_source_sha256'],numeric.REFERENCE_SHA)
        self.assertEqual(support['contact_domain']['schema'],'v12-reviewed-contact-domain-1')
        self.assertNotEqual(support['approved_package_sha256'],numeric.PACKAGE_SHA)

    def test_multiple_reviews_select_by_actual_source_identity(self):
        new=self.source.replace('Ft_budget[i]*FnC[i, b]','(Ft_budget[i])*FnC[i, b]')
        old=dict(approved_package_sha256=family.PACKAGE_SHA,modelica_source_sha256=numeric.REFERENCE_SHA,family_shape_sha256=family._source_identity(self.source))
        later=dict(approved_package_sha256=numeric.PACKAGE_SHA,modelica_source_sha256=family.source_sha(new),family_shape_sha256=family._source_identity(new),contact_domain=numeric.verify_numerical_delta(self.ctx,new))
        registry=Mock();registry.glob.return_value=[Path('a'),Path('b')]
        with patch.object(family,'REGISTRY_ROOT',registry),patch.object(family,'_read_registration',side_effect=lambda p:old if p.name=='a' else later):
            self.assertEqual(family.support_for_reviewed_source(self.source)['approved_package_sha256'],family.PACKAGE_SHA)
            self.assertEqual(family.support_for_reviewed_source(new)['approved_package_sha256'],numeric.PACKAGE_SHA)

    def test_revision_binding_is_explicit_and_one_set_counter(self):
        with tempfile.TemporaryDirectory(prefix='revision-guard-',dir=OUT) as temp:
            ctx=deepcopy(self.ctx);ctx['root']=temp
            self.assertEqual(numeric.source_revision(ctx),0)
            with self.assertRaises(PermissionError):numeric.source_revision(ctx,require=True)
            budget=CaseBudget.__new__(CaseBudget);budget.path=Path(temp)/'ledger.sqlite';budget.task_context=None;budget._require_time=Mock()
            budget.authority=dict(caps={},repair_allocation=numeric.package()['repair_allocation'])
            with connection(budget.path) as db:
                db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)');db.execute('CREATE TABLE counters(kind TEXT,event_id TEXT,detail_json TEXT,recorded_at TEXT,PRIMARY KEY(kind,event_id))')
            budget.consume('source_revision','first');budget.consume('source_revision','first')
            with self.assertRaisesRegex(RuntimeError,'exhausted'):budget.consume('source_revision','second')

    def test_prompt_context_binds_failed_source_not_v0_generation(self):
        context=family.planning_context(self.ctx)
        self.assertTrue(context['numerical_only']);self.assertEqual(context['repair_reference']['modelica_source'],self.source)
        self.assertEqual(context['approved_package_sha256'],numeric.PACKAGE_SHA)

    def test_two_repairs_reserved_until_first_valid_source(self):
        with tempfile.TemporaryDirectory(prefix='repair-guard-',dir=OUT) as temp:
            budget=CaseBudget.__new__(CaseBudget);budget.path=Path(temp)/'ledger.sqlite';budget.task_context=None;budget._require_time=Mock()
            budget.authority=dict(caps=dict(software_repair_rounds=4),repair_allocation=numeric.package()['repair_allocation'])
            with connection(budget.path) as db:
                db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)');db.execute('CREATE TABLE counters(kind TEXT,event_id TEXT,detail_json TEXT,recorded_at TEXT,PRIMARY KEY(kind,event_id))')
            budget.consume('software_repair','a');budget.consume('software_repair','b')
            with self.assertRaisesRegex(RuntimeError,'reserves at least two'):budget.consume('software_repair','c')
            budget.mark_valid_generated_source('TEST_ONLY_valid_reviewed_digest')
            budget.consume('software_repair','c');budget.consume('software_repair','d')
            with self.assertRaisesRegex(RuntimeError,'exhausted'):budget.consume('software_repair','e')

    def test_saved_explanation_overlay_does_not_write_old_partial(self):
        ctx=load_request(numeric.package()['repair_reference']['request_id']);path=Path(ctx['root'])/'turns/explain_0/response.json';before=sha_file(path)
        flow=TaskFlow(ctx);state=flow.state()
        partial=flow._terminal_explanation_overlay(dict(stage='EXECUTION_TERMINAL_EXPLANATION_INCOMPLETE',execution=state['latest_request_execution'],state=state))
        self.assertEqual(sha_file(path),before)
        self.assertEqual(partial['explanation']['status'],'COMPLETE_SAVED_PRODUCT_RESPONSE')
        self.assertFalse(partial['explanation']['pipeline_completion_claimed'])
        self.assertEqual(partial['execution']['tool_status'],'TOOL_FAIL')


if __name__=='__main__':unittest.main()
