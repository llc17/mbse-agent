# -*- coding: utf-8 -*-
"""
=============================================================================
pipeline.py — V4 LangGraph 主图编排
=============================================================================

这是整个系统的"大脑"——定义流水线的拓扑结构和 HITL 交互逻辑。

V4 图拓扑（★ 标记 V4 改动）:

  START
    ↓
  node1_refine            — LLM 需求精炼（V3 prompt 含矛盾自检）
    ↓
  node1_hitl              — 人工确认/打回（打回 → node1_refine）
    ↓ (通过)
  node2_generate          — LLM 生成 SysML v2（V4 prompt 对齐官方写法）
    ↓
  Q_cross_validate        — V3: LLM 交叉校验 req vs SysML
    ↓ (通过)                ↓ (失败 → node2_generate)
  node2_hitl              — 人工确认/打回
    ↓ (通过)
  node3_subgraph          — Modelica 生成+编译+仿真+修复（4节点子图）
    ↓ (仿真成功)            ↓ (失败 → 回溯 node1/node2/node4)
  ★ node3_hitl            — V4 新增: 展示仿真曲线，人工确认/打回
    ↓ (通过)                ↓ (打回 Modelica → node3_subgraph repair)
    ↓                       ↓ (打回 SysML → node2_generate)
  Q_physics_validate      — V3: CSV 物理量验证（V4 配置驱动）
    ↓ (通过)                ↓ (失败 → node3_subgraph repair)
  node4_summary           — LLM 生成总结报告
    ↓
  END

V4 新增 State 字段:
  - expected_physics: Optional[dict] — 物理验证配置（实验模式从 test_case 传入）
=============================================================================
"""

import logging
from typing import TypedDict, Optional, Any

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt  # HITL 暂停点

from src.node1_requirement import node1_refine
from src.node2_sysml import node2_generate
from src.node3_modelica import build_node3_subgraph
from src.node4_summary import node4_summary
from src.node_quality import q_cross_validate, q_physics_validate

logger = logging.getLogger("pipeline")


# ==========================================================================
# PipelineState — 贯通全图的状态对象
# ==========================================================================
# TypedDict vs Pydantic:
#   LangGraph 的 checkpoint 序列化对 TypedDict 支持更好。
#   每个节点返回 dict（而非完整 State），LangGraph 自动 merge 增量。
#   total=False = 所有字段都是 Optional。

class PipelineState(TypedDict, total=False):
    """全图共享状态。每个节点读自己需要的字段，写自己产出的字段。"""

    # ── 核心数据管道 ──
    raw_input: str                # 用户原始自然语言输入（入口）
    req: Optional[dict]           # StructuredRequirement 的 dict（节点1产出）
    sysml: Optional[dict]         # SysMLArtifact 的 dict（节点2产出）
    mo: Optional[dict]            # ModelicaArtifact 的 dict（节点3产出）
    summary: Optional[dict]       # SummaryArtifact 的 dict（节点4产出）

    # ── HITL（人机交互）控制 ──
    node_status: dict[str, str]   # 每个节点的状态: "pending"/"approved"/"rejected"
    human_feedback: str           # 用户打回时的反馈文本
    reject_count_per_node: dict[str, int]  # 每个节点被打回了几次（防无限循环）
    mode: str                     # "interactive"（暂停问用户）| "experiment"（自动确认）

    # ── LLM 参数 ──
    temperature: float            # LLM 温度（0=确定, 1=创意）
    max_retries: int              # node3 子图最大修复次数
    max_rejects: int              # HITL 最大打回次数
    dialogue_history: list[dict]  # 对话历史（interactive 模式用）

    # ── 运行管理 ──
    timing: dict[str, float]      # 每个节点的耗时（秒）
    run_dir: str                  # 本次运行的输出目录路径

    # ── V3 新增: 质量基础设施 ──
    quality_checks: dict          # 交叉校验 + 物理验证的结果
    repair_log: list[dict]        # node3 每次 LLM 修复的记录
    physics_feedback: str         # 物理验证失败时打回 node3 的反馈

    # ── V4 新增: 物理验证配置 ──
    expected_physics: Optional[dict]  # 来自 test_case 的物理验证期望值配置


# ==========================================================================
# build_pipeline() — 构建主图
# ==========================================================================

