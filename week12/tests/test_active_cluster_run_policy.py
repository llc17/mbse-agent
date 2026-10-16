import copy
from datetime import datetime,timezone
import unittest
from week12.src.active_cluster.execution import DEFAULT_OUT,read,Stop,ORDER
from week12.src.active_cluster import run_policy as p

class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.base=read(DEFAULT_OUT/'ledger-before-execution-remediation.json')
        self.data=copy.deepcopy(self.base)
    def test_original_history(self):p.validate_history(self.base,self.data)
    def test_history_deleted(self):
        self.data['scene_attempts']=[]
        with self.assertRaises(Stop):p.validate_history(self.base,self.data)
    def test_original_cap_inflated(self):
        self.data['process_cap_s']+=1
        with self.assertRaises(Stop):p.validate_history(self.base,self.data)
    def test_closed_scope_changed(self):
        self.data['vehicle_recovery']['status']='RUNNING'
        with self.assertRaises(Stop):p.validate_history(self.base,self.data)
    def test_old_repairs_cannot_gain_unused_round(self):
        self.data['repairs'].append({})
        with self.assertRaises(Stop):p.validate_history(self.base,self.data)
    def test_prep_and_final_time_count(self):
        rows=[{'started_utc':'2026-10-09T00:00:00Z','ended_utc':'2026-10-09T00:00:10Z'}]
        self.assertEqual(p.active_seconds(rows,datetime(2026,10,9,1,tzinfo=timezone.utc)),10)
    def test_recorded_wait_excluded(self):
        row={'started_utc':'2026-10-09T00:00:00Z','ended_utc':'2026-10-09T00:00:10Z','excluded_wait':True,'independent_wait_evidence':'bound-receipt'}
        self.assertEqual(p.active_seconds([row],datetime(2026,10,9,1,tzinfo=timezone.utc)),0)
    def test_unproven_wait_not_excluded(self):
        row={'started_utc':'2026-10-09T00:00:00Z','ended_utc':'2026-10-09T00:00:10Z','excluded_wait':True}
        with self.assertRaises(Stop):p.active_seconds([row],datetime(2026,10,9,1,tzinfo=timezone.utc))
    def test_overlapping_intervals_rejected(self):
        row={'started_utc':'2026-10-09T00:00:00Z','ended_utc':'2026-10-09T00:00:10Z'}
        with self.assertRaises(Stop):p.active_seconds([row,row],datetime(2026,10,9,1,tzinfo=timezone.utc))
    def test_original_all_processes_including_outer(self):
        self.assertAlmostEqual(p.cumulative_process(36.4908883,[],[]),36.4908883)
    def test_live_child_not_double_counted(self):
        row={'status':'CLAIMED','nonce':'n'}
        self.assertEqual(p.cumulative_process(10,[5],[row],{'nonce':'n','elapsed_s':7,'child_s':5}),17)
    def test_unreconciled_parent_charged_reserve(self):
        self.assertEqual(p.cumulative_process(10,[],[{'status':'RETURNED','reserved_parent_s':60}]),70)
    def test_foreign_live_launcher_rejected(self):
        with self.assertRaises(Stop):p.cumulative_process(10,[],[{'status':'CLAIMED','nonce':'n'}],{'nonce':'other','elapsed_s':1,'child_s':0})
    def test_whole_two_sets_reserved(self):p.reserve_whole_sets(36.5,15000,0,2)
    def test_spent_helper_is_not_reserved_twice(self):
        p.reserve_whole_sets(37.0,15000,.5,2,helper_used=.5)
    def test_remaining_helpers_still_reserved(self):
        with self.assertRaises(Stop):
            p.reserve_whole_sets(6990,15000,.5,2,helper_used=.5)
    def test_no_partial_repair_reserve(self):
        with self.assertRaises(Stop):p.reserve_whole_sets(6800,15000,0,1)
    def test_fixed_order(self):p.next_scene([],4,{},ORDER[0]+'-base')
    def test_refined_first_rejected(self):
        with self.assertRaises(Stop):p.next_scene([],4,{},ORDER[0]+'-refined')
    def test_failure_cannot_be_retried(self):
        with self.assertRaises(Stop):p.next_scene([{'round':4,'status':'FAIL'}],4,{},ORDER[0]+'-refined')
    def test_both_small_gates_before20(self):
        rows=[{'round':4,'status':'PASS'}]*10
        with self.assertRaises(Stop):p.next_scene(rows,4,{},ORDER[5]+'-base')
    def test_correct20_gate(self):
        p.next_scene([{'round':4,'status':'PASS'}]*10,4,{k:'PASS' for k in ORDER[:5]},ORDER[5]+'-base')
    def test_bare_frontend_flag_rejected(self):
        with self.assertRaises(Stop):p.validate_frontend(True,'h',p.EXPECTED_CLASSES,'r',{})
    def test_real_frontend_exact(self):
        actual=read(DEFAULT_OUT/'readiness-diagnostic-01/result.json')
        proof={'model_sha256':'h','result_sha256':'r','classes':p.EXPECTED_CLASSES}
        self.assertTrue(p.validate_frontend(proof,'h',p.EXPECTED_CLASSES,'r',actual))
    def test_frontend_class_swap(self):
        m=copy.deepcopy(p.EXPECTED_CLASSES);m[ORDER[0]]=m[ORDER[1]]
        with self.assertRaises(Stop):p.validate_frontend({'model_sha256':'h','result_sha256':'r','classes':m},'h',m,'r',{})
    def test_missing_physical_proof_is_not_implementation(self):
        with self.assertRaises(Stop):p.allow_repair([],{'classification':'PHYSICAL_FAIL'})
    def test_single_proven_repair(self):
        proof={'classification':'PROVEN_IMPLEMENTATION_DEFECT','root_review':'ACCEPTED','physical_control_task_unchanged':True}
        p.allow_repair([],proof)
        with self.assertRaises(Stop):p.allow_repair([proof],proof)
    def test_admission_requires_actual_bound_review(self):
        with self.assertRaises(Stop):p.validate_admission({},'b','r','v')
