"""Versioned car Service: retain V10 project/source/confirmation semantics."""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping
import uuid

from week11.src.service import Service, _public_proposal
from week11.src.store import digest_json, safe_id, canonical_json
from .contracts import (WEEK12_ROOT, PHYSICAL_SHA, PHYSICAL_PATH, CONTRACT_SHA,
                        validate_spec, physical_spec, authorization, approved_context, inside_week12, profile_module, execution_route)
from .store_adapter import CarStore, ReadOnlyCarStore


class CarService(Service):
    def __init__(self, db_path, session_id, *, worker_manager=None, recover=False, read_only=False, profile=None):
        safe_id(session_id, "session_id")
        self.session_id = session_id
        self.profile = profile_module(profile).PROFILE
        if read_only:
            self.store = ReadOnlyCarStore(db_path)
            self.worker_manager = None
            self._current_project()  # Existing session only; do not create or recover anything.
            return
        self.store = CarStore(db_path)
        self.store.ensure_session(session_id)
        if recover:
            self.store.recover_orphans()
        if worker_manager is None:
            from .worker_adapter import CarWorkerManager
            worker_manager = CarWorkerManager(self.store, profile=self.profile)
        self.worker_manager = worker_manager

    @staticmethod
    def _ok(key, value, **extra):
        return {"schema_version": "v11-car-service-1", "ok": True, key: value, **extra}

    def proposal_prepare(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        project = self._current_project()
        checked = validate_spec(spec)
        if checked.get("ready") is not True:
            return {"schema_version": "v11-car-service-1", "ok": False, "reason": "spec_not_ready", "issues": checked["issues"]}
        canonical = checked["spec"]
        if canonical.get("profile") != self.profile:
            raise ValueError("proposal profile differs from selected service execution profile")
        auth = authorization(self.profile)
        self._verify_sources(project["project_id"], canonical)
        if project["selected_version_id"] != project["current_version_id"]:
            raise ValueError("historical selected version is read-only")
        if project["current_version_id"] is not None:
            raise ValueError("reference profile cannot replace a vehicle current version")
        selected=profile_module(self.profile)
        PROFILE,SOURCE_SHA,REF_CONTRACT_SHA=selected.PROFILE,getattr(selected,"SOURCE_SHA",None),selected.CONTRACT_SHA
        context = approved_context(self.profile)
        display = {"schema_version": "v11-reference-proposal-display-1", "project_id": project["project_id"], "project_name": project["name"],
                   "operation": "create", "baseline_version_id": None, "project_revision": project["revision"],
                   "spec_digest": digest_json(canonical), "sources": deepcopy(canonical["sources"]),
                   "requirements": deepcopy(canonical["requirements"]), "design_permission": deepcopy(canonical["design_permission"]),
                   "constraints": [], "preferences": [], "ideas": [], "profile": PROFILE,
                   "reference_ref": deepcopy(canonical.get("reference_ref")), "parameters": deepcopy(canonical.get("parameters")),
                   "support": "USER_REFERENCE_TEMPLATE_REUSE", "new_design_synthesis": False,
                   "limitations": context["limitations"], "vehicle_task_status": "UNKNOWN",
                   "execution_scope_id": auth["scope_id"], "reference_contract_sha256": REF_CONTRACT_SHA}
        if self.profile == 'active_eightwheel_v1':
            from .eightwheel_profile import PROFILE as EIGHTWHEEL_PROFILE, CONTRACT_SHA as EIGHTWHEEL_CONTRACT, GENERATED_BODY_SHA
            display.update(schema_version="v11-eightwheel-proposal-display-1", profile=EIGHTWHEEL_PROFILE,
                           support="EXISTING_APPROVED_EIGHTWHEEL_ONLY", physical_ref=deepcopy(canonical["physical_ref"]),
                           execution_contract_sha256=EIGHTWHEEL_CONTRACT, physical_spec_sha256=PHYSICAL_SHA,
                           generated_body_sha256=GENERATED_BODY_SHA)
            for key in ("reference_ref","parameters","reference_contract_sha256"):
                display.pop(key,None)
        proposal = self.store.insert_proposal({"proposal_id": "prop_" + uuid.uuid4().hex, "session_id": self.session_id,
                                              "project_id": project["project_id"], "operation": display["operation"],
                                              "baseline_version_id": project["current_version_id"], "project_revision": project["revision"],
                                              "spec": canonical, "spec_digest": digest_json(canonical), "display": display,
                                              "displayed_digest": digest_json(display)})
        return self._ok("proposal", _public_proposal(proposal))

    def confirm_approved_test_only(self, proposal_id: str, displayed_digest: str) -> dict[str, Any]:
        """Programmatic acceptance import, not a fabricated human confirmation."""
        project = self._current_project()
        if not project["name"].startswith("TEST_ONLY"):
            raise ValueError("test-only authorization import requires TEST_ONLY project")
        proposal = self.store.proposal(proposal_id)
        self._require_current_project(proposal["project_id"])
        auth = authorization(self.profile)
        checked = validate_spec(proposal["spec"])
        selected=profile_module(self.profile)
        PROFILE,SOURCE_SHA,REF_CONTRACT_SHA,AUTH_SHA=selected.PROFILE,getattr(selected,"SOURCE_SHA",None),selected.CONTRACT_SHA,selected.AUTH_SHA
        if (not checked.get("ready") or proposal["display"].get("profile") != self.profile
                or proposal["display"].get("execution_scope_id") != auth["scope_id"]
                or digest_json(checked["spec"]) != proposal["spec_digest"]
                or digest_json(proposal["display"]) != displayed_digest):
            raise ValueError("test import restricted to exact new reference approval and displayed proposal")
        self._verify_sources(project["project_id"], checked["spec"])
        self.store.confirm(proposal_id, displayed_digest, self.session_id)
        with self.store.transaction() as db:
            row = db.execute("SELECT confirmation_json,state FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
            if row["state"] != "approved":
                raise ValueError("test confirmation changed concurrently")
            confirmation = json.loads(row["confirmation_json"])
            confirmation.update(channel="EXISTING_USER_AUTHORIZATION_IMPORT", human_new_approval=False,
                                reference_contract_sha256=REF_CONTRACT_SHA, reference_source_sha256=SOURCE_SHA,
                                authorization_sha256=AUTH_SHA, profile=PROFILE, execution_scope_id=auth["scope_id"])
            if self.profile == 'active_eightwheel_v1':
                from .eightwheel_profile import CONTRACT_SHA as EIGHTWHEEL_CONTRACT, AUTH_SHA as EIGHTWHEEL_AUTH, GENERATED_BODY_SHA
                for key in ("reference_contract_sha256","reference_source_sha256"):
                    confirmation.pop(key,None)
                confirmation.update(profile=self.profile, authorization_sha256=EIGHTWHEEL_AUTH,
                                    execution_contract_sha256=EIGHTWHEEL_CONTRACT,physical_spec_sha256=PHYSICAL_SHA,
                                    generated_body_sha256=GENERATED_BODY_SHA)
            db.execute("UPDATE proposals SET confirmation_json=? WHERE proposal_id=?", (canonical_json(confirmation), proposal_id))
        return self._ok("proposal", _public_proposal(self.store.proposal(proposal_id)))

    def execution_start(self, proposal_id, request_id):
        proposal = self.store.proposal(proposal_id)
        self._require_current_project(proposal["project_id"])
        checked = validate_spec(proposal["spec"])
        if not checked.get("ready") or digest_json(checked["spec"]) != proposal["spec_digest"]:
            raise ValueError("approved car specification failed revalidation")
        if proposal["spec"].get("profile") != self.profile:
            raise ValueError("task differs from selected execution profile")
        execution_route(self.profile).verify_confirmation(proposal, self.session_id)
        authorization(self.profile)
        self._verify_sources(proposal["project_id"], proposal["spec"])
        return super().execution_start(proposal_id, request_id)

    def _version_dir(self, project_id, version_id) -> Path:
        record = self.store.version(project_id, version_id)
        path = inside_week12(WEEK12_ROOT / record["directory"])
        expected = (WEEK12_ROOT / "projects" / project_id / "versions" / version_id).resolve()
        if path != expected:
            raise ValueError("version directory escapes reserved project version")
        return path

    def _verify_version(self, project_id, version_id) -> Path:
        path = self._version_dir(project_id, version_id)
        record = self.store.version(project_id, version_id)
        task = self.store.task(record["run_id"])
        proposal = self.store.proposal(task["proposal_id"])
        from .eightwheel_profile import PROFILE as EIGHTWHEEL_PROFILE
        eightwheel = proposal["spec"].get("profile") == EIGHTWHEEL_PROFILE
        filename = "eightwheel-outcome-manifest.json" if eightwheel else "version-manifest.json"
        manifest = _verify_manifest(path / filename, record["manifest_sha256"], path)
        schema_ok = manifest.get("schema") == "v11-eightwheel-manifest-1" if eightwheel else manifest.get("schema_version") == "v11-car-version-manifest-1"
        if not schema_ok or manifest.get("project_id") != project_id or manifest.get("task_id") != record["run_id"]:
            raise ValueError("published car manifest identity/schema mismatch")
        return path

    def _load_version_spec(self, project_id, version_id):
        path = self._verify_version(project_id, version_id)
        checked = validate_spec(json.loads((path / "spec.json").read_bytes()))
        if not checked.get("ready"):
            raise ValueError("invalid published car specification")
        return checked["spec"]

    def versions_compare(self, before_version_id, after_version_id):
        project = self._current_project()
        left = self._load_version_spec(project["project_id"], before_version_id)
        right = self._load_version_spec(project["project_id"], after_version_id)
        return self._ok("comparison", {"project_id": project["project_id"], "before_version_id": before_version_id,
                                      "after_version_id": after_version_id, "physical_ref_same": left.get("physical_ref",left.get("reference_ref")) == right.get("physical_ref",right.get("reference_ref")),
                                      "requirements_before": left["requirements"], "requirements_after": right["requirements"],
                                      "scope": "hash-verified car versions; no V8 graph or physics equivalence claim"})

    def evidence_query(self, question: str, version_id=None):
        project = self._current_project()
        if not isinstance(question, str) or not question.startswith("task:"):
            raise ValueError("car evidence query uses fixed syntax task:<task_id>")
        task_id = safe_id(question[5:], "task_id")
        task = self.store.task(task_id)
        self._require_current_project(task["project_id"])
        if version_id is not None:
            version = self.store.version(project["project_id"], version_id)
            if version["run_id"] != task_id:
                raise ValueError("requested version belongs to another task")
        result = task.get("result") or {}
        base = (WEEK12_ROOT / "projects" / project["project_id"] / "versions" / task["version_id"]).resolve()
        raw_path = result.get("outcome_manifest_path")
        if not isinstance(raw_path, str) or not result.get("outcome_manifest_sha256"):
            raise ValueError("task has no committed outcome manifest")
        manifest_path = Path(raw_path)
        if not manifest_path.is_absolute():
            manifest_path = base / manifest_path
        manifest_path = inside_week12(manifest_path)
        try:
            manifest_path.relative_to(base)
        except ValueError as exc:
            raise ValueError("task outcome manifest escapes this project's task") from exc
        manifest = _verify_manifest(manifest_path, result["outcome_manifest_sha256"], base)
        binding = manifest.get("binding", manifest)
        if binding.get("task_id") != task_id or binding.get("project_id") != project["project_id"]:
            raise ValueError("manifest task/project binding mismatch")
        proposal = self.store.proposal(task["proposal_id"])
        from .reference_profile import PROFILE as REFERENCE_PROFILE
        if proposal["spec"].get("profile") == REFERENCE_PROFILE:
            from .reference_history import verify_historical_reference
            verify_historical_reference(manifest, result, task, proposal)
        else:
            execution_route(proposal["spec"].get("profile")).verify_manifest_binding(
                manifest,result,task,proposal,task["worker_owner"])
        return self._ok("evidence", {"project_id": project["project_id"], "task_id": task_id,
                                     "version_id": result.get("published_version_id"), "outcome": result,
                                     "manifest_sha256": result["outcome_manifest_sha256"], "manifest": manifest})


def _verify_manifest(path: Path, expected_sha: str, root: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError("manifest hash mismatch")
    manifest = json.loads(raw)
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("manifest must enumerate immutable evidence files")
    for name, record in files.items():
        if not isinstance(name, str) or not name or Path(name).is_absolute() or ".." in Path(name).parts or not isinstance(record, dict):
            raise ValueError("unsafe evidence manifest path")
        candidate = (root / name).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError as exc:
            raise ValueError("evidence path escapes this task") from exc
        with candidate.open("rb") as stream:
            actual_sha = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual_sha != record.get("sha256") or candidate.stat().st_size != record.get("bytes"):
            raise ValueError("evidence artifact hash mismatch")
    return manifest
