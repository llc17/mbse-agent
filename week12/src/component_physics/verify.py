"""Independent result checks for the frozen V11 S3-B development fixtures.

The checker consumes only raw CSV observations and constants frozen by
``B-基础物理组件与验证协议.md``.  It deliberately does not import the Modelica
generator or the production contact/drive implementation.  Loss columns are
instantaneous non-negative powers; integrations retain every CSV row, including
zero-width duplicate-time event rows.
"""
from __future__ import annotations

import bisect
import csv
from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any, Callable, Iterable


_STOPS = {"B01": 4.0, "B02": 4.0, "B03": 3.0, "B04": 1.0, "B05": 1.0, "B06": 0.6}
_ZERO = 1e-8
_CONTACT_ZERO = 1e-6
_MASS = 2.0
_INERTIA = 0.01
_RADIUS = 0.1
_STIFFNESS = 100000.0
_GRAVITY = 9.81
_VREG = 0.001

_GENERIC = {
    "x", "z", "vx", "vz", "omega", "Fx", "Fz", "normal", "penetration",
    "slip", "normalLoss", "frictionLoss", "elasticEnergy", "kineticEnergy",
    "potentialEnergy", "shaftPower", "dcPower", "driveLoss", "energyAvailable",
    "driveTorque", "supportTorque", "contactCount", "featureId",
}
_B01 = {
    "theta", "omega", "alpha", "kineticEnergy", "potentialEnergy", "pivotFx",
    "pivotFz", "jointTorque", "fixedPositionResidual", "fixedAngleResidual",
}


class _CsvProblem(ValueError):
    pass


@dataclass(frozen=True)
class _Data:
    columns: dict[str, tuple[float, ...]]
    row_count: int

    @property
    def time(self) -> tuple[float, ...]:
        return self.columns["time"]

    def col(self, name: str) -> tuple[float, ...]:
        return self.columns[name]


def _read_csv(path: Path, ident: str) -> _Data:
    required = {"time"} | (_B01 if ident == "B01" else _GENERIC)
    try:
        with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
            rows = csv.reader(handle)
            header = next(rows, None)
            if not header:
                raise _CsvProblem("csv_header_missing")
            headers = [item.strip() for item in header]
            if any(not item for item in headers):
                raise _CsvProblem("csv_empty_header")
            if len(set(headers)) != len(headers):
                raise _CsvProblem("csv_duplicate_header")
            missing = sorted(required - set(headers))
            if missing:
                raise _CsvProblem("csv_missing_columns: " + ",".join(missing))
            # All cells are still parsed and checked for finiteness. Retain only
            # observations consumed by this scene, plus the optional force peak.
            # This avoids materialising every internal MSL column three times.
            selected = [(index, name) for index, name in enumerate(headers)
                        if name in required or name == "normal"]
            retained = {name: [] for _, name in selected}
            count = 0
            for line_number, row in enumerate(rows, start=2):
                if len(row) != len(headers):
                    raise _CsvProblem(f"csv_row_width: line {line_number}")
                try:
                    parsed = list(map(float, row))
                except ValueError as exc:
                    raise _CsvProblem(f"csv_nonnumeric: line {line_number}") from exc
                if not all(map(math.isfinite, parsed)):
                    raise _CsvProblem(f"csv_nonfinite: line {line_number}")
                for index, name in selected:
                    retained[name].append(parsed[index])
                count += 1
    except (OSError, UnicodeError, csv.Error) as exc:
        raise _CsvProblem(f"csv_read_error: {exc}") from exc
    if count < 2:
        raise _CsvProblem("csv_too_few_rows")
    columns = {name: tuple(values) for name, values in retained.items()}
    times = columns["time"]
    if any(later < earlier for earlier, later in zip(times, times[1:])):
        raise _CsvProblem("csv_time_decreases")
    if max(b - a for a, b in zip(times, times[1:])) > 0.0001 + 1e-9:
        raise _CsvProblem("csv_sampling_gap_exceeds_frozen_interval")
    stop = _STOPS[ident]
    tolerance = max(1e-12, stop * 1e-9)
    if abs(times[0]) > tolerance or abs(times[-1] - stop) > tolerance:
        raise _CsvProblem("csv_time_coverage_incomplete")
    return _Data(columns, count)


def _add(checks: list[dict[str, Any]], name: str, value: Any, limit: Any, passed: bool) -> None:
    checks.append({"name": name, "value": value, "limit": limit, "pass": bool(passed)})


def _finish(checks: list[dict[str, Any]], metrics: dict[str, Any], issues: list[str]) -> dict[str, Any]:
    return {
        "status": "PASS" if not issues and all(item["pass"] for item in checks) else "FAIL",
        "checks": checks,
        "metrics": metrics,
        "issues": issues,
    }


def _trapz(times: tuple[float, ...], values: Iterable[float]) -> float:
    series = tuple(values)
    return sum((b_t - a_t) * (a_y + b_y) / 2.0
               for a_t, b_t, a_y, b_y in zip(times, times[1:], series, series[1:]))


def _cumulative(times: tuple[float, ...], values: Iterable[float]) -> tuple[float, ...]:
    series = tuple(values)
    result = [0.0]
    for a_t, b_t, a_y, b_y in zip(times, times[1:], series, series[1:]):
        result.append(result[-1] + (b_t - a_t) * (a_y + b_y) / 2.0)
    return tuple(result)


