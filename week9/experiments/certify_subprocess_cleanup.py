"""Offline certification for timeout-driven descendant process cleanup."""

from __future__ import annotations

import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import time
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from week9.experiments.subprocess_control import run_controlled  # noqa: E402


RESULTS_ROOT = PROJECT_ROOT / "week9" / "results"
IMPLEMENTATION_PATH = PROJECT_ROOT / "week9" / "experiments" / "subprocess_control.py"


def certify_cleanup(
    *, output_root: Path | None = None, trials: int = 3
) -> tuple[dict[str, Any], Path]:
    if type(trials) is not int or trials < 3:
        raise ValueError("cleanup certification requires at least three trials")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = (output_root or RESULTS_ROOT).resolve()
    result_dir = root / f"subprocess-cleanup-certification-{timestamp}"
    result_dir.mkdir(parents=True, exist_ok=False)
    work_dir = result_dir / "work"
    work_dir.mkdir()

    normal = run_controlled(
        (sys.executable, "-c", "print('normal-exit',flush=True)"),
        cwd=work_dir,
        timeout_s=10.0,
    )
    records: list[dict[str, Any]] = []
    for trial in range(1, trials + 1):
        pid_path = work_dir / f"descendant-{trial}.pid"
        parent_script = (
            "import pathlib,subprocess,sys,time;"
            "time.sleep(0.2);"
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
            f"pathlib.Path({str(pid_path)!r}).write_text(str(child.pid),encoding='ascii');"
            "print('descendant-started',flush=True);time.sleep(60)"
        )
        outcome = run_controlled(
            (sys.executable, "-c", parent_script),
            cwd=work_dir,
            timeout_s=1.5,
        )
        descendant_pid = int(pid_path.read_text(encoding="ascii")) if pid_path.is_file() else 0
        for _ in range(30):
            if not descendant_pid or not _pid_exists(descendant_pid):
                break
            time.sleep(0.1)
        descendant_alive = bool(descendant_pid and _pid_exists(descendant_pid))
        records.append(
            {
                "trial": trial,
                "timed_out": outcome.timed_out,
                "cleanup_attempted": outcome.cleanup_attempted,
                "cleanup_succeeded": outcome.cleanup_succeeded,
                "cleanup_detail": outcome.cleanup_detail,
                "descendant_pid_recorded": descendant_pid > 0,
                "descendant_alive_after_cleanup": descendant_alive,
                "pass": bool(
                    outcome.timed_out
                    and outcome.cleanup_attempted
                    and outcome.cleanup_succeeded
                    and descendant_pid > 0
                    and not descendant_alive
                ),
            }
        )

    normal_pass = bool(
        normal.returncode == 0
        and not normal.timed_out
        and "normal-exit" in normal.stdout
    )
    passed = normal_pass and all(item["pass"] for item in records)
    report = {
        "schema_version": "v8-subprocess-cleanup-certification-1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "verdict": "PROCESS_TREE_CLEANUP_PASS" if passed else "PROCESS_TREE_CLEANUP_FAIL",
        "offline_only": True,
        "online_api_used": False,
        "holdout_accessed": False,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "implementation_sha256": hashlib.sha256(
            IMPLEMENTATION_PATH.read_bytes()
        ).hexdigest(),
        "normal_execution_pass": normal_pass,
        "timeout_trial_count": trials,
        "timeout_pass_count": sum(item["pass"] for item in records),
        "trials": records,
    }
    report_path = result_dir / "subprocess-cleanup-certification.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
    report_path.with_suffix(".sha256").write_text(
        f"{digest}  {report_path.name}\n", encoding="ascii"
    )
    return report, result_dir


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


def main() -> int:
    report, result_dir = certify_cleanup()
    print(f"process_tree_cleanup={report['verdict']} -> {result_dir}")
    return 0 if report["verdict"] == "PROCESS_TREE_CLEANUP_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
