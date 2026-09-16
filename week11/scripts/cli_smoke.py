"""Bounded public online smoke through the production CLI and real MCP.

This command prepares a proposal only. It cannot approve or execute it.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stdout
import json
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from week11.src.cli import CLI
from week11.src.runtime import DeepSeekRuntime
from week11.src.store import Store, WEEK11_ROOT


class RecordedRuntime:
    def __init__(self, root: Path, limit: int = 2) -> None:
        self.inner = DeepSeekRuntime()
        self.root, self.limit, self.calls = root, limit, 0

    def complete(self, messages, tools, request_id):
        if self.calls >= self.limit:
            raise RuntimeError("smoke call limit reached")
        self.calls += 1
        record = {"request_id": request_id, "messages": messages, "tools": tools}
        target = self.root / f"call-{self.calls}.json"
        try:
            record["response"] = self.inner.complete(messages, tools, request_id)
            return record["response"]
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            target.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


class Tee:
    def __init__(self, file):
        self.file = file

    def write(self, value):
        self.file.write(value)
        return sys.__stdout__.write(value)

    def flush(self):
        self.file.flush()
        sys.__stdout__.flush()


async def run(root: Path, case_id: str) -> int:
    root = root.resolve()
    root.relative_to(WEEK11_ROOT.resolve())
    root.mkdir(parents=True, exist_ok=False)
    protocol = json.loads((WEEK11_ROOT / "configs/online_acceptance.json").read_text(encoding="utf-8"))
    case = next(x for x in protocol["cases"] if x["id"] == case_id)
    if case.get("baseline") or case.get("project", True) is None:
        raise ValueError("smoke requires a new-project case")
    db = root / "projects.sqlite"
    session_id = "smoke"
    runtime = RecordedRuntime(root)
    store = Store(db)
    parameters = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "week11.src.mcp_server", "--db", str(db), "--session", session_id],
        cwd=str(WEEK11_ROOT.parent))
    summary = {"schema_version": "v10-cli-smoke-1", "case_id": case_id,
               "real_mcp": True, "confirmation": "none", "execution": "none"}
    try:
        with (root / "transcript.txt").open("w", encoding="utf-8") as transcript:
            with redirect_stdout(Tee(transcript)):
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as session:
                        cli = CLI(session, store, session_id, runtime)
                        await cli.initialize()
                        project = await cli._call("project_create", {"name": "V10 public " + case_id})
                        summary["project"] = project
                        for text in case["turns"]:
                            print("USER: " + text, flush=True)
                            await cli._natural_language(text)
                        if cli.last_proposal_id:
                            summary["proposal"] = await cli._call("proposal_get", {"proposal_id": cli.last_proposal_id})
                        summary["status"] = await cli._call("project_status", {})
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
    summary["model_calls"] = runtime.calls
    (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"saved": str(root), "model_calls": runtime.calls, "proposal": bool(summary.get("proposal")), "error": summary.get("error")}, ensure_ascii=False), flush=True)
    return 0 if summary.get("proposal", {}).get("ok") else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", default="rc_public")
    args = parser.parse_args()
    return asyncio.run(run(args.output, args.case))


if __name__ == "__main__":
    raise SystemExit(main())
