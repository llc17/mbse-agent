from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from week9.experiments.cases import DEVELOPMENT_REGRESSION_CASES, HOLDOUT_CASES
from week9.experiments.version_adapters import (
    LegacyAdapter,
    V8Adapter,
    normalize_legacy_result,
)
from week9.src.schemas import CheckStatus


class VersionAdapterTests(unittest.TestCase):
    def setUp(self):
        self.case = DEVELOPMENT_REGRESSION_CASES["rc_lowpass"]

    def test_legacy_command_uses_isolated_output_and_exact_requirement(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            spec = LegacyAdapter("v4").build_command(
                self.case, Path(temp_dir), run_id="unique-run"
            )
        self.assertIn("week8", spec.command[1])
        self.assertIn("--raw-input", spec.command)
        self.assertIn(self.case.raw_input, spec.command)
        self.assertIn("unique-run", spec.output_path)

    def test_v8_command_exposes_required_single_case_cli(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            spec = V8Adapter().build_command(self.case, Path(temp_dir), run_id="v8-run")
        self.assertIn("--case", spec.command)
        self.assertIn("--out", spec.command)
        self.assertIn("--run-id", spec.command)
        self.assertIn("--raw-input", spec.command)
        self.assertIn(self.case.raw_input, spec.command)
        self.assertIn("--case-sha256", spec.command)
        self.assertIn(self.case.sha256, spec.command)

    def test_v8_holdout_command_requests_controlled_access(self):
        holdout = HOLDOUT_CASES["three_room_thermal_chain"]
        with tempfile.TemporaryDirectory() as temp_dir:
            spec = V8Adapter().build_command(holdout, Path(temp_dir), run_id="holdout-run")
        self.assertIn("--formal-holdout-access", spec.command)

    def test_v8_online_command_selects_guarded_provider_and_run_kind(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            spec = V8Adapter().build_command(
                self.case,
                Path(temp_dir),
                run_id="online-run",
                online_generation=True,
                run_kind="formal",
            )
        self.assertIn("--generation-provider", spec.command)
        self.assertIn("deepseek-online", spec.command)
        self.assertIn("--online-run-kind", spec.command)
        self.assertIn("formal", spec.command)

    def test_legacy_success_is_evidence_not_external_success(self):
        raw = {
            "success": True,
            "sysml_code": "package P { part def A; }",
            "modelica_code": "model A\nend A;",
            "physics_passed": True,
            "parameters": {"R": 1000.0},
            "token_stats": {"total_tokens": 123},
        }
        result = normalize_legacy_result(raw, self.case, "v4", "run-1")
        check_map = {item.check_type: item for item in result.checks}
        self.assertEqual(check_map["legacy.mo_success"].status, CheckStatus.PASS)
        self.assertEqual(check_map["modelica_compile"].status, CheckStatus.UNVERIFIED)
        self.assertFalse(result.overall_success)

    def test_runner_error_is_infrastructure_error(self):
        result = normalize_legacy_result(
            {"success": False, "error": "network timeout"},
            self.case,
            "v6",
            "run-2",
        )
        check_map = {item.check_type: item for item in result.checks}
        self.assertEqual(check_map["adapter.execution"].status, CheckStatus.ERROR)
        self.assertEqual(result.infrastructure_errors, ["network timeout"])

    def test_online_execution_is_guarded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(PermissionError):
                LegacyAdapter("v6").run(self.case, Path(temp_dir))
            with self.assertRaises(PermissionError):
                LegacyAdapter("v6").run(
                    self.case, Path(temp_dir), allow_online=True, run_kind="smoke"
                )

    def test_v8_offline_adapter_is_rechecked_by_external_evaluator(self):
        with tempfile.TemporaryDirectory(prefix="v8-adapter-external-") as temp_dir:
            result = V8Adapter().run(
                self.case,
                Path(temp_dir),
                allow_online=False,
                run_kind="smoke",
            )
        self.assertEqual(
            result.raw_metrics.get("external_evaluator"),
            "week9.cross_version_validation.v1",
        )
        self.assertTrue(result.overall_success)

    def test_unsafe_run_id_cannot_escape_output_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ValueError):
                LegacyAdapter("v4").build_command(
                    self.case, Path(temp_dir), run_id="..\\escape"
                )

    def test_malformed_legacy_fields_become_blocking_adapter_error(self):
        result = normalize_legacy_result(
            {"success": True, "parameters": [], "token_stats": "bad"},
            self.case,
            "v4",
            "malformed-run",
        )
        check_map = {item.check_type: item for item in result.checks}
        self.assertEqual(check_map["adapter.execution"].status, CheckStatus.ERROR)
        self.assertFalse(result.overall_success)


if __name__ == "__main__":
    unittest.main()
