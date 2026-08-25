"""
LangGraph 状态图 — V6 5-Agent 版核心编排。

V4 → V6 变更:
  - 各节点内部升级为三阶段 Agent（生成→审查→修正），图拓扑不变
  - 路由函数改用 LLM 根因分析（替代 V4 关键词匹配）
  - 新增断路器逻辑：审查循环超限 → 标记问题转 HITL
  - 支持跨步一致性检查（SysML↔Modelica）

主图结构（继承 V4，不变）:
  START → node1_refine → node1_hitl → node2_generate → Q_cross_validate
       → node2_hitl → node3_subgraph → node3_hitl → Q_physics_validate → node4_summary → END

V6 关键路由变更:
  - _route_after_node3: 不再用关键词匹配，改为读取 node3 子图输出的 root_cause
  - 根因分析由 node3 repair 中的 LLM 调用完成，pipeline 只读结论

用法:
    from src.pipeline import build_pipeline, PipelineState
    graph = build_pipeline()
    graph.invoke(initial_state, config)
"""

import logging
from typing import TypedDict, Optional, Any

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt

from src.node1_requirement import node1_refine
from src.node2_sysml import node2_generate
from src.node3_modelica import build_node3_subgraph
from src.node4_summary import node4_summary
from src.node_quality import q_cross_validate, q_physics_validate

logger = logging.getLogger("pipeline")


class PipelineState(TypedDict, total=False):
    """贯通全图的状态对象。所有字段 JSON 可序列化。"""
    raw_input: str
    req: Optional[dict]
    sysml: Optional[dict]
    mo: Optional[dict]
    summary: Optional[dict]
    node_status: dict[str, str]          # "pending" | "approved" | "rejected"
    human_feedback: str
    reject_count_per_node: dict[str, int]
    temperature: float
    max_retries: int
    max_rejects: int
    dialogue_history: list[dict]
    timing: dict[str, float]
    run_dir: str
    mode: str                            # "interactive" | "experiment"
    # V3/V4 保留
    quality_checks: dict
    repair_log: list[dict]
    physics_feedback: str
    expected_physics: Optional[dict]
    # V6 新增
    circuit_breaker_triggered: bool      # 断路器是否触发（审查循环超限）
    breaker_details: str                 # 断路器触发时的详情（给 HITL 展示）


def build_pipeline() -> StateGraph:
    """构建 V6 MBSE 主流水线状态图。"""
    builder = StateGraph(PipelineState)

    # 注册节点（同 V4 拓扑）
    builder.add_node("node1_refine", node1_refine)
    builder.add_node("node1_hitl", _node1_hitl)
    builder.add_node("node2_generate", node2_generate)
    builder.add_node("Q_cross_validate", q_cross_validate)
    builder.add_node("node2_hitl", _node2_hitl)
    builder.add_node("node3_subgraph", build_node3_subgraph())
    builder.add_node("node3_hitl", _node3_hitl)
    builder.add_node("Q_physics_validate", q_physics_validate)
    builder.add_node("node4_summary", node4_summary)

    # 连线（同 V4 拓扑）
    builder.add_edge(START, "node1_refine")
    builder.add_edge("node1_refine", "node1_hitl")

    builder.add_conditional_edges("node1_hitl", _route_after_hitl1, {
        "node1_refine": "node1_refine",
        "node2_generate": "node2_generate",
    })

    builder.add_edge("node2_generate", "Q_cross_validate")
    builder.add_conditional_edges("Q_cross_validate", _route_after_cross_validate, {
        "node2_hitl": "node2_hitl",
        "node2_generate": "node2_generate",
    })

    builder.add_conditional_edges("node2_hitl", _route_after_hitl2, {
        "node2_generate": "node2_generate",
        "node3_subgraph": "node3_subgraph",
    })

    # V6: node3 路由 — 使用 LLM 根因分析结果
    builder.add_conditional_edges("node3_subgraph", _route_after_node3, {
        "node3_hitl": "node3_hitl",
        "node1_refine": "node1_refine",
        "node2_generate": "node2_generate",
        "node4_summary": "node4_summary",
    })
    builder.add_conditional_edges("node3_hitl", _route_after_node3_hitl, {
        "Q_physics_validate": "Q_physics_validate",
        "node3_subgraph": "node3_subgraph",
        "node2_generate": "node2_generate",
    })
    builder.add_conditional_edges("Q_physics_validate", _route_after_physics, {
        "node4_summary": "node4_summary",
        "node3_subgraph": "node3_subgraph",
    })

    builder.add_edge("node4_summary", END)

    return builder.compile(checkpointer=MemorySaver())


# ============================================================
# HITL 节点（继承 V4，微调展示内容）
# ============================================================

