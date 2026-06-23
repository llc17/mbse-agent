"""
节点间数据契约 — Pydantic 类型校验。

每个节点产出一个 Schema 实例，下游节点只认这个接口。
字段缺失或类型不对 → Pydantic 直接抛 ValidationError，不用人肉 debug。
"""

from datetime import datetime
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


# ============================================================
# 节点 1 产出：结构化需求
# ============================================================
class StructuredRequirement(BaseModel):
    """用户自然语言精炼后的结构化需求，节点 2/3 的唯一输入源。"""

    component_type: str = Field(
        description="系统类型，例如 RC低通滤波器 / RLC带通滤波器 / 单房间热传导"
    )
    component_name: str = Field(
        default="",
        description="用户或系统自动命名的组件名，如 my_rc_filter",
    )
    parameters: dict[str, float] = Field(
        default_factory=dict,
        description="参数名→数值，例如 {'R': 1000, 'C': 1e-6, 'cutoff_freq': 159}",
    )
    topology: str = Field(
        default="",
        description="拓扑描述，例如 串联RC / 并联RLC",
    )
    constraints: list[str] = Field(
        default_factory=list,
        description="约束条件列表，例如 ['截止频率约1kHz', '电阻取标准值']",
    )
    raw_input: str = Field(
        description="用户原始输入文本，用于追溯",
    )
    clarification_rounds: int = Field(
        default=0,
        description="需求精炼对话轮数",
    )
    missing_fields: list[str] = Field(
        default_factory=list,
        description="检查后仍缺失的字段名（为空=完整）",
    )
    created_at: str = Field(
        default_factory=lambda: datetime.now().isoformat(),
        description="创建时间 ISO 字符串",
    )

    @property
    def is_complete(self) -> bool:
        return len(self.missing_fields) == 0


# ============================================================
# 节点 2 产出：SysML v2 代码
# ============================================================
class SysMLArtifact(BaseModel):
    """LLM 生成的 SysML v2 文本代码。用户手动 Eclipse 看图。"""

    sysml_code: str = Field(default="", description="SysML v2 文本代码内容")
    file_path: str = Field(default="", description="保存到磁盘的路径")
    attempts: int = Field(default=1, description="生成尝试次数（含重试）")
    errors: list[str] = Field(default_factory=list, description="各次尝试的语法/逻辑错误")
    source_requirement: Optional[StructuredRequirement] = Field(
        default=None, description="回溯：来自哪个需求"
    )


# ============================================================
# 节点 3 产出：Modelica 仿真结果
# ============================================================
class ModelicaArtifact(BaseModel):
    """LLM 生成 Modelica 代码 + OMC 编译仿真结果。"""

    modelica_code: str = Field(default="", description="Modelica .mo 文本代码")
    file_path: str = Field(default="", description=".mo 文件保存路径")
    csv_path: str = Field(default="", description="仿真结果 CSV 路径")
    plot_path: str = Field(default="", description="仿真曲线 PNG 路径")
    attempts: int = Field(default=1, description="生成+编译尝试次数")
    errors: list[str] = Field(default_factory=list, description="各次编译/仿真错误信息")
    success: bool = Field(default=False, description="最终是否通过编译并跑出仿真")


# ============================================================
# 节点 4 产出：流程总结
# ============================================================
class SummaryArtifact(BaseModel):
    """汇总前面 3 个节点的产出，生成人类可读的总结。"""

    summary_text: str = Field(description="Markdown 格式的总结全文")
    file_path: str = Field(default="", description="summary.md 路径")
    requirement_path: str = Field(default="", description="requirement.json 路径引用")
    sysml_path: str = Field(default="", description="model.sysml 路径引用")
    modelica_path: str = Field(default="", description="model.mo 路径引用")
    plot_path: str = Field(default="", description="仿真曲线 PNG 路径引用")
