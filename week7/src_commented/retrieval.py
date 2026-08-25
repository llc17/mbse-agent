"""
=============================================================================
retrieval.py — SysML 检索层（V6 核心基础设施）
=============================================================================
定位: V6 最重要的新基础设施——决定审查质量的基础。
用途:
  - 从 42 个官方 SysML v2 Training 示例中按系统域自动匹配最相关的 .sysml 文件
  - 拼接为参考代码注入 prompt，让 LLM 有"标准答案"可以对照
  - 解决 V4 "LLM 凭记忆写 SysML，不知道官方标准长什么样"的问题

核心设计（对齐 V6 Prompt 设计规范）:
  1. Python 写死映射规则，不用 LLM 猜（确定性、可验证）
  2. 域冲突时（如"热敏电阻"）用一次轻量 LLM 调用消歧（temperature=0, max_tokens=128）
  3. stage 参数控制输出格式: generate=完整示例 / review=规范对照 / repair=语法修正

检索规则（来自 V6 启动 prompt 第92-96行）:
  RC/RLC/运放 → 07_Parts, 09_Connections, 10_Ports
  热传导     → 08_Items, 13_Flows
  机械       → 07_Parts, 09_Connections, 08_Items
  通用/语法  → 01_Packages, 02_Part Definitions, 03_Generalization

用法:
    from src.retrieval import get_references, detect_domain
    refs = get_references("RC低通滤波器", stage="generate")
=============================================================================
"""

# ---------------------------------------------------------------------------
# 第 1 层: 导入依赖
# ---------------------------------------------------------------------------
import logging                           # 日志
import re                                # 正则（LLM 返回中提取 JSON）
from pathlib import Path                 # 跨平台路径
from functools import lru_cache          # 文件读取缓存（避免重复读磁盘）
from typing import Optional              # 可选类型


logger = logging.getLogger("retrieval")

# ---------------------------------------------------------------------------
# 第 2 层: 全局配置
# ---------------------------------------------------------------------------

# 官方 SysML v2 Training 示例目录（42 个文件，由 OMG 官方提供）
_TRAINING_DIR = Path(r"D:\sysml-v2-official\sysml\src\training")

# ---------------------------------------------------------------------------
# 第 3 层: 域 → 示例目录映射（Python 写死，确定性规则）
# ---------------------------------------------------------------------------
# 为什么写死？——如果让 LLM 猜，又变成了"凭记忆"，跟 V4 没区别。
# 这些映射来自对 42 个官方示例目录内容的分析：
#   07_Parts     = part/part def 的用法
#   09_Connections = connect 语法
#   10_Ports     = port def / port 用法
#   08_Items     = item def（热/流体域常用）
#   13_Flows     = flow def / flow usage（热传导建模用）
# ---------------------------------------------------------------------------

DOMAIN_MAP: dict[str, list[str]] = {
    # 电气域: 电阻/电容/电感/运放 等电子元件建模
    "electrical": [
        "07. Parts",                      # part def 定义（元件声明）
        "09. Connections",                # connect 连接语法
        "10. Ports",                      # port 端口定义
        "11. Interfaces",                 # interface 接口定义
        "12. Binding Connectors",         # 绑定连接器
    ],
    # 热域: 热阻/热容/恒温源 等热系统建模
    "thermal": [
        "08. Items",                      # item 定义（热/流体域常用）
        "13. Flows",                      # flow 定义（热量流动建模）
        "07. Parts",                      # 基础组件定义
    ],
    # 机械域（预留给未来扩展）
    "mechanical": [
        "07. Parts",
        "09. Connections",
        "08. Items",
    ],
    # 语法基础（所有域都需要的基本语法参考）
    "syntax": [
        "01. Packages",                   # package 声明结构
        "02. Part Definitions",           # 基础 part def 写法
        "03. Generalization",             # specializes / :> 继承语法
        "04. Subsetting",                 # 子集化
        "05. Redefinition",               # redefines 重定义
        "06. Enumeration Definitions",    # enum 枚举
    ],
}

# ---------------------------------------------------------------------------
# 第 4 层: 域检测关键词（用于 Python 自动检测域）
# ---------------------------------------------------------------------------
# detect_domain() 用这些关键词在 component_type 中做子串匹配
# 匹配得分 = 命中的关键词数量，得分高的域优先
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# 第 5 层: Stage 说明文本（注入到返回的参考代码前）
# ---------------------------------------------------------------------------
# 不同 stage 需要不同类型的"参考标准":
#   generate: "照这个格式写"
#   review:   "对照这个格式挑"
#   repair:   "按这个格式改"
# ---------------------------------------------------------------------------

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
# 第 6 层: 文件读取缓存（避免重复读磁盘）
# ============================================================================