def _node1_hitl(state: PipelineState) -> dict:
    if state.get("mode") == "experiment":
        ns = dict(state.get("node_status", {}))
        ns["node1"] = "approved"
        return {"node_status": ns}

    req = state.get("req", {})
    decision = interrupt({
        "node": "node1",
        "type": "hitl_confirm",
        "message": "Agent ① 需求分析完成 — 请确认结构化需求",
        "data": {
            "component_type": req.get("component_type"),
            "parameters": req.get("parameters"),
            "topology": req.get("topology"),
            "constraints": req.get("constraints"),
            "clarification_rounds": req.get("clarification_rounds"),
        },
    })

    if isinstance(decision, dict) and decision.get("action") == "reject":
        rejects = dict(state.get("reject_count_per_node", {}))
        rejects["node1"] = rejects.get("node1", 0) + 1
        logger.info("节点1 HITL: 用户打回 (第%s次)", rejects["node1"])
        return {
            "node_status": {**state.get("node_status", {}), "node1": "rejected"},
            "human_feedback": decision.get("feedback", ""),
            "reject_count_per_node": rejects,
        }

    logger.info("节点1 HITL: 用户确认")
    return {"node_status": {**state.get("node_status", {}), "node1": "approved"}}


def _node2_hitl(state: PipelineState) -> dict:
    if state.get("mode") == "experiment":
        ns = dict(state.get("node_status", {}))
        ns["node2"] = "approved"
        return {"node_status": ns}

    sysml = state.get("sysml", {})
    quality_checks = state.get("quality_checks", {})
    cross = quality_checks.get("cross_validate", {})

    decision = interrupt({
        "node": "node2",
        "type": "hitl_confirm",
        "message": "Agent ② SysML 建模完成 — 请用 Eclipse 查看 SysML 图后确认",
        "data": {
            "file_path": sysml.get("file_path"),
            "attempts": sysml.get("attempts"),
            "errors": sysml.get("errors"),
            "cross_validate_passed": cross.get("passed"),
            "cross_validate_score": cross.get("score", "N/A"),
        },
    })

    if isinstance(decision, dict) and decision.get("action") == "reject":
        rejects = dict(state.get("reject_count_per_node", {}))
        rejects["node2"] = rejects.get("node2", 0) + 1
        logger.info("节点2 HITL: 用户打回 (第%s次)", rejects["node2"])
        return {
            "node_status": {**state.get("node_status", {}), "node2": "rejected"},
            "human_feedback": decision.get("feedback", ""),
            "reject_count_per_node": rejects,
        }

    logger.info("节点2 HITL: 用户确认")
    return {"node_status": {**state.get("node_status", {}), "node2": "approved"}}


def _node3_hitl(state: PipelineState) -> dict:
    """V6: 展示仿真结果 + 断路器信息（如有）。"""
    if state.get("mode") == "experiment":
        ns = dict(state.get("node_status", {}))
        ns["node3"] = "approved"
        return {"node_status": ns}

    mo = state.get("mo", {})
    quality_checks = state.get("quality_checks", {})
    repair_log = state.get("repair_log", [])
    breaker_info = ""
    if state.get("circuit_breaker_triggered"):
        breaker_info = state.get("breaker_details", "")

    decision = interrupt({
        "node": "node3",
        "type": "hitl_confirm",
        "message": "Agent ③ Modelica 仿真完成 — 请确认仿真结果",
        "data": {
            "success": mo.get("success"),
            "attempts": mo.get("attempts"),
            "plot_path": mo.get("plot_path"),
            "csv_path": mo.get("csv_path"),
            "errors": mo.get("errors", [])[-3:],
            "repair_count": len(repair_log),
            "root_cause": mo.get("root_cause"),          # V6: 根因分析结果
            "root_cause_detail": mo.get("root_cause_detail", ""),
            "breaker_triggered": state.get("circuit_breaker_triggered", False),
            "breaker_details": breaker_info,
            "quality_preview": {
                "physics_passed": quality_checks.get("physics_validate", {}).get("passed"),
                "physics_deviation": quality_checks.get("physics_validate", {}).get("deviation_percent"),
                "sysml_modelica_passed": quality_checks.get("sysml_modelica", {}).get("passed"),
            },
        },
    })

    if isinstance(decision, dict):
        action = decision.get("action", "approve")
        if action == "reject":
            rejects = dict(state.get("reject_count_per_node", {}))
            rejects["node3"] = rejects.get("node3", 0) + 1
            logger.info("节点3 HITL: 用户打回 Modelica 重做 (第%s次)", rejects["node3"])
            return {
                "node_status": {**state.get("node_status", {}), "node3": "rejected"},
                "human_feedback": decision.get("feedback", ""),
                "reject_count_per_node": rejects,
                "node3_reject_target": "modelica",
            }
        elif action == "reject_sysml":
            rejects = dict(state.get("reject_count_per_node", {}))
            rejects["node3"] = rejects.get("node3", 0) + 1
            logger.info("节点3 HITL: 用户打回 SysML 重做 (第%s次)", rejects["node3"])
            return {
                "node_status": {**state.get("node_status", {}), "node3": "rejected"},
                "human_feedback": decision.get("feedback", ""),
                "reject_count_per_node": rejects,
                "node3_reject_target": "sysml",
            }

    logger.info("节点3 HITL: 用户确认")
    return {"node_status": {**state.get("node_status", {}), "node3": "approved"}}


