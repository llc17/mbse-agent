# -*- coding: utf-8 -*-
"""
=============================================================================
node3_modelica.py — 节点 3：Modelica 生成 + 编译 + 仿真 + 自修复（V3 版）
=============================================================================

这是系统中最复杂的节点。它是一个 LangGraph 子图（subgraph），
内部包含 4 个子节点 + 条件路由。

子图结构:
    generate_mo → compile_mo → simulate_mo → END(子图出口，成功)
                     ↓ 失败        ↓ 失败
                  repair_mo ←─────────┘
                     ↓
                (retries < max_retries → 回到 compile_mo)
                (retries >= max_retries → END 失败，由主图路由决定去向)

V3 改动（相比 V2）：
  1. stopTime 自动适配：不再硬编码 0.01s，
     _simulate() 根据 component_type 选择电气 0.01s / 热 1000s
  2. MAT→CSV 转换：OMPython 默认输出 .mat 格式，
     新增 _convert_mat_to_csv() 通过 OMC API 读取时间序列后写 CSV
  3. 修复日志：_repair_mo() 每次记录 {attempt, errors_before,
     code_before_snippet, code_after_snippet}
  4. run_dir 传递：所有子图节点的 return 都显式回传 run_dir，
     修复了 V2 中 modelica/ 落在项目根目录而非 outputs/ 下的 bug
"""

# ====================================================================
# 导入
# ====================================================================
import csv
import logging
import re
import subprocess
import time
from pathlib import Path

from langgraph.graph import StateGraph, START, END

from src.llm_client import chat, user_msg
from src.schemas import StructuredRequirement, ModelicaArtifact
from src.utils import load_prompt, clean_code_block, get_stop_time_for_domain

logger = logging.getLogger("node3")


# ====================================================================
# 构建子图 — 被 pipeline.py 调用
# ====================================================================

def build_node3_subgraph() -> StateGraph:
    """构建并返回编译后的 node3 子图"""
    builder = StateGraph(dict)

    builder.add_node("generate_mo", _generate_mo)
    builder.add_node("compile_mo", _compile_mo)
    builder.add_node("simulate_mo", _simulate_mo)
    builder.add_node("repair_mo", _repair_mo)

    builder.add_edge(START, "generate_mo")
    builder.add_edge("generate_mo", "compile_mo")

    builder.add_conditional_edges("compile_mo", _route_after_compile, {
        "simulate_mo": "simulate_mo",
        "repair_mo": "repair_mo",
        "end_fail": END,
    })

    builder.add_conditional_edges("simulate_mo", _route_after_simulate, {
        "end_success": END,
        "repair_mo": "repair_mo",
        "end_fail": END,
    })

    builder.add_conditional_edges("repair_mo", _route_after_repair, {
        "compile_mo": "compile_mo",
        "end_fail": END,
    })

    return builder.compile()


# ====================================================================
# 子节点 1：generate_mo — LLM 生成 Modelica 代码
# ====================================================================
# 这是子图的入口。读 req + sysml，调 LLM 生成初始 .mo 代码。
# 如果这是修复循环中的一次调用，prev_error_section 会包含上次的错误日志。

