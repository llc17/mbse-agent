"""Deterministic structural lowering and an S2-shaped generation boundary.

Artifacts are non-executable JSON IR, never fake Modelica/SysML. This release
cannot compile a physical model. A future backend must register real component
implementations and a new version before executable generation is enabled.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .schema import (
    ROOT, GROUPS, ContractError, _issue, canonical, digest, load_library,
    require_valid, check_confirmation,
)


def compile_design(design, library=None, *, purpose="confirmed_generation"):
    """Return artifacts + trace_map; preview is explicitly unapproved/non-executable.

    contract_test accepts only marked synthetic receipts and never user approval.
    confirmed_generation accepts only an externally supplied bound user receipt.
    Regardless of purpose, the physical backend remains UNSUPPORTED in A.
    """
    design = deepcopy(design)
    library = deepcopy(load_library() if library is None else library)
    require_valid(design, library)
    if purpose not in {"structure_preview", "contract_test", "confirmed_generation"}:
        raise ContractError([_issue("UNSUPPORTED", "purpose", "unknown compilation purpose")])
    if purpose != "structure_preview":
        check_confirmation(design, test_only=purpose == "contract_test")
    components = []
    trace = {}
    for group in (*GROUPS, "power_supply"):
        items = [design[group]] if group == "power_supply" else design[group]
        for item in items:
            trace["component:" + item["id"]] = {
                "input_collection": group, "input_id": item["id"],
                "component_kind": item["kind"], "component_version": item["component_version"],
                "source": item["source"], "parameter_sources": {
                    key: val["source"] for key, val in item.items()
                    if isinstance(val, dict) and "source" in val
                },
            }
            components.append(deepcopy(item))
    components.sort(key=lambda x: x["id"])
    connections = sorted(deepcopy(design["connections"]), key=lambda x: x["id"])
    for connection in connections:
        trace["connection:" + connection["id"]] = {
            "input_collection": "connections", "input_id": connection["id"],
            "endpoints": [connection["from"], connection["to"]], "source": connection["source"],
        }
    mass_terms = [{"component_id": c["id"], "physical_count": c["count"],
                   "mass_per_instance_kg": c["mass"]["value"],
                   "mass_aggregate_kg": c["mass"]["value"] * c["count"],
                   "inertia_per_instance_kg_m2": c["inertia"]["value"],
                   "inertia_aggregate_about_instance_centres_kg_m2": c["inertia"]["value"] * c["count"]}
                  for c in components]
    drives = [{"component_id": c["id"], "physical_count": c["count"],
               "torque_per_wheel_nm": c["torque"]["value"],
               "torque_pair_nm": c["torque"]["value"] * c["count"],
               "mechanical_power_per_instance_w": c["power"]["value"],
               "mechanical_power_pair_w": c["power"]["value"] * c["count"],
               "electrical_power_pair_at_rating_w": c["power"]["value"] * c["count"] / c["efficiency"]["value"]}
              for c in components if c["kind"] == "drive_pair"]
    payload = design["requirements"]["payload_mass"]["value"]
    total = sum(term["mass_aggregate_kg"] for term in mass_terms)
    topology = {"components": [{"id": c["id"], "kind": c["kind"], "component_version": c["component_version"],
                                "count": c["count"], "ports": {k: v["type"] for k, v in c["ports"].items()},
                                **({"dof": c["dof"]} if "dof" in c else {}),
                                **({"mount_body": c["mount_body"]} if "mount_body" in c else {})}
                               for c in components],
                "connections": [{k: c[k] for k in ("id", "from", "to")} for c in connections],
                "library_digest": design["library_digest"], "generator_version": design["generator_version"]}
    released = [{"joint_id": j["id"], "dof": j["dof"], "physical_count": j["count"]}
                for j in sorted(design["joints"], key=lambda x: x["id"]) if j["dof"]]
    ir = {
        "schema_version": "v11-structure-ir-1", "design_digest": design["design_digest"],
        "design_id": design["design_id"], "revision": design["revision"],
        "project_id": design["project_id"], "baseline_version_id": design["baseline_version_id"],
        "library_version": design["library_version"], "library_digest": design["library_digest"],
        "generator_version": design["generator_version"], "topology_digest": digest(topology),
        "representation": deepcopy(design["representation"]),
        "requirements": deepcopy(design["requirements"]), "scenario": deepcopy(design["scenario"]),
        "provenance": deepcopy(design["provenance"]), "components": components, "connections": connections,
        "mass_accounting": {"basis": "per_instance_times_count_once", "terms": mass_terms,
                            "payload_kg": payload, "total_loaded_mass_kg": total,
                            "vehicle_without_payload_kg": total-payload,
                            "inertia_note": "local intrinsic inertia only; not whole-vehicle inertia"},
        "drive_accounting": drives,
        "supply_contract": {"rated_power_w": design["power_supply"]["power"]["value"],
                            "sum_drive_electrical_rating_w": sum(d["electrical_power_pair_at_rating_w"] for d in drives),
                            "continuous_peak_compatibility": "UNVERIFIED_B_REQUIRED"},
        "kinematics": {"floating_chassis_dof": ["x", "z", "pitch_y"], "released_joints": released,
                       "representative_planar_dof": 3 + len(released),
                       "scope": "tree kinematics only; no contact constraints or dynamic equations"},
    }
    return {
        "status": "STRUCTURE_COMPILED", "purpose": purpose, "executable": False,
        "approval_status": {"structure_preview": "PENDING_REVIEW", "contract_test": "SYNTHETIC_TEST_ONLY",
                            "confirmed_generation": "USER_RECEIPT_BOUND"}[purpose],
        "backend_status": "UNSUPPORTED", "backend_reason": "A has no physical component implementation",
        "tool_status": "NOT_RUN", "physical_status": "UNVERIFIED", "product_acceptance": "not_claimed",
        "artifacts": {"structure.ir.json": ir}, "trace_map": trace,
    }


class ContractGenerator:
    """generate(spec, scenario, directory) protocol adapter, not S2Runner wiring.

    Existing S2Runner.load_spec is S1-only. Do not insert new designs into a legacy
    candidate wrapper. E must replace that loading/execution boundary explicitly.
    Output paths are restricted to new directories under week12/results.
    """
    def __init__(self, library=None, *, test_only=False):
        self.library = deepcopy(load_library() if library is None else library)
        self.test_only = test_only

    def generate(self, spec, scenario, directory):
        compiled = compile_design(spec, self.library, purpose="contract_test" if self.test_only else "confirmed_generation")
        if scenario not in ("ascent", "descent", "flat"):
            raise ContractError([_issue("UNSUPPORTED", "scenario", "unknown scenario")])
        directory = Path(directory).resolve()
        try:
            directory.relative_to((ROOT / "results").resolve())
        except ValueError as exc:
            raise ContractError([_issue("INVALID", "directory", "output must be under week12/results")]) from exc
        # Validates everything before the first write. Exclusive create preserves
        # previous output, including failed/partial generations.
        directory.mkdir(parents=False, exist_ok=False)
        artifacts = deepcopy(compiled["artifacts"])
        artifacts["trace-map.json"] = compiled["trace_map"]
        artifacts["input.json"] = deepcopy(spec)
        artifacts["scenario.json"] = {"name": scenario, "definition": deepcopy(spec["scenario"][scenario]),
                                      "design_digest": spec["design_digest"]}
        summary = {k: v for k, v in compiled.items() if k not in ("artifacts", "trace_map")}
        summary["scenario"] = scenario
        summary["files"] = sorted(artifacts)
        artifacts["generation.json"] = summary
        for name, value in artifacts.items():
            (directory / name).write_bytes(canonical(value))
        (directory / "manifest.json").write_bytes(canonical({"files": {name: digest(val) for name, val in artifacts.items()}}))
        return summary

    def generate_executable(self, spec, scenario, directory):
        # Deliberately no artifact or directory creation on unsupported backend.
        compile_design(spec, self.library, purpose="contract_test" if self.test_only else "confirmed_generation")
        raise ContractError([_issue("UNSUPPORTED", "physical_backend", "B physical components are not implemented")])
