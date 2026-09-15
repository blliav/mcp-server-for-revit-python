# -*- coding: utf-8 -*-
"""MCP registration and visible selector schema checks."""

from mcp.server.fastmcp import FastMCP

from tools import register_tools


async def _get(*args, **kwargs):
    return {"status": "success"}


async def _post(*args, **kwargs):
    return {"status": "success"}


async def _image(*args, **kwargs):
    return "image"


def test_instance_listing_is_exposed_and_revit_tools_advertise_instance():
    server = FastMCP("schema-test")
    register_tools(server, _get, _post, _image)
    tools = {tool.name: tool for tool in server._tool_manager.list_tools()}

    assert "list_revit_instances" in tools
    assert "instance" not in tools["list_revit_instances"].parameters.get(
        "properties", {}
    )

    excluded = {"list_revit_instances", "list_revit_installations", "launch_revit"}
    for name, tool in tools.items():
        if name in excluded:
            continue
        assert "instance" in tool.parameters.get("properties", {}), name
