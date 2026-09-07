"""Compile, simulate, and certify all five V8 template skeletons offline."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from week9.src.hard_gate import SysMLParserAdapter, run_hard_gate  # noqa: E402
from week9.src.modelica_templates import (  # noqa: E402
    TEMPLATE_REGISTRY,
    get_template_spec,
    render_template,
    representative_parameters,
)
from week9.src.modelica_tool import OpenModelicaTool  # noqa: E402
from week9.experiments.cases import ERROR_INJECTION_CASES  # noqa: E402
from week9.src.schemas import (  # noqa: E402
    CheckStatus,
    FailureKind,
    QualityCheckResult,
    utc_now_iso,
)


RELEASE_REQUIRED_CHECKS = (
    "requirement_normalization",
    "sysml_syntax",
    "certified_template_integrity",
    "template_certification_anchor",
    "execution_provenance",
    "modelica_compile",
    "parameter_unit_consistency",
    "simulation",
    "physical_behavior",
    "cross_model_consistency",
)


def certify(*, workspace_root: Path) -> dict[str, Any]:
    tool = OpenModelicaTool()
    parser = SysMLParserAdapter()
    certificates: dict[str, Any] = {}
    baselines: dict[str, tuple[Any, ...]] = {}
    for template_id in TEMPLATE_REGISTRY:
        parameters = representative_parameters(template_id)
        rendered = render_template(
            template_id,
            parameters,
            model_name=f"Certified_{template_id}",
        )
        execution = tool.execute_rendered(rendered, workspace_root=workspace_root)
        gate = run_hard_gate(
            template_id=template_id,
            parameters=parameters,
            sysml_source=_certification_sysml(template_id, parameters),
            rendered=rendered,
            execution=execution,
            sysml_parser=parser,
            recertification_mode=True,
        )
        checks = {check.check_type: check for check in gate.checks}
        certificates[template_id] = {
            "template_sha256": rendered.template_sha256,
            "rendered_sha256": rendered.rendered_sha256,
            "stimulus_type": get_template_spec(template_id).stimulus_type,
            "representative_parameters": [
                {
                    "canonical_name": item.canonical_name,
                    "value": item.value,
                    "unit": item.unit,
                    "source_text": item.source_text,
                    "status": item.status.value,
                }
                for item in parameters
            ],
            "expected_output_variables": list(
                get_template_spec(template_id).output_variables
            ),
            "checks": {
                name: _compact_check(checks[name])
                for name in (
                    "requirement_normalization",
                    "sysml_syntax",
                    "certified_template_integrity",
                    "template_certification_anchor",
                    "execution_provenance",
                    "modelica_compile",
                    "parameter_unit_consistency",
                    "simulation",
                    "physical_behavior",
                    "cross_model_consistency",
                )
            },
            "overall_success": gate.overall_success,
            "blocking_checks": list(gate.decision.blocking_checks),
        }
        baselines[template_id] = (
            parameters,
            rendered,
            execution,
            _certification_sysml(template_id, parameters),
        )
        print(
            f"[{template_id}] compile={checks['modelica_compile'].status.value} "
            f"simulation={checks['simulation'].status.value} "
            f"physics={checks['physical_behavior'].status.value} "
            f"gate={'PASS' if gate.overall_success else 'FAIL'}",
            flush=True,
        )

    successful = sum(
        1 for certificate in certificates.values() if certificate["overall_success"]
    )
    msl_versions = sorted(
        {
            str(
                certificate["checks"]["modelica_compile"]["evidence"].get(
                    "modelica_library_version", "unknown"
                )
            )
            for certificate in certificates.values()
        }
    )
    error_injections = _run_error_injections(
        tool=tool,
        parser=parser,
        workspace_root=workspace_root,
        baselines=baselines,
    )
    false_releases = sum(1 for item in error_injections if item["overall_success"])
    injection_expectations_matched = all(
        item["expectation_matched"] for item in error_injections
    )
    stage1_gate_passed = (
        successful == len(certificates)
        and false_releases == 0
        and injection_expectations_matched
    )
    return {
        "schema_version": "v8-template-certification-1",
        "certified_at": utc_now_iso(),
        "implementation_sha256": next(iter(certificates.values()))["checks"][
            "template_certification_anchor"
        ]["evidence"]["implementation_sha256"],
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "openmodelica": tool.version,
            "modelica_standard_library_versions": msl_versions,
            "sysmlpy": parser.version,
            "sysml_parser_api": "sysmlpy.antlr_parser.parse",
            "online_api_used": False,
        },
        "policy": {
            "rendering": "declared numeric slots only",
            "post_render_full_rewrite_allowed": False,
            "required_check_release_rule": "all required checks must be PASS",
            "sysml_semantic_claim": "not made; syntax + minimum structure only",
        },
        "summary": {
            "template_count": len(certificates),
            "successful_certifications": successful,
            "failed_certifications": len(certificates) - successful,
            "all_certified": successful == len(certificates),
            "correct_sample_false_rejection_count": len(certificates) - successful,
            "correct_sample_count": len(certificates),
            "known_hard_error_false_release_count": false_releases,
            "known_hard_error_count": len(error_injections),
            "error_injection_expectations_matched": injection_expectations_matched,
            "stage1_gate_passed": stage1_gate_passed,
        },
        "templates": certificates,
        "error_injections": error_injections,
    }


def build_release_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    """Promote a passing full candidate into the schema consumed at release time."""

    if candidate.get("schema_version") != "v8-template-certification-1":
        raise ValueError("unsupported certification candidate schema")
    summary = candidate.get("summary")
    if not isinstance(summary, dict) or summary.get("stage1_gate_passed") is not True:
        raise ValueError("a failing Stage 1 candidate cannot be promoted")
    environment = candidate.get("environment")
    if not isinstance(environment, dict):
        raise ValueError("certification candidate has no environment record")
    msl_versions = environment.get("modelica_standard_library_versions")
    if not isinstance(msl_versions, list) or len(msl_versions) != 1:
        raise ValueError("release requires exactly one certified MSL version")

    candidate_templates = candidate.get("templates")
    if not isinstance(candidate_templates, dict) or set(candidate_templates) != set(
        TEMPLATE_REGISTRY
    ):
        raise ValueError("candidate does not exactly cover the five Stage 1 templates")
    promoted_templates: dict[str, Any] = {}
    for template_id in TEMPLATE_REGISTRY:
        certificate = candidate_templates[template_id]
        checks = certificate.get("checks")
        if not isinstance(checks, dict) or set(checks) != set(RELEASE_REQUIRED_CHECKS):
            raise ValueError(f"{template_id} does not contain all required checks")
        for check_name in RELEASE_REQUIRED_CHECKS:
            if checks[check_name].get("status") != CheckStatus.PASS.value:
                raise ValueError(f"{template_id}.{check_name} is not PASS")
        promoted_templates[template_id] = {
            "template_sha256": certificate["template_sha256"],
            "rendered_sha256": certificate["rendered_sha256"],
            "stimulus_type": certificate["stimulus_type"],
            "representative_parameters": certificate["representative_parameters"],
            "expected_output_variables": certificate["expected_output_variables"],
            "checks": checks,
            "compile": checks["modelica_compile"]["status"],
            "simulation": checks["simulation"]["status"],
            "physical_behavior": checks["physical_behavior"]["status"],
            "full_gate": "PASS" if certificate["overall_success"] else "FAIL",
            "overall_success": bool(certificate["overall_success"]),
            "blocking_checks": list(certificate["blocking_checks"]),
            "csv_sha256": checks["simulation"]["evidence"]["csv_sha256"],
            "physical_evidence": checks["physical_behavior"]["evidence"],
        }

    return {
        "schema_version": "v8-template-certification-summary-1",
        "certified_at": candidate["certified_at"],
        "implementation_sha256": candidate["implementation_sha256"],
        "environment": {
            "python": environment["python"],
            "platform": environment["platform"],
            "openmodelica": environment["openmodelica"],
            "modelica_standard_library": msl_versions[0],
            "sysmlpy": environment["sysmlpy"],
            "sysml_parser_api": environment["sysml_parser_api"],
            "online_api_used": environment["online_api_used"],
        },
        "policy": candidate["policy"],
        "summary": summary,
        "required_checks": list(RELEASE_REQUIRED_CHECKS),
        "error_injections": candidate["error_injections"],
        "templates": promoted_templates,
    }


def _compact_check(check) -> dict[str, Any]:
    evidence_keys = {
        "sysml_syntax": ("parser_api", "mode", "self_test", "semantic_claim"),
        "certified_template_integrity": (
            "template_id",
            "template_sha256",
            "rendered_sha256",
            "policy",
        ),
        "template_certification_anchor": (
            "mode",
            "not_release_authorization",
            "template_sha256",
            "implementation_sha256",
        ),
        "execution_provenance": (
            "template_id",
            "model_name",
            "source_sha256",
            "csv_sha256",
            "simulation_plan",
        ),
        "modelica_compile": (
            "returncode",
            "source_sha256",
            "executable_present",
            "executable_sha256",
            "modelica_library_version",
        ),
        "parameter_unit_consistency": (
            "canonical_values",
            "canonical_units",
            "relation",
        ),
        "simulation": (
            "returncode",
            "csv_present",
            "csv_sha256",
            "csv_size",
            "row_count",
            "raw_row_count",
            "duplicate_rows_collapsed",
            "validated_variables",
        ),
        "physical_behavior": tuple(check.evidence),
        "cross_model_consistency": (
            "scope",
            "matched_parameters",
            "matched_components",
            "matched_connections",
            "semantic_claim",
        ),
        "requirement_normalization": ("canonical_parameters",),
    }
    selected = evidence_keys.get(check.check_type, ())
    return {
        "status": check.status.value,
        "required": check.required,
        "issues": list(check.issues),
        "failure_kind": check.failure_kind.value,
        "failure_category": check.failure_category,
        "tool_name": check.tool_name,
        "tool_version": check.tool_version,
        "duration_s": check.duration_s,
        "evidence": {
            key: check.evidence[key] for key in selected if key in check.evidence
        },
    }


def _certification_sysml(template_id, parameters) -> str:
    spec = get_template_spec(template_id)
    attributes = " ".join(
        f"attribute {item.canonical_name} : Real = {item.value};"
        for item in parameters
    )
    parts = " ".join(f"part {name};" for name in spec.sysml_components)
    connections = " ".join(
        f"connect {left} to {right};" for left, right in spec.sysml_connections
    )
    return (
        f"package Certification_{template_id} {{ part def System {{ "
        f"{attributes} {parts} {connections} }} part system : System; }}"
    )


def _run_error_injections(*, tool, parser, workspace_root, baselines):
    rc_parameters, rc_rendered, rc_execution, rc_sysml = baselines["rc_filter"]
    thermal_parameters, thermal_rendered, _, thermal_sysml = baselines[
        "thermal_single_room"
    ]

    actual_executions = {
        "modelica_syntax_error": tool.execute_source(
            rc_rendered.code.replace(
                f"end {rc_rendered.model_name};", f"end {rc_rendered.model_name}"
            ),
            model_name=rc_rendered.model_name,
            template_spec=get_template_spec("rc_filter"),
            workspace_root=workspace_root,
        ),
        "wrong_component_path": tool.execute_source(
            thermal_rendered.code.replace(
                "HeatTransfer.Sources.FixedTemperature",
                "HeatTransfer.Components.FixedTemperature",
            ),
            model_name=thermal_rendered.model_name,
            template_spec=get_template_spec("thermal_single_room"),
            workspace_root=workspace_root,
        ),
        "port_mismatch": tool.execute_source(
            rc_rendered.code.replace(
                "connect(source.p, resistor.p);", "connect(source.p, resistor.R);"
            ),
            model_name=rc_rendered.model_name,
            template_spec=get_template_spec("rc_filter"),
            workspace_root=workspace_root,
        ),
    }

    missing_csv_check = QualityCheckResult(
        check_type="simulation",
        status=CheckStatus.UNVERIFIED,
        required=True,
        issues=["injected missing simulation CSV"],
        evidence={"reason": "simulation_csv_missing"},
        tool_name="V8 certification fault injector",
        tool_version="stage1",
        failure_kind=FailureKind.CONTENT,
        failure_category="simulation_csv_missing",
    )
    file_lock_compile = QualityCheckResult(
        check_type="modelica_compile",
        status=CheckStatus.ERROR,
        required=True,
        issues=["injected WinError 32 file lock"],
        evidence={"reason": "file_lock", "source_sha256": rc_execution.source_sha256},
        tool_name="V8 certification fault injector",
        tool_version="stage1",
        failure_kind=FailureKind.INFRASTRUCTURE,
        failure_category="file_lock",
    )
    file_lock_simulation = QualityCheckResult(
        check_type="simulation",
        status=CheckStatus.ERROR,
        required=True,
        issues=["simulation blocked by injected file lock"],
        evidence={"reason": "compile_prerequisite_failed"},
        tool_name="V8 certification fault injector",
        tool_version="stage1",
        failure_kind=FailureKind.INFRASTRUCTURE,
        failure_category="compile_prerequisite_failed",
    )

    def explode(*_args):
        raise RuntimeError("injected validator exception")

    invocations = {
        "missing_sysml": dict(
            template_id="rc_filter",
            parameters=rc_parameters,
            sysml_source=None,
            rendered=rc_rendered,
            execution=rc_execution,
        ),
        "missing_modelica": dict(
            template_id="rc_filter",
            parameters=rc_parameters,
            sysml_source=rc_sysml,
            rendered=rc_rendered,
            execution=None,
        ),
        "missing_csv": dict(
            template_id="rc_filter",
            parameters=rc_parameters,
            sysml_source=rc_sysml,
            rendered=rc_rendered,
            execution=replace(
                rc_execution,
                csv_path="",
                csv_sha256="",
                csv_data=None,
                simulation_check=missing_csv_check,
            ),
        ),
        "modelica_syntax_error": dict(
            template_id="rc_filter",
            parameters=rc_parameters,
            sysml_source=rc_sysml,
            rendered=rc_rendered,
            execution=actual_executions["modelica_syntax_error"],
        ),
        "wrong_component_path": dict(
            template_id="thermal_single_room",
            parameters=thermal_parameters,
            sysml_source=thermal_sysml,
            rendered=thermal_rendered,
            execution=actual_executions["wrong_component_path"],
        ),
        "port_mismatch": dict(
            template_id="rc_filter",
            parameters=rc_parameters,
            sysml_source=rc_sysml,
            rendered=rc_rendered,
            execution=actual_executions["port_mismatch"],
        ),
        "validator_exception": dict(
            template_id="rc_filter",
            parameters=rc_parameters,
            sysml_source=rc_sysml,
            rendered=rc_rendered,
            execution=rc_execution,
            physical_validator=explode,
        ),
        "file_lock": dict(
            template_id="rc_filter",
            parameters=rc_parameters,
            sysml_source=rc_sysml,
            rendered=rc_rendered,
            execution=replace(
                rc_execution,
                executable_path="",
                csv_path="",
                csv_sha256="",
                csv_data=None,
                compile_check=file_lock_compile,
                simulation_check=file_lock_simulation,
            ),
        ),
    }
    results = []
    for injection_id, specification in ERROR_INJECTION_CASES.items():
        gate = run_hard_gate(
            **invocations[injection_id],
            sysml_parser=parser,
            recertification_mode=True,
        )
        target = next(
            check for check in gate.checks if check.check_type == specification.target_check
        )
        results.append(
            {
                "injection_id": injection_id,
                "execution_mode": (
                    "real_openmodelica"
                    if injection_id in actual_executions
                    else "deterministic_fault_injection"
                ),
                "target_check": specification.target_check,
                "expected_status": specification.expected_status.value,
                "actual_status": target.status.value,
                "expected_failure_kind": specification.expected_failure_kind,
                "actual_failure_kind": target.failure_kind.value,
                "expectation_matched": (
                    target.status is specification.expected_status
                    and target.failure_kind.value == specification.expected_failure_kind
                ),
                "overall_success": gate.overall_success,
                "blocking_checks": list(gate.decision.blocking_checks),
            }
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", help="manifest JSON path")
    parser.add_argument(
        "--release-summary-out",
        help="promote a passing candidate to the release-consumer summary schema",
    )
    parser.add_argument("--workspace-root", help="root for unique OMC work directories")
    args = parser.parse_args(argv)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    default_root = PROJECT_ROOT / "week9" / "results" / f"template-certification-{run_id}"
    workspace_root = Path(args.workspace_root).resolve() if args.workspace_root else default_root
    output_path = (
        Path(args.out).resolve() if args.out else workspace_root / "certification.json"
    )
    manifest = certify(workspace_root=workspace_root / "omc-runs")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(f"certification_manifest={output_path}", flush=True)
    if args.release_summary_out:
        release_summary_path = Path(args.release_summary_out).resolve()
        release_summary = build_release_summary(manifest)
        release_summary_path.parent.mkdir(parents=True, exist_ok=True)
        release_summary_path.write_text(
            json.dumps(
                release_summary, ensure_ascii=False, indent=2, allow_nan=False
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"release_summary={release_summary_path}", flush=True)
    return 0 if manifest["summary"]["stage1_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
