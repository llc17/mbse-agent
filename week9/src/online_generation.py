"""Guarded DeepSeek natural-language projection for V8.

Importing this module is offline.  A request is made only when ``generate`` is
called on an explicitly constructed provider.  The model may propose a
certified template and explicit observations, but the proposal is accepted
only when the audited local grammar independently produces the same result.
This preserves the V8 hard boundary: an LLM cannot invent a value, select an
uncertified topology, or bypass deterministic normalization and validation.
"""

from __future__ import annotations

from dataclasses import asdict
import json
import math
import time
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from week9.src.generation import (
    DeterministicNaturalLanguageProvider,
    GenerationIssue,
    NaturalLanguageGenerationResult,
)
from week9.src.schemas import sha256_text


ONLINE_PROVIDER_NAME = "deepseek"
ONLINE_PROJECTION_SCHEMA = "v8-certified-requirement-projection-1"
ONLINE_PROMPT_VERSION = "v8-online-certified-projection-prompt-1"
DEFAULT_TEMPERATURE = 0.3
DEFAULT_MAX_TOKENS = 8192

_SYSTEM_PROMPT = """You are the constrained requirement projection step of V8.
Return one JSON object and nothing else. Never infer a missing numeric value,
unit, stimulus, or topology. Only these certified templates are allowed:
rc_filter, rlc_filter, opamp_inverting, thermal_single_room,
thermal_dual_room. For status=ready, observations must use canonical names,
numeric values, explicit units, and source_text copied exactly from the input.
For status=blocked, observations must be empty and issues must explain why.
The exact schema is:
{"schema_version":"v8-certified-requirement-projection-1",
 "status":"ready|blocked", "template_id":"string",
 "observations":[{"name":"string","value":0.0,"unit":"string",
                  "source_text":"exact input substring"}],
 "issues":[{"code":"string","message":"string"}]}
Do not emit markdown fences or additional keys."""

ONLINE_PROMPT_SHA256 = sha256_text(
    ONLINE_PROMPT_VERSION + "\n" + _SYSTEM_PROMPT
)

Transport = Callable[[Mapping[str, Any]], Mapping[str, Any]]


class OnlineGenerationError(RuntimeError):
    """An API or response-contract failure, classified as infrastructure."""