def build_pipeline() -> StateGraph:
    """
    构建 MBSE 主流水线状态图，注册所有节点和连线。

    返回编译好的 StateGraph（带 MemorySaver checkpoint，支持 HITL 中断恢复）。
    """
    builder = StateGraph(PipelineState)

    # ── 注册节点: 8 个主节点 + 1 个子图 = 共 12 个处理单元 ──
    builder.add_node("node1_refine", node1_refine)
    builder.add_node("node1_hitl", _node1_hitl)
    builder.add_node("node2_generate", node2_generate)
    builder.add_node("Q_cross_validate", q_cross_validate)   # V3: 交叉校验
    builder.add_node("node2_hitl", _node2_hitl)
    builder.add_node("node3_subgraph", build_node3_subgraph())  # 子图（4节点）
    builder.add_node("node3_hitl", _node3_hitl)              # V4: 仿真确认
    builder.add_node("Q_physics_validate", q_physics_validate)  # V3: 物理验证
    builder.add_node("node4_summary", node4_summary)

    # ── 连线 ──

    # 阶段 1: 需求解析 + HITL
    builder.add_edge(START, "node1_refine")
    builder.add_edge("node1_refine", "node1_hitl")
    builder.add_conditional_edges("node1_hitl", _route_after_hitl1, {
        "node1_refine": "node1_refine",     # 用户打回 → 重做需求
        "node2_generate": "node2_generate", # 确认 → 生成 SysML
    })

    # 阶段 2: SysML 生成 + 交叉校验 + HITL
    builder.add_edge("node2_generate", "Q_cross_validate")
    builder.add_conditional_edges("Q_cross_validate", _route_after_cross_validate, {
        "node2_hitl": "node2_hitl",          # 校验通过 → 人工确认
        "node2_generate": "node2_generate",  # 校验失败 → LLM 重生成
    })
    builder.add_conditional_edges("node2_hitl", _route_after_hitl2, {
        "node2_generate": "node2_generate",  # 打回 → 重生成
        "node3_subgraph": "node3_subgraph",  # 确认 → 仿真
    })

    # 阶段 3: V4 仿真 + 仿真HITL + 物理验证
    builder.add_conditional_edges("node3_subgraph", _route_after_node3, {
        "node3_hitl": "node3_hitl",          # 仿真成功 → V4 人工确认
        "node1_refine": "node1_refine",      # 失败(参数) → 重做需求
        "node2_generate": "node2_generate",  # 失败(拓扑) → 重做 SysML
        "node4_summary": "node4_summary",   # 失败(不明) → 总结
    })
    builder.add_conditional_edges("node3_hitl", _route_after_node3_hitl, {
        "Q_physics_validate": "Q_physics_validate",  # 确认 → 物理验证
        "node3_subgraph": "node3_subgraph",          # 打回 Modelica → 修复
        "node2_generate": "node2_generate",          # 打回 SysML → 重做
    })
    builder.add_conditional_edges("Q_physics_validate", _route_after_physics, {
        "node4_summary": "node4_summary",    # 通过 → 总结
        "node3_subgraph": "node3_subgraph",  # 失败 → 修复
    })

    # 阶段 4: 总结
    builder.add_edge("node4_summary", END)

    # MemorySaver: 提供 checkpoint 能力，支持 interrupt 后恢复
    return builder.compile(checkpointer=MemorySaver())


# ==========================================================================
# HITL 节点 — interrupt() 暂停点
# ==========================================================================
# interrupt() 是 LangGraph 的"暂停执行，等待外部输入"机制。
# 在 experimental 模式下跳过所有 HITL（无人值守）。

def _node1_hitl(state: PipelineState) -> dict:
    """节点 1 HITL: 需求确认。展示 LLM 解析出的结构化需求参数。"""
    if state.get("mode") == "experiment":
        # 实验模式: 自动确认，不等待用户
        ns = dict(state.get("node_status", {}))
        ns["node1"] = "approved"
        return {"node_status": ns}

    req = state.get("req", {})
    # interrupt() 暂停，等待外部调用 Command(resume=decision)
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

    # 用户选了 "打回" → 计数 + 记录反馈
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
    """节点 2 HITL: SysML 确认。提示用户用 Eclipse 查看 .sysml 的图形化模型。"""
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
    """
    V4 新增: 节点 3 HITL — 仿真完成后人工确认。

    和 node1/node2 不同:
      - 此处展示的是仿真曲线 PNG 路径 + 关键数值摘要
      - 用户有三个选项: 确认 / 打回 Modelica 重做 / 打回 SysML 重做
      - 打回 SysML 是 V4 新增逻辑: 用户认为问题不在 Modelica 代码，
        而在更上游的 SysML 模型设计（如拓扑连接错误）
    """
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
            "plot_path": mo.get("plot_path"),     # PNG 曲线图路径
            "csv_path": mo.get("csv_path"),       # CSV 数据路径
            "errors": mo.get("errors", [])[-3:],  # 最近 3 个错误（如果有）
            "repair_count": len(repair_log),
            # 物理验证预检（如果已经跑了物理验证—通常是上一个 HITL 循环的结果）
            "quality_preview": {
                "physics_passed": quality_checks.get("physics_validate", {}).get("passed"),
                "physics_deviation": quality_checks.get("physics_validate", {}).get("deviation_percent"),
            },
        },
    })

    if isinstance(decision, dict):
        action = decision.get("action", "approve")
        if action == "reject":
            # 打回 Modelica 重做（reject 保持兼容 V3 的命名）
            rejects = dict(state.get("reject_count_per_node", {}))
            rejects["node3"] = rejects.get("node3", 0) + 1
            logger.info("节点3 HITL: 用户打回 Modelica 重做 (第%s次)", rejects["node3"])
            return {
                "node_status": {**state.get("node_status", {}), "node3": "rejected"},
                "human_feedback": decision.get("feedback", ""),
                "reject_count_per_node": rejects,
                "node3_reject_target": "modelica",    # V4: 告诉路由去哪修复
            }
        elif action == "reject_sysml":
            # V4 新增: 打回 SysML 重做
            rejects = dict(state.get("reject_count_per_node", {}))
            rejects["node3"] = rejects.get("node3", 0) + 1
            logger.info("节点3 HITL: 用户打回 SysML 重做 (第%s次)", rejects["node3"])
            return {
                "node_status": {**state.get("node_status", {}), "node3": "rejected"},
                "human_feedback": decision.get("feedback", ""),
                "reject_count_per_node": rejects,
                "node3_reject_target": "sysml",       # V4: 告诉路由回 node2
            }

    logger.info("节点3 HITL: 用户确认")
    return {"node_status": {**state.get("node_status", {}), "node3": "approved"}}


