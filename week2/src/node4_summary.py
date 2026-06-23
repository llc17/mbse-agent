"""
节点 4 — 总结生成：汇总前 3 个节点的产出，LLM 拼 summary.md。

用法：
    from src.node4_summary import generate_summary
    summary = generate_summary(req, sysml, mo, work_dir)
"""

import os
from pathlib import Path

from src.llm_client import chat, user_msg
from src.schemas import (
    StructuredRequirement,
    SysMLArtifact,
    ModelicaArtifact,
    SummaryArtifact,
)


def generate_summary(
    req: StructuredRequirement,
    sysml_artifact: SysMLArtifact,
    mo_artifact: ModelicaArtifact,
    results_dir: Path,
) -> SummaryArtifact:
    """把全流程产物汇总为人类可读的 Markdown 总结。"""
    prompt = f"""你是一个系统工程报告撰写人。请根据以下全流程产出的数据，写一份简洁的系统设计总结报告。

## 需求
{req.model_dump_json(indent=2)}

## SysML v2 代码（节选前 1000 字符）
{sysml_artifact.sysml_code[:1000]}

## Modelica 代码（节选前 1000 字符）
{mo_artifact.modelica_code[:1000]}

## 仿真结果
- 编译+仿真成功: {mo_artifact.success}
- 尝试次数: {mo_artifact.attempts}
- 错误记录: {chr(10).join(f'  - {e[:100]}' for e in mo_artifact.errors) if mo_artifact.errors else '无'}

## 要求
写一份 Markdown 格式的总结，包含以下章节：
1. 项目概述（一句话）
2. 系统参数（表格）
3. 架构说明（基于 SysML v2 的关键部件和连接）
4. 仿真结果（成功/失败，关键曲线变量名）
5. 反思与下一步（哪些环节顺利，哪些需要改进）

总字数控制在 500 字以内。直接输出 Markdown。"""

    print("[节点4] 生成总结...")
    summary_text = chat([user_msg(prompt)], temperature=0.3, max_tokens=2048)

    file_path = results_dir / "summary.md"
    file_path.write_text(summary_text, encoding="utf-8")

    return SummaryArtifact(
        summary_text=summary_text,
        file_path=str(file_path),
        requirement_path=str(results_dir / "requirement.json"),
        sysml_path=sysml_artifact.file_path,
        modelica_path=mo_artifact.file_path,
        plot_path=mo_artifact.plot_path,
    )
