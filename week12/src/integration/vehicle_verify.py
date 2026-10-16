"""Independent V11 S3-C vehicle/probe verification from raw CSV observations.

The module intentionally does not import the executable generator.  It rebuilds
mass, energy, support, terrain-envelope and event evidence from the frozen
proposal and the CSV.  Every CSV cell is parsed and checked for finiteness, but
only protocol observations are retained in memory.  Duplicate-time event rows
are preserved and contribute zero width to trapezoidal integration.
"""
from __future__ import annotations

import bisect
import csv
import hashlib
from dataclasses import dataclass
import json
import math
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable, Sequence


_G = 9.81
_EPS = 1e-9
_PROBES = {"shared_supply_probe", "rolling_resistance_probe"}


class _EvidenceProblem(ValueError):
    """The supplied observations cannot prove a required claim."""


@dataclass(frozen=True)
class _Data:
    columns: dict[str, tuple[float, ...]]
    row_count: int

    @property
    def time(self) -> tuple[float, ...]:
        return self.columns["time"]

    def col(self, name: str) -> tuple[float, ...]:
        return self.columns[name]


def _value(item: Any, default: float | None = None) -> float:
    if isinstance(item, dict) and "value" in item:
        item = item["value"]
    if item is None:
        if default is None:
            raise _EvidenceProblem("missing_numeric_parameter")
        return default
    result = float(item)
    if not math.isfinite(result):
        raise _EvidenceProblem("nonfinite_numeric_parameter")
    return result


