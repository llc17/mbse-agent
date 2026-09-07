"""Private single-attempt worker for the blinded V8 formal comparison.

The operating-system command line contains only a public case reference.  The
worker resolves sealed plaintext after validating the final protocol, the
one-batch authorization receipt, and its consumption marker.  All generator
stdout/stderr and candidate contracts stay under the private results root.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from week9.experiments.blinded_formal_execution import (
    DEFAULT_FINAL_PROTOCOL,
    DEFAULT_PRIVATE_RESULTS_ROOT,
    DEVELOPMENT_CASE_REFS,
    FORMAL_ARM_IDS,
    PINNED_DEEPSEEK_MODEL,
    PINNED_DEEPSEEK_THINKING_MODE,
    SEALED_CASE_REFS,
    file_sha256,
    load_sealed_holdout,
    validate_execution_authorization,
)
from week9.experiments.external_evaluator import EvaluationContext, evaluate
from week9.experiments.freeze_independent_holdout_binding import DEFAULT_BINDING
from week9.src.generation import (
    DeterministicNaturalLanguageProvider,
    NaturalLanguageGenerationResult,
)
from week9.src.online_generation import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    DeepSeekNaturalLanguageProvider,
)
from week9.src.pipeline import CertifiedTemplateRepairPipeline
from week9.src.repair_loop import ArtifactBundle
from week9.src.schemas import (
    CheckStatus,
    FailureKind,
    ModelConfiguration,
    QualityCheckResult,
    ReproductionManifest,
    RunCounters,
    RunResult,
    TokenUsage,
)


LEGACY_RUNNER = PROJECT_ROOT / "week8" / "src" / "runner.py"
ARM_VERSION = {
    "v4_default_deepseek": "v4",
    "v6_default_deepseek": "v6",
    "v8_controlled_deepseek": "v8",
    "v8_default_local": "v8",
}


def execute_private_arm(
    *,
    arm_id: str,
    case: Any,
    run_id: str,
    work_root: Path,
) -> RunResult:
    """Execute one already-authorized arm; callers own all authorization gates."""

    if arm_id not in FORMAL_ARM_IDS:
        raise ValueError("unsupported blinded formal arm")
    online_generation = arm_id != "v8_default_local"
    if online_generation:
        _validate_pinned_environment()
    if arm_id.startswith("v4_"):
        with _pinned_legacy_transport():
            result = _run_legacy("v4", case, run_id, work_root)
    elif arm_id.startswith("v6_"):
        with _pinned_legacy_transport():
            result = _run_legacy("v6", case, run_id, work_root)
    else:
        result = _run_v8(
            case,
            run_id,
            work_root,
            online_generation=(arm_id == "v8_controlled_deepseek"),
        )
    if online_generation:
        _bind_pinned_profile(result)
    return result


def _validate_pinned_environment() -> None:
    if os.environ.get("DEEPSEEK_MODEL") != PINNED_DEEPSEEK_MODEL:
        raise PermissionError("DeepSeek model differs from the frozen v2 profile")
    api_base = os.environ.get("DEEPSEEK_API_BASE", "https://api.deepseek.com")
    if urlparse(api_base).hostname != "api.deepseek.com":
        raise PermissionError("DeepSeek API host differs from the frozen v2 profile")


@contextmanager
def _pinned_legacy_transport() -> Iterable[None]:
    """Inject the retired alias' frozen non-thinking semantics into V4/V6."""

    original_post = requests.post

    def pinned_post(url: str, *args: Any, **kwargs: Any) -> Any:
        payload = kwargs.get("json")
        if not isinstance(payload, Mapping):
            raise PermissionError("legacy DeepSeek request payload is unavailable")
        request = dict(payload)
        if request.get("model") != PINNED_DEEPSEEK_MODEL:
            raise PermissionError("legacy DeepSeek request model differs")
        temperature = request.get("temperature")
        if type(temperature) not in {int, float} or float(temperature) != DEFAULT_TEMPERATURE:
            raise PermissionError("legacy DeepSeek request temperature differs")
        max_tokens = request.get("max_tokens")
        if type(max_tokens) is not int or not 0 < max_tokens <= DEFAULT_MAX_TOKENS:
            raise PermissionError("legacy DeepSeek request token limit differs")
        if urlparse(str(url)).hostname != "api.deepseek.com":
            raise PermissionError("legacy DeepSeek request host differs")
        request["thinking"] = {"type": PINNED_DEEPSEEK_THINKING_MODE}
        kwargs["json"] = request
        return original_post(url, *args, **kwargs)

    requests.post = pinned_post
    try:
        yield
    finally:
        requests.post = original_post


