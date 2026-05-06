import asyncio
import functools
import inspect
import json
import os
import pathlib
from datetime import datetime, timezone

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from ssr_mcp.client import SSRClient

load_dotenv(pathlib.Path(__file__).parent.parent / ".env", override=True)

# ------------------------------------------------------------------
# HTTP transport configuration
# ------------------------------------------------------------------

_TRANSPORT = os.environ.get("SSR_MCP_TRANSPORT", "stdio")
_HOST = os.environ.get("SSR_MCP_HOST", "127.0.0.1")
_PORT = int(os.environ.get("SSR_MCP_PORT", "8000"))
_AUTH_TOKEN = os.environ.get("SSR_MCP_AUTH_TOKEN")


class _BearerAuthMiddleware:
    """Pure-ASGI bearer token gate. Passes lifespan/websocket scopes through unchanged."""

    def __init__(self, app: object, token: str) -> None:
        self._app = app
        self._token = token

    async def __call__(self, scope: dict, receive: object, send: object) -> None:
        if scope["type"] == "http":
            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            auth = headers.get(b"authorization", b"").decode()
            if not (auth.startswith("Bearer ") and auth[7:] == self._token):
                await self._reject(scope, send)
                return
        await self._app(scope, receive, send)

    @staticmethod
    async def _reject(scope: dict, send: object) -> None:
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [[b"www-authenticate", b"Bearer"], [b"content-length", b"12"]],
        })
        await send({"type": "http.response.body", "body": b"Unauthorized"})


mcp = FastMCP(
    "SSR — Session Smart Router",
    host=_HOST,
    port=_PORT,
)

# ------------------------------------------------------------------
# Tool-call logging
# ------------------------------------------------------------------

_LOG_PATH = pathlib.Path(
    os.environ.get("SSR_MCP_LOG_FILE", pathlib.Path.home() / ".ssr-mcp" / "tool_calls.jsonl")
)


def _log_tool_call(name: str, kwargs: dict, response: str) -> None:
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": name,
            "args": kwargs,
            "response_chars": len(response),
        }
        with _LOG_PATH.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass  # never let logging break a tool call


_mcp_tool = mcp.tool


def _logged_tool(*args, **kwargs):
    original_decorator = _mcp_tool(*args, **kwargs)

    def decorator(fn):
        @functools.wraps(fn)
        async def logged(**fkwargs):
            result = await fn(**fkwargs)
            _log_tool_call(fn.__name__, fkwargs, result)
            return result

        logged.__signature__ = inspect.signature(fn)
        return original_decorator(logged)

    return decorator


mcp.tool = _logged_tool

# ------------------------------------------------------------------
# Client singleton
# ------------------------------------------------------------------

# Client is initialised lazily on first use so the server starts even if
# credentials aren't set yet (useful during development).
_client: SSRClient | None = None


def get_client() -> SSRClient:
    global _client
    if _client is None:
        host = os.environ["SSR_HOST"]
        username = os.environ["SSR_USERNAME"]
        password = os.environ["SSR_PASSWORD"]
        verify_ssl = os.environ.get("SSR_VERIFY_SSL", "true").lower() != "false"
        port = int(os.environ.get("SSR_PORT", "443"))
        _client = SSRClient(host, username, password, verify_ssl, port)
    return _client


# ------------------------------------------------------------------
# Routers
# ------------------------------------------------------------------


@mcp.tool()
async def get_connection_info() -> str:
    """Identify the device SSR_HOST is connected to and determine which tools
    are applicable. Call this first in any session.

    Returns mode, router name, node name, role, software version, and alarm
    counts for the connected device.

    Modes:
      conductor       — SSR_HOST is a conductor. All managed routers are
                        accessible by name via the router: parameter.
                        Conductor-specific tools (get_assets, find_sessions)
                        are available. Use list_routers to enumerate managed
                        routers.

      router-managed  — SSR_HOST is a router managed by a conductor but you
                        are connected directly to the router, not the conductor.
                        Only this router is accessible. Use the router and node
                        names from this response for all router-targeted tools.

      router-cloud    — SSR_HOST is a Mist/cloud-managed router with no
                        on-premises conductor. Only this router is accessible.
                        Conductor-specific tools are not available.

      router-standalone — SSR_HOST is a standalone router with no conductor
                          or cloud management. Only this router is accessible.
    """
    data = await get_client().get_connection_info()
    return json.dumps(data, indent=2)


@mcp.tool()
async def list_routers() -> str:
    """List all routers managed by the conductor, or the local router when
    connected directly to a standalone router.

    Context: conductor — returns all managed routers.
             standalone router — returns only the local device.

    Use get_connection_info for richer context including the connection mode.
    """
    routers = await get_client().get_routers()
    return json.dumps(routers, indent=2)


@mcp.tool()
async def get_router(router: str) -> str:
    """Get details for a specific router by name.

    Context: router — pass the router name from list_routers or
             get_connection_info.

    Args:
        router: Router name (e.g. 'boston-branch-01').
    """
    data = await get_client().get_router(router)
    return json.dumps(data, indent=2)


@mcp.tool()
async def list_router_nodes(router: str) -> str:
    """List the nodes (control/combo/primary/secondary) that make up a router.

    Context: router — targets a specific managed router or the local device.

    Args:
        router: Router name.
    """
    nodes = await get_client().get_router_nodes(router)
    return json.dumps(nodes, indent=2)


# ------------------------------------------------------------------
# Alarms
# ------------------------------------------------------------------


@mcp.tool()
async def get_alarms(router: str | None = None, node: str | None = None) -> str:
    """Retrieve active alarms.

    Context: any — when router is omitted, returns all alarms visible to the
             connected device (authority-wide on a conductor; local on a
             standalone router). Narrow with router/node to target a specific
             router.

    Args:
        router: (optional) Limit to alarms on this router.
        node:   (optional) Limit to alarms on this node within the router.
    """
    alarms = await get_client().get_alarms(router, node)
    return json.dumps(alarms, indent=2)


# ------------------------------------------------------------------
# Health summary (composite)
# ------------------------------------------------------------------


@mcp.tool()
async def get_router_health(
    router: str,
    node: str | None = None,
) -> str:
    """Get a concise health summary for a router: alarms, node status, process
    state, and resource utilization in a single call.

    Use this when asked whether a router is healthy, what is wrong with it,
    or to triage a reported problem. Follow up with more specific tools only
    if the summary reveals something worth investigating.

    For static router facts (nodes, software version, app-id capability) use
    get_router_info instead.

    Returns overall_status:
      ONLINE   — all nodes online, no alarms, all processes running
      DEGRADED — nodes online but active alarms or processes not running
      OFFLINE  — one or more nodes not online

    Args:
        router: Router name (required).
        node:   (optional) Limit to a specific node.
    """
    client = get_client()

    alarms_data, state_data, processes_data, util_data = await asyncio.gather(
        client.get_alarms(router, node),
        client.get_system_state(router, node),
        client.get_system_processes(router, node),
        client.get_node_utilization(router, node),
    )

    router_nodes_state = (
        state_data
        .get("data", {})
        .get("allRouters", {})
        .get("nodes", [{}])[0]
        .get("nodes", {})
        .get("nodes", [])
    )
    router_nodes_procs = (
        processes_data
        .get("data", {})
        .get("allRouters", {})
        .get("nodes", [{}])[0]
        .get("nodes", {})
        .get("nodes", [])
    )

    procs_by_node = {
        n["name"]: n.get("state", {}).get("processes", [])
        for n in router_nodes_procs
    }
    util_by_node = {u["name"]: u for u in _parse_node_utilization(util_data)}

    nodes_summary = []
    any_offline = False

    for n in router_nodes_state:
        name = n.get("name")
        state = n.get("state", {})
        status = state.get("status", "UNKNOWN")
        if status != "RUNNING":
            any_offline = True

        processes = procs_by_node.get(name, [])
        processes_down = [p["name"] for p in processes if p.get("status") != "RUNNING"]

        util = util_by_node.get(name, {})
        node_entry = {
            "name": name,
            "status": status,
            "role": state.get("role"),
            "software_version": state.get("softwareVersion"),
            "start_time": state.get("startTime"),
            "alarm_count": state.get("alarmCount", 0),
            "processes_down": processes_down,
        }
        if util:
            node_entry["cpu_high"] = util["cpu_high"]
            node_entry["memory_usage_pct"] = util["memory_usage_pct"]
            node_entry["disk_high"] = util["disk_high"]

        nodes_summary.append(node_entry)

    alarms_summary = [
        {
            "severity": a.get("severity"),
            "category": a.get("category"),
            "message": a.get("message"),
            "node": a.get("node"),
            "timestamp": a.get("timestamp"),
        }
        for a in alarms_data
    ]

    resource_pressure = any(
        n.get("cpu_high") or n.get("memory_usage_pct", 0) >= 90 or n.get("disk_high")
        for n in nodes_summary
    )
    has_issues = alarms_summary or any(n["processes_down"] for n in nodes_summary) or resource_pressure

    if any_offline:
        overall = "OFFLINE"
    elif has_issues:
        overall = "DEGRADED"
    else:
        overall = "ONLINE"

    return json.dumps(
        {
            "overall_status": overall,
            "alarm_count": len(alarms_summary),
            "alarms": alarms_summary,
            "nodes": nodes_summary,
        },
        indent=2,
    )


