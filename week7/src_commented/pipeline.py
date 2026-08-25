"""
=============================================================================
pipeline.py — LangGraph 状态图编排（V6 5-Agent 版本）
=============================================================================
定位: 整个系统的"大脑"——定义状态对象、节点注册、连线、HITL 暂停点、路由逻辑。

V4 → V6 变更（图拓扑不变，路由逻辑升级）:
  - 各节点内部升级为三阶段 Agent（生成→审查→修正），节点外接口不变
  - _route_after_node3: 不再用关键词匹配，改为读取 node3 子图输出的 root_cause
  - 根因分析由 node3 repair 中的 LLM 调用完成，pipeline 只读结论（关注点分离）
  - 新增断路器字段到 state: circuit_breaker_triggered + breaker_details

主图结构（继承 V4，不变）:
  START → node1_refine → node1_hitl → node2_generate → Q_cross_validate
       → node2_hitl → node3_subgraph(子图: generate→compile→sim→repair)
       → node3_hitl → Q_physics_validate → node4_summary → END

关键设计:
  - StateGraph + MemorySaver: LangGraph 内置状态管理 + 内存检查点
  - interrupt(): experiment 模式自动跳过 HITL，interactive 模式暂停等待
  - conditional_edges: 根据质量检查结果和人工确认动态路由
=============================================================================
"""

import logging
from typing import TypedDict, Optional, Any

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt

# ── 导入各 Agent 节点的入口函数 ──
from src.node1_requirement import node1_refine      # Agent ① 需求分析
from src.node2_sysml import node2_generate          # Agent ② SysML 生成
from src.node3_modelica import build_node3_subgraph # Agent ③ Modelica 子图
from src.node4_summary import node4_summary          # Agent ⑤ 总结
from src.node_quality import q_cross_validate, q_physics_validate  # Agent ④ 质量检查

logger = logging.getLogger("pipeline")


# ============================================================================
# 第 1 层: 状态对象定义（贯穿全图的"共享内存"）
# ============================================================================

class PipelineState(TypedDict, total=False):
    """
    贯通全图的状态对象。每个节点读入 state 的某些字段，写回另一些字段。
    TypedDict + total=False = 所有字段可选，节点只需写回自己负责的字段。

    V6 新增:
      circuit_breaker_triggered: 断路器是否触发
      breaker_details: 断路器触发时的详情
    """
    # ── 核心数据流 ──
    raw_input: str                           # 用户原始输入（自然语言）
    req: Optional[dict]                      # node1 产出 → 结构化需求
    sysml: Optional[dict]                    # node2 产出 → SysML v2 代码
    mo: Optional[dict]                       # node3 产出 → Modelica 仿真结果
    summary: Optional[dict]                  # node4 产出 → 总结报告

    # ── 流程控制 ──
    node_status: dict[str, str]              # 各节点状态: "pending" | "approved" | "rejected"
    human_feedback: str                      # 用户打回时的反馈文本
    reject_count_per_node: dict[str, int]    # 各节点累计打回次数（断路器用）
    temperature: float                       # LLM temperature 参数
    max_retries: int                         # node3 最大重试次数（默认 5）
    max_rejects: int                         # HITL 最大打回次数（默认 3）
    dialogue_history: list[dict]             # 多轮对话历史（interactive 模式）
    timing: dict[str, float]                 # 各阶段耗时统计
    run_dir: str                             # 输出目录路径
    mode: str                                # "interactive" | "experiment"

    # ── V3/V4 保留 ──
    quality_checks: dict                     # 质量检查结果（cross_validate + physics_validate）
    repair_log: list[dict]                   # node3 修复日志
    physics_feedback: str                    # 物理验证失败时打回 node3 的反馈
    expected_physics: Optional[dict]         # 物理验证配置（实验模式）

    # ── V6 新增 ──
    circuit_breaker_triggered: bool          # 断路器是否触发（三阶段审查超限）
    breaker_details: str                     # 断路器触发详情（给 HITL 展示）


# ============================================================================
# 第 2 层: 构建流水线图
# ============================================================================

