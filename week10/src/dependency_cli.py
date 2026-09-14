"""Command-line access to explicitly recorded arithmetic dependencies."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from week10.src.dependency_trace import DependencyTraceGraph
from week10.src.trace_cli import DEFAULT_MANIFEST, load_runs


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -B -m week10.src.dependency_cli")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("graph")
    impact = commands.add_parser("impact")
    impact.add_argument("--run-id", required=True)
    impact.add_argument("--target-id", required=True)
    return parser


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    graph = DependencyTraceGraph(load_runs(args.manifest))
    if args.command == "graph":
        return graph.to_dict()
    if args.command == "impact":
        return graph.impact(args.run_id, args.target_id)
    raise ValueError("unknown command")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        output = dispatch(_parser().parse_args(argv))
    except KeyboardInterrupt:
        raise
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception:
        print(json.dumps({"error": "unexpected error"}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(output, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
