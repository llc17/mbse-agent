# -*- coding: utf-8 -*-
"""
=============================================================================
utils.py — 共享工具函数
=============================================================================

本文件提取出各节点共用的"琐碎但不重复写"的工具函数，避免代码重复。

包含：
  1. load_prompt()            — 加载 prompt 模板文件
  2. clean_code_block()       — 去掉 LLM 返回的 ```lang ... ``` 包裹
  3. extract_json()           — 从 LLM 返回中提取纯 JSON
  4. extract_model_name()     — 从 Modelica 代码提取模型名
  5. format_history()         — 格式化对话历史为可读文本
  6. make_run_dir()           — 创建时间戳输出目录
  7. check_prerequisites()    — 检查 Python 包 / OpenModelica / API Key
  8. get_stop_time_for_domain() — V3: 根据物理域自动选 stopTime
=============================================================================
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from datetime import datetime


# ==========================================================================
# 1. Prompt 模板加载
# ==========================================================================

def load_prompt(name: str, prompts_dir: str | Path | None = None) -> str:
    """
    从 prompts/ 目录加载 .txt prompt 模板文件。

    为什么用文件而不是直接嵌在 Python 里：
      - prompt 很长（几百到几千字），嵌在代码里难以阅读和修改
      - 单独文件可以用任何文本编辑器调 prompt，不用改 Python 代码
      - 模板含占位符 {component_type} 等，运行时用 .replace() 填充

    Args:
        name: 文件名，如 "node2_sysml.txt"
        prompts_dir: 自定义目录（None=自动找项目下的 prompts/）

    Returns:
        prompt 模板的完整文本
    """
    if prompts_dir is None:
        # __file__ = utils.py 的绝对路径
        # .parent = src/ 目录
        # .parent.parent = week5/ 目录
        # / prompts = week5/prompts/
        prompts_dir = Path(__file__).parent.parent / "prompts"
    path = Path(prompts_dir) / name
    if not path.exists():
        raise FileNotFoundError(f"Prompt 文件不存在: {path}")
    return path.read_text(encoding="utf-8")


# ==========================================================================
# 2. LLM 输出清洗
# ==========================================================================

def clean_code_block(text: str, lang: str = "") -> str:
    """
    去掉 LLM 返回的 markdown 代码块包裹。

    LLM 常返回：
        ```sysml
        package X { ... }
        ```

    我们需要的是纯代码内容，所以要剥掉第一行 ```sysml 和最后一行 ```。

    Args:
        text: LLM 原始返回文本
        lang: 代码语言（仅用于文档，不影响逻辑）

    Returns:
        纯代码文本
    """
    text = text.strip()
    if text.startswith("```"):
        # 按换行符拆分
        lines = text.split("\n")
        # 去掉第一行（```sysml 或 ```）
        lines = lines[1:]
        # 如果最后一行是 ```，也去掉
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text


def extract_json(text: str) -> str:
    """
    从 LLM 返回中提取纯 JSON 文本，去掉 markdown 包裹。

    与 clean_code_block 逻辑相同，但语义上表明"返回的是 JSON"。
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]       # 去掉 ```json
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]       # 去掉 ```
        text = "\n".join(lines)
    return text


# ==========================================================================
# 3. Modelica 相关
# ==========================================================================

def extract_model_name(code: str) -> str | None:
    """
    从 Modelica 代码中提取模型类名。

    例: "model RCLowPassFilter ... end RCLowPassFilter;"
         → 返回 "RCLowPassFilter"

    用途: OMPython 的 ModelicaSystem(some_path, model_name) 需要模型名参数。
    """
    m = re.search(r"model\s+(\w+)", code)  # 正则: model 后跟空格再跟一个单词
    return m.group(1) if m else None        # group(1) = 第一个捕获组 = 模型名


# ==========================================================================
# 4. 对话历史格式化
# ==========================================================================

