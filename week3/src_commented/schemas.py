# -*- coding: utf-8 -*-
"""
=============================================================================
schemas.py — 数据契约（Data Contract）
=============================================================================

这个文件定义了流水线中 4 个节点之间的"数据接口"。
可以理解为：4 个车间之间的"交接单"，格式不对就不让过。

核心概念：Pydantic BaseModel
  - 一个继承 BaseModel 的类 = 一个数据模板
  - 定义了字段名、类型、默认值
  - 如果传入的数据类型不对，Pydantic 直接报错，不用人肉 debug

本项目的 4 个节点各产出 1 个 Schema：
  节点1: StructuredRequirement  → 用户需求的结构化表达
  节点2: SysMLArtifact          → LLM 生成的 SysML v2 代码
  节点3: ModelicaArtifact       → Modelica 仿真代码 + 结果
  节点4: SummaryArtifact        → 人类可读的总结报告
"""

# ====================================================================
# 导入
# ====================================================================

from datetime import datetime                          # 用来生成时间戳字符串
from typing import Optional                            # Optional[X] 表示 "X 或 None"

from pydantic import BaseModel, Field                  # BaseModel: 数据模板基类
                                                       # Field: 给字段加说明/默认值


# ====================================================================
# 节点 1 产出：StructuredRequirement（结构化需求）
# ====================================================================
# 这个类描述了"用户到底想要一个什么系统"。
# 它是节点 2 和节点 3 的唯一输入源——后续所有代码都从这个对象生成。
# ====================================================================

class StructuredRequirement(BaseModel):
    """
    用户自然语言精炼后的结构化需求。

    用法：
        req = StructuredRequirement(
            component_type="RC低通滤波器",
            parameters={"R": 1000, "C": 1e-6},
            ...
        )
    """

    # ---- 系统基本描述 ----

    component_type: str = Field(                        # component_type: 组件类型
        description="系统类型，例如 RC低通滤波器 / RLC带通滤波器 / 单房间热传导"
    )                                                   # Field(description=...) 是给字段加注释，Pydantic 会用于生成 JSON Schema

    component_name: str = Field(                        # component_name: 组件名
        default="",                                      # default="" 表示可以不传，默认为空字符串
        description="用户或系统自动命名的组件名，如 my_rc_filter",
    )

    # ---- 系统参数 ----

    parameters: dict[str, float] = Field(               # dict[str, float] 表示 "键是字符串，值是浮点数" 的字典
        default_factory=dict,                            # default_factory=dict 表示如果不传，默认给一个空字典 {}
        description="参数名→数值，例如 {'R': 1000, 'C': 1e-6, 'cutoff_freq': 159}",
    )

    topology: str = Field(                               # topology: 拓扑（电路结构）
        default="",                                      # 例如 "串联RC" / "并联RLC"
        description="拓扑描述，例如 串联RC / 并联RLC",
    )

    # ---- 约束与元数据 ----

    constraints: list[str] = Field(                      # list[str] 表示 "字符串列表"
        default_factory=list,                            # 例如 ["截止频率约1kHz", "电阻取标准值"]
        description="约束条件列表",
    )

    raw_input: str = Field(                              # raw_input: 用户原始输入
        description="用户原始输入文本，用于追溯",            # 注意这个字段没有 default，所以创建时必须传
    )                                                    # Pydantic 会在缺少时抛出 ValidationError

    clarification_rounds: int = Field(                   # clarification_rounds: 对话轮数
        default=0,                                       # 记录节点1 跟用户反问了多少轮
        description="需求精炼对话轮数",
    )

    missing_fields: list[str] = Field(                   # missing_fields: 仍然缺失的字段
        default_factory=list,                            # 如果为空列表 → 信息完整
        description="检查后仍缺失的字段名（为空=完整）",
    )

    created_at: str = Field(                             # created_at: 创建时间
        default_factory=lambda: datetime.now().isoformat(),
        # 上面的 lambda 是一个"工厂函数"：每次创建实例时自动调用
        # datetime.now().isoformat() 返回类似 "2026-06-09T14:30:00" 的字符串
        description="创建时间 ISO 字符串",
    )

    # ---- 计算属性 ----
    @property                                            # @property 装饰器：让它像字段一样调用，但值是算出来的
    def is_complete(self) -> bool:
        """缺失字段列表为空 = 需求信息完整"""
        return len(self.missing_fields) == 0             # 判断 missing_fields 列表长度是否为 0


