# -*- coding: utf-8 -*-
"""
=============================================================================
node4_summary.py — 节点 4：总结生成（V3 版）
=============================================================================

V3 改动：summary prompt 中增加两个内容块——
  1. 质量检查结果（quality_checks JSON → 表格）
  2. 修复日志摘要（repair_log → 文本列表）

这样生成的 summary.md 会包含完整的三部曲质量报告，
可以一次看到：成功了没 / 交叉校验过了没 / 物理偏差多少 / 修了几次。
"""

import time
import logging
from pathlib import Path

from src.llm_client import chat, user_msg
from src.schemas import StructuredRequirement, SysMLArtifact, ModelicaArtifact, SummaryArtifact

logger = logging.getLogger("node4")


def node4_summary(state: dict) -> dict:
    """LangGraph 节点：生成最终总结报告"""

    t0 = time.time()
    req = StructuredRequirement(**state.get("req", {}))
    sysml = SysMLArtifact(**state.get("sysml", {}))
    mo = ModelicaArtifact(**state.get("mo", {}))
    results_dir = Path(state.get("run_dir", ".")) / "results"
    temperature = state.get("temperature", 0.3)

    # ── V3: 汇总质量检查结果 ──
    import json
    quality_checks = state.get("quality_checks", {})
    qc_section = ""
    if quality_checks:
        qc_section = "## 质量检查 (V3)\n" + json.dumps(quality_checks, ensure_ascii=False, indent=2)

    # ── V3: 修复日志摘要 ──
    repair_log = state.get("repair_log", [])
    repair_section = ""
    if repair_log:
        repair_section = f"\n## 修复日志（共 {len(repair_log)} 次修复）\n"
        for entry in repair_log:
            repair_section += (
                f"- 第{entry['attempt']}次: "
                f"{entry['errors_before'][:100] if entry['errors_before'] else '无错误记录'}\n"
            )

    # ---- 构造总结 prompt ----
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

{qc_section}
{repair_section}

## 要求
写一份 Markdown 格式的总结，包含：
1. 项目概述（一句话）
2. 系统参数（表格）
3. 架构说明（基于 SysML v2 的关键部件和连接）
4. 仿真结果（成功/失败，关键变量名）
5. 质量检查（交叉校验结果、物理验证偏差）
6. 反思与下一步

总字数 600 字以内。直接输出 Markdown。"""

    logger.info("节点4 生成总结...")
    summary_text = chat([user_msg(prompt)], temperature=temperature, max_tokens=2048)

    results_dir.mkdir(parents=True, exist_ok=True)
    file_path = results_dir / "summary.md"
    file_path.write_text(summary_text, encoding="utf-8")

    artifact = SummaryArtifact(
        summary_text=summary_text,
        file_path=str(file_path),
        requirement_path=str(results_dir / "requirement.json"),
        sysml_path=sysml.file_path,
        modelica_path=mo.file_path,
        plot_path=mo.plot_path,
    )

    elapsed = time.time() - t0
    logger.info("节点4 完成 (%.1fs)", elapsed)

    return {
        "summary": artifact.model_dump(),
        "timing": {**state.get("timing", {}), "node4": elapsed},
    }
