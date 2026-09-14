"""Command-line renderer for deterministic V9 graph evidence."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from typing import Any
from week10.src.graph_retrieval_v2 import EvidenceIndexV2
from week10.src.graph_evidence_view import build_evidence_answer, render_markdown
from week10.src.trace_cli import DEFAULT_MANIFEST, load_runs

class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None: raise ValueError(message)

def _cards(value: str) -> int:
    result = int(value)
    if result < 0: raise argparse.ArgumentTypeError("must be non-negative")
    return result

def _chars(value: str) -> int:
    result = int(value)
    if result < 2: raise argparse.ArgumentTypeError("must be at least 2")
    return result

def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -B -m week10.src.graph_evidence_cli")
    parser.add_argument("--question", required=True)
    parser.add_argument("--run-id", action="append", required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--max-cards", type=_cards, default=24)
    parser.add_argument("--max-chars", type=_chars, default=12000)
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser

def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    return build_evidence_answer(EvidenceIndexV2(load_runs(args.manifest)), args.question, args.run_id, args.max_cards, args.max_chars)

def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"): sys.stderr.reconfigure(encoding="utf-8")
    try:
        args = _parser().parse_args(argv)
        result = dispatch(args)
        output = json.dumps(result, ensure_ascii=False, allow_nan=False) if args.format == "json" else render_markdown(result)
        print(output, end="")
    except KeyboardInterrupt: raise
    except (ValueError, OSError, argparse.ArgumentTypeError):
        print(json.dumps({"error": "invalid input"}, ensure_ascii=False), file=sys.stderr); return 2
    except Exception:
        print(json.dumps({"error": "unexpected error"}, ensure_ascii=False), file=sys.stderr); return 2
    return 0

if __name__ == "__main__": raise SystemExit(main())
