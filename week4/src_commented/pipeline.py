# -*- coding: utf-8 -*-
"""
=============================================================================
pipeline.py — V3 LangGraph 状态图（主图编排）
=============================================================================

这是整个系统的"中央调度器"。它定义：
  1. 有哪些节点（7 个主节点 + node3 的 4 个子节点 = 11 个处理单元）
  2. 节点之间如何连线（7 条固定边 + 4 个条件路由）
  3. 全局状态 PipelineState 有哪些字段

V3 图拓扑（相比 V2 的改动用 ★ 标记）：

  START
    ↓
  node1_refine          — LLM 需求精炼（V3 prompt 含矛盾自检）
    ↓
  node1_hitl            — 人工确认/打回
    ↓ (通过)
  node2_generate        — LLM 生成 SysML v2
    ↓
  ★ Q_cross_validate   — 【V3 新增】LLM 交叉校验 req vs SysML
    ↓ (通过)               ↓ (失败 → 打回 node2_generate)
  node2_hitl            — 人工确认/打回
    ↓ (通过)
  node3_subgraph         — Modelica 生成+编译+仿真+修复子图
    ↓ (仿真成功)          ↓ (失败 → 回溯 node1/node2)
  ★ Q_physics_validate  — 【V3 新增】CSV 物理量验证
    ↓ (通过)               ↓ (失败 → 打回 node3_subgraph repair)
  node4_summary          — 生成总结报告
    ↓
  END

V3 新增 State 字段：
  - quality_checks: dict — 存交叉校验和物理验证的结果
  - repair_log: list[dict] — node3 每次修复的记录
  - physics_feedback: str — 物理验证失败时打回 node3 的反馈
"""

# ====================================================================
# 导入
# ====================================================================
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


# ====================================================================
# PipelineState — 贯通全图的状态对象
# ====================================================================
# 使用 TypedDict 而非 Pydantic 的原因：
#   LangGraph 的 checkpoint 序列化对 TypedDict 支持更好。
#   每个节点返回 dict（而非完整 State），LangGraph 自动 merge。
#   所以 total=False（所有字段都是 Optional）。

class PipelineState(TypedDict, total=False):
    """
    全图共享的状态。每个节点读自己需要的字段，写自己产出的字段。

    V2 字段（保留）：
    """
    # ---- 核心数据 ----
    raw_input: str                    # 用户原始自然语言输入
    req: Optional[dict]               # StructuredRequirement 的 dict
    sysml: Optional[dict]             # SysMLArtifact 的 dict
    mo: Optional[dict]                # ModelicaArtifact 的 dict
    summary: Optional[dict]           # SummaryArtifact 的 dict

    # ---- HITL 控制 ----
    node_status: dict[str, str]       # {"node1":"approved","node2":"pending",...}
    human_feedback: str               # 用户打回时的反馈文本
    reject_count_per_node: dict[str, int]  # {"node1":2,...} 各节点被打回次数
    max_rejects: int                  # 打回上限（默认 3）

    # ---- LLM 参数 ----
    temperature: float                # LLM 温度（0.0~1.0）
    max_retries: int                  # node3 最大修复次数（默认 5）

    # ---- 辅助 ----
    dialogue_history: list[dict]      # node1 交互模式的对话记录
    timing: dict[str, float]          # 各节点耗时 {"node1":12.3, "node2":5.1,...}
    run_dir: str                      # 本次运行的输出目录
    mode: str                         # "interactive" | "experiment"

    # ════════════════════════════════════════════════════
    # ── V3 新增字段 ──
    # ════════════════════════════════════════════════════
    quality_checks: dict              # {"cross_validate":{...},"physics_validate":{...}}
    repair_log: list[dict]            # node3 每次修复记录 [RepairLogEntry,...]
    physics_feedback: str             # 物理验证失败打回 node3 的反馈文本


# ====================================================================
# build_pipeline() — 组装主图
# ====================================================================