# ====================================================================
# 节点 2 产出：SysMLArtifact（SysML v2 代码产物）
# ====================================================================
# SysML v2 是一种文本格式的系统建模语言（由 OMG 标准化）。
# LLM 生成 .sysml 文本文件，用户用 Eclipse 打开看图。
# 本类记录了生成的代码内容和重试过程。
# ====================================================================

class SysMLArtifact(BaseModel):
    """LLM 生成的 SysML v2 文本代码。用户手动 Eclipse 看图。"""

    sysml_code: str = Field(                             # sysml_code: SysML v2 文本代码
        default="",                                      # 这就是 LLM 产出的核心内容
        description="SysML v2 文本代码内容",
    )

    file_path: str = Field(                              # file_path: 文件保存路径
        default="",                                      # 例如 "outputs/run_xxx/sysml/model.sysml"
        description="保存到磁盘的路径",
    )

    attempts: int = Field(                               # attempts: 尝试次数
        default=1,                                       # 记录了 LLM 重试了几次才通过语法检查
        description="生成尝试次数（含重试）",
    )

    errors: list[str] = Field(                           # errors: 错误记录
        default_factory=list,                            # 每次尝试如果有语法错误就追加一条
        description="各次尝试的语法/逻辑错误",
    )


# ====================================================================
# 节点 3 产出：ModelicaArtifact（Modelica 仿真产物）
# ====================================================================
# Modelica 是一种多域物理建模语言。
# 本类记录了 LLM 生成的 .mo 代码 + 编译结果 + 仿真数据。
# ====================================================================

class ModelicaArtifact(BaseModel):
    """LLM 生成 Modelica 代码 + OMC 编译仿真结果。"""

    modelica_code: str = Field(                          # modelica_code: Modelica 仿真代码
        default="",                                      # LLM 生成的 .mo 文件内容
        description="Modelica .mo 文本代码",
    )

    file_path: str = Field(                              # file_path: .mo 文件路径
        default="",
        description=".mo 文件保存路径",
    )

    csv_path: str = Field(                               # csv_path: 仿真数据路径
        default="",                                      # OpenModelica 仿真结果存储为 CSV
        description="仿真结果 CSV 路径",
    )

    plot_path: str = Field(                              # plot_path: 仿真曲线图路径
        default="",                                      # matplotlib 生成的 PNG 图片
        description="仿真曲线 PNG 路径",
    )

    attempts: int = Field(                               # attempts: 总尝试次数
        default=1,                                       # 编译 + 仿真失败后自修复的次数
        description="生成+编译尝试次数",
    )

    errors: list[str] = Field(                           # errors: 错误日志
        default_factory=list,                            # 记录每次编译/仿真失败的错误信息
        description="各次编译/仿真错误信息",
    )

    success: bool = Field(                               # success: 最终是否成功
        default=False,                                   # True = 编译通过且仿真跑出了 CSV
        description="最终是否通过编译并跑出仿真",
    )


# ====================================================================
# 节点 4 产出：SummaryArtifact（流程总结）
# ====================================================================
# 这个类汇总前面 3 个节点的所有产出物。
# LLM 会基于这些信息生成人类可读的 Markdown 总结。
# ====================================================================

class SummaryArtifact(BaseModel):
    """汇总前面 3 个节点的产出，生成人类可读的总结。"""

    summary_text: str = Field(                           # summary_text: 总结内容
        description="Markdown 格式的总结全文",
    )

    file_path: str = Field(                              # file_path: summary.md 路径
        default="",
        description="summary.md 路径",
    )

    # 以下 4 个字段记录了其他产物的路径，方便后续查看
    requirement_path: str = Field(                       # 指向 requirement.json
        default="",
    )

    sysml_path: str = Field(                             # 指向 model.sysml
        default="",
    )

    modelica_path: str = Field(                          # 指向 model.mo
        default="",
    )

    plot_path: str = Field(                              # 指向 simulation.png
        default="",
    )
