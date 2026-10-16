from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from week12.src.composition.compiler import ContractGenerator, compile_design
from week12.src.composition.examples import synthetic_receipt
from week12.src.composition.schema import (
    ContractError,
    canonical,
    design_digest,
    load_library,
    seal_design,
    validate_design,
)


ROOT = Path(__file__).resolve().parents[1]
RESULTS_ROOT = ROOT / "results"
CONFIG_ROOT = ROOT / "configs" / "composition"
# The evidence runner sets this to a fresh directory. Normal unit-test runs do
# not write durable evidence.
EVIDENCE_DIR = None


def example(topology: str) -> dict:
    """Missing delivered fixtures must fail rather than silently rebuilding them."""
    path = CONFIG_ROOT / f"{topology}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def diagnostic(design: dict) -> dict:
    result = validate_design(design)
    if not result:
        raise AssertionError("expected structural diagnostic")
    return result[0]


def write_evidence(topology: str, design: dict, compiled: dict) -> None:
    if EVIDENCE_DIR is None:
        return
    destination = Path(EVIDENCE_DIR) / topology
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "input.json").write_bytes(canonical(design))
    (destination / "compilation.json").write_bytes(canonical(compiled))


class CompositionContractTests(unittest.TestCase):
    def test_fixed_nose_preview_compiles_from_static_structure_fixture(self):
        design = example("fixed_nose")
        compiled = compile_design(design, purpose="structure_preview")
        self.assertEqual(validate_design(design), [])
        self.assertEqual(compiled["status"], "STRUCTURE_COMPILED")
        self.assertFalse(compiled["executable"])
        self.assertEqual(compiled["backend_status"], "UNSUPPORTED")
        self.assertIn("component:nose", compiled["trace_map"])
        self.assertEqual(compiled, compile_design(design, purpose="structure_preview"))
        ir = compiled["artifacts"]["structure.ir.json"]
        self.assertAlmostEqual(ir["mass_accounting"]["total_loaded_mass_kg"], 40.3)
        self.assertEqual(ir["mass_accounting"]["payload_kg"], 20)
        self.assertEqual(ir["kinematics"]["representative_planar_dof"], 6)
        self.assertEqual(len(compiled["trace_map"]), len(ir["components"]) + len(ir["connections"]))
        rear = next(term for term in ir["mass_accounting"]["terms"] if term["component_id"] == "rear")
        rear_drive = next(term for term in ir["drive_accounting"] if term["component_id"] == "rear_drive")
        self.assertEqual((rear["physical_count"], rear["mass_aggregate_kg"]), (2, 1.8))
        self.assertEqual((rear_drive["torque_pair_nm"], rear_drive["mechanical_power_pair_w"]), (36, 200))
        self.assertEqual(rear_drive["electrical_power_pair_at_rating_w"], 250)
        write_evidence("fixed_nose", design, compiled)

    def test_rocker_bogie_preview_has_distinct_connection_topology(self):
        fixed = compile_design(example("fixed_nose"), purpose="structure_preview")
        design = example("rocker_bogie")
        compiled = compile_design(design, purpose="structure_preview")
        self.assertEqual(validate_design(design), [])
        self.assertNotEqual(
            fixed["artifacts"]["structure.ir.json"]["topology_digest"],
            compiled["artifacts"]["structure.ir.json"]["topology_digest"],
        )
        self.assertIn("component:rocker", compiled["trace_map"])
        self.assertIn("connection:rocker_pivot_a", compiled["trace_map"])
        ir = compiled["artifacts"]["structure.ir.json"]
        self.assertAlmostEqual(ir["mass_accounting"]["total_loaded_mass_kg"], 45.5)
        self.assertEqual(ir["kinematics"]["representative_planar_dof"], 8)
        self.assertEqual(ir["library_digest"], fixed["artifacts"]["structure.ir.json"]["library_digest"])
        self.assertEqual(sum(c["count"] for c in ir["components"] if c["kind"] == "tire_pair"), 6)
        write_evidence("rocker_bogie", design, compiled)

    def test_strict_units_are_rejected_before_stale_digest(self):
        wrong_unit = example("fixed_nose")
        wrong_unit["wheels"][0]["radius"]["unit"] = "mm"
        self.assertEqual(diagnostic(wrong_unit)["path"], "rear.radius")

    def test_missing_mass_is_rejected_before_stale_digest(self):
        missing_mass = example("fixed_nose")
        del missing_mass["wheels"][0]["mass"]
        self.assertEqual(diagnostic(missing_mass)["path"], "wheels")

    def test_unknown_port_reference_is_rejected(self):
        unknown = example("fixed_nose")
        unknown["connections"][0]["from"] = "missing.port"
        self.assertEqual(diagnostic(unknown)["path"], unknown["connections"][0]["id"])

    def test_duplicate_component_id_is_rejected(self):
        duplicate = example("fixed_nose")
        duplicate["wheels"][0]["id"] = "chassis"
        self.assertEqual(diagnostic(duplicate)["path"], "chassis")

    def test_closed_loop_is_not_lowered(self):
        closed = example("fixed_nose")
        closed["bodies"][0]["ports"]["loop"] = deepcopy(closed["bodies"][0]["ports"]["payload"])
        closed["bodies"][1]["ports"]["loop"] = deepcopy(closed["bodies"][1]["ports"]["mount"])
        joint = deepcopy(closed["joints"][0])
        joint.update(id="loop_joint", kind="passive_revolute", dof=["ry"])
        closed["joints"].append(joint)
        closed["connections"].extend([
            {"id": "loop_joint_a", "from": "chassis.loop", "to": "loop_joint.a", "source": "test"},
            {"id": "loop_joint_b", "from": "payload.loop", "to": "loop_joint.b", "source": "test"},
        ])
        self.assertEqual(diagnostic(closed)["code"], "UNSUPPORTED")

    def test_illegal_joint_dof_is_not_lowered(self):
        illegal_dof = example("fixed_nose")
        illegal_dof["joints"][0]["dof"] = ["ry"]
        self.assertEqual(diagnostic(illegal_dof)["code"], "UNSUPPORTED")

    def test_same_version_library_content_change_is_rejected(self):
        library = load_library()
        library["components"]["tire_pair"]["physical_implementation"] = "unverified_replacement"
        with self.assertRaises(ContractError) as mismatch:
            compile_design(synthetic_receipt(example("fixed_nose")), library, purpose="contract_test")
        self.assertEqual(mismatch.exception.diagnostics[0]["code"], "UNSUPPORTED")

    def test_pending_and_synthetic_receipts_cannot_use_production_compilation(self):
        pending = example("fixed_nose")
        with self.assertRaises(ContractError) as pending_error:
            compile_design(pending)
        self.assertEqual(pending_error.exception.diagnostics[0]["code"], "UNCONFIRMED")
        synthetic = synthetic_receipt(example("fixed_nose"))
        with self.assertRaises(ContractError) as synthetic_error:
            compile_design(synthetic)
        self.assertEqual(synthetic_error.exception.diagnostics[0]["code"], "UNCONFIRMED")

    def test_seal_and_receipt_binding_invalidate_changed_design_content(self):
        sealed = seal_design(synthetic_receipt(example("fixed_nose")))
        self.assertFalse(sealed["confirmation"]["human_confirmed"])
        self.assertEqual(sealed["confirmation"]["origin"], "pending")
        changed = synthetic_receipt(example("fixed_nose"))
        changed["scenario"]["flat"]["speed"]["value"] = 0.5
        changed["design_digest"] = design_digest(changed)
        self.assertEqual(validate_design(changed), [])
        with self.assertRaises(ContractError) as stale:
            compile_design(changed, purpose="contract_test")
        self.assertEqual(stale.exception.diagnostics[0]["code"], "STALE")
        self.assertEqual(stale.exception.diagnostics[0]["path"], "confirmation.design_digest")

    def test_protocol_adapter_writes_only_nonexecutable_ir_and_executable_is_unsupported(self):
        design = synthetic_receipt(example("fixed_nose"))
        generator = ContractGenerator(test_only=True)
        with tempfile.TemporaryDirectory(dir=RESULTS_ROOT) as root:
            output = Path(root) / "generated"
            summary = generator.generate(design, "descent", output)
            self.assertFalse(summary["executable"])
            self.assertEqual(summary["backend_status"], "UNSUPPORTED")
            self.assertTrue((output / "structure.ir.json").is_file())
            self.assertFalse(any(path.suffix == ".mo" for path in output.iterdir()))
            executable = Path(root) / "executable"
            with self.assertRaises(ContractError) as unsupported:
                generator.generate_executable(design, "descent", executable)
            self.assertEqual(unsupported.exception.diagnostics[0]["code"], "UNSUPPORTED")
            self.assertFalse(executable.exists())

            with self.assertRaises(FileExistsError):
                generator.generate(design, "descent", output)
            outside = Path(tempfile.gettempdir()) / "composition-outside-output"
            with self.assertRaises(ContractError) as outside_error:
                generator.generate(design, "flat", outside)
            self.assertEqual(outside_error.exception.diagnostics[0]["path"], "directory")


if __name__ == "__main__":
    unittest.main()
