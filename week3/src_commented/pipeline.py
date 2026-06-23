# -*- coding: utf-8 -*-
"""
=============================================================================
pipeline.py — LangGraph 主状态图（V2 核心编排）
=============================================================================

这是整个 V2 的"大脑"——它定义了一张有向图（节点 + 箭头），
LangGraph 负责按图执行节点、传递状态、处理中断。

══════════════════════════════════════════════════════════════
如果只能理解一个文件，看这个。
══════════════════════════════════════════════════════════════

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
核心概念 1: StateGraph（状态图）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  一张图 = 一堆节点 + 一堆箭头。

  节点（Node）= 一个 Python 函数。输入是 state，输出是要更新的字段。
  箭头（Edge）= 控制流。"从 A 走到 B"。
  条件边（Conditional Edge）= "从 A 出发，根据 state 决定走 B 还是 C"。

  所有节点共享同一个 state 字典（PipelineState）。
  节点 A 修改了 state["req"]，节点 B 立刻就能读到。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
核心概念 2: HITL（Human-in-the-Loop）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  LangGraph 的 interrupt() 函数可以暂停图的执行。
  暂停后，用户可以做任何事（去 Eclipse 看图、喝咖啡...），
  然后用 Command(resume=decision) 恢复执行。

  本文件中有 2 个 HITL 节点:
    node1_hitl → 节点1完成后暂停，用户确认/打回需求
    node2_hitl → 节点2完成后暂停，用户 Eclipse 看图后确认/打回

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
核心概念 3: 子图（Subgraph）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  节点3 不是一个普通函数，而是一个"图中图"。
  父图把子图当成一个黑盒节点，子图内部有自己的节点和路由。
  这样节点3的 self-repair 循环不会污染主图的逻辑。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
核心概念 4: Checkpoint（检查点）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  MemorySaver 是一个内存中的 checkpointer。
  每次图执行一个节点后，自动保存 state 快照。
  作用：中断恢复、时间旅行调试。

══════════════════════════════════════════════════════════════
图的结构（一眼看全）:

  START
    │
    ▼
  [node1_refine]       ← LLM 多轮对话精炼需求
    │
    ▼
  [node1_hitl]         ← ⏸️ 暂停：用户确认/打回
    │
    ├── 确认 → [node2_generate]   ← LLM 生成 SysML v2 代码
    └── 打回 → [node1_refine]     ← 回到节点1重做
    │
    ▼
  [node2_hitl]         ← ⏸️ 暂停：Eclipse 看图后确认/打回
    │
    ├── 确认 → [node3_subgraph]   ← Modelica 子图（自修复循环）
    └── 打回 → [node2_generate]   ← 回到节点2重做
    │
    ▼
  [node3_subgraph 出口]
    │
    ├── 成功 → [node4_summary]    ← LLM 生成总结
    ├── 失败+需求问题 → [node1_refine]
    └── 失败+SysML问题 → [node2_generate]
    │
    ▼
  [node4_summary]      ← LLM 生成 summary.md
    │
    ▼
  END
══════════════════════════════════════════════════════════════
"""

# ====================================================================
# 导入
# ====================================================================

import logging
from typing import TypedDict, Optional, Any

from langgraph.graph import StateGraph, START, END          # 图构建
from langgraph.checkpoint.memory import MemorySaver          # 内存检查点
from langgraph.types import interrupt                        # HITL 暂停
# interrupt(value) 会暂停图执行，把 value 返回给调用方
# 调用方用 Command(resume=decision) 恢复，decision 成为 interrupt() 的返回值

from src.node1_requirement import node1_refine              # 节点1 函数
from src.node2_sysml import node2_generate                  # 节点2 函数
from src.node3_modelica import build_node3_subgraph         # 节点3 子图构建函数
from src.node4_summary import node4_summary                 # 节点4 函数

logger = logging.getLogger("pipeline")


# ====================================================================
# PipelineState — 贯通全图的状态类型
# ====================================================================
class PipelineState(TypedDict, total=False):
    """
    定义图中流转的状态对象。

    TypedDict 是 Python 的类型提示（不是真正的 dict 子类），
    用来告诉 IDE 和类型检查器"这个字典应该有哪些键"。
    total=False 表示所有字段都是可选的。

    字段说明:
      raw_input              — 用户原始输入文本
      req                    — 节点1产出的 StructuredRequirement（存为字典）
      sysml                  — 节点2产出的 SysMLArtifact（存为字典）
      mo                     — 节点3产出的 ModelicaArtifact（存为字典）
      summary                — 节点4产出的 SummaryArtifact（存为字典）
      node_status            — 各节点状态: {"node1": "approved", ...}
      human_feedback         — 用户打回时输入的反馈文字
      reject_count_per_node  — 每个节点被打了多少次: {"node1": 2, ...}
      temperature            — LLM 温度参数（全局统一）
      max_retries            — 节点3 最大自修复次数（默认 5）
      max_rejects            — 用户最多可以打回几次（默认 3）
      dialogue_history       — 节点1 的多轮对话历史
      timing                 — 各节点耗时: {"node1": 1.2, ...}
      run_dir                — 输出目录路径
      mode                   — "interactive" | "experiment"

    为什么存字典而不是 Pydantic 对象:
      LangGraph 的 checkpoint 要求 state 中的所有值都 JSON 可序列化。
      Pydantic 对象不行，dict 可以。
      所以在节点中: 读时 dict → Pydantic（用 ** 解包）
                    写时 Pydantic → dict（用 .model_dump()）
    """
    raw_input: str
    req: Optional[dict]                                    # Optional[X] = X | None
    sysml: Optional[dict]
    mo: Optional[dict]
    summary: Optional[dict]
    node_status: dict[str, str]
    human_feedback: str
    reject_count_per_node: dict[str, int]
    temperature: float
    max_retries: int
    max_rejects: int
    dialogue_history: list[dict]
    timing: dict[str, float]
    run_dir: str
    mode: str


