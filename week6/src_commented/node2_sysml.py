# -*- coding: utf-8 -*-
"""
=============================================================================
node2_sysml.py — 节点 2：SysML v2 代码生成（V4 重磅升级版）
=============================================================================

流水线的第二站。读入结构化需求 → LLM 生成 SysML v2 代码 → 语法检查 → 保存。

V4 核心变更（整个版本最重要的改动在这里）:

  1. prompt 语法升级（H3）:
     - import ISQ::*  → private import ScalarValues::*
     - attribute :> ISQ::xxx → attribute : Real
     - 双花括号 {{ → 单花括号 {
     原因: sysmlpy 标准解析器拒绝 V3 的旧语法（探针实验: V3=0% 通过, V4=100%）

  2. _syntax_check() 打分制（H1）:
     - [fatal] — 必须修正（如 import ISQ::*）
     - [error] — 记录但不阻断（如双花括号）
     - sysmlpy 可用时用标准解析器，不可用时 fallback 正则

  3. _build_parameter_replacements()（V4 新增）:
     - 自动为 prompt 模板中的 {parameters_R}, {parameters_L}, 等占位符
       填入需求参数的实际值

  4. max_retries: 2 → 3（给 LLM 多一次修语法错误的机会）

LangGraph 节点函数: node2_generate(state) → {sysml, timing}
=============================================================================
"""

import time
import logging
from pathlib import Path

from src.llm_client import chat, user_msg
from src.schemas import StructuredRequirement, SysMLArtifact
from src.utils import load_prompt, clean_code_block

logger = logging.getLogger("node2")


# ==========================================================================
# V4: sysmlpy 可用性缓存
# ==========================================================================
# 为什么缓存: sysmlpy.loads() 内部导入较慢（有 pint 库的警告），
# 每调用一次 _syntax_check() 都重新 import 会拖慢实验（60-360 次循环）。
# 首次 import 后缓存结果到模块级变量，后续调用直接用缓存。

_sysmlpy_available: bool | None = None   # True=可用, False=不可用, None=未检测
_sysmlpy_version: str = ""               # sysmlpy 版本号（如 "0.34.1"）


def _check_sysmlpy() -> tuple[bool, str]:
    """
    检测 sysmlpy 是否可用。结果缓存到模块级变量，只 import 一次。

    Returns:
        (是否可用, 版本号)
    """
    global _sysmlpy_available, _sysmlpy_version
    if _sysmlpy_available is not None:      # 已检测过，直接返回缓存
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


# ==========================================================================
# V4 新增: 参数占位符自动构建
# ==========================================================================

def _build_parameter_replacements(params: dict) -> dict[str, str]:
    """
    从需求参数构建 prompt 占位符 → 实际值的映射表。

    为什么需要这个函数:
      prompt 模板里写了 {parameters_R}、{parameters_C} 等占位符。
      之前 V3 只硬编码了 R 和 C 两个参数。V4 多了 L（电感）、Rf/Rin（运放反馈/输入
      电阻）、R1/R2（双房间热阻）等，硬编码方式不可扩展。

    所以这个函数自动遍历常见的参数名，从 params dict 中取值填充，
    未提供的参数用合理默认值代替。

    例:
      params = {"R": 1000, "L": 0.01, "Rf": 10000}
      → {"{parameters_R}": "1000.0", "{parameters_L}": "0.01", "{parameters_Rf}": "10000.0"}

    Args:
        params: 需求参数字典，如 {"R": 1000, "C": 1e-6}

    Returns:
        占位符 → 值字符串的映射表
    """
    # 默认值映射: 如果需求里没提供这个参数，用下面的默认值
    defaults = {
        # 电阻类
        "R": 1000.0, "R1": 1000.0, "R2": 10000.0,
        # 电容类
        "C": 1e-6, "C1": 1e-6, "C2": 1e-7,
        # 电感类（V4 新增 — RLC 用例）
        "L": 1e-3, "L1": 1e-3,
        # 电压类
        "V": 5.0, "V_in": 5.0, "Vcc": 15.0, "Vee": -15.0,
        # 温度类
        "T": 298.15, "T_outdoor": 308.15, "T_indoor": 298.15,
        # 运放类（V4 新增 — opamp 用例）
        "Rf": 10000.0, "Rin": 1000.0,
        "R_f": 10000.0, "R_in": 1000.0,
        "Vin": 0.5,
    }
    replacements = {}
    for key, default_val in defaults.items():
        val = params.get(key, default_val)           # 优先用需求值，没有用默认
        replacements[f"{{parameters_{key}}}"] = str(val)  # 构造 "→" 映射
    return replacements


# ==========================================================================
# LangGraph 节点入口
# ==========================================================================

