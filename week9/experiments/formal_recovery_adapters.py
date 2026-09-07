"""Additive adapter integration for the offline V8 recovery protocol.

All four frozen arm commands can be constructed, but this recovery milestone
executes only ``v8_default_local`` on development cases.  Paid arms and every
holdout-shaped case fail before process launch.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Mapping

from week9.experiments.cases import CaseDefinition
from week9.experiments.cross_version_validation import externally_validate
from week9.experiments.formal_recovery import FORMAL_DEADLINE_S
from week9.experiments.formal_recovery_harness import (
    RecoveryAttemptSpec,
    RecoveryHarnessResult,
    execute_recovery_attempt,
)
from week9.experiments.version_adapters import (
    AdapterCommand,
    LegacyAdapter,
    V8Adapter,
    normalize_legacy_result,
)
from week9.src.quality_gate import apply_to_run_result
from week9.src.schemas import RunResult


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ARM_SPEC = {
    "v4_default_deepseek": ("v4", True),
    "v6_default_deepseek": ("v6", True),
    "v8_controlled_deepseek": ("v8", True),
    "v8_default_local": ("v8", False),
}
_SENSITIVE_ENV_PREFIXES = (
    "ANTHROPIC_",
    "DEEPSEEK_",
    "KIMI_",
    "OPENAI_",
    "ZHIPU_",
)


@dataclass(frozen=True, slots=True)
class RecoveryAdapterRun:
    arm_id: str
    case_id: str
    harness_result: RecoveryHarnessResult
    externally_validated_result: RunResult | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "arm_id": self.arm_id,
            "case_id": self.case_id,
            "harness": self.harness_result.to_dict(),
            "externally_validated_result": (
                self.externally_validated_result.to_dict()
                if self.externally_validated_result is not None
                else None
            ),
        }


def build_recovery_adapter_command(
    arm_id: str,
    case: CaseDefinition,
    output_root: Path,
) -> AdapterCommand:
    """Build a frozen arm command without launching it or reading credentials."""

    if arm_id not in ARM_SPEC:
        raise ValueError(f"unsupported recovery arm: {arm_id}")
    _require_development_case(case)
    version, online = ARM_SPEC[arm_id]
    if version in {"v4", "v6"}:
        return LegacyAdapter(version).build_command(case, output_root)
    return V8Adapter().build_command(
        case,
        output_root,
        online_generation=online,
        run_kind="formal",
    )


def run_development_recovery_arm(
    arm_id: str,
    case: CaseDefinition,
    output_root: Path,
    *,
    deadline_s: float = FORMAL_DEADLINE_S,
    environment: Mapping[str, str] | None = None,
) -> RecoveryAdapterRun:
    """Run the only currently authorized recovery arm: local V8 development."""

    _require_development_case(case)
    if arm_id != "v8_default_local":
        raise PermissionError(
            "offline recovery protocol blocks every paid arm until a new sealed "
            "holdout, final protocol, and new user confirmation exist"
        )
    root = output_root.resolve()
    if not root.is_relative_to(PROJECT_ROOT):
        raise ValueError("recovery adapter output_root must remain inside the project")
    command = build_recovery_adapter_command(arm_id, case, root)
    candidate_path = Path(command.output_path).resolve()
    attempt_spec = RecoveryAttemptSpec.build(
        attempt_id=command.run_id,
        command=command.command,
        cwd=command.cwd,
        workspace_root=PROJECT_ROOT,
        candidate_contract_path=candidate_path,
    )
    validated: dict[str, RunResult] = {}

    def common_external_evaluator(payload: Mapping[str, Any]) -> bool:
        result = apply_to_run_result(RunResult.from_dict(payload))
        if (
            result.version.lower() != "v8"
            or result.case_id != case.case_id
            or result.run_id != command.run_id
        ):
            raise ValueError("V8 candidate identity differs from the adapter command")
        external = externally_validate(
            result,
            case,
            workspace_root=candidate_path.parent / "external-evaluation-v2",
        )
        validated["result"] = external
        return external.overall_success

    harness = execute_recovery_attempt(
        attempt_spec,
        deadline_s=deadline_s,
        external_evaluator=common_external_evaluator,
        environment=_offline_environment(environment or os.environ),
    )
    return RecoveryAdapterRun(
        arm_id=arm_id,
        case_id=case.case_id,
        harness_result=harness,
        externally_validated_result=validated.get("result"),
    )


def normalize_future_arm_payload(
    *,
    arm_id: str,
    case: CaseDefinition,
    run_id: str,
    payload: Mapping[str, Any],
) -> RunResult:
    """Normalize any future arm payload without launching a paid process."""

    if arm_id not in ARM_SPEC:
        raise ValueError(f"unsupported recovery arm: {arm_id}")
    _require_development_case(case)
    version, _online = ARM_SPEC[arm_id]
    if version in {"v4", "v6"}:
        return normalize_legacy_result(dict(payload), case, version, run_id)
    result = apply_to_run_result(RunResult.from_dict(payload))
    if result.version.lower() != "v8" or result.case_id != case.case_id:
        raise ValueError("V8 candidate identity differs from recovery case")
    return result


def _require_development_case(case: CaseDefinition) -> None:
    if case.dataset != "development_regression":
        raise PermissionError(
            "recovery adapter milestone accepts development cases only; all old and "
            "future holdout execution is blocked"
        )


def _offline_environment(source: Mapping[str, str]) -> dict[str, str]:
    environment = {
        key: value
        for key, value in source.items()
        if not key.upper().startswith(_SENSITIVE_ENV_PREFIXES)
        and key.upper() != "LLM_PROVIDER"
    }
    environment["V8_RECOVERY_OFFLINE_ONLY"] = "1"
    return environment


__all__ = [
    "ARM_SPEC",
    "RecoveryAdapterRun",
    "build_recovery_adapter_command",
    "normalize_future_arm_payload",
    "run_development_recovery_arm",
]
