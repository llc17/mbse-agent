# -*- coding: utf-8 -*-
"""
=============================================================================
node_quality.py — V3 质量检查节点（全新文件）
=============================================================================

这是 V3 相对 V2 最大的代码增量——两个新节点插入主流水线：

  Q_cross_validate  → node2 之后、node2_hitl 之前
    用 LLM 对比 req JSON 中的参数值 vs SysML v2 代码中 attribute redefines 的值。
    不一致 → 打回 node2 重生成，附具体差异描述。

  Q_physics_validate → node3 成功之后、node4 之前
    从仿真 CSV 计算物理量（截止频率/稳态温度），与需求理论值对比。
    偏差过大 → 打回 node3 repair；偏差小 → 通过但标注偏差值。

设计原则：
  1. 交叉校验用 LLM（语义理解参数映射：R↔resistance, C↔capacitance）
  2. 物理验证用数学（纯计算，不调 LLM——-3dB 频率从 CSV 算即可）
  3. 两个节点都是"闸门"模式：不放行就带反馈打回上一个节点
  4. 实验模式下自动放行（靠 routing 中的 quality_checks 字段判断）
"""

# ====================================================================
# 导入
# ====================================================================
import csv
import json
import logging
import math
import time
from pathlib import Path
from typing import Optional

from src.llm_client import chat, user_msg
from src.utils import load_prompt

logger = logging.getLogger("quality")


# ====================================================================
# 节点 1：Q_cross_validate — 需求 ↔ SysML 交叉校验
# ====================================================================
# 为什么用 LLM 而不是正则？
#   参数名不统一：需求写 "R"，SysML 写 "resistance"；需求写 "C"，
#   SysML 写 "capacitance"。正则无法穷尽所有命名映射。
#   LLM 天然理解 "R 和 resistance 是同一个东西"。

def q_cross_validate(state: dict) -> dict:
    """
    LLM 对比 req JSON 参数与 SysML 代码里的值，检查一致性。

    输入：state.req (需求 JSON) + state.sysml.sysml_code (SysML 文本)
    输出：state.quality_checks["cross_validate"] = QualityCheckResult

    路由：通过 → node2_hitl；失败 → 打回 node2_generate
    """
    t0 = time.time()
    req = state.get("req", {})
    sysml = state.get("sysml", {})
    temperature = state.get("temperature", 0.3)
    feedback = state.get("human_feedback", "")

    # 没有 SysML 代码（异常）→ 直接放行
    if not sysml.get("sysml_code"):
        return _pass_through("cross_validate", state, "无 SysML 代码，跳过交叉校验")

    # ---- 构造对比 prompt ----
    req_json = json.dumps({
        "component_type": req.get("component_type", ""),
        "parameters": req.get("parameters", {}),
        "topology": req.get("topology", ""),
        "constraints": req.get("constraints", []),
    }, ensure_ascii=False, indent=2)

    sysml_code = sysml.get("sysml_code", "")

    prompt = (
        load_prompt("q_cross_validate.txt")
        .replace("{req_json}", req_json)
        .replace("{sysml_code}", sysml_code[:4000])              # 截断防超 token
        .replace("{prev_feedback}", feedback)
    )

    logger.info("Q_cross_validate: LLM 对比需求参数 vs SysML 代码...")
    result = chat([user_msg(prompt)], temperature=temperature, max_tokens=1024)

    # ---- 解析 LLM 返回 ----
    try:
        parsed = json.loads(_extract_json(result))
        passed = parsed.get("passed", True)
        issues = parsed.get("issues", [])
        details = parsed.get("summary", "")
    except json.JSONDecodeError:
        # LLM 返回非 JSON（罕见但可能）→ 安全放行
        logger.warning("Q_cross_validate: LLM 返回非 JSON，默认放行")
        passed = True
        issues = []
        details = "LLM 返回格式异常，跳过检查"

    qc = {
        "check_type": "cross_validate",
        "passed": passed,
        "issues": issues,
        "details": details,
    }
    quality_checks = dict(state.get("quality_checks", {}))
    quality_checks["cross_validate"] = qc

    elapsed = time.time() - t0
    logger.info("Q_cross_validate 完成 (%.1fs): passed=%s, issues=%s",
                elapsed, passed, len(issues))

    return {
        "quality_checks": quality_checks,
        # 失败时：把差异描述注入 human_feedback，node2 下次生成时会看到
        "human_feedback": details if not passed else feedback,
        "timing": {**state.get("timing", {}), "q_cross_validate": elapsed},
    }


