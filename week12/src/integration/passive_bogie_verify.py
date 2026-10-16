"""Independent verifier for the single frozen passive-bogie candidate.

This is an exact specialization, not a generic passive vehicle backend.  The
unchanged vehicle verifier supplies per-body kinetic energy, own-frame terrain
envelopes, whole-system external balance and the original task criteria.  Its
sanity function is cloned into private globals solely to add independently
reconstructed spring storage to its mechanical energy calculation.  No old
module globals, proposal, physical thresholds or result files are modified.

Review notes: old case-name dispatch cannot be reused for the new flat case;
the identical brake criteria are dispatched explicitly below.  The old main
pitch settling criterion is supplemented by the frozen bogie settling clause.
The 60 degree absolute-pitch criterion remains the original main-frame check;
bogie absolute pitch is diagnostic only.  Historical descent clauses are not
executed.  A declared joint-domain violation is a failure, not a hard stop.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import sys
import traceback
from types import FunctionType
from typing import Any
import xml.etree.ElementTree as ET

from . import vehicle_verify as old
from ..composition.schema import digest

ROOT = Path(__file__).resolve().parents[2]
FROZEN = ROOT / "results/s3-c-passive-bogie-preparation-20261006-01"
BUNDLE_DIGEST = "3048bd7ac34cb8814114743fc84b8636e6ece72b18d55033624763be1333d030"
CONTRACT_SHA = "575f85c1a7dfd5504789f56050420464dc6040b5aba181b7ef1fa404e1051ed9"
CASE_ORDER = ("passive_spring_probe", "passive_bogie_flat_brake", "passive_bogie_three_up")
NEW_OUTPUTS = ("bogieRelativeAngle", "bogieRelativeSpeed", "bogieSpringEnergy", "bogieRestoringTorque")
PROBE_OUTPUTS = ("time", "q", "w", "alpha", "springTorque", "elasticEnergy", "totalEnergy",
                 "negativeTorque", "zeroTorque", "positiveTorque")
CARRIERS = {"rear": "chassis", "front": "bogie", "nose": "bogie"}


def _require_frozen(bundle, contract, case_id):
    if case_id not in CASE_ORDER:
        raise old._EvidenceProblem("exact_passive_three_case_scope_required")
    if digest(bundle) != BUNDLE_DIGEST or contract.get("bundle_digest") != BUNDLE_DIGEST:
        raise old._EvidenceProblem("exact_passive_bundle_required")
    raw = (FROZEN / "verification-contract.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != CONTRACT_SHA or json.loads(raw) != contract:
        raise old._EvidenceProblem("exact_frozen_passive_contract_required")


def required_columns(bundle, case_id):
    """Public filter inventory, including every original per-body observation."""
    if case_id not in CASE_ORDER:
        raise old._EvidenceProblem("exact_passive_three_case_scope_required")
    if case_id == "passive_spring_probe":
        return set(PROBE_OUTPUTS)
    return old._required_columns(bundle, case_id) | set(NEW_OUTPUTS)


def expected_numerics(contract, case_id, variant):
    if variant not in ("base", "refined"):
        raise old._EvidenceProblem("explicit_base_or_refined_variant_required")
    rows = contract["passive_spring_probe"]["variants"] if case_id == CASE_ORDER[0] else contract["numerical"]
    return {**deepcopy(rows[variant]), "keep_event_rows": True}


def _stop(contract, bundle, case_id):
    return (old._value(contract["passive_spring_probe"]["stop_time_s"])
            if case_id == CASE_ORDER[0] else old._stop_time(bundle, contract, case_id))


def _runtime_check(contract, case_id, variant, runtime, checks, metrics):
    expected = expected_numerics(contract, case_id, variant)
    if runtime is None:
        old._add(checks, "actual_variant_numerics_and_event_rows", "missing", expected,
                 False, evidence=False)
        return
    actual = runtime.get("numerics", {})
    passed = (runtime.get("status") == "PASS_BEFORE_SIMULATION" and actual == expected
              and type(actual.get("keep_event_rows")) is bool)
    old._add(checks, "actual_variant_numerics_and_event_rows", actual, expected, passed)
    metrics["runtime_evidence_digest"] = digest(runtime)
    metrics["actual_numerics"] = deepcopy(actual)


def _identity(checks, name, observed, expected, absolute=1e-8, relative=1e-6):
    worst, maximum = 0.0, 0.0
    for a, b in zip(observed, expected):
        error = abs(a-b)
        limit = absolute + relative*max(abs(a), abs(b))
        worst = max(worst, error/limit)
        maximum = max(maximum, error)
    old._add(checks, name, {"max_error": maximum, "max_tolerance_ratio": worst},
             {"absolute": absolute, "relative": relative, "rule": "absolute+relative*scale"}, worst <= 1)


def _joint_series(data):
    q = tuple(a-b for a, b in zip(data.col("body_bogie_pitch"), data.col("body_chassis_pitch")))
    w = tuple(a-b for a, b in zip(data.col("body_bogie_pitchRate"), data.col("body_chassis_pitchRate")))
    energy = tuple(160*max(a, 0)**2 for a in q)
    torque = tuple(-320*max(a, 0) for a in q)
    return q, w, energy, torque


def _vehicle_energy(bundle, data):
    """Preserve original scale/work/loss accounting, add internal spring U once."""
    energy, residual = old._vehicle_energy(bundle, data)
    _, _, spring, _ = _joint_series(data)
    residual = [r+u-spring[0] for r, u in zip(residual, spring)]
    energy.update(spring_initial_J=spring[0], spring_final_J=spring[-1],
                  spring_change_J=spring[-1]-spring[0], spring_peak_J=max(spring),
                  mechanical_residual_max_J=max(map(abs, residual)))
    return energy, residual


# Only this function's internal energy dispatch is specialized.  All its
# electrical, normal/loss, ratings and absolute rolling checks are unchanged.
_sanity_globals = dict(vars(old), _vehicle_energy=_vehicle_energy)
_vehicle_sanity = FunctionType(old._vehicle_sanity.__code__, _sanity_globals,
                              old._vehicle_sanity.__name__, old._vehicle_sanity.__defaults__,
                              old._vehicle_sanity.__closure__)
_vehicle_sanity.__kwdefaults__ = deepcopy(old._vehicle_sanity.__kwdefaults__)


def _articulated(bundle, contract, data, checks, metrics):
    q, w, spring, torque = _joint_series(data)
    for name, values in zip(NEW_OUTPUTS, (q, w, spring, torque)):
        _identity(checks, name+"_independent_body_reconstruction", data.col(name), values)
    low, high = contract["articulated_checking"]["joint_domain_rad"]
    old._add(checks, "declared_relative_joint_domain", [min(q), max(q)], [low, high],
             min(q) >= low and max(q) <= high)
    _identity(checks, "main_pitch_output_identity", data.col("chassisPitch"), data.col("body_chassis_pitch"))
    _identity(checks, "main_pitch_rate_output_identity", data.col("chassisPitchRate"), data.col("body_chassis_pitchRate"))
    for wheel, carrier in CARRIERS.items():
        omega = data.col("wheel_"+wheel+"_omega")
        _identity(checks, wheel+"_absolute_roll_from_wheel_body", omega,
                  tuple(-v for v in data.col("body_"+wheel+"_pitchRate")))
        # WheelPair positive roll is minus body pitch rate; its positive shaft
        # roll is therefore omega_absolute + carrier.pitchRate, not minus it.
        relative = tuple(a+b for a, b in zip(omega, data.col("body_"+carrier+"_pitchRate")))
        _identity(checks, wheel+"_relative_roll_to_"+carrier,
                  data.col("wheel_"+wheel+"_omegaRelative"), relative)
        _identity(checks, wheel+"_shaft_power_from_actual_carrier",
                  data.col("wheel_"+wheel+"_shaftPower"),
                  tuple(t*v for t, v in zip(data.col("wheel_"+wheel+"_torque"), relative)))
    start, stop = contract["physical"]["settled_window_s"]
    bogie_rate = max(map(abs, old._window(data, "body_bogie_pitchRate", start, stop)))
    limit = contract["physical"]["settled_pitch_rate_max_rad_s"]
    old._add(checks, "settled_bogie_pitch_rate", bogie_rate, limit, bogie_rate <= limit)
    # Main+bogie vertical speeds are already included in the unchanged old
    # all-component settled_vertical_speed check; make the carrier clause clear.
    for carrier in ("chassis", "bogie"):
        speed = max(map(abs, old._window(data, "body_"+carrier+"_vz", start, stop)))
        limit = contract["physical"]["settled_vertical_speed_max_m_s"]
        old._add(checks, "settled_"+carrier+"_vertical_speed", speed, limit, speed <= limit)
    storage_rate = tuple(320*max(a, 0)*v for a, v in zip(q, w))
    # Use the separately observed cut torque with independently reconstructed
    # qdot here; do not manufacture zero power residual from the same formula.
    internal_power = tuple(t*v for t, v in zip(data.col("bogieRestoringTorque"), w))
    _identity(checks, "passive_spring_algebraic_power_balance", internal_power,
              tuple(-p for p in storage_rate))
    input_work = old._cumulative(data.time, (-p for p in internal_power))
    residual = max(abs(u-spring[0]-work) for u, work in zip(spring, input_work))
    work_scale = max(1.0, old._trapz(data.time, map(abs, internal_power)),
                     max(spring)-min(spring))
    rules = contract["numerical_comparison"]
    limit = max(rules["energy_abs_J"], rules["energy_relative"]*work_scale)
    old._add(checks, "passive_spring_integrated_storage_balance", residual, limit, residual <= limit)
    metrics.update(joint={"q_min_rad": min(q), "q_max_rad": max(q),
                          "qdot_peak_rad_s": max(map(abs, w)), "spring_initial_J": spring[0],
                          "spring_final_J": spring[-1], "spring_peak_J": max(spring),
                          "storage_work_J": input_work[-1], "storage_residual_max_J": residual,
                          "storage_work_scale_J": work_scale},
                   bogie_absolute_pitch_peak_rad_diagnostic=max(map(abs, data.col("body_bogie_pitch"))))
    metrics["cumulative"].update(spring_stored_change=spring[-1]-spring[0], spring_storage_work=input_work[-1])


def _vehicle_case(bundle, contract, case_id, data, checks, metrics):
    case = old._case(bundle, case_id)
    _vehicle_sanity(bundle, contract, data, checks, metrics)
    terrain = old._geometry(bundle, case, data, contract, checks, metrics)
    old._settled_balance(bundle, data, contract, terrain, checks, metrics)
    old._task_common(bundle, case, data, contract, terrain, checks, metrics)
    _articulated(bundle, contract, data, checks, metrics)
    if case_id == "passive_bogie_flat_brake":
        start, stop = map(float, contract["physical"]["brake_stop_window_s"])
        speed = max(map(abs, old._window(data, "body_chassis_vx", start, stop)))
        omega = max(max(map(abs, old._window(data, "wheel_"+wheel["id"]+"_omega", start, stop)))
                    for wheel in old._wheels(bundle))
        distance = max(data.col("body_chassis_x"))-old._interp(data, "body_chassis_x", 12.0)
        for name, value, key in (("brake_vehicle_speed", speed, "brake_speed_max_m_s"),
                                 ("brake_wheel_speed", omega, "brake_wheel_speed_max_rad_s"),
                                 ("brake_stop_distance", distance, "brake_stop_distance_from_t12_max_m")):
            limit = old._value(contract["physical"][key])
            old._add(checks, name, value, limit, value <= limit)
    else:
        if case.get("direction") != "ascent" or case.get("steps") != 3:
            raise old._EvidenceProblem("exact_three_up_only_no_descent")
        old._stair_events(bundle, case, data, contract, terrain, checks, metrics)
        old._exit_and_cruise(bundle, case, data, contract, terrain, checks, metrics)
    signals = {}
    wheel_ids = {row["id"] for row in old._wheels(bundle)}
    for row in old._mass_components(bundle):
        for suffix in ("x", "z", "vx", "vz", "pitch", "pitchRate"):
            if row["id"] in wheel_ids and suffix in ("pitch", "pitchRate"):
                continue  # Wheel angles wrap; compare absolute roll separately.
            name = "body_"+row["id"]+"_"+suffix
            values = data.col(name)
            signals[name] = {"peak_abs": max(map(abs, values)), "terminal": values[-1]}
    for wheel in old._wheels(bundle):
        for suffix in ("omega", "normal"):
            name = "wheel_"+wheel["id"]+"_"+suffix
            values = data.col(name)
            signals[name] = {"peak_abs": max(map(abs, values)), "terminal": values[-1]}
    metrics["signals"] = signals


def _spring_probe(contract, data, checks, metrics):
    probe = contract["passive_spring_probe"]
    tolerance = probe["tolerances"]
    frequency = math.sqrt(320)
    crossing = math.pi/(2*frequency)
    q, w = data.col("q"), data.col("w")
    expected_q = tuple(.1*math.cos(frequency*t) if t <= crossing else -.1*frequency*(t-crossing)
                       for t in data.time)
    expected_w = tuple(-.1*frequency*math.sin(frequency*t) if t <= crossing else -.1*frequency
                       for t in data.time)
    for name, values, reference, limit in (
            ("analytical_release_q", q, expected_q, tolerance["q_abs_rad"]),
            ("analytical_release_w", w, expected_w, tolerance["w_abs_rad_s"])):
        error = max(abs(a-b) for a, b in zip(values, reference))
        old._add(checks, name, error, limit, error <= limit)
    static = probe["static_cases"]
    for name, expected, limit in (
            ("negativeTorque", 0.0, static["zero_torque_abs_tolerance_Nm"]),
            ("zeroTorque", 0.0, static["zero_torque_abs_tolerance_Nm"]),
            ("positiveTorque", 32.0, static["positive_torque_abs_tolerance_Nm"])):
        error = max(abs(a-expected) for a in data.col(name))
        old._add(checks, name+"_exact_static_branch", error, limit, error <= limit)
    spring = tuple(160*max(a, 0)**2 for a in q)
    cut = tuple(320*max(a, 0) for a in q)
    _identity(checks, "probe_cut_torque_exact_law", data.col("springTorque"), cut)
    _identity(checks, "probe_storage_exact_law", data.col("elasticEnergy"), spring)
    _identity(checks, "probe_acceleration_restoring_sign", data.col("alpha"), tuple(-v for v in cut))
    # Check every initialization row; do not take only the post-event t=0 row.
    initial = [i for i, t in enumerate(data.time) if t == 0]
    if not initial:
        raise old._EvidenceProblem("probe_exact_initialization_sample_missing")
    for column, expected, limit in (("q", .1, tolerance["q_abs_rad"]),
                                    ("w", 0., tolerance["w_abs_rad_s"]),
                                    ("alpha", -32., tolerance["initial_acceleration_abs_rad_s2"])):
        error = max(abs(data.col(column)[i]-expected) for i in initial)
        old._add(checks, "probe_initial_"+column, error, limit, error <= limit)
    total = tuple(.5*v*v+u for v, u in zip(w, spring))
    for name, values in (("reconstructed_energy", total), ("reported_total_energy", data.col("totalEnergy"))):
        error = max(abs(e-1.6) for e in values)
        limit = tolerance["energy_abs_J"]
        old._add(checks, "probe_"+name+"_conserved", error, limit, error <= limit)
    _identity(checks, "probe_total_energy_identity", data.col("totalEnergy"), total)
    crossings = old._crossings(data.time, q, 0.0, -1)
    if not crossings:
        old._add(checks, "probe_release_crossing_time", "missing", crossing, False, evidence=False)
    else:
        difference = abs(crossings[0]-crossing)
        limit = tolerance["crossing_time_abs_s"]
        old._add(checks, "probe_release_crossing_time", difference, limit,
                 len(crossings) == 1 and difference <= limit)
    metrics.update(crossing_time_s=crossings[0] if crossings else None,
                   analytic_crossing_s=crossing,
                   reconstructed_energy_peak_error_J=max(abs(e-1.6) for e in total),
                   initialization_rows=len(initial),
                   signals={name: {"peak_abs": max(map(abs, values)), "terminal": values[-1]}
                            for name, values in (("q", q), ("w", w), ("energy", total))})


def verify(bundle, contract, case_id, csv_path, *, variant="base", runtime_evidence=None):
    """Verify raw observations and explicit variant runtime metadata, fail closed."""
    checks, diagnostics = [], []
    metrics: dict[str, Any] = {"case_id": case_id, "variant": variant,
                               "verifier": "exact_passive_bogie_independent_1",
                               "bundle_digest": BUNDLE_DIGEST,
                               "verification_digest": digest(contract)}
    try:
        _require_frozen(bundle, contract, case_id)
        numerical = expected_numerics(contract, case_id, variant)
        data = old._read_csv(Path(csv_path), required_columns(bundle, case_id),
                             _stop(contract, bundle, case_id), numerical["output_interval_s"])
        metrics.update(csv_sha256=hashlib.sha256(Path(csv_path).read_bytes()).hexdigest(),
                       row_count=data.row_count, observed_start_s=data.time[0], observed_stop_s=data.time[-1],
                       duplicate_time_rows=sum(a == b for a, b in zip(data.time, data.time[1:])))
        _runtime_check(contract, case_id, variant, runtime_evidence, checks, metrics)
        if case_id == CASE_ORDER[0]:
            _spring_probe(contract, data, checks, metrics)
        else:
            _vehicle_case(bundle, contract, case_id, data, checks, metrics)
    except (old._EvidenceProblem, OSError, UnicodeError) as exc:
        diagnostics.append(str(exc))
    except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError, NameError, IndexError) as exc:
        _calculation_diagnostic(metrics, diagnostics, "verification", exc)
    return old._finish(checks, metrics, diagnostics)


def _calculation_diagnostic(metrics, diagnostics, phase, exc):
    """Preserve an actionable exception; it is not automatic repair authority."""
    item = {"phase": phase, "type": type(exc).__name__, "message": str(exc),
            "traceback": traceback.format_exc(), "requires_source_diagnosis": True,
            "not_a_physical_failure_reclassification": True}
    metrics.setdefault("calculation_exceptions", []).append(item)
    diagnostics.append(phase+"_input_or_calculation_error:"+type(exc).__name__+":"+str(exc))


def _sample_values(data, column, when):
    """All event sides at a sampled timestamp, or interpolated unique value."""
    left = old.bisect.bisect_left(data.time, when)
    right = old.bisect.bisect_right(data.time, when)
    return data.col(column)[left:right] if right > left else (old._interp(data, column, when),)


def _aligned(checks, name, a, b, column, floor, relative):
    worst = maximum = 0.0
    for when in sorted(set(a.time) | set(b.time)):
        av, bv = _sample_values(a, column, when), _sample_values(b, column, when)
        # Preserve pre/post event sides. When row counts differ, compare first
        # to first and last to last and each retained interior side to a peer.
        if len(av) == len(bv) == 1:
            pairs = [(av[0], bv[0])]
        else:
            pairs = [(av[0], bv[0]), (av[-1], bv[-1])]
            pairs.extend((x, bv[min(i, len(bv)-1)]) for i, x in enumerate(av))
            pairs.extend((av[min(i, len(av)-1)], x) for i, x in enumerate(bv))
        for x, y in pairs:
            error = abs(x-y)
            limit = max(floor, relative*max(abs(x), abs(y)))
            maximum = max(maximum, error)
            worst = max(worst, error/limit)
    old._add(checks, name+"_aligned_state", {"max_error": maximum, "max_tolerance_ratio": worst},
             {"absolute_floor": floor, "relative": relative, "retained_event_sides": True}, worst <= 1)


def _bound_report(bundle, contract, case_id, path, variant, report, runtime):
    if report is None:
        return verify(bundle, contract, case_id, path, variant=variant, runtime_evidence=runtime)
    metrics = report.get("metrics", {})
    expected = {"csv_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                "case_id": case_id, "variant": variant,
                "verifier": "exact_passive_bogie_independent_1", "bundle_digest": BUNDLE_DIGEST,
                "verification_digest": digest(contract)}
    if runtime is not None:
        expected["runtime_evidence_digest"] = digest(runtime)
    if any(metrics.get(key) != value for key, value in expected.items()):
        raise old._EvidenceProblem("reused_report_csv_candidate_contract_or_variant_mismatch")
    return report


def compare(bundle, contract, case_id, base_csv, refined_csv, *, base_report=None,
            refined_report=None, base_runtime=None, refined_runtime=None):
    """Original vehicle comparisons plus explicit joint/storage comparison."""
    checks, diagnostics, metrics = [], [], {"case_id": case_id}
    try:
        _require_frozen(bundle, contract, case_id)
        reports = [_bound_report(bundle, contract, case_id, path, variant, report, runtime)
                   for path, variant, report, runtime in (
                       (base_csv, "base", base_report, base_runtime),
                       (refined_csv, "refined", refined_report, refined_runtime))]
        for variant, report in zip(("base", "refined"), reports):
            old._add(checks, variant+"_physical_and_task", report["status"], "PASS",
                     report["status"] == "PASS", evidence=report["status"] != "INSUFFICIENT_EVIDENCE")
        metrics.update(base_status=reports[0]["status"], refined_status=reports[1]["status"])
        if any(report["status"] != "PASS" for report in reports):
            return old._finish(checks, metrics, diagnostics)
        data = [old._read_csv(Path(path), required_columns(bundle, case_id), _stop(contract, bundle, case_id),
                             expected_numerics(contract, case_id, variant)["output_interval_s"])
                for path, variant in ((base_csv, "base"), (refined_csv, "refined"))]
        a, b = data
        if case_id == CASE_ORDER[0]:
            tolerance = contract["passive_spring_probe"]["tolerances"]
            for column, floor in (("q", tolerance["q_abs_rad"]), ("w", tolerance["w_abs_rad_s"]),
                                  ("totalEnergy", tolerance["energy_abs_J"]),
                                  ("elasticEnergy", tolerance["energy_abs_J"])):
                _aligned(checks, "probe_"+column, a, b, column, floor, 0.0)
            crossing_error = abs(reports[0]["metrics"]["crossing_time_s"]-reports[1]["metrics"]["crossing_time_s"])
            limit = tolerance["crossing_time_abs_s"]
            old._add(checks, "probe_crossing_pair", crossing_error, limit, crossing_error <= limit)
        else:
            inherited = old._compare_reports(contract, *reports)
            checks.extend(inherited["checks"])
            diagnostics.extend(inherited["diagnostics"])
            metrics.update(inherited["metrics"])
            rules = contract["numerical_comparison"]
            for name in reports[0]["metrics"]["signals"]:
                if name.endswith("_normal"):
                    continue  # Named peak force comparison retained above.
                floor = (rules["position_abs_m"] if name.endswith(("_x", "_z")) else
                         rules["pitch_abs_rad"] if name.endswith("_pitch") else
                         rules["wheel_speed_abs_rad_s"] if name.endswith("_omega") else
                         rules["velocity_abs_m_s"])
                _aligned(checks, name, a, b, name, floor, rules["state_relative"])
            for column, floor, relative in (("bogieRelativeAngle", rules["pitch_abs_rad"], rules["state_relative"]),
                                            ("bogieRelativeSpeed", rules["velocity_abs_m_s"], rules["state_relative"]),
                                            ("bogieSpringEnergy", rules["energy_abs_J"], rules["energy_relative"])):
                _aligned(checks, column, a, b, column, floor, relative)
            for statistic in ("spring_initial_J", "spring_final_J", "spring_peak_J"):
                old._relative(checks, statistic+"_pair", reports[0]["metrics"]["joint"][statistic],
                              reports[1]["metrics"]["joint"][statistic],
                              rules["energy_relative"], rules["energy_abs_J"])
        metrics.update(base_metrics=reports[0]["metrics"], refined_metrics=reports[1]["metrics"])
    except (old._EvidenceProblem, OSError, UnicodeError) as exc:
        diagnostics.append(str(exc))
    except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError, NameError, IndexError) as exc:
        _calculation_diagnostic(metrics, diagnostics, "comparison", exc)
    return old._finish(checks, metrics, diagnostics)


def _runtime_from_folder(folder, contract, bundle, case_id, variant):
    """Recheck saved argv/XML evidence; never infer runtime settings from CSV."""
    folder = Path(folder)
    runtime = json.loads((folder / "actual-numerics.json").read_text(encoding="utf-8"))
    expected = expected_numerics(contract, case_id, variant)
    xml_path = folder / "model_init.xml"
    if hashlib.sha256(xml_path.read_bytes()).hexdigest() != runtime.get("init_xml_sha256"):
        raise old._EvidenceProblem("actual_runtime_xml_hash_mismatch")
    nodes = list(ET.parse(xml_path).getroot().iter("DefaultExperiment"))
    experiment = {"startTime": 0., "stopTime": _stop(contract, bundle, case_id),
                  "stepSize": expected["output_interval_s"], "tolerance": expected["tolerance"]}
    if (len(nodes) != 1 or any(float(nodes[0].get(key, "nan")) != value for key, value in experiment.items())
            or nodes[0].get("solver") != expected["solver"] or nodes[0].get("outputFormat") != "csv"
            or runtime.get("default_experiment") != experiment or runtime.get("numerics") != expected):
        raise old._EvidenceProblem("actual_runtime_xml_or_variant_numerics_mismatch")
    command = runtime.get("runner_command", [])
    if (len(command) != 4 or command[1:] != ["-s=dassl", f'-maxStepSize={expected["max_step_s"]}', "-r=result.csv"]
            or runtime.get("launch_command") != [*command, "-lv=LOG_STATS"]):
        raise old._EvidenceProblem("actual_runtime_argv_or_event_rows_mismatch")
    # OM disables event rows only via -noEventEmit; the exact allowed argv above
    # excludes it. CSV keeps duplicate times as delivered, including t=0 rows.
    return runtime


def _save_new(path, data):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def _emit_calculation_errors(report):
    # Emit the original captured traceback, not a made-up wrapper exception.
    # The bounded runner still diagnoses it and must prioritize any real FAIL.
    for item in report.get("metrics", {}).get("calculation_exceptions", []):
        sys.stderr.write(item["traceback"])
    sys.stderr.flush()


def verify_scene(folder, bundle, contract, case_id, base=None):
    """Metered postprocess API: write fresh physical/numerical reports once.

    The runner owns result.json and process/budget status. This routine creates
    physical.json and (only after both physical gates pass) numerical.json. It
    returns status fields suitable for the runner; it launches no subprocesses.
    """
    folder = Path(folder)
    variant = "refined" if folder.name.endswith("-refined") else "base" if folder.name.endswith("-base") else None
    try:
        _require_frozen(bundle, contract, case_id)
        if folder.name != case_id+"-"+str(variant):
            raise old._EvidenceProblem("same_case_variant_scene_folder_required")
        runtime = _runtime_from_folder(folder, contract, bundle, case_id, variant)
        physical = verify(bundle, contract, case_id, folder / "result.csv", variant=variant,
                          runtime_evidence=runtime)
    except (old._EvidenceProblem, OSError, UnicodeError, ValueError, TypeError, KeyError, ET.ParseError) as exc:
        physical = old._finish([], {"case_id": case_id, "variant": variant}, [str(exc)])
    _save_new(folder / "physical.json", physical)
    _emit_calculation_errors(physical)
    result = {"physical": physical["status"], "numeric": "UNVERIFIED", "status": physical["status"]}
    if base is not None and physical["status"] == "PASS":
        try:
            if variant != "refined":
                raise old._EvidenceProblem("pair_target_must_be_refined")
            base = Path(base)
            if base.name != case_id+"-base" or base.parent.resolve() != folder.parent.resolve():
                raise old._EvidenceProblem("same_case_base_folder_required")
            base_runtime = _runtime_from_folder(base, contract, bundle, case_id, "base")
            base_report = json.loads((base / "physical.json").read_text(encoding="utf-8"))
            numerical = compare(bundle, contract, case_id, base / "result.csv", folder / "result.csv",
                                base_report=base_report, refined_report=physical,
                                base_runtime=base_runtime, refined_runtime=runtime)
        except (old._EvidenceProblem, OSError, UnicodeError, ValueError, TypeError, KeyError, ET.ParseError) as exc:
            numerical = old._finish([], {"case_id": case_id}, [str(exc)])
        _save_new(folder / "numerical.json", numerical)
        _emit_calculation_errors(numerical)
        result.update(numeric=numerical["status"], status=numerical["status"])
    elif variant == "refined" and physical["status"] == "PASS":
        result["status"] = "INSUFFICIENT_EVIDENCE"
    return result