def _generate_mo(state: dict) -> dict:
    t0 = time.time()
    req_dict = state.get("req", {})
    sysml_dict = state.get("sysml", {})
    sysml_code = sysml_dict.get("sysml_code", "")
    temperature = state.get("temperature", 0.3)

    req = StructuredRequirement(**req_dict) if req_dict else StructuredRequirement(
        component_type="unknown", raw_input=""
    )

    params_str = "\n".join(f"  {k} = {v}" for k, v in req.parameters.items())
    constraints_str = "\n".join(f"  - {c}" for c in req.constraints)

    # ---- 用 prompt 模板构造完整 prompt ----
    prompt = (
        load_prompt("node3_modelica.txt")
        .replace("{component_type}", req.component_type)
        .replace("{parameters}", params_str)
        .replace("{topology}", req.topology)
        .replace("{constraints}", constraints_str)
        .replace("{sysml_code}", sysml_code[:3000])              # 截断到 3000 字符
        .replace("{prev_error_section}", "")                     # 首次生成无错误
    )

    logger.info("节点3 generate: 生成 Modelica 代码...")
    mo_code = chat([user_msg(prompt)], temperature=temperature, max_tokens=4096).strip()
    mo_code = clean_code_block(mo_code, "modelica")

    model_name = _extract_model_name(mo_code) or "MyModel"
    logger.info("节点3 generate 完成, 模型名=%s", model_name)

    return {
        "mo": {
            "modelica_code": mo_code, "file_path": "", "csv_path": "",
            "plot_path": "", "attempts": 0, "errors": [], "success": False,
        },
        "node3_attempts": 0,
        "node3_step_ok": False,
        "run_dir": state.get("run_dir", ""),                     # V3: 显式传递
        "timing": {**state.get("timing", {}), "node3_generate": time.time() - t0},
    }


# ====================================================================
# 子节点 2：compile_mo — 编译 Modelica 代码
# ====================================================================
# 关键设计：节点通过 node3_step_ok: bool 告诉路由"我成功了还是失败了"。
# 这比 V1 中用历史错误列表判断成败更可靠——避免修复后仍携带旧错误的边界 case。

def _compile_mo(state: dict) -> dict:
    t0 = time.time()
    mo_dict = state.get("mo", {})
    run_dir = Path(state.get("run_dir", ".")).resolve()          # V3: resolve 绝对路径
    modelica_dir = run_dir / "modelica"

    modelica_code = mo_dict.get("modelica_code", "")
    model_name = _extract_model_name(modelica_code) or "MyModel"

    modelica_dir.mkdir(parents=True, exist_ok=True)
    mo_path = (modelica_dir / "model.mo")
    mo_path.write_text(modelica_code, encoding="utf-8")

    logger.info("节点3 compile: 编译 %s...", model_name)
    compile_ok, compile_err = _compile(str(mo_path.resolve()), model_name)

    errors = list(mo_dict.get("errors", []))

    if not compile_ok:
        # ---- 编译失败 → 计数器 +1，路由到 repair ----
        attempts = state.get("node3_attempts", 0) + 1
        logger.warning("节点3 compile 失败 (第%s次): %s", attempts, compile_err[:200])
        errors.append(f"[编译错误 #{attempts}] {compile_err[:500]}")

        return {
            "mo": {**mo_dict, "errors": errors, "attempts": attempts,
                   "file_path": str(mo_path.resolve())},
            "node3_attempts": attempts,
            "node3_step_ok": False,                               # ← 路由据此判断：需要修复
            "run_dir": state.get("run_dir", ""),
            "timing": {**state.get("timing", {}), "node3_compile": time.time() - t0},
        }

    logger.info("节点3 compile 成功")
    return {
        "mo": {**mo_dict, "errors": errors, "file_path": str(mo_path.resolve())},
        "node3_attempts": state.get("node3_attempts", 0),
        "node3_step_ok": True,                                    # ← 路由据此判断：去仿真
        "run_dir": state.get("run_dir", ""),
        "timing": {**state.get("timing", {}), "node3_compile": time.time() - t0},
    }


# ====================================================================
# 子节点 3：simulate_mo — 仿真 + CSV 转换
# ====================================================================
# V3 核心改动在这里：
#   1. 读取 req.component_type → get_stop_time_for_domain() → 动态 stopTime
#   2. 仿真后用 _convert_mat_to_csv() 将 MAT 结果转 CSV
#   3. 仿真成功时保存 repair_log.json 到 results 目录