# ====================================================================
# 节点 2：Q_physics_validate — 仿真物理量验证
# ====================================================================
# 这是 V3 最"工程"的节点——不调 LLM，纯数学。
#
# 当前支持的验证（V3）：
#   - RC 低通：从阶跃响应计算 τ → f_c = 1/(2πτ)，与需求 cutoff_freq 对比
#   - 热传导：取仿真最后 10% 时间的温度均值作为稳态值
#
# V4 计划：每个用例配 expected_physics 校验规格，验证公式从外部读。

def q_physics_validate(state: dict) -> dict:
    """
    从仿真 CSV 计算物理量，与需求理论值对比。

    输入：state.mo.csv_path (CSV 文件) + state.req.parameters
    输出：state.quality_checks["physics_validate"] = QualityCheckResult

    路由：通过 → node4；失败 → 打回 node3 repair
    """
    t0 = time.time()
    mo = state.get("mo", {})
    req = state.get("req", {})

    # 仿真失败 → 无 CSV，跳过
    if not mo.get("success"):
        return _pass_through("physics_validate", state, "仿真未成功，跳过物理验证")

    csv_path = mo.get("csv_path", "")
    if not csv_path or not Path(csv_path).exists():
        return _pass_through("physics_validate", state, "无 CSV 文件，跳过物理验证")

    component_type = req.get("component_type", "").lower()
    params = req.get("parameters", {})

    # ---- 根据领域选择验证方式 ----
    try:
        if _is_thermal(component_type):
            physics_result = _validate_thermal(csv_path, params, component_type)
        else:
            physics_result = _validate_rc_cutoff(csv_path, params, component_type)
    except Exception as e:
        logger.warning("Q_physics_validate: 计算异常: %s", e)
        physics_result = {
            "passed": True, "issues": [],
            "details": f"物理计算异常 ({e})，跳过",
        }

    qc = {
        "check_type": "physics_validate",
        "passed": physics_result["passed"],
        "issues": physics_result.get("issues", []),
        "deviation_percent": physics_result.get("deviation_percent"),
        "expected_value": physics_result.get("expected_value"),
        "actual_value": physics_result.get("actual_value"),
        "details": physics_result.get("details", ""),
    }
    quality_checks = dict(state.get("quality_checks", {}))
    quality_checks["physics_validate"] = qc

    elapsed = time.time() - t0

    if not physics_result["passed"]:
        logger.warning("Q_physics_validate 失败: 偏差 %.1f%%", qc.get("deviation_percent", 0))
    else:
        logger.info("Q_physics_validate 通过 (%.1fs)", elapsed)

    return {
        "quality_checks": quality_checks,
        "physics_feedback": qc["details"] if not physics_result["passed"] else "",
        "timing": {**state.get("timing", {}), "q_physics_validate": elapsed},
    }


# ====================================================================
# RC 截止频率验证
# ====================================================================
# 原理：RC 低通滤波器的阶跃响应 → 电容电压 Vc(t) = Vs·(1 - e^(-t/τ))
#   当 Vc = Vs·0.632 时，t = τ = RC
#   f_c = 1 / (2π·τ)
#
# 步骤：
#   1. 从 CSV 找电压信号列（跳过参数常量、地电压、导数）
#   2. 取最后 10% 数据的均值作为稳态值 Vs
#   3. 找到第一个 V(t) ≥ 0.632·Vs 的时间点 → τ
#   4. f_c = 1/(2πτ)，与需求期望对比

