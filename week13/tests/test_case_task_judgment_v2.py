"""Offline tests of the separated route/contact judgment; no dynamics or HTTP."""
from copy import deepcopy
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch,Mock

from week13.src.case_contact_validity import diagnose_contact_rows,RULE_VERSION
from week13.src.case_contact_review import SCHEMA,COMPLETE,current_summary,task_conclusion,_legacy_center_failure_only,_dual_evidence_pass
from week13.src.case_console_result import prepare_result
from week13.src.case_checks import assess_rows,body_motion_observations
from week13.src.inherited.model_artifact import summarize_execution
from week13.src.case_semantic_guard import protect_explanation
from week13.src import case_projects
from week13.tests.test_case_contact_validity import VALUES,samples


def bound(*,diagnostic='PASS',state='succeeded',legacy=False,task='PASS'):
    if diagnostic=='FAIL':task='UNKNOWN'
    completion='CONTACT_ANOMALY_ROUTE_COMPLETE' if diagnostic=='FAIL' else COMPLETE
    identity=dict(project_id='p',task_id='t',version_id='v000001',spec_digest='a'*64,
                  source_sha256='b'*64,manifest_sha256='c'*64,csv_sha256='d'*64,rule_version=RULE_VERSION)
    assessment=dict(task_status=task,route_status='PASS',validity_status=diagnostic,
                    diagnostic_status=diagnostic,completion_status=completion,completion_time=34.35)
    review=dict(schema=SCHEMA,rule_version=RULE_VERSION,status=diagnostic,validity_status=diagnostic,
                task_status=task,route_status='PASS',diagnostic_status=diagnostic,
                completion_status=completion,current_assessment=assessment,binding=identity,
                execution_evidence_status='PASS',legacy_center_failure_reclassified=legacy,
                current_chain_status='PASS',contact_validity=dict(hard_contradictions=[]))
    return dict(**{k:v for k,v in identity.items() if k not in ('csv_sha256','rule_version')},
                state=state,tool_status='PASS',chain_status='BLOCKED_INVALID_MODEL' if legacy else 'PASS',
                task_status='UNKNOWN' if legacy else task,evidence_status='COMPLETE',current_contact_review=review)


class FullCircleGeometry(unittest.TestCase):
    def test_same_exact_rule_for_all_entities_and_positive_margin(self):
        report=diagnose_contact_rows(samples(x=3.2,z=.6),VALUES)
        self.assertEqual(report['status'],'FAIL')
        self.assertEqual(report['physical_status'],'UNKNOWN')
        for entity in ('n','f','r'):
            item=report['per_entity'][entity]
            self.assertEqual(item['counters']['full_circle_inside_solid_sample_count'],2)
            evidence=item['evidence']['maximum_full_circle_inside_solid']
            self.assertAlmostEqual(evidence['full_circle_interior_margin_m'],.3-item['radius_m'])
            self.assertTrue(evidence['full_circle_inside_solid'])

    def test_nearest_finite_riser_prevents_false_full_circle(self):
        report=diagnose_contact_rows(samples(x=2.01,z=.1),VALUES)
        self.assertEqual(report['status'],'PASS')
        self.assertEqual(report['counters']['center_inside_solid_sample_count'],6)
        self.assertEqual(report['counters']['full_circle_inside_solid_sample_count'],0)

    def test_inside_with_nearest_corner_is_not_tread_height_proxy(self):
        report=diagnose_contact_rows(samples(x=2.27,z=.29),VALUES)
        e=report['per_entity']['f']['evidence']['first_center_inside_solid']
        self.assertEqual(e['nearest_boundary']['kind'],'tread')
        self.assertAlmostEqual(e['center_to_boundary_distance_m'],math.hypot(.01,.01))
        self.assertEqual(report['status'],'PASS')

    def test_d_equal_radius_is_boundary_uncertain_not_failure(self):
        rows=samples(x=0,z=-VALUES['R_main'],z_n=-VALUES['R_nose'])
        report=diagnose_contact_rows(rows,VALUES)
        self.assertEqual(report['status'],'PASS')
        self.assertEqual(report['counters']['full_circle_boundary_uncertain_sample_count'],6)
        self.assertEqual(report['hard_contradictions'],[])

    def test_no_solver_tolerance_is_reinterpreted_as_metres(self):
        # A 1 mm positive sampled margin is reported, with no invented .01 m gate.
        report=diagnose_contact_rows(samples(x=0,z=-.128,z_n=-.071),VALUES)
        self.assertEqual(report['status'],'FAIL')
        self.assertAlmostEqual(report['hard_contradictions'][0]['full_circle_interior_margin_m'],.001)
        self.assertEqual(report['physical_status'],'UNKNOWN')

    def test_only_arithmetic_epsilon_can_mark_unresolved_boundary(self):
        report=diagnose_contact_rows(samples(x=0,z=-.127-1e-14,z_n=-.07-1e-14),VALUES)
        self.assertEqual(report['status'],'PASS')
        self.assertEqual(report['counters']['full_circle_boundary_uncertain_sample_count'],6)

    def test_mixed_legacy_height_formulas_do_not_create_new_failure(self):
        report=diagnose_contact_rows(samples(h_n=.3,h_f=0,h_r=0),VALUES)
        self.assertEqual(report['status'],'PASS')
        self.assertGreater(report['counters']['legacy_height_disagreement_sample_count'],0)


