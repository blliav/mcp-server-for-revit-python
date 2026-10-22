# -*- coding: utf-8 -*-
"""Status and model information tools"""

from typing import Optional

from mcp.server.fastmcp import Context
from .utils import format_response, routing_kwargs


def register_status_tools(mcp, revit_get):
    """Register status-related tools"""

    @mcp.tool()
    async def get_revit_status(ctx: Context, instance: Optional[str] = None) -> str:
        """Check whether a Revit MCP API is active and responding.

        When multiple Revit instances are active, pass the current canonical
        selector (for example ``instance="A"``) or call list_revit_instances
        first. Selectors are dynamic and are assigned by current port order.
        """
        response = await revit_get(
            "/status/", ctx, timeout=10.0, **routing_kwargs(instance)
        )
        return format_response(response)

    @mcp.tool()
    async def get_revit_model_info(ctx: Context, instance: Optional[str] = None) -> str:
        """Get comprehensive information about a current Revit model.

        Use ``instance="A"`` or another selector from list_revit_instances
        when more than one Revit instance is available.
        """
        response = await revit_get(
            "/model_info/", ctx, **routing_kwargs(instance)
        )
        return format_response(response)
