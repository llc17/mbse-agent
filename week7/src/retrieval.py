"""
SysML 检索层 — V6 核心基础设施。

从 42 个官方 SysML v2 Training 示例中，按系统域（电气/热/机械）自动匹配
最相关的 .sysml 文件，拼接为参考代码注入 prompt，让 LLM 有"标准答案"可以对照。

核心设计（对齐 V6 Prompt 设计规范）:
  - Python 写死映射规则（不用 LLM 猜）
  - LLM 轻量参与解决歧义（如"热敏电阻"同时命中热+电气）
  - Stage 参数控制输出格式: generate=完整示例, review=规范对照, repair=正确语法模式

用法:
    from src.retrieval import get_references, detect_domain

    refs = get_references("RC低通滤波器", stage="generate")
    # → 返回 07_Parts + 09_Connections + 10_Ports 的拼接代码

    refs = get_references("双房间热传导", stage="review")
    # → 返回 08_Items + 13_Flows + 07_Parts 的拼接代码 + 审查清单

检索规则:
    RC/RLC/运放/电路 → 07_Parts, 09_Connections, 10_Ports, 11_Interfaces
    热/温度          → 08_Items, 13_Flows, 07_Parts
    机械             → 07_Parts, 09_Connections, 08_Items
    通用/语法        → 01_Packages, 02_Part Definitions, 03_Generalization, 06_Enumeration
"""

import logging
import re
from pathlib import Path
from functools import lru_cache
from typing import Optional

logger = logging.getLogger("retrieval")

# ============================================================================
# 配置: 官方 SysML 示例目录
# ============================================================================
_TRAINING_DIR = Path(r"D:\sysml-v2-official\sysml\src\training")

# ============================================================================
# 域 → 示例目录映射（Python 写死，不用 LLM 猜）
# ============================================================================
DOMAIN_MAP: dict[str, list[str]] = {
    "electrical": [
        "07. Parts",
        "09. Connections",
        "10. Ports",
        "11. Interfaces",
        "12. Binding Connectors",
    ],
    "thermal": [
        "08. Items",
        "13. Flows",
        "07. Parts",
    ],
    "mechanical": [
        "07. Parts",
        "09. Connections",
        "08. Items",
    ],
    "syntax": [
        "01. Packages",
        "02. Part Definitions",
        "03. Generalization",
        "04. Subsetting",
        "05. Redefinition",
        "06. Enumeration Definitions",
    ],
}

# ============================================================================
# 域检测关键词（用于 _detect_domain）
# ============================================================================
DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "electrical": [
        "rc", "rlc", "运放", "opamp", "op-amp", "电路", "电气", "electrical",
        "电阻", "resistor", "电容", "capacitor", "电感", "inductor",
        "滤波器", "filter", "放大器", "amplifier", "反相", "同相",
        "电压", "voltage", "电流", "current", "信号", "signal",
        "mosfet", "bjt", "二极管", "diode", "振荡", "oscillator",
        "阻抗", "impedance", "频率", "frequency", "截止", "cutoff",
        "电源", "power supply",
    ],
    "thermal": [
        "热", "thermal", "heat", "温度", "temperature",
        "房间", "room", "墙", "wall", "室外", "outdoor",
        "散热", "冷却", "cooling", "加热", "heating",
        "热阻", "thermal resistance", "热容", "heat capacity",
        "热传导", "conduction", "对流", "convection", "辐射", "radiation",
        "恒温", "thermostat",
    ],
    "mechanical": [
        "机械", "mechanical", "弹簧", "spring", "阻尼", "damper",
        "质量块", "mass", "力", "force", "位移", "displacement",
        "速度", "velocity", "加速度", "acceleration",
        "齿轮", "gear", "轴", "shaft", "轴承", "bearing",
        "转动", "rotation", "扭矩", "torque",
    ],
}

