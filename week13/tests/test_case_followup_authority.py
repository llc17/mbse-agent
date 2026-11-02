"""Pure local checks for the newly approved 3-round allocation; no ledger."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import asyncio
import unittest
from unittest.mock import Mock,patch
from week13.src.case_paths import ROOT,read_json
from week13.src.case_requests import _validate_authority
from week13.src.case_taskflow import TaskFlow


class FollowupAuthority(unittest.TestCase):
    def value(self,total=3,before=1,reserved=2):
        controls=read_json(ROOT/'configs/case_runtime.json')
        caps=dict(http_requests=10,product_cny=55,total_tokens=10000000,total_output_tokens=3932160,
                  compile=4,simulation=4,candidates=4,software_repair_rounds=total,parameter_revision_rounds=0,observation_extensions=0)
        controls.update(max_requests=10,max_total_tokens=10000000,max_total_output_tokens=3932160,max_cny=55)
        now=datetime.now(timezone.utc)
        return dict(started_at_utc=now.isoformat(),hard_deadline_utc=(now+timedelta(hours=2)).isoformat(),caps=caps,
                    model_controls=controls,permissions=dict(planning_http=True,execution=True),repair_allocation=dict(maximum_extra_rounds=total,
                    maximum_before_first_valid_generated_source=before,reserved_for_post_generation_or_runtime=reserved,
                    maximum_complete_affected_scenario_rerun_rounds=1))
    def test_new_and_historical_valid_allocations(self):
        _validate_authority(self.value());_validate_authority(self.value(4,2,2))
    def test_mismatch_or_spent_post_generation_reserve_rejected(self):
        for total,before,reserved in ((3,2,2),(3,3,0),(3,2,1),(3,True,2)):
            with self.subTest(values=(total,before,reserved)),self.assertRaises(PermissionError):
                _validate_authority(self.value(total,before,reserved))
    def test_ordinary_generation_marks_source_valid_after_validation_before_MCP(self):
        flow=object.__new__(TaskFlow)
        flow.ctx=dict(runtime_authority=dict(ledger_path='not-created'))
        flow.open=Mock(side_effect=RuntimeError('STOP_BEFORE_ANY_MCP'))
        with patch('week13.src.case_taskflow.baseline_sysml_for_context',return_value='fixture'), \
             patch('week13.src.case_taskflow.emit_progress'),patch('week13.src.case_budget.CaseBudget') as budget:
            with self.assertRaisesRegex(RuntimeError,'STOP_BEFORE_ANY_MCP'):
                asyncio.run(flow._prepare_generation({}, {},dict(spec_digest='verified_fixture_digest'),None))
            budget.return_value.mark_valid_generated_source.assert_called_once_with('verified_fixture_digest')


if __name__=='__main__':unittest.main()
