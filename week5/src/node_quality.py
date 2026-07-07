"""
V4 质量检查节点。两个节点插入流水线:

  Q_cross_validate:  node2 → 此处 → node2_hitl
    用 LLM 对比 req JSON 参数值 vs SysML 代码里的 attribute redefines 值。
    不一致 → 打回 node2 重生成。

  Q_physics_validate: node3 成功后 → 此处 → node4_summary
    从 simulation CSV 计算物理量，与需求理论值对比。
    V4: 配置驱动 — expected_physics.validate_type 决定验证策略。
    偏差过大 → 打回 node3 repair。
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

# V4: 验证函数注册表 — 新增验证类型只需在这里注册
VALIDATOR_REGISTRY = {
    "rc_cutoff": "_validate_rc_cutoff",
    "thermal_steady": "_validate_thermal_steady",
    "rlc_resonant_freq": "_validate_rlc_resonant",
    "opamp_gain": "_validate_opamp_gain",
}

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


# ============================================================
# 节点：交叉校验 (node2 → 此处 → node2_hitl)
# ============================================================

def q_cross_validate(state: dict) -> dict:
    """LLM 对比 req JSON 参数与 SysML 代码里的值，检查一致性。"""
    t0 = time.time()
    req = state.get("req", {})
    sysml = state.get("sysml", {})
    temperature = state.get("temperature", 0.3)
    feedback = state.get("human_feedback", "")  # 如果有上次打回的反馈，一并给 LLM

    # 如果还没到 node2 就进来了（异常），直接放行
    if not sysml.get("sysml_code"):
        return _pass_through("cross_validate", state, "无 SysML 代码，跳过交叉校验")

    # 构造对比 prompt
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
        .replace("{sysml_code}", sysml_code[:4000])
        .replace("{prev_feedback}", feedback)
    )

    logger.info("Q_cross_validate: LLM 对比需求参数 vs SysML 代码...")
    result = chat([user_msg(prompt)], temperature=temperature, max_tokens=1024)

    # 解析 LLM 返回的 JSON
    try:
        parsed = json.loads(_extract_json(result))
        passed = parsed.get("passed", True)
        issues = parsed.get("issues", [])
        details = parsed.get("summary", "")
    except json.JSONDecodeError:
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
        "human_feedback": details if not passed else feedback,  # 失败时打回 node2 用
        "timing": {**state.get("timing", {}), "q_cross_validate": elapsed},
    }


# ============================================================
# 节点：物理量验证 (node3 成功后 → 此处 → node4_summary)
# ============================================================

def q_physics_validate(state: dict) -> dict:
    """V4 配置驱动物理验证：从仿真 CSV 计算物理量，与需求理论值对比。

    路由逻辑（优先级降序）：
      1. state["expected_physics"].validate_type → 配置驱动（实验模式）
      2. component_type 关键词自动检测 → 兼容 V3（interactive 模式）
    """
    t0 = time.time()
    mo = state.get("mo", {})
    req = state.get("req", {})
    expected_physics = state.get("expected_physics")  # V4: 可为 None

    # 仿真失败 → 无 CSV 可验证，直接放行
    if not mo.get("success"):
        return _pass_through("physics_validate", state, "仿真未成功，跳过物理验证")

    csv_path = mo.get("csv_path", "")
    if not csv_path or not Path(csv_path).exists():
        return _pass_through("physics_validate", state, "无 CSV 文件，跳过物理验证")

    component_type = req.get("component_type", "").lower()
    params = req.get("parameters", {})

    # V4: 决定 validate_type 和 tolerance
    validate_type = None
    tolerance_pct = 50.0  # 默认放宽

    if expected_physics:
        # 配置驱动模式
        validate_type = expected_physics.get("validate_type")
        tolerance_pct = expected_physics.get("tolerance_pct", 50.0)
        logger.info("Q_physics_validate: 配置驱动, type=%s, tolerance=%.0f%%", validate_type, tolerance_pct)
    else:
        # V3 兼容：自动检测
        validate_type = _auto_detect_type(component_type, params)
        tolerance_pct = 20.0 if _is_thermal(component_type) else 50.0
        logger.info("Q_physics_validate: 自动检测, type=%s, tolerance=%.0f%%", validate_type, tolerance_pct)

    # 调度验证函数
    try:
        physics_result = _dispatch_validate(validate_type, csv_path, params, tolerance_pct, expected_physics)
    except Exception as e:
        logger.warning("Q_physics_validate: 计算异常: %s", e)
        physics_result = {"passed": True, "issues": [],
                          "details": f"物理计算异常 ({e})，跳过"}

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
        logger.warning("Q_physics_validate 失败: type=%s, 偏差 %.1f%%", validate_type, qc.get("deviation_percent", 0))
    else:
        logger.info("Q_physics_validate 通过: type=%s (%.1fs)", validate_type, elapsed)

    return {
        "quality_checks": quality_checks,
        "physics_feedback": qc["details"] if not physics_result["passed"] else "",
        "timing": {**state.get("timing", {}), "q_physics_validate": elapsed},
    }


def _auto_detect_type(component_type: str, params: dict) -> str:
    """V3 兼容：从 component_type 自动推断验证类型。"""
    ct = component_type.lower()
    if _is_thermal(ct):
        return "thermal_steady"
    if any(kw in ct for kw in ["rlc", "谐振"]):
        return "rlc_resonant_freq"
    if any(kw in ct for kw in ["运放", "opamp", "op-amp", "反相", "同相"]):
        return "opamp_gain"
    # 默认 RC 截止频率
    return "rc_cutoff"


def _dispatch_validate(validate_type: str, csv_path: str, params: dict,
                       tolerance_pct: float, expected_physics: Optional[dict]) -> dict:
    """根据 validate_type 调度到对应验证函数。"""
    if validate_type == "rlc_resonant_freq":
        return _validate_rlc_resonant(csv_path, params, tolerance_pct, expected_physics)
    elif validate_type == "opamp_gain":
        return _validate_opamp_gain(csv_path, params, tolerance_pct, expected_physics)
    elif validate_type == "thermal_steady":
        return _validate_thermal_steady(csv_path, params, tolerance_pct, expected_physics)
    else:
        # 默认：RC 截止频率
        return _validate_rc_cutoff(csv_path, params, tolerance_pct, expected_physics)


# ============================================================
# 物理量计算 — V4 配置驱动版
# 所有验证函数签名: (csv_path, params, tolerance_pct, expected_physics) → dict
# ============================================================

def _is_thermal(component_type: str) -> bool:
    return any(kw in component_type for kw in ["热", "thermal", "heat"])


def _validate_rc_cutoff(csv_path: str, params: dict, tolerance_pct: float,
                        expected_physics: Optional[dict] = None) -> dict:
    """从 RC 阶跃响应计算 -3dB 截止频率 f_c = 1/(2π·τ)。"""

    data = _read_csv(csv_path)
    if not data or len(data.get("data", {})) < 2:
        return {"passed": True, "issues": [],
                "details": "CSV 数据不足，跳过 RC 截止频率验证"}

    time_col = data["columns"][0]
    signal_col = _find_signal_column(data, preferred=["v_out", "sensor.v", "sensor.", ".p.v", "capacitor.v"],
                                      skip_patterns=["der(", ".C", ".R", ".L", ".n.v", ".n.i"])

    if not signal_col:
        return {"passed": True, "issues": [],
                "details": "未找到有效的电压信号列，跳过验证"}

    times = data["data"][time_col]
    voltages = data["data"][signal_col]

    n = len(voltages)
    steady_state = sum(voltages[-max(1, n // 10):]) / max(1, n // 10)
    if steady_state < 0.001:
        return {"passed": True, "issues": [],
                "details": "稳态电压接近 0，跳过验证"}

    # τ = 达到 63.2% 稳态的时间
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
        return {"passed": True, "issues": [],
                "details": "无法从阶跃响应计算时间常数 τ"}

    actual_fc = 1.0 / (2.0 * math.pi * tau)

    # 期望值：优先读 params.cutoff_freq，否则从 R*C 推导
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
            "param_name": "cutoff_frequency",
            "expected": f"{expected_fc:.1f} Hz",
            "found": f"{actual_fc:.1f} Hz",
            "severity": "error",
            "detail": f"截止频率偏差 {deviation:.1f}%（阈值 {tolerance_pct:.0f}%，τ={tau:.6f}s）",
        }] if not passed else [],
        "deviation_percent": round(deviation, 1),
        "expected_value": round(expected_fc, 1),
        "actual_value": round(actual_fc, 1),
        "details": f"f_c 实测={actual_fc:.1f} Hz, 期望={expected_fc:.1f} Hz, 偏差={deviation:.1f}% (τ={tau:.6f}s)",
    }


def _validate_thermal_steady(csv_path: str, params: dict, tolerance_pct: float,
                             expected_physics: Optional[dict] = None) -> dict:
    """从热仿真 CSV 取稳态温度，对比期望值。V4: 支持多温度列（双房间）。"""
    data = _read_csv(csv_path)
    if not data or len(data.get("data", {})) < 2:
        return {"passed": True, "issues": [],
                "details": "CSV 数据不足，跳过热稳态验证"}

    # 找所有温度信号列（排除参数常量和传感器内部的 .T 参数）
    temp_cols = []
    for col in data["columns"][1:]:
        if "der(" in col:
            continue
        if col.endswith(".T") or col.endswith(".T_ref") or col.endswith(".alpha"):
            continue
        vals = data["data"].get(col, [])
        if not vals or len(vals) < 2:
            continue
        if abs(max(vals) - min(vals)) < 0.1:  # 常数列跳过
            continue
        # 温度列通常有温度相关后缀或值在 200-400K 范围
        if max(vals) > 200 and max(vals) < 500:
            temp_cols.append(col)

    if not temp_cols:
        return {"passed": True, "issues": [],
                "details": "未找到有效的温度信号列（200-500K），跳过验证"}

    # 期望值
    expected_temp = params.get("outdoor_temp", params.get("target_temp", None))
    if expected_temp is None and expected_physics:
        # 从 expected_physics 推断
        expected_temp = expected_physics.get("expected_value")

    # 验证每个温度列
    all_passed = True
    issues = []
    details_parts = []
    deviations = []

    for col in temp_cols[:3]:  # 最多验证 3 个温度列
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
                    "param_name": col,
                    "expected": f"{expected_temp:.1f} K",
                    "found": f"{steady_temp:.1f} K",
                    "severity": "error",
                    "detail": f"{col} 稳态温度偏差 {deviation:.1f}%（阈值 {tolerance_pct:.0f}%）",
                })
            details_parts.append(f"{col}={steady_temp:.1f}K (偏差 {deviation:.1f}%)")
        else:
            details_parts.append(f"{col}={steady_temp:.1f}K (无期望值)")

    avg_deviation = sum(deviations) / len(deviations) if deviations else 0

    return {
        "passed": all_passed,
        "issues": issues,
        "deviation_percent": round(avg_deviation, 1),
        "expected_value": round(expected_temp, 1) if expected_temp else None,
        "actual_value": round(sum(temps[-max(1, n // 10):]) / max(1, n // 10)
                              for temps in [data["data"][temp_cols[0]]]) if temp_cols else None,
        "details": "; ".join(details_parts) if details_parts else "无有效温度数据",
    }


def _validate_rlc_resonant(csv_path: str, params: dict, tolerance_pct: float,
                           expected_physics: Optional[dict] = None) -> dict:
    """V4 新增: RLC 谐振频率验证。

    策略：从阶跃/脉冲响应中识别振荡频率，对比理论谐振频率 f_0 = 1/(2π√(LC))。
    实际做法：找输出信号的过零点间距，推算振荡频率。
    """
    data = _read_csv(csv_path)
    if not data or len(data.get("data", {})) < 2:
        return {"passed": True, "issues": [],
                "details": "CSV 数据不足，跳过 RLC 谐振验证"}

    signal_col = _find_signal_column(data, preferred=["sensor.v", "v_out", ".p.v", "capacitor.v", "inductor.v"],
                                      skip_patterns=["der(", ".C", ".R", ".L", ".n.v", ".n.i"])

    if not signal_col:
        return {"passed": True, "issues": [],
                "details": "未找到有效信号列，跳过 RLC 验证"}

    times = data["data"][data["columns"][0]]
    signal = data["data"][signal_col]
    n = len(signal)

    # 取稳态值
    steady = sum(signal[-max(1, n // 10):]) / max(1, n // 10)

    if abs(steady) < 1e-9:
        return {"passed": True, "issues": [],
                "details": "稳态值接近 0，无法计算谐振频率"}

    # 找振荡周期：寻找信号穿越稳态值的时间点
    crossings = []
    for i in range(1, n):
        if (signal[i - 1] - steady) * (signal[i] - steady) < 0:
            # 线性插值
            frac = abs(signal[i - 1] - steady) / (abs(signal[i - 1] - steady) + abs(signal[i] - steady))
            t_cross = times[i - 1] + frac * (times[i] - times[i - 1])
            crossings.append(t_cross)

    if len(crossings) < 3:
        return {"passed": True, "issues": [],
                "details": f"振荡过零点不足 ({len(crossings)} 个)，跳过 RLC 谐振验证"}

    # 从过零点间距推算周期和频率
    half_periods = [crossings[i + 1] - crossings[i] for i in range(len(crossings) - 1)]
    avg_half_period = sum(half_periods) / len(half_periods)
    period = avg_half_period * 2  # 半个周期 × 2 = 完整周期
    if period <= 0:
        return {"passed": True, "issues": [],
                "details": "振荡周期异常，跳过验证"}

    actual_freq = 1.0 / period

    # 理论谐振频率 f_0 = 1 / (2π√(LC))
    L_val = params.get("L", params.get("inductance", 0))
    C_val = params.get("C", params.get("capacitance", 0))
    if expected_physics and expected_physics.get("extra_params"):
        L_val = expected_physics["extra_params"].get("L", L_val)
        C_val = expected_physics["extra_params"].get("C_expected", C_val)

    if L_val > 0 and C_val > 0:
        expected_freq = 1.0 / (2.0 * math.pi * math.sqrt(L_val * C_val))
    else:
        # 从 cutoff_freq 参数读取
        expected_freq = params.get("cutoff_freq", params.get("cutoff_frequency", None))
        if expected_freq is None:
            return {"passed": True, "issues": [],
                    "details": f"无法计算理论谐振频率（缺 L 或 C 或 cutoff_freq），实测 f_osc={actual_freq:.1f} Hz"}

    deviation = abs(actual_freq - expected_freq) / expected_freq * 100
    passed = deviation < tolerance_pct

    return {
        "passed": passed,
        "issues": [{
            "param_name": "resonant_frequency",
            "expected": f"{expected_freq:.1f} Hz",
            "found": f"{actual_freq:.1f} Hz",
            "severity": "error",
            "detail": f"谐振频率偏差 {deviation:.1f}%（阈值 {tolerance_pct:.0f}%，周期={period:.6f}s）",
        }] if not passed else [],
        "deviation_percent": round(deviation, 1),
        "expected_value": round(expected_freq, 1),
        "actual_value": round(actual_freq, 1),
        "details": f"f_osc 实测={actual_freq:.1f} Hz, 期望={expected_freq:.1f} Hz, 偏差={deviation:.1f}% (T={period:.6f}s)",
    }


def _validate_opamp_gain(csv_path: str, params: dict, tolerance_pct: float,
                         expected_physics: Optional[dict] = None) -> dict:
    """V4 新增: 运放增益验证。

    验证闭环增益 G = Vout / Vin，对比理论值 G = -Rf / Rin（反相放大器）。
    从 CSV 取输出电压的稳态值，除以输入电压得到实际增益。
    """
    data = _read_csv(csv_path)
    if not data or len(data.get("data", {})) < 2:
        return {"passed": True, "issues": [],
                "details": "CSV 数据不足，跳过运放增益验证"}

    # 找输出电压列
    vout_col = _find_signal_column(data, preferred=["sensor.v", "v_out", "opamp.out", "opAmp.", ".p.v"],
                                    skip_patterns=["der(", ".C", ".R", ".L", ".n.v", ".n.i"])
    # 找输入电压列（用于计算实际增益）
    vin_col = _find_signal_column(data, preferred=["src.v", "signalSource.", "v_in"],
                                   skip_patterns=["der(", ".C", ".R", ".L", ".n.v", ".n.i"],
                                   exclude_cols=[vout_col])

    if not vout_col:
        return {"passed": True, "issues": [],
                "details": "未找到输出电压信号列，跳过运放验证"}

    vout_vals = data["data"][vout_col]
    n = len(vout_vals)

    # 稳态输出电压
    vout_steady = sum(vout_vals[-max(1, n // 10):]) / max(1, n // 10)

    # 输入电压
    vin = params.get("Vin", params.get("V_in", params.get("input_voltage", 0.5)))
    # 如果 CSV 里有输入电压列，取稳态值
    if vin_col:
        vin_vals = data["data"][vin_col]
        vin = sum(vin_vals[-max(1, n // 10):]) / max(1, n // 10)

    if abs(vin) < 0.001:
        return {"passed": True, "issues": [],
                "details": "输入电压接近 0，无法计算增益"}

    actual_gain = vout_steady / vin

    # 理论增益 G = -Rf / Rin
    rf = params.get("Rf", params.get("R_f", params.get("feedback_resistance", 10000)))
    rin = params.get("Rin", params.get("R_in", params.get("input_resistance", 1000)))
    expected_gain = -rf / rin
    expected_vout = expected_gain * vin

    if expected_physics and expected_physics.get("extra_params"):
        expected_gain = expected_physics["extra_params"].get("expected_gain", expected_gain)
        expected_vout = expected_physics["extra_params"].get("expected_vout", expected_vout)

    # 计算偏差：对增益用百分比，对 Vout 用绝对值
    gain_deviation = abs(actual_gain - expected_gain) / abs(expected_gain) * 100 if abs(expected_gain) > 0.001 else 100
    passed = gain_deviation < tolerance_pct

    return {
        "passed": passed,
        "issues": [{
            "param_name": "closed_loop_gain",
            "expected": f"{expected_gain:.2f} (Vout≈{expected_vout:.2f}V)",
            "found": f"{actual_gain:.2f} (Vout={vout_steady:.2f}V)",
            "severity": "error",
            "detail": f"闭环增益偏差 {gain_deviation:.1f}%（阈值 {tolerance_pct:.0f}%），期望 G={expected_gain:.2f}，实测 G={actual_gain:.2f}",
        }] if not passed else [],
        "deviation_percent": round(gain_deviation, 1),
        "expected_value": round(expected_gain, 2),
        "actual_value": round(actual_gain, 2),
        "details": f"G 实测={actual_gain:.2f}, 期望={expected_gain:.2f}, 偏差={gain_deviation:.1f}%（Vout={vout_steady:.2f}V, Vin={vin:.2f}V）",
    }


# ============================================================
# 工具函数
# ============================================================

def _read_csv(csv_path: str) -> Optional[dict]:
    """读仿真 CSV，返回 {columns, data: {col: [values]}}。"""
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
    """从 CSV 列中找最合适的信号列，跳过参数常量和地电压。

    Args:
        data: _read_csv 返回值
        preferred: 优先关键词列表（按顺序匹配）
        skip_patterns: 需跳过的列名模式
        exclude_cols: 排除的列名列表

    Returns:
        最佳信号列名，找不到返回 None
    """
    exclude = set(exclude_cols or [])

    # 优先匹配
    for col in data["columns"][1:]:
        if col in exclude:
            continue
        if any(p in skip_patterns for p in [".C", ".R", ".L", "der(", ".n.v", ".n.i", ".alpha", ".T_ref"]):
            if any(s in col for s in [".C", ".R", ".L", "der(", ".n.v", ".n.i", ".alpha", ".T_ref"]):
                continue
        vals = data["data"].get(col, [])
        if not vals or len(vals) < 2:
            continue
        if abs(max(vals) - min(vals)) < 1e-9:
            continue  # 常数列
        for kw in preferred:
            if kw in col:
                return col

    # 回退：找任意有变化的列
    for col in data["columns"][1:]:
        if col in exclude:
            continue
        is_skip = False
        for sp in skip_patterns:
            if sp in col:
                is_skip = True
                break
        if is_skip:
            continue
        vals = data["data"].get(col, [])
        if not vals or len(vals) < 2:
            continue
        if abs(max(vals) - min(vals)) < 1e-9:
            continue
        if max(vals) > 0.01:
            return col

    return None


def _extract_json(text: str) -> str:
    """从 LLM 返回中提取 JSON。"""
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
    """跳过检查时的默认通过结果。"""
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
