# -*- coding: utf-8 -*-
"""
=============================================================================
node1_requirement.py — 节点 1：需求解析
=============================================================================

流水线的第一站。用户输入自然语言（如"做一个 1kHz RC 滤波器"），
节点 1 把它转成机器可读的 StructuredRequirement。

两种运行模式:
  1. interactive — 多轮对话精炼（LLM 反问 → 用户回答 → 再问 → 确认）
  2. experiment — 单次 LLM 调用直接出结果（批量实验模式，无人值守）

LangGraph 节点函数: node1_refine(state) → {req, dialogue_history, timing}
=============================================================================
"""

import json
import time
import logging

# 从其他模块导入依赖
from src.llm_client import chat, user_msg, assistant_msg, system_msg
from src.schemas import StructuredRequirement
from src.utils import load_prompt, extract_json, format_history

logger = logging.getLogger("node1")


# ==========================================================================
# LangGraph 节点入口
# ==========================================================================

def node1_refine(state: dict) -> dict:
    """
    LangGraph 节点函数。Pipeline 通过 state 传入上下文，本函数读 state、
    调用 LLM 精炼需求、写回结果。

    V3: prompt 内嵌矛盾自检提示（"R×C 乘积是否与截止频率一致？"）

    流程:
      1. 读 state["mode"] 决定 interactive/experiment
      2. experiment → _refine_single_pass()（单次 LLM，不用反问）
      3. interactive → _refine_interactive()（多轮对话）
      4. 把结果写入 state["req"] 返回，LangGraph 自动合并
    """
    t0 = time.time()                        # 开始计时（用于 timing 统计）
    mode = state.get("mode", "interactive")  # 默认 interactive
    raw_input = state.get("raw_input", "")   # 用户原始输入
    history = list(state.get("dialogue_history", []))  # 对话历史（拷贝，避免修改原值）
    temperature = state.get("temperature", 0.3)

    if mode == "experiment":
        # ── 实验模式: 不回问，一次生成 ──
        req = _refine_single_pass(raw_input, temperature)
        req.clarification_rounds = 0
        history = [user_msg(raw_input)]
    else:
        # ── 交互模式: 多轮对话 ──
        feedback = state.get("human_feedback", "")
        if feedback and history:
            # 用户上次打回了，把反馈追加到对话历史中
            history.append(user_msg(f"上次需求被驳回，反馈: {feedback}"))
        elif not history:
            # 第一次进入，初始化对话历史
            history = [user_msg(raw_input)]
        req = _refine_interactive(history, temperature)

    elapsed = time.time() - t0
    logger.info("节点1 完成 (%.1fs), 类型=%s, 参数=%s", elapsed, req.component_type, req.parameters)

    # 返回 dict → LangGraph 自动 merge 到全局 state
    return {
        "req": req.model_dump(),            # Pydantic → dict（JSON 可序列化）
        "dialogue_history": history,
        "timing": {**state.get("timing", {}), "node1": elapsed},
    }


# ==========================================================================
# 交互模式: 多轮对话精炼
# ==========================================================================

