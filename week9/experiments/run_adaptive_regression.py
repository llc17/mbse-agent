"""Run the V8-only offline Stage-4 adaptive mechanism regression."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import re
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEEK9_ROOT = PROJECT_ROOT / "week9"
SUITE_PATH = WEEK9_ROOT / "configs" / "stage4_adaptive_regression_suite.json"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from week9.experiments.report_adaptive_regression import (  # noqa: E402
    build_adaptive_regression_report,
)
from week9.src.modelica_templates import representative_parameters  # noqa: E402
from week9.src.modelica_tool import resolve_omc_path  # noqa: E402
from week9.src.parameter_normalization import (  # noqa: E402
    ParameterNormalizationError,
    ParameterObservation,
    parameter_observations_sha256,
)
from week9.src.pipeline import (  # noqa: E402
    AdaptivePreparationError,
    CertifiedTemplateRepairPipeline,
)
from week9.src.repair_loop import ArtifactBundle, RepairLoopConfig  # noqa: E402
from week9.src.retrieval import (  # noqa: E402
    RetrievalQuery,
    load_default_corpus,
    load_default_policy,
    retrieve_knowledge,
)
from week9.src.schemas import (  # noqa: E402
    BudgetDimension,
    RunResult,
    TraceLinkStatus,
    sha256_text,
)
from week9.src.traceability import (  # noqa: E402
    EXPLICIT_BINDING_BASIS,
    TEMPLATE_TRACE_REGISTRY,
    RequirementItem,
    RequirementTargetBinding,
    TraceabilityInput,
)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _model_name(case_id: str) -> str:
    return "Stage4_" + re.sub(r"[^A-Za-z0-9_]", "_", case_id)


def _trace_input(
    template_id: str, *, mark_first_not_implemented: bool = False
) -> tuple[str, TraceabilityInput]:
    registry = TEMPLATE_TRACE_REGISTRY[template_id]
    parts: list[str] = []
    requirements: list[RequirementItem] = []
    bindings: list[RequirementTargetBinding] = []
    offset = 0
    for index, target_id in enumerate(registry.expected_target_ids, start=1):
        text = f"Requirement for {target_id}"
        requirement_id = f"REQ-{index:03d}"
        requirements.append(
            RequirementItem(
                requirement_id=requirement_id,
                text=text,
                source_start=offset,
                source_end=offset + len(text),
                critical=True,
                not_implemented_reason=(
                    "explicit upstream requirement is not implemented"
                    if mark_first_not_implemented and index == 1
                    else ""
                ),
            )
        )
        bindings.append(
            RequirementTargetBinding(
                requirement_id=requirement_id,
                target_id=target_id,
                basis=EXPLICIT_BINDING_BASIS,
            )
        )
        parts.append(text)
        offset += len(text)
        if index != len(registry.expected_target_ids):
            parts.append("\n")
            offset += 1
    requirement = "".join(parts)
    return requirement, TraceabilityInput(
        requirement_artifact_sha256=sha256_text(requirement),
        requirements=tuple(requirements),
        bindings=tuple(bindings),
    )


def _structured_observations(template_id: str) -> tuple[ParameterObservation, ...]:
    return tuple(
        ParameterObservation(
            name=item.canonical_name,
            value=item.value,
            unit=item.unit,
            source_text=f"{item.canonical_name}={item.value} {item.unit}",
        )
        for item in representative_parameters(template_id)
    )


def _retrieval_assisted_rlc():
    observations = (
        ParameterObservation("R", 100.0, "Ohm", "R=100 Ohm"),
        ParameterObservation("L", 10.0, "mH", "L=10 mH"),
        ParameterObservation("C", 25.33029591058444, "nF", "C=25.33 nF"),
        ParameterObservation("f0", 10.0, "kHz", "f0=10 kHz"),
        ParameterObservation(
            "source_amplitude", 5.0, "V", "source_amplitude=5 V"
        ),
    )
    corpus = load_default_corpus()
    policy = replace(load_default_policy(corpus), enabled=True)
    query = RetrievalQuery(
        template_id="rlc_filter",
        domain="electrical",
        target_language="structured_parameter",
        repair_layer="requirement",
        check_type="requirement_normalization",
        failure_category="parameter_normalization_failed",
        diagnostic_subcategory="unknown_parameter_alias",
        text="rlc_filter unknown source_amplitude",
        trigger_reason="explicit_knowledge_gap",
        failed_snapshot_sha256=parameter_observations_sha256(
            "rlc_filter", observations
        ),
        local_resolution_attempted=True,
        symbols=("source_amplitude",),
        library="Modelica Standard Library",
        library_version="4.0.0",
        allowed_scope="alias_resolution",
    )
    retrieval = retrieve_knowledge(query, corpus, policy)
    if not retrieval.triggered:
        raise RuntimeError("the frozen retrieval case did not select its certified alias")
    return observations, retrieval


def _actual_from_result(result: RunResult) -> dict[str, Any]:
    trace = result.traceability
    critical = set(trace.critical_requirement_ids if trace is not None else ())
    actions = [item.action.value for item in result.routing.events]
    ledger = result.budget_ledger
    return {
        "input_kind": result.routing.input_kind.value,
        "path": result.routing.path.value,
        "final_state": (
            result.routing.final_state.value if result.routing.final_state else ""
        ),
        "overall_success": result.overall_success,
        "validation_count": result.counters.validation_count,
        "repair_attempted": result.routing.repair_attempted,
        "content_repair_count": result.counters.content_repair_count,
        "upstream_redo_count": result.counters.upstream_redo_count,
        "retrieval_attempted": result.routing.retrieval_attempted,
        "retrieval_triggered": result.routing.retrieval_triggered,
        "retrieval_tokens_positive": result.token_usage.retrieval_tokens > 0,
        "modelica_rerender_action": "rerender_certified_modelica" in actions,
        "sysml_rebuild_action": "rebuild_canonical_sysml" in actions,
        "trace_integrity": bool(trace and trace.integrity_passed),
        "all_critical_verified": bool(critical)
        and all(
            item.status is TraceLinkStatus.VERIFIED
            for item in trace.links
            if item.requirement_id in critical
        ),
        "critical_not_implemented": bool(trace)
        and any(
            item.requirement_id in critical
            and item.status is TraceLinkStatus.NOT_IMPLEMENTED
            for item in trace.links
        ),
        "repair_stop_reason": result.repair_stop_reason.value,
        "resolved_by": result.resolved_by,
        "token_budget_status": (
            ledger.token_budget_status.value if ledger is not None else "NOT_RECORDED"
        ),
        "duration_budget_status": (
            ledger.duration_budget_status.value
            if ledger is not None
            else "NOT_RECORDED"
        ),
        "online_api_used": result.routing.online_api_used,
    }


def _evidence_from_result(result: RunResult) -> dict[str, Any]:
    trace = result.traceability
    ledger = result.budget_ledger
    return {
        "input_snapshot_sha256": (
            result.routing.events[0].input_snapshot_sha256
            if result.routing.events
            else ""
        ),
        "initial_artifact_bundle_sha256": result.routing.initial_artifact_bundle_sha256,
        "final_artifact_bundle_sha256": result.raw_metrics["artifact_bundle_sha256"],
        "routing_event_count": len(result.routing.events),
        "routing_first_event_sha256": result.routing.events[0].event_sha256,
        "routing_last_event_sha256": result.routing.events[-1].event_sha256,
        "routing_actions": [item.action.value for item in result.routing.events],
        "escalation_reasons": list(result.routing.escalation_reasons),
        "full_hard_gate_profile": result.routing.full_hard_gate_profile,
        "reviewer_rounds": result.routing.reviewer_rounds,
        "natural_language_interpretation_performed": (
            result.routing.natural_language_interpretation_performed
        ),
        "budget_usage": (
            {
                item.value: ledger.usage(item)
                for item in BudgetDimension
            }
            if ledger is not None
            else {}
        ),
        "observed_model_tokens": (
            ledger.observed_model_tokens if ledger is not None else 0
        ),
        "observed_duration_s": (
            ledger.observed_duration_s if ledger is not None else result.duration_s
        ),
        "trace_report_sha256": (
            next(
                item.evidence["report_sha256"]
                for item in result.checks
                if item.check_type == "traceability_integrity"
            )
            if trace is not None
            else ""
        ),
        "trace_link_counts": (
            {
                status.value: sum(item.status is status for item in trace.links)
                for status in TraceLinkStatus
            }
            if trace is not None
            else {}
        ),
        "trace_issues": list(trace.issues if trace is not None else ()),
    }


def _actual_from_preflight(error: Exception) -> tuple[dict[str, Any], dict[str, Any]]:
    route = error.routing
    reasons = set(route.escalation_reasons)
    actual = {
        "path": route.path.value,
        "final_state": route.final_state.value,
        "validation_count": 0,
        "full_hard_gate_profile": route.full_hard_gate_profile,
        "parameter_conflict_reason": "conflicting_parameter_values" in reasons,
        "no_certified_template_reason": "no_certified_template" in reasons,
        "online_api_used": route.online_api_used,
    }
    evidence = {
        "input_snapshot_sha256": error.input_snapshot_sha256,
        "routing_event_count": len(route.events),
        "routing_first_event_sha256": route.events[0].event_sha256,
        "routing_last_event_sha256": route.events[-1].event_sha256,
        "routing_actions": [item.action.value for item in route.events],
        "escalation_reasons": list(route.escalation_reasons),
        "initial_checks": [],
        "artifact_generation_started": False,
    }
    return actual, evidence


def _pipeline_for_case(case: dict[str, Any], workspace: Path):
    template_id = case["template_id"]
    kind = case["kind"]
    name = _model_name(case["case_id"])
    if kind == "structured_fast":
        return CertifiedTemplateRepairPipeline.from_observations(
            template_id=template_id,
            model_name=name,
            observations=_structured_observations(template_id),
            workspace_root=workspace,
        )
    if kind == "retrieval_normalization":
        observations, retrieval = _retrieval_assisted_rlc()
        return CertifiedTemplateRepairPipeline.from_observations(
            template_id=template_id,
            model_name=name,
            observations=observations,
            retrieval_result=retrieval,
            workspace_root=workspace,
        )
    return CertifiedTemplateRepairPipeline(
        template_id=template_id,
        model_name=name,
        parameters=representative_parameters(template_id),
        workspace_root=workspace,
    )


def _run_case(
    case: dict[str, Any], *, workspace: Path, config: RepairLoopConfig
) -> tuple[dict[str, Any], RunResult | None]:
    kind = case["kind"]
    if kind == "parameter_preflight_failure":
        observations = list(_structured_observations("rc_filter"))
        first = observations[0]
        observations.append(
            replace(first, value=float(first.value) * 2.0, source_text="conflicting duplicate")
        )
        try:
            CertifiedTemplateRepairPipeline.from_observations(
                template_id="rc_filter",
                model_name=_model_name(case["case_id"]),
                observations=observations,
                workspace_root=workspace,
            )
        except ParameterNormalizationError as error:
            actual, evidence = _actual_from_preflight(error)
            return {
                **case,
                "actual": actual,
                "evidence": evidence,
                "run_result_file": "",
            }, None
        raise RuntimeError("parameter conflict unexpectedly reached artifact generation")
    if kind == "template_preflight_failure":
        try:
            CertifiedTemplateRepairPipeline.from_observations(
                template_id=case["template_id"],
                model_name=_model_name(case["case_id"]),
                observations=(
                    ParameterObservation("x", 1.0, "1", "x=1"),
                ),
                workspace_root=workspace,
            )
        except AdaptivePreparationError as error:
            actual, evidence = _actual_from_preflight(error)
            return {
                **case,
                "actual": actual,
                "evidence": evidence,
                "run_result_file": "",
            }, None
        raise RuntimeError("unknown template unexpectedly reached artifact generation")

    pipeline = _pipeline_for_case(case, workspace)
    requirement, trace_input = _trace_input(
        case["template_id"],
        mark_first_not_implemented=kind == "trace_not_implemented",
    )
    sysml = pipeline.canonical_sysml
    modelica = pipeline.canonical_render.code
    if kind == "modelica_injection":
        modelica = modelica.replace(
            f"end {pipeline.context.model_name};",
            f"end {pipeline.context.model_name}",
        )
    elif kind == "sysml_injection":
        sysml = sysml[:-1]
    result = pipeline.run(
        case_id=case["case_id"],
        run_id="offline-stage4",
        initial_artifacts=ArtifactBundle(requirement, sysml, modelica),
        config=config,
        traceability_input=trace_input,
    )
    return {
        **case,
        "actual": _actual_from_result(result),
        "evidence": _evidence_from_result(result),
        "run_result_file": "",
    }, result


def run_regression(*, output_directory: Path) -> dict[str, Any]:
    if resolve_omc_path() is None:
        raise RuntimeError("OpenModelica is unavailable; Stage-4 regression cannot run")
    suite_bytes = SUITE_PATH.read_bytes()
    suite_sha256 = hashlib.sha256(suite_bytes).hexdigest()
    suite = json.loads(suite_bytes.decode("utf-8"))
    if any(
        suite.get(name) is not False
        for name in (
            "holdout_access_allowed",
            "cross_version_comparison_allowed",
            "online_api_allowed",
        )
    ):
        raise RuntimeError("Stage-4 suite does not enforce its offline boundary")
    limits = suite["repair_limits"]
    config = RepairLoopConfig(**limits)

    output_directory.mkdir(parents=True, exist_ok=False)
    runs_directory = output_directory / "runs"
    runs_directory.mkdir()
    (output_directory / "suite-snapshot.json").write_bytes(suite_bytes)

    records: list[dict[str, Any]] = []
    for index, case in enumerate(suite["cases"], start=1):
        record, result = _run_case(
            dict(case),
            workspace=output_directory / "work" / case["case_id"],
            config=config,
        )
        if result is not None:
            relative = Path("runs") / f"{index:02d}-{case['case_id']}.run-result.json"
            _write_json(output_directory / relative, result.to_dict())
            record["run_result_file"] = relative.as_posix()
        records.append(record)
        print(
            f"[{case['case_id']}] path={record['actual']['path']} "
            f"outcome={record['actual']['final_state']}",
            flush=True,
        )

    anchored_files = (
        "src/schemas.py",
        "src/pipeline.py",
        "src/routing.py",
        "src/traceability.py",
        "src/hard_gate.py",
        "configs/template_certifications.json",
        "configs/retrieval_corpus.json",
        "configs/retrieval_policy.json",
    )
    offline_guard = {
        "version_under_test": "v8",
        "online_api_used": False,
        "external_network_used": False,
        "holdout_accessed": False,
        "legacy_adapters_used": False,
        "cross_version_comparison_performed": False,
        "model_provider": "deterministic_offline",
        "cases_module_imported": False,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "openmodelica_path_recorded": bool(resolve_omc_path()),
        "implementation_sha256": {
            name: _file_sha256(WEEK9_ROOT / name) for name in anchored_files
        },
    }
    report = build_adaptive_regression_report(
        suite_snapshot=suite,
        suite_sha256=suite_sha256,
        cases=records,
        offline_guard=offline_guard,
    )
    report_path = output_directory / "adaptive-regression-report.json"
    _write_json(report_path, report)
    report_sha256 = _file_sha256(report_path)
    (output_directory / "adaptive-regression-report.sha256").write_text(
        report_sha256 + "\n", encoding="ascii"
    )
    summary = (
        "# V8 Stage 4 离线机制回归\n\n"
        f"- 结论：`{report['verdict']}`\n"
        f"- 通过：{report['summary']['pass_count']}/{report['summary']['run_case_count']}\n"
        "- 范围：仅验证 fast/escalation/trace/budget 离线机制。\n"
        "- 未运行：V4/V6 公平比较、holdout、在线模型、Judge、成本优势与泛化评估。\n"
        "- 成本阈值：`NOT_FROZEN`；本机耗时只作诊断。\n"
    )
    (output_directory / "adaptive-regression-summary.md").write_text(
        summary, encoding="utf-8"
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default=None)
    args = parser.parse_args(argv)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_directory = (
        Path(args.out_dir).resolve()
        if args.out_dir
        else WEEK9_ROOT / "results" / f"stage4-adaptive-regression-offline-{stamp}"
    )
    report = run_regression(output_directory=output_directory)
    print(f"report={output_directory / 'adaptive-regression-report.json'}")
    print(f"verdict={report['verdict']}")
    return 0 if report["verdict"] == "MECHANISM_REGRESSION_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
