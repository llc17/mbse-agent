"""Two-call, projection-only diagnostic probe for the public V9 cases."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEEK10_ROOT = PROJECT_ROOT / "week10"
RESULTS_ROOT = WEEK10_ROOT / "results"
PARENT_PROTOCOL = WEEK10_ROOT / "configs" / "projection_diagnostic_protocol_v1.json"
PARENT_SIDECAR = PARENT_PROTOCOL.with_name(PARENT_PROTOCOL.name + ".sha256")
SUITE_PATH = WEEK10_ROOT / "configs" / "development_diagnostic_suite.json"
OFFLINE_ACCEPTANCE = WEEK10_ROOT / "configs" / "offline_acceptance_v1.json"
DEFAULT_PROBE_PROTOCOL = WEEK10_ROOT / "configs" / "projection_probe_protocol_v1.json"
RECEIPT_ROOT = WEEK10_ROOT / "configs" / "authorization_receipts"
CONSUMED_ROOT = RESULTS_ROOT / ".v9_projection_probe_receipts"
PARENT_PROTOCOL_SHA256 = (
    "089fe03aef216e084ae1f9837c6d16df2cecd5bb60b619b7589594bd99474737"
)
IMPLEMENTATION_FILES = (
    "week10/experiments/projection_probe.py",
    "week10/experiments/run_projection_probe.py",
    "week10/tests/test_projection_probe.py",
)
CASES = ("dual_room_thermal", "rlc_lowpass")

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from week9.src.online_generation import OnlineGenerationError  # noqa: E402
from week10.experiments.projection_probe import inspect_response  # noqa: E402
from week10.experiments.run_projection_diagnostic import (  # noqa: E402
    ExecutionBlocked,
    _atomic_write,
    _canonical,
    _digest_path,
    _input_estimate,
    _load_json,
    _terminate_process_tree,
    validate_frozen_context,
)
from week10.src.online_generation import DeepSeekNaturalLanguageProvider  # noqa: E402


_SHA = re.compile(r"^[0-9a-f]{64}$")
_BATCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_PROTOCOL_FIELDS = {
    "schema_version",
    "status",
    "scope",
    "parent_protocol_path",
    "parent_protocol_sha256",
    "suite_path",
    "suite_sha256",
    "implementation_sha256",
    "batch_id",
    "cases",
    "prompt_arm",
    "api_base",
    "model",
    "temperature",
    "max_tokens",
    "thinking",
    "http_timeout_s",
    "attempt_timeout_s",
    "automatic_retries",
    "input_token_reservation_per_call",
    "max_calls",
    "max_total_tokens",
    "max_cost_usd",
    "input_usd_per_million",
    "output_usd_per_million",
    "raw_response_storage_allowed",
    "pipeline_allowed",
    "authorization_policy",
}
_RECEIPT_FIELDS = {
    "schema_version",
    "approved",
    "protocol_sha256",
    "batch_id",
    "data_scope",
    "max_calls",
    "max_total_tokens",
    "max_cost_usd",
    "expires_at",
}
_INFRASTRUCTURE_REASONS = {
    "api_transport_error",
    "response_contract_invalid",
    "response_model_mismatch",
    "usage_contract_invalid",
    "local_generation_error",
    "response_json_invalid",
    "projection_schema_invalid",
    "unknown_diagnostic",
}


def _fail(code: str) -> None:
    raise ExecutionBlocked(code)


def _validate_probe_protocol(
    protocol_path: Path, parent: Mapping[str, Any]
) -> tuple[dict[str, Any], str]:
    if protocol_path.resolve() != DEFAULT_PROBE_PROTOCOL.resolve():
        _fail("PROBE_PROTOCOL_PATH_INVALID")
    protocol = _load_json(protocol_path, require_canonical=True)
    if set(protocol) != _PROTOCOL_FIELDS:
        _fail("PROBE_PROTOCOL_SCHEMA_INVALID")
    parent_protocol = parent["protocol"]
    if (
        protocol.get("schema_version") != "v9-projection-probe-protocol-1"
        or protocol.get("status") != "FROZEN"
        or protocol.get("scope") != "public_development_only"
        or protocol.get("parent_protocol_path")
        != "week10/configs/projection_diagnostic_protocol_v1.json"
        or protocol.get("parent_protocol_sha256") != PARENT_PROTOCOL_SHA256
        or parent.get("protocol_sha256") != PARENT_PROTOCOL_SHA256
        or protocol.get("suite_path")
        != "week10/configs/development_diagnostic_suite.json"
        or protocol.get("suite_sha256") != parent.get("suite_sha256")
        or protocol.get("batch_id") is None
        or not isinstance(protocol["batch_id"], str)
        or not _BATCH.fullmatch(protocol["batch_id"])
        or protocol.get("cases") != list(CASES)
        or protocol.get("prompt_arm") != "B"
        or protocol.get("api_base") != parent_protocol.get("api_base")
        or protocol.get("model") != parent_protocol.get("model")
        or protocol.get("temperature") != parent_protocol.get("temperature")
        or protocol.get("max_tokens") != 8192
        or protocol.get("max_tokens") != parent_protocol.get("max_tokens")
        or protocol.get("thinking") != parent_protocol.get("thinking")
        or protocol.get("http_timeout_s") != 120
        or protocol.get("attempt_timeout_s") != 180
        or protocol.get("automatic_retries") != 0
        or protocol.get("input_token_reservation_per_call") != 32768
        or protocol.get("max_calls") != 2
        or protocol.get("max_total_tokens") != 81920
        or protocol.get("max_cost_usd") != 0.1
        or protocol.get("input_usd_per_million")
        != parent_protocol.get("input_usd_per_million")
        or protocol.get("output_usd_per_million")
        != parent_protocol.get("output_usd_per_million")
        or protocol.get("raw_response_storage_allowed") is not False
        or protocol.get("pipeline_allowed") is not False
        or protocol.get("authorization_policy")
        != "digest_bound_single_batch_receipt_consumed_before_first_request"
    ):
        _fail("PROBE_PROTOCOL_VALUE_INVALID")
    recorded = protocol.get("implementation_sha256")
    if not isinstance(recorded, dict) or set(recorded) != set(IMPLEMENTATION_FILES):
        _fail("PROBE_IMPLEMENTATION_HASH_SET_INVALID")
    for relative in IMPLEMENTATION_FILES:
        expected = recorded.get(relative)
        if (
            not isinstance(expected, str)
            or not _SHA.fullmatch(expected)
            or _digest_path(PROJECT_ROOT / relative) != expected
        ):
            _fail("PROBE_IMPLEMENTATION_HASH_MISMATCH")
    sidecar = protocol_path.with_name(protocol_path.name + ".sha256")
    protocol_sha256 = _digest_path(protocol_path)
    try:
        sidecar_value = sidecar.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        _fail("PROBE_PROTOCOL_SIDECAR_INVALID")
    if sidecar_value != protocol_sha256 or not _SHA.fullmatch(sidecar_value):
        _fail("PROBE_PROTOCOL_SIDECAR_INVALID")
    suite = parent.get("suite")
    if not isinstance(suite, dict):
        _fail("PROBE_SUITE_INVALID")
    selected = [
        item
        for item in suite.get("cases", [])
        if isinstance(item, dict) and item.get("case_id") in CASES
    ]
    if (
        len(selected) != 2
        or {item.get("case_id") for item in selected} != set(CASES)
        or any(item.get("positive") is not True for item in selected)
    ):
        _fail("PROBE_SUITE_INVALID")
    reserve_tokens, reserve_cost = _reservation(protocol)
    if (
        reserve_tokens * 2 != protocol["max_total_tokens"]
        or reserve_cost * 2 > protocol["max_cost_usd"]
    ):
        _fail("PROBE_BUDGET_INVALID")
    return protocol, protocol_sha256


def _validate_receipt(
    path: Path, protocol: Mapping[str, Any], protocol_sha256: str
) -> tuple[dict[str, Any], str]:
    expected_path = RECEIPT_ROOT / f"{protocol['batch_id']}.json"
    if path.resolve() != expected_path.resolve():
        _fail("PROBE_AUTHORIZATION_PATH_INVALID")
    receipt = _load_json(path, require_canonical=True)
    if set(receipt) != _RECEIPT_FIELDS:
        _fail("PROBE_AUTHORIZATION_SCHEMA_INVALID")
    if (
        receipt.get("schema_version") != "v9-projection-probe-authorization-1"
        or receipt.get("approved") is not True
        or receipt.get("protocol_sha256") != protocol_sha256
        or receipt.get("batch_id") != protocol.get("batch_id")
        or receipt.get("data_scope") != "public_development_only"
        or receipt.get("max_calls") != 2
        or receipt.get("max_total_tokens") != 81920
        or receipt.get("max_cost_usd") != 0.1
    ):
        _fail("PROBE_AUTHORIZATION_INVALID")
    try:
        expires_at = receipt["expires_at"]
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except (AttributeError, KeyError, TypeError, ValueError):
        _fail("PROBE_AUTHORIZATION_EXPIRY_INVALID")
    if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
        _fail("PROBE_AUTHORIZATION_EXPIRED")
    return receipt, _digest_path(path)


def validate_probe_context(
    *, protocol_path: str | Path, receipt_path: str | Path
) -> dict[str, Any]:
    """Validate the frozen parent, probe protocol, hashes, and receipt."""

    parent = validate_frozen_context(
        protocol_path=PARENT_PROTOCOL,
        suite_path=SUITE_PATH,
        protocol_sidecar_path=PARENT_SIDECAR,
        offline_acceptance_path=OFFLINE_ACCEPTANCE,
    )
    protocol_file = Path(protocol_path).resolve()
    protocol, protocol_sha256 = _validate_probe_protocol(protocol_file, parent)
    receipt, receipt_sha256 = _validate_receipt(
        Path(receipt_path).resolve(), protocol, protocol_sha256
    )
    return {
        **parent,
        "probe_protocol": protocol,
        "probe_protocol_sha256": protocol_sha256,
        "receipt": receipt,
        "receipt_sha256": receipt_sha256,
    }


def _reservation(protocol: Mapping[str, Any]) -> tuple[int, float]:
    tokens = protocol["input_token_reservation_per_call"] + protocol["max_tokens"]
    cost = (
        protocol["input_token_reservation_per_call"]
        * protocol["input_usd_per_million"]
        + protocol["max_tokens"] * protocol["output_usd_per_million"]
    ) / 1_000_000
    return tokens, cost


def _rows(context: Mapping[str, Any]) -> list[dict[str, Any]]:
    suite_cases = {item["case_id"]: item for item in context["suite"]["cases"]}
    return [
        {
            "run_id": f"probe-{index:02d}-{case_id}",
            "case_id": case_id,
            "requirement_sha256": suite_cases[case_id]["requirement_sha256"],
            "arm": "B",
            "attempted": False,
            "status": "PLANNED",
            "stop_reason": "NOT_STARTED",
            "result": None,
        }
        for index, case_id in enumerate(CASES, 1)
    ]


def _new_ledger(context: Mapping[str, Any], mode: str) -> dict[str, Any]:
    return {
        "schema_version": "v9-projection-probe-ledger-1",
        "mode": mode,
        "batch_id": context["probe_protocol"]["batch_id"],
        "probe_protocol_sha256": context["probe_protocol_sha256"],
        "parent_protocol_sha256": context["protocol_sha256"],
        "suite_sha256": context["suite_sha256"],
        "authorization_consumed": False,
        "batch_status": "PLANNED",
        "rows": _rows(context),
        "budget": {
            "reserved_tokens": 0,
            "reserved_cost_usd": 0.0,
            "known_tokens": 0,
            "known_cost_usd": 0.0,
            "budget_status": "OK",
        },
    }


def _consume_receipt(context: Mapping[str, Any]) -> Path:
    marker = CONSUMED_ROOT / f"{context['receipt_sha256']}.consumed.json"
    _atomic_write(
        marker,
        {
            "schema_version": "v9-projection-probe-authorization-consumed-1",
            "receipt_sha256": context["receipt_sha256"],
            "protocol_sha256": context["probe_protocol_sha256"],
            "batch_id": context["probe_protocol"]["batch_id"],
        },
        create=True,
    )
    return marker


def _inspection_shape(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    expected = inspect_response({}, "safe offline invalid input")
    if set(value) != set(expected):
        return False
    if set(value.get("model_reported_issue_code_counts", {})) != set(
        expected["model_reported_issue_code_counts"]
    ):
        return False
    if set(value.get("model_reported_issue_mentions", {})) != set(
        expected["model_reported_issue_mentions"]
    ):
        return False
    bool_keys = {
        "response_valid",
        "local_ready",
        "remote_ready",
        "template_match",
        "projection_accepted",
    }
    count_keys = {
        "expected_parameter_count",
        "observed_parameter_count",
        "unknown_parameter_count",
        "model_reported_unknown_issue_count",
    }
    list_keys = {
        "missing_parameter_names",
        "unexpected_known_parameter_names",
        "duplicate_parameter_names",
        "numeric_mismatch_keys",
        "unit_mismatch_keys",
        "source_span_mismatch_keys",
    }
    safe_names = {
        "resistance", "capacitance", "cutoff_frequency", "input_voltage",
        "inductance", "input_resistance", "feedback_resistance",
        "positive_supply", "negative_supply", "heat_capacity",
        "thermal_resistance", "initial_temperature", "outdoor_temperature",
        "heat_capacity_a", "heat_capacity_b", "shared_resistance",
        "outdoor_resistance_a", "outdoor_resistance_b",
        "initial_temperature_a", "initial_temperature_b",
    }
    if any(type(value.get(key)) is not bool for key in bool_keys):
        return False
    if any(type(value.get(key)) is not int or value[key] < 0 for key in count_keys):
        return False
    if any(
        not isinstance(value.get(key), list)
        or value[key] != sorted(set(value[key]))
        or any(name not in safe_names for name in value[key])
        for key in list_keys
    ):
        return False
    if any(
        type(count) is not int or count < 0
        for count in value["model_reported_issue_code_counts"].values()
    ):
        return False
    if any(
        type(flag) is not bool
        for flag in value["model_reported_issue_mentions"].values()
    ):
        return False
    if value.get("schema_version") != "v9-projection-probe-inspection-1":
        return False
    if value.get("failure_code") not in {
        "none", "response_schema_invalid", "response_json_invalid",
        "projection_schema_invalid", "local_generation_error", "diagnostic_error",
    }:
        return False
    if value.get("reason_code") not in {
        "accepted", "response_json_invalid", "projection_schema_invalid",
        "unknown_diagnostic", "ready_mismatch", "local_input_blocked",
        "template_mismatch", "parameter_count_mismatch", "duplicate_parameter",
        "parameter_name_mismatch", "unit_mismatch", "source_span_mismatch",
        "value_mismatch",
    }:
        return False
    return True


def _sanitize_worker_result(value: object, row: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "case_id", "api_call_count", "usage",
        "response_valid", "reason_code", "projection_accepted", "inspection",
    }:
        _fail("PROBE_WORKER_RESULT_INVALID")
    usage = value.get("usage")
    if usage is not None:
        if not isinstance(usage, dict) or set(usage) != {
            "prompt_tokens", "completion_tokens", "total_tokens"
        }:
            _fail("PROBE_WORKER_RESULT_INVALID")
        if any(type(item) is not int or item < 0 for item in usage.values()):
            _fail("PROBE_WORKER_RESULT_INVALID")
        if usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]:
            _fail("PROBE_WORKER_RESULT_INVALID")
    inspection = value.get("inspection")
    if (
        value.get("schema_version") != "v9-projection-probe-result-1"
        or value.get("case_id") != row["case_id"]
        or value.get("api_call_count") != 1
        or type(value.get("response_valid")) is not bool
        or type(value.get("projection_accepted")) is not bool
        or value.get("reason_code") not in _INFRASTRUCTURE_REASONS
        | {
            "accepted", "ready_mismatch", "local_input_blocked", "template_mismatch",
            "parameter_count_mismatch", "duplicate_parameter",
            "parameter_name_mismatch", "unit_mismatch", "source_span_mismatch",
            "value_mismatch",
        }
        or not _inspection_shape(inspection)
    ):
        _fail("PROBE_WORKER_RESULT_INVALID")
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _finish_unrun(ledger: dict[str, Any], start: int, reason: str) -> None:
    for row in ledger["rows"][start:]:
        if not row["attempted"]:
            row["status"] = "NOT_RUN"
            row["stop_reason"] = reason


def execute_projection_probe(
    *, protocol_path: str | Path, receipt_path: str | Path, execute: bool = False
) -> dict[str, Any]:
    """Dry-run or execute the fixed two-row projection-only schedule."""

    context = validate_probe_context(
        protocol_path=protocol_path, receipt_path=receipt_path
    )
    protocol = context["probe_protocol"]
    ledger = _new_ledger(context, "online" if execute else "dry_run")
    if not execute:
        _finish_unrun(ledger, 0, "DRY_RUN")
        ledger["batch_status"] = "COMPLETED"
        return ledger

    output_dir = RESULTS_ROOT / protocol["batch_id"]
    try:
        output_dir.mkdir(parents=False, exist_ok=False)
    except (FileExistsError, OSError):
        _fail("PROBE_OUTPUT_EXISTS")
    ledger_path = output_dir / "ledger.json"
    _consume_receipt(context)
    ledger["authorization_consumed"] = True
    _atomic_write(ledger_path, ledger, create=True)
    reserve_tokens, reserve_cost = _reservation(protocol)

    for index, row in enumerate(ledger["rows"]):
        case = next(
            item for item in context["suite"]["cases"]
            if item["case_id"] == row["case_id"]
        )
        if (
            _input_estimate(case["requirement_text"], "B", protocol)
            > protocol["input_token_reservation_per_call"]
        ):
            ledger["budget"]["budget_status"] = "EXHAUSTED"
            _finish_unrun(ledger, index, "BUDGET_EXHAUSTED")
            ledger["batch_status"] = "STOPPED"
            _atomic_write(ledger_path, ledger)
            break
        row["attempted"] = True
        row["status"] = "INTERRUPTED"
        row["stop_reason"] = "INTERRUPTED"
        ledger["budget"]["reserved_tokens"] += reserve_tokens
        ledger["budget"]["reserved_cost_usd"] = round(
            ledger["budget"]["reserved_cost_usd"] + reserve_cost, 12
        )
        _atomic_write(ledger_path, ledger)
        stop_reason = None
        try:
            raw = _run_worker_process(
                protocol_path=Path(protocol_path).resolve(),
                receipt_path=Path(receipt_path).resolve(),
                ledger_path=ledger_path,
                row_index=index,
                batch_id=protocol["batch_id"],
                timeout_s=protocol["attempt_timeout_s"],
            )
            result = _sanitize_worker_result(raw, row)
            row["result"] = result
            row["status"] = "COMPLETED"
            row["stop_reason"] = "COMPLETED"
            usage = result["usage"]
            if usage is not None:
                ledger["budget"]["reserved_tokens"] -= reserve_tokens
                ledger["budget"]["reserved_cost_usd"] = round(
                    ledger["budget"]["reserved_cost_usd"] - reserve_cost, 12
                )
                ledger["budget"]["known_tokens"] += usage["total_tokens"]
                ledger["budget"]["known_cost_usd"] = round(
                    ledger["budget"]["known_cost_usd"]
                    + (
                        usage["prompt_tokens"] * protocol["input_usd_per_million"]
                        + usage["completion_tokens"] * protocol["output_usd_per_million"]
                    ) / 1_000_000,
                    12,
                )
                if (
                    usage["prompt_tokens"]
                    > protocol["input_token_reservation_per_call"]
                    or usage["completion_tokens"] > protocol["max_tokens"]
                ):
                    ledger["budget"]["budget_status"] = "EXHAUSTED"
                    stop_reason = "BUDGET_EXHAUSTED"
            if (
                usage is None
                or not result["response_valid"]
                or result["reason_code"] in _INFRASTRUCTURE_REASONS
            ):
                stop_reason = "INFRASTRUCTURE_ERROR"
        except KeyboardInterrupt:
            row["stop_reason"] = "INTERRUPTED"
            ledger["budget"]["budget_status"] = "STOPPED"
            _finish_unrun(ledger, index + 1, "INTERRUPTED")
            ledger["batch_status"] = "STOPPED"
            _atomic_write(ledger_path, ledger)
            return ledger
        except TimeoutError:
            row["stop_reason"] = "TIMEOUT"
            ledger["budget"]["budget_status"] = "STOPPED"
            stop_reason = "TIMEOUT"
        except ExecutionBlocked:
            row["stop_reason"] = "PRIVACY_OR_EXECUTION_ERROR"
            ledger["budget"]["budget_status"] = "STOPPED"
            stop_reason = "PRIVACY_OR_EXECUTION_ERROR"
        except Exception:
            row["stop_reason"] = "EXECUTION_ERROR"
            ledger["budget"]["budget_status"] = "STOPPED"
            stop_reason = "EXECUTION_ERROR"
        if stop_reason is not None:
            if ledger["budget"]["budget_status"] == "OK":
                ledger["budget"]["budget_status"] = (
                    "EXHAUSTED" if stop_reason == "BUDGET_EXHAUSTED" else "STOPPED"
                )
            _finish_unrun(ledger, index + 1, stop_reason)
            ledger["batch_status"] = "STOPPED"
            _atomic_write(ledger_path, ledger)
            break
        _atomic_write(ledger_path, ledger)
    else:
        ledger["batch_status"] = "COMPLETED"
        _atomic_write(ledger_path, ledger)
    return ledger


def _run_worker_process(
    *,
    protocol_path: Path,
    receipt_path: Path,
    ledger_path: Path,
    row_index: int,
    batch_id: str,
    timeout_s: int | float,
) -> dict[str, Any]:
    command = [
        sys.executable, "-B", "-m", "week10.experiments.run_projection_probe",
        "--_worker", "--protocol", str(protocol_path), "--receipt", str(receipt_path),
        "--ledger", str(ledger_path), "--row-index", str(row_index),
        "--batch-id", batch_id,
    ]
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
    )
    try:
        stdout, _stderr = process.communicate(timeout=float(timeout_s))
    except subprocess.TimeoutExpired as exc:
        _terminate_process_tree(process)
        process.communicate()
        raise TimeoutError from exc
    except KeyboardInterrupt:
        _terminate_process_tree(process)
        process.communicate()
        raise
    if process.returncode != 0:
        _fail("PROBE_WORKER_PROCESS_ERROR")
    try:
        value = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail("PROBE_WORKER_OUTPUT_INVALID")
    if not isinstance(value, dict):
        _fail("PROBE_WORKER_OUTPUT_INVALID")
    return value


def _worker(
    *, protocol_path: Path, receipt_path: Path, ledger_path: Path,
    row_index: int, batch_id: str,
) -> dict[str, Any]:
    context = validate_probe_context(
        protocol_path=protocol_path, receipt_path=receipt_path
    )
    protocol = context["probe_protocol"]
    if batch_id != protocol["batch_id"] or not _BATCH.fullmatch(batch_id):
        _fail("PROBE_WORKER_BATCH_INVALID")
    expected_ledger = (RESULTS_ROOT / batch_id / "ledger.json").resolve()
    if ledger_path.resolve() != expected_ledger:
        _fail("PROBE_WORKER_LEDGER_PATH_INVALID")
    consumed = CONSUMED_ROOT / f"{context['receipt_sha256']}.consumed.json"
    if _load_json(consumed, require_canonical=True) != {
        "schema_version": "v9-projection-probe-authorization-consumed-1",
        "receipt_sha256": context["receipt_sha256"],
        "protocol_sha256": context["probe_protocol_sha256"],
        "batch_id": batch_id,
    }:
        _fail("PROBE_WORKER_CONSUMED_INVALID")
    ledger = _load_json(ledger_path, require_canonical=True)
    expected_rows = _rows(context)
    if (
        set(ledger) != {
            "schema_version", "mode", "batch_id", "probe_protocol_sha256",
            "parent_protocol_sha256", "suite_sha256", "authorization_consumed",
            "batch_status", "rows", "budget",
        }
        or ledger.get("schema_version") != "v9-projection-probe-ledger-1"
        or ledger.get("mode") != "online"
        or ledger.get("batch_id") != batch_id
        or ledger.get("probe_protocol_sha256") != context["probe_protocol_sha256"]
        or ledger.get("parent_protocol_sha256") != context["protocol_sha256"]
        or ledger.get("suite_sha256") != context["suite_sha256"]
        or ledger.get("authorization_consumed") is not True
        or ledger.get("batch_status") != "PLANNED"
        or not isinstance(ledger.get("rows"), list)
        or len(ledger["rows"]) != 2
        or type(row_index) is not int
        or not 0 <= row_index < 2
    ):
        _fail("PROBE_WORKER_LEDGER_INVALID")
    row = ledger["rows"][row_index]
    if any(
        row.get(name) != expected_rows[row_index][name]
        for name in ("run_id", "case_id", "requirement_sha256", "arm")
    ) or (
        row.get("attempted") is not True
        or row.get("status") != "INTERRUPTED"
        or row.get("stop_reason") != "INTERRUPTED"
        or row.get("result") is not None
    ):
        _fail("PROBE_WORKER_ROW_INVALID")
    prior_results: list[dict[str, Any]] = []
    for index, prior in enumerate(ledger["rows"][:row_index]):
        if (
            prior.get("attempted") is not True
            or prior.get("status") != "COMPLETED"
            or prior.get("stop_reason") != "COMPLETED"
        ):
            _fail("PROBE_WORKER_PRIOR_ROW_INVALID")
        checked = _sanitize_worker_result(prior.get("result"), expected_rows[index])
        if (
            checked["usage"] is None
            or checked["response_valid"] is not True
            or checked["reason_code"] in _INFRASTRUCTURE_REASONS
            or checked["usage"]["prompt_tokens"]
            > protocol["input_token_reservation_per_call"]
            or checked["usage"]["completion_tokens"] > protocol["max_tokens"]
        ):
            _fail("PROBE_WORKER_PRIOR_ROW_INVALID")
        prior_results.append(checked)
    if any(later != expected_rows[index] for index, later in enumerate(ledger["rows"][row_index + 1:], row_index + 1)):
        _fail("PROBE_WORKER_LATER_ROW_INVALID")
    reserve_tokens, reserve_cost = _reservation(protocol)
    budget = ledger.get("budget")
    if not isinstance(budget, dict) or set(budget) != {
        "reserved_tokens", "reserved_cost_usd", "known_tokens",
        "known_cost_usd", "budget_status",
    }:
        _fail("PROBE_WORKER_BUDGET_INVALID")
    known_tokens = sum(item["usage"]["total_tokens"] for item in prior_results)
    known_cost = sum(
        (
            item["usage"]["prompt_tokens"] * protocol["input_usd_per_million"]
            + item["usage"]["completion_tokens"] * protocol["output_usd_per_million"]
        ) / 1_000_000
        for item in prior_results
    )
    if (
        budget.get("budget_status") != "OK"
        or budget.get("reserved_tokens") != reserve_tokens
        or not isinstance(budget.get("reserved_cost_usd"), (int, float))
        or not math.isclose(float(budget["reserved_cost_usd"]), reserve_cost, abs_tol=1e-12)
        or budget.get("known_tokens") != known_tokens
        or not isinstance(budget.get("known_cost_usd"), (int, float))
        or not math.isclose(float(budget["known_cost_usd"]), known_cost, abs_tol=1e-12)
        or known_tokens + reserve_tokens > protocol["max_total_tokens"]
        or known_cost + reserve_cost > protocol["max_cost_usd"]
        or row_index + 1 > protocol["max_calls"]
    ):
        _fail("PROBE_WORKER_BUDGET_INVALID")
    case = next(
        item for item in context["suite"]["cases"] if item["case_id"] == row["case_id"]
    )
    requirement = case["requirement_text"]
    if (
        _input_estimate(requirement, "B", protocol)
        > protocol["input_token_reservation_per_call"]
    ):
        _fail("PROBE_WORKER_INPUT_RESERVATION_EXCEEDED")
    dispatch = CONSUMED_ROOT / f"{batch_id}.{row_index}.dispatch.json"
    _atomic_write(
        dispatch,
        {
            "schema_version": "v9-projection-probe-dispatch-1",
            "batch_id": batch_id,
            "row_index": row_index,
            "run_id": row["run_id"],
            "pid": os.getpid(),
        },
        create=True,
    )
    inspection: dict[str, Any] | None = None
    provider: DeepSeekNaturalLanguageProvider

    def transport(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal inspection
        response = provider._post(payload)
        inspection = inspect_response(response, requirement)
        return response

    provider = DeepSeekNaturalLanguageProvider(
        api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
        api_base=protocol["api_base"],
        model=protocol["model"],
        temperature=protocol["temperature"],
        max_tokens=protocol["max_tokens"],
        timeout_s=protocol["http_timeout_s"],
        prompt_arm="B",
        transport=transport,
    )
    try:
        provider.generate(requirement)
    except OnlineGenerationError:
        pass
    if inspection is None:
        inspection = inspect_response({}, requirement)
    diagnostic = provider.last_diagnostic or {
        "reason_code": "unknown_diagnostic",
        "projection_accepted": False,
        "response_valid": False,
    }
    reason_code = diagnostic.get("reason_code", "unknown_diagnostic")
    if reason_code not in _INFRASTRUCTURE_REASONS | {
        "accepted", "ready_mismatch", "local_input_blocked", "template_mismatch",
        "parameter_count_mismatch", "duplicate_parameter", "parameter_name_mismatch",
        "unit_mismatch", "source_span_mismatch", "value_mismatch",
    }:
        reason_code = "unknown_diagnostic"
    response_valid = bool(diagnostic.get("response_valid", False))
    projection_accepted = bool(diagnostic.get("projection_accepted", False))
    if reason_code in _INFRASTRUCTURE_REASONS:
        response_valid = False
        projection_accepted = False
    if not response_valid:
        inspection = dict(inspection)
        inspection["response_valid"] = False
        inspection["failure_code"] = (
            inspection["failure_code"]
            if inspection["failure_code"] != "none"
            else "diagnostic_error"
        )
        inspection["reason_code"] = (
            reason_code
            if reason_code in {"response_json_invalid", "projection_schema_invalid"}
            else "unknown_diagnostic"
        )
        inspection["projection_accepted"] = False
    return {
        "schema_version": "v9-projection-probe-result-1",
        "case_id": row["case_id"],
        "api_call_count": provider.api_call_count,
        "usage": provider.last_usage,
        "response_valid": response_valid,
        "reason_code": reason_code,
        "projection_accepted": projection_accepted,
        "inspection": inspection,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="V9 projection-only probe")
    parser.add_argument("--protocol", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ledger", help=argparse.SUPPRESS)
    parser.add_argument("--row-index", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--batch-id", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args._worker:
            if args.execute or args.ledger is None or args.row_index is None or args.batch_id is None:
                _fail("PROBE_WORKER_ARGUMENT_INVALID")
            result = _worker(
                protocol_path=Path(args.protocol), receipt_path=Path(args.receipt),
                ledger_path=Path(args.ledger), row_index=args.row_index,
                batch_id=args.batch_id,
            )
            sys.stdout.buffer.write(_canonical(result))
            return 0
        if any(value is not None for value in (args.ledger, args.row_index, args.batch_id)):
            _fail("PROBE_ARGUMENT_INVALID")
        ledger = execute_projection_probe(
            protocol_path=args.protocol, receipt_path=args.receipt, execute=args.execute
        )
    except (ExecutionBlocked, OSError, TypeError, ValueError):
        print("BLOCKED: PROBE_EXECUTION_REFUSED", file=sys.stderr)
        return 2
    if args.execute:
        print(
            f"[v9-projection-probe] mode=online batch_status={ledger['batch_status']} "
            f"attempted={sum(row['attempted'] for row in ledger['rows'])}",
            flush=True,
        )
    else:
        sys.stdout.buffer.write(_canonical(ledger))
    return 0 if ledger["batch_status"] == "COMPLETED" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CASES",
    "IMPLEMENTATION_FILES",
    "execute_projection_probe",
    "validate_probe_context",
    "main",
]