@mcp.tool()
async def get_router_info(router: str) -> str:
    """Get static facts about a router: its nodes, software version, and
    application identification capability.

    Use this at the start of any session involving a specific router, and
    always before using application identification tools. The node names
    returned here are required by most other router-targeted tools.

    Returns:
      nodes       — list of nodes with name, role, and software version
      app_id      — whether app-id is enabled and which tool families are
                    valid (has_module, has_http_https)

    app_id tool families:
      has_module     — get_app_id_modules, get_application_names,
                       app_id_lookup (address mode)
      has_http_https — get_app_id_cache, get_application_series,
                       get_web_filtering_state, get_app_id_categories,
                       app_id_lookup (address and domain modes)

    Args:
        router: Router name (required).
    """
    client = get_client()

    state_data, app_id_config = await asyncio.gather(
        client.get_system_state(router),
        client.get_app_id_config(router),
    )

    raw_nodes = (
        state_data
        .get("data", {})
        .get("allRouters", {})
        .get("nodes", [{}])[0]
        .get("nodes", {})
        .get("nodes", [])
    )
    nodes = [
        {
            "name": n.get("name"),
            "role": n.get("state", {}).get("role"),
            "software_version": n.get("state", {}).get("softwareVersion"),
        }
        for n in raw_nodes
    ]

    modes = (app_id_config or {}).get("mode") or []
    if app_id_config is None:
        app_id: dict = {"enabled": False, "reason": "not configured (404)"}
    elif not modes:
        app_id = {"enabled": False, "reason": "no modes active"}
    else:
        has_all = "all" in modes
        app_id = {
            "enabled": True,
            "modes": modes,
            "has_module": has_all or "module" in modes,
            "has_http_https": has_all or "http" in modes or "https" in modes,
        }

    return json.dumps({"nodes": nodes, "app_id": app_id}, indent=2)


# ------------------------------------------------------------------
# Interfaces
# ------------------------------------------------------------------


@mcp.tool()
async def get_device_interfaces(
    router: str,
    node: str | None = None,
    device_interface: str | None = None,
    window_minutes: int = 5,
) -> str:
    """Get physical (device) interface state and traffic metrics. Includes
    admin/operational/redundancy status, MAC, speed, duplex, description,
    link settings, average bandwidth over the requested window, the network
    interfaces configured on each device interface, and per-interface
    byte/packet/error counters for both directions.

    Args:
        router:           Router name (required).
        node:             (optional) Limit to a specific node.
        device_interface: (optional) Limit to a specific interface by name.
        window_minutes:   Time window for the averageBandwidth analytic.
                          Default 5 minutes.
    """
    data = await get_client().get_device_interfaces(router, node, device_interface, window_minutes)
    return json.dumps(data, indent=2)


@mcp.tool()
async def get_network_interface_applications(
    router: str | None = None,
    node: str | None = None,
    network_interface: str | None = None,
) -> str:
    """Get raw application plugin state for network interfaces. Returns the
    full plugin state blob including Kea DHCP service status, metrics, and
    lease data.

    Prefer get_dhcp_leases for questions about clients or leases — it returns
    the same data in a clean, flat structure with all metrics noise removed.
    Use this tool only when you need the raw Kea service status or metrics.

    Args:
        router:            (optional) Limit to a specific router.
        node:              (optional) Limit to a specific node.
        network_interface: (optional) Limit to a specific interface by name.
    """
    data = await get_client().get_network_interface_applications(router, node, network_interface)
    return json.dumps(data, indent=2)


def _parse_node_utilization(data: dict) -> list[dict]:
    result = []
    for router_node in data.get("data", {}).get("allRouters", {}).get("nodes", []):
        for n in router_node.get("nodes", {}).get("nodes", []):
            # Deduplicate by core index, preferring packetProcessing over machine.
            # Rename API type "machine" -> "general".
            cores_by_index: dict = {}
            for c in n.get("cpu", []):
                idx = c.get("core")
                if idx not in cores_by_index or c.get("type") == "packetProcessing":
                    cores_by_index[idx] = c
            cpu_cores = [
                {**c, "type": "general" if c.get("type") == "machine" else c.get("type")}
                for c in cores_by_index.values()
            ]
            high_cpu = [c for c in cpu_cores if (c.get("utilization") or 0) >= 90]

            mem = n.get("memory") or {}
            mem_cap = mem.get("capacity") or 0
            mem_use = mem.get("usage") or 0
            mem_pct = round(mem_use / mem_cap * 100, 1) if mem_cap else 0

            disks = []
            high_disk = []
            for d in n.get("disk") or []:
                cap = d.get("capacity") or 0
                use = d.get("usage") or 0
                pct = round(use / cap * 100, 1) if cap else 0
                entry = {
                    "partition": d.get("partition"),
                    "capacity_gb": round(cap / 1e9, 1),
                    "usage_gb": round(use / 1e9, 1),
                    "usage_pct": pct,
                }
                disks.append(entry)
                if pct >= 85:
                    high_disk.append(entry)

            result.append({
                "name": n.get("name"),
                "cpu_cores": cpu_cores,
                "cpu_high": high_cpu,
                "memory_capacity_gb": round(mem_cap / 1e9, 1),
                "memory_usage_gb": round(mem_use / 1e9, 1),
                "memory_usage_pct": mem_pct,
                "disk": disks,
                "disk_high": high_disk,
            })
    return result


def _extract_dhcp_leases(data: dict) -> list[dict]:
    leases_by_interface = []
    for router_node in data.get("data", {}).get("allRouters", {}).get("nodes", []):
        for node in router_node.get("nodes", {}).get("nodes", []):
            for dev_iface in node.get("deviceInterfaces", {}).get("nodes", []):
                for net_iface in dev_iface.get("networkInterfaces", {}).get("nodes", []):
                    plugin_state = net_iface.get("plugins", {}).get("state", {})
                    dhcp_server = plugin_state.get("dhcp-server")
                    if not dhcp_server:
                        continue

                    subnets = next(
                        (item["subnets"] for item in dhcp_server if "subnets" in item),
                        [],
                    )
                    for subnet_wrapper in subnets:
                        subnet_data = subnet_wrapper.get("subnet", {})
                        leases = [
                            {
                                "ip_address": item["lease"]["ip-address"],
                                "hostname": item["lease"].get("hostname"),
                                "hw_address": item["lease"].get("hw-address"),
                                "last_seen": item["lease"].get("client-last-transaction-time"),
                                "valid_lifetime_seconds": item["lease"].get("valid-lifetime"),
                            }
                            for item in subnet_data.get("current-leases", [])
                        ]
                        leases_by_interface.append({
                            "interface": net_iface.get("name"),
                            "subnet": subnet_data.get("subnet"),
                            "lease_count": subnet_data.get("current-lease-count", len(leases)),
                            "leases": leases,
                        })
    return leases_by_interface


