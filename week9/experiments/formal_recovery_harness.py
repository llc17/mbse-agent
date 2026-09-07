"""Generic offline-safe outer harness for future V8 formal attempts.

The harness owns process launch, the preregistered deadline, contract loading,
and evaluator exception capture.  It deliberately knows nothing about V4,
V6, V8, model providers, or dataset contents.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping, Sequence

from week9.experiments.formal_recovery import (
    AttemptDecision,
    AttemptObservation,
    CandidateContractStatus,
    ExternalEvaluationStatus,
    TerminationReason,
    classify_attempt,
    launch_failure_observation,
    observation_from_process_result,
)
from week9.experiments.subprocess_control import run_controlled


_SAFE_ATTEMPT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
ContractLoader = Callable[[Path], Mapping[str, Any]]
ExternalEvaluator = Callable[[Mapping[str, Any]], bool]


@dataclass(frozen=True, slots=True)
class RecoveryAttemptSpec:
    attempt_id: str
    command: tuple[str, ...]
    cwd: Path
    workspace_root: Path
    candidate_contract_path: Path

    def __post_init__(self) -> None:
        if not _SAFE_ATTEMPT_ID.fullmatch(self.attempt_id) or ".." in self.attempt_id:
            raise ValueError(f"unsafe attempt_id: {self.attempt_id!r}")
        if not self.command or any(
            not isinstance(item, str) or not item for item in self.command
        ):
            raise ValueError("command must contain non-empty string arguments")
        for value, name in (
            (self.cwd, "cwd"),
            (self.workspace_root, "workspace_root"),
            (self.candidate_contract_path, "candidate_contract_path"),
        ):
            if not isinstance(value, Path) or value != value.resolve():
                raise ValueError(f"{name} must be an absolute resolved Path")
        if not self.cwd.is_relative_to(self.workspace_root):
            raise ValueError("cwd escapes workspace_root")
        if not self.candidate_contract_path.is_relative_to(self.workspace_root):
            raise ValueError("candidate contract path escapes workspace_root")

    @classmethod
    def build(
        cls,
        *,
        attempt_id: str,
        command: Sequence[str],
        cwd: str | Path,
        workspace_root: str | Path,
        candidate_contract_path: str | Path,
    ) -> "RecoveryAttemptSpec":
        root = Path(workspace_root).resolve()
        resolved_cwd = Path(cwd).resolve()
        resolved_contract = Path(candidate_contract_path).resolve()
        command_tuple = tuple(command)
        return cls(
            attempt_id=attempt_id,
            command=command_tuple,
            cwd=resolved_cwd,
            workspace_root=root,
            candidate_contract_path=resolved_contract,
        )


@dataclass(frozen=True, slots=True)
class RecoveryHarnessResult:
    observation: AttemptObservation
    decision: AttemptDecision
    candidate_contract_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation": self.observation.to_dict(),
            "decision": self.decision.to_dict(),
            "candidate_contract_sha256": self.candidate_contract_sha256,
        }


def execute_recovery_attempt(
    spec: RecoveryAttemptSpec,
    *,
    deadline_s: float,
    external_evaluator: ExternalEvaluator,
    contract_loader: ContractLoader | None = None,
    environment: Mapping[str, str] | None = None,
) -> RecoveryHarnessResult:
    """Execute one attempt and always return an outer audit decision."""

    if (
        type(deadline_s) not in {int, float}
        or not math.isfinite(float(deadline_s))
        or deadline_s <= 0
    ):
        raise ValueError("deadline_s must be finite and positive")
    if not spec.cwd.is_dir() or not spec.workspace_root.is_dir():
        raise ValueError("workspace_root and cwd must exist")
    if spec.candidate_contract_path.exists():
        raise FileExistsError(
            f"candidate contract already exists: {spec.candidate_contract_path}"
        )
    try:
        spec.candidate_contract_path.parent.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        observation = _harness_error_observation(
            attempt_id=spec.attempt_id,
            process_started=False,
            process_returncode=None,
            duration_s=0.0,
            detail=f"attempt directory error: {type(exc).__name__}: {str(exc)[:1000]}",
        )
        return RecoveryHarnessResult(observation, classify_attempt(observation))
    started = time.perf_counter()
    try:
        process = run_controlled(
            spec.command,
            cwd=spec.cwd,
            timeout_s=float(deadline_s),
            env=environment,
        )
    except OSError as exc:
        observation = launch_failure_observation(
            attempt_id=spec.attempt_id,
            detail=f"{type(exc).__name__}: {str(exc)[:1000]}",
        )
        return RecoveryHarnessResult(observation, classify_attempt(observation))
    except Exception as exc:
        observation = _harness_error_observation(
            attempt_id=spec.attempt_id,
            process_started=False,
            process_returncode=None,
            duration_s=time.perf_counter() - started,
            detail=f"{type(exc).__name__}: {str(exc)[:1000]}",
        )
        return RecoveryHarnessResult(observation, classify_attempt(observation))

    duration_s = time.perf_counter() - started
    if process.timed_out:
        observation = observation_from_process_result(
            attempt_id=spec.attempt_id,
            process_result=process,
            candidate_contract_status=CandidateContractStatus.NOT_APPLICABLE,
            external_evaluation_status=ExternalEvaluationStatus.NOT_RUN,
            duration_s=duration_s,
            deadline_s=float(deadline_s),
        )
        return RecoveryHarnessResult(observation, classify_attempt(observation))

    contract_status = CandidateContractStatus.MISSING
    evaluation_status = ExternalEvaluationStatus.NOT_RUN
    contract_digest = ""
    detail_parts: list[str] = []
    payload: Mapping[str, Any] | None = None
    if spec.candidate_contract_path.is_file():
        try:
            raw = spec.candidate_contract_path.read_bytes()
            contract_digest = hashlib.sha256(raw).hexdigest()
            loader = contract_loader or _load_json_contract
            payload = loader(spec.candidate_contract_path)
            if not isinstance(payload, Mapping):
                raise ValueError("contract loader must return a mapping")
            contract_status = CandidateContractStatus.VALID
        except OSError as exc:
            observation = _harness_error_observation(
                attempt_id=spec.attempt_id,
                process_started=True,
                process_returncode=process.returncode,
                duration_s=duration_s,
                detail=(
                    "candidate contract read error: "
                    f"{type(exc).__name__}: {str(exc)[:1000]}"
                ),
            )
            return RecoveryHarnessResult(
                observation,
                classify_attempt(observation),
                candidate_contract_sha256=contract_digest,
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            contract_status = CandidateContractStatus.INVALID
            detail_parts.append(
                f"candidate contract invalid: {type(exc).__name__}: {str(exc)[:800]}"
            )
    if payload is not None:
        try:
            passed = external_evaluator(payload)
            if type(passed) is not bool:
                raise TypeError("external evaluator must return a bool")
            evaluation_status = (
                ExternalEvaluationStatus.PASS
                if passed
                else ExternalEvaluationStatus.FAIL
            )
        except Exception as exc:
            evaluation_status = ExternalEvaluationStatus.ERROR
            detail_parts.append(
                f"external evaluator error: {type(exc).__name__}: {str(exc)[:800]}"
            )

    stderr_tail = process.stderr[-500:].strip()
    if stderr_tail:
        detail_parts.append(f"stderr_tail={stderr_tail}")
    observation = observation_from_process_result(
        attempt_id=spec.attempt_id,
        process_result=process,
        candidate_contract_status=contract_status,
        external_evaluation_status=evaluation_status,
        duration_s=duration_s,
        deadline_s=float(deadline_s),
        detail="; ".join(detail_parts),
    )
    return RecoveryHarnessResult(
        observation,
        classify_attempt(observation),
        candidate_contract_sha256=contract_digest,
    )


def _load_json_contract(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("candidate contract root must be an object")
    return value


def _harness_error_observation(
    *,
    attempt_id: str,
    process_started: bool,
    process_returncode: int | None,
    duration_s: float,
    detail: str,
) -> AttemptObservation:
    return AttemptObservation(
        attempt_id=attempt_id,
        termination_reason=TerminationReason.HARNESS_ERROR,
        process_started=process_started,
        process_returncode=process_returncode,
        candidate_contract_status=CandidateContractStatus.NOT_APPLICABLE,
        external_evaluation_status=ExternalEvaluationStatus.ERROR,
        cleanup_attempted=False,
        cleanup_succeeded=None,
        duration_s=duration_s,
        detail=detail[:2000],
    )


__all__ = [
    "ContractLoader",
    "ExternalEvaluator",
    "RecoveryAttemptSpec",
    "RecoveryHarnessResult",
    "execute_recovery_attempt",
]
