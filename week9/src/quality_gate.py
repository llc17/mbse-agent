"""Deterministic aggregation for V8 final quality checks."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable

from week9.src.schemas import CheckStatus, QualityCheckResult, RunResult, index_checks


@dataclass(frozen=True, slots=True)
class QualityGateDecision:
    overall_success: bool
    blocking_checks: tuple[str, ...]
    failure_reasons: tuple[str, ...]
    primary_failure_category: str
    primary_failure_evidence: dict


_STATUS_PRIORITY = {
    CheckStatus.FAIL: 0,
    CheckStatus.ERROR: 1,
    CheckStatus.UNVERIFIED: 2,
    CheckStatus.NOT_APPLICABLE: 3,
    CheckStatus.PASS: 4,
}


def decide(checks: Iterable[QualityCheckResult]) -> QualityGateDecision:
    """Apply the V8 release rule to a set of final checks.

    A release requires at least one required check and every required check must
    be ``PASS``. Optional checks (for example, Kimi Judge) remain observable but
    cannot override or dilute a hard result.
    """

    indexed = index_checks(checks)
    required = [check for check in indexed.values() if check.required]

    if not required:
        return QualityGateDecision(
            overall_success=False,
            blocking_checks=("quality_gate",),
            failure_reasons=("没有声明任何发布必需检查，结果不可验证",),
            primary_failure_category="quality_gate_configuration",
            primary_failure_evidence={"required_check_count": 0},
        )

    blockers = [check for check in required if check.status is not CheckStatus.PASS]
    if not blockers:
        return QualityGateDecision(
            overall_success=True,
            blocking_checks=(),
            failure_reasons=(),
            primary_failure_category="",
            primary_failure_evidence={},
        )

    blockers.sort(key=lambda item: (_STATUS_PRIORITY[item.status], item.check_type))
    reasons = tuple(_failure_reason(check) for check in blockers)
    primary = blockers[0]
    return QualityGateDecision(
        overall_success=False,
        blocking_checks=tuple(check.check_type for check in blockers),
        failure_reasons=reasons,
        primary_failure_category=primary.failure_category or primary.check_type,
        primary_failure_evidence={
            "check_type": primary.check_type,
            "status": primary.status.value,
            "issues": list(primary.issues),
            "evidence": dict(primary.evidence),
            "failure_kind": primary.failure_kind.value,
        },
    )


def apply_to_run_result(result: RunResult) -> RunResult:
    """Return a copy whose success fields are recomputed from final checks."""

    decision = decide(result.checks)
    return replace(
        result,
        overall_success=decision.overall_success,
        primary_failure_category=decision.primary_failure_category,
        primary_failure_evidence=decision.primary_failure_evidence,
    )


def _failure_reason(check: QualityCheckResult) -> str:
    detail = check.issues[0] if check.issues else str(check.evidence.get("reason", ""))
    suffix = f"：{detail}" if detail else ""
    return f"{check.check_type}={check.status.value}{suffix}"

