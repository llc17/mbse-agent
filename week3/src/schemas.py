"""
节点间数据契约 — Pydantic 类型校验。V2 沿用 V1 的 Schema，增加序列化辅助。
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
