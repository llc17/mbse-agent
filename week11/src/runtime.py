"""Bounded DeepSeek chat transport for V10.

It has no tool dispatcher: callers receive the provider's chat-format tool
calls and decide whether to act on them.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
from types import MappingProxyType
from typing import Any, Callable
from urllib.parse import urlsplit


_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_CONFIG = _ROOT / "configs" / "runtime.json"
_ALLOWED_RESPONSE_MODELS = frozenset(("deepseek-flash", "DeepSeek-V4.1-Flash", "deepseek-v4.1-flash"))


class V10RuntimeError(RuntimeError):
    pass
RuntimeError = V10RuntimeError


@contextmanager
def _connection(path: Path):
    connection = sqlite3.connect(path, timeout=30)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def build_system_prompt(tool_instructions: str = "") -> str:
    suffix = f"\nTool instructions supplied by the service:\n{tool_instructions}" if tool_instructions else ""
    return (
        "You organize V10 MBSE requests into structured proposals. Use only rc_filter, "
        "rlc_filter, and thermal_dual_room. A proposal contains schema_version v10-spec-1, "
        "sources, parameters (name/value/unit/source_id/source_text/origin), stimulus, "
        "derivations, constraints, and assumptions. Cite source_text as an exact substring "
        "of a real user source; never invent a source. Ask for clarification for missing, "
        "conflicting, unsupported, or unsafe requests. RC uses a step and RLC a sine input; "
        "only the certified capacitance formulas may be requested. Prepare modification "
        "proposals for supported changes; never approve or claim human confirmation. "
        "Execution requires the service's recorded human approval."
        + suffix
    )


class DeepSeekRuntime:
    def __init__(self, config_path: str | Path | None = None, ledger_path: str | Path | None = None, *, transport: Callable[..., dict[str, Any]] | None = None):
        self.config_path = _inside_week11(Path(config_path or _DEFAULT_CONFIG))
        self.config = _load_config(self.config_path)
        self.ledger_path = _inside_week11(Path(ledger_path or (_ROOT / "results" / "runtime-ledger.sqlite")))
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.transport = transport
        self._config_hash = _digest(dict(self.config))
        self._init_ledger()

    def complete(self, messages: list, tools: list, request_id: str) -> dict[str, Any]:
        if not isinstance(request_id, str) or not request_id:
            raise RuntimeError("request_id must be a non-empty string")
        if not isinstance(messages, list) or not isinstance(tools, list):
            raise RuntimeError("messages and tools must be lists")
        request_hash = _digest({"messages": messages, "tools": tools})
        row = self._request(request_id)
        if row:
            if row["content_hash"] != request_hash:
                raise RuntimeError("request_id conflicts with previously persisted content")
            if row["status"] == "succeeded":
                return json.loads(row["response"])
            raise RuntimeError("request_id has a prior failed or unknown attempt; automatic resend is forbidden")
        estimated_input = _estimate_input(messages, tools)
        if estimated_input > self.config["max_input_tokens"]:
            raise RuntimeError("estimated input exceeds max_input_tokens")
        reserve_tokens = estimated_input + self.config["max_tokens"]
        reserve_cost = estimated_input * self.config["input_usd_per_million"] / 1_000_000 + self.config["max_tokens"] * self.config["output_usd_per_million"] / 1_000_000
        self._reserve(request_id, request_hash, reserve_tokens, reserve_cost)
        try:
            response = self._send(messages, tools)
        except Exception as exc:
            diagnostic = _safe_transport_diagnostic(exc)
            self._transport_error(request_id, diagnostic)
            raise RuntimeError("transport failed; request remains reserved and will not be resent; " + diagnostic) from None
        try:
            result, input_tokens, output_tokens = _parse_response(response)
            response_model = result["response_model"]
            if response_model not in _ALLOWED_RESPONSE_MODELS:
                raise RuntimeError("provider returned an unapproved model identity")
            if input_tokens > estimated_input or output_tokens > self.config["max_tokens"]:
                self._record_known_usage(request_id, input_tokens + output_tokens, input_tokens * self.config["input_usd_per_million"] / 1_000_000 + output_tokens * self.config["output_usd_per_million"] / 1_000_000)
                self._mark_unknown(request_id, "usage exceeded reservation")
                raise RuntimeError("provider usage exceeded reservation; runtime frozen")
            self._succeed(request_id, result, input_tokens + output_tokens, input_tokens * self.config["input_usd_per_million"] / 1_000_000 + output_tokens * self.config["output_usd_per_million"] / 1_000_000)
            return result
        except RuntimeError:
            self._mark_unknown(request_id, "invalid response or budget overrun")
            raise

    def _send(self, messages: list, tools: list) -> dict[str, Any]:
        key = _api_key()
        payload = {"model": self.config["model"], "messages": messages, "tools": tools, "tool_choice": "auto", "thinking": {"type": "disabled"}, "temperature": self.config["temperature"], "max_tokens": self.config["max_tokens"]}
        if self.transport is not None:
            return self.transport(payload, key, self.config["timeout_s"])
        import httpx
        with httpx.Client(timeout=self.config["timeout_s"], follow_redirects=False) as client:
            response = client.post(self.config["api_base"] + "/chat/completions", headers={"Authorization": "Bearer " + key}, json=payload)
            response.raise_for_status()
            return response.json()

    def _init_ledger(self) -> None:
        with _connection(self.ledger_path) as conn:
            conn.execute("create table if not exists meta (key text primary key, value text not null)")
            conn.execute("create table if not exists requests (request_id text primary key, content_hash text not null, status text not null, reserve_tokens integer not null, reserve_cost real not null, actual_tokens integer, actual_cost real, errors integer not null default 0, response text, reason text)")
            old = conn.execute("select value from meta where key='config_hash'").fetchone()
            if old and old[0] != self._config_hash:
                raise RuntimeError("ledger configuration hash differs; create a new ledger")
            conn.execute("insert or ignore into meta(key,value) values('config_hash',?)", (self._config_hash,))

    def _request(self, request_id: str):
        with _connection(self.ledger_path) as conn:
            row = conn.execute("select content_hash,status,response from requests where request_id=?", (request_id,)).fetchone()
        return None if row is None else {"content_hash": row[0], "status": row[1], "response": row[2]}

    def _reserve(self, request_id: str, content_hash: str, tokens: int, cost: float) -> None:
        with _connection(self.ledger_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("select content_hash,status from requests where request_id=?", (request_id,)).fetchone()
            if existing:
                if existing[0] != content_hash: raise RuntimeError("request_id conflicts with previously persisted content")
                raise RuntimeError("request_id already reserved; automatic resend is forbidden")
            if conn.execute("select value from meta where key='frozen'").fetchone():
                raise RuntimeError("runtime is frozen after a transport or budget safety failure")
            totals = conn.execute("select count(*), coalesce(sum(coalesce(actual_tokens,reserve_tokens)),0), coalesce(sum(coalesce(actual_cost,reserve_cost)),0) from requests").fetchone()
            if totals[0] >= self.config["max_requests"] or totals[1] + tokens > self.config["max_total_tokens"] or totals[2] + cost > self.config["max_usd"]:
                raise RuntimeError("runtime budget exhausted")
            conn.execute("insert into requests(request_id,content_hash,status,reserve_tokens,reserve_cost) values(?,?,?,?,?)", (request_id, content_hash, "reserved", tokens, cost))

    def _succeed(self, request_id: str, result: dict[str, Any], tokens: int, cost: float) -> None:
        with _connection(self.ledger_path) as conn:
            conn.execute("update requests set status='succeeded',actual_tokens=?,actual_cost=?,response=? where request_id=?", (tokens, cost, json.dumps(result, ensure_ascii=False, separators=(",", ":")), request_id))

    def _record_known_usage(self, request_id: str, tokens: int, cost: float) -> None:
        with _connection(self.ledger_path) as conn:
            conn.execute("update requests set actual_tokens=?,actual_cost=? where request_id=?", (tokens, cost, request_id))

    def _mark_unknown(self, request_id: str, reason: str) -> None:
        with _connection(self.ledger_path) as conn:
            conn.execute("update requests set status='unknown', reason=coalesce(reason,?) where request_id=?", (reason, request_id))
            conn.execute("insert or ignore into meta(key,value) values('frozen',?)", (reason,))

    def _transport_error(self, request_id: str, diagnostic: str) -> None:
        with _connection(self.ledger_path) as conn:
            statuses = [row[0] for row in conn.execute("select status from requests order by rowid desc").fetchall()]
            errors = 1
            for status in statuses[1:]:
                if status == "failed":
                    errors += 1
                else:
                    break
            conn.execute("update requests set status='failed', errors=?, reason=? where request_id=?", (errors, diagnostic, request_id))
            if errors >= self.config["max_consecutive_transport_errors"]:
                conn.execute("insert or replace into meta(key,value) values('frozen','transport_errors')")


def _safe_transport_diagnostic(exc: BaseException) -> str:
    """Record only exception types and numeric codes, never request/body/headers."""
    chain = []
    seen = set()
    while exc is not None and id(exc) not in seen and len(chain) < 8:
        seen.add(id(exc))
        item = {"type": type(exc).__name__}
        for name in ("errno", "winerror"):
            value = getattr(exc, name, None)
            if type(value) is int:
                item[name] = value
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if type(status) is int:
            item["http_status"] = status
        chain.append(item)
        exc = exc.__cause__ or exc.__context__
    return json.dumps({"transport_exception_chain": chain}, separators=(",", ":"))


def _load_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError("runtime configuration cannot be read") from exc
    required = {"schema_version", "model", "api_base", "thinking", "temperature", "max_tokens", "timeout_s", "max_requests", "max_total_tokens", "max_usd", "max_input_tokens", "input_usd_per_million", "output_usd_per_million", "max_consecutive_transport_errors", "automatic_retries"}
    if not isinstance(config, dict) or not required <= set(config) or config.get("schema_version") != "v10-runtime-1" or config.get("model") != "deepseek-flash":
        raise RuntimeError("invalid runtime configuration")
    if not isinstance(config["api_base"], str): raise RuntimeError("invalid api_base")
    try:
        origin = urlsplit(config["api_base"])
        port = origin.port
    except ValueError: raise RuntimeError("invalid api_base") from None
    if origin.scheme != "https" or origin.hostname != "api.deepseek.com" or port is not None or origin.username or origin.password or origin.query or origin.fragment or origin.path not in ("", "/"):
        raise RuntimeError("api_base must be the official DeepSeek HTTPS origin")
    config["api_base"] = config["api_base"].rstrip("/")
    if config["thinking"] != "disabled" or isinstance(config["temperature"], bool) or config["temperature"] != 0 or isinstance(config["automatic_retries"], bool) or config["automatic_retries"] != 0:
        raise RuntimeError("runtime configuration violates frozen transport settings")
    import math
    numeric = ("max_tokens", "timeout_s", "max_requests", "max_total_tokens", "max_usd", "max_input_tokens", "input_usd_per_million", "output_usd_per_million", "max_consecutive_transport_errors")
    if any(isinstance(config[k], bool) or not isinstance(config[k], (int,float)) or not math.isfinite(config[k]) or config[k] <= 0 for k in numeric):
        raise RuntimeError("invalid runtime budget")
    if any(not isinstance(config[k], int) or isinstance(config[k], bool) for k in ("max_tokens","timeout_s","max_requests","max_total_tokens","max_input_tokens","max_consecutive_transport_errors")):
        raise RuntimeError("invalid integer runtime limit")
    return MappingProxyType(dict(config))


def _inside_week11(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=False)
        root = _ROOT.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        raise RuntimeError("runtime paths must resolve inside week11") from None
    return resolved


def _parse_response(response: Any) -> tuple[dict[str, Any], int, int]:
    if not isinstance(response, dict) or not isinstance(response.get("model"), str) or response.get("model") not in _ALLOWED_RESPONSE_MODELS:
        raise RuntimeError("invalid provider response identity")
    choices, usage = response.get("choices"), response.get("usage")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict) or choices[0].get("finish_reason") not in ("stop", "tool_calls") or not isinstance(choices[0].get("message"), dict):
        raise RuntimeError("invalid provider response choices")
    if not isinstance(usage, dict):
        raise RuntimeError("invalid provider usage")
    prompt, completion, total = usage.get("prompt_tokens"), usage.get("completion_tokens"), usage.get("total_tokens")
    if any(isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in (prompt, completion, total)) or total != prompt + completion:
        raise RuntimeError("invalid provider usage")
    message = choices[0]["message"]
    content, calls = message.get("content"), message.get("tool_calls", [])
    if content is not None and not isinstance(content, str) or not isinstance(calls, list):
        raise RuntimeError("invalid provider message")
    identifiers = set()
    for call in calls:
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(call, dict) or not isinstance(call.get("id"), str) or not call["id"] or call["id"] in identifiers or call.get("type") != "function" or not isinstance(function, dict) or not isinstance(function.get("name"), str) or not function["name"] or not isinstance(function.get("arguments"), str):
            raise RuntimeError("invalid provider tool call")
        identifiers.add(call["id"])
    if not (content or calls):
        raise RuntimeError("provider message is empty")
    if (choices[0]["finish_reason"] == "tool_calls") != bool(calls):
        raise RuntimeError("provider finish reason conflicts with tool calls")
    return {"content": content or "", "tool_calls": calls, "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}, "response_model": response["model"]}, prompt, completion


def _estimate_input(messages: list, tools: list) -> int:
    encoded = json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return len(encoded) + 64 * (len(messages) + len(tools) + 1)


def _api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        env = _ROOT.parent / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith("DEEPSEEK_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip("\"'")
                    break
    if not key:
        raise RuntimeError("DeepSeek API key is unavailable")
    return key


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
