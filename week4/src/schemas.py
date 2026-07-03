"""
节点间数据契约 — Pydantic 类型校验。V3 新增 QualityCheckResult + RepairLogEntry。
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ============================================================
# 节点 1 产出：结构化需求
# ============================================================
class StructuredRequirement(BaseModel):
    component_type: str = Field(description="系统类型，如 RC低通滤波器")
    component_name: str = Field(default="", description="组件名，如 my_rc_filter")
    parameters: dict[str, float] = Field(default_factory=dict, description="参数名→数值")
    topology: str = Field(default="", description="拓扑描述，如 串联RC")
    constraints: list[str] = Field(default_factory=list, description="约束列表")
    raw_input: str = Field(description="用户原始输入")
    clarification_rounds: int = Field(default=0, description="精炼轮数")
    missing_fields: list[str] = Field(default_factory=list, description="仍缺的字段")
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())

    @property
    def is_complete(self) -> bool:
        return len(self.missing_fields) == 0


# ============================================================
# 节点 2 产出：SysML v2 代码
# ============================================================
class SysMLArtifact(BaseModel):
    sysml_code: str = Field(default="", description="SysML v2 文本代码")
    file_path: str = Field(default="", description="保存路径")
    attempts: int = Field(default=1, description="生成尝试次数")
    errors: list[str] = Field(default_factory=list)


# ============================================================
# 节点 3 产出：Modelica 仿真结果
# ============================================================
class ModelicaArtifact(BaseModel):
    modelica_code: str = Field(default="", description="Modelica .mo 文本")
    file_path: str = Field(default="", description=".mo 文件路径")
    csv_path: str = Field(default="", description="仿真 CSV 路径")
    plot_path: str = Field(default="", description="仿真 PNG 路径")
    attempts: int = Field(default=1, description="总尝试次数")
    errors: list[str] = Field(default_factory=list, description="编译/仿真错误")
    success: bool = Field(default=False, description="仿真是否成功")


# ============================================================
# 节点 4 产出：流程总结
# ============================================================
class SummaryArtifact(BaseModel):
    summary_text: str = Field(description="Markdown 总结")
    file_path: str = Field(default="", description="summary.md 路径")
    requirement_path: str = Field(default="")
    sysml_path: str = Field(default="")
    modelica_path: str = Field(default="")
    plot_path: str = Field(default="")


# ============================================================
# V3 新增：质量检查 & 修复日志
# ============================================================

class QualityIssue(BaseModel):
    """单条质量检查问题。"""
    param_name: str = Field(default="", description="涉及的参数名")
    expected: str = Field(default="", description="期望值")
    found: str = Field(default="", description="实际值")
    severity: str = Field(default="warning", description="error | warning")
    detail: str = Field(default="", description="问题描述")


class QualityCheckResult(BaseModel):
    """一次质量检查的完整结果。"""
    check_type: str = Field(description="cross_validate | physics_validate")
    passed: bool = Field(default=False)
    issues: list[dict] = Field(default_factory=list)
    deviation_percent: Optional[float] = Field(default=None, description="物理偏差百分比")
    expected_value: Optional[float] = Field(default=None)
    actual_value: Optional[float] = Field(default=None)
    details: str = Field(default="")


class RepairLogEntry(BaseModel):
    """node3 单次修复记录。"""
    attempt: int
    errors_before: list[str] = Field(default_factory=list)
    code_before_snippet: str = Field(default="", description="修复前代码前200字符")
    code_after_snippet: str = Field(default="", description="修复后代码前200字符")
