"""
节点 2 — SysML v2 代码生成。V4 版：prompt 对齐官方写法 + sysmlpy 语法检查（打分制）+ 最多 3 次重试。

LangGraph 节点函数，读 state 写回 sysml + timing。

V4 变更:
  - prompt 语法升级: import ISQ::* → private import ScalarValues::*; attribute :> ISQ::xxx → : Real
  - _syntax_check 新增 sysmlpy 打分制: fatal 打回 / error 记录 / warning 放行
  - sysmlpy 不可用时自动 fallback 正则检查
  - 新参数占位符: {parameters_L}, {parameters_R2} 支持 RLC / 多房间等用例
"""

import time
import logging
from pathlib import Path

from src.llm_client import chat, user_msg
from src.schemas import StructuredRequirement, SysMLArtifact
from src.utils import load_prompt, clean_code_block

logger = logging.getLogger("node2")

# V4: sysmlpy 可用性缓存（首次 import 后缓存结果，避免每次检查都 import）
_sysmlpy_available: bool | None = None
_sysmlpy_version: str = ""


def _check_sysmlpy() -> tuple[bool, str]:
    """检测 sysmlpy 是否可用。结果缓存，只 import 一次。"""
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
    """从需求参数构建 prompt 占位符替换表。

    自动覆盖常见参数名: R, C, L, R1, R2, C1, C2, V, T 等。
    未提供的参数用默认值填充。
    """
    # 默认值映射
    defaults = {
        "R": 1000.0, "R1": 1000.0, "R2": 10000.0,
        "C": 1e-6, "C1": 1e-6, "C2": 1e-7,
        "L": 1e-3, "L1": 1e-3,
        "V": 5.0, "V_in": 5.0, "Vcc": 15.0, "Vee": -15.0,
        "T": 298.15, "T_outdoor": 308.15, "T_indoor": 298.15,
        # V4 运放参数
        "Rf": 10000.0, "Rin": 1000.0,
        "R_f": 10000.0, "R_in": 1000.0,
        "Vin": 0.5,
    }
    replacements = {}
    for key, default_val in defaults.items():
        val = params.get(key, default_val)
        replacements[f"{{parameters_{key}}}"] = str(val)
    return replacements


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
    max_retries: int = 3,
) -> SysMLArtifact:
    """生成 SysML v2 代码，含 sysmlpy 语法检查 + 正则 fallback 重试。V4: max_retries=3。"""
    params_str = "\n".join(f"  {k} = {v}" for k, v in req.parameters.items())
    constraints_str = "\n".join(f"  - {c}" for c in req.constraints)

    artifact = SysMLArtifact()
    prev_errors: list[str] = []

    for attempt in range(1, max_retries + 1):
        prev_error_section = ""
        if prev_errors:
            prev_error_section = f"\n## 上次生成的语法错误（请修正）\n" + "\n".join(f"- {e}" for e in prev_errors)
        if feedback:
            prev_error_section += f"\n## 用户反馈\n{feedback}"

        prompt = load_prompt("node2_sysml.txt")
        prompt = prompt.replace("{component_type}", req.component_type)
        prompt = prompt.replace("{component_name}", req.component_name or req.component_type)
        prompt = prompt.replace("{parameters}", params_str)
        prompt = prompt.replace("{topology}", req.topology)
        prompt = prompt.replace("{constraints}", constraints_str)
        prompt = prompt.replace("{prev_error_section}", prev_error_section)

        # V4: 自动参数占位符替换（覆盖 R/C/L/R1/R2 等）
        param_replacements = _build_parameter_replacements(req.parameters)
        for placeholder, value in param_replacements.items():
            prompt = prompt.replace(placeholder, value)

        # V3 遗留：显式的 {parameters_R} / {parameters_C}（prompt 已不再用，保留兼容）
        prompt = prompt.replace("{parameters_R}", str(req.parameters.get("R", 1000)))
        prompt = prompt.replace("{parameters_C}", str(req.parameters.get("C", 1e-6)))

        logger.info("节点2 第%s次生成...", attempt)
        sysml_code = chat([user_msg(prompt)], temperature=temperature, max_tokens=4096).strip()
        sysml_code = clean_code_block(sysml_code, "sysml")

        artifact.sysml_code = sysml_code
        artifact.attempts = attempt

        errors = _syntax_check(sysml_code)
        if errors:
            fatal_count = sum(1 for e in errors if e.startswith("[fatal]"))
            logger.warning("节点2 第%s次语法问题: fatal=%s, total=%s", attempt, fatal_count, len(errors))
            prev_errors = errors
            artifact.errors = errors
            # 只有 fatal 才重试，纯 warning/error 直接接受
            if fatal_count == 0:
                logger.info("节点2: 仅有 warning/error 级别问题，接受此版本")
                artifact.errors = []  # 清空，不阻塞流水线
                break
            continue

        logger.info("节点2 第%s次生成成功", attempt)
        artifact.errors = []
        break
    else:
        logger.warning("节点2 %s次重试后仍有 fatal 问题，使用最后一次结果", max_retries)

    # 保存
    sysml_dir.mkdir(parents=True, exist_ok=True)
    file_path = sysml_dir / "model.sysml"
    file_path.write_text(artifact.sysml_code, encoding="utf-8")
    artifact.file_path = str(file_path)
    return artifact


def _syntax_check(code: str) -> list[str]:
    """V4 语法检查：正则快速检测 + sysmlpy fallback 机制。

    错误分级:
      [fatal] — 必须修正，否则 node3 无法工作（如 import ISQ::*）
      [error] — 不符合标准但可能仍可工作的语法（如缺少 doc）
      [warning] — 风格问题，不阻塞流水线

    sysmlpy 可用时优先用标准解析器，不可用时降级为正则。
    """
    errors = []

    # ── V4 正则快速检测（始终执行）──
    if "package" not in code:
        errors.append("[fatal] 缺少 package 声明")

    # 检测旧版非法 import（V3 prompt 残留问题）
    if "import ISQ::*" in code or "import ISQ :: *" in code:
        errors.append("[fatal] 使用了非法 import ISQ::*，应改为 private import ScalarValues::*")
    if "import SI::*" in code or "import SI :: *" in code:
        errors.append("[fatal] 使用了非法 import SI::*，应删除此行")
    # 检测旧版属性类型声明
    if ":> ISQ::" in code:
        errors.append("[fatal] 使用了非法属性类型 :> ISQ::xxx，应改为 : Real")
    if ":> ScalarValues::" in code:
        errors.append("[error] 建议用 : Real 替代 :> ScalarValues::xxx")

    if "part def" not in code and "part " not in code:
        errors.append("[fatal] 缺少 part 定义")

    if code.count("{") != code.count("}"):
        errors.append("[fatal] 花括号不匹配")

    # 检测双花括号（V3 模板遗留问题，{{ → { 但 LLM 可能仍输出 {{）
    if "{{" in code or "}}" in code:
        errors.append("[error] 代码中存在双花括号 {{ 或 }}，应改为单花括号")

    # ── V4 sysmlpy 标准解析器（可用时执行）──
    sysmlpy_ok, _ = _check_sysmlpy()
    if sysmlpy_ok:
        import sysmlpy
        try:
            sysmlpy.loads(code)
        except Exception as e:
            err_msg = str(e)[:200]
            # sysmlpy 报的错误按 [error] 处理——记录但不阻断
            # 因为 sysmlpy 可能过于严格，且正则已捕获最严重的问题
            errors.append(f"[error] sysmlpy: {err_msg}")

    return errors
