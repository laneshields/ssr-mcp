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

_SESSION_GAP_SECONDS = 30 * 60  # gap after which a new session is assumed


def _maybe_log_session_start(tool_name: str) -> None:
    """Write a synthetic query record if this looks like the start of a new session.

    Fires when any tool other than begin_query is the first call after a gap of
    at least _SESSION_GAP_SECONDS, so orphaned tool calls are still groupable by
    session even when the model skips begin_query.
    """
    if tool_name == "begin_query":
        return
    try:
        now = datetime.now(timezone.utc)
        last_ts = None

        if _LOG_PATH.exists():
            with _LOG_PATH.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                if size > 0:
                    f.seek(max(0, size - 4096))
                    tail = f.read().decode("utf-8", errors="replace")
                    lines = [ln for ln in tail.strip().splitlines() if ln.strip()]
                    if lines:
                        try:
                            last_ts = datetime.fromisoformat(
                                json.loads(lines[-1])["ts"]
                            )
                        except Exception:
                            pass

        is_new_session = last_ts is None or (
            now - last_ts
        ).total_seconds() > _SESSION_GAP_SECONDS

        if is_new_session:
            _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "ts": now.isoformat(),
                "type": "query",
                "question": f"[unlogged — first tool: {tool_name}]",
            }
            with _LOG_PATH.open("a") as f:
                f.write(json.dumps(record) + "\n")
    except Exception:
        pass  # never let logging break a tool call


def _log_tool_call(name: str, kwargs: dict, response: str) -> None:
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": "tool_call",
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
            _maybe_log_session_start(fn.__name__)
            result = await fn(**fkwargs)
            _log_tool_call(fn.__name__, fkwargs, result)
            return result

        logged.__signature__ = inspect.signature(fn)
        return original_decorator(logged)

    return decorator


mcp.tool = _logged_tool

# ------------------------------------------------------------------
# Query context
# ------------------------------------------------------------------

_GUIDANCE = """# SSR MCP Operational Guidance

## Session startup

1. Call `begin_query` first on every user request — before any other tool.
2. Call `get_connection_info` at the start of a session to determine connection
   mode (conductor, router-managed, router-cloud, router-standalone).
3. Before doing router-specific work, call `get_router_info` to get node names
   (required by most router-targeted tools), software version, and whether
   app-id is enabled. Do not guess node names.

## Connection modes

| Mode | Description |
|---|---|
| conductor | SSR_HOST is a conductor. All managed routers accessible by name via `router:` param. Conductor-only tools available (`get_assets`, `find_sessions`). |
| router-managed | Connected directly to a conductor-managed router. Only local device accessible. Conductor-only tools unavailable. |
| router-cloud | Mist/cloud-managed router. No on-premises conductor. |
| router-standalone | Standalone router with no management plane. |

In conductor mode, tools with an optional `router:` param scope authority-wide
when omitted. In all router modes, they scope to the local device.

## SSR traffic flow

Understanding this prevents misdiagnosis.

1. **Tenant classification** — source IP + ingress interface matched against
   tenant membership rules to assign a tenant.
2. **FIB lookup** — tenant + dest IP + port + protocol looked up. Either 0 or 1
   match.
   - **No match (FIB miss):** SSR sends ICMP network unreachable. Packet is
     silently dropped and **does NOT appear in `get_dropped_packets`**.
     `fib_lookup` returning no result confirms a FIB miss. Root cause: missing
     service or incorrect tenant config.
   - **Match:** entry identifies the service and provides 0 or more next-hops.
3. **Next-hop check:**
   - **0 next-hops:** traffic cannot be forwarded. **Appears in
     `get_dropped_packets`.**
   - **1+ next-hops:** packet proceeds to service area for session processing.
4. **Service area processing** — session established or fails. Failures **appear
   in `get_dropped_packets`**.
5. **Session established but traffic broken** — if `get_dropped_packets` is
   empty, `fib_lookup` shows a valid match with next-hops, and sessions are
   establishing but traffic is still broken, the fault is likely outside the
   SSR. Check `list_service_paths` and `list_peer_paths`; if healthy the
   problem is external.

## Connectivity troubleshooting decision tree

- No source/destination specifics → `get_dropped_packets` unfiltered
- Service area CPU high, no explanation → `get_dropped_packets` (flood of
  unmatched traffic)
- Have source + destination → `fib_lookup`
  - No result → FIB miss → tenant or service config problem (not in dropped
    packets)
  - Result, 0 next-hops → `get_dropped_packets` to confirm + check
    service/path config
  - Result, 1+ next-hops → `get_sessions` to confirm session; if session
    exists check `list_service_paths` / `list_peer_paths`
- `get_dropped_packets` empty + valid FIB match + sessions establishing +
  traffic broken → problem outside SSR

## Resolving a vague source

- Hostname/device description → `get_dhcp_leases` (by hostname) or `get_arp`
- Physical interface name → `get_device_interfaces`; if single network
  interface on it, use that name directly
- Network interface name → match directly in `get_network_interfaces`
- Source IP, directly connected → match subnet against ethernet-type
  interfaces in `get_network_interfaces`
- Source IP, off-network → `get_rib` LPM → next-hop `interfaceName` is a giid
  (e.g. `g12`) → match number against `globalId` in `get_network_interfaces`

## Resolving a vague destination

- Named service ("internet", "corporate-vpn") → `list_services` then
  `list_service_paths`
- Application name ("Teams", "Zoom"):
  - `has_http_https` enabled → `get_app_id_cache` (address cache,
    `summarize=False`) filtered by app name → dest IP/port/protocol →
    `fib_lookup`
  - Completely broken (cache may be empty) → `get_dropped_packets`
  - App-id not enabled → ask user for IP/port
- Domain name → `app_id_lookup` domain mode (requires `has_http_https`)
- All else fails → `get_dropped_packets`

## Slow traffic investigation

Use this when the user reports slowness, high latency, or degraded throughput
— as opposed to traffic being completely broken (use the connectivity decision
tree for that).

**First: ask which router** if in conductor mode and not already clear from
context. Don't broadcast across all routers — slow traffic is always
router-specific.

**Key tool distinction:**
- `get_top_applications` — bandwidth ranking ("what's using my bandwidth?")
- `get_application_tcp_health` — TCP health signals ("what's slow or lossy?")

For slow traffic, **start with `get_application_tcp_health`**, not sessions or
top sources. It returns retransmission rates, duplicate ACKs, out-of-window
segments, and RTT per application — the signals that explain *why* traffic is
slow, not just how much there is.

**Step order:**
1. `begin_query` + `get_router_info` (get node name and confirm app-id status)
2. In parallel: `get_session_processor_utilization`, `get_node_utilization`,
   `get_dropped_packets`, `get_network_interfaces`, `get_device_interfaces`,
   `get_alarms`
3. If `has_http_https` true: `get_application_tcp_health` (window_minutes=30)
   — identifies which applications have elevated retransmissions/latency
4. `list_service_paths` filtered to the affected service — check path quality
   (latency, jitter, loss) on each peer path

**TCP health signal interpretation:**
- `tcp_retrans_from_server_pct` high → loss between SSR and server (WAN side)
- `tcp_retrans_from_client_pct` high → loss between client and SSR (LAN side)
- `ssr_retrans_to_client` / `ssr_retrans_to_server` non-zero → SSR itself is
  retransmitting; correlate with session processor CPU
- `avg_fwd_rtt_ms` elevated → WAN latency to the server is the bottleneck
- `avg_tcp_connection_ms` (TTFP) high → session setup latency; check peer path
  latency and service path availability

**If app-id is not enabled:** skip `get_application_tcp_health`; use
`query_stats` on `tcp-retransmissions` by network-interface to find which
interface has active loss, then check `list_service_paths` for that path.

**Critical: correlate path quality to the service actually in use.**
`list_peer_paths` and `get_router_health` show SVR peer path quality between
routers. A peer path being DOWN or lossy only affects services that route
traffic through that peer. It does not affect services using direct internet
breakout (e.g. a service whose `list_service_paths` shows a WAN interface
next-hop rather than an SVR peer). Before citing peer path degradation as a
cause of slowness, confirm with `list_service_paths` that the affected traffic
actually traverses that peer. For direct internet services, the relevant signal
is WAN interface quality: check `get_device_interfaces` for errors and
`query_stats` for `tcp-retransmissions` by network-interface on the WAN
interface the service uses.

## Metric interpretation

SSR metrics fall into two types. Using the wrong approach risks false alarms
or missed problems.

**Cumulative counters** (dropped packets, TCP retransmissions, fragmentation
events, error counts): values only ever increase. The raw value is meaningless
in isolation. Use `query_metrics` with `counter=True` and check `total_change`
over a window (default 30 min). Zero = no events in that period. Non-zero =
condition is active now.

**Gauge time-series** (CPU%, memory%, session count, bandwidth): values
fluctuate. A single point-in-time reading cannot distinguish a spike from
sustained load. Use `query_metrics` with `counter=False` and compare `avg`
against `current` over a 30-min window:
- `current` >> `avg`: transient spike; not necessarily a problem.
- `current` ≈ `avg` and both high: sustained load; treat as a real issue.
- `trend = "decreasing"`: condition was worse earlier; may be self-resolving.

**Rule:** when a point-in-time tool flags something high, use `query_metrics`
to confirm whether it is sustained or historical before treating it as a
confirmed problem.
"""


@mcp.tool()
async def begin_query(question: str) -> str:
    """Log the user's question and return operational guidance.

    Call this FIRST at the start of every user request, before calling any
    other tools. Pass a concise restatement of what the user is asking.

    Returns operational guidance covering session startup rules, connection
    modes, the SSR traffic flow model, connectivity and slow-traffic
    troubleshooting decision trees, and metric interpretation. Read it before
    proceeding — it determines which tools to call and in what order.

    Args:
        question: A one- or two-sentence summary of what the user is asking.
    """
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": "query",
            "question": question,
        }
        with _LOG_PATH.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass
    return _GUIDANCE


@mcp.tool()
async def get_guidance() -> str:
    """Return operational guidance for using this MCP server effectively.

    Also returned automatically by `begin_query` — call this only if you need
    to re-read the guidance mid-session without logging a new query.

    No arguments required.
    """
    return _GUIDANCE


@mcp.tool()
async def report_issue(
    tool: str,
    observation: str,
    data: str | None = None,
) -> str:
    """Report a suspected bug or data anomaly encountered during a tool call.

    Call this whenever a tool result looks wrong or inconsistent — for example,
    a summary returning "unknown" for fields that should have values, a result
    that contradicts another tool's output, or a response shape that doesn't
    match the documented format.

    Reports are written to the same log file as tool calls and query records so
    they can be correlated with the session that triggered them.

    Args:
        tool:        Name of the tool that returned the suspicious result.
        observation: Concise description of what was unexpected and why.
        data:        Optional snippet of the suspicious result (JSON string or
                     short excerpt) to attach for context.
    """
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record: dict = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": "issue",
            "tool": tool,
            "observation": observation,
        }
        if data is not None:
            record["data"] = data
        with _LOG_PATH.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass
    return "Issue logged. Thank you — this helps improve the tools."


