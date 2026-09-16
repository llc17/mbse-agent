"""Owned subprocess execution and publication for V10."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
from typing import Any

from week9.src.hard_gate import REQUIRED_HARD_CHECK_TYPES
from week9.src.schemas import CheckStatus, RunResult
from week11.src.generation import execute_spec
from week11.src.spec import spec_digest, validate_spec
from week11.src.store import Store, WEEK11_ROOT, canonical_json, safe_id


WORKER_TIMEOUT_S = 300


class WorkerManager:
    """Launch workers and kill only subprocess handles owned by this instance."""

    def __init__(self, store: Store, *, timeout_s: int = WORKER_TIMEOUT_S) -> None:
        self.store = store
        self.timeout_s = timeout_s
        self.owner = "owner_" + secrets.token_hex(16)
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._lock = threading.Lock()

    def start(self, task_id: str) -> None:
        safe_id(task_id, "task_id")
        task = self.store.task(task_id)
        project_dir = WEEK11_ROOT / "projects" / task["project_id"]
        log_dir = project_dir / "task-logs"
        log_path = log_dir / f"{task_id}.log"
        command = [
            sys.executable,
            "-B",
            "-m",
            "week11.src.worker",
            "--db",
            str(self.store.db_path),
            "--task",
            task_id,
            "--owner",
            self.owner,
        ]
        kwargs: dict[str, Any] = {"cwd": str(WEEK11_ROOT.parent), "stdin": subprocess.DEVNULL}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            with log_path.open("ab") as log:
                process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, **kwargs)
        except (OSError, subprocess.SubprocessError) as exc:
            self.store.finish_task(
                task_id,
                "failed",
                error=f"worker failed to start: {type(exc).__name__}: {exc}",
            )
            return
        with self._lock:
            self._processes[task_id] = process
        threading.Thread(target=self._monitor, args=(task_id, process), daemon=True).start()

    def _monitor(self, task_id: str, process: subprocess.Popen[bytes]) -> None:
        try:
            return_code = process.wait(timeout=self.timeout_s)
            current = self.store.task(task_id)
            if current["state"] in {"queued", "running", "cancel_requested"}:
                try:
                    self.store.finish_task(
                        task_id,
                        "uncertain",
                        error=f"worker exited with code {return_code} without a committed terminal state; descendant cleanup is unknown",
                    )
                except ValueError:
                    pass
        except subprocess.TimeoutExpired:
            self.store.request_cancel(task_id)
            cleaned = self._terminate_owned(task_id, process)
            try:
                self.store.finish_task(
                    task_id,
                    "failed" if cleaned else "uncertain",
                    error=(f"worker exceeded {self.timeout_s:g} seconds and its owned process tree was terminated" if cleaned else f"worker exceeded {self.timeout_s:g} seconds; process-tree termination was not proven"),
                )
            except ValueError:
                pass
        finally:
            with self._lock:
                if self._processes.get(task_id) is process:
                    self._processes.pop(task_id, None)

    def cancel(self, task_id: str) -> dict[str, Any]:
        task = self.store.request_cancel(task_id)
        if task["state"] != "cancel_requested":
            return task
        with self._lock:
            process = self._processes.get(task_id)
        if process is None or task.get("worker_owner") not in {None, self.owner}:
            return self.store.finish_task(
                task_id,
                "uncertain",
                error="worker is not owned by this service instance; cleanup was not attempted",
            )
        cleaned = self._terminate_owned(task_id, process)
        return self.store.finish_task(
            task_id,
            "cancelled" if cleaned else "uncertain",
            error="" if cleaned else "owned process-tree termination was not proven",
        )

    def _terminate_owned(self, task_id: str, process: subprocess.Popen[bytes]) -> bool:
        with self._lock:
            if self._processes.get(task_id) is not process:
                return False
        if process.poll() is not None:
            return False
        try:
            if os.name == "nt":
                completed = subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    check=False,
                )
                if completed.returncode == 0:
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                    if process.poll() is not None:
                        return True
                return self._terminate_owned_with_psutil(process)
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            process.wait(timeout=10)
            return process.poll() is not None
        except (OSError, subprocess.SubprocessError):
            return False

    @staticmethod
    def _terminate_owned_with_psutil(process: subprocess.Popen[bytes]) -> bool:
        """Fallback: kill descendants of the exact owned Popen PID and verify exit."""
        try:
            import psutil

            parent = psutil.Process(process.pid)
            if process.poll() is not None:
                return False
            parent.suspend()
            descendants = parent.children(recursive=True)
            for child in reversed(descendants):
                child.kill()
            parent.kill()
            gone, alive = psutil.wait_procs([parent, *descendants], timeout=5)
            process.wait(timeout=5)
            return not alive and process.poll() is not None
        except Exception:
            return False

    def shutdown(self) -> None:
        with self._lock:
            task_ids = list(self._processes)
        for task_id in task_ids:
            try:
                self.cancel(task_id)
            except (OSError, TypeError, ValueError):
                try:
                    self.store.finish_task(
                        task_id,
                        "uncertain",
                        error="service shutdown could not prove owned process-tree cleanup",
                    )
                except (OSError, TypeError, ValueError):
                    pass


def execute_task(db_path: str | Path, task_id: str, owner: str) -> dict[str, Any]:
    store = Store(db_path)
    task = store.claim_task(task_id, owner, os.getpid())
    result: dict[str, Any] | None = None
    try:
        proposal = store.proposal(task["proposal_id"])
        project = store.project(task["project_id"])
        _verify_execution_binding(task, proposal, project)
        checked = validate_spec(proposal["spec"])
        if checked.get("ready") is not True or spec_digest(checked["spec"]) != proposal["spec_digest"]:
            raise ValueError("approved specification failed worker revalidation")
        store.set_task_progress(task_id, "running certified generation")
        version_root = (WEEK11_ROOT / "projects" / project["project_id"] / "versions").resolve()
        version_dir = (version_root / task["version_id"]).resolve()
        try:
            version_dir.relative_to(version_root)
        except ValueError as exc:
            raise ValueError("generated version path escapes its project") from exc
        result = execute_spec(
            checked["spec"],
            version_dir,
            project_id=project["project_id"],
            run_id=task_id,
        )
        store.set_task_progress(task_id, "verifying technical and evidence artifacts")
        _verify_generation_result(result, version_dir, project["project_id"], task_id, proposal["spec_digest"])
        confirmation_raw = (canonical_json(proposal["confirmation"]) + "\n").encode("utf-8")
        with (version_dir / "confirmation.json").open("xb") as handle:
            handle.write(confirmation_raw)
        manifest_raw = _write_version_manifest(version_dir)
        relative = str(version_dir.relative_to(WEEK11_ROOT)).replace("\\", "/")
        version = store.publish_version(task_id, hashlib.sha256(manifest_raw).hexdigest(), relative)
        return {"task_id": task_id, "state": "succeeded", "version": version}
    except BaseException as exc:
        current = store.task(task_id)
        if current["state"] in {"running", "queued", "cancel_requested"}:
            terminal = "cancelled" if current["state"] == "cancel_requested" else "failed"
            diagnostic = None
            if result is not None:
                diagnostic = {
                    "generation_status": result.get("status"),
                    "technical": result.get("hard_gate"),
                    "evidence": result.get("evidence"),
                }
            store.finish_task(task_id, terminal, result=diagnostic, error=f"{type(exc).__name__}: {exc}")
        raise


def _verify_execution_binding(task: dict[str, Any], proposal: dict[str, Any], project: dict[str, Any]) -> None:
    confirmation = proposal.get("confirmation")
    if proposal["state"] != "approved" or not isinstance(confirmation, dict):
        raise ValueError("worker requires an approved proposal")
    if proposal["project_id"] != task["project_id"] or project["project_id"] != task["project_id"]:
        raise ValueError("task, proposal, and project disagree")
    if project["revision"] != proposal["project_revision"] or project["current_version_id"] != proposal["baseline_version_id"]:
        raise ValueError("approved baseline changed before execution")
    for name in ("proposal_id", "project_id", "baseline_version_id", "project_revision", "spec_digest", "displayed_digest", "operation"):
        if confirmation.get(name) != proposal.get(name):
            raise ValueError(f"confirmation does not bind {name}")


def _verify_generation_result(result: dict[str, Any], version_dir: Path, project_id: str, run_id: str, expected_spec_digest: str) -> None:
    if result.get("status") != "succeeded":
        raise ValueError("technical generation did not succeed")
    gate = result.get("hard_gate", {})
    evidence = result.get("evidence", {})
    if gate.get("passed") is not True or gate.get("overall_success") is not True:
        raise ValueError("original technical hard gate did not pass")
    if evidence.get("complete") is not True or evidence.get("trace_indexed") is not True:
        raise ValueError("trace evidence indexing is incomplete")
    run = RunResult.from_dict(json.loads((version_dir / "run-result.json").read_text(encoding="utf-8")))
    if run.version != "v8" or run.overall_success is not True or run.case_id != project_id or run.run_id != run_id:
        raise ValueError("persisted RunResult is not a passing frozen V8 result")
    indexed = {item.check_type: item for item in run.checks}
    if any(name not in indexed or not indexed[name].required or indexed[name].status is not CheckStatus.PASS for name in REQUIRED_HARD_CHECK_TYPES):
        raise ValueError("persisted RunResult does not pass every original hard check")
    trace = indexed.get("traceability_integrity")
    if trace is None or not trace.required or trace.status is not CheckStatus.PASS or run.traceability is None or not run.traceability.integrity_passed:
        raise ValueError("persisted RunResult traceability is incomplete")
    binding = json.loads((version_dir / "generation-binding.json").read_text(encoding="utf-8"))
    if binding.get("spec_digest") != expected_spec_digest or binding.get("project_id") != project_id or binding.get("run_id") != run_id:
        raise ValueError("generation binding does not match approved execution")
    generation_manifest = json.loads((version_dir / "manifest.json").read_text(encoding="utf-8"))
    if generation_manifest.get("schema_version") != "v10-generation-manifest-1":
        raise ValueError("generation manifest schema mismatch")
    _verify_files(version_dir, generation_manifest.get("files"))


def _verify_files(root: Path, files: Any) -> None:
    if not isinstance(files, dict) or not files:
        raise ValueError("manifest file set is empty")
    for name, record in files.items():
        if not isinstance(name, str) or Path(name).name != name or not isinstance(record, dict):
            raise ValueError("unsafe manifest entry")
        candidate = (root / name).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError as exc:
            raise ValueError("manifest entry escapes version directory") from exc
        raw = candidate.read_bytes()
        if hashlib.sha256(raw).hexdigest() != record.get("sha256") or len(raw) != record.get("bytes"):
            raise ValueError("generation manifest hash mismatch")


def _write_version_manifest(version_dir: Path) -> bytes:
    names = [
        "spec.json", "confirmation.json", "requirement.txt", "model.sysml", "model.mo",
        "run-result.json", "generation-binding.json", "trace-evidence.json", "manifest.json",
    ]
    files: dict[str, dict[str, Any]] = {}
    for name in names:
        raw = (version_dir / name).read_bytes()
        files[name] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    generation_manifest_sha = files["manifest.json"]["sha256"]
    payload = {
        "schema_version": "v10-version-manifest-1",
        "generation_manifest_sha256": generation_manifest_sha,
        "confirmation_sha256": files["confirmation.json"]["sha256"],
        "spec_sha256": files["spec.json"]["sha256"],
        "technical_status": "passed",
        "evidence_status": "ready",
        "immutable": True,
        "files": files,
    }
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    with (version_dir / "version-manifest.json").open("xb") as handle:
        handle.write(raw)
    return raw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="V10 owned generation worker")
    parser.add_argument("--db", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--owner", required=True)
    args = parser.parse_args(argv)
    try:
        execute_task(args.db, args.task, args.owner)
        return 0
    except BaseException as exc:
        print(f"worker failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["WORKER_TIMEOUT_S", "WorkerManager", "execute_task", "main"]
