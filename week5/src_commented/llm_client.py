# -*- coding: utf-8 -*-
"""
=============================================================================
llm_client.py — LLM 客户端（V5 版：+ 多模型支持）
=============================================================================

这是整个系统与 LLM 通信的唯一入口。所有 LLM 调用都经过这里。

V4 已有:
  - chat()              — 发消息，返回纯文本
  - chat_structured()   — 发消息，返回 Pydantic 对象（JSON Schema 约束）
  - get_token_stats()   — 获取累计 token 消耗
  - reset_token_stats() — 重置计数器
  - _record_usage()     — 每次 API 调用后累加 token 数
  - 线程安全：用 threading.Lock() 保护全局计数器

V5 新增:
  - LLMProvider 抽象基类        — 统一 chat() / chat_structured() 接口
  - DeepSeekProvider            — DeepSeek 实现（V4 逻辑重构进类）
  - ZhipuProvider              — 智谱 GLM 实现
  - set_provider()              — 运行时切换提供商
  - get_provider()              — 查询当前提供商
  - get_available_providers()   — 列出可用提供商（供 Streamlit UI）
  - _init_provider()            — 根据环境变量 LLM_PROVIDER 自动初始化

为什么需要抽象层:
  - V5 的 Streamlit UI 需要侧栏切换模型
  - V6 的 5-Agent 可能需要不同 Agent 用不同模型
  - V7 的 benchmark 需要多模型对比数据
  - 向后兼容: 现有代码 from src.llm_client import chat 完全不用改

用法:
    from src.llm_client import chat, user_msg
    reply = chat([user_msg("你好")], temperature=0.3)

    # 切换模型
    from src.llm_client import set_provider
    set_provider("zhipu")
=============================================================================
"""

import json
import logging
import os
import time
from abc import ABC, abstractmethod         # V5: 抽象基类支持
from typing import Type, TypeVar

import requests                             # HTTP 请求库
from pydantic import BaseModel              # Pydantic 基类（用于 chat_structured）

logger = logging.getLogger("llm_client")

# ==========================================================================
# V4: 全局 token 计数器（线程安全）
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
    "model": "",                        # 使用的模型名（首次调用时记录）
}

# --- 重试参数 ---
_MAX_RETRIES = 3       # 最大重试次数
_BASE_DELAY = 2.0      # 首次重试等待秒数（指数退避: 2s → 4s → 8s）

T = TypeVar("T", bound=BaseModel)       # 泛型: 约束 T 必须是 BaseModel 子类


def get_token_stats() -> dict:
    """
    返回当前累计 token 统计的副本（线程安全）。

    V5 改进: 返回结果中自动附加当前提供商名称和模型名。
    实验完成后调用此函数，写入 token_usage.json。

    Returns:
        {"prompt_tokens": 1234, "completion_tokens": 567, ...,
         "provider": "deepseek", "model": "deepseek-v4-pro"}
    """
    with _token_lock:                   # 获取锁，保证读取时不被其他线程修改
        stats = dict(_token_stats)      # dict() 创建浅拷贝
    # V5: 附加当前提供商信息
    if _provider is not None:
        stats["provider"] = _provider.provider_name
        stats["model"] = _provider.model_name
    return stats


def reset_token_stats():
    """
    重置 token 计数器（线程安全）。

    每次跑实验前调用，确保统计数字从 0 开始。
    """
    with _token_lock:
        for k in _token_stats:
            if isinstance(_token_stats[k], int):  # 只重置 int 字段，model 保留了
                _token_stats[k] = 0


