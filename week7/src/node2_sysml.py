"""
节点 2 — SysML Agent（V6 三阶段 + 检索增强版）。

V4 → V6 变更:
  - 生成前：检索 42 个官方 SysML 示例 → 注入 prompt（SysML 检索层）
  - 生成后：LLM 审查员对照官方示例逐条检查（而非只用正则）
  - 审查发现语义/参数问题时：LLM 修正员针对性修改
  - 保留 V4 的正则语法检查作为第二层防线
  - Prompt 全部按 V6 规范重写：具体角色 + 检查清单 + JSON 输出

Agent ② 内部流程:
  检索官方示例 → 生成 SysML 代码 → 审查（对照官方示例+需求参数）
  → 如有问题 → 修正 → 再审 → 通过 / 断路器截断

关键设计（对齐 V6 Prompt 设计规范）:
  - 审查员的 prompt 必须贴官方示例（有"标准答案"可对比）
  - 不是"你觉得对不对"，是"这段跟官方示例写法差在哪"
"""

import json
import logging
import time
from pathlib import Path

from src.llm_client import chat, chat_with, user_msg
from src.agent_loop import (
    run_review_loop,
    ReviewResult,
    ReviewIssue,
    AgentLoopResult,
    parse_review_json,
)
from src.retrieval import get_references, get_references_llm_select
from src.schemas import StructuredRequirement, SysMLArtifact
from src.utils import load_prompt, clean_code_block

logger = logging.getLogger("node2")

# V4 兼容：sysmlpy 可用性缓存
_sysmlpy_available: bool | None = None
_sysmlpy_version: str = ""


def _check_sysmlpy() -> tuple[bool, str]:
    """检测 sysmlpy 是否可用。结果缓存。"""
    global _sysmlpy_available, _sysmlpy_version
    if _sysmlpy_available is not None:
        return _sysmlpy_available, _sysmlpy_version
    try:
        import sysmlpy
        _sysmlpy_available = True
        _sysmlpy_version = getattr(sysmlpy, "__version__", "unknown")
        logger.info("sysmlpy %s 可用，启用标准语法检查", _sysmlpy_version)
    except ImportError:
        _sysmlpy_available = False
        _sysmlpy_version = ""
        logger.warning("sysmlpy 未安装，降级为 V3 正则语法检查。安装: pip install sysmlpy")
    return _sysmlpy_available, _sysmlpy_version


def _build_parameter_replacements(params: dict) -> dict[str, str]:
    """从需求参数构建 prompt 占位符替换表。"""
    defaults = {
        "R": 1000.0, "R1": 1000.0, "R2": 10000.0,
        "C": 1e-6, "C1": 1e-6, "C2": 1e-7,
        "L": 1e-3, "L1": 1e-3,
        "V": 5.0, "V_in": 5.0, "Vcc": 15.0, "Vee": -15.0,
        "T": 298.15, "T_outdoor": 308.15, "T_indoor": 298.15,
        "Rf": 10000.0, "Rin": 1000.0, "R_f": 10000.0, "R_in": 1000.0,
        "Vin": 0.5,
    }
    replacements = {}
    for key, default_val in defaults.items():
        val = params.get(key, default_val)
        replacements[f"{{parameters_{key}}}"] = str(val)
    return replacements


# ============================================================
# LangGraph 节点入口
# ============================================================

def node2_generate(state: dict) -> dict:
    """LangGraph 节点：生成 SysML v2 .sysml 文件（V6 Agent 版）。"""
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


# ============================================================
# V6: 三阶段 Agent 生成（检索增强）
# ============================================================