class SeparatedAssessment(unittest.TestCase):
    def test_severe_contact_does_not_erase_route(self):
        route=dict(task_status='PASS',validity_status='PASS',completion_status=COMPLETE,issues=[],completion_time=12.)
        with patch('week13.src.case_checks.assess_route_rows',return_value=route):
            report=assess_rows(samples(x=3.2,z=.6),VALUES)
        self.assertEqual(report['task_status'],'UNKNOWN')
        self.assertEqual(report['route_status'],'PASS')
        self.assertEqual(report['diagnostic_status'],'FAIL')
        self.assertEqual(report['completion_time'],12.)
        self.assertEqual(report['overall_summary'],'ROUTE_COMPLETE_CONTACT_ANOMALY')

    def test_motion_missing_platform_is_observation_unknown_only(self):
        rows=samples()
        for row in rows:row.update(x=0,z=.3,psi=.1,omega_psi=.2)
        report=body_motion_observations(rows,VALUES)
        self.assertEqual(report['status'],'OBSERVED')
        self.assertEqual(report['phases']['all_wheels_platform']['status'],'UNKNOWN')
        self.assertIsNone(report['acceptance_thresholds'])
        self.assertEqual(report['final_z_relative_entry_m'],0.)

    def test_motion_reports_measured_lift_drop_pose_without_new_gate(self):
        rows=[]
        for i,(x,z) in enumerate(((0,.3),(3.4,1.2),(6.4,.31))):
            row=samples(x=x)[0];row.update(time=i,x=x,z=z,psi=.1*i,omega_psi=.02*i);rows.append(row)
        report=body_motion_observations(rows,VALUES)
        self.assertAlmostEqual(report['phases']['all_wheels_platform']['z_relative_entry_range_m'][0],.9)
        self.assertAlmostEqual(report['platform_to_exit_drop_m'],.89)
        self.assertEqual(report['whole_observation']['psi_range_rad'],[0.,.2])

    def summarized(self,mission,trajectory=None,checks=None):
        tool=dict(compile=dict(status='PASS'),simulation=dict(status='PASS'),runtime_parameter_readback=dict(status='PASS'))
        spec=dict(checks=checks or [])
        if trajectory is not None:spec['trajectory_contract']={}
        with patch('week13.src.inherited.artifact_delivery.read_signals',return_value=dict(mapping={},series={})),patch('week13.src.case_checks.assess',return_value=mission),patch('week13.src.inherited.artifact_trajectory.evaluate_trajectory',return_value=dict(task_status=trajectory,scope='TEST_ONLY')):
            return summarize_execution(spec,tool,'unused.csv')

    def test_new_anomaly_execution_is_not_task_pass_with_failed_chain(self):
        report=self.summarized(dict(task_status='UNKNOWN',route_status='PASS',diagnostic_status='FAIL',validity_status='FAIL',completion_status='CONTACT_ANOMALY_ROUTE_COMPLETE',assessment_evidence_status='PASS',overall_summary='ROUTE_COMPLETE_CONTACT_ANOMALY'))
        self.assertEqual(report['task_status'],'UNKNOWN');self.assertEqual(report['chain_status'],'PASS')
        self.assertEqual(report['diagnostic_status'],'FAIL');self.assertEqual(report['physical_status'],'UNKNOWN')

    def test_empty_signal_checks_do_not_skip_trajectory_fail(self):
        report=self.summarized(dict(task_status='PASS',route_status='PASS',validity_status='PASS',completion_status=COMPLETE),trajectory='FAIL')
        self.assertEqual(report['task_status'],'FAIL')
        self.assertEqual(report['route_status'],'PASS')
        self.assertEqual(report['completion_status'],'TASK_CHECK_FAILED')