@mcp.tool()
async def get_dhcp_leases(
    router: str | None = None,
    node: str | None = None,
    network_interface: str | None = None,
) -> str:
    """Get active DHCP leases across all DHCP-server interfaces on a router.
    Returns a flat, clean list of leases — IP address, hostname, MAC address,
    last-seen time, and lease lifetime — grouped by interface and subnet.

    Use this to answer questions like "what devices are on the network?",
    "what IP did host X get?", or "how many clients are connected to home_lan?".

    Args:
        router:            (optional) Limit to a specific router.
        node:              (optional) Limit to a specific node.
        network_interface: (optional) Limit to a single interface by name,
                           e.g. 'home_lan'. Strongly recommended when you
                           know which interface to inspect — reduces response
                           size significantly.
    """
    data = await get_client().get_network_interface_applications(router, node, network_interface)
    interfaces = _extract_dhcp_leases(data)
    total = sum(i["lease_count"] for i in interfaces)
    return json.dumps({"total_leases": total, "interfaces": interfaces}, indent=2)


@mcp.tool()
async def get_network_interfaces(
    router: str | None = None,
    node: str | None = None,
    network_interface: str | None = None,
) -> str:
    """Get network interface (VLAN) configuration and state, including
    configured and DHCP-resolved IP addresses, gateway, prefix length,
    and the operational status of the underlying device interface.

    Args:
        router:            (optional) Limit to a specific router.
        node:              (optional) Limit to a specific node.
        network_interface: (optional) Limit to a specific interface by name.
    """
    data = await get_client().get_network_interfaces(router, node, network_interface)
    return json.dumps(data, indent=2)


# ------------------------------------------------------------------
# Routing / services
# ------------------------------------------------------------------


@mcp.tool()
async def get_sessions(
    router: str,
    node: str | None = None,
    limit: int = 200,
    filter: str | None = None,
    summarize: bool = False,
) -> str:
    """Get active forwarding sessions on a router node.

    Use summarize=True to get session counts grouped by (service, tenant,
    protocol) with an encrypted/unencrypted breakdown. This is useful for
    questions like "how many sessions does this tenant have per service?" or
    "how much traffic is encrypted?" Note: summarize returns session counts
    only — for bandwidth use list_services (per-service) or get_top_sources
    (per-client).

    Use summarize=False (default) only when you need individual flow detail —
    specific IPs, ports, or UUIDs. Always pair with a filter or a small limit
    in that case.

    Args:
        router:    Router name (required).
        node:      (optional) Node name — omit to return sessions across all nodes.
        limit:     Max sessions to fetch. Default 200. Pass 0 for no limit —
                   only do this with a narrow filter on a quiet router.
        filter:    Optional filter expression.
                   Syntax: "<field>"="<value>"  (exact match)
                           "<field>"~"<value>"  (contains)
                           ~"<value>"           (wildcard — searches all fields)

                   Filterable fields:
                     source_ip, dest_ip, source_port, dest_port,
                     vlan, device_port, protocol (IP protocol number),
                     session_uuid, nat_ip, nat_port,
                     service_name, tenant, encrypted ("true"/"false"),
                     device_interface_name, network_interface_name,
                     inactivity_timeout

                   Note: start_time and forward are not filterable.

                   Examples:
                     '"service_name"="internet"'
                     '"source_ip"~"10.0."'
                     '"dest_port"="443"'
                     '"encrypted"="true"'
                     '~"my-tenant"'
        summarize: When True, aggregate by (service, tenant, protocol) and
                   return counts with encrypted/unencrypted breakdown instead
                   of raw flows. Default False.
    """
    actual_limit = limit if limit > 0 else None
    sessions = await get_client().get_sessions(router, node, actual_limit, filter)

    if not summarize:
        return json.dumps({"count": len(sessions), "sessions": sessions}, indent=2)

    groups: dict[tuple, dict] = {}
    for s in sessions:
        key = (s.get("serviceName") or "unknown", s.get("tenant") or "unknown", s.get("protocol") or "unknown")
        if key not in groups:
            groups[key] = {"service": key[0], "tenant": key[1], "protocol": key[2], "total": 0, "encrypted": 0}
        groups[key]["total"] += 1
        if s.get("encrypted"):
            groups[key]["encrypted"] += 1

    summary = sorted(groups.values(), key=lambda x: x["total"], reverse=True)
    for row in summary:
        row["unencrypted"] = row["total"] - row["encrypted"]

    return json.dumps(
        {"sessions_sampled": len(sessions), "limit_applied": actual_limit, "breakdown": summary},
        indent=2,
    )


@mcp.tool()
async def find_sessions(
    filter: str | None = None,
    limit_per_router: int = 250,
) -> str:
    """Search for sessions across all routers simultaneously. Each result
    includes _router and _node fields so you can see which router each
    flow leg is on.

    Use this instead of get_sessions when:
    - You have a session UUID and want to find all its legs across the network
      (inter-router sessions appear on multiple routers)
    - You don't know which router a session is on
    - You want to see whether a flow is being handled end-to-end across
      multiple hops

    Context: conductor only — requires visibility into all managed routers.

    Args:
        filter:           Filter expression — same syntax as get_sessions.
                          Searching by session UUID is the most common use:
                            '~"<uuid>"'  or  '"sessionUuid"="<uuid>"'
        limit_per_router: Max sessions to fetch per router. Default 250.
                          Raise this if you suspect results are being truncated
                          on a busy network.

    Note: routers that are offline or unreachable return a connectivity error
    rather than an empty session list. These are reported in unreachable_routers
    so you know which routers could not be searched.
    """
    result = await get_client().find_sessions(filter, limit_per_router)
    sessions = result["sessions"]
    unreachable = result["unreachable"]
    return json.dumps(
        {
            "count": len(sessions),
            "sessions": sessions,
            "unreachable_count": len(unreachable),
            "unreachable_routers": unreachable,
        },
        indent=2,
    )


@mcp.tool()
async def get_app_id_modules(router: str, node: str) -> str:
    """List the application identification modules registered and running
    on a router node.

    Requires: app-id with 'module' mode — check app_id.has_module in get_router_info.

    Args:
        router: Router name (required).
        node:   Node name (required).
    """
    data = await get_client().get_app_id_modules(router, node)
    return json.dumps(data, indent=2)


@mcp.tool()
async def get_application_names(
    router: str,
    node: str,
    limit: int | None = None,
) -> str:
    """List named applications with their active session count and number of
    IP tuples resolved for each application name.

    Requires: app-id with 'module' mode — check app_id.has_module in get_router_info.

    Args:
        router: Router name (required).
        node:   Node name (required).
        limit:  Max entries to return. Omit to return all entries.
    """
    entries = await get_client().get_application_names(router, node, limit)
    return json.dumps({"count": len(entries), "applications": entries}, indent=2)


@mcp.tool()
async def get_web_filtering_state(router: str, node: str) -> str:
    """Get the web filtering enabled/disabled state for a router node.

    Requires: app-id with 'http' or 'https' mode — check app_id.has_http_https in get_router_info.

    Args:
        router: Router name (required).
        node:   Node name (required).
    """
    data = await get_client().get_web_filtering_state(router, node)
    return json.dumps(data, indent=2)


@mcp.tool()
async def get_app_id_categories(router: str, node: str) -> str:
    """List the application categories known to the SSR for web filtering.

    Requires: app-id with 'http' or 'https' mode — check app_id.has_http_https in get_router_info.

    Args:
        router: Router name (required).
        node:   Node name (required).
    """
    data = await get_client().get_app_id_categories(router, node)
    return json.dumps(data, indent=2)


@mcp.tool()
async def app_id_lookup(
    router: str,
    node: str,
    mode: str = "address",
    ip: str | None = None,
    port: int | None = None,
    protocol: str | None = None,
    domain: str | None = None,
) -> str:
    """Look up the application classification for a destination. Returns the
    application, category, and domain/URL that app-id would resolve.

    Requires: app-id enabled — check app_id.enabled in get_router_info.
    Domain mode additionally requires has_http_https.

    Two modes:
      'address' — looks up by destination IP, port, and protocol.
                  Requires: ip, port, protocol.
                  Example: ip='1.1.1.1', port=53, protocol='udp'

      'domain'  — looks up by domain name or URL.
                  Requires: domain.
                  Requires http or https app-id mode.
                  Examples: domain='www.youtube.com'
                            domain='http://192.168.1.5/index.html'

    Cache miss behaviour: if the destination has not been seen before, the
    lookup will return no result but will trigger the app-id engine to
    classify it. Call this tool a second time for the same destination and
    the result should be populated from the newly created cache entry.

    Args:
        router:   Router name (required).
        node:     Node name (required).
        mode:     'address' (default) or 'domain'.
        ip:       Destination IP — required for address mode.
        port:     Destination port — required for address mode.
        protocol: Protocol ('udp', 'tcp') — required for address mode.
        domain:   Domain name or URL — required for domain mode.
    """
    data = await get_client().app_id_lookup(router, node, mode, ip, port, protocol, domain)
    return json.dumps(data, indent=2)


