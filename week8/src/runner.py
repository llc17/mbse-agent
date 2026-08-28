"""V7 thin runner — 在独立子进程中跑单个版本（V4 或 V6）的单个用例。

为什么需要这个 runner（V7 修订版的核心修正之一）:
  1. **import 冲突**: week5 和 week7 都有同名 `src/` 包（src.pipeline / src.llm_client ...）。
     在同一进程里 import 两次会互相覆盖。解法是每种模式单独起一个子进程跑。
  2. **入口不对称**: V4 的实验入口其实不是 `week5/src/main.py --mode experiment --case`
     （那个入口没有 --case，还会 input() 阻塞等 HITL）。V4 真正能自动跑实验的是
     `week5/experiments/run_experiment.py`，而它是参数扫描框架。V6 入口是 `week7/main.py`。
     两者机制不同，统一用 thin runner 直接调各自的 `build_pipeline().invoke()` 最干净。
  3. **.env 加载不对称**: V4 的 `week5/src/llm_client.py` 不加载 .env（V6 会加载）。
     thin runner 在 import src 之前先 load_dotenv 项目根 .env，保证 V4 也能读到 key。
  4. **指标统一**: token 统计、耗时、语法错误数都在这里统一采集并写 JSON，benchmark 读 JSON 汇总。

用法（由 benchmark.py 用 subprocess 调起）:
    python runner.py --version v4 --case rc_lowpass --out <结果json路径>

输出: 一个 JSON 文件，含最终 state 的全部指标 + 产出代码文本（供 judge 用）。
"""

import argparse
import json
import sys
import time
from pathlib import Path

# 项目根目录（week8 的上一级）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent  # D:\mbse

# Windows: force UTF-8（避免中文/emoji 输出乱码）
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _load_env():
    """加载项目根目录 .env（V4 的 llm_client 不会自己加载，必须这里先加载）。"""
    env_path = _PROJECT_ROOT / ".env"
    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path)
        except ImportError:
            # 没有 python-dotenv 时，手动解析 .env（最简 KEY=VALUE 格式）
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                import os
                os.environ.setdefault(key.strip(), val.strip())


def _build_initial_state(case: dict, version: str, run_dir: Path) -> dict:
    """构造 pipeline 初始 state。字段对齐 V4/V6 的 PipelineState（TypedDict total=False）。"""
    return {
        "raw_input": case["raw_input"],
        "req": None,
        "sysml": None,
        "mo": None,
        "summary": None,
        "node_status": {"node1": "pending", "node2": "pending", "node3": "pending", "node4": "pending"},
        "human_feedback": "",
        "reject_count_per_node": {},
        "temperature": 0.3,
        "max_retries": 5,
        "max_rejects": 3,
        "dialogue_history": [],
        "timing": {},
        "run_dir": str(run_dir),
        "mode": "experiment",  # ← 关键: experiment 模式下 HITL 节点自动 approve，不 interrupt
        "quality_checks": {},
        "repair_log": [],
        "physics_feedback": "",
        "expected_physics": case.get("expected_physics"),
        # V6 新增字段（V4 的 state 里没有也无害，TypedDict total=False）
        "circuit_breaker_triggered": False,
        "breaker_details": "",
    }


def _collect(final_state: dict, version: str, case_id: str, duration: float,
             run_dir: Path, token_stats: dict) -> dict:
    """从最终 state 提取统一指标 + 产出文本。"""
    mo = final_state.get("mo", {}) or {}
    sysml = final_state.get("sysml", {}) or {}
    req = final_state.get("req", {}) or {}
    summary = final_state.get("summary", {}) or {}
    quality_checks = final_state.get("quality_checks", {}) or {}

    # 统一语法检查（不依赖 V4/V6 各自清空 errors 的逻辑）
    from syntax_check import check as syntax_check
    sysml_code = sysml.get("sysml_code", "")
    syntax = syntax_check(sysml_code)

    # 物理偏差
    physics = quality_checks.get("physics_validate", {}) or {}
    cross = quality_checks.get("cross_validate", {}) or {}

    return {
        "version": version,
        "case_id": case_id,
        "duration_s": round(duration, 1),
        "success": bool(mo.get("success", False)),
        "mo_attempts": mo.get("attempts", 0),
        "sysml_attempts": sysml.get("attempts", 0),
        # 语法错误（统一口径）
        "syntax_fatal": syntax["fatal"],
        "syntax_error": syntax["error"],
        "syntax_warning": syntax["warning"],
        "syntax_issues": syntax["issues"],
        # 质量检查
        "cross_validate_passed": cross.get("passed"),
        "physics_passed": physics.get("passed"),
        "physics_deviation_pct": physics.get("deviation_percent"),
        "physics_expected": physics.get("expected_value"),
        "physics_actual": physics.get("actual_value"),
        # 修复/根因
        "repair_count": len(final_state.get("repair_log", [])),
        "root_cause": mo.get("root_cause"),           # V6 才有，V4 恒为 None
        "root_cause_detail": mo.get("root_cause_detail", ""),
        # token 统计
        "token_stats": token_stats,
        # 产出代码 + 需求（供 judge 盲评打分）
        "component_type": req.get("component_type", ""),
        "parameters": req.get("parameters", {}),
        "sysml_code": sysml_code,
        "modelica_code": mo.get("modelica_code", ""),
        "summary_text": summary.get("summary_text", ""),
        # 产出文件定位
        "run_dir": str(run_dir),
        "sysml_file": sysml.get("file_path", ""),
        "csv_path": mo.get("csv_path", ""),
    }


