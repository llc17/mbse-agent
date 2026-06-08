"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  节点 2 — SysML v2 代码生成                                                  ║
║                                                                            ║
║  输入：StructuredRequirement（从节点1来）                                     ║
║  输出：SysMLArtifact（.sysml 文件路径 + 代码内容）                             ║
║                                                                            ║
║  流程（while 循环，最多重试2次）：                                             ║
║    ① LLM 根据需求生成 SysML v2 代码                                          ║
║    ② 基本语法检查（有没有 package？花括号匹配？）                               ║
║    ③ 有错误 → 错误回喂 prompt → 重试（回到①）                                 ║
║    ④ 无错误 → 保存 .sysml 文件 → return                                      ║
║                                                                            ║
║  下游消费者：node3_modelica（读 sysml_code），用户（Eclipse 看图）             ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import os  # 标准库：拼接文件路径
from pathlib import Path  # 标准库：Path 对象表示文件路径

from src.llm_client import chat, user_msg  # 调 LLM
from src.schemas import StructuredRequirement, SysMLArtifact  # 输入/输出 Schema


# ===== 辅助函数：读 prompt 模板 =====
def _load_prompt(name: str) -> str:
    path = os.path.join(os.path.dirname(__file__), "..", "prompts", name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ===== 核心函数：生成 SysML v2 =====
def generate_sysml(
    req: StructuredRequirement,            # 节点1的产出 — 上游数据
    sysml_dir: Path,                       # 保存目录，如 outputs/run_xxx/sysml/
    max_retries: int = 2,                  # 最多重试2次
) -> SysMLArtifact:                        # 返回节点2的产出
    """根据需求生成 .sysml 文件。"""

    # 第30-31行：把参数和约束格式化为字符串，供 prompt 使用
    params_str = "\n".join(                # 用换行符连接
        f"  {k} = {v}" for k, v in req.parameters.items()
    )                                      # → "  R = 1000\n  C = 1e-6"
    constraints_str = "\n".join(           # 约束列表同理
        f"  - {c}" for c in req.constraints
    )                                      # → "  - 截止频率约1kHz"

    artifact = SysMLArtifact(source_requirement=req)  # 创建产出对象，关联来源需求
    prev_errors: list[str] = []            # 上一轮的错误记录（第1轮为空）

    # 第36行：while 循环 = 自修复核心
    for attempt in range(1, max_retries + 1):  # attempt = 1, 2

        # === 构建 prompt：如果有上次错误，拼入修正提示 ===
        prev_error_section = ""            # 默认无错误提示
        if prev_errors:                    # 第2次才有内容
            prev_error_section = (         # 拼错误回喂段落到 prompt
                f"\n## 上次生成的错误（请修正）\n"
                f"{chr(10).join(f'- {e}' for e in prev_errors)}"
            )                              # chr(10) = "\n" 换行符

        prompt = (                         # 拼最终 prompt
            _load_prompt("node2_sysml.txt")      # 从磁盘读模板
            .replace("{component_type}", req.component_type)     # 填组件类型
            .replace("{component_name}", req.component_name or req.component_type)
            .replace("{parameters}", params_str)                 # 填参数
            .replace("{topology}", req.topology)                 # 填拓扑
            .replace("{constraints}", constraints_str)           # 填约束
            .replace("{parameters_R}", str(req.parameters.get("R", 1000)))
            .replace("{parameters_C}", str(req.parameters.get("C", 1e-6)))
            .replace("{prev_error_section}", prev_error_section) # 填错误提示
        )

        print(f"[节点2] 第{attempt}次尝试生成 SysML...")
        sysml_code = chat(                 # 调 LLM 生成代码
            [user_msg(prompt)],
            temperature=0.2,               # 低温度 → 代码生成要精确
            max_tokens=4096                # 代码可能很长
        ).strip()

        sysml_code = _clean_code_block(sysml_code, "sysml")  # 去掉 ```sysml ... ```
        artifact.sysml_code = sysml_code   # 填入产出对象
        artifact.attempts = attempt        # 记录第几次尝试

        # === 基本语法检查 ===
        errors = _basic_syntax_check(sysml_code)  # 返回错误列表，如 ["缺少package声明"]
        if errors:                         # 有错误
            print(f"[节点2] 第{attempt}次有语法警告: {errors}")
            prev_errors = errors           # 保存错误 → 下一轮回喂 LLM
            artifact.errors = errors       # 记录到产出对象
            continue                       # ← 跳回 for 循环开头，重试
            #                                (continue 配合 for 循环，不是 while)

        print(f"[节点2] 第{attempt}次生成成功。")
        break                              # 成功 → 跳出循环
    else:
        # for...else: 循环正常结束（没用 break）时执行
        # 即 max_retries 次全失败
        print(f"[节点2] {max_retries}次重试后仍存在问题，使用最后一次结果。")

    # === 保存到磁盘 ===
    file_path = sysml_dir / "model.sysml"  # Path 对象用 / 拼接路径
    file_path.write_text(                  # 写文本文件
        artifact.sysml_code,               # 写入的内容
        encoding="utf-8"                   # 编码
    )
    artifact.file_path = str(file_path)    # 记录保存路径 → 产出对象

    return artifact                        # ← 返回到 main.py


# ===== 辅助函数：去掉 markdown 代码块 =====
def _clean_code_block(text: str, lang: str) -> str:
    text = text.strip()
    if text.startswith("```"):             # LLM 包在 ```sysml ... ``` 里
        lines = text.split("\n")
        lines = lines[1:]                  # 去掉第一行 ```sysml
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]             # 去掉最后一行 ```
        text = "\n".join(lines)
    return text


# ===== 辅助函数：简单语法校验 =====
def _basic_syntax_check(code: str) -> list[str]:
    """检查 SysML v2 的基本语法。不是完整解析器，只是快速检查。"""
    errors = []
    if "package" not in code.lower():      # SysML v2 必须有 package
        errors.append("缺少 package 声明")
    if code.count("{") != code.count("}"): # 花括号必须成对
        errors.append("花括号不匹配")
    if "part def" not in code and "part " not in code.lower():
        errors.append("缺少 part 定义")    # 没有 part = 只有空壳
    return errors


# ╔══════════════════════════════════════════════════════════════╗
# ║  数据流追踪：                                                  ║
# ║                                                              ║
# ║  main.py:                                                    ║
# ║    sysml = generate_sysml(req, sysml_dir)                     ║
# ║            │ req = StructuredRequirement (来自节点1)            ║
# ║            │ sysml_dir = outputs/run_xxx/sysml/               ║
# ║            ▼                                                 ║
# ║  node2:  第1次: LLM 生成代码 → 语法检查通过 → break             ║
# ║          (或) 第1次失败 → 错误回喂 → 第2次重试                  ║
# ║         保存 sysml_dir/model.sysml                            ║
# ║         return SysMLArtifact(sysml_code=..., file_path=...)    ║
# ║                                                              ║
# ║  main.py 继续:                                                ║
# ║    mo = generate_and_simulate(req, sysml, ...)                ║
# ║         ↑ req和sysml都传给节点3                                 ║
# ╚══════════════════════════════════════════════════════════════╝
