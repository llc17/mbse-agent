from __future__ import annotations

from copy import deepcopy
import json
import re
from pathlib import Path
import unittest

from week12.src.integration.model_generator import (
    B_SOURCE_SHA256, DESIGN_DIGEST, MODEL_CLASS, PHYSICAL_DIGEST,
    SPECIFICATION_DIGEST, generate,
)


ROOT = Path(__file__).resolve().parents[1]
PROPOSAL = ROOT / "results/s3-c-user-vehicle-execution-20260924-01/timing-amendment-draft/proposal.json"
CONTRACT = PROPOSAL.with_name('verification-contract.json')


class CModelGeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = json.loads(PROPOSAL.read_text(encoding="utf-8"))
        cls.contract = json.loads(CONTRACT.read_text(encoding="utf-8"))

    def test_vehicle_structure_mass_reaction_initialization_and_source_consistency(self):
        result = generate(self.bundle, "fixed_nose_full_mission")
        source, trace = result["modelica"], result["trace"]
        self.assertEqual(result["model_class"], MODEL_CLASS)
        self.assertIn("MB.Joints.Planar root(n={0,-1,0}, n_x={1,0,0}", source)
        for name in ("s_x", "s_y", "phi", "v_x", "v_y", "w"):
            self.assertRegex(source, rf"{name}\(start=[^,\)]+, fixed=true\)")
        self.assertAlmostEqual(float(re.search(r's_y\(start=([^,]+)',source).group(1)),.215)
        self.assertNotIn("global_vertical_lift", source)
        self.assertEqual(sum(c["count"] * c["mass"]["value"] for c in [
            *self.bundle["design"]["bodies"], *self.bundle["design"]["wheels"],
            *self.bundle["design"]["drives"], self.bundle["design"]["power_supply"],
        ]), 85)
        for ident in ("chassis", "payload", "controller", "rear_drive", "front_drive", "nose_drive", "battery"):
            self.assertIn(f"V11PhysicalB1.RigidBody2D body_{ident}", source)
        for ident in ("rear", "front", "nose"):
            self.assertIn(f"WheelPairWithTerrainPort wheel_{ident}", source)
            self.assertIn("extends V11PhysicalB1.WheelPair;", source)
            self.assertIn("connect(wheelBody.frame, terrainTorqueFrame);", source)
            self.assertIn(f"connect(rollingWorld_{ident}.frame_b, wheel_{ident}.terrainTorqueFrame);", source)
            self.assertIn(f"MB.Forces.WorldTorque rollingWorld_{ident}", source)
            self.assertIn(f"rollingLoss_{ident} = -rollingTorque_{ident}*wheel_{ident}.omega", source)
            self.assertIn(f"connect(drive_{ident}_drive.support, wheel_{ident}.support)", source)
        self.assertIn("if time < 4.0 then 0 else", source)
        self.assertEqual(self.contract['physical']['settled_window_s'],[3,4])
        for case in self.bundle['physical']['cases']:
            self.assertEqual(case['reference_speed_table_s_m_s'][1:3],[[4,0],[6,.4]])
        self.assertIn("max(abs(drive_rear_drive.omegaRelative),1.0)", source)
        self.assertIn("refSpeed/0.127 - wheel_rear.omega", source)
        self.assertIn("busPower = drive_rear_drive.dcPower + drive_front_drive.dcPower + drive_nose_drive.dcPower + auxPower", source)
        self.assertIn("der(busEnergy) = -busPower", source)
        self.assertNotIn("energyAvailable +", source)
        self.assertEqual(trace["frozen"]["specification_digest"], SPECIFICATION_DIGEST)
        self.assertEqual(trace["frozen"]["design_digest"], DESIGN_DIGEST)
        self.assertEqual(trace["frozen"]["physical_digest"], PHYSICAL_DIGEST)
        self.assertEqual(trace["frozen"]["b_source_sha256"], B_SOURCE_SHA256)
        self.assertTrue(trace["one_solve"])
        self.assertEqual(set(trace["state_continuity"]["no_reset"]), set(self.contract["physical"]["serial_mission_rule"]["no_reset"]))
        self.assertIn("attribute loadedMass_kg : Real = 85.0", result["sysml"])
        self.assertIn("part rear : PhysicalComponent", result["sysml"])
        self.assertIn("connect rear to rear_drive", result["sysml"])
        self.assertTrue(result["variable_filter"].startswith("^(time|body_chassis_x|"))
        self.assertTrue(result["variable_filter"].endswith("|refSpeed)$"))
        self.assertNotIn("PASS", result["variable_filter"])

    def test_probes_all_cases_and_unsupported_inputs(self):
        expected = self.contract["case_order"]
        actual_cases = [case["id"] for case in self.bundle["physical"]["cases"]]
        self.assertEqual(expected[2:], actual_cases)
        for case_id in expected:
            with self.subTest(case_id=case_id):
                result = generate(self.bundle, case_id)
                self.assertEqual(set(result), {"modelica", "model_class", "sysml", "trace", "variable_filter"})
                self.assertEqual(result["model_class"], MODEL_CLASS)
                self.assertEqual(result["trace"]["case_id"], case_id)
                self.assertIn("probeOmega" if case_id.endswith("probe") else "chassisPitch", result["variable_filter"])
        supply = generate(self.bundle, "shared_supply_probe")["modelica"]
        self.assertIn("probeBusPower = probeDcPower + 5", supply)
        self.assertIn("probeBusEnergy(start=100, fixed=true)", supply)
        rolling = generate(self.bundle, "rolling_resistance_probe")["modelica"]
        self.assertIn("probeRollingTorque = -0.05*0.1*10*tanh(probeOmega/0.1)", rolling)
        self.assertIn("connect(terrainTorque.support, ground.flange)", rolling)
        changed = deepcopy(self.bundle)
        changed["design"]["joints"][0]["kind"] = "passive_revolute"
        with self.assertRaisesRegex(ValueError, "UNSUPPORTED: fixed-only"):
            generate(changed, "fixed_nose_flat_brake")
        with self.assertRaisesRegex(ValueError, "UNSUPPORTED: unknown C case"):
            generate(self.bundle, "not_a_case")


if __name__ == "__main__":
    unittest.main()