@mcp.tool()
async def fib_lookup(
    router: str,
    node: str,
    dest_ip: str,
    dest_port: int,
    protocol: str,
    tenant: str,
) -> str:
    """Look up the FIB entry that would be matched for a specific packet,
    given its destination IP, port, protocol, and source tenant. Returns
    the service and next-hop the dataplane would select for that traffic.

    Args:
        router:    Router name (required).
        node:      Node name (required).
        dest_ip:   Destination IP address, e.g. '1.1.1.1'.
        dest_port: Destination L4 port, e.g. 53.
        protocol:  IP protocol, e.g. 'udp' or 'tcp'.
        tenant:    Source tenant name, e.g. 'lan.corp'.
    """
    data = await get_client().fib_lookup(router, node, dest_ip, dest_port, protocol, tenant)
    return json.dumps(data, indent=2)


@mcp.tool()
async def get_fib(
    router: str,
    node: str,
    limit: int | None = None,
    vrf: str | None = None,
    ip_prefix: str | None = None,
) -> str:
    """Get the Forwarding Information Base (FIB) for a node — the resolved
    set of prefixes and next-hops the dataplane will actually use.

    WARNING: Unfiltered calls may return thousands of entries. Use ip_prefix
    or vrf to narrow results, or pass a limit.

    Args:
        router:    Router name (required).
        node:      Node name within that router (required).
        limit:     Max entries to return. Omit to return all entries.
        vrf:       (optional) Filter by VRF name.
        ip_prefix: (optional) Filter by IP prefix, e.g. '10.0.0.0/8'.
    """
    entries = await get_client().get_fib(router, node, limit, vrf, ip_prefix)
    return json.dumps({"count": len(entries), "fib": entries}, indent=2)


@mcp.tool()
async def list_service_paths(
    router: str,
    node: str | None = None,
    limit: int | None = None,
    filter: str | None = None,
) -> str:
    """List service paths showing per-path state, SLA compliance, capacity,
    cost, and vector for each service route on a node.

    Args:
        router: Router name (required).
        node:   Node name within that router (required).
        limit:  Max total paths to return. Omit to return all paths.
        filter: Optional filter expression using the same syntax as
                get_sessions, e.g. '"service_name"="internet"'.
    """
    paths = await get_client().get_service_paths(router, node, limit, filter)
    return json.dumps({"count": len(paths), "service_paths": paths}, indent=2)


@mcp.tool()
async def get_software_version() -> str:
    """Get the software version of the device SSR_HOST points to — either
    the conductor or the standalone router. Does not accept a router argument;
    to check a specific managed router's version use get_system_state instead.

    Context: any — always returns the version of the connected device.
    """
    data = await get_client().get_software_version()
    return json.dumps(data, indent=2)


@mcp.tool()
async def get_session_processor_utilization(router: str, node: str) -> str:
    """Get CPU utilization of the service area (session processor) threads.

    Args:
        router: Router name (required).
        node:   Node name (required).
    """
    data = await get_client().get_session_processor_utilization(router, node)
    return json.dumps(data, indent=2)


@mcp.tool()
async def get_resource_allocation(router: str, node: str) -> str:
    """Get forwarding core and hugepage memory allocation for a node.

    Args:
        router: Router name (required).
        node:   Node name (required).
    """
    data = await get_client().get_resource_allocation(router, node)
    return json.dumps(data, indent=2)


@mcp.tool()
async def get_vrfs(router: str, limit: int | None = None) -> str:
    """Get VRFs configured on a router.

    Args:
        router: Router name (required).
        limit:  Max entries to return. Omit to return all VRFs.
    """
    entries = await get_client().get_vrfs(router, limit)
    return json.dumps({"count": len(entries), "vrfs": entries}, indent=2)


@mcp.tool()
async def get_top_sources(
    router: str | None = None,
    node: str | None = None,
    limit: int = 10,
    order_by: str = "TOTAL_DATA",
) -> str:
    """Get the most active client source IPs on a router, ranked by a traffic
    metric. Shows tenant, IP, current bandwidth, total data, and session count.

    Use this to answer "who is using the most bandwidth?" — it identifies
    specific clients rather than services or applications. For what services
    are carrying traffic use list_services; for which applications are in use
    use get_application_series (requires app-id http/https mode).

    Args:
        router:   (optional) Limit to a specific router.
        node:     (optional) Limit to a specific node.
        limit:    Number of top sources to return. Default 10.
        order_by: Metric to rank by. Known values: 'TOTAL_DATA',
                  'CURRENT_BANDWIDTH', 'SESSION_COUNT'. Default 'TOTAL_DATA'.
    """
    data = await get_client().get_top_sources(router, node, limit, order_by)
    return json.dumps(data, indent=2)


@mcp.tool()
async def get_rib(
    router: str,
    vrf: str | None = None,
    ip: str | None = None,
    filter: str | None = None,
    sub_command: str | None = None,
    limit: int | None = None,
) -> str:
    """Get all routes in the Routing Information Base (RIB) for a router.
    Includes next-hops, protocol, distance, metric, uptime, and FIB selection.

    WARNING: Unfiltered calls may return thousands of routes. Use ip or vrf
    to narrow results.

    Args:
        router:      Router name (required).
        vrf:         (optional) Filter by VRF name.
        ip:          (optional) Filter by IP prefix, e.g. '10.0.0.0/8'.
        filter:      (optional) Additional filter string.
        sub_command: (optional) RibSubCommand enum value.
        limit:       Max entries to return. Omit to return all entries.
    """
    entries = await get_client().get_rib(router, vrf, ip, filter, sub_command, limit)
    return json.dumps({"count": len(entries), "rib": entries}, indent=2)


@mcp.tool()
async def get_events(
    router: str,
    from_time: str | None = None,
    to_time: str | None = None,
    event_types: list[str] | None = None,
    subtype: str | None = None,
    limit: int | None = None,
) -> str:
    """Get the event (audit log) history for a router.

    WARNING: Without a from_time filter this queries from the beginning of
    time and may return enormous amounts of data. Always use from_time to
    bound the query to a relevant time window.

    Args:
        router:      Router name (required).
        from_time:   Start of time range in ISO 8601, e.g. '2026-05-01T00:00:00Z'.
        to_time:     End of time range in ISO 8601. Defaults to now.
        event_types: (optional) List of AuditLogType values to filter by.
        subtype:     (optional) Event subtype to filter by.
        limit:       Max events to return. Omit to return all events in range.
    """
    events = await get_client().get_events(router, from_time, to_time, event_types, subtype, limit)
    return json.dumps({"count": len(events), "events": events}, indent=2)


@mcp.tool()
async def get_assets(asset_ids: list[str] | None = None) -> str:
    """Get asset status for onboarding and upgrades.

    Context: conductor only — not available on standalone routers.

    Shows each asset's SSR version, onboarding status, installation type,
    platform, and any error details.

    Args:
        asset_ids: (optional) List of specific asset IDs to filter.
                   Omit to return all assets.
    """
    data = await get_client().get_assets(asset_ids)
    return json.dumps(data, indent=2)


@mcp.tool()
async def get_system_processes(
    router: str | None = None,
    node: str | None = None,
) -> str:
    """Get the status of all major SSR processes on a node, including whether
    HA-capable processes are active or standby (leaderStatus).

    Context: any — omit router to query the connected device itself.

    Args:
        router: (optional) Limit to a specific router.
        node:   (optional) Limit to a specific node within that router.
    """
    data = await get_client().get_system_processes(router, node)
    return json.dumps(data, indent=2)


