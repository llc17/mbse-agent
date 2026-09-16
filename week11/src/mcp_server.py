"""Actual stdio MCP facade for the session-scoped V10 service."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP

from week11.src.service import Service
from week11.src.store import WEEK11_ROOT


TOOL_NAMES = (
    "project_create", "project_list", "project_open", "project_status",
    "proposal_prepare", "proposal_get", "proposal_cancel",
    "execution_start", "execution_status", "execution_cancel",
    "evidence_query", "versions_list", "versions_select", "versions_compare",
)


def _safe_call(function: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        return function(*args, **kwargs)
    except (OSError, TypeError, ValueError) as exc:
        return {"schema_version": "v10-service-1", "ok": False, "reason": str(exc), "error_type": type(exc).__name__}


def create_mcp(service: Service) -> FastMCP:
    server = FastMCP(
        "V10 MBSE",
        instructions=(
            "Session-scoped project, proposal, execution, version and evidence tools. "
            "No tool can record user input or approve a proposal; confirmation is local CLI-only."
        ),
        json_response=True,
        log_level="WARNING",
    )

    @server.tool(name="project_create", description="Create and select a project.")
    def project_create(name: str) -> dict[str, Any]:
        return _safe_call(service.project_create, name)

    @server.tool(name="project_list", description="List projects and the selected project.")
    def project_list() -> dict[str, Any]:
        return _safe_call(service.project_list)

    @server.tool(name="project_open", description="Explicitly select an existing project.")
    def project_open(project_id: str) -> dict[str, Any]:
        return _safe_call(service.project_open, project_id)

    @server.tool(name="project_status", description="Show selected-project state and latest task.")
    def project_status() -> dict[str, Any]:
        return _safe_call(service.project_status)

    @server.tool(name="proposal_prepare", description="Validate and prepare a source-bound model proposal. This does not approve it.")
    def proposal_prepare(spec: dict[str, Any]) -> dict[str, Any]:
        return _safe_call(service.proposal_prepare, spec)

    @server.tool(name="proposal_get", description="Get a proposal and its exact display digest.")
    def proposal_get(proposal_id: str) -> dict[str, Any]:
        return _safe_call(service.proposal_get, proposal_id)

    @server.tool(name="proposal_cancel", description="Cancel an unexecuted proposal.")
    def proposal_cancel(proposal_id: str) -> dict[str, Any]:
        return _safe_call(service.proposal_cancel, proposal_id)

    @server.tool(name="execution_start", description="Queue a locally approved proposal. Approval cannot be supplied as an argument.")
    def execution_start(proposal_id: str, request_id: str) -> dict[str, Any]:
        return _safe_call(service.execution_start, proposal_id, request_id)

    @server.tool(name="execution_status", description="Poll concise execution state and progress.")
    def execution_status(task_id: str) -> dict[str, Any]:
        return _safe_call(service.execution_status, task_id)

    @server.tool(name="execution_cancel", description="Request cancellation of an execution owned by this server.")
    def execution_cancel(task_id: str) -> dict[str, Any]:
        return _safe_call(service.execution_cancel, task_id)

    @server.tool(name="evidence_query", description="Query hash-verified evidence for one selected project version.")
    def evidence_query(question: str, version_id: str | None = None) -> dict[str, Any]:
        return _safe_call(service.evidence_query, question, version_id)

    @server.tool(name="versions_list", description="List immutable validated versions.")
    def versions_list() -> dict[str, Any]:
        return _safe_call(service.versions_list)

    @server.tool(name="versions_select", description="Select a validated version and invalidate stale proposals.")
    def versions_select(version_id: str) -> dict[str, Any]:
        return _safe_call(service.versions_select, version_id)

    @server.tool(name="versions_compare", description="Compare two hash-verified versions of the selected project.")
    def versions_compare(before_version_id: str, after_version_id: str) -> dict[str, Any]:
        return _safe_call(service.versions_compare, before_version_id, after_version_id)

    return server


@contextmanager
def exclusive_server_lock(db_path: Path):
    """Hold one OS lock for this database across recovery and server lifetime."""
    selected = db_path.resolve()
    try:
        selected.relative_to(WEEK11_ROOT.resolve())
    except ValueError as exc:
        raise ValueError("server database must be inside week11") from exc
    lock_path = selected.with_suffix(selected.suffix + ".server.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("another MCP server owns this database") from exc
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("another MCP server owns this database") from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="V10 MBSE stdio MCP server")
    parser.add_argument("--db", type=Path, default=Path("week11/data/projects.sqlite"))
    parser.add_argument("--session", default="default")
    args = parser.parse_args(argv)
    with exclusive_server_lock(args.db):
        service = Service(args.db, args.session, recover=True)
        try:
            create_mcp(service).run("stdio")
        finally:
            service.worker_manager.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["TOOL_NAMES", "create_mcp", "exclusive_server_lock", "main"]
