"""Executable Modelica/SysML lowering for the approved V11 S3-C vehicle.

This backend is deliberately narrow: it accepts the frozen fixed-body vehicle,
its seven cases, and the two frozen C0 probes.  It generates source only and
never invokes SysML, OpenModelica, a simulator, or an online service.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import math
from pathlib import Path
import re

from week12.src.composition.schema import digest, load_library, require_valid
from week12.src.component_physics.contract import corner_kinds, terrain_segments
from .output_contract import probe_output_names, variable_filter, vehicle_output_names


VERSION = "v11-c-executable-generator-2-timing"
MODEL_CLASS = "V11CGenerated.Scene"
ROOT = Path(__file__).resolve().parents[2]
B_SOURCE = ROOT / "models/components/V11PhysicalB1/package.mo"
SPECIFICATION_DIGEST = "aee33eb72f551dd60cd1652f48a93b054bc5f36444f74f04cd9db25bf833ded9"
DESIGN_DIGEST = "db1e982c9ff58d336db083881a1b7a7c76cbaf83932fb76a2d318784d9663d4a"
PHYSICAL_DIGEST = "8bb4c94a71f2668ef34075f5d52d5042759e87eec6a9871432602e9502ada0cb"
VERIFICATION_DIGEST = "dd012243eff16af605cf79b822bab3b8787e48e6ccd2b1de5760547fa8edd773"
B_SOURCE_SHA256 = "0fbf2e80bb4e43f1a5bfcf382c42c8b4837f32b83628e17fc2e2f167bd8d774e"
PROBES = ("shared_supply_probe", "rolling_resistance_probe")
NO_RESET = (
    "time", "position", "attitude", "wheel_speed", "controller_state",
    "battery_energy", "contact_energy",
)


def _ident(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is None:
        raise ValueError("UNSUPPORTED: identifier")
    return value


def _f(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("INVALID: finite numeric input required")
    result = format(float(value), ".17g")
    return result if any(c in result for c in ".eE") else result + ".0"


def _array(values) -> str:
    return "{" + ",".join(_f(value) for value in values) + "}"


def _digest_without(record, field: str) -> str:
    value = deepcopy(record)
    value.pop(field, None)
    return digest(value)


def _validate_bundle(bundle):
    if not isinstance(bundle, dict) or set(bundle) != {"design", "physical", "binding", "reuse_static_reference"}:
        raise ValueError("INVALID: frozen proposal bundle required")
    design, physical, binding = bundle["design"], bundle["physical"], bundle["binding"]

    # Wheel revolutes are absorbed exactly once by WheelPair.  Every other
    # structural joint in this backend must be fixed.
    for joint in design.get("joints", []):
        if joint.get("kind") not in {"fixed", "wheel_revolute"}:
            raise ValueError("UNSUPPORTED: fixed-only C backend")
    require_valid(design, load_library())
    if design.get("design_digest") != DESIGN_DIGEST:
        raise ValueError("STALE: design digest is outside the approved specification")
    if physical.get("physical_digest") != PHYSICAL_DIGEST or _digest_without(physical, "physical_digest") != PHYSICAL_DIGEST:
        raise ValueError("STALE: physical description changed")
    if binding.get("design_digest") != DESIGN_DIGEST or binding.get("accepted_b_source_sha256") != B_SOURCE_SHA256:
        raise ValueError("STALE: binding preview changed")
    if hashlib.sha256(B_SOURCE.read_bytes()).hexdigest() != B_SOURCE_SHA256:
        raise ValueError("STALE: accepted B source changed")
    if any(not wheel.get("driven") for wheel in design["wheels"]):
        raise ValueError("UNSUPPORTED: approved vehicle requires every wheel pair driven")
    if set(wheel["id"] for wheel in design["wheels"]) != set(drive["id"].removesuffix("_drive") for drive in design["drives"]):
        raise ValueError("UNSUPPORTED: one direct drive per wheel pair required")
    return design, physical, binding


def _speed_expression(table) -> str:
    if len(table) < 2:
        raise ValueError("INVALID: reference speed table")
    points = [(float(point[0]), float(point[1])) for point in table]
    if points[0][0] != 0 or any(b[0] <= a[0] for a, b in zip(points, points[1:])):
        raise ValueError("INVALID: reference speed times")
    expression = _f(points[-1][1])
    for (t0, v0), (t1, v1) in reversed(list(zip(points, points[1:]))):
        ramp = f"{_f(v0)} + (time - {_f(t0)})*({_f(v1)} - {_f(v0)})/({_f(t1)} - {_f(t0)})"
        expression = f"if time < {_f(t1)} then ({ramp}) else ({expression})"
    return f"if time <= {_f(points[0][0])} then {_f(points[0][1])} else ({expression})"


def _rotation(name: str, data: dict) -> str:
    r = data["r_m"]
    return (
        f"  MB.Parts.FixedRotation {name}(r={{{_f(r[0])},0,{_f(r[2])}}}, "
        f"n={{0,-1,0}}, angle={_f(data['angle_deg'])}, animation=false);"
    )


def _pitch(frame: str) -> str:
    return f"Frames.planarRotationAngle({{0,-1,0}},{{1,0,0}},Frames.resolve2({frame}.R,{{1,0,0}}))"


def _vehicle_model(design, physical, binding, case) -> str:
    contact = physical["contact_per_physical_wheel"]
    supply = physical["supply"]
    controller = physical["controller"]
    points = case["terrain_points"]
    terrain_segments(points)
    kinds = [0, *({"convex": 1, "concave": -1, "collinear": 0}[row["kind"]]
                   for row in corner_kinds(points)), 0]
    xs, zs = zip(*points)
    root_pose = binding["root"]["initial_pose"]
    root_z = root_pose[1] + case["initial_world_translation_z_m"]
    components = {item["id"]: item for item in [
        *design["bodies"], *design["wheels"], *design["drives"], design["power_supply"]
    ]}
    bodies = {item["input_id"]: item for item in binding["bodies"]}
    wheels = {item["input_id"]: item for item in binding["wheels"]}
    mounts = {item["input_id"]: item for item in binding["hardware_mass_mounts"]}
    drives = {item["input_id"]: item for item in binding["drives"]}

    lines = [
        "within;",
        "package V11CGenerated",
        "  import SI = Modelica.Units.SI;",
        "  import MB = Modelica.Mechanics.MultiBody;",
        "  import Rot = Modelica.Mechanics.Rotational;",
        "  import Frames = Modelica.Mechanics.MultiBody.Frames;",
        "  model WheelPairWithTerrainPort",
        "    extends V11PhysicalB1.WheelPair;",
        "    MB.Interfaces.Frame_b terrainTorqueFrame;",
        "  equation",
        "    connect(wheelBody.frame, terrainTorqueFrame);",
        "  end WheelPairWithTerrainPort;",
        "  model Scene",
        f"    // case_id={case['id']}; specification_digest={SPECIFICATION_DIGEST}",
        "    inner MB.World world(gravityType=MB.Types.GravityTypes.UniformGravity,",
        f"      g={_f(physical['gravity_m_s2'])}, n={{0,0,-1}}, enableAnimation=false);",
        "    MB.Joints.Planar root(n={0,-1,0}, n_x={1,0,0}, animation=false,",
        f"      s_x(start={_f(root_pose[0])}, fixed=true),",
        f"      s_y(start={_f(root_z)}, fixed=true),",
        f"      phi(start={_f(root_pose[2])}, fixed=true),",
        "      v_x(start=0, fixed=true), v_y(start=0, fixed=true),",
        "      w(start=0, fixed=true), stateSelect=StateSelect.always);",
    ]
    for item in design["bodies"]:
        lowered = bodies[item["id"]]
        lines.append(
            f"    V11PhysicalB1.RigidBody2D body_{_ident(item['id'])}(m={_f(lowered['m_kg'])}, "
            f"J={_f(lowered['J_kg_m2'])});"
        )
    for item in design["wheels"]:
        lowered = wheels[item["id"]]
        lines.append(
            f"    WheelPairWithTerrainPort wheel_{_ident(item['id'])}(m={_f(lowered['m_kg'])}, "
            f"J={_f(lowered['J_kg_m2'])}, R={_f(item['radius']['value'])}, "
            f"k={_f(item['count']*contact['stiffness_N_m'])}, "
            f"c={_f(item['count']*contact['damping_Ns_m'])}, mu={_f(contact['friction'])}, "
            f"vreg={_f(contact['vreg_m_s'])}, nPoints={len(points)}, xPoints={_array(xs)}, "
            f"zPoints={_array(zs)}, vertexKind={{{','.join(str(v) for v in kinds)}}}, "
            "phi0=0, omega0=0);"
        )
    for item in [*design["drives"], design["power_supply"]]:
        lowered = mounts[item["id"]]
        lines.append(
            f"    V11PhysicalB1.RigidBody2D body_{_ident(item['id'])}(m={_f(lowered['m_kg'])}, "
            f"J={_f(lowered['J_kg_m2'])});"
        )
    for item in design["drives"]:
        pair_torque = item["count"] * item["torque"]["value"]
        pair_power = item["count"] * item["power"]["value"]
        lines.extend([
            f"    V11PhysicalB1.IdealDriveSupply drive_{_ident(item['id'])}(",
            f"      ratedTorque={_f(pair_torque)}, ratedMechanicalPower={_f(pair_power)},",
            f"      voltage={_f(item['voltage']['value'])}, efficiency={_f(item['efficiency']['value'])},",
            f"      supplyPower={_f(supply['continuous_power_W'])}, initialEnergy={_f(supply['usable_energy_J'])});",
        ])
    for item in design["wheels"]:
        ident = _ident(item["id"])
        lines.append(
            f"    MB.Forces.WorldTorque rollingWorld_{ident}(resolveInFrame=MB.Types.ResolveInFrameB.world, animation=false);"
        )
        lines.extend([
            f"    SI.Torque command_{ident};",
            f"    SI.Torque capPerWheel_{ident};",
            f"    SI.Torque rollingTorque_{ident};",
            f"    SI.Power rollingLoss_{ident};",
        ])
    for joint in binding["joints"]:
        if joint["kind"] != "fixed":
            raise ValueError("UNSUPPORTED: fixed-only C backend")
        ident = _ident(joint["input_joint"])
        lines.append(_rotation(f"mount_{ident}_a", joint["a_to_joint"]).replace("  MB", "    MB", 1))
        lines.append(_rotation(f"mount_{ident}_b", joint["joint_to_b"]).replace("  MB", "    MB", 1))
    for item in design["wheels"]:
        ident = _ident(item["id"])
        lines.append(_rotation(f"mount_wheel_{ident}", wheels[item["id"]]["mount_and_bearing"]["mount"]).replace("  MB", "    MB", 1))
    for item in [*design["drives"], design["power_supply"]]:
        ident = _ident(item["id"])
        lines.append(_rotation(f"mount_hardware_{ident}", mounts[item["id"]]["mount"]).replace("  MB", "    MB", 1))

    mass_items = [*design["bodies"], *design["wheels"], *design["drives"], design["power_supply"]]
    for item in mass_items:
        ident = _ident(item["id"])
        for suffix in ("x", "z", "vx", "vz", "pitch", "pitchRate"):
            lines.append(f"    output Real body_{ident}_{suffix};")
    for item in design["wheels"]:
        ident = _ident(item["id"])
        for suffix in ("featureId", "contactCount"):
            lines.append(f"    output Integer wheel_{ident}_{suffix};")
        for suffix in (
            "omega", "omegaRelative", "Fx", "Fz", "normal", "penetration", "slip",
            "elasticEnergy", "normalLoss", "frictionLoss", "rollingTorque", "rollingLoss",
            "torque", "shaftPower", "dcPower", "driveLoss",
        ):
            lines.append(f"    output Real wheel_{ident}_{suffix};")
    lines.extend([
        "    output Real chassisPitch;",
        "    output Real chassisPitchRate;",
        "    output Real busPower;",
        f"    output Real busEnergy(start={_f(supply['usable_energy_J'])}, fixed=true);",
        "    output Real busCurrent;",
        f"    output Real auxPower = {_f(supply['auxiliary_W'])};",
        "    output Real refSpeed;",
        "  equation",
        "    connect(world.frame_b, root.frame_a);",
        "    connect(root.frame_b, body_chassis.frame);",
    ])
    for joint in binding["joints"]:
        ident = _ident(joint["input_joint"])
        a, b = _ident(joint["body_a"]), _ident(joint["body_b"])
        lines.extend([
            f"    connect(body_{a}.frame, mount_{ident}_a.frame_a);",
            f"    connect(mount_{ident}_a.frame_b, mount_{ident}_b.frame_a);",
            f"    connect(mount_{ident}_b.frame_b, body_{b}.frame);",
        ])
    for item in design["wheels"]:
        ident = _ident(item["id"])
        carrier = _ident(wheels[item["id"]]["mount_and_bearing"]["carrier"])
        lines.extend([
            f"    connect(body_{carrier}.frame, mount_wheel_{ident}.frame_a);",
            f"    connect(mount_wheel_{ident}.frame_b, wheel_{ident}.carrier);",
        ])
    for item in [*design["drives"], design["power_supply"]]:
        ident, parent = _ident(item["id"]), _ident(item["mount_body"])
        lines.extend([
            f"    connect(body_{parent}.frame, mount_hardware_{ident}.frame_a);",
            f"    connect(mount_hardware_{ident}.frame_b, body_{ident}.frame);",
        ])
    lines.append(f"    refSpeed = {_speed_expression(case['reference_speed_table_s_m_s'])};")
    drive_for_wheel = {row["wheel_id"]: row["input_id"] for row in drives.values()}
    for item in design["wheels"]:
        ident = _ident(item["id"])
        drive_id = _ident(drive_for_wheel[item["id"]])
        drive_item = components[drive_id]
        lines.extend([
            f"    connect(drive_{drive_id}.shaft, wheel_{ident}.axis);",
            f"    connect(drive_{drive_id}.support, wheel_{ident}.support);",
            f"    capPerWheel_{ident} = min({_f(drive_item['torque']['value'])}, "
            f"{_f(drive_item['power']['value'])}/max(abs(drive_{drive_id}.omegaRelative),1.0));",
            # The explicit time gate is required: v_ref=0 alone would apply
            # feedback braking when a wheel spins during free settlement.
            f"    command_{ident} = if time < {_f(controller['settle_until_s'])} then 0 else "
            f"{item['count']}*max(-capPerWheel_{ident}, min(capPerWheel_{ident}, "
            f"{_f(controller['kp_Nm_per_rad_s_per_wheel'])}*(refSpeed/{_f(item['radius']['value'])} - wheel_{ident}.omega)));",
            f"    drive_{drive_id}.torqueCommand = command_{ident};",
            f"    rollingTorque_{ident} = -{_f(physical['rolling_resistance']['coefficient'])}*"
            f"{_f(item['radius']['value'])}*wheel_{ident}.contact.normal*"
            f"tanh(wheel_{ident}.omega/{_f(physical['rolling_resistance']['omega_regularization_rad_s'])});",
            f"    rollingLoss_{ident} = -rollingTorque_{ident}*wheel_{ident}.omega;",
            f"    rollingWorld_{ident}.torque = {{0,rollingTorque_{ident},0}};",
            f"    connect(rollingWorld_{ident}.frame_b, wheel_{ident}.terrainTorqueFrame);",
            f"    assert(rollingLoss_{ident} >= -1e-10, \"rolling loss must be nonnegative\");",
        ])
    dc_sum = " + ".join(f"drive_{_ident(item['id'])}.dcPower" for item in design["drives"])
    lines.extend([
        f"    busPower = {dc_sum} + auxPower;",
        "    der(busEnergy) = -busPower;",
        f"    busCurrent = busPower/{_f(supply['voltage_V'])};",
        f"    assert(busPower <= {_f(supply['continuous_power_W'])} + 1e-8, \"shared bus power exceeded\");",
        f"    assert(busCurrent <= {_f(supply['continuous_current_A'])} + 1e-8, \"shared bus current exceeded\");",
        "    assert(busEnergy >= -1e-8, \"shared bus energy exhausted\");",
        "    chassisPitch = root.phi;",
        "    chassisPitchRate = root.w;",
    ])
    for item in mass_items:
        ident = _ident(item["id"])
        instance = f"wheel_{ident}.wheelBody" if item["kind"] == "tire_pair" else f"body_{ident}"
        lines.extend([
            f"    body_{ident}_x = {instance}.x;",
            f"    body_{ident}_z = {instance}.z;",
            f"    body_{ident}_vx = {instance}.vx;",
            f"    body_{ident}_vz = {instance}.vz;",
            f"    body_{ident}_pitch = {_pitch(instance + '.frame')};",
            f"    body_{ident}_pitchRate = {instance}.pitchRate;",
        ])
    for item in design["wheels"]:
        ident = _ident(item["id"])
        drive_id = _ident(drive_for_wheel[item["id"]])
        mapping = {
            "omega": f"wheel_{ident}.omega",
            "omegaRelative": f"wheel_{ident}.omegaRelative",
            "Fx": f"wheel_{ident}.contact.Fx", "Fz": f"wheel_{ident}.contact.Fz",
            "normal": f"wheel_{ident}.contact.normal",
            "penetration": f"wheel_{ident}.contact.penetration",
            "slip": f"wheel_{ident}.contact.slip",
            "elasticEnergy": f"wheel_{ident}.contact.elasticEnergy",
            "normalLoss": f"wheel_{ident}.contact.normalLoss",
            "frictionLoss": f"wheel_{ident}.contact.frictionLoss",
            "featureId": f"wheel_{ident}.contact.featureId",
            "contactCount": f"wheel_{ident}.contact.contactCount",
            "rollingTorque": f"rollingTorque_{ident}", "rollingLoss": f"rollingLoss_{ident}",
            "torque": f"command_{ident}", "shaftPower": f"drive_{drive_id}.shaftPower",
            "dcPower": f"drive_{drive_id}.dcPower", "driveLoss": f"drive_{drive_id}.driveLoss",
        }
        lines.extend(f"    wheel_{ident}_{name} = {source};" for name, source in mapping.items())
    lines.extend([
        "    annotation(experiment(StartTime=0), uses(Modelica(version=\"4.0.0\")));",
        "  end Scene;",
        "end V11CGenerated;",
        "",
    ])
    return "\n".join(lines)


def _probe_outputs() -> list[str]:
    return [f"    output Real {name};" for name in probe_output_names() if name != "time"]


def _shared_supply_probe() -> str:
    lines = [
        "within;", "package V11CGenerated", "  model Scene",
        "    import Rot = Modelica.Mechanics.Rotational;",
        "    V11PhysicalB1.IdealDriveSupply drive1(ratedTorque=1, ratedMechanicalPower=20, voltage=48, efficiency=0.8, supplyPower=50, initialEnergy=100);",
        "    V11PhysicalB1.IdealDriveSupply drive2(ratedTorque=2, ratedMechanicalPower=30, voltage=48, efficiency=0.8, supplyPower=50, initialEnergy=100);",
        "    Rot.Sources.ConstantSpeed speed1(w_fixed=-10);",
        "    Rot.Sources.ConstantSpeed speed2(w_fixed=-10);",
        "    Rot.Components.Fixed support1;",
        "    Rot.Components.Fixed support2;",
        "    Real electricalSpent(start=0, fixed=true);",
        "    " + "\n    ".join(line.strip() for line in _probe_outputs()),
        "  equation",
        "    connect(drive1.shaft, speed1.flange);",
        "    connect(drive2.shaft, speed2.flange);",
        "    connect(drive1.support, support1.flange);",
        "    connect(drive2.support, support2.flange);",
        "    drive1.torqueCommand = 1;",
        "    drive2.torqueCommand = 2;",
        "    probeOmega = drive1.omegaRelative;",
        "    probeShaftPower = drive1.shaftPower + drive2.shaftPower;",
        "    probeDcPower = drive1.dcPower + drive2.dcPower;",
        "    probeDriveLoss = drive1.driveLoss + drive2.driveLoss;",
        "    probeBusPower = probeDcPower + 5;",
        "    der(probeBusEnergy) = -probeBusPower;",
        "    probeBusCurrent = probeBusPower/48;",
        "    der(electricalSpent) = probeBusPower;",
        "    probeElectricalEnergyResidual = 100 - probeBusEnergy - electricalSpent;",
        "    probeLoss = probeDriveLoss;",
        "    probeKinetic = 0;",
        "    probeRollingTorque = 0;",
        "    probeRollingLoss = 0;",
        "    probeMechanicalEnergyResidual = 0;",
        "    annotation(experiment(StartTime=0, StopTime=2), uses(Modelica(version=\"4.0.0\")));",
        "  end Scene;", "end V11CGenerated;", "",
    ]
    # A public output state needs its approved initial value on the declaration.
    return "\n".join(lines).replace("output Real probeBusEnergy;", "output Real probeBusEnergy(start=100, fixed=true);")


def _rolling_probe() -> str:
    lines = [
        "within;", "package V11CGenerated", "  model Scene",
        "    import Rot = Modelica.Mechanics.Rotational;",
        "    Rot.Components.Inertia rotor(J=0.01, phi(start=0, fixed=true), w(start=-10, fixed=true), stateSelect=StateSelect.always);",
        "    Rot.Sources.Torque terrainTorque(useSupport=true);",
        "    Rot.Components.Fixed ground;",
        "    Real rollingDissipated(start=0, fixed=true);",
        "    " + "\n    ".join(line.strip() for line in _probe_outputs()),
        "  equation",
        "    connect(terrainTorque.flange, rotor.flange_a);",
        "    connect(terrainTorque.support, ground.flange);",
        "    probeOmega = -rotor.w;",
        "    probeRollingTorque = -0.05*0.1*10*tanh(probeOmega/0.1);",
        "    terrainTorque.tau = -probeRollingTorque;",
        "    probeRollingLoss = -probeRollingTorque*probeOmega;",
        "    der(rollingDissipated) = probeRollingLoss;",
        "    probeKinetic = 0.5*0.01*probeOmega^2;",
        "    probeMechanicalEnergyResidual = probeKinetic + rollingDissipated - 0.5;",
        "    probeLoss = probeRollingLoss;",
        "    probeShaftPower = 0; probeDcPower = 0; probeDriveLoss = 0;",
        "    probeBusPower = 0; probeBusEnergy = 0; probeBusCurrent = 0;",
        "    probeElectricalEnergyResidual = 0;",
        "    assert(probeRollingLoss >= -1e-10, \"rolling loss must be nonnegative\");",
        "    annotation(experiment(StartTime=0, StopTime=2), uses(Modelica(version=\"4.0.0\")));",
        "  end Scene;", "end V11CGenerated;", "",
    ]
    return "\n".join(lines)


def _sysml(design, case_id: str, output_count: int) -> str:
    if case_id in PROBES:
        return (
            "package V11CGeneratedStructure { "
            "part def ProbeComponent; "
            f"part def System {{ attribute caseCode : Integer = {PROBES.index(case_id) + 1}; "
            f"attribute tracedOutputCount : Integer = {output_count}; part probe : ProbeComponent; }} "
            "part system : System; }"
        )
    mass_items = [*design["bodies"], *design["wheels"], *design["drives"], design["power_supply"]]
    parts = " ".join(f"part {_ident(item['id'])} : PhysicalComponent;" for item in mass_items)
    attrs = [
        f"attribute loadedMass_kg : Real = {_f(sum(item['count']*item['mass']['value'] for item in mass_items))};",
        f"attribute physicalComponentCount : Integer = {len(mass_items)};",
        f"attribute tracedOutputCount : Integer = {output_count};",
        "attribute frozenSpecificationBound : Integer = 1;",
    ]
    for item in mass_items:
        ident = _ident(item["id"])
        attrs.extend([
            f"attribute {ident}_physicalCount : Integer = {item['count']};",
            f"attribute {ident}_aggregateMass_kg : Real = {_f(item['count']*item['mass']['value'])};",
        ])
    edges = []
    seen = set()
    for connection in design["connections"]:
        left, right = connection["from"].split(".")[0], connection["to"].split(".")[0]
        if left in {joint["id"] for joint in design["joints"]}:
            continue
        if right in {joint["id"] for joint in design["joints"]}:
            continue
        edge = tuple(sorted((left, right)))
        if edge not in seen:
            edges.append(f"connect {edge[0]} to {edge[1]};")
            seen.add(edge)
    for joint in design["joints"]:
        endpoints = []
        for connection in design["connections"]:
            if connection["from"].startswith(joint["id"] + "."):
                endpoints.append(connection["to"].split(".")[0])
            elif connection["to"].startswith(joint["id"] + "."):
                endpoints.append(connection["from"].split(".")[0])
        if len(endpoints) == 2:
            edges.append(f"connect {endpoints[0]} to {endpoints[1]};")
    return (
        "package V11CGeneratedStructure { "
        "part def PhysicalComponent; "
        f"part def System {{ {' '.join(attrs)} {parts} {' '.join(edges)} }} "
        f"\n// case={case_id} specification_digest={SPECIFICATION_DIGEST} trace=source_to_instance_and_outputs\n"
        "part system : System; }"
    )


def _trace(design, physical, binding, case_id: str, names, modelica: str, sysml: str) -> dict:
    component_map = []
    for collection in ("bodies", "wheels", "drives"):
        for item in design[collection]:
            ident = item["id"]
            component_map.append({
                "source_collection": collection,
                "source_id": ident,
                "modelica_instance": ("wheel_" if collection == "wheels" else "body_" if collection == "bodies" else "drive_") + ident,
                "body_outputs": [name for name in names if name.startswith("body_" + ident + "_")],
                "wheel_outputs": [name for name in names if name.startswith("wheel_" + ident + "_")],
                "physical_count": item["count"],
            })
    item = design["power_supply"]
    component_map.append({
        "source_collection": "power_supply", "source_id": item["id"],
        "modelica_instance": "body_" + item["id"],
        "body_outputs": [name for name in names if name.startswith("body_" + item["id"] + "_")],
        "wheel_outputs": [], "physical_count": item["count"],
    })
    full_mission = case_id == "fixed_nose_full_mission"
    return {
        "schema": "v11-c-generated-trace-1",
        "generator_version": VERSION,
        "case_id": case_id,
        "model_class": MODEL_CLASS,
        "one_solve": True,
        "state_continuity": {"no_reset": list(NO_RESET) if full_mission else [], "full_mission": full_mission},
        "frozen": {
            "specification_digest": SPECIFICATION_DIGEST,
            "design_digest": DESIGN_DIGEST,
            "physical_digest": PHYSICAL_DIGEST,
            "verification_digest": VERIFICATION_DIGEST,
            "b_source_sha256": B_SOURCE_SHA256,
        },
        "binding_version": binding["version"],
        "component_map": component_map,
        "output_names": list(names),
        "output_semantics": {
            "wheel": "synchronous_pair_aggregate_count_applied_once",
            "body_mass": "aggregate_component_mass_count_applied_once",
            "bus_energy": "single_shared_account_branch_energy_is_redundant_guard",
        },
        "modelica_source_sha256": hashlib.sha256(modelica.encode("utf-8")).hexdigest(),
        "sysml_source_sha256": hashlib.sha256(sysml.encode("utf-8")).hexdigest(),
    }


def generate(bundle, case_id: str) -> dict:
    """Generate one approved source pair without executing either artifact."""
    design, physical, binding = _validate_bundle(bundle)
    if case_id in PROBES:
        modelica = _shared_supply_probe() if case_id == PROBES[0] else _rolling_probe()
        names = probe_output_names()
    else:
        cases = {case["id"]: case for case in physical["cases"]}
        if case_id not in cases:
            raise ValueError("UNSUPPORTED: unknown C case")
        modelica = _vehicle_model(design, physical, binding, cases[case_id])
        names = vehicle_output_names(design)
    filt = variable_filter(names)
    sysml = _sysml(design, case_id, len(names) - 1)
    trace = _trace(design, physical, binding, case_id, names, modelica, sysml)
    return {
        "modelica": modelica,
        "model_class": MODEL_CLASS,
        "sysml": sysml,
        "trace": trace,
        "variable_filter": filt,
    }
