# -*- coding: utf-8 -*-
"""
=============================================================================
schemas.py — V3 数据契约（Data Contract）
=============================================================================

V3 在 V2 的 4 个节点 Schema 基础上新增 3 个质量相关模型：
  - QualityIssue：单条质量问题的结构化描述
  - QualityCheckResult：一次质量检查（交叉校验/物理验证）的完整结果
  - RepairLogEntry：node3 单次修复的记录

设计原则：
  1. 所有 Schema 都用 Pydantic BaseModel，类型不对直接报错
  2. 质量检查结果不是"通过/失败"二元——还带偏差百分比、期望值vs实际值
  3. 修复日志记录了修复前后的代码片段，可用于论文中分析"LLM修了什么"
"""

# ====================================================================
# 导入
# ====================================================================
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ====================================================================
# 节点 1 产出：StructuredRequirement（结构化需求）
# ====================================================================
# 用户输入自然语言 → LLM 精炼 → 这个对象。
# 它是后续所有节点（SysML 生成、Modelica 生成）的唯一需求来源。
# V3 改动：prompt 中加入了矛盾自检提示。
# ====================================================================

class StructuredRequirement(BaseModel):
    """用户需求结构化表达——节点1的产出"""

    component_type: str = Field(
        description="系统类型，如 RC低通滤波器 / 单房间热传导"
    )
    component_name: str = Field(
        default="",
        description="组件名，如 my_rc_filter。可以为空，后续自动生成"
    )
    parameters: dict[str, float] = Field(
        default_factory=dict,
        description="参数名→数值映射。例: {'R': 1000, 'cutoff_freq': 1000}"
    )
    topology: str = Field(
        default="",
        description="拓扑描述文本。例: '串联RC，电容接地，输出电压取自电容两端'"
    )
    constraints: list[str] = Field(
        default_factory=list,
        description="约束条件列表。例: ['截止频率=1kHz', '输入电压=5V阶跃']"
    )
    raw_input: str = Field(
        description="用户原始自然语言输入，用于追溯"
    )
    clarification_rounds: int = Field(
        default=0,
        description="交互模式下精炼反问的轮数。0=实验模式单次生成"
    )
    missing_fields: list[str] = Field(
        default_factory=list,
        description="信息不完整时记录缺失字段，为空=完整"
    )
    created_at: str = Field(
        default_factory=lambda: datetime.now().isoformat(),
        description="ISO 格式创建时间戳"
    )

    @property
    def is_complete(self) -> bool:
        """需求是否完整（没有缺失字段）"""
        return len(self.missing_fields) == 0


# ====================================================================
# 节点 2 产出：SysMLArtifact（SysML v2 代码）
# ====================================================================
# LLM 根据 StructuredRequirement 生成的 SysML v2 文本代码。
# V3 改动：prompt 中要求 doc 注释标注参数来源（追溯链）。
# ====================================================================

class SysMLArtifact(BaseModel):
    """SysML v2 模型产物——节点2的产出"""

    sysml_code: str = Field(
        default="",
        description="SysML v2 文本代码（package / part def / port / connect 等）"
    )
    file_path: str = Field(
        default="",
        description=".sysml 文件保存的绝对路径"
    )
    attempts: int = Field(
        default=1,
        description="生成尝试次数（含语法检查重试）"
    )
    errors: list[str] = Field(
        default_factory=list,
        description="语法检查发现的错误/警告列表"
    )


# ====================================================================
# 节点 3 产出：ModelicaArtifact（仿真代码 + 结果）
# ====================================================================
# V3 改动：
#   - stopTime 不再硬编码 0.01，根据 component_type 自动选
#   - CSV 路径现在指向 MAT→CSV 转换后的 simulation.csv
#   - errors 列表包含每次编译/仿真失败的完整 OMC 错误日志
# ====================================================================

