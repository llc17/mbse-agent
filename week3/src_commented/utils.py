# -*- coding: utf-8 -*-
"""
=============================================================================
utils.py — 共享工具函数库
=============================================================================

这个文件是 V2 新增的。V1 中每个节点文件都复制了一份相同的工具函数
（比如 _load_prompt、_clean_code_block），代码重复。
V2 把它们全部提取出来放在这里，所有节点共用一份。

工具函数列表：
  load_prompt()          — 加载 prompt 模板文件
  clean_code_block()     — 去掉 LLM 返回的 ```代码块标记```
  extract_json()         — 从 LLM 返回中提取 JSON 文本
  extract_model_name()   — 从 Modelica 代码中提取模型类名
  format_history()       — 把对话历史格式化为可读文本
  make_run_dir()         — 创建带时间戳的输出目录
  check_prerequisites()  — 检查依赖是否安装完整
"""

# ====================================================================
# 导入
# ====================================================================

import json                                              # 处理 JSON 数据
import os                                                # 读取环境变量、操作文件路径
import re                                                # 正则表达式（提取模型名）
import subprocess                                        # 调用外部命令（omc 编译器）
import sys                                               # 系统相关（退出程序等）
from pathlib import Path                                 # 跨平台的路径操作（比 os.path 更现代）
from datetime import datetime                            # 生成时间戳


# ====================================================================
# 1. load_prompt() — 加载 prompt 模板
# ====================================================================
def load_prompt(name: str, prompts_dir: str | Path | None = None) -> str:
    """
    读取 prompts/ 目录下的 .txt 提示词文件。

    参数:
      name:        文件名，例如 "node2_sysml.txt"
      prompts_dir: 自定义 prompt 目录路径。不传则自动找 ../prompts/

    返回:
      文件内容（字符串）

    示例:
      template = load_prompt("node2_sysml.txt")
    """
    # ---- 确定 prompts 目录 ----
    if prompts_dir is None:                              # 如果不传 prompts_dir
        prompts_dir = Path(__file__).parent.parent / "prompts"
        # Path(__file__) = 当前文件路径（.../src_commented/utils.py）
        # .parent = 上一级目录（.../src_commented/）
        # .parent.parent = 上上级目录（.../week3/）
        # / "prompts" = .../week3/prompts/

    path = Path(prompts_dir) / name                      # 拼接文件路径

    if not path.exists():                                # 如果文件不存在
        raise FileNotFoundError(f"Prompt 文件不存在: {path}")  # 直接抛异常（程序停止）

    return path.read_text(encoding="utf-8")              # 读取文件全部内容，以 UTF-8 编码返回


# ====================================================================
# 2. clean_code_block() — 清洗代码块标记
# ====================================================================
def clean_code_block(text: str, lang: str = "") -> str:
    """
    LLM 返回代码时经常用 markdown 代码块包裹：
      ```python
      代码内容
      ```
    这个函数剥掉外层 ``` 标记，只保留代码内容。

    参数:
      text: LLM 的原始返回
      lang: 语言名（没用，只是为了和 V1 接口兼容）

    返回:
      清洗后的纯代码
    """
    text = text.strip()                                  # 去掉首尾空白字符（空格、换行、tab）

    if text.startswith("```"):                           # 如果以三个反引号开头（代码块标记）
        lines = text.split("\n")                         # 按换行符拆成多行
        lines = lines[1:]                                # 去掉第一行（```python 或 ```sysml）
        if lines and lines[-1].strip() == "```":         # 如果最后一行是一个单独的 ```
            lines = lines[:-1]                           #   去掉最后一行
        text = "\n".join(lines)                          # 重新拼成字符串

    return text


