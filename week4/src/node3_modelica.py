"""
节点 3 — Modelica 生成 + 编译 + 仿真 + 自修复。V2 版：LangGraph 子图，max_retries=5。

子图结构:
  generate_mo → compile_mo → simulate_mo → END(成功)
                   ↓ 失败        ↓ 失败
                repair_mo ←─────────────┘
                   ↓
              (retries < 5 → compile_mo)
              (retries >= 5 → END 失败)

关键设计:
  - node3_step_ok: bool — 当前步骤(compile/simulate)是否成功，路由据此判断
  - node3_attempts: int — 独立计数器，靠 _always_pass() 模板确保每个节点都回传
  - 编译错误信息包含完整的 OMC 模型检查错误（不只是 Python 异常）
"""

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


# ============================================================
# 工具: 确保关键计数器不会因某个节点忘记回传而丢失
# ============================================================
def _always_pass(state: dict) -> dict:
    """每个节点返回时调用，确保计数器字段始终在 state 中。"""
    return {
        "node3_attempts": state.get("node3_attempts", 0),
        "node3_step_ok": state.get("node3_step_ok", False),
    }


# ============================================================
# 构建子图
# ============================================================
def build_node3_subgraph() -> StateGraph:
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


# ============================================================
# 节点函数
# ============================================================

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

    prompt = (
        load_prompt("node3_modelica.txt")
        .replace("{component_type}", req.component_type)
        .replace("{parameters}", params_str)
        .replace("{topology}", req.topology)
        .replace("{constraints}", constraints_str)
        .replace("{sysml_code}", sysml_code[:3000])
        .replace("{prev_error_section}", "")
    )

    logger.info("节点3 generate: 生成 Modelica 代码...")
    mo_code = chat([user_msg(prompt)], temperature=temperature, max_tokens=4096).strip()
    mo_code = clean_code_block(mo_code, "modelica")

    model_name = _extract_model_name(mo_code) or "MyModel"
    logger.info("节点3 generate 完成, 模型名=%s", model_name)

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
        "node3_attempts": 0,
        "node3_step_ok": False,
        "run_dir": state.get("run_dir", ""),
        "timing": {**state.get("timing", {}), "node3_generate": time.time() - t0},
    }


def _compile_mo(state: dict) -> dict:
    t0 = time.time()
    mo_dict = state.get("mo", {})
    run_dir = Path(state.get("run_dir", ".")).resolve()
    modelica_dir = run_dir / "modelica"

    modelica_code = mo_dict.get("modelica_code", "")
    model_name = _extract_model_name(modelica_code) or "MyModel"

    modelica_dir.mkdir(parents=True, exist_ok=True)
    mo_path = modelica_dir / "model.mo"
    mo_path.write_text(modelica_code, encoding="utf-8")

    logger.info("节点3 compile: 编译 %s...", model_name)
    compile_ok, compile_err = _compile(str(mo_path), model_name)

    errors = list(mo_dict.get("errors", []))

    if not compile_ok:
        attempts = state.get("node3_attempts", 0) + 1
        logger.warning("节点3 compile 失败 (第%s次): %s", attempts, compile_err[:200])
        errors.append(f"[编译错误 #{attempts}] {compile_err[:500]}")

        return {
            "mo": {**mo_dict, "errors": errors, "attempts": attempts, "file_path": str(mo_path.resolve()) if mo_path else ""},
            "node3_attempts": attempts,
            "node3_step_ok": False,                        # ← 关键: 告诉路由"这一步失败了"
            "run_dir": state.get("run_dir", ""),
            "timing": {**state.get("timing", {}), "node3_compile": time.time() - t0},
        }

    logger.info("节点3 compile 成功")
    return {
        "mo": {**mo_dict, "errors": errors, "file_path": str(mo_path.resolve()) if mo_path else ""},
        "node3_attempts": state.get("node3_attempts", 0),   # 保持计数器
        "node3_step_ok": True,                               # ← 关键: 告诉路由"这一步成功了"
        "run_dir": state.get("run_dir", ""),
        "timing": {**state.get("timing", {}), "node3_compile": time.time() - t0},
    }


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

    logger.info("节点3 simulate: 仿真 %s...", model_name)
    # V3: 根据 component_type 自动选 stopTime
    req = state.get("req", {})
    stop_time = get_stop_time_for_domain(req.get("component_type", ""))
    sim_ok, sim_err = _simulate(mo_path, model_name, results_dir, stop_time)

    errors = list(mo_dict.get("errors", []))

    if not sim_ok:
        attempts = state.get("node3_attempts", 0) + 1
        logger.warning("节点3 simulate 失败 (第%s次): %s", attempts, sim_err[:200])
        errors.append(f"[仿真错误 #{attempts}] {sim_err[:500]}")

        return {
            "mo": {**mo_dict, "errors": errors, "attempts": attempts},
            "node3_attempts": attempts,
            "node3_step_ok": False,                        # ← 失败
            "run_dir": state.get("run_dir", ""),
            "timing": {**state.get("timing", {}), "node3_simulate": time.time() - t0},
        }

    csv_path = results_dir / "simulation.csv"
    plot_path = results_dir / "simulation.png"

    if csv_path.exists():
        _plot_csv(str(csv_path), str(plot_path), state.get("req", {}).get("component_type", "System"))

    logger.info("节点3 simulate 成功, PNG: %s", plot_path)

    # V3: 保存修复日志到文件
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
        "node3_step_ok": True,                               # ← 成功
        "run_dir": state.get("run_dir", ""),
        "timing": {**state.get("timing", {}), "node3_simulate": time.time() - t0},
    }


