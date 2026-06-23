# -*- coding: utf-8 -*-
"""
=============================================================================
node3_modelica.py — 节点3：Modelica 生成 + 编译 + 仿真 + 自修复
=============================================================================

这是整个流水线最复杂的节点。它内部用 LangGraph 子图实现自修复循环。

V1 的问题:
  while 循环，重试逻辑和生成逻辑搅在一起，不好追踪也不好 debug。

V2 的改进:
  用 LangGraph 子图，每个步骤（生成/编译/仿真/修复）都是独立的图节点。
  每次尝试的状态变化都被 checkpoint 记录，可以追踪每一步的输入输出。

子图结构（数据流图）:

  generate_mo → compile_mo → simulate_mo → END(成功)
                   │ 失败        │ 失败
                   ▼             ▼
                repair_mo ←──────┘
                   │
              (retries < 5 → compile_mo)
              (retries >= 5 → END 失败)

═══════════════════════════════════════════════════════
调试过程中发现的关键坑：
═══════════════════════════════════════════════════════

1. attempts 计数器不能存在 mo 字典内部
   原因: 每次节点返回时重建 Pydantic 对象，默认值覆盖了真实计数
   修复: 用独立的 state 键 node3_attempts，compile/simulate 失败时递增

2. 路由判断不能用历史错误列表
   原因: 编译成功→去仿真→仿真失败→去 repair→repair 完→回去编译→
         编译成功，但 errors 里还有之前的仿真错误记录，路由误判为"又失败了"
   修复: 用 node3_step_ok 标志——每一步执行完显式设为 True/False

3. OMPython API 参数名变了
   原因: 新版 OMPython (对应 Modelica 4.0.0) 只有 startTime/stopTime/stepSize/
         tolerance/solver/outputFormat 共 6 个参数
         numberOfIntervals 已移除！
   修复: 用 stepSize=0.00002（ = stopTime/500 ）替代

4. 本机 omc CLI 不在系统 PATH
   修复: _compile 和 _simulate 全用 OMPython，不尝试 subprocess

5. Windows GBK 编码
   原因: OMPython 返回的模型编译错误含非 GBK 字符，str(e) 直接炸
   修复: _safe_str() 函数捕获编码异常后 fallback 到 repr(e)
"""

# ====================================================================
# 导入
# ====================================================================

import csv                                               # 读取仿真输出的 CSV 文件
import logging                                           # 日志
import re                                                # 正则（提取模型类名）
import subprocess                                        # subprocess（备用；本机实际不用）
import time                                              # 计时
from pathlib import Path

from langgraph.graph import StateGraph, START, END       # LangGraph 子图构建
# StateGraph: 状态图构建器
# START: 表示图的入口（特殊节点）
# END:   表示图的出口（特殊节点）

from src.llm_client import chat, user_msg
from src.schemas import StructuredRequirement, ModelicaArtifact
from src.utils import load_prompt, clean_code_block

logger = logging.getLogger("node3")


# ====================================================================
# 工具: _safe_str — 安全转换异常为字符串
# ====================================================================
def _safe_str(e: Exception) -> str:
    """
    安全地把异常转字符串，绕过 Windows GBK 编码问题。

    流程:
      1. 先试 str(e) — 正常情况都能过
      2. 如果抛出 UnicodeEncodeError/UnicodeDecodeError（中文 Windows 常见）
         → 改用 repr(e)，格式如 "RuntimeError('xxx')"，但不会炸
      3. 如果连 repr 都炸了（极其罕见），回退到类型名
      4. 截取前 500 字符（防止超长错误信息塞满 state）
    """
    try:
        s = str(e)                                       # 第一步：正常转字符串
    except (UnicodeEncodeError, UnicodeDecodeError):     # Windows 中文环境下 OMPython 常触发
        try:
            s = repr(e)                                  # 第二步：用 repr 兜底
        except Exception:                                # 极其罕见的极端情况
            s = f"{type(e).__name__}"                    # 第三步：只返回异常类型名
    return s[:500]                                       # 截断到 500 字符


