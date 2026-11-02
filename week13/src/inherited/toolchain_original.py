"""V11 的真实 SysML 与 OpenModelica 工具链适配层。

本模块只报告语法、编译和仿真证据；它不据此作出任何车辆物理结论。
"""

from __future__ import annotations

from dataclasses import asdict
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any

from week9.src.hard_gate import SysMLParserAdapter
from week9.src.modelica_tool import (
    CsvValidationError,
    _terminate_process_tree,
    read_simulation_csv,
)


_WEEK12_ROOT = Path(__file__).resolve().parents[1]
_OMC = Path(r"D:\openmodelica\bin\omc.exe")
_MODEL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")


def check_sysml(source: str) -> dict[str, Any]:
    """Parse SysML with the pinned V8 ANTLR adapter and preserve all evidence."""

    check, structure = SysMLParserAdapter().inspect(source)
    payload = asdict(check)
    # ``asdict`` preserves Enum objects; callers need stable JSON-ready values.
    payload["status"] = check.status.value
    payload["failure_kind"] = check.failure_kind.value
    payload["structure"] = asdict(structure) if structure is not None else None
    return payload


def execute_model(
    source: str,
    model_name: str,
    out: Path,
    stop: float,
    intervals: int = 2000,
    simulation_timeout_s: float = 120.0,
) -> dict[str, Any]:
    """Check, build, and execute a Modelica model in one newly-created directory.

    A zero process return code or an OpenModelica success log is insufficient:
    simulation passes only after its generated CSV is parsed, finite, and reaches
    ``stop``.  The function intentionally makes no domain-level physical claim.
    """

    run_dir = _prepare_out(out)
    source_path = run_dir / "model.mo"
    source_path.write_text(source, encoding="utf-8", newline="\n")
    result: dict[str, Any] = {
        "run_directory": str(run_dir),
        "source_path": str(source_path),
        "model_name": model_name,
        "stop": stop,
        "intervals": intervals,
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "numeric_tolerance": {"time_start": 0.0, "time_endpoint": max(1e-12, abs(stop) * 1e-9) if isinstance(stop, (int, float)) else None},
        "tool": {"omc_path": str(_OMC), "version": "unavailable", "msl_version": "unknown"},
        "compile": {"status": "UNVERIFIED", "check_model": "UNVERIFIED", "build_model": "UNVERIFIED", "issues": []},
        "simulation": {"status": "UNVERIFIED", "issues": [], "physical_conclusion": "not_made"},
    }
    if not isinstance(stop, (int, float)) or not math.isfinite(stop) or stop <= 0:
        result["compile"] = _failed("FAIL", "invalid_stop")
        return _finish(result, run_dir)
    if not isinstance(intervals, int) or intervals < 2:
        result["compile"] = _failed("FAIL", "invalid_intervals")
        return _finish(result, run_dir)
    if not isinstance(simulation_timeout_s, (int, float)) or not math.isfinite(simulation_timeout_s) or simulation_timeout_s <= 0:
        result["simulation"] = _failed("FAIL", "invalid_simulation_timeout")
        return _finish(result, run_dir)
    if not _MODEL_NAME.fullmatch(model_name):
        result["compile"] = _failed("FAIL", "invalid_model_name")
        return _finish(result, run_dir)
    if not _OMC.is_file():
        result["compile"] = _failed("ERROR", "omc_unavailable")
        result["simulation"] = _failed("ERROR", "compile_prerequisite_failed")
        return _finish(result, run_dir)

    version = _run([str(_OMC), "--version"], run_dir, 20.0)
    (run_dir / "version.stdout.log").write_text(version["stdout"], encoding="utf-8")
    (run_dir / "version.stderr.log").write_text(version["stderr"], encoding="utf-8")
    if version["returncode"] == 0:
        result["tool"]["version"] = version["stdout"].strip().splitlines()[0] if version["stdout"].strip() else "unknown"

    mos = _build_mos(model_name, stop, intervals)
    mos_path = run_dir / "build.mos"
    mos_path.write_text(mos, encoding="utf-8", newline="\n")
    build = _run([str(_OMC), str(mos_path.name)], run_dir, 120.0)
    (run_dir / "build.stdout.log").write_text(build["stdout"], encoding="utf-8")
    (run_dir / "build.stderr.log").write_text(build["stderr"], encoding="utf-8")
    result["tool"]["msl_version"] = _marker(build["stdout"], "__V11_MSL_VERSION__") or "unknown"
    check_text = _marker(build["stdout"], "__V11_CHECK_MODEL__")
    exe_text = _marker(build["stdout"], "__V11_BUILD_EXE__")
    executable = _resolve_generated(run_dir, exe_text)
    compile_issues: list[str] = []
    check_ok = build["returncode"] == 0 and _check_model_succeeded(check_text)
    build_ok = check_ok and executable is not None and executable.is_file()
    if build["timed_out"]:
        compile_issues.append("openmodelica_timeout")
    if build["uncertain"]:
        compile_issues.append("process_cleanup_uncertain")
    if build["error"]:
        compile_issues.append(build["error"])
    if not check_text:
        compile_issues.append("checkModel evidence missing")
    elif not _check_model_succeeded(check_text):
        compile_issues.append("checkModel failed: " + check_text[:1000])
    if not exe_text:
        compile_issues.append("buildModel executable evidence missing")
    elif executable is None or not executable.is_file():
        compile_issues.append("buildModel executable missing: " + exe_text[:1000])
    result["compile"] = {
        "status": "PASS" if build_ok else "FAIL",
        "check_model": "PASS" if check_ok else "FAIL",
        "build_model": "PASS" if build_ok else "FAIL",
        "issues": compile_issues,
        "returncode": build["returncode"],
        "timed_out": build["timed_out"],
        "build_mos": str(mos_path),
        "executable": str(executable) if executable else "",
    }
    if not build_ok:
        result["simulation"] = _failed("UNVERIFIED", "compile_prerequisite_failed")
        return _finish(result, run_dir)

    assert executable is not None
    csv_path = run_dir / "model_res.csv"
    simulation = _run([str(executable), "-r=" + str(csv_path)], run_dir, float(simulation_timeout_s))
    (run_dir / "simulation.stdout.log").write_text(simulation["stdout"], encoding="utf-8")
    (run_dir / "simulation.stderr.log").write_text(simulation["stderr"], encoding="utf-8")
    result["simulation"] = _validate_simulation(simulation, csv_path, stop)
    result["simulation"]["wall_timeout_s"] = float(simulation_timeout_s)
    return _finish(result, run_dir)


