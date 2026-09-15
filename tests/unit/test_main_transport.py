# -*- coding: utf-8 -*-
"""Tests for request-scoped routing in the MCP transport helpers."""

import base64
from unittest.mock import AsyncMock, patch

import pytest

import main
from tools.instance_router import InstanceRoutingError, ResolvedInstance


class FakeResponse:
    status_code = 200
    text = ""

    def json(self):
        return {"status": "success", "data": "ok"}


class FakeClient:
    def __init__(self):
        self.urls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def get(self, url, **kwargs):
        self.urls.append(("GET", url, kwargs))
        return FakeResponse()

    async def post(self, url, **kwargs):
        self.urls.append(("POST", url, kwargs))
        return FakeResponse()


def _instance(selector="B", port=48885):
    return ResolvedInstance(
        selector=selector,
        numeric_alias="2",
        port=port,
        base_url="http://127.0.0.1:{}/revit_mcp".format(port),
        status={"status": "active", "health": "healthy"},
        document_title="Model.rvt",
    )


@pytest.mark.asyncio
async def test_revit_get_resolves_once_and_uses_one_base_url():
    client = FakeClient()
    resolver = AsyncMock(return_value=_instance())
    context = type("ContextStub", (), {"info": AsyncMock()})()

    with patch.object(main, "resolve_instance", resolver), patch.object(
        main.httpx, "AsyncClient", return_value=client
    ):
        result = await main.revit_get(
            "/model_info/", context, instance="Revit B", timeout=12.0
        )

    assert result == {"status": "success", "data": "ok"}
    resolver.assert_awaited_once_with("Revit B")
    assert client.urls == [
        ("GET", "http://127.0.0.1:48885/revit_mcp/model_info/", {"params": None})
    ]
    context.info.assert_awaited_once()


@pytest.mark.asyncio
async def test_revit_post_does_not_retry_an_explicit_target():
    client = FakeClient()
    resolver = AsyncMock(return_value=_instance("A", 48884))

    with patch.object(main, "resolve_instance", resolver), patch.object(
        main.httpx, "AsyncClient", return_value=client
    ):
        result = await main.revit_post(
            "/execute_code/", {"code": "print(1)"}, instance="A"
        )

    assert result["status"] == "success"
    resolver.assert_awaited_once_with("A")
    assert len(client.urls) == 1
    assert client.urls[0][1] == "http://127.0.0.1:48884/revit_mcp/execute_code/"


@pytest.mark.asyncio
async def test_revit_image_uses_the_resolved_target():
    client = FakeClient()
    client.get = AsyncMock(
        return_value=type(
            "ImageResponse",
            (),
            {
                "status_code": 200,
                "text": "",
                "json": lambda self: {
                    "image_data": base64.b64encode(b"png-bytes").decode("ascii")
                },
            },
        )()
    )
    resolver = AsyncMock(return_value=_instance("A", 48884))

    with patch.object(main, "resolve_instance", resolver), patch.object(
        main.httpx, "AsyncClient", return_value=client
    ):
        result = await main.revit_image("/get_view/Level 1", instance="A")

    assert result.data == b"png-bytes"
    resolver.assert_awaited_once_with("A")
    client.get.assert_awaited_once_with(
        "http://127.0.0.1:48884/revit_mcp/get_view/Level 1"
    )


@pytest.mark.asyncio
async def test_ambiguous_routing_returns_error_without_http_call():
    resolver = AsyncMock(
        side_effect=InstanceRoutingError(
            "Multiple Revit instances are active; specify instance A/B/C/..."
        )
    )
    client = FakeClient()

    with patch.object(main, "resolve_instance", resolver), patch.object(
        main.httpx, "AsyncClient", return_value=client
    ):
        result = await main.revit_get("/status/")

    assert result.startswith("Error: Multiple Revit instances")
    assert client.urls == []
