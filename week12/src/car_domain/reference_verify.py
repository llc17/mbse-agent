"""Independent source algebra and historical trajectory checks, never vehicle PASS."""
from __future__ import annotations
from bisect import bisect_left
import csv
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping
from .reference_profile import (
    PROFILE, REPO_ROOT, WEEK12_ROOT, RESULT_ROOT, SOURCE_PATH, SOURCE_SHA, MODEL_NAME,
    CONTRACT_SHA, AUTH_SHA, SCOPE_ID, LIMITATIONS, CORE, parameter_descriptors,
    parameter_values, derived_values, sha256_bytes, source_bytes, validate_reference_spec,
)

BASELINE_CSV_PATH = "week12/results/user-4w2n/original-20260922-01/runs/full/observed.csv"
BASELINE_CSV_SHA = "59dd0ac18299512b9f639f506699d0d850b253d571c2cc76f63b9c0bd2cb8023"
REQUIRED_COLUMNS = ("time","x","z","v","vz","psi","omega_psi","x_n","z_n","h_n","Fz_n","Fx_n",
                    "x_f","z_f","h_f","Fz_f","Fx_f","x_r","z_r","h_r","Fz_r","Fx_r",
                    "power_factor","target_speed","nose_on_ground")
TRAJECTORY_TOLERANCES = {"x":.005,"z":.005,"x_n":.005,"x_f":.005,"x_r":.005,
                         "v":.01,"vz":.01,"psi":.01}
_DECL = re.compile(r"(?m)^[ \t]*parameter[ \t]+(Real|Integer)[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*([^;\r\n]+);")


class ReferenceUnknown(ValueError):
    """Insufficient or ambiguous engineering evidence."""


def _inside_work(path: str|Path) -> Path:
    resolved=Path(path).resolve()
    try:
        relative=resolved.relative_to(WEEK12_ROOT.resolve())
    except ValueError as exc:
        raise ValueError('reference evidence must remain within week12') from exc
    parts=relative.parts
    in_results=len(parts)>2 and parts[0]=='results' and parts[1]!='user-4w2n'
    in_version=(len(parts)>4 and parts[0]=='projects' and parts[2]=='versions'
                and re.fullmatch(r'prj_[0-9a-f]+',parts[1]) and re.fullmatch(r'v[0-9]+',parts[3]))
    if not (in_results or in_version) or 'source-v1' in parts:
        raise ValueError('new reference evidence requires results or exact reserved version directory')
    return resolved


def _read_csv(path: Path, columns: tuple[str,...]) -> list[dict[str,float]]:
    with path.open("r",encoding="utf-8-sig",newline="") as handle:
        reader=csv.DictReader(handle)
        header=reader.fieldnames
        if header is None or len(header)!=len(set(header)) or not set(columns).issubset(header):
            raise ReferenceUnknown("missing/duplicate required CSV columns")
        rows=[]
        for line,row in enumerate(reader,2):
            if None in row or any(row.get(k) is None for k in header):
                raise ReferenceUnknown("malformed CSV row "+str(line))
            converted={}
            for name in header:
                text=row[name]
                # Output Boolean must be actual typed 0/1 or true/false; never truthy arbitrary text.
                if name=="nose_on_ground" and text.lower() in ("true","false"):
                    value=float(text.lower()=="true")
                else:
                    try:
                        value=float(text)
                    except (TypeError,ValueError) as exc:
                        raise ReferenceUnknown("nonnumeric CSV field "+name) from exc
                if not math.isfinite(value):
                    raise ReferenceUnknown("nonfinite CSV field "+name)
                if name=="nose_on_ground" and value not in (0.,1.):
                    raise ReferenceUnknown("invalid Boolean nose_on_ground")
                converted[name]=value
            if rows and converted["time"]<rows[-1]["time"]:
                raise ReferenceUnknown("CSV time not nondecreasing")
            rows.append(converted)
    if len(rows)<3 or rows[0]["time"]!=0 or rows[-1]["time"]!=120:
        raise ReferenceUnknown("CSV must actually cover complete positive-duration 0..120s")
    return rows


