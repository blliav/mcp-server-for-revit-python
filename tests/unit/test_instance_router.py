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
    normalize_selector,
    normalize_sibling_endpoint,
    parse_sibling_response,
    port_label_index,
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

    async def get(self, url, timeout=None):
        self.urls.append(url)
        matches = [
            (prefix, response)
            for prefix, response in self.responses
            if url.startswith(prefix)
        ]
        if not matches:
            raise RuntimeError("unexpected URL: {}".format(url))
        prefix, response = max(matches, key=lambda item: len(item[0]))
        if isinstance(response, Exception):
            raise response
        return response


def _healthy(title):
    return FakeResponse(
        payload={
            "status": "active",
            "health": "healthy",
            "revit_available": True,
            "document_title": title,
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


def test_port_label_index_is_anchored_to_the_port_not_the_snapshot_order():
    assert port_label_index(48884) == 1
    assert port_label_index(48885) == 2
    assert port_label_index(48890) == 7
    # Out-of-range ports have no slot, so they continue after the range (7 ports).
    assert port_label_index(48999) == 8
    assert port_label_index(50000, out_of_range_ports=[48999, 50000]) == 9
    with pytest.raises(ValueError):
        port_label_index("not-a-port")
    with pytest.raises(ValueError):
        port_label_index(0)


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
    assert parse_sibling_response(payload) == [48884, 48885, 50000]
    assert normalize_sibling_endpoint("http://localhost:48886/") == (
        DISCOVERY_HOST,
        48886,
    )
    assert normalize_sibling_endpoint(
        {"server_host": "remote.example", "server_port": 48886}
    ) is None


def test_sibling_parser_rejects_malformed_payload():
    assert parse_sibling_response([]) == []
    assert parse_sibling_response({"sisters": []}) == []
    with pytest.raises(ValueError):
        parse_sibling_response({"message": "not a sibling list"})


@pytest.mark.asyncio
async def test_discovery_status_probes_every_in_range_port_directly():
    client = FakeClient(
        [
            ("http://127.0.0.1:48884/revit_mcp/status/", _healthy("A.rvt")),
            ("http://127.0.0.1:48885/revit_mcp/status/", _healthy("B.rvt")),
            ("http://127.0.0.1:48886/revit_mcp/status/", _healthy("C.rvt")),
            (
                "http://127.0.0.1:48884/routes/sisters",
                FakeResponse(payload=[48884, 48885, 48886]),
            ),
        ]
    )

    snapshot = await discover_snapshot(ports=[48884, 48885, 48886], client=client)

    assert [
        (item.selector, item.numeric_alias, item.port) for item in snapshot.instances
    ] == [
        ("A", "1", 48884),
        ("B", "2", 48885),
        ("C", "3", 48886),
    ]
    assert snapshot.scanned_ports == (48884, 48885, 48886)
    assert snapshot.unavailable_ports == ()
    for port in (48884, 48885, 48886):
        assert "http://127.0.0.1:{}/revit_mcp/status/".format(port) in client.urls
    assert sum(1 for url in client.urls if url.endswith("/routes/sisters")) == 1


@pytest.mark.asyncio
async def test_discovery_does_not_hide_healthy_sibling_when_one_status_is_dead():
    client = FakeClient(
        [
            ("http://127.0.0.1:48884/revit_mcp/status/", _healthy("Good.rvt")),
            (
                "http://127.0.0.1:48885/revit_mcp/status/",
                FakeResponse(status_code=503, payload={"status": "unhealthy"}),
            ),
            (
                "http://127.0.0.1:48884/routes/sisters",
                FakeResponse(payload=[48884, 48885]),
            ),
        ]
    )

    snapshot = await discover_snapshot(ports=[48884, 48885], client=client)

    assert len(snapshot.instances) == 1
    assert snapshot.instances[0].port == 48884
    assert snapshot.instances[0].document_title == "Good.rvt"
    assert snapshot.unavailable_ports == ()
    assert snapshot.rejected_candidates == ("48885 (unhealthy: HTTP 503)",)


@pytest.mark.asyncio
async def test_unhealthy_incomplete_registry_does_not_hide_healthy_instances():
    """Regression: an unhealthy first port with an incomplete /sisters must not
    hide healthy instances (the original first-responder bootstrap trusted it).
    """
    client = FakeClient(
        [
            (
                "http://127.0.0.1:48884/revit_mcp/status/",
                FakeResponse(
                    status_code=503,
                    payload={"status": "unhealthy", "revit_available": False},
                ),
            ),
            ("http://127.0.0.1:48885/revit_mcp/status/", _healthy("B.rvt")),
            ("http://127.0.0.1:48886/revit_mcp/status/", _healthy("C.rvt")),
            ("http://127.0.0.1:48884/routes/sisters", FakeResponse(payload=[48884])),
        ]
    )

    snapshot = await discover_snapshot(ports=[48884, 48885, 48886], client=client)

    # Labels stay anchored to the port: the closed 48884 leaves a gap at A, and
    # the surviving instances keep their own selectors instead of shifting down.
    assert [
        (item.selector, item.numeric_alias, item.port, item.document_title)
        for item in snapshot.instances
    ] == [
        ("B", "2", 48885, "B.rvt"),
        ("C", "3", 48886, "C.rvt"),
    ]
    assert snapshot.rejected_candidates == ("48884 (unhealthy: HTTP 503)",)
    assert "http://127.0.0.1:48884/routes/sisters" not in client.urls


@pytest.mark.asyncio
async def test_registry_retries_a_port_the_direct_scan_could_not_reach():
    class FlakyClient(FakeClient):
        def __init__(self, responses):
            super().__init__(responses)
            self.calls = {}

        async def get(self, url, timeout=None):
            self.calls[url] = self.calls.get(url, 0) + 1
            if (
                url == "http://127.0.0.1:48885/revit_mcp/status/"
                and self.calls[url] == 1
            ):
                raise RuntimeError("transient")
            return await super().get(url, timeout)

    client = FlakyClient(
        [
            ("http://127.0.0.1:48884/revit_mcp/status/", _healthy("A.rvt")),
            ("http://127.0.0.1:48885/revit_mcp/status/", _healthy("B.rvt")),
            (
                "http://127.0.0.1:48884/routes/sisters",
                FakeResponse(payload=[48884, 48885]),
            ),
        ]
    )

    snapshot = await discover_snapshot(ports=[48884, 48885], client=client)

    assert [(item.selector, item.port) for item in snapshot.instances] == [
        ("A", 48884),
        ("B", 48885),
    ]
    assert 48885 not in snapshot.unavailable_ports
    assert client.calls["http://127.0.0.1:48885/revit_mcp/status/"] == 2


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
async def test_list_tool_returns_current_mapping(monkeypatch, mock_mcp):
    async def fake_discovery():
        return _snapshot(48884, 48885)

    monkeypatch.setattr("tools.instance_router.discover_snapshot", fake_discovery)
    register_instance_tools(mock_mcp)

    result = json.loads(await mock_mcp.tools["list_revit_instances"](ctx=None))

    assert result["labels_dynamic"] is False
    assert result["selectors_stable"] is True
    assert [item["selector"] for item in result["instances"]] == ["A", "B"]
    assert [item["port"] for item in result["instances"]] == [48884, 48885]


@pytest.mark.asyncio
async def test_discovery_status_scans_then_makes_one_sisters_call():
    client = FakeClient(
        [
            ("http://127.0.0.1:48884/revit_mcp/status/", _healthy("A.rvt")),
            ("http://127.0.0.1:48885/revit_mcp/status/", _healthy("B.rvt")),
            (
                "http://127.0.0.1:48884/routes/sisters",
                FakeResponse(payload=[48884, 48885]),
            ),
        ]
    )

    snapshot = await discover_snapshot(ports=[48884, 48885], client=client)

    assert [(item.selector, item.port) for item in snapshot.instances] == [
        ("A", 48884),
        ("B", 48885),
    ]
    assert sum(1 for url in client.urls if url.endswith("/routes/sisters")) == 1
    assert sum(1 for url in client.urls if url.endswith("/revit_mcp/status/")) == 2
    assert "http://127.0.0.1:48884/routes/sisters" in client.urls


@pytest.mark.asyncio
async def test_discovery_survives_a_dead_first_port():
    client = FakeClient(
        [
            ("http://127.0.0.1:48884/revit_mcp/status/", RuntimeError("dead")),
            ("http://127.0.0.1:48885/revit_mcp/status/", _healthy("B.rvt")),
            (
                "http://127.0.0.1:48885/routes/sisters",
                FakeResponse(payload=[48884, 48885]),
            ),
        ]
    )

    snapshot = await discover_snapshot(ports=[48884, 48885], client=client)

    assert [item.port for item in snapshot.instances] == [48885]
    assert snapshot.instances[0].selector == "B"
    assert snapshot.instances[0].numeric_alias == "2"
    assert 48884 in snapshot.unavailable_ports
    assert sum(1 for url in client.urls if url.endswith("/routes/sisters")) == 1
    assert "http://127.0.0.1:48885/routes/sisters" in client.urls


@pytest.mark.asyncio
async def test_labels_do_not_shift_when_a_lower_instance_closes():
    """Closing 48884 must leave 48885/48886 as B/C instead of renaming them."""
    b_response = _healthy("B.rvt")
    c_response = _healthy("C.rvt")

    without_a = FakeClient(
        [
            ("http://127.0.0.1:48885/revit_mcp/status/", b_response),
            ("http://127.0.0.1:48886/revit_mcp/status/", c_response),
            (
                "http://127.0.0.1:48885/routes/sisters",
                FakeResponse(payload=[48885, 48886]),
            ),
        ]
    )
    snapshot = await discover_snapshot(
        ports=[48884, 48885, 48886], client=without_a
    )

    assert [
        (item.selector, item.numeric_alias, item.port) for item in snapshot.instances
    ] == [("B", "2", 48885), ("C", "3", 48886)]
    assert 48884 in snapshot.unavailable_ports
    assert (await resolve_instance("2", snapshot)).port == 48885
    with pytest.raises(InstanceRoutingError, match="unavailable"):
        await resolve_instance("A", snapshot)

    with_a = FakeClient(
        [
            ("http://127.0.0.1:48884/revit_mcp/status/", _healthy("A.rvt")),
            ("http://127.0.0.1:48885/revit_mcp/status/", b_response),
            ("http://127.0.0.1:48886/revit_mcp/status/", c_response),
            (
                "http://127.0.0.1:48884/routes/sisters",
                FakeResponse(payload=[48884, 48885, 48886]),
            ),
        ]
    )
    reopened = await discover_snapshot(
        ports=[48884, 48885, 48886], client=with_a
    )

    assert [(item.selector, item.port) for item in reopened.instances] == [
        ("A", 48884),
        ("B", 48885),
        ("C", 48886),
    ]


@pytest.mark.asyncio
async def test_discovery_reports_no_responder_in_range():
    client = FakeClient([])

    snapshot = await discover_snapshot(ports=[48884, 48885], client=client)

    assert snapshot.instances == ()
    assert snapshot.scanned_ports == (48884, 48885)
    assert snapshot.unavailable_ports == (48884, 48885)
    assert sum(1 for url in client.urls if url.endswith("/routes/sisters")) == 0

    with pytest.raises(InstanceRoutingError) as excinfo:
        await resolve_instance(snapshot=snapshot)
    message = str(excinfo.value)
    assert "No healthy Revit instances" in message
    assert "Scanned ports" in message
    assert "Unavailable ports" in message
    assert "48885" in message


@pytest.mark.asyncio
async def test_malformed_registry_does_not_hide_in_range_instances():
    client = FakeClient(
        [
            ("http://127.0.0.1:48884/revit_mcp/status/", _healthy("A.rvt")),
            ("http://127.0.0.1:48885/revit_mcp/status/", _healthy("B.rvt")),
            (
                "http://127.0.0.1:48884/routes/sisters",
                FakeResponse(payload={"not_siblings": True}),
            ),
        ]
    )

    snapshot = await discover_snapshot(ports=[48884, 48885], client=client)

    assert [(item.selector, item.port) for item in snapshot.instances] == [
        ("A", 48884),
        ("B", 48885),
    ]


@pytest.mark.asyncio
async def test_discovery_accepts_out_of_range_sibling_but_not_scan_candidate():
    client = FakeClient(
        [
            (
                "http://127.0.0.1:48884/routes/sisters",
                FakeResponse(payload=[48884, 48999]),
            ),
            ("http://127.0.0.1:48884/revit_mcp/status/", _healthy("A.rvt")),
            ("http://127.0.0.1:48999/revit_mcp/status/", _healthy("Out.rvt")),
        ]
    )

    snapshot = await discover_snapshot(ports=[48884, 48999], client=client)

    # 48884 keeps slot A; the out-of-range port continues after the 7-port range.
    assert [
        (item.selector, item.numeric_alias, item.port)
        for item in snapshot.instances
    ] == [
        ("A", "1", 48884),
        ("H", "8", 48999),
    ]
    assert snapshot.scanned_ports == (48884,)
    assert "http://127.0.0.1:48999/routes/sisters" not in client.urls
    assert any(url.startswith("http://127.0.0.1:48999/") for url in client.urls)


def test_sibling_endpoints_accept_any_local_port_and_reject_remote_hosts():
    assert normalize_sibling_endpoint(48999) == (DISCOVERY_HOST, 48999)
    assert normalize_sibling_endpoint("http://127.0.0.1:48999/") == (
        DISCOVERY_HOST,
        48999,
    )
    assert normalize_sibling_endpoint(
        {"server_host": "remote.example", "server_port": 48999}
    ) is None
    assert parse_sibling_response([{"server_port": 48999}, 48885]) == [48885, 48999]
