# -*- coding: utf-8 -*-
"""
=============================================================================
node2_sysml.py — 节点 2：SysML v2 代码生成（V3 保持不变）
=============================================================================

这个节点读 StructuredRequirement，调 LLM 生成 OMG SysML v2 文本代码。
V3 对此文件**零修改**——代码逻辑完全继承 V2。

为什么不动 node2？
  V3 的交叉校验（Q_cross_validate）是独立节点插入到 node2 之后，
  不修改 node2 本身的生成逻辑。这是"关注点分离"：
    - node2 只管生成
    - Q_cross_validate 只管校验
  两者独立调试、独立开关。

V3 对 node2 唯一的改动在 prompt 模板 node2_sysml.txt：
  规则 6：doc 注释中标注参数来源（追溯链）
  "在 doc 注释中标注参数来源，例：doc // R=1000 来源: 需求参数 resistance"

工作流程（与 V2 完全相同）：
  1. 读 req（StructuredRequirement）
  2. 构造 prompt → LLM 生成 → 清洗代码块
  3. 基本语法检查（package/花括号/part def）
  4. 失败则重试（最多 2 次），用上次错误信息作为修复提示
  5. 保存 model.sysml → 写回 state
"""

# ====================================================================
# 导入
# ====================================================================
import time
import logging
from pathlib import Path

from src.llm_client import chat, user_msg
from src.schemas import StructuredRequirement, SysMLArtifact
from src.utils import load_prompt, clean_code_block

logger = logging.getLogger("node2")


# ====================================================================
# LangGraph 节点入口 — 被 pipeline.py 调用
# ====================================================================

def node2_generate(state: dict) -> dict:
    """
    LangGraph 节点函数：从结构化需求生成 SysML v2 代码。

    输入：
        state.req — StructuredRequirement 的 dict
        state.human_feedback — 如被打回，包含用户反馈
        state.temperature
        state.run_dir

    输出（写回 state）：
        sysml — SysMLArtifact 的 dict
        timing.node2 — 本节点耗时
    """
    t0 = time.time()
    req_dict = state.get("req", {})
    req = StructuredRequirement(**req_dict)
    run_dir = Path(state.get("run_dir", "."))
    sysml_dir = run_dir / "sysml"
    temperature = state.get("temperature", 0.3)
    feedback = state.get("human_feedback", "")

    artifact = _generate(req, sysml_dir, temperature, feedback)
    elapsed = time.time() - t0
    logger.info("节点2 完成 (%.1fs), 尝试=%s, 错误=%s",
                elapsed, artifact.attempts, len(artifact.errors))

    return {
        "sysml": artifact.model_dump(),
        "timing": {**state.get("timing", {}), "node2": elapsed},
    }


# ====================================================================
# 核心生成逻辑
# ====================================================================
# 最多重试 2 次。每次重试时把上次的语法错误嵌入 prompt，
# 让 LLM 有上下文修复。

def _generate(
    req: StructuredRequirement,
    sysml_dir: Path,
    temperature: float,
    feedback: str = "",
    max_retries: int = 2,
) -> SysMLArtifact:
    """生成 SysML v2 代码，含基本语法检查重试"""

    # ---- 格式化参数和约束 ----
    params_str = "\n".join(f"  {k} = {v}" for k, v in req.parameters.items())
    constraints_str = "\n".join(f"  - {c}" for c in req.constraints)

    artifact = SysMLArtifact()
    prev_errors: list[str] = []

    for attempt in range(1, max_retries + 1):
        # ---- 构造错误提示段落 ----
        prev_error_section = ""
        if prev_errors:
            prev_error_section = (
                f"\n## 上次生成的错误\n"
                + "\n".join(f"- {e}" for e in prev_errors)
            )
        if feedback:
            # 用户打回或交叉校验失败的反馈
            prev_error_section += f"\n## 用户反馈\n{feedback}"

        # ---- 用 prompt 模板构造完整 prompt ----
        # node2_sysml.txt 包含完整的 SysML v2 语法规范和 RC/热域参考示例
        prompt = (
            load_prompt("node2_sysml.txt")
            .replace("{component_type}", req.component_type)
            .replace("{component_name}", req.component_name or req.component_type)
            .replace("{parameters}", params_str)
            .replace("{topology}", req.topology)
            .replace("{constraints}", constraints_str)
            .replace("{parameters_R}", str(req.parameters.get("R", 1000)))
            .replace("{parameters_C}", str(req.parameters.get("C", 1e-6)))
            .replace("{prev_error_section}", prev_error_section)
        )

        logger.info("节点2 第%s次生成...", attempt)
        sysml_code = chat([user_msg(prompt)], temperature=temperature, max_tokens=4096).strip()
        sysml_code = clean_code_block(sysml_code, "sysml")

        artifact.sysml_code = sysml_code
        artifact.attempts = attempt

        # ---- 语法检查 ----
        errors = _syntax_check(sysml_code)
        if errors:
            logger.warning("节点2 第%s次语法警告: %s", attempt, errors)
            prev_errors = errors
            artifact.errors = errors
            continue                                      # 重试

        logger.info("节点2 第%s次生成成功", attempt)
        artifact.errors = []
        break
    else:
        # for-else: 所有重试都失败 → 用最后一次结果继续
        logger.warning("节点2 %s次重试后仍有问题，使用最后一次结果", max_retries)

    # ---- 保存到文件 ----
    sysml_dir.mkdir(parents=True, exist_ok=True)
    file_path = sysml_dir / "model.sysml"
    file_path.write_text(artifact.sysml_code, encoding="utf-8")
    artifact.file_path = str(file_path)
    return artifact


# ====================================================================
# 语法检查 — 轻量级正则检查
# ====================================================================
# 这不是 ANTLR 标准解析（V8 才做），只是三个基本检查：
#   1. 有 package 声明
#   2. 花括号匹配
#   3. 有 part 定义
#
# 限制：无法检测类型错误、端口连接语义错误等深层问题。
#       这些在 node3 编译阶段才会暴露（编译错误→repair 循环）。

def _syntax_check(code: str) -> list[str]:
    """基本语法检查——轻量级，仅检测最明显的错误"""
    errors = []
    if "package" not in code.lower():
        errors.append("缺少 package 声明")
    if code.count("{") != code.count("}"):
        errors.append("花括号不匹配")
    if "part def" not in code and "part " not in code.lower():
        errors.append("缺少 part 定义")
    return errors