def _interp(data: _Data, column: str, when: float) -> float:
    times, values = data.time, data.col(column)
    if when < times[0] or when > times[-1]:
        raise ValueError("interpolation outside observation interval")
    right = bisect.bisect_right(times, when)
    if right and times[right - 1] == when:
        return values[right - 1]
    if right == 0 or right == len(times):
        return values[0] if right == 0 else values[-1]
    left = right - 1
    width = times[right] - times[left]
    if width <= 0:
        return values[right]
    weight = (when - times[left]) / width
    return values[left] + weight * (values[right] - values[left])


def _window(data: _Data, column: str, start: float, stop: float) -> tuple[float, ...]:
    values = [value for time, value in zip(data.time, data.col(column)) if start <= time <= stop]
    values.extend((_interp(data, column, start), _interp(data, column, stop)))
    return tuple(values)


def _crossings(times: tuple[float, ...], values: Iterable[float], level: float,
               direction: int = 0) -> list[float]:
    series = tuple(value - level for value in values)
    found: list[float] = []
    for t0, t1, y0, y1 in zip(times, times[1:], series, series[1:]):
        rising = y0 <= 0 < y1
        falling = y0 >= 0 > y1
        if not ((direction >= 0 and rising) or (direction <= 0 and falling)):
            continue
        if t1 == t0 or y1 == y0:
            event = t1
        else:
            event = t0 - y0 * (t1 - t0) / (y1 - y0)
        if not found or abs(event - found[-1]) > 1e-12:
            found.append(event)
    return found


def _contact_event_times(data: _Data) -> dict[str, float]:
    upward = _crossings(data.time, data.col("normal"), _CONTACT_ZERO, 1)
    downward = _crossings(data.time, data.col("normal"), _CONTACT_ZERO, -1)
    events: dict[str, float] = {}
    if upward:
        events["contact"] = upward[0]
        after = [value for value in downward if value > upward[0]]
        if after:
            events["separation"] = after[0]
    return events


def _energy_balance(data: _Data, *, external: Iterable[str] = (),
                    losses: Iterable[str] = ("normalLoss", "frictionLoss"),
                    gravity: float = _GRAVITY) -> tuple[float, float, dict[str, float]]:
    # These six fixtures have at most one active contact. Reconstruct storage
    # from observed states, independently of the model's energy outputs.
    kinetic = tuple(0.5 * _MASS * (vx*vx + vz*vz) + 0.5 * _INERTIA * w*w
                    for vx, vz, w in zip(data.col("vx"), data.col("vz"), data.col("omega")))
    elastic = tuple(0.5 * _STIFFNESS * d*d for d in data.col("penetration"))
    total = tuple(k + _MASS * gravity * z + e
                  for k, z, e in zip(kinetic, data.col("z"), elastic))
    work = [0.0] * data.row_count
    for name in external:
        work = [left + right for left, right in zip(work, data.col(name))]
    dissipated = [0.0] * data.row_count
    for name in losses:
        dissipated = [left + right for left, right in zip(dissipated, data.col(name))]
    cumulative_work = _cumulative(data.time, work)
    cumulative_loss = _cumulative(data.time, dissipated)
    residual = tuple(energy - total[0] - supplied + lost
                     for energy, supplied, lost in zip(total, cumulative_work, cumulative_loss))
    height_scale = _MASS * gravity * (max(data.col("z")) - min(data.col("z")))
    # A world-height potential offset is not an error scale.  Initial kinetic
    # and elastic energy, actual height range, and accumulated external work are.
    initial_releasable = kinetic[0] + elastic[0]
    scale = max(0.01, initial_releasable, _trapz(data.time, map(abs, work)), height_scale)
    terms = {
        "mechanical_energy_change": total[-1] - total[0],
        "external_work": cumulative_work[-1],
        "normal_loss": _trapz(data.time, data.col("normalLoss")) if "normalLoss" in data.columns else 0.0,
        "friction_loss": _trapz(data.time, data.col("frictionLoss")) if "frictionLoss" in data.columns else 0.0,
        "energy_residual_max": max(map(abs, residual)),
    }
    return max(map(abs, residual)), scale, terms


def _generic_sanity(data: _Data, checks: list[dict[str, Any]]) -> None:
    _add(checks, "normal_force_has_no_tension", min(data.col("normal")), ">=-1e-8 N",
         min(data.col("normal")) >= -1e-8)
    _add(checks, "penetration_nonnegative", min(data.col("penetration")), ">=-1e-8 m",
         min(data.col("penetration")) >= -1e-8)
    for name in ("normalLoss", "frictionLoss", "driveLoss"):
        _add(checks, name + "_nonnegative", min(data.col(name)), ">=-1e-8 W",
             min(data.col(name)) >= -1e-8)
    count_error = max(abs(value - round(value)) for value in data.col("contactCount"))
    _add(checks, "contact_count_integral_nonnegative", count_error,
         "integer >=0", count_error <= _ZERO and min(data.col("contactCount")) >= 0)


def _initial_state(data: _Data, checks: list[dict[str, Any]], expected: dict[str, float]) -> None:
    error = max(abs(data.col(name)[0] - value) for name, value in expected.items())
    _add(checks, "frozen_initial_state", error, _ZERO, error <= _ZERO)


