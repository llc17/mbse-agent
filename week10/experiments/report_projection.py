"""Fail-closed, offline aggregation for the V9 projection diagnostic ledger."""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Any
from week9.src.hard_gate import REQUIRED_HARD_CHECK_TYPES
from week10.src.projection_contract import V8_SOURCE_HASHES


LEDGER_SCHEMA = "v9-projection-diagnostic-ledger-1"
REPORT_SCHEMA = "v9-projection-diagnostic-report-1"
_SHA = re.compile(r"^[0-9a-f]{64}$")
_CASE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RUN = re.compile(r"^v9pd-[0-9]{3}$")
_TOP = {"schema_version", "mode", "protocol_sha256", "suite_sha256", "batch_status", "rows", "budget", "offline_acceptance_verified"}
_ROW = {"run_id", "case_id", "arm", "positive", "repetition", "requirement_sha256", "prompt_sha256", "status", "attempted", "result", "stop_reason"}
_RESULT = {"schema_version", "version", "case_id", "stage", "provider", "prompt_arm", "prompt_sha256", "v8_online_generation_sha256", "response_model", "response_valid", "usage", "api_call_count", "reason_code", "parameter_keys", "legacy_disposition", "projection_accepted", "local_fallback_used", "pipeline_attempted", "pipeline_success", "hard_gate_passed", "quality_decision_passed", "published", "required_check_statuses", "run_result_version", "duration_s"}
_MODES = {"dry_run", "offline_mock", "online"}
_BATCH = {"PLANNED", "COMPLETED", "STOPPED", "INTERRUPTED"}
_STATUS = {"PLANNED", "COMPLETED", "NOT_RUN", "INTERRUPTED"}
_STOP = {"NOT_STARTED", "COMPLETED", "DRY_RUN", "API_TRANSPORT_ERROR", "TIMEOUT", "INTERRUPTED", "BUDGET_EXHAUSTED", "MODEL_MISMATCH", "USAGE_INVALID", "EXECUTION_ERROR", "PRIVACY_ERROR", "HASH_MISMATCH", "FALSE_RELEASE"}
_STAGE = {"generation_error", "generation_blocked", "pipeline_error", "pipeline_complete"}
_REASONS = {"accepted", "response_json_invalid", "projection_schema_invalid", "unknown_diagnostic", "ready_mismatch", "local_input_blocked", "template_mismatch", "parameter_count_mismatch", "duplicate_parameter", "parameter_name_mismatch", "unit_mismatch", "source_span_mismatch", "value_mismatch", "api_transport_error", "response_contract_invalid", "response_model_mismatch", "usage_contract_invalid", "local_generation_error"}
_PARAMETERS = {"resistance", "capacitance", "cutoff_frequency", "input_voltage", "inductance", "input_resistance", "feedback_resistance", "positive_supply", "negative_supply", "heat_capacity", "thermal_resistance", "initial_temperature", "outdoor_temperature", "heat_capacity_a", "heat_capacity_b", "shared_resistance", "outdoor_resistance_a", "outdoor_resistance_b", "initial_temperature_a", "initial_temperature_b", "unknown_parameter"}
_BUDGET = {"reserved_tokens", "reserved_cost_usd", "known_cost_usd", "budget_status"}
_BUDGET_STATUS = {"OK", "EXHAUSTED", "STOPPED"}
_CHECK_STATUSES = {"PASS", "FAIL", "ERROR", "UNVERIFIED", "NOT_APPLICABLE", "MISSING"}
_THRESHOLDS = {"b_positive_accepted_min": 8, "b_each_positive_accepted_min": 2, "b_minus_a_positive_accepted_min": 3, "b_positive_hard_success_min": 8, "b_all_accepted_positive_hard_success_required": True, "negative_published_max": 0, "required_default_local_successes": 3}


def _fail(code: str) -> None:
    raise ValueError(code)


def _sha(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA.fullmatch(value))