def _source_parameter_readback(raw: bytes) -> dict[str,int|float]:
    declarations=[{"type":m.group(1),"name":m.group(2),"expression":m.group(3).strip()}
                  for m in _DECL.finditer(raw.decode("utf-8"))]
    expected=[{"type":d["type"],"name":d["name"],"expression":d["expression"]}
              for d in parameter_descriptors()]
    if declarations!=expected:
        raise ValueError("generated/source actual parameter declarations mismatch")
    # Parse actual literals rather than trusting generator provenance or PASS.
    values={}
    for item,descriptor in zip(declarations,parameter_descriptors()):
        if descriptor["kind"]=="literal":
            values[item["name"]]=int(item["expression"]) if item["type"]=="Integer" else float(item["expression"])
    if values!=parameter_values():
        raise ValueError("reference actual model numeric parameter mismatch")
    return derived_values(values)


def _terrain(x: float, p: Mapping[str,float], nose: bool) -> float:
    if x<p["x_start_up"]:
        return 0.
    if x<p["x_end_up"]:
        step=math.floor((x-p["x_start_up"])/p["stepD"]+(0. if nose else 1e-3))
        return (step+(1 if nose else 0))*p["stepH"]
    if x<p["x_start_down"]:
        return p["H_peak"]
    if x<p["x_end_down"]:
        step=math.floor((x-p["x_start_down"])/p["stepD"]+(0. if nose else 1e-3))
        value=p["H_peak"]-(step+(1 if nose else 0))*p["stepH"]
        return value if nose else max(0.,value)
    return 0.


def _algebra(rows: list[dict[str,float]], p: Mapping[str,float]) -> dict[str,Any]:
    max_error={}
    first_failures=[]
    for index,row in enumerate(rows):
        x,z,psi=row["x"],row["z"],row["psi"]
        c,s=math.cos(psi),math.sin(psi)
        expected={"x_f":x+p["a"]*c,"z_f":z+p["a"]*s,
                  "x_r":x-p["b"]*c,"z_r":z-p["b"]*s,
                  "x_n":x+p["L_nose_CoM"]*c-(p["h_nose"]-p["R_main"])*s,
                  "z_n":z+p["L_nose_CoM"]*s+(p["h_nose"]-p["R_main"])*c,
                  "target_speed":.4}
        for group in ("n","f","r"):
            expected["h_"+group]=_terrain(row["x_"+group],p,group=="n")
        nose=row["z_n"]<row["h_n"]+p["R_nose"]
        expected["nose_on_ground"]=float(nose)
        for group in ("n","f","r"):
            radius=p["R_nose"] if group=="n" else p["R_main"]
            fz=max(0.,p["k_ground"]*(row["h_"+group]+radius-row["z_"+group])-p["c_ground"]*row["vz"])
            expected["Fz_"+group]=fz if group!="n" or nose else 0.
        speed=row["v"]
        pf=1. if speed<.4 else (1.-(speed-.4)/.2 if speed<.6 else -1.)
        expected["power_factor"]=pf
        for group in ("n","f","r"):
            torque=p["T_nose_max"] if group=="n" else p["T_main_max"]
            radius=p["R_nose"] if group=="n" else p["R_main"]
            fx=pf*min(torque/radius,p["mu_s"]*row["Fz_"+group])
            expected["Fx_"+group]=fx if group!="n" or nose else 0.
        for name,value in expected.items():
            error=abs(row[name]-value)
            max_error[name]=max(max_error.get(name,0.),error)
            limit=0. if name=="nose_on_ground" else 1e-6+1e-6*abs(value)
            if error>limit and len(first_failures)<12:
                first_failures.append({"row":index,"time":row["time"],"column":name,
                                       "actual":row[name],"expected":value,"abs_error":error,"limit":limit})
    return {"status":"FAIL_IN_REFERENCE_SCOPE" if first_failures else "PASS_IN_REFERENCE_SCOPE",
            "checked_rows":len(rows),"absolute_tolerance":1e-6,"relative_tolerance":1e-6,
            "max_absolute_errors":max_error,"first_failures":first_failures,
            "scope":"PURE_SOURCE_ALGEBRA_ONLY_NOT_PHYSICAL_VALIDITY_OR_CONVERGENCE"}


