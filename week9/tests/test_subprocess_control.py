from __future__ import annotations

import ctypes
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

from week9.experiments.subprocess_control import run_controlled


def _pid_exists(pid: int) -> bool:
    if sys.platform == "win32":
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information, False, pid
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class SubprocessControlTests(unittest.TestCase):
    def test_successful_process_is_captured_without_cleanup(self):
        result = run_controlled(
            [sys.executable, "-c", "print('controlled-ok')"],
            cwd=Path.cwd(),
            timeout_s=10.0,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("controlled-ok", result.stdout)
        self.assertFalse(result.timed_out)
        self.assertFalse(result.cleanup_attempted)
        self.assertTrue(result.cleanup_succeeded)

    def test_timeout_terminates_descendant_process(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pid_path = Path(temp_dir) / "child.pid"
            parent_script = (
                "import pathlib,subprocess,sys,time;"
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
                f"pathlib.Path({str(pid_path)!r}).write_text(str(child.pid),encoding='ascii');"
                "print('child-started',flush=True);time.sleep(60)"
            )
            result = run_controlled(
                [sys.executable, "-c", parent_script],
                cwd=temp_dir,
                timeout_s=1.5,
            )
            self.assertTrue(result.timed_out)
            self.assertTrue(result.cleanup_attempted)
            self.assertTrue(result.cleanup_succeeded, result.cleanup_detail)
            self.assertTrue(pid_path.is_file())
            child_pid = int(pid_path.read_text(encoding="ascii"))
            for _ in range(20):
                if not _pid_exists(child_pid):
                    break
                time.sleep(0.1)
            self.assertFalse(_pid_exists(child_pid), result.cleanup_detail)


if __name__ == "__main__":
    unittest.main()
