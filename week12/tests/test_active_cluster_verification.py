"""Meaningful independent negative checks; root executes once under its ledger.

Fixtures exercise raw algebra/force/ownership/timing. They do not assert a
generator-written status, run OMC, import a paused backend, or claim task PASS.
"""
import csv
import copy
import tempfile
import json
import re
import unittest
from pathlib import Path

from week12.src.active_cluster.verify import (
    Accumulator, EvidenceMissing, Report, _contact_frame, _electrical_frame,
    _frames, _primitive, _progress_frame, _rollup, _shape_clearance,
    _validate_contract, compare_numerics, check_csv, _literal_bodies, _model_bodies, _close,
)


def _drive_fixture(tau=16.0, speed=-.5):
    drive = dict(id="test_carrier", kind="carrier", multiplicity=2,
                 physical_ids=["left_motor", "right_motor"], gear=100., eta=.8,
                 kt=.1, Ra=.2, continuous=100., peak=150.)
    keys = ["tau", "wrel", "power", "current", "voltage", "gear_loss",
            "copper_loss", "driver_loss", "dc"]
    drive["columns"] = {key: key for key in keys}
    p = tau*speed
    mt = tau/2/(100*.8) if p >= 0 else .8*tau/2/100
    current = mt/.1
    gear = (.25*p) if p >= 0 else .2*abs(p)
    copper = 2*current*current*.2
    motor = p+gear
    loss = (1/.95-1)*(abs(motor)+copper)
    dc = motor+copper+loss
    frame = dict(time=0., tau=tau, wrel=speed, power=p, current=current,
                 voltage=.2*current+.1*100*speed, gear_loss=gear,
                 copper_loss=copper, driver_loss=loss, dc=dc,
                 battery_power=80+max(0, dc), battery_energy=0.,
                 dump_power=max(0, -dc), dump_energy=0., shaft_power=p)
    total = {key: key for key in ["battery_power", "battery_energy", "dump_power",
                                  "dump_energy", "shaft_power"]}
    spec = {"control": {"DC_proxy": {"driver_efficiency": .95, "kt_ke": .1, "aux_W": 80}}}
    contract = {"drives": [drive], "columns": total}
    return spec, contract, frame


def _contact_fixture():
    wheel = dict(id="front_A", owner_index=11, multiplicity=2, radius=.14,
                 columns={key: key for key in ["x", "z", "vx", "vz",
                     "omega_world_y", "Fx", "Fz", "normal", "rolling_torque_world_y"]})
    keys = ["Fn", "Ft", "active", "penetration", "slip", "nx", "nz", "owner"]
    feature = dict(id="front_A_tread", owner_id="front_A", kind="face",
                   segment=[[-1., 0.], [1., 0.]], columns={key: "f_"+key for key in keys})
    spec = {"physics": {"proposed_contact_parameters": {
        "per_physical_tyre_k": 200000., "c": 400., "mu": .8, "vreg": .001,
        "rolling_coefficient": .02, "rolling_omega_regularization": .05}}}
    frame = dict(time=0., x=0., z=.139, vx=0., vz=0., omega_world_y=0.,
                 Fx=0., Fz=400., normal=400., rolling_torque_world_y=0.,
                 f_Fn=400., f_Ft=0., f_active=1., f_penetration=.001,
                 f_slip=0., f_nx=0., f_nz=1., f_owner=11.)
    return spec, {"wheels": [wheel]}, {"features": [feature]}, frame