@mcp.tool()
async def get_app_id_cache(
    router: str,
    node: str | None = None,
    cache: str = "address",
    limit: int = 500,
    summarize: bool = False,
) -> str:
    """Get the application identification cache — destination IPs, ports, and
    protocols that have been classified by app-id based on traffic through the
    router, along with the resolved application, category, and domain/URL.

    Requires: app-id with 'http' or 'https' mode — check app_id.has_http_https in get_router_info.

    WARNING: On an active router the cache can contain tens of thousands of
    entries. Use summarize=True for broad questions, or keep the default limit
    and pair summarize=False with a specific known application. Only pass
    limit=0 (no limit) when you explicitly need the full raw cache.

    Use summarize=True (the default for broad questions like "what apps are in
    use?") to get a ranked count by application and category instead of raw
    entries. Use summarize=False only when you need specific IPs, ports, or
    domains for a known application.

    Args:
        router:    Router name (required).
        node:      (optional) Limit to a specific node.
        cache:     Cache type. Known values: 'address', 'domain', 'url'.
                   Default 'address'.
        limit:     Max entries to fetch before summarising or returning.
                   Default 500. Pass a larger value or 0 for no limit only
                   when you need comprehensive raw data.
        summarize: When True, return per-application counts sorted by
                   frequency instead of raw entries. Default False.
    """
    actual_limit = limit if limit > 0 else None
    entries = await get_client().get_app_id_cache(router, node, cache, actual_limit)

    if not summarize:
        return json.dumps({"count": len(entries), "app_id_cache": entries}, indent=2)

    counts: dict[tuple, int] = {}
    for e in entries:
        key = (e.get("application") or "unknown", e.get("category") or "unknown", e.get("subCategory") or "")
        counts[key] = counts.get(key, 0) + 1

    applications = sorted(
        [
            {"application": app, "category": cat, "sub_category": sub, "count": n}
            for (app, cat, sub), n in counts.items()
        ],
        key=lambda x: x["count"],
        reverse=True,
    )

    return json.dumps(
        {
            "entries_sampled": len(entries),
            "application_count": len(applications),
            "applications": applications,
        },
        indent=2,
    )


@mcp.tool()
async def get_arp(
    router: str,
    node: str,
    limit: int | None = None,
) -> str:
    """Get the ARP table for a node.

    Args:
        router: Router name (required).
        node:   Node name (required).
        limit:  Max entries to return. Omit to return all entries.
    """
    entries = await get_client().get_arp(router, node, limit)
    return json.dumps({"count": len(entries), "arp": entries}, indent=2)


@mcp.tool()
async def get_platform(
    router: str | None = None,
    node: str | None = None,
) -> str:
    """Get hardware platform information for nodes — CPU, memory, NICs, disks,
    OS, and vendor/product details.

    Context: any — omit router to query the connected device itself.

    Args:
        router: (optional) Limit to a specific router.
        node:   (optional) Limit to a specific node within that router.
    """
    data = await get_client().get_platform(router, node)
    return json.dumps(data, indent=2)


@mcp.tool()
async def get_system_services(
    router: str | None = None,
    node: str | None = None,
) -> str:
    """Get the status of systemd services used alongside the SSR application.

    Context: any — omit router to query the connected device itself.

    Args:
        router: (optional) Limit to a specific router.
        node:   (optional) Limit to a specific node within that router.
    """
    data = await get_client().get_system_services(router, node)
    return json.dumps(data, indent=2)


@mcp.tool()
async def get_system_connectivity(
    router: str | None = None,
    node: str | None = None,
) -> str:
    """Get management connectivity status between nodes and the conductor,
    or between nodes in an HA router pair.

    Context: any — omit router to query the connected device itself.

    Args:
        router: (optional) Limit to a specific router.
        node:   (optional) Limit to a specific node within that router.
    """
    data = await get_client().get_system_connectivity(router, node)
    return json.dumps(data, indent=2)


@mcp.tool()
async def get_system_state(
    router: str | None = None,
    node: str | None = None,
) -> str:
    """Get basic system state for routers/nodes — status, uptime, role,
    software version, and active alarm count.

    Context: any — omit router to query the connected device itself.
             On a conductor, omitting router returns state for all managed
             routers.

    Args:
        router: (optional) Limit to a specific router.
        node:   (optional) Limit to a specific node within that router.
    """
    data = await get_client().get_system_state(router, node)
    return json.dumps(data, indent=2)


@mcp.tool()
async def get_node_utilization(
    router: str,
    node: str | None = None,
) -> str:
    """Get live CPU, memory, and disk utilization for router nodes.

    CPU cores are classified as 'packetProcessing' (fastpath forwarding,
    isolated from the OS) or 'general' (OS/application cores).
    High-utilization cores (>= 90%) are surfaced separately so they're
    easy to spot without scanning every core.

    Args:
        router: Router name (required).
        node:   (optional) Limit to a specific node.
    """
    data = await get_client().get_node_utilization(router, node)
    nodes = _parse_node_utilization(data)
    return json.dumps({"nodes": nodes}, indent=2)


@mcp.tool()
async def get_capacity(
    router: str | None = None,
    node: str | None = None,
) -> str:
    """Get network resource capacity for routers/nodes — shows current usage
    count and limit for each resource type (sessions, flows, etc.).

    Context: any — omit router to query the connected device itself.

    Args:
        router: (optional) Limit to a specific router.
        node:   (optional) Limit to a specific node within that router.
    """
    data = await get_client().get_capacity(router, node)
    return json.dumps(data, indent=2)


@mcp.tool()
async def list_tenant_members(
    router: str,
    node: str,
    limit: int | None = None,
) -> str:
    """List tenant membership entries for a node — shows which source IP
    prefixes, VLANs, and interfaces are assigned to each tenant.

    Args:
        router: Router name (required).
        node:   Node name (required).
        limit:  Max entries to return. Omit to return all entries.
    """
    entries = await get_client().get_tenant_members(router, node, limit)
    return json.dumps({"count": len(entries), "tenant_members": entries}, indent=2)


@mcp.tool()
async def list_services(
    router: str,
    node: str | None = None,
    filter: str | None = None,
) -> str:
    """List configured services with live traffic metrics (session count,
    bandwidth in/out) and service configuration (prefixes, transport, policy,
    service routes, access lists).

    Use this as the first stop for any bandwidth or traffic volume question.
    Every session on the router matches a service, so this gives a complete
    picture of what is carrying traffic without requiring app-id. It will not
    break out individual applications within a broad service like 'internet' —
    use get_application_series for that (requires app-id with http/https mode).

    Args:
        router: Router name (required).
        node:   (optional) Limit to a specific node within that router.
        filter: (optional) Filter expression using the same syntax as
                get_sessions. Known filterable field: service_name.
                Examples:
                  '"service_name"="internet"'   - exact match
                  '"service_name"~"lane"'        - contains
    """
    data = await get_client().get_services(router, node, filter)
    return json.dumps(data, indent=2)


@mcp.tool()
async def get_session(
    session_id: str,
    router: str,
    node: str | None = None,
) -> str:
    """Get detailed information about a specific session by its UUID.
    Includes full flow detail (forward, reverse, detached), byte/packet counts,
    TCP state, path attributes, app identification, and security policy info.

    Typical workflow: call get_sessions to find sessions of interest, then pass
    a sessionUuid from those results to this tool for deeper inspection.

    Args:
        session_id: Session UUID (from sessionUuid in get_sessions results).
        router:     Router name (required).
        node:       (optional) Node name — speeds up lookup if known.
    """
    data = await get_client().get_session(session_id, router, node)
    return json.dumps(data, indent=2)


@mcp.tool()
async def list_peer_paths(
    router: str | None = None,
    peer_name: str | None = None,
    detail: bool = False,
) -> str:
    """List SSR peer paths (waypoint adjacencies) showing per-path status,
    latency, jitter, loss, MOS, and uptime. Uses the GraphQL API.

    Args:
        router:    (optional) Limit to a specific router by name.
        peer_name: (optional) Limit to a specific peer by name.
        detail:    Include BFD intervals, key exchange, and crypto fields.
    """
    data = await get_client().get_peer_paths(router, peer_name, detail)
    return json.dumps(data, indent=2)


# ------------------------------------------------------------------
# Dropped packets
# ------------------------------------------------------------------


