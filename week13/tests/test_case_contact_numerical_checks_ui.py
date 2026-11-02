"""Local fixtures only: exact-domain, partial CSV, narration and boundary checks."""
from copy import deepcopy
import csv
import hashlib
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from week13.src.case_checks import REQUIRED,assess,source_contact_domain,CONTACT_DOMAIN_REVIEWED_SOURCE_SHA256,REVIEWED_CONTACT_FAMILY
from week13.src.case_contact_boundary import build_fixture,check_csv,extract_fragment
from week13.src.case_console_result import prepare_result
from week13.tests.test_case_contact_validity import VALUES,samples
from week13.tests.test_case_contact_physics_checks import SOURCE,write_csv
from week13.tests.test_case_console_result_headline import saved_result

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'results/contact-numerics-20261022-01'


def support(source,reviewed=None):
    sha=hashlib.sha256(source.encode('utf8')).hexdigest()
    reviewed=reviewed or sha
    domain=dict(schema='v12-reviewed-contact-domain-1',reviewed_source_sha256=reviewed,family_shape_sha256='b'*64,
                reference_source_sha256=CONTACT_DOMAIN_REVIEWED_SOURCE_SHA256,geometry_unchanged=True,
                ground_pad_extension_m=100.,corner_regularization_distance_m=1e-9)
    return dict(family=REVIEWED_CONTACT_FAMILY,modelica_source_sha256=sha,reviewed_modelica_source_sha256=reviewed,
                family_shape_sha256='b'*64,source_review_sha256='c'*64,approved_package_sha256='d'*64,contact_domain=domain)


class ExactDomainAndPartialChecks(unittest.TestCase):
    def domain(self,source=SOURCE,binding=None,rows=None):
        binding=support(source) if binding is None else binding
        with patch('week13.src.case_contact_family.support_for_reviewed_source',return_value=binding):
            return source_contact_domain(rows or samples(),VALUES,binding,actual_source_sha256=hashlib.sha256(source.encode('utf8')).hexdigest(),actual_source=source)

    def test_old_and_new_precise_review_domains_coexist(self):
        for source,review in ((SOURCE,CONTACT_DOMAIN_REVIEWED_SOURCE_SHA256),(SOURCE+' //new',None)):
            report=self.domain(source,support(source,review))
            self.assertEqual(report['status'],'PASS')
            self.assertEqual(report['binding']['contact_domain']['reference_source_sha256'],CONTACT_DOMAIN_REVIEWED_SOURCE_SHA256)

    def test_arbitrary_64_hash_without_actual_trusted_source_does_not_pass(self):
        binding=support(SOURCE)
        report=source_contact_domain(samples(),VALUES,binding,actual_source_sha256=binding['modelica_source_sha256'])
        self.assertEqual(report['status'],'UNKNOWN')

    def test_domain_changes_cannot_inherit_geometry_review(self):
        for key,value in (('schema','fake'),('reference_source_sha256','a'*64),('reviewed_source_sha256','a'*64),
                          ('family_shape_sha256','a'*64),('geometry_unchanged',False),('ground_pad_extension_m',1000.),
                          ('corner_regularization_distance_m',0.)):
            binding=support(SOURCE);binding['contact_domain'][key]=value
            self.assertEqual(self.domain(binding=binding)['status'],'UNKNOWN',key)

    def test_fabricated_support_disagrees_with_actual_registry(self):
        binding=support(SOURCE)
        with patch('week13.src.case_contact_family.support_for_reviewed_source',return_value=None):
            report=source_contact_domain(samples(),VALUES,binding,actual_source_sha256=binding['modelica_source_sha256'],actual_source=SOURCE)
        self.assertEqual(report['status'],'UNKNOWN')

    def assess_prefix(self,rows=None,registry_error=False):
        OUT.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='checker-fixture-',dir=OUT) as folder:
            prefix=[]
            for raw in rows or samples():
                row={k:0. for k in REQUIRED};row.update(raw)
                row.update(x_n=raw['x_n']+.5,x_f=raw['x_f']+.3,x_r=raw['x_r']+.1)
                prefix.append(row)
            prefix.append(dict(prefix[-1],time=.1))
            path=Path(folder)/'partial.csv';write_csv(path,prefix,('Fx_n','Fx_f','Fx_r'))
            spec=dict(artifact=dict(modelica_source=SOURCE),settings=dict(stop_time=120.,intervals=2400))
            with patch('week13.src.case_contact_family.support_for_reviewed_source',side_effect=ValueError('trusted review drift') if registry_error else None,return_value=support(SOURCE)):
                return assess(spec,path)

    def test_partial_new_contact_csv_keeps_current_limitations_and_unknown(self):
        report=self.assess_prefix()
        self.assertEqual(report['task_status'],'UNKNOWN');self.assertEqual(report['validity_status'],'UNKNOWN')
        self.assertEqual(report['completion_status'],'CSV_OBSERVATION_INCOMPLETE')
        self.assertIn('已审核源码补入',''.join(report['limitations']))
        self.assertNotIn('原模型没有完整立面',''.join(report['limitations']))
        self.assertEqual(report['observation_binding']['status'],'FAIL')

    def test_partial_or_unregistered_source_does_not_hide_center_interior(self):
        for drift in (False,True):
            report=self.assess_prefix(samples(2.13,.2),registry_error=drift)
            self.assertEqual((report['validity_status'],report['task_status'],report['completion_status']),('FAIL','UNKNOWN','INVALID_MODEL'))

    def test_near_corner_and_outside_ground_stay_unknown(self):
        for rows in (samples(2.,.3),samples(-100.,.5)):
            self.assertEqual(self.domain(rows=rows)['status'],'UNKNOWN')


