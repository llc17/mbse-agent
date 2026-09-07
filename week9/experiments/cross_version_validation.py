"""Deterministic, version-external validation for comparison artifacts.

The comparison must not trust V4/V6/V8 internal ``success`` flags.  This
module recompiles the submitted Modelica source in a fresh directory and
applies the same requirement, syntax, parameter, simulation, physical, and
cross-model checks to a final artifact bundle.

The validator is intentionally limited to the frozen development topologies.
Unknown topologies are ``UNVERIFIED`` instead of being guessed.
"""

from __future__ import annotations

import ast
import csv
from dataclasses import replace
import math
from pathlib import Path
import re
import time
from typing import Any, Iterable, Mapping

from week9.experiments.cases import CaseDefinition
from week9.experiments.external_evaluator import EvaluationContext, evaluate
from week9.src.generation import DeterministicNaturalLanguageProvider
from week9.src.hard_gate import SysMLParserAdapter, check_requirement_normalization
from week9.src.modelica_templates import TemplateSpec, get_template_spec
from week9.src.modelica_tool import CsvData, OpenModelicaTool, read_simulation_csv
from week9.src.parameter_normalization import normalize_parameters
from week9.src.schemas import (
    ArtifactRecord,
    CheckStatus,
    FailureKind,
    NormalizedParameter,
    QualityCheckResult,
    RepairStopReason,
    ReproductionManifest,
    RunCounters,
    RunResult,
)


_MODEL_DECLARATION = re.compile(
    r"(?m)^\s*(?:partial\s+)?model\s+([A-Za-z_][A-Za-z0-9_]*)\b"
)
_CONNECT = re.compile(
    r"\bconnect\s*\(\s*([A-Za-z_]\w*)(?:\.[^,()]*)?\s*,\s*"
    r"([A-Za-z_]\w*)(?:\.[^,()]*)?\s*\)\s*;",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"(?<![A-Za-z_])[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")


