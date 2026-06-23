# -*- coding: utf-8 -*-
"""
=============================================================================
schemas.py — 数据契约（Data Contract）
=============================================================================

这个文件定义了 4 个节点之间的"数据接口"。
可以理解为：4 个车间之间的"交接单"，格式不对就不让过。

核心概念 — Pydantic BaseModel:
  - 一个继承 BaseModel 的类 = 一个数据模板
  - 定义了字段名、字段类型、默认值
  - 如果传入的数据类型不对，Pydantic 直接报错，不用人肉 debug

Week 2 只有这 4 个 Schema，没有独立的 utils.py 和 pipeline.py。
"""

# ====================================================================
# 导入
# ====================================================================

from datetime import datetime                          # 生成时间戳字符串（如 "2026-05-26T16:30:00"）
from pathlib import Path                                # 跨平台路径操作
from typing import Optional                             # Optional[X] = X | None（可以为空）

from pydantic import BaseModel, Field                  # BaseModel: 数据模板基类
                                                       # Field: 给字段加说明/默认值


# ====================================================================
# 节点 1 产出：StructuredRequirement（结构化需求）
# ====================================================================
# 把用户的自然语言（"做个1kHz低通滤波器"）转成机器可读的结构化数据。
# 节点 2 和节点 3 从这个对象获取所有信息来生成代码。
# ====================================================================

class StructuredRequirement(BaseModel):
    """用户自然语言精炼后的结构化需求。节点 2/3 的唯一输入源。"""

    component_type: str = Field(                        # component_type: 系统类型
        description="系统类型，例如 RC低通滤波器 / RLC带通滤波器 / 单房间热传导"
    )                                                   # Field(description=...) 是给字段加注释，用于生成 JSON Schema

    component_name: str = Field(                        # component_name: 组件名
        default="",                                      # default="" 表示不传时默认为空字符串
        description="用户或系统自动命名的组件名，如 my_rc_filter",
    )

    parameters: dict[str, float] = Field(               # parameters: 参数字典
        default_factory=dict,                            # default_factory=dict → 不传时给空字典 {}
        description="参数名→数值，例如 {'R': 1000, 'C': 1e-6, 'cutoff_freq': 159}",
    )                                                   # dict[str, float] = "键是字符串，值是浮点数"

    topology: str = Field(                               # topology: 电路拓扑
        default="",                                      # 例如 "串联RC" / "并联RLC"
        description="拓扑描述，例如 串联RC / 并联RLC",
    )

    constraints: list[str] = Field(                      # constraints: 约束列表
        default_factory=list,                            # list[str] = "字符串列表"
        description="约束条件列表，例如 ['截止频率约1kHz', '电阻取标准值']",
    )

    raw_input: str = Field(                              # raw_input: 用户原始输入
        description="用户原始输入文本，用于追溯",            # 没有 default → 创建时必须传入
    )

    clarification_rounds: int = Field(                   # clarification_rounds: 精炼轮数
        default=0,                                       # 记录节点1 跟用户反问了多少轮
        description="需求精炼对话轮数",
    )

    missing_fields: list[str] = Field(                   # missing_fields: 仍缺的字段
        default_factory=list,                            # 为空列表 = 信息完整
        description="检查后仍缺失的字段名（为空=完整）",
    )

    created_at: str = Field(                             # created_at: 创建时间
        default_factory=lambda: datetime.now().isoformat(),
        # lambda 是一个"工厂函数单行写法"：每次创建实例时自动调用
        # datetime.now().isoformat() 返回 "2026-05-26T16:30:00"
        description="创建时间 ISO 字符串",
    )

    @property                                            # @property 让它像字段一样调用，但值是计算的
    def is_complete(self) -> bool:
        """missing_fields 为空 = 需求完整"""
        return len(self.missing_fields) == 0


# ====================================================================
# 节点 2 产出：SysMLArtifact（SysML v2 代码产物）
# ====================================================================
# SysML v2 = OMG 发布的系统建模语言（文本语法）。
# LLM 生成 .sysml 文件，用户用 Eclipse 打开看图。
# ====================================================================

class SysMLArtifact(BaseModel):
    """LLM 生成的 SysML v2 文本代码。用户手动 Eclipse 看图。"""

    sysml_code: str = Field(                             # sysml_code: 生成的代码文本
        default="",
        description="SysML v2 文本代码内容",
    )

    file_path: str = Field(                              # file_path: 保存路径
        default="",                                      # 例如 "outputs/run_xxx/sysml/model.sysml"
        description="保存到磁盘的路径",
    )

    attempts: int = Field(                               # attempts: 尝试次数
        default=1,                                       # LLM 重试了几次才通过语法检查
        description="生成尝试次数（含重试）",
    )

    errors: list[str] = Field(                           # errors: 每次尝试的错误
        default_factory=list,
        description="各次尝试的语法/逻辑错误",
    )

    source_requirement: Optional[StructuredRequirement] = Field(
        default=None,                                    # Optional[X] = X | None
        description="回溯：来自哪个需求",                  # 可以追溯到是哪个 req 生成的这个 SysML
    )


# ====================================================================
# 节点 3 产出：ModelicaArtifact（Modelica 仿真产物）
# ====================================================================
# Modelica = 多域物理建模语言。OpenModelica(OMC) 编译并仿真。
# 本类记录 .mo 代码 + 编译/仿真结果 + 数据文件。
# ====================================================================

class ModelicaArtifact(BaseModel):
    """LLM 生成 Modelica 代码 + OMC 编译仿真结果。"""

    modelica_code: str = Field(                          # modelica_code: LLM 生成的代码
        default="",
        description="Modelica .mo 文本代码",
    )

    file_path: str = Field(                              # file_path: .mo 文件路径
        default="",
        description=".mo 文件保存路径",
    )

    csv_path: str = Field(                               # csv_path: 仿真数据
        default="",                                      # OpenModelica 输出 CSV 格式
        description="仿真结果 CSV 路径",
    )

    plot_path: str = Field(                              # plot_path: 仿真曲线图
        default="",                                      # matplotlib 生成的 PNG
        description="仿真曲线 PNG 路径",
    )

    attempts: int = Field(                               # attempts: 总尝试次数
        default=1,                                       # 编译 + 仿真失败后重试的次数
        description="生成+编译尝试次数",
    )

    errors: list[str] = Field(                           # errors: 编译/仿真错误
        default_factory=list,
        description="各次编译/仿真错误信息",
    )

    success: bool = Field(                               # success: 最终是否成功
        default=False,                                   # True = 编译通过 + 仿真跑出了 CSV
        description="最终是否通过编译并跑出仿真",
    )


# ====================================================================
# 节点 4 产出：SummaryArtifact（流程总结）
# ====================================================================
# 把前面 3 个节点的产物汇总，LLM 写人类可读的 Markdown 总结。
# ====================================================================

class SummaryArtifact(BaseModel):
    """汇总前面 3 个节点的产出，生成人类可读的总结。"""

    summary_text: str = Field(                           # summary_text: 总结全文
        description="Markdown 格式的总结全文",
    )

    file_path: str = Field(                              # file_path: summary.md 路径
        default="",
        description="summary.md 路径",
    )

    requirement_path: str = Field(                       # 引用：需求文件路径
        default="",
    )

    sysml_path: str = Field(                             # 引用：SysML 文件路径
        default="",
    )

    modelica_path: str = Field(                          # 引用：Modelica 文件路径
        default="",
    )

    plot_path: str = Field(                              # 引用：仿真曲线图路径
        default="",
    )