class IndependentNegativeChecks(unittest.TestCase):
    def metric(self, state, code):
        return state.metrics[code]["ok"]

    def test_negative_shaft_power_uses_generating_eta(self):
        spec, contract, frame = _drive_fixture()
        state = Accumulator(Report("unit physical account"))
        _electrical_frame(state, frame, spec, contract, 0.)
        self.assertTrue(self.metric(state, "gear_loss_test_carrier"))
        # A motoring division on negative shaft work is physically wrong.
        frame["gear_loss"] = abs(frame["power"])*(1/.8-1)
        state = Accumulator(Report("negative gear branch"))
        _electrical_frame(state, frame, spec, contract, 0.)
        self.assertFalse(self.metric(state, "gear_loss_test_carrier"))

    def test_world_spin_is_not_relative_shaft_power(self):
        spec, contract, frame = _drive_fixture(tau=16., speed=.2)
        frame["power"] = frame["tau"]*.7  # parent world motion incorrectly added
        state = Accumulator(Report("shaft frame"))
        _electrical_frame(state, frame, spec, contract, 0.)
        self.assertFalse(self.metric(state, "relative_shaft_power_test_carrier"))

    def test_zero_speed_holding_has_nonzero_copper(self):
        spec, contract, frame = _drive_fixture(tau=120., speed=0.)
        frame["copper_loss"] = 0.
        state = Accumulator(Report("holding winding"))
        _electrical_frame(state, frame, spec, contract, 0.)
        self.assertFalse(self.metric(state, "copper_loss_test_carrier"))

    def test_same_pair_supply_cannot_be_added_twice(self):
        spec, contract, frame = _drive_fixture(tau=16., speed=.2)
        frame["battery_power"] = 80+2*max(frame["dc"], 0)
        state = Accumulator(Report("shared supply"))
        _electrical_frame(state, frame, spec, contract, 0.)
        self.assertFalse(self.metric(state, "shared_battery_positive_draw"))

    def test_missing_actual_feature_force_rejected(self):
        spec, contract, info, frame = _contact_fixture()
        del frame["f_Fn"]
        with self.assertRaises(KeyError):
            _contact_frame(Accumulator(Report("missing raw feature")), frame, spec, contract, info)

    def test_wrong_actual_feature_owner_is_failure(self):
        spec, contract, info, frame = _contact_fixture()
        frame["f_owner"] = 12.
        state = Accumulator(Report("actual owner"))
        _contact_frame(state, frame, spec, contract, info)
        self.assertFalse(self.metric(state, "actual_owner_front_A_tread"))

    def test_contact_force_cannot_be_prescribed_independently(self):
        spec, contract, info, frame = _contact_fixture()
        frame["f_Fn"] = 999.
        state = Accumulator(Report("actual spring force"))
        _contact_frame(state, frame, spec, contract, info)
        self.assertFalse(self.metric(state, "actual_Fn_front_A_tread"))

    def test_missing_csv_data_never_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"never_created.csv"
            result = check_csv(path, {}, {}, {"scenes": {}}, "three_up")
            self.assertNotEqual(result["status"], "PASS")
            with self.assertRaises(EvidenceMissing):
                list(_frames(path, {"time", "Fn"}))

    def test_nonfinite_feature_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"invalid.csv"
            path.write_text("time,Fn\n0,1\n0.02,nan\n", encoding="utf-8")
            with self.assertRaises(EvidenceMissing):
                list(_frames(path, {"time", "Fn"}))

    def test_same_time_event_rows_remain_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"event.csv"
            path.write_text("time,Fn\n0,0\n0.02,0\n0.02,40\n0.04,40\n", encoding="utf-8")
            rows = list(_frames(path, {"time", "Fn"}))
            self.assertEqual(len(rows), 4)
            self.assertEqual([r["Fn"] for r in rows[1:3]], [0., 40.])

    def test_wrong_physical_count_fails(self):
        report = Report("physical counts")
        contract = dict(bodies=[{"id": "chassis"}], drives=[{
            "id": "one", "physical_ids": ["one"], "multiplicity": 1}],
            wheels=[], definitions={"power": "tau*wrel"})
        _validate_contract(report, contract, {}, "three_up")
        self.assertEqual(report.result()["status"], "FAIL")

    def test_false_front_first_canned_flag_cannot_pass(self):
        # Actual rear wheel drops before front; an unrelated true status is ignored.
        wheels = [dict(id="front", radius=.14, columns={"x": "fx", "z": "fz"}),
                  dict(id="rear", radius=.14, columns={"x": "rx", "z": "rz"})]
        contract = {"wheels": wheels}
        info = dict(chassis_id="chassis", direction="down",
                    front_wheel_ids=["front"], rear_wheel_ids=["rear"], exit_edge_x=2.)
        poses = {"chassis": dict(x=0., vx=.1, comx=0., comvx=.1, phi=-.1)}
        state = Accumulator(Report("front first from raw pose"))
        _progress_frame(state, dict(time=0., fx=0., fz=.59, rx=-.5, rz=.59),
                        contract, info, poses, 0., .1, 0.)
        _progress_frame(state, dict(time=1., fx=.1, fz=.59, rx=-.4, rz=.57,
                                    canned_front_first=True),
                        contract, info, poses, .1, .1, 1.)
        self.assertFalse(self.metric(state, "front_main_wheel_first_forward_down"))

    def test_hardware_clearance_from_actual_pose_not_nominal_gap(self):
        shape = dict(kind="circle", center=[0., 0.], radius=.055)
        floor = [[-2., 0.], [2., 0.]]
        self.assertAlmostEqual(_shape_clearance(shape, (0., .059, 0.), floor), .004)
        self.assertLess(_shape_clearance(shape, (0., .05, 0.), floor), 0)

    def test_primitive_inertia_not_generator_intrinsic_answer(self):
        primitive = dict(kind="box", m=12., centre=[0., 0., 0.],
                         dimensions=[.6, .36, .12], intrinsic_diagonal=[999, 999, 999])
        mass, _, tensor = _primitive(primitive)
        self.assertEqual(mass, 12.)
        self.assertAlmostEqual(tensor[1][1], 12*(.6**2+.12**2)/12)

    def test_pair_tensor_uses_real_lateral_parallel_axis(self):
        parts = [dict(kind="cylinder_y", m=1., centre=[0., y, 0.],
                      dimensions=[.04, .04]) for y in (-.26, .26)]
        mass, com, tensor = _rollup(parts)
        self.assertEqual(mass, 2.)
        self.assertEqual(com, [0., 0., 0.])
        self.assertGreater(tensor[0][0], .13)
        self.assertAlmostEqual(tensor[1][1], .0016)


