"""
=============================================================================
llm_client.py — LLM 客户端（V5 多模型 + V6 per-agent provider）
=============================================================================
这是整个系统的 LLM 调用入口，所有 Agent 都通过它调用 LLM。

版本演进:
  V4: DeepSeek 单模型 → 1 个 chat() 函数
  V5: LLMProvider 抽象层 → 支持 DeepSeek + 智谱 GLM，运行时切换
  V6: Per-agent provider → 不同 Agent 可配不同模型（生成用 DeepSeek，审查用智谱）

核心类:
  LLMProvider (ABC)        — 抽象基类，定义 chat() / chat_structured() 接口
  ├── DeepSeekProvider     — DeepSeek API（默认）
  └── ZhipuProvider        — 智谱 GLM（含中转站兼容）

核心函数:
  chat(messages)            — 全局默认提供商的对话调用
  chat_with(agent, messages) — V6: 指定 Agent 的对话调用（支持 per-agent provider）
  set_provider(name)        — 运行时切换全局提供商
  set_agent_provider(agent, provider) — V6: 配置某个 Agent 的提供商

环境变量:
  LLM_PROVIDER              — "deepseek"(默认) | "zhipu"
  AGENT_REVIEW_PROVIDER     — V6: review agent 的提供商（如 "zhipu"）
  DEEPSEEK_API_KEY / DEEPSEEK_API_BASE / DEEPSEEK_MODEL
  ZHIPU_API_KEY / ZHIPU_API_BASE / ZHIPU_MODEL
=============================================================================
"""
import json, logging, os, time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Type, TypeVar
import requests
from pydantic import BaseModel

# ── 自动加载项目根目录 .env 文件（API key 配置）──
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).parent.parent.parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
except ImportError:
    pass

logger = logging.getLogger("llm_client")


# ============================================================================
# V4: 全局 token 计数器（线程安全的累加器）
# ============================================================================
from threading import Lock

_token_lock = Lock()                       # 线程锁——多线程环境下防止计数竞争
_token_stats = {                           # 累计统计（全局唯一实例）
    "prompt_tokens": 0,                    # 总输入 token 数
    "completion_tokens": 0,                # 总输出 token 数
    "total_tokens": 0,                     # 总 token 数
    "api_calls": 0,                        # 总 API 调用次数
    "model": "",                           # 当前模型名
}

_MAX_RETRIES = 3                           # API 调用最多重试 3 次
_BASE_DELAY = 2.0                          # 重试基础延迟（指数退避: 2s → 4s → 8s）

T = TypeVar("T", bound=BaseModel)          # 结构化输出的泛型变量


def get_token_stats() -> dict:
    """返回当前累计 token 统计的副本（线程安全）"""
    with _token_lock:
        stats = dict(_token_stats)
    if _provider is not None:
        stats["provider"] = _provider.provider_name
        stats["model"] = _provider.model_name
    return stats


def reset_token_stats():
    """重置 token 计数器（每次新流水线启动时调用）"""
    with _token_lock:
        for k in _token_stats:
            if isinstance(_token_stats[k], int):
                _token_stats[k] = 0


def _record_usage(usage: dict, provider_name: str = "", model_name: str = ""):
    """记录一次 API 调用的 token 消耗到全局计数器"""
    with _token_lock:
        _token_stats["prompt_tokens"] += usage.get("prompt_tokens", 0)
        _token_stats["completion_tokens"] += usage.get("completion_tokens", 0)
        _token_stats["total_tokens"] += usage.get("total_tokens", 0)
        _token_stats["api_calls"] += 1
        if not _token_stats["model"] and model_name:
            _token_stats["model"] = model_name


# ============================================================================
# V5: LLMProvider 抽象层（策略模式——运行时切换 LLM 提供商）
# ============================================================================

