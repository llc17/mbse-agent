"""
Agent 三阶段循环 — V6 核心编排模式。

把每个流水线节点的"一次 LLM 调用"升级为"生成→审查→修正"的三阶段 Agent 循环。
同一 LLM 换不同 system prompt 扮演不同角色（生成者/审查者/修正者）。

核心组件:
  - ReviewResult: 结构化审查结果（对齐 V6 Prompt 设计规范：JSON 输出）
  - run_review_loop(): 三阶段循环编排器（含断路器，max 3 轮）
  - parse_review_json(): 从 LLM 返回中提取审查 JSON

审查 JSON 规范（强制，来自 V6 Prompt 设计规范）:
  {
    "ok": true/false,           // 是否通过审查（通过则跳过修正步骤）
    "score": 0-100,            // 质量评分（可选，用于 A/B 对比）
    "issues": [
      {
        "severity": "fatal" | "error" | "warning",
        "category": "syntax" | "semantics" | "consistency" | "completeness",
        "description": "具体问题描述",
        "location": "代码中出问题的位置（如行号或代码片段）",
        "suggestion": "具体修改建议"
      }
    ],
    "summary": "审查总结"
  }

用法:
    from src.agent_loop import run_review_loop, ReviewResult

    result = run_review_loop(
        generate_fn=lambda: chat(generate_messages, ...),
        review_fn=lambda output: ReviewResult(...),
        revise_fn=lambda output, issues: chat(revise_messages, ...),
        max_rounds=3,
    )
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger("agent_loop")


# ============================================================================
# 数据结构
# ============================================================================

@dataclass
class ReviewIssue:
    """单条审查发现的问题。"""
    severity: str = "warning"        # fatal | error | warning
    category: str = "semantics"      # syntax | semantics | consistency | completeness
    description: str = ""
    location: str = ""              # 代码中的位置
    suggestion: str = ""            # 修改建议


@dataclass
class ReviewResult:
    """一次审查的完整结果。"""
    ok: bool = True                 # 是否通过审查
    score: int = 100                # 0-100 质量评分
    issues: list[ReviewIssue] = field(default_factory=list)
    summary: str = ""               # 审查总结
    raw_response: str = ""          # LLM 原始返回（调试用）

    @property
    def fatal_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "fatal")

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity in ("fatal", "error"))

    @property
    def has_issues(self) -> bool:
        return not self.ok or len(self.issues) > 0


@dataclass
class AgentLoopResult:
    """三阶段循环的完整结果。"""
    final_output: str               # 最终输出（通过审查或被断路器截断）
    rounds: int                     # 实际执行轮数
    review_history: list[ReviewResult] = field(default_factory=list)
    passed: bool = False            # 是否通过最终审查
    total_time: float = 0.0         # 总耗时


# ============================================================================
# 审查 JSON 解析
# ============================================================================

def parse_review_json(raw_text: str) -> ReviewResult:
    """从 LLM 返回中解析审查 JSON。

    容错设计（多级 fallback）:
      1. 从 ```json ... ``` markdown 代码块提取
      2. 从 ``` ... ``` 任意代码块提取
      3. 贪婪正则匹配 { ... } JSON 对象
      4. 手工括号计数 —— 找到第一个 { 和对应的 }
      5. 全部失败 → 放行（ok=True），不阻塞流水线

    注意: JSON 解析失败时设为 ok=True（而非 ok=False），防止无意义的修正循环。
    如果 LLM 真的发现问题，它会在 JSON 中说清楚；
    如果 LLM 没输出 JSON，说明它没发现问题，默认通过。
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
            pass  # 继续尝试其他策略

    # ── 策略 2: 贪婪匹配 { ... } ──
    json_match = re.search(r"\{.*\}", text, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(0))
            return _parse_ok(data, raw_text)
        except json.JSONDecodeError:
            pass  # 继续尝试

    # ── 策略 3: 手工括号计数 ──
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

    # ── 全部失败 → 放行，不阻塞 ──
    logger.warning(
        "审查 JSON 解析失败 (len=%s chars, 前120字: %.120s)，默认通过",
        len(raw_text), raw_text if raw_text else "(空)"
    )
    return _make_pass_result(raw_text, "审查返回格式异常，无法解析 JSON，默认通过")


def _parse_ok(data: dict, raw_text: str) -> ReviewResult:
    """从成功解析的 JSON dict 构建 ReviewResult。"""
    ok = data.get("ok", True)
    score = int(data.get("score", 100))
    summary = data.get("summary", "")

    issues = []
    for issue_data in data.get("issues", []):
        if isinstance(issue_data, str):
            issues.append(ReviewIssue(description=issue_data))
        elif isinstance(issue_data, dict):
            issues.append(ReviewIssue(
                severity=issue_data.get("severity", "warning"),
                category=issue_data.get("category", "semantics"),
                description=issue_data.get("description", str(issue_data)),
                location=issue_data.get("location", ""),
                suggestion=issue_data.get("suggestion", ""),
            ))

    # 如果有 fatal 或 error 级别的 issue，强制 ok=False
    if any(i.severity in ("fatal", "error") for i in issues):
        ok = False

    logger.info(
        "审查解析: ok=%s, score=%s, issues=%s (fatal=%s, error=%s)",
        ok, score, len(issues),
        sum(1 for i in issues if i.severity == "fatal"),
        sum(1 for i in issues if i.severity in ("fatal", "error")),
    )

    return ReviewResult(
        ok=ok,
        score=score,
        issues=issues,
        summary=summary,
        raw_response=raw_text,
    )