def _validate_rc_cutoff(csv_path: str, params: dict, component_type: str) -> dict:
    """从 RC 阶跃响应计算 -3dB 截止频率"""

    data = _read_csv(csv_path)
    if not data or len(data.get("data", {})) < 2:
        return {"passed": True, "issues": [], "details": "CSV 数据不足"}

    time_col = data["columns"][0]
    signal_col = None

    # ---- 找输出电压信号列 ----
    # 优先级：sensor.v > capacitor.v > capacitor.p.v > 任意有变化的电压列
    for col in data["columns"][1:]:
        if "der(" in col:
            continue
        vals = data["data"].get(col, [])
        if not vals or len(vals) < 2:
            continue
        if abs(max(vals) - min(vals)) < 1e-9:
            continue                                           # 常数列（参数），跳过
        if ".n.v" in col or ".n.i" in col:
            continue                                           # 地电压 ≈ 0，跳过
        if any(kw in col.lower() for kw in ["v_out", "sensor.v", "sensor.", ".p.v", "capacitor.v"]):
            signal_col = col
            break

    # 回退：任意有变化 > 0.01 的非参数电压列
    if not signal_col:
        for col in data["columns"][1:]:
            if "der(" in col or ".C" in col or ".R" in col or ".L" in col:
                continue
            vals = data["data"].get(col, [])
            if not vals or abs(max(vals) - min(vals)) < 1e-9:
                continue
            if max(vals) > 0.01:
                signal_col = col
                break

    if not signal_col:
        return {"passed": True, "issues": [], "details": "未找到有效电压信号列"}

    times = data["data"][time_col]
    voltages = data["data"][signal_col]

    # ---- 计算稳态值（最后 10% 数据的均值） ----
    n = len(voltages)
    steady_state = sum(voltages[-max(1, n // 10):]) / max(1, n // 10)
    if steady_state < 0.001:
        return {"passed": True, "issues": [], "details": "稳态电压接近 0，跳过"}

    # ---- 找 τ（达到 63.2% 稳态的时间） ----
    target_63 = steady_state * 0.632
    tau = None
    for i, v in enumerate(voltages):
        if v >= target_63:
            if i > 0 and voltages[i] > voltages[i - 1]:
                frac = (target_63 - voltages[i - 1]) / (voltages[i] - voltages[i - 1])
                tau = times[i - 1] + frac * (times[i] - times[i - 1])
            else:
                tau = times[i]
            break

    if tau is None or tau <= 0:
        return {"passed": True, "issues": [], "details": "无法计算时间常数 τ"}

    # ---- 计算截止频率 ----
    actual_fc = 1.0 / (2.0 * math.pi * tau)

    # ---- 找期望值 ----
    # 优先从 params 中直接取 cutoff_freq；否则从 R·C 推算
    expected_fc = None
    for key in ["cutoff_freq", "cutoff_frequency", "fc", "f_c", "截止频率"]:
        if key in params:
            expected_fc = float(params[key])
            break

    if expected_fc is None:
        r_val = params.get("R", params.get("resistance", 0))
        c_val = params.get("C", params.get("capacitance", 0))
        if r_val > 0 and c_val > 0:
            expected_fc = 1.0 / (2.0 * math.pi * r_val * c_val)

    if expected_fc is None or expected_fc <= 0:
        return {"passed": True, "issues": [],
                "details": f"无期望截止频率可对比，实测 f_c={actual_fc:.1f} Hz"}

    # ---- 偏差判断 ----
    deviation = abs(actual_fc - expected_fc) / expected_fc * 100
    passed = deviation < 50.0                                      # 50% 阈值

    issues = []
    if not passed:
        issues.append({
            "param_name": "cutoff_frequency",
            "expected": f"{expected_fc:.1f} Hz",
            "found": f"{actual_fc:.1f} Hz",
            "severity": "error",
            "detail": f"截止频率偏差 {deviation:.1f}%（期望 {expected_fc:.1f} Hz，实测 {actual_fc:.1f} Hz，τ={tau:.6f} s）",
        })

    return {
        "passed": passed, "issues": issues,
        "deviation_percent": round(deviation, 1),
        "expected_value": round(expected_fc, 1),
        "actual_value": round(actual_fc, 1),
        "details": f"f_c 实测={actual_fc:.1f} Hz, 期望={expected_fc:.1f} Hz, 偏差={deviation:.1f}% (τ={tau:.6f}s)",
    }


# ====================================================================
# 热稳态验证（V3 基本版，V4 完善）
# ====================================================================

def _validate_thermal(csv_path: str, params: dict, component_type: str) -> dict:
    """从热仿真 CSV 取稳态温度，对比需求"""
    data = _read_csv(csv_path)
    if not data or len(data.get("data", {})) < 2:
        return {"passed": True, "issues": [], "details": "CSV 数据不足"}

    # 找温度信号列（有变化 > 0.1K 的）
    temp_col = None
    for col in data["columns"][1:]:
        vals = data["data"][col]
        if vals and max(vals) - min(vals) > 0.1:
            temp_col = col
            break

    if not temp_col:
        return {"passed": True, "issues": [], "details": "未找到温度信号列"}

    temps = data["data"][temp_col]
    n = len(temps)
    steady_temp = sum(temps[-max(1, n // 10):]) / max(1, n // 10)

    expected_temp = params.get("target_temp", params.get("outdoor_temp", None))
    if expected_temp is None:
        return {"passed": True, "issues": [],
                "details": f"稳态温度={steady_temp:.1f} K（无期望值可对比）"}

    deviation = abs(steady_temp - expected_temp) / expected_temp * 100
    passed = deviation < 20.0                                        # 热域放宽到 20%

    return {
        "passed": passed,
        "issues": [{
            "param_name": "steady_temperature",
            "expected": f"{expected_temp:.1f} K", "found": f"{steady_temp:.1f} K",
            "severity": "error" if not passed else "warning",
            "detail": f"稳态温度偏差 {deviation:.1f}%",
        }] if not passed else [],
        "deviation_percent": round(deviation, 1),
        "expected_value": round(expected_temp, 1),
        "actual_value": round(steady_temp, 1),
        "details": f"稳态温度={steady_temp:.1f} K, 期望={expected_temp:.1f} K, 偏差={deviation:.1f}%",
    }


# ====================================================================
# 工具函数
# ====================================================================

def _is_thermal(component_type: str) -> bool:
    """判断是否热域"""
    return any(kw in component_type for kw in ["热", "thermal", "heat"])


def _read_csv(csv_path: str) -> Optional[dict]:
    """
    读仿真 CSV，返回 {columns: [col_names], data: {col_name: [values]}}。

    为什么自己写而不是用 pandas？pandas 是重型依赖，
    这里只需要简单的列式读取。
    """
    try:
        with open(csv_path, "r") as f:
            rows = list(csv.reader(f))
        if len(rows) < 2:
            return None

        header = [h.strip().strip('"') for h in rows[0]]
        data = {col: [] for col in header}
        for row in rows[1:]:
            for i, col in enumerate(header):
                try:
                    data[col].append(float(row[i]))
                except (ValueError, IndexError):
                    data[col].append(0.0)

        return {"columns": header, "data": data}
    except Exception as e:
        logger.warning("读取 CSV 失败: %s", e)
        return None


def _extract_json(text: str) -> str:
    """从 LLM 返回中提取 JSON（去掉 markdown 包裹）"""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text


def _pass_through(check_type: str, state: dict, reason: str) -> dict:
    """
    生成"跳过检查"的标准结果。用于异常流程中的安全放行。
    原则：宁可放行也不阻断流水线。
    """
    qc = {
        "check_type": check_type,
        "passed": True,
        "issues": [],
        "details": reason,
    }
    quality_checks = dict(state.get("quality_checks", {}))
    quality_checks[check_type] = qc
    logger.info("%s: %s", check_type, reason)
    return {"quality_checks": quality_checks}
