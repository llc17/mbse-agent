from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from week9.src.schemas import RunResult
from week11.src.cli import CLI
from week11.src.service import Service
from week11.src.store import Store, WEEK11_ROOT, canonical_json, digest_json
from week11.src.worker import WorkerManager, execute_task


def rc_spec(source_id: str, text: str, *, voltage: float = 1.0, cutoff: float = 1000.0) -> dict:
    formula = "C = 1 / (2 * pi * R * fc)"
    return {
        "schema_version": "v10-spec-1",
        "template_id": "rc_filter",
        "sources": [{"id": source_id, "text": text}],
        "parameters": [
            {"name": "resistance", "value": 1000.0, "unit": "Ohm", "source_id": source_id, "source_text": "R=1000 Ohm", "origin": "direct"},
            {"name": "cutoff_frequency", "value": cutoff, "unit": "Hz", "source_id": source_id, "source_text": f"fc={cutoff:g} Hz", "origin": "user"},
            {"name": "input_voltage", "value": voltage, "unit": "V", "source_id": source_id, "source_text": f"Vin={voltage:g} V", "origin": "user"},
        ],
        "stimulus": {"kind": "step_voltage", "source_id": source_id, "source_text": "step at 0 s"},
        "derivations": [{"target": "capacitance", "formula": formula, "input_names": ["resistance", "cutoff_frequency"], "source_id": source_id, "source_text": formula}],
        "constraints": ["fixed_certified_template", "fixed_stimulus", "rc_time_constant_check"],
        "assumptions": ["certified zero initial capacitor voltage"],
    }


def source_text(*, voltage: float = 1.0, cutoff: float = 1000.0) -> str:
    return f"RC request R=1000 Ohm, fc={cutoff:g} Hz, Vin={voltage:g} V, step at 0 s; C = 1 / (2 * pi * R * fc)"


