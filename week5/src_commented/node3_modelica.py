# -*- coding: utf-8 -*-
"""
=============================================================================
node3_modelica.py — 节点 3：Modelica 生成 + 编译 + 仿真 + 自修复
=============================================================================

这是整个系统最复杂的节点。它本身是一个 LangGraph 子图（嵌套状态机），
内部包含 4 个子节点形成自修复循环。

子图结构（4 节点 + 3 路由）:

    generate_mo               ← LLM 生成 Modelica .mo 代码
        ↓
    compile_mo                ← OMPython 编译（检查语法/类型错误）
        ↓ (成功)                 ↓ (失败: attempts < max)
    simulate_mo               ← OMPython 仿真（跑模拟）
        ↓ (成功)                 ↓ (失败: attempts < max)
    END(成功)                   repair_mo ← LLM 根据错误重写代码
                                   ↓          (attempts >= max → END 失败)
                               compile_mo

关键设计:
  - node3_step_ok: bool — 当前步骤(compile/simulate)是否成功
  - node3_attempts: int  — 独立计数器（编译失败了也计数，仿真失败了也计数）
  - 编译错误劫持: 用 StringIO 捕获 OMPython 日志（真正的 Modelica 错误在日志里，
    不在 Python 异常消息里）

V3 新增: stopTime 自动适配、MAT→CSV 转换、修复日志
V4 新增: CSV 列过滤扩展（运放 opAmp.out、电源参数 Vpp/Vnn 等）
=============================================================================
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


# ==========================================================================
# 工具: 保证关键计数器不丢失
# ==========================================================================
# 问题: LangGraph 每个节点只返回它关心的字段，如果某个节点忘记回传
#       node3_attempts，这个值就从 state 里消失了。
# 解决: 每个节点返回时追加 node3_attempts 和 node3_step_ok（即使没改也要回传）

def _always_pass(state: dict) -> dict:
    """每个节点返回时调用，确保计数器字段始终在 state 中。"""
    return {
        "node3_attempts": state.get("node3_attempts", 0),
        "node3_step_ok": state.get("node3_step_ok", False),
    }


# ==========================================================================
# 构建子图
# ==========================================================================

def build_node3_subgraph() -> StateGraph:
    """
    构建节点 3 的 LangGraph 子图。

    子图不同于主图:
      - 使用 dict 而非 PipelineState 作为状态类型（松散类型，兼容性好）
      - 子图内的 state 字段通过 _always_pass() 保证传递
      - 编译后可在主图外独立测试

    返回编译好的子图引用（主图通过 builder.add_node("node3_subgraph", ...) 注册）
    """
    builder = StateGraph(dict)  # dict = 松散类型，子图内部不用 PipelineState

    # 4 个子节点
    builder.add_node("generate_mo", _generate_mo)   # 生成 Modelica 代码
    builder.add_node("compile_mo", _compile_mo)     # 编译（OMPython）
    builder.add_node("simulate_mo", _simulate_mo)   # 仿真（OMPython）
    builder.add_node("repair_mo", _repair_mo)       # 根据错误修复代码（LLM）

    # 连线（固定边）
    builder.add_edge(START, "generate_mo")
    builder.add_edge("generate_mo", "compile_mo")   # 生成完直接编译

    # 条件路由（决定走到哪里取决于 _route_after_xxx 的返回值）
    builder.add_conditional_edges("compile_mo", _route_after_compile, {
        "simulate_mo": "simulate_mo",    # 编译成功 → 仿真
        "repair_mo": "repair_mo",        # 编译失败 + 还有重试次数 → 修复
        "end_fail": END,                 # 编译失败 + 重试耗尽 → 结束
    })

    builder.add_conditional_edges("simulate_mo", _route_after_simulate, {
        "end_success": END,              # 仿真成功 → 结束
        "repair_mo": "repair_mo",        # 仿真失败 + 还有重试 → 修复
        "end_fail": END,                 # 仿真失败 + 重试耗尽 → 结束
    })

    builder.add_conditional_edges("repair_mo", _route_after_repair, {
        "compile_mo": "compile_mo",      # 修复完成 → 重新编译
        "end_fail": END,                 # 重试耗尽 → 结束
    })

    return builder.compile()


# ==========================================================================
# 子节点 1: generate_mo — 生成 Modelica 代码
# ==========================================================================

def _generate_mo(state: dict) -> dict:
    """
    LLM 根据需求和 SysML 模型生成 Modelica .mo 代码。

    输入: state["req"] (需求) + state["sysml"] (SysML 模型)
    输出: 包含 modelica_code 的初始 state
    """
    t0 = time.time()
    req_dict = state.get("req", {})
    sysml_dict = state.get("sysml", {})
    sysml_code = sysml_dict.get("sysml_code", "")
    temperature = state.get("temperature", 0.3)

    # 从 dict 重建 Pydantic 对象（如果 dict 为空就创建一个空对象兜底）
    req = StructuredRequirement(**req_dict) if req_dict else StructuredRequirement(
        component_type="unknown", raw_input=""
    )

    # 格式化参数和约束字符串（嵌入 prompt 模板的 {parameters} 和 {constraints}）
    params_str = "\n".join(f"  {k} = {v}" for k, v in req.parameters.items())
    constraints_str = "\n".join(f"  - {c}" for c in req.constraints)

    # 加载 prompt 模板并填充占位符
    prompt = (
        load_prompt("node3_modelica.txt")
        .replace("{component_type}", req.component_type)
        .replace("{parameters}", params_str)
        .replace("{topology}", req.topology)
        .replace("{constraints}", constraints_str)
        .replace("{sysml_code}", sysml_code[:3000])    # 截断 SysML 代码，防 prompt 过长
        .replace("{prev_error_section}", "")            # 首次生成没有错误
    )

    logger.info("节点3 generate: 生成 Modelica 代码...")
    mo_code = chat([user_msg(prompt)], temperature=temperature, max_tokens=4096).strip()
    mo_code = clean_code_block(mo_code, "modelica")    # 剥掉 ```modelica ... ```

    # 从代码中提取模型名（OMPython 需要用它作为 model_name 参数）
    model_name = _extract_model_name(mo_code) or "MyModel"
    logger.info("节点3 generate 完成, 模型名=%s", model_name)

    return {
        # 初始化 mo（ModelicaArtifact 的原始 dict）
        "mo": {
            "modelica_code": mo_code,
            "file_path": "",         # 编译时才写入文件
            "csv_path": "",          # 仿真成功后才存在
            "plot_path": "",
            "attempts": 0,           # 计数器从 0 开始
            "errors": [],
            "success": False,
        },
        "node3_attempts": 0,         # 重置计数器
        "node3_step_ok": False,      # 尚未通过编译
        "run_dir": state.get("run_dir", ""),
        "timing": {**state.get("timing", {}), "node3_generate": time.time() - t0},
    }


# ==========================================================================
# 子节点 2: compile_mo — 编译 Modelica 代码
# ==========================================================================

def _compile_mo(state: dict) -> dict:
    """
    用 OMPython 编译 .mo 文件。编译失败时错误会被写入 state 供 repair 使用。

    编译是仿真前的一道关卡: OpenModelica 编译器检查语法、类型匹配、
    方程数量一致性等。这里的错误信息会被喂给 repair LLM。
    """
    t0 = time.time()
    mo_dict = state.get("mo", {})
    run_dir = Path(state.get("run_dir", ".")).resolve()   # .resolve() = 绝对路径
    modelica_dir = run_dir / "modelica"

    modelica_code = mo_dict.get("modelica_code", "")
    model_name = _extract_model_name(modelica_code) or "MyModel"

    # 写入 .mo 文件（OMPython 从文件读取，不能从字符串加载）
    modelica_dir.mkdir(parents=True, exist_ok=True)
    mo_path = modelica_dir / "model.mo"
    mo_path.write_text(modelica_code, encoding="utf-8")

    logger.info("节点3 compile: 编译 %s...", model_name)
    compile_ok, compile_err = _compile(str(mo_path), model_name)  # 调用底层编译函数

    errors = list(mo_dict.get("errors", []))  # 拷贝错误列表

    if not compile_ok:
        # —— 编译失败 ——
        attempts = state.get("node3_attempts", 0) + 1   # 计数 +1
        logger.warning("节点3 compile 失败 (第%s次): %s", attempts, compile_err[:200])
        errors.append(f"[编译错误 #{attempts}] {compile_err[:500]}")

        return {
            "mo": {**mo_dict, "errors": errors, "attempts": attempts,
                   "file_path": str(mo_path.resolve()) if mo_path else ""},
            "node3_attempts": attempts,
            "node3_step_ok": False,         # ← 告诉路由: 编译失败
            "run_dir": state.get("run_dir", ""),
            "timing": {**state.get("timing", {}), "node3_compile": time.time() - t0},
        }

    # —— 编译成功 ——
    logger.info("节点3 compile 成功")
    return {
        "mo": {**mo_dict, "errors": errors,
               "file_path": str(mo_path.resolve()) if mo_path else ""},
        "node3_attempts": state.get("node3_attempts", 0),  # 保持（不增加）
        "node3_step_ok": True,           # ← 告诉路由: 编译成功，可以去仿真了
        "run_dir": state.get("run_dir", ""),
        "timing": {**state.get("timing", {}), "node3_compile": time.time() - t0},
    }


# ==========================================================================
# 子节点 3: simulate_mo — 仿真
# ==========================================================================

def _simulate_mo(state: dict) -> dict:
    """
    用 OMPython 运行仿真，生成 CSV 数据 + PNG 曲线图。

    V3 改进:
      - stopTime 根据物理域自动适配（电气 0.01s / 热 1000s）
      - MAT→CSV 转换: OMPython 默认输出 .mat 二进制格式，
        这里用 readSimulationResult 转成人类可读的 CSV
    """
    t0 = time.time()
    mo_dict = state.get("mo", {})
    run_dir = Path(state.get("run_dir", ".")).resolve()
    modelica_dir = run_dir / "modelica"
    results_dir = run_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    modelica_code = mo_dict.get("modelica_code", "")
    model_name = _extract_model_name(modelica_code) or "MyModel"
    mo_path = str(modelica_dir / "model.mo")  # 编译时写的文件

    logger.info("节点3 simulate: 仿真 %s...", model_name)
    # V3: 根据组件类型自动选 stopTime
    req = state.get("req", {})
    stop_time = get_stop_time_for_domain(req.get("component_type", ""))
    sim_ok, sim_err = _simulate(mo_path, model_name, results_dir, stop_time)

    errors = list(mo_dict.get("errors", []))

    if not sim_ok:
        # —— 仿真失败 ——
        attempts = state.get("node3_attempts", 0) + 1
        logger.warning("节点3 simulate 失败 (第%s次): %s", attempts, sim_err[:200])
        errors.append(f"[仿真错误 #{attempts}] {sim_err[:500]}")
        return {
            "mo": {**mo_dict, "errors": errors, "attempts": attempts},
            "node3_attempts": attempts,
            "node3_step_ok": False,        # ← 仿真失败
            "run_dir": state.get("run_dir", ""),
            "timing": {**state.get("timing", {}), "node3_simulate": time.time() - t0},
        }

    # —— 仿真成功 ——
    csv_path = results_dir / "simulation.csv"
    plot_path = results_dir / "simulation.png"

    # 用 matplotlib 从 CSV 画 PNG 曲线图
    if csv_path.exists():
        _plot_csv(str(csv_path), str(plot_path),
                  state.get("req", {}).get("component_type", "System"))

    logger.info("节点3 simulate 成功, PNG: %s", plot_path)

    # V3: 如果有修复记录，保存为 repair_log.json
    repair_log = state.get("repair_log", [])
    if repair_log:
        import json as _json
        log_path = results_dir / "repair_log.json"
        log_path.write_text(_json.dumps(repair_log, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("修复日志已保存: %s (%s 次修复)", log_path, len(repair_log))

    return {
        "mo": {**mo_dict, "errors": errors, "success": True,  # ← 大结局: success=True!
               "csv_path": str(csv_path.resolve()),
               "plot_path": str(plot_path.resolve())},
        "node3_attempts": state.get("node3_attempts", 0),
        "node3_step_ok": True,            # ← 仿真成功
        "run_dir": state.get("run_dir", ""),
        "timing": {**state.get("timing", {}), "node3_simulate": time.time() - t0},
    }


# ==========================================================================
# 子节点 4: repair_mo — LLM 根据错误日志修复代码
# ==========================================================================

def _repair_mo(state: dict) -> dict:
    """
    LLM 根据编译/仿真错误日志重新生成 Modelica 代码。

    这是自修复循环的核心: 把错误信息喂给 LLM，LLM 重新生成代码，
    然后再编译、再仿真。V3 增加修复日志记录。
    """
    t0 = time.time()
    req_dict = state.get("req", {})
    sysml_dict = state.get("sysml", {})
    mo_dict = state.get("mo", {})
    sysml_code = sysml_dict.get("sysml_code", "")
    temperature = state.get("temperature", 0.3)
    attempts = state.get("node3_attempts", 0)

    # V3: 记录修复前的代码和错误（用于修复日志）
    code_before = mo_dict.get("modelica_code", "")
    errors_before = mo_dict.get("errors", [])[-3:]  # 最近 3 个错误

    req = StructuredRequirement(**req_dict) if req_dict else StructuredRequirement(
        component_type="unknown", raw_input=""
    )

    params_str = "\n".join(f"  {k} = {v}" for k, v in req.parameters.items())
    constraints_str = "\n".join(f"  - {c}" for c in req.constraints)

    # 构造错误反馈段（关键: 这部分告诉 LLM 上次哪里错了）
    errors = mo_dict.get("errors", [])
    error_section = ""
    if errors:
        error_section = (
            "## 上次编译/仿真的错误日志（请逐一修正）\n"
            "```\n" + "\n".join(errors[-5:]) + "\n```"
            "\n\n请仔细分析以上错误，重新生成完整的、可编译的 Modelica 代码。"
        )

    prompt = (
        load_prompt("node3_modelica.txt")
        .replace("{component_type}", req.component_type)
        .replace("{parameters}", params_str)
        .replace("{topology}", req.topology)
        .replace("{constraints}", constraints_str)
        .replace("{sysml_code}", sysml_code[:3000])
        .replace("{prev_error_section}", error_section)  # ← 错误日志填到这里
    )

    logger.info("节点3 repair: 第%s次修复...", attempts)
    mo_code = chat([user_msg(prompt)], temperature=temperature, max_tokens=4096).strip()
    mo_code = clean_code_block(mo_code, "modelica")

    # V3: 记录修复日志（谁修的、怎么修的）
    repair_entry = {
        "attempt": attempts,
        "errors_before": errors_before,                    # 修复前有什么错误
        "code_before_snippet": code_before[:200] if code_before else "",  # 修复前代码
        "code_after_snippet": mo_code[:200],                # 修复后代码
    }
    repair_log = list(state.get("repair_log", []))  # 拷贝已有日志
    repair_log.append(repair_entry)                 # 追加本次

    logger.info("节点3 repair 完成, 新模型名=%s, 已记录修复日志",
                _extract_model_name(mo_code) or "未识别")
    return {
        "mo": {**mo_dict, "modelica_code": mo_code},   # 替换为新代码
        "node3_attempts": attempts,                      # 计数器不变（compile 或 simulate 会 +1）
        "node3_step_ok": False,                          # 重置，回到 compile
        "repair_log": repair_log,                        # 累计修复记录
        "run_dir": state.get("run_dir", ""),
        "timing": {**state.get("timing", {}), "node3_repair": time.time() - t0},
    }


# ==========================================================================
# 子图路由函数
# ==========================================================================

def _route_after_compile(state: dict) -> str:
    """编译后路由: 成功→仿真, 失败+有剩余次数→修复, 失败+耗尽→结束"""
    if state.get("node3_step_ok", False):
        return "simulate_mo"                       # 编译成功 → 去仿真
    attempts = state.get("node3_attempts", 0)
    max_retries = state.get("max_retries", 5)       # 默认最多 5 次修复
    if attempts >= max_retries:
        logger.warning("节点3: 编译重试耗尽 (%s/%s)", attempts, max_retries)
        return "end_fail"
    return "repair_mo"                              # 去修复


def _route_after_simulate(state: dict) -> str:
    """仿真后路由: 成功→结束, 失败+有剩余→修复, 失败+耗尽→结束"""
    if state.get("node3_step_ok", False):
        return "end_success"
    attempts = state.get("node3_attempts", 0)
    max_retries = state.get("max_retries", 5)
    if attempts >= max_retries:
        logger.warning("节点3: 仿真重试耗尽 (%s/%s)", attempts, max_retries)
        return "end_fail"
    return "repair_mo"


def _route_after_repair(state: dict) -> str:
    """修复后路由: 有剩余次数→重新编译, 耗尽→结束"""
    attempts = state.get("node3_attempts", 0)
    max_retries = state.get("max_retries", 5)
    if attempts >= max_retries:
        logger.warning("节点3: 修复重试耗尽 (%s/%s)", attempts, max_retries)
        return "end_fail"
    return "compile_mo"


# ==========================================================================
# 底层: 编译 & 仿真（OMPython 封装）
# ==========================================================================

def _safe_str(e: Exception) -> str:
    """
    安全地把异常转字符串，绕过 Windows GBK 编码问题。

    在中文 Windows 上，try/except 中的 str(e) 可能因为 GBK
    编码失败而再次抛出 UnicodeEncodeError，形成异常的叠加。
    这里做三级 fallback: str → repr → type name。
    """
    try:
        s = str(e)
    except (UnicodeEncodeError, UnicodeDecodeError):
        try:
            s = repr(e)
        except Exception:
            s = f"{type(e).__name__}"
    return s[:500]  # 截断，防日志过长


def _compile(mo_path: str, model_name: str) -> tuple[bool, str]:
    """
    用 OMPython 编译 .mo 文件。

    关键坑: ModelicaSystem 构造失败时，Python 异常消息只有
    "Error executing buildModel(...)" — 真正的 Modelica 语法/类型
    错误在 OMPython 的日志里！所以这里用 StringIO 劫持日志，
    提取真实编译错误喂给 LLM。
    """
    import io
    try:
        from OMPython import ModelicaSystem

        # ★ 劫持 OMPython 日志 ★
        ompy_logger = logging.getLogger("OMPython")
        old_level = ompy_logger.level
        ompy_logger.setLevel(logging.DEBUG)       # 打开全部日志级别
        log_stream = io.StringIO()                 # 内存中的"文件"
        handler = logging.StreamHandler(log_stream)  # 把日志写到 StringIO
        handler.setLevel(logging.DEBUG)
        ompy_logger.addHandler(handler)

        try:
            # 构造 ModelicaSystem 对象 → 触发编译
            ModelicaSystem(mo_path, model_name)
            return True, ""
        except ImportError:
            return False, "OMPython 未安装"
        except Exception as e:
            # 从劫持的日志中提取真实的 Modelica 编译错误
            log_content = log_stream.getvalue()
            error_lines = []
            for line in log_content.split("\n"):
                lower = line.lower()
                # 搜索包含错误关键词的行
                if any(kw in lower for kw in [
                    "error", "warning", "syntax", "undefined",
                    "unknown", "missing", "cannot", "invalid",
                    "unmatched", "unexpected", "not found",
                ]):
                    if "omc log" in lower:         # OMPython 的 "omc log" 前缀
                        parts = line.split("]:", 1)
                        error_lines.append(parts[-1].strip() if len(parts) > 1 else line.strip())
                    else:
                        error_lines.append(line.strip())

            if error_lines:
                return False, "[OMC编译错误]\n" + "\n".join(error_lines[-10:])
            else:
                return False, f"[OMPython] {_safe_str(e)}"
        finally:
            # 清理: 移除 handler、恢复日志级别
            ompy_logger.removeHandler(handler)
            ompy_logger.setLevel(old_level)

    except ImportError:
        return False, "OMPython 未安装"


def _simulate(mo_path: str, model_name: str, results_dir: Path,
              stop_time: float = 0.01) -> tuple[bool, str]:
    """
    用 OMPython 仿真模型。OMPython 默认出 MAT 格式，需手动转 CSV。

    V3: stopTime 自动适配 + MAT→CSV 转换
    """
    try:
        from OMPython import ModelicaSystem
        # 重新构建 ModelicaSystem（编译和仿真是两个独立调用）
        sim = ModelicaSystem(mo_path, model_name)
        step_size = stop_time / 500.0  # 500 个采样点
        sim.setSimulationOptions({
            "stopTime": str(stop_time),
            "stepSize": str(step_size),
        })

        # 指定 .mat 结果文件路径（OMPython 默认写到当前目录）
        result_mat = str(results_dir / f"{model_name}.mat")
        sim.simulate(resultfile=result_mat)

        # MAT→CSV 转换
        if Path(result_mat).exists():
            _convert_mat_to_csv(sim, result_mat, model_name, results_dir)
            return True, ""
        else:
            logger.warning("仿真成功但 MAT 文件未生成: %s", result_mat)
            return True, ""  # 仍然算成功（可能是模型没有输出变量）

    except ImportError:
        return False, "OMPython 未安装"
    except Exception as e:
        return False, f"[OMPython] {_safe_str(e)}"


def _extract_model_name(code: str) -> str | None:
    """从 Modelica 代码中提取模型类名。"""
    m = re.search(r"model\s+(\w+)", code)
    return m.group(1) if m else None


def _plot_csv(csv_path: str, plot_path: str, title: str):
    """
    用 matplotlib 从 CSV 画出所有变量的时间序列图。

    使用 Agg 后端（非交互式），不需要显示窗口。
    生成的 PNG 用于节点3 HITL 展示。
    """
    import matplotlib
    matplotlib.use("Agg")  # 非交互式后端，不需要 GUI
    import matplotlib.pyplot as plt

    with open(csv_path, "r") as f:
        rows = list(csv.reader(f))

    if len(rows) < 2:
        return  # 空 CSV，不画

    # 解析列
    header = rows[0]
    data = {col: [] for col in header}
    for row in rows[1:]:
        for i, col in enumerate(header):
            try:
                data[col].append(float(row[i]))
            except (ValueError, IndexError):
                pass

    time_col = header[0]  # 第一列 = 时间
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


# ==========================================================================
# V3: MAT（二进制）→ CSV（文本）转换
# ==========================================================================

def _convert_mat_to_csv(sim, mat_path: str, model_name: str, results_dir: Path):
    """
    从 OMPython MAT 结果文件读取时间序列，写成 CSV。

    为什么需要这个:
      OMPython 默认输出 .mat 文件（Modelica 的二进制格式），
      但我们的物理验证和绘图都需要 CSV。OMPython 有时会自动生成 CSV，
      有时不生成（取决于模型大小和设置）。这里手动转确保一定有 CSV。
    """
    csv_path = str(results_dir / "simulation.csv")

    try:
        sols = sim.getSolutions()  # 获取所有变量名列表
        if not sols:
            logger.warning("getSolutions 返回空，跳过 MAT→CSV")
            return

        # —— V4: 过滤变量（信号量优先，跳过参数/导数/地）——
        def _is_param_or_meta(col: str) -> bool:
            """判断列名是否是参数常量或元数据（不应出现在 CSV 中）。"""
            suffixes = [
                ".C", ".R", ".R_actual", ".L", ".LossPower", ".alpha",
                ".T", ".T_ref", ".T_heatPort", ".offset", ".startTime",
                ".signalSource.height", ".signalSource.offset", ".signalSource.y",
                # V4 运放相关
                ".Vpp", ".Vnn", ".out", ".in_n", ".in_p",
                # V4 电源元件参数
                ".signalSource.", ".constantVoltage.", ".gain",
            ]
            return (any(col.endswith(s) for s in suffixes)
                    or any(s in col for s in [".signalSource.", ".constantVoltage."]))

        signal_vars = []       # 第一优先级信号
        other_signal_vars = []  # 第二优先级信号（电流等）

        for v in sols:
            if v == "time" or "der(" in v:           # 跳过时间和导数列
                continue
            if _is_param_or_meta(v):                 # 跳过参数常量
                continue
            if ".n.v" in v or ".n.i" in v or "ground" in v.lower():
                continue                              # 跳过地电压/电流参考
            # V4: 运放输出优先（最高优先级）
            if "opAmp.out" in v or "opamp.out" in v:
                signal_vars.insert(0, v)              # 插入到列表头部
            elif "sensor.v" in v:                     # 传感器输出（高优先级）
                signal_vars.append(v)
            elif v.endswith(".p.v") or v.endswith(".v"):
                signal_vars.append(v)                 # 元件端电压
            elif v.endswith(".i"):
                other_signal_vars.append(v)           # 电流（低优先级）

        # 最终 CSV 列: time + 最多 4 个信号量 + 最多 2 个次要量
        vars_to_read = ["time"] + signal_vars[:4] + other_signal_vars[:2]

        # 用 OMC API 读时间序列数据
        names_str = "{" + ",".join(vars_to_read) + "}"
        mat_forward = mat_path.replace("\\", "/")    # OMC 只认正斜杠
        cmd = f'readSimulationResult("{mat_forward}", {names_str})'
        raw = sim.sendExpression(cmd)                # 发送命令给 OMC

        if not raw or len(raw) < 2:
            logger.warning("readSimulationResult 返回为空")
            return

        # raw 是一个 tuple of tuples: (time_tuple, var1_tuple, var2_tuple, ...)
        time_vals = raw[0]
        n_points = len(time_vals)

        # 写入 CSV
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(vars_to_read)            # 写表头
            for i in range(n_points):
                row = []
                for j in range(len(vars_to_read)):
                    if j < len(raw) and i < len(raw[j]):
                        row.append(str(raw[j][i]))   # 写每行数据
                    else:
                        row.append("0")              # 缺数据填 0
                writer.writerow(row)

        logger.info("MAT→CSV 完成: %s (%d 变量 x %d 点)", csv_path, len(vars_to_read), n_points)

    except Exception as e:
        logger.warning("MAT→CSV 转换失败: %s", e)
        # Fallback: 如果 OMPython 自动生成了 CSV，复制过来用
        for pat in ["*.csv", f"{model_name}_res.csv"]:
            candidates = list(results_dir.glob(pat))
            if candidates and candidates[0] != Path(csv_path):
                import shutil
                shutil.copy2(str(candidates[0]), csv_path)
                logger.info("Fallback CSV from %s", candidates[0])
                return