# ====================================================================
# build_node3_subgraph() — 构建子图
# ====================================================================
def build_node3_subgraph() -> StateGraph:
    """
    构建并返回节点3的 LangGraph 子图。

    这个函数在 pipeline.py 中被调用：
      builder.add_node("node3_subgraph", build_node3_subgraph())

    返回值是一个"编译好的图"（CompiledGraph），
    父图把它当作一个黑盒节点来用。
    """
    builder = StateGraph(dict)                           # dict 做 state 类型（兼容父图）

    # 注册节点
    builder.add_node("generate_mo", _generate_mo)        # 生成 Modelica 代码
    builder.add_node("compile_mo", _compile_mo)          # 编译
    builder.add_node("simulate_mo", _simulate_mo)        # 仿真
    builder.add_node("repair_mo", _repair_mo)            # 自修复

    # 固定边
    builder.add_edge(START, "generate_mo")               # 入口 → 生成代码
    builder.add_edge("generate_mo", "compile_mo")        # 生成后 → 直接编译

    # 条件边（每个条件边 = 源节点 + 路由函数 + {返回值: 目标节点}）
    builder.add_conditional_edges(
        "compile_mo",                                    # 从编译节点出发
        _route_after_compile,                            # 路由函数
        {
            "simulate_mo": "simulate_mo",                #   编译成功 → 去仿真
            "repair_mo": "repair_mo",                    #   编译失败 → 去修复
            "end_fail": END,                             #   重试耗尽 → 子图出口（失败）
        }
    )

    builder.add_conditional_edges(
        "simulate_mo",
        _route_after_simulate,
        {
            "end_success": END,                          #   仿真成功 → 出口
            "repair_mo": "repair_mo",                    #   仿真失败 → 修复
            "end_fail": END,                             #   重试耗尽 → 出口
        }
    )

    builder.add_conditional_edges(
        "repair_mo",
        _route_after_repair,
        {
            "compile_mo": "compile_mo",                  #   修完后回去编译
            "end_fail": END,                             #   重试耗尽 → 出口
        }
    )

    return builder.compile()


# ============================================================
# 子图节点函数
# 每个函数: 读 state → 做事情 → 返回要更新的字段
#
# 关键: 返回的 dict 中要包含 node3_attempts 和 node3_step_ok
#       （即使值不变也要回传，否则 LangGraph 会从 state 中删除）
# ============================================================

def _generate_mo(state: dict) -> dict:
    """
    子图节点：LLM 从 StructuredRequirement + SysML v2 代码生成 Modelica 代码。

    这是子图的入口，只执行一次。后续修复由 repair_mo 负责。
    这里把 node3_attempts 初始化为 0。
    """
    t0 = time.time()

    # ---- 从 state 提取数据 ----
    req_dict = state.get("req", {})
    sysml_dict = state.get("sysml", {})
    sysml_code = sysml_dict.get("sysml_code", "")
    temperature = state.get("temperature", 0.3)

    # 安全构造 StructuredRequirement
    if req_dict:
        req = StructuredRequirement(**req_dict)
    else:
        req = StructuredRequirement(component_type="unknown", raw_input="")

    params_str = "\n".join(f"  {k} = {v}" for k, v in req.parameters.items())
    constraints_str = "\n".join(f"  - {c}" for c in req.constraints)

    # 构造 prompt（第一次不包含错误信息）
    prompt = (
        load_prompt("node3_modelica.txt")
        .replace("{component_type}", req.component_type)
        .replace("{parameters}", params_str)
        .replace("{topology}", req.topology)
        .replace("{constraints}", constraints_str)
        .replace("{sysml_code}", sysml_code[:3000])
        .replace("{prev_error_section}", "")             # 第一次无错误日志
    )

    logger.info("节点3 generate: 生成 Modelica 代码...")
    mo_code = chat([user_msg(prompt)], temperature=temperature, max_tokens=4096).strip()
    mo_code = clean_code_block(mo_code, "modelica")

    model_name = _extract_model_name(mo_code) or "MyModel"
    logger.info("节点3 generate 完成, 模型名=%s", model_name)

    # 初始 mo 字典（后续节点直接在字典上操作，不重建 Pydantic 对象）
    return {
        "mo": {
            "modelica_code": mo_code,
            "file_path": "",
            "csv_path": "",
            "plot_path": "",
            "attempts": 0,
            "errors": [],
            "success": False,
        },
        "node3_attempts": 0,                             # ← 独立计数器，不存 mo 内部
        "node3_step_ok": False,                          # ← 当前步骤是否成功
        "timing": {**state.get("timing", {}), "node3_generate": time.time() - t0},
    }


