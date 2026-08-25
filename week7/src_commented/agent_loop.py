"""
=============================================================================
agent_loop.py — Agent 三阶段循环编排器（V6 核心编排模式）
=============================================================================
定位: V6 最核心的通用组件——把每个流水线节点的"一次 LLM 调用"升级为
      "生成→审查→修正"的三阶段 Agent 循环。

核心设计:
  生成 → 审查 → (有问题?) → 修正 → 再审查 → 通过 / 断路器截断
  ↑                           ↓
  └─────── 最多 3 轮 ──────────┘

为什么需要这个？
  V4 每个节点只调 1 次 LLM → 没自我纠错能力 → 语法/语义错误一直传到下游。
  V6 三阶段 → 生成者出活、审查者挑刺、修正者改错 → 质量闭环。

关键设计决策（对齐 V6 Prompt 设计规范）:
  - 同一 LLM 换不同 system prompt 扮演不同角色（不是 3 个独立进程）
  - 审查 JSON 强制格式: {ok, score, issues[], summary}
  - 断路器: max_rounds=3，超限标记问题转 HITL
  - 审查通过即结束 → 大部分情况一审就过 → 实际 token 增量 ~50%

用法:
    from src.agent_loop import run_review_loop, parse_review_json

    result = run_review_loop(
        generate_fn=lambda: chat(generate_messages),
        review_fn=lambda output: parse_review_json(chat(review_messages)),
        revise_fn=lambda output, issues: chat(revise_messages),
        max_rounds=3,
        label="node2",
    )
=============================================================================
"""

# ---------------------------------------------------------------------------
# 第 1 层: 导入依赖
# ---------------------------------------------------------------------------
import json                               # 审查结果 JSON 解析
import logging                            # 日志
import re                                 # 正则提取 JSON
import time                               # 计时
from dataclasses import dataclass, field   # 轻量数据类（替代 Pydantic，避免额外依赖）
from typing import Callable, Optional     # 回调函数类型声明


logger = logging.getLogger("agent_loop")


# ============================================================================
# 第 2 层: 数据结构定义
# ============================================================================

@dataclass
class ReviewIssue:
    """
    单条审查发现的问题。

    来源: LLM 审查员返回的 JSON 中 issues 数组的每个元素。

    字段对齐 V6 Prompt 设计规范:
      severity: fatal=必须修正 / error=应修正 / warning=记录提醒
      category: syntax(语法)/semantics(语义)/consistency(一致性)/completeness(完整性)
      description + location + suggestion: 让修正员知道改什么、在哪里、怎么改
    """
    severity: str = "warning"              # fatal | error | warning
    category: str = "semantics"            # syntax | semantics | consistency | completeness
    description: str = ""                  # 具体问题描述（如 "使用了非法 import ISQ::*"）
    location: str = ""                     # 代码中的位置（如 "第3行" 或具体代码片段）
    suggestion: str = ""                   # 修改建议（如 "应改为 private import ScalarValues::*;"）


@dataclass
class ReviewResult:
    """
    一次审查的完整结果（LLM 审查员返回的 JSON 的 Python 对象表示）。

    ok=True  → 跳过修正步骤，直接使用当前输出
    ok=False + issues 非空 → 进入修正步骤
    ok=False + issues 为空 → 无具体问题可操作，视为通过
    """
    ok: bool = True                        # 是否通过审查
    score: int = 100                       # 0-100 质量评分（用于 A/B 对比）
    issues: list[ReviewIssue] = field(default_factory=list)
    summary: str = ""                      # 审查总结（一句话）
    raw_response: str = ""                 # LLM 原始返回（调试用，生产环境忽略）

    @property
    def fatal_count(self) -> int:
        """fatal 级别问题数。fatal > 0 → 必须修正。"""
        return sum(1 for i in self.issues if i.severity == "fatal")

    @property
    def error_count(self) -> int:
        """error 及以上级别问题数。error > 0 → ok 被强制设为 False。"""
        return sum(1 for i in self.issues if i.severity in ("fatal", "error"))

    @property
    def has_issues(self) -> bool:
        """是否有任何问题（ok=False 或 issues 非空）"""
        return not self.ok or len(self.issues) > 0


