"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  节点 1 — 需求解析                                                          ║
║                                                                            ║
║  输入：用户自然语言（如 "做个1kHz低通滤波器"）                                ║
║  输出：StructuredRequirement（结构化数据）                                    ║
║                                                                            ║
║  流程（for 循环，最多10轮）：                                                 ║
║    ① LLM 看对话历史 → 判完整性({is_complete, missing_fields})                 ║
║    ② 如果完整 → 从历史提取 StructuredRequirement → return                    ║
║    ③ 如果不完整 → LLM 生成反问 → print 给用户 → input() 等回答                ║
║    ④ 反问+回答追加到 history → 回到①                                         ║
║                                                                            ║
║  下游消费者：node2_sysml, node3_modelica（读 req.parameters 等字段）          ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import json  # 标准库：解析 LLM 返回的 JSON

from src.llm_client import chat, user_msg, assistant_msg, system_msg
#                        └────┬────┘  └──────┬──────┘  └─────────┘
#                     调 LLM 的核心函数   构造消息字典的便利函数

from src.schemas import StructuredRequirement
#                       └────── 最终要产出的数据结构 ──────┘


# ===== 辅助函数：从磁盘读 prompt 模板 =====
def _load_prompt(name: str) -> str:         # 输入文件名，返回模板文本字符串
    import os                               # 局部导入，只在函数内用
    path = os.path.join(                    # 拼接文件路径
        os.path.dirname(__file__),          # 当前文件所在目录 = src_annotated/
        "..",                               # 上一级 = week2/
        "prompts",                          # prompts/ 目录
        name                                # 文件名，如 "node1_completeness.txt"
    )
    with open(path, "r", encoding="utf-8") as f:  # 打开文件，utf-8 编码
        return f.read()                     # 读出全部文本 → 返回


# ===== 核心函数：多轮对话精炼需求 =====
def refine_requirement(
    raw_input: str,                         # 用户第一句话
    max_rounds: int = 10                    # 最多反问10轮
) -> StructuredRequirement:                 # 返回结构化需求对象
    """把自然语言精炼为 StructuredRequirement。"""

    # 第28行：建立对话历史，初始只含用户第一句话
    history: list[dict] = [user_msg(raw_input)]
    #    └──┬──┘         └────┬────┘
    #  类型注解：字典列表   构造 {"role":"user","content":"做个低通"}

    # 第30行：主循环，从第1轮到第10轮
    for round_num in range(1, max_rounds + 1):  # range(1,11) → 1,2,3...10

        # ===== 步骤①：LLM 检查完整性 =====
        completeness_prompt = (               # 拼最终发给 LLM 的提示词
            _load_prompt("node1_completeness.txt")     # 从磁盘读模板
            .replace(                         # 替换占位符
                "{dialogue_history}",         # 模板里的占位符
                _format_history(history)      # 格式化的对话文本
            )
        )
        result = chat(                        # 调 DeepSeek
            [user_msg(completeness_prompt)],  # 包装成 messages 格式
            temperature=0.1,                  # 最低温度→尽量确定，判断 yes/no 不需要创造力
            max_tokens=512                    # 返回短 JSON，不需要很多 token
        )

        # 解析 LLM 返回的 JSON
        try:                                  # try = 尝试执行，可能出错
            completeness = json.loads(        # JSON 字符串 → Python dict
                _extract_json(result)         # 先清洗掉可能的 ```markdown``` 包裹
            )
        except json.JSONDecodeError:          # JSON 解析失败（LLM 没按格式返回）
            completeness = {                  # 给个默认值，当不完整处理
                "is_complete": False,
                "missing_fields": ["JSON解析失败"],
                "suggestions": []
            }

        # ===== 步骤②：信息够了 → 提取并返回 =====
        if completeness.get("is_complete"):   # dict.get() 取键，不存在返 None
            final_prompt = (                  # 拼提取 prompt
                f"根据以下对话内容，提取系统需求的结构化信息。\n\n"
                f"对话历史：\n{_format_history(history)}\n\n"
                f"返回 JSON Schema：\n"
                f"{json.dumps(StructuredRequirement.model_json_schema(), ensure_ascii=False, indent=2)}"
            )
            final_result = chat(              # 调 LLM 提取
                [user_msg(final_prompt)],
                temperature=0.2,              # 低温度 → 准确提取
                max_tokens=2048               # 完整 JSON 需要更多 token
            )
            req = StructuredRequirement.model_validate_json(  # JSON → Pydantic 对象（自动校验）
                _extract_json(final_result)   # 清洗 markdown 包裹
            )
            req.raw_input = raw_input         # 覆盖原始输入（LLM 可能没正确填入）
            req.clarification_rounds = round_num - 1  # 记录反问了几轮
            print(f"\n[节点1] 需求完整，{round_num - 1}轮精炼完成。")
            return req                        # ← 函数结束，req 传到 main.py 的变量里

        # ===== 步骤③：信息不够 → LLM 生成反问 =====
        missing_str = "\n".join(              # 把缺失字段拼成字符串
            f"- {m}" for m in completeness.get("missing_fields", [])
        )
        question = (                          # 构造反问指令
            f"根据当前已知信息，还缺少: {missing_str}。"
            f"请用中文友好地向用户提问，一次只问1-2个最重要的点，给具体选项。"
        )
        clarify_msg = chat(                   # 调 LLM 生成自然反问
            [system_msg(                      # system 消息 = 给 LLM 的角色指令
                "你是系统需求分析师，与用户对话。"
                "请根据以下对话历史，生成一句友好的反问。"
                f"\n\n对话历史：\n{_format_history(history)}"
                f"\n\n{question}"
            )],
            temperature=0.5,                  # 中等温度 → 反问要自然口语化
            max_tokens=256                    # 一句话够用
        ).strip()                            # 去首尾空白

        print(f"\n[节点1] 第{round_num}轮: {clarify_msg}")  # 显示反问给用户

        # ===== 步骤④：等用户回答 =====
        user_answer = input("\n你的回答: ").strip()  # 程序暂停，等用户打字
        if not user_answer:                          # 用户直接按回车
            user_answer = "不需要补充，用已有信息即可。"

        # ===== 步骤⑤：追加到历史，下一轮 LLM 能看到 =====
        history.append(assistant_msg(clarify_msg))   # 分析师的提问
        history.append(user_msg(user_answer))         # 用户的回答

    # 第82行：循环结束（10轮还没够）→ 强制提取
    print(f"\n[节点1] 达到最大轮数 {max_rounds}，用最后状态。")
    final_prompt = (                       # 同上面的提取逻辑
        f"根据以下对话内容，提取系统需求的结构化信息。\n\n"
        f"对话历史：\n{_format_history(history)}\n\n"
        f"返回 JSON Schema：\n"
        f"{json.dumps(StructuredRequirement.model_json_schema(), ensure_ascii=False, indent=2)}"
    )
    final_result = chat([user_msg(final_prompt)], temperature=0.2, max_tokens=2048)
    try:                                   # 最后尝试解析
        req = StructuredRequirement.model_validate_json(_extract_json(final_result))
    except Exception:                      # 任何错误 → 返回空壳
        req = StructuredRequirement(component_type="未知", raw_input=raw_input)
    req.raw_input = raw_input
    req.clarification_rounds = max_rounds
    return req


