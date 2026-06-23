# -*- coding: utf-8 -*-
"""
=============================================================================
node4_summary.py — 节点4：总结生成
=============================================================================

这个节点是整个流水线的终点。它的任务是把前面 3 个节点的产出物汇总，
找 LLM 写一份人类可读的 Markdown 总结报告。

输入:
  - StructuredRequirement（需求 JSON）
  - SysMLArtifact（.sysml 代码，只取前 1000 字符）
  - ModelicaArtifact（.mo 代码 + 仿真成功/失败状态 + 错误记录）

输出:
  - SummaryArtifact（summary.md 文件）
"""

# ====================================================================
# 导入
# ====================================================================

import time
import logging
from pathlib import Path

from src.llm_client import chat, user_msg
from src.schemas import (
    StructuredRequirement,
    SysMLArtifact,
    ModelicaArtifact,
    SummaryArtifact,
)

logger = logging.getLogger("node4")


# ====================================================================
# node4_summary() — LangGraph 节点函数
# ====================================================================
def node4_summary(state: dict) -> dict:
    """
    主图调用的节点函数。汇总全流程产物，生成 summary.md。
    """
    t0 = time.time()                                     # 计时开始

    # ---- 从 state 重建 Pydantic 对象 ----
    # 注意 state 中存的是字典（因为 LangGraph 要求 JSON 可序列化）
    # 用 ** 解包把字典转回 Pydantic 对象
    req = StructuredRequirement(**state.get("req", {}))
    sysml = SysMLArtifact(**state.get("sysml", {}))
    mo = ModelicaArtifact(**state.get("mo", {}))
    # 也可以用 SysMLArtifact.model_validate(state["sysml"]) —— 一样的

    results_dir = Path(state.get("run_dir", ".")) / "results"
    temperature = state.get("temperature", 0.3)

    # ---- 构造 prompt：把所有产物喂给 LLM ----
    prompt = f"""你是一个系统工程报告撰写人。请根据全流程产出，写一份简洁的系统设计总结报告。

## 需求
{req.model_dump_json(indent=2)}
# model_dump_json(): 把 Pydantic 对象序列化为格式化的 JSON 字符串

## SysML v2 代码（节选前 1000 字符）
{sysml.sysml_code[:1000]}
# 只取前 1000 字符——防止 prompt 超 token 限制

## Modelica 代码（节选前 1000 字符）
{mo.modelica_code[:1000]}

## 仿真结果
- 编译+仿真成功: {mo.success}
- 尝试次数: {mo.attempts}
- 错误记录: {
    chr(10).join(f'  - {e[:100]}' for e in mo.errors)  # chr(10) = 换行符 \n
    if mo.errors else '无'
}

## 要求
写一份 Markdown 格式的总结，包含：
1. 项目概述（一句话）
2. 系统参数（表格）
3. 架构说明（基于 SysML v2 的关键部件和连接）
4. 仿真结果（成功/失败，关键变量名）
5. 反思与下一步

总字数 500 字以内。直接输出 Markdown。"""

    # ---- 调用 LLM 生成总结 ----
    logger.info("节点4 生成总结...")
    summary_text = chat(
        [user_msg(prompt)],
        temperature=temperature,
        max_tokens=2048                                   # 500 字中文 ≈ 1000 tokens，留足空间
    )

    # ---- 保存到文件 ----
    results_dir.mkdir(parents=True, exist_ok=True)
    file_path = results_dir / "summary.md"
    file_path.write_text(summary_text, encoding="utf-8")

    # ---- 构造产物 ----
    artifact = SummaryArtifact(
        summary_text=summary_text,
        file_path=str(file_path),
        requirement_path=str(results_dir / "requirement.json"),
        sysml_path=sysml.file_path,
        modelica_path=mo.file_path,
        plot_path=mo.plot_path,
    )

    elapsed = time.time() - t0
    logger.info("节点4 完成 (%.1fs)", elapsed)

    return {
        "summary": artifact.model_dump(),
        "timing": {**state.get("timing", {}), "node4": elapsed},
    }