def _verify_b01(data: _Data, checks: list[dict[str, Any]], metrics: dict[str, Any], issues: list[str]) -> None:
    theta, omega, alpha = data.col("theta"), data.col("omega"), data.col("alpha")
    _initial_state(data, checks, {"theta": 0.02, "omega": 0.0})
    rod_mass, load_mass, length, gravity = 2.0, 1.0, 0.5, 9.81
    inertia = rod_mass * length**2 / 12 + rod_mass * (length / 2)**2 + 0.0025 + load_mass * length**2
    gravity_arm = (rod_mass * length / 2 + load_mass * length) * gravity
    expected_period = 2 * math.pi * math.sqrt(inertia / gravity_arm) * (1 + 0.02**2 / 16)
    descending = _crossings(data.time, theta, 0.0, -1)
    periods = [later - earlier for earlier, later in zip(descending, descending[1:])]
    if not periods:
        issues.append("pendulum_period_unobservable")
    else:
        observed_period = sum(periods) / len(periods)
        _add(checks, "pendulum_period", abs(observed_period - expected_period),
             0.002 * expected_period, abs(observed_period - expected_period) <= 0.002 * expected_period)
        metrics["period"] = observed_period
    expected_alpha = tuple(-gravity_arm * math.sin(value) / inertia for value in theta)
    metrics["pendulum_equation_residual_max"] = max(abs(a - b) for a, b in zip(alpha, expected_alpha))
    energy = tuple(0.5 * inertia * w*w + gravity_arm * (1 - math.cos(q))
                   for q, w in zip(theta, omega))
    initial_vibration = gravity_arm * (1 - math.cos(0.02))
    drift = max(energy) - min(energy)
    _add(checks, "pendulum_energy_drift", drift, 0.001 * initial_vibration,
         drift <= 0.001 * initial_vibration)
    position_residual = max(map(abs, data.col("fixedPositionResidual")))
    angle_residual = max(map(abs, data.col("fixedAngleResidual")))
    joint_torque = max(map(abs, data.col("jointTorque")))
    _add(checks, "fixed_position_residual", position_residual, 1e-7, position_residual <= 1e-7)
    _add(checks, "fixed_angle_residual", angle_residual, 1e-7, angle_residual <= 1e-7)
    _add(checks, "free_joint_axis_torque", joint_torque, 1e-6, joint_torque <= 1e-6)
    reference: list[tuple[float, float]] = []
    for angle, speed, angular_accel in zip(theta, omega, expected_alpha):
        ax_rod = length / 2 * (math.cos(angle) * angular_accel - math.sin(angle) * speed**2)
        az_rod = length / 2 * (math.sin(angle) * angular_accel + math.cos(angle) * speed**2)
        ax_load = length * (math.cos(angle) * angular_accel - math.sin(angle) * speed**2)
        az_load = length * (math.sin(angle) * angular_accel + math.cos(angle) * speed**2)
        reference.append((rod_mass * ax_rod + load_mass * ax_load,
                          rod_mass * az_rod + load_mass * az_load + (rod_mass + load_mass) * gravity))
    reaction_error = max(math.hypot(fx - refx, fz - refz)
                         for fx, fz, (refx, refz) in zip(data.col("pivotFx"), data.col("pivotFz"), reference))
    reference_peak = max(math.hypot(fx, fz) for fx, fz in reference)
    reaction_limit = max(0.02, reference_peak * 0.001)
    _add(checks, "independent_pivot_reaction", reaction_error, reaction_limit,
         reaction_error <= reaction_limit)
    metrics.update({
        "energy_scale": initial_vibration,
        "energy_drift": drift,
        "signals": _signal_metrics(data, ("theta", "omega")),
        "cumulative": {"energy_drift": drift},
        "event_times": {},
    })


def _verify_b02(data: _Data, checks: list[dict[str, Any]], metrics: dict[str, Any], issues: list[str]) -> None:
    _initial_state(data, checks, {"x": 0.0, "z": 0.12, "vx": 0.0, "vz": 0.0, "omega": 0.0})
    penetration_from_geometry = tuple(_RADIUS - z for z in data.col("z"))
    contacts = _crossings(data.time, penetration_from_geometry, 0.0, 1)
    if not contacts:
        issues.append("first_contact_unobservable")
    else:
        observed = contacts[0]
        expected = math.sqrt(2 * 0.02 / _GRAVITY)
        _add(checks, "first_contact_time", abs(observed - expected), 0.0002,
             abs(observed - expected) <= 0.0002)
        metrics["event_times"] = {"contact": observed}
    normal_error = max(abs(value - 19.62) for value in _window(data, "normal", 3.0, 4.0))
    expected_penetration = 19.62 / _STIFFNESS
    penetration_error = max(abs(value - expected_penetration)
                            for value in _window(data, "penetration", 3.0, 4.0))
    settled_speed = max(map(abs, _window(data, "vz", 3.0, 4.0)))
    _add(checks, "settled_normal_load", normal_error, 19.62 * 0.005, normal_error <= 19.62 * 0.005)
    _add(checks, "settled_penetration", penetration_error, expected_penetration * 0.005,
         penetration_error <= expected_penetration * 0.005)
    _add(checks, "settled_vertical_speed", settled_speed, 1e-4, settled_speed <= 1e-4)
    maximum_penetration = max(data.col("penetration"))
    _add(checks, "maximum_penetration", maximum_penetration, 0.005, maximum_penetration <= 0.005)
    residual, scale, terms = _energy_balance(data)
    _add(checks, "energy_closure", residual, 0.005 * scale, residual <= 0.005 * scale)
    metrics.update({"energy_scale": scale, "energy_terms": terms, "signals": _signal_metrics(data),
                    "cumulative": _cumulative_metrics(data, terms)})


