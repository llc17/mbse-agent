# -*- coding: utf-8 -*-
"""
=============================================================================
schemas.py — 节点间数据契约（Pydantic 类型校验）
=============================================================================

作用：定义全流程中的"数据结构"——每个节点生产什么格式的数据，下游节点
      消费什么格式的数据。用 Pydantic 做类型校验，防止 LLM 输出畸形 JSON
      毒化流水线。

V3 新增：QualityIssue, QualityCheckResult, RepairLogEntry
V4 保持不变（兼容 V3）

5 个核心模型对应 4 个节点 + 质量检查：
  节点1 → StructuredRequirement    (结构化需求)
  节点2 → SysMLArtifact            (SysML v2 代码)
  节点3 → ModelicaArtifact         (Modelica 仿真结果)
  节点4 → SummaryArtifact          (总结报告)
  质量检查 → QualityIssue, QualityCheckResult (检查问题/结果)
  修复日志 → RepairLogEntry        (每次修复的 diff)
=============================================================================
"""

from datetime import datetime   # 用于自动生成时间戳
from typing import Optional     # 可选字段类型

from pydantic import BaseModel, Field  # Pydantic: Python 数据校验库


# ==========================================================================
# 节点 1 产出：结构化需求
# ==========================================================================
# 用户输入 "做一个 1kHz RC 滤波器" →
# LLM 将其转换为结构化的 component_type + parameters + topology + constraints

class StructuredRequirement(BaseModel):
    """
    从自然语言需求中提取的结构化信息。

    字段说明：
      - component_type: 大类，如 "RC低通滤波器"
      - parameters:     参数名→数值，如 {"R": 1000, "cutoff_freq": 1000}
      - topology:       拓扑描述，如 "串联RC"
      - constraints:    约束列表，如 ["截止频率 = 1kHz"]
      - raw_input:      用户原始输入（保留原文用于追溯）
      - missing_fields: 仍缺少的信息（interactive 模式用，用于反问）
    """
    component_type: str = Field(description="系统类型，如 RC低通滤波器")
    component_name: str = Field(default="", description="组件名，如 my_rc_filter")
    parameters: dict[str, float] = Field(default_factory=dict, description="参数名→数值")
    topology: str = Field(default="", description="拓扑描述，如 串联RC")
    constraints: list[str] = Field(default_factory=list, description="约束列表")
    raw_input: str = Field(description="用户原始输入")
    clarification_rounds: int = Field(default=0, description="精炼轮数")  # interactive 模式反问了几轮
    missing_fields: list[str] = Field(default_factory=list, description="仍缺的字段")
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())  # 自动打时间戳

    @property
    def is_complete(self) -> bool:
        """需求是否完整（没有缺失字段）。"""
        return len(self.missing_fields) == 0


# ==========================================================================
# 节点 2 产出：SysML v2 代码
# ==========================================================================
# LLM 根据 StructuredRequirement 生成一份 .sysml 文本 → 保存为文件

class SysMLArtifact(BaseModel):
    """
    生成的 SysML v2 文本代码及其元信息。

    字段说明：
      - sysml_code: SysML v2 文本代码（核心产出）
      - file_path:  保存到磁盘的路径，如 outputs/run_xxx/sysml/model.sysml
      - attempts:   生成尝试次数（含重试）
      - errors:     _syntax_check() 发现的语法问题
    """
    sysml_code: str = Field(default="", description="SysML v2 文本代码")
    file_path: str = Field(default="", description="保存路径")
    attempts: int = Field(default=1, description="生成尝试次数")
    errors: list[str] = Field(default_factory=list)


# ==========================================================================
# 节点 3 产出：Modelica 仿真结果
# ==========================================================================
# OMPython 编译 Modelica → 仿真 → 生成 CSV + PNG

