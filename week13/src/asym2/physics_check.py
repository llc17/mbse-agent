"""Independent CSV geometry, force, impulse and energy diagnostics.

Copied from week13/sixwheel_fix/check_results.py; only the acceptance file path
changes. That JSON contains the original pre-run numeric thresholds, no scope.
The terrain here is a closed solid polygon, not the model's contact flags or
its floor/index height function. Wheel groups only are represented: this
checker cannot establish chassis clearance or real vehicle capability.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET

import numpy as np

BASELINE = dict(m=85., g=9.81, I_pitch=20., R_main=.127, R_nose=.07,
                a=.22, b=.23, L_nose_CoM=.54, h_nose=.19,
                x_start_up=2., len_flat=2., T_main_max=60., T_nose_max=25.,
                k_ground=4e4, c_ground=3e3, mu_s=.85, v_eps=.005,
                target_speed=.4, speed_error_scale=.1, driveScale=1.,
                **{"wheel_J[1]": .02205, "wheel_J[2]": .145161, "wheel_J[3]": .145161})
STATE = ("x", "z", "psi", "v", "vz", "omega_psi", "u[1]", "u[2]", "u[3]")
SCALARS = (*STATE, "x_n", "z_n", "x_f", "z_f", "x_r", "z_r", "Ekin", "Epot",
           "Ucontact", "Etotal", "Wdrive", "Wdiss", "Pdrive", "Pdiss", "energyResidual",
           "Fx_total", "Fz_total", "Mcontact_total", "power_factor")
ARRAYS = ("Fn", "Ft", "vslip", "delta", "gap", "valid", "vn", "nx", "nz", "px", "pz")
NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf8")


def terrain_polygon(step_h, step_d, n_steps, start_up=2., len_flat=2.,
                    left=-1000., right=None, bottom=-1000.):
    """Full exposed boundary plus distant closing edges, counterclockwise.

    Up risers are at start_up+j*step_d, j=0..n-1. The final tread
    joins the platform; down risers start at start_up+n*step_d+len_flat.
    The exit coordinate retains the original task's final tread depth.
    """
    n = int(n_steps)
    if n < 1 or step_h <= 0 or step_d <= 0 or len_flat < 0:
        raise ValueError("invalid terrain dimensions")
    down = start_up + n * step_d + len_flat
    end = down + n * step_d
    right = end + 1000 if right is None else right
    if left >= start_up or right <= end or bottom >= 0:
        raise ValueError("polygon closing edges must be outside the task")
    points = [(left, 0.)]
    for j in range(n):
        edge = start_up + j * step_d
        points.extend(((edge, j * step_h), (edge, (j + 1) * step_h)))
    for j in range(n):
        edge = down + j * step_d
        points.extend(((edge, (n - j) * step_h), (edge, (n - j - 1) * step_h)))
    points.extend(((right, 0.), (right, bottom), (left, bottom)))
    # Exact repeated vertices do not contribute edges or affect ray crossings.
    cleaned = [points[0]]
    for point in points[1:]:
        if point != cleaned[-1]:
            cleaned.append(point)
    return np.asarray(cleaned, dtype=float)


def point_solid_geometry(points, polygon, boundary_tolerance=1e-10):
    """True minimum distance to every segment and independent ray-crossing inside.

    A point on the boundary is reported separately, not ambiguously inside.
    This accepts Nx2 points and does not consult contact flags or floor heights.
    """
    points = np.atleast_2d(np.asarray(points, dtype=float))
    polygon = np.asarray(polygon, dtype=float)
    nearest_sq = np.full(len(points), np.inf)
    nearest_edge = np.full(len(points), -1, dtype=int)
    inside = np.zeros(len(points), dtype=bool)
    x, z = points[:, 0], points[:, 1]
    for index, a in enumerate(polygon):
        b = polygon[(index + 1) % len(polygon)]
        edge = b - a
        length_sq = float(edge @ edge)
        if not length_sq:
            continue
        t = np.clip(((points - a) @ edge) / length_sq, 0., 1.)
        distance_sq = np.sum((points - (a + t[:, None] * edge)) ** 2, axis=1)
        better = distance_sq < nearest_sq
        nearest_sq[better] = distance_sq[better]
        nearest_edge[better] = index
        if edge[1] != 0:
            crossing = ((a[1] > z) != (b[1] > z))
            crossing &= x < a[0] + (z - a[1]) * edge[0] / edge[1]
            inside ^= crossing
    distance = np.sqrt(nearest_sq)
    boundary = distance <= boundary_tolerance
    return dict(distance=distance, inside=inside & ~boundary, boundary=boundary,
                signed_distance=np.where(inside & ~boundary, -distance, distance),
                nearest_edge=nearest_edge)


def wheel_centers(data, parameters):
    x, z, psi = (data[key] for key in ("x", "z", "psi"))
    c, s = np.cos(psi), np.sin(psi)
    offsets = ((parameters["L_nose_CoM"], parameters["h_nose"] - parameters["R_main"]),
               (parameters["a"], 0.), (-parameters["b"], 0.))
    return [np.column_stack((x + bx * c - bz * s, z + bx * s + bz * c)) for bx, bz in offsets]


def cumulative_trapezoid(values, times):
    values = np.asarray(values)
    increments = .5 * (values[1:] + values[:-1]) * np.diff(times)
    return np.r_[0., np.cumsum(increments)]


def residual_metric(residual, scale=None):
    residual = np.asarray(residual)
    result = dict(max_abs=float(np.max(np.abs(residual))), final=float(residual[-1]),
                  rms=float(np.sqrt(np.mean(residual ** 2))))
    if scale is not None:
        denominator = max(1., float(np.max(np.abs(scale))))
        result.update(normalization_scale=denominator, max_relative=result["max_abs"] / denominator)
    return result


def read_runtime_line(path):
    lines = [line.strip() for line in Path(path).read_text(encoding="utf8", errors="replace").splitlines()
             if re.match(r"^\s*time\s*=", line)]
    if len(lines) != 1:
        raise ValueError("same-run stdout must contain exactly one final output line")
    values = {}
    for token in lines[0].split(","):
        match = re.fullmatch(r"\s*([A-Za-z_]\w*(?:\[\d+\])?)\s*=\s*(" + NUMBER + r")\s*", token, re.ASCII)
        if not match or match[1] in values:
            raise ValueError("invalid or duplicate final runtime output")
        values[match[1]] = float(match[2])
    return values


def evidence(run_dir):
    """Independently bind files and parse actual stdout, not runner's PASS label."""
    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf8"))
    issues = []
    for key in ("source", "wrapper", "executable", "init_xml", "build_manifest"):
        record = manifest[key]
        if not Path(record["path"]).is_file() or sha(record["path"]) != record["sha256"]:
            issues.append("file binding changed: " + key)
    for key, record in manifest["files"].items():
        if record["sha256"] is None or not Path(record["path"]).is_file() or sha(record["path"]) != record["sha256"]:
            issues.append("run output binding missing/changed: " + key)
    build = json.loads(Path(manifest["build_manifest"]["path"]).read_text(encoding="utf8"))
    actual = read_runtime_line(manifest["files"]["stdout"]["path"])
    direct_actual = dict(actual)
    expected = dict(BASELINE)
    expected.update(build["requested_geometry"])
    expected.update(manifest["requested_overrides"])
    expected["x_end_up"] = expected["x_start_up"] + expected["n_steps"] * expected["stepD"]
    expected["x_start_down"] = expected["x_end_up"] + expected["len_flat"]
    expected["x_end_down"] = expected["x_start_down"] + expected["n_steps"] * expected["stepD"]
    expected["H_peak"] = expected["n_steps"] * expected["stepH"]
    expected["nReach"] = math.ceil(max(expected["R_main"], expected["R_nose"]) / expected["stepD"])
    expected["nLocal"] = 2 * expected["nReach"] + 1
    expected["nContact"] = 3 + 3 * expected["nLocal"]
    # The first two owned runs predate export of calculatedParameters without
    # XML starts. Preserve them: terrain endpoints may be explicitly derived
    # from directly read-back dimensions; the nose offset is recovered below
    # from its actual reported centers, rather than substituted from requests.
    recoverable = {"L_nose_CoM", "x_end_up", "x_start_down", "x_end_down"} - set(manifest["requested_output_parameters"])
    if "x_end_up" not in actual and "x_end_up" in recoverable:
        actual["x_end_up"] = actual["x_start_up"] + actual["n_steps"] * actual["stepD"]
    if "x_start_down" not in actual and "x_start_down" in recoverable:
        actual["x_start_down"] = actual["x_end_up"] + actual["len_flat"]
    if "x_end_down" not in actual and "x_end_down" in recoverable:
        actual["x_end_down"] = actual["x_start_down"] + actual["n_steps"] * actual["stepD"]
    mismatch = {key: dict(expected=value, actual=actual.get(key)) for key, value in expected.items()
                if not (key == "L_nose_CoM" and key in recoverable and key not in actual)
                and (key not in actual or not math.isclose(value, actual[key], rel_tol=1e-8, abs_tol=1e-9))}
    if mismatch:
        issues.append("actual parameter readback is missing or differs from the bound requests")
    xml_values = {}
    for node in ET.parse(manifest["init_xml"]["path"]).iter("ScalarVariable"):
        child = next(iter(node), None)
        if child is not None and child.get("start") is not None:
            try:
                xml_values[node.get("name")] = float(child.get("start"))
            except ValueError:
                pass
    compiled_mismatch = {key: dict(expected=value, compiled=xml_values.get(key))
                         for key, value in build.get("compiled_geometry_expected", {}).items()
                         if key not in xml_values or not math.isclose(value, xml_values[key], rel_tol=1e-10, abs_tol=1e-12)}
    if compiled_mismatch:
        issues.append("compiled structural readback differs")
    return manifest, actual, dict(status="PASS" if not issues else "FAIL", issues=issues,
                                 parameter_mismatch=mismatch, compiled_geometry_mismatch=compiled_mismatch,
                                 actual=actual, directly_read_back=direct_actual,
                                 derived_endpoint_parameters={key: actual[key] for key in ("x_end_up", "x_start_down", "x_end_down")
                                                              if key not in direct_actual and key in actual},
                                 expected=expected)


