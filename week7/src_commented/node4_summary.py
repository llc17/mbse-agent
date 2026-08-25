"""
=============================================================================
node4_summary.py — Agent ⑤ 总结 Agent（V6 三阶段自检版）
=============================================================================
定位: 流水线最后一站——汇总全流程产物，生成 Markdown 总结报告。
      V6 升级为三阶段 Agent: 生成报告 → 自查遗漏 → 补充。

Agent ⑤ 内部流程:
  收集全流程产物（req + sysml + modelica + quality_checks）
  → 生成 Markdown 报告 → 自查遗漏 → 如果缺关键信息 → 补充 → 通过

报告包含 7 个章节:
  1. 项目概述  2. 系统参数表  3. 架构说明  4. 仿真结果
  5. 质量检查  6. V6 Agent 循环统计  7. 反思与下一步
=============================================================================
"""

import json, logging, time
from pathlib import Path

from src.llm_client import chat, chat_with, user_msg
from src.agent_loop import (
    run_review_loop, ReviewResult, ReviewIssue,
    AgentLoopResult, parse_review_json,
)
from src.schemas import StructuredRequirement, SysMLArtifact, ModelicaArtifact, SummaryArtifact

logger = logging.getLogger("node4")


# ============================================================================
# LangGraph 节点入口
# ============================================================================

def node4_summary(state: dict) -> dict:
    """
    LangGraph 节点：生成总结（V6 自检版）。

    输入: state 中的 req + sysml + mo + quality_checks + timing
    输出: summary (SummaryArtifact) + timing
    """
    t0 = time.time()
    # ── 解析上游节点产出（Pydantic 类型校验）──
    req = StructuredRequirement(**state.get("req", {}))
    sysml = SysMLArtifact(**state.get("sysml", {}))
    mo = ModelicaArtifact(**state.get("mo", {}))
    results_dir = Path(state.get("run_dir", ".")) / "results"
    temperature = state.get("temperature", 0.3)

    # ── 汇总质量检查结果（只传关键字段给 LLM，避免 prompt 过长）──
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
        qc_section = "## 质量检查\n```\n" + json.dumps(qc_summary, ensure_ascii=False, indent=2) + "\n```"

    # ── 修复日志摘要（最多最近 5 次）──
    repair_log = state.get("repair_log", [])
    repair_section = ""
    if repair_log:
        repair_section = f"\n## 修复日志（共 {len(repair_log)} 次修复）\n"
        for entry in repair_log[-5:]:
            rc = entry.get("root_cause", {})
            rc_category = rc.get("category", "unknown") if isinstance(rc, dict) else "unknown"
            repair_section += (f"- 第{entry['attempt']}次 [{rc_category}]: "
                             f"{entry.get('errors_before', ['无'])[0][:80] if entry.get('errors_before') else '无错误'}\n")

    # ── V6 Agent 循环统计（各 Agent 耗时、评分等）──
    agent_stats = _build_agent_stats_section(state)

    # ── Stage 1: 生成报告 ──
    def generate_report() -> str:
        """用全流程产物生成 Markdown 总结报告"""
        prompt = (
            f"你是一个系统工程报告撰写人。请根据全流程产出，写一份系统设计总结报告。\n\n"
            f"## 需求\n{req.model_dump_json(indent=2)}\n\n"
            f"## SysML v2 代码（节选）\n```sysml\n{sysml.sysml_code[:1500]}\n```\n\n"
            f"## Modelica 代码（节选）\n```modelica\n{mo.modelica_code[:1500]}\n```\n\n"
            f"## 仿真结果\n- 编译+仿真成功: {mo.success}\n- 尝试次数: {mo.attempts}\n"
            f"- 错误记录: {chr(10).join(f'  - {e[:100]}' for e in mo.errors) if mo.errors else '无'}\n\n"
            f"{qc_section}\n{repair_section}\n{agent_stats}\n\n"
            f"## 要求\n写一份 Markdown 格式的总结（500-800 字），含：\n"
            f"1. 项目概述  2. 系统参数表  3. 架构说明  4. 仿真结果\n"
            f"5. 质量检查  6. V6 Agent 统计  7. 反思与下一步\n直接输出 Markdown。"
        )
        return chat([user_msg(prompt)], temperature=temperature, max_tokens=2048)

    # ── Stage 2: 自查遗漏 ──
    def review_report(report: str) -> ReviewResult:
        """检查报告是否遗漏关键信息（4 条检查项）"""
        review_prompt = (
            f"## 角色\n你是技术报告审查员。检查这份系统设计总结是否遗漏关键信息。\n\n"
            f"## 报告\n{report}\n\n"
            f"## 可用源数据\n- 需求参数: {json.dumps(req.parameters, ensure_ascii=False)}\n"
            f"- 仿真成功: {mo.success}\n- 尝试次数: {mo.attempts}\n"
            f"- 质量检查通过: {all(c.get('passed', True) for c in quality_checks.values()) if quality_checks else True}\n"
            f"- 质量检查项: {', '.join(quality_checks.keys()) if quality_checks else '无'}\n\n"
            f"## 检查清单（逐条执行）\n"
            f"1. 参数表含所有关键参数？（R/C/L/温度等都在表中）\n"
            f"2. 仿真含数值对比？（截止频率实测vs理论等）\n"
            f"3. 质量检查未通过项在报告中提及？\n"
            f"4. 反思基于实际数据？（非泛泛之谈）\n\n"
            f"## 输出 JSON（只报遗漏，基本完整则 ok=true）\n"
            f'{{"ok": true, "score": 95, "issues": [], "summary": "报告完整"}}'
        )
        review_raw = chat_with("review", [user_msg(review_prompt)], temperature=0.1, max_tokens=512)
        return parse_review_json(review_raw)

    # ── Stage 3: 补充遗漏 ──
    def revise_report(report: str, issues: list[ReviewIssue]) -> str:
        """补充报告中的遗漏信息"""
        issues_text = "\n".join(f"- {i.description} → {i.suggestion}" for i in issues)
        revise_prompt = (
            f"## 角色\n你是技术报告修正员。补充遗漏信息。\n\n"
            f"## 当前报告\n{report}\n\n## 需要补充\n{issues_text}\n\n"
            f"## 源数据\n- 需求: {json.dumps({'component_type': req.component_type, 'parameters': req.parameters, 'topology': req.topology}, ensure_ascii=False, indent=2)}\n"
            f"- 仿真: success={mo.success}, attempts={mo.attempts}\n"
            f"- 质量: {json.dumps({k: {'passed': v.get('passed')} for k, v in quality_checks.items()}, ensure_ascii=False, indent=2)}\n\n"
            f"## 要求\n1. 在原文基础上补充  2. 保持结构和风格  3. 输出完整的修正后 Markdown"
        )
        return chat([user_msg(revise_prompt)], temperature=0.2, max_tokens=2048)

    # ── 运行三阶段循环 ──
    loop_result = run_review_loop(
        generate_fn=generate_report, review_fn=review_report,
        revise_fn=revise_report, max_rounds=2, label="node4",
    )
    summary_text = loop_result.final_output

    # ── 保存文件 ──
    results_dir.mkdir(parents=True, exist_ok=True)
    file_path = results_dir / "summary.md"
    file_path.write_text(summary_text, encoding="utf-8")

    artifact = SummaryArtifact(
        summary_text=summary_text, file_path=str(file_path),
        requirement_path=str(results_dir / "requirement.json"),
        sysml_path=sysml.file_path, modelica_path=mo.file_path, plot_path=mo.plot_path,
    )

    elapsed = time.time() - t0
    logger.info("节点4 完成 (%.1fs), 审查轮数=%s, 通过=%s",
                elapsed, loop_result.rounds, loop_result.passed)
    return {
        "summary": artifact.model_dump(),
        "timing": {**state.get("timing", {}), "node4": elapsed},
    }


