"""Static V9 contract for the guarded online requirement projection.

This module deliberately contains instructions only.  It has no request
parameter, does not inspect regression cases, and does not construct a local
answer for an online request.  The V8 parser and verifier remain authoritative.
"""

from __future__ import annotations

from week9.src.schemas import sha256_text


PROMPT_VERSION: str = "v9-certified-requirement-projection-prompt-1"
PROJECTION_SCHEMA: str = "v8-certified-requirement-projection-1"
REL_TOL: float = 1e-12
ABS_TOL: float = 1e-15

# Immutable V8 provenance captured in week10/configs/v8_source_hashes.json.
V8_SOURCE_HASHES: dict[str, str] = {
    "week9/src/generation.py": "3e1b3c242a417699fdefc4277fe04c1885c9625f4fa501031dd6bec9187c359d",
    "week9/src/modelica_templates.py": "2fd30c798f4e20b70247770192f8df41054343d231d0b584a01133826875cad2",
    "week9/src/online_generation.py": "ce689879b03ccff9672ffd30fd8a85f996b03d2f9c118342dff2be8442e0a4f5",
    "week9/src/traceability.py": "c18fa1cd3baa08f1df346f1180b06929b0b8d5a96c5f2d3e5d358c14a6fae1ad",
}

TEMPLATE_CONTRACTS: dict[str, dict[str, object]] = {
    "rc_filter": {
        "fields": ("resistance", "capacitance", "cutoff_frequency", "input_voltage"),
        "units": {"resistance": "Ohm", "capacitance": "F", "cutoff_frequency": "Hz", "input_voltage": "V"},
        "stimulus": "must contain 阶跃 and must not contain 正弦",
    },
    "rlc_filter": {
        "fields": ("resistance", "inductance", "capacitance", "cutoff_frequency", "input_voltage"),
        "units": {"resistance": "Ohm", "inductance": "H", "capacitance": "F", "cutoff_frequency": "Hz", "input_voltage": "V"},
        "stimulus": "must contain 正弦 and must not contain 阶跃",
    },
    "opamp_inverting": {
        "fields": ("input_voltage", "input_resistance", "feedback_resistance", "positive_supply", "negative_supply"),
        "units": {"input_voltage": "V", "input_resistance": "Ohm", "feedback_resistance": "Ohm", "positive_supply": "V", "negative_supply": "V"},
        "stimulus": "no extra stimulus-text check",
    },
    "thermal_single_room": {
        "fields": ("heat_capacity", "thermal_resistance", "initial_temperature", "outdoor_temperature"),
        "units": {"heat_capacity": "J/K", "thermal_resistance": "K/W", "initial_temperature": "K", "outdoor_temperature": "K"},
        "stimulus": "no extra stimulus-text check",
    },
    "thermal_dual_room": {
        "fields": ("heat_capacity_a", "heat_capacity_b", "shared_resistance", "outdoor_resistance_a", "outdoor_resistance_b", "initial_temperature_a", "initial_temperature_b", "outdoor_temperature"),
        "units": {"heat_capacity_a": "J/K", "heat_capacity_b": "J/K", "shared_resistance": "K/W", "outdoor_resistance_a": "K/W", "outdoor_resistance_b": "K/W", "initial_temperature_a": "K", "initial_temperature_b": "K", "outdoor_temperature": "K"},
        "stimulus": "no extra stimulus-text check",
    },
}

CAPTURE_RULES: dict[str, str] = {
    "number": r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)",
    "resistance": r"MΩ|kΩ|Ω|MOhm|kOhm|Ohm|ohm",
    "capacitance": r"mF|uF|μF|µF|nF|pF|F",
    "inductance": r"mH|uH|H",
    "frequency": r"MHz|kHz|Hz|mhz|khz|hz",
    "voltage": r"mV|V",
    "heat_capacity": r"MJ/K|kJ/K|J/K",
    "thermal_resistance": r"K/W",
    "temperature": r"°C|℃|degC|K",
}

