"""Owned Asym2 compile/run and independent result adapter for ordinary Agent work.

Physics, contact, energy and sampled stage algorithms are copied under asym2/;
this module only binds the confirmed spec, actual processes and their files.
No historic scope, runner or ledger is executed or resumed.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import xml.etree.ElementTree as ET

from . import case_paths as paths
from .case_progress import emit_progress
from .inherited import toolchain_original as inherited
from week9.src.modelica_tool import _terminate_process_tree

MODEL = "ZLTECH_Full_Mission_Asym2"
VARIANT = "AgentAsym2Build"
PARAMETERS = ("m", "g", "I_pitch", "R_main", "R_nose", "T_main_max", "T_nose_max",
              "a", "b", "L_nose_ext", "L_nose_CoM", "h_nose", "stepH", "stepD",
              "h19", "h20", "d19", "d20", "nEndSteps", "n_steps", "x_start_up",
              "len_flat", "x_end_up", "x_start_down", "x_end_down", "H_peak",
              "x_last_up", "x_last_down", "fixedTopTread", "k_ground", "c_ground",
              "mu_s", "v_eps", "target_speed", "speed_error_scale", "driveScale",
              "nReach", "nLocal", "nContact", "wheel_J[1]", "wheel_J[2]", "wheel_J[3]",
              "x0", "z0", "psi0", "v0", "vz0", "omega0")
GEOMETRY = ("n_steps", "stepH", "stepD", "h19", "h20", "d19", "d20", "nEndSteps")


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf8")


def binding(path):
    path = Path(path).resolve()
    return dict(path=str(path), sha256=sha(path) if path.is_file() else None)


def _flatten(values):
    result = {}
    for key, value in values.items():
        if isinstance(value, (list, tuple)):
            result.update({f"{key}[{i}]": float(item) for i, item in enumerate(value, 1)})
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            result[key] = float(value)
    return result


def confirmed_expectations(spec):
    """The mass/H/D expectations originate from the approved design metadata.

    Never accept a source-only execution and never fill expectations from XML,
    stdout or an actual CSV. The owned worker verifies confirmation/spec digest.
    """
    from .case_asym2_family import expected_parameters, is_asym2_source
    if not isinstance(spec, dict) or not is_asym2_source(spec.get("artifact", {}).get("modelica_source", "")):
        raise ValueError("Asym2 execution requires the confirmed full candidate spec")
    values = _flatten(expected_parameters(spec))
    missing = sorted(set(PARAMETERS)-set(values))
    if missing or any(not math.isfinite(values[k]) for k in PARAMETERS):
        raise ValueError("confirmed parameter expectations incomplete/nonfinite: "+",".join(missing))
    return {name: values[name] for name in PARAMETERS}


def _mismatches(expected, actual, *, rel_tol=1e-8, abs_tol=1e-9):
    return {key: dict(expected=value, actual=actual.get(key)) for key, value in expected.items()
            if key not in actual or not math.isfinite(actual[key])
            or not math.isclose(value, actual[key], rel_tol=rel_tol, abs_tol=abs_tol)}


def _source_values(source):
    from .case_asym2_family import modelica_parameters
    values = modelica_parameters(source)
    return _flatten(values[0] if isinstance(values, tuple) else values)


def _read_xml(path):
    values, calculated, declared = {}, set(), set()
    root = ET.parse(path).getroot()
    for node in root.iter("ScalarVariable"):
        name = node.get("name")
        if name in declared:
            raise ValueError("duplicate compiled variable: "+str(name))
        declared.add(name)
        if node.get("causality") == "calculatedParameter":
            calculated.add(name)
        child = next(iter(node), None)
        if child is not None and child.get("start") is not None:
            try:
                value = float(child.get("start"))
                if math.isfinite(value): values[name] = value
            except (TypeError, ValueError):
                pass
    experiment = root.find(".//DefaultExperiment")
    return values, calculated, declared, dict(experiment.attrib) if experiment is not None else {}


def _compiled_readback(path, expected):
    values, calculated, declared, experiment = _read_xml(path)
    # Derived parameters without starts must still be declared and then read
    # directly by -output. Direct mass and all other literal starts must match.
    missing = sorted(set(expected)-declared)
    direct = {k: v for k, v in expected.items() if k not in calculated}
    mismatch = _mismatches(direct, values, rel_tol=1e-10, abs_tol=1e-12)
    for key in set(expected) & set(values):
        if not math.isclose(expected[key], values[key], rel_tol=1e-10, abs_tol=1e-12):
            mismatch[key] = dict(expected=expected[key], actual=values[key])
    settings_mismatch={}
    for key,value in dict(startTime=0.,stopTime=300.,stepSize=.01,tolerance=1e-5).items():
        try:matches=math.isclose(float(experiment[key]),value,rel_tol=1e-10,abs_tol=1e-12)
        except (KeyError,ValueError,TypeError):matches=False
        if not matches:settings_mismatch[key]=dict(expected=value,actual=experiment.get(key))
    if experiment.get("solver")!="dassl":settings_mismatch["solver"]=dict(expected="dassl",actual=experiment.get("solver"))
    return dict(status="PASS" if not missing and not mismatch and not settings_mismatch else "FAIL", expected=expected,
                actual={k: values[k] for k in expected if k in values}, missing=missing,
                mismatches=mismatch, calculated_parameters=sorted(calculated & set(expected)),
                compiled_default_experiment=experiment,settings_mismatch=settings_mismatch)


def _launch(command, cwd, kind, cap):
    """Use the product's current request ledger and one owned process tree."""
    paths.require_time(); paths.inside_write(cwd)
    task = os.environ.get("V12_CASE_TASK_ID")
    if not task: raise ValueError("tool launch requires owned current V12 task identity")
    timeout = min(float(cap), float(paths.authorization()["timeouts_s"][kind]), paths.remaining())
    if timeout <= 0: raise RuntimeError("shared hard deadline reached before process launch")
    detail = dict(task_id=task, command=list(command), cwd=str(cwd), timeout_s=timeout)
    attempt = paths.process_attempt(kind, detail)
    record = dict(detail, attempt_id=attempt, pid=None, returncode=None, timed_out=False,
                  uncertain=False, cleanup_confirmed=True, launch_error=None,
                  started_utc=paths.now().isoformat())
    save(Path(cwd)/(kind+".command.json"), record)
    environment = dict(os.environ)
    environment["PATH"] = str(inherited._OMC.parent)+os.pathsep+environment.get("PATH", "")
    options = dict(cwd=str(cwd), env=environment, stdin=subprocess.DEVNULL,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if os.name == "nt": options["creationflags"] = subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP
    else: options["start_new_session"] = True
    begin = time.monotonic(); raw_out, raw_err = b"", b""
    try:
        process = subprocess.Popen(command, **options)
    except OSError as exc:
        record.update(launch_error=str(exc), error=str(exc))
    else:
        record["pid"] = process.pid
        paths.mark_process_launched(attempt, process.pid)
        save(Path(cwd)/(kind+".command.json"), record)
        emit_progress(kind, "正在真实编译Asym2模型。" if kind=="compile" else "正在真实运行Asym2完整上下楼观察。", task_id=task)
        try:
            raw_out, raw_err = process.communicate(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            cleaned, error = _terminate_process_tree(process)
            try: raw_out, raw_err = process.communicate(timeout=5) if cleaned else (b"", b"")
            except subprocess.TimeoutExpired: cleaned=False
            record.update(timed_out=isinstance(exc, subprocess.TimeoutExpired),
                          interrupted=isinstance(exc, KeyboardInterrupt), uncertain=not cleaned,
                          cleanup_confirmed=cleaned, cleanup_error=error)
        record["returncode"] = process.poll()
    (Path(cwd)/(kind+".stdout.log")).write_bytes(raw_out)
    (Path(cwd)/(kind+".stderr.log")).write_bytes(raw_err)
    record.update(elapsed_s=time.monotonic()-begin, ended_utc=paths.now().isoformat(),
                  stdout=binding(Path(cwd)/(kind+".stdout.log")), stderr=binding(Path(cwd)/(kind+".stderr.log")))
    save(Path(cwd)/(kind+".command.json"), record)
    paths.finish_process(attempt, record)
    return record


def _process_ok(record):
    return (record.get("returncode")==0 and record.get("timed_out") is False
            and record.get("uncertain") is False and record.get("cleanup_confirmed") is True
            and not record.get("launch_error"))


def execute_asym2_model(source, model_name, out, stop, intervals=30000,
                       simulation_timeout_s=300, *, confirmed_spec=None):
    expected = confirmed_expectations(confirmed_spec)
    if source != confirmed_spec["artifact"]["modelica_source"] or model_name != MODEL:
        raise ValueError("executed source differs from confirmed Asym2 candidate")
    if stop != 300 or intervals != 30000:
        raise ValueError("Asym2 requires the approved 0..300s/.01s observation")
    mismatch = _mismatches(expected, _source_values(source), rel_tol=1e-10, abs_tol=1e-12)
    if mismatch: raise ValueError("confirmed/source parameter mismatch: "+json.dumps(mismatch))
    run = paths.inside_write(out)
    if run.exists(): raise FileExistsError("Asym2 tool output must be a new directory")
    run.mkdir()
    (run/"model.mo").write_text(source, encoding="utf8", newline="\n")
    wrapper = f"model {VARIANT}\n  extends {MODEL};\nend {VARIANT};\n"
    (run/"wrapper.mo").write_text(wrapper, encoding="utf8", newline="\n")
    save(run/"confirmed-spec.json", confirmed_spec)
    result = dict(run_directory=str(run), source_path=str(run/"model.mo"),
        source_sha256=hashlib.sha256(source.encode("utf8")).hexdigest(), model_name=model_name,
        compile=dict(status="NOT_RUN", check_model="NOT_RUN"), simulation=dict(status="NOT_RUN"),
        actual_run_settings=dict(solver="dassl", tolerance=1e-5, max_step_s=.002,
                                 output_interval_s=.01, stop_time=300),
        runtime_parameter_readback=dict(status="UNKNOWN", expected=expected, actual={}))
    mos = f'''loadModel(Modelica);
loadFile("model.mo");
loadFile("wrapper.mo");
print("__V11_CHECK_MODEL__=" + checkModel({VARIANT}) + "\\n");
b := buildModel({VARIANT}, startTime=0, stopTime=300, numberOfIntervals=30000, tolerance=1e-5, method="dassl", outputFormat="csv", fileNamePrefix="model");
print("__V11_BUILD_EXE__=" + b[1] + "\\n");
print("__V11_ERRORS__=" + getErrorString() + "\\n");
'''
    (run/"build.mos").write_text(mos, encoding="utf8", newline="\n")
    compile_record = _launch([str(inherited._OMC), "build.mos"], run, "compile", 180)
    stdout = (run/"compile.stdout.log").read_text(encoding="utf8", errors="replace")
    check_text = inherited._marker(stdout, "__V11_CHECK_MODEL__")
    exe_text = inherited._marker(stdout, "__V11_BUILD_EXE__")
    exe = inherited._resolve_generated(run, exe_text)
    xml = run/"model_init.xml"
    compiled = _compiled_readback(xml, expected) if xml.is_file() else dict(status="UNKNOWN", issues=["compiled XML absent"])
    check_ok = _process_ok(compile_record) and inherited._check_model_succeeded(check_text)
    compile_ok = check_ok and exe is not None and exe.is_file() and compiled["status"]=="PASS"
    build = dict(schema="asym2-agent-build-1", status="SUCCESS" if compile_ok else "TOOL_FAILURE",
        source=binding(run/"model.mo"), wrapper=binding(run/"wrapper.mo"),
        confirmed_spec=binding(run/"confirmed-spec.json"), executable=binding(exe) if exe else dict(path="",sha256=None),
        init_xml=binding(xml), requested_geometry={k: expected[k] for k in GEOMETRY},
        compiled_parameter_readback=compiled, process=compile_record,
        process_receipt=binding(run/"compile.command.json"))
    save(run/"build.json", build)
    result["compile"] = dict(status="PASS" if compile_ok else "FAIL", check_model="PASS" if check_ok else "FAIL",
                            build_model="PASS" if compile_ok else "FAIL", process=compile_record,
                            compiled_parameter_readback=compiled, issues=[] if compile_ok else ["compile or source-bound XML readback failed"])
    if not compile_ok:
        save(run/"toolchain.json", result); return result
    command = [str(exe), "-s=dassl", "-r="+str(run/"model_res.csv"),
               "-override=startTime=0,stopTime=300,tolerance=1e-5,stepSize=0.01",
               "-maxStepSize=0.002", "-output="+",".join(PARAMETERS)]
    simulation_record = _launch(command, run, "simulation", min(300, float(simulation_timeout_s)))
    from .asym2.physics_check import read_runtime_line
    try:
        actual = read_runtime_line(run/"simulation.stdout.log")
        runtime_mismatch = _mismatches(dict(expected, time=300.), actual)
        output_error = None
    except (OSError, ValueError) as exc:
        actual={}; runtime_mismatch={}; output_error=str(exc)
    runtime_ok = _process_ok(simulation_record) and not runtime_mismatch and not output_error
    result["runtime_parameter_readback"] = dict(status="PASS" if runtime_ok else "UNKNOWN",
        expected=expected, actual={k:v for k,v in actual.items() if k!="time"},
        runtime_final_values=actual, mismatches=runtime_mismatch, issues=[output_error] if output_error else [],
        compiled_parameter_readback=compiled, compiled_default_experiment=compiled.get("compiled_default_experiment", {}),
        runtime_stdout_evidence=simulation_record)
    csv = run/"model_res.csv"
    simulation_ok = _process_ok(simulation_record) and csv.is_file() and csv.stat().st_size>0
    result["simulation"] = dict(status="PASS" if simulation_ok else "FAIL", process=simulation_record,
                                issues=[] if simulation_ok else ["owned simulation failed or CSV absent"])
    manifest = dict(schema="asym2-agent-run-1", status="SUCCESS" if simulation_ok else "TOOL_FAILURE",
        source=build["source"], wrapper=build["wrapper"], executable=build["executable"], init_xml=build["init_xml"],
        confirmed_spec=build["confirmed_spec"], build_manifest=binding(run/"build.json"),
        requested_stop=300., requested_overrides={}, requested_output_parameters=list(PARAMETERS),
        settings=result["actual_run_settings"], process=simulation_record,
        process_receipt=binding(run/"simulation.command.json"),
        files=dict(csv=binding(csv),stdout=binding(run/"simulation.stdout.log"),stderr=binding(run/"simulation.stderr.log")))
    save(run/"run.json", manifest)
    save(run/"toolchain.json", result)
    return result


def verify_run_evidence(run_dir, expected=None):
    """Independently re-read spec/source/build/XML/stdout; never trust PASS flags."""
    run = Path(run_dir).resolve()
    manifest = json.loads((run/"run.json").read_text(encoding="utf8")); issues=[]
    for key in ("source", "wrapper", "executable", "init_xml", "build_manifest", "confirmed_spec", "process_receipt"):
        record=manifest[key]
        if not Path(record["path"]).is_file() or sha(record["path"])!=record["sha256"]:
            raise ValueError("run input binding changed: "+key)
    for key, record in manifest["files"].items():
        if not Path(record["path"]).is_file() or sha(record["path"])!=record["sha256"]:
            raise ValueError("run output binding changed: "+key)
    build=json.loads(Path(manifest["build_manifest"]["path"]).read_text(encoding="utf8"))
    for key in ("source", "wrapper", "executable", "init_xml", "confirmed_spec"):
        if build.get(key)!=manifest[key]: issues.append("build/run binding differs: "+key)
    compile_receipt=build["process_receipt"]
    if not Path(compile_receipt["path"]).is_file() or sha(compile_receipt["path"])!=compile_receipt["sha256"]:
        raise ValueError("compile process receipt binding changed")
    compile_process=json.loads(Path(compile_receipt["path"]).read_text(encoding="utf8"))
    if (compile_process!=build["process"] or not _process_ok(compile_process) or not compile_process.get("attempt_id")
            or not compile_process.get("task_id") or type(compile_process.get("pid")) is not int or compile_process["pid"]<=0):
        issues.append("compile has no successful owned process receipt")
    if (compile_process.get("command")!=[str(inherited._OMC),"build.mos"]
            or Path(compile_process.get("cwd","")).resolve()!=run
            or not 0<compile_process.get("timeout_s",0)<=180):
        issues.append("compile command or process limit differs from approved execution")
    for key in ("stdout","stderr"):
        rec=compile_process[key]
        if not Path(rec["path"]).is_file() or sha(rec["path"])!=rec["sha256"]:
            raise ValueError("compile log binding changed: "+key)
    compile_stdout=Path(compile_process["stdout"]["path"]).read_text(encoding="utf8",errors="replace")
    if not inherited._check_model_succeeded(inherited._marker(compile_stdout,"__V11_CHECK_MODEL__")):
        issues.append("actual checkModel did not pass")
    built_exe=inherited._resolve_generated(Path(manifest["build_manifest"]["path"]).parent,inherited._marker(compile_stdout,"__V11_BUILD_EXE__"))
    if built_exe is None or str(built_exe)!=manifest["executable"]["path"]:
        issues.append("compile executable marker differs from run binding")
    spec=json.loads(Path(manifest["confirmed_spec"]["path"]).read_text(encoding="utf8"))
    wanted=confirmed_expectations(spec)
    if Path(manifest["source"]["path"]).read_text(encoding="utf8")!=spec["artifact"]["modelica_source"]:
        issues.append("executed source differs from confirmed spec")
    if Path(manifest["wrapper"]["path"]).read_text(encoding="utf8")!=f"model {VARIANT}\n  extends {MODEL};\nend {VARIANT};\n":
        issues.append("build wrapper is not the frozen identity adapter")
    source_mismatch=_mismatches(wanted,_source_values(spec["artifact"]["modelica_source"]),rel_tol=1e-10,abs_tol=1e-12)
    if source_mismatch: issues.append("source differs from independently confirmed expectations")
    if expected is not None and _mismatches(expected,wanted): issues.append("caller/spec expected parameters differ")
    compiled=_compiled_readback(manifest["init_xml"]["path"],wanted)
    if compiled["status"]!="PASS": issues.append("compiled parameters differ from confirmed expectations")
    from .asym2.physics_check import read_runtime_line
    actual=read_runtime_line(manifest["files"]["stdout"]["path"])
    mismatch=_mismatches(dict(wanted,time=300.),actual)
    if mismatch: issues.append("runtime stdout differs from independently confirmed expectations")
    receipt=json.loads(Path(manifest["process_receipt"]["path"]).read_text(encoding="utf8"))
    if (receipt!=manifest["process"] or not receipt.get("attempt_id") or not receipt.get("task_id")
            or type(receipt.get("pid")) is not int or receipt["pid"]<=0):
        issues.append("owned simulation receipt differs or has no process identity")
    if (receipt.get("task_id")!=compile_process.get("task_id") or Path(receipt.get("cwd","")).resolve()!=run
            or not 0<receipt.get("timeout_s",0)<=300):
        issues.append("compile/simulation owner or process bounds differ")
    if receipt.get("stdout")!=manifest["files"]["stdout"] or receipt.get("stderr")!=manifest["files"]["stderr"]:
        issues.append("owned stdout/stderr receipt differs")
    command=receipt.get("command",[])
    if not command or command[0]!=manifest["executable"]["path"]:
        issues.append("simulation command executable differs from compiled binding")
    required=("-s=dassl","-override=startTime=0,stopTime=300,tolerance=1e-5,stepSize=0.01","-maxStepSize=0.002","-output="+",".join(PARAMETERS))
    if command!=[manifest["executable"]["path"],required[0],"-r="+manifest["files"]["csv"]["path"],*required[1:]]:
        issues.append("actual solver/output command differs from approved settings")
    if manifest.get("requested_overrides")!={} or manifest.get("requested_output_parameters")!=list(PARAMETERS):
        issues.append("unauthorized physical overrides or output names")
    if manifest.get("settings")!=dict(solver="dassl",tolerance=1e-5,max_step_s=.002,output_interval_s=.01,stop_time=300):
        issues.append("run settings differ from approved observation")
    return manifest,actual,dict(status="PASS" if not issues else "FAIL",issues=issues,
        parameter_mismatch=dict(source=source_mismatch,runtime=mismatch) if source_mismatch or mismatch else {},
        compiled_geometry_mismatch=compiled.get("mismatches",{}),actual=actual,directly_read_back=actual.copy(),
        expected=wanted,confirmed_spec=manifest["confirmed_spec"],compiled_parameter_readback=compiled)


def assess_asym2(spec, csv_path):
    from .asym2.mixed_check import check
    result=dict(task_status="UNKNOWN",validity_status="UNKNOWN",route_status="UNKNOWN",diagnostic_status="UNKNOWN",
                overall_summary="EVIDENCE_UNKNOWN",completion_status="UNKNOWN",assessment_evidence_status="UNKNOWN",
                scope="ASym2_PLANAR_THREE_EQUIVALENT_WHEEL_GROUP_DIAGNOSTIC",issues=[],limitations=[
                    "三个纵向等效轮组；不包含车架碰撞、真实机电热约束或实物能力认证。",
                    "300秒是有限观察窗口，不是业务规定的完成时限。"])
    try:
        expected=confirmed_expectations(spec)
        path=Path(csv_path).resolve()
        report=check(path.parent,expected=expected,make_plot=False)
        bound=report.get("csv",{})
        if bound and (Path(bound["path"]).resolve()!=path or bound["sha256"]!=sha(path)):
            raise ValueError("assessed CSV differs from worker output")
        classification=report["classification"]
        valid=report.get("validity",{}).get("status")=="PASS"
        evidence_ok=report.get("evidence",{}).get("status")=="PASS" and report.get("tool_process",{}).get("status")=="PASS"
        result.update(classification=classification,independent_report=report,
            validity_status="PASS" if valid else "FAIL" if classification=="INVALID_MODEL" else "UNKNOWN",
            assessment_evidence_status="PASS" if evidence_ok else "UNKNOWN",
            diagnostic_status="PASS" if valid else "FAIL" if classification=="INVALID_MODEL" else "UNKNOWN",
            issues=report.get("invalidity_reasons",[])+report.get("issues",[]),
            completion_time=report.get("route",{}).get("completion_time_s"),
            route=report.get("route",{}),stage_report=report.get("stage_report",{}))
        mapping={"PASS":("PASS","PASS","COMPLETE_FORWARD_UP20_DOWN20_EXIT_AND_CONTINUE","TASK_COMPLETED"),
                 "PHYSICAL_STALL":("FAIL","FAIL","PHYSICAL_STALL","VALID_PHYSICAL_STALL"),
                 "INCOMPLETE":("UNKNOWN","INCOMPLETE","OBSERVATION_INTERVAL_INCOMPLETE","VALID_OBSERVATION_INCOMPLETE"),
                 "INVALID_MODEL":("UNKNOWN","UNKNOWN","INVALID_MODEL","INVALID_MODEL"),
                 "TOOL_FAILURE":("UNKNOWN","UNKNOWN","TOOL_FAILURE","TOOL_FAILURE")}
        task,route,completion,summary=mapping[classification]
        result.update(task_status=task,route_status=route,completion_status=completion,overall_summary=summary)
    except (OSError,ValueError,KeyError,TypeError,ET.ParseError) as exc:
        result.update(classification="TOOL_FAILURE",completion_status="TOOL_FAILURE",issues=[str(exc)])
    return result


def summarize_asym2_execution(spec,tool_result,csv_path):
    tool_ok=all(tool_result.get(k,{}).get("status")=="PASS" for k in ("compile","simulation"))
    result=dict(schema_version="v11-artifact-outcome-1",backend="model-artifact",tool_status="PASS" if tool_ok else "TOOL_FAIL",
        chain_status="PASS" if tool_ok else "BLOCKED_TOOL",task_status="UNKNOWN",vehicle_task_status="UNKNOWN",
        physical_acceptance_status="NOT_VERIFIED",physical_status="UNKNOWN",published_version_id=None,
        observations=[],tool_result=tool_result,runtime_parameter_readback=tool_result.get("runtime_parameter_readback",{}))
    if not tool_ok:return result
    from .inherited.artifact_delivery import read_signals,SignalEvidenceError
    try:
        samples=read_signals(spec,csv_path)
    except SignalEvidenceError as exc:
        result.update(chain_status="OUTPUT_CONTRACT_MISMATCH" if exc.missing else "BLOCKED_EVIDENCE",evidence_issues=[str(exc)],output_mapping=exc.mapping)
        if exc.missing:result["missing_output_columns"]=exc.missing
        return result
    result.update(output_mapping=samples["mapping"],output_contract_status="PASS")
    mission=assess_asym2(spec,csv_path)
    result.update(mission_assessment=mission,**{k:mission[k] for k in ("task_status","route_status","diagnostic_status","overall_summary","completion_status")},
                  task_scope="ASym2_BOUND_CSV_CONTACT_ENERGY_AND_ORDERED_ROUTE_DIAGNOSTIC")
    if mission["classification"]=="TOOL_FAILURE":result.update(chain_status="BLOCKED_EVIDENCE",task_status="UNKNOWN")
    elif mission["validity_status"]!="PASS":result.update(chain_status="BLOCKED_INVALID_MODEL",task_status="UNKNOWN")
    if result["runtime_parameter_readback"].get("status")!="PASS":result.update(chain_status="BLOCKED_PARAMETER_EVIDENCE",task_status="UNKNOWN")
    for item in spec["checks"]:
        value=samples["series"][item["output"]][-1]
        passed=value>=item["threshold"] if item["kind"]=="final_ge" else value<=item["threshold"]
        result["observations"].append(dict(output=samples["mapping"][item["output"]]["resolved"],value=value,criterion=item,status="PASS" if passed else "FAIL"))
    result["signal_check_status"]="FAIL" if any(x["status"]=="FAIL" for x in result["observations"]) else "PASS"
    if result["chain_status"]=="PASS" and result["signal_check_status"]=="FAIL":
        result.update(task_status="FAIL",overall_summary="TASK_CHECK_FAILED")
    result["vehicle_task_status"]=result["task_status"]
    return result