class DeepSeekNaturalLanguageProvider:
    """One-call DeepSeek projection with deterministic local verification."""

    provider = ONLINE_PROVIDER_NAME

    def __init__(
        self,
        *,
        api_key: str,
        api_base: str,
        model: str,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout_s: float = 120.0,
        transport: Transport | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("DeepSeek API key is required")
        if not isinstance(api_base, str) or not api_base.strip():
            raise ValueError("DeepSeek API base is required")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("DeepSeek model is required")
        if type(temperature) not in {int, float} or not math.isfinite(
            float(temperature)
        ):
            raise ValueError("temperature must be finite")
        if type(max_tokens) is not int or max_tokens <= 0:
            raise ValueError("max_tokens must be a positive integer")
        if type(timeout_s) not in {int, float} or not math.isfinite(
            float(timeout_s)
        ) or timeout_s <= 0:
            raise ValueError("timeout_s must be positive and finite")
        self._api_key = api_key.strip()
        self._api_base = api_base.strip()
        self.model = model.strip()
        self.temperature = float(temperature)
        self.max_tokens = max_tokens
        self.timeout_s = float(timeout_s)
        self._transport = transport

    def generate(self, requirement_text: str) -> NaturalLanguageGenerationResult:
        if not isinstance(requirement_text, str) or not requirement_text.strip():
            raise ValueError("requirement_text must be a non-empty string")
        request_payload = self._request_payload(requirement_text)
        request_sha256 = _canonical_sha256(request_payload)
        started = time.monotonic()
        try:
            response = (
                self._transport(request_payload)
                if self._transport is not None
                else self._post(request_payload)
            )
            response_object = _object(response, "response")
            response_sha256 = _canonical_sha256(response_object)
            content, response_model, usage = _response_fields(response_object)
            if response_model != self.model:
                raise OnlineGenerationError(
                    "response model differs from the frozen requested model"
                )
            projection = _parse_projection(content)
            local = DeterministicNaturalLanguageProvider().generate(requirement_text)
            issues = _verify_projection(projection, local)
            duration_s = time.monotonic() - started
            common = {
                "requirement_text": requirement_text,
                "requirement_sha256": sha256_text(requirement_text),
                "provider": self.provider,
                "model": response_model,
                "prompt_tokens": usage[0],
                "completion_tokens": usage[1],
                "total_tokens": usage[2],
                "duration_s": duration_s,
                "online_api_used": True,
                "api_call_count": 1,
                "prompt_sha256": ONLINE_PROMPT_SHA256,
                "request_sha256": request_sha256,
                "response_sha256": response_sha256,
                "response_content_sha256": sha256_text(content),
                "verifier_sha256": local.evidence_sha256,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
            }
            if issues:
                return NaturalLanguageGenerationResult(
                    **common,
                    template_id=local.template_id,
                    issues=issues,
                )
            if not local.ready:
                return NaturalLanguageGenerationResult(
                    **common,
                    template_id=local.template_id,
                    issues=local.issues,
                )
            return NaturalLanguageGenerationResult(
                **common,
                template_id=local.template_id,
                model_name=local.model_name,
                observations=local.observations,
                traceability_input=local.traceability_input,
                derivations=local.derivations,
            )
        except OnlineGenerationError:
            raise
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise OnlineGenerationError(
                f"invalid DeepSeek response contract: {type(exc).__name__}: {exc}"
            ) from exc

    def _request_payload(self, requirement_text: str) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": requirement_text},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
        }

    def _post(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        url = _chat_url(self._api_base)
        body = json.dumps(
            dict(payload), ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        request = Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "mbse-v8-online-generation/1",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                raw = response.read()
        except HTTPError as exc:
            raise OnlineGenerationError(
                f"DeepSeek HTTP error: status={exc.code}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise OnlineGenerationError(
                f"DeepSeek transport error: {type(exc).__name__}"
            ) from exc
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OnlineGenerationError("DeepSeek response is not UTF-8 JSON") from exc
        return _object(decoded, "response")


def _parse_projection(content: str) -> dict[str, Any]:
    if not content.strip() or content.strip() != content:
        raise ValueError("response content must be non-empty without outer whitespace")
    value = json.loads(content)
    projection = _object(value, "projection")
    expected = {"schema_version", "status", "template_id", "observations", "issues"}
    if set(projection) != expected:
        raise ValueError("projection keys differ from the frozen schema")
    if projection.get("schema_version") != ONLINE_PROJECTION_SCHEMA:
        raise ValueError("projection schema version differs")
    status = projection.get("status")
    if status not in {"ready", "blocked"}:
        raise ValueError("projection status must be ready or blocked")
    if not isinstance(projection.get("template_id"), str):
        raise TypeError("template_id must be a string")
    observations = projection.get("observations")
    issues = projection.get("issues")
    if not isinstance(observations, list) or not isinstance(issues, list):
        raise TypeError("projection observations and issues must be arrays")
    for item in observations:
        observed = _object(item, "observation")
        if set(observed) != {"name", "value", "unit", "source_text"}:
            raise ValueError("observation keys differ from the frozen schema")
        if (
            not isinstance(observed.get("name"), str)
            or type(observed.get("value")) not in {int, float}
            or not math.isfinite(float(observed["value"]))
            or not isinstance(observed.get("unit"), str)
            or not isinstance(observed.get("source_text"), str)
        ):
            raise TypeError("observation field types are invalid")
    for item in issues:
        issue = _object(item, "issue")
        if set(issue) != {"code", "message"} or any(
            not isinstance(issue.get(name), str) or not issue[name].strip()
            for name in ("code", "message")
        ):
            raise TypeError("issue fields are invalid")
    if status == "ready" and (not observations or issues):
        raise ValueError("ready projection requires observations and no issues")
    if status == "blocked" and (observations or not issues):
        raise ValueError("blocked projection requires issues and no observations")
    return projection


def _verify_projection(
    projection: Mapping[str, Any], local: NaturalLanguageGenerationResult
) -> tuple[GenerationIssue, ...]:
    remote_ready = projection["status"] == "ready"
    if remote_ready is not local.ready:
        return (
            GenerationIssue(
                "model_verifier_disagreement",
                "模型投影与本地确定性语法器的就绪结论不一致",
            ),
        )
    if not local.ready:
        return ()
    if projection["template_id"] != local.template_id:
        return (
            GenerationIssue(
                "model_verifier_disagreement",
                "模型投影与本地确定性语法器的模板选择不一致",
            ),
        )
    remote = projection["observations"]
    expected = [asdict(item) for item in local.observations]
    if len(remote) != len(expected):
        return (
            GenerationIssue(
                "model_verifier_disagreement",
                "模型投影与本地确定性语法器的参数数量不一致",
            ),
        )
    remote_by_name = {item["name"]: item for item in remote}
    if len(remote_by_name) != len(remote):
        return (
            GenerationIssue("model_verifier_disagreement", "模型投影包含重复参数"),
        )
    for item in expected:
        observed = remote_by_name.get(item["name"])
        if observed is None or (
            observed["unit"] != item["unit"]
            or observed["source_text"] != item["source_text"]
            or not math.isclose(
                float(observed["value"]),
                float(item["value"]),
                rel_tol=1e-12,
                abs_tol=1e-15,
            )
        ):
            return (
                GenerationIssue(
                    "model_verifier_disagreement",
                    f"参数 {item['name']} 未通过本地确定性复核",
                ),
            )
    return ()


def _response_fields(response: Mapping[str, Any]) -> tuple[str, str, tuple[int, int, int]]:
    choices = response.get("choices")
    usage = _object(response.get("usage"), "usage")
    model = response.get("model")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError("response must contain exactly one choice")
    message = _object(_object(choices[0], "choice").get("message"), "message")
    content = message.get("content")
    if not isinstance(content, str):
        raise TypeError("response message content must be a string")
    if not isinstance(model, str) or not model.strip():
        raise TypeError("response model must be a non-empty string")
    counts = tuple(
        _nonnegative_int(usage.get(name), name)
        for name in ("prompt_tokens", "completion_tokens", "total_tokens")
    )
    if counts[2] != counts[0] + counts[1]:
        raise ValueError("usage total_tokens differs from prompt plus completion")
    return content, model.strip(), counts


def _nonnegative_int(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise TypeError(f"{name} must be a non-negative integer")
    return value


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    return dict(value)


def _canonical_sha256(value: Any) -> str:
    return sha256_text(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def _chat_url(api_base: str) -> str:
    base = api_base.rstrip("/")
    return (
        f"{base}/chat/completions"
        if base.endswith("/v1")
        else f"{base}/v1/chat/completions"
    )


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TEMPERATURE",
    "DeepSeekNaturalLanguageProvider",
    "ONLINE_PROJECTION_SCHEMA",
    "ONLINE_PROMPT_SHA256",
    "ONLINE_PROVIDER_NAME",
    "OnlineGenerationError",
]
