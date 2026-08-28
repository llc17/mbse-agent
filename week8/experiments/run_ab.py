"""V7 A/B 对比实验 — 主编排脚本。

流程:
  1. 加载统一用例集（cases.py 里的 3 个用例）
  2. 对每个用例，跑 V4 和 V6 各一次（benchmark.run_version，subprocess 隔离）
  3. 对每份产出，调 judge.py 打分（Kimi 盲评）
  4. 汇总成 Markdown 对比表 + 原始 JSON

用法:
    python experiments/run_ab.py                          # 全量: 3 用例 × 2 模式
    python experiments/run_ab.py --case rc_lowpass        # 单用例调试（先跑通这个）
    python experiments/run_ab.py --no-judge               # 只跑 A/B，不调裁判（省钱）
    python experiments/run_ab.py --check-api              # 预检 API key 连通性（跑之前先确认 key 有效）
"""

import argparse
import json
import subprocess
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
_JUDGE = _SRC_DIR / "judge.py"
_RESULTS_ROOT = _EXP_DIR.parent / "results"          # week8/results

# 把 src 加入 sys.path，以便 import cases / benchmark（可复用库在 src/ 下）
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from cases import UNIFIED_CASES, list_case_ids  # noqa: E402
from benchmark import run_version  # noqa: E402


