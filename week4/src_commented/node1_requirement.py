# -*- coding: utf-8 -*-
"""
=============================================================================
node1_requirement.py — 节点 1：需求解析（V3 版）
=============================================================================

这个节点的职责：把用户的自然语言变成 StructuredRequirement 对象。

两种运行模式：
  1. interactive（交互）：多轮对话，LLM 反问用户补充缺失信息
  2. experiment（实验）：单次 LLM 调用直接出结果，缺失用默认值填充

V3 改动（相比 V2）：
  - prompt 中加入矛盾自检提示：
    "生成前检查：参数之间是否存在矛盾（如 R 和 C 的乘积与截止频率不一致）？"
    这不是一次独立的 LLM 调用，而是嵌入在生成 prompt 中的一句话。
    目的是让 LLM 在生成需求时多一步自我审视——如用户同时指定了 R、C、fc，
    但 RC 乘积与 fc 公式不匹配，LLM 应该以用户明确指定的值推导其他参数。
"""

# ====================================================================
# 导入
# ====================================================================
import json
import time
import logging

from src.llm_client import chat, user_msg, assistant_msg, system_msg
from src.schemas import StructuredRequirement
from src.utils import load_prompt, extract_json, format_history

logger = logging.getLogger("node1")


# ====================================================================
# LangGraph 节点入口 — 被 pipeline.py 调用
# ====================================================================

def node1_refine(state: dict) -> dict:
    """
    LangGraph 节点函数：需求精炼。

    输入:
        state.raw_input: 用户原始自然语言输入
        state.mode: "interactive" | "experiment"
        state.human_feedback: 如果是打回重做，包含反馈内容
        state.dialogue_history: 多轮对话历史
        state.temperature: LLM 温度参数

    输出（写回 state）:
        req: StructuredRequirement 的 dict 形式
        dialogue_history: 更新后的对话历史
        timing.node1: 本节点耗时（秒）
    """
    t0 = time.time()
    mode = state.get("mode", "interactive")
    raw_input = state.get("raw_input", "")
    history = list(state.get("dialogue_history", []))
    temperature = state.get("temperature", 0.3)

    if mode == "experiment":
        # ---- 实验模式：单次调用，不反问 ----
        req = _refine_single_pass(raw_input, temperature)
        req.clarification_rounds = 0
        history = [user_msg(raw_input)]
    else:
        # ---- 交互模式：多轮对话 ----
        feedback = state.get("human_feedback", "")
        if feedback and history:
            # 被打回重做：在历史中追加反馈
            history.append(user_msg(f"上次需求被驳回，反馈: {feedback}"))
        elif not history:
            # 首次进入：初始化对话历史
            history = [user_msg(raw_input)]
        req = _refine_interactive(history, temperature)

    elapsed = time.time() - t0
    logger.info("节点1 完成 (%.1fs), 类型=%s, 参数=%s", elapsed, req.component_type, req.parameters)

    return {
        "req": req.model_dump(),
        "dialogue_history": history,
        "timing": {**state.get("timing", {}), "node1": elapsed},
    }


# ====================================================================
# 交互模式 — 多轮对话精炼
# ====================================================================
# 流程：
#   1. 检查信息完整性（node1_completeness.txt prompt）
#   2. 如果完整 → 直接生成 StructuredRequirement
#   3. 如果不完整 → 生成反问 → 等用户回答 → 回到步骤 1
#   最多 max_rounds=10 轮，超限强制生成
# ====================================================================

