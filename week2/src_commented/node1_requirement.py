# -*- coding: utf-8 -*-
"""
=============================================================================
node1_requirement.py — 节点1：需求解析（多轮对话）
=============================================================================

把用户自然语言（"做个 1kHz 低通"）→ 多轮对话 → StructuredRequirement。

V1 流程（和 V2 的区别：没有 experiment 模式，只有 interactive）:

  1. 用户输入一句话需求
  2. LLM 用完整对话历史判断完整性 → 缺信息就生成反问
  3. 用户回答，追加到历史 → 回到步骤 2
  4. 信息完整 → 从全部历史中提取 StructuredRequirement JSON

关键设计:
  - 每次检查都把完整的对话历史传给 LLM，不只是 req 对象
  - 这样 LLM 能看到上下文，不会问重复问题
"""

# ====================================================================
# 导入
# ====================================================================

import json                                              # 解析/生成 JSON
import os                                                # 路径操作

from src.llm_client import chat, user_msg, assistant_msg, system_msg
# chat:          发消息，返回文本
# user_msg:      构造 {"role": "user", "content": "..."}
# assistant_msg: 构造 {"role": "assistant", "content": "..."}
# system_msg:    构造 {"role": "system", "content": "..."}

from src.schemas import StructuredRequirement            # 节点1 的产出类型


