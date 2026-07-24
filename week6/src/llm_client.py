"""
LLM 客户端 — V5 多模型支持。

V4: DeepSeek 单模型
V5: LLMProvider 抽象层，支持 DeepSeek + 智谱 GLM，运行时切换

用法:
    from src.llm_client import chat, chat_structured, user_msg

    # 默认用 DeepSeek（向后兼容）
    reply = chat([user_msg("你好")], temperature=0.3)

    # 切换提供商
    from src.llm_client import set_provider
    set_provider("zhipu")

环境变量:
    LLM_PROVIDER          — "deepseek"(默认) | "zhipu"  切换提供商
    DEEPSEEK_API_KEY      — DeepSeek API 密钥
    DEEPSEEK_API_BASE     — DeepSeek API 地址
    DEEPSEEK_MODEL        — DeepSeek 模型名
    ZHIPU_API_KEY         — 智谱 API 密钥
    ZHIPU_API_BASE        — 智谱 API 地址
    ZHIPU_MODEL           — 智谱模型名
"""

import json
import logging
import os
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Type, TypeVar

import requests
from pydantic import BaseModel

# V5: 自动加载项目根目录的 .env 文件
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).parent.parent.parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
except ImportError:
    pass

logger = logging.getLogger("llm_client")

# ============================================================================
# V4: 全局 token 计数器（线程安全）
# ============================================================================
from threading import Lock

_token_lock = Lock()
_token_stats = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "api_calls": 0,
    "model": "",
}

_MAX_RETRIES = 3
_BASE_DELAY = 2.0

T = TypeVar("T", bound=BaseModel)


def get_token_stats() -> dict:
    """返回当前累计 token 统计的副本，含当前提供商信息。"""
    with _token_lock:
        stats = dict(_token_stats)
    if _provider is not None:
        stats["provider"] = _provider.provider_name
        stats["model"] = _provider.model_name
    return stats


def reset_token_stats():
    """重置 token 计数器。"""
    with _token_lock:
        for k in _token_stats:
            if isinstance(_token_stats[k], int):
                _token_stats[k] = 0


def _record_usage(usage: dict, provider_name: str = "", model_name: str = ""):
    """记录一次 API 调用的 token 消耗。"""
    with _token_lock:
        _token_stats["prompt_tokens"] += usage.get("prompt_tokens", 0)
        _token_stats["completion_tokens"] += usage.get("completion_tokens", 0)
        _token_stats["total_tokens"] += usage.get("total_tokens", 0)
        _token_stats["api_calls"] += 1
        if not _token_stats["model"] and model_name:
            _token_stats["model"] = model_name


# ============================================================================
# V5: LLMProvider 抽象层
# ============================================================================

