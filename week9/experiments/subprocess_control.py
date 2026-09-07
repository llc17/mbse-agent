"""Fail-closed subprocess execution with process-tree cleanup.

Experiment runners can launch compilers and model clients of their own.  A
timeout therefore has to terminate the complete descendant tree, not only the
thin adapter process.  The helper is intentionally independent of any model
provider and never performs network access itself.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess
import sys
from typing import Mapping, Sequence


@dataclass(frozen=True, slots=True)
class ControlledProcessResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    cleanup_attempted: bool
    cleanup_succeeded: bool
    cleanup_detail: str = ""


def run_controlled(
    command: Sequence[str],
    *,
    cwd: str | Path,
    timeout_s: float,
    env: Mapping[str, str] | None = None,
) -> ControlledProcessResult:
    """Run one isolated command and reap its full process tree on timeout."""

    if not command or any(not isinstance(part, str) or not part for part in command):
        raise ValueError("command must contain non-empty string arguments")
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")

    popen_kwargs: dict[str, object] = {
        "cwd": str(Path(cwd).resolve()),
        "env": dict(env) if env is not None else None,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    process = subprocess.Popen(list(command), **popen_kwargs)
    windows_job, job_detail = _assign_windows_kill_job(process)
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
        _close_windows_handle(windows_job)
        return ControlledProcessResult(
            returncode=process.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
            timed_out=False,
            cleanup_attempted=False,
            cleanup_succeeded=True,
        )
    except subprocess.TimeoutExpired as exc:
        partial_stdout = _decode_partial(exc.stdout)
        partial_stderr = _decode_partial(exc.stderr)
        cleanup_succeeded, cleanup_detail = _terminate_process_tree(
            process, windows_job=windows_job, job_detail=job_detail
        )
        try:
            stdout, stderr = process.communicate(timeout=10.0)
        except subprocess.TimeoutExpired:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                pass
            stdout, stderr = "", ""
            _close_pipe(process.stdout)
            _close_pipe(process.stderr)
            cleanup_succeeded = False
            cleanup_detail = _join_detail(
                cleanup_detail, "direct child required final kill fallback"
            )
        return ControlledProcessResult(
            returncode=process.returncode,
            stdout=_merge_partial(partial_stdout, stdout or ""),
            stderr=_merge_partial(partial_stderr, stderr or ""),
            timed_out=True,
            cleanup_attempted=True,
            cleanup_succeeded=cleanup_succeeded,
            cleanup_detail=cleanup_detail,
        )


def _terminate_process_tree(
    process: subprocess.Popen[str], *, windows_job: int | None, job_detail: str
) -> tuple[bool, str]:
    if process.poll() is not None:
        return True, "process exited before cleanup"

    if sys.platform == "win32":
        if windows_job is not None:
            _close_windows_handle(windows_job)
            try:
                process.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                return False, _join_detail(job_detail, "job close did not terminate child")
            return True, _join_detail(job_detail, "job closed with kill-on-close")
        try:
            taskkill = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            process.kill()
            return False, f"taskkill failed: {type(exc).__name__}: {exc}"
        if process.poll() is None:
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                return False, _join_detail(
                    taskkill.stderr.strip(), "taskkill did not reap direct child"
                )
        succeeded = taskkill.returncode == 0
        detail = (taskkill.stdout or taskkill.stderr).strip()
        return succeeded, detail

    details: list[str] = []
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=3.0)
    except ProcessLookupError:
        return True, "process group already exited"
    except subprocess.TimeoutExpired:
        details.append("SIGTERM grace period expired")
        try:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5.0)
        except (ProcessLookupError, subprocess.TimeoutExpired) as exc:
            process.kill()
            return False, _join_detail(*details, f"SIGKILL failed: {exc}")
    except OSError as exc:
        process.kill()
        return False, f"process-group cleanup failed: {type(exc).__name__}: {exc}"
    return True, _join_detail(*details, "process group terminated")


def _assign_windows_kill_job(
    process: subprocess.Popen[str],
) -> tuple[int | None, str]:
    if sys.platform != "win32":
        return None, ""
    try:
        import ctypes
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(
            handle, 9, ctypes.byref(info), ctypes.sizeof(info)
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            _close_windows_handle(int(handle))
            raise error
        process_handle = wintypes.HANDLE(int(process._handle))
        if not kernel32.AssignProcessToJobObject(handle, process_handle):
            error = ctypes.WinError(ctypes.get_last_error())
            _close_windows_handle(int(handle))
            raise error
        return int(handle), "Windows Job Object assigned"
    except (AttributeError, OSError) as exc:
        return None, f"Windows Job Object unavailable: {type(exc).__name__}: {exc}"


def _close_windows_handle(handle: int | None) -> None:
    if sys.platform != "win32" or handle is None:
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle(wintypes.HANDLE(handle))


def _close_pipe(pipe: object | None) -> None:
    if pipe is None:
        return
    try:
        pipe.close()  # type: ignore[attr-defined]
    except OSError:
        pass


def _decode_partial(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _merge_partial(partial: str, final: str) -> str:
    if not partial:
        return final
    if final.startswith(partial):
        return final
    return partial + final


def _join_detail(*parts: str) -> str:
    return "; ".join(part.strip() for part in parts if part and part.strip())


__all__ = ["ControlledProcessResult", "run_controlled"]
