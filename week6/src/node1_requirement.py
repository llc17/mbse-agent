"""
节点 1 — 需求解析。V5 版：支持 interactive / experiment / streamlit 三种模式。

interactive: 多轮对话 input() 精炼（V4 CLI 模式）
experiment: 单次 LLM 调用直接出 StructuredRequirement
streamlit:  多轮对话 interrupt() 精炼（V5 Web UI 模式）
"""

import json
import time
import logging

from langgraph.types import interrupt

from src.llm_client import chat, chat_structured, user_msg, assistant_msg, system_msg
from src.schemas import StructuredRequirement, CompletenessCheck
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
    elif mode == "streamlit":
        # V5: Streamlit 模式 — 用 interrupt() 代替 input()
        feedback = state.get("human_feedback", "")
        if feedback and history:
            history.append(user_msg(f"上次需求被驳回，反馈: {feedback}"))
        elif not history:
            history = [user_msg(raw_input)]
        req = _refine_streamlit(history, temperature)
    else:
        # V4 CLI 模式 — 用 input()
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


def _refine_streamlit(history: list[dict], temperature: float, max_rounds: int = 10) -> StructuredRequirement:
    """V5: Streamlit 模式 — 用 interrupt() 获取用户回答。"""

    prompts_dir = None

    for round_num in range(1, max_rounds + 1):
        # 完整性检查：只有 history 足够丰富时才允许提取
        if round_num <= 3 or len(history) <= 3:
            completeness_prompt = (
                load_prompt("node1_completeness.txt", prompts_dir)
                .replace("{dialogue_history}", format_history(history))
            )
            try:
                completeness = chat_structured(
                    [user_msg(completeness_prompt)],
                    CompletenessCheck,
                    temperature=0.1, max_tokens=512,
                )
            except Exception:
                # chat_structured 失败 → 回退到普通 chat
                try:
                    result = chat([user_msg(completeness_prompt)], temperature=0.1, max_tokens=512)
                    json_text = extract_json(result)
                    data = json.loads(json_text)
                    completeness = CompletenessCheck(**data)
                except Exception:
                    logger.debug("完整性检查 JSON 解析失败（fallback 继续问问题）")
                    completeness = CompletenessCheck(is_complete=False, missing_fields=[], suggestions=[])

            # 至少经过 2 轮用户回答才允许提取（round_num >= 3 = 用户已回答 2 轮）
            if completeness.is_complete and round_num >= 3:
                try:
                    req = _extract_requirement(history)
                except Exception as e:
                    logger.warning("需求提取失败，继续问问题: %s", e)
                    completeness = {"is_complete": False, "missing_fields": ["提取失败"], "suggestions": []}
                else:
                    req.raw_input = history[0].get("content", "")
                    req.clarification_rounds = round_num - 1
                    return req

            # 生成反问
            missing_fields = completeness.missing_fields or []
            if missing_fields:
                missing_str = "\n".join(f"- {m}" for m in missing_fields)
                question_ctx = f"根据当前信息，还缺少: {missing_str}。"
            else:
                question_ctx = "根据当前信息，还缺少关键细节。"

            question = (
                f"{question_ctx}"
                "请用中文友好地向用户提问，一次只问1-2个最重要的问题，给出A/B/C选项。"
            )
        else:
            # 已达轮数上限，强制提取
            try:
                req = _extract_requirement(history)
            except Exception as e:
                logger.error("强制提取也失败: %s", e)
                req = StructuredRequirement(component_type="未知", raw_input=history[0].get("content", ""))
            req.raw_input = history[0].get("content", "")
            req.clarification_rounds = max_rounds
            return req

        clarify_msg = chat([
            system_msg("你是系统需求分析师，负责向用户澄清需求。一次只问1-2个问题，给出A/B/C选项。"),
            user_msg(f"对话历史:\n{format_history(history)}\n\n{question}\n\n请生成用户友好的反问。"),
        ], temperature=0.5, max_tokens=512).strip()

        logger.info("[节点1 Streamlit] 第%d轮: %s", round_num, clarify_msg)

        # 安全阀：生成的澄清问题为空或太短 → 直接提取需求
        if not clarify_msg or len(clarify_msg) < 15:
            logger.warning("第%d轮生成的澄清问题为空或太短，跳过询问直接提取", round_num)
            try:
                req = _extract_requirement(history)
            except Exception as e:
                logger.error("安全阀提取失败: %s", e)
                req = StructuredRequirement(component_type="未知", raw_input=history[0].get("content", ""))
            req.raw_input = history[0].get("content", "")
            req.clarification_rounds = round_num - 1
            return req

        # V5: interrupt() 替代 input()
        user_answer = interrupt({
            "node": "node1_clarify",
            "type": "hitl_clarify",
            "round": round_num,
            "message": clarify_msg,
            "data": {
                "question": clarify_msg,
                "round": round_num,
                "missing_fields": completeness.missing_fields or [],
            },
        })

        if isinstance(user_answer, dict):
            user_answer = user_answer.get("answer", "")
        if not user_answer or not str(user_answer).strip():
            user_answer = "不需要补充，用已有信息即可。"

        history.append(assistant_msg(clarify_msg))
        history.append(user_msg(str(user_answer)))

    # 达到 max_rounds，强制提取
    try:
        req = _extract_requirement(history)
    except Exception as e:
        logger.error("达到 max_rounds 强制提取也失败: %s", e)
        req = StructuredRequirement(component_type="未知", raw_input=history[0].get("content", ""))
    req.raw_input = history[0].get("content", "")
    req.clarification_rounds = max_rounds
    return req


