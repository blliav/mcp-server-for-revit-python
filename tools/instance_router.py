# -*- coding: utf-8 -*-
"""Discovery and request-scoped routing for local Revit Routes instances."""

import asyncio
import inspect
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import Context


# pyRevit allocates Routes ports starting at this configured port and increments
# for additional Revit processes. Keep the scan bounded to local Routes ports,
# while allowing the label mapping itself to grow without a fixed instance cap.
DISCOVERY_HOST = "127.0.0.1"
DISCOVERY_PORT_START = 48884
DISCOVERY_PORT_END = 48890
DISCOVERY_PORTS = tuple(range(DISCOVERY_PORT_START, DISCOVERY_PORT_END + 1))
DISCOVERY_TIMEOUT = 2.0
SISTERS_ENDPOINT = "/routes/sisters"
STATUS_ENDPOINT = "/revit_mcp/status/"
REVIT_MCP_PREFIX = "/revit_mcp"

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
_SELECTOR_PREFIX = re.compile(r"^revit\s*", re.IGNORECASE)
_ALPHABETIC_SELECTOR = re.compile(r"^[A-Z]+$")
_NUMERIC_SELECTOR = re.compile(r"^[0-9]+$")


class MalformedSiblingResponse(ValueError):
    """Raised when a sisters response contains no usable endpoint data."""


class InstanceRoutingError(RuntimeError):
    """Raised when a request cannot be mapped to one healthy Revit instance."""

    def __init__(self, message: str, snapshot: "DiscoverySnapshot" = None):
        super().__init__(message)
        self.snapshot = snapshot


@dataclass(frozen=True)
class ResolvedInstance:
    """One healthy Revit instance resolved from a discovery snapshot."""

    selector: str
    numeric_alias: str
    port: int
    base_url: str
    status: Mapping[str, Any] = field(default_factory=dict)
    document_title: str = "Unknown"
    health: str = "healthy"

    def as_dict(self) -> Dict[str, Any]:
        """Return the public representation used by list_revit_instances."""
        return {
            "selector": self.selector,
            "numeric_alias": self.numeric_alias,
            "port": self.port,
            "base_url": self.base_url,
            "healthy": True,
            "health": self.health,
            "document_title": self.document_title,
            "status": dict(self.status),
        }


@dataclass(frozen=True)
class DiscoverySnapshot:
    """A complete, request-local view of healthy instances and scan issues."""

    instances: Tuple[ResolvedInstance, ...] = ()
    scanned_ports: Tuple[int, ...] = ()
    unavailable_ports: Tuple[int, ...] = ()
    malformed_ports: Tuple[int, ...] = ()
    rejected_candidates: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        """Return discovery metadata suitable for a JSON tool response."""
        return {
            "instances": [instance.as_dict() for instance in self.instances],
            "scanned_ports": list(self.scanned_ports),
            "unavailable_ports": list(self.unavailable_ports),
            "malformed_ports": list(self.malformed_ports),
            "rejected_candidates": list(self.rejected_candidates),
        }


def alphabetic_label(index: int) -> str:
    """Convert a one-based integer to an unbounded Excel-style label."""
    if not isinstance(index, int) or isinstance(index, bool) or index < 1:
        raise ValueError("Instance labels use one-based positive integers")

    characters = []
    value = index
    while value:
        value, remainder = divmod(value - 1, 26)
        characters.append(chr(ord("A") + remainder))
    return "".join(reversed(characters))


# Descriptive aliases make the label conversion easy to discover for callers
# and keep the implementation name independent from the public terminology.
index_to_label = alphabetic_label
label_for_index = alphabetic_label


def _alphabetic_index(label: str) -> int:
    value = 0
    for character in label:
        value = value * 26 + ord(character) - ord("A") + 1
    return value


def normalize_selector(selector: Any) -> Optional[str]:
    """Normalize ``A``, ``Revit A``, and numeric aliases for lookup.

    Alphabetic values are returned uppercase. Numeric values are returned as a
    canonical one-based decimal string; zero is never a valid alias.
    """
    if selector is None:
        return None

    value = str(selector).strip()
    value = _SELECTOR_PREFIX.sub("", value).strip()
    if not value:
        raise ValueError("The Revit instance selector cannot be empty")

    upper_value = value.upper()
    if _ALPHABETIC_SELECTOR.match(upper_value):
        return upper_value
    if _NUMERIC_SELECTOR.match(value):
        numeric_value = int(value)
        if numeric_value < 1:
            raise ValueError("Numeric Revit instance aliases start at 1")
        return str(numeric_value)
    raise ValueError(
        "Invalid Revit instance selector '{}'; use A/B/C/... or 1/2/3/...".format(
            selector
        )
    )