# ====================================================================
# build_pipeline() — 构建主图
# ====================================================================
def build_pipeline() -> StateGraph:
    """
    构建 MBSE 主流水线的 LangGraph 状态图。

    返回:
      编译好的图（带 MemorySaver checkpoint）

    用法:
      graph = build_pipeline()
      state = graph.invoke(initial_state, config)
    """
    # Step 1: 创建图构建器，指定状态类型
    builder = StateGraph(PipelineState)
    # PipelineState 作为泛型参数传入，给 TypeScript 那样的类型检查

    # Step 2: 注册所有节点
    builder.add_node("node1_refine", node1_refine)         # 需求精炼
    builder.add_node("node1_hitl", _node1_hitl)            # 需求确认暂停点
    builder.add_node("node2_generate", node2_generate)     # SysML 生成
    builder.add_node("node2_hitl", _node2_hitl)            # SysML 确认暂停点
    builder.add_node("node3_subgraph", build_node3_subgraph())
    # ↑ 关键：build_node3_subgraph() 返回一个编译好的子图
    #   父图把它当作普通节点对待——不关心内部结构
    builder.add_node("node4_summary", node4_summary)       # 总结生成

    # Step 3: 连线（定义节点间的箭头）

    # 固定边：START → node1 → node1_hitl（不需要判断，直接走）
    builder.add_edge(START, "node1_refine")                # 入口 → 节点1
    builder.add_edge("node1_refine", "node1_hitl")        # 节点1 → 暂停确认

    # 条件边：node1_hitl 之后，根据用户决策走不同路
    builder.add_conditional_edges(
        "node1_hitl",                                      # 源节点
        _route_after_hitl1,                                # 路由函数（返回 "node1_refine" 或 "node2_generate"）
        {
            "node1_refine": "node1_refine",                #   打回 → 回到节点1重做
            "node2_generate": "node2_generate",            #   确认 → 去节点2
        }
    )

    builder.add_edge("node2_generate", "node2_hitl")      # 节点2 → 暂停确认

    builder.add_conditional_edges(
        "node2_hitl",
        _route_after_hitl2,
        {
            "node2_generate": "node2_generate",            #   打回 → 重做节点2
            "node3_subgraph": "node3_subgraph",            #   确认 → 去节点3子图
        }
    )

    # 节点3 子图出口的条件路由（最复杂的一个）
    builder.add_conditional_edges(
        "node3_subgraph",
        _route_after_node3,                                # 根据仿真结果决定
        {
            "node1_refine": "node1_refine",                #   失败原因=需求问题 → 回到节点1
            "node2_generate": "node2_generate",            #   失败原因=SysML问题 → 回到节点2
            "node4_summary": "node4_summary",              #   成功 → 去总结
        }
    )

    builder.add_edge("node4_summary", END)                # 节点4 → 完成

    # Step 4: 编译图（加 checkpoint）
    return builder.compile(checkpointer=MemorySaver())
    # MemorySaver(): 把 checkpoint 存在内存中
    # 如果需要持久化（程序重启后仍可恢复），可以换 SqliteSaver


# ============================================================
# HITL 节点 — interrupt 暂停点
# ============================================================

