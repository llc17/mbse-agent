"""Copied Tail2 CSV physics/stage algorithms with confirmed Agent evidence.

Source: week13/sixwheel_fix/tail80_tuning_20261030/mixed_check.py.
The original physics/terrain/torque thresholds are unchanged. Only local module
paths and the independently confirmed source/build/XML/stdout binder differ.
No compiler, simulator, product API, or process launch is available here.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np

HERE = Path(__file__).resolve().parent
OLD_PATH = HERE / "physics_check.py"


def _load_old():
    spec = importlib.util.spec_from_file_location("tail2_frozen_physics", OLD_PATH)
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    # Geometry/energy algorithms are copied verbatim. New request expectations
    # are obtained by evidence() from the frozen confirmed spec, not this table.
    old.BASELINE.update(R_main=.1195, T_main_max=40., T_nose_max=17.,
                        **{"wheel_J[2]": .12852225, "wheel_J[3]": .12852225})
    return old


_OLD = _load_old()
sha, write_json, read_csv, array, wheel_centers = (_OLD.sha, _OLD.write_json, _OLD.read_csv,
                                                _OLD.array, _OLD.wheel_centers)


def derived(p):
    if p["n_steps"]!=20 or p.get("nEndSteps")!=2 or any(p[k]<=0 for k in ("stepH","stepD","h19","h20","d19","d20")):
        raise ValueError("Asym2 requires fixed20 and positive geometry")
    up=p["x_start_up"]+17*p["stepD"]+p["d19"]+p["d20"]
    down=up+2.28
    last=down+p["d20"]+p["d19"]+17*p["stepD"]
    reach=math.ceil(max(p["R_main"],p["R_nose"])/min(p["stepD"],p["d19"],p["d20"]))
    return dict(x_last_up=up,x_end_up=up+.28,x_start_down=down,x_last_down=last,x_end_down=last+.28,H_peak=18*p["stepH"]+p["h19"]+p["h20"],fixedTopTread=.28,nReach=reach,nLocal=2*reach+1,nContact=3+3*(2*reach+1))

def up_edges(p):
    main=[(p["x_start_up"]+j*p["stepD"],(j+1)*p["stepH"]) for j in range(18)]
    x,z=main[-1]
    return main+[(x+p["d19"],z+p["h19"]),(x+p["d19"]+p["d20"],z+p["h19"]+p["h20"])]

def down_edges(p):
    down=derived(p)["x_start_down"]
    tail=[(down,18*p["stepH"]+p["h19"]),(down+p["d20"],18*p["stepH"])]
    main=[(down+p["d20"]+p["d19"]+j*p["stepD"],(17-j)*p["stepH"]) for j in range(18)]
    return tail+main


def terrain_polygon(p, left=-1000., right=None, bottom=-1000.):
    endpoints = derived(p)
    right = endpoints["x_end_down"] + 1000 if right is None else right
    if left >= p["x_start_up"] or right <= endpoints["x_end_down"] or bottom >= 0:
        raise ValueError("closing edges must remain outside task terrain")
    points = [(left, 0.)]
    before = 0.
    for x, after in up_edges(p) + down_edges(p):
        points.extend(((x, before), (x, after)))
        before = after
    points.extend(((right, 0.), (right, bottom), (left, bottom)))
    return np.asarray(points, dtype=float)


def evidence(run_dir, expected=None):
    from ..case_asym2_execution import verify_run_evidence
    return verify_run_evidence(run_dir, expected)


def torque_check(data):
    result = {}
    for i, limit in ((1, 17.), (2, 40.), (3, 40.)):
        v = data[f"T[{i}]"]
        if not np.all(np.isfinite(v)):
            result[str(i)] = dict(max_abs_Nm=None, rated_group_limit_Nm=limit,
                numerical_tolerance_Nm=1e-7, time_of_maximum_s=None, status="FAIL",
                issue="nonfinite motor torque sample")
            continue
        peak = int(np.argmax(np.abs(v)))
        result[str(i)] = dict(max_abs_Nm=float(abs(v[peak])), rated_group_limit_Nm=limit,
            numerical_tolerance_Nm=1e-7, time_of_maximum_s=float(data["time"][peak]),
            status="PASS" if abs(v[peak]) <= limit + 1e-7 else "FAIL")
    return result


def _physics(run_dir, expected=None, make_plot=False, data=None, stop=None):
    old = _load_old()  # Local module instance: never monkey-patch shared old checker.
    state = {}

    def bound(path):
        manifest, actual, binding = evidence(path, expected)
        state["actual"] = actual
        return manifest, actual, binding

    old.evidence = bound
    old.terrain_polygon = lambda h, d, n, start, flat, **kw: terrain_polygon(state["actual"], **kw)
    if data is not None:
        old.read_csv = lambda path: (data, len(data))
    report = old.check(run_dir, expected_stop=stop, make_plot=make_plot)
    report["schema"] = "tail2-independent-check-1"
    report["checker_sources"] = dict(physics_path=str(OLD_PATH), physics_sha256=sha(OLD_PATH),
        adapter_path=str(Path(__file__).resolve()), adapter_sha256=sha(__file__))
    try:
        if data is None:
            data, _ = read_csv(report["csv"]["path"])
        report["torque_limits"] = torque_check(data)
        for wheel, record in report["torque_limits"].items():
            if record["status"] != "PASS":
                report["invalidity_reasons"].append("wheel " + wheel + " exceeds fixed rated torque")
                if report["classification"] != "TOOL_FAILURE":
                    report["classification"] = "INVALID_MODEL"
    except (OSError, KeyError, ValueError) as exc:
        report.setdefault("issues", []).append("torque readback failed: " + str(exc))
        report["classification"] = "TOOL_FAILURE"
    trusted = (report.get("evidence", {}).get("status") == "PASS"
               and report.get("tool_process", {}).get("status") == "PASS"
               and not report.get("invalidity_reasons")
               and report.get("energy", {}).get("pre_run_normalized_balance_guard", {}).get("passed") is True
               and len(report.get("contacts", {})) == 3
               and all(v["status"] == "PASS" for v in report.get("torque_limits", {}).values())
               and report.get("observation", {}).get("reached_requested_stop") is True)
    report["validity"] = dict(status="PASS" if trusted else "FAIL",
        issues=report.get("invalidity_reasons", []) + report.get("issues", []))
    return report


def check_prefix(run_dir, data, end):
    mask = data["time"] <= end + 1e-10
    prefix = {k: v[mask] for k, v in data.items()}
    result = _physics(run_dir, data=prefix, stop=float(prefix["time"][-1]))
    return dict(status=result["validity"]["status"], through_time_s=float(prefix["time"][-1]),
        rows=len(prefix["time"]), issues=result["validity"]["issues"],
        csv_sha256=result.get("csv", {}).get("sha256"),
        max_compression_over_radius=max((v["max_compression_over_radius"] for v in result.get("wheels", {}).values()), default=None),
        energy_guard=result.get("energy", {}).get("pre_run_normalized_balance_guard"),
        torque_limits=result.get("torque_limits"))


def check(run_dir, expected=None, make_plot=False):
    report = _physics(Path(run_dir).resolve(), expected, make_plot)
    if report.get("evidence", {}).get("status") == "PASS" and "csv" in report:
        from . import stage_check as stage
        staged = stage.check_stage(run_dir, report)
        report["stage_report"] = staged
        report.update({key: staged["stages"].get(key) for key in ("main18", "tail19", "tail20", "ascent", "fullroute")})
        report["physics_classification"] = report["classification"]
        if report["classification"] == "PASS" and report["fullroute"]["status"] != "PASS":
            report["classification"] = "INCOMPLETE"
            report["issues"].append("geometric route completion lacks trusted sampled edge-support stages")
            report["classification_meaning"] = "Route geometry completed, but independent sampled-stage support is unverified."
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = check(args.run_dir)
    write_json(args.out or args.run_dir / "check.json", report)
    print(json.dumps(dict(classification=report["classification"], validity=report.get("validity"),
                         stages={k: report[k]["status"] for k in ("main18", "tail19", "tail20", "ascent", "fullroute") if report.get(k)})))


if __name__ == "__main__":
    main()
