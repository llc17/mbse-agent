"""V9 A/B wrapper around the frozen V8 online projection boundary.

The transport is called exactly once.  V8 remains authoritative for parsing,
local generation, and verification; this wrapper adds prompt arms and safe
diagnostics without changing the accepted ``NaturalLanguageGenerationResult``.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from week9.src.generation import (
    DeterministicNaturalLanguageProvider,
    NaturalLanguageGenerationResult,
)
from week9.src.online_generation import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    ONLINE_PROMPT_SHA256,
    ONLINE_PROVIDER_NAME,
    OnlineGenerationError,
    _parse_projection,
    _verify_projection,
)
from week9.src.online_generation import _SYSTEM_PROMPT as V8_SYSTEM_PROMPT
from week9.src.schemas import sha256_text
from week10.src.projection_contract import V8_SOURCE_HASHES
from week10.src.projection_contract_v2 import (
    PROMPT_SHA256,
    SYSTEM_PROMPT,
)
from week10.src.projection_diagnostics import diagnose_parsed_projection


Transport = Callable[[Mapping[str, Any]], Mapping[str, Any]]
V8_ONLINE_GENERATION_SHA256 = V8_SOURCE_HASHES["week9/src/online_generation.py"]

_INFRASTRUCTURE_REASONS = frozenset(
    {
        "api_transport_error",
        "response_contract_invalid",
        "response_model_mismatch",
        "usage_contract_invalid",
        "local_generation_error",
        "response_json_invalid",
        "projection_schema_invalid",
        "unknown_diagnostic",
    }
)


class _ApiTransportFailure(RuntimeError):
    """Internal stage marker; its message is never made public."""


class _ResponseContractFailure(RuntimeError):
    """Internal stage marker; its message is never made public."""


class _RejectRedirects(HTTPRedirectHandler):
    """Keep the authorized endpoint from redirecting to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class DeepSeekNaturalLanguageProvider:
    """One-call provider with V8 acceptance semantics and safe V9 state."""

    provider = ONLINE_PROVIDER_NAME
    v8_online_generation_sha256 = V8_ONLINE_GENERATION_SHA256

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
        prompt_arm: str = "B",
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("DeepSeek API key is required")
        if not isinstance(api_base, str) or not api_base.strip():
            raise ValueError("DeepSeek API base is required")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("DeepSeek model is required")
        if type(temperature) not in {int, float} or not math.isfinite(float(temperature)):
            raise ValueError("temperature must be finite")
        if type(max_tokens) is not int or max_tokens <= 0:
            raise ValueError("max_tokens must be a positive integer")
        if (
            type(timeout_s) not in {int, float}
            or not math.isfinite(float(timeout_s))
            or timeout_s <= 0
        ):
            raise ValueError("timeout_s must be positive and finite")
        if prompt_arm not in {"A", "B"}:
            raise ValueError("prompt_arm must be A or B")
        self._api_key = api_key.strip()
        self._api_base = api_base.strip()
        self.model = model.strip()
        self.temperature = float(temperature)
        self.max_tokens = max_tokens
        self.timeout_s = float(timeout_s)
        self._transport = transport
        self.prompt_arm = prompt_arm
        self.prompt_sha256 = (
            ONLINE_PROMPT_SHA256 if prompt_arm == "A" else PROMPT_SHA256
        )
        self.api_call_count = 0
        self.last_usage: dict[str, int] | None = None
        self.last_diagnostic: dict[str, object] | None = None
        self.last_response_valid: bool | None = None
        self.last_response_model: str | None = None

    def generate(self, requirement_text: str) -> NaturalLanguageGenerationResult:
        if not isinstance(requirement_text, str) or not requirement_text.strip():
            raise ValueError("requirement_text must be a non-empty string")
        self.api_call_count = 0
        self.last_usage = None
        self.last_diagnostic = None
        self.last_response_valid = False
        self.last_response_model = None
        request_payload = self._request_payload(requirement_text)
        request_sha256 = _canonical_sha256(request_payload)
        started = time.monotonic()

        # Count the attempt before control reaches either the injected or HTTP
        # transport.  An exception cannot erase an attempted paid call.
        self.api_call_count = 1
        try:
            response = (
                self._transport(request_payload)
                if self._transport is not None
                else self._post(request_payload)
            )
        except _ResponseContractFailure as exc:
            self._fail("response_contract_invalid", exc)
        except _ApiTransportFailure as exc:
            self._fail("api_transport_error", exc)
        except Exception as exc:
            self._fail("api_transport_error", exc)

        try:
            response_object = _object(response)
            response_sha256 = _canonical_sha256(response_object)
            content, response_model = _response_content_and_model(response_object)
        except Exception as exc:
            self._fail("response_contract_invalid", exc)

        try:
            usage = _response_usage(response_object)
        except Exception as exc:
            self._fail("usage_contract_invalid", exc)
        self.last_usage = {
            "prompt_tokens": usage[0],
            "completion_tokens": usage[1],
            "total_tokens": usage[2],
        }

        if response_model != self.model:
            self._fail("response_model_mismatch")
        self.last_response_model = self.model

        try:
            projection = _parse_projection(content)
        except json.JSONDecodeError as exc:
            self._fail("response_json_invalid", exc)
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            self._fail("projection_schema_invalid", exc)
        except Exception as exc:
            self._fail("unknown_diagnostic", exc)

        self.last_response_valid = True
        try:
            local = DeterministicNaturalLanguageProvider().generate(requirement_text)
        except Exception as exc:
            self._fail("local_generation_error", exc, response_valid=True)

        try:
            # Both calls deliberately reach the frozen V8 verifier.  The first
            # preserves V8's exact returned issues; the diagnostic call assigns
            # the more precise, allow-listed V9 reason without changing them.
            issues = _verify_projection(projection, local)
            diagnostic = diagnose_parsed_projection(projection, local)
        except Exception as exc:
            self._fail("unknown_diagnostic", exc, response_valid=True)
        self.last_diagnostic = {
            **diagnostic.public_dict(),
            "response_valid": True,
        }

        duration_s = time.monotonic() - started
        common = {
            "requirement_text": requirement_text,
            "requirement_sha256": sha256_text(requirement_text),
            "provider": self.provider,
            "model": self.model,
            "prompt_tokens": usage[0],
            "completion_tokens": usage[1],
            "total_tokens": usage[2],
            "duration_s": duration_s,
            "online_api_used": True,
            "api_call_count": 1,
            "prompt_sha256": self.prompt_sha256,
            "request_sha256": request_sha256,
            "response_sha256": response_sha256,
            "response_content_sha256": sha256_text(content),
            "verifier_sha256": local.evidence_sha256,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if issues:
            return NaturalLanguageGenerationResult(
                **common, template_id=local.template_id, issues=issues
            )
        if not local.ready:
            return NaturalLanguageGenerationResult(
                **common, template_id=local.template_id, issues=local.issues
            )
        return NaturalLanguageGenerationResult(
            **common,
            template_id=local.template_id,
            model_name=local.model_name,
            observations=local.observations,
            traceability_input=local.traceability_input,
            derivations=local.derivations,
        )

    def _request_payload(self, requirement_text: str) -> dict[str, Any]:
        system_prompt = V8_SYSTEM_PROMPT if self.prompt_arm == "A" else SYSTEM_PROMPT
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": requirement_text},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
        }

    def _post(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        base = self._api_base.rstrip("/")
        url = (
            f"{base}/chat/completions"
            if base.endswith("/v1")
            else f"{base}/v1/chat/completions"
        )
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
                "User-Agent": "mbse-v9-online-generation/1",
            },
        )
        try:
            with build_opener(_RejectRedirects()).open(
                request, timeout=self.timeout_s
            ) as response:
                raw = response.read()
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise _ApiTransportFailure from exc
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _ResponseContractFailure from exc
        try:
            return _object(decoded)
        except (TypeError, ValueError) as exc:
            raise _ResponseContractFailure from exc

    def _fail(
        self,
        reason_code: str,
        cause: BaseException | None = None,
        *,
        response_valid: bool = False,
    ) -> None:
        if reason_code not in _INFRASTRUCTURE_REASONS:
            reason_code = "unknown_diagnostic"
        self.last_response_valid = response_valid
        self.last_diagnostic = {
            "reason_code": reason_code,
            "parameter_keys": (),
            "projection_accepted": False,
            "legacy_disposition": "infrastructure_error",
            "response_valid": response_valid,
        }
        error = OnlineGenerationError(reason_code)
        if cause is None:
            raise error
        raise error from cause


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("object required")
    return dict(value)


def _response_content_and_model(response: Mapping[str, Any]) -> tuple[str, str]:
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError("one choice required")
    choice = _object(choices[0])
    message = _object(choice.get("message"))
    content = message.get("content")
    model = response.get("model")
    if not isinstance(content, str):
        raise TypeError("content must be a string")
    if not isinstance(model, str) or not model.strip():
        raise TypeError("model must be a non-empty string")
    return content, model.strip()


def _response_usage(response: Mapping[str, Any]) -> tuple[int, int, int]:
    usage = _object(response.get("usage"))
    counts = tuple(
        _nonnegative_int(usage.get(name))
        for name in ("prompt_tokens", "completion_tokens", "total_tokens")
    )
    if counts[2] != counts[0] + counts[1]:
        raise ValueError("usage total mismatch")
    return counts  # type: ignore[return-value]


def _nonnegative_int(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise TypeError("non-negative integer required")
    return value


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


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TEMPERATURE",
    "DeepSeekNaturalLanguageProvider",
    "ONLINE_PROMPT_SHA256",
    "ONLINE_PROVIDER_NAME",
    "OnlineGenerationError",
    "PROMPT_SHA256",
    "V8_ONLINE_GENERATION_SHA256",
]
