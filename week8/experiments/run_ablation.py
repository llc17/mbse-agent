"""V7 消融实验 — 检索层 on/off 对照（支持多次采样）。

背景（从 3 用例 A/B 结果发现的问题）:
  `dual_room_thermal` 用例上，V6（含检索层）反而比 V4 失败，裁判分 64 vs 86。
  怀疑检索层贴的官方示例带偏了 LLM。但单次采样有噪声（LLM 输出 + judge 都有方差），
  单次"检索开 70 vs 检索关 75"不足以定论。

  这个脚本做**单变量消融 + 多次采样取均值**：V6「检索开」vs V6「检索关」，
  每配置跑 N 次（--trials），对裁判分/token/耗时取均值±标准差，成功率取比例。

关键设计:
  - "检索关"通过 runner 的 --no-retrieval 实现（monkey-patch，不改 week7）
  - 只跑 V6（V4 本来就没检索层，是天然"检索关"参照）
  - 每次采样独立子目录，产出文件天然隔离
  - judge 每份产出评一次（Kimi 盲评），对 N 次 judge 分取均值

用法:
    python experiments/run_ablation.py --case dual_room_thermal --trials 3
    python experiments/run_ablation.py --trials 3          # 全量 3 用例 × 2 配置 × 3 采样
"""

import argparse
import json
import statistics
import sys
from datetime import datetime
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

from cases import UNIFIED_CASES, list_case_ids  # noqa: E402
from benchmark import run_version  # noqa: E402


def _judge(result: dict) -> dict:
    """同进程调 judge（load env + import judge.judge_one）。"""
    import sys as _sys
    if str(_SRC_DIR) not in _sys.path:
        _sys.path.insert(0, str(_SRC_DIR))
    from judge import _load_env, judge_one
    _load_env()
    return judge_one(result, references="")


def _stat(vals: list[float]) -> str:
    """均值±标准差，用于表格。"""
    if not vals:
        return "-"
    m = statistics.mean(vals)
    if len(vals) >= 2:
        s = statistics.stdev(vals)
        return f"{m:.1f}±{s:.1f}"
    return f"{m:.1f}"


def _success_rate(vals: list[bool]) -> str:
    if not vals:
        return "-"
    return f"{sum(vals)}/{len(vals)}"


