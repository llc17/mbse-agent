"""Bind task facts, a pending A draft and conditional cycle checks to B mappings.

This is C development preparation, not the D/E product entry or an autonomous
vehicle designer. It reuses A structural IR and the existing generic B binding
preview; it never calls a frozen candidate generator or a solver. A successful
preparation is still missing a physical descriptor and verified full trajectory.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import math
from pathlib import Path

from week12.src.composition.compiler import compile_design
from week12.src.composition.schema import ROOT, canonical, digest, seal_design, validate_design
from week12.src.integration.preview import binding_preview
from .cycle_gates import evaluate_cycle_constraints

VERSION = "v11-design-preparation-1"
TASK_FIELDS = {"wheel_count", "payload_mass_kg", "stair_riser_m", "stair_tread_m",
               "stair_steps", "mission", "descent_mode"}
OPTIONAL_TASK_FIELDS = {"total_loaded_mass_kg"}
LIMITS = [
    "Conditional necessary checks are not a continuous path or force solution.",
    "B fixture acceptance and a binding preview do not validate a new vehicle.",
    "No Modelica/SysML executable source, confirmation or simulation is produced.",
    "Initial proposal synthesis, physical descriptor lowering and D/E acceptance remain open.",
]


def _diagnostic(code, path, message):
    return {"code": code, "path": path, "message": message}


def _positive(value):
    return (type(value) in (int, float) and math.isfinite(value) and value > 0)


def validate_task(task):
    """Concentrate missing user facts; do not invent wheel count or stair task."""
    if not isinstance(task, dict):
        return [_diagnostic("INVALID", "task", "task object required")]
    if any(type(k) is not str for k in task):
        return [_diagnostic("INVALID", "task", "JSON object keys must be strings")]
    issues = [_diagnostic("MISSING", "task." + k, "explicit task fact required")
              for k in sorted(TASK_FIELDS - task.keys())]
    issues += [_diagnostic("INVALID", "task." + k, "unknown task field")
               for k in sorted(task.keys() - TASK_FIELDS - OPTIONAL_TASK_FIELDS)]
    if "wheel_count" in task:
        n = task["wheel_count"]
        if type(n) is not int or n < 2 or n % 2:
            issues.append(_diagnostic("UNSUPPORTED", "task.wheel_count", "positive symmetric wheel pairs required"))
    for k in ("payload_mass_kg", "stair_riser_m", "stair_tread_m", "total_loaded_mass_kg"):
        if k in task and not _positive(task[k]):
            issues.append(_diagnostic("INVALID", "task." + k, "finite positive number required; bool is not a number"))
    if "stair_steps" in task and (type(task["stair_steps"]) is not int or task["stair_steps"] < 1):
        issues.append(_diagnostic("INVALID", "task.stair_steps", "positive integer required"))
    if "mission" in task:
        m = task["mission"]
        if (not isinstance(m, list) or not m or any(type(v) is not str or v not in {"ascent", "descent", "flat"} for v in m)
                or len(set(m)) != len(m)):
            issues.append(_diagnostic("INVALID", "task.mission", "explicit nonempty unique ascent/descent/flat list required"))
    if "descent_mode" in task and task["descent_mode"] != "forward_front_wheels_first":
        issues.append(_diagnostic("UNSUPPORTED", "task.descent_mode", "only forward front-main-first descent supported"))
    if _positive(task.get("payload_mass_kg")) and task["payload_mass_kg"] != 20:
        issues.append(_diagnostic("UNSUPPORTED", "task.payload_mass_kg", "V11 development profile requires 20 kg equipment payload"))
    if all(_positive(task.get(k)) for k in ("payload_mass_kg", "total_loaded_mass_kg")):
        if task["total_loaded_mass_kg"] <= task["payload_mass_kg"]:
            issues.append(_diagnostic("INVALID", "task.total_loaded_mass_kg", "loaded mass must include vehicle and payload"))
    return issues


def _rigid_groups(design):
    """Physical fixed-body connectivity, independent of names or case labels."""
    parent = {b["id"]: b["id"] for b in design["bodies"]}
    def root(key):
        while parent[key] != key:
            key = parent[key]
        return key
    peers = {}
    for c in design["connections"]:
        peers[c["from"]] = c["to"]
        peers[c["to"]] = c["from"]
    carriers = {}
    for j in design["joints"]:
        ids = [peers[j["id"] + "." + side].split(".")[0] for side in ("a", "b")]
        if j["kind"] == "fixed":
            parent[root(ids[0])] = root(ids[1])
        elif j["kind"] == "wheel_revolute":
            wheel = next(i for i in ids if i not in parent)
            carriers[wheel] = next(i for i in ids if i in parent)
    return {w: root(c) for w, c in carriers.items()}


def _bind_cycle(task, design, intent):
    """Derive radii, stairs, friction and fixed-distance topology from the draft.

    A phase pose is expressed relative to the first upper tread edge, not silently
    read as a runtime position. Angles and load estimates remain proposal inputs.
    """
    if not isinstance(intent, dict):
        return {}, [_diagnostic("INVALID", "cycle_intent", "object required")]
    if any(type(k) is not str for k in intent):
        return {}, [_diagnostic("INVALID", "cycle_intent", "JSON object keys must be strings")]
    allowed = {"fixed_main_same_tread", "nose_next_riser", "corner_traction"}
    issues = [_diagnostic("INVALID", "cycle_intent." + k, "unknown conditional check") for k in sorted(intent.keys() - allowed)]
    wheels = {w["id"]: w for w in design["wheels"]}
    groups = _rigid_groups(design)
    bound = {}
    for key, spec in intent.items():
        if key not in allowed:
            continue
        prefix = "cycle_intent." + key
        if not isinstance(spec, dict):
            issues.append(_diagnostic("INVALID", prefix, "object required"))
            continue
        assumptions = spec.get("assumptions")
        if not isinstance(assumptions, dict):
            issues.append(_diagnostic("MISSING", prefix + ".assumptions", "explicit contact-mode assumptions required"))
            continue
        s = deepcopy(spec)
        if key == "fixed_main_same_tread":
            fields = {"rear_wheel_id", "front_wheel_id", "front_tread_index", "assumptions"}
            if set(s) != fields or any(not isinstance(s.get(k), str) or s[k] not in wheels for k in ("rear_wheel_id", "front_wheel_id")):
                issues.append(_diagnostic("INVALID", prefix, "two known wheel IDs, tread index and assumptions required"))
                continue
            rear, front = (wheels[s[k]] for k in ("rear_wheel_id", "front_wheel_id"))
            if rear["id"] == front["id"] or rear["radius"]["value"] != front["radius"]["value"]:
                issues.append(_diagnostic("UNSUPPORTED", prefix, "distinct equal-radius wheels required by this conditional formula"))
                continue
            k = s["front_tread_index"]
            if type(k) is not int or not 1 <= k <= task["stair_steps"]:
                issues.append(_diagnostic("INVALID", prefix + ".front_tread_index", "index outside task stairs"))
                continue
            fixed = groups[rear["id"]] == groups[front["id"]]
            a = deepcopy(assumptions)
            # Never allow a caller's fixed/free claim to override actual topology.
            a["fixed_main_center_distance"] = fixed
            p, q = rear["pose"], front["pose"]
            L = math.hypot(q["x"]["value"]-p["x"]["value"], q["z"]["value"]-p["z"]["value"])
            bound[key] = {"main_radius_m": rear["radius"]["value"], "riser_m": task["stair_riser_m"],
                          "tread_m": task["stair_tread_m"], "front_tread_index": k,
                          "main_center_distance_m": L if fixed else None, "assumptions": a}
        else:
            wid = s.get("wheel_id")
            if not isinstance(wid, str) or wid not in wheels:
                issues.append(_diagnostic("INVALID", prefix + ".wheel_id", "known wheel required"))
                continue
            if key == "nose_next_riser":
                fields = {"wheel_id", "tread_index", "nose_center_x_m", "nose_center_z_m", "coordinate_frame", "assumptions"}
                if set(s) != fields or s["coordinate_frame"] != "relative_first_upper_edge":
                    issues.append(_diagnostic("INVALID", prefix, "explicit relative-first-upper-edge phase pose required"))
                    continue
                k = s["tread_index"]
                if type(k) is not int or not 1 <= k <= task["stair_steps"]:
                    issues.append(_diagnostic("INVALID", prefix + ".tread_index", "index outside task stairs"))
                    continue
                bound[key] = {"nose_radius_m": wheels[wid]["radius"]["value"],
                              "riser_m": task["stair_riser_m"], "tread_m": task["stair_tread_m"],
                              "nose_center_x_m": s["nose_center_x_m"], "nose_center_z_m": s["nose_center_z_m"],
                              "tread_start_x_m": (k-1)*task["stair_tread_m"], "tread_height_m": k*task["stair_riser_m"],
                              "has_next_riser": k < task["stair_steps"], "assumptions": deepcopy(assumptions)}
            else:
                fields = {"wheel_id", "beta_rad", "scenario", "assumptions"}
                if set(s)-fields-{"normal_load_N"} or fields-set(s) or s["scenario"] not in task["mission"]:
                    issues.append(_diagnostic("INVALID", prefix, "explicit task scenario and corner angle required"))
                    continue
                bound[key] = {"beta_rad": s["beta_rad"], "friction_coefficient": design["scenario"][s["scenario"]]["friction"]["value"],
                              "assumptions": deepcopy(assumptions)}
                if "normal_load_N" in s:
                    bound[key]["normal_load_N"] = s["normal_load_N"]
    return bound, issues


def _task_matches(task, design, ir):
    issues = []
    actual = {"wheel_count": sum(w["count"] for w in design["wheels"]),
              "payload_mass_kg": ir["mass_accounting"]["payload_kg"],
              "descent_mode": design["requirements"]["descent_mode"]}
    if "total_loaded_mass_kg" in task:
        actual["total_loaded_mass_kg"] = ir["mass_accounting"]["total_loaded_mass_kg"]
    for k, v in actual.items():
        same = math.isclose(task[k], v, rel_tol=0, abs_tol=1e-9) if type(v) in (int, float) else task[k] == v
        if not same:
            issues.append(_diagnostic("TASK_MISMATCH", "task." + k, "draft differs from explicit task"))
    for name in task["mission"]:
        if name != "flat":
            for dk, tk in (("riser", "stair_riser_m"), ("tread", "stair_tread_m"), ("steps", "stair_steps")):
                if design["scenario"][name][dk]["value"] != task[tk]:
                    issues.append(_diagnostic("TASK_MISMATCH", "design.scenario." + name + "." + dk, "draft differs from explicit task"))
    return issues


def _physical_gaps(design):
    """Explicit missing executable contract; no default force law or controller."""
    return {
        "status": "DESCRIPTOR_AND_BACKEND_REQUIRED",
        "wheel_contacts": {w["id"]: ["versioned_contact_implementation", "stiffness_and_damping_per_wheel",
                                        "friction_and_regularization", "rolling_resistance", "terrain_feature_ownership"] for w in design["wheels"]},
        "passive_joints": {j["id"]: ["constitutive_law_and_parameters", "relative_angle_domain", "initial_state_and_force_balance"]
                           for j in design["joints"] if j["kind"] == "passive_revolute"},
        "controls": {d["id"]: ["relative_vs_absolute_speed_definition", "control_law_and_limits", "reference_schedule"] for d in design["drives"]},
        "shared_supply": ["auxiliary_power", "continuous_and_peak_limits", "one_shared_energy_state"],
        "whole_vehicle": ["nonwheel_envelopes_and_self_collision_scope", "initial_conditions",
                          "continuous_contact_and_load_path", "per_body_force_moment_and_joint_balance",
                          "entry_and_exit_transitions", "forward_descent_braking_and_order", "frozen_verification_and_finite_budget"],
    }


def prepare_design(task, draft, cycle_intent=None):
    """Return deterministic pending preparation for any supported A graph.

    Invalid facts/structures fail before B reads. No approval or execution method
    is exposed. Changed task, phase assumptions, source or draft changes identity.
    """
    task, draft, intent = deepcopy(task), deepcopy(draft), deepcopy({} if cycle_intent is None else cycle_intent)
    report = {"schema_version": VERSION, "status": "NEEDS_DESIGN_ANALYSIS", "executable": False,
              "approval_status": "PENDING_REVIEW", "task": task, "diagnostics": [], "limits": list(LIMITS),
              "execution": {"simulations": 0, "SysML": 0, "product_online": 0}}
    try:
        canonical({"task": task, "draft": draft, "cycle_intent": intent})
    except (ValueError, TypeError, OverflowError):
        # Do not echo nonfinite, unserializable input into JSON artifacts.
        report["task"] = None
        report["diagnostics"] = [_diagnostic("INVALID", "input", "finite JSON-compatible inputs required")]
        report["status"] = "INVALID_INPUT"
        report["preparation_digest"] = digest(report)
        return report
    issues = validate_task(task)
    if issues:
        report["status"] = "NEEDS_TASK_FACTS" if any(i["code"] == "MISSING" for i in issues) else "INVALID_INPUT"
        report["diagnostics"] = issues
        report["preparation_digest"] = digest(report)
        return report
    if draft is None:
        report["status"] = "NEEDS_AGENT_DESIGN"
        report["diagnostics"] = [_diagnostic("MISSING", "draft", "Agent must supply a supported structured design; user need not supply mechanical answers")]
        report["preparation_digest"] = digest(report)
        return report
    issues = validate_design(draft)
    if issues:
        report["status"] = "INVALID_DESIGN"
        report["diagnostics"] = issues
        report["preparation_digest"] = digest(report)
        return report
    pending = seal_design(draft)
    compiled = compile_design(pending, purpose="structure_preview")
    ir = compiled["artifacts"]["structure.ir.json"]
    report["diagnostics"] = _task_matches(task, pending, ir)
    if report["diagnostics"]:
        report["status"] = "TASK_DESIGN_MISMATCH"
        report["preparation_digest"] = digest(report)
        return report
    bound, issues = _bind_cycle(task, pending, intent)
    report["diagnostics"] = issues
    if issues:
        report["status"] = "INVALID_CYCLE_INTENT"
        report["preparation_digest"] = digest(report)
        return report
    gates = evaluate_cycle_constraints(bound)
    report.update(design=pending, design_digest=pending["design_digest"], cycle_intent=intent,
                  bound_cycle_inputs=bound, cycle_checks=gates, structure_ir=ir,
                  physical_descriptor_contract=_physical_gaps(pending))
    if gates["status"] == "INVALID":
        report["status"] = "INVALID_CYCLE_INTENT"
    elif gates["status"] == "CONFLICT":
        report["status"] = "BLOCKED_CONTACT_MODE"
    # Mapping is useful preparation even when a contact mode is impossible; it
    # must never erase that failure or claim an executable backend.
    try:
        report["component_binding_preview"] = binding_preview(pending)
    except (ValueError, OSError) as exc:
        report["diagnostics"].append(_diagnostic("BINDING_BLOCKED", "component_binding", str(exc)))
        if report["status"] == "NEEDS_DESIGN_ANALYSIS":
            report["status"] = "BINDING_BLOCKED"
    report["source_sha256"] = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (Path(__file__), Path(__file__).with_name("cycle_gates.py"),
                  ROOT/"src/integration/preview.py", ROOT/"src/composition/schema.py", ROOT/"src/composition/compiler.py")
    }
    report["preparation_digest"] = digest(report)
    return report


def write_preparation(report, directory):
    """Exclusive, non-executable JSON evidence under week12/results only."""
    if not isinstance(report, dict) or report.get("schema_version") != VERSION or report.get("executable") is not False:
        raise ValueError("INVALID: preparation report required")
    old = report.get("preparation_digest")
    raw = {k: v for k, v in report.items() if k != "preparation_digest"}
    if not old or digest(raw) != old:
        raise ValueError("STALE: preparation content changed")
    target = Path(directory).resolve()
    base = (ROOT/"results").resolve()
    if target == base or not target.is_relative_to(base):
        raise ValueError("INVALID: new output directory must be inside week12/results")
    encoded = canonical(report)
    manifest = canonical({"schema_version": VERSION, "files": {"preparation.json": hashlib.sha256(encoded).hexdigest()},
                          "preparation_digest": old, "executable": False})
    target.mkdir(parents=False, exist_ok=False)
    (target/"preparation.json").write_bytes(encoded)
    (target/"manifest.json").write_bytes(manifest)
    return {"status": report["status"], "directory": str(target), "preparation_digest": old, "executable": False}