def _repair_mo(state: dict) -> dict:
    """LLM 根据错误日志重新生成 Modelica 代码。不修改计数器。V3: 记录修复日志。"""
    t0 = time.time()
    req_dict = state.get("req", {})
    sysml_dict = state.get("sysml", {})
    mo_dict = state.get("mo", {})
    sysml_code = sysml_dict.get("sysml_code", "")
    temperature = state.get("temperature", 0.3)
    attempts = state.get("node3_attempts", 0)
    # V3: 记录修复前的代码和错误
    code_before = mo_dict.get("modelica_code", "")
    errors_before = mo_dict.get("errors", [])[-3:]  # 最近 3 个错误

    req = StructuredRequirement(**req_dict) if req_dict else StructuredRequirement(
        component_type="unknown", raw_input=""
    )

    params_str = "\n".join(f"  {k} = {v}" for k, v in req.parameters.items())
    constraints_str = "\n".join(f"  - {c}" for c in req.constraints)

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

    # V3: 记录修复日志
    repair_entry = {
        "attempt": attempts,
        "errors_before": errors_before,
        "code_before_snippet": code_before[:200] if code_before else "",
        "code_after_snippet": mo_code[:200],
    }
    repair_log = list(state.get("repair_log", []))
    repair_log.append(repair_entry)

    logger.info("节点3 repair 完成, 新模型名=%s, 已记录修复日志", _extract_model_name(mo_code) or "未识别")
    return {
        "mo": {**mo_dict, "modelica_code": mo_code},
        "node3_attempts": attempts,                         # 保持不变
        "node3_step_ok": False,                              # 重置
        "repair_log": repair_log,                            # V3: 累计修复记录
        "run_dir": state.get("run_dir", ""),
        "timing": {**state.get("timing", {}), "node3_repair": time.time() - t0},
    }


# ============================================================
# 路由 — 改查 node3_step_ok 而不是历史错误
# ============================================================

def _route_after_compile(state: dict) -> str:
    """编译后路由：检查当前步骤是否成功，而非历史错误。"""
    if state.get("node3_step_ok", False):
        return "simulate_mo"                                # 编译成功 → 去仿真

    attempts = state.get("node3_attempts", 0)
    max_retries = state.get("max_retries", 5)
    if attempts >= max_retries:
        logger.warning("节点3: 编译重试耗尽 (%s/%s)", attempts, max_retries)
        return "end_fail"

    return "repair_mo"


def _route_after_simulate(state: dict) -> str:
    """仿真后路由。"""
    if state.get("node3_step_ok", False):
        return "end_success"

    attempts = state.get("node3_attempts", 0)
    max_retries = state.get("max_retries", 5)
    if attempts >= max_retries:
        logger.warning("节点3: 仿真重试耗尽 (%s/%s)", attempts, max_retries)
        return "end_fail"

    return "repair_mo"


def _route_after_repair(state: dict) -> str:
    attempts = state.get("node3_attempts", 0)
    max_retries = state.get("max_retries", 5)
    if attempts >= max_retries:
        logger.warning("节点3: 修复重试耗尽 (%s/%s)", attempts, max_retries)
        return "end_fail"

    return "compile_mo"


# ============================================================
# 编译 & 仿真
# ============================================================

