"""V8 single-case CLI with a fail-closed natural-language entry.

Development cases first pass through the audited local grammar in
``week9.src.generation``. A successful interpretation still has to traverse
normalization, certified rendering, the complete hard gate, bounded repair,
adaptive routing, budget accounting, and traceability. Unknown or ambiguous
input is written as a machine-readable failure contract.

The default runner performs no network or paid API call.  The explicit
``deepseek-online`` provider remains blocked unless the shared frozen protocol
authorizes the selected online milestone. Holdout access has an additional
formal-access guard.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from week9.experiments.cases import get_case  # noqa: E402
from week9.experiments.external_evaluator import EvaluationContext, evaluate  # noqa: E402
from week9.experiments.online_policy import assert_online_allowed  # noqa: E402
from week9.src.generation import (  # noqa: E402
    DeterministicNaturalLanguageProvider,
    NaturalLanguageGenerationProvider,
    NaturalLanguageGenerationResult,
)
from week9.src.pipeline import CertifiedTemplateRepairPipeline  # noqa: E402
from week9.src.online_generation import (  # noqa: E402
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    DeepSeekNaturalLanguageProvider,
)
from week9.src.repair_loop import ArtifactBundle  # noqa: E402
from week9.src.schemas import (  # noqa: E402
    CheckStatus,
    FailureKind,
    ModelConfiguration,
    QualityCheckResult,
    ReproductionManifest,
    RunCounters,
    TokenUsage,
)


PROTOCOL_PATH = PROJECT_ROOT / "week9" / "configs" / "experiment_protocol.json"
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def build_stage0_result(
    case_id: str,
    run_id: str,
    *,
    raw_input: str | None = None,
    case_sha256: str | None = None,
    formal_holdout_access: bool = False,
    workspace_root: str | Path | None = None,
    generation_provider: NaturalLanguageGenerationProvider | None = None,
):
    """Run one frozen case or return a complete blocking result.

    The historical function name is retained for adapter compatibility. It is
    now the Stage-0 end-to-end entry rather than a permanent NOT_IMPLEMENTED
    stub.
    """

    _validate_identifier(case_id, "case_id")
    _validate_identifier(run_id, "run_id")
    holdout_blocked = formal_holdout_access and not _holdout_access_enabled()
    case = None if holdout_blocked else get_case(
        case_id, include_holdout=formal_holdout_access
    )
    if case is None:
        reason = "holdout_access_disabled" if holdout_blocked else "unknown_case"
        issue = (
            "留出集只允许在代码、配置、阈值和正式访问开关冻结后使用"
            if holdout_blocked
            else f"未知或当前不可访问的用例：{case_id}"
        )
        return _blocking_result(
            case_id=case_id,
            run_id=run_id,
            requirement_text="",
            required_checks=("case.lookup",),
            check_type="case.lookup",
            issue_messages=(issue,),
            reason=reason,
            failure_kind=FailureKind.INFRASTRUCTURE,
            evidence={"formal_holdout_access": formal_holdout_access},
        )

    requirement_text = raw_input if raw_input is not None else case.raw_input
    contract_errors: list[str] = []
    if raw_input is not None and raw_input != case.raw_input:
        contract_errors.append("传入需求与冻结 case contract 不一致")
    if case_sha256 is not None and case_sha256 != case.sha256:
        contract_errors.append("传入 case hash 与冻结 case contract 不一致")
    if contract_errors:
        return _blocking_result(
            case_id=case_id,
            run_id=run_id,
            requirement_text=requirement_text,
            required_checks=case.required_checks,
            check_type="case.contract",
            issue_messages=contract_errors,
            reason="case_contract_mismatch",
            failure_kind=FailureKind.INFRASTRUCTURE,
            evidence={
                "expected_sha256": case.sha256,
                "received_sha256": case_sha256 or "",
            },
        )

    provider = generation_provider or DeterministicNaturalLanguageProvider()
    try:
        generation = provider.generate(requirement_text)
    except Exception as exc:  # provider failure is observable and never a release
        return _blocking_result(
            case_id=case_id,
            run_id=run_id,
            requirement_text=requirement_text,
            required_checks=case.required_checks,
            check_type="natural_language_generation",
            issue_messages=(f"自然语言解释器异常：{type(exc).__name__}: {exc}",),
            reason="generation_provider_error",
            failure_kind=FailureKind.INFRASTRUCTURE,
            evidence={"provider_exception_type": type(exc).__name__},
        )
    if not generation.ready:
        return _generation_blocked_result(case, run_id, generation)

    selected_workspace = (
        Path(workspace_root).resolve()
        if workspace_root is not None
        else (
            Path(tempfile.gettempdir()) / "v8-natural-language-runner" / run_id
        ).resolve()
    )
    try:
        pipeline = CertifiedTemplateRepairPipeline.from_natural_language_generation(
            generation=generation,
            workspace_root=selected_workspace,
        )
        initial_artifacts = ArtifactBundle(
            requirement=generation.requirement_text,
            sysml=pipeline.canonical_sysml,
            modelica=pipeline.canonical_render.code,
        )
        return pipeline.run(
            case_id=case.case_id,
            run_id=run_id,
            initial_artifacts=initial_artifacts,
        )
    except Exception as exc:  # always preserve a machine-readable failure
        return _blocking_result(
            case_id=case_id,
            run_id=run_id,
            requirement_text=requirement_text,
            required_checks=case.required_checks,
            check_type="pipeline.execution",
            issue_messages=(f"V8 pipeline 异常：{type(exc).__name__}: {exc}",),
            reason="pipeline_execution_error",
            failure_kind=FailureKind.INFRASTRUCTURE,
            evidence={
                "generation": generation.evidence(),
                "workspace_root": str(selected_workspace),
                "exception_type": type(exc).__name__,
            },
            generation=generation,
        )


def _generation_blocked_result(case, run_id: str, generation):
    return _blocking_result(
        case_id=case.case_id,
        run_id=run_id,
        requirement_text=generation.requirement_text,
        required_checks=case.required_checks,
        check_type="natural_language_generation",
        issue_messages=tuple(item.message for item in generation.issues),
        reason=generation.issues[0].code,
        failure_kind=FailureKind.CONTENT,
        evidence={"generation": generation.evidence()},
        generation=generation,
    )


def _blocking_result(
    *,
    case_id: str,
    run_id: str,
    requirement_text: str,
    required_checks: Iterable[str],
    check_type: str,
    issue_messages: Iterable[str],
    reason: str,
    failure_kind: FailureKind,
    evidence: dict | None = None,
    generation: NaturalLanguageGenerationResult | None = None,
):
    issues = tuple(issue_messages)
    check = QualityCheckResult(
        check_type=check_type,
        status=(
            CheckStatus.ERROR
            if failure_kind is FailureKind.INFRASTRUCTURE
            else CheckStatus.FAIL
        ),
        required=True,
        issues=list(issues),
        evidence={"reason": reason, **dict(evidence or {})},
        tool_name="week9.runner",
        tool_version="stage0-natural-language-v1",
        failure_kind=failure_kind,
        failure_category=reason,
    )
    generation_evidence = generation.evidence() if generation is not None else None
    return evaluate(
        context=EvaluationContext(
            version="v8",
            case_id=case_id,
            run_id=run_id,
            requirement_text=requirement_text,
            artifacts={},
        ),
        required_check_types=tuple(required_checks),
        precomputed_external_checks=[check],
        counters=RunCounters(generation_count=(1 if generation is not None else 0)),
        token_usage=TokenUsage(
            prompt_tokens=(generation.prompt_tokens if generation is not None else 0),
            completion_tokens=(
                generation.completion_tokens if generation is not None else 0
            ),
            total_tokens=(generation.total_tokens if generation is not None else 0),
        ),
        duration_s=(generation.duration_s if generation is not None else 0.0),
        model_configuration=ModelConfiguration(
            provider=(generation.provider if generation is not None else ""),
            model=(
                "certified_template_adaptive_path"
                if generation is not None
                else ""
            ),
            parameters={
                "generator_model": (
                    generation.model if generation is not None else ""
                ),
                "temperature": (
                    generation.temperature if generation is not None else None
                ),
                "max_tokens": (
                    generation.max_tokens if generation is not None else 0
                ),
                "api_call_count": (
                    generation.api_call_count if generation is not None else 0
                ),
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
        infrastructure_errors=(
            issues if failure_kind is FailureKind.INFRASTRUCTURE else ()
        ),
        raw_metrics={
            "online_api_used": (
                generation.online_api_used if generation is not None else False
            ),
            "natural_language_interpretation_performed": generation is not None,
            "natural_language_generation": generation_evidence,
        },
    )


def _holdout_access_enabled() -> bool:
    try:
        protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(
        protocol.get("formal_online_runs_allowed")
        and protocol.get("holdout_policy", {}).get("access_enabled")
        and protocol.get("online_execution_policy", {}).get("user_confirmation_id")
    )


def _validate_identifier(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or not _SAFE_IDENTIFIER.fullmatch(value)
        or ".." in value
    ):
        raise ValueError(f"unsafe {field_name}: {value!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="V8 single-version, single-case runner")
    parser.add_argument("--case", required=True, help="case id")
    parser.add_argument("--out", required=True, help="RunResult JSON path")
    parser.add_argument("--run-id", default="stage0_manual", help="unique run id")
    parser.add_argument("--raw-input", default=None, help="exact frozen requirement text")
    parser.add_argument("--case-sha256", default=None, help="expected frozen case hash")
    parser.add_argument(
        "--formal-holdout-access",
        action="store_true",
        help="request controlled holdout access; protocol must already permit it",
    )
    parser.add_argument(
        "--generation-provider",
        choices=("local", "deepseek-online"),
        default="local",
        help="local is offline; deepseek-online requires frozen authorization",
    )
    parser.add_argument(
        "--online-run-kind",
        choices=("smoke", "pilot", "formal"),
        default="smoke",
        help="authorization milestone for an explicitly selected online provider",
    )
    args = parser.parse_args(argv)

    generation_provider = None
    if args.generation_provider == "deepseek-online":
        try:
            assert_online_allowed(allow_online=True, run_kind=args.online_run_kind)
            generation_provider = DeepSeekNaturalLanguageProvider(
                api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
                api_base=os.environ.get(
                    "DEEPSEEK_API_BASE", "https://api.deepseek.com"
                ),
                model=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro"),
                temperature=DEFAULT_TEMPERATURE,
                max_tokens=DEFAULT_MAX_TOKENS,
            )
        except (PermissionError, ValueError) as exc:
            print(f"BLOCKED: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2

    output_path = Path(args.out).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result = build_stage0_result(
        args.case,
        args.run_id,
        raw_input=args.raw_input,
        case_sha256=args.case_sha256,
        formal_holdout_access=args.formal_holdout_access,
        workspace_root=output_path.parent / f"{args.run_id}.work",
        generation_provider=generation_provider,
    )
    output_path.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    print(
        f"[v8:{args.case}] {result.routing.path.value} "
        f"overall_success={result.overall_success} out={output_path}",
        flush=True,
    )
    return 0 if result.overall_success else 2


if __name__ == "__main__":
    raise SystemExit(main())
