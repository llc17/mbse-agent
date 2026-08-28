"""V7 benchmark — 单模式运行器。

给定"版本（v4 或 v6）"+"用例"，用 subprocess 调 runner.py 跑一次，读回结果 JSON。

关键设计:
  - 全程 subprocess 隔离，V4/V6 的 src 包互不干扰（难点1）
  - runner 自己处理 .env 加载、state 构造、指标采集（难点2/3）
  - 这里只负责: 起子进程 → 传参 → 读 JSON → 返回指标 dict

用法:
    from benchmark import run_version
    result = run_version("v4", "rc_lowpass", output_dir=Path("results"))
"""

import json
import subprocess
import sys
from pathlib import Path

_RUNNER = Path(__file__).resolve().parent / "runner.py"


def run_version(version: str, case_id: str, output_dir: Path, raw_input: str | None = None,
                no_retrieval: bool = False) -> dict:
    """跑一个版本的一个用例，返回结果 dict。

    Args:
        version: "v4" 或 "v6"
        case_id: 用例 id（见 cases.py）
        output_dir: 结果 JSON 写到哪里
        raw_input: 可选，自定义需求文本（覆盖 cases.py 的 raw_input，用于构造失败用例）
        no_retrieval: 消融用，关闭 V6 检索层（仅 version=v6 时生效）

    Returns:
        runner 输出的结果 dict（含 success/token/syntax/physics/产出代码等）
        如果子进程跑崩，返回带 error 字段的 dict（不抛异常，保证全量不中断）。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = "_noretr" if no_retrieval else ""
    out_path = output_dir / f"{version}_{case_id}{suffix}.json"

    cmd = [
        sys.executable, str(_RUNNER),
        "--version", version,
        "--case", case_id,
        "--out", str(out_path),
    ]
    if raw_input:
        cmd += ["--raw-input", raw_input]
    if no_retrieval:
        cmd += ["--no-retrieval"]

    print(f"\n  ▶ 跑 {version}:{case_id} ...", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")

    # 打印 runner 的 stdout（状态行）
    if proc.stdout.strip():
        print(f"    {proc.stdout.strip()}")

    if out_path.exists():
        try:
            return json.loads(out_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            return {"version": version, "case_id": case_id, "success": False,
                    "error": f"结果 JSON 解析失败: {e}"}

    # 子进程整体失败（runner 没写 JSON）
    stderr_tail = (proc.stderr or "")[-500:]
    return {"version": version, "case_id": case_id, "success": False,
            "error": f"runner 退出码 {proc.returncode}: {stderr_tail}"}
