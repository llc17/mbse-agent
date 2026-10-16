"""Owned asynchronous V10 worker pattern adapted to week12 car outcomes."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any

from week11.src.worker import WorkerManager, _verify_execution_binding
from week11.src.store import canonical_json, digest_json, safe_id
from .contracts import WEEK12_ROOT, PHYSICAL_SHA, validate_spec, authorization, inside_week12, profile_module, execution_route
from .store_adapter import CarStore, TERMINAL
from .service_adapter import CarService, _verify_manifest

WORKER_TIMEOUT_S = 1860


class CarWorkerManager(WorkerManager):
    def __init__(self, store: CarStore, *, timeout_s=None, accountant=None, profile=None):
        self.profile = profile_module(profile).PROFILE
        if timeout_s is None:
            from .eightwheel_profile import PROFILE as EIGHTWHEEL_PROFILE
            timeout_s = 4200 if self.profile == EIGHTWHEEL_PROFILE else WORKER_TIMEOUT_S
        super().__init__(store, timeout_s=timeout_s)
        if accountant is None:
            accountant = execution_route(self.profile)
        self.accountant = accountant
        self._monitors = {}

    def start(self, task_id):
        safe_id(task_id, "task_id")
        authorization(self.profile)
        existing = self.store.task(task_id)
        if self.store.proposal(existing["proposal_id"])["spec"].get("profile") != self.profile:
            raise ValueError("worker manager profile mismatch")
        task = self.store.reserve_launch(task_id, self.owner)
        parent_token = None
        started = time.monotonic()
        try:
            parent_token = self.accountant.reserve_parent("car-worker:" + task_id)
            log_dir = inside_week12(WEEK12_ROOT / "projects" / task["project_id"] / "task-logs")
            log_dir.mkdir(parents=True, exist_ok=True)
            command = [sys.executable, "-B", "-m", "week12.src.car_domain.worker_adapter", "--db", str(self.store.db_path), "--task", task_id, "--owner", self.owner, "--parent-token", parent_token, "--profile", self.profile]
            kwargs = {"cwd": str(WEEK12_ROOT.parent), "stdin": subprocess.DEVNULL}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            with (log_dir / (task_id + ".log")).open("ab") as log:
                process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, **kwargs)
        except Exception as exc:
            # This manager owns the durable launch intent even if Popen fails.
            self.store.finish_task(task_id, "failed", owner=self.owner, error="worker launch failed: " + type(exc).__name__)
            if parent_token is not None:
                self.accountant.finish_parent(parent_token, time.monotonic() - started, status="FAILED_START")
            return
        with self._lock:
            self._processes[task_id] = process
        monitor = threading.Thread(target=self._monitor_car, args=(task_id, process, parent_token, started), daemon=True)
        with self._lock:
            self._monitors[task_id] = monitor
        monitor.start()

    def _monitor_car(self, task_id, process, token, started):
        status='UNKNOWN';accounted=False
        try:
            code=process.wait(timeout=self.timeout_s)
            with self.accountant.transaction() as (_,s):
                parent=next(p for p in s['parents'] if p['token']==token)
                parent.update(actual_exit_code=code,launch_pid=process.pid,actual_exit_utc=self.accountant.stamp())
                if code!=0:s.update(status='STOPPED_WORKER_NONZERO',stop_reason='actual owned worker nonzero exit')
            status='RETURNED' if code==0 else 'FAILED_PROCESS'
            self.accountant.finish_parent(token,time.monotonic()-started,status=status);accounted=True
            current=self.store.task(task_id)
            if current['state'] not in TERMINAL:
                pending=current.get('result') or {}
                SUCCESS = self.accountant.SUCCESS
                if code==0 and all(pending.get(k)==v for k,v in SUCCESS.items()):
                    from .eightwheel_profile import PROFILE as EIGHTWHEEL_PROFILE
                    if self.profile == EIGHTWHEEL_PROFILE:
                        directory = (WEEK12_ROOT/'projects'/current['project_id']/'versions'/current['version_id']).relative_to(WEEK12_ROOT).as_posix()
                        self.store.publish_version(task_id,pending['outcome_manifest_sha256'],directory,owner=self.owner,result=pending)
                    else:
                        self.store.finish_task(task_id,'succeeded',owner=self.owner,result=pending)
                else:self.store.finish_task(task_id,'failed',owner=self.owner,result=pending or None,error='actual worker exit/evidence failed')
        except subprocess.TimeoutExpired:
            self.store.request_cancel(task_id);cleaned=self._terminate_owned(task_id,process)
            status='TIMEOUT_CLEANED' if cleaned else 'UNKNOWN'
            self.store.finish_task(task_id,'failed' if cleaned else 'uncertain',owner=self.owner,error='worker deadline/cleanup')
        except BaseException as exc:
            current=self.store.task(task_id)
            if current['state'] not in TERMINAL:
                self.store.finish_task(task_id,'failed',owner=self.owner,result=current.get('result'),error='worker final commit failed: '+type(exc).__name__)
            status='UNKNOWN'
        finally:
            if not accounted:self.accountant.finish_parent(token,time.monotonic()-started,status=status)
            with self._lock:
                if self._processes.get(task_id) is process:self._processes.pop(task_id,None)

    def cancel(self, task_id):
        task = self.store.task(task_id)
        if task["state"] in TERMINAL:
            return task
        # A different service can request cancellation, but cannot assert cleanup.
        task = self.store.request_cancel(task_id)
        with self._lock:
            process = self._processes.get(task_id)
        if process is None or task.get("worker_owner") != self.owner:
            return task
        cleaned = self._terminate_owned(task_id, process)
        try:
            return self.store.finish_task(task_id, "cancelled" if cleaned else "uncertain", owner=self.owner,
                                          error="" if cleaned else "owned process cleanup unproven")
        except ValueError:
            return self.store.task(task_id)

    def shutdown(self):
        with self._lock:
            ids = list(self._processes)
        for task_id in ids:
            try:
                self.cancel(task_id)
            except (OSError, ValueError):
                try:
                    self.store.finish_task(task_id, "uncertain", owner=self.owner, error="shutdown cleanup unproven")
                except ValueError:
                    pass
        # Commit each launched parent's accounting before the MCP parent exits.
        with self._lock:
            monitors = list(self._monitors.values())
        for monitor in monitors:
            monitor.join(timeout=5)


def _write_once(path, payload):
    raw = (canonical_json(payload) + "\n").encode("utf-8")
    with path.open("xb") as handle:
        handle.write(raw)
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def execute_task(db_path, task_id, owner) -> dict[str, Any]:
    store = CarStore(db_path)
    # Claim failure has no terminal fallback: a rejected process owns no task.
    task = store.claim_task(task_id, owner, os.getpid())
    result = None
    output_dir = None
    try:
        proposal = store.proposal(task["proposal_id"])
        project = store.project(task["project_id"])
        _verify_execution_binding(task, proposal, project)
        profile = proposal["spec"].get("profile")
        route = execution_route(profile)
        auth = authorization(profile)
        if proposal["display"].get("execution_scope_id") != auth["scope_id"]:
            raise ValueError("car execution scope mismatch")
        checked = validate_spec(proposal["spec"])
        if not checked.get("ready") or digest_json(checked["spec"]) != proposal["spec_digest"]:
            raise ValueError("car specification revalidation failed")
        CarService(db_path, task["session_id"], worker_manager=object(), profile=profile)._verify_sources(project["project_id"], checked["spec"])
        output_dir = inside_week12(WEEK12_ROOT / "projects" / project["project_id"] / "versions" / task["version_id"])
        output_dir.mkdir(parents=True, exist_ok=False)
        spec_file = _write_once(output_dir / "spec.json", checked["spec"])
        confirmation_file = _write_once(output_dir / "confirmation.json", proposal["confirmation"])
        store.set_task_progress(task_id, "generating and running approved car evidence", owner=owner)
        from .reference_profile import PROFILE as REFERENCE_PROFILE
        route.verify_confirmation(proposal, task["session_id"])
        runner = route.execute_reference_task if profile in (REFERENCE_PROFILE,"REFERENCE_TEMPLATE_THREE_STEP") else route.execute_eightwheel_task
        result = runner(task, proposal, output_dir, owner)
        expected_schema = "v11-reference-outcome-1" if profile in (REFERENCE_PROFILE,"REFERENCE_TEMPLATE_THREE_STEP") else "v11-car-outcome-1"
        if not isinstance(result,dict) or result.get("schema_version") != expected_schema:
            raise ValueError("unsupported selected profile outcome")
        manifest_path = Path(result["outcome_manifest_path"])
        if not manifest_path.is_absolute():
            manifest_path = output_dir / manifest_path
        manifest_path = inside_week12(manifest_path)
        manifest_path.relative_to(output_dir)
        manifest = _verify_manifest(manifest_path, result["outcome_manifest_sha256"], output_dir)
        route.verify_manifest_binding(manifest, result, task, proposal, owner, require_worker_exit=False)
        complete = all(result.get(k)==v for k,v in route.SUCCESS.items())
        if complete:store.stage_result(task_id,owner=owner,result=result)
        else:store.finish_task(task_id,"failed",owner=owner,result=result)
        return store.task(task_id)
    except BaseException as exc:
        current = store.task(task_id)
        if current["state"] not in TERMINAL:
            diagnostic = {"schema_version": "v11-car-outcome-1", "agent_execution_status": "BLOCKED", "vehicle_task_status": "UNKNOWN",
                          "tool_status": "TOOL_FAIL", "numerical_status": "NOT_RUN", "evidence_status": "INCOMPLETE_OR_UNTRUSTED", "published_version_id": None,
                          "classification": "TOOL_FAIL", "error_type": type(exc).__name__}
            if output_dir is not None:
                files = {}
                for name in ("spec.json", "confirmation.json"):
                    path = output_dir / name
                    if path.is_file():
                        content = path.read_bytes()
                        files[name] = {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
                fallback = output_dir / "worker-error-manifest.json"
                if files and not fallback.exists():
                    record = _write_once(fallback, {**diagnostic, "task_id": task_id, "project_id": task["project_id"], "proposal_id": task["proposal_id"], "owner": owner, "files": files})
                    diagnostic.update(outcome_manifest_path=str(fallback), outcome_manifest_sha256=record["sha256"])
            state = "cancelled" if current["state"] == "cancel_requested" else "failed"
            store.finish_task(task_id, state, owner=owner, result=diagnostic, error="car worker failed: " + type(exc).__name__)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="V11 owned car worker")
    parser.add_argument("--db", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--parent-token", required=True)
    parser.add_argument("--profile")
    args = parser.parse_args(argv)
    selected = profile_module(args.profile).PROFILE
    store = CarStore(args.db)
    task = store.task(args.task)
    if store.proposal(task["proposal_id"])["spec"].get("profile") != selected:
        raise ValueError("worker process explicit profile mismatch")
    execution_route(selected).claim_parent(args.parent_token, "car-worker:"+args.task)
    try:
        execute_task(args.db, args.task, args.owner)
        return 0
    except BaseException as exc:
        print("car worker stopped: " + type(exc).__name__, file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
