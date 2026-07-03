"""
节点 2 — SysML v2 代码生成。V2 版：prompt 参考官方库，basic 语法检查 + 最多 2 次重试。

LangGraph 节点函数，读 state 写回 sysml + timing。
"""

import time
import logging
from pathlib import Path

from src.llm_client import chat, user_msg
from src.schemas import StructuredRequirement, SysMLArtifact
from src.utils import load_prompt, clean_code_block

logger = logging.getLogger("node2")


def node2_generate(state: dict) -> dict:
    """LangGraph 节点：生成 SysML v2 .sysml 文件。"""
    t0 = time.time()
    req_dict = state.get("req", {})
    req = StructuredRequirement(**req_dict)
    run_dir = Path(state.get("run_dir", "."))
    sysml_dir = run_dir / "sysml"
    temperature = state.get("temperature", 0.3)
    feedback = state.get("human_feedback", "")

    artifact = _generate(req, sysml_dir, temperature, feedback)
    elapsed = time.time() - t0
    logger.info("节点2 完成 (%.1fs), 尝试=%s, 错误=%s", elapsed, artifact.attempts, len(artifact.errors))

    return {
        "sysml": artifact.model_dump(),
        "timing": {**state.get("timing", {}), "node2": elapsed},
    }


def _generate(
    req: StructuredRequirement,
    sysml_dir: Path,
    temperature: float,
    feedback: str = "",
    max_retries: int = 2,
) -> SysMLArtifact:
    """生成 SysML v2 代码，含基本语法检查重试。"""
    params_str = "\n".join(f"  {k} = {v}" for k, v in req.parameters.items())
    constraints_str = "\n".join(f"  - {c}" for c in req.constraints)

    artifact = SysMLArtifact()
    prev_errors: list[str] = []

    for attempt in range(1, max_retries + 1):
        prev_error_section = ""
        if prev_errors:
            prev_error_section = f"\n## 上次生成的错误\n" + "\n".join(f"- {e}" for e in prev_errors)
        if feedback:
            prev_error_section += f"\n## 用户反馈\n{feedback}"

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

        errors = _syntax_check(sysml_code)
        if errors:
            logger.warning("节点2 第%s次语法警告: %s", attempt, errors)
            prev_errors = errors
            artifact.errors = errors
            continue

        logger.info("节点2 第%s次生成成功", attempt)
        artifact.errors = []
        break
    else:
        logger.warning("节点2 %s次重试后仍有问题，使用最后一次结果", max_retries)

    # 保存
    sysml_dir.mkdir(parents=True, exist_ok=True)
    file_path = sysml_dir / "model.sysml"
    file_path.write_text(artifact.sysml_code, encoding="utf-8")
    artifact.file_path = str(file_path)
    return artifact


def _syntax_check(code: str) -> list[str]:
    """基本语法检查。"""
    errors = []
    if "package" not in code.lower():
        errors.append("缺少 package 声明")
    if code.count("{") != code.count("}"):
        errors.append("花括号不匹配")
    if "part def" not in code and "part " not in code.lower():
        errors.append("缺少 part 定义")
    return errors