def externally_validate(
    candidate: RunResult,
    case: CaseDefinition,
    *,
    workspace_root: str | Path,
    modelica_tool: OpenModelicaTool | None = None,
) -> RunResult:
    """Return a new RunResult whose decision comes only from external checks."""

    if candidate.case_id != case.case_id:
        raise ValueError("candidate and case ids differ")
    if candidate.requirement_text != case.raw_input:
        raise ValueError("candidate requirement differs from the frozen case")

    started = time.monotonic()
    generation = DeterministicNaturalLanguageProvider().generate(case.raw_input)
    if not generation.ready:
        return _unsupported_requirement_result(
            candidate,
            case,
            generation_issue_codes=tuple(item.code for item in generation.issues),
            started=started,
        )
    template_id = generation.template_id
    parameters = list(
        normalize_parameters(template_id, generation.observations).parameters
    )
    spec = get_template_spec(template_id)
    sysml_source = _artifact_content(candidate, "sysml")
    modelica_source = _artifact_content(candidate, "modelica")

    requirement_check = check_requirement_normalization(parameters, spec)
    sysml_check, _ = SysMLParserAdapter().inspect(sysml_source or None)
    model_name = _extract_model_name(modelica_source)
    execution_spec = replace(spec, output_variables=("time",))
    tool = modelica_tool or OpenModelicaTool()
    execution = None
    if not modelica_source.strip():
        compile_check = _result(
            "modelica_compile",
            CheckStatus.UNVERIFIED,
            "modelica_artifact_missing",
            "Modelica 产物缺失或为空",
        )
        simulation_check = _result(
            "simulation",
            CheckStatus.UNVERIFIED,
            "modelica_compile_not_passed",
            "Modelica 未编译，无法仿真",
        )
    elif not model_name:
        compile_check = _result(
            "modelica_compile",
            CheckStatus.FAIL,
            "model_declaration_missing",
            "找不到可执行的顶层 Modelica model 声明",
        )
        simulation_check = _result(
            "simulation",
            CheckStatus.UNVERIFIED,
            "modelica_compile_not_passed",
            "Modelica 未编译，无法仿真",
        )
    else:
        execution = tool.execute_source(
            modelica_source,
            model_name=model_name,
            template_spec=execution_spec,
            workspace_root=workspace_root,
            parameter_values={item.canonical_name: float(item.value) for item in parameters},
        )
        compile_check = execution.compile_check
        simulation_check = execution.simulation_check

    observed, extraction_issues = _extract_parameters(template_id, modelica_source)
    parameter_check = _parameter_check(parameters, observed, extraction_issues)
    topology_ok, topology_issues, topology_evidence = _topology_check(
        template_id, modelica_source
    )
    physical_csv = None
    if execution is not None and execution.csv_path:
        try:
            physical_csv = _read_all_csv(execution.csv_path)
        except (OSError, ValueError):
            physical_csv = None
    physical_check = _physical_check(
        template_id=template_id,
        expected=parameters,
        observed=observed,
        csv_data=physical_csv,
        topology_ok=topology_ok,
        topology_issues=topology_issues,
    )
    cross_check = _cross_model_check(
        parameters=parameters,
        sysml_source=sysml_source,
        sysml_status=sysml_check.status,
        topology_ok=topology_ok,
        topology_issues=topology_issues,
        topology_evidence=topology_evidence,
    )
    adapter_execution = _adapter_execution_check(candidate)
    external_checks = (
        adapter_execution,
        requirement_check,
        sysml_check,
        compile_check,
        parameter_check,
        simulation_check,
        physical_check,
        cross_check,
    )
    infrastructure_errors = list(candidate.infrastructure_errors)
    infrastructure_errors.extend(
        issue
        for check in external_checks
        if check.failure_kind is FailureKind.INFRASTRUCTURE
        for issue in check.issues
    )

    artifacts = {
        "requirement": ArtifactRecord(
            "requirement", content=case.raw_input, media_type="text/plain"
        ),
        "sysml": ArtifactRecord(
            "sysml", content=sysml_source, media_type="text/x-sysml"
        ),
        "modelica": ArtifactRecord(
            "modelica", content=modelica_source, media_type="text/x-modelica"
        ),
        "simulation_csv": ArtifactRecord(
            "simulation_csv",
            path=execution.csv_path if execution is not None else "",
            media_type="text/csv",
        ),
    }
    raw_metrics = {
        "external_evaluator": "week9.cross_version_validation.v1",
        "candidate_internal_overall_success": candidate.overall_success,
        "candidate_raw_metrics": dict(candidate.raw_metrics),
        "template_id": template_id,
        "external_model_name": model_name,
        "external_validation_duration_s": time.monotonic() - started,
        "external_execution_directory": (
            execution.run_directory if execution is not None else ""
        ),
        "external_parameter_observations": observed,
    }
    result = evaluate(
        context=EvaluationContext(
            version=candidate.version,
            case_id=case.case_id,
            run_id=candidate.run_id,
            requirement_text=case.raw_input,
            artifacts=artifacts,
            metadata={"case_sha256": case.sha256, "template_id": template_id},
        ),
        required_check_types=case.required_checks,
        parameters=parameters,
        precomputed_external_checks=external_checks,
        repairs=candidate.repairs,
        infrastructure_retries=candidate.infrastructure_retries,
        counters=RunCounters(
            generation_count=candidate.counters.generation_count,
            validation_count=len(external_checks),
            content_repair_count=candidate.counters.content_repair_count,
            accepted_content_repair_count=candidate.counters.accepted_content_repair_count,
            repair_attempt_count=candidate.counters.repair_attempt_count,
            upstream_redo_count=candidate.counters.upstream_redo_count,
            infrastructure_retry_count=candidate.counters.infrastructure_retry_count,
            no_op_count=candidate.counters.no_op_count,
            repeated_error_count=candidate.counters.repeated_error_count,
            rollback_count=candidate.counters.rollback_count,
        ),
        token_usage=candidate.token_usage,
        duration_s=candidate.duration_s,
        model_configuration=candidate.model_configuration,
        reproduction=ReproductionManifest(
            case_sha256=case.sha256,
            tool_versions={
                "external_evaluator": "week9.cross_version_validation.v1",
                "openmodelica": tool.version,
            },
        ),
        infrastructure_errors=infrastructure_errors,
        raw_metrics=raw_metrics,
    )
    if result.overall_success and candidate.version.lower() == "v8":
        result.repair_stop_reason = RepairStopReason.ALL_CHECKS_PASS
    return result


