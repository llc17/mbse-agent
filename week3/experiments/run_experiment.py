"""
V2 实验框架 — 批量参数扫描 + 成功率曲线。

用法:
    python experiments/run_experiment.py                          # 全量: 4×3×30 = 360 次
    python experiments/run_experiment.py --small                  # 小规模验证: 4×3×5 = 60 次
    python experiments/run_experiment.py --resume                # 从中断恢复
    python experiments/run_experiment.py --case rc_lowpass       # 只跑指定用例

输出:
    experiments/results/experiment_<时间戳>/
    ├── results.json          # 全量原始数据
    ├── summary.json          # 汇总统计
    └── success_rate.png      # 成功率曲线图
"""

import sys
from pathlib import Path

# 把项目根目录加入搜索路径
_project_dir = Path(__file__).resolve().parent.parent   # D:\mbse\week3
if str(_project_dir) not in sys.path:
    sys.path.insert(0, str(_project_dir))

import argparse
import json
import logging
import time
import uuid
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.pipeline import build_pipeline, PipelineState
from src.utils import check_prerequisites

logger = logging.getLogger("experiment")

# 实验参数矩阵
RETRIES_LEVELS = [0, 1, 3, 5]
TEMPERATURES = [0.1, 0.3, 0.7]
TRIALS_FULL = 30
TRIALS_SMALL = 5


def load_test_cases() -> list[dict]:
    path = Path(__file__).parent / "test_cases.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)["test_cases"]


def run_single_trial(
    graph,
    test_case: dict,
    temperature: float,
    max_retries: int,
    trial_id: int,
) -> dict:
    """跑单次试验。返回结果字典。"""
    thread_id = f"exp-{test_case['id']}-r{max_retries}-t{int(temperature*100)}-{trial_id}"
    config = {"configurable": {"thread_id": thread_id}}

    state: PipelineState = {
        "raw_input": test_case["raw_input"],
        "req": None, "sysml": None, "mo": None, "summary": None,
        "node_status": {},
        "human_feedback": "",
        "reject_count_per_node": {},
        "temperature": temperature,
        "max_retries": max_retries,
        "max_rejects": 3,
        "dialogue_history": [],
        "timing": {},
        "run_dir": str(Path("outputs") / f"exp_{thread_id}"),
        "mode": "experiment",
    }

    t0 = time.time()
    try:
        final_state = graph.invoke(state, config)
        duration = time.time() - t0
        mo = final_state.get("mo", {})
        return {
            "trial_id": trial_id,
            "test_case": test_case["id"],
            "temperature": temperature,
            "max_retries": max_retries,
            "success": mo.get("success", False),
            "attempts": mo.get("attempts", 0),
            "errors": mo.get("errors", []),
            "duration": round(duration, 1),
            "error": None,
        }
    except Exception as e:
        duration = time.time() - t0
        logger.error("Trial %s 异常: %s", trial_id, str(e)[:200])
        return {
            "trial_id": trial_id,
            "test_case": test_case["id"],
            "temperature": temperature,
            "max_retries": max_retries,
            "success": False,
            "attempts": 0,
            "errors": [str(e)[:300]],
            "duration": round(duration, 1),
            "error": str(e)[:300],
        }


def compute_summary(results: list[dict]) -> dict:
    """汇总统计数据。"""
    groups = {}
    for r in results:
        key = (r["test_case"], r["max_retries"], r["temperature"])
        if key not in groups:
            groups[key] = {"total": 0, "success": 0, "attempts": [], "durations": []}
        g = groups[key]
        g["total"] += 1
        if r["success"]:
            g["success"] += 1
        g["attempts"].append(r["attempts"])
        g["durations"].append(r["duration"])

    summary = []
    for (case, retries, temp), g in sorted(groups.items()):
        summary.append({
            "test_case": case,
            "max_retries": retries,
            "temperature": temp,
            "total": g["total"],
            "success": g["success"],
            "success_rate": round(g["success"] / g["total"], 3) if g["total"] else 0,
            "avg_attempts": round(sum(g["attempts"]) / len(g["attempts"]), 1),
            "avg_duration": round(sum(g["durations"]) / len(g["durations"]), 1),
        })
    return summary


