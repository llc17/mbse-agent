"""Eight-wheel handoff gates; synthetic rows are not physics evidence."""
from copy import deepcopy
import json
import unittest
from week12.src.car_domain.outcomes import REQUIRED_SEQUENCE,make_outcome,scene_classification
from week12.src.car_domain.eightwheel_preparation import fixed_scene_plan,verification_command
from week12.src.car_domain.contracts import REPO_ROOT,PHYSICAL_PATH


def rows():
    return [dict(scene=s,variant=v,status='PASS',compile='PASS',simulation='COMPLETED',physical='PASS',
        numeric='PASS' if v=='refined' else 'NOT_RUN_BASE_VARIANT',verification_returncode=0,
        verification_timed_out=False,verification_uncertain=False) for s,v in REQUIRED_SEQUENCE]


class EightwheelHandoff(unittest.TestCase):
    def test_original_sequence_and_budget_preserved(self):
        plan=fixed_scene_plan(json.loads((REPO_ROOT/PHYSICAL_PATH).read_bytes()))
        self.assertEqual(plan['scene_count'],14)
        self.assertEqual(plan['complete_set_wall_cap_s'],3180)
        self.assertEqual(plan['failure_audit'],0)
        self.assertEqual(plan['status'],'PLAN_ONLY_NO_EXECUTION_AUTHORITY')
        self.assertEqual([(r['scene'],r['variant']) for r in plan['scenes']],list(REQUIRED_SEQUENCE))

    def test_physical_change_rejected_before_handoff(self):
        spec=json.loads((REPO_ROOT/PHYSICAL_PATH).read_bytes());spec['requirements']['total_mass']=86
        with self.assertRaises(ValueError):fixed_scene_plan(spec)

    def test_actual_vehicle_checker_not_reference_checker(self):
        command=verification_command('work/scene','manifest.json','graph.json','three_forward_down','base')
        self.assertIn('week12.scripts.s3_c_active_cluster',command)
        self.assertNotIn('week12.scripts.s3_c_car_increment_acceptance',command)
        self.assertIn('--base',command)
        with self.assertRaises(ValueError):verification_command('work','m','g','invented')

    def test_all_fourteen_gate_rows_required_in_order(self):
        complete=rows()
        self.assertEqual(make_outcome(complete)['vehicle_task_status'],'PASS')
        for bad in (complete[:-1],[complete[0]]*14,list(reversed(complete)),complete+[complete[0]]):
            self.assertEqual(make_outcome(bad)['vehicle_task_status'],'UNKNOWN')

    def test_success_strings_cannot_hide_tool_or_numeric_failure(self):
        for key,value in [('compile','FAIL'),('simulation','NOT_RUN'),('physical','FAIL'),('verification_returncode',2),
                          ('verification_returncode',None),('verification_timed_out',True),('verification_uncertain',True),('numeric','FAIL')]:
            data=rows();data[-1][key]=value
            with self.subTest(key=key,value=value):self.assertEqual(make_outcome(data)['vehicle_task_status'],'UNKNOWN')
        self.assertEqual(make_outcome(rows(),error='worker failed')['vehicle_task_status'],'UNKNOWN')

    def test_failure_audit_cannot_promote_unknown_or_model_failure(self):
        data=rows()[:6];data[-1]['status']='FAIL'
        result=make_outcome(data,failure_audit={'status':'TRUSTED_FAILURE_EVIDENCE'})
        self.assertEqual(result['vehicle_task_status'],'UNKNOWN')
        self.assertEqual(result['agent_execution_status'],'BLOCKED')
        self.assertFalse(result['failure_audit_promotes_result'])
        self.assertIsNone(result['published_version_id'])

    def test_r1_actual_model_failure_codes_never_become_trusted_failure(self):
        for code in ('body_force_x_chassis','body_moment_carrier','actual_joint_origin_z','summed_contact_x','actual_owner_normal','unrecognized_code'):
            report={'physical':{'status':'FAIL','checks':[{'code':'actual_hardware_5mm_body','status':'FAIL'},{'code':code,'status':'FAIL'}]}}
            self.assertEqual(scene_classification(rows()[0],report),'CHECK_FAILED_VEHICLE_CONCLUSION_UNKNOWN')

    def test_report_pass_cannot_hide_missing_or_failed_checks(self):
        for checks in ([],[{'code':'body_force_x','status':'REQUIRES_EVIDENCE'}],[{'code':'new','status':'UNKNOWN'}],[{'code':'body_moment','status':'FAIL'}]):
            self.assertNotEqual(scene_classification(rows()[0],{'physical':{'status':'PASS','checks':checks}}),'PASS')

    def test_r2_nonzero_timeout_or_unknown_exit_not_report_pass(self):
        report={'physical':{'status':'PASS','checks':[{'code':'fixture','status':'PASS'}]}}
        for key,value in [('verification_returncode',None),('verification_returncode',1),('verification_returncode',2),('verification_timed_out',True),('verification_uncertain',True)]:
            result=rows()[0];result[key]=value
            self.assertEqual(scene_classification(result,report),'TOOL_FAIL')

    def test_base_and_refined_reports_have_different_numeric_requirements(self):
        report={'physical':{'status':'PASS','checks':[{'code':'fixture','status':'PASS'}]},'numeric':{'status':'NOT_RUN_BASE_VARIANT'}}
        self.assertEqual(scene_classification(rows()[0],report),'PASS')
        self.assertEqual(scene_classification(rows()[1],report),'NUMERIC_UNTRUSTED')
