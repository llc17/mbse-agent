"""
节点 4 — 总结 Agent（V6 三阶段自检版）。

V4 → V6 变更:
  - 单次 chat() → 生成报告 → 自查遗漏 → 补充（三阶段 Agent 循环）
  - 自查依据 quality_checks 结果，确认是否遗漏关键发现
  - 报告含 V6 Agent 循环统计（各 Agent 的审查轮数、评分、token 消耗）

Agent ⑤ 内部流程:
  生成总结报告 → 自查遗漏 → 如果缺少关键信息 → 补充 → 通过
"""

import json
import logging
import time
from pathlib import Path

from src.llm_client import chat, chat_with, user_msg
from src.agent_loop import (
    run_review_loop,
    ReviewResult,
    ReviewIssue,
    AgentLoopResult,
    parse_review_json,
)
from src.schemas import StructuredRequirement, SysMLArtifact, ModelicaArtifact, SummaryArtifact

logger = logging.getLogger("node4")


def node4_summary(state: dict) -> dict:
    """LangGraph 节点：生成总结（V6 自检版）。"""
    t0 = time.time()
    req = StructuredRequirement(**state.get("req", {}))
    sysml = SysMLArtifact(**state.get("sysml", {}))
    mo = ModelicaArtifact(**state.get("mo", {}))
    results_dir = Path(state.get("run_dir", ".")) / "results"
    temperature = state.get("temperature", 0.3)

    # ── 汇总质量检查结果 ──
    quality_checks = state.get("quality_checks", {})
    qc_section = ""
    if quality_checks:
        qc_summary = {}
        for check_name, check_data in quality_checks.items():
            qc_summary[check_name] = {
                "passed": check_data.get("passed"),
                "issues_count": len(check_data.get("issues", [])),
                "details": check_data.get("details", ""),
            }
        qc_section = "## 质量检查\n```json\n" + json.dumps(qc_summary, ensure_ascii=False, indent=2) + "\n```"

    # ── 修复日志摘要 ──
    repair_log = state.get("repair_log", [])
    repair_section = ""
    if repair_log:
        repair_section = f"\n## 修复日志（共 {len(repair_log)} 次修复）\n"
        for entry in repair_log[-5:]:  # 最多展示最近 5 次
            rc = entry.get("root_cause", {})
            rc_category = rc.get("category", "unknown") if isinstance(rc, dict) else "unknown"
            repair_section += (f"- 第{entry['attempt']}次 "
                             f"[{rc_category}]: "
                             f"{entry.get('errors_before', ['无'])[0][:80] if entry.get('errors_before') else '无错误'}\n")

    # ── V6 Agent 循环统计 ──
    timing = state.get("timing", {})
    agent_stats = _build_agent_stats_section(state)

    # ── Stage 1: 生成报告 ──
    def generate_report() -> str:
        prompt = f"""你是一个系统工程报告撰写人。请根据全流程产出，写一份系统设计总结报告。

## 需求
{req.model_dump_json(indent=2)}

## SysML v2 代码（节选前 1500 字符）
```sysml
{sysml.sysml_code[:1500]}
```

## Modelica 代码（节选前 1500 字符）
```modelica
{mo.modelica_code[:1500]}
```

## 仿真结果
- 编译+仿真成功: {mo.success}
- 尝试次数: {mo.attempts}
- 错误记录: {chr(10).join(f'  - {e[:100]}' for e in mo.errors) if mo.errors else '无'}

{qc_section}
{repair_section}
{agent_stats}

## 要求
写一份 Markdown 格式的总结，包含：
1. **项目概述**（一句话）
2. **系统参数**（表格形式：参数名 | 值 | 单位）
3. **架构说明**（基于 SysML v2 的关键部件和连接关系）
4. **仿真结果**（成功/失败，关键变量名，与理论值对比）
5. **质量检查结果**（各检查项通过/失败，主要问题摘要）
6. **V6 Agent 循环统计**（各 Agent 审查轮数、最终评分）
7. **反思与下一步**（如果仿真失败，分析可能原因；如果成功，提出可改进的方向）

总字数 500-800 字。直接输出 Markdown。"""

        result = chat([user_msg(prompt)], temperature=temperature, max_tokens=2048)
        return result

    # ── Stage 2: 自查遗漏 ──
    def review_report(report: str) -> ReviewResult:
        """检查报告是否遗漏关键信息。"""
        review_prompt = f"""## 角色
你是技术报告审查员。检查这份系统设计总结是否遗漏关键信息。

## 报告
{report}

## 可用的源数据摘要
- 需求参数: {json.dumps(req.parameters, ensure_ascii=False)}
- 仿真成功: {mo.success}
- 仿真尝试次数: {mo.attempts}
- 质量检查通过: {all(c.get('passed', True) for c in quality_checks.values()) if quality_checks else True}
- 主要质量检查: {', '.join(quality_checks.keys()) if quality_checks else '无'}

## 检查清单（逐条执行）
1. 系统参数表格是否包含所有关键参数？（如R/C/L/温度等都应在表中）
2. 仿真结果是否包含具体数值对比（如截止频率实测vs理论、稳态温度vs期望）？
3. 质量检查的每个未通过项是否在报告中提及？
4. 反思部分是否基于实际数据（而非泛泛的"可以改进"）？

## 输出格式（严格 JSON）
```json
{{
  "ok": true/false,
  "score": 0-100,
  "issues": [
    {{
      "severity": "error|warning",
      "category": "completeness",
      "description": "遗漏了X信息",
      "location": "报告中的章节",
      "suggestion": "补充X内容"
    }}
  ],
  "summary": "审查总结"
}}
```

## 重要规则
1. 只报实际遗漏的关键信息
2. 缺少1-2个细节不算问题
3. 如果报告基本完整，ok=true
4. 不要输出 JSON 之外的任何内容"""

        review_raw = chat_with("review", [user_msg(review_prompt)], temperature=0.1, max_tokens=512)
        return parse_review_json(review_raw)

    # ── Stage 3: 补充遗漏 ──
    def revise_report(report: str, issues: list[ReviewIssue]) -> str:
        """补充报告中的遗漏信息。"""
        issues_text = "\n".join(
            f"- {i.description} → {i.suggestion}" for i in issues
        )

        revise_prompt = f"""## 角色
你是技术报告修正员。根据审查发现的问题补充报告中的遗漏信息。

## 当前报告
{report}

## 需要补充的内容
{issues_text}

## 源数据（用于补充）
- 需求: {json.dumps({'component_type': req.component_type, 'parameters': req.parameters, 'topology': req.topology, 'constraints': req.constraints}, ensure_ascii=False, indent=2)}
- 仿真状态: success={mo.success}, attempts={mo.attempts}
- 质量检查: {json.dumps({k: {'passed': v.get('passed'), 'issues': len(v.get('issues', []))} for k, v in quality_checks.items()}, ensure_ascii=False, indent=2)}

## 要求
1. 在原报告基础上补充遗漏信息
2. 保持原报告的结构和风格
3. 只输出完整的修正后报告（Markdown）"""

        revised = chat([user_msg(revise_prompt)], temperature=0.2, max_tokens=2048)
        return revised

    # ── 运行三阶段循环 ──
    loop_result = run_review_loop(
        generate_fn=generate_report,
        review_fn=review_report,
        revise_fn=revise_report,
        max_rounds=2,  # 报告检查 2 轮足够
        label="node4",
    )

    summary_text = loop_result.final_output

    # ── 保存 ──
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
    logger.info("节点4 完成 (%.1fs), 审查轮数=%s, 通过=%s",
                elapsed, loop_result.rounds, loop_result.passed)

    return {
        "summary": artifact.model_dump(),
        "timing": {**state.get("timing", {}), "node4": elapsed},
    }


