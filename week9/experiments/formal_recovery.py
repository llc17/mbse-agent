"""Offline-only outcome policy for a future, independent V8 formal experiment.

The original V8 experiment and its consumed holdout remain immutable.  This
module defines a new outer attempt envelope for a future protocol: an arm that
reaches a preregistered deadline is an observed arm failure when process-tree
cleanup succeeds, while harness failures still invalidate the batch.

Nothing in this module reads datasets, invokes model providers, or changes an
existing ``RunResult``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import math
import re
from typing import Any

from week9.experiments.subprocess_control import ControlledProcessResult


FORMAL_DEADLINE_S = 900.0
FORMAL_ARM_IDS = (
    "v4_default_deepseek",
    "v6_default_deepseek",
    "v8_controlled_deepseek",
    "v8_default_local",
)
_SAFE_ATTEMPT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class TerminationReason(str, Enum):
    COMPLETED = "completed"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    PROCESS_LAUNCH_FAILED = "process_launch_failed"
    HARNESS_ERROR = "harness_error"


class CandidateContractStatus(str, Enum):
    VALID = "valid"
    MISSING = "missing"
    INVALID = "invalid"
    NOT_APPLICABLE = "not_applicable"


class ExternalEvaluationStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NOT_RUN = "not_run"
    ERROR = "error"


class AttemptDisposition(str, Enum):
    VALID_SUCCESS = "valid_success"
    VALID_ARM_FAILURE = "valid_arm_failure"
    INVALID_INFRASTRUCTURE = "invalid_infrastructure"


@dataclass(frozen=True, slots=True)
class AttemptObservation:
    """Structured facts captured by the outer experiment harness."""

    attempt_id: str
    termination_reason: TerminationReason
    process_started: bool
    process_returncode: int | None
    candidate_contract_status: CandidateContractStatus
    external_evaluation_status: ExternalEvaluationStatus
    cleanup_attempted: bool
    cleanup_succeeded: bool | None
    duration_s: float
    deadline_s: float | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if not _SAFE_ATTEMPT_ID.fullmatch(self.attempt_id) or ".." in self.attempt_id:
            raise ValueError(f"unsafe attempt_id: {self.attempt_id!r}")
        for value, expected, name in (
            (self.termination_reason, TerminationReason, "termination_reason"),
            (
                self.candidate_contract_status,
                CandidateContractStatus,
                "candidate_contract_status",
            ),
            (
                self.external_evaluation_status,
                ExternalEvaluationStatus,
                "external_evaluation_status",
            ),
        ):
            if not isinstance(value, expected):
                raise TypeError(f"{name} must be {expected.__name__}")
        if type(self.process_started) is not bool:
            raise TypeError("process_started must be a bool")
        if self.process_returncode is not None and type(self.process_returncode) is not int:
            raise TypeError("process_returncode must be an int or None")
        if type(self.cleanup_attempted) is not bool:
            raise TypeError("cleanup_attempted must be a bool")
        if self.cleanup_succeeded is not None and type(self.cleanup_succeeded) is not bool:
            raise TypeError("cleanup_succeeded must be a bool or None")
        if self.cleanup_attempted != (self.cleanup_succeeded is not None):
            raise ValueError(
                "cleanup_succeeded must be recorded exactly when cleanup is attempted"
            )
        if type(self.duration_s) not in {int, float} or not math.isfinite(
            float(self.duration_s)
        ) or self.duration_s < 0:
            raise ValueError("duration_s must be finite and non-negative")
        if self.deadline_s is not None and (
            type(self.deadline_s) not in {int, float}
            or not math.isfinite(float(self.deadline_s))
            or self.deadline_s <= 0
        ):
            raise ValueError("deadline_s must be finite and positive when recorded")
        if len(self.detail) > 2000:
            raise ValueError("detail exceeds the audit envelope limit")

        if self.termination_reason is TerminationReason.COMPLETED:
            if not self.process_started or self.process_returncode is None:
                raise ValueError("completed observation requires a process return code")
            if self.cleanup_attempted or self.deadline_s is not None:
                raise ValueError("completed observation cannot carry deadline cleanup")
        elif self.termination_reason is TerminationReason.DEADLINE_EXCEEDED:
            if not self.process_started or self.deadline_s is None:
                raise ValueError("deadline observation requires a started process and limit")
            if not self.cleanup_attempted:
                raise ValueError("deadline observation requires process-tree cleanup evidence")
            if self.external_evaluation_status is ExternalEvaluationStatus.PASS:
                raise ValueError("a deadline observation cannot pass external evaluation")
        elif self.termination_reason is TerminationReason.PROCESS_LAUNCH_FAILED:
            if self.process_started or self.process_returncode is not None:
                raise ValueError("launch failure cannot report a started process")
            if self.cleanup_attempted or self.deadline_s is not None:
                raise ValueError("launch failure cannot carry cleanup or deadline evidence")
            if (
                self.candidate_contract_status
                is not CandidateContractStatus.NOT_APPLICABLE
                or self.external_evaluation_status
                is not ExternalEvaluationStatus.NOT_RUN
            ):
                raise ValueError("launch failure cannot have candidate or evaluator output")
        elif self.termination_reason is TerminationReason.HARNESS_ERROR:
            if self.external_evaluation_status is ExternalEvaluationStatus.PASS:
                raise ValueError("harness error cannot pass external evaluation")

        if self.external_evaluation_status in {
            ExternalEvaluationStatus.PASS,
            ExternalEvaluationStatus.FAIL,
        } and self.candidate_contract_status is not CandidateContractStatus.VALID:
            raise ValueError("external PASS/FAIL requires a valid candidate contract")
        if self.candidate_contract_status in {
            CandidateContractStatus.MISSING,
            CandidateContractStatus.INVALID,
            CandidateContractStatus.NOT_APPLICABLE,
        } and self.external_evaluation_status not in {
            ExternalEvaluationStatus.NOT_RUN,
            ExternalEvaluationStatus.ERROR,
        }:
            raise ValueError("candidate without a valid contract cannot be evaluated")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["termination_reason"] = self.termination_reason.value
        value["candidate_contract_status"] = self.candidate_contract_status.value
        value["external_evaluation_status"] = self.external_evaluation_status.value
        return value


@dataclass(frozen=True, slots=True)
class AttemptDecision:
    disposition: AttemptDisposition
    counts_as_completed_run: bool
    overall_success: bool
    infrastructure_failure: bool
    failure_category: str
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["disposition"] = self.disposition.value
        return value


def classify_attempt(observation: AttemptObservation) -> AttemptDecision:
    """Classify one outer attempt without inspecting requirement or holdout data."""

    if observation.termination_reason in {
        TerminationReason.PROCESS_LAUNCH_FAILED,
        TerminationReason.HARNESS_ERROR,
    }:
        return _infrastructure_decision(
            "process_launch_failed"
            if observation.termination_reason is TerminationReason.PROCESS_LAUNCH_FAILED
            else "experiment_harness_error",
            "The experiment harness could not produce an auditable arm observation.",
        )

    if observation.termination_reason is TerminationReason.DEADLINE_EXCEEDED:
        if observation.cleanup_succeeded is not True:
            return _infrastructure_decision(
                "process_tree_cleanup_failed",
                "A deadline was observed but descendant cleanup was not certified.",
            )
        return _arm_failure_decision(
            "arm_deadline_exceeded",
            "The arm reached the preregistered deadline and cleanup succeeded.",
        )

    if observation.external_evaluation_status is ExternalEvaluationStatus.ERROR:
        return _infrastructure_decision(
            "external_evaluator_error",
            "The common external evaluator failed rather than returning PASS or FAIL.",
        )

    if observation.candidate_contract_status is CandidateContractStatus.MISSING:
        if observation.process_returncode != 0:
            return _arm_failure_decision(
                "arm_process_nonzero_exit",
                "The arm ended nonzero without a valid candidate contract.",
            )
        return _arm_failure_decision(
            "candidate_contract_missing",
            "The arm completed without producing its required candidate contract.",
        )
    if observation.candidate_contract_status is CandidateContractStatus.INVALID:
        if observation.process_returncode != 0:
            return _arm_failure_decision(
                "arm_process_nonzero_exit",
                "The arm ended nonzero without a valid candidate contract.",
            )
        return _arm_failure_decision(
            "candidate_contract_invalid",
            "The arm produced a candidate contract that failed schema validation.",
        )
    if observation.candidate_contract_status is CandidateContractStatus.NOT_APPLICABLE:
        return _infrastructure_decision(
            "candidate_contract_state_invalid",
            "A completed arm must declare a candidate contract state.",
        )

    if observation.external_evaluation_status is ExternalEvaluationStatus.NOT_RUN:
        return _infrastructure_decision(
            "external_evaluation_not_run",
            "A valid candidate contract was not evaluated by the common evaluator.",
        )
    if observation.external_evaluation_status is ExternalEvaluationStatus.FAIL:
        return _arm_failure_decision(
            "external_evaluation_failed",
            "The common external evaluator returned a deterministic hard failure.",
        )
    return AttemptDecision(
        disposition=AttemptDisposition.VALID_SUCCESS,
        counts_as_completed_run=True,
        overall_success=True,
        infrastructure_failure=False,
        failure_category="",
        rationale="The arm completed with a valid contract and external PASS.",
    )


def observation_from_process_result(
    *,
    attempt_id: str,
    process_result: ControlledProcessResult,
    candidate_contract_status: CandidateContractStatus,
    external_evaluation_status: ExternalEvaluationStatus,
    duration_s: float,
    deadline_s: float,
    detail: str = "",
) -> AttemptObservation:
    """Convert the existing process-control result into the new outer envelope."""

    timed_out = process_result.timed_out
    combined_detail = "; ".join(
        item for item in (detail.strip(), process_result.cleanup_detail.strip()) if item
    )
    return AttemptObservation(
        attempt_id=attempt_id,
        termination_reason=(
            TerminationReason.DEADLINE_EXCEEDED
            if timed_out
            else TerminationReason.COMPLETED
        ),
        process_started=True,
        process_returncode=process_result.returncode,
        candidate_contract_status=candidate_contract_status,
        external_evaluation_status=external_evaluation_status,
        cleanup_attempted=process_result.cleanup_attempted if timed_out else False,
        cleanup_succeeded=(
            process_result.cleanup_succeeded if timed_out else None
        ),
        duration_s=duration_s,
        deadline_s=deadline_s if timed_out else None,
        detail=combined_detail[:2000],
    )


def launch_failure_observation(*, attempt_id: str, detail: str) -> AttemptObservation:
    return AttemptObservation(
        attempt_id=attempt_id,
        termination_reason=TerminationReason.PROCESS_LAUNCH_FAILED,
        process_started=False,
        process_returncode=None,
        candidate_contract_status=CandidateContractStatus.NOT_APPLICABLE,
        external_evaluation_status=ExternalEvaluationStatus.NOT_RUN,
        cleanup_attempted=False,
        cleanup_succeeded=None,
        duration_s=0.0,
        detail=detail[:2000],
    )


def recovery_policy_manifest() -> dict[str, Any]:
    """Return the exact policy object frozen by the offline recovery protocol."""

    return {
        "schema_version": "v8-formal-recovery-outcome-policy-1",
        "applies_to_all_arms": list(FORMAL_ARM_IDS),
        "formal_deadline_s": FORMAL_DEADLINE_S,
        "deadline_exceeded_cleanup_succeeded": {
            "disposition": AttemptDisposition.VALID_ARM_FAILURE.value,
            "counts_as_completed_run": True,
            "overall_success": False,
            "infrastructure_failure": False,
        },
        "deadline_exceeded_cleanup_failed": {
            "disposition": AttemptDisposition.INVALID_INFRASTRUCTURE.value,
            "counts_as_completed_run": False,
            "overall_success": False,
            "infrastructure_failure": True,
        },
        "process_launch_or_harness_error": {
            "disposition": AttemptDisposition.INVALID_INFRASTRUCTURE.value,
            "counts_as_completed_run": False,
            "overall_success": False,
            "infrastructure_failure": True,
        },
        "candidate_missing_invalid_or_nonzero_without_valid_contract": {
            "disposition": AttemptDisposition.VALID_ARM_FAILURE.value,
            "counts_as_completed_run": True,
            "overall_success": False,
            "infrastructure_failure": False,
        },
        "external_evaluator_error_or_not_run_on_valid_contract": {
            "disposition": AttemptDisposition.INVALID_INFRASTRUCTURE.value,
            "counts_as_completed_run": False,
            "overall_success": False,
            "infrastructure_failure": True,
        },
        "external_evaluator_fail": {
            "disposition": AttemptDisposition.VALID_ARM_FAILURE.value,
            "counts_as_completed_run": True,
            "overall_success": False,
            "infrastructure_failure": False,
        },
        "external_evaluator_pass": {
            "disposition": AttemptDisposition.VALID_SUCCESS.value,
            "counts_as_completed_run": True,
            "overall_success": True,
            "infrastructure_failure": False,
        },
        "valid_contract_uses_external_evaluator_despite_internal_exit_code": True,
        "old_attempt_reclassification_allowed": False,
        "future_independently_sealed_holdout_only": True,
    }


def _arm_failure_decision(category: str, rationale: str) -> AttemptDecision:
    return AttemptDecision(
        disposition=AttemptDisposition.VALID_ARM_FAILURE,
        counts_as_completed_run=True,
        overall_success=False,
        infrastructure_failure=False,
        failure_category=category,
        rationale=rationale,
    )


def _infrastructure_decision(category: str, rationale: str) -> AttemptDecision:
    return AttemptDecision(
        disposition=AttemptDisposition.INVALID_INFRASTRUCTURE,
        counts_as_completed_run=False,
        overall_success=False,
        infrastructure_failure=True,
        failure_category=category,
        rationale=rationale,
    )


__all__ = [
    "AttemptDecision",
    "AttemptDisposition",
    "AttemptObservation",
    "CandidateContractStatus",
    "ExternalEvaluationStatus",
    "FORMAL_ARM_IDS",
    "FORMAL_DEADLINE_S",
    "TerminationReason",
    "classify_attempt",
    "launch_failure_observation",
    "observation_from_process_result",
    "recovery_policy_manifest",
]
