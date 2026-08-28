"""V7 judge 重采样 — 对已产出的代码做多次盲评，量化 judge 打分方差。

背景:
  消融实验的方向反转（thermal 单次"检索开70<关75" vs 全量"开77>关72"）暴露了噪声问题。
  噪声有两个来源:
    1. pipeline 重跑的 LLM 生成随机性（DeepSeek temperature=0.3）
    2. judge 打分随机性（Kimi K3 temperature 固定 1.0，同一份产出每次打分都不同）

  这个脚本针对来源 2（更便宜、更直接）: 对**同一份已产出的代码**用 Kimi 评 N 次，
  看 judge 分的均值±std。如果 std 大到能覆盖 5 分的差异，说明"方向反转"是 judge 噪声，
  不能当结论。

用法:
    python experiments/resample_judge.py --result <某份ablation的json> --trials 3
    python experiments/resample_judge.py --all --trials 3   # 对全量消融的 6 份都重采样
"""

import argparse
import json
import statistics
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_EXP_DIR = Path(__file__).resolve().parent          # week8/experiments
_SRC_DIR = _EXP_DIR.parent / "src"                   # week8/src
_RESULTS_ROOT = _EXP_DIR.parent / "results"          # week8/results

if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from judge import _load_env, judge_one  # noqa: E402


def _resample_one(result: dict, trials: int) -> dict:
    """对一份产出评 N 次，返回 {scores均值, total均值, total_std, totals列表}。"""
    import time as _time
    totals = []
    scores_accum = {"syntax": [], "consistency": [], "topology": [], "traceability": []}

    for t in range(trials):
        if t > 0:
            # Kimi RPM=3，采样之间等 22s 避免限流
            print(f"    (等待 22s 避免 Kimi 限流...)", flush=True)
            _time.sleep(22)
        score = judge_one(result, references="")
        if score.get("total") is not None:
            totals.append(score["total"])
            for k in scores_accum:
                scores_accum[k].append(score.get("scores", {}).get(k, 0))
        print(f"    第{t+1}次: total={score.get('total')}", flush=True)

    out = {
        "trials": len(totals),
        "total_mean": round(statistics.mean(totals), 1) if totals else None,
        "total_std": round(statistics.stdev(totals), 1) if len(totals) >= 2 else None,
        "totals": totals,
    }
    if totals:
        for k in scores_accum:
            out[f"{k}_mean"] = round(statistics.mean(scores_accum[k]), 1)
    return out


def main():
    parser = argparse.ArgumentParser(description="V7 judge 重采样")
    parser.add_argument("--result", type=str, default=None, help="单份 ablation 结果 JSON")
    parser.add_argument("--all", action="store_true", help="对全量消融 6 份都重采样")
    parser.add_argument("--ablation-dir", type=str, default=None,
                        help="全量消融结果目录（含 ablation_results.json）")
    parser.add_argument("--trials", type=int, default=3, help="每份评几次（默认 3）")
    args = parser.parse_args()

    _load_env()

    if args.all:
        # 找到最近的全量消融 ablation_results.json
        if args.ablation_dir:
            json_path = Path(args.ablation_dir) / "ablation_results.json"
        else:
            results_root = _RESULTS_ROOT
            cands = sorted(results_root.glob("ablation_*/ablation_results.json"))
            if not cands:
                print("❌ 没找到 ablation_results.json，请用 --ablation-dir 指定")
                sys.exit(1)
            json_path = cands[-1]  # 最新的
        results = json.loads(json_path.read_text(encoding="utf-8"))
        print(f"对 {json_path} 里的 {len(results)} 份产出做 judge 重采样（每份 {args.trials} 次）\n")
    else:
        if not args.result:
            print("❌ 需指定 --result 或 --all")
            sys.exit(1)
        results = [json.loads(Path(args.result).read_text(encoding="utf-8"))]
        json_path = Path(args.result)

    report_lines = ["# Judge 重采样结果\n"]
    for r in results:
        if not r.get("sysml_code"):
            print(f"  ⚠️ {r.get('case_id')}/{r.get('retrieval')} 无代码，跳过")
            continue
        label = f"{r.get('case_id')}_{r.get('retrieval')}"
        print(f"\n=== {label}（原 judge={r.get('judge_total')}）===")
        res = _resample_one(r, args.trials)
        report_lines.append(
            f"**{label}**: 原评分={r.get('judge_total')} → "
            f"重采样 {res['trials']} 次均值={res['total_mean']}±{res['total_std']} "
            f"(明细 {res['totals']})"
        )

    report = "\n\n".join(report_lines) + "\n\n## 判读\n\n" + (
        "若 std >= 5，说明 judge 打分噪声足以覆盖消融的 5~10 分差异，"
        "方向反转可能是噪声；若 std 很小（<2），说明 judge 稳定，方向反转来自 pipeline 生成随机性。"
    )

    out_path = json_path.parent / "judge_resample.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"\n结果已写: {out_path}")


if __name__ == "__main__":
    main()
