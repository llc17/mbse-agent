# -*- coding: utf-8 -*-
"""
=============================================================================
node4_summary.py — 节点4：总结生成
=============================================================================

把前面 3 个节点的所有产出汇总，LLM 写一份 Markdown 总结报告。

输入:
  - StructuredRequirement（需求）
  - SysMLArtifact（.sysml 代码节选）
  - ModelicaArtifact（.mo 代码节选 + 仿真成功/失败状态）

输出:
  - SummaryArtifact（summary.md 文件）
"""

# ====================================================================
# 导入
# ====================================================================

import os                                                # 路径操作
from pathlib import Path

from src.llm_client import chat, user_msg
from src.schemas import (
    StructuredRequirement,
    SysMLArtifact,
    ModelicaArtifact,
    SummaryArtifact,
)


# ====================================================================
# generate_summary() — 节点4 主入口
# ====================================================================
def generate_summary(
    req: StructuredRequirement,                           # 节点1 需求
    sysml_artifact: SysMLArtifact,                       # 节点2 SysML 代码
    mo_artifact: ModelicaArtifact,                       # 节点3 仿真结果
    results_dir: Path,                                   # 输出目录
) -> SummaryArtifact:
    """
    把全流程产物汇总为人类可读的 Markdown 总结。

    prompt 包含:
      - 需求 JSON 全文
      - SysML v2 代码前 1000 字符
      - Modelica 代码前 1000 字符
      - 仿真结果（成功/失败 + 错误记录）

    LLM 按照 5 个章节生成 500 字以内的总结。
    """
    # ---- 构造 prompt ----
    prompt = f"""你是一个系统工程报告撰写人。请根据以下全流程产出的数据，写一份简洁的系统设计总结报告。

## 需求
{req.model_dump_json(indent=2)}
# .model_dump_json() 把 Pydantic 对象序列化为格式化的 JSON 字符串

## SysML v2 代码（节选前 1000 字符）
{sysml_artifact.sysml_code[:1000]}
# 只取前 1000 字符 —— 防止 prompt 超 token 限制

## Modelica 代码（节选前 1000 字符）
{mo_artifact.modelica_code[:1000]}

## 仿真结果
- 编译+仿真成功: {mo_artifact.success}
- 尝试次数: {mo_artifact.attempts}
- 错误记录: {
    chr(10).join(f'  - {e[:100]}' for e in mo_artifact.errors)
    # chr(10) = 换行符 \n
    # 每条错误只取前 100 字符
    if mo_artifact.errors else '无'
}

## 要求
写一份 Markdown 格式的总结，包含以下章节：
1. 项目概述（一句话）
2. 系统参数（表格）
3. 架构说明（基于 SysML v2 的关键部件和连接）
4. 仿真结果（成功/失败，关键曲线变量名）
5. 反思与下一步（哪些环节顺利，哪些需要改进）

总字数控制在 500 字以内。直接输出 Markdown。"""

    # ---- 调 LLM 生成总结 ----
    print("[节点4] 生成总结...")
    summary_text = chat(
        [user_msg(prompt)],
        temperature=0.3,                                 # 中等温度，允许一点创意
        max_tokens=2048,                                 # 500 字中文 ≈ 1000 tokens，留足空间
    )

    # ---- 保存到文件 ----
    file_path = results_dir / "summary.md"
    file_path.write_text(summary_text, encoding="utf-8")

    # ---- 返回产物 ----
    return SummaryArtifact(
        summary_text=summary_text,
        file_path=str(file_path),
        requirement_path=str(results_dir / "requirement.json"),
        sysml_path=sysml_artifact.file_path,
        modelica_path=mo_artifact.file_path,
        plot_path=mo_artifact.plot_path,
    )
