# -*- coding: utf-8 -*-
"""
=============================================================================
node4_summary.py — 节点 4：总结报告生成
=============================================================================

流水线的最后一站。全流程产物（需求 + SysML + Modelica + 仿真结果 + 质量检查
+ 修复日志）汇总后，让 LLM 写一份 Markdown 总结报告，保存为 summary.md。

这是一个"综述"节点 — 它不修改任何技术产物，只是把前面的结果整理成
人类可读的报告。

V3 新增: 质量检查结果和修复日志也会写入总结
V4: 基本保持不变（V4 的质量检查数据自动流到这里）

LangGraph 节点函数: node4_summary(state) → {summary, timing}
=============================================================================
"""

import time
import logging
from pathlib import Path

from src.llm_client import chat, user_msg
from src.schemas import StructuredRequirement, SysMLArtifact, ModelicaArtifact, SummaryArtifact

logger = logging.getLogger("node4")


def node4_summary(state: dict) -> dict:
    """
    LangGraph 节点函数: 生成 Markdown 总结报告。

    流程:
      1. 读取上游所有节点的产物（req, sysml, mo, quality_checks, repair_log）
      2. 构造 prompt: 把需求、SysML 代码、Modelica 代码、仿真结果、
         质量检查结果、修复日志全部嵌进去
      3. LLM 生成 500 字以内的 Markdown 总结
      4. 保存为 summary.md

    V3: 新增质量检查和修复日志段落
    """
    t0 = time.time()

    # —— 把 state 中的 dict 转回 Pydantic 对象 ——
    req = StructuredRequirement(**state.get("req", {}))
    sysml = SysMLArtifact(**state.get("sysml", {}))
    mo = ModelicaArtifact(**state.get("mo", {}))
    results_dir = Path(state.get("run_dir", ".")) / "results"
    temperature = state.get("temperature", 0.3)

    # —— 导入 json（用完即弃，只在需要时导入）——
    import json

    # V3: 汇总质量检查结果（交叉校验 + 物理验证）
    quality_checks = state.get("quality_checks", {})
    qc_section = ""
    if quality_checks:
        # 把整个 quality_checks dict 序列化为 JSON 嵌入 prompt
        qc_section = "## 质量检查 (V3)\n" + json.dumps(quality_checks, ensure_ascii=False, indent=2)

    # V3: 修复日志摘要（node3 每次 self-repair 的记录）
    repair_log = state.get("repair_log", [])
    repair_section = ""
    if repair_log:
        repair_section = f"\n## 修复日志（共 {len(repair_log)} 次修复）\n"
        for entry in repair_log:
            # 只展示每次修复前的错误信息（截断到 100 字符）
            repair_section += (
                f"- 第{entry['attempt']}次: "
                f"{entry['errors_before'][:100] if entry['errors_before'] else '无错误记录'}\n"
            )

    # —— 构造 prompt: 把所有信息喂给 LLM ——
    prompt = f"""你是一个系统工程报告撰写人。请根据全流程产出，写一份简洁的系统设计总结报告。

## 需求
{req.model_dump_json(indent=2)}

## SysML v2 代码（节选前 1000 字符）
{sysml.sysml_code[:1000]}

## Modelica 代码（节选前 1000 字符）
{mo.modelica_code[:1000]}

## 仿真结果
- 编译+仿真成功: {mo.success}
- 尝试次数: {mo.attempts}
- 错误记录: {chr(10).join(f'  - {e[:100]}' for e in mo.errors) if mo.errors else '无'}
    # ↑ chr(10) = 换行符 "\n"，因为在 f-string 里直接写 \n 是语法错误

{qc_section}
{repair_section}

## 要求
写一份 Markdown 格式的总结，包含：
1. 项目概述（一句话）
2. 系统参数（表格）
3. 架构说明（基于 SysML v2 的关键部件和连接）
4. 仿真结果（成功/失败，关键变量名）
5. 反思与下一步

总字数 500 字以内。直接输出 Markdown。"""

    logger.info("节点4 生成总结...")
    # temperature 用 state 里的一致值，但实际总结不需要太高创造性
    summary_text = chat([user_msg(prompt)], temperature=temperature, max_tokens=2048)

    # —— 保存到磁盘 ——
    results_dir.mkdir(parents=True, exist_ok=True)
    file_path = results_dir / "summary.md"
    file_path.write_text(summary_text, encoding="utf-8")

    # 构建 SummaryArtifact（Pydantic 对象）
    artifact = SummaryArtifact(
        summary_text=summary_text,
        file_path=str(file_path),
        requirement_path=str(results_dir / "requirement.json"),  # 需求也会写到这里
        sysml_path=sysml.file_path,
        modelica_path=mo.file_path,
        plot_path=mo.plot_path,  # 仿真曲线图
    )

    elapsed = time.time() - t0
    logger.info("节点4 完成 (%.1fs)", elapsed)

    return {
        "summary": artifact.model_dump(),  # Pydantic → dict
        "timing": {**state.get("timing", {}), "node4": elapsed},
    }
