"""Bound user-reference input; this profile never accepts vehicle physics."""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[3]
WEEK12_ROOT = REPO_ROOT / "week12"
RESULT_ROOT = WEEK12_ROOT / "results/s3-c-active-eightwheel-execution-20261009-01"
PROFILE = "REFERENCE_TEMPLATE_GENERATION_ONLY"
SOURCE_PATH = "week12/models/user-4w2n/source-v1/运动/6lun_zhenggeliucheng.mo"
SOURCE_SHA = "a9bcd9d260342bde94d74b26b76af586682cd70c2a353d611e68bb5cd44b452f"
MODEL_NAME = "ZLTECH_Full_Mission"
CONTRACT_SHA = "49b6d88da1d3155aae6e9ee7bb64ef9ee84f9c73ac0903a477bbe6552b999c39"
AUTH_SHA = "1df784103cc626a73ade56e68593b870064a3f369073a0c96d6d7c73a4388b84"
OLD_LEDGER_SHA = "3257dead0c2472583103d8d48bf48ca5761236d19604d37c684e4495627455bf"
SCOPE_ID = "reference-core-ad9eb814-f49d-4467-864c-7fd4df602ba5"
from .reference_execution import EXECUTION, execution_authorization
CONTRACT_SHA = EXECUTION['proposal_sha256']
AUTH_SHA = EXECUTION['authorization_sha256']
OLD_LEDGER_SHA = EXECUTION['snapshot_sha256']
SCOPE_ID = EXECUTION['scope_id']
SPEC_SCHEMA = "v11-reference-spec-1"
TASK_NAME = "ascend_and_forward_head_down_front_first_descend"
CORE = {"total_mass": (85, "kg"), "payload_mass": (20, "kg"),
        "step_height": (.15, "m"), "step_depth": (.28, "m"),
        "continuous_steps": (20, "count"), "passage_task": (TASK_NAME, "task")}
LIMITATIONS = [
    "Original m=85 is one aggregate scalar; independent 20kg payload body/inertia NOT_VERIFIED.",
    "Three 2D equivalent contacts do not represent six independent wheel bodies.",
    "Main and nose wheels use different terrain floor formulas; known solid overlap remains.",
    "Horizontal traction lacks stair-edge contact normals, spin/slip and per-body conservation.",
    "wheelbase is declared-unused; actual axle spacing is a+b.",
    "Hardware, battery/endurance, braking, small-step gate, real contact and numerical convergence NOT_VERIFIED.",
    "Vehicle task UNKNOWN; physical NOT_ACCEPTED_KNOWN_SIMPLIFICATION; no vehicle publication.",
]
_PARAMETERS = [
    ("Real","m","85.0","literal"), ("Real","g","9.81","literal"),
    ("Real","I_pitch","20.0","literal"), ("Real","R_main","0.127","literal"),
    ("Real","R_nose","0.07","literal"), ("Real","T_main_max","60.0","literal"),
    ("Real","T_nose_max","25.0","literal"), ("Real","wheelbase","0.45","literal"),
    ("Real","L_nose_ext","0.32","literal"), ("Real","h_nose","0.19","literal"),
    ("Real","a","0.22","literal"), ("Real","b","0.23","literal"),
    ("Real","L_nose_CoM","a + L_nose_ext","derived"), ("Real","stepH","0.15","literal"),
    ("Real","stepD","0.28","literal"), ("Integer","n_steps","20","literal"),
    ("Real","x_start_up","2.0","literal"),
    ("Real","x_end_up","x_start_up + n_steps * stepD","derived"),
    ("Real","len_flat","2.0","literal"),
    ("Real","x_start_down","x_end_up + len_flat","derived"),
    ("Real","x_end_down","x_start_down + n_steps * stepD","derived"),
    ("Real","H_peak","n_steps * stepH","derived"),
    ("Real","k_ground","4.0e4","literal"), ("Real","c_ground","3.0e3","literal"),
    ("Real","mu_s","0.85","literal"),
]


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def digest_json(value: Any) -> str:
    return sha256_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"), allow_nan=False).encode("utf-8"))


def source_bytes() -> bytes:
    raw = (REPO_ROOT / SOURCE_PATH).read_bytes()
    if sha256_bytes(raw) != SOURCE_SHA:
        raise ValueError("reference original source drift")
    return raw


