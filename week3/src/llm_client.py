"""
DeepSeek API 封装 — V2 增强版：retry + 日志。

用法：
    from src.llm_client import chat, chat_structured, user_msg
"""

import json
import logging
import os
import time
from typing import Type, TypeVar

import requests
from pydantic import BaseModel

logger = logging.getLogger("llm_client")

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
API_BASE = os.environ.get("DEEPSEEK_API_BASE", "https://api.deepseek.com")
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")

T = TypeVar("T", bound=BaseModel)

_MAX_RETRIES = 3
_BASE_DELAY = 2.0  # seconds


def _api_request(payload: dict, timeout: int = 120) -> dict:
    """发送 API 请求，带 retry + exponential backoff。"""
    last_error = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = requests.post(
                f"{API_BASE}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout,
            )
            if resp.ok:
                body = resp.json()
                usage = body.get("usage", {})
                logger.info(
                    "API 调用成功 | tokens: in=%s out=%s total=%s",
                    usage.get("prompt_tokens", "?"),
                    usage.get("completion_tokens", "?"),
                    usage.get("total_tokens", "?"),
                )
                return body
            detail = resp.text[:300]
            logger.warning("API 返回 %s (attempt %s): %s", resp.status_code, attempt, detail)
            last_error = RuntimeError(f"API {resp.status_code}: {detail}")
        except requests.RequestException as e:
            logger.warning("API 网络错误 (attempt %s): %s", attempt, e)
            last_error = e

        if attempt < _MAX_RETRIES:
            delay = _BASE_DELAY * (2 ** (attempt - 1))
            logger.info("重试 %s/%s, 等待 %.1fs", attempt + 1, _MAX_RETRIES, delay)
            time.sleep(delay)

    raise RuntimeError(f"API 调用失败 ({_MAX_RETRIES} 次重试后): {last_error}")


def chat(
    messages: list[dict],
    *,
    temperature: float = 0.3,
    max_tokens: int = 8192,
) -> str:
    """发送对话请求，返回纯文本。"""
    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    body = _api_request(payload)
    return body["choices"][0]["message"]["content"]


def chat_structured(
    messages: list[dict],
    response_model: Type[T],
    *,
    temperature: float = 0.3,
    max_tokens: int = 8192,
) -> T:
    """发送对话请求，返回 Pydantic 结构化对象。"""
    schema = json.dumps(response_model.model_json_schema(), ensure_ascii=False, indent=2)
    system_prompt = {
        "role": "system",
        "content": (
            "你必须始终输出合法的 JSON 对象。\n"
            f"JSON Schema:\n{schema}\n\n"
            "只输出 JSON，不要包含任何解释、markdown 代码块标记。"
        ),
    }
    full_messages = [system_prompt] + list(messages)

    payload = {
        "model": MODEL,
        "messages": full_messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    body = _api_request(payload)
    raw = body["choices"][0]["message"]["content"].strip()

    # 清洗可能的 markdown 包裹
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    return response_model.model_validate_json(raw)


def user_msg(content: str) -> dict:
    return {"role": "user", "content": content}


def assistant_msg(content: str) -> dict:
    return {"role": "assistant", "content": content}


def system_msg(content: str) -> dict:
    return {"role": "system", "content": content}