# ===== 辅助函数：去掉 LLM 可能加的 ```json ... ``` =====
def _extract_json(text: str) -> str:       # 输入可能有 markdown 包裹的文本
    text = text.strip()                    # 去首尾空白
    if text.startswith("```"):             # LLM 包在代码块里了
        lines = text.split("\n")           # 按行切
        if lines[0].startswith("```"):     # 去掉第一行 ```
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":  # 如果最后一行是 ```
            lines = lines[:-1]             # 去掉最后一行
        text = "\n".join(lines)            # 重新拼回
    return text


# ===== 辅助函数：对话字典列表 → 人类可读文本 =====
def _format_history(history: list[dict]) -> str:
    lines = []                             # 存放每一行
    for msg in history:                    # 遍历每条消息
        role = "用户" if msg["role"] == "user" else "分析师"  # 翻译角色名
        lines.append(f"{role}: {msg['content'][:300]}")       # 截前300字
    return "\n".join(lines)               # 用换行符拼接


# ╔══════════════════════════════════════════════════════════════╗
# ║  数据流追踪（以"做个1kHz低通"为例）：                          ║
# ║                                                              ║
# ║  main.py:                                                    ║
# ║    raw_input = input()  ← 用户打字                             ║
# ║    req = refine_requirement(raw_input)                        ║
# ║            │                                                 ║
# ║            ▼                                                 ║
# ║  node1: history = [user_msg("做个1kHz低通")]                   ║
# ║         第1轮: LLM → {is_complete: false, missing:["R值"]}     ║
# ║              反问: "电阻值多少？"                               ║
# ║              用户答: "1kΩ串联"                                  ║
# ║              history追加2条                                    ║
# ║         第2轮: LLM → {is_complete: true}                       ║
# ║              提取 → StructuredRequirement(                    ║
# ║                component_type="RC低通滤波器",                   ║
# ║                parameters={"R":1000, "cutoff_freq":1592})      ║
# ║              return req ──→ 回到 main.py                       ║
# ║                                                              ║
# ║  main.py 继续:                                                ║
# ║    sysml = generate_sysml(req, ...)  ← req 传给节点2           ║
# ╚══════════════════════════════════════════════════════════════╝
