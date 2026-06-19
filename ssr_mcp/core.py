import functools
import inspect
import json
import os
import pathlib
from datetime import datetime, timezone

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ssr_mcp.client import SSRClient

load_dotenv(pathlib.Path(__file__).parent.parent / ".env", override=True)

# ------------------------------------------------------------------
# HTTP transport configuration
# ------------------------------------------------------------------

_TRANSPORT = os.environ.get("SSR_MCP_TRANSPORT", "stdio")
_HOST = os.environ.get("SSR_MCP_HOST", "127.0.0.1")
_PORT = int(os.environ.get("SSR_MCP_PORT", "8000"))
_AUTH_TOKEN = os.environ.get("SSR_MCP_AUTH_TOKEN")


_GUIDANCE = """# SSR MCP Operational Guidance

## Session startup

1. Call `begin_query` first on every user request — before any other tool.
2. Call `get_connection_info` at the start of a session to determine connection
   mode (conductor, router-managed, router-cloud, router-standalone).
3. Before doing router-specific work, call `get_router_info` and
   `get_router_health` **in parallel** to get node names, software version,
   app-id status, and current health state in a single round trip. Do not
   guess node names.

## Connection modes

| Mode | Description |
|---|---|
| conductor | SSR_HOST is a conductor. All managed routers accessible by name via `router:` param. Conductor-only tools available (`get_assets`, `trace_session`). |
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
- Domain name → `app_id_domain_lookup` (requires `has_http_https`)
- All else fails → `get_dropped_packets`

## Slow traffic investigation

Use this when the user reports slowness, high latency, or degraded throughput
— as opposed to traffic being completely broken (use the connectivity decision
tree for that).

**First: ask which router** if in conductor mode and not already clear from
context. Don't broadcast across all routers — slow traffic is always
router-specific.

**Key tool:**
`get_application_traffic` with three views:
- `view='top'` — bandwidth ranking ("what's using my bandwidth?")
- `view='tcp_health'` — TCP health signals ("what's slow or lossy?")
- `view='clients'` — per-client-IP breakdown ("which clients are the problem?")

For slow traffic, **start with `get_application_traffic(view='tcp_health')`**, not
sessions or top sources. It returns retransmission rates, duplicate ACKs, out-of-window
segments, and RTT per application — the signals that explain *why* traffic is
slow, not just how much there is.

**Step order:**
1. `begin_query` + `get_router_info` (get node name and confirm app-id status)
2. In parallel: `get_session_processor_utilization`, `get_node_utilization`,
   `get_dropped_packets`, `get_network_interfaces`, `get_device_interfaces`,
   `get_alarms`
3. If `has_http_https` true: `get_application_traffic(view='tcp_health', window_minutes=30)`
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

**If app-id is not enabled:** skip `get_application_traffic`; use
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


mcp = FastMCP(
    "SSR — Session Smart Router",
    host=_HOST,
    port=_PORT,
    instructions=_GUIDANCE,
)

_RO = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
_LW = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)


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