@lru_cache(maxsize=128)                  # 最多缓存 128 个文件的读取结果
def _read_file_cached(path: str) -> str:
    """缓存读取单个 .sysml 文件。LRU 策略自动淘汰最久未用的。"""
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("读取示例文件失败: %s — %s", path, e)
        return ""


def _get_example_files(directories: list[str]) -> list[Path]:
    """
    根据目录名列表，返回所有 .sysml 文件的 Path 列表。

    Args:
        directories: 如 ["07. Parts", "09. Connections"]

    Returns:
        文件 Path 对象列表（按文件名排序，保证稳定顺序）
    """
    files = []
    for dir_name in directories:
        dir_path = _TRAINING_DIR / dir_name
        if not dir_path.exists():
            logger.warning("SysML 示例目录不存在: %s", dir_path)
            continue
        # 按文件名排序 → 每次检索结果一致（确定性）
        for f in sorted(dir_path.glob("*.sysml")):
            files.append(f)
    return files


# ============================================================================
# 第 7 层: 公开 API — 域检测
# ============================================================================

def detect_domain(component_type: str) -> list[str]:
    """
    检测系统类型所属的物理域。

    算法: 对每个域的关键词列表做子串匹配，按命中数降序排列。

    Args:
        component_type: 如 "RC低通滤波器"、"双房间热传导"

    Returns:
        候选域列表（按匹配得分降序）。始终包含 "syntax" 作为基础参考。

    例:
        detect_domain("RC低通滤波器") → ["electrical", "syntax"]
        detect_domain("热敏电阻")     → ["electrical", "thermal", "syntax"]  # 冲突！
    """
    ct = component_type.lower()
    scores: dict[str, int] = {}

    # 遍历所有域，累计关键词命中数
    for domain, keywords in DOMAIN_KEYWORDS.items():
        score = 0
        for kw in keywords:
            if kw in ct:                # 子串匹配（case-insensitive）
                score += 1
        if score > 0:
            scores[domain] = score

    # 按得分降序排列
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    domains = [d for d, _ in ranked]

    # 语法域始终作为基础参考（所有人都需要基本语法）
    if "syntax" not in domains:
        domains.append("syntax")

    logger.info("域检测: '%s' → %s (得分: %s)", component_type, domains,
                {d: scores.get(d, 0) for d in domains})
    return domains


# ============================================================================
# 第 8 层: 公开 API — 检索参考代码
# ============================================================================

