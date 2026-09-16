"""Business rules for the V10 project and execution service."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping
import uuid

from week9.src.schemas import RunResult
from week10.src.graph_evidence_view import build_evidence_answer
from week10.src.graph_retrieval_v2 import EvidenceIndexV2
from week10.src.trace_graph import TraceGraph
from week11.src.spec import spec_digest, validate_spec
from week11.src.store import Store, WEEK11_ROOT, digest_json, safe_id


SERVICE_SCHEMA = "v10-service-1"


class Service:
    """One session-scoped API used by both the CLI and MCP server."""

    def __init__(
        self,
        db_path: str | Path,
        session_id: str,
        *,
        worker_manager: Any | None = None,
        recover: bool = False,
    ) -> None:
        safe_id(session_id, "session_id")
        self.session_id = session_id
        self.store = Store(db_path)
        self.store.ensure_session(session_id)
        if recover:
            self.store.recover_orphans()
        if worker_manager is None:
            from week11.src.worker import WorkerManager

            worker_manager = WorkerManager(self.store)
        self.worker_manager = worker_manager

    def project_create(self, name: str) -> dict[str, Any]:
        return self._ok("project", self.store.create_project(self.session_id, name))

    def project_list(self) -> dict[str, Any]:
        current = self.store.current_project(self.session_id)
        return self._ok(
            "projects",
            {
                "current_project_id": current["project_id"] if current else None,
                "items": self.store.list_projects(),
            },
        )

    def project_open(self, project_id: str) -> dict[str, Any]:
        return self._ok("project", self.store.open_project(self.session_id, project_id))

    def project_status(self) -> dict[str, Any]:
        project = self._current_project()
        tasks = self.store.tasks_for_project(project["project_id"])
        return self._ok(
            "status",
            {
                "project": project,
                "latest_task": tasks[-1] if tasks else None,
                "version_count": len(self.store.versions(project["project_id"])),
                "pending_proposals": self.store.pending_proposals(self.session_id, project["project_id"]),
            },
        )

    def record_source(self, text: str) -> dict[str, Any]:
        """CLI-only trusted input registration; never placed in the MCP tool list."""
        return self.store.record_source(self.session_id, text)

    def proposal_prepare(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        project = self._current_project()
        result = validate_spec(spec)
        if result.get("ready") is not True:
            return {
                "schema_version": SERVICE_SCHEMA,
                "ok": False,
                "reason": "spec_not_ready",
                "issues": result.get("issues", []),
            }
        canonical = result["spec"]
        self._verify_sources(project["project_id"], canonical)
        digest = spec_digest(canonical)
        baseline_id = project["current_version_id"]
        operation = "create" if baseline_id is None else "modify"
        changes: list[dict[str, Any]] = []
        preserved: list[str] = []
        if operation == "modify":
            if project["selected_version_id"] != project["current_version_id"]:
                raise ValueError("historical selected version is read-only; select the latest version before modification")
            baseline_spec = self._load_version_spec(project["project_id"], baseline_id)
            changes, preserved = self._validate_modification(baseline_spec, canonical)
            baseline_evidence = self._modification_baseline_evidence(
                project["project_id"], baseline_id, baseline_spec, changes
            )
        else:
            baseline_evidence = None
        display = {
            "schema_version": "v10-proposal-display-1",
            "project_id": project["project_id"],
            "project_name": project["name"],
            "operation": operation,
            "baseline_version_id": baseline_id,
            "project_revision": project["revision"],
            "spec_digest": digest,
            "template_id": canonical["template_id"],
            "sources": deepcopy(canonical["sources"]),
            "normalized_parameters": deepcopy(canonical["normalized_parameters"]),
            "derivations": deepcopy(canonical["derivations"]),
            "fixed_stimulus": deepcopy(canonical["fixed_stimulus"]),
            "stimulus_source": deepcopy(canonical["stimulus"]),
            "assumptions": deepcopy(canonical["assumptions"]),
            "constraints": deepcopy(canonical["constraints"]),
            "changes": changes,
            "preserved": preserved,
            "baseline_evidence": baseline_evidence,
        }
        proposal_id = "prop_" + uuid.uuid4().hex
        displayed_digest = digest_json(display)
        proposal = self.store.insert_proposal(
            {
                "proposal_id": proposal_id,
                "session_id": self.session_id,
                "project_id": project["project_id"],
                "operation": operation,
                "baseline_version_id": baseline_id,
                "project_revision": project["revision"],
                "spec_digest": digest,
                "spec": canonical,
                "display": display,
                "displayed_digest": displayed_digest,
            }
        )
        return self._ok("proposal", _public_proposal(proposal))

    def proposal_get(self, proposal_id: str) -> dict[str, Any]:
        proposal = self.store.proposal(proposal_id)
        self._require_current_project(proposal["project_id"])
        return self._ok("proposal", _public_proposal(proposal))

    def proposal_cancel(self, proposal_id: str) -> dict[str, Any]:
        proposal = self.store.proposal(proposal_id)
        self._require_current_project(proposal["project_id"])
        return self._ok("proposal", _public_proposal(self.store.cancel_proposal(proposal_id, self.session_id)))

    def confirm_local(self, proposal_id: str, displayed_digest: str) -> dict[str, Any]:
        """Trusted CLI confirmation.  MCP intentionally has no corresponding tool."""
        proposal = self.store.proposal(proposal_id)
        self._require_current_project(proposal["project_id"])
        confirmed = self.store.confirm(proposal_id, displayed_digest, self.session_id)
        return self._ok("proposal", _public_proposal(confirmed))

    def execution_start(self, proposal_id: str, request_id: str) -> dict[str, Any]:
        proposal = self.store.proposal(proposal_id)
        self._require_current_project(proposal["project_id"])
        task, created = self.store.create_task(proposal_id, self.session_id, request_id)
        if created:
            self.worker_manager.start(task["task_id"])
            task = self.store.task(task["task_id"])
        return self._ok("task", _public_task(task), reused=not created)

    def execution_status(self, task_id: str) -> dict[str, Any]:
        task = self.store.task(task_id)
        self._require_current_project(task["project_id"])
        return self._ok("task", _public_task(task))

    def execution_cancel(self, task_id: str) -> dict[str, Any]:
        task = self.store.task(task_id)
        self._require_current_project(task["project_id"])
        return self._ok("task", _public_task(self.worker_manager.cancel(task_id)))

    def versions_list(self) -> dict[str, Any]:
        project = self._current_project()
        return self._ok(
            "versions",
            {
                "current_version_id": project["current_version_id"],
                "selected_version_id": project["selected_version_id"],
                "items": self.store.versions(project["project_id"]),
            },
        )

    def versions_select(self, version_id: str) -> dict[str, Any]:
        project = self._current_project()
        return self._ok("project", self.store.select_version(project["project_id"], version_id))

    def versions_compare(self, before_version_id: str, after_version_id: str) -> dict[str, Any]:
        project = self._current_project()
        before = self._load_run(project["project_id"], before_version_id)
        after = self._load_run(project["project_id"], after_version_id)
        graph = TraceGraph()
        graph.add_run(before)
        graph.add_run(after)
        return self._ok("comparison", graph.compare_runs(before.run_id, after.run_id))

    def evidence_query(self, question: str, version_id: str | None = None) -> dict[str, Any]:
        project = self._current_project()
        selected = version_id or project["selected_version_id"] or project["current_version_id"]
        if selected is None:
            raise ValueError("project has no validated version")
        run = self._load_run(project["project_id"], selected)
        query = question
        if "来自" in query or "来源" in query:
            query = query.replace("来自", "对应需求").replace("来源", "对应需求")
        index = EvidenceIndexV2([run])
        answer = build_evidence_answer(index, query, [run.run_id])
        spec = self._load_version_spec(project["project_id"], selected)
        return self._ok(
            "evidence",
            {
                "project_id": project["project_id"],
                "version_id": selected,
                "run_id": run.run_id,
                "question": question,
                "answer": answer,
                "v10_derivations": deepcopy(spec.get("derivations", [])),
                "dependency_scope": "V10 explicit formula provenance; no V9 DependencyTraceGraph claim",
            },
        )

    def _current_project(self) -> dict[str, Any]:
        project = self.store.current_project(self.session_id)
        if project is None:
            raise ValueError("no project selected; create or open a project first")
        return project

    def _require_current_project(self, project_id: str) -> None:
        current = self._current_project()
        if current["project_id"] != project_id:
            raise ValueError("object belongs to another project; open it explicitly")

    def _verify_sources(self, project_id: str, spec: Mapping[str, Any]) -> None:
        declared = spec.get("sources", [])
        ids = [item.get("id") for item in declared if isinstance(item, Mapping)]
        if len(ids) != len(declared) or any(not isinstance(item, str) for item in ids):
            raise ValueError("spec sources are invalid")
        stored = self.store.sources(self.session_id, project_id, ids)
        expected = {item["source_id"]: item["text"] for item in stored}
        actual = {item["id"]: item["text"] for item in declared}
        if actual != expected:
            raise ValueError("spec source text differs from trusted CLI input")

    def _validate_modification(self, before: Mapping[str, Any], after: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        if before["template_id"] != "rc_filter" or after["template_id"] != "rc_filter":
            raise ValueError("V10 first release permits modifications only for RC filters")
        if before["fixed_stimulus"] != after["fixed_stimulus"] or before["stimulus"]["kind"] != after["stimulus"]["kind"]:
            raise ValueError("RC modification must preserve the fixed stimulus")
        if before["constraints"] != after["constraints"] or before["assumptions"] != after["assumptions"]:
            raise ValueError("RC modification must preserve constraints and assumptions")
        left = {item["name"]: item for item in before["normalized_parameters"]}
        right = {item["name"]: item for item in after["normalized_parameters"]}
        if set(left) != set(right):
            raise ValueError("RC modification changed the parameter set")
        changed = [name for name in left if not _same_quantity(left[name], right[name])]
        allowed = {"input_voltage", "cutoff_frequency", "capacitance"}
        if not changed or not set(changed) <= allowed:
            raise ValueError("only RC input_voltage or cutoff_frequency may change")
        if "capacitance" in changed and "cutoff_frequency" not in changed:
            raise ValueError("capacitance may change only as a cutoff-frequency derivation")
        if "cutoff_frequency" in changed:
            if set(changed) != {"cutoff_frequency", "capacitance"}:
                raise ValueError("cutoff change must alter only cutoff_frequency and derived capacitance")
            formula = "C = 1 / (2 * pi * R * fc)"
            for candidate in (before, after):
                derivations = candidate.get("derivations", [])
                if len(derivations) != 1 or derivations[0].get("target") != "capacitance" or derivations[0].get("formula") != formula:
                    raise ValueError("cutoff modification requires the recorded certified RC capacitance derivation")
            if not _same_quantity(left["resistance"], right["resistance"]):
                raise ValueError("cutoff modification must preserve resistance")
        changes = [
            {
                "name": name,
                "before": {"value": left[name]["value"], "unit": left[name]["unit"]},
                "after": {"value": right[name]["value"], "unit": right[name]["unit"]},
                "derived": name == "capacitance",
            }
            for name in changed
        ]
        return changes, sorted(set(left) - set(changed))

    def _modification_baseline_evidence(
        self,
        project_id: str,
        version_id: str,
        baseline_spec: Mapping[str, Any],
        changes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Bind a modify display to declarations and explicit baseline trace links."""

        path = self._verify_version(project_id, version_id)
        run = self._load_run(project_id, version_id)
        graph = TraceGraph()
        if graph.add_run(run) != run.run_id:
            raise ValueError("TraceGraph returned another baseline run id")

        run_parameters = {item.canonical_name: item for item in run.parameters}
        spec_parameters = {
            item["name"]: item for item in baseline_spec["normalized_parameters"]
        }
        if set(run_parameters) != set(spec_parameters) or any(
            not _same_quantity(
                {"value": run_parameters[name].value, "unit": run_parameters[name].unit},
                spec_parameters[name],
            )
            for name in spec_parameters
        ):
            raise ValueError("baseline RunResult parameters differ from stored specification")
        artifact_files = {"sysml": "model.sysml", "modelica": "model.mo"}
        for layer, filename in artifact_files.items():
            artifact = run.artifacts.get(layer)
            if artifact is None or (path / filename).read_text(encoding="utf-8") != artifact.content:
                raise ValueError(f"baseline {layer} artifact differs from its verified RunResult")

        fields: list[dict[str, Any]] = []
        for change in changes:
            name = change["name"]
            parameter = run_parameters.get(name)
            if parameter is None or not _same_quantity(
                {"value": parameter.value, "unit": parameter.unit}, change["before"]
            ):
                raise ValueError(f"baseline RunResult parameter differs from stored specification: {name}")
            target_id = f"parameter:{name}"
            impact = graph.impact(run.run_id, target_id)
            if impact.get("target_ids") != [target_id]:
                raise ValueError(f"baseline trace impact is not confined to changed field: {name}")
            declarations = [
                deepcopy(item)
                for item in impact.get("elements", [])
                if item.get("layer") in artifact_files
            ]
            layers = {item.get("layer") for item in declarations}
            if layers != set(artifact_files):
                raise ValueError(f"baseline trace lacks SysML or Modelica declaration: {name}")
            for declaration in declarations:
                selector = declaration.get("exact_selector")
                layer = declaration["layer"]
                if not isinstance(selector, str) or not selector or selector not in run.artifacts[layer].content:
                    raise ValueError(f"baseline trace declaration is absent from {layer}: {name}")
            fields.append(
                {
                    "name": name,
                    "target_id": target_id,
                    "requirements": deepcopy(impact.get("requirements", [])),
                    "declarations": declarations,
                    "explicit_checks": deepcopy(impact.get("checks", [])),
                    "rerun_required_checks": deepcopy(impact.get("rerun_required_checks", [])),
                    "evidence_sha256": impact.get("evidence_sha256"),
                    "scope": impact.get("scope"),
                    "numerical_behavior_guarantee": impact.get("numerical_behavior_guarantee"),
                }
            )
        return {
            "schema_version": "v10-proposal-baseline-evidence-1",
            "baseline_version_id": version_id,
            "run_id": run.run_id,
            "scope": "verified baseline RunResult; explicit structural correspondence only",
            "changed_fields": fields,
        }

    def _version_dir(self, project_id: str, version_id: str) -> Path:
        record = self.store.version(project_id, version_id)
        path = (WEEK11_ROOT / record["directory"]).resolve()
        try:
            path.relative_to((WEEK11_ROOT / "projects" / project_id / "versions").resolve())
        except ValueError as exc:
            raise ValueError("version directory escapes its project") from exc
        return path

    def _verify_version(self, project_id: str, version_id: str) -> Path:
        record = self.store.version(project_id, version_id)
        path = self._version_dir(project_id, version_id)
        manifest_path = path / "version-manifest.json"
        raw = manifest_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != record["manifest_sha256"]:
            raise ValueError("version manifest hash mismatch")
        manifest = json.loads(raw)
        if manifest.get("schema_version") != "v10-version-manifest-1":
            raise ValueError("unsupported version manifest")
        files = manifest.get("files", {})
        required = {
            "spec.json", "confirmation.json", "requirement.txt", "model.sysml", "model.mo",
            "run-result.json", "generation-binding.json", "trace-evidence.json", "manifest.json",
        }
        if not isinstance(files, dict) or set(files) != required:
            raise ValueError("version manifest file set is incomplete")
        for name, evidence in files.items():
            candidate = (path / name).resolve()
            try:
                candidate.relative_to(path)
            except ValueError as exc:
                raise ValueError("version manifest path escapes version") from exc
            content = candidate.read_bytes()
            if hashlib.sha256(content).hexdigest() != evidence.get("sha256") or len(content) != evidence.get("bytes"):
                raise ValueError("version artifact hash mismatch")
        return path

    def _load_version_spec(self, project_id: str, version_id: str) -> dict[str, Any]:
        path = self._verify_version(project_id, version_id)
        value = json.loads((path / "spec.json").read_text(encoding="utf-8"))
        checked = validate_spec(value)
        if checked.get("ready") is not True:
            raise ValueError("stored version specification is invalid")
        return checked["spec"]

    def _load_run(self, project_id: str, version_id: str) -> RunResult:
        path = self._verify_version(project_id, version_id)
        run = RunResult.from_dict(json.loads((path / "run-result.json").read_text(encoding="utf-8")))
        if not run.overall_success or run.case_id != project_id:
            raise ValueError("stored RunResult is not a passing snapshot for this project")
        return run

    @staticmethod
    def _ok(key: str, value: Any, **extra: Any) -> dict[str, Any]:
        return {"schema_version": SERVICE_SCHEMA, "ok": True, key: value, **extra}


def _same_quantity(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return left.get("unit") == right.get("unit") and math.isclose(float(left.get("value")), float(right.get("value")), rel_tol=1e-12, abs_tol=1e-15)


def _public_proposal(value: Mapping[str, Any]) -> dict[str, Any]:
    return {name: deepcopy(value.get(name)) for name in (
        "proposal_id", "project_id", "operation", "baseline_version_id", "project_revision",
        "spec_digest", "displayed_digest", "state", "display", "confirmation", "created_at", "updated_at",
    )}


def _public_task(value: Mapping[str, Any]) -> dict[str, Any]:
    return {name: deepcopy(value.get(name)) for name in (
        "task_id", "project_id", "proposal_id", "request_id", "version_id", "state", "progress", "result", "error", "created_at", "updated_at",
    )}


__all__ = ["SERVICE_SCHEMA", "Service"]
