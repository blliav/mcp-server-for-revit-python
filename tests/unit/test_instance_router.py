# -*- coding: utf-8 -*-
"""Tests for Revit instance discovery and request-scoped selection."""

import pytest
import json

from tools.instance_router import (
    DISCOVERY_HOST,
    DiscoverySnapshot,
    InstanceRoutingError,
    ResolvedInstance,
    alphabetic_label,
    discover_snapshot,
    format_instance_mapping,
    normalize_selector,
    normalize_sibling_endpoint,
    parse_sibling_response,
    resolve_instance,
    register_instance_tools,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or str(payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.urls = []

    async def get(self, url):
        self.urls.append(url)
        for prefix, response in self.responses:
            if url.startswith(prefix):
                if isinstance(response, Exception):
                    raise response
                return response
        raise RuntimeError("unexpected URL: {}".format(url))


def _healthy(title, version="Revit 2025"):
    return FakeResponse(
        payload={
            "status": "active",
            "health": "healthy",
            "revit_available": True,
            "document_title": title,
            "revit_version": version,
        }
    )


def _snapshot(*ports):
    instances = tuple(
        ResolvedInstance(
            selector=alphabetic_label(index),
            numeric_alias=str(index),
            port=port,
            base_url="http://127.0.0.1:{}/revit_mcp".format(port),
            status={"status": "active", "health": "healthy"},
            document_title="Model {}".format(index),
            revit_version="Revit 2026",
        )
        for index, port in enumerate(sorted(ports), start=1)
    )
    return DiscoverySnapshot(instances=instances)


def test_alphabetic_labels_continue_past_z():
    assert alphabetic_label(1) == "A"
    assert alphabetic_label(26) == "Z"
    assert alphabetic_label(27) == "AA"
    assert alphabetic_label(28) == "AB"
    assert alphabetic_label(52) == "AZ"
    assert alphabetic_label(53) == "BA"


@pytest.mark.parametrize(
    "value, expected",
    [
        ("a", "A"),
        ("Revit B", "B"),
        ("revitAA", "AA"),
        ("1", "1"),
        ("Revit 27", "27"),
    ],
)
def test_selector_normalization(value, expected):
    assert normalize_selector(value) == expected


def test_selector_normalization_rejects_zero_and_unknown_values():
    with pytest.raises(ValueError):
        normalize_selector("0")
    with pytest.raises(ValueError):
        normalize_selector("A-1")


def test_sibling_parser_normalizes_urls_ports_and_deduplicates():
    payload = [
        {"server_host": "", "server_port": 48885},
        {"url": "http://127.0.0.1:48884/revit_mcp"},
        "localhost:48885",
        {"server_port": 50000},
        {"server_host": "remote.example", "server_port": 48886},
    ]
    assert parse_sibling_response(payload) == [48884, 48885]
    assert normalize_sibling_endpoint("http://localhost:48886/") == (
        DISCOVERY_HOST,
        48886,
    )


def test_sibling_parser_rejects_malformed_payload():
    assert parse_sibling_response([]) == []
    assert parse_sibling_response({"sisters": []}) == []
    with pytest.raises(ValueError):
        parse_sibling_response({"message": "not a sibling list"})


@pytest.mark.asyncio
async def test_discovery_merges_siblings_and_keeps_malformed_bootstrap_visible():
    client = FakeClient(
        [
            (
                "http://127.0.0.1:48884/routes/sisters",
                FakeResponse(payload={"not_siblings": True}),
            ),
            (
                "http://127.0.0.1:48885/routes/sisters",
                FakeResponse(payload=["http://127.0.0.1:48884", 48886]),
            ),
            ("http://127.0.0.1:48886/routes/sisters", RuntimeError("dead")),
            ("http://127.0.0.1:48884/revit_mcp/status/", _healthy("A.rvt")),
            ("http://127.0.0.1:48885/revit_mcp/status/", _healthy("B.rvt")),
            ("http://127.0.0.1:48886/revit_mcp/status/", _healthy("C.rvt")),
        ]
    )

    snapshot = await discover_snapshot(ports=[48884, 48885, 48886], client=client)

    assert [(item.selector, item.numeric_alias, item.port) for item in snapshot.instances] == [
        ("A", "1", 48884),
        ("B", "2", 48885),
        ("C", "3", 48886),
    ]
    assert [item.revit_version for item in snapshot.instances] == [
        "Revit 2025",
        "Revit 2025",
        "Revit 2025",
    ]
    assert [item.as_dict()["revit_version"] for item in snapshot.instances] == [
        "Revit 2025",
        "Revit 2025",
        "Revit 2025",
    ]
    assert snapshot.malformed_ports == (48884,)
    assert snapshot.unavailable_ports == (48886,)
    assert any(url.endswith("/routes/sisters") for url in client.urls)
    assert any(url.endswith("/revit_mcp/status/") for url in client.urls)


@pytest.mark.asyncio
async def test_discovery_does_not_hide_healthy_sibling_when_one_status_is_dead():
    client = FakeClient(
        [
            (
                "http://127.0.0.1:48884/routes/sisters",
                FakeResponse(payload=[48884, 48885]),
            ),
            ("http://127.0.0.1:48885/routes/sisters", RuntimeError("dead")),
            ("http://127.0.0.1:48884/revit_mcp/status/", _healthy("Good.rvt")),
            (
                "http://127.0.0.1:48885/revit_mcp/status/",
                FakeResponse(status_code=503, payload={"status": "unhealthy"}),
            ),
        ]
    )

    snapshot = await discover_snapshot(ports=[48884, 48885], client=client)

    assert len(snapshot.instances) == 1
    assert snapshot.instances[0].port == 48884
    assert snapshot.instances[0].document_title == "Good.rvt"


@pytest.mark.asyncio
async def test_resolve_policy_for_zero_one_and_multiple_instances():
    with pytest.raises(InstanceRoutingError, match="No healthy Revit instances"):
        await resolve_instance(snapshot=DiscoverySnapshot())

    one = _snapshot(48884)
    assert (await resolve_instance(snapshot=one)).selector == "A"

    multiple = _snapshot(48884, 48885)
    with pytest.raises(InstanceRoutingError, match="Multiple Revit instances"):
        await resolve_instance(snapshot=multiple)
    assert (await resolve_instance("b", multiple)).port == 48885
    assert (await resolve_instance("Revit 2", multiple)).selector == "B"


@pytest.mark.asyncio
async def test_resolve_rejects_unknown_selector_without_fallback():
    with pytest.raises(InstanceRoutingError, match="unavailable"):
        await resolve_instance("C", _snapshot(48884, 48885))


@pytest.mark.asyncio
async def test_resolve_discovers_for_each_request(monkeypatch):
    calls = []
    snapshot = _snapshot(48884)

    async def fake_discovery():
        calls.append(True)
        return snapshot

    monkeypatch.setattr("tools.instance_router.discover_snapshot", fake_discovery)
    await resolve_instance()
    await resolve_instance()

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_missing_revit_version_defaults_to_unknown():
    client = FakeClient(
        [
            ("http://127.0.0.1:48884/routes/sisters", FakeResponse(payload=[48884])),
            (
                "http://127.0.0.1:48884/revit_mcp/status/",
                FakeResponse(
                    payload={
                        "status": "active",
                        "health": "healthy",
                        "revit_available": True,
                        "document_title": "Legacy.rvt",
                    }
                ),
            ),
        ]
    )

    snapshot = await discover_snapshot(ports=[48884], client=client)

    assert snapshot.instances[0].document_title == "Legacy.rvt"
    assert snapshot.instances[0].revit_version == "Unknown"


def test_mapping_format_includes_revit_version():
    mapped = format_instance_mapping(list(_snapshot(48884, 48885).instances))
    assert "port 48884, Revit 2026, document 'Model 1'" in mapped
    assert "Revit 2026" in mapped


@pytest.mark.asyncio
async def test_list_tool_returns_current_mapping(monkeypatch, mock_mcp):
    async def fake_discovery():
        return _snapshot(48884, 48885)

    monkeypatch.setattr("tools.instance_router.discover_snapshot", fake_discovery)
    register_instance_tools(mock_mcp)

    result = json.loads(await mock_mcp.tools["list_revit_instances"](ctx=None))

    assert result["labels_dynamic"] is True
    assert [item["selector"] for item in result["instances"]] == ["A", "B"]
    assert [item["port"] for item in result["instances"]] == [48884, 48885]
