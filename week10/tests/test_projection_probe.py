from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from week9.src.generation import DeterministicNaturalLanguageProvider
from week9.src.online_generation import (
    ONLINE_PROJECTION_SCHEMA,
    _parse_projection,
    _verify_projection,
)
from week10.experiments.projection_probe import inspect_response
from week10.experiments import run_projection_probe as runner


RLC = (
    "设计一个 RLC 串联低通滤波器，截止频率 10kHz，电阻 100Ω，"
    "电感 10mH，电容根据谐振频率计算，输入电压 5V 正弦波"
)
DUAL = (
    "设计一个双房间热传导系统：房间A热容 300kJ/K，房间B热容 200kJ/K，"
    "两房间之间墙体热阻 0.05 K/W，房间A外墙热阻 0.03 K/W 连接到室外 35°C，"
    "房间B外墙热阻 0.04 K/W 连接到室外 35°C，两房间初始温度均为 20°C"
)


def _projection(requirement: str) -> dict:
    local = DeterministicNaturalLanguageProvider().generate(requirement)
    return {
        "schema_version": ONLINE_PROJECTION_SCHEMA,
        "status": "ready" if local.ready else "blocked",
        "template_id": local.template_id,
        "observations": [asdict(item) for item in local.observations],
        "issues": [] if local.ready else [asdict(item) for item in local.issues],
    }


def _response(projection: dict) -> dict:
    return {
        "model": "deepseek-v4-flash",
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        projection,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                }
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
        },
    }


class ProjectionProbeInspectorTests(unittest.TestCase):
    def test_real_local_projection_is_accepted_and_json_safe(self):
        projection = _projection(DUAL)
        response = _response(projection)
        result = inspect_response(response, DUAL)

        self.assertTrue(result["response_valid"])
        self.assertTrue(result["local_ready"])
        self.assertTrue(result["remote_ready"])
        self.assertTrue(result["template_match"])
        self.assertEqual(result["expected_parameter_count"], 8)
        self.assertEqual(result["observed_parameter_count"], 8)
        self.assertEqual(result["reason_code"], "accepted")
        self.assertTrue(result["projection_accepted"])
        json.dumps(result, allow_nan=False)

    def test_counts_missing_unexpected_duplicate_and_unknown_without_leak(self):
        projection = _projection(DUAL)
        removed = projection["observations"].pop()
        projection["observations"].append(
            {"name": "resistance", "value": 1, "unit": "SECRET-U", "source_text": "SECRET-SPAN"}
        )
        projection["observations"].append(dict(projection["observations"][0]))
        projection["observations"].append(
            {"name": "SECRET-NAME", "value": 987654321, "unit": "SECRET-UNIT", "source_text": "SECRET-SOURCE"}
        )
        result = inspect_response(_response(projection), DUAL)

        self.assertEqual(result["expected_parameter_count"], 8)
        self.assertEqual(result["observed_parameter_count"], 10)
        self.assertEqual(result["missing_parameter_names"], [removed["name"]])
        self.assertEqual(result["unexpected_known_parameter_names"], ["resistance"])
        self.assertEqual(result["duplicate_parameter_names"], ["heat_capacity_a"])
        self.assertEqual(result["unknown_parameter_count"], 1)
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True)
        for secret in (
            "SECRET-NAME",
            "SECRET-U",
            "SECRET-SPAN",
            "SECRET-UNIT",
            "SECRET-SOURCE",
            "987654321",
        ):
            self.assertNotIn(secret, encoded)

    def test_blocked_rlc_keeps_only_allowlisted_model_reported_metadata(self):
        projection = _projection(RLC)
        projection.update(status="blocked", observations=[])
        projection["issues"] = [
            {
                "code": "missing_parameter",
                "message": "SECRET: missing capacitance derivation from resonance cutoff",
            },
            {
                "code": "SECRET-CODE",
                "message": "SECRET: 电容计算与谐振频率冲突",
            },
        ]
        result = inspect_response(_response(projection), RLC)

        self.assertTrue(result["response_valid"])
        self.assertTrue(result["local_ready"])
        self.assertFalse(result["remote_ready"])
        self.assertEqual(result["reason_code"], "ready_mismatch")
        self.assertEqual(
            result["model_reported_issue_code_counts"]["missing_parameter"], 1
        )
        self.assertEqual(result["model_reported_unknown_issue_count"], 1)
        self.assertEqual(
            result["model_reported_issue_mentions"],
            {
                "resonance": True,
                "cutoff": True,
                "capacitance": True,
                "missing": True,
                "conflict": True,
                "derivation": True,
            },
        )
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("SECRET", encoded)

    def test_mismatch_lists_contain_keys_only(self):
        projection = _projection(RLC)
        projection["observations"][0]["value"] += 1
        projection["observations"][1]["unit"] = "PRIVATE-UNIT"
        projection["observations"][2]["source_text"] = "PRIVATE-SPAN"
        result = inspect_response(_response(projection), RLC)

        self.assertEqual(result["numeric_mismatch_keys"], ["resistance"])
        self.assertEqual(result["unit_mismatch_keys"], ["inductance"])
        self.assertEqual(result["source_span_mismatch_keys"], ["capacitance"])
        encoded = json.dumps(result)
        self.assertNotIn("PRIVATE", encoded)

    def test_malformed_response_has_only_finite_failure_codes(self):
        projection = _projection(RLC)
        bad_schema = _response(projection)
        bad_schema["choices"][0]["message"]["content"] = json.dumps(
            {"SECRET-KEY": "SECRET-VALUE"}
        )
        cases = (
            ({"SECRET-KEY": "SECRET-VALUE"}, "response_schema_invalid"),
            (_response_with_content("{SECRET"), "response_json_invalid"),
            (bad_schema, "projection_schema_invalid"),
        )
        for response, expected in cases:
            with self.subTest(expected=expected):
                result = inspect_response(response, RLC)
                self.assertFalse(result["response_valid"])
                self.assertEqual(result["failure_code"], expected)
                self.assertNotIn("SECRET", json.dumps(result))

    def test_inspection_does_not_change_frozen_verifier_outcome(self):
        projection = _projection(RLC)
        projection["observations"][0]["value"] += 1
        local = DeterministicNaturalLanguageProvider().generate(RLC)
        parsed = _parse_projection(
            json.dumps(projection, ensure_ascii=False, separators=(",", ":"))
        )
        before = _verify_projection(parsed, local)
        inspect_response(_response(projection), RLC)
        after = _verify_projection(parsed, local)
        self.assertEqual(before, after)
        self.assertTrue(before)


