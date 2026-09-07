"""Version-neutral data contracts for V8.

These contracts deliberately do not reuse the V4/V6 ``passed`` or ``success``
booleans.  Legacy values can be retained as evidence, but only the V8 quality
gate may set ``RunResult.overall_success``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import math
import re
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "8.0-stage4"


def utc_now_iso() -> str:
    """Return a timezone-aware UTC timestamp suitable for JSON output."""

    return datetime.now(timezone.utc).isoformat()


def sha256_text(text: str) -> str:
    """Hash text using the single encoding used by all V8 manifests."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_sha256(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


class CheckStatus(str, Enum):
    """Outcome of a quality check.

    ``UNVERIFIED`` means the artifact/evidence needed to perform the check was
    absent. ``ERROR`` means the checker itself failed or was unavailable.
    Neither is equivalent to a successful check.
    """

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNVERIFIED = "UNVERIFIED"
    ERROR = "ERROR"


class ParameterStatus(str, Enum):
    KNOWN = "known"
    MISSING = "missing"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


class FailureKind(str, Enum):
    NONE = "none"
    CONTENT = "content"
    INFRASTRUCTURE = "infrastructure"
    NOT_IMPLEMENTED = "not_implemented"
    UNKNOWN = "unknown"


class RepairLayer(str, Enum):
    REQUIREMENT = "requirement"
    SYSML = "sysml"
    MODELICA = "modelica"


class RepairAction(str, Enum):
    RESTORE_CERTIFIED_TEMPLATE = "restore_certified_template"
    REBUILD_SYSML = "rebuild_sysml"
    UPSTREAM_REQUIREMENT_REDO = "upstream_requirement_redo"
    CUSTOM_AUDITED_PATCH = "custom_audited_patch"


class RepairOutcome(str, Enum):
    ACCEPTED = "accepted"
    PASSED = "passed"
    NO_OP = "no_op"
    ROLLED_BACK = "rolled_back"
    REJECTED = "rejected"


class RepairStopReason(str, Enum):
    NONE = ""
    ALL_CHECKS_PASS = "all_checks_pass"
    MAX_REPAIRS = "max_repairs"
    NO_OP = "no_op"
    REPEATED_ERROR = "repeated_error"
    REGRESSION = "regression"
    INFRASTRUCTURE_RETRY_EXHAUSTED = "infrastructure_retry_exhausted"
    UPSTREAM_REQUIRED = "upstream_required"
    UNREPAIRABLE = "unrepairable"
    REPAIR_PROVIDER_ERROR = "repair_provider_error"


class InfrastructureRetryOutcome(str, Enum):
    RESOLVED = "resolved"
    RETRY_FAILED = "retry_failed"
    EXHAUSTED = "exhausted"


class RoutingInputKind(str, Enum):
    NOT_RECORDED = "not_recorded"
    STRUCTURED_OBSERVATIONS = "structured_observations"
    PRENORMALIZED_PARAMETERS = "prenormalized_parameters"
    RAW_NATURAL_LANGUAGE = "raw_natural_language"


class RoutingState(str, Enum):
    RECEIVED = "received"
    NORMALIZING = "normalizing"
    RETRIEVING = "retrieving"
    CERTIFIED_GATE = "certified_gate"
    BOUNDED_REPAIR = "bounded_repair"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RoutingPath(str, Enum):
    NOT_EVALUATED = "not_evaluated"
    FAST_PATH = "fast_path"
    ESCALATION_PATH = "escalation_path"
    UNAVAILABLE_ENTRY = "unavailable_entry"


class RoutingAction(str, Enum):
    INTERPRET_NATURAL_LANGUAGE = "interpret_natural_language"
    ACCEPT_STRUCTURED_INPUT = "accept_structured_input"
    ACCEPT_PRENORMALIZED_INPUT = "accept_prenormalized_input"
    RETRIEVE_CERTIFIED_ALIAS = "retrieve_certified_alias"
    VALIDATE_CERTIFIED_SNAPSHOT = "validate_certified_snapshot"
    ENTER_BOUNDED_REPAIR = "enter_bounded_repair"
    RETRY_IDENTICAL_SNAPSHOT = "retry_identical_snapshot"
    REBUILD_CANONICAL_SYSML = "rebuild_canonical_sysml"
    RERENDER_CERTIFIED_MODELICA = "rerender_certified_modelica"
    RELEASE_INITIAL_GATE = "release_initial_gate"
    RELEASE_AFTER_ESCALATION = "release_after_escalation"
    STOP_UPSTREAM_REQUIRED = "stop_upstream_required"
    STOP_RECERTIFICATION_REQUIRED = "stop_recertification_required"
    STOP_UNREPAIRABLE = "stop_unrepairable"
    STOP_BUDGET_EXHAUSTED = "stop_budget_exhausted"
    STOP_NOT_IMPLEMENTED = "stop_not_implemented"


class BudgetDimension(str, Enum):
    VALIDATION = "validation"
    REPAIR_ATTEMPT = "repair_attempt"
    MODELICA_CHANGED_REPAIR = "modelica_changed_repair"
    UPSTREAM_CHANGED_REPAIR = "upstream_changed_repair"
    INFRASTRUCTURE_RETRY = "infrastructure_retry"
    RETRIEVAL_EVENT = "retrieval_event"
    RETRIEVAL_CONTEXT_TOKEN = "retrieval_context_token"


class BudgetEvaluationStatus(str, Enum):
    NOT_FROZEN = "not_frozen"
    NOT_APPLICABLE = "not_applicable"
    PASS = "PASS"
    FAIL = "FAIL"


class TraceLayer(str, Enum):
    SYSML = "sysml"
    MODELICA = "modelica"


class TraceLinkStatus(str, Enum):
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


@dataclass(frozen=True, slots=True)
class RoutingCheckSnapshot:
    check_type: str
    status: CheckStatus
    required: bool
    failure_kind: FailureKind
    failure_category: str
    semantic_evidence_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", CheckStatus(self.status))
        object.__setattr__(self, "failure_kind", FailureKind(self.failure_kind))
        if not self.check_type.strip():
            raise ValueError("routing check_type must not be empty")
        if type(self.required) is not bool:
            raise TypeError("routing required must be a bool")
        if not _is_sha256(self.semantic_evidence_sha256):
            raise ValueError("routing check evidence must be a SHA-256 digest")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RoutingCheckSnapshot":
        return cls(**dict(data))


@dataclass(frozen=True, slots=True)
class RoutingEvent:
    sequence: int
    from_state: RoutingState
    to_state: RoutingState
    action: RoutingAction
    reason_code: str
    input_snapshot_sha256: str
    artifact_bundle_sha256: str = ""
    validation_sha256: str = ""
    previous_event_sha256: str = ""
    event_sha256: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "from_state", RoutingState(self.from_state))
        object.__setattr__(self, "to_state", RoutingState(self.to_state))
        object.__setattr__(self, "action", RoutingAction(self.action))
        if type(self.sequence) is not int or self.sequence < 1:
            raise ValueError("routing event sequence starts at 1")
        if not self.reason_code.strip():
            raise ValueError("routing reason_code must not be empty")
        if not _is_sha256(self.input_snapshot_sha256):
            raise ValueError("routing input snapshot must be a SHA-256 digest")
        for name in (
            "artifact_bundle_sha256",
            "validation_sha256",
            "previous_event_sha256",
        ):
            value = getattr(self, name)
            if value and not _is_sha256(value):
                raise ValueError(f"{name} must be empty or a SHA-256 digest")
        expected = routing_event_sha256(self)
        if self.event_sha256 != expected:
            raise ValueError("routing event hash does not match its immutable fields")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RoutingEvent":
        return cls(**dict(data))


