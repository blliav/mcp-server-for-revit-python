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
DISCOVERY_RANGE_SIZE = DISCOVERY_PORT_END - DISCOVERY_PORT_START + 1
DISCOVERY_TIMEOUT = 1.0
# Dead loopback ports drop SYNs instead of refusing them, so a full-range scan
# would otherwise burn the whole request timeout on each dead port. Bounding
# only the connect phase keeps the scan cheap while leaving live servers the
# full DISCOVERY_TIMEOUT to answer.
CONNECT_TIMEOUT = 0.1
SISTERS_ENDPOINT = "/routes/sisters"
STATUS_ENDPOINT = "/revit_mcp/status/"
REVIT_MCP_PREFIX = "/revit_mcp"
# Health of a Revit process whose Routes server answers but has no open model.
NO_DOCUMENT_HEALTH = "no_document"
NO_DOCUMENT_ERROR = "no active revit document"

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
    revit_version: str = "Unknown"
    health: str = "healthy"

    def as_dict(self) -> Dict[str, Any]:
        """Return the public representation used by list_revit_instances."""
        return {
            "selector": self.selector,
            "numeric_alias": self.numeric_alias,
            "port": self.port,
            "base_url": self.base_url,
            "healthy": self.health == "healthy",
            "health": self.health,
            "has_document": self.health != NO_DOCUMENT_HEALTH,
            "document_title": self.document_title,
            "revit_version": self.revit_version,
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


def _in_scan_range(port: int) -> bool:
    return DISCOVERY_PORT_START <= port <= DISCOVERY_PORT_END


def port_label_index(port: Any, out_of_range_ports: Sequence[int] = ()) -> int:
    """Return the stable one-based selector index for one healthy port.

    In-range Routes ports map to a fixed slot, so port 48884 is always ``A`` and
    closing a lower instance never renumbers the ones above it. Ports outside
    the scan range have no fixed slot, so they are labeled after the range in
    ascending port order, which keeps them distinct from every slot label.
    """
    normalized = _normalise_sibling_port(port)
    if normalized is None:
        raise ValueError("Instance labels require a valid local port")

    if _in_scan_range(normalized):
        return normalized - DISCOVERY_PORT_START + 1

    ordered = sorted(
        {
            candidate
            for candidate in (
                _normalise_sibling_port(item) for item in out_of_range_ports
            )
            if candidate is not None and not _in_scan_range(candidate)
        }
    )
    try:
        offset = ordered.index(normalized)
    except ValueError:
        offset = len(ordered)
    return DISCOVERY_RANGE_SIZE + offset + 1


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


def _normalise_sibling_port(port: Any) -> Optional[int]:
    """Accept any valid local port reported by the sibling registry.

    Unlike ``_normalise_port`` this is not bounded to the configured Routes
    scan range: ``/routes/sisters`` can legitimately report an instance on any
    port, and the only safety requirement is that the port itself is a valid
    local port number.
    """
    if isinstance(port, bool):
        return None
    try:
        if isinstance(port, float) and not port.is_integer():
            return None
        value = int(str(port).strip())
    except (TypeError, ValueError):
        return None
    if not 1 <= value <= 65535:
        return None
    return value


def _normalise_port(port: Any) -> Optional[int]:
    """Normalize a port for the bounded direct-probe scan only."""
    value = _normalise_sibling_port(port)
    if value is None:
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
        port = _normalise_sibling_port(text)
        return (DISCOVERY_HOST, port) if port is not None else None

    candidate = text
    if "://" not in candidate:
        candidate = "//" + candidate
    try:
        parsed = urlparse(candidate)
        host = _normalise_host(parsed.hostname, default_host)
        port = _normalise_sibling_port(parsed.port)
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
        port = _normalise_sibling_port(value)
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
            normalized_port = _normalise_sibling_port(value[key])
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


def _is_no_document_status(status_code: int, payload: Mapping[str, Any]) -> bool:
    """Recognize the 503 that revit_mcp/status.py returns when no model is open.

    Such a Revit process is running and routable (for example for
    open_document), so discovery lists it instead of rejecting it.
    """
    if status_code != 503 or payload.get("revit_available") is not False:
        return False
    return NO_DOCUMENT_ERROR in str(payload.get("error", "")).lower()


def _document_title(status: Mapping[str, Any]) -> str:
    title = status.get("document_title")
    if title:
        return str(title)
    if status.get("revit_available") is False:
        return "No active document"
    return "Unknown"


def _revit_version(status: Mapping[str, Any]) -> str:
    version = status.get("revit_version")
    if version:
        return str(version)
    return "Unknown"


async def _response_json(response: Any) -> Any:
    payload = response.json()
    if inspect.isawaitable(payload):
        payload = await payload
    return payload


async def _parse_sisters_response(
    response: Any, bootstrap_port: int, default_host: str
) -> Tuple[List[int], Optional[str]]:
    """Parse a sisters response into candidate ports plus an issue kind.

    A responding Routes server is itself a candidate even when its sibling
    cache is stale or malformed, so the bootstrap port is always seeded on an
    HTTP 200.
    """
    if getattr(response, "status_code", None) != 200:
        return [], "unavailable: HTTP {}".format(
            getattr(response, "status_code", "unknown")
        )

    candidates = {bootstrap_port}
    try:
        payload = await _response_json(response)
        candidates.update(parse_sibling_response(payload, default_host))
        return sorted(candidates), None
    except Exception as exc:
        return sorted(candidates), "malformed: {}".format(
            str(exc) or type(exc).__name__
        )


async def _fetch_sister_ports(
    client: Any, bootstrap_port: int, default_host: str
) -> Tuple[int, List[int], Optional[str]]:
    """Fetch one sisters endpoint and return ports plus an issue kind."""
    url = "http://{}:{}{}".format(default_host, bootstrap_port, SISTERS_ENDPOINT)
    try:
        response = await client.get(url)
    except Exception as exc:
        return bootstrap_port, [], "unavailable: {}".format(str(exc) or type(exc).__name__)
    candidates, issue = await _parse_sisters_response(
        response, bootstrap_port, default_host
    )
    return bootstrap_port, candidates, issue


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
    if _is_no_document_status(status_code, payload):
        payload["health"] = NO_DOCUMENT_HEALTH
        return port, payload, None
    if not _is_healthy_status(status_code, payload):
        return port, None, "unhealthy: HTTP {}".format(status_code)
    return port, payload, None


async def _scan_status(
    client: Any, ports: Sequence[int]
) -> Dict[int, Tuple[Optional[Dict[str, Any]], Optional[str]]]:
    """Status-probe every scan port concurrently and cache the results.

    The direct probe is the ground truth for in-range instances: any port that
    answers the ``/revit_mcp/status/`` contract is found here regardless of what
    the ``/routes/sisters`` registry reports. Returns {port: (payload, issue)}.
    """
    results = await asyncio.gather(*(_fetch_status(client, port) for port in ports))
    return {port: (payload, issue) for port, payload, issue in results}


def _select_registry_responder(
    status_cache: Dict[int, Tuple[Optional[Dict[str, Any]], Optional[str]]],
    scan_ports: Sequence[int],
) -> Optional[int]:
    """Pick one in-range port to serve the single ``/routes/sisters`` call.

    A healthy port is preferred because its registry view is the most reliable.
    When none is healthy the lowest port that still answered is used, so a
    registry read never has to trust the first port blindly. Returns ``None``
    when no in-range port answered at all.
    """
    healthy = [
        port
        for port in scan_ports
        if status_cache.get(port, (None, None))[0] is not None
    ]
    if healthy:
        return min(healthy)
    responded = [
        port
        for port in scan_ports
        if status_cache.get(port, (None, None))[1] is not None
        and not status_cache[port][1].startswith("unavailable")
    ]
    return min(responded) if responded else None


async def discover_snapshot(
    host: str = DISCOVERY_HOST,
    ports: Optional[Iterable[int]] = None,
    timeout: float = DISCOVERY_TIMEOUT,
    client: Any = None,
) -> DiscoverySnapshot:
    """Discover and label healthy Revit instances for one request.

    Every in-range port is status-probed directly and concurrently, so healthy
    in-range instances are found independently of the ``/routes/sisters``
    registry; an unhealthy or incomplete registry view cannot hide them. A
    single ``/routes/sisters`` call on one in-range responder then augments the
    snapshot with registered ports outside the scan range. ``client`` is
    injectable for unit tests. When omitted, this function owns and closes an
    ``httpx.AsyncClient``. No result is cached between calls.
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
        status_cache = await _scan_status(active_client, scan_ports)

        candidates = set(scan_ports)

        # One registry read augments the direct scan with ports outside the
        # configured range. It is additive only: a stale or incomplete registry
        # view can never remove or hide an instance that answered its own
        # status probe. Registered ports that the direct scan could not reach
        # are retried once, because a registry entry is evidence of liveness.
        responder = _select_registry_responder(status_cache, scan_ports)
        if responder is not None:
            _, registered_ports, issue = await _fetch_sister_ports(
                active_client, responder, normalized_host
            )
            if issue is None:
                retry_ports = []
                for port in registered_ports:
                    payload, port_issue = status_cache.get(port, (None, None))
                    if payload is not None:
                        continue
                    if port_issue is None or port_issue.startswith("unavailable"):
                        retry_ports.append(port)
                if retry_ports:
                    results = await asyncio.gather(
                        *(
                            _fetch_status(active_client, port)
                            for port in sorted(retry_ports)
                        )
                    )
                    for port, payload, ext_issue in results:
                        status_cache[port] = (payload, ext_issue)
                        candidates.add(port)

        healthy = []
        rejected = []
        unavailable = set()
        malformed = set()
        for port in sorted(candidates):
            payload, issue = status_cache.get(port, (None, None))
            if payload is not None:
                healthy.append((port, payload))
            elif issue:
                if issue.startswith("unavailable"):
                    unavailable.add(port)
                else:
                    if issue.startswith("malformed"):
                        malformed.add(port)
                    rejected.append("{} ({})".format(port, issue))

        resolved_instances = []
        healthy_status = {port: status for port, status in healthy}
        healthy_ports = sorted(healthy_status)
        # Labels are anchored to the Routes port instead of the current position
        # in this snapshot, so an instance keeps its selector while other
        # instances start or close. Gaps are expected: with 48884 closed, the
        # next instance is still labeled B.
        out_of_range_ports = tuple(
            port for port in healthy_ports if not _in_scan_range(port)
        )
        for port in sorted(
            healthy_ports,
            key=lambda item: port_label_index(item, out_of_range_ports),
        ):
            status = healthy_status[port]
            index = port_label_index(port, out_of_range_ports)
            resolved_instances.append(
                ResolvedInstance(
                    selector=alphabetic_label(index),
                    numeric_alias=str(index),
                    port=port,
                    base_url=_base_url(port),
                    status=status,
                    document_title=_document_title(status),
                    revit_version=_revit_version(status),
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

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT),
        trust_env=False,
    ) as active_client:
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
        "{} / {} (port {}, {}, document '{}')".format(
            instance.selector,
            instance.numeric_alias,
            instance.port,
            instance.revit_version,
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
        message = (
            "No healthy Revit instances found. pyRevit Routes was not found "
            "on 127.0.0.1 ports {}-{}.".format(
                DISCOVERY_PORT_START, DISCOVERY_PORT_END
            )
        )
        if current.scanned_ports:
            message += " Scanned ports: {}.".format(
                ", ".join(str(port) for port in current.scanned_ports)
            )
        if current.unavailable_ports:
            message += " Unavailable ports: {}.".format(
                ", ".join(str(port) for port in current.unavailable_ports)
            )
        raise InstanceRoutingError(message, current)

    try:
        normalized = normalize_selector(instance)
    except ValueError as exc:
        raise InstanceRoutingError("{} Available instances: {}".format(exc, mapping), current)

    if normalized is None:
        if len(instances) == 1:
            return instances[0]
        raise InstanceRoutingError(
            "Multiple Revit instances are active; specify which instance to use. "
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
        """List currently healthy Revit instances and their stable selectors.

        Selectors are anchored to the Routes port: 48884 is always ``A``, 48885
        is always ``B``, and so on, with numeric aliases ``1``, ``2``, ... Ports
        outside the configured scan range are labeled after the range. A
        selector therefore keeps referring to the same Revit instance even when
        other instances start or close, and a listed label may have a gap above
        it (for example only B and C when the instance on 48884 is closed). Use
        the returned selector in the ``instance`` argument of Revit-facing tools.
        """
        try:
            snapshot = await discover_snapshot()
            result = {
                "status": "success",
                "instances": [item.as_dict() for item in snapshot.instances],
                "count": len(snapshot.instances),
                "labels_dynamic": False,
                "selectors_stable": True,
                "message": (
                    "Selectors are anchored to the Routes port and stay stable "
                    "when other instances start or close."
                ),
                "discovery": snapshot.as_dict(),
            }
        except Exception as exc:
            result = {
                "status": "error",
                "error": "Revit instance discovery failed: {}".format(str(exc)),
            }
        return json.dumps(result, indent=2)