class LLMProvider(ABC):
    """
    LLM 提供商抽象基类。

    设计模式: 策略模式 (Strategy Pattern)
      - 所有提供商实现相同接口 (chat / chat_structured)
      - 底层 HTTP 请求逻辑 (重试、指数退避) 共用 _request()
      - 子类只需提供 api_key / api_base / model 三个参数

    V6 新增: 中转站 URL 兼容
      - 如果 api_base 以 /v1 结尾 → chat_path = /chat/completions
      - 否则 → chat_path = /v1/chat/completions
      - 避免中转站的 URL 被拼接成 /v1/v1/chat/completions
    """

    def __init__(self, api_key: str, api_base: str, model: str, name: str):
        self.api_key = api_key                                                   # API 密钥
        self.api_base = api_base.rstrip("/")                                     # API 基础 URL（去掉末尾 /）
        self.model = model                                                       # 模型名
        self._name = name                                                        # 提供商名（deepseek / zhipu）
        # V6: 自动检测中转站 URL —— 避免 /v1/v1 重复拼接
        if self.api_base.endswith("/v1"):
            self._chat_path = "/chat/completions"                                # api_base 已有 /v1
        else:
            self._chat_path = "/v1/chat/completions"                             # api_base 需要追加 /v1

    @property
    def provider_name(self) -> str:
        return self._name

    @property
    def model_name(self) -> str:
        return self.model

    # ── 底层 HTTP 请求（自动重试 + 指数退避）──

    def _request(self, payload: dict, timeout: int = 120) -> dict:
        """
        发送 POST 请求到 LLM API，带自动重试（最多 3 次）+ 指数退避。

        重试策略:
          第 1 次失败 → 等 2s  → 重试
          第 2 次失败 → 等 4s  → 重试
          第 3 次失败 → 抛 RuntimeError

        指数退避避免了"瞬间重试 → 全部失败"的情况，给网络恢复留时间。
        """
        last_error = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = requests.post(
                    f"{self.api_base}{self._chat_path}",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",               # 标准 Bearer Token 鉴权
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=timeout,
                )
                if resp.ok:                                                      # 200 OK
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
                logger.warning("[%s] API 返回 %s (attempt %s): %s",
                             self._name, resp.status_code, attempt, detail)
                last_error = RuntimeError(f"API {resp.status_code}: {detail}")

            except requests.RequestException as e:                               # 网络超时/连接失败等
                logger.warning("[%s] API 网络错误 (attempt %s): %s", self._name, attempt, e)
                last_error = e

            if attempt < _MAX_RETRIES:                                           # 还有重试次数
                delay = _BASE_DELAY * (2 ** (attempt - 1))                       # 2s → 4s → 8s
                logger.info("[%s] 重试 %s/%s, 等待 %.1fs",
                          self._name, attempt + 1, _MAX_RETRIES, delay)
                time.sleep(delay)

        raise RuntimeError(f"[{self._name}] API 调用失败 ({_MAX_RETRIES} 次重试后): {last_error}")

    # ── 公共接口 ──

    def chat(self, messages: list[dict], *, temperature: float = 0.3,
             max_tokens: int = 8192) -> str:
        """发送对话请求，返回纯文本。所有 Agent 的 LLM 调用最终都走这个方法。"""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        body = self._request(payload)
        return body["choices"][0]["message"]["content"]                          # 提取 assistant 返回的文本


    def chat_structured(self, messages: list[dict], response_model: Type[T], *,
                        temperature: float = 0.3, max_tokens: int = 8192) -> T:
        """发送对话请求，返回 Pydantic 结构化对象。用于需要严格 JSON Schema 的场景。"""
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
            "response_format": {"type": "json_object"},                          # 强制 JSON 模式
        }
        body = self._request(payload)
        raw = body["choices"][0]["message"]["content"].strip()

        # 清洗 markdown 包裹（LLM 仍然可能在 JSON 外包裹 ```json ... ```）
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        return response_model.model_validate_json(raw)


# ============================================================================
# V5: 具体提供商实现（继承 LLMProvider，提供默认配置）
# ============================================================================

class DeepSeekProvider(LLMProvider):
    """DeepSeek API 提供商。环境变量: DEEPSEEK_API_KEY, DEEPSEEK_API_BASE, DEEPSEEK_MODEL"""
    def __init__(self):
        super().__init__(
            api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
            api_base=os.environ.get("DEEPSEEK_API_BASE", "https://api.deepseek.com"),
            model=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro"),
            name="deepseek",
        )


class ZhipuProvider(LLMProvider):
    """智谱 AI（GLM）提供商。支持官方 API 和中转站。环境变量: ZHIPU_API_KEY, ZHIPU_API_BASE, ZHIPU_MODEL"""
    def __init__(self):
        super().__init__(
            api_key=os.environ.get("ZHIPU_API_KEY", ""),
            api_base=os.environ.get("ZHIPU_API_BASE", "https://open.bigmodel.cn/api/paas/v4"),
            model=os.environ.get("ZHIPU_MODEL", "glm-4"),
            name="zhipu",
        )


# ============================================================================
# V5: 模块级单例管理（全局唯一的 provider 实例）
# ============================================================================
_provider: LLMProvider | None = None


