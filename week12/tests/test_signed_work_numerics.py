"""Small real CSV anti-masking fixtures; no model, solver or physics reruns."""
import csv
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from week12.src.active_cluster.verify import SIGNED_WORK_ADDENDUM, compare_numerics


class SignedWorkNumerics(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.contract = {"scenes": {"active_joint_energy": {"metadata": {}, "columns": {
            "shaft_energy": "o_shaft_energy", "battery_energy": "battery",
            "loss_energy": "loss"}}}}

    def write(self, name, rows):
        path = self.root/name
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["time", "o_shaft_energy", "position", "battery", "loss"])
            for row in rows:
                writer.writerow(list(row)+[0., 2764700., 100.])
        return path

    def summary(self, path, contract, scene):
        # Only legacy scene/event plumbing is mocked; new streaming grid and
        # full-cycle extrema consume the actual CSV, including invalid rows.
        with Path(path).open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        last = {k: float(v) for k, v in rows[-1].items()}
        columns = {"position_x": "position", "energy_shaft_energy": "o_shaft_energy",
                   "energy_battery_energy": "battery", "energy_loss_energy": "loss"}
        return last, {"task_done": .04}, columns, {"task_done"}

    def compare(self, a, b, addendum=SIGNED_WORK_ADDENDUM, contract=None):
        with patch("week12.src.active_cluster.verify._numeric_summary", side_effect=self.summary):
            return compare_numerics(a, b, contract or self.contract, "active_joint_energy",
                                    numerical_addendum=addendum)

    def check(self, report, code):
        return next(c for c in report["checks"] if c["code"] == code)["status"]

    def test_near_zero_cancellation_and_none_preserves_legacy_failure(self):
        a = self.write("base.csv", [(0, 0), (.02, 100), (.04, 0)])
        b = self.write("refined.csv", [(0, 0), (.01, 50), (.02, 100.1), (.03, 50), (.04, .001)])
        revised = self.compare(a, b)
        self.assertEqual(revised["status"], "PASS")
        self.assertEqual(revised["diagnostics"]["signed_work"]["common_grid_count"], 3)
        self.assertEqual(revised["diagnostics"]["signed_work"]["addendum"], SIGNED_WORK_ADDENDUM)
        legacy = self.compare(a, b, None)
        self.assertEqual(self.check(legacy, "final_energy_1percent_energy_shaft_energy"), "FAIL")
        self.assertNotIn("signed_work", legacy["diagnostics"])

    def test_large_intermediate_error_rejects_identical_final_net(self):
        a = self.write("base.csv", [(0, 0), (.02, 100), (.04, 0)])
        b = self.write("refined.csv", [(0, 0), (.02, 90), (.04, 0)])
        report = self.compare(a, b)
        self.assertEqual(self.check(report, "final_energy_1percent_energy_shaft_energy"), "PASS")
        self.assertEqual(self.check(report, "whole_curve_1percent_energy_shaft_energy"), "FAIL")

    def test_zero_pair_floor_and_spurious_nonzero(self):
        a = self.write("base.csv", [(0, 0), (.02, 0), (.04, 0)])
        b = self.write("zero.csv", [(0, 0), (.02, 0), (.04, 0)])
        report = self.compare(a, b)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["diagnostics"]["signed_work"]["scale_J"], 1e-8)
        tiny = self.write("tiny.csv", [(0, 0), (.02, 5e-11), (.04, 5e-11)])
        self.assertEqual(self.compare(a, tiny)["status"], "PASS")
        spurious = self.write("spurious.csv", [(0, 0), (.02, 1), (.04, 1)])
        self.assertEqual(self.compare(a, spurious)["status"], "FAIL")

    def test_starting_offsets_are_subtracted(self):
        a = self.write("base.csv", [(0, 300), (.02, 400), (.04, 300)])
        b = self.write("refined.csv", [(0, -200), (.02, -100), (.04, -200)])
        self.assertEqual(self.compare(a, b)["status"], "PASS")

    def test_actual_duplicate_timestamp_uses_last_row_including_endpoints(self):
        a = self.write("base.csv", [(0, 7), (0, 10), (.02, 90), (.02, 110), (.04, 999), (.04, 10)])
        b = self.write("refined.csv", [(0, 0), (.02, 100), (.04, 0)])
        report = self.compare(a, b)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["diagnostics"]["signed_work"]["base_final_net_J"], 0.)
        # Every raw row affects the scale, even a pre-event duplicate endpoint.
        self.assertEqual(report["diagnostics"]["signed_work"]["scale_J"], 989.)

    def test_missing_nonfinite_negative_or_unsorted_actual_evidence(self):
        a = self.write("base.csv", [(0, 0), (.02, 100), (.04, 0)])
        cases = [[(0, 0), (.04, 0)], [(0, 0), (.02, float("nan")), (.04, 0)],
                 [(0, 0), (.02, float("inf")), (.04, 0)],
                 [(-1e-10, 0), (.02, 100), (.04, 0)],
                 [(0, 0), (.03, 100), (.02, 100), (.04, 0)],
                 [(0, 0), (float("nan"), 100), (.04, 0)]]
        for i, rows in enumerate(cases):
            with self.subTest(rows=rows):
                b = self.write(str(i)+".csv", rows)
                self.assertEqual(self.compare(a, b)["status"], "REQUIRES_EVIDENCE")

    def test_exact_grid_row_has_priority_over_distinct_nearby_event(self):
        a = self.write("base.csv", [(0, 0), (.02, 100), (.04, 0)])
        b = self.write("refined.csv", [(0, 0), (.02, 100), (.020000001, 101), (.04, 0)])
        report = self.compare(a, b)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["diagnostics"]["signed_work"]["max_curve_error_J"], 0.)
        ambiguous = self.write("ambiguous.csv", [(0, 0), (.019999999, 100), (.020000001, 100), (.04, 0)])
        self.assertEqual(self.compare(a, ambiguous)["status"], "REQUIRES_EVIDENCE")

    def test_actual_solver_event_rows_after_one_second_are_not_grid_rows(self):
        a = self.write("base.csv", [(i*.02, 0) for i in range(50)]+[(1, 0), (1.000000000200015, 0), (1.000000000200015, 0), (1.02, 0)])
        b = self.write("refined.csv", [(i*.02, 0) for i in range(50)]+[(1, 0), (1.000000000020004, 0), (1.000000000020004, 0), (1.02, 0)])
        self.assertEqual(self.compare(a, b)["status"], "PASS")

    def test_timestamp_tolerance_bounded_without_interpolation(self):
        a = self.write("base.csv", [(0, 0), (.02, 100), (.04, 0)])
        inside = self.write("inside.csv", [(0, 0), (.020000005, 100), (.04, 0)])
        outside = self.write("outside.csv", [(0, 0), (.02000002, 100), (.04, 0)])
        self.assertEqual(self.compare(a, inside)["status"], "PASS")
        self.assertEqual(self.compare(a, outside)["status"], "REQUIRES_EVIDENCE")

    def test_full_raw_cycle_extrema_include_non_common_refined_rows(self):
        a = self.write("base.csv", [(0, 0), (.02, 0), (.04, 0)])
        b = self.write("refined.csv", [(0, 0), (.01, 100), (.02, .5), (.04, 0)])
        report = self.compare(a, b)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["diagnostics"]["signed_work"]["scale_J"], 100.)

    def test_alias_missing_metadata_and_unknown_addendum_are_rejected(self):
        a = self.write("base.csv", [(0, 0), (.02, 100), (.04, 0)])
        for change in ("alias", "collision", "metadata", "shaft"):
            contract = copy.deepcopy(self.contract)
            raw = contract["scenes"]["active_joint_energy"]
            if change == "alias": raw["columns"]["shaft_energy"] = "battery"
            elif change == "collision": raw["columns"]["loss_energy"] = "o_shaft_energy"
            elif change == "metadata": del raw["metadata"]
            else: del raw["columns"]["shaft_energy"]
            with self.subTest(change=change):
                self.assertEqual(self.compare(a, a, contract=contract)["status"], "REQUIRES_EVIDENCE")
        self.assertEqual(self.compare(a, a, "future-or-invalid")["status"], "REQUIRES_EVIDENCE")

    def test_final_net_at_actual_non_grid_end_is_checked_separately(self):
        a = self.write("base.csv", [(0, 0), (.02, 100), (.04, 0), (.045, 0)])
        b = self.write("refined.csv", [(0, 0), (.02, 100), (.04, 0), (.045, 2)])
        report = self.compare(a, b)
        self.assertEqual(self.check(report, "whole_curve_1percent_energy_shaft_energy"), "PASS")
        self.assertEqual(self.check(report, "final_energy_1percent_energy_shaft_energy"), "FAIL")
        self.assertEqual(report["diagnostics"]["signed_work"]["common_grid_count"], 3)

    def test_original_end_time_agreement_still_rejects(self):
        a = self.write("base.csv", [(0, 0), (.02, 100), (.04, 0)])
        b = self.write("refined.csv", [(0, 0), (.02, 100), (.04, 0), (.06, 0)])
        report = self.compare(a, b)
        self.assertEqual(self.check(report, "whole_curve_1percent_energy_shaft_energy"), "PASS")
        self.assertEqual(self.check(report, "same_actual_end_time"), "FAIL")

    def test_positive_battery_and_loss_do_not_use_signed_scale(self):
        a = self.write("base.csv", [(0, 0), (.02, 10000), (.04, 0)])
        b = self.write("refined.csv", [(0, 0), (.02, 10000), (.04, 0)])
        with b.open(newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        rows[-1][3:] = ["2764698", "102"]
        with b.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)
        report = self.compare(a, b)
        self.assertEqual(self.check(report, "whole_curve_1percent_energy_shaft_energy"), "PASS")
        self.assertEqual(self.check(report, "final_energy_1percent_energy_battery_energy"), "FAIL")
        self.assertEqual(self.check(report, "final_energy_1percent_energy_loss_energy"), "FAIL")


if __name__ == "__main__":
    unittest.main()