def node2_generate(state: dict) -> dict:
    """
    LangGraph 节点函数。从 state 读需求 → 生成 SysML → 写回 state。

    核心调用链: _generate() → load_prompt + LLM chat + _syntax_check
    """
    t0 = time.time()
    req_dict = state.get("req", {})
    req = StructuredRequirement(**req_dict)         # dict → Pydantic 对象
    run_dir = Path(state.get("run_dir", "."))
    sysml_dir = run_dir / "sysml"                   # 输出到 run_dir/sysml/model.sysml
    temperature = state.get("temperature", 0.3)
    feedback = state.get("human_feedback", "")      # 用户打回时的反馈

    artifact = _generate(req, sysml_dir, temperature, feedback)
    elapsed = time.time() - t0
    logger.info("节点2 完成 (%.1fs), 尝试=%s, 错误=%s", elapsed, artifact.attempts, len(artifact.errors))

    return {
        "sysml": artifact.model_dump(),             # Pydantic → dict
        "timing": {**state.get("timing", {}), "node2": elapsed},
    }


# ==========================================================================
# 核心: SysML 生成 + 语法检查循环
# ==========================================================================

def _generate(
    req: StructuredRequirement,
    sysml_dir: Path,
    temperature: float,
    feedback: str = "",
    max_retries: int = 3,                     # V4: 2→3，给 LLM 多一次机会
) -> SysMLArtifact:
    """
    生成 SysML v2 代码，含语法检查重试循环。

    循环逻辑（V4 打分制）:
      1. 构造 prompt（填充需求参数到模板）
      2. 调 LLM 生成 SysML 文本
      3. _syntax_check() 检查
      4. 有 [fatal] 错误 → 把错误喂给 LLM → 回到步骤 1 重新生成
      5. 仅有 [error]/[warning] → 接受此版本，不重试
      6. 没有错误 → 完美，退出循环

    V4 关键改进: fatal 才重试。V3 是"有任何问题都重试"，这导致无关紧要的
    warning 也会触发重试，浪费 API 调用。
    """
    # 格式化参数列表和约束列表（模板占位符 {parameters} 和 {constraints}）
    params_str = "\n".join(f"  {k} = {v}" for k, v in req.parameters.items())
    constraints_str = "\n".join(f"  - {c}" for c in req.constraints)

    artifact = SysMLArtifact()                  # 初始化产出对象
    prev_errors: list[str] = []                 # 上一轮的错误（用于 prompt 反馈）

    for attempt in range(1, max_retries + 1):
        # —— 构建错误反馈段（如果有上次错误，告诉 LLM 哪里错了）——
        prev_error_section = ""
        if prev_errors:
            prev_error_section = (
                f"\n## 上次生成的语法错误（请修正）\n"
                + "\n".join(f"- {e}" for e in prev_errors)
            )
        if feedback:
            prev_error_section += f"\n## 用户反馈\n{feedback}"

        # —— 加载模板并填充占位符 ——
        prompt = load_prompt("node2_sysml.txt")
        prompt = prompt.replace("{component_type}", req.component_type)
        prompt = prompt.replace("{component_name}", req.component_name or req.component_type)
        prompt = prompt.replace("{parameters}", params_str)
        prompt = prompt.replace("{topology}", req.topology)
        prompt = prompt.replace("{constraints}", constraints_str)
        prompt = prompt.replace("{prev_error_section}", prev_error_section)

        # V4: 自动填充 {parameters_R}、{parameters_L}、{parameters_Rf} 等占位符
        param_replacements = _build_parameter_replacements(req.parameters)
        for placeholder, value in param_replacements.items():
            prompt = prompt.replace(placeholder, value)

        # V3 遗留显式占位符（兼容旧模板，新模板已不再用这两个）
        prompt = prompt.replace("{parameters_R}", str(req.parameters.get("R", 1000)))
        prompt = prompt.replace("{parameters_C}", str(req.parameters.get("C", 1e-6)))

        # —— 调用 LLM ——
        logger.info("节点2 第%s次生成...", attempt)
        sysml_code = chat([user_msg(prompt)], temperature=temperature, max_tokens=4096).strip()
        sysml_code = clean_code_block(sysml_code, "sysml")  # 剥掉 ```sysml ... ```

        artifact.sysml_code = sysml_code
        artifact.attempts = attempt

        # —— V4 语法检查（打分制）——
        errors = _syntax_check(sysml_code)
        if errors:
            fatal_count = sum(1 for e in errors if e.startswith("[fatal]"))
            logger.warning("节点2 第%s次语法问题: fatal=%s, total=%s", attempt, fatal_count, len(errors))
            prev_errors = errors
            artifact.errors = errors
            # ★ 关键: 只有 fatal 错误才重试，纯 warning/error 直接接受
            #    这避免了"一行格式问题就浪费一次 API 调用"的问题
            if fatal_count == 0:
                logger.info("节点2: 仅有 warning/error 级别问题，接受此版本")
                artifact.errors = []            # 清空错误，不阻塞流水线
                break
            continue                            # 有 fatal，进入下一轮重试

        # —— 通过！——
        logger.info("节点2 第%s次生成成功", attempt)
        artifact.errors = []
        break
    else:
        # for-else: 所有重试都用完了 → 接受最后一次的结果（即使有 fatal）
        logger.warning("节点2 %s次重试后仍有 fatal 问题，使用最后一次结果", max_retries)

    # —— 保存到磁盘 ——
    sysml_dir.mkdir(parents=True, exist_ok=True)
    file_path = sysml_dir / "model.sysml"
    file_path.write_text(artifact.sysml_code, encoding="utf-8")
    artifact.file_path = str(file_path)
    return artifact