def _design(bundle: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        return bundle["design"], bundle["physical"]
    except (KeyError, TypeError) as exc:
        raise _EvidenceProblem("proposal_missing_design_or_physical") from exc


def _case(bundle: dict[str, Any], case_id: str) -> dict[str, Any] | None:
    if case_id in _PROBES:
        return None
    _, physical = _design(bundle)
    matches = [row for row in physical.get("cases", []) if row.get("id") == case_id]
    if len(matches) != 1:
        raise _EvidenceProblem("case_not_frozen_in_proposal")
    return matches[0]


def _stop_time(bundle: dict[str, Any], contract: dict[str, Any], case_id: str) -> float:
    if case_id in _PROBES:
        rows = [row for row in contract.get("probes", []) if row.get("id") == case_id]
        if len(rows) != 1:
            raise _EvidenceProblem("probe_not_frozen_in_contract")
        return _value(rows[0].get("stop_time_s"))
    case = _case(bundle, case_id)
    assert case is not None
    return _value(case.get("stop_time_s"))


def _mass_components(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    design, _ = _design(bundle)
    result = [*design.get("bodies", []), *design.get("wheels", []),
              *design.get("drives", [])]
    supply = design.get("power_supply")
    if supply:
        result.append(supply)
    if not result or any(not row.get("id") for row in result):
        raise _EvidenceProblem("mass_component_description_incomplete")
    return result


def _wheels(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    design, _ = _design(bundle)
    wheels = list(design.get("wheels", []))
    if not wheels:
        raise _EvidenceProblem("wheel_description_missing")
    return wheels


def _required_columns(bundle: dict[str, Any], case_id: str) -> set[str]:
    if case_id == "shared_supply_probe":
        return {"time", "probeShaftPower", "probeDcPower", "probeDriveLoss",
                "probeBusPower", "probeBusEnergy", "probeBusCurrent"}
    if case_id == "rolling_resistance_probe":
        return {"time", "probeOmega", "probeLoss", "probeKinetic",
                "probeRollingTorque", "probeRollingLoss"}
    required = {"time", "chassisPitch", "chassisPitchRate", "busPower",
                "busEnergy", "busCurrent", "auxPower", "refSpeed"}
    for row in _mass_components(bundle):
        stem = "body_" + row["id"] + "_"
        required.update(stem + suffix for suffix in
                        ("x", "z", "vx", "vz", "pitch", "pitchRate"))
    for row in _wheels(bundle):
        stem = "wheel_" + row["id"] + "_"
        required.update(stem + suffix for suffix in (
            "omega", "omegaRelative", "Fx", "Fz", "normal", "penetration",
            "slip", "elasticEnergy", "normalLoss", "frictionLoss", "featureId",
            "contactCount", "rollingTorque", "rollingLoss", "torque",
            "shaftPower", "dcPower", "driveLoss"))
    return required


def _read_csv(path: Path, required: set[str], stop: float, interval: float) -> _Data:
    try:
        with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
            if not header:
                raise _EvidenceProblem("csv_header_missing")
            names = [name.strip().strip('"') for name in header]
            if any(not name for name in names) or len(names) != len(set(names)):
                raise _EvidenceProblem("csv_header_invalid_or_duplicate")
            missing = sorted(required - set(names))
            if missing:
                raise _EvidenceProblem("csv_missing_columns:" + ",".join(missing))
            selected = [(index, name) for index, name in enumerate(names) if name in required]
            kept = {name: [] for _, name in selected}
            count = 0
            for line, row in enumerate(reader, start=2):
                if len(row) != len(names):
                    raise _EvidenceProblem(f"csv_row_width:line={line}")
                try:
                    parsed = [float(cell) for cell in row]
                except ValueError as exc:
                    raise _EvidenceProblem(f"csv_nonnumeric:line={line}") from exc
                if not all(math.isfinite(value) for value in parsed):
                    raise _EvidenceProblem(f"csv_nonfinite:line={line}")
                for index, name in selected:
                    kept[name].append(parsed[index])
                count += 1
    except _EvidenceProblem:
        raise
    except (OSError, UnicodeError, csv.Error) as exc:
        raise _EvidenceProblem("csv_read_error:" + str(exc)) from exc
    if count < 2:
        raise _EvidenceProblem("csv_too_few_rows")
    columns = {name: tuple(values) for name, values in kept.items()}
    times = columns["time"]
    if any(b < a for a, b in zip(times, times[1:])):
        raise _EvidenceProblem("csv_time_decreases")
    maximum_gap = max(b - a for a, b in zip(times, times[1:]))
    if maximum_gap > interval + max(1e-9, interval * 1e-6):
        raise _EvidenceProblem("csv_sampling_gap_exceeds_contract")
    tolerance = max(1e-9, stop * 1e-9)
    if abs(times[0]) > tolerance or abs(times[-1] - stop) > tolerance:
        raise _EvidenceProblem("csv_time_coverage_incomplete")
    return _Data(columns, count)


def _trapz(times: Sequence[float], values: Iterable[float]) -> float:
    series = tuple(values)
    return sum((tb - ta) * (ya + yb) / 2
               for ta, tb, ya, yb in zip(times, times[1:], series, series[1:]))


def _cumulative(times: Sequence[float], values: Iterable[float]) -> tuple[float, ...]:
    series = tuple(values)
    result = [0.0]
    for ta, tb, ya, yb in zip(times, times[1:], series, series[1:]):
        result.append(result[-1] + (tb - ta) * (ya + yb) / 2)
    return tuple(result)


def _interp(data: _Data, column: str, when: float) -> float:
    times, values = data.time, data.col(column)
    if when < times[0] or when > times[-1]:
        raise _EvidenceProblem("required_window_outside_csv")
    right = bisect.bisect_right(times, when)
    if right and times[right - 1] == when:
        return values[right - 1]
    if right == 0 or right == len(times):
        return values[0] if right == 0 else values[-1]
    left = right - 1
    width = times[right] - times[left]
    if width <= 0:
        return values[right]
    alpha = (when - times[left]) / width
    return values[left] + alpha * (values[right] - values[left])


def _window(data: _Data, column: str, start: float, stop: float) -> tuple[float, ...]:
    if start > stop or start < data.time[0] or stop > data.time[-1]:
        raise _EvidenceProblem("required_window_outside_csv")
    values = [value for time, value in zip(data.time, data.col(column))
              if start <= time <= stop]
    values.extend((_interp(data, column, start), _interp(data, column, stop)))
    return tuple(values)


def _crossings(times: Sequence[float], values: Sequence[float], level: float,
               direction: int = 1) -> list[float]:
    result: list[float] = []
    shifted = [value - level for value in values]
    for ta, tb, ya, yb in zip(times, times[1:], shifted, shifted[1:]):
        crossed = (direction >= 0 and ya <= 0 < yb) or (direction <= 0 and ya >= 0 > yb)
        if not crossed:
            continue
        event = tb if tb == ta or yb == ya else ta - ya * (tb - ta) / (yb - ya)
        if not result or abs(event - result[-1]) > 1e-12:
            result.append(event)
    return result


def _add(checks: list[dict[str, Any]], name: str, value: Any, limit: Any,
         passed: bool, *, evidence: bool = True) -> None:
    checks.append({"name": name, "value": value, "limit": limit,
                   "pass": bool(passed), "evidence": bool(evidence)})


def _finish(checks: list[dict[str, Any]], metrics: dict[str, Any],
            diagnostics: list[str]) -> dict[str, Any]:
    hard_fail = any(item["evidence"] and not item["pass"] for item in checks)
    insufficient = any(not item["evidence"] for item in checks)
    if hard_fail:
        status = "FAIL"
    elif insufficient or diagnostics:
        status = "INSUFFICIENT_EVIDENCE"
    else:
        status = "PASS"
    return {"status": status, "checks": checks, "diagnostics": diagnostics,
            "issues": diagnostics, "metrics": metrics}


def _safe_cv(values: Sequence[float]) -> float:
    mean = fmean(values)
    if abs(mean) <= _EPS:
        return math.inf
    variance = fmean((value - mean) ** 2 for value in values)
    return math.sqrt(variance) / abs(mean)


def _point_segment_distance(px: float, pz: float, a: Sequence[float],
                            b: Sequence[float]) -> float:
    ax, az = a
    bx, bz = b
    dx, dz = bx - ax, bz - az
    scale = dx * dx + dz * dz
    ratio = 0.0 if scale == 0 else max(0.0, min(1.0, ((px-ax)*dx + (pz-az)*dz) / scale))
    return math.hypot(px - (ax + ratio*dx), pz - (az + ratio*dz))


def _terrain(points: Sequence[Sequence[float]]) -> dict[str, Any]:
    pts = [(float(point[0]), float(point[1])) for point in points]
    if len(pts) < 2:
        raise _EvidenceProblem("terrain_polyline_incomplete")
    segments = []
    horizontals = []
    verticals = []
    for index, (a, b) in enumerate(zip(pts, pts[1:]), start=1):
        if b[0] < a[0] or (b[0] != a[0] and b[1] != a[1]) or a == b:
            raise _EvidenceProblem("terrain_not_monotone_axis_aligned")
        segments.append((a, b))
        if a[1] == b[1]:
            horizontals.append((a[0], b[0], a[1], index))
        else:
            verticals.append((a[0], a[1], b[1], index))
    return {"points": pts, "segments": segments, "horizontals": horizontals,
            "verticals": verticals}


def _ground_height(terrain: dict[str, Any], x: float) -> float:
    candidates = [z for left, right, z, _ in terrain["horizontals"]
                  if left - _EPS <= x <= right + _EPS]
    if not candidates:
        raise _EvidenceProblem("body_outside_terrain_observation")
    return max(candidates)


def _world_point(x: float, z: float, pitch: float, point: Sequence[float]) -> tuple[float, float]:
    c, s = math.cos(pitch), math.sin(pitch)
    return x + c*point[0] - s*point[1], z + s*point[0] + c*point[1]


def _edge_vertical_clearance(a: tuple[float, float], b: tuple[float, float],
                             terrain: dict[str, Any]) -> float:
    values: list[float] = []
    for x, za, zb, _ in terrain["verticals"]:
        if min(a[0], b[0]) - _EPS <= x <= max(a[0], b[0]) + _EPS:
            if abs(b[0] - a[0]) <= _EPS:
                z = min(a[1], b[1])
            else:
                ratio = (x - a[0]) / (b[0] - a[0])
                z = a[1] + ratio*(b[1] - a[1])
            values.append(z - max(za, zb))
    return min(values, default=math.inf)


def _shape_clearance(shape: dict[str, Any], pose: tuple[float, float, float],
                     terrain: dict[str, Any]) -> float:
    x, z, pitch = pose
    if shape.get("type") == "polygon":
        vertices = [_world_point(x, z, pitch, point) for point in shape.get("vertices", [])]
        if len(vertices) < 3:
            raise _EvidenceProblem("polygon_envelope_invalid")
        clearances = [point[1] - _ground_height(terrain, point[0]) for point in vertices]
        for a, b in zip(vertices, vertices[1:] + vertices[:1]):
            clearances.append(_edge_vertical_clearance(a, b, terrain))
        return min(clearances)
    if shape.get("type") == "capsule":
        a = _world_point(x, z, pitch, shape["start"])
        b = _world_point(x, z, pitch, shape["end"])
        radius = _value(shape.get("radius"))
        clearances = [a[1] - _ground_height(terrain, a[0]),
                      b[1] - _ground_height(terrain, b[0]),
                      _edge_vertical_clearance(a, b, terrain)]
        signed = min(clearances)
        if signed <= 0:
            return signed - radius
        # A near vertical riser may touch the rounded side without either
        # centreline endpoint crossing it. Segment distance catches that case.
        distance = min(min(_point_segment_distance(*a, c, d),
                           _point_segment_distance(*b, c, d),
                           _point_segment_distance(*c, a, b),
                           _point_segment_distance(*d, a, b))
                       for c, d in terrain['segments'])
        return distance - radius
    raise _EvidenceProblem("unsupported_envelope_shape")


def _wheel_geometric_penetration(x: float, z: float, radius: float,
                                 terrain: dict[str, Any]) -> tuple[float, set[int]]:
    distances = [_point_segment_distance(x, z, a, b)
                 for a, b in terrain["segments"]]
    nearest = min(distances)
    features = {100+index + 1 for index, value in enumerate(distances)
                if value <= nearest + 5e-4}
    for index,point in enumerate(terrain['points'][1:-1],start=2):
        if math.hypot(x-point[0],z-point[1])<=nearest+5e-4:
            features.add(200+index)
    if z < _ground_height(terrain,x):
        return radius+nearest, features
    return max(0.0, radius - nearest), features


def _verify_shared_probe(data: _Data, checks: list[dict[str, Any]],
                         metrics: dict[str, Any]) -> None:
    expected = {"probeShaftPower": 30.0, "probeDcPower": 37.5,
                "probeDriveLoss": 7.5, "probeBusPower": 42.5,
                "probeBusCurrent": 42.5/48.0}
    for name, target in expected.items():
        error = max(abs(value-target) for value in data.col(name))
        limit = max(1e-8, abs(target)*1e-6)
        _add(checks, name + "_analytic", error, limit, error <= limit)
    energy = data.col("probeBusEnergy")
    closure = max(abs(value - energy[0] + used)
                  for value, used in zip(energy, _cumulative(data.time, data.col("probeBusPower"))))
    _add(checks, "independent_electrical_energy_closure", closure, 1e-5, closure <= 1e-5)
    _add(checks, "probe_initial_energy", abs(energy[0]-100), 1e-8, abs(energy[0]-100) <= 1e-8)
    _add(checks, "probe_terminal_energy", abs(energy[-1]-15), 1e-5, abs(energy[-1]-15) <= 1e-5)
    metrics.update({"energy_scale": 85.0, "cumulative": {"bus_energy_used": energy[0]-energy[-1]},
                    "signals": {name: {"peak_abs": max(map(abs, data.col(name))),
                                       "terminal": data.col(name)[-1]}
                                for name in expected}, "event_times": {}})


def _rolling_reference(time: float) -> float:
    # asinh(exp(log(sinh(100))-50t)) avoids creating a second enormous value.
    log_sinh_100 = 100.0 - math.log(2.0) + math.log1p(-math.exp(-200.0))
    y = log_sinh_100 - 50.0*time
    return 0.1 * math.asinh(math.exp(y))


def _verify_rolling_probe(data: _Data, checks: list[dict[str, Any]],
                          metrics: dict[str, Any]) -> None:
    omega = data.col("probeOmega")
    reference = tuple(_rolling_reference(time) for time in data.time)
    omega_error = max(abs(a-b) for a, b in zip(omega, reference))
    _add(checks, "independent_rolling_omega", omega_error, 1e-4, omega_error <= 1e-4)
    kinetic = tuple(0.5*0.01*value*value for value in omega)
    kinetic_error = max(abs(a-b) for a, b in zip(data.col("probeKinetic"), kinetic))
    _add(checks, "independent_rotor_kinetic_energy", kinetic_error, 1e-8, kinetic_error <= 1e-8)
    torque_ref = tuple(-0.05*math.tanh(value/0.1) for value in omega)
    torque_error = max(abs(a-b) for a, b in zip(data.col("probeRollingTorque"), torque_ref))
    _add(checks, "independent_rolling_torque", torque_error, 1e-8, torque_error <= 1e-8)
    loss_ref = tuple(-torque*speed for torque, speed in zip(torque_ref, omega))
    loss = data.col("probeRollingLoss")
    loss_error = max(abs(a-b) for a, b in zip(loss, loss_ref))
    _add(checks, "independent_rolling_loss", loss_error, 1e-8, loss_error <= 1e-8)
    generic_loss_error = max(abs(a-b) for a, b in zip(data.col("probeLoss"), loss_ref))
    _add(checks, "reported_probe_loss_identity", generic_loss_error, 1e-8, generic_loss_error <= 1e-8)
    closure = max(abs(k - kinetic[0] + used)
                  for k, used in zip(kinetic, _cumulative(data.time, loss)))
    _add(checks, "independent_mechanical_energy_closure", closure, 5e-5, closure <= 5e-5)
    _add(checks, "rolling_loss_nonnegative", min(loss), ">=-1e-8W", min(loss) >= -1e-8)
    metrics.update({"energy_scale": 0.5, "cumulative": {"rolling_loss": _trapz(data.time, loss)},
                    "signals": {"omega": {"peak_abs": max(map(abs, omega)), "terminal": omega[-1]}},
                    "event_times": {}})


def _component_series(bundle: dict[str, Any], data: _Data) -> tuple[list[float], list[float],
                                                                      list[float], list[float]]:
    components = _mass_components(bundle)
    wheels = {row["id"] for row in _wheels(bundle)}
    kinetic = [0.0] * data.row_count
    potential = [0.0] * data.row_count
    com_x_num = [0.0] * data.row_count
    com_z_num = [0.0] * data.row_count
    total_mass = 0.0
    for row in components:
        ident = row["id"]
        count = _value(row.get("count"), 1.0)
        mass = count * _value(row.get("mass"))
        inertia = count * _value(row.get("inertia"), 0.0)
        stem = "body_" + ident + "_"
        xs, zs = data.col(stem+"x"), data.col(stem+"z")
        vxs, vzs = data.col(stem+"vx"), data.col(stem+"vz")
        rates = data.col("wheel_"+ident+"_omega") if ident in wheels else data.col(stem+"pitchRate")
        for index in range(data.row_count):
            kinetic[index] += 0.5*mass*(vxs[index]**2 + vzs[index]**2) + 0.5*inertia*rates[index]**2
            potential[index] += mass*_G*zs[index]
            com_x_num[index] += mass*xs[index]
            com_z_num[index] += mass*zs[index]
        total_mass += mass
    if total_mass <= 0:
        raise _EvidenceProblem("nonpositive_total_mass")
    return kinetic, potential, [value/total_mass for value in com_x_num], [value/total_mass for value in com_z_num]


def _vehicle_energy(bundle: dict[str, Any], data: _Data) -> tuple[dict[str, Any], list[float]]:
    kinetic, potential, _, _ = _component_series(bundle, data)
    wheels = _wheels(bundle)
    elastic = [sum(data.col("wheel_"+row["id"]+"_elasticEnergy")[index] for row in wheels)
               for index in range(data.row_count)]
    shaft = [sum(data.col("wheel_"+row["id"]+"_shaftPower")[index] for row in wheels)
             for index in range(data.row_count)]
    losses = [sum(data.col("wheel_"+row["id"]+suffix)[index]
                  for row in wheels for suffix in ("_normalLoss", "_frictionLoss", "_rollingLoss"))
              for index in range(data.row_count)]
    total = [k+u+e for k, u, e in zip(kinetic, potential, elastic)]
    work = _cumulative(data.time, shaft)
    dissipated = _cumulative(data.time, losses)
    residual = [energy-total[0]-supplied+lost
                for energy, supplied, lost in zip(total, work, dissipated)]
    height_scale = 0.0
    for row in _mass_components(bundle):
        mass = _value(row.get("count"), 1.0)*_value(row.get("mass"))
        z = data.col("body_"+row["id"]+"_z")
        height_scale += mass*_G*(max(z)-min(z))
    scale = max(1.0, _trapz(data.time, map(abs, shaft)), height_scale)
    metrics = {"kinetic_initial_J": kinetic[0], "kinetic_final_J": kinetic[-1],
               "potential_change_J": potential[-1]-potential[0],
               "elastic_initial_J": elastic[0], "elastic_final_J": elastic[-1],
               "shaft_work_J": work[-1], "contact_and_rolling_loss_J": dissipated[-1],
               "mechanical_residual_max_J": max(map(abs, residual)), "energy_scale_J": scale}
    return metrics, residual


def _vehicle_sanity(bundle: dict[str, Any], contract: dict[str, Any], data: _Data,
                    checks: list[dict[str, Any]], metrics: dict[str, Any]) -> None:
    _, physical = _design(bundle)
    wheels = _wheels(bundle)
    tension_tolerance = _value(contract["physical"].get("normal_tension_tolerance_N"), 1e-8)
    loss_tolerance = _value(contract["physical"].get("negative_loss_tolerance_W"), 1e-8)
    for wheel in wheels:
        ident = wheel["id"]
        stem = "wheel_"+ident+"_"
        normal = data.col(stem+"normal")
        _add(checks, ident+"_normal_no_tension", min(normal), ">=-tolerance",
             min(normal) >= -tension_tolerance)
        radius = _value(wheel.get("radius"))
        limit = min(0.02, 0.15*radius)
        penetration = data.col(stem+"penetration")
        _add(checks, ident+"_penetration_limit", max(penetration), limit,
             min(penetration) >= -1e-8 and max(penetration) <= limit)
        for suffix in ("normalLoss", "frictionLoss", "rollingLoss", "driveLoss"):
            values = data.col(stem+suffix)
            _add(checks, ident+"_"+suffix+"_nonnegative", min(values), ">=-tolerance",
                 min(values) >= -loss_tolerance)
        counts = data.col(stem+"contactCount")
        count_error = max(abs(value-round(value)) for value in counts)
        _add(checks, ident+"_contact_count_integral", count_error, 1e-8,
             count_error <= 1e-8 and min(counts) >= 0)
        shaft, torque, omega = data.col(stem+"shaftPower"), data.col(stem+"torque"), data.col(stem+"omegaRelative")
        shaft_error = max(abs(power-tau*speed) for power, tau, speed in zip(shaft, torque, omega))
        shaft_limit = max(1e-8, max(map(abs, shaft))*1e-6)
        _add(checks, ident+"_shaft_power_identity", shaft_error, shaft_limit, shaft_error <= shaft_limit)
        dc, drive_loss = data.col(stem+"dcPower"), data.col(stem+"driveLoss")
        drive_error = max(abs(loss-(supply-power)) for loss, supply, power in zip(drive_loss, dc, shaft))
        drive_limit = max(1e-8, max(map(abs, drive_loss))*1e-6)
        _add(checks, ident+"_drive_loss_identity", drive_error, drive_limit, drive_error <= drive_limit)
        drive=next(r for r in bundle['design']['drives'] if r['id']==ident+'_drive')
        torque_cap=drive['count']*_value(drive['torque'])
        power_cap=drive['count']*_value(drive['power'])
        _add(checks,ident+'_rated_torque',max(map(abs,torque)),torque_cap,max(map(abs,torque))<=torque_cap+1e-8)
        _add(checks,ident+'_rated_shaft_power',max(map(abs,shaft)),power_cap,max(map(abs,shaft))<=power_cap+1e-8)
        dc_error=max(abs(v-max(p,0)/_value(drive['efficiency'])) for v,p in zip(dc,shaft))
        _add(checks,ident+'_dc_efficiency_identity',dc_error,1e-6,dc_error<=1e-6)
        rr=physical['rolling_resistance']
        expected_rr=[-_value(rr['coefficient'])*radius*f*math.tanh(w/_value(rr['omega_regularization_rad_s'])) for f,w in zip(normal,data.col(stem+'omega'))]
        rr_error=max(abs(a-b) for a,b in zip(expected_rr,data.col(stem+'rollingTorque')))
        _add(checks,ident+'_external_rolling_torque',rr_error,1e-7,rr_error<=1e-7)
        loss_error=max(abs(loss+tau*w) for loss,tau,w in zip(data.col(stem+'rollingLoss'),expected_rr,data.col(stem+'omega')))
        _add(checks,ident+'_external_rolling_loss',loss_error,1e-7,loss_error<=1e-7)
    bus_expected = [sum(data.col("wheel_"+wheel["id"]+"_dcPower")[index] for wheel in wheels)
                    + data.col("auxPower")[index] for index in range(data.row_count)]
    bus_error = max(abs(a-b) for a, b in zip(data.col("busPower"), bus_expected))
    _add(checks, "shared_bus_power_identity", bus_error,
         max(1e-6, max(map(abs, bus_expected))*1e-6),
         bus_error <= max(1e-6, max(map(abs, bus_expected))*1e-6))
    supply = physical.get("supply", {})
    aux_error=max(abs(p-_value(supply['auxiliary_W'])) for p in data.col('auxPower'))
    _add(checks,'auxiliary_power',aux_error,1e-8,aux_error<=1e-8)
    initial_error=abs(data.col('busEnergy')[0]-_value(supply['usable_energy_J']))
    _add(checks,'shared_initial_energy',initial_error,1e-5,initial_error<=1e-5)
    voltage = _value(supply.get("voltage_V"))
    current_error = max(abs(power-voltage*current)
                        for power, current in zip(data.col("busPower"), data.col("busCurrent")))
    _add(checks, "bus_current_identity", current_error,
         max(1e-6, max(map(abs, data.col("busPower")))*1e-6),
         current_error <= max(1e-6, max(map(abs, data.col("busPower")))*1e-6))
    _add(checks, "supply_power_limit", max(data.col("busPower")), supply.get("continuous_power_W"),
         max(data.col("busPower")) <= _value(supply.get("continuous_power_W"))+1e-8)
    _add(checks, "supply_current_limit", max(data.col("busCurrent")), supply.get("continuous_current_A"),
         max(data.col("busCurrent")) <= _value(supply.get("continuous_current_A"))+1e-8)
    _add(checks, "usable_energy_nonnegative", min(data.col("busEnergy")), ">=0J",
         min(data.col("busEnergy")) >= -1e-8)
    electrical = [value-data.col("busEnergy")[0]+used for value, used in
                  zip(data.col("busEnergy"), _cumulative(data.time, data.col("busPower")))]
    electrical_scale = max(1.0, _trapz(data.time, data.col("busPower")))
    electrical_limit = _value(contract["physical"].get("electrical_energy_residual_fraction"), .005)*electrical_scale
    _add(checks, "electrical_energy_closure", max(map(abs, electrical)), electrical_limit,
         max(map(abs, electrical)) <= electrical_limit)
    energy, residual = _vehicle_energy(bundle, data)
    mechanical_limit = _value(contract["physical"].get("mechanical_energy_residual_fraction"), .01)*energy["energy_scale_J"]
    _add(checks, "mechanical_energy_closure", max(map(abs, residual)), mechanical_limit,
         max(map(abs, residual)) <= mechanical_limit)
    metrics.update({"energy_terms": energy, "energy_scale": energy["energy_scale_J"],
                    "cumulative": {"shaft_work": energy["shaft_work_J"],
                                   "contact_and_rolling_loss": energy["contact_and_rolling_loss_J"],
                                   "bus_energy_used": data.col("busEnergy")[0]-data.col("busEnergy")[-1]}})


def _geometry(bundle: dict[str, Any], case: dict[str, Any], data: _Data,
              contract: dict[str, Any], checks: list[dict[str, Any]],
              metrics: dict[str, Any]) -> dict[str, Any]:
    design, physical = _design(bundle)
    terrain = _terrain(case.get("terrain_points", []))
    minimum_envelope = math.inf
    for ident, shapes in physical.get("envelopes_local_xz", {}).items():
        stem = "body_"+ident+"_"
        for index in range(data.row_count):
            pose = (data.col(stem+"x")[index], data.col(stem+"z")[index],
                    data.col(stem+"pitch")[index])
            for shape in shapes:
                minimum_envelope = min(minimum_envelope, _shape_clearance(shape, pose, terrain))
    clearance_limit = _value(contract["physical"].get("nonwheel_terrain_clearance_min_m"), -1e-4)
    _add(checks, "nonwheel_terrain_clearance", minimum_envelope, clearance_limit,
         minimum_envelope >= clearance_limit)
    max_geom_error = 0.0
    bad_feature = 0
    for wheel in design.get("wheels", []):
        ident, radius = wheel["id"], _value(wheel.get("radius"))
        xs, zs = data.col("body_"+ident+"_x"), data.col("body_"+ident+"_z")
        reported = data.col("wheel_"+ident+"_penetration")
        normal = data.col("wheel_"+ident+"_normal")
        feature = data.col("wheel_"+ident+"_featureId")
        for x, z, observed, force, raw_feature in zip(xs, zs, reported, normal, feature):
            penetration, nearest = _wheel_geometric_penetration(x, z, radius, terrain)
            max_geom_error = max(max_geom_error, abs(observed-penetration))
            if force > 5:
                rounded = round(raw_feature)
                if abs(raw_feature-rounded) > 1e-8 or rounded not in nearest:
                    bad_feature += 1
    _add(checks, "wheel_penetration_from_geometry", max_geom_error, 5e-4,
         max_geom_error <= 5e-4)
    _add(checks, "loaded_contact_feature_matches_geometry", bad_feature, 0, bad_feature == 0)
    metrics["minimum_nonwheel_clearance_m"] = minimum_envelope
    metrics["wheel_penetration_geometry_error_max_m"] = max_geom_error
    return terrain


def _settled_balance(bundle: dict[str, Any], data: _Data, contract: dict[str, Any],
                     terrain: dict[str, Any], checks: list[dict[str, Any]],
                     metrics: dict[str, Any]) -> None:
    start, stop = map(float, contract["physical"]["settled_window_s"])
    wheels = _wheels(bundle)
    total_mass = sum(_value(row.get("count"), 1.0)*_value(row.get("mass"))
                     for row in _mass_components(bundle))
    vertical_speed = max(max(map(abs, _window(data, "body_"+row["id"]+"_vz", start, stop)))
                         for row in _mass_components(bundle))
    pitch_rate = max(map(abs, _window(data, "chassisPitchRate", start, stop)))
    _add(checks, "settled_vertical_speed", vertical_speed,
         contract["physical"]["settled_vertical_speed_max_m_s"],
         vertical_speed <= _value(contract["physical"]["settled_vertical_speed_max_m_s"]))
    _add(checks, "settled_pitch_rate", pitch_rate,
         contract["physical"]["settled_pitch_rate_max_rad_s"],
         pitch_rate <= _value(contract["physical"]["settled_pitch_rate_max_rad_s"]))
    _, _, com_x, com_z = _component_series(bundle, data)
    residuals, moments = [], []
    for index, time in enumerate(data.time):
        if not start <= time <= stop:
            continue
        fx = sum(data.col("wheel_"+wheel["id"]+"_Fx")[index] for wheel in wheels)
        fz = sum(data.col("wheel_"+wheel["id"]+"_Fz")[index] for wheel in wheels)
        residuals.append(math.hypot(fx, fz-total_mass*_G))
        moment = 0.0
        for wheel in wheels:
            ident = wheel["id"]
            x = data.col("body_"+ident+"_x")[index]
            z = data.col("body_"+ident+"_z")[index]
            wx = data.col("wheel_"+ident+"_Fx")[index]
            wz = data.col("wheel_"+ident+"_Fz")[index]
            # During the frozen zero-drive settling window every entry is a
            # horizontal plane. Use the centre force plus its true friction
            # couple, not the compressed terrain point as a shortened lever.
            distances = [_point_segment_distance(x, z, a, b) for a, b in terrain["segments"]]
            segment = terrain["segments"][distances.index(min(distances))]
            a, b = segment
            dx, dz = b[0]-a[0], b[1]-a[1]
            if abs(dz)>1e-12:
                raise _EvidenceProblem('settling_contact_is_not_on_frozen_entry_plane')
            moment += (x-com_x[index])*wz - (z-com_z[index])*wx + _value(wheel['radius'])*wx
            # Public rollingTorque is world+y, while pitch moment is -y.
            moment -= data.col("wheel_"+ident+"_rollingTorque")[index]
        moments.append(abs(moment))
    if not residuals:
        raise _EvidenceProblem("settled_window_has_no_rows")
    force_limit = _value(contract["physical"]["settled_force_residual_fraction_of_weight"])*total_mass*_G
    _add(checks, "settled_external_force_balance", max(residuals), force_limit,
         max(residuals) <= force_limit)
    moment_limit = _value(contract["physical"]["settled_moment_residual_Nm"])
    _add(checks, "settled_external_moment_balance", max(moments), moment_limit,
         max(moments) <= moment_limit)
    metrics.update({"settled_force_residual_max_N": max(residuals),
                    "settled_moment_residual_max_Nm": max(moments)})


def _continuous_duration(times: Sequence[float], mask: Sequence[bool]) -> float:
    longest = current = 0.0
    for ta, tb, active_a, active_b in zip(times, times[1:], mask, mask[1:]):
        if active_a and active_b:
            current += tb-ta
            longest = max(longest, current)
        else:
            current = 0.0
    return longest


def _task_common(bundle: dict[str, Any], case: dict[str, Any], data: _Data,
                 contract: dict[str, Any], terrain: dict[str, Any],
                 checks: list[dict[str, Any]], metrics: dict[str, Any]) -> None:
    physical = contract["physical"]
    pitch_limit = math.radians(_value(physical["pitch_absolute_limit_deg"]))
    pitch_peak = max(map(abs, data.col("chassisPitch")))
    _add(checks, "absolute_pitch_limit", pitch_peak, pitch_limit, pitch_peak <= pitch_limit)
    wheels = _wheels(bundle)
    longest_slip = 0.0
    for wheel in wheels:
        stem = "wheel_"+wheel["id"]+"_"
        mask = [force > 5 and abs(slip) > _value(physical["slip_high_threshold_m_s"])
                for force, slip in zip(data.col(stem+"normal"), data.col(stem+"slip"))]
        longest_slip = max(longest_slip, _continuous_duration(data.time, mask))
    _add(checks, "continuous_high_slip", longest_slip,
         physical["slip_high_continuous_time_max_s"],
         longest_slip <= _value(physical["slip_high_continuous_time_max_s"])+1e-9)
    x = data.col("body_chassis_x")
    maximum = x[0]
    backslide = 0.0
    for value in x:
        maximum = max(maximum, value)
        backslide = max(backslide, maximum-value)
    _add(checks, "maximum_backslide", backslide, physical["backslide_max_m"],
         backslide <= _value(physical["backslide_max_m"])+1e-9)
    if case.get("steps", 0) and case.get("id") != "fixed_nose_flat_brake":
        stagnation = False
        control=bundle['physical']['controller']
        start_index = bisect.bisect_left(data.time, control['settle_until_s']+control['ramp_duration_s'])
        for index in range(start_index, data.row_count):
            end = data.time[index]+_value(physical["stagnation_window_s"])
            if end > data.time[-1]:
                break
            later = _interp(data, "body_chassis_x", end)
            if later-x[index] < _value(physical["stagnation_progress_min_m"])-1e-9:
                stagnation = True
                break
        _add(checks, "no_stagnation_during_traversal", stagnation, False, not stagnation)
    metrics.update({"pitch_peak_rad": pitch_peak, "high_slip_longest_s": longest_slip,
                    "maximum_backslide_m": backslide})


def _exit_threshold(case: dict[str, Any], terrain: dict[str, Any]) -> float:
    if case.get('exit_plane_x_m') is not None:
        return float(case['exit_plane_x_m'])+float(case['exit_margin_m'])
    tread = _value(case.get("tread_m"), .28)
    long_segments = [(left, right) for left, right, _, _ in terrain["horizontals"]
                     if right-left > 2*tread]
    if not long_segments:
        raise _EvidenceProblem("exit_tread_not_identifiable")
    return max(left for left, _ in long_segments) + 0.5


def _exit_and_cruise(bundle: dict[str, Any], case: dict[str, Any], data: _Data,
                     contract: dict[str, Any], terrain: dict[str, Any],
                     checks: list[dict[str, Any]], metrics: dict[str, Any]) -> None:
    threshold = _exit_threshold(case, terrain)
    minimum = math.inf
    for wheel in _wheels(bundle):
        minimum = min(minimum, data.col("body_"+wheel["id"]+"_x")[-1]-_value(wheel["radius"]))
    _, physical = _design(bundle)
    for ident, shapes in physical.get("envelopes_local_xz", {}).items():
        pose = (data.col("body_"+ident+"_x")[-1], data.col("body_"+ident+"_z")[-1],
                data.col("body_"+ident+"_pitch")[-1])
        for shape in shapes:
            points = shape.get("vertices", [shape.get("start"), shape.get("end")])
            radius = _value(shape.get("radius"), 0.0)
            minimum = min(minimum, min(_world_point(*pose, point)[0] for point in points)-radius)
    _add(checks, "complete_geometry_beyond_exit", minimum, threshold, minimum >= threshold)
    duration = _value(contract["physical"]["exit_speed_window_s"])
    speeds = _window(data, "body_chassis_vx", data.time[-1]-duration, data.time[-1])
    low, high = map(float, contract["physical"]["cruise_exit_speed_window_m_s"])
    _add(checks, "continued_exit_cruise", [min(speeds), max(speeds)], [low, high],
         min(speeds) >= low and max(speeds) <= high)
    metrics.update({"exit_threshold_x_m": threshold, "terminal_geometry_x_min_m": minimum})


def _last_support_window(data, ident, mask, end):
    """Latest continuous 0.1s stable support window, including event rows."""
    zs=data.col('body_'+ident+'_z'); vz=data.col('body_'+ident+'_vz')
    run_start=0; latest=None
    for i,t in enumerate(data.time):
        if t>end: break
        if not mask[i] or abs(vz[i])>.02:
            run_start=i+1
            continue
        left=bisect.bisect_left(data.time,t-.1)
        if left<run_start or left>i or t-data.time[left]<.1-1e-8: continue
        window=zs[left:i+1]
        if max(window)-min(window)<=.005:
            latest=(t,fmean(window))
    return latest


def _stair_events(bundle: dict[str, Any], case: dict[str, Any], data: _Data,
                  contract: dict[str, Any], terrain: dict[str, Any],
                  checks: list[dict[str, Any]], metrics: dict[str, Any]) -> None:
    direction = case.get("direction")
    verticals = terrain["verticals"]
    if direction == "ascent":
        risers = [row for row in verticals if row[2] > row[1]]
    elif direction == "descent":
        risers = [row for row in verticals if row[2] < row[1]]
    else:
        risers = verticals
    expected = 40 if case["id"] == "fixed_nose_full_mission" else int(case.get("steps", 0))
    _add(checks, "frozen_riser_count", len(risers), expected, len(risers) == expected)
    event_times: dict[str, float] = {}
    crossing_by_first_edge: dict[str, float] = {}
    for wheel in _wheels(bundle):
        ident = wheel["id"]
        xs = data.col("body_"+ident+"_x")
        zs = data.col("body_"+ident+"_z")
        normal = data.col("wheel_"+ident+"_normal")
        features = data.col("wheel_"+ident+"_featureId")
        complete = True
        for number, (edge_x, za, zb, _) in enumerate(risers, start=1):
            crossings = _crossings(data.time, xs, edge_x, 1)
            if not crossings:
                complete = False
                continue
            event = crossings[0]
            event_times[f"{ident}_riser_{number}"] = event
            if number == (1 if direction == "descent" else max(1, len(risers)//2+1) if case["id"] == "fixed_nose_full_mission" else 1):
                crossing_by_first_edge[ident] = event
            left = bisect.bisect_left(data.time, max(data.time[0], event-0.25))
            right = bisect.bisect_right(data.time, min(data.time[-1], event+0.75))
            local_z = zs[left:right]
            contact_motion = ((local_z and max(local_z)-min(local_z) >= 0.005) or
                              (right > left and max(normal[left:right]) > 5 and
                               len({round(value) for value in features[left:right]}) >= 2))
            complete = complete and bool(contact_motion)
        _add(checks, ident+"_all_riser_crossings_with_physics", complete, True, complete,
             evidence=bool(risers) and all(f"{ident}_riser_{n}" in event_times for n in range(1, len(risers)+1)))
    if direction in {"descent", "up_then_forward_down"}:
        rule = contract["physical"].get("serial_mission_rule", {}) if direction == "up_then_forward_down" else contract["physical"]["front_first_rule"]
        order = rule.get("edge_crossing_order", {}).get("fixed_nose") if isinstance(rule.get("edge_crossing_order"), dict) else rule.get("edge_crossing_order")
        if order and all(name in crossing_by_first_edge for name in order):
            values = [crossing_by_first_edge[name] for name in order]
            tie = _value(contract["physical"]["front_first_rule"].get("event_tie_tolerance_s"), .002)
            ordered = all(later+tie >= earlier for earlier, later in zip(values, values[1:]))
            _add(checks, "forward_descent_edge_order", values, order, ordered)
        else:
            _add(checks, "forward_descent_edge_order", "missing", order, False, evidence=False)
        drops: dict[str, float] = {}
        descending=[r for r in verticals if r[2]<r[1]]
        first_edge,upper_z,_,_=descending[0]
        upper=next(row for row in terrain['horizontals'] if abs(row[1]-first_edge)<1e-8 and abs(row[2]-upper_z)<1e-8)
        upper_feature=100+upper[3]
        lower_features={100+i for _,_,height,i in terrain['horizontals'] if height<upper_z-1e-8}
        lower_features.update(200+i for i,(_,height) in enumerate(terrain['points'],1) if height<upper_z-1e-8)
        histories={}
        for ident in ("front", "rear", "nose"):
            if ident not in crossing_by_first_edge:
                continue
            crossing = crossing_by_first_edge[ident]
            z = data.col("body_"+ident+"_z")
            normal = data.col("wheel_"+ident+"_normal")
            features=data.col('wheel_'+ident+'_featureId')
            counts=data.col('wheel_'+ident+'_contactCount')
            xs=data.col('body_'+ident+'_x')
            # With exactly one active contact, the public normal is the upper
            # face force. Multiple contacts are not silently treated as that face.
            supported=[round(f)==upper_feature and n>5 and round(c)==1 for f,n,c in zip(features,normal,counts)]
            serial=direction=='up_then_forward_down'
            settle_start,settle_end=contract['physical']['settled_window_s']
            eligible=[s and x>=upper[0]-1e-8 for s,x in zip(supported,xs)] if serial else [s and settle_start<=t<=settle_end for s,t in zip(supported,data.time)]
            reference_window=_last_support_window(data,ident,eligible,crossing if serial else settle_end)
            history={'upper_feature':upper_feature,'reference_window':reference_window,
                     'initially_airborne':max(_window(data,'wheel_'+ident+'_normal',settle_start,settle_end))<=5}
            lower=next((t for t,f,n in zip(data.time,features,normal) if t>=crossing and round(f) in lower_features and n>5),None)
            history['lower_contact_s']=lower
            if lower is not None: event_times[ident+'_lower_contact']=lower
            histories[ident]=history
            if reference_window is None:
                if ident!='nose':
                    _add(checks,ident+'_upper_support_reference','missing','continuous 0.1s',False,evidence=False)
                continue
            window_end,reference=reference_window
            loss=next((t for t,s in zip(data.time,supported) if t>window_end and not s),None)
            history['upper_support_loss_s']=loss
            if loss is not None: event_times[ident+'_upper_support_loss']=loss
            _add(checks,ident+'_upper_to_lower_support',history, 'upper loss then lower acquisition',
                 loss is not None and lower is not None and lower>=loss)
            for time, height in zip(data.time, z):
                if time >= (window_end if serial else crossing) and height <= reference-.01:
                    drops[ident] = time
                    event_times[ident+"_actual_drop"] = time
                    break
        metrics['descent_support_histories']=histories
        if {'front','rear'}<=set(drops):
            tie = _value(contract["physical"]["front_first_rule"].get("event_tie_tolerance_s"), .002)
            _add(checks, "forward_descent_actual_drop_order", [drops["front"], drops["rear"]],
                 ["front", "rear"], drops["front"] <= drops["rear"]+tie)
        else:
            _add(checks, "forward_descent_actual_drop_order", sorted(drops), ["front", "rear"],
                 False, evidence=False)
    metrics["event_times"] = event_times


def _full_mission_trace(csv_path: Path, bundle: dict[str, Any], contract: dict[str, Any],
                        checks: list[dict[str, Any]]) -> None:
    path = Path(csv_path).with_name("trace.json")
    try:
        trace = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        _add(checks, "single_solve_trace", "trace.json missing/invalid", True, False, evidence=False)
        return
    required = set(contract["physical"]["serial_mission_rule"]["no_reset"])
    actual = set(trace.get("state_continuity", {}).get("no_reset", []))
    semantic = trace.get("output_semantics", {}).get("wheel")
    passed = (trace.get("case_id") == "fixed_nose_full_mission" and
              trace.get("model_class") == "V11CGenerated.Scene" and
              trace.get("one_solve") is True and required <= actual and
              semantic == "synchronous_pair_aggregate_count_applied_once")
    _add(checks, "single_solve_trace", {"one_solve": trace.get("one_solve"),
         "no_reset": sorted(actual), "wheel_semantics": semantic}, True, passed)
    design, physical = _design(bundle)
    frozen = trace.get("frozen", {})
    for name, expected in (("design_digest", design.get("design_digest")),
                           ("physical_digest", physical.get("physical_digest"))):
        _add(checks, "trace_"+name, frozen.get(name), expected,
             bool(expected) and frozen.get(name) == expected)


def _vehicle_case(bundle: dict[str, Any], contract: dict[str, Any], case_id: str,
                  csv_path: Path, data: _Data, checks: list[dict[str, Any]],
                  metrics: dict[str, Any]) -> None:
    case = _case(bundle, case_id)
    assert case is not None
    _vehicle_sanity(bundle, contract, data, checks, metrics)
    terrain = _geometry(bundle, case, data, contract, checks, metrics)
    _settled_balance(bundle, data, contract, terrain, checks, metrics)
    _task_common(bundle, case, data, contract, terrain, checks, metrics)
    if case_id == "fixed_nose_flat_brake":
        start, stop = map(float, contract["physical"]["brake_stop_window_s"])
        speed = max(map(abs, _window(data, "body_chassis_vx", start, stop)))
        omega = max(max(map(abs, _window(data, "wheel_"+wheel["id"]+"_omega", start, stop)))
                    for wheel in _wheels(bundle))
        distance = max(data.col("body_chassis_x"))-_interp(data, "body_chassis_x", 12.0)
        _add(checks, "brake_vehicle_speed", speed, contract["physical"]["brake_speed_max_m_s"],
             speed <= _value(contract["physical"]["brake_speed_max_m_s"]))
        _add(checks, "brake_wheel_speed", omega, contract["physical"]["brake_wheel_speed_max_rad_s"],
             omega <= _value(contract["physical"]["brake_wheel_speed_max_rad_s"]))
        _add(checks, "brake_stop_distance", distance,
             contract["physical"]["brake_stop_distance_from_t12_max_m"],
             distance <= _value(contract["physical"]["brake_stop_distance_from_t12_max_m"]))
    elif case_id == "fixed_nose_flat_energy":
        start, stop = map(float, contract["physical"]["steady_energy_window_s"])
        speed = _window(data, "body_chassis_vx", start, stop)
        target = _window(data, "refSpeed", start, stop)
        relative = max(abs(a-b)/max(abs(b), 1e-9) for a, b in zip(speed, target))
        power = _window(data, "busPower", start, stop)
        cv = _safe_cv(power)
        _add(checks, "steady_speed_tracking", relative, contract["physical"]["steady_speed_relative_error"],
             relative <= _value(contract["physical"]["steady_speed_relative_error"]))
        _add(checks, "steady_power_cv", cv, contract["physical"]["steady_power_cv_max"],
             cv <= _value(contract["physical"]["steady_power_cv_max"]))
        metrics.update({"steady_speed_relative_error": relative, "steady_power_cv": cv})
    else:
        _stair_events(bundle, case, data, contract, terrain, checks, metrics)
        _exit_and_cruise(bundle, case, data, contract, terrain, checks, metrics)
        if case_id == "fixed_nose_full_mission":
            _full_mission_trace(csv_path, bundle, contract, checks)
    signals: dict[str, dict[str, float]] = {}
    wheel_ids={w['id'] for w in _wheels(bundle)}
    for row in _mass_components(bundle):
        for suffix in ("x", "z", "vx", "vz", "pitch", "pitchRate"):
            if row['id'] in wheel_ids and suffix in ('pitch','pitchRate'):
                continue  # wheel spin is compared as omega; atan2 orientation wraps
            name = "body_"+row["id"]+"_"+suffix
            values = data.col(name)
            signals[name] = {"peak_abs": max(map(abs, values)), "terminal": values[-1]}
    for wheel in _wheels(bundle):
        for suffix in ("omega", "normal"):
            name = "wheel_"+wheel["id"]+"_"+suffix
            values = data.col(name)
            signals[name] = {"peak_abs": max(map(abs, values)), "terminal": values[-1]}
    metrics["signals"] = signals


def verify(bundle: dict[str, Any], contract: dict[str, Any], case_id: str,
           csv_path: str | Path) -> dict[str, Any]:
    """Verify one probe or vehicle case without trusting model-reported PASS flags."""
    checks: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {"case_id": case_id}
    diagnostics: list[str] = []
    try:
        stop = _stop_time(bundle, contract, case_id)
        required = _required_columns(bundle, case_id)
        interval = _value(contract.get("numerical", {}).get("base", {}).get("output_interval_s"), .001)
        data = _read_csv(Path(csv_path), required, stop, interval)
        metrics['csv_sha256']=hashlib.sha256(Path(csv_path).read_bytes()).hexdigest()
        metrics.update({"row_count": data.row_count, "observed_start_s": data.time[0],
                        "observed_stop_s": data.time[-1],
                        "duplicate_time_rows": sum(a == b for a, b in zip(data.time, data.time[1:]))})
        if case_id == "shared_supply_probe":
            _verify_shared_probe(data, checks, metrics)
        elif case_id == "rolling_resistance_probe":
            _verify_rolling_probe(data, checks, metrics)
        else:
            _vehicle_case(bundle, contract, case_id, Path(csv_path), data, checks, metrics)
    except _EvidenceProblem as exc:
        diagnostics.append(str(exc))
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        diagnostics.append("verification_input_or_calculation_error:"+str(exc))
    return _finish(checks, metrics, diagnostics)


def _relative(checks: list[dict[str, Any]], name: str, base: float, refined: float,
              relative: float, floor: float) -> None:
    difference = abs(refined-base)
    limit = max(floor, relative*max(abs(base), abs(refined)))
    _add(checks, name, difference, limit, difference <= limit)


def _compare_reports(contract: dict[str, Any], base: dict[str, Any],
                     refined: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    metrics: dict[str, Any] = {"base_status": base["status"], "refined_status": refined["status"]}
    for label, report in (("base", base), ("refined", refined)):
        if report["status"] == "FAIL":
            _add(checks, label+"_physical_and_task", report["status"], "PASS", False)
        elif report["status"] != "PASS":
            _add(checks, label+"_physical_and_task", report["status"], "PASS", False, evidence=False)
        else:
            _add(checks, label+"_physical_and_task", "PASS", "PASS", True)
    if base["status"] != "PASS" or refined["status"] != "PASS":
        return _finish(checks, metrics, diagnostics)
    rules = contract["numerical_comparison"]
    base_signals = base["metrics"].get("signals", {})
    refined_signals = refined["metrics"].get("signals", {})
    if set(base_signals) != set(refined_signals):
        _add(checks, "same_signal_set", sorted(set(base_signals)^set(refined_signals)), [], False,
             evidence=False)
    for name in sorted(set(base_signals) & set(refined_signals)):
        if name.endswith("_normal"):
            _relative(checks, name+"_peak", base_signals[name]["peak_abs"],
                      refined_signals[name]["peak_abs"],
                      _value(rules["peak_contact_force_relative"]),
                      _value(rules["peak_contact_force_abs_N"]))
            continue
        if name.endswith(("_x", "_z")):
            floor = _value(rules["position_abs_m"])
        elif name.endswith(("_vx", "_vz", "_pitchRate")):
            floor = _value(rules["velocity_abs_m_s"])
        elif name.endswith("_omega") or name == "omega":
            floor = _value(rules["wheel_speed_abs_rad_s"])
        elif name.endswith("_pitch"):
            floor = _value(rules["pitch_abs_rad"])
        else:
            floor = _value(rules["velocity_abs_m_s"])
        for statistic in ("peak_abs", "terminal"):
            _relative(checks, name+"_"+statistic, base_signals[name][statistic],
                      refined_signals[name][statistic], _value(rules["state_relative"]), floor)
    base_events = base["metrics"].get("event_times", {})
    refined_events = refined["metrics"].get("event_times", {})
    for name in sorted(set(base_events)|set(refined_events)):
        if name not in base_events or name not in refined_events:
            _add(checks, name+"_event_time", "missing", rules["event_time_abs_s"], False,
                 evidence=False)
        else:
            difference = abs(base_events[name]-refined_events[name])
            limit = _value(rules["event_time_abs_s"])
            _add(checks, name+"_event_time", difference, limit, difference <= limit)
    base_cumulative = base["metrics"].get("cumulative", {})
    refined_cumulative = refined["metrics"].get("cumulative", {})
    for name in sorted(set(base_cumulative)|set(refined_cumulative)):
        if name not in base_cumulative or name not in refined_cumulative:
            _add(checks, name+"_energy", "missing", "named tolerance", False, evidence=False)
        else:
            _relative(checks, name+"_energy", base_cumulative[name], refined_cumulative[name],
                      _value(rules["energy_relative"]), _value(rules["energy_abs_J"]))
    metrics.update({"base_metrics": base["metrics"], "refined_metrics": refined["metrics"]})
    return _finish(checks, metrics, diagnostics)


def compare(bundle: dict[str, Any], contract: dict[str, Any], case_id: str,
            base_csv: str | Path, refined_csv: str | Path, *,
            base_report=None, refined_report=None) -> dict[str, Any]:
    """Compare base/refined observations after independently verifying both."""
    reports=[]
    for path,report in ((base_csv,base_report),(refined_csv,refined_report)):
        if report is None:
            report=verify(bundle,contract,case_id,path)
        elif report['metrics'].get('csv_sha256')!=hashlib.sha256(Path(path).read_bytes()).hexdigest():
            raise _EvidenceProblem('reused_report_csv_hash_mismatch')
        reports.append(report)
    result=_compare_reports(contract,*reports)
    if any(r['status']!='PASS' for r in reports): return result
    stop=_stop_time(bundle,contract,case_id)
    required=_required_columns(bundle,case_id)
    a,b=(_read_csv(Path(p),required,stop,.001) for p in (base_csv,refined_csv))
    rules=contract['numerical_comparison']
    names=list(reports[0]['metrics'].get('signals',{}))
    for name in names:
        if name.endswith('_normal'): continue  # named peak-force comparison
        column='probeOmega' if name=='omega' else name
        if column not in a.columns: continue
        floor=(rules['position_abs_m'] if name.endswith(('_x','_z')) else
               rules['pitch_abs_rad'] if name.endswith('_pitch') else
               rules['wheel_speed_abs_rad_s'] if name.endswith('_omega') or name=='omega' else
               rules['velocity_abs_m_s'])
        # Linear interpolation at the union of both sampled timelines retains
        # post-event values at duplicated timestamps and catches interior drift.
        worst=0.0; max_error=0.0
        for t in sorted(set(a.time)|set(b.time)):
            av,bv=_interp(a,column,t),_interp(b,column,t)
            error=abs(av-bv); limit=max(floor,rules['state_relative']*max(abs(av),abs(bv)))
            worst=max(worst,error/limit); max_error=max(max_error,error)
        _add(result['checks'],name+'_aligned_state',{'max_error':max_error,'max_tolerance_ratio':worst},
             'named floor or 1% at every sampled time',worst<=1)
    return _finish(result['checks'],result['metrics'],result['diagnostics'])
