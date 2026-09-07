from __future__ import annotations

import unittest

from week9.src.quality_gate import decide
from week9.src.schemas import CheckStatus, FailureKind, QualityCheckResult


def check(name: str, status: CheckStatus, *, required: bool = True):
    evidence = {"reason": "not needed"} if status is CheckStatus.NOT_APPLICABLE else {}
    failed = status in {CheckStatus.FAIL, CheckStatus.UNVERIFIED, CheckStatus.ERROR}
    return QualityCheckResult(
        check_type=name,
        status=status,
        required=required,
        issues=["failed"] if failed else [],
        evidence=evidence,
        failure_kind=FailureKind.CONTENT if failed else FailureKind.NONE,
    )


class QualityGateTests(unittest.TestCase):
    def test_all_required_pass_releases(self):
        decision = decide(
            [
                check("sysml", CheckStatus.PASS),
                check("compile", CheckStatus.PASS),
                check("judge", CheckStatus.ERROR, required=False),
            ]
        )
        self.assertTrue(decision.overall_success)
        self.assertEqual(decision.blocking_checks, ())

    def test_every_non_pass_state_blocks_when_required(self):
        for status in (
            CheckStatus.FAIL,
            CheckStatus.UNVERIFIED,
            CheckStatus.ERROR,
        ):
            with self.subTest(status=status):
                decision = decide([check("compile", status)])
                self.assertFalse(decision.overall_success)
                self.assertEqual(decision.blocking_checks, ("compile",))

    def test_required_not_applicable_is_rejected_by_contract(self):
        with self.assertRaises(ValueError):
            check("compile", CheckStatus.NOT_APPLICABLE)

    def test_no_required_checks_is_unverified_not_success(self):
        decision = decide([check("judge", CheckStatus.PASS, required=False)])
        self.assertFalse(decision.overall_success)
        self.assertEqual(decision.primary_failure_category, "quality_gate_configuration")

    def test_duplicate_check_types_are_rejected(self):
        with self.assertRaises(ValueError):
            decide(
                [
                    check("compile", CheckStatus.PASS),
                    check("compile", CheckStatus.FAIL),
                ]
            )

    def test_content_failure_is_primary_before_checker_error(self):
        decision = decide(
            [
                check("validator", CheckStatus.ERROR),
                check("compile", CheckStatus.FAIL),
            ]
        )
        self.assertEqual(decision.blocking_checks[0], "compile")


if __name__ == "__main__":
    unittest.main()
