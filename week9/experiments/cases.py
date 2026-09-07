"""Frozen case contracts for V8 development and evaluation.

The holdout requirements are frozen before template work begins. They are not
used by unit tests or template selection during development, and this file does
not contain a reference Modelica/SysML answer.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from typing import Any, Mapping

from week9.src.schemas import CheckStatus


CORE_REQUIRED_CHECKS = (
    "requirement_normalization",
    "sysml_syntax",
    "modelica_compile",
    "parameter_unit_consistency",
    "simulation",
    "physical_behavior",
    "cross_model_consistency",
)


@dataclass(frozen=True, slots=True)
class CaseDefinition:
    case_id: str
    dataset: str
    domain: str
    raw_input: str
    required_checks: tuple[str, ...] = CORE_REQUIRED_CHECKS
    expected_physics: Mapping[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()

    def contract_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "dataset": self.dataset,
            "domain": self.domain,
            "raw_input": self.raw_input,
            "required_checks": list(self.required_checks),
            "expected_physics": dict(self.expected_physics),
            "tags": list(self.tags),
        }

    @property
    def sha256(self) -> str:
        payload = json.dumps(
            self.contract_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ErrorInjectionSpec:
    injection_id: str
    target_check: str
    injection_type: str
    expected_status: CheckStatus
    expected_failure_kind: str
    description: str

    def contract_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["expected_status"] = self.expected_status.value
        return data

    @property
    def sha256(self) -> str:
        payload = json.dumps(
            self.contract_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


DEVELOPMENT_REGRESSION_CASES: dict[str, CaseDefinition] = {
    "rc_lowpass": CaseDefinition(
        case_id="rc_lowpass",
        dataset="development_regression",
        domain="electrical",
        raw_input=(
            "设计一个 RC 低通滤波器，截止频率 1kHz，电阻 1kΩ，"
            "电容根据截止频率计算，输入电压 5V 阶跃信号"
        ),
        expected_physics={
            "validate_type": "rc_cutoff",
            "expected_value_source": "params.cutoff_freq",
            "tolerance_pct": 10.0,
        },
        tags=("known_template", "step_input"),
    ),
    "dual_room_thermal": CaseDefinition(
        case_id="dual_room_thermal",
        dataset="development_regression",
        domain="thermal",
        raw_input=(
            "设计一个双房间热传导系统：房间A热容 300kJ/K，房间B热容 200kJ/K，"
            "两房间之间墙体热阻 0.05 K/W，房间A外墙热阻 0.03 K/W 连接到室外 35°C，"
            "房间B外墙热阻 0.04 K/W 连接到室外 35°C，两房间初始温度均为 20°C"
        ),
        expected_physics={
            "validate_type": "thermal_steady",
            "expected_value_source": "params.outdoor_temp",
            "tolerance_pct": 20.0,
        },
        tags=("known_template", "known_v6_failure"),
    ),
    "rlc_lowpass": CaseDefinition(
        case_id="rlc_lowpass",
        dataset="development_regression",
        domain="electrical",
        raw_input=(
            "设计一个 RLC 串联低通滤波器，截止频率 10kHz，电阻 100Ω，电感 10mH，"
            "电容根据谐振频率计算，输入电压 5V 正弦波"
        ),
        expected_physics={
            "validate_type": "rlc_resonant_freq",
            "expected_value_source": "params.cutoff_freq",
            "tolerance_pct": 15.0,
            "extra_params": {"L": 0.01, "C_expected": 2.53e-8},
        },
        tags=("known_template", "sine_input", "known_v6_failure"),
    ),
}


HOLDOUT_CASES: dict[str, CaseDefinition] = {
    "rc_lowpass_parameter_variant": CaseDefinition(
        case_id="rc_lowpass_parameter_variant",
        dataset="holdout",
        domain="electrical",
        raw_input=(
            "设计一个 RC 低通滤波器，截止频率 2.5kHz，电阻 2.2kΩ，"
            "计算所需电容，输入为 3.3V 阶跃信号"
        ),
        expected_physics={
            "validate_type": "rc_cutoff",
            "expected_value_source": "params.cutoff_freq",
            "tolerance_pct": 10.0,
        },
        tags=("parameter_variant", "do_not_use_for_development"),
    ),
    "three_room_thermal_chain": CaseDefinition(
        case_id="three_room_thermal_chain",
        dataset="holdout",
        domain="thermal",
        raw_input=(
            "建立三个房间串联的热网络：房间 A、B、C 的热容分别为 250、300、220 kJ/K，"
            "A-B 与 B-C 之间的热阻分别为 0.04、0.06 K/W；A 和 C 再分别通过 0.03、0.05 K/W "
            "的外墙热阻连接同一个 32°C 恒温室外环境。三个房间初温分别为 20、21、19°C。"
            "生成可仿真的模型，并验证长期温度行为符合能量守恒和稳态边界。"
        ),
        expected_physics={
            "validate_type": "thermal_energy_and_steady_state",
            "tolerance_pct": 20.0,
        },
        tags=("uncovered_topology", "do_not_use_for_development"),
    ),
}


ERROR_INJECTION_CASES: dict[str, ErrorInjectionSpec] = {
    "missing_sysml": ErrorInjectionSpec(
        "missing_sysml", "sysml_syntax", "missing_artifact",
        CheckStatus.UNVERIFIED, "content", "SysML 产物缺失时必须阻断发布。",
    ),
    "missing_modelica": ErrorInjectionSpec(
        "missing_modelica", "modelica_compile", "missing_artifact",
        CheckStatus.UNVERIFIED, "content", "Modelica 产物缺失时必须阻断发布。",
    ),
    "missing_csv": ErrorInjectionSpec(
        "missing_csv", "simulation", "missing_artifact",
        CheckStatus.UNVERIFIED, "content", "仿真 CSV 缺失不能算成功。",
    ),
    "modelica_syntax_error": ErrorInjectionSpec(
        "modelica_syntax_error", "modelica_compile", "syntax_error",
        CheckStatus.FAIL, "content", "Modelica 语法错误必须由编译检查拦截。",
    ),
    "wrong_component_path": ErrorInjectionSpec(
        "wrong_component_path", "modelica_compile", "wrong_library_path",
        CheckStatus.FAIL, "content", "错误标准库组件路径必须被拦截。",
    ),
    "port_mismatch": ErrorInjectionSpec(
        "port_mismatch", "modelica_compile", "incompatible_ports",
        CheckStatus.FAIL, "content", "不兼容端口连接必须被拦截。",
    ),
    "validator_exception": ErrorInjectionSpec(
        "validator_exception", "physical_behavior", "validator_exception",
        CheckStatus.ERROR, "infrastructure", "验证器异常必须成为 ERROR。",
    ),
    "file_lock": ErrorInjectionSpec(
        "file_lock", "modelica_compile", "file_lock",
        CheckStatus.ERROR, "infrastructure", "文件锁属于基础设施错误，不送入内容修复。",
    ),
}


def get_case(case_id: str, *, include_holdout: bool = False) -> CaseDefinition | None:
    case = DEVELOPMENT_REGRESSION_CASES.get(case_id)
    if case is not None:
        return case
    if include_holdout:
        return HOLDOUT_CASES.get(case_id)
    return None


def dataset_hash(records: Mapping[str, Any]) -> str:
    hashes = {key: value.sha256 for key, value in sorted(records.items())}
    payload = json.dumps(hashes, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def frozen_dataset_manifest() -> dict[str, Any]:
    return {
        "development_regression": {
            "case_hashes": {
                key: value.sha256 for key, value in DEVELOPMENT_REGRESSION_CASES.items()
            },
            "dataset_sha256": dataset_hash(DEVELOPMENT_REGRESSION_CASES),
        },
        "holdout": {
            "case_hashes": {key: value.sha256 for key, value in HOLDOUT_CASES.items()},
            "dataset_sha256": dataset_hash(HOLDOUT_CASES),
        },
        "error_injection": {
            "case_hashes": {
                key: value.sha256 for key, value in ERROR_INJECTION_CASES.items()
            },
            "dataset_sha256": dataset_hash(ERROR_INJECTION_CASES),
        },
    }