@mcp.tool()
async def report_feedback(
    complaint: str,
    what_went_wrong: str,
    context: str,
) -> str:
    """Log user dissatisfaction with a result during a troubleshooting session.

    Call this when the user indicates the answer or analysis was wrong,
    incomplete, or unhelpful — for example: "that's not right", "you missed X",
    "I expected Y", "that doesn't make sense". Do not wait for the user to ask
    you to log it; call it proactively as soon as dissatisfaction is clear.

    Args:
        complaint:      What the user said or expressed, quoted or closely
                        paraphrased.
        what_went_wrong: Your honest self-assessment of the error — which step
                         failed, what assumption was wrong, or what you should
                         have done differently.
        context:        A brief summary of the interaction at the point of
                        complaint: which tools were called, what they returned,
                        and what you reported to the user. Include key values
                        (router name, tool names, result snippets) so the record
                        is useful without replaying the full conversation.
    """
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": "feedback",
            "complaint": complaint,
            "what_went_wrong": what_went_wrong,
            "context": context,
        }
        with _LOG_PATH.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass
    return "Feedback logged. Thank you — this helps improve the tools."


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
                        Conductor-specific tools are not available. A
                        display_name field is included — use it (not the
                        router UUID) when referring to the device to the user.

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
async def get_conductor_summary() -> str:
    """Get a compact health overview of the entire conductor deployment.

    Returns aggregate counts rather than raw lists, making it safe to call on
    conductors managing hundreds or thousands of routers. Specifically:
    - Conductor software version and alarm counts
    - Router counts: total, connected, disconnected (disconnected names capped
      at 20; use list_routers if you need the full list)
    - Alarm counts by severity across all routers, sorted by worst severity
      first, with a per-router breakdown of which routers have alarms

    Use this as the first call in a conductor-wide health check instead of
    list_routers + get_assets + get_alarms. Follow up with get_router_health
    on specific routers identified here.

    Context: conductor only
    """
    result = await get_client().get_conductor_summary()
    return json.dumps(result, indent=2)


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
                for net_iface in (dev_iface.get("networkInterfaces") or {}).get("nodes") or []:
                    if (net_iface.get("name") or "").startswith("dhcp-server-gen-"):
                        continue
                    plugin_state = (net_iface.get("plugins") or {}).get("state") or {}
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
    the operational status of the underlying device interface, the configured
    mtu, and enforcedMss.

    MTU/MSS notes:
    - mtu: the configured MTU on this interface. For IP-routed (non-SVR)
      traffic, enforcedMss=automatic clamps TCP MSS based on this value.
    - enforcedMss: automatic = SSR clamps TCP MSS; disabled = no clamping.
    - For SVR traffic, MSS clamping uses the path-discovered MTU from
      list_peer_paths, not this configured value.
    - Even when SVR paths show a valid discovered MTU and enforcedMss is
      automatic, verify this configured mtu matches the physical network
      — it governs MSS for all non-SVR traffic through the interface.

    Each interface includes a globalId field — this is the internal global
    interface ID (giid) used by the routing stack. RIB next-hop entries
    reference interfaces by giid in the format 'gX' (e.g. 'g12'). Match the
    number X against globalId to resolve a giid to a network interface name.

    To resolve a source IP to a network interface name for fib_lookup:
      1. Check whether the source IP falls within the subnet of any interface
         whose deviceInterface.type is 'ethernet' (forwarding interfaces).
         If it matches, that interface name is the source_interface. Ignore
         host-type device interfaces — these are internal SSR interfaces.
      2. If no subnet matches (off-network source), call get_rib with the
         source IP to get the LPM next-hop, extract the giid from
         interfaceName (e.g. 'g12'), then match against globalId here to
         get the interface name.

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
    tenant: str | None = None,
    source_ip: str | None = None,
    source_interface: str | None = None,
) -> str:
    """Look up the FIB entry that would be matched for a specific packet.
    Returns the matched service name and next-hop(s) the dataplane would
    select, including the resolved tenant.

    Use this when you know the source and destination details for a flow
    that is not working — it gives a definitive answer about what the
    dataplane would do with that traffic. Use get_dropped_packets instead
    when you don't have specific flow details.

    Two ways to identify the source:
      source_ip + source_interface — provide the client IP and the ingress
          network interface name. The router resolves the tenant automatically.
          Prefer this when the tenant name is not already known. Use
          get_network_interfaces to resolve source_interface from the source IP.

      tenant — provide the tenant name directly. Use when you already know
          the tenant from a prior tool call (e.g. get_dropped_packets output
          or list_tenant_members).

    The response includes the matched service name. Pass that to
    list_service_paths to check whether the service's paths are up.

    dest_ip, dest_port, and protocol can be sourced from get_app_id_cache
    address entries when the destination is known by application name rather
    than IP (e.g. 'Teams', 'Zoom'). If the cache has no entries for the
    application and all else fails, use get_dropped_packets to watch for
    relevant failures in the dropped packet stream.

    Args:
        router:           Router name (required).
        node:             Node name (required).
        dest_ip:          Destination IP address, e.g. '1.1.1.1'.
        dest_port:        Destination L4 port, e.g. 53.
        protocol:         IP protocol, e.g. 'udp', 'tcp', 'icmp'.
        tenant:           Source tenant name — use if already known.
        source_ip:        Source IP address — use with source_interface to
                          resolve tenant automatically.
        source_interface: Ingress network interface name, e.g. 'home_lan' —
                          use with source_ip.
    """
    data = await get_client().fib_lookup(
        router, node, dest_ip, dest_port, protocol, tenant, source_ip, source_interface
    )
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

    Each path includes reachabilityProbeType and reachabilityProbes fields.
    When reachabilityProbeType is not null, an ICMP reachability probe is
    configured for that service route. The probe status ("up"/"down") is a
    binary reachability signal. For actual performance metrics from the probe,
    query these stats (itemize by node for HA pairs):
      /stats/icmp/reachability-probe/service-routes/latency  (ms)
      /stats/icmp/reachability-probe/service-routes/jitter   (ms)
      /stats/icmp/reachability-probe/service-routes/loss     (%)
    These stats aggregate across all probed service routes on the router.

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

    Always pass router when connected to a conductor that manages many routers.
    Omitting router fans out the query to every managed router, generates a
    large response, and produces connectivity errors for any offline nodes.

    Args:
        router:   Router name. Always provide this on a multi-router conductor.
        node:     (optional) Limit to a specific node.
        limit:    Number of top sources to return. Default 10.
        order_by: Metric to rank by. Valid values: 'TOTAL_DATA',
                  'SESSION_COUNT'. Default 'TOTAL_DATA'.
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

    Passing a host address to ip (e.g. '192.168.1.50') returns the
    longest-prefix match for that address, which is useful for determining
    which interface traffic from an off-network source arrives on. Next-hop
    interfaceName values use the internal giid format ('g2', 'g0', etc.) —
    match the number against the globalId field in get_network_interfaces
    output to resolve to the actual network interface name.

    Args:
        router:      Router name (required).
        vrf:         (optional) Filter by VRF name.
        ip:          (optional) Host address or prefix for LPM lookup,
                     e.g. '192.168.1.50' or '10.0.0.0/8'.
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

    Use summarize=True for broad questions ("what apps are in use?") to get a
    ranked count by application and category instead of raw entries.

    Use summarize=False with cache='address' to find the destination IPs,
    ports, and protocols associated with a specific application name — filter
    the results by the application field and feed matching entries into
    fib_lookup. Note: there is no server-side application filter; filtering
    happens on the returned entries. This is most useful for transient failures
    where the application was previously working and cache entries still exist.
    If connectivity is completely broken and sessions never established, the
    cache may be empty for that application — use get_dropped_packets instead.

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

    Also use this as the first step when the destination is described by name
    rather than IP (e.g. "the internet", "corporate VPN", "the file server").
    Find the most likely matching service name, then pass it to
    list_service_paths to check whether that service's paths are up.

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

    Use this in two situations:
      1. Connectivity broken, no specific flow details — unfiltered, it shows
         what traffic is failing and why across the whole node, which is often
         enough to identify the root cause without knowing the source or
         destination in advance.
      2. Service area CPU is unexpectedly high — drops consume service area
         CPU even when no user is aware of the failing traffic (e.g. a flood
         of unmatched packets, a misconfigured client, or a port scan). Use
         this alongside get_node_utilization or get_session_processor_utilization
         to investigate elevated CPU with no obvious active-session explanation.

    When you do have specific flow details, use filters to narrow the stream
    to that traffic, or use fib_lookup instead for a definitive answer on
    what the dataplane would do with a specific packet.

    Returns a pattern summary: drops by reason, by ingress interface, top
    source IPs, top destination IP:port pairs, and protocol breakdown. This
    is usually enough to identify the root cause and the configuration change
    needed to resolve it.

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

        src_obj = event.get("source") or {}
        src = src_obj.get("address") or event.get("sourceIp") or event.get("sourceAddress") or "unknown"
        by_source_ip[src] = by_source_ip.get(src, 0) + 1

        dst_obj = event.get("destination") or {}
        dst_ip = dst_obj.get("address") or event.get("destIp") or event.get("destinationIp") or "unknown"
        dst_port = dst_obj.get("port") or event.get("destPort") or event.get("destinationPort")
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

    All metrics live at the nextHopInterface level in the raw API (without
    roll-up-metrics). Client objects carry only failedSessions; application
    objects carry no metrics at all. activeSessions is therefore read from
    nexthops, not from the client object.

    IDP dedup: when IDP service function chaining is active, the same client
    IP can appear under two different ingressIntf values (the true dedup key
    is clientIp + networkInterface). We dedup by address only via max(), which
    is correct for the IDP case (both entries represent the same session) but
    would merge a legitimately multi-homed client into one row.

    Localization semantics for TCP fields:
      tcp_retrans_from_server + dup_acks_fwd high → WAN downlink loss (server→client)
      tcp_retrans_from_client + dup_acks_rev high → LAN or uplink loss (client→server)
      ssr_retrans_to_* non-zero                   → SSR itself is retransmitting
      avg_fwd_rtt_ms high                         → WAN latency (client→server RTT)
      avg_rev_rtt_ms high                         → reverse-path latency (server→client RTT)
    """
    # All metrics read from the nextHopInterface level, deduped via max() per client addr.
    # Intermediate accumulator keys (ttfp_*, rtt_*) are used to compute averages and
    # are not included in the final result dict.
    _TCP_KEYS = (
        "active_sessions",
        "tcp_retrans_from_server",
        "tcp_retrans_from_client",
        "ssr_retrans_to_client",
        "ssr_retrans_to_server",
        "dup_acks_fwd",
        "dup_acks_rev",
        "out_of_window_fwd",
        "out_of_window_rev",
        "tcp_resets",
        "new_sessions",
        "rx_packets",
        "tx_packets",
        "ttfp_total_ms",
        "ttfp_count",
        "fwd_rtt_total_ms",
        "fwd_rtt_count",
        "rev_rtt_total_ms",
        "rev_rtt_count",
    )
    # failed_sessions is at the client level (sessions dropped before nexthop assignment),
    # not the nexthop level where it is always 0. Tracked separately.
    _CLIENT_KEYS = ("failed_sessions",)

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
                    "client_stats": {},  # addr -> {rx, tx, tcp...}
                    "tenants": set(),
                    "services": set(),
                    "next_hop_types": set(),
                    "svr_peers": set(),
                    "traffic_classes": set(),
                }

            app = apps[name]

            for client in entry.get("clients", []):
                addr = client.get("address", "unknown")
                if client.get("tenant"):
                    app["tenants"].add(client["tenant"])
                for svc in client.get("services") or []:
                    app["services"].add(svc if isinstance(svc, str) else svc.get("name", str(svc)))

                # Client-level fields (before nexthop assignment).
                client_vals: dict[str, int] = {
                    "failed_sessions": client.get("failedSessions") or 0,
                }

                # Sum nexthop-level metrics across all nexthops for this appearance.
                nh_rx = nh_tx = 0
                tcp: dict[str, int] = {k: 0 for k in _TCP_KEYS}

                for nh in client.get("nextHopInterface") or []:
                    if nh.get("type"):
                        app["next_hop_types"].add(nh["type"])
                    if nh.get("type") == "INTER_ROUTER" and nh.get("peerName"):
                        app["svr_peers"].add(nh["peerName"])
                    if nh.get("trafficClass"):
                        app["traffic_classes"].add(nh["trafficClass"])
                    nh_rx += nh.get("rxBytes") or 0
                    nh_tx += nh.get("txBytes") or 0
                    tcp["active_sessions"] += nh.get("activeSessions") or 0
                    tcp["tcp_retrans_from_server"] += nh.get("tcpRetransmissionPacketsFromServer") or 0
                    tcp["tcp_retrans_from_client"] += nh.get("tcpRetransmissionPacketsFromClient") or 0
                    tcp["ssr_retrans_to_client"] += nh.get("ssrInitiatedTcpRetransmissionPacketsToClient") or 0
                    tcp["ssr_retrans_to_server"] += nh.get("ssrInitiatedTcpRetransmissionPacketsToServer") or 0
                    tcp["dup_acks_fwd"] += nh.get("fwdTcpDuplicateAcks") or 0
                    tcp["dup_acks_rev"] += nh.get("revTcpDuplicateAcks") or 0
                    tcp["out_of_window_fwd"] += nh.get("fwdTcpOutOfWindows") or 0
                    tcp["out_of_window_rev"] += nh.get("revTcpOutOfWindows") or 0
                    tcp["tcp_resets"] += (
                        (nh.get("rxFwdTcpResets") or 0)
                        + (nh.get("rxRevTcpResets") or 0)
                        + (nh.get("txFwdTcpResets") or 0)
                        + (nh.get("txRevTcpResets") or 0)
                    )
                    tcp["new_sessions"] += nh.get("newSessions") or 0
                    tcp["rx_packets"] += nh.get("rxPackets") or 0
                    tcp["tx_packets"] += nh.get("txPackets") or 0
                    # TTFP: aggregate TCP and TLS keys (TLS uses its own key, not "TCP")
                    ttfp = nh.get("timeToFirstDataPacketMs")
                    if isinstance(ttfp, dict):
                        for proto in ("TCP", "TLS"):
                            if proto in ttfp:
                                tcp["ttfp_total_ms"] += ttfp[proto].get("total") or 0
                                tcp["ttfp_count"] += ttfp[proto].get("count") or 0
                    # RTT: fwdTcpAckRttMs / revTcpAckRttMs use hardcoded "TCP" key for both TCP and TLS
                    fwd_rtt = nh.get("fwdTcpAckRttMs")
                    if isinstance(fwd_rtt, dict) and "TCP" in fwd_rtt:
                        tcp["fwd_rtt_total_ms"] += fwd_rtt["TCP"].get("total") or 0
                        tcp["fwd_rtt_count"] += fwd_rtt["TCP"].get("count") or 0
                    rev_rtt = nh.get("revTcpAckRttMs")
                    if isinstance(rev_rtt, dict) and "TCP" in rev_rtt:
                        tcp["rev_rtt_total_ms"] += rev_rtt["TCP"].get("total") or 0
                        tcp["rev_rtt_count"] += rev_rtt["TCP"].get("count") or 0

                # max() across appearances of the same addr (IDP dedup + bucket dedup)
                if addr not in app["client_stats"]:
                    app["client_stats"][addr] = {
                        "rx": nh_rx, "tx": nh_tx,
                        **tcp, **client_vals,
                    }
                else:
                    s = app["client_stats"][addr]
                    s["rx"] = max(s["rx"], nh_rx)
                    s["tx"] = max(s["tx"], nh_tx)
                    for k in _TCP_KEYS:
                        s[k] = max(s[k], tcp[k])
                    for k in _CLIENT_KEYS:
                        s[k] = max(s[k], client_vals[k])

    result = []
    for app in apps.values():
        clients = app["client_stats"]
        total_rx = sum(c["rx"] for c in clients.values())
        total_tx = sum(c["tx"] for c in clients.values())
        # Sum deduplicated per-client totals across all clients
        all_keys = _TCP_KEYS + _CLIENT_KEYS
        tcp_totals: dict[str, int] = {k: sum(c[k] for c in clients.values()) for k in all_keys}
        total_pkts = tcp_totals["rx_packets"] + tcp_totals["tx_packets"]
        avg_ttfp_ms = (
            round(tcp_totals["ttfp_total_ms"] / tcp_totals["ttfp_count"])
            if tcp_totals["ttfp_count"] else None
        )
        avg_fwd_rtt_ms = (
            round(tcp_totals["fwd_rtt_total_ms"] / tcp_totals["fwd_rtt_count"])
            if tcp_totals["fwd_rtt_count"] else None
        )
        avg_rev_rtt_ms = (
            round(tcp_totals["rev_rtt_total_ms"] / tcp_totals["rev_rtt_count"])
            if tcp_totals["rev_rtt_count"] else None
        )

        result.append({
            "name": app["name"],
            "type": app["type"],
            "category": app["category"],
            "active_sessions": tcp_totals["active_sessions"],
            "new_sessions": tcp_totals["new_sessions"],
            "failed_sessions": tcp_totals["failed_sessions"],
            "unique_clients": len(clients),
            "clients": sorted(clients.keys()),
            "tenants": sorted(app["tenants"]),
            "services": sorted(app["services"]),
            "next_hop_types": sorted(app["next_hop_types"]),
            "svr_peers": sorted(app["svr_peers"]),
            "traffic_classes": sorted(app["traffic_classes"]),
            "rx_bytes": total_rx,
            "tx_bytes": total_tx,
            "rx_packets": tcp_totals["rx_packets"],
            "tx_packets": tcp_totals["tx_packets"],
            "tcp_retrans_from_server": tcp_totals["tcp_retrans_from_server"],
            "tcp_retrans_from_server_pct": round(100 * tcp_totals["tcp_retrans_from_server"] / total_pkts, 2) if total_pkts else None,
            "tcp_retrans_from_client": tcp_totals["tcp_retrans_from_client"],
            "tcp_retrans_from_client_pct": round(100 * tcp_totals["tcp_retrans_from_client"] / total_pkts, 2) if total_pkts else None,
            "ssr_retrans_to_client": tcp_totals["ssr_retrans_to_client"],
            "ssr_retrans_to_server": tcp_totals["ssr_retrans_to_server"],
            "dup_acks_fwd": tcp_totals["dup_acks_fwd"],
            "dup_acks_rev": tcp_totals["dup_acks_rev"],
            "out_of_window_fwd": tcp_totals["out_of_window_fwd"],
            "out_of_window_rev": tcp_totals["out_of_window_rev"],
            "tcp_resets": tcp_totals["tcp_resets"],
            "avg_tcp_connection_ms": avg_ttfp_ms,
            "avg_fwd_rtt_ms": avg_fwd_rtt_ms,
            "avg_rev_rtt_ms": avg_rev_rtt_ms,
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
    """Get full per-application traffic data including client IPs, tenants, and
    path detail for a router node.

    Requires: app-id with 'http' or 'https' mode — check app_id.has_http_https in get_router_info.

    Prefer the focused tools for common cases — they call the same API but
    return only what's needed:
      get_top_applications     — "what's using my bandwidth?" (compact, top-N)
      get_application_tcp_health — "what's slow or lossy?" (TCP health metrics only)

    Use this tool when you need per-client detail: which specific IPs are using
    an application, which tenants, which SVR peers, or which services — typically
    after get_top_applications or get_application_tcp_health identifies a suspect.

    Use summarize=False only when you need raw per-nexthop time-bucketed data
    for a specific flow investigation — combine with the application filter
    to keep the response manageable.

    TCP health fields in the summarize output (key for slow-traffic triage):
      tcp_retrans_from_server / tcp_retrans_from_server_pct
          Server is retransmitting → loss on the WAN downlink (server→client).
      tcp_retrans_from_client / tcp_retrans_from_client_pct
          Client is retransmitting → loss on the LAN or WAN uplink (client→server).
      ssr_retrans_to_client / ssr_retrans_to_server
          SSR itself is retransmitting — non-zero means the SSR is the bottleneck.
      dup_acks_fwd / dup_acks_rev
          Duplicate ACKs in the forward (fwd) and reverse (rev) directions;
          corroborate retransmission direction — fwd high → WAN downlink loss,
          rev high → LAN/uplink loss.
      out_of_window_fwd / out_of_window_rev
          TCP out-of-window events; elevated fwd suggests client buffer exhaustion,
          elevated rev suggests server buffer exhaustion.
      tcp_resets     — total TCP RST events; elevated = connections being torn down.
      failed_sessions — sessions that could not be established.
      avg_tcp_connection_ms — mean time to first data packet for TCP flows;
          effectively RTT to the server plus server processing time.

    services field maps each application to the SSR service(s) handling it —
    use with list_service_paths to check path health and SVR vs IP forwarding.
    next_hop_types: PUBLIC = plain IP forwarding; INTER_ROUTER = SVR peer path.
    svr_peers: router names of SVR peers carrying this application's traffic —
    use with list_peer_paths to check that specific peer's path health.
    traffic_classes: SSR per-nexthop traffic class tags (high/medium/low/best-effort).
    Reflects the queue slot the SSR assigns each flow, not whether TE is actively
    enforcing differentiation. Without explicit TE config all traffic typically lands
    in low regardless of what this field shows.

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