class ModelicaArtifact(BaseModel):
    """Modelica 仿真产物——节点3的产出"""

    modelica_code: str = Field(
        default="",
        description="Modelica .mo 源代码"
    )
    file_path: str = Field(
        default="",
        description=".mo 文件的绝对路径"
    )
    csv_path: str = Field(
        default="",
        description="仿真结果 CSV 的绝对路径（V3: MAT→CSV 转换）"
    )
    plot_path: str = Field(
        default="",
        description="仿真曲线 PNG 的绝对路径"
    )
    attempts: int = Field(
        default=1,
        description="总尝试次数（含编译+仿真）"
    )
    errors: list[str] = Field(
        default_factory=list,
        description="所有编译/仿真错误的日志"
    )
    success: bool = Field(
        default=False,
        description="编译+仿真是否均成功"
    )


# ====================================================================
# 节点 4 产出：SummaryArtifact（总结报告）
# ====================================================================
# V3 改动：summary prompt 中包含质量检查结果 + 修复日志摘要。
# ====================================================================

class SummaryArtifact(BaseModel):
    """流程总结报告——节点4的产出"""

    summary_text: str = Field(
        description="Markdown 格式的总结全文"
    )
    file_path: str = Field(
        default="",
        description="summary.md 保存路径"
    )
    requirement_path: str = Field(default="")
    sysml_path: str = Field(default="")
    modelica_path: str = Field(default="")
    plot_path: str = Field(default="")


# ====================================================================
# ── V3 新增 ──
# 质量检查 & 修复日志的数据模型
# ====================================================================
# 这三个模型是 V3 相对于 V2 最核心的数据结构变化。
# 它们让"系统自检"的结果有了标准化的存储和传递方式。
# ====================================================================


class QualityIssue(BaseModel):
    """
    单条质量问题的结构化描述。
    用于交叉校验时记录"参数 X 在需求里是 A，在 SysML 里是 B"。
    """

    param_name: str = Field(
        default="",
        description="出问题的参数名。例: 'resistance'"
    )
    expected: str = Field(
        default="",
        description="期望值（来自需求 JSON）。例: '1000 Ω'"
    )
    found: str = Field(
        default="",
        description="实际值（在 SysML/Modelica 代码中发现的）。例: '10 Ω'"
    )
    severity: str = Field(
        default="warning",
        description="严重程度: error=必须修复, warning=可继续但需注意"
    )
    detail: str = Field(
        default="",
        description="人类可读的问题描述"
    )


class QualityCheckResult(BaseModel):
    """
    一次质量检查的完整结果。

    这个对象被存入 PipelineState.quality_checks 字典，
    实验框架的 results.json 从中提取 cross_validate_passed、
    physics_deviation_pct 等质量指标。
    """

    check_type: str = Field(
        description="检查类型: 'cross_validate' | 'physics_validate'"
    )
    passed: bool = Field(
        default=False,
        description="是否通过检查"
    )
    issues: list[dict] = Field(
        default_factory=list,
        description="发现的问题列表（QualityIssue 的 dict 形式）"
    )
    deviation_percent: Optional[float] = Field(
        default=None,
        description="物理偏差百分比。仅 physics_validate 有值"
    )
    expected_value: Optional[float] = Field(
        default=None,
        description="理论期望值。例: 1000 (Hz)"
    )
    actual_value: Optional[float] = Field(
        default=None,
        description="仿真实测值。例: 998 (Hz)"
    )
    details: str = Field(
        default="",
        description="文本描述，如 'f_c 实测=1000 Hz, 期望=1000 Hz, 偏差=0.0%'"
    )


class RepairLogEntry(BaseModel):
    """
    node3 子图中单次修复的记录。

    用途：
      1. 论文材料——分析"LLM 在修复时改了什么"
      2. 面试陈述——"通过修复日志发现，LLM 倾向改参数而非改结构"
      3. 调试——当仿真反复失败时，回溯每次 repair 的变化
    """

    attempt: int
    errors_before: list[str] = Field(
        default_factory=list,
        description="修复前的错误信息（最近 3 条）"
    )
    code_before_snippet: str = Field(
        default="",
        description="修复前代码的前 200 字符"
    )
    code_after_snippet: str = Field(
        default="",
        description="修复后代码的前 200 字符"
    )
