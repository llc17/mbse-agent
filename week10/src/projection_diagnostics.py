"""Safe, offline diagnostics for the frozen V8 online projection contract.

This module intentionally reuses V8's parser and verifier.  It only turns
their outcome into a small, serialisable diagnosis suitable for reports.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from typing import Any, Mapping

from week9.src.generation import NaturalLanguageGenerationResult
from week9.src.online_generation import _parse_projection, _verify_projection


_SAFE_PARAMETER_NAMES = frozenset(
    {
        "resistance",
        "capacitance",
        "cutoff_frequency",
        "input_voltage",
        "inductance",
        "input_resistance",
        "feedback_resistance",
        "positive_supply",
        "negative_supply",
        "heat_capacity",
        "thermal_resistance",
        "initial_temperature",
        "outdoor_temperature",
        "heat_capacity_a",
        "heat_capacity_b",
        "shared_resistance",
        "outdoor_resistance_a",
        "outdoor_resistance_b",
        "initial_temperature_a",
        "initial_temperature_b",
    }
)
_UNKNOWN_PARAMETER = "unknown_parameter"
_REASON_CODES = frozenset(
    {
        "accepted",
        "response_json_invalid",
        "projection_schema_invalid",
        "unknown_diagnostic",
        "ready_mismatch",
        "local_input_blocked",
        "template_mismatch",
        "parameter_count_mismatch",
        "duplicate_parameter",
        "parameter_name_mismatch",
        "unit_mismatch",
        "source_span_mismatch",
        "value_mismatch",
    }
)
_DISPOSITIONS = frozenset(
    {"accepted", "content_rejection", "infrastructure_error"}
)


@dataclass(frozen=True, slots=True)
class ProjectionDiagnostic:
    """An immutable, non-sensitive explanation of a projection decision."""

    reason_code: str
    parameter_keys: tuple[str, ...]
    projection_accepted: bool
    legacy_disposition: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameter_keys", tuple(self.parameter_keys))
        if self.reason_code not in _REASON_CODES:
            raise ValueError("unsupported projection diagnostic reason")
        if self.legacy_disposition not in _DISPOSITIONS:
            raise ValueError("unsupported projection diagnostic disposition")
        if any(key not in _SAFE_PARAMETER_NAMES | {_UNKNOWN_PARAMETER} for key in self.parameter_keys):
            raise ValueError("projection diagnostic contains an unsafe parameter key")
        if type(self.projection_accepted) is not bool:
            raise TypeError("projection_accepted must be a bool")
        if self.projection_accepted != (self.reason_code == "accepted"):
            raise ValueError("accepted diagnostic must agree with its reason")
        required_disposition = (
            "accepted"
            if self.reason_code == "accepted"
            else "infrastructure_error"
            if self.reason_code in {"response_json_invalid", "projection_schema_invalid", "unknown_diagnostic"}
            else "content_rejection"
        )
        if self.legacy_disposition != required_disposition:
            raise ValueError("accepted diagnostic must agree with its disposition")

    def public_dict(self) -> dict[str, object]:
        """Return only fixed categories and allow-listed parameter keys."""
        return {
            "reason_code": self.reason_code,
            "parameter_keys": self.parameter_keys,
            "projection_accepted": self.projection_accepted,
            "legacy_disposition": self.legacy_disposition,
        }


def diagnose_projection(
    content: str, local: NaturalLanguageGenerationResult
) -> ProjectionDiagnostic:
    """Parse ``content`` with V8 and diagnose its V8 verification outcome."""
    try:
        projection = _parse_projection(content)
    except json.JSONDecodeError:
        return _diagnostic("response_json_invalid", ())
    except (TypeError, ValueError, KeyError, AttributeError):
        return _diagnostic("projection_schema_invalid", ())
    except Exception:
        # Do not expose exception text: a future provider maps this to its
        # existing OnlineGenerationError boundary.
        return _diagnostic("unknown_diagnostic", ())
    return diagnose_parsed_projection(projection, local)


def diagnose_parsed_projection(
    projection: Mapping[str, Any], local: NaturalLanguageGenerationResult
) -> ProjectionDiagnostic:
    """Diagnose an already V8-parsed projection without changing verification."""
    # This is deliberately called even though the ordered diagnosis below
    # explains the verifier's generic disagreement result more precisely.
    verifier_issues = _verify_projection(projection, local)
    remote_ready = projection["status"] == "ready"
    if remote_ready is not local.ready:
        return _diagnostic("ready_mismatch", ())
    if not local.ready:
        return _diagnostic("local_input_blocked", ())
    if projection["template_id"] != local.template_id:
        return _diagnostic("template_mismatch", ())

    remote = projection["observations"]
    expected = [asdict(item) for item in local.observations]
    if len(remote) != len(expected):
        return _diagnostic("parameter_count_mismatch", ())
    remote_by_name = {item["name"]: item for item in remote}
    if len(remote_by_name) != len(remote):
        return _diagnostic("duplicate_parameter", ())
    if any(item["name"] not in _SAFE_PARAMETER_NAMES for item in remote):
        return _diagnostic("parameter_name_mismatch", (_UNKNOWN_PARAMETER,))
    for item in expected:
        observed = remote_by_name.get(item["name"])
        if observed is None:
            return _diagnostic("parameter_name_mismatch", (_safe_key(item["name"]),))
        if observed["unit"] != item["unit"]:
            return _diagnostic("unit_mismatch", (_safe_key(item["name"]),))
        if observed["source_text"] != item["source_text"]:
            return _diagnostic("source_span_mismatch", (_safe_key(item["name"]),))
        if not math.isclose(float(observed["value"]), float(item["value"]), rel_tol=1e-12, abs_tol=1e-15):
            return _diagnostic("value_mismatch", (_safe_key(item["name"]),))
    if verifier_issues:
        # A parsed projection that cannot be explained by frozen checks is an
        # infrastructure condition, never a place to disclose provider data.
        return _diagnostic("unknown_diagnostic", ())
    return _diagnostic("accepted", tuple(_safe_key(item["name"]) for item in expected))


def _safe_key(value: object) -> str:
    return value if isinstance(value, str) and value in _SAFE_PARAMETER_NAMES else _UNKNOWN_PARAMETER


def _diagnostic(reason_code: str, parameter_keys: tuple[str, ...]) -> ProjectionDiagnostic:
    disposition = (
        "accepted"
        if reason_code == "accepted"
        else "infrastructure_error"
        if reason_code in {"response_json_invalid", "projection_schema_invalid", "unknown_diagnostic"}
        else "content_rejection"
    )
    return ProjectionDiagnostic(
        reason_code=reason_code,
        parameter_keys=parameter_keys,
        projection_accepted=reason_code == "accepted",
        legacy_disposition=disposition,
    )


__all__ = [
    "ProjectionDiagnostic",
    "diagnose_parsed_projection",
    "diagnose_projection",
]
