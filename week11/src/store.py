"""Transactional V10 project, proposal, task, and version storage."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator, Mapping
import uuid

from week11.src.recovery import capture_recovery_context, validate_recovery_context


STORE_SCHEMA = "v10-store-1"
WEEK11_ROOT = Path(__file__).resolve().parents[1]
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def safe_id(value: str, label: str = "identifier") -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value) or ".." in value:
        raise ValueError(f"unsafe {label}")
    return value


class Store:
    """Small SQLite store; all state transitions use immediate transactions."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).resolve()
        self._inside_week11(self.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @staticmethod
    def _inside_week11(path: Path) -> None:
        try:
            path.resolve().relative_to(WEEK11_ROOT.resolve())
        except ValueError as exc:
            raise ValueError("store path must be inside week11") from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def reading(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.transaction() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions(
                  session_id TEXT PRIMARY KEY,
                  current_project_id TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS projects(
                  project_id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  revision INTEGER NOT NULL DEFAULT 0,
                  current_version_id TEXT,
                  selected_version_id TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sources(
                  source_id TEXT PRIMARY KEY,
                  session_id TEXT NOT NULL,
                  project_id TEXT NOT NULL,
                  text TEXT NOT NULL,
                  text_sha256 TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  FOREIGN KEY(session_id) REFERENCES sessions(session_id),
                  FOREIGN KEY(project_id) REFERENCES projects(project_id)
                );
                CREATE TABLE IF NOT EXISTS proposals(
                  proposal_id TEXT PRIMARY KEY,
                  session_id TEXT NOT NULL,
                  project_id TEXT NOT NULL,
                  operation TEXT NOT NULL,
                  baseline_version_id TEXT,
                  project_revision INTEGER NOT NULL,
                  spec_digest TEXT NOT NULL,
                  spec_json TEXT NOT NULL,
                  display_json TEXT NOT NULL,
                  displayed_digest TEXT NOT NULL,
                  state TEXT NOT NULL,
                  confirmation_json TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  FOREIGN KEY(session_id) REFERENCES sessions(session_id),
                  FOREIGN KEY(project_id) REFERENCES projects(project_id)
                );
                CREATE TABLE IF NOT EXISTS tasks(
                  task_id TEXT PRIMARY KEY,
                  session_id TEXT NOT NULL,
                  project_id TEXT NOT NULL,
                  proposal_id TEXT NOT NULL UNIQUE,
                  request_id TEXT NOT NULL,
                  request_sha256 TEXT NOT NULL,
                  version_id TEXT NOT NULL,
                  state TEXT NOT NULL,
                  progress TEXT NOT NULL,
                  worker_pid INTEGER,
                  worker_owner TEXT,
                  result_json TEXT,
                  error TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(session_id, request_id),
                  FOREIGN KEY(proposal_id) REFERENCES proposals(proposal_id)
                );
                CREATE TABLE IF NOT EXISTS versions(
                  version_id TEXT NOT NULL,
                  project_id TEXT NOT NULL,
                  sequence INTEGER NOT NULL,
                  run_id TEXT NOT NULL,
                  spec_digest TEXT NOT NULL,
                  baseline_version_id TEXT,
                  operation TEXT NOT NULL,
                  technical_status TEXT NOT NULL,
                  evidence_status TEXT NOT NULL,
                  directory TEXT NOT NULL,
                  manifest_sha256 TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(project_id, version_id),
                  UNIQUE(project_id, sequence),
                  UNIQUE(project_id, run_id)
                );
                CREATE TABLE IF NOT EXISTS recovery_context(
                  task_id TEXT PRIMARY KEY,
                  schema_version TEXT NOT NULL,
                  identity_scheme TEXT NOT NULL,
                  machine_digest TEXT NOT NULL,
                  boot_identifier TEXT NOT NULL,
                  context_digest TEXT NOT NULL,
                  captured_at TEXT NOT NULL,
                  FOREIGN KEY(task_id) REFERENCES tasks(task_id)
                );
                CREATE TABLE IF NOT EXISTS recovery_audit(
                  audit_id TEXT PRIMARY KEY,
                  task_id TEXT NOT NULL,
                  prior_state TEXT NOT NULL,
                  resulting_state TEXT NOT NULL,
                  stored_context_digest TEXT NOT NULL,
                  observed_context_digest TEXT NOT NULL,
                  inspection_digest TEXT NOT NULL,
                  recovered_by_session_id TEXT NOT NULL,
                  recovered_at TEXT NOT NULL,
                  FOREIGN KEY(task_id) REFERENCES tasks(task_id)
                );
                CREATE TRIGGER IF NOT EXISTS recovery_context_no_update
                BEFORE UPDATE ON recovery_context BEGIN SELECT RAISE(ABORT, 'recovery context is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS recovery_context_no_delete
                BEFORE DELETE ON recovery_context BEGIN SELECT RAISE(ABORT, 'recovery context is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS recovery_audit_no_update
                BEFORE UPDATE ON recovery_audit BEGIN SELECT RAISE(ABORT, 'recovery audit is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS recovery_audit_no_delete
                BEFORE DELETE ON recovery_audit BEGIN SELECT RAISE(ABORT, 'recovery audit is immutable'); END;
                """
            )

    def ensure_session(self, session_id: str) -> dict[str, Any]:
        safe_id(session_id, "session_id")
        now = utc_now()
        with self.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO sessions(session_id,created_at,updated_at) VALUES(?,?,?)",
                (session_id, now, now),
            )
            row = db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        return dict(row)

    def create_project(self, session_id: str, name: str) -> dict[str, Any]:
        self.ensure_session(session_id)
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
            raise ValueError("project name must be 1..120 characters")
        project_id = "prj_" + uuid.uuid4().hex
        now = utc_now()
        with self.transaction() as db:
            db.execute(
                "INSERT INTO projects(project_id,name,created_at,updated_at) VALUES(?,?,?,?)",
                (project_id, name.strip(), now, now),
            )
            db.execute(
                "UPDATE sessions SET current_project_id=?,updated_at=? WHERE session_id=?",
                (project_id, now, session_id),
            )
        return self.project(project_id)

    def list_projects(self) -> list[dict[str, Any]]:
        with self.reading() as db:
            rows = db.execute("SELECT * FROM projects ORDER BY created_at,project_id").fetchall()
        return [dict(row) for row in rows]

    def project(self, project_id: str) -> dict[str, Any]:
        safe_id(project_id, "project_id")
        with self.reading() as db:
            row = db.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if row is None:
            raise ValueError("unknown project")
        return dict(row)

    def current_project(self, session_id: str) -> dict[str, Any] | None:
        session = self.ensure_session(session_id)
        project_id = session.get("current_project_id")
        return None if project_id is None else self.project(project_id)

    def open_project(self, session_id: str, project_id: str) -> dict[str, Any]:
        self.ensure_session(session_id)
        project = self.project(project_id)
        now = utc_now()
        with self.transaction() as db:
            db.execute(
                "UPDATE sessions SET current_project_id=?,updated_at=? WHERE session_id=?",
                (project_id, now, session_id),
            )
        return project

    def record_source(self, session_id: str, text: str) -> dict[str, Any]:
        """Trusted CLI-only source entry.  This method is never exposed as MCP."""
        project = self.current_project(session_id)
        if project is None:
            raise ValueError("no project selected")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("source text must not be empty")
        source_id = "src_" + uuid.uuid4().hex
        now = utc_now()
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self.transaction() as db:
            db.execute(
                "INSERT INTO sources VALUES(?,?,?,?,?,?)",
                (source_id, session_id, project["project_id"], text, sha, now),
            )
        return {"source_id": source_id, "text": text, "text_sha256": sha, "project_id": project["project_id"]}

    def sources(self, session_id: str, project_id: str, ids: list[str]) -> list[dict[str, Any]]:
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("source ids must be a non-empty unique list")
        for item in ids:
            safe_id(item, "source_id")
        placeholders = ",".join("?" for _ in ids)
        with self.reading() as db:
            rows = db.execute(
                f"SELECT * FROM sources WHERE session_id=? AND project_id=? AND source_id IN ({placeholders})",
                (session_id, project_id, *ids),
            ).fetchall()
        by_id = {row["source_id"]: dict(row) for row in rows}
        if set(by_id) != set(ids):
            raise ValueError("source is unknown or belongs to another session/project")
        return [by_id[item] for item in ids]

    def insert_proposal(self, record: Mapping[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self.transaction() as db:
            db.execute(
                "UPDATE proposals SET state='stale',updated_at=? WHERE project_id=? AND state IN ('awaiting_confirmation','approved')",
                (now, record["project_id"]),
            )
            db.execute(
                """INSERT INTO proposals(proposal_id,session_id,project_id,operation,
                baseline_version_id,project_revision,spec_digest,spec_json,display_json,
                displayed_digest,state,confirmation_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    record["proposal_id"], record["session_id"], record["project_id"],
                    record["operation"], record.get("baseline_version_id"),
                    record["project_revision"], record["spec_digest"],
                    canonical_json(record["spec"]), canonical_json(record["display"]),
                    record["displayed_digest"], "awaiting_confirmation", None, now, now,
                ),
            )
        return self.proposal(str(record["proposal_id"]))

    def proposal(self, proposal_id: str) -> dict[str, Any]:
        safe_id(proposal_id, "proposal_id")
        with self.reading() as db:
            row = db.execute("SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
        if row is None:
            raise ValueError("unknown proposal")
        result = dict(row)
        result["spec"] = json.loads(result.pop("spec_json"))
        result["display"] = json.loads(result.pop("display_json"))
        result["confirmation"] = json.loads(result.pop("confirmation_json")) if result.get("confirmation_json") else None
        return result

    def pending_proposals(self, session_id: str, project_id: str) -> list[dict[str, Any]]:
        """Discover actionable proposals without granting or changing approval."""
        with self.reading() as db:
            rows = db.execute(
                """SELECT p.proposal_id,p.operation,p.baseline_version_id,p.state,p.created_at
                FROM proposals p JOIN projects j ON j.project_id=p.project_id
                WHERE p.session_id=? AND p.project_id=?
                  AND p.state IN ('awaiting_confirmation','approved')
                  AND p.project_revision=j.revision
                  AND p.baseline_version_id IS j.current_version_id
                ORDER BY p.created_at,p.proposal_id""",
                (session_id, project_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def recovery_tasks(self) -> list[dict[str, Any]]:
        """Read database-wide blockers, including ones in another selected project."""
        with self.reading() as db:
            rows = db.execute(
                """SELECT task_id,project_id,state,progress,worker_pid,error,created_at
                FROM tasks WHERE state IN ('queued','running','cancel_requested','uncertain')
                ORDER BY created_at,task_id"""
            ).fetchall()
        return [dict(row) for row in rows]

    def cancel_proposal(self, proposal_id: str, session_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.transaction() as db:
            row = db.execute("SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
            if row is None or row["session_id"] != session_id:
                raise ValueError("unknown proposal")
            if row["state"] == "executed":
                raise ValueError("executed proposal cannot be cancelled")
            db.execute("UPDATE proposals SET state='cancelled',updated_at=? WHERE proposal_id=?", (now, proposal_id))
        return self.proposal(proposal_id)

    def confirm(self, proposal_id: str, displayed_digest: str, session_id: str) -> dict[str, Any]:
        """Trusted CLI-only confirmation transition; intentionally absent from MCP."""
        now = utc_now()
        with self.transaction() as db:
            row = db.execute("SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
            if row is None or row["session_id"] != session_id:
                raise ValueError("unknown proposal")
            if row["state"] not in {"awaiting_confirmation", "approved"}:
                raise ValueError("proposal is not awaiting confirmation")
            if displayed_digest != row["displayed_digest"]:
                raise ValueError("displayed proposal digest mismatch")
            session = db.execute("SELECT current_project_id FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if session is None or session["current_project_id"] != row["project_id"]:
                raise ValueError("proposal project is not selected in this session")
            project = db.execute("SELECT * FROM projects WHERE project_id=?", (row["project_id"],)).fetchone()
            if project is None or project["revision"] != row["project_revision"] or project["current_version_id"] != row["baseline_version_id"]:
                raise ValueError("proposal baseline or project revision is stale")
            if digest_json(json.loads(row["display_json"])) != row["displayed_digest"] or digest_json(json.loads(row["spec_json"])) != row["spec_digest"]:
                raise ValueError("stored proposal binding is inconsistent")
            if row["state"] == "approved":
                unchanged = json.loads(row["confirmation_json"] or "null")
                if not isinstance(unchanged, dict) or unchanged.get("displayed_digest") != displayed_digest:
                    raise ValueError("stored confirmation is inconsistent")
            else:
                confirmation = {
                    "schema_version": "v10-confirmation-1",
                    "proposal_id": proposal_id,
                    "project_id": row["project_id"],
                    "operation": row["operation"],
                    "baseline_version_id": row["baseline_version_id"],
                    "project_revision": row["project_revision"],
                    "spec_digest": row["spec_digest"],
                    "displayed_digest": displayed_digest,
                    "confirmed_at": now,
                    "channel": "trusted_local_cli",
                }
                db.execute(
                    "UPDATE proposals SET state='approved',confirmation_json=?,updated_at=? WHERE proposal_id=?",
                    (canonical_json(confirmation), now, proposal_id),
                )
        return self.proposal(proposal_id)

    def create_task(self, proposal_id: str, session_id: str, request_id: str) -> tuple[dict[str, Any], bool]:
        safe_id(request_id, "request_id")
        safe_id(proposal_id, "proposal_id")
        request_sha = digest_json({"proposal_id": proposal_id})
        now = utc_now()
        with self.transaction() as db:
            proposal_row = db.execute("SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
            if proposal_row is None:
                raise ValueError("unknown proposal")
            proposal = dict(proposal_row)
            existing = db.execute("SELECT * FROM tasks WHERE session_id=? AND request_id=?", (session_id, request_id)).fetchone()
            if existing is not None:
                if existing["request_sha256"] != request_sha:
                    raise ValueError("request_id was already used with different content")
                return self._task_row(existing), False
            if proposal["session_id"] != session_id or proposal["state"] != "approved":
                raise ValueError("proposal is not approved for this session")
            session = db.execute("SELECT current_project_id FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            project = db.execute("SELECT * FROM projects WHERE project_id=?", (proposal["project_id"],)).fetchone()
            confirmation = json.loads(proposal["confirmation_json"] or "null")
            if session is None or session["current_project_id"] != proposal["project_id"]:
                raise ValueError("proposal project is not selected in this session")
            if project is None or project["revision"] != proposal["project_revision"] or project["current_version_id"] != proposal["baseline_version_id"]:
                raise ValueError("proposal baseline or revision is stale")
            if not isinstance(confirmation, dict) or confirmation.get("spec_digest") != proposal["spec_digest"] or confirmation.get("displayed_digest") != proposal["displayed_digest"]:
                raise ValueError("proposal confirmation binding is invalid")
            if digest_json(json.loads(proposal["spec_json"])) != proposal["spec_digest"] or digest_json(json.loads(proposal["display_json"])) != proposal["displayed_digest"]:
                raise ValueError("stored proposal binding is inconsistent")
            if db.execute("SELECT 1 FROM tasks WHERE proposal_id=?", (proposal_id,)).fetchone():
                raise ValueError("proposal already has an execution task")
            active = db.execute("SELECT 1 FROM tasks WHERE state IN ('queued','running','cancel_requested','uncertain')").fetchone()
            if active:
                raise ValueError("another execution is active")
            recovery = validate_recovery_context(capture_recovery_context())
            sequence = db.execute("SELECT COALESCE(MAX(CAST(substr(version_id,2) AS INTEGER)),0)+1 FROM tasks WHERE project_id=?", (proposal["project_id"],)).fetchone()[0]
            version_id = f"v{int(sequence):06d}"
            task_id = "task_" + uuid.uuid4().hex
            db.execute(
                """INSERT INTO tasks(task_id,session_id,project_id,proposal_id,request_id,
                request_sha256,version_id,state,progress,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, session_id, proposal["project_id"], proposal_id, request_id,
                 request_sha, version_id, "queued", "queued", now, now),
            )
            context_payload = {
                "task_id": task_id,
                "schema_version": recovery["schema_version"],
                "identity_scheme": recovery["identity_scheme"],
                "machine_digest": recovery["machine_digest"],
                "boot_identifier": recovery["boot_identifier"],
                "captured_at": now,
            }
            context_digest = digest_json(context_payload)
            db.execute(
                """INSERT INTO recovery_context(task_id,schema_version,identity_scheme,
                machine_digest,boot_identifier,context_digest,captured_at)
                VALUES(?,?,?,?,?,?,?)""",
                (task_id, recovery["schema_version"], recovery["identity_scheme"],
                 recovery["machine_digest"], recovery["boot_identifier"], context_digest, now),
            )
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._task_row(row), True

    def inspect_task_recovery(self, task_id: str) -> dict[str, Any]:
        """Inspect reboot proof without changing task or process state."""
        safe_id(task_id, "task_id")
        with self.reading() as db:
            task = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            context = db.execute("SELECT * FROM recovery_context WHERE task_id=?", (task_id,)).fetchone()
        if task is None:
            raise ValueError("unknown task")
        try:
            observed = validate_recovery_context(capture_recovery_context())
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return {
                "task_id": task_id,
                "project_id": task["project_id"],
                "status": "unknown",
                "eligible": False,
                "reason": f"current host/boot identity unavailable: {exc}",
                "digest": None,
                "stored_context_digest": None if context is None else context["context_digest"],
                "observed_context_digest": None,
            }
        inspection = self._recovery_inspection(
            dict(task), None if context is None else dict(context), observed
        )
        public = {
            key: inspection[key]
            for key in (
                "task_id", "project_id", "status", "eligible", "reason", "digest",
                "stored_context_digest", "observed_context_digest",
            )
        }
        return public

    @staticmethod
    def _recovery_inspection(
        task: Mapping[str, Any], context: Mapping[str, Any] | None, observed: Mapping[str, str]
    ) -> dict[str, Any]:
        status = str(task["state"])
        base: dict[str, Any] = {
            "task_id": task["task_id"],
            "project_id": task["project_id"],
            "status": status,
            "eligible": False,
            "stored_context_digest": None,
            "observed_context_digest": digest_json(dict(observed)),
        }
        if context is None:
            return {**base, "reason": "task predates persisted reboot identity", "digest": None}
        stored_payload = {
            "task_id": task["task_id"],
            "schema_version": context["schema_version"],
            "identity_scheme": context["identity_scheme"],
            "machine_digest": context["machine_digest"],
            "boot_identifier": context["boot_identifier"],
            "captured_at": context["captured_at"],
        }
        if digest_json(stored_payload) != context["context_digest"]:
            return {**base, "reason": "stored reboot identity is inconsistent", "digest": None}
        base["stored_context_digest"] = context["context_digest"]
        if status not in {"uncertain", "running", "cancel_requested"}:
            return {**base, "reason": "task state is not recoverable after reboot", "digest": None}
        if context["schema_version"] != observed["schema_version"] or context["identity_scheme"] != observed["identity_scheme"]:
            return {**base, "reason": "host identity scheme changed", "digest": None}
        if context["machine_digest"] != observed["machine_digest"]:
            return {**base, "reason": "task belongs to a different machine identity", "digest": None}
        if context["boot_identifier"] == observed["boot_identifier"]:
            return {**base, "reason": "task was created during the current boot", "digest": None}
        observed_digest = base["observed_context_digest"]
        inspection_digest = digest_json({
            "schema_version": "v10-recovery-inspection-1",
            "task_id": task["task_id"],
            "session_id": task["session_id"],
            "project_id": task["project_id"],
            "task_state": status,
            "stored_context_digest": context["context_digest"],
            "observed_context_digest": observed_digest,
        })
        return {
            **base,
            "eligible": True,
            "reason": "same machine and a different current OS boot are proven",
            "digest": inspection_digest,
        }

    def resolve_after_reboot(self, task_id: str, session_id: str, expected_digest: str) -> dict[str, Any]:
        """Mark one proven post-reboot orphan interrupted; never run or publish it."""
        safe_id(task_id, "task_id")
        safe_id(session_id, "session_id")
        if not isinstance(expected_digest, str) or len(expected_digest) != 64:
            raise ValueError("recovery inspection digest is invalid")
        try:
            int(expected_digest, 16)
        except ValueError as exc:
            raise ValueError("recovery inspection digest is invalid") from exc
        try:
            observed = validate_recovery_context(capture_recovery_context())
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ValueError(f"current host/boot identity unavailable: {exc}") from exc
        now = utc_now()
        with self.transaction() as db:
            task_row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if task_row is None:
                raise ValueError("unknown task")
            task = dict(task_row)
            if task["session_id"] != session_id:
                raise ValueError("task belongs to another session")
            session = db.execute("SELECT current_project_id FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if session is None or session["current_project_id"] != task["project_id"]:
                raise ValueError("task project is not selected in this session")
            context_row = db.execute("SELECT * FROM recovery_context WHERE task_id=?", (task_id,)).fetchone()
            inspection = self._recovery_inspection(task, None if context_row is None else dict(context_row), observed)
            if not inspection["eligible"]:
                raise ValueError(str(inspection["reason"]))
            if inspection["digest"] != expected_digest:
                raise ValueError("recovery inspection changed; inspect the task again")
            db.execute(
                "UPDATE tasks SET state='interrupted',progress='interrupted',updated_at=? WHERE task_id=?",
                (now, task_id),
            )
            db.execute(
                """INSERT INTO recovery_audit(audit_id,task_id,prior_state,resulting_state,
                stored_context_digest,observed_context_digest,inspection_digest,
                recovered_by_session_id,recovered_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                ("recovery_" + uuid.uuid4().hex, task_id, task["state"], "interrupted",
                 inspection["stored_context_digest"], inspection["observed_context_digest"],
                 expected_digest, session_id, now),
            )
            result = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._task_row(result)

    def task(self, task_id: str) -> dict[str, Any]:
        safe_id(task_id, "task_id")
        with self.reading() as db:
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError("unknown task")
        return self._task_row(row)

    @staticmethod
    def _task_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["result"] = json.loads(result.pop("result_json")) if result.get("result_json") else None
        return result

    def claim_task(self, task_id: str, owner: str, pid: int) -> dict[str, Any]:
        now = utc_now()
        with self.transaction() as db:
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None or row["state"] != "queued":
                raise ValueError("task is not claimable")
            db.execute("UPDATE tasks SET state='running',progress='validating approval and baseline',worker_pid=?,worker_owner=?,updated_at=? WHERE task_id=?", (pid, owner, now, task_id))
        return self.task(task_id)

    def set_task_progress(self, task_id: str, progress: str) -> None:
        with self.transaction() as db:
            db.execute("UPDATE tasks SET progress=?,updated_at=? WHERE task_id=? AND state='running'", (progress[:500], utc_now(), task_id))

    def finish_task(self, task_id: str, state: str, *, result: Any = None, error: str = "") -> dict[str, Any]:
        if state not in {"succeeded", "failed", "cancelled", "interrupted", "uncertain"}:
            raise ValueError("invalid terminal task state")
        already_terminal = False
        with self.transaction() as db:
            row = db.execute("SELECT state FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError("unknown task")
            if row["state"] in {"succeeded", "failed", "cancelled", "interrupted", "uncertain"}:
                if row["state"] != state:
                    raise ValueError("terminal task state cannot be overwritten")
                already_terminal = True
            if not already_terminal:
                db.execute(
                    "UPDATE tasks SET state=?,progress=?,result_json=?,error=?,updated_at=? WHERE task_id=?",
                    (state, state, canonical_json(result) if result is not None else None, error[:2000], utc_now(), task_id),
                )
        return self.task(task_id)

    def request_cancel(self, task_id: str) -> dict[str, Any]:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError("unknown task")
            if row["state"] in {"succeeded", "failed", "cancelled", "interrupted", "uncertain"}:
                return self._task_row(row)
            db.execute("UPDATE tasks SET state='cancel_requested',progress='cancellation requested',updated_at=? WHERE task_id=?", (utc_now(), task_id))
        return self.task(task_id)

    def recover_orphans(self) -> list[str]:
        affected: list[str] = []
        with self.transaction() as db:
            rows = db.execute("SELECT task_id,state FROM tasks WHERE state IN ('queued','running','cancel_requested')").fetchall()
            for row in rows:
                full = db.execute("SELECT worker_pid FROM tasks WHERE task_id=?", (row["task_id"],)).fetchone()
                if row["state"] in {"running", "cancel_requested"} and _pid_alive(full["worker_pid"]):
                    continue
                state = "interrupted" if row["state"] == "queued" else "uncertain"
                db.execute("UPDATE tasks SET state=?,progress=?,error=?,updated_at=? WHERE task_id=?", (state, state, "service restarted; prior worker completion was not proven", utc_now(), row["task_id"]))
                affected.append(row["task_id"])
        return affected

    def version(self, project_id: str, version_id: str) -> dict[str, Any]:
        safe_id(version_id, "version_id")
        with self.reading() as db:
            row = db.execute("SELECT * FROM versions WHERE project_id=? AND version_id=?", (project_id, version_id)).fetchone()
        if row is None:
            raise ValueError("unknown version")
        return dict(row)

    def versions(self, project_id: str) -> list[dict[str, Any]]:
        with self.reading() as db:
            rows = db.execute("SELECT * FROM versions WHERE project_id=? ORDER BY sequence", (project_id,)).fetchall()
        return [dict(row) for row in rows]

    def publish_version(self, task_id: str, manifest_sha256: str, directory: str) -> dict[str, Any]:
        now = utc_now()
        with self.transaction() as db:
            task = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            proposal = db.execute("SELECT * FROM proposals WHERE proposal_id=?", (task["proposal_id"],)).fetchone() if task else None
            project = db.execute("SELECT * FROM projects WHERE project_id=?", (task["project_id"],)).fetchone() if task else None
            if task is None or proposal is None or project is None or task["state"] != "running":
                raise ValueError("task is not publishable")
            if proposal["state"] != "approved" or proposal["project_revision"] != project["revision"] or proposal["baseline_version_id"] != project["current_version_id"]:
                raise ValueError("approval or baseline changed before publication")
            sequence = int(task["version_id"][1:])
            db.execute(
                "INSERT INTO versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (task["version_id"], task["project_id"], sequence, task["task_id"], proposal["spec_digest"], proposal["baseline_version_id"], proposal["operation"], "passed", "ready", directory, manifest_sha256, now),
            )
            db.execute("UPDATE projects SET revision=revision+1,current_version_id=?,selected_version_id=?,updated_at=? WHERE project_id=?", (task["version_id"], task["version_id"], now, task["project_id"]))
            db.execute("UPDATE proposals SET state='executed',updated_at=? WHERE proposal_id=?", (now, proposal["proposal_id"]))
            db.execute("UPDATE proposals SET state='stale',updated_at=? WHERE project_id=? AND proposal_id<>? AND state IN ('awaiting_confirmation','approved')", (now, task["project_id"], proposal["proposal_id"]))
            db.execute("UPDATE tasks SET state='succeeded',progress='succeeded',result_json=?,error='',updated_at=? WHERE task_id=?", (canonical_json({"version_id": task["version_id"], "technical_status": "passed", "evidence_status": "ready"}), now, task_id))
        return self.version(task["project_id"], task["version_id"])

    def select_version(self, project_id: str, version_id: str) -> dict[str, Any]:
        self.version(project_id, version_id)
        with self.transaction() as db:
            db.execute("UPDATE projects SET selected_version_id=?,revision=revision+1,updated_at=? WHERE project_id=?", (version_id, utc_now(), project_id))
            db.execute("UPDATE proposals SET state='stale',updated_at=? WHERE project_id=? AND state IN ('awaiting_confirmation','approved')", (utc_now(), project_id))
        return self.project(project_id)

    def tasks_for_project(self, project_id: str) -> list[dict[str, Any]]:
        with self.reading() as db:
            rows = db.execute("SELECT * FROM tasks WHERE project_id=? ORDER BY created_at,task_id", (project_id,)).fetchall()
        return [self._task_row(row) for row in rows]

    def latest_sources(self, session_id: str, project_id: str, limit: int = 8) -> list[dict[str, Any]]:
        """Read only this session/project's recent authentic user inputs."""
        safe_id(session_id, "session_id")
        safe_id(project_id, "project_id")
        if type(limit) is not int or not 1 <= limit <= 32:
            raise ValueError("source history limit must be 1..32")
        with self.reading() as db:
            rows = db.execute(
                "SELECT source_id,text,text_sha256,created_at FROM sources WHERE session_id=? AND project_id=? ORDER BY created_at DESC,source_id DESC LIMIT ?",
                (session_id, project_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]


__all__ = ["STORE_SCHEMA", "Store", "WEEK11_ROOT", "canonical_json", "digest_json", "safe_id", "utc_now"]


def _pid_alive(pid: Any) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False
