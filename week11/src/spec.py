"""Structured, locally-verifiable V10 model specifications.

This module deliberately consumes a proposal that has already been projected
into fields.  It never reads natural language to infer a field or a topology.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
from typing import Any, Mapping

from week9.src.modelica_templates import convert_unit, get_template_spec, relation_issues
from week9.src.parameter_normalization import ParameterObservation


SCHEMA_VERSION = "v10-spec-1"
_OPEN_TEMPLATES = frozenset(("rc_filter", "rlc_filter", "thermal_dual_room"))
_KNOWN_CLOSED_TEMPLATES = frozenset(("opamp_inverting", "thermal_single_room"))
_DERIVATIONS = {
    "rc_filter": (
        "C = 1 / (2 * pi * R * fc)",
        ("resistance", "cutoff_frequency"),
    ),
    "rlc_filter": (
        "C = 1 / ((2 * pi * fc)^2 * L)",
        ("inductance", "cutoff_frequency"),
    ),
}
_SUPPORTED_CONSTRAINTS = {
    "rc_filter": frozenset(("fixed_certified_template", "fixed_stimulus", "rc_time_constant_check")),
    "rlc_filter": frozenset(("fixed_certified_template", "fixed_stimulus", "rlc_resonance_gain_check")),
    "thermal_dual_room": frozenset(("fixed_certified_template", "fixed_stimulus", "shared_steady_state_check")),
}
_FIXED_STIMULI = {
    "rc_filter": {"kind": "step_voltage", "time_origin_seconds": 0.0, "initial_capacitor_voltage": 0.0},
    "rlc_filter": {"kind": "sine_voltage", "time_origin_seconds": 0.0, "phase_radians": 0.0, "bias_voltage": 0.0, "initial_current": 0.0, "initial_capacitor_voltage": 0.0},
    "thermal_dual_room": {"kind": "fixed_temperature_boundary", "passive_heat_exchange": True, "boundary": "outdoor_temperature"},
}
_EXPLICIT_CAPACITANCE = re.compile(
    r"(?:capacitance|电容|(?<![A-Za-z])C(?![A-Za-z]))\s*(?:=|为|是)?\s*"
    r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)\s*(?:pF|nF|uF|µF|μF|mF|F)\b",
    re.IGNORECASE,
)
_NUMBER_WITH_UNIT = re.compile(
    r"(?P<value>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*"
    r"(?P<unit>[A-Za-zµμΩ/°℃]+)"
)


def validate_spec(draft: Any) -> dict[str, Any]:
    """Return a canonical spec only when every local, objective check passes."""

    issues: list[dict[str, str]] = []
    if not isinstance(draft, Mapping):
        return _blocked(("invalid_draft", "spec draft must be an object"))
    data = dict(draft)
    _unknown_root_fields(data, issues)
    if data.get("schema_version") != SCHEMA_VERSION:
        _issue(issues, "schema_version", f"schema_version must be {SCHEMA_VERSION!r}")
    template_id = data.get("template_id")
    if not isinstance(template_id, str):
        _issue(issues, "template_id", "template_id must be a string")
        template_id = ""
    elif template_id in _KNOWN_CLOSED_TEMPLATES:
        _issue(issues, "template_not_open", f"template {template_id!r} is not open in V10 first release")
    elif template_id not in _OPEN_TEMPLATES:
        _issue(issues, "unknown_template", f"template {template_id!r} is not supported")

    sources = _sources(data.get("sources"), issues)
    source_map = {item["id"]: item["text"] for item in sources}
    expected = _parameter_specs(template_id, issues)
    parameters, normalized = _parameters(data.get("parameters"), expected, source_map, issues)
    values = {item["name"]: item["value"] for item in normalized}
    derivations, derived_values = _derivations(data.get("derivations"), template_id, values, source_map, issues)

    expected_names = tuple(item.name for item in expected)
    supplied_names = {item["name"] for item in parameters}
    derived_names = {item["target"] for item in derivations}
    for name in expected_names:
        if name not in supplied_names and name not in derived_names:
            _issue(issues, "missing_parameter", f"missing parameter: {name}")
    for item in derived_values:
        values[item["target"]] = item["value"]
        normalized.append({"name": item["target"], "value": item["value"], "unit": "F", "source_text": item["source_text"]})

    if not issues and set(values) == set(expected_names):
        for message in relation_issues(template_id, values):
            _issue(issues, "parameter_relation", message)
    stimulus = _stimulus(data.get("stimulus"), template_id, source_map, issues)
    constraints = _strings(data.get("constraints"), "constraints", issues)
    if template_id in _SUPPORTED_CONSTRAINTS:
        for constraint in constraints:
            if constraint not in _SUPPORTED_CONSTRAINTS[template_id]:
                _issue(issues, "unsupported_constraint", f"constraint is not supported by the fixed template: {constraint!r}")
    assumptions = _strings(data.get("assumptions"), "assumptions", issues)

    if issues:
        return {"ready": False, "issues": issues, "spec": None}
    normalized.sort(key=lambda item: expected_names.index(item["name"]))
    spec = {
        "schema_version": SCHEMA_VERSION,
        "template_id": template_id,
        "sources": sources,
        "parameters": parameters,
        "normalized_parameters": normalized,
        "stimulus": stimulus,
        "fixed_stimulus": deepcopy(_FIXED_STIMULI[template_id]),
        "derivations": derivations,
        "derived_values": derived_values,
        "constraints": constraints,
        "assumptions": assumptions,
    }
    return {"ready": True, "issues": [], "spec": spec}


def spec_digest(spec: Any) -> str:
    """Hash a revalidated canonical spec; caller-provided derived fields are ignored."""

    result = validate_spec(spec)
    if not result["ready"]:
        raise ValueError("cannot digest an invalid spec")
    encoded = json.dumps(result["spec"], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def observations_for_spec(validated_spec: Any) -> tuple[ParameterObservation, ...]:
    """Return fresh old-pipeline observations after revalidation.

    This adapter preserves direct source units/text.  A locally calculated
    capacitance is represented with the formula-request source, never an
    invented numeric source quote.
    """

    result = validate_spec(validated_spec)
    if not result["ready"]:
        raise ValueError("cannot create observations from an invalid spec")
    spec = result["spec"]
    direct = {item["name"]: item for item in spec["parameters"]}
    derived = {item["target"]: item for item in spec["derived_values"]}
    observations: list[ParameterObservation] = []
    for item in spec["normalized_parameters"]:
        original = direct.get(item["name"])
        if original is not None:
            observations.append(ParameterObservation(item["name"], original["value"], original["unit"], original["source_text"]))
        else:
            value = derived[item["name"]]
            observations.append(ParameterObservation(item["name"], value["value"], "F", value["source_text"]))
    return tuple(observations)


def _unknown_root_fields(data: Mapping[str, Any], issues: list[dict[str, str]]) -> None:
    allowed = {"schema_version", "template_id", "sources", "parameters", "stimulus", "derivations", "constraints", "assumptions", "normalized_parameters", "derived_values", "fixed_stimulus"}
    for name in sorted(set(data) - allowed):
        _issue(issues, "unknown_field", f"unknown spec field: {name}")


def _sources(value: Any, issues: list[dict[str, str]]) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        _issue(issues, "sources", "sources must be a non-empty list")
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping) or set(item) != {"id", "text"}:
            _issue(issues, "source", f"source {index} must contain exactly id and text")
            continue
        source_id, text = item.get("id"), item.get("text")
        if not isinstance(source_id, str) or not source_id.strip() or not isinstance(text, str) or not text:
            _issue(issues, "source", f"source {index} has invalid id or text")
        elif source_id in seen:
            _issue(issues, "duplicate_source", f"duplicate source id: {source_id}")
        else:
            seen.add(source_id)
            result.append({"id": source_id, "text": text})
    return result


def _parameter_specs(template_id: str, issues: list[dict[str, str]]) -> tuple[Any, ...]:
    if template_id not in _OPEN_TEMPLATES:
        return ()
    return get_template_spec(template_id).parameters


def _parameters(value: Any, expected: tuple[Any, ...], source_map: Mapping[str, str], issues: list[dict[str, str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(value, list):
        _issue(issues, "parameters", "parameters must be a list")
        return [], []
    expected_by_name = {item.name: item for item in expected}
    seen: set[str] = set()
    raw: list[dict[str, Any]] = []
    normalized: list[dict[str, Any]] = []
    fields = {"name", "value", "unit", "source_id", "source_text", "origin"}
    for index, item in enumerate(value):
        if not isinstance(item, Mapping) or set(item) != fields:
            _issue(issues, "parameter", f"parameter {index} has invalid fields")
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            _issue(issues, "parameter_name", f"parameter {index} has invalid name")
            continue
        if name in seen:
            _issue(issues, "duplicate_parameter", f"duplicate parameter: {name}")
            continue
        seen.add(name)
        if name not in expected_by_name:
            _issue(issues, "unknown_parameter", f"unknown template parameter: {name}")
            continue
        source_id, source_text, origin = item.get("source_id"), item.get("source_text"), item.get("origin")
        _source_reference(source_id, source_text, source_map, f"parameter {name}", issues)
        if origin not in ("direct", "user"):
            _issue(issues, "origin", f"parameter {name} origin must be direct or user")
        number = item.get("value")
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            _issue(issues, "numeric_value", f"parameter {name} must be a real number")
            continue
        try:
            numeric_value = float(number)
        except (OverflowError, ValueError):
            _issue(issues, "numeric_value", f"parameter {name} must be finite")
            continue
        if not math.isfinite(numeric_value):
            _issue(issues, "numeric_value", f"parameter {name} must be finite")
            continue
        unit = item.get("unit")
        if not isinstance(unit, str) or not unit.strip():
            _issue(issues, "unit", f"parameter {name} must have a unit")
            continue
        try:
            canonical = convert_unit(numeric_value, unit, expected_by_name[name].unit)
        except (ValueError, OverflowError) as exc:
            _issue(issues, "unit", f"parameter {name}: {exc}")
            continue
        if not math.isfinite(canonical):
            _issue(issues, "numeric_value", f"parameter {name} conversion must be finite")
            continue
        if not _source_proves_value(number, unit, source_text, expected_by_name[name].unit, canonical):
            _issue(issues, "source_value_unit", f"parameter {name} source_text does not prove its value and unit")
            continue
        if not _in_bounds(canonical, expected_by_name[name]):
            _issue(issues, "parameter_range", f"parameter {name} is outside its allowed range")
            continue
        entry = {key: item[key] for key in fields}
        raw.append(entry)
        normalized.append({"name": name, "value": canonical, "unit": expected_by_name[name].unit, "source_text": source_text})
    return raw, normalized


def _derivations(value: Any, template_id: str, values: Mapping[str, float], source_map: Mapping[str, str], issues: list[dict[str, str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(value, list):
        _issue(issues, "derivations", "derivations must be a list")
        return [], []
    result: list[dict[str, Any]] = []
    derived: list[dict[str, Any]] = []
    targets: set[str] = set()
    fields = {"target", "formula", "input_names", "source_id", "source_text"}
    for index, item in enumerate(value):
        if not isinstance(item, Mapping) or set(item) != fields:
            _issue(issues, "derivation", f"derivation {index} has invalid fields")
            continue
        target, formula, inputs = item.get("target"), item.get("formula"), item.get("input_names")
        if not isinstance(target, str) or not target:
            _issue(issues, "derivation", f"derivation {index} target must be a non-empty string")
            continue
        if target in targets:
            _issue(issues, "duplicate_derivation", f"duplicate derivation target: {target}")
            continue
        targets.add(target)
        expected = _DERIVATIONS.get(template_id)
        if expected is None or target != "capacitance" or formula != expected[0] or not isinstance(inputs, list) or tuple(inputs) != expected[1]:
            _issue(issues, "unsupported_formula", f"derivation {index} is not an allowed formula for {template_id}")
            continue
        _source_reference(item.get("source_id"), item.get("source_text"), source_map, "derivation", issues)
        if isinstance(item.get("source_text"), str) and _EXPLICIT_CAPACITANCE.search(item["source_text"]):
            _issue(issues, "derivation_conflict", "a derived capacitance source cannot assert an explicit capacitance value")
        if target in values:
            _issue(issues, "derivation_conflict", "capacitance cannot be both direct and derived")
            continue
        if any(name not in values for name in expected[1]):
            _issue(issues, "derivation_inputs", "derivation inputs must be supplied direct parameters")
            continue
        try:
            capacitance = (1.0 / (2.0 * math.pi * values["resistance"] * values["cutoff_frequency"]) if template_id == "rc_filter" else 1.0 / (((2.0 * math.pi * values["cutoff_frequency"]) ** 2) * values["inductance"]))
        except (ArithmeticError, OverflowError, ZeroDivisionError):
            _issue(issues, "derivation_value", "derived capacitance overflowed or divided by zero")
            continue
        if not math.isfinite(capacitance) or capacitance <= 0:
            _issue(issues, "derivation_value", "derived capacitance is invalid")
            continue
        clean = {key: item[key] for key in fields}
        clean["input_names"] = list(inputs)
        result.append(clean)
        derived.append({"target": target, "value": capacitance, "unit": "F", "source_text": item["source_text"]})
    return result, derived


def _stimulus(value: Any, template_id: str, source_map: Mapping[str, str], issues: list[dict[str, str]]) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"kind", "source_id", "source_text"}:
        _issue(issues, "stimulus", "stimulus must contain exactly kind, source_id, and source_text")
        return {}
    kind = value.get("kind")
    if template_id in _OPEN_TEMPLATES and kind != _FIXED_STIMULI[template_id]["kind"]:
        _issue(issues, "stimulus_mismatch", f"template {template_id} requires {_FIXED_STIMULI[template_id]['kind']}")
    _source_reference(value.get("source_id"), value.get("source_text"), source_map, "stimulus", issues)
    return {"kind": kind, "source_id": value.get("source_id"), "source_text": value.get("source_text")}


def _strings(value: Any, name: str, issues: list[dict[str, str]]) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        _issue(issues, name, f"{name} must be a list of non-empty strings")
        return []
    return list(value)


def _source_reference(source_id: Any, source_text: Any, source_map: Mapping[str, str], owner: str, issues: list[dict[str, str]]) -> None:
    if not isinstance(source_id, str) or source_id not in source_map:
        _issue(issues, "source_reference", f"{owner} references an unknown source_id")
    if not isinstance(source_text, str) or not source_text:
        _issue(issues, "source_text", f"{owner} source_text must be non-empty")
    elif isinstance(source_id, str) and source_id in source_map and source_text not in source_map[source_id]:
        _issue(issues, "source_text", f"{owner} source_text is not an exact substring of its source")


def _in_bounds(value: float, parameter: Any) -> bool:
    if not math.isfinite(value):
        return False
    if parameter.minimum is not None and (value < parameter.minimum if parameter.minimum_inclusive else value <= parameter.minimum):
        return False
    if parameter.maximum is not None and (value > parameter.maximum if parameter.maximum_inclusive else value >= parameter.maximum):
        return False
    return True


def _issue(issues: list[dict[str, str]], code: str, message: str) -> None:
    issues.append({"code": code, "message": message})


def _blocked(issue: tuple[str, str]) -> dict[str, Any]:
    return {"ready": False, "issues": [{"code": issue[0], "message": issue[1]}], "spec": None}


def _source_proves_value(raw_value: float | int, raw_unit: str, source_text: Any, target_unit: str, canonical: float) -> bool:
    """Check numeric provenance inside the already-cited substring only.

    This intentionally has no parameter-label grammar: natural labels are a
    presentation concern.  Every compatible number/unit pair in the cited
    fragment must agree, so a fragment such as ``R=1 kOhm; R=2 kOhm`` cannot
    silently support either value.
    """

    if not isinstance(source_text, str):
        return False
    compatible_values: list[float] = []
    for match in _NUMBER_WITH_UNIT.finditer(source_text):
        try:
            observed = float(match.group("value"))
            source_unit = {"°C": "degC", "℃": "degC"}.get(match.group("unit"), match.group("unit"))
            # A numerically invalid compatible pair is an explicit local
            # conflict, never harmless text that can be skipped.
            if not math.isfinite(observed):
                convert_unit(1.0, source_unit, target_unit)
                return False
            converted = convert_unit(observed, source_unit, target_unit)
        except (ValueError, OverflowError):
            continue
        if math.isfinite(converted):
            compatible_values.append(converted)
    if not compatible_values:
        return False
    # Explicit local alternatives for the same physical dimension are a
    # conflict, even if one happens to equal the structured value.
    return all(math.isclose(candidate, canonical, rel_tol=1e-12, abs_tol=0.0) for candidate in compatible_values)
