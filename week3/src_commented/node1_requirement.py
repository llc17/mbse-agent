# -*- coding: utf-8 -*-
"""
=============================================================================
node1_requirement.py — 节点1：需求解析
=============================================================================

这个节点是整个流水线的入口。它的任务是把用户输入的自然语言（"做个1kHz低通滤波器"）
转化为结构化的数据对象（StructuredRequirement）。

V2 支持两种运行模式：

  【interactive 模式】（交互模式）
    用户一句话 → LLM 检查是否信息齐全
      ├─ 不全 → LLM 反问 → 用户回答 → 回到检查步骤
      └─ 齐全 → LLM 提取 StructuredRequirement JSON
    这个模式下用户真正参与对话。

  【experiment 模式】（实验模式）
    用户给完整的输入文本 → LLM 一次调用直接出 StructuredRequirement
    不反问、不打断。用于批量跑 360 次实验。

本文件的 LangGraph 节点函数是 node1_refine()，它：
  1. 读 PipelineState 中的 raw_input 和 dialogue_history
  2. 调用 LLM 精炼需求
  3. 把 req 和更新后的 dialogue_history 写回 state
"""

# ====================================================================
# 导入
# ====================================================================

import json                                              # 解析 LLM 返回的完整性检查 JSON
import time                                              # 计时
import logging                                           # 日志

from src.llm_client import chat, user_msg, assistant_msg, system_msg
# chat:          发消息给 LLM，返回纯文本
# user_msg:      构造 {"role": "user", "content": "..."}
# assistant_msg: 构造 {"role": "assistant", "content": "..."}
# system_msg:    构造 {"role": "system", "content": "..."}

from src.schemas import StructuredRequirement            # 节点1 产出的数据类型
from src.utils import load_prompt, extract_json, format_history
# load_prompt:    加载 prompt 模板文件
# extract_json:   从 LLM 返回中提取 JSON
# format_history: 把对话历史转成可读字符串

logger = logging.getLogger("node1")                      # 创建节点1 专用的日志记录器


# ====================================================================
# node1_refine() — LangGraph 节点函数（入口）
# ====================================================================
def node1_refine(state: dict) -> dict:
    """
    LangGraph 节点函数。被主图调用。

    参数:
      state: PipelineState 字典（LangGraph 自动传入）

    返回:
      需要更新的 state 字段（LangGraph 自动合并回 state）

    LangGraph 的工作原理：
      - 节点函数接收完整的 state
      - 节点函数返回一个 dict，只包含想要更新的字段
      - LangGraph 自动把返回的 dict 合并到主 state 中
      - 不需要返回的字段可以省略
    """
    t0 = time.time()                                     # 记录开始时间（用于计时）

    # ---- 从 state 中读取相关字段 ----
    mode = state.get("mode", "interactive")              # 运行模式，默认 "interactive"
    # state.get("key", default_value): 安全取值，如果 key 不存在返回默认值

    raw_input = state.get("raw_input", "")               # 用户原始输入
    history = list(state.get("dialogue_history", []))     # 对话历史列表
    # list(...) 创建副本：不修改原 state 中的列表（好习惯）
    temperature = state.get("temperature", 0.3)          # LLM 温度参数

    # ---- 根据模式走不同路径 ----
    if mode == "experiment":
        # 实验模式：单次 LLM 调用，无对话
        req = _refine_single_pass(raw_input, temperature)
        req.clarification_rounds = 0                     # 精炼轮数固定为 0
        history = [user_msg(raw_input)]                  # 对话历史只存一条用户消息
    else:
        # 交互模式：多轮对话
        feedback = state.get("human_feedback", "")
        # 如果存在打回反馈，把它加到对话历史里
        if feedback and history:
            history.append(user_msg(                     # 构造一条用户消息
                f"上次需求被驳回，反馈: {feedback}"        # 内容包含驳回原因
            ))
        elif not history:                                # 如果是第一次进入节点1（没有历史）
            history = [user_msg(raw_input)]              #   用原始输入初始化对话历史

        req = _refine_interactive(history, temperature)   # 调用多轮对话函数

    elapsed = time.time() - t0                           # 计算耗时
    logger.info("节点1 完成 (%.1fs), 类型=%s, 参数=%s",
                elapsed, req.component_type, req.parameters)

    # ---- 返回要更新的字段 ----
    return {
        "req": req.model_dump(),                         # .model_dump() 把 Pydantic 对象转成普通字典
        # LangGraph 要求 state 中的所有数据都 JSON 可序列化，所以存 dict 而不是 Pydantic 对象
        "dialogue_history": history,                     # 更新对话历史
        "timing": {**state.get("timing", {}), "node1": elapsed},
        # ** 是字典解包：把旧 timing 字典展开，再加一个 "node1": elapsed
        # 等价于: new_timing = old_timing.copy(); new_timing["node1"] = elapsed
    }


