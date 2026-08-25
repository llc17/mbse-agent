"""
=============================================================================
schemas.py — 节点间数据契约（Pydantic 类型校验层）
=============================================================================
用途:
  - 定义所有 Agent 节点的输入/输出数据结构（数据契约）
  - 每个字段都有类型声明和中文描述，IDE 能自动补全
  - 贯穿整个流水线：node1→node2→node3→node4→quality

V3 新增: QualityCheckResult（质量检查结果）+ RepairLogEntry（修复日志）
V4 继承: 未改动
=============================================================================
"""

# ---------------------------------------------------------------------------
# 第 1 层: 导入依赖
# ---------------------------------------------------------------------------
from datetime import datetime          # 自动生成时间戳
from typing import Optional             # 可选字段类型

from pydantic import BaseModel, Field   # Pydantic = Python 数据校验库


# ============================================================================
# 节点 1 产出：结构化需求
# ============================================================================
class StructuredRequirement(BaseModel):
    """
    节点1（需求分析 Agent）的输出 —— 从自然语言中提取的结构化需求。

    产生流程: 用户输入 "做一个1kHz低通滤波器"
             → 节点1 提取 → component_type="RC低通滤波器", parameters={R:1000, C:1.59e-7}

    该对象全流水线可见（node2 用它生成 SysML，node3 用它生成 Modelica）
    """
    # ── 核心字段 ──
    component_type: str = Field(
        description="系统类型，如 RC低通滤波器"
        # 例: "RC低通滤波器", "RLC谐振电路", "双房间热传导"
    )
    component_name: str = Field(
        default="",
        description="组件名，如 my_rc_filter"
        # 用于 Modelica 的 model 名称，LLM 自动生成
    )
    parameters: dict[str, float] = Field(
        default_factory=dict,
        description="参数名→数值"
        # 例: {"R": 1000.0, "C": 1.59e-07, "f_c": 1000.0}
    )
    topology: str = Field(
        default="",
        description="拓扑描述，如 串联RC"
        # 告诉 node2 组件怎么连接
    )
    constraints: list[str] = Field(
        default_factory=list,
        description="约束列表"
        # 例: ["f_c = 1/(2πRC)", "截止频率=1000Hz"]
    )

    # ── 元信息 ──
    raw_input: str = Field(
        description="用户原始输入"
        # 保留原始文本，用于日志和调试
    )
    clarification_rounds: int = Field(
        default=0,
        description="精炼轮数"
        # interactive 模式下，每轮反问用户就是 +1；experiment 模式下始终为 0
    )
    missing_fields: list[str] = Field(
        default_factory=list,
        description="仍缺的字段"
        # 自查完整性发现缺失时填写，为空 = 完整
    )
    created_at: str = Field(
        default_factory=lambda: datetime.now().isoformat()
        # 自动生成 ISO 8601 时间戳，如 "2026-07-29T12:00:00"
    )

    @property
    def is_complete(self) -> bool:
        """需求是否完整（无缺失字段）"""
        return len(self.missing_fields) == 0


# ============================================================================
# 节点 2 产出：SysML v2 代码
# ============================================================================
class SysMLArtifact(BaseModel):
    """
    节点2（SysML Agent）的输出 —— LLM 生成的 SysML v2 文本代码。

    sysmlpy 标准解析器做语法检查，正则做快速检查。
    """
    sysml_code: str = Field(
        default="",
        description="SysML v2 文本代码"
        # 完整的 .sysml 文件内容，可被 Eclipse SysML v2 插件渲染
    )
    file_path: str = Field(
        default="",
        description="保存路径"
        # 例: "outputs/run_xxx/sysml/model.sysml"
    )
    attempts: int = Field(
        default=1,
        description="生成尝试次数"
        # V6: 三阶段审查循环每轮 +1
    )
    errors: list[str] = Field(
        default_factory=list
        # 语法检查错误列表，[] = 无错误
    )