# ============================================================
# 路由函数（V6: 使用 LLM 根因分析替代关键词匹配）
# ============================================================

def _route_after_hitl1(state: PipelineState) -> str:
    ns = state.get("node_status", {})
    if ns.get("node1") == "approved":
        return "node2_generate"
    rejects = state.get("reject_count_per_node", {}).get("node1", 0)
    max_rj = state.get("max_rejects", 3)
    if rejects >= max_rj:
        logger.warning("节点1 打回 %s 次已达上限，强制继续", rejects)
        return "node2_generate"
    return "node1_refine"


def _route_after_cross_validate(state: PipelineState) -> str:
    qc = state.get("quality_checks", {})
    cross = qc.get("cross_validate", {})
    if cross.get("passed"):
        logger.info("交叉校验通过 → node2_hitl")
        return "node2_hitl"

    rejects = state.get("reject_count_per_node", {}).get("node2", 0)
    max_rj = state.get("max_rejects", 3)
    if rejects >= max_rj:
        logger.warning("交叉校验打回 %s 次已达上限，强制继续到 HITL", rejects)
        return "node2_hitl"

    logger.info("交叉校验失败 (score=%s): %s → 打回 node2",
                cross.get("score"), cross.get("issues", []))
    return "node2_generate"


def _route_after_hitl2(state: PipelineState) -> str:
    ns = state.get("node_status", {})
    if ns.get("node2") == "approved":
        return "node3_subgraph"
    rejects = state.get("reject_count_per_node", {}).get("node2", 0)
    max_rj = state.get("max_rejects", 3)
    if rejects >= max_rj:
        logger.warning("节点2 打回 %s 次已达上限，强制继续", rejects)
        return "node3_subgraph"
    return "node2_generate"


def _route_after_node3(state: PipelineState) -> str:
    """V6: 使用 LLM 根因分析结果路由。

    优先读取 node3 子图输出的 root_cause 字段（由 _repair_mo 中的 LLM 分析产生）。
    如果没有 root_cause（仿真直接成功或首次失败），回退到 V4 关键词匹配。
    """
    mo = state.get("mo", {})
    if mo.get("success"):
        return "node3_hitl"

    # ── V6: 优先使用 LLM 根因分析 ──
    root_cause = mo.get("root_cause", "")
    attempts = mo.get("attempts", 0)
    max_retries = state.get("max_retries", 5)

    # 断路器：重试耗尽
    if attempts >= max_retries:
        logger.warning("节点3 重试耗尽 (%s/%s)，强制到 node4", attempts, max_retries)
        return "node4_summary"

    if root_cause == "missing_parameters":
        logger.info("V6根因路由: 缺参数 → 打回 node1_refine")
        return "node1_refine"

    if root_cause == "sysml_topology":
        logger.info("V6根因路由: SysML拓扑 → 打回 node2_generate")
        return "node2_generate"

    # ── Fallback: V4 关键词匹配 ──
    errors = mo.get("errors", [])
    error_text = " ".join(errors[-3:]).lower() if errors else ""

    if any(kw in error_text for kw in ["parameter", "not found", "undeclared", "missing", "未定义"]):
        logger.info("Fallback路由: 需求参数不足 → 回到节点1")
        return "node1_refine"

    if any(kw in error_text for kw in ["connect", "port", "type mismatch", "equation", "连接"]):
        logger.info("Fallback路由: SysML 拓扑问题 → 回到节点2")
        return "node2_generate"

    logger.info("节点3 失败原因不明，继续到 node4 生成总结")
    return "node4_summary"


def _route_after_node3_hitl(state: PipelineState) -> str:
    ns = state.get("node_status", {})
    if ns.get("node3") == "approved":
        return "Q_physics_validate"

    rejects = state.get("reject_count_per_node", {}).get("node3", 0)
    max_rj = state.get("max_rejects", 3)
    target = state.get("node3_reject_target", "modelica")

    if rejects >= max_rj:
        logger.warning("节点3 HITL 打回 %s 次已达上限，强制继续到物理验证", rejects)
        return "Q_physics_validate"

    if target == "sysml":
        logger.info("节点3 HITL: 打回 SysML 重做")
        return "node2_generate"
    else:
        logger.info("节点3 HITL: 打回 Modelica 重做")
        return "node3_subgraph"


def _route_after_physics(state: PipelineState) -> str:
    qc = state.get("quality_checks", {})
    physics = qc.get("physics_validate", {})

    if physics.get("passed"):
        logger.info("物理验证通过 → node4_summary")
        return "node4_summary"

    mo = state.get("mo", {})
    attempts = mo.get("attempts", 0)
    max_retries = state.get("max_retries", 5)
    if attempts >= max_retries:
        logger.warning("node3 重试 %s/%s 已达上限，强制到 node4", attempts, max_retries)
        return "node4_summary"

    logger.info("物理验证失败 (偏差 %.1f%%) → 打回 node3 repair",
                physics.get("deviation_percent", 0))
    return "node3_subgraph"