def _record_usage(usage: dict, provider_name: str = "", model_name: str = ""):
    """
    记录一次 API 调用的 token 消耗（线程安全）。

    V5 改进: 接受 provider_name 和 model_name 参数，
    用于 get_token_stats() 返回当前提供商信息。

    Args:
        usage: API response body 中的 usage 字段
            例: {"prompt_tokens": 1500, "completion_tokens": 800, "total_tokens": 2300}
        provider_name: 提供商名称（如 "deepseek"）
        model_name: 模型名（如 "deepseek-v4-pro"）
    """
    with _token_lock:                   # 获取锁，保护写操作
        _token_stats["prompt_tokens"] += usage.get("prompt_tokens", 0)
        _token_stats["completion_tokens"] += usage.get("completion_tokens", 0)
        _token_stats["total_tokens"] += usage.get("total_tokens", 0)
        _token_stats["api_calls"] += 1
        if not _token_stats["model"] and model_name:  # 首次调用时记录模型名
            _token_stats["model"] = model_name


# ==========================================================================
# V5: LLMProvider 抽象层
# ==========================================================================
# 为什么需要抽象层:
#   - V5 功能需求: Streamlit UI 侧栏切换模型
#   - V6 需求前瞻: 5-Agent 系统可能需要不同 Agent 用不同模型
#     （例如: 需求分析用 DeepSeek，代码生成用 GPT-4o）
#   - V7 需求前瞻: benchmark 需要对比不同模型在同一任务上的效果
#
# 设计原则:
#   - 所有提供商实现相同的 chat() / chat_structured() 接口
#   - 底层 HTTP 请求逻辑（重试、指数退避）放在基类 _request() 中复用
#   - 每个提供商从自己的环境变量读取配置（密钥、地址、模型名）
#   - 模块级 chat() / chat_structured() 函数改为委托给当前 _provider