# ====================================================================
# 3. extract_json() — 提取 JSON
# ====================================================================
def extract_json(text: str) -> str:
    """
    和 clean_code_block 逻辑一样，只是函数名不同。
    用于从 LLM 返回中提取 JSON 文本（去掉可能的 ```json ... ``` 标记）。

    为什么单独一个函数：
      语义更清晰 —— "提取 JSON" 比 "清洗代码" 更好理解。
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):                   # 去掉第一行（```json 或 ```）
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":         # 去掉最后一行（```）
            lines = lines[:-1]
        text = "\n".join(lines)
    return text


# ====================================================================
# 4. extract_model_name() — 提取 Modelica 模型名
# ====================================================================
def extract_model_name(code: str) -> str | None:
    """
    从 Modelica 代码中提取模型类名。

    例如:
      输入: "model RCLowPassFilter\n  ...\nend RCLowPassFilter;"
      输出: "RCLowPassFilter"

    原理:
      正则表达式 r"model\\s+(\\w+)" 匹配 "model 后面跟至少一个空白，然后捕获下一串字母数字"
      model\\s+ → 匹配 "model" 关键字和后面的空格
      (\w+)     → 捕获组：匹配一串字母/数字/下划线（即模型名）
    """
    m = re.search(r"model\s+(\w+)", code)                # 在代码中搜索 model 关键字后面的单词
    return m.group(1) if m else None                     # 如果找到了，返回 group(1)（捕获组内容）；否则返回 None


# ====================================================================
# 5. format_history() — 格式化对话历史
# ====================================================================
def format_history(history: list[dict]) -> str:
    """
    把 LLM 对话历史列表转成人类可读的文本。

    输入格式（OpenAI 对话格式）:
      [
        {"role": "user", "content": "做个低通滤波器"},
        {"role": "assistant", "content": "请确认截止频率..."}
      ]

    输出格式:
      用户: 做个低通滤波器
      分析师: 请确认截止频率...

    为什么只取前 300 字符：
      防止 conversation 太长导致 prompt 爆掉 token 限制。
    """
    lines = []                                           # 存放每行文本的列表
    for msg in history:                                  # 遍历每条消息
        role = "用户" if msg["role"] == "user" else "分析师"
        # 三元表达式：如果是 user → 显示"用户"，否则显示"分析师"
        lines.append(f"{role}: {msg['content'][:300]}")  # 拼接 "角色: 内容前300字"，加入列表
    return "\n".join(lines)                              # 用换行符连接所有行


# ====================================================================
# 6. make_run_dir() — 创建运行目录
# ====================================================================
def make_run_dir(base: str | Path = "outputs") -> Path:
    """
    创建带时间戳的输出目录。

    目录结构:
      outputs/
        run_2026-06-09_143000/
          ├── sysml/      ← 存放 .sysml 文件
          ├── modelica/   ← 存放 .mo 文件
          └── results/    ← 存放 CSV、PNG、JSON、summary.md

    返回:
      run_dir 的 Path 对象
    """
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    # strftime: 把时间格式化为字符串，例如 "2026-06-09_143000"

    run_dir = Path(base) / f"run_{timestamp}"            # Path / 字符串 = 拼接路径

    for sub in ["sysml", "modelica", "results"]:         # 创建 3 个子目录
        (run_dir / sub).mkdir(parents=True, exist_ok=True)
        # mkdir: 创建目录
        # parents=True: 如果父目录不存在，自动创建
        # exist_ok=True: 如果目录已存在，不报错

    return run_dir


# ====================================================================
# 7. check_prerequisites() — 环境检查
# ====================================================================
def check_prerequisites() -> list[str]:
    """
    在程序启动时检查运行环境。

    检查项：
      1. Python 包（requests, pydantic, matplotlib, langgraph）
      2. OpenModelica（先尝试 OMPython Python 包，再尝试命令行 omc）
      3. 环境变量 DEEPSEEK_API_KEY

    返回:
      缺失项列表。如果列表为空，说明环境没问题。
    """
    missing = []                                         # 存放缺失项的空列表

    # ---- 检查 Python 包 ----
    for mod in ["requests", "pydantic", "matplotlib"]:   # 逐个检查
        try:
            __import__(mod)                              # 尝试导入（__import__ 是 import 的底层函数）
        except ImportError:                              # 如果导入失败（包没装）
            missing.append(f"Python 包: {mod}")          #   记录缺失

    # ---- 检查 LangGraph ----
    try:
        import langgraph                                 # 尝试导入 langgraph
    except ImportError:
        missing.append("Python 包: langgraph")           # 没装就记录

    # ---- 检查 OpenModelica ----
    # 方案 A：Python API（OMPython 包）
    omc_ok = False
    try:
        from OMPython import ModelicaSystem               # 尝试导入 OMPython
        omc_ok = True                                    #   成功 → 标记 OK
    except ImportError:
        pass                                             #   失败 → 继续尝试方案 B

    # 方案 B：命令行（omc 可执行文件）
    if not omc_ok:
        try:
            r = subprocess.run(                          # 执行外部命令
                ["omc", "--version"],                    # 在终端里相当于 omc --version
                capture_output=True,                     # 捕获 stdout 和 stderr
                text=True,                               # 输出以文本返回（而不是 bytes）
                timeout=10,                              # 最多等 10 秒
            )
            if r.returncode == 0:                        # returncode=0 表示命令执行成功
                omc_ok = True
        except (FileNotFoundError, Exception):           # FileNotFoundError: omc 不在 PATH 里
            pass                                         # 其他异常也吞掉，最后统一报 missing

    if not omc_ok:
        missing.append("OpenModelica (omc 或 OMPython)") # 两个方案都失败 → 记录缺失

    # ---- 检查 API Key ----
    if not os.environ.get("DEEPSEEK_API_KEY"):           # os.environ.get("KEY") 读取环境变量
        missing.append("环境变量: DEEPSEEK_API_KEY")      # 没设置就记录

    return missing                                       # 返回缺失项列表（空 = 环境 OK）
