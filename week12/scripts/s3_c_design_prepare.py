"""C development preparation CLI; deliberately no execute/confirm/online modes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from week12.src.design_preparation import prepare_design, write_preparation


def _read(path):
    # Only explicit task files. Never discover credentials or historical designs.
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description="V11 C pending design preparation; no physical execution")
    parser.add_argument("--task", required=True, type=Path)
    parser.add_argument("--draft", type=Path)
    parser.add_argument("--cycle-intent", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = prepare_design(_read(args.task), _read(args.draft) if args.draft else None,
                            _read(args.cycle_intent) if args.cycle_intent else None)
    result = write_preparation(report, args.output)
    print(json.dumps(result, ensure_ascii=False))
    # 0 means this non-executable development step completed, never vehicle PASS.
    return 0 if report["status"] in {"NEEDS_DESIGN_ANALYSIS", "NEEDS_AGENT_DESIGN", "NEEDS_TASK_FACTS"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
