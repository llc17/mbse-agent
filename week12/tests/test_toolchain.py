from __future__ import annotations

from pathlib import Path
import unittest
import uuid

from week12.src.toolchain import check_sysml, execute_model


ROOT = Path(__file__).resolve().parents[1]


class ToolchainIntegrationTests(unittest.TestCase):
    def _out(self, name: str) -> Path:
        return ROOT / "results" / ("toolchain-" + name + "-" + uuid.uuid4().hex[:8])

    def test_simple_ode_requires_real_csv_evidence(self) -> None:
        source = """model SpringMass
  parameter Real m = 1;
  parameter Real c = 0.1;
  parameter Real k = 4;
  Real x(start=1);
  Real v(start=0);
equation
  der(x) = v;
  m * der(v) + c * v + k * x = 0;
end SpringMass;
"""
        result = execute_model(source, "SpringMass", self._out("ode"), 2.0, 200)
        self.assertEqual(result["compile"]["status"], "PASS", result)
        self.assertEqual(result["simulation"]["status"], "PASS", result)
        self.assertEqual(result["simulation"]["observed_stop"], 2.0)
        self.assertEqual(result["simulation"]["physical_conclusion"], "not_made")

    def test_underdetermined_model_fails_compile(self) -> None:
        source = """model Underdetermined
  Real x;
  Real y;
equation
  x = y;
end Underdetermined;
"""
        result = execute_model(source, "Underdetermined", self._out("under"), 1.0, 100)
        self.assertEqual(result["compile"]["status"], "FAIL", result)
        self.assertEqual(result["simulation"]["status"], "UNVERIFIED", result)

    def test_sysml_status_is_enum_value_and_evidence_is_serializable(self) -> None:
        result = check_sysml("package P { part def A; part a : A; }")
        self.assertIn(result["status"], {"PASS", "FAIL", "ERROR", "UNVERIFIED"})
        self.assertIsInstance(result["issues"], list)


if __name__ == "__main__":
    unittest.main()
