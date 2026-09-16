"""V10 validated-specification adapter for the certified V8 pipeline.

This module is deliberately below the approval boundary.  It accepts only an
already constructed specification, validates it again, and projects explicit
structured observations into the unchanged certified pipeline.  It performs
no natural-language extraction and no network access.
"""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from week9.src.hard_gate import REQUIRED_HARD_CHECK_TYPES
from week9.src.pipeline import CertifiedTemplateRepairPipeline
from week9.src.repair_loop import ArtifactBundle, RepairLoopConfig
from week9.src.schemas import (
    CheckStatus,
    RunResult,
    sha256_text,
    traceability_report_sha256,
)
from week9.src.traceability import (
    EXPLICIT_BINDING_BASIS,
    TOPOLOGY_TARGET_ID,
    RequirementItem,
    RequirementTargetBinding,
    TraceabilityInput,
    parameter_target_id,
)
from week10.src.trace_graph import TraceGraph
from week11.src.spec import observations_for_spec, spec_digest, validate_spec


GENERATION_RESULT_SCHEMA = "v10-generation-result-1"
GENERATION_BINDING_SCHEMA = "v10-generation-binding-1"
MANIFEST_SCHEMA = "v10-generation-manifest-1"
TRACE_EVIDENCE_SCHEMA = "v10-trace-evidence-1"

_WEEK11_ROOT = Path(__file__).resolve().parents[1]
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_OUTPUT_NAMES = (
    "spec.json",
    "requirement.txt",
    "model.sysml",
    "model.mo",
    "run-result.json",
    "generation-binding.json",
    "trace-evidence.json",
    "manifest.json",
)


class SpecExecutionError(ValueError):
    """Raised when execution-time specification validation fails closed."""

    def __init__(self, issues: Sequence[Mapping[str, Any] | str]) -> None:
        self.issues = tuple(issues)
        super().__init__("specification is not ready for generation")


def execute_spec(
    spec: Mapping[str, Any],
    workspace: Path,
    project_id: str,
    run_id: str,
) -> dict[str, Any]:
    """Revalidate and execute one V10 specification through the old hard gate.

    ``execute_spec`` is an internal integration function.  It neither checks
    nor records product approval and must not be registered directly as an MCP
    tool.  The execution service owns that trust boundary.
    """

    selected_workspace = _validate_workspace(workspace)
    _validate_identifier(project_id, "project_id")
    _validate_identifier(run_id, "run_id")

    validation = validate_spec(spec)
    if not isinstance(validation, Mapping):
        raise TypeError("validate_spec returned an invalid result")
    if validation.get("ready") is not True or not isinstance(
        validation.get("spec"), Mapping
    ):
        raw_issues = validation.get("issues", ())
        issues = raw_issues if isinstance(raw_issues, Sequence) else ()
        raise SpecExecutionError(issues)
    validated_spec = _json_clone(validation["spec"])
    digest = spec_digest(validated_spec)

    requirement_text, trace_input, trace_bindings = _build_trace_input(validated_spec)
    observations = tuple(observations_for_spec(validated_spec))
    if not observations:
        raise ValueError("validated specification produced no observations")

    model_name = "V10Model_" + hashlib.sha256(
        f"{project_id}\0{run_id}\0{digest}".encode("utf-8")
    ).hexdigest()[:20]
    pipeline = CertifiedTemplateRepairPipeline.from_observations(
        template_id=str(validated_spec["template_id"]),
        model_name=model_name,
        observations=observations,
        workspace_root=selected_workspace,
    )
    _assert_parameter_equivalence(validated_spec, pipeline.context.parameters)
    _prepare_workspace(selected_workspace)

    initial_artifacts = ArtifactBundle(
        requirement=requirement_text,
        sysml=pipeline.canonical_sysml,
        modelica=pipeline.canonical_render.code,
    )
    no_repair_config = RepairLoopConfig(
        max_repairs=0,
        max_content_repairs=0,
        max_upstream_redos=0,
        max_infrastructure_retries=0,
    )
    run_result = pipeline.run(
        case_id=project_id,
        run_id=run_id,
        initial_artifacts=initial_artifacts,
        config=no_repair_config,
        traceability_input=trace_input,
    )
    if not isinstance(run_result, RunResult):
        raise TypeError("certified pipeline returned an invalid RunResult")
    if run_result.version != "v8":
        raise ValueError("certified pipeline changed its frozen RunResult version")
    if run_result.case_id != project_id or run_result.run_id != run_id:
        raise ValueError("certified pipeline returned a result for another execution")

    run_dict = run_result.to_dict()
    statuses, hard_gate_passed = _hard_gate_statuses(run_result)
    trace_status = _trace_status(run_result)
    trace_evidence, evidence_status = _build_trace_evidence(run_result, digest)
    binding = _build_generation_binding(
        validated_spec=validated_spec,
        digest=digest,
        observations=observations,
        pipeline=pipeline,
        run_result=run_result,
        statuses=statuses,
        hard_gate_passed=hard_gate_passed,
        trace_status=trace_status,
        trace_bindings=trace_bindings,
        evidence_status=evidence_status,
    )

    written = _write_outputs(
        selected_workspace,
        validated_spec=validated_spec,
        requirement_text=requirement_text,
        run_result=run_result,
        run_dict=run_dict,
        binding=binding,
        trace_evidence=trace_evidence,
    )
    succeeded = bool(run_result.overall_success and hard_gate_passed and evidence_status["complete"])
    return {
        "schema_version": GENERATION_RESULT_SCHEMA,
        "project_id": project_id,
        "run_id": run_id,
        "spec_digest": digest,
        "status": "succeeded" if succeeded else "failed",
        "run_result": run_dict,
        "hard_gate": {
            "passed": hard_gate_passed,
            "required_check_statuses": statuses,
            "traceability_status": trace_status,
            "overall_success": bool(run_result.overall_success),
        },
        "generation_binding": binding,
        "evidence": evidence_status,
        "files": written,
    }


