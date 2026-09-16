from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from contextlib import redirect_stdout

from week11.src.cli import CLI, async_main
from week11.src.service import Service
from week11.src.store import Store, WEEK11_ROOT
from week11.tests.test_service import DeferredWorker, rc_spec, source_text


def tool(name: str, properties: dict, required: list[str] | None = None):
    return SimpleNamespace(
        name=name,
        description=name,
        inputSchema={"type": "object", "properties": properties, "required": required or [], "additionalProperties": False},
    )


class FakeSession:
    def __init__(self, service: Service) -> None:
        self.service = service
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        methods = {
            "project_status": self.service.project_status,
            "proposal_get": self.service.proposal_get,
            "proposal_prepare": self.service.proposal_prepare,
            "execution_start": self.service.execution_start,
        }
        result = methods[name](**arguments)
        return SimpleNamespace(structuredContent=result, content=[])


class InspectingRuntime:
    def __init__(self, store: Store, session_id: str) -> None:
        self.store = store
        self.session_id = session_id
        self.called = False

    def complete(self, messages, tools, request_id):
        self.called = True
        project = self.store.current_project(self.session_id)
        with self.store.reading() as db:
            count = db.execute("SELECT COUNT(*) FROM sources WHERE session_id=? AND project_id=?", (self.session_id, project["project_id"])).fetchone()[0]
        if count != 1:
            raise AssertionError("runtime was called before trusted source persistence")
        return {"content": "需要更多信息", "tool_calls": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}, "response_model": "test"}


class CLITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="wp3-cli-", dir=WEEK11_ROOT)
        self.db = Path(self.temp.name) / "cli.sqlite"
        self.service = Service(self.db, "cli-session", worker_manager=DeferredWorker())
        self.service.project_create("CLI RC")

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_plain_text_is_persisted_before_runtime(self) -> None:
        runtime = InspectingRuntime(self.service.store, "cli-session")
        session = FakeSession(self.service)
        cli = CLI(session, self.service.store, "cli-session", runtime)
        cli._tools = {"project_status": tool("project_status", {})}
        output = io.StringIO()
        with redirect_stdout(output):
            await cli._natural_language("自然同义表达的 RC 请求")
        self.assertTrue(runtime.called)
        self.assertIn("需要更多信息", output.getvalue())
        self.assertEqual("project_status", session.calls[0][0])

    async def test_restart_discovers_pending_without_approving_or_executing(self) -> None:
        text = source_text()
        source = self.service.record_source(text)
        proposal = self.service.proposal_prepare(rc_spec(source["source_id"], text))["proposal"]
        session = FakeSession(self.service)
        reopened = CLI(session, Store(self.db), "cli-session", None)
        reopened._tools = {"project_status": tool("project_status", {})}
        output = io.StringIO()
        with redirect_stdout(output):
            await reopened._command("/proposals")
        self.assertEqual(proposal["proposal_id"], reopened.last_proposal_id)
        self.assertIn(proposal["proposal_id"], output.getvalue())
        self.assertEqual("awaiting_confirmation", self.service.store.proposal(proposal["proposal_id"])["state"])
        self.assertEqual([], self.service.store.tasks_for_project(proposal["project_id"]))
        self.assertEqual(["project_status"], [name for name, _ in session.calls])

        other = Service(self.db, "other-session", worker_manager=DeferredWorker())
        other.project_open(proposal["project_id"])
        self.assertEqual([], other.store.pending_proposals("other-session", proposal["project_id"]))
        self.service.proposal_cancel(proposal["proposal_id"])
        with redirect_stdout(io.StringIO()):
            await reopened._command("/status")
        self.assertIsNone(reopened.last_proposal_id)

    async def test_recovery_shows_other_project_blocker_without_mutation(self) -> None:
        text = source_text()
        source = self.service.record_source(text)
        proposal = self.service.proposal_prepare(rc_spec(source["source_id"], text))["proposal"]
        self.service.confirm_local(proposal["proposal_id"], proposal["displayed_digest"])
        task = self.service.execution_start(proposal["proposal_id"], "orphan-test")["task"]
        self.service.store.claim_task(task["task_id"], "lost-owner", 424242)
        self.service.store.finish_task(task["task_id"], "uncertain", error="test fixture: ownership lost")
        self.service.project_create("another project")
        cli = CLI(FakeSession(self.service), self.service.store, "cli-session", None)
        output = io.StringIO()
        with redirect_stdout(output):
            await cli._command("/recovery")
        self.assertIn(task["task_id"], output.getvalue())
        self.assertIn(proposal["project_id"], output.getvalue())
        self.assertIn("uncertain", output.getvalue())
        self.assertEqual("uncertain", self.service.store.task(task["task_id"])["state"])
        self.assertIsNone(cli.last_task_id)

    async def test_status_does_not_adopt_persisted_task_for_exit_cancellation(self) -> None:
        text = source_text()
        source = self.service.record_source(text)
        proposal = self.service.proposal_prepare(rc_spec(source["source_id"], text))["proposal"]
        self.service.confirm_local(proposal["proposal_id"], proposal["displayed_digest"])
        task = self.service.execution_start(proposal["proposal_id"], "persisted-task")["task"]
        session = FakeSession(self.service)
        cli = CLI(session, Store(self.db), "cli-session", None)
        cli._tools = {"project_status": tool("project_status", {})}
        with redirect_stdout(io.StringIO()):
            await cli._command("/status")
            await cli._cancel_latest_on_interrupt()
        self.assertIsNone(cli.last_task_id)
        self.assertEqual("queued", self.service.store.task(task["task_id"])["state"])
        self.assertEqual(["project_status"], [name for name, _ in session.calls])

    async def test_confirm_is_local_exact_phrase_and_not_an_mcp_call(self) -> None:
        text = source_text()
        source = self.service.record_source(text)
        proposal = self.service.proposal_prepare(rc_spec(source["source_id"], text))["proposal"]
        session = FakeSession(self.service)
        cli = CLI(session, self.service.store, "cli-session", None)
        cli._tools = {
            "proposal_get": tool("proposal_get", {"proposal_id": {"type": "string"}}, ["proposal_id"]),
            "execution_start": tool("execution_start", {"proposal_id": {"type": "string"}, "request_id": {"type": "string"}}, ["proposal_id", "request_id"]),
        }
        cli._watch_task = AsyncMock()
        with patch("builtins.input", return_value=f"CONFIRM {proposal['proposal_id']}"):
            await cli._command(f"/confirm {proposal['proposal_id']}")
        self.assertEqual("approved", self.service.store.proposal(proposal["proposal_id"])["state"])
        self.assertEqual(["proposal_get", "execution_start"], [name for name, _ in session.calls])
        cli._watch_task.assert_awaited_once()

    async def test_reopened_cli_retains_clarification_sources_and_reply(self) -> None:
        service = self.service
        contexts = []
        class ClarifyingRuntime:
            def complete(self, messages, tools, request_id):
                contexts.append(messages)
                return {"content": "请补充输入电压和激励类型", "tool_calls": []}
        first = CLI(FakeSession(service), service.store, "cli-session", ClarifyingRuntime())
        first._tools = {"project_status": tool("project_status", {})}
        with redirect_stdout(io.StringIO()):
            await first._natural_language("RC 低通，电阻 1 kOhm，截止频率 1 kHz，按公式计算电容。")
            reopened = CLI(FakeSession(service), service.store, "cli-session", ClarifyingRuntime())
            reopened._tools = first._tools
            await reopened._natural_language("输入使用 5 V 阶跃。")
        context = json.loads(contexts[1][1]["content"].split("\n", 1)[1])
        self.assertEqual(2, len(context["trusted_sources"]))
        self.assertEqual("输入使用 5 V 阶跃。", context["trusted_sources"][1]["text"])
        self.assertIn({"role": "assistant", "content": "请补充输入电压和激励类型"}, context["recent_dialogue"])

    async def test_wrong_confirmation_phrase_leaves_proposal_pending(self) -> None:
        text = source_text()
        source = self.service.record_source(text)
        proposal = self.service.proposal_prepare(rc_spec(source["source_id"], text))["proposal"]
        session = FakeSession(self.service)
        cli = CLI(session, self.service.store, "cli-session", None)
        cli._tools = {"proposal_get": tool("proposal_get", {"proposal_id": {"type": "string"}}, ["proposal_id"])}
        with patch("builtins.input", return_value="yes"):
            await cli._command(f"/confirm {proposal['proposal_id']}")
        self.assertEqual("awaiting_confirmation", self.service.store.proposal(proposal["proposal_id"])["state"])

    async def test_scripted_cli_uses_actual_stdio_mcp_for_project_commands(self) -> None:
        second_db = Path(self.temp.name) / "scripted.sqlite"
        args = argparse.Namespace(db=second_db, session="scripted", debug_offline=True)
        with patch("builtins.input", side_effect=["/new Scripted RC", "/status", "/exit"]):
            code = await async_main(args, runtime=None)
        self.assertEqual(0, code)
        current = Store(second_db).current_project("scripted")
        self.assertEqual("Scripted RC", current["name"])

    async def test_recovery_requires_exact_local_phrase_and_never_calls_mcp(self) -> None:
        session = FakeSession(self.service)
        cli = CLI(session, self.service.store, "cli-session", None)
        inspection = {"eligible": True, "digest": "test-evidence", "task_id": "task_test"}
        recovered = {"task_id": "task_test", "state": "interrupted", "version_id": "v000001"}
        with patch.object(Store, "inspect_task_recovery", return_value=inspection), patch.object(Store, "resolve_after_reboot", return_value=recovered) as resolve:
            with redirect_stdout(io.StringIO()), patch("builtins.input", return_value="继续"):
                await cli._command("/recover task_test")
            resolve.assert_not_called()
            with redirect_stdout(io.StringIO()), patch("builtins.input", return_value="RECOVER task_test"):
                await cli._command("/recover task_test")
            resolve.assert_called_once_with("task_test", "cli-session", "test-evidence")
        self.assertEqual([], session.calls)

    async def test_unproven_recovery_does_not_ask_or_mutate(self) -> None:
        cli = CLI(FakeSession(self.service), self.service.store, "cli-session", None)
        with patch.object(Store, "inspect_task_recovery", return_value={"eligible": False, "reason": "same boot"}), patch.object(Store, "resolve_after_reboot") as resolve, patch("builtins.input") as prompt, redirect_stdout(io.StringIO()):
            await cli._command("/recover task_test")
        prompt.assert_not_called()
        resolve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