def _groups(rows: list[dict[str,float]]) -> tuple[list[float],list[list[dict[str,float]]]]:
    times=[]
    groups=[]
    for row in rows:
        if times and row["time"]==times[-1]:
            groups[-1].append(row)
        else:
            times.append(row["time"])
            groups.append([row])
    return times,groups


def _branch_equal(group: list[dict[str,float]], names: tuple[str,...]) -> bool:
    return all(all(abs(row[name]-group[0][name])<=1e-10 for name in names) for row in group[1:])


def _sample(times: list[float], groups: list[list[dict[str,float]]], t: float,
            names: tuple[str,...]) -> list[dict[str,float]]:
    pos=bisect_left(times,t)
    if pos<len(times) and times[pos]==t:
        return groups[pos]
    if pos==0 or pos==len(times):
        raise ReferenceUnknown("comparison point outside actual covered domain")
    # Distinct times interpolate post-event state on left to pre-event state on right.
    left,right=groups[pos-1][-1],groups[pos][0]
    fraction=(t-times[pos-1])/(times[pos]-times[pos-1])
    return [{name:left[name]+fraction*(right[name]-left[name]) for name in names}]


def _replay(new: list[dict[str,float]], old: list[dict[str,float]]) -> dict[str,Any]:
    names=tuple(TRAJECTORY_TOLERANCES)
    old_times,old_groups=_groups(old)
    new_times,new_groups=_groups(new)
    expected_grid=[i*.05 for i in range(2401)]
    # Float spelling differs around e.g. 0.15; explicit exact 0.05-grid tolerance is time only.
    if len(old_times)!=2401 or any(abs(a-b)>1e-9 for a,b in zip(old_times,expected_grid)):
        raise ReferenceUnknown("historical reference grid not complete 0..120/.05")
    if len(new_times)<2401 or any(not any(abs(t-q)<=1e-9 for q in new_times) for t in old_times):
        raise ReferenceUnknown("new actual output lacks complete original time grid")
    errors={name:0. for name in names}
    branches={"exact_left_right_pairs":0,"trajectory_branch_invariant_pairs":0,
              "interpolated_same_branch_pairs":0}
    for t,old_group in zip(old_times,old_groups):
        # Snap only the timestamp spelling (<=1ns); no state extrapolation.
        pos=bisect_left(new_times,t)
        candidates=[p for p in (pos-1,pos) if 0<=p<len(new_times) and abs(new_times[p]-t)<=1e-9]
        sample_time=new_times[min(candidates,key=lambda p:abs(new_times[p]-t))] if candidates else t
        new_group=_sample(new_times,new_groups,sample_time,names)
        if len(old_group)==len(new_group):
            pairs=list(zip(old_group,new_group))
            branches["exact_left_right_pairs"]+=len(pairs)
        elif _branch_equal(old_group,names) and _branch_equal(new_group,names):
            pairs=[(old_group[0],new_group[0])]
            branches["trajectory_branch_invariant_pairs"]+=1
        else:
            raise ReferenceUnknown("ambiguous unequal duplicate-time left/right trajectory branches")
        for previous,current in pairs:
            for name in names:
                errors[name]=max(errors[name],abs(previous[name]-current[name]))
    violations={name:error for name,error in errors.items() if error>TRAJECTORY_TOLERANCES[name]}
    return {"status":"FAIL_IN_REFERENCE_SCOPE" if violations else "PASS",
            "grid_points":len(old_times),"covered_interval_s":[0.,120.],
            "tolerances":dict(TRAJECTORY_TOLERANCES),"max_absolute_errors":errors,
            "violations":violations,"event_branches":branches,
            "alignment":"FULL_COMMON_ACTUAL_DOMAIN_NO_EXTRAPOLATION"}


