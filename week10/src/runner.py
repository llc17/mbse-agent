"""Safe V9 integration layer over the frozen V8 certified pipeline."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from enum import Enum
import json
import math
from pathlib import Path
import re
import sys
import tempfile
import time
from types import MappingProxyType
from typing import Any, Mapping
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEEK10_ROOT = PROJECT_ROOT / "week10"
RESULTS_ROOT = WEEK10_ROOT / "results"
SUITE_PATH = WEEK10_ROOT / "configs" / "development_diagnostic_suite.json"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from week9.src.generation import (  # noqa: E402
    DeterministicNaturalLanguageProvider,
    NaturalLanguageGenerationResult,
)
from week9.src.hard_gate import REQUIRED_HARD_CHECK_TYPES  # noqa: E402
from week9.src.pipeline import CertifiedTemplateRepairPipeline  # noqa: E402
from week9.src.quality_gate import decide  # noqa: E402
from week9.src.repair_loop import ArtifactBundle  # noqa: E402
from week9.src.schemas import CheckStatus, RunResult, sha256_text  # noqa: E402
from week10.src.online_generation import (  # noqa: E402
    ONLINE_PROMPT_SHA256,
    PROMPT_SHA256,
    V8_ONLINE_GENERATION_SHA256,
)


PUBLIC_SCHEMA_VERSION = "v9-integration-result-1"
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_PUBLIC_CASE_IDS = frozenset(
    {
        "rc_lowpass",
        "dual_room_thermal",
        "rlc_lowpass",
        "negative_missing",
        "negative_conflict",
        "negative_unknown",
    }
)
_SAFE_PROVIDER_NAMES = frozenset(
    {"v8_deterministic_local_grammar", "deepseek", "unknown_provider"}
)
_SAFE_REASON_CODES = frozenset(
    {
        "accepted",
        "response_json_invalid",
        "projection_schema_invalid",
        "unknown_diagnostic",
        "ready_mismatch",
        "local_input_blocked",
        "template_mismatch",
        "parameter_count_mismatch",
        "duplicate_parameter",
        "parameter_name_mismatch",
        "unit_mismatch",
        "source_span_mismatch",
        "value_mismatch",
        "api_transport_error",
        "response_contract_invalid",
        "response_model_mismatch",
        "usage_contract_invalid",
        "local_generation_error",
    }
)
_SAFE_PARAMETER_NAMES = frozenset(
    {
        "resistance",
        "capacitance",
        "cutoff_frequency",
        "input_voltage",
        "inductance",
        "input_resistance",
        "feedback_resistance",
        "positive_supply",
        "negative_supply",
        "heat_capacity",
        "thermal_resistance",
        "initial_temperature",
        "outdoor_temperature",
        "heat_capacity_a",
        "heat_capacity_b",
        "shared_resistance",
        "outdoor_resistance_a",
        "outdoor_resistance_b",
        "initial_temperature_a",
        "initial_temperature_b",
        "unknown_parameter",
    }
)
_DISPOSITIONS = frozenset(
    {"accepted", "content_rejection", "infrastructure_error"}
)
_CHECK_PUBLIC_STATUSES = frozenset(
    {"PASS", "FAIL", "ERROR", "UNVERIFIED", "NOT_APPLICABLE", "MISSING"}
)


class RunStage(str, Enum):
    GENERATION_ERROR = "generation_error"
    GENERATION_BLOCKED = "generation_blocked"
    PIPELINE_ERROR = "pipeline_error"
    PIPELINE_COMPLETE = "pipeline_complete"


@dataclass(frozen=True, slots=True)
class V9RunResult:
    """In-memory result with an explicitly allow-listed public projection."""

    case_id: str
    run_id: str
    stage: RunStage
    provider: str
    prompt_arm: str | None
    prompt_sha256: str
    response_model: str | None
    response_valid: bool | None
    usage: Mapping[str, int] | None
    api_call_count: int
    reason_code: str
    parameter_keys: tuple[str, ...]
    legacy_disposition: str
    projection_accepted: bool | None
    local_fallback_used: bool
    pipeline_attempted: bool
    pipeline_success: bool | None
    hard_gate_passed: bool | None
    quality_decision_passed: bool | None
    published: bool
    required_check_statuses: Mapping[str, str]
    run_result_version: str | None
    duration_s: float
    generation: NaturalLanguageGenerationResult | None = None
    run_result: RunResult | None = None
    error: BaseException | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "stage", RunStage(self.stage))
        object.__setattr__(self, "parameter_keys", tuple(self.parameter_keys))
        _validate_run_id(self.run_id)
        object.__setattr__(
            self,
            "required_check_statuses",
            MappingProxyType(dict(self.required_check_statuses)),
        )
        if self.provider not in _SAFE_PROVIDER_NAMES:
            raise ValueError("unsafe provider category")
        if self.prompt_arm not in {None, "A", "B"}:
            raise ValueError("unsupported prompt arm")
        if self.reason_code not in _SAFE_REASON_CODES:
            raise ValueError("unsafe reason category")
        if self.legacy_disposition not in _DISPOSITIONS:
            raise ValueError("unsafe disposition category")
        if any(name not in _SAFE_PARAMETER_NAMES for name in self.parameter_keys):
            raise ValueError("unsafe parameter category")
        if type(self.api_call_count) is not int or self.api_call_count < 0:
            raise ValueError("api_call_count must be non-negative")
        if type(self.local_fallback_used) is not bool or self.local_fallback_used:
            raise ValueError("V9 does not permit a local fallback")
        for name in (
            "response_valid",
            "projection_accepted",
            "pipeline_success",
            "hard_gate_passed",
            "quality_decision_passed",
        ):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise TypeError(f"{name} must be bool or null")
        if type(self.pipeline_attempted) is not bool or type(self.published) is not bool:
            raise TypeError("pipeline_attempted and published must be bool")
        if self.duration_s < 0 or not math.isfinite(self.duration_s):
            raise ValueError("duration_s must be finite and non-negative")
        if self.usage is not None:
            if set(self.usage) != {"prompt_tokens", "completion_tokens", "total_tokens"}:
                raise ValueError("usage fields differ from the public contract")
            values = tuple(self.usage[name] for name in (
                "prompt_tokens", "completion_tokens", "total_tokens"
            ))
            if any(type(value) is not int or value < 0 for value in values):
                raise ValueError("usage values must be non-negative integers")
            if values[2] != values[0] + values[1]:
                raise ValueError("usage total differs from its components")
            object.__setattr__(self, "usage", MappingProxyType(dict(self.usage)))
        if set(self.required_check_statuses) - set(REQUIRED_HARD_CHECK_TYPES):
            raise ValueError("unknown hard-check name")
        if any(value not in _CHECK_PUBLIC_STATUSES for value in self.required_check_statuses.values()):
            raise ValueError("unknown hard-check status")
        if self.response_model is not None and self.provider != "deepseek":
            raise ValueError("only a verified online response model may be public")
        if not self.pipeline_attempted and any(
            value is not None
            for value in (
                self.pipeline_success,
                self.hard_gate_passed,
                self.quality_decision_passed,
            )
        ):
            raise ValueError("unattempted downstream outcomes must be null")
        if self.published and not (
            self.pipeline_success is True
            and self.hard_gate_passed is True
            and self.quality_decision_passed is True
        ):
            raise ValueError("published requires all downstream decisions")

    def public_dict(self) -> dict[str, object]:
        """Serialize only fixed safe metadata; never serialize inner evidence."""

        public_case_id = self.case_id if self.case_id in _public_cases() else "unknown_case"
        return {
            "schema_version": PUBLIC_SCHEMA_VERSION,
            "version": "v9",
            "case_id": public_case_id,
            "stage": self.stage.value,
            "provider": self.provider,
            "prompt_arm": self.prompt_arm,
            "prompt_sha256": self.prompt_sha256,
            "v8_online_generation_sha256": V8_ONLINE_GENERATION_SHA256,
            "response_model": self.response_model,
            "response_valid": self.response_valid,
            "usage": (
                {
                    name: self.usage[name]
                    for name in ("prompt_tokens", "completion_tokens", "total_tokens")
                }
                if self.usage is not None
                else None
            ),
            "api_call_count": self.api_call_count,
            "reason_code": self.reason_code,
            "parameter_keys": list(self.parameter_keys),
            "legacy_disposition": self.legacy_disposition,
            "projection_accepted": self.projection_accepted,
            "local_fallback_used": self.local_fallback_used,
            "pipeline_attempted": self.pipeline_attempted,
            "pipeline_success": self.pipeline_success,
            "hard_gate_passed": self.hard_gate_passed,
            "quality_decision_passed": self.quality_decision_passed,
            "published": self.published,
            "required_check_statuses": {
                name: self.required_check_statuses[name]
                for name in REQUIRED_HARD_CHECK_TYPES
                if name in self.required_check_statuses
            },
            "run_result_version": self.run_result_version,
            "duration_s": self.duration_s,
        }


def run_case(
    case_id: str,
    requirement_text: str,
    *,
    workspace_root: str | Path,
    generation_provider=None,
    run_id: str | None = None,
) -> V9RunResult:
    """Run one public or synthetic case through generation and the V8 pipeline."""

    _validate_case_id(case_id)
    selected_run_id = run_id or f"v9_{uuid.uuid4().hex}"
    _validate_run_id(selected_run_id)
    if not isinstance(requirement_text, str) or not requirement_text.strip():
        raise ValueError("requirement_text must be a non-empty string")
    selected_workspace = _validate_workspace(workspace_root)
    provider = generation_provider or DeterministicNaturalLanguageProvider()
    provider_name = _provider_category(provider)
    prompt_arm = getattr(provider, "prompt_arm", None) if provider_name == "deepseek" else None
    prompt_hash = _prompt_hash(provider, provider_name, prompt_arm)
    started = time.monotonic()

    try:
        generation = provider.generate(requirement_text)
        if not isinstance(generation, NaturalLanguageGenerationResult):
            raise TypeError("provider returned an invalid generation result")
        if (
            generation.requirement_text != requirement_text
            or generation.requirement_sha256 != sha256_text(requirement_text)
        ):
            raise ValueError("generation result is bound to another requirement")
    except Exception as exc:
        diagnostic = _safe_diagnostic(provider, default_reason=(
            "local_generation_error" if provider_name != "deepseek" else "unknown_diagnostic"
        ))
        return _result(
            case_id=case_id,
            run_id=selected_run_id,
            stage=RunStage.GENERATION_ERROR,
            provider=provider_name,
            prompt_arm=prompt_arm,
            prompt_sha256=prompt_hash,
            response_model=_verified_response_model(provider),
            response_valid=_nullable_bool(getattr(provider, "last_response_valid", None)),
            usage=_safe_usage(getattr(provider, "last_usage", None)),
            api_call_count=_safe_call_count(provider),
            diagnostic=diagnostic,
            projection_accepted=(None if provider_name != "deepseek" else diagnostic["projection_accepted"]),
            pipeline_attempted=False,
            duration_s=time.monotonic() - started,
            error=exc,
        )

    diagnostic = _generation_diagnostic(provider, provider_name, generation)
    usage = _generation_usage(provider, provider_name, generation)
    projection_accepted = (
        None if provider_name != "deepseek" else bool(diagnostic["projection_accepted"])
    )
    if not generation.ready:
        return _result(
            case_id=case_id,
            run_id=selected_run_id,
            stage=RunStage.GENERATION_BLOCKED,
            provider=provider_name,
            prompt_arm=prompt_arm,
            prompt_sha256=prompt_hash,
            response_model=_verified_response_model(provider),
            response_valid=(None if provider_name != "deepseek" else _nullable_bool(
                getattr(provider, "last_response_valid", True)
            )),
            usage=usage,
            api_call_count=(generation.api_call_count if provider_name == "deepseek" else 0),
            diagnostic=diagnostic,
            projection_accepted=projection_accepted,
            pipeline_attempted=False,
            duration_s=time.monotonic() - started,
            generation=generation,
        )

    try:
        if provider_name == "deepseek":
            # The V9 verifier has already reduced the online natural-language
            # response to an exact structured observation contract.  Enter V8
            # at that boundary so its route records the work it actually does;
            # V9 retains the upstream online provenance and cost metadata.
            pipeline = CertifiedTemplateRepairPipeline.from_observations(
                template_id=generation.template_id,
                model_name=generation.model_name,
                observations=generation.observations,
                workspace_root=selected_workspace,
            )
            pipeline_run_kwargs = {
                "traceability_input": generation.traceability_input,
            }
        else:
            pipeline = CertifiedTemplateRepairPipeline.from_natural_language_generation(
                generation=generation,
                workspace_root=selected_workspace,
            )
            pipeline_run_kwargs = {}
        initial_artifacts = ArtifactBundle(
            requirement=generation.requirement_text,
            sysml=pipeline.canonical_sysml,
            modelica=pipeline.canonical_render.code,
        )
        run_result = pipeline.run(
            case_id=case_id,
            run_id=selected_run_id,
            initial_artifacts=initial_artifacts,
            **pipeline_run_kwargs,
        )
        if not isinstance(run_result, RunResult):
            raise TypeError("pipeline returned an invalid run result")
    except Exception as exc:
        return _result(
            case_id=case_id,
            run_id=selected_run_id,
            stage=RunStage.PIPELINE_ERROR,
            provider=provider_name,
            prompt_arm=prompt_arm,
            prompt_sha256=prompt_hash,
            response_model=_verified_response_model(provider),
            response_valid=_successful_response_valid(provider, provider_name),
            usage=usage,
            api_call_count=(generation.api_call_count if provider_name == "deepseek" else 0),
            diagnostic=diagnostic,
            projection_accepted=projection_accepted,
            pipeline_attempted=True,
            pipeline_success=False,
            duration_s=time.monotonic() - started,
            generation=generation,
            error=exc,
        )

    try:
        statuses, hard_gate_passed = _hard_gate_statuses(run_result)
        quality_passed = decide(run_result.checks).overall_success
    except Exception as exc:
        return _result(
            case_id=case_id,
            run_id=selected_run_id,
            stage=RunStage.PIPELINE_ERROR,
            provider=provider_name,
            prompt_arm=prompt_arm,
            prompt_sha256=prompt_hash,
            response_model=_verified_response_model(provider),
            response_valid=_successful_response_valid(provider, provider_name),
            usage=usage,
            api_call_count=(generation.api_call_count if provider_name == "deepseek" else 0),
            diagnostic=diagnostic,
            projection_accepted=projection_accepted,
            pipeline_attempted=True,
            pipeline_success=False,
            duration_s=time.monotonic() - started,
            generation=generation,
            run_result=run_result,
            error=exc,
        )
    pipeline_success = bool(run_result.overall_success)
    published = pipeline_success and hard_gate_passed and quality_passed
    return _result(
        case_id=case_id,
        run_id=selected_run_id,
        stage=RunStage.PIPELINE_COMPLETE,
        provider=provider_name,
        prompt_arm=prompt_arm,
        prompt_sha256=prompt_hash,
        response_model=_verified_response_model(provider),
        response_valid=_successful_response_valid(provider, provider_name),
        usage=usage,
        api_call_count=(generation.api_call_count if provider_name == "deepseek" else 0),
        diagnostic=diagnostic,
        projection_accepted=projection_accepted,
        pipeline_attempted=True,
        pipeline_success=pipeline_success,
        hard_gate_passed=hard_gate_passed,
        quality_decision_passed=quality_passed,
        published=published,
        required_check_statuses=statuses,
        run_result_version=run_result.version,
        duration_s=time.monotonic() - started,
        generation=generation,
        run_result=run_result,
    )


def _result(
    *,
    case_id: str,
    run_id: str,
    stage: RunStage,
    provider: str,
    prompt_arm: str | None,
    prompt_sha256: str,
    response_model: str | None,
    response_valid: bool | None,
    usage: Mapping[str, int] | None,
    api_call_count: int,
    diagnostic: Mapping[str, object],
    projection_accepted: bool | None,
    pipeline_attempted: bool,
    duration_s: float,
    pipeline_success: bool | None = None,
    hard_gate_passed: bool | None = None,
    quality_decision_passed: bool | None = None,
    published: bool = False,
    required_check_statuses: Mapping[str, str] | None = None,
    run_result_version: str | None = None,
    generation: NaturalLanguageGenerationResult | None = None,
    run_result: RunResult | None = None,
    error: BaseException | None = None,
) -> V9RunResult:
    return V9RunResult(
        case_id=case_id,
        run_id=run_id,
        stage=stage,
        provider=provider,
        prompt_arm=prompt_arm,
        prompt_sha256=prompt_sha256,
        response_model=response_model,
        response_valid=response_valid,
        usage=usage,
        api_call_count=api_call_count,
        reason_code=str(diagnostic["reason_code"]),
        parameter_keys=tuple(diagnostic["parameter_keys"]),
        legacy_disposition=str(diagnostic["legacy_disposition"]),
        projection_accepted=projection_accepted,
        local_fallback_used=False,
        pipeline_attempted=pipeline_attempted,
        pipeline_success=pipeline_success,
        hard_gate_passed=hard_gate_passed,
        quality_decision_passed=quality_decision_passed,
        published=published,
        required_check_statuses=required_check_statuses or {},
        run_result_version=run_result_version,
        duration_s=duration_s,
        generation=generation,
        run_result=run_result,
        error=error,
    )


def _generation_diagnostic(provider, provider_name: str, generation) -> dict[str, object]:
    if provider_name == "deepseek":
        return _safe_diagnostic(
            provider,
            default_reason=("accepted" if generation.ready else "unknown_diagnostic"),
        )
    return {
        "reason_code": "accepted" if generation.ready else "local_input_blocked",
        "parameter_keys": (),
        "projection_accepted": False,
        "legacy_disposition": "accepted" if generation.ready else "content_rejection",
    }


def _safe_diagnostic(provider, *, default_reason: str) -> dict[str, object]:
    value = getattr(provider, "last_diagnostic", None)
    if isinstance(value, Mapping):
        reason = value.get("reason_code")
        keys = value.get("parameter_keys")
        projection = value.get("projection_accepted")
        disposition = value.get("legacy_disposition")
        if (
            reason in _SAFE_REASON_CODES
            and isinstance(keys, (list, tuple))
            and all(key in _SAFE_PARAMETER_NAMES for key in keys)
            and type(projection) is bool
            and disposition in _DISPOSITIONS
        ):
            return {
                "reason_code": reason,
                "parameter_keys": tuple(keys),
                "projection_accepted": projection,
                "legacy_disposition": disposition,
            }
    return {
        "reason_code": default_reason,
        "parameter_keys": (),
        "projection_accepted": default_reason == "accepted",
        "legacy_disposition": (
            "accepted" if default_reason == "accepted" else "infrastructure_error"
        ),
    }


def _generation_usage(provider, provider_name: str, generation) -> dict[str, int] | None:
    if provider_name != "deepseek":
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    usage = _safe_usage(getattr(provider, "last_usage", None))
    if usage is not None:
        return usage
    return {
        "prompt_tokens": generation.prompt_tokens,
        "completion_tokens": generation.completion_tokens,
        "total_tokens": generation.total_tokens,
    }


def _safe_usage(value: object) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    names = ("prompt_tokens", "completion_tokens", "total_tokens")
    if set(value) != set(names):
        return None
    counts = tuple(value.get(name) for name in names)
    if any(type(item) is not int or item < 0 for item in counts):
        return None
    if counts[2] != counts[0] + counts[1]:
        return None
    return dict(zip(names, counts))


def _hard_gate_statuses(run_result: RunResult) -> tuple[dict[str, str], bool]:
    indexed = {check.check_type: check for check in run_result.checks}
    statuses = {
        name: (
            indexed[name].status.value if name in indexed else "MISSING"
        )
        for name in REQUIRED_HARD_CHECK_TYPES
    }
    passed = all(
        name in indexed
        and indexed[name].required
        and indexed[name].status is CheckStatus.PASS
        for name in REQUIRED_HARD_CHECK_TYPES
    )
    return statuses, passed


def _provider_category(provider) -> str:
    name = getattr(provider, "provider", "")
    return name if name in _SAFE_PROVIDER_NAMES - {"unknown_provider"} else "unknown_provider"


def _prompt_hash(provider, provider_name: str, prompt_arm: str | None) -> str:
    if provider_name != "deepseek":
        return ""
    value = getattr(provider, "prompt_sha256", "")
    expected = ONLINE_PROMPT_SHA256 if prompt_arm == "A" else PROMPT_SHA256
    return value if value == expected else ""


def _verified_response_model(provider) -> str | None:
    value = getattr(provider, "last_response_model", None)
    expected = getattr(provider, "model", None)
    return value if isinstance(value, str) and value == expected else None


def _safe_call_count(provider) -> int:
    value = getattr(provider, "api_call_count", 0)
    return value if type(value) is int and value >= 0 else 0


def _nullable_bool(value: object) -> bool | None:
    return value if type(value) is bool else None


def _successful_response_valid(provider, provider_name: str) -> bool | None:
    if provider_name != "deepseek":
        return None
    value = getattr(provider, "last_response_valid", None)
    return value if type(value) is bool else True


def _validate_case_id(case_id: str) -> None:
    if not isinstance(case_id, str) or not _SAFE_IDENTIFIER.fullmatch(case_id) or ".." in case_id:
        raise ValueError("unsafe case_id")


def _validate_run_id(run_id: str) -> None:
    if not isinstance(run_id, str) or not _SAFE_IDENTIFIER.fullmatch(run_id) or ".." in run_id:
        raise ValueError("unsafe run_id")


def _validate_workspace(workspace_root: str | Path) -> Path:
    selected = Path(workspace_root).resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if not (_is_within(selected, RESULTS_ROOT.resolve()) or _is_within(selected, temp_root)):
        raise ValueError("workspace_root must be under week10/results or the temporary directory")
    return selected


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _load_suite() -> dict[str, str]:
    data = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
    if data.get("schema_version") != "v9-development-diagnostic-suite-1":
        raise ValueError("unsupported public suite")
    cases = data.get("cases")
    if not isinstance(cases, list):
        raise ValueError("public suite cases must be an array")
    result: dict[str, str] = {}
    for item in cases:
        if not isinstance(item, Mapping):
            raise ValueError("invalid public suite case")
        case_id = item.get("case_id")
        text = item.get("requirement_text")
        _validate_case_id(case_id)
        if not isinstance(text, str) or not text.strip() or case_id in result:
            raise ValueError("invalid public suite case")
        result[case_id] = text
    return result


def _public_cases() -> frozenset[str]:
    return _PUBLIC_CASE_IDS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="V9 safe local integration runner")
    parser.add_argument("--case", required=True, help="public development case id")
    parser.add_argument("--out", required=True, help="new JSON report below week10/results")
    args = parser.parse_args(argv)
    try:
        suite = _load_suite()
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        print("BLOCKED: cli_suite_error", file=sys.stderr)
        return 2
    if args.case not in suite:
        parser.error("unknown public development case")

    output_path = Path(args.out).resolve()
    if not _is_within(output_path, RESULTS_ROOT.resolve()):
        parser.error("output must be below week10/results")
    if output_path.suffix.lower() != ".json" or output_path.exists():
        parser.error("output must be a new JSON file")
    report_dir = output_path.parent
    if report_dir == RESULTS_ROOT.resolve() or report_dir.exists():
        parser.error("output parent must be a new unique directory")
    try:
        report_dir.mkdir(parents=True, exist_ok=False)
        result = run_case(
            args.case,
            suite[args.case],
            workspace_root=report_dir / "work",
        )
        output_path.write_text(
            json.dumps(result.public_dict(), ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        print("BLOCKED: cli_execution_error", file=sys.stderr)
        return 2
    print(
        f"[v9:{result.public_dict()['case_id']}] stage={result.stage.value} "
        f"published={result.published} out={output_path}",
        flush=True,
    )
    return 0 if result.published else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PUBLIC_SCHEMA_VERSION",
    "RunStage",
    "V9RunResult",
    "main",
    "run_case",
]