def _verify_b03(data: _Data, checks: list[dict[str, Any]], metrics: dict[str, Any], issues: list[str]) -> None:
    _initial_state(data, checks, {"x": 0.0, "z": 0.1 - 19.62 / _STIFFNESS,
                                  "vx": 0.0, "vz": 0.0, "omega": 0.0})
    expected_acceleration = 0.2 * _RADIUS / (_INERTIA + _MASS * _RADIUS**2)
    expected_force = _MASS * expected_acceleration
    for label, start, stop, sign in (("drive", 0.3, 1.3, 1.0), ("brake", 1.7, 2.3, -1.0)):
        observed_acceleration = (_interp(data, "vx", stop) - _interp(data, "vx", start)) / (stop - start)
        target_acceleration = sign * expected_acceleration
        acceleration_error = abs(observed_acceleration - target_acceleration)
        _add(checks, label + "_acceleration", acceleration_error, abs(target_acceleration) * 0.02,
             acceleration_error <= abs(target_acceleration) * 0.02)
        force = sign * expected_force
        expected_slip = -_VREG * math.atanh(force / (0.6 * _MASS * _GRAVITY))
        slip_error = max(abs(value - expected_slip) for value in _window(data, "slip", start, stop))
        _add(checks, label + "_regularized_slip", slip_error, 2e-5, slip_error <= 2e-5)
    # supportTorque is the ground constraint acting on the carrier, positive
    # about -y. It opposes the bearing/drive reaction on that carrier.
    support_error = max(abs(support + torque)
                        for support, torque in zip(data.col("supportTorque"), data.col("driveTorque")))
    _add(checks, "support_reaction_torque", support_error, 1e-6, support_error <= 1e-6)
    slip_identity_error = max(abs(slip - (vx - _RADIUS * omega))
                              for slip, vx, omega in zip(data.col("slip"), data.col("vx"), data.col("omega")))
    _add(checks, "slip_kinematic_identity", slip_identity_error, _ZERO,
         slip_identity_error <= _ZERO)
    shaft_identity_error = max(abs(power - torque * omega)
                               for power, torque, omega in zip(data.col("shaftPower"),
                                                               data.col("driveTorque"), data.col("omega")))
    shaft_identity_limit = max(_ZERO, max(map(abs, data.col("shaftPower"))) * 1e-8)
    _add(checks, "shaft_power_identity", shaft_identity_error, shaft_identity_limit,
         shaft_identity_error <= shaft_identity_limit)
    dc_identity_error = max(abs(dc - max(shaft, 0.0) / 0.8)
                            for dc, shaft in zip(data.col("dcPower"), data.col("shaftPower")))
    dc_identity_limit = max(_ZERO, max(map(abs, data.col("dcPower"))) * 1e-8)
    _add(checks, "dc_power_identity", dc_identity_error, dc_identity_limit,
         dc_identity_error <= dc_identity_limit)
    loss_identity_error = max(abs(loss - (dc - shaft))
                              for loss, dc, shaft in zip(data.col("driveLoss"), data.col("dcPower"),
                                                        data.col("shaftPower")))
    loss_identity_limit = max(_ZERO, max(map(abs, data.col("driveLoss"))) * 1e-8)
    _add(checks, "drive_loss_identity", loss_identity_error, loss_identity_limit,
         loss_identity_error <= loss_identity_limit)
    expected_torque = []
    for time in data.time:
        if time < 0.1:
            expected_torque.append(2.0 * time)
        elif time < 1.4:
            expected_torque.append(0.2)
        elif time < 1.5:
            expected_torque.append(0.2 - 4.0 * (time - 1.4))
        elif time < 2.4:
            expected_torque.append(-0.2)
        elif time < 2.5:
            expected_torque.append(-0.2 + 2.0 * (time - 2.4))
        else:
            expected_torque.append(0.0)
    torque_schedule_error = max(abs(observed - expected)
                                for observed, expected in zip(data.col("driveTorque"), expected_torque))
    _add(checks, "frozen_torque_schedule", torque_schedule_error, _ZERO,
         torque_schedule_error <= _ZERO)
    torque_peak = max(map(abs, data.col("driveTorque")))
    shaft_peak = max(map(abs, data.col("shaftPower")))
    dc_peak = max(data.col("dcPower"))
    _add(checks, "paired_torque_rating", torque_peak, 0.2, torque_peak <= 0.2 + _ZERO)
    _add(checks, "paired_mechanical_power_rating", shaft_peak, 4.0, shaft_peak <= 4.0 + _ZERO)
    _add(checks, "supply_power_limit", dc_peak, 10.0, dc_peak <= 10.0 + _ZERO)
    _add(checks, "dc_power_nonnegative", min(data.col("dcPower")), ">=-1e-8 W",
         min(data.col("dcPower")) >= -_ZERO)
    energy_used = data.col("energyAvailable")[0] - min(data.col("energyAvailable"))
    _add(checks, "usable_energy_limit", energy_used, 100.0, energy_used <= 100.0 + _ZERO)
    _add(checks, "energy_available_nonnegative", min(data.col("energyAvailable")), ">=-1e-8 J",
         min(data.col("energyAvailable")) >= -_ZERO)
    electrical = _cumulative(data.time, data.col("dcPower"))
    shaft = _cumulative(data.time, data.col("shaftPower"))
    drive_loss = _cumulative(data.time, data.col("driveLoss"))
    balance_error = max(abs(electric - mechanical - loss)
                        for electric, mechanical, loss in zip(electrical, shaft, drive_loss))
    energy_scale = max(0.01, _trapz(data.time, map(abs, data.col("dcPower"))),
                       _trapz(data.time, map(abs, data.col("shaftPower"))))
    _add(checks, "electrical_shaft_loss_balance", balance_error, 0.005 * energy_scale,
         balance_error <= 0.005 * energy_scale)
    battery_error = max(abs(100.0 - available - used)
                        for available, used in zip(data.col("energyAvailable"), electrical))
    _add(checks, "battery_energy_balance", battery_error, 0.005 * energy_scale,
         battery_error <= 0.005 * energy_scale)
    mechanical_error, mechanical_scale, mechanical_terms = _energy_balance(data, external=("shaftPower",))
    _add(checks, "mechanical_energy_closure", mechanical_error, 0.005 * mechanical_scale,
         mechanical_error <= 0.005 * mechanical_scale)
    braking_dc = max(map(abs, _window(data, "dcPower", 1.5, 2.4)))
    braking_loss = min(_window(data, "driveLoss", 1.5, 2.4))
    _add(checks, "braking_dc_power_zero", braking_dc, _ZERO, braking_dc <= _ZERO)
    _add(checks, "braking_drive_loss_nonnegative", braking_loss, ">=-1e-8 W", braking_loss >= -_ZERO)
    coast_start, coast_end = _interp(data, "vx", 2.5), _interp(data, "vx", 3.0)
    coast_change = abs(coast_end - coast_start)
    coast_limit = max(_ZERO, abs(coast_start) * 0.005)
    _add(checks, "coast_speed_change", coast_change, coast_limit, coast_change <= coast_limit)
    metrics.update({
        "energy_scale": energy_scale,
        "signals": _signal_metrics(data),
        "event_times": _contact_event_times(data),
        "energy_terms": {**mechanical_terms, "dc_energy": electrical[-1], "shaft_work": shaft[-1],
                         "drive_loss": drive_loss[-1], "energy_residual_max": balance_error},
        "cumulative": {**_cumulative_metrics(data, mechanical_terms),
                       "dc_energy": electrical[-1], "shaft_work": shaft[-1],
                       "drive_loss": drive_loss[-1]},
    })


