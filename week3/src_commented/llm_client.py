# -*- coding: utf-8 -*-
"""
=============================================================================
llm_client.py — DeepSeek LLM 调用封装
=============================================================================

这个文件是对 DeepSeek API 的统一封装，给所有节点使用。

核心函数：
  chat()            — 发消息给 LLM，返回文本
  chat_structured() — 发消息给 LLM，返回 Pydantic 结构化对象
  user_msg()        — 构造一条"用户消息"字典
  assistant_msg()   — 构造一条"助手消息"字典
  system_msg()      — 构造一条"系统消息"字典

V2 相比 V1 新增：
  1. API 调用失败自动重试（最多 3 次，指数退避）
  2. logging 记录每次调用的 token 消耗
  3. max_tokens 默认从 16384 提到 8192

LLM 调用通过 HTTP POST 请求完成，遵循 OpenAI 兼容的 API 格式：
  POST https://api.deepseek.com/v1/chat/completions
  请求体: { model, messages, temperature, max_tokens, ... }
  响应:   { choices: [{ message: { content: "..." } }], usage: { ... } }
"""

# ====================================================================
# 导入
# ====================================================================

import json                                              # 处理 JSON（构建 prompt、解析响应）
import logging                                           # 记录日志
import os                                                # 读取环境变量（API Key）
import time                                              # sleep（重试等待）
from typing import Type, TypeVar                         # 泛型类型标注

import requests                                          # HTTP 请求库（pip install requests）
from pydantic import BaseModel                           # Pydantic 基类

# ---- 创建本模块的 logger ----
logger = logging.getLogger("llm_client")                  # getLogger 创建一个日志记录器
# 日志级别从低到高: DEBUG < INFO < WARNING < ERROR < CRITICAL
# 只有 >= 当前配置级别的日志才会输出

# ---- 从环境变量读取配置 ----
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")          # 读取 API 密钥（没有则为空字符串）
API_BASE = os.environ.get("DEEPSEEK_API_BASE", "https://api.deepseek.com")
# https://api.deepseek.com 是 DeepSeek 的默认 API 地址
# 如果你用的是代理或其他兼容服务，改环境变量 DEEPSEEK_API_BASE

MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
# 模型名，可选: deepseek-chat, deepseek-reasoner, deepseek-v4-pro

# ---- 泛型变量 T 用于 chat_structured 的类型推断 ----
T = TypeVar("T", bound=BaseModel)                        # T 可以被任何继承 BaseModel 的类型替换
# 例如: T 可以是 StructuredRequirement、SysMLArtifact 等

# ---- 重试配置 ----
_MAX_RETRIES = 3                                         # API 调用失败后最多重试 3 次
_BASE_DELAY = 2.0                                        # 第一次重试等待 2 秒（后续指数增长：4秒, 8秒）


# ====================================================================
# _api_request() — 底层 HTTP 请求（带重试逻辑）
# ====================================================================
def _api_request(payload: dict, timeout: int = 120) -> dict:
    """
    发送 HTTP POST 请求到 DeepSeek API，带自动重试。

    参数:
      payload:  请求体字典（model, messages, temperature 等）
      timeout:  HTTP 超时秒数（默认 120s）

    返回:
      API 响应的 JSON 字典

    异常:
      3 次重试后仍失败 → 抛出 RuntimeError

    重试策略（指数退避 exponential backoff）:
      第 1 次失败 → 等 2 秒
      第 2 次失败 → 等 4 秒
      第 3 次失败 → 不再重试，抛出异常
    """
    last_error = None                                    # 记录最后一次错误

    for attempt in range(1, _MAX_RETRIES + 1):           # 循环: attempt = 1, 2, 3
        try:
            # ---- 发送 HTTP POST 请求 ----
            resp = requests.post(                        # requests.post(url, headers=..., json=...)
                f"{API_BASE}/v1/chat/completions",        # 完整的 API 端点 URL
                headers={                                # HTTP 请求头
                    "Authorization": f"Bearer {API_KEY}", # Bearer Token 认证
                    "Content-Type": "application/json",   # 告诉服务器：我发的是 JSON
                },
                json=payload,                            # 请求体（自动序列化为 JSON）
                timeout=timeout,                         # 超过 120 秒无响应就断开
            )

            if resp.ok:                                  # HTTP 状态码 2xx = ok（200, 201 等）
                body = resp.json()                       #   解析响应 JSON
                usage = body.get("usage", {})            #   提取 token 用量统计
                logger.info(                             #   记录 INFO 级别日志
                    "API 调用成功 | tokens: in=%s out=%s total=%s",
                    usage.get("prompt_tokens", "?"),      #     输入 token 数
                    usage.get("completion_tokens", "?"),  #     输出 token 数
                    usage.get("total_tokens", "?"),       #     总 token 数
                )
                return body                              #   返回响应体

            # ---- 如果 HTTP 状态码不是 2xx ----
            detail = resp.text[:300]                     # 取出前 300 个字符（防止错误页面太长）
            logger.warning(                              # 记录 WARNING 级别日志
                "API 返回 %s (attempt %s): %s",
                resp.status_code, attempt, detail
            )
            last_error = RuntimeError(                   # 构造一个 RuntimeError
                f"API {resp.status_code}: {detail}"
            )

        except requests.RequestException as e:           # 网络异常（DNS 解析失败、连接超时等）
            logger.warning("API 网络错误 (attempt %s): %s", attempt, e)
            last_error = e

        # ---- 如果还能重试，等待后继续 ----
        if attempt < _MAX_RETRIES:
            delay = _BASE_DELAY * (2 ** (attempt - 1))   # 指数退避: 2^0=1, 2^1=2, 2^2=4 → 乘以基数2.0 → 2, 4, 8 秒
            logger.info("重试 %s/%s, 等待 %.1fs", attempt + 1, _MAX_RETRIES, delay)
            time.sleep(delay)                            # 暂停 delay 秒

    # ---- 3 次全部失败 → 抛出异常 ----
    raise RuntimeError(f"API 调用失败 ({_MAX_RETRIES} 次重试后): {last_error}")


