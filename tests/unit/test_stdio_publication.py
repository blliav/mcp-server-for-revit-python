"""Exercise the actual stdio process and MCP protocol, without Revit."""
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[2]


async def test_stdio_inventory_and_unpaired_refusal(tmp_path):
    env = dict(os.environ, LOCALAPPDATA=str(tmp_path), REVIT_HOST="127.0.0.1")
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "main.py")],
                                  cwd=str(ROOT), env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            assert initialized.protocolVersion == "2025-11-25"
            tools = (await session.list_tools()).tools
            manifest = json.loads((ROOT / "mcp-tool-manifest.json").read_text(encoding="utf-8"))
            assert sorted((tool.name, tool.description) for tool in tools) == sorted(
                (tool["name"], tool["description"]) for tool in manifest["tools"])
            assert (await session.list_resources()).resources == []
            assert (await session.list_prompts()).prompts == []
            result = await session.call_tool("list_revit_installations", {})
            assert json.loads(result.content[0].text)["status"] == "success"