def _suite_cases(suite: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(suite, dict) or set(suite) != {"schema_version", "source", "cases"} or suite["schema_version"] != "v9-development-diagnostic-suite-1" or not isinstance(suite["cases"], list): _fail("SUITE_SCHEMA_INVALID")
    cases = suite["cases"]
    if len(cases) != 6: _fail("SUITE_CASE_COUNT_INVALID")
    seen: set[str] = set()
    for item in cases:
        if not isinstance(item, dict) or set(item) != {"case_id", "positive", "requirement_text", "expected_template_id", "requirement_sha256"}: _fail("SUITE_CASE_SCHEMA_INVALID")
        case_id = item["case_id"]
        if not isinstance(case_id, str) or not _CASE.fullmatch(case_id) or case_id in seen or type(item["positive"]) is not bool or not isinstance(item["requirement_text"], str) or not item["requirement_text"] or not _sha(item["requirement_sha256"]): _fail("SUITE_CASE_INVALID")
        if hashlib.sha256(item["requirement_text"].encode("utf-8")).hexdigest() != item["requirement_sha256"]: _fail("SUITE_HASH_MISMATCH")
        seen.add(case_id)
    if sum(item["positive"] for item in cases) != 3: _fail("SUITE_POLARITY_INVALID")
    return cases


def build_schedule(suite: dict, prompt_hashes: dict) -> list[dict]:
    """Build the only permitted 24-row AB/BA diagnostic schedule."""
    cases = _suite_cases(suite)
    if not isinstance(prompt_hashes, dict) or set(prompt_hashes) != {"A", "B"} or not all(_sha(value) for value in prompt_hashes.values()): _fail("PROMPT_HASHES_INVALID")
    rows: list[dict] = []
    pair = 0
    positives, negatives = [c for c in cases if c["positive"]], [c for c in cases if not c["positive"]]
    for repetition in range(1, 4):
        for case in positives:
            arms = ("A", "B") if pair % 2 == 0 else ("B", "A")
            pair += 1
            for arm in arms:
                rows.append({"run_id": f"v9pd-{len(rows)+1:03d}", "case_id": case["case_id"], "arm": arm, "positive": case["positive"], "repetition": repetition, "requirement_sha256": case["requirement_sha256"], "prompt_sha256": prompt_hashes[arm], "status": "PLANNED", "attempted": False, "result": None, "stop_reason": "NOT_STARTED"})
    for case in negatives:
        arms = ("A", "B") if pair % 2 == 0 else ("B", "A")
        pair += 1
        for arm in arms:
            rows.append({"run_id": f"v9pd-{len(rows)+1:03d}", "case_id": case["case_id"], "arm": arm, "positive": False, "repetition": 1, "requirement_sha256": case["requirement_sha256"], "prompt_sha256": prompt_hashes[arm], "status": "PLANNED", "attempted": False, "result": None, "stop_reason": "NOT_STARTED"})
    if len(rows) != 24: _fail("SCHEDULE_COUNT_INVALID")
    return rows


def _validate_result(value: Any, row: dict[str, Any]) -> None:
    if not isinstance(value, dict) or set(value) != _RESULT: _fail("RESULT_SCHEMA_INVALID")
    if value["case_id"] != row["case_id"] or value["prompt_arm"] != row["arm"] or value["prompt_sha256"] != row["prompt_sha256"]: _fail("RESULT_METADATA_DRIFT")
    if value["schema_version"] != "v9-integration-result-1" or value["version"] != "v9" or value["provider"] != "deepseek" or value["v8_online_generation_sha256"] != V8_SOURCE_HASHES["week9/src/online_generation.py"] or value["stage"] not in _STAGE or value["reason_code"] not in _REASONS or value["legacy_disposition"] not in {"accepted", "content_rejection", "infrastructure_error"} or type(value["response_valid"]) is not bool or type(value["api_call_count"]) is not int or value["api_call_count"] not in {0, 1}: _fail("RESULT_ENUM_INVALID")
    if value["usage"] is not None:
        if not isinstance(value["usage"], dict) or set(value["usage"]) != {"prompt_tokens", "completion_tokens", "total_tokens"} or any(type(v) is not int or v < 0 for v in value["usage"].values()) or value["usage"]["total_tokens"] != value["usage"]["prompt_tokens"] + value["usage"]["completion_tokens"]: _fail("USAGE_SCHEMA_INVALID")
    for key in ("projection_accepted", "pipeline_attempted", "pipeline_success", "hard_gate_passed", "quality_decision_passed", "published"):
        if value[key] is not None and type(value[key]) is not bool: _fail("RESULT_BOOLEAN_INVALID")
    if type(value["local_fallback_used"]) is not bool or value["local_fallback_used"] or not isinstance(value["parameter_keys"], list) or any(v not in _PARAMETERS for v in value["parameter_keys"]) or not isinstance(value["required_check_statuses"], dict) or set(value["required_check_statuses"]) - set(REQUIRED_HARD_CHECK_TYPES) or any(v not in _CHECK_STATUSES for v in value["required_check_statuses"].values()) or not isinstance(value["duration_s"], (int, float)) or not math.isfinite(float(value["duration_s"])) or value["duration_s"] < 0: _fail("RESULT_TYPE_INVALID")
    if value["response_model"] is not None and value["response_model"] != "deepseek-v4-flash": _fail("RESULT_MODEL_INVALID")
    if value["projection_accepted"] is True and not value["response_valid"]: _fail("RESULT_STAGE_INVALID")
    if value["stage"] in {"generation_error", "generation_blocked"} and (value["pipeline_attempted"] is not False or any(value[key] is not None for key in ("pipeline_success", "hard_gate_passed", "quality_decision_passed")) or value["published"] is not False): _fail("RESULT_STAGE_INVALID")
    if value["pipeline_attempted"] is False and any(value[key] is not None for key in ("pipeline_success", "hard_gate_passed", "quality_decision_passed")): _fail("RESULT_STAGE_INVALID")
    if value["pipeline_attempted"] is True and value["projection_accepted"] is not True: _fail("RESULT_STAGE_INVALID")
    if value["published"] and not (value["pipeline_success"] is True and value["hard_gate_passed"] is True and value["quality_decision_passed"] is True): _fail("RESULT_STAGE_INVALID")
    if value["projection_accepted"] is True and not (value["reason_code"] == "accepted" and value["legacy_disposition"] == "accepted" and value["response_valid"] is True and value["api_call_count"] == 1 and value["response_model"] == "deepseek-v4-flash" and value["usage"] is not None): _fail("RESULT_ACCEPTANCE_INVALID")


def _validate_ledger(ledger: dict[str, Any], suite: dict[str, Any], protocol: dict[str, Any]) -> list[dict[str, Any]]:
    cases = _suite_cases(suite)
    if not isinstance(ledger, dict) or set(ledger) != _TOP or ledger["schema_version"] != LEDGER_SCHEMA or ledger["mode"] not in _MODES or ledger["batch_status"] not in _BATCH or not _sha(ledger["protocol_sha256"]) or not _sha(ledger["suite_sha256"]) or type(ledger["offline_acceptance_verified"]) is not bool: _fail("LEDGER_SCHEMA_INVALID")
    if ledger["protocol_sha256"] != _protocol_hash(protocol) or ledger["suite_sha256"] != _suite_hash(suite) or protocol.get("suite_sha256") != ledger["suite_sha256"]: _fail("LEDGER_HASH_MISMATCH")
    budget = ledger["budget"]
    if not isinstance(budget, dict) or set(budget) != _BUDGET or budget["budget_status"] not in _BUDGET_STATUS or type(budget["reserved_tokens"]) is not int or budget["reserved_tokens"] < 0 or any(type(budget[k]) not in {int, float} or not math.isfinite(float(budget[k])) or float(budget[k]) < 0 for k in ("reserved_cost_usd", "known_cost_usd")): _fail("BUDGET_SCHEMA_INVALID")
    schedule = build_schedule(suite, protocol.get("prompt_sha256", {}))
    expected = {(r["case_id"], r["arm"], r["repetition"]): r for r in schedule}
    if not isinstance(ledger["rows"], list) or len(ledger["rows"]) != 24: _fail("ROW_COUNT_INVALID")
    actual = set()
    for index, row in enumerate(ledger["rows"]):
        if not isinstance(row, dict) or set(row) != _ROW: _fail("ROW_SCHEMA_INVALID")
        key = (row["case_id"], row["arm"], row["repetition"])
        if key in actual or key not in expected or not _RUN.fullmatch(row["run_id"]) or type(row["positive"]) is not bool or row["status"] not in _STATUS or row["stop_reason"] not in _STOP or type(row["attempted"]) is not bool or any(row[name] != schedule[index][name] for name in ("run_id", "case_id", "arm", "positive", "repetition", "requirement_sha256", "prompt_sha256")): _fail("ROW_METADATA_INVALID")
        actual.add(key)
        if row["status"] == "COMPLETED":
            if not row["attempted"] or row["result"] is None or row["stop_reason"] != "COMPLETED": _fail("ROW_STATE_INVALID")
            _validate_result(row["result"], row)
        elif row["result"] is not None or (row["status"] == "PLANNED" and (row["attempted"] or row["stop_reason"] != "NOT_STARTED")): _fail("ROW_STATE_INVALID")
    if set(actual) != set(expected): _fail("ROW_SET_MISMATCH")
    if ledger["mode"] == "dry_run" and any(row["attempted"] or row["status"] == "COMPLETED" for row in ledger["rows"]): _fail("DRY_RUN_STATE_INVALID")
    return ledger["rows"]


def _suite_hash(suite: dict[str, Any]) -> str:
    import json
    return hashlib.sha256(json.dumps(suite, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _protocol_hash(protocol: dict[str, Any]) -> str:
    import json
    return hashlib.sha256(json.dumps(protocol, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def summarize(ledger: dict, suite: dict, protocol: dict) -> dict:
    """Return only fixed-label counters and safe per-case counters; never raw data."""
    if not isinstance(protocol, dict) or not isinstance(protocol.get("thresholds"), dict) or protocol["thresholds"] != _THRESHOLDS or protocol.get("max_calls") != 24 or not isinstance(protocol.get("offline_acceptance_sha256"), str): _fail("PROTOCOL_INVALID")
    rows = _validate_ledger(ledger, suite, protocol)
    positive = [row for row in rows if row["positive"]]
    negative = [row for row in rows if not row["positive"]]
    completed = [row for row in rows if row["status"] == "COMPLETED"]
    def result(row: dict[str, Any]) -> dict[str, Any] | None: return row["result"] if row["status"] == "COMPLETED" else None
    accepted = lambda row: bool(result(row) and result(row)["projection_accepted"])
    hard = lambda row: bool(result(row) and result(row)["stage"] == "pipeline_complete" and all(result(row)[key] is True for key in ("pipeline_success", "hard_gate_passed", "quality_decision_passed", "published")) and set(result(row)["required_check_statuses"]) == set(REQUIRED_HARD_CHECK_TYPES) and all(value == "PASS" for value in result(row)["required_check_statuses"].values()))
    published = lambda row: bool(result(row) and result(row)["published"])
    arm_accept = {arm: sum(accepted(row) for row in positive if row["arm"] == arm) for arm in ("A", "B")}
    valid_b = sum(bool(result(row) and result(row)["response_valid"]) for row in positive if row["arm"] == "B")
    usage_rows = [result(row) for row in completed if result(row) and result(row)["api_call_count"] > 0]
    known_usage = [item["usage"] for item in usage_rows if item["usage"] is not None]
    by_case = []
    for case in _suite_cases(suite):
        matching = [row for row in rows if row["case_id"] == case["case_id"]]
        by_case.append({"case_id": case["case_id"], "positive": case["positive"], "a_accepted": sum(accepted(r) for r in matching if r["arm"] == "A"), "b_accepted": sum(accepted(r) for r in matching if r["arm"] == "B"), "b_hard_success": sum(hard(r) for r in matching if r["arm"] == "B"), "negative_published": sum(published(r) for r in matching if not r["positive"])})
    online_complete = ledger["mode"] == "online" and ledger["batch_status"] == "COMPLETED" and len(completed) == 24 and ledger["budget"]["budget_status"] == "OK" and ledger["budget"]["reserved_tokens"] + sum(item["total_tokens"] for item in known_usage) <= protocol["max_total_tokens"] and ledger["budget"]["reserved_cost_usd"] + ledger["budget"]["known_cost_usd"] <= protocol["max_cost_usd"]
    offline_ok = bool(protocol["offline_acceptance_sha256"] and _sha(protocol["offline_acceptance_sha256"]) and ledger["offline_acceptance_verified"])
    b_each = all(item["b_accepted"] >= 2 for item in by_case if item["positive"])
    gate_ok = arm_accept["B"] >= 8 and b_each and arm_accept["B"] - arm_accept["A"] >= 3 and sum(hard(r) for r in positive if r["arm"] == "B") >= 8 and all(not accepted(r) or hard(r) for r in positive if r["arm"] == "B") and sum(published(r) for r in negative) == 0
    if ledger["mode"] == "dry_run": gate = "NOT_RUN"
    elif ledger["mode"] == "offline_mock": gate = "OFFLINE_MOCK_PASS" if ledger["batch_status"] == "COMPLETED" and len(completed) == 24 and gate_ok else ("INCOMPLETE" if len(completed) != 24 else "OFFLINE_MOCK_FAIL")
    elif not online_complete: gate = "INCOMPLETE"
    elif not offline_ok: gate = "DEV_GATE_FAIL"
    else: gate = "DEV_GATE_PASS" if gate_ok else "DEV_GATE_FAIL"
    reasons = Counter(result(row)["reason_code"] for row in completed)
    stages = Counter(result(row)["stage"] for row in completed)
    attempted_unfinished = [row for row in rows if row["attempted"] and row["result"] is None]
    return {"schema_version": REPORT_SCHEMA, "mode": ledger["mode"], "batch_status": ledger["batch_status"], "development_hypothesis_status": "NOT_RUN" if ledger["mode"] != "online" else ("PASS" if gate == "DEV_GATE_PASS" else "FAIL" if gate == "DEV_GATE_FAIL" else "NOT_RUN"), "development_gate": gate, "scheduled_runs": 24, "planned_runs": sum(row["status"] == "PLANNED" for row in rows), "attempted_runs": sum(row["attempted"] for row in rows), "completed_runs": len(completed), "not_run_runs": sum(row["status"] == "NOT_RUN" for row in rows), "api_call_count": sum(result(row)["api_call_count"] for row in completed) + len(attempted_unfinished), "response_valid_count": sum(bool(result(row)["response_valid"]) for row in completed), "positive_a_accepted": arm_accept["A"], "positive_b_accepted": arm_accept["B"], "positive_a_denominator": 9, "positive_b_denominator": 9, "positive_a_accepted_per_9": arm_accept["A"], "positive_b_accepted_per_9": arm_accept["B"], "positive_a_valid_response_denominator": sum(bool(result(row) and result(row)["response_valid"]) for row in positive if row["arm"] == "A"), "positive_b_valid_response_denominator": valid_b, "positive_b_accepted_per_valid_response": None if valid_b == 0 else arm_accept["B"] / valid_b, "positive_b_hard_success": sum(hard(r) for r in positive if r["arm"] == "B"), "negative_correct_rejected": sum(row["status"] == "COMPLETED" and result(row)["response_valid"] and result(row)["reason_code"] == "local_input_blocked" and not accepted(row) and not result(row)["pipeline_attempted"] and not published(row) for row in negative), "negative_published": sum(published(r) for r in negative), "usage_prompt_tokens": sum(item["prompt_tokens"] for item in known_usage), "usage_completion_tokens": sum(item["completion_tokens"] for item in known_usage), "usage_total_tokens": sum(item["total_tokens"] for item in known_usage), "usage_unknown_requests": sum(item["usage"] is None for item in usage_rows) + len(attempted_unfinished), "duration_s_total": sum(result(row)["duration_s"] for row in completed), "reserved_tokens": ledger["budget"]["reserved_tokens"], "reserved_cost_usd": ledger["budget"]["reserved_cost_usd"], "known_cost_usd": ledger["budget"]["known_cost_usd"], "reason_counts": dict(sorted(reasons.items())), "stage_counts": dict(sorted(stages.items())), "case_results": by_case}


__all__ = ["build_schedule", "summarize"]