@mcp.tool()
async def get_top_applications(
    router: str,
    node: str,
    top_n: int = 10,
    window_minutes: int = 30,
) -> str:
    """Get the top N applications by traffic volume for a router node.

    Requires: app-id with 'http' or 'https' mode — check app_id.has_http_https in get_router_info.

    Returns a compact summary (name, bytes, sessions, services, path types) for
    the highest-bandwidth applications. Use this as the first step when asked
    "what's using my bandwidth?" or when triage shows high throughput with no
    obvious cause.

    For TCP health / slow traffic: use get_application_tcp_health instead.
    For per-client detail on a specific app: use get_application_series with
    the application filter.

    Args:
        router:         Router name (required).
        node:           Node name (required).
        top_n:          Number of top applications to return, sorted by total bytes. Default 10.
        window_minutes: Time window to query. Default 30 minutes.
    """
    buckets = await get_client().get_application_series(router, node, window_minutes)
    summary = _summarize_app_series(buckets)
    top = summary[:top_n]
    return json.dumps(
        {
            "window_minutes": window_minutes,
            "showing": len(top),
            "total_applications": len(summary),
            "applications": [
                {
                    "name": app["name"],
                    "category": app["category"],
                    "rx_bytes": app["rx_bytes"],
                    "tx_bytes": app["tx_bytes"],
                    "active_sessions": app["active_sessions"],
                    "unique_clients": app["unique_clients"],
                    "services": app["services"],
                    "next_hop_types": app["next_hop_types"],
                }
                for app in top
            ],
        },
        indent=2,
    )


