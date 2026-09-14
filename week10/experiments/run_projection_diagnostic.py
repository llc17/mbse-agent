"""Durable, fail-closed executor for the V9 projection diagnostic batch."""

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
from typing import Any, Callable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEEK10_ROOT = PROJECT_ROOT / "week10"
RESULTS_ROOT = WEEK10_ROOT / "results"
DEFAULT_PROTOCOL = WEEK10_ROOT / "configs" / "projection_diagnostic_protocol_v1.json"
DEFAULT_SUITE = WEEK10_ROOT / "configs" / "development_diagnostic_suite.json"
V8_HASH_MANIFEST = WEEK10_ROOT / "configs" / "v8_source_hashes.json"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from week10.experiments.report_projection import (  # noqa: E402
    LEDGER_SCHEMA,
    _THRESHOLDS,
    _validate_result,
    build_schedule,
    summarize,
)
from week9.src.hard_gate import REQUIRED_HARD_CHECK_TYPES  # noqa: E402
from week10.src.projection_contract import V8_SOURCE_HASHES  # noqa: E402
from week10.src.projection_contract_v2 import PROMPT_SHA256  # noqa: E402
from week10.src.online_generation import ONLINE_PROMPT_SHA256  # noqa: E402


Dispatcher = Callable[[dict[str, Any], str, dict[str, Any], Path], object]
_SHA = re.compile(r"^[0-9a-f]{64}$")
_BATCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
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
_ACCEPTANCE_FIELDS = {
    "schema_version",
    "status",
    "local_successes",
    "api_call_count",
    "verifier_drift_count",
    "privacy_leak_count",
    "protected_v8_drift",
    "evidence_sha256",
}
IMPLEMENTATION_FILES = (
    "week10/src/projection_contract.py",
    "week10/src/projection_contract_v2.py",
    "week10/src/projection_diagnostics.py",
    "week10/src/online_generation.py",
    "week10/src/runner.py",
    "week10/experiments/report_projection.py",
    "week10/experiments/run_projection_diagnostic.py",
    "week10/experiments/projection_worker.py",
)


class ExecutionBlocked(RuntimeError):
    """A fixed-code execution refusal safe to show to an operator."""


class PrivacyViolation(ExecutionBlocked):
    """An unsafe worker result that must never be persisted."""


def _fail(code: str) -> None:
    raise ExecutionBlocked(code)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest_path(path: Path) -> str:
    return _digest_bytes(path.read_bytes())


def _load_json(path: Path, *, require_canonical: bool = False) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        _fail("JSON_INPUT_INVALID")
    if not isinstance(value, dict):
        _fail("JSON_INPUT_INVALID")
    if require_canonical and raw != _canonical(value):
        _fail("CANONICAL_JSON_REQUIRED")
    return value