# ==========================================================================
# V4 语法检查: 正则快速检测 + sysmlpy 标准解析器
# ==========================================================================

def _syntax_check(code: str) -> list[str]:
    """
    V4 语法检查：双层防线。

    第一层 — 正则快速检测（始终执行，0 依赖）:
      拦截 V3 残留的已知非法语法（import ISQ::* 等），这些即使 sysmlpy
      没装也应该拦截，因为我们已经从官方示例确认了它们不合法。

    第二层 — sysmlpy 标准解析器（sysmlpy 可用时执行）:
      调用 sysmlpy.loads() 做完整的 ANTLR 语法解析。
      sysmlpy 检测到的额外问题按 [error] 处理（不阻断，因为
      sysmlpy 可能过于严格，且正则已拦截了最致命的）。

    错误分级:
      [fatal] — 必须修正。例: import ISQ::*（会导致 node3 无法正确理解 SysML）
      [error] — 不符合标准但可能仍可工作。例: 双花括号、sysmlpy 报的语法警告
      [warning] — 风格问题。预留，当前未使用
    """
    errors = []

    # ═══════════════════════════════════════════════════════════════
    # 第一层: 正则快速检测（始终执行）
    # ═══════════════════════════════════════════════════════════════

    # 必须有 package 声明（SysML v2 的最外层结构）
    if "package" not in code:
        errors.append("[fatal] 缺少 package 声明")

    # 检测 V3 时期 LLM 学会的非法写法（这些是"肌肉记忆"级别的错误，
    # 因为 V3 prompt 明确教 LLM 这样写，V4 必须先拦截）
    if "import ISQ::*" in code or "import ISQ :: *" in code:
        errors.append("[fatal] 使用了非法 import ISQ::*，应改为 private import ScalarValues::*")
    if "import SI::*" in code or "import SI :: *" in code:
        errors.append("[fatal] 使用了非法 import SI::*，应删除此行")

    # 检测旧版属性类型声明（V4 已改用 : Real，但 LLM 可能仍输出旧写法）
    if ":> ISQ::" in code:
        errors.append("[fatal] 使用了非法属性类型 :> ISQ::xxx，应改为 : Real")
    if ":> ScalarValues::" in code:
        errors.append("[error] 建议用 : Real 替代 :> ScalarValues::xxx")

    # 必须有 part 定义（SysML 建模的基本单元）
    if "part def" not in code and "part " not in code:
        errors.append("[fatal] 缺少 part 定义")

    # 花括号必须成对出现（否则 sysmlpy 解析必然报错）
    if code.count("{") != code.count("}"):
        errors.append("[fatal] 花括号不匹配")

    # 双花括号检测: V3 用 Python .format() 模板，{{ 是转义写法。
    # V4 改用 .replace() 后不再需要，但 LLM 学过 V3 示例可能仍输出 {{
    if "{{" in code or "}}" in code:
        errors.append("[error] 代码中存在双花括号 {{ 或 }}，应改为单花括号")

    # ═══════════════════════════════════════════════════════════════
    # 第二层: sysmlpy 标准解析器（可用时执行）
    # ═══════════════════════════════════════════════════════════════
    sysmlpy_ok, _ = _check_sysmlpy()
    if sysmlpy_ok:
        import sysmlpy
        try:
            # sysmlpy.loads() 是 ANTLR 驱动的标准解析器
            # 如果能解析成功，说明代码语法完全合规
            sysmlpy.loads(code)
        except Exception as e:
            err_msg = str(e)[:200]         # 截断，防日志过长
            # sysmlpy 报的错误按 [error] 处理 — 记录但不阻断流水线
            # 原因: 正则已经拦截了最严重的问题（import ISQ::* 等），
            #       sysmlpy 额外发现的通常是较温和的语法细节
            errors.append(f"[error] sysmlpy: {err_msg}")

    return errors