def _impact_energy(data: _Data, checks: list[dict[str, Any]], fraction: float,
                   *, gravity: float = _GRAVITY) -> tuple[float, dict[str, float]]:
    residual, scale, terms = _energy_balance(data, gravity=gravity)
    _add(checks, "energy_closure", residual, fraction * scale, residual <= fraction * scale)
    return scale, terms


def _separation_checks(data: _Data, checks: list[dict[str, Any]], issues: list[str]) -> dict[str, float]:
    events = _contact_event_times(data)
    if "contact" not in events:
        issues.append("contact_event_unobservable")
    if "separation" not in events:
        issues.append("separation_event_unobservable")
    _add(checks, "post_impact_rebound", data.col("vx")[-1], "<0 m/s", data.col("vx")[-1] < 0)
    final_force = max(abs(data.col("normal")[-1]), abs(data.col("Fx")[-1]), abs(data.col("Fz")[-1]))
    _add(checks, "force_returns_to_zero", final_force, 1e-6, final_force <= 1e-6)
    return events


def _verify_b04(data: _Data, checks: list[dict[str, Any]], metrics: dict[str, Any], issues: list[str]) -> None:
    _initial_state(data, checks, {"x": -0.15, "z": 0.15, "vx": 0.2, "vz": 0.0, "omega": 0.0})
    geometric = _crossings(data.time, (x + _RADIUS for x in data.col("x")), 0.0, 1)
    if not geometric:
        issues.append("first_contact_unobservable")
    else:
        error = abs(geometric[0] - 0.25)
        _add(checks, "first_contact_time", error, 0.0002, error <= 0.0002)
    fz_peak = max(map(abs, data.col("Fz")))
    z_drift = max(data.col("z")) - min(data.col("z"))
    direction_error = max(abs(fx + normal) for fx, normal in zip(data.col("Fx"), data.col("normal")))
    _add(checks, "vertical_force_zero", fz_peak, 1e-7, fz_peak <= 1e-7)
    _add(checks, "vertical_position_drift", z_drift, 1e-7, z_drift <= 1e-7)
    _add(checks, "riser_force_direction", direction_error, 1e-6, direction_error <= 1e-6)
    impulse = _trapz(data.time, data.col("Fx"))
    momentum_change = _MASS * (data.col("vx")[-1] - data.col("vx")[0])
    impulse_error = abs(momentum_change - impulse)
    impulse_scale = max(1e-6, _MASS * abs(data.col("vx")[0]))
    _add(checks, "horizontal_impulse", impulse_error, 0.005 * impulse_scale,
         impulse_error <= 0.005 * impulse_scale)
    penetration_bound = 1.01 * math.sqrt(_MASS * 0.2**2 / _STIFFNESS)
    _add(checks, "lossless_penetration_bound", max(data.col("penetration")), penetration_bound,
         max(data.col("penetration")) <= penetration_bound)
    events = _separation_checks(data, checks, issues)
    if geometric:
        events["contact_geometry"] = geometric[0]
    scale, terms = _impact_energy(data, checks, 0.005, gravity=0.0)
    metrics.update({"event_times": events, "energy_scale": scale, "energy_terms": terms,
                    "signals": _signal_metrics(data),
                    "cumulative": _cumulative_metrics(data, terms, impulse_x=impulse)})