def _refine_interactive(history: list[dict], temperature: float, max_rounds: int = 10) -> StructuredRequirement:
    """多轮对话精炼模式"""

    for round_num in range(1, max_rounds + 1):
        # ---- 步骤 1：检查完整性 ----
        completeness_prompt = (
            load_prompt("node1_completeness.txt")
            .replace("{dialogue_history}", format_history(history))
        )
        result = chat([user_msg(completeness_prompt)], temperature=0.1, max_tokens=512)
        try:
            completeness = json.loads(extract_json(result))
        except json.JSONDecodeError:
            completeness = {"is_complete": False, "missing_fields": ["JSON解析失败"], "suggestions": []}

        # ---- 步骤 2：完整 → 直接生成 ----
        if completeness.get("is_complete"):
            final_prompt = (
                "根据以下对话内容，提取系统需求的结构化信息。\n\n"
                f"对话历史：\n{format_history(history)}\n\n"
                "返回 JSON Schema：\n"
                f"{json.dumps(StructuredRequirement.model_json_schema(), ensure_ascii=False, indent=2)}"
            )
            final_result = chat([user_msg(final_prompt)], temperature=0.2, max_tokens=2048)
            req = StructuredRequirement.model_validate_json(extract_json(final_result))
            req.raw_input = history[0].get("content", "")
            req.clarification_rounds = round_num - 1
            return req

        # ---- 步骤 3：不完整 → 生成反问 ----
        missing_str = "\n".join(f"- {m}" for m in completeness.get("missing_fields", []))
        question = (
            f"根据当前信息，还缺少: {missing_str}。"
            "请用中文友好地向用户提问，一次只问1-2个最重要的问题，给出具体选项。"
        )
        clarify_msg = chat([
            system_msg(
                f"你是系统需求分析师。对话历史:\n{format_history(history)}\n\n{question}"
            ),
        ], temperature=0.5, max_tokens=256).strip()

        # ---- 步骤 4：展示反问，等用户输入 ----
        print(f"\n[节点1] 第{round_num}轮: {clarify_msg}")
        user_answer = input("\n你的回答: ").strip()
        if not user_answer:
            user_answer = "不需要补充，用已有信息即可。"

        history.append(assistant_msg(clarify_msg))
        history.append(user_msg(user_answer))

    # ---- 达到 max_rounds → 强制提取 ----
    final_prompt = (
        "根据对话提取结构化需求。\n"
        f"对话历史：\n{format_history(history)}\n\n"
        f"JSON Schema:\n{json.dumps(StructuredRequirement.model_json_schema(), ensure_ascii=False, indent=2)}"
    )
    final_result = chat([user_msg(final_prompt)], temperature=0.2, max_tokens=2048)
    try:
        req = StructuredRequirement.model_validate_json(extract_json(final_result))
    except Exception:
        req = StructuredRequirement(component_type="未知", raw_input=history[0].get("content", ""))
    req.raw_input = history[0].get("content", "")
    req.clarification_rounds = max_rounds
    return req


# ====================================================================
# 实验模式 — 单次 LLM 调用
# ====================================================================
# V3 核心改动就在这个函数里：
#   prompt 中加了一句矛盾自检——
#   "【V3 自检】生成前检查：参数之间是否存在矛盾（如 R 和 C 的乘积
#    与截止频率不一致）？如有矛盾，以用户明确指定的值为准推导其他参数。"
#
# 为什么不做独立 LLM 调用？
#   1. V2 实验 60 次中，需求矛盾导致的失败接近 0（矛盾检测的实际价值低）
#   2. 一次额外 API 调用 ≈ +3~5s 延迟，对成功率几乎无贡献
#   3. 嵌入 prompt 的自我审视，LLM 有能力在生成时一并处理
# ====================================================================

def _refine_single_pass(raw_input: str, temperature: float) -> StructuredRequirement:
    """
    实验模式：单次 LLM 调用直接出 StructuredRequirement。

    V3 改动：prompt 中加入矛盾自检提示。
    """
    prompt = (
        "根据以下用户需求，直接生成结构化需求 JSON。不需要反问，缺失信息用合理默认值填充。\n\n"
        f"用户需求: {raw_input}\n\n"
        # ── V3 自检提示 ──
        "【V3 自检】生成前检查：参数之间是否存在矛盾（如 R 和 C 的乘积与截止频率不一致）？"
        "如有矛盾，以用户明确指定的值为准推导其他参数。\n\n"
        # ── Schema ──
        f"JSON Schema:\n{json.dumps(StructuredRequirement.model_json_schema(), ensure_ascii=False, indent=2)}"
    )
    result = chat([user_msg(prompt)], temperature=temperature, max_tokens=2048)
    try:
        req = StructuredRequirement.model_validate_json(extract_json(result))
    except Exception:
        req = StructuredRequirement(component_type="未知", raw_input=raw_input)
    req.raw_input = raw_input
    return req
