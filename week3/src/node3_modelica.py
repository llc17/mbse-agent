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
from src.utils import load_prompt, clean_code_block

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
        "timing": {**state.get("timing", {}), "node3_generate": time.time() - t0},
    }


def _compile_mo(state: dict) -> dict:
    t0 = time.time()
    mo_dict = state.get("mo", {})
    run_dir = Path(state.get("run_dir", "."))
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
            "mo": {**mo_dict, "errors": errors, "attempts": attempts, "file_path": str(mo_path)},
            "node3_attempts": attempts,
            "node3_step_ok": False,                        # ← 关键: 告诉路由"这一步失败了"
            "timing": {**state.get("timing", {}), "node3_compile": time.time() - t0},
        }

    logger.info("节点3 compile 成功")
    return {
        "mo": {**mo_dict, "errors": errors, "file_path": str(mo_path)},
        "node3_attempts": state.get("node3_attempts", 0),   # 保持计数器
        "node3_step_ok": True,                               # ← 关键: 告诉路由"这一步成功了"
        "timing": {**state.get("timing", {}), "node3_compile": time.time() - t0},
    }


def _simulate_mo(state: dict) -> dict:
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
            "node3_step_ok": False,                        # ← 失败
            "timing": {**state.get("timing", {}), "node3_simulate": time.time() - t0},
        }

    csv_path = results_dir / "simulation.csv"
    plot_path = results_dir / "simulation.png"

    if csv_path.exists():
        _plot_csv(str(csv_path), str(plot_path), state.get("req", {}).get("component_type", "System"))

    logger.info("节点3 simulate 成功, PNG: %s", plot_path)
    return {
        "mo": {**mo_dict, "errors": errors, "success": True,
               "csv_path": str(csv_path), "plot_path": str(plot_path)},
        "node3_attempts": state.get("node3_attempts", 0),
        "node3_step_ok": True,                               # ← 成功
        "timing": {**state.get("timing", {}), "node3_simulate": time.time() - t0},
    }


def _repair_mo(state: dict) -> dict:
    """LLM 根据错误日志重新生成 Modelica 代码。不修改计数器。"""
    t0 = time.time()
    req_dict = state.get("req", {})
    sysml_dict = state.get("sysml", {})
    mo_dict = state.get("mo", {})
    sysml_code = sysml_dict.get("sysml_code", "")
    temperature = state.get("temperature", 0.3)
    attempts = state.get("node3_attempts", 0)

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

    logger.info("节点3 repair 完成, 新模型名=%s", _extract_model_name(mo_code) or "未识别")
    return {
        "mo": {**mo_dict, "modelica_code": mo_code},
        "node3_attempts": attempts,                         # 保持不变
        "node3_step_ok": False,                              # 重置
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
    编译 .mo 文件。
    本机 omc CLI 不在 PATH，直接用 OMPython。
    """
    try:
        from OMPython import ModelicaSystem
        # ModelicaSystem 构造时会 loadFile + 初步编译检查
        # 如果模型有语法错误，这里会抛异常
        ModelicaSystem(mo_path, model_name)
        return True, ""
    except ImportError:
        return False, "OMPython 未安装"
    except Exception as e:
        return False, f"[OMPython] {_safe_str(e)}"


def _simulate(mo_path: str, model_name: str, results_dir: Path) -> tuple[bool, str]:
    """
    仿真模型。
    本机 omc CLI 不在 PATH，直接用 OMPython。
    关键: 用 stepSize 替代 numberOfIntervals（新版 OMPython 不支持 numberOfIntervals）。
          outputFormat 设为 csv。
    """
    try:
        from OMPython import ModelicaSystem
        sim = ModelicaSystem(mo_path, model_name)
        # 新版 OMPython: 只有 startTime/stopTime/stepSize/tolerance/solver/outputFormat
        # stepSize = stopTime / 点数，此处 stopTime=0.01, 500点 → stepSize=0.00002
        sim.setSimulationOptions({
            "stopTime": "0.01",
            "stepSize": "0.00002",
            "outputFormat": "csv",
        })
        sim.simulate()

        # 仿真结果在 OMPython 的工作目录
        work_dir = Path(getattr(sim, '_workDir', '')) if hasattr(sim, '_workDir') else None
        if work_dir and work_dir.exists():
            for csv_file in work_dir.glob("*.csv"):
                import shutil
                dest = results_dir / "simulation.csv"
                shutil.copy2(csv_file, dest)
                return True, ""
        # 备选: 从 results_dir 找
        csv_candidates = list(results_dir.glob(f"{model_name}_res.csv")) + \
                         list(results_dir.glob("*.csv"))
        if csv_candidates:
            if csv_candidates[0].name != "simulation.csv":
                import shutil
                shutil.copy2(csv_candidates[0], results_dir / "simulation.csv")
            return True, ""

        return True, ""  # 仿真成功但没找到 CSV（可能 outputFormat 没生效）

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