def read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        header = next(csv.reader(stream))
    if len(header) != len(set(header)):
        raise ValueError("duplicate CSV column name")
    indices = [i for i, name in enumerate(header)
               if name == "time" or name in SCALARS or name in BASELINE or
               re.fullmatch(r"(?:Fn|Ft|vslip|delta|gap|valid|vn|nx|nz|px|pz)\[\d+,\d+\]", name) or
               re.fullmatch(r"(?:T|u|Fx_wheel|Fz_wheel)\[\d+\]", name)]
    values = np.loadtxt(path, delimiter=",", skiprows=1, usecols=indices, ndmin=2)
    return {header[index]: values[:, column] for column, index in enumerate(indices)}, len(header)


def array(data, name, wheel, count):
    names = [f"{name}[{wheel},{j}]" for j in range(1, count + 1)]
    if any(key not in data for key in names):
        raise ValueError("contact CSV array missing: " + name + " wheel " + str(wheel))
    return np.column_stack([data[key] for key in names])


def first_time(mask, times):
    where = np.flatnonzero(mask)
    return float(times[where[0]]) if len(where) else None


def plot_trajectory(path, data, centers, polygon, parameters, summary):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    figure, axes = plt.subplots(2, 1, figsize=(11, 7), constrained_layout=True)
    p = np.vstack((polygon, polygon[0]))
    axes[0].fill(p[:, 0], p[:, 1], color="#cdd2d8", alpha=.75)
    axes[0].plot(data["x"], data["z"], label="CoM", color="black", linewidth=1.)
    for name, center, color in zip(("nose", "front", "rear"), centers, ("#cb6631", "#18659a", "#328357")):
        axes[0].plot(center[:, 0], center[:, 1], label=name, linewidth=.8, color=color)
    end = parameters["x_end_down"]
    axes[0].set(xlim=(min(-.5, float(np.min(data["x"])) - .5), max(end + 1., float(np.max(data["x"])) + 1.)),
                ylim=(min(-.15, float(np.min(data["z"])) - .2), max(parameters["H_peak"] + .6, float(np.max(data["z"])) + .2)),
                xlabel="World x (m)", ylabel="World z (m)", title=summary["classification"] + " - wheel-group diagnostic")
    axes[0].legend(ncol=4)
    axes[0].grid(alpha=.2)
    axes[1].plot(data["time"], data["v"], label="horizontal velocity (m/s)")
    axes[1].plot(data["time"], np.degrees(data["psi"]), label="pitch (deg)")
    axes[1].set(xlabel="Time (s)")
    axes[1].legend()
    axes[1].grid(alpha=.2)
    figure.savefig(path, dpi=150)
    plt.close(figure)