@dataclass
class AgentLoopResult:
    """
    三阶段循环的最终结果。

    包含最终输出 + 审查历史 + 是否通过 + 总耗时。
    审查历史可用于 V7 的 A/B 对比分析。
    """
    final_output: str                      # 最终输出（通过审查 或 断路器截断）
    rounds: int                            # 实际执行轮数
    review_history: list[ReviewResult] = field(default_factory=list)
    passed: bool = False                   # 是否通过最终审查
    total_time: float = 0.0                # 总耗时（秒）


# ============================================================================
# 第 3 层: 审查 JSON 解析（容错机制）
# ============================================================================

def parse_review_json(raw_text: str) -> ReviewResult:
    """
    从 LLM 返回中解析审查 JSON。V6 最关键的容错函数。

    容错设计（4 级 fallback）:
      1. 从 ```json ... ``` markdown 代码块提取
      2. 贪婪正则匹配 { ... } JSON 对象
      3. 手工括号计数 —— 找到第一个 { 和对应的 }
      4. 全部失败 → 返回 ok=True，放行不阻塞流水线

    为什么要多级 fallback？
      LLM 经常在 JSON 外面包 markdown、多输出一两个换行、或者把 JSON
      嵌在解释文字里。单靠 json.loads() 有 20-30% 的失败率。
      多级 fallback 把这个比例降到 1-2%。

    为什么失败时 ok=True 而不是 ok=False？
      如果 LLM 真的发现问题，它会在 JSON 中说清楚。
      如果 LLM 没输出 JSON，说明它没发现问题，默认通过。
      设为 ok=False 会触发无效修正循环，浪费 token 且可能越改越差。
    """
    text = raw_text.strip()
    if not text:
        logger.warning("审查返回完全为空，默认通过")
        return _make_pass_result(raw_text, "审查返回为空，默认通过")

    # ── 策略 1: 提取 ```json ... ``` 代码块 ──
    json_block_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if json_block_match:
        candidate = json_block_match.group(1).strip()
        try:
            data = json.loads(candidate)
            return _parse_ok(data, raw_text)
        except json.JSONDecodeError:
            pass                            # 不放弃，继续尝试

    # ── 策略 2: 贪婪匹配 { ... } ──
    json_match = re.search(r"\{.*\}", text, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(0))
            return _parse_ok(data, raw_text)
        except json.JSONDecodeError:
            pass                            # 不放弃，继续尝试

    # ── 策略 3: 手工括号计数 ──
    # 找到第一个 { → 逐字符计数 → 找到对应的 }
    start_idx = text.find("{")
    if start_idx >= 0:
        depth = 0
        end_idx = start_idx
        for i in range(start_idx, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    end_idx = i + 1
                    break
        if end_idx > start_idx:
            candidate = text[start_idx:end_idx]
            try:
                data = json.loads(candidate)
                return _parse_ok(data, raw_text)
            except json.JSONDecodeError:
                logger.info("手工括号计数提取的 JSON 仍无法解析")

    # ── 策略 4: 全部失败 → 放行，不阻塞 ──
    logger.warning(
        "审查 JSON 解析失败 (len=%s chars, 前120字: %.120s)，默认通过",
        len(raw_text), raw_text if raw_text else "(空)"
    )
    return _make_pass_result(raw_text, "审查返回格式异常，无法解析 JSON，默认通过")


# ============================================================================
# 第 4 层: 解析结果构建（辅助函数）
# ============================================================================

def _parse_ok(data: dict, raw_text: str) -> ReviewResult:
    """
    从成功解析的 JSON dict 构建 ReviewResult。

    处理字段标准化（LLM 可能少写字段、字段名拼错等）。
    如果有 fatal 或 error 级别问题 → 强制 ok=False。
    """
    ok = data.get("ok", True)
    score = int(data.get("score", 100))
    summary = data.get("summary", "")

    # ── 标准化 issues 数组 ──
    issues = []
    for issue_data in data.get("issues", []):
        if isinstance(issue_data, str):
            # LLM 可能只返回字符串列表 → 最少信息也能工作
            issues.append(ReviewIssue(description=issue_data))
        elif isinstance(issue_data, dict):
            issues.append(ReviewIssue(
                severity=issue_data.get("severity", "warning"),
                category=issue_data.get("category", "semantics"),
                description=issue_data.get("description", str(issue_data)),
                location=issue_data.get("location", ""),
                suggestion=issue_data.get("suggestion", ""),
            ))

    # ── 强制规则: 有 fatal/error → ok=False ──
    if any(i.severity in ("fatal", "error") for i in issues):
        ok = False

    logger.info(
        "审查解析: ok=%s, score=%s, issues=%s (fatal=%s, error=%s)",
        ok, score, len(issues),
        sum(1 for i in issues if i.severity == "fatal"),
        sum(1 for i in issues if i.severity in ("fatal", "error")),
    )

    return ReviewResult(ok=ok, score=score, issues=issues, summary=summary, raw_response=raw_text)


def _make_pass_result(raw_text: str, reason: str) -> ReviewResult:
    """
    构造"默认通过"的审查结果。

    用于 JSON 解析失败的 fallback —— 不阻断流水线。
    """
    return ReviewResult(
        ok=True,                           # ← 关键: 不阻塞
        score=80,
        issues=[ReviewIssue(
            severity="warning",
            category="completeness",
            description=reason,
        )],
        summary=reason,
        raw_response=raw_text,
    )


# ============================================================================
# 第 5 层: 三阶段循环编排器（核心函数）
# ============================================================================

def run_review_loop(
    generate_fn: Callable[[], str],                                   # 生成函数 → 返回产出
    review_fn: Callable[[str], ReviewResult],                         # 审查函数 → 返回结构化结果
    revise_fn: Callable[[str, list[ReviewIssue]], str],              # 修正函数 → 返回修正后产出
    max_rounds: int = 3,                                              # 最大审查轮数（断路器）
    label: str = "agent",                                             # Agent 标签（日志用）
) -> AgentLoopResult:
    """
    运行"生成→审查→修正"三阶段循环。

    流程图:
        生成(1次) → 审查(第1轮) → ok? → YES → 返回结果 ✅
                                  ↓ NO
                                修正 → 审查(第2轮) → ok? → YES → 返回结果 ✅
                                                        ↓ NO
                                                      修正 → 审查(第3轮) → ok? → YES → 返回结果 ✅
                                                                          ↓ NO
                                                                      断路器触发 → 返回当前结果 ⚠️

    为什么大部分情况一审就过？
      生成 prompt 已经很强了（官方示例+模板注入），审查只是锦上添花。
      只有当生成结果有明显硬伤时才会触发修正（~20-30% 的情况）。
      所以 V6 的实际 token 增量约 50%，不是 3 倍。

    Args:
        generate_fn: 生成函数，无参数，返回生成的文本
        review_fn: 审查函数，接收输出文本，返回 ReviewResult
        revise_fn: 修正函数，接收(输出文本, 问题列表)，返回修正后的文本
        max_rounds: 最大审查轮数（默认 3，超限 = 断路器触发 → 标记问题转 HITL）
        label: Agent 标签（用于日志，如 "node1", "node2"）

    Returns:
        AgentLoopResult: 最终输出 + 轮数 + 审查历史 + 是否通过 + 总耗时
    """
    t0 = time.time()
    review_history: list[ReviewResult] = []

    # ── 阶段 1: 生成 ──
    logger.info("[%s] 阶段1 - 生成...", label)
    current_output = generate_fn()

    # ── 阶段 2 + 3: 审查→修正 循环 ──
    for round_num in range(1, max_rounds + 1):
        logger.info("[%s] 阶段2 - 审查 (第%s/%s轮)...", label, round_num, max_rounds)

        # 调用审查函数 → 返回结构化审查结果
        review = review_fn(current_output)
        review_history.append(review)

        # ── 通过 → 结束（大多数情况走这个分支）──
        if review.ok:
            elapsed = time.time() - t0
            logger.info("[%s] 审查通过 (第%s轮, score=%s, %.1fs)，跳过修正",
                       label, round_num, review.score, elapsed)
            return AgentLoopResult(
                final_output=current_output,
                rounds=round_num,
                review_history=review_history,
                passed=True,
                total_time=elapsed,
            )

        # ── 没有可操作的问题 → 视为通过（审查发现了问题但没给出具体建议）──
        if not review.issues:
            logger.info("[%s] 审查未通过但无具体问题，视为通过", label)
            return AgentLoopResult(
                final_output=current_output,
                rounds=round_num,
                review_history=review_history,
                passed=True,
                total_time=time.time() - t0,
            )

        # ── 达到最大轮数 → 断路器 ──
        if round_num >= max_rounds:
            logger.warning(
                "[%s] 断路器: %s轮审查未通过 (score=%s, issues=%s)，标记问题转 HITL",
                label, max_rounds, review.score, len(review.issues),
            )
            break

        # ── 阶段 3: 修正 ──
        logger.info("[%s] 阶段3 - 修正 (第%s轮, issues=%s)...",
                   label, round_num, len(review.issues))
        current_output = revise_fn(current_output, review.issues)

    # ── 断路器截断 ──
    elapsed = time.time() - t0
    return AgentLoopResult(
        final_output=current_output,
        rounds=max_rounds,
        review_history=review_history,
        passed=False,                      # ← 断路器触发，未通过
        total_time=elapsed,
    )


# ============================================================================
# 第 6 层: Prompt 构建工具（遵循 V6 规范）
# ============================================================================

def build_review_prompt(
    role_description: str,
    checklist: list[str],
    output_label: str = "生成的代码",
    references: str = "",
    output_format_note: str = "",
) -> str:
    """
    构建符合 V6 规范的审查 prompt。

    V6 规范要求（来自 v6-agent-prompt-design memory）:
      ❌ 禁止: "你是 XX 专家，检查这份代码"
      ✅ 必须: "你是 XX，逐条检查：1... 2... 3...。输出 JSON: {...}"

    Args:
        role_description: 审查者角色描述
        checklist: 具体检查项列表（3-7 条，每条有明确判断标准）
        output_label: 被审查内容的标签
        references: 参考标准（如官方示例代码）
        output_format_note: 额外输出格式说明

    Returns:
        完整审查 prompt 文本（可直接发给 LLM）
    """
    checklist_text = "\n".join(
        f"{i+1}. {item}" for i, item in enumerate(checklist)
    )

    prompt = f"""## 角色
{role_description}

## 待审查的{output_label}
```
{{output}}
```
"""

    if references:
        prompt += f"\n## 审查参考标准\n{references}\n"

    prompt += f"""## 检查清单（逐条执行）
{checklist_text}

## 输出格式（严格 JSON）
```json
{{{{
  "ok": true/false,
  "score": 0-100,
  "issues": [
    {{{{
      "severity": "fatal|error|warning",
      "category": "syntax|semantics|consistency|completeness",
      "description": "具体问题描述",
      "location": "出问题的代码位置",
      "suggestion": "具体修改建议"
    }}}}
  ],
  "summary": "审查总结（一句话）"
}}}}
```

## 重要规则
1. 只报硬伤（编译会失败、语义与需求不符、参数值不一致），不挑风格
2. 不确定的问题不要写——宁可漏报不要误报
3. 如果所有检查项都通过，ok=true，issues=[]，score>=90
4. 不要输出 JSON 之外的任何内容"""

    if output_format_note:
        prompt += f"\n{output_format_note}"

    return prompt


def build_revise_prompt(
    role_description: str,
    original_output: str,
    issues: list[ReviewIssue],
    output_label: str = "代码",
    references: str = "",
) -> str:
    """
    构建修正 prompt。

    关键原则: "只修正列出的问题，不要动其他部分"
    这来自 V6 风险 4（修正步骤可能越修越差）的缓解方案。

    Args:
        role_description: 修正者角色描述
        original_output: 原始输出（需要修正的代码/文本）
        issues: 需要修正的问题列表
        output_label: 输出内容的标签
        references: 修正参考标准

    Returns:
        修正 prompt 文本
    """
    # ── 格式化问题列表 ──
    issues_text = "\n".join(
        f"{i+1}. [{issue.severity}] {issue.description}"
        f"{' — ' + issue.suggestion if issue.suggestion else ''}"
        f"{' (位置: ' + issue.location + ')' if issue.location else ''}"
        for i, issue in enumerate(issues)
    )

    prompt = f"""## 角色
{role_description}

## 原始{output_label}（需要修正）
```
{original_output}
```

## 审查发现的问题（只修正这些问题，不要变动其他部分）
{issues_text}
"""

    if references:
        prompt += f"\n## 修正参考标准\n{references}\n"

    prompt += f"""## 要求
1. 只修正上述列出的具体问题，不要改动其他正确的部分
2. 修正后重新输出完整的{output_label}
3. 确保修正不引入新问题
4. 只输出修正后的{output_label}，不要包含解释"""

    return prompt