def main():
    parser = argparse.ArgumentParser(description="V7 thin runner — 跑单版本单用例")
    parser.add_argument("--version", choices=["v4", "v6"], required=True)
    parser.add_argument("--case", required=True, help="用例 id（见 cases.py）")
    parser.add_argument("--raw-input", type=str, default=None,
                        help="可选: 自定义需求文本，覆盖 cases.py 里的 raw_input（用于构造失败用例）")
    parser.add_argument("--no-retrieval", action="store_true",
                        help="消融用: 关闭 V6 检索层（monkey-patch get_references 返回空，不改 week7）")
    parser.add_argument("--out", required=True, help="结果 JSON 输出路径")
    args = parser.parse_args()

    _load_env()

    # ── 选版本，把对应 week 目录加入 sys.path（先于 import src）──
    week_dir = _PROJECT_ROOT / ("week5" if args.version == "v4" else "week7")
    if not week_dir.exists():
        print(f"❌ 版本目录不存在: {week_dir}", file=sys.stderr)
        sys.exit(1)
    sys.path.insert(0, str(week_dir))

    # 把 week8/src 加入 path（以便 import cases / syntax_check，它们在 src/ 下）
    _exp_dir = str(Path(__file__).resolve().parent)
    if _exp_dir not in sys.path:
        sys.path.insert(0, _exp_dir)

    from cases import get_case
    case = get_case(args.case)
    if case is None and args.raw_input is None:
        print(f"❌ 未找到用例: {args.case}（且未提供 --raw-input）", file=sys.stderr)
        sys.exit(1)

    # 支持自定义需求（构造失败用例时，case 可能不在 cases.py 里）
    if case is None:
        case = {"raw_input": args.raw_input, "expected_physics": None}
    elif args.raw_input:
        # 覆盖 raw_input（保留 expected_physics）
        case = {**case, "raw_input": args.raw_input}

    # ── 消融: 关闭检索层（monkey-patch src.retrieval，不改 week7 代码）──
    # 关键时机: 必须在 node2 的 `from src.retrieval import ...` 之前 patch 模块属性，
    # 这样 node2 的 from-import 才会拿到 patch 后的 stub。
    if args.no_retrieval and args.version == "v6":
        import src.retrieval as _retrieval
        _retrieval.get_references = lambda *a, **k: ""
        _retrieval.get_references_llm_select = lambda *a, **k: ("", ["syntax"])
        print("  [ablation] 检索层已关闭（get_references → 空）", flush=True)

    # ── 延迟 import（.env 已加载、sys.path 已设好）──
    from src.pipeline import build_pipeline
    from src.llm_client import get_token_stats, reset_token_stats

    # 每个用例一个独立 run_dir，产出文件隔离（消融用 _noretr 后缀区分）
    _suffix = "_noretr" if (args.no_retrieval and args.version == "v6") else ""
    run_dir = Path(args.out).resolve().parent / f"run_{args.version}_{args.case}{_suffix}"
    run_dir.mkdir(parents=True, exist_ok=True)

    initial_state = _build_initial_state(case, args.version, run_dir)
    config = {"configurable": {"thread_id": f"{args.version}_{args.case}{_suffix}"}, "recursion_limit": 100}

    reset_token_stats()  # 清零，保证拿到的是本次用例的 token（V6 main.py 也这么做）

    t0 = time.time()
    final_state = None
    error = None
    try:
        graph = build_pipeline()
        final_state = graph.invoke(initial_state, config)
    except Exception as e:
        error = f"{type(e).__name__}: {str(e)[:500]}"
    duration = time.time() - t0

    token_stats = get_token_stats()

    if error:
        # 跑崩了：仍然落盘一个"失败"结果，让 benchmark 能汇总，不中断全量
        result = {
            "version": args.version,
            "case_id": args.case,
            "duration_s": round(duration, 1),
            "success": False,
            "error": error,
            "token_stats": token_stats,
            "run_dir": str(run_dir),
        }
    else:
        result = _collect(final_state, args.version, args.case, duration, run_dir, token_stats)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    # 简短状态行（benchmark 可读 stdout）
    status = "OK" if result.get("success") else ("ERR" if error else "FAIL")
    print(f"[{args.version}:{args.case}] {status} {result['duration_s']}s "
          f"tokens={token_stats.get('total_tokens', 0)}", flush=True)


if __name__ == "__main__":
    main()
