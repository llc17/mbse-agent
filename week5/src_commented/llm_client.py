# -*- coding: utf-8 -*-
"""
=============================================================================
llm_client.py — DeepSeek API 封装（V4 版：+ token 追踪）
=============================================================================

这是整个系统与 LLM 通信的唯一入口。所有 LLM 调用都经过这里。

V2 已有:
  - chat()              — 发消息，返回纯文本
  - chat_structured()   — 发消息，返回 Pydantic 对象（JSON Schema 约束）
  - _api_request()      — 底层 HTTP 请求（3 次重试 + 指数退避）

V4 新增:
  - get_token_stats()   — 获取累计 token 消耗（用于实验报告）
  - reset_token_stats() — 重置计数器
  - _record_usage()     — 每次 API 调用后累加 token 数
  - 线程安全：用 threading.Lock() 保护全局计数器

用法:
    from src.llm_client import chat, user_msg
    reply = chat([user_msg("你好")], temperature=0.3)
=============================================================================
"""

import json
import logging
import os
import time
from typing import Type, TypeVar

import requests                         # HTTP 请求库
from pydantic import BaseModel          # Pydantic 基类（用于 chat_structured）

logger = logging.getLogger("llm_client")

# ==========================================================================
# V4: 全局 token 计数器
# ==========================================================================
# 为什么需要:
#   - M1 月报需要写"V4 期间总消耗 XX M tokens"
#   - 实验框架需要统计每个参数组合的成本
#   - 线程安全: 虽然当前是单线程，但 LangGraph 的 checkpoint 可能在异步模式下
#     并发访问，所以用 Lock 保护
from threading import Lock
_token_lock = Lock()                    # 互斥锁，保护 _token_stats 的读写
_token_stats = {                        # 累计统计数据
    "prompt_tokens": 0,                 # 输入 token 累计
    "completion_tokens": 0,             # 输出 token 累计
    "total_tokens": 0,                  # 总 token = 输入 + 输出
    "api_calls": 0,                     # API 调用次数
    "model": "",                        # 使用的模型名
}


def get_token_stats() -> dict:
    """
    返回当前累计 token 统计的副本（线程安全）。

    返回 dict 而非直接返回 _token_stats，防止外部代码意外修改内部状态。
    实验完成后调用此函数，写入 token_usage.json。
    """
    with _token_lock:                   # 获取锁，保证读取时不被其他线程修改
        return dict(_token_stats)       # dict() 创建浅拷贝


def reset_token_stats():
    """
    重置 token 计数器（线程安全）。

    每次跑实验前调用，确保统计数字从 0 开始。
    """
    with _token_lock:
        for k in _token_stats:
            if isinstance(_token_stats[k], int):  # 只重置 int 字段，model 保留了
                _token_stats[k] = 0


def _record_usage(usage: dict):
    """
    记录一次 API 调用的 token 消耗（线程安全）。

    每次 _api_request() 成功返回后调用。usage 来自 API response body。
    例: usage = {"prompt_tokens": 1500, "completion_tokens": 800, "total_tokens": 2300}
    """
    with _token_lock:                   # 获取锁，保护写操作
        _token_stats["prompt_tokens"] += usage.get("prompt_tokens", 0)
        _token_stats["completion_tokens"] += usage.get("completion_tokens", 0)
        _token_stats["total_tokens"] += usage.get("total_tokens", 0)
        _token_stats["api_calls"] += 1
        if not _token_stats["model"] and MODEL:  # 首次调用时记录模型名
            _token_stats["model"] = MODEL


# ==========================================================================
# 配置 — 从环境变量读取
# ==========================================================================
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")       # API 密钥（必须）
API_BASE = os.environ.get("DEEPSEEK_API_BASE",         # API 地址（默认 DeepSeek 官方）
                          "https://api.deepseek.com")
MODEL = os.environ.get("DEEPSEEK_MODEL",               # 模型名（可通过环境变量切换）
                        "deepseek-v4-pro")

T = TypeVar("T", bound=BaseModel)  # 泛型: 约束 T 必须是 BaseModel 子类

# --- 重试参数 ---
_MAX_RETRIES = 3       # 最大重试次数
_BASE_DELAY = 2.0      # 首次重试等待秒数（指数退避: 2s → 4s → 8s）


# ==========================================================================
# 底层: HTTP 请求 + 重试逻辑
# ==========================================================================