def _bind_pinned_profile(result: RunResult) -> None:
    result.model_configuration.provider = "deepseek"
    result.model_configuration.model = PINNED_DEEPSEEK_MODEL
    result.model_configuration.parameters.update(
        {
            "generator_model": PINNED_DEEPSEEK_MODEL,
            "temperature": DEFAULT_TEMPERATURE,
            "max_tokens": DEFAULT_MAX_TOKENS,
            "thinking_mode": PINNED_DEEPSEEK_THINKING_MODE,
        }
    )
    result.raw_metrics["blinded_execution_profile"] = {
        "model": PINNED_DEEPSEEK_MODEL,
        "thinking_mode": PINNED_DEEPSEEK_THINKING_MODE,
    }


class _PinnedDeepSeekNaturalLanguageProvider(DeepSeekNaturalLanguageProvider):
    def _request_payload(self, requirement_text: str) -> dict[str, Any]:
        payload = super()._request_payload(requirement_text)
        payload["thinking"] = {"type": PINNED_DEEPSEEK_THINKING_MODE}
        return payload


def _run_legacy(version: str, case: Any, run_id: str, work_root: Path) -> RunResult:
    from week9.experiments.version_adapters import normalize_legacy_result

    raw_path = work_root / "legacy-raw-result.json"
    module_spec = importlib.util.spec_from_file_location(
        f"v8_blinded_legacy_runner_{version}", LEGACY_RUNNER
    )
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError("legacy runner module is unavailable")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    module._load_env = lambda: None
    original_argv = list(sys.argv)
    try:
        sys.argv = [
            str(LEGACY_RUNNER),
            "--version",
            version,
            "--case",
            case.case_id,
            "--raw-input",
            case.raw_input,
            "--out",
            str(raw_path),
        ]
        try:
            module.main()
        except SystemExit:
            pass
    finally:
        sys.argv = original_argv
    try:
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("legacy result root differs")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raw = {"success": False, "error": "private legacy worker did not produce a valid result"}
    return normalize_legacy_result(raw, case, version, run_id)


def _run_v8(
    case: Any,
    run_id: str,
    work_root: Path,
    *,
    online_generation: bool,
) -> RunResult:
    provider = (
        _PinnedDeepSeekNaturalLanguageProvider(
            api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
            api_base=os.environ.get("DEEPSEEK_API_BASE", "https://api.deepseek.com"),
            model=os.environ.get("DEEPSEEK_MODEL", PINNED_DEEPSEEK_MODEL),
            temperature=DEFAULT_TEMPERATURE,
            max_tokens=DEFAULT_MAX_TOKENS,
        )
        if online_generation
        else DeterministicNaturalLanguageProvider()
    )
    try:
        generation = provider.generate(case.raw_input)
    except Exception as exc:
        return _blocking_result(
            case=case,
            run_id=run_id,
            check_type="natural_language_generation",
            issue_messages=(f"provider failure: {type(exc).__name__}",),
            reason="generation_provider_error",
            failure_kind=FailureKind.INFRASTRUCTURE,
        )
    if not generation.ready:
        return _blocking_result(
            case=case,
            run_id=run_id,
            check_type="natural_language_generation",
            issue_messages=tuple(item.message for item in generation.issues),
            reason=generation.issues[0].code,
            failure_kind=FailureKind.CONTENT,
            generation=generation,
        )
    try:
        pipeline = CertifiedTemplateRepairPipeline.from_natural_language_generation(
            generation=generation,
            workspace_root=work_root / "v8-pipeline",
        )
        initial = ArtifactBundle(
            requirement=generation.requirement_text,
            sysml=pipeline.canonical_sysml,
            modelica=pipeline.canonical_render.code,
        )
        return pipeline.run(
            case_id=case.case_id,
            run_id=run_id,
            initial_artifacts=initial,
        )
    except Exception as exc:
        return _blocking_result(
            case=case,
            run_id=run_id,
            check_type="pipeline.execution",
            issue_messages=(f"pipeline failure: {type(exc).__name__}",),
            reason="pipeline_execution_error",
            failure_kind=FailureKind.INFRASTRUCTURE,
            generation=generation,
        )


