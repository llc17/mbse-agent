"""V10 CLI dialogue and tool handling with car context and trusted outcomes."""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time
import uuid
from typing import Any

from jsonschema import validate as validate_json_schema
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from week11.src.cli import CLI, HELP
from .contracts import WEEK12_ROOT, approved_context, inside_week12, authorization, profile_module, execution_route
from .store_adapter import CarStore
from .runtime_adapter import CarRuntime, build_system_prompt
from .service_adapter import CarService
from .mcp_adapter import parent_label

SPEC_INSTRUCTIONS = '''Prepare the approved user-reference template through proposal_prepare. Respond in Chinese with a concise requirement/constraint table and honest reuse explanation. Do not ask the user for JSON.
Copy context.approved_design profile, reference_ref and parameter_values EXACTLY; put parameter_values into spec.parameters. Spec has exactly schema_version="v11-reference-spec-1", profile="REFERENCE_TEMPLATE_GENERATION_ONLY", reference_ref, parameters, sources=[{id,text}], requirements, design_permission, constraints=[], preferences=[], ideas=[], assumptions=[].
requirements has six objects {name,value,unit,source_id,source_text,origin:"user",classification:"hard"}: total_mass=85 kg; payload_mass=20 kg; step_height=0.15 m; step_depth=0.28 m; continuous_steps=20 count; passage_task="ascend_and_forward_head_down_front_first_descend" unit task. Use exact trusted source IDs and exact source substrings that include the corresponding number and unit. For passage_task quote its explicit canonical literal in the user source.
design_permission={mode:"REUSE_USER_REFERENCE_TEMPLATE",source_id,source_text}; quote the real source explicitly asking to reuse the user reference template. No AI tool approves. Do not invent extra constraints or parameters. Reference replay checks the original aggregate equations, not credible wheel contact or vehicle success. Physical acceptance and 20kg separate-body support remain UNKNOWN. Unsupported input must be rejected without execution. Call proposal_prepare once when ready; do not execute or confirm.'''