def check(run_dir, csv_path=None, expected_stop=None, make_plot=True,
          mission_stop=300., stall_window=5., stall_range=.005):
    run_dir = Path(run_dir).resolve()
    scope_path = Path(__file__).resolve().parent / "acceptance.json"
    acceptance = json.loads(scope_path.read_text(encoding="utf-8-sig"))["acceptance_before_vehicle_run"]
    center_tol = acceptance["center_inside_solid_tolerance_m"]
    compression_limit = acceptance["maximum_contact_compression_over_radius"]
    energy_limit = acceptance["normalized_energy_balance_residual_limit"]
    mission_stop = acceptance["formal_observation_seconds"]
    stall_window = acceptance["stall_observation_seconds"]
    stall_range = acceptance["stall_horizontal_position_range_m"]
    exit_margin = acceptance["complete_exit_margin_m"]
    summary = dict(schema="sixwheel-independent-check-1", checked_utc=datetime.now(timezone.utc).isoformat(),
                   classification="INCOMPLETE", scope="PLANAR_THREE_EQUIVALENT_WHEEL_GROUP_DIAGNOSTIC",
                   real_vehicle_capability="NOT_ASSESSED", chassis_collision="NOT_MODELLED",
                   acceptance_source=dict(path=str(scope_path), sha256=sha(scope_path), thresholds=acceptance),
                   residual_thresholds="ENERGY_AND_COMPRESSION_FROZEN_GUARDS_OTHER_RESIDUALS_RAW",
                   rules=dict(center_inside_tolerance_m=center_tol, numerical_force_abs_N=1e-5,
                              numerical_relative=1e-8, geometric_air_tolerance_m=1e-6,
                              mission_observation_s=mission_stop, stall_window_s=stall_window,
                              stall_x_range_m=stall_range), issues=[], invalidity_reasons=[])
    try:
        manifest, parameters, bound = evidence(run_dir)
        summary["evidence"] = bound
    except (OSError, ValueError, KeyError, ET.ParseError) as exc:
        summary.update(classification="TOOL_FAILURE", issues=["unable to bind actual execution evidence: " + str(exc)])
        return summary
    process = manifest.get("process", {})
    tool_ok = (process.get("returncode") == 0 and process.get("timed_out") is False
               and process.get("cleanup_confirmed") is True and not process.get("launch_error"))
    summary["tool_process"] = dict(status="PASS" if tool_ok else "FAIL", returncode=process.get("returncode"),
                                   timed_out=process.get("timed_out"), elapsed_s=process.get("elapsed_s"))
    csv_path = Path(csv_path).resolve() if csv_path else Path(manifest["files"]["csv"]["path"])
    if str(csv_path) != str(Path(manifest["files"]["csv"]["path"]).resolve()):
        summary.update(classification="TOOL_FAILURE", issues=["CSV path differs from same-run binding"])
        return summary
    try:
        data, total_columns = read_csv(csv_path)
    except (OSError, ValueError, StopIteration) as exc:
        summary.update(classification="TOOL_FAILURE", issues=["CSV unreadable: " + str(exc)])
        return summary
    summary["csv"] = dict(path=str(csv_path), sha256=sha(csv_path), rows=len(next(iter(data.values()), [])),
                          total_columns=total_columns, inspected_columns=len(data))
    missing = [name for name in ("time", *STATE) if name not in data]
    if missing or summary["csv"]["rows"] < 2:
        summary.update(classification="TOOL_FAILURE", issues=["missing state series or too few rows: " + ",".join(missing)])
        return summary
    if any(not np.all(np.isfinite(values)) for values in data.values()):
        summary.update(classification="INVALID_MODEL" if tool_ok else "TOOL_FAILURE",
                       invalidity_reasons=["nonfinite states or inspected forces/energy"])
        return summary
    t = data["time"]
    stop = float(expected_stop if expected_stop is not None else manifest["requested_stop"])
    reached_stop = bool(abs(t[0]) <= 1e-8 and abs(t[-1] - stop) <= max(1e-7, stop * 1e-8) and np.all(np.diff(t) >= 0))
    summary["observation"] = dict(start=float(t[0]), end=float(t[-1]), requested_stop=stop,
                                  reached_requested_stop=reached_stop,
                                  full_mission_observation=bool(reached_stop and stop >= mission_stop - 1e-7),
                                  max_output_gap_s=float(np.max(np.diff(t))))
    if "L_nose_CoM" not in parameters:
        if "x_n" not in data or "z_n" not in data:
            summary.update(classification="TOOL_FAILURE", issues=["missing calculated nose offset and actual nose centers"])
            return summary
        actual_offset = (data["x_n"] - data["x"]) * np.cos(data["psi"]) + (data["z_n"] - data["z"]) * np.sin(data["psi"])
        parameters["L_nose_CoM"] = float(np.median(actual_offset))
        bound["nose_offset_recovered_from_actual_centers"] = dict(value=parameters["L_nose_CoM"],
                                                                  max_variation_m=float(np.ptp(actual_offset)))
        if abs(parameters["L_nose_CoM"] - BASELINE["L_nose_CoM"]) > 1e-7 or np.ptp(actual_offset) > 1e-7:
            bound["status"] = "FAIL"
            bound["parameter_mismatch"]["L_nose_CoM"] = dict(expected=BASELINE["L_nose_CoM"], actual=parameters["L_nose_CoM"])
    if bound["status"] != "PASS":
        summary["invalidity_reasons"].append("actual parameter or file binding mismatch")
        # Requested parameters must not fill unknown or mismatched runtime facts.
        if bound["parameter_mismatch"]:
            summary["classification"] = "INVALID_MODEL" if tool_ok else "TOOL_FAILURE"
            return summary
    p = parameters
    centers = wheel_centers(data, p)
    all_centers = np.vstack(centers)
    polygon = terrain_polygon(p["stepH"], p["stepD"], round(p["n_steps"]), p["x_start_up"], p["len_flat"],
                              left=min(-1., float(np.min(all_centers[:, 0])) - 2.),
                              right=max(p["x_end_down"] + 1., float(np.max(all_centers[:, 0])) + 2.),
                              bottom=min(-10., float(np.min(all_centers[:, 1])) - 2.))
    radii = (p["R_nose"], p["R_main"], p["R_main"])
    summary["wheels"] = {}
    geometry = []
    completed_masks = []
    for name, short, center, radius in zip(("nose", "front", "rear"), ("n", "f", "r"), centers, radii):
        geom = point_solid_geometry(center, polygon)
        geometry.append(geom)
        compression = np.maximum(0., radius - geom["distance"])
        maximum = int(np.argmax(compression))
        center_inside = geom["inside"] & (geom["distance"] > center_tol)
        model_center_error = None
        if "x_" + short in data and "z_" + short in data:
            model_center_error = float(np.max(np.hypot(data["x_" + short] - center[:, 0], data["z_" + short] - center[:, 1])))
            if model_center_error > 1e-7:
                summary["invalidity_reasons"].append(name + " reported center differs from rigid-body transform")
        exited = center[:, 0] - radius > p["x_end_down"] + exit_margin
        completed_masks.append(exited)
        summary["wheels"][name] = dict(radius_m=radius, inside_samples=int(np.count_nonzero(center_inside)),
                                      first_inside_s=first_time(center_inside, t),
                                      deepest_center_inside_m=float(np.max(np.where(center_inside, geom["distance"], 0.))),
                                      max_compression_m=float(compression[maximum]),
                                      max_compression_over_radius=float(compression[maximum] / radius),
                                      max_compression_time_s=float(t[maximum]),
                                      max_compression_center_m=center[maximum].tolist(),
                                      nearest_boundary_edge_at_max_compression_m=[polygon[geom["nearest_edge"][maximum]].tolist(),
                                          polygon[(geom["nearest_edge"][maximum] + 1) % len(polygon)].tolist()],
                                      reported_center_max_error_m=model_center_error,
                                      first_fully_past_exit_s=first_time(exited, t))
        if np.any(center_inside):
            summary["invalidity_reasons"].append(name + " center entered the solid polygon")
        if compression[maximum] / radius > compression_limit:
            summary["invalidity_reasons"].append(name + " compression/radius exceeds the pre-run model validity guard")
    fully_out = np.logical_and.reduce(completed_masks)
    completion = first_time(fully_out, t)
    front_down = first_time(centers[1][:, 0] > p["x_start_down"], t)
    rear_down = first_time(centers[2][:, 0] > p["x_start_down"], t)
    nose_down = first_time(centers[0][:, 0] > p["x_start_down"], t)
    front_first = (front_down is not None and rear_down is not None and front_down <= rear_down)
    summary["route"] = dict(completed=completion is not None, completion_time_s=completion,
                            final_all_wheels_out=bool(fully_out[-1]), exit_line_x_m=p["x_end_down"] + exit_margin,
                            nose_crossed_down_edge_s=nose_down, front_main_crossed_down_edge_s=front_down,
                            rear_main_crossed_down_edge_s=rear_down, front_main_before_rear_main=front_first,
                            final_x_m=float(data["x"][-1]), final_z_m=float(data["z"][-1]),
                            max_abs_pitch_deg=float(np.max(np.abs(np.degrees(data["psi"])))))
    kinematics = {}
    for position, velocity in (("x", "v"), ("z", "vz"), ("psi", "omega_psi")):
        change = data[position] - data[position][0]
        kinematics[position] = residual_metric(change - cumulative_trapezoid(data[velocity], t), change)
    summary["kinematic_integral_residuals"] = kinematics
    count = round(p["nContact"])
    required_energy = ("Ekin", "Epot", "Ucontact", "Etotal", "Wdrive", "Wdiss", "Pdrive", "Pdiss")
    energy_complete = all(key in data for key in required_energy)
    if not energy_complete:
        summary["issues"].append("energy guard requires missing series: " + ",".join(key for key in required_energy if key not in data))
    wheel_j = np.array([p[f"wheel_J[{i}]"] for i in range(1, 4)])
    omega = data["omega_psi"]
    u = np.column_stack([data[f"u[{i}]"] for i in range(1, 4)])
    independent_ekin = .5 * p["m"] * (data["v"] ** 2 + data["vz"] ** 2)
    independent_ekin += .5 * (p["I_pitch"] - np.sum(wheel_j)) * omega ** 2
    independent_ekin += .5 * np.sum(wheel_j * (u - omega[:, None]) ** 2, axis=1)
    independent_epot = p["m"] * p["g"] * data["z"]
    force_x, force_z, torque = np.zeros(len(t)), np.zeros(len(t)), np.zeros(len(t))
    recomputed_ucontact, friction_loss, normal_loss = np.zeros(len(t)), np.zeros(len(t)), np.zeros(len(t))
    torque_inputs, wheel_contact = [], []
    summary["contacts"] = {}
    contact_complete = True
    for i, (name, center, radius, geom) in enumerate(zip(("nose", "front", "rear"), centers, radii, geometry), 1):
        try:
            arrays = {key: array(data, key, i, count) for key in ARRAYS}
            motor_torque = data[f"T[{i}]"]
        except (ValueError, KeyError) as exc:
            summary["issues"].append(str(exc))
            contact_complete = False
            continue
        fn, ft, slip = (arrays[key] for key in ("Fn", "Ft", "vslip"))
        nx, nz, delta = (arrays[key] for key in ("nx", "nz", "delta"))
        valid = arrays["valid"] > .5
        force_scale = max(1., float(np.max(np.abs(fn))), float(np.max(np.abs(ft))))
        force_tol = 1e-5 + 1e-8 * force_scale
        friction_excess = np.abs(ft) - p["mu_s"] * fn
        contact_air = (~valid) | (arrays["gap"] >= 0)
        geometric_air = (~geom["inside"]) & (geom["distance"] > radius + 1e-6)
        circle_px = center[:, 0, None] - radius * nx
        circle_pz = center[:, 1, None] - radius * nz
        vx_at_p = data["v"][:, None] - omega[:, None] * (circle_pz - data["z"][:, None])
        vz_at_p = data["vz"][:, None] + omega[:, None] * (circle_px - data["x"][:, None])
        actual_slip = vx_at_p * nz - vz_at_p * nx - radius * u[:, i - 1, None]
        fx, fz = fn * nx + ft * nz, fn * nz - ft * nx
        wheel_fx, wheel_fz = np.sum(fx, axis=1), np.sum(fz, axis=1)
        force_x += wheel_fx
        force_z += wheel_fz
        wheel_moment = np.sum((circle_px - data["x"][:, None]) * fz - (circle_pz - data["z"][:, None]) * fx, axis=1)
        torque += wheel_moment
        recomputed_ucontact += .5 * p["k_ground"] * np.sum(delta ** 2, axis=1)
        friction_loss += -np.sum(ft * actual_slip, axis=1)
        normal_loss += np.sum((p["k_ground"] * delta - fn) * arrays["vn"], axis=1)
        independent_compression = np.maximum(0., radius - geom["distance"])
        valid_delta = np.where(valid, np.maximum(0., -arrays["gap"]), 0.)
        raw = dict(min_normal_N=float(np.min(fn)),
                   max_friction_bound_excess_N=float(max(0., np.max(friction_excess))),
                   max_positive_friction_power_W=float(max(0., np.max(ft * actual_slip))),
                   max_air_contact_force_N=float(np.max(np.where(contact_air, np.hypot(fn, ft), 0.))),
                   max_geometrically_airborne_force_N=float(np.max(np.where(geometric_air, np.hypot(wheel_fx, wheel_fz), 0.))),
                   max_slip_recompute_error_m_s=float(np.max(np.abs(slip - actual_slip))),
                   max_contact_point_error_m=float(np.max(np.hypot(arrays["px"] - circle_px, arrays["pz"] - circle_pz))),
                   max_delta_internal_error_m=float(np.max(np.abs(delta - valid_delta))),
                   max_compression_coverage_error_m=float(np.max(np.abs(np.max(delta, axis=1) - independent_compression))),
                   max_valid_normal_norm_error=float(np.max(np.where(valid, np.abs(nx * nx + nz * nz - 1.), 0.))),
                   mean_abs_motor_torque_Nm=float(np.mean(np.abs(motor_torque))),
                   final_center_m=center[-1].tolist(),
                   final_contact_normal_resultant_N=float(np.sum(fn[-1])),
                   final_strongest_contact=dict(slot=int(np.argmax(fn[-1])) + 1,
                                                Fn_N=float(np.max(fn[-1])),
                                                point_m=[float(circle_px[-1, np.argmax(fn[-1])]), float(circle_pz[-1, np.argmax(fn[-1])])]),
                   numerical_force_tolerance_N=force_tol)
        summary["contacts"][name] = raw
        if raw["min_normal_N"] < -force_tol or raw["max_friction_bound_excess_N"] > force_tol:
            summary["invalidity_reasons"].append(name + " normal/friction force bound violated")
        if raw["max_positive_friction_power_W"] > force_tol * max(1., float(np.max(np.abs(actual_slip)))):
            summary["invalidity_reasons"].append(name + " friction adds energy at the contact")
        if max(raw["max_air_contact_force_N"], raw["max_geometrically_airborne_force_N"]) > force_tol:
            summary["invalidity_reasons"].append(name + " contact force acts in air")
        if max(raw["max_contact_point_error_m"], raw["max_delta_internal_error_m"]) > 1e-7 or raw["max_slip_recompute_error_m_s"] > 1e-6:
            summary["invalidity_reasons"].append(name + " contact kinematics inconsistent")
        if raw["max_compression_coverage_error_m"] > 1e-6 or raw["max_valid_normal_norm_error"] > 1e-7:
            summary["invalidity_reasons"].append(name + " contact geometry disagrees with the independent boundary")
        torque_inputs.append(motor_torque)
        wheel_contact.append(np.sum(ft, axis=1))
    if contact_complete:
        net_fx, net_fz = force_x - 50. * data["v"], force_z - p["m"] * p["g"]
        momentum_x = p["m"] * (data["v"] - data["v"][0])
        momentum_z = p["m"] * (data["vz"] - data["vz"][0])
        angular_momentum = p["I_pitch"] * omega - u @ wheel_j
        angular_change = angular_momentum - angular_momentum[0]
        impulses = dict(horizontal_Ns=residual_metric(momentum_x - cumulative_trapezoid(net_fx, t), momentum_x),
                        vertical_Ns=residual_metric(momentum_z - cumulative_trapezoid(net_fz, t), momentum_z),
                        angular_Nms=residual_metric(angular_change - cumulative_trapezoid(torque - 300. * omega, t), angular_change))
        for i, (radius, j, applied, tangential) in enumerate(zip(radii, wheel_j, torque_inputs, wheel_contact), 1):
            spin = j * ((u[:, i - 1] - omega) - (u[0, i - 1] - omega[0]))
            impulses[f"wheel_{i}_Nms"] = residual_metric(spin - cumulative_trapezoid(applied - radius * tangential, t), spin)
        summary["impulse_residuals"] = impulses
        independent_total = independent_ekin + independent_epot + recomputed_ucontact
        independent_pdrive = np.sum(np.column_stack(torque_inputs) * u, axis=1)
        independent_pdiss = friction_loss + normal_loss + 50. * data["v"] ** 2 + 300. * omega ** 2
        energy = dict(kinetic_recompute_J=residual_metric(data["Ekin"] - independent_ekin) if "Ekin" in data else None,
                      potential_recompute_J=residual_metric(data["Epot"] - independent_epot) if "Epot" in data else None,
                      elastic_recompute_J=residual_metric(data["Ucontact"] - recomputed_ucontact) if "Ucontact" in data else None,
                      total_recompute_J=residual_metric(data["Etotal"] - independent_total) if "Etotal" in data else None,
                      drive_power_recompute_W=residual_metric(data["Pdrive"] - independent_pdrive) if "Pdrive" in data else None,
                      dissipation_power_recompute_W=residual_metric(data["Pdiss"] - independent_pdiss) if "Pdiss" in data else None,
                      min_recomputed_dissipation_W=float(np.min(independent_pdiss)))
        if "Wdrive" in data and "Wdiss" in data:
            wd, wl = data["Wdrive"] - data["Wdrive"][0], data["Wdiss"] - data["Wdiss"][0]
            residual = independent_total - independent_total[0] - wd + wl
            throughput = np.maximum.reduce((np.abs(independent_total), np.abs(wd), np.abs(wl)))
            energy["balance_using_integrated_work_J"] = residual_metric(residual, throughput)
            normalization = np.maximum(1., np.abs(independent_total - independent_total[0]) + np.abs(wd) + wl)
            normalized_residual = np.abs(residual) / normalization
            energy["pre_run_normalized_balance_guard"] = dict(
                maximum=float(np.max(normalized_residual)), final=float(normalized_residual[-1]),
                time_of_maximum_s=float(t[np.argmax(normalized_residual)]), limit=energy_limit,
                normalization="max(1 J, abs(E-E_initial)+abs(Wdrive)+Wdiss)",
                passed=bool(np.max(normalized_residual) <= energy_limit))
            if np.max(normalized_residual) > energy_limit:
                summary["invalidity_reasons"].append("normalized energy balance exceeds the pre-run model validity guard")
            energy["drive_work_vs_sampled_power_J"] = residual_metric(wd - cumulative_trapezoid(independent_pdrive, t), wd)
            energy["dissipation_work_vs_sampled_power_J"] = residual_metric(wl - cumulative_trapezoid(independent_pdiss, t), wl)
            if "energyResidual" in data:
                energy["model_residual_recompute_J"] = residual_metric(data["energyResidual"] - residual)
        energy["balance_using_sampled_power_J"] = residual_metric(independent_total - independent_total[0]
            - cumulative_trapezoid(independent_pdrive - independent_pdiss, t), independent_total)
        summary["energy"] = energy
        force_output = {}
        for key, recomputed in (("Fx_total", force_x), ("Fz_total", force_z), ("Mcontact_total", torque)):
            if key in data:
                force_output[key] = residual_metric(data[key] - recomputed)
        summary["force_sum_recompute"] = force_output
        is_static_flat = p["driveScale"] == 0 and all(np.all(center[:, 0] + radius < p["x_start_up"])
                                                    for center, radius in zip(centers, radii))
        if is_static_flat and t[-1] >= 1.:
            settled = t >= t[-1] - 1.
            expected_support = p["m"] * p["g"]
            relative_error = np.abs(force_z[settled] - expected_support) / expected_support
            support_limit = acceptance["flat_static_support_relative_error_limit"]
            summary["flat_static_support"] = dict(
                window_s=1., expected_mg_N=expected_support, final_support_N=float(force_z[-1]),
                mean_support_N=float(np.mean(force_z[settled])),
                maximum_relative_error_last_second=float(np.max(relative_error)),
                limit=support_limit, passed=bool(np.max(relative_error) <= support_limit))
            if np.max(relative_error) > support_limit:
                summary["invalidity_reasons"].append("flat static support differs from mg beyond the pre-run guard")
    last = t >= max(float(t[0]), float(t[-1]) - stall_window)
    tail_drive = [float(np.mean(np.abs(data[f"T[{i}]"][last]))) for i in range(1, 4) if f"T[{i}]" in data]
    tail_support = float(np.mean(np.hypot(force_x[last], force_z[last]))) if contact_complete else None
    tail_range = float(np.ptp(data["x"][last]))
    stalled = bool(reached_stop and stop >= mission_stop - 1e-7 and t[-1] - t[last][0] >= stall_window - 1e-6
                   and tail_range < stall_range and tail_drive and max(tail_drive) > 1. and tail_support is not None
                   and tail_support > 1. and completion is None)
    summary["stall_observation"] = dict(window_s=float(t[-1] - t[last][0]), x_range_m=tail_range,
                                       mean_abs_motor_torques_Nm=tail_drive, mean_contact_resultant_N=tail_support,
                                       physical_stall_rule_met=stalled,
                                       rule_scope="Only a completed full observation may classify PHYSICAL_STALL")
    if not tool_ok:
        summary["classification"] = "TOOL_FAILURE"
    elif summary["invalidity_reasons"]:
        summary["classification"] = "INVALID_MODEL"
    elif not reached_stop or not contact_complete or not energy_complete:
        summary["classification"] = "INCOMPLETE"
    elif completion is not None and front_first:
        summary["classification"] = "PASS"
    elif stalled:
        summary["classification"] = "PHYSICAL_STALL"
    else:
        summary["classification"] = "INCOMPLETE"
    summary["classification_meaning"] = {
        "PASS": "Completed route with the reported hard consistency checks; residual magnitudes remain raw diagnostics.",
        "PHYSICAL_STALL": "Valid full observation stalled with continuing drive/contact; not a real-hardware impossibility proof.",
        "INCOMPLETE": "No complete valid mission outcome within this observation.",
        "INVALID_MODEL": "Independent hard model/evidence inconsistency detected; do not interpret route success as credible.",
        "TOOL_FAILURE": "Execution or result/evidence interface failed; not a physical task failure."
    }[summary["classification"]]
    if make_plot:
        try:
            path = run_dir / "trajectory.png"
            plot_trajectory(path, data, centers, polygon, p, summary)
            summary["trajectory_png"] = str(path)
        except (ImportError, OSError, ValueError) as exc:
            summary["issues"].append("plot unavailable: " + str(exc))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--stop", type=float, help="Expected observation end; defaults to the bound run request")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--no-plot", action="store_true")
    args = parser.parse_args()
    try:
        result = check(args.run_dir, args.csv, args.stop, not args.no_plot)
    except (OSError, ValueError, KeyError, ET.ParseError) as exc:
        result = dict(schema="sixwheel-independent-check-1", classification="TOOL_FAILURE", issues=[str(exc)])
    output = args.output or args.run_dir / "check.json"
    write_json(output, result)
    print(json.dumps(dict(classification=result["classification"], report=str(output.resolve()),
                          route=result.get("route"), issues=result.get("issues", [])), ensure_ascii=False, allow_nan=False))
    return 0 if result["classification"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