def _api_request(payload: dict, timeout: int = 120) -> dict:
    """
    发送 POST 请求到 DeepSeek API，带自动重试。

    为什么需要重试:
      - 网络抖动导致临时不可达
      - API 限流（429 Too Many Requests）
      - 服务端短暂故障（500/502/503）

    重试策略: 指数退避（exponential backoff）
      第1次失败 → 等 2s
      第2次失败 → 等 4s
      第3次失败 → 抛出异常

    Args:
        payload: 请求体（model, messages, temperature, max_tokens 等）
        timeout: 单次 HTTP 超时秒数

    Returns:
        API response body dict
    """
    last_error = None

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            # 发送 HTTP POST 请求
            resp = requests.post(
                f"{API_BASE}/v1/chat/completions",   # OpenAI 兼容端点
                headers={
                    "Authorization": f"Bearer {API_KEY}",  # Bearer Token 认证
                    "Content-Type": "application/json",
                },
                json=payload,                        # requests 自动序列化为 JSON
                timeout=timeout,
            )

            # HTTP 200 → 解析响应
            if resp.ok:
                body = resp.json()
                usage = body.get("usage", {})        # 提取 token 使用量
                logger.info(
                    "API 调用成功 | tokens: in=%s out=%s total=%s",
                    usage.get("prompt_tokens", "?"),
                    usage.get("completion_tokens", "?"),
                    usage.get("total_tokens", "?"),
                )
                _record_usage(usage)                  # V4: 累计 token 消耗
                return body

            # HTTP 非 200 → 记录错误，准备重试
            detail = resp.text[:300]                  # 只取前 300 字符防日志爆炸
            logger.warning("API 返回 %s (attempt %s): %s", resp.status_code, attempt, detail)
            last_error = RuntimeError(f"API {resp.status_code}: {detail}")

        except requests.RequestException as e:        # 网络层异常（DNS/连接超时等）
            logger.warning("API 网络错误 (attempt %s): %s", attempt, e)
            last_error = e

        # 如果不是最后一次尝试，等待后重试
        if attempt < _MAX_RETRIES:
            delay = _BASE_DELAY * (2 ** (attempt - 1))  # 2^0=2, 2^1=4, 2^2=8
            logger.info("重试 %s/%s, 等待 %.1fs", attempt + 1, _MAX_RETRIES, delay)
            time.sleep(delay)                           # 阻塞等待

    # 3 次全部失败 → 抛异常
    raise RuntimeError(f"API 调用失败 ({_MAX_RETRIES} 次重试后): {last_error}")


# ==========================================================================
# 高层 API: 聊天接口
# ==========================================================================

def chat(
    messages: list[dict],
    *,
    temperature: float = 0.3,           # 温度: 0=确定, 1=创意（* 强制后续参数用关键字传）
    max_tokens: int = 8192,
) -> str:
    """
    发送对话请求，返回 LLM 的纯文本回复。

    这是最常用的接口。所有节点的 LLM 调用都通过这个函数。
    不带 JSON 约束，LLM 自由输出文本。

    Args:
        messages: 消息列表，每条是 {"role": "user/assistant/system", "content": "..."}
        temperature: 0.0-1.0，越低越确定，越高越随机
        max_tokens: 最大输出 token 数

    Returns:
        LLM 回复的纯文本
    """
    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    body = _api_request(payload)
    # 从 response body 中提取文本内容
    # body["choices"][0]["message"]["content"] 是 OpenAI 兼容的标准路径
    return body["choices"][0]["message"]["content"]


def chat_structured(
    messages: list[dict],
    response_model: Type[T],             # Pydantic 模型类
    *,
    temperature: float = 0.3,
    max_tokens: int = 8192,
) -> T:
    """
    发送对话请求，返回 Pydantic 结构化对象。

    与 chat() 的区别:
      - chat(): "请写一段 SysML 代码" → 返回文本
      - chat_structured(): "返回 JSON，格式如下 Schema" → 返回 Python 对象

    实现方式:
      1. 把 Pydantic model 的 JSON Schema 发给 LLM
      2. 设置 response_format={"type": "json_object"}
      3. LLM 返回 JSON → Pydantic 自动校验 → 返回 Python 对象

    Args:
        response_model: Pydantic 模型类（如 StructuredRequirement）
        其他参数同 chat()

    Returns:
        校验通过的 Pydantic 对象
    """
    # 把 Pydantic 模型序列化为 JSON Schema
    schema = json.dumps(response_model.model_json_schema(), ensure_ascii=False, indent=2)

    # 拼一个 system prompt，强制 LLM 只输出 JSON
    system_prompt = {
        "role": "system",
        "content": (
            "你必须始终输出合法的 JSON 对象。\n"
            f"JSON Schema:\n{schema}\n\n"
            "只输出 JSON，不要包含任何解释、markdown 代码块标记。"
        ),
    }
    # system prompt 放在最前面，然后是用户消息
    full_messages = [system_prompt] + list(messages)

    payload = {
        "model": MODEL,
        "messages": full_messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},  # ← 告诉 API 要 JSON 格式
    }
    body = _api_request(payload)
    raw = body["choices"][0]["message"]["content"].strip()

    # 清洗可能的 markdown 包裹（有时 LLM 还是包了 ```json ... ```）
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    # 用 Pydantic 校验 JSON → 如果 JSON 不合 Schema，这里会抛 ValidationError
    return response_model.model_validate_json(raw)


# ==========================================================================
# 快捷消息构造函数
# ==========================================================================

def user_msg(content: str) -> dict:
    """构造一条 user 角色消息。"""
    return {"role": "user", "content": content}


def assistant_msg(content: str) -> dict:
    """构造一条 assistant 角色消息（用于对话历史）。"""
    return {"role": "assistant", "content": content}


def system_msg(content: str) -> dict:
    """构造一条 system 角色消息（用于设定 LLM 行为）。"""
    return {"role": "system", "content": content}