def reference_approval() -> dict[str, Any]:
    raw = (RESULT_ROOT / "reference-core-chain-proposal-final.json").read_bytes()
    receipt_raw = (RESULT_ROOT / "reference-core-chain-authorization.json").read_bytes()
    receipt = json.loads(receipt_raw)
    if (sha256_bytes(raw) != "49b6d88da1d3155aae6e9ee7bb64ef9ee84f9c73ac0903a477bbe6552b999c39" or sha256_bytes(receipt_raw) != "1df784103cc626a73ade56e68593b870064a3f369073a0c96d6d7c73a4388b84"
            or receipt.get("status") != "APPROVED"
            or receipt.get("proposal_sha256") != "49b6d88da1d3155aae6e9ee7bb64ef9ee84f9c73ac0903a477bbe6552b999c39"
            or receipt.get("profile") != PROFILE or receipt.get("scope_id") != "reference-core-ad9eb814-f49d-4467-864c-7fd4df602ba5"
            or receipt.get("reference_source_sha256") != SOURCE_SHA
            or receipt.get("old_ledger_sha256") != "3257dead0c2472583103d8d48bf48ca5761236d19604d37c684e4495627455bf"
            or receipt.get("old_ledger_snapshot_file") != "ledger-before-reference-core-chain.json"):
        raise ValueError("reference authorization/contract binding mismatch")
    if sha256_bytes((RESULT_ROOT / "ledger-before-reference-core-chain.json").read_bytes()) != "3257dead0c2472583103d8d48bf48ca5761236d19604d37c684e4495627455bf":
        raise ValueError("reference retired ledger snapshot drift")
    source_bytes()
    contract = json.loads(raw)
    if (contract["baseline"]["reference_source"] != SOURCE_PATH
            or contract["baseline"]["reference_source_sha256"] != SOURCE_SHA
            or contract["requirements"]["source_parameters"] != [
                {"type":t,"name":n,"expression":e,"kind":k,"origin":"用户原模型","source":SOURCE_PATH}
                for t,n,e,k in _PARAMETERS]):
        raise ValueError("reference source parameter contract mismatch")
    return receipt


def authorization() -> dict[str, Any]:
    reference_approval()  # Original physical/template approval remains unchanged.
    return execution_authorization()


def parameter_descriptors() -> list[dict[str, Any]]:
    return [{"type":t, "name":n, "expression":e, "kind":k, "literal":k=="literal",
             "derived":k=="derived", "source":SOURCE_PATH,
             "declared_unused":n=="wheelbase"} for t,n,e,k in _PARAMETERS]


def parameter_values() -> dict[str, int | float]:
    return {n: int(e) if t=="Integer" else float(e)
            for t,n,e,k in _PARAMETERS if k=="literal"}


def derived_values(values: Mapping[str, int | float]) -> dict[str, int | float]:
    p = dict(values)
    p["L_nose_CoM"] = p["a"] + p["L_nose_ext"]
    p["x_end_up"] = p["x_start_up"] + p["n_steps"] * p["stepD"]
    p["x_start_down"] = p["x_end_up"] + p["len_flat"]
    p["x_end_down"] = p["x_start_down"] + p["n_steps"] * p["stepD"]
    p["H_peak"] = p["n_steps"] * p["stepH"]
    return p


def _source(item: Mapping[str, Any], sources: dict[str, str]) -> None:
    sid, snippet = item.get("source_id"), item.get("source_text")
    if (not isinstance(sid,str) or sid not in sources or not isinstance(snippet,str)
            or not snippet.strip() or snippet not in sources[sid]):
        raise ValueError("exact user source substring required")


_NUMBER = r"(?<![A-Za-z0-9_.+\-])[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_UNITS = {"kg": r"(?:kg(?![A-Za-z/])|千克(?!每)|公斤(?!每))", "m": r"(?:m(?![A-Za-z/²³])|米(?!每|/|²|³))",
          "count": r"(?:count(?![A-Za-z])|steps?(?![A-Za-z])|级)"}


def _numeric_source(snippet: str, value: float, unit: str) -> None:
    matches = re.finditer("(" + _NUMBER + r")\s*" + _UNITS[unit], snippet, re.I)
    if not any(math.isclose(float(m.group(1)), value, abs_tol=1e-12, rel_tol=0) for m in matches):
        raise ValueError("value with matching adjacent unit absent from exact source")


def _check_explicit_conflicts(sources: dict[str,str]) -> None:
    labels = {"total_mass":r"(?:总质量|总重|total_mass|total mass)",
              "payload_mass":r"(?:设备载荷|载荷|payload_mass|payload mass)",
              "step_height":r"(?:台阶高度|台阶高|楼梯高度|step_height|step height)",
              "step_depth":r"(?:台阶深度|台阶深|楼梯深度|step_depth|step depth)",
              "continuous_steps":r"(?:连续级数|连续台阶|continuous_steps|continuous steps)"}
    for text in sources.values():
        for name,label in labels.items():
            value,unit = CORE[name]
            pattern = label + r"\s*(?:为|是|[:=：])?\s*(" + _NUMBER + r")\s*" + _UNITS[unit]
            for match in re.finditer(pattern,text,re.I):
                if not math.isclose(float(match.group(1)),value,abs_tol=1e-12,rel_tol=0):
                    raise ValueError("conflicting explicit user source: "+name)