def _verify_b05(data: _Data, checks: list[dict[str, Any]], metrics: dict[str, Any], issues: list[str]) -> None:
    root2 = math.sqrt(2.0)
    _initial_state(data, checks, {"x": -0.15 / root2, "z": 0.3 + 0.15 / root2,
                                  "vx": 0.2 / root2, "vz": -0.2 / root2, "omega": 0.0})
    distance = tuple(math.hypot(x, z - 0.3) for x, z in zip(data.col("x"), data.col("z")))
    geometric = _crossings(data.time, (value - _RADIUS for value in distance), 0.0, -1)
    if not geometric:
        issues.append("first_contact_unobservable")
    else:
        error = abs(geometric[0] - 0.25)
        _add(checks, "first_contact_time", error, 0.0002, error <= 0.0002)
    geometry_error = max(abs(penetration - max(0.0, _RADIUS - radial))
                         for penetration, radial in zip(data.col("penetration"), distance))
    _add(checks, "independent_corner_distance", geometry_error, 1e-7, geometry_error <= 1e-7)
    nx, nz = -1 / root2, 1 / root2
    contact_indices = [index for index, value in enumerate(data.col("normal")) if value > _CONTACT_ZERO]
    if not contact_indices:
        issues.append("corner_contact_unobservable")
        normal_error: float | None = None
    else:
        normal_error = max(
            math.hypot(data.col("Fx")[index] / data.col("normal")[index] - nx,
                       data.col("Fz")[index] / data.col("normal")[index] - nz)
            for index in contact_indices)
    _add(checks, "corner_radial_normal", normal_error, 1e-6,
         normal_error is not None and normal_error <= 1e-6)
    contact_count_error = (max(abs(data.col("contactCount")[index] - 1.0)
                               for index in contact_indices) if contact_indices else None)
    feature_ids = {round(data.col("featureId")[index]) for index in contact_indices}
    _add(checks, "single_corner_feature", {"count_error": contact_count_error, "feature_ids": sorted(feature_ids)},
         "contactCount=1 and one featureId",
         contact_count_error is not None and contact_count_error <= _ZERO and len(feature_ids) == 1)
    tangent = tuple((vx + vz) / root2 for vx, vz in zip(data.col("vx"), data.col("vz")))
    tangent_change = max(abs(value - tangent[0]) for value in tangent)
    omega_peak = max(map(abs, data.col("omega")))
    _add(checks, "tangential_velocity_change", tangent_change, 1e-6, tangent_change <= 1e-6)
    _add(checks, "wheel_speed_zero", omega_peak, 1e-6, omega_peak <= 1e-6)
    impulse_x, impulse_z = _trapz(data.time, data.col("Fx")), _trapz(data.time, data.col("Fz"))
    for axis, velocity, impulse in (("x", data.col("vx"), impulse_x), ("z", data.col("vz"), impulse_z)):
        change = _MASS * (velocity[-1] - velocity[0])
        scale = max(1e-6, _MASS * abs(velocity[0]))
        error = abs(change - impulse)
        _add(checks, axis + "_impulse", error, 0.005 * scale, error <= 0.005 * scale)
    penetration_bound = 1.01 * math.sqrt(_MASS * 0.2**2 / _STIFFNESS)
    _add(checks, "lossless_penetration_bound", max(data.col("penetration")), penetration_bound,
         max(data.col("penetration")) <= penetration_bound)
    events = _separation_checks(data, checks, issues)
    if geometric:
        events["contact_geometry"] = geometric[0]
    energy_scale, terms = _impact_energy(data, checks, 0.005, gravity=0.0)
    metrics.update({"event_times": events, "energy_scale": energy_scale, "energy_terms": terms,
                    "signals": _signal_metrics(data),
                    "cumulative": _cumulative_metrics(data, terms, impulse_x=impulse_x, impulse_z=impulse_z)})


def _flight_segment(data: _Data) -> tuple[int, int, int, int] | None:
    normal = data.col("normal")
    supported = [value > _CONTACT_ZERO for value in normal]
    candidates: list[tuple[float, int, int, int, int]] = []
    index = 0
    while index < len(supported):
        if supported[index]:
            index += 1
            continue
        start = index
        while index + 1 < len(supported) and not supported[index + 1]:
            index += 1
        end = index
        # Corner support may extend beyond x=0 before lift-off. Locate the
        # earlier high face support, not necessarily the last contact row.
        before = next((item for item in range(start - 1, -1, -1)
                       if supported[item] and data.col("x")[item] < 0
                       and abs(data.col("z")[item] + data.col("penetration")[item] - 0.18) <= 1e-7), None)
        after = next((item for item in range(end + 1, len(supported)) if supported[item]), None)
        if (before is not None and after is not None and data.col("x")[after] > 0
                and abs(data.col("z")[after] + data.col("penetration")[after] - 0.1) <= 1e-7):
            candidates.append((data.time[end] - data.time[start], start, end, before, after))
        index += 1
    if not candidates:
        return None
    _, start, end, before, after = max(candidates)
    return start, end, before, after


