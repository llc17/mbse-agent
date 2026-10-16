"""Actual stdio MCP: the same fourteen V10 tools, a guarded car Service."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import time

from week11.src.mcp_server import create_mcp, TOOL_NAMES
from week11.src.store import safe_id
from .contracts import WEEK12_ROOT, inside_week12, authorization, profile_module, execution_route
from .service_adapter import CarService


def parent_label(db_path, session_id):
    return "car-mcp:" + session_id + ":" + str(inside_week12(db_path))


@contextmanager
def exclusive_server_lock(db_path):
    selected = inside_week12(db_path)
    lock_path = selected.with_suffix(selected.suffix + ".server.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        handle.seek(0)
        if not handle.read(1):
            handle.write(b"0")
            handle.flush()
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


def create_readonly_mcp(service):
    from mcp.server.fastmcp import FastMCP
    from week11.src.mcp_server import _safe_call
    server = FastMCP('V11 historical evidence',json_response=True,log_level='WARNING')
    @server.tool(name='evidence_query',description='Read immutable historical task evidence; cannot execute or modify anything.')
    def evidence_query(question: str, version_id: str | None = None):
        return _safe_call(service.evidence_query,question,version_id)
    if getattr(service,'artifact_readonly',False):
        @server.tool(name='project_status',description='Read the existing session project and artifact snapshot status.')
        def project_status():return _safe_call(service.project_status)
        @server.tool(name='execution_status',description='Read an existing artifact task; no execution or cancellation.')
        def execution_status(task_id:str):return _safe_call(service.execution_status,task_id)
        @server.tool(name='versions_list',description='List read-only reserved execution snapshots, not published vehicle versions.')
        def versions_list():return _safe_call(service.versions_list)
        @server.tool(name='versions_compare',description='Compare frozen same-project artifact snapshots without combining capabilities.')
        def versions_compare(before_version_id:str,after_version_id:str):return _safe_call(service.versions_compare,before_version_id,after_version_id)
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="V11 car-domain stdio MCP")
    parser.add_argument("--db", type=Path, default=WEEK12_ROOT / "data/car-projects.sqlite")
    parser.add_argument("--session", default="default")
    parser.add_argument("--parent-token")
    parser.add_argument("--profile")
    parser.add_argument("--backend",choices=['reference','model-artifact'],default='reference')
    parser.add_argument("--execution-context",type=Path)
    parser.add_argument("--read-only",action="store_true",help="Query existing evidence without execution budget or database writes")
    args = parser.parse_args(argv)
    safe_id(args.session, "session_id")
    inside_week12(args.db)
    if args.backend=='model-artifact':
        if args.parent_token is not None:
            raise ValueError('artifact mode uses its trusted caller context, not an old scope token')
        if args.read_only:
            if args.execution_context is not None:raise ValueError('read-only artifact query does not use an execution context')
            from .artifact_service import ArtifactService
            service=ArtifactService(args.db,args.session,read_only=True);service.artifact_readonly=True
            create_readonly_mcp(service).run('stdio')
            return 0
        if args.execution_context is None:raise ValueError('artifact backend requires caller execution context')
        from .artifact_service import ArtifactService
        with exclusive_server_lock(args.db):
            # FastMCP runs synchronous tools in an AnyIO worker thread. Load
            # SysML's native transitive dependencies on the main thread first:
            # a cold worker import blocked at numpy's native module loader.
            from sysmlpy.antlr_parser import parse as _sysml_parse
            service=ArtifactService(args.db,args.session,context=args.execution_context,recover=True)
            try:create_mcp(service).run('stdio')
            finally:service.worker_manager.shutdown()
        return 0
    if args.read_only:
        if args.parent_token is not None:
            raise ValueError('read-only query cannot claim an execution parent')
        create_readonly_mcp(CarService(args.db,args.session,read_only=True)).run('stdio')
        return 0
    selected = profile_module(args.profile).PROFILE
    authorization(selected)
    route_adapter = execution_route(selected)
    started = time.monotonic()
    token = args.parent_token
    external_parent = token is not None
    with exclusive_server_lock(args.db):
        if token is None:
            token = route_adapter.reserve_parent(parent_label(args.db, args.session), pid=os.getpid())
            route_adapter.set_parent_context(token)
        else:
            route_adapter.claim_parent(token, parent_label(args.db, args.session))
        service = None
        status = "UNKNOWN"
        try:
            service = CarService(args.db, args.session, recover=True, profile=selected)
            create_mcp(service).run("stdio")
            status = "RETURNED"
        finally:
            if service is not None:
                service.worker_manager.shutdown()
            if not external_parent:
                route_adapter.finish_parent(token, time.monotonic() - started, status=status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
