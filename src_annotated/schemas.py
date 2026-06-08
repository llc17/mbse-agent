"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  数据契约 — 4 个节点之间的"交货单"                                          ║
║                                                                            ║
║  数据流向（整条流水线）：                                                    ║
║    用户输入                                                                  ║
║      │                                                                      ║
║      ▼                                                                      ║
║  节点1: refine_requirement()  ──产出──►  StructuredRequirement              ║
║      │                                                                      ║
║      ├───────────────────────────────►  传给节点2                            ║
║      │                                                                      ║
║      ▼                                                                      ║
║  节点2: generate_sysml(req)     ──产出──►  SysMLArtifact                    ║
║      │                                                                      ║
║      ├───────────────────────────────►  传给节点3（同时传 StructuredReq）     ║
║      │                                                                      ║
║      ▼                                                                      ║
║  节点3: generate_and_simulate(req, sysml) ──►  ModelicaArtifact             ║
║      │                                                                      ║
║      ├───────────────────────────────►  传给节点4（同时传前两个）             ║
║      │                                                                      ║
║      ▼                                                                      ║
║  节点4: generate_summary(req, sysml, mo) ──►  SummaryArtifact               ║
║                                                                            ║
║  每个 Schema 的作用：                                                        ║
║    - 定义字段名和类型（保证节点间接头一致）                                    ║
║    - 自动校验（Pydantic 在 runtime 检查类型）                                 ║
║    - 序列化（.model_dump_json() 一键导出 JSON）                               ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from datetime import datetime  # 标准库：获取当前时间
from pathlib import Path       # 标准库：文件路径处理
from typing import Optional    # 类型注解：Optional[X] 表示 X 或 None

from pydantic import BaseModel, Field  # 第三方库：BaseModel=父类, Field=字段配置


# ============================================================
#  节点 1 → 节点 2,3 的数据
#  这是整条流水线的起点
# ============================================================
class StructuredRequirement(BaseModel):             # 继承 BaseModel → 自动获得校验能力
    """节点1的产出。自然语言 → 结构化数据。"""

    # ── 以下两个字段没有默认值 → 创建对象时必须传 ──
    component_type: str = Field(                    # str = 必须是字符串
        description="系统类型，如 RC低通滤波器"      # description 只是注释，不影响逻辑
    )                                               # → 节点2/3 根据这个决定生成什么模型
    raw_input: str = Field(                         # 用户原始输入，不做任何处理
        description="用户原始输入文本，用于追溯"      # → 论文对比"精炼前 vs 精炼后"
    )

    # ── 以下字段有默认值 → 创建对象时可以不传 ──
    component_name: str = Field(
        default="",                                 # 默认空字符串
        description="组件名，如 my_rc_filter",
    )
    parameters: dict[str, float] = Field(           # 键是str，值是float 的字典
        default_factory=dict,                       # 不用 {} 避免 Python 可变默认参数陷阱
        description="参数，如 {'R': 1000, 'C': 1e-6}",
    )
    topology: str = Field(
        default="",                                 # 节点1精炼后填入，如"串联RC"
        description="拓扑描述",
    )
    constraints: list[str] = Field(                 # 字符串列表
        default_factory=list,                       # 默认空列表
        description="约束条件，如 ['截止频率约1kHz']",
    )
    clarification_rounds: int = Field(
        default=0,                                  # 初始0轮
        description="需求精炼对话轮数",              # → 论文数据
    )
    missing_fields: list[str] = Field(
        default_factory=list,
        description="缺失字段名（为空=完整）",        # → 节点1 用此判断是否继续反问
    )
    created_at: str = Field(
        default_factory=lambda: datetime.now().isoformat(),  # lambda=匿名函数，每次创建时执行
        description="创建时间 ISO 字符串",
    )

    @property                                       # 把方法变成属性，调用不加括号
    def is_complete(self) -> bool:                  # → req.is_complete 不是 req.is_complete()
        """missing_fields 为空 = 需求完整。"""
        return len(self.missing_fields) == 0


# ============================================================
#  节点 2 → 节点 3 的数据
# ============================================================
class SysMLArtifact(BaseModel):
    """节点2的产出。LLM 生成的 .sysml 文本。"""

    sysml_code: str = Field(                        # 默认空字符串
        default="",                                 # 节点2 的 while 循环里逐步填充
        description="SysML v2 文本代码"
    )
    file_path: str = Field(
        default="",                                 # 保存后才填入，如 "outputs/.../sysml/model.sysml"
        description="保存到磁盘的路径",
    )
    attempts: int = Field(
        default=1,                                  # 最少1次
        description="生成尝试次数（含重试）",         # → 论文数据
    )
    errors: list[str] = Field(
        default_factory=list,
        description="语法/逻辑错误记录",              # → 自修复回喂 LLM
    )
    source_requirement: Optional[StructuredRequirement] = Field(  # Optional = 可以是 None
        default=None,
        description="回溯：来自哪个需求",
    )


# ============================================================
#  节点 3 → 节点 4 的数据
# ============================================================
class ModelicaArtifact(BaseModel):
    """节点3的产出。Modelica 代码 + 编译仿真结果。"""

    modelica_code: str = Field(default="", description="Modelica .mo 文本代码")
    file_path: str = Field(default="", description=".mo 文件保存路径")
    csv_path: str = Field(default="", description="仿真结果 CSV 路径")
    plot_path: str = Field(default="", description="仿真曲线 PNG 路径")
    attempts: int = Field(default=1, description="生成+编译尝试次数")
    errors: list[str] = Field(                      # 自修复核心
        default_factory=list,
        description="编译/仿真错误信息"               # → 失败时回喂 LLM，论文里统计错误类型
    )
    success: bool = Field(                          # 最关键字段
        default=False,                              # 默认失败
        description="是否通过编译并跑出仿真"          # → 论文："一次通过率 = success=True / 总次数"
    )


# ============================================================
#  节点 4 → 用户 的数据
# ============================================================
class SummaryArtifact(BaseModel):
    """节点4的产出。人类可读的全流程总结。"""

    summary_text: str = Field(description="Markdown 格式的总结全文")
    file_path: str = Field(default="", description="summary.md 路径")
    requirement_path: str = Field(default="", description="requirement.json 路径引用")
    sysml_path: str = Field(default="", description="model.sysml 路径引用")
    modelica_path: str = Field(default="", description="model.mo 路径引用")
    plot_path: str = Field(default="", description="仿真曲线 PNG 路径引用")


# ╔══════════════════════════════════════════════════════════════╗
# ║  main.py 中的调用顺序 = 整条数据流：                          ║
# ║                                                              ║
# ║  req     = refine_requirement(raw_input)    → StructuredReq  ║
# ║  sysml   = generate_sysml(req, dir)         → SysMLArtifact  ║
# ║  mo      = generate_and_simulate(req, sysml, dirs)            ║
# ║                                            → ModelicaArtifact║
# ║  summary = generate_summary(req, sysml, mo, dir)              ║
# ║                                            → SummaryArtifact ║
# ╚══════════════════════════════════════════════════════════════╝