def _atomic_write(path: Path, value: dict[str, Any], *, create: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    if create and (path.exists() or temporary.exists()):
        _fail("OUTPUT_EXISTS")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except FileExistsError:
        _fail("OUTPUT_EXISTS")
    except OSError:
        _fail("LEDGER_WRITE_ERROR")


def _base_validation(protocol: dict[str, Any], suite: dict[str, Any]) -> None:
    required = {
        "schema_version", "status", "scope", "suite_path", "suite_sha256",
        "model", "api_base", "temperature", "max_tokens", "thinking",
        "http_timeout_s", "attempt_timeout_s", "max_calls",
        "input_token_reservation_per_call", "max_total_tokens", "max_cost_usd",
        "input_usd_per_million", "output_usd_per_million", "automatic_retries",
        "local_fallback_allowed", "online_repair_allowed",
        "raw_response_storage_allowed", "positive_repetitions",
        "negative_repetitions", "schedule_policy",
        "stop_on_first_transport_or_execution_error", "thresholds",
        "prompt_sha256", "implementation_sha256", "offline_acceptance_sha256",
        "offline_acceptance_path", "authorization_policy",
        "v8_source_manifest_sha256",
    }
    if not required.issubset(protocol):
        _fail("PROTOCOL_SCHEMA_INVALID")
    if (
        protocol.get("schema_version") != "v9-projection-diagnostic-protocol-1"
        or protocol.get("scope") != "public_development_only"
        or protocol.get("api_base") != "https://api.deepseek.com"
        or protocol.get("model") != "deepseek-v4-flash"
        or protocol.get("thinking") != {"type": "disabled"}
        or protocol.get("automatic_retries") != 0
        or protocol.get("local_fallback_allowed") is not False
        or protocol.get("online_repair_allowed") is not False
        or protocol.get("raw_response_storage_allowed") is not False
        or protocol.get("max_calls") != 24
        or protocol.get("max_tokens") != 8192
        or protocol.get("input_token_reservation_per_call") != 32768
        or type(protocol.get("max_total_tokens")) is not int
        or not 0 < protocol["max_total_tokens"] <= 983040
        or type(protocol.get("max_cost_usd")) not in {int, float}
        or not 0 < float(protocol["max_cost_usd"]) <= 1.0
        or protocol.get("http_timeout_s") != 120
        or protocol.get("attempt_timeout_s") != 180
        or protocol.get("temperature") != 0.3
        or protocol.get("input_usd_per_million") != 0.44
        or protocol.get("output_usd_per_million") != 1.32
        or protocol.get("positive_repetitions") != 3
        or protocol.get("negative_repetitions") != 1
        or protocol.get("schedule_policy")
        != "paired_alternating_ab_ba_positives_then_negatives"
        or protocol.get("stop_on_first_transport_or_execution_error") is not True
        or protocol.get("authorization_policy")
        != "separate_digest_bound_single_batch_receipt_consumed_before_first_request"
        or protocol.get("suite_path")
        != "week10/configs/development_diagnostic_suite.json"
        or protocol.get("prompt_sha256")
        != {"A": ONLINE_PROMPT_SHA256, "B": PROMPT_SHA256}
        or protocol.get("offline_acceptance_path")
        != "week10/configs/offline_acceptance_v1.json"
        or protocol.get("thresholds") != _THRESHOLDS
    ):
        _fail("PROTOCOL_VALUE_INVALID")
    if _digest_bytes(_canonical(suite)) != protocol.get("suite_sha256"):
        _fail("SUITE_HASH_MISMATCH")
    try:
        build_schedule(suite, protocol["prompt_sha256"])
    except Exception:
        _fail("SUITE_SCHEMA_INVALID")


def _validate_source_hashes(protocol: dict[str, Any]) -> None:
    recorded = protocol.get("implementation_sha256")
    if not isinstance(recorded, dict) or set(recorded) != set(IMPLEMENTATION_FILES):
        _fail("IMPLEMENTATION_HASH_SET_INVALID")
    for relative in IMPLEMENTATION_FILES:
        if recorded.get(relative) != _digest_path(PROJECT_ROOT / relative):
            _fail("IMPLEMENTATION_HASH_MISMATCH")
    for relative, expected in V8_SOURCE_HASHES.items():
        if not _SHA.fullmatch(expected) or _digest_path(PROJECT_ROOT / relative) != expected:
            _fail("V8_SOURCE_HASH_MISMATCH")
    if protocol.get("v8_source_manifest_sha256") != _digest_path(V8_HASH_MANIFEST):
        _fail("V8_HASH_MANIFEST_DIGEST_MISMATCH")
    manifest = _load_json(V8_HASH_MANIFEST)
    hashes = manifest.get("sha256")
    if (
        manifest.get("schema_version") != "v9-v8-source-hashes-1"
        or manifest.get("certification_binding_drift") != []
        or not isinstance(hashes, dict)
        or len(hashes) != 33
    ):
        _fail("V8_HASH_MANIFEST_INVALID")
    permitted_result = (
        "week9/results/blinded-formal-20260901T064757Z/"
        "blinded-formal-report.json"
    )
    for relative, expected in hashes.items():
        if (
            not isinstance(relative, str)
            or not isinstance(expected, str)
            or not _SHA.fullmatch(expected)
            or (relative.startswith("week9/results/") and relative != permitted_result)
        ):
            _fail("V8_HASH_MANIFEST_INVALID")
        candidate = (PROJECT_ROOT / relative).resolve()
        if not _is_within(candidate, (PROJECT_ROOT / "week9").resolve()):
            _fail("V8_HASH_MANIFEST_INVALID")
        if _digest_path(candidate) != expected:
            _fail("V8_SOURCE_HASH_MISMATCH")


def _validate_sidecar(protocol_path: Path, protocol_sha256: str, sidecar_path: Path) -> None:
    try:
        value = sidecar_path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        _fail("PROTOCOL_SIDECAR_INVALID")
    if value != protocol_sha256 or not _SHA.fullmatch(value):
        _fail("PROTOCOL_SIDECAR_INVALID")
    if sidecar_path != protocol_path.with_name(protocol_path.name + ".sha256"):
        _fail("PROTOCOL_SIDECAR_PATH_INVALID")


def _validate_acceptance(protocol: dict[str, Any], path: Path) -> None:
    acceptance = _load_json(path, require_canonical=True)
    if set(acceptance) != _ACCEPTANCE_FIELDS:
        _fail("OFFLINE_ACCEPTANCE_SCHEMA_INVALID")
    if (
        acceptance.get("schema_version") != "v9-offline-acceptance-1"
        or acceptance.get("status") != "PASS"
        or acceptance.get("local_successes") != 3
        or acceptance.get("api_call_count") != 0
        or acceptance.get("verifier_drift_count") != 0
        or acceptance.get("privacy_leak_count") != 0
        or acceptance.get("protected_v8_drift") != []
    ):
        _fail("OFFLINE_ACCEPTANCE_FAILED")
    if protocol.get("offline_acceptance_sha256") != _digest_path(path):
        _fail("OFFLINE_ACCEPTANCE_HASH_MISMATCH")
    configured_path = protocol.get("offline_acceptance_path")
    if configured_path != "week10/configs/offline_acceptance_v1.json" or path != (PROJECT_ROOT / configured_path).resolve():
        _fail("OFFLINE_ACCEPTANCE_PATH_INVALID")
    evidence = acceptance.get("evidence_sha256")
    if not isinstance(evidence, dict) or len(evidence) < 3:
        _fail("OFFLINE_ACCEPTANCE_EVIDENCE_INVALID")
    local_summary_found = False
    differential_found = False
    test_summary_found = False
    expected_local_cases = {"rc_lowpass", "rlc_lowpass", "dual_room_thermal"}
    for relative, expected in evidence.items():
        if (
            not isinstance(relative, str)
            or not isinstance(expected, str)
            or not _SHA.fullmatch(expected)
        ):
            _fail("OFFLINE_ACCEPTANCE_EVIDENCE_INVALID")
        evidence_path = (PROJECT_ROOT / relative).resolve()
        if not _is_within(evidence_path, WEEK10_ROOT.resolve()) or not evidence_path.is_file():
            _fail("OFFLINE_ACCEPTANCE_EVIDENCE_INVALID")
        if _digest_path(evidence_path) != expected:
            _fail("OFFLINE_ACCEPTANCE_EVIDENCE_HASH_MISMATCH")
        candidate = _load_json(evidence_path)
        if candidate.get("schema_version") == "v9-local-regression-summary-1":
            summary = candidate
            if (
                summary.get("scope") != "three_public_development_positives"
                or summary.get("real_execution") is not True
                or summary.get("successes") != 3
                or summary.get("api_call_count") != 0
                or summary.get("protected_v8_files_checked") != 33
                or summary.get("protected_v8_drift") != []
                or summary.get("online_hypothesis_status") != "NOT_RUN"
                or not isinstance(summary.get("cases"), list)
                or len(summary["cases"]) != 3
            ):
                _fail("LOCAL_ACCEPTANCE_INVALID")
            seen_cases: set[str] = set()
            for item in summary["cases"]:
                if (
                    not isinstance(item, dict)
                    or item.get("case_id") not in expected_local_cases
                    or item.get("case_id") in seen_cases
                    or item.get("published") is not True
                    or item.get("api_call_count") != 0
                    or item.get("required_checks_passed") != 10
                    or not isinstance(item.get("result_path"), str)
                    or not isinstance(item.get("result_sha256"), str)
                    or not _SHA.fullmatch(item["result_sha256"])
                ):
                    _fail("LOCAL_ACCEPTANCE_INVALID")
                seen_cases.add(item["case_id"])
                result_path = (PROJECT_ROOT / item["result_path"]).resolve()
                if (
                    not _is_within(result_path, (WEEK10_ROOT / "results").resolve())
                    or not result_path.is_file()
                    or _digest_path(result_path) != item["result_sha256"]
                ):
                    _fail("LOCAL_ACCEPTANCE_RESULT_HASH_MISMATCH")
                result = _load_json(result_path)
                if (
                    result.get("case_id") != item["case_id"]
                    or result.get("api_call_count") != 0
                    or result.get("pipeline_success") is not True
                    or result.get("hard_gate_passed") is not True
                    or result.get("quality_decision_passed") is not True
                    or result.get("published") is not True
                    or result.get("required_check_statuses")
                    != {name: "PASS" for name in REQUIRED_HARD_CHECK_TYPES}
                ):
                    _fail("LOCAL_ACCEPTANCE_RESULT_INVALID")
            if seen_cases != expected_local_cases:
                _fail("LOCAL_ACCEPTANCE_INVALID")
            local_summary_found = True
        elif candidate.get("schema_version") == "v9-projection-differential-audit-1":
            if (
                candidate.get("comparisons") != 222
                or candidate.get("mismatches") != 0
                or candidate.get("leaks") != 0
                or candidate.get("online_api_calls") != 0
            ):
                _fail("DIFFERENTIAL_ACCEPTANCE_INVALID")
            differential_found = True
        elif candidate.get("schema_version") == "v9-offline-test-summary-1":
            test_summary = candidate
            if (
                type(test_summary.get("tests_run")) is not int
                or test_summary["tests_run"] <= 0
                or test_summary.get("failures") != 0
                or test_summary.get("errors") != 0
                or test_summary.get("skipped") != 0
                or test_summary.get("online_api_calls") != 0
            ):
                _fail("TEST_ACCEPTANCE_INVALID")
            test_summary_found = True
    if not local_summary_found:
        _fail("LOCAL_ACCEPTANCE_MISSING")
    if not differential_found:
        _fail("DIFFERENTIAL_ACCEPTANCE_MISSING")
    if not test_summary_found:
        _fail("TEST_ACCEPTANCE_MISSING")


def _validate_receipt(
    protocol: dict[str, Any], protocol_sha256: str, receipt_path: Path
) -> tuple[dict[str, Any], str]:
    if not receipt_path.is_file():
        _fail("AUTHORIZATION_REQUIRED")
    receipt = _load_json(receipt_path, require_canonical=True)
    if set(receipt) != _RECEIPT_FIELDS:
        _fail("AUTHORIZATION_SCHEMA_INVALID")
    if (
        receipt.get("schema_version") != "v9-projection-diagnostic-authorization-1"
        or receipt.get("approved") is not True
        or receipt.get("protocol_sha256") != protocol_sha256
        or receipt.get("data_scope") != "public_development_only"
        or receipt.get("max_calls") != protocol.get("max_calls")
        or receipt.get("max_total_tokens") != protocol.get("max_total_tokens")
        or receipt.get("max_cost_usd") != protocol.get("max_cost_usd")
        or not isinstance(receipt.get("batch_id"), str)
        or not _BATCH.fullmatch(receipt["batch_id"])
    ):
        _fail("AUTHORIZATION_INVALID")
    expires_at = receipt.get("expires_at")
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        _fail("AUTHORIZATION_EXPIRY_INVALID")
    if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
        _fail("AUTHORIZATION_EXPIRED")
    return receipt, _digest_path(receipt_path)


def validate_frozen_context(
    *,
    protocol_path: str | Path,
    suite_path: str | Path,
    protocol_sidecar_path: str | Path,
    offline_acceptance_path: str | Path,
) -> dict[str, Any]:
    """Validate immutable inputs for a frozen run without an authorization receipt."""

    protocol_file = Path(protocol_path).resolve()
    suite_file = Path(suite_path).resolve()
    if (
        protocol_file != DEFAULT_PROTOCOL.resolve()
        or suite_file != DEFAULT_SUITE.resolve()
    ):
        _fail("ONLINE_CONTROL_PATH_INVALID")
    protocol = _load_json(protocol_file, require_canonical=True)
    suite = _load_json(suite_file, require_canonical=True)
    _base_validation(protocol, suite)
    if protocol.get("status") not in {"FROZEN", "FROZEN_NOT_AUTHORIZED"}:
        _fail("PROTOCOL_NOT_FROZEN")
    protocol_sha256 = _digest_path(protocol_file)
    suite_sha256 = _digest_path(suite_file)
    if protocol.get("suite_sha256") != suite_sha256:
        _fail("SUITE_BYTE_HASH_MISMATCH")
    _validate_source_hashes(protocol)
    _validate_sidecar(
        protocol_file,
        protocol_sha256,
        Path(protocol_sidecar_path).resolve(),
    )
    _validate_acceptance(protocol, Path(offline_acceptance_path).resolve())
    return {
        "protocol": protocol,
        "suite": suite,
        "schedule": build_schedule(suite, protocol["prompt_sha256"]),
        "protocol_sha256": protocol_sha256,
        "suite_sha256": suite_sha256,
    }


def validate_online_context(
    *,
    protocol_path: str | Path,
    suite_path: str | Path,
    protocol_sidecar_path: str | Path,
    offline_acceptance_path: str | Path,
    authorization_receipt_path: str | Path,
) -> dict[str, Any]:
    """Validate the frozen boundary and its one-batch authorization receipt."""

    context = validate_frozen_context(
        protocol_path=protocol_path,
        suite_path=suite_path,
        protocol_sidecar_path=protocol_sidecar_path,
        offline_acceptance_path=offline_acceptance_path,
    )
    protocol_file = Path(protocol_path).resolve()
    receipt_file = Path(authorization_receipt_path).resolve()
    protocol = context["protocol"]
    protocol_sha256 = context["protocol_sha256"]
    receipt, receipt_sha256 = _validate_receipt(
        protocol, protocol_sha256, receipt_file
    )
    expected_receipt = protocol_file.parent / "authorization_receipts" / (
        receipt["batch_id"] + ".json"
    )
    if receipt_file != expected_receipt.resolve():
        _fail("AUTHORIZATION_PATH_INVALID")
    return {
        **context,
        "receipt": receipt,
        "receipt_sha256": receipt_sha256,
    }


def _consume_receipt(
    root: Path,
    *,
    receipt: dict[str, Any],
    receipt_sha256: str,
    protocol_sha256: str,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    marker = root / f"{receipt_sha256}.consumed.json"
    payload = {
        "schema_version": "v9-projection-authorization-consumed-1",
        "receipt_sha256": receipt_sha256,
        "protocol_sha256": protocol_sha256,
        "batch_id": receipt["batch_id"],
    }
    try:
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical(payload))
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        _fail("AUTHORIZATION_ALREADY_CONSUMED")
    except OSError:
        _fail("AUTHORIZATION_CONSUME_ERROR")
    return marker


def _reservation(protocol: dict[str, Any]) -> tuple[int, float]:
    input_tokens = protocol["input_token_reservation_per_call"]
    output_tokens = protocol["max_tokens"]
    cost = (
        input_tokens * protocol["input_usd_per_million"]
        + output_tokens * protocol["output_usd_per_million"]
    ) / 1_000_000
    return input_tokens + output_tokens, cost


def _input_estimate(
    text: str, arm: str, protocol: dict[str, Any]
) -> int:
    # Constructing a provider and its public payload is offline.  This measures
    # the exact serialized request body that the worker will send, then adds the
    # frozen 1024-token conservative margin required by the protocol.
    from week10.src.online_generation import DeepSeekNaturalLanguageProvider

    provider = DeepSeekNaturalLanguageProvider(
        api_key="offline-size-estimate",
        api_base=protocol["api_base"],
        model=protocol["model"],
        temperature=protocol["temperature"],
        max_tokens=protocol["max_tokens"],
        timeout_s=protocol["http_timeout_s"],
        prompt_arm=arm,
        transport=lambda payload: {},
    )
    return len(_canonical(provider._request_payload(text))) + 1024


def _new_ledger(
    *, mode: str, protocol_sha256: str, suite_sha256: str, rows: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "schema_version": LEDGER_SCHEMA,
        "mode": mode,
        "protocol_sha256": protocol_sha256,
        "suite_sha256": suite_sha256,
        "batch_status": "PLANNED",
        "rows": rows,
        "budget": {
            "reserved_tokens": 0,
            "reserved_cost_usd": 0.0,
            "known_cost_usd": 0.0,
            "budget_status": "OK",
        },
        "offline_acceptance_verified": mode == "online",
    }


def _sanitize_result(value: object, row: dict[str, Any]) -> dict[str, Any]:
    if hasattr(value, "public_dict"):
        value = value.public_dict()
    if not isinstance(value, dict):
        raise PrivacyViolation("WORKER_RESULT_INVALID")
    try:
        copied = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
        _validate_result(copied, row)
        if copied["api_call_count"] != 1:
            _fail("API_CALL_COUNT_INVALID")
    except Exception as exc:
        raise PrivacyViolation("WORKER_RESULT_INVALID") from exc
    return copied


def _stop_for_result(row: dict[str, Any]) -> str | None:
    result = row["result"]
    if row["positive"] is False and result["published"]:
        return "FALSE_RELEASE"
    reason = result["reason_code"]
    if reason in {"response_json_invalid", "projection_schema_invalid"}:
        return None
    if reason == "api_transport_error":
        return "API_TRANSPORT_ERROR"
    if reason == "response_model_mismatch":
        return "MODEL_MISMATCH"
    if reason == "usage_contract_invalid":
        return "USAGE_INVALID"
    if result["stage"] == "pipeline_error" or reason in {
        "response_contract_invalid", "local_generation_error", "unknown_diagnostic"
    }:
        return "EXECUTION_ERROR"
    return None


def _finish_unrun(ledger: dict[str, Any], start: int, reason: str) -> None:
    for row in ledger["rows"][start:]:
        if not row["attempted"]:
            row["status"] = "NOT_RUN"
            row["stop_reason"] = reason


def _known_totals(ledger: dict[str, Any]) -> tuple[int, int]:
    calls = 0
    tokens = 0
    for row in ledger["rows"]:
        result = row.get("result")
        if isinstance(result, dict):
            calls += result["api_call_count"]
            usage = result.get("usage")
            if isinstance(usage, dict):
                tokens += usage["total_tokens"]
    calls += sum(row["attempted"] and row["result"] is None for row in ledger["rows"])
    return calls, tokens


def _rebuild_budget_from_rows(
    ledger: dict[str, Any], protocol: dict[str, Any], *, status: str
) -> None:
    """Reconstruct conservative accounting after an exceptional exit."""

    reserve_tokens, reserve_cost = _reservation(protocol)
    reserved_tokens = 0
    reserved_cost = 0.0
    known_cost = 0.0
    for row in ledger["rows"]:
        if not row.get("attempted"):
            continue
        result = row.get("result")
        usage = result.get("usage") if isinstance(result, dict) else None
        if isinstance(usage, dict):
            known_cost += (
                usage["prompt_tokens"] * protocol["input_usd_per_million"]
                + usage["completion_tokens"] * protocol["output_usd_per_million"]
            ) / 1_000_000
        else:
            reserved_tokens += reserve_tokens
            reserved_cost += reserve_cost
    ledger["budget"] = {
        "reserved_tokens": reserved_tokens,
        "reserved_cost_usd": round(reserved_cost, 12),
        "known_cost_usd": round(known_cost, 12),
        "budget_status": status,
    }


def execute_projection_diagnostic(
    *,
    protocol_path: str | Path,
    suite_path: str | Path,
    output_path: str | Path,
    mode: str = "dry_run",
    dispatcher: Dispatcher | None = None,
    authorization_receipt_path: str | Path | None = None,
    protocol_sidecar_path: str | Path | None = None,
    offline_acceptance_path: str | Path | None = None,
    consumed_root: str | Path | None = None,
) -> dict[str, Any]:
    """Create and durably execute the fixed schedule without retries."""

    if mode not in {"dry_run", "offline_mock", "online"}:
        _fail("MODE_INVALID")
    if mode == "online" and dispatcher is not None:
        _fail("ONLINE_DISPATCH_OVERRIDE_FORBIDDEN")
    if mode == "offline_mock" and dispatcher is None:
        _fail("OFFLINE_MOCK_DISPATCH_REQUIRED")
    protocol_file = Path(protocol_path).resolve()
    suite_file = Path(suite_path).resolve()
    output_file = Path(output_path).resolve()
    if output_file.exists():
        _fail("OUTPUT_EXISTS")
    protocol = _load_json(protocol_file, require_canonical=True)
    suite = _load_json(suite_file, require_canonical=True)
    _base_validation(protocol, suite)
    protocol_sha256 = _digest_path(protocol_file)
    suite_sha256 = _digest_path(suite_file)
    if protocol["suite_sha256"] != suite_sha256:
        _fail("SUITE_BYTE_HASH_MISMATCH")
    if protocol.get("status") in {"FROZEN", "FROZEN_NOT_AUTHORIZED"}:
        sidecar = (
            Path(protocol_sidecar_path).resolve()
            if protocol_sidecar_path is not None
            else protocol_file.with_name(protocol_file.name + ".sha256")
        )
        acceptance = (
            Path(offline_acceptance_path).resolve()
            if offline_acceptance_path is not None
            else (PROJECT_ROOT / protocol["offline_acceptance_path"]).resolve()
        )
        frozen = validate_frozen_context(
            protocol_path=protocol_file,
            suite_path=suite_file,
            protocol_sidecar_path=sidecar,
            offline_acceptance_path=acceptance,
        )
        protocol = frozen["protocol"]
        suite = frozen["suite"]
        protocol_sha256 = frozen["protocol_sha256"]
        suite_sha256 = frozen["suite_sha256"]
    rows = build_schedule(suite, protocol["prompt_sha256"])
    ledger = _new_ledger(
        mode=mode,
        protocol_sha256=protocol_sha256,
        suite_sha256=suite_sha256,
        rows=rows,
    )
    if mode == "dry_run":
        _finish_unrun(ledger, 0, "DRY_RUN")
        ledger["batch_status"] = "COMPLETED"
        _atomic_write(output_file, ledger, create=True)
        return ledger

    receipt = None
    receipt_sha256 = ""
    consumed_marker = None
    if mode == "online":
        if any(
            value is None
            for value in (
                protocol_sidecar_path,
                offline_acceptance_path,
                authorization_receipt_path,
            )
        ):
            _fail("ONLINE_AUTHORIZATION_INPUT_REQUIRED")
        receipt_file = Path(authorization_receipt_path).resolve()
        context = validate_online_context(
            protocol_path=protocol_file,
            suite_path=suite_file,
            protocol_sidecar_path=protocol_sidecar_path,
            offline_acceptance_path=offline_acceptance_path,
            authorization_receipt_path=receipt_file,
        )
        receipt = context["receipt"]
        receipt_sha256 = context["receipt_sha256"]
        expected_output = RESULTS_ROOT / receipt["batch_id"] / "ledger.json"
        if output_file != expected_output.resolve():
            _fail("ONLINE_OUTPUT_PATH_INVALID")
        if consumed_root is not None:
            _fail("ONLINE_CONSUMED_ROOT_OVERRIDE_FORBIDDEN")
    else:
        receipt_file = Path(".")

    _atomic_write(output_file, ledger, create=True)
    if mode == "online":
        marker_root = RESULTS_ROOT / ".v9_projection_receipts"
        consumed_marker = _consume_receipt(
            marker_root,
            receipt=receipt,
            receipt_sha256=receipt_sha256,
            protocol_sha256=protocol_sha256,
        )

    case_text = {item["case_id"]: item["requirement_text"] for item in suite["cases"]}
    reserve_tokens, reserve_cost = _reservation(protocol)
    stop_reason = None
    for index, row in enumerate(ledger["rows"]):
        text = case_text[row["case_id"]]
        if _input_estimate(text, row["arm"], protocol) > protocol["input_token_reservation_per_call"]:
            stop_reason = "BUDGET_EXHAUSTED"
            ledger["budget"]["budget_status"] = "EXHAUSTED"
            _finish_unrun(ledger, index, stop_reason)
            break
        calls, known_tokens = _known_totals(ledger)
        budget = ledger["budget"]
        if (
            calls + 1 > protocol["max_calls"]
            or known_tokens + budget["reserved_tokens"] + reserve_tokens
            > protocol["max_total_tokens"]
            or budget["known_cost_usd"] + budget["reserved_cost_usd"] + reserve_cost
            > protocol["max_cost_usd"]
        ):
            stop_reason = "BUDGET_EXHAUSTED"
            budget["budget_status"] = "EXHAUSTED"
            _finish_unrun(ledger, index, stop_reason)
            break

        row["attempted"] = True
        row["status"] = "INTERRUPTED"
        row["stop_reason"] = "INTERRUPTED"
        budget["reserved_tokens"] += reserve_tokens
        budget["reserved_cost_usd"] = round(
            budget["reserved_cost_usd"] + reserve_cost, 12
        )
        _atomic_write(output_file, ledger)
        try:
            if mode == "offline_mock":
                raw_result = dispatcher(row.copy(), text, protocol.copy(), output_file.parent)
            else:
                raw_result = _run_worker_process(
                    ledger_path=output_file,
                    result_path=output_file.parent / f".{row['run_id']}.result.json",
                    row_index=index,
                    batch_id=receipt["batch_id"],
                    timeout_s=protocol["attempt_timeout_s"],
                )
            result = _sanitize_result(raw_result, row)
            row["result"] = result
            row["status"] = "COMPLETED"
            row["stop_reason"] = "COMPLETED"
            usage = result["usage"]
            if usage is not None:
                budget["reserved_tokens"] -= reserve_tokens
                budget["reserved_cost_usd"] = round(
                    max(0.0, budget["reserved_cost_usd"] - reserve_cost), 12
                )
                budget["known_cost_usd"] = round(
                    budget["known_cost_usd"]
                    + (
                        usage["prompt_tokens"] * protocol["input_usd_per_million"]
                        + usage["completion_tokens"] * protocol["output_usd_per_million"]
                    ) / 1_000_000,
                    12,
                )
                if (
                    usage["prompt_tokens"] > protocol["input_token_reservation_per_call"]
                    or usage["completion_tokens"] > protocol["max_tokens"]
                    or _known_totals(ledger)[1] + budget["reserved_tokens"]
                    > protocol["max_total_tokens"]
                    or budget["known_cost_usd"] + budget["reserved_cost_usd"]
                    > protocol["max_cost_usd"]
                ):
                    budget["budget_status"] = "EXHAUSTED"
                    stop_reason = "BUDGET_EXHAUSTED"
            if stop_reason is None:
                stop_reason = _stop_for_result(row)
        except KeyboardInterrupt:
            if row.get("result") is not None:
                row["status"] = "COMPLETED"
                row["stop_reason"] = "COMPLETED"
            else:
                row["status"] = "INTERRUPTED"
                row["stop_reason"] = "INTERRUPTED"
            ledger["batch_status"] = "INTERRUPTED"
            _rebuild_budget_from_rows(ledger, protocol, status="STOPPED")
            _finish_unrun(ledger, index + 1, "INTERRUPTED")
            _atomic_write(output_file, ledger)
            return ledger
        except TimeoutError:
            if row.get("result") is not None:
                row["status"] = "COMPLETED"
                row["stop_reason"] = "COMPLETED"
            else:
                row["status"] = "INTERRUPTED"
                row["stop_reason"] = "TIMEOUT"
            stop_reason = "TIMEOUT"
            _rebuild_budget_from_rows(ledger, protocol, status="STOPPED")
        except PrivacyViolation:
            if row.get("result") is not None:
                row["status"] = "COMPLETED"
                row["stop_reason"] = "COMPLETED"
            else:
                row["status"] = "INTERRUPTED"
                row["stop_reason"] = "PRIVACY_ERROR"
            stop_reason = "PRIVACY_ERROR"
            _rebuild_budget_from_rows(ledger, protocol, status="STOPPED")
        except ExecutionBlocked:
            if row.get("result") is not None:
                row["status"] = "COMPLETED"
                row["stop_reason"] = "COMPLETED"
            else:
                row["status"] = "INTERRUPTED"
                row["stop_reason"] = "EXECUTION_ERROR"
            stop_reason = "EXECUTION_ERROR"
            _rebuild_budget_from_rows(ledger, protocol, status="STOPPED")
        except Exception:
            if row.get("result") is not None:
                row["status"] = "COMPLETED"
                row["stop_reason"] = "COMPLETED"
            else:
                row["status"] = "INTERRUPTED"
                row["stop_reason"] = "EXECUTION_ERROR"
            stop_reason = "EXECUTION_ERROR"
            _rebuild_budget_from_rows(ledger, protocol, status="STOPPED")
        if stop_reason is not None:
            _rebuild_budget_from_rows(
                ledger,
                protocol,
                status=(
                    "EXHAUSTED"
                    if stop_reason == "BUDGET_EXHAUSTED"
                    else "STOPPED"
                ),
            )
            _finish_unrun(ledger, index + 1, stop_reason)
        _atomic_write(output_file, ledger)
        if stop_reason is not None:
            break

    if stop_reason is None:
        ledger["batch_status"] = "COMPLETED"
    else:
        ledger["batch_status"] = "STOPPED"
    _atomic_write(output_file, ledger)
    return ledger


def _run_worker_process(
    *,
    ledger_path: Path,
    result_path: Path,
    row_index: int,
    batch_id: str,
    timeout_s: int | float,
) -> dict[str, Any]:
    command = [
        sys.executable, "-B", "-m", "week10.experiments.projection_worker",
        "--ledger", str(ledger_path), "--row-index", str(row_index),
        "--batch-id", batch_id,
    ]
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    try:
        process.wait(timeout=float(timeout_s))
    except subprocess.TimeoutExpired as exc:
        _terminate_process_tree(process)
        raise TimeoutError from exc
    except KeyboardInterrupt:
        _terminate_process_tree(process)
        raise
    if process.returncode != 0 or not result_path.is_file():
        _fail("WORKER_PROCESS_ERROR")
    result = _load_json(result_path)
    return result


def _terminate_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        else:
            process.kill()
        process.wait(timeout=15)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="V9 projection diagnostic executor")
    parser.add_argument("--protocol", default=str(DEFAULT_PROTOCOL))
    parser.add_argument("--suite", default=str(DEFAULT_SUITE))
    parser.add_argument("--out", required=True)
    parser.add_argument("--execute-online", action="store_true")
    parser.add_argument("--authorization-receipt")
    parser.add_argument("--protocol-sidecar")
    parser.add_argument("--offline-acceptance")
    args = parser.parse_args(argv)
    output = Path(args.out).resolve()
    if (
        not _is_within(output, RESULTS_ROOT.resolve())
        or output.name != "ledger.json"
    ):
        print("BLOCKED: OUTPUT_PATH_INVALID", file=sys.stderr)
        return 2
    if output.exists() or output.parent.exists():
        print("BLOCKED: OUTPUT_EXISTS", file=sys.stderr)
        return 2
    try:
        ledger = execute_projection_diagnostic(
            protocol_path=args.protocol,
            suite_path=args.suite,
            output_path=output,
            mode="online" if args.execute_online else "dry_run",
            authorization_receipt_path=args.authorization_receipt,
            protocol_sidecar_path=args.protocol_sidecar,
            offline_acceptance_path=args.offline_acceptance,
        )
        protocol = _load_json(Path(args.protocol).resolve(), require_canonical=True)
        suite = _load_json(Path(args.suite).resolve(), require_canonical=True)
        report = summarize(ledger, suite, protocol)
        _atomic_write(output.parent / "report.json", report, create=True)
    except (ExecutionBlocked, OSError, TypeError, ValueError):
        print("BLOCKED: EXECUTION_REFUSED", file=sys.stderr)
        return 2
    print(
        f"[v9-projection] mode={ledger['mode']} batch_status={ledger['batch_status']} "
        f"attempted={sum(row['attempted'] for row in ledger['rows'])} out={output}",
        flush=True,
    )
    return 0 if ledger["mode"] == "dry_run" or ledger["batch_status"] == "COMPLETED" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ExecutionBlocked",
    "PrivacyViolation",
    "IMPLEMENTATION_FILES",
    "validate_frozen_context",
    "validate_online_context",
    "execute_projection_diagnostic",
    "main",
]
