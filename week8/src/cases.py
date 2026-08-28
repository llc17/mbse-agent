"""V7 统一用例集。

设计要点（V7 修订版的核心修正之一）:
  V4 的 `week5/experiments/test_cases.json` 与 V6 的 `week7/main.py` PREDEFINED_CASES
  用例文本**不一致**（例如 V6 把 RC 电容值直接喂死 C=0.159μF，V4 让 LLM 自己算）。
  如果 benchmark 分别复用它俩各自预定义的用例，测出来的差异会混入"需求文本差异"，
  无法归因给"Agent 机制"。

  因此 V7 在这里**自持一套统一用例集**，同时喂给 V4 和 V6，唯一变量 = 有没有 Agent 机制。
  文本与 expected_physics 直接取自 V4 的 test_cases.json（那是更严谨、更完整的配置），
  保证两边的物理验证口径也完全一致。

用法:
    from cases import UNIFIED_CASES, get_case
    case = get_case("rc_lowpass")
"""

# 统一用例集。raw_input + expected_physics 取自 week5/experiments/test_cases.json。
# 这里只保留 V7 要跑的 3 个核心用例（RC 低通 / 双房间热 / RLC 谐振）。
UNIFIED_CASES: dict[str, dict] = {
    "rc_lowpass": {
        "domain": "electrical",
        "raw_input": "设计一个 RC 低通滤波器，截止频率 1kHz，电阻 1kΩ，电容根据截止频率计算，输入电压 5V 阶跃信号",
        "expected_physics": {
            "validate_type": "rc_cutoff",
            "expected_value_source": "params.cutoff_freq",
            "tolerance_pct": 10.0,
            "description": "截止频率 f_c = 1/(2πRC)",
        },
    },
    "dual_room_thermal": {
        "domain": "thermal",
        "raw_input": (
            "设计一个双房间热传导系统：房间A热容 300kJ/K，房间B热容 200kJ/K，"
            "两房间之间墙体热阻 0.05 K/W，房间A外墙热阻 0.03 K/W 连接到室外 35°C，"
            "房间B外墙热阻 0.04 K/W 连接到室外 35°C，两房间初始温度均为 20°C"
        ),
        "expected_physics": {
            "validate_type": "thermal_steady",
            "expected_value_source": "params.outdoor_temp",
            "tolerance_pct": 20.0,
            "description": "双房间最终都趋近室外温度 35°C (308.15K)，验证两个房间稳态温度",
        },
    },
    "rlc_lowpass": {
        "domain": "electrical",
        "raw_input": (
            "设计一个 RLC 串联低通滤波器，截止频率 10kHz，电阻 100Ω，电感 10mH，"
            "电容根据谐振频率计算，输入电压 5V 正弦波"
        ),
        "expected_physics": {
            "validate_type": "rlc_resonant_freq",
            "expected_value_source": "params.cutoff_freq",
            "tolerance_pct": 15.0,
            "description": "谐振频率 f_0 = 1/(2π√(LC))，应接近截止频率 10kHz",
            "extra_params": {"L": 0.01, "C_expected": 2.53e-8},
        },
    },
}


def get_case(case_id: str) -> dict | None:
    """按 id 取用例。找不到返回 None。"""
    return UNIFIED_CASES.get(case_id)


def list_case_ids() -> list[str]:
    """列出所有用例 id。"""
    return list(UNIFIED_CASES.keys())
