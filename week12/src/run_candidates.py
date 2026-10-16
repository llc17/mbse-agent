from __future__ import annotations

import csv, hashlib, json, re, uuid
from pathlib import Path
from .physics import default_designs, generate_model
from .toolchain import execute_model, check_sysml
from .specification import SpecificationError, load_spec

ROOT = Path(__file__).resolve().parents[1]

def _csv(path: Path) -> dict[str, list[float]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    return {h.strip('"'): [float(x) for x in col] for h, col in zip(rows[0], zip(*rows[1:]))}

def _metrics(run: dict) -> dict:
    if run.get("simulation", {}).get("status") != "PASS":
        return {"status": "UNVERIFIED", "reason": "simulation did not pass", "run": run}
    data = _csv(Path(run["simulation"]["csv_path"]))
    def last(name, default=0.0): return data.get(name, [default])[-1]
    max_pen = max(data.get("penetration", [float("nan")]))
    max_pitch = max(abs(x) for x in data.get("pitch", [float("nan")]))
    final_x, final_z = last("x"), last("z")
    return {"status": "measured", "final_x_m": final_x, "final_z_m": final_z,
            "max_penetration_m": max_pen, "max_abs_pitch_rad": max_pitch,
            "max_battery_current_a": max(data.get("batteryCurrent", [0.0])),
            "battery_energy_j": last("batteryEnergy"), "mechanical_work_j": last("mechanicalWork"),
            "max_slip_mps": max(data.get("slipSpeed", [0.0])), "csv_sha256": hashlib.sha256(Path(run["simulation"]["csv_path"]).read_bytes()).hexdigest()}

def run_all(spec_path: Path | None = None) -> list[dict]:
    """Run only a confirmed S1 specification.

    The former hard-coded three-candidate sample is intentionally no longer a
    generation input.  Callers must provide the pending S1 artifact after the
    user has confirmed its exact project/baseline binding.
    """
    if spec_path is None:
        spec_path = ROOT / "results" / "s1-spec-20260918" / "spec.json"
    try:
        approved = load_spec(spec_path, require_generation_ready=True)
    except SpecificationError as exc:
        raise RuntimeError(f"generation refused: {exc}") from exc
    stair = approved["stair"]
    assumptions = approved["assumptions"]
    design = dict(approved["candidate"]["design"])
    design["candidate_id"] = approved["candidate"]["candidate_id"]
    all_results = []
    spec = {"design": design, "stair": stair, "assumptions": assumptions}
    item = {"candidate": design, "spec": spec, "scenarios": {}}
    item["sysml"] = _sysml(design, stair)
    item["sysml_check"] = check_sysml(item["sysml"])
    for scenario, stop in (("stair", assumptions["stair_stop_s"]), ("flat", assumptions["flat_stop_s"])):
        run_dir = ROOT / "results" / ("candidate-" + design["candidate_id"] + "-" + scenario + "-" + uuid.uuid4().hex[:8])
        run = execute_model(generate_model(spec, scenario), "V11Stair" if scenario == "stair" else "V11Flat", run_dir, stop, 300)
        item["scenarios"][scenario] = {"tool": run, "metrics": _metrics(run)}
    stair_m = item["scenarios"]["stair"]["metrics"]
    flat_m = item["scenarios"]["flat"]["metrics"]
    target_x = stair["count"] * stair["tread_m"] + design["wheelbase_m"]/2
    item["verdict"] = {"climb": "pass" if stair_m.get("status")=="measured" and stair_m["final_x_m"] >= target_x and stair_m["max_penetration_m"] <= 0.008 and stair_m["max_abs_pitch_rad"] <= 0.75 else "fail_or_unverified", "climb_target_x_m": target_x, "cruise": "measured" if flat_m.get("status")=="measured" else "unverified", "limits": ["2D planar aggregate axle model", "lateral stability and real tire calibration not verified"]}
    if flat_m.get("status") == "measured":
        usable_j = design["battery_wh"] * design["usable_energy_fraction"] * 3600
        item["verdict"]["estimated_cruise_hours"] = usable_j / max(flat_m["battery_energy_j"], 1e-9) * assumptions["flat_stop_s"] / 3600
        item["verdict"]["cruise_method"] = "same candidate battery and measured flat task average power; extrapolation, not full discharge"
    out = ROOT / "results" / ("candidate-" + design["candidate_id"] + "-record-" + uuid.uuid4().hex[:8])
    out.mkdir(parents=True, exist_ok=False)
    (out / "spec.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    (out / "model.sysml").write_text(item["sysml"], encoding="utf-8")
    (out / "model-stair.mo").write_text(generate_model(spec, "stair"), encoding="utf-8")
    (out / "model-flat.mo").write_text(generate_model(spec, "flat"), encoding="utf-8")
    (out / "result.json").write_text(json.dumps(item, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    all_results.append(item)
    summary = ROOT / "results" / ("v11-development-summary-" + uuid.uuid4().hex[:8])
    summary.mkdir()
    (summary / "summary.json").write_text(json.dumps({"schema":"v11-dev-summary-1","input":stair,"candidates":all_results,"official_github_reference":"not retrieved: network unavailable","tool_versions":{"openmodelica":"1.25.7","msl":"4.0.0","sysmlpy":"0.34.1"}}, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    return all_results

def _sysml(design: dict, stair: dict) -> str:
    # SysML identifiers cannot contain the hyphens used by the S1 candidate
    # record (for example ``s1-clearance-pending``).  Keep the evidence label
    # in attributes while emitting a valid package identifier.
    safe = re.sub(r"[^A-Za-z0-9_]", "_", str(design["candidate_id"]))
    return f'''package V11Car_{safe} {{
  requirement def StairTask {{ attribute riser_m : Real; attribute tread_m : Real; attribute count : Integer; }}
  part def Car {{ attribute wheelCount : Integer; attribute wheelRadius_m : Real; attribute wheelbase_m : Real; attribute mass_kg : Real; attribute battery_Wh : Real; }}
  part def System {{ attribute wheelCount : Integer = {design["wheel_count"]}; attribute wheelRadius_m : Real = {design["wheel_radius_m"]}; attribute wheelbase_m : Real = {design["wheelbase_m"]}; attribute mass_kg : Real = {design["mass_kg"]}; attribute battery_Wh : Real = {design["battery_wh"]}; attribute stairRiser_m : Real = {stair["riser_m"]}; attribute stairTread_m : Real = {stair["tread_m"]}; attribute stairCount : Integer = {stair["count"]}; }}
  part car : Car;
  satisfy car by StairTask;
}}\n'''

if __name__ == "__main__":
    print(json.dumps(run_all(), ensure_ascii=False, indent=2))
