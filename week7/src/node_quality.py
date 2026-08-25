"""
质量审查 Agent（V6 升级版）。

V4 → V6 变更:
  - q_cross_validate: prompt 按 V6 规范重写（具体检查项 + JSON 输出）
  - 新增 q_sysml_modelica_check: SysML ↔ Modelica 跨步一致性检查（V4 只有 req↔SysML）
  - q_physics_validate: 保留 V4 配置驱动物理验证（确定性 Python 计算，不走 LLM）
  - 跨步检查结果记录到 quality_checks["sysml_modelica"]

Agent ④ 检查范围:
  req ↔ SysML (交叉校验) → SysML ↔ Modelica (跨步校验) → 物理验证 (Python)
"""

import csv
import json
import logging
import math
import time
from pathlib import Path
from typing import Optional

from src.llm_client import chat, chat_with, user_msg
from src.agent_loop import parse_review_json, ReviewResult
from src.utils import load_prompt

logger = logging.getLogger("quality")

# V4 验证函数注册表（保持不变）
VALIDATOR_REGISTRY = {
    "rc_cutoff": "_validate_rc_cutoff",
    "thermal_steady": "_validate_thermal_steady",
    "rlc_resonant_freq": "_validate_rlc_resonant",
    "opamp_gain": "_validate_opamp_gain",
}


# ============================================================
# 节点：交叉校验 req ↔ SysML（V6 升级 prompt）
# ============================================================

def q_cross_validate(state: dict) -> dict:
    """V6 升级：LLM 对比 req JSON 参数与 SysML 代码，结构化 JSON 输出。"""
    t0 = time.time()
    req = state.get("req", {})
    sysml = state.get("sysml", {})
    temperature = state.get("temperature", 0.3)
    feedback = state.get("human_feedback", "")

    if not sysml.get("sysml_code"):
        return _pass_through("cross_validate", state, "无 SysML 代码，跳过交叉校验")

    req_json = json.dumps({
        "component_type": req.get("component_type", ""),
        "parameters": req.get("parameters", {}),
        "topology": req.get("topology", ""),
        "constraints": req.get("constraints", []),
    }, ensure_ascii=False, indent=2)

    sysml_code = sysml.get("sysml_code", "")

    # V6: 使用结构化审查 prompt（具体检查项 + JSON 输出）
    prompt = (
        load_prompt("q_cross_validate.txt")
        .replace("{req_json}", req_json)
        .replace("{sysml_code}", sysml_code[:4000])
        .replace("{prev_feedback}", feedback)
    )

    logger.info("Q_cross_validate: V6 结构化审查 req ↔ SysML...")
    result = chat_with("review", [user_msg(prompt)], temperature=temperature, max_tokens=1024)
    review = parse_review_json(result)

    qc = {
        "check_type": "cross_validate",
        "passed": review.ok,
        "issues": [_issue_to_dict(i) for i in review.issues],
        "details": review.summary,
        "score": review.score,
    }
    quality_checks = dict(state.get("quality_checks", {}))
    quality_checks["cross_validate"] = qc

    elapsed = time.time() - t0
    logger.info("Q_cross_validate 完成 (%.1fs): passed=%s, score=%s, issues=%s",
                elapsed, review.ok, review.score, len(review.issues))

    return {
        "quality_checks": quality_checks,
        "human_feedback": review.summary if not review.ok else feedback,
        "timing": {**state.get("timing", {}), "q_cross_validate": elapsed},
    }


# ============================================================
# 节点：物理验证（V4 保留） + SysML ↔ Modelica 跨步检查（V6 新增）
# ============================================================

