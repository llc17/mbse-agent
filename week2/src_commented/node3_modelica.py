# -*- coding: utf-8 -*-
"""
=============================================================================
node3_modelica.py — 节点3：Modelica 生成 + 编译 + 仿真 + 自修复
=============================================================================

从 StructuredRequirement + SysMLArtifact 出发：
  1. LLM 生成 .mo 代码
  2. OpenModelica 编译
  3. OpenModelica 仿真
  4. 编译/仿真失败 → 错误日志回喂 LLM → 重试（最多 2 次）
  5. 成功 → CSV + matplotlib PNG

V1 特点:
  - V1 用 while 循环做自修复（非图结构）
  - max_retries=2（V2 提到 5）
  - OMPython 和 subprocess 双通道
  - 节点函数不是 LangGraph 节点（V1 没有 LangGraph）
"""

# ====================================================================
# 导入
# ====================================================================

import csv                                               # 读取仿真输出的 CSV 文件
import os                                                # 路径操作
import re                                                # 正则表达式（提取模型类名）
import subprocess                                        # 调用外部命令（omc 编译器）
from pathlib import Path

from src.llm_client import chat, user_msg
from src.schemas import StructuredRequirement, SysMLArtifact, ModelicaArtifact


# ====================================================================
# _load_prompt() — 加载 prompt 模板
# ====================================================================
def _load_prompt(name: str) -> str:
    path = os.path.join(os.path.dirname(__file__), "..", "prompts", name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ====================================================================
# generate_and_simulate() — 节点3 主入口
# ====================================================================
def generate_and_simulate(
    req: StructuredRequirement,                           # 节点1 需求
    sysml_artifact: SysMLArtifact,                       # 节点2 SysML 代码
    modelica_dir: Path,                                  # .mo 文件存放目录
    results_dir: Path,                                   # CSV + PNG 存放目录
    max_retries: int = 2,                                # V1 只重试 2 次
) -> ModelicaArtifact:
    """
    生成 Modelica 代码 → 编译 → 仿真 → 失败自修复。

    V1 核心：while 循环自修复。
    每次失败把 error_log 回喂 LLM，最多重试 max_retries 次。
    """
    # ---- 准备 prompt 变量 ----
    params_str = "\n".join(f"  {k} = {v}" for k, v in req.parameters.items())
    constraints_str = "\n".join(f"  - {c}" for c in req.constraints)

    artifact = ModelicaArtifact()                        # 创建空产物
    attempt = 1

    # ---- while 循环自修复（V1 风格）- ----
    while attempt <= max_retries:

        # ---- 构造错误反馈段落 ----
        prev_error_section = ""
        if artifact.errors:                              # 如果之前有编译/仿真错误
            prev_error_section = (
                f"\n## 上次编译/仿真的错误日志（请修正这些错误）\n"
                f"```\n{chr(10).join(artifact.errors)}\n```"
            )

        # ---- 构造完整 prompt ----
        prompt = (
            _load_prompt("node3_modelica.txt")           # 加载模板（含手写热传导示例）
            .replace("{component_type}", req.component_type)
            .replace("{parameters}", params_str)
            .replace("{topology}", req.topology)
            .replace("{constraints}", constraints_str)
            .replace("{sysml_code}", sysml_artifact.sysml_code[:3000])
            # 只取前 3000 字符（防止超 token 限制）
            .replace("{prev_error_section}", prev_error_section)
        )

        # ---- 调 LLM 生成 Modelica 代码 ----
        print(f"[节点3] 第{attempt}次尝试生成 Modelica...")
        mo_code = chat(
            [user_msg(prompt)],
            temperature=0.2,
            max_tokens=4096,
        ).strip()
        mo_code = _clean_code_block(mo_code, "modelica") # 去掉 ```modelica ```

        # ---- 提取模型名 ----
        model_name = _extract_model_name(mo_code) or "MyModel"
        # 正则找 "model Xxx"，找不到用 "MyModel" 兜底

        artifact.modelica_code = mo_code
        artifact.attempts = attempt

        # ---- 保存 .mo 文件 ----
        mo_path = modelica_dir / "model.mo"
        mo_path.write_text(mo_code, encoding="utf-8")
        artifact.file_path = str(mo_path)

        # ========== 编译 ==========
        print(f"[节点3] 编译 {model_name}...")
        compile_ok, compile_err = _compile(str(mo_path), model_name)

        if not compile_ok:
            print(f"[节点3] 编译失败: {compile_err[:200]}")
            artifact.errors.append(f"编译: {compile_err[:500]}")
            attempt += 1                                 # 重试次数 +1
            continue                                     # 回到循环开头（生成新代码）

        # ========== 仿真 ==========
        print(f"[节点3] 仿真 {model_name}...")
        sim_ok, sim_err = _simulate(str(mo_path), model_name, results_dir)

        if not sim_ok:
            print(f"[节点3] 仿真失败: {sim_err[:200]}")
            artifact.errors.append(f"仿真: {sim_err[:500]}")
            attempt += 1
            continue

        # ========== 成功！==========
        csv_path = results_dir / "simulation.csv"
        plot_path = results_dir / "simulation.png"
        artifact.csv_path = str(csv_path)
        artifact.plot_path = str(plot_path)
        artifact.success = True                          # 标记仿真成功

        # ---- 画曲线图 ----
        if csv_path.exists():
            _plot_csv(str(csv_path), str(plot_path), req.component_type)
        else:
            print(f"[节点3] 警告: CSV 文件未找到 {csv_path}")

        print(f"[节点3] 仿真成功！PNG: {plot_path}")
        break                                            # 成功 → 跳出循环
    else:
        # while...else: 循环没有被 break 打断 → 所有重试都失败了
        print(f"[节点3] {max_retries}次重试后仿真仍未成功。")

    return artifact


# ====================================================================
# 编译与仿真（复用 Week 1 的模式）
# ====================================================================

def _compile(mo_path: str, model_name: str) -> tuple[bool, str]:
    """
    编译 .mo 文件。

    方案 A: OMPython（Python API）
      → 安装 OMPython 包后可以直接调
    方案 B: subprocess 调命令行 omc
      → omc --modelica model.mo

    返回: (是否成功, 错误信息)
    """
    # ---- 方案 A: OMPython ----
    try:
        from OMPython import ModelicaSystem              # OpenModelica 的 Python 绑定
        ModelicaSystem(mo_path, model_name)              # 构造时自动做语法 + 编译检查
        return True, ""                                  # 不抛异常 = 成功
    except ImportError:                                  # OMPython 包没装
        pass                                             #   跳过，尝试方案 B
    except Exception as e:                               # 模型有编译错误
        return False, str(e)                             #   返回错误

    # ---- 方案 B: subprocess 调命令行 ----
    try:
        r = subprocess.run(                              # 在子进程中执行命令
            ["omc", "--modelica", mo_path],              # 等效于终端: omc --modelica model.mo
            capture_output=True,                         # 捕获 stdout 和 stderr
            text=True,                                   # 返回文本（非 bytes）
            timeout=60,                                  # 超时 60 秒
        )
        if r.returncode == 0:                            # 返回码 0 = 成功
            return True, ""
        return False, r.stderr or r.stdout               # 错误信息
    except FileNotFoundError:                            # omc 命令不在 PATH 中
        return False, "omc 命令未找到，请确认 OpenModelica 已安装并在 PATH 中"
    except Exception as e:
        return False, str(e)


def _simulate(mo_path: str, model_name: str, results_dir: Path) -> tuple[bool, str]:
    """
    仿真模型，生成 CSV 结果。

    方案 A: OMPython
      setSimulationOptions → simulate()
    方案 B: subprocess 执行 .mos 脚本
      loadFile → simulate

    仿真参数:
      stopTime=10.0         — 仿真到 10 秒
      numberOfIntervals=500 — 采样 500 个点
    """
    # ---- 方案 A: OMPython ----
    try:
        from OMPython import ModelicaSystem
        sim = ModelicaSystem(mo_path, model_name)
        sim.setSimulationOptions(                        # 设置仿真参数
            "stopTime=10.0",                             # 仿真时长
            "numberOfIntervals=500"                      # 采样点
        )
        sim.simulate()                                   # 执行仿真
        return True, ""
    except ImportError:
        pass
    except Exception as e:
        return False, str(e)

    # ---- 方案 B: subprocess 执行 .mos 脚本 ----
    try:
        mos_script = results_dir / "_sim.mos"            # .mos 是 OpenModelica 脚本格式
        mos_script.write_text(                           # 写入脚本内容
            f'loadFile("{mo_path}");\n'                  #   第一行: 加载模型
            f"simulate({model_name}, stopTime=10.0, numberOfIntervals=500);\n"
        )                                                #   第二行: 仿真
        r = subprocess.run(
            ["omc", str(mos_script)],                    # 执行: omc _sim.mos
            capture_output=True, text=True,
            timeout=120,                                 # 仿真可能比编译慢
            cwd=str(results_dir),                        # 工作目录 = results/
        )
        if r.returncode == 0:
            return True, ""
        return False, r.stderr or r.stdout
    except FileNotFoundError:
        return False, "omc 命令未找到"
    except Exception as e:
        return False, str(e)


# ====================================================================
# 工具函数
# ====================================================================

def _clean_code_block(text: str, lang: str) -> str:
    """去掉 LLM 返回的 ```modelica ... ``` 包裹。"""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]                                # 去掉第一行
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]                           # 去掉最后一行
        text = "\n".join(lines)
    return text