class ModelicaArtifact(BaseModel):
    """
    Modelica 仿真从代码生成到结果的全链路产出。

    字段说明：
      - modelica_code: Modelica .mo 文本（LLM 生成）
      - file_path:     .mo 文件路径
      - csv_path:      仿真数据 CSV 路径（用于物理验证）
      - plot_path:     仿真曲线 PNG 路径（用于节点3 HITL 展示）
      - attempts:      总尝试次数（含编译+仿真+修复循环）
      - errors:        编译/仿真错误日志（喂给 repair LLM）
      - success:       最终是否成功（True=CSV+PNG 都存在）
    """
    modelica_code: str = Field(default="", description="Modelica .mo 文本")
    file_path: str = Field(default="", description=".mo 文件路径")
    csv_path: str = Field(default="", description="仿真 CSV 路径")
    plot_path: str = Field(default="", description="仿真 PNG 路径")
    attempts: int = Field(default=1, description="总尝试次数")
    errors: list[str] = Field(default_factory=list, description="编译/仿真错误")
    success: bool = Field(default=False, description="仿真是否成功")


# ==========================================================================
# 节点 4 产出：流程总结
# ==========================================================================
# LLM 读全流程产物 → 生成 Markdown 总结报告

class SummaryArtifact(BaseModel):
    """
    全流程完成后的总结报告及其文件引用。

    字段说明：
      - summary_text:  Markdown 格式的总结内容
      - file_path:     summary.md 路径
      - *_path:        上游各节点的产出文件路径（形成追溯链）
    """
    summary_text: str = Field(description="Markdown 总结")
    file_path: str = Field(default="", description="summary.md 路径")
    requirement_path: str = Field(default="")  # 指向 requirement.json
    sysml_path: str = Field(default="")        # 指向 model.sysml
    modelica_path: str = Field(default="")     # 指向 model.mo
    plot_path: str = Field(default="")          # 指向 simulation.png


# ==========================================================================
# V3 新增：质量检查 & 修复日志
# ==========================================================================
# 这两个类专门服务于 V3 新增的两个质量检查节点

class QualityIssue(BaseModel):
    """
    单条质量问题。交叉校验或物理验证发现一个不一致时生成一条。

    字段说明：
      - param_name: 哪个参数出问题，如 "cutoff_frequency"
      - expected:   期望值，如 "1000.0 Hz"
      - found:      实际值，如 "100.0 Hz"
      - severity:   严重程度 — "error"（打回重做）或 "warning"（记录不阻断）
      - detail:     问题的一句话描述
    """
    param_name: str = Field(default="", description="涉及的参数名")
    expected: str = Field(default="", description="期望值")
    found: str = Field(default="", description="实际值")
    severity: str = Field(default="warning", description="error | warning")
    detail: str = Field(default="", description="问题描述")


class QualityCheckResult(BaseModel):
    """
    一次质量检查的完整结果（交叉校验或物理验证）。

    字段说明：
      - check_type:        "cross_validate" 或 "physics_validate"
      - passed:            是否通过
      - issues:            发现的问题列表
      - deviation_percent: 物理量偏差百分比（仅 physics_validate 有值）
      - expected_value:    理论期望值（仅 physics_validate）
      - actual_value:      仿真实测值（仅 physics_validate）
      - details:           人类可读的检查摘要
    """
    check_type: str = Field(description="cross_validate | physics_validate")
    passed: bool = Field(default=False)
    issues: list[dict] = Field(default_factory=list)
    deviation_percent: Optional[float] = Field(default=None, description="物理偏差百分比")
    expected_value: Optional[float] = Field(default=None)
    actual_value: Optional[float] = Field(default=None)
    details: str = Field(default="")


class RepairLogEntry(BaseModel):
    """
    节点3 单次修复记录。记录"修复前有什么错误、代码是什么样、修复后变成什么样"。
    这些记录会被保存到 repair_log.json，用于事后分析 LLM 的修复能力。
    """
    attempt: int                                                # 第几次修复
    errors_before: list[str] = Field(default_factory=list)     # 修复前的错误信息
    code_before_snippet: str = Field(default="", description="修复前代码前200字符")
    code_after_snippet: str = Field(default="", description="修复后代码前200字符")