def _generate(
    req: StructuredRequirement,
    sysml_dir: Path,
    temperature: float,
    feedback: str = "",
    max_syntax_retries: int = 3,
) -> SysMLArtifact:
    """V6: 检索官方示例 → 生成 → 审查 → 修正 → 语法检查。"""
    component_type = req.component_type or ""
    params_str = "\n".join(f"  {k} = {v}" for k, v in req.parameters.items())
    constraints_str = "\n".join(f"  - {c}" for c in req.constraints)

    # ── 第 0 步: 检索官方示例 ──
    logger.info("节点2: 检索 SysML 官方示例...")
    references, selected_domains = get_references_llm_select(
        component_type, stage="generate", code_fences=True
    )
    review_references, _ = get_references_llm_select(
        component_type, stage="review", code_fences=False
    )

    # ── 第 1 步: 生成 ──
    def generate_sysml() -> str:
        """使用检索到的官方示例生成 SysML 代码。"""
        prompt = (
            load_prompt("node2_sysml.txt")
            .replace("{component_type}", req.component_type)
            .replace("{component_name}", req.component_name or req.component_type)
            .replace("{parameters}", params_str)
            .replace("{topology}", req.topology)
            .replace("{constraints}", constraints_str)
            .replace("{references}", references)
            .replace("{prev_error_section}", f"## 用户反馈\n{feedback}" if feedback else "")
        )

        # 参数占位符替换
        param_replacements = _build_parameter_replacements(req.parameters)
        for placeholder, value in param_replacements.items():
            prompt = prompt.replace(placeholder, value)

        # V3 遗留兼容
        prompt = prompt.replace("{parameters_R}", str(req.parameters.get("R", 1000)))
        prompt = prompt.replace("{parameters_C}", str(req.parameters.get("C", 1e-6)))

        logger.info("节点2: 调用 LLM 生成 SysML 代码（域: %s）...", selected_domains)
        sysml_code = chat([user_msg(prompt)], temperature=temperature, max_tokens=4096).strip()
        sysml_code = clean_code_block(sysml_code, "sysml")
        return sysml_code

    # ── 第 2 步: 审查 ──
    def review_sysml(sysml_code: str) -> ReviewResult:
        """V6 结构化审查：对照官方示例 + 需求参数逐条检查。"""
        req_json = json.dumps({
            "component_type": req.component_type,
            "component_name": req.component_name,
            "parameters": req.parameters,
            "topology": req.topology,
            "constraints": req.constraints,
        }, ensure_ascii=False, indent=2)

        review_prompt = (
            load_prompt("node2_review.txt")
            .replace("{component_type}", req.component_type)
            .replace("{req_json}", req_json)
            .replace("{sysml_code}", sysml_code)
            .replace("{references}", review_references)
        )

        review_raw = chat_with("review", [user_msg(review_prompt)], temperature=0.1, max_tokens=2048)
        return parse_review_json(review_raw)

    # ── 第 3 步: 修正 ──
    def revise_sysml(sysml_code: str, issues: list[ReviewIssue]) -> str:
        """根据审查发现的具体问题修正代码。"""
        issues_text = _format_issues(issues)
        req_json_str = json.dumps({
            'component_type': req.component_type,
            'parameters': req.parameters,
            'topology': req.topology,
            'constraints': req.constraints,
        }, ensure_ascii=False, indent=2)

        revise_prompt = (
            f"## 角色\n你是一个 SysML v2 修正专家。根据审查发现的具体问题修正 SysML 代码。\n\n"
            f"## 输入数据\n```\n"
            f"=== 原始需求 ===\n{req_json_str}\n\n"
            f"=== 当前 SysML 代码（需要修正）===\n{sysml_code}\n"
            f"```\n\n"
            f"## 审查发现的问题（只修正这些问题，不要变动其他部分）\n{issues_text}\n\n"
            f"## 官方参考示例\n{review_references}\n\n"
            f"## 要求\n"
            f"1. 只修正上述列出的具体问题，不要改动其他正确的部分\n"
            f"2. 修正后重新输出完整的 SysML v2 代码\n"
            f"3. 确保修正不引入新问题\n"
            f"4. 保持 package 结构、import 声明、part/port/connect 的完整性\n"
            f"5. 只输出修正后的 SysML v2 代码，不要包含解释"
        )

        revised = chat([user_msg(revise_prompt)], temperature=0.2, max_tokens=4096).strip()
        return clean_code_block(revised, "sysml")

    # ── 运行三阶段循环 ──
    loop_result = run_review_loop(
        generate_fn=generate_sysml,
        review_fn=review_sysml,
        revise_fn=revise_sysml,
        max_rounds=3,
        label="node2",
    )

    sysml_code = loop_result.final_output
    artifact = SysMLArtifact(
        sysml_code=sysml_code,
        attempts=loop_result.rounds,
    )

    # ── 语法检查（第二层防线）──
    syntax_errors = _syntax_check(sysml_code)
    if syntax_errors:
        fatal_count = sum(1 for e in syntax_errors if e.startswith("[fatal]"))
        logger.warning("节点2 语法检查: fatal=%s, total=%s", fatal_count, len(syntax_errors))

        if fatal_count > 0 and loop_result.passed:
            # 审查通过了但语法检查发现 fatal → 用语法错误做一次修正
            logger.info("节点2: 审查通过但有 fatal 语法问题，进行一次语法修正")
            sysml_code = _syntax_fix(sysml_code, syntax_errors, req, review_references)
            artifact.attempts += 1

        # 只有 warning/error → 记录但不阻塞
        artifact.errors = [
            e for e in syntax_errors
            if not (e.startswith("[error]") or e.startswith("[warning]"))
        ]
        if not artifact.errors:
            artifact.errors = []

    artifact.sysml_code = sysml_code

    # ── 保存 ──
    sysml_dir.mkdir(parents=True, exist_ok=True)
    file_path = sysml_dir / "model.sysml"
    file_path.write_text(sysml_code, encoding="utf-8")
    artifact.file_path = str(file_path)

    return artifact