def plot_results(summary: list[dict], output_dir: Path):
    """画成功率曲线。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # 按 test_case 分组
    cases = sorted(set(s["test_case"] for s in summary))

    fig, axes = plt.subplots(1, len(cases), figsize=(6 * len(cases), 5), squeeze=False)
    axes = axes[0]

    for ax, case in zip(axes, cases):
        case_data = [s for s in summary if s["test_case"] == case]
        for temp in TEMPERATURES:
            points = [s for s in case_data if s["temperature"] == temp]
            points.sort(key=lambda s: s["max_retries"])
            x = [p["max_retries"] for p in points]
            y = [p["success_rate"] for p in points]
            ax.plot(x, y, marker="o", label=f"T={temp}")

        ax.set_xlabel("Max Self-Repair Retries")
        ax.set_ylabel("Success Rate")
        ax.set_title(f"Test Case: {case}")
        ax.set_ylim(0, 1.05)
        ax.grid(True, alpha=0.3)
        ax.legend()

    plt.tight_layout()
    plot_path = output_dir / "success_rate.png"
    plt.savefig(plot_path, dpi=150)
    plt.close()
    logger.info("成功率曲线已保存: %s", plot_path)


def main():
    parser = argparse.ArgumentParser(description="V2 实验框架")
    parser.add_argument("--small", action="store_true", help="小规模验证 (5 trials)")
    parser.add_argument("--resume", action="store_true", help="从中断结果恢复")
    parser.add_argument("--case", type=str, default=None, help="只跑指定 test_case")
    parser.add_argument("--output", type=str, default=None, help="输出目录")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    # 环境检查
    missing = check_prerequisites()
    if missing:
        print("\n❌ 环境检查失败:")
        for m in missing:
            print(f"  - {m}")
        sys.exit(1)

    # 加载用例
    test_cases = load_test_cases()
    if args.case:
        test_cases = [tc for tc in test_cases if tc["id"] == args.case]
        if not test_cases:
            print(f"未找到用例: {args.case}")
            sys.exit(1)

    trials_per = TRIALS_SMALL if args.small else TRIALS_FULL
    total = len(test_cases) * len(RETRIES_LEVELS) * len(TEMPERATURES) * trials_per
    print(f"实验规模: {len(test_cases)} 用例 × {len(RETRIES_LEVELS)} retries × {len(TEMPERATURES)} temps × {trials_per} trials = {total} 次")
    print()

    # 输出目录
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    output_dir = Path(args.output) if args.output else Path("experiments/results") / f"experiment_{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    # 恢复
    results = []
    results_path = output_dir / "results.json"
    completed_keys = set()
    if args.resume and results_path.exists():
        results = json.loads(results_path.read_text())
        completed_keys = {(r["test_case"], r["max_retries"], r["temperature"], r["trial_id"]) for r in results}
        print(f"从中断恢复: 已完成 {len(results)} 次，剩余 {total - len(results)} 次\n")

    # 构建图（单例复用）
    graph = build_pipeline()

    # 批量运行
    count = len(results)
    for tc in test_cases:
        for retries in RETRIES_LEVELS:
            for temp in TEMPERATURES:
                for trial in range(1, trials_per + 1):
                    key = (tc["id"], retries, temp, trial)
                    if key in completed_keys:
                        continue
                    count += 1
                    print(f"[{count}/{total}] case={tc['id']} retries={retries} temp={temp} trial={trial} ...", end=" ", flush=True)
                    result = run_single_trial(graph, tc, temp, retries, trial)
                    results.append(result)
                    status = "✅" if result["success"] else "❌"
                    print(f"{status} ({result['duration']}s)")

                    # 每 10 次存一次
                    if count % 10 == 0:
                        results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2))

    # 最终保存
    results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2))

    # 汇总
    summary = compute_summary(results)
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))

    print(f"\n{'='*60}")
    print("实验完成!")
    print(f"  总次数: {len(results)}")
    print(f"  成功率: {sum(1 for r in results if r['success'])}/{len(results)} = {sum(1 for r in results if r['success'])/len(results)*100:.1f}%")
    print(f"  结果: {results_path}")
    print(f"  汇总: {summary_path}")

    # 画图
    plot_results(summary, output_dir)


if __name__ == "__main__":
    main()