def run_judge(result: dict, output_dir: Path) -> dict | None:
    """对一份产出调 judge.py（subprocess 隔离，避免 judge import week7 干扰主进程）。"""
    version = result.get("version")
    case_id = result.get("case_id")
    # 先确认 result 里有可评的产出
    if not result.get("sysml_code"):
        print(f"    ⚠️ {version}:{case_id} 无产出，跳过 judge")
        return None

    result_path = output_dir / f"{version}_{case_id}.json"  # 就是 benchmark 落盘的那个
    out_path = output_dir / f"judge_{version}_{case_id}.json"

    cmd = [sys.executable, str(_JUDGE), "--result", str(result_path), "--out", str(out_path)]
    print(f"\n  ▶ judge {version}:{case_id} ...", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.stdout.strip():
        print(f"    {proc.stdout.strip()}")
    if proc.stderr.strip():
        # judge 的 stderr 可能有警告，但非致命，简单回显
        for line in proc.stderr.strip().splitlines()[-3:]:
            print(f"    [judge-stderr] {line}")

    if out_path.exists():
        try:
            return json.loads(out_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"    ⚠️ judge 结果解析失败: {e}")
            return None
    return None


def _fmt(val, default="-"):
    """表格单元格格式化：None/空 → '-'，浮点保留小数。"""
    if val is None or val == "":
        return default
    if isinstance(val, float):
        return f"{val:.1f}"
    return str(val)


def _render_table(rows: list[dict]) -> str:
    """渲染对比表（Markdown）。"""
    header = ["用例", "模式", "成功率", "语法错误", "物理偏差", "裁判分", "token", "耗时(s)"]
    lines = ["| " + " | ".join(header) + " |",
             "|" + "---|" * len(header)]

    for r in rows:
        # 语法错误 = fatal + error（warning 不算硬伤）
        syntax_err = (r.get("syntax_fatal", 0) or 0) + (r.get("syntax_error", 0) or 0)
        dev = r.get("physics_deviation_pct")
        dev_str = "-" if dev is None else f"{dev:.1f}%"
        judge_total = r.get("judge_total")
        judge_str = "-" if judge_total is None else str(judge_total)
        tokens = (r.get("token_stats") or {}).get("total_tokens", "-")
        if isinstance(tokens, int):
            tokens = f"{tokens:,}"

        lines.append(
            f"| {r['case_id']} | {r['version']} | "
            f"{'✅' if r.get('success') else '❌'} | "
            f"{syntax_err} | {dev_str} | {judge_str} | {tokens} | "
            f"{r.get('duration_s', '-')} |"
        )
    return "\n".join(lines)


def check_api() -> bool:
    """预检 API key 连通性。返回是否全部就绪。失效则打印原因并返回 False。"""
    import os
    from dotenv import load_dotenv
    load_dotenv(_EXP_DIR.parent.parent / ".env")

    ok = True

    # DeepSeek（生成用，硬依赖）
    dk = os.environ.get("DEEPSEEK_API_KEY", "")
    if not dk:
        print("❌ DEEPSEEK_API_KEY 缺失")
        ok = False
    else:
        try:
            import requests
            api_base = os.environ.get("DEEPSEEK_API_BASE", "https://api.deepseek.com").rstrip("/")
            url = f"{api_base}{'/chat/completions' if api_base.endswith('/v1') else '/v1/chat/completions'}"
            model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
            r = requests.post(url, json={"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
                              headers={"Authorization": f"Bearer {dk}"}, timeout=30)
            if r.status_code == 401:
                print(f"❌ DeepSeek key 失效（401）: {r.text[:120]}")
                ok = False
            else:
                print(f"✅ DeepSeek 可连接（HTTP {r.status_code}）")
        except Exception as e:
            print(f"⚠️ DeepSeek 连通性测试异常: {str(e)[:120]}")
            # 网络异常不算硬失败，但提示

    # Kimi（裁判用）
    kk = os.environ.get("KIMI_API_KEY", "")
    if not kk:
        print("⚠️ KIMI_API_KEY 缺失（judge 将无法运行，除非 --no-judge）")
    else:
        try:
            import requests
            api_base = os.environ.get("KIMI_API_BASE", "https://api.moonshot.cn/v1").rstrip("/")
            url = f"{api_base}{'/chat/completions' if api_base.endswith('/v1') else '/v1/chat/completions'}"
            model = os.environ.get("KIMI_MODEL", "kimi-k3")
            r = requests.post(url, json={"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
                              headers={"Authorization": f"Bearer {kk}"}, timeout=30)
            if r.status_code == 401:
                print(f"❌ Kimi key 失效（401）: {r.text[:120]}")
                ok = False
            else:
                print(f"✅ Kimi 可连接（HTTP {r.status_code}）")
        except Exception as e:
            print(f"⚠️ Kimi 连通性测试异常: {str(e)[:120]}")

    return ok


def main():
    parser = argparse.ArgumentParser(description="V7 A/B 对比实验")
    parser.add_argument("--case", type=str, default=None, help="只跑指定用例")
    parser.add_argument("--no-judge", action="store_true", help="跳过 LLM-as-Judge（省钱/先验证流程）")
    parser.add_argument("--check-api", action="store_true", help="预检 API key 连通性后退出")
    args = parser.parse_args()

    # ── API 预检 ──
    if args.check_api:
        ok = check_api()
        sys.exit(0 if ok else 1)

    case_ids = [args.case] if args.case else list_case_ids()
    # 校验用例存在
    for cid in case_ids:
        if cid not in UNIFIED_CASES:
            print(f"❌ 未找到用例: {cid}（可用: {', '.join(list_case_ids())}）")
            sys.exit(1)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    results_dir = _RESULTS_ROOT / f"ab_{timestamp}"
    results_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("V7 A/B 对比实验")
    print(f"  用例: {', '.join(case_ids)}")
    print(f"  模式: V4 基线 vs V6 Agent")
    print(f"  Judge: {'Kimi 盲评' if not args.no_judge else '跳过'}")
    print(f"  输出: {results_dir}")
    print("=" * 60)

    all_results = []   # 每个 (case, version) 的 runner 结果 + judge 结果合并
    rows = []          # 对比表行

    for cid in case_ids:
        for version in ["v4", "v6"]:
            result = run_version(version, cid, results_dir)

            # 合并 judge 结果
            judge = None
            if not args.no_judge:
                judge = run_judge(result, results_dir)
            if judge:
                result["judge_scores"] = judge.get("scores")
                result["judge_total"] = judge.get("total")
                result["judge_provider"] = judge.get("provider")
                result["judge_model"] = judge.get("model")
                result["judge_comment"] = judge.get("comment")
            else:
                result["judge_total"] = None

            all_results.append(result)
            rows.append(result)

    # ── 落盘原始 JSON ──
    raw_path = results_dir / "results.json"
    raw_path.write_text(json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 对比表 ──
    table = _render_table(rows)

    report = f"""# V7 A/B 对比报告

生成时间: {timestamp}
用例: {', '.join(case_ids)}
模式 A = V4 基线（单次 LLM 调用，无审查/检索/根因分析）
模式 B = V6 Agent（三阶段审查 + SysML 检索 + LLM 根因分析）
裁判 = Kimi（盲评，与被评的 DeepSeek 不同）

## 对比表

{table}

## 指标说明

- **成功率**: 仿真是否成功（mo.success）
- **语法错误**: 统一语法检查的 fatal + error 数（口径一致，warning 不计入）
- **物理偏差**: 仿真值 vs 理论值的偏差百分比（- 表示无数据或跳过）
- **裁判分**: Kimi 盲评 0-100（syntax/consistency/topology/traceability 各 25）
- **token**: 该次运行的总 token 消耗（prompt + completion）
- **耗时(s)**: 端到端运行时间

## 原始数据

- `results.json`: 全部原始指标 + 产出代码文本 + judge 评分

"""

    # 追加 judge 明细（若跑了 judge）
    if not args.no_judge:
        report += "## Judge 评分明细\n\n"
        for r in rows:
            scores = r.get("judge_scores") or {}
            if scores:
                report += (f"**{r['case_id']} / {r['version']}** (总分 {r.get('judge_total')}): "
                           f"syntax={scores.get('syntax')}, consistency={scores.get('consistency')}, "
                           f"topology={scores.get('topology')}, traceability={scores.get('traceability')}\n")
                if r.get("judge_comment"):
                    report += f"  > {r['judge_comment']}\n"
            else:
                report += f"**{r['case_id']} / {r['version']}**: 无评分\n"

    report_path = results_dir / "report.md"
    report_path.write_text(report, encoding="utf-8")

    print("\n" + "=" * 60)
    print("A/B 对比完成")
    print(f"  原始数据: {raw_path}")
    print(f"  报告: {report_path}")
    print("=" * 60)
    print("\n" + table + "\n")


if __name__ == "__main__":
    main()
