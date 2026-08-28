"""统一 SysML 语法检查器。

为什么需要这个模块:
  V4 的 `week5/src/node2_sysml.py::_syntax_check` 与 V6 的 `week7/src/node2_sysml.py::_syntax_check`
  逻辑几乎相同，但**两边的 node2 在成功路径上都会清空 errors**：
    - V4: 仅 warning/error 时 `artifact.errors = []`
    - V6: 过滤掉 error/warning，只保留 fatal
  因此直接读 state 里的 `sysml.errors` 拿不到可比的"语法错误数"。

  解法: benchmark 在 runner 里用**本模块这一套统一检查器**对最终产出的 sysml_code
  重新检查一遍，得到口径一致的 fatal/error/warning 计数。这样"语法错误数"指标
  不依赖 V4/V6 各自内部怎么清空 errors，公平可比。

实现上直接复用 V4/V6 里 `_syntax_check` 的正则规则（两版一致），sysmlpy 可用时叠加。
"""

import logging

logger = logging.getLogger("syntax_check")

# sysmlpy 可用性缓存（首次 import 后缓存，避免每次检查都 import）
_sysmlpy_available: bool | None = None


def _check_sysmlpy() -> bool:
    """检测 sysmlpy 是否可用。结果缓存。"""
    global _sysmlpy_available
    if _sysmlpy_available is not None:
        return _sysmlpy_available
    try:
        import sysmlpy  # noqa: F401
        _sysmlpy_available = True
    except ImportError:
        _sysmlpy_available = False
        logger.info("sysmlpy 未安装，仅用正则语法检查")
    return _sysmlpy_available


def check(code: str) -> dict:
    """对 SysML v2 代码做统一语法检查。

    返回:
        {
            "fatal": int,      # 必须修正（缺 package / 非法 import / 花括号不匹配 等）
            "error": int,      # 不符合标准但可能可工作
            "warning": int,    # 风格问题
            "issues": list[str],  # 全部问题描述（含 [fatal]/[error]/[warning] 前缀）
        }
    """
    issues: list[str] = []

    # ── 正则快速检查（与 V4/V6 的 _syntax_check 一致）──
    if "package" not in code:
        issues.append("[fatal] 缺少 package 声明")

    if "import ISQ::*" in code or "import ISQ :: *" in code:
        issues.append("[fatal] 使用了非法 import ISQ::*，应改为 private import ScalarValues::*")
    if "import SI::*" in code or "import SI :: *" in code:
        issues.append("[fatal] 使用了非法 import SI::*，应删除此行")
    if ":> ISQ::" in code:
        issues.append("[fatal] 使用了非法属性类型 :> ISQ::xxx，应改为 : Real")
    if ":> ScalarValues::" in code:
        issues.append("[error] 建议用 : Real 替代 :> ScalarValues::xxx")

    if "part def" not in code and "part " not in code:
        issues.append("[fatal] 缺少 part 定义")

    if code.count("{") != code.count("}"):
        issues.append("[fatal] 花括号不匹配")

    if "{{" in code or "}}" in code:
        issues.append("[error] 代码中存在双花括号 {{ 或 }}，应改为单花括号")

    # ── sysmlpy 标准解析器（可用时叠加，报错按 error 处理）──
    if _check_sysmlpy():
        import sysmlpy
        try:
            sysmlpy.loads(code)
        except Exception as e:
            issues.append(f"[error] sysmlpy: {str(e)[:200]}")

    # 计数
    fatal = sum(1 for i in issues if i.startswith("[fatal]"))
    error = sum(1 for i in issues if i.startswith("[error]"))
    warning = sum(1 for i in issues if i.startswith("[warning]"))

    return {"fatal": fatal, "error": error, "warning": warning, "issues": issues}
