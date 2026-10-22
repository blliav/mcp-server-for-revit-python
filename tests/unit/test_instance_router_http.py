# -*- coding: utf-8 -*-
"""Instance discovery bypasses proxies and lists Revits without a document."""
import json

import httpx
import pytest

from tools import instance_router
from tools.instance_router import (
    InstanceRoutingError,
    discover_snapshot,
    register_instance_tools,
    resolve_instance,
)

NO_DOCUMENT = {
    "status": "unhealthy",
    "revit_available": False,
    "error": "No active Revit document",
    "revit_version": "Autodesk Revit 2025",
    "api_name": "revit_mcp",
}


@pytest.fixture
def routes_server(monkeypatch):
    """Replace httpx.AsyncClient in the router with a fake Routes server.

    48884 has a model open, 48885 runs Revit with no document, and every
    other port refuses the connection.
    """
    original = httpx.AsyncClient
    state = {"requests": [], "client_kwargs": []}

    def handler(request):
        state["requests"].append(request)
        port = request.url.port
        if request.url.path == "/routes/sisters" and port in (48884, 48885):
            return httpx.Response(200, json=[{"server_port": 48884}, {"server_port": 48885}])
        if request.url.path == "/revit_mcp/status/" and port == 48884:
            return httpx.Response(200, json={
                "status": "active", "health": "healthy", "revit_available": True,
                "document_title": "Tower.rvt", "revit_version": "Autodesk Revit 2025",
            })
        if request.url.path == "/revit_mcp/status/" and port == 48885:
            return httpx.Response(503, json=NO_DOCUMENT)
        raise httpx.ConnectError("refused", request=request)

    def client(**kwargs):
        state["client_kwargs"].append(kwargs)
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(instance_router.httpx, "AsyncClient", client)
    return state


async def test_discovery_ignores_proxies(routes_server):
    snapshot = await discover_snapshot()

    assert routes_server["client_kwargs"][0]["trust_env"] is False
    paths = {request.url.path for request in routes_server["requests"]}
    assert {"/revit_mcp/status/", "/routes/sisters"} <= paths
    assert [item.port for item in snapshot.instances] == [48884, 48885]
    assert snapshot.rejected_candidates == ()


async def test_instance_without_document_is_listed_not_hidden(routes_server, mock_mcp):
    register_instance_tools(mock_mcp)
    result = json.loads(await mock_mcp.tools["list_revit_instances"]())

    assert result["status"] == "success"
    by_selector = {item["selector"]: item for item in result["instances"]}
    assert by_selector["A"]["document_title"] == "Tower.rvt"
    assert by_selector["A"]["healthy"] is True
    assert by_selector["A"]["has_document"] is True
    assert by_selector["B"]["port"] == 48885
    assert by_selector["B"]["health"] == "no_document"
    assert by_selector["B"]["healthy"] is False
    assert by_selector["B"]["has_document"] is False
    assert by_selector["B"]["document_title"] == "No active document"
    assert by_selector["B"]["revit_version"] == "Autodesk Revit 2025"


async def test_instance_without_document_is_routable_but_never_guessed(routes_server):
    # open_document must be able to reach an empty Revit by its selector...
    assert (await resolve_instance("B")).port == 48885
    # ...but with two running instances an omitted selector is still refused.
    with pytest.raises(InstanceRoutingError, match="Multiple Revit instances"):
        await resolve_instance(None)


async def test_unknown_503_is_still_rejected():
    async def get(url, timeout=None):
        if url.endswith("/revit_mcp/status/") and ":48884/" in url:
            return httpx.Response(503, json={"status": "unhealthy", "revit_available": False,
                                             "error": "Revit API context unavailable"})
        raise httpx.ConnectError("refused")

    client = type("Client", (), {"get": staticmethod(get)})()
    snapshot = await discover_snapshot(ports=[48884], client=client)
    assert snapshot.instances == ()
    assert snapshot.rejected_candidates == ("48884 (unhealthy: HTTP 503)",)