def _refine_interactive(history: list[dict], temperature: float, max_rounds: int = 10) -> StructuredRequirement:
    """
    多轮对话需求精炼（interactive 模式专用）。

    循环逻辑:
      1. 让 LLM 检查"信息是否足够生成 SysML 模型"（completeness check）
      2. 如果足够 → 直接提取 StructuredRequirement
      3. 如果不够 → LLM 生成反问 → 用户回答 → 回到步骤 1
      4. 最多 max_rounds 轮，超限则强制提取

    这种"反问→补全"的设计让你不需要一次性给出所有参数，
    系统会主动问你还缺什么。
    """
    prompts_dir = None  # 使用默认目录

    for round_num in range(1, max_rounds + 1):
        # ── 步骤 1: 检查完整性 ──
        # 用 node1_completeness.txt 作为 prompt，让 LLM 判断还缺什么信息
        completeness_prompt = (
            load_prompt("node1_completeness.txt", prompts_dir)
            .replace("{dialogue_history}", format_history(history))
        )
        # temperature=0.1: 完整性判断需要确定性，不需要创造性
        result = chat([user_msg(completeness_prompt)], temperature=0.1, max_tokens=512)
        try:
            completeness = json.loads(extract_json(result))
        except json.JSONDecodeError:
            # LLM 输出的不是有效 JSON → 当作"不完整"处理，继续反问
            completeness = {"is_complete": False, "missing_fields": ["JSON解析失败"], "suggestions": []}

        # ── 步骤 2: 如果完整 → 直接提取需求 ──
        if completeness.get("is_complete"):
            final_prompt = (
                "根据以下对话内容，提取系统需求的结构化信息。\n\n"
                f"对话历史：\n{format_history(history)}\n\n"
                "返回 JSON Schema：\n"
                f"{json.dumps(StructuredRequirement.model_json_schema(), ensure_ascii=False, indent=2)}"
            )
            # temperature=0.2: 提取结构化数据，稍低温度提高准确性
            final_result = chat([user_msg(final_prompt)], temperature=0.2, max_tokens=2048)
            req = StructuredRequirement.model_validate_json(extract_json(final_result))
            req.raw_input = history[0].get("content", "")   # 保留原始输入
            req.clarification_rounds = round_num - 1         # 记录反问了几个回合
            return req

        # ── 步骤 3: 不完整 → LLM 生成反问 → 等待用户回答 ──
        missing_str = "\n".join(f"- {m}" for m in completeness.get("missing_fields", []))
        question = (
            f"根据当前信息，还缺少: {missing_str}。"
            "请用中文友好地向用户提问，一次只问1-2个最重要的问题，给出具体选项。"
        )
        # temperature=0.5: 问问题需要一些创造性
        clarify_msg = chat([
            system_msg(
                f"你是系统需求分析师。对话历史:\n{format_history(history)}\n\n{question}"
            ),
        ], temperature=0.5, max_tokens=256).strip()

        # 打印反问并等待用户输入
        print(f"\n[节点1] 第{round_num}轮: {clarify_msg}")
        user_answer = input("\n你的回答: ").strip()
        if not user_answer:
            user_answer = "不需要补充，用已有信息即可。"

        # 追加到对话历史中
        history.append(assistant_msg(clarify_msg))   # LLM 的反问
        history.append(user_msg(user_answer))         # 用户的回答

    # ── 达到 max_rounds: 强制提取 ──
    # 不完美但至少有个结果，不会让流水线卡住
    final_prompt = (
        "根据对话提取结构化需求。\n"
        f"对话历史：\n{format_history(history)}\n\n"
        f"JSON Schema:\n{json.dumps(StructuredRequirement.model_json_schema(), ensure_ascii=False, indent=2)}"
    )
    final_result = chat([user_msg(final_prompt)], temperature=0.2, max_tokens=2048)
    try:
        req = StructuredRequirement.model_validate_json(extract_json(final_result))
    except Exception:
        # 兜底：即使 LLM 输出坏了，也返回一个带错误标记的对象
        req = StructuredRequirement(component_type="未知", raw_input=history[0].get("content", ""))
    req.raw_input = history[0].get("content", "")
    req.clarification_rounds = max_rounds
    return req


# ==========================================================================
# 实验模式: 单次提取
# ==========================================================================

def _refine_single_pass(raw_input: str, temperature: float) -> StructuredRequirement:
    """
    实验模式：单次 LLM 调用直接出 StructuredRequirement。

    不反问用户，缺失信息用合理默认值填充。

    V3 新增: prompt 内嵌矛盾自检——
      "生成前检查：参数之间是否存在矛盾（如 R 和 C 的乘积与截止频率不一致）？"
      这不是一个独立的 LLM 调用，而是嵌在 node1 的 prompt 里，
      LLM 在生成结构化需求的同时自我审查参数一致性。
    """
    prompt = (
        "根据以下用户需求，直接生成结构化需求 JSON。不需要反问，缺失信息用合理默认值填充。\n\n"
        f"用户需求: {raw_input}\n\n"
        # ↓ V3 矛盾自检：让 LLM 在生成前思考参数间是否自洽
        "【V3 自检】生成前检查：参数之间是否存在矛盾（如 R 和 C 的乘积与截止频率不一致）？"
        "如有矛盾，以用户明确指定的值为准推导其他参数。\n\n"
        f"JSON Schema:\n{json.dumps(StructuredRequirement.model_json_schema(), ensure_ascii=False, indent=2)}"
    )
    result = chat([user_msg(prompt)], temperature=temperature, max_tokens=2048)
    try:
        req = StructuredRequirement.model_validate_json(extract_json(result))
    except Exception:
        req = StructuredRequirement(component_type="未知", raw_input=raw_input)
    req.raw_input = raw_input
    return req
