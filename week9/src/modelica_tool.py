"""Fail-closed OpenModelica build and simulation wrapper.

Every invocation runs in a newly-created directory.  OpenModelica diagnostics
are classified separately from process/tool failures, and a successful return
message is never accepted without the expected executable and parseable CSV.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
import hashlib
import locale
import math
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import tempfile
import time
from typing import Mapping, Sequence
import uuid

from week9.src.diagnostics import classify_modelica_diagnostics
from week9.src.modelica_templates import (
    RenderedTemplate,
    SimulationPlan,
    TemplateSpec,
    get_template_spec,
    simulation_plan,
)
from week9.src.schemas import CheckStatus, FailureKind, QualityCheckResult, utc_now_iso


@dataclass(frozen=True, slots=True)
class CsvData:
    columns: Mapping[str, tuple[float, ...]]
    row_count: int
    raw_row_count: int
    duplicate_rows_collapsed: int = 0


@dataclass(frozen=True, slots=True)
class ModelicaExecution:
    run_directory: str
    source_path: str
    executable_path: str
    csv_path: str
    compile_check: QualityCheckResult
    simulation_check: QualityCheckResult
    csv_data: CsvData | None
    template_id: str = ""
    model_name: str = ""
    source_sha256: str = ""
    csv_sha256: str = ""
    simulation_start_time: float = 0.0
    simulation_stop_time: float = 0.0
    simulation_intervals: int = 0


@dataclass(frozen=True, slots=True)
class _ProcessOutcome:
    command: tuple[str, ...]
    returncode: int | None
    stdout: str
    duration_s: float
    error: BaseException | None = None
    timed_out: bool = False
    cleanup_attempted: bool = False
    tree_cleanup_succeeded: bool | None = None
    cleanup_error: str = ""


class OpenModelicaTool:
    """Compile and run one Modelica model with explicit evidence."""

    def __init__(
        self,
        omc_path: str | Path | None = None,
        *,
        compile_timeout_s: float = 120.0,
        simulation_timeout_s: float = 120.0,
    ) -> None:
        self.omc_path = resolve_omc_path(omc_path)
        self.compile_timeout_s = compile_timeout_s
        self.simulation_timeout_s = simulation_timeout_s
        self._version: str | None = None

    @property
    def available(self) -> bool:
        return self.omc_path is not None

    @property
    def version(self) -> str:
        if self._version is not None:
            return self._version
        if self.omc_path is None:
            self._version = "unavailable"
            return self._version
        outcome = _run_process(
            (str(self.omc_path), "--version"),
            cwd=self.omc_path.parent,
            timeout_s=min(self.compile_timeout_s, 15.0),
        )
        if outcome.error is not None or outcome.returncode != 0:
            self._version = "unknown"
        else:
            self._version = outcome.stdout.strip().splitlines()[0][:200]
        return self._version

    def execute_rendered(
        self,
        rendered: RenderedTemplate,
        *,
        workspace_root: str | Path,
    ) -> ModelicaExecution:
        spec = get_template_spec(rendered.template_id)
        return self.execute_source(
            rendered.code,
            model_name=rendered.model_name,
            template_spec=spec,
            workspace_root=workspace_root,
            parameter_values=rendered.parameter_map,
        )

    def execute_source(
        self,
        source: str,
        *,
        model_name: str,
        template_spec: TemplateSpec,
        workspace_root: str | Path,
        parameter_values: Mapping[str, float] | None = None,
    ) -> ModelicaExecution:
        """Build and simulate raw source (also used by error-injection tests)."""

        root = Path(workspace_root).resolve()
        safe_name = re.sub(r"[^A-Za-z0-9_]", "_", model_name)[:60] or "model"
        run_dir = root / f"{safe_name}-{uuid.uuid4().hex}"
        source_path = run_dir / "model.mo"
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", model_name):
            compile_check = _failed_check(
                "modelica_compile",
                CheckStatus.FAIL,
                FailureKind.CONTENT,
                "invalid_model_name",
                f"非法 Modelica 模型名：{model_name!r}",
                evidence={"reason": "invalid_model_name"},
                tool_version=self.version,
            )
            return _empty_execution(
                run_dir,
                source_path,
                template_spec,
                model_name,
                compile_check,
            )
        try:
            root.mkdir(parents=True, exist_ok=True)
            run_dir.mkdir(exist_ok=False)
            source_path.write_text(source, encoding="utf-8", newline="\n")
        except OSError as exc:
            category = _os_error_category(exc)
            compile_check = _failed_check(
                "modelica_compile",
                CheckStatus.ERROR,
                FailureKind.INFRASTRUCTURE,
                category,
                f"无法创建 Modelica 唯一工作目录或源码：{type(exc).__name__}: {exc}",
                evidence={"reason": category, "run_directory": str(run_dir)},
                tool_version=self.version,
            )
            return _empty_execution(
                run_dir,
                source_path,
                template_spec,
                model_name,
                compile_check,
            )
        source_sha256 = _file_sha256(source_path)
        plan = (
            simulation_plan(template_spec.template_id, parameter_values)
            if parameter_values is not None
            else SimulationPlan(
                start_time=0.0,
                stop_time=template_spec.simulation_stop_time,
                intervals=template_spec.simulation_intervals,
            )
        )

        if self.omc_path is None:
            compile_check = _failed_check(
                "modelica_compile",
                CheckStatus.ERROR,
                FailureKind.INFRASTRUCTURE,
                "openmodelica_unavailable",
                "找不到 OpenModelica 编译器",
                evidence={"reason": "omc_not_found", "run_directory": str(run_dir)},
                tool_version="unavailable",
            )
            return ModelicaExecution(
                run_directory=str(run_dir),
                source_path=str(source_path),
                executable_path="",
                csv_path="",
                compile_check=compile_check,
                simulation_check=_blocked_simulation_check(compile_check),
                csv_data=None,
                template_id=template_spec.template_id,
                model_name=model_name,
                source_sha256=source_sha256,
                simulation_start_time=plan.start_time,
                simulation_stop_time=plan.stop_time,
                simulation_intervals=plan.intervals,
            )

        build_prefix = f"v8_{safe_name}"
        mos_path = run_dir / "build.mos"
        try:
            mos_path.write_text(
                _build_mos(
                    source_filename=source_path.name,
                    model_name=model_name,
                    build_prefix=build_prefix,
                    start_time=plan.start_time,
                    stop_time=plan.stop_time,
                    intervals=plan.intervals,
                ),
                encoding="utf-8",
                newline="\n",
            )
        except OSError as exc:
            category = _os_error_category(exc)
            compile_check = _failed_check(
                "modelica_compile",
                CheckStatus.ERROR,
                FailureKind.INFRASTRUCTURE,
                category,
                f"无法写入 OpenModelica 构建脚本：{type(exc).__name__}: {exc}",
                evidence={"reason": category, "run_directory": str(run_dir)},
                tool_version=self.version,
            )
            return ModelicaExecution(
                run_directory=str(run_dir),
                source_path=str(source_path),
                executable_path="",
                csv_path="",
                compile_check=compile_check,
                simulation_check=_blocked_simulation_check(compile_check),
                csv_data=None,
                template_id=template_spec.template_id,
                model_name=model_name,
                source_sha256=source_sha256,
                simulation_start_time=plan.start_time,
                simulation_stop_time=plan.stop_time,
                simulation_intervals=plan.intervals,
            )
        started_at = utc_now_iso()
        compile_outcome = _run_process(
            (str(self.omc_path), mos_path.name),
            cwd=run_dir,
            timeout_s=self.compile_timeout_s,
        )
        executable = run_dir / f"{build_prefix}.exe"
        if not executable.exists():
            alternative = run_dir / build_prefix
            if alternative.exists():
                executable = alternative

        try:
            compile_check = self._compile_result(
                outcome=compile_outcome,
                source_path=source_path,
                executable=executable,
                run_dir=run_dir,
                started_at=started_at,
            )
        except OSError as exc:
            category = _os_error_category(exc)
            compile_check = _failed_check(
                "modelica_compile",
                CheckStatus.ERROR,
                FailureKind.INFRASTRUCTURE,
                category,
                f"读取编译产物失败：{type(exc).__name__}: {exc}",
                evidence={"reason": category, "run_directory": str(run_dir)},
                tool_version=self.version,
                started_at=started_at,
                duration_s=compile_outcome.duration_s,
            )
        if compile_check.status is not CheckStatus.PASS:
            return ModelicaExecution(
                run_directory=str(run_dir),
                source_path=str(source_path),
                executable_path=str(executable) if executable.exists() else "",
                csv_path="",
                compile_check=compile_check,
                simulation_check=_blocked_simulation_check(compile_check),
                csv_data=None,
                template_id=template_spec.template_id,
                model_name=model_name,
                source_sha256=source_sha256,
                simulation_start_time=plan.start_time,
                simulation_stop_time=plan.stop_time,
                simulation_intervals=plan.intervals,
            )

        csv_path = run_dir / "simulation.csv"
        simulation_started = utc_now_iso()
        simulation_outcome = _run_process(
            (str(executable), f"-r={csv_path.name}"),
            cwd=run_dir,
            timeout_s=self.simulation_timeout_s,
            extra_path=self.omc_path.parent,
        )
        try:
            simulation_check, csv_data = self._simulation_result(
                outcome=simulation_outcome,
                csv_path=csv_path,
                required_variables=template_spec.output_variables,
                run_dir=run_dir,
                started_at=simulation_started,
                expected_start_time=plan.start_time,
                expected_stop_time=plan.stop_time,
            )
            csv_sha256 = _file_sha256(csv_path) if csv_path.is_file() else ""
        except OSError as exc:
            category = _os_error_category(exc)
            simulation_check = _failed_check(
                "simulation",
                CheckStatus.ERROR,
                FailureKind.INFRASTRUCTURE,
                category,
                f"读取仿真产物失败：{type(exc).__name__}: {exc}",
                evidence={"reason": category, "run_directory": str(run_dir)},
                tool_version=self.version,
                started_at=simulation_started,
                duration_s=simulation_outcome.duration_s,
            )
            csv_data = None
            csv_sha256 = ""
        return ModelicaExecution(
            run_directory=str(run_dir),
            source_path=str(source_path),
            executable_path=str(executable),
            csv_path=str(csv_path) if csv_path.exists() else "",
            compile_check=compile_check,
            simulation_check=simulation_check,
            csv_data=csv_data,
            template_id=template_spec.template_id,
            model_name=model_name,
            source_sha256=source_sha256,
            csv_sha256=csv_sha256,
            simulation_start_time=plan.start_time,
            simulation_stop_time=plan.stop_time,
            simulation_intervals=plan.intervals,
        )

    def _compile_result(
        self,
        *,
        outcome: _ProcessOutcome,
        source_path: Path,
        executable: Path,
        run_dir: Path,
        started_at: str,
    ) -> QualityCheckResult:
        diagnostic_classification = classify_modelica_diagnostics(outcome.stdout)
        evidence = {
            "command": list(outcome.command),
            "returncode": outcome.returncode,
            "run_directory": str(run_dir),
            "source_sha256": _file_sha256(source_path),
            "executable_present": executable.is_file(),
            "diagnostics": _diagnostic_excerpt(outcome.stdout),
            "diagnostic_classification": diagnostic_classification.evidence(),
            "modelica_library_version": _marker_value(
                outcome.stdout, "__V8_MSL_VERSION__"
            ),
            "process_cleanup_attempted": outcome.cleanup_attempted,
            "process_tree_cleanup_succeeded": outcome.tree_cleanup_succeeded,
            "process_cleanup_error": outcome.cleanup_error,
        }
        if outcome.error is not None:
            category = (
                "process_tree_cleanup_failed"
                if outcome.cleanup_attempted
                and outcome.tree_cleanup_succeeded is False
                else "openmodelica_timeout"
                if outcome.timed_out
                else _os_error_category(outcome.error)
            )
            message = (
                f"OpenModelica 超时后的进程树清理失败：{outcome.cleanup_error}"
                if category == "process_tree_cleanup_failed"
                else f"OpenModelica 编译超时（{self.compile_timeout_s:g}s）"
                if category == "openmodelica_timeout"
                else f"无法执行 OpenModelica：{type(outcome.error).__name__}: {outcome.error}"
            )
            return _failed_check(
                "modelica_compile",
                CheckStatus.ERROR,
                FailureKind.INFRASTRUCTURE,
                category,
                message,
                evidence={**evidence, "reason": category},
                tool_version=self.version,
                started_at=started_at,
                duration_s=outcome.duration_s,
            )

        infrastructure_category = _infrastructure_diagnostic_category(outcome.stdout)
        if infrastructure_category:
            return _failed_check(
                "modelica_compile",
                CheckStatus.ERROR,
                FailureKind.INFRASTRUCTURE,
                infrastructure_category,
                "OpenModelica 构建遇到基础设施错误",
                evidence={**evidence, "reason": infrastructure_category},
                tool_version=self.version,
                started_at=started_at,
                duration_s=outcome.duration_s,
            )

        markers_ok = (
            _marker_value(outcome.stdout, "__V8_LOAD_MODELICA__") == "true"
            and _marker_value(outcome.stdout, "__V8_LOAD_SOURCE__") == "true"
            and "completed successfully" in _marker_block(
                outcome.stdout, "__V8_CHECK_BEGIN__", "__V8_CHECK_END__"
            )
        )
        issues = _compiler_issues(outcome.stdout)
        if outcome.returncode != 0 and not issues:
            return _failed_check(
                "modelica_compile",
                CheckStatus.ERROR,
                FailureKind.INFRASTRUCTURE,
                "openmodelica_process_failed",
                f"OpenModelica 进程异常退出且没有内容诊断（exit={outcome.returncode}）",
                evidence={**evidence, "reason": "openmodelica_process_failed"},
                tool_version=self.version,
                started_at=started_at,
                duration_s=outcome.duration_s,
            )
        if outcome.returncode != 0 or not markers_ok or not executable.is_file():
            if not issues:
                issues = ["OpenModelica 未产生可执行模型，或 checkModel 未明确成功"]
            return QualityCheckResult(
                check_type="modelica_compile",
                status=CheckStatus.FAIL,
                required=True,
                issues=issues,
                evidence={**evidence, "reason": "modelica_build_failed"},
                tool_name="OpenModelica",
                tool_version=self.version,
                started_at=started_at,
                duration_s=outcome.duration_s,
                failure_kind=FailureKind.CONTENT,
                failure_category="modelica_build_failed",
            )

        return QualityCheckResult(
            check_type="modelica_compile",
            status=CheckStatus.PASS,
            required=True,
            evidence={
                **evidence,
                "executable_path": str(executable),
                "executable_sha256": _file_sha256(executable),
            },
            tool_name="OpenModelica",
            tool_version=self.version,
            started_at=started_at,
            duration_s=outcome.duration_s,
        )

    def _simulation_result(
        self,
        *,
        outcome: _ProcessOutcome,
        csv_path: Path,
        required_variables: Sequence[str],
        run_dir: Path,
        started_at: str,
        expected_start_time: float,
        expected_stop_time: float,
    ) -> tuple[QualityCheckResult, CsvData | None]:
        evidence = {
            "command": list(outcome.command),
            "returncode": outcome.returncode,
            "run_directory": str(run_dir),
            "csv_path": str(csv_path),
            "csv_present": csv_path.is_file(),
            "diagnostics": _diagnostic_excerpt(outcome.stdout),
            "process_cleanup_attempted": outcome.cleanup_attempted,
            "process_tree_cleanup_succeeded": outcome.tree_cleanup_succeeded,
            "process_cleanup_error": outcome.cleanup_error,
        }
        if outcome.error is not None:
            category = (
                "process_tree_cleanup_failed"
                if outcome.cleanup_attempted
                and outcome.tree_cleanup_succeeded is False
                else "simulation_timeout"
                if outcome.timed_out
                else _os_error_category(outcome.error)
            )
            return (
                _failed_check(
                    "simulation",
                    CheckStatus.ERROR,
                    FailureKind.INFRASTRUCTURE,
                    category,
                    f"仿真进程失败：{type(outcome.error).__name__}: {outcome.error}",
                    evidence={**evidence, "reason": category},
                    tool_version=self.version,
                    started_at=started_at,
                    duration_s=outcome.duration_s,
                ),
                None,
            )
        infrastructure_category = _infrastructure_diagnostic_category(outcome.stdout)
        if infrastructure_category:
            return (
                _failed_check(
                    "simulation",
                    CheckStatus.ERROR,
                    FailureKind.INFRASTRUCTURE,
                    infrastructure_category,
                    "仿真进程遇到基础设施错误",
                    evidence={**evidence, "reason": infrastructure_category},
                    tool_version=self.version,
                    started_at=started_at,
                    duration_s=outcome.duration_s,
                ),
                None,
            )
        if outcome.returncode != 0:
            issues = _compiler_issues(outcome.stdout) or [
                f"仿真可执行文件返回非零退出码 {outcome.returncode}"
            ]
            if not _compiler_issues(outcome.stdout):
                return (
                    _failed_check(
                        "simulation",
                        CheckStatus.ERROR,
                        FailureKind.INFRASTRUCTURE,
                        "simulation_process_failed",
                        issues[0],
                        evidence={**evidence, "reason": "simulation_process_failed"},
                        tool_version=self.version,
                        started_at=started_at,
                        duration_s=outcome.duration_s,
                    ),
                    None,
                )
            return (
                _failed_check(
                    "simulation",
                    CheckStatus.FAIL,
                    FailureKind.CONTENT,
                    "modelica_runtime_failure",
                    issues[0],
                    extra_issues=issues[1:],
                    evidence={**evidence, "reason": "modelica_runtime_failure"},
                    tool_version=self.version,
                    started_at=started_at,
                    duration_s=outcome.duration_s,
                ),
                None,
            )
        if not csv_path.is_file() or csv_path.stat().st_size == 0:
            return (
                _failed_check(
                    "simulation",
                    CheckStatus.UNVERIFIED,
                    FailureKind.CONTENT,
                    "simulation_csv_missing",
                    "仿真进程结束但必需 CSV 缺失或为空",
                    evidence={**evidence, "reason": "simulation_csv_missing"},
                    tool_version=self.version,
                    started_at=started_at,
                    duration_s=outcome.duration_s,
                ),
                None,
            )
        try:
            csv_data = read_simulation_csv(csv_path, required_variables)
        except CsvValidationError as exc:
            return (
                _failed_check(
                    "simulation",
                    exc.status,
                    FailureKind.CONTENT,
                    exc.category,
                    str(exc),
                    evidence={**evidence, "reason": exc.category},
                    tool_version=self.version,
                    started_at=started_at,
                    duration_s=outcome.duration_s,
                ),
                None,
            )
        except (OSError, csv.Error, UnicodeError) as exc:
            return (
                _failed_check(
                    "simulation",
                    CheckStatus.ERROR,
                    FailureKind.INFRASTRUCTURE,
                    "csv_reader_error",
                    f"CSV 验证器异常：{type(exc).__name__}: {exc}",
                    evidence={**evidence, "reason": "csv_reader_error"},
                    tool_version=self.version,
                    started_at=started_at,
                    duration_s=outcome.duration_s,
                ),
                None,
            )

        times = csv_data.columns["time"]
        coverage_tolerance = max(
            1e-12,
            abs(expected_stop_time - expected_start_time) * 1e-9,
        )
        if (
            abs(times[0] - expected_start_time) > coverage_tolerance
            or abs(times[-1] - expected_stop_time) > coverage_tolerance
        ):
            return (
                _failed_check(
                    "simulation",
                    CheckStatus.UNVERIFIED,
                    FailureKind.CONTENT,
                    "csv_time_coverage_incomplete",
                    "仿真 CSV 未覆盖声明的完整时间窗",
                    evidence={
                        **evidence,
                        "reason": "csv_time_coverage_incomplete",
                        "expected_start_time": expected_start_time,
                        "expected_stop_time": expected_stop_time,
                        "observed_start_time": times[0],
                        "observed_stop_time": times[-1],
                    },
                    tool_version=self.version,
                    started_at=started_at,
                    duration_s=outcome.duration_s,
                ),
                None,
            )
        return (
            QualityCheckResult(
                check_type="simulation",
                status=CheckStatus.PASS,
                required=True,
                evidence={
                    **evidence,
                    "csv_sha256": _file_sha256(csv_path),
                    "csv_size": csv_path.stat().st_size,
                    "row_count": csv_data.row_count,
                    "raw_row_count": csv_data.raw_row_count,
                    "duplicate_rows_collapsed": csv_data.duplicate_rows_collapsed,
                    "validated_variables": list(csv_data.columns),
                    "expected_start_time": expected_start_time,
                    "expected_stop_time": expected_stop_time,
                },
                tool_name="OpenModelica runtime + V8 CSV validator",
                tool_version=self.version,
                started_at=started_at,
                duration_s=outcome.duration_s,
            ),
            csv_data,
        )


class CsvValidationError(ValueError):
    def __init__(self, message: str, *, status: CheckStatus, category: str):
        self.status = status
        self.category = category
        super().__init__(message)


def read_simulation_csv(path: str | Path, required_variables: Sequence[str]) -> CsvData:
    """Read only required numeric columns and enforce strict time-series shape."""

    csv_path = Path(path)
    if not csv_path.is_file() or csv_path.stat().st_size == 0:
        raise CsvValidationError(
            "必需仿真 CSV 缺失或为空",
            status=CheckStatus.UNVERIFIED,
            category="simulation_csv_missing",
        )
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            headers = next(reader)
        except StopIteration as exc:
            raise CsvValidationError(
                "仿真 CSV 没有表头",
                status=CheckStatus.UNVERIFIED,
                category="csv_header_missing",
            ) from exc
        headers = [item.strip() for item in headers]
        if len(headers) != len(set(headers)):
            raise CsvValidationError(
                "仿真 CSV 包含重复列名",
                status=CheckStatus.FAIL,
                category="csv_duplicate_columns",
            )
        missing = [item for item in required_variables if item not in headers]
        if missing:
            raise CsvValidationError(
                f"仿真 CSV 缺少必需信号：{', '.join(missing)}",
                status=CheckStatus.UNVERIFIED,
                category="csv_signal_missing",
            )
        indices = {name: headers.index(name) for name in required_variables}
        values: dict[str, list[float]] = {name: [] for name in required_variables}
        for row_number, row in enumerate(reader, start=2):
            if len(row) != len(headers):
                raise CsvValidationError(
                    f"仿真 CSV 第 {row_number} 行列数不一致",
                    status=CheckStatus.FAIL,
                    category="csv_malformed_row",
                )
            for name, index in indices.items():
                try:
                    number = float(row[index])
                except ValueError as exc:
                    raise CsvValidationError(
                        f"仿真 CSV 第 {row_number} 行信号 {name} 不是数值",
                        status=CheckStatus.FAIL,
                        category="csv_non_numeric",
                    ) from exc
                if not math.isfinite(number):
                    raise CsvValidationError(
                        f"仿真 CSV 第 {row_number} 行信号 {name} 非有限值",
                        status=CheckStatus.FAIL,
                        category="csv_non_finite",
                    )
                values[name].append(number)

    raw_row_count = len(next(iter(values.values()), ()))
    if raw_row_count < 2:
        raise CsvValidationError(
            "仿真 CSV 数据点不足",
            status=CheckStatus.UNVERIFIED,
            category="csv_insufficient_rows",
        )
    times = values.get("time")
    if times is None:
        raise CsvValidationError(
            "仿真 CSV 缺少 time 列",
            status=CheckStatus.UNVERIFIED,
            category="csv_time_missing",
        )
    keep_indices = [0]
    duplicates = 0
    for index in range(1, raw_row_count):
        previous_index = keep_indices[-1]
        if times[index] < times[previous_index]:
            raise CsvValidationError(
                "仿真 CSV 时间发生倒退",
                status=CheckStatus.FAIL,
                category="csv_time_not_increasing",
            )
        if times[index] == times[previous_index]:
            if any(values[name][index] != values[name][previous_index] for name in values):
                raise CsvValidationError(
                    "仿真 CSV 同一时刻包含不一致的必需信号值",
                    status=CheckStatus.FAIL,
                    category="csv_ambiguous_duplicate_time",
                )
            duplicates += 1
            continue
        keep_indices.append(index)
    filtered = {
        name: tuple(column[index] for index in keep_indices)
        for name, column in values.items()
    }
    if len(keep_indices) < 2:
        raise CsvValidationError(
            "仿真 CSV 去除完全重复时刻后数据点不足",
            status=CheckStatus.UNVERIFIED,
            category="csv_insufficient_distinct_times",
        )
    return CsvData(
        columns=filtered,
        row_count=len(keep_indices),
        raw_row_count=raw_row_count,
        duplicate_rows_collapsed=duplicates,
    )


def resolve_omc_path(explicit: str | Path | None = None) -> Path | None:
    if explicit is not None:
        try:
            resolved = Path(explicit).expanduser().resolve()
        except OSError:
            return None
        return resolved if resolved.is_file() else None
    candidates: list[Path] = []
    command = shutil.which("omc")
    if command:
        candidates.append(Path(command))
    install_root = os.environ.get("OPENMODELICAHOME")
    if install_root:
        candidates.append(Path(install_root) / "bin" / "omc.exe")
    if os.name == "nt":
        candidates.append(Path(r"D:\openmodelica\bin\omc.exe"))
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved.is_file():
            return resolved
    return None


def _build_mos(
    *,
    source_filename: str,
    model_name: str,
    build_prefix: str,
    start_time: float,
    stop_time: float,
    intervals: int,
) -> str:
    return f'''modelicaLoaded := loadModel(Modelica);
print("__V8_LOAD_MODELICA__=" + String(modelicaLoaded) + "\\n");
print("__V8_MSL_VERSION__=" + getVersion(Modelica) + "\\n");
sourceLoaded := loadFile("{source_filename}");
print("__V8_LOAD_SOURCE__=" + String(sourceLoaded) + "\\n");
checkResult := checkModel({model_name});
print("__V8_CHECK_BEGIN__\\n" + checkResult + "__V8_CHECK_END__\\n");
buildResult := buildModel({model_name}, startTime={start_time:.17g}, stopTime={stop_time:.17g}, numberOfIntervals={intervals}, tolerance=1e-6, method="dassl", fileNamePrefix="{build_prefix}", outputFormat="csv");
print("__V8_BUILD_EXE__=" + buildResult[1] + "\\n");
print("__V8_BUILD_INIT__=" + buildResult[2] + "\\n");
print("__V8_ERRORS_BEGIN__\\n" + getErrorString() + "__V8_ERRORS_END__\\n");
'''


def _run_process(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_s: float,
    extra_path: Path | None = None,
) -> _ProcessOutcome:
    started = time.monotonic()
    process_environment = None
    if extra_path is not None:
        process_environment = os.environ.copy()
        process_environment["PATH"] = str(extra_path) + os.pathsep + process_environment.get(
            "PATH", ""
        )
    popen_options: dict[str, object] = {}
    if os.name == "nt":
        popen_options["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
    else:
        popen_options["start_new_session"] = True

    with tempfile.TemporaryFile(mode="w+b") as captured:
        try:
            process = subprocess.Popen(
                list(command),
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=captured,
                stderr=subprocess.STDOUT,
                close_fds=True,
                env=process_environment,
                **popen_options,
            )
        except OSError as exc:
            return _ProcessOutcome(
                tuple(command), None, "", time.monotonic() - started, exc
            )

        try:
            returncode = process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            cleanup_succeeded, cleanup_error = _terminate_process_tree(process)
            output = _read_captured_output(captured)
            return _ProcessOutcome(
                command=tuple(command),
                returncode=process.poll(),
                stdout=output,
                duration_s=time.monotonic() - started,
                error=exc,
                timed_out=True,
                cleanup_attempted=True,
                tree_cleanup_succeeded=cleanup_succeeded,
                cleanup_error=cleanup_error,
            )
        except OSError as exc:
            cleanup_succeeded, cleanup_error = _terminate_process_tree(process)
            output = _read_captured_output(captured)
            return _ProcessOutcome(
                command=tuple(command),
                returncode=process.poll(),
                stdout=output,
                duration_s=time.monotonic() - started,
                error=exc,
                cleanup_attempted=True,
                tree_cleanup_succeeded=cleanup_succeeded,
                cleanup_error=cleanup_error,
            )
        return _ProcessOutcome(
            command=tuple(command),
            returncode=returncode,
            stdout=_read_captured_output(captured),
            duration_s=time.monotonic() - started,
        )


def _read_captured_output(handle, *, limit_bytes: int = 32 * 1024 * 1024) -> str:
    """Read bounded process output without waiting for inherited pipe handles."""

    handle.flush()
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    handle.seek(0)
    if size <= limit_bytes:
        payload = handle.read()
    else:
        half = limit_bytes // 2
        beginning = handle.read(half)
        handle.seek(-half, os.SEEK_END)
        ending = handle.read(half)
        payload = beginning + b"\n__V8_PROCESS_OUTPUT_TRUNCATED__\n" + ending
    return payload.decode("utf-8", errors="replace")


def _terminate_process_tree(
    process: subprocess.Popen, *, cleanup_timeout_s: float = 5.0
) -> tuple[bool, str]:
    """Terminate a timed-out process and its descendants, then reap the root."""

    errors: list[str] = []
    tree_signal_succeeded = False
    if os.name == "nt":
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        taskkill = system_root / "System32" / "taskkill.exe"
        try:
            completed = subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=cleanup_timeout_s,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            )
            tree_signal_succeeded = completed.returncode == 0
            if not tree_signal_succeeded:
                detail = completed.stdout.decode(
                    locale.getpreferredencoding(False), errors="replace"
                ).strip()[:500]
                errors.append(
                    f"taskkill_exit={completed.returncode}"
                    + (f": {detail}" if detail else "")
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"taskkill_{type(exc).__name__}: {exc}")
        if not tree_signal_succeeded:
            fallback_succeeded, fallback_error = _terminate_tree_with_psutil(
                process.pid, cleanup_timeout_s
            )
            tree_signal_succeeded = fallback_succeeded
            if fallback_error:
                errors.append(fallback_error)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            tree_signal_succeeded = True
        except ProcessLookupError:
            tree_signal_succeeded = process.poll() is not None
        except OSError as exc:
            errors.append(f"killpg_{type(exc).__name__}: {exc}")

    try:
        process.wait(timeout=cleanup_timeout_s)
    except subprocess.TimeoutExpired:
        errors.append("root_wait_timeout")
        try:
            process.kill()
            process.wait(timeout=cleanup_timeout_s)
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"root_kill_{type(exc).__name__}: {exc}")
    except OSError as exc:
        errors.append(f"root_wait_{type(exc).__name__}: {exc}")

    root_reaped = process.poll() is not None
    if not root_reaped:
        errors.append("root_process_not_reaped")
    succeeded = tree_signal_succeeded and root_reaped
    return succeeded, "; ".join(errors)


def _terminate_tree_with_psutil(
    root_pid: int, cleanup_timeout_s: float
) -> tuple[bool, str]:
    """Optional same-user fallback when Windows taskkill is unavailable or denied."""

    try:
        import psutil
    except ImportError:
        return False, "psutil_fallback_unavailable"
    try:
        root = psutil.Process(root_pid)
        processes = [*root.children(recursive=True), root]
    except psutil.NoSuchProcess:
        return True, ""
    except (psutil.AccessDenied, OSError) as exc:
        return False, f"psutil_enumeration_{type(exc).__name__}: {exc}"
    errors: list[str] = []
    # Enumerate descendants before the root.  Killing the root first can make
    # later tree discovery/reaping unreliable on Windows, which is exactly the
    # ordering hazard this fallback is meant to avoid.
    for item in processes:
        try:
            item.kill()
        except psutil.NoSuchProcess:
            continue
        except (psutil.AccessDenied, OSError) as exc:
            errors.append(f"psutil_kill_{item.pid}_{type(exc).__name__}: {exc}")
    _, alive = psutil.wait_procs(processes, timeout=cleanup_timeout_s)
    for item in alive:
        if item.is_running():
            errors.append(f"psutil_process_still_active={item.pid}")
    return not alive and not errors, "; ".join(errors)


def _blocked_simulation_check(compile_check: QualityCheckResult) -> QualityCheckResult:
    infrastructure = compile_check.failure_kind is FailureKind.INFRASTRUCTURE
    return _failed_check(
        "simulation",
        CheckStatus.ERROR if infrastructure else CheckStatus.UNVERIFIED,
        FailureKind.INFRASTRUCTURE if infrastructure else FailureKind.CONTENT,
        "compile_prerequisite_failed",
        f"未运行仿真：modelica_compile={compile_check.status.value}",
        evidence={
            "reason": "compile_prerequisite_failed",
            "compile_status": compile_check.status.value,
        },
        tool_version=compile_check.tool_version,
    )


def _empty_execution(
    run_dir: Path,
    source_path: Path,
    template_spec: TemplateSpec,
    model_name: str,
    compile_check: QualityCheckResult,
) -> ModelicaExecution:
    return ModelicaExecution(
        run_directory=str(run_dir),
        source_path=str(source_path),
        executable_path="",
        csv_path="",
        compile_check=compile_check,
        simulation_check=_blocked_simulation_check(compile_check),
        csv_data=None,
        template_id=template_spec.template_id,
        model_name=model_name,
        simulation_start_time=0.0,
        simulation_stop_time=template_spec.simulation_stop_time,
        simulation_intervals=template_spec.simulation_intervals,
    )


def _failed_check(
    check_type: str,
    status: CheckStatus,
    failure_kind: FailureKind,
    category: str,
    issue: str,
    *,
    extra_issues: Sequence[str] = (),
    evidence: Mapping[str, object] | None = None,
    tool_version: str = "",
    started_at: str | None = None,
    duration_s: float = 0.0,
) -> QualityCheckResult:
    return QualityCheckResult(
        check_type=check_type,
        status=status,
        required=True,
        issues=[issue, *extra_issues],
        evidence=dict(evidence or {"reason": category}),
        tool_name="OpenModelica" if check_type == "modelica_compile" else "OpenModelica runtime",
        tool_version=tool_version,
        started_at=started_at or utc_now_iso(),
        duration_s=duration_s,
        failure_kind=failure_kind,
        failure_category=category,
    )


def _marker_value(text: str, marker: str) -> str:
    match = re.search(re.escape(marker) + r"=([^\r\n]*)", text)
    return match.group(1).strip() if match else ""


def _marker_block(text: str, start: str, end: str) -> str:
    match = re.search(re.escape(start) + r"(.*?)" + re.escape(end), text, re.DOTALL)
    return match.group(1).strip() if match else ""


def _compiler_issues(output: str) -> list[str]:
    issues: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if not stripped:
            continue
        if any(
            token in lowered
            for token in (
                "error:",
                "[error",
                "failed to",
                "syntax error",
                "not found in scope",
                "assertion",
                "solver failed",
                "division by zero",
            )
        ):
            issues.append(stripped[:1000])
        if len(issues) >= 8:
            break
    return issues


def _infrastructure_diagnostic_category(output: str) -> str:
    lowered = output.lower()
    if "being used by another process" in lowered or "winerror 32" in lowered:
        return "file_lock"
    if "access is denied" in lowered or "permission denied" in lowered:
        return "access_denied"
    return ""


def _os_error_category(error: BaseException) -> str:
    winerror = getattr(error, "winerror", None)
    if winerror == 32:
        return "file_lock"
    if isinstance(error, PermissionError):
        return "access_denied"
    if isinstance(error, FileNotFoundError):
        return "executable_missing"
    return "process_start_error"


def _diagnostic_excerpt(text: str, limit: int = 8000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