# ============================================================
# 语法检查（V4 保留，第二层防线）
# ============================================================

def _syntax_check(code: str) -> list[str]:
    """V4 语法检查：正则快速检测 + sysmlpy 标准解析器。

    错误分级:
      [fatal] — 必须修正（如 import ISQ::*, 缺 package）
      [error] — 不符合标准但可能可工作
      [warning] — 风格问题，不阻塞
    """
    errors = []

    if "package" not in code:
        errors.append("[fatal] 缺少 package 声明")

    if "import ISQ::*" in code or "import ISQ :: *" in code:
        errors.append("[fatal] 使用了非法 import ISQ::*，应改为 private import ScalarValues::*")
    if "import SI::*" in code or "import SI :: *" in code:
        errors.append("[fatal] 使用了非法 import SI::*，应删除此行")
    if ":> ISQ::" in code:
        errors.append("[fatal] 使用了非法属性类型 :> ISQ::xxx，应改为 : Real")
    if ":> ScalarValues::" in code:
        errors.append("[error] 建议用 : Real 替代 :> ScalarValues::xxx")

    if "part def" not in code and "part " not in code:
        errors.append("[fatal] 缺少 part 定义")

    if code.count("{") != code.count("}"):
        errors.append("[fatal] 花括号不匹配")

    if "{{" in code or "}}" in code:
        errors.append("[error] 代码中存在双花括号 {{ 或 }}，应改为单花括号")

    # sysmlpy 标准解析器
    sysmlpy_ok, _ = _check_sysmlpy()
    if sysmlpy_ok:
        import sysmlpy
        try:
            sysmlpy.loads(code)
        except Exception as e:
            err_msg = str(e)[:200]
            errors.append(f"[error] sysmlpy: {err_msg}")

    return errors


def _syntax_fix(sysml_code: str, errors: list[str],
                req: StructuredRequirement, references: str) -> str:
    """仅针对语法错误做一次修正（不跑完整 review loop，避免无限循环）。"""
    params_str = "\n".join(f"  {k} = {v}" for k, v in req.parameters.items())
    error_text = "\n".join(f"- {e}" for e in errors)

    prompt = (
        f"## 角色\nSysML v2 语法修正专家。\n\n"
        f"## 语法检查错误\n{error_text}\n\n"
        f"## 组件信息\n- 类型: {req.component_type}\n- 参数: {params_str}\n- 拓扑: {req.topology}\n\n"
        f"## 当前代码\n```\n{sysml_code}\n```\n\n"
        f"## 参考规范\n{references}\n\n"
        f"请修正以上语法错误，输出完整的 SysML v2 代码。只输出代码，不要解释。"
    )
    fixed = chat([user_msg(prompt)], temperature=0.1, max_tokens=4096).strip()
    return clean_code_block(fixed, "sysml")


# ============================================================
# 工具函数
# ============================================================

def _format_issues(issues: list[ReviewIssue]) -> str:
    """格式化问题列表为可读文本。"""
    lines = []
    for i, issue in enumerate(issues):
        loc = f" (位置: {issue.location})" if issue.location else ""
        sug = f" → 建议: {issue.suggestion}" if issue.suggestion else ""
        lines.append(f"{i+1}. [{issue.severity}][{issue.category}] {issue.description}{loc}{sug}")
    return "\n".join(lines)