_PROTOCOL_NUMBERS = {
    "icmp": 1, "tcp": 6, "udp": 17, "gre": 47, "esp": 50, "sctp": 132,
}

_DROP_REASONS = {"UNKNOWN", "ACCESS", "CONFIGURATION", "ROUTE_PROVISION", "ERROR"}


def _build_drop_filter(
    source_ip: str | None,
    dest_ip: str | None,
    source_port: int | None,
    dest_port: int | None,
    protocol: str | None,
    reason: str | None,
) -> dict | None:
    rules = []
    if source_ip:
        rules.append({"field": "SourceIP", "operator": "==", "value": source_ip})
    if dest_ip:
        rules.append({"field": "DestinationIP", "operator": "==", "value": dest_ip})
    if source_port is not None:
        rules.append({"field": "SourcePort", "operator": "==", "value": source_port})
    if dest_port is not None:
        rules.append({"field": "DestinationPort", "operator": "==", "value": dest_port})
    if protocol is not None:
        proto_lower = str(protocol).lower()
        proto_num = _PROTOCOL_NUMBERS.get(proto_lower)
        if proto_num is None and str(protocol).isdigit():
            proto_num = int(protocol)
        if proto_num is not None:
            rules.append({"field": "Protocol", "operator": "==", "value": proto_num})
    if reason is not None:
        rules.append({"field": "Reason", "operator": "==", "value": reason.upper()})
    if not rules:
        return None
    return {"version": "1.0", "rule": {"conjunction": "AND", "rules": rules}}


@mcp.tool()
async def get_dropped_packets(
    router: str,
    node: str,
    duration: float = 10.0,
    source_ip: str | None = None,
    dest_ip: str | None = None,
    source_port: int | None = None,
    dest_port: int | None = None,
    protocol: str | None = None,
    reason: str | None = None,
    raw: bool = False,
) -> str:
    """Sample the dropped-packets stream for a node — packets that hit the
    service area but could not establish a session. Each drop consumes CPU
    regardless of outcome, so identifying and fixing drop patterns frees
    service area capacity for legitimate traffic.

    Returns a pattern summary: drops by reason, by ingress interface, top
    source IPs, top destination IP:port pairs, and protocol breakdown. This
    is usually enough to identify the root cause and the configuration change
    needed to resolve it.

    Filters are applied server-side before events are streamed, so use them
    to narrow a noisy stream to specific traffic of interest.

    Common drop reasons:
      ACCESS          — no matching service or tenant access denied
      CONFIGURATION   — misconfigured service or route
      ROUTE_PROVISION — route not yet provisioned
      ERROR           — dataplane error during session setup

    Args:
        router:      Router name (required).
        node:        Node name (required).
        duration:    How many seconds to collect drops. Default 10.
                     Increase to 30+ on quiet routers to catch infrequent drops.
        source_ip:   Filter to drops from this source IP.
        dest_ip:     Filter to drops toward this destination IP.
        source_port: Filter to drops from this source port.
        dest_port:   Filter to drops toward this destination port.
        protocol:    Filter by protocol — common names (tcp, udp, icmp, gre,
                     esp, sctp) or IANA protocol number.
        reason:      Filter by drop reason: UNKNOWN, ACCESS, CONFIGURATION,
                     ROUTE_PROVISION, or ERROR.
        raw:         Include the full list of individual drop events. Default
                     False — the pattern summary is sufficient for most
                     investigations.
    """
    filter_body = _build_drop_filter(source_ip, dest_ip, source_port, dest_port, protocol, reason)
    result = await get_client().get_dropped_packets(router, node, duration, filter_body)
    events = result["events"]

    by_reason: dict[str, int] = {}
    by_interface: dict[str, int] = {}
    by_source_ip: dict[str, int] = {}
    by_dest: dict[str, int] = {}
    by_protocol: dict[str, int] = {}

    for event in events:
        by_reason[event.get("reason", "UNKNOWN")] = by_reason.get(event.get("reason", "UNKNOWN"), 0) + 1

        iface = event.get("ingressInterface", "unknown")
        by_interface[iface] = by_interface.get(iface, 0) + 1

        src = event.get("sourceIp") or event.get("sourceAddress") or "unknown"
        by_source_ip[src] = by_source_ip.get(src, 0) + 1

        dst_ip = event.get("destIp") or event.get("destinationIp") or "unknown"
        dst_port = event.get("destPort") or event.get("destinationPort")
        dest_key = f"{dst_ip}:{dst_port}" if dst_port else dst_ip
        by_dest[dest_key] = by_dest.get(dest_key, 0) + 1

        proto = str(event.get("protocol", "unknown"))
        by_protocol[proto] = by_protocol.get(proto, 0) + 1

    def top(d: dict, n: int = 10) -> dict:
        return dict(sorted(d.items(), key=lambda x: x[1], reverse=True)[:n])

    output: dict = {
        "duration_seconds": duration,
        "total_dropped": len(events),
        "skipped_count": result["skipped_count"],
        "by_reason": by_reason,
        "by_ingress_interface": by_interface,
        "top_source_ips": top(by_source_ip),
        "top_destinations": top(by_dest),
        "by_protocol": by_protocol,
    }
    if filter_body:
        output["filter_applied"] = filter_body["rule"]["rules"]
    if raw:
        output["events"] = events
    return json.dumps(output, indent=2)


# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------


@mcp.tool()
async def get_running_config(router: str | None = None) -> str:
    """Fetch the running configuration.

    WARNING: Without a router filter this returns the full authority
    configuration, which can be very large on conductors managing many
    routers. Pass router to limit the response to a single router's config
    subtree whenever you only need one router's configuration.

    Context: any — without router, returns the full authority config (conductor)
             or the local device config (standalone router). With router,
             returns that router's config subtree regardless of connection mode.

    Args:
        router: (optional) Limit to this router's config subtree.
    """
    config = await get_client().get_running_config(router)
    return json.dumps(config, indent=2)


# ------------------------------------------------------------------
# Application series
# ------------------------------------------------------------------


def _summarize_app_series(buckets: list, application: str | None = None) -> list:
    """Aggregate application series buckets into a per-application summary.

    Byte totals use the peak value seen per client across buckets to avoid
    double-counting the same session's bytes in multiple time windows.
    """
    apps: dict[str, dict] = {}

    for bucket in buckets:
        for entry in bucket.get("value", []):
            name = entry.get("name", "unknown")
            if application and application.lower() not in name.lower():
                continue

            if name not in apps:
                apps[name] = {
                    "name": name,
                    "type": entry.get("type"),
                    "category": entry.get("category"),
                    "client_stats": {},  # address -> {rx, tx}
                    "tenants": set(),
                    "services": set(),
                    "next_hop_types": set(),
                }

            app = apps[name]

            for client in entry.get("clients", []):
                addr = client.get("address", "unknown")
                tenant = client.get("tenant")
                if tenant:
                    app["tenants"].add(tenant)
                for svc in client.get("services") or []:
                    app["services"].add(svc if isinstance(svc, str) else svc.get("name", str(svc)))

                # Bytes are on nextHopInterface entries, not the client entry itself
                nh_rx = sum((nh.get("rxBytes") or 0) for nh in client.get("nextHopInterface") or [])
                nh_tx = sum((nh.get("txBytes") or 0) for nh in client.get("nextHopInterface") or [])

                active = client.get("activeSessions") or 0

                if addr not in app["client_stats"]:
                    app["client_stats"][addr] = {"rx": nh_rx, "tx": nh_tx, "sessions": active}
                else:
                    app["client_stats"][addr]["rx"] = max(app["client_stats"][addr]["rx"], nh_rx)
                    app["client_stats"][addr]["tx"] = max(app["client_stats"][addr]["tx"], nh_tx)
                    app["client_stats"][addr]["sessions"] = max(app["client_stats"][addr]["sessions"], active)

                for nh in client.get("nextHopInterface") or []:
                    nh_type = nh.get("type")
                    if nh_type:
                        app["next_hop_types"].add(nh_type)

    result = []
    for app in apps.values():
        total_rx = sum(c["rx"] for c in app["client_stats"].values())
        total_tx = sum(c["tx"] for c in app["client_stats"].values())
        total_sessions = sum(c["sessions"] for c in app["client_stats"].values())
        result.append({
            "name": app["name"],
            "type": app["type"],
            "category": app["category"],
            "active_sessions": total_sessions,
            "unique_clients": len(app["client_stats"]),
            "clients": sorted(app["client_stats"].keys()),
            "tenants": sorted(app["tenants"]),
            "services": sorted(app["services"]),
            "next_hop_types": sorted(app["next_hop_types"]),
            "rx_bytes": total_rx,
            "tx_bytes": total_tx,
        })

    return sorted(result, key=lambda x: x["rx_bytes"] + x["tx_bytes"], reverse=True)


