"""Fail-closed natural-language preparation for the certified V8 pipeline.

The local provider is deliberately a small audited grammar, not a claim of
general language understanding.  It accepts explicit Chinese requirements for
the five certified topologies, extracts only values attached to known labels,
and derives capacitance only when the requirement explicitly asks for the
certified RC/RLC formula.  Ambiguity, missing values, stimulus mismatch, or an
unknown topology produces a structured blocking result.

Online providers must return the same immutable result contract and record
request/response hashes.  The concrete network client lives in
``week9.src.online_generation`` so importing this module remains offline.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import math
import re
import time
from typing import Protocol

from week9.src.modelica_templates import convert_unit
from week9.src.parameter_normalization import (
    ParameterObservation,
    normalize_unit_label,
)
from week9.src.schemas import sha256_text
from week9.src.traceability import (
    EXPLICIT_BINDING_BASIS,
    TOPOLOGY_TARGET_ID,
    RequirementItem,
    RequirementTargetBinding,
    TraceabilityInput,
    get_trace_registry,
    parameter_target_id,
)


GENERATION_SCHEMA_VERSION = "v8-natural-language-generation-1"
LOCAL_PROVIDER_NAME = "v8_deterministic_local_grammar"
LOCAL_PROVIDER_MODEL = "certified_requirement_grammar_v1"
LOCAL_NORMALIZATION_MODE = "deterministic_natural_language_extraction_v1"
_GRAMMAR_SPEC = (
    "v1|templates=rc,rlc,opamp,single-room,dual-room|"
    "values=explicit-label-unit|derivations=rc-cutoff,rlc-resonance|"
    "policy=no-defaults,no-fuzzy-topology,no-implicit-stimulus"
)
LOCAL_GRAMMAR_SHA256 = sha256_text(_GRAMMAR_SPEC)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
_RESISTANCE_UNIT = r"(?:MΩ|kΩ|Ω|MOhm|kOhm|Ohm|ohm)"
_CAPACITANCE_UNIT = r"(?:mF|uF|µF|μF|nF|pF|F)"
_INDUCTANCE_UNIT = r"(?:mH|uH|H)"
_FREQUENCY_UNIT = r"(?:MHz|kHz|Hz|mhz|khz|hz)"
_VOLTAGE_UNIT = r"(?:mV|V)"
_HEAT_CAPACITY_UNIT = r"(?:MJ/K|kJ/K|J/K)"
_THERMAL_RESISTANCE_UNIT = r"(?:K/W)"
_TEMPERATURE_UNIT = r"(?:°C|℃|degC|K)"

_CANONICAL_CAPTURE_UNITS = {
    "resistance": "Ohm",
    "input_resistance": "Ohm",
    "feedback_resistance": "Ohm",
    "inductance": "H",
    "capacitance": "F",
    "cutoff_frequency": "Hz",
    "input_voltage": "V",
    "positive_supply": "V",
    "negative_supply": "V",
    "heat_capacity": "J/K",
    "heat_capacity_a": "J/K",
    "heat_capacity_b": "J/K",
    "thermal_resistance": "K/W",
    "shared_resistance": "K/W",
    "outdoor_resistance_a": "K/W",
    "outdoor_resistance_b": "K/W",
    "initial_temperature": "K",
    "initial_temperature_a": "K",
    "initial_temperature_b": "K",
    "joint_initial_temperature": "K",
    "outdoor_temperature": "K",
}


@dataclass(frozen=True, slots=True)
class GenerationIssue:
    code: str
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code.strip():
            raise ValueError("generation issue code must not be empty")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("generation issue message must not be empty")


@dataclass(frozen=True, slots=True)
class GenerationDerivation:
    target_name: str
    formula: str
    input_names: tuple[str, ...]
    value: float
    unit: str
    source_start: int
    source_end: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_names", tuple(self.input_names))
        if not self.target_name.strip() or not self.formula.strip() or not self.unit.strip():
            raise ValueError("generation derivation fields must not be empty")
        if not self.input_names or len(self.input_names) != len(set(self.input_names)):
            raise ValueError("generation derivation inputs must be unique and non-empty")
        if not math.isfinite(self.value):
            raise ValueError("generation derivation value must be finite")
        if self.source_start < 0 or self.source_end <= self.source_start:
            raise ValueError("generation derivation requires a valid source span")


@dataclass(frozen=True, slots=True)
class NaturalLanguageGenerationResult:
    requirement_text: str
    requirement_sha256: str
    provider: str
    model: str
    template_id: str = ""
    model_name: str = ""
    observations: tuple[ParameterObservation, ...] = ()
    traceability_input: TraceabilityInput | None = None
    derivations: tuple[GenerationDerivation, ...] = ()
    issues: tuple[GenerationIssue, ...] = ()
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    duration_s: float = 0.0
    online_api_used: bool = False
    api_call_count: int = 0
    prompt_sha256: str = ""
    request_sha256: str = ""
    response_sha256: str = ""
    response_content_sha256: str = ""
    verifier_sha256: str = ""
    temperature: float | None = None
    max_tokens: int = 0
    schema_version: str = GENERATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "observations", tuple(self.observations))
        object.__setattr__(self, "derivations", tuple(self.derivations))
        object.__setattr__(self, "issues", tuple(self.issues))
        if self.schema_version != GENERATION_SCHEMA_VERSION:
            raise ValueError("unsupported natural-language generation schema")
        if not isinstance(self.requirement_text, str) or not self.requirement_text.strip():
            raise ValueError("natural-language requirement must not be empty")
        if self.requirement_sha256 != sha256_text(self.requirement_text):
            raise ValueError("generation requirement hash does not match source text")
        if not self.provider.strip() or not self.model.strip():
            raise ValueError("generation provider and model must not be empty")
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError("generation total_tokens must equal prompt plus completion")
        if not isinstance(self.duration_s, (int, float)) or not math.isfinite(
            self.duration_s
        ) or self.duration_s < 0:
            raise ValueError("generation duration_s must be a finite non-negative number")
        if type(self.online_api_used) is not bool:
            raise TypeError("online_api_used must be a bool")
        if type(self.api_call_count) is not int or self.api_call_count < 0:
            raise ValueError("api_call_count must be a non-negative integer")
        if type(self.max_tokens) is not int or self.max_tokens < 0:
            raise ValueError("max_tokens must be a non-negative integer")
        if self.temperature is not None and (
            type(self.temperature) not in {int, float}
            or not math.isfinite(float(self.temperature))
        ):
            raise ValueError("temperature must be null or a finite number")
        evidence_hashes = (
            self.prompt_sha256,
            self.request_sha256,
            self.response_sha256,
            self.response_content_sha256,
            self.verifier_sha256,
        )
        if any(value and not _SHA256.fullmatch(value) for value in evidence_hashes):
            raise ValueError("generation evidence hashes must be SHA-256 strings")
        if self.online_api_used:
            if self.api_call_count != 1:
                raise ValueError("online generation must record exactly one API call")
            if self.max_tokens <= 0 or self.temperature is None:
                raise ValueError("online generation requires frozen request parameters")
            if any(not value for value in evidence_hashes):
                raise ValueError("online generation requires complete hash evidence")
        elif (
            self.api_call_count != 0
            or self.max_tokens != 0
            or self.temperature is not None
            or any(evidence_hashes)
        ):
            raise ValueError("offline generation cannot claim online request evidence")
        if any(not isinstance(item, ParameterObservation) for item in self.observations):
            raise TypeError("generation observations must be ParameterObservation objects")
        if any(not isinstance(item, GenerationIssue) for item in self.issues):
            raise TypeError("generation issues must be GenerationIssue objects")
        if any(not isinstance(item, GenerationDerivation) for item in self.derivations):
            raise TypeError("generation derivations must be GenerationDerivation objects")
        if self.ready:
            if not self.template_id.strip() or not self.model_name.strip():
                raise ValueError("ready generation requires template_id and model_name")
            if not self.observations or self.traceability_input is None:
                raise ValueError("ready generation requires observations and traceability")
            if (
                self.traceability_input.requirement_artifact_sha256
                != self.requirement_sha256
            ):
                raise ValueError("generation traceability is bound to another requirement")
            expected_targets = set(get_trace_registry(self.template_id).expected_target_ids)
            actual_targets = {
                item.target_id for item in self.traceability_input.bindings
            }
            if actual_targets != expected_targets:
                raise ValueError("generation traceability does not cover the template")
        elif not self.issues:
            raise ValueError("an unready generation result requires blocking issues")

    @property
    def ready(self) -> bool:
        return not self.issues

    @property
    def normalization_mode(self) -> str:
        return LOCAL_NORMALIZATION_MODE if self.provider == LOCAL_PROVIDER_NAME else (
            "model_natural_language_generation_v1"
        )

    def evidence(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "requirement_sha256": self.requirement_sha256,
            "provider": self.provider,
            "model": self.model,
            "template_id": self.template_id,
            "model_name": self.model_name,
            "ready": self.ready,
            "observations": [asdict(item) for item in self.observations],
            "derivations": [asdict(item) for item in self.derivations],
            "issues": [asdict(item) for item in self.issues],
            "traceability_input_sha256": (
                self.traceability_input.sha256
                if self.traceability_input is not None
                else ""
            ),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "duration_s": self.duration_s,
            "online_api_used": self.online_api_used,
            "api_call_count": self.api_call_count,
            "prompt_sha256": self.prompt_sha256,
            "request_sha256": self.request_sha256,
            "response_sha256": self.response_sha256,
            "response_content_sha256": self.response_content_sha256,
            "verifier_sha256": self.verifier_sha256,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "grammar_sha256": (
                LOCAL_GRAMMAR_SHA256 if self.provider == LOCAL_PROVIDER_NAME else ""
            ),
            "policy": "explicit_values_and_certified_derivations_only",
        }

    @property
    def evidence_sha256(self) -> str:
        return sha256_text(
            json.dumps(
                self.evidence(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )


class NaturalLanguageGenerationProvider(Protocol):
    def generate(self, requirement_text: str) -> NaturalLanguageGenerationResult:
        """Interpret one exact requirement without mutating the source text."""


@dataclass(frozen=True, slots=True)
class _Capture:
    text: str
    value: float
    unit: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _PreparedField:
    canonical_name: str
    observation: ParameterObservation
    source_text: str
    source_start: int
    source_end: int


class DeterministicNaturalLanguageProvider:
    """Audited zero-token provider for explicit certified requirements."""

    provider = LOCAL_PROVIDER_NAME
    model = LOCAL_PROVIDER_MODEL

    def generate(self, requirement_text: str) -> NaturalLanguageGenerationResult:
        started = time.monotonic()
        result = self._generate(requirement_text)
        return replace(result, duration_s=time.monotonic() - started)

    def _generate(self, requirement_text: str) -> NaturalLanguageGenerationResult:
        if not isinstance(requirement_text, str) or not requirement_text.strip():
            raise ValueError("requirement_text must be a non-empty string")
        template_id, topology, identification_issues = _identify_template(
            requirement_text
        )
        if identification_issues:
            return _blocked_result(
                requirement_text,
                issues=identification_issues,
                template_id=template_id,
            )
        assert topology is not None
        fields, derivations, issues = _extract_template_fields(
            template_id, requirement_text
        )
        if issues:
            return _blocked_result(
                requirement_text,
                issues=issues,
                template_id=template_id,
            )
        registry = get_trace_registry(template_id)
        by_name = {item.canonical_name: item for item in fields}
        missing = [name for name in registry.parameter_names if name not in by_name]
        extra = [name for name in by_name if name not in registry.parameter_names]
        if missing or extra:
            return _blocked_result(
                requirement_text,
                template_id=template_id,
                issues=(
                    GenerationIssue(
                        "generation_contract_incomplete",
                        f"字段合同不完整；missing={missing}, extra={extra}",
                    ),
                ),
            )
        ordered_fields = tuple(by_name[name] for name in registry.parameter_names)
        trace_input = _build_traceability_input(
            requirement_text,
            ordered_fields,
            topology,
        )
        requirement_hash = sha256_text(requirement_text)
        return NaturalLanguageGenerationResult(
            requirement_text=requirement_text,
            requirement_sha256=requirement_hash,
            provider=self.provider,
            model=self.model,
            template_id=template_id,
            model_name=_model_name(template_id, requirement_hash),
            observations=tuple(item.observation for item in ordered_fields),
            traceability_input=trace_input,
            derivations=derivations,
        )


def _identify_template(
    text: str,
) -> tuple[str, _Capture | None, tuple[GenerationIssue, ...]]:
    signatures = (
        ("rlc_filter", r"RLC\s*(?:串联)?低通滤波器"),
        ("rc_filter", r"(?<![A-Za-z])RC(?![A-Za-z])\s*(?:串联)?低通滤波器"),
        ("opamp_inverting", r"反相(?:运算)?(?:放大器|运放)"),
        ("thermal_dual_room", r"双房间热传导系统"),
        ("thermal_single_room", r"单房间热传导系统"),
    )
    matches: list[tuple[str, re.Match[str]]] = []
    for template_id, pattern in signatures:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match is not None:
            matches.append((template_id, match))
    if not matches:
        return "", None, (
            GenerationIssue(
                "no_certified_template",
                "需求未显式命中五类认证拓扑之一",
            ),
        )
    if len(matches) != 1:
        return "", None, (
            GenerationIssue(
                "ambiguous_template",
                "需求同时命中多个认证拓扑，禁止自动选择",
            ),
        )
    template_id, match = matches[0]
    topology = _Capture(match.group(0), 0.0, "topology", match.start(), match.end())
    return template_id, topology, ()


def _extract_template_fields(
    template_id: str, text: str
) -> tuple[
    tuple[_PreparedField, ...],
    tuple[GenerationDerivation, ...],
    tuple[GenerationIssue, ...],
]:
    if template_id == "rc_filter":
        return _extract_rc(text)
    if template_id == "rlc_filter":
        return _extract_rlc(text)
    if template_id == "opamp_inverting":
        return _extract_opamp(text)
    if template_id == "thermal_single_room":
        return _extract_single_room(text)
    if template_id == "thermal_dual_room":
        return _extract_dual_room(text)
    return (), (), (
        GenerationIssue("no_certified_template", f"未知认证模板：{template_id}"),
    )


def _extract_rc(text: str):
    issues = list(_require_stimulus(text, required="阶跃", forbidden="正弦"))
    cutoff = _capture_parameter(
        text, "cutoff_frequency", rf"截止频率\s*(?P<value>{_NUMBER})\s*(?P<unit>{_FREQUENCY_UNIT})"
    )
    resistance = _capture_parameter(
        text, "resistance", rf"(?<!输入)(?<!反馈)电阻(?:值)?\s*(?P<value>{_NUMBER})\s*(?P<unit>{_RESISTANCE_UNIT})"
    )
    voltage = _capture_parameter(
        text, "input_voltage", rf"(?:输入电压|输入(?:为|是)?|源电压)\s*(?P<value>{_NUMBER})\s*(?P<unit>{_VOLTAGE_UNIT})"
    )
    fields, capture_issues = _collect_required(cutoff, resistance, voltage)
    issues.extend(capture_issues)
    capacitance, derivation, cap_issues = _capacitance_or_rc_derivation(
        text, fields
    )
    issues.extend(cap_issues)
    if capacitance is not None:
        fields.append(capacitance)
    return tuple(fields), ((derivation,) if derivation else ()), tuple(issues)


def _extract_rlc(text: str):
    issues = list(_require_stimulus(text, required="正弦", forbidden="阶跃"))
    cutoff = _capture_parameter(
        text, "cutoff_frequency", rf"(?:截止|谐振)频率\s*(?P<value>{_NUMBER})\s*(?P<unit>{_FREQUENCY_UNIT})"
    )
    resistance = _capture_parameter(
        text, "resistance", rf"(?<!输入)(?<!反馈)(?:串联)?电阻(?:值)?\s*(?P<value>{_NUMBER})\s*(?P<unit>{_RESISTANCE_UNIT})"
    )
    inductance = _capture_parameter(
        text, "inductance", rf"(?:串联)?电感\s*(?P<value>{_NUMBER})\s*(?P<unit>{_INDUCTANCE_UNIT})"
    )
    voltage = _capture_parameter(
        text, "input_voltage", rf"(?:输入电压|输入(?:为|是)?|源电压)\s*(?P<value>{_NUMBER})\s*(?P<unit>{_VOLTAGE_UNIT})"
    )
    fields, capture_issues = _collect_required(
        cutoff, resistance, inductance, voltage
    )
    issues.extend(capture_issues)
    capacitance, derivation, cap_issues = _capacitance_or_rlc_derivation(
        text, fields
    )
    issues.extend(cap_issues)
    if capacitance is not None:
        fields.append(capacitance)
    return tuple(fields), ((derivation,) if derivation else ()), tuple(issues)


def _extract_opamp(text: str):
    captures = (
        _capture_parameter(text, "input_voltage", rf"输入电压\s*(?P<value>{_NUMBER})\s*(?P<unit>{_VOLTAGE_UNIT})"),
        _capture_parameter(text, "input_resistance", rf"输入电阻\s*(?P<value>{_NUMBER})\s*(?P<unit>{_RESISTANCE_UNIT})"),
        _capture_parameter(text, "feedback_resistance", rf"反馈电阻\s*(?P<value>{_NUMBER})\s*(?P<unit>{_RESISTANCE_UNIT})"),
        _capture_parameter(text, "positive_supply", rf"正电源\s*(?P<value>{_NUMBER})\s*(?P<unit>{_VOLTAGE_UNIT})"),
        _capture_parameter(text, "negative_supply", rf"负电源\s*(?P<value>{_NUMBER})\s*(?P<unit>{_VOLTAGE_UNIT})"),
    )
    fields, issues = _collect_required(*captures)
    return tuple(fields), (), tuple(issues)


def _extract_single_room(text: str):
    captures = (
        _capture_parameter(text, "heat_capacity", rf"(?:房间)?热容\s*(?P<value>{_NUMBER})\s*(?P<unit>{_HEAT_CAPACITY_UNIT})"),
        _capture_parameter(text, "thermal_resistance", rf"(?:墙体)?热阻\s*(?P<value>{_NUMBER})\s*(?P<unit>{_THERMAL_RESISTANCE_UNIT})"),
        _capture_parameter(text, "initial_temperature", rf"(?:室内)?初始温度\s*(?P<value>{_NUMBER})\s*(?P<unit>{_TEMPERATURE_UNIT})"),
        _capture_parameter(text, "outdoor_temperature", rf"(?:室外温度|室外)\s*(?P<value>{_NUMBER})\s*(?P<unit>{_TEMPERATURE_UNIT})"),
    )
    fields, issues = _collect_required(*captures)
    return tuple(fields), (), tuple(issues)


def _extract_dual_room(text: str):
    captures = [
        _capture_parameter(text, "heat_capacity_a", rf"房间\s*A\s*热容\s*(?P<value>{_NUMBER})\s*(?P<unit>{_HEAT_CAPACITY_UNIT})"),
        _capture_parameter(text, "heat_capacity_b", rf"房间\s*B\s*热容\s*(?P<value>{_NUMBER})\s*(?P<unit>{_HEAT_CAPACITY_UNIT})"),
        _capture_parameter(text, "shared_resistance", rf"两房间之间(?:墙体)?热阻\s*(?P<value>{_NUMBER})\s*(?P<unit>{_THERMAL_RESISTANCE_UNIT})"),
        _capture_parameter(text, "outdoor_resistance_a", rf"房间\s*A\s*外墙热阻\s*(?P<value>{_NUMBER})\s*(?P<unit>{_THERMAL_RESISTANCE_UNIT})"),
        _capture_parameter(text, "outdoor_resistance_b", rf"房间\s*B\s*外墙热阻\s*(?P<value>{_NUMBER})\s*(?P<unit>{_THERMAL_RESISTANCE_UNIT})"),
        _capture_parameter(text, "outdoor_temperature", rf"(?:室外温度|室外)\s*(?P<value>{_NUMBER})\s*(?P<unit>{_TEMPERATURE_UNIT})"),
    ]
    joint_initial = _capture_raw(
        text,
        "joint_initial_temperature",
        rf"两房间初始温度均(?:为)?\s*(?P<value>{_NUMBER})\s*(?P<unit>{_TEMPERATURE_UNIT})",
    )
    if isinstance(joint_initial, _Capture):
        captures.extend(
            (
                _field_from_capture("initial_temperature_a", joint_initial),
                _field_from_capture("initial_temperature_b", joint_initial),
            )
        )
    else:
        captures.extend(
            (
                _capture_parameter(text, "initial_temperature_a", rf"房间\s*A\s*初始温度\s*(?P<value>{_NUMBER})\s*(?P<unit>{_TEMPERATURE_UNIT})"),
                _capture_parameter(text, "initial_temperature_b", rf"房间\s*B\s*初始温度\s*(?P<value>{_NUMBER})\s*(?P<unit>{_TEMPERATURE_UNIT})"),
            )
        )
    fields, issues = _collect_required(*captures)
    if isinstance(joint_initial, GenerationIssue) and joint_initial.code == "ambiguous_parameter":
        issues.append(joint_initial)
    return tuple(fields), (), tuple(issues)


def _capacitance_or_rc_derivation(text: str, fields: list[_PreparedField]):
    explicit = _capture_parameter(
        text,
        "capacitance",
        rf"电容(?:值)?\s*(?P<value>{_NUMBER})\s*(?P<unit>{_CAPACITANCE_UNIT})",
    )
    if isinstance(explicit, _PreparedField):
        return explicit, None, ()
    if isinstance(explicit, GenerationIssue) and explicit.code == "ambiguous_parameter":
        return None, None, (explicit,)
    phrase = _formula_phrase(text, r"(?:电容根据截止频率计算|计算所需电容)")
    if phrase is None:
        return None, None, (
            GenerationIssue("missing_parameter", "缺少电容值或明确 RC 计算要求"),
        )
    by_name = {item.canonical_name: item for item in fields}
    if "resistance" not in by_name or "cutoff_frequency" not in by_name:
        return None, None, ()
    resistance = _canonical_value(by_name["resistance"], "Ohm")
    frequency = _canonical_value(by_name["cutoff_frequency"], "Hz")
    value = 1.0 / (2.0 * math.pi * resistance * frequency)
    field = _derived_field("capacitance", value, "F", phrase)
    return field, GenerationDerivation(
        target_name="capacitance",
        formula="C=1/(2*pi*R*fc)",
        input_names=("resistance", "cutoff_frequency"),
        value=value,
        unit="F",
        source_start=phrase.start,
        source_end=phrase.end,
    ), ()


def _capacitance_or_rlc_derivation(text: str, fields: list[_PreparedField]):
    explicit = _capture_parameter(
        text,
        "capacitance",
        rf"电容(?:值)?\s*(?P<value>{_NUMBER})\s*(?P<unit>{_CAPACITANCE_UNIT})",
    )
    if isinstance(explicit, _PreparedField):
        return explicit, None, ()
    if isinstance(explicit, GenerationIssue) and explicit.code == "ambiguous_parameter":
        return None, None, (explicit,)
    phrase = _formula_phrase(text, r"(?:电容根据谐振频率计算|计算所需电容)")
    if phrase is None:
        return None, None, (
            GenerationIssue("missing_parameter", "缺少电容值或明确 RLC 计算要求"),
        )
    by_name = {item.canonical_name: item for item in fields}
    if "inductance" not in by_name or "cutoff_frequency" not in by_name:
        return None, None, ()
    inductance = _canonical_value(by_name["inductance"], "H")
    frequency = _canonical_value(by_name["cutoff_frequency"], "Hz")
    value = 1.0 / (((2.0 * math.pi * frequency) ** 2) * inductance)
    field = _derived_field("capacitance", value, "F", phrase)
    return field, GenerationDerivation(
        target_name="capacitance",
        formula="C=1/((2*pi*f0)^2*L)",
        input_names=("cutoff_frequency", "inductance"),
        value=value,
        unit="F",
        source_start=phrase.start,
        source_end=phrase.end,
    ), ()


def _capture_parameter(text: str, canonical_name: str, pattern: str):
    capture = _capture_raw(text, canonical_name, pattern)
    if isinstance(capture, _Capture):
        return _field_from_capture(canonical_name, capture)
    return capture


def _capture_raw(text: str, label: str, pattern: str):
    matches = list(re.finditer(pattern, text, flags=re.IGNORECASE))
    if not matches:
        return GenerationIssue("missing_parameter", f"缺少显式参数：{label}")
    captures = tuple(
        _Capture(
            text=match.group(0),
            value=float(match.group("value")),
            unit=match.group("unit"),
            start=match.start(),
            end=match.end(),
        )
        for match in matches
    )
    if len(captures) == 1:
        return captures[0]
    target_unit = _CANONICAL_CAPTURE_UNITS.get(label)
    if target_unit is None:
        raise ValueError(f"no canonical capture unit registered for {label}")
    canonical_values: list[float] = []
    for item in captures:
        try:
            canonical_values.append(
                convert_unit(
                    item.value,
                    normalize_unit_label(item.unit),
                    target_unit,
                )
            )
        except ValueError:
            return GenerationIssue(
                "invalid_parameter_unit",
                f"参数 {label} 使用了不受支持的单位",
            )
    reference = canonical_values[0]
    if any(
        not math.isclose(value, reference, rel_tol=1e-12, abs_tol=1e-15)
        for value in canonical_values[1:]
    ):
        return GenerationIssue(
            "ambiguous_parameter",
            f"参数 {label} 出现多个冲突值，禁止自动选择",
        )
    return captures[0]


def _collect_required(*items):
    fields: list[_PreparedField] = []
    issues: list[GenerationIssue] = []
    for item in items:
        if isinstance(item, _PreparedField):
            fields.append(item)
        elif isinstance(item, GenerationIssue):
            issues.append(item)
        else:
            raise TypeError("unexpected generation capture result")
    return fields, issues


def _field_from_capture(canonical_name: str, capture: _Capture) -> _PreparedField:
    return _PreparedField(
        canonical_name=canonical_name,
        observation=ParameterObservation(
            name=canonical_name,
            value=capture.value,
            unit=capture.unit,
            source_text=capture.text,
        ),
        source_text=capture.text,
        source_start=capture.start,
        source_end=capture.end,
    )


def _derived_field(
    canonical_name: str, value: float, unit: str, phrase: _Capture
) -> _PreparedField:
    return _PreparedField(
        canonical_name=canonical_name,
        observation=ParameterObservation(
            name=canonical_name,
            value=value,
            unit=unit,
            source_text=phrase.text,
        ),
        source_text=phrase.text,
        source_start=phrase.start,
        source_end=phrase.end,
    )


def _formula_phrase(text: str, pattern: str) -> _Capture | None:
    matches = list(re.finditer(pattern, text, flags=re.IGNORECASE))
    if len(matches) != 1:
        return None
    match = matches[0]
    return _Capture(match.group(0), 0.0, "formula", match.start(), match.end())


def _require_stimulus(
    text: str, *, required: str, forbidden: str
) -> tuple[GenerationIssue, ...]:
    if forbidden in text:
        return (
            GenerationIssue(
                "stimulus_conflict",
                f"认证模板要求{required}激励，但需求声明了{forbidden}激励",
            ),
        )
    if required not in text:
        return (
            GenerationIssue(
                "missing_stimulus",
                f"需求必须显式声明{required}激励",
            ),
        )
    return ()


def _canonical_value(field: _PreparedField, target_unit: str) -> float:
    return convert_unit(
        float(field.observation.value),
        normalize_unit_label(field.observation.unit),
        target_unit,
    )


def _build_traceability_input(
    requirement_text: str,
    fields: tuple[_PreparedField, ...],
    topology: _Capture,
) -> TraceabilityInput:
    requirements: list[RequirementItem] = []
    bindings: list[RequirementTargetBinding] = []
    for index, field in enumerate(fields, start=1):
        requirement_id = f"REQ-NL-{index:03d}"
        requirements.append(
            RequirementItem(
                requirement_id=requirement_id,
                text=field.source_text,
                source_start=field.source_start,
                source_end=field.source_end,
                critical=True,
            )
        )
        bindings.append(
            RequirementTargetBinding(
                requirement_id=requirement_id,
                target_id=parameter_target_id(field.canonical_name),
                basis=EXPLICIT_BINDING_BASIS,
            )
        )
    topology_id = f"REQ-NL-{len(fields) + 1:03d}"
    requirements.append(
        RequirementItem(
            requirement_id=topology_id,
            text=topology.text,
            source_start=topology.start,
            source_end=topology.end,
            critical=True,
        )
    )
    bindings.append(
        RequirementTargetBinding(
            requirement_id=topology_id,
            target_id=TOPOLOGY_TARGET_ID,
            basis=EXPLICIT_BINDING_BASIS,
        )
    )
    return TraceabilityInput(
        requirement_artifact_sha256=sha256_text(requirement_text),
        requirements=tuple(requirements),
        bindings=tuple(bindings),
    )


def _blocked_result(
    requirement_text: str,
    *,
    issues: tuple[GenerationIssue, ...],
    template_id: str = "",
) -> NaturalLanguageGenerationResult:
    return NaturalLanguageGenerationResult(
        requirement_text=requirement_text,
        requirement_sha256=sha256_text(requirement_text),
        provider=LOCAL_PROVIDER_NAME,
        model=LOCAL_PROVIDER_MODEL,
        template_id=template_id,
        issues=tuple(dict.fromkeys(issues)),
    )


def _model_name(template_id: str, requirement_sha256: str) -> str:
    stems = {
        "rc_filter": "RCFilter",
        "rlc_filter": "RLCFilter",
        "opamp_inverting": "InvertingOpAmp",
        "thermal_single_room": "SingleRoomThermal",
        "thermal_dual_room": "DualRoomThermal",
    }
    return f"V8NL_{stems[template_id]}_{requirement_sha256[:8]}"


__all__ = [
    "DeterministicNaturalLanguageProvider",
    "GENERATION_SCHEMA_VERSION",
    "GenerationDerivation",
    "GenerationIssue",
    "LOCAL_GRAMMAR_SHA256",
    "LOCAL_NORMALIZATION_MODE",
    "LOCAL_PROVIDER_MODEL",
    "LOCAL_PROVIDER_NAME",
    "NaturalLanguageGenerationProvider",
    "NaturalLanguageGenerationResult",
]
