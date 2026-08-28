"""V7 根因分析样本收集（可选任务④）。

背景:
  V6 的 LLM 根因分析只在**仿真/编译失败**时才产出 `mo.root_cause`。
  如果 3 个成功用例跑下来，根本没有根因样本，"根因分析准确率"无从算起。

  这个脚本构造一组**故意会失败**的用例（缺参数 / 拓扑错），专门触发
  失败路径 → 收集 root_cause 样本 → 落盘，供后续人工/LLM 标注判断对错。

⚠️ 这个脚本只跑 V6（V4 没有 LLM 根因分析，是关键词匹配，不在评估范围内）。
   它不影响 A/B 对比（A/B 用正常用例，这个用失败用例，两者分开）。

用法:
    python experiments/collect_root_causes.py
"""

import json
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_EXP_DIR = Path(__file__).resolve().parent          # week8/experiments
_SRC_DIR = _EXP_DIR.parent / "src"                   # week8/src
_RESULTS_ROOT = _EXP_DIR.parent / "results"          # week8/results

if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from benchmark import run_version  # noqa: E402

# ── 故意构造的失败用例（raw_input 本身制造缺陷）──
# 每个用例标注 expected_root_cause（人工已知的"真因"），用于算准确率
FAILURE_CASES: list[dict] = [
    {
        "id": "fail_missing_resistor",
        "raw_input": "设计一个 RC 低通滤波器，截止频率 1kHz，输入电压 5V 阶跃信号（故意不提供电阻值）",
        "expected_root_cause": "missing_parameters",
    },
    {
        "id": "fail_bad_topology",
        "raw_input": "设计一个 RLC 串联电路，但要求电容两端短接、电感开路，拓扑自相矛盾无法工作",
        "expected_root_cause": "sysml_topology",
    },
]


def main():
    print("=" * 60)
    print("V7 根因分析样本收集（构造失败用例，只跑 V6）")
    print("=" * 60)

    results_dir = _RESULTS_ROOT / "root_causes"
    results_dir.mkdir(parents=True, exist_ok=True)

    samples = []
    for fc in FAILURE_CASES:
        # 用 --raw-input 覆盖标准用例，触发失败路径
        result = run_version("v6", fc["id"], results_dir, raw_input=fc["raw_input"])

        sample = {
            "case_id": fc["id"],
            "raw_input": fc["raw_input"],
            "expected_root_cause": fc["expected_root_cause"],
            "actual_root_cause": result.get("root_cause"),
            "root_cause_detail": result.get("root_cause_detail"),
            "success": result.get("success"),
            "mo_attempts": result.get("mo_attempts"),
            "repair_count": result.get("repair_count"),
            "error": result.get("error"),
        }
        samples.append(sample)

        match = sample["actual_root_cause"] == sample["expected_root_cause"]
        print(f"\n  {fc['id']}: 期望={fc['expected_root_cause']} "
              f"实际={sample['actual_root_cause']} {'✅' if match else '❌'}")

    out_path = results_dir / "root_cause_samples.json"
    out_path.write_text(json.dumps(samples, ensure_ascii=False, indent=2), encoding="utf-8")

    matched = sum(1 for s in samples if s["actual_root_cause"] == s["expected_root_cause"])
    print(f"\n根因分析准确率: {matched}/{len(samples)}（样本过少时仅供参考，需人工复核）")
    print(f"样本已保存: {out_path}")


if __name__ == "__main__":
    main()
