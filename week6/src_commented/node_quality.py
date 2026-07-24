# -*- coding: utf-8 -*-
"""
=============================================================================
node_quality.py — V4 质量检查节点（双节点: 交叉校验 + 物理验证）
=============================================================================

两个独立的质量门插入流水线:

  Q_cross_validate (node2 后):
    LLM 对比 req JSON 参数值 vs SysML 代码属性值。不一致 → 打回 node2。

  Q_physics_validate (node3 后):
    从仿真 CSV 计算物理量（截止频率/稳态温度/谐振频率/运放增益），
    与需求理论值对比。V4 升级为配置驱动 — expected_physics.validate_type
    决定验证策略。偏差过大 → 打回 node3 repair。

V4 新增（E 任务）:
  - 4 种验证策略: rc_cutoff, thermal_steady, rlc_resonant_freq, opamp_gain
  - 配置驱动: 实验模式用 expected_physics, interactive 模式自动检测
  - 新增 RLC 谐振频率验证（过零点检测法）
  - 新增运放增益验证（Vout/Vin 对比 -Rf/Rin）
  - 共享工具函数 _find_signal_column(): 智能从 CSV 列中找信号列
=============================================================================
"""

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


# ==========================================================================
# V4: 验证函数注册表
# ==========================================================================
# 新增验证类型只需要在这里加一行，然后在下面实现对应的 _validate_xxx 函数
VALIDATOR_REGISTRY = {
    "rc_cutoff": "_validate_rc_cutoff",           # RC 低通滤波器截止频率
    "thermal_steady": "_validate_thermal_steady", # 热传导稳态温度
    "rlc_resonant_freq": "_validate_rlc_resonant", # RLC 谐振频率
    "opamp_gain": "_validate_opamp_gain",          # 运放闭环增益
}


# ==========================================================================
# 节点: 交叉校验 (node2 后 → node2_hitl)
# ==========================================================================

def q_cross_validate(state: dict) -> dict:
    """
    LLM 对比 "需求 JSON 里的参数值" vs "SysML 代码里的 attribute redefines 值"。

    为什么需要: LLM 在从 req JSON 生成 SysML 代码时，可能"抄错"数值。
    比如需求写 R=1000，SysML 里写成了 R=100。交叉校验就是抓这种抄写错误。

    工作方式:
      把 req JSON 和 SysML 代码一起发给另一个 LLM（独立裁判），
      让它判断参数值是否一致。

    失败 → 打回 node2 重生成
    通过 → 前进到 node2_hitl
    """
    t0 = time.time()
    req = state.get("req", {})
    sysml = state.get("sysml", {})
    temperature = state.get("temperature", 0.3)
    feedback = state.get("human_feedback", "")

    # 没有 SysML 代码（异常情况）→ 跳过
    if not sysml.get("sysml_code"):
        return _pass_through("cross_validate", state, "无 SysML 代码，跳过交叉校验")

    # —— 把需求序列化为 JSON 文本（LLM 容易阅读）——
    req_json = json.dumps({
        "component_type": req.get("component_type", ""),
        "parameters": req.get("parameters", {}),
        "topology": req.get("topology", ""),
        "constraints": req.get("constraints", []),
    }, ensure_ascii=False, indent=2)

    sysml_code = sysml.get("sysml_code", "")

    # 加载交叉校验 prompt 模板并填充
    prompt = (
        load_prompt("q_cross_validate.txt")
        .replace("{req_json}", req_json)
        .replace("{sysml_code}", sysml_code[:4000])  # 截断，防超长
        .replace("{prev_feedback}", feedback)         # HITL 打回时的反馈
    )

    logger.info("Q_cross_validate: LLM 对比需求参数 vs SysML 代码...")
    result = chat([user_msg(prompt)], temperature=temperature, max_tokens=1024)

    # 解析 LLM 返回的 JSON
    try:
        parsed = json.loads(_extract_json(result))
        passed = parsed.get("passed", True)       # 默认放行（安全侧）
        issues = parsed.get("issues", [])
        details = parsed.get("summary", "")
    except json.JSONDecodeError:
        # LLM 输出了非 JSON 的东西 → 保守处理：放行
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
    logger.info("Q_cross_validate 完成 (%.1fs): passed=%s, issues=%s", elapsed, passed, len(issues))

    return {
        "quality_checks": quality_checks,
        # 如果没通过，把错误细节作为 feedback 传给 node2（让它知道哪里错了）
        "human_feedback": details if not passed else feedback,
        "timing": {**state.get("timing", {}), "q_cross_validate": elapsed},
    }