def _prepare_out(out: Path) -> Path:
    candidate = Path(out).resolve()
    root = _WEEK12_ROOT.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("out must be inside week12") from exc
    if candidate.exists():
        raise FileExistsError("out must be a new directory: " + str(candidate))
    if not candidate.parent.is_dir():
        raise FileNotFoundError("out parent must already exist: " + str(candidate.parent))
    candidate.mkdir()
    return candidate


def _build_mos(model_name: str, stop: float, intervals: int) -> str:
    return f'''loaded := loadModel(Modelica);
print("__V11_MSL_VERSION__=" + getVersion(Modelica) + "\\n");
loadedFile := loadFile("model.mo");
check := checkModel({model_name});
print("__V11_CHECK_MODEL__=" + check + "\\n");
build := buildModel({model_name}, startTime=0, stopTime={stop:.17g}, numberOfIntervals={intervals}, outputFormat="csv", fileNamePrefix="model");
print("__V11_BUILD_EXE__=" + build[1] + "\\n");
print("__V11_ERRORS__=" + getErrorString() + "\\n");
'''


def _run(command: list[str], cwd: Path, timeout: float) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["PATH"] = str(_OMC.parent) + os.pathsep + environment.get("PATH", "")
    options: dict[str, Any] = {"cwd": str(cwd), "stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "env": environment}
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200) | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    else:
        options["start_new_session"] = True
    started = time.monotonic()
    try:
        process = subprocess.Popen(command, **options)
    except OSError as exc:
        return _run_result(None, b"", b"", False, False, f"{type(exc).__name__}: {exc}", started)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return _run_result(process.returncode, stdout, stderr, False, False, "", started)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        cleanup_ok, cleanup_error = _terminate_process_tree(process)
        interrupted = isinstance(exc, KeyboardInterrupt)
        if cleanup_ok:
            try:
                stdout, stderr = process.communicate(timeout=5.0)
            except subprocess.TimeoutExpired:
                cleanup_ok = False
                cleanup_error = (cleanup_error + "; post_cleanup_wait_timeout").strip("; ")
                stdout, stderr = b"", b""
        else:
            stdout, stderr = b"", b""
        error = "keyboard_interrupt" if interrupted else "timeout"
        return _run_result(process.poll(), stdout, stderr, not interrupted, not cleanup_ok, error, started, interrupted, cleanup_ok, cleanup_error)