@mcp.tool()
async def get_application_series(
    router: str,
    node: str,
    window_minutes: int = 30,
    application: str | None = None,
    summarize: bool = True,
) -> str:
    """Get per-application traffic data for a router node — which applications
    (YouTube, Zoom, Teams, etc.) are active, by which clients, and over which
    WAN path types (PUBLIC/INTER_ROUTER).

    Requires: app-id with 'http' or 'https' mode — check app_id.has_http_https in get_router_info.

    Use this when list_services shows a broad service like 'internet' carrying
    significant traffic and you need to know which specific applications are
    responsible. list_services answers "what services?" — this answers "what
    apps within those services?"

    The raw API is extremely verbose (per-client, per-nexthop stats across
    multiple time buckets). Use summarize=True (default) for a clean
    per-application summary: client IPs, tenants, services in use, WAN path
    types, and rx/tx byte totals.

    Use summarize=False only when you need per-client or per-nexthop detail
    for a specific flow investigation — combine with the application filter
    to keep the response manageable.

    Args:
        router:         Router name (required).
        node:           Node name (required).
        window_minutes: Time window to query. Default 30 minutes.
                        Use 2-5 minutes for "what's active right now" and
                        a longer window for sustained-usage analysis.
        application:    (optional) Case-insensitive substring filter on
                        application name. E.g. 'youtube', 'zoom', 'teams'.
        summarize:      When True (default), collapse all time buckets into a
                        per-application summary sorted by total bytes.
                        When False, return the raw time-bucketed series.
    """
    buckets = await get_client().get_application_series(router, node, window_minutes)

    if not summarize:
        if application:
            for bucket in buckets:
                bucket["value"] = [
                    e for e in bucket.get("value", [])
                    if application.lower() in (e.get("name") or "").lower()
                ]
        return json.dumps({"bucket_count": len(buckets), "series": buckets}, indent=2)

    summary = _summarize_app_series(buckets, application)
    return json.dumps(
        {
            "window_minutes": window_minutes,
            "bucket_count": len(buckets),
            "application_count": len(summary),
            "applications": summary,
        },
        indent=2,
    )


# ------------------------------------------------------------------
# Metrics
# ------------------------------------------------------------------


def _format_bps(bps: float) -> str:
    if bps >= 1_000_000_000:
        return f"{bps / 1_000_000_000:.1f} Gbps"
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.1f} Mbps"
    if bps >= 1_000:
        return f"{bps / 1_000:.1f} Kbps"
    return f"{bps:.0f} bps"


@mcp.tool()
async def query_stats(
    router: str,
    stat_id: str,
    parameters: list[dict] | None = None,
    unit: str | None = None,
) -> str:
    """Get the current (instantaneous) value of a stat, optionally broken down
    by a dimension. Complements query_metrics — same stat IDs, but returns a
    live point-in-time value instead of a historical time series.

    The stat ID is the same metric path used in query_metrics, e.g.:
      /stats/aggregate-session/node/session-count
      /stats/aggregate-session/node/bandwidth
      /stats/aggregate-session/network-interface/bandwidth
      /stats/aggregate-session/service/session-count

    Parameters control which dimension to itemize by. Each entry is a dict
    with 'name' (the dimension) and 'itemize': true to get per-item breakdown.
    Omit parameters to get a single aggregate value.

    Each parameter entry uses one of two styles:
      itemize  — {'name': 'X', 'itemize': True}  — return all values of X
      filter   — {'name': 'X', 'values': ['v1']} — restrict to specific values

    These can be combined: filter some dimensions to specific values while
    itemizing others to get a breakdown.

    Common parameter names:
      node, network-interface, service, tenant, device-interface,
      process-name, thread-name

    Examples:
      # Bandwidth broken down per network interface
      stat_id='/stats/aggregate-session/network-interface/bandwidth',
      parameters=[{'name': 'network-interface', 'itemize': True}]

      # All SessionProc thread CPU usage for the highway process on one node
      stat_id='/stats/process/thread/cpu/usage',
      parameters=[{'name': 'node', 'values': ['piedmont']},
                  {'name': 'process-name', 'values': ['highway']},
                  {'name': 'thread-name', 'itemize': True}]

      # Single aggregate value (no breakdown)
      stat_id='/stats/aggregate-session/node/bandwidth',
      parameters=[]

    Args:
        router:     Router name (required).
        stat_id:    Stat path (required). Same format as query_metrics metric_id.
        parameters: List of parameter dicts using itemize or values style.
        unit:       Unit hint for formatting. Pass 'bps' to add human-readable
                    bandwidth fields alongside raw values.
    """
    raw = await get_client().query_stats(router, stat_id, parameters)

    # Flatten permutations to a clean list of {dimension: value, value: number}
    results = []
    for entry in raw:
        for perm in entry.get("permutations", []):
            row: dict = {}
            for param in perm.get("parameters", []):
                row[param["name"]] = param["value"]
            raw_value = perm.get("value", "0")
            try:
                row["value"] = float(raw_value) if "." in str(raw_value) else int(raw_value)
            except (ValueError, TypeError):
                row["value"] = raw_value
            if unit == "bps" and isinstance(row["value"], (int, float)):
                row["value_human"] = _format_bps(row["value"])
            results.append(row)

    # Sort by value descending so the most active items appear first
    results.sort(key=lambda x: x.get("value", 0) if isinstance(x.get("value"), (int, float)) else 0, reverse=True)

    return json.dumps({"stat": stat_id, "count": len(results), "values": results}, indent=2)


