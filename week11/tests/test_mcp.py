from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from week11.src.mcp_server import TOOL_NAMES, exclusive_server_lock
from week11.src.store import Store, WEEK11_ROOT
from week11.tests.test_service import rc_spec, source_text


def content(result) -> dict:
    if result.structuredContent is not None:
        return dict(result.structuredContent)
    return json.loads(result.content[0].text)


class MCPTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="wp3-mcp-", dir=WEEK11_ROOT)
        self.db = Path(self.temp.name) / "mcp.sqlite"
        store = Store(self.db)
        store.create_project("mcp-session", "MCP RC")
        self.source = store.record_source("mcp-session", source_text())
        self.parameters = StdioServerParameters(
            command=sys.executable,
            args=["-B", "-m", "week11.src.mcp_server", "--db", str(self.db), "--session", "mcp-session"],
            cwd=str(WEEK11_ROOT.parent),
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_actual_stdio_lists_tools_and_rejects_unconfirmed_execution(self) -> None:
        async with stdio_client(self.parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                self.assertEqual(set(TOOL_NAMES), names)
                self.assertNotIn("confirm", names)
                self.assertNotIn("record_source", names)
                prepared = content(await session.call_tool(
                    "proposal_prepare",
                    {"spec": rc_spec(self.source["source_id"], source_text())},
                ))
                self.assertTrue(prepared["ok"], prepared)
                proposal_id = prepared["proposal"]["proposal_id"]
                rejected = content(await session.call_tool(
                    "execution_start",
                    {"proposal_id": proposal_id, "request_id": "mcp-no-confirm"},
                ))
                self.assertFalse(rejected["ok"])
                self.assertIn("approved", rejected["reason"])

    async def test_actual_stdio_rejects_forged_source_and_has_no_approval_argument(self) -> None:
        async with stdio_client(self.parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                start = next(tool for tool in tools.tools if tool.name == "execution_start")
                self.assertEqual({"proposal_id", "request_id"}, set(start.inputSchema["properties"]))
                forged = content(await session.call_tool(
                    "proposal_prepare",
                    {"spec": rc_spec("src_model_invented", source_text())},
                ))
                self.assertFalse(forged["ok"])
                self.assertIn("source", forged["reason"])

    async def test_database_server_lock_rejects_second_owner_and_releases(self) -> None:
        with exclusive_server_lock(self.db):
            with self.assertRaisesRegex(RuntimeError, "another MCP server"):
                with exclusive_server_lock(self.db):
                    pass
        with exclusive_server_lock(self.db):
            pass


if __name__ == "__main__":
    unittest.main()