def main():
    parser = argparse.ArgumentParser(description="V7 消融：检索层 on/off（多次采样）")
    parser.add_argument("--case", type=str, default=None, help="只跑指定用例")
    parser.add_argument("--trials", type=int, default=3, help="每配置采样次数（默认 3）")
    args = parser.parse_args()

    case_ids = [args.case] if args.case else list_case_ids()
    for cid in case_ids:
        if cid not in UNIFIED_CASES:
            print(f"❌ 未找到用例: {cid}")
            sys.exit(1)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    results_dir = _RESULTS_ROOT / f"ablation_{timestamp}"
    results_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("V7 消融实验：V6 检索层 on/off（多次采样）")
    print(f"  用例: {', '.join(case_ids)}")
    print(f"  对照: V6(检索开) vs V6(检索关)")
    print(f"  每配置采样: {args.trials} 次")
    print(f"  输出: {results_dir}")
    print("=" * 60)

    # 汇总结构: (case_id, retrieval) -> {judge: [], token: [], dur: [], success: [], syntax: []}
    agg: dict[tuple, dict] = {}
    all_results = []  # 原始每次采样结果

    for cid in case_ids:
        for label, no_retr in [("on", False), ("off", True)]:
            key = (cid, label)
            agg[key] = {"judge": [], "token": [], "dur": [], "success": [], "syntax": []}

            for t in range(1, args.trials + 1):
                trial_dir = results_dir / f"{cid}_{label}" / f"trial{t}"
                result = run_version("v6", cid, trial_dir, no_retrieval=no_retr)

                # judge 打分
                if result.get("sysml_code"):
                    score = _judge(result)
                    jt = score.get("total")
                    result["judge_total"] = jt
                    result["judge_scores"] = score.get("scores")
                else:
                    result["judge_total"] = None

                result["retrieval"] = label
                result["trial"] = t
                all_results.append(result)

                # 累计
                toks = (result.get("token_stats") or {}).get("total_tokens", 0)
                syntax_err = (result.get("syntax_fatal", 0) or 0) + (result.get("syntax_error", 0) or 0)
                agg[key]["token"].append(toks)
                agg[key]["dur"].append(result.get("duration_s", 0))
                agg[key]["success"].append(bool(result.get("success")))
                agg[key]["syntax"].append(syntax_err)
                if result.get("judge_total") is not None:
                    agg[key]["judge"].append(result["judge_total"])

                print(f"  [{cid}] 检索{label} 第{t}次: "
                      f"{'OK' if result.get('success') else 'FAIL'} "
                      f"tokens={toks} judge={result.get('judge_total')}", flush=True)

    # 落盘原始数据
    raw_path = results_dir / "ablation_results.json"
    raw_path.write_text(json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8")

    # 汇总表
    header = ["用例", "检索", "成功率", "裁判分(均值±std)", "token(均值)", "耗时(均值s)", "语法错误(均值)"]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for cid in case_ids:
        for label in ["on", "off"]:
            key = (cid, label)
            g = agg[key]
            lines.append(
                f"| {cid} | {label} | {_success_rate(g['success'])} | "
                f"{_stat([float(x) for x in g['judge']])} | "
                f"{_stat([float(x) for x in g['token']])} | "
                f"{_stat([float(x) for x in g['dur']])} | "
                f"{_stat([float(x) for x in g['syntax']])} |"
            )
    table = "\n".join(lines)

    # 结论判读（自动比较裁判分均值）
    conclusions = []
    for cid in case_ids:
        on = agg[(cid, "on")]["judge"]
        off = agg[(cid, "off")]["judge"]
        on_succ = sum(agg[(cid, "on")]["success"])
        off_succ = sum(agg[(cid, "off")]["success"])
        if on and off:
            diff = statistics.mean(on) - statistics.mean(off)
            direction = "正收益" if diff > 0 else ("负收益" if diff < 0 else "无差异")
            conclusions.append(
                f"- **{cid}**: 检索开 {statistics.mean(on):.1f} vs 检索关 {statistics.mean(off):.1f} "
                f"（Δ={diff:+.1f}，{direction}）；成功率 开{on_succ} vs 关{off_succ}"
            )

    report = f"""# V7 消融实验报告：检索层 on/off（多次采样）

生成时间: {timestamp}
用例: {', '.join(case_ids)}
对照: V6 检索开 vs V6 检索关（--no-retrieval，monkey-patch 关闭，不改 week7）
每配置采样: {args.trials} 次

## 汇总表（均值±标准差）

{table}

## 结论

{chr(10).join(conclusions) if conclusions else '(无数据)'}

## 方法学说明

- 裁判分/token/耗时取 N 次采样的均值±标准差（消除 LLM 输出随机性）
- 成功率是 N 次中成功的比例
- judge 每份产出评一次（Kimi 盲评，temperature 由 K3 固定为 1.0，本身仍有方差，未做 judge 多次采样）
- 若 Δ 接近标准差量级，说明差异不显著；若 Δ 远大于 std，方向才可信

## 原始数据

- `ablation_results.json`（含每次采样的完整指标 + 产出代码 + judge 评分）
"""
    report_path = results_dir / "report.md"
    report_path.write_text(report, encoding="utf-8")

    print("\n" + "=" * 60)
    print("消融完成")
    print(f"  原始数据: {raw_path}")
    print(f"  报告: {report_path}")
    print("=" * 60)
    print("\n" + table + "\n")


if __name__ == "__main__":
    main()
