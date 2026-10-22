# -*- coding: utf-8 -*-
import os
import sys
import httpx
import anyio
from mcp.server.fastmcp import FastMCP, Image, Context
import base64
from typing import Optional, Dict, Any, Union

from tools.instance_router import (
    DISCOVERY_HOST,
    DISCOVERY_PORT_START,
    InstanceRoutingError,
    REVIT_MCP_PREFIX,
    resolve_instance,
)

# Create a generic MCP server for interacting with Revit
# Use stateless_http=True and json_response=True for better compatibility
mcp = FastMCP(
    "Revit MCP Server", 
    host="127.0.0.1", 
    port=8000,
    stateless_http=True,
    json_response=True
)

# Configuration
# The submitted local architecture only connects to the same machine.
# Remote pyRevit Routes requires a separately reviewed authenticated HTTPS design.
REVIT_HOST = os.environ.get("REVIT_HOST", "127.0.0.1")
if REVIT_HOST not in {"127.0.0.1", "localhost", "::1"}:
    raise ValueError("REVIT_HOST must be loopback; remote HTTP bridges are not supported.")
REVIT_PORT = int(os.environ.get("REVIT_PORT", "48884"))
if not 1 <= REVIT_PORT <= 65535:
    raise ValueError("REVIT_PORT must be between 1 and 65535.")
URL_HOST = "[::1]" if REVIT_HOST == "::1" else REVIT_HOST
BASE_URL = f"http://{URL_HOST}:{REVIT_PORT}/revit_mcp"


async def _log_routing(ctx: Context, resolved_instance) -> None:
    """Send target visibility to MCP clients without changing tool results."""
    if not ctx:
        return
    info = getattr(ctx, "info", None)
    if not info:
        return
    try:
        await info(
            "Routing request to Revit {} / {} (port {}, {}, document '{}').".format(
                resolved_instance.selector,
                resolved_instance.numeric_alias,
                resolved_instance.port,
                resolved_instance.revit_version,
                resolved_instance.document_title,
            )
        )
    except Exception:
        # Logging must never turn a valid Revit request into a tool failure.
        pass


def _target_error(message: str, resolved_instance) -> str:
    """Annotate transport failures with the exact resolved target."""
    return "{} (Revit {} on port {})".format(
        message, resolved_instance.selector, resolved_instance.port
    )


def _request_url(base_url: str, endpoint: str) -> str:
    path = endpoint if endpoint.startswith("/") else "/" + endpoint
    return "{}{}".format(base_url.rstrip("/"), path)


async def revit_get(
    endpoint: str, ctx: Context = None, instance: str = None, **kwargs
) -> Union[Dict, str]:
    """Make a request-scoped GET request to one Revit Routes instance."""
    return await _revit_call("GET", endpoint, ctx=ctx, instance=instance, **kwargs)


async def revit_post(
    endpoint: str,
    data: Dict[str, Any],
    ctx: Context = None,
    instance: str = None,
    **kwargs
) -> Union[Dict, str]:
    """Make a request-scoped POST request to one Revit Routes instance."""
    return await _revit_call(
        "POST", endpoint, data=data, ctx=ctx, instance=instance, **kwargs
    )


async def revit_image(
    endpoint: str, ctx: Context = None, instance: str = None
) -> Union[Image, str]:
    """GET request that returns an Image object from one Revit instance."""
    resolved_instance = None
    try:
        resolved_instance = await resolve_instance(instance)
        await _log_routing(ctx, resolved_instance)
        async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
            response = await client.get(_request_url(resolved_instance.base_url, endpoint))

            if response.status_code == 200:
                data = response.json()
                image_bytes = base64.b64decode(data["image_data"])
                return Image(data=image_bytes, format="png")
            return _target_error(
                "Error: {} - {}".format(response.status_code, response.text),
                resolved_instance,
            )
    except InstanceRoutingError as exc:
        return "Error: {}".format(exc)
    except httpx.TimeoutException:
        message = "Error: Image export timed out after 60 seconds."
        return _target_error(message, resolved_instance) if resolved_instance else message
    except Exception as e:
        msg = str(e) or type(e).__name__
        message = "Error: {}".format(msg)
        return _target_error(message, resolved_instance) if resolved_instance else message