def _run_result(returncode: int | None, stdout: bytes, stderr: bytes, timed_out: bool, uncertain: bool, error: str, started: float, interrupted: bool = False, cleanup_succeeded: bool | None = None, cleanup_error: str = "") -> dict[str, Any]:
    return {"returncode": returncode, "stdout": stdout.decode("utf-8", errors="replace"), "stderr": stderr.decode("utf-8", errors="replace"), "timed_out": timed_out, "uncertain": uncertain, "interrupted": interrupted, "cleanup_succeeded": cleanup_succeeded, "cleanup_error": cleanup_error, "error": error, "duration_s": time.monotonic() - started}


def _resolve_generated(run_dir: Path, reported: str) -> Path | None:
    reported = reported.strip().strip('"')
    if not reported:
        return None
    item = Path(reported)
    path = (item if item.is_absolute() else run_dir / item).resolve()
    try:
        path.relative_to(run_dir.resolve())
    except ValueError:
        return None
    if path.is_file():
        return path
    if not path.suffix and path.with_suffix(".exe").is_file():
        exe = path.with_suffix(".exe").resolve()
        try:
            exe.relative_to(run_dir.resolve())
        except ValueError:
            return None
        return exe
    return None


def _marker(text: str, name: str) -> str:
    found = re.search(re.escape(name) + r"=([^\r\n]*)", text)
    return found.group(1).strip() if found else ""


def _check_model_succeeded(text: str) -> bool:
    """OpenModelica releases return either ``true`` or a success sentence."""

    normalized = text.strip().lower()
    return normalized == "true" or "completed successfully" in normalized


def _validate_simulation(run: dict[str, Any], csv_path: Path, stop: float) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "FAIL", "issues": [], "returncode": run["returncode"], "timed_out": run["timed_out"], "uncertain": run["uncertain"], "csv_path": str(csv_path), "physical_conclusion": "not_made"}
    if run["uncertain"]:
        result.update({"status": "ERROR", "issues": ["process_cleanup_uncertain"]})
        return result
    if run["timed_out"]:
        result["issues"].append("simulation_timeout")
        return result
    if run["returncode"] != 0:
        result["issues"].append("simulation_process_failed")
        return result
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            headers = next(csv.reader(handle), [])
        headers = [header.strip() for header in headers]
        if not headers:
            raise CsvValidationError("CSV header missing", status=None, category="csv_header_missing")
        data = _read_event_csv(csv_path)
    except CsvValidationError as exc:
        result["issues"].append(exc.category)
        return result
    except (OSError, UnicodeError) as exc:
        result["issues"].append("csv_reader_error: " + str(exc))
        return result
    times = data.columns["time"]
    tolerance = max(1e-12, abs(stop) * 1e-9)
    if abs(times[0]) > tolerance or abs(times[-1] - stop) > tolerance:
        result["issues"].append("csv_time_coverage_incomplete")
        result["observed_start"] = times[0]
        result["observed_stop"] = times[-1]
        return result
    result.update({"status": "PASS", "row_count": data.row_count, "raw_row_count": data.raw_row_count, "observed_start": times[0], "observed_stop": times[-1], "validated_columns": list(data.columns)})
    return result


def _failed(status: str, issue: str) -> dict[str, Any]:
    return {"status": status, "issues": [issue]}


def _read_event_csv(path: Path):
    import csv
    from types import SimpleNamespace
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    if len(rows) < 3 or not rows[0] or rows[0][0].strip('"') != "time":
        raise CsvValidationError("CSV header missing", status=None, category="csv_header_missing")
    try:
        columns = {header.strip('"'): tuple(float(v) for v in values) for header, values in zip(rows[0], zip(*rows[1:]))}
    except (ValueError, TypeError) as exc:
        raise CsvValidationError("CSV contains nonnumeric data", status=None, category="csv_nonfinite") from exc
    if any(not math.isfinite(v) for values in columns.values() for v in values):
        raise CsvValidationError("CSV contains nonfinite data", status=None, category="csv_nonfinite")
    times = columns["time"]
    if any(b < a for a, b in zip(times, times[1:])):
        raise CsvValidationError("CSV time decreases", status=None, category="csv_time_not_monotonic")
    return SimpleNamespace(columns=columns, row_count=len(times), raw_row_count=len(times))


def _finish(result: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    """Persist a JSON-safe execution summary for every completed return path."""

    (run_dir / "tool-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return result