@dataclass(frozen=True, slots=True)
class RoutingTrace:
    schema_version: str = "v8-adaptive-routing-1"
    input_kind: RoutingInputKind = RoutingInputKind.NOT_RECORDED
    path: RoutingPath = RoutingPath.NOT_EVALUATED
    final_state: RoutingState | None = None
    certified_template_id: str = ""
    normalization_mode: str = "not_recorded"
    events: tuple[RoutingEvent, ...] = ()
    initial_checks: tuple[RoutingCheckSnapshot, ...] = ()
    initial_artifact_bundle_sha256: str = ""
    initial_validation_sha256: str = ""
    full_hard_gate_profile: bool = False
    escalation_reasons: tuple[str, ...] = ()
    retrieval_attempted: bool = False
    retrieval_triggered: bool = False
    repair_attempted: bool = False
    infrastructure_retry_attempted: bool = False
    reviewer_rounds: int = 0
    natural_language_interpretation_performed: bool = False
    online_api_used: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_kind", RoutingInputKind(self.input_kind))
        object.__setattr__(self, "path", RoutingPath(self.path))
        if self.final_state is not None:
            object.__setattr__(self, "final_state", RoutingState(self.final_state))
        object.__setattr__(self, "events", tuple(self.events))
        object.__setattr__(self, "initial_checks", tuple(self.initial_checks))
        object.__setattr__(self, "escalation_reasons", tuple(self.escalation_reasons))
        for name in (
            "full_hard_gate_profile",
            "retrieval_attempted",
            "retrieval_triggered",
            "repair_attempted",
            "infrastructure_retry_attempted",
            "natural_language_interpretation_performed",
            "online_api_used",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"routing {name} must be a bool")
        if type(self.reviewer_rounds) is not int or self.reviewer_rounds < 0:
            raise ValueError("reviewer_rounds must be a non-negative integer")
        if len(self.escalation_reasons) != len(set(self.escalation_reasons)) or any(
            not isinstance(item, str) or not item.strip()
            for item in self.escalation_reasons
        ):
            raise ValueError("routing escalation reasons must be unique and non-empty")
        check_names = [item.check_type for item in self.initial_checks]
        if len(check_names) != len(set(check_names)):
            raise ValueError("routing initial checks must be unique")
        for name in (
            "initial_artifact_bundle_sha256",
            "initial_validation_sha256",
        ):
            value = getattr(self, name)
            if value and not _is_sha256(value):
                raise ValueError(f"routing {name} must be empty or a SHA-256 digest")
        if self.path is RoutingPath.NOT_EVALUATED:
            if self.events or self.initial_checks or self.escalation_reasons:
                raise ValueError("an unevaluated route cannot carry routing evidence")
            if self.final_state is not None:
                raise ValueError("an unevaluated route cannot claim a final state")
            return
        if not self.events or self.final_state is None:
            raise ValueError("an evaluated route requires events and a final state")
        for index, event in enumerate(self.events):
            if event.sequence != index + 1:
                raise ValueError("routing event sequences must be consecutive")
            expected_previous = "" if index == 0 else self.events[index - 1].event_sha256
            if event.previous_event_sha256 != expected_previous:
                raise ValueError("routing event hash chain is broken")
            if index and self.events[index - 1].to_state is not event.from_state:
                raise ValueError("routing event states are not contiguous")
        if self.events[0].from_state is not RoutingState.RECEIVED:
            raise ValueError("routing must start from received")
        if self.events[-1].to_state is not self.final_state:
            raise ValueError("routing final state does not match the last event")
        if self.initial_checks:
            if self.initial_validation_sha256 != routing_validation_sha256(
                self.initial_checks
            ):
                raise ValueError(
                    "routing initial validation hash does not match initial checks"
                )
        elif self.initial_validation_sha256:
            raise ValueError(
                "routing cannot claim an initial validation hash without checks"
            )
        if self.retrieval_triggered and not self.retrieval_attempted:
            raise ValueError("triggered retrieval requires a retrieval attempt")
        if (
            self.input_kind is RoutingInputKind.PRENORMALIZED_PARAMETERS
            and self.normalization_mode != "pre_normalized_input"
        ):
            raise ValueError("pre-normalized routing has a contradictory mode")
        if self.input_kind is RoutingInputKind.STRUCTURED_OBSERVATIONS:
            expected_mode = (
                "retrieval_assisted_structured_normalization"
                if self.retrieval_triggered
                else "local_structured_normalization"
            )
            if self.normalization_mode != expected_mode:
                raise ValueError("structured routing has a contradictory normalization mode")
        escalation_actions = {
            RoutingAction.RETRIEVE_CERTIFIED_ALIAS,
            RoutingAction.ENTER_BOUNDED_REPAIR,
            RoutingAction.RETRY_IDENTICAL_SNAPSHOT,
            RoutingAction.REBUILD_CANONICAL_SYSML,
            RoutingAction.RERENDER_CERTIFIED_MODELICA,
            RoutingAction.RELEASE_AFTER_ESCALATION,
        }
        escalated = any(item.action in escalation_actions for item in self.events)
        if self.path is RoutingPath.FAST_PATH:
            if (
                self.final_state is not RoutingState.SUCCEEDED
                or not self.full_hard_gate_profile
                or self.escalation_reasons
                or escalated
                or self.retrieval_attempted
                or self.repair_attempted
                or self.infrastructure_retry_attempted
                or self.reviewer_rounds
                or self.online_api_used
            ):
                raise ValueError("fast path requires one complete local hard-gate pass")
        elif self.path is RoutingPath.ESCALATION_PATH:
            if not self.escalation_reasons or not (
                escalated
                or self.retrieval_attempted
                or self.repair_attempted
                or self.infrastructure_retry_attempted
                or self.final_state is RoutingState.FAILED
            ):
                raise ValueError("escalation path requires an auditable escalation reason")
            if (
                self.input_kind is RoutingInputKind.RAW_NATURAL_LANGUAGE
                and not self.natural_language_interpretation_performed
            ):
                raise ValueError(
                    "an evaluated natural-language route must record interpretation"
                )
        elif self.path is RoutingPath.UNAVAILABLE_ENTRY:
            if self.final_state is not RoutingState.FAILED:
                raise ValueError("an unavailable entry must fail closed")
        if self.final_state is RoutingState.SUCCEEDED and not self.full_hard_gate_profile:
            raise ValueError("successful certified routing requires the full hard-gate profile")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RoutingTrace":
        values = dict(data)
        values["events"] = tuple(
            RoutingEvent.from_dict(item) for item in values.get("events", ())
        )
        values["initial_checks"] = tuple(
            RoutingCheckSnapshot.from_dict(item)
            for item in values.get("initial_checks", ())
        )
        values["escalation_reasons"] = tuple(values.get("escalation_reasons", ()))
        return cls(**values)


@dataclass(frozen=True, slots=True)
class BudgetLimit:
    dimension: BudgetDimension
    limit: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "dimension", BudgetDimension(self.dimension))
        if type(self.limit) is not int or self.limit < 0:
            raise ValueError("budget limits must be non-negative integers")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BudgetLimit":
        return cls(**dict(data))


@dataclass(frozen=True, slots=True)
class BudgetEvent:
    sequence: int
    dimension: BudgetDimension
    amount: int
    action: str
    subject_sha256: str
    before: int
    after: int
    previous_event_sha256: str = ""
    event_sha256: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "dimension", BudgetDimension(self.dimension))
        for name in ("sequence", "amount", "before", "after"):
            value = getattr(self, name)
            minimum = 1 if name == "sequence" else 0
            if type(value) is not int or value < minimum:
                raise ValueError(f"budget {name} is outside its allowed range")
        if self.amount < 1 or self.after != self.before + self.amount:
            raise ValueError("budget events must consume a positive exact amount")
        if not self.action.strip() or not _is_sha256(self.subject_sha256):
            raise ValueError("budget events require an action and subject hash")
        if self.previous_event_sha256 and not _is_sha256(self.previous_event_sha256):
            raise ValueError("budget previous event hash must be empty or SHA-256")
        if self.event_sha256 != budget_event_sha256(self):
            raise ValueError("budget event hash does not match its immutable fields")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BudgetEvent":
        return cls(**dict(data))


@dataclass(frozen=True, slots=True)
class BudgetLedger:
    schema_version: str = "v8-budget-ledger-1"
    limits: tuple[BudgetLimit, ...] = ()
    events: tuple[BudgetEvent, ...] = ()
    simple_case_token_budget: int | None = None
    simple_case_duration_budget_s: float | None = None
    token_budget_status: BudgetEvaluationStatus = BudgetEvaluationStatus.NOT_APPLICABLE
    duration_budget_status: BudgetEvaluationStatus = BudgetEvaluationStatus.NOT_APPLICABLE
    observed_model_tokens: int = 0
    observed_duration_s: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "limits", tuple(self.limits))
        object.__setattr__(self, "events", tuple(self.events))
        object.__setattr__(
            self, "token_budget_status", BudgetEvaluationStatus(self.token_budget_status)
        )
        object.__setattr__(
            self,
            "duration_budget_status",
            BudgetEvaluationStatus(self.duration_budget_status),
        )
        limit_names = [item.dimension for item in self.limits]
        if len(limit_names) != len(set(limit_names)):
            raise ValueError("budget dimensions must have unique limits")
        if self.limits and set(limit_names) != set(BudgetDimension):
            raise ValueError("an evaluated budget ledger requires every dimension")
        if type(self.observed_model_tokens) is not int or self.observed_model_tokens < 0:
            raise ValueError("observed_model_tokens must be a non-negative integer")
        if (
            isinstance(self.observed_duration_s, bool)
            or not isinstance(self.observed_duration_s, (int, float))
            or not math.isfinite(float(self.observed_duration_s))
            or self.observed_duration_s < 0
        ):
            raise ValueError("observed_duration_s must be finite and non-negative")
        for name, value in (
            ("simple_case_token_budget", self.simple_case_token_budget),
            ("simple_case_duration_budget_s", self.simple_case_duration_budget_s),
        ):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value < 0
            ):
                raise ValueError(f"{name} must be null or finite and non-negative")
        usage = {dimension: 0 for dimension in BudgetDimension}
        for index, event in enumerate(self.events):
            if event.sequence != index + 1:
                raise ValueError("budget event sequences must be consecutive")
            expected_previous = "" if index == 0 else self.events[index - 1].event_sha256
            if event.previous_event_sha256 != expected_previous:
                raise ValueError("budget event hash chain is broken")
            if event.before != usage[event.dimension]:
                raise ValueError("budget event before value does not match prior usage")
            usage[event.dimension] = event.after
        limits = {item.dimension: item.limit for item in self.limits}
        if limits and any(usage[item] > limits[item] for item in BudgetDimension):
            raise ValueError("budget usage exceeds a configured hard limit")
        _validate_budget_status(
            self.simple_case_token_budget,
            self.observed_model_tokens,
            self.token_budget_status,
            "token",
        )
        _validate_budget_status(
            self.simple_case_duration_budget_s,
            float(self.observed_duration_s),
            self.duration_budget_status,
            "duration",
        )

    def usage(self, dimension: BudgetDimension | str) -> int:
        selected = BudgetDimension(dimension)
        return sum(item.amount for item in self.events if item.dimension is selected)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BudgetLedger":
        values = dict(data)
        values["limits"] = tuple(
            BudgetLimit.from_dict(item) for item in values.get("limits", ())
        )
        values["events"] = tuple(
            BudgetEvent.from_dict(item) for item in values.get("events", ())
        )
        return cls(**values)


