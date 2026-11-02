"""Offline evidence tests. No HTTP, OMC, simulator or real process is launched."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET
import tempfile
import unittest
from unittest.mock import patch

from week13.src import case_asym2_execution as execution
from week13.src.case_asym2_family import effective_source, modelica_parameters, PLAN_PARAMETERS


def spec(mass=85., source_mass=None):
    source=effective_source()
    if source_mass is None:source_mass=mass
    source=source.replace("parameter Real m=85.0;",f"parameter Real m={source_mass};")
    values=modelica_parameters(source)[0]
    return dict(artifact=dict(modelica_source=source,model_name=execution.MODEL),
        design=dict(parameters=[dict(name=k,value=mass if k=="m" else values[k]) for k in PLAN_PARAMETERS]),
        outputs={"position":"x"},checks=[])


def xml_file(path,values,*,calculated=()):
    root=ET.Element("fmiModelDescription")
    ET.SubElement(root,"DefaultExperiment",startTime="0",stopTime="300",stepSize="0.01",tolerance="1e-5",solver="dassl")
    variables=ET.SubElement(root,"ModelVariables")
    for name,value in values.items():
        node=ET.SubElement(variables,"ScalarVariable",name=name,
            causality="calculatedParameter" if name in calculated else "parameter")
        ET.SubElement(node,"Real",**({} if name in calculated else dict(start=str(value))))
    ET.ElementTree(root).write(path,encoding="utf8")


def test_mass_expected_is_confirmed_metadata_never_actual_source():
    candidate=spec(90,source_mass=85)
    expected=execution.confirmed_expectations(candidate)
    assert expected["m"]==90
    assert execution._source_values(candidate["artifact"]["modelica_source"])["m"]==85
    assert execution._mismatches(expected,execution._source_values(candidate["artifact"]["modelica_source"]))["m"]==dict(expected=90.,actual=85.)


def test_xml_requires_direct_mass_but_calculated_no_start_is_allowed(tmp_path):
    expected=execution.confirmed_expectations(spec(90))
    path=tmp_path/"init.xml"
    xml_file(path,expected,calculated=("x_end_up","L_nose_CoM"))
    assert execution._compiled_readback(path,expected)["status"]=="PASS"
    actual=dict(expected,m=85.)
    xml_file(path,actual)
    result=execution._compiled_readback(path,expected)
    assert result["status"]=="FAIL"
    assert result["mismatches"]["m"]["expected"]==90.


def owned_run(tmp_path):
    candidate=spec(90)
    expected=execution.confirmed_expectations(candidate)
    launches=[]
    def launch(command,cwd,kind,cap):
        launches.append(dict(command=command,kind=kind,cap=cap))
        if kind=="compile":
            exe=cwd/("model.exe" if execution.os.name=="nt" else "model")
            exe.write_bytes(b"TEST_ONLY_NOT_EXECUTABLE")
            xml_file(cwd/"model_init.xml",expected,calculated=("x_end_up","x_end_down","x_start_down","L_nose_CoM"))
            stdout=("__V11_CHECK_MODEL__=Check of AgentAsym2Build completed successfully.\n"
                    "__V11_BUILD_EXE__="+str(exe)+"\n")
        else:
            stdout="time=300,"+",".join(k+"="+str(v) for k,v in expected.items())+"\n"
            (cwd/"model_res.csv").write_text("time,x\n0,0\n300,1\n",encoding="utf8")
        (cwd/(kind+".stdout.log")).write_text(stdout,encoding="utf8")
        (cwd/(kind+".stderr.log")).write_bytes(b"")
        record=dict(command=command,cwd=str(cwd),timeout_s=cap,attempt_id=kind+"_MOCK",task_id="test_task",
                    pid=12345,returncode=0,timed_out=False,uncertain=False,cleanup_confirmed=True,
                    launch_error=None,elapsed_s=.001,stdout=execution.binding(cwd/(kind+".stdout.log")),
                    stderr=execution.binding(cwd/(kind+".stderr.log")))
        execution.save(cwd/(kind+".command.json"),record)
        return record
    out=tmp_path/"owned"
    with patch.object(execution.paths,"inside_write",lambda path:Path(path).resolve()),patch.object(execution,"_launch",launch):
        result=execution.execute_asym2_model(candidate["artifact"]["modelica_source"],execution.MODEL,out,300,30000,
                                             confirmed_spec=candidate)
    return SimpleNamespace(path=out,result=result,launches=launches,candidate=candidate)


def test_normal_runner_binds_solver_arrays_and_independent_expectations(owned_run):
    assert owned_run.result["compile"]["status"]=="PASS"
    assert owned_run.result["simulation"]["status"]=="PASS"
    assert owned_run.result["runtime_parameter_readback"]["expected"]["m"]==90.
    assert owned_run.result["runtime_parameter_readback"]["status"]=="PASS"
    assert [(x["kind"],x["cap"]) for x in owned_run.launches]==[("compile",180),("simulation",300)]
    flags=owned_run.launches[-1]["command"]
    assert "-s=dassl" in flags and "-maxStepSize=0.002" in flags
    assert "-output="+",".join(execution.PARAMETERS) in flags
    assert "wheel_J[3]" in flags[-1]
    _,actual,bound=execution.verify_run_evidence(owned_run.path)
    assert bound["status"]=="PASS",bound
    assert actual["m"]==90.


def test_source_only_execution_and_source_expected_mismatch_are_rejected(tmp_path):
    with unittest.TestCase().assertRaisesRegex(ValueError,"confirmed full"):
        execution.execute_asym2_model(effective_source(),execution.MODEL,tmp_path/"none",300,30000)
    candidate=spec(90,source_mass=85)
    with unittest.TestCase().assertRaisesRegex(ValueError,"confirmed/source parameter mismatch"):
        execution.execute_asym2_model(candidate["artifact"]["modelica_source"],execution.MODEL,tmp_path/"bad",300,30000,confirmed_spec=candidate)


def test_bound_stdout_change_is_rejected(owned_run):
    path=owned_run.path/"simulation.stdout.log"
    path.write_text(path.read_text(encoding="utf8").replace("m=90.0,","m=85.0,"),encoding="utf8")
    with unittest.TestCase().assertRaisesRegex(ValueError,"output binding changed"):
        execution.verify_run_evidence(owned_run.path)


def test_independent_evidence_rejects_rebound_wrong_execution(owned_run,mutation):
    root=owned_run.path
    run=json.loads((root/"run.json").read_text(encoding="utf8"))
    build=json.loads((root/"build.json").read_text(encoding="utf8"))
    if mutation=="exe":
        receipt=json.loads((root/"simulation.command.json").read_text(encoding="utf8"))
        receipt["command"][0]=str(root/"another.exe")
        execution.save(root/"simulation.command.json",receipt)
        run.update(process=receipt,process_receipt=execution.binding(root/"simulation.command.json"))
    elif mutation=="compile_failure":
        receipt=json.loads((root/"compile.command.json").read_text(encoding="utf8"));receipt["returncode"]=1
        execution.save(root/"compile.command.json",receipt)
        build.update(process=receipt,process_receipt=execution.binding(root/"compile.command.json"))
    else:
        (root/"wrapper.mo").write_text("model Evil end Evil;",encoding="utf8")
        build["wrapper"]=execution.binding(root/"wrapper.mo");run["wrapper"]=build["wrapper"]
    execution.save(root/"build.json",build);run["build_manifest"]=execution.binding(root/"build.json")
    execution.save(root/"run.json",run)
    _,_,bound=execution.verify_run_evidence(root)
    assert bound["status"]=="FAIL"


def test_classification_preserves_valid_incomplete_and_stall(tmp_path):
    from week13.src.asym2 import mixed_check
    path=tmp_path/"result.csv";path.write_text("time,x\n0,0\n300,1\n",encoding="utf8")
    for classification,wanted in (("PASS","PASS"),("PHYSICAL_STALL","FAIL"),("INCOMPLETE","UNKNOWN")):
        report=dict(classification=classification,validity=dict(status="PASS"),evidence=dict(status="PASS"),
            tool_process=dict(status="PASS"),csv=execution.binding(path),route={},stage_report={})
        with patch.object(mixed_check,"check",lambda *args,**kwargs:copy.deepcopy(report)):
            mission=execution.assess_asym2(spec(),path)
            assert mission["task_status"]==wanted
            assert mission["validity_status"]=="PASS"
            tool=dict(compile=dict(status="PASS"),simulation=dict(status="PASS"),runtime_parameter_readback=dict(status="PASS"))
            result=execution.summarize_asym2_execution(spec(),tool,path)
            assert result["chain_status"]=="PASS",result
            assert result["vehicle_task_status"]==wanted
    assert sorted(x.name for x in tmp_path.iterdir())==["result.csv"]  # read-only reopen never writes


def test_frozen_thresholds_are_unchanged():
    from week13.src.asym2 import physics_check
    acceptance=json.loads((Path(physics_check.__file__).parent/"acceptance.json").read_text(encoding="utf8"))["acceptance_before_vehicle_run"]
    assert acceptance["maximum_contact_compression_over_radius"]==.2
    assert acceptance["normalized_energy_balance_residual_limit"]==.01
    assert acceptance["center_inside_solid_tolerance_m"]==1e-7
    assert acceptance["complete_exit_margin_m"]==.5


def test_launch_reserves_and_finishes_owned_process_once(tmp_path):
    events=[]
    process=SimpleNamespace(pid=12345,communicate=lambda **kw:(b"result",b""),poll=lambda:0)
    with patch.object(execution.paths,"require_time",lambda:None), \
         patch.object(execution.paths,"inside_write",lambda p:Path(p)), \
         patch.object(execution.paths,"remaining",lambda:50.), \
         patch.object(execution.paths,"authorization",lambda:{"timeouts_s":{"simulation":300}}), \
         patch.object(execution.paths,"process_attempt",lambda kind,detail:events.append(("reserve",kind,detail)) or "attempt"), \
         patch.object(execution.paths,"mark_process_launched",lambda attempt,pid:events.append(("launch",attempt,pid))), \
         patch.object(execution.paths,"finish_process",lambda attempt,result:events.append(("finish",attempt,result))), \
         patch.object(execution,"emit_progress",lambda *a,**kw:None), \
         patch.object(execution.subprocess,"Popen",lambda *a,**kw:process), \
         patch.dict(execution.os.environ,{"V12_CASE_TASK_ID":"test_owned_task"}):
        result=execution._launch(["TEST_ONLY_MOCK"],tmp_path,"simulation",300)
    assert [x[0] for x in events]==["reserve","launch","finish"]
    assert result["timeout_s"]==50.
    assert result["task_id"]=="test_owned_task" and result["pid"]==12345
    assert execution._process_ok(result)


class Asym2ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)

    def test_confirmed_mass(self):test_mass_expected_is_confirmed_metadata_never_actual_source()
    def test_compiled_mass(self):test_xml_requires_direct_mass_but_calculated_no_start_is_allowed(self.path)
    def test_runner_binding(self):test_normal_runner_binds_solver_arrays_and_independent_expectations(owned_run(self.path))
    def test_requires_confirmation(self):test_source_only_execution_and_source_expected_mismatch_are_rejected(self.path)
    def test_stdout_tamper(self):test_bound_stdout_change_is_rejected(owned_run(self.path))
    def test_executable_tamper(self):test_independent_evidence_rejects_rebound_wrong_execution(owned_run(self.path),"exe")
    def test_failed_compile_tamper(self):test_independent_evidence_rejects_rebound_wrong_execution(owned_run(self.path),"compile_failure")
    def test_wrapper_tamper(self):test_independent_evidence_rejects_rebound_wrong_execution(owned_run(self.path),"wrapper")
    def test_valid_outcome_mapping(self):test_classification_preserves_valid_incomplete_and_stall(self.path)
    def test_frozen_thresholds(self):test_frozen_thresholds_are_unchanged()
    def test_owned_launch_accounting(self):test_launch_reserves_and_finishes_owned_process_once(self.path)


if __name__=="__main__":unittest.main()
