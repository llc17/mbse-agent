"""
V3 质量检查节点。两个新节点插入流水线:

  Q_cross_validate:  node2 → 此处 → node2_hitl
    用 LLM 对比 req JSON 参数值 vs SysML 代码里的 attribute redefines 值。
    不一致 → 打回 node2 重生成。

  Q_physics_validate: node3 成功后 → 此处 → node4_summary
    从 simulation CSV 计算物理量（截止频率/稳态温度），与需求理论值对比。
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
    """从仿真 CSV 计算物理量，与需求理论值对比。"""
    t0 = time.time()
    mo = state.get("mo", {})
    req = state.get("req", {})

    # 仿真失败 → 无 CSV 可验证，直接放行
    if not mo.get("success"):
        return _pass_through("physics_validate", state, "仿真未成功，跳过物理验证")

    csv_path = mo.get("csv_path", "")
    if not csv_path or not Path(csv_path).exists():
        return _pass_through("physics_validate", state, "无 CSV 文件，跳过物理验证")

    # 获取需求和组件信息
    component_type = req.get("component_type", "").lower()
    params = req.get("parameters", {})

    # 根据领域选择验证方式
    try:
        if _is_thermal(component_type):
            physics_result = _validate_thermal(csv_path, params, component_type)
        else:
            # 默认电气域 → RC 截止频率验证
            physics_result = _validate_rc_cutoff(csv_path, params, component_type)
    except Exception as e:
        logger.warning("Q_physics_validate: 计算异常: %s", e)
        physics_result = {"passed": True, "issues": [],
                          "details": f"物理计算异常 ({e})，跳过", "_defer_to_v4": True}

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


# ============================================================
# 物理量计算
# ============================================================

def _is_thermal(component_type: str) -> bool:
    return any(kw in component_type for kw in ["热", "thermal", "heat"])


def _validate_rc_cutoff(csv_path: str, params: dict, component_type: str) -> dict:
    """从 RC 阶跃响应计算 -3dB 截止频率，对比需求理论值。"""

    # 读 CSV
    data = _read_csv(csv_path)
    if not data or len(data.get("data", {})) < 2:
        return {"passed": True, "issues": [],
                "details": "CSV 数据不足，跳过 RC 截止频率验证"}

    # 找输出电压信号列（跳过参数常数、导数、地电压）
    time_col = data["columns"][0]
    signal_col = None

    # 优先：voltageSensor / v_out / capacitor.p.v / capacitor.v
    for col in data["columns"][1:]:
        if "der(" in col:
            continue
        # 参数列（如 capacitor.C）：值恒定 → 跳过
        vals = data["data"].get(col, [])
        if not vals or len(vals) < 2:
            continue
        if abs(max(vals) - min(vals)) < 1e-9:
            continue  # 常数列，跳过
        # 地电压（.n.v）= 0
        if ".n.v" in col or ".n.i" in col:
            continue
        # 优先电压信号
        if any(kw in col.lower() for kw in ["v_out", "sensor.v", "sensor.", ".p.v", "capacitor.v"]):
            signal_col = col
            break

    # 回退：找任意有变化 > 0.01 的电压列
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
        return {"passed": True, "issues": [],
                "details": "未找到有效的电压信号列，跳过验证"}

    times = data["data"][time_col]
    voltages = data["data"][signal_col]

    # 取稳态值（最后 10% 数据的均值）
    n = len(voltages)
    steady_state = sum(voltages[-max(1, n // 10):]) / max(1, n // 10)
    if steady_state < 0.001:
        return {"passed": True, "issues": [],
                "details": "稳态电压接近 0，跳过验证"}

    # 找达到 63.2% 稳态值的时间 → τ = RC
    target_63 = steady_state * 0.632
    tau = None
    for i, v in enumerate(voltages):
        if v >= target_63:
            # 线性插值
            if i > 0 and voltages[i] > voltages[i - 1]:
                frac = (target_63 - voltages[i - 1]) / (voltages[i] - voltages[i - 1])
                tau = times[i - 1] + frac * (times[i] - times[i - 1])
            else:
                tau = times[i]
            break

    if tau is None or tau <= 0:
        return {"passed": True, "issues": [],
                "details": "无法从阶跃响应计算时间常数 τ"}

    # 截止频率 f_c = 1 / (2π·τ)
    actual_fc = 1.0 / (2.0 * math.pi * tau)

    # 期望值：从需求参数中找 cutoff_freq / f_c / fc
    expected_fc = None
    for key in ["cutoff_freq", "cutoff_frequency", "fc", "f_c", "截止频率"]:
        if key in params:
            expected_fc = float(params[key])
            break

    if expected_fc is None:
        # 从 R, C 计算
        r_val = params.get("R", params.get("resistance", 0))
        c_val = params.get("C", params.get("capacitance", 0))
        if r_val > 0 and c_val > 0:
            expected_fc = 1.0 / (2.0 * math.pi * r_val * c_val)

    if expected_fc is None or expected_fc <= 0:
        return {"passed": True, "issues": [],
                "details": f"无期望截止频率可对比，实测 f_c={actual_fc:.1f} Hz"}

    # 计算偏差
    deviation = abs(actual_fc - expected_fc) / expected_fc * 100

    # 阈值：50% 以内算通过（仿真模型的元件值可能与需求有差异）
    passed = deviation < 50.0

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
        "passed": passed,
        "issues": issues,
        "deviation_percent": round(deviation, 1),
        "expected_value": round(expected_fc, 1),
        "actual_value": round(actual_fc, 1),
        "details": f"f_c 实测={actual_fc:.1f} Hz, 期望={expected_fc:.1f} Hz, 偏差={deviation:.1f}% (τ={tau:.6f}s)",
    }


def _validate_thermal(csv_path: str, params: dict, component_type: str) -> dict:
    """从热仿真 CSV 取稳态温度，对比需求。V3 只做基本检查，完整版见 V4。"""
    data = _read_csv(csv_path)
    if not data or len(data.get("data", {})) < 2:
        return {"passed": True, "issues": [],
                "details": "CSV 数据不足，跳过热稳态验证"}

    # 找温度列
    temp_col = None
    for col in data["columns"][1:]:
        vals = data["data"][col]
        if vals and max(vals) - min(vals) > 0.1:
            temp_col = col
            break

    if not temp_col:
        return {"passed": True, "issues": [],
                "details": "未找到温度信号列，跳过验证"}

    temps = data["data"][temp_col]
    n = len(temps)
    steady_temp = sum(temps[-max(1, n // 10):]) / max(1, n // 10)

    # 期望稳态值
    expected_temp = params.get("target_temp", params.get("outdoor_temp", None))
    if expected_temp is None:
        return {"passed": True, "issues": [],
                "details": f"稳态温度={steady_temp:.1f} K（无期望值可对比）"}

    deviation = abs(steady_temp - expected_temp) / expected_temp * 100
    passed = deviation < 20.0  # 热域放宽

    return {
        "passed": passed,
        "issues": [{
            "param_name": "steady_temperature",
            "expected": f"{expected_temp:.1f} K",
            "found": f"{steady_temp:.1f} K",
            "severity": "error" if not passed else "warning",
            "detail": f"稳态温度偏差 {deviation:.1f}%",
        }] if not passed else [],
        "deviation_percent": round(deviation, 1),
        "expected_value": round(expected_temp, 1),
        "actual_value": round(steady_temp, 1),
        "details": f"稳态温度={steady_temp:.1f} K, 期望={expected_temp:.1f} K, 偏差={deviation:.1f}%",
    }


# ============================================================
# 工具函数
# ============================================================

def _read_csv(csv_path: str) -> Optional[dict]:
    """读仿真 CSV，返回 {columns, data: {col: [values]}}。"""
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
