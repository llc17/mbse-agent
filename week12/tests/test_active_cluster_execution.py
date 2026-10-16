"""Negative lifecycle checks for the new named route, without model execution."""
import json
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from week12.src.active_cluster import execution as ex


class ExecutionBounds(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.data = {
            'proposal_sha256': ex.EXPECTED_SPEC, 'approval_sha256': 'approval',
            'started_utc': ex.stamp(), 'status': 'IMPLEMENTING',
            'future_cap_s': 23400, 'process_cap_s': 10800,
            'implementation_analysis_cap_s': 12600, 'process_time_uncertainty_s': 0,
            'scene_attempts': [], 'parse_attempts': [], 'other_checks': [],
            'repairs': [], 'source_bindings': []}
        ex.save(self.directory/'ledger.json', self.data)
        self.auth = patch.object(ex, 'authorized', return_value=({}, {'execution_contract_proposal': {
            'all_scene_set_cap_s':3180, 'other_process_checks_reserved_s':1170,
            'parse_each_cap_s':30, 'SysML_parse_attempts':3,
            'scene_caps':[{'name':'active_joint_energy','wall_each_s':30}]}}))
        self.sha = patch.object(ex, 'sha', return_value='approval')
        self.auth.start(); self.sha.start()
        self.route = ex.Route(self.directory)

    def tearDown(self):
        self.auth.stop(); self.sha.stop(); self.temp.cleanup()

    def set_data(self, **values):
        self.data.update(values); ex.save(self.directory/'ledger.json', self.data)

    def test_uncertain_running_reservation_prevents_resume(self):
        self.set_data(scene_attempts=[{'status':'RUNNING','processes':[]}])
        with self.assertRaisesRegex(ex.Stop, 'UNCERTAIN_PRIOR'):
            self.route.reserve('scene','next',30)

    def test_stopped_route_cannot_silently_reopen(self):
        self.set_data(status='STOPPED_PHYSICS')
        with self.assertRaisesRegex(ex.Stop, 'ALREADY_STOPPED'):
            self.route.reserve('scene','next',30)

    def test_attempt_cap_counts_creation_failures(self):
        self.set_data(other_checks=[{'status':'NOT_LAUNCHED','processes':[]}]*12)
        with self.assertRaisesRegex(ex.Stop, 'ATTEMPT_LIMIT'):
            self.route.reserve('other','one more',10)

    def test_auxiliary_aggregate_reserve_is_enforced(self):
        self.set_data(other_checks=[{'status':'FAIL','processes':[{'duration_s':1160}]}])
        with self.assertRaisesRegex(ex.Stop, 'KIND_PROCESS_TIME'):
            self.route.reserve('other','too much',20)

    def test_incomplete_repair_scene_set_is_rejected(self):
        self.set_data(repairs=[{'evidence':'proved implementation bug'}],
                      scene_attempts=[{'status':'FAIL','processes':[]}]*29)
        with self.assertRaisesRegex(ex.Stop, 'COMPLETE_AFFECTED_SET'):
            self.route.source_bind([], {}, 2)

    def test_analysis_deadline_is_separate_from_process_cap(self):
        with patch.object(self.route,'elapsed',return_value=12601):
            with self.assertRaisesRegex(ex.Stop,'IMPLEMENTATION_ANALYSIS'):
                self.route.assert_ready()

    def test_binding_drift_is_rejected(self):
        self.set_data(approval_sha256='stale')
        with self.assertRaisesRegex(ex.Stop,'LEDGER_APPROVAL_BINDING_DRIFT'):
            self.route.refresh()

    def test_source_drift_is_rejected(self):
        with patch.object(ex,'sha',return_value='changed'):
            with self.assertRaisesRegex(ex.Stop,'SOURCE_DRIFT'):
                self.route.assert_sources({'files':[{'path':'AGENTS.md','sha256':'original'}]})

    def test_current_operation_is_saved_before_launch(self):
        slot=self.route.reserve('scene','active_joint_energy-base',30)
        persisted=ex.read(self.directory/'ledger.json')
        self.assertEqual(persisted['scene_attempts'][0]['status'],'RUNNING')
        self.assertEqual(slot,('scene_attempts',0))

    def test_individual_scene_cannot_spend_another_scene_allowance(self):
        with self.assertRaisesRegex(ex.Stop,'SCENE_SINGLE_ATTEMPT_CAP'):
            self.route.reserve('scene','active_joint_energy-base',31)

    def test_parse_single_limit_is_enforced(self):
        with self.assertRaisesRegex(ex.Stop,'PARSE_SINGLE_ATTEMPT_CAP'):
            self.route.reserve('parse','generated',31)

    def test_physical_failure_cannot_authorize_implementation_repair(self):
        self.set_data(status='STOPPED_AFFECTED_EXECUTION')
        ex.save(self.directory/'false-repair.json',{
            'classification':'PHYSICAL_FAILURE', 'root_review_status':'ACCEPTED',
            'proposal_sha256':ex.EXPECTED_SPEC, 'failed_operation':'COMPILE_PROCESS_FAILURE',
            'specific_defect':'fiction', 'proof_files':[{'path':'x','sha256':'x'}],
            'unchanged_physical_control_task_attestation':True})
        with self.assertRaisesRegex(ex.Stop,'NO_REVIEWED_IMPLEMENTATION'):
                self.route.accept_implementation_repair(self.directory/'false-repair.json')

    def test_closed_active_interval_preserves_spent_time_excluding_approval_wait(self):
        now=datetime.now(timezone.utc)
        start=(now-timedelta(seconds=100)).isoformat()
        self.set_data(started_utc=start,active_intervals=[{
            'started_utc':start,'ended_utc':(now-timedelta(seconds=80)).isoformat()}])
        self.route.refresh()
        self.assertAlmostEqual(self.route.elapsed(),20,places=6)

    def test_active_time_cannot_reset_initial_origin(self):
        self.set_data(active_intervals=[{'started_utc':'2026-10-01T00:00:00Z',
                                        'ended_utc':'2026-10-01T00:00:01Z'}])
        self.route.refresh()
        with self.assertRaisesRegex(ex.Stop,'INITIAL_ACTIVE_TIME_ORIGIN_DRIFT'):
            self.route.elapsed()

    def test_auxiliary_cap_cannot_expand_without_bound_user_addendum(self):
        self.set_data(other_check_cap=15)
        with self.assertRaisesRegex(ex.Stop,'WITHOUT_EXTERNAL_APPROVAL'):
            self.route.reserve('other','unapproved expansion',20)

    def test_third_repair_cannot_expand_without_final_receipt(self):
        self.set_data(repair_rounds_cap=3)
        self.route.refresh()
        with self.assertRaisesRegex(ex.Stop,'FINAL_REPAIR_LIMIT_WITHOUT'):
            self.route.repair_limit()

    def final_fixture(self):
        folder=self.directory/'final-fixture';folder.mkdir()
        names=['authorization.json','auxiliary-cap-addendum-authorization.json',
               'final-correction-budget-proposal.json','final-correction-authorization.json',
               'ledger-before-final-correction.json','ledger.json']
        for name in names:
            (folder/name).write_bytes((ex.DEFAULT_OUT/name).read_bytes())
        data=ex.read(folder/'ledger.json');data['status']='IMPLEMENTING'
        # Reserve slot17 in the synthetic copy even when the real dispatcher
        # already occupies it. The immutable pre-approval prefix has15 rows.
        data['other_checks']=data['other_checks'][:16]
        ex.save(folder/'ledger.json',data)
        return folder

    @staticmethod
    def real_sha(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def test_actual_final_receipt_enforces_cap17_and_round4(self):
        folder=self.final_fixture()
        with patch.object(ex,'sha',side_effect=self.real_sha):
            route=ex.Route(folder)
            self.assertEqual(route.auxiliary_limit(),17)
            self.assertEqual(route.repair_limit(),3)
            slot=route.reserve('other','last launcher',60,4)
            route.finish(slot,'PASS')
            with self.assertRaisesRegex(ex.Stop,'FINITE_ATTEMPT_LIMIT'):
                route.reserve('other','eighteenth',1,4)

    def test_final_receipt_cannot_erase_failed_scene_history(self):
        folder=self.final_fixture()
        data=ex.read(folder/'ledger.json');data['scene_attempts']=[]
        ex.save(folder/'ledger.json',data)
        with patch.object(ex,'sha',side_effect=self.real_sha):
            with self.assertRaisesRegex(ex.Stop,'HISTORICAL_ROWS_DRIFT'):
                ex.Route(folder).source_round_limit()

    def test_final_receipt_cannot_increase_original_time(self):
        folder=self.final_fixture()
        data=ex.read(folder/'ledger.json');data['future_cap_s']+=1
        ex.save(folder/'ledger.json',data)
        with patch.object(ex,'sha',side_effect=self.real_sha):
            with self.assertRaisesRegex(ex.Stop,'CANNOT_RESET_OR_EXPAND_TIME'):
                ex.Route(folder).repair_limit()

    def test_all_seven_frontend_checks_precede_first_component_build(self):
        classes=tuple('V11GeneratedActive.'+name for name in ex.ORDER)
        text=ex.build_mos(classes[0],Path('V11GeneratedActive.mo'),12,.02,False,
                          '^(time|o_.*)$',classes)
        checks=[text.index('checkModel('+cls+')') for cls in classes]
        gate=text.index('if not frontendOK then exit(1); end if;')
        self.assertTrue(all(position<gate for position in checks))
        self.assertLess(gate,text.index('buildModel('))
        self.assertNotIn('simulate(',text)


if __name__=='__main__':
    unittest.main()