def _blocking_result(
    *,
    case: Any,
    run_id: str,
    check_type: str,
    issue_messages: Iterable[str],
    reason: str,
    failure_kind: FailureKind,
    generation: NaturalLanguageGenerationResult | None = None,
) -> RunResult:
    issues = tuple(issue_messages)
    generation_evidence = generation.evidence() if generation is not None else None
    check = QualityCheckResult(
        check_type=check_type,
        status=(CheckStatus.ERROR if failure_kind is FailureKind.INFRASTRUCTURE else CheckStatus.FAIL),
        required=True,
        issues=list(issues),
        evidence={"reason": reason},
        tool_name="week9.blinded_arm_worker",
        tool_version="1",
        failure_kind=failure_kind,
        failure_category=reason,
    )
    return evaluate(
        context=EvaluationContext(
            version="v8",
            case_id=case.case_id,
            run_id=run_id,
            requirement_text=case.raw_input,
            artifacts={},
        ),
        required_check_types=tuple(case.required_checks),
        precomputed_external_checks=[check],
        counters=RunCounters(generation_count=(1 if generation is not None else 0)),
        token_usage=TokenUsage(
            prompt_tokens=(generation.prompt_tokens if generation is not None else 0),
            completion_tokens=(generation.completion_tokens if generation is not None else 0),
            total_tokens=(generation.total_tokens if generation is not None else 0),
        ),
        duration_s=(generation.duration_s if generation is not None else 0.0),
        model_configuration=ModelConfiguration(
            provider=(generation.provider if generation is not None else ""),
            model=("certified_template_adaptive_path" if generation is not None else ""),
            parameters={
                "generator_model": generation.model if generation is not None else "",
                "temperature": generation.temperature if generation is not None else None,
                "max_tokens": generation.max_tokens if generation is not None else 0,
                "api_call_count": generation.api_call_count if generation is not None else 0,
                "natural_language_generation": generation_evidence,
            },
        ),
        reproduction=ReproductionManifest(
            prompt_sha256=(
                str(generation_evidence.get("grammar_sha256") or "")
                if generation_evidence is not None
                else ""
            )
        ),
        infrastructure_errors=(issues if failure_kind is FailureKind.INFRASTRUCTURE else ()),
        raw_metrics={
            "online_api_used": generation.online_api_used if generation is not None else False,
            "natural_language_interpretation_performed": generation is not None,
            "natural_language_generation": generation_evidence,
        },
    )


def _case_from_reference(dataset: str, case_ref: str, binding_path: Path) -> Any:
    if dataset == "holdout":
        if case_ref not in SEALED_CASE_REFS:
            raise ValueError("unknown sealed case reference")
        return load_sealed_holdout(binding_path).case_for_ref(case_ref)
    if dataset == "development":
        if case_ref not in DEVELOPMENT_CASE_REFS:
            raise ValueError("unknown development case reference")
        from week9.experiments.cases import DEVELOPMENT_REGRESSION_CASES

        return DEVELOPMENT_REGRESSION_CASES[case_ref]
    raise ValueError("unsupported blinded dataset")


