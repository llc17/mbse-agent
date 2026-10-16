"""Strict SI, symmetric planar tree contract, independent of legacy S1/physics.

The registry describes types, not implementations. Diagnostics distinguish
malformed input (INVALID) from a topology outside this contract (UNSUPPORTED).
Every component's mass/inertia is per physical instance; count is applied once.
Positions are initial world x/z/pitch; frame ports are local to their component.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_VERSION = "v11-composition-1"
GENERATOR_VERSION = "v11-structure-ir-1"
GROUPS = ("bodies", "wheels", "joints", "drives")
IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")


class ContractError(ValueError):
    def __init__(self, diagnostics):
        self.diagnostics = diagnostics
        super().__init__("; ".join(f"{d['code']}:{d['path']}: {d['message']}" for d in diagnostics))


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def load_library():
    return json.loads((ROOT / "models/components/contract-library-v1.json").read_text(encoding="utf-8"))


def library_digest(library):
    return digest(library)


def design_digest(design):
    # Only these two envelope fields are excluded. No scenario, provenance,
    # status, component version or project metadata can escape the binding.
    return digest({k: v for k, v in design.items() if k not in {"confirmation", "design_digest"}})


def seal_design(design):
    """Make a fresh pending proposal. Never carries a prior confirmation."""
    result = deepcopy(design)
    result["confirmation"] = {"origin": "pending", "human_confirmed": False}
    result["design_digest"] = design_digest(result)
    return result


def _issue(code, path, message):
    return {"code": code, "path": path, "message": message}


class _Invalid(Exception):
    pass


class _Reader:
    def fail(self, path, message, code="INVALID"):
        raise _Invalid(_issue(code, path, message))

    def keys(self, obj, required, path, optional=()):
        if not isinstance(obj, dict):
            self.fail(path, "must be an object")
        if set(obj) - set(required) - set(optional) or set(required) - set(obj):
            self.fail(path, f"expected fields {sorted(required)}; optional {sorted(optional)}")

    def text(self, value, path, identifier=False):
        if not isinstance(value, str) or not value.strip() or (identifier and not IDENTIFIER.fullmatch(value)):
            self.fail(path, "nonempty text / safe identifier required")

    def number(self, value, path, minimum=None, positive=False):
        if type(value) not in (float, int) or not math.isfinite(value):
            self.fail(path, "finite number required (boolean is not a number)")
        if (minimum is not None and value < minimum) or (positive and value <= 0):
            self.fail(path, "number outside allowed range")
        return value

    def quantity(self, item, unit, path, minimum=None, positive=False):
        self.keys(item, {"value", "unit", "source"}, path)
        self.text(item["source"], path + ".source")
        if item["unit"] != unit:
            self.fail(path, f"expected SI unit {unit}; implicit conversion is forbidden")
        return self.number(item["value"], path, minimum, positive)

    def pose(self, item, path):
        self.keys(item, {"x", "z", "pitch"}, path)
        return tuple(self.quantity(item[k], u, path + "." + k) for k, u in (("x", "m"), ("z", "m"), ("pitch", "rad")))


def _validate(design, library):
    r = _Reader()
    r.keys(design, {"schema_version", "design_id", "revision", "project_id", "baseline_version_id",
                   "generator_version", "library_version", "library_digest", "requirements", "bodies",
                   "wheels", "joints", "connections", "drives", "power_supply", "scenario",
                   "provenance", "representation", "confirmation", "design_digest"}, "design")
    if design["representation"] != {
        "coordinates": "world_xz_pitch_y", "parameter_basis": "per_physical_instance",
        "pair_motion": "synchronized_left_right", "aggregation": "multiply_by_count_once",
    }:
        r.fail("representation", "explicit coordinate/per-instance/pair aggregation convention required", "UNSUPPORTED")
    if design["schema_version"] != SCHEMA_VERSION:
        r.fail("schema_version", "unknown schema", "UNSUPPORTED")
    for key in ("design_id", "revision", "project_id", "baseline_version_id"):
        r.text(design[key], key, identifier=True)
    if design["generator_version"] != GENERATOR_VERSION:
        r.fail("generator_version", "generator not registered", "UNSUPPORTED")
    if design["library_version"] != library.get("library_version") or design["library_digest"] != library_digest(library):
        r.fail("library_version", "library content/version mismatch", "UNSUPPORTED")
    # This release has no executable component registry, even if callers supply one.
    if library != load_library():
        r.fail("library", "only the installed structural contract is registered", "UNSUPPORTED")
    r.keys(design["provenance"], {"origin", "reference", "scope"}, "provenance")
    for key, value in design["provenance"].items():
        r.text(value, "provenance." + key)
    req = design["requirements"]
    r.keys(req, {"profile", "payload_mass", "descent_mode", "motion_scope", "exit_behavior", "wheel_type"}, "requirements")
    payload = r.quantity(req["payload_mass"], "kg", "requirements.payload_mass", positive=True)
    if req["profile"] not in ("V11", "general"):
        r.fail("requirements.profile", "unknown profile")
    if req["profile"] == "V11" and payload != 20:
        r.fail("requirements.payload_mass", "V11 requires 20 kg equipment payload")
    if (req["descent_mode"] != "forward_front_wheels_first" or req["motion_scope"] != "symmetric_planar"
            or req["wheel_type"] != "ordinary_tire"):
        r.fail("requirements", "only ordinary tires and symmetric planar forward descent supported", "UNSUPPORTED")
    if req["exit_behavior"] != "continue_cruise":
        r.fail("requirements.exit_behavior", "this contract describes continuous cruise", "UNSUPPORTED")
    scenarios = design["scenario"]
    r.keys(scenarios, {"ascent", "descent", "flat"}, "scenario")
    for name, scenario in scenarios.items():
        fields = {"speed", "initial_speed", "friction", "platform_length", "entry", "exit"}
        if name != "flat":
            fields |= {"riser", "tread", "steps"}
        r.keys(scenario, fields, "scenario." + name)
        for key, unit in (("speed", "m/s"), ("platform_length", "m"), ("friction", "1")):
            r.quantity(scenario[key], unit, f"scenario.{name}.{key}", positive=True)
        r.quantity(scenario["initial_speed"], "m/s", f"scenario.{name}.initial_speed", minimum=0)
        if scenario["entry"] != {"ascent": "flat_before_first_riser", "descent": "top_before_first_riser", "flat": "level_ground"}[name]:
            r.fail("scenario." + name, "missing explicit entry condition")
        if scenario["exit"] != ("continue_cruise" if name == "flat" else "last_wheel_leaves_steps_then_continue_cruise"):
            r.fail("scenario." + name, "missing full exit and continued cruise")
        if name != "flat":
            r.quantity(scenario["riser"], "m", "scenario.riser", positive=True)
            r.quantity(scenario["tread"], "m", "scenario.tread", positive=True)
            n = r.quantity(scenario["steps"], "1", "scenario.steps", positive=True)
            if type(n) is not int:
                r.fail("scenario.steps", "integer count required")
    components, group_for, ports, poses = {}, {}, {}, {}
    allowed_kinds = {"bodies": {"rigid_body"}, "wheels": {"tire_pair"},
                     "joints": {"fixed", "passive_revolute", "wheel_revolute"},
                     "drives": {"drive_pair"}, "power_supply": {"dc_supply"}}
    for group in (*GROUPS, "power_supply"):
        items = [design[group]] if group == "power_supply" else design[group]
        if not isinstance(items, list) or (group in ("bodies", "wheels", "joints") and not items):
            r.fail(group, "component list required and structural groups cannot be empty")
        for item in items:
            common = {"id", "kind", "component_version", "count", "mass", "inertia", "pose", "ports", "source"}
            extras = {"bodies": {"role"}, "wheels": {"radius", "driven"}, "joints": {"dof", "axis"},
                      "drives": {"mount_body", "torque", "power", "voltage", "efficiency"},
                      "power_supply": {"mount_body", "voltage", "power", "usable_energy"}}[group]
            r.keys(item, common | extras, group)
            cid = item["id"]
            r.text(cid, group + ".id", identifier=True)
            if cid in components:
                r.fail(cid, "duplicate component id")
            if not isinstance(item["kind"], str) or item["kind"] not in allowed_kinds[group]:
                r.fail(cid, "component kind is unsupported", "UNSUPPORTED")
            if item["component_version"] != library["components"][item["kind"]]["version"]:
                r.fail(cid, "component version is not registered", "UNSUPPORTED")
            r.text(item["source"], cid + ".source")
            if type(item["count"]) is not int or item["count"] not in (1, 2):
                r.fail(cid, "count must explicitly be 1 or a symmetric pair of 2")
            if group in ("wheels", "drives") and item["count"] != 2:
                r.fail(cid, "this contract uses explicit left/right pairs", "UNSUPPORTED")
            if group == "power_supply" and item["count"] != 1:
                r.fail(cid, "supply is a single physical instance")
            for key, unit in (("mass", "kg"), ("inertia", "kg*m^2")):
                val = r.quantity(item[key], unit, cid + "." + key, minimum=0, positive=group != "joints")
                if group == "joints" and val != 0:
                    r.fail(cid, "ideal joint must have zero mass/inertia; hardware belongs to a body")
            poses[cid] = r.pose(item["pose"], cid + ".pose")
            if group == "bodies" and item["role"] not in ("chassis", "payload", "link", "controller"):
                r.fail(cid, "unknown body role")
            if group == "wheels":
                r.quantity(item["radius"], "m", cid + ".radius", positive=True)
                if type(item["driven"]) is not bool:
                    r.fail(cid, "driven must be a boolean")
            if group == "joints":
                if item["axis"] != "y" or item["dof"] != ([] if item["kind"] == "fixed" else ["ry"]):
                    r.fail(cid, "fixed releases none; passive/wheel revolute releases only ry", "UNSUPPORTED")
            if group in ("drives", "power_supply"):
                r.text(item["mount_body"], cid + ".mount_body", identifier=True)
                for key, unit in (("power", "W"), ("voltage", "V")):
                    r.quantity(item[key], unit, cid + "." + key, positive=True)
                if group == "drives":
                    r.quantity(item["torque"], "N*m", cid + ".torque", positive=True)
                    eff = r.quantity(item["efficiency"], "1", cid + ".efficiency", positive=True)
                    if eff > 1:
                        r.fail(cid, "efficiency cannot exceed 1")
                else:
                    r.quantity(item["usable_energy"], "J", cid + ".usable_energy", positive=True)
            if not isinstance(item["ports"], dict) or not item["ports"]:
                r.fail(cid, "explicit ports required")
            expected = {"joints": {"a": "frame2d", "b": "frame2d"},
                        "wheels": {"hub": "frame2d", **({"shaft": "rotational1d"} if item.get("driven") else {})},
                        "drives": {"shaft": "rotational1d", "dc": "dc"},
                        "power_supply": {"dc": "dc"}}.get(group)
            if expected and set(item["ports"]) != set(expected):
                r.fail(cid, "port names do not match registered component contract")
            for pname, port in item["ports"].items():
                r.text(pname, cid + ".port", identifier=True)
                r.keys(port, {"type", "pose"}, cid + "." + pname)
                if port["type"] != (expected[pname] if expected else "frame2d"):
                    r.fail(cid + "." + pname, "port type does not match component")
                local = r.pose(port["pose"], cid + "." + pname + ".pose")
                if group != "bodies" and local != (0, 0, 0):
                    r.fail(cid + "." + pname, "non-body ports are at component origin")
                ports[cid + "." + pname] = (cid, port["type"], local)
            components[cid], group_for[cid] = item, group
    chassis = [x["id"] for x in design["bodies"] if x["role"] == "chassis"]
    loads = [x for x in design["bodies"] if x["role"] == "payload"]
    if len(chassis) != 1 or components[chassis[0]]["count"] != 1 or len(loads) != 1 or loads[0]["count"] != 1 or loads[0]["mass"]["value"] != payload:
        r.fail("bodies", "exactly one chassis and one explicit payload body matching requirements are required")
    for item in [*design["drives"], design["power_supply"]]:
        mount = components.get(item["mount_body"])
        if mount is None or group_for[mount["id"]] != "bodies":
            r.fail(item["id"], "mount_body must reference a body; mass cannot be unattached")
        if mount["count"] > item["count"]:
            r.fail(item["id"], "one device cannot be mounted to two moving bodies", "UNSUPPORTED")
    connections = design["connections"]
    if not isinstance(connections, list):
        r.fail("connections", "list required")
    used = {p: [] for p in ports}
    cids = set(components)
    for connection in connections:
        r.keys(connection, {"id", "from", "to", "source"}, "connections")
        r.text(connection["id"], "connection.id", identifier=True)
        r.text(connection["source"], "connection.source")
        if connection["id"] in cids:
            r.fail("connections", "duplicate id")
        cids.add(connection["id"])
        a, b = connection["from"], connection["to"]
        if not isinstance(a, str) or not isinstance(b, str) or a not in ports or b not in ports:
            r.fail(connection["id"], "missing component/port reference")
        ac, typ, local_a = ports[a]
        bc, btype, local_b = ports[b]
        if ac == bc or typ != btype:
            r.fail(connection["id"], "self connection or incompatible ports")
        ag, bg = group_for[ac], group_for[bc]
        pair = {ag, bg}
        if typ == "frame2d":
            if "joints" not in pair or not pair.intersection({"bodies", "wheels"}) or ag == bg:
                r.fail(connection["id"], "frames connect one joint endpoint to one body/wheel")
            def world(cid, local):
                x, z, p = poses[cid]
                u, v, q = local
                return x + math.cos(p)*u - math.sin(p)*v, z + math.sin(p)*u + math.cos(p)*v, p+q
            wa, wb = world(ac, local_a), world(bc, local_b)
            if any(abs(x-y) > 1e-9 for x, y in zip(wa, wb)):
                r.fail(connection["id"], "initial connected frame poses disagree (m/rad tolerance 1e-9)")
        elif typ == "rotational1d":
            if pair != {"drives", "wheels"} or components[ac]["count"] != components[bc]["count"]:
                r.fail(connection["id"], "shaft must connect one drive pair to one wheel pair")
        elif typ == "dc":
            if pair != {"power_supply", "drives"}:
                r.fail(connection["id"], "DC must connect supply to drive")
            if components[ac]["voltage"]["value"] != components[bc]["voltage"]["value"]:
                r.fail(connection["id"], "nominal supply and drive voltage mismatch")
        used[a].append(b)
        used[b].append(a)
    for endpoint, peers in used.items():
        cid, typ, _ = ports[endpoint]
        fanout = group_for[cid] == "power_supply" and typ == "dc"
        if (not fanout and len(peers) != 1) or len(peers) != len(set(peers)):
            r.fail(endpoint, "missing, duplicate or multiply occupied port")
        if fanout and len(peers) != len(design["drives"]):
            r.fail(endpoint, "supply must feed every drive exactly once")
    nodes = {cid for cid, group in group_for.items() if group in ("bodies", "wheels")}
    parent = {n: n for n in nodes}
    adjacency = {n: [] for n in nodes}
    def root(n):
        while parent[n] != n:
            n = parent[n]
        return n
    for joint in design["joints"]:
        jid = joint["id"]
        a, b = (ports[used[jid + "." + p][0]][0] for p in ("a", "b"))
        groups = {group_for[a], group_for[b]}
        if joint["kind"] == "wheel_revolute":
            if groups != {"bodies", "wheels"} or joint["count"] != 2:
                r.fail(jid, "wheel bearing connects a body and a wheel pair")
        elif groups != {"bodies"}:
            r.fail(jid, "structural joints connect bodies only")
        if joint["count"] != max(components[a]["count"], components[b]["count"]):
            r.fail(jid, "joint physical multiplicity must match attached pair")
        if a == b:
            r.fail(jid, "joint cannot connect a body to itself")
        if root(a) == root(b):
            r.fail(jid, "closed-loop/parallel linkage outside tree contract", "UNSUPPORTED")
        parent[root(a)] = root(b)
        adjacency[a].append((b, joint))
        adjacency[b].append((a, joint))
    if len({root(n) for n in nodes}) != 1:
        r.fail("connections", "disconnected mechanical graph")
    for wheel in design["wheels"]:
        if len(adjacency[wheel["id"]]) != 1:
            r.fail(wheel["id"], "wheel must be a leaf with one bearing")
        if wheel["driven"]:
            drive = components[ports[used[wheel["id"] + ".shaft"][0]][0]]
            if drive["mount_body"] != adjacency[wheel["id"]][0][0]:
                r.fail(drive["id"], "drive must mount on the wheel carrier; remote transmission unsupported", "UNSUPPORTED")
    # Payload and electronics must be rigidly attached, not dangling as an
    # accidental pendulum; paired links retain their explicitly released DOF.
    for body in design["bodies"]:
        if body["role"] in {"payload", "controller"}:
            if len(adjacency[body["id"]]) != 1 or adjacency[body["id"]][0][1]["kind"] != "fixed":
                r.fail(body["id"], "payload/controller needs a single fixed attachment")
    if not isinstance(design["confirmation"], dict):
        r.fail("confirmation", "confirmation envelope must be an object")
    if design["design_digest"] != design_digest(design):
        r.fail("design_digest", "proposal content has changed; reseal and request fresh confirmation", "STALE")


def validate_design(design, library=None):
    """Return [] on valid structure, or a deterministic first actionable diagnostic.

    This checks the structural contract, not collisions, stresses or feasibility.
    It deliberately validates legal input before consulting any confirmation.
    """
    try:
        _validate(design, load_library() if library is None else library)
        return []
    except _Invalid as exc:
        return [exc.args[0]]
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        return [_issue("INVALID", "design", f"malformed JSON value: {type(exc).__name__}")]


def require_valid(design, library):
    diagnostics = validate_design(design, library)
    if diagnostics:
        raise ContractError(diagnostics)


def check_confirmation(design, *, test_only=False):
    confirmation = design["confirmation"]
    required = {"origin", "human_confirmed", "project_id", "baseline_version_id", "design_digest", "confirmed_at", "scope"}
    if set(confirmation) != required:
        raise ContractError([_issue("UNCONFIRMED", "confirmation", "explicit receipt required")])
    expected_origin = "synthetic_test" if test_only else "user"
    if (confirmation["origin"] != expected_origin or confirmation["human_confirmed"] is not (not test_only)
            or confirmation["scope"] != ("contract_test" if test_only else "generation")
            or not isinstance(confirmation["confirmed_at"], str) or not confirmation["confirmed_at"].strip()):
        raise ContractError([_issue("UNCONFIRMED", "confirmation", "receipt origin/scope is not authorized")])
    for key in ("project_id", "baseline_version_id", "design_digest"):
        if confirmation[key] != design[key]:
            raise ContractError([_issue("STALE", "confirmation." + key, "receipt binding mismatch")])


def confirm_design(design, receipt, library=None):
    """Bind a receipt supplied by a trusted user interaction (transport is E).

    This function does not authenticate a human. It cannot manufacture approval.
    Synthetic test receipts are never accepted by this production entry point.
    """
    result = deepcopy(design)
    require_valid(result, load_library() if library is None else library)
    result["confirmation"] = deepcopy(receipt)
    check_confirmation(result)
    return result
