"""Concrete deterministic checks used by the V8 hard quality gate."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterable, Mapping

from week9.src.modelica_templates import (
    RenderedTemplate,
    TemplateInputError,
    TemplateSpec,
    canonicalize_parameters,
    get_template_spec,
    relation_issues,
    simulation_plan,
    verify_rendered_template,
)
from week9.src.modelica_tool import CsvData, ModelicaExecution, read_simulation_csv
from week9.src.quality_gate import QualityGateDecision, decide
from week9.src.schemas import (
    CheckStatus,
    FailureKind,
    NormalizedParameter,
    ParameterStatus,
    QualityCheckResult,
    utc_now_iso,
)


@dataclass(frozen=True, slots=True)
class SysMLStructure:
    context_counts: Mapping[str, int]
    attribute_values: Mapping[str, tuple[float, ...]]
    part_usages: tuple[str, ...]
    part_definitions: Mapping[str, tuple["SysMLPartDefinitionStructure", ...]]


@dataclass(frozen=True, slots=True)
class SysMLPartDefinitionStructure:
    name: str
    attribute_values: Mapping[str, tuple[float, ...]]
    part_usages: tuple[str, ...]
    connections: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class HardGateResult:
    checks: tuple[QualityCheckResult, ...]
    decision: QualityGateDecision

    @property
    def overall_success(self) -> bool:
        return self.decision.overall_success


@dataclass(frozen=True, slots=True)
class PhysicalValidation:
    passed: bool
    issues: tuple[str, ...]
    evidence: Mapping[str, Any]


PhysicalValidator = Callable[
    [TemplateSpec, Mapping[str, float], CsvData], PhysicalValidation
]


CERTIFICATION_MANIFEST_PATH = (
    Path(__file__).resolve().parents[1] / "configs" / "template_certifications.json"
)
_CERTIFIED_IMPLEMENTATION_FILES = (
    "src/modelica_templates.py",
    "src/modelica_tool.py",
    "src/diagnostics.py",
    "src/hard_gate.py",
    "src/quality_gate.py",
    "src/schemas.py",
    "src/repair_loop.py",
    "src/parameter_normalization.py",
    "src/retrieval.py",
    "src/generation.py",
    "src/pipeline.py",
    "src/routing.py",
    "src/traceability.py",
    "src/runner.py",
    "configs/retrieval_corpus.json",
    "configs/retrieval_corpus.sha256",
    "configs/retrieval_policy.json",
    "experiments/certify_templates.py",
    "experiments/repair_regression.py",
    "experiments/run_retrieval_ablation.py",
    "experiments/report_adaptive_regression.py",
    "experiments/run_adaptive_regression.py",
    "experiments/run_natural_language_regression.py",
    "experiments/cases.py",
    "configs/stage4_adaptive_regression_suite.json",
    "configs/stage0_natural_language_regression_suite.json",
    "configs/reproducibility_manifest.template.json",
)
_RELEASE_MANIFEST_SCHEMA = "v8-template-certification-summary-1"
REQUIRED_HARD_CHECK_TYPES = (
    "requirement_normalization",
    "sysml_syntax",
    "certified_template_integrity",
    "template_certification_anchor",
    "execution_provenance",
    "modelica_compile",
    "parameter_unit_consistency",
    "simulation",
    "physical_behavior",
    "cross_model_consistency",
)
_REQUIRED_RELEASE_CHECKS = REQUIRED_HARD_CHECK_TYPES
_REQUIRED_CERTIFIED_TEMPLATE_IDS = frozenset(
    {
        "rc_filter",
        "rlc_filter",
        "opamp_inverting",
        "thermal_single_room",
        "thermal_dual_room",
    }
)
_REQUIRED_ERROR_INJECTIONS = {
    "missing_sysml": ("sysml_syntax", "UNVERIFIED", "content"),
    "missing_modelica": ("modelica_compile", "UNVERIFIED", "content"),
    "missing_csv": ("simulation", "UNVERIFIED", "content"),
    "modelica_syntax_error": ("modelica_compile", "FAIL", "content"),
    "wrong_component_path": ("modelica_compile", "FAIL", "content"),
    "port_mismatch": ("modelica_compile", "FAIL", "content"),
    "validator_exception": ("physical_behavior", "ERROR", "infrastructure"),
    "file_lock": ("modelica_compile", "ERROR", "infrastructure"),
}


class SysMLParserAdapter:
    """Pinned ANTLR syntax adapter with a fail-closed startup self-test."""

    def __init__(self, parser: Callable[[str], Any] | None = None) -> None:
        self._parser = parser
        self._syntax_error_type: type[BaseException] | tuple[type[BaseException], ...] = ()
        self._load_error: BaseException | None = None
        self._self_test_result: tuple[bool, str] | None = None
        try:
            self.version = importlib.metadata.version("sysmlpy")
        except importlib.metadata.PackageNotFoundError:
            self.version = "unavailable"
        if parser is None:
            try:
                from sysmlpy.antlr_parser import SysMLSyntaxError, parse

                self._parser = parse
                self._syntax_error_type = SysMLSyntaxError
            except (ImportError, AttributeError) as exc:
                self._load_error = exc
        else:
            self.version = "injected-test-adapter"
            self._syntax_error_type = Exception

    def inspect(self, source: str | None) -> tuple[QualityCheckResult, SysMLStructure | None]:
        started_at = utc_now_iso()
        started = time.monotonic()
        if source is None or not source.strip():
            return (
                _failure(
                    "sysml_syntax",
                    CheckStatus.UNVERIFIED,
                    FailureKind.CONTENT,
                    "sysml_artifact_missing",
                    "SysML 产物缺失或为空",
                    evidence={"reason": "sysml_artifact_missing"},
                    tool_name="sysmlpy ANTLR parser",
                    tool_version=self.version,
                    started_at=started_at,
                    duration_s=time.monotonic() - started,
                ),
                None,
            )
        if self._load_error is not None or self._parser is None:
            return (
                _failure(
                    "sysml_syntax",
                    CheckStatus.ERROR,
                    FailureKind.INFRASTRUCTURE,
                    "sysml_parser_unavailable",
                    f"sysmlpy ANTLR parser 不可用：{self._load_error}",
                    evidence={"reason": "sysml_parser_unavailable"},
                    tool_name="sysmlpy ANTLR parser",
                    tool_version=self.version,
                    started_at=started_at,
                    duration_s=time.monotonic() - started,
                ),
                None,
            )
        self_test_ok, self_test_issue = self._self_test()
        if not self_test_ok:
            return (
                _failure(
                    "sysml_syntax",
                    CheckStatus.ERROR,
                    FailureKind.INFRASTRUCTURE,
                    "sysml_parser_self_test_failed",
                    self_test_issue,
                    evidence={"reason": "sysml_parser_self_test_failed"},
                    tool_name="sysmlpy ANTLR parser",
                    tool_version=self.version,
                    started_at=started_at,
                    duration_s=time.monotonic() - started,
                ),
                None,
            )
        try:
            tree = self._parser(source)
        except self._syntax_error_type as exc:
            return (
                _failure(
                    "sysml_syntax",
                    CheckStatus.FAIL,
                    FailureKind.CONTENT,
                    "sysml_syntax_error",
                    str(exc)[:2000] or "SysML ANTLR 语法解析失败",
                    evidence={"reason": "sysml_syntax_error"},
                    tool_name="sysmlpy ANTLR parser",
                    tool_version=self.version,
                    started_at=started_at,
                    duration_s=time.monotonic() - started,
                ),
                None,
            )
        except Exception as exc:  # parser implementation failures are infrastructure
            return (
                _failure(
                    "sysml_syntax",
                    CheckStatus.ERROR,
                    FailureKind.INFRASTRUCTURE,
                    "sysml_parser_exception",
                    f"SysML parser 内部异常：{type(exc).__name__}: {exc}",
                    evidence={"reason": "sysml_parser_exception"},
                    tool_name="sysmlpy ANTLR parser",
                    tool_version=self.version,
                    started_at=started_at,
                    duration_s=time.monotonic() - started,
                ),
                None,
            )
        try:
            structure = _inspect_parse_tree(tree)
        except Exception as exc:
            return (
                _failure(
                    "sysml_syntax",
                    CheckStatus.ERROR,
                    FailureKind.INFRASTRUCTURE,
                    "sysml_tree_inspection_error",
                    f"SysML parse tree 检查异常：{type(exc).__name__}: {exc}",
                    evidence={"reason": "sysml_tree_inspection_error"},
                    tool_name="sysmlpy ANTLR parser",
                    tool_version=self.version,
                    started_at=started_at,
                    duration_s=time.monotonic() - started,
                ),
                None,
            )
        missing_structure = []
        if structure.context_counts.get("PackageContext", 0) < 1:
            missing_structure.append("package")
        if (
            structure.context_counts.get("PartDefinitionContext", 0)
            + structure.context_counts.get("PartUsageContext", 0)
            < 1
        ):
            missing_structure.append("part definition or usage")
        if missing_structure:
            return (
                _failure(
                    "sysml_syntax",
                    CheckStatus.FAIL,
                    FailureKind.CONTENT,
                    "sysml_minimum_structure_missing",
                    "SysML 缺少最小系统结构：" + ", ".join(missing_structure),
                    evidence={
                        "reason": "sysml_minimum_structure_missing",
                        "context_counts": dict(structure.context_counts),
                    },
                    tool_name="sysmlpy ANTLR parser",
                    tool_version=self.version,
                    started_at=started_at,
                    duration_s=time.monotonic() - started,
                ),
                structure,
            )
        return (
            QualityCheckResult(
                check_type="sysml_syntax",
                status=CheckStatus.PASS,
                required=True,
                evidence={
                    "parser_api": "sysmlpy.antlr_parser.parse",
                    "mode": "ANTLR_SYNTAX_PLUS_MINIMUM_STRUCTURE",
                    "self_test": "PASS",
                    "context_counts": dict(structure.context_counts),
                    "semantic_claim": "not_made",
                },
                tool_name="sysmlpy ANTLR parser",
                tool_version=self.version,
                started_at=started_at,
                duration_s=time.monotonic() - started,
            ),
            structure,
        )

    def _self_test(self) -> tuple[bool, str]:
        if self._self_test_result is not None:
            return self._self_test_result
        assert self._parser is not None
        try:
            self._parser("package V8Sentinel { part def ValidPart; }")
        except Exception as exc:
            self._self_test_result = (
                False,
                f"SysML parser 拒绝有效哨兵：{type(exc).__name__}: {exc}",
            )
            return self._self_test_result
        try:
            self._parser("package V8Sentinel { part def BrokenPart }")
        except Exception:
            self._self_test_result = (True, "")
            return self._self_test_result
        self._self_test_result = (False, "SysML parser 错误接受无分号哨兵")
        return self._self_test_result


def run_hard_gate(
    *,
    template_id: str,
    parameters: Iterable[NormalizedParameter],
    sysml_source: str | None,
    rendered: RenderedTemplate | None,
    execution: ModelicaExecution | None,
    sysml_parser: SysMLParserAdapter | None = None,
    physical_validator: PhysicalValidator | None = None,
    recertification_mode: bool = False,
) -> HardGateResult:
    """Run the complete stage-1 gate without any LLM vote or fallback pass."""

    parameter_list = tuple(parameters)
    spec = get_template_spec(template_id)
    requirement_check = check_requirement_normalization(parameter_list, spec)
    syntax_check, structure = (sysml_parser or SysMLParserAdapter()).inspect(sysml_source)
    integrity_check = check_certified_template_integrity(rendered, template_id)
    certification_check = check_certification_anchor(
        spec,
        execution,
        recertification_mode=recertification_mode,
    )
    provenance_check = check_execution_provenance(spec, rendered, execution)
    unit_check = check_parameter_unit_consistency(parameter_list, spec, rendered)
    if execution is None:
        compile_check = _failure(
            "modelica_compile",
            CheckStatus.UNVERIFIED,
            FailureKind.CONTENT,
            "modelica_execution_missing",
            "没有 Modelica 编译证据",
            evidence={"reason": "modelica_execution_missing"},
            tool_name="OpenModelica",
        )
        simulation_check = _failure(
            "simulation",
            CheckStatus.UNVERIFIED,
            FailureKind.CONTENT,
            "simulation_execution_missing",
            "没有仿真执行证据",
            evidence={"reason": "simulation_execution_missing"},
            tool_name="OpenModelica runtime",
        )
    else:
        compile_check = execution.compile_check
        simulation_check = execution.simulation_check
    physics_check = check_physical_behavior(
        spec,
        rendered,
        execution,
        validator=physical_validator,
    )
    cross_check = check_cross_model_consistency(
        spec,
        rendered,
        structure,
        syntax_check,
    )
    checks = (
        requirement_check,
        syntax_check,
        integrity_check,
        certification_check,
        provenance_check,
        compile_check,
        unit_check,
        simulation_check,
        physics_check,
        cross_check,
    )
    return HardGateResult(checks=checks, decision=decide(checks))


def check_requirement_normalization(
    parameters: Iterable[NormalizedParameter], spec: TemplateSpec
) -> QualityCheckResult:
    started_at = utc_now_iso()
    started = time.monotonic()
    items = tuple(parameters)
    issues: list[str] = []
    names = [item.canonical_name for item in items]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    issues.extend(f"重复规范参数：{name}" for name in duplicates)
    expected = set(spec.parameter_names)
    received = set(names)
    issues.extend(f"缺少规范参数：{name}" for name in sorted(expected - received))
    issues.extend(f"模板不接受规范参数：{name}" for name in sorted(received - expected))
    for item in items:
        if item.status is not ParameterStatus.KNOWN:
            issues.append(f"参数 {item.canonical_name} 状态为 {item.status.value}")
        if isinstance(item.value, bool) or not isinstance(item.value, (int, float)):
            issues.append(f"参数 {item.canonical_name} 不是实数")
        elif not math.isfinite(float(item.value)):
            issues.append(f"参数 {item.canonical_name} 不是有限值")
        if not item.source_text.strip():
            issues.append(f"参数 {item.canonical_name} 缺少来源文本")
    duration = time.monotonic() - started
    if issues:
        return _failure(
            "requirement_normalization",
            CheckStatus.FAIL,
            FailureKind.CONTENT,
            "requirement_parameter_incomplete",
            issues[0],
            extra_issues=issues[1:],
            evidence={"reason": "requirement_parameter_incomplete"},
            tool_name="V8 normalized parameter contract",
            tool_version="stage1",
            started_at=started_at,
            duration_s=duration,
        )
    return QualityCheckResult(
        check_type="requirement_normalization",
        status=CheckStatus.PASS,
        required=True,
        evidence={"canonical_parameters": sorted(received)},
        tool_name="V8 normalized parameter contract",
        tool_version="stage1",
        started_at=started_at,
        duration_s=duration,
    )


def check_parameter_unit_consistency(
    parameters: Iterable[NormalizedParameter],
    spec: TemplateSpec,
    rendered: RenderedTemplate | None,
) -> QualityCheckResult:
    started_at = utc_now_iso()
    started = time.monotonic()
    try:
        canonical = canonicalize_parameters(spec.template_id, parameters)
    except TemplateInputError as exc:
        return _failure(
            "parameter_unit_consistency",
            CheckStatus.FAIL,
            FailureKind.CONTENT,
            "parameter_or_unit_mismatch",
            exc.issues[0],
            extra_issues=exc.issues[1:],
            evidence={"reason": "parameter_or_unit_mismatch"},
            tool_name="V8 unit and relation validator",
            tool_version="stage1",
            started_at=started_at,
            duration_s=time.monotonic() - started,
        )
    if rendered is None:
        return _failure(
            "parameter_unit_consistency",
            CheckStatus.UNVERIFIED,
            FailureKind.CONTENT,
            "rendered_model_missing",
            "无法比较规范参数与 Modelica 参数：渲染产物缺失",
            evidence={"reason": "rendered_model_missing"},
            tool_name="V8 unit and relation validator",
            tool_version="stage1",
            started_at=started_at,
            duration_s=time.monotonic() - started,
        )
    canonical_map = {item.name: item.value for item in canonical}
    rendered_map = rendered.parameter_map
    mismatches = _numeric_mapping_mismatches(canonical_map, rendered_map)
    mismatches.extend(relation_issues(spec.template_id, rendered_map))
    if mismatches:
        return _failure(
            "parameter_unit_consistency",
            CheckStatus.FAIL,
            FailureKind.CONTENT,
            "rendered_parameter_mismatch",
            mismatches[0],
            extra_issues=mismatches[1:],
            evidence={"reason": "rendered_parameter_mismatch"},
            tool_name="V8 unit and relation validator",
            tool_version="stage1",
            started_at=started_at,
            duration_s=time.monotonic() - started,
        )
    return QualityCheckResult(
        check_type="parameter_unit_consistency",
        status=CheckStatus.PASS,
        required=True,
        evidence={
            "canonical_values": canonical_map,
            "canonical_units": {item.name: item.unit for item in canonical},
            "relation": spec.relation or "none",
        },
        tool_name="V8 unit and relation validator",
        tool_version="stage1",
        started_at=started_at,
        duration_s=time.monotonic() - started,
    )


def check_certified_template_integrity(
    rendered: RenderedTemplate | None, template_id: str
) -> QualityCheckResult:
    if rendered is None:
        return _failure(
            "certified_template_integrity",
            CheckStatus.UNVERIFIED,
            FailureKind.CONTENT,
            "rendered_model_missing",
            "认证模板渲染产物缺失",
            evidence={"reason": "rendered_model_missing"},
            tool_name="V8 slot-only template verifier",
            tool_version="stage1",
        )
    if rendered.template_id != template_id:
        return _failure(
            "certified_template_integrity",
            CheckStatus.FAIL,
            FailureKind.CONTENT,
            "template_identity_mismatch",
            f"渲染模板 {rendered.template_id} 与要求模板 {template_id} 不一致",
            evidence={"reason": "template_identity_mismatch"},
            tool_name="V8 slot-only template verifier",
            tool_version="stage1",
        )
    valid, issues = verify_rendered_template(rendered)
    if not valid:
        return _failure(
            "certified_template_integrity",
            CheckStatus.FAIL,
            FailureKind.CONTENT,
            "certified_template_modified",
            issues[0],
            extra_issues=issues[1:],
            evidence={"reason": "certified_template_modified"},
            tool_name="V8 slot-only template verifier",
            tool_version="stage1",
        )
    return QualityCheckResult(
        check_type="certified_template_integrity",
        status=CheckStatus.PASS,
        required=True,
        evidence={
            "template_id": rendered.template_id,
            "template_sha256": rendered.template_sha256,
            "rendered_sha256": rendered.rendered_sha256,
            "policy": "slot_only_no_unconstrained_full_rewrite",
        },
        tool_name="V8 slot-only template verifier",
        tool_version="stage1",
    )


def check_execution_provenance(
    spec: TemplateSpec,
    rendered: RenderedTemplate | None,
    execution: ModelicaExecution | None,
) -> QualityCheckResult:
    """Bind compiler/runtime evidence to this exact certified render and run."""

    if rendered is None or execution is None:
        return _failure(
            "execution_provenance",
            CheckStatus.UNVERIFIED,
            FailureKind.CONTENT,
            "execution_binding_missing",
            "缺少认证渲染或 Modelica 执行证据，无法绑定来源",
            evidence={"reason": "execution_binding_missing"},
            tool_name="V8 execution provenance validator",
            tool_version="stage1",
        )
    mismatches: list[str] = []
    missing: list[str] = []
    if execution.template_id != rendered.template_id:
        mismatches.append(
            f"execution.template_id={execution.template_id!r} 与 rendered.template_id={rendered.template_id!r} 不一致"
        )
    if execution.model_name != rendered.model_name:
        mismatches.append(
            f"execution.model_name={execution.model_name!r} 与 rendered.model_name={rendered.model_name!r} 不一致"
        )
    if execution.source_sha256 != rendered.rendered_sha256:
        mismatches.append("execution.source_sha256 与认证渲染哈希不一致")
    if execution.compile_check.check_type != "modelica_compile" or not execution.compile_check.required:
        mismatches.append("compile_check 类型或 required 标志非法")
    if execution.simulation_check.check_type != "simulation" or not execution.simulation_check.required:
        mismatches.append("simulation_check 类型或 required 标志非法")
    expected_plan = simulation_plan(spec.template_id, rendered.parameter_map)
    if not math.isclose(
        execution.simulation_start_time, expected_plan.start_time, rel_tol=0.0, abs_tol=1e-15
    ):
        mismatches.append("执行 start_time 与参数派生计划不一致")
    if not math.isclose(
        execution.simulation_stop_time, expected_plan.stop_time, rel_tol=1e-12, abs_tol=1e-15
    ):
        mismatches.append("执行 stop_time 与参数派生计划不一致")
    if execution.simulation_intervals != expected_plan.intervals:
        mismatches.append("执行 intervals 与参数派生计划不一致")

    try:
        run_dir = Path(execution.run_directory).resolve(strict=True)
    except (OSError, ValueError):
        run_dir = None
        missing.append("run_directory")
    source_path = Path(execution.source_path)
    if not source_path.is_file():
        missing.append("source_path")
    else:
        try:
            resolved_source = source_path.resolve(strict=True)
            if run_dir is None or resolved_source.parent != run_dir:
                mismatches.append("source_path 不在声明的唯一 run_directory 内")
            actual_source_hash = _file_sha256(resolved_source)
            if actual_source_hash != rendered.rendered_sha256:
                mismatches.append("磁盘 Modelica 源码哈希与认证渲染不一致")
            if execution.compile_check.evidence.get("source_sha256") != actual_source_hash:
                mismatches.append("编译检查 source_sha256 与磁盘源码不一致")
        except OSError as exc:
            return _failure(
                "execution_provenance",
                CheckStatus.ERROR,
                FailureKind.INFRASTRUCTURE,
                "execution_artifact_read_error",
                f"读取执行来源产物失败：{type(exc).__name__}: {exc}",
                evidence={"reason": "execution_artifact_read_error"},
                tool_name="V8 execution provenance validator",
                tool_version="stage1",
            )

    if execution.compile_check.status is CheckStatus.PASS:
        executable_path = Path(execution.executable_path)
        if not executable_path.is_file():
            missing.append("compiled_executable")
        else:
            resolved_executable = executable_path.resolve()
            if run_dir is None or resolved_executable.parent != run_dir:
                mismatches.append("compiled executable 不在声明的唯一 run_directory 内")
            actual_executable_hash = _file_sha256(resolved_executable)
            if (
                execution.compile_check.evidence.get("executable_sha256")
                != actual_executable_hash
            ):
                mismatches.append("编译检查 executable_sha256 与磁盘可执行文件不一致")

    rebound_csv: CsvData | None = None
    if execution.simulation_check.status is CheckStatus.PASS:
        csv_path = Path(execution.csv_path)
        if not csv_path.is_file():
            missing.append("simulation_csv")
        else:
            try:
                resolved_csv = csv_path.resolve(strict=True)
                if run_dir is None or resolved_csv.parent != run_dir:
                    mismatches.append("simulation CSV 不在声明的唯一 run_directory 内")
                actual_csv_hash = _file_sha256(resolved_csv)
                if execution.csv_sha256 != actual_csv_hash:
                    mismatches.append("execution.csv_sha256 与磁盘 CSV 不一致")
                if execution.simulation_check.evidence.get("csv_sha256") != actual_csv_hash:
                    mismatches.append("仿真检查 csv_sha256 与磁盘 CSV 不一致")
                rebound_csv = read_simulation_csv(resolved_csv, spec.output_variables)
            except Exception as exc:
                return _failure(
                    "execution_provenance",
                    CheckStatus.ERROR,
                    FailureKind.INFRASTRUCTURE,
                    "execution_csv_revalidation_error",
                    f"重验仿真 CSV 异常：{type(exc).__name__}: {exc}",
                    evidence={"reason": "execution_csv_revalidation_error"},
                    tool_name="V8 execution provenance validator",
                    tool_version="stage1",
                )
            if execution.csv_data != rebound_csv:
                mismatches.append("内存 CSV 数据与磁盘重验结果不一致")
            times = rebound_csv.columns["time"]
            tolerance = max(1e-12, expected_plan.stop_time * 1e-9)
            if (
                abs(times[0] - expected_plan.start_time) > tolerance
                or abs(times[-1] - expected_plan.stop_time) > tolerance
            ):
                mismatches.append("CSV 时间覆盖与参数派生仿真计划不一致")

    if mismatches:
        return _failure(
            "execution_provenance",
            CheckStatus.FAIL,
            FailureKind.CONTENT,
            "execution_evidence_mismatch",
            mismatches[0],
            extra_issues=mismatches[1:],
            evidence={"reason": "execution_evidence_mismatch"},
            tool_name="V8 execution provenance validator",
            tool_version="stage1",
        )
    if missing:
        return _failure(
            "execution_provenance",
            CheckStatus.UNVERIFIED,
            FailureKind.CONTENT,
            "execution_artifact_missing",
            "执行来源产物缺失：" + ", ".join(missing),
            evidence={"reason": "execution_artifact_missing"},
            tool_name="V8 execution provenance validator",
            tool_version="stage1",
        )
    return QualityCheckResult(
        check_type="execution_provenance",
        status=CheckStatus.PASS,
        required=True,
        evidence={
            "template_id": execution.template_id,
            "model_name": execution.model_name,
            "source_sha256": execution.source_sha256,
            "csv_sha256": execution.csv_sha256,
            "run_directory": str(run_dir),
            "simulation_plan": {
                "start_time": expected_plan.start_time,
                "stop_time": expected_plan.stop_time,
                "intervals": expected_plan.intervals,
            },
        },
        tool_name="V8 execution provenance validator",
        tool_version="stage1",
    )


def _release_manifest_issues(manifest: Mapping[str, Any]) -> list[str]:
    """Validate the promoted certificate as a self-consistent release record."""

    issues: list[str] = []
    if manifest.get("schema_version") != _RELEASE_MANIFEST_SCHEMA:
        issues.append("认证清单 schema 不是受支持的发布摘要版本")
    if tuple(manifest.get("required_checks", ())) != _REQUIRED_RELEASE_CHECKS:
        issues.append("认证清单的必需检查集合或顺序不完整")

    policy = manifest.get("policy")
    if not isinstance(policy, dict):
        issues.append("认证清单缺少发布策略")
    else:
        if policy.get("post_render_full_rewrite_allowed") is not False:
            issues.append("认证清单没有禁止渲染后的全文重写")
        if policy.get("required_check_release_rule") != "all required checks must be PASS":
            issues.append("认证清单没有声明必需检查全 PASS 的发布规则")

    environment = manifest.get("environment")
    if not isinstance(environment, dict):
        issues.append("认证清单缺少环境记录")
    elif environment.get("online_api_used") is not False:
        issues.append("认证清单没有证明离线执行")

    templates_value = manifest.get("templates")
    templates = templates_value if isinstance(templates_value, dict) else {}
    if set(templates) != _REQUIRED_CERTIFIED_TEMPLATE_IDS:
        issues.append("认证清单没有精确覆盖五个 Stage 1 模板")
    successful_templates = 0
    for template_id, certificate in templates.items():
        if not isinstance(certificate, dict):
            issues.append(f"模板 {template_id} 的认证记录格式无效")
            continue
        if certificate.get("full_gate") == "PASS":
            successful_templates += 1
        for field in ("compile", "simulation", "physical_behavior", "full_gate"):
            if certificate.get(field) != "PASS":
                issues.append(f"模板 {template_id} 的 {field} 不是 PASS")

    injections_value = manifest.get("error_injections")
    injections = injections_value if isinstance(injections_value, list) else []
    injection_by_id: dict[str, Mapping[str, Any]] = {}
    duplicate_injection_ids = False
    injection_records_valid = True
    for record in injections:
        if not isinstance(record, dict) or not isinstance(record.get("injection_id"), str):
            injection_records_valid = False
            continue
        injection_id = record["injection_id"]
        if injection_id in injection_by_id:
            duplicate_injection_ids = True
        injection_by_id[injection_id] = record
    if duplicate_injection_ids or set(injection_by_id) != set(_REQUIRED_ERROR_INJECTIONS):
        issues.append("认证清单没有精确覆盖八个冻结错误注入")
        injection_records_valid = False

    false_releases = 0
    for injection_id, expectation in _REQUIRED_ERROR_INJECTIONS.items():
        record = injection_by_id.get(injection_id)
        if record is None:
            injection_records_valid = False
            continue
        target_check, expected_status, expected_kind = expectation
        blocking_checks = record.get("blocking_checks")
        record_valid = (
            record.get("target_check") == target_check
            and record.get("expected_status") == expected_status
            and record.get("actual_status") == expected_status
            and record.get("expected_failure_kind") == expected_kind
            and record.get("actual_failure_kind") == expected_kind
            and record.get("expectation_matched") is True
            and record.get("overall_success") is False
            and isinstance(blocking_checks, list)
            and target_check in blocking_checks
        )
        if not record_valid:
            injection_records_valid = False
            issues.append(f"错误注入 {injection_id} 的阻断证据与冻结预期不一致")
        if record.get("overall_success") is True:
            false_releases += 1

    summary_value = manifest.get("summary")
    summary = summary_value if isinstance(summary_value, dict) else {}
    template_count = len(templates)
    failed_templates = template_count - successful_templates
    all_certified = (
        template_count == len(_REQUIRED_CERTIFIED_TEMPLATE_IDS)
        and failed_templates == 0
    )
    expectations_matched = (
        len(injections) == len(_REQUIRED_ERROR_INJECTIONS)
        and injection_records_valid
    )
    expected_summary = {
        "template_count": template_count,
        "successful_certifications": successful_templates,
        "failed_certifications": failed_templates,
        "all_certified": all_certified,
        "correct_sample_false_rejection_count": failed_templates,
        "correct_sample_count": template_count,
        "known_hard_error_false_release_count": false_releases,
        "known_hard_error_count": len(injections),
        "error_injection_expectations_matched": expectations_matched,
        "stage1_gate_passed": (
            all_certified and false_releases == 0 and expectations_matched
        ),
    }
    for field, expected in expected_summary.items():
        if summary.get(field) != expected:
            issues.append(f"认证清单 summary.{field} 与逐项结果不一致")
    if summary.get("stage1_gate_passed") is not True:
        issues.append("认证清单没有达到 Stage 1 发布门槛")
    return issues


def check_certification_anchor(
    spec: TemplateSpec,
    execution: ModelicaExecution | None,
    *,
    recertification_mode: bool = False,
) -> QualityCheckResult:
    """Require the current implementation and skeleton to match a real certificate."""

    current_hashes = _implementation_hashes()
    if recertification_mode:
        return QualityCheckResult(
            check_type="template_certification_anchor",
            status=CheckStatus.PASS,
            required=True,
            evidence={
                "mode": "recertification_candidate",
                "not_release_authorization": True,
                "template_sha256": spec.source_sha256,
                "implementation_sha256": current_hashes,
            },
            tool_name="V8 certification manifest verifier",
            tool_version="stage1",
        )
    try:
        manifest = json.loads(CERTIFICATION_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _failure(
            "template_certification_anchor",
            CheckStatus.ERROR,
            FailureKind.INFRASTRUCTURE,
            "certification_manifest_unavailable",
            f"无法读取模板认证清单：{type(exc).__name__}: {exc}",
            evidence={"reason": "certification_manifest_unavailable"},
            tool_name="V8 certification manifest verifier",
            tool_version="stage1",
        )
    if not isinstance(manifest, dict):
        return _failure(
            "template_certification_anchor",
            CheckStatus.UNVERIFIED,
            FailureKind.CONTENT,
            "template_recertification_required",
            "认证清单根节点不是 JSON object",
            evidence={"reason": "template_recertification_required"},
            tool_name="V8 certification manifest verifier",
            tool_version="stage1",
        )
    issues = _release_manifest_issues(manifest)
    certificate = manifest.get("templates", {}).get(spec.template_id)
    if not isinstance(certificate, dict):
        issues.append(f"认证清单缺少模板 {spec.template_id}")
        certificate = {}
    if certificate.get("template_sha256") != spec.source_sha256:
        issues.append("当前模板 skeleton 哈希与认证清单不一致")
    if certificate.get("full_gate") != "PASS":
        issues.append("认证清单中的 full_gate 不是 PASS")
    if manifest.get("implementation_sha256") != current_hashes:
        issues.append("当前门禁/编译器包装实现哈希与认证清单不一致")
    if execution is not None and execution.compile_check.status is CheckStatus.PASS:
        certified_environment = manifest.get("environment", {})
        if execution.compile_check.tool_version != certified_environment.get("openmodelica"):
            issues.append("当前 OpenModelica 版本与认证环境不一致")
        if (
            execution.compile_check.evidence.get("modelica_library_version")
            != certified_environment.get("modelica_standard_library")
        ):
            issues.append("当前 Modelica Standard Library 版本与认证环境不一致")
    if issues:
        return _failure(
            "template_certification_anchor",
            CheckStatus.UNVERIFIED,
            FailureKind.CONTENT,
            "template_recertification_required",
            issues[0],
            extra_issues=issues[1:],
            evidence={
                "reason": "template_recertification_required",
                "template_id": spec.template_id,
                "current_template_sha256": spec.source_sha256,
                "current_implementation_sha256": current_hashes,
            },
            tool_name="V8 certification manifest verifier",
            tool_version="stage1",
        )
    return QualityCheckResult(
        check_type="template_certification_anchor",
        status=CheckStatus.PASS,
        required=True,
        evidence={
            "manifest_path": str(CERTIFICATION_MANIFEST_PATH),
            "certified_at": manifest.get("certified_at", ""),
            "template_sha256": spec.source_sha256,
            "implementation_sha256": current_hashes,
        },
        tool_name="V8 certification manifest verifier",
        tool_version="stage1",
    )


def check_cross_model_consistency(
    spec: TemplateSpec,
    rendered: RenderedTemplate | None,
    structure: SysMLStructure | None,
    syntax_check: QualityCheckResult,
) -> QualityCheckResult:
    if syntax_check.status is CheckStatus.ERROR:
        return _failure(
            "cross_model_consistency",
            CheckStatus.ERROR,
            FailureKind.INFRASTRUCTURE,
            "sysml_parser_prerequisite_error",
            "跨模型检查无法运行：SysML parser 异常",
            evidence={"reason": "sysml_parser_prerequisite_error"},
            tool_name="V8 parse-tree cross-model validator",
            tool_version="stage1",
        )
    if syntax_check.status is not CheckStatus.PASS or structure is None or rendered is None:
        return _failure(
            "cross_model_consistency",
            CheckStatus.UNVERIFIED,
            FailureKind.CONTENT,
            "cross_model_artifact_unavailable",
            "跨模型检查缺少已解析 SysML 或认证 Modelica 产物",
            evidence={"reason": "cross_model_artifact_unavailable"},
            tool_name="V8 parse-tree cross-model validator",
            tool_version="stage1",
        )
    systems = structure.part_definitions.get("System", ())
    if len(systems) != 1:
        return _failure(
            "cross_model_consistency",
            CheckStatus.UNVERIFIED if not systems else CheckStatus.FAIL,
            FailureKind.CONTENT,
            "sysml_system_scope_ambiguous",
            f"跨模型 profile 要求恰好一个 part def System，实际为 {len(systems)}",
            evidence={"reason": "sysml_system_scope_ambiguous"},
            tool_name="V8 scoped parse-tree cross-model validator",
            tool_version="stage1",
        )
    system = systems[0]
    missing_values: list[str] = []
    mismatches: list[str] = []
    for name, expected in rendered.parameter_map.items():
        observed = system.attribute_values.get(name, ())
        if not observed:
            missing_values.append(name)
            continue
        if any(not math.isclose(value, expected, rel_tol=1e-9, abs_tol=1e-12) for value in observed):
            mismatches.append(
                f"SysML 参数 {name}={list(observed)} 与 Modelica SI 值 {expected:.12g} 不一致"
            )
    if missing_values:
        return _failure(
            "cross_model_consistency",
            CheckStatus.UNVERIFIED,
            FailureKind.CONTENT,
            "sysml_parameter_not_extractable",
            "SysML 缺少可从 parse tree 提取的规范参数：" + ", ".join(missing_values),
            evidence={"reason": "sysml_parameter_not_extractable"},
            tool_name="V8 parse-tree cross-model validator",
            tool_version="stage1",
        )
    expected_parts = set(spec.sysml_components)
    observed_parts = set(system.part_usages)
    if expected_parts != observed_parts:
        missing_parts = sorted(expected_parts - observed_parts)
        extra_parts = sorted(observed_parts - expected_parts)
        mismatches.append(
            "SysML System 部件集合不一致："
            f"missing={missing_parts}, extra={extra_parts}"
        )
    expected_connections = {
        tuple(sorted(connection)) for connection in spec.sysml_connections
    }
    observed_connections = {
        tuple(sorted(connection)) for connection in system.connections
    }
    if expected_connections != observed_connections:
        mismatches.append(
            "SysML System 关键连接集合不一致："
            f"missing={sorted(expected_connections - observed_connections)}, "
            f"extra={sorted(observed_connections - expected_connections)}"
        )
    if mismatches:
        return _failure(
            "cross_model_consistency",
            CheckStatus.FAIL,
            FailureKind.CONTENT,
            "cross_model_mismatch",
            mismatches[0],
            extra_issues=mismatches[1:],
            evidence={"reason": "cross_model_mismatch"},
            tool_name="V8 scoped parse-tree cross-model validator",
            tool_version="stage1",
        )
    return QualityCheckResult(
        check_type="cross_model_consistency",
        status=CheckStatus.PASS,
        required=True,
        evidence={
            "scope": "part def System: canonical SI values + exact components + key connections",
            "matched_parameters": sorted(rendered.parameter_map),
            "matched_components": sorted(spec.sysml_components),
            "matched_connections": [
                list(connection) for connection in sorted(expected_connections)
            ],
            "semantic_claim": "not_made",
        },
        tool_name="V8 scoped parse-tree cross-model validator",
        tool_version="stage1",
    )


def check_physical_behavior(
    spec: TemplateSpec,
    rendered: RenderedTemplate | None,
    execution: ModelicaExecution | None,
    *,
    validator: PhysicalValidator | None = None,
) -> QualityCheckResult:
    if rendered is None or execution is None:
        return _failure(
            "physical_behavior",
            CheckStatus.UNVERIFIED,
            FailureKind.CONTENT,
            "physical_evidence_missing",
            "缺少认证参数或仿真产物，无法执行物理检查",
            evidence={"reason": "physical_evidence_missing"},
            tool_name="V8 deterministic physics validator",
            tool_version="stage1",
        )
    simulation_check = execution.simulation_check
    if simulation_check.status is CheckStatus.ERROR:
        return _failure(
            "physical_behavior",
            CheckStatus.ERROR,
            FailureKind.INFRASTRUCTURE,
            "simulation_prerequisite_error",
            "物理检查无法运行：仿真工具异常",
            evidence={"reason": "simulation_prerequisite_error"},
            tool_name="V8 deterministic physics validator",
            tool_version="stage1",
        )
    if simulation_check.status is not CheckStatus.PASS or execution.csv_data is None:
        return _failure(
            "physical_behavior",
            CheckStatus.UNVERIFIED,
            FailureKind.CONTENT,
            "simulation_evidence_unavailable",
            f"物理检查无法运行：simulation={simulation_check.status.value}",
            evidence={"reason": "simulation_evidence_unavailable"},
            tool_name="V8 deterministic physics validator",
            tool_version="stage1",
        )
    selected_validator = validator or _PHYSICAL_VALIDATORS[spec.template_id]
    started_at = utc_now_iso()
    started = time.monotonic()
    try:
        result = selected_validator(spec, rendered.parameter_map, execution.csv_data)
    except Exception as exc:
        return _failure(
            "physical_behavior",
            CheckStatus.ERROR,
            FailureKind.INFRASTRUCTURE,
            "physical_validator_exception",
            f"物理验证器异常：{type(exc).__name__}: {exc}",
            evidence={"reason": "physical_validator_exception"},
            tool_name="V8 deterministic physics validator",
            tool_version="stage1",
            started_at=started_at,
            duration_s=time.monotonic() - started,
        )
    if not result.passed:
        issues = result.issues or ("物理行为断言失败",)
        return _failure(
            "physical_behavior",
            CheckStatus.FAIL,
            FailureKind.CONTENT,
            "physical_assertion_failed",
            issues[0],
            extra_issues=issues[1:],
            evidence={**dict(result.evidence), "reason": "physical_assertion_failed"},
            tool_name="V8 deterministic physics validator",
            tool_version="stage1",
            started_at=started_at,
            duration_s=time.monotonic() - started,
        )
    return QualityCheckResult(
        check_type="physical_behavior",
        status=CheckStatus.PASS,
        required=True,
        evidence=dict(result.evidence),
        tool_name="V8 deterministic physics validator",
        tool_version="stage1",
        started_at=started_at,
        duration_s=time.monotonic() - started,
    )


def _validate_rc(
    spec: TemplateSpec, parameters: Mapping[str, float], data: CsvData
) -> PhysicalValidation:
    times = data.columns["time"]
    outputs = data.columns["output_voltage"]
    expected_tau = parameters["resistance"] * parameters["capacitance"]
    target = parameters["input_voltage"]
    crossing_target = target * (1.0 - math.exp(-1.0))
    tau_observed = _first_crossing_time(times, outputs, crossing_target, rising=target >= 0)
    issues: list[str] = []
    initial_tolerance = max(abs(target) * 1e-6, 1e-9)
    if abs(outputs[0]) > initial_tolerance:
        issues.append(f"RC 初始电容电压应为 0 V，观测为 {outputs[0]:.6g} V")
    if tau_observed is None:
        issues.append("RC 输出未达到 63.2% 阶跃响应点")
        tau_error = None
    else:
        tau_error = abs(tau_observed - expected_tau) / expected_tau
        if tau_error > 0.05:
            issues.append(f"RC 时间常数相对误差 {tau_error:.3%} 超过 5%")
    final_error = abs(outputs[-1] - target) / max(abs(target), 1e-12)
    if final_error > 0.02:
        issues.append(f"RC 最终输出相对误差 {final_error:.3%} 超过 2%")
    lower, upper = sorted((0.0, target))
    span_tolerance = max(abs(target) * 0.01, 1e-9)
    if any(value < lower - span_tolerance or value > upper + span_tolerance for value in outputs):
        issues.append("RC 阶跃响应越过被动一阶网络边界")
    if target >= 0:
        if any(after + span_tolerance * 1e-4 < before for before, after in zip(outputs, outputs[1:])):
            issues.append("RC 正阶跃响应不是单调上升")
    elif any(after - span_tolerance * 1e-4 > before for before, after in zip(outputs, outputs[1:])):
        issues.append("RC 负阶跃响应不是单调下降")
    return PhysicalValidation(
        passed=not issues,
        issues=tuple(issues),
        evidence={
            "assertion": "RC step response and tau=R*C",
            "expected_tau_s": expected_tau,
            "observed_tau_s": tau_observed,
            "tau_relative_error": tau_error,
            "final_output_v": outputs[-1],
            "target_output_v": target,
            "final_relative_error": final_error,
        },
    )


def _validate_rlc(
    spec: TemplateSpec, parameters: Mapping[str, float], data: CsvData
) -> PhysicalValidation:
    times = data.columns["time"]
    inputs = data.columns["input_signal"]
    outputs = data.columns["output_voltage"]
    expected_frequency = parameters["cutoff_frequency"]
    crossings = _upward_zero_crossings(times, inputs)
    issues: list[str] = []
    if len(crossings) < 3:
        # Unavailable observations are explicit JSON nulls.  NaN/Infinity
        # would make valid failure evidence impossible to serialize under the
        # strict RunResult contract.
        observed_frequency = None
        frequency_error = None
        issues.append("RLC 输入信号没有足够的正向零交叉，不能证明正弦激励")
    else:
        periods = [right - left for left, right in zip(crossings, crossings[1:])]
        observed_frequency = 1.0 / (sum(periods) / len(periods))
        frequency_error = abs(observed_frequency - expected_frequency) / expected_frequency
        if frequency_error > 0.02:
            issues.append(f"RLC 正弦激励频率相对误差 {frequency_error:.3%} 超过 2%")
    start = len(outputs) * 2 // 3
    input_amplitude = _half_peak_to_peak(inputs[start:])
    output_amplitude = _half_peak_to_peak(outputs[start:])
    omega = 2.0 * math.pi * expected_frequency
    detuning = omega * parameters["inductance"] - 1.0 / (
        omega * parameters["capacitance"]
    )
    expected_gain = 1.0 / (
        omega
        * parameters["capacitance"]
        * math.sqrt(parameters["resistance"] ** 2 + detuning**2)
    )
    observed_gain = output_amplitude / max(input_amplitude, 1e-30)
    gain_error = abs(observed_gain - expected_gain) / max(abs(expected_gain), 1e-30)
    if input_amplitude < abs(parameters["input_voltage"]) * 0.95:
        issues.append("RLC 输入正弦幅值不足")
    if gain_error > 0.15:
        issues.append(f"RLC 稳态增益相对误差 {gain_error:.3%} 超过 15%")
    return PhysicalValidation(
        passed=not issues,
        issues=tuple(issues),
        evidence={
            "assertion": "sine stimulus frequency and series-RLC capacitor gain",
            "expected_frequency_hz": expected_frequency,
            "observed_frequency_hz": observed_frequency,
            "frequency_relative_error": frequency_error,
            "expected_gain": expected_gain,
            "observed_gain": observed_gain,
            "gain_relative_error": gain_error,
        },
    )


def _validate_opamp(
    spec: TemplateSpec, parameters: Mapping[str, float], data: CsvData
) -> PhysicalValidation:
    outputs = data.columns["output_voltage"]
    ideal = -parameters["input_voltage"] * parameters["feedback_resistance"] / parameters[
        "input_resistance"
    ]
    expected = min(parameters["positive_supply"], max(parameters["negative_supply"], ideal))
    tail = outputs[max(0, len(outputs) * 9 // 10) :]
    observed = sum(tail) / len(tail)
    tolerance = max(0.05, 0.01 * (parameters["positive_supply"] - parameters["negative_supply"]))
    issues: list[str] = []
    if abs(observed - expected) > tolerance:
        issues.append(
            f"反相运放输出 {observed:.6g} V 与期望限幅输出 {expected:.6g} V 偏差过大"
        )
    if any(
        value < parameters["negative_supply"] - tolerance
        or value > parameters["positive_supply"] + tolerance
        for value in outputs
    ):
        issues.append("运放输出越过电源轨")
    return PhysicalValidation(
        passed=not issues,
        issues=tuple(issues),
        evidence={
            "assertion": "inverting gain with supply clipping",
            "ideal_output_v": ideal,
            "expected_clipped_output_v": expected,
            "observed_output_v": observed,
            "absolute_tolerance_v": tolerance,
        },
    )


def _validate_single_thermal(
    spec: TemplateSpec, parameters: Mapping[str, float], data: CsvData
) -> PhysicalValidation:
    times = data.columns["time"]
    temperatures = data.columns["room_temperature"]
    initial = parameters["initial_temperature"]
    outdoor = parameters["outdoor_temperature"]
    tau = parameters["heat_capacity"] * parameters["thermal_resistance"]
    expected_final = outdoor + (initial - outdoor) * math.exp(-times[-1] / tau)
    absolute_error = abs(temperatures[-1] - expected_final)
    issues = _thermal_series_issues(temperatures, (initial,), outdoor)
    if abs(temperatures[0] - initial) > 0.01:
        issues.append(
            f"单房间初始温度 {temperatures[0]:.6g} K 与声明 {initial:.6g} K 不一致"
        )
    if absolute_error > 0.05:
        issues.append(f"单房间解析终值误差 {absolute_error:.6g} K 超过 0.05 K")
    return PhysicalValidation(
        passed=not issues,
        issues=tuple(issues),
        evidence={
            "assertion": "first-order thermal response and boundary envelope",
            "tau_s": tau,
            "expected_final_k": expected_final,
            "observed_final_k": temperatures[-1],
            "absolute_error_k": absolute_error,
        },
    )


def _validate_dual_thermal(
    spec: TemplateSpec, parameters: Mapping[str, float], data: CsvData
) -> PhysicalValidation:
    room_a = data.columns["room_a_temperature"]
    room_b = data.columns["room_b_temperature"]
    outdoor = parameters["outdoor_temperature"]
    initials = (parameters["initial_temperature_a"], parameters["initial_temperature_b"])
    issues = _thermal_series_issues(room_a, initials, outdoor)
    issues.extend(_thermal_series_issues(room_b, initials, outdoor))
    if abs(room_a[0] - initials[0]) > 0.01:
        issues.append("房间 A 仿真初值与声明初始温度不一致")
    if abs(room_b[0] - initials[1]) > 0.01:
        issues.append("房间 B 仿真初值与声明初始温度不一致")
    final_error_a = abs(room_a[-1] - outdoor)
    final_error_b = abs(room_b[-1] - outdoor)
    if final_error_a > 0.1 or final_error_b > 0.1:
        issues.append(
            f"双房间长期温度未接近室外边界：A={final_error_a:.6g} K, B={final_error_b:.6g} K"
        )
    return PhysicalValidation(
        passed=not issues,
        issues=tuple(dict.fromkeys(issues)),
        evidence={
            "assertion": "two-room passive bounds and common steady-state boundary",
            "outdoor_temperature_k": outdoor,
            "room_a_final_k": room_a[-1],
            "room_b_final_k": room_b[-1],
            "room_a_final_error_k": final_error_a,
            "room_b_final_error_k": final_error_b,
        },
    )


_PHYSICAL_VALIDATORS: Mapping[str, PhysicalValidator] = {
    "rc_filter": _validate_rc,
    "rlc_filter": _validate_rlc,
    "opamp_inverting": _validate_opamp,
    "thermal_single_room": _validate_single_thermal,
    "thermal_dual_room": _validate_dual_thermal,
}


def _inspect_parse_tree(tree: Any) -> SysMLStructure:
    context_counts: dict[str, int] = {}
    attribute_values: dict[str, list[float]] = {}
    part_usages: list[str] = []
    part_definitions: dict[str, list[SysMLPartDefinitionStructure]] = {}
    stack = [tree]
    while stack:
        node = stack.pop()
        class_name = type(node).__name__
        context_counts[class_name] = context_counts.get(class_name, 0) + 1
        if class_name == "AttributeUsageContext":
            match = re.search(
                r"attribute(?:redefines)?([A-Za-z_][A-Za-z0-9_]*)(?::[^=;]+)?="
                r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*;",
                node.getText(),
            )
            if match:
                attribute_values.setdefault(match.group(1), []).append(float(match.group(2)))
        elif class_name == "PartUsageContext":
            match = re.match(
                r"part(?:redefines)?([A-Za-z_][A-Za-z0-9_]*)",
                node.getText(),
            )
            if match:
                part_usages.append(match.group(1))
        elif class_name == "PartDefinitionContext":
            definition = _inspect_part_definition(node)
            if definition is not None:
                part_definitions.setdefault(definition.name, []).append(definition)
        child_count = node.getChildCount()
        stack.extend(node.getChild(index) for index in range(child_count))
    return SysMLStructure(
        context_counts=context_counts,
        attribute_values={name: tuple(values) for name, values in attribute_values.items()},
        part_usages=tuple(part_usages),
        part_definitions={
            name: tuple(definitions) for name, definitions in part_definitions.items()
        },
    )


def _inspect_part_definition(node: Any) -> SysMLPartDefinitionStructure | None:
    name_match = re.match(r"partdef([A-Za-z_][A-Za-z0-9_]*)", node.getText())
    if not name_match:
        return None
    attributes: dict[str, list[float]] = {}
    parts: list[str] = []
    connections: list[tuple[str, str]] = []
    stack = [node]
    while stack:
        current = stack.pop()
        class_name = type(current).__name__
        text = current.getText()
        if current is not node and class_name == "PartDefinitionContext":
            continue
        if class_name == "AttributeUsageContext":
            match = re.search(
                r"attribute(?:redefines)?([A-Za-z_][A-Za-z0-9_]*)(?::[^=;]+)?="
                r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*;",
                text,
            )
            if match:
                attributes.setdefault(match.group(1), []).append(float(match.group(2)))
        elif class_name == "PartUsageContext":
            match = re.match(r"part(?:redefines)?([A-Za-z_][A-Za-z0-9_]*)", text)
            if match:
                parts.append(match.group(1))
        elif class_name == "ConnectionUsageContext":
            ends = _connection_end_names(current)
            if len(ends) == 2:
                connections.append((ends[0], ends[1]))
        stack.extend(current.getChild(index) for index in range(current.getChildCount()))
    return SysMLPartDefinitionStructure(
        name=name_match.group(1),
        attribute_values={name: tuple(values) for name, values in attributes.items()},
        part_usages=tuple(parts),
        connections=tuple(connections),
    )


def _connection_end_names(connection_node: Any) -> list[str]:
    ends: list[str] = []
    stack = [connection_node]
    while stack:
        current = stack.pop()
        if current is not connection_node and type(current).__name__ == "ConnectorEndContext":
            text = current.getText()
            match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", text)
            if match:
                ends.append(match.group(1))
            continue
        stack.extend(
            current.getChild(index) for index in reversed(range(current.getChildCount()))
        )
    return ends


def _numeric_mapping_mismatches(
    expected: Mapping[str, float], observed: Mapping[str, float]
) -> list[str]:
    issues: list[str] = []
    if set(expected) != set(observed):
        issues.append(
            f"参数键不一致：expected={sorted(expected)}, observed={sorted(observed)}"
        )
    for name in sorted(set(expected) & set(observed)):
        if not math.isclose(expected[name], observed[name], rel_tol=1e-12, abs_tol=1e-15):
            issues.append(
                f"参数 {name} 不一致：expected={expected[name]:.12g}, observed={observed[name]:.12g}"
            )
    return issues


def _first_crossing_time(
    times: tuple[float, ...],
    values: tuple[float, ...],
    target: float,
    *,
    rising: bool,
) -> float | None:
    for index in range(1, len(values)):
        before, after = values[index - 1], values[index]
        crossed = before <= target <= after if rising else before >= target >= after
        if crossed:
            if after == before:
                return times[index]
            fraction = (target - before) / (after - before)
            return times[index - 1] + fraction * (times[index] - times[index - 1])
    return None


def _upward_zero_crossings(
    times: tuple[float, ...], values: tuple[float, ...]
) -> list[float]:
    crossings: list[float] = []
    for index in range(1, len(values)):
        before, after = values[index - 1], values[index]
        if before <= 0.0 < after:
            fraction = -before / (after - before) if after != before else 0.0
            crossings.append(times[index - 1] + fraction * (times[index] - times[index - 1]))
    return crossings


def _half_peak_to_peak(values: tuple[float, ...]) -> float:
    return (max(values) - min(values)) / 2.0


def _thermal_series_issues(
    values: tuple[float, ...], initial_values: tuple[float, ...], outdoor: float
) -> list[str]:
    lower = min((*initial_values, outdoor)) - 1e-6
    upper = max((*initial_values, outdoor)) + 1e-6
    issues: list[str] = []
    if any(value < lower or value > upper for value in values):
        issues.append("被动热网络温度越过初始值/边界温度包络")
    if all(initial <= outdoor for initial in initial_values):
        if any(after + 1e-7 < before for before, after in zip(values, values[1:])):
            issues.append("升温场景出现非单调降温")
    elif all(initial >= outdoor for initial in initial_values):
        if any(after - 1e-7 > before for before, after in zip(values, values[1:])):
            issues.append("降温场景出现非单调升温")
    return issues


def _failure(
    check_type: str,
    status: CheckStatus,
    kind: FailureKind,
    category: str,
    issue: str,
    *,
    extra_issues: Iterable[str] = (),
    evidence: Mapping[str, Any] | None = None,
    tool_name: str,
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
        tool_name=tool_name,
        tool_version=tool_version,
        started_at=started_at or utc_now_iso(),
        duration_s=duration_s,
        failure_kind=kind,
        failure_category=category,
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _implementation_hashes() -> dict[str, str]:
    week9_root = Path(__file__).resolve().parents[1]
    return {
        relative: _file_sha256(week9_root / relative)
        for relative in _CERTIFIED_IMPLEMENTATION_FILES
    }
