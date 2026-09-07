"""Stable, conservative diagnostic subcategories for precise V8 retrieval.

Only explicit compiler phrases are classified.  Ambiguous output remains
``unknown_diagnostic`` so it cannot trigger a broad retrieval query.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any
import unicodedata


DIAGNOSTIC_CLASSIFIER_VERSION = "v8-modelica-diagnostics-1"


@dataclass(frozen=True, slots=True)
class DiagnosticClassification:
    subcategory: str
    matched_rule: str
    symbols: tuple[str, ...] = ()

    @property
    def retrievable(self) -> bool:
        return self.subcategory in {
            "unknown_class_or_library",
            "unknown_component_parameter",
            "unknown_port_or_member",
            "connector_type_mismatch",
        }

    def evidence(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "classifier_version": DIAGNOSTIC_CLASSIFIER_VERSION,
            "retrievable": self.retrievable,
        }


_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "syntax_error",
        "explicit_syntax_error",
        re.compile(r"\b(?:syntax|parser)\s+error\b", re.IGNORECASE),
    ),
    (
        "equation_balance_error",
        "equation_system_balance",
        re.compile(
            r"(?:too\s+(?:many|few)\s+equations|under-?determined|over-?determined|"
            r"equation\s+count\s+is\s+(?:larger|smaller)|structurally\s+singular)",
            re.IGNORECASE,
        ),
    ),
    (
        "connector_type_mismatch",
        "invalid_or_incompatible_connector",
        re.compile(
            r"(?:\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)\s+is\s+not\s+a\s+valid\s+connector\b|"
            r"connectors?\s+do(?:es)?\s+not\s+match|incompatible\s+connector|"
            r"types?\s+of\s+connected\s+connectors?)",
            re.IGNORECASE,
        ),
    ),
    (
        "unknown_component_parameter",
        "unknown_component_parameter",
        re.compile(
            r"(?:unknown\s+(?:component\s+)?parameter\s+([A-Za-z_]\w*)|"
            r"(?:parameter|modifier)\s+([A-Za-z_]\w*)\s+(?:is\s+)?not\s+found|"
            r"component\s+[A-Za-z_]\w*\s+has\s+no\s+parameter\s+(?:named\s+)?([A-Za-z_]\w*))",
            re.IGNORECASE,
        ),
    ),
    (
        "unknown_port_or_member",
        "unknown_qualified_member",
        re.compile(
            r"(?:variable|component|member)\s+"
            r"([A-Za-z_]\w*\.[A-Za-z_]\w*)\s+not\s+found\s+in\s+scope|"
            r"(?:has\s+no|unknown)\s+(?:connector|port|member)\s+"
            r"([A-Za-z_]\w*)",
            re.IGNORECASE,
        ),
    ),
    (
        "unknown_class_or_library",
        "unknown_modelica_class",
        re.compile(
            r"class\s+(Modelica(?:\.[A-Za-z_]\w*)+)\s+not\s+found\s+in\s+scope|"
            r"(?:unknown|missing)\s+(?:class|library)\s+"
            r"(Modelica(?:\.[A-Za-z_]\w*)+)",
            re.IGNORECASE,
        ),
    ),
)


def classify_modelica_diagnostics(text: str) -> DiagnosticClassification:
    if not isinstance(text, str):
        raise TypeError("Modelica diagnostics must be text")
    normalized = unicodedata.normalize("NFKC", text)
    if not normalized.strip() or not re.search(
        r"(?:error|failed|not found|invalid|incompatible|determined|singular)",
        normalized,
        re.IGNORECASE,
    ):
        return DiagnosticClassification("none", "no_error_diagnostic")
    for subcategory, rule_id, pattern in _RULES:
        match = pattern.search(normalized)
        if match:
            symbols = tuple(
                dict.fromkeys(
                    value
                    for value in match.groups()
                    if isinstance(value, str) and value.strip()
                )
            )
            return DiagnosticClassification(subcategory, rule_id, symbols)
    return DiagnosticClassification("unknown_diagnostic", "no_stable_rule")


__all__ = [
    "DIAGNOSTIC_CLASSIFIER_VERSION",
    "DiagnosticClassification",
    "classify_modelica_diagnostics",
]