def _compile_mo(state: dict) -> dict:
    """
    子图节点：用 OMPython 编译 Modelica 代码。

    注意: 本机 omc CLI 不在 PATH，直接用 OMPython。

    关键: 编译成功时也回传 node3_attempts，防止计数被 LangGraph 清掉。
         编译成功 → node3_step_ok = True
         编译失败 → node3_step_ok = False, 计数+1
    """
    t0 = time.time()
    mo_dict = state.get("mo", {})
    run_dir = Path(state.get("run_dir", "."))
    modelica_dir = run_dir / "modelica"

    modelica_code = mo_dict.get("modelica_code", "")
    model_name = _extract_model_name(modelica_code) or "MyModel"

    # 保存 .mo 文件到磁盘
    modelica_dir.mkdir(parents=True, exist_ok=True)
    mo_path = modelica_dir / "model.mo"
    mo_path.write_text(modelica_code, encoding="utf-8")

    logger.info("节点3 compile: 编译 %s...", model_name)
    compile_ok, compile_err = _compile(str(mo_path), model_name)

    errors = list(mo_dict.get("errors", []))             # 复制现有错误列表（list() 新建副本）

    if not compile_ok:
        attempts = state.get("node3_attempts", 0) + 1    # 计数 +1
        logger.warning("节点3 compile 失败 (第%s次): %s", attempts, compile_err[:200])
        errors.append(f"[编译错误 #{attempts}] {compile_err[:500]}")

        return {
            "mo": {**mo_dict, "errors": errors, "attempts": attempts, "file_path": str(mo_path)},
            "node3_attempts": attempts,                  # 递增后的计数器
            "node3_step_ok": False,                      # ← 路由靠这个判断
            "timing": {**state.get("timing", {}), "node3_compile": time.time() - t0},
        }

    # 编译成功
    logger.info("节点3 compile 成功")
    return {
        "mo": {**mo_dict, "errors": errors, "file_path": str(mo_path)},
        "node3_attempts": state.get("node3_attempts", 0), # ← 保持原值，不变
        "node3_step_ok": True,                           # ← 告诉路由: 这一步 OK
        "timing": {**state.get("timing", {}), "node3_compile": time.time() - t0},
    }


def _simulate_mo(state: dict) -> dict:
    """
    子图节点：仿真模型，生成 CSV 数据。

    关键: 用 stepSize 替代 numberOfIntervals。
          新版 OMPython 合法参数只有: startTime/stopTime/stepSize/tolerance/solver/outputFormat。
    """
    t0 = time.time()
    mo_dict = state.get("mo", {})
    run_dir = Path(state.get("run_dir", "."))
    modelica_dir = run_dir / "modelica"
    results_dir = run_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    modelica_code = mo_dict.get("modelica_code", "")
    model_name = _extract_model_name(modelica_code) or "MyModel"
    mo_path = str(modelica_dir / "model.mo")

    logger.info("节点3 simulate: 仿真 %s...", model_name)
    sim_ok, sim_err = _simulate(mo_path, model_name, results_dir)

    errors = list(mo_dict.get("errors", []))

    if not sim_ok:
        attempts = state.get("node3_attempts", 0) + 1
        logger.warning("节点3 simulate 失败 (第%s次): %s", attempts, sim_err[:200])
        errors.append(f"[仿真错误 #{attempts}] {sim_err[:500]}")

        return {
            "mo": {**mo_dict, "errors": errors, "attempts": attempts},
            "node3_attempts": attempts,
            "node3_step_ok": False,                      # ← 仿真失败
            "timing": {**state.get("timing", {}), "node3_simulate": time.time() - t0},
        }

    # 仿真成功 → 找 CSV 文件
    csv_path = results_dir / "simulation.csv"
    plot_path = results_dir / "simulation.png"

    # 尝试从 OMPython 工作目录找 CSV
    # （OMPython 的 ModelicaSystem 对象在 _simulate 内部创建，这里无法访问其 _workDir）
    # 所以这里只做标记，CSV 的定位在 _simulate 内部完成

    if csv_path.exists():
        _plot_csv(str(csv_path), str(plot_path), state.get("req", {}).get("component_type", "System"))

    logger.info("节点3 simulate 成功, PNG: %s", plot_path)
    return {
        "mo": {**mo_dict, "errors": errors, "success": True,
               "csv_path": str(csv_path), "plot_path": str(plot_path)},
        "node3_attempts": state.get("node3_attempts", 0), # ← 保持原值
        "node3_step_ok": True,                           # ← 仿真成功
        "timing": {**state.get("timing", {}), "node3_simulate": time.time() - t0},
    }


