"""
共享工具函数。V2 从各节点文件中提取出来，避免重复。
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from datetime import datetime


def load_prompt(name: str, prompts_dir: str | Path | None = None) -> str:
    """加载 prompt 模板文件。"""
    if prompts_dir is None:
        prompts_dir = Path(__file__).parent.parent / "prompts"
    path = Path(prompts_dir) / name
    if not path.exists():
        raise FileNotFoundError(f"Prompt 文件不存在: {path}")
    return path.read_text(encoding="utf-8")


def clean_code_block(text: str, lang: str = "") -> str:
    """去掉 LLM 返回的 ```lang ... ``` 包裹。"""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]  # 去掉第一行 ```lang
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text


def extract_json(text: str) -> str:
    """从 LLM 返回中提取 JSON 文本。"""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text


def extract_model_name(code: str) -> str | None:
    """从 Modelica 代码中提取模型类名。"""
    m = re.search(r"model\s+(\w+)", code)
    return m.group(1) if m else None


def format_history(history: list[dict]) -> str:
    """把对话历史格式化为可读文本。"""
    lines = []
    for msg in history:
        role = "用户" if msg["role"] == "user" else "分析师"
        lines.append(f"{role}: {msg['content'][:300]}")
    return "\n".join(lines)


def make_run_dir(base: str | Path = "outputs") -> Path:
    """创建时间戳输出目录。"""
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run_dir = Path(base) / f"run_{timestamp}"
    for sub in ["sysml", "modelica", "results"]:
        (run_dir / sub).mkdir(parents=True, exist_ok=True)
    return run_dir


def check_prerequisites() -> list[str]:
    """检查运行环境。返回缺失项列表。"""
    missing = []

    # Python 依赖
    for mod in ["requests", "pydantic", "matplotlib"]:
        try:
            __import__(mod)
        except ImportError:
            missing.append(f"Python 包: {mod}")

    # LangGraph
    try:
        import langgraph
    except ImportError:
        missing.append("Python 包: langgraph")

    # OpenModelica
    omc_ok = False
    try:
        from OMPython import ModelicaSystem
        omc_ok = True
    except ImportError:
        pass
    if not omc_ok:
        try:
            r = subprocess.run(["omc", "--version"], capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                omc_ok = True
        except (FileNotFoundError, Exception):
            pass
    if not omc_ok:
        missing.append("OpenModelica (omc 或 OMPython)")

    # API Key
    if not os.environ.get("DEEPSEEK_API_KEY"):
        missing.append("环境变量: DEEPSEEK_API_KEY")

    return missing