SYSTEM_PROMPT: str = r'''You are the constrained requirement projection step of V9. Return exactly one JSON object and nothing else. Do not emit markdown fences, outer whitespace, prose, or extra keys. This is a proposal for the frozen V8 verifier; never infer missing values, units, topology, or stimulus.

Schema is exactly {"schema_version":"v8-certified-requirement-projection-1","status":"ready|blocked","template_id":"string","observations":[{"name":"string","value":0.0,"unit":"string","source_text":"exact input substring"}],"issues":[{"code":"string","message":"string"}]}. Numeric value is a finite JSON int or float, never bool. ready requires nonempty observations and empty issues; blocked requires empty observations and nonempty issues. No extra or omitted fields are allowed.

Only select one certified topology. rc_filter requires resistance, capacitance, cutoff_frequency, input_voltage with downstream target units Ohm, F, Hz, V. rlc_filter requires resistance, inductance, capacitance, cutoff_frequency, input_voltage with Ohm, H, F, Hz, V. opamp_inverting requires input_voltage, input_resistance, feedback_resistance, positive_supply, negative_supply with V, Ohm, Ohm, V, V. thermal_single_room requires heat_capacity, thermal_resistance, initial_temperature, outdoor_temperature with J/K, K/W, K, K. thermal_dual_room requires heat_capacity_a, heat_capacity_b, shared_resistance, outdoor_resistance_a, outdoor_resistance_b, initial_temperature_a, initial_temperature_b, outdoor_temperature with J/K, J/K, K/W, K/W, K/W, K, K, K. Names must be exactly these canonical names. Topology regexes are RLC\s*(?:串联)?低通滤波器; (?<![A-Za-z])RC(?![A-Za-z])\s*(?:串联)?低通滤波器 (the RC letter boundaries are required); 反相(?:运算)?(?:放大器|运放); 单房间热传导系统; 双房间热传导系统. Multiple different topology hits or no hit is blocked. RC must have 阶跃 and no 正弦; RLC must have 正弦 and no 阶跃; the other three add no stimulus-text condition.

For direct observations, retain the exact regex group(0) input substring as source_text: label, every matched whitespace, number, and unit, excluding surrounding punctuation or description. Every field pattern is LABEL\s*(?P<value>NUMBER)\s*(?P<unit>UNIT), except where LABEL itself contains whitespace rules below; `\s*` occurs between label and number and between number and unit, and every such matched character belongs in source_text. Use these exact label patterns: RC cutoff_frequency 截止频率; RC resistance (?<!输入)(?<!反馈)电阻(?:值)?; RC input_voltage (?:输入电压|输入(?:为|是)?|源电压); RLC cutoff_frequency (?:截止|谐振)频率; RLC resistance (?<!输入)(?<!反馈)(?:串联)?电阻(?:值)?; RLC inductance (?:串联)?电感; RLC input_voltage (?:输入电压|输入(?:为|是)?|源电压); each RC/RLC explicit capacitance 电容(?:值)?; opamp input_voltage 输入电压, input_resistance 输入电阻, feedback_resistance 反馈电阻, positive_supply 正电源, negative_supply 负电源; single room heat_capacity (?:房间)?热容, thermal_resistance (?:墙体)?热阻, initial_temperature (?:室内)?初始温度, outdoor_temperature (?:室外温度|室外); dual room heat_capacity_a 房间\s*A\s*热容, heat_capacity_b 房间\s*B\s*热容, shared_resistance 两房间之间(?:墙体)?热阻, outdoor_resistance_a 房间\s*A\s*外墙热阻, outdoor_resistance_b 房间\s*B\s*外墙热阻, outdoor_temperature (?:室外温度|室外), initial_temperature_a 房间\s*A\s*初始温度, initial_temperature_b 房间\s*B\s*初始温度. Retain the captured numeric value and original unit; do not convert it to the downstream target unit. Labels and units use the frozen IGNORECASE regex behavior. Captured unit alternatives are resistance MΩ/kΩ/Ω/MOhm/kOhm/Ohm/ohm; capacitance mF/uF/μF/µF/nF/pF/F; inductance mH/uH/H; frequency MHz/kHz/Hz/mhz/khz/hz; voltage mV/V; heat capacity MJ/K/kJ/K/J/K; thermal resistance K/W; temperature °C/℃/degC/K. IGNORECASE matching and unit capture do not guarantee a unit is convertible downstream: preserve the actual original unit and leave normalization to V8. Number syntax is [+-]?(?:\d+(?:\.\d*)?|\.\d+); do not recognize scientific notation.

For repeated occurrences of one field, V8 converts to the downstream target unit and compares with rel_tol=1e-12 and abs_tol=1e-15. Equivalent repeats use the first occurrence's original value, unit, and span. Conflicting repeats are blocked. For dual rooms, a successful joint pattern 两房间初始温度均(?:为)?\s*(?P<value>NUMBER)\s*(?P<unit>TEMPERATURE_UNIT) supplies both initial_temperature_a and initial_temperature_b using the same complete span, and takes priority over separately listed initial temperatures. A conflicting joint value blocks. Do not invent any additional rejection rule for a joint and separate initial-temperature combination.

An explicit capacitance has priority. Derive capacitance only when explicit capacitance is absent and exactly one certified calculation phrase appears: RC 电容根据截止频率计算/计算所需电容, formula C=1/(2*pi*R*fc); RLC 电容根据谐振频率计算/计算所需电容, formula C=1/((2*pi*f0)^2*L). Inputs are normalized by V8 to Ohm/Hz/H before the formula. A derived observation has calculated value, unit F, and the complete calculation phrase as source_text. Any other wording, repeated calculation phrase, or missing inputs must be blocked rather than guessed.

Example of one field only (not a complete request): input substring "电阻 3 kOhm" yields observation {"name":"resistance","value":3,"unit":"kOhm","source_text":"电阻 3 kOhm"}. This example never establishes a topology or ready status.'''

PROMPT_SHA256: str = sha256_text(PROMPT_VERSION + "\n" + SYSTEM_PROMPT)


__all__ = [
    "ABS_TOL", "CAPTURE_RULES", "PROJECTION_SCHEMA", "PROMPT_SHA256", "PROMPT_VERSION",
    "REL_TOL", "SYSTEM_PROMPT", "TEMPLATE_CONTRACTS", "V8_SOURCE_HASHES",
]
