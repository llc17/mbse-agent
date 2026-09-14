"""Privacy-safe inspection of one raw projection response.

The inspector is deliberately separate from generation and the pipeline. It
uses the frozen V8 parser/verifier and the V9 diagnostic classifier, but emits
only fixed metadata, counts, booleans, and allow-listed canonical names.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
import json
import math
from typing import Any, Mapping

from week9.src.generation import DeterministicNaturalLanguageProvider
from week9.src.online_generation import _parse_projection, _response_fields
from week10.src.projection_diagnostics import (
    _SAFE_PARAMETER_NAMES,
    diagnose_parsed_projection,
)


_ISSUE_CODES = (
    "ambiguous_parameter",
    "ambiguous_template",
    "generation_contract_incomplete",
    "invalid_parameter_unit",
    "missing_parameter",
    "missing_stimulus",
    "no_certified_template",
    "stimulus_conflict",
)
_ISSUE_MENTIONS = {
    "resonance": ("resonance", "resonant", "谐振"),
    "cutoff": ("cutoff", "截止"),
    "capacitance": ("capacitance", "capacitor", "电容"),
    "missing": ("missing", "缺少", "缺失"),
    "conflict": ("conflict", "conflicting", "冲突", "矛盾"),
    "derivation": (
        "derive",
        "derived",
        "derivation",
        "calculate",
        "calculation",
        "计算",
        "推导",
        "派生",
    ),
}
_FAILURE_CODES = frozenset(
    {
        "none",
        "response_schema_invalid",
        "response_json_invalid",
        "projection_schema_invalid",
        "local_generation_error",
        "diagnostic_error",
    }
)


def _empty_result() -> dict[str, Any]:
    return {
        "schema_version": "v9-projection-probe-inspection-1",
        "response_valid": False,
        "failure_code": "diagnostic_error",
        "local_ready": False,
        "remote_ready": False,
        "template_match": False,
        "expected_parameter_count": 0,
        "observed_parameter_count": 0,
        "missing_parameter_names": [],
        "unexpected_known_parameter_names": [],
        "duplicate_parameter_names": [],
        "unknown_parameter_count": 0,
        "model_reported_issue_code_counts": {code: 0 for code in _ISSUE_CODES},
        "model_reported_unknown_issue_count": 0,
        "model_reported_issue_mentions": {
            name: False for name in _ISSUE_MENTIONS
        },
        "numeric_mismatch_keys": [],
        "unit_mismatch_keys": [],
        "source_span_mismatch_keys": [],
        "reason_code": "unknown_diagnostic",
        "projection_accepted": False,
    }


def _failed(result: dict[str, Any], code: str) -> dict[str, Any]:
    if code not in _FAILURE_CODES or code == "none":
        code = "diagnostic_error"
    result["failure_code"] = code
    if code == "response_json_invalid":
        result["reason_code"] = "response_json_invalid"
    elif code == "projection_schema_invalid":
        result["reason_code"] = "projection_schema_invalid"
    return result


def inspect_response(response: Mapping[str, Any], requirement_text: str) -> dict[str, Any]:
    """Return a JSON-safe inspection without reflecting provider-controlled text."""

    result = _empty_result()
    try:
        local = DeterministicNaturalLanguageProvider().generate(requirement_text)
    except Exception:
        return _failed(result, "local_generation_error")
    result["local_ready"] = bool(local.ready)
    expected = [asdict(item) for item in local.observations]
    result["expected_parameter_count"] = len(expected)
    expected_by_name = {item["name"]: item for item in expected}

    try:
        if not isinstance(response, Mapping):
            raise TypeError
        content, _model, _usage = _response_fields(dict(response))
    except Exception:
        return _failed(result, "response_schema_invalid")
    try:
        projection = _parse_projection(content)
    except json.JSONDecodeError:
        return _failed(result, "response_json_invalid")
    except (TypeError, ValueError, KeyError, AttributeError):
        return _failed(result, "projection_schema_invalid")
    except Exception:
        return _failed(result, "diagnostic_error")

    result["response_valid"] = True
    result["failure_code"] = "none"
    result["remote_ready"] = projection["status"] == "ready"
    result["template_match"] = projection["template_id"] == local.template_id

    observations = projection["observations"]
    result["observed_parameter_count"] = len(observations)
    known_names = [
        item["name"] for item in observations if item["name"] in _SAFE_PARAMETER_NAMES
    ]
    known_counts = Counter(known_names)
    result["missing_parameter_names"] = sorted(
        set(expected_by_name) - set(known_counts)
    )
    result["unexpected_known_parameter_names"] = sorted(
        set(known_counts) - set(expected_by_name)
    )
    result["duplicate_parameter_names"] = sorted(
        name for name, count in known_counts.items() if count > 1
    )
    result["unknown_parameter_count"] = sum(
        item["name"] not in _SAFE_PARAMETER_NAMES for item in observations
    )

    first_known: dict[str, Mapping[str, Any]] = {}
    for item in observations:
        if item["name"] in _SAFE_PARAMETER_NAMES and item["name"] not in first_known:
            first_known[item["name"]] = item
    matching_names = sorted(set(expected_by_name) & set(first_known))
    result["numeric_mismatch_keys"] = [
        name
        for name in matching_names
        if not math.isclose(
            float(first_known[name]["value"]),
            float(expected_by_name[name]["value"]),
            rel_tol=1e-12,
            abs_tol=1e-15,
        )
    ]
    result["unit_mismatch_keys"] = [
        name
        for name in matching_names
        if first_known[name]["unit"] != expected_by_name[name]["unit"]
    ]
    result["source_span_mismatch_keys"] = [
        name
        for name in matching_names
        if first_known[name]["source_text"] != expected_by_name[name]["source_text"]
    ]

    issue_counts = Counter(issue["code"] for issue in projection["issues"])
    result["model_reported_issue_code_counts"] = {
        code: issue_counts.get(code, 0) for code in _ISSUE_CODES
    }
    result["model_reported_unknown_issue_count"] = sum(
        count for code, count in issue_counts.items() if code not in _ISSUE_CODES
    )
    messages = tuple(issue["message"].casefold() for issue in projection["issues"])
    result["model_reported_issue_mentions"] = {
        name: any(term.casefold() in message for message in messages for term in terms)
        for name, terms in _ISSUE_MENTIONS.items()
    }

    try:
        diagnostic = diagnose_parsed_projection(projection, local)
    except Exception:
        result["response_valid"] = False
        result["failure_code"] = "diagnostic_error"
        return result
    result["reason_code"] = diagnostic.reason_code
    result["projection_accepted"] = diagnostic.projection_accepted
    return result


__all__ = ["inspect_response"]