def _adapter_execution_check(candidate: RunResult) -> QualityCheckResult:
    existing = next(
        (check for check in candidate.checks if check.check_type == "adapter.execution"),
        None,
    )
    if existing is not None:
        return existing
    if candidate.version.lower() == "v8" and not candidate.infrastructure_errors:
        return _result(
            "adapter.execution",
            CheckStatus.PASS,
            "",
            evidence={"reason": "v8_run_result_contract_loaded"},
        )
    return _result(
        "adapter.execution",
        CheckStatus.ERROR,
        "adapter_execution_evidence_missing",
        "adapter execution evidence is missing",
    )


def _unsupported_requirement_result(
    candidate: RunResult,
    case: CaseDefinition,
    *,
    generation_issue_codes: tuple[str, ...],
    started: float,
) -> RunResult:
    """Fail closed without guessing how to validate an unknown topology."""

    adapter_execution = _adapter_execution_check(candidate)
    unsupported = _result(
        "requirement_normalization",
        CheckStatus.FAIL,
        "external_topology_not_supported",
        "版本外 evaluator 不支持该需求拓扑，禁止猜测或放行",
        evidence={"generation_issue_codes": list(generation_issue_codes)},
    )
    artifacts = {
        "requirement": ArtifactRecord(
            "requirement", content=case.raw_input, media_type="text/plain"
        ),
        "sysml": ArtifactRecord(
            "sysml",
            content=_artifact_content(candidate, "sysml"),
            media_type="text/x-sysml",
        ),
        "modelica": ArtifactRecord(
            "modelica",
            content=_artifact_content(candidate, "modelica"),
            media_type="text/x-modelica",
        ),
    }
    checks = (adapter_execution, unsupported)
    infrastructure_errors = list(candidate.infrastructure_errors)
    infrastructure_errors.extend(
        issue
        for check in checks
        if check.failure_kind is FailureKind.INFRASTRUCTURE
        for issue in check.issues
    )
    return evaluate(
        context=EvaluationContext(
            version=candidate.version,
            case_id=case.case_id,
            run_id=candidate.run_id,
            requirement_text=case.raw_input,
            artifacts=artifacts,
            metadata={"case_sha256": case.sha256, "template_id": ""},
        ),
        required_check_types=case.required_checks,
        parameters=candidate.parameters,
        precomputed_external_checks=checks,
        repairs=candidate.repairs,
        infrastructure_retries=candidate.infrastructure_retries,
        counters=RunCounters(
            generation_count=candidate.counters.generation_count,
            validation_count=len(checks),
            content_repair_count=candidate.counters.content_repair_count,
            accepted_content_repair_count=candidate.counters.accepted_content_repair_count,
            repair_attempt_count=candidate.counters.repair_attempt_count,
            upstream_redo_count=candidate.counters.upstream_redo_count,
            infrastructure_retry_count=candidate.counters.infrastructure_retry_count,
            no_op_count=candidate.counters.no_op_count,
            repeated_error_count=candidate.counters.repeated_error_count,
            rollback_count=candidate.counters.rollback_count,
        ),
        token_usage=candidate.token_usage,
        duration_s=candidate.duration_s,
        model_configuration=candidate.model_configuration,
        reproduction=ReproductionManifest(
            case_sha256=case.sha256,
            tool_versions={"external_evaluator": "week9.cross_version_validation.v1"},
        ),
        infrastructure_errors=infrastructure_errors,
        raw_metrics={
            "external_evaluator": "week9.cross_version_validation.v1",
            "candidate_internal_overall_success": candidate.overall_success,
            "candidate_raw_metrics": dict(candidate.raw_metrics),
            "template_id": "",
            "external_validation_duration_s": time.monotonic() - started,
            "external_validation_scope": "unsupported_topology_fail_closed",
            "external_parameter_observations": {},
        },
    )


def _artifact_content(candidate: RunResult, name: str) -> str:
    artifact = candidate.artifacts.get(name)
    if artifact is None:
        return ""
    if artifact.content:
        return artifact.content
    if artifact.path:
        try:
            return Path(artifact.path).read_text(encoding="utf-8")
        except OSError:
            return ""
    return ""


def _extract_model_name(source: str) -> str:
    match = _MODEL_DECLARATION.search(_strip_comments(source))
    return match.group(1) if match else ""


def _strip_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", " ", source)