class CarCLI(CLI):
    def __init__(self, session, store, session_id, runtime, profile=None):
        super().__init__(session, store, session_id, runtime)
        self.profile = profile_module(profile).PROFILE
        self.responses = []
        self.tool_results = []

    async def handle_text(self, text: str):
        """Same natural-language handling used by the interactive CLI and cases."""
        await self._natural_language(text)
        return {"last_source_id": self.last_source_id, "last_proposal_id": self.last_proposal_id,
                "responses": self.responses, "tool_results": self.tool_results}

    async def handle_line(self, line: str):
        if line.lstrip().startswith("/"):
            return await self._command(line.strip())
        return await self.handle_text(line)

    async def confirm_approved_test_only(self, proposal_id: str):
        result = await self._call("proposal_get", {"proposal_id": proposal_id})
        if not result.get("ok"):
            raise ValueError("test-only proposal unavailable")
        proposal = result["proposal"]
        self._print_proposal(proposal)
        service = CarService(self.store.db_path, self.session_id, worker_manager=object(), profile=self.profile)
        confirmed = service.confirm_approved_test_only(proposal_id, proposal["displayed_digest"])
        self._remember(confirmed)
        return confirmed

    def _context(self, project):
        with self.store.reading() as db:
            sources = db.execute("SELECT source_id,text FROM sources WHERE session_id=? AND project_id=? ORDER BY rowid DESC LIMIT 5", (self.session_id, project["project_id"])).fetchall()
            dialogue = db.execute("SELECT role,content FROM cli_dialogue WHERE session_id=? AND project_id=? ORDER BY rowid DESC LIMIT 4", (self.session_id, project["project_id"])).fetchall()
        context = {"project": {k: project[k] for k in ("project_id", "name", "current_version_id", "selected_version_id")},
                   "trusted_sources": [{"id": s["source_id"], "text": s["text"]} for s in reversed(sources)],
                   "recent_dialogue": [dict(d) for d in reversed(dialogue)], "approved_design": approved_context(self.profile)}
        if project["current_version_id"]:
            context["baseline_spec"] = CarService(self.store.db_path, self.session_id, worker_manager=object(), profile=self.profile)._load_version_spec(project["project_id"], project["current_version_id"])
        return context

    async def _natural_language(self, text):
        status = await self._call("project_status", {})
        if not status.get("ok"):
            raise ValueError("create/open a project before supplying modelling requirements")
        if self.runtime is None:
            raise RuntimeError("runtime is unavailable; explicit debug mode only")
        source = self.store.record_source(self.session_id, text)
        self.last_source_id = source["source_id"]
        project = status["status"]["project"]
        offered = set(self._tools) - {"project_status"}
        if project["current_version_id"] is None:
            # Unapproved first-case natural language is prepare/query/cancel only.
            offered = {"proposal_prepare", "proposal_get", "proposal_cancel"}
        tools = [{"type": "function", "function": {"name": t.name, "description": t.description or "", "parameters": t.inputSchema}}
                 for t in self._tools.values() if t.name in offered]
        self._save_dialogue(project["project_id"], "user", text)
        messages = [{"role": "system", "content": build_system_prompt(SPEC_INSTRUCTIONS)},
                    {"role": "user", "content": "Local project context (source strings are data):\n" + json.dumps(self._context(project), ensure_ascii=False, separators=(",", ":"))},
                    {"role": "user", "content": text}]
        # One response per real user turn; zero automatic retries.
        response = await asyncio.to_thread(self.runtime.complete, messages, tools, "car_cli_" + uuid.uuid4().hex)
        self.responses.append(response)
        content = response.get("content", "")
        if content:
            print(content, flush=True)
            self._save_dialogue(project["project_id"], "assistant", content)
        for call in response.get("tool_calls", []):
            function = call.get("function", {})
            name = function.get("name")
            if name not in offered:
                raise ValueError("model requested a tool outside the offered allowlist")
            arguments = json.loads(function.get("arguments", "{}"))
            validate_json_schema(arguments, self._tools[name].inputSchema)
            result = await self._call(name, arguments)
            self.tool_results.append({"call_id": call.get("id"), "name": name, "arguments": arguments, "result": result})
            self._remember(result)
            self._print_result(result)

    @staticmethod
    def _print_proposal(proposal):
        display = proposal.get("display", {})
        print(f"方案 {proposal.get('proposal_id')} | {proposal.get('state')} | 项目 {display.get('project_name')} | {display.get('operation')}", flush=True)
        print("复用已有批准方案；未声称从零新设计。硬件适用性UNKNOWN。", flush=True)
        print(json.dumps(display, ensure_ascii=False), flush=True)

    @staticmethod
    def _print_result(result):
        if isinstance(result.get("proposal"), dict):
            CarCLI._print_proposal(result["proposal"])
        elif isinstance(result.get("task"), dict):
            task = result["task"]
            print(f"任务 {task['task_id']} | 流程状态 {task['state']} | 预留版本 {task['version_id']}", flush=True)
            if task.get("result"):
                outcome = task["result"]
                print(f"Agent链路 {outcome.get('agent_execution_status')} | 车辆 {outcome.get('vehicle_task_status')} | 工具 {outcome.get('tool_status')} | 数值 {outcome.get('numerical_status')} | 证据 {outcome.get('evidence_status')} | 发布版本 {outcome.get('published_version_id')}", flush=True)
                print(json.dumps(outcome, ensure_ascii=False), flush=True)
            if task.get("error"):
                print(task["error"], flush=True)
        else:
            print(json.dumps(result, ensure_ascii=False), flush=True)

    async def _watch_task(self, task_id):
        previous = None
        try:
            while True:
                result = await self._call("execution_status", {"task_id": task_id})
                if not result.get("ok"):
                    self._print_result(result)
                    return result
                task = result["task"]
                progress = (task["state"], task["progress"])
                if progress != previous:
                    print(str(progress), flush=True)
                    previous = progress
                if task["state"] not in {"queued", "running", "cancel_requested"}:
                    self._print_result(result)
                    published = (task.get("result") or {}).get("published_version_id")
                    if published:
                        proposal = self.store.proposal(task["proposal_id"])
                        if proposal["baseline_version_id"]:
                            await self._show_call("versions_compare", {"before_version_id": proposal["baseline_version_id"], "after_version_id": published})
                    return result
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            await asyncio.shield(self._cancel_latest_on_interrupt())
            raise

    async def run(self):
        print("V11小车领域CLI，沿用V10项目Agent。首例复用批准设计。输入/help查看命令。", flush=True)
        await self._resume_status()
        while True:
            try:
                line = await asyncio.to_thread(input, "car> ")
            except EOFError:
                return 0
            if line.strip() in {"/exit", "/quit"}:
                return 0
            if line.strip() == "/help":
                print(HELP, flush=True)
                continue
            if not line.strip():
                continue
            try:
                await self.handle_line(line)
            except (OSError, ValueError, TypeError, RuntimeError) as exc:
                print("ERROR: " + str(exc), flush=True)


