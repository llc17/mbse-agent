"""Versioned active twin-wheel tree descriptor; no execution or approval creation."""
from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path

APPROVED_SHA = "ec45de70bd6ffc114c5a9670c0db5000f5ee131a4144f6949fd5df7215c627ac"
ROOT = Path(__file__).resolve().parents[3]

class UnsupportedDesign(ValueError):
    """An explicit unsupported/invalid structure, never an inherited component PASS."""

def _primitive(kind, mass, centre, dimensions, source):
    if kind == "box":
        a,b,c = dimensions
        diag = [mass*(b*b+c*c)/12, mass*(a*a+c*c)/12, mass*(a*a+b*b)/12]
    elif kind == "cylinder_y":
        r,w = dimensions
        diag = [mass*(3*r*r+w*w)/12, mass*r*r/2, mass*(3*r*r+w*w)/12]
    elif kind == "ring_y":
        r,w = dimensions
        diag = [mass*(r*r/2+w*w/12),mass*r*r,mass*(r*r/2+w*w/12)]
    elif kind == "line_x":
        length, = dimensions
        diag = [0,mass*length*length/12,mass*length*length/12]
    else:
        raise UnsupportedDesign("UNSUPPORTED_PRIMITIVE:"+kind)
    return dict(source_id=source,kind=kind,m=mass,centre=centre,dimensions=dimensions,intrinsic_diagonal=diag,source=source)

def _body(identifier, kind, primitives, physical_ids, parent, mount):
    mass = sum(p["m"] for p in primitives)
    centre = [sum(p["m"]*p["centre"][i] for p in primitives)/mass for i in range(3)]
    tensor = [[0.0]*3 for _ in range(3)]
    for p in primitives:
        d = [p["centre"][i]-centre[i] for i in range(3)]
        for i in range(3):
            for j in range(3):
                tensor[i][j] += (p["intrinsic_diagonal"][i] if i == j else 0)+p["m"]*((sum(x*x for x in d) if i==j else 0)-d[i]*d[j])
    return dict(id=identifier,source_id=identifier,kind=kind,m=mass,com_local=centre,inertia=tensor,
                primitives=[dict(p, id=identifier+"_primitive_"+str(i), source_id=("payload" if p["source"].startswith("user mass20kg") else identifier+"_primitive_"+str(i)), role=("payload" if p["source"].startswith("user mass20kg") else "structure")) for i,p in enumerate(primitives)],physical_ids=physical_ids,multiplicity=len(physical_ids),parent=parent,mount=mount)