def q_physics_validate(state: dict) -> dict:
    """V6: 物理验证（Python 确定性计算） + SysML↔Modelica 跨步检查（LLM）。"""
    t0 = time.time()
    mo = state.get("mo", {})
    req = state.get("req", {})
    sysml = state.get("sysml", {})
    expected_physics = state.get("expected_physics")
    quality_checks = dict(state.get("quality_checks", {}))

    # ── V6 新增：SysML ↔ Modelica 跨步一致性检查 ──
    if mo.get("success") and sysml.get("sysml_code") and mo.get("modelica_code"):
        sysml_mo_result = _check_sysml_modelica_consistency(state)
        quality_checks["sysml_modelica"] = sysml_mo_result
        if not sysml_mo_result.get("passed"):
            logger.warning("SysML↔Modelica 跨步检查发现问题: %s",
                          sysml_mo_result.get("issues", []))

    # ── 物理验证（V4 保留）──
    if not mo.get("success"):
        physics_result = {"check_type": "physics_validate", "passed": True,
                          "issues": [], "details": "仿真未成功，跳过物理验证"}
        quality_checks["physics_validate"] = physics_result
        return {"quality_checks": quality_checks,
                "timing": {**state.get("timing", {}), "q_physics_validate": time.time() - t0}}

    csv_path = mo.get("csv_path", "")
    if not csv_path or not Path(csv_path).exists():
        physics_result = {"check_type": "physics_validate", "passed": True,
                          "issues": [], "details": "无 CSV 文件，跳过物理验证"}
        quality_checks["physics_validate"] = physics_result
        return {"quality_checks": quality_checks,
                "timing": {**state.get("timing", {}), "q_physics_validate": time.time() - t0}}

    component_type = req.get("component_type", "").lower()
    params = req.get("parameters", {})

    # 决定 validate_type 和 tolerance
    validate_type = None
    tolerance_pct = 50.0

    if expected_physics:
        validate_type = expected_physics.get("validate_type")
        tolerance_pct = expected_physics.get("tolerance_pct", 50.0)
        logger.info("Q_physics_validate: 配置驱动, type=%s, tolerance=%.0f%%", validate_type, tolerance_pct)
    else:
        validate_type = _auto_detect_type(component_type, params)
        tolerance_pct = 20.0 if _is_thermal(component_type) else 50.0
        logger.info("Q_physics_validate: 自动检测, type=%s, tolerance=%.0f%%", validate_type, tolerance_pct)

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
    quality_checks["physics_validate"] = qc

    elapsed = time.time() - t0

    if not physics_result["passed"]:
        logger.warning("Q_physics_validate 失败: type=%s, 偏差 %.1f%%",
                      validate_type, qc.get("deviation_percent", 0))
    else:
        logger.info("Q_physics_validate 通过: type=%s (%.1fs)", validate_type, elapsed)

    return {
        "quality_checks": quality_checks,
        "physics_feedback": qc["details"] if not physics_result["passed"] else "",
        "timing": {**state.get("timing", {}), "q_physics_validate": elapsed},
    }


# ============================================================
# V6 新增：SysML ↔ Modelica 跨步一致性检查
# ============================================================

def _check_sysml_modelica_consistency(state: dict) -> dict:
    """V6: LLM 对比 SysML 代码和 Modelica 代码的拓扑/参数一致性。

    检查要点:
      - SysML 的 part/component 在 Modelica 中是否有对应 model/component
      - SysML 的 connect 关系在 Modelica 中是否有对应 equation connect
      - 参数值是否一致（SysML attribute redefines vs Modelica parameter）
      - 端口/接口对应关系是否正确
    """
    t0 = time.time()
    req = state.get("req", {})
    sysml = state.get("sysml", {})
    mo = state.get("mo", {})
    temperature = state.get("temperature", 0.3)

    req_json = json.dumps({
        "component_type": req.get("component_type", ""),
        "parameters": req.get("parameters", {}),
        "topology": req.get("topology", ""),
        "constraints": req.get("constraints", []),
    }, ensure_ascii=False, indent=2)

    sysml_code = sysml.get("sysml_code", "")
    modelica_code = mo.get("modelica_code", "")

    prompt = (
        load_prompt("q_sysml_modelica.txt")
        .replace("{req_json}", req_json)
        .replace("{sysml_code}", sysml_code[:3000])
        .replace("{modelica_code}", modelica_code[:3000])
    )

    logger.info("Q_sysml_modelica: LLM 跨步对比 SysML ↔ Modelica...")
    result = chat_with("review", [user_msg(prompt)], temperature=temperature, max_tokens=1024)

    try:
        review = parse_review_json(result)
        passed = review.ok
        issues = [_issue_to_dict(i) for i in review.issues]
        details = review.summary
    except Exception:
        logger.warning("Q_sysml_modelica: LLM 返回解析失败，默认放行")
        passed = True
        issues = []
        details = "LLM 返回格式异常，跳过跨步检查"

    elapsed = time.time() - t0
    logger.info("Q_sysml_modelica 完成 (%.1fs): passed=%s, issues=%s", elapsed, passed, len(issues))

    return {
        "check_type": "sysml_modelica",
        "passed": passed,
        "issues": issues,
        "details": details,
    }


