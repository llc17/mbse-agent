"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  DeepSeek API 统一封装 — 所有节点调 LLM 的唯一入口                           ║
║                                                                            ║
║  调用链：main → node1/2/3/4 → llm_client.chat() → DeepSeek 服务器            ║
║                                                                            ║
║  📋 = DeepSeek 官方规定  │  🫵 = 我设计的                                   ║
║  📋 POST /v1/chat/completions   — API 路径                                 ║
║  📋 headers: Authorization, Content-Type — HTTP 头                         ║
║  📋 messages: [{role, content}] — OpenAI 兼容格式                           ║
║  🫵 MODEL = "deepseek-v4-pro"   — 去 platform.deepseek.com 查              ║
║  🫵 temperature / max_tokens    — 我根据场景设的值                           ║
║  🫵 错误处理                     — 我自己加的保护                             ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import json             # 标准库：JSON 处理
import os               # 标准库：读环境变量 os.environ.get()
import sys              # 标准库：系统功能
from typing import Optional, Type, TypeVar  # 类型注解

import requests         # 第三方库：发 HTTP 请求 pip install requests
from pydantic import BaseModel  # 第三方库：Pydantic 基类


# ============================================================
#  配置 — 换模型/换 API 提供商只改这三行
# ============================================================
API_KEY = os.environ.get(              # 从系统环境变量读取
    "DEEPSEEK_API_KEY",                # 变量名
    ""  # 默认值（没设环境变量时用这个）
)                                      # 面试重点：密钥不放代码里

API_BASE = os.environ.get(             # API 网址
    "DEEPSEEK_API_BASE",
    "https://api.deepseek.com"        # 📋 DeepSeek 官方定死
)

MODEL = os.environ.get(                # 模型名
    "DEEPSEEK_MODEL",
    "deepseek-v4-pro"                  # 🫵 去 docs 查的最新模型
)

if not API_KEY:                        # 万一没 Key，提前警告
    print("[llm_client] 警告: 未设置 DEEPSEEK_API_KEY 环境变量")

T = TypeVar("T", bound=BaseModel)      # 泛型约束：T 必须是 BaseModel 子类
                                       # → chat_structured 用，当前项目未用


# ============================================================
#  chat() — 核心函数，所有节点都通过它调 LLM
# ============================================================
def chat(
    messages: list[dict],              # 消息列表：[{"role":"user","content":"..."}, ...]
    *,                                 # * 之后参数必须用关键字传递，如 temperature=0.3
    temperature: float = 0.3,          # 0=确定 2=随机。代码用 0.1~0.2，反问用 0.5
    max_tokens: int = 16384,           # 最多输出 token 数。DeepSeek V4 上限 384K
) -> str:                              # 返回值：LLM 的纯文本回复
    """发送对话，返回文本。"""

    resp = requests.post(               # 📋 HTTP POST 请求
        f"{API_BASE}/v1/chat/completions",  # 📋 API 完整路径
        headers={                       # 📋 HTTP 头
            "Authorization": f"Bearer {API_KEY}",  # 📋 Bearer 认证
            "Content-Type": "application/json",     # 📋 声明 JSON 格式
        },
        json={                          # 📋 请求体，以下参数名全是官方定死
            "model": MODEL,             # 🫵 我选的模型
            "messages": messages,       # 📋 对话内容
            "temperature": temperature, # 🫵 我设的温度
            "max_tokens": max_tokens,   # 🫵 我设的上限
        },
        timeout=120,                    # 🫵 超时保护，120秒
    )

    if not resp.ok:                     # 🫵 HTTP 状态不是 2xx
        detail = resp.text[:500]        # 截前 500 字错误详情
        raise RuntimeError(             # 带上详细信息抛异常
            f"API 调用失败 ({resp.status_code}): {detail}\n"
            f"当前模型: {MODEL}（有效值: deepseek-chat / deepseek-reasoner）"
        )

    return resp.json()["choices"][0]["message"]["content"]
    #      └────┬────┘ └──┬──┘ └┬┘ └──┬───┘ └──┬──┘
    #     HTTP响应→JSON choices数组 [0] message content=LLM文本
    #     📋 这个返回结构是所有 OpenAI 兼容 API 的标准


# ============================================================
#  chat_structured() — 返回 Pydantic 对象（备用，当前未使用）
# ============================================================
def chat_structured(
    messages: list[dict],
    response_model: Type[T],           # Pydantic 类（不是实例），如 StructuredRequirement
    *,
    temperature: float = 0.3,
    max_tokens: int = 16384,
) -> T:                                # 返回推断的类型
    """JSON Mode 让 LLM 输出 JSON，再 parse 为 Pydantic。"""

    system_prompt = {                  # system 角色=给 LLM 的指令
        "role": "system",
        "content": (
            f"你必须始终输出合法的 JSON 对象。\n"
            f"JSON Schema:\n{json.dumps(response_model.model_json_schema(), ensure_ascii=False, indent=2)}\n\n"
            f"只输出 JSON，不要包含任何解释、markdown 代码块标记或其他文本。"
        ),
    }
    full_messages = [system_prompt] + list(messages)  # system 放最前面

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
            "response_format": {"type": "json_object"},  # 📋 强制 JSON 输出
        },
        timeout=120,
    )

    if not resp.ok:                    # 同 chat() 的错误处理
        detail = resp.text[:500]
        raise RuntimeError(
            f"API 调用失败 ({resp.status_code}): {detail}\n"
            f"当前模型: {MODEL}（有效值: deepseek-chat / deepseek-reasoner）"
        )

    raw = resp.json()["choices"][0]["message"]["content"]

    raw = raw.strip()                  # 去首尾空白
    if raw.startswith("```"):          # LLM 有时包在 ```json ... ``` 里
        lines = raw.split("\n")        # 按行切
        raw = "\n".join(               # 去首尾 ``` 行
            lines[1:-1] if lines[-1].strip() == "```"
            else lines[1:]
        )

    return response_model.model_validate_json(raw)  # JSON字符串 → Pydantic对象+校验


# ============================================================
#  消息构造函数 — 防止手写 {"role":"user","content":"..."} 打错
# ============================================================
def user_msg(content: str) -> dict:        # 构造用户消息
    return {"role": "user", "content": content}

def assistant_msg(content: str) -> dict:   # 构造助手消息
    return {"role": "assistant", "content": content}

def system_msg(content: str) -> dict:      # 构造系统消息
    return {"role": "system", "content": content}