# ============================================================================
# Stage 说明（注入到返回的参考代码前）
# ============================================================================
STAGE_HEADERS: dict[str, str] = {
    "generate": (
        "## 官方 SysML v2 参考示例（来自 OMG SysML v2.0 Training）\n"
        "请严格参照以下官方示例的语法格式、命名规范和结构组织来生成代码。\n"
        "特别注意：import 写法、属性类型声明、part def 结构、connect 语法。\n\n"
    ),
    "review": (
        "## 官方 SysML v2 参考示例（审查对照标准）\n"
        "请逐条对比生成的代码与以下官方示例，检查：\n"
        "1. package 结构是否完整（package → part def → part usage）\n"
        "2. import 声明是否正确（private import ScalarValues::*）\n"
        "3. 属性类型是否用 Real（非 ISQ::xxx）\n"
        "4. connect 语法是否符合官方写法\n"
        "5. port 定义是否使用了正确的端口类型\n"
        "只报与官方示例写法不一致的硬伤，不确定的不写。\n\n"
    ),
    "repair": (
        "## 官方 SysML v2 参考示例（修正标准）\n"
        "请参照以下官方示例的正确写法修正代码中的语法和结构问题。\n"
        "确保修正后的代码：\n"
        "1. 语法格式与官方示例一致\n"
        "2. 结构组织（package/part def/part usage）符合规范\n"
        "3. 不引入新的问题\n\n"
    ),
}


# ============================================================================
# 文件缓存（避免重复读取）
# ============================================================================
@lru_cache(maxsize=128)
def _read_file_cached(path: str) -> str:
    """缓存读取单个 .sysml 文件。"""
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("读取示例文件失败: %s — %s", path, e)
        return ""


def _get_example_files(directories: list[str]) -> list[Path]:
    """根据目录名列表，返回所有 .sysml 文件的 Path 列表。"""
    files = []
    for dir_name in directories:
        dir_path = _TRAINING_DIR / dir_name
        if not dir_path.exists():
            logger.warning("SysML 示例目录不存在: %s", dir_path)
            continue
        for f in sorted(dir_path.glob("*.sysml")):
            files.append(f)
    return files


# ============================================================================
# 公开 API
# ============================================================================

def detect_domain(component_type: str) -> list[str]:
    """检测系统类型所属的域。

    返回候选域列表（按匹配关键词数降序排列）。
    如果同时命中多个域（如"热敏电阻"→ electrical + thermal），
    调用方应使用 LLM 确认最相关的域。
    """
    ct = component_type.lower()
    scores: dict[str, int] = {}

    for domain, keywords in DOMAIN_KEYWORDS.items():
        score = 0
        for kw in keywords:
            if kw in ct:
                score += 1
        if score > 0:
            scores[domain] = score

    # 按得分降序
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    domains = [d for d, _ in ranked]

    # 始终包含 syntax 域作为基础参考
    if "syntax" not in domains:
        domains.append("syntax")

    logger.info("域检测: '%s' → %s (得分: %s)", component_type, domains,
                {d: scores.get(d, 0) for d in domains})
    return domains


def get_references(
    component_type: str = "",
    stage: str = "generate",
    domains: Optional[list[str]] = None,
    max_files_per_domain: int = 2,
    code_fences: bool = True,
) -> str:
    """从官方 SysML 示例中检索最相关的参考代码。

    Args:
        component_type: 系统类型（如 "RC低通滤波器"），用于自动检测域
        stage: 使用阶段 — "generate"(生成), "review"(审查), "repair"(修正)
        domains: 手动指定域列表（如果提供，跳过自动检测）
        max_files_per_domain: 每个目录最多取几个文件（防止 prompt 过长）
        code_fences: True=用 ```sysml 包裹（generate 用），False=纯文本（review 用，避免 DeepSeek 多代码块 quirk）

    Returns:
        拼接好的参考代码字符串，可直接注入 prompt。
        如果训练目录不存在，返回带基本规范的空字符串。
    """
    # ── 解析域 ──
    if domains is None:
        if component_type:
            domains = detect_domain(component_type)
        else:
            domains = ["syntax"]  # 无组件类型 → 只给语法参考

    # ── 解析目录 ──
    directories = []
    for domain in domains:
        dirs = DOMAIN_MAP.get(domain, [])
        directories.extend(dirs)

    # 去重保序
    seen = set()
    directories = [d for d in directories if not (d in seen or seen.add(d))]

    # ── 收集文件 ──
    files = _get_example_files(directories)
    if not files:
        logger.warning("未找到任何匹配的 SysML 示例文件，domains=%s", domains)
        return _fallback_reference()

    # ── 限制数量 ──
    if max_files_per_domain > 0 and len(files) > max_files_per_domain * len(directories):
        # 每个目录取前 max_files_per_domain 个文件
        limited = []
        dir_counts: dict[str, int] = {}
        for f in files:
            dir_name = f.parent.name
            count = dir_counts.get(dir_name, 0)
            if count < max_files_per_domain:
                limited.append(f)
                dir_counts[dir_name] = count + 1
        files = limited

    # ── 拼接 ──
    header = STAGE_HEADERS.get(stage, STAGE_HEADERS["generate"])
    parts = [header]

    current_dir = ""
    for f in files:
        dir_name = f.parent.name
        if dir_name != current_dir:
            current_dir = dir_name
            parts.append(f"\n### {dir_name}\n")
        content = _read_file_cached(str(f))
        if content:
            if code_fences:
                parts.append(f"```sysml\n{content.strip()}\n```\n")
            else:
                parts.append(f"\n--- {f.name} ---\n{content.strip()}\n")

    result = "\n".join(parts)
    logger.info(
        "检索完成: component='%s', stage='%s', domains=%s, files=%s, chars=%s",
        component_type, stage, domains, len(files), len(result),
    )
    return result