def _validate_workspace(workspace: Path) -> Path:
    if not isinstance(workspace, Path):
        raise TypeError("workspace must be a pathlib.Path")
    selected = workspace.resolve()
    try:
        relative = selected.relative_to(_WEEK11_ROOT.resolve())
    except ValueError as exc:
        raise ValueError("workspace must be inside week11") from exc
    if not relative.parts:
        raise ValueError("workspace must be a child directory of week11")
    return selected


def _validate_identifier(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or not _IDENTIFIER.fullmatch(value)
        or ".." in value
    ):
        raise ValueError(f"unsafe {label}")


def _prepare_workspace(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    collisions = [name for name in _OUTPUT_NAMES if (workspace / name).exists()]
    if collisions:
        raise FileExistsError(
            "generation output already exists: " + ", ".join(collisions)
        )


def _build_trace_input(
    validated_spec: Mapping[str, Any],
) -> tuple[str, TraceabilityInput, list[dict[str, Any]]]:
    sources = validated_spec.get("sources")
    normalized = validated_spec.get("normalized_parameters")
    if not isinstance(sources, list) or not sources:
        raise ValueError("validated specification has no sources")
    if not isinstance(normalized, list) or not normalized:
        raise ValueError("validated specification has no normalized parameters")

    parts = [
        "[v10 validated structured specification]",
        f"template_id={validated_spec['template_id']}",
    ]
    source_offsets: dict[str, tuple[int, str]] = {}
    for source in sources:
        if not isinstance(source, Mapping):
            raise ValueError("validated specification contains an invalid source")
        source_id = source.get("id")
        text = source.get("text")
        if not isinstance(source_id, str) or not isinstance(text, str) or not text:
            raise ValueError("validated specification contains an invalid source")
        heading = f"[source:{source_id}]"
        prefix = "\n".join(parts) + "\n" + heading + "\n"
        source_offsets[source_id] = (len(prefix), text)
        parts.extend((heading, text))
    requirement_text = "\n".join(parts)

    requirements: list[RequirementItem] = []
    bindings: list[RequirementTargetBinding] = []
    binding_records: list[dict[str, Any]] = []
    topology_text = f"template_id={validated_spec['template_id']}"
    topology_start = requirement_text.index(topology_text)
    requirements.append(
        RequirementItem(
            requirement_id="REQ-V10-TOPOLOGY",
            text=topology_text,
            source_start=topology_start,
            source_end=topology_start + len(topology_text),
            critical=True,
        )
    )
    bindings.append(
        RequirementTargetBinding(
            requirement_id="REQ-V10-TOPOLOGY",
            target_id=TOPOLOGY_TARGET_ID,
            basis=EXPLICIT_BINDING_BASIS,
        )
    )
    binding_records.append(
        {
            "requirement_id": "REQ-V10-TOPOLOGY",
            "target_id": TOPOLOGY_TARGET_ID,
            "source_kind": "validated_spec_template_selection",
            "source_id": None,
            "source_text": topology_text,
        }
    )

    original_parameters = validated_spec.get("parameters", [])
    derivations = validated_spec.get("derivations", [])
    for index, parameter in enumerate(normalized, start=1):
        if not isinstance(parameter, Mapping):
            raise ValueError("validated specification contains an invalid normalized parameter")
        name = parameter.get("name")
        source_text = parameter.get("source_text")
        if not isinstance(name, str) or not isinstance(source_text, str) or not source_text:
            raise ValueError("normalized parameter source evidence is incomplete")
        source_id, source_kind = _parameter_source(
            name, source_text, original_parameters, derivations, sources
        )
        source_start, source_body = source_offsets[source_id]
        local_start = source_body.find(source_text)
        if local_start < 0:
            raise ValueError(f"parameter {name!r} source text is absent from source {source_id!r}")
        absolute_start = source_start + local_start
        requirement_id = f"REQ-V10-PARAM-{index:03d}"
        target_id = parameter_target_id(name)
        requirements.append(
            RequirementItem(
                requirement_id=requirement_id,
                text=source_text,
                source_start=absolute_start,
                source_end=absolute_start + len(source_text),
                critical=True,
            )
        )
        bindings.append(
            RequirementTargetBinding(
                requirement_id=requirement_id,
                target_id=target_id,
                basis=EXPLICIT_BINDING_BASIS,
            )
        )
        binding_records.append(
            {
                "requirement_id": requirement_id,
                "target_id": target_id,
                "source_kind": source_kind,
                "source_id": source_id,
                "source_text": source_text,
            }
        )

    trace_input = TraceabilityInput(
        requirement_artifact_sha256=sha256_text(requirement_text),
        requirements=tuple(requirements),
        bindings=tuple(bindings),
    )
    return requirement_text, trace_input, binding_records


def _parameter_source(
    name: str,
    source_text: str,
    parameters: Any,
    derivations: Any,
    sources: list[Any],
) -> tuple[str, str]:
    for collection, kind, key in (
        (parameters, "direct_or_user_parameter", "name"),
        (derivations, "deterministically_derived_parameter", "target"),
    ):
        if isinstance(collection, list):
            for item in collection:
                if (
                    isinstance(item, Mapping)
                    and item.get(key) == name
                    and item.get("source_text") == source_text
                    and isinstance(item.get("source_id"), str)
                ):
                    return str(item["source_id"]), kind
    matching_sources = [
        str(item["id"])
        for item in sources
        if isinstance(item, Mapping)
        and isinstance(item.get("id"), str)
        and isinstance(item.get("text"), str)
        and source_text in item["text"]
    ]
    if len(matching_sources) == 1:
        return matching_sources[0], "validated_parameter_source"
    raise ValueError(f"parameter {name!r} does not have an unambiguous source")


def _assert_parameter_equivalence(
    validated_spec: Mapping[str, Any], parameters: Sequence[Any]
) -> None:
    expected_items = validated_spec.get("normalized_parameters")
    if not isinstance(expected_items, list):
        raise ValueError("validated specification has no normalized parameter contract")
    expected = {str(item["name"]): item for item in expected_items}
    actual = {item.canonical_name: item for item in parameters}
    if set(expected) != set(actual):
        raise ValueError("old pipeline normalized a different parameter set")
    for name, item in expected.items():
        actual_item = actual[name]
        expected_value = item.get("value")
        if (
            isinstance(expected_value, bool)
            or not isinstance(expected_value, (int, float))
            or not math.isclose(
                float(actual_item.value),
                float(expected_value),
                rel_tol=1e-12,
                abs_tol=1e-15,
            )
            or actual_item.unit != item.get("unit")
            or actual_item.source_text != item.get("source_text")
        ):
            raise ValueError(f"old pipeline changed normalized parameter {name!r}")


def _hard_gate_statuses(run_result: RunResult) -> tuple[dict[str, str], bool]:
    indexed = {check.check_type: check for check in run_result.checks}
    statuses = {
        name: indexed[name].status.value if name in indexed else "MISSING"
        for name in REQUIRED_HARD_CHECK_TYPES
    }
    passed = all(
        name in indexed
        and indexed[name].required
        and indexed[name].status is CheckStatus.PASS
        for name in REQUIRED_HARD_CHECK_TYPES
    )
    return statuses, passed


def _trace_status(run_result: RunResult) -> str:
    check = next(
        (item for item in run_result.checks if item.check_type == "traceability_integrity"),
        None,
    )
    return check.status.value if check is not None else "MISSING"


def _build_trace_evidence(
    run_result: RunResult, digest: str
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    try:
        graph = TraceGraph()
        added = graph.add_run(run_result)
        if added != run_result.run_id:
            raise ValueError("TraceGraph returned another run id")
        evidence = {
            "schema_version": TRACE_EVIDENCE_SCHEMA,
            "spec_digest": digest,
            "run_id": run_result.run_id,
            "scope": "V10 adapter evidence over the frozen V8 RunResult trace",
            "trace_graph": graph.to_dict(),
        }
        return evidence, {
            "complete": True,
            "trace_indexed": True,
            "reason": "",
            "file": "trace-evidence.json",
        }
    except (TypeError, ValueError, OverflowError) as exc:
        return None, {
            "complete": False,
            "trace_indexed": False,
            "reason": f"{type(exc).__name__}: {exc}"[:1000],
            "file": None,
        }


def _build_generation_binding(
    *,
    validated_spec: Mapping[str, Any],
    digest: str,
    observations: Sequence[Any],
    pipeline: CertifiedTemplateRepairPipeline,
    run_result: RunResult,
    statuses: Mapping[str, str],
    hard_gate_passed: bool,
    trace_status: str,
    trace_bindings: list[dict[str, Any]],
    evidence_status: Mapping[str, Any],
) -> dict[str, Any]:
    anchor_status = statuses.get("template_certification_anchor", "MISSING")
    run_payload_sha256 = hashlib.sha256(
        json.dumps(
            run_result.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": GENERATION_BINDING_SCHEMA,
        "spec_schema_version": validated_spec.get("schema_version"),
        "spec_digest": digest,
        "spec_revalidated_at_execution": True,
        "template_id": validated_spec.get("template_id"),
        "project_id": run_result.case_id,
        "run_id": run_result.run_id,
        "pipeline_entry": "CertifiedTemplateRepairPipeline.from_observations",
        "pipeline_input_kind": "structured_observations",
        "pipeline_input_snapshot_sha256": pipeline.input_snapshot_sha256,
        "parameter_equivalence_checked": True,
        "observations": [asdict(item) for item in observations],
        "normalized_parameters": [
            {
                "name": item.canonical_name,
                "value": item.value,
                "unit": item.unit,
                "source_text": item.source_text,
            }
            for item in pipeline.context.parameters
        ],
        "trace_input_sha256": (
            run_result.traceability.trace_input_sha256
            if run_result.traceability is not None
            else ""
        ),
        "traceability_report_sha256": (
            traceability_report_sha256(run_result.traceability)
            if run_result.traceability is not None
            else ""
        ),
        "trace_bindings": trace_bindings,
        "derivations": _json_clone(validated_spec.get("derivations", [])),
        "stimulus": _json_clone(validated_spec.get("stimulus")),
        "natural_language_parser_used": False,
        "online_api_used": False,
        "max_repairs": 0,
        "repair_count": len(run_result.repairs),
        "run_result_version": run_result.version,
        "run_result_payload_sha256": run_payload_sha256,
        "artifact_sha256": {
            name: artifact.sha256
            for name, artifact in sorted(run_result.artifacts.items())
            if name in {"requirement", "sysml", "modelica"}
        },
        "hard_gate_passed": hard_gate_passed,
        "required_check_statuses": dict(statuses),
        "traceability_status": trace_status,
        "trace_evidence_complete": bool(evidence_status.get("complete")),
        "certification": {
            "old_template_anchor_checked_by_original_hard_gate": anchor_status
            != "MISSING",
            "template_certification_anchor_status": anchor_status,
            "v10_spec_adapter_separately_bound": True,
            "old_certification_covers_v10_adapter": False,
        },
    }


def _write_outputs(
    workspace: Path,
    *,
    validated_spec: Mapping[str, Any],
    requirement_text: str,
    run_result: RunResult,
    run_dict: Mapping[str, Any],
    binding: Mapping[str, Any],
    trace_evidence: Mapping[str, Any] | None,
) -> dict[str, str]:
    contents: dict[str, bytes] = {
        "spec.json": _json_bytes(validated_spec),
        "requirement.txt": requirement_text.encode("utf-8"),
        "model.sysml": run_result.artifacts["sysml"].content.encode("utf-8"),
        "model.mo": run_result.artifacts["modelica"].content.encode("utf-8"),
        "run-result.json": _json_bytes(run_dict),
        "generation-binding.json": _json_bytes(binding),
    }
    if trace_evidence is not None:
        contents["trace-evidence.json"] = _json_bytes(trace_evidence)
    for name, raw in contents.items():
        with (workspace / name).open("xb") as handle:
            handle.write(raw)
    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "files": {
            name: {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
            for name, raw in sorted(contents.items())
        },
    }
    manifest_raw = _json_bytes(manifest)
    with (workspace / "manifest.json").open("xb") as handle:
        handle.write(manifest_raw)
    return {
        name: str((workspace / name).relative_to(_WEEK11_ROOT)).replace("\\", "/")
        for name in (*contents.keys(), "manifest.json")
    }


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


__all__ = [
    "GENERATION_BINDING_SCHEMA",
    "GENERATION_RESULT_SCHEMA",
    "SpecExecutionError",
    "execute_spec",
]
