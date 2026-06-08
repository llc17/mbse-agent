"""
节点 1 — 需求解析：多轮对话把自然语言精炼为 StructuredRequirement。

流程：
  1. 用户输入一句话需求
  2. LLM 看对话历史判断完整性 → 缺信息则生成反问
  3. 用户回答，追加到历史 → 回到步骤 2
  4. 完整 → 从全部历史中提取 StructuredRequirement

关键：每次检查都把完整对话历史传给 LLM，不只是 req 对象。
"""

import json

from src.llm_client import chat, user_msg, assistant_msg, system_msg
from src.schemas import StructuredRequirement


def _load_prompt(name: str) -> str:
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "prompts", name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def refine_requirement(raw_input: str, max_rounds: int = 10) -> StructuredRequirement:
    """多轮对话精炼需求，最多 max_rounds 轮。"""
    history: list[dict] = [user_msg(raw_input)] #history：变量名  : ：固定格式，代表「这个变量的类型是」 list[dict]：列表里面装字典
    #我声明一个叫 history 的变量，它必须是一个列表，而且列表里的每一条数据，都是字典格式。 
    #实际上完全可以写为history= [user_msg(raw_input)]      : list[dict]只是方便理解

    for round_num in range(1, max_rounds + 1):
        # Step 1: 用对话历史检查完整性
        completeness_prompt = (
            _load_prompt("node1_completeness.txt")
            .replace("{dialogue_history}", _format_history(history))
        )
        result = chat([user_msg(completeness_prompt)], temperature=0.1, max_tokens=512)
        try:
            completeness = json.loads(_extract_json(result))
        except json.JSONDecodeError:
            completeness = {"is_complete": False, "missing_fields": ["JSON解析失败"], "suggestions": []}     

        if completeness.get("is_complete"):
            # 信息充足 → 从全部历史中提取 StructuredRequirement
            final_prompt = (
                f"根据以下对话内容，提取系统需求的结构化信息。\n\n"
                f"对话历史：\n{_format_history(history)}\n\n"
                f"返回 JSON Schema：\n"
                f"{json.dumps(StructuredRequirement.model_json_schema(), ensure_ascii=False, indent=2)}"
            )
            final_result = chat([user_msg(final_prompt)], temperature=0.2, max_tokens=2048)
            req = StructuredRequirement.model_validate_json(_extract_json(final_result))
            req.raw_input = raw_input
            req.clarification_rounds = round_num - 1
            print(f"\n[节点1] 需求完整，{round_num - 1}轮精炼完成。")
            return req

        # Step 2: 缺信息 → 生成反问
        missing_str = "\n".join(f"- {m}" for m in completeness.get("missing_fields", []))
        question = (
            f"根据当前已知信息，还缺少: {missing_str}。"
            f"请用中文友好地向用户提问，一次只问1-2个最重要的点，给具体选项。"
        )
        clarify_msg = chat([
            system_msg(
                "你是系统需求分析师，与用户对话。"
                "请根据以下对话历史，生成一句友好的反问。"
                f"\n\n对话历史：\n{_format_history(history)}"
                f"\n\n{question}"
            ),
        ], temperature=0.5, max_tokens=256).strip()

        print(f"\n[节点1] 第{round_num}轮: {clarify_msg}")

        # Step 3: 获取用户回答
        user_answer = input("\n你的回答: ").strip()
        if not user_answer:
            user_answer = "不需要补充，用已有信息即可。"

        history.append(assistant_msg(clarify_msg))
        history.append(user_msg(user_answer))

    print(f"\n[节点1] 达到最大轮数 {max_rounds}，用最后状态。")
    # 最后尝试从全部历史中提取
    final_prompt = (
        f"根据以下对话内容，提取系统需求的结构化信息。\n\n"
        f"对话历史：\n{_format_history(history)}\n\n"
        f"返回 JSON Schema：\n"
        f"{json.dumps(StructuredRequirement.model_json_schema(), ensure_ascii=False, indent=2)}"
    )
    final_result = chat([user_msg(final_prompt)], temperature=0.2, max_tokens=2048)
    try:
        req = StructuredRequirement.model_validate_json(_extract_json(final_result))
    except Exception:
        req = StructuredRequirement(component_type="未知", raw_input=raw_input)
    req.raw_input = raw_input
    req.clarification_rounds = max_rounds
    return req


def _extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text


def _format_history(history: list[dict]) -> str:
    lines = []
    for msg in history:
        role = "用户" if msg["role"] == "user" else "分析师"
        lines.append(f"{role}: {msg['content'][:300]}")
    return "\n".join(lines)