class ImmediateWorker:
    """Publish the two frozen public RC snapshots used by these lifecycle tests."""

    def __init__(self, store: Store, *, fail: bool = False) -> None:
        self.store = store
        self.fail = fail
        self.started: list[str] = []

    def start(self, task_id: str) -> None:
        self.started.append(task_id)
        task = self.store.claim_task(task_id, "test-owner", 424242)
        if self.fail:
            self.store.finish_task(task_id, "failed", error="injected technical failure")
            return
        proposal = self.store.proposal(task["proposal_id"])
        root = WEEK11_ROOT / "projects" / task["project_id"] / "versions" / task["version_id"]
        root.mkdir(parents=True, exist_ok=False)
        normalized = {item["name"]: item for item in proposal["spec"]["normalized_parameters"]}
        public_snapshot = "changed" if normalized["input_voltage"]["value"] == 6.0 else "baseline"
        public_run_path = WEEK11_ROOT.parent / "week10" / "results" / "trace-stage2-20260909" / public_snapshot / "run-result.json"
        run = RunResult.from_dict(json.loads(public_run_path.read_text(encoding="utf-8")))
        run.case_id = task["project_id"]
        run.run_id = task["task_id"]
        run_raw = (json.dumps(run.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
        payloads = {
            "spec.json": (json.dumps(proposal["spec"], ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(),
            "confirmation.json": (canonical_json(proposal["confirmation"]) + "\n").encode(),
            "run-result.json": run_raw,
            "requirement.txt": run.artifacts["requirement"].content.encode(),
            "model.sysml": run.artifacts["sysml"].content.encode(),
            "model.mo": run.artifacts["modelica"].content.encode(),
            "generation-binding.json": b"{}\n",
            "trace-evidence.json": b"{}\n",
            "manifest.json": b"{}\n",
        }
        for name, raw in payloads.items():
            (root / name).write_bytes(raw)
        manifest = {
            "schema_version": "v10-version-manifest-1",
            "files": {name: {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)} for name, raw in payloads.items()},
        }
        raw = (json.dumps(manifest, sort_keys=True) + "\n").encode()
        (root / "version-manifest.json").write_bytes(raw)
        relative = str(root.relative_to(WEEK11_ROOT)).replace("\\", "/")
        self.store.publish_version(task_id, hashlib.sha256(raw).hexdigest(), relative)

    def cancel(self, task_id: str) -> dict:
        self.store.request_cancel(task_id)
        return self.store.finish_task(task_id, "cancelled")


class DeferredWorker:
    def __init__(self) -> None:
        self.started: list[str] = []

    def start(self, task_id: str) -> None:
        self.started.append(task_id)

    def cancel(self, task_id: str) -> dict:
        raise AssertionError("not used")


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="wp3-service-", dir=WEEK11_ROOT)
        self.db = Path(self.temp.name) / "state.sqlite"
        self.store = Store(self.db)
        self.worker = ImmediateWorker(self.store)
        self.service = Service(self.db, "session-a", worker_manager=self.worker)
        self.project = self.service.project_create("RC project")["project"]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def prepare(self, *, voltage: float = 5.0, cutoff: float = 1000.0) -> dict:
        text = source_text(voltage=voltage, cutoff=cutoff)
        source = self.service.record_source(text)
        result = self.service.proposal_prepare(rc_spec(source["source_id"], text, voltage=voltage, cutoff=cutoff))
        self.assertTrue(result["ok"], result)
        return result["proposal"]

    def approve_and_run(self, proposal: dict, request_id: str) -> dict:
        self.service.confirm_local(proposal["proposal_id"], proposal["displayed_digest"])
        return self.service.execution_start(proposal["proposal_id"], request_id)["task"]

    def test_forged_source_and_unconfirmed_execution_are_rejected(self) -> None:
        text = source_text()
        with self.assertRaisesRegex(ValueError, "source"):
            self.service.proposal_prepare(rc_spec("src_forged", text))
        proposal = self.prepare()
        with self.assertRaisesRegex(ValueError, "approved"):
            self.service.execution_start(proposal["proposal_id"], "request-a")

    def test_confirmation_is_bound_and_replacement_invalidates_old_approval(self) -> None:
        first = self.prepare()
        with self.assertRaisesRegex(ValueError, "digest"):
            self.service.confirm_local(first["proposal_id"], "0" * 64)
        self.service.confirm_local(first["proposal_id"], first["displayed_digest"])
        second = self.prepare(voltage=1.2)
        with self.assertRaisesRegex(ValueError, "approved"):
            self.service.execution_start(first["proposal_id"], "stale-request")
        self.assertEqual("awaiting_confirmation", second["state"])

    def test_idempotent_request_and_conflicting_request_id(self) -> None:
        proposal = self.prepare()
        first = self.approve_and_run(proposal, "same-request")
        repeated = self.service.execution_start(proposal["proposal_id"], "same-request")
        self.assertTrue(repeated["reused"])
        self.assertEqual(first["task_id"], repeated["task"]["task_id"])
        proposal2 = self.prepare(voltage=1.1)
        self.service.confirm_local(proposal2["proposal_id"], proposal2["displayed_digest"])
        with self.assertRaisesRegex(ValueError, "different content"):
            self.service.execution_start(proposal2["proposal_id"], "same-request")

    def test_cross_project_objects_require_explicit_open(self) -> None:
        proposal = self.prepare()
        other = self.service.project_create("other")["project"]
        with self.assertRaisesRegex(ValueError, "another project"):
            self.service.proposal_get(proposal["proposal_id"])
        self.service.project_open(self.project["project_id"])
        self.assertEqual(proposal["proposal_id"], self.service.proposal_get(proposal["proposal_id"])["proposal"]["proposal_id"])
        self.assertNotEqual(other["project_id"], self.project["project_id"])

    def test_create_then_two_rc_modifications_use_successive_baselines(self) -> None:
        first = self.prepare()
        first_task = self.approve_and_run(first, "create-request")
        self.assertEqual("v000001", first_task["version_id"])
        voltage = self.prepare(voltage=6.0)
        self.assertEqual("v000001", voltage["baseline_version_id"])
        self.assertEqual(["input_voltage"], [item["name"] for item in voltage["display"]["changes"]])
        voltage_task = self.approve_and_run(voltage, "voltage-request")
        self.assertEqual("v000002", voltage_task["version_id"])
        cutoff = self.prepare(voltage=6.0, cutoff=2000.0)
        self.assertEqual("v000002", cutoff["baseline_version_id"])
        self.assertEqual({"cutoff_frequency", "capacitance"}, {item["name"] for item in cutoff["display"]["changes"]})
        cutoff_task = self.approve_and_run(cutoff, "cutoff-request")
        self.assertEqual("v000003", cutoff_task["version_id"])
        versions = self.service.versions_list()["versions"]
        self.assertEqual("v000003", versions["current_version_id"])
        self.assertEqual(3, len(versions["items"]))

    def test_modify_display_binds_verified_public_baseline_evidence(self) -> None:
        first = self.prepare()
        self.approve_and_run(first, "public-baseline")
        changed = self.prepare(voltage=6.0)

        evidence = changed["display"]["baseline_evidence"]
        self.assertEqual("v000001", evidence["baseline_version_id"])
        self.assertEqual("v10-proposal-baseline-evidence-1", evidence["schema_version"])
        self.assertEqual(1, len(evidence["changed_fields"]))
        field = evidence["changed_fields"][0]
        self.assertEqual("parameter:input_voltage", field["target_id"])
        self.assertEqual({"sysml", "modelica"}, {item["layer"] for item in field["declarations"]})
        self.assertTrue(all(item["exact_selector"] for item in field["declarations"]))
        self.assertIn("cross_model_consistency", {item["check_type"] for item in field["explicit_checks"]})
        self.assertIn("physical_behavior", {item["check_type"] for item in field["rerun_required_checks"]})
        self.assertFalse(field["numerical_behavior_guarantee"])
        self.assertEqual(changed["displayed_digest"], digest_json(changed["display"]))
        display_without_evidence = dict(changed["display"])
        display_without_evidence.pop("baseline_evidence")
        self.assertNotEqual(changed["displayed_digest"], digest_json(display_without_evidence))

        stdout = StringIO()
        with redirect_stdout(stdout):
            CLI._print_proposal(changed)
        rendered = stdout.getvalue()
        for declaration in field["declarations"]:
            self.assertIn(declaration["layer"], rendered)
            self.assertIn(declaration["exact_selector"], rendered)
        for check in field["explicit_checks"]:
            self.assertIn(check["check_type"], rendered)
        for check in field["rerun_required_checks"]:
            self.assertIn(check["check_type"], rendered)

    def test_modify_rejects_tampered_baseline_before_display(self) -> None:
        first = self.prepare()
        task = self.approve_and_run(first, "tamper-baseline")
        version = self.service.store.version(self.project["project_id"], task["version_id"])
        model_path = WEEK11_ROOT / version["directory"] / "model.sysml"
        model_path.write_text(model_path.read_text(encoding="utf-8") + "\n// tampered\n", encoding="utf-8")
        text = source_text(voltage=6.0)
        source = self.service.record_source(text)
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.service.proposal_prepare(rc_spec(source["source_id"], text, voltage=6.0))

    def test_failed_generation_preserves_current_version(self) -> None:
        first = self.prepare()
        self.approve_and_run(first, "create-good")
        self.service.worker_manager = ImmediateWorker(self.service.store, fail=True)
        changed = self.prepare(voltage=1.5)
        task = self.approve_and_run(changed, "modify-fails")
        self.assertEqual("failed", self.service.execution_status(task["task_id"])["task"]["state"])
        self.assertEqual("v000001", self.service.project_status()["status"]["project"]["current_version_id"])

    def test_index_failure_does_not_publish(self) -> None:
        deferred = DeferredWorker()
        service = Service(self.db, "session-a", worker_manager=deferred)
        proposal = self.prepare()
        self.service.confirm_local(proposal["proposal_id"], proposal["displayed_digest"])
        task = service.execution_start(proposal["proposal_id"], "index-failure")["task"]
        fake_result = {"status": "succeeded", "hard_gate": {"passed": True, "overall_success": True}, "evidence": {"complete": False, "trace_indexed": False}}
        with patch("week11.src.worker.execute_spec", return_value=fake_result):
            with self.assertRaisesRegex(ValueError, "indexing"):
                execute_task(self.db, task["task_id"], "test-worker")
        self.assertEqual("failed", service.execution_status(task["task_id"])["task"]["state"])
        result = service.execution_status(task["task_id"])["task"]["result"]
        self.assertTrue(result["technical"]["passed"])
        self.assertFalse(result["evidence"]["complete"])
        self.assertIsNone(service.project_status()["status"]["project"]["current_version_id"])

    def test_recovery_distinguishes_queued_and_unknown_running(self) -> None:
        deferred = DeferredWorker()
        service = Service(self.db, "session-a", worker_manager=deferred)
        proposal = self.prepare()
        self.service.confirm_local(proposal["proposal_id"], proposal["displayed_digest"])
        task = service.execution_start(proposal["proposal_id"], "queued-orphan")["task"]
        affected = service.store.recover_orphans()
        self.assertIn(task["task_id"], affected)
        self.assertEqual("interrupted", service.store.task(task["task_id"])["state"])

    def test_historical_selection_is_read_only_and_latest_remains_current(self) -> None:
        first = self.prepare()
        self.approve_and_run(first, "first-version")
        second = self.prepare(voltage=6.0)
        self.approve_and_run(second, "second-version")
        selected = self.service.versions_select("v000001")["project"]
        self.assertEqual("v000002", selected["current_version_id"])
        self.assertEqual("v000001", selected["selected_version_id"])
        text = source_text(voltage=5.0)
        source = self.service.record_source(text)
        with self.assertRaisesRegex(ValueError, "read-only"):
            self.service.proposal_prepare(rc_spec(source["source_id"], text, voltage=5.0))
        latest = self.service.versions_select("v000002")["project"]
        self.assertEqual("v000002", latest["current_version_id"])
        prepared = self.service.proposal_prepare(rc_spec(source["source_id"], text, voltage=5.0))
        self.assertTrue(prepared["ok"])

    def test_uncertain_orphan_blocks_new_execution(self) -> None:
        deferred = DeferredWorker()
        service = Service(self.db, "session-a", worker_manager=deferred)
        first = self.prepare()
        self.service.confirm_local(first["proposal_id"], first["displayed_digest"])
        task = service.execution_start(first["proposal_id"], "uncertain-first")["task"]
        service.store.claim_task(task["task_id"], "lost-owner", 999999)
        service.store.recover_orphans()
        self.assertEqual("uncertain", service.store.task(task["task_id"])["state"])
        other = self.prepare()
        self.service.confirm_local(other["proposal_id"], other["displayed_digest"])
        with self.assertRaisesRegex(ValueError, "active"):
            service.execution_start(other["proposal_id"], "uncertain-second")

    def test_worker_spawn_failure_is_terminal_failed(self) -> None:
        manager = WorkerManager(self.service.store)
        service = Service(self.db, "session-a", worker_manager=manager)
        proposal = self.prepare()
        self.service.confirm_local(proposal["proposal_id"], proposal["displayed_digest"])
        with patch("week11.src.worker.subprocess.Popen", side_effect=OSError("spawn denied")):
            task = service.execution_start(proposal["proposal_id"], "spawn-failure")["task"]
        self.assertEqual("failed", task["state"])
        self.assertIn("worker failed to start", task["error"])

    def test_cancel_request_prevents_publication_and_terminal_reversal(self) -> None:
        deferred = DeferredWorker()
        service = Service(self.db, "session-a", worker_manager=deferred)
        proposal = self.prepare()
        self.service.confirm_local(proposal["proposal_id"], proposal["displayed_digest"])
        task = service.execution_start(proposal["proposal_id"], "cancel-race")["task"]
        service.store.claim_task(task["task_id"], "test-owner", 999999)
        service.store.request_cancel(task["task_id"])
        with self.assertRaisesRegex(ValueError, "publishable"):
            service.store.publish_version(task["task_id"], "a" * 64, "projects/unused")
        service.store.finish_task(task["task_id"], "cancelled")
        with self.assertRaisesRegex(ValueError, "cannot be overwritten"):
            service.store.finish_task(task["task_id"], "succeeded")

    def test_source_history_is_scoped_to_session_and_project(self) -> None:
        source = self.service.record_source(source_text())
        other = self.service.project_create("other project")["project"]
        self.service.record_source(source_text(voltage=2.0))
        first_history = self.service.store.latest_sources("session-a", self.project["project_id"])
        self.assertEqual([source["source_id"]], [item["source_id"] for item in first_history])
        other_history = self.service.store.latest_sources("session-a", other["project_id"])
        self.assertEqual(1, len(other_history))
        self.assertNotEqual(source["source_id"], other_history[0]["source_id"])

    def test_owned_harmless_process_tree_cancel_is_verified(self) -> None:
        self._check_owned_tree_cleanup(timeout=False)

    def test_owned_worker_timeout_cleans_tree_and_marks_failed(self) -> None:
        self._check_owned_tree_cleanup(timeout=True)

    def _check_owned_tree_cleanup(self, *, timeout: bool) -> None:
        """Exercise actual Windows tree cleanup without OpenModelica or network."""
        if os.name != "nt":
            self.skipTest("Windows process-tree cleanup test")
        import psutil

        deferred = DeferredWorker()
        service = Service(self.db, "session-a", worker_manager=deferred)
        proposal = self.prepare()
        self.service.confirm_local(proposal["proposal_id"], proposal["displayed_digest"])
        task = service.execution_start(proposal["proposal_id"], "owned-tree-cancel")["task"]
        command = [
            sys.executable,
            "-B",
            "-c",
            (
                "import subprocess,sys,time; "
                "c=subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(30)'], "
                "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                "print(c.pid,flush=True); time.sleep(30)"
            ),
        ]
        parent = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        child_pid = None
        try:
            child_pid = int(parent.stdout.readline().decode().strip())
            manager = WorkerManager(service.store, timeout_s=0.1 if timeout else 300)
            service.store.claim_task(task["task_id"], manager.owner, parent.pid)
            with manager._lock:
                manager._processes[task["task_id"]] = parent
            if timeout:
                manager._monitor(task["task_id"], parent)
                outcome = service.store.task(task["task_id"])
            else:
                outcome = manager.cancel(task["task_id"])
            self.assertEqual("failed" if timeout else "cancelled", outcome["state"], "actual lifecycle acceptance needs proven cleanup")
            print(f"owned-tree timeout={timeout}; state={outcome['state']}; cleanup_proven=True", flush=True)
            if outcome["state"] in {"cancelled", "failed"}:
                self.assertIsNotNone(parent.poll())
                for _ in range(30):
                    if not psutil.pid_exists(child_pid):
                        break
                    time.sleep(0.1)
                self.assertFalse(psutil.pid_exists(child_pid), "claimed-clean child is still alive")
        finally:
            # Only these two PIDs were launched by this test.
            if child_pid is not None and psutil.pid_exists(child_pid):
                psutil.Process(child_pid).kill()
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=5)
            if parent.stdout is not None:
                parent.stdout.close()


if __name__ == "__main__":
    unittest.main()