def _repair_mo(state: dict) -> dict:
    """
    子图节点：LLM 根据错误日志重新生成 Modelica 代码。

    这是"自修复"的核心:
      1. 从 state.mo.errors 取出最近的错误日志（最近 5 条）
      2. 把错误日志注入 prompt
      3. LLM 生成修正后的代码
      4. 覆盖旧的 modelica_code（errors 保留，不清空）

    注意: repair 本身不增/减计数器（compile 和 simulate 失败时才递增）。
          repair 只做一件事：换代码。
    """
    t0 = time.time()

    req_dict = state.get("req", {})
    sysml_dict = state.get("sysml", {})
    mo_dict = state.get("mo", {})
    sysml_code = sysml_dict.get("sysml_code", "")
    temperature = state.get("temperature", 0.3)
    attempts = state.get("node3_attempts", 0)            # 当前已尝试次数（只读，不修改）

    req = StructuredRequirement(**req_dict) if req_dict else StructuredRequirement(
        component_type="unknown", raw_input=""
    )

    params_str = "\n".join(f"  {k} = {v}" for k, v in req.parameters.items())
    constraints_str = "\n".join(f"  - {c}" for c in req.constraints)

    # 构造错误反馈段落（取最近 5 条，给 LLM 更多上下文）
    errors = mo_dict.get("errors", [])
    if errors:
        error_section = (
            "## 上次编译/仿真的错误日志（请逐一修正）\n"
            "```\n" + "\n".join(errors[-5:]) + "\n```\n\n"
            "请仔细分析以上错误，重新生成完整的、可编译的 Modelica 代码。"
        )
    else:
        error_section = ""

    prompt = (
        load_prompt("node3_modelica.txt")
        .replace("{component_type}", req.component_type)
        .replace("{parameters}", params_str)
        .replace("{topology}", req.topology)
        .replace("{constraints}", constraints_str)
        .replace("{sysml_code}", sysml_code[:3000])
        .replace("{prev_error_section}", error_section)  # ← 这次有错误信息了
    )

    logger.info("节点3 repair: 第%s次修复...", attempts)
    mo_code = chat([user_msg(prompt)], temperature=temperature, max_tokens=4096).strip()
    mo_code = clean_code_block(mo_code, "modelica")

    logger.info("节点3 repair 完成, 新模型名=%s", _extract_model_name(mo_code) or "未识别")

    # 只换代码，不动计数器
    return {
        "mo": {**mo_dict, "modelica_code": mo_code},
        "node3_attempts": attempts,                      # ← 保持不变
        "node3_step_ok": False,                          # ← 重置为 False（等下轮 compile 来设 True）
        "timing": {**state.get("timing", {}), "node3_repair": time.time() - t0},
    }


# ============================================================
# 路由函数 — 查 node3_step_ok 而不是历史错误
#
# 核心逻辑:
#   node3_step_ok == True  → 当前步骤成功，往下走
#   node3_step_ok == False → 当前步骤失败
#     - node3_attempts < max_retries → repair_mo（还有机会）
#     - node3_attempts >= max_retries → end_fail（放弃）
# ============================================================

def _route_after_compile(state: dict) -> str:
    """编译后路由：查 node3_step_ok，不是查历史错误列表。"""
    if state.get("node3_step_ok", False):
        return "simulate_mo"                             # 编译成功 → 去仿真

    attempts = state.get("node3_attempts", 0)
    max_retries = state.get("max_retries", 5)
    if attempts >= max_retries:                          # >=: 等于上限时就停
        logger.warning("节点3: 编译重试耗尽 (%s/%s)", attempts, max_retries)
        return "end_fail"

    return "repair_mo"


def _route_after_simulate(state: dict) -> str:
    """仿真后路由：查 node3_step_ok。"""
    if state.get("node3_step_ok", False):
        return "end_success"

    attempts = state.get("node3_attempts", 0)
    max_retries = state.get("max_retries", 5)
    if attempts >= max_retries:
        logger.warning("节点3: 仿真重试耗尽 (%s/%s)", attempts, max_retries)
        return "end_fail"

    return "repair_mo"


def _route_after_repair(state: dict) -> str:
    """修复后路由：只看计数器。"""
    attempts = state.get("node3_attempts", 0)
    max_retries = state.get("max_retries", 5)
    if attempts >= max_retries:
        logger.warning("节点3: 修复重试耗尽 (%s/%s)", attempts, max_retries)
        return "end_fail"

    return "compile_mo"


# ============================================================
# 编译 & 仿真 — 底层实现（本机用 OMPython）
# ============================================================