# ==========================================================================
# 路由函数 — 决定流程下一步走向
# ==========================================================================
# 每个路由函数检查 state 中的条件，返回下一个节点名（str）。
# 路由名 → 节点名 的映射在 builder.add_conditional_edges() 中定义。

def _route_after_hitl1(state: PipelineState) -> str:
    """节点1 HITL 后: 确认→node2，打回+未超限→node1，超限→强制 node2。"""
    ns = state.get("node_status", {})
    if ns.get("node1") == "approved":
        return "node2_generate"

    rejects = state.get("reject_count_per_node", {}).get("node1", 0)
    max_rj = state.get("max_rejects", 3)
    if rejects >= max_rj:
        logger.warning("节点1 打回 %s 次已达上限，强制继续", rejects)
        return "node2_generate"           # 超限: 即使不满意也往前走

    return "node1_refine"                  # 回 node1 重做需求


def _route_after_cross_validate(state: PipelineState) -> str:
    """交叉校验后: 通过→HITL，失败+未超限→node2重生成，超限→强制进HITL。"""
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

    logger.info("交叉校验失败: %s → 打回 node2", cross.get("issues", []))
    return "node2_generate"


def _route_after_hitl2(state: PipelineState) -> str:
    """节点2 HITL 后: 确认→node3，打回+未超限→node2，超限→强制 node3。"""
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
    V4: node3 子图完成后路由。
    仿真成功 → node3_hitl（V4 人工确认）
    仿真失败 → 根据错误信息推断根本原因，回溯到对应上游节点
    """
    mo = state.get("mo", {})
    if mo.get("success"):
        # V4: 成功 → 先给用户看曲线，不直接进物理验证
        return "node3_hitl"

    # 失败原因分类（根据错误日志关键词）
    errors = mo.get("errors", [])
    error_text = " ".join(errors[-3:]).lower() if errors else ""

    # 参数/未定义变量 → 需求信息不足，回 node1
    if any(kw in error_text for kw in ["parameter", "not found", "undeclared", "missing", "未定义"]):
        logger.info("节点3 失败原因分析: 需求参数不足 → 回到节点1")
        return "node1_refine"

    # 连接/端口/类型不匹配 → SysML 模型有问题，回 node2
    if any(kw in error_text for kw in ["connect", "port", "type mismatch", "equation", "连接"]):
        logger.info("节点3 失败原因分析: SysML 拓扑问题 → 回到节点2")
        return "node2_generate"

    logger.info("节点3 失败原因不明，继续到节点4生成总结")
    return "node4_summary"


def _route_after_node3_hitl(state: PipelineState) -> str:
    """
    V4 新增: node3 HITL 后路由。

    三种返回路径:
      - 确认 → Q_physics_validate（物理验证）
      - 打回 Modelica (reject) → node3_subgraph（进修复循环）
      - 打回 SysML (reject_sysml) → node2_generate（重做 SysML 模型）
    """
    ns = state.get("node_status", {})
    if ns.get("node3") == "approved":
        return "Q_physics_validate"

    rejects = state.get("reject_count_per_node", {}).get("node3", 0)
    max_rj = state.get("max_rejects", 3)
    target = state.get("node3_reject_target", "modelica")  # V4: 默认打回 Modelica

    if rejects >= max_rj:
        logger.warning("节点3 HITL 打回 %s 次已达上限，强制继续到物理验证", rejects)
        return "Q_physics_validate"

    if target == "sysml":
        logger.info("节点3 HITL: 打回 SysML 重做")
        return "node2_generate"
    else:
        logger.info("节点3 HITL: 打回 Modelica 重做")
        return "node3_subgraph"  # 回子图 repair → compile → simulate


def _route_after_physics(state: PipelineState) -> str:
    """物理验证后: 通过→node4总结, 失败+未超限→node3修复, 超限→强制node4。"""
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
