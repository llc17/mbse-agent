"""Software-only fixtures: no test fixture is actual new simulation evidence."""
from __future__ import annotations
from copy import deepcopy
import math
from pathlib import Path
import tempfile
import unittest
from week12.src.car_domain import reference_profile as profile
from week12.src.car_domain.reference_generator import generate_reference, render_model
from week12.src.car_domain.reference_verify import (
    _algebra, _replay, _crossing, ReferenceUnknown, check_reference_csv,
)


def reference_spec():
    facts={"total_mass":"总质量85kg","payload_mass":"设备载荷20kg",
           "step_height":"台阶高度0.15m","step_depth":"台阶深度0.28m",
           "continuous_steps":"连续级数20级","passage_task":profile.TASK_NAME}
    permission="复用用户原成功案例参考模板"
    text="；".join(facts.values())+"；"+permission
    return {"schema_version":profile.SPEC_SCHEMA,"profile":profile.PROFILE,
            "reference_ref":{"path":profile.SOURCE_PATH,"sha256":profile.SOURCE_SHA},
            "parameters":profile.parameter_values(),"sources":[{"id":"user","text":text}],
            "requirements":[{"name":name,"value":value,"unit":unit,
                             "source_id":"user","source_text":facts[name],
                             "origin":"user","classification":"hard"}
                            for name,(value,unit) in profile.CORE.items()],
            "design_permission":{"mode":"REUSE_USER_REFERENCE_TEMPLATE",
                                 "source_id":"user","source_text":permission},
            "constraints":[],"preferences":[],"ideas":[],"assumptions":[]}


class ReferenceInputTests(unittest.TestCase):
    def test_chinese_adjacent_values_and_literal_task(self):
        result=profile.validate_reference_spec(reference_spec())
        self.assertTrue(result["ready"],result)
        self.assertEqual(len(profile.parameter_values()),20)
        self.assertEqual(len(profile.parameter_descriptors()),25)

    def test_changed_parameters_or_source_and_additional_fields_rejected(self):
        for field in ("parameter","source","extra","constraint"):
            spec=reference_spec()
            if field=="parameter": spec["parameters"]["m"]=84
            elif field=="source": spec["reference_ref"]["sha256"]="0"*64
            elif field=="extra": spec["free_model"]="model arbitrary"
            else: spec["constraints"]=[{"text":"new hard task"}]
            self.assertFalse(profile.validate_reference_spec(spec)["ready"],field)

    def test_source_unit_mismatch_and_negative_rejected(self):
        for snippet in ("台阶高度0.15mm","台阶高度-0.15m","台阶高度0.15kg"):
            spec=reference_spec()
            spec["sources"][0]["text"]=spec["sources"][0]["text"].replace("台阶高度0.15m",snippet)
            next(r for r in spec["requirements"] if r["name"]=="step_height")["source_text"]=snippet
            self.assertFalse(profile.validate_reference_spec(spec)["ready"],snippet)

    def test_conflicting_and_forged_snippet_rejected(self):
        spec=reference_spec()
        spec["sources"].append({"id":"second","text":"设备载荷25kg"})
        self.assertFalse(profile.validate_reference_spec(spec)["ready"])
        spec=reference_spec()
        spec["requirements"][0]["source_text"]="not present"
        self.assertFalse(profile.validate_reference_spec(spec)["ready"])

    def test_permission_cannot_delegate_a_new_vehicle(self):
        spec=reference_spec()
        spec["design_permission"]["source_text"]="Agent决定任何方式"
        spec["sources"][0]["text"]+="；Agent决定任何方式"
        self.assertFalse(profile.validate_reference_spec(spec)["ready"])

    def test_integer_boolean_and_nan_rejected(self):
        for value in (True,20.0,float("nan")):
            spec=reference_spec()
            spec["parameters"]["n_steps"]=value
            self.assertFalse(profile.validate_reference_spec(spec)["ready"])


class ReferenceRenderingTests(unittest.TestCase):
    def test_exact_original_bytes_and_actual_parameter_probe(self):
        values=profile.parameter_values()
        rendered=render_model(values)
        self.assertEqual(rendered,profile.source_bytes())
        self.assertEqual(profile.sha256_bytes(rendered),profile.SOURCE_SHA)
        for slot,value,line in (("m",86.0,b"parameter Real m = 86.0;"),
                                ("stepH",.16,b"parameter Real stepH = 0.16;")):
            variant=dict(values)
            variant[slot]=value
            probe=render_model(variant)
            self.assertNotEqual(probe,rendered)
            self.assertIn(line,probe)
            self.assertEqual(probe.split(b"equation",1)[1],rendered.split(b"equation",1)[1])
        self.assertIn(b"x_end_up = x_start_up + n_steps * stepD;",rendered)

    def test_generator_readback_and_existing_destination_rejected(self):
        with tempfile.TemporaryDirectory(prefix="software-reference-",dir=profile.WEEK12_ROOT/"results") as temp:
            output=Path(temp)/"new"
            manifest=generate_reference(reference_spec(),output)
            self.assertEqual(Path(manifest["model_path"]).read_bytes(),profile.source_bytes())
            self.assertEqual(manifest["sysml_syntax_status"],"NOT_RUN")
            self.assertEqual(manifest["vehicle_task_status"],"UNKNOWN")
            with self.assertRaises(ValueError):
                generate_reference(reference_spec(),output)

    def test_unapproved_variant_and_outside_destination_rejected(self):
        spec=reference_spec()
        spec["parameters"]["stepH"]=.16
        with tempfile.TemporaryDirectory(prefix="software-reference-",dir=profile.WEEK12_ROOT/"results") as temp:
            with self.assertRaises(ValueError):
                generate_reference(spec,Path(temp)/"new")
        with self.assertRaises(ValueError):
            generate_reference(reference_spec(),profile.REPO_ROOT/"not-reference-output")


