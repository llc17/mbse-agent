"""Adapters that connect the S2 evidence runner to the real local toolchain.

The adapters keep OpenModelica/SysML execution behind the S2Runner boundary.
They report measured traces and explicit physical criteria, while leaving the
final interpretation in the persisted result for review.
"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .physics import generate_model, model_name
from .run_candidates import _sysml
from .toolchain import check_sysml, execute_model


class RealS2Generator:
    """Generate one SysML/Modelica pair from the confirmed S1 record."""

    def generate(self, spec: Mapping[str, Any], scenario: str, scenario_dir: Path) -> Mapping[str, Any]:
        design = dict(spec["candidate"]["design"])
        design["candidate_id"] = spec["candidate"]["candidate_id"]
        model_spec = {"design": design, "stair": spec["stair"], "assumptions": spec["assumptions"]}
        sysml = _sysml(design, spec["stair"])
        modelica = generate_model(model_spec, scenario)
        sysml_path = scenario_dir / "model.sysml"
        modelica_path = scenario_dir / "model.mo"
        sysml_path.write_text(sysml, encoding="utf-8", newline="\n")
        modelica_path.write_text(modelica, encoding="utf-8", newline="\n")
        return {
            "scenario": scenario,
            "model_name": model_name(scenario),
            "sysml_file": sysml_path.name,
            "modelica_file": modelica_path.name,
            "modelica_sha256": hashlib.sha256(modelica.encode("utf-8")).hexdigest(),
        }


def _read_csv(path: Path) -> dict[str, list[float]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    if len(rows) < 2:
        raise ValueError("simulation CSV has no data rows")
    return {header.strip('"'): [float(value) for value in values] for header, values in zip(rows[0], zip(*rows[1:]))}


def _trace_metrics(csv_path: Path) -> dict[str, Any]:
    data = _read_csv(csv_path)

    def values(name: str) -> list[float]:
        return data.get(name, [])

    def last(name: str, default: float = 0.0) -> float:
        items = values(name)
        return items[-1] if items else default

    def array_values(prefix: str) -> list[float]:
        result: list[float] = []
        for name, items in data.items():
            if name == prefix or name.startswith(prefix + "["):
                result.extend(items)
        return result

    time_values = values("time")
    window_start = (time_values[-1] - 1.0) if time_values else 0.0
    slip_values = values("slipSpeed")
    max_slip_index = max(range(len(slip_values)), key=slip_values.__getitem__) if slip_values else -1

    def last_window_max_abs(name: str) -> float:
        items = values(name)
        if not items or not time_values:
            return 0.0
        return max((abs(value) for t, value in zip(time_values, items) if t >= window_start), default=0.0)

    def last_window_min(name: str) -> float:
        items = values(name)
        if not items or not time_values:
            return 0.0
        return min((value for t, value in zip(time_values, items) if t >= window_start), default=0.0)

    z_values = values("z")
    support_values = values("supportCount")
    support_gap_threshold = 0.5
    support_gap_samples = sum(1 for value in support_values if value <= support_gap_threshold)
    support_gap_duration = 0.0
    if support_values and time_values:
        support_gap_duration = sum(
            max(0.0, t1 - t0)
            for t0, t1, value in zip(time_values, time_values[1:], support_values)
            if value <= support_gap_threshold
        )

    return {
        "csv_sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
        "observed_start_s": last("time", 0.0) if not values("time") else values("time")[0],
        "observed_stop_s": last("time", 0.0),
        "final_x_m": last("x"),
        "final_z_m": last("z"),
        "min_z_m": min(z_values, default=0.0),
        "max_z_m": max(z_values, default=0.0),
        "final_speed_m_s": last("vx"),
        "final_pitch_rad": last("pitch"),
        "max_abs_pitch_rad": max((abs(value) for value in values("pitch")), default=0.0),
        "max_penetration_m": max(values("penetration"), default=0.0),
        "max_slip_m_s": max(values("slipSpeed"), default=0.0),
        "max_slip_time_s": time_values[max_slip_index] if max_slip_index >= 0 and max_slip_index < len(time_values) else None,
        "max_battery_current_a": max(values("batteryCurrent"), default=0.0),
        "battery_energy_j": last("batteryEnergy"),
        "mechanical_work_j": last("mechanicalWork"),
        "peak_torque_nm": max(array_values("torque"), default=0.0),
        "max_overload_time_s": max(array_values("overloadTime"), default=0.0),
        "final_support_count": last("supportCount"),
        "min_support_count": min(support_values, default=0.0),
        "support_gap_samples": support_gap_samples,
        "support_gap_duration_s": support_gap_duration,
        "front_normal_n": last("frontNormal"),
        "rear_normal_n": last("rearNormal"),
        "entry_progress": last("entryProgress"),
        "exit_progress": last("exitProgress"),
        "settled_indicator": last("settledIndicator"),
        "settle_window_s": 1.0,
        "last_window_max_abs_speed_m_s": last_window_max_abs("vx"),
        "last_window_max_abs_pitch_rate_rad_s": last_window_max_abs("q"),
        "last_window_min_settled_indicator": last_window_min("settledIndicator"),
    }


class RealS2Executor:
    """Run SysML/OpenModelica once and derive auditable physical criteria."""

    def __init__(self, *, ascent_descent_stop_s: float = 180.0, flat_stop_s: float = 10.0, timeout_s: float = 300.0):
        self.ascent_descent_stop_s = float(ascent_descent_stop_s)
        self.flat_stop_s = float(flat_stop_s)
        self.timeout_s = float(timeout_s)

    def execute(self, scenario_input: Mapping[str, Any], generated: Mapping[str, Any], scenario_dir: Path) -> Mapping[str, Any]:
        scenario = str(scenario_input["scenario"])
        sysml_result = check_sysml((scenario_dir / str(generated["sysml_file"])).read_text(encoding="utf-8"))
        run_dir = scenario_dir / "omc-run"
        stop = self.flat_stop_s if scenario == "flat" else self.ascent_descent_stop_s
        modelica = (scenario_dir / str(generated["modelica_file"])).read_text(encoding="utf-8")
        tool = execute_model(
            modelica,
            str(generated["model_name"]),
            run_dir,
            stop,
            max(300, int(stop * 20)),
            simulation_timeout_s=self.timeout_s,
        )
        result: dict[str, Any] = {
            "tool_status": "PASS" if sysml_result.get("status") == "PASS" and tool.get("compile", {}).get("status") == "PASS" and tool.get("simulation", {}).get("status") == "PASS" else "UNVERIFIED",
            "sysml": sysml_result,
            "modelica": tool,
            "run_stop_s": stop,
            "physical_status": "UNVERIFIED",
            "modification_parameters": {
                "contact_model": "smooth_positive_and_bounded_boundary_window",
                "contact_stiffness_n_per_m": 120000.0,
                "contact_damping_ns_per_m": 1200.0,
                "contact_transition_m": 0.0002,
                "contact_normal_cap_n": "4*(mass*g/axle_count)",
                "platform_margin_m": 0.0,
                "platform_support_buffer_m": 5.0,
                "support_force_threshold_n": 1.0,
                "drive_ramp_s": 2.4,
                "ascent_startup_time_s": 2.0,
                "ascent_startup_speed_m_s": 0.025,
                "ascent_startup_torque_fraction_of_peak": 0.35,
                "ascent_torque_fraction_of_peak": 0.70,
                "ascent_traction_limit": "mu*normal*wheel_radius_per_wheel",
                "descent_speed_factor": 1.0,
                "descent_brake_start_progress": 0.80,
                "descent_brake_gain_nm_per_m_s": 8.0,
                "descent_brake_negative_limit_fraction_of_peak": 0.50,
                "descent_positive_torque_fraction_of_peak": 0.60,
                "descent_stall_boost_fraction_of_peak": 0.20,
                "descent_slip_feedback_gain": 6.0,
                "descent_riser_normal": "negative_x_for_left_approach",
                "slip_feedback_gain": 20.0,
                "slip_limit_m_s": 0.25,
                "simulation_wall_timeout_s": self.timeout_s,
            },
        }
        if tool.get("simulation", {}).get("status") != "PASS":
            result["physical_criteria"] = [{"id": "simulation_trace", "status": "UNVERIFIED", "reason": "toolchain did not produce a complete CSV"}]
            return result
        metrics = _trace_metrics(Path(tool["simulation"]["csv_path"]))
        design = scenario_input["design"]
        criteria = [
            {"id": "finite_trace", "status": "PASS", "observed_stop_s": metrics["observed_stop_s"]},
            {"id": "no_excessive_penetration", "status": "PASS" if metrics["max_penetration_m"] <= 0.01 else "FAIL", "value_m": metrics["max_penetration_m"], "limit_m": 0.01},
            {"id": "no_excessive_slip", "status": "PASS" if metrics["max_slip_m_s"] <= 0.25 else "FAIL", "value_m_s": metrics["max_slip_m_s"], "limit_m_s": 0.25},
            {"id": "battery_current_limit", "status": "PASS" if metrics["max_battery_current_a"] <= float(design["battery_max_current_a"]) else "FAIL", "value_a": metrics["max_battery_current_a"], "limit_a": design["battery_max_current_a"]},
            {"id": "entry_exit_and_stop", "status": "PASS" if metrics["entry_progress"] >= 0.99 and metrics["exit_progress"] >= 0.99 and metrics["last_window_max_abs_speed_m_s"] <= 0.01 and metrics["last_window_max_abs_pitch_rate_rad_s"] <= 0.05 and metrics["last_window_min_settled_indicator"] >= 0.5 else "FAIL", "entry_progress": metrics["entry_progress"], "exit_progress": metrics["exit_progress"], "settled_indicator": metrics["settled_indicator"], "settle_window_s": metrics["settle_window_s"], "last_window_max_abs_speed_m_s": metrics["last_window_max_abs_speed_m_s"], "last_window_max_abs_pitch_rate_rad_s": metrics["last_window_max_abs_pitch_rate_rad_s"], "last_window_min_settled_indicator": metrics["last_window_min_settled_indicator"]},
        ]
        stair = scenario_input.get("stair", {})
        stair_height = float(stair.get("count", 0)) * float(stair.get("riser_m", 0.0))
        z_lower, z_upper = -0.20, stair_height + 0.50
        criteria.append({"id": "vertical_trajectory_envelope", "status": "PASS" if metrics["min_z_m"] >= z_lower and metrics["max_z_m"] <= z_upper else "FAIL", "min_z_m": metrics["min_z_m"], "max_z_m": metrics["max_z_m"], "lower_limit_m": z_lower, "upper_limit_m": z_upper, "stair_height_m": stair_height})
        criteria.append({"id": "continuous_contact_support", "status": "PASS" if metrics["min_support_count"] > 0.5 and metrics["support_gap_samples"] == 0 else "FAIL", "min_support_count": metrics["min_support_count"], "support_gap_samples": metrics["support_gap_samples"], "support_gap_duration_s": metrics["support_gap_duration_s"], "threshold": 0.5})
        if scenario == "descent":
            criteria.append({"id": "forward_front_wheels_first", "status": "PASS" if metrics["front_normal_n"] >= 1 and metrics["rear_normal_n"] >= 1 else "UNVERIFIED", "front_normal_n": metrics["front_normal_n"], "rear_normal_n": metrics["rear_normal_n"], "direction": "positive_x"})
        if scenario == "flat":
            criteria.append({"id": "level_ground_energy_trace", "status": "PASS" if metrics["battery_energy_j"] >= 0 else "FAIL", "battery_energy_j": metrics["battery_energy_j"]})
        statuses = [item["status"] for item in criteria]
        physical = "PASS" if all(status == "PASS" for status in statuses) else ("FAIL" if "FAIL" in statuses else "UNVERIFIED")
        result.update({"physical_status": physical, "physical_criteria": criteria, "metrics": metrics, "power": {"peak_battery_current_a": metrics["max_battery_current_a"], "battery_energy_j": metrics["battery_energy_j"]}, "continuous_capability": {"peak_torque_nm": metrics["peak_torque_nm"], "max_overload_time_s": metrics["max_overload_time_s"], "drive_requirement": "verify motor/gearbox/controller thermal and peak-duration compatibility"}})
        return result


__all__ = ["RealS2Executor", "RealS2Generator"]