class LLMProvider(ABC):
    """
    LLM 提供商抽象基类。

    所有提供商必须实现:
      - provider_name  属性 → 返回 "deepseek" / "zhipu" 等
      - model_name     属性 → 返回模型名
      - chat()          方法 → 对话，返回纯文本
      - chat_structured() 方法 → 对话，返回 Pydantic 对象

    基类已提供:
      - _request()      方法 → HTTP POST + 重试 + 指数退避（子类直接调）
      - chat()          默认实现 → 标准 与 Zhipu 兼容协议的 chat/completions
      - chat_structured() 默认实现 → JSON Schema 约束的结构化输出
    """

    def __init__(self, api_key: str, api_base: str, model: str, name: str):
        """
        Args:
            api_key: API 密钥（Bearer Token）
            api_base: API 地址（如 https://api.deepseek.com）
            model: 模型名（如 deepseek-v4-pro）
            name: 提供商标识（如 "deepseek"）
        """
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.model = model
        self._name = name
        self._chat_path = "/v1/chat/completions"  # V5: 子类可覆盖路径

    @property
    def provider_name(self) -> str:
        """提供商名称，如 "deepseek" / "zhipu"。"""
        return self._name

    @property
    def model_name(self) -> str:
        """当前使用的模型名，如 "deepseek-v4-pro" / "glm-4"。"""
        return self.model

    # ---- 底层 HTTP 请求 ----

    def _request(self, payload: dict, timeout: int = 120) -> dict:
        """
        发送 POST 请求到 LLM API，带自动重试 + 指数退避。

        V4 中这是模块级函数 _api_request()，直接读取模块级常量 API_KEY/API_BASE/MODEL。
        V5 改为 LLMProvider 的方法，从 self 读取配置。重试逻辑完全不变。

        重试策略: 指数退避（exponential backoff）
          第1次失败 → 等 2s
          第2次失败 → 等 4s
          第3次失败 → 抛出异常

        Args:
            payload: 请求体（model, messages, temperature, max_tokens 等）
            timeout: 单次 HTTP 超时秒数

        Returns:
            API response body dict（包含 choices[0].message.content 和 usage）
        """
        last_error = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                # 发送 HTTP POST 请求
                # 使用 self.api_base（而不是全局 API_BASE），加上可配置的 _chat_path
                resp = requests.post(
                    f"{self.api_base}{self._chat_path}",   # V5: 可配置路径
                    headers={
                        "Authorization": f"Bearer {self.api_key}",  # Bearer Token 认证
                        "Content-Type": "application/json",
                    },
                    json=payload,                             # requests 自动序列化为 JSON
                    timeout=timeout,
                )

                # HTTP 200 → 解析响应
                if resp.ok:
                    body = resp.json()
                    usage = body.get("usage", {})              # 提取 token 使用量
                    logger.info(
                        "[%s] API 调用成功 | tokens: in=%s out=%s total=%s",
                        self._name,
                        usage.get("prompt_tokens", "?"),
                        usage.get("completion_tokens", "?"),
                        usage.get("total_tokens", "?"),
                    )
                    _record_usage(usage, self._name, self.model)  # V5: 传提供商信息
                    return body

                # HTTP 非 200 → 记录错误，准备重试
                detail = resp.text[:300]                        # 只取前 300 字符防日志爆炸
                logger.warning("[%s] API 返回 %s (attempt %s): %s",
                              self._name, resp.status_code, attempt, detail)
                last_error = RuntimeError(f"API {resp.status_code}: {detail}")

            except requests.RequestException as e:               # 网络层异常（DNS/连接超时等）
                logger.warning("[%s] API 网络错误 (attempt %s): %s", self._name, attempt, e)
                last_error = e

            # 如果不是最后一次尝试，等待后重试
            if attempt < _MAX_RETRIES:
                delay = _BASE_DELAY * (2 ** (attempt - 1))       # 2^0=2, 2^1=4, 2^2=8
                logger.info("[%s] 重试 %s/%s, 等待 %.1fs",
                           self._name, attempt + 1, _MAX_RETRIES, delay)
                time.sleep(delay)                                # 阻塞等待

        # 3 次全部失败 → 抛异常
        raise RuntimeError(f"[{self._name}] API 调用失败 ({_MAX_RETRIES} 次重试后): {last_error}")

    # ---- 公共接口 ----

    def chat(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.3,         # 温度: 0=确定, 1=创意
        max_tokens: int = 8192,
    ) -> str:
        """
        发送对话请求，返回 LLM 的纯文本回复。

        这是最常用的接口。所有节点的 LLM 调用都通过这个方法。

        V4 中这是模块级函数，读全局 MODEL 常量。
        V5 移到 LLMProvider 基类，使用 self.model。
        两个子类（DeepSeek / 智谱 GLM）直接继承此实现，不需要重写——
        因为它们都走 与 Zhipu 兼容的 /v1/chat/completions 协议。

        Args:
            messages: 消息列表，每条是 {"role": "user/assistant/system", "content": "..."}
            temperature: 0.0-1.0，越低越确定，越高越随机
            max_tokens: 最大输出 token 数

        Returns:
            LLM 回复的纯文本
        """
        payload = {
            "model": self.model,          # V5: 使用 self.model 而非全局 MODEL
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        body = self._request(payload)
        # 从 response body 中提取文本内容
        # body["choices"][0]["message"]["content"] 是 与 Zhipu 兼容的标准路径
        return body["choices"][0]["message"]["content"]

    def chat_structured(
        self,
        messages: list[dict],
        response_model: Type[T],           # Pydantic 模型类
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

        注意:
          response_format={"type": "json_object"} 是 与 Zhipu 兼容的参数。
          大多数国产模型（DeepSeek、通义千问、GLM 等）都支持。
          如果某个提供商不支持，需要在子类重写此方法（去掉 response_format 行）。

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
            "model": self.model,          # V5: 使用 self.model 而非全局 MODEL
            "messages": full_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},  # ← 告诉 API 要 JSON 格式
        }
        body = self._request(payload)
        raw = body["choices"][0]["message"]["content"].strip()

        # 清洗可能的 markdown 包裹（有时 LLM 还是包了 ```json ... ```）
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        # 用 Pydantic 校验 JSON → 如果 JSON 不合 Schema，这里会抛 ValidationError
        return response_model.model_validate_json(raw)