class IndependentCheckerSoftwareTests(unittest.TestCase):
    def test_known_initial_algebra_fixture(self):
        # Hand-written initial geometry/forces, independent of generator implementation.
        row={"time":0.,"x":0.,"z":.137,"v":0.,"vz":0.,"psi":0.,"omega_psi":0.,
             "x_f":.22,"z_f":.137,"x_r":-.23,"z_r":.137,
             "x_n":.54,"z_n":.200,"h_n":0.,"h_f":0.,"h_r":0.,
             "Fz_n":0.,"Fz_f":0.,"Fz_r":0.,"Fx_n":0.,"Fx_f":0.,"Fx_r":0.,
             "power_factor":1.,"target_speed":.4,"nose_on_ground":0.}
        p=profile.derived_values(profile.parameter_values())
        self.assertEqual(_algebra([row],p)["status"],"PASS_IN_REFERENCE_SCOPE")
        bad=dict(row,Fx_f=1.)
        self.assertEqual(_algebra([bad],p)["status"],"FAIL_IN_REFERENCE_SCOPE")

    def test_full_grid_regression_and_unknown_duplicate_event(self):
        names=("x","z","x_n","x_f","x_r","v","vz","psi")
        rows=[dict(time=i*.05,**{name:0. for name in names}) for i in range(2401)]
        self.assertEqual(_replay(rows,rows)["status"],"PASS")
        changed=deepcopy(rows)
        changed[10]["x"]=.006
        self.assertEqual(_replay(changed,rows)["status"],"FAIL_IN_REFERENCE_SCOPE")
        duplicate=deepcopy(rows)
        duplicate.insert(11,dict(duplicate[10],x=.01))
        with self.assertRaises(ReferenceUnknown):
            _replay(duplicate,rows)
        invariant=deepcopy(rows)
        invariant.insert(11,dict(invariant[10]))
        self.assertEqual(_replay(invariant,rows)["status"],"PASS")
        with self.assertRaises(ReferenceUnknown):
            _replay(rows[1:],rows)

    def test_missing_new_dynamic_columns_unknown_without_vehicle_pass(self):
        with tempfile.TemporaryDirectory(prefix="software-reference-",dir=profile.WEEK12_ROOT/"results") as temp:
            output=Path(temp)/"new"
            manifest=generate_reference(reference_spec(),output)
            csv_path=output/"SOFTWARE_ONLY_NOT_SIMULATION.csv"
            csv_path.write_text("time,x\n0,0\n120,20\n",encoding="utf-8")
            report=check_reference_csv(csv_path,reference_spec(),manifest["model_path"])
            self.assertEqual(report["status"],"UNKNOWN")
            self.assertEqual(report["vehicle_task_status"],"UNKNOWN")
            self.assertEqual(report["numeric_convergence"],"NOT_VERIFIED")

    def test_tampered_generated_source_refused(self):
        with tempfile.TemporaryDirectory(prefix="software-reference-",dir=profile.WEEK12_ROOT/"results") as temp:
            output=Path(temp)/"new"
            manifest=generate_reference(reference_spec(),output)
            model=Path(manifest["model_path"])
            model.write_bytes(model.read_bytes().replace(b"m = 85.0;",b"m = 84.0;"))
            report=check_reference_csv(output/"absent.csv",reference_spec(),model)
            self.assertEqual(report["status"],"FAIL_IN_REFERENCE_SCOPE")
            self.assertEqual(report["vehicle_task_status"],"UNKNOWN")

    def test_crossing_requires_actual_unambiguous_forward_domain(self):
        rows=[{"time":0.,"x_f":0.,"psi":0.},{"time":1.,"x_f":2.,"psi":-.1}]
        self.assertEqual(_crossing(rows,"x_f",1.)["time"],.5)
        with self.assertRaises(ReferenceUnknown):
            _crossing(rows,"x_f",3.)


if __name__ == "__main__":
    unittest.main()
