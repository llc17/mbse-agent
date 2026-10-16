"""Independent frozen-spec artifact and raw CSV checks for active twin clusters.

No generator/backend import, model execution, subprocess, parameter search or
automatic physical repair. PASS refers only to the declared symmetric planar
macro model; supplier hardware/thermal/fail-safe certification stays UNKNOWN.
"""
from __future__ import annotations

import csv
import bisect
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

APPROVED_SHA = "ec45de70bd6ffc114c5a9670c0db5000f5ee131a4144f6949fd5df7215c627ac"
SCENES = {"active_joint_energy", "one_twin_module_load_cycle",
          "flat_energy_and_brake", "three_up", "three_forward_down",
          "twenty_up", "twenty_forward_down"}
EPS = 1e-8


class EvidenceMissing(ValueError):
    pass


class Report:
    def __init__(self, scope: str):
        self.checks: list[dict] = []
        self.diagnostics: dict[str, Any] = {}
        self.scope = scope

    def add(self, code: str, ok: bool, detail: Any = None):
        self.checks.append({"code": code, "status": "PASS" if ok else "FAIL",
                            "detail": detail})

    def missing(self, code: str, detail: Any):
        self.checks.append({"code": code, "status": "REQUIRES_EVIDENCE",
                            "detail": detail})

    def section(self, code: str, fn):
        try:
            fn()
        except EvidenceMissing as exc:
            self.missing(code, str(exc))
        except (KeyError, TypeError, ValueError, IndexError, OSError) as exc:
            self.missing(code, f"{type(exc).__name__}: {exc}")

    def result(self):
        states = {c["status"] for c in self.checks}
        status = ("FAIL" if "FAIL" in states else "REQUIRES_EVIDENCE"
                  if "REQUIRES_EVIDENCE" in states or not states else "PASS")
        return {"status": status, "scope": self.scope, "checks": self.checks,
                "diagnostics": self.diagnostics,
                "hardware_certification": "UNKNOWN",
                "V11_complete": False}


def _need(obj, key):
    if key not in obj:
        raise EvidenceMissing(f"Missing contract field {key}")
    return obj[key]


def _close(a, b, atol=1e-8, rtol=1e-8):
    return math.isfinite(a) and math.isfinite(b) and abs(a-b) <= atol+rtol*max(abs(a), abs(b))


def _matrix(v):
    if len(v) != 3 or any(len(row) != 3 for row in v):
        raise EvidenceMissing("Inertia must be a complete 3x3 tensor")
    return [[float(x) for x in row] for row in v]


def _primitive(p):
    """Recompute approved primitive descriptors; intrinsic_diagonal is ignored."""
    m = float(p.get("mass", _need(p, "m") if "mass" not in p else p["mass"]))
    kind = _need(p, "kind")
    dimensions = list(map(float, _need(p, "dimensions")))
    if m <= 0 or any(x <= 0 for x in dimensions):
        raise EvidenceMissing("Primitive mass/dimensions must be positive")
    if kind == "box":
        a, b, c = dimensions
        diag = [m*(b*b+c*c)/12, m*(a*a+c*c)/12, m*(a*a+b*b)/12]
    elif kind in ("cylinder_y", "ring_y", "thin_ring_y"):
        r, w = dimensions
        is_cylinder = kind == "cylinder_y"
        diag = [m*(3*r*r+w*w)/12 if is_cylinder else m*(r*r/2+w*w/12),
                m*r*r/(2 if is_cylinder else 1),
                m*(3*r*r+w*w)/12 if is_cylinder else m*(r*r/2+w*w/12)]
    elif kind in ("line_x", "thin_rod_x"):
        length, = dimensions
        diag = [0.0, m*length*length/12, m*length*length/12]
    else:
        raise EvidenceMissing(f"Unsupported independent primitive formula {kind}")
    q = _matrix(p.get("rotation", [[1, 0, 0], [0, 1, 0], [0, 0, 1]]))
    if not all(_close(sum(q[k][i]*q[k][j] for k in range(3)), float(i == j))
               for i in range(3) for j in range(3)):
        raise EvidenceMissing("Primitive rotation is not orthogonal")
    tensor = [[sum(q[i][k]*diag[k]*q[j][k] for k in range(3))
               for j in range(3)] for i in range(3)]
    center = list(map(float, p.get("center", _need(p, "centre") if "center" not in p else p["center"])))
    if len(center) != 3:
        raise EvidenceMissing("Primitive xyz center missing")
    return m, center, tensor


def _rollup(primitives):
    parts = [_primitive(p) for p in primitives]
    if not parts:
        raise EvidenceMissing("No source primitive ledger")
    mass = sum(p[0] for p in parts)
    center = [sum(m*c[i] for m, c, _ in parts)/mass for i in range(3)]
    tensor = [[0.0]*3 for _ in range(3)]
    for m, c, j in parts:
        d = [c[i]-center[i] for i in range(3)]
        d2 = sum(x*x for x in d)
        for i in range(3):
            for k in range(3):
                tensor[i][k] += j[i][k]+m*(d2*float(i == k)-d[i]*d[k])
    return mass, center, tensor


ROOT = Path(__file__).resolve().parents[3]
SPEC_REL = "week12/results/s3-c-open-configuration-proposal-20261009-01/proposal-final.json"
RESULT_REL = "week12/results/s3-c-active-eightwheel-execution-20261009-01"


def _safe_artifact_path(value, *, spec=False):
    path = Path(value)
    path = path if path.is_absolute() else ROOT/path
    path = path.resolve()
    relative = path.relative_to(ROOT.resolve()).as_posix()
    if spec:
        if relative != SPEC_REL:
            raise EvidenceMissing("Spec must be the exact registered frozen proposal path")
        return path
    allowed = (relative.startswith(RESULT_REL+"/") or
               relative.startswith("week12/src/active_cluster/") or
               relative in {"week12/models/components/V11ActiveClusterC3/package.mo",
                            "week12/models/components/V11PhysicalB1/package.mo",
                            "week12/models/components/V11ContactC2/package.mo",
                            "week12/scripts/s3_c_active_cluster.py",
                            "week12/tests/test_active_cluster_generation.py",
                            "week12/tests/test_active_cluster_verification.py"})
    if not allowed or path.name.lower() == ".env":
        raise EvidenceMissing("Artifact read path outside the active registered task")
    return path


def _model_bodies(design):
    return _need(_need(design, "graph"), "bodies")



def _expected_primitives(spec, body):
    """Independent reconstruction of approved dimensions and placements."""
    expected = []
    def add(kind, mass, center, dimensions):
        expected.append((kind, float(mass), list(center), list(dimensions)))
    kind = body["kind"]
    p = spec["mass"]["additional_primitives"]
    lanes = spec["lateral_solids"]
    if kind == "chassis":
        for row in spec["mass"]["rows"][:4]:
            add("box", row["m"], [row["x"], 0, row["z"]], p[row["id"]+"_box_m"])
        B = spec["geometry"]["fore_aft_hub_distance"]
        for side in (-1, 1):
            for x in (-B/2, B/2):
                for key, lane in (("carrier_actuator_y_cylinder", "hub_motor_gear"),
                                  ("brake_y_cylinder", "brake")):
                    q = p[key]
                    add("cylinder_y", q["m_each"], [x, side*lanes[lane]["center_y"], 0],
                        [q["r"], q["width"]])
    elif kind == "carrier_pair":
        rho = spec["geometry"]["carrier_radius"]
        for side in (-1, 1):
            add("line_x", 1., [0, side*lanes["carrier_rod"]["center_y"], 0],
                [spec["geometry"]["wheel_center_distance"]])
            q = p["carrier_feed_y_cylinder"]
            add("cylinder_y", q["m_each"], [0, side*lanes["feed_bearing"]["center_y"], 0],
                [q["r"], q["width"]])
            q = p["carrier_stator_y_cylinders"]
            for x in (-rho, rho):
                add("cylinder_y", q["m_each"], [x, side*lanes["wheel_stator"]["center_y"], 0],
                    [q["r"], q["width"]])
    elif kind == "wheel_pair":
        for side in (-1, 1):
            for primitive_kind, key in (("ring_y", "wheel_tyre_thin_ring"),
                                        ("cylinder_y", "wheel_rotor_y_cylinder")):
                q = p[key]
                add(primitive_kind, q["m_each"], [0, side*spec["geometry"]["lane_y"], 0],
                    [q["r"], q["width"]])
    else:
        raise EvidenceMissing("No approved primitive binding for body kind "+kind)
    return expected


def _bound_primitives(spec, body):
    remaining = _expected_primitives(spec, body)
    for primitive in body["primitives"]:
        item = (primitive["kind"], float(primitive["m"]), primitive["centre"], primitive["dimensions"])
        match = next((i for i, wanted in enumerate(remaining)
                      if item[0] == wanted[0] and _close(item[1], wanted[1])
                      and len(item[2]) == len(wanted[2]) and len(item[3]) == len(wanted[3])
                      and all(_close(a, b) for a, b in zip(item[2], wanted[2]))
                      and all(_close(a, b) for a, b in zip(item[3], wanted[3]))), None)
        if match is None or primitive.get("rotation", [[1, 0, 0], [0, 1, 0], [0, 0, 1]]) != [[1, 0, 0], [0, 1, 0], [0, 0, 1]]:
            return False
        remaining.pop(match)
    return not remaining
def _literal_bodies(section: str) -> dict:
    """Parse actual generated body literals including explicitly declared gravity."""
    matches = re.findall(
        r"\bC\.Body\s+(\w+)\s*\(\s*m\s*=\s*([^,]+),\s*g\s*=\s*([^,]+),\s*rCM\s*=\s*(\{[^;]+?\}),\s*I\s*=\s*(\{\{[^;]+?\}\})\s*\);",
        section)
    observed = {}
    for ident, mass, gravity, com_text, tensor_text in matches:
        if ident in observed:
            raise EvidenceMissing("Duplicate actual body literal: "+ident)
        values = (float(mass),
                  json.loads(com_text.replace("{", "[").replace("}", "]")),
                  json.loads(tensor_text.replace("{", "[").replace("}", "]")),
                  float(gravity))
        if not all(math.isfinite(x) for x in [values[0], values[3]]+values[1]+[x for row in values[2] for x in row]):
            raise EvidenceMissing("Nonfinite actual body literal: "+ident)
        observed[ident] = values
    return observed


