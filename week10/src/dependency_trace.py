"""Explicit, evidence-bounded arithmetic dependency tracing for published runs.

This module intentionally recognizes only the recorded RC capacitance derivation.
It does not infer physical causality from model artifacts or checks.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from typing import Any, Iterable, Mapping

from week9.src.schemas import RunResult
from week10.src.trace_graph import TraceGraph


DEPENDENCY_SCHEMA_VERSION = "v9-explicit-arithmetic-dependency-1"
DEPENDENCY_SCOPE = (
    "explicit_recorded_arithmetic_dependencies_and_explicit_structural_relationships_only; "
    "does_not_establish_arbitrary_physical_causality_or_minimal_system_reruns"
)
_FORMULA = "C=1/(2*pi*R*fc)"
_INPUTS = ("resistance", "cutoff_frequency")
_TARGET = "capacitance"
_LOCAL_PROVIDER = "v8_deterministic_local_grammar"
_PHRASES = {"电容根据截止频率计算", "计算所需电容"}
_GENERATION_SCHEMA_VERSION = "v8-natural-language-generation-1"


def _canonical_sha256(value: Any) -> str:
    try:
        raw = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("generation evidence is not canonical JSON data") from exc
    return hashlib.sha256(raw).hexdigest()


def _id(run_id: str, generation_sha256: str, source: str, target: str) -> str:
    return "dependency:" + _canonical_sha256(
        [run_id, generation_sha256, source, target, "input_to_derived_parameter"]
    )


def _finite_positive(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite numeric value")
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{label} must be a positive finite value")
    return number


class DependencyTraceGraph:
    """A detached ``TraceGraph`` plus separately stored, validated dependency edges."""

    def __init__(self, runs: Iterable[RunResult]) -> None:
        self.graph = TraceGraph()
        self._runs: dict[str, RunResult] = {}
        for run in runs:
            if not isinstance(run, RunResult):
                raise ValueError("invalid RunResult snapshot")
            snapshot = RunResult.from_dict(run.to_dict())
            self.graph.add_run(snapshot)
            self._runs[snapshot.run_id] = snapshot
        graph_data = self.graph.to_dict()
        self._target_nodes: dict[tuple[str, str], str] = {
            (node["run_id"], node["target_id"]): node["id"]
            for node in graph_data["nodes"]
            if node.get("type") == "target"
        }
        self._run_sha256 = {
            item["run_id"]: item["run_snapshot_sha256"] for item in graph_data["runs"]
        }
        self._dependency_edges: list[dict[str, Any]] = []
        self._derivation_status: dict[str, str] = {}
        for run_id in sorted(self._runs):
            self._record_run_derivations(self._runs[run_id])

    def to_dict(self) -> dict[str, Any]:
        """Return original trace graph data and detached arithmetic edges."""
        result = self.graph.to_dict()
        result.update(
            {
                "dependency_schema_version": DEPENDENCY_SCHEMA_VERSION,
                "dependency_scope": DEPENDENCY_SCOPE,
                "derivation_status": [
                    {"run_id": run_id, "status": self._derivation_status[run_id]}
                    for run_id in sorted(self._derivation_status)
                ],
                "dependency_edges": deepcopy(sorted(self._dependency_edges, key=lambda edge: edge["id"])),
            }
        )
        return result

    def impact(self, run_id: str, target_id: str) -> dict[str, Any]:
        """Follow only recorded forward arithmetic edges, then expose trace evidence."""
        if run_id not in self._runs:
            raise ValueError(f"unknown run_id: {run_id}")
        if (run_id, target_id) not in self._target_nodes:
            raise ValueError(f"unknown target_id for run {run_id}: {target_id}")
        adjacency: dict[str, list[dict[str, Any]]] = {}
        for edge in self._dependency_edges:
            if edge["run_id"] == run_id:
                adjacency.setdefault(edge["source_target_id"], []).append(edge)
        for edges in adjacency.values():
            edges.sort(key=lambda item: item["id"])

        paths: list[dict[str, Any]] = []
        affected: set[str] = set()
        stack: list[tuple[str, list[str], list[str]]] = [(target_id, [target_id], [])]
        while stack:
            current, nodes, edge_ids = stack.pop()
            if current in nodes[:-1]:
                continue
            affected.add(current)
            outgoing = adjacency.get(current, [])
            if not outgoing:
                paths.append({"target_ids": nodes, "edge_ids": edge_ids})
                continue
            for edge in reversed(outgoing):
                next_target = edge["target_target_id"]
                if next_target in nodes:
                    continue
                stack.append((next_target, nodes + [next_target], edge_ids + [edge["id"]]))

        ordered_targets = [target_id] + sorted(affected - {target_id})
        return {
            "run_id": run_id,
            "queried_target_id": target_id,
            "affected_target_ids": ordered_targets,
            "dependency_paths": sorted(paths, key=lambda item: (item["target_ids"], item["edge_ids"])),
            "target_impacts": [self.graph.impact(run_id, item) for item in ordered_targets],
            "rerun_required_checks": self._required_checks(self._runs[run_id]),
            "scope": DEPENDENCY_SCOPE,
        }

    @staticmethod
    def _required_checks(run: RunResult) -> list[dict[str, Any]]:
        return sorted(
            [
                {"check_type": check.check_type, "status": getattr(check.status, "value", check.status), "required": check.required}
                for check in run.checks if check.required
            ], key=lambda item: item["check_type"]
        )

    def _record_run_derivations(self, run: RunResult) -> None:
        evidence = run.raw_metrics.get("natural_language_generation")
        if evidence is None:
            self._derivation_status[run.run_id] = "no_recorded_derivations"
            return
        if not isinstance(evidence, Mapping):
            raise ValueError("natural_language_generation must be an object")
        recorded_hash = run.raw_metrics.get("natural_language_generation_sha256")
        if not isinstance(recorded_hash, str) or recorded_hash != _canonical_sha256(evidence):
            raise ValueError("natural_language_generation_sha256 does not match canonical evidence")
        if evidence.get("schema_version") != _GENERATION_SCHEMA_VERSION:
            raise ValueError("unsupported natural language generation schema")
        if evidence.get("requirement_sha256") != hashlib.sha256(run.requirement_text.encode("utf-8")).hexdigest():
            raise ValueError("generation requirement_sha256 does not match requirement_text")
        if evidence.get("ready") is not True:
            raise ValueError("generation evidence is not ready")
        if run.traceability is None or evidence.get("template_id") != run.traceability.template_id:
            raise ValueError("generation template_id does not match traceability")
        if evidence.get("provider") != _LOCAL_PROVIDER or evidence.get("online_api_used") is not False:
            raise ValueError("generation evidence must record the offline local provider")
        api_call_count = evidence.get("api_call_count")
        if isinstance(api_call_count, bool) or not isinstance(api_call_count, int) or api_call_count != 0:
            raise ValueError("generation evidence must record zero offline API calls")
        derivations = evidence.get("derivations")
        if not isinstance(derivations, list):
            raise ValueError("generation derivations must be a list")
        if not derivations:
            self._derivation_status[run.run_id] = "no_recorded_derivations"
            return
        if evidence.get("template_id") != "rc_filter":
            raise ValueError("recorded derivation template is not supported")
        if len(derivations) != 1:
            raise ValueError("only one recorded RC capacitance derivation is supported")
        derivation = derivations[0]
        if not isinstance(derivation, Mapping):
            raise ValueError("invalid recorded derivation")
        self._validate_and_add_rc_derivation(run, evidence, recorded_hash, derivation)
        self._derivation_status[run.run_id] = "validated_recorded_derivations"

    def _validate_and_add_rc_derivation(self, run: RunResult, evidence: Mapping[str, Any], evidence_hash: str, derivation: Mapping[str, Any]) -> None:
        if derivation.get("target_name") != _TARGET or derivation.get("formula") != _FORMULA:
            raise ValueError("unsupported recorded derivation formula or target")
        inputs = derivation.get("input_names")
        if not isinstance(inputs, list) or tuple(inputs) != _INPUTS or len(set(inputs)) != len(inputs):
            raise ValueError("recorded derivation inputs must be resistance and cutoff_frequency")
        start, end = derivation.get("source_start"), derivation.get("source_end")
        if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int):
            raise ValueError("recorded derivation source span must use integer offsets")
        if start < 0 or end < start or end > len(run.requirement_text):
            raise ValueError("recorded derivation source span is outside requirement_text")
        source_text = run.requirement_text[start:end]
        if source_text not in _PHRASES:
            raise ValueError("recorded derivation source span is not an exact formula phrase")
        by_name: dict[str, Any] = {}
        for parameter in run.parameters:
            name = parameter.canonical_name
            if name in by_name:
                raise ValueError("normalized parameter names must be unique")
            by_name[name] = parameter
        required_names = set(_INPUTS) | {_TARGET}
        if not required_names.issubset(by_name):
            raise ValueError("recorded derivation parameters are missing")
        resistance, frequency = by_name["resistance"], by_name["cutoff_frequency"]
        if resistance.unit != "Ohm" or frequency.unit != "Hz":
            raise ValueError("recorded derivation inputs must use normalized Ohm and Hz")
        r = _finite_positive(resistance.value, "resistance")
        fc = _finite_positive(frequency.value, "cutoff_frequency")
        expected = 1.0 / (2.0 * math.pi * r * fc)
        if derivation.get("unit") != "F" or _finite_positive(derivation.get("value"), "derivation value") != expected:
            raise ValueError("recorded RC derivation value or unit does not match formula")
        capacitance = by_name[_TARGET]
        if capacitance.unit != "F" or _finite_positive(capacitance.value, "capacitance") != expected:
            raise ValueError("normalized capacitance does not match recorded RC derivation")
        node_ids = {name: self._target_nodes.get((run.run_id, f"parameter:{name}")) for name in (*_INPUTS, _TARGET)}
        if any(value is None for value in node_ids.values()):
            raise ValueError("recorded derivation target is absent from the trace graph")
        for name in _INPUTS:
            source_target, target_target = f"parameter:{name}", f"parameter:{_TARGET}"
            self._dependency_edges.append({
                "id": _id(run.run_id, evidence_hash, node_ids[name], node_ids[_TARGET]),
                "run_id": run.run_id,
                "source": node_ids[name],
                "target": node_ids[_TARGET],
                "source_target_id": source_target,
                "target_target_id": target_target,
                "relation": "input_to_derived_parameter",
                "formula": _FORMULA,
                "source_start": start,
                "source_end": end,
                "source_text": source_text,
                "generation_evidence_sha256": evidence_hash,
                "run_snapshot_sha256": self._run_sha256[run.run_id],
                "target_name": _TARGET,
                "provider": evidence["provider"],
                "offline_local_provider": True,
            })


__all__ = ["DEPENDENCY_SCHEMA_VERSION", "DEPENDENCY_SCOPE", "DependencyTraceGraph"]
