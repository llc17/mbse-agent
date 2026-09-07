from __future__ import annotations

import unittest

from week9.src.repair_loop import (
    ArtifactBundle,
    RepairLoopConfig,
    RepairProposal,
    ValidationSnapshot,
    failure_fingerprint,
    repair_layer_for_check,
    run_repair_loop,
)
from week9.src.schemas import (
    CheckStatus,
    FailureKind,
    InfrastructureRetryOutcome,
    QualityCheckResult,
    RepairAction,
    RepairLayer,
    RepairOutcome,
    RepairStopReason,
)


def _pass(name: str) -> QualityCheckResult:
    return QualityCheckResult(name, CheckStatus.PASS, True)


def _fail(
    name: str,
    *,
    category: str = "content_failure",
    issue: str = "content failure",
    status: CheckStatus = CheckStatus.FAIL,
    kind: FailureKind = FailureKind.CONTENT,
    evidence: dict | None = None,
) -> QualityCheckResult:
    return QualityCheckResult(
        check_type=name,
        status=status,
        required=True,
        issues=[issue],
        evidence={"reason": category, **(evidence or {})},
        failure_kind=kind,
        failure_category=category,
    )


def _snapshot(*checks: QualityCheckResult) -> ValidationSnapshot:
    return ValidationSnapshot(checks=checks)


class _SequenceValidator:
    def __init__(self, *snapshots: ValidationSnapshot):
        self.snapshots = snapshots
        self.calls: list[ArtifactBundle] = []

    def __call__(self, artifacts: ArtifactBundle) -> ValidationSnapshot:
        self.calls.append(artifacts)
        index = min(len(self.calls) - 1, len(self.snapshots) - 1)
        return self.snapshots[index]


