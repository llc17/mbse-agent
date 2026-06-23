"""
LangGraph 状态图 — V2 核心编排。

主图结构:
  START → node1_refine → node1_hitl → node2_generate → node2_hitl
       → node3_subgraph → node4_summary → END

HITL: node1_hitl, node2_hitl 处暂停，用户可确认/打回。
打回时路由回对应节点重新执行，打回超过 max_rejects 强制继续。

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

logger = logging.getLogger("pipeline")


class PipelineState(TypedDict, total=False):
    """贯通全图的状态对象。所有字段 JSON 可序列化（Pydantic → dict）。"""
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


def build_pipeline() -> StateGraph:
    """构建 MBSE 主流水线状态图。"""
    builder = StateGraph(PipelineState)

    # 注册节点
    builder.add_node("node1_refine", node1_refine)
    builder.add_node("node1_hitl", _node1_hitl)
    builder.add_node("node2_generate", node2_generate)
    builder.add_node("node2_hitl", _node2_hitl)
    builder.add_node("node3_subgraph", build_node3_subgraph())
    builder.add_node("node4_summary", node4_summary)

    # 连线
    builder.add_edge(START, "node1_refine")
    builder.add_edge("node1_refine", "node1_hitl")

    builder.add_conditional_edges("node1_hitl", _route_after_hitl1, {
        "node1_refine": "node1_refine",
        "node2_generate": "node2_generate",
    })

    builder.add_edge("node2_generate", "node2_hitl")

    builder.add_conditional_edges("node2_hitl", _route_after_hitl2, {
        "node2_generate": "node2_generate",
        "node3_subgraph": "node3_subgraph",
    })

    builder.add_conditional_edges("node3_subgraph", _route_after_node3, {
        "node1_refine": "node1_refine",
        "node2_generate": "node2_generate",
        "node4_summary": "node4_summary",
    })

    builder.add_edge("node4_summary", END)

    return builder.compile(checkpointer=MemorySaver())


# ============================================================
# HITL 节点 — interrupt 暂停点
# ============================================================

def _node1_hitl(state: PipelineState) -> dict:
    """节点1完成后的人工确认。"""
    if state.get("mode") == "experiment":
        ns = dict(state.get("node_status", {}))
        ns["node1"] = "approved"
        return {"node_status": ns}

    req = state.get("req", {})
    decision = interrupt({
        "node": "node1",
        "type": "hitl_confirm",
        "message": "节点1完成 — 请确认结构化需求",
        "data": {
            "component_type": req.get("component_type"),
            "parameters": req.get("parameters"),
            "topology": req.get("topology"),
            "constraints": req.get("constraints"),
            "clarification_rounds": req.get("clarification_rounds"),
        },
    })

    # decision 是 Command(resume=...) 传入的值
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
    """节点2完成后的人工确认（用户需 Eclipse 看图）。"""
    if state.get("mode") == "experiment":
        ns = dict(state.get("node_status", {}))
        ns["node2"] = "approved"
        return {"node_status": ns}

    sysml = state.get("sysml", {})
    decision = interrupt({
        "node": "node2",
        "type": "hitl_confirm",
        "message": "节点2完成 — 请用 Eclipse 查看 SysML 图后确认",
        "data": {
            "file_path": sysml.get("file_path"),
            "attempts": sysml.get("attempts"),
            "errors": sysml.get("errors"),
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


# ============================================================
# 路由函数
# ============================================================

def _route_after_hitl1(state: PipelineState) -> str:
    """节点1 HITL 后路由：确认→node2，打回未超限→node1，超限强制→node2。"""
    ns = state.get("node_status", {})
    if ns.get("node1") == "approved":
        return "node2_generate"

    rejects = state.get("reject_count_per_node", {}).get("node1", 0)
    max_rj = state.get("max_rejects", 3)
    if rejects >= max_rj:
        logger.warning("节点1 打回 %s 次已达上限，强制继续", rejects)
        return "node2_generate"

    return "node1_refine"


def _route_after_hitl2(state: PipelineState) -> str:
    """节点2 HITL 后路由。"""
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
    """节点3 出口路由：成功→node4，失败按原因回溯。"""
    mo = state.get("mo", {})
    if mo.get("success"):
        return "node4_summary"

    # 分析失败原因
    errors = mo.get("errors", [])
    error_text = " ".join(errors[-3:]).lower() if errors else ""

    # 缺少激励源/参数 → 需求信息不足，回节点1
    if any(kw in error_text for kw in ["parameter", "not found", "undeclared", "missing", "未定义"]):
        logger.info("节点3 失败原因分析: 需求参数不足 → 回到节点1")
        return "node1_refine"

    # 拓扑/连接错误 → SysML 可能有问题，回节点2
    if any(kw in error_text for kw in ["connect", "port", "type mismatch", "equation", "连接"]):
        logger.info("节点3 失败原因分析: SysML 拓扑问题 → 回到节点2")
        return "node2_generate"

    # 其他错误 → 带着失败记录去节点4
    logger.info("节点3 失败原因不明，继续到节点4生成总结")
    return "node4_summary"