def verify_generated(design: dict, manifest: dict, spec: dict) -> dict:
    """Actual artifacts/digests and independent primitive/tensor/port checks."""
    r = Report("generated active-cluster artifacts, frozen spec, symmetric planar")
    def identity():
        r.add("approved_candidate", spec.get("candidate_id") == "active-eight-wheel-twin-cluster")
        actual_path = _safe_artifact_path(manifest.get("spec_path", ROOT/SPEC_REL), spec=True)
        data = actual_path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        r.add("actual_frozen_spec_digest", sha == APPROVED_SHA,
              {"actual": sha, "approved": APPROVED_SHA})
        r.add("supplied_spec_equals_actual", json.loads(data) == spec)
        r.add("manifest_spec_digest", manifest.get("spec_sha256", manifest.get("approved_spec_sha256")) == APPROVED_SHA)
        r.add("versioned_active_schema", design.get("schema") == "v11-active-cluster-design-1")
    r.section("artifact_identity", identity)

    def ledger():
        bodies = _model_bodies(design)
        ids = [str(_need(b, "id")) for b in bodies]
        r.add("source_body_ids_unique", len(ids) == len(set(ids)))
        kinds = [b["kind"] for b in bodies]
        r.add("lowered_counts", kinds.count("chassis") == 1 and kinds.count("carrier_pair") == 2
              and kinds.count("wheel_pair") == 4, kinds)
        mass_sum = 0.
        payload = 0.
        source_ids = []
        for b in bodies:
            primitives = _need(b, "primitives")
            r.add("approved_primitive_dimensions_positions_"+b["id"], _bound_primitives(spec, b))
            m, com, tensor = _rollup(primitives)
            mass_sum += m
            for p in primitives:
                pid = _need(p, "source_id")
                source_ids.append(pid)
                if pid == "payload" or p.get("role") == "payload":
                    payload += float(p.get("m", p.get("mass", 0)))
            declared = _matrix(_need(b, "inertia"))
            r.add("source_tensor_"+b["id"],
                  _close(m, float(_need(b, "m"))) and
                  all(_close(com[i], b["com_local"][i]) for i in range(3)) and
                  all(_close(tensor[i][j], declared[i][j]) for i in range(3) for j in range(3)),
                  {"independent_mass": m, "independent_com": com, "independent_inertia": tensor})
            expected = {"chassis": (59.8, 2.6102958862876258),
                        "carrier_pair": (5.6, 2*.03472333333333334),
                        "wheel_pair": (3.5, 2*.029600000000000005)}[b["kind"]]
            r.add("frozen_mass_Jy_"+b["id"], _close(m, expected[0]) and _close(tensor[1][1], expected[1]))
        r.add("primitive_source_ids_once", len(source_ids) == len(set(source_ids)))
        r.add("macro_mass_85kg", _close(mass_sum, 85), mass_sum)
        r.add("source_payload_20kg_once", _close(payload, 20), payload)
        r.diagnostics["source_macro_mass_kg"] = mass_sum
    r.section("source_mass_full_tensor", ledger)

    def topology():
        graph = _need(design, "graph")
        bodies, joints = graph["bodies"], _need(graph, "joints")
        mids = [b["id"] for b in bodies]
        r.add("lowered_7_body_6_joint", len(bodies) == 7 and len(joints) == 6)
        r.add("lowered_joint_counts", sum(j["kind"] == "active_revolute" for j in joints) == 2
              and sum(j["kind"] == "wheel_revolute" for j in joints) == 4)
        physical = [p for b in bodies for p in _need(b, "physical_ids")]
        r.add("13_physical_body_ids_once", len(physical) == len(set(physical)) == 13)
        r.add("4carrier8wheel", sum(len(b["physical_ids"]) for b in bodies if b["kind"] == "carrier_pair") == 4
              and sum(len(b["physical_ids"]) for b in bodies if b["kind"] == "wheel_pair") == 8)
        connections = _need(graph, "connections")
        ends = {(c.get("from_port", c.get("from")), c.get("to_port", c.get("to")))
                for c in connections if c.get("from_port", c.get("from"))}
        physical_drives = 0
        parents = {}
        for j in joints:
            physical_drives += int(j["multiplicity"])
            parents[j["child"]] = j["parent"]
            r.add("body_parent_"+j["id"], j["parent"] in mids and j["child"] in mids and j["parent"] != j["child"])
            r.add("axis_minus_y_"+j["id"], j["axis"] == [0, -1, 0])
            d = j["drive_id"]
            r.add("shaft_graph_"+j["id"], (d+".shaft", j["id"]+".axis") in ends or (j["id"]+".axis", d+".shaft") in ends)
            r.add("moving_support_graph_"+j["id"], (d+".support", j["id"]+".support") in ends
                  or (j["id"]+".support", d+".support") in ends)
        r.add("12_physical_motors_once", physical_drives == 12)
        r.add("tree_parent_coverage", len(parents) == 6 and set(parents) == set(mids)-{"chassis"})
        for child in parents:
            seen = set()
            cursor = child
            while cursor in parents and cursor not in seen:
                seen.add(cursor)
                cursor = parents[cursor]
            r.add("tree_acyclic_"+child, cursor == "chassis")
        representation = _need(design, "representation")
        r.add("declared_planar_dof9", representation.get("dof") == 9
              and representation.get("no_independent_lateral_dof") is True)
    r.section("actual_source_ID_graph", topology)

    def artifacts():
        source_rows = manifest.get("sources", manifest.get("source_rows"))
        if not isinstance(source_rows, list) or not source_rows:
            raise EvidenceMissing("Actual source digest rows required")
        for source in source_rows:
            path = _safe_artifact_path(_need(source, "path"))
            r.add("source_digest_"+str(path), hashlib.sha256(path.read_bytes()).hexdigest() == source.get("sha256"))
        package = _safe_artifact_path(_need(manifest, "package_path"))
        sysml = _safe_artifact_path(_need(manifest, "sysml_path"))
        code = package.read_text(encoding="utf-8")
        sysml_code = sysml.read_text(encoding="utf-8")
        r.add("actual_model_and_sysml_nonempty", bool(code.strip()) and bool(sysml_code.strip()))
        for body in _model_bodies(design):
            pattern = r"\bpart\s+"+re.escape(body["id"])+r"\s*:\s*RigidEntity\s*\{\s*attribute\s+mass\s*=\s*([^;]+);\s*attribute\s+jy\s*=\s*([^;]+);"
            found = re.search(pattern, sysml_code)
            r.add("actual_sysml_mass_Jy_"+body["id"], bool(found) and _close(float(found.group(1)), body["m"]) and _close(float(found.group(2)), body["inertia"][1][1]))
        stripped = re.sub(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"', "", code, flags=re.S)
        forbidden = re.findall(r"(?:Rotational\.Sources\.(?:Position|Speed)|"
                               r"Rot\.Sources\.(?:Position|Speed)|\breinit\s*\()", stripped)
        r.add("no_ideal_joint_source_or_state_reset", not forbidden, forbidden)
        required_text = _need(manifest, "modelica_trace")
        for row in required_text:
            snippets = _need(row, "required_snippets")
            r.add("actual_modelica_trace_"+row["source_id"], bool(snippets) and all(s in code for s in snippets))
        r.add("seven_registered_scenes", set(_need(manifest, "model_class_by_scene")) == SCENES)
        package_path=_safe_artifact_path(manifest['package_path'])
        root_names={value.split('.')[0] for value in manifest['model_class_by_scene'].values()}
        package_name_ok=(len(root_names)==1 and
            (package_path.parent.name==next(iter(root_names)) if package_path.name=='package.mo'
             else package_path.name==next(iter(root_names))+'.mo'))
        r.add('actual_Modelica_package_filename_matches_root',package_name_ok,
              {'file':str(package_path),'declared_roots':sorted(root_names)})

        model_sections = {m.group(1): m.group(2) for m in re.finditer(
            r"\bmodel\s+(\w+)\b(.*?)\bend\s+\1\s*;", code, re.S)}
        for scene_name, class_name in manifest["model_class_by_scene"].items():
            name = class_name.rsplit(".", 1)[-1]
            if name not in model_sections:
                raise EvidenceMissing("Actual registered Modelica class text absent: "+name)
            section = model_sections[name]
            observed = _literal_bodies(section)
            source = {b["id"]: b for b in _model_bodies(design)}
            expected_ids = set(source) if scene_name != "one_twin_module_load_cycle" else {
                ident for ident in source if ident == "chassis" or ident.startswith("F")}
            r.add("actual_lowered_body_IDs_"+scene_name, set(observed) == expected_ids)
            for ident in expected_ids & set(observed):
                b = source[ident]
                mass, com, tensor = _rollup(b["primitives"])
                if scene_name == "one_twin_module_load_cycle" and ident == "chassis":
                    mass /= 2
                    tensor = [[v/2 for v in row] for row in tensor]
                am, ac, ai, ag = observed[ident]
                r.add("actual_lowered_gravity_"+scene_name+"_"+ident,
                      _close(ag, 0.0 if scene_name == "active_joint_energy" else 9.81))
                r.add("actual_lowered_mass_full_tensor_"+scene_name+"_"+ident,
                      _close(am, mass) and all(_close(ac[i], com[i]) for i in range(3))
                      and all(_close(ai[i][j], tensor[i][j]) for i in range(3) for j in range(3)))
            if scene_name not in ("active_joint_energy", "one_twin_module_load_cycle"):
                names = "|".join(re.escape(ident) for ident in expected_ids)
                forbidden_pose = re.findall(r"\b(?:"+names+r")\.(?:x|y|z|omega|frame\.r_0\[[123]\])\s*=", section)
                locks = re.findall(r"(?:Rot(?:ational)?\.Components\.Fixed|Rotational\.Sources\.(?:Position|Speed)|PlanarWithPitchAxis)", section)
                r.add("no_vehicle_ideal_pose_or_pitch_lock_"+scene_name, not forbidden_pose and not locks)
            for ident in expected_ids:
                if source[ident]["kind"] == "wheel_pair":
                    r.add("actual_contact_owner_connection_"+scene_name+"_"+ident, "connect("+ident+".frame,contact_"+ident+".frame)" in section)

        r.diagnostics["static_trace_is_not_dynamic_PASS"] = True
    r.section("actual_artifact_sources", artifacts)
    return r.result()
def _transform(x, z, phi, point):
    # math-positive rotation about -y, in world x/z.
    a, b = point[0], point[-1]
    return x+math.cos(phi)*a-math.sin(phi)*b, z+math.sin(phi)*a+math.cos(phi)*b


def _point_segment(p, a, b):
    dx, dz = b[0]-a[0], b[1]-a[1]
    q = min(1.0, max(0.0, ((p[0]-a[0])*dx+(p[1]-a[1])*dz)/(dx*dx+dz*dz)))
    return math.hypot(p[0]-a[0]-q*dx, p[1]-a[1]-q*dz)


def _segments_intersect(a, b, c, d):
    def cross(p, q, s):
        return (q[0]-p[0])*(s[1]-p[1])-(q[1]-p[1])*(s[0]-p[0])
    v1, v2, v3, v4 = cross(a, b, c), cross(a, b, d), cross(c, d, a), cross(c, d, b)
    if v1*v2 > 0 or v3*v4 > 0:
        return False
    return max(min(a[0], b[0]), min(c[0], d[0])) <= min(max(a[0], b[0]), max(c[0], d[0]))+EPS and \
        max(min(a[1], b[1]), min(c[1], d[1])) <= min(max(a[1], b[1]), max(c[1], d[1]))+EPS


def _segment_distance(a, b, c, d):
    if _segments_intersect(a, b, c, d):
        return 0.0
    return min(_point_segment(a, c, d), _point_segment(b, c, d),
               _point_segment(c, a, b), _point_segment(d, a, b))


def _terrain_slice(points, low, high):
    """Exact x-bound pruning; ignored segments are farther than known upper bound."""
    xs = [p[0] for p in points]
    begin = max(0, bisect.bisect_left(xs, low)-1)
    end = min(len(points)-1, bisect.bisect_right(xs, high)+1)
    return zip(points[begin:end], points[begin+1:end+1])


def _inside_terrain(p, points):
    xs = [q[0] for q in points]
    index = bisect.bisect_left(xs, p[0])
    heights = []
    for i in range(max(0, index-2), min(len(points)-1, index+2)):
        a, b = points[i], points[i+1]
        if b[0] > a[0] and a[0]-EPS <= p[0] <= b[0]+EPS:
            heights.append(a[1]+(p[0]-a[0])*(b[1]-a[1])/(b[0]-a[0]))
    if not heights:
        raise EvidenceMissing("Actual hardware outside declared finite terrain domain")
    return p[1] <= max(heights)


def _shape_clearance(shape, pose, terrain):
    x, z, phi = pose
    kind = _need(shape, "kind")
    radius = float(shape.get("radius", 0.0))
    if kind == "circle":
        center = _transform(x, z, phi, _need(shape, "center"))
        nearby = list(_terrain_slice(terrain, center[0], center[0]))
        upper = min(_point_segment(center, a, b) for a, b in nearby)
        minimum = min(_point_segment(center, a, b)
                      for a, b in _terrain_slice(terrain, center[0]-upper, center[0]+upper))
        return (-minimum if _inside_terrain(center, terrain) else minimum)-radius
    if kind == "capsule":
        a, b = [_transform(x, z, phi, p) for p in _need(shape, "endpoints")]
        if _inside_terrain(a, terrain) or _inside_terrain(b, terrain):
            return -radius
        low, high = min(a[0], b[0]), max(a[0], b[0])
        nearby = list(_terrain_slice(terrain, low, high))
        upper = min(_segment_distance(a, b, c, d) for c, d in nearby)
        return min(_segment_distance(a, b, c, d)
                   for c, d in _terrain_slice(terrain, low-upper, high+upper))-radius
    if kind == "polygon":
        pts = [_transform(x, z, phi, p) for p in _need(shape, "vertices")]
        if len(pts) < 3:
            raise EvidenceMissing("Rigid hardware polygon needs >=3 vertices")
        if any(_inside_terrain(p, terrain) for p in pts):
            return -EPS
        low, high = min(p[0] for p in pts), max(p[0] for p in pts)
        edges = list(zip(pts, pts[1:]+pts[:1]))
        nearby = list(_terrain_slice(terrain, low, high))
        upper = min(_segment_distance(a, b, c, d) for a, b in edges for c, d in nearby)
        return min(_segment_distance(a, b, c, d) for a, b in edges
                   for c, d in _terrain_slice(terrain, low-upper, high+upper))
    raise EvidenceMissing(f"No independent hardware geometry for {kind}")
def _walk_columns(obj):
    """Select only fields declared by the contract, never raw summary statuses."""
    if isinstance(obj, dict):
        if isinstance(obj.get("columns"), dict):
            for value in obj["columns"].values():
                if isinstance(value, str):
                    yield value
        for key, value in obj.items():
            if key != "columns":
                yield from _walk_columns(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_columns(value)


def _frames(path, names):
    """One actual row at a time, including both sides of duplicate-time events."""
    path = Path(path)
    forbidden = str(path.resolve()).lower()
    if path.name.lower() == ".env" or "v8_holdout_private" in forbidden or "v8_formal_private" in forbidden:
        raise EvidenceMissing("CSV path is outside allowed evidence scope")
    if not path.is_file():
        raise EvidenceMissing(f"Actual CSV absent: {path}")
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        if len(headers) != len(set(headers)):
            raise EvidenceMissing("Duplicate CSV columns")
        missing = sorted(set(names)-set(headers))
        if missing:
            raise EvidenceMissing("Actual CSV columns absent: "+repr(missing[:30]))
        last = -math.inf
        for row in reader:
            frame = {}
            for key in names:
                raw = row.get(key, "")
                try:
                    value = float(raw.lower() == "true") if raw.lower() in ("true", "false") else float(raw)
                except (ValueError, TypeError, AttributeError):
                    raise EvidenceMissing(f"Invalid numeric CSV {key}={raw!r}")
                if not math.isfinite(value):
                    raise EvidenceMissing(f"Nonfinite actual CSV {key}")
                frame[key] = value
            t = frame["time"]
            if t < last or t < -EPS:
                raise EvidenceMissing("Time decreases; event duplicates are allowed")
            last = t
            yield frame


class Accumulator:
    """Only one previous sample and finite scalar extrema/integrals are retained."""
    def __init__(self, report):
        self.r = report
        self.metrics = {}
        self.previous = None
        self.count = 0
        self.first_time = None
        self.last_time = None
        self.integrals = {}
        self.previous_power = {}
        self.duration = {}
        self.max_duration = {}
        self.first_energy = {}
        self.events = {}

    def check(self, code, ok, value=None):
        item = self.metrics.setdefault(code, {"ok": True, "samples": 0})
        item["ok"] = item["ok"] and bool(ok)
        item["samples"] += 1
        if isinstance(value, (float, int)):
            item["minimum"] = min(item.get("minimum", value), value)
            item["maximum"] = max(item.get("maximum", value), value)
        elif value is not None and not ok:
            item.setdefault("first_failure", value)

    def equal(self, code, a, b, atol=1e-6, rtol=1e-6):
        self.check(code, _close(a, b, atol, rtol), abs(a-b))

    def integrate(self, code, t, value, dt):
        if code in self.previous_power:
            self.integrals[code] = self.integrals.get(code, 0.0)+dt*(value+self.previous_power[code])/2
        else:
            self.integrals[code] = 0.0
        self.previous_power[code] = value

    def peak(self, code, flag, dt):
        # Conservative whole sampling interval when either endpoint exceeds.
        previous_flag = self.duration.get(code, (0.0, False))[1]
        duration = self.duration.get(code, (0.0, False))[0]
        duration = duration+dt if flag or previous_flag else 0.0
        self.duration[code] = duration, flag
        self.max_duration[code] = max(self.max_duration.get(code, 0.0), duration)

    def finish(self):
        for code, metric in self.metrics.items():
            self.r.add(code, metric.pop("ok"), metric)
        for code, duration in self.max_duration.items():
            self.r.add(code+"_duration_1s", duration <= 1.0+1e-8, duration)


def _value(frame, group, key):
    if key in group.get("constants", {}):
        return float(group["constants"][key])
    return frame[_need(_need(group, "columns"), key)]


def _total(frame, contract, key):
    if key in contract.get("_computed_columns", {}):
        return sum(frame[c] for c in contract["_computed_columns"][key])
    if key == "brake_active" and contract.get("_brake_trigger_s") is not None:
        return float(frame["time"] >= contract["_brake_trigger_s"])
    return frame[_need(_need(contract, "columns"), key)]


def _electrical_frame(s, frame, spec, contract, dt):
    proxy = spec["control"]["DC_proxy"]
    draw, dump, shaft = float(proxy["aux_W"]), 0.0, 0.0
    for d in _need(contract, "drives"):
        ident = d["id"]
        mult = int(_need(d, "multiplicity"))
        tau, speed = _value(frame, d, "tau"), _value(frame, d, "wrel")
        p = tau*speed
        s.equal("relative_shaft_power_"+ident, _value(frame, d, "power"), p)
        eta, n, kt, ra = (float(_need(d, k)) for k in ("eta", "gear", "kt", "Ra"))
        per_tau = tau/mult
        motor_tau = per_tau/(n*eta) if p >= 0 else eta*per_tau/n
        current = motor_tau/kt
        gear = (1/eta-1)*p if p >= 0 else (1-eta)*abs(p)
        copper = mult*current*current*ra
        motor_power = p+gear
        driver = (1/proxy["driver_efficiency"]-1)*(abs(motor_power)+copper)
        dc = motor_power+copper+driver
        voltage = ra*current+proxy["kt_ke"]*n*speed
        for key, expected in (("current", current), ("gear_loss", gear),
                              ("copper_loss", copper), ("driver_loss", driver),
                              ("dc", dc), ("voltage", voltage)):
            s.equal(key+"_"+ident, _value(frame, d, key), expected)
        kind = _need(d, "kind")
        rated, peak, max_speed = ((100., 150., .25) if kind == "carrier"
                                  else (20., 40., 4.))
        s.check("frozen_drive_parameters_"+ident,
                _close(eta, .8) and _close(kt, .1) and _close(ra, .2)
                and _close(n, 100 if kind == "carrier" else 25)
                and _close(float(_need(d, "continuous")), rated)
                and _close(float(_need(d, "peak")), peak))
        s.check("voltage_48V_"+ident, abs(voltage) <= 48+1e-6, abs(voltage))
        s.check("finite_peak_torque_"+ident, abs(per_tau) <= peak+1e-6, abs(per_tau))
        s.peak("above_continuous_torque_"+ident, abs(per_tau) > rated+1e-6, dt)
        s.check("relative_speed_"+ident, abs(speed) <= max_speed+1e-6, abs(speed))
        if kind == "wheel":
            s.check("wheel_250W_"+ident, abs(p)/mult <= 250+1e-6, abs(p)/mult)
        draw += max(dc, 0.0)
        dump += max(-dc, 0.0)
        shaft += p
        for key, power in (("copper", copper), ("gear", gear), ("driver", driver)):
            integral_key = key+"_"+ident
            s.integrate(integral_key, frame["time"], power, dt)
            if key+"_energy" in d["columns"]:
                heat = _value(frame, d, key+"_energy")
                s.first_energy.setdefault(integral_key, heat)
                s.check("actual_heat_integral_"+integral_key, _close(heat-s.first_energy[integral_key], s.integrals[integral_key], .5, .005), abs(heat-s.first_energy[integral_key]-s.integrals[integral_key]))
                if s.previous:
                    s.check("heat_no_reset_"+integral_key, heat >= _value(s.previous, d, key+"_energy")-1e-7)
    s.equal("shared_battery_positive_draw", _total(frame, contract, "battery_power"), draw)
    s.equal("shared_dump_negative_dc_route", _total(frame, contract, "dump_power"), dump)
    s.equal("shared_shaft_power_once", _total(frame, contract, "shaft_power"), shaft)
    s.check("shared_48V80A", draw <= 3840+1e-6, draw/48)
    s.check("dump_peak_1000W", dump <= 1000+1e-6, dump)
    s.peak("dump_above_300W", dump > 300+1e-6, dt)
    for key, value in (("battery", draw), ("dump", dump), ("shaft", shaft)):
        s.integrate(key, frame["time"], value, dt)
    battery_raw = _total(frame, contract, "battery_energy")
    battery = 2764800.-battery_raw if contract.get("_battery_remaining") else battery_raw
    dump_e = _total(frame, contract, "dump_energy")
    s.check("battery_usable_2764800J", -1e-6 <= battery <= 2764800+1e-6, battery)
    s.check("dump_capacity_5000J", -1e-6 <= dump_e <= 5000+1e-6, dump_e)
    for key, value in (("battery", battery), ("dump", dump_e)):
        s.first_energy.setdefault(key, value)
        if s.previous:
            old = _total(s.previous, contract, key+"_energy")
            if key == "battery" and contract.get("_battery_remaining"):
                old = 2764800.-old
            s.check(key+"_no_phase_reset", value >= old-1e-7, value-old)
        integrated = s.integrals[key]
        s.check(key+"_energy_integral", _close(value-s.first_energy[key], integrated, .5, .005),
                abs(value-s.first_energy[key]-integrated))
    return shaft


def _contact_frame(s, frame, spec, contract, info):
    """All dynamic features checked; inactive contacts use a verified zero path."""
    if not hasattr(s, "_contact_plan"):
        wheels = {w["id"]: w for w in contract["wheels"]}
        records = []
        required_loaded = set()
        for tr in info.get("transfers", []):
            required_loaded.add(tr["target_feature"])
            required_loaded.update(tr["old_features"])
        for f in _need(info, "features"):
            w = wheels[_need(f, "owner_id")]
            if f["kind"] == "face":
                a, b = _need(f, "segment")
                length = math.dist(a, b)
                tx, tz = (b[0]-a[0])/length, (b[1]-a[1])/length
                geometry = (a, length, tx, tz, -tz, tx)
            elif f["kind"] == "vertex":
                a = _need(f, "point")
                prev, nxt = _need(f, "previous"), _need(f, "next")
                geometry = (a, prev, nxt, math.dist(prev, a), math.dist(a, nxt))
            else:
                raise EvidenceMissing("Finite face/explicit convex corner required")
            records.append((f, w, geometry))
        s._contact_plan = wheels, records, required_loaded
    wheels, records, required_loaded = s._contact_plan
    params = spec["physics"]["proposed_contact_parameters"]
    states = {ident: tuple(_value(frame, w, key) for key in ("x", "z", "vx", "vz", "omega_world_y"))
              for ident, w in wheels.items()}
    result = {ident: {"Fx": 0., "Fz": 0., "Ty": 0., "elastic": 0.,
                      "normal_loss": 0., "friction_loss": 0., "normal": 0.}
              for ident in wheels}
    loaded = {}
    for f, w, geometry in records:
        x, z, vx, vz, omega = states[w["id"]]
        radius, mult = float(w["radius"]), int(w["multiplicity"])
        if f["kind"] == "face":
            a, length, tx, tz, nx, nz = geometry
            projection = (x-a[0])*tx+(z-a[1])*tz
            dist = (x-a[0])*nx+(z-a[1])*nz
            active = (projection > 0 if f.get("start_convex") else projection >= 0) and projection < length and 0 <= dist < radius
        else:
            a, prev, nxt, plen, nlen = geometry
            dist = math.hypot(x-a[0], z-a[1])
            nx, nz = ((x-a[0])/dist, (z-a[1])/dist) if dist > EPS else (0., 0.)
            prior = ((x-prev[0])*(a[0]-prev[0])+(z-prev[1])*(a[1]-prev[1]))/plen
            following = ((x-a[0])*(nxt[0]-a[0])+(z-a[1])*(nxt[1]-a[1]))/nlen
            active = bool(_need(f, "convex")) and EPS < dist < radius and prior >= plen and following <= 0
        ident = f["id"]
        values = {key: _value(frame, f, key) for key in
                  ("Fn", "Ft", "active", "penetration", "slip", "nx", "nz", "owner")}
        owner = float(_need(w, "owner_index"))
        if not active:
            expected = {"Fn": 0., "Ft": 0., "active": 0., "penetration": 0.,
                        "slip": 0., "nx": nx, "nz": nz, "owner": owner}
            ok = all(_close(values[key], target, 1e-7, 1e-6) for key, target in expected.items())
            s.check("actual_feature_inactive_"+ident, ok)
            if not ok:
                for key, target in expected.items():
                    if not _close(values[key], target, 1e-7, 1e-6):
                        s.equal("actual_"+("owner" if key == "owner" else key)+"_"+ident,
                                values[key], target, 1e-7, 1e-6)
            if ident in required_loaded:
                loaded[ident] = {"Fn": values["Fn"], "active": bool(values["active"]),
                    "normal": (nx, nz), "owner": w["id"], "perphysical": values["Fn"]/mult}
            continue
        delta = max(0., radius-dist)
        vn = vx*nx+vz*nz
        vt = vx*nz-vz*nx-radius*omega
        fe = mult*params["per_physical_tyre_k"]*delta
        fd = max(-fe, min(fe, -mult*params["c"]*vn))
        fn = fe+fd
        ft = -params["mu"]*fn*math.tanh(vt/params["vreg"])
        for key, expected, tol in (("Fn", fn, 1e-4), ("Ft", ft, 1e-4),
                                   ("active", 1., 1e-8), ("penetration", delta, 1e-7),
                                   ("slip", vt, 1e-6), ("nx", nx, 1e-6), ("nz", nz, 1e-6)):
            s.equal("actual_"+key+"_"+ident, values[key], expected, tol, 1e-5)
        s.equal("actual_owner_"+ident, values["owner"], owner, 1e-8, 0)
        s.check("unilateral_cone_"+ident, values["Fn"] >= -1e-6
                and abs(values["Ft"]) <= params["mu"]*values["Fn"]+1e-6)
        s.check("tyre_penetration_5mm_"+ident, delta <= .005+1e-8, delta)
        item = result[w["id"]]
        item["Fx"] += fn*nx+ft*nz
        item["Fz"] += fn*nz-ft*nx
        item["Ty"] += radius*ft
        item["elastic"] += .5*mult*params["per_physical_tyre_k"]*delta*delta
        item["normal_loss"] += fd*(-vn)
        item["friction_loss"] += -ft*vt
        item["normal"] += fn
        if ident in required_loaded:
            loaded[ident] = {"Fn": values["Fn"], "active": bool(values["active"]),
                "normal": (nx, nz), "owner": w["id"], "perphysical": values["Fn"]/mult}
    loaded["_wheel_normal"] = {}
    for ident, item in result.items():
        w = wheels[ident]
        omega = states[ident][4]
        rr_y = -params["rolling_coefficient"]*item["normal"]*float(w["radius"])*math.tanh(omega/params["rolling_omega_regularization"])
        item["Ty"] -= rr_y  # math minus y
        item["rolling_loss"] = -rr_y*omega
        for key in ("Fx", "Fz", "normal"):
            s.equal("summed_contact_"+key+"_"+ident, _value(frame, w, key), item[key], 1e-4, 1e-5)
        actual_rr_y = -frame[w["_rolling_torque_math_column"]] if "_rolling_torque_math_column" in w else _value(frame, w, "rolling_torque_world_y")
        s.equal("rolling_world_torque_"+ident, actual_rr_y, rr_y)
        s.check("ground_rolling_loss_nonnegative_"+ident, item["rolling_loss"] >= -1e-8)
        loaded["_wheel_normal"][ident] = _value(frame, w, "normal")
    return result, loaded
def _body_frame(s, frame, contract, contact, dt):
    bodies = {b["id"]: b for b in contract["bodies"]}
    mechanics, potential, totalmass, weighted_x, weighted_vx = 0., 0., 0., 0., 0.
    poses = {}
    for ident, b in bodies.items():
        x, z, phi, vx, vz, omega, ax, az, alpha = [_value(frame, b, k)
             for k in ("x", "z", "phi", "vx", "vz", "omega", "ax", "az", "alpha")]
        m = float(_need(b, "m"))
        com = _need(b, "com_local")
        tensor = _matrix(_need(b, "inertia"))
        cx, cz = _transform(x, z, phi, com)
        dx, dz = cx-x, cz-z
        cvx, cvz = vx-omega*dz, vz+omega*dx
        cax = ax-alpha*dz-omega*omega*dx
        caz = az+alpha*dx-omega*omega*dz
        kinetic = .5*m*(cvx*cvx+cvz*cvz)+.5*tensor[1][1]*omega*omega
        gravity = float(contract.get("_gravity", 9.81))
        pot = m*gravity*cz
        s.equal("actual_msl_mass_"+ident, _value(frame, b, "mass"), m)
        for key, i, j in (("Ixx", 0, 0), ("Iyy", 1, 1), ("Izz", 2, 2),
                          ("Ixy", 0, 1), ("Ixz", 0, 2), ("Iyz", 1, 2)):
            s.equal("actual_msl_"+key+"_"+ident, _value(frame, b, key), tensor[i][j])
        s.equal("independent_kinetic_"+ident, _value(frame, b, "kinetic"), kinetic)
        s.equal("independent_potential_"+ident, _value(frame, b, "potential"), pot)
        fx, fz, ty = 0., -m*gravity, 0.
        # Body endpoint wrenches are real MSL frame loads resolved in world x/z;
        # the complete net wrench must be emitted, not a canned residual.
        for load in _need(b, "loads"):
            lf, lz, lt = (_value(frame, load, k) for k in ("Fx", "Fz", "Ty"))
            lp = _need(load, "point_local")
            px, pz = _transform(x, z, phi, lp)
            fx += lf
            fz += lz
            ty += lt+(px-cx)*lz-(pz-cz)*lf
        if ident in contact:
            co = contact[ident]
            fx += co["Fx"]
            fz += co["Fz"]
            ty += co["Ty"]+(x-cx)*co["Fz"]-(z-cz)*co["Fx"]
        s.equal("body_force_x_"+ident, fx, m*cax, 1e-3, 5e-5)
        s.equal("body_force_z_"+ident, fz, m*caz, 1e-3, 5e-5)
        s.equal("body_moment_"+ident, ty, tensor[1][1]*alpha, 1e-3, 5e-5)
        mechanics += kinetic
        potential += pot
        totalmass += m
        weighted_x += m*cx
        weighted_vx += m*cvx
        poses[ident] = {"x": x, "z": z, "phi": phi, "comx": cx, "comz": cz,
                        "vx": vx, "comvx": cvx}
    return mechanics+potential, poses, weighted_x/totalmass, weighted_vx/totalmass



def _whole_module_frame(s, frame, contract, contact):
    """Ground moment plus carrier drive; wheel drives remain internal."""
    bodies = {b["id"]: b for b in contract["bodies"]}
    drives = {d["body_id"]: d for d in contract["drives"]}
    for carrier in (b for b in contract["bodies"] if b.get("kind") == "carrier_pair"):
        cid = carrier["id"]
        if cid not in drives:
            raise EvidenceMissing("Whole-module carrier drive source absent")
        cx, cz = _value(frame, carrier, "x"), _value(frame, carrier, "z")
        external = _value(frame, drives[cid], "tau")
        inertial = 0.
        for b in contract["bodies"]:
            if b["id"] != cid and b.get("parent") != cid:
                continue
            x, z = _value(frame, b, "x"), _value(frame, b, "z")
            phi = _value(frame, b, "phi")
            bx, bz = _transform(x, z, phi, b["com_local"])
            dx, dz = bx-x, bz-z
            omega = _value(frame, b, "omega")
            alpha = _value(frame, b, "alpha")
            ax = _value(frame, b, "ax")-alpha*dz-omega*omega*dx
            az = _value(frame, b, "az")+alpha*dx-omega*omega*dz
            mass, jy = b["m"], b["inertia"][1][1]
            external -= mass*float(contract.get("_gravity", 9.81))*(bx-cx)
            inertial += jy*alpha+mass*((bx-cx)*az-(bz-cz)*ax)
            if b["id"] in contact:
                co = contact[b["id"]]
                external += (x-cx)*co["Fz"]-(z-cz)*co["Fx"]+co["Ty"]
        s.equal("whole_module_no_internal_drive_doublecount_"+cid,
                external, inertial, 1e-3, 5e-5)
def _joint_frame(s, frame, contract):
    for j in _need(contract, "joints"):
        q, w, a, err = [_value(frame, j, k) for k in ("q", "w", "a", "error")]
        expected_error = _value(frame, j, "qref")-q if contract.get("_reference_minus_actual") else q-_value(frame, j, "qref")
        s.equal("actual_q_reference_error_"+j["id"], err, expected_error)
        if j["kind"] == "active_revolute" and not contract.get("_probe"):

            s.check("actual_qerror_01rad_"+j["id"], abs(err) <= .01+1e-8, abs(err))
            s.check("relative_q_speed_025_"+j["id"], abs(w) <= .25+1e-8, abs(w))
            s.check("reference_speed_025_"+j["id"], abs(_value(frame, j, "wref")) <= .25+1e-8)
            # .04 is a reference-domain bound; actual acceleration is measured,
            # used in the independent inertial torque checks, never idealized.
            s.check("reference_accel_004_"+j["id"], abs(_value(frame, j, "aref")) <= .04+1e-8)
        s.check("actual_acceleration_finite_"+j["id"], math.isfinite(a), abs(a))



def _shape_x_min(shape, pose):
    x, z, phi = pose
    radius = float(shape.get("radius", 0.))
    if shape["kind"] == "circle":
        return _transform(x, z, phi, shape["center"])[0]-radius
    points = shape["endpoints"] if shape["kind"] == "capsule" else shape["vertices"]
    return min(_transform(x, z, phi, p)[0] for p in points)-radius
def _progress_frame(s, frame, contract, info, poses, whole_x, whole_vx, dt):
    chassis = poses[_need(info, "chassis_id")]
    for name, x, velocity in (("midpoint", chassis["x"], chassis["vx"]),
                              ("main_COM", chassis["comx"], chassis["comvx"]),
                              ("whole_COM", whole_x, whole_vx)):
        s.check("forward_velocity_"+name, velocity >= -.001-1e-8, velocity)
        s.integrate("reverse_"+name, frame["time"], max(0., -velocity), dt)
        s.check("reverse_distance_1mm_"+name, s.integrals["reverse_"+name] <= .001+1e-8,
                s.integrals["reverse_"+name])
    wheels = contract["wheels"]
    if info.get("direction") == "down":
        for w in wheels:
            ident = w["id"]
            z = _value(frame, w, "z")
            initial = s.first_energy.setdefault("wheel_z_"+ident, z)
            if z < initial-.005 and ident not in s.events:
                s.events[ident] = frame["time"]
        if any(k in s.events for k in _need(info, "rear_wheel_ids")):
            front = [s.events[k] for k in _need(info, "front_wheel_ids") if k in s.events]
            rear = [s.events[k] for k in info["rear_wheel_ids"] if k in s.events]
            s.check("front_main_wheel_first_forward_down", bool(front) and min(front) <= min(rear)+1e-8)
        if any(k in s.events for k in info["front_wheel_ids"]):
            s.check("head_down_actual_pitch", chassis["phi"] <= 1e-6, chassis["phi"])
    trailing = min(_value(frame, w, "x")-float(w["radius"]) for w in wheels)
    for shape in contract.get("hardware", []):
        p = poses[shape["body_id"]]
        trailing = min(trailing, _shape_x_min(shape, (p["x"], p["z"], p["phi"])))
    edge = float(_need(info, "exit_edge_x"))
    if trailing >= edge+.02 and "whole_exit_time" not in s.events:
        s.events["whole_exit_time"] = frame["time"]
        s.events["whole_exit_midpoint_x"] = chassis["x"]
    if "whole_exit_time" in s.events:
        s.diagnostics_exit = {"cruise_m": chassis["x"]-s.events["whole_exit_midpoint_x"],
                              "terminal_vx": chassis["vx"], "terminal_main_COM_vx": chassis["comvx"],
                              "terminal_whole_COM_vx": whole_vx, "trailing": trailing}
    return chassis


def _transfers_frame(s, frame, contract, info, loaded, dt):
    """No ideal reference geometry supplies the actual loading guard."""
    for tr in _need(info, "transfers"):
        ident = _need(tr, "id")
        if "_segment_column" in tr:
            segment = int(frame[tr["_segment_column"]])
            clock = frame[tr["_phase_clock_column"]]
            reference_clock = tr["_nominal_reference_s"]-clock if tr["_down"] else clock
            at_edge = reference_clock <= tr["_begin_clock"]+1e-8 if tr["_down"] else reference_clock >= tr["_end_clock"]-1e-8
            request = segment == tr["segment"] and at_edge
            previous_segment = int(s.previous[tr["_segment_column"]]) if s.previous else segment
            complete = previous_segment == tr["segment"] and segment != previous_segment
        else:
            request = _value(frame, tr, "requested") > .5
            complete = _value(frame, tr, "completed") > .5
        target = loaded[_need(tr, "target_feature")]
        old_ids = _need(tr, "old_features")
        if not old_ids:
            raise EvidenceMissing("Transfer lacks explicit old carrying feature owner: "+ident)
        oldnormal = sum(loaded[f]["Fn"] for f in old_ids)
        horizontal = abs(target["normal"][0]) <= 1e-6 and target["normal"][1] >= 1-1e-6
        valid = target["active"] and horizontal and target["perphysical"] >= 20-1e-8
        key = "transfer_guard_"+ident
        hold = s.duration.get(key, (0., False))[0]
        previous_valid = s.duration.get(key, (0., False))[1]
        hold = hold+dt if valid and previous_valid else 0.
        s.duration[key] = hold, valid
        if request and not complete:
            s.check("old_load_before_new_"+ident, oldnormal > 1e-8, oldnormal)
        if complete and "transfer_completed_"+ident not in s.events:
            s.check("new_actual_tread_load_02s_"+ident, valid and hold >= .2-1e-8, hold)
        if request:
            s.events.setdefault("transfer_requested_"+ident, frame["time"])
        if complete:
            if "transfer_requested_"+ident not in s.events:
                # Conservative bracket if solver emits only post-event row.
                s.events["transfer_requested_"+ident] = s.previous["time"] if s.previous else frame["time"]
            s.events.setdefault("transfer_completed_"+ident, frame["time"])
        if request and not complete:
            wait = frame["time"]-s.events["transfer_requested_"+ident]
            s.check("load_transfer_wait_2s_"+ident, wait <= 2+1e-8, wait)


def _validate_contract(r, contract, info, scene):
    if scene not in SCENES:
        r.add("known_scene", False, scene)
    groups = _need(contract, "bodies")
    ids = [b["id"] for b in groups]
    r.add("actual_body_ids_unique", len(ids) == len(set(ids)))
    driveids = [p for d in _need(contract, "drives") for p in _need(d, "physical_ids")]
    r.add("drive_entity_ids_once", len(driveids) == len(set(driveids)))
    if scene not in ("active_joint_energy", "one_twin_module_load_cycle"):
        r.add("full_vehicle_count", len(groups) == 7 and len(driveids) == 12 and len(contract["wheels"]) == 4)
    for d in contract["drives"]:
        r.add("drive_multiplicity_"+d["id"], d["multiplicity"] == len(d["physical_ids"]))
    if contract.get("definitions", {}).get("power") not in ("tau_math_minus_y*wrel_math_minus_y", "tau*wrel"):
        raise EvidenceMissing("Explicit shaft-axis/moving-support power convention required")
    if contract["wheels"]:
        features = _need(info, "features")
        points = _need(_need(info, "terrain"), "points")
        faces = len(points)-1
        owners = {w["id"] for w in contract["wheels"]}
        r.add("feature_owner_coverage", owners == {f["owner_id"] for f in features})
        for ident in owners:
            ff = [f for f in features if f["owner_id"] == ident]
            r.add("all_finite_faces_"+ident, sum(f["kind"] == "face" for f in ff) == faces)
    r.diagnostics["supplier_hardware_duty"] = "UNKNOWN"
    r.diagnostics["fail_safe_160Nm"] = "UNKNOWN_NOT_MODELLED"




def _normalize_scene(contract, scene, spec, graph):
    """Adapt the actual backend's nested scene fields without importing it.

    Static finite-face normals and owners are derived from frozen geometry and
    source IDs. Every dynamic Fn/Ft/active/penetration/slip remains a CSV field.
    """
    raw = _need(_need(contract, "scenes"), scene)
    if "metadata" not in raw:
        return contract, raw
    meta = _need(raw, "metadata")
    local = {k: v for k, v in raw.items()}
    local["definitions"] = dict(_need(contract, "definitions"))
    local["definitions"]["power"] = "tau*wrel" if local["definitions"].get("drive_power") == "aggregate_pair_tau*wrel" else local["definitions"].get("power")
    local["_reference_minus_actual"] = False
    local["_battery_remaining"] = True
    local["_mechanical_includes_elastic"] = True
    local["_gravity"] = 0. if meta["scope"] == "active_joint_free_moving_support_probe" else 9.81
    local["_probe"] = meta["scope"] == "active_joint_free_moving_support_probe"
    local["_fixture"] = meta["scope"] == "one_twin_module_pitch_constrained_half_vehicle_load_fixture"
    columns = dict(_need(raw, "columns"))
    columns["elastic_energy"] = _need(columns, "elastic_energy")
    columns["failure_code"] = _need(columns, "failure_code")
    local["columns"] = columns
    local["_computed_columns"] = {"ground_loss_power": [columns[k] for k in ("normal_loss", "friction_loss", "rolling_loss")]}
    local["_brake_trigger_s"] = meta.get("brake_trigger_s")
    bodies = []
    for b in _need(raw, "bodies"):
        row = dict(b, columns=dict(b["columns"]), loads=[])
        if local["_fixture"]:
            row["m"] = _need(b, "actual_mass")
            row["com_local"] = _need(b, "actual_com_local")
            row["inertia"] = _need(b, "actual_inertia")
        for load in _need(b, "loads"):
            # Contact wrenches are independently reconstructed from actual
            # geometry/features below; they must not be counted a second time.
            if load.get("id", "").startswith("terrain_"):
                continue
            entry = dict(load)
            entry["point_local"] = load.get("point_local", _need(load, "point") if "point_local" not in load else load["point_local"])
            row["loads"].append(entry)
        bodies.append(row)
    local["bodies"] = bodies
    bodymap = {b["id"]: b for b in bodies}
    wheels = []
    for w in _need(raw, "wheels"):
        row = dict(w, columns=dict(w["columns"]))
        row["radius"] = w.get("radius", _need(w, "r") if "radius" not in w else w["radius"])
        row["owner_index"] = ["F_A", "F_B", "R_A", "R_B"].index(w["id"])+1
        for key in ("x", "z", "vx", "vz"):
            row["columns"][key] = _need(bodymap[w["id"]]["columns"], key)
        row["_rolling_torque_math_column"] = _need(w["columns"], "tau_rr")
        wheels.append(row)
    local["wheels"] = wheels
    wheelmap = {w["id"]: w for w in wheels}
    terrain_record = _need(meta, "terrain")
    terrain = _need(terrain_record, "points") if isinstance(terrain_record, dict) else terrain_record
    kinds = _need(meta, "vertex_kind")
    features = []
    for f in _need(raw, "features"):
        row = dict(f, columns=dict(f["columns"]), constants={})
        index = int(_need(f, "index"))-1
        row["constants"]["owner"] = wheelmap[f["owner_id"]]["owner_index"]
        if f["kind"] == "face":
            row["segment"] = [f["point"], _need(f, "end")]
            row["start_convex"] = kinds[index] == 1
            nx, nz = _need(f, "normal")
            row["constants"].update(nx=nx, nz=nz)
        elif f["kind"] == "vertex":
            row["previous"], row["next"] = terrain[index-1], terrain[index+1]
            row["convex"] = kinds[index] == 1
        features.append(row)
    info = dict(meta, features=features, terrain={"points": terrain},
                chassis_id="chassis", direction="down" if meta.get("forward_down") else "up",
                front_wheel_ids=_need(meta, "front_wheels"),
                rear_wheel_ids=[w["id"] for w in wheels if w["id"].startswith("R")],
                exit_edge_x=meta.get("exit_boundary"), transfers=[])
    for tr in _need(raw, "transfers"):
        row = dict(tr)
        target_faces = [f["id"] for f in features if f["owner_id"] == tr["target_wheel"]
                        and f["kind"] == "face" and f["normal"][1] > .9
                        and abs(f["point"][1]-tr["target_height"]) <= 1e-8]
        if len(target_faces) != 1:
            raise EvidenceMissing("Transfer must target one actual finite horizontal tread")
        row["target_feature"] = target_faces[0]
        row["old_features"] = list(_need(tr, "old_features"))
        row["_phase_clock_column"] = columns["phase_clock"]
        row["_segment_column"] = _need(tr, "phase_column")
        row["_end_clock"] = meta["reference_segments"][tr["segment"]-1]["end"]
        row["_begin_clock"] = meta["reference_segments"][tr["segment"]-1]["start"]
        row["_nominal_reference_s"] = meta["nominal_reference_s"]
        row["_down"] = meta.get("forward_down", False)
        info["transfers"].append(row)
    return local, info
def _frozen_hardware(spec, graph, body_ids):
    """Rebuild hardware dimensions from approved sources, not nominal gaps."""
    bykind = {}
    for body in graph["bodies"]:
        if body["id"] in body_ids:
            bykind.setdefault(body["kind"], []).append(body)
    out = []
    def circle(ident, body, center, radius):
        out.append(dict(id=ident, body_id=body, kind="circle", center=center, radius=radius))
    for b in bykind.get("chassis", []):
        for row in spec["mass"]["rows"][:4]:
            dims = spec["mass"]["additional_primitives"][row["id"]+"_box_m"]
            a, _, c = dims
            x, z = row["x"], row["z"]
            out.append(dict(id=b["id"]+"_"+row["id"], body_id=b["id"], kind="polygon",
                            vertices=[[x-a/2, z-c/2], [x+a/2, z-c/2],
                                      [x+a/2, z+c/2], [x-a/2, z+c/2]]))
        half_B = spec["geometry"]["fore_aft_hub_distance"]/2
        for sign in (-1, 1):
            circle(b["id"]+"_hub_drive_"+str(sign), b["id"], [sign*half_B, 0],
                   spec["lateral_solids"]["hub_motor_gear"]["radial"])
            circle(b["id"]+"_brake_"+str(sign), b["id"], [sign*half_B, 0],
                   spec["lateral_solids"]["brake"]["radial"])
    rho = spec["geometry"]["carrier_radius"]
    for b in bykind.get("carrier_pair", []):
        circle(b["id"]+"_feed", b["id"], [0, 0], spec["lateral_solids"]["feed_bearing"]["radial"])
        out.append(dict(id=b["id"]+"_rod", body_id=b["id"], kind="capsule",
                        endpoints=[[-rho, 0], [rho, 0]],
                        radius=spec["lateral_solids"]["carrier_rod"]["collision_radius_xz"]))
        for sign in (-1, 1):
            circle(b["id"]+"_stator_"+str(sign), b["id"], [sign*rho, 0],
                   spec["lateral_solids"]["wheel_stator"]["radial"])
    for b in bykind.get("wheel_pair", []):
        circle(b["id"]+"_rotor", b["id"], [0, 0], spec["lateral_solids"]["wheel_rotor"]["radial"])
    if not out:
        raise EvidenceMissing("No approved source hardware matched actual body IDs")
    return out

def _bind_csv_graph(r, graph, contract, scene):
    if "graph" in graph:
        graph = graph["graph"]
    source = {b["id"]: b for b in _need(graph, "bodies")}
    full = scene not in ("active_joint_energy", "one_twin_module_load_cycle")
    if full:
        r.add("actual_csv_graph_body_ID_coverage", set(source) == {b["id"] for b in contract["bodies"]})
    for b in contract["bodies"]:
        if b["id"] not in source:
            if full:
                r.add("unregistered_actual_body_"+b["id"], False)
            continue
        expected = source[b["id"]]
        if contract.get("_fixture") and b["id"] == "chassis":
            expected = dict(expected, m=expected["m"]/2, inertia=[[v/2 for v in row] for row in expected["inertia"]])
        r.add("csv_source_mass_tensor_"+b["id"],
              _close(float(_need(b, "m")), float(expected["m"])) and
              _need(b, "com_local") == expected["com_local"] and
              _need(b, "inertia") == expected["inertia"] and
              _need(b, "physical_ids") == expected["physical_ids"] and
              _need(b, "multiplicity") == expected["multiplicity"])
    if full:
        sj = {j["id"]: j for j in graph["joints"]}
        r.add("actual_joint_ID_coverage", set(sj) == {j["id"] for j in contract["joints"]})
        for j in contract["joints"]:
            original = sj.get(j["id"], {})
            r.add("actual_joint_parent_child_"+j["id"],
                  j.get("parent") == original.get("parent") and
                  j.get("child") == original.get("child") and
                  j.get("kind") == original.get("kind"))
    return graph


def _actual_joint_closure(s, frame, contract, graph):
    bodies = {b["id"]: b for b in contract["bodies"]}
    joints = {j["id"]: j for j in contract["joints"]}
    for j in graph["joints"]:
        if j["id"] not in joints or j["parent"] not in bodies or j["child"] not in bodies:
            continue
        parent, child, obs = bodies[j["parent"]], bodies[j["child"]], joints[j["id"]]
        px, pz, pp = [_value(frame, parent, k) for k in ("x", "z", "phi")]
        cx, cz, cp = [_value(frame, child, k) for k in ("x", "z", "phi")]
        ex, ez = _transform(px, pz, pp, _need(j, "mount"))
        s.equal("actual_joint_origin_x_"+j["id"], cx, ex, 1e-7, 1e-7)
        s.equal("actual_joint_origin_z_"+j["id"], cz, ez, 1e-7, 1e-7)
        q = _value(frame, obs, "q")
        error = math.atan2(math.sin(cp-pp-q), math.cos(cp-pp-q))
        s.check("actual_joint_angle_closure_"+j["id"], abs(error) <= 1e-6, abs(error))
        relative = _value(frame, child, "omega")-_value(frame, parent, "omega")
        s.equal("actual_joint_relative_speed_"+j["id"], _value(frame, obs, "w"), relative)
        s.equal("actual_joint_relative_acceleration_"+j["id"], _value(frame, obs, "a"),
                _value(frame, child, "alpha")-_value(frame, parent, "alpha"))
def _supervisor_forces(frame, info, segment):
    targets = info['reference_segments'][segment-1].get('targets', [])
    result = []
    for target in targets:
        faces = [f for f in info['features'] if f['kind']=='face' and f['owner_id']==target['wheel'] and f['index']==target['face']]
        if len(faces)!=1: raise EvidenceMissing('Supervisor target face unavailable')
        height=faces[0]['point'][1]
        result.append(sum(frame[f['columns']['Fn']] for f in info['features'] if f['kind']=='face' and f['owner_id']==target['wheel'] and f['normal'][1]>.9 and abs(f['point'][1]-height)<1e-8))
    return (result+[0.,0.])[:2]


def _supervisor_frame(s, frame, local, info):
    """Replay held commits independently, retaining event-level loss evidence."""
    supervisor = info.get('sampled_supervisor')
    if not supervisor:
        return
    c = supervisor['columns']
    v = lambda key: frame[c[key]]
    tick = int(v('supervisorTick'))
    segment = int(frame[local['columns']['segment']])
    paused = frame[local['columns']['paused']] > .5
    previous = getattr(s, 'supervisor_previous', None)
    raw_load = frame[local['columns']['load_ok']]>.5
    raw_forces = _supervisor_forces(frame,info,segment)
    targets = info['reference_segments'][segment-1].get('targets', [])
    real_load = all(raw_forces[i]>=40 for i in range(len(targets)))
    s.check('supervisor_raw_load_from_actual_features', raw_load==real_load)
    state=getattr(s,'supervisor_events',None)
    if state is None:
        raw_failed=frame[local['columns']['failed']]>.5
        state=dict(loss=0,gain=0,entries=0,loaded_since=frame['time'] if raw_load else -1.,last_loss=-1.,last_gain=frame['time'] if raw_load else -1.,failures=int(raw_failed),first_failure=frame['time'] if raw_failed else -1.,last_failure=frame['time'] if raw_failed else -1.)
        s.supervisor_events=state
    s.check('supervisor_no_success_after_any_failure', not (v('failureSeen') > .5 and frame[local['columns']['task_done']] > .5))
    s.check('supervisor_failure_latch_matches_code', v('failureSeen') < .5 or frame[local['columns']['failure_code']] > .5)
    if previous is None:
        s.check('supervisor_initial_state', tick == 0 and segment == supervisor['initial_segment'] and paused)
    else:
        ptick, psegment, ppaused, pframe = previous
        # Validate the final row of the preceding event after all same-time
        # rows have been consumed; reconstruct events from raw load/stage,
        # independently of generated counters and timestamps.
        if frame['time']>pframe['time']+1e-8:
            for key,expected in (('loadLossCount',state['loss']),('loadGainCount',state['gain']),('stageEntryCount',state['entries']),('loadedSince',state['loaded_since']),('loadLossTime',state['last_loss']),('loadGainTime',state['last_gain']),('failureCount',state['failures']),('failureTime',state['first_failure']),('lastFailureEventTime',state['last_failure']),('failureSeen',float(state['failures']>0))):
                s.check('supervisor_raw_event_reconstruction_'+key, _close(pframe[c[key]],expected,1e-7,0))
        prior_load=pframe[local['columns']['load_ok']]>.5
        if raw_load!=prior_load:
            state['gain' if raw_load else 'loss']+=1
            state['last_gain' if raw_load else 'last_loss']=frame['time']
        if segment!=psegment:state['entries']+=1
        if raw_load!=prior_load or segment!=psegment:state['loaded_since']=frame['time'] if raw_load else -1.
        if frame[local['columns']['failed']]>.5 and pframe[local['columns']['failed']]<.5:
            state['failures']+=1;state['last_failure']=frame['time']
            if state['first_failure']<0:state['first_failure']=frame['time']
        s.check('supervisor_tick_monotone', tick >= ptick and tick <= ptick+1)
        s.check('supervisor_failure_latch_monotone', v('failureSeen') >= pframe[c['failureSeen']])
        for count, event_time in (('loadLossCount','loadLossTime'),('loadGainCount','loadGainTime'),('failureCount','lastFailureEventTime')):
            change = v(count)-pframe[c[count]]
            s.check('supervisor_event_counter_'+count, change >= 0 and change <= 1)
            if change:
                # Exact event rows must exist; a regular 20ms grid alone is
                # insufficient to justify absence of intertick loss/failure.
                s.check('supervisor_exact_event_evidence_'+count, abs(v(event_time)-frame['time']) <= 1e-7)
                if count == 'loadLossCount':
                    s.check('supervisor_loss_resets_dwell', v('loadedSince') < 0 or v('loadedSince') >= v('loadLossTime')-1e-8)
        if tick == ptick:
            s.check('supervisor_outputs_held_between_ticks', segment == psegment and paused == ppaused)
            s.check('supervisor_snapshot_held_after_first_iteration', all(_close(v(key),pframe[c[key]],1e-7,0) for key in c if key.startswith('snap') or key=='decisionTime'))
        else:
            t = v('decisionTime')
            if abs(pframe['time']-frame['time'])>1e-7:
                raise EvidenceMissing('Tick requires actual same-time left event row; regular sampled CSV cannot bind snapshots')
            s.check('supervisor_tick_time', abs(t-tick*.02) <= 1e-7 and abs(frame['time']-t) <= 1e-7)
            old = int(v('snapSegment'))
            s.check('supervisor_old_stage_snapshot', old == psegment)
            for snap,raw in (('snapPaused','paused'),('snapTracking','tracking_ok'),('snapSupport','old_support_ok'),('snapFailed','failed')):
                s.check('supervisor_raw_left_binding_'+snap, _close(v(snap),pframe[local['columns'][raw]],1e-7,0))
            for snap,raw in (('snapSaturated','saturated'),('snapFailureSeen','failureSeen'),('snapLoadedSince','loadedSince'),('snapClock','referenceClock')):
                s.check('supervisor_raw_left_binding_'+snap, _close(v(snap),pframe[c[raw]],1e-7,0))
            old_forces=_supervisor_forces(pframe,info,old)
            s.check('supervisor_raw_left_binding_force', all(_close(v(key),force,1e-5,1e-7) for key,force in zip(('snapForce','snapForce2'),old_forces)))
            owner, owner2 = int(v('snapOwner')), int(v('snapOwner2'))
            load = (owner == 0 or v('snapForce') >= 40) and (owner2 == 0 or v('snapForce2') >= 40)
            guard = owner == 0 or (load and v('snapLoadedSince') >= 0 and t-v('snapLoadedSince') >= .2)
            stage = info['reference_segments'][old-1]
            endpoint = v('snapClock') <= stage['start'] if info.get('direction') == 'down' else v('snapClock') >= stage['end']
            targets = stage.get('targets', [])
            owners = [['F_A','F_B','R_A','R_B'].index(x['wheel'])+1 for x in targets]
            s.check('supervisor_snapshot_target_binding', owner == (owners[0] if owners else 0) and owner2 == (owners[1] if len(owners)>1 else 0))
            s.check('supervisor_snapshot_load_guard', bool(v('snapLoad')>.5) == load and bool(v('snapGuard')>.5) == guard and bool(v('snapEndpoint')>.5) == endpoint)
            failed = v('snapFailureSeen') > .5 or v('snapFailed') > .5
            expected = old
            if not failed and endpoint and guard and load:
                expected = max(1,old-1) if info.get('direction') == 'down' else min(supervisor['stage_count'],old+1)
            expected_pause = failed or expected != old or v('snapTracking') < .5 or v('snapSupport') < .5 or v('snapSaturated') > .5 or (endpoint and not guard)
            s.check('supervisor_single_causal_commit', segment == expected and paused == expected_pause)
            if expected != old:
                s.check('supervisor_handoff_one_tick', paused and abs(v('stageEntryTime')-t) <= 1e-7)
                s.check('supervisor_continuous_dwell_at_advance', guard and load and (owner == 0 or t-v('snapLoadedSince') >= .2))
    s.supervisor_previous = (tick, segment, paused, frame)


def _supervisor_finish(s, info):
    supervisor=info.get('sampled_supervisor')
    if not supervisor or not getattr(s,'supervisor_previous',None):return
    c=supervisor['columns'];frame=s.supervisor_previous[3];state=s.supervisor_events
    for key,expected in (('loadLossCount',state['loss']),('loadGainCount',state['gain']),('stageEntryCount',state['entries']),('loadedSince',state['loaded_since']),('loadLossTime',state['last_loss']),('loadGainTime',state['last_gain']),('failureCount',state['failures']),('failureTime',state['first_failure']),('lastFailureEventTime',state['last_failure']),('failureSeen',float(state['failures']>0))):
        s.check('supervisor_final_raw_event_reconstruction_'+key,_close(frame[c[key]],expected,1e-7,0))


def check_csv(csv_path: Path, spec: dict, graph: dict, output_contract: dict, scene: str) -> dict:
    """One-pass finite-state validation of real simulator outputs.

    No raw feature history is stored. Event rows at the same timestamp remain
    distinct. Missing required outputs cannot be promoted to a successful gate.
    """
    r = Report("actual raw CSV, symmetric planar macro model; not whole-V11 acceptance")
    contract = output_contract
    s = Accumulator(r)
    def run():
        contract_local, info = _normalize_scene(contract, scene, spec, graph)
        # Scene contracts select only actual bodies/drives needed by component
        # probes. The full-vehicle scene cannot reduce its physics coverage.
        local = dict(contract_local)
        for key in ("bodies", "joints", "drives", "wheels", "hardware"):
            if key in info:
                local[key] = info[key]
        _validate_contract(r, local, info, scene)
        bound_graph = _bind_csv_graph(r, graph, local, scene)
        hardware_ids = {b["id"] for b in local["bodies"]}
        if local.get("_fixture"):
            hardware_ids.discard("chassis")
            bound_graph = dict(bound_graph, joints=[dict(j, mount=[0, 0, 0]) if j["kind"] == "active_revolute" else j for j in bound_graph["joints"]])
        if local["wheels"]:
            local["hardware"] = _frozen_hardware(spec, bound_graph, hardware_ids)
        names = set(_walk_columns(local))
        # Other scenes' per-feature columns are not demanded in this scene CSV.
        names = set(_walk_columns({k: v for k, v in local.items() if k not in ("scenes", "features", "transfers")})) \
            | set(_walk_columns(info)) | {"time"}
        first_mechanical = None
        for frame in _frames(csv_path, names):
            t = frame["time"]
            dt = 0. if s.last_time is None else t-s.last_time
            s.first_time = t if s.first_time is None else s.first_time
            s.last_time = t
            s.count += 1
            _supervisor_frame(s, frame, local, info)
            s.check("finite_output_interval", dt <= .0200001, dt)
            shaft = _electrical_frame(s, frame, spec, local, dt)
            if local["wheels"]:
                contact, loaded = _contact_frame(s, frame, spec, local, info)
                _transfers_frame(s, frame, local, info, loaded, dt)
            else:
                contact, loaded = {}, {}
            mechanical, poses, whole_x, whole_vx = _body_frame(s, frame, local, contact, dt)
            _whole_module_frame(s, frame, local, contact)
            _joint_frame(s, frame, local)
            _actual_joint_closure(s, frame, local, bound_graph)
            elastic = sum(c["elastic"] for c in contact.values())
            loss = sum(c["normal_loss"]+c["friction_loss"]+c["rolling_loss"] for c in contact.values())
            s.equal("mechanical_energy_sum", _total(frame, local, "mechanical_energy"), mechanical+elastic if local.get("_mechanical_includes_elastic") else mechanical)
            s.equal("actual_elastic_energy_sum", _total(frame, local, "elastic_energy"), elastic)
            s.equal("actual_ground_loss_sum", _total(frame, local, "ground_loss_power"), loss)
            s.integrate("ground_loss", t, loss, dt)
            complete_energy = mechanical+elastic
            if first_mechanical is None:
                first_mechanical = complete_energy
            work = s.integrals["shaft"]
            dissipated = s.integrals["ground_loss"]
            residual = complete_energy-first_mechanical-work+dissipated
            scale = max(abs(complete_energy-first_mechanical), abs(work), dissipated)
            s.check("mechanical_energy_balance", abs(residual) <= .5+.005*scale, abs(residual))
            if local["wheels"]:
                terrain = _need(info, "terrain")["points"]
                bodymap = {b["id"]: b for b in local["bodies"]}
                for shape in _need(local, "hardware"):
                    b = bodymap[shape["body_id"]]
                    pose = tuple(_value(frame, b, key) for key in ("x", "z", "phi"))
                    clearance = _shape_clearance(shape, pose, terrain)
                    s.check("actual_hardware_5mm_"+shape["id"], clearance >= .005-1e-8, clearance)
            if scene not in ("active_joint_energy", "one_twin_module_load_cycle", "flat_energy_and_brake"):
                _progress_frame(s, frame, local, info, poses, whole_x, whole_vx, dt)
            if scene == "flat_energy_and_brake":
                body = poses[_need(info, "chassis_id")]
                brake = _total(frame, local, "brake_active") > .5
                if brake and "brake_start_x" not in s.events:
                    s.events["brake_start_x"] = body["x"]
                if brake and abs(body["vx"]) <= .001:
                    s.events.setdefault("brake_stop_x", body["x"])
                if "brake_start_x" in s.events:
                    s.check("basic_flat_brake_02m", body["x"]-s.events["brake_start_x"] <= .2+1e-8)
            # Actual controller state can stop a scene, but cannot certify it.
            s.check("no_controller_failure", _total(frame, local, "failure_code") == 0)
            s.previous = frame
        _supervisor_finish(s, info)
        if s.count < 2 or s.last_time <= s.first_time:
            raise EvidenceMissing("No positive actual simulated duration")
        expected_stop = {"active_joint_energy": 12., "one_twin_module_load_cycle": 120.,
                         "flat_energy_and_brake": 40., "three_up": 600.,
                         "three_forward_down": 600., "twenty_up": 2400.,
                         "twenty_forward_down": 2400.}[scene]
        mission = scene not in ("active_joint_energy", "one_twin_module_load_cycle", "flat_energy_and_brake")
        if mission:
            completion_time = _total(s.previous, local, "completion_time")
            r.add("legitimate_actual_mission_termination", 0 < s.last_time <= expected_stop+1e-6
                  and _close(completion_time, s.last_time, 1e-6, 0)
                  and _total(s.previous, local, "task_done") > .5,
                  {"actual_end_s": s.last_time, "timeout_cap_s": expected_stop, "actual_completion_time": completion_time})
        else:
            r.add("complete_stopTime", _close(s.last_time, expected_stop, 1e-6, 0),
                  {"actual_end_s": s.last_time, "required_s": expected_stop})
        r.add("starts_at_zero", abs(s.first_time) <= 1e-8)
        r.add("actual_task_completion", _total(s.previous, local, "task_done") > .5)
        if scene == "flat_energy_and_brake":
            r.add("basic_flat_brake_actual_stop", "brake_stop_x" in s.events)
            chassis_body = next(b for b in local["bodies"] if b["id"] == info["chassis_id"])
            r.add("basic_flat_brake_final_stopped", abs(_value(s.previous, chassis_body, "vx")) <= .001)
        elif scene not in ("active_joint_energy", "one_twin_module_load_cycle"):
            exit_info = getattr(s, "diagnostics_exit", {})
            r.add("all_wheels_exit_plus_02_and_05cruise",
                  exit_info.get("cruise_m", -math.inf) >= .5-1e-8
                  and exit_info.get("trailing", -math.inf) >= info["exit_edge_x"]+.02)
            r.add("terminal_cruise_nonzero_008", all(exit_info.get(key, -math.inf) >= .08-1e-8
                  for key in ("terminal_vx", "terminal_main_COM_vx", "terminal_whole_COM_vx")))
            if info.get("direction") == "down":
                r.add("front_main_first_has_actual_evidence", any(w in s.events for w in info["front_wheel_ids"])
                      and any(w in s.events for w in info["rear_wheel_ids"]))
        for tr in _need(info, "transfers"):
            r.add("required_load_transfer_completed_"+tr["id"], "transfer_completed_"+tr["id"] in s.events)
        totalwait = sum(s.events.get("transfer_completed_"+tr["id"], s.last_time)
                        -s.events.get("transfer_requested_"+tr["id"], s.last_time)
                        for tr in info["transfers"])
        r.add("total_load_guard_wait_40s", totalwait <= 40+1e-8, totalwait)
        r.diagnostics.update(samples=s.count, first_time=s.first_time, last_time=s.last_time,
                             independently_integrated=s.integrals, actual_events=s.events,
                             memory="one previous row; finite extrema/integrals/event states")
    r.section("raw_complete_physical_outputs", run)
    s.finish()
    return r.result()


def _numeric_summary(path, contract, scene):
    raw = contract["scenes"][scene]
    if "metadata" not in raw:
        info = raw
        columns = _need(info, "numerical_columns")
        event_columns = _need(info, "event_columns")
        if not event_columns:
            raise EvidenceMissing("No required actual numerical events")
        names = set(columns.values()) | set(event_columns.values()) | {"time"}
        last, previous, events, count = None, None, {}, 0
        for frame in _frames(path, names):
            count += 1
            for ident, column in event_columns.items():
                if frame[column] > .5 and (previous is None or previous[column] <= .5):
                    if ident in events:
                        raise EvidenceMissing(f"Repeated numerical event {ident}")
                    events[ident] = frame["time"]
            previous = last = frame
        required_events = set(event_columns)
    else:
        meta, totals = raw["metadata"], raw["columns"]
        columns = {}
        for b in _need(raw, "bodies"):
            for key in ("x", "z"):
                columns["position_"+b["id"]+"_"+key] = _need(b["columns"], key)
        for key in ("battery_energy", "dump_energy", "mechanical_energy",
                    "shaft_energy", "contact_loss_energy", "loss_energy"):
            columns["energy_"+key] = _need(totals, key)
        event_columns = {"task_done": _need(totals, "task_done")}
        segment_col = _need(totals, "segment")
        names = set(columns.values()) | set(event_columns.values()) | {segment_col, "time"}
        if scene not in ("active_joint_energy", "one_twin_module_load_cycle", "flat_energy_and_brake"):
            names.add(_need(totals, "completion_time"))
        body = next(b for b in raw["bodies"] if b["id"] == "chassis")
        names.add(body["columns"]["vx"])
        plan = meta["reference_segments"]
        required_events = {"task_done"} | {"segment_enter_"+str(i+1) for i in range(len(plan))}
        if scene == "flat_energy_and_brake":
            required_events.add("actual_brake_stop")
        last, previous, events, count = None, None, {}, 0
        for frame in _frames(path, names):
            count += 1
            if frame[event_columns["task_done"]] > .5 and "task_done" not in events:
                events["task_done"] = frame["time"]
            segment = int(frame[segment_col])
            if plan and (previous is None or segment != int(previous[segment_col])):
                key = "segment_enter_"+str(segment)
                if key in events:
                    raise EvidenceMissing("Actual phase segment revisited")
                events[key] = frame["time"]
            if scene == "flat_energy_and_brake" and frame["time"] >= meta["brake_trigger_s"] \
                    and abs(frame[body["columns"]["vx"]]) <= .001:
                events.setdefault("actual_brake_stop", frame["time"])
            previous = last = frame
    if count < 2 or last is None:
        raise EvidenceMissing("Base/refined CSV actual samples absent")
    stop = {"active_joint_energy": 12., "one_twin_module_load_cycle": 120.,
            "flat_energy_and_brake": 40., "three_up": 600.,
            "three_forward_down": 600., "twenty_up": 2400.,
            "twenty_forward_down": 2400.}[scene]
    mission = scene not in ("active_joint_energy", "one_twin_module_load_cycle", "flat_energy_and_brake")
    if mission and "metadata" in raw:
        if not (0 < last["time"] <= stop+1e-6 and last[raw["columns"]["task_done"]] > .5
                and _close(last[raw["columns"]["completion_time"]], last["time"], 1e-6, 0)):
            raise EvidenceMissing("Missing actual successful completion timestamp, flag, or timeout compliance")
    elif not _close(last["time"], stop, 1e-6, 0):
        raise EvidenceMissing(f"Incomplete actual numerical CSV stopTime {last['time']} != {stop}")
    return last, events, columns, required_events


SIGNED_WORK_ADDENDUM = "signed-shaft-work-cycle-1percent-v1"


def _signed_work_column(output_contract, scene):
    """Only the current seven-model canonical output may opt into this addendum."""
    raw = _need(_need(output_contract, "scenes"), scene)
    if not isinstance(raw.get("metadata"), dict):
        raise EvidenceMissing("Signed-work addendum requires canonical scene metadata")
    columns = _need(raw, "columns")
    column = _need(columns, "shaft_energy")
    if column != "o_shaft_energy" or any(
            other != "shaft_energy" and alias == column for other, alias in columns.items()):
        raise EvidenceMissing("Signed-work addendum requires distinct canonical o_shaft_energy alias")
    return column


def _signed_work_stats(path, column):
    """Full raw CSV extrema, with the last actual duplicate row at each endpoint."""
    first_time = first_energy = last_time = last_energy = None
    low, high, count = math.inf, -math.inf, 0
    for frame in _frames(path, {"time", column}):
        t, energy = frame["time"], frame[column]
        if t < 0:
            raise EvidenceMissing("Signed-work actual time must be nonnegative")
        if first_time is None:
            first_time = t
        if t == first_time:
            first_energy = energy
        low, high = min(low, energy), max(high, energy)
        last_time, last_energy = t, energy
        count += 1
    if count < 2 or first_time is None or abs(first_time) > 1e-8 or last_time <= first_time:
        raise EvidenceMissing("Signed-work CSV requires a complete positive cycle starting at zero")
    final = last_energy-first_energy
    scale = max(abs(low-first_energy), abs(high-first_energy), abs(final), 1e-8)
    if not all(math.isfinite(v) for v in (final, scale)):
        raise EvidenceMissing("Nonfinite signed-work net change or cycle scale")
    return dict(first=first_energy, final=final, end=last_time, scale=scale, rows=count)


def _signed_work_unique_rows(path, column):
    """Collapse only exactly equal actual timestamps, preserving the last row."""
    previous = None
    for frame in _frames(path, {"time", column}):
        if previous is not None and frame["time"] != previous["time"]:
            yield previous
        previous = frame
    if previous is not None:
        yield previous


def _signed_work_grid(path, column, first, count):
    rows = iter(_signed_work_unique_rows(path, column))
    current = next(rows, None)
    for index in range(count):
        # The declared decimal output grid, not the nearby solver event time.
        target = round(index*.02, 2)
        while current is not None and current["time"] < target-1e-8:
            current = next(rows, None)
        matched, sole, candidates = None, None, 0
        while current is not None and current["time"] <= target+1e-8:
            candidates += 1
            sole = current
            if current["time"] == target:
                matched = current
            current = next(rows, None)
        if matched is None:
            if candidates != 1:
                raise EvidenceMissing(f"Missing or ambiguous actual signed-work grid point {target}")
            matched = sole
        # Exact grid sample has precedence; distinct nearby event rows are
        # never merged into it. Equal actual timestamps were collapsed above.
        energy = matched[column]-first
        if not math.isfinite(energy):
            raise EvidenceMissing("Nonfinite signed-work grid net change")
        yield energy


def _signed_work_comparison(base_csv, refined_csv, column):
    base, refined = (_signed_work_stats(path, column) for path in (base_csv, refined_csv))
    scale = max(base["scale"], refined["scale"], 1e-8)
    count = math.floor(min(base["end"], refined["end"])/.02)+1
    maximum = 0.
    for a, b in zip(_signed_work_grid(base_csv, column, base["first"], count),
                    _signed_work_grid(refined_csv, column, refined["first"], count)):
        error = abs(a-b)
        if not math.isfinite(error):
            raise EvidenceMissing("Nonfinite signed-work curve error")
        maximum = max(maximum, error)
    final_error = abs(base["final"]-refined["final"])
    if not math.isfinite(final_error):
        raise EvidenceMissing("Nonfinite signed-work final error")
    return dict(addendum=SIGNED_WORK_ADDENDUM, canonical_alias=column, scale_J=scale,
                max_curve_error_J=maximum, final_net_error_J=final_error,
                common_grid_count=count, common_grid_interval_s=.02,
                base_final_net_J=base["final"], refined_final_net_J=refined["final"],
                base_raw_rows=base["rows"], refined_raw_rows=refined["rows"],
                threshold_J=.01*scale, timestamp_match_tolerance_s=1e-8)


def compare_numerics(base_csv: Path, refined_csv: Path, output_contract: dict, scene: str,
                     numerical_addendum: str | None = None) -> dict:
    """Finite memory final-position/energy/event-time numerical trust checks."""
    r = Report("base/refined actual numerical agreement for declared planar scene")
    def run():
        if numerical_addendum is not None and numerical_addendum != SIGNED_WORK_ADDENDUM:
            raise EvidenceMissing("Unknown numerical acceptance addendum")
        signed_column = (_signed_work_column(output_contract, scene)
                         if numerical_addendum is not None else None)
        base, be, cols, required = _numeric_summary(base_csv, output_contract, scene)
        refined, revents, _, required_refined = _numeric_summary(refined_csv, output_contract, scene)
        mission = scene not in ("active_joint_energy", "one_twin_module_load_cycle", "flat_energy_and_brake")
        r.add("same_actual_end_time", abs(base["time"]-refined["time"]) <= (.25+1e-8 if mission else 1e-6))
        positions = [key for key in cols if key.startswith("position_")]
        energies = [key for key in cols if key.startswith("energy_")]
        if not positions or not energies:
            raise EvidenceMissing("Actual position and energy comparison columns absent")
        for key in positions:
            error = abs(base[cols[key]]-refined[cols[key]])
            r.add("final_position_5mm_"+key, error <= .005+1e-8, error)
        signed = None
        if signed_column is not None:
            if cols.get("energy_shaft_energy") != signed_column:
                raise EvidenceMissing("Canonical signed shaft-energy numerical interface absent")
            signed = _signed_work_comparison(base_csv, refined_csv, signed_column)
            r.diagnostics["signed_work"] = signed
            r.add("whole_curve_1percent_energy_shaft_energy",
                  signed["max_curve_error_J"] <= signed["threshold_J"], signed)
        for key in energies:
            if key == "energy_shaft_energy" and signed is not None:
                r.add("final_energy_1percent_"+key,
                      signed["final_net_error_J"] <= signed["threshold_J"], signed)
                continue
            a, b = base[cols[key]], refined[cols[key]]
            if key == "energy_battery_energy" and "metadata" in output_contract["scenes"][scene]:
                a, b = 2764800.-a, 2764800.-b
            error = abs(a-b)
            r.add("final_energy_1percent_"+key, error <= .01*max(abs(a), abs(b), 1e-8), error)
        r.add("actual_event_coverage", set(be) == set(revents) == required == required_refined,
              {"base": be, "refined": revents, "required": sorted(required)})
        for key in sorted(set(be) & set(revents)):
            error = abs(be[key]-revents[key])
            r.add("actual_event_time_025s_"+key, error <= .25+1e-8, error)
        r.diagnostics["memory"] = "one current/previous row and one timestamp per unique event"
    r.section("actual_numerical_csvs", run)
    return r.result()
