from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from week9.experiments.cases import get_case
from week9.experiments.freeze_protocol import DEFAULT_PROTOCOL, validate_protocol
from week9.src.runner import build_stage0_result, main


class ProtocolAndRunnerTests(unittest.TestCase):
    def test_holdout_is_not_returned_during_normal_development(self):
        self.assertIsNone(get_case("three_room_thermal_chain"))
        self.assertIsNotNone(
            get_case("three_room_thermal_chain", include_holdout=True)
        )

    def test_current_frozen_protocol_matches_sources(self):
        protocol = json.loads(DEFAULT_PROTOCOL.read_text(encoding="utf-8"))
        self.assertEqual(validate_protocol(protocol), [])

    def test_formal_runs_cannot_be_enabled_with_unfrozen_thresholds(self):
        protocol = json.loads(DEFAULT_PROTOCOL.read_text(encoding="utf-8"))
        protocol = deepcopy(protocol)
        protocol["formal_online_runs_allowed"] = True
        protocol["release_thresholds"]["simple_case_token_budget"] = None
        errors = validate_protocol(protocol)
        self.assertTrue(any("threshold" in error for error in errors))

    def test_any_protocol_policy_tamper_breaks_full_digest(self):
        protocol = json.loads(DEFAULT_PROTOCOL.read_text(encoding="utf-8"))
        protocol = deepcopy(protocol)
        protocol["fairness"]["same_raw_requirement"] = False
        errors = validate_protocol(protocol)
        self.assertTrue(any("complete experiment protocol" in error for error in errors))
        self.assertTrue(any("fairness" in error for error in errors))

    def test_replacement_formal_run_cannot_reuse_aborted_records(self):
        protocol = json.loads(DEFAULT_PROTOCOL.read_text(encoding="utf-8"))
        protocol = deepcopy(protocol)
        protocol["formal_restart_preflight"]["reuse_aborted_runs"] = True
        errors = validate_protocol(protocol)
        self.assertTrue(any("formal restart boundary" in error for error in errors))

    def test_post_holdout_attempt_cannot_enable_tuning(self):
        protocol = json.loads(DEFAULT_PROTOCOL.read_text(encoding="utf-8"))
        protocol = deepcopy(protocol)
        protocol["latest_formal_attempt"]["post_holdout_tuning_allowed"] = True
        errors = validate_protocol(protocol)
        self.assertTrue(any("latest formal attempt boundary" in error for error in errors))

    def test_stage0_runner_reports_generation_provider_error_without_crashing(self):
        class BrokenProvider:
            def generate(self, _requirement):
                raise RuntimeError("injected provider failure")

        result = build_stage0_result(
            "rc_lowpass", "stage0-test", generation_provider=BrokenProvider()
        )
        self.assertFalse(result.overall_success)
        self.assertEqual(result.primary_failure_category, "generation_provider_error")

    def test_runner_rejects_requirement_or_hash_mismatch(self):
        case = get_case("rc_lowpass")
        result = build_stage0_result(
            "rc_lowpass",
            "mismatch-test",
            raw_input="different requirement",
            case_sha256=case.sha256,
        )
        self.assertFalse(result.overall_success)
        self.assertEqual(result.primary_failure_category, "case_contract_mismatch")

    def test_runner_refuses_holdout_while_protocol_access_is_disabled(self):
        with patch("week9.src.runner._holdout_access_enabled", return_value=False):
            result = build_stage0_result(
                "three_room_thermal_chain",
                "holdout-test",
                formal_holdout_access=True,
            )
        self.assertFalse(result.overall_success)
        self.assertEqual(result.primary_failure_category, "holdout_access_disabled")

    def test_cli_writes_machine_readable_failure_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "result.json"
            exit_code = main(
                [
                    "--case",
                    "rc_lowpass",
                    "--run-id",
                    "cli-test",
                    "--raw-input",
                    "different requirement",
                    "--out",
                    str(output),
                ]
            )
            data = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 2)
        self.assertFalse(data["overall_success"])
        self.assertEqual(data["primary_failure_category"], "case_contract_mismatch")


if __name__ == "__main__":
    unittest.main()
