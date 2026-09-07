from __future__ import annotations

import csv
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from week9.src.modelica_templates import get_template_spec
from week9.src.modelica_tool import (
    _ProcessOutcome,
    _run_process,
    CsvValidationError,
    OpenModelicaTool,
    read_simulation_csv,
)
from week9.src.schemas import CheckStatus, FailureKind


class CsvEvidenceTests(unittest.TestCase):
    def _write(self, rows: list[list[object]]) -> Path:
        root = Path(self._temporary.name)
        path = root / f"case-{len(list(root.iterdir()))}.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows(rows)
        return path

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="v8-csv-test-")

    def tearDown(self):
        self._temporary.cleanup()

    def test_valid_csv_is_parsed(self):
        path = self._write([["time", "signal"], [0, 1], [1, 2]])
        data = read_simulation_csv(path, ("time", "signal"))
        self.assertEqual(data.row_count, 2)
        self.assertEqual(data.columns["signal"], (1.0, 2.0))

    def test_identical_duplicate_endpoint_is_only_collapsed(self):
        path = self._write(
            [["time", "signal"], [0, 1], [1, 2], [1, 2]]
        )
        data = read_simulation_csv(path, ("time", "signal"))
        self.assertEqual(data.row_count, 2)
        self.assertEqual(data.raw_row_count, 3)
        self.assertEqual(data.duplicate_rows_collapsed, 1)

    def test_only_one_distinct_time_after_dedup_is_unverified(self):
        path = self._write([["time", "signal"], [0, 1], [0, 1]])
        with self.assertRaises(CsvValidationError) as caught:
            read_simulation_csv(path, ("time", "signal"))
        self.assertEqual(caught.exception.status, CheckStatus.UNVERIFIED)
        self.assertEqual(
            caught.exception.category, "csv_insufficient_distinct_times"
        )

    def test_conflicting_duplicate_time_is_rejected(self):
        path = self._write(
            [["time", "signal"], [0, 1], [1, 2], [1, 3]]
        )
        with self.assertRaisesRegex(CsvValidationError, "同一时刻"):
            read_simulation_csv(path, ("time", "signal"))

    def test_time_reversal_is_rejected(self):
        path = self._write([["time", "signal"], [0, 1], [2, 2], [1, 3]])
        with self.assertRaisesRegex(CsvValidationError, "时间发生倒退"):
            read_simulation_csv(path, ("time", "signal"))

    def test_nan_is_rejected(self):
        path = self._write([["time", "signal"], [0, 1], [1, "nan"]])
        with self.assertRaisesRegex(CsvValidationError, "非有限值"):
            read_simulation_csv(path, ("time", "signal"))

    def test_missing_signal_is_unverified(self):
        path = self._write([["time", "other"], [0, 1], [1, 2]])
        with self.assertRaises(CsvValidationError) as caught:
            read_simulation_csv(path, ("time", "signal"))
        self.assertEqual(caught.exception.status, CheckStatus.UNVERIFIED)