def _compile(mo_path: str, model_name: str) -> tuple[bool, str]:
    """
    编译 .mo 文件。

    本机 omc CLI 不在系统 PATH → 直接用 OMPython。
    ModelicaSystem 构造时会 loadFile + 初步编译——如果模型有语法错误这里会抛异常。

    返回:
      (True, "")           编译成功
      (False, "错误信息")   编译失败
    """
    try:
        from OMPython import ModelicaSystem              # OMPython = OpenModelica 的 Python 绑定
        ModelicaSystem(mo_path, model_name)              # 构造时自动做语法 + 编译检查
        return True, ""                                  # 不抛异常 = 成功
    except ImportError:                                  # OMPython 包没装
        return False, "OMPython 未安装"
    except Exception as e:                               # 模型有编译错误（变量未定义等）
        return False, f"[OMPython] {_safe_str(e)}"       # 用 _safe_str 防 GBK 编码炸


def _simulate(mo_path: str, model_name: str, results_dir: Path) -> tuple[bool, str]:
    """
    仿真模型。

    关键坑: 新版 OMPython 去掉了 numberOfIntervals，改用 stepSize。
    合法参数: startTime / stopTime / stepSize / tolerance / solver / outputFormat

    stepSize = stopTime / 采样点数
    例如: stopTime=0.01, 500点 → stepSize = 0.01/500 = 0.00002

    输出格式设为 csv，结果从 OMPython 工作目录复制到 results/。
    """
    try:
        from OMPython import ModelicaSystem
        sim = ModelicaSystem(mo_path, model_name)

        # 用 dict 格式传参（新版 API 推荐方式）
        sim.setSimulationOptions({
            "stopTime": "0.01",
            "stepSize": "0.00002",                       # = 0.01 / 500
            "outputFormat": "csv",                       # 输出 CSV（而不是默认的 mat）
        })
        sim.simulate()

        # 仿真结果在 OMPython 的临时工作目录，复制到我们的 results/
        work_dir = Path(getattr(sim, '_workDir', '')) if hasattr(sim, '_workDir') else None
        if work_dir and work_dir.exists():
            for csv_file in work_dir.glob("*.csv"):
                import shutil
                dest = results_dir / "simulation.csv"
                shutil.copy2(csv_file, dest)
                return True, ""

        # 备选：直接在 results_dir 找
        csv_candidates = list(results_dir.glob(f"{model_name}_res.csv")) + \
                         list(results_dir.glob("*.csv"))
        if csv_candidates:
            if csv_candidates[0].name != "simulation.csv":
                import shutil
                shutil.copy2(csv_candidates[0], results_dir / "simulation.csv")
            return True, ""

        return True, ""                                  # 仿真成功但没找到 CSV

    except ImportError:
        return False, "OMPython 未安装"
    except Exception as e:
        return False, f"[OMPython] {_safe_str(e)}"


def _extract_model_name(code: str) -> str | None:
    """
    从 Modelica 代码中提取模型名。

    正则: r"model\\s+(\\w+)"
      model  → 匹配关键字
      空白   → 至少一个
      (\\w+) → 捕获模型名（字母/数字/下划线）

    例如 "model SingleRoomThermal" → 返回 "SingleRoomThermal"
    """
    m = re.search(r"model\s+(\w+)", code)
    return m.group(1) if m else None


def _plot_csv(csv_path: str, plot_path: str, title: str):
    """
    从 CSV 数据画仿真曲线图。

    CSV 格式:
      第一列 = 时间（time），后续列 = 各物理变量的值

    用 matplotlib Agg 后端（无需 GUI，适合服务器/脚本环境）。
    """
    import matplotlib
    matplotlib.use("Agg")                                # Agg 后端 = 不弹窗口
    import matplotlib.pyplot as plt

    with open(csv_path, "r") as f:
        rows = list(csv.reader(f))                       # 二维列表: [[表头], [数据行], ...]

    if len(rows) < 2:                                    # 只有表头无数据
        return

    header = rows[0]                                     # 第一行 = 列名
    data = {col: [] for col in header}                   # 初始化每列的空列表

    for row in rows[1:]:                                 # 从第二行开始（跳过表头）
        for i, col in enumerate(header):
            try:
                data[col].append(float(row[i]))          # 字符串 → 浮点数
            except (ValueError, IndexError):
                pass

    time_col = header[0]                                 # 第一列 = X 轴
    plt.figure(figsize=(10, 5))

    for col in header[1:]:                               # 跳过时间列，画其余所有列
        if data[col]:
            plt.plot(
                data[time_col][:len(data[col])],         # X 轴对齐长度
                data[col],
                label=col
            )

    plt.xlabel(time_col)
    plt.ylabel("Value")
    plt.title(f"Simulation: {title}")
    plt.legend()
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(plot_path, dpi=150)
    plt.close()