def _safe_str(e: Exception) -> str:
    """安全地把异常转字符串，绕过 Windows GBK 编码问题。"""
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
    编译 .mo 文件。捕获 OMPython 日志获取真实编译错误。

    关键坑: ModelicaSystem 构造失败时，Python 异常只返回
    "Error executing buildModel(...)" —— 真正的 Modelica 语法/类型错误
    在 OMPython 的日志里，不在异常消息里。
    这里用 StringIO 把日志劫持下来，提取真实错误喂给 LLM。
    """
    import io
    try:
        from OMPython import ModelicaSystem

        # 劫持 OMPython 日志 → 抓真实编译错误
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
            # 从 OMPython 日志提取真正的编译错误
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


def _simulate(mo_path: str, model_name: str, results_dir: Path, stop_time: float = 0.01) -> tuple[bool, str]:
    """
    仿真模型。OMPython 默认出 MAT 格式，需手动转 CSV。
    V3: stopTime 自动适配 + MAT→CSV 转换。
    """
    try:
        from OMPython import ModelicaSystem
        sim = ModelicaSystem(mo_path, model_name)
        step_size = stop_time / 500.0
        sim.setSimulationOptions({
            "stopTime": str(stop_time),
            "stepSize": str(step_size),
        })

        # 显式 resultfile（.mat 后缀确保 getSolutions 能找到）
        result_mat = str(results_dir / f"{model_name}.mat")
        sim.simulate(resultfile=result_mat)

        # MAT→CSV: 用 getSolutions() 拿变量名，再从 MAT 读时间序列
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


def _extract_model_name(code: str) -> str | None:
    m = re.search(r"model\s+(\w+)", code)
    return m.group(1) if m else None


def _plot_csv(csv_path: str, plot_path: str, title: str):
    import matplotlib
    matplotlib.use("Agg")
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


# ============================================================
# V3: MAT → CSV 转换
# ============================================================

def _convert_mat_to_csv(sim, mat_path: str, model_name: str, results_dir: Path):
    """从 OMPython MAT 结果文件读时间序列，写成 CSV。V3: 优先选输出信号量。"""
    csv_path = str(results_dir / "simulation.csv")

    try:
        sols = sim.getSolutions()
        if not sols:
            logger.warning("getSolutions 返回空，跳过 MAT→CSV")
            return

        # V3: 过滤要输出到 CSV 的变量：信号量优先、排除参数/导数/地
        def _is_param_or_meta(col: str) -> bool:
            suffixes = [".C", ".R", ".R_actual", ".L", ".LossPower", ".alpha",
                        ".T", ".T_ref", ".T_heatPort", ".offset", ".startTime",
                        ".signalSource.height", ".signalSource.offset", ".signalSource.y"]
            return any(col.endswith(s) for s in suffixes)

        signal_vars = []
        other_signal_vars = []
        for v in sols:
            if v == "time" or "der(" in v:
                continue
            if _is_param_or_meta(v):
                continue
            if ".n.v" in v or ".n.i" in v or "ground" in v.lower():
                continue
            if "sensor.v" in v:                                      # 传感器输出 → 最高优先
                signal_vars.append(v)
            elif v.endswith(".p.v") or v.endswith(".v"):
                signal_vars.append(v)                                # 元件端电压
            elif v.endswith(".i"):
                other_signal_vars.append(v)                          # 电流 → 次要

        vars_to_read = ["time"] + signal_vars[:4] + other_signal_vars[:2]

        # 用 OMC API 读取时间序列
        names_str = "{" + ",".join(vars_to_read) + "}"
        # 必须用正斜杠，否则 OMC 把 \t, \m 当转义符
        mat_forward = mat_path.replace("\\", "/")
        cmd = f'readSimulationResult("{mat_forward}", {names_str})'
        raw = sim.sendExpression(cmd)

        if not raw or len(raw) < 2:
            logger.warning("readSimulationResult 返回为空")
            return

        # raw 是 tuple of tuples: (time_tuple, var1_tuple, var2_tuple, ...)
        time_vals = raw[0]
        n_points = len(time_vals)

        # 写 CSV
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
        # Fallback: 复制已有 CSV
        for pat in ["*.csv", f"{model_name}_res.csv"]:
            candidates = list(results_dir.glob(pat))
            if candidates and candidates[0] != Path(csv_path):
                import shutil
                shutil.copy2(str(candidates[0]), csv_path)
                logger.info("Fallback CSV from %s", candidates[0])
                return
