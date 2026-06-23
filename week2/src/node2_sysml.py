"""
节点 2 — SysML v2 生成：把 StructuredRequirement 转为 .sysml 文本文件。

不自动渲染。用户手动打开 Eclipse（Pilot Implementation）看图。

用法：
    from src.node2_sysml import generate_sysml
    artifact = generate_sysml(req, work_dir)
"""

import os
from pathlib import Path

from src.llm_client import chat, user_msg
from src.schemas import StructuredRequirement, SysMLArtifact


def _load_prompt(name: str) -> str:
    path = os.path.join(os.path.dirname(__file__), "..", "prompts", name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def generate_sysml(
    req: StructuredRequirement,
    sysml_dir: Path,
    max_retries: int = 2,
) -> SysMLArtifact:
    """根据结构化需求生成 SysML v2 代码。"""
    params_str = "\n".join(f"  {k} = {v}" for k, v in req.parameters.items())
    constraints_str = "\n".join(f"  - {c}" for c in req.constraints)

    artifact = SysMLArtifact(source_requirement=req)
    prev_errors: list[str] = []

    for attempt in range(1, max_retries + 1):
        # 构建 prompt
        prev_error_section = ""
        if prev_errors:
            prev_error_section = (
                f"\n## 上次生成的错误（请修正）\n{chr(10).join(f'- {e}' for e in prev_errors)}"
            )

        prompt = (
            _load_prompt("node2_sysml.txt")
            .replace("{component_type}", req.component_type)
            .replace("{component_name}", req.component_name or req.component_type)
            .replace("{parameters}", params_str)
            .replace("{topology}", req.topology)
            .replace("{constraints}", constraints_str)
            .replace("{parameters_R}", str(req.parameters.get("R", 1000)))
            .replace("{parameters_C}", str(req.parameters.get("C", 1e-6)))
            .replace("{prev_error_section}", prev_error_section)
        )

        print(f"[节点2] 第{attempt}次尝试生成 SysML...")
        sysml_code = chat([user_msg(prompt)], temperature=0.2, max_tokens=4096).strip()

        # 清洗 markdown 代码块
        sysml_code = _clean_code_block(sysml_code, "sysml")

        artifact.sysml_code = sysml_code
        artifact.attempts = attempt

        # 基本语法检查
        errors = _basic_syntax_check(sysml_code)
        if errors:
            print(f"[节点2] 第{attempt}次有语法警告: {errors}")
            prev_errors = errors
            artifact.errors = errors
            continue

        print(f"[节点2] 第{attempt}次生成成功。")
        break
    else:
        print(f"[节点2] {max_retries}次重试后仍存在问题，使用最后一次结果。")

    # 保存到 sysml/ 子目录
    file_path = sysml_dir / "model.sysml"
    file_path.write_text(artifact.sysml_code, encoding="utf-8")
    artifact.file_path = str(file_path)

    return artifact


def _clean_code_block(text: str, lang: str) -> str:
    """去掉 LLM 返回的 ```sysml ... ``` 等包裹。"""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]  # 去掉 ```sysml
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text


def _basic_syntax_check(code: str) -> list[str]:
    """简单语法检查：package/packages 闭合、花括号匹配。"""
    errors = []
    if "package" not in code.lower():
        errors.append("缺少 package 声明")
    if code.count("{") != code.count("}"):
        errors.append("花括号不匹配")
    if "part def" not in code and "part " not in code.lower():
        errors.append("缺少 part 定义")
    return errors
