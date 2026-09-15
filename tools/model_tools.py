# -*- coding: utf-8 -*-
"""Model structure and hierarchy tools"""

from mcp.server.fastmcp import Context
from .utils import format_response, routing_kwargs


def register_model_tools(mcp, revit_get):
    """Register model structure tools"""

    @mcp.tool()
    async def list_levels(ctx: Context = None, instance: str = None) -> str:
        """Get all levels in a Revit model.

        Pass a selector such as ``instance="A"`` when multiple Revit
        instances are active; use list_revit_instances to see the mapping.
        """
        response = await revit_get(
            "/list_levels/", ctx, **routing_kwargs(instance)
        )
        return format_response(response)
