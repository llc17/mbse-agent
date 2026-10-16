"""Independent analytical verification for the four C2 contact probes.

The verifier deliberately knows only the frozen prescribed trajectories and
the analytical stair geometry.  It does not read or execute Modelica source.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
import csv
import hashlib
import math
from pathlib import Path
from typing import Any


COLUMNS = (
    "time", "x", "z", "vx", "vz", "omega", "Fx", "Fz", "normal",
    "penetration", "slip", "elasticEnergy", "normalLoss",
    "frictionLoss", "featureId", "contactCount",
)

STOP_TIME_S = 0.01
BOUNDARY_TIME_S = 0.005
OUTPUT_INTERVAL_S = 1e-5
RADIUS_M = 0.127
FIXED_NORMAL_COORDINATE_M = 0.1243687729613299
K_N_PER_M = 100000.0
C_N_S_PER_M = 200.0
MU = 0.85
VREG_M_S = 0.001

GEOMETRY_ABS_M = 5e-7
FORCE_ABS_N = 0.1
FORCE_REL = 1e-5
MIN_NORMAL_N = 5.0
NO_TENSION_N = -1e-8
NEGATIVE_LOSS_W = -1e-8
STATE_COMPARE_ABS_M = 1e-7
EVENT_COMPARE_ABS_S = 2e-6

_PROBES = {
    "T01": ("x", 0.4, 203, 103),
    "T02": ("x", -0.4, 103, 203),
    "T03": ("z", 0.4, 102, 203),
    "T04": ("z", -0.4, 203, 102),
}


class EvidenceProblem(ValueError):
    """The supplied CSV cannot serve as complete numerical evidence."""


def _add(checks: list[dict[str, Any]], name: str, observed: Any,
         limit: Any, passed: bool) -> None:
    checks.append({
        "name": name,
        "observed": observed,
        "limit": limit,
        "passed": bool(passed),
    })


def _finish(checks: list[dict[str, Any]], metrics: dict[str, Any],
            diagnostics: list[str]) -> dict[str, Any]:
    passed = bool(checks) and not diagnostics and all(c["passed"] for c in checks)
    return {
        "status": "PASS" if passed else "FAIL",
        "checks": checks,
        "metrics": metrics,
        "diagnostics": diagnostics,
    }


def _read_csv(path: Path) -> tuple[list[dict[str, float]], list[float]]:
    rows: list[dict[str, float]] = []
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames
        if fields is None:
            raise EvidenceProblem("csv_header_missing")
        if len(fields) != len(set(fields)):
            raise EvidenceProblem("csv_duplicate_header")
        missing = sorted(set(COLUMNS) - set(fields))
        extra = sorted(set(fields) - set(COLUMNS))
        if missing or extra:
            raise EvidenceProblem(
                "csv_columns_mismatch:missing=" + repr(missing) + ":extra=" + repr(extra)
            )
        for row_number, raw in enumerate(reader, start=2):
            if None in raw:
                raise EvidenceProblem(f"csv_extra_cells:{row_number}")
            parsed: dict[str, float] = {}
            for name in COLUMNS:
                value = raw.get(name)
                if value is None or not value.strip():
                    raise EvidenceProblem(f"csv_empty_cell:{row_number}:{name}")
                try:
                    number = float(value)
                except ValueError as exc:
                    raise EvidenceProblem(f"csv_non_numeric:{row_number}:{name}") from exc
                if not math.isfinite(number):
                    raise EvidenceProblem(f"csv_nonfinite:{row_number}:{name}")
                parsed[name] = number
            rows.append(parsed)
    if not rows:
        raise EvidenceProblem("csv_no_data_rows")

    times = [row["time"] for row in rows]
    if abs(times[0]) > 1e-9 or abs(times[-1] - STOP_TIME_S) > 1e-9:
        raise EvidenceProblem("csv_time_bounds")
    for left, right in zip(times, times[1:]):
        delta = right - left
        if delta < -1e-12:
            raise EvidenceProblem("csv_time_decreases")
        if delta > OUTPUT_INTERVAL_S + 2e-9:
            raise EvidenceProblem("csv_output_interval_gap")
    for index in range(1001):
        nominal = index * OUTPUT_INTERVAL_S
        position = bisect_left(times, nominal)
        nearby = times[max(0, position - 1):min(len(times), position + 1)]
        if not nearby or min(abs(value - nominal) for value in nearby) > 2e-9:
            raise EvidenceProblem("csv_nominal_output_time_missing")
    return rows, times


def _pose(ident: str, time_s: float) -> dict[str, float]:
    axis, speed, _, _ = _PROBES[ident]
    displacement = speed * (time_s - BOUNDARY_TIME_S)
    if axis == "x":
        x, z, vx, vz = displacement, FIXED_NORMAL_COORDINATE_M, speed, 0.0
    else:
        x, z, vx, vz = -FIXED_NORMAL_COORDINATE_M, displacement, 0.0, speed
    return {
        "x": x,
        "z": z,
        "vx": vx,
        "vz": vz,
        "omega": speed / RADIUS_M,
    }


def _oracle(ident: str, time_s: float) -> tuple[dict[str, float], set[int]]:
    pose = _pose(ident, time_s)
    x, z = pose["x"], pose["z"]

    # The directed outline is (-1,-.15)->(0,-.15)->(0,0)->(1,0).
    # Its only convex point is vertex 3 at the origin.  Classification below
    # is finite-segment geometry, independent of the candidate's relations.
    if x > 0.0:
        feature, nx, nz, distance = 103, 0.0, 1.0, z
    elif z < 0.0:
        feature, nx, nz, distance = 102, -1.0, 0.0, -x
    else:
        feature = 203
        distance = math.hypot(x, z)
        if distance <= 0.0:
            raise ArithmeticError("probe_corner_normal_undefined")
        nx, nz = x / distance, z / distance

    delta = max(0.0, RADIUS_M - distance)
    vx, vz, omega = pose["vx"], pose["vz"], pose["omega"]
    vn = vx * nx + vz * nz
    vt = vx * nz - vz * nx - RADIUS_M * omega
    elastic_force = K_N_PER_M * delta
    damping_force = max(-elastic_force, min(elastic_force, -C_N_S_PER_M * vn))
    normal_force = elastic_force + damping_force
    tangent_force = -MU * normal_force * math.tanh(vt / VREG_M_S)

    expected = dict(pose)
    expected.update({
        "Fx": normal_force * nx + tangent_force * nz,
        "Fz": normal_force * nz - tangent_force * nx,
        "normal": normal_force,
        "penetration": delta,
        "slip": vt,
        "elasticEnergy": 0.5 * K_N_PER_M * delta * delta,
        "normalLoss": damping_force * (-vn),
        "frictionLoss": -tangent_force * vt,
        "featureId": float(feature),
        "contactCount": 1.0,
    })

    axis = _PROBES[ident][0]
    boundary_coordinate = x if axis == "x" else z
    if abs(boundary_coordinate) <= GEOMETRY_ABS_M:
        allowed = {203, 103} if axis == "x" else {102, 203}
    else:
        allowed = {feature}
    return expected, allowed


def _max_error(rows: list[dict[str, float]], expected_rows: list[dict[str, float]],
               name: str) -> float:
    return max(abs(row[name] - expected[name]) for row, expected in zip(rows, expected_rows))


def _scaled_limit(actual: float, expected: float, absolute: float) -> float:
    return max(absolute, FORCE_REL * max(abs(actual), abs(expected)))


def verify(ident: str, csv_path: str | Path) -> dict[str, Any]:
    """Verify one raw probe CSV against prescribed motion and contact physics."""
    checks: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {"probe_id": ident}
    diagnostics: list[str] = []
    try:
        if ident not in _PROBES:
            raise EvidenceProblem("unknown_probe:" + str(ident))
        path = Path(csv_path)
        rows, times = _read_csv(path)
        expected_rows: list[dict[str, float]] = []
        feature_mismatches = 0
        noninteger_discrete = 0
        force_worst = {name: 0.0 for name in ("Fx", "Fz", "normal")}
        force_ratio = {name: 0.0 for name in ("Fx", "Fz", "normal")}
        energy_worst = 0.0
        loss_worst = {"normalLoss": 0.0, "frictionLoss": 0.0}
        final_feature = _PROBES[ident][3]
        event_time: float | None = None

        for row, time_s in zip(rows, times):
            expected, allowed_features = _oracle(ident, time_s)
            expected_rows.append(expected)
            feature = int(round(row["featureId"]))
            count = int(round(row["contactCount"]))
            if (abs(row["featureId"] - feature) > 1e-9 or
                    abs(row["contactCount"] - count) > 1e-9):
                noninteger_discrete += 1
            if feature not in allowed_features:
                feature_mismatches += 1
            if event_time is None and feature == final_feature:
                event_time = time_s
            for name in force_worst:
                error = abs(row[name] - expected[name])
                limit = _scaled_limit(row[name], expected[name], FORCE_ABS_N)
                force_worst[name] = max(force_worst[name], error)
                force_ratio[name] = max(force_ratio[name], error / limit)
            energy_worst = max(
                energy_worst, abs(row["elasticEnergy"] - expected["elasticEnergy"])
            )
            for name in loss_worst:
                loss_worst[name] = max(loss_worst[name], abs(row[name] - expected[name]))

        metrics.update({
            "csv_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "row_count": len(rows),
            "duplicate_time_rows": sum(a == b for a, b in zip(times, times[1:])),
            "observed_start_s": times[0],
            "observed_stop_s": times[-1],
            "event_time_s": event_time,
            "feature_sequence": [
                int(round(row["featureId"])) for index, row in enumerate(rows)
                if index == 0 or row["featureId"] != rows[index - 1]["featureId"]
            ],
        })

        for name in ("x", "z"):
            error = _max_error(rows, expected_rows, name)
            _add(checks, name + "_prescribed_pose", error, GEOMETRY_ABS_M,
                 error <= GEOMETRY_ABS_M)
        for name in ("vx", "vz", "omega", "slip"):
            error = _max_error(rows, expected_rows, name)
            _add(checks, name + "_prescribed_state", error, GEOMETRY_ABS_M,
                 error <= GEOMETRY_ABS_M)
        penetration_error = _max_error(rows, expected_rows, "penetration")
        _add(checks, "penetration_geometry", penetration_error, GEOMETRY_ABS_M,
             penetration_error <= GEOMETRY_ABS_M)

        for name in force_worst:
            _add(checks, name + "_analytic_force",
                 {"max_error": force_worst[name], "max_tolerance_ratio": force_ratio[name]},
                 "max(0.1 N, 1e-5 relative) at every row", force_ratio[name] <= 1.0)

        # These limits are propagated conservatively from the named geometry
        # and force floors: dU <= Fn*d(delta), dP <= dF*|v|.
        energy_limit = max(2e-4, FORCE_REL * max(
            max(abs(row["elasticEnergy"]) for row in rows),
            max(abs(row["elasticEnergy"]) for row in expected_rows),
        ))
        _add(checks, "elastic_energy_oracle", energy_worst, energy_limit,
             energy_worst <= energy_limit)
        for name in loss_worst:
            loss_limit = max(0.04, FORCE_REL * max(
                max(abs(row[name]) for row in rows),
                max(abs(row[name]) for row in expected_rows),
            ))
            _add(checks, name + "_oracle", loss_worst[name], loss_limit,
                 loss_worst[name] <= loss_limit)

        min_normal = min(row["normal"] for row in rows)
        counts = sorted({int(round(row["contactCount"])) for row in rows})
        min_loss = min(min(row["normalLoss"], row["frictionLoss"]) for row in rows)
        _add(checks, "normal_remains_positive", min_normal, MIN_NORMAL_N,
             min_normal > MIN_NORMAL_N and min_normal >= NO_TENSION_N)
        _add(checks, "exactly_one_active_feature", counts, [1],
             counts == [1] and noninteger_discrete == 0)
        _add(checks, "feature_owner", feature_mismatches, 0,
             feature_mismatches == 0)
        _add(checks, "loss_nonnegative", min_loss, NEGATIVE_LOSS_W,
             min_loss >= NEGATIVE_LOSS_W)
        _add(checks, "event_observed", event_time, BOUNDARY_TIME_S,
             event_time is not None and
             abs(event_time - BOUNDARY_TIME_S) <= EVENT_COMPARE_ABS_S)
    except (EvidenceProblem, OSError) as exc:
        diagnostics.append(str(exc))
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        diagnostics.append("verification_input_or_calculation_error:" + str(exc))
    return _finish(checks, metrics, diagnostics)


def _interpolate(rows: list[dict[str, float]], times: list[float],
                 name: str, target: float) -> float:
    right = bisect_right(times, target)
    if right == 0:
        return rows[0][name]
    if right == len(times):
        return rows[-1][name]
    left = right - 1
    if times[left] == target or times[right] == times[left]:
        return rows[left][name]
    fraction = (target - times[left]) / (times[right] - times[left])
    return rows[left][name] + fraction * (rows[right][name] - rows[left][name])


def compare(ident: str, base_csv: str | Path,
            refined_csv: str | Path) -> dict[str, Any]:
    """Compare base/refined probe results after both physical checks pass."""
    checks: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {"probe_id": ident}
    diagnostics: list[str] = []
    base_report = verify(ident, base_csv)
    refined_report = verify(ident, refined_csv)
    for label, report in (("base", base_report), ("refined", refined_report)):
        _add(checks, label + "_physical", report["status"], "PASS",
             report["status"] == "PASS")
    metrics.update({"base_physical": base_report, "refined_physical": refined_report})
    if base_report["status"] != "PASS" or refined_report["status"] != "PASS":
        return _finish(checks, metrics, diagnostics)

    try:
        base_rows, base_times = _read_csv(Path(base_csv))
        refined_rows, refined_times = _read_csv(Path(refined_csv))
        timeline = sorted(set(base_times) | set(refined_times))
        for name in ("x", "z"):
            maximum = max(abs(
                _interpolate(base_rows, base_times, name, time_s) -
                _interpolate(refined_rows, refined_times, name, time_s)
            ) for time_s in timeline)
            _add(checks, name + "_base_refined_state", maximum,
                 STATE_COMPARE_ABS_M, maximum <= STATE_COMPARE_ABS_M)
        base_event = base_report["metrics"]["event_time_s"]
        refined_event = refined_report["metrics"]["event_time_s"]
        event_difference = abs(refined_event - base_event)
        _add(checks, "event_time_base_refined", event_difference,
             EVENT_COMPARE_ABS_S, event_difference <= EVENT_COMPARE_ABS_S)
    except (EvidenceProblem, OSError, KeyError, TypeError, ArithmeticError) as exc:
        diagnostics.append("comparison_error:" + str(exc))
    return _finish(checks, metrics, diagnostics)