def _extract_requirement(history: list[dict]) -> StructuredRequirement:
    """从对话历史中提取结构化需求。使用 chat_structured 强制 JSON 输出。"""

    messages = [
        system_msg(
            "你是系统需求分析师。从以下对话历史中提取结构化需求信息，"
            "填写为具体的 JSON 数据。不要输出 JSON Schema 本身，"
            "要输出填充了具体数值的实例。缺失的信息用合理默认值。\n\n"
            f"对话历史:\n{format_history(history)}"
        ),
        user_msg("请输出结构化需求 JSON 数据。"),
    ]
    try:
        return chat_structured(messages, StructuredRequirement, temperature=0.2, max_tokens=2048)
    except Exception as e:
        logger.warning("chat_structured 提取失败 (%s)，回退到普通 chat", e)
        # 回退方案: 普通 chat + 手动解析
        schema_str = json.dumps(StructuredRequirement.model_json_schema(), ensure_ascii=False, indent=2)
        final_prompt = (
            "从对话历史中提取结构化需求，按以下 JSON Schema 输出具体数据。\n"
            "重要: 输出的是填充了数值的 JSON 实例，不是 Schema 本身！\n\n"
            f"对话历史:\n{format_history(history)}\n\n"
            f"JSON Schema:\n{schema_str}"
        )
        final_result = chat([user_msg(final_prompt)], temperature=0.2, max_tokens=2048)
        return StructuredRequirement.model_validate_json(extract_json(final_result))


def _refine_interactive(history: list[dict], temperature: float, max_rounds: int = 10) -> StructuredRequirement:
    """多轮对话精炼（interactive CLI 模式）。"""
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
                "根据以下对话内容，提取系统需求的结构化信息，按指定 JSON 格式输出。\n\n"
                f"对话历史：\n{format_history(history)}\n\n"
                "输出 JSON 格式（不要输出 schema，输出具体数据）：\n"
                f"{json.dumps(StructuredRequirement.model_json_schema(), ensure_ascii=False, indent=2)}"
            )
            final_result = chat([user_msg(final_prompt)], temperature=0.2, max_tokens=2048)
            try:
                req = StructuredRequirement.model_validate_json(extract_json(final_result))
            except Exception:
                req = StructuredRequirement(component_type="未知", raw_input=history[0].get("content", ""))
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
            system_msg("你是系统需求分析师，负责向用户澄清需求。一次只问1-2个问题，给出A/B/C选项。"),
            user_msg(f"对话历史:\n{format_history(history)}\n\n{question}\n\n请生成用户友好的反问。"),
        ], temperature=0.5, max_tokens=512).strip()

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
    """实验模式：单次 LLM 调用直接出 StructuredRequirement。V3: 加入矛盾自检提示。"""
    prompt = (
        "根据以下用户需求，直接生成结构化需求 JSON。不需要反问，缺失信息用合理默认值填充。\n\n"
        f"用户需求: {raw_input}\n\n"
        "【V3 自检】生成前检查：参数之间是否存在矛盾（如 R 和 C 的乘积与截止频率不一致）？"
        "如有矛盾，以用户明确指定的值为准推导其他参数。\n\n"
        "输出 JSON 格式（不要输出 schema，输出具体数据）：\n"
        f"JSON Schema:\n{json.dumps(StructuredRequirement.model_json_schema(), ensure_ascii=False, indent=2)}"
    )
    result = chat([user_msg(prompt)], temperature=temperature, max_tokens=2048)
    try:
        req = StructuredRequirement.model_validate_json(extract_json(result))
    except Exception:
        req = StructuredRequirement(component_type="未知", raw_input=raw_input)
    req.raw_input = raw_input
    return req