def _normalise_host(host: Any, default_host: str = DISCOVERY_HOST) -> Optional[str]:
    value = default_host if host is None or str(host).strip() == "" else str(host).strip()
    value = value.lower().strip("[]")
    if value not in {item.strip("[]").lower() for item in _LOCAL_HOSTS}:
        return None
    return DISCOVERY_HOST


def _normalise_port(port: Any) -> Optional[int]:
    if isinstance(port, bool):
        return None
    try:
        if isinstance(port, float) and not port.is_integer():
            return None
        value = int(str(port).strip())
    except (TypeError, ValueError):
        return None
    if not DISCOVERY_PORT_START <= value <= DISCOVERY_PORT_END:
        return None
    return value


def _endpoint_from_url(value: str, default_host: str) -> Optional[Tuple[str, int]]:
    text = value.strip()
    if not text:
        return None

    # A bare port is a valid sibling representation.
    if _NUMERIC_SELECTOR.match(text):
        port = _normalise_port(text)
        return (DISCOVERY_HOST, port) if port is not None else None

    candidate = text
    if "://" not in candidate:
        candidate = "//" + candidate
    try:
        parsed = urlparse(candidate)
        host = _normalise_host(parsed.hostname, default_host)
        port = _normalise_port(parsed.port)
    except (TypeError, ValueError):
        return None
    if host is None or port is None:
        return None
    return host, port