def _crossing(rows: list[dict[str,float]], column: str, threshold: float) -> dict[str,float]:
    crossings=[]
    for left,right in zip(rows,rows[1:]):
        if left[column]<threshold<=right[column]:
            if right["time"]<=left["time"] or right[column]<=left[column]:
                raise ReferenceUnknown("ambiguous zero-duration crossing")
            fraction=(threshold-left[column])/(right[column]-left[column])
            crossings.append({"time":left["time"]+fraction*(right["time"]-left["time"]),
                              "psi":left["psi"]+fraction*(right["psi"]-left["psi"])})
        elif right[column]<threshold<=left[column]:
            raise ReferenceUnknown("reverse crossing invalidates forward passage evidence")
    if len(crossings)!=1:
        raise ReferenceUnknown("missing or ambiguous forward crossing: "+column+" "+str(threshold))
    return crossings[0]


def _events(new: list[dict[str,float]], old: list[dict[str,float]], p: Mapping[str,float]) -> dict[str,Any]:
    boundaries=("x_start_up","x_end_up","x_start_down","x_end_down")
    events={}
    violations=[]
    for boundary in boundaries:
        crossings={group:_crossing(new,"x_"+group,p[boundary]) for group in ("n","f","r")}
        events[boundary]={"x_m":p[boundary],"crossings":crossings}
        if crossings["f"]["time"]>=crossings["r"]["time"]:
            violations.append("front-main not first at "+boundary)
    event_deltas={}
    for boundary in boundaries:
        event_deltas[boundary]={}
        for group in ('n','f','r'):
            previous=_crossing(old,'x_'+group,p[boundary])
            delta=abs(events[boundary]['crossings'][group]['time']-previous['time'])
            event_deltas[boundary][group]=delta
            if delta>.25:violations.append('boundary regression >.25s: '+boundary+':'+group)
    exit_deltas=event_deltas['x_end_down']
    exit_time=events["x_end_down"]["crossings"]["r"]["time"]
    after=[row for row in new if row["time"]>=exit_time]
    final=new[-1]
    continuation=(len(after)>=2 and final["x_r"]>p["x_end_down"] and final["x"]>after[0]["x"]
                  and final["v"]>0 and all(row["x_f"]>row["x_r"] for row in after))
    if not continuation:
        violations.append("rear exit and continued forward travel missing")
    descend=[row for row in new if p["x_start_down"]<=row["x_f"]<p["x_end_down"]]
    head_down_observed=bool(descend) and any(row["psi"]<0 for row in descend)
    if not head_down_observed:
        violations.append("head-down pitch not observed in reference descent")
    return {"status":"FAIL_IN_REFERENCE_SCOPE" if violations else "PASS",
            "boundaries":events,"exit_reference_time_deltas_s":exit_deltas,"all_boundary_reference_time_deltas_s":event_deltas,
            "exit_time_tolerance_s":.25,"continued_forward_travel":continuation,
            "head_down_pitch_observed":head_down_observed,"final_speed_m_s":final["v"],
            "final_rear_x_m":final["x_r"],"violations":violations,
            "scope":"OBSERVED_REFERENCE_TRAJECTORY_NOT_VEHICLE_CERTIFICATION"}