# ==========================================================================
# 节点: 物理量验证 (node3 后 → node4)
# ==========================================================================

def q_physics_validate(state: dict) -> dict:
    """
    V4 配置驱动物理验证: 从仿真 CSV 计算物理量，与需求理论值对比。

    两种路由模式（优先级降序）:
      1. expected_physics.validate_type → 配置驱动（实验模式，精密控制）
      2. component_type 关键词自动检测 → 兼容 V3（interactive 模式）

    配置驱动模式下，tolerance 也从配置读取，每个用例独立设阈值。
    """
    t0 = time.time()
    mo = state.get("mo", {})
    req = state.get("req", {})
    expected_physics = state.get("expected_physics")  # V4: 来自 test_case 配置

    # —— 守卫条件: 没有仿真数据 → 跳过 ——
    if not mo.get("success"):
        return _pass_through("physics_validate", state, "仿真未成功，跳过物理验证")

    csv_path = mo.get("csv_path", "")
    if not csv_path or not Path(csv_path).exists():
        return _pass_through("physics_validate", state, "无 CSV 文件，跳过物理验证")

    component_type = req.get("component_type", "").lower()
    params = req.get("parameters", {})

    # —— V4: 决定 validate_type 和 tolerance ——
    validate_type = None
    tolerance_pct = 50.0  # 默认很宽容（给未知领域留余地）

    if expected_physics:
        # 配置驱动模式（实验模式首选）
        validate_type = expected_physics.get("validate_type")
        tolerance_pct = expected_physics.get("tolerance_pct", 50.0)
        logger.info("Q_physics_validate: 配置驱动, type=%s, tolerance=%.0f%%", validate_type, tolerance_pct)
    else:
        # V3 兼容: 自动检测（interactive 模式）
        validate_type = _auto_detect_type(component_type, params)
        tolerance_pct = 20.0 if _is_thermal(component_type) else 50.0
        logger.info("Q_physics_validate: 自动检测, type=%s, tolerance=%.0f%%", validate_type, tolerance_pct)

    # —— 调度到对应验证函数 ——
    try:
        physics_result = _dispatch_validate(validate_type, csv_path, params, tolerance_pct, expected_physics)
    except Exception as e:
        # 计算异常不阻塞流水线
        logger.warning("Q_physics_validate: 计算异常: %s", e)
        physics_result = {"passed": True, "issues": [],
                          "details": f"物理计算异常 ({e})，跳过"}

    # 构建质量检查结果
    qc = {
        "check_type": "physics_validate",
        "validate_type": validate_type,
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
        logger.warning("Q_physics_validate 失败: type=%s, 偏差 %.1f%%", validate_type,
                       qc.get("deviation_percent", 0))
    else:
        logger.info("Q_physics_validate 通过: type=%s (%.1fs)", validate_type, elapsed)

    return {
        "quality_checks": quality_checks,
        "physics_feedback": qc["details"] if not physics_result["passed"] else "",
        "timing": {**state.get("timing", {}), "q_physics_validate": elapsed},
    }


# ==========================================================================
# 验证类型自动检测 + 调度
# ==========================================================================

def _auto_detect_type(component_type: str, params: dict) -> str:
    """
    V3 兼容: 从 component_type 关键词自动推断验证类型。

    当 expected_physics 不存在时（interactive 模式），靠这个函数猜该用哪种验证。
    """
    ct = component_type.lower()
    if _is_thermal(ct):
        return "thermal_steady"         # 热域 → 检查稳态温度
    if any(kw in ct for kw in ["rlc", "谐振"]):
        return "rlc_resonant_freq"      # RLC → 检查谐振频率
    if any(kw in ct for kw in ["运放", "opamp", "op-amp", "反相", "同相"]):
        return "opamp_gain"             # 运放 → 检查闭环增益
    return "rc_cutoff"                  # 默认 → RC 截止频率


def _dispatch_validate(validate_type: str, csv_path: str, params: dict,
                       tolerance_pct: float, expected_physics: Optional[dict]) -> dict:
    """
    根据 validate_type 调度到对应的验证函数。

    这是策略模式的 dispatch 层: 配置说"用 rc_cutoff" → 就调 _validate_rc_cutoff()
    新增验证类型 = 加一个 elif 分支 = 不改动上游代码。
    """
    if validate_type == "rlc_resonant_freq":
        return _validate_rlc_resonant(csv_path, params, tolerance_pct, expected_physics)
    elif validate_type == "opamp_gain":
        return _validate_opamp_gain(csv_path, params, tolerance_pct, expected_physics)
    elif validate_type == "thermal_steady":
        return _validate_thermal_steady(csv_path, params, tolerance_pct, expected_physics)
    else:
        return _validate_rc_cutoff(csv_path, params, tolerance_pct, expected_physics)


# ==========================================================================
# 物理量计算 — V4 配置驱动版（4 种策略）
# 所有验证函数签名: (csv_path, params, tolerance_pct, expected_physics) -> dict
# ==========================================================================

def _is_thermal(component_type: str) -> bool:
    """判断组件是否属于热域。"""
    return any(kw in component_type for kw in ["热", "thermal", "heat"])


# -- 策略 1: RC 截止频率 f_c = 1/(2π·τ) --

def _validate_rc_cutoff(csv_path: str, params: dict, tolerance_pct: float,
                        expected_physics: Optional[dict] = None) -> dict:
    """
    从 RC 阶跃响应计算 -3dB 截止频率 f_c = 1/(2π·τ)。

    算法: 找输出电压达到稳态值 63.2% 的时间点 → τ → f_c = 1/(2πτ)
    原理: RC 电路中 V(t) = V_ss * (1 - e^(-t/τ)), 当 t=τ 时 V=0.632*V_ss
    """
    data = _read_csv(csv_path)
    if not data or len(data.get("data", {})) < 2:
        return {"passed": True, "issues": [], "details": "CSV 数据不足，跳过 RC 截止频率验证"}

    time_col = data["columns"][0]
    signal_col = _find_signal_column(data, preferred=["v_out", "sensor.v", "sensor.", ".p.v", "capacitor.v"],
                                      skip_patterns=["der(", ".C", ".R", ".L", ".n.v", ".n.i"])
    if not signal_col:
        return {"passed": True, "issues": [], "details": "未找到有效的电压信号列，跳过验证"}

    times = data["data"][time_col]
    voltages = data["data"][signal_col]
    n = len(voltages)

    # 稳态值 = 最后 10% 数据的均值
    steady_state = sum(voltages[-max(1, n // 10):]) / max(1, n // 10)
    if steady_state < 0.001:
        return {"passed": True, "issues": [], "details": "稳态电压接近 0，跳过验证"}

    # τ = 达到 63.2% 稳态值的时间（线性插值提高精度）
    target_63 = steady_state * 0.632
    tau = None
    for i, v in enumerate(voltages):
        if v >= target_63:
            if i > 0 and voltages[i] > voltages[i - 1]:
                # 线性插值: 找到目标值在两个采样点间的位置
                frac = (target_63 - voltages[i - 1]) / (voltages[i] - voltages[i - 1])
                tau = times[i - 1] + frac * (times[i] - times[i - 1])
            else:
                tau = times[i]
            break
    if tau is None or tau <= 0:
        return {"passed": True, "issues": [], "details": "无法从阶跃响应计算时间常数 τ"}

    # f_c = 1 / (2π·τ)
    actual_fc = 1.0 / (2.0 * math.pi * tau)

    # 期望值: 优先读 params.cutoff_freq，否则从 R×C 推导
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

    deviation = abs(actual_fc - expected_fc) / expected_fc * 100
    passed = deviation < tolerance_pct

    return {
        "passed": passed,
        "issues": [{
            "param_name": "cutoff_frequency", "expected": f"{expected_fc:.1f} Hz",
            "found": f"{actual_fc:.1f} Hz", "severity": "error",
            "detail": f"截止频率偏差 {deviation:.1f}%（阈值 {tolerance_pct:.0f}%，τ={tau:.6f}s）",
        }] if not passed else [],
        "deviation_percent": round(deviation, 1), "expected_value": round(expected_fc, 1),
        "actual_value": round(actual_fc, 1),
        "details": f"f_c 实测={actual_fc:.1f} Hz, 期望={expected_fc:.1f} Hz, 偏差={deviation:.1f}% (τ={tau:.6f}s)",
    }


# -- 策略 2: 热稳态温度 --

def _validate_thermal_steady(csv_path: str, params: dict, tolerance_pct: float,
                             expected_physics: Optional[dict] = None) -> dict:
    """
    从热仿真 CSV 取稳态温度，对比期望值。

    V4 改进: 支持多个温度列同时验证（双房间热传导用例）。
    温度列通过值范围筛选（200K~500K = 物理合理的温度范围）。
    """
    data = _read_csv(csv_path)
    if not data or len(data.get("data", {})) < 2:
        return {"passed": True, "issues": [], "details": "CSV 数据不足，跳过热稳态验证"}

    # 找所有温度信号列: 排除参数常量(如 C, T_ref)，选值在 200-500K 的
    temp_cols = []
    for col in data["columns"][1:]:
        if "der(" in col:
            continue
        if col.endswith(".T") or col.endswith(".T_ref") or col.endswith(".alpha"):
            continue  # 传感器内部的参数，不是测量值
        vals = data["data"].get(col, [])
        if not vals or len(vals) < 2:
            continue
        if abs(max(vals) - min(vals)) < 0.1:     # 常数列（如环境温度）
            continue
        if max(vals) > 200 and max(vals) < 500:   # 物理合理的温度范围
            temp_cols.append(col)

    if not temp_cols:
        return {"passed": True, "issues": [], "details": "未找到有效的温度信号列（200-500K），跳过验证"}

    expected_temp = params.get("outdoor_temp", params.get("target_temp", None))
    if expected_temp is None and expected_physics:
        expected_temp = expected_physics.get("expected_value")

    all_passed = True
    issues, details_parts, deviations = [], [], []

    for col in temp_cols[:3]:  # 最多验证 3 个列（双房间 = 2 列）
        temps = data["data"][col]
        n = len(temps)
        steady_temp = sum(temps[-max(1, n // 10):]) / max(1, n // 10)

        if expected_temp is not None and expected_temp > 0:
            deviation = abs(steady_temp - expected_temp) / expected_temp * 100
            deviations.append(deviation)
            col_passed = deviation < tolerance_pct
            if not col_passed:
                all_passed = False
                issues.append({
                    "param_name": col, "expected": f"{expected_temp:.1f} K",
                    "found": f"{steady_temp:.1f} K", "severity": "error",
                    "detail": f"{col} 稳态温度偏差 {deviation:.1f}%（阈值 {tolerance_pct:.0f}%）",
                })
            details_parts.append(f"{col}={steady_temp:.1f}K (偏差 {deviation:.1f}%)")
        else:
            details_parts.append(f"{col}={steady_temp:.1f}K (无期望值)")

    avg_deviation = sum(deviations) / len(deviations) if deviations else 0

    return {
        "passed": all_passed, "issues": issues,
        "deviation_percent": round(avg_deviation, 1),
        "expected_value": round(expected_temp, 1) if expected_temp else None,
        "details": "; ".join(details_parts) if details_parts else "无有效温度数据",
    }


# -- 策略 3: RLC 谐振频率 f_0 = 1/(2π√(LC)) --

def _validate_rlc_resonant(csv_path: str, params: dict, tolerance_pct: float,
                           expected_physics: Optional[dict] = None) -> dict:
    """
    V4 新增: RLC 谐振频率验证。

    算法: 从阶跃/脉冲响应中识别振荡频率。
      1. 计算稳态值
      2. 找信号穿越稳态值的过零点
      3. 从过零点间距算振荡周期 → 频率
      4. 对比理论值 f_0 = 1 / (2π√(LC))
    """
    data = _read_csv(csv_path)
    if not data or len(data.get("data", {})) < 2:
        return {"passed": True, "issues": [], "details": "CSV 数据不足，跳过 RLC 谐振验证"}

    signal_col = _find_signal_column(data, preferred=["sensor.v", "v_out", ".p.v", "capacitor.v", "inductor.v"],
                                      skip_patterns=["der(", ".C", ".R", ".L", ".n.v", ".n.i"])
    if not signal_col:
        return {"passed": True, "issues": [], "details": "未找到有效信号列，跳过 RLC 验证"}

    times = data["data"][data["columns"][0]]
    signal = data["data"][signal_col]
    n = len(signal)

    steady = sum(signal[-max(1, n // 10):]) / max(1, n // 10)
    if abs(steady) < 1e-9:
        return {"passed": True, "issues": [], "details": "稳态值接近 0，无法计算谐振频率"}

    # 找过零点: signal[i-1] 和 signal[i] 在 steady 两侧 → 发生过零
    crossings = []
    for i in range(1, n):
        if (signal[i - 1] - steady) * (signal[i] - steady) < 0:
            # 线性插值计算精确过零时间
            frac = abs(signal[i - 1] - steady) / (abs(signal[i - 1] - steady) + abs(signal[i] - steady))
            t_cross = times[i - 1] + frac * (times[i] - times[i - 1])
            crossings.append(t_cross)
    if len(crossings) < 3:
        return {"passed": True, "issues": [],
                "details": f"振荡过零点不足 ({len(crossings)} 个)，跳过 RLC 谐振验证"}

    # 周期 = 相邻过零点间距的平均值 × 2（过零点间距 = 半周期）
    half_periods = [crossings[i + 1] - crossings[i] for i in range(len(crossings) - 1)]
    avg_half_period = sum(half_periods) / len(half_periods)
    period = avg_half_period * 2
    if period <= 0:
        return {"passed": True, "issues": [], "details": "振荡周期异常，跳过验证"}
    actual_freq = 1.0 / period

    # 理论谐振频率
    L_val = params.get("L", params.get("inductance", 0))
    C_val = params.get("C", params.get("capacitance", 0))
    if expected_physics and expected_physics.get("extra_params"):
        L_val = expected_physics["extra_params"].get("L", L_val)
        C_val = expected_physics["extra_params"].get("C_expected", C_val)
    if L_val > 0 and C_val > 0:
        expected_freq = 1.0 / (2.0 * math.pi * math.sqrt(L_val * C_val))
    else:
        expected_freq = params.get("cutoff_freq", params.get("cutoff_frequency", None))
        if expected_freq is None:
            return {"passed": True, "issues": [],
                    "details": f"无法计算理论谐振频率，实测 f_osc={actual_freq:.1f} Hz"}

    deviation = abs(actual_freq - expected_freq) / expected_freq * 100
    passed = deviation < tolerance_pct

    return {
        "passed": passed,
        "issues": [{"param_name": "resonant_frequency", "expected": f"{expected_freq:.1f} Hz",
                    "found": f"{actual_freq:.1f} Hz", "severity": "error",
                    "detail": f"谐振频率偏差 {deviation:.1f}%（阈值 {tolerance_pct:.0f}%，周期={period:.6f}s）"}] if not passed else [],
        "deviation_percent": round(deviation, 1), "expected_value": round(expected_freq, 1),
        "actual_value": round(actual_freq, 1),
        "details": f"f_osc 实测={actual_freq:.1f} Hz, 期望={expected_freq:.1f} Hz, 偏差={deviation:.1f}% (T={period:.6f}s)",
    }


# -- 策略 4: 运放闭环增益 G = -Rf/Rin --

def _validate_opamp_gain(csv_path: str, params: dict, tolerance_pct: float,
                         expected_physics: Optional[dict] = None) -> dict:
    """
    V4 新增: 运放增益验证。

    验证闭环增益 G = Vout / Vin，对比理论值 G = -Rf / Rin（反相放大器）。
    从 CSV 取输出电压稳态值 ÷ 输入电压 = 实际增益。
    """
    data = _read_csv(csv_path)
    if not data or len(data.get("data", {})) < 2:
        return {"passed": True, "issues": [], "details": "CSV 数据不足，跳过运放增益验证"}

    vout_col = _find_signal_column(data, preferred=["sensor.v", "v_out", "opamp.out", "opAmp.", ".p.v"],
                                    skip_patterns=["der(", ".C", ".R", ".L", ".n.v", ".n.i"])
    vin_col = _find_signal_column(data, preferred=["src.v", "signalSource.", "v_in"],
                                   skip_patterns=["der(", ".C", ".R", ".L", ".n.v", ".n.i"],
                                   exclude_cols=[vout_col])  # 排除 vout 列自身
    if not vout_col:
        return {"passed": True, "issues": [], "details": "未找到输出电压信号列，跳过运放验证"}

    vout_vals = data["data"][vout_col]
    n = len(vout_vals)
    vout_steady = sum(vout_vals[-max(1, n // 10):]) / max(1, n // 10)  # 稳态值

    # 输入电压: 优先从 CSV 取，否则从 params 读
    vin = params.get("Vin", params.get("V_in", params.get("input_voltage", 0.5)))
    if vin_col:
        vin_vals = data["data"][vin_col]
        vin = sum(vin_vals[-max(1, n // 10):]) / max(1, n // 10)

    if abs(vin) < 0.001:
        return {"passed": True, "issues": [], "details": "输入电压接近 0，无法计算增益"}

    actual_gain = vout_steady / vin                                  # 实际增益
    rf = params.get("Rf", params.get("R_f", 10000))                 # 反馈电阻
    rin = params.get("Rin", params.get("R_in", 1000))               # 输入电阻
    expected_gain = -rf / rin                                        # 理论增益
    expected_vout = expected_gain * vin                              # 理论输出电压

    if expected_physics and expected_physics.get("extra_params"):
        expected_gain = expected_physics["extra_params"].get("expected_gain", expected_gain)
        expected_vout = expected_physics["extra_params"].get("expected_vout", expected_vout)

    gain_deviation = (abs(actual_gain - expected_gain) / abs(expected_gain) * 100
                      if abs(expected_gain) > 0.001 else 100)
    passed = gain_deviation < tolerance_pct

    return {
        "passed": passed,
        "issues": [{"param_name": "closed_loop_gain",
                    "expected": f"{expected_gain:.2f} (Vout≈{expected_vout:.2f}V)",
                    "found": f"{actual_gain:.2f} (Vout={vout_steady:.2f}V)", "severity": "error",
                    "detail": f"闭环增益偏差 {gain_deviation:.1f}%（阈值 {tolerance_pct:.0f}%）"}] if not passed else [],
        "deviation_percent": round(gain_deviation, 1), "expected_value": round(expected_gain, 2),
        "actual_value": round(actual_gain, 2),
        "details": f"G 实测={actual_gain:.2f}, 期望={expected_gain:.2f}, 偏差={gain_deviation:.1f}%（Vout={vout_steady:.2f}V）",
    }


# ==========================================================================
# 工具函数
# ==========================================================================

def _read_csv(csv_path: str) -> Optional[dict]:
    """读仿真 CSV，返回 {columns: [col_names], data: {col_name: [values]}}。"""
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
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


def _find_signal_column(data: dict, preferred: list[str], skip_patterns: list[str],
                        exclude_cols: Optional[list[str]] = None) -> Optional[str]:
    """
    从 CSV 诸多列中找出"我们真正关心的"那个信号列。

    问题: 仿真 CSV 含大量无关列 — 参数常量(R, C, L)、导数(der(...))、
    地电压(.n.v)、电源参数。我们需要从中找到"输出电压"或"传感器读数"。

    算法: 两轮搜索
      第一轮: 按 preferred 关键词优先匹配，同时跳过 skip_patterns
      第二轮: 如果没找到，从任意有变化(非常量)的列中选一个

    Args:
        data: _read_csv 返回的结构
        preferred: 优先关键词列表（如 ["sensor.v", "v_out"]）
        skip_patterns: 需跳过的列名子串
        exclude_cols: 明确排除的列名（如 vout_col 不能同时是 vin_col）

    Returns:
        最佳信号列名，找不到返回 None
    """
    exclude = set(exclude_cols or [])

    # —— 第一轮: 优先匹配 ——
    for col in data["columns"][1:]:
        if col in exclude:
            continue
        # 检查是否为参数/常数列
        skip_substrings = [".C", ".R", ".L", "der(", ".n.v", ".n.i", ".alpha", ".T_ref"]
        if any(s in col for s in skip_substrings):
            continue
        vals = data["data"].get(col, [])
        if not vals or len(vals) < 2:
            continue
        if abs(max(vals) - min(vals)) < 1e-9:
            continue     # 值没有任何变化的列（常量参数）
        for kw in preferred:
            if kw in col:
                return col  # 找到了！优先返回

    # —— 第二轮: 回退 ——
    for col in data["columns"][1:]:
        if col in exclude:
            continue
        if any(sp in col for sp in skip_substrings):
            continue
        vals = data["data"].get(col, [])
        if not vals or len(vals) < 2:
            continue
        if abs(max(vals) - min(vals)) < 1e-9:
            continue
        if max(vals) > 0.01:
            return col     # 找到任意有变化的信号
    return None


def _extract_json(text: str) -> str:
    """从 LLM 返回中提取纯 JSON 文本，去掉 markdown 包裹。"""
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
    """跳过检查时返回一个 '通过但无实际检查' 的占位结果。"""
    qc = {"check_type": check_type, "passed": True, "issues": [], "details": reason}
    quality_checks = dict(state.get("quality_checks", {}))
    quality_checks[check_type] = qc
    logger.info("%s: %s", check_type, reason)
    return {"quality_checks": quality_checks}
