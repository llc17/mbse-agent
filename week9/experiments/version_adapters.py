"""Subprocess-isolated adapters for frozen V4/V6 and the new V8 runner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import uuid
from typing import Any, Mapping

from week9.experiments.cases import CaseDefinition
from week9.experiments.external_evaluator import EvaluationContext, evaluate
from week9.experiments.cross_version_validation import externally_validate
from week9.experiments.online_policy import assert_online_allowed
from week9.experiments.subprocess_control import run_controlled
from week9.src.quality_gate import apply_to_run_result
from week9.src.schemas import (
    ArtifactRecord,
    CheckStatus,
    FailureKind,
    ModelConfiguration,
    NormalizedParameter,
    QualityCheckResult,
    RunCounters,
    RunResult,
    TokenUsage,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
V7_RUNNER = PROJECT_ROOT / "week8" / "src" / "runner.py"
V8_RUNNER = PROJECT_ROOT / "week9" / "src" / "runner.py"
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def new_run_id(version: str, case_id: str) -> str:
    _validate_identifier(version, "version")
    _validate_identifier(case_id, "case_id")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}_{version}_{case_id}_{uuid.uuid4().hex[:8]}"


def _validate_identifier(value: str, field_name: str) -> str:
    if not _SAFE_IDENTIFIER.fullmatch(value) or ".." in value:
        raise ValueError(f"unsafe {field_name}: {value!r}")
    return value


def _output_path(output_root: Path, run_id: str, filename: str) -> Path:
    _validate_identifier(run_id, "run_id")
    root = output_root.resolve()
    candidate = (root / run_id / filename).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("resolved output path escapes output_root")
    return candidate


@dataclass(frozen=True, slots=True)
class AdapterCommand:
    version: str
    run_id: str
    command: tuple[str, ...]
    cwd: str
    output_path: str


class LegacyAdapter:
    def __init__(self, version: str):
        if version not in {"v4", "v6"}:
            raise ValueError("LegacyAdapter supports only v4 and v6")
        self.version = version

    def build_command(
        self, case: CaseDefinition, output_root: Path, *, run_id: str | None = None
    ) -> AdapterCommand:
        run_id = run_id or new_run_id(self.version, case.case_id)
        _validate_identifier(case.case_id, "case_id")
        output_path = _output_path(output_root, run_id, "raw_result.json")
        command = (
            sys.executable,
            str(V7_RUNNER),
            "--version",
            self.version,
            "--case",
            case.case_id,
            "--raw-input",
            case.raw_input,
            "--out",
            str(output_path),
        )
        return AdapterCommand(
            version=self.version,
            run_id=run_id,
            command=command,
            cwd=str((PROJECT_ROOT / "week8").resolve()),
            output_path=str(output_path),
        )

    def run(
        self,
        case: CaseDefinition,
        output_root: Path,
        *,
        timeout_s: float = 600.0,
        allow_online: bool = False,
        run_kind: str = "smoke",
        environment: Mapping[str, str] | None = None,
    ) -> RunResult:
        assert_online_allowed(allow_online=allow_online, run_kind=run_kind)
        spec = self.build_command(case, output_root)
        output_path = Path(spec.output_path)
        output_path.parent.mkdir(parents=True, exist_ok=False)
        try:
            proc = run_controlled(
                spec.command,
                cwd=spec.cwd,
                timeout_s=timeout_s,
                env=environment,
            )
        except OSError as exc:
            raw = {
                "error": f"adapter launch failed: {type(exc).__name__}: {exc}",
                "success": False,
            }
            return normalize_legacy_result(raw, case, self.version, spec.run_id)

        if proc.timed_out:
            raw = {
                "error": f"adapter timeout after {timeout_s}s",
                "success": False,
                "process_tree_cleanup_attempted": proc.cleanup_attempted,
                "process_tree_cleanup_succeeded": proc.cleanup_succeeded,
                "process_tree_cleanup_detail": proc.cleanup_detail,
                "stdout_tail": proc.stdout[-500:],
                "stderr_tail": proc.stderr[-500:],
            }
            return normalize_legacy_result(raw, case, self.version, spec.run_id)

        if not output_path.is_file():
            raw = {
                "success": False,
                "error": f"runner exited {proc.returncode} without a result file",
                "stdout_tail": proc.stdout[-500:],
                "stderr_tail": proc.stderr[-500:],
            }
        else:
            try:
                raw = json.loads(output_path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("result JSON root must be an object")
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                raw = {"success": False, "error": f"invalid result JSON: {exc}"}

        if proc.returncode != 0:
            raw["adapter_returncode"] = proc.returncode
            raw["error"] = str(raw.get("error") or f"runner exited with code {proc.returncode}")
        candidate = normalize_legacy_result(raw, case, self.version, spec.run_id)
        try:
            return externally_validate(
                candidate,
                case,
                workspace_root=output_path.parent / "external-evaluation",
            )
        except Exception as exc:
            return _adapter_failure_result(
                case,
                self.version,
                spec.run_id,
                "external evaluation failed: "
                f"{type(exc).__name__}: {str(exc)[:1000]}",
            )


class V8Adapter:
    version = "v8"

    def build_command(
        self,
        case: CaseDefinition,
        output_root: Path,
        *,
        run_id: str | None = None,
        online_generation: bool = False,
        run_kind: str = "smoke",
    ) -> AdapterCommand:
        if run_kind not in {"smoke", "pilot", "formal"}:
            raise ValueError(f"unsupported run_kind: {run_kind}")
        run_id = run_id or new_run_id(self.version, case.case_id)
        _validate_identifier(case.case_id, "case_id")
        output_path = _output_path(output_root, run_id, "run_result.json")
        command_parts = [
            sys.executable,
            str(V8_RUNNER),
            "--case",
            case.case_id,
            "--run-id",
            run_id,
            "--raw-input",
            case.raw_input,
            "--case-sha256",
            case.sha256,
            "--out",
            str(output_path),
        ]
        if case.dataset == "holdout":
            command_parts.append("--formal-holdout-access")
        if online_generation:
            command_parts.extend(
                (
                    "--generation-provider",
                    "deepseek-online",
                    "--online-run-kind",
                    run_kind,
                )
            )
        command = tuple(command_parts)
        return AdapterCommand(
            version=self.version,
            run_id=run_id,
            command=command,
            cwd=str((PROJECT_ROOT / "week9").resolve()),
            output_path=str(output_path),
        )

    def run(
        self,
        case: CaseDefinition,
        output_root: Path,
        *,
        timeout_s: float = 600.0,
        allow_online: bool = False,
        run_kind: str = "smoke",
        environment: Mapping[str, str] | None = None,
    ) -> RunResult:
        if run_kind not in {"smoke", "pilot", "formal"}:
            raise ValueError(f"unsupported run_kind: {run_kind}")
        if allow_online:
            assert_online_allowed(allow_online=True, run_kind=run_kind)
        spec = self.build_command(
            case,
            output_root,
            online_generation=allow_online,
            run_kind=run_kind,
        )
        output_path = Path(spec.output_path)
        output_path.parent.mkdir(parents=True, exist_ok=False)
        try:
            proc = run_controlled(
                spec.command,
                cwd=spec.cwd,
                timeout_s=timeout_s,
                env=environment,
            )
        except OSError as exc:
            return _adapter_failure_result(
                case, self.version, spec.run_id, f"V8 runner failed: {type(exc).__name__}: {exc}"
            )
        if proc.timed_out:
            return _adapter_failure_result(
                case,
                self.version,
                spec.run_id,
                "V8 runner timed out; "
                f"process_tree_cleanup_succeeded={proc.cleanup_succeeded}; "
                f"detail={proc.cleanup_detail}",
            )
        if not output_path.is_file():
            return _adapter_failure_result(
                case,
                self.version,
                spec.run_id,
                f"V8 runner exited {proc.returncode} without result: {proc.stderr[-500:]}",
            )
        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("V8 result JSON root must be an object")
            result = apply_to_run_result(RunResult.from_dict(payload))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return _adapter_failure_result(
                case,
                self.version,
                spec.run_id,
                f"invalid V8 result contract: {type(exc).__name__}: {exc}",
            )
        if proc.returncode != 0 and result.overall_success:
            return _adapter_failure_result(
                case,
                self.version,
                spec.run_id,
                f"V8 runner returned success JSON but exited with code {proc.returncode}",
            )
        try:
            return externally_validate(
                result,
                case,
                workspace_root=output_path.parent / "external-evaluation",
            )
        except Exception as exc:
            return _adapter_failure_result(
                case,
                self.version,
                spec.run_id,
                "external evaluation failed: "
                f"{type(exc).__name__}: {str(exc)[:1000]}",
            )


def normalize_legacy_result(
    raw: dict[str, Any], case: CaseDefinition, version: str, run_id: str
) -> RunResult:
    """Preserve legacy metrics while refusing to trust legacy success flags."""

    if not isinstance(raw, dict):
        raw = {"success": False, "error": "legacy result root is not a JSON object"}
    else:
        raw = dict(raw)
    normalization_errors: list[str] = []
    raw_parameters = raw.get("parameters") or {}
    if not isinstance(raw_parameters, dict):
        normalization_errors.append("legacy parameters field is not an object")
        raw_parameters = {}
    token_stats = raw.get("token_stats") or {}
    if not isinstance(token_stats, dict):
        normalization_errors.append("legacy token_stats field is not an object")
        token_stats = {}
    if normalization_errors:
        existing_error = str(raw.get("error") or "")
        raw["error"] = "; ".join(filter(None, [existing_error, *normalization_errors]))

    artifacts = {
        "sysml": ArtifactRecord(
            artifact_type="sysml",
            content=str(raw.get("sysml_code") or ""),
            path=str(raw.get("sysml_file") or ""),
            media_type="text/x-sysml",
        ),
        "modelica": ArtifactRecord(
            artifact_type="modelica",
            content=str(raw.get("modelica_code") or ""),
            path=str(raw.get("modelica_file") or ""),
            media_type="text/x-modelica",
        ),
        "simulation_csv": ArtifactRecord(
            artifact_type="simulation_csv",
            path=str(raw.get("csv_path") or ""),
            media_type="text/csv",
        ),
    }

    parameters = [
        NormalizedParameter(
            canonical_name=str(name),
            value=value,
            unit="unreported",
            source_text=f"legacy structured parameter: {name}",
            status="known" if value is not None else "unknown",
        )
        for name, value in raw_parameters.items()
    ]

    legacy_checks = _legacy_observations(raw)
    error = str(raw.get("error") or "")
    execution_check = QualityCheckResult(
        check_type="adapter.execution",
        status=CheckStatus.ERROR if error else CheckStatus.PASS,
        required=True,
        issues=[error] if error else [],
        evidence={"reason": "legacy_runner_error" if error else "", "legacy": True},
        tool_name="week9.version_adapter",
        tool_version="1",
        failure_kind=FailureKind.INFRASTRUCTURE if error else FailureKind.NONE,
        failure_category="legacy_runner_error" if error else "",
    )
    legacy_checks.append(execution_check)

    provider = str(token_stats.get("provider") or "").strip()
    if not provider and version == "v4":
        provider = "deepseek"
    model = str(token_stats.get("model") or "").strip()
    return evaluate(
        context=EvaluationContext(
            version=version,
            case_id=case.case_id,
            run_id=run_id,
            requirement_text=case.raw_input,
            artifacts=artifacts,
            metadata={"legacy_internal_success": raw.get("success")},
        ),
        required_check_types=case.required_checks,
        parameters=parameters,
        precomputed_external_checks=legacy_checks,
        counters=RunCounters(
            generation_count=1 if artifacts["modelica"].content else 0,
            validation_count=sum(1 for item in legacy_checks if item.status is not CheckStatus.UNVERIFIED),
            content_repair_count=int(raw.get("repair_count") or 0),
        ),
        token_usage=TokenUsage(
            prompt_tokens=_safe_nonnegative_int(token_stats.get("prompt_tokens")),
            completion_tokens=_safe_nonnegative_int(token_stats.get("completion_tokens")),
            total_tokens=_safe_nonnegative_int(token_stats.get("total_tokens")),
        ),
        model_configuration=ModelConfiguration(
            provider=provider,
            model=model,
            parameters={
                "temperature": 0.3,
                "max_tokens": 8192,
                "configuration_source": "frozen_legacy_runner",
            },
        ),
        duration_s=_safe_nonnegative_float(raw.get("duration_s")),
        infrastructure_errors=[error] if error else [],
        raw_metrics=dict(raw),
    )


def _safe_nonnegative_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, parsed)


def _safe_nonnegative_float(value: Any) -> float:
    try:
        parsed = float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if parsed < 0 or parsed != parsed or parsed in {float("inf"), float("-inf")}:
        return 0.0
    return parsed


def _adapter_failure_result(
    case: CaseDefinition, version: str, run_id: str, message: str
) -> RunResult:
    execution = QualityCheckResult(
        check_type="adapter.execution",
        status=CheckStatus.ERROR,
        required=True,
        issues=[message],
        evidence={"reason": "adapter_execution_error"},
        tool_name="week9.version_adapter",
        tool_version="1",
        failure_kind=FailureKind.INFRASTRUCTURE,
        failure_category="adapter_execution_error",
    )
    return evaluate(
        context=EvaluationContext(
            version=version,
            case_id=case.case_id,
            run_id=run_id,
            requirement_text=case.raw_input,
            artifacts={},
        ),
        required_check_types=case.required_checks,
        precomputed_external_checks=[execution],
        infrastructure_errors=[message],
    )


def _legacy_observations(raw: dict[str, Any]) -> list[QualityCheckResult]:
    checks: list[QualityCheckResult] = []
    legacy_success = raw.get("success")
    checks.append(
        QualityCheckResult(
            check_type="legacy.mo_success",
            status=(
                CheckStatus.UNVERIFIED if legacy_success is None
                else CheckStatus.PASS if bool(legacy_success)
                else CheckStatus.FAIL
            ),
            required=False,
            issues=[] if legacy_success else ["旧版本内部 mo.success 未通过或缺失"],
            evidence={"legacy_value": legacy_success, "reason": "legacy_boolean_only"},
            tool_name="legacy.pipeline",
            failure_kind=(
                FailureKind.NONE
                if legacy_success is not None and bool(legacy_success)
                else FailureKind.UNKNOWN
            ),
            failure_category=(
                ""
                if legacy_success is not None and bool(legacy_success)
                else "legacy_internal_status"
            ),
        )
    )

    physics = raw.get("physics_passed")
    checks.append(
        QualityCheckResult(
            check_type="legacy.physics_passed",
            status=(
                CheckStatus.UNVERIFIED if physics is None
                else CheckStatus.PASS if bool(physics)
                else CheckStatus.FAIL
            ),
            required=False,
            issues=[] if physics else ["旧版本物理检查未通过或缺少证据"],
            evidence={
                "legacy_value": physics,
                "expected": raw.get("physics_expected"),
                "actual": raw.get("physics_actual"),
                "deviation_pct": raw.get("physics_deviation_pct"),
                "reason": "legacy_boolean_only",
            },
            tool_name="legacy.physics_validate",
            failure_kind=(
                FailureKind.NONE if physics is not None and bool(physics)
                else FailureKind.UNKNOWN
            ),
            failure_category=(
                "" if physics is not None and bool(physics)
                else "legacy_internal_status"
            ),
        )
    )
    return checks


def get_adapter(version: str):
    if version in {"v4", "v6"}:
        return LegacyAdapter(version)
    if version == "v8":
        return V8Adapter()
    raise ValueError(f"unsupported version: {version}")
