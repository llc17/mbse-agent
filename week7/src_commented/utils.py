"""
=============================================================================
utils.py — 共享工具函数集
=============================================================================
用途:
  - 加载 prompt 模板文件（load_prompt）
  - 清理 LLM 返回的 markdown 包裹（clean_code_block / extract_json）
  - 创建输出目录（make_run_dir）
  - 环境检查（check_prerequisites）
  - 仿真参数自动适配（get_stop_time_for_domain）

所有节点都 import 这个模块来避免代码重复。
=============================================================================
"""

# ---------------------------------------------------------------------------
# 第 1 层: 导入依赖
# ---------------------------------------------------------------------------
import json                               # JSON 解析
import os                                 # 环境变量读取
import re                                 # 正则匹配（提取 model 名）
import subprocess                         # 检查 OpenModelica 是否安装
import sys                                # (保留，未直接使用)
from pathlib import Path                  # 跨平台路径处理（/ vs \）
from datetime import datetime             # 生成时间戳目录名


# ============================================================================
# 第 2 层: Prompt 模板加载
# ============================================================================

def load_prompt(name: str, prompts_dir: str | Path | None = None) -> str:
    """
    加载 prompt 模板文件。

    模板文件在 week7/prompts/ 目录下，用 .txt 格式保存。
    加载后用 Python 的 .replace() 填充占位符（如 {component_type} → "RC滤波器"）。

    Args:
        name: 模板文件名，如 "node2_sysml.txt"
        prompts_dir: 模板目录路径。None = 自动定位到 week7/prompts/

    Returns:
        模板文件的完整文本内容（UTF-8 编码）

    Raises:
        FileNotFoundError: 模板文件不存在时抛出
    """
    # ── 自动定位 prompts 目录 ──
    if prompts_dir is None:
        # 当前文件 src_commented/utils.py → 上级目录 → prompts/
        prompts_dir = Path(__file__).parent.parent / "prompts"

    # ── 读取文件 ──
    path = Path(prompts_dir) / name
    if not path.exists():
        raise FileNotFoundError(f"Prompt 文件不存在: {path}")
    return path.read_text(encoding="utf-8")


# ============================================================================
# 第 3 层: LLM 返回文本清理
# ============================================================================

def clean_code_block(text: str, lang: str = "") -> str:
    """
    去掉 LLM 返回的 markdown 代码块包裹。

    LLM 经常返回：
      ```modelica
      model X ... end X;
      ```

    本函数只提取代码内容，丢掉首尾的 ``` 标记行。

    Args:
        text: LLM 返回的原始文本
        lang: 语言标签（未使用，保留接口兼容性）

    Returns:
        剥离包裹后的纯代码文本
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # 去掉第一行（```lang 或 ```）
        lines = lines[1:]
        # 如果最后一行是 ```，也去掉
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text


def extract_json(text: str) -> str:
    """
    从 LLM 返回中提取 JSON 文本（去掉 markdown 包裹）。

    与 clean_code_block 类似，但专门用于 JSON 输出。

    Args:
        text: LLM 返回的原始文本

    Returns:
        剥离 markdown 包裹后的 JSON 字符串
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


def extract_model_name(code: str) -> str | None:
    """
    从 Modelica 代码中提取模型类名。

    正则匹配 "model 类名" 模式，返回类名。

    Args:
        code: Modelica 代码文本

    Returns:
        模型类名（如 "my_rc_filter"），未找到返回 None

    例: "model my_rc_filter ... end my_rc_filter;" → "my_rc_filter"
    """
    m = re.search(r"model\s+(\w+)", code)
    return m.group(1) if m else None


# ============================================================================
# 第 4 层: 对话历史 & 输出目录
# ============================================================================

def format_history(history: list[dict]) -> str:
    """
    把对话历史格式化为可读文本。

    用于 interactive 模式的 prompt 注入——让 LLM 看到之前的问答。

    Args:
        history: [{"role": "user", "content": "..."}, ...] 格式的消息列表

    Returns:
        格式化的对话文本，每条消息截断到 300 字符
    """
    lines = []
    for msg in history:
        # 按角色显示标签
        role = "用户" if msg["role"] == "user" else "分析师"
        lines.append(f"{role}: {msg['content'][:300]}")
    return "\n".join(lines)


def make_run_dir(base: str | Path = "outputs") -> Path:
    """
    创建时间戳输出目录。

    每次运行创建独立目录，避免覆盖之前的结果。

    目录结构:
      outputs/
        run_2026-07-29_120230/   ← 时间戳目录
          sysml/                   ← node2 产出
          modelica/                ← node3 产出
          results/                 ← 仿真 CSV + PNG + summary.md

    Args:
        base: 基础输出目录

    Returns:
        创建的运行目录 Path 对象
    """
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run_dir = Path(base) / f"run_{timestamp}"
    for sub in ["sysml", "modelica", "results"]:
        (run_dir / sub).mkdir(parents=True, exist_ok=True)
    return run_dir


# ============================================================================
# 第 5 层: 环境依赖检查
# ============================================================================

def check_prerequisites() -> list[str]:
    """
    检查运行环境是否完整。返回缺失项列表。

    检查项:
      1. Python 包: requests, pydantic, matplotlib, langgraph
      2. OpenModelica: OMPython 或 omc 命令行
      3. API Key: DEEPSEEK_API_KEY 环境变量

    Returns:
        缺失项列表（[] = 环境完整，可运行）
    """
    missing = []

    # ── Python 包检查 ──
    for mod in ["requests", "pydantic", "matplotlib"]:
        try:
            __import__(mod)  # 尝试导入，失败 = 未安装
        except ImportError:
            missing.append(f"Python 包: {mod}")

    # ── LangGraph 检查 ──
    try:
        import langgraph
    except ImportError:
        missing.append("Python 包: langgraph")

    # ── OpenModelica 检查（两种方式）──
    omc_ok = False
    try:
        from OMPython import ModelicaSystem  # 方式1: Python 包
        omc_ok = True
    except ImportError:
        pass
    if not omc_ok:
        try:
            r = subprocess.run(["omc", "--version"],          # 方式2: 命令行
                             capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                omc_ok = True
        except (FileNotFoundError, Exception):
            pass
    if not omc_ok:
        missing.append("OpenModelica (omc 或 OMPython)")

    # ── API Key 检查 ──
    if not os.environ.get("DEEPSEEK_API_KEY"):
        missing.append("环境变量: DEEPSEEK_API_KEY")

    return missing


# ============================================================================
# 第 6 层: V3 仿真参数自动适配
# ============================================================================

def get_stop_time_for_domain(component_type: str) -> float:
    """
    根据物理域返回合适的仿真停止时间。

    不同物理域的时间尺度差异巨大:
      - 电气（RC/RLC/运放）: 微秒~毫秒级 → stopTime=0.01s
      - 热（热传导）: 分钟~小时级 → stopTime=1000s

    用错 stopTime 会导致仿真数据不够或太密。

    Args:
        component_type: 系统类型（如 "RC低通滤波器"、"双房间热传导"）

    Returns:
        stopTime 值（秒）
    """
    ct = component_type.lower()
    # 热系统 → 1000 秒（让温度有足够时间趋于稳态）
    if any(kw in ct for kw in ["热", "thermal", "heat", "温度"]):
        return 1000.0
    # 电气系统 → 0.01 秒（几倍时间常数足够看到阶跃响应）
    if any(kw in ct for kw in ["电气", "electrical", "rc", "rlc", "运放", "opamp", "op-amp", "电路"]):
        return 0.01
    return 0.01  # 默认电气
