"""Deterministic parameter alias and unit normalization for V8 Stage 3.

The normalizer consumes already extracted, structured observations.  It does
not infer values from prose and never supplies template defaults.  Every
observation is either mapped with conversion evidence or reported explicitly as
unknown, invalid, conflicting, or missing.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from enum import Enum
import json
import math
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping
import unicodedata

from week9.src.modelica_templates import convert_unit, get_template_spec
from week9.src.retrieval import (
    RetrievalResult,
    load_default_corpus,
    load_default_policy,
    retrieve_knowledge,
)
from week9.src.schemas import (
    NormalizedParameter,
    ParameterStatus,
    RoutingTrace,
    sha256_text,
)


@dataclass(frozen=True, slots=True)
class ParameterObservation:
    name: str
    value: Any
    unit: str
    source_text: str

    def __post_init__(self) -> None:
        for field_name in ("name", "unit", "source_text"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")


@dataclass(frozen=True, slots=True)
class ParameterNormalizationIssue:
    code: str
    message: str
    canonical_name: str = ""
    observed_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.message.strip():
            raise ValueError("normalization issue code and message must not be empty")


@dataclass(frozen=True, slots=True)
class ParameterNormalizationResult:
    template_id: str
    parameters: tuple[NormalizedParameter, ...]
    issues: tuple[ParameterNormalizationIssue, ...]
    unmapped_observations: tuple[ParameterObservation, ...]
    retrieval_evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "retrieval_evidence", _deep_freeze(dict(self.retrieval_evidence))
        )

    @property
    def ready(self) -> bool:
        return not self.issues and all(
            item.status is ParameterStatus.KNOWN for item in self.parameters
        )

    def evidence(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "ready": self.ready,
            "parameters": [asdict(item) for item in self.parameters],
            "issues": [asdict(item) for item in self.issues],
            "unmapped_observations": [
                asdict(item) for item in self.unmapped_observations
            ],
            "retrieval": _deep_thaw(self.retrieval_evidence),
            "policy": "no_defaults_no_value_inference",
        }


class ParameterNormalizationError(ValueError):
    """Raised when a caller requests a model from unresolved observations."""

    def __init__(
        self,
        result: ParameterNormalizationResult,
        *,
        routing: RoutingTrace | None = None,
        input_snapshot_sha256: str = "",
    ):
        if result.ready:
            raise ValueError("a ready normalization result is not an error")
        self.result = result
        self.routing = routing
        self.input_snapshot_sha256 = input_snapshot_sha256
        self.upgrade_reason_codes = tuple(
            dict.fromkeys(item.code for item in result.issues)
        )
        summary = "; ".join(item.message for item in result.issues)
        super().__init__(summary or "parameter normalization is not ready")

    def evidence(self) -> dict[str, Any]:
        return {
            "outcome": "blocked_before_artifact_generation",
            "upgrade_required": True,
            "upgrade_reason_codes": list(self.upgrade_reason_codes),
            "input_snapshot_sha256": self.input_snapshot_sha256,
            "normalization": self.result.evidence(),
            "routing": (
                _deep_thaw(asdict(self.routing)) if self.routing is not None else None
            ),
        }


class ParameterAliasProjectionError(ValueError):
    """Raised when retrieved alias evidence cannot be applied safely."""


_ALIASES: Mapping[str, Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "rc_filter": {
            "resistance": (
                "R",
                "R1",
                "resistor_resistance",
                "resistance_ohm",
                "电阻",
                "电阻值",
            ),
            "capacitance": (
                "C",
                "C1",
                "capacitor_capacitance",
                "电容",
                "电容量",
            ),
            "cutoff_frequency": (
                "fc",
                "f_cutoff",
                "cutoff",
                "cutoff_freq",
                "frequency",
                "截止频率",
            ),
            "input_voltage": (
                "V",
                "Vin",
                "V_in",
                "Vin_step",
                "input_v",
                "source_voltage",
                "输入电压",
                "源电压",
            ),
        },
        "rlc_filter": {
            "resistance": ("R", "R1", "series_resistance", "电阻", "串联电阻"),
            "inductance": ("L", "L1", "series_inductance", "电感", "串联电感"),
            "capacitance": ("C", "C1", "series_capacitance", "电容", "串联电容"),
            "cutoff_frequency": (
                "fc",
                "f0",
                "f_resonance",
                "resonant_frequency",
                "frequency",
                "谐振频率",
                "截止频率",
            ),
            "input_voltage": (
                "V",
                "Vin",
                "V_in",
                "input_v",
                "source_voltage",
                "输入电压",
                "源电压",
            ),
        },
        "opamp_inverting": {
            "input_voltage": (
                "Vin",
                "V_in",
                "Vsignal",
                "signal_voltage",
                "输入电压",
            ),
            "input_resistance": ("Rin", "R_in", "input_resistor", "输入电阻"),
            "feedback_resistance": ("Rf", "R_f", "feedback_resistor", "反馈电阻"),
            "positive_supply": (
                "Vcc",
                "V_plus",
                "positive_rail",
                "supply_positive",
                "正电源",
            ),
            "negative_supply": (
                "Vee",
                "V_minus",
                "negative_rail",
                "supply_negative",
                "负电源",
            ),
        },
        "thermal_single_room": {
            "heat_capacity": (
                "Cth",
                "C_thermal",
                "thermal_capacitance",
                "热容",
                "房间热容",
            ),
            "thermal_resistance": (
                "Rth",
                "R_thermal",
                "R_wall",
                "wall_resistance",
                "热阻",
                "墙体热阻",
            ),
            "initial_temperature": (
                "T0",
                "T_start",
                "T_indoor",
                "room_initial_temperature",
                "初始温度",
                "室内初始温度",
            ),
            "outdoor_temperature": (
                "T_out",
                "T_ambient",
                "T_outdoor",
                "ambient_temperature",
                "室外温度",
                "环境温度",
            ),
        },
        "thermal_dual_room": {
            "heat_capacity_a": (
                "Ca",
                "Cth_a",
                "C_thermal1",
                "heat_capacity1",
                "房间A热容",
            ),
            "heat_capacity_b": (
                "Cb",
                "Cth_b",
                "C_thermal2",
                "heat_capacity2",
                "房间B热容",
            ),
            "shared_resistance": (
                "R_ab",
                "R_shared",
                "R_wall_shared",
                "两房间之间墙体热阻",
                "共享墙热阻",
            ),
            "outdoor_resistance_a": (
                "R_out_a",
                "R_wall1",
                "outdoor_wall_a",
                "房间A外墙热阻",
            ),
            "outdoor_resistance_b": (
                "R_out_b",
                "R_wall2",
                "outdoor_wall_b",
                "房间B外墙热阻",
            ),
            "initial_temperature_a": (
                "T0_a",
                "T_start1",
                "T_indoor1",
                "房间A初始温度",
            ),
            "initial_temperature_b": (
                "T0_b",
                "T_start2",
                "T_indoor2",
                "房间B初始温度",
            ),
            "outdoor_temperature": (
                "T_out",
                "T_ambient",
                "T_outdoor",
                "室外温度",
                "环境温度",
            ),
        },
    }
)


_UNIT_ALIASES: Mapping[str, str] = MappingProxyType(
    {
        "Ohm": "Ohm",
        "ohm": "Ohm",
        "ohms": "Ohm",
        "OHM": "Ohm",
        "Ω": "Ohm",
        "kOhm": "kOhm",
        "kohm": "kOhm",
        "kΩ": "kOhm",
        "kiloohm": "kOhm",
        "MOhm": "MOhm",
        "MΩ": "MOhm",
        "F": "F",
        "mF": "mF",
        "uF": "uF",
        "µF": "uF",
        "μF": "uF",
        "nF": "nF",
        "pF": "pF",
        "H": "H",
        "mH": "mH",
        "uH": "uH",
        "V": "V",
        "mV": "mV",
        "Hz": "Hz",
        "hz": "Hz",
        "kHz": "kHz",
        "khz": "kHz",
        "MHz": "MHz",
        "K": "K",
        "degC": "degC",
        "°C": "degC",
        "Celsius": "degC",
        "J/K": "J/K",
        "kJ/K": "kJ/K",
        "MJ/K": "MJ/K",
        "K/W": "K/W",
    }
)


def normalize_parameters(
    template_id: str,
    observations: Iterable[ParameterObservation],
) -> ParameterNormalizationResult:
    """Map aliases and units without dropping or inventing information."""

    return _normalize_parameters(
        template_id,
        tuple(observations),
        retrieved_aliases={},
        retrieval_evidence={"triggered": False, "reason": "local_alias_registry"},
    )


def normalize_parameters_with_retrieval(
    template_id: str,
    observations: Iterable[ParameterObservation],
    retrieval: RetrievalResult,
) -> ParameterNormalizationResult:
    """Apply only certified retrieved aliases to the exact failed observation set.

    The projection is intentionally narrower than general RAG prompting.  A
    fragment may resolve an otherwise-unmapped name, but may not replace local
    aliases, target another template, add a value/unit, or change any observed
    content.  All downstream unit and conflict checks still run unchanged.
    """

    observation_list = tuple(observations)
    expected_snapshot = parameter_observations_sha256(template_id, observation_list)
    if retrieval.query.failed_snapshot_sha256 != expected_snapshot:
        raise ParameterAliasProjectionError(
            "retrieval result is not bound to the exact parameter observation snapshot"
        )
    if (
        retrieval.query.template_id != template_id
        or retrieval.query.target_language != "structured_parameter"
        or retrieval.query.repair_layer != "requirement"
        or retrieval.query.diagnostic_subcategory != "unknown_parameter_alias"
    ):
        raise ParameterAliasProjectionError(
            "retrieval query is not an unknown structured-parameter alias repair"
        )

    trusted_corpus = load_default_corpus()
    if retrieval.corpus_sha256 != trusted_corpus.sha256:
        raise ParameterAliasProjectionError(
            "retrieval result is not bound to the installed frozen corpus"
        )
    installed_policy = load_default_policy(trusted_corpus)
    trusted_policy = replace(
        installed_policy, enabled=retrieval.policy.enabled
    )
    if retrieval.policy != trusted_policy:
        raise ParameterAliasProjectionError(
            "retrieval result does not use the installed policy"
        )
    recomputed = retrieve_knowledge(
        retrieval.query,
        trusted_corpus,
        trusted_policy,
        prior_retrieval_events=0,
    )
    if recomputed != retrieval:
        raise ParameterAliasProjectionError(
            "retrieval result differs from deterministic recomputation"
        )
    if not retrieval.triggered:
        return _normalize_parameters(
            template_id,
            observation_list,
            retrieved_aliases={},
            retrieval_evidence=retrieval.evidence(),
        )
    trusted_fragments = {
        item.fragment_id: item for item in trusted_corpus.fragments
    }
    for selected in retrieval.fragments:
        if trusted_fragments.get(selected.fragment.fragment_id) != selected.fragment:
            raise ParameterAliasProjectionError(
                f"fragment {selected.fragment.fragment_id} differs from the frozen corpus"
            )

    spec = get_template_spec(template_id)
    local_index = _alias_index(template_id)
    local_result = _normalize_parameters(
        template_id,
        observation_list,
        retrieved_aliases={},
        retrieval_evidence={"triggered": False, "reason": "pre_retrieval_local_attempt"},
    )
    unsafe_issue_codes = {
        item.code for item in local_result.issues
    } - {"unknown_parameter_alias", "missing_parameter"}
    if unsafe_issue_codes:
        raise ParameterAliasProjectionError(
            "retrieved aliases cannot resolve invalid values, units, or conflicts"
        )
    locally_unmapped = {
        _normalize_parameter_name(item.name)
        for item in local_result.unmapped_observations
    }
    retrieved_aliases: dict[str, tuple[str, dict[str, Any]]] = {}
    for selected in retrieval.fragments:
        fragment = selected.fragment
        if fragment.validation.template_id != template_id:
            raise ParameterAliasProjectionError(
                f"fragment {fragment.fragment_id} targets a different template"
            )
        for raw_alias, canonical in fragment.parameter_aliases.items():
            if canonical not in spec.parameter_names:
                raise ParameterAliasProjectionError(
                    f"fragment {fragment.fragment_id} targets unknown parameter {canonical}"
                )
            normalized_alias = _normalize_parameter_name(raw_alias)
            if normalized_alias in local_index:
                raise ParameterAliasProjectionError(
                    f"retrieved alias {raw_alias!r} overlaps the local registry"
                )
            if normalized_alias not in locally_unmapped:
                raise ParameterAliasProjectionError(
                    f"retrieved alias {raw_alias!r} was not locally unmapped"
                )
            if normalized_alias in retrieved_aliases:
                raise ParameterAliasProjectionError(
                    f"retrieved alias {raw_alias!r} is provided more than once"
                )
            retrieved_aliases[normalized_alias] = (
                canonical,
                {
                    "kind": "retrieved_fragment",
                    "fragment_id": fragment.fragment_id,
                    "content_sha256": fragment.content_sha256,
                    "corpus_sha256": retrieval.corpus_sha256,
                    "failed_snapshot_sha256": expected_snapshot,
                },
            )
    if not retrieved_aliases:
        raise ParameterAliasProjectionError(
            "retrieval selected no applicable parameter alias mapping"
        )
    if set(retrieved_aliases) != locally_unmapped:
        raise ParameterAliasProjectionError(
            "retrieval did not resolve exactly the locally unknown aliases"
        )
    missing_canonical = {
        item.canonical_name
        for item in local_result.parameters
        if item.status is ParameterStatus.MISSING
    }
    retrieved_targets = {
        canonical for canonical, _provenance in retrieved_aliases.values()
    }
    if not missing_canonical.issubset(retrieved_targets):
        raise ParameterAliasProjectionError(
            "retrieval cannot hide independent missing parameters"
        )
    return _normalize_parameters(
        template_id,
        observation_list,
        retrieved_aliases=retrieved_aliases,
        retrieval_evidence=retrieval.evidence(),
    )


def parameter_observations_sha256(
    template_id: str,
    observations: Iterable[ParameterObservation],
) -> str:
    """Hash the ordered raw structured observations used by both ablation arms."""

    if not isinstance(template_id, str) or not template_id.strip():
        raise ValueError("template_id must not be empty")
    observation_list = tuple(observations)
    if any(not isinstance(item, ParameterObservation) for item in observation_list):
        raise TypeError("observations must contain ParameterObservation objects")
    payload = {
        "schema_version": "v8-parameter-observation-snapshot-1",
        "template_id": template_id,
        "observations": [
            {
                "name": item.name,
                "value": _stable_observation_value(item.value),
                "unit": item.unit,
                "source_text": item.source_text,
            }
            for item in observation_list
        ],
    }
    return sha256_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _normalize_parameters(
    template_id: str,
    observations: tuple[ParameterObservation, ...],
    *,
    retrieved_aliases: Mapping[str, tuple[str, Mapping[str, Any]]],
    retrieval_evidence: Mapping[str, Any],
) -> ParameterNormalizationResult:
    """Shared implementation for local-only and retrieval-assisted resolution."""

    spec = get_template_spec(template_id)
    observation_list = observations
    if any(not isinstance(item, ParameterObservation) for item in observation_list):
        raise TypeError("observations must contain ParameterObservation objects")
    alias_index = _alias_index(template_id)
    alias_provenance: dict[str, Mapping[str, Any]] = {
        normalized: {
            "kind": "local_template_alias_registry",
            "template_id": template_id,
        }
        for normalized in alias_index
    }
    for normalized, (canonical, provenance) in retrieved_aliases.items():
        if normalized in alias_index:
            raise ParameterAliasProjectionError(
                "retrieved aliases must not override the local registry"
            )
        alias_index[normalized] = canonical
        alias_provenance[normalized] = dict(provenance)
    grouped: dict[str, list[ParameterObservation]] = {
        parameter.name: [] for parameter in spec.parameters
    }
    unmapped: list[ParameterObservation] = []
    issues: list[ParameterNormalizationIssue] = []

    for observation in observation_list:
        normalized_observed_name = _normalize_parameter_name(observation.name)
        canonical = alias_index.get(normalized_observed_name)
        if canonical is None:
            unmapped.append(observation)
            issues.append(
                ParameterNormalizationIssue(
                    code="unknown_parameter_alias",
                    message=f"unrecognized parameter alias: {observation.name}",
                    observed_names=(observation.name,),
                )
            )
            continue
        grouped[canonical].append(observation)

    parameters: list[NormalizedParameter] = []
    target_units = {item.name: item.unit for item in spec.parameters}
    for canonical in spec.parameter_names:
        matches = grouped[canonical]
        target_unit = target_units[canonical]
        if not matches:
            issues.append(
                ParameterNormalizationIssue(
                    code="missing_parameter",
                    message=f"missing required parameter: {canonical}",
                    canonical_name=canonical,
                )
            )
            parameters.append(
                NormalizedParameter(
                    canonical_name=canonical,
                    value=None,
                    unit=target_unit,
                    source_text="missing from structured parameter observations",
                    status=ParameterStatus.MISSING,
                )
            )
            continue

        converted: list[dict[str, Any]] = []
        invalid_messages: list[str] = []
        for observation in matches:
            normalized_observed_name = _normalize_parameter_name(observation.name)
            if isinstance(observation.value, bool) or not isinstance(
                observation.value, (int, float)
            ):
                invalid_messages.append(
                    f"{observation.name} value is not a real number"
                )
                continue
            numeric = float(observation.value)
            if not math.isfinite(numeric):
                invalid_messages.append(f"{observation.name} value is not finite")
                continue
            try:
                normalized_unit = normalize_unit_label(observation.unit)
                canonical_value = convert_unit(numeric, normalized_unit, target_unit)
            except ValueError as exc:
                invalid_messages.append(f"{observation.name}: {exc}")
                continue
            converted.append(
                {
                    "observed_name": observation.name,
                    "observed_value": numeric,
                    "observed_unit": observation.unit,
                    "normalized_unit": normalized_unit,
                    "canonical_value": canonical_value,
                    "canonical_unit": target_unit,
                    "source_text": observation.source_text,
                    "alias_resolution": dict(
                        alias_provenance[normalized_observed_name]
                    ),
                }
            )

        observed_names = tuple(dict.fromkeys(item.name for item in matches))
        source_text = " | ".join(
            dict.fromkeys(item.source_text.strip() for item in matches)
        )
        if invalid_messages:
            issues.append(
                ParameterNormalizationIssue(
                    code="invalid_parameter_observation",
                    message="; ".join(invalid_messages),
                    canonical_name=canonical,
                    observed_names=observed_names,
                )
            )
            parameters.append(
                NormalizedParameter(
                    canonical_name=canonical,
                    value=None,
                    unit=target_unit,
                    source_text=source_text,
                    status=ParameterStatus.UNKNOWN,
                    aliases=list(observed_names),
                    conversion_evidence={
                        "target_unit": target_unit,
                        "valid_observations": converted,
                        "invalid_observations": invalid_messages,
                    },
                )
            )
            continue

        reference = converted[0]["canonical_value"]
        conflict = any(
            not math.isclose(
                item["canonical_value"],
                reference,
                rel_tol=1e-12,
                abs_tol=max(1e-15, abs(reference) * 1e-12),
            )
            for item in converted[1:]
        )
        if conflict:
            issues.append(
                ParameterNormalizationIssue(
                    code="conflicting_parameter_values",
                    message=f"conflicting observations for {canonical}",
                    canonical_name=canonical,
                    observed_names=observed_names,
                )
            )
            status = ParameterStatus.CONFLICT
            value = None
            resolution = "conflict"
        else:
            status = ParameterStatus.KNOWN
            value = reference
            resolution = (
                "equivalent_duplicates" if len(converted) > 1 else "single_observation"
            )
        parameters.append(
            NormalizedParameter(
                canonical_name=canonical,
                value=value,
                unit=target_unit,
                source_text=source_text,
                status=status,
                aliases=list(observed_names),
                conversion_evidence={
                    "target_unit": target_unit,
                    "resolution": resolution,
                    "observations": converted,
                },
            )
        )

    return ParameterNormalizationResult(
        template_id=template_id,
        parameters=tuple(parameters),
        issues=tuple(issues),
        unmapped_observations=tuple(unmapped),
        retrieval_evidence=retrieval_evidence,
    )


def _stable_observation_value(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "none", "value": None}
    if isinstance(value, bool):
        return {"type": "bool", "value": value}
    if isinstance(value, int):
        return {"type": "int", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value.hex()}
    if isinstance(value, str):
        return {"type": "str", "value": value}
    raise TypeError(
        "parameter observation snapshot supports only scalar JSON-like values"
    )


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_deep_thaw(item) for item in value]
    return value


def normalize_unit_label(unit: str) -> str:
    if not isinstance(unit, str) or not unit.strip():
        raise ValueError("unit must not be empty")
    key = unicodedata.normalize("NFKC", unit).strip()
    key = re.sub(r"\s+", "", key)
    try:
        return _UNIT_ALIASES[key]
    except KeyError as exc:
        raise ValueError(f"unknown unit: {unit}") from exc


def aliases_for_template(template_id: str) -> Mapping[str, tuple[str, ...]]:
    get_template_spec(template_id)
    aliases = _ALIASES.get(template_id, {})
    return MappingProxyType(
        {
            canonical: tuple((canonical, *aliases.get(canonical, ())))
            for canonical in get_template_spec(template_id).parameter_names
        }
    )


def _alias_index(template_id: str) -> dict[str, str]:
    index: dict[str, str] = {}
    for canonical, aliases in aliases_for_template(template_id).items():
        for alias in aliases:
            normalized = _normalize_parameter_name(alias)
            previous = index.get(normalized)
            if previous is not None and previous != canonical:
                raise RuntimeError(
                    f"ambiguous alias {alias!r} maps to {previous} and {canonical}"
                )
            index[normalized] = canonical
    return index


def _normalize_parameter_name(name: str) -> str:
    text = unicodedata.normalize("NFKC", name).strip().casefold()
    return re.sub(r"[\s_\-]+", "", text)


__all__ = [
    "ParameterAliasProjectionError",
    "ParameterNormalizationError",
    "ParameterNormalizationIssue",
    "ParameterNormalizationResult",
    "ParameterObservation",
    "aliases_for_template",
    "normalize_parameters",
    "normalize_parameters_with_retrieval",
    "normalize_unit_label",
    "parameter_observations_sha256",
]