class LLMProvider(ABC):
    """LLM 提供商抽象基类。

    所有提供商实现 chat() 和 chat_structured() 两个接口。
    底层 HTTP 请求逻辑（重试、指数退避）共用 _request()。
    """

    def __init__(self, api_key: str, api_base: str, model: str, name: str):
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.model = model
        self._name = name
        self._chat_path = "/v1/chat/completions"  # 子类可覆盖

    @property
    def provider_name(self) -> str:
        return self._name

    @property
    def model_name(self) -> str:
        return self.model

    # ---- 底层 HTTP 请求 ----

    def _request(self, payload: dict, timeout: int = 120) -> dict:
        """发送 POST 请求到 LLM API，带自动重试 + 指数退避。"""
        last_error = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = requests.post(
                    f"{self.api_base}{self._chat_path}",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=timeout,
                )
                if resp.ok:
                    body = resp.json()
                    usage = body.get("usage", {})
                    logger.info(
                        "[%s] API 调用成功 | tokens: in=%s out=%s total=%s",
                        self._name,
                        usage.get("prompt_tokens", "?"),
                        usage.get("completion_tokens", "?"),
                        usage.get("total_tokens", "?"),
                    )
                    _record_usage(usage, self._name, self.model)
                    return body

                detail = resp.text[:300]
                logger.warning("[%s] API 返回 %s (attempt %s): %s", self._name, resp.status_code, attempt, detail)
                last_error = RuntimeError(f"API {resp.status_code}: {detail}")

            except requests.RequestException as e:
                logger.warning("[%s] API 网络错误 (attempt %s): %s", self._name, attempt, e)
                last_error = e

            if attempt < _MAX_RETRIES:
                delay = _BASE_DELAY * (2 ** (attempt - 1))
                logger.info("[%s] 重试 %s/%s, 等待 %.1fs", self._name, attempt + 1, _MAX_RETRIES, delay)
                time.sleep(delay)

        raise RuntimeError(f"[{self._name}] API 调用失败 ({_MAX_RETRIES} 次重试后): {last_error}")

    # ---- 公共接口 ----

    def chat(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.3,
        max_tokens: int = 8192,
    ) -> str:
        """发送对话请求，返回纯文本。"""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        body = self._request(payload)
        return body["choices"][0]["message"]["content"]

    def chat_structured(
        self,
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
            "model": self.model,
            "messages": full_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        body = self._request(payload)
        raw = body["choices"][0]["message"]["content"].strip()

        # 清洗可能的 markdown 包裹
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        return response_model.model_validate_json(raw)


# ============================================================================
# V5: 具体提供商实现
# ============================================================================

class DeepSeekProvider(LLMProvider):
    """DeepSeek API 提供商。

    从环境变量读取:
        DEEPSEEK_API_KEY  — API 密钥
        DEEPSEEK_API_BASE — API 地址（默认 https://api.deepseek.com）
        DEEPSEEK_MODEL    — 模型名（默认 deepseek-v4-pro）
    """

    def __init__(self):
        super().__init__(
            api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
            api_base=os.environ.get("DEEPSEEK_API_BASE", "https://api.deepseek.com"),
            model=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro"),
            name="deepseek",
        )


class ZhipuProvider(LLMProvider):
    """智谱 AI（GLM）提供商。

    智谱的 API 与 OpenAI 兼容，但端点路径不同。
    适用于 glm-4 / glm-4-flash 等模型。

    从环境变量读取:
        ZHIPU_API_KEY  — API 密钥
        ZHIPU_API_BASE — API 地址（默认 https://open.bigmodel.cn/api/paas/v4）
        ZHIPU_MODEL    — 模型名（默认 glm-4）
    """

    def __init__(self):
        super().__init__(
            api_key=os.environ.get("ZHIPU_API_KEY", ""),
            api_base=os.environ.get("ZHIPU_API_BASE", "https://open.bigmodel.cn/api/paas/v4"),
            model=os.environ.get("ZHIPU_MODEL", "glm-4"),
            name="zhipu",
        )


# ============================================================================
# V5: 模块级单例管理
# ============================================================================

_provider: LLMProvider | None = None


def _init_provider() -> LLMProvider:
    """初始化 LLM 提供商，根据环境变量 LLM_PROVIDER 选择。"""
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
    """运行时切换 LLM 提供商。

    Args:
        name: "deepseek" 或 "zhipu"

    Returns:
        新的 LLMProvider 实例

    Raises:
        ValueError: 不支持的提供商名称
    """
    global _provider
    old_name = _provider.provider_name if _provider else "none"
    name = name.lower()

    if name == "deepseek":
        _provider = DeepSeekProvider()
    elif name in ("zhipu", "glm"):
        _provider = ZhipuProvider()
    else:
        raise ValueError(f"不支持的 LLM 提供商: {name}。支持: deepseek, zhipu")

    logger.info("LLM 提供商切换: %s → %s (%s)", old_name, _provider.provider_name, _provider.model_name)
    return _provider


def get_provider() -> LLMProvider:
    """返回当前 LLM 提供商。"""
    return _provider


def get_available_providers() -> dict[str, str]:
    """返回所有可用的提供商及其模型名称。

    用于 Streamlit UI 侧栏下拉菜单。
    """
    providers = {
        "deepseek": os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro"),
    }
    if os.environ.get("ZHIPU_API_KEY"):
        providers["zhipu"] = os.environ.get("ZHIPU_MODEL", "glm-4")
    return providers


# 模块加载时自动初始化
_init_provider()


# ============================================================================
# 向后兼容的模块级函数（委托给当前 _provider）
# ============================================================================

def chat(
    messages: list[dict],
    *,
    temperature: float = 0.3,
    max_tokens: int = 8192,
) -> str:
    """发送对话请求，返回纯文本。（委托给当前 LLMProvider）"""
    return _provider.chat(messages, temperature=temperature, max_tokens=max_tokens)


def chat_structured(
    messages: list[dict],
    response_model: Type[T],
    *,
    temperature: float = 0.3,
    max_tokens: int = 8192,
) -> T:
    """发送对话请求，返回 Pydantic 结构化对象。（委托给当前 LLMProvider）"""
    return _provider.chat_structured(
        messages, response_model,
        temperature=temperature, max_tokens=max_tokens,
    )


def user_msg(content: str) -> dict:
    """构造一条 user 角色消息。"""
    return {"role": "user", "content": content}


def assistant_msg(content: str) -> dict:
    """构造一条 assistant 角色消息。"""
    return {"role": "assistant", "content": content}


def system_msg(content: str) -> dict:
    """构造一条 system 角色消息。"""
    return {"role": "system", "content": content}
