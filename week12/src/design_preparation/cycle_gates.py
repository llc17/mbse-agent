"""Conditional analytic gates; no simulation, optimizer or whole-car success.

The equations parameterize the reviewed 2026-10-08 full-cycle reasoning.
Lengths are metres, angles radians and loads newtons. Every applicability
assumption must be explicit; callers must bind it to the actual design.
"""
from __future__ import annotations

import math

SCHEMA_VERSION = "v11-design-cycle-gates-1"
CONTACT_TOLERANCE_M = 1e-9
_REPORT_LIMITS = [
    "Conditional necessary checks only; no continuous path or whole-vehicle success is proved.",
    "No load distribution, articulated-body equilibrium, motor/control, energy or dynamics is solved.",
    "Entry, exit, forward descent and repeated-step compatibility require separate analysis.",
    "The caller must verify the explicit assumptions against the actual design and scenario.",
]


class _Invalid(ValueError):
    pass


def _keys(value, required, optional=()):
    if not isinstance(value, dict):
        raise _Invalid("object required")
    if set(value) - set(required) - set(optional) or set(required) - set(value):
        raise _Invalid(f"expected fields {sorted(required)}; optional {sorted(optional)}")


def _number(value, name, *, minimum=None, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise _Invalid(f"{name}: finite number required; bool is not a number")
    if (minimum is not None and value < minimum) or (positive and value <= 0):
        raise _Invalid(f"{name}: outside supported domain")
    return value


def _assumptions(section, names):
    assumptions = section["assumptions"]
    _keys(assumptions, names)
    if any(type(assumptions[name]) is not bool for name in names):
        raise _Invalid("assumptions: each required assumption must be an explicit boolean")
    return dict(assumptions)


def _check(check_id, assumptions, inputs, limits):
    return {"id": check_id, "status": "REQUIRES_ANALYSIS", "assumptions": assumptions,
            "inputs": inputs, "derived": {}, "limits": limits}


def _fixed_main(section):
    fields = {"main_radius_m", "riser_m", "tread_m", "front_tread_index",
              "main_center_distance_m", "assumptions"}
    _keys(section, fields)
    assumptions = _assumptions(section, {"ideal_disks", "fixed_main_center_distance",
                                       "front_stays_same_tread_full_rear_arc"})
    inputs = {key: section[key] for key in fields - {"assumptions"}}
    radius = _number(inputs["main_radius_m"], "main_radius_m", positive=True)
    height = _number(inputs["riser_m"], "riser_m", positive=True)
    depth = _number(inputs["tread_m"], "tread_m", positive=True)
    k = inputs["front_tread_index"]
    if type(k) is not int or k < 1 or not math.isfinite(k):
        raise _Invalid("front_tread_index: integer >= 1 required")
    length = inputs["main_center_distance_m"]
    if length is not None:
        _number(length, "main_center_distance_m", positive=True)
    limits = [
        "Only ideal equal-radius main disks with 0 < riser <= main radius are supported.",
        "Front stays on the same kth horizontal tread through the entire rear first-corner arc.",
        "Endpoint interval intersection is necessary, not a sufficient full-arc path condition.",
        "A conflict excludes only this fixed-distance support mode; not articulation or high-demand-only staggering.",
        "Any effective center-distance variation bound is not a specified slider stroke.",
    ]
    result = _check("fixed_main_same_tread", assumptions, inputs, limits)
    if not all(assumptions.values()):
        result["derived"]["reason"] = "declared assumptions do not cover the fixed-distance full-arc mode"
        return result
    if height > radius:
        raise _Invalid("riser_m: H > R requires extended geometry; this formula cannot be applied")
    # R*sqrt((H/R)*(2-H/R)) avoids squaring very large/small radii.
    reach = radius * math.sqrt((height / radius) * (2 - height / radius))
    flat_width = depth - reach
    result["derived"] = {"corner_horizontal_reach_m": reach,
                         "front_flat_center_x_interval_m": [(k - 1) * depth, k * depth - reach],
                         "front_flat_center_window_width_m": flat_width}
    if flat_width < 0:
        result["status"] = "CONFLICT"
        result["derived"]["reason"] = "no horizontal-tread center window exists in the declared mode"
        result["derived"]["endpoint_L_intersection_m"] = None
        return result
    start = [math.hypot((k - 1) * depth + reach, k * height),
             math.hypot(k * depth, k * height)]
    end = [math.hypot((k - 1) * depth, (k - 1) * height),
           math.hypot(k * depth - reach, (k - 1) * height)]
    lo, hi = max(start[0], end[0]), min(start[1], end[1])
    intersection = [lo, hi] if lo <= hi else None
    selected_in_bounds = None if length is None else intersection is not None and lo <= length <= hi
    result["derived"].update({
        "rear_start_relative_corner_xz_m": [-reach, radius],
        "rear_end_relative_corner_xz_m": [0, height + radius],
        "start_L_interval_m": start, "end_L_interval_m": end,
        "endpoint_L_intersection_m": intersection,
        "selected_L_within_endpoint_intersection": selected_in_bounds,
        "necessary_effective_center_distance_variation_lower_bound_m": max(0.0, lo - hi),
    })
    result["status"] = ("CONFLICT" if intersection is None or selected_in_bounds is False
                        else "NECESSARY_CONDITIONS_ONLY")
    return result


def _nose(section):
    fields = {"nose_radius_m", "riser_m", "tread_m", "nose_center_x_m", "nose_center_z_m",
              "tread_start_x_m", "tread_height_m", "has_next_riser", "assumptions"}
    _keys(section, fields)
    assumptions = _assumptions(section, {"ideal_disk", "nose_remains_on_current_tread"})
    inputs = {key: section[key] for key in fields - {"assumptions"}}
    for key in fields - {"has_next_riser", "assumptions"}:
        _number(inputs[key], key, positive=key in {"nose_radius_m", "riser_m", "tread_m"})
    if type(inputs["has_next_riser"]) is not bool:
        raise _Invalid("has_next_riser: explicit boolean required")
    radius, height, depth = (inputs[key] for key in ("nose_radius_m", "riser_m", "tread_m"))
    x, z, start, level = (inputs[key] for key in ("nose_center_x_m", "nose_center_z_m",
                                                "tread_start_x_m", "tread_height_m"))
    result = _check("nose_next_riser", assumptions, inputs, [
        "Ideal disk posture geometry only; a tangent disk need not carry positive load or sufficient traction.",
        f"Current-tread contact residual must be <= {CONTACT_TOLERANCE_M} m before applying the bound.",
        "Current-tread entrance is a support bound, not a full entry-transition or ground-clearance proof.",
        "A terminal platform cannot establish repeating-step compatibility.",
        "A conflict excludes the declared current-tread posture, not transfer to another contact mode.",
    ])
    if not all(assumptions.values()):
        result["derived"]["reason"] = "declared assumptions do not cover a disk remaining on this tread"
        return result
    contact_residual = z - (level + radius)
    result["derived"] = {"current_tread_contact_residual_m": contact_residual,
                         "current_tread_center_lower_x_m": start}
    if abs(contact_residual) > CONTACT_TOLERANCE_M:
        result["derived"]["reason"] = "posture does not satisfy current-tread contact; bound not applied"
        return result
    entrance_ok = x >= start - CONTACT_TOLERANCE_M
    result["derived"]["current_tread_entrance_bound_satisfied"] = entrance_ok
    if not inputs["has_next_riser"]:
        result["derived"]["reason"] = "terminal platform has no next-riser bound; continuous cycle unproved"
        result["status"] = "REQUIRES_ANALYSIS" if entrance_ok else "CONFLICT"
        return result
    next_x, top_z = start + depth, level + height
    reach = radius if radius <= height else radius * math.sqrt((height / radius) * (2 - height / radius))
    bound = next_x - reach
    # Exact distance to the finite riser and to the following horizontal ray.
    nearest_riser_z = min(max(z, level), top_z)
    riser_distance = math.hypot(x - next_x, z - nearest_riser_z)
    upper_tread_distance = math.hypot(max(next_x - x, 0.0), z - top_z)
    disk_clear = (x <= next_x + CONTACT_TOLERANCE_M
                  and riser_distance >= radius - CONTACT_TOLERANCE_M
                  and upper_tread_distance >= radius - CONTACT_TOLERANCE_M)
    bound_ok = x <= bound + CONTACT_TOLERANCE_M
    result["derived"].update({
        "next_riser_x_m": next_x, "next_riser_vertical_extent_m": [level, top_z],
        "next_obstruction_mode": "vertical_riser" if radius <= height else "upper_corner",
        "next_riser_horizontal_reach_m": reach,
        "current_tread_center_x_interval_m": [start, bound],
        "next_riser_center_limit_x_m": bound,
        "next_riser_bound_violation_m": max(0.0, x - bound),
        "disk_distance_to_next_riser_segment_m": riser_distance,
        "disk_distance_to_next_upper_tread_ray_m": upper_tread_distance,
        "disk_clear_of_next_step": disk_clear,
    })
    result["status"] = ("NECESSARY_CONDITIONS_ONLY" if entrance_ok and bound_ok and disk_clear
                        else "CONFLICT")
    return result


def _corner(section):
    _keys(section, {"beta_rad", "friction_coefficient", "assumptions"}, {"normal_load_N"})
    assumptions = _assumptions(section, {"single_corner_contact", "coulomb_friction"})
    inputs = {key: section[key] for key in section if key != "assumptions"}
    beta = _number(inputs["beta_rad"], "beta_rad", minimum=0)
    mu = _number(inputs["friction_coefficient"], "friction_coefficient", minimum=0)
    if beta > math.pi / 2:
        raise _Invalid("beta_rad: only uphill first-corner arc 0 <= beta <= pi/2 is supported")
    if "normal_load_N" in inputs:
        _number(inputs["normal_load_N"], "normal_load_N", minimum=0)
    result = _check("corner_traction", assumptions, inputs, [
        "One ideal uphill corner contact with N >= 0 and abs(T) <= mu*N.",
        "A negative Fx upper bound limits this contact; other wheels may supply compensating forward traction.",
        "A nonnegative upper bound proves neither actual traction nor whole-car success.",
        "Motor torque, rolling resistance, inertia, other contacts and body/moment balances remain unchecked.",
    ])
    if not all(assumptions.values()):
        result["derived"]["reason"] = "declared assumptions do not cover a single Coulomb corner contact"
        return result
    cosine = 0.0 if beta == math.pi / 2 else math.cos(beta)
    sine = math.sin(beta)
    threshold = math.atan2(1.0, mu)
    ratio = 0.0 if beta == threshold else -cosine + mu * sine
    derived = {"normal_basis_xz": [-cosine, sine], "uphill_tangent_basis_xz": [sine, cosine],
               "maximum_forward_force_per_normal_load": ratio,
               "nonnegative_forward_cone_beta_rad": threshold,
               "forward_force_possible": ratio > 0,
               "positive_forward_force_possible_at_positive_normal": ratio > 0,
               "contact_forward_traction_limited": ratio < 0}
    if "normal_load_N" in inputs:
        derived["maximum_forward_force_N"] = inputs["normal_load_N"] * ratio
    result["derived"] = derived
    result["status"] = "REQUIRES_ANALYSIS" if ratio < 0 else "NECESSARY_CONDITIONS_ONLY"
    return result


def _invalid(check_id, message):
    return {"id": check_id, "status": "INVALID", "assumptions": {}, "inputs": {},
            "derived": {"reason": message}, "limits": ["No formula applied to invalid input."]}


def _all_finite(value):
    if isinstance(value, dict):
        return all(_all_finite(v) for v in value.values())
    if isinstance(value, list):
        return all(_all_finite(v) for v in value)
    return not isinstance(value, float) or math.isfinite(value)


def evaluate_cycle_constraints(intent: dict) -> dict:
    """Return a strict-JSON report; never returns PASS or physical feasibility.

    Optional sections are fixed_main_same_tread, nose_next_riser and
    corner_traction. Present sections require all their documented fields and
    explicit assumption booleans. Empty input has no applicable evidence.
    Unsupported domains and malformed input return INVALID without raising.
    """
    handlers = {"fixed_main_same_tread": _fixed_main, "nose_next_riser": _nose,
                "corner_traction": _corner}
    report = {"schema_version": SCHEMA_VERSION, "status": "REQUIRES_ANALYSIS",
              "checks": [], "limits": list(_REPORT_LIMITS)}
    try:
        _keys(intent, set(), handlers)
    except _Invalid as exc:
        report["checks"].append(_invalid("intent", str(exc)))
        report["status"] = "INVALID"
        return report
    for name, handler in handlers.items():
        if name not in intent:
            continue
        try:
            check = handler(intent[name])
            if not _all_finite(check):
                raise _Invalid("derived arithmetic exceeds finite supported range")
        except (_Invalid, TypeError, ValueError, OverflowError) as exc:
            check = _invalid(name, str(exc))
        report["checks"].append(check)
    statuses = {check["status"] for check in report["checks"]}
    for status in ("INVALID", "CONFLICT", "REQUIRES_ANALYSIS", "NECESSARY_CONDITIONS_ONLY"):
        if status in statuses:
            report["status"] = status
            break
    return report
