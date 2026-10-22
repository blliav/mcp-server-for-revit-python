# -*- coding: utf-8 -*-
"""launch_revit must wait for the Revit it started, not an already open one."""
import json
from unittest.mock import AsyncMock, patch

from tools import launch_tools
from tools.instance_router import DiscoverySnapshot, ResolvedInstance
from tools.launch_tools import _wait_for_new_instance, register_launch_tools


def _instance(selector, port, title="Model.rvt"):
    return ResolvedInstance(
        selector=selector,
        numeric_alias=str(port - 48883),
        port=port,
        base_url="http://127.0.0.1:{}/revit_mcp".format(port),
        status={"status": "active", "health": "healthy", "document_title": title},
        document_title=title,
    )


def _snapshots(*steps):
    return AsyncMock(side_effect=[DiscoverySnapshot(instances=tuple(step)) for step in steps])


@patch("tools.launch_tools.anyio.sleep", new_callable=AsyncMock)
async def test_existing_instance_a_is_not_mistaken_for_the_new_revit(mock_sleep):
    a = _instance("A", 48884, "Tower.rvt")
    discover = _snapshots([a], [a], [a, _instance("B", 48885, "No active document")])

    ready, new = await _wait_for_new_instance(None, 60, {48884}, discover, poll_interval=1)

    assert ready is True
    assert (new.selector, new.port) == ("B", 48885)
    assert mock_sleep.await_count == 2


@patch("tools.launch_tools.anyio.sleep", new_callable=AsyncMock)
async def test_first_revit_becomes_instance_a(mock_sleep):
    discover = _snapshots([], [_instance("A", 48884)])

    ready, new = await _wait_for_new_instance(None, 60, set(), discover, poll_interval=1)

    assert ready is True
    assert new.selector == "A"


async def test_times_out_when_no_new_instance_appears():
    a = _instance("A", 48884)
    discover = AsyncMock(return_value=DiscoverySnapshot(instances=(a,)))

    with patch("tools.launch_tools.anyio.sleep", new_callable=AsyncMock), \
            patch("time.time", side_effect=[0, 0, 10, 10, 100]):
        ready, new = await _wait_for_new_instance(None, 30, {48884}, discover, poll_interval=1)

    assert (ready, new) == (False, None)


@patch("tools.launch_tools.anyio.sleep", new_callable=AsyncMock)
async def test_launch_revit_reports_the_new_instance(mock_sleep, mock_mcp, mock_revit_get):
    a = _instance("A", 48884, "Tower.rvt")
    discover = _snapshots([a], [a], [a, _instance("B", 48885, "No active document")])
    register_launch_tools(mock_mcp, mock_revit_get)

    with patch.object(launch_tools, "discover_snapshot", discover), \
            patch.object(launch_tools, "_find_revit_installations",
                         return_value=[{"year": "2026", "path": r"C:\Revit\Revit.exe"}]), \
            patch.object(launch_tools.subprocess, "Popen") as popen:
        result = json.loads(await mock_mcp.tools["launch_revit"](ctx=None))

    popen.assert_called_once()
    assert result["revit_ready"] is True
    assert result["instance"] == "B"
    assert result["port"] == 48885
    assert 'instance="B"' in result["message"]
    mock_revit_get.assert_not_called()