class NarrationAndSysmlChecks(unittest.TestCase):
    def test_case_banner_uses_verified_completion_and_keeps_v0_history(self):
        from week13.src.case_console import Console
        OUT.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='banner-fixture-',dir=OUT) as folder:
            lines=[];ui=Console(console_root=Path(folder),output=lines.append)
            ui.ctx=dict(verified_source_facts=dict(baseline_sysml_completion=dict(status='VERIFIED')))
            ui.catalog=dict(project_label='TEST_ONLY',case_id='TEST_ONLY_case',project_id='TEST_ONLY_project',case_status='REQUIRES_SOURCE_COMPLETION',baselines=[dict(version_id='v000000')],missing=['原基准缺少SysML'])
            before=deepcopy(ui.catalog);ui.show_case()
            self.assertEqual(ui.catalog,before)
            self.assertTrue(any(line.startswith('历史导入状态：') for line in lines))
            self.assertIn('案例SysML状态：原基准可信侧证已补齐并验证；原v0历史文件仍无SysML。',lines)

    def overlay(self):
        value=saved_result(tool_status='TOOL_FAIL',chain_status='TOOL_FAIL',task_status='UNKNOWN',mission={})
        value.update(current_explanation_success=True,read_only_explanation_overlay=True,product_explanation_success=False)
        value['explanation'].update(status='COMPLETE_SAVED_PRODUCT_RESPONSE',binding=dict(version_id='vTEST',task_id='TEST_ONLY_task'),
                                    request_evidence='fixture-request.json',response_evidence='fixture-response.json',response_sha256='a'*64,
                                    usage_known=True,pipeline_completion_claimed=False)
        return value

    def test_complete_bound_explanation_cannot_turn_tool_failure_into_task_pass(self):
        value=self.overlay();before=deepcopy(value)
        view,execution,_,lines=prepare_result(value,request_id='TEST_ONLY_request')
        self.assertEqual(value,before);self.assertEqual(execution['task_status'],'UNKNOWN')
        self.assertIn('验证受阻',lines[0]);self.assertTrue(view['verified_current_explanation_available'])
        self.assertIn('已独立保存并绑定',lines[-1])

    def test_missing_http_response_or_version_binding_does_not_claim_complete(self):
        for key in ('response_evidence','response_sha256','request_evidence','usage_known','binding'):
            value=self.overlay();value['explanation'].pop(key)
            view,_,_,lines=prepare_result(value,request_id='TEST_ONLY_request')
            self.assertFalse(view['verified_current_explanation_available']);self.assertIn('产品解释未完成',lines[-1])

    def test_invalid_geometry_precedes_simultaneous_tool_failure(self):
        value=saved_result(tool_status='TOOL_FAIL',chain_status='TOOL_FAIL',task_status='UNKNOWN',mission=dict(validity_status='FAIL',task_status='UNKNOWN',completion_status='INVALID_MODEL'))
        self.assertIn('INVALID_MODEL',prepare_result(value)[3][0])

    def test_verified_sidecar_is_available_without_changing_imported_v0(self):
        from week13.src.case_console_data import verified_source_facts
        OUT.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='sysml-fixture-',dir=OUT) as folder:
            ctx=dict(root=folder,baseline_version_id='v000000',baseline_ref=dict(version_id='v000000'),baseline_snapshot=dict(frozen_spec=dict(artifact=dict(modelica_source='fixture'))))
            before=deepcopy(ctx)
            (Path(folder)/'baseline-sysml-reference.json').write_text('{}',encoding='utf8')
            with patch('week13.src.case_taskflow.baseline_sysml_for_context',return_value='TEST_ONLY_VALIDATED_SYSMl'):
                facts=verified_source_facts(ctx)
            self.assertEqual(ctx,before);self.assertFalse(facts['selected_baseline_sysml_present'])
            self.assertTrue(facts['baseline_sysml_available']);self.assertEqual(facts['required_source_completion'],[])
            self.assertEqual(facts['baseline_sysml_completion']['status'],'VERIFIED')
            with patch('week13.src.case_taskflow.baseline_sysml_for_context',side_effect=ValueError('sidecar drift')):
                facts=verified_source_facts(ctx)
            self.assertFalse(facts['baseline_sysml_available']);self.assertEqual(facts['baseline_sysml_completion']['status'],'UNKNOWN')