def build_pipeline() -> StateGraph:
    """
    构建 V6 MBSE 主流水线状态图。

    返回的 StateGraph 实例是编译好的（已调用 .compile()），
    可直接用 .invoke(initial_state, config) 执行。

    图结构:
      START
        → node1_refine          (Agent ①: 需求分析)
        → node1_hitl            (人工确认)
        → node2_generate        (Agent ②: SysML 生成)
        → Q_cross_validate      (Agent ④: req↔SysML 交叉校验)
        → node2_hitl            (人工确认)
        → node3_subgraph        (Agent ③: Modelica 子图)
        → node3_hitl            (人工确认)
        → Q_physics_validate    (Agent ④: 物理验证 + SysML↔Modelica)
        → node4_summary         (Agent ⑤: 总结)
        → END
    """
    builder = StateGraph(PipelineState)

    # ── 注册节点 ──
    builder.add_node("node1_refine", node1_refine)
    builder.add_node("node1_hitl", _node1_hitl)
    builder.add_node("node2_generate", node2_generate)
    builder.add_node("Q_cross_validate", q_cross_validate)
    builder.add_node("node2_hitl", _node2_hitl)
    builder.add_node("node3_subgraph", build_node3_subgraph())  # 子图（嵌套状态机）
    builder.add_node("node3_hitl", _node3_hitl)
    builder.add_node("Q_physics_validate", q_physics_validate)
    builder.add_node("node4_summary", node4_summary)

    # ── 连线 ──
    builder.add_edge(START, "node1_refine")
    builder.add_edge("node1_refine", "node1_hitl")

    # node1 确认后 → 继续 / 打回重做
    builder.add_conditional_edges("node1_hitl", _route_after_hitl1, {
        "node1_refine": "node1_refine",
        "node2_generate": "node2_generate",
    })

    # node2 产出 → 交叉校验 → 通过→HITL / 失败→打回 node2
    builder.add_edge("node2_generate", "Q_cross_validate")
    builder.add_conditional_edges("Q_cross_validate", _route_after_cross_validate, {
        "node2_hitl": "node2_hitl",
        "node2_generate": "node2_generate",
    })

    # node2 确认后 → 继续 / 打回重做
    builder.add_conditional_edges("node2_hitl", _route_after_hitl2, {
        "node2_generate": "node2_generate",
        "node3_subgraph": "node3_subgraph",
    })

    # V6: node3 子图完成后 → 根据根因分析路由（LLM 替代关键词）
    builder.add_conditional_edges("node3_subgraph", _route_after_node3, {
        "node3_hitl": "node3_hitl",           # 仿真成功 → 人工确认
        "node1_refine": "node1_refine",       # 缺参数 → 打回 node1
        "node2_generate": "node2_generate",   # SysML 拓扑错 → 打回 node2
        "node4_summary": "node4_summary",     # 彻底失败 → 写报告
    })

    # node3 确认后 → 物理验证 / 打回重做
    builder.add_conditional_edges("node3_hitl", _route_after_node3_hitl, {
        "Q_physics_validate": "Q_physics_validate",
        "node3_subgraph": "node3_subgraph",
        "node2_generate": "node2_generate",
    })

    # 物理验证后 → 写总结 / 打回 node3 repair
    builder.add_conditional_edges("Q_physics_validate", _route_after_physics, {
        "node4_summary": "node4_summary",
        "node3_subgraph": "node3_subgraph",
    })

    builder.add_edge("node4_summary", END)

    # MemorySaver: 基于内存的检查点，支持 interrupt + resume
    return builder.compile(checkpointer=MemorySaver())


# ============================================================================
# 第 3 层: HITL 节点（人工确认暂停点）
# ============================================================================
# experiment 模式: 自动 approve → 全自动跑完
# interactive 模式: interrupt() 暂停 → 等待用户输入 → resume 继续