# ====================================================================
# chat() — 对话接口（纯文本返回）
# ====================================================================
def chat(
    messages: list[dict],                                # 消息列表，每个元素是 {"role": "...", "content": "..."}
    *,
    temperature: float = 0.3,                            # 温度参数：0=确定、1=随机（越高越"有创造力"）
    max_tokens: int = 8192,                              # 最大输出 token 数
) -> str:
    """
    发送对话给 LLM，返回纯文本。

    用法:
        response = chat([
            {"role": "user", "content": "你好"}
        ])
        print(response)  # → "你好！有什么可以帮助你的吗？"

    注意:
      这个函数不启用 JSON Mode，LLM 可以自由输出文字。
      需要结构化输出时用 chat_structured()。
    """
    # ---- 构建请求体 ----
    payload = {                                          # 这是 OpenAI 兼容的请求格式
        "model": MODEL,                                  #   模型名，如 "deepseek-v4-pro"
        "messages": messages,                            #   对话消息列表
        "temperature": temperature,                      #   随机度
        "max_tokens": max_tokens,                        #   限制 LLM 输出长度
    }
    body = _api_request(payload)                         # 发送请求（带重试）

    return body["choices"][0]["message"]["content"]      # 从响应中提取文本
    # 响应结构: {"choices": [{"message": {"content": "..."}}]}
    # choices[0] = 第一个（通常也是唯一一个）选项
    # message.content = LLM 的回复文本


# ====================================================================
# chat_structured() — 结构化对话接口
# ====================================================================
def chat_structured(
    messages: list[dict],
    response_model: Type[T],                             # 目标 Pydantic 类型，如 StructuredRequirement
    *,
    temperature: float = 0.3,
    max_tokens: int = 8192,
) -> T:                                                  # 返回值类型是 T（你传入的 response_model 类型）
    """
    发送对话给 LLM，返回 Pydantic 结构化对象。

    用法:
        req = chat_structured(
            [user_msg("设计一个 RC 滤波器")],
            response_model=StructuredRequirement,
        )
        print(req.component_type)  # → "RC低通滤波器"

    原理:
      1. 在 system prompt 中注入 JSON Schema（告诉 LLM 输出格式）
      2. 设置 response_format={"type": "json_object"}（强制 LLM 输出 JSON）
      3. LLM 返回 JSON → Pydantic 验证 → 返回结构化对象
    """
    # ---- 构造 JSON Schema ----
    schema = json.dumps(                                 # 把 Pydantic model 转成 JSON Schema 字符串
        response_model.model_json_schema(),              # model_json_schema() 是 Pydantic 内置方法
        ensure_ascii=False,                              #   ensure_ascii=False: 允许中文字符
        indent=2                                         #   indent=2: 格式化输出（2 空格缩进）
    )

    # ---- 构造 system prompt（注入格式要求） ----
    system_prompt = {
        "role": "system",                                # system 角色：给 LLM 设定行为规则
        "content": (
            "你必须始终输出合法的 JSON 对象。\n"
            f"JSON Schema:\n{schema}\n\n"
            "只输出 JSON，不要包含任何解释、markdown 代码块标记。"
        ),
    }

    full_messages = [system_prompt] + list(messages)     # system prompt 放在最前面

    # ---- 构建请求（启用 JSON Mode） ----
    payload = {
        "model": MODEL,
        "messages": full_messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},      # 关键：强制 LLM 输出合法 JSON
        # 不是所有模型都支持。DeepSeek 部分模型支持，需要在 API 文档中确认。
    }
    body = _api_request(payload)                         # 发送请求

    raw = body["choices"][0]["message"]["content"].strip()  # 提取文本

    # ---- 清洗可能的 markdown 代码块 ----
    # 即使要求 LLM 不要加 ``` ，有时它还是会加
    if raw.startswith("```"):                            # 如果以 ``` 开头
        lines = raw.split("\n")                          #   拆行
        raw = "\n".join(                                 #   重新拼接
            lines[1:-1] if lines[-1].strip() == "```"    #   如果最后一行是 ```：去头去尾
            else lines[1:]                                #   否则只去头
        )

    return response_model.model_validate_json(raw)       # Pydantic 校验 + 返回
    # model_validate_json: 把 JSON 字符串解析为 Pydantic 对象
    # 如果 JSON 格式不对或字段缺失 → 抛出 ValidationError


# ====================================================================
# 消息构造器 — 快捷函数
# ====================================================================

def user_msg(content: str) -> dict:
    """构造一条用户消息。"""
    return {"role": "user", "content": content}

def assistant_msg(content: str) -> dict:
    """构造一条助手消息（用于保存对话历史）。"""
    return {"role": "assistant", "content": content}

def system_msg(content: str) -> dict:
    """构造一条系统消息（用于设定 LLM 行为）。"""
    return {"role": "system", "content": content}
