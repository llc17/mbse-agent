# -*- coding: utf-8 -*-
"""
=============================================================================
llm_client.py — DeepSeek API 调用封装
=============================================================================

这个文件是对 DeepSeek API 的统一封装，所有节点都通过它来调 LLM。

核心函数:
  chat()            — 发消息，返回纯文本
  chat_structured() — 发消息，返回 Pydantic 结构化对象
  user_msg()        — 构造 "用户消息" 字典
  assistant_msg()   — 构造 "助手消息" 字典
  system_msg()      — 构造 "系统消息" 字典

协议: OpenAI 兼容 API
  POST https://api.deepseek.com/v1/chat/completions
  请求体: { model, messages, temperature, max_tokens }
  响应:   { choices: [{ message: { content: "..." } }], usage: { ... } }

V1 特点:
  - 无自动重试（API 挂了直接报错）
  - 无日志
  - chat_structured 用 JSON Mode 强制 LLM 输出合法 JSON
"""

# ====================================================================
# 导入
# ====================================================================

import json                                              # JSON 序列化/反序列化
import os                                                # 读环境变量（DEEPSEEK_API_KEY）
import sys                                               # sys 模块
from typing import Optional, Type, TypeVar               # 类型标注工具

import requests                                          # HTTP 请求库（pip install requests）
from pydantic import BaseModel                           # Pydantic 基类


# ====================================================================
# 配置 — 从环境变量读取
# ====================================================================

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")          # 读 API 密钥（没有则为空字符串）
# os.environ.get(key, default): 安全读环境变量，key 不存在时返回 default

API_BASE = os.environ.get("DEEPSEEK_API_BASE", "https://api.deepseek.com")
# API 基础地址。如果用的是代理或兼容服务，改环境变量 DEEPSEEK_API_BASE

MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
# 模型名，可选: deepseek-chat, deepseek-reasoner, deepseek-v4-pro

if not API_KEY:                                          # 如果 API Key 没设置
    print("[llm_client] 警告: 未设置 DEEPSEEK_API_KEY 环境变量")
    # 不退出 —— 让后续调用报错时自然发现


# ====================================================================
# 泛型变量 — 用于 chat_structured 的类型推断
# ====================================================================

T = TypeVar("T", bound=BaseModel)                        # T 可以是任何继承 BaseModel 的类型
# TypeVar 是 Python 泛型：约束 T 必须是 BaseModel 的子类
# 这样 IDE 和类型检查器知道 chat_structured 返回的就是你传入的类型


# ====================================================================
# chat() — 纯文本对话
# ====================================================================
def chat(
    messages: list[dict],                                # 对话消息列表
    *,                                                   # * 后面的参数必须用关键字传（不能按位置）
    temperature: float = 0.3,                            # 温度参数：0=确定、1=随机
    max_tokens: int = 16384,                             # 最大输出 token 数
) -> str:
    """
    发送对话给 LLM，返回纯文本。

    用法:
        response = chat([{"role": "user", "content": "你好"}])
    """
    # ---- 发送 HTTP POST 请求 ----
    resp = requests.post(                                # requests 库的 post 方法
        f"{API_BASE}/v1/chat/completions",               # 完整 API 端点
        headers={                                        # HTTP 请求头
            "Authorization": f"Bearer {API_KEY}",         # Bearer Token 认证（f-string 格式化）
            "Content-Type": "application/json",           # 声明请求体是 JSON
        },
        json={                                           # 请求体（requests 自动序列化为 JSON）
            "model": MODEL,                              #   模型名
            "messages": messages,                        #   对话消息
            "temperature": temperature,                  #   温度
            "max_tokens": max_tokens,                    #   最大输出
        },
        timeout=120,                                     # 超时 120 秒
    )

    # ---- 检查 HTTP 状态码 ----
    if not resp.ok:                                      # resp.ok = HTTP 2xx
        detail = resp.text[:500]                         # 取前 500 字符错误信息
        raise RuntimeError(                              # 抛出运行时异常
            f"API 调用失败 ({resp.status_code}): {detail}\n"
            f"当前模型: {MODEL}（有效值: deepseek-chat / deepseek-reasoner）"
        )

    # ---- 从响应中提取文本 ----
    return resp.json()["choices"][0]["message"]["content"]
    # resp.json() = 把响应体解析为 Python 字典
    # ["choices"][0] = 第一个（通常唯一一个）选项
    # ["message"]["content"] = LLM 回复的文本


# ====================================================================
# chat_structured() — 结构化输出
# ====================================================================
def chat_structured(
    messages: list[dict],
    response_model: Type[T],                             # 目标 Pydantic 类型
    *,                                                   # 后面的参数必须用关键字传
    temperature: float = 0.3,
    max_tokens: int = 16384,
) -> T:                                                  # 返回值类型 = 你传入的 response_model 类型
    """
    发送对话给 LLM，返回 Pydantic 结构化对象。

    原理:
      1. 把 JSON Schema 注入 system prompt（告诉 LLM 输出格式）
      2. 设置 response_format={"type": "json_object"}（强制 LLM 输出 JSON）
      3. LLM 返回 JSON → Pydantic 验证 → 返回对象

    用法:
        req = chat_structured(
            [user_msg("设计一个 RC 滤波器")],
            response_model=StructuredRequirement,
        )
    """
    # ---- 步骤 1: 生成 JSON Schema ----
    system_prompt = {
        "role": "system",                                # system 角色 → 给 LLM 设定行为
        "content": (
            f"你必须始终输出合法的 JSON 对象。\n"
            f"JSON Schema:\n"
            f"{json.dumps(response_model.model_json_schema(), ensure_ascii=False, indent=2)}\n\n"
            # model_json_schema(): Pydantic 内置方法，生成 JSON Schema
            # json.dumps: 把 Python 对象转为 JSON 字符串
            # ensure_ascii=False: 允许中文
            # indent=2: 缩进 2 空格
            f"只输出 JSON，不要包含任何解释、markdown 代码块标记或其他文本。"
        ),
    }

    # ---- 步骤 2: 构造完整消息 ----
    full_messages = [system_prompt] + list(messages)     # system prompt 放在最前面

    # ---- 步骤 3: 发送请求（启用 JSON Mode） ----
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
            "response_format": {"type": "json_object"},  # 关键！强制 LLM 输出合法 JSON
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

    # ---- 步骤 4: 清洗可能的 markdown 代码块 ----
    raw = raw.strip()
    if raw.startswith("```"):                            # 如果 LLM 又加了 ``` 标记
        lines = raw.split("\n")                          #   按行拆分
        raw = "\n".join(                                 #   重新拼接
            lines[1:-1] if lines[-1].strip() == "```"    #   如果最后一行是 ```：去头去尾
            else lines[1:]                                #   否则只去头
        )

    # ---- 步骤 5: Pydantic 验证 ----
    return response_model.model_validate_json(raw)
    # model_validate_json: 把 JSON 字符串解析为 Pydantic 对象
    # 如果格式不对或字段缺失 → 抛出 ValidationError


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