def fixture_rows():
    for time,normal,pf in ((0.,0.,0.),(.1,5e-10,.5),(.2,1e-9,.5),(.5,1.,.5),(1.,0.,-.5),(1.5,1.,-.5),(1.8,1.,-.5)):
        row=dict(time=time,power_factor=pf,mu_s=.85)
        for i in (1,2,3):
            radius=.07 if i==1 else .127;torque=25. if i==1 else 60.
            total=normal if normal<=1e-9 else (50. if i==1 else 1000. if i==2 else 100.)
            budget=pf*min(torque/radius,.85*total)
            row.update({f'R_w[{i}]':radius,f'T_w[{i}]':torque,f'Fn_sum[{i}]':total,f'Ft_budget[{i}]':budget})
            for b in range(1,42):
                fn=total*({1:.2,2:.3,3:.5}.get(b,0.))
                row[f'FnC[{i},{b}]']=fn;row[f'FtC[{i},{b}]']=budget*fn/total if total>1e-9 else 0.
        yield row


class ExtractedBoundaryChecks(unittest.TestCase):
    def test_fixture_copies_real_reference_equations_without_repairing_them(self):
        source=(ROOT/'results/contact-physics-20261022-01/product-source-review/candidate.mo').read_text(encoding='utf8')
        model,receipt=build_fixture(source)
        for key,equation in receipt['equations'].items():
            self.assertIn(equation,source);self.assertIn(equation,model)
            self.assertEqual(receipt['equation_sha256'][key],hashlib.sha256(equation.encode('utf8')).hexdigest())
        self.assertIn('else if time < 0.3 then 1e-9',model)
        with self.assertRaisesRegex(ValueError,'one source equation'):
            extract_fragment(source+'\n'+receipt['equations']['FtC'])

    def report(self,rows):
        OUT.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='boundary-csv-fixture-',dir=OUT) as folder:
            path=Path(folder)/'fixture.csv'
            with path.open('w',encoding='utf8',newline='') as handle:
                writer=csv.DictWriter(handle,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
            return check_csv(path)

    def test_checker_accepts_valid_fixture_invariants_with_all_required_cases(self):
        report=self.report(list(fixture_rows()))
        self.assertEqual(report['status'],'PASS');self.assertFalse(report['missing_cases'])

    def test_checker_rejects_torque_or_each_contact_friction_excess(self):
        rows=list(fixture_rows());rows[3]['FtC[2,1]']=1000.
        self.assertEqual(self.report(rows)['status'],'FAIL')
        rows=list(fixture_rows());rows[3]['FtC[1,41]']=1.
        self.assertEqual(self.report(rows)['status'],'FAIL')

    def test_checker_rejects_nonfinite_or_cutoff_leak(self):
        rows=list(fixture_rows());rows[1]['FtC[1,1]']=1e-5
        self.assertEqual(self.report(rows)['status'],'FAIL')
        rows=list(fixture_rows());rows[1]['FtC[1,1]']=math.nan
        with self.assertRaisesRegex(ValueError,'nonfinite'):self.report(rows)


if __name__=='__main__':unittest.main()
