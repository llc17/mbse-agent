"""Independent analytical witnesses for the C2 prescribed contact probes."""

import csv
import math
from pathlib import Path
import tempfile
import unittest

from week12.src.integration import contact_probe_verify as probe


def _analytic_row(ident: str, time_s: float, feature_override=None):
    axis, speed = ("x", 0.4) if ident == "T01" else (
        ("x", -0.4) if ident == "T02" else
        (("z", 0.4) if ident == "T03" else ("z", -0.4))
    )
    displacement = speed * (time_s - 0.005)
    if axis == "x":
        x, z, vx, vz = displacement, 0.1243687729613299, speed, 0.0
    else:
        x, z, vx, vz = -0.1243687729613299, displacement, 0.0, speed
    omega = speed / 0.127
    if x > 0:
        feature, nx, nz, distance = 103, 0.0, 1.0, z
    elif z < 0:
        feature, nx, nz, distance = 102, -1.0, 0.0, -x
    else:
        feature = 203
        distance = math.hypot(x, z)
        nx, nz = x / distance, z / distance
    delta = 0.127 - distance
    vn = vx * nx + vz * nz
    slip = vx * nz - vz * nx - 0.127 * omega
    elastic_force = 100000.0 * delta
    damping = max(-elastic_force, min(elastic_force, -200.0 * vn))
    normal = elastic_force + damping
    tangent = -0.85 * normal * math.tanh(slip / 0.001)
    return {
        "time": time_s, "x": x, "z": z, "vx": vx, "vz": vz,
        "omega": omega, "Fx": normal * nx + tangent * nz,
        "Fz": normal * nz - tangent * nx, "normal": normal,
        "penetration": delta, "slip": slip,
        "elasticEnergy": 0.5 * 100000.0 * delta * delta,
        "normalLoss": damping * (-vn), "frictionLoss": -tangent * slip,
        "featureId": feature if feature_override is None else feature_override,
        "contactCount": 1,
    }


def _good_rows(ident: str):
    rows = []
    for index in range(1001):
        time_s = index * 1e-5
        rows.append(_analytic_row(ident, time_s))
        if index == 500:
            adjacent = 103 if ident in ("T01", "T02") else 102
            rows.append(_analytic_row(ident, time_s, adjacent))
    return rows


def _write(path: Path, rows, columns=probe.COLUMNS):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row[name] for name in columns})


class CContactProbeTests(unittest.TestCase):
    def test_independent_parser_oracle_positive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for ident in ("T01", "T02", "T03", "T04"):
                base = root / (ident + "-base.csv")
                refined = root / (ident + "-refined.csv")
                _write(base, _good_rows(ident))
                _write(refined, _good_rows(ident))
                report = probe.verify(ident, base)
                self.assertEqual(report["status"], "PASS", report)
                self.assertEqual(report["metrics"]["duplicate_time_rows"], 1)
                self.assertEqual(probe.compare(ident, base, refined)["status"], "PASS")

    def test_event_zero_contact_negative(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event-zero.csv"
            rows = _good_rows("T01")
            event = next(row for row in rows if row["time"] == 0.005)
            for name in ("Fx", "Fz", "normal", "penetration", "elasticEnergy"):
                event[name] = 0.0
            event["featureId"] = 0
            event["contactCount"] = 0
            _write(path, rows)
            self.assertEqual(probe.verify("T01", path)["status"], "FAIL")

    def test_force_and_double_count_negative(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            force_rows = _good_rows("T02")
            force_rows[200]["Fx"] += 0.2
            force_path = root / "force.csv"
            _write(force_path, force_rows)
            self.assertEqual(probe.verify("T02", force_path)["status"], "FAIL")
            count_rows = _good_rows("T03")
            count_rows[500]["contactCount"] = 2
            count_path = root / "count.csv"
            _write(count_path, count_rows)
            self.assertEqual(probe.verify("T03", count_path)["status"], "FAIL")

    def test_missing_column_time_and_nonfinite_numeric_negative(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = _good_rows("T04")
            missing_column = root / "missing-column.csv"
            _write(missing_column, rows, tuple(c for c in probe.COLUMNS if c != "slip"))
            missing_time = root / "missing-time.csv"
            _write(missing_time, [
                row for row in rows if abs(row["time"] - 0.006) > 1e-12
            ])
            nonfinite = root / "nonfinite.csv"
            bad_rows = _good_rows("T04")
            bad_rows[10]["x"] = float("nan")
            _write(nonfinite, bad_rows)
            for path in (missing_column, missing_time, nonfinite):
                with self.subTest(path=path.name):
                    self.assertEqual(probe.verify("T04", path)["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
