"""Strict supported-first-case schema and immutable authorization binding."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping

from week11.src.store import digest_json

WEEK12_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = WEEK12_ROOT.parent
RESULT_ROOT = WEEK12_ROOT / "results/s3-c-active-eightwheel-execution-20261009-01"
PHYSICAL_PATH = "week12/results/s3-c-open-configuration-proposal-20261009-01/proposal-final.json"
PHYSICAL_SHA = "ec45de70bd6ffc114c5a9670c0db5000f5ee131a4144f6949fd5df7215c627ac"
CONTRACT_SHA = "768d7b3f60a5d762477dc22e81e53e8c43477297f0d1d222e8dd78af546cba55"
SPEC_SCHEMA = "v11-car-spec-1"
TASK_NAME = "ascend_and_forward_head_down_front_first_descend"
CORE = {"payload_mass": (20, "kg"), "step_height": (.15, "m"),
        "step_depth": (.28, "m"), "continuous_steps": (20, "count"),
        "passage_task": (TASK_NAME, "task")}


def inside_week12(path: str | Path) -> Path:
    resolved = Path(path).resolve()
    try:
        resolved.relative_to(WEEK12_ROOT.resolve())
    except ValueError as exc:
        raise ValueError("car adapter path must be inside week12") from exc
    return resolved


def authorization() -> dict[str, Any]:
    raw = (RESULT_ROOT / "v10-car-increment-proposal-final.json").read_bytes()
    receipt = json.loads((RESULT_ROOT / "v10-car-increment-authorization.json").read_bytes())
    if (hashlib.sha256(raw).hexdigest() != CONTRACT_SHA or receipt.get("status") != "APPROVED"
            or receipt.get("proposal_sha256") != CONTRACT_SHA
            or receipt.get("physical_spec_sha256") != PHYSICAL_SHA):
        raise ValueError("car increment authorization binding mismatch")
    physical = (REPO_ROOT / PHYSICAL_PATH).read_bytes()
    if hashlib.sha256(physical).hexdigest() != PHYSICAL_SHA:
        raise ValueError("approved physical reference changed")
    return receipt


def physical_spec() -> dict[str, Any]:
    authorization()
    return json.loads((REPO_ROOT / PHYSICAL_PATH).read_bytes())


def _source(item: Mapping[str, Any], sources: dict[str, str]) -> None:
    sid, snippet = item.get("source_id"), item.get("source_text")
    if not isinstance(sid, str) or sid not in sources or not isinstance(snippet, str) or not snippet.strip() or snippet not in sources[sid]:
        raise ValueError("requirement/constraint/permission lacks an exact trusted source substring")


def validate_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Reject changes; local confirmation cannot authorize a different vehicle."""
    try:
        if not isinstance(spec, Mapping):
            raise ValueError("car specification must be an object")
        expected = {"schema_version", "design_mode", "physical_ref", "sources", "requirements", "design_permission", "constraints", "preferences", "ideas", "assumptions"}
        if set(spec) != expected or spec.get("schema_version") != SPEC_SCHEMA:
            raise ValueError("unsupported car specification fields/schema")
        if spec.get("design_mode") != "REUSE_APPROVED_DESIGN":
            raise ValueError("NEEDS_COMBINED_CONFIRMATION: only existing approved design reuse is supported")
        if spec.get("physical_ref") != {"path": PHYSICAL_PATH, "sha256": PHYSICAL_SHA}:
            raise ValueError("NEEDS_COMBINED_CONFIRMATION: physical reference differs from frozen approval")
        declared = spec.get("sources")
        if not isinstance(declared, list) or not declared or any(not isinstance(s, dict) or set(s) != {"id", "text"} or not isinstance(s["id"], str) or not isinstance(s["text"], str) or not s["text"].strip() for s in declared):
            raise ValueError("invalid car sources")
        sources = {s["id"]: s["text"] for s in declared}
        if len(sources) != len(declared):
            raise ValueError("duplicate source id")
        rows = spec.get("requirements")
        if not isinstance(rows, list) or len(rows) != len(CORE):
            raise ValueError("missing core car requirements")
        indexed = {}
        for item in rows:
            if not isinstance(item, dict) or set(item) != {"name", "value", "unit", "source_id", "source_text", "origin", "classification"} or item["name"] in indexed:
                raise ValueError("invalid or duplicate car requirement")
            if item.get("origin") != "user" or item.get("classification") != "hard":
                raise ValueError("core requirements must retain genuine user provenance")
            _source(item, sources)
            indexed[item["name"]] = item
        if set(indexed) != set(CORE):
            raise ValueError("missing/unsupported core car requirements")
        for name, (value, unit) in CORE.items():
            item = indexed[name]
            if item["unit"] != unit:
                raise ValueError("unsupported unit: " + name)
            if isinstance(value, str):
                if item["value"] != value:
                    raise ValueError("NEEDS_COMBINED_CONFIRMATION: passage task changed")
                if not any(t in item["source_text"].lower() for t in ("正向下", "forward", TASK_NAME)):
                    raise ValueError("task source does not establish forward descent")
                snippet = item["source_text"].lower()
                if TASK_NAME not in snippet and not (all(t in snippet for t in ("上", "下", "前轮")) and any(t in snippet for t in ("车头朝下", "车头向下", "头朝下"))):
                    raise ValueError("task source must retain ascent/head-down/front-first descent")
            else:
                actual = item["value"]
                if isinstance(actual, bool) or not isinstance(actual, (int, float)) or not math.isfinite(actual) or not math.isclose(actual, value, abs_tol=1e-12, rel_tol=0):
                    raise ValueError("NEEDS_COMBINED_CONFIRMATION: supported first-case requirement differs: " + name)
                tokens = re.findall(r"(?<![A-Za-z0-9_.])(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", item["source_text"])
                if not any(math.isclose(float(t), value, abs_tol=1e-12, rel_tol=0) for t in tokens):
                    raise ValueError("requirement value absent from exact source: " + name)
                units = {"kg": ("kg", "公斤", "千克"), "m": ("m", "米"), "count": ("count", "级", "steps")}[unit]
                if not any(re.search(r"(?<![A-Za-z])" + re.escape(u) + r"(?![A-Za-z])", item["source_text"]) for u in units):
                    raise ValueError("requirement unit absent from source: " + name)
        permission = spec.get("design_permission")
        if not isinstance(permission, dict) or set(permission) != {"wheel_count", "architecture", "tracks_allowed", "source_id", "source_text"} or permission.get("wheel_count") != "delegated" or permission.get("architecture") != "delegated" or permission.get("tracks_allowed") is not True:
            raise ValueError("design authority must explicitly match the delegated approved case")
        _source(permission, sources)
        text = permission["source_text"].lower()
        if not any(t in text for t in ("委派", "delegat", "agent决定")):
            raise ValueError("source does not delegate design authority")
        if not any(t in text for t in ("任何方式", "允许履带", "履带允许", "tracks allowed", "any architecture")):
            raise ValueError("source does not authorize the declared track/open architecture scope")
        for field, classification in (("constraints", "hard"), ("preferences", "preference"), ("ideas", "idea")):
            entries = spec[field]
            if not isinstance(entries, list):
                raise ValueError("invalid " + field)
            for item in entries:
                if not isinstance(item, dict) or set(item) != {"text", "source_id", "source_text", "origin", "classification"} or item["origin"] != "user" or item["classification"] != classification or item["text"] != item["source_text"]:
                    raise ValueError("invalid source-bound " + field)
                _source(item, sources)
            if field == "constraints" and entries:
                raise ValueError("NEEDS_COMBINED_CONFIRMATION: additional hard constraints require an explicit supported compatibility check")
        if not isinstance(spec["assumptions"], list) or spec["assumptions"]:
            raise ValueError("first case assumptions come from the immutable approved design only")
        canonical = deepcopy(dict(spec))
        canonical["requirements"] = [indexed[k] for k in CORE]
        return {"ready": True, "spec": canonical, "spec_digest": digest_json(canonical),
                "support": "EXISTING_APPROVED_DESIGN_REUSED", "new_design_synthesis": False}
    except (KeyError, TypeError, ValueError) as exc:
        return {"ready": False, "issues": [{"message": str(exc)}]}