# ============================================================================
# 节点 3 产出：Modelica 仿真结果
# ============================================================================
class ModelicaArtifact(BaseModel):
    """
    节点3（Modelica Agent）的输出 —— LLM 生成的 Modelica 代码 + 仿真结果。

    V6 新增: _template 字段记录使用的模板名。
    """
    modelica_code: str = Field(
        default="",
        description="Modelica .mo 文本"
        # 完整的 .mo 文件内容，包含 model ... end model; 结构
    )
    file_path: str = Field(
        default="",
        description=".mo 文件路径"
    )
    csv_path: str = Field(
        default="",
        description="仿真 CSV 路径"
        # MAT→CSV 转换后的仿真数据文件
    )
    plot_path: str = Field(
        default="",
        description="仿真 PNG 路径"
        # matplotlib 绘制的仿真曲线图
    )
    attempts: int = Field(
        default=1,
        description="总尝试次数"
        # 编译+仿真的累计尝试次数
    )
    errors: list[str] = Field(
        default_factory=list,
        description="编译/仿真错误"
        # 每次失败追加一条 "[编译错误 #N] ..." 或 "[仿真错误 #N] ..."
    )
    success: bool = Field(
        default=False,
        description="仿真是否成功"
        # True = 仿真完成，CSV + PNG 已生成
    )


# ============================================================================
# 节点 4 产出：流程总结
# ============================================================================
class SummaryArtifact(BaseModel):
    """
    节点4（总结 Agent）的输出 —— 全流程的 Markdown 总结报告。
    """
    summary_text: str = Field(
        description="Markdown 总结"
        # 完整报告内容，500-800 字
    )
    file_path: str = Field(
        default="",
        description="summary.md 路径"
    )
    requirement_path: str = Field(default="")
    sysml_path: str = Field(default="")
    modelica_path: str = Field(default="")
    plot_path: str = Field(default="")
    # 以上四个字段指向各节点的产物路径，方便报告的"相关文件"章节


# ============================================================================
# V3 新增：质量检查 & 修复日志
# ============================================================================

class QualityIssue(BaseModel):
    """
    单条质量检查问题。

    来源: Q_cross_validate 或 Q_physics_validate 的审查结果。
    """
    param_name: str = Field(
        default="",
        description="涉及的参数名"
        # 例: "R", "cutoff_frequency"
    )
    expected: str = Field(
        default="",
        description="期望值"
        # 例: "1000.0 Hz"
    )
    found: str = Field(
        default="",
        description="实际值"
        # 例: "950.0 Hz" —— 与期望值不一致时触发打回
    )
    severity: str = Field(
        default="warning",
        description="error | warning"
        # error = 必须修正，warning = 记录但不阻断
    )
    detail: str = Field(
        default="",
        description="问题描述"
    )


class QualityCheckResult(BaseModel):
    """
    一次质量检查的完整结果。

    V6 新增: validate_type 字段（rc_cutoff / thermal_steady / rlc_resonant / opamp_gain）
    """
    check_type: str = Field(
        description="cross_validate | physics_validate | sysml_modelica"
        # 三种检查类型: 需求↔SysML交叉校验 / 物理量验证 / SysML↔Modelica跨步检查
    )
    passed: bool = Field(default=False)
    issues: list[dict] = Field(default_factory=list)
    deviation_percent: Optional[float] = Field(
        default=None,
        description="物理偏差百分比"
        # 例: 5.2 表示偏差 5.2%，超过 tolerance 则判定为不通过
    )
    expected_value: Optional[float] = Field(default=None)
    actual_value: Optional[float] = Field(default=None)
    details: str = Field(default="")


class RepairLogEntry(BaseModel):
    """
    node3 单次修复记录。

    V6 新增: root_cause 字段（LLM 根因分析结果）。
    """
    attempt: int                                   # 第几次修复
    errors_before: list[str] = Field(default_factory=list)   # 修复前的错误
    code_before_snippet: str = Field(
        default="",
        description="修复前代码前200字符"
    )
    code_after_snippet: str = Field(
        default="",
        description="修复后代码前200字符"
    )
