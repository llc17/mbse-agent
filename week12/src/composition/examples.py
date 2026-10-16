"""Public synthetic structural examples; not approved cars or tuned physics.

Fixed nose uses the user-described topology only, not private/source-v1 numeric
answers. Both examples use this same contract vocabulary; the compiler never
branches on their names. This helper creates pending review inputs only.
"""
from __future__ import annotations

from copy import deepcopy

from .schema import GENERATOR_VERSION, SCHEMA_VERSION, load_library, library_digest, seal_design


def quantity(value, unit, source="ai_design_parameter"):
    return {"value": value, "unit": unit, "source": source}


def pose(x=0, z=0, pitch=0):
    return {"x": quantity(x, "m"), "z": quantity(z, "m"), "pitch": quantity(pitch, "rad")}


def port(kind="frame2d", x=0, z=0):
    return {"type": kind, "pose": pose(x, z)}


def component(cid, kind, count, mass, inertia, x, z, ports, **extra):
    return {"id": cid, "kind": kind, "component_version": "1-contract", "count": count,
            "mass": quantity(mass, "kg"), "inertia": quantity(inertia, "kg*m^2"),
            "pose": pose(x, z), "ports": ports, "source": "ai_design_parameter", **extra}


def make_example(topology):
    if topology not in {"fixed_nose", "rocker_bogie"}:
        raise ValueError("example must be fixed_nose or rocker_bogie")
    library = load_library()
    scenario = {}
    for name in ("ascent", "descent", "flat"):
        scenario[name] = {"speed": quantity(0.4, "m/s", "ai_assumption"),
                          "initial_speed": quantity(0, "m/s", "ai_assumption"),
                          "friction": quantity(0.6, "1", "ai_assumption"),
                          "platform_length": quantity(1.5, "m", "ai_assumption"),
                          "entry": {"ascent": "flat_before_first_riser", "descent": "top_before_first_riser", "flat": "level_ground"}[name],
                          "exit": "continue_cruise" if name == "flat" else "last_wheel_leaves_steps_then_continue_cruise"}
        if name != "flat":
            scenario[name].update(riser=quantity(0.15, "m", "ai_assumption"),
                                  tread=quantity(0.28, "m", "ai_assumption"),
                                  steps=quantity(20, "1", "ai_assumption"))
    bodies = [component("chassis", "rigid_body", 1, 8, 1, 0, 0.5,
                        {"payload": port(z=0.15)}, role="chassis"),
              component("payload", "rigid_body", 1, 20, 0.5, 0, 0.65,
                        {"mount": port()}, role="payload"),
              component("controller", "rigid_body", 1, 0.5, 0.01, 0, 0.55,
                        {"mount": port()}, role="controller")]
    bodies[1]["mass"]["source"] = "user_requirement"
    bodies[0]["ports"]["controller"] = port(z=0.05)
    wheels, joints, connections, drives = [], [], [], []

    def body(cid):
        return next(b for b in bodies if b["id"] == cid)

    def link(jid, kind, a, ap, b, bp, x, z, count=1):
        joints.append(component(jid, kind, count, 0, 0, x, z, {"a": port(), "b": port()},
                                dof=[] if kind == "fixed" else ["ry"], axis="y"))
        for side, endpoint in (("a", f"{a}.{ap}"), ("b", f"{b}.{bp}")):
            connections.append({"id": jid + "_" + side, "from": endpoint,
                                "to": jid + "." + side, "source": "ai_design_parameter"})

    link("payload_mount", "fixed", "chassis", "payload", "payload", "mount", 0, 0.65)
    link("controller_mount", "fixed", "chassis", "controller", "controller", "mount", 0, 0.55)
    if topology == "fixed_nose":
        carriers = [("rear", "chassis", -0.35, 0.24, True),
                    ("front", "chassis", 0.35, 0.24, True),
                    ("nose", "chassis", 0.65, 0.38, False)]
    else:
        bodies.append(component("rocker", "rigid_body", 2, 1.2, 0.04, 0, 0.42,
                                {"pivot": port(), "bogie": port(x=-0.30, z=-0.08)}, role="link"))
        bodies.append(component("bogie", "rigid_body", 2, 0.8, 0.025, -0.30, 0.34,
                                {"pivot": port()}, role="link"))
        body("chassis")["ports"]["rocker"] = port(z=-0.08)
        link("rocker_pivot", "passive_revolute", "chassis", "rocker", "rocker", "pivot", 0, 0.42, 2)
        link("bogie_pivot", "passive_revolute", "rocker", "bogie", "bogie", "pivot", -0.30, 0.34, 2)
        carriers = [("rear", "bogie", -0.55, 0.24, True),
                    ("middle", "bogie", -0.05, 0.24, True),
                    ("front", "rocker", 0.5, 0.24, True)]
    for wid, carrier, x, z, driven in carriers:
        wheels.append(component(wid, "tire_pair", 2, 0.9, 0.026, x, z,
                                {"hub": port(), **({"shaft": port("rotational1d")} if driven else {})},
                                radius=quantity(0.12 if wid == "nose" else 0.24, "m"), driven=driven))
        origin = body(carrier)["pose"]
        body(carrier)["ports"][wid] = port(x=x-origin["x"]["value"], z=z-origin["z"]["value"])
        link(wid + "_bearing", "wheel_revolute", carrier, wid, wid, "hub", x, z, 2)
        if driven:
            did = wid + "_drive"
            drives.append(component(did, "drive_pair", 2, 0.6, 0.001, x, z,
                                    {"shaft": port("rotational1d"), "dc": port("dc")}, mount_body=carrier,
                                    torque=quantity(18, "N*m"), power=quantity(100, "W"),
                                    voltage=quantity(24, "V"), efficiency=quantity(0.8, "1")))
            connections.extend([
                {"id": did + "_shaft", "from": did + ".shaft", "to": wid + ".shaft", "source": "ai_design_parameter"},
                {"id": did + "_dc", "from": "battery.dc", "to": did + ".dc", "source": "ai_design_parameter"},
            ])
    supply = component("battery", "dc_supply", 1, 4, 0.03, 0, 0.5, {"dc": port("dc")},
                       mount_body="chassis", voltage=quantity(24, "V"), power=quantity(800, "W"),
                       usable_energy=quantity(1000000, "J"))
    return seal_design({
        "schema_version": SCHEMA_VERSION, "design_id": "example_" + topology, "revision": "draft_1",
        "project_id": "composition_development", "baseline_version_id": "s3_a_contract_v1",
        "generator_version": GENERATOR_VERSION, "library_version": library["library_version"],
        "library_digest": library_digest(library),
        "representation": {"coordinates": "world_xz_pitch_y", "parameter_basis": "per_physical_instance",
                           "pair_motion": "synchronized_left_right", "aggregation": "multiply_by_count_once"},
        "requirements": {"profile": "V11", "payload_mass": quantity(20, "kg", "user_requirement"),
                         "descent_mode": "forward_front_wheels_first", "motion_scope": "symmetric_planar",
                         "exit_behavior": "continue_cruise", "wheel_type": "ordinary_tire"},
        "bodies": bodies, "wheels": wheels, "joints": joints, "drives": drives,
        "power_supply": supply, "connections": connections, "scenario": scenario,
        "provenance": {"origin": "synthetic_development_example",
                       "reference": "user-described fixed nose topology" if topology == "fixed_nose" else "public generic rocker-bogie topology",
                       "scope": "Structural fixture only; all dimensions are AI assumptions, not source-v1 parameters or approved vehicle designs."},
    })


def synthetic_receipt(design):
    """Test fixture only; production confirmation and generation reject it."""
    result = deepcopy(design)
    result["confirmation"] = {"origin": "synthetic_test", "human_confirmed": False,
                              "project_id": result["project_id"], "baseline_version_id": result["baseline_version_id"],
                              "design_digest": result["design_digest"], "confirmed_at": "test_fixture",
                              "scope": "contract_test"}
    return result