def _response_with_content(content: str) -> dict:
    response = _response(_projection(RLC))
    response["choices"][0]["message"]["content"] = content
    return response


ROOT = Path(__file__).resolve().parents[2]


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


class ProjectionProbeRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.protocol_path = self.root / "configs/projection_probe_protocol_v1.json"
        self.receipt_root = self.root / "configs/authorization_receipts"
        self.results_root = self.root / "results"
        self.results_root.mkdir(parents=True)
        self.consumed_root = self.results_root / ".receipts"
        parent_protocol = json.loads(
            (ROOT / "week10/configs/projection_diagnostic_protocol_v1.json").read_text(
                encoding="utf-8"
            )
        )
        suite_path = ROOT / "week10/configs/development_diagnostic_suite.json"
        suite = json.loads(suite_path.read_text(encoding="utf-8"))
        self.parent = {
            "protocol": parent_protocol,
            "suite": suite,
            "schedule": [],
            "protocol_sha256": runner.PARENT_PROTOCOL_SHA256,
            "suite_sha256": hashlib.sha256(suite_path.read_bytes()).hexdigest(),
        }
        protocol = {
            "schema_version": "v9-projection-probe-protocol-1",
            "status": "FROZEN",
            "scope": "public_development_only",
            "parent_protocol_path": "week10/configs/projection_diagnostic_protocol_v1.json",
            "parent_protocol_sha256": runner.PARENT_PROTOCOL_SHA256,
            "suite_path": "week10/configs/development_diagnostic_suite.json",
            "suite_sha256": self.parent["suite_sha256"],
            "implementation_sha256": {
                relative: hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
                for relative in runner.IMPLEMENTATION_FILES
            },
            "batch_id": "v9pp-test-01",
            "cases": list(runner.CASES),
            "prompt_arm": "B",
            "api_base": parent_protocol["api_base"],
            "model": parent_protocol["model"],
            "temperature": parent_protocol["temperature"],
            "max_tokens": 8192,
            "thinking": parent_protocol["thinking"],
            "http_timeout_s": 120,
            "attempt_timeout_s": 180,
            "automatic_retries": 0,
            "input_token_reservation_per_call": 32768,
            "max_calls": 2,
            "max_total_tokens": 81920,
            "max_cost_usd": 0.1,
            "input_usd_per_million": parent_protocol["input_usd_per_million"],
            "output_usd_per_million": parent_protocol["output_usd_per_million"],
            "raw_response_storage_allowed": False,
            "pipeline_allowed": False,
            "authorization_policy": "digest_bound_single_batch_receipt_consumed_before_first_request",
        }
        _write(self.protocol_path, protocol)
        self.protocol_sha = hashlib.sha256(self.protocol_path.read_bytes()).hexdigest()
        self.protocol_path.with_name(self.protocol_path.name + ".sha256").write_text(
            self.protocol_sha, encoding="ascii"
        )
        self.receipt_path = self.receipt_root / "v9pp-test-01.json"
        self.receipt = {
            "schema_version": "v9-projection-probe-authorization-1",
            "approved": True,
            "protocol_sha256": self.protocol_sha,
            "batch_id": "v9pp-test-01",
            "data_scope": "public_development_only",
            "max_calls": 2,
            "max_total_tokens": 81920,
            "max_cost_usd": 0.1,
            "expires_at": "2099-01-01T00:00:00Z",
        }
        _write(self.receipt_path, self.receipt)
        self.paths = patch.multiple(
            runner,
            DEFAULT_PROBE_PROTOCOL=self.protocol_path,
            RECEIPT_ROOT=self.receipt_root,
            RESULTS_ROOT=self.results_root,
            CONSUMED_ROOT=self.consumed_root,
        )
        self.paths.start()
        self.addCleanup(self.paths.stop)

    def _context(self):
        with patch.object(runner, "validate_frozen_context", return_value=self.parent):
            return runner.validate_probe_context(
                protocol_path=self.protocol_path, receipt_path=self.receipt_path
            )

    def _failure_result(self, case_id: str, usage=None):
        requirement = next(
            item["requirement_text"]
            for item in self.parent["suite"]["cases"]
            if item["case_id"] == case_id
        )
        inspection = inspect_response({}, requirement)
        return {
            "schema_version": "v9-projection-probe-result-1",
            "case_id": case_id,
            "api_call_count": 1,
            "usage": usage,
            "response_valid": False,
            "reason_code": "response_contract_invalid",
            "projection_accepted": False,
            "inspection": inspection,
        }

    def _success_result(self, case_id: str, usage):
        requirement = next(
            item["requirement_text"]
            for item in self.parent["suite"]["cases"]
            if item["case_id"] == case_id
        )
        inspection = inspect_response(_response(_projection(requirement)), requirement)
        return {
            "schema_version": "v9-projection-probe-result-1",
            "case_id": case_id,
            "api_call_count": 1,
            "usage": usage,
            "response_valid": True,
            "reason_code": "accepted",
            "projection_accepted": True,
            "inspection": inspection,
        }

    def test_dry_run_validates_receipt_without_env_network_or_output_dir(self):
        with patch.object(runner, "validate_frozen_context", return_value=self.parent), patch.object(
            runner.os.environ, "get", side_effect=AssertionError("credential read")
        ), patch.object(
            runner, "_run_worker_process", side_effect=AssertionError("network dispatch")
        ):
            ledger = runner.execute_projection_probe(
                protocol_path=self.protocol_path,
                receipt_path=self.receipt_path,
                execute=False,
            )
        self.assertEqual(ledger["mode"], "dry_run")
        self.assertEqual([row["status"] for row in ledger["rows"]], ["NOT_RUN", "NOT_RUN"])
        self.assertFalse((self.results_root / "v9pp-test-01").exists())

    def test_expired_receipt_is_rejected(self):
        receipt = dict(self.receipt, expires_at="2020-01-01T00:00:00Z")
        _write(self.receipt_path, receipt)
        with patch.object(runner, "validate_frozen_context", return_value=self.parent), self.assertRaisesRegex(
            runner.ExecutionBlocked, "PROBE_AUTHORIZATION_EXPIRED"
        ):
            runner.validate_probe_context(
                protocol_path=self.protocol_path,
                receipt_path=self.receipt_path,
            )

    def test_unknown_usage_stops_second_row_and_batch_cannot_be_reused(self):
        first = self._success_result("dual_room_thermal", usage=None)
        with patch.object(runner, "validate_frozen_context", return_value=self.parent), patch.object(
            runner, "_input_estimate", return_value=1024
        ), patch.object(runner, "_run_worker_process", return_value=first) as dispatch:
            ledger = runner.execute_projection_probe(
                protocol_path=self.protocol_path,
                receipt_path=self.receipt_path,
                execute=True,
            )
            with self.assertRaisesRegex(runner.ExecutionBlocked, "PROBE_OUTPUT_EXISTS"):
                runner.execute_projection_probe(
                    protocol_path=self.protocol_path,
                    receipt_path=self.receipt_path,
                    execute=True,
                )
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(ledger["batch_status"], "STOPPED")
        self.assertEqual(ledger["rows"][1]["status"], "NOT_RUN")
        self.assertEqual(ledger["budget"]["reserved_tokens"], 40960)

    def test_two_successful_rows_complete_with_known_usage(self):
        usage = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
        results = [
            self._success_result("dual_room_thermal", usage),
            self._success_result("rlc_lowpass", usage),
        ]
        with patch.object(runner, "validate_frozen_context", return_value=self.parent), patch.object(
            runner, "_input_estimate", return_value=1024
        ), patch.object(runner, "_run_worker_process", side_effect=results) as dispatch:
            ledger = runner.execute_projection_probe(
                protocol_path=self.protocol_path,
                receipt_path=self.receipt_path,
                execute=True,
            )
        self.assertEqual(dispatch.call_count, 2)
        self.assertEqual(ledger["batch_status"], "COMPLETED")
        self.assertEqual([row["status"] for row in ledger["rows"]], ["COMPLETED", "COMPLETED"])
        self.assertEqual(ledger["budget"]["reserved_tokens"], 0)
        self.assertEqual(ledger["budget"]["known_tokens"], 60)

    def test_timeout_and_keyboard_interrupt_reap_worker_tree(self):
        for error, expected in (
            (subprocess.TimeoutExpired("worker", 180), TimeoutError),
            (KeyboardInterrupt(), KeyboardInterrupt),
        ):
            with self.subTest(error=type(error).__name__):
                process = Mock()
                process.communicate.side_effect = [error, (b"", b"")]
                with patch.object(runner.subprocess, "Popen", return_value=process), patch.object(
                    runner, "_terminate_process_tree"
                ) as terminate:
                    with self.assertRaises(expected):
                        runner._run_worker_process(
                            protocol_path=self.protocol_path,
                            receipt_path=self.receipt_path,
                            ledger_path=self.results_root / "x/ledger.json",
                            row_index=0,
                            batch_id="v9pp-test-01",
                            timeout_s=180,
                        )
                    terminate.assert_called_once_with(process)

    def test_model_mismatch_keeps_usage_and_safe_counts_but_marks_invalid(self):
        context = self._context()
        ledger = runner._new_ledger(context, "online")
        ledger["authorization_consumed"] = True
        ledger["rows"][0].update(
            attempted=True, status="INTERRUPTED", stop_reason="INTERRUPTED"
        )
        tokens, cost = runner._reservation(context["probe_protocol"])
        ledger["budget"]["reserved_tokens"] = tokens
        ledger["budget"]["reserved_cost_usd"] = round(cost, 12)
        consumed = {
            "schema_version": "v9-projection-probe-authorization-consumed-1",
            "receipt_sha256": context["receipt_sha256"],
            "protocol_sha256": context["probe_protocol_sha256"],
            "batch_id": "v9pp-test-01",
        }
        response = _response(_projection(DUAL))

        class FakeProvider:
            def __init__(self, **kwargs):
                self.transport = kwargs["transport"]
                self.api_call_count = 1
                self.last_usage = {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                }
                self.last_diagnostic = {
                    "reason_code": "response_model_mismatch",
                    "projection_accepted": False,
                    "response_valid": False,
                }

            def _post(self, payload):
                return response

            def generate(self, requirement):
                self.transport({})
                raise runner.OnlineGenerationError("response_model_mismatch")

        with patch.object(runner, "validate_probe_context", return_value=context), patch.object(
            runner, "_load_json", side_effect=[consumed, ledger]
        ), patch.object(runner, "_atomic_write"), patch.object(
            runner, "DeepSeekNaturalLanguageProvider", FakeProvider
        ), patch.object(runner, "_input_estimate", return_value=1024), patch.object(
            runner.os.environ, "get", return_value="key"
        ):
            result = runner._worker(
                protocol_path=self.protocol_path,
                receipt_path=self.receipt_path,
                ledger_path=self.results_root / "v9pp-test-01/ledger.json",
                row_index=0,
                batch_id="v9pp-test-01",
            )
        self.assertEqual(result["reason_code"], "response_model_mismatch")
        self.assertEqual(result["usage"]["total_tokens"], 30)
        self.assertFalse(result["response_valid"])
        self.assertFalse(result["inspection"]["response_valid"])
        self.assertEqual(result["inspection"]["expected_parameter_count"], 8)


if __name__ == "__main__":
    unittest.main()