class CurrentProjection(unittest.TestCase):
    def test_current_rule_reclassifies_only_attributed_old_failure(self):
        original=bound(state='failed',legacy=True);before=deepcopy(original)
        current=current_summary(original)
        self.assertEqual(current['recorded_state'],'failed');self.assertEqual(current['state'],'failed')
        self.assertEqual(current['recorded_chain_status'],'BLOCKED_INVALID_MODEL')
        self.assertEqual(current['chain_status'],'PASS');self.assertEqual(current['task_status'],'PASS')
        self.assertTrue(task_conclusion(current)[1]);self.assertEqual(original,before)

    def test_unattributed_failed_state_never_upgraded(self):
        current=current_summary(bound(state='failed'))
        self.assertEqual(current['task_status'],'UNKNOWN')
        self.assertFalse(task_conclusion(current)[1])

    def test_old_schema_or_rule_cache_is_not_new_review(self):
        for key,value in (('schema','v12-current-contact-review-1'),('rule_version','OLD')):
            old=bound();old['current_contact_review'][key]=value
            current=current_summary(old)
            self.assertEqual(current['current_review_state'],'NOT_REVIEWED')
            self.assertEqual(current['task_status'],'UNKNOWN')

    def test_anomaly_headline_preserves_route_and_no_capability_claim(self):
        current=current_summary(bound(diagnostic='FAIL'))
        description,passed=task_conclusion(current)
        self.assertIn('轨迹已完成',description);self.assertIn('模型接触异常',description)
        self.assertFalse(passed);self.assertEqual(current['task_status'],'UNKNOWN')

    def test_binding_conflict_and_tool_fail_still_block(self):
        for update in ('binding','tool'):
            item=bound()
            if update=='binding':item['current_contact_review']['binding']['csv_sha256']='broken'
            else:item['tool_status']='TOOL_FAIL'
            summary=current_summary(item)
            self.assertFalse(task_conclusion(summary)[1]);self.assertEqual(summary['task_status'],'UNKNOWN')

    def test_test_only_can_never_be_real_task_or_physical_pass(self):
        item=bound();item['outcome']=dict(test_only=True)
        summary=current_summary(item)
        self.assertEqual(summary['task_status'],'UNKNOWN');self.assertFalse(task_conclusion(summary)[1])
        self.assertEqual(summary['physical_status'],'UNKNOWN')

    def test_anomaly_rendering_keeps_route_details(self):
        item=bound(diagnostic='FAIL')
        _,execution,_,lines=prepare_result(dict(request_id='r',execution=item),request_id='r')
        self.assertIn('模型接触异常',lines[0]);self.assertEqual(execution['route_status'],'PASS')
        self.assertTrue(any(line.startswith('轨迹覆盖观测：') for line in lines))

    def test_precise_old_ok_false_classification_is_not_tool_fail(self):
        item=bound(state='failed',legacy=True)
        result=dict(request_id='r',execution=dict(item,ok=False),state=dict(latest_request_execution=item))
        _,execution,_,lines=prepare_result(result,request_id='r')
        self.assertEqual(execution['tool_status'],'PASS');self.assertIn('任务成功（当前简化模型内）',lines[0])
        result['execution']['chain_status']='BLOCKED_TOOL'
        _,_,_,lines=prepare_result(result,request_id='r')
        self.assertIn('证据存在冲突',lines[0])

    def test_explanation_facts_carry_current_diagnostic_and_recorded_terminal(self):
        item=bound(state='failed',legacy=True)
        report=protect_explanation('本简化模型内任务已完成，物理状态：UNKNOWN。',dict(request_id='r',project_id='p',latest_request_execution=item))
        fact=report['explanation_validation']['facts'][0]
        self.assertEqual(fact['route_status'],'PASS');self.assertEqual(fact['state'],'failed')
        self.assertEqual(fact['recorded_chain_status'],'BLOCKED_INVALID_MODEL')
        self.assertIn('任务成功（当前简化模型内）',report['program_fact_lines'][0])

    def test_real_dual_modelica_status_is_a_three_stage_protocol(self):
        outcome=dict(dual_source_contract='v11-dual-source-check-1',sysml_status='PASS',consistency_status='PASS',modelica_status=dict(check='PASS',compile='PASS',simulation='PASS'))
        self.assertTrue(_dual_evidence_pass(outcome))
        for stage in ('check','compile','simulation'):
            changed=deepcopy(outcome);changed['modelica_status'][stage]='FAIL'
            self.assertFalse(_dual_evidence_pass(changed))

    def test_legacy_reclassification_requires_the_specific_only_old_failure(self):
        issue=['legacy center interior diagnostic']
        mission=dict(completion_status='INVALID_MODEL',validity_status='FAIL',issues=issue,
                     route_assessment=dict(validity_status='PASS',task_status='PASS'),
                     contact_validity=dict(schema='v12-contact-validity-1',conclusion='HARD_GEOMETRIC_CONTRADICTION',counters=dict(center_inside_solid_sample_count=2),issues=issue),
                     terrain_binding=dict(status='PASS'),observation_binding=dict(status='PASS'))
        outcome=dict(worker_exit_code=0,tool_status='PASS',chain_status='BLOCKED_INVALID_MODEL',task_status='UNKNOWN',mission_assessment=mission,
                     tool_result=dict(compile=dict(status='PASS'),simulation=dict(status='PASS'),runtime_parameter_readback=dict(status='PASS')))
        snapshot=dict(state='failed',error='')
        self.assertTrue(_legacy_center_failure_only(snapshot,outcome))
        for field,value in (('worker_exit_code',1),('tool_status','TOOL_FAIL'),('chain_status','BLOCKED_PARAMETER_EVIDENCE')):
            changed=deepcopy(outcome);changed[field]=value
            self.assertFalse(_legacy_center_failure_only(snapshot,changed))
        changed=deepcopy(outcome);changed['mission_assessment']['issues']=['another independent failure']
        self.assertFalse(_legacy_center_failure_only(snapshot,changed))

    def test_correct_anomaly_explanation_is_visible_but_physical_pass_is_rejected(self):
        item=bound(diagnostic='FAIL')
        binding=dict(request_id='r',project_id='p',latest_request_execution=item)
        answer='轨迹已完成，但模型接触异常，不能用于车辆能力证明；physical_status=UNKNOWN。'
        report=protect_explanation(answer,binding)
        self.assertTrue(report['explanation_validation']['body_displayed'])
        wrong=protect_explanation('physical_status=PASS，已证明车辆能力。',binding)
        self.assertFalse(wrong['explanation_validation']['body_displayed'])