@dataclass(frozen=True, slots=True)
class TraceElementReference:
    element_id: str
    layer: TraceLayer
    kind: str
    exact_selector: str
    artifact_sha256: str
    cardinality: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "layer", TraceLayer(self.layer))
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (self.element_id, self.kind, self.exact_selector)
        ):
            raise ValueError("trace element identifiers and selectors must be explicit")
        if not _is_sha256(self.artifact_sha256):
            raise ValueError("trace elements must bind an artifact SHA-256")
        if type(self.cardinality) is not int or self.cardinality < 0:
            raise ValueError("trace element cardinality must be non-negative")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TraceElementReference":
        return cls(**dict(data))


@dataclass(frozen=True, slots=True)
class TraceCheckReference:
    check_type: str
    status: CheckStatus
    required: bool
    semantic_evidence_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", CheckStatus(self.status))
        if not self.check_type.strip() or self.check_type == "traceability_integrity":
            raise ValueError("trace check references must name a base check")
        if type(self.required) is not bool:
            raise TypeError("trace check required must be a bool")
        if not _is_sha256(self.semantic_evidence_sha256):
            raise ValueError("trace check evidence must be a SHA-256 digest")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TraceCheckReference":
        return cls(**dict(data))


@dataclass(frozen=True, slots=True)
class TraceLink:
    requirement_id: str
    requirement_text: str
    requirement_text_sha256: str
    target_id: str
    status: TraceLinkStatus
    requirement_source_start: int = -1
    requirement_source_end: int = -1
    critical: bool = True
    not_implemented_reason: str = ""
    binding_basis: str = "explicit_structured_input"
    normalized_parameter_sha256: str = ""
    sysml_elements: tuple[TraceElementReference, ...] = ()
    modelica_elements: tuple[TraceElementReference, ...] = ()
    check_references: tuple[TraceCheckReference, ...] = ()
    issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", TraceLinkStatus(self.status))
        object.__setattr__(self, "sysml_elements", tuple(self.sysml_elements))
        object.__setattr__(self, "modelica_elements", tuple(self.modelica_elements))
        object.__setattr__(self, "check_references", tuple(self.check_references))
        object.__setattr__(self, "issues", tuple(self.issues))
        if not self.requirement_id.strip() or not self.requirement_text:
            raise ValueError("trace links require an explicit requirement item")
        if self.requirement_text_sha256 != sha256_text(self.requirement_text):
            raise ValueError("trace requirement hash does not match its text")
        if (
            type(self.requirement_source_start) is not int
            or type(self.requirement_source_end) is not int
            or self.requirement_source_start < 0
            or self.requirement_source_end <= self.requirement_source_start
            or self.requirement_source_end - self.requirement_source_start
            != len(self.requirement_text)
        ):
            raise ValueError("trace requirement source span must exactly cover its text")
        if type(self.critical) is not bool:
            raise TypeError("trace link critical must be a bool")
        if not isinstance(self.not_implemented_reason, str):
            raise TypeError("trace not_implemented_reason must be a string")
        if self.binding_basis != "explicit_structured_input":
            raise ValueError("trace links require explicit structured bindings")
        if not self.target_id.strip():
            raise ValueError("trace target_id must not be empty")
        if self.normalized_parameter_sha256 and not _is_sha256(
            self.normalized_parameter_sha256
        ):
            raise ValueError("normalized parameter trace must use SHA-256")
        element_ids = [
            item.element_id for item in self.sysml_elements + self.modelica_elements
        ]
        if len(element_ids) != len(set(element_ids)):
            raise ValueError("trace element references must be unique")
        check_names = [item.check_type for item in self.check_references]
        if len(check_names) != len(set(check_names)):
            raise ValueError("trace check references must be unique")
        if any(item.layer is not TraceLayer.SYSML for item in self.sysml_elements):
            raise ValueError("sysml_elements may contain only SysML references")
        if any(
            item.layer is not TraceLayer.MODELICA for item in self.modelica_elements
        ):
            raise ValueError("modelica_elements may contain only Modelica references")
        if any(not isinstance(item, str) or not item.strip() for item in self.issues):
            raise ValueError("trace issues must be non-empty strings")
        if self.status is TraceLinkStatus.VERIFIED:
            if (
                not self.sysml_elements
                or not self.modelica_elements
                or not self.check_references
                or any(item.cardinality != 1 for item in self.sysml_elements)
                or any(item.cardinality != 1 for item in self.modelica_elements)
                or any(not item.required for item in self.check_references)
                or self.issues
            ):
                raise ValueError("verified trace links require exact model and check evidence")
            if self.target_id.startswith("parameter:") and not (
                self.normalized_parameter_sha256
            ):
                raise ValueError(
                    "verified parameter trace links require normalized parameter evidence"
                )
        elif self.status is TraceLinkStatus.NOT_IMPLEMENTED:
            if not self.issues or not self.not_implemented_reason:
                raise ValueError("NOT_IMPLEMENTED trace links require a reason")
            if self.sysml_elements or self.modelica_elements or self.check_references:
                raise ValueError("NOT_IMPLEMENTED trace links cannot claim model evidence")
        elif not self.issues:
            raise ValueError("UNVERIFIED trace links require explicit issues")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TraceLink":
        values = dict(data)
        values["sysml_elements"] = tuple(
            TraceElementReference.from_dict(item)
            for item in values.get("sysml_elements", ())
        )
        values["modelica_elements"] = tuple(
            TraceElementReference.from_dict(item)
            for item in values.get("modelica_elements", ())
        )
        values["check_references"] = tuple(
            TraceCheckReference.from_dict(item)
            for item in values.get("check_references", ())
        )
        values["issues"] = tuple(values.get("issues", ()))
        return cls(**values)


@dataclass(frozen=True, slots=True)
class TraceabilityReport:
    schema_version: str = "v8-traceability-report-2"
    scope: str = ""
    template_id: str = ""
    template_sha256: str = ""
    requirement_artifact_sha256: str = ""
    sysml_artifact_sha256: str = ""
    modelica_artifact_sha256: str = ""
    trace_input_sha256: str = ""
    trace_input_requirement_artifact_sha256: str = ""
    links: tuple[TraceLink, ...] = ()
    critical_requirement_ids: tuple[str, ...] = ()
    integrity_passed: bool = False
    issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "links", tuple(self.links))
        object.__setattr__(
            self, "critical_requirement_ids", tuple(self.critical_requirement_ids)
        )
        object.__setattr__(self, "issues", tuple(self.issues))
        if type(self.integrity_passed) is not bool:
            raise TypeError("traceability integrity_passed must be a bool")
        if self.links:
            if not self.scope.strip() or not self.template_id.strip():
                raise ValueError("traceability reports require scope and template identity")
            for name in (
                "template_sha256",
                "requirement_artifact_sha256",
                "sysml_artifact_sha256",
                "modelica_artifact_sha256",
                "trace_input_sha256",
                "trace_input_requirement_artifact_sha256",
            ):
                if not _is_sha256(getattr(self, name)):
                    raise ValueError(f"traceability {name} must be SHA-256")
        requirement_ids = [item.requirement_id for item in self.links]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("traceability requirement links must be unique")
        if len(self.critical_requirement_ids) != len(set(self.critical_requirement_ids)):
            raise ValueError("critical requirement ids must be unique")
        if not set(self.critical_requirement_ids).issubset(requirement_ids):
            raise ValueError("critical requirement ids must refer to trace links")
        derived_critical_ids = tuple(
            item.requirement_id for item in self.links if item.critical
        )
        if self.critical_requirement_ids != derived_critical_ids:
            raise ValueError(
                "critical requirement ids must be derived from immutable trace links"
            )
        if any(not isinstance(item, str) or not item.strip() for item in self.issues):
            raise ValueError("traceability issues must be non-empty strings")
        computed_pass = (
            bool(self.links)
            and bool(self.critical_requirement_ids)
            and not self.issues
            and all(
            item.status is TraceLinkStatus.VERIFIED
            for item in self.links
            if item.requirement_id in self.critical_requirement_ids
            )
        )
        if self.integrity_passed is not computed_pass:
            raise ValueError("traceability integrity decision is inconsistent with links")
        if self.links and self.trace_input_sha256 != trace_input_semantic_sha256(
            self.trace_input_requirement_artifact_sha256, self.links
        ):
            raise ValueError("traceability input hash does not match its persisted links")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TraceabilityReport":
        values = dict(data)
        values["links"] = tuple(
            TraceLink.from_dict(item) for item in values.get("links", ())
        )
        values["critical_requirement_ids"] = tuple(
            values.get("critical_requirement_ids", ())
        )
        values["issues"] = tuple(values.get("issues", ()))
        return cls(**values)


