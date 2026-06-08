"""
DeepSeek API 统一封装。

用法：
    from src.llm_client import chat

    response = chat("你好")
    structured = chat("生成需求", response_model=StructuredRequirement)
"""

import json
import os
import sys
from typing import Optional, Type, TypeVar

import requests
from pydantic import BaseModel

# ---- 配置 ----
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
API_BASE = os.environ.get("DEEPSEEK_API_BASE", "https://api.deepseek.com")
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")

if not API_KEY:
    print("[llm_client] 警告: 未设置 DEEPSEEK_API_KEY 环境变量")

T = TypeVar("T", bound=BaseModel)


def chat(
    messages: list[dict],
    *,
    temperature: float = 0.3,
    max_tokens: int = 16384,
) -> str:
    """发送对话请求，返回纯文本响应。"""
    resp = requests.post(
        f"{API_BASE}/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": MODEL,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
        timeout=120,
    )
    if not resp.ok:
        detail = resp.text[:500]
        raise RuntimeError(
            f"API 调用失败 ({resp.status_code}): {detail}\n"
            f"当前模型: {MODEL}（有效值: deepseek-chat / deepseek-reasoner）"
        )
    return resp.json()["choices"][0]["message"]["content"]


def chat_structured(
    messages: list[dict],
    response_model: Type[T],
    *,
    temperature: float = 0.3,
    max_tokens: int = 16384,
) -> T:
    """发送对话请求，返回 Pydantic 结构化对象。

    通过 JSON Mode 让 LLM 输出合法 JSON，再 parse 为 Pydantic。
    """
    system_prompt = {
        "role": "system",
        "content": (
            f"你必须始终输出合法的 JSON 对象。\n"
            f"JSON Schema:\n{json.dumps(response_model.model_json_schema(), ensure_ascii=False, indent=2)}\n\n"
            f"只输出 JSON，不要包含任何解释、markdown 代码块标记或其他文本。"
        ),
    }

    full_messages = [system_prompt] + list(messages)

    resp = requests.post(
        f"{API_BASE}/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": MODEL,
            "messages": full_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        },
        timeout=120,
    )
    if not resp.ok:
        detail = resp.text[:500]
        raise RuntimeError(
            f"API 调用失败 ({resp.status_code}): {detail}\n"
            f"当前模型: {MODEL}（有效值: deepseek-chat / deepseek-reasoner）"
        )
    raw = resp.json()["choices"][0]["message"]["content"]

    # 清洗可能的 markdown 代码块标记
    raw = raw.strip()
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