def _init_provider() -> LLMProvider:
    """初始化 LLM 提供商（模块加载时自动调用）。根据 LLM_PROVIDER 环境变量选择。"""
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
    """运行时切换 LLM 提供商。name: "deepseek" 或 "zhipu"。"""
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
    """返回所有可用的提供商及其模型名称。用于 Streamlit UI。"""
    providers = {"deepseek": os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")}
    if os.environ.get("ZHIPU_API_KEY"):
        providers["zhipu"] = os.environ.get("ZHIPU_MODEL", "glm-4")
    return providers


# ============================================================================
# V6: Per-agent provider — 不同 Agent 可配不同模型
# ============================================================================
# 这是 V6 应对 Risk 1（同模型审查自己）的关键机制。
# 审查节点用智谱（独立审查），生成节点用 DeepSeek（价格低+速度快）。
#
# 配置方式（优先级从高到低）:
#   1. 代码: set_agent_provider("review", "zhipu")
#   2. 环境变量: AGENT_REVIEW_PROVIDER=zhipu
#   3. 全局: LLM_PROVIDER=deepseek
#
# 降级机制: per-agent provider 不可用时（key 无效/网络不通）自动回退到全局 provider。
#   不会因为智谱挂了就导致整个流水线中断。

_agent_providers: dict[str, str] = {}             # agent_name → provider_name
_agent_provider_cache: dict[str, LLMProvider] = {}  # provider_name → 缓存的 provider 实例


def set_agent_provider(agent: str, provider_name: str):
    """配置某个 Agent 使用的 LLM 提供商。例: set_agent_provider("review", "zhipu")"""
    _agent_providers[agent.lower()] = provider_name.lower()
    logger.info("Agent '%s' LLM 提供商 → %s", agent, provider_name)


def _get_provider_for(agent: str | None) -> LLMProvider:
    """
    解析 Agent 应使用的 LLMProvider 实例。

    优先级: 代码 set_agent_provider() > 环境变量 AGENT_xxx_PROVIDER > 全局 _provider
    降级: per-agent provider 可用性检查 → key 为空则回退
    """
    if agent:
        name = agent.lower()
        target = None
        # 1. 代码配置（set_agent_provider 设置的）
        if name in _agent_providers:
            target = _agent_providers[name]
        # 2. 环境变量（如 AGENT_REVIEW_PROVIDER=zhipu）
        elif os.environ.get(f"AGENT_{name.upper()}_PROVIDER"):
            target = os.environ[f"AGENT_{name.upper()}_PROVIDER"].lower()

        if target and target != _provider.provider_name:
            try:
                if target not in _agent_provider_cache:
                    _agent_provider_cache[target] = _create_provider_by_name(target)
                    p = _agent_provider_cache[target]
                    if not p.api_key:                                          # key 为空 → 不可用
                        logger.warning("Agent '%s' 提供商 '%s' API key 为空，回退到 %s",
                                     agent, target, _provider.provider_name)
                        return _provider
                return _agent_provider_cache[target]
            except Exception as e:
                logger.warning("Agent '%s' 提供商 '%s' 初始化失败 (%s)，回退到 %s",
                             agent, target, e, _provider.provider_name)
    # 3. 全局默认
    return _provider


def _create_provider_by_name(name: str) -> LLMProvider:
    """按名称创建 LLMProvider 实例。"""
    if name == "deepseek":
        return DeepSeekProvider()
    elif name in ("zhipu", "glm"):
        return ZhipuProvider()
    else:
        logger.warning("未知提供商 '%s'，回退到 deepseek", name)
        return DeepSeekProvider()


def chat_with(agent: str | None, messages: list[dict], *, temperature: float = 0.3,
              max_tokens: int = 8192) -> str:
    """
    V6: 发送对话请求，可选指定 Agent（使用其配置的 LLM 提供商）。

    与 chat() 的区别:
      chat()        → 始终使用全局 provider
      chat_with()   → 先尝试 agent 的定制 provider，失败则回退到全局 provider

    回退机制: 先尝试 per-agent provider → 捕获异常 → 自动回退到全局 provider。
    这样智谱挂了也不会中断流水线，只是审查降级回 DeepSeek。

    Args:
        agent: Agent 名称（如 "review", "node2"），None = 使用全局默认
        messages: 消息列表
        temperature: 温度参数
        max_tokens: 最大输出 token 数
    """
    p = _get_provider_for(agent)
    # 如果 per-agent provider 与全局不同 → 先尝试，失败则回退
    if p is not _provider:
        try:
            return p.chat(messages, temperature=temperature, max_tokens=max_tokens)
        except Exception as e:
            logger.warning("Agent '%s' 提供商 '%s' 调用失败 (%s)，回退到 %s",
                         agent, p.provider_name, str(e)[:100], _provider.provider_name)
    return _provider.chat(messages, temperature=temperature, max_tokens=max_tokens)


# ── 模块加载时自动初始化 ──
_init_provider()


# ============================================================================
# 向后兼容的模块级函数（委托给当前 _provider，保证 V4/V5 代码不需要改动）
# ============================================================================

def chat(messages: list[dict], *, temperature: float = 0.3, max_tokens: int = 8192) -> str:
    """发送对话请求（使用全局默认 provider）"""
    return _provider.chat(messages, temperature=temperature, max_tokens=max_tokens)


def chat_structured(messages: list[dict], response_model: Type[T], *,
                    temperature: float = 0.3, max_tokens: int = 8192) -> T:
    """发送对话请求，返回 Pydantic 结构化对象（使用全局默认 provider）"""
    return _provider.chat_structured(messages, response_model, temperature=temperature, max_tokens=max_tokens)


def user_msg(content: str) -> dict:
    """构造一条 user 角色消息"""
    return {"role": "user", "content": content}


def assistant_msg(content: str) -> dict:
    """构造一条 assistant 角色消息"""
    return {"role": "assistant", "content": content}


def system_msg(content: str) -> dict:
    """构造一条 system 角色消息"""
    return {"role": "system", "content": content}