@dataclass(slots=True)
class NormalizedParameter:
    canonical_name: str
    value: Any = None
    unit: str = "unknown"
    source_text: str = ""
    status: ParameterStatus = ParameterStatus.UNKNOWN
    aliases: list[str] = field(default_factory=list)
    conversion_evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.canonical_name.strip():
            raise ValueError("canonical_name must not be empty")
        self.status = ParameterStatus(self.status)
        if self.status is ParameterStatus.KNOWN and self.value is None:
            raise ValueError("a known parameter must have a value")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "NormalizedParameter":
        return cls(**dict(data))


@dataclass(slots=True)
class ArtifactRecord:
    """A final artifact represented by content, a path, or both."""

    artifact_type: str
    content: str = ""
    path: str = ""
    sha256: str = ""
    media_type: str = "text/plain"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.artifact_type.strip():
            raise ValueError("artifact_type must not be empty")
        if self.artifact_type != self.artifact_type.strip():
            raise ValueError("artifact_type must not contain surrounding whitespace")
        if self.content:
            actual_hash = sha256_text(self.content)
            if self.sha256 and self.sha256 != actual_hash:
                raise ValueError("artifact sha256 does not match content")
            self.sha256 = actual_hash

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArtifactRecord":
        return cls(**dict(data))


@dataclass(slots=True)
class QualityCheckResult:
    check_type: str
    status: CheckStatus
    required: bool
    issues: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    tool_name: str = ""
    tool_version: str = ""
    started_at: str = field(default_factory=utc_now_iso)
    duration_s: float = 0.0
    failure_kind: FailureKind = FailureKind.NONE
    failure_category: str = ""

    def __post_init__(self) -> None:
        if not self.check_type.strip():
            raise ValueError("check_type must not be empty")
        if self.check_type != self.check_type.strip():
            raise ValueError("check_type must not contain surrounding whitespace")
        if type(self.required) is not bool:
            raise TypeError("required must be a bool")
        self.status = CheckStatus(self.status)
        self.failure_kind = FailureKind(self.failure_kind)
        if self.duration_s < 0:
            raise ValueError("duration_s must be non-negative")
        if self.status is CheckStatus.NOT_APPLICABLE and not self.evidence.get("reason"):
            raise ValueError("NOT_APPLICABLE checks must include evidence.reason")
        if self.status is CheckStatus.NOT_APPLICABLE and self.required:
            raise ValueError("NOT_APPLICABLE is valid only for optional checks")
        failed = self.status in {
            CheckStatus.FAIL,
            CheckStatus.UNVERIFIED,
            CheckStatus.ERROR,
        }
        if failed and not (self.issues or self.evidence.get("reason")):
            raise ValueError("non-pass checks must include an issue or evidence.reason")
        if failed and self.failure_kind is FailureKind.NONE:
            raise ValueError("non-pass checks must classify failure_kind")
        if not failed and self.failure_kind is not FailureKind.NONE:
            raise ValueError("PASS/NOT_APPLICABLE checks cannot carry a failure_kind")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "QualityCheckResult":
        return cls(**dict(data))