# ==========================================================================
# V5: 具体提供商实现
# ==========================================================================
# 两个子类都非常简短——因为核心逻辑都在 LLMProvider 基类。
# 它们只负责从环境变量读取各自的配置，传给基类的 __init__。
#
# 如果要加新提供商（如 Anthropic Claude）:
#   1. 创建一个继承 LLMProvider 的子类
#   2. 在 __init__ 中从环境变量读配置
#   3. 如果该提供商的 API 不是 与 Zhipu 兼容协议（如 Anthropic 的 /v1/messages），
#      需要重写 chat() 和 chat_structured() 方法


class DeepSeekProvider(LLMProvider):
    """
    DeepSeek API 提供商（V4 默认）。

    从环境变量读取:
        DEEPSEEK_API_KEY  — API 密钥（必须）
        DEEPSEEK_API_BASE — API 地址（默认 https://api.deepseek.com）
        DEEPSEEK_MODEL    — 模型名（默认 deepseek-v4-pro）

    DeepSeek 的 API 与 Zhipu 完全兼容，所以直接继承基类的 chat/chat_structured。
    不需要重写任何方法。
    """

    def __init__(self):
        super().__init__(
            api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
            api_base=os.environ.get("DEEPSEEK_API_BASE", "https://api.deepseek.com"),
            model=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro"),
            name="deepseek",
        )


class ZhipuProvider(LLMProvider):
    """
    智谱 AI（GLM）提供商（V5 新增）。

    智谱的 API 与 OpenAI 兼容，但端点路径不同（/api/paas/v4/chat/completions）。
    适用于 glm-4 / glm-4-flash 等模型。

    从环境变量读取:
        ZHIPU_API_KEY  — API 密钥（必须）
        ZHIPU_API_BASE — API 地址（默认 https://open.bigmodel.cn/api/paas/v4）
        ZHIPU_MODEL    — 模型名（默认 glm-4）

    用法:
        # 设置环境变量
        export LLM_PROVIDER=zhipu
        export ZHIPU_API_KEY=xxx
        export ZHIPU_MODEL=glm-4

        # 或代码中切换
        from src.llm_client import set_provider
        set_provider("zhipu")
    """

    def __init__(self):
        super().__init__(
            api_key=os.environ.get("ZHIPU_API_KEY", ""),
            api_base=os.environ.get("ZHIPU_API_BASE", "https://open.bigmodel.cn/api/paas/v4"),
            model=os.environ.get("ZHIPU_MODEL", "glm-4"),
            name="zhipu",
        )


# ==========================================================================
# V5: 模块级单例管理
# ==========================================================================
# 为什么用模块级单例:
#   1. 当前系统是单线程 LangGraph，一次只用一种模型
#   2. 所有节点的 import (from src.llm_client import chat) 通过模块级函数委托
#   3. set_provider() 切换后，后续所有 chat() 调用自动使用新模型
#   4. V6 如果需要"不同节点用不同模型"，可以改为 per-node provider 注入
#
# 初始化时机: 模块 import 时自动调用 _init_provider()。
# 如果找不到 API Key 也不会报错——延迟到第一次 API 调用时才暴露。


_provider: LLMProvider | None = None    # 模块级单例，import 时自动初始化


def _init_provider() -> LLMProvider:
    """
    根据环境变量 LLM_PROVIDER 初始化 LLM 提供商。

    调用时机: 模块 import 时自动执行一次。

    优先级:
      1. LLM_PROVIDER 环境变量 → "deepseek" 或 "zhipu"
      2. 没设 → 默认 "deepseek"（向后兼容 V4 行为）
      3. 设了但未知 → 警告 + 回退到 "deepseek"

    Returns:
        初始化后的 LLMProvider 实例
    """
    global _provider
    name = os.environ.get("LLM_PROVIDER", "deepseek").lower()

    if name == "deepseek":
        _provider = DeepSeekProvider()
    elif name in ("zhipu", "glm"):
        _provider = ZhipuProvider()
    else:
        logger.warning("未知的 LLM_PROVIDER '%s'，回退到 deepseek", name)
        _provider = DeepSeekProvider()

    logger.info("LLM 提供商: %s | 模型: %s | API: %s",
                _provider.provider_name, _provider.model_name, _provider.api_base)
    return _provider


