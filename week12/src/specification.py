"""V11 S1 specification and approval boundary.

This module is the only place where the mission requirements are projected
into a candidate input.  It deliberately stops before physical execution:
the generated S1 record is a pending design proposal until a human binds the
exact specification to a project and baseline.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any

from .physics import default_designs


class SpecificationError(ValueError):
    """Raised when an S1 input is incomplete, stale, or unsafe to generate."""


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def digest_json(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _digest_payload(spec: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(spec)
    payload.pop("confirmation", None)
    payload.pop("status", None)
    payload.pop("generated_at", None)
    payload.pop("spec_digest", None)
    return payload


def specification_digest(spec: dict[str, Any]) -> str:
    return digest_json(_digest_payload(spec))


def load_mission(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SpecificationError(f"cannot load mission input: {path}") from exc
    if not isinstance(raw, dict):
        raise SpecificationError("mission input must be a JSON object")
    return raw


def _require_number(value: Any, name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SpecificationError(f"{name} must be numeric")
    result = float(value)
    if minimum is not None and result < minimum:
        raise SpecificationError(f"{name} must be >= {minimum}")
    return result


def validate_mission(mission: dict[str, Any]) -> None:
    requirements = mission.get("user_requirements")
    assumptions = mission.get("assumptions")
    if not isinstance(requirements, dict) or not isinstance(assumptions, dict):
        raise SpecificationError("mission must contain user_requirements and assumptions")
    payload = requirements.get("equipment_payload")
    if not isinstance(payload, dict) or payload.get("unit") != "kg":
        raise SpecificationError("equipment payload must declare kg")
    if _require_number(payload.get("value"), "equipment_payload.value") != 20.0:
        raise SpecificationError("V11 design payload is fixed at 20 kg")
    if requirements.get("descent_mode", {}).get("value") != "forward_front_wheels_first":
        raise SpecificationError("descent mode must be forward_front_wheels_first")
    envelope = requirements.get("exploration_envelope")
    if not isinstance(envelope, dict):
        raise SpecificationError("exploration envelope is required")
    if _require_number(envelope.get("maximum_riser_m"), "maximum_riser_m") > 0.200:
        raise SpecificationError("riser exceeds the V11 upper boundary")
    if _require_number(envelope.get("minimum_tread_m"), "minimum_tread_m") < 0.220:
        raise SpecificationError("tread is below the V11 lower boundary")
    grid = assumptions.get("capability_grid")
    if not isinstance(grid, dict) or not isinstance(grid.get("riser_m"), list) or not isinstance(grid.get("tread_m"), list):
        raise SpecificationError("capability_grid must provide riser_m and tread_m lists")
    if not grid["riser_m"] or not grid["tread_m"]:
        raise SpecificationError("capability_grid cannot be empty")
    if len(grid["riser_m"]) * len(grid["tread_m"]) > 25:
        raise SpecificationError("S1 scan grid exceeds the 25-point finite budget")
    for item in grid["riser_m"]:
        if _require_number(item, "capability_grid.riser_m") > 0.200:
            raise SpecificationError("scan riser exceeds 200 mm")
    for item in grid["tread_m"]:
        if _require_number(item, "capability_grid.tread_m") < 0.220:
            raise SpecificationError("scan tread is below 220 mm")
    if requirements.get("wheel_restriction") != "中国大陆可采购的常见普通轮胎式车轮；禁止履带":
        raise SpecificationError("wheel restriction must keep ordinary tire wheels and reject tracks")


def _scenario_records(mission: dict[str, Any]) -> dict[str, Any]:
    assumptions = mission["assumptions"]
    steps = int(assumptions["steps_per_floor_transition"])
    common = {
        "platform_length_m": {"value": 1.0, "unit": "m", "source": "AI design parameter", "status": "must_be_confirmed"},
        "stop_time_s": {"value": 5.0, "unit": "s", "source": "AI design parameter", "status": "must_be_confirmed"},
        "initial_speed_m_s": {"value": 0.0, "unit": "m/s", "source": "AI assumption", "status": "must_be_confirmed"},
    }
    return {
        "ascent": {**deepcopy(common), "direction": "up", "entry": "flat_approach_to_first_riser", "exit": "last_wheel_leaves_steps_and_enters_top_platform", "steps": steps},
        "descent": {**deepcopy(common), "direction": "down", "mode": "forward_front_wheels_first", "entry": "top_platform_before_first_riser", "exit": "last_wheel_leaves_steps_and_enters_bottom_flat", "steps": steps, "braking_required": True},
        "flat": {"direction": "level", "entry": "level_ground", "exit": "level_ground_stop", "target_speed_m_s": assumptions["flat_target_speed_m_s"]},
    }


def build_s1_spec(mission: dict[str, Any], *, mission_path: Path, project_id: str, baseline_version_id: str) -> dict[str, Any]:
    """Build one pending S1 candidate; no simulation or approval is performed."""
    validate_mission(mission)
    if not project_id or not baseline_version_id:
        raise SpecificationError("project_id and baseline_version_id are required for confirmation binding")
    payload_kg = float(mission["user_requirements"]["equipment_payload"]["value"])
    stair = {
        "riser_m": float(mission["assumptions"]["starting_case"]["riser_m"]),
        "tread_m": float(mission["assumptions"]["starting_case"]["tread_m"]),
        "count": int(mission["assumptions"]["steps_per_floor_transition"]),
        "width_m": float(mission["assumptions"]["stair_clear_width_m"]),
    }
    candidates = default_designs(stair, [4, 6], payload_kg=payload_kg)
    design = next(item for item in candidates if item["candidate_id"] == "clearance")
    assumptions = mission["assumptions"]
    parameter_register = [
        {"id": "equipment_payload", "value": payload_kg, "unit": "kg", "source": "user_requirement", "status": "user_fact"},
        {"id": "total_loaded_mass", "value": design["mass_kg"], "unit": "kg", "source": "derived_from_design_components", "status": "AI_design_pending_confirmation"},
        {"id": "stair_riser", "value": stair["riser_m"], "unit": "m", "source": "AI_starting_case", "status": "AI_assumption_pending_confirmation"},
        {"id": "stair_tread", "value": stair["tread_m"], "unit": "m", "source": "AI_starting_case", "status": "AI_assumption_pending_confirmation"},
        {"id": "stair_steps_per_transition", "value": stair["count"], "unit": "count", "source": "user_assumed_geometry_interpreted_by_AI", "status": "AI_assumption_pending_confirmation"},
        {"id": "descent_mode", "value": "forward_front_wheels_first", "unit": "enum", "source": "user_requirement", "status": "user_fact"},
        {"id": "platform_length", "value": 1.0, "unit": "m", "source": "AI_design_parameter", "status": "AI_assumption_pending_confirmation"},
        {"id": "stop_time", "value": 5.0, "unit": "s", "source": "AI_design_parameter", "status": "AI_assumption_pending_confirmation"},
        {"id": "friction_nominal", "value": assumptions["tire_friction"]["nominal"], "unit": "coefficient", "source": "AI_assumption", "status": "AI_assumption_pending_confirmation"},
        {"id": "battery_usable_fraction", "value": assumptions["usable_battery_fraction"], "unit": "fraction", "source": "AI_assumption", "status": "AI_assumption_pending_confirmation"},
    ]
    spec: dict[str, Any] = {
        "schema_version": "v11-s1-spec-1",
        "status": "pending_user_confirmation",
        "mission_id": mission["mission_id"],
        "source": {"path": str(mission_path), "sha256": hashlib.sha256(mission_path.read_bytes()).hexdigest(), "origin": mission["source"]},
        "project_binding": {"project_id": project_id, "baseline_version_id": baseline_version_id, "binding_status": "development_pending"},
        "candidate": {"candidate_id": "s1-clearance-pending", "design": design, "selection_reason": "能力优先的首个待确认候选；不是产品批准"},
        "parameter_register": parameter_register,
        "mass": {"payload_kg": payload_kg, "vehicle_without_payload_kg": design["mass_kg"] - payload_kg, "total_loaded_mass_kg": design["mass_kg"], "breakdown_kg": design["mass_breakdown_kg"], "source": "AI design estimate plus user 20 kg design load", "status": "pending_user_confirmation"},
        "stair": stair,
        "scenarios": _scenario_records(mission),
        "assumptions": {"friction": assumptions["tire_friction"]["nominal"], "rolling_coefficient": assumptions["rolling_resistance_coefficient"]["nominal"], "cruise_speed_m_s": assumptions["flat_target_speed_m_s"], "stair_speed_m_s": assumptions["stair_initial_target_speed_m_s"], "stair_stop_s": 5.0, "flat_stop_s": 5.0, "payload_cg": assumptions["payload_cg"], "surface": assumptions["stair_surface"], "nosing": assumptions["stair_nosing"]},
        "component_requirements": {"drive": {"continuous_torque_nm_min": design["wheel_continuous_torque_nm"], "peak_torque_nm_min": design["wheel_peak_torque_nm"], "peak_duration_s": 10.0, "allowed_peak_duration_s": [5.0, 15.0], "allowed_efficiency_range": [0.75, 0.92], "compatibility": "motor, gearbox and controller must share voltage and torque envelope", "status": "must_be_confirmed_and_verified"}, "battery": {"nominal_voltage_v": design["battery_voltage_v"], "allowed_voltage_range_v": [22.0, 26.0], "max_current_a_min": design["battery_max_current_a"], "allowed_current_range_a": [0.0, design["battery_max_current_a"]], "usable_fraction": assumptions["usable_battery_fraction"], "bms_and_controller_compatibility": "must be verified against peak current, continuous current and voltage range"}},
        "portability": {"mass_without_payload_kg": design["mass_kg"] - payload_kg, "total_loaded_mass_kg": design["mass_kg"], "envelope_m": {"length": design["body_length_m"] + 2 * design["wheel_radius_m"], "width": design["track_m"] + design["tire_width_m"], "height": 2 * design["wheel_radius_m"] + design["body_half_height_m"]}, "includes_battery": True, "handling": "carry as one unit; folding/disassembly not designed", "status": "comparison_only_pending_verification"},
        "scan_protocol": {"riser_points_m": assumptions["capability_grid"]["riser_m"], "tread_points_m": assumptions["capability_grid"]["tread_m"], "max_points": 25, "max_candidates": 1, "refinement_passes": 1, "timeout_s": 180, "stop_rules": ["preserve failed or timed-out run", "no automatic retry", "stop at budget", "unmeasured combinations remain unverified"], "status": "frozen_for_confirmation_pending"},
        "s1_budget": {"local_validation_cases_max": 6, "online_requests": 0, "openmodelica_simulations": 0, "stop_on_missing_mass_or_binding": True},
        "confirmation": {"human_confirmed": False, "spec_digest": None, "confirmed_at": None, "origin": "pending_user_confirmation"},
    }
    spec["spec_digest"] = specification_digest(spec)
    return spec


def validate_spec(spec: dict[str, Any]) -> None:
    if spec.get("schema_version") != "v11-s1-spec-1":
        raise SpecificationError("unsupported V11 S1 specification schema")
    mass = spec.get("mass", {})
    if mass.get("payload_kg") != 20.0 or mass.get("total_loaded_mass_kg", 0) < 20.0:
        raise SpecificationError("20 kg payload is missing from total mass")
    binding = spec.get("project_binding", {})
    if not binding.get("project_id") or not binding.get("baseline_version_id"):
        raise SpecificationError("specification is not bound to a project and baseline")
    if spec.get("spec_digest") != specification_digest(spec):
        raise SpecificationError("specification digest is stale")
    validate_mission({"user_requirements": {"equipment_payload": {"value": 20.0, "unit": "kg"}, "descent_mode": {"value": "forward_front_wheels_first"}, "exploration_envelope": {"maximum_riser_m": 0.2, "minimum_tread_m": 0.22}, "wheel_restriction": "中国大陆可采购的常见普通轮胎式车轮；禁止履带"}, "assumptions": {"capability_grid": {"riser_m": spec["scan_protocol"]["riser_points_m"], "tread_m": spec["scan_protocol"]["tread_points_m"]}}})


def confirm_spec(spec: dict[str, Any], confirmation: dict[str, Any]) -> dict[str, Any]:
    """Apply an explicit human confirmation record to a matching proposal."""
    validate_spec(spec)
    if confirmation.get("human_confirmed") is not True or confirmation.get("origin") != "user":
        raise SpecificationError("AI or scripted confirmation cannot approve a product specification")
    binding = spec["project_binding"]
    for key in ("project_id", "baseline_version_id"):
        if confirmation.get(key) != binding.get(key):
            raise SpecificationError(f"confirmation does not bind {key}")
    if confirmation.get("spec_digest") != spec["spec_digest"]:
        raise SpecificationError("confirmation digest does not match displayed specification")
    result = deepcopy(spec)
    result["status"] = "confirmed_for_generation"
    result["confirmation"] = deepcopy(confirmation)
    result["confirmation"]["spec_digest"] = spec["spec_digest"]
    return result


def assert_generation_ready(spec: dict[str, Any]) -> None:
    validate_spec(spec)
    if spec.get("status") != "confirmed_for_generation":
        raise SpecificationError("concrete design is awaiting human confirmation")
    confirmation = spec.get("confirmation", {})
    if confirmation.get("human_confirmed") is not True or confirmation.get("origin") != "user":
        raise SpecificationError("generation requires an explicit user confirmation")
    for key in ("project_id", "baseline_version_id", "spec_digest"):
        if confirmation.get(key) != (spec["project_binding"].get(key) if key != "spec_digest" else spec["spec_digest"]):
            raise SpecificationError(f"confirmation binding mismatch: {key}")


def load_spec(path: Path, *, require_generation_ready: bool = False) -> dict[str, Any]:
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SpecificationError(f"cannot load specification: {path}") from exc
    if not isinstance(spec, dict):
        raise SpecificationError("specification must be a JSON object")
    validate_spec(spec)
    if require_generation_ready:
        assert_generation_ready(spec)
    return spec


def write_s1_artifact(mission_path: Path, output_dir: Path, *, project_id: str, baseline_version_id: str) -> dict[str, Any]:
    """Persist the pending S1 proposal and its validation evidence."""
    mission = load_mission(mission_path)
    spec = build_s1_spec(mission, mission_path=mission_path, project_id=project_id, baseline_version_id=baseline_version_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "spec.json").write_bytes(_canonical(spec))
    candidate = {"schema_version": "v11-s1-candidate-1", "status": spec["status"], "candidate": spec["candidate"], "mass": spec["mass"], "stair": spec["stair"], "scenarios": spec["scenarios"], "scan_protocol": spec["scan_protocol"], "s1_budget": spec["s1_budget"]}
    (output_dir / "candidate.json").write_bytes(_canonical(candidate))
    validation = {"schema_version": "v11-s1-validation-1", "status": "pending_user_confirmation", "checks": [{"id": "payload_in_total_mass", "passed": spec["mass"]["payload_kg"] == 20.0 and spec["mass"]["total_loaded_mass_kg"] >= 20.0}, {"id": "forward_descent_and_transitions_declared", "passed": bool(spec["scenarios"]["descent"]["mode"] == "forward_front_wheels_first" and spec["scenarios"]["descent"]["entry"] and spec["scenarios"]["descent"]["exit"])}, {"id": "finite_scan_budget", "passed": spec["scan_protocol"]["max_points"] == 25 and spec["s1_budget"]["openmodelica_simulations"] == 0}, {"id": "project_baseline_binding", "passed": bool(project_id and baseline_version_id)}, {"id": "human_confirmation", "passed": False}], "simulation_count": 0, "online_request_count": 0}
    (output_dir / "validation.json").write_bytes(_canonical(validation))
    files = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(output_dir.iterdir()) if path.is_file()}
    (output_dir / "manifest.json").write_bytes(_canonical({"schema_version": "v11-s1-manifest-1", "files": files}))
    return spec


__all__ = ["SpecificationError", "assert_generation_ready", "build_s1_spec", "confirm_spec", "digest_json", "load_mission", "load_spec", "specification_digest", "validate_mission", "validate_spec", "write_s1_artifact"]
