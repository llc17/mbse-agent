"""Bounded command-line access to explicitly published V9 trace records."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re
import sys
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = PROJECT_ROOT / "week10" / "results"
DEFAULT_MANIFEST = RESULTS_ROOT / "trace-stage2-20260909" / "manifest.json"
MANIFEST_SCHEMA = "v9-public-trace-manifest-1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _public_run_paths(manifest_path: Path) -> list[tuple[str, Path, str]]:
    """Validate every manifest entry before reading any run record."""
    results_root = RESULTS_ROOT.resolve(strict=True)
    manifest_path = manifest_path.resolve(strict=True)
    if not _inside(manifest_path, results_root):
        raise ValueError("manifest must be under week10/results")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("invalid manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != MANIFEST_SCHEMA:
        raise ValueError("unsupported manifest schema")
    if manifest.get("public_data_only") is not True:
        raise ValueError("manifest is not marked public")
    entries = manifest.get("runs")
    if not isinstance(entries, list):
        raise ValueError("manifest runs must be a list")

    seen: set[str] = set()
    resolved: list[tuple[str, Path, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("invalid manifest run entry")
        run_id, raw_path, digest = entry.get("run_id"), entry.get("path"), entry.get("sha256")
        if not isinstance(run_id, str) or not run_id or run_id in seen:
            raise ValueError("duplicate or invalid manifest run_id")
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("invalid manifest path")
        normalized = raw_path.replace("\\", "/")
        if ".." in Path(normalized).parts:
            raise ValueError("manifest path escapes week10/results")
        if (
            not normalized.startswith("week10/results/")
            or Path(normalized).is_absolute()
            or PureWindowsPath(raw_path).drive
        ):
            raise ValueError("manifest path must be project-relative under week10/results")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ValueError("invalid manifest sha256")
        try:
            candidate = (PROJECT_ROOT / Path(normalized)).resolve(strict=True)
        except OSError as exc:
            raise ValueError("manifest run file is unavailable") from exc
        if not _inside(candidate, results_root):
            raise ValueError("manifest path escapes week10/results")
        seen.add(run_id)
        resolved.append((run_id, candidate, digest))
    return resolved


def load_runs(manifest_path: Path) -> list[Any]:
    """Load only hash-bound RunResult JSON records named by a public manifest."""
    from week9.src.schemas import RunResult

    records: list[Any] = []
    for expected_id, path, expected_sha in _public_run_paths(manifest_path):
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ValueError("manifest run file is unavailable") from exc
        if hashlib.sha256(raw).hexdigest() != expected_sha:
            raise ValueError("manifest run hash mismatch")
        try:
            result = RunResult.from_dict(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError("invalid RunResult record") from exc
        if result.run_id != expected_id:
            raise ValueError("manifest run_id does not match RunResult")
        records.append(result)
    return records


def _requirements(runs: Iterable[Any]) -> dict[str, Any]:
    response: list[dict[str, Any]] = []
    for run in runs:
        traceability = getattr(run, "traceability", None)
        links = getattr(traceability, "links", ()) if traceability is not None else ()
        response.append({
            "run_id": run.run_id,
            "requirements": [
                {
                    "requirement_id": link.requirement_id,
                    "requirement_text": link.requirement_text,
                    "target_id": link.target_id,
                    "status": getattr(link.status, "value", link.status),
                }
                for link in links
            ],
        })
    return {"runs": response}


def _graph(runs: Iterable[Any]) -> Any:
    from week10.src.trace_graph import TraceGraph

    graph = TraceGraph()
    for run in runs:
        returned_id = graph.add_run(run)
        if returned_id != run.run_id:
            raise ValueError("TraceGraph returned an unexpected run_id")
    return graph


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -m week10.src.trace_cli")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("graph")
    commands.add_parser("requirements")
    trace = commands.add_parser("trace")
    trace.add_argument("--run-id", required=True)
    trace.add_argument("--requirement-id", required=True)
    impact = commands.add_parser("impact")
    impact.add_argument("--run-id", required=True)
    impact.add_argument("--target-id", required=True)
    reverse = commands.add_parser("reverse")
    reverse.add_argument("--run-id", required=True)
    reverse.add_argument("--check-type", required=True)
    compare = commands.add_parser("compare")
    compare.add_argument("--before", required=True)
    compare.add_argument("--after", required=True)
    return parser


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    runs = load_runs(args.manifest)
    if args.command == "requirements":
        return _requirements(runs)
    graph = _graph(runs)
    if args.command == "graph":
        return graph.to_dict()
    if args.command == "trace":
        return graph.query_requirement(args.run_id, args.requirement_id)
    if args.command == "impact":
        return graph.impact(args.run_id, args.target_id)
    if args.command == "reverse":
        return graph.reverse_check(args.run_id, args.check_type)
    if args.command == "compare":
        return graph.compare_runs(args.before, args.after)
    raise ValueError("unknown command")


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
    print(json.dumps(output, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
