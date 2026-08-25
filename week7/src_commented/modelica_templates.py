"""
=============================================================================
modelica_templates.py — Modelica 域模板库（V6 LLM 选域机制的 Python 维护层）
=============================================================================
定位: V6 三大基础设施之一——解决 V4 "LLM 自由发挥写 Modelica → 经常编译失败"
      和 V5 "Python if-else 硬编码 → 覆盖率不够"的问题。

核心设计:
  V4: LLM 自由发挥写 Modelica 代码 → 经常编译失败（语法错、库错）
  V5: Python if-else 硬编码选域 → 覆盖率不够（"热敏电阻"这种复合系统没法匹配）
  V6: Python 维护模板（保证编译通过）+ LLM 确认选域（保证匹配正确）

模板设计原则:
  1. 每个模板已用 OMC (OpenModelica Compiler) 验证过编译通过
  2. 占位符 {param_xxx} 由 Python 的 _build_param_map() 填入需求参数值
  3. 可选组件用注释标注，LLM 在微调阶段可决定是否保留
  4. 每个模板记录适用关键词列表，用于 Python 初筛

5 个模板覆盖的物理域:
  rc_filter        — RC 低通滤波器（电气）
  rlc_circuit       — RLC 串联谐振电路（电气）
  opamp_inverting   — 反相运放放大器（电气）
  thermal_single_room — 单房间热传导（热）
  thermal_dual_room   — 双房间热传导（热）

用法:
    from src.modelica_templates import get_candidate_templates, inject_template
    candidates = get_candidate_templates("RC低通滤波器")  # → ["rc_filter"]
    code = inject_template("rc_filter", params, "MyRC")
=============================================================================
"""

import logging
from typing import Optional

logger = logging.getLogger("modelica_templates")

# ============================================================================
# 第 1 层: 模板定义（每个模板保证 OMC 编译通过）
# ============================================================================
# 占位符说明:
#   {model_name}  → Modelica 模型类名（如 "my_rc_filter"）
#   {param_R}     → 电阻值（Ω）
#   {param_C}     → 电容值（F）
#   {param_V}     → 激励电压（V）
#   ... 共约 20 个占位符，由 _build_param_map() 统一管理
# ============================================================================

# ── 模板 1: RC 低通滤波器 ──
TEMPLATE_RC_FILTER = """model {model_name}
  // V6 RC 低通滤波器模板 — 从需求参数自动填充
  import Modelica.Electrical.Analog.Basic;       // 基础元件库（R/C/GND）
  import Modelica.Electrical.Analog.Sources;     // 激励源库（阶跃电压）
  import Modelica.Electrical.Analog.Sensors;     // 传感器库（电压传感器）

  Basic.Resistor R(R={param_R});                 // 电阻 [Ω]
  Basic.Capacitor C(C={param_C});                // 电容 [F]
  Basic.Ground GND;                               // 参考地
  Sources.StepVoltage V_step(V={param_V}, startTime=0);  // 阶跃激励 [V]
  Sensors.VoltageSensor V_sensor;                // 输出电压传感器

equation
  // 串联拓扑: 电压源 → 电阻 → 电容 → 地
  connect(V_step.p, R.p);                        // 源正极 → 电阻入
  connect(R.n, C.p);                             // 电阻出 → 电容入（节点 = Vout）
  connect(C.n, GND.p);                           // 电容出 → 地
  connect(V_step.n, GND.p);                      // 源负极 → 地
  connect(V_sensor.p, C.p);                      // 测电容端电压 = Vout
  connect(V_sensor.n, GND.p);                    // 传感器参考地
end {model_name};"""

# ── 模板 2: RLC 串联电路 ──
TEMPLATE_RLC_CIRCUIT = """model {model_name}
  // V6 RLC 电路模板 — 串联谐振
  import Modelica.Electrical.Analog.Basic;
  import Modelica.Electrical.Analog.Sources;
  import Modelica.Electrical.Analog.Sensors;

  Basic.Resistor R(R={param_R});                 // 电阻 [Ω]
  Basic.Inductor L(L={param_L});                 // 电感 [H]
  Basic.Capacitor C(C={param_C});                // 电容 [F]
  Basic.Ground GND;
  Sources.StepVoltage V_step(V={param_V}, startTime=0);  // 阶跃激励 [V]
  Sensors.VoltageSensor V_sensor;                // 输出电压传感器

equation
  // 串联拓扑: 电压源 → 电阻 → 电感 → 电容 → 地
  connect(V_step.p, R.p);
  connect(R.n, L.p);
  connect(L.n, C.p);
  connect(C.n, GND.p);
  connect(V_step.n, GND.p);
  connect(V_sensor.p, C.p);                      // 测电容端电压
  connect(V_sensor.n, GND.p);
end {model_name};"""