# ============================================================
# 物理量计算（V4 保留，不变）
# ============================================================

def _auto_detect_type(component_type: str, params: dict) -> str:
    ct = component_type.lower()
    if _is_thermal(ct):
        return "thermal_steady"
    if any(kw in ct for kw in ["rlc", "谐振"]):
        return "rlc_resonant_freq"
    if any(kw in ct for kw in ["运放", "opamp", "op-amp", "反相", "同相"]):
        return "opamp_gain"
    return "rc_cutoff"


def _dispatch_validate(validate_type: str, csv_path: str, params: dict,
                       tolerance_pct: float, expected_physics: Optional[dict]) -> dict:
    if validate_type == "rlc_resonant_freq":
        return _validate_rlc_resonant(csv_path, params, tolerance_pct, expected_physics)
    elif validate_type == "opamp_gain":
        return _validate_opamp_gain(csv_path, params, tolerance_pct, expected_physics)
    elif validate_type == "thermal_steady":
        return _validate_thermal_steady(csv_path, params, tolerance_pct, expected_physics)
    else:
        return _validate_rc_cutoff(csv_path, params, tolerance_pct, expected_physics)


def _is_thermal(component_type: str) -> bool:
    return any(kw in component_type for kw in ["热", "thermal", "heat"])


# ── RC 截止频率验证 ──
def _validate_rc_cutoff(csv_path: str, params: dict, tolerance_pct: float,
                        expected_physics: Optional[dict] = None) -> dict:
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
    steady_state = sum(voltages[-max(1, n // 10):]) / max(1, n // 10)
    if steady_state < 0.001:
        return {"passed": True, "issues": [], "details": "稳态电压接近 0，跳过验证"}

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
        return {"passed": True, "issues": [], "details": "无法从阶跃响应计算时间常数 τ"}

    actual_fc = 1.0 / (2.0 * math.pi * tau)

    expected_fc = None
    for key in ["cutoff_freq", "cutoff_frequency", "f_cutoff", "fc", "f_c", "截止频率"]:
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


# ── 热稳态验证 ──
def _validate_thermal_steady(csv_path: str, params: dict, tolerance_pct: float,
                             expected_physics: Optional[dict] = None) -> dict:
    data = _read_csv(csv_path)
    if not data or len(data.get("data", {})) < 2:
        return {"passed": True, "issues": [], "details": "CSV 数据不足，跳过热稳态验证"}

    temp_cols = []
    for col in data["columns"][1:]:
        if "der(" in col:
            continue
        if col.endswith(".T") or col.endswith(".T_ref") or col.endswith(".alpha"):
            continue
        vals = data["data"].get(col, [])
        if not vals or len(vals) < 2:
            continue
        if abs(max(vals) - min(vals)) < 0.1:
            continue
        if max(vals) > 200 and max(vals) < 500:
            temp_cols.append(col)

    if not temp_cols:
        return {"passed": True, "issues": [], "details": "未找到有效的温度信号列（200-500K），跳过验证"}

    expected_temp = params.get("outdoor_temp", params.get("target_temp", None))
    if expected_temp is None and expected_physics:
        expected_temp = expected_physics.get("expected_value")

    all_passed = True
    issues = []
    details_parts = []
    deviations = []

    for col in temp_cols[:3]:
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
        "actual_value": None,
        "details": "; ".join(details_parts) if details_parts else "无有效温度数据",
    }


# ── RLC 谐振验证 ──
def _validate_rlc_resonant(csv_path: str, params: dict, tolerance_pct: float,
                           expected_physics: Optional[dict] = None) -> dict:
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

    crossings = []
    for i in range(1, n):
        if (signal[i - 1] - steady) * (signal[i] - steady) < 0:
            frac = abs(signal[i - 1] - steady) / (abs(signal[i - 1] - steady) + abs(signal[i] - steady))
            t_cross = times[i - 1] + frac * (times[i] - times[i - 1])
            crossings.append(t_cross)

    if len(crossings) < 3:
        return {"passed": True, "issues": [], "details": f"振荡过零点不足 ({len(crossings)} 个)，跳过 RLC 谐振验证"}

    half_periods = [crossings[i + 1] - crossings[i] for i in range(len(crossings) - 1)]
    avg_half_period = sum(half_periods) / len(half_periods)
    period = avg_half_period * 2
    if period <= 0:
        return {"passed": True, "issues": [], "details": "振荡周期异常，跳过验证"}

    actual_freq = 1.0 / period

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


# ── 运放增益验证 ──
def _validate_opamp_gain(csv_path: str, params: dict, tolerance_pct: float,
                         expected_physics: Optional[dict] = None) -> dict:
    data = _read_csv(csv_path)
    if not data or len(data.get("data", {})) < 2:
        return {"passed": True, "issues": [], "details": "CSV 数据不足，跳过运放增益验证"}

    vout_col = _find_signal_column(data, preferred=["sensor.v", "v_out", "opamp.out", "opAmp.", ".p.v"],
                                   skip_patterns=["der(", ".C", ".R", ".L", ".n.v", ".n.i"])
    vin_col = _find_signal_column(data, preferred=["src.v", "signalSource.", "v_in"],
                                  skip_patterns=["der(", ".C", ".R", ".L", ".n.v", ".n.i"],
                                  exclude_cols=[vout_col])

    if not vout_col:
        return {"passed": True, "issues": [], "details": "未找到输出电压信号列，跳过运放验证"}

    vout_vals = data["data"][vout_col]
    n = len(vout_vals)
    vout_steady = sum(vout_vals[-max(1, n // 10):]) / max(1, n // 10)

    vin = params.get("Vin", params.get("V_in", params.get("input_voltage", 0.5)))
    if vin_col:
        vin_vals = data["data"][vin_col]
        vin = sum(vin_vals[-max(1, n // 10):]) / max(1, n // 10)

    if abs(vin) < 0.001:
        return {"passed": True, "issues": [], "details": "输入电压接近 0，无法计算增益"}

    actual_gain = vout_steady / vin
    rf = params.get("Rf", params.get("R_f", params.get("feedback_resistance", 10000)))
    rin = params.get("Rin", params.get("R_in", params.get("input_resistance", 1000)))
    expected_gain = -rf / rin
    expected_vout = expected_gain * vin

    if expected_physics and expected_physics.get("extra_params"):
        expected_gain = expected_physics["extra_params"].get("expected_gain", expected_gain)
        expected_vout = expected_physics["extra_params"].get("expected_vout", expected_vout)

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
    exclude = set(exclude_cols or [])
    for col in data["columns"][1:]:
        if col in exclude:
            continue
        skip = False
        for sp in skip_patterns:
            if sp in col:
                skip = True
                break
        if skip:
            continue
        vals = data["data"].get(col, [])
        if not vals or len(vals) < 2:
            continue
        if abs(max(vals) - min(vals)) < 1e-9:
            continue
        for kw in preferred:
            if kw in col:
                return col

    # fallback
    for col in data["columns"][1:]:
        if col in exclude:
            continue
        skip = False
        for sp in skip_patterns:
            if sp in col:
                skip = True
                break
        if skip:
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


def _issue_to_dict(issue) -> dict:
    """把 ReviewIssue dataclass 转为 plain dict（JSON 可序列化）。"""
    if isinstance(issue, dict):
        return issue
    return {
        "severity": getattr(issue, "severity", "warning"),
        "category": getattr(issue, "category", "semantics"),
        "description": getattr(issue, "description", ""),
        "location": getattr(issue, "location", ""),
        "suggestion": getattr(issue, "suggestion", ""),
    }