def _verify_b06(data: _Data, checks: list[dict[str, Any]], metrics: dict[str, Any], issues: list[str]) -> None:
    _initial_state(data, checks, {"x": -0.2, "z": 0.18 - 19.62 / _STIFFNESS,
                                  "vx": 2.0, "vz": 0.0, "omega": 20.0})
    segment = _flight_segment(data)
    events: dict[str, float] = {}
    if segment is None:
        issues.append("high_support_flight_low_support_sequence_unobservable")
    else:
        start, end, before, after = segment
        duration = data.time[end] - data.time[start]
        _add(checks, "continuous_free_flight", duration, ">=0.03 s", duration >= 0.03)
        sequence_ok = (data.col("normal")[before] > _CONTACT_ZERO and data.col("x")[before] <= 0
                       and data.col("normal")[after] > _CONTACT_ZERO and data.col("x")[after] >= 0)
        _add(checks, "high_platform_flight_low_platform_order",
             {"high_x": data.col("x")[before], "low_x": data.col("x")[after]},
             "supported x<=0, flight, supported x>=0", sequence_ok)
        t0 = data.time[start]
        x0, z0 = data.col("x")[start], data.col("z")[start]
        vx0, vz0, omega0 = data.col("vx")[start], data.col("vz")[start], data.col("omega")[start]
        x_error = z_error = vx_error = vz_error = omega_error = 0.0
        for index in range(start, end + 1):
            elapsed = data.time[index] - t0
            x_error = max(x_error, abs(data.col("x")[index] - (x0 + vx0 * elapsed)))
            z_error = max(z_error, abs(data.col("z")[index] - (z0 + vz0 * elapsed - _GRAVITY * elapsed**2 / 2)))
            vx_error = max(vx_error, abs(data.col("vx")[index] - vx0))
            vz_error = max(vz_error, abs(data.col("vz")[index] - (vz0 - _GRAVITY * elapsed)))
            omega_error = max(omega_error, abs(data.col("omega")[index] - omega0))
        _add(checks, "flight_horizontal_position", x_error, 2e-4, x_error <= 2e-4)
        _add(checks, "flight_vertical_position", z_error, 2e-4, z_error <= 2e-4)
        _add(checks, "flight_horizontal_speed", vx_error, 2e-3, vx_error <= 2e-3)
        _add(checks, "flight_vertical_speed", vz_error, 2e-3, vz_error <= 2e-3)
        _add(checks, "flight_wheel_speed", omega_error, 1e-4, omega_error <= 1e-4)
        falling = _crossings(data.time, data.col("normal"), _CONTACT_ZERO, -1)
        rising = _crossings(data.time, data.col("normal"), _CONTACT_ZERO, 1)
        liftoff = max((value for value in falling if value <= data.time[start] + 1e-12),
                      default=data.time[start])
        landing = min((value for value in rising if value >= data.time[end] - 1e-12),
                      default=data.time[after])
        events = {"liftoff": liftoff, "landing": landing}
    maximum_penetration = max(data.col("penetration"))
    _add(checks, "maximum_landing_penetration", maximum_penetration, 0.02,
         maximum_penetration <= 0.02)
    energy_scale, terms = _impact_energy(data, checks, 0.01)
    metrics.update({"event_times": events, "energy_scale": energy_scale, "energy_terms": terms,
                    "signals": _signal_metrics(data), "cumulative": _cumulative_metrics(data, terms)})


def _signal_metrics(data: _Data, names: Iterable[str] = ("x", "z", "vx", "vz", "omega")) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for name in names:
        if name in data.columns:
            values = data.col(name)
            result[name] = {"peak_abs": max(map(abs, values)), "terminal": values[-1]}
    if "normal" in data.columns:
        result["normal"] = {"peak_abs": max(map(abs, data.col("normal"))),
                            "terminal": data.col("normal")[-1]}
    return result


def _cumulative_metrics(data: _Data, energy_terms: dict[str, float], *,
                        impulse_x: float | None = None, impulse_z: float | None = None) -> dict[str, float]:
    result = {name: value for name, value in energy_terms.items()
              if name in {"normal_loss", "friction_loss", "dc_energy", "shaft_work", "drive_loss"}}
    if impulse_x is not None:
        result["impulse_x"] = impulse_x
    if impulse_z is not None:
        result["impulse_z"] = impulse_z
    return result


_VERIFIERS: dict[str, Callable[[_Data, list[dict[str, Any]], dict[str, Any], list[str]], None]] = {
    "B01": _verify_b01,
    "B02": _verify_b02,
    "B03": _verify_b03,
    "B04": _verify_b04,
    "B05": _verify_b05,
    "B06": _verify_b06,
}


def _verify(ident: str, csv_path: Path) -> tuple[dict[str, Any], _Data | None]:
    checks: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {}
    issues: list[str] = []
    if ident not in _VERIFIERS:
        return _finish(checks, metrics, ["unsupported_ident"]), None
    try:
        data = _read_csv(Path(csv_path), ident)
    except (_CsvProblem, TypeError, ValueError) as exc:
        return _finish(checks, metrics, [str(exc)]), None
    metrics.update({"row_count": data.row_count, "observed_start": data.time[0],
                    "observed_stop": data.time[-1], "duplicate_time_rows":
                    sum(a == b for a, b in zip(data.time, data.time[1:]))})
    if ident != "B01":
        _generic_sanity(data, checks)
    try:
        _VERIFIERS[ident](data, checks, metrics, issues)
    except (ArithmeticError, ValueError) as exc:
        issues.append("verification_error: " + str(exc))
    return _finish(checks, metrics, issues), data