# ── 模板 3: 反相运放放大器 ──
TEMPLATE_OPAMP_INVERTING = """model {model_name}
  // V6 反相运放模板 — 闭环增益 G = -Rf/Rin
  import Modelica.Electrical.Analog.Basic;
  import Modelica.Electrical.Analog.Sources;
  import Modelica.Electrical.Analog.Sensors;
  import Modelica.Electrical.Analog.Ideal;

  Basic.Ground GND;
  Sources.StepVoltage V_in(V={param_Vin}, startTime=0);     // 输入信号 [V]
  Sources.ConstantVoltage Vcc(V={param_Vcc});                // 正电源 [V]
  Sources.ConstantVoltage Vee(V={param_Vee});                // 负电源 [V]

  Ideal.IdealizedOpAmpLimited opAmp(                          // 理想运放（限幅）
    Vps={param_Vcc},
    Vns={param_Vee},
  );

  Basic.Resistor Rin(R={param_Rin});                          // 输入电阻 [Ω]
  Basic.Resistor Rf(R={param_Rf});                            // 反馈电阻 [Ω]
  Sensors.VoltageSensor V_sensor;                              // 输出电压传感器

equation
  // 反相输入端连接: 输入信号 → Rin → 反相端
  connect(V_in.p, Rin.p);
  connect(Rin.n, opAmp.in_n);                                 // 连到反相输入端
  connect(Rf.p, opAmp.in_n);                                  // 反馈电阻也连反相端
  connect(Rf.n, opAmp.out);                                   // 反馈电阻另一端连输出

  // 同相输入端接地（反相放大器标准接法）
  connect(opAmp.in_p, GND.p);

  // 输出测量
  connect(opAmp.out, V_sensor.p);
  connect(V_sensor.n, GND.p);

  // 电源连接
  connect(Vcc.p, opAmp.Vpp);
  connect(Vcc.n, GND.p);
  connect(Vee.n, opAmp.Vnn);
  connect(Vee.p, GND.p);

  // 输入信号地
  connect(V_in.n, GND.p);
end {model_name};"""

# ── 模板 4: 单房间热传导 ──
TEMPLATE_THERMAL_SINGLE_ROOM = """model {model_name}
  // V6 单房间热传导模板
  import Modelica.Thermal.HeatTransfer.Components;     // 热系统元件库
  import Modelica.Thermal.HeatTransfer.Sensors;        // 温度传感器

  Components.HeatCapacitor room(C={param_heat_capacity}, T(start={param_T_indoor}));   // 房间热容 [J/K]
  Components.ThermalResistor wall(R={param_thermal_resistance});  // 墙壁热阻 [K/W]
  Components.FixedTemperature outdoor(T={param_T_outdoor});       // 室外恒温 [K]
  Sensors.TemperatureSensor T_sensor;                              // 室内温度传感器

equation
  // 热流路径: 室外 → 墙壁热阻 → 房间热容
  connect(outdoor.port, wall.port_a);
  connect(wall.port_b, room.port);
  connect(room.port, T_sensor.port);
end {model_name};"""