@dataclass(slots=True)
class RepairRecord:
    """Auditable evidence for one content-repair attempt."""

    attempt: int
    failed_check: str
    failure_kind: FailureKind
    error_category: str
    repair_layer: RepairLayer
    action: RepairAction
    artifact_type: str
    failure_fingerprint_before: str
    previous_actions: list[str]
    original_evidence: dict[str, Any]
    allowed_scope: list[str]
    artifact_before: str
    artifact_after: str
    before_sha256: str
    after_sha256: str
    diff: str
    required_revalidation_checks: list[str]
    revalidation_results: list[QualityCheckResult] = field(default_factory=list)
    failure_fingerprint_after: str = ""
    accepted: bool = False
    rolled_back: bool = False
    effective_after_sha256: str = ""
    outcome: RepairOutcome = RepairOutcome.REJECTED
    stop_reason: RepairStopReason = RepairStopReason.NONE

    def __post_init__(self) -> None:
        self.failure_kind = FailureKind(self.failure_kind)
        self.repair_layer = RepairLayer(self.repair_layer)
        self.action = RepairAction(self.action)
        self.outcome = RepairOutcome(self.outcome)
        self.stop_reason = RepairStopReason(self.stop_reason)
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("repair attempt numbers start at 1")
        if self.failure_kind is not FailureKind.CONTENT:
            raise ValueError("RepairRecord is only for content or upstream repairs")
        for name, value in (
            ("failed_check", self.failed_check),
            ("error_category", self.error_category),
            ("artifact_type", self.artifact_type),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        expected_artifact = {
            RepairLayer.REQUIREMENT: "requirement",
            RepairLayer.SYSML: "sysml",
            RepairLayer.MODELICA: "modelica",
        }[self.repair_layer]
        if self.artifact_type != expected_artifact:
            raise ValueError("artifact_type must match repair_layer")
        allowed_actions = {
            RepairLayer.REQUIREMENT: {
                RepairAction.UPSTREAM_REQUIREMENT_REDO,
                RepairAction.CUSTOM_AUDITED_PATCH,
            },
            RepairLayer.SYSML: {
                RepairAction.REBUILD_SYSML,
                RepairAction.CUSTOM_AUDITED_PATCH,
            },
            RepairLayer.MODELICA: {
                RepairAction.RESTORE_CERTIFIED_TEMPLATE,
            },
        }
        if self.action not in allowed_actions[self.repair_layer]:
            raise ValueError("repair action is not allowed for the target layer")
        if not self.allowed_scope or any(
            not isinstance(item, str) or not item.strip()
            for item in self.allowed_scope
        ):
            raise ValueError("allowed_scope must contain explicit non-empty entries")
        if len(self.allowed_scope) != len(set(self.allowed_scope)):
            raise ValueError("allowed_scope entries must be unique")
        if any(not isinstance(item, str) or not item.strip() for item in self.previous_actions):
            raise ValueError("previous_actions entries must be non-empty strings")
        if any(
            not isinstance(item, str) or not item.strip()
            for item in self.required_revalidation_checks
        ):
            raise ValueError("required_revalidation_checks entries must be non-empty")
        if len(self.required_revalidation_checks) != len(
            set(self.required_revalidation_checks)
        ):
            raise ValueError("required_revalidation_checks entries must be unique")
        if not _is_sha256(self.failure_fingerprint_before):
            raise ValueError("failure_fingerprint_before must be a SHA-256 digest")
        if self.failure_fingerprint_after and not _is_sha256(
            self.failure_fingerprint_after
        ):
            raise ValueError("failure_fingerprint_after must be a SHA-256 digest")
        actual_before = sha256_text(self.artifact_before)
        actual_after = sha256_text(self.artifact_after)
        if self.before_sha256 != actual_before or self.after_sha256 != actual_after:
            raise ValueError("repair artifact hashes must match the complete snapshots")
        if type(self.accepted) is not bool or type(self.rolled_back) is not bool:
            raise TypeError("accepted and rolled_back must be bool")
        if self.rolled_back and self.accepted:
            raise ValueError("a rolled-back repair cannot be accepted")
        if self.is_noop:
            if self.diff:
                raise ValueError("a no-op repair cannot carry a diff")
            if self.accepted or self.rolled_back:
                raise ValueError("a no-op repair cannot be accepted or rolled back")
            if self.outcome is not RepairOutcome.NO_OP:
                raise ValueError("unchanged snapshots require outcome=no_op")
            if self.stop_reason is not RepairStopReason.NO_OP:
                raise ValueError("a no-op repair must stop with reason=no_op")
            if self.failure_fingerprint_after != self.failure_fingerprint_before:
                raise ValueError("a no-op repair must retain the same failure fingerprint")
            expected_effective_hash = self.before_sha256
        else:
            if not self.diff.strip():
                raise ValueError("a changed repair must carry an auditable diff")
            if not self.revalidation_results:
                raise ValueError("a changed repair must contain revalidation results")
            if not self.required_revalidation_checks:
                raise ValueError("a changed repair must declare required revalidation checks")
            result_name_list = [item.check_type for item in self.revalidation_results]
            if len(result_name_list) != len(set(result_name_list)):
                raise ValueError("revalidation results must have unique check types")
            result_names = set(result_name_list)
            if not set(self.required_revalidation_checks).issubset(result_names):
                raise ValueError("required revalidation checks are missing from evidence")
            indexed_results = {
                item.check_type: item for item in self.revalidation_results
            }
            if any(
                not indexed_results[name].required
                for name in self.required_revalidation_checks
            ):
                raise ValueError("declared required revalidation checks must remain required")
            if self.rolled_back:
                if self.outcome is not RepairOutcome.ROLLED_BACK:
                    raise ValueError("rolled_back repairs require outcome=rolled_back")
                expected_effective_hash = self.before_sha256
            elif self.accepted:
                if self.outcome not in {
                    RepairOutcome.ACCEPTED,
                    RepairOutcome.PASSED,
                }:
                    raise ValueError("accepted repair has an inconsistent outcome")
                if self.outcome is RepairOutcome.PASSED:
                    required_results = [
                        indexed_results[name]
                        for name in self.required_revalidation_checks
                    ]
                    if any(item.status is not CheckStatus.PASS for item in required_results):
                        raise ValueError("outcome=passed requires every hard check to PASS")
                    if self.failure_fingerprint_after:
                        raise ValueError("a passing repair cannot retain a failure fingerprint")
                    if self.stop_reason is not RepairStopReason.ALL_CHECKS_PASS:
                        raise ValueError("outcome=passed must stop with all_checks_pass")
                elif not self.failure_fingerprint_after:
                    raise ValueError("an accepted but failing repair needs a new fingerprint")
                expected_effective_hash = self.after_sha256
            else:
                if self.outcome is not RepairOutcome.REJECTED:
                    raise ValueError("unaccepted repair has an inconsistent outcome")
                expected_effective_hash = self.before_sha256
        if self.effective_after_sha256 != expected_effective_hash:
            raise ValueError("effective_after_sha256 does not match commit/rollback state")

    @property
    def is_noop(self) -> bool:
        return self.before_sha256 == self.after_sha256

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RepairRecord":
        values = dict(data)
        values["revalidation_results"] = [
            QualityCheckResult.from_dict(item)
            for item in values.get("revalidation_results", [])
        ]
        return cls(**values)


@dataclass(slots=True)
class RunCounters:
    generation_count: int = 0
    validation_count: int = 0
    repair_attempt_count: int = 0
    content_repair_count: int = 0
    accepted_content_repair_count: int = 0
    upstream_redo_count: int = 0
    infrastructure_retry_count: int = 0
    no_op_count: int = 0
    rollback_count: int = 0
    repeated_error_count: int = 0

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if type(value) is not int or value < 0:
                raise ValueError(f"counter {name} must be a non-negative integer")
        if self.accepted_content_repair_count > self.content_repair_count:
            raise ValueError("accepted content repairs cannot exceed content repairs")


@dataclass(slots=True)
class InfrastructureRetryRecord:
    attempt: int
    failed_check: str
    error_category: str
    failure_fingerprint: str
    artifact_bundle_sha256: str
    original_evidence: dict[str, Any]
    revalidation_results: list[QualityCheckResult]
    outcome: InfrastructureRetryOutcome

    def __post_init__(self) -> None:
        self.outcome = InfrastructureRetryOutcome(self.outcome)
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("infrastructure retry attempt numbers start at 1")
        if not self.failed_check.strip() or not self.error_category.strip():
            raise ValueError("infrastructure retry classification must not be empty")
        if not _is_sha256(self.failure_fingerprint):
            raise ValueError("failure_fingerprint must be a SHA-256 digest")
        if not _is_sha256(self.artifact_bundle_sha256):
            raise ValueError("artifact_bundle_sha256 must be a SHA-256 digest")
        if not self.revalidation_results:
            raise ValueError("infrastructure retries must record revalidation results")
        check_names = [item.check_type for item in self.revalidation_results]
        if len(check_names) != len(set(check_names)):
            raise ValueError("infrastructure revalidation checks must be unique")


@dataclass(slots=True)
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    retrieval_tokens: int = 0

    def __post_init__(self) -> None:
        for name in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "retrieval_tokens",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


@dataclass(slots=True)
class ModelConfiguration:
    provider: str = ""
    model: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ReproductionManifest:
    git_commit: str = ""
    prompt_sha256: str = ""
    case_sha256: str = ""
    corpus_sha256: str = ""
    tool_versions: dict[str, str] = field(default_factory=dict)
    budgets: dict[str, Any] = field(default_factory=dict)
    judge_configuration: dict[str, Any] = field(default_factory=dict)
    generated_at: str = field(default_factory=utc_now_iso)


@dataclass(slots=True)
class RunResult:
    """The common V4/V6/V8 result contract."""

    version: str
    case_id: str
    run_id: str
    requirement_text: str
    parameters: list[NormalizedParameter] = field(default_factory=list)
    artifacts: dict[str, ArtifactRecord] = field(default_factory=dict)
    checks: list[QualityCheckResult] = field(default_factory=list)
    repairs: list[RepairRecord] = field(default_factory=list)
    infrastructure_retries: list[InfrastructureRetryRecord] = field(default_factory=list)
    repair_stop_reason: RepairStopReason = RepairStopReason.NONE
    overall_success: bool = False
    primary_failure_category: str = ""
    primary_failure_evidence: dict[str, Any] = field(default_factory=dict)
    resolved_by: str = ""
    counters: RunCounters = field(default_factory=RunCounters)
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    duration_s: float = 0.0
    model_configuration: ModelConfiguration = field(default_factory=ModelConfiguration)
    retrieval_triggered: bool = False
    retrieval_fragment_ids: list[str] = field(default_factory=list)
    reproduction: ReproductionManifest = field(default_factory=ReproductionManifest)
    infrastructure_errors: list[str] = field(default_factory=list)
    raw_metrics: dict[str, Any] = field(default_factory=dict)
    routing: RoutingTrace = field(default_factory=RoutingTrace)
    budget_ledger: BudgetLedger | None = None
    traceability: TraceabilityReport | None = None
    execution_contract: str = "unspecified"
    schema_version: str = SCHEMA_VERSION
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        self.repair_stop_reason = RepairStopReason(self.repair_stop_reason)
        if not self.version.strip() or not self.case_id.strip() or not self.run_id.strip():
            raise ValueError("version, case_id, and run_id are required")
        if self.duration_s < 0:
            raise ValueError("duration_s must be non-negative")
        check_names = [check.check_type for check in self.checks]
        if len(check_names) != len(set(check_names)):
            raise ValueError("final RunResult checks must have unique check_type values")
        for key, artifact in self.artifacts.items():
            if key != artifact.artifact_type:
                raise ValueError(
                    f"artifact mapping key {key!r} does not match type {artifact.artifact_type!r}"
                )
            if artifact.content and artifact.sha256 != sha256_text(artifact.content):
                raise ValueError(
                    f"artifact {key!r} content changed after its hash was recorded"
                )
        requirement_artifact = self.artifacts.get("requirement")
        if (
            requirement_artifact is not None
            and requirement_artifact.content
            and self.requirement_text != requirement_artifact.content
        ):
            raise ValueError(
                "requirement_text must equal the final requirement artifact content"
            )
        if self.version.lower() == "v8":
            if [repair.attempt for repair in self.repairs] != list(
                range(1, len(self.repairs) + 1)
            ):
                raise ValueError("V8 repair attempt numbers must be consecutive")
            if [retry.attempt for retry in self.infrastructure_retries] != list(
                range(1, len(self.infrastructure_retries) + 1)
            ):
                raise ValueError("V8 infrastructure retry numbers must be consecutive")
            changed_modelica = sum(
                1
                for repair in self.repairs
                if repair.repair_layer is RepairLayer.MODELICA and not repair.is_noop
            )
            accepted_modelica = sum(
                1
                for repair in self.repairs
                if repair.repair_layer is RepairLayer.MODELICA
                and not repair.is_noop
                and repair.accepted
            )
            changed_upstream = sum(
                1
                for repair in self.repairs
                if repair.repair_layer in {
                    RepairLayer.REQUIREMENT,
                    RepairLayer.SYSML,
                }
                and not repair.is_noop
            )
            expected_counters = {
                "repair_attempt_count": len(self.repairs),
                "content_repair_count": changed_modelica,
                "accepted_content_repair_count": accepted_modelica,
                "upstream_redo_count": changed_upstream,
                "infrastructure_retry_count": len(self.infrastructure_retries),
                "no_op_count": sum(repair.is_noop for repair in self.repairs),
                "rollback_count": sum(repair.rolled_back for repair in self.repairs),
                "repeated_error_count": sum(
                    repair.stop_reason is RepairStopReason.REPEATED_ERROR
                    for repair in self.repairs
                ),
            }
            for name, expected in expected_counters.items():
                if getattr(self.counters, name) != expected:
                    raise ValueError(
                        f"V8 counter {name} does not match auditable records"
                    )
            minimum_validations = (
                (1 if self.repairs or self.infrastructure_retries else 0)
                + sum(not repair.is_noop for repair in self.repairs)
                + len(self.infrastructure_retries)
            )
            if self.counters.validation_count < minimum_validations:
                raise ValueError("V8 validation_count is lower than the evidence permits")
            for artifact_type in {repair.artifact_type for repair in self.repairs}:
                final_record = next(
                    repair
                    for repair in reversed(self.repairs)
                    if repair.artifact_type == artifact_type
                )
                artifact = self.artifacts.get(artifact_type)
                if artifact is None or artifact.sha256 != final_record.effective_after_sha256:
                    raise ValueError(
                        f"final {artifact_type} artifact is not bound to repair evidence"
                    )
            if (
                self.repair_stop_reason is RepairStopReason.REPEATED_ERROR
                and self.counters.repeated_error_count < 1
            ):
                raise ValueError("repeated_error stop requires a repeated error count")
            if self.repair_stop_reason is RepairStopReason.NO_OP and (
                not self.repairs or not self.repairs[-1].is_noop
            ):
                raise ValueError("no_op stop requires a final no-op repair record")
            if self.repair_stop_reason is RepairStopReason.REGRESSION and (
                not self.repairs or not self.repairs[-1].rolled_back
            ):
                raise ValueError("regression stop requires a rolled-back repair record")
            if self.repair_stop_reason is RepairStopReason.ALL_CHECKS_PASS:
                required = [check for check in self.checks if check.required]
                if not required or any(
                    check.status is not CheckStatus.PASS for check in required
                ):
                    raise ValueError("all_checks_pass stop requires every hard check to PASS")
            required = [check for check in self.checks if check.required]
            gate_success = bool(required) and all(
                check.status is CheckStatus.PASS for check in required
            )
            if self.overall_success is not gate_success:
                raise ValueError("V8 overall_success must equal the hard-check decision")
            if gate_success and self.repair_stop_reason is not RepairStopReason.ALL_CHECKS_PASS:
                raise ValueError(
                    "a successful V8 run must stop because all checks pass"
                )
            if self.retrieval_triggered:
                if (
                    not self.retrieval_fragment_ids
                    or len(self.retrieval_fragment_ids)
                    != len(set(self.retrieval_fragment_ids))
                    or self.token_usage.retrieval_tokens <= 0
                    or not re.fullmatch(r"[0-9a-f]{64}", self.reproduction.corpus_sha256)
                ):
                    raise ValueError(
                        "triggered V8 retrieval requires unique fragments, token usage, and corpus hash"
                    )
                retrieval_decision = self.raw_metrics.get("retrieval_decision")
                if not isinstance(retrieval_decision, Mapping) or (
                    retrieval_decision.get("triggered") is not True
                    or retrieval_decision.get("fragment_ids")
                    != self.retrieval_fragment_ids
                    or retrieval_decision.get("estimated_tokens")
                    != self.token_usage.retrieval_tokens
                    or retrieval_decision.get("corpus_sha256")
                    != self.reproduction.corpus_sha256
                ):
                    raise ValueError(
                        "triggered V8 retrieval fields must match raw retrieval evidence"
                    )
                if self.overall_success and not self.resolved_by.startswith(
                    "retrieval_assisted_"
                ):
                    raise ValueError(
                        "triggered successful retrieval must be attributed in resolved_by"
                    )
                if not self.overall_success and self.resolved_by.startswith(
                    "retrieval_assisted_"
                ):
                    raise ValueError(
                        "failed retrieval cannot claim retrieval-assisted resolution"
                    )
            elif self.retrieval_fragment_ids or self.token_usage.retrieval_tokens:
                raise ValueError(
                    "non-triggered V8 retrieval cannot claim fragments or retrieval tokens"
                )
            elif self.resolved_by.startswith("retrieval_assisted_"):
                raise ValueError(
                    "non-triggered V8 run cannot claim retrieval-assisted resolution"
                )
            self._validate_stage4_evidence()
        if self.overall_success:
            required = [check for check in self.checks if check.required]
            if not required or any(check.status is not CheckStatus.PASS for check in required):
                raise ValueError(
                    "overall_success=True is inconsistent with the required final checks"
                )

    def _validate_stage4_evidence(self) -> None:
        if not isinstance(self.routing, RoutingTrace):
            raise TypeError("routing must be a RoutingTrace")
        trace_checks = [
            item for item in self.checks if item.check_type == "traceability_integrity"
        ]
        if bool(trace_checks) != (self.traceability is not None):
            raise ValueError(
                "traceability report and traceability_integrity check must co-exist"
            )
        if not isinstance(self.execution_contract, str) or not self.execution_contract.strip():
            raise ValueError("execution_contract must be a non-empty string")
        adaptive_contract = self.execution_contract == "certified_template_adaptive_v1"
        if adaptive_contract and (
            self.model_configuration.model != "certified_template_adaptive_path"
        ):
            raise ValueError("adaptive execution contract and model configuration differ")
        if adaptive_contract and (
            self.routing.path is RoutingPath.NOT_EVALUATED
            or self.budget_ledger is None
        ):
            raise ValueError(
                "the certified adaptive pipeline requires routing and budget evidence"
            )
        trace_required = self.model_configuration.parameters.get(
            "traceability_required", False
        )
        if type(trace_required) is not bool:
            raise TypeError("traceability_required must be a bool")
        if adaptive_contract and trace_required and self.traceability is None:
            raise ValueError("the selected adaptive pipeline requires traceability evidence")
        if self.routing.path is not RoutingPath.NOT_EVALUATED:
            if self.routing.retrieval_triggered is not self.retrieval_triggered:
                raise ValueError("routing retrieval flag does not match RunResult")
            if self.routing.repair_attempted is not bool(self.repairs):
                raise ValueError("routing repair flag does not match repair records")
            if self.routing.infrastructure_retry_attempted is not bool(
                self.infrastructure_retries
            ):
                raise ValueError(
                    "routing infrastructure flag does not match retry records"
                )
            raw_online = self.raw_metrics.get("online_api_used")
            if type(raw_online) is not bool or raw_online is not self.routing.online_api_used:
                raise ValueError("routing online flag does not match raw execution evidence")
            raw_natural_language = self.raw_metrics.get(
                "natural_language_interpretation_performed"
            )
            if (
                type(raw_natural_language) is not bool
                or raw_natural_language
                is not self.routing.natural_language_interpretation_performed
            ):
                raise ValueError(
                    "routing natural-language flag does not match raw execution evidence"
                )
            generation_evidence = self.raw_metrics.get(
                "natural_language_generation"
            )
            generation_evidence_sha256 = self.raw_metrics.get(
                "natural_language_generation_sha256", ""
            )
            if self.routing.natural_language_interpretation_performed:
                if (
                    self.routing.input_kind is not RoutingInputKind.RAW_NATURAL_LANGUAGE
                    or not isinstance(generation_evidence, Mapping)
                    or generation_evidence.get("ready") is not True
                    or generation_evidence.get("requirement_sha256")
                    != sha256_text(self.requirement_text)
                    or self.routing.events[0].input_snapshot_sha256
                    != generation_evidence.get("requirement_sha256")
                    or generation_evidence.get("template_id")
                    != self.routing.certified_template_id
                    or generation_evidence.get("provider")
                    != self.model_configuration.provider
                    or generation_evidence.get("online_api_used") is not raw_online
                    or generation_evidence.get("total_tokens")
                    != self.token_usage.total_tokens
                ):
                    raise ValueError(
                        "natural-language generation evidence is inconsistent"
                    )
                expected_generation_sha256 = sha256_text(
                    json.dumps(
                        dict(generation_evidence),
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                if (
                    generation_evidence_sha256 != expected_generation_sha256
                    or self.model_configuration.parameters.get(
                        "natural_language_generation"
                    )
                    != generation_evidence
                    or self.model_configuration.parameters.get(
                        "natural_language_generation_sha256"
                    )
                    != expected_generation_sha256
                    or generation_evidence.get("traceability_input_sha256")
                    != self.raw_metrics.get("traceability_input_sha256")
                ):
                    raise ValueError(
                        "natural-language generation hash or trace binding is inconsistent"
                    )
            elif generation_evidence is not None or generation_evidence_sha256:
                raise ValueError(
                    "non-natural-language routing cannot claim generation evidence"
                )
            route_succeeded = self.routing.final_state is RoutingState.SUCCEEDED
            if route_succeeded is not self.overall_success:
                raise ValueError("routing terminal state does not match hard-gate success")
            if not self.overall_success and self.resolved_by:
                raise ValueError("a failed evaluated route cannot claim a resolution")
            if self.routing.path is RoutingPath.FAST_PATH:
                if (
                    self.counters.validation_count != 1
                    or self.repairs
                    or self.infrastructure_retries
                    or self.retrieval_triggered
                    or self.token_usage.prompt_tokens
                    or self.token_usage.completion_tokens
                    or self.token_usage.retrieval_tokens
                    or self.resolved_by != "initial_hard_gate"
                ):
                    raise ValueError("fast-path evidence contains an escalation or model cost")
                final_by_name = {
                    item.check_type: item
                    for item in self.checks
                    if item.check_type != "traceability_integrity"
                }
                if set(final_by_name) != {
                    item.check_type for item in self.routing.initial_checks
                }:
                    raise ValueError("fast-path initial and final check profiles differ")
                for item in self.routing.initial_checks:
                    final_check = final_by_name[item.check_type]
                    if (
                        item.status is not final_check.status
                        or item.required is not final_check.required
                        or item.semantic_evidence_sha256
                        != quality_check_semantic_sha256(final_check)
                    ):
                        raise ValueError("fast-path check evidence changed after routing")
                final_bundle_sha256 = self.raw_metrics.get("artifact_bundle_sha256")
                if final_bundle_sha256 != self.routing.initial_artifact_bundle_sha256:
                    raise ValueError("fast-path artifact snapshot changed after routing")
            elif self.overall_success:
                expected_resolution = (
                    "retrieval_assisted_deterministic_offline_repair"
                    if self.retrieval_triggered and self.repairs
                    else (
                        "retrieval_assisted_infrastructure_retry"
                        if self.retrieval_triggered and self.infrastructure_retries
                        else (
                            "retrieval_assisted_parameter_normalization"
                            if self.retrieval_triggered
                            else (
                                "deterministic_offline_repair"
                                if self.repairs
                                else (
                                "infrastructure_retry"
                                if self.infrastructure_retries
                                else (
                                    "initial_hard_gate_after_retrieval_no_match"
                                    if self.routing.retrieval_attempted
                                    else (
                                        "initial_hard_gate_after_natural_language_interpretation"
                                        if self.routing.natural_language_interpretation_performed
                                        else ""
                                    )
                                )
                            )
                        )
                    )
                    )
                )
                if self.resolved_by != expected_resolution:
                    raise ValueError("escalation resolution is inconsistent with route evidence")
            final_bundle_sha256 = _run_result_artifact_bundle_sha256(self.artifacts)
            if self.raw_metrics.get("artifact_bundle_sha256") != final_bundle_sha256:
                raise ValueError("final artifact bundle hash is not bound to RunResult artifacts")
            final_event = self.routing.events[-1]
            if final_event.artifact_bundle_sha256 != final_bundle_sha256:
                raise ValueError("routing terminal event uses a stale artifact bundle")
            expected_terminal_validation = (
                self.routing.initial_validation_sha256
                if self.routing.path is RoutingPath.FAST_PATH
                else routing_validation_sha256(self.checks)
            )
            if final_event.validation_sha256 != expected_terminal_validation:
                raise ValueError("routing terminal event uses stale validation evidence")

        if self.budget_ledger is not None:
            if not isinstance(self.budget_ledger, BudgetLedger):
                raise TypeError("budget_ledger must be a BudgetLedger")
            expected_usage = {
                BudgetDimension.VALIDATION: self.counters.validation_count,
                BudgetDimension.REPAIR_ATTEMPT: len(self.repairs),
                BudgetDimension.MODELICA_CHANGED_REPAIR: self.counters.content_repair_count,
                BudgetDimension.UPSTREAM_CHANGED_REPAIR: self.counters.upstream_redo_count,
                BudgetDimension.INFRASTRUCTURE_RETRY: len(self.infrastructure_retries),
                BudgetDimension.RETRIEVAL_EVENT: int(
                    bool(
                        isinstance(self.raw_metrics.get("retrieval_decision"), Mapping)
                        and self.raw_metrics["retrieval_decision"].get("attempted") is True
                    )
                ),
                BudgetDimension.RETRIEVAL_CONTEXT_TOKEN: self.token_usage.retrieval_tokens,
            }
            for dimension, expected in expected_usage.items():
                if self.budget_ledger.usage(dimension) != expected:
                    raise ValueError(f"budget ledger {dimension.value} usage is inconsistent")
            if self.budget_ledger.observed_model_tokens != self.token_usage.total_tokens:
                raise ValueError("budget ledger model tokens do not match TokenUsage")
            if self.budget_ledger.observed_duration_s != self.duration_s:
                raise ValueError("budget ledger duration does not match RunResult")
            ledger_limits = {
                item.dimension.value: item.limit for item in self.budget_ledger.limits
            }
            if ledger_limits and any(
                self.reproduction.budgets.get(name) != value
                for name, value in ledger_limits.items()
            ):
                raise ValueError("reproduction budgets do not match the budget ledger")
            if adaptive_contract:
                required_budget_keys = {
                    "max_repairs",
                    "max_content_repairs",
                    "max_upstream_redos",
                    "max_infrastructure_retries",
                    BudgetDimension.RETRIEVAL_EVENT.value,
                    BudgetDimension.RETRIEVAL_CONTEXT_TOKEN.value,
                }
                if any(
                    type(self.reproduction.budgets.get(name)) is not int
                    or self.reproduction.budgets[name] < 0
                    for name in required_budget_keys
                ):
                    raise ValueError("adaptive reproduction budgets are incomplete")
                expected_limits = {
                    BudgetDimension.VALIDATION.value: (
                        1
                        + self.reproduction.budgets["max_repairs"]
                        + self.reproduction.budgets["max_infrastructure_retries"]
                    ),
                    BudgetDimension.REPAIR_ATTEMPT.value: self.reproduction.budgets[
                        "max_repairs"
                    ],
                    BudgetDimension.MODELICA_CHANGED_REPAIR.value: (
                        self.reproduction.budgets["max_content_repairs"]
                    ),
                    BudgetDimension.UPSTREAM_CHANGED_REPAIR.value: (
                        self.reproduction.budgets["max_upstream_redos"]
                    ),
                    BudgetDimension.INFRASTRUCTURE_RETRY.value: (
                        self.reproduction.budgets["max_infrastructure_retries"]
                    ),
                    BudgetDimension.RETRIEVAL_EVENT.value: self.reproduction.budgets[
                        BudgetDimension.RETRIEVAL_EVENT.value
                    ],
                    BudgetDimension.RETRIEVAL_CONTEXT_TOKEN.value: (
                        self.reproduction.budgets[
                            BudgetDimension.RETRIEVAL_CONTEXT_TOKEN.value
                        ]
                    ),
                }
                if ledger_limits != expected_limits:
                    raise ValueError("adaptive budget limits are not policy-derived")
                validation_events = [
                    item
                    for item in self.budget_ledger.events
                    if item.dimension is BudgetDimension.VALIDATION
                ]
                if not validation_events or (
                    validation_events[0].subject_sha256
                    != self.routing.initial_validation_sha256
                ):
                    raise ValueError(
                        "budget validation ledger is not bound to initial routing evidence"
                    )
                final_base_checks = [
                    item
                    for item in self.checks
                    if item.check_type != "traceability_integrity"
                ]
                if validation_events[-1].subject_sha256 != routing_validation_sha256(
                    final_base_checks
                ):
                    raise ValueError(
                        "budget validation ledger is not bound to final base checks"
                    )

        if self.traceability is not None:
            if not isinstance(self.traceability, TraceabilityReport):
                raise TypeError("traceability must be a TraceabilityReport")
            required_artifacts = {
                "requirement": self.traceability.requirement_artifact_sha256,
                "sysml": self.traceability.sysml_artifact_sha256,
                "modelica": self.traceability.modelica_artifact_sha256,
            }
            for artifact_type, expected_sha256 in required_artifacts.items():
                artifact = self.artifacts.get(artifact_type)
                if artifact is None or artifact.sha256 != expected_sha256:
                    raise ValueError(
                        f"traceability is not bound to the final {artifact_type} artifact"
                    )
            trace_check = next(
                (
                    item
                    for item in self.checks
                    if item.check_type == "traceability_integrity"
                ),
                None,
            )
            if trace_check is None or not trace_check.required:
                raise ValueError("a traceability report requires a hard integrity check")
            expected_trace_status = (
                CheckStatus.PASS
                if self.traceability.integrity_passed
                else CheckStatus.FAIL
            )
            if trace_check.status is not expected_trace_status:
                raise ValueError("traceability report and integrity check disagree")
            if trace_check.evidence.get("report_sha256") != traceability_report_sha256(
                self.traceability
            ):
                raise ValueError("traceability integrity check is not bound to the report")
            if (
                trace_check.evidence.get("trace_input_sha256")
                != self.traceability.trace_input_sha256
                or self.raw_metrics.get("traceability_input_sha256")
                != self.traceability.trace_input_sha256
            ):
                raise ValueError("traceability evidence is not bound to its explicit input")
            final_checks = {item.check_type: item for item in self.checks}
            parameter_hashes = {
                f"parameter:{item.canonical_name}": normalized_parameter_semantic_sha256(
                    item
                )
                for item in self.parameters
            }
            requirement_content = self.artifacts["requirement"].content
            sysml_content = self.artifacts["sysml"].content
            modelica_content = self.artifacts["modelica"].content
            if any(not value for value in (requirement_content, sysml_content, modelica_content)):
                raise ValueError("traceability requires inline final artifact content")

            integrity_check = final_checks.get("certified_template_integrity")
            anchor_check = final_checks.get("template_certification_anchor")
            if integrity_check is None or anchor_check is None:
                raise ValueError("traceability requires certified template anchor checks")
            configured_template_id = self.model_configuration.parameters.get("template_id")
            if configured_template_id != self.traceability.template_id:
                raise ValueError("trace template id differs from the adaptive configuration")
            anchor_template_sha256 = anchor_check.evidence.get(
                "template_sha256",
                anchor_check.evidence.get("current_template_sha256", ""),
            )
            if anchor_template_sha256 and (
                anchor_template_sha256 != self.traceability.template_sha256
            ):
                raise ValueError("trace template hash differs from certification evidence")
            if (
                anchor_check.status is CheckStatus.PASS
                and not anchor_template_sha256
            ):
                raise ValueError("passing certification evidence lacks a template hash")
            if integrity_check.status is CheckStatus.PASS and (
                integrity_check.evidence.get("template_id")
                != self.traceability.template_id
                or integrity_check.evidence.get("template_sha256")
                != self.traceability.template_sha256
                or integrity_check.evidence.get("rendered_sha256")
                != self.traceability.modelica_artifact_sha256
            ):
                raise ValueError("trace template identity differs from integrity evidence")

            for link in self.traceability.links:
                if requirement_content[
                    link.requirement_source_start : link.requirement_source_end
                ] != link.requirement_text:
                    raise ValueError(
                        "trace requirement span does not match the final requirement"
                    )
                expected_parameter_hash = parameter_hashes.get(link.target_id)
                if expected_parameter_hash is not None and (
                    link.status is not TraceLinkStatus.NOT_IMPLEMENTED
                    and link.normalized_parameter_sha256 != expected_parameter_hash
                ):
                    raise ValueError(
                        "trace parameter target is not bound to its final normalized parameter"
                    )
                if (
                    expected_parameter_hash is None
                    and link.normalized_parameter_sha256
                ):
                    raise ValueError("non-parameter trace target claims parameter evidence")
                for element in link.sysml_elements:
                    if element.artifact_sha256 != self.traceability.sysml_artifact_sha256:
                        raise ValueError("SysML trace element uses a stale artifact hash")
                    if sysml_content.count(element.exact_selector) != element.cardinality:
                        raise ValueError(
                            "SysML trace selector cardinality differs from final artifact"
                        )
                for element in link.modelica_elements:
                    if element.artifact_sha256 != self.traceability.modelica_artifact_sha256:
                        raise ValueError("Modelica trace element uses a stale artifact hash")
                    if modelica_content.count(element.exact_selector) != element.cardinality:
                        raise ValueError(
                            "Modelica trace selector cardinality differs from final artifact"
                        )
                for reference in link.check_references:
                    check = final_checks.get(reference.check_type)
                    if check is None or (
                        reference.status is not check.status
                        or reference.required is not check.required
                        or reference.semantic_evidence_sha256
                        != quality_check_semantic_sha256(check)
                    ):
                        raise ValueError("trace check reference does not match final evidence")

    def to_dict(self) -> dict[str, Any]:
        # Re-run every cross-record invariant so post-construction mutation of
        # legacy mutable fields cannot be serialized as valid Stage-4 evidence.
        self.__post_init__()
        return _json_ready(asdict(self))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RunResult":
        values = dict(data)
        values["parameters"] = [
            NormalizedParameter.from_dict(item) for item in values.get("parameters", [])
        ]
        values["artifacts"] = {
            key: ArtifactRecord.from_dict(item)
            for key, item in values.get("artifacts", {}).items()
        }
        values["checks"] = [
            QualityCheckResult.from_dict(item) for item in values.get("checks", [])
        ]
        values["repairs"] = [
            RepairRecord.from_dict(item) for item in values.get("repairs", [])
        ]
        values["infrastructure_retries"] = [
            InfrastructureRetryRecord(
                **{
                    **item,
                    "revalidation_results": [
                        QualityCheckResult.from_dict(check)
                        for check in item.get("revalidation_results", [])
                    ],
                }
            )
            for item in values.get("infrastructure_retries", [])
        ]
        values["counters"] = RunCounters(**values.get("counters", {}))
        values["token_usage"] = TokenUsage(**values.get("token_usage", {}))
        values["model_configuration"] = ModelConfiguration(
            **values.get("model_configuration", {})
        )
        values["reproduction"] = ReproductionManifest(**values.get("reproduction", {}))
        values["routing"] = RoutingTrace.from_dict(values.get("routing", {}))
        ledger = values.get("budget_ledger")
        values["budget_ledger"] = (
            BudgetLedger.from_dict(ledger) if ledger is not None else None
        )
        traceability = values.get("traceability")
        values["traceability"] = (
            TraceabilityReport.from_dict(traceability)
            if traceability is not None
            else None
        )
        return cls(**values)


def quality_check_semantic_sha256(check: QualityCheckResult) -> str:
    """Hash stable check semantics while excluding timing and run timestamps."""

    if not isinstance(check, QualityCheckResult):
        raise TypeError("check must be a QualityCheckResult")
    return _canonical_json_sha256(
        {
            "check_type": check.check_type,
            "status": check.status.value,
            "required": check.required,
            "issues": list(check.issues),
            "evidence": dict(check.evidence),
            "tool_name": check.tool_name,
            "tool_version": check.tool_version,
            "failure_kind": check.failure_kind.value,
            "failure_category": check.failure_category,
        }
    )


def routing_validation_sha256(
    checks: Iterable[QualityCheckResult | RoutingCheckSnapshot],
) -> str:
    """Hash a check profile independent of validator output ordering."""

    payload: list[dict[str, Any]] = []
    for item in checks:
        if isinstance(item, QualityCheckResult):
            payload.append(
                {
                    "check_type": item.check_type,
                    "status": item.status.value,
                    "required": item.required,
                    "failure_kind": item.failure_kind.value,
                    "failure_category": item.failure_category,
                    "semantic_evidence_sha256": quality_check_semantic_sha256(item),
                }
            )
        elif isinstance(item, RoutingCheckSnapshot):
            payload.append(
                {
                    "check_type": item.check_type,
                    "status": item.status.value,
                    "required": item.required,
                    "failure_kind": item.failure_kind.value,
                    "failure_category": item.failure_category,
                    "semantic_evidence_sha256": item.semantic_evidence_sha256,
                }
            )
        else:
            raise TypeError("routing validation accepts only check records")
    names = [item["check_type"] for item in payload]
    if len(names) != len(set(names)):
        raise ValueError("routing validation checks must be unique")
    return _canonical_json_sha256(sorted(payload, key=lambda item: item["check_type"]))


def routing_event_sha256(event: RoutingEvent) -> str:
    return _canonical_json_sha256(
        {
            "sequence": event.sequence,
            "from_state": event.from_state.value,
            "to_state": event.to_state.value,
            "action": event.action.value,
            "reason_code": event.reason_code,
            "input_snapshot_sha256": event.input_snapshot_sha256,
            "artifact_bundle_sha256": event.artifact_bundle_sha256,
            "validation_sha256": event.validation_sha256,
            "previous_event_sha256": event.previous_event_sha256,
        }
    )


def budget_event_sha256(event: BudgetEvent) -> str:
    return _canonical_json_sha256(
        {
            "sequence": event.sequence,
            "dimension": event.dimension.value,
            "amount": event.amount,
            "action": event.action,
            "subject_sha256": event.subject_sha256,
            "before": event.before,
            "after": event.after,
            "previous_event_sha256": event.previous_event_sha256,
        }
    )


def traceability_report_sha256(report: TraceabilityReport) -> str:
    if not isinstance(report, TraceabilityReport):
        raise TypeError("report must be a TraceabilityReport")
    return _canonical_json_sha256(asdict(report))


def trace_input_semantic_sha256(
    requirement_artifact_sha256: str,
    links: Iterable[TraceLink],
) -> str:
    """Reconstruct the explicit trace input contract persisted by a report."""

    if not _is_sha256(requirement_artifact_sha256):
        raise ValueError("trace input requires a requirement artifact SHA-256")
    link_tuple = tuple(links)
    if not link_tuple or any(not isinstance(item, TraceLink) for item in link_tuple):
        raise TypeError("trace input reconstruction requires TraceLink records")
    return _canonical_json_sha256(
        {
            "schema_version": "v8-traceability-input-1",
            "requirement_artifact_sha256": requirement_artifact_sha256,
            "requirements": [
                {
                    "requirement_id": item.requirement_id,
                    "text": item.requirement_text,
                    "source_start": item.requirement_source_start,
                    "source_end": item.requirement_source_end,
                    "critical": item.critical,
                    "not_implemented_reason": item.not_implemented_reason,
                }
                for item in link_tuple
            ],
            "bindings": [
                {
                    "requirement_id": item.requirement_id,
                    "target_id": item.target_id,
                    "basis": item.binding_basis,
                }
                for item in link_tuple
            ],
        }
    )


def normalized_parameter_semantic_sha256(parameter: NormalizedParameter) -> str:
    """Hash one normalized parameter including its status and provenance."""

    if not isinstance(parameter, NormalizedParameter):
        raise TypeError("parameter must be a NormalizedParameter")
    return _canonical_json_sha256(asdict(parameter))


def _run_result_artifact_bundle_sha256(
    artifacts: Mapping[str, ArtifactRecord],
) -> str:
    required = ("requirement", "sysml", "modelica")
    if any(name not in artifacts or not artifacts[name].content for name in required):
        raise ValueError(
            "evaluated adaptive routing requires all three inline final artifacts"
        )
    return _canonical_json_sha256(
        {name: artifacts[name].content for name in required}
    )


def _canonical_json_sha256(payload: Any) -> str:
    return sha256_text(
        json.dumps(
            _json_ready(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def _validate_budget_status(
    configured: int | float | None,
    observed: int | float,
    status: BudgetEvaluationStatus,
    label: str,
) -> None:
    if status is BudgetEvaluationStatus.NOT_APPLICABLE:
        return
    if configured is None:
        if status is not BudgetEvaluationStatus.NOT_FROZEN:
            raise ValueError(f"{label} budget without a frozen limit must be not_frozen")
        return
    expected = (
        BudgetEvaluationStatus.PASS
        if observed <= configured
        else BudgetEvaluationStatus.FAIL
    )
    if status is not expected:
        raise ValueError(f"{label} budget status does not match observed usage")


def index_checks(
    checks: Iterable[QualityCheckResult],
) -> dict[str, QualityCheckResult]:
    """Index final checks and reject ambiguous duplicate evidence."""

    indexed: dict[str, QualityCheckResult] = {}
    for check in checks:
        if check.check_type in indexed:
            raise ValueError(f"duplicate check_type: {check.check_type}")
        indexed[check.check_type] = check
    return indexed


def _json_ready(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite floats are not valid V8 JSON values")
        return value
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("V8 JSON object keys must be strings")
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    raise TypeError(f"unsupported V8 JSON value: {type(value).__name__}")
