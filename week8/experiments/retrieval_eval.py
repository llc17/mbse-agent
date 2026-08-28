"""V7 检索精度提升 — 复合词术语表（任务⑦，独立验证，不改 week7）。

背景:
  V6 的 `week7/src/retrieval.py::detect_domain()` 用关键词子串匹配。
  遇到复合词会误判: `detect_domain("热敏电阻")` 同时命中 thermal（"热"）和 electrical（"电阻"），
  返回两个域，触发 LLM 选域（多花一次 token 且可能选错）。

改进: 两级域检测
  第 1 级: 复合词术语表**整体精确匹配**，命中即锁定域，不再拆字
  第 2 级: 原有关键词子串匹配（兜底）

⚠️ 关键: 这个改动**不能写进 week7/src/retrieval.py**（A/B 对比要求 V6 冻结）。
  所以这里用 monkey-patch 方式，在运行时临时替换 detect_domain，做完改前/改后的
  检索准确率对比就结束，磁盘上 week7 一行不动。

用法:
    python experiments/retrieval_eval.py
"""

import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
week7_dir = str(_PROJECT_ROOT / "week7")
if week7_dir not in sys.path:
    sys.path.insert(0, week7_dir)

from src import retrieval  # noqa: E402

# ── 复合词术语表（整体词 → 唯一域）──
# 键: 复合词整体；值: 应锁定的域。命中即返回 [该域, syntax]，不再走子串匹配。
COMPOUND_TERMS: dict[str, str] = {
    "热敏电阻": "thermal",
    "热电偶": "thermal",
    "热电堆": "thermal",
    "热电阻": "thermal",
    "光电二极管": "electrical",
    "光电晶体管": "electrical",
    "光敏电阻": "electrical",
    "压敏电阻": "electrical",
    "磁敏电阻": "electrical",
    "温差发电": "thermal",
    "热继电器": "thermal",
    "热电制冷": "thermal",
}

# ── 测试用例: (component_type, 期望域) ──
# 期望域 = 人工标注的"正确答案"，用来算检索准确率
TEST_CASES: list[tuple[str, str]] = [
    ("热敏电阻", "thermal"),
    ("热电偶", "thermal"),
    ("光电二极管", "electrical"),
    ("热电堆", "thermal"),
    ("压敏电阻", "electrical"),
    ("RC低通滤波器", "electrical"),   # 普通词，子串匹配应正常
    ("单房间热传导", "thermal"),       # 普通词
    ("反相运放", "electrical"),         # 普通词
]


def detect_domain_v2(component_type: str) -> list[str]:
    """改后的两级域检测（术语表优先 + 子串兜底）。

    与 V6 原版 detect_domain 的接口一致: 返回域列表（含 syntax）。
    """
    ct = component_type.lower()

    # ── 第 1 级: 复合词术语表整体精确匹配 ──
    for term, domain in COMPOUND_TERMS.items():
        if term in ct:  # 用 in 而非 ==，容忍"XX热敏电阻"这类带前后缀的写法
            # 命中即锁定，不再拆字
            return [domain, "syntax"]

    # ── 第 2 级: 原有关键词子串匹配（复用 V6 的 DOMAIN_KEYWORDS + 逻辑）──
    scores: dict[str, int] = {}
    for domain, keywords in retrieval.DOMAIN_KEYWORDS.items():
        score = 0
        for kw in keywords:
            if kw in ct:
                score += 1
        if score > 0:
            scores[domain] = score

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    domains = [d for d, _ in ranked]
    if "syntax" not in domains:
        domains.append("syntax")
    return domains


def _evaluate(detect_fn) -> tuple[int, int, list[dict]]:
    """用 detect_fn 跑测试集，返回 (正确数, 总数, 明细)。"""
    correct = 0
    detail = []
    for component_type, expected in TEST_CASES:
        got = detect_fn(component_type)
        # 取第一个非 syntax 域作为"主域"判断
        main = next((d for d in got if d != "syntax"), None)
        ok = main == expected
        if ok:
            correct += 1
        detail.append({
            "component_type": component_type,
            "expected": expected,
            "got_main": main,
            "got_full": got,
            "ok": ok,
        })
    return correct, len(TEST_CASES), detail


def main():
    print("=" * 60)
    print("V7 检索精度提升验证（复合词术语表）")
    print("=" * 60)

    # ── 改前（V6 原版）──
    old_correct, total, old_detail = _evaluate(retrieval.detect_domain)

    # ── 改后（术语表 + 子串兜底）──
    new_correct, total, new_detail = _evaluate(detect_domain_v2)

    print(f"\n改前（V6 原版子串匹配）准确率: {old_correct}/{total} = {old_correct/total*100:.0f}%")
    print(f"改后（术语表+兜底）     准确率: {new_correct}/{total} = {new_correct/total*100:.0f}%\n")

    print("明细（★ = 改后修正了改前的误判）:")
    for old, new in zip(old_detail, new_detail):
        marker = "★" if (not old["ok"] and new["ok"]) else (" " if old["ok"] == new["ok"] else "✗")
        print(f"  {marker} {old['component_type']:12s} 期望={old['expected']:10s} "
              f"改前主域={str(old['got_main']):12s} 改后主域={str(new['got_main'])}")

    print("\n结论: 若 ★ 行存在，说明术语表修正了复合词误判；若改后仍有 ✗，说明术语表不完整。")


if __name__ == "__main__":
    main()