def format_history(history: list[dict]) -> str:
    """
    把对话历史（list of {"role":..., "content":...}）转成可读文本。

    用于 prompt 中的 "{dialogue_history}" 占位符。
    每条消息截断到 300 字符（防止 prompt 超长）。
    """
    lines = []
    for msg in history:
        role = "用户" if msg["role"] == "user" else "分析师"
        lines.append(f"{role}: {msg['content'][:300]}")
    return "\n".join(lines)


# ==========================================================================
# 5. 输出目录管理
# ==========================================================================

def make_run_dir(base: str | Path = "outputs") -> Path:
    """
    创建一次运行的时间戳输出目录。

    目录结构:
      outputs/run_2026-07-07_143000/
        ├── sysml/      ← 存 model.sysml
        ├── modelica/   ← 存 model.mo
        └── results/    ← 存 simulation.csv, simulation.png, summary.md
    """
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run_dir = Path(base) / f"run_{timestamp}"
    for sub in ["sysml", "modelica", "results"]:
        (run_dir / sub).mkdir(parents=True, exist_ok=True)  # parents=True: 自动创建父目录
    return run_dir


# ==========================================================================
# 6. 环境检查
# ==========================================================================

def check_prerequisites() -> list[str]:
    """
    检查运行环境是否就绪，返回缺失项列表。

    检查项:
      1. Python 包: requests, pydantic, matplotlib
      2. LangGraph（状态图引擎）
      3. OpenModelica（编译+仿真引擎）
      4. 环境变量 DEEPSEEK_API_KEY（LLM API 密钥）

    返回空列表 = 一切就绪。
    """
    missing = []

    # --- Python 包: 尝试 import 每个必需的包 ---
    for mod in ["requests", "pydantic", "matplotlib"]:
        try:
            __import__(mod)     # __import__() = Python 内置的动态 import 函数
        except ImportError:
            missing.append(f"Python 包: {mod}")

    # --- LangGraph ---
    try:
        import langgraph
    except ImportError:
        missing.append("Python 包: langgraph")

    # --- OpenModelica: 两种检测方式 ---
    omc_ok = False
    try:
        from OMPython import ModelicaSystem  # 方式1: OMPython 包可用
        omc_ok = True
    except ImportError:
        pass
    if not omc_ok:
        try:
            # 方式2: omc 命令行可用
            r = subprocess.run(["omc", "--version"], capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                omc_ok = True
        except (FileNotFoundError, Exception):
            pass
    if not omc_ok:
        missing.append("OpenModelica (omc 或 OMPython)")

    # --- API 密钥: 从环境变量读取 ---
    if not os.environ.get("DEEPSEEK_API_KEY"):
        missing.append("环境变量: DEEPSEEK_API_KEY")

    return missing


# ==========================================================================
# 7. V3 新增：仿真参数自动适配
# ==========================================================================

def get_stop_time_for_domain(component_type: str) -> float:
    """
    根据物理域返回合适的 stopTime（仿真结束时间）。

    为什么需要这个函数:
      - 电气仿真（RC/RLC/运放）在微秒~毫秒级完成 → stopTime=0.01s
      - 热仿真（房间热传导）需要几分钟到几小时 → stopTime=1000s
      - 如果用 0.01s 跑热仿真，曲线还没动就结束了
      - 如果用 1000s 跑电气仿真，浪费计算资源

    V4 扩展: 加入 RLC 和运放关键词。
    """
    ct = component_type.lower()  # 统一转小写，方便匹配
    # 热域 → 1000s（热量传导慢）
    if any(kw in ct for kw in ["热", "thermal", "heat", "温度"]):
        return 1000.0
    # 电气域 → 0.01s（电压电流变化快）
    if any(kw in ct for kw in ["电气", "electrical", "rc", "rlc", "运放", "opamp", "op-amp", "电路"]):
        return 0.01
    # 默认按电气处理
    return 0.01