def validate_reference_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    try:
        authorization()
        expected = {"schema_version","profile","reference_ref","parameters","sources",
                    "requirements","design_permission","constraints","preferences","ideas","assumptions"}
        if not isinstance(spec,Mapping) or set(spec) != expected or spec["schema_version"] != SPEC_SCHEMA:
            raise ValueError("unsupported reference schema/fields")
        if spec["profile"] != PROFILE or spec["reference_ref"] != {"path":SOURCE_PATH,"sha256":SOURCE_SHA}:
            raise ValueError("NEEDS_COMBINED_CONFIRMATION: reference profile/source changed")
        params = spec["parameters"]
        frozen = parameter_values()
        if not isinstance(params,dict) or set(params) != set(frozen):
            raise ValueError("exact 20 literal reference parameters required")
        for name,value in frozen.items():
            actual=params[name]
            if (isinstance(actual,bool) or not isinstance(actual,(int,float)) or not math.isfinite(actual)
                    or actual != value or (name=="n_steps" and not isinstance(actual,int))):
                raise ValueError("NEEDS_COMBINED_CONFIRMATION: reference parameter changed: "+name)
        declared=spec["sources"]
        if not isinstance(declared,list) or not declared or any(
            not isinstance(s,dict) or set(s)!={"id","text"} or not isinstance(s["id"],str)
            or not s["id"].strip() or not isinstance(s["text"],str) or not s["text"].strip()
            for s in declared):
            raise ValueError("invalid user reference sources")
        sources={s["id"]:s["text"] for s in declared}
        if len(sources)!=len(declared):
            raise ValueError("duplicate source id")
        _check_explicit_conflicts(sources)
        rows=spec["requirements"]
        if not isinstance(rows,list) or len(rows)!=len(CORE):
            raise ValueError("exact six source-bound reference requirements required")
        indexed={}
        for item in rows:
            if (not isinstance(item,dict) or set(item)!={"name","value","unit","source_id","source_text","origin","classification"}
                    or not isinstance(item["name"],str) or item["name"] in indexed
                    or item["origin"]!="user" or item["classification"]!="hard"):
                raise ValueError("invalid or duplicate user requirement")
            _source(item,sources)
            indexed[item["name"]]=item
        if set(indexed)!=set(CORE):
            raise ValueError("unsupported or missing reference hard requirement")
        for name,(value,unit) in CORE.items():
            item=indexed[name]
            if item["unit"]!=unit:
                raise ValueError("unit mismatch: "+name)
            if isinstance(value,str):
                if item["value"]!=value:
                    raise ValueError("passage task differs")
                snippet=item["source_text"]
                if TASK_NAME not in snippet and not (
                    all(t in snippet for t in ("上","下","前轮"))
                    and any(t in snippet for t in ("正向","前向"))
                    and any(t in snippet for t in ("车头朝下","车头向下","头朝下"))):
                    raise ValueError("source must establish ascent/forward/head-down/front-first descent")
            else:
                actual=item["value"]
                if (isinstance(actual,bool) or not isinstance(actual,(int,float))
                        or not math.isfinite(actual) or actual!=value):
                    raise ValueError("reference requirement value changed: "+name)
                _numeric_source(item["source_text"],value,unit)
        permission=spec["design_permission"]
        if not isinstance(permission,dict) or set(permission)!={"mode","source_id","source_text"} or permission["mode"]!="REUSE_USER_REFERENCE_TEMPLATE":
            raise ValueError("explicit user reference-template reuse permission required")
        _source(permission,sources)
        text=permission["source_text"].lower()
        if not (any(t in text for t in ("复用","使用","reuse","use "))
                and any(t in text for t in ("原案例","原模型","原成功案例","参考模板","reference template","original model","original case"))):
            raise ValueError("source does not authorize reuse of the user original reference")
        for field in ("constraints","preferences","ideas","assumptions"):
            if not isinstance(spec[field],list) or spec[field]:
                raise ValueError("NEEDS_COMBINED_CONFIRMATION: extra "+field+" unsupported by fixed reference")
        canonical=deepcopy(dict(spec))
        canonical["parameters"]=frozen
        canonical["requirements"]=[indexed[k] for k in CORE]
        canonical["sources"]=sorted(declared,key=lambda s:s["id"])
        return {"ready":True,"spec":canonical,"spec_digest":digest_json(canonical),
                "profile":PROFILE,"execution_scope_id":SCOPE_ID,
                "support":"USER_REFERENCE_TEMPLATE_ONLY","new_design_synthesis":False,
                "limitations":list(LIMITATIONS),"issues":[]}
    except (KeyError,TypeError,ValueError,OSError) as exc:
        return {"ready":False,"issues":[{"message":str(exc)}]}


def approved_reference_context() -> dict[str, Any]:
    receipt=authorization()
    requirements={k:{"value":v,"unit":u} for k,(v,u) in CORE.items()}
    return {"profile":PROFILE,"schema_version":SPEC_SCHEMA,
            "reference_ref":{"path":SOURCE_PATH,"sha256":SOURCE_SHA},
            "parameter_values":parameter_values(),"parameters":parameter_values(),
            "parameter_descriptors":parameter_descriptors(),"requirements":requirements,
            "supported_requirements":requirements,"limitations":list(LIMITATIONS),
            "design_permission":{"mode":"REUSE_USER_REFERENCE_TEMPLATE"},
            "execution_scope_id":receipt["scope_id"],
            "contract_sha256":CONTRACT_SHA,"authorization_sha256":AUTH_SHA,
            "new_design_synthesis":False,"vehicle_task_status":"UNKNOWN",
            "physical_acceptance_status":"NOT_ACCEPTED_KNOWN_SIMPLIFICATION"}
