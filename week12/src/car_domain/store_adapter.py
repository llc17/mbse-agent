"""V10 transactional store reused with week12 paths and owner CAS writes."""
from __future__ import annotations
from pathlib import Path
from typing import Any
import json

from week11.src.store import Store, canonical_json, digest_json, safe_id, utc_now, _pid_alive
from .contracts import WEEK12_ROOT, inside_week12

TERMINAL = {"succeeded", "failed", "cancelled", "interrupted", "uncertain"}


class CarStore(Store):
    @staticmethod
    def _inside_week11(path: Path) -> None:
        inside_week12(path)

    def _initialize(self) -> None:
        super()._initialize()
        with self.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS car_launch_intents(task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),owner TEXT NOT NULL,created_at TEXT NOT NULL)")

    def reserve_launch(self, task_id: str, owner: str) -> dict[str, Any]:
        safe_id(owner, "worker owner")
        with self.transaction() as db:
            task = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if task is None or task["state"] != "queued":
                raise ValueError("task is not launchable")
            if db.execute("SELECT 1 FROM car_launch_intents WHERE task_id=?", (task_id,)).fetchone():
                raise ValueError("task launch already reserved")
            db.execute("INSERT INTO car_launch_intents VALUES(?,?,?)", (task_id, owner, utc_now()))
            db.execute("UPDATE tasks SET worker_owner=?,updated_at=? WHERE task_id=? AND state='queued'", (owner, utc_now(), task_id))
        return self.task(task_id)

    @staticmethod
    def _owned(db, task_id: str, owner: str, states: set[str]):
        safe_id(owner, "worker owner")
        row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        intent = db.execute("SELECT owner FROM car_launch_intents WHERE task_id=?", (task_id,)).fetchone()
        if row is None or row["worker_owner"] != owner or intent is None or intent["owner"] != owner or row["state"] not in states:
            raise ValueError("task owner or allowed prior state mismatch")
        return row

    def claim_task(self, task_id: str, owner: str, pid: int) -> dict[str, Any]:
        if type(pid) is not int or pid <= 0:
            raise ValueError("invalid worker pid")
        with self.transaction() as db:
            self._owned(db, task_id, owner, {"queued"})
            changed = db.execute("UPDATE tasks SET state='running',progress='validating car approval',worker_pid=?,updated_at=? WHERE task_id=? AND state='queued' AND worker_owner=?", (pid, utc_now(), task_id, owner)).rowcount
            if changed != 1:
                raise ValueError("task claim CAS failed")
        return self.task(task_id)

    def set_task_progress(self, task_id: str, progress: str, *, owner: str) -> None:
        with self.transaction() as db:
            self._owned(db, task_id, owner, {"running"})
            db.execute("UPDATE tasks SET progress=?,updated_at=? WHERE task_id=? AND worker_owner=? AND state='running'", (str(progress)[:500], utc_now(), task_id, owner))

    def stage_reference_result(self,task_id,*,owner,result):
        return self.stage_result(task_id,owner=owner,result=result)

    def stage_result(self,task_id,*,owner,result):
        with self.transaction() as db:
            row=self._owned(db,task_id,owner,{'running'})
            proposal = db.execute('SELECT spec_json FROM proposals WHERE proposal_id=?',(row['proposal_id'],)).fetchone()
            from .contracts import execution_route
            SUCCESS = execution_route(json.loads(proposal['spec_json']).get('profile')).SUCCESS
            if any(result.get(k)!=v for k,v in SUCCESS.items()):raise ValueError('only validated selected profile may await exit')
            db.execute("UPDATE tasks SET result_json=?,progress='evidence ready; awaiting actual worker exit',updated_at=? WHERE task_id=? AND worker_owner=? AND state='running'",
                (canonical_json(result),utc_now(),task_id,owner))

    def finish_task(self, task_id: str, state: str, *, owner: str, result: Any = None, error: str = "") -> dict[str, Any]:
        if state not in TERMINAL:
            raise ValueError("invalid terminal state")
        with self.transaction() as db:
            row = self._owned(db, task_id, owner, {"queued", "running", "cancel_requested"} | TERMINAL)
            proposal_row = db.execute("SELECT * FROM proposals WHERE proposal_id=?", (row["proposal_id"],)).fetchone()
            if proposal_row is None:
                raise ValueError("task proposal missing")
            spec = json.loads(proposal_row["spec_json"])
            if state == "succeeded":
                from .reference_profile import PROFILE
                from .eightwheel_profile import PROFILE as EIGHTWHEEL_PROFILE
                if spec.get("profile") in (PROFILE,EIGHTWHEEL_PROFILE,"REFERENCE_TEMPLATE_THREE_STEP"):
                    if spec.get("profile") == EIGHTWHEEL_PROFILE:
                        raise ValueError("eightwheel success requires atomic validated version publication")
                    from .contracts import execution_route
                    route = execution_route(spec.get("profile"))
                    SUCCESS, verify_manifest_binding = route.SUCCESS, route.verify_manifest_binding
                    from .service_adapter import _verify_manifest
                    proposal = dict(proposal_row)
                    proposal['spec'] = json.loads(proposal.pop('spec_json'))
                    proposal['display'] = json.loads(proposal.pop('display_json'))
                    proposal['confirmation'] = json.loads(proposal.pop('confirmation_json'))
                    task = self._task_row(row)
                    project = db.execute("SELECT * FROM projects WHERE project_id=?", (row["project_id"],)).fetchone()
                    session = db.execute("SELECT current_project_id FROM sessions WHERE session_id=?", (row["session_id"],)).fetchone()
                    if (proposal["session_id"] != row["session_id"] or not project or not session
                            or session["current_project_id"] != row["project_id"]
                            or project["revision"] != proposal["project_revision"]
                            or project["current_version_id"] != proposal["baseline_version_id"]):
                        raise ValueError("reference actual project/session/baseline mismatch")
                    if not isinstance(result,dict) or any(result.get(k)!=v for k,v in SUCCESS.items()):
                        raise ValueError("reference cannot complete with vehicle PASS or an incomplete tuple")
                    base = (WEEK12_ROOT/"projects"/row["project_id"]/"versions"/row["version_id"]).resolve()
                    manifest_path = inside_week12(result["outcome_manifest_path"])
                    manifest_path.relative_to(base)
                    manifest = _verify_manifest(manifest_path,result["outcome_manifest_sha256"],base)
                    verify_manifest_binding(manifest,result,task,proposal,owner)
                else:
                    raise ValueError("closed/unsupported vehicle completion; trusted failure unavailable")
            if row["state"] in TERMINAL:
                if row["state"] != state or row["result_json"] != (canonical_json(result) if result is not None else None) or (row["error"] or "") != error[:2000]:
                    raise ValueError("terminal task evidence cannot be overwritten")
            else:
                db.execute("UPDATE tasks SET state=?,progress=?,result_json=?,error=?,updated_at=? WHERE task_id=? AND worker_owner=? AND state IN ('queued','running','cancel_requested')", (state, state, canonical_json(result) if result is not None else None, error[:2000], utc_now(), task_id, owner))
                db.execute("UPDATE proposals SET state='executed',updated_at=? WHERE proposal_id=? AND state='approved'", (utc_now(), row["proposal_id"]))
        return self.task(task_id)

    def publish_version(self, task_id: str, manifest_sha256: str, directory: str, *, owner: str, result: dict[str, Any]) -> dict[str, Any]:
        """Validate final owned evidence and atomically publish after actual worker exit."""
        if (result.get("vehicle_task_status") != "PASS" or result.get("agent_execution_status") != "COMPLETED"
                or result.get("tool_status") != "PASS" or result.get("numerical_status") != "PASS"
                or result.get("evidence_status") != "COMPLETE"):
            raise ValueError("only fully validated car PASS is publishable")
        if not isinstance(manifest_sha256, str) or len(manifest_sha256) != 64:
            raise ValueError("invalid manifest digest")
        with self.transaction() as db:
            task = self._owned(db, task_id, owner, {"running"})
            expected = (WEEK12_ROOT / "projects" / task["project_id"] / "versions" / task["version_id"]).resolve()
            path = inside_week12(WEEK12_ROOT / directory)
            if path != expected:
                raise ValueError("published directory must be this task's reserved project version")
            proposal = db.execute("SELECT * FROM proposals WHERE proposal_id=?", (task["proposal_id"],)).fetchone()
            from .reference_profile import PROFILE
            if proposal is not None and json.loads(proposal["spec_json"]).get("profile") == PROFILE:
                raise ValueError("reference profile cannot publish a vehicle/current version")
            project = db.execute("SELECT * FROM projects WHERE project_id=?", (task["project_id"],)).fetchone()
            if proposal is None or project is None or proposal["state"] != "approved" or project["revision"] != proposal["project_revision"] or project["current_version_id"] != proposal["baseline_version_id"]:
                raise ValueError("approval or baseline changed before publication")
            if digest_json(json.loads(proposal["spec_json"])) != proposal["spec_digest"] or digest_json(json.loads(proposal["display_json"])) != proposal["displayed_digest"]:
                raise ValueError("proposal binding drifted")
            from .eightwheel_profile import PROFILE as EIGHTWHEEL_PROFILE
            if json.loads(proposal["spec_json"]).get("profile") != EIGHTWHEEL_PROFILE:
                raise ValueError("only explicit active eightwheel profile may publish")
            from .contracts import execution_route
            route = execution_route(EIGHTWHEEL_PROFILE)
            if (not isinstance(result,dict) or any(result.get(k)!=v for k,v in route.SUCCESS.items())
                    or result.get("published_version_id") is not None
                    or result.get("outcome_manifest_sha256") != manifest_sha256):
                raise ValueError("eightwheel final completion tuple/manifest mismatch")
            session = db.execute("SELECT current_project_id FROM sessions WHERE session_id=?",(task["session_id"],)).fetchone()
            if not session or session["current_project_id"] != task["project_id"] or proposal["session_id"] != task["session_id"]:
                raise ValueError("eightwheel final project/session mismatch")
            bound_proposal = dict(proposal)
            bound_proposal["spec"] = json.loads(bound_proposal.pop("spec_json"))
            bound_proposal["display"] = json.loads(bound_proposal.pop("display_json"))
            bound_proposal["confirmation"] = json.loads(bound_proposal.pop("confirmation_json"))
            from .service_adapter import _verify_manifest
            manifest_path = inside_week12(result["outcome_manifest_path"])
            manifest_path.relative_to(expected)
            manifest = _verify_manifest(manifest_path,manifest_sha256,expected)
            if (manifest_path.name != "eightwheel-outcome-manifest.json" or manifest.get("schema") != "v11-eightwheel-manifest-1"
                    or manifest.get("task_id") != task_id or manifest.get("project_id") != task["project_id"]):
                raise ValueError("eightwheel version manifest identity mismatch")
            route.verify_manifest_binding(manifest,result,self._task_row(task),bound_proposal,owner,require_worker_exit=True)
            now = utc_now()
            db.execute("INSERT INTO versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (task["version_id"], task["project_id"], int(task["version_id"][1:]), task_id, proposal["spec_digest"], proposal["baseline_version_id"], proposal["operation"], "passed", "ready", directory, manifest_sha256, now))
            db.execute("UPDATE projects SET revision=revision+1,current_version_id=?,selected_version_id=?,updated_at=? WHERE project_id=? AND revision=?", (task["version_id"], task["version_id"], now, task["project_id"], project["revision"]))
            db.execute("UPDATE proposals SET state='executed',updated_at=? WHERE proposal_id=?", (now, proposal["proposal_id"]))
            db.execute("UPDATE proposals SET state='stale',updated_at=? WHERE project_id=? AND proposal_id<>? AND state IN ('awaiting_confirmation','approved')", (now, task["project_id"], proposal["proposal_id"]))
            persisted = {**result, "published_version_id": task["version_id"]}
            db.execute("UPDATE tasks SET state='succeeded',progress='succeeded',result_json=?,error='',updated_at=? WHERE task_id=? AND worker_owner=? AND state='running'", (canonical_json(persisted), now, task_id, owner))
        return self.version(task["project_id"], task["version_id"])

    def recover_orphans(self) -> list[str]:
        """No new server owns old workers; uncertain cleanup freezes new execution."""
        affected = []
        with self.transaction() as db:
            rows = db.execute("SELECT * FROM tasks WHERE state IN ('queued','running','cancel_requested')").fetchall()
            for row in rows:
                if row["worker_pid"] and _pid_alive(row["worker_pid"]):
                    continue
                state = "uncertain" if row["worker_owner"] else "interrupted"
                db.execute("UPDATE tasks SET state=?,progress=?,error=?,updated_at=? WHERE task_id=? AND state=?", (state, state, "server restart: prior launch/descendant cleanup is not proven", utc_now(), row["task_id"], row["state"]))
                affected.append(row["task_id"])
        return affected


class ReadOnlyCarStore(CarStore):
    """Reuse Store readers; SQLite itself rejects every write, without migration."""
    def __init__(self, db_path):
        self.db_path = inside_week12(db_path)
        if not self.db_path.is_file():
            raise ValueError('historical database does not exist')

    def _connect(self):
        import sqlite3
        connection = sqlite3.connect(self.db_path.as_uri()+'?mode=ro',uri=True,timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        return connection

    def ensure_session(self, session_id):
        safe_id(session_id,'session_id')
        with self.reading() as db:
            row = db.execute('SELECT * FROM sessions WHERE session_id=?',(session_id,)).fetchone()
        if row is None:
            raise ValueError('historical session does not exist')
        return dict(row)
