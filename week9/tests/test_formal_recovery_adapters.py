from __future__ import annotations

from pathlib import Path
import unittest

from week9.experiments.cases import CaseDefinition, DEVELOPMENT_REGRESSION_CASES
from week9.experiments.formal_recovery_adapters import (
    ARM_SPEC,
    build_recovery_adapter_command,
    run_development_recovery_arm,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class FormalRecoveryAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = DEVELOPMENT_REGRESSION_CASES["rc_lowpass"]
        self.output_root = PROJECT_ROOT / "week9" / "results" / "command-build-only"

    def test_all_four_frozen_arm_commands_build_without_execution(self):
        commands = {
            arm_id: build_recovery_adapter_command(
                arm_id,
                self.case,
                self.output_root,
            )
            for arm_id in ARM_SPEC
        }
        self.assertEqual(set(commands), set(ARM_SPEC))
        self.assertIn("--version", commands["v4_default_deepseek"].command)
        self.assertIn("v4", commands["v4_default_deepseek"].command)
        self.assertIn("v6", commands["v6_default_deepseek"].command)
        self.assertIn(
            "deepseek-online",
            commands["v8_controlled_deepseek"].command,
        )
        self.assertNotIn(
            "deepseek-online",
            commands["v8_default_local"].command,
        )
        self.assertTrue(
            all("--formal-holdout-access" not in item.command for item in commands.values())
        )

    def test_paid_arms_are_blocked_before_process_launch(self):
        for arm_id in (
            "v4_default_deepseek",
            "v6_default_deepseek",
            "v8_controlled_deepseek",
        ):
            with self.subTest(arm_id=arm_id):
                with self.assertRaisesRegex(PermissionError, "blocks every paid arm"):
                    run_development_recovery_arm(
                        arm_id,
                        self.case,
                        self.output_root,
                    )

    def test_holdout_shaped_case_is_blocked_before_command_build(self):
        synthetic_holdout = CaseDefinition(
            case_id="synthetic_unseen_holdout",
            dataset="holdout",
            domain="synthetic",
            raw_input="content intentionally unavailable",
        )
        with self.assertRaisesRegex(PermissionError, "development cases only"):
            build_recovery_adapter_command(
                "v8_default_local",
                synthetic_holdout,
                self.output_root,
            )

    def test_unknown_arm_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported recovery arm"):
            build_recovery_adapter_command(
                "unknown_arm",
                self.case,
                self.output_root,
            )


if __name__ == "__main__":
    unittest.main()