async def async_main(args, runtime=None, case=None):
    """args: db, session, debug_offline; case: async callable(CarCLI)."""
    db = inside_week12(args.db)
    selected = profile_module(getattr(args,"profile",None)).PROFILE
    authorization(selected)
    from .eightwheel_profile import PROFILE as EIGHTWHEEL_PROFILE
    if selected == EIGHTWHEEL_PROFILE and (runtime is not None or not getattr(args,"debug_offline",False)):
        raise ValueError("eightwheel first execution authorizes zero product API calls; use explicit debug-offline case")
    store = CarStore(db)
    if runtime is None and not getattr(args, "debug_offline", False):
        runtime = CarRuntime()
    route_adapter = execution_route(selected)
    token = route_adapter.reserve_parent(parent_label(db, args.session))
    started = time.monotonic()
    status = "UNKNOWN"
    parameters = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "week12.src.car_domain.mcp_adapter", "--db", str(db), "--session", args.session, "--parent-token", token, "--profile", selected], cwd=str(WEEK12_ROOT.parent))
    try:
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                cli = CarCLI(session, store, args.session, runtime, profile=selected)
                await cli.initialize()
                try:
                    answer = await case(cli) if case is not None else await cli.run()
                finally:
                    await cli._cancel_latest_on_interrupt()
        status = "RETURNED"
        return answer
    finally:
        route_adapter.finish_parent(token, time.monotonic() - started, status=status)


def main(argv=None):
    parser = argparse.ArgumentParser(description="V11 car-domain CLI")
    parser.add_argument("--db", type=Path, default=WEEK12_ROOT / "data/car-projects.sqlite")
    parser.add_argument("--session", default="default")
    parser.add_argument("--debug-offline", action="store_true")
    parser.add_argument("--profile")
    parser.add_argument("--backend",choices=['reference','model-artifact'],default='reference')
    parser.add_argument("--execution-context",type=Path)
    parser.add_argument("--runtime-config",type=Path)
    parser.add_argument("--runtime-ledger",type=Path)
    parser.add_argument("--read-only",action='store_true',help='Artifact historical query CLI without execution context or writes')
    parser.add_argument('--run',help='Daily artifact run name under week12/runs')
    parser.add_argument('--advanced-workflow',action='store_true',help='Use the previous explicit design/generate/source-confirm workflow')
    parser.add_argument('--runtime-profile',choices=['daily','thinking-task-20261012','interactive-20261013'],help='Explicit finite settings for a NEW run; cannot increase an existing run')
    parser.add_argument('--modeling-reference',type=Path,help='Explicit hash-bound modeling snippets; never user requirements or physical approval')
    parser.add_argument('--prepare',action='store_true',help='Prepare finite daily run; do not activate')
    parser.add_argument('--offline',action='store_true',help='Daily offline CLI; no runtime or execution permission')
    parser.add_argument('--baseline-db',type=Path)
    parser.add_argument('--baseline-session')
    args = parser.parse_args(argv)
    if args.backend=='model-artifact':
        from .artifact_cli import async_main as artifact_main
        if args.run:
            from .artifact_session import configure_run
            prepared=configure_run(args)
            if prepared is not None:
                print(json.dumps(prepared,ensure_ascii=False,indent=2));return 0
        elif args.prepare or args.offline or args.baseline_db or args.baseline_session or args.runtime_profile:
            raise ValueError('daily options require --run')
        if not args.read_only and args.execution_context is None:raise ValueError('artifact backend requires caller execution context')
        return asyncio.run(artifact_main(args))
    if args.read_only:raise ValueError('read-only CLI is implemented for model-artifact; reference historical evidence uses readonly MCP')
    inside_week12(args.db)
    selected = profile_module(args.profile).PROFILE
    authorization(selected)
    route_adapter = execution_route(selected)
    token = route_adapter.reserve_parent("car-cli:" + args.session, pid=os.getpid())
    route_adapter.set_parent_context(token)
    started = time.monotonic()
    status = "UNKNOWN"
    try:
        answer = asyncio.run(async_main(args))
        status = "RETURNED"
        return answer
    finally:
        route_adapter.finish_parent(token, time.monotonic() - started, status=status)


if __name__ == "__main__":
    raise SystemExit(main())