def _node1_hitl(state: PipelineState) -> dict:
    """
    节点1完成后的暂停确认。

    工作流程:
      1. 如果是实验模式 → 自动确认（无人值守）
      2. 如果是交互模式 → interrupt({
           "node": "node1",
           "data": {需求详情},
         })
      3. 用户选确认 → 返回 {"action": "approve"}
      4. 用户选打回 → 返回 {"action": "reject", "feedback": "..."}
      5. main.py 中的 handle_interrupt() 处理用户的输入
      6. 用户的选择通过 Command(resume=decision) 传回来
      7. interrupt() 的返回值就是 decision
    """
    # ---- 实验模式：跳过人工确认 ----
    if state.get("mode") == "experiment":
        ns = dict(state.get("node_status", {}))            # 复制 node_status
        ns["node1"] = "approved"                           # 直接标记批准
        return {"node_status": ns}                         # 只返回要更新的字段

    # ---- 交互模式：暂停 ----
    req = state.get("req", {})
    decision = interrupt({                                 # ← 这里暂停！
        # interrupt() 在首次调用时抛出 GraphInterrupt 异常
        # 调用方（main.py）捕获这个异常，拿到 value，展示给用户
        # 用户选择后，调用方用 graph.invoke(Command(resume=decision))
        # 图从这一行恢复执行，interrupt() 返回 decision
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

    # ---- 处理用户决策 ----
    # decision 的格式: {"action": "approve"} 或 {"action": "reject", "feedback": "..."}
    if isinstance(decision, dict) and decision.get("action") == "reject":
        # 用户打回
        rejects = dict(state.get("reject_count_per_node", {}))
        rejects["node1"] = rejects.get("node1", 0) + 1    # 打回次数 +1
        logger.info("节点1 HITL: 用户打回 (第%s次)", rejects["node1"])
        return {
            "node_status": {**state.get("node_status", {}), "node1": "rejected"},
            "human_feedback": decision.get("feedback", ""),
            "reject_count_per_node": rejects,
        }

    # 用户确认
    logger.info("节点1 HITL: 用户确认")
    return {
        "node_status": {**state.get("node_status", {}), "node1": "approved"}
    }


def _node2_hitl(state: PipelineState) -> dict:
    """
    节点2完成后的暂停确认。

    和 node1_hitl 逻辑一样，只是展示的数据不同：
      - 显示 SysML 文件路径
      - 提示用户去 Eclipse 看图
    """
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
# 路由函数 — 决定图中"下一步该走哪"
# ============================================================
# 每个路由函数接收 state → 返回一个字符串（目标节点名）
# LangGraph 根据返回值去条件边映射表中查对应的真实节点

def _route_after_hitl1(state: PipelineState) -> str:
    """
    节点1 HITL 后路由。

    逻辑:
      - 用户确认 → "node2_generate"（继续往下走）
      - 用户打回 + 未超限 → "node1_refine"（回去重做）
      - 用户打回 + 超限 → "node2_generate"（强制继续）

    为什么有"强制继续"：
      防止用户无限打回（死循环）。打回超过 max_rejects 次就强行往下走，
      给用户最后的反馈记录在 state 里但不再回头。
    """
    ns = state.get("node_status", {})
    if ns.get("node1") == "approved":
        return "node2_generate"

    rejects = state.get("reject_count_per_node", {}).get("node1", 0)
    max_rj = state.get("max_rejects", 3)
    if rejects >= max_rj:
        logger.warning("节点1 打回 %s 次已达上限，强制继续", rejects)
        return "node2_generate"                            # 强制继续

    return "node1_refine"                                  # 回去重做


def _route_after_hitl2(state: PipelineState) -> str:
    """节点2 HITL 后路由，逻辑同上。"""
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
    节点3 子图出口路由。决定仿真结束后去哪个节点。

    这是 V2 新增的关键能力——失败回溯。

    逻辑:
      - 仿真成功 → "node4_summary"（正常去总结）
      - 失败 + 错误日志包含"parameter/not found/undeclared/missing/未定义"
        → 可能是需求信息不完整 → 回到 node1_refine
      - 失败 + 错误日志包含"connect/port/type mismatch/equation/连接"
        → 可能是 SysML 拓扑有误 → 回到 node2_generate
      - 其他失败 → "node4_summary"
        （例如 5 次自修复耗尽，未知原因，带着失败记录去写总结）

    为什么这样分类：
      仿真错误有很多种。编译错误"variable R not found"可能是需求漏了参数。
      端口连接错误"connect: incompatible types"可能是 SysML 拓扑错了。
      通过关键词猜测病根，自动路由回正确的上游节点。
    """
    mo = state.get("mo", {})
    if mo.get("success"):
        return "node4_summary"

    # ---- 分析失败原因 ----
    errors = mo.get("errors", [])
    # 把最近 3 条错误信息拼成一个字符串
    error_text = " ".join(errors[-3:]).lower() if errors else ""

    # 关键词匹配（用 Python 的 any() + generator）
    if any(kw in error_text for kw in [
        "parameter", "not found", "undeclared", "missing", "未定义"
    ]):
        # 这些关键词暗示"LLM 用了一个没定义的变量/参数"
        # → 可能是需求阶段漏了参数 → 回到节点1 补充需求
        logger.info("节点3 失败原因分析: 需求参数不足 → 回到节点1")
        return "node1_refine"

    if any(kw in error_text for kw in [
        "connect", "port", "type mismatch", "equation", "连接"
    ]):
        # 这些关键词暗示"组件之间的连接关系有问题"
        # → 可能是 SysML 模型拓扑错了 → 回到节点2 修改 SysML
        logger.info("节点3 失败原因分析: SysML 拓扑问题 → 回到节点2")
        return "node2_generate"

    # 兜底：不清楚为什么失败，带着失败记录去节点4
    logger.info("节点3 失败原因不明，继续到节点4生成总结")
    return "node4_summary"