def build_pipeline() -> StateGraph:
    """构建 MBSE V3 主流水线状态图。"""
    builder = StateGraph(PipelineState)

    # ---- 注册所有节点 ----
    builder.add_node("node1_refine", node1_refine)                  # LLM 需求解析
    builder.add_node("node1_hitl", _node1_hitl)                     # 人工确认（需求）
    builder.add_node("node2_generate", node2_generate)              # LLM SysML 生成
    builder.add_node("Q_cross_validate", q_cross_validate)          # ★ V3: 交叉校验
    builder.add_node("node2_hitl", _node2_hitl)                     # 人工确认（SysML）
    builder.add_node("node3_subgraph", build_node3_subgraph())      # Modelica 子图
    builder.add_node("Q_physics_validate", q_physics_validate)      # ★ V3: 物理验证
    builder.add_node("node4_summary", node4_summary)                # 总结报告

    # ---- 连线 ----
    # 固定边（无条件）
    builder.add_edge(START, "node1_refine")
    builder.add_edge("node1_refine", "node1_hitl")

    # node1 HITL 条件路由：通过→node2, 打回→node1
    builder.add_conditional_edges("node1_hitl", _route_after_hitl1, {
        "node1_refine": "node1_refine",
        "node2_generate": "node2_generate",
    })

    # ★ V3: node2 → 交叉校验 → HITL2
    # 交叉校验失败 → 打回 node2 带差异描述
    builder.add_edge("node2_generate", "Q_cross_validate")
    builder.add_conditional_edges("Q_cross_validate", _route_after_cross_validate, {
        "node2_hitl": "node2_hitl",
        "node2_generate": "node2_generate",
    })

    # node2 HITL 条件路由
    builder.add_conditional_edges("node2_hitl", _route_after_hitl2, {
        "node2_generate": "node2_generate",
        "node3_subgraph": "node3_subgraph",
    })

    # ★ V3: node3 成功后 → 物理验证（而非直接 node4）
    builder.add_conditional_edges("node3_subgraph", _route_after_node3, {
        "Q_physics_validate": "Q_physics_validate",       # 成功 → 物理闸
        "node1_refine": "node1_refine",                   # 参数不足 → node1
        "node2_generate": "node2_generate",               # 拓扑错误 → node2
        "node4_summary": "node4_summary",                 # 不明原因 → 出报告
    })

    # ★ V3: 物理验证 → node4（通过）或 node3（失败）
    builder.add_conditional_edges("Q_physics_validate", _route_after_physics, {
        "node4_summary": "node4_summary",
        "node3_subgraph": "node3_subgraph",
    })

    builder.add_edge("node4_summary", END)

    return builder.compile(checkpointer=MemorySaver())


# ====================================================================
# HITL 节点 — 人工确认/打回
# ====================================================================

def _node1_hitl(state: PipelineState) -> dict:
    """节点1完成后的人工确认。实验模式下自动放行。"""
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
    """节点2完成后的人工确认（用户需 Eclipse 看图）。实验模式下自动放行。"""
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


# ====================================================================
# 路由函数 — 每个条件边的决策逻辑
# ====================================================================

def _route_after_hitl1(state: PipelineState) -> str:
    """node1 HITL 后：通过→node2 / 打回未超限→node1 / 超限强制→node2"""
    ns = state.get("node_status", {})
    if ns.get("node1") == "approved":
        return "node2_generate"

    rejects = state.get("reject_count_per_node", {}).get("node1", 0)
    max_rj = state.get("max_rejects", 3)
    if rejects >= max_rj:
        logger.warning("节点1 打回 %s 次已达上限，强制继续", rejects)
        return "node2_generate"
    return "node1_refine"


# ── V3 新增路由 ──

def _route_after_cross_validate(state: PipelineState) -> str:
    """
    ★ V3: 交叉校验后路由。
    通过 → node2_hitl（人确认）
    失败 → node2_generate（打回重生成，节点详情作为 feedback）
    """
    qc = state.get("quality_checks", {})
    cross = qc.get("cross_validate", {})
    if cross.get("passed"):
        logger.info("交叉校验通过 → node2_hitl")
        return "node2_hitl"

    # 打回 node2，交叉校验的差异描述作为 human_feedback
    rejects = state.get("reject_count_per_node", {}).get("node2", 0)
    max_rj = state.get("max_rejects", 3)
    if rejects >= max_rj:
        logger.warning("交叉校验打回 %s 次已达上限，强制继续到 HITL", rejects)
        return "node2_hitl"

    logger.info("交叉校验失败: %s → 打回 node2", cross.get("issues", []))
    return "node2_generate"


def _route_after_hitl2(state: PipelineState) -> str:
    """node2 HITL 后路由"""
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
    """
    ★ V3: node3 出口路由改版。
    仿真成功 → Q_physics_validate（而非直接 node4）
    仿真失败 → 按错误关键词回溯 node1/node2/node4
    """
    mo = state.get("mo", {})
    if mo.get("success"):
        return "Q_physics_validate"                                  # ★ 先过物理闸

    # 仿真失败 → 分析原因
    errors = mo.get("errors", [])
    error_text = " ".join(errors[-3:]).lower() if errors else ""

    if any(kw in error_text for kw in ["parameter", "not found", "undeclared", "missing", "未定义"]):
        logger.info("节点3 失败原因: 需求参数不足 → 回到节点1")
        return "node1_refine"

    if any(kw in error_text for kw in ["connect", "port", "type mismatch", "equation", "连接"]):
        logger.info("节点3 失败原因: SysML 拓扑问题 → 回到节点2")
        return "node2_generate"

    logger.info("节点3 失败原因不明，继续到节点4生成总结")
    return "node4_summary"


# ── V3 新增路由 ──

def _route_after_physics(state: PipelineState) -> str:
    """
    ★ V3: 物理验证后路由。
    通过 → node4 出报告
    失败 → node3_subgraph repair（带 physics_feedback）
    """
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
