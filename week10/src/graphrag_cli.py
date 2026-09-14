"""CLI for offline evidence retrieval and optional one-call answering."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

from week10.src.trace_cli import DEFAULT_MANIFEST, load_runs


DEFAULT_CLI_MODEL = "deepseek-v4-pro"
CLI_MODELS = (DEFAULT_CLI_MODEL, "deepseek-v4-flash")


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -B -m week10.src.graphrag_cli")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--question", required=True)
    parser.add_argument("--run-id", action="append", required=True, dest="run_ids")
    parser.add_argument("--mode", choices=("graph", "lexical"), default="graph")
    parser.add_argument("--model", choices=CLI_MODELS, default=DEFAULT_CLI_MODEL)
    parser.add_argument("--answer", action="store_true")
    return parser


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    from week10.src.graph_retrieval import EvidenceIndex

    if not isinstance(args.question, str) or not args.question.strip():
        raise ValueError("question must be a non-empty string")
    runs = load_runs(args.manifest)
    retrieval = EvidenceIndex(runs).retrieve(
        args.question,
        args.run_ids,
        mode=args.mode,
        max_cards=24,
        max_chars=12_000,
    )
    if not args.answer:
        return retrieval

    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key.strip():
        raise ValueError("DEEPSEEK_API_KEY is required with --answer")
    from week10.src.graph_answer import answer_question

    answer_result = answer_question(
        args.question,
        retrieval["context"],
        api_key=api_key,
        model=args.model,
    )
    return {"retrieval": retrieval, "answer_result": answer_result}


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        args = _parser().parse_args(argv)
        output = dispatch(args)
    except KeyboardInterrupt:
        raise
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception:
        print(json.dumps({"error": "unexpected error"}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(
        json.dumps(
            output,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    if args.answer and output["answer_result"].get("status") != "ok":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
