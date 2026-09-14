"""Evidence-bounded trace graph for validated V8 ``RunResult`` snapshots.

``TraceGraph.to_dict`` returns ``schema_version``, ``scope``, ``runs``, ``nodes``
and ``edges``.  Node and edge IDs are deterministic SHA-256 identifiers scoped
by ``run_id``.  Query methods share these stable JSON fields::

    run_id, requirements[{id, text}], target_ids[str],
    elements[{layer, element_id, exact_selector, artifact_sha256}],
    artifacts[{artifact_type, sha256}],
    checks[{check_type, status, required}], evidence_sha256

Only VERIFIED ``TraceLink`` records create graph facts.  Shared artifacts and
checks are never traversed to infer additional requirements.  In particular,
reverse and impact results describe explicit structural correspondence rather
than numerical dependency or root cause.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any, Iterable, Mapping

from week9.src.schemas import (
    RunResult,
    TraceLinkStatus,
    traceability_report_sha256,
)


SCHEMA_VERSION = "v9-evidenced-trace-graph-1"
SCOPE = "explicit_structural_correspondence_only"
REVERSE_SCOPE_WARNING = (
    "An explicit check association is not proof that the requirement is a root cause."
)


def _sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _graph_id(run_id: str, kind: str, identity: Any) -> str:
    """Return an opaque ID whose hash input always includes the run version."""

    return f"{kind}:{_sha256([run_id, kind, identity])}"


def _status(value: Any) -> str:
    return str(getattr(value, "value", value))


def _sorted_unique(records: Iterable[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    indexed: dict[tuple[Any, ...], dict[str, Any]] = {}
    for record in records:
        key = tuple(record.get(name) for name in keys)
        indexed.setdefault(key, record)
    return [indexed[key] for key in sorted(indexed, key=lambda item: tuple(str(v) for v in item))]


class TraceGraph:
    """Store defensive run snapshots and expose bounded trace queries."""

    def __init__(self) -> None:
        self._runs: dict[str, RunResult] = {}
        self._run_sha256: dict[str, str] = {}
        self._report_sha256: dict[str, str] = {}
        self._links: dict[str, tuple[Any, ...]] = {}
        self._nodes: dict[str, dict[str, Any]] = {}
        self._edges: dict[str, dict[str, Any]] = {}

    def add_run(self, run: RunResult) -> str:
        """Validate, defensively copy, and add one immutable evidence snapshot."""

        if not isinstance(run, RunResult):
            raise ValueError("invalid RunResult snapshot")
        candidate_id = getattr(run, "run_id", None)
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            raise ValueError("invalid RunResult snapshot")
        if candidate_id in self._runs:
            raise ValueError(f"duplicate run_id: {candidate_id}")
        try:
            serialized = run.to_dict()
            snapshot = RunResult.from_dict(serialized)
            # Exercise serialization validation on the independent object too.
            snapshot_dict = snapshot.to_dict()
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("invalid RunResult snapshot") from exc

        run_id = snapshot.run_id
        if run_id in self._runs:
            raise ValueError(f"duplicate run_id: {run_id}")
        report = snapshot.traceability
        if report is None or not report.links or not report.integrity_passed:
            raise ValueError("run requires non-empty passing traceability evidence")

        verified = tuple(
            link for link in report.links if link.status is TraceLinkStatus.VERIFIED
        )
        if not verified:
            raise ValueError("run has no VERIFIED trace links")

        report_sha = traceability_report_sha256(report)
        run_sha = _sha256(snapshot_dict)
        self._runs[run_id] = snapshot
        self._run_sha256[run_id] = run_sha
        self._report_sha256[run_id] = report_sha
        self._links[run_id] = verified
        self._build_run_graph(snapshot, verified, report_sha, run_sha)
        return run_id

    def to_dict(self) -> dict[str, Any]:
        """Return a detached, deterministically ordered JSON-ready graph."""

        runs = [
            {
                "run_id": run_id,
                "case_id": self._runs[run_id].case_id,
                "template_id": self._runs[run_id].traceability.template_id,
                "run_snapshot_sha256": self._run_sha256[run_id],
                "evidence_sha256": self._report_sha256[run_id],
            }
            for run_id in sorted(self._runs)
        ]
        result = {
            "schema_version": SCHEMA_VERSION,
            "scope": SCOPE,
            "runs": runs,
            "nodes": [deepcopy(self._nodes[key]) for key in sorted(self._nodes)],
            "edges": [deepcopy(self._edges[key]) for key in sorted(self._edges)],
        }
        return result

    def query_requirement(self, run_id: str, requirement_id: str) -> dict[str, Any]:
        """Return exactly the verified link for ``requirement_id``."""

        self._require_run(run_id)
        selected = [
            link for link in self._links[run_id]
            if link.requirement_id == requirement_id
        ]
        if not selected:
            raise ValueError(f"unknown requirement_id for run {run_id}: {requirement_id}")
        result = self._query(run_id, selected)
        result["coverage_status"] = "explicit_verified_link"
        return result

    def impact(self, run_id: str, target_id: str) -> dict[str, Any]:
        """Return links explicitly bound to a target and a conservative check set."""

        run = self._require_run(run_id)
        selected = [link for link in self._links[run_id] if link.target_id == target_id]
        if not selected:
            raise ValueError(f"unknown target_id for run {run_id}: {target_id}")
        result = self._query(run_id, selected)
        result.update(
            {
                "scope": SCOPE,
                "numerical_behavior_guarantee": False,
                "rerun_required_checks": self._required_checks(run),
            }
        )
        return result

    def reverse_check(self, run_id: str, check_type: str) -> dict[str, Any]:
        """Return only links that explicitly reference ``check_type``.

        A run check with no trace references intentionally yields empty coverage;
        the implementation does not walk through a shared check or artifact node.
        """

        run = self._require_run(run_id)
        if not isinstance(check_type, str) or not check_type.strip():
            raise ValueError("check_type must be a non-empty string")
        selected = [
            link
            for link in self._links[run_id]
            if any(ref.check_type == check_type for ref in link.check_references)
        ]
        result = self._query(run_id, selected, check_type=check_type)
        present = any(check.check_type == check_type for check in run.checks)
        result.update(
            {
                "scope": SCOPE,
                "scope_warning": REVERSE_SCOPE_WARNING,
                "coverage_status": (
                    "explicit_verified_links"
                    if selected
                    else (
                        "check_present_without_explicit_trace_links"
                        if present
                        else "check_absent_from_run"
                    )
                ),
            }
        )
        return result

    def compare_runs(self, before_run_id: str, after_run_id: str) -> dict[str, Any]:
        """Compare physical parameter values/units and actual artifact hashes."""

        before = self._require_run(before_run_id)
        after = self._require_run(after_run_id)
        if before.case_id != after.case_id:
            raise ValueError("cannot compare runs with different case_id values")
        before_template = before.traceability.template_id
        after_template = after.traceability.template_id
        if before_template != after_template:
            raise ValueError("cannot compare runs with different template_id values")

        before_parameters = self._parameter_map(before)
        after_parameters = self._parameter_map(after)
        parameter_changes: list[dict[str, Any]] = []
        for name in sorted(set(before_parameters) | set(after_parameters)):
            left = before_parameters.get(name)
            right = after_parameters.get(name)
            if left is not None and right is not None and left == right:
                continue
            parameter_changes.append(
                {
                    "canonical_name": name,
                    "before": None if left is None else {"value": left[0], "unit": left[1]},
                    "after": None if right is None else {"value": right[0], "unit": right[1]},
                }
            )

        changed_targets = [
            f"parameter:{item['canonical_name']}" for item in parameter_changes
        ]
        before_artifacts = {name: item.sha256 for name, item in before.artifacts.items()}
        after_artifacts = {name: item.sha256 for name, item in after.artifacts.items()}
        changed_artifacts = sorted(
            name
            for name in set(before_artifacts) | set(after_artifacts)
            if before_artifacts.get(name) != after_artifacts.get(name)
        )

        impacts = [
            self._comparison_impact(before_run_id, after_run_id, target_id)
            for target_id in changed_targets
        ]
        return {
            "before_run_id": before_run_id,
            "after_run_id": after_run_id,
            "case_id": before.case_id,
            "template_id": before_template,
            "scope": SCOPE,
            "parameter_changes": parameter_changes,
            "changed_target_ids": changed_targets,
            "changed_artifact_types": changed_artifacts,
            "impacts": impacts,
            "before_required_checks": self._required_checks(before),
            "after_required_checks": self._required_checks(after),
        }

    def _require_run(self, run_id: str) -> RunResult:
        try:
            return self._runs[run_id]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"unknown run_id: {run_id}") from exc

    @staticmethod
    def _parameter_map(run: RunResult) -> dict[str, tuple[Any, str]]:
        result: dict[str, tuple[Any, str]] = {}
        for parameter in run.parameters:
            if parameter.canonical_name in result:
                raise ValueError("normalized parameter names must be unique for comparison")
            result[parameter.canonical_name] = (parameter.value, parameter.unit)
        return result

    @staticmethod
    def _required_checks(run: RunResult) -> list[dict[str, Any]]:
        return sorted(
            (
                {
                    "check_type": check.check_type,
                    "status": _status(check.status),
                    "required": check.required,
                }
                for check in run.checks
                if check.required
            ),
            key=lambda item: item["check_type"],
        )

    def _query(
        self,
        run_id: str,
        links: Iterable[Any],
        *,
        check_type: str | None = None,
    ) -> dict[str, Any]:
        requirements: list[dict[str, Any]] = []
        targets: set[str] = set()
        elements: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        checks: list[dict[str, Any]] = []
        for link in links:
            requirements.append(
                {
                    "id": link.requirement_id,
                    "text": link.requirement_text,
                    "critical": link.critical,
                    "source_start": link.requirement_source_start,
                    "source_end": link.requirement_source_end,
                    "requirement_artifact_sha256": (
                        self._runs[run_id].traceability.requirement_artifact_sha256
                    ),
                }
            )
            targets.add(link.target_id)
            for element in (*link.sysml_elements, *link.modelica_elements):
                elements.append(
                    {
                        "layer": _status(element.layer),
                        "element_id": element.element_id,
                        "exact_selector": element.exact_selector,
                        "artifact_sha256": element.artifact_sha256,
                        "kind": element.kind,
                        "cardinality": element.cardinality,
                    }
                )
                artifacts.append(
                    {
                        "artifact_type": _status(element.layer),
                        "sha256": element.artifact_sha256,
                    }
                )
            for reference in link.check_references:
                if check_type is None or reference.check_type == check_type:
                    checks.append(
                        {
                            "check_type": reference.check_type,
                            "status": _status(reference.status),
                            "required": reference.required,
                            "semantic_evidence_sha256": reference.semantic_evidence_sha256,
                        }
                    )
        return {
            "run_id": run_id,
            "requirements": _sorted_unique(requirements, ("id",)),
            "target_ids": sorted(targets),
            "elements": _sorted_unique(
                elements, ("layer", "element_id", "exact_selector", "artifact_sha256")
            ),
            "artifacts": _sorted_unique(artifacts, ("artifact_type", "sha256")),
            "checks": _sorted_unique(checks, ("check_type",)),
            "evidence_sha256": self._report_sha256[run_id],
        }

    def _comparison_impact(
        self, before_run_id: str, after_run_id: str, target_id: str
    ) -> dict[str, Any]:
        before_links = [
            link for link in self._links[before_run_id] if link.target_id == target_id
        ]
        after_links = [
            link for link in self._links[after_run_id] if link.target_id == target_id
        ]
        before_result = self._query(before_run_id, before_links) if before_links else None
        after_result = self._query(after_run_id, after_links) if after_links else None
        return {
            "target_id": target_id,
            "scope": SCOPE,
            "numerical_behavior_guarantee": False,
            "before": before_result,
            "after": after_result,
        }

    def _build_run_graph(
        self,
        run: RunResult,
        links: tuple[Any, ...],
        report_sha: str,
        run_sha: str,
    ) -> None:
        report = run.traceability
        for link in links:
            link_sha = _sha256(
                {
                    "requirement_id": link.requirement_id,
                    "requirement_text_sha256": link.requirement_text_sha256,
                    "target_id": link.target_id,
                    "normalized_parameter_sha256": link.normalized_parameter_sha256,
                    "elements": [
                        [element.layer.value, element.element_id, element.exact_selector,
                         element.artifact_sha256, element.cardinality]
                        for element in (*link.sysml_elements, *link.modelica_elements)
                    ],
                    "checks": [
                        [ref.check_type, ref.status.value, ref.required,
                         ref.semantic_evidence_sha256]
                        for ref in link.check_references
                    ],
                }
            )
            requirement_id = self._node(
                run.run_id,
                "requirement",
                link.requirement_id,
                {
                    "requirement_id": link.requirement_id,
                    "text": link.requirement_text,
                    "text_sha256": link.requirement_text_sha256,
                    "source_start": link.requirement_source_start,
                    "source_end": link.requirement_source_end,
                    "requirement_artifact_sha256": report.requirement_artifact_sha256,
                },
            )
            requirement_artifact_id = self._node(
                run.run_id,
                "artifact",
                ["requirement", report.requirement_artifact_sha256],
                {
                    "artifact_type": "requirement",
                    "sha256": report.requirement_artifact_sha256,
                },
            )
            target_id = self._node(
                run.run_id, "target", link.target_id, {"target_id": link.target_id}
            )
            provenance = {
                "source": "TraceabilityReport.links",
                "binding_basis": link.binding_basis,
                "requirement_id": link.requirement_id,
                "target_id": link.target_id,
            }
            self._edge(
                run.run_id, requirement_id, target_id, "explicitly_bound_to",
                provenance, report_sha, run_sha, link_sha
            )
            self._edge(
                run.run_id,
                requirement_id,
                requirement_artifact_id,
                "sourced_from_exact_span",
                {
                    **provenance,
                    "source_start": link.requirement_source_start,
                    "source_end": link.requirement_source_end,
                },
                report_sha,
                run_sha,
                link_sha,
            )
            for element in (*link.sysml_elements, *link.modelica_elements):
                element_id = self._node(
                    run.run_id,
                    "element",
                    [element.layer.value, element.element_id, element.exact_selector,
                     element.artifact_sha256],
                    {
                        "layer": element.layer.value,
                        "element_id": element.element_id,
                        "kind": element.kind,
                        "exact_selector": element.exact_selector,
                        "artifact_sha256": element.artifact_sha256,
                        "cardinality": element.cardinality,
                    },
                )
                artifact_id = self._node(
                    run.run_id,
                    "artifact",
                    [element.layer.value, element.artifact_sha256],
                    {"artifact_type": element.layer.value, "sha256": element.artifact_sha256},
                )
                self._edge(
                    run.run_id, target_id, element_id, "structurally_represented_by",
                    provenance, report_sha, run_sha, link_sha
                )
                self._edge(
                    run.run_id, element_id, artifact_id, "evidenced_in_artifact",
                    provenance, report_sha, run_sha, link_sha
                )
            for reference in link.check_references:
                check_id = self._node(
                    run.run_id,
                    "check",
                    [reference.check_type, reference.semantic_evidence_sha256],
                    {
                        "check_type": reference.check_type,
                        "status": reference.status.value,
                        "required": reference.required,
                        "semantic_evidence_sha256": reference.semantic_evidence_sha256,
                    },
                )
                self._edge(
                    run.run_id, target_id, check_id, "explicitly_checked_by",
                    provenance, report_sha, run_sha, link_sha
                )

    def _node(
        self, run_id: str, kind: str, identity: Any, payload: Mapping[str, Any]
    ) -> str:
        node_id = _graph_id(run_id, kind, identity)
        node = {"id": node_id, "run_id": run_id, "type": kind, **dict(payload)}
        existing = self._nodes.get(node_id)
        if existing is not None and existing != node:
            raise ValueError("conflicting graph node evidence")
        self._nodes[node_id] = node
        return node_id

    def _edge(
        self,
        run_id: str,
        source: str,
        target: str,
        relation: str,
        provenance: Mapping[str, Any],
        report_sha: str,
        run_sha: str,
        link_sha: str,
    ) -> None:
        identity = [source, target, relation, link_sha]
        edge_id = _graph_id(run_id, "edge", identity)
        self._edges[edge_id] = {
            "id": edge_id,
            "run_id": run_id,
            "source": source,
            "target": target,
            "relation": relation,
            "provenance": dict(provenance),
            "evidence_sha256": report_sha,
            "run_snapshot_sha256": run_sha,
            "link_evidence_sha256": link_sha,
        }
