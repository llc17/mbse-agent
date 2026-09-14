from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from week9.src.hard_gate import REQUIRED_HARD_CHECK_TYPES
import week10.experiments.run_projection_diagnostic as executor
from week10.experiments.run_projection_diagnostic import (
    ExecutionBlocked,
    IMPLEMENTATION_FILES,
    _consume_receipt,
    _run_worker_process,
    _validate_receipt,
    execute_projection_diagnostic,
)
from week10.src.projection_contract import V8_SOURCE_HASHES
from week10.src.online_generation import ONLINE_PROMPT_SHA256, PROMPT_SHA256


ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_PATH = ROOT / "week10/configs/projection_diagnostic_protocol_v1.json"
SUITE_PATH = ROOT / "week10/configs/development_diagnostic_suite.json"


def _canonical(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: dict) -> None:
    path.write_bytes(_canonical(value))


def _public_result(row: dict, *, kind: str = "success") -> dict:
    success = kind == "success" and row["positive"]
    if kind == "schema":
        stage = "generation_error"
        reason = "projection_schema_invalid"
        disposition = "infrastructure_error"
        response_valid = False
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    elif kind in {"api", "unknown_usage"}:
        stage = "generation_error"
        reason = "api_transport_error"
        disposition = "infrastructure_error"
        response_valid = False
        usage = None
    elif success:
        stage = "pipeline_complete"
        reason = "accepted"
        disposition = "accepted"
        response_valid = True
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    else:
        stage = "generation_blocked"
        reason = "local_input_blocked"
        disposition = "content_rejection"
        response_valid = True
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    pipeline = True if success else False
    return {
        "schema_version": "v9-integration-result-1",
        "version": "v9",
        "case_id": row["case_id"],
        "stage": stage,
        "provider": "deepseek",
        "prompt_arm": row["arm"],
        "prompt_sha256": row["prompt_sha256"],
        "v8_online_generation_sha256": (
            "ce689879b03ccff9672ffd30fd8a85f996b03d2f9c118342dff2be8442e0a4f5"
        ),
        "response_model": (
            "deepseek-v4-flash" if kind not in {"api", "unknown_usage"} else None
        ),
        "response_valid": response_valid,
        "usage": usage,
        "api_call_count": 1,
        "reason_code": reason,
        "parameter_keys": [],
        "legacy_disposition": disposition,
        "projection_accepted": True if success else False,
        "local_fallback_used": False,
        "pipeline_attempted": pipeline,
        "pipeline_success": True if success else None,
        "hard_gate_passed": True if success else None,
        "quality_decision_passed": True if success else None,
        "published": success,
        "required_check_statuses": (
            {name: "PASS" for name in REQUIRED_HARD_CHECK_TYPES} if success else {}
        ),
        "run_result_version": "v8" if success else None,
        "duration_s": 0.01,
    }


class ExecutionLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="v9-projection-execution-")
        self.root = Path(self.temp.name)
        self.suite = _load(SUITE_PATH)
        self.protocol = _load(PROTOCOL_PATH)
        self.protocol["prompt_sha256"] = {
            "A": ONLINE_PROMPT_SHA256,
            "B": PROMPT_SHA256,
        }
        self.protocol["status"] = "DRAFT_NOT_AUTHORIZED"
        self.protocol["status"] = "DRAFT_NOT_AUTHORIZED"
        self.suite_path = self.root / "suite.json"
        self.protocol_path = self.root / "protocol.json"
        _write(self.suite_path, self.suite)
        self.protocol["suite_sha256"] = hashlib.sha256(
            self.suite_path.read_bytes()
        ).hexdigest()
        _write(self.protocol_path, self.protocol)

    def tearDown(self):
        self.temp.cleanup()

    def _execute(self, dispatcher, *, protocol=None, name="ledger.json"):
        if protocol is not None:
            _write(self.protocol_path, protocol)
        return execute_projection_diagnostic(
            protocol_path=self.protocol_path,
            suite_path=self.suite_path,
            output_path=self.root / name,
            mode="offline_mock",
            dispatcher=dispatcher,
        )

    def test_full_mock_executes_all_24_with_durable_pre_dispatch_state(self):
        calls = []
        output = self.root / "ledger.json"

        def dispatch(row, text, protocol, workspace):
            durable = _load(output)
            self.assertEqual(len(durable["rows"]), 24)
            pending = durable["rows"][len(calls)]
            self.assertTrue(pending["attempted"])
            self.assertEqual(pending["status"], "INTERRUPTED")
            self.assertGreater(durable["budget"]["reserved_tokens"], 0)
            calls.append(row["run_id"])
            return _public_result(row)

        ledger = self._execute(dispatch)
        self.assertEqual(len(calls), 24)
        self.assertEqual(ledger["batch_status"], "COMPLETED")
        self.assertTrue(all(row["status"] == "COMPLETED" for row in ledger["rows"]))
        self.assertEqual(ledger["budget"]["reserved_tokens"], 0)
        self.assertGreater(ledger["budget"]["known_cost_usd"], 0)

    def test_schema_rejection_continues_but_api_error_stops(self):
        count = 0

        def schema_then_success(row, text, protocol, workspace):
            nonlocal count
            count += 1
            return _public_result(row, kind="schema" if count == 1 else "success")

        ledger = self._execute(schema_then_success, name="schema-ledger.json")
        self.assertEqual(count, 24)
        self.assertEqual(ledger["batch_status"], "COMPLETED")

        count = 0

        def api_error(row, text, protocol, workspace):
            nonlocal count
            count += 1
            return _public_result(row, kind="api")

        ledger = self._execute(api_error, name="api-ledger.json")
        self.assertEqual(count, 1)
        self.assertEqual(ledger["batch_status"], "STOPPED")
        self.assertEqual(ledger["rows"][0]["stop_reason"], "COMPLETED")
        self.assertTrue(all(row["status"] == "NOT_RUN" for row in ledger["rows"][1:]))

    def test_unknown_usage_keeps_full_reservation(self):
        ledger = self._execute(
            lambda row, text, protocol, workspace: _public_result(
                row, kind="unknown_usage"
            )
        )
        self.assertEqual(ledger["budget"]["reserved_tokens"], 40960)
        self.assertGreater(ledger["budget"]["reserved_cost_usd"], 0)
        self.assertEqual(ledger["budget"]["known_cost_usd"], 0)

    def test_interrupt_after_result_preserves_completed_row_and_cost(self):
        with patch.object(executor, "_stop_for_result", side_effect=KeyboardInterrupt):
            ledger = self._execute(
                lambda row, text, protocol, workspace: _public_result(row)
            )
        first = ledger["rows"][0]
        self.assertEqual(first["status"], "COMPLETED")
        self.assertEqual(first["stop_reason"], "COMPLETED")
        self.assertIsNotNone(first["result"])
        self.assertEqual(ledger["batch_status"], "INTERRUPTED")
        self.assertEqual(ledger["budget"]["reserved_tokens"], 0)
        self.assertGreater(ledger["budget"]["known_cost_usd"], 0)
        self.assertTrue(
            all(row["status"] == "NOT_RUN" for row in ledger["rows"][1:])
        )
        report = executor.summarize(ledger, self.suite, self.protocol)
        self.assertEqual(report["attempted_runs"], 1)

    def test_insufficient_budget_never_dispatches(self):
        protocol = dict(self.protocol)
        protocol["max_total_tokens"] = 1
        calls = []
        ledger = self._execute(
            lambda *args: calls.append(args), protocol=protocol
        )
        self.assertEqual(calls, [])
        self.assertEqual(ledger["budget"]["budget_status"], "EXHAUSTED")
        self.assertTrue(all(row["status"] == "NOT_RUN" for row in ledger["rows"]))

    def test_malicious_result_is_not_persisted(self):
        secret = "raw-response-secret-sentinel"

        def malicious(row, text, protocol, workspace):
            value = _public_result(row)
            value["raw_response"] = secret
            return value

        ledger = self._execute(malicious)
        encoded = (self.root / "ledger.json").read_text(encoding="utf-8")
        self.assertNotIn(secret, encoded)
        self.assertEqual(ledger["rows"][0]["status"], "INTERRUPTED")
        self.assertEqual(ledger["rows"][0]["stop_reason"], "PRIVACY_ERROR")

    def test_dry_run_is_zero_dispatch_and_output_is_unique(self):
        output = self.root / "dry.json"
        ledger = execute_projection_diagnostic(
            protocol_path=self.protocol_path,
            suite_path=self.suite_path,
            output_path=output,
        )
        self.assertEqual(ledger["mode"], "dry_run")
        self.assertTrue(all(not row["attempted"] for row in ledger["rows"]))
        self.assertTrue(all(row["status"] == "NOT_RUN" for row in ledger["rows"]))
        with self.assertRaisesRegex(ExecutionBlocked, "OUTPUT_EXISTS"):
            execute_projection_diagnostic(
                protocol_path=self.protocol_path,
                suite_path=self.suite_path,
                output_path=output,
            )

    def test_cli_writes_safe_ledger_and_report_in_one_unique_directory(self):
        batch = self.root / "cli-batch"
        output = batch / "ledger.json"
        with patch.object(executor, "RESULTS_ROOT", self.root):
            code = executor.main(
                [
                    "--protocol",
                    str(self.protocol_path),
                    "--suite",
                    str(self.suite_path),
                    "--out",
                    str(output),
                ]
            )
        self.assertEqual(code, 0)
        self.assertTrue(output.is_file())
        report = _load(batch / "report.json")
        self.assertEqual(report["development_hypothesis_status"], "NOT_RUN")
        self.assertEqual(report["attempted_runs"], 0)

    def test_suite_hash_drift_is_rejected(self):
        protocol = dict(self.protocol)
        protocol["suite_sha256"] = "0" * 64
        with self.assertRaisesRegex(ExecutionBlocked, "SUITE_HASH_MISMATCH"):
            self._execute(lambda *args: None, protocol=protocol)


class AuthorizationAndProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="v9-auth-")
        self.root = Path(self.temp.name)
        self.protocol = _load(PROTOCOL_PATH)
        self.protocol_sha = "a" * 64

    def tearDown(self):
        self.temp.cleanup()

    def _receipt(self, *, expired=False):
        expiry = datetime.now(timezone.utc) + (
            -timedelta(hours=1) if expired else timedelta(hours=1)
        )
        return {
            "schema_version": "v9-projection-diagnostic-authorization-1",
            "approved": True,
            "protocol_sha256": self.protocol_sha,
            "batch_id": "authorized-batch",
            "data_scope": "public_development_only",
            "max_calls": self.protocol["max_calls"],
            "max_total_tokens": self.protocol["max_total_tokens"],
            "max_cost_usd": self.protocol["max_cost_usd"],
            "expires_at": expiry.isoformat().replace("+00:00", "Z"),
        }

    def test_missing_expired_and_hash_mismatched_receipts_fail(self):
        missing = self.root / "missing.json"
        with self.assertRaises(ExecutionBlocked):
            _validate_receipt(self.protocol, self.protocol_sha, missing)

        expired = self.root / "expired.json"
        _write(expired, self._receipt(expired=True))
        with self.assertRaisesRegex(ExecutionBlocked, "AUTHORIZATION_EXPIRED"):
            _validate_receipt(self.protocol, self.protocol_sha, expired)

        mismatch = self._receipt()
        mismatch["protocol_sha256"] = "b" * 64
        path = self.root / "mismatch.json"
        _write(path, mismatch)
        with self.assertRaisesRegex(ExecutionBlocked, "AUTHORIZATION_INVALID"):
            _validate_receipt(self.protocol, self.protocol_sha, path)

    def test_consumed_receipt_cannot_be_reused(self):
        receipt = self._receipt()
        path = self.root / "receipt.json"
        _write(path, receipt)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        _consume_receipt(
            self.root / "consumed",
            receipt=receipt,
            receipt_sha256=digest,
            protocol_sha256=self.protocol_sha,
        )
        with self.assertRaisesRegex(ExecutionBlocked, "AUTHORIZATION_ALREADY_CONSUMED"):
            _consume_receipt(
                self.root / "consumed",
                receipt=receipt,
                receipt_sha256=digest,
                protocol_sha256=self.protocol_sha,
            )

    def test_timeout_and_keyboard_interrupt_reap_worker_tree(self):
        process = Mock()
        process.wait.side_effect = subprocess.TimeoutExpired("worker", 1)
        process.poll.return_value = None
        with patch("week10.experiments.run_projection_diagnostic.subprocess.Popen", return_value=process), patch(
            "week10.experiments.run_projection_diagnostic._terminate_process_tree"
        ) as terminate:
            with self.assertRaises(TimeoutError):
                _run_worker_process(
                    ledger_path=self.root / "ledger.json",
                    result_path=self.root / "result.json",
                    row_index=0,
                    batch_id="batch",
                    timeout_s=1,
                )
            terminate.assert_called_once_with(process)

        process = Mock()
        process.wait.side_effect = KeyboardInterrupt
        process.poll.return_value = None
        with patch("week10.experiments.run_projection_diagnostic.subprocess.Popen", return_value=process), patch(
            "week10.experiments.run_projection_diagnostic._terminate_process_tree"
        ) as terminate:
            with self.assertRaises(KeyboardInterrupt):
                _run_worker_process(
                    ledger_path=self.root / "ledger.json",
                    result_path=self.root / "result.json",
                    row_index=0,
                    batch_id="batch",
                    timeout_s=1,
                )
            terminate.assert_called_once_with(process)

    def _frozen_fixture(self, name="project", *, omit_schema=None):
        project = self.root / name
        config = project / "week10/configs"
        results = project / "week10/results"
        config.mkdir(parents=True)
        results.mkdir(parents=True)

        for relative in IMPLEMENTATION_FILES:
            target = project / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / relative).read_bytes())
        manifest_hashes = {}
        for relative, expected in V8_SOURCE_HASHES.items():
            target = project / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / relative).read_bytes())
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), expected)
            manifest_hashes[relative] = expected
        for index in range(29):
            relative = f"week9/src/public_fixture_{index:02d}.txt"
            target = project / relative
            target.write_bytes(f"fixture-{index}".encode("ascii"))
            manifest_hashes[relative] = hashlib.sha256(target.read_bytes()).hexdigest()
        manifest_path = config / "v8_source_hashes.json"
        _write(
            manifest_path,
            {
                "schema_version": "v9-v8-source-hashes-1",
                "captured_on": "2026-09-08",
                "certification_binding_drift": [],
                "sha256": manifest_hashes,
            },
        )

        suite_path = config / "development_diagnostic_suite.json"
        _write(suite_path, _load(SUITE_PATH))
        local_cases = []
        for index, case_id in enumerate(("rc_lowpass", "rlc_lowpass", "dual_room_thermal")):
            relative = f"week10/results/local-{index}/result.json"
            target = project / relative
            target.parent.mkdir(parents=True)
            target.write_bytes(
                _canonical(
                    {
                        "schema_version": "v9-integration-result-1",
                        "case_id": case_id,
                        "api_call_count": 0,
                        "pipeline_success": True,
                        "hard_gate_passed": True,
                        "quality_decision_passed": True,
                        "published": True,
                        "required_check_statuses": {
                            check: "PASS" for check in REQUIRED_HARD_CHECK_TYPES
                        },
                    }
                )
            )
            local_cases.append(
                {
                    "case_id": case_id,
                    "result_path": relative,
                    "result_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    "required_checks_passed": 10,
                    "published": True,
                    "api_call_count": 0,
                }
            )
        summary_path = results / "local-regression-summary.json"
        _write(
            summary_path,
            {
                "schema_version": "v9-local-regression-summary-1",
                "scope": "three_public_development_positives",
                "real_execution": True,
                "cases": local_cases,
                "successes": 3,
                "api_call_count": 0,
                "protected_v8_files_checked": 33,
                "protected_v8_drift": [],
                "online_hypothesis_status": "NOT_RUN",
            },
        )
        differential_evidence = results / "differential-summary.json"
        _write(
            differential_evidence,
            {
                "schema_version": "v9-projection-differential-audit-1",
                "comparisons": 222,
                "mismatches": 0,
                "leaks": 0,
                "online_api_calls": 0,
            },
        )
        test_evidence = results / "offline-test-summary.json"
        _write(
            test_evidence,
            {
                "schema_version": "v9-offline-test-summary-1",
                "tests_run": 28,
                "failures": 0,
                "errors": 0,
                "skipped": 0,
                "online_api_calls": 0,
            },
        )
        extra_evidence = results / "process-cleanup-summary.json"
        _write(
            extra_evidence,
            {"schema_version": "v9-process-cleanup-summary-1", "api_calls": 0},
        )
        evidence_paths = [summary_path, differential_evidence, test_evidence, extra_evidence]
        evidence = {
            str(path.relative_to(project)).replace("\\", "/"): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in evidence_paths
            if _load(path).get("schema_version") != omit_schema
        }
        acceptance_path = config / "offline_acceptance_v1.json"
        _write(
            acceptance_path,
            {
                "schema_version": "v9-offline-acceptance-1",
                "status": "PASS",
                "local_successes": 3,
                "api_call_count": 0,
                "verifier_drift_count": 0,
                "privacy_leak_count": 0,
                "protected_v8_drift": [],
                "evidence_sha256": evidence,
            },
        )
        protocol = _load(PROTOCOL_PATH)
        protocol["prompt_sha256"] = {
            "A": ONLINE_PROMPT_SHA256,
            "B": PROMPT_SHA256,
        }
        protocol["status"] = "FROZEN_NOT_AUTHORIZED"
        protocol["suite_sha256"] = hashlib.sha256(suite_path.read_bytes()).hexdigest()
        protocol["offline_acceptance_sha256"] = hashlib.sha256(
            acceptance_path.read_bytes()
        ).hexdigest()
        protocol["v8_source_manifest_sha256"] = hashlib.sha256(
            manifest_path.read_bytes()
        ).hexdigest()
        protocol["implementation_sha256"] = {
            relative: hashlib.sha256((project / relative).read_bytes()).hexdigest()
            for relative in IMPLEMENTATION_FILES
        }
        protocol_path = config / "projection_diagnostic_protocol_v1.json"
        _write(protocol_path, protocol)
        protocol_sha = hashlib.sha256(protocol_path.read_bytes()).hexdigest()
        (config / "projection_diagnostic_protocol_v1.json.sha256").write_text(
            protocol_sha, encoding="ascii"
        )
        return {
            "project": project,
            "config": config,
            "protocol_path": protocol_path,
            "suite_path": suite_path,
            "manifest_path": manifest_path,
            "acceptance_path": acceptance_path,
            "sidecar_path": config / "projection_diagnostic_protocol_v1.json.sha256",
            "protocol_sha": protocol_sha,
        }

    def _patched_frozen_roots(self, fixture):
        return patch.multiple(
            executor,
            PROJECT_ROOT=fixture["project"],
            WEEK10_ROOT=fixture["project"] / "week10",
            RESULTS_ROOT=fixture["project"] / "week10/results",
            DEFAULT_PROTOCOL=fixture["protocol_path"],
            DEFAULT_SUITE=fixture["suite_path"],
            V8_HASH_MANIFEST=fixture["manifest_path"],
        )

    def test_complete_frozen_context_preflight_without_receipt(self):
        fixture = self._frozen_fixture()
        with self._patched_frozen_roots(fixture):
            context = executor.validate_frozen_context(
                protocol_path=fixture["protocol_path"],
                suite_path=fixture["suite_path"],
                protocol_sidecar_path=fixture["sidecar_path"],
                offline_acceptance_path=fixture["acceptance_path"],
            )
        self.assertEqual(context["protocol_sha256"], fixture["protocol_sha"])
        self.assertEqual(len(context["schedule"]), 24)

    def test_frozen_context_requires_all_three_acceptance_evidence_types(self):
        schemas = (
            "v9-local-regression-summary-1",
            "v9-projection-differential-audit-1",
            "v9-offline-test-summary-1",
        )
        for index, schema in enumerate(schemas):
            with self.subTest(schema=schema):
                fixture = self._frozen_fixture(f"missing-{index}", omit_schema=schema)
                with self._patched_frozen_roots(fixture), self.assertRaises(
                    ExecutionBlocked
                ):
                    executor.validate_frozen_context(
                        protocol_path=fixture["protocol_path"],
                        suite_path=fixture["suite_path"],
                        protocol_sidecar_path=fixture["sidecar_path"],
                        offline_acceptance_path=fixture["acceptance_path"],
                    )

    def test_frozen_dry_run_rejects_source_drift_before_dispatch(self):
        fixture = self._frozen_fixture("drift")
        drifted = fixture["project"] / IMPLEMENTATION_FILES[0]
        drifted.write_bytes(drifted.read_bytes() + b"\n")
        output = fixture["project"] / "week10/results/drift-batch/ledger.json"
        with self._patched_frozen_roots(fixture), patch.object(
            executor, "_input_estimate", side_effect=AssertionError("dispatch reached")
        ), patch.object(executor.os, "getenv", side_effect=AssertionError("credential read")):
            with self.assertRaisesRegex(ExecutionBlocked, "IMPLEMENTATION_HASH_MISMATCH"):
                executor.execute_projection_diagnostic(
                    protocol_path=fixture["protocol_path"],
                    suite_path=fixture["suite_path"],
                    output_path=output,
                    mode="dry_run",
                )
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
