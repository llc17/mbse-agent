# -*- coding: utf-8 -*-
"""
=============================================================================
node2_sysml.py — 节点2：SysML v2 代码生成
=============================================================================

这个节点把 StructuredRequirement 转化为 SysML v2 文本代码。

SysML v2 是什么：
  - OMG（国际对象管理组织）发布的系统建模语言
  - 用文本语法描述系统结构：part def（部件定义）、port（端口）、connect（连接）
  - .sysml 文件可以用 Eclipse Pilot Implementation 打开，渲染成图形

V2 改进：
  - prompt 参考了官方 SysML-v2-Release 仓库的 Vehicle Example
  - 包含 RC 滤波器 + 热传导两个域的标准示例
  - 语法检查 + 最多 2 次重试

LangGraph 节点函数：
  node2_generate(state) → 返回 {"sysml": ..., "timing": ...}
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
# node2_generate() — LangGraph 节点函数（入口）
# ====================================================================
def node2_generate(state: dict) -> dict:
    """
    主图调用的节点函数。从 state 提取 req，调用 LLM 生成 SysML，写回。

    参数 & 返回值同 node1_refine。
    """
    t0 = time.time()                                     # 计时开始

    # ---- 从 state 提取数据 ----
    req_dict = state.get("req", {})                      # 节点1 产出的需求字典
    req = StructuredRequirement(**req_dict)              # ** 把字典解包为关键字参数
    # 等价于: StructuredRequirement(component_type="...", parameters={...}, ...)

    run_dir = Path(state.get("run_dir", "."))            # 运行输出目录
    sysml_dir = run_dir / "sysml"                        # SysML 文件子目录
    temperature = state.get("temperature", 0.3)
    feedback = state.get("human_feedback", "")           # 用户打回时的反馈（可能为空）

    # ---- 调用生成逻辑 ----
    artifact = _generate(req, sysml_dir, temperature, feedback)

    elapsed = time.time() - t0
    logger.info("节点2 完成 (%.1fs), 尝试=%s, 错误=%s",
                elapsed, artifact.attempts, len(artifact.errors))

    return {
        "sysml": artifact.model_dump(),
        "timing": {**state.get("timing", {}), "node2": elapsed},
    }


# ====================================================================
# _generate() — 核心生成逻辑
# ====================================================================
def _generate(
    req: StructuredRequirement,
    sysml_dir: Path,
    temperature: float,
    feedback: str = "",
    max_retries: int = 2,                                # 最多重试 2 次
) -> SysMLArtifact:
    """
    生成 SysML v2 代码，含基本语法检查和重试。

    流程:
      第 1 次: LLM 生成 → 语法检查
        ├─ 通过 → 保存文件 → 返回
        └─ 失败 → 第 2 次: LLM 带着错误信息重新生成 → 语法检查
           ├─ 通过 → 保存 → 返回
           └─ 失败 → 使用最后一次结果（即使有语法警告）
    """
    # ---- 准备 prompt 变量 ----
    params_str = "\n".join(                              # 把参数字典转成多行文本
        f"  {k} = {v}" for k, v in req.parameters.items()
    )                                                    # 例如 "  R = 1000\n  C = 1e-6"

    constraints_str = "\n".join(                         # 约束列表转多行
        f"  - {c}" for c in req.constraints
    )                                                    # 例如 "  - 截止频率约1kHz"

    artifact = SysMLArtifact()                           # 创建空的产物对象
    prev_errors: list[str] = []                          # 记录前一次尝试的错误

    for attempt in range(1, max_retries + 1):            # attempt = 1, 2

        # ---- 构造错误反馈段落 ----
        prev_error_section = ""
        if prev_errors:
            prev_error_section = (
                f"\n## 上次生成的错误\n"
                + "\n".join(f"- {e}" for e in prev_errors)
            )
        if feedback:                                     # 如果有用户打回反馈，也加进去
            prev_error_section += f"\n## 用户反馈\n{feedback}"

        # ---- 构造 prompt ----
        prompt = (
            load_prompt("node2_sysml.txt")               # 加载模板文件（含官方示例）
            .replace("{component_type}", req.component_type)
            .replace("{component_name}", req.component_name or req.component_type)
            # req.component_name or req.component_type:
            #   如果 name 为空，用 type 代替（Python 的 or 返回第一个真值）
            .replace("{parameters}", params_str)
            .replace("{topology}", req.topology)
            .replace("{constraints}", constraints_str)
            .replace("{parameters_R}", str(req.parameters.get("R", 1000)))
            # 单独替换 R 和 C 的值（用于 few-shot 示例中的默认值）
            .replace("{parameters_C}", str(req.parameters.get("C", 1e-6)))
            .replace("{prev_error_section}", prev_error_section)
        )

        # ---- 调用 LLM ----
        logger.info("节点2 第%s次生成...", attempt)
        sysml_code = chat(
            [user_msg(prompt)],
            temperature=temperature,
            max_tokens=4096
        ).strip()

        # ---- 清洗 + 保存 ----
        sysml_code = clean_code_block(sysml_code, "sysml")
        artifact.sysml_code = sysml_code
        artifact.attempts = attempt

        # ---- 语法检查 ----
        errors = _syntax_check(sysml_code)
        if errors:
            logger.warning("节点2 第%s次语法警告: %s", attempt, errors)
            prev_errors = errors
            artifact.errors = errors
            continue                                     # continue: 跳到下一次循环 → 重试

        logger.info("节点2 第%s次生成成功", attempt)
        artifact.errors = []
        break                                            # break: 跳出循环 → 生成成功
    else:
        # ---- for...else: 循环没有被 break 打断时执行 ----
        # 也就是说：max_retries 次全部失败，走到这里
        logger.warning("节点2 %s次重试后仍有问题，使用最后一次结果", max_retries)

    # ---- 保存文件 ----
    sysml_dir.mkdir(parents=True, exist_ok=True)         # 确保目录存在
    file_path = sysml_dir / "model.sysml"                # 固定文件名 model.sysml
    file_path.write_text(artifact.sysml_code, encoding="utf-8")
    # write_text: Path 的方法，一次性写入文本文件
    artifact.file_path = str(file_path)                  # 记录路径（字符串格式）

    return artifact


# ====================================================================
# _syntax_check() — 基本语法检查
# ====================================================================
def _syntax_check(code: str) -> list[str]:
    """
    对 SysML v2 代码做简单的语法检查。

    检查项:
      1. 是否包含 package 关键字（SysML v2 必须用 package 包裹）
      2. 花括号 { } 数量是否匹配（每个 { 必须有对应的 }）
      3. 是否包含 part 定义（系统建模必须有部件）

    注意:
      这只是基础正则检查，不是真正的语法解析。
      V7 会引入 ANTLR 做严格的 SysML v2 语法检查。
    """
    errors = []

    if "package" not in code.lower():                    # .lower() 把代码转小写再检查
        errors.append("缺少 package 声明")               #   不区分大小写

    if code.count("{") != code.count("}"):               # str.count(): 统计字符出现次数
        errors.append("花括号不匹配")                     #   { 和 } 数量必须相等

    if "part def" not in code and "part " not in code.lower():
        errors.append("缺少 part 定义")                  #   part def 是 SysML v2 部件定义关键字

    return errors                                        # 返回错误列表（空 = 通过）
