# -*- coding: utf-8 -*-
"""
=============================================================================
llm_client.py — DeepSeek API 封装（V3 保持不变）
=============================================================================

本文件是系统中唯一与外部 AI 服务交互的模块。
所有节点都通过 chat() 函数调用 LLM。

设计决策：
  1. 单一入口：整个项目只有一个 chat() 函数 + 一个 _api_request() 底层
  2. 自动重试：网络错误或 API 返回异常时，exponential backoff 重试最多 3 次
  3. 日志记录：每次 API 调用记录 token 消耗，用于成本分析
  4. 环境变量配置：API key / base URL / model 全从环境变量读，方便切换

V3 改动：无。此文件在 V2 已稳定，V3 直接复用。

用法:
    from src.llm_client import chat, user_msg, system_msg
    response = chat([user_msg("你好")], temperature=0.3)
"""

# ====================================================================
# 导入
# ====================================================================
import json
import logging
import os
import time
from typing import Type, TypeVar

import requests
from pydantic import BaseModel

# ====================================================================
# 日志
# ====================================================================
logger = logging.getLogger("llm_client")

# ====================================================================
# 配置 — 从环境变量读取，不硬编码
# ====================================================================
# 这样切换模型或 API 服务商时只需要改环境变量，不需要改代码

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
#   你的 DeepSeek API key。设置方式（PowerShell）:
#     $env:DEEPSEEK_API_KEY = "sk-..."
#   未设置时程序会在 check_prerequisites() 阶段报错

API_BASE = os.environ.get("DEEPSEEK_API_BASE", "https://api.deepseek.com")
#   API 地址。默认 DeepSeek 官方。如果用了代理或兼容接口可以改

MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
#   模型名。支持所有 DeepSeek 兼容模型

# ====================================================================
# 重试配置
# ====================================================================
# API 调用可能因网络波动、限流等原因失败。这里用 exponential backoff
# 重试 3 次：等待 2s → 4s → 8s

_MAX_RETRIES = 3      # 最大重试次数
_BASE_DELAY = 2.0     # 基础等待时间（秒），每次翻倍

T = TypeVar("T", bound=BaseModel)  # 泛型：chat_structured 的返回类型


# ====================================================================
# 底层 HTTP 请求 — 真正发请求的函数
# ====================================================================

def _api_request(payload: dict, timeout: int = 120) -> dict:
    """
    向 DeepSeek API 发送 POST 请求，带重试逻辑。

    参数:
        payload: OpenAI 兼容格式的请求体，包含 model/messages/temperature 等
        timeout: 单次请求的超时秒数（默认 120s，考虑长文本生成）

    返回:
        API 响应的 JSON 字典，至少包含 choices[0].message.content

    异常:
        重试 3 次后仍失败 → 抛出 RuntimeError
    """
    last_error = None

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            # ---- 发送请求 ----
            resp = requests.post(
                f"{API_BASE}/v1/chat/completions",             # OpenAI 兼容端点
                headers={
                    "Authorization": f"Bearer {API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout,
            )

            # ---- 成功 ----
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

            # ---- 失败（如 429 限流、500 服务错误） ----
            detail = resp.text[:300]
            logger.warning("API 返回 %s (attempt %s): %s", resp.status_code, attempt, detail)
            last_error = RuntimeError(f"API {resp.status_code}: {detail}")

        except requests.RequestException as e:
            # ---- 网络错误（如 DNS 失败、连接超时） ----
            logger.warning("API 网络错误 (attempt %s): %s", attempt, e)
            last_error = e

        # ---- 重试前等待（exponential backoff） ----
        if attempt < _MAX_RETRIES:
            delay = _BASE_DELAY * (2 ** (attempt - 1))          # 2s → 4s → 8s
            logger.info("重试 %s/%s, 等待 %.1fs", attempt + 1, _MAX_RETRIES, delay)
            time.sleep(delay)

    # ---- 所有重试都失败 ----
    raise RuntimeError(f"API 调用失败 ({_MAX_RETRIES} 次重试后): {last_error}")


# ====================================================================
# 公开 API — 项目其他地方只调用这两个函数
# ====================================================================

def chat(
    messages: list[dict],
    *,
    temperature: float = 0.3,
    max_tokens: int = 8192,
) -> str:
    """
    发送对话请求，返回 LLM 的纯文本回复。

    这是项目中最常用的函数——所有节点的 prompt 都通过它发送。

    参数:
        messages: 对话消息列表 [{"role": "user", "content": "..."}, ...]
        temperature: 生成温度（0.0=确定性，1.0=随机）。节点1用 0.1~0.5，节点2/3用 0.3
        max_tokens: 最大输出 token 数。代码生成用 4096，简短判断用 512

    返回:
        LLM 的文本回复（str）

    用法:
        from src.llm_client import chat, user_msg
        reply = chat([user_msg("解释RC电路")], temperature=0.3)
    """
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
    """
    发送对话请求，返回 Pydantic 结构化对象（而非纯文本）。

    原理：在 system prompt 中注入 JSON Schema，让 LLM 输出合规 JSON，
    然后用 response_model.model_validate_json() 解析。

    参数:
        messages: 对话消息
        response_model: Pydantic 类（如 StructuredRequirement）
        temperature: 温度（建议 0.1~0.2，确保 JSON 格式稳定）
        max_tokens: 最大输出

    返回:
        response_model 的实例

    注意：此函数在 V2 已定义但实际使用较少，
    大部分节点仍用 chat() + 手动 extract_json() 模式。
    """
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

    # 清洗：去掉可能的 markdown 包裹
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    return response_model.model_validate_json(raw)


# ====================================================================
# 便捷函数 — 构造消息字典
# ====================================================================
# 为什么要这些函数而不是手写字典？
#   1. 防 typo："role": "user" 不会写成 "role": "usr"
#   2. 可扩展：以后如果要加 metadata 字段，只改这里

def user_msg(content: str) -> dict:
    """构造 user 角色消息"""
    return {"role": "user", "content": content}


def assistant_msg(content: str) -> dict:
    """构造 assistant 角色消息（用于多轮对话中提供历史上下文）"""
    return {"role": "assistant", "content": content}


def system_msg(content: str) -> dict:
    """构造 system 角色消息（用于设定 LLM 的行为角色）"""
    return {"role": "system", "content": content}