def _validate_consumption_marker(
    marker_path: Path,
    *,
    authorization_path: Path,
    protocol_sha256: str,
) -> None:
    value = json.loads(marker_path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise PermissionError("authorization consumption marker differs")
    if (
        value.get("schema_version")
        != "v8-blinded-formal-authorization-consumption-2"
        or value.get("authorization_receipt_sha256") != file_sha256(authorization_path)
        or value.get("blinded_protocol_sha256") != protocol_sha256
    ):
        raise PermissionError("authorization consumption marker differs")
    sidecar = marker_path.with_suffix(".sha256")
    if sidecar.read_text(encoding="ascii").strip() != file_sha256(marker_path):
        raise PermissionError("authorization consumption marker hash differs")


def _safe_private_path(path: Path, root: Path) -> Path:
    resolved_root = root.resolve(strict=True)
    resolved = path.resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ValueError("private worker path escapes the private results root")
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", default=str(DEFAULT_FINAL_PROTOCOL))
    parser.add_argument("--authorization", required=True)
    parser.add_argument("--consumption-marker", required=True)
    parser.add_argument("--binding", default=str(DEFAULT_BINDING))
    parser.add_argument("--dataset", choices=("development", "holdout"), required=True)
    parser.add_argument("--case-ref", required=True)
    parser.add_argument("--arm-id", choices=FORMAL_ARM_IDS, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--work-root", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        protocol_path = Path(args.protocol).resolve()
        authorization_path = Path(args.authorization).resolve()
        protocol, _authorization, errors = validate_execution_authorization(
            protocol_path,
            authorization_path,
            confirmation_id=None,
        )
        if errors:
            raise PermissionError("authorization gate failed")
        protocol_sha256 = protocol_path.with_suffix(".sha256").read_text(
            encoding="ascii"
        ).strip()
        _validate_consumption_marker(
            Path(args.consumption_marker).resolve(),
            authorization_path=authorization_path,
            protocol_sha256=protocol_sha256,
        )
        binding_path = Path(args.binding).resolve()
        if binding_path != (PROJECT_ROOT / str(protocol["holdout_binding"]["path"])).resolve():
            raise PermissionError("holdout binding path differs from final protocol")
        private_root = Path(str(protocol["private_execution_boundary"]["root"])).resolve(
            strict=True
        )
        if private_root != DEFAULT_PRIVATE_RESULTS_ROOT.resolve(strict=True):
            raise PermissionError("private results root differs from final protocol")
        work_root = _safe_private_path(Path(args.work_root), private_root)
        output_path = _safe_private_path(Path(args.out), private_root)
        if output_path.exists():
            raise FileExistsError("candidate contract already exists")
        work_root.mkdir(parents=True, exist_ok=True)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        case = _case_from_reference(args.dataset, args.case_ref, binding_path)
        stdout_log = work_root / "private-worker.stdout.log"
        stderr_log = work_root / "private-worker.stderr.log"
        with stdout_log.open("w", encoding="utf-8") as stdout_handle, stderr_log.open(
            "w", encoding="utf-8"
        ) as stderr_handle, redirect_stdout(stdout_handle), redirect_stderr(stderr_handle):
            result = execute_private_arm(
                arm_id=args.arm_id,
                case=case,
                run_id=args.run_id,
                work_root=work_root,
            )
        output_path.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
    except Exception:
        print("BLINDED_ARM_WORKER_BLOCKED_OR_FAILED", file=sys.stderr)
        return 2
    print(
        f"BLINDED_ARM_WORKER_COMPLETE case_ref={args.case_ref} arm_id={args.arm_id}",
        flush=True,
    )
    return 0 if result.overall_success else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ARM_VERSION", "execute_private_arm", "main"]