# ====================================================================
# _refine_interactive() — 多轮对话精炼（interactive 模式）
# ====================================================================
def _refine_interactive(
    history: list[dict],                                 # 对话历史列表
    temperature: float,                                  # LLM 温度
    max_rounds: int = 10,                                # 最多反问 10 轮（防止无限循环）
) -> StructuredRequirement:
    """
    多轮对话精炼需求。

    每一轮的流程:
      1. 把对话历史发给 LLM → 判断信息是否齐全
      2. 如果齐全 → 从历史中提取 StructuredRequirement → 返回
      3. 如果不全 → LLM 生成一句反问 → 用户回答 → 追加到历史 → 下一轮

    这个函数直接调用 input()，和终端用户交互。
    """
    prompts_dir = None                                   # 使用默认 prompt 目录

    for round_num in range(1, max_rounds + 1):           # 循环: round_num = 1, 2, 3, ..., 10

        # ========== 步骤 1：检查完整性 ==========
        # 加载 prompt 模板，替换 {dialogue_history} 占位符
        completeness_prompt = (
            load_prompt("node1_completeness.txt", prompts_dir)
            .replace("{dialogue_history}", format_history(history))
            # str.replace("旧文本", "新文本"): 替换字符串
        )

        result = chat(                                   # 调用 LLM（纯文本模式）
            [user_msg(completeness_prompt)],             # 消息只包一层用户消息
            temperature=0.1,                             # 低温度 = LLM 输出更稳定
            max_tokens=512,                              # 返回很短（JSON 就几个字段）
        )

        # ---- 解析 LLM 返回的 JSON ----
        try:
            completeness = json.loads(extract_json(result))
            # json.loads(): 把 JSON 字符串解析为 Python 字典
            # extract_json(): 清洗可能的 markdown 包裹
        except json.JSONDecodeError:                     # JSON 解析失败（LLM 没按格式输出）
            completeness = {
                "is_complete": False,                    #   默认判为不完整
                "missing_fields": ["JSON解析失败"],        #   记录异常
                "suggestions": [],
            }

        # ========== 步骤 2：如果完整 → 提取需求 ==========
        if completeness.get("is_complete"):              # 字典的 .get() 安全取值
            # 构造最终提取 prompt
            final_prompt = (
                "根据以下对话内容，提取系统需求的结构化信息。\n\n"
                f"对话历史：\n{format_history(history)}\n\n"
                "返回 JSON Schema：\n"
                f"{json.dumps(                            # 把 Pydantic schema 转成 JSON 字符串
                    StructuredRequirement.model_json_schema(),
                    ensure_ascii=False, indent=2
                )}"
            )
            # 调用 LLM 提取
            final_result = chat(
                [user_msg(final_prompt)],
                temperature=0.2,                          # 较低温度，保证格式稳定
                max_tokens=2048                           # 给足够输出空间
            )

            req = StructuredRequirement.model_validate_json(
                extract_json(final_result)
            )                                            # 解析 JSON → Pydantic 对象
            req.raw_input = history[0].get("content", "")
            # 记录原始输入（对话历史第一条用户消息的内容）
            req.clarification_rounds = round_num - 1      # 精炼轮数（当前轮 - 1）
            return req                                    # 返回结构化需求

        # ========== 步骤 3：不完整 → 生成反问 ==========
        # 把缺失字段拼成字符串
        missing_str = "\n".join(                          # 用换行符连接
            f"- {m}" for m in completeness.get("missing_fields", [])
        )                                                # 这行是一个生成器表达式

        question = (
            f"根据当前信息，还缺少: {missing_str}。"
            "请用中文友好地向用户提问，一次只问 1-2 个最重要的问题，给出具体选项。"
        )

        # 用 system_msg 设定 LLM 角色 → 生成友好的反问
        clarify_msg = chat([
            system_msg(                                   # system 消息设定 LLM 行为
                f"你是系统需求分析师。对话历史:\n{format_history(history)}\n\n{question}"
            ),
        ], temperature=0.5, max_tokens=256).strip()       # 温度 0.5 = 让反问更自然

        print(f"\n[节点1] 第{round_num}轮: {clarify_msg}")

        # ========== 步骤 4：获取用户回答 ==========
        user_answer = input("\n你的回答: ").strip()       # input() 从终端读一行输入
        if not user_answer:                               # 如果用户直接按回车（空输入）
            user_answer = "不需要补充，用已有信息即可。"

        # 把本轮对话追加到历史
        history.append(assistant_msg(clarify_msg))        # 助手（LLM）的反问
        history.append(user_msg(user_answer))             # 用户的回答

    # ---- 达到 max_rounds（10轮），强制提取 ----
    print(f"\n[节点1] 达到最大轮数 {max_rounds}，用最后状态。")
    final_prompt = (
        "根据对话提取结构化需求。\n"
        f"对话历史：\n{format_history(history)}\n\n"
        f"JSON Schema:\n{json.dumps(StructuredRequirement.model_json_schema(), ensure_ascii=False, indent=2)}"
    )
    final_result = chat([user_msg(final_prompt)], temperature=0.2, max_tokens=2048)
    try:
        req = StructuredRequirement.model_validate_json(extract_json(final_result))
    except Exception:                                    # 解析失败：给一个最小化的 fallback
        req = StructuredRequirement(
            component_type="未知",
            raw_input=history[0].get("content", "")
        )
    req.raw_input = history[0].get("content", "")
    req.clarification_rounds = max_rounds
    return req


# ====================================================================
# _refine_single_pass() — 单次调用（experiment 模式）
# ====================================================================
def _refine_single_pass(
    raw_input: str,                                      # 用户的完整输入文本
    temperature: float,                                  # LLM 温度
) -> StructuredRequirement:
    """
    实验模式：一次 LLM 调用直接出结果。
    不反问，缺失信息用合理默认值填充。

    为什么实验模式用这个：
      批量跑 360 次实验时不能用 input() 等用户回答，
      必须一句话出结果。所以输入要写得足够完整。
    """
    prompt = (
        "根据以下用户需求，直接生成结构化需求 JSON。"
        "不需要反问，缺失信息用合理默认值填充。\n\n"
        f"用户需求: {raw_input}\n\n"
        f"JSON Schema:\n"
        f"{json.dumps(StructuredRequirement.model_json_schema(), ensure_ascii=False, indent=2)}"
    )
    result = chat([user_msg(prompt)], temperature=temperature, max_tokens=2048)
    try:
        req = StructuredRequirement.model_validate_json(extract_json(result))
    except Exception:
        req = StructuredRequirement(component_type="未知", raw_input=raw_input)
    req.raw_input = raw_input
    return req