# ============================================================================
# V6: Agent 循环统计构建
# ============================================================================

def _build_agent_stats_section(state: dict) -> str:
    """构建 V6 Agent 循环统计信息（各阶段耗时 + 质量通过情况 + 修复统计）"""
    timing = state.get("timing", {})
    quality_checks = state.get("quality_checks", {})
    repair_log = state.get("repair_log", [])

    parts = ["## V6 Agent 循环统计"]

    # ── 各阶段耗时 ──
    parts.append("\n### 各阶段耗时")
    for node_name in ["node1", "node2", "node3_generate", "node3_compile",
                       "node3_simulate", "q_cross_validate", "q_physics_validate", "node4"]:
        if node_name in timing:
            parts.append(f"- {node_name}: {timing[node_name]:.1f}s")

    # ── 质量检查通过情况 ──
    if quality_checks:
        parts.append("\n### 质量检查通过情况")
        for check_name, check_data in quality_checks.items():
            status = "PASS" if check_data.get("passed") else "FAIL"
            issues_count = len(check_data.get("issues", []))
            parts.append(f"- {status} {check_name}: {issues_count} 个问题")

    # ── 修复日志统计（含根因分布）──
    if repair_log:
        parts.append(f"\n### 修复统计\n共 {len(repair_log)} 次修复")
        root_causes = {}
        for entry in repair_log:
            rc = entry.get("root_cause", {})
            cat = rc.get("category", "unknown") if isinstance(rc, dict) else "unknown"
            root_causes[cat] = root_causes.get(cat, 0) + 1
        for cat, count in root_causes.items():
            parts.append(f"- {cat}: {count} 次")

    return "\n".join(parts)