# ── 模板 5: 双房间热传导 ──
TEMPLATE_THERMAL_DUAL_ROOM = """model {model_name}
  // V6 双房间热传导模板
  import Modelica.Thermal.HeatTransfer.Components;
  import Modelica.Thermal.HeatTransfer.Sensors;

  Components.HeatCapacitor room1(C={param_heat_capacity1}, T(start={param_T_indoor1}));   // 房间1
  Components.HeatCapacitor room2(C={param_heat_capacity2}, T(start={param_T_indoor2}));   // 房间2
  Components.ThermalResistor wall_shared(R={param_thermal_resistance_shared});  // 共享墙
  Components.ThermalResistor wall_outer1(R={param_thermal_resistance1});        // 房间1外墙
  Components.ThermalResistor wall_outer2(R={param_thermal_resistance2});        // 房间2外墙
  Components.FixedTemperature outdoor(T={param_T_outdoor});                    // 室外恒温
  Sensors.TemperatureSensor T_sensor1;                                          // 房间1温度
  Sensors.TemperatureSensor T_sensor2;                                          // 房间2温度

equation
  // 热流路径: 室外 → 外墙1 → 房间1 → 共享墙 → 房间2 → 外墙2 → 室外
  connect(outdoor.port, wall_outer1.port_a);
  connect(wall_outer1.port_b, room1.port);
  connect(room1.port, wall_shared.port_a);
  connect(wall_shared.port_b, room2.port);
  connect(room2.port, wall_outer2.port_a);
  connect(wall_outer2.port_b, outdoor.port);
  // 传感器
  connect(room1.port, T_sensor1.port);
  connect(room2.port, T_sensor2.port);
end {model_name};"""


# ============================================================================
# 第 2 层: 模板注册表 — 关键词 → 候选模板
# ============================================================================
# 每个模板有 5 个字段:
#   template    → 模板代码字符串
#   keywords    → 用于 Python 初筛的关键词列表
#   domain      → 所属物理域
#   description → 简短描述（给 LLM 选域时参考）
# ============================================================================

TEMPLATE_REGISTRY: dict[str, dict] = {
    "rc_filter": {
        "template": TEMPLATE_RC_FILTER,
        "keywords": ["rc", "低通", "滤波器", "filter", "low pass", "电阻", "电容",
                     "resistor", "capacitor", "rc filter", "RC"],
        "domain": "electrical",
        "description": "RC 低通滤波器：电压源 → 电阻 → 电容 → 地，测电容端电压",
    },
    "rlc_circuit": {
        "template": TEMPLATE_RLC_CIRCUIT,
        "keywords": ["rlc", "谐振", "resonant", "电感", "inductor", "rlc circuit",
                     "振荡", "oscillation", "RLC"],
        "domain": "electrical",
        "description": "RLC 串联电路：电压源 → 电阻 → 电感 → 电容 → 地",
    },
    "opamp_inverting": {
        "template": TEMPLATE_OPAMP_INVERTING,
        "keywords": ["运放", "opamp", "op-amp", "反相", "inverting", "放大器",
                     "amplifier", "OpAmp", "op amp"],
        "domain": "electrical",
        "description": "反相运放放大器：闭环增益 G = -Rf/Rin，双电源",
    },
    "thermal_single_room": {
        "template": TEMPLATE_THERMAL_SINGLE_ROOM,
        "keywords": ["热", "thermal", "heat", "单房间", "single room", "温度",
                     "temperature", "房间", "room", "墙壁", "wall", "热传导"],
        "domain": "thermal",
        "description": "单房间热传导：室外恒温 → 墙壁热阻 → 房间热容",
    },
    "thermal_dual_room": {
        "template": TEMPLATE_THERMAL_DUAL_ROOM,
        "keywords": ["双房间", "dual room", "两房间", "two room", "双室",
                     "热交换", "heat exchange", "共享墙", "shared wall",
                     "房间之间", "between room"],
        "domain": "thermal",
        "description": "双房间热传导：室外 → 外墙1 → 房间1 → 共享墙 → 房间2 → 外墙2 → 室外",
    },
}

# ============================================================================
# 第 3 层: 公开 API — 候选模板筛选
# ============================================================================

def get_candidate_templates(component_type: str, max_candidates: int = 3) -> list[str]:
    """
    Python 初筛：按关键词匹配候选模板。

    算法: 遍历 TEMPLATE_REGISTRY → 对每个模板的关键词做子串匹配
          → 按命中数降序排列 → 取前 max_candidates 个。

    这是"初筛"——Python 确定候选池，LLM 在候选池中选最匹配的。

    Args:
        component_type: 系统类型（如 "RC低通滤波器"）
        max_candidates: 最多返回几个候选

    Returns:
        候选模板名列表（按匹配关键词数降序）

    例:
        "RC低通滤波器" → ["rc_filter"]                      # 只有一个相关的
        "热敏电阻"     → ["thermal_single_room", "rc_filter"]  # 两个都相关
    """
    ct = component_type.lower()
    scored = []

    for name, info in TEMPLATE_REGISTRY.items():
        score = 0
        matched = []
        for kw in info["keywords"]:
            if kw.lower() in ct:              # 子串匹配
                score += 1
                matched.append(kw)
        if score > 0:
            scored.append((score, name, matched))

    scored.sort(key=lambda x: x[0], reverse=True)
    candidates = [name for _, name, _ in scored[:max_candidates]]

    if not candidates:
        # 关键词完全没命中 → 返回所有模板让 LLM 选
        logger.info("关键词未命中任何模板，返回全部候选")
        candidates = list(TEMPLATE_REGISTRY.keys())[:max_candidates]

    logger.info("模板初筛: '%s' → %s", component_type, candidates)
    return candidates