class ToolAvailabilityTests(unittest.TestCase):
    def test_missing_omc_is_infrastructure_error(self):
        with tempfile.TemporaryDirectory(prefix="v8-no-omc-") as temporary:
            tool = OpenModelicaTool(Path(temporary) / "does-not-exist.exe")
            result = tool.execute_source(
                "model NoTool end NoTool;",
                model_name="NoTool",
                template_spec=get_template_spec("rc_filter"),
                workspace_root=temporary,
            )
        self.assertEqual(result.compile_check.status, CheckStatus.ERROR)
        self.assertEqual(result.compile_check.failure_kind, FailureKind.INFRASTRUCTURE)
        self.assertEqual(result.simulation_check.status, CheckStatus.ERROR)

    def test_file_lock_is_infrastructure_error(self):
        locked = OSError("injected file lock")
        locked.winerror = 32
        outcome = _ProcessOutcome(
            command=("omc", "build.mos"),
            returncode=None,
            stdout="",
            duration_s=0.01,
            error=locked,
        )
        tool = OpenModelicaTool()
        with tempfile.TemporaryDirectory(prefix="v8-lock-") as temporary:
            with patch("week9.src.modelica_tool._run_process", return_value=outcome):
                result = tool.execute_source(
                    "model Locked end Locked;",
                    model_name="Locked",
                    template_spec=get_template_spec("rc_filter"),
                    workspace_root=temporary,
                )
        self.assertEqual(result.compile_check.status, CheckStatus.ERROR)
        self.assertEqual(result.compile_check.failure_category, "file_lock")
        self.assertEqual(result.compile_check.failure_kind, FailureKind.INFRASTRUCTURE)

    def test_success_message_without_csv_is_unverified(self):
        tool = OpenModelicaTool()
        outcome = _ProcessOutcome(
            command=("simulation.exe",),
            returncode=0,
            stdout="simulation finished",
            duration_s=0.01,
        )
        with tempfile.TemporaryDirectory(prefix="v8-missing-csv-") as temporary:
            root = Path(temporary)
            check, data = tool._simulation_result(
                outcome=outcome,
                csv_path=root / "absent.csv",
                required_variables=("time", "signal"),
                run_dir=root,
                started_at="2026-08-29T00:00:00+00:00",
                expected_start_time=0.0,
                expected_stop_time=1.0,
            )
        self.assertEqual(check.status, CheckStatus.UNVERIFIED)
        self.assertEqual(check.failure_category, "simulation_csv_missing")
        self.assertIsNone(data)

    def test_truncated_csv_time_coverage_is_unverified(self):
        tool = OpenModelicaTool()
        outcome = _ProcessOutcome(
            command=("simulation.exe",),
            returncode=0,
            stdout="simulation finished",
            duration_s=0.01,
        )
        with tempfile.TemporaryDirectory(prefix="v8-short-csv-") as temporary:
            root = Path(temporary)
            csv_path = root / "short.csv"
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                csv.writer(handle).writerows(
                    [["time", "signal"], [0.0, 0.0], [0.5, 1.0]]
                )
            check, data = tool._simulation_result(
                outcome=outcome,
                csv_path=csv_path,
                required_variables=("time", "signal"),
                run_dir=root,
                started_at="2026-08-29T00:00:00+00:00",
                expected_start_time=0.0,
                expected_stop_time=1.0,
            )
        self.assertEqual(check.status, CheckStatus.UNVERIFIED)
        self.assertEqual(check.failure_category, "csv_time_coverage_incomplete")
        self.assertIsNone(data)

    def test_raw_model_name_cannot_inject_mos_commands(self):
        tool = OpenModelicaTool()
        with tempfile.TemporaryDirectory(prefix="v8-name-injection-") as temporary:
            result = tool.execute_source(
                "model Safe end Safe;",
                model_name='Safe); system("bad")',
                template_spec=get_template_spec("rc_filter"),
                workspace_root=temporary,
            )
        self.assertEqual(result.compile_check.status, CheckStatus.FAIL)
        self.assertEqual(result.compile_check.failure_category, "invalid_model_name")


class ProcessLifecycleTests(unittest.TestCase):
    def test_normal_process_output_and_return_code_are_captured(self):
        with tempfile.TemporaryDirectory(prefix="v8-process-ok-") as temporary:
            outcome = _run_process(
                [
                    sys.executable,
                    "-c",
                    "import sys;sys.stdout.buffer.write('V8 unicode: 测试\\n'.encode('utf-8'))",
                ],
                cwd=Path(temporary),
                timeout_s=5.0,
            )
        self.assertEqual(outcome.returncode, 0)
        self.assertIsNone(outcome.error)
        self.assertIn("V8 unicode: 测试", outcome.stdout)
        self.assertFalse(outcome.cleanup_attempted)

    @unittest.skipUnless(os.name == "nt", "Windows process-tree cleanup probe")
    def test_timeout_kills_descendant_and_does_not_wait_for_inherited_output(self):
        parent_script = (
            "import subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); "
            "print(child.pid,flush=True); time.sleep(30)"
        )
        child_pid = None
        started = time.monotonic()
        try:
            outcome = _run_process(
                [sys.executable, "-c", parent_script],
                cwd=Path(tempfile.gettempdir()),
                timeout_s=0.3,
            )
            elapsed = time.monotonic() - started
            pid_lines = [
                line.strip() for line in outcome.stdout.splitlines()
                if line.strip().isdigit()
            ]
            self.assertTrue(pid_lines, outcome.stdout)
            child_pid = int(pid_lines[0])
            self.assertTrue(outcome.timed_out)
            self.assertTrue(outcome.cleanup_attempted)
            self.assertTrue(
                outcome.tree_cleanup_succeeded, outcome.cleanup_error
            )
            self.assertLess(elapsed, 8.0)
            self.assertFalse(_windows_process_is_active(child_pid))
        finally:
            if child_pid is not None and _windows_process_is_active(child_pid):
                subprocess.run(
                    ["taskkill", "/PID", str(child_pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5.0,
                )


def _windows_process_is_active(pid: int) -> bool:
    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
