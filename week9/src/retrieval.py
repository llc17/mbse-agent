"""Small, deterministic and fail-closed retrieval for V8 Stage 3.

The retriever is deliberately not a vector service.  It loads a frozen JSON
corpus, verifies every fragment against a certified template and its source
file, applies metadata filters before lexical ranking, and enforces immutable
per-fragment and total context budgets.  It never edits artifacts itself.

The default policy is disabled.  A caller must also prove that a real content
failure exists, local resolution has already been attempted, and the query is
bound to the exact failed snapshot.  Infrastructure failures and broad
"something went wrong" queries fail closed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence
import unicodedata


RETRIEVER_VERSION = "v8-filtered-lexical-1"
TOKEN_ESTIMATOR_ID = "unicode-regex-estimator-1"
CORPUS_SCHEMA_VERSION = "v8-retrieval-corpus-1"
POLICY_SCHEMA_VERSION = "v8-retrieval-policy-1"

WEEK9_ROOT = Path(__file__).resolve().parents[1]
CONFIG_ROOT = WEEK9_ROOT / "configs"
DEFAULT_CORPUS_PATH = CONFIG_ROOT / "retrieval_corpus.json"
DEFAULT_CORPUS_HASH_PATH = CONFIG_ROOT / "retrieval_corpus.sha256"
DEFAULT_POLICY_PATH = CONFIG_ROOT / "retrieval_policy.json"
DEFAULT_CERTIFICATION_PATH = CONFIG_ROOT / "template_certifications.json"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")
_TOKEN_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_./:-]*|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|"
    r"[\u3400-\u4dbf\u4e00-\u9fff]|[^\s]"
)
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]*|\d+(?:\.\d+)?|[\u3400-\u4dbf\u4e00-\u9fff]")

_ALLOWED_DOMAINS = frozenset({"common", "electrical", "thermal"})
_ALLOWED_LANGUAGES = frozenset({"modelica", "sysml", "structured_parameter"})
_ALLOWED_REPAIR_LAYERS = frozenset({"requirement", "sysml", "modelica"})
_ALLOWED_VALIDATION_STATUSES = frozenset(
    {"compile_verified", "behavior_verified", "certified"}
)
_VALIDATION_RANK = MappingProxyType(
    {"compile_verified": 1, "behavior_verified": 2, "certified": 3}
)
_FORBIDDEN_SOURCE_PARTS = frozenset(
    {"results", "holdout", "answers", "judge", "formal_outputs"}
)
_FORBIDDEN_SOURCE_FILES = frozenset({"cases.py"})
_APPROVED_FRAGMENT_SOURCES = frozenset({"src/modelica_templates.py"})
_APPROVED_SOURCE_KINDS = frozenset({"certified_template_contract"})
_REQUIRED_RETRIEVAL_ANCHOR_FILES = frozenset(
    {
        "src/parameter_normalization.py",
        "src/retrieval.py",
        "src/pipeline.py",
        "configs/retrieval_corpus.json",
        "configs/retrieval_corpus.sha256",
        "configs/retrieval_policy.json",
    }
)
_INFRASTRUCTURE_FAILURE_KINDS = frozenset(
    {"infrastructure", "tooling", "timeout", "file_lock", "validator_error"}
)


class RetrievalConfigurationError(ValueError):
    """Raised when corpus, policy or query evidence is unsafe or inconsistent."""


@dataclass(frozen=True, slots=True)
class FragmentSource:
    kind: str
    path: str
    version: str
    library: str
    library_version: str
    locator: str
    source_sha256: str

    def __post_init__(self) -> None:
        for name in ("kind", "path", "version", "library", "library_version", "locator"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise RetrievalConfigurationError(f"fragment source {name} is required")
        _validate_relative_source_path(self.path)
        if self.path not in _APPROVED_FRAGMENT_SOURCES:
            raise RetrievalConfigurationError(
                f"fragment source is not explicitly approved: {self.path}"
            )
        if self.kind not in _APPROVED_SOURCE_KINDS:
            raise RetrievalConfigurationError(
                f"fragment source kind is not approved: {self.kind}"
            )
        _validate_sha256(self.source_sha256, "fragment source_sha256")


@dataclass(frozen=True, slots=True)
class FragmentValidation:
    status: str
    template_id: str
    template_sha256: str
    check_types: tuple[str, ...]
    manifest_path: str = "configs/template_certifications.json"

    def __post_init__(self) -> None:
        if self.status not in _ALLOWED_VALIDATION_STATUSES:
            raise RetrievalConfigurationError(
                f"unsupported fragment validation status: {self.status}"
            )
        if not self.template_id.strip():
            raise RetrievalConfigurationError("validation template_id is required")
        _validate_sha256(self.template_sha256, "validation template_sha256")
        if not self.check_types or any(not item.strip() for item in self.check_types):
            raise RetrievalConfigurationError("validation check_types must not be empty")
        if len(set(self.check_types)) != len(self.check_types):
            raise RetrievalConfigurationError("validation check_types must be unique")
        _validate_relative_source_path(self.manifest_path)
        if self.manifest_path != "configs/template_certifications.json":
            raise RetrievalConfigurationError(
                "fragment validation must use the release certification manifest"
            )


@dataclass(frozen=True, slots=True)
class KnowledgeFragment:
    fragment_id: str
    revision: int
    domain: str
    target_language: str
    repair_layer: str
    knowledge_type: str
    title: str
    content: str
    content_sha256: str
    keywords: tuple[str, ...]
    check_types: tuple[str, ...]
    failure_categories: tuple[str, ...]
    diagnostic_subcategories: tuple[str, ...]
    symbols: tuple[str, ...]
    component_paths: tuple[str, ...]
    allowed_scopes: tuple[str, ...]
    contraindications: tuple[str, ...]
    source: FragmentSource
    validation: FragmentValidation
    parameter_aliases: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(self.fragment_id):
            raise RetrievalConfigurationError(
                f"invalid fragment_id: {self.fragment_id!r}"
            )
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            raise RetrievalConfigurationError("fragment revision must be a positive integer")
        if self.domain not in _ALLOWED_DOMAINS:
            raise RetrievalConfigurationError(f"unsupported fragment domain: {self.domain}")
        if self.target_language not in _ALLOWED_LANGUAGES:
            raise RetrievalConfigurationError(
                f"unsupported fragment target_language: {self.target_language}"
            )
        if self.repair_layer not in _ALLOWED_REPAIR_LAYERS:
            raise RetrievalConfigurationError(
                f"unsupported fragment repair_layer: {self.repair_layer}"
            )
        for name in ("knowledge_type", "title", "content"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise RetrievalConfigurationError(f"fragment {name} is required")
        if self.content != self.content.strip():
            raise RetrievalConfigurationError("fragment content must not have outer whitespace")
        _validate_sha256(self.content_sha256, "fragment content_sha256")
        if hashlib.sha256(self.content.encode("utf-8")).hexdigest() != self.content_sha256:
            raise RetrievalConfigurationError(
                f"fragment content hash mismatch: {self.fragment_id}"
            )
        for name in (
            "keywords",
            "check_types",
            "failure_categories",
            "diagnostic_subcategories",
            "allowed_scopes",
        ):
            values = getattr(self, name)
            if not values or any(not isinstance(item, str) or not item.strip() for item in values):
                raise RetrievalConfigurationError(f"fragment {name} must not be empty")
            if len(set(values)) != len(values):
                raise RetrievalConfigurationError(f"fragment {name} must be unique")
        for name in ("symbols", "component_paths", "contraindications"):
            values = getattr(self, name)
            if any(not isinstance(item, str) or not item.strip() for item in values):
                raise RetrievalConfigurationError(f"fragment {name} contains an empty value")
            if len(set(values)) != len(values):
                raise RetrievalConfigurationError(f"fragment {name} must be unique")
        if not isinstance(self.parameter_aliases, Mapping):
            raise RetrievalConfigurationError("parameter_aliases must be an object")
        aliases = dict(self.parameter_aliases)
        if self.repair_layer != "requirement" and aliases:
            raise RetrievalConfigurationError(
                "parameter_aliases are allowed only on requirement fragments"
            )
        if any(
            not isinstance(alias, str)
            or not alias.strip()
            or not isinstance(canonical, str)
            or not canonical.strip()
            for alias, canonical in aliases.items()
        ):
            raise RetrievalConfigurationError("parameter_aliases must map non-empty strings")
        object.__setattr__(self, "parameter_aliases", MappingProxyType(aliases))

    @property
    def prompt_text(self) -> str:
        """The exact text counted against the retrieval context budget."""

        header = (
            f"[{self.fragment_id}; source={self.source.path}#{self.source.locator}; "
            f"version={self.source.version}; library={self.source.library} "
            f"{self.source.library_version}; validation={self.validation.status}]"
        )
        return f"{header}\n{self.content}"


@dataclass(frozen=True, slots=True)
class KnowledgeCorpus:
    corpus_id: str
    frozen_at: str
    fragments: tuple[KnowledgeFragment, ...]
    sha256: str
    path: str
    formal_experiment_authorized: bool = False
    holdout_content_included: bool = False

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(self.corpus_id):
            raise RetrievalConfigurationError(f"invalid corpus_id: {self.corpus_id!r}")
        _validate_sha256(self.sha256, "corpus sha256")
        ids = [item.fragment_id for item in self.fragments]
        if not ids or len(ids) != len(set(ids)):
            raise RetrievalConfigurationError("corpus fragment IDs must be non-empty and unique")
        if self.holdout_content_included:
            raise RetrievalConfigurationError("holdout content is forbidden in retrieval corpus")
        if not isinstance(self.formal_experiment_authorized, bool):
            raise RetrievalConfigurationError(
                "formal_experiment_authorized must be boolean"
            )
        if not isinstance(self.frozen_at, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", self.frozen_at
        ):
            raise RetrievalConfigurationError("corpus frozen_at must be UTC second precision")


@dataclass(frozen=True, slots=True)
class RetrievalPolicy:
    enabled: bool = False
    top_k: int = 3
    max_fragment_tokens: int = 160
    max_total_retrieval_tokens: int = 480
    max_retrieval_events_per_run: int = 1
    allowed_trigger_reasons: tuple[str, ...] = (
        "explicit_knowledge_gap",
        "local_repair_failed",
    )
    allowed_diagnostic_subcategories: tuple[str, ...] = (
        "unknown_parameter_alias",
        "unknown_class_or_library",
        "unknown_component_parameter",
        "unknown_port_or_member",
        "connector_type_mismatch",
        "wrong_stimulus",
    )
    corpus_sha256: str = ""
    retriever_version: str = RETRIEVER_VERSION
    token_estimator: str = TOKEN_ESTIMATOR_ID
    formal_experiment_authorized: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool) or not isinstance(
            self.formal_experiment_authorized, bool
        ):
            raise RetrievalConfigurationError("policy enable/authorization flags must be boolean")
        for name in (
            "top_k",
            "max_fragment_tokens",
            "max_total_retrieval_tokens",
            "max_retrieval_events_per_run",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise RetrievalConfigurationError(f"policy {name} must be positive")
        if self.max_fragment_tokens > self.max_total_retrieval_tokens:
            raise RetrievalConfigurationError(
                "max_fragment_tokens cannot exceed max_total_retrieval_tokens"
            )
        if self.retriever_version != RETRIEVER_VERSION:
            raise RetrievalConfigurationError("unsupported retriever_version")
        if self.token_estimator != TOKEN_ESTIMATOR_ID:
            raise RetrievalConfigurationError("unsupported token_estimator")
        if self.corpus_sha256:
            _validate_sha256(self.corpus_sha256, "policy corpus_sha256")
        if not self.allowed_trigger_reasons or not self.allowed_diagnostic_subcategories:
            raise RetrievalConfigurationError("policy trigger allowlists must not be empty")
        for name in ("allowed_trigger_reasons", "allowed_diagnostic_subcategories"):
            values = getattr(self, name)
            if any(not isinstance(item, str) or not item.strip() for item in values):
                raise RetrievalConfigurationError(f"policy {name} contains an empty value")
            if len(values) != len(set(values)):
                raise RetrievalConfigurationError(f"policy {name} must be unique")


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    template_id: str
    domain: str
    target_language: str
    repair_layer: str
    check_type: str
    failure_category: str
    diagnostic_subcategory: str
    text: str
    trigger_reason: str
    failed_snapshot_sha256: str
    local_resolution_attempted: bool
    failure_kind: str = "content"
    symbols: tuple[str, ...] = ()
    library: str = ""
    library_version: str = ""
    allowed_scope: str = ""
    active_contexts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.template_id, str) or not self.template_id.strip():
            raise RetrievalConfigurationError("query template_id is required")
        if self.domain not in _ALLOWED_DOMAINS - {"common"}:
            raise RetrievalConfigurationError(f"unsupported query domain: {self.domain}")
        if self.target_language not in _ALLOWED_LANGUAGES:
            raise RetrievalConfigurationError(
                f"unsupported query target_language: {self.target_language}"
            )
        if self.repair_layer not in _ALLOWED_REPAIR_LAYERS:
            raise RetrievalConfigurationError(
                f"unsupported query repair_layer: {self.repair_layer}"
            )
        for name in (
            "check_type",
            "failure_category",
            "diagnostic_subcategory",
            "text",
            "trigger_reason",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise RetrievalConfigurationError(f"query {name} is required")
        _validate_sha256(self.failed_snapshot_sha256, "failed_snapshot_sha256")
        if any(not item.strip() for item in (*self.symbols, *self.active_contexts)):
            raise RetrievalConfigurationError("query symbols/contexts must not be empty")
        if bool(self.library) != bool(self.library_version):
            raise RetrievalConfigurationError(
                "query library and library_version must be provided together"
            )
        if not isinstance(self.local_resolution_attempted, bool):
            raise RetrievalConfigurationError(
                "query local_resolution_attempted must be boolean"
            )


@dataclass(frozen=True, slots=True)
class RetrievedFragment:
    fragment: KnowledgeFragment
    rank: int
    estimated_tokens: int
    score: int
    score_evidence: Mapping[str, int]

    def __post_init__(self) -> None:
        if isinstance(self.rank, bool) or not isinstance(self.rank, int) or self.rank < 1:
            raise RetrievalConfigurationError("retrieved fragment rank must be positive")
        exact_tokens = estimate_tokens(self.fragment.prompt_text)
        if self.estimated_tokens != exact_tokens:
            raise RetrievalConfigurationError(
                "retrieved fragment token count does not match the fixed estimator"
            )
        if not isinstance(self.score_evidence, Mapping):
            raise RetrievalConfigurationError("retrieval score evidence must be a mapping")
        score_evidence = dict(self.score_evidence)
        if any(
            not isinstance(name, str)
            or not name.strip()
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for name, value in score_evidence.items()
        ):
            raise RetrievalConfigurationError("retrieval score evidence is invalid")
        if self.score != sum(score_evidence.values()):
            raise RetrievalConfigurationError("retrieval score does not match its evidence")
        object.__setattr__(self, "score_evidence", MappingProxyType(score_evidence))


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    attempted: bool
    triggered: bool
    reason: str
    query: RetrievalQuery
    fragments: tuple[RetrievedFragment, ...]
    estimated_tokens: int
    corpus_sha256: str
    policy: RetrievalPolicy
    filtered_candidate_count: int = 0

    def __post_init__(self) -> None:
        _validate_sha256(self.corpus_sha256, "retrieval result corpus_sha256")
        if self.policy.corpus_sha256 and self.policy.corpus_sha256 != self.corpus_sha256:
            raise RetrievalConfigurationError(
                "retrieval result policy and corpus hashes differ"
            )
        if not isinstance(self.attempted, bool) or not isinstance(self.triggered, bool):
            raise RetrievalConfigurationError("retrieval attempted/triggered flags must be boolean")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise RetrievalConfigurationError("retrieval result reason is required")
        if (
            isinstance(self.filtered_candidate_count, bool)
            or not isinstance(self.filtered_candidate_count, int)
            or self.filtered_candidate_count < 0
        ):
            raise RetrievalConfigurationError(
                "filtered_candidate_count must be a non-negative integer"
            )
        if self.triggered and (not self.attempted or not self.policy.enabled):
            raise RetrievalConfigurationError(
                "triggered retrieval requires an attempted, enabled policy"
            )
        if self.triggered != bool(self.fragments):
            raise RetrievalConfigurationError(
                "retrieval triggered flag must equal fragment presence"
            )
        if not self.attempted and (
            self.fragments or self.estimated_tokens or self.filtered_candidate_count
        ):
            raise RetrievalConfigurationError(
                "a refused retrieval cannot claim candidates, fragments, or tokens"
            )
        ranks = [item.rank for item in self.fragments]
        if ranks != list(range(1, len(self.fragments) + 1)):
            raise RetrievalConfigurationError("retrieval ranks must be consecutive")
        ids = [item.fragment.fragment_id for item in self.fragments]
        if len(ids) != len(set(ids)):
            raise RetrievalConfigurationError("retrieval fragments must be unique")
        exact_total = sum(item.estimated_tokens for item in self.fragments)
        if self.estimated_tokens != exact_total:
            raise RetrievalConfigurationError(
                "retrieval total tokens do not match selected fragments"
            )
        if len(self.fragments) > self.policy.top_k:
            raise RetrievalConfigurationError("retrieval exceeds top_k")
        if any(
            item.estimated_tokens > self.policy.max_fragment_tokens
            for item in self.fragments
        ) or self.estimated_tokens > self.policy.max_total_retrieval_tokens:
            raise RetrievalConfigurationError("retrieval exceeds its context budget")
        if self.filtered_candidate_count < len(self.fragments):
            raise RetrievalConfigurationError(
                "filtered candidate count is below selected fragment count"
            )
        if self.triggered and self.reason != "fragments_selected":
            raise RetrievalConfigurationError(
                "triggered retrieval must report fragments_selected"
            )

    @property
    def fragment_ids(self) -> tuple[str, ...]:
        return tuple(item.fragment.fragment_id for item in self.fragments)

    @property
    def context(self) -> str:
        return "\n\n".join(item.fragment.prompt_text for item in self.fragments)

    def evidence(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "triggered": self.triggered,
            "reason": self.reason,
            "failed_snapshot_sha256": self.query.failed_snapshot_sha256,
            "query": asdict(self.query),
            "fragment_ids": list(self.fragment_ids),
            "estimated_tokens": self.estimated_tokens,
            "token_estimator": self.policy.token_estimator,
            "corpus_sha256": self.corpus_sha256,
            "filtered_candidate_count": self.filtered_candidate_count,
            "limits": {
                "top_k": self.policy.top_k,
                "max_fragment_tokens": self.policy.max_fragment_tokens,
                "max_total_retrieval_tokens": self.policy.max_total_retrieval_tokens,
                "max_retrieval_events_per_run": self.policy.max_retrieval_events_per_run,
            },
            "fragments": [
                {
                    "fragment_id": item.fragment.fragment_id,
                    "rank": item.rank,
                    "estimated_tokens": item.estimated_tokens,
                    "score": item.score,
                    "score_evidence": dict(item.score_evidence),
                    "content_sha256": item.fragment.content_sha256,
                    "source": asdict(item.fragment.source),
                    "validation": asdict(item.fragment.validation),
                }
                for item in self.fragments
            ],
        }


def estimate_tokens(text: str) -> int:
    """Return a deterministic estimate; this is not a model tokenizer claim."""

    if not isinstance(text, str):
        raise TypeError("token estimation input must be a string")
    return len(_TOKEN_RE.findall(unicodedata.normalize("NFKC", text)))


def load_default_corpus() -> KnowledgeCorpus:
    expected = DEFAULT_CORPUS_HASH_PATH.read_text(encoding="utf-8").strip().lower()
    return load_knowledge_corpus(
        DEFAULT_CORPUS_PATH,
        certification_path=DEFAULT_CERTIFICATION_PATH,
        project_root=WEEK9_ROOT,
        expected_sha256=expected,
    )


def load_default_policy(corpus: KnowledgeCorpus | None = None) -> RetrievalPolicy:
    selected = corpus or load_default_corpus()
    return load_retrieval_policy(DEFAULT_POLICY_PATH, corpus_sha256=selected.sha256)


def load_knowledge_corpus(
    path: str | Path,
    *,
    certification_path: str | Path,
    project_root: str | Path,
    expected_sha256: str = "",
) -> KnowledgeCorpus:
    corpus_path = Path(path)
    raw = corpus_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256:
        _validate_sha256(expected_sha256, "expected corpus sha256")
        if digest != expected_sha256.lower():
            raise RetrievalConfigurationError("frozen retrieval corpus hash mismatch")
    try:
        data = _loads_json_strict(raw.decode("utf-8"), "retrieval corpus")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RetrievalConfigurationError(f"invalid retrieval corpus JSON: {exc}") from exc
    _require_exact_keys(
        data,
        required={
            "schema_version",
            "corpus_id",
            "frozen_at",
            "frozen",
            "formal_experiment_authorized",
            "holdout_content_included",
            "exclusions",
            "fragments",
        },
        context="corpus",
    )
    if data["schema_version"] != CORPUS_SCHEMA_VERSION:
        raise RetrievalConfigurationError("unsupported retrieval corpus schema")
    if data["frozen"] is not True:
        raise RetrievalConfigurationError("retrieval corpus must be explicitly frozen")
    if data["holdout_content_included"] is not False:
        raise RetrievalConfigurationError("retrieval corpus declares holdout content")
    exclusions = data["exclusions"]
    _require_exact_keys(
        exclusions,
        required={"holdout_content", "experiment_results", "manual_repair_answers"},
        context="corpus exclusions",
    )
    if any(exclusions.get(name) is not True for name in exclusions):
        raise RetrievalConfigurationError("all corpus exclusions must be true")
    if not isinstance(data["fragments"], list):
        raise RetrievalConfigurationError("corpus fragments must be a list")

    root = Path(project_root).resolve()
    certification = _load_certification(Path(certification_path))
    _verify_certification_implementation(certification, root)
    fragments = tuple(
        _parse_fragment(item, project_root=root, certification=certification)
        for item in data["fragments"]
    )
    return KnowledgeCorpus(
        corpus_id=data["corpus_id"],
        frozen_at=data["frozen_at"],
        fragments=fragments,
        sha256=digest,
        path=str(corpus_path.resolve()),
        formal_experiment_authorized=_strict_bool(
            data["formal_experiment_authorized"], "formal_experiment_authorized"
        ),
        holdout_content_included=False,
    )


def load_retrieval_policy(
    path: str | Path,
    *,
    corpus_sha256: str,
) -> RetrievalPolicy:
    _validate_sha256(corpus_sha256, "corpus_sha256")
    try:
        data = _loads_json_strict(
            Path(path).read_text(encoding="utf-8"), "retrieval policy"
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RetrievalConfigurationError(f"invalid retrieval policy JSON: {exc}") from exc
    _require_exact_keys(
        data,
        required={
            "schema_version",
            "enabled",
            "retriever_version",
            "token_estimator",
            "top_k",
            "max_fragment_tokens",
            "max_total_retrieval_tokens",
            "max_retrieval_events_per_run",
            "allowed_trigger_reasons",
            "allowed_diagnostic_subcategories",
            "corpus_sha256",
            "formal_experiment_authorized",
        },
        context="retrieval policy",
    )
    if data["schema_version"] != POLICY_SCHEMA_VERSION:
        raise RetrievalConfigurationError("unsupported retrieval policy schema")
    if data["corpus_sha256"].lower() != corpus_sha256.lower():
        raise RetrievalConfigurationError("retrieval policy corpus hash mismatch")
    return RetrievalPolicy(
        enabled=_strict_bool(data["enabled"], "enabled"),
        top_k=data["top_k"],
        max_fragment_tokens=data["max_fragment_tokens"],
        max_total_retrieval_tokens=data["max_total_retrieval_tokens"],
        max_retrieval_events_per_run=data["max_retrieval_events_per_run"],
        allowed_trigger_reasons=_string_tuple(
            data["allowed_trigger_reasons"], "allowed_trigger_reasons"
        ),
        allowed_diagnostic_subcategories=_string_tuple(
            data["allowed_diagnostic_subcategories"],
            "allowed_diagnostic_subcategories",
        ),
        corpus_sha256=data["corpus_sha256"].lower(),
        retriever_version=data["retriever_version"],
        token_estimator=data["token_estimator"],
        formal_experiment_authorized=_strict_bool(
            data["formal_experiment_authorized"], "formal_experiment_authorized"
        ),
    )


def retrieve_knowledge(
    query: RetrievalQuery,
    corpus: KnowledgeCorpus,
    policy: RetrievalPolicy,
    *,
    prior_retrieval_events: int = 0,
) -> RetrievalResult:
    """Filter, rank and budget fragments without mutating the failed snapshot."""

    if (
        isinstance(prior_retrieval_events, bool)
        or not isinstance(prior_retrieval_events, int)
        or prior_retrieval_events < 0
    ):
        raise RetrievalConfigurationError(
            "prior_retrieval_events must be a non-negative integer"
        )
    if policy.corpus_sha256 and policy.corpus_sha256 != corpus.sha256:
        raise RetrievalConfigurationError("policy is not bound to the loaded corpus")
    refusal = _retrieval_refusal_reason(query, policy, prior_retrieval_events)
    if refusal:
        return RetrievalResult(
            attempted=False,
            triggered=False,
            reason=refusal,
            query=query,
            fragments=(),
            estimated_tokens=0,
            corpus_sha256=corpus.sha256,
            policy=policy,
        )

    candidates: list[tuple[int, str, int, KnowledgeFragment, dict[str, int]]] = []
    for fragment in corpus.fragments:
        score_evidence = _filter_and_score(query, fragment)
        if score_evidence is None:
            continue
        tokens = estimate_tokens(fragment.prompt_text)
        if tokens > policy.max_fragment_tokens:
            continue
        score = sum(score_evidence.values())
        candidates.append((score, fragment.fragment_id, tokens, fragment, score_evidence))
    candidates.sort(key=lambda item: (-item[0], item[1]))

    selected: list[RetrievedFragment] = []
    total = 0
    for score, _fragment_id, tokens, fragment, score_evidence in candidates:
        if len(selected) >= policy.top_k:
            break
        if total + tokens > policy.max_total_retrieval_tokens:
            continue
        selected.append(
            RetrievedFragment(
                fragment=fragment,
                rank=len(selected) + 1,
                estimated_tokens=tokens,
                score=score,
                score_evidence=score_evidence,
            )
        )
        total += tokens

    triggered = bool(selected)
    return RetrievalResult(
        attempted=True,
        triggered=triggered,
        reason="fragments_selected" if triggered else "no_valid_fragment_match",
        query=query,
        fragments=tuple(selected),
        estimated_tokens=total,
        corpus_sha256=corpus.sha256,
        policy=policy,
        filtered_candidate_count=len(candidates),
    )


def _retrieval_refusal_reason(
    query: RetrievalQuery,
    policy: RetrievalPolicy,
    prior_retrieval_events: int,
) -> str:
    if not policy.enabled:
        return "retrieval_disabled"
    if query.failure_kind.casefold() in _INFRASTRUCTURE_FAILURE_KINDS:
        return "infrastructure_failure_not_retrievable"
    if query.failure_kind.casefold() != "content":
        return "unknown_failure_kind"
    if not query.local_resolution_attempted:
        return "local_resolution_required_first"
    if query.trigger_reason not in policy.allowed_trigger_reasons:
        return "trigger_reason_not_allowed"
    if query.diagnostic_subcategory not in policy.allowed_diagnostic_subcategories:
        return "diagnostic_subcategory_not_allowed"
    if prior_retrieval_events >= policy.max_retrieval_events_per_run:
        return "retrieval_event_budget_exhausted"
    return ""


def _filter_and_score(
    query: RetrievalQuery,
    fragment: KnowledgeFragment,
) -> dict[str, int] | None:
    if fragment.validation.template_id != query.template_id:
        return None
    if fragment.domain not in {query.domain, "common"}:
        return None
    if fragment.target_language != query.target_language:
        return None
    if fragment.repair_layer != query.repair_layer:
        return None
    if query.check_type not in fragment.check_types:
        return None
    if query.failure_category not in fragment.failure_categories:
        return None
    if query.allowed_scope and query.allowed_scope not in fragment.allowed_scopes:
        return None
    if set(query.active_contexts).intersection(fragment.contraindications):
        return None
    if query.library and (
        _fold(query.library) != _fold(fragment.source.library)
        or query.library_version != fragment.source.library_version
    ):
        return None

    diagnostic_match = query.diagnostic_subcategory in fragment.diagnostic_subcategories
    query_symbols = {
        form for item in query.symbols for form in _symbol_forms(item)
    }
    fragment_symbols = {
        form
        for item in (*fragment.symbols, *fragment.component_paths)
        for form in _symbol_forms(item)
    }
    symbol_hits = len(query_symbols.intersection(fragment_symbols))
    # Error type is a metadata filter, not a soft lexical hint.  A familiar
    # symbol must never pull in a fragment for a different diagnosed failure.
    if not diagnostic_match:
        return None
    if fragment.target_language == "structured_parameter":
        query_aliases = {
            re.sub(r"[\s_-]+", "", _fold(item)) for item in query.symbols
        }
        fragment_aliases = {
            re.sub(r"[\s_-]+", "", _fold(item))
            for item in fragment.parameter_aliases
        }
        if not query_aliases.intersection(fragment_aliases):
            return None
    elif fragment.target_language == "modelica" and symbol_hits == 0:
        return None

    query_terms = _lexical_terms(" ".join((query.text, *query.symbols)))
    keyword_terms = _lexical_terms(" ".join(fragment.keywords))
    content_terms = _lexical_terms(
        " ".join((fragment.title, fragment.content, *fragment.symbols, *fragment.component_paths))
    )
    keyword_overlap = len(query_terms.intersection(keyword_terms))
    content_overlap = len(query_terms.intersection(content_terms))
    return {
        "diagnostic_exact": 100 if diagnostic_match else 0,
        "symbol_exact": 20 * symbol_hits,
        "failure_category_exact": 10,
        "check_type_exact": 8,
        "domain_exact": 4 if fragment.domain == query.domain else 2,
        "keyword_overlap": 3 * keyword_overlap,
        "content_overlap": content_overlap,
        "validation_rank": _VALIDATION_RANK[fragment.validation.status],
    }


def _parse_fragment(
    data: Mapping[str, Any],
    *,
    project_root: Path,
    certification: Mapping[str, Any],
) -> KnowledgeFragment:
    _require_exact_keys(
        data,
        required={
            "fragment_id",
            "revision",
            "domain",
            "target_language",
            "repair_layer",
            "knowledge_type",
            "title",
            "content",
            "content_sha256",
            "keywords",
            "retrieval_keys",
            "applicability",
            "source",
            "validation",
        },
        optional={"parameter_aliases"},
        context="fragment",
    )
    retrieval_keys = data["retrieval_keys"]
    _require_exact_keys(
        retrieval_keys,
        required={
            "check_types",
            "failure_categories",
            "diagnostic_subcategories",
            "symbols",
            "component_paths",
        },
        context=f"fragment {data.get('fragment_id')} retrieval_keys",
    )
    applicability = data["applicability"]
    _require_exact_keys(
        applicability,
        required={"allowed_scopes", "contraindications"},
        context=f"fragment {data.get('fragment_id')} applicability",
    )
    source_data = data["source"]
    _require_exact_keys(
        source_data,
        required={
            "kind",
            "path",
            "version",
            "library",
            "library_version",
            "locator",
            "source_sha256",
        },
        context=f"fragment {data.get('fragment_id')} source",
    )
    validation_data = data["validation"]
    _require_exact_keys(
        validation_data,
        required={"status", "template_id", "template_sha256", "check_types"},
        optional={"manifest_path"},
        context=f"fragment {data.get('fragment_id')} validation",
    )
    source = FragmentSource(**source_data)
    validation = FragmentValidation(
        status=validation_data["status"],
        template_id=validation_data["template_id"],
        template_sha256=validation_data["template_sha256"],
        check_types=_string_tuple(
            validation_data["check_types"], "fragment validation check_types"
        ),
        manifest_path=validation_data.get(
            "manifest_path", "configs/template_certifications.json"
        ),
    )
    fragment = KnowledgeFragment(
        fragment_id=data["fragment_id"],
        revision=data["revision"],
        domain=data["domain"],
        target_language=data["target_language"],
        repair_layer=data["repair_layer"],
        knowledge_type=data["knowledge_type"],
        title=data["title"],
        content=data["content"],
        content_sha256=data["content_sha256"],
        keywords=_string_tuple(data["keywords"], "fragment keywords"),
        check_types=_string_tuple(
            retrieval_keys["check_types"], "fragment check_types"
        ),
        failure_categories=_string_tuple(
            retrieval_keys["failure_categories"], "fragment failure_categories"
        ),
        diagnostic_subcategories=_string_tuple(
            retrieval_keys["diagnostic_subcategories"],
            "fragment diagnostic_subcategories",
        ),
        symbols=_string_tuple(
            retrieval_keys["symbols"], "fragment symbols", allow_empty=True
        ),
        component_paths=_string_tuple(
            retrieval_keys["component_paths"],
            "fragment component_paths",
            allow_empty=True,
        ),
        allowed_scopes=_string_tuple(
            applicability["allowed_scopes"], "fragment allowed_scopes"
        ),
        contraindications=_string_tuple(
            applicability["contraindications"],
            "fragment contraindications",
            allow_empty=True,
        ),
        source=source,
        validation=validation,
        parameter_aliases=data.get("parameter_aliases", {}),
    )
    if not set(fragment.check_types).issubset(fragment.validation.check_types):
        raise RetrievalConfigurationError(
            f"fragment retrieval checks exceed its validation checks: {fragment.fragment_id}"
        )
    _verify_fragment_source(fragment, project_root)
    _verify_fragment_certification(fragment, certification)
    if estimate_tokens(fragment.prompt_text) > 1024:
        raise RetrievalConfigurationError(
            f"fragment is not independently small: {fragment.fragment_id}"
        )
    return fragment


def _verify_fragment_source(fragment: KnowledgeFragment, project_root: Path) -> None:
    relative = PurePosixPath(fragment.source.path)
    candidate = (project_root / Path(*relative.parts)).resolve()
    try:
        candidate.relative_to(project_root)
    except ValueError as exc:
        raise RetrievalConfigurationError("fragment source escapes project root") from exc
    if not candidate.is_file():
        raise RetrievalConfigurationError(
            f"fragment source does not exist: {fragment.source.path}"
        )
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    if digest != fragment.source.source_sha256:
        raise RetrievalConfigurationError(
            f"fragment source hash mismatch: {fragment.fragment_id}"
        )


def _verify_fragment_certification(
    fragment: KnowledgeFragment,
    certification: Mapping[str, Any],
) -> None:
    summary = certification.get("summary", {})
    if summary.get("all_certified") is not True:
        raise RetrievalConfigurationError("template certification summary is not passing")
    if (
        summary.get("stage1_gate_passed") is not True
        or summary.get("known_hard_error_false_release_count") != 0
        or summary.get("correct_sample_false_rejection_count") != 0
    ):
        raise RetrievalConfigurationError(
            "template certification release metrics are not passing"
        )
    implementation = certification.get("implementation_sha256", {})
    if implementation.get(fragment.source.path) != fragment.source.source_sha256:
        raise RetrievalConfigurationError(
            f"fragment source is not bound to the certification implementation: {fragment.fragment_id}"
        )
    templates = certification.get("templates")
    if not isinstance(templates, Mapping):
        raise RetrievalConfigurationError("certification manifest has no template mapping")
    certificate = templates.get(fragment.validation.template_id)
    if not isinstance(certificate, Mapping):
        raise RetrievalConfigurationError(
            f"fragment template is not certified: {fragment.validation.template_id}"
        )
    if certificate.get("template_sha256") != fragment.validation.template_sha256:
        raise RetrievalConfigurationError(
            f"fragment template hash is stale: {fragment.fragment_id}"
        )
    if certificate.get("overall_success") is not True or certificate.get("full_gate") != "PASS":
        raise RetrievalConfigurationError(
            f"fragment template certificate is not fully passing: {fragment.fragment_id}"
        )
    checks = certificate.get("checks", {})
    for check_type in fragment.validation.check_types:
        check = checks.get(check_type)
        if not isinstance(check, Mapping) or check.get("status") != "PASS":
            raise RetrievalConfigurationError(
                f"fragment lacks passing validation check {check_type}: {fragment.fragment_id}"
            )


def _load_certification(path: Path) -> Mapping[str, Any]:
    try:
        data = _loads_json_strict(
            path.read_text(encoding="utf-8"), "template certification"
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RetrievalConfigurationError(f"invalid certification JSON: {exc}") from exc
    if not isinstance(data, Mapping):
        raise RetrievalConfigurationError("certification manifest must be an object")
    return data


def _verify_certification_implementation(
    certification: Mapping[str, Any],
    project_root: Path,
) -> None:
    implementation = certification.get("implementation_sha256")
    if not isinstance(implementation, Mapping) or not implementation:
        raise RetrievalConfigurationError(
            "certification manifest has no implementation hash mapping"
        )
    missing_anchors = sorted(_REQUIRED_RETRIEVAL_ANCHOR_FILES - set(implementation))
    if missing_anchors:
        raise RetrievalConfigurationError(
            f"certification manifest omits retrieval anchors: {missing_anchors}"
        )
    for raw_path, expected in implementation.items():
        if not isinstance(raw_path, str) or "\\" in raw_path:
            raise RetrievalConfigurationError(
                "certification implementation paths must use project-relative '/' syntax"
            )
        relative = PurePosixPath(raw_path)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise RetrievalConfigurationError(
                "certification implementation path escapes project root"
            )
        _validate_sha256(expected, f"certification hash for {raw_path}")
        candidate = (project_root / Path(*relative.parts)).resolve()
        try:
            candidate.relative_to(project_root)
        except ValueError as exc:
            raise RetrievalConfigurationError(
                "certification implementation path resolves outside project root"
            ) from exc
        if not candidate.is_file():
            raise RetrievalConfigurationError(
                f"certification implementation file is missing: {raw_path}"
            )
        actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if actual != expected:
            raise RetrievalConfigurationError(
                f"certification implementation is stale: {raw_path}"
            )


def _validate_relative_source_path(value: str) -> None:
    if "\\" in value:
        raise RetrievalConfigurationError("fragment source paths must use '/' separators")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise RetrievalConfigurationError("fragment source path must stay project-relative")
    folded_parts = {_fold(part) for part in path.parts}
    forbidden_stems = ("holdout", "result", "answer", "judge", "manualrepair")
    compact_parts = {re.sub(r"[^a-z0-9]", "", part) for part in folded_parts}
    if folded_parts.intersection(_FORBIDDEN_SOURCE_PARTS) or any(
        stem in part for stem in forbidden_stems for part in compact_parts
    ):
        raise RetrievalConfigurationError("forbidden retrieval source directory")
    if _fold(path.name) in _FORBIDDEN_SOURCE_FILES:
        raise RetrievalConfigurationError("case definitions cannot be retrieval sources")


def _validate_sha256(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or value != value.lower()
        or not _SHA256_RE.fullmatch(value)
    ):
        raise RetrievalConfigurationError(f"{label} must be a lowercase SHA-256 digest")


def _string_tuple(
    value: Any,
    label: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise RetrievalConfigurationError(f"{label} must be a JSON list")
    if not allow_empty and not value:
        raise RetrievalConfigurationError(f"{label} must not be empty")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise RetrievalConfigurationError(f"{label} must contain non-empty strings")
    if len(value) != len(set(value)):
        raise RetrievalConfigurationError(f"{label} must contain unique strings")
    return tuple(value)


def _loads_json_strict(text: str, context: str) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RetrievalConfigurationError(
                    f"{context} contains duplicate JSON key {key!r}"
                )
            result[key] = value
        return result

    def reject_constant(value: str) -> Any:
        raise RetrievalConfigurationError(
            f"{context} contains non-standard numeric constant {value}"
        )

    return json.loads(
        text,
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )


def _strict_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise RetrievalConfigurationError(f"{label} must be boolean")
    return value


def _require_exact_keys(
    value: Any,
    *,
    required: set[str],
    context: str,
    optional: set[str] | None = None,
) -> None:
    if not isinstance(value, Mapping):
        raise RetrievalConfigurationError(f"{context} must be an object")
    actual = set(value)
    allowed = required | (optional or set())
    missing = sorted(required - actual)
    unknown = sorted(actual - allowed)
    if missing or unknown:
        raise RetrievalConfigurationError(
            f"{context} keys invalid; missing={missing}, unknown={unknown}"
        )


def _lexical_terms(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", text)
    return {_fold(item) for item in _WORD_RE.findall(normalized)}


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip().casefold()


def _symbol_forms(text: str) -> set[str]:
    folded = _fold(text)
    forms = {folded}
    identifiers = re.findall(r"[A-Za-z_]\w*", folded)
    if identifiers:
        forms.add(identifiers[-1])
    return forms


__all__ = [
    "CORPUS_SCHEMA_VERSION",
    "DEFAULT_CERTIFICATION_PATH",
    "DEFAULT_CORPUS_HASH_PATH",
    "DEFAULT_CORPUS_PATH",
    "DEFAULT_POLICY_PATH",
    "FragmentSource",
    "FragmentValidation",
    "KnowledgeCorpus",
    "KnowledgeFragment",
    "POLICY_SCHEMA_VERSION",
    "RETRIEVER_VERSION",
    "RetrievalConfigurationError",
    "RetrievalPolicy",
    "RetrievalQuery",
    "RetrievalResult",
    "RetrievedFragment",
    "TOKEN_ESTIMATOR_ID",
    "estimate_tokens",
    "load_default_corpus",
    "load_default_policy",
    "load_knowledge_corpus",
    "load_retrieval_policy",
    "retrieve_knowledge",
]