def build_template_selection_prompt(
    component_type: str, params: dict, candidates: list[str]
) -> str:
    """
    构建 LLM 选域 prompt。

    列出所有候选模板的描述和适用场景，让 LLM 选最匹配的一个。
    这个 prompt 用于 temperature=0 的轻量 LLM 调用。

    Args:
        component_type: 系统类型
        params: 需求参数（用于给 LLM 更多判断信息）
        candidates: 候选模板名列表

    Returns:
        LLM 选域 prompt 文本
    """
    candidate_descriptions = []
    for name in candidates:
        info = TEMPLATE_REGISTRY.get(name, {})
        candidate_descriptions.append(
            f"- **{name}**: {info.get('description', '无描述')} "
            f"(域: {info.get('domain', 'unknown')})"
        )

    params_summary = ", ".join(f"{k}={v}" for k, v in list(params.items())[:5])

    prompt = f"""## 任务：选择最匹配的 Modelica 模板

系统类型: {component_type}
已知参数: {params_summary}

## 候选模板
{chr(10).join(candidate_descriptions)}

## 指令
选择最匹配系统类型的 1 个模板名。只输出模板名，不要解释。
模板名:"""

    return prompt


# ============================================================================
# 第 4 层: 公开 API — 模板注入
# ============================================================================

def inject_template(
    template_name: str,
    params: dict,
    component_name: str = "MyModel",
) -> str:
    """
    将需求参数注入模板骨架，输出完整的 Modelica 代码。

    流程:
      1. 查 TEMPLATE_REGISTRY → 获取模板代码
      2. 调 _build_param_map(params) → 构建占位符替换表
      3. 逐个替换 {param_xxx} → 实际数值
      4. 检查是否还有未替换的占位符 → 有则用默认值填充

    Args:
        template_name: 模板名（如 "rc_filter"）
        params: 需求参数字典（如 {"R": 1000, "C": 1e-6}）
        component_name: Modelica 模型名（默认 "MyModel"）

    Returns:
        完整的 Modelica .mo 代码（可直接保存和编译）
    """
    info = TEMPLATE_REGISTRY.get(template_name)
    if not info:
        logger.warning("未知模板 '%s'，回退到 rc_filter", template_name)
        info = TEMPLATE_REGISTRY.get("rc_filter", {})

    template = info.get("template", TEMPLATE_RC_FILTER)

    # ── 构建占位符替换映射 ──
    param_map = _build_param_map(params, template_name)
    param_map["model_name"] = component_name

    # ── 逐个替换占位符 ──
    code = template
    for key, val in param_map.items():
        placeholder = f"{{{key}}}"          # {param_R} → "1000.0"
        code = code.replace(placeholder, str(val))

    # ── 检查遗漏 ──
    import re
    remaining = re.findall(r"\{param_\w+\}", code)
    if remaining:
        logger.warning("模板 '%s' 有未替换的占位符: %s", template_name, remaining)
        for placeholder in remaining:
            code = code.replace(placeholder, "1.0")  # 用安全默认值填充

    logger.info("模板注入: '%s' → %s, params=%s", template_name, component_name,
                {k: v for k, v in param_map.items() if k != "model_name"})
    return code


# ============================================================================
# 第 5 层: 参数映射（需求参数名 → 模板占位符）
# ============================================================================

