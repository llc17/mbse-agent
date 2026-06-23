"""
节点 1 — 需求解析。V2 版：支持 interactive/experiment 两种模式。

interactive: 多轮对话 input() 精炼
experiment: 单次 LLM 调用直接出 StructuredRequirement
"""

import json
import time
import logging

from src.llm_client import chat, user_msg, assistant_msg, system_msg
from src.schemas import StructuredRequirement
from src.utils import load_prompt, extract_json, format_history

logger = logging.getLogger("node1")


def node1_refine(state: dict) -> dict:
    """LangGraph 节点：需求精炼。读 state，写回 req + dialogue_history + timing。"""
    t0 = time.time()
    mode = state.get("mode", "interactive")
    raw_input = state.get("raw_input", "")
    history = list(state.get("dialogue_history", []))
    temperature = state.get("temperature", 0.3)

    if mode == "experiment":
        req = _refine_single_pass(raw_input, temperature)
        req.clarification_rounds = 0
        history = [user_msg(raw_input)]
    else:
        feedback = state.get("human_feedback", "")
        if feedback and history:
            history.append(user_msg(f"上次需求被驳回，反馈: {feedback}"))
        elif not history:
            history = [user_msg(raw_input)]
        req = _refine_interactive(history, temperature)

    elapsed = time.time() - t0
    logger.info("节点1 完成 (%.1fs), 类型=%s, 参数=%s", elapsed, req.component_type, req.parameters)

    return {
        "req": req.model_dump(),
        "dialogue_history": history,
        "timing": {**state.get("timing", {}), "node1": elapsed},
    }


def _refine_interactive(history: list[dict], temperature: float, max_rounds: int = 10) -> StructuredRequirement:
    """多轮对话精炼（interactive 模式）。"""
    prompts_dir = None  # use default

    for round_num in range(1, max_rounds + 1):
        # 检查完整性
        completeness_prompt = (
            load_prompt("node1_completeness.txt", prompts_dir)
            .replace("{dialogue_history}", format_history(history))
        )
        result = chat([user_msg(completeness_prompt)], temperature=0.1, max_tokens=512)
        try:
            completeness = json.loads(extract_json(result))
        except json.JSONDecodeError:
            completeness = {"is_complete": False, "missing_fields": ["JSON解析失败"], "suggestions": []}

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

        # 生成反问
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

        print(f"\n[节点1] 第{round_num}轮: {clarify_msg}")
        user_answer = input("\n你的回答: ").strip()
        if not user_answer:
            user_answer = "不需要补充，用已有信息即可。"

        history.append(assistant_msg(clarify_msg))
        history.append(user_msg(user_answer))

    # 达到 max_rounds，强制提取
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


def _refine_single_pass(raw_input: str, temperature: float) -> StructuredRequirement:
    """实验模式：单次 LLM 调用直接出 StructuredRequirement。"""
    prompt = (
        "根据以下用户需求，直接生成结构化需求 JSON。不需要反问，缺失信息用合理默认值填充。\n\n"
        f"用户需求: {raw_input}\n\n"
        f"JSON Schema:\n{json.dumps(StructuredRequirement.model_json_schema(), ensure_ascii=False, indent=2)}"
    )
    result = chat([user_msg(prompt)], temperature=temperature, max_tokens=2048)
    try:
        req = StructuredRequirement.model_validate_json(extract_json(result))
    except Exception:
        req = StructuredRequirement(component_type="未知", raw_input=raw_input)
    req.raw_input = raw_input
    return req