def normalize_sibling_endpoint(
    value: Any, default_host: str = DISCOVERY_HOST
) -> Optional[Tuple[str, int]]:
    """Normalize a sibling port/URL into the local host and numeric port."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        port = _normalise_port(value)
        return (DISCOVERY_HOST, port) if port is not None else None
    if isinstance(value, str):
        return _endpoint_from_url(value, default_host)
    if not isinstance(value, Mapping):
        return None

    # pyRevit's native response uses server_host/server_port. Other versions
    # and wrappers commonly expose the same values as host/port or a URL.
    host = value.get("server_host", value.get("host", default_host))
    for key in (
        "url",
        "base_url",
        "endpoint",
        "address",
        "server_url",
    ):
        if key in value:
            endpoint = _endpoint_from_url(str(value[key]), str(host or default_host))
            if endpoint is not None:
                return endpoint

    for key in ("server_port", "port", "routes_port"):
        if key in value:
            normalized_host = _normalise_host(host, default_host)
            normalized_port = _normalise_port(value[key])
            if normalized_host is not None and normalized_port is not None:
                return normalized_host, normalized_port

    # Permit a nested endpoint object without accepting arbitrary numeric
    # fields such as process IDs or Revit years as ports.
    for key in ("server", "instance", "route", "sibling"):
        if key in value:
            endpoint = normalize_sibling_endpoint(value[key], default_host)
            if endpoint is not None:
                return endpoint
    return None


def parse_sibling_response(
    payload: Any, default_host: str = DISCOVERY_HOST
) -> List[int]:
    """Extract sorted, deduplicated allowed ports from a sisters response.

    Valid entries are retained when a list also contains malformed entries. A
    completely unusable payload raises ``MalformedSiblingResponse`` so callers
    can report that separately from an unavailable port.
    """
    ports = set()

    def collect(value: Any) -> None:
        if isinstance(value, (list, tuple, set)):
            for item in value:
                collect(item)
            return

        if isinstance(value, Mapping):
            direct_endpoint = normalize_sibling_endpoint(value, default_host)
            if direct_endpoint is not None:
                ports.add(direct_endpoint[1])
                return
            for key in ("sisters", "servers", "instances", "data", "results"):
                if key in value:
                    collect(value[key])
            return

        endpoint = normalize_sibling_endpoint(value, default_host)
        if endpoint is not None:
            ports.add(endpoint[1])

    # An empty sisters list is a valid response for a single Routes server.
    if isinstance(payload, (list, tuple, set)) and not payload:
        return []

    collect(payload)
    if isinstance(payload, Mapping) and any(
        isinstance(payload[key], (list, tuple, set)) and not payload[key]
        for key in ("sisters", "servers", "instances", "data", "results")
        if key in payload
    ):
        # Wrapped empty lists are valid for the same reason as a direct list.
        return sorted(ports)
    if not ports:
        raise MalformedSiblingResponse(
            "The /routes/sisters response contained no allowed local endpoints"
        )
    return sorted(ports)


def _base_url(port: int, host: str = DISCOVERY_HOST) -> str:
    return "http://{}:{}/revit_mcp".format(host, port)


def _status_payload(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, Mapping):
        nested = payload.get("data")
        if isinstance(nested, Mapping):
            return dict(nested)
        return dict(payload)
    return {}


def _is_healthy_status(status_code: int, payload: Mapping[str, Any]) -> bool:
    if status_code != 200:
        return False
    status = str(payload.get("status", "")).lower()
    health = str(payload.get("health", "")).lower()
    if status in {"error", "unhealthy", "inactive"}:
        return False
    return bool(
        payload.get("revit_available") is True
        or health == "healthy"
        or status == "active"
    )


def _document_title(status: Mapping[str, Any]) -> str:
    title = status.get("document_title")
    if title:
        return str(title)
    if status.get("revit_available") is False:
        return "No active document"
    return "Unknown"


async def _response_json(response: Any) -> Any:
    payload = response.json()
    if inspect.isawaitable(payload):
        payload = await payload
    return payload


async def _fetch_sister_ports(
    client: Any, bootstrap_port: int, default_host: str
) -> Tuple[int, List[int], Optional[str]]:
    """Fetch one sisters endpoint and return ports plus an issue kind."""
    url = "http://{}:{}{}".format(default_host, bootstrap_port, SISTERS_ENDPOINT)
    try:
        response = await client.get(url)
    except Exception as exc:
        return bootstrap_port, [], "unavailable: {}".format(str(exc) or type(exc).__name__)

    if getattr(response, "status_code", None) != 200:
        return bootstrap_port, [], "unavailable: HTTP {}".format(
            getattr(response, "status_code", "unknown")
        )

    # A responding Routes server is itself a candidate even if its sibling
    # cache is stale or malformed.
    candidates = {bootstrap_port}
    try:
        payload = await _response_json(response)
        candidates.update(parse_sibling_response(payload, default_host))
        return bootstrap_port, sorted(candidates), None
    except Exception as exc:
        return bootstrap_port, sorted(candidates), "malformed: {}".format(
            str(exc) or type(exc).__name__
        )


async def _fetch_status(
    client: Any, port: int
) -> Tuple[int, Optional[Dict[str, Any]], Optional[str]]:
    url = "{}{}".format(_base_url(port), STATUS_ENDPOINT[len(REVIT_MCP_PREFIX) :])
    try:
        response = await client.get(url)
    except Exception as exc:
        return port, None, "unavailable: {}".format(str(exc) or type(exc).__name__)

    try:
        payload = _status_payload(await _response_json(response))
    except Exception as exc:
        return port, None, "malformed status: {}".format(
            str(exc) or type(exc).__name__
        )

    status_code = getattr(response, "status_code", None)
    if not _is_healthy_status(status_code, payload):
        return port, None, "unhealthy: HTTP {}".format(status_code)
    return port, payload, None


async def discover_snapshot(
    host: str = DISCOVERY_HOST,
    ports: Optional[Iterable[int]] = None,
    timeout: float = DISCOVERY_TIMEOUT,
    client: Any = None,
) -> DiscoverySnapshot:
    """Discover and label healthy Revit instances for one request.

    ``client`` is injectable for unit tests. When omitted, this function owns
    and closes an ``httpx.AsyncClient``. No result is cached between calls.
    """
    normalized_host = _normalise_host(host, DISCOVERY_HOST)
    if normalized_host is None:
        raise ValueError("Discovery host must be local")

    scan_ports = tuple(
        sorted(
            {
                normalized_port
                for normalized_port in (
                    _normalise_port(port)
                    for port in (DISCOVERY_PORTS if ports is None else ports)
                )
                if normalized_port is not None
            }
        )
    )

    async def run(active_client: Any) -> DiscoverySnapshot:
        sister_results = await asyncio.gather(
            *(
                _fetch_sister_ports(active_client, port, normalized_host)
                for port in scan_ports
            )
        )

        candidates = set()
        unavailable = set()
        malformed = set()
        for bootstrap_port, candidate_ports, issue in sister_results:
            candidates.update(candidate_ports)
            if issue:
                if issue.startswith("malformed"):
                    malformed.add(bootstrap_port)
                else:
                    unavailable.add(bootstrap_port)

        status_results = await asyncio.gather(
            *(_fetch_status(active_client, port) for port in sorted(candidates))
        )
        healthy = []
        rejected = []
        for port, status, issue in status_results:
            if status is not None:
                healthy.append((port, status))
            elif issue:
                if issue.startswith("unavailable"):
                    unavailable.add(port)
                elif issue.startswith("malformed"):
                    malformed.add(port)
                rejected.append("{} ({})".format(port, issue))

        resolved_instances = []
        for index, (port, status) in enumerate(sorted(healthy), start=1):
            selector = alphabetic_label(index)
            resolved_instances.append(
                ResolvedInstance(
                    selector=selector,
                    numeric_alias=str(index),
                    port=port,
                    base_url=_base_url(port),
                    status=status,
                    document_title=_document_title(status),
                    health=str(status.get("health", "healthy")),
                )
            )

        return DiscoverySnapshot(
            instances=tuple(resolved_instances),
            scanned_ports=scan_ports,
            unavailable_ports=tuple(sorted(unavailable)),
            malformed_ports=tuple(sorted(malformed)),
            rejected_candidates=tuple(rejected),
        )

    if client is not None:
        return await run(client)

    async with httpx.AsyncClient(timeout=timeout) as active_client:
        return await run(active_client)


async def discover_instances(
    host: str = DISCOVERY_HOST,
    ports: Optional[Iterable[int]] = None,
    timeout: float = DISCOVERY_TIMEOUT,
    client: Any = None,
) -> List[ResolvedInstance]:
    """Return the healthy instances from a fresh discovery snapshot."""
    snapshot = await discover_snapshot(host, ports, timeout, client)
    return list(snapshot.instances)


def format_instance_mapping(instances: Sequence[ResolvedInstance]) -> str:
    """Format selectors, ports, and document titles for routing errors."""
    if not instances:
        return "(none)"
    return "; ".join(
        "{} / {} (port {}, document '{}')".format(
            instance.selector,
            instance.numeric_alias,
            instance.port,
            instance.document_title,
        )
        for instance in instances
    )


async def resolve_instance(
    instance: Any = None,
    snapshot: Optional[DiscoverySnapshot] = None,
) -> ResolvedInstance:
    """Resolve one selector according to the zero/one/multiple policy."""
    current = snapshot if snapshot is not None else await discover_snapshot()
    instances = list(current.instances)
    mapping = format_instance_mapping(instances)

    if not instances:
        raise InstanceRoutingError(
            "No healthy Revit instances found. pyRevit Routes was not found "
            "on 127.0.0.1 ports {}-{}.".format(
                DISCOVERY_PORT_START, DISCOVERY_PORT_END
            ),
            current,
        )

    try:
        normalized = normalize_selector(instance)
    except ValueError as exc:
        raise InstanceRoutingError("{} Available instances: {}".format(exc, mapping), current)

    if normalized is None:
        if len(instances) == 1:
            return instances[0]
        raise InstanceRoutingError(
            "Multiple Revit instances are active; specify instance A/B/C/... "
            "Available instances: {}. Call list_revit_instances first if needed.".format(
                mapping
            ),
            current,
        )

    for candidate in instances:
        if normalized in {candidate.selector, candidate.numeric_alias}:
            return candidate

    raise InstanceRoutingError(
        "Revit instance selector '{}' is unavailable in the current discovery "
        "snapshot. Available instances: {}. Do not fall back to another model.".format(
            instance, mapping
        ),
        current,
    )


resolve_revit_instance = resolve_instance


def register_instance_tools(mcp: Any) -> None:
    """Register the MCP tool that exposes the current dynamic mapping."""

    @mcp.tool()
    async def list_revit_instances(ctx: Context = None) -> str:
        """List currently healthy Revit instances and their dynamic selectors.

        Canonical selectors are assigned by ascending Routes port for this
        discovery snapshot. They are intentionally dynamic and may change when
        Revit instances start or close. Use the returned selector (for example
        ``A`` or ``B``) in the ``instance`` argument of Revit-facing tools.
        """
        try:
            snapshot = await discover_snapshot()
            result = {
                "status": "success",
                "instances": [item.as_dict() for item in snapshot.instances],
                "count": len(snapshot.instances),
                "labels_dynamic": True,
                "message": (
                    "Selectors are assigned by current port order and may change "
                    "after an instance starts or closes."
                ),
                "discovery": snapshot.as_dict(),
            }
        except Exception as exc:
            result = {
                "status": "error",
                "error": "Revit instance discovery failed: {}".format(str(exc)),
            }
        return json.dumps(result, indent=2)