def set_provider(name: str) -> LLMProvider:
    """
    运行时切换 LLM 提供商。

    调用后，所有后续的 chat() / chat_structured() 调用都会使用新提供商。
    切换是即时的——不需要重启进程或重新初始化状态图。

    典型使用场景:
      - Streamlit UI 侧栏下拉菜单选择模型后调用
      - 实验框架中根据 --provider 参数切换
      - 交互模式下运行时热切换

    Args:
        name: "deepseek" 或 "zhipu"

    Returns:
        新的 LLMProvider 实例

    Raises:
        ValueError: 不支持的提供商名称（如 set_provider("claude")）
    """
    global _provider
    old_name = _provider.provider_name if _provider else "none"
    name = name.lower()

    if name == "deepseek":
        _provider = DeepSeekProvider()
    elif name in ("zhipu", "glm"):
        _provider = ZhipuProvider()
    else:
        raise ValueError(
            f"不支持的 LLM 提供商: {name}。当前支持: deepseek, zhipu"
        )

    logger.info("LLM 提供商切换: %s → %s (%s)",
                old_name, _provider.provider_name, _provider.model_name)
    return _provider


def get_provider() -> LLMProvider:
    """
    返回当前活跃的 LLM 提供商。

    用于: 需要直接访问 provider 的场景，如:
      - 想打印当前使用的模型名
      - 想检查 provider 是否支持某个特性
    """
    return _provider


def get_available_providers() -> dict[str, str]:
    """
    返回所有可用的提供商及其模型名。

    "可用"的定义:
      - deepseek: 始终在列表中（因为 DEEPSEEK_MODEL 有默认值）
      - zhipu: 仅当 ZHIPU_API_KEY 已设置时才出现

    用于: Streamlit UI 的侧栏下拉菜单。
    例: {"deepseek": "deepseek-v4-pro", "zhipu": "glm-4"}

    Returns:
        {provider_name: model_name} 字典
    """
    providers = {
        "deepseek": os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro"),
    }
    # zhipu 需要 API Key 才能用——没 Key 就不显示
    if os.environ.get("ZHIPU_API_KEY"):
        providers["zhipu"] = os.environ.get("ZHIPU_MODEL", "glm-4")
    return providers


# ==========================================================================
# 模块加载时自动初始化
# ==========================================================================
# 这行代码在 import 时执行一次。相当于之前的模块级常量初始化。
_init_provider()


# ==========================================================================
# 向后兼容的模块级函数（委托给当前 _provider）
# ==========================================================================
# V4 代码中到处都是:
#     from src.llm_client import chat, user_msg
#     reply = chat([...], temperature=0.3)
#
# V5 保持这些函数名不变，内部改为委托给 _provider 的同名方法。
# 这样所有节点代码（node1~node4, node_quality, run_experiment）完全不需要改。

def chat(
    messages: list[dict],
    *,
    temperature: float = 0.3,
    max_tokens: int = 8192,
) -> str:
    """
    发送对话请求，返回纯文本。

    V5: 委托给当前 _provider.chat()。
    调用方不需要知道底层用的是 DeepSeek 还是智谱。
    """
    return _provider.chat(messages, temperature=temperature, max_tokens=max_tokens)


def chat_structured(
    messages: list[dict],
    response_model: Type[T],
    *,
    temperature: float = 0.3,
    max_tokens: int = 8192,
) -> T:
    """
    发送对话请求，返回 Pydantic 结构化对象。

    V5: 委托给当前 _provider.chat_structured()。
    """
    return _provider.chat_structured(
        messages, response_model,
        temperature=temperature, max_tokens=max_tokens,
    )


# ==========================================================================
# 快捷消息构造函数（V4 已有，V5 不变）
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