def build_design(spec, *, spec_sha):
    if spec_sha != APPROVED_SHA:
        raise UnsupportedDesign("UNBOUND_SPEC: byte digest differs from approved candidate")
    g,m = spec["geometry"],spec["mass"]
    ids = spec["identity"]
    if ids["physical_wheels"] != 8 or ids["physical_carriers"] != 4 or ids["pair_groups"] != ["F0","F1","R0","R1"]:
        raise UnsupportedDesign("UNSUPPORTED_TREE: approved two active carrier-pairs with two wheel-pairs each required")
    B,rho,r = g["fore_aft_hub_distance"],g["carrier_radius"],g["r"]
    p = m["additional_primitives"]
    chassis = []
    for row in m["rows"][:4]:
        chassis.append(_primitive("box",row["m"],[row["x"],0,row["z"]],p[row["id"]+"_box_m"],row["source"]))
    for side in (-1,1):
        for x in (-B/2,B/2):
            for kind,key,lane in (("carrier_actuator","carrier_actuator_y_cylinder","hub_motor_gear"),("brake","brake_y_cylinder","brake")):
                q=p[key]
                chassis.append(_primitive("cylinder_y",q["m_each"],[x,side*spec["lateral_solids"][lane]["center_y"],0],[q["r"],q["width"]],"AI hardware proxy "+kind))
    bodies=[_body("chassis","chassis",chassis,["chassis"],None,[0,0,0])]
    joints=[]
    for group,x in (("F",B/2),("R",-B/2)):
        prim=[]
        for side in (-1,1):
            prim.append(_primitive("line_x",1,[0,side*spec["lateral_solids"]["carrier_rod"]["center_y"],0],[g["wheel_center_distance"]],"AI thin-line rod inertia; finite capsule geometry separately"))
            q=p["carrier_feed_y_cylinder"]
            prim.append(_primitive("cylinder_y",q["m_each"],[0,side*spec["lateral_solids"]["feed_bearing"]["center_y"],0],[q["r"],q["width"]],"AI feed bearing proxy"))
            q=p["carrier_stator_y_cylinders"]
            for dx in (-rho,rho):
                prim.append(_primitive("cylinder_y",q["m_each"],[dx,side*spec["lateral_solids"]["wheel_stator"]["center_y"],0],[q["r"],q["width"]],"AI wheel stator allocated to carrier"))
        cid=group+"_carrier"
        bodies.append(_body(cid,"carrier_pair",prim,[group+"0",group+"1"],"chassis",[x,0,0]))
        joints.append(dict(id=cid+"_joint",parent="chassis",child=cid,kind="active_revolute",axis=[0,-1,0],mount=[x,0,0],multiplicity=2,drive_id=cid+"_drive"))
        for label,dx in (("A",rho),("B",-rho)):
            prim=[]
            for side in (-1,1):
                q=p["wheel_tyre_thin_ring"]
                prim.append(_primitive("ring_y",q["m_each"],[0,side*g["lane_y"],0],[q["r"],q["width"]],"AI ordinary tyre thin ring"))
                q=p["wheel_rotor_y_cylinder"]
                prim.append(_primitive("cylinder_y",q["m_each"],[0,side*g["lane_y"],0],[q["r"],q["width"]],"AI rotor allocated to wheel, stator excluded"))
            wid=group+"_"+label
            bodies.append(_body(wid,"wheel_pair",prim,[group+"0_"+label,group+"1_"+label],cid,[dx,0,0]))
            joints.append(dict(id=wid+"_joint",parent=cid,child=wid,kind="wheel_revolute",axis=[0,-1,0],mount=[dx,0,0],multiplicity=2,drive_id=wid+"_drive"))
    if abs(sum(b["m"] for b in bodies)-spec["requirements"]["total_mass"])>1e-8:
        raise UnsupportedDesign("MASS_CONFLICT")
    expected={"chassis":m["chassis_Jy"],"carrier_pair":2*m["carrier_each"]["Jy"],"wheel_pair":2*m["wheel_each"]["Jy"]}
    for b in bodies:
        if abs(b["inertia"][1][1]-expected[b["id"] if b["id"]=="chassis" else b["kind"]])>1e-9:
            raise UnsupportedDesign("INERTIA_CONFLICT:"+b["id"])
    connections=[]
    for j in joints:
        connections.extend([dict(id=j["id"]+"_frame",parent=j["parent"],child=j["child"],joint=j["id"],mount=j["mount"]),dict(id=j["drive_id"]+"_shaft",from_port=j["drive_id"]+".shaft",to_port=j["id"]+".axis"),dict(id=j["drive_id"]+"_support",from_port=j["drive_id"]+".support",to_port=j["id"]+".support")])
    return dict(schema="v11-active-cluster-design-1",candidate_id=spec["candidate_id"],spec_sha256=spec_sha,
                family="active_twin_wheel_tree",representation=dict(mode="symmetric_planar_pair",physical_carriers=4,physical_wheels=8,physical_motors=12,body_instances=7,dof=9,no_independent_lateral_dof=True,axis="math-minus-y"),
                graph=dict(bodies=bodies,joints=joints,connections=connections),mass_ledger=dict(total=sum(b["m"] for b in bodies),payload=20,body_count=len(bodies)),
                geometry=g,lateral_solids=spec["lateral_solids"],control=spec["control"],physics=spec["physics"],requirements=spec["requirements"],path=spec["path"],thresholds=spec["execution_contract_proposal"]["proposed_thresholds"],
                hardware_status="UNKNOWN_AI_PROXY_NOT_SUPPLIER_ACCEPTANCE",fail_safe_brake_status="UNKNOWN_NOT_MODELLED",support_scope="Only approved active twin-wheel symmetric planar tree; unsupported other active/track graphs rejected")

def load_design(path):
    raw=Path(path).read_bytes()
    return build_design(json.loads(raw),spec_sha=hashlib.sha256(raw).hexdigest())
