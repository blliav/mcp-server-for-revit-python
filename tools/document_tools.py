# -*- coding: utf-8 -*-
"""Document management tools for Revit MCP Server"""

from typing import Optional

from mcp.server.fastmcp import Context
from .utils import format_response, routing_kwargs


def register_document_tools(mcp, revit_get, revit_post):
    """Register document management tools with the MCP server."""

    @mcp.tool()
    async def open_document(
        ctx: Context,
        file_path: str,
        detach: bool = False,
        audit: bool = False,
        instance: Optional[str] = None,
    ) -> str:
        """Open a Revit document file in the running Revit instance.

        Supports workshared (central) files with options to detach from
        central or audit the file on open.

        Args:
            file_path: Absolute path to a .rvt, .rfa, or .rte file.
            detach: If True, open detached from central (workshared files only).
                    Preserves worksets but severs the link to the central model.
            audit: If True, audit the file on open to check for corruption.
            instance: Current Revit selector, such as "A" or "B". Call
                list_revit_instances first when multiple instances are active.
        """
        data = {
            "file_path": file_path,
            "detach": detach,
            "audit": audit,
        }
        response = await revit_post(
            "/open_document/", data, ctx, timeout=120.0, **routing_kwargs(instance)
        )
        return format_response(response)

    @mcp.tool()
    async def close_document(
        ctx: Context,
        save: bool = False,
        instance: Optional[str] = None,
    ) -> str:
        """Close the active Revit document.

        Args:
            save: If True, save the document before closing.
                  If False (default), close without saving.
            instance: Current Revit selector from list_revit_instances.
        """
        data = {"save": save}
        response = await revit_post(
            "/close_document/", data, ctx, **routing_kwargs(instance)
        )
        return format_response(response)

    @mcp.tool()
    async def save_document(
        ctx: Context,
        file_path: Optional[str] = None,
        instance: Optional[str] = None,
    ) -> str:
        """Save the active Revit document.

        If file_path is omitted, saves the document in place.
        If file_path is provided, performs a Save As to the new location.

        Args:
            file_path: Optional path for Save As. If omitted, saves in place.
            instance: Current Revit selector from list_revit_instances.
        """
        data = {"file_path": file_path}
        response = await revit_post(
            "/save_document/", data, ctx, **routing_kwargs(instance)
        )
        return format_response(response)

    @mcp.tool()
    async def sync_with_central(
        ctx: Context,
        comment: str = "",
        compact: bool = False,
        relinquish_all: bool = True,
        instance: Optional[str] = None,
    ) -> str:
        """Synchronize the active workshared document with central.

        Only works with workshared (central model) documents. For non-workshared
        documents, use save_document instead.

        Args:
            comment: Sync comment visible in the worksharing log.
            compact: If True, compact the central model during sync.
            relinquish_all: If True (default), relinquish all borrowed elements
                           and worksets after sync.
            instance: Current Revit selector from list_revit_instances.
        """
        data = {
            "comment": comment,
            "compact": compact,
            "relinquish_all": relinquish_all,
        }
        response = await revit_post(
            "/sync_with_central/",
            data,
            ctx,
            timeout=120.0,
            **routing_kwargs(instance)
        )
        return format_response(response)
