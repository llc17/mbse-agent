# -*- coding: utf-8 -*-
"""
=============================================================================
node2_sysml.py — 节点2：SysML v2 代码生成
=============================================================================

把 StructuredRequirement → LLM 生成 SysML v2 文本代码。

SysML v2 = OMG 发布的系统建模语言（文本语法）。
用 Eclipse Pilot Implementation 可以打开 .sysml 文件，渲染成图形。

V1 特点:
  - 手写的 RC 滤波器 few-shot 示例（非官方）
  - 基本语法检查（package + 花括号 + part 定义）
  - 最多 2 次重试
  - 不自动渲染（用户手动 Eclipse 看图）
"""

# ====================================================================
# 导入
# ====================================================================

import os                                                # 路径操作
from pathlib import Path                                 # 跨平台路径

from src.llm_client import chat, user_msg
from src.schemas import StructuredRequirement, SysMLArtifact


# ====================================================================
# _load_prompt() — 加载 prompt 模板
# ====================================================================
def _load_prompt(name: str) -> str:
    path = os.path.join(os.path.dirname(__file__), "..", "prompts", name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ====================================================================
# generate_sysml() — 节点2 主入口
# ====================================================================
def generate_sysml(
    req: StructuredRequirement,                           # 节点1 产出的需求
    sysml_dir: Path,                                     # 输出目录（Path 对象）
    max_retries: int = 2,                                # 最多重试 2 次
) -> SysMLArtifact:
    """
    根据结构化需求生成 SysML v2 代码。

    流程:
      第 1 次: LLM 生成 → 语法检查
        ├─ 通过 → 保存文件 → 返回
        └─ 失败 → 第 2 次: LLM 带着错误信息重新生成

    prompt 模板中的占位符:
      {component_type}  → "RC低通滤波器"
      {parameters}      → "  R = 1000\n  C = 1e-6"
      {topology}        → "串联RC"
      {constraints}     → "  - 截止频率约1kHz"
    """
    # ---- 步骤 1: 准备 prompt 变量 ----
    params_str = "\n".join(                              # 把参数字典转成多行字符串
        f"  {k} = {v}" for k, v in req.parameters.items()
    )                                                    # .items() 返回 (键, 值) 对

    constraints_str = "\n".join(                         # 约束列表转多行
        f"  - {c}" for c in req.constraints
    )

    artifact = SysMLArtifact(source_requirement=req)     # 创建空的产物对象，记录来源
    prev_errors: list[str] = []                          # 记录前一次尝试的错误

    # ---- 步骤 2: 重试循环 ----
    for attempt in range(1, max_retries + 1):            # attempt = 1, 2

        # ---- 构造错误反馈段落 ----
        prev_error_section = ""
        if prev_errors:                                  # 如果之前有错误
            prev_error_section = (
                f"\n## 上次生成的错误（请修正）\n"
                f"{chr(10).join(f'- {e}' for e in prev_errors)}"
                # chr(10) = '\n'（换行符）
            )

        # ---- 构造完整 prompt ----
        prompt = (
            _load_prompt("node2_sysml.txt")              # 加载模板（含手写 RC few-shot）
            .replace("{component_type}", req.component_type)
            .replace("{component_name}", req.component_name or req.component_type)
            .replace("{parameters}", params_str)
            .replace("{topology}", req.topology)
            .replace("{constraints}", constraints_str)
            .replace("{parameters_R}", str(req.parameters.get("R", 1000)))
            # .get("R", 1000): 取 R 的值，不存在用 1000
            .replace("{parameters_C}", str(req.parameters.get("C", 1e-6)))
            .replace("{prev_error_section}", prev_error_section)
        )

        # ---- 调 LLM ----
        print(f"[节点2] 第{attempt}次尝试生成 SysML...")
        sysml_code = chat(
            [user_msg(prompt)],
            temperature=0.2,                             # 低温度，保证格式稳定
            max_tokens=4096,
        ).strip()

        # ---- 清洗 markdown 代码块 ----
        sysml_code = _clean_code_block(sysml_code, "sysml")

        # ---- 保存产物 ----
        artifact.sysml_code = sysml_code
        artifact.attempts = attempt

        # ---- 语法检查 ----
        errors = _basic_syntax_check(sysml_code)
        if errors:                                       # 如果检查有问题
            print(f"[节点2] 第{attempt}次有语法警告: {errors}")
            prev_errors = errors                         # 记录错误，下次重试时回喂
            artifact.errors = errors
            continue                                     # continue = 回到循环开头，重试

        # ---- 通过！ ----
        print(f"[节点2] 第{attempt}次生成成功。")
        break                                            # break = 跳出循环
    else:
        # ---- for...else: 循环没有被 break 打断时执行 ----
        # 也就是：max_retries 次全失败了
        print(f"[节点2] {max_retries}次重试后仍存在问题，使用最后一次结果。")

    # ---- 保存文件到磁盘 ----
    file_path = sysml_dir / "model.sysml"                # Path / 字符串 = 拼接路径
    file_path.write_text(artifact.sysml_code, encoding="utf-8")
    # write_text: Path 的方法，一次性写入文本文件
    artifact.file_path = str(file_path)                  # 转为字符串保存

    return artifact


# ====================================================================
# _clean_code_block() — 去掉 LLM 返回的 ``` 包裹
# ====================================================================
def _clean_code_block(text: str, lang: str) -> str:
    """
    LLM 经常返回:
      ```sysml
      package xxx { ... }
      ```

    这个函数剥掉外层标记，只保留代码。
    lang 参数只为了和 V1 接口一致，实际没用。
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]                                # 去掉第一行（```sysml）
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]                           # 去掉最后一行（```）
        text = "\n".join(lines)
    return text


# ====================================================================
# _basic_syntax_check() — 基本语法检查
# ====================================================================
def _basic_syntax_check(code: str) -> list[str]:
    """
    简单的语法检查：

    1. 是否包含 "package" 关键字（SysML v2 必须用 package 包裹）
    2. 花括号 { } 数量是否匹配
    3. 是否包含 "part" 定义（系统建模必须有部件）

    注意: 这不是 ANTLR 严格检查，只是正则级别的快速筛查。
          V7 会引入真正的 SysML v2 语法解析器。
    """
    errors = []

    if "package" not in code.lower():                    # .lower() 转小写
        errors.append("缺少 package 声明")

    if code.count("{") != code.count("}"):               # str.count() 统计出现次数
        errors.append("花括号不匹配")

    if "part def" not in code and "part " not in code.lower():
        errors.append("缺少 part 定义")

    return errors                                        # 返回错误列表（空 = 通过）