@mcp.tool()
async def query_metrics(
    router: str,
    metric_id: str,
    window_seconds: int = 1800,
    transform: str = "average",
    resolution: int = 2,
    filters: dict | None = None,
    unit: str | None = None,
    counter: bool = False,
    raw: bool = False,
) -> str:
    """Query a time-series metric from the router's metrics API and return a
    statistical summary. The raw time series (5-second samples) is collapsed
    into current/min/max/average and a trend indicator, which is far more
    useful than hundreds of raw data points.

    Two modes depending on the metric type:

    Gauge metrics (counter=False, default): values that go up and down —
    session count, bandwidth. Summary reports current/min/max/average of
    the raw values.

    Counter metrics (counter=True): values that only ever increase — bytes
    transmitted, packets dropped, TCP retransmissions. Summary reports the
    rate of change between samples (current rate, avg rate, max rate, total
    change over the window). Counter resets (e.g. after a reboot) are treated
    as zero rather than negative spikes.

    Known metric IDs:
      /stats/aggregate-session/node/session-count  — gauge  (unit: 'count')
      /stats/aggregate-session/node/bandwidth       — gauge  (unit: 'bps')

    Args:
        router:         Router name (required).
        metric_id:      Metric path to query (required).
        window_seconds: How far back to look. Default 1800 (30 min).
        transform:      Aggregation applied by the server. Default 'average'.
                        Other known values: 'max', 'min', 'sum', 'latest'.
        resolution:     Sample resolution passed to the API. Default 2.
        filters:        Optional dict of filters passed to the API.
        unit:           Unit hint for human-readable formatting. Pass 'bps'
                        to add formatted fields like '4.2 Mbps' alongside
                        raw values. Works for both gauge and counter mode.
        counter:        Set True for monotonically increasing counters.
                        Computes per-sample deltas and reports rates instead
                        of raw values. Default False.
        raw:            When True, include the full time series in the response.
                        Default False.
    """
    series = await get_client().query_metrics(
        router, metric_id, window_seconds, transform, resolution, filters
    )

    valid = [p["value"] for p in series if "value" in p]

    if not valid:
        return json.dumps({"metric": metric_id, "error": "no data returned"}, indent=2)

    summary: dict = {
        "metric": metric_id,
        "window_seconds": window_seconds,
        "samples": len(valid),
    }

    if counter:
        # Compute per-sample deltas (data is newest-first, so delta = valid[i] - valid[i+1])
        interval = window_seconds / len(valid) if len(valid) > 1 else window_seconds
        deltas = [max(0, valid[i] - valid[i + 1]) for i in range(len(valid) - 1)]

        current_rate = round(deltas[0] / interval, 2) if deltas else 0
        avg_rate = round(sum(deltas) / len(deltas) / interval, 2) if deltas else 0
        max_rate = round(max(deltas) / interval, 2) if deltas else 0
        total_change = valid[0] - valid[-1]  # net increase over the window

        trend = "unknown"
        if len(deltas) >= 4:
            quarter = max(1, len(deltas) // 4)
            recent_avg = sum(deltas[:quarter]) / quarter
            old_avg = sum(deltas[-quarter:]) / quarter
            threshold = old_avg * 0.1 if old_avg else 1
            if recent_avg - old_avg > threshold:
                trend = "increasing"
            elif old_avg - recent_avg > threshold:
                trend = "decreasing"
            else:
                trend = "stable"

        summary.update({
            "sample_interval_seconds": round(interval, 1),
            "current_rate": current_rate,
            "avg_rate": avg_rate,
            "max_rate": max_rate,
            "total_change": total_change,
            "trend": trend,
        })

        if unit == "bps":
            summary["current_rate_human"] = _format_bps(current_rate)
            summary["avg_rate_human"] = _format_bps(avg_rate)
            summary["max_rate_human"] = _format_bps(max_rate)
    else:
        current = valid[0]
        minimum = min(valid)
        maximum = max(valid)
        average = round(sum(valid) / len(valid), 1)

        trend = "unknown"
        if len(valid) >= 4:
            quarter = max(1, len(valid) // 4)
            recent_avg = sum(valid[:quarter]) / quarter
            old_avg = sum(valid[-quarter:]) / quarter
            threshold = old_avg * 0.1
            if recent_avg - old_avg > threshold:
                trend = "increasing"
            elif old_avg - recent_avg > threshold:
                trend = "decreasing"
            else:
                trend = "stable"

        summary.update({
            "current": current,
            "min": minimum,
            "max": maximum,
            "average": average,
            "trend": trend,
        })

        if unit == "bps":
            summary["current_human"] = _format_bps(current)
            summary["average_human"] = _format_bps(average)
            summary["max_human"] = _format_bps(maximum)

    if raw:
        summary["series"] = series

    return json.dumps(summary, indent=2)


@mcp.tool()
async def get_bgp_summary(
    router: str,
    vrf: str = "default",
    address_family: str = "all",
) -> str:
    """Get BGP summary for a router — equivalent to 'show bgp summary'.

    Returns structured per-address-family data including router ID, local AS,
    RIB counts, and a per-peer breakdown with state, uptime, prefix counts,
    connections established/dropped, and hostname.

    Context: router

    Args:
        router:         Router name (required).
        vrf:            VRF name. Default 'default'.
        address_family: Address family to query. Default 'all'.
                        Other values: 'ipv4', 'ipv6'.
    """
    result = await get_client().get_bgp_summary(router, vrf, address_family)
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_bgp_routes(
    router: str,
    vrf: str = "default",
    address_family: str = "ipv4",
) -> str:
    """Get the full BGP routing table — equivalent to 'show bgp'.

    Returns every prefix with all candidate paths. Each path includes:
    bestpath flag, selection reason, AS path, origin, metric, weight,
    peer ID, and nexthops with hostnames.

    Context: router

    Args:
        router:         Router name (required).
        vrf:            VRF name. Default 'default'.
        address_family: Address family. Default 'ipv4'. Also: 'ipv6'.
    """
    result = await get_client().get_bgp_routes(router, vrf, address_family)
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_bgp_advertised_routes(
    router: str,
    neighbor: str,
    vrf: str = "default",
    address_family: str = "ipv4",
) -> str:
    """Get BGP routes advertised to a specific neighbor — equivalent to
    'show bgp neighbors <neighbor> advertised-routes'.

    Returns each advertised prefix with next-hop, AS path, origin code,
    metric, weight, and applied status symbols.

    Context: router

    Args:
        router:         Router name (required).
        neighbor:       Neighbor IP address (required).
        vrf:            VRF name. Default 'default'.
        address_family: Address family. Default 'ipv4'. Also: 'ipv6'.
    """
    result = await get_client().get_bgp_advertised_routes(router, neighbor, vrf, address_family)
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_bgp_received_routes(
    router: str,
    neighbor: str,
    vrf: str = "default",
    address_family: str = "ipv4",
) -> str:
    """Get BGP routes received from a specific neighbor — equivalent to
    'show bgp neighbors <neighbor> received-routes'.

    Returns each received prefix with next-hop, AS path, origin code, metric,
    weight, and applied status symbols (valid, best, etc.).

    Context: router

    Args:
        router:         Router name (required).
        neighbor:       Neighbor IP address (required).
        vrf:            VRF name. Default 'default'.
        address_family: Address family. Default 'ipv4'. Also: 'ipv6'.
    """
    result = await get_client().get_bgp_received_routes(router, neighbor, vrf, address_family)
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_bgp_neighbors(
    router: str,
    vrf: str = "default",
    address_family: str = "ipv4",
    neighbor: str | None = None,
) -> str:
    """Get detailed BGP neighbor information — equivalent to 'show bgp neighbors'.

    Returns per-neighbor detail including BGP state, uptime, message stats,
    capabilities, address family info (accepted/sent prefix counts, route-maps),
    graceful restart state, last reset reason, and estimated RTT.

    Also includes a top-level '_svr_neighbors' list of neighbor IPs that are
    BGP-over-SVR peers (detected by the presence of a '_bgp_<ip>/32' service).
    Neighbors whose IP appears in '_svr_neighbors' use SVR transport; all others
    are standard BGP over IP.

    Context: router

    Args:
        router:         Router name (required).
        vrf:            VRF name. Default 'default'.
        address_family: Address family. Default 'ipv4'. Also: 'ipv6'.
        neighbor:       Filter to a specific neighbor IP. Omit for all neighbors.
    """
    result = await get_client().get_bgp_neighbors(router, vrf, address_family, neighbor)
    return json.dumps(result, indent=2)


@mcp.tool()
async def ping(
    router: str,
    node: str,
    destination_ip: str,
    count: int = 10,
    size: int = 56,
    timeout: int = 3,
    egress_interface: str | None = None,
    dont_frag: bool = False,
) -> str:
    """Send a series of ICMP echo requests from an SSR node and return a
    latency / loss summary.

    Each probe is an individual ICMP echo request. Results are aggregated
    into min/avg/max RTT, jitter (std-dev of RTTs), and packet loss percent,
    plus a per-probe breakdown so intermittent drops are visible.

    Context: router

    Args:
        router:           Router name (required).
        node:             Node name to send the pings from (required).
        destination_ip:   Target IP address.
        count:            Number of ICMP probes to send. Default 10.
        size:             ICMP payload size in bytes. Default 56.
        timeout:          Per-probe timeout in seconds. Default 3.
        egress_interface: Network interface to send probes out of
                          (e.g. "uverse"). Omit to use default routing.
        dont_frag:        Set the DF bit. Default False.
    """
    result = await get_client().ping(
        router, node, destination_ip, count, size, timeout, egress_interface, dont_frag
    )
    return json.dumps(result, indent=2)


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------


def main() -> None:
    if _TRANSPORT == "stdio":
        mcp.run(transport="stdio")
        return

    import anyio
    import uvicorn

    async def _serve() -> None:
        if _TRANSPORT == "streamable-http":
            app: object = mcp.streamable_http_app()
        else:
            app = mcp.sse_app()
        if _AUTH_TOKEN:
            app = _BearerAuthMiddleware(app, _AUTH_TOKEN)
        config = uvicorn.Config(app, host=_HOST, port=_PORT)
        await uvicorn.Server(config).serve()

    anyio.run(_serve)


if __name__ == "__main__":
    main()