def _make_pass_result(raw_text: str, reason: str) -> ReviewResult:
    """构造"默认通过"的审查结果（JSON 解析失败时用）。"""
    return ReviewResult(
        ok=True,  # ← 不阻塞流水线
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
# 三阶段循环编排器
# ============================================================================

def run_review_loop(
    generate_fn: Callable[[], str],
    review_fn: Callable[[str], ReviewResult],
    revise_fn: Callable[[str, list[ReviewIssue]], str],
    max_rounds: int = 3,
    label: str = "agent",
) -> AgentLoopResult:
    """运行"生成→审查→修正"三阶段循环。

    流程:
      1. generate_fn() → 初始输出
      2. review_fn(output) → 结构化审查结果
      3. 如果 ok → 结束（大部分情况一审就过，省 token）
      4. 如果有问题 → revise_fn(output, issues) → 新输出
      5. 回到步骤 2，最多 max_rounds 轮（断路器）

    Args:
        generate_fn: 生成函数，无参数，返回生成的文本
        review_fn: 审查函数，接收输出文本，返回 ReviewResult
        revise_fn: 修正函数，接收(输出文本, 问题列表)，返回修正后的文本
        max_rounds: 最大审查轮数（默认 3，超限转 HITL）
        label: Agent 标签（用于日志）

    Returns:
        AgentLoopResult: 包含最终输出、轮数、审查历史、是否通过
    """
    t0 = time.time()
    review_history: list[ReviewResult] = []

    # ── 第 1 步: 生成 ──
    logger.info("[%s] 阶段1 - 生成...", label)
    current_output = generate_fn()

    # ── 第 2 步: 审查→修正循环 ──
    for round_num in range(1, max_rounds + 1):
        logger.info("[%s] 阶段2 - 审查 (第%s/%s轮)...", label, round_num, max_rounds)

        review = review_fn(current_output)
        review_history.append(review)

        # 通过 → 结束
        if review.ok:
            elapsed = time.time() - t0
            logger.info(
                "[%s] 审查通过 (第%s轮, score=%s, %.1fs)，跳过修正",
                label, round_num, review.score, elapsed,
            )
            return AgentLoopResult(
                final_output=current_output,
                rounds=round_num,
                review_history=review_history,
                passed=True,
                total_time=elapsed,
            )

        # 没有可操作的问题 → 结束
        if not review.issues:
            logger.info("[%s] 审查未通过但无具体问题，视为通过", label)
            return AgentLoopResult(
                final_output=current_output,
                rounds=round_num,
                review_history=review_history,
                passed=True,
                total_time=time.time() - t0,
            )

        # 达到最大轮数 → 断路器
        if round_num >= max_rounds:
            logger.warning(
                "[%s] 断路器: %s轮审查未通过 (score=%s, issues=%s)，标记问题转 HITL",
                label, max_rounds, review.score, len(review.issues),
            )
            break

        # ── 第 3 步: 修正 ──
        logger.info(
            "[%s] 阶段3 - 修正 (第%s轮, issues=%s)...",
            label, round_num, len(review.issues),
        )
        current_output = revise_fn(current_output, review.issues)

    # 断路器截断
    elapsed = time.time() - t0
    return AgentLoopResult(
        final_output=current_output,
        rounds=max_rounds,
        review_history=review_history,
        passed=False,
        total_time=elapsed,
    )


# ============================================================================
# Prompt 构建工具（遵循 V6 Prompt 写作规范）
# ============================================================================

def build_review_prompt(
    role_description: str,
    checklist: list[str],
    output_label: str = "生成的代码",
    references: str = "",
    output_format_note: str = "",
) -> str:
    """构建符合 V6 规范的审查 prompt。

    规范要求（来自 v6-agent-prompt-design）:
      ❌ 禁止: "你是 XX 专家，检查这份代码"
      ✅ 必须: "你是 XX，逐条检查：1... 2... 3...。输出 JSON: {...}"

    Args:
        role_description: 审查者角色描述（如 "SysML v2 语法审查员"）
        checklist: 具体检查项列表（3-7 条，每条有明确判断标准）
        output_label: 被审查内容的标签（如 "SysML 代码"）
        references: 参考标准（如官方示例代码）
        output_format_note: 额外的输出格式说明

    Returns:
        完整的审查 prompt 文本
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
{{
  "ok": true/false,
  "score": 0-100,
  "issues": [
    {{
      "severity": "fatal|error|warning",
      "category": "syntax|semantics|consistency|completeness",
      "description": "具体问题描述",
      "location": "出问题的代码位置",
      "suggestion": "具体修改建议"
    }}
  ],
  "summary": "审查总结（一句话）"
}}
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
    """构建修正 prompt。

    Args:
        role_description: 修正者角色
        original_output: 原始输出
        issues: 需要修正的问题列表
        output_label: 输出内容的标签
        references: 修正参考标准

    Returns:
        修正 prompt 文本
    """
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