@mcp.tool()
async def get_application_tcp_health(
    router: str,
    node: str,
    window_minutes: int = 30,
    min_sessions: int = 5,
) -> str:
    """Get TCP health metrics for active applications, sorted by worst retransmission rate.

    Requires: app-id with 'http' or 'https' mode — check app_id.has_http_https in get_router_info.

    Use this when traffic is reported as slow, laggy, or lossy with no specific
    application or client identified. Returns retransmissions, duplicate ACKs,
    out-of-window events, resets, connection time, and RTT — the signals that
    distinguish WAN loss, uplink loss, and SSR-induced retransmissions.

    Applications with fewer than min_sessions (new + active) are excluded as
    noise. Results are sorted worst-first by total retransmission rate.

    Interpretation:
      tcp_retrans_from_server_pct high → loss on WAN downlink (server→client)
      tcp_retrans_from_client_pct high → loss on LAN or uplink (client→server)
      ssr_retrans_to_* non-zero        → SSR itself is retransmitting (bottleneck)
      avg_fwd_rtt_ms high              → WAN latency to server
      avg_rev_rtt_ms high              → reverse-path latency

    For bandwidth analysis: use get_top_applications instead.
    For per-client detail on a specific app: use get_application_series with
    the application filter.

    Args:
        router:         Router name (required).
        node:           Node name (required).
        window_minutes: Time window to query. Default 30 minutes.
        min_sessions:   Minimum new+active sessions to include an application. Default 5.
    """
    buckets = await get_client().get_application_series(router, node, window_minutes)
    summary = _summarize_app_series(buckets)

    active = [
        a for a in summary
        if (a["new_sessions"] + a["active_sessions"]) >= min_sessions
    ]

    def retrans_rate(app: dict) -> float:
        total_pkts = app["rx_packets"] + app["tx_packets"]
        if not total_pkts:
            return 0.0
        return (app["tcp_retrans_from_server"] + app["tcp_retrans_from_client"]) / total_pkts

    active.sort(key=retrans_rate, reverse=True)

    return json.dumps(
        {
            "window_minutes": window_minutes,
            "application_count": len(active),
            "applications": [
                {
                    "name": app["name"],
                    "category": app["category"],
                    "active_sessions": app["active_sessions"],
                    "new_sessions": app["new_sessions"],
                    "failed_sessions": app["failed_sessions"],
                    "tcp_retrans_from_server": app["tcp_retrans_from_server"],
                    "tcp_retrans_from_server_pct": app["tcp_retrans_from_server_pct"],
                    "tcp_retrans_from_client": app["tcp_retrans_from_client"],
                    "tcp_retrans_from_client_pct": app["tcp_retrans_from_client_pct"],
                    "ssr_retrans_to_client": app["ssr_retrans_to_client"],
                    "ssr_retrans_to_server": app["ssr_retrans_to_server"],
                    "dup_acks_fwd": app["dup_acks_fwd"],
                    "dup_acks_rev": app["dup_acks_rev"],
                    "out_of_window_fwd": app["out_of_window_fwd"],
                    "out_of_window_rev": app["out_of_window_rev"],
                    "tcp_resets": app["tcp_resets"],
                    "avg_tcp_connection_ms": app["avg_tcp_connection_ms"],
                    "avg_fwd_rtt_ms": app["avg_fwd_rtt_ms"],
                    "avg_rev_rtt_ms": app["avg_rev_rtt_ms"],
                }
                for app in active
            ],
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
async def get_fragmentation_stats(router: str, window_minutes: int = 30) -> str:
    """Get IP fragmentation and reassembly activity for a router over a time window.

    Reports the delta (total_change) for each counter over the window — not the
    cumulative all-time total. total_change = 0 means no events in that period
    regardless of historical totals. Each counter also includes current_rate,
    avg_rate, max_rate (events/second), and trend.

    Use when investigating MTU/MSS-related slowness or packet loss. Three scenarios:

    1. SSR is the MTU constraint (sent.ipv4_dont_fragment_drop.total_change > 0):
       DF-set packets arrived that exceeded the SSR's egress interface MTU. The SSR
       dropped them and sent ICMP Fragmentation Needed. Always a misconfiguration —
       lower the interface MTU config or the upstream sender's MSS.

    2. SSR is fragmenting non-DF traffic (sent.ipv4_packets_fragmented.total_change > 0,
       ipv4_dont_fragment_drop.total_change = 0):
       The SSR is fragmenting packets without DF set — typically UDP from LAN hosts
       whose MTU exceeds the WAN interface MTU (e.g. LAN=1500, WAN=1492). Sub-optimal
       but expected; TCP is protected by enforcedMss, UDP has no MSS negotiation.

    3. Downstream MTU mismatch (all total_change = 0, but ping DF search fails):
       The SSR's configured MTU is optimistic — a downstream hop silently drops
       oversized packets. These counters do NOT fire for this case. Use ping with
       dont_frag=True + binary search on packet size to detect and confirm.

    Key counters (each reports total_change, current_rate, avg_rate, max_rate, trend):
      sent.ipv4_dont_fragment_drop        — SSR dropped DF-set packet (always a problem)
      sent.ipv4_packets_fragmented        — SSR fragmented a non-DF packet (may be expected)
      sent.ipv4_non_fabric_fragments      — fragments on standard IP-routed paths
      sent.ipv4_fabric_fragments          — fragments on SVR paths
      received.successfully_reassembled   — fragmented traffic arriving at SSR
      received.fragment_chains_timeout    — reassembly timed out (~15s); lost fragments
      received.failure_to_reassemble      — fragments collected but reassembly failed

    Args:
        router:         Router name (required).
        window_minutes: How far back to look. Default 30 minutes.
    """
    return json.dumps(await get_client().get_fragmentation_stats(router, window_minutes), indent=2)


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
    CPU%, session count, bandwidth. Summary reports current/min/max/average
    of the raw values. Use avg to distinguish sustained load from transient
    spikes: if current >> avg the reading is a spike; if current ≈ avg and
    both are high the load is sustained and worth acting on.

    Counter metrics (counter=True): values that only ever increase — bytes
    transmitted, packets dropped, TCP retransmissions, fragmentation events.
    Summary reports the rate of change between samples (current rate, avg
    rate, max rate, total change over the window). Use total_change to
    determine whether the condition is active: zero means no events in the
    window regardless of the all-time total; non-zero means it is happening
    now. Counter resets (e.g. after a reboot) are treated as zero rather
    than negative spikes.

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
async def get_source_nat_utilization(router: str, node: str) -> str:
    """Get source NAT pool utilization for a router node.

    STUB: Source NAT pool utilization is not yet exposed via the SSR REST or
    GraphQL APIs. This tool returns an unavailable marker so health check
    workflows can handle it gracefully. When this data becomes available via
    the API, this tool will be updated to return actual utilization.

    Source NAT pool exhaustion causes new sessions requiring NAT to fail
    even when routing and service paths are healthy.

    Args:
        router: Router name (required).
        node:   Node name (required).
    """
    data = await get_client().get_source_nat_utilization(router, node)
    return json.dumps(data, indent=2)


@mcp.tool()
async def get_waypoint_utilization(router: str, node: str) -> str:
    """Get SVR waypoint pool utilization for a router node.

    STUB: Waypoint pool utilization is not yet exposed via the SSR REST or
    GraphQL APIs. This tool returns an unavailable marker so health check
    workflows can handle it gracefully. When this data becomes available via
    the API, this tool will be updated to return actual utilization.

    Waypoint pool exhaustion prevents new SVR sessions from being established
    even when peer paths are up and BFD is healthy.

    Args:
        router: Router name (required).
        node:   Node name (required).
    """
    data = await get_client().get_waypoint_utilization(router, node)
    return json.dumps(data, indent=2)


@mcp.tool()
async def get_idp_status(router: str, node: str) -> str:
    """Get IDP (Intrusion Detection and Prevention) status for a router node.

    Combines IDP engine state, cSRX container health, SPU utilization, and
    detailed IDP traffic statistics into a single call.

    When idpTopology is 'disabled' the node has no IDP configuration — only
    engine and pod fields are returned. When enabled, the full response includes:

      engine — overall engine state:
        current: 'on' when running; securityPackages: signature version,
        accessibility, last/next update; networks: internal network reachability

      pod — cSRX container health:
        podState / dockerState: 'active'/'running' when healthy

      monitoring — SPU resource utilization (only when IDP enabled):
        SpuCPUUtilization, SpuMemoryUtilization, SpuCurrentFlowSession,
        SpuMaxFlowSession

      idp — traffic and flow detail (only when IDP enabled):
        uptime, packets/sec and kbits/sec with peak values, min/max/avg
        latency, flow counts by protocol (current and peak), session counts,
        and active policy name

      stats — cumulative IDP activity counters since last reset (only when
        IDP enabled): attacks.total, attacks.received, attacks.missed,
        packets.dropped, packets.processed, bytes.received, bytes.transmitted.
        attacks.received > 0 confirms the IDP engine is actively detecting
        and responding to threats. Individual event details are forwarded to
        the Mist cloud dashboard; only aggregate counts are available locally.

    Note: the API spells 'accessible' as 'accesible' (one 's') in the
    securityPackages object — this is a known typo in the SSR API.

    Context: router

    Args:
        router: Router name (required).
        node:   Node name (required).
    """
    data = await get_client().get_idp_status(router, node)
    return json.dumps(data, indent=2)


_SECURITY_EVENT_NOISE = frozenset({
    "node", "application", "dst_zone", "src_zone", "elapsed_time",
    "email-from", "email-to", "file", "info", "tls_peer", "msg_id",
    "severity", "threat-score", "in_bytes", "out_bytes",
    "in_packets", "out_packets", "detection_time",
})


def _clean_security_event(event: dict) -> dict:
    data = {k: v for k, v in event.get("data", {}).items() if k not in _SECURITY_EVENT_NOISE}
    return {
        "timestamp": event.get("timestamp"),
        **data,
    }


def _summarize_security_events(events: list) -> dict:
    by_attack: dict = {}
    for event in events:
        d = event.get("data", {})
        key = d.get("attack", "UNKNOWN")
        if key not in by_attack:
            by_attack[key] = {
                "attack": key,
                "cve_id": d.get("cve_id") or None,
                "msg_type": d.get("msg_type"),
                "threat_severity": d.get("threat_severity"),
                "action": d.get("action"),
                "is_alert": d.get("is_alert"),
                "count": 0,
                "src_addrs": set(),
                "dest_addrs": set(),
                "services": set(),
                "tenants": set(),
            }
        entry = by_attack[key]
        entry["count"] += 1
        if d.get("src_addr"):
            entry["src_addrs"].add(d["src_addr"])
        if d.get("dest_addr"):
            entry["dest_addrs"].add(d["dest_addr"])
        if d.get("service_name"):
            entry["services"].add(d["service_name"])
        if d.get("tenant_name"):
            entry["tenants"].add(d["tenant_name"])

    result = []
    for entry in sorted(by_attack.values(), key=lambda x: x["count"], reverse=True):
        result.append({
            **{k: v for k, v in entry.items() if k not in ("src_addrs", "dest_addrs", "services", "tenants")},
            "src_addrs": sorted(entry["src_addrs"]),
            "dest_addrs": sorted(entry["dest_addrs"]),
            "services": sorted(entry["services"]),
            "tenants": sorted(entry["tenants"]),
        })

    timestamps = [e.get("timestamp") for e in events if e.get("timestamp")]
    return {
        "event_count": len(events),
        "time_range": {
            "newest": timestamps[0] if timestamps else None,
            "oldest": timestamps[-1] if timestamps else None,
        },
        "attacks": result,
    }


@mcp.tool()
async def get_security_events(
    router: str,
    node: str,
    limit: int = 100,
    start_time: str | None = None,
    summarize: bool = True,
    subtype: str = "IDP",
) -> str:
    """Retrieve security events from the router's IDP audit log.

    Use get_idp_status for engine health and aggregate attack counts.
    Use this tool when you need to know what attacks occurred, against which
    hosts, from which sources, and whether they were blocked or just logged.

    Events are returned newest-first. start_time limits results to events
    that occurred at or after that timestamp (ISO 8601 UTC, e.g.
    '2026-05-11T12:00:00Z'). Without start_time, the most recent `limit`
    events are returned regardless of age.

    summarize=True (default) groups events by attack type and returns counts,
    unique source/dest IPs, severity, and action — useful for a quick overview.
    summarize=False returns one cleaned record per event with these fields:
      timestamp       — UTC time of detection
      attack          — signature or anomaly name
      cve_id          — associated CVE if known
      threat_severity — CRITICAL / HIGH / MEDIUM / LOW / INFO
      action          — CLOSE (session torn down) / DROP / ALERT
      is_alert        — true = logged only; false = actively blocked
      msg_type        — SIG (signature match) or ANOMALY (protocol anomaly)
      src_addr / src_port / src_interface — attack source
      dest_addr / dest_port / dest_interface — targeted host
      protocol        — TCP / UDP / ICMP
      tenant_name     — SSR tenant the traffic belongs to
      service_name    — SSR service matched
      session_id      — SSR session identifier
      repeat_count    — number of times this attack repeated in the session
      url             — URL targeted (populated for some HTTP attacks)

    Context: router

    Args:
        router:     Router name (required).
        node:       Node name (required).
        limit:      Maximum number of events to fetch (default 100).
        start_time: ISO 8601 UTC lower bound (e.g. '2026-05-11T12:00:00Z').
                    Events before this time are excluded.
        summarize:  True (default) = grouped summary by attack type.
                    False = one cleaned record per event.
        subtype:    Event subtype filter (default 'IDP').
    """
    data = await get_client().get_security_events(
        router, node, limit=limit, subtype=subtype, start_time=start_time
    )
    if summarize:
        return json.dumps(_summarize_security_events(data), indent=2)
    return json.dumps([_clean_security_event(e) for e in data], indent=2)


@mcp.tool()
async def get_mist_info(router: str, node: str) -> str:
    """Return Mist/cloud management details for a router managed by Juniper Mist.

    Includes cloud connection status, agent assignment, org/site IDs, device ID,
    config health, interface roles, telemetry state, pushed cloud config (port
    config, network, routing/service policies, VPN, DHCP, BGP, OSPF), and agent
    runtime state. Sensitive fields (SSH authorized keys, Artifactory credentials,
    root password hash) are stripped before returning.

    Use this for deeper analysis of cloud-managed routers: verifying the device is
    correctly assigned, checking which org/site it belongs to, inspecting the
    cloud-pushed config, or diagnosing agent/connectivity issues with Mist.

    Context: router (router-cloud mode)

    Args:
        router: Router name (the UUID/MAC-based name from get_connection_info).
        node:   Node name (from get_connection_info).
    """
    data = await get_client().get_mist_info(router, node)
    return json.dumps(data, indent=2)


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
# Prompts
# ------------------------------------------------------------------


@mcp.prompt()
def troubleshoot_traffic(
    router: str | None = None,
    source: str | None = None,
    destination: str | None = None,
) -> str:
    """Guided traffic troubleshooting for a specific router.

    Provide as much detail as you have — all parameters are optional.

    Args:
        router: Router name. In conductor mode, required; in router mode, defaults
            to the connected router.
        source: Where traffic originates — IP address, hostname, device description,
            physical interface name, or network interface name.
        destination: Where traffic is headed — IP address, service name
            ("internet", "corporate-vpn"), application name ("Teams", "Zoom"),
            or domain name.
    """
    parts = []
    if router:
        parts.append(f"router `{router}`")
    if source:
        parts.append(f"source `{source}`")
    if destination:
        parts.append(f"destination `{destination}`")
    scope_desc = ", ".join(parts) if parts else "this SSR deployment"
    intro = f"Troubleshoot a traffic problem on {scope_desc}."

    router_step = (
        f"The target router is `{router}`. Skip straight to `get_router_info` for it."
        if router
        else (
            "- **conductor mode** → ask the user which router to investigate if not "
            "already clear from context.\n"
            "- **router mode** → use the router and node from `get_connection_info`."
        )
    )

    source_hint = (
        f"\n\nThe user identified the source as `{source}`. Use Step 4 to resolve "
        "it to a concrete IP and network interface name before calling `fib_lookup`."
        if source
        else ""
    )

    destination_hint = (
        f"\n\nThe user identified the destination as `{destination}`. Use Step 5 to "
        "resolve it to a concrete IP, port, and protocol (or service name) before "
        "calling `fib_lookup`."
        if destination
        else ""
    )

    have_details = source or destination

    triage_branch = (
        (
            "You have source and/or destination details. Proceed to Step 4 to resolve "
            "them, then Step 6 (FIB lookup)."
        )
        if have_details
        else (
            "No source or destination details are available. Call `get_dropped_packets` "
            "(unfiltered, router + node) with the default duration, then go to Step 3b "
            "before proceeding to Step 7."
        )
    )

    return f"""{intro}
{source_hint}{destination_hint}

## Step 1 — Log the query

Call `begin_query` with a one-sentence description of what the user asked (e.g.
"Why can't the user at 10.1.2.3 reach Teams?" or "Internet traffic broken on router X").

Throughout this workflow: if any tool returns a result that looks wrong or inconsistent
— a field that should have a value returning empty or `"unknown"`, a response shape
that doesn't match the documented format, or data that contradicts another tool's
output — call `report_issue` to log it before continuing. If the user expresses
dissatisfaction with any result or analysis ("that's not right", "you missed X",
"I expected Y"), call `report_feedback` immediately with their complaint, your
self-assessment of what went wrong, and a brief summary of what tools were called
and what they returned at that point.

## Step 2 — Establish context

Call `get_connection_info`. Note the mode, router name, and node name.

{router_step}

Call `get_router_info` for the target router. Record:
- Node name(s) — required by most tools.
- `has_module` and `has_http_https` — determines which app-id tools are available.

## Step 3 — Initial triage branch

{triage_branch}

## Step 3b — Interpret dropped-packet result

After any `get_dropped_packets` call (Branch A or Step 6):

**If total_dropped is 0 or very low (< 5):**
Tell the user you collected few or no drops and ask whether to try again with a longer
duration (e.g. 30 seconds). Wait for their answer before continuing.

**If `top_source_ips` or `top_destinations` contains `"unknown"`:**
Call `get_dropped_packets` again with `raw=True` (same filters, same duration) to
retrieve the individual events and read the actual IP addresses from the `source.address`
and `destination.address` fields in each event. Use those IPs for all further analysis.

## Step 4 — Resolve source to IP + network interface

Skip this step if you already have a concrete source IP and know which network
interface it enters on.

Match the user-provided source description using the first rule that applies:

| Source description | Resolution |
|--------------------|------------|
| Hostname or device name | `get_dhcp_leases` (filter by hostname), or `get_arp` |
| Physical interface name (e.g. `eth0`, `xe-0/0/1`) | `get_device_interfaces` → find the network interface bound to it |
| Network interface name (e.g. `LAN`, `WAN`) | `get_network_interfaces` — match directly |
| IP address, likely directly connected | `get_network_interfaces` — match source subnet against ethernet-type interfaces |
| IP address, off-network (routed) | `get_rib` LPM lookup → next-hop `interfaceName` is a giid (e.g. `g12`) → match number against `globalId` in `get_network_interfaces` |
| Unknown | Ask the user for the source IP or interface before continuing |

Once resolved, note the **source IP** and **network interface name** for the FIB lookup.

## Step 5 — Resolve destination to IP/port/protocol or service name

Skip this step if you already have a concrete destination IP, port, and protocol.

Match the user-provided destination description using the first rule that applies:

| Destination description | Resolution |
|-------------------------|------------|
| Named service (e.g. "internet", "corporate-vpn") | `list_services` to confirm the service name, then `list_service_paths` to check path health; also do FIB lookup if you have source details |
| Application name (e.g. "Teams", "Zoom") and `has_http_https` is true | `get_app_id_cache` (summarize=False) filtered client-side by application name → extract dest IP, port, protocol for FIB lookup; if cache is empty fall back to `get_dropped_packets` filtered by the known source IP |
| Application name and `has_http_https` is false | App-id not active — ask user for destination IP and port |
| Domain name and `has_http_https` is true | `app_id_lookup` in domain mode |
| Domain name and `has_http_https` is false | Ask user for destination IP and port |
| IP address | Use directly |
| Unknown | Fall back: call `get_dropped_packets` (filtered by source IP if known) and skip the FIB lookup — go straight to Step 7 |

Once resolved, note the **destination IP**, **port**, and **protocol** for the FIB lookup.

## Step 6 — FIB lookup and session check

### 6a — FIB lookup

Call `fib_lookup` with:
- `router` and `node` from Step 2.
- `destination_ip` from Step 5.
- `tenant` if known; otherwise `source_ip` + `source_interface` from Step 4 (preferred
  when tenant is unknown — SSR will derive the tenant).
- `port` and `protocol` if known.

**Branch on result:**

#### No FIB result (FIB miss)

The SSR has no route for this traffic. It sends an ICMP network unreachable reply.
**FIB misses do not appear in `get_dropped_packets`** — do not call it here.

Likely causes: missing service definition, incorrect tenant membership, or the source
IP is not classified into the expected tenant. Check `list_tenant_members` for the
resolved tenant (or ask the user which tenant should apply) and `list_services` to
confirm the service exists and covers the destination.

Skip to Step 7.

#### FIB result with 0 next-hops

A matching service exists but has no viable paths. The SSR will drop packets.
Call `get_dropped_packets` (router + node, filtered by source IP if known) to
confirm active drops.

Also call `list_service_paths` for the matched service and `list_peer_paths` (router)
to identify which paths are down and why.

Skip to Step 7.

#### FIB result with 1+ next-hops

The FIB entry is healthy. Call `get_sessions` (router + node, filtered by source IP
and/or destination IP) to check whether sessions are actually establishing.

**If sessions are present and active:** The SSR is forwarding traffic. Call
`list_service_paths` and `list_peer_paths` (router) to verify path health. If those
are healthy too, the problem is likely outside the SSR (upstream/downstream device,
firewall, server). Report this clearly.

**If no sessions are found:** Traffic may not be reaching the router, or sessions are
being established and torn down immediately. Call `get_dropped_packets` (router + node,
filtered by source IP if known) to check for active drops in the service area.

## Step 7 — Report

Open with a one-sentence verdict stating where the problem lies (or confirming no
SSR-side problem was found).

Then report findings section by section. Omit sections where nothing was found.

### Dropped Packets
List drop reasons and counts. Group by reason code. Flag high-volume drops prominently.
Report actual source and destination IPs if available (from raw events). If addresses
still show as `unknown` after a raw call, note that the SSR did not capture them for
this traffic type.

### FIB Lookup
State the result: miss, 0 next-hops, or match with N next-hops. Include the matched
service name and tenant if present.

### Service / Peer Paths
For each unhealthy path: name, state, and the reported reason.

### Sessions
Whether active sessions were found. If found, include session count and any notable
state (e.g. sessions present but peer paths degraded).

### Conclusion
- If a root cause was found: state it clearly and suggest the likely config fix
  (missing service, bad tenant, down peer path, etc.).
- If no SSR-side cause was found: state explicitly that the SSR is forwarding traffic
  correctly and the fault is likely external.
- Offer to dig deeper into any specific area the user wants to explore.
"""


@mcp.prompt()
def health_check(router: str | None = None) -> str:
    """Run a guided health check.

    Omit router for a conductor-wide triage followed by an offer to drill
    into a specific router. Specify a router name to go straight to a
    single-router deep dive.
    """
    intro = (
        f"Run a health check for router `{router}`."
        if router
        else "Run a health check of this SSR deployment."
    )
    scope_step = (
        f"Go straight to Step 5 (single-router deep dive) for router `{router}`."
        if router
        else (
            "Branch on mode:\n\n"
            "- **conductor mode, no router specified** → proceed to Step 4 "
            "(conductor-wide triage), then stop and wait.\n"
            "- **any router mode** → skip Step 4, go straight to Step 5 using "
            "the router and node from `get_connection_info`."
        )
    )
    return f"""{intro}

## Step 1 — Log the query

Call `begin_query` with a one-sentence description of what the user asked (e.g.
"Health check of the full SSR deployment" or "Health check for router X").

Throughout this workflow: if any tool returns a result that looks wrong or inconsistent
— a field that should have a value returning empty or `"unknown"`, a response shape
that doesn't match the documented format, or data that contradicts another tool's
output — call `report_issue` to log it before continuing. If the user expresses
dissatisfaction with any result or analysis ("that's not right", "you missed X",
"I expected Y"), call `report_feedback` immediately with their complaint, your
self-assessment of what went wrong, and a brief summary of what tools were called
and what they returned at that point.

## Step 2 — Establish context

Call `get_connection_info`. Note the mode, router name, and node name — you will
need them throughout.

## Step 3 — Determine scope

{scope_step}

## Step 4 — Conductor-wide triage

Call `get_conductor_summary`. This returns aggregate counts safe for any deployment
size — do not call list_routers, get_assets, or get_alarms here.

**Report a summary table:**

| Routers | Connected | Disconnected | Total Alarms | Critical | Major |
|---------|-----------|--------------|--------------|----------|-------|

- List any disconnected routers by name. Note if the count was capped (the response
  will say so) and tell the user they can ask for the full list.
- List alarms below the table grouped by router, worst severity first. Note shelved
  alarms separately as operator-acknowledged (not urgent).
- Give a one-sentence overall verdict.
- Identify the single most problematic router (priority: disconnected > most critical
  alarms > most major alarms). Suggest drilling into it by name. **Stop here and
  wait for the user to confirm before proceeding to Step 5.**

## Step 5 — Single-router deep dive

Call `get_router_info` for the target router. This provides node names (needed for
the parallel calls below) and the software version. For HA routers with multiple
nodes, run all node-scoped tools for each node.

Then call **in parallel**:
- `get_system_state` (router + node)
- `get_system_connectivity` (router)
- `get_node_utilization` (router + node)
- `get_session_processor_utilization` (router + node)
- `get_capacity` (router + node)
- `get_idp_status` (router + node)
- `get_resource_allocation` (router + node)
- `get_source_nat_utilization` (router + node)
- `get_waypoint_utilization` (router + node)

If `get_system_state` returns anything other than `RUNNING`, also call
`get_system_processes` (router + node).

## Step 6 — Report

Open with an overall verdict line: **HEALTHY**, **DEGRADED**, or **CRITICAL**.

Report each section below. Omit sections with nothing notable.

### Software State
State value and software version. If not `RUNNING`, list processes not in their
expected state — `highway` and `conflux` are most critical.

### Connectivity
Skip this section entirely if `connectivity` is an empty array (expected for
cloud-managed single-node routers).
Otherwise flag any entry not in `CONNECTED` state. For each flagged entry identify
whether it is a conductor link (management plane) or an HA node-to-node link.

### Node Resources
CPU, memory, and disk per node. If `cpu_high` is non-empty (≥90%), cross-reference
`get_resource_allocation` before investigating: if a `csrx` key is present, parse
`csrx.cores.assignedMask` as a hex bitmask to identify IDP-dedicated core indices
(e.g. `0x4` → bit 2 → core 2). Any `cpu_high` entry whose core index matches the
IDP mask is the cSRX container's dedicated host core — it runs at 100% regardless
of IDP engine load and is expected. Report it in the IDP section rather than as a
CPU alarm and do not call `query_metrics` for it. For remaining high-CPU cores,
use `query_metrics` (counter=False, window_seconds=1800) to verify the load is
sustained — compare `avg` vs `current`. If avg is also high, report as sustained
high CPU. If avg is low, note it as a transient spike and do not alarm. Flag
`disk_high: true` (≥85%) prominently without needing trend confirmation (disk usage
does not spike transiently).

### Service Area
`get_session_processor_utilization` state and per-thread CPU. If state is `High`
or any thread CPU is elevated, use `query_metrics` (counter=False, window_seconds=1800)
on the session processor CPU to confirm the load is sustained before flagging it
as critical. Sustained High service area CPU causes session drops and is a serious
finding; a transient spike is not.

### Capacity Pools
Utilization for FIB_TABLE, FLOW_TABLE, ACTION_POOL, SOURCE_TENANT_TABLE, and
ACCESS_POLICY_TABLE. Flag any pool above 80%.

### IDP
Skip this section entirely if `idpTopology == "disabled"`.
Otherwise report: engine state, security package accessibility
(`securityPackages.accesible`), network reachability (`networks[].pingable`), pod
state, and SPU CPU/memory/flow utilization. Flag any failures.

Silently skip `get_source_nat_utilization` and `get_waypoint_utilization` results
if they return `available: false`.
"""


@mcp.prompt()
def troubleshoot_slow_traffic(
    router: str | None = None,
    application: str | None = None,
) -> str:
    """Guided slow-traffic / performance triage for a specific router.

    Use when a user reports that an application or connection feels slow,
    bandwidth is lower than expected, or latency is high — as opposed to
    traffic being completely broken (use troubleshoot_traffic for that).

    The central goal is to determine whether the SSR itself is the source
    of the problem, or whether the problem lies north (WAN/internet) or
    south (LAN) of the SSR — and to name the specific interface or path
    where evidence points.

    Args:
        router: Router name. In conductor mode, required; in router mode,
            defaults to the connected router.
        application: Application the user reports as slow (e.g. "Teams",
            "YouTube", "Zoom"). Case-insensitive substring match against
            get_application_series results.
    """
    parts = []
    if router:
        parts.append(f"router `{router}`")
    if application:
        parts.append(f"application `{application}`")
    scope_desc = ", ".join(parts) if parts else "this SSR deployment"
    intro = f"Troubleshoot a slow-traffic / performance problem on {scope_desc}."

    router_step = (
        f"The target router is `{router}`. Skip straight to `get_router_info` for it."
        if router
        else (
            "- **conductor mode** → ask the user which router to investigate if not "
            "already clear from context.\n"
            "- **router mode** → use the router and node from `get_connection_info`."
        )
    )

    app_hint = (
        f"\n\nThe user identified `{application}` as the slow application. "
        "In Step 5, filter `get_application_series` by this name. If it does not "
        "appear in results, note that no traffic matching that name was seen in the "
        "last 30 minutes and ask whether the application is actively generating "
        "traffic before proceeding with unfiltered analysis."
        if application
        else ""
    )

    return f"""{intro}
{app_hint}

The central goal is to determine whether the SSR itself is the source of the problem,
or whether evidence points north or south of the SSR — and to name the specific
interface or path where that evidence lands.

## Step 1 — Log the query

Call `begin_query` with a one-sentence description of what the user reported
(e.g. "Teams calls are choppy on router X" or "Internet feels slow on lane-ssr400").

Throughout this workflow: if any tool returns a result that looks wrong or inconsistent
— a field that should have a value returning empty or `"unknown"`, a response shape
that doesn't match the documented format, or data that contradicts another tool's
output — call `report_issue` to log it before continuing. If the user expresses
dissatisfaction with any result or analysis, call `report_feedback` immediately.

## Step 2 — Establish context

Call `get_connection_info`. Note the mode, router name, and node name.

{router_step}

Call `get_router_info` for the target router. Record:
- Node name(s) — required by all router-scoped tools.
- `app_id.has_http_https` — determines whether `get_application_series` is available.

## Step 3 — SSR health snapshot

Call **in parallel** (router + node):
- `get_session_processor_utilization` — service area CPU and thread state
- `get_capacity` — FIB, flow table, action pool utilization
- `get_node_utilization` — CPU, memory, disk
- `get_resource_allocation` — core assignment (identifies IDP-dedicated cores)
- `get_source_nat_utilization` — source NAT port pool (skip silently if `available: false`)
- `get_waypoint_utilization` — waypoint port pool (skip silently if `available: false`)

**Interpret:**
- `get_session_processor_utilization`: if state is `High` or any thread CPU is
  elevated, use `query_metrics` (counter=False, window_seconds=1800) on the session
  processor CPU threads to confirm the load is sustained. Compare `avg` vs `current`:
  sustained high (avg also high) means the forwarding plane is genuinely under
  pressure and is a likely cause of slowness; a transient spike is not. Note and
  continue regardless — the SSR may still be showing application symptoms worth
  investigating.
- `get_capacity`: pool exhaustion causes session drops, not gradual slowness — only
  flag if a pool is at or very near 100%. Redirect to `health_check` if so.
- `get_node_utilization`: if `cpu_high` is set, cross-reference `get_resource_allocation`
  first — if a `csrx` key is present, any `cpu_high` core whose index matches
  `csrx.cores.assignedMask` is the IDP dedicated host core and is expected at 100%;
  skip it. For remaining high cores, use `query_metrics` (counter=False,
  window_seconds=1800) to confirm the load is sustained before treating it as a
  contributing factor. Flag `disk_high` without trend confirmation needed.
- Source NAT / waypoint pools: port exhaustion causes failed or hanging sessions
  that can appear as intermittent slowness. Flag if pool is exhausted.

## Step 4 — Universal diagnostics

Run regardless of app-id availability. Call **in parallel** (router + node):
- `get_dropped_packets` — session establishment failures: service area drops, policy
  rejections. A cluster of drops from many source IPs hitting the same service
  suggests resource exhaustion (source NAT, waypoint, session processor). Note:
  these are failed session setups, not mid-stream packet loss.
- `get_network_interfaces` — operational state of all interfaces; flag any that
  are down or degraded.
- `get_device_interfaces` — physical interface health; flag errors, speed/duplex
  mismatches. A half-duplex mismatch causes slow-but-not-broken symptoms.
- `get_alarms` (router) — active alarms may already name the problem directly.
- `query_stats` with `stat_id=/stats/aggregate-session/network-interface/tcp-retransmissions`
  and `parameters=[{{"name": "network-interface", "itemize": true}}]` — which interface
  has active retransmissions. Primary loss-localization signal before app-series is
  run: the interface with the highest count is the first suspect.
- `query_stats` with `stat_id=/stats/aggregate-session/network-interface/tcp-resets-transmitted`
  and `parameters=[{{"name": "network-interface", "itemize": true}}]` — RSTs the SSR
  itself sent. Non-zero means the SSR is actively terminating connections.
- `get_fragmentation_stats` (router only, no node parameter) — delta-based activity
  over the last 30 minutes for seven fragmentation/reassembly counters. The result
  drives Step 7.

Then call `get_bgp_summary` (router + node) **if BGP is configured**:
- All neighbors Established → BGP healthy, move on quickly.
- Any neighbor not Established → flag it. A missing BGP neighbor removes route
  coverage from that peer, which is more likely a broken-traffic cause than
  a slow-traffic cause. Note it and suggest `troubleshoot_traffic` if the user's
  affected traffic depends on routes from that peer.
- Do not attempt to determine whether specific routes are missing — that belongs
  in `troubleshoot_traffic`.

**Interpret Step 4 results:**
- `get_dropped_packets` non-empty → SSR is failing to establish sessions; note
  the service and source pattern. Redirect to `troubleshoot_traffic` if this is
  the primary complaint.
- TCP retransmissions by network-interface → interfaces with non-zero counts are
  where loss is observed. Carry these interface names forward into Step 5.
  These are windowed ~5-second samples — zero at query time doesn't rule out
  intermittent loss, but consistent elevation is a reliable signal.
- TCP resets-transmitted non-zero → SSR is actively terminating connections. Call
  `query_stats tcp-invalid-state-transitions` and `tcp-bad-flag-combinations` by
  network-interface (same parameters) to distinguish the cause:
  - Those stats elevated → SSR responding to malformed TCP (attack traffic, buggy
    clients, or NAT state issues). Not a link-quality problem.
  - Those stats near zero → RSTs from policy enforcement, capacity, or access control.
- Fragmentation (`get_fragmentation_stats`):
  Results show the delta over the last 30 minutes (`total_change`), not cumulative
  totals. Zero means no events in that window regardless of historical counts.
  - `sent.ipv4_dont_fragment_drop.total_change > 0` → SSR is actively dropping
    DF-set packets too large for the outbound MTU. Always a misconfiguration. Carry
    forward to Step 7.
  - `sent.ipv4_packets_fragmented.total_change > 0` (and DF-drop = 0) → SSR is
    actively fragmenting non-DF packets (typically UDP). Sub-optimal but expected.
    Carry forward to Step 7.
  - All total_change = 0 → no fragmentation in the last 30 minutes. A downstream
    MTU black hole is still possible; investigate in Step 7 only if Step 5 reveals
    link-wide retransmissions that Step 6 cannot explain.

**Interface correlation note:** observe which interfaces have problems (down, errors,
alarms, retransmissions). You will use this in Step 5 to check whether the affected
traffic actually traverses those interfaces. An interface problem on a path not used
by the affected traffic should still be reported, but clearly noted as unrelated to
the slow-traffic complaint.

## Step 5 — Application-layer triage

### Branch A: `has_http_https` is true

**Sub-case A1: No specific application named (general slow traffic)**

Call `get_application_tcp_health` (router + node, `window_minutes=30`).
This returns all active applications sorted by worst retransmission rate — compact
and focused on TCP health signals. Use it to identify which applications are
exhibiting the most loss or latency symptoms before pulling any per-client detail.

**TCP health signals to assess:**
- Use `tcp_retrans_from_server_pct` and `tcp_retrans_from_client_pct` to rank
  severity across applications.
- `ssr_retrans_to_client` or `ssr_retrans_to_server` non-zero → SSR is
  retransmitting; correlate with session processor CPU from Step 3.
- `dup_acks_fwd` / `dup_acks_rev` — corroborate the retransmission direction.
- `out_of_window_fwd` — server-side receive buffer exhausted; may indicate
  congestion between SSR and server, or a slow server.
- `avg_tcp_connection_ms` (TTFP) — use for relative latency comparison across
  applications, not as an absolute threshold. Biased downward under loss (failed
  connections excluded from the average).
- `avg_fwd_rtt_ms` / `avg_rev_rtt_ms` — forward and reverse path RTT; elevated
  fwd suggests WAN latency to the server.

**Pattern recognition:**
- **One application elevated, others normal** → application-specific or server/CDN
  issue; drill into that app with `get_application_series` (see below).
- **All applications elevated** → link-wide or SSR-wide problem; check peer path
  metrics in Step 6 and MTU/MSS in Step 7.
- **Retransmissions low but TTFP/RTT elevated uniformly** → latency rather than
  loss; check peer path latency in Step 6.
- **Drops in Step 4 correlate with retransmissions** → SSR is the common thread;
  note source NAT / waypoint exhaustion if drops are from many clients.

**Drill-down for a suspect application:**
Once `get_application_tcp_health` identifies an application with elevated signals,
call `get_application_series` (router + node, `window_minutes=30`,
`application=<name>`) for per-client and per-interface detail. The application
filter keeps the response small.

**IDP note:** if IDP is enabled, the same client appears twice per time bucket —
once under the base service (IDP→WAN leg) and once under the `*-idp*` variant
(LAN→IDP leg). The summarized output deduplicates these automatically.

**Pre-processing — exclude management traffic:**
Entries where `clients[].tenant == "_internal_"` or
`clients[].networkInterface == "controlKniIf"` are SSR management sessions.
Exclude them from performance analysis.

**Identify the two key interfaces from the drill-down result:**

For each application, note:
- `clients[].networkInterface` — where client traffic **enters** the SSR
- `nextHopInterface[].interface` — where the SSR **forwards** traffic onward

These are the localization anchors. Cross-reference with interface problems found
in Step 4: if an interface with errors or alarms matches one of these, that
strengthens the case that the interface is contributing to the problem.

**Determine flow direction from the drill-down result:**

Inspect `clients[].address` and `clients[].tenant`:

- **Outbound flow** (typical): client IP is private RFC1918 (10.x, 172.16–31.x,
  192.168.x), or tenant is a LAN/internal tenant.
  - `tcp_retrans_from_server` elevated → server retransmitting; problem is on or
    beyond **`nextHopInterface[].interface`** (toward the server)
  - `tcp_retrans_from_client` elevated → client retransmitting; problem is on or
    before **`clients[].networkInterface`** (toward the client)

- **Inbound flow** (WAN client → local service): client IP is a public address,
  OR `clients[].tenant` is a WAN-facing tenant (e.g. `wan`), OR service name
  starts with `*Host-Service-`.
  - `tcp_retrans_from_client` elevated → WAN client retransmitting; problem is on
    or beyond **`clients[].networkInterface`** (toward WAN)
  - `tcp_retrans_from_server` elevated → local server retransmitting; problem is on
    or beyond **`nextHopInterface[].interface`** (toward LAN)

Report the suspected interface by name. E.g.: "retransmissions suggest a problem
on or beyond interface `ge-0-3`."

**Sub-case A2: Specific application named**

Call `get_application_series` (router + node, `window_minutes=30`,
`application=<name>`) directly. The filter keeps the response small. Apply the
same flow-direction and interface-correlation analysis described above.

**If results are ambiguous** — retransmissions elevated but not clearly
isolated to one interface, or no traffic found for the specified application:
Call in parallel (same router + node):
- `query_stats` with `stat_id=/stats/aggregate-session/network-interface/tcp-duplicate-acks`
  and `parameters=[{{"name": "network-interface", "itemize": true}}]`
- `query_stats` with `stat_id=/stats/aggregate-session/network-interface/tcp-out-of-window`
  and `parameters=[{{"name": "network-interface", "itemize": true}}]`

These are interface-level TCP health signals independent of application classification:
- `tcp-duplicate-acks` elevated → corroborates retransmission direction on that interface
- `tcp-out-of-window` elevated → receiver buffer pressure; consistent with congestion
  rather than hard loss
Cross-reference with the interface names from Step 4's retransmission result.

**Bandwidth and traffic engineering check:**

Call in parallel (router only — no node parameter needed):
- `query_stats` `/stats/traffic-eng/device-interface/per-traffic-class/schedule-success-bandwidth`
  with `parameters=[{{"name": "traffic-class", "itemize": true}}]`
- `query_stats` `/stats/traffic-eng/device-interface/per-traffic-class/schedule-failure-bandwidth`
  with `parameters=[{{"name": "traffic-class", "itemize": true}}]`

If the previous results are empty (count: 0), try the same paths under
`/stats/traffic-eng/network-interface/...` with `network-interface` itemize — TE may
be configured at the network-interface level instead (rare but valid).

**Interpret:**
- `schedule-failure-bandwidth == 0` for all classes → TE is not actively dropping
  traffic; bandwidth contention is not the cause of slowness. Move on.
- `schedule-failure-bandwidth > 0` for any class → TE is intentionally dropping
  traffic for that class. This is the primary signal that the link is saturated
  and TE is managing contention:
  - `best-effort` drops only → lower-priority traffic being shaped; expected and
    acceptable; real-time application traffic (high/medium/low) is protected.
  - `low` class drops → most user traffic is being impacted; link is congested.
  - `high` or `medium` drops → even prioritized traffic is being squeezed; link is
    severely constrained.
- Compare `schedule-success-bandwidth` vs `schedule-failure-bandwidth` per class
  to quantify what fraction of each class is being dropped.
- Cross-check with `get_device_interfaces` results from Step 4: compare
  `averageBandwidth` (bps) against `state.speed` (Mbps × 1,000,000 bps) to see
  whether the physical interface is near saturation.
- If TE drops are present but `averageBandwidth` is well below physical speed: a
  `transmit-cap` may be configured lower than the physical link rate. This is set
  under `traffic-engineering.transmit-cap` in the device-interface config and creates
  an effective cap independent of the physical interface speed.
- `traffic_classes` from `get_application_series` (`summarize=True`) shows which
  SSR queue slots this router's traffic is using. All traffic in `low` only means
  no TE classification is active in the service config — contention still affects
  all traffic equally rather than prioritizing by class.

**Service path linkage:**
Call `list_service_paths` filtered to the service names identified in the
drill-down results (the `services` field from `get_application_series`). Use
the filter syntax: `'"service_name"="<name>"'`. Do NOT call `list_service_paths`
without a filter. If multiple suspect services were identified, call once per
service in parallel. This confirms path state and identifies whether traffic
uses SVR (`INTER_ROUTER`) or IP forwarding (`PUBLIC`). Proceed to Step 6 if
SVR paths are in use.

For each service path, examine these fields:
- `state` — `"Up"` or `"Down"`. A down path is not forwarding traffic.
- `warning` — if non-null, read it directly; it names the problem (e.g.,
  `"Path link is down"`).
- `meetsSLA` / `prevMeetsSLA` — SLA compliance now vs the preceding interval.
  `meetsSLA: "No"` → path is currently failing configured SLA thresholds.
  `prevMeetsSLA: "No"` with `meetsSLA: "Yes"` → path recently recovered; may
  explain intermittent complaints. Correlate with alarms from Step 4.
- `reachabilityProbeType` / `reachabilityProbes` — if `reachabilityProbeType`
  is not null, the operator configured an ICMP reachability probe on this path.
  Check each probe's `status`:
  - `"up"` → destination is reachable.
  - `"down"` → destination is unreachable via this path; traffic will failover
    to another path if one exists. A probe that recently toggled (path up but
    `prevMeetsSLA: "No"`) suggests flapping.
  When any service path has a probe configured, query these stats in parallel
  (itemize by node for HA pairs) to get actual performance metrics from the probe:
  - `query_stats` `/stats/icmp/reachability-probe/service-routes/latency`
  - `query_stats` `/stats/icmp/reachability-probe/service-routes/jitter`
  - `query_stats` `/stats/icmp/reachability-probe/service-routes/loss`
  These give live RTT (ms), jitter (ms), and loss (%) measured by the probe.
  They aggregate across all probed service routes, so cross-reference with the
  specific probed routes from `list_service_paths` when interpreting results.
  Elevated probe latency/jitter with low loss → WAN latency problem toward the
  probe destination. Elevated loss → link quality issue on that path.
- For `peer` (SVR) paths: `latency`, `jitter`, `loss` are populated per traffic
  class. Elevated values on the specific service's path directly explain slow
  or choppy traffic. Proceed to Step 6 for deeper peer path analysis.

### Branch B: `has_http_https` is false (or specified application not found)

Without application-series data, establish what traffic is doing and look for
convergent signals across multiple tools.

Call **in parallel** (router + node):
- `get_top_sources` — which source IPs consume the most bandwidth; correlate with
  interface problems found in Step 4 (is the heavy traffic on the affected interface?)
- `list_services` — which services carry the most traffic; high session counts on a
  specific service may point to overload or misconfiguration
- `get_sessions` (`summarize=True`) — total active sessions and per-service
  distribution; unexpectedly high counts suggest capacity pressure

**Interpret with Step 4 context:** without app-series to name specific nexthop
interfaces, use `get_top_sources` and `list_services` to infer whether traffic
is flowing through the interface(s) where Step 4 found problems.

If significant drops appear in `get_dropped_packets`: the SSR is rejecting sessions.
If drops are from many source IPs hitting the same service, this points to resource
exhaustion (source NAT, waypoint, or session processor). Redirect to
`troubleshoot_traffic` for direct data-plane investigation.

**TCP health drill-down** — call in parallel (same router + node):
- `query_stats` with `stat_id=/stats/aggregate-session/service/tcp-retransmissions`
  and `parameters=[{{"name": "service", "itemize": true}}]` — which service has the
  most retransmissions; correlate with top-source bandwidth data
- `query_stats` with `stat_id=/stats/aggregate-session/tenant/tcp-retransmissions`
  and `parameters=[{{"name": "tenant", "itemize": true}}]` — which tenant's traffic
  is most impacted
- `query_stats` with `stat_id=/stats/aggregate-session/network-interface/tcp-duplicate-acks`
  and `parameters=[{{"name": "network-interface", "itemize": true}}]` — directional
  corroboration by interface
- `query_stats` with `stat_id=/stats/aggregate-session/network-interface/tcp-out-of-window`
  and `parameters=[{{"name": "network-interface", "itemize": true}}]` — receiver
  buffer pressure; suggests congestion rather than hard packet loss
- `query_stats` with `stat_id=/stats/aggregate-session/service/tcp-resets-transmitted`
  and `parameters=[{{"name": "service", "itemize": true}}]` — which service the SSR
  is actively RST-ing

**Convergent signal:** the strongest conclusion available without app-id is when
multiple signals agree. For example: Step 4 shows retransmissions on `ge-0-3` +
service retransmissions concentrated on one service + that service's top-source
traffic flowing through `ge-0-3` = strong case for a link quality problem on `ge-0-3`
affecting that service.

If `tcp-resets-transmitted` by service is non-zero, call `query_stats
tcp-invalid-state-transitions` and `tcp-bad-flag-combinations` by network-interface
(same itemize parameters) to determine if the SSR is responding to malformed packets.

**Bandwidth and TE check** — run the same TE stat queries as Branch A:
- `query_stats` `/stats/traffic-eng/device-interface/per-traffic-class/schedule-success-bandwidth`
- `query_stats` `/stats/traffic-eng/device-interface/per-traffic-class/schedule-failure-bandwidth`
Both with `parameters=[{{"name": "traffic-class", "itemize": true}}]`. If empty, retry
under `/stats/traffic-eng/network-interface/...`. Interpret using the same rules as
Branch A: `schedule-failure-bandwidth > 0` means TE is actively dropping traffic;
compare `averageBandwidth` from Step 4 against `state.speed × 1,000,000` for saturation.

If the user can provide a specific source IP and destination, suggest running
`troubleshoot_traffic` to follow the exact data-plane path for that flow.

If app-id is supported by the router's software, recommend enabling `has_http_https`
for richer per-application triage in future investigations.

## Step 6 — Peer path investigation

Call `list_peer_paths` (router + node) in either of these situations:
- An SVR (`INTER_ROUTER`) path was identified in Step 5 for the affected traffic
- Step 4 found interface problems and peer paths exist on that interface

For peer paths **carrying the affected traffic** (SVR):
- `loss > 0%` → directly explains elevated retransmissions; strong correlation.
- High `latency` → explains elevated `avg_tcp_connection_ms` (TTFP).
- High `jitter` → causes variable performance; correlates with dup ACKs.
- Path not in active/up state → not carrying traffic.

For peer paths **on the same interface but not carrying this traffic**:
- Loss, latency, or jitter is supporting evidence that the underlying link has
  quality issues — SVR probes see the same impairment as user traffic.
- Important caveat: peer path metrics reflect conditions to the far-end peer, which
  may be geographically distant. Degradation could originate at the far end, not
  the local link. Present as supporting signal, not as proof of local failure.

**If FPM (performance monitoring) is configured** on this router's adjacencies:
`query_stats` provides richer per-service-class and per-protocol breakdown than
the BFD-based metrics in `list_peer_paths`. Call in parallel, itemizing by
`peer-name` and `service-class`:
- `/stats/performance-monitoring/peer-path/latency`
- `/stats/performance-monitoring/peer-path/jitter`
- `/stats/performance-monitoring/peer-path/loss`
- `/stats/performance-monitoring/peer-path/mos`

FPM shows whether degradation is uniform across all traffic classes or concentrated
on a specific class (e.g., `low`-latency traffic degraded while `best-effort` is
fine). `mos` is ×100 in the API (so 450 = MOS 4.5) and directly models perceived
voice/video call quality. FPM applies to SVR paths only — if traffic uses plain IP
forwarding (`PUBLIC` nexthop), FPM stats are not available.

## Step 7 — MTU/MSS investigation

Run this step when **any** of the following is true:
- Step 4 fragmentation stats show `sent.ipv4_dont_fragment_drop > 0`
- Step 4 fragmentation stats show `sent.ipv4_packets_fragmented > 0`
- Step 5 shows a link-wide retransmission pattern (all applications elevated on the
  same nexthop interface) and Step 6 found no peer path quality issue explaining it

Skip this step if none of these conditions are met.

### MTU configuration context

From `get_network_interfaces` results (already available from Step 4), for each WAN or
nexthop interface implicated by the retransmission or fragmentation pattern, record:
- `mtu` — configured interface MTU. Non-SVR TCP MSS clamping is derived from this value.
  If this value is wrong (e.g., 1500 on a PPPoE link that only supports 1492), the SSR
  will clamp MSS to the wrong size for non-SVR traffic.
- `enforcedMss` — `automatic` means the SSR clamps TCP MSS to fit the MTU;
  `disabled` means no clamping (TCP sessions are not protected from oversized segments).

From `list_peer_paths` results (already available from Step 6, if SVR paths are in use),
note the path-discovered `mtu` for SVR paths on the affected interface. SVR MSS clamping
uses this value (not the configured interface MTU). Even with `enforcedMss=automatic`,
verify that the configured interface `mtu` also matches the physical network MTU —
SVR and non-SVR traffic use different MTU sources for MSS clamping.

### Classify the fragmentation scenario

**Scenario 1 — SSR is the MTU constraint (`ipv4_dont_fragment_drop > 0`):**
The SSR received a DF-bit packet too large for the outbound interface MTU and dropped it.
This is always a misconfiguration — the configured `mtu` on the interface does not match
the actual network MTU.
- If `enforcedMss=automatic`: TCP sessions are protected (MSS is clamped), but UDP and
  IP-fragmented traffic are not. DF-drops will appear for UDP payloads exceeding the MTU.
- If `enforcedMss=disabled`: TCP sessions are also affected — large TCP segments are
  dropped because MSS was never negotiated down.
- Common causes: interface `mtu=1500` on a PPPoE link (actual MTU 1492), tunnel interface
  MTU not reduced to account for encapsulation overhead.
- Run the ping DF binary search (see below) to confirm the effective path MTU and
  compare against the configured interface `mtu`.

**Scenario 2 — Unavoidable fragmentation (`ipv4_packets_fragmented > 0`, `ipv4_dont_fragment_drop == 0`):**
The SSR is fragmenting non-DF packets (typically UDP). The SSR is doing all it can —
it cannot negotiate a lower MTU for UDP traffic, so it fragments instead.
- Report as: sub-optimal but expected. Fragmentation increases latency for latency-sensitive
  UDP applications (VoIP, DNS, video). Recommend raising the upstream path MTU or, for
  application control, configuring a smaller UDP packet size at the source.
- Do not characterise this as an SSR failure or misconfiguration.

**Scenario 3 — Possible downstream MTU black hole (fragmentation stats near zero):**
A device between the SSR and the destination is silently dropping large DF-bit packets
without sending ICMP unreachable back to the SSR. The SSR never learns of the drop.
- Trigger: fragmentation stats are zero, but Step 5 shows link-wide retransmission
  elevation that Step 6 cannot explain.
- Run the ping DF binary search (see below) to confirm. If packets above a certain size
  fail while smaller ones succeed, a downstream black hole is present.
- If `enforcedMss=automatic` and TCP retransmissions are elevated: either the configured
  interface `mtu` is higher than the actual path MTU (causing MSS to be clamped to the
  wrong value), or a non-TCP protocol (e.g., UDP) is triggering the symptom.

### Ping DF binary search

Use `ping` with `dont_frag=True` to probe the effective path MTU toward the affected
destination. The `size` parameter is the **payload byte count** only. The full IP packet
size = `size + 28` (20-byte IP header + 8-byte ICMP header).

Use the WAN interface's gateway address or a known far-end host as `host`. Specify the
affected router and node.

**Procedure:**

1. `size=1472` → tests a 1500-byte IP packet. If `reachable=true`: path MTU ≥ 1500.
   Unless DF-drops are already confirmed, MTU is not the problem here — stop.
2. If `reachable=false` (or if DF-drops confirmed): bisect between 576 and 1472.
   - Try `size=1024`. If `reachable=true`: search 1024–1472. If false: search 576–1024.
   - Continue halving the remaining range until the passing and failing sizes differ by ≤ 10.
3. **Effective path MTU = last passing `size` + 28.**

Compare the effective path MTU against the configured `mtu` on the interface.
A lower effective path MTU than configured `mtu` confirms the mismatch and its magnitude.

## Step 8 — Report findings

**Performance verdict** — state which of these best fits, with supporting data:
- SSR is dropping sessions — specify service and likely cause (exhaustion vs policy)
- Problem on interface `<name>` (toward server/nexthop) — retransmission evidence
- Problem on interface `<name>` (toward client/ingress) — retransmission evidence
- SVR peer path degraded — peer name, loss/latency/jitter
- MTU mismatch on interface `<name>` — scenario (SSR MTU constraint / downstream black
  hole), configured MTU vs effective path MTU from ping DF search, MSS enforcement state
- Unavoidable UDP fragmentation on interface `<name>` — SSR fragmenting non-DF traffic;
  sub-optimal but expected; recommend raising upstream path MTU
- WAN interface saturated — `averageBandwidth` near physical speed; which traffic
  classes are dropping (`schedule-failure-bandwidth`); whether a `transmit-cap` is set
- SSR forwarding plane constrained — session processor CPU or capacity pool near 100%
- No clear SSR-side signal — problem may be external; describe what was checked

**Supporting evidence:**
- Affected applications and their retransmission rates (`_pct` fields), or top
  bandwidth consumers and services if app-id unavailable
- Interface problems found in Step 4, and whether traffic traverses them
- Peer path metrics (note: SVR correlation vs supporting evidence)
- SSR resource readings from Step 3

**Recommendations:**
- *SSR drops / resource exhaustion*: run `health_check`; source NAT and waypoint
  pool exhaustion are stubs pending full API support.
- *Interface-localized loss*: check upstream connectivity on that interface.
  If retransmissions are uniformly high across all apps on the same interface,
  MTU/enforced-mss is a possible cause (stub — see below).
- *SVR peer path degraded*: use `ping` to test reachability to the peer's WAN
  address; check `get_system_connectivity` on the peer router.
- *No TE configured*: if contention appears to be a factor, suggest configuring
  traffic engineering with `high`/`medium`/`low`/`best-effort` traffic classes
  so the SSR can prioritize critical applications during congestion (stub —
  transmit-cap and TE configuration not yet fully explored).

**Stubs — mention if relevant, do not investigate further:**
- *MTU / enforced-mss*: uniform retransmissions across all apps on one interface
  may indicate a path MTU black-hole. The SSR's `enforced-mss` setting clamps TCP
  SYN MSS values to prevent this. Suggest investigating enforced-mss configuration
  and using `ping` with the DF bit at varying packet sizes to find the path MTU.
- *Source NAT / waypoint port exhaustion*: if drops show many clients failing on
  the same service, port pool exhaustion is possible. Full API support pending.
"""


# ------------------------------------------------------------------
# explore prompt
# ------------------------------------------------------------------


@mcp.prompt()
def explore(router: str | None = None) -> str:
    """Explore a site: interfaces, connected clients, BGP neighbors, SVR peers,
    applications seen, and top sources.

    Use to get a quick orientation to a router — what is connected, what is
    running, and what traffic is flowing. Not a health check; use health_check
    for fault triage.

    Args:
        router: Router name. In conductor mode, required. In router mode,
            defaults to the connected router.
    """
    intro = (
        f"Explore site `{router}` and produce a summary of its interfaces, "
        "connected clients, BGP neighbors, SVR peers, applications, and top sources."
        if router
        else "Explore this SSR site and produce a summary of its interfaces, "
        "connected clients, BGP neighbors, SVR peers, applications, and top sources."
    )

    router_step = (
        f"The target router is `{router}`. Proceed directly to Step 3."
        if router
        else (
            "- **conductor mode** → if no router is clear from context, ask the user "
            "to specify one before proceeding.\n"
            "- **router mode** → use the router and node from `get_connection_info`."
        )
    )

    return f"""{intro}

## Step 1 — Log the query

Call `begin_query` with a one-sentence description (e.g. "Explore site router-X"
or "Site overview for branch-office-01").

Throughout this workflow: if any tool returns a result that looks wrong or
inconsistent — empty fields that should have values, a response shape that
doesn't match documented format, or data that contradicts another tool's output
— call `report_issue` before continuing. If the user expresses dissatisfaction
with any result, call `report_feedback` immediately.

## Step 2 — Establish context

Call `get_connection_info`. Note the mode, router name, and node name.

{router_step}

## Step 3 — Router info

Call `get_router_info` for the target router. Record:
- Node name(s) — required for all node-scoped tools below.
- Software version.
- Whether the router is HA (multiple nodes). For HA routers, run all
  node-scoped tools against the **primary node only** (the first node listed).

## Step 4 — Gather site data (all in parallel)

Call all of the following in a single parallel batch:

- `get_device_interfaces` (router + node) — physical interface link state
- `get_network_interfaces` (router + node) — logical interfaces with IPs and tenants
- `get_dhcp_leases` (router) — DHCP-assigned clients
- `get_arp` (router + node) — ARP table
- `get_bgp_neighbors` (router) — BGP neighbor details
- `list_peer_paths` (router) — SVR peer path status
- `get_application_names` (router) — application names seen
- `get_top_sources` (router) — top traffic sources

## Step 5 — Produce the site summary

Present the following sections. Omit a section entirely if the data is empty
or not applicable (e.g. no BGP config, no SVR peers).

### Site Summary
One line: router name, software version, single-node or HA (N nodes).

### Interfaces

**Physical** — table from `get_device_interfaces`:

| Interface | Speed | Link |
|-----------|-------|------|

**Logical** — table from `get_network_interfaces` (skip internal/loopback types
if they add noise; include all named interfaces that carry tenant traffic):

| Interface | Address | Tenant | State |
|-----------|---------|--------|-------|

### Connected Clients

Merge `get_dhcp_leases` and `get_arp` into a single table. For each unique IP:
- If the IP appears in both sources, combine into one row (DHCP wins for hostname
  and interface name; ARP contributes the MAC if DHCP doesn't have one).
- If the IP appears only in DHCP, mark source as `DHCP`.
- If the IP appears only in ARP, mark source as `ARP`. For ARP-only rows, resolve
  the interface by matching the ARP entry's `deviceInterface` and `vlan` against
  `get_network_interfaces` data (already fetched in Step 4): find the network
  interface whose `deviceInterface.name` matches and whose `vlan` matches — use
  that network interface's name. If no match, fall back to `deviceInterface (VLAN X)`.

| IP | MAC | Hostname | Interface | Source |
|----|-----|----------|-----------|--------|

Show total counts beneath: e.g. "N clients (X via DHCP, Y ARP-only)".
If the merged table exceeds 30 rows, show the first 30 and note how many were
omitted.

### BGP Neighbors

Table from `get_bgp_neighbors`:

| Neighbor | Peer ASN | State | Prefixes Received |
|----------|----------|-------|-------------------|

Note the total neighbor count and how many are in `Established` state.

### SVR Peers

Table from `list_peer_paths`:

| Peer Router | Interface | Status | Latency |
|-------------|-----------|--------|---------|

Note total path count and how many are up.

### Applications Seen

Bullet list of application names from `get_application_names`. If the list
exceeds 20 entries, show the first 20 and note the total count.

### Top Sources

Table from `get_top_sources`:

| Source IP | Tenant | Sessions | Bytes |
|-----------|--------|----------|-------|
"""


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