def verify_case(ident: str, csv_path: Path) -> dict[str, Any]:
    """Verify one frozen B01--B06 CSV; missing evidence always returns ``FAIL``."""

    report, _ = _verify(ident, Path(csv_path))
    return report


def _relative_check(checks: list[dict[str, Any]], name: str, base: float, refined: float,
                    relative: float, absolute_floor: float) -> None:
    difference = abs(refined - base)
    limit = max(absolute_floor, relative * max(abs(base), abs(refined)))
    _add(checks, name, difference, limit, difference <= limit)


def compare_cases(ident: str, base_csv: Path, refined_csv: Path) -> dict[str, Any]:
    """Compare protocol base/refined runs after requiring both to pass physically."""

    base, _ = _verify(ident, Path(base_csv))
    refined, _ = _verify(ident, Path(refined_csv))
    return _compare_reports(ident, base, refined)


def verification_outputs(ident: str, csv_path: Path, base_csv: Path | None = None):
    """Yield fresh physical then numerical reports, reading each CSV only once.

    Reports are computed from raw inputs in this call, never trusted from an
    externally supplied PASS JSON. Yielding physical first preserves partial
    evidence if a later stage fails or times out.
    """
    physical = verify_case(ident, csv_path)
    yield "physical", physical
    if physical["status"] != "PASS" or base_csv is None:
        return
    base = verify_case(ident, base_csv)
    yield "base_physical", base
    yield "numerical", _compare_reports(ident, base, physical)


def _compare_reports(ident: str, base: dict[str, Any], refined: dict[str, Any]) -> dict[str, Any]:
    """Unchanged comparison logic, shared only by fresh-report entry points."""
    checks: list[dict[str, Any]] = []
    issues: list[str] = []
    metrics: dict[str, Any] = {"base_status": base["status"], "refined_status": refined["status"]}
    _add(checks, "base_physics_status", base["status"], "PASS", base["status"] == "PASS")
    _add(checks, "refined_physics_status", refined["status"], "PASS", refined["status"] == "PASS")
    if ident not in _VERIFIERS:
        issues.append("unsupported_ident")
        return _finish(checks, metrics, issues)
    if base["status"] != "PASS" or refined["status"] != "PASS":
        issues.append("individual_case_verification_failed")
        metrics["base_issues"] = base["issues"]
        metrics["refined_issues"] = refined["issues"]
        return _finish(checks, metrics, issues)
    base_signals, refined_signals = base["metrics"]["signals"], refined["metrics"]["signals"]
    for name in sorted(set(base_signals) & set(refined_signals)):
        if name == "normal":
            _relative_check(checks, "normal_peak", base_signals[name]["peak_abs"],
                            refined_signals[name]["peak_abs"], 0.05, _ZERO)
            continue
        floor = 1e-5 if name in {"x", "z", "theta"} else 1e-3 if name == "omega" else 1e-4
        _relative_check(checks, name + "_peak", base_signals[name]["peak_abs"],
                        refined_signals[name]["peak_abs"], 0.01, floor)
        _relative_check(checks, name + "_terminal", base_signals[name]["terminal"],
                        refined_signals[name]["terminal"], 0.01, floor)
    base_events, refined_events = base["metrics"].get("event_times", {}), refined["metrics"].get("event_times", {})
    for name in sorted(set(base_events) | set(refined_events)):
        if name not in base_events or name not in refined_events:
            _add(checks, name + "_event_time", "missing", 0.0002, False)
        else:
            difference = abs(base_events[name] - refined_events[name])
            _add(checks, name + "_event_time", difference, 0.0002, difference <= 0.0002)
    if ident == "B01":
        _relative_check(checks, "pendulum_period", base["metrics"]["period"],
                        refined["metrics"]["period"], 0.001, 0.0)
    base_cumulative = base["metrics"].get("cumulative", {})
    refined_cumulative = refined["metrics"].get("cumulative", {})
    energy_scale = max(base["metrics"].get("energy_scale", 0.0),
                       refined["metrics"].get("energy_scale", 0.0))
    for name in sorted(set(base_cumulative) | set(refined_cumulative)):
        if name not in base_cumulative or name not in refined_cumulative:
            _add(checks, name + "_cumulative", "missing", "1% scale", False)
            continue
        if name.startswith("impulse"):
            if ident == "B04":
                scale = _MASS * 0.2 if name == "impulse_x" else 1e-6
            elif ident == "B05":
                scale = _MASS * 0.2 / math.sqrt(2.0)
            else:
                scale = 1e-6
        else:
            scale = energy_scale if ident == "B01" else max(0.01, energy_scale)
        difference = abs(base_cumulative[name] - refined_cumulative[name])
        _add(checks, name + "_cumulative", difference, 0.01 * scale, difference <= 0.01 * scale)
    metrics["base_metrics"] = base["metrics"]
    metrics["refined_metrics"] = refined["metrics"]
    return _finish(checks, metrics, issues)