def _simulate_mo(state: dict) -> dict:
    t0 = time.time()
    mo_dict = state.get("mo", {})
    run_dir = Path(state.get("run_dir", ".")).resolve()
    modelica_dir = run_dir / "modelica"
    results_dir = run_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    modelica_code = mo_dict.get("modelica_code", "")
    model_name = _extract_model_name(modelica_code) or "MyModel"
    mo_path = str(modelica_dir / "model.mo")

    # ── V3: 根据 component_type 自动选 stopTime ──
    req = state.get("req", {})
    stop_time = get_stop_time_for_domain(req.get("component_type", ""))
    logger.info("节点3 simulate: 仿真 %s (stopTime=%s)...", model_name, stop_time)

    sim_ok, sim_err = _simulate(mo_path, model_name, results_dir, stop_time)

    errors = list(mo_dict.get("errors", []))

    if not sim_ok:
        # ---- 仿真失败 ----
        attempts = state.get("node3_attempts", 0) + 1
        logger.warning("节点3 simulate 失败 (第%s次): %s", attempts, sim_err[:200])
        errors.append(f"[仿真错误 #{attempts}] {sim_err[:500]}")

        return {
            "mo": {**mo_dict, "errors": errors, "attempts": attempts},
            "node3_attempts": attempts,
            "node3_step_ok": False,
            "run_dir": state.get("run_dir", ""),
            "timing": {**state.get("timing", {}), "node3_simulate": time.time() - t0},
        }

    # ---- 仿真成功 ----
    csv_path = results_dir / "simulation.csv"
    plot_path = results_dir / "simulation.png"

    if csv_path.exists():
        _plot_csv(str(csv_path), str(plot_path),
                  state.get("req", {}).get("component_type", "System"))

    logger.info("节点3 simulate 成功, PNG: %s", plot_path)

    # ── V3: 保存修复日志 ----
    repair_log = state.get("repair_log", [])
    if repair_log:
        import json as _json
        log_path = results_dir / "repair_log.json"
        log_path.write_text(_json.dumps(repair_log, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("修复日志已保存: %s (%s 次修复)", log_path, len(repair_log))

    return {
        "mo": {**mo_dict, "errors": errors, "success": True,
               "csv_path": str(csv_path.resolve()), "plot_path": str(plot_path.resolve())},
        "node3_attempts": state.get("node3_attempts", 0),
        "node3_step_ok": True,
        "run_dir": state.get("run_dir", ""),
        "timing": {**state.get("timing", {}), "node3_simulate": time.time() - t0},
    }


# ====================================================================
# 子节点 4：repair_mo — LLM 修复 Modelica 代码
# ====================================================================
# V3 改动：每次修复都记录到 repair_log。
# 日志包含修复前的错误 + 代码片段 vs 修复后的代码片段。
# 这对于分析"LLM 修了什么"至关重要。

def _repair_mo(state: dict) -> dict:
    """LLM 根据错误日志重新生成 Modelica 代码。V3: 记录修复日志。"""
    t0 = time.time()
    req_dict = state.get("req", {})
    sysml_dict = state.get("sysml", {})
    mo_dict = state.get("mo", {})
    sysml_code = sysml_dict.get("sysml_code", "")
    temperature = state.get("temperature", 0.3)
    attempts = state.get("node3_attempts", 0)

    # ---- V3: 记录修复前的代码和错误 ----
    code_before = mo_dict.get("modelica_code", "")
    errors_before = mo_dict.get("errors", [])[-3:]             # 最近 3 个错误

    req = StructuredRequirement(**req_dict) if req_dict else StructuredRequirement(
        component_type="unknown", raw_input=""
    )

    params_str = "\n".join(f"  {k} = {v}" for k, v in req.parameters.items())
    constraints_str = "\n".join(f"  - {c}" for c in req.constraints)

    # ---- 构造带错误日志的修复 prompt ----
    errors = mo_dict.get("errors", [])
    error_section = (
        "## 上次编译/仿真的错误日志（请逐一修正）\n"
        "```\n" + "\n".join(errors[-5:]) + "\n```"
        "\n\n请仔细分析以上错误，重新生成完整的、可编译的 Modelica 代码。"
    ) if errors else ""

    prompt = (
        load_prompt("node3_modelica.txt")
        .replace("{component_type}", req.component_type)
        .replace("{parameters}", params_str)
        .replace("{topology}", req.topology)
        .replace("{constraints}", constraints_str)
        .replace("{sysml_code}", sysml_code[:3000])
        .replace("{prev_error_section}", error_section)
    )

    logger.info("节点3 repair: 第%s次修复...", attempts)
    mo_code = chat([user_msg(prompt)], temperature=temperature, max_tokens=4096).strip()
    mo_code = clean_code_block(mo_code, "modelica")

    # ---- V3: 记录修复日志 ----
    repair_entry = {
        "attempt": attempts,
        "errors_before": errors_before,
        "code_before_snippet": code_before[:200] if code_before else "",
        "code_after_snippet": mo_code[:200],
    }
    repair_log = list(state.get("repair_log", []))
    repair_log.append(repair_entry)

    logger.info("节点3 repair 完成, 新模型名=%s, 已记录修复日志",
                _extract_model_name(mo_code) or "未识别")
    return {
        "mo": {**mo_dict, "modelica_code": mo_code},
        "node3_attempts": attempts,
        "node3_step_ok": False,                                  # 重置：回去编译
        "repair_log": repair_log,                                # V3: 累计修复记录
        "run_dir": state.get("run_dir", ""),
        "timing": {**state.get("timing", {}), "node3_repair": time.time() - t0},
    }


# ====================================================================
# 子图路由 — 三个条件边
# ====================================================================

def _route_after_compile(state: dict) -> str:
    """编译后：成功→仿真, 失败且未超限→修复, 超限→子图失败出口"""
    if state.get("node3_step_ok", False):
        return "simulate_mo"
    attempts = state.get("node3_attempts", 0)
    max_retries = state.get("max_retries", 5)
    if attempts >= max_retries:
        logger.warning("节点3: 编译重试耗尽 (%s/%s)", attempts, max_retries)
        return "end_fail"
    return "repair_mo"


def _route_after_simulate(state: dict) -> str:
    """仿真后：成功→子图成功出口, 失败且未超限→修复, 超限→子图失败出口"""
    if state.get("node3_step_ok", False):
        return "end_success"
    attempts = state.get("node3_attempts", 0)
    max_retries = state.get("max_retries", 5)
    if attempts >= max_retries:
        logger.warning("节点3: 仿真重试耗尽 (%s/%s)", attempts, max_retries)
        return "end_fail"
    return "repair_mo"


def _route_after_repair(state: dict) -> str:
    """修复后：未超限→回去编译, 超限→子图失败出口"""
    attempts = state.get("node3_attempts", 0)
    max_retries = state.get("max_retries", 5)
    if attempts >= max_retries:
        logger.warning("节点3: 修复重试耗尽 (%s/%s)", attempts, max_retries)
        return "end_fail"
    return "compile_mo"


# ====================================================================
# 编译 — OMPython
# ====================================================================
# 关键坑（V2 已知，V3 沿用）：
#   ModelicaSystem 构造失败时，Python 异常只返回
#   "Error executing buildModel(...)"。
#   真正的 Modelica 语法/类型错误在 OMPython 的日志里，不在异常消息里。
#   因此用 StringIO 劫持日志，提取 error/warning/syntax 行喂给 LLM repair。

def _safe_str(e: Exception) -> str:
    """安全异常转字符串，绕过 Windows GBK 编码问题"""
    try:
        s = str(e)
    except (UnicodeEncodeError, UnicodeDecodeError):
        try:
            s = repr(e)
        except Exception:
            s = f"{type(e).__name__}"
    return s[:500]


def _compile(mo_path: str, model_name: str) -> tuple[bool, str]:
    """
    编译 .mo 文件。劫持 OMPython 日志获取真实编译错误。
    返回 (成功?, 错误信息)
    """
    import io
    try:
        from OMPython import ModelicaSystem

        # ---- 劫持 OMPython 日志 → 抓真实编译错误 ----
        ompy_logger = logging.getLogger("OMPython")
        old_level = ompy_logger.level
        ompy_logger.setLevel(logging.DEBUG)
        log_stream = io.StringIO()
        handler = logging.StreamHandler(log_stream)
        handler.setLevel(logging.DEBUG)
        ompy_logger.addHandler(handler)

        try:
            ModelicaSystem(mo_path, model_name)
            return True, ""
        except ImportError:
            return False, "OMPython 未安装"
        except Exception as e:
            # ---- 从日志提取真正的编译错误 ----
            log_content = log_stream.getvalue()
            error_lines = []
            for line in log_content.split("\n"):
                lower = line.lower()
                if any(kw in lower for kw in [
                    "error", "warning", "syntax", "undefined",
                    "unknown", "missing", "cannot", "invalid",
                    "unmatched", "unexpected", "not found",
                ]):
                    if "omc log" in lower:
                        parts = line.split("]:", 1)
                        error_lines.append(parts[-1].strip() if len(parts) > 1 else line.strip())
                    else:
                        error_lines.append(line.strip())

            if error_lines:
                return False, "[OMC编译错误]\n" + "\n".join(error_lines[-10:])
            else:
                return False, f"[OMPython] {_safe_str(e)}"
        finally:
            ompy_logger.removeHandler(handler)
            ompy_logger.setLevel(old_level)

    except ImportError:
        return False, "OMPython 未安装"


# ====================================================================
# 仿真 + MAT→CSV — V3 核心改动
# ====================================================================
# OMPython simulate() 默认输出 .mat 格式。
# V2 尝试搜 CSV 文件但经常找不到（OMPython 不保证 CSV 输出）。
# V3 改为：显式指定 resultfile → 从 MAT 通过 OMC API 读时间序列 → 写 CSV。

def _simulate(mo_path: str, model_name: str, results_dir: Path,
              stop_time: float = 0.01) -> tuple[bool, str]:
    """
    仿真模型，自动将 MAT 结果转为 CSV。
    V3: stopTime 根据物理域动态设置，不再硬编码。
    """
    try:
        from OMPython import ModelicaSystem
        sim = ModelicaSystem(mo_path, model_name)
        step_size = stop_time / 500.0
        sim.setSimulationOptions({
            "stopTime": str(stop_time),
            "stepSize": str(step_size),
        })

        # 显式 resultfile（.mat 后缀确保 OMC 能找到）
        result_mat = str(results_dir / f"{model_name}.mat")
        sim.simulate(resultfile=result_mat)

        if Path(result_mat).exists():
            _convert_mat_to_csv(sim, result_mat, model_name, results_dir)
            return True, ""
        else:
            logger.warning("仿真成功但 MAT 文件未生成: %s", result_mat)
            return True, ""

    except ImportError:
        return False, "OMPython 未安装"
    except Exception as e:
        return False, f"[OMPython] {_safe_str(e)}"


# ====================================================================
# MAT → CSV 转换
# ====================================================================
# 为什么这么复杂？
#   OpenModelica 的 CSV 输出不可靠——outputFormat=csv 设置了但
#   文件可能落在 OMPython 内部工作目录、当前目录、或根本不生成。
#   但 .mat 文件是可靠产出的（二进制格式，固定路径）。
#   因此最稳定的方案是：出 MAT → 用 OMC API 读 → 写我们自己的 CSV。

def _convert_mat_to_csv(sim, mat_path: str, model_name: str, results_dir: Path):
    """
    从 OMPython MAT 结果文件读时间序列，写成 simulation.csv。

    步骤：
      1. sim.getSolutions() 获取所有可用变量名（42 个左右）
      2. 过滤：排除参数常量（.C, .R, .alpha...）、导数（der(...)）、地电压
      3. 优先保留信号量：sensor.v, capacitor.p.v, capacitor.v
      4. 用 sim.sendExpression(readSimulationResult(...)) 读取时间序列
      5. 写 CSV（time + 信号量）
    """
    csv_path = str(results_dir / "simulation.csv")

    try:
        sols = sim.getSolutions()
        if not sols:
            logger.warning("getSolutions 返回空，跳过 MAT→CSV")
            return

        # ---- V3: 过滤变量 ----
        def _is_param_or_meta(col: str) -> bool:
            """参数常量或元数据列（值恒定，不是信号）"""
            suffixes = [".C", ".R", ".R_actual", ".L", ".LossPower", ".alpha",
                        ".T", ".T_ref", ".T_heatPort", ".offset", ".startTime",
                        ".signalSource.height", ".signalSource.offset", ".signalSource.y"]
            return any(col.endswith(s) for s in suffixes)

        signal_vars = []     # 信号电压 → 最高优先级
        other_signal_vars = []  # 信号电流 → 次要

        for v in sols:
            if v == "time" or "der(" in v:
                continue
            if _is_param_or_meta(v):
                continue
            if ".n.v" in v or ".n.i" in v or "ground" in v.lower():
                continue

            if "sensor.v" in v:                                    # 传感器输出 = 我们最关心的
                signal_vars.append(v)
            elif v.endswith(".p.v") or v.endswith(".v"):
                signal_vars.append(v)                              # 端电压
            elif v.endswith(".i"):
                other_signal_vars.append(v)                        # 电流（次要）

        vars_to_read = ["time"] + signal_vars[:4] + other_signal_vars[:2]

        # ---- 用 OMC API 读取时间序列 ----
        names_str = "{" + ",".join(vars_to_read) + "}"
        mat_forward = mat_path.replace("\\", "/")                  # 必须正斜杠！OMC 把 \t,\m 当转义
        cmd = f'readSimulationResult("{mat_forward}", {names_str})'
        raw = sim.sendExpression(cmd)

        if not raw or len(raw) < 2:
            logger.warning("readSimulationResult 返回为空")
            return

        # raw 是 tuple of tuples: (time_tuple, var1_tuple, var2_tuple, ...)
        time_vals = raw[0]
        n_points = len(time_vals)

        # ---- 写 CSV ----
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(vars_to_read)
            for i in range(n_points):
                row = []
                for j in range(len(vars_to_read)):
                    if j < len(raw) and i < len(raw[j]):
                        row.append(str(raw[j][i]))
                    else:
                        row.append("0")
                writer.writerow(row)

        logger.info("MAT→CSV 完成: %s (%d 变量 × %d 点)", csv_path, len(vars_to_read), n_points)

    except Exception as e:
        logger.warning("MAT→CSV 转换失败: %s", e)
        # Fallback: 尝试复制已有 CSV
        for pat in ["*.csv", f"{model_name}_res.csv"]:
            candidates = list(results_dir.glob(pat))
            if candidates and candidates[0] != Path(csv_path):
                import shutil
                shutil.copy2(str(candidates[0]), csv_path)
                logger.info("Fallback CSV from %s", candidates[0])
                return


# ====================================================================
# 辅助函数
# ====================================================================

def _extract_model_name(code: str) -> str | None:
    """正则提取 Modelica 模型名"""
    m = re.search(r"model\s+(\w+)", code)
    return m.group(1) if m else None


def _plot_csv(csv_path: str, plot_path: str, title: str):
    """读 CSV 画 matplotlib 曲线"""
    import matplotlib
    matplotlib.use("Agg")                                        # 无 GUI 后端
    import matplotlib.pyplot as plt

    with open(csv_path, "r") as f:
        rows = list(csv.reader(f))

    if len(rows) < 2:
        return

    header = rows[0]
    data = {col: [] for col in header}
    for row in rows[1:]:
        for i, col in enumerate(header):
            try:
                data[col].append(float(row[i]))
            except (ValueError, IndexError):
                pass

    time_col = header[0]
    plt.figure(figsize=(10, 5))
    for col in header[1:]:
        if data[col]:
            plt.plot(data[time_col][:len(data[col])], data[col], label=col)

    plt.xlabel(time_col)
    plt.ylabel("Value")
    plt.title(f"Simulation: {title}")
    plt.legend()
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(plot_path, dpi=150)
    plt.close()