class RepairLoopStateMachineTests(unittest.TestCase):
    def setUp(self):
        self.initial = ArtifactBundle("requirement", "sysml", "bad-modelica")

    def test_initial_pass_has_one_validation_and_no_repairs(self):
        result = run_repair_loop(
            self.initial,
            lambda _: _snapshot(_pass("modelica_compile")),
            lambda _: self.fail("provider must not be called"),
        )
        self.assertTrue(result.overall_success)
        self.assertEqual(result.stop_reason, RepairStopReason.ALL_CHECKS_PASS)
        self.assertEqual(result.counters.validation_count, 1)
        self.assertEqual(result.counters.repair_attempt_count, 0)
        self.assertEqual(result.counters.infrastructure_retry_count, 0)

    def test_one_real_modelica_fix_is_committed(self):
        def validator(artifacts):
            return _snapshot(
                _pass("modelica_compile")
                if artifacts.modelica == "fixed-modelica"
                else _fail("modelica_compile", category="syntax_error")
            )

        result = run_repair_loop(
            self.initial,
            validator,
            lambda request: RepairProposal(
                "fixed-modelica", metadata={"action": request.action.value}
            ),
        )
        self.assertEqual(result.final_artifacts.modelica, "fixed-modelica")
        self.assertEqual(result.stop_reason, RepairStopReason.ALL_CHECKS_PASS)
        self.assertEqual(
            (
                result.counters.repair_attempt_count,
                result.counters.content_repair_count,
                result.counters.accepted_content_repair_count,
                result.counters.validation_count,
            ),
            (1, 1, 1, 2),
        )
        self.assertEqual(result.repairs[0].action, RepairAction.RESTORE_CERTIFIED_TEMPLATE)
        self.assertEqual(result.repairs[0].outcome, RepairOutcome.PASSED)

    def test_provider_returning_original_text_is_a_no_op(self):
        result = run_repair_loop(
            self.initial,
            lambda _: _snapshot(_fail("modelica_compile", category="syntax_error")),
            lambda request: RepairProposal(request.artifact_before),
        )
        self.assertEqual(result.stop_reason, RepairStopReason.NO_OP)
        self.assertEqual(result.counters.repair_attempt_count, 1)
        self.assertEqual(result.counters.content_repair_count, 0)
        self.assertEqual(result.counters.no_op_count, 1)
        self.assertEqual(result.counters.validation_count, 1)

    def test_repeated_content_error_requires_two_changed_attempts(self):
        proposals: list[str] = []

        def provider(request):
            text = f"changed-modelica-{request.attempt}"
            proposals.append(text)
            return RepairProposal(text)

        result = run_repair_loop(
            self.initial,
            lambda _: _snapshot(_fail("modelica_compile", category="syntax_error")),
            provider,
            RepairLoopConfig(max_repairs=5, max_content_repairs=5),
        )
        self.assertEqual(len(proposals), 2)
        self.assertEqual(result.stop_reason, RepairStopReason.REPEATED_ERROR)
        self.assertEqual(result.final_artifacts, self.initial)
        self.assertEqual(
            (
                result.counters.repair_attempt_count,
                result.counters.content_repair_count,
                result.counters.accepted_content_repair_count,
                result.counters.rollback_count,
                result.counters.repeated_error_count,
                result.counters.validation_count,
            ),
            (2, 2, 0, 2, 1, 3),
        )
        self.assertTrue(all(record.rolled_back for record in result.repairs))

    def test_new_blocker_rolls_back_as_regression(self):
        baseline = _snapshot(_fail("modelica_compile"), _pass("guard"))
        regressed = _snapshot(_pass("modelica_compile"), _fail("guard"))
        validator = _SequenceValidator(baseline, regressed)
        result = run_repair_loop(
            self.initial,
            validator,
            lambda _: RepairProposal("candidate"),
        )
        self.assertEqual(result.stop_reason, RepairStopReason.REGRESSION)
        self.assertEqual(result.final_artifacts, self.initial)
        self.assertEqual(result.counters.rollback_count, 1)
        self.assertFalse(result.repairs[0].accepted)

    def test_same_check_with_more_hard_issues_is_regression(self):
        baseline = _snapshot(_fail("modelica_compile", issue="one"))
        regressed = _snapshot(
            QualityCheckResult(
                "modelica_compile",
                CheckStatus.FAIL,
                True,
                issues=["one", "two"],
                evidence={"reason": "content_failure"},
                failure_kind=FailureKind.CONTENT,
                failure_category="content_failure",
            )
        )
        result = run_repair_loop(
            self.initial,
            _SequenceValidator(baseline, regressed),
            lambda _: RepairProposal("candidate"),
        )
        self.assertEqual(result.stop_reason, RepairStopReason.REGRESSION)
        self.assertEqual(result.final_artifacts, self.initial)

    def test_mixed_infrastructure_and_content_retries_before_repairing(self):
        calls: list[RepairLayer] = []
        snapshots = _SequenceValidator(
            _snapshot(
                _fail(
                    "modelica_compile",
                    category="file_lock",
                    status=CheckStatus.ERROR,
                    kind=FailureKind.INFRASTRUCTURE,
                ),
                _fail("sysml_syntax", category="missing_sysml"),
            ),
            _snapshot(_pass("modelica_compile"), _fail("sysml_syntax")),
            _snapshot(_pass("modelica_compile"), _pass("sysml_syntax")),
        )

        def provider(request):
            calls.append(request.repair_layer)
            return RepairProposal("fixed-sysml")

        result = run_repair_loop(self.initial, snapshots, provider)
        self.assertEqual(calls, [RepairLayer.SYSML])
        self.assertEqual(result.infrastructure_retries[0].outcome, InfrastructureRetryOutcome.RESOLVED)
        self.assertEqual(result.counters.infrastructure_retry_count, 1)
        self.assertEqual(result.counters.upstream_redo_count, 1)
        self.assertEqual(result.counters.validation_count, 3)

    def test_persistent_infrastructure_failure_uses_exact_budget(self):
        provider_calls = 0

        def provider(_):
            nonlocal provider_calls
            provider_calls += 1
            return RepairProposal("must-not-run")

        result = run_repair_loop(
            self.initial,
            lambda _: _snapshot(
                _fail(
                    "modelica_compile",
                    category="file_lock",
                    status=CheckStatus.ERROR,
                    kind=FailureKind.INFRASTRUCTURE,
                )
            ),
            provider,
            RepairLoopConfig(max_infrastructure_retries=2),
        )
        self.assertEqual(provider_calls, 0)
        self.assertEqual(
            result.stop_reason, RepairStopReason.INFRASTRUCTURE_RETRY_EXHAUSTED
        )
        self.assertEqual(result.counters.infrastructure_retry_count, 2)
        self.assertEqual(result.counters.validation_count, 3)
        self.assertEqual(result.counters.repeated_error_count, 0)
        self.assertEqual(
            result.infrastructure_retries[-1].outcome,
            InfrastructureRetryOutcome.EXHAUSTED,
        )

    def test_changed_candidate_error_then_recovers_before_commit(self):
        validator = _SequenceValidator(
            _snapshot(_fail("modelica_compile", category="syntax_error")),
            _snapshot(
                _fail(
                    "modelica_compile",
                    category="file_lock",
                    status=CheckStatus.ERROR,
                    kind=FailureKind.INFRASTRUCTURE,
                )
            ),
            _snapshot(_pass("modelica_compile")),
        )
        result = run_repair_loop(
            self.initial,
            validator,
            lambda _: RepairProposal("fixed-after-retry"),
        )
        self.assertTrue(result.overall_success)
        self.assertEqual(result.final_artifacts.modelica, "fixed-after-retry")
        self.assertEqual(
            (
                result.counters.content_repair_count,
                result.counters.accepted_content_repair_count,
                result.counters.infrastructure_retry_count,
                result.counters.validation_count,
            ),
            (1, 1, 1, 3),
        )

    def test_changed_candidate_persistent_error_rolls_back_committed_snapshot(self):
        initial_failure = _snapshot(
            _fail("modelica_compile", category="syntax_error")
        )
        infrastructure = _snapshot(
            _fail(
                "modelica_compile",
                category="file_lock",
                status=CheckStatus.ERROR,
                kind=FailureKind.INFRASTRUCTURE,
            )
        )
        result = run_repair_loop(
            self.initial,
            _SequenceValidator(initial_failure, infrastructure, infrastructure, infrastructure),
            lambda _: RepairProposal("pending-candidate"),
            RepairLoopConfig(max_infrastructure_retries=2),
        )
        self.assertEqual(
            result.stop_reason, RepairStopReason.INFRASTRUCTURE_RETRY_EXHAUSTED
        )
        self.assertEqual(result.final_artifacts, self.initial)
        self.assertEqual(result.final_validation, initial_failure)
        self.assertEqual(
            (
                result.counters.repair_attempt_count,
                result.counters.content_repair_count,
                result.counters.accepted_content_repair_count,
                result.counters.rollback_count,
                result.counters.infrastructure_retry_count,
                result.counters.validation_count,
            ),
            (1, 1, 0, 1, 2, 4),
        )

    def test_partial_improvement_is_committed_then_content_budget_stops(self):
        baseline = _snapshot(
            _fail("modelica_compile", category="syntax_error"),
            _fail("physical_behavior", category="physical_assertion_failed"),
        )
        improved = _snapshot(
            _pass("modelica_compile"),
            _fail("physical_behavior", category="physical_assertion_failed"),
        )
        result = run_repair_loop(
            self.initial,
            _SequenceValidator(baseline, improved),
            lambda _: RepairProposal("partially-fixed"),
            RepairLoopConfig(max_content_repairs=1),
        )
        self.assertEqual(result.stop_reason, RepairStopReason.MAX_REPAIRS)
        self.assertEqual(result.final_artifacts.modelica, "partially-fixed")
        self.assertEqual(result.counters.accepted_content_repair_count, 1)
        self.assertEqual(result.counters.validation_count, 2)

    def test_missing_required_revalidation_check_cannot_be_accepted(self):
        baseline = _snapshot(
            _fail("modelica_compile"),
            _pass("physical_behavior"),
        )
        omitted = _snapshot(_pass("modelica_compile"))
        result = run_repair_loop(
            self.initial,
            _SequenceValidator(baseline, omitted),
            lambda _: RepairProposal("candidate"),
            RepairLoopConfig(max_infrastructure_retries=0),
        )
        self.assertEqual(
            result.stop_reason, RepairStopReason.INFRASTRUCTURE_RETRY_EXHAUSTED
        )
        self.assertFalse(result.repairs[0].accepted)
        checks = {check.check_type: check for check in result.repairs[0].revalidation_results}
        self.assertEqual(checks["physical_behavior"].status, CheckStatus.ERROR)

    def test_missing_csv_is_reexecuted_without_content_provider(self):
        provider_calls = 0

        def provider(_):
            nonlocal provider_calls
            provider_calls += 1
            return RepairProposal("must-not-run")

        result = run_repair_loop(
            self.initial,
            _SequenceValidator(
                _snapshot(
                    _fail(
                        "simulation",
                        category="simulation_csv_missing",
                        status=CheckStatus.UNVERIFIED,
                    )
                ),
                _snapshot(_pass("simulation")),
            ),
            provider,
        )
        self.assertEqual(provider_calls, 0)
        self.assertTrue(result.overall_success)
        self.assertEqual(result.counters.infrastructure_retry_count, 1)

    def test_execution_provenance_pass_resolves_one_infrastructure_retry(self):
        result = run_repair_loop(
            self.initial,
            _SequenceValidator(
                _snapshot(
                    _fail(
                        "execution_provenance",
                        category="execution_provenance_unavailable",
                        status=CheckStatus.ERROR,
                        kind=FailureKind.INFRASTRUCTURE,
                    )
                ),
                _snapshot(_pass("execution_provenance")),
            ),
            lambda _: self.fail("content provider must not be called"),
            RepairLoopConfig(max_infrastructure_retries=1),
        )
        self.assertTrue(result.overall_success)
        self.assertEqual(result.stop_reason, RepairStopReason.ALL_CHECKS_PASS)
        self.assertEqual(result.counters.infrastructure_retry_count, 1)
        self.assertEqual(
            result.infrastructure_retries[0].outcome,
            InfrastructureRetryOutcome.RESOLVED,
        )

    def test_blocked_physics_does_not_hide_compile_content_repair(self):
        baseline = _snapshot(
            _fail("modelica_compile", category="modelica_build_failed"),
            _fail(
                "physical_behavior",
                category="simulation_evidence_unavailable",
                status=CheckStatus.UNVERIFIED,
            ),
        )
        fixed = _snapshot(_pass("modelica_compile"), _pass("physical_behavior"))
        result = run_repair_loop(
            self.initial,
            _SequenceValidator(baseline, fixed),
            lambda _: RepairProposal("fixed-modelica"),
        )
        self.assertTrue(result.overall_success)
        self.assertEqual(result.counters.infrastructure_retry_count, 0)
        self.assertEqual(result.counters.content_repair_count, 1)

    def test_template_anchor_failure_requires_recertification_not_patch(self):
        provider_calls = 0

        def provider(_):
            nonlocal provider_calls
            provider_calls += 1
            return RepairProposal("must-not-run")

        result = run_repair_loop(
            self.initial,
            lambda _: _snapshot(
                _fail(
                    "template_certification_anchor",
                    category="template_recertification_required",
                )
            ),
            provider,
        )
        self.assertEqual(provider_calls, 0)
        self.assertEqual(result.stop_reason, RepairStopReason.UNREPAIRABLE)

    def test_process_tree_cleanup_failure_stops_before_any_content_change(self):
        provider_calls = 0

        def provider(_):
            nonlocal provider_calls
            provider_calls += 1
            return RepairProposal("must-not-run")

        result = run_repair_loop(
            self.initial,
            lambda _: _snapshot(
                _fail("sysml_syntax", category="missing_sysml"),
                _fail(
                    "modelica_compile",
                    category="process_tree_cleanup_failed",
                    status=CheckStatus.ERROR,
                    kind=FailureKind.INFRASTRUCTURE,
                ),
            ),
            provider,
        )
        self.assertEqual(provider_calls, 0)
        self.assertEqual(result.stop_reason, RepairStopReason.UNREPAIRABLE)

    def test_failure_fingerprint_ignores_unique_run_paths_and_timestamps(self):
        first = _fail(
            "modelica_compile",
            category="syntax_error",
            issue=(
                "[D:\\tmp\\run-123\\Model.mo:4:2-4:7] Error at "
                "2026-08-29T12:01:02Z"
            ),
        )
        second = _fail(
            "modelica_compile",
            category="syntax_error",
            issue=(
                "[D:\\elsewhere\\run-999\\Model.mo:40:20-40:70] Error at "
                "2026-08-30T09:08:07Z"
            ),
        )
        self.assertEqual(failure_fingerprint(first), failure_fingerprint(second))

    def test_complete_snapshots_and_tail_diff_are_not_truncated(self):
        before = "head\n" + ("x" * 1000) + "\nTAIL_BEFORE\n"
        after = "head\n" + ("x" * 1000) + "\nTAIL_AFTER\n"
        initial = ArtifactBundle("requirement", "sysml", before)
        result = run_repair_loop(
            initial,
            lambda artifacts: _snapshot(
                _pass("modelica_compile")
                if artifacts.modelica == after
                else _fail("modelica_compile")
            ),
            lambda _: RepairProposal(after),
        )
        record = result.repairs[0]
        self.assertEqual(record.artifact_before, before)
        self.assertEqual(record.artifact_after, after)
        self.assertIn("TAIL_BEFORE", record.diff)
        self.assertIn("TAIL_AFTER", record.diff)

    def test_cross_and_physics_can_route_from_owner_evidence(self):
        cross = _fail(
            "cross_model_consistency",
            evidence={"owner_layer": "modelica"},
        )
        physics = _fail(
            "physical_behavior",
            evidence={"owner_layer": "requirement"},
        )
        self.assertEqual(repair_layer_for_check(cross), RepairLayer.MODELICA)
        self.assertEqual(repair_layer_for_check(physics), RepairLayer.REQUIREMENT)


if __name__ == "__main__":
    unittest.main()