class AutomaticDefaultSelection(unittest.TestCase):
    def test_only_import_or_current_pass_can_be_automatic_default(self):
        source='model M parameter Real m=85; parameter Real I_pitch=20; parameter Real T_main_max=60; parameter Real T_nose_max=25; end M;'
        spec=dict(artifact=dict(modelica_source=source,sysml_source='TEST_ONLY'))
        original=dict(project_id='p',version_id='v000000',task_id='reference',record_kind='IMPORTED_REFERENCE',
                      frozen_spec=spec,source_sha256='e'*64,task_status='NOT_RUN')
        for mode in ('not_reviewed','anomaly','current_pass'):
            latest=bound(diagnostic='FAIL' if mode=='anomaly' else 'PASS')
            latest['frozen_spec']=spec;latest['record_kind']='ARTIFACT_EXECUTION_SNAPSHOT'
            if mode=='not_reviewed':latest.pop('current_contact_review')
            with self.subTest(mode=mode),tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'results') as folder:
                root=Path(folder);case=dict(case_id='test',project_id='p',project_root=str(root),status='TEST_ONLY',kind='TEST_ONLY',name='TEST_ONLY')
                svc=SimpleNamespace(_artifact_version=lambda version:deepcopy(original if version=='v000000' else latest),versions_list=lambda:dict(versions=dict(items=[latest])))
                controls=dict.fromkeys(('model','thinking','reasoning_effort','context_window_tokens','max_tokens','maximum_request_reserve_cny'),'TEST_ONLY')
                with patch.object(case_projects,'ROOT',root),patch.object(case_projects,'list_cases',return_value=[case]),patch.object(case_projects,'resolve_case',return_value=case),patch.object(case_projects,'verify_case',return_value=case),patch.object(case_projects,'_service',return_value=svc),patch.object(case_projects,'support_for_source',return_value=dict(status='SUPPORTED')),patch.object(case_projects,'_read',return_value=controls),patch('week13.src.case_contact_review.with_review',side_effect=lambda record,_:record):
                    result=case_projects.discover_case('test')
                expected='v000001' if mode=='current_pass' else 'v000000'
                self.assertEqual(result['default_baseline']['version_id'],expected)
                candidate=result['baselines'][-1]
                self.assertTrue(candidate['selectable'],'explicit source selection stays available')
                self.assertEqual(candidate['automatic_default_eligible'],mode=='current_pass')


if __name__=='__main__':unittest.main()