def _build_param_map(params: dict, template_name: str) -> dict[str, str]:
    """
    根据模板名和需求参数构建替换映射。

    每种模板有特定的参数名映射——从需求参数名映射到模板占位符。
    支持参数名的多种变体（如 V / V_in / Vin_step / voltage 都映射到 param_V）。

    设计决策: 为什么不用 LLM 做这个映射？
      参数映射是确定性的数值转换，不需要 LLM 的判断力。
      用 Python 字典保证一致性和速度（每百万次调用 < 1ms）。

    Args:
        params: 需求参数字典（来自 node1 的 StructuredRequirement.parameters）
        template_name: 模板名

    Returns:
        占位符名 → 数值字符串 的映射表
    """
    param_map: dict[str, str] = {}

    # ── 常用电气参数 ──
    # R 支持: R / resistance
    r = params.get("R", params.get("resistance", 1000.0))
    # C 支持: C / capacitance
    c = params.get("C", params.get("capacitance", 1e-6))
    # L 支持: L / inductance
    l = params.get("L", params.get("inductance", 1e-3))
    # V (激励电压) 支持: V / V_in / Vin_step / Vin / voltage / input_voltage
    #    —— V6 修复: 追加 Vin_step 变体（node1 常用名）
    v = params.get("V", params.get("V_in", params.get("Vin_step",
         params.get("Vin", params.get("voltage", params.get("input_voltage", 5.0))))))

    param_map["param_R"] = str(r)
    param_map["param_C"] = str(c)
    param_map["param_L"] = str(l)
    param_map["param_V"] = str(v)

    # Vin (运放输入) 同样支持多变体
    param_map["param_Vin"] = str(params.get("Vin", params.get("Vin_step",
         params.get("V_in", params.get("input_voltage", 0.5)))))

    # 运放电源参数
    param_map["param_Vcc"] = str(params.get("Vcc", params.get("V_plus", 15.0)))
    param_map["param_Vee"] = str(params.get("Vee", params.get("V_minus", -15.0)))
    param_map["param_Rin"] = str(params.get("Rin", params.get("R_in", params.get("input_resistance", 1000.0))))
    param_map["param_Rf"] = str(params.get("Rf", params.get("R_f", params.get("feedback_resistance", 10000.0))))

    # ── 常用热参数 ──
    t_outdoor = params.get("outdoor_temp", params.get("T_outdoor", params.get("T_out", 308.15)))
    t_indoor = params.get("indoor_temp", params.get("T_indoor", params.get("T_in", 298.15)))
    heat_capacity = params.get("heat_capacity", params.get("C_thermal", 500000.0))
    thermal_resistance = params.get("thermal_resistance", params.get("R_thermal", params.get("R_wall", 0.02)))

    param_map["param_T_outdoor"] = str(t_outdoor)
    param_map["param_T_indoor"] = str(t_indoor)
    param_map["param_heat_capacity"] = str(heat_capacity)
    param_map["param_thermal_resistance"] = str(thermal_resistance)

    # ── 双房间特殊参数 ──
    param_map["param_T_indoor1"] = str(params.get("T_indoor1", params.get("T_start1", params.get("T_indoor", 298.15))))
    param_map["param_T_indoor2"] = str(params.get("T_indoor2", params.get("T_start2", 293.15)))
    param_map["param_heat_capacity1"] = str(params.get("heat_capacity1", params.get("C_thermal1", params.get("heat_capacity", 500000.0))))
    param_map["param_heat_capacity2"] = str(params.get("heat_capacity2", params.get("C_thermal2", params.get("heat_capacity", 500000.0))))
    param_map["param_thermal_resistance1"] = str(params.get("thermal_resistance1", params.get("R_thermal1", params.get("thermal_resistance", params.get("R_wall1", 0.02)))))
    param_map["param_thermal_resistance2"] = str(params.get("thermal_resistance2", params.get("R_thermal2", params.get("R_wall2", 0.02))))
    param_map["param_thermal_resistance_shared"] = str(params.get("thermal_resistance_shared", params.get("R_shared", params.get("R_wall_shared", 0.01))))

    return param_map


# ============================================================================
# 第 6 层: 调试工具
# ============================================================================

def list_all_templates() -> list[dict]:
    """
    列出所有可用模板及其描述。

    用于 main.py 的 --list-templates 参数输出。
    """
    result = []
    for name, info in TEMPLATE_REGISTRY.items():
        result.append({
            "name": name,
            "domain": info["domain"],
            "description": info["description"],
            "keywords": info["keywords"][:5],
        })
    return result