def approved_context() -> dict[str, Any]:
    p = physical_spec()
    return {"design_mode": "REUSE_APPROVED_DESIGN", "physical_ref": {"path": PHYSICAL_PATH, "sha256": PHYSICAL_SHA},
            "candidate_id": p["candidate_id"], "supported_requirements": {k: {"value": v, "unit": u} for k, (v, u) in CORE.items()},
            "design_permission": {"wheel_count": "delegated", "architecture": "delegated", "tracks_allowed": True},
            "physical_assumptions_origin": "AI_DESIGN_PARAMETERS_IN_ORIGINAL_APPROVAL", "hardware_status": "UNKNOWN",
            "precheck": {"mass_kg": p["requirements"]["total_mass"], "geometric_and_force_witness": "APPROVED_NECESSARY_PRECHECK_ONLY_NOT_DYNAMICS_PASS",
                         "battery_endurance": "UNKNOWN_UNTIL_ACTUAL_ENERGY", "generality": "SUPPORTED_APPROVED_FIRST_CASE_ONLY"}}


# Explicit reference dispatch; legacy vehicle constants remain historical.
_legacy_validate_spec = validate_spec
_legacy_authorization = authorization
_legacy_approved_context = approved_context

def profile_module(profile=None):
    from . import reference_profile
    if profile is None or profile == reference_profile.PROFILE:
        return reference_profile
    from . import reference_three_profile
    if profile == reference_three_profile.PROFILE:
        return reference_three_profile
    from . import eightwheel_profile
    if profile == eightwheel_profile.PROFILE:
        return eightwheel_profile
    raise ValueError('unsupported explicit car profile')

def execution_route(profile=None):
    selected = profile_module(profile)
    from . import reference_profile
    if selected.PROFILE == reference_profile.PROFILE:
        from . import reference_route
        return reference_route
    from . import reference_three_profile
    if selected.PROFILE == reference_three_profile.PROFILE:
        from . import reference_three_route
        return reference_three_route
    from . import eightwheel_route
    return eightwheel_route

def authorization(profile=None):
    return profile_module(profile).authorization()

def validate_spec(spec):
    if not isinstance(spec, Mapping):
        return {'ready':False,'issues':[{'message':'explicit supported car profile required'}]}
    from .reference_profile import PROFILE as REFERENCE_PROFILE, validate_reference_spec
    if spec.get('profile') == REFERENCE_PROFILE:
        return validate_reference_spec(spec)
    if spec.get('profile') == 'REFERENCE_TEMPLATE_THREE_STEP':
        from .reference_three_profile import validate_reference_spec as validate_three
        return validate_three(spec)
    from .eightwheel_profile import PROFILE as EIGHTWHEEL_PROFILE, validate_eightwheel_spec
    if spec.get('profile') == EIGHTWHEEL_PROFILE:
        return validate_eightwheel_spec(spec)
    return {'ready':False,'issues':[{'message':'CLOSED_LEGACY_VEHICLE_SCOPE_OR_UNSUPPORTED_PROFILE'}]}

def approved_context(profile=None):
    selected = profile_module(profile)
    if hasattr(selected,'approved_eightwheel_context'):
        return selected.approved_eightwheel_context()
    return selected.approved_reference_context()