def check_reference_csv(csv_path: str|Path, spec: Mapping[str,Any],
                        generated_model_path: str|Path,
                        output_report_path: str|Path|None=None) -> dict[str,Any]:
    report={"schema_version":"v11-reference-independent-check-1","profile":PROFILE,
            "execution_scope_id":SCOPE_ID,"status":"UNKNOWN","reference_replay_status":"UNKNOWN",
            "source_algebra_status":"UNKNOWN","vehicle_task_status":"UNKNOWN",
            "physical_acceptance_status":"NOT_ACCEPTED_KNOWN_SIMPLIFICATION",
            "numeric_convergence":"NOT_VERIFIED","whole_V11_complete":False,
            "limitations":list(LIMITATIONS),"issues":[]}
    try:
        validation=validate_reference_spec(spec)
        if not validation["ready"]:
            raise ValueError("reference spec not ready")
        model=_inside_work(generated_model_path)
        actual_csv=_inside_work(csv_path)
        original=source_bytes()
        generated=model.read_bytes()
        if generated!=original or sha256_bytes(generated)!=SOURCE_SHA:
            raise ValueError("new actual generated source/body hash mismatch")
        p=_source_parameter_readback(original)
        if _source_parameter_readback(generated)!=p:
            raise ValueError("generated/source parameter readback mismatch")
        baseline=(REPO_ROOT/BASELINE_CSV_PATH)
        baseline_raw=baseline.read_bytes()
        if sha256_bytes(baseline_raw)!=BASELINE_CSV_SHA:
            raise ValueError("historical reference CSV drift")
        contract=json.loads((RESULT_ROOT/"reference-core-chain-proposal-final.json").read_bytes())
        if (contract["baseline"]["reference_csv_path"]!=BASELINE_CSV_PATH
                or contract["baseline"]["reference_csv_sha256"]!=BASELINE_CSV_SHA):
            raise ValueError("historical reference binding mismatch")
        new=_read_csv(actual_csv,REQUIRED_COLUMNS)
        old=_read_csv(baseline,("time",)+tuple(TRAJECTORY_TOLERANCES))
        report["binding"]={"spec_digest":validation["spec_digest"],"contract_sha256":CONTRACT_SHA,
                           "authorization_sha256":AUTH_SHA,"source_path":SOURCE_PATH,
                           "source_body_sha256":sha256_bytes(original),"generated_model_path":str(model),
                           "generated_body_sha256":sha256_bytes(generated),"new_csv_path":str(actual_csv),
                           "new_csv_sha256":sha256_bytes(actual_csv.read_bytes()),
                           "baseline_csv_path":BASELINE_CSV_PATH,"baseline_csv_sha256":BASELINE_CSV_SHA}
        report["actual_model_parameter_readback"]=p
        report["xml_actual_parameter_readback_status"]="REQUIRED_EXTERNAL_ROUTE_GATE"
        report["dynamic_columns_present"]=list(REQUIRED_COLUMNS)
        algebra=_algebra(new,p)
        replay=_replay(new,old)
        events=_events(new,old,p)
        report.update(source_algebra=algebra,reference_replay=replay,reference_events=events,
                      source_algebra_status=algebra["status"],reference_replay_status=replay["status"])
        passed=(algebra["status"]=="PASS_IN_REFERENCE_SCOPE" and replay["status"]=="PASS"
                and events["status"]=="PASS")
        report["status"]="PASS_IN_REFERENCE_SCOPE" if passed else "FAIL_IN_REFERENCE_SCOPE"
    except ReferenceUnknown as exc:
        report["issues"].append({"kind":"MISSING_OR_AMBIGUOUS_REFERENCE_EVIDENCE","message":str(exc)})
    except (ValueError,TypeError,KeyError,OSError,UnicodeError) as exc:
        report["status"]="FAIL_IN_REFERENCE_SCOPE"
        report["issues"].append({"kind":"REFERENCE_BINDING_OR_FORMAT_FAILURE","message":str(exc)})
    if output_report_path is not None:
        output=_inside_work(output_report_path)
        if output.exists() or not output.parent.is_dir():
            raise ValueError("reference report requires absent path in an existing new work directory")
        output.write_text(json.dumps(report,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    return report
