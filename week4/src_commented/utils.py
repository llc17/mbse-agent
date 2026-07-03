# -*- coding: utf-8 -*-
"""
=============================================================================
utils.py — 共享工具函数（V3 版）
=============================================================================

各节点共用的辅助函数，避免代码重复。
原则：每个函数只做一件事，输入输出明确，有 docstring。

V3 新增：
  - get_stop_time_for_domain(): 根据物理域自动选仿真时长
    （解决 V2 中电气仿真 0.01s 和热仿真 1000s 硬编码的问题）
"""

# ====================================================================
# 导入
# ====================================================================
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from datetime import datetime


# ====================================================================
# Prompt 模板加载
# ====================================================================

def load_prompt(name: str, prompts_dir: str | Path | None = None) -> str:
    """
    从 prompts/ 目录加载 prompt 模板文件。

    为什么要单独存文件而不是写在 Python 里？
      1. 方便非程序员（导师/合作者）直接修改 prompt
      2. 版本管理清晰——git diff 能看到 prompt 的具体改动
      3. 实验时自动复制到 run_dir，保证可复现

    参数:
        name: 模板文件名。例: "node2_sysml.txt"
        prompts_dir: 模板目录路径。默认 week4/prompts/

    返回:
        文件内容字符串（含 {placeholder} 占位符）

    用法:
        prompt = load_prompt("node2_sysml.txt")
        prompt = prompt.replace("{component_type}", "RC低通滤波器")
    """
    if prompts_dir is None:
        prompts_dir = Path(__file__).parent.parent / "prompts"
    path = Path(prompts_dir) / name
    if not path.exists():
        raise FileNotFoundError(f"Prompt 文件不存在: {path}")
    return path.read_text(encoding="utf-8")


# ====================================================================
# 字符串清洗
# ====================================================================

def clean_code_block(text: str, lang: str = "") -> str:
    """
    去掉 LLM 返回的 ```lang ... ``` markdown 包裹。

    问题背景：LLM 经常在生成代码时包裹 markdown 代码块标记：
      ```modelica
      model Foo ...
      ```
    但我们只需要纯代码文本。

    参数:
        text: LLM 原始返回
        lang: （可选）标注原语言，仅用于日志

    返回:
        去掉包裹后的纯代码
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]                                          # 去掉第一行 ```lang
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]                                     # 去掉最后一行 ```
        text = "\n".join(lines)
    return text


def extract_json(text: str) -> str:
    """
    从 LLM 返回中提取 JSON 文本（去掉 markdown 包裹）。

    与 clean_code_block 逻辑相同但语义独立——以后可能加 JSON 特有的清洗。
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text


# ====================================================================
# Modelica 辅助
# ====================================================================

def extract_model_name(code: str) -> str | None:
    """
    从 Modelica 代码中提取模型类名。
    例: "model RCLowPassFilter ..." → "RCLowPassFilter"

    用于编译和仿真时指定正确的模型名。
    """
    m = re.search(r"model\s+(\w+)", code)
    return m.group(1) if m else None


# ====================================================================
# 对话历史格式化
# ====================================================================

def format_history(history: list[dict]) -> str:
    """
    把对话历史格式化为可读文本，用于注入 prompt。

    例:
        [{"role":"user","content":"我要一个RC滤波器"}, ...]
      →
        "用户: 我要一个RC滤波器\n分析师: 请问截止频率是多少？..."

    每段截断到 300 字符，防止 prompt 过长。
    """
    lines = []
    for msg in history:
        role = "用户" if msg["role"] == "user" else "分析师"
        lines.append(f"{role}: {msg['content'][:300]}")
    return "\n".join(lines)


# ====================================================================
# 输出目录管理
# ====================================================================

def make_run_dir(base: str | Path = "outputs") -> Path:
    """
    创建带时间戳的输出目录，预建 sysml/modelica/results 子目录。

    示例: outputs/run_2026-06-25_103000/
            ├── sysml/
            ├── modelica/
            └── results/

    返回: Path 对象指向 run 目录
    """
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run_dir = Path(base) / f"run_{timestamp}"
    for sub in ["sysml", "modelica", "results"]:
        (run_dir / sub).mkdir(parents=True, exist_ok=True)
    return run_dir


# ====================================================================
# 环境检查
# ====================================================================

def check_prerequisites() -> list[str]:
    """
    检查运行环境是否就绪。返回缺失项列表（空=一切正常）。

    检查项：
      1. Python 依赖（requests / pydantic / matplotlib / langgraph）
      2. OpenModelica（OMPython 或 omc CLI）
      3. DEEPSEEK_API_KEY 环境变量
    """
    missing = []

    # ---- Python 依赖 ----
    for mod in ["requests", "pydantic", "matplotlib"]:
        try:
            __import__(mod)
        except ImportError:
            missing.append(f"Python 包: {mod}")

    try:
        import langgraph
    except ImportError:
        missing.append("Python 包: langgraph")

    # ---- OpenModelica ----
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

    # ---- API Key ----
    if not os.environ.get("DEEPSEEK_API_KEY"):
        missing.append("环境变量: DEEPSEEK_API_KEY")

    return missing


# ====================================================================
# ── V3 新增 ──
# 仿真参数自动适配
# ====================================================================

def get_stop_time_for_domain(component_type: str) -> float:
    """
    根据物理域返回合理的仿真 stopTime。

    背景：V2 中 stopTime 硬编码为 0.01s。
    这对 RC 电路合适（τ=RC≈0.16ms，0.01s ≈ 62τ），
    但对热系统完全不够（热时间常数可能数百秒，0.01s 看不到任何变化）。

    规则：
      - 热/thermal/heat → 1000s（热传导过程很慢）
      - 电气/electrical/rc/rlc/运放 → 0.01s（电路瞬态很快）
      - 其他 → 默认 0.01s

    参数:
        component_type: 来自 StructuredRequirement.component_type
                       例: "RC低通滤波器", "单房间热传导"

    返回:
        stopTime 秒数（float）
    """
    ct = component_type.lower()
    if any(kw in ct for kw in ["热", "thermal", "heat", "温度"]):
        return 1000.0
    if any(kw in ct for kw in ["电气", "electrical", "rc", "rlc", "运放", "opamp", "op-amp", "电路"]):
        return 0.01
    return 0.01  # 默认电气
