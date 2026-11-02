"""Read-only Tail2 stages; force-supported edges and valid completed prefixes.

Copied from week13/sixwheel_fix/tail80_tuning_20261030/stage_check.py.
Only local module/provenance paths differ; the original file is untouched.
Middle stage uses 6..10, not the 50-step reader's 10..19.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
from . import mixed_check as _CHECK
_OLD = _CHECK._OLD
STAGE_NAMES = ("entrance", "middle6to10", "main18", "tail19", "tail20", "ascent", "down_entry", "fullroute")


def _time(mask, times):
    ids = np.flatnonzero(mask)
    return float(times[ids[0]]) if len(ids) else None


def _wheel_events(data, p, center, wheel, compression_ratio=.2, force_abs=1e-5,
                  force_rel=1e-8, air_tol=1e-6):
    t, radius = data["time"], p["R_main"]
    count = int(round(p["nContact"]))
    arrays = {name: _OLD.array(data, name, wheel, count)
              for name in ("Fn", "Ft", "nx", "nz", "gap", "valid")}
    fn, ft, nx, nz, gap, valid = (arrays[name] for name in ("Fn", "Ft", "nx", "nz", "gap", "valid"))
    force_tol = force_abs + force_rel * max(1., float(np.max(np.abs(fn))), float(np.max(np.abs(ft))))
    qx = center[:, 0, None] - (radius + gap) * nx
    qz = center[:, 1, None] - (radius + gap) * nz
    fz = fn * nz - ft * nx
    active = (valid > .5) & (gap < 0) & (fn > force_tol) & (fz > force_tol)
    events, previous_index, previous_time = [], None, None
    bottom, rear_edge = center[:, 1] - radius, center[:, 0] - radius
    polygon = _CHECK.terrain_polygon(p)
    for k, (ex, ez) in enumerate(_CHECK.up_edges(p), 1):
        clear = (rear_edge > ex) & (bottom >= ez - compression_ratio * radius)
        hits = np.flatnonzero(clear)
        index = int(hits[0]) if len(hits) else None
        clear_time = float(t[index]) if index is not None else None
        approach = ((center[:, 0] + radius >= ex) & (rear_edge <= ex)
                    & (bottom <= ez + air_tol) & (center[:, 1] + radius >= ez - air_tol))
        if index is not None:
            approach[index:] = False
        approach_ids = np.flatnonzero(approach)
        first_approach = int(approach_ids[0]) if len(approach_ids) else None
        near_top = (np.abs(qx - ex) <= radius + air_tol) & (np.abs(qz - ez) <= air_tol)
        support = active & near_top
        window = np.zeros(len(t), dtype=bool)
        if first_approach is not None:
            window[first_approach:(index + 1 if index is not None else len(t))] = True
        support &= window[:, None]
        # A claimed support point must also lie on the independent terrain.
        ids = np.flatnonzero(support)
        if len(ids):
            points = np.column_stack((qx.ravel()[ids], qz.ravel()[ids]))
            boundary = _OLD.point_solid_geometry(points, polygon)["distance"] <= air_tol
            support.ravel()[ids[~boundary]] = False
        rows = np.flatnonzero(np.any(support, axis=1))
        evidence = dict(status="OBSERVED" if len(rows) else "NOT_OBSERVED",
            positive_support_samples=int(len(rows)), force_tolerance_N=force_tol,
            first_time_s=float(t[rows[0]]) if len(rows) else None,
            last_time_s=float(t[rows[-1]]) if len(rows) else None,
            peak_vertical_support_N=0.)
        if len(rows):
            force = np.where(support, fz, 0.)
            row, slot = np.unravel_index(int(np.argmax(force)), force.shape)
            evidence.update(peak_vertical_support_N=float(force[row, slot]),
                peak_time_s=float(t[row]), slot=int(slot) + 1,
                normal_force_N=float(fn[row, slot]), terrain_point_m=[float(qx[row, slot]), float(qz[row, slot])])
        issues = []
        if index is not None:
            if first_approach is None:
                issues.append("corner approach was not sampled before clearance")
            if not len(rows):
                issues.append("no positive support on the real top corner/new tread during passage")
            if k > 1 and (previous_index is None or index <= previous_index or clear_time <= previous_time):
                issues.append("successive edges were skipped or cleared in the same sample")
        event = dict(step=k, edge_x_m=ex, edge_z_m=ez, geometrically_cleared=index is not None,
            first_approach_s=float(t[first_approach]) if first_approach is not None else None,
            clearance_time_s=clear_time, trusted_clearance=index is not None and not issues,
            support=evidence, issues=issues)
        if index is not None:
            event["clearance_sample"] = dict(time_s=clear_time, center_m=center[index].tolist(),
                rear_edge_x_m=float(rear_edge[index]), bottom_z_m=float(bottom[index]),
                x_margin_m=float(rear_edge[index] - ex), z_margin_m=float(bottom[index] - ez))
        events.append(event)
        previous_index, previous_time = index, clear_time
    return events


def check_stage(run_dir, check_report=None):
    if check_report is not None and "stage_report" in check_report:
        return check_report["stage_report"]
    run_dir = Path(run_dir).resolve()
    out = dict(schema="tail2-stage-readback-1", checked_utc=datetime.now(timezone.utc).isoformat(),
        run_dir=str(run_dir), original_classification="UNKNOWN", effective_classification="TOOL_FAILURE",
        validity=dict(status="FAIL", issues=[]), stages={},
        scope="SAMPLED_PLANAR_THREE_WHEEL_GROUP_DIAGNOSTIC",
        limitations=["No chassis clearance or hardware capability certification.",
            "Sampled positive main-wheel support is required; nose may be airborne.",
            "Completed ascent is checked on its real time prefix, independently of later descent."])
    source_path = HERE / "stage_check.py"
    out["checker_source"] = dict(path=str(source_path), sha256=_OLD.sha(source_path),
        adapter_path=str(Path(__file__).resolve()), adapter_sha256=_OLD.sha(__file__))
    try:
        report = check_report if check_report is not None else json.loads((run_dir / "check.json").read_text(encoding="utf-8-sig"))
        out["original_classification"] = report.get("classification", "UNKNOWN")
        manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8-sig"))
        record = manifest["files"]["csv"]
        path = Path(record["path"]).resolve()
        digest = _OLD.sha(path)
        if digest != record["sha256"] or digest != report["csv"]["sha256"] or path != Path(report["csv"]["path"]).resolve():
            raise ValueError("CSV differs from bound execution or physics check")
        data, _ = _OLD.read_csv(path)
        if any(not np.all(np.isfinite(v)) for v in data.values()) or len(data["time"]) < 2 or np.any(np.diff(data["time"]) < 0):
            raise ValueError("invalid/nonfinite CSV time or inspected values")
        p = report["evidence"]["actual"]
        _CHECK.derived(p)
        binding_ok = (report["evidence"]["status"] == "PASS"
                      and report.get("tool_process", {}).get("status") == "PASS")
        full_valid = bool(binding_ok and report.get("validity", {}).get("status") == "PASS"
                          and report.get("observation", {}).get("full_mission_observation"))
        out["validity"] = dict(status="PASS" if full_valid else "FAIL",
            issues=report.get("invalidity_reasons", []) + report.get("issues", []))
        out["effective_classification"] = report["classification"] if binding_ok else "TOOL_FAILURE"
        out["csv"] = dict(path=str(path), sha256=digest)
        out["rules"] = dict(middle_steps=list(range(6, 11)), main_steps=list(range(1, 19)),
            edge_rule="steps1..18:(start+(k-1)D,kH); k19:(start+17D+d19,18H+h19); k20:(start+17D+d19+d20,18H+h19+h20)",
            clearance="cx-R>edgeX and cz-R>=edgeZ-.2R; sampled real positive main-wheel support",
            ascent="all wheel rear edges>x_last_up+.5 and bottoms>=H_peak-.2R; all20 main-wheel pairs supported",
            rated_group_torque_Nm=[17, 40, 40], nose_airborne_allowed=True)
        centers = _OLD.wheel_centers(data, p)
        front = _wheel_events(data, p, centers[1], 2)
        rear = _wheel_events(data, p, centers[2], 3)
        steps = []
        for f, r in zip(front, rear):
            ft, rt = f["clearance_time_s"], r["clearance_time_s"]
            both = ft is not None and rt is not None
            order = both and ft <= rt
            steps.append(dict(step=f["step"], edge_x_m=f["edge_x_m"], edge_z_m=f["edge_z_m"],
                front=f, rear=r, front_before_rear=bool(order), both_geometrically_cleared=both,
                trusted_pair_clearance=bool(f["trusted_clearance"] and r["trusted_clearance"] and order),
                completion_time_s=max(ft, rt) if both else None))
        out["up_steps"] = steps
        cache = {}

        def stage(reached, complete, completion, issues=None):
            issues = list(issues or [])
            prefix = None
            trusted = full_valid
            if complete and completion is not None and full_valid:
                prefix = dict(status="PASS", through_time_s=completion,
                    coverage="This prefix is covered by the same bound 300s whole-observation hard validity checks.",
                    csv_sha256=digest, independently_recomputed=False)
            elif complete and completion is not None and binding_ok:
                if completion not in cache:
                    cache[completion] = _CHECK.check_prefix(run_dir, data, completion)
                prefix = cache[completion]
                trusted = prefix["status"] == "PASS"
            if not reached:
                status = "NOT_REACHED"
            elif not trusted or issues:
                status = "UNVERIFIED"
            elif complete:
                status = "PASS"
            elif report["classification"] == "PHYSICAL_STALL":
                status = "PHYSICAL_STALL"
            else:
                status = "INCOMPLETE"
            return dict(status=status, reached=bool(reached), geometrically_completed=bool(complete),
                completion_time_s=completion, trusted=bool(trusted and not issues), issues=issues,
                validity_prefix=prefix)

        def upstage(first, last):
            chosen = steps[first - 1:last]
            reached = front[first - 1]["first_approach_s"] is not None or front[first - 1]["geometrically_cleared"]
            complete = all(s["both_geometrically_cleared"] for s in chosen)
            bad = [s["step"] for s in chosen if s["both_geometrically_cleared"] and not s["trusted_pair_clearance"]]
            result = stage(reached, complete, chosen[-1]["completion_time_s"] if complete else None,
                           ["unverified sampled support/order at steps " + str(bad)] if bad else [])
            result.update(first_step=first, last_step=last, unverified_steps=bad)
            return result

        out["stages"]["entrance"] = upstage(1, 1)
        out["stages"]["middle6to10"] = upstage(6, 10)
        middle = out["stages"]["middle6to10"]
        if middle["completion_time_s"] is not None:
            before = data["time"] <= middle["completion_time_s"] + 1e-10
            furthest = max(float(np.max(c[before, 0] + r)) for c, r in zip(centers, (p["R_nose"], p["R_main"], p["R_main"])))
            end_influence = _CHECK.up_edges(p)[18][0]
            middle.update(furthest_wheel_front_x_m=furthest, terminal_tread_start_x_m=end_influence,
                          end_step_not_yet_reached=bool(furthest < end_influence))
            if furthest >= end_influence:
                middle["status"], middle["trusted"] = "UNVERIFIED", False
                middle["issues"].append("middle-stage wheels already reached the terminal tread region")
        out["stages"]["main18"] = upstage(1, 18)
        out["stages"]["tail19"] = upstage(19, 19)
        out["stages"]["tail20"] = upstage(20, 20)
        radii = (p["R_nose"], p["R_main"], p["R_main"])
        masks = [(c[:, 0] - r > p["x_last_up"] + .5) & (c[:, 1] - r >= p["H_peak"] - .2 * r)
                 for c, r in zip(centers, radii)]
        ascent_time = _time(np.logical_and.reduce(masks), data["time"])
        supported = all(s["trusted_pair_clearance"] for s in steps)
        bad = [s["step"] for s in steps if s["both_geometrically_cleared"] and not s["trusted_pair_clearance"]]
        out["stages"]["ascent"] = stage(bool(steps[0]["front"]["first_approach_s"] is not None),
            ascent_time is not None and supported, ascent_time,
            ["unverified main-wheel edge support/order: " + str(bad)] if bad else [])
        out["stages"]["ascent"].update(exit_line_x_m=p["x_last_up"] + .5,
            all_wheels_geometrically_passed=ascent_time is not None)
        route = report.get("route", {})
        fd, rd = route.get("front_main_crossed_down_edge_s"), route.get("rear_main_crossed_down_edge_s")
        down_complete = fd is not None and rd is not None and fd <= rd
        out["stages"]["down_entry"] = stage(fd is not None or rd is not None, down_complete,
            max(fd, rd) if down_complete else None,
            ["rear entered descent before front"] if fd is not None and rd is not None and fd > rd else [])
        out["stages"]["down_entry"].update(front_crossed_s=fd, rear_crossed_s=rd,
            rule="main-center crossing; front first; no per-step descent claim")
        full_complete = bool(route.get("completed") and route.get("front_main_before_rear_main") and supported)
        full = stage(bool(steps[0]["front"]["first_approach_s"] is not None), full_complete,
            route.get("completion_time_s"), ["unverified main-wheel edge support/order: " + str(bad)] if bad else [])
        # A full-route verdict additionally needs the complete 300s validity.
        if not full_valid:
            full["status"], full["trusted"] = "UNVERIFIED", False
        out["stages"]["fullroute"] = full
        geometric = [s["step"] for s in steps if s["both_geometrically_cleared"]]
        supported_steps = [s["step"] for s in steps if s["trusted_pair_clearance"]]
        if not steps[0]["both_geometrically_cleared"]:
            location = "entrance"
        elif not steps[17]["both_geometrically_cleared"]:
            location = "main18_late" if steps[9]["both_geometrically_cleared"] else "main18_middle"
        elif not steps[18]["both_geometrically_cleared"]:
            location = "tail19"
        elif not steps[19]["both_geometrically_cleared"]:
            location = "tail20"
        elif ascent_time is None:
            location = "top_platform"
        elif fd is None:
            location = "platform_before_down_entry"
        elif not route.get("completed"):
            location = "descent"
        else:
            location = "route_complete"
        out["progress"] = dict(last_both_main_geometrically_cleared_step=max(geometric, default=0),
            last_both_main_supported_clearance_step=max(supported_steps, default=0), terminal_location=location,
            physical_stall_stage=location if full_valid and report["classification"] == "PHYSICAL_STALL" else None)
        out["torque_limits"] = report.get("torque_limits")
        return out
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        out["validity"]["issues"].append("stage readback failed: " + str(exc))
        out["stages"] = {name: dict(status="UNVERIFIED", reached=None, completion_time_s=None,
            issues=["stage interface failed; no physical-failure conclusion"]) for name in STAGE_NAMES}
        return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = check_stage(args.run_dir)
    _OLD.write_json(args.out or args.run_dir / "stage.json", report)
    print(json.dumps({k: v["status"] for k, v in report["stages"].items()}))


if __name__ == "__main__":
    main()