def get_references(
    component_type: str = "",
    stage: str = "generate",
    domains: Optional[list[str]] = None,
    max_files_per_domain: int = 2,
    code_fences: bool = True,
) -> str:
    """
    从官方 SysML 示例中检索最相关的参考代码，拼接后注入 prompt。

    Args:
        component_type: 系统类型，用于自动检测域
        stage: 使用阶段:
               "generate" — 生成代码时的参考（带 ```sysml 包裹）
               "review"   — 审查时的对照标准（纯文本，避免 DeepSeek 空响应 quirk）
               "repair"   — 修正时的语法标准
        domains: 手动指定域列表（如果提供，跳过自动检测）
        max_files_per_domain: 每个目录最多取几个文件（默认 2，防止 prompt 过长）
        code_fences: True=用 ```sysml 包裹（generate 用）；False=纯文本（review 用）

    Returns:
        拼接好的参考代码字符串，可直接注入 prompt（通常 8000+ 字符）。
    """
    # ── 步骤 1: 解析域 ──
    if domains is None:
        if component_type:
            domains = detect_domain(component_type)
        else:
            domains = ["syntax"]          # 无组件类型 → 只给语法参考

    # ── 步骤 2: 域 → 目录名 ──
    directories = []
    for domain in domains:
        dirs = DOMAIN_MAP.get(domain, [])
        directories.extend(dirs)

    # 去重保序（set 不保序，用 seen 手动去重）
    seen = set()
    directories = [d for d in directories if not (d in seen or seen.add(d))]

    # ── 步骤 3: 收集 .sysml 文件 ──
    files = _get_example_files(directories)
    if not files:
        logger.warning("未找到任何匹配的 SysML 示例文件，domains=%s", domains)
        return _fallback_reference()     # 回退到内置基本语法规范

    # ── 步骤 4: 限制文件数量（防止 prompt 过长超出 token 限制）──
    if max_files_per_domain > 0 and len(files) > max_files_per_domain * len(directories):
        limited = []
        dir_counts: dict[str, int] = {}
        for f in files:
            dir_name = f.parent.name
            count = dir_counts.get(dir_name, 0)
            if count < max_files_per_domain:
                limited.append(f)
                dir_counts[dir_name] = count + 1
        files = limited

    # ── 步骤 5: 拼接输出 ──
    # header: stage 对应的说明文本（告诉 LLM 怎么用这些参考代码）
    header = STAGE_HEADERS.get(stage, STAGE_HEADERS["generate"])
    parts = [header]

    current_dir = ""
    for f in files:
        dir_name = f.parent.name
        # 按目录分组，每个目录加一个小标题
        if dir_name != current_dir:
            current_dir = dir_name
            parts.append(f"\n### {dir_name}\n")
        content = _read_file_cached(str(f))
        if content:
            if code_fences:
                # generate 模式: 带 ```sysml 标签（LLM 会模仿这个格式）
                parts.append(f"```sysml\n{content.strip()}\n```\n")
            else:
                # review 模式: 纯文本（避免 DeepSeek 对多代码块返回空响应）
                parts.append(f"\n--- {f.name} ---\n{content.strip()}\n")

    result = "\n".join(parts)
    logger.info(
        "检索完成: component='%s', stage='%s', domains=%s, files=%s, chars=%s",
        component_type, stage, domains, len(files), len(result),
    )
    return result


# ============================================================================
# 第 9 层: LLM 辅助域消歧
# ============================================================================

def get_references_llm_select(
    component_type: str,
    stage: str = "generate",
    code_fences: bool = True,
) -> tuple[str, list[str]]:
    """
    LLM 辅助选择最相关的参考示例（解决域歧义）。

    什么时候触发 LLM？
      detect_domain() 返回多个非语法域时（如 "热敏电阻" → [electrical, thermal]）
      → 用一次轻量 LLM 调用（temperature=0, max_tokens=128）确认最相关的域。
      → 选择错误比贴错示例的代价小得多。

    为什么不用更复杂的逻辑？
      这是 V6 风险 3（检索层可能贴错示例）的缓解方案。
      轻量调用 → 低延迟、低 token 消耗 → 即使失败也不影响主流程。

    Returns:
        (references_text, selected_domains): 拼接的参考代码和最终选择的域列表
    """
    candidate_domains = detect_domain(component_type)

    # 只有一个非语法域 → 不需要 LLM，直接返回
    non_syntax = [d for d in candidate_domains if d != "syntax"]
    if len(non_syntax) <= 1:
        return get_references(component_type, stage, domains=candidate_domains,
                             code_fences=code_fences), candidate_domains

    # ── 多个域冲突 → LLM 轻量确认 ──
    try:
        from src.llm_client import chat, user_msg

        # 列出每个域的典型关键词，帮助 LLM 判断
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
        # 提取 [ ... ] JSON 数组
        import json
        match = re.search(r"\[.*?\]", result, re.DOTALL)
        if match:
            selected = json.loads(match.group())
            selected = [d for d in selected if d in non_syntax]
            if selected:
                if "syntax" not in selected:
                    selected.append("syntax")
                logger.info("LLM 域选择: '%s' → %s (候选=%s)",
                           component_type, selected, non_syntax)
                return get_references(component_type, stage, domains=selected,
                                     code_fences=code_fences), selected

    except Exception as e:
        logger.warning("LLM 域选择失败，回退 Python 排序: %s", e)

    # Fallback: 用 Python 分数最高的域（确定性回退，不阻塞主流程）
    return get_references(component_type, stage, domains=candidate_domains,
                         code_fences=code_fences), candidate_domains


# ============================================================================
# 第 10 层: 回退机制 — 训练目录不可用时的最小语法参考
# ============================================================================

def _fallback_reference() -> str:
    """
    训练目录不可用时的最小语法参考。

    发生条件: D:\sysml-v2-official\sysml\src\training\ 不存在或为空。
    此时不抛异常，而是返回内置的 SysML v2 基本语法规范。
    """
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