def get_references_llm_select(
    component_type: str,
    stage: str = "generate",
    code_fences: bool = True,
) -> tuple[str, list[str]]:
    """LLM 辅助选择最相关的 2 个参考示例（解决域歧义）。

    当 detect_domain() 返回多个域时（如 component_type="热敏电阻" 同时命中
    electrical 和 thermal），用一次轻量 LLM 调用（temperature=0, max_tokens=128）
    确认最相关的域，避免贴错示例干扰 LLM。

    Returns:
        (references_text, selected_domains): 拼接的参考代码和最终选择的域列表

    注意: 这个函数会发起 LLM 调用。如果 LLM 调用失败，回退到 Python 关键词排序。
    """
    candidate_domains = detect_domain(component_type)

    # 如果只有一个非 syntax 域，直接返回
    non_syntax = [d for d in candidate_domains if d != "syntax"]
    if len(non_syntax) <= 1:
        return get_references(component_type, stage, domains=candidate_domains,
                             code_fences=code_fences), candidate_domains

    # ── 多个域冲突 → LLM 轻量确认 ──
    try:
        from src.llm_client import chat, user_msg

        domain_descriptions = []
        for d in non_syntax:
            keywords = DOMAIN_KEYWORDS.get(d, [])[:8]
            domain_descriptions.append(f"- {d}: {', '.join(keywords)}")

        prompt = (
            f"系统类型: \"{component_type}\"\n\n"
            f"候选域（每个域的关键词参考）:\n"
            + "\n".join(domain_descriptions) +
            f"\n\n请选择最匹配的 1-2 个域。只输出域名的 JSON 数组，如 [\"electrical\"]。"
            f"\n不要输出其他内容。"
        )

        result = chat([user_msg(prompt)], temperature=0.0, max_tokens=128).strip()
        # 提取 JSON 数组
        import json
        match = re.search(r"\[.*?\]", result, re.DOTALL)
        if match:
            selected = json.loads(match.group())
            selected = [d for d in selected if d in non_syntax]
            if selected:
                # syntax 始终带上
                if "syntax" not in selected:
                    selected.append("syntax")
                logger.info("LLM 域选择: '%s' → %s (候选=%s)", component_type, selected, non_syntax)
                return get_references(component_type, stage, domains=selected,
                                     code_fences=code_fences), selected

    except Exception as e:
        logger.warning("LLM 域选择失败，回退 Python 排序: %s", e)

    # Fallback: 用 Python 分数最高的域
    return get_references(component_type, stage, domains=candidate_domains,
                         code_fences=code_fences), candidate_domains


def _fallback_reference() -> str:
    """训练目录不可用时的最小语法参考。"""
    return """
## SysML v2 基本语法规范（官方示例不可用，使用内置规范）

标准 import:
```
private import ScalarValues::*;
```

标准 part def:
```
part def ComponentName {
    attribute paramName : Real;
    port p : PortType;
    port n : PortType;
}
```

标准 connect:
```
connect component_a.port_x to component_b.port_y;
```

禁止写法:
- `import ISQ::*` 或 `import SI::*` — 应改为 `private import ScalarValues::*`
- `:> ISQ::xxx` 或 `:> ScalarValues::xxx` — 应改为 `: Real`
- 缺少 package 声明
- 花括号不匹配
"""