def _node1_hitl(state: PipelineState) -> dict:
    """Agent ① 完成后的人工确认"""
    if state.get("mode") == "experiment":
        # 实验模式 → 自动通过
        ns = dict(state.get("node_status", {}))
        ns["node1"] = "approved"
        return {"node_status": ns}

    # 交互模式 → 暂停，等待用户确认
    req = state.get("req", {})
    decision = interrupt({
        "node": "node1", "type": "hitl_confirm",
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
    """Agent ② 完成后的人工确认（用户需 Eclipse 看图确认）"""
    if state.get("mode") == "experiment":
        ns = dict(state.get("node_status", {}))
        ns["node2"] = "approved"
        return {"node_status": ns}

    sysml = state.get("sysml", {})
    cross = state.get("quality_checks", {}).get("cross_validate", {})

    decision = interrupt({
        "node": "node2", "type": "hitl_confirm",
        "message": "Agent ② SysML 建模完成 — 请用 Eclipse 查看 SysML 图后确认",
        "data": {
            "file_path": sysml.get("file_path"), "attempts": sysml.get("attempts"),
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
    """Agent ③ 仿真完成后的 HITL — V6 展示根因分析 + 断路器信息"""
    if state.get("mode") == "experiment":
        ns = dict(state.get("node_status", {}))
        ns["node3"] = "approved"
        return {"node_status": ns}

    mo = state.get("mo", {})
    quality_checks = state.get("quality_checks", {})
    repair_log = state.get("repair_log", [])

    decision = interrupt({
        "node": "node3", "type": "hitl_confirm",
        "message": "Agent ③ Modelica 仿真完成 — 请确认仿真结果",
        "data": {
            "success": mo.get("success"), "attempts": mo.get("attempts"),
            "plot_path": mo.get("plot_path"), "csv_path": mo.get("csv_path"),
            "errors": mo.get("errors", [])[-3:], "repair_count": len(repair_log),
            "root_cause": mo.get("root_cause"),
            "root_cause_detail": mo.get("root_cause_detail", ""),
            "breaker_triggered": state.get("circuit_breaker_triggered", False),
            "breaker_details": state.get("breaker_details", ""),
            "quality_preview": {
                "physics_passed": quality_checks.get("physics_validate", {}).get("passed"),
                "physics_deviation": quality_checks.get("physics_validate", {}).get("deviation_percent"),
                "sysml_modelica_passed": quality_checks.get("sysml_modelica", {}).get("passed"),
            },
        },
    })

    if isinstance(decision, dict):
        action = decision.get("action", "approve")
        if action == "reject":              # 打回 Modelica 重做
            rejects = dict(state.get("reject_count_per_node", {}))
            rejects["node3"] = rejects.get("node3", 0) + 1
            return {
                "node_status": {**state.get("node_status", {}), "node3": "rejected"},
                "human_feedback": decision.get("feedback", ""),
                "reject_count_per_node": rejects, "node3_reject_target": "modelica",
            }
        elif action == "reject_sysml":      # 打回 SysML 重做（问题在拓扑/参数层面）
            rejects = dict(state.get("reject_count_per_node", {}))
            rejects["node3"] = rejects.get("node3", 0) + 1
            return {
                "node_status": {**state.get("node_status", {}), "node3": "rejected"},
                "human_feedback": decision.get("feedback", ""),
                "reject_count_per_node": rejects, "node3_reject_target": "sysml",
            }

    logger.info("节点3 HITL: 用户确认")
    return {"node_status": {**state.get("node_status", {}), "node3": "approved"}}


# ============================================================================
# 第 4 层: 路由函数（决定状态图下一步走哪个节点）
# ============================================================================

def _route_after_hitl1(state: PipelineState) -> str:
    """节点1 HITL 后: 确认→node2 / 打回未超限→node1 / 超限强制→node2"""
    ns = state.get("node_status", {})
    if ns.get("node1") == "approved":
        return "node2_generate"
    rejects = state.get("reject_count_per_node", {}).get("node1", 0)
    if rejects >= state.get("max_rejects", 3):
        logger.warning("节点1 打回 %s 次已达上限，强制继续", rejects)
        return "node2_generate"
    return "node1_refine"


def _route_after_cross_validate(state: PipelineState) -> str:
    """交叉校验后: 通过→HITL2 / 失败→打回 node2（超限则强制到 HITL）"""
    cross = state.get("quality_checks", {}).get("cross_validate", {})
    if cross.get("passed"):
        logger.info("交叉校验通过 → node2_hitl")
        return "node2_hitl"
    rejects = state.get("reject_count_per_node", {}).get("node2", 0)
    if rejects >= state.get("max_rejects", 3):
        logger.warning("交叉校验打回 %s 次已达上限，强制继续", rejects)
        return "node2_hitl"
    logger.info("交叉校验失败 → 打回 node2")
    return "node2_generate"


def _route_after_hitl2(state: PipelineState) -> str:
    """节点2 HITL 后: 确认→node3 / 打回未超限→node2 / 超限强制→node3"""
    ns = state.get("node_status", {})
    if ns.get("node2") == "approved":
        return "node3_subgraph"
    rejects = state.get("reject_count_per_node", {}).get("node2", 0)
    if rejects >= state.get("max_rejects", 3):
        logger.warning("节点2 打回 %s 次已达上限，强制继续", rejects)
        return "node3_subgraph"
    return "node2_generate"


def _route_after_node3(state: PipelineState) -> str:
    """
    V6 核心路由: 使用 LLM 根因分析结果替代 V4 关键词匹配。

    路由优先级:
      1. 仿真成功 → node3_hitl
      2. root_cause=="missing_parameters" → node1_refine（V6 LLM 根因分析）
      3. root_cause=="sysml_topology"     → node2_generate（V6 LLM 根因分析）
      4. 重试耗尽 → node4_summary（强制结束，写报告）
      5. Fallback: V4 关键词匹配（LLM 根因分析不可用时的兜底）
    """
    mo = state.get("mo", {})
    if mo.get("success"):
        return "node3_hitl"

    root_cause = mo.get("root_cause", "")
    attempts = mo.get("attempts", 0)
    max_retries = state.get("max_retries", 5)

    # 断路器: 重试耗尽 → 强制到 node4
    if attempts >= max_retries:
        logger.warning("节点3 重试耗尽 (%s/%s)，强制到 node4", attempts, max_retries)
        return "node4_summary"

    # V6: LLM 根因分析路由 —— 精准打回
    if root_cause == "missing_parameters":
        logger.info("V6根因路由: 缺参数 → 打回 node1_refine")
        return "node1_refine"
    if root_cause == "sysml_topology":
        logger.info("V6根因路由: SysML拓扑 → 打回 node2_generate")
        return "node2_generate"

    # Fallback: V4 关键词匹配（作为安全网）
    error_text = " ".join(mo.get("errors", [])[-3:]).lower()
    if any(kw in error_text for kw in ["parameter", "not found", "undeclared", "missing"]):
        return "node1_refine"
    if any(kw in error_text for kw in ["connect", "port", "type mismatch", "equation"]):
        return "node2_generate"

    logger.info("节点3 失败原因不明，继续到 node4")
    return "node4_summary"


def _route_after_node3_hitl(state: PipelineState) -> str:
    """node3 HITL 后: 确认→物理验证 / 打回 Modelica→node3 / 打回 SysML→node2"""
    ns = state.get("node_status", {})
    if ns.get("node3") == "approved":
        return "Q_physics_validate"
    rejects = state.get("reject_count_per_node", {}).get("node3", 0)
    if rejects >= state.get("max_rejects", 3):
        return "Q_physics_validate"          # 超限强制继续
    target = state.get("node3_reject_target", "modelica")
    return "node2_generate" if target == "sysml" else "node3_subgraph"


def _route_after_physics(state: PipelineState) -> str:
    """物理验证后: 通过→node4 / 失败→打回 node3 repair"""
    physics = state.get("quality_checks", {}).get("physics_validate", {})
    if physics.get("passed"):
        logger.info("物理验证通过 → node4_summary")
        return "node4_summary"
    attempts = state.get("mo", {}).get("attempts", 0)
    if attempts >= state.get("max_retries", 5):
        logger.warning("node3 重试 %s/%s 已达上限，强制到 node4", attempts, 5)
        return "node4_summary"
    logger.info("物理验证失败 (偏差 %.1f%%) → 打回 node3 repair",
                physics.get("deviation_percent", 0))
    return "node3_subgraph"
