"""CLI for offline V2 path retrieval and optional coverage-gated answering."""

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


def _int_at_least(minimum: int):
    def parse(raw: str) -> int:
        try:
            value = int(raw)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("must be an integer") from exc
        if value < minimum:
            raise argparse.ArgumentTypeError(f"must be at least {minimum}")
        return value

    return parse


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -B -m week10.src.graphrag_v2_cli")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--question", required=True)
    parser.add_argument("--run-id", action="append", required=True, dest="run_ids")
    parser.add_argument("--model", choices=CLI_MODELS, default=DEFAULT_CLI_MODEL)
    parser.add_argument("--answer", action="store_true")
    parser.add_argument("--max-cards", type=_int_at_least(0), default=24)
    parser.add_argument("--max-chars", type=_int_at_least(2), default=12_000)
    return parser


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    from week10.src.graph_retrieval_v2 import EvidenceIndexV2

    if not isinstance(args.question, str) or not args.question.strip():
        raise ValueError("question must be a non-empty string")
    runs = load_runs(args.manifest)
    retrieval = EvidenceIndexV2(runs).retrieve(
        args.question,
        args.run_ids,
        max_cards=args.max_cards,
        max_chars=args.max_chars,
    )
    if not args.answer:
        return retrieval

    from week10.src.graph_answer_v2 import answer_from_retrieval

    if retrieval.get("status") != "ready":
        answer_result = answer_from_retrieval(retrieval, model=args.model)
    else:
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key.strip():
            raise ValueError("DEEPSEEK_API_KEY is required with --answer")
        answer_result = answer_from_retrieval(
            retrieval,
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
    status = (
        output.get("answer_result", {}).get("status")
        if args.answer
        else output.get("status")
    )
    return 0 if status in {"ready", "ok", "abstained"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