async def _revit_call(
    method: str,
    endpoint: str,
    data: Dict = None,
    ctx: Context = None,
    timeout: float = 30.0,
    params: Dict = None,
    instance: str = None,
) -> Union[Dict, str]:
    """Resolve one target and handle a single complete HTTP request."""
    resolved_instance = None
    try:
        resolved_instance = await resolve_instance(instance)
        await _log_routing(ctx, resolved_instance)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            url = _request_url(resolved_instance.base_url, endpoint)

            if method == "GET":
                response = await client.get(url, params=params)
            else:  # POST
                response = await client.post(
                    url,
                    json=data,
                    headers={"Content-Type": "application/json"},
                )

            if response.status_code == 200:
                return response.json()
            return _target_error(
                "Error: {} - {}".format(response.status_code, response.text),
                resolved_instance,
            )
    except InstanceRoutingError as exc:
        return "Error: {}".format(exc)
    except httpx.TimeoutException:
        message = (
            "Error: Request timed out after {} seconds. "
            "The operation may still be running in Revit.".format(timeout)
        )
        return _target_error(message, resolved_instance) if resolved_instance else message
    except Exception as e:
        msg = str(e) or type(e).__name__
        message = "Error: {}".format(msg)
        return _target_error(message, resolved_instance) if resolved_instance else message


async def _launch_revit_get(
    endpoint: str, ctx: Context = None, **kwargs
) -> Union[Dict, str]:
    """Poll the legacy bootstrap port for the MCP-local launch tool.

    ``launch_revit`` is deliberately outside request-scoped instance routing:
    it only needs to detect that the Routes service started after spawning a
    process. All normal Revit-facing tools continue through ``revit_get``.
    """
    timeout = kwargs.get("timeout", 30.0)
    url = _request_url(
        "http://{}:{}{}".format(
            DISCOVERY_HOST, DISCOVERY_PORT_START, REVIT_MCP_PREFIX
        ),
        endpoint,
    )
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.get(url, params=kwargs.get("params"))
            if response.status_code == 200:
                return response.json()
            return "Error: {} - {}".format(response.status_code, response.text)
    except httpx.TimeoutException:
        return "Error: Request timed out after {} seconds.".format(timeout)
    except Exception as e:
        msg = str(e) or type(e).__name__
        return "Error: {}".format(msg)


# Register all tools BEFORE the main block
from tools import register_tools
register_tools(mcp, revit_get, revit_post, revit_image, _launch_revit_get)


def build_http_app(transport):
    """Build an authenticated app for the selected loopback transport."""
    if transport not in {"sse", "streamable-http", "combined"}:
        raise ValueError("Unknown HTTP transport.")
    if transport == "sse":
        http_app = mcp.sse_app()
    else:
        http_app = mcp.streamable_http_app()
        if transport == "combined":
            http_app.routes.extend(mcp.sse_app().routes)
    return http_app


async def run_http_async(transport):
    """Run paired, loopback-only SSE, streamable HTTP, or both."""
    import uvicorn
    http_app = build_http_app(transport)

    config = uvicorn.Config(
        http_app,
        host=mcp.settings.host,
        port=mcp.settings.port,
        log_level=mcp.settings.log_level.lower(),
    )
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    transport = "stdio"

    if "--sse" in sys.argv:
        transport = "sse"
    elif "--http" in sys.argv or "--streamable-http" in sys.argv:
        transport = "streamable-http"
    elif "--combined" in sys.argv:
        transport = "combined"

    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        anyio.run(run_http_async, transport)
