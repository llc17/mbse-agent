from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from week9.experiments.formal_recovery import (
    AttemptDisposition,
    AttemptObservation,
    CandidateContractStatus,
    ExternalEvaluationStatus,
    FORMAL_ARM_IDS,
    FORMAL_DEADLINE_S,
    TerminationReason,
    classify_attempt,
    launch_failure_observation,
    recovery_policy_manifest,
)
from week9.experiments.formal_recovery_harness import RecoveryAttemptSpec
from week9.experiments.freeze_formal_recovery_protocol import (
    DEFAULT_PROTOCOL,
    validate_recovery_protocol,
)
from week9.experiments.run_formal_recovery_preflight import run_preflight


def _completed(
    *,
    attempt_id: str = "completed",
    returncode: int = 0,
    contract: CandidateContractStatus = CandidateContractStatus.VALID,
    evaluation: ExternalEvaluationStatus = ExternalEvaluationStatus.PASS,
) -> AttemptObservation:
    return AttemptObservation(
        attempt_id=attempt_id,
        termination_reason=TerminationReason.COMPLETED,
        process_started=True,
        process_returncode=returncode,
        candidate_contract_status=contract,
        external_evaluation_status=evaluation,
        cleanup_attempted=False,
        cleanup_succeeded=None,
        duration_s=1.0,
    )


class FormalRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.recovery_protocol = json.loads(
            DEFAULT_PROTOCOL.read_text(encoding="utf-8")
        )

    def test_external_pass_is_valid_success(self):
        decision = classify_attempt(_completed())
        self.assertEqual(decision.disposition, AttemptDisposition.VALID_SUCCESS)
        self.assertTrue(decision.counts_as_completed_run)
        self.assertTrue(decision.overall_success)
        self.assertFalse(decision.infrastructure_failure)

    def test_external_pass_is_source_of_truth_for_valid_nonzero_contract(self):
        decision = classify_attempt(_completed(returncode=2))
        self.assertEqual(decision.disposition, AttemptDisposition.VALID_SUCCESS)
        self.assertTrue(decision.counts_as_completed_run)
        self.assertTrue(decision.overall_success)
        self.assertFalse(decision.infrastructure_failure)

    def test_external_hard_fail_is_valid_arm_failure(self):
        decision = classify_attempt(
            _completed(evaluation=ExternalEvaluationStatus.FAIL)
        )
        self.assertEqual(
            decision.disposition, AttemptDisposition.VALID_ARM_FAILURE
        )
        self.assertEqual(decision.failure_category, "external_evaluation_failed")
        self.assertTrue(decision.counts_as_completed_run)
        self.assertFalse(decision.infrastructure_failure)

    def test_preregistered_deadline_with_cleanup_is_valid_arm_failure(self):
        observation = AttemptObservation(
            attempt_id="deadline-clean",
            termination_reason=TerminationReason.DEADLINE_EXCEEDED,
            process_started=True,
            process_returncode=1,
            candidate_contract_status=CandidateContractStatus.NOT_APPLICABLE,
            external_evaluation_status=ExternalEvaluationStatus.NOT_RUN,
            cleanup_attempted=True,
            cleanup_succeeded=True,
            duration_s=FORMAL_DEADLINE_S,
            deadline_s=FORMAL_DEADLINE_S,
        )
        decision = classify_attempt(observation)
        self.assertEqual(
            decision.disposition, AttemptDisposition.VALID_ARM_FAILURE
        )
        self.assertEqual(decision.failure_category, "arm_deadline_exceeded")
        self.assertTrue(decision.counts_as_completed_run)
        self.assertFalse(decision.infrastructure_failure)

    def test_deadline_cleanup_failure_invalidates_batch(self):
        observation = AttemptObservation(
            attempt_id="deadline-dirty",
            termination_reason=TerminationReason.DEADLINE_EXCEEDED,
            process_started=True,
            process_returncode=None,
            candidate_contract_status=CandidateContractStatus.NOT_APPLICABLE,
            external_evaluation_status=ExternalEvaluationStatus.NOT_RUN,
            cleanup_attempted=True,
            cleanup_succeeded=False,
            duration_s=FORMAL_DEADLINE_S,
            deadline_s=FORMAL_DEADLINE_S,
        )
        decision = classify_attempt(observation)
        self.assertEqual(
            decision.disposition, AttemptDisposition.INVALID_INFRASTRUCTURE
        )
        self.assertEqual(decision.failure_category, "process_tree_cleanup_failed")
        self.assertFalse(decision.counts_as_completed_run)
        self.assertTrue(decision.infrastructure_failure)

    def test_missing_invalid_contract_and_nonzero_exit_are_arm_failures(self):
        observations = (
            _completed(
                attempt_id="missing",
                contract=CandidateContractStatus.MISSING,
                evaluation=ExternalEvaluationStatus.NOT_RUN,
            ),
            _completed(
                attempt_id="invalid",
                contract=CandidateContractStatus.INVALID,
                evaluation=ExternalEvaluationStatus.NOT_RUN,
            ),
            _completed(
                attempt_id="nonzero",
                returncode=2,
                contract=CandidateContractStatus.MISSING,
                evaluation=ExternalEvaluationStatus.NOT_RUN,
            ),
        )
        expected = (
            "candidate_contract_missing",
            "candidate_contract_invalid",
            "arm_process_nonzero_exit",
        )
        for observation, category in zip(observations, expected, strict=True):
            with self.subTest(observation=observation.attempt_id):
                decision = classify_attempt(observation)
                self.assertEqual(
                    decision.disposition, AttemptDisposition.VALID_ARM_FAILURE
                )
                self.assertEqual(decision.failure_category, category)

    def test_launch_harness_and_evaluator_errors_are_infrastructure(self):
        observations = (
            launch_failure_observation(attempt_id="launch", detail="injected"),
            AttemptObservation(
                attempt_id="harness",
                termination_reason=TerminationReason.HARNESS_ERROR,
                process_started=True,
                process_returncode=None,
                candidate_contract_status=CandidateContractStatus.NOT_APPLICABLE,
                external_evaluation_status=ExternalEvaluationStatus.ERROR,
                cleanup_attempted=False,
                cleanup_succeeded=None,
                duration_s=0.1,
            ),
            _completed(evaluation=ExternalEvaluationStatus.ERROR),
            _completed(evaluation=ExternalEvaluationStatus.NOT_RUN),
        )
        for observation in observations:
            with self.subTest(observation=observation.attempt_id):
                decision = classify_attempt(observation)
                self.assertEqual(
                    decision.disposition,
                    AttemptDisposition.INVALID_INFRASTRUCTURE,
                )
                self.assertFalse(decision.counts_as_completed_run)
                self.assertTrue(decision.infrastructure_failure)

    def test_contradictory_observation_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "cleanup_succeeded"):
            AttemptObservation(
                attempt_id="bad-cleanup",
                termination_reason=TerminationReason.COMPLETED,
                process_started=True,
                process_returncode=0,
                candidate_contract_status=CandidateContractStatus.VALID,
                external_evaluation_status=ExternalEvaluationStatus.PASS,
                cleanup_attempted=False,
                cleanup_succeeded=True,
                duration_s=1.0,
            )
        with self.assertRaisesRegex(ValueError, "requires a valid candidate"):
            _completed(
                attempt_id="bad-evaluation",
                contract=CandidateContractStatus.MISSING,
                evaluation=ExternalEvaluationStatus.PASS,
            )

    def test_policy_manifest_is_global_and_cannot_reclassify_old_attempt(self):
        policy = recovery_policy_manifest()
        self.assertEqual(policy["applies_to_all_arms"], list(FORMAL_ARM_IDS))
        self.assertEqual(policy["formal_deadline_s"], 900.0)
        self.assertFalse(policy["old_attempt_reclassification_allowed"])
        self.assertTrue(policy["future_independently_sealed_holdout_only"])

    def test_current_recovery_protocol_is_valid_and_offline_only(self):
        self.assertEqual(validate_recovery_protocol(self.recovery_protocol), [])
        self.assertFalse(self.recovery_protocol["formal_online_runs_allowed"])
        self.assertFalse(self.recovery_protocol["formal_judge_allowed"])
        self.assertFalse(
            self.recovery_protocol["new_holdout_gate"]["access_enabled"]
        )

    def test_recovery_protocol_cannot_unlock_online_or_holdout_access(self):
        protocol = deepcopy(self.recovery_protocol)
        protocol["formal_online_runs_allowed"] = True
        protocol["new_holdout_gate"]["access_enabled"] = True
        protocol["online_execution_gate"]["user_confirmation_id"] = "reused-id"
        errors = validate_recovery_protocol(protocol)
        self.assertTrue(any("cannot enable paid" in item for item in errors))
        self.assertTrue(any("holdout gate" in item for item in errors))
        self.assertTrue(any("cannot record or reuse" in item for item in errors))

    def test_recovery_protocol_cannot_reclassify_or_reuse_old_evidence(self):
        protocol = deepcopy(self.recovery_protocol)
        protocol["baseline_boundary"]["old_attempt_reclassification_allowed"] = True
        protocol["retired_attempt_markers"]["records_reusable"] = True
        errors = validate_recovery_protocol(protocol)
        self.assertTrue(any("immutability boundary" in item for item in errors))
        self.assertTrue(any("retired attempt" in item for item in errors))

    def test_recovery_attempt_spec_rejects_workspace_escape(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            with self.assertRaisesRegex(ValueError, "escapes workspace_root"):
                RecoveryAttemptSpec.build(
                    attempt_id="escape",
                    command=["safe-command"],
                    cwd=root,
                    workspace_root=root,
                    candidate_contract_path=root.parent / "escaped.json",
                )

    def test_offline_preflight_writes_verified_pass_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            report, result_dir = run_preflight(output_root=Path(temp_dir))
            report_path = result_dir / "formal-recovery-preflight-report.json"
            sidecar = report_path.with_suffix(".sha256")
            expected = sidecar.read_text(encoding="ascii").strip().split()[0]
            actual = hashlib.sha256(report_path.read_bytes()).hexdigest()
            self.assertEqual(expected, actual)
        self.assertEqual(report["verdict"], "RECOVERY_PREFLIGHT_PASS")
        self.assertEqual(report["completed_scenario_count"], 11)
        self.assertTrue(all(report["pass_conditions"].values()))
        self.assertTrue(report["guard"]["synthetic_fault_injection_only"])
        self.assertFalse(report["guard"]["external_network_allowed"])
        self.assertFalse(report["guard"]["old_holdout_accessed"])
        self.assertFalse(report["guard"]["new_holdout_accessed"])


if __name__ == "__main__":
    unittest.main()