def _extract_model_name(code: str) -> str | None:
    """
    从 Modelica 代码中提取模型类名。

    正则: r"model\\s+(\\w+)"
      model   → 匹配关键字
      \\s+    → 至少一个空白字符
      (\\w+)  → 捕获模型名（字母/数字/下划线）

    例如 "model RCLowPassFilter" → 返回 "RCLowPassFilter"
    """
    m = re.search(r"model\s+(\w+)", code)
    return m.group(1) if m else None                     # 找到返回组1，否则 None


def _plot_csv(csv_path: str, plot_path: str, title: str):
    """
    从 CSV 数据画仿真曲线图。

    用 matplotlib Agg 后端（无需 GUI，适合命令行）。
    输出 PNG 文件。
    """
    import matplotlib                                    # pip install matplotlib
    matplotlib.use("Agg")                                # Agg = 不弹窗口，直接画到文件
    import matplotlib.pyplot as plt                      # 绘图 API

    # ---- 读取 CSV ----
    rows = []
    with open(csv_path, "r") as f:                       # 以读模式打开 CSV
        reader = csv.reader(f)                           # csv.reader 解析 CSV
        rows = list(reader)                              # 转成列表

    if len(rows) < 2:                                    # 只有表头，无数据行
        print("[节点3] CSV 数据不足，跳过画图。")
        return

    # ---- 按列整理数据 ----
    header = rows[0]                                     # 第一行 = 列名
    data = {col: [] for col in header}                   # 字典: {列名: [值列表]}

    for row in rows[1:]:                                 # 从第二行开始（跳过表头）
        for i, col in enumerate(header):                 # enumerate 返回 (索引, 值)
            try:
                data[col].append(float(row[i]))          # 字符串 → 浮点数，加入列表
            except (ValueError, IndexError):
                pass                                     # 转换失败就跳过

    time_col = header[0]                                 # 第一列 = 时间（X 轴）

    # ---- 画图 ----
    plt.figure(figsize=(10, 5))                          # 画布大小 10×5 英寸

    for col in header[1:]:                               # 从第二列开始（跳过时间）
        if data[col]:
            plt.plot(                                    # 画折线
                data[time_col][:len(data[col])],          # X: 时间（对齐长度）
                data[col],                               # Y: 变量值
                label=col,                               # 图例 = 变量名
            )

    plt.xlabel(time_col)
    plt.ylabel("Value")
    plt.title(f"仿真结果: {title}")
    plt.legend()
    plt.grid(True)
    plt.tight_layout()                                   # 自动调整边距
    plt.savefig(plot_path, dpi=150)                      # 保存 PNG（150 DPI）
    plt.close()                                          # 关闭画布，释放内存