# ====================================================================
# _load_prompt() — 加载 prompt 模板
# ====================================================================
def _load_prompt(name: str) -> str:
    """
    读取 prompts/ 目录下的 .txt 文件。

    注意:
      V1 中这个函数定义在每个节点文件里（重复代码）。
      V2 把它提取到了独立的 utils.py 中共享。
    """
    # os.path.dirname(__file__) = 当前文件所在目录（.../week2/src_commented/）
    # os.path.join(..., "..", "prompts", name) = .../week2/prompts/name
    path = os.path.join(os.path.dirname(__file__), "..", "prompts", name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()                                  # read() 返回文件全部内容


# ====================================================================
# refine_requirement() — 节点1 主入口
# ====================================================================
def refine_requirement(raw_input: str, max_rounds: int = 10) -> StructuredRequirement:
    """
    多轮对话精炼需求。

    参数:
      raw_input:  用户最初的输入文本
      max_rounds: 最多反问多少轮（防止无限循环）

    返回:
      StructuredRequirement 对象

    变量 history 的结构:
      一个列表（list），每个元素是一个字典（dict），表示一条消息：
      [
        {"role": "user", "content": "做个低通滤波器"},        ← 第0条: 用户初始输入
        {"role": "assistant", "content": "截止频率是多少?"},   ← 第1条: LLM 反问
        {"role": "user", "content": "1kHz"},                  ← 第2条: 用户回答
        ...
      ]
    """
    # ---- 初始化对话历史 ----
    history: list[dict] = [user_msg(raw_input)]           # 列表里只有一条: 用户初始输入
    # : list[dict] 是类型标注（type annotation），Python 运行时不管，
    # 但 IDE 和类型检查器会用它来推断变量类型

    # ---- 多轮对话循环 ----
    for round_num in range(1, max_rounds + 1):           # range(1, 11) → 1, 2, ..., 10

        # ====== 步骤 1: 用完整对话历史检查完整性 ======
        completeness_prompt = (
            _load_prompt("node1_completeness.txt")        # 加载 prompt 模板
            .replace("{dialogue_history}", _format_history(history))
            # str.replace("旧", "新"): 替换模板中的占位符
        )

        result = chat(                                   # 调 LLM（纯文本模式）
            [user_msg(completeness_prompt)],             # 消息只包一层用户消息
            temperature=0.1,                             # 低温度 = LLM 输出更稳定
            max_tokens=512,                              # 返回很短（JSON 只有几个字段）
        )

        # ---- 解析 LLM 返回的 JSON ----
        try:
            completeness = json.loads(_extract_json(result))
            # json.loads("字符串") → Python 字典
        except json.JSONDecodeError:                     # JSON 解析失败（LLM 没按格式）
            completeness = {
                "is_complete": False,
                "missing_fields": ["JSON解析失败"],
                "suggestions": [],
            }

        # ====== 步骤 2: 如果信息完整 → 提取 StructuredRequirement ======
        if completeness.get("is_complete"):
            final_prompt = (
                f"根据以下对话内容，提取系统需求的结构化信息。\n\n"
                f"对话历史：\n{_format_history(history)}\n\n"
                f"返回 JSON Schema：\n"
                f"{json.dumps(StructuredRequirement.model_json_schema(), ensure_ascii=False, indent=2)}"
                # json.dumps: 字典 → JSON 字符串
                # model_json_schema(): Pydantic 内置方法
            )

            final_result = chat(                         # 调 LLM 生成最终 StructuredRequirement
                [user_msg(final_prompt)],
                temperature=0.2,                         # 较低温度保证格式稳定
                max_tokens=2048,
            )

            req = StructuredRequirement.model_validate_json(_extract_json(final_result))
            # model_validate_json: JSON 字符串 → Pydantic 对象

            req.raw_input = raw_input                    # 记录原始输入
            req.clarification_rounds = round_num - 1     # 精炼轮数（当前轮 - 1，因为第一轮是初始检查）
            print(f"\n[节点1] 需求完整，{round_num - 1}轮精炼完成。")
            return req

        # ====== 步骤 3: 信息不完整 → 生成反问 ======
        missing_str = "\n".join(                         # 用换行符连接
            f"- {m}" for m in completeness.get("missing_fields", [])
        )                                                # 生成器表达式: 把每项格式化为 "- 字段名"

        question = (
            f"根据当前已知信息，还缺少: {missing_str}。"
            f"请用中文友好地向用户提问，一次只问1-2个最重要的点，给具体选项。"
        )

        # 用 system_msg 设定 LLM 角色 → 生成友好的反问
        clarify_msg = chat([
            system_msg(
                "你是系统需求分析师，与用户对话。"
                "请根据以下对话历史，生成一句友好的反问。"
                f"\n\n对话历史：\n{_format_history(history)}"
                f"\n\n{question}"
            ),
        ], temperature=0.5, max_tokens=256).strip()     # 温度 0.5 = 反问更自然

        print(f"\n[节点1] 第{round_num}轮: {clarify_msg}")

        # ====== 步骤 4: 获取用户回答 ======
        user_answer = input("\n你的回答: ").strip()      # input() 从终端读一行

        if not user_answer:                              # 用户直接按回车（空输入）
            user_answer = "不需要补充，用已有信息即可。"

        # 把本轮对话追加到历史
        history.append(assistant_msg(clarify_msg))        # LLM 的反问
        history.append(user_msg(user_answer))             # 用户的回答

    # ---- 达到 max_rounds 限制，强制提取 ----
    print(f"\n[节点1] 达到最大轮数 {max_rounds}，用最后状态。")

    final_prompt = (
        f"根据以下对话内容，提取系统需求的结构化信息。\n\n"
        f"对话历史：\n{_format_history(history)}\n\n"
        f"返回 JSON Schema：\n"
        f"{json.dumps(StructuredRequirement.model_json_schema(), ensure_ascii=False, indent=2)}"
    )
    final_result = chat([user_msg(final_prompt)], temperature=0.2, max_tokens=2048)
    try:
        req = StructuredRequirement.model_validate_json(_extract_json(final_result))
    except Exception:                                    # 解析失败：给最小化 fallback
        req = StructuredRequirement(component_type="未知", raw_input=raw_input)

    req.raw_input = raw_input
    req.clarification_rounds = max_rounds
    return req


# ====================================================================
# _extract_json() — 从 LLM 输出中提取 JSON
# ====================================================================
def _extract_json(text: str) -> str:
    """
    清洗 LLM 返回中的 markdown 代码块标记。

    比如 LLM 可能返回:
      ```json
      {"component_type": "RC低通", ...}
      ```
    或者:
      ```
      {"component_type": "RC低通", ...}
      ```

    这个函数剥掉外层的 ``` 包裹，只保留 JSON 文本。
    """
    text = text.strip()                                  # 去首尾空白

    if text.startswith("```"):                           # 以代码块标记开头
        lines = text.split("\n")                         # 拆行
        if lines[0].startswith("```"):                   # 第一行是 ```json 或 ```
            lines = lines[1:]                            #   去掉第一行
        if lines and lines[-1].strip() == "```":         # 最后一行是 ```
            lines = lines[:-1]                           #   去掉最后一行
        text = "\n".join(lines)                          # 重新拼成字符串

    return text


# ====================================================================
# _format_history() — 把对话历史格式化为可读文本
# ====================================================================
def _format_history(history: list[dict]) -> str:
    """
    把对话历史从机器格式转为人类可读字符串，然后喂给 LLM。

    输入:
      [{"role": "user", "content": "做个低通"},
       {"role": "assistant", "content": "截止频率?"},
       {"role": "user", "content": "1kHz"}]

    输出:
      "用户: 做个低通\n分析师: 截止频率?\n用户: 1kHz"

    注意: 每条消息只取前 300 字符，防止 prompt 太长。
    """
    lines = []                                           # 放每行文本的列表

    for msg in history:                                  # 遍历每条消息
        role = "用户" if msg["role"] == "user" else "分析师"
        # 三元表达式: a if 条件 else b

        lines.append(f"{role}: {msg['content'][:300]}")
        # msg['content'][:300]: 只取前 300 个字符

    return "\n".join(lines)                              # 用换行符连接所有行