def _declarations(source: str, class_name: str) -> list[tuple[str, str]]:
    clean = _strip_comments(source)
    pattern = re.compile(
        rf"\b(?:[A-Za-z_]\w*\.)*{re.escape(class_name)}\s+"
        rf"([A-Za-z_]\w*)\s*\(",
        re.IGNORECASE,
    )
    declarations: list[tuple[str, str]] = []
    for match in pattern.finditer(clean):
        opening = match.end() - 1
        depth = 0
        end = None
        for index in range(opening, len(clean)):
            char = clean[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    end = index
                    break
        if end is not None:
            declarations.append((match.group(1), clean[opening + 1 : end]))
    return declarations


def _numeric_argument(
    body: str,
    name: str,
    *,
    symbols: Mapping[str, float],
    nested: bool = False,
) -> float | None:
    expression_chars = r"[-+0-9.eE*/()A-Za-z_\s]+"
    if nested:
        match = re.search(
            rf"\b{re.escape(name)}\s*=\s*({expression_chars})", body
        )
    else:
        match = re.search(
            rf"(?:^|,)\s*{re.escape(name)}\s*=\s*({expression_chars})", body
        )
    if not match:
        return None
    expression = match.group(1).strip()
    while expression.endswith(")") and expression.count("(") < expression.count(")"):
        expression = expression[:-1].rstrip()
    try:
        return _safe_number(expression, symbols=symbols)
    except (SyntaxError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None


def _safe_number(
    expression: str, *, symbols: Mapping[str, float] | None = None
) -> float:
    tree = ast.parse(expression, mode="eval")
    symbol_values = dict(symbols or {})

    def visit(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            return float(node.value)
        if isinstance(node, ast.Name) and node.id in symbol_values:
            return float(symbol_values[node.id])
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and isinstance(
            node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)
        ):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            return left**right
        raise ValueError("unsupported numeric expression")

    value = visit(tree)
    if not math.isfinite(value):
        raise ValueError("numeric expression is not finite")
    return value


def _parameter_symbols(source: str) -> dict[str, float]:
    clean = _strip_comments(source)
    declarations = re.findall(
        r"\b(?:final\s+)?parameter\s+(?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*\s+"
        r"([A-Za-z_]\w*)\s*=\s*([^;]+);",
        clean,
        flags=re.IGNORECASE,
    )
    symbols: dict[str, float] = {"pi": math.pi}
    unresolved = list(declarations)
    for _ in range(len(unresolved) + 1):
        changed = False
        for name, expression in list(unresolved):
            try:
                value = _safe_number(expression.strip(), symbols=symbols)
            except (SyntaxError, TypeError, ValueError, ZeroDivisionError, OverflowError):
                continue
            symbols[name] = value
            unresolved.remove((name, expression))
            changed = True
        if not changed:
            break
    return symbols


def _values(source: str, class_name: str, argument: str, *, nested: bool = False):
    result: list[tuple[str, float]] = []
    symbols = _parameter_symbols(source)
    for instance, body in _declarations(source, class_name):
        value = _numeric_argument(
            body, argument, symbols=symbols, nested=nested
        )
        if value is not None:
            result.append((instance, value))
    return result


def _instances(source: str, class_name: str) -> set[str]:
    clean = _strip_comments(source)
    pattern = re.compile(
        rf"\b(?:[A-Za-z_]\w*\.)*{re.escape(class_name)}\s+"
        rf"([A-Za-z_]\w*)\s*(?:\(|;)",
        re.IGNORECASE,
    )
    return {match.group(1) for match in pattern.finditer(clean)}


def _extract_parameters(
    template_id: str, source: str
) -> tuple[dict[str, Any], list[str]]:
    observed: dict[str, Any] = {}
    issues: list[str] = []
    if template_id in {"rc_filter", "rlc_filter"}:
        observed["resistances"] = _values(source, "Resistor", "R")
        observed["capacitances"] = _values(source, "Capacitor", "C")
        sources = _values(source, "StepVoltage", "V") + _values(
            source, "SineVoltage", "V"
        )
        observed["source_voltages"] = sources
        observed["voltage_sensors"] = sorted(_instances(source, "VoltageSensor"))
        observed["stimulus"] = (
            "sine" if _instances(source, "SineVoltage") else
            "step" if _instances(source, "StepVoltage") else "missing"
        )
        if template_id == "rlc_filter":
            observed["inductances"] = _values(source, "Inductor", "L")
            frequencies = _values(source, "SineVoltage", "f")
            frequencies += _values(source, "SineVoltage", "freqHz")
            observed["source_frequencies"] = frequencies
    elif template_id == "thermal_dual_room":
        capacities = _values(source, "HeatCapacitor", "C")
        initials = _values(source, "HeatCapacitor", "start", nested=True)
        resistances = _values(source, "ThermalResistor", "R")
        conductances = _values(source, "ThermalConductor", "G")
        for name, conductance in conductances:
            if conductance == 0:
                issues.append(f"thermal conductor {name} has zero conductance")
            else:
                resistances.append((name, 1.0 / conductance))
        observed.update(
            heat_capacities=capacities,
            temperature_sensors=sorted(_instances(source, "TemperatureSensor")),
            initial_temperatures=initials,
            thermal_resistances=resistances,
            outdoor_temperatures=_values(source, "FixedTemperature", "T"),
        )
    else:
        issues.append(f"external extraction is not implemented for {template_id}")
    return observed, issues


def _expected_map(parameters: Iterable[NormalizedParameter]) -> dict[str, float]:
    return {item.canonical_name: float(item.value) for item in parameters}


def _parameter_check(
    parameters: list[NormalizedParameter],
    observed: Mapping[str, Any],
    extraction_issues: list[str],
) -> QualityCheckResult:
    expected = _expected_map(parameters)
    issues = list(extraction_issues)
    comparisons: dict[str, Any] = {}

    def compare(name: str, values: Iterable[tuple[str, float]], targets: Iterable[float]):
        actual = [float(value) for _, value in values]
        target_list = [float(value) for value in targets]
        comparisons[name] = {"expected": target_list, "actual": actual}
        unmatched = list(actual)
        for target in target_list:
            index = next(
                (i for i, value in enumerate(unmatched) if _close(value, target)), None
            )
            if index is None:
                issues.append(f"{name} lacks required value {target}")
            else:
                unmatched.pop(index)

    if "resistance" in expected:
        compare("resistance", observed.get("resistances", ()), [expected["resistance"]])
        compare("capacitance", observed.get("capacitances", ()), [expected["capacitance"]])
        compare("input_voltage", observed.get("source_voltages", ()), [expected["input_voltage"]])
        if "inductance" in expected:
            compare("inductance", observed.get("inductances", ()), [expected["inductance"]])
            compare(
                "source_frequency",
                observed.get("source_frequencies", ()),
                [expected["cutoff_frequency"]],
            )
            if observed.get("stimulus") != "sine":
                issues.append("RLC requirement needs a sine voltage stimulus")
        elif observed.get("stimulus") != "step":
            issues.append("RC requirement needs a step voltage stimulus")
    elif "heat_capacity_a" in expected:
        compare(
            "heat_capacity",
            observed.get("heat_capacities", ()),
            [expected["heat_capacity_a"], expected["heat_capacity_b"]],
        )
        compare(
            "thermal_resistance",
            observed.get("thermal_resistances", ()),
            [
                expected["shared_resistance"],
                expected["outdoor_resistance_a"],
                expected["outdoor_resistance_b"],
            ],
        )
        compare(
            "initial_temperature",
            observed.get("initial_temperatures", ()),
            [expected["initial_temperature_a"], expected["initial_temperature_b"]],
        )
        compare(
            "outdoor_temperature",
            observed.get("outdoor_temperatures", ()),
            [expected["outdoor_temperature"]],
        )
    return _result(
        "parameter_unit_consistency",
        CheckStatus.FAIL if issues else CheckStatus.PASS,
        "external_parameter_mismatch" if issues else "",
        *issues,
        evidence={"comparisons": comparisons, "observed": dict(observed)},
    )


def _topology_check(
    template_id: str, source: str
) -> tuple[bool, list[str], dict[str, Any]]:
    clean = _strip_comments(source)
    edges = {frozenset((left, right)) for left, right in _CONNECT.findall(clean)}
    classes: dict[str, set[str]] = {}
    for class_name in (
        "Resistor", "Capacitor", "Inductor", "Ground", "StepVoltage",
        "SineVoltage", "HeatCapacitor", "ThermalResistor", "ThermalConductor",
        "FixedTemperature",
    ):
        classes[class_name] = _instances(clean, class_name)
    issues: list[str] = []

    def linked(lefts: Iterable[str], rights: Iterable[str]) -> bool:
        return any(frozenset((left, right)) in edges for left in lefts for right in rights)

    if template_id == "rc_filter":
        source_names = classes["StepVoltage"]
        if not (
            linked(source_names, classes["Resistor"])
            and linked(classes["Resistor"], classes["Capacitor"])
            and linked(classes["Capacitor"], classes["Ground"])
            and linked(source_names, classes["Ground"])
        ):
            issues.append("RC source-resistor-capacitor-ground path is incomplete")
    elif template_id == "rlc_filter":
        source_names = classes["SineVoltage"]
        if not (
            linked(source_names, classes["Resistor"])
            and linked(classes["Resistor"], classes["Inductor"])
            and linked(classes["Inductor"], classes["Capacitor"])
            and linked(classes["Capacitor"], classes["Ground"])
            and linked(source_names, classes["Ground"])
        ):
            issues.append("RLC sine-source series path is incomplete")
    elif template_id == "thermal_dual_room":
        rooms = classes["HeatCapacitor"]
        walls = classes["ThermalResistor"] | classes["ThermalConductor"]
        outdoors = classes["FixedTemperature"]
        shared = any(
            sum(linked((wall,), (room,)) for room in rooms) >= 2 for wall in walls
        )
        exterior_links = sum(
            linked((wall,), outdoors) and any(linked((wall,), (room,)) for room in rooms)
            for wall in walls
        )
        if len(rooms) < 2 or len(walls) < 3 or not shared or exterior_links < 2:
            issues.append("dual-room shared-wall and two exterior-wall paths are incomplete")
    else:
        issues.append(f"topology validation is not implemented for {template_id}")
    return not issues, issues, {
        "classes": {name: sorted(values) for name, values in classes.items() if values},
        "edges": sorted(sorted(edge) for edge in edges),
    }


def _physical_check(
    *,
    template_id: str,
    expected: list[NormalizedParameter],
    observed: Mapping[str, Any],
    csv_data: CsvData | None,
    topology_ok: bool,
    topology_issues: list[str],
) -> QualityCheckResult:
    issues = list(topology_issues)
    evidence: dict[str, Any] = {"topology_ok": topology_ok}
    if csv_data is None:
        return _result(
            "physical_behavior",
            CheckStatus.UNVERIFIED,
            "simulation_evidence_missing",
            "没有可复核的仿真 CSV",
            evidence=evidence,
        )
    expected_map = _expected_map(expected)
    columns = csv_data.columns
    evidence["available_columns"] = sorted(columns)

    if template_id == "rc_filter":
        names = [name for name, _ in observed.get("capacitances", ())]
        names.extend(observed.get("voltage_sensors", ()))
        series_name, series = _find_series(
            columns,
            names,
            suffix=".v",
            direct_names=("output_voltage", "Vout"),
        )
        target = expected_map["input_voltage"]
        if series is None:
            issues.append("RC capacitor voltage series is missing")
        else:
            evidence.update(series=series_name, initial=series[0], final=series[-1])
            if abs(series[-1] - target) > max(0.05, abs(target) * 0.1):
                issues.append("RC final capacitor voltage does not approach the step input")
            if abs(series[-1] - target) >= abs(series[0] - target):
                issues.append("RC response does not move toward its expected steady state")
    elif template_id == "rlc_filter":
        names = [name for name, _ in observed.get("capacitances", ())]
        names.extend(observed.get("voltage_sensors", ()))
        series_name, series = _find_series(
            columns,
            names,
            suffix=".v",
            direct_names=("output_voltage", "Vout"),
        )
        if series is None:
            issues.append("RLC capacitor voltage series is missing")
        else:
            span = max(series) - min(series)
            evidence.update(series=series_name, peak_to_peak=span)
            if span <= max(1e-6, expected_map["input_voltage"] * 0.02):
                issues.append("RLC output does not show a finite oscillatory response")
        if observed.get("stimulus") != "sine":
            issues.append("RLC simulation did not use the required sine stimulus")
    elif template_id == "thermal_dual_room":
        names = [name for name, _ in observed.get("heat_capacities", ())]
        sensors = list(observed.get("temperature_sensors", ()))
        selected = []
        for index, name in enumerate(names[:2]):
            candidates = [name]
            if index < len(sensors):
                candidates.append(sensors[index])
            selected.append(
                _find_series(
                    columns,
                    candidates,
                    suffix=".T",
                    direct_names=(
                        "room_a_temperature" if index == 0 else "room_b_temperature",
                    ),
                )
            )
        series_values = [item for item in selected if item[1] is not None]
        if len(series_values) < 2:
            issues.append("dual-room temperature series are missing")
        else:
            outdoor = expected_map["outdoor_temperature"]
            initials = [
                expected_map["initial_temperature_a"],
                expected_map["initial_temperature_b"],
            ]
            summaries = []
            for index, (name, series) in enumerate(series_values):
                assert series is not None
                summaries.append({"series": name, "initial": series[0], "final": series[-1]})
                low, high = sorted((initials[index], outdoor))
                if min(series) < low - 1.0 or max(series) > high + 1.0:
                    issues.append(f"{name} leaves the physical temperature bounds")
                if abs(series[-1] - outdoor) >= abs(series[0] - outdoor):
                    issues.append(f"{name} does not move toward the outdoor boundary")
            evidence["temperature_series"] = summaries
    else:
        return _result(
            "physical_behavior",
            CheckStatus.UNVERIFIED,
            "physical_validator_not_implemented",
            f"没有 {template_id} 的版本外物理验证器",
            evidence=evidence,
        )
    return _result(
        "physical_behavior",
        CheckStatus.FAIL if issues else CheckStatus.PASS,
        "external_physical_mismatch" if issues else "",
        *issues,
        evidence=evidence,
    )


def _find_series(
    columns: Mapping[str, tuple[float, ...]],
    names: Iterable[str],
    *,
    suffix: str,
    direct_names: Iterable[str] = (),
) -> tuple[str, tuple[float, ...] | None]:
    lowered = {name.lower(): name for name in columns}
    for direct in direct_names:
        if direct.lower() in lowered:
            actual = lowered[direct.lower()]
            return actual, columns[actual]
    for instance in names:
        desired = f"{instance}{suffix}".lower()
        if desired in lowered:
            actual = lowered[desired]
            return actual, columns[actual]
        match = next(
            (name for name in columns if name.lower().endswith("." + desired)), None
        )
        if match:
            return match, columns[match]
    return "", None


def _read_all_csv(path: str | Path) -> CsvData:
    csv_path = Path(path)
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        headers = next(reader)
    normalized = tuple(item.strip() for item in headers)
    return read_simulation_csv(csv_path, normalized)


def _cross_model_check(
    *,
    parameters: list[NormalizedParameter],
    sysml_source: str,
    sysml_status: CheckStatus,
    topology_ok: bool,
    topology_issues: list[str],
    topology_evidence: Mapping[str, Any],
) -> QualityCheckResult:
    issues = list(topology_issues)
    if sysml_status is not CheckStatus.PASS:
        issues.append("SysML syntax is not verified")
    numbers = [float(item.group()) for item in _NUMBER.finditer(_strip_comments(sysml_source))]
    missing_values: list[str] = []
    for parameter in parameters:
        alternatives = [float(parameter.value)]
        for observation in parameter.conversion_evidence.get("observations", []):
            value = observation.get("observed_value")
            if type(value) in {int, float}:
                alternatives.append(float(value))
        if not any(_close(number, target) for number in numbers for target in alternatives):
            missing_values.append(parameter.canonical_name)
    if missing_values:
        issues.append("SysML lacks requirement values: " + ", ".join(missing_values))
    return _result(
        "cross_model_consistency",
        CheckStatus.FAIL if issues else CheckStatus.PASS,
        "external_cross_model_mismatch" if issues else "",
        *issues,
        evidence={
            "topology_ok": topology_ok,
            "topology": dict(topology_evidence),
            "sysml_numeric_values": numbers,
            "missing_parameter_values": missing_values,
        },
    )


def _close(actual: float, expected: float) -> bool:
    return math.isclose(actual, expected, rel_tol=0.02, abs_tol=1e-9)


def _result(
    check_type: str,
    status: CheckStatus,
    category: str,
    *issues: str,
    evidence: Mapping[str, Any] | None = None,
) -> QualityCheckResult:
    failure_kind = (
        FailureKind.NONE
        if status is CheckStatus.PASS
        else FailureKind.INFRASTRUCTURE
        if status is CheckStatus.ERROR
        else FailureKind.CONTENT
    )
    return QualityCheckResult(
        check_type=check_type,
        status=status,
        required=True,
        issues=[issue for issue in issues if issue],
        evidence={"reason": category, **dict(evidence or {})},
        tool_name="week9.cross_version_validation",
        tool_version="1",
        failure_kind=failure_kind,
        failure_category=category,
    )


__all__ = ["externally_validate"]
