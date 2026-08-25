"""
节点 1 — 需求分析 Agent（V6 三阶段版）。

V4 → V6 变更:
  - 单次 chat() → 生成→自查完整性→补充缺失信息（三阶段 Agent 循环）
  - 自查 prompt 按 V6 规范重写：具体角色 + 5 条检查项 + JSON 输出
  - 实验模式：自查发现缺失 → LLM 自动补全合理默认值（不需要用户干预）
  - 交互模式：同 V4，但每轮使用结构化自查 prompt 替代原来的笼统检查

Agent ① 内部流程:
  生成需求 → 自查完整性 → 如果有关键缺失 → 补全 → 再查 → 通过
"""

import json
import logging
import time

from src.llm_client import chat, chat_with, user_msg, assistant_msg, system_msg
from src.agent_loop import (
    run_review_loop,
    ReviewResult,
    ReviewIssue,
    AgentLoopResult,
    parse_review_json,
)
from src.schemas import StructuredRequirement
from src.utils import load_prompt, extract_json, format_history

logger = logging.getLogger("node1")


def node1_refine(state: dict) -> dict:
    """LangGraph 节点：需求精炼（V6 Agent 版）。

    读 state，写回 req + dialogue_history + timing。
    """
    t0 = time.time()
    mode = state.get("mode", "interactive")
    raw_input = state.get("raw_input", "")
    history = list(state.get("dialogue_history", []))
    temperature = state.get("temperature", 0.3)

    if mode == "experiment":
        feedback = state.get("human_feedback", "")
        req = _refine_with_agent_loop(raw_input, temperature, feedback)
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
    logger.info("节点1 完成 (%.1fs), 类型=%s, 参数=%s",
                elapsed, req.component_type, req.parameters)

    return {
        "req": req.model_dump(),
        "dialogue_history": history,
        "timing": {**state.get("timing", {}), "node1": elapsed},
    }


# ============================================================
# V6: 实验模式 — 三阶段 Agent 循环
# ============================================================

def _refine_with_agent_loop(raw_input: str, temperature: float,
                           feedback: str = "") -> StructuredRequirement:
    """V6 实验模式：生成 → 自查 → 补全（最多 2 轮）。"""

    # ── Stage 1: 生成 ──
    def generate() -> str:
        """从自然语言生成结构化需求 JSON。"""
        feedback_section = ""
        if feedback:
            feedback_section = (
                f"\n## 上次流水线的反馈（请据此修正需求）\n{feedback}\n"
                f"请确保参数完整，缺失的信息用合理的工程默认值填充。\n"
            )
        prompt = (
            "根据以下用户需求，直接生成结构化需求 JSON。\n"
            "不需要反问，缺失信息用合理的工程默认值填充。\n\n"
            "【V6 自检】生成前检查参数之间的一致性：\n"
            "- R和C的乘积是否符合截止频率？（如用户说1kHz截止频率、R=1kΩ，则C≈0.159μF）\n"
            "- 热参数是否自洽？（如房间体积→热容估算是否合理）\n"
            "- 运放增益是否与反馈电阻比一致？\n"
            "如有矛盾，以用户明确指定的值为准，推导其他参数。\n\n"
            f"用户需求: {raw_input}\n"
            f"{feedback_section}\n"
            f"JSON Schema:\n"
            f"{json.dumps(StructuredRequirement.model_json_schema(), ensure_ascii=False, indent=2)}"
        )
        result = chat([user_msg(prompt)], temperature=temperature, max_tokens=2048)
        return result

    # ── Stage 2: 自查完整性 ──
    def review(output: str) -> ReviewResult:
        """结构化自查：具体检查项 + JSON 输出。"""
        req_json_text = _try_extract_req_json(output)
        review_prompt = (
            load_prompt("node1_review.txt")
            .replace("{raw_input}", raw_input)
            .replace("{generated_req}", req_json_text)
        )
        review_raw = chat_with("review", [user_msg(review_prompt)], temperature=0.1, max_tokens=1024)
        return parse_review_json(review_raw)

    # ── Stage 3: 补全 ──
    def revise(output: str, issues: list[ReviewIssue]) -> str:
        """根据自查发现的问题，补充缺失信息。"""
        issues_text = "\n".join(
            f"- [{i.severity}] {i.description} → {i.suggestion}"
            for i in issues
        )
        revise_prompt = (
            f"以下是根据用户需求生成的结构化需求，但自查发现以下问题：\n\n"
            f"## 用户原始需求\n{raw_input}\n\n"
            f"## 当前生成的需求\n{output}\n\n"
            f"## 自查发现的问题\n{issues_text}\n\n"
            f"请根据问题修正需求 JSON。缺失的信息用合理的工程默认值填充。\n"
            f"只输出修正后的完整 JSON，不要包含解释。\n\n"
            f"JSON Schema:\n"
            f"{json.dumps(StructuredRequirement.model_json_schema(), ensure_ascii=False, indent=2)}"
        )
        revised = chat([user_msg(revise_prompt)], temperature=0.2, max_tokens=2048)
        return revised

    # ── 运行三阶段循环 ──
    loop_result = run_review_loop(
        generate_fn=generate,
        review_fn=review,
        revise_fn=revise,
        max_rounds=2,  # 需求阶段 2 轮足够
        label="node1",
    )

    # ── 解析最终输出为 StructuredRequirement ──
    return _parse_req_result(loop_result, raw_input)


def _try_extract_req_json(output: str) -> str:
    """尝试从 LLM 输出中提取需求 JSON（用于自查）。"""
    text = output.strip()
    # 尝试直接 parse
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass
    # 尝试剥离 markdown 后 parse
    cleaned = extract_json(text)
    try:
        json.loads(cleaned)
        return cleaned
    except json.JSONDecodeError:
        pass
    # 无法解析，返回原始文本（自查 LLM 可以处理非 JSON）
    return text


def _parse_req_result(loop_result: AgentLoopResult, raw_input: str) -> StructuredRequirement:
    """从 Agent 循环结果中解析 StructuredRequirement。"""
    output = loop_result.final_output
    try:
        req = StructuredRequirement.model_validate_json(extract_json(output))
    except Exception:
        logger.warning("节点1: 结构化解析失败，使用默认值")
        req = StructuredRequirement(component_type="未知", raw_input=raw_input)
    req.raw_input = raw_input

    # 记录审查结果到 clarification_rounds（复用字段）
    if loop_result.review_history:
        req.clarification_rounds = len(loop_result.review_history)
    return req


# ============================================================
# V4 兼容：交互模式（多轮对话，用 V6 自查 prompt 替代）
# ============================================================

def _refine_interactive(history: list[dict], temperature: float,
                        max_rounds: int = 10) -> StructuredRequirement:
    """多轮对话精炼（interactive 模式），用 V6 结构化自查 prompt。"""

    for round_num in range(1, max_rounds + 1):
        # ── V6: 用结构化自查 prompt（替代 V4 的笼统检查）──
        completeness_prompt = (
            load_prompt("node1_completeness.txt")
            .replace("{dialogue_history}", format_history(history))
        )
        result = chat([user_msg(completeness_prompt)], temperature=0.1, max_tokens=512)
        try:
            completeness = json.loads(extract_json(result))
        except json.JSONDecodeError:
            completeness = {"is_complete": False, "missing_fields": ["JSON解析失败"],
                          "suggestions": []}

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
        req = StructuredRequirement(component_type="未知",
                                    raw_input=history[0].get("content", ""))
    req.raw_input = history[0].get("content", "")
    req.clarification_rounds = max_rounds
    return req
