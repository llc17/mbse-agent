"""
LangGraph 状态图 — V4 核心编排。

主图结构:
  START → node1_refine → node1_hitl → node2_generate → Q_cross_validate
       → node2_hitl → node3_subgraph → node3_hitl(🆕) → Q_physics_validate → node4_summary → END

V4 新增：
  - H1: sysmlpy 标准语法检查（打分制：fatal/error/warning）
  - H3: prompt 升级对齐官方 SysML 写法（private import ScalarValues::*）
  - D: 多用例 3→6（+RLC / 双房间热 / 运放）
  - E: 物理验证配置驱动（expected_physics）
  - F: 节点3 HITL（仿真曲线确认/打回）

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
    # V3 新增
    quality_checks: dict                 # {"cross_validate": {...}, "physics_validate": {...}}
    repair_log: list[dict]               # node3 每次修复的记录
    physics_feedback: str                # 物理验证打回 node3 的反馈
    # V4 新增
    expected_physics: Optional[dict]     # 物理验证配置（实验模式），interactive 模式为 None 自动检测


def build_pipeline() -> StateGraph:
    """构建 MBSE 主流水线状态图。"""
    builder = StateGraph(PipelineState)

    # 注册节点
    builder.add_node("node1_refine", node1_refine)
    builder.add_node("node1_hitl", _node1_hitl)
    builder.add_node("node2_generate", node2_generate)
    builder.add_node("Q_cross_validate", q_cross_validate)    # V3: 交叉校验
    builder.add_node("node2_hitl", _node2_hitl)
    builder.add_node("node3_subgraph", build_node3_subgraph())
    builder.add_node("node3_hitl", _node3_hitl)               # V4: 仿真确认
    builder.add_node("Q_physics_validate", q_physics_validate)  # V3: 物理验证
    builder.add_node("node4_summary", node4_summary)

    # 连线
    builder.add_edge(START, "node1_refine")
    builder.add_edge("node1_refine", "node1_hitl")

    builder.add_conditional_edges("node1_hitl", _route_after_hitl1, {
        "node1_refine": "node1_refine",
        "node2_generate": "node2_generate",
    })

    # V3: node2 → 交叉校验 → HITL2（失败则打回 node2）
    builder.add_edge("node2_generate", "Q_cross_validate")
    builder.add_conditional_edges("Q_cross_validate", _route_after_cross_validate, {
        "node2_hitl": "node2_hitl",
        "node2_generate": "node2_generate",
    })

    builder.add_conditional_edges("node2_hitl", _route_after_hitl2, {
        "node2_generate": "node2_generate",
        "node3_subgraph": "node3_subgraph",
    })

    # V4: node3 成功后 → node3_hitl（用户确认曲线），失败 → 按原因回溯
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


def _node3_hitl(state: PipelineState) -> dict:
    """V4: 节点3仿真完成后的HITL——展示关键数值摘要，用户确认/打回。"""
    if state.get("mode") == "experiment":
        ns = dict(state.get("node_status", {}))
        ns["node3"] = "approved"
        return {"node_status": ns}

    mo = state.get("mo", {})
    quality_checks = state.get("quality_checks", {})
    repair_log = state.get("repair_log", [])

    decision = interrupt({
        "node": "node3",
        "type": "hitl_confirm",
        "message": "节点3仿真完成 — 请确认仿真结果",
        "data": {
            "success": mo.get("success"),
            "attempts": mo.get("attempts"),
            "plot_path": mo.get("plot_path"),
            "csv_path": mo.get("csv_path"),
            "errors": mo.get("errors", [])[-3:],  # 最近 3 个错误
            "repair_count": len(repair_log),
            "quality_preview": {
                "physics_passed": quality_checks.get("physics_validate", {}).get("passed"),
                "physics_deviation": quality_checks.get("physics_validate", {}).get("deviation_percent"),
            },
        },
    })

    if isinstance(decision, dict):
        action = decision.get("action", "approve")
        if action == "reject":
            # 打回 Modelica 重做
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
            # 打回 SysML 重做（问题在拓扑/参数层面）
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


def _route_after_cross_validate(state: PipelineState) -> str:
    """V3: 交叉校验后路由。通过→HITL2确认，失败→打回node2重生成。"""
    qc = state.get("quality_checks", {})
    cross = qc.get("cross_validate", {})
    if cross.get("passed"):
        logger.info("交叉校验通过 → node2_hitl")
        return "node2_hitl"

    # 打回 node2，交叉校验问题作为 feedback
    rejects = state.get("reject_count_per_node", {}).get("node2", 0)
    max_rj = state.get("max_rejects", 3)
    if rejects >= max_rj:
        logger.warning("交叉校验打回 %s 次已达上限，强制继续到 HITL", rejects)
        return "node2_hitl"

    logger.info("交叉校验失败: %s → 打回 node2", cross.get("issues", []))
    return "node2_generate"


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
    """V4: 仿真成功→node3_hitl（人工确认），失败→按原因回溯。"""
    mo = state.get("mo", {})
    if mo.get("success"):
        return "node3_hitl"

    # 仿真失败 → 分析原因
    errors = mo.get("errors", [])
    error_text = " ".join(errors[-3:]).lower() if errors else ""

    if any(kw in error_text for kw in ["parameter", "not found", "undeclared", "missing", "未定义"]):
        logger.info("节点3 失败原因分析: 需求参数不足 → 回到节点1")
        return "node1_refine"

    if any(kw in error_text for kw in ["connect", "port", "type mismatch", "equation", "连接"]):
        logger.info("节点3 失败原因分析: SysML 拓扑问题 → 回到节点2")
        return "node2_generate"

    logger.info("节点3 失败原因不明，继续到节点4生成总结")
    return "node4_summary"


def _route_after_node3_hitl(state: PipelineState) -> str:
    """V4: node3 HITL 后路由。确认→物理验证，打回Modelica→node3修复，打回SysML→node2重做。"""
    ns = state.get("node_status", {})
    if ns.get("node3") == "approved":
        return "Q_physics_validate"

    # 打回处理
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
    """V3: 物理验证后路由。通过→node4，失败→打回node3 repair。"""
    qc = state.get("quality_checks", {})
    physics = qc.get("physics_validate", {})

    if physics.get("passed"):
        logger.info("物理验证通过 → node4_summary")
        return "node4_summary"

    # 物理验证失败 → 打回 node3 repair
    mo = state.get("mo", {})
    attempts = mo.get("attempts", 0)
    max_retries = state.get("max_retries", 5)
    if attempts >= max_retries:
        logger.warning("node3 重试 %s/%s 已达上限，强制到 node4", attempts, max_retries)
        return "node4_summary"

    logger.info("物理验证失败 (偏差 %.1f%%) → 打回 node3 repair",
                physics.get("deviation_percent", 0))
    return "node3_subgraph"