def _build_agent_stats_section(state: dict) -> str:
    """构建 V6 Agent 循环统计信息。"""
    timing = state.get("timing", {})
    quality_checks = state.get("quality_checks", {})
    repair_log = state.get("repair_log", [])

    parts = ["## V6 Agent 循环统计"]

    # 各节点耗时
    parts.append("\n### 各阶段耗时")
    for node_name in ["node1", "node2", "node3_generate", "node3_compile",
                       "node3_simulate", "q_cross_validate", "q_physics_validate", "node4"]:
        if node_name in timing:
            parts.append(f"- {node_name}: {timing[node_name]:.1f}s")

    # 质量检查通过情况
    if quality_checks:
        parts.append("\n### 质量检查通过情况")
        for check_name, check_data in quality_checks.items():
            status = "✅" if check_data.get("passed") else "❌"
            issues_count = len(check_data.get("issues", []))
            parts.append(f"- {status} {check_name}: {issues_count} 个问题")

    # 修复日志统计
    if repair_log:
        parts.append(f"\n### 修复统计\n共 {len(repair_log)} 次修复")
        # 统计根因分布
        root_causes = {}
        for entry in repair_log:
            rc = entry.get("root_cause", {})
            if isinstance(rc, dict):
                cat = rc.get("category", "unknown")
            else:
                cat = "unknown"
            root_causes[cat] = root_causes.get(cat, 0) + 1
        for cat, count in root_causes.items():
            parts.append(f"- {cat}: {count} 次")

    return "\n".join(parts)