if __name__ == "__main__":
    unittest.main()


class ActualGeneratedBodyLiteralRegression(unittest.TestCase):
    def _actual(self):
        directory = Path(__file__).resolve().parents[1]/"results"/"s3-c-active-eightwheel-execution-20261009-01"/"generated-round-01"
        # Read preserved real generation, never regenerate or mutate its files.
        code = (directory/"package.mo").read_text(encoding="utf-8")
        manifest = json.loads((directory/"manifest.json").read_text(encoding="utf-8"))
        design = json.loads((directory/"design.json").read_text(encoding="utf-8"))
        sections = {m.group(1): m.group(2) for m in re.finditer(r"\bmodel\s+(\w+)\b(.*?)\bend\s+\1\s*;", code, re.S)}
        return manifest, design, sections

    def test_real_generated_seven_scene_body_literals_with_gravity(self):
        manifest, design, sections = self._actual()
        source = {b["id"]: b for b in _model_bodies(design)}
        for scene, name in manifest["model_class_by_scene"].items():
            actual = _literal_bodies(sections[name.rsplit(".",1)[-1]])
            expected = set(source) if scene != "one_twin_module_load_cycle" else {ident for ident in source if ident == "chassis" or ident.startswith("F")}
            self.assertEqual(set(actual), expected)
            for ident in expected:
                mass, com, tensor = _rollup(source[ident]["primitives"])
                if scene == "one_twin_module_load_cycle" and ident == "chassis":
                    mass /= 2
                    tensor = [[v/2 for v in row] for row in tensor]
                am, ac, ai, ag = actual[ident]
                self.assertTrue(_close(am,mass))
                self.assertTrue(all(_close(ac[i],com[i]) for i in range(3)))
                self.assertTrue(all(_close(ai[i][j],tensor[i][j]) for i in range(3) for j in range(3)))
                self.assertTrue(_close(ag,0.0 if scene == "active_joint_energy" else 9.81))

    def test_actual_generated_changed_mass_and_gravity_are_rejected(self):
        manifest, design, sections = self._actual()
        section = sections[manifest["model_class_by_scene"]["active_joint_energy"].rsplit(".",1)[-1]]
        original = _literal_bodies(section)["chassis"]
        changed = re.sub(r"(C\.Body\s+chassis\(m=)[^,]+",r"\g<1>60.8",section,count=1)
        self.assertFalse(_close(_literal_bodies(changed)["chassis"][0],original[0]))
        wrong_g = re.sub(r"(C\.Body\s+chassis\(m=[^,]+,g=)[^,]+",r"\g<1>9.81",section,count=1)
        self.assertFalse(_close(_literal_bodies(wrong_g)["chassis"][3],0.0))
