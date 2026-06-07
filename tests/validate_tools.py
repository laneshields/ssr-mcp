#!/usr/bin/env python3
"""
validate_tools.py — live integration test for every SSR MCP tool.

Calls SSRClient and server helpers directly against a real SSR instance.
No MCP host required. Reads credentials from .env in the repo root.

Usage:
    uv run python tests/validate_tools.py
    uv run python tests/validate_tools.py --env-file .env.mist-managed

Set SSR_TEST_ROUTER in .env to pin a managed router for conductor-mode tests
and skip the interactive prompt. If unset, the script discovers connected
routers and asks which one to use, suggesting the first available.

Exit code 0 = all applicable tests passed. Non-zero = one or more failures.

When failures occur the script prints a JSON block containing the test context
(connection mode, SSR versions, enabled features) alongside each error. Include
this block in bug reports or when asking Claude Code to investigate a fix — the
context prevents version-specific assumptions.
"""

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Allow importing from repo root regardless of working directory
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument("--env-file", default=None)
_args, _ = _parser.parse_known_args()
_env_path = Path(_args.env_file) if _args.env_file else Path(__file__).parent.parent / ".env"
load_dotenv(_env_path, override=True)

from ssr_mcp.client import SSRClient
import ssr_mcp.server as _server
from ssr_mcp.server import _extract_dhcp_leases, _parse_node_utilization
from contextlib import contextmanager

# server.py also calls load_dotenv at import time; re-apply to ensure --env-file wins.
load_dotenv(_env_path, override=True)


@contextmanager
def _inject_client(client: SSRClient):
    """Temporarily set the server module's client singleton to our test client.

    Lets us call server-level composite tools (get_router_health, etc.) that
    use get_client() internally, without re-instantiating from env vars.
    """
    old = _server._client
    _server._client = client
    try:
        yield
    finally:
        _server._client = old


# ---------------------------------------------------------------------------
# Skip signal
# ---------------------------------------------------------------------------

class _SkipTest(Exception):
    """Raise inside a test function to signal a conditional skip."""


# ---------------------------------------------------------------------------
# Test context
# ---------------------------------------------------------------------------

@dataclass
class TestContext:
    mode: str            # conductor | router-managed | router-cloud | router-standalone
    router: str          # router name used for all router-scoped tests
    node: str            # node name on that router
    router_version: str  # SSR software version on the test router
    conductor_version: str | None = None  # conductor version (conductor mode only)
    has_app_id: bool = False
    has_module: bool = False
    has_http_https: bool = False
    has_idp: bool = False

    def describe(self) -> str:
        lines = ["Test context:"]
        if self.conductor_version:
            lines.append(f"  Conductor version : {self.conductor_version}")
        lines += [
            f"  Connection mode   : {self.mode}",
            f"  Test router       : {self.router} / {self.node}",
            f"  Router version    : {self.router_version}",
        ]
        if self.has_app_id:
            app_id_detail = f"enabled (module={self.has_module}, http_https={self.has_http_https})"
        else:
            app_id_detail = "disabled"
        lines.append(f"  App-ID            : {app_id_detail}")
        lines.append(f"  IDP               : {'enabled' if self.has_idp else 'disabled'}")
        return "\n".join(lines)

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "router": self.router,
            "node": self.node,
            "router_version": self.router_version,
            "conductor_version": self.conductor_version,
            "has_app_id": self.has_app_id,
            "has_module": self.has_module,
            "has_http_https": self.has_http_https,
            "has_idp": self.has_idp,
        }


# ---------------------------------------------------------------------------
# Test registry
# ---------------------------------------------------------------------------

_TESTS: list[dict] = []


def register(*, modes: list[str] | None = None, requires: list[str] | None = None):
    """Decorator to register a test with optional mode and feature requirements.

    modes:    If set, test only runs when ctx.mode is in this list.
    requires: List of TestContext boolean attribute names that must be True.
              Tests are skipped (not failed) when requirements aren't met.
    """
    def decorator(fn: Callable) -> Callable:
        _TESTS.append({
            "fn": fn,
            "name": fn.__name__.removeprefix("test_"),
            "modes": modes,
            "requires": requires or [],
        })
        return fn
    return decorator


# ---------------------------------------------------------------------------
# Context discovery
# ---------------------------------------------------------------------------

async def discover_context(client: SSRClient) -> TestContext:
    conn = await client.get_connection_info()
    mode = conn["mode"]
    local_router = conn.get("router")
    local_node = conn.get("node")
    conductor_version = conn.get("software_version") if mode == "conductor" else None

    if mode == "conductor":
        test_router, test_node = await _pick_test_router(client, local_router)
    else:
        test_router = local_router
        test_node = local_node

    router_version = await _get_router_version(client, test_router)

    app_id_config = await client.get_app_id_config(test_router)
    if app_id_config:
        modes = app_id_config.get("mode") or []
        has_all = "all" in modes
        has_app_id = bool(modes)
        has_module = has_all or "module" in modes
        has_http_https = has_all or "http" in modes or "https" in modes
    else:
        has_app_id = has_module = has_http_https = False

    has_idp = False
    try:
        idp = await client.get_idp_status(test_router, test_node)
        has_idp = idp.get("engine", {}).get("idpTopology", "disabled") != "disabled"
    except Exception:
        pass

    return TestContext(
        mode=mode,
        router=test_router,
        node=test_node,
        router_version=router_version,
        conductor_version=conductor_version,
        has_app_id=has_app_id,
        has_module=has_module,
        has_http_https=has_http_https,
        has_idp=has_idp,
    )


async def _pick_test_router(client: SSRClient, conductor_name: str | None) -> tuple[str, str]:
    env_router = os.environ.get("SSR_TEST_ROUTER")
    if env_router:
        nodes = await client.get_router_nodes(env_router)
        node = nodes[0]["name"] if isinstance(nodes, list) and nodes else "node0"
        print(f"  SSR_TEST_ROUTER={env_router} / {node}")
        return env_router, node

    # Use assets to find connected managed routers
    assets_raw = await client.get_assets()
    assets = (
        assets_raw.get("data", {})
        .get("allAuthorities", {})
        .get("nodes", [{}])[0]
        .get("assets", [])
    )
    candidates = [
        a["routerName"] for a in assets
        if a.get("routerName")
        and a.get("routerName") != conductor_name
        and a.get("status") not in (None, "disconnected")
    ]

    if not candidates:
        all_routers = await client.get_routers()
        candidates = [
            r["name"] for r in all_routers
            if isinstance(r, dict) and r.get("name") != conductor_name
        ]

    if not candidates:
        raise RuntimeError("No managed routers found. Is the conductor managing any routers?")

    suggestion = candidates[0]
    print(f"\n  SSR_TEST_ROUTER not set.")
    print(f"  Connected managed routers: {', '.join(candidates)}")
    answer = input(f"  Press Enter to use '{suggestion}' or type a router name: ").strip()
    chosen = answer if answer else suggestion

    nodes = await client.get_router_nodes(chosen)
    node = nodes[0]["name"] if isinstance(nodes, list) and nodes else "node0"
    return chosen, node


async def _get_router_version(client: SSRClient, router: str) -> str:
    try:
        state = await client.get_system_state(router)
        nodes = (
            state.get("data", {})
            .get("allRouters", {})
            .get("nodes", [{}])[0]
            .get("nodes", {})
            .get("nodes", [])
        )
        if nodes:
            return nodes[0].get("state", {}).get("softwareVersion") or "unknown"
    except Exception:
        pass
    return "unknown"


# ---------------------------------------------------------------------------
# Tests — connection / identity
# ---------------------------------------------------------------------------

@register()
async def test_get_connection_info(c: SSRClient, ctx: TestContext):
    r = await c.get_connection_info()
    assert "mode" in r

@register()
async def test_get_software_version(c: SSRClient, ctx: TestContext):
    r = await c.get_software_version()
    assert r

@register()
async def test_get_routers(c: SSRClient, ctx: TestContext):
    r = await c.get_routers()
    assert isinstance(r, list) and len(r) > 0
    names = [entry.get("name") for entry in r]
    assert ctx.router in names

@register()
async def test_get_router_health(c: SSRClient, ctx: TestContext):
    with _inject_client(c):
        result = json.loads(await _server.get_router_health(router=ctx.router, node=ctx.node))
    assert "overall_status" in result

# ---------------------------------------------------------------------------
# Tests — system state
# ---------------------------------------------------------------------------

@register()
async def test_get_platform(c: SSRClient, ctx: TestContext):
    r = await c.get_platform(ctx.router, ctx.node)
    assert r

@register()
async def test_get_system_state(c: SSRClient, ctx: TestContext):
    r = await c.get_system_state(ctx.router, ctx.node)
    assert r

@register()
async def test_get_system_services(c: SSRClient, ctx: TestContext):
    r = await c.get_system_services(ctx.router, ctx.node)
    assert isinstance(r, (list, dict))

@register()
async def test_get_system_processes(c: SSRClient, ctx: TestContext):
    r = await c.get_system_processes(ctx.router, ctx.node)
    assert isinstance(r, (list, dict))

@register()
async def test_get_system_connectivity(c: SSRClient, ctx: TestContext):
    r = await c.get_system_connectivity(ctx.router, ctx.node)
    assert isinstance(r, (list, dict))

@register()
async def test_get_node_utilization(c: SSRClient, ctx: TestContext):
    raw = await c.get_node_utilization(ctx.router, ctx.node)
    parsed = _parse_node_utilization(raw)
    assert isinstance(parsed, list)

@register()
async def test_get_session_processor_utilization(c: SSRClient, ctx: TestContext):
    r = await c.get_session_processor_utilization(ctx.router, ctx.node)
    assert isinstance(r, (list, dict))

@register()
async def test_get_resource_allocation(c: SSRClient, ctx: TestContext):
    r = await c.get_resource_allocation(ctx.router, ctx.node)
    assert isinstance(r, (list, dict))

@register()
async def test_get_capacity(c: SSRClient, ctx: TestContext):
    r = await c.get_capacity(ctx.router, ctx.node)
    assert isinstance(r, (list, dict))

# ---------------------------------------------------------------------------
# Tests — interfaces
# ---------------------------------------------------------------------------

@register()
async def test_get_device_interfaces(c: SSRClient, ctx: TestContext):
    r = await c.get_device_interfaces(ctx.router, ctx.node)
    assert isinstance(r, (list, dict))

@register()
async def test_get_network_interfaces(c: SSRClient, ctx: TestContext):
    r = await c.get_network_interfaces(ctx.router, ctx.node)
    assert isinstance(r, (list, dict))

@register()
async def test_get_network_interface_applications(c: SSRClient, ctx: TestContext):
    r = await c.get_network_interface_applications(ctx.router, ctx.node)
    assert isinstance(r, (list, dict))

@register()
async def test_get_dhcp_leases(c: SSRClient, ctx: TestContext):
    raw = await c.get_network_interface_applications(ctx.router, ctx.node)
    leases = _extract_dhcp_leases(raw)
    assert isinstance(leases, list)

@register()
async def test_get_arp(c: SSRClient, ctx: TestContext):
    r = await c.get_arp(ctx.router, ctx.node)
    assert isinstance(r, (list, dict))

# ---------------------------------------------------------------------------
# Tests — routing
# ---------------------------------------------------------------------------

@register()
async def test_get_rib(c: SSRClient, ctx: TestContext):
    # Summary mode via showCommand
    raw = await c.get_rib_summary(ctx.router)
    assert isinstance(raw, dict) and "data" in raw, "get_rib_summary did not return expected shape"

    from ssr_mcp.server import _parse_rib_summary
    parsed = _parse_rib_summary(raw["data"])
    assert parsed, "RIB summary parsed to empty result"
    assert any("ipv4" in af_data for af_data in parsed.values()), "no ipv4 AF in parsed summary"

    # Prefix lookup
    entries = await c.get_rib(ctx.router, ip="0.0.0.0/0")
    assert isinstance(entries, list) and len(entries) > 0, "prefix lookup returned no results"
    assert all(e.get("prefix") == "0.0.0.0/0" for e in entries), "prefix lookup returned wrong prefix"

    # Next-hop analysis: find a non-blackhole interfaceName from the full RIB
    all_entries = await c.get_rib(ctx.router)
    iface_names = [
        nh["interfaceName"]
        for e in all_entries
        for nh in (e.get("nextHops") or [])
        if nh.get("interfaceName") and not nh.get("blackhole")
    ]
    if iface_names:
        target_iface = iface_names[0]
        filtered = await c.get_rib(ctx.router, next_hop=target_iface)
        assert all(
            any(nh.get("interfaceName") == target_iface for nh in (e.get("nextHops") or []))
            for e in filtered
        ), "next_hop interface filter returned entries that don't use that interface"

    # Blackhole filter
    blackhole_entries = await c.get_rib(ctx.router, next_hop="blackhole")
    assert all(
        any(nh.get("blackhole") for nh in (e.get("nextHops") or []))
        for e in blackhole_entries
    ), "blackhole filter returned entries without blackhole next-hop"

    # Next-hop overview (next_hop="*")
    from ssr_mcp.server import get_rib as server_get_rib
    import json as _json
    overview_json = await server_get_rib(router=ctx.router, next_hop="*")
    overview = _json.loads(overview_json)
    assert "next_hops" in overview, "next_hop='*' did not return next_hops key"
    nh_types = {g["type"] for g in overview["next_hops"]}
    assert nh_types, "next-hop overview returned no groups"
    # Counts should be positive and prefixes non-empty
    for g in overview["next_hops"]:
        assert g["count"] > 0
        assert isinstance(g["prefixes"], list) and len(g["prefixes"]) > 0
        assert g["type"] in ("blackhole", "ip", "interface")

@register()
async def test_get_fib(c: SSRClient, ctx: TestContext):
    # Default call: no args — client returns all entries for summary aggregation
    r = await c.get_fib(ctx.router, ctx.node)
    assert isinstance(r, list)

    # Filtered by service: pick the most common service from the full result
    services = [e.get("service") for e in r if e.get("service")]
    if services:
        top_service = max(set(services), key=services.count)
        filtered = await c.get_fib(ctx.router, ctx.node, service=top_service)
        assert all(e.get("service") == top_service for e in filtered), (
            f"service filter returned entries with unexpected service values"
        )

    # Filtered by tenant: same pattern
    tenants = [e.get("tenant") for e in r if e.get("tenant")]
    if tenants:
        top_tenant = max(set(tenants), key=tenants.count)
        filtered = await c.get_fib(ctx.router, ctx.node, tenant=top_tenant)
        assert all(e.get("tenant") == top_tenant for e in filtered), (
            f"tenant filter returned entries with unexpected tenant values"
        )

@register()
async def test_fib_lookup(c: SSRClient, ctx: TestContext):
    # FIB lookup requires tenant or source context — derive parameters from a real session.
    sessions = await c.get_sessions(ctx.router, ctx.node, limit=10)
    target = next(
        (s for s in sessions if s.get("tenant") and s.get("destIp") and s.get("destPort") and s.get("protocol")),
        None,
    )
    if target is None:
        raise _SkipTest("no active sessions with full flow details for FIB lookup")
    # Protocol from sessions may come as a name ("UDP") or numeric string ("17").
    proto_str = str(target["protocol"]).lower()
    proto_map = {"6": "tcp", "17": "udp", "1": "icmp", "58": "icmpv6"}
    protocol = proto_map.get(proto_str, proto_str)
    r = await c.fib_lookup(
        ctx.router, ctx.node,
        dest_ip=target["destIp"],
        dest_port=int(target["destPort"]),
        protocol=protocol,
        tenant=target["tenant"],
    )
    assert isinstance(r, (list, dict))

@register()
async def test_get_vrfs(c: SSRClient, ctx: TestContext):
    r = await c.get_vrfs(ctx.router)
    assert isinstance(r, (list, dict))

# ---------------------------------------------------------------------------
# Tests — services and paths
# ---------------------------------------------------------------------------

@register()
async def test_get_services(c: SSRClient, ctx: TestContext):
    r = await c.get_services(ctx.router)
    assert isinstance(r, (list, dict))

@register()
async def test_get_service_paths(c: SSRClient, ctx: TestContext):
    r = await c.get_service_paths(ctx.router, ctx.node)
    assert isinstance(r, (list, dict))

@register()
async def test_get_peer_paths(c: SSRClient, ctx: TestContext):
    r = await c.get_peer_paths(ctx.router, ctx.node)
    assert isinstance(r, (list, dict))

@register()
async def test_get_tenant_members(c: SSRClient, ctx: TestContext):
    r = await c.get_tenant_members(ctx.router, ctx.node)
    assert isinstance(r, (list, dict))

# ---------------------------------------------------------------------------
# Tests — sessions
# ---------------------------------------------------------------------------

@register()
async def test_get_sessions(c: SSRClient, ctx: TestContext):
    r = await c.get_sessions(ctx.router, ctx.node, limit=5)
    assert isinstance(r, list)

@register()
async def test_get_session(c: SSRClient, ctx: TestContext):
    sessions = await c.get_sessions(ctx.router, ctx.node, limit=1)
    if not sessions:
        raise _SkipTest("no active sessions to look up")
    uuid = sessions[0]["sessionUuid"]
    r = await c.get_session(uuid, ctx.router, ctx.node)
    assert isinstance(r, (list, dict))

@register()
async def test_get_dropped_packets(c: SSRClient, ctx: TestContext):
    with _inject_client(c):
        result = json.loads(await _server.get_dropped_packets(router=ctx.router, node=ctx.node, duration=5))
    assert "total_dropped" in result

@register()
async def test_get_fragmentation_stats(c: SSRClient, ctx: TestContext):
    r = await c.get_fragmentation_stats(ctx.router)
    assert "sent" in r and "received" in r
    assert "ipv4_dont_fragment_drop" in r["sent"]
    assert "ipv4_packets_fragmented" in r["sent"]
    assert "successfully_reassembled" in r["received"]
    assert all(isinstance(v, dict) for v in r["sent"].values())
    assert all(isinstance(v, dict) for v in r["received"].values())
    assert "total_change" in r["sent"]["ipv4_dont_fragment_drop"]

@register()
async def test_get_top_sources(c: SSRClient, ctx: TestContext):
    r = await c.get_top_sources(ctx.router, ctx.node)
    assert isinstance(r, (list, dict))

# ---------------------------------------------------------------------------
# Tests — config, alarms, events
# ---------------------------------------------------------------------------

@register()
async def test_get_running_config(c: SSRClient, ctx: TestContext):
    r = await c.get_running_config(ctx.router)
    assert isinstance(r, (list, dict))

@register()
async def test_get_alarms(c: SSRClient, ctx: TestContext):
    r = await c.get_alarms(ctx.router)
    assert isinstance(r, (list, dict))

@register()
async def test_get_events(c: SSRClient, ctx: TestContext):
    r = await c.get_events(ctx.router, limit=10)
    assert isinstance(r, list)

# ---------------------------------------------------------------------------
# Tests — metrics
# ---------------------------------------------------------------------------

@register()
async def test_query_metrics(c: SSRClient, ctx: TestContext):
    r = await c.query_metrics(
        ctx.router,
        metric_id="/stats/aggregate-session/node/session-count",
        window_seconds=300,
    )
    assert isinstance(r, (list, dict))

@register()
async def test_query_stats(c: SSRClient, ctx: TestContext):
    r = await c.query_stats(
        ctx.router,
        stat_id="/stats/aggregate-session/node/bandwidth",
    )
    assert isinstance(r, (list, dict))

# ---------------------------------------------------------------------------
# Tests — ping
# ---------------------------------------------------------------------------

@register()
async def test_ping(c: SSRClient, ctx: TestContext):
    r = await c.ping(ctx.router, ctx.node, destination_ip="8.8.8.8", count=3)
    assert "packets_sent" in r

# ---------------------------------------------------------------------------
# Tests — BGP
# ---------------------------------------------------------------------------

@register()
async def test_get_bgp_summary(c: SSRClient, ctx: TestContext):
    r = await c.get_bgp_summary(ctx.router)
    assert isinstance(r, (list, dict))

@register()
async def test_get_bgp_neighbors(c: SSRClient, ctx: TestContext):
    r = await c.get_bgp_neighbors(ctx.router)
    assert isinstance(r, (list, dict))

@register()
async def test_get_bgp_routes(c: SSRClient, ctx: TestContext):
    r = await c.get_bgp_routes(ctx.router)
    assert isinstance(r, (list, dict))

def _bgp_neighbor_ips(neighbors: dict) -> list[str]:
    """Extract real BGP neighbor IPs from get_bgp_neighbors response.

    The REST response uses neighbor IPs as top-level keys. _svr_neighbors is
    an internal list added by the client and is not a real BGP peer.
    """
    return [k for k, v in neighbors.items() if k != "_svr_neighbors" and isinstance(v, dict)]

@register()
async def test_get_bgp_advertised_routes(c: SSRClient, ctx: TestContext):
    neighbors = await c.get_bgp_neighbors(ctx.router)
    peer_ips = _bgp_neighbor_ips(neighbors)
    if not peer_ips:
        raise _SkipTest("no BGP neighbors configured")
    r = await c.get_bgp_advertised_routes(ctx.router, neighbor=peer_ips[0])
    assert isinstance(r, (list, dict))

@register()
async def test_get_bgp_received_routes(c: SSRClient, ctx: TestContext):
    neighbors = await c.get_bgp_neighbors(ctx.router)
    peer_ips = _bgp_neighbor_ips(neighbors)
    if not peer_ips:
        raise _SkipTest("no BGP neighbors configured")
    r = await c.get_bgp_received_routes(ctx.router, neighbor=peer_ips[0])
    assert isinstance(r, (list, dict))

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Tests — conductor-only
# ---------------------------------------------------------------------------

@register(modes=["conductor"])
async def test_get_conductor_summary(c: SSRClient, ctx: TestContext):
    r = await c.get_conductor_summary()
    assert isinstance(r, dict)
    assert "routers" in r and "alarms" in r and "conductor" in r
    assert isinstance(r["routers"]["total"], int)
    assert isinstance(r["alarms"]["total"], int)

@register(modes=["conductor"])
async def test_get_assets(c: SSRClient, ctx: TestContext):
    r = await c.get_assets()
    assert isinstance(r, (list, dict))

@register(modes=["conductor"])
async def test_find_sessions(c: SSRClient, ctx: TestContext):
    r = await c.find_sessions(limit_per_router=3)
    assert isinstance(r, (list, dict))

# ---------------------------------------------------------------------------
# Tests — app-id (module mode)
# ---------------------------------------------------------------------------

@register(requires=["has_module"])
async def test_get_app_id_modules(c: SSRClient, ctx: TestContext):
    r = await c.get_app_id_modules(ctx.router, ctx.node)
    assert isinstance(r, (list, dict))

@register(requires=["has_module"])
async def test_get_application_names(c: SSRClient, ctx: TestContext):
    r = await c.get_application_names(ctx.router, ctx.node)
    assert isinstance(r, (list, dict))

@register(requires=["has_app_id"])
async def test_app_id_lookup_address(c: SSRClient, ctx: TestContext):
    r = await c.app_id_lookup(
        ctx.router, ctx.node,
        mode="address", ip="8.8.8.8", port=53, protocol="udp",
    )
    assert isinstance(r, dict)

# ---------------------------------------------------------------------------
# Tests — app-id (http/https mode)
# ---------------------------------------------------------------------------

@register(requires=["has_http_https"])
async def test_app_id_lookup_domain(c: SSRClient, ctx: TestContext):
    # {} is a valid result on cache miss; the lookup triggers classification
    r = await c.app_id_lookup(
        ctx.router, ctx.node,
        mode="domain", domain="www.google.com",
    )
    assert isinstance(r, dict)

@register(requires=["has_http_https"])
async def test_get_web_filtering_state(c: SSRClient, ctx: TestContext):
    r = await c.get_web_filtering_state(ctx.router, ctx.node)
    assert isinstance(r, (list, dict))

@register(requires=["has_http_https"])
async def test_get_app_id_categories(c: SSRClient, ctx: TestContext):
    r = await c.get_app_id_categories(ctx.router, ctx.node)
    assert isinstance(r, (list, dict))

@register(requires=["has_http_https"])
async def test_get_application_series(c: SSRClient, ctx: TestContext):
    r = await c.get_application_series(ctx.router, ctx.node, window_minutes=5)
    assert isinstance(r, (list, dict))

@register(requires=["has_http_https"])
async def test_summarize_app_series_fields(c: SSRClient, ctx: TestContext):
    """Verify _summarize_app_series produces correct fields on real data."""
    from ssr_mcp.server import _summarize_app_series
    buckets = await c.get_application_series(ctx.router, ctx.node, window_minutes=5)
    summary = _summarize_app_series(buckets)
    assert isinstance(summary, list)
    if not summary:
        raise _SkipTest("no application data in window")
    app = summary[0]
    # active_sessions must be present — was always 0 (bug) before fix
    assert "active_sessions" in app, "active_sessions missing from summary"
    # RTT fields must be present (None is acceptable if no TCP data)
    assert "avg_fwd_rtt_ms" in app, "avg_fwd_rtt_ms missing from summary"
    assert "avg_rev_rtt_ms" in app, "avg_rev_rtt_ms missing from summary"
    assert "avg_tcp_connection_ms" in app, "avg_tcp_connection_ms missing from summary"
    # active_sessions should not be universally 0 when there is traffic
    total_sessions = sum(a["active_sessions"] for a in summary)
    new_sessions = sum(a["new_sessions"] for a in summary)
    assert total_sessions > 0 or new_sessions > 0, (
        f"active_sessions={total_sessions} and new_sessions={new_sessions} — "
        "expected at least some session activity with app-id traffic present"
    )

@register(requires=["has_http_https"])
async def test_get_top_applications(c: SSRClient, ctx: TestContext):
    with _inject_client(c):
        result = json.loads(await _server.get_top_applications(
            router=ctx.router, node=ctx.node, top_n=5, window_minutes=5
        ))
    assert "applications" in result
    assert "total_applications" in result
    assert len(result["applications"]) <= 5
    if result["applications"]:
        app = result["applications"][0]
        for field in ("name", "rx_bytes", "tx_bytes", "active_sessions", "unique_clients"):
            assert field in app, f"get_top_applications missing field: {field}"

@register(requires=["has_http_https"])
async def test_get_application_tcp_health(c: SSRClient, ctx: TestContext):
    with _inject_client(c):
        result = json.loads(await _server.get_application_tcp_health(
            router=ctx.router, node=ctx.node, window_minutes=5, min_sessions=1
        ))
    assert "applications" in result
    assert "application_count" in result
    if result["applications"]:
        app = result["applications"][0]
        for field in ("name", "tcp_retrans_from_server_pct", "tcp_retrans_from_client_pct",
                      "avg_tcp_connection_ms", "avg_fwd_rtt_ms", "avg_rev_rtt_ms"):
            assert field in app, f"get_application_tcp_health missing field: {field}"

# ---------------------------------------------------------------------------
# Tests — IDP
# ---------------------------------------------------------------------------

@register(requires=["has_idp"])
async def test_get_idp_status(c: SSRClient, ctx: TestContext):
    r = await c.get_idp_status(ctx.router, ctx.node)
    assert "engine" in r

# ---------------------------------------------------------------------------
# Tests — Mist / cloud-managed
# ---------------------------------------------------------------------------

@register(modes=["router-cloud"])
async def test_get_connection_info_display_name(c: SSRClient, ctx: TestContext):
    r = await c.get_connection_info()
    assert "display_name" in r, "display_name missing from get_connection_info for router-cloud mode"
    assert r["display_name"], "display_name is empty"

@register(modes=["router-cloud"])
async def test_get_mist_info(c: SSRClient, ctx: TestContext):
    r = await c.get_mist_info(ctx.router, ctx.node)
    assert "Name" in r
    assert "Connection" in r
    assert "Config" in r
    cloud = r.get("Config", {}).get("mist", {}).get("cloud", {})
    for sensitive in ("SSH", "Artifactory", "root_password"):
        assert sensitive not in cloud, f"sensitive key '{sensitive}' present in get_mist_info output"

@register(requires=["has_idp"])
async def test_get_security_events(c: SSRClient, ctx: TestContext):
    r = await c.get_security_events(ctx.router, ctx.node, limit=10)
    assert isinstance(r, list)
    if r:
        event = r[0]
        assert "data" in event
        assert "attack" in event["data"]
        assert "timestamp" in event
        # start_time: 1 second after the newest event → should return nothing
        from datetime import datetime, timezone, timedelta
        newest_dt = datetime.fromisoformat(r[0]["timestamp"].replace("Z", "+00:00"))
        after_newest = (newest_dt + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        r2 = await c.get_security_events(ctx.router, ctx.node, limit=10, start_time=after_newest)
        assert isinstance(r2, list)
        assert len(r2) == 0, f"expected 0 events with start_time after newest, got {len(r2)}"
        # start_time: 1 second before the oldest event → should return same or more events
        oldest_dt = datetime.fromisoformat(r[-1]["timestamp"].replace("Z", "+00:00"))
        before_oldest = (oldest_dt - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        r3 = await c.get_security_events(ctx.router, ctx.node, limit=10, start_time=before_oldest)
        assert isinstance(r3, list)
        assert len(r3) >= len(r), f"expected >= {len(r)} events with start_time before oldest, got {len(r3)}"
        assert "threat_severity" in r[0]["data"]

# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_tests(client: SSRClient, ctx: TestContext) -> tuple[dict, list[dict]]:
    counts = {"pass": 0, "fail": 0, "skip": 0}
    failures: list[dict] = []

    for t in _TESTS:
        name = t["name"]

        if t["modes"] and ctx.mode not in t["modes"]:
            print(f"  -  {name}  (skip: requires {'/'.join(t['modes'])} mode)")
            counts["skip"] += 1
            continue

        skipped = False
        for req in t["requires"]:
            if not getattr(ctx, req, False):
                print(f"  -  {name}  (skip: requires {req})")
                counts["skip"] += 1
                skipped = True
                break
        if skipped:
            continue

        try:
            await t["fn"](client, ctx)
            print(f"  ✓  {name}")
            counts["pass"] += 1
        except _SkipTest as e:
            print(f"  -  {name}  (skip: {e})")
            counts["skip"] += 1
        except Exception as e:
            print(f"  ✗  {name}  — {e}")
            counts["fail"] += 1
            failures.append({"test": name, "error": str(e)})

    return counts, failures


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    host = os.environ.get("SSR_HOST")
    username = os.environ.get("SSR_USERNAME", "admin")
    password = os.environ.get("SSR_PASSWORD", "")
    verify_ssl = os.environ.get("SSR_VERIFY_SSL", "true").lower() not in ("false", "0")
    port = int(os.environ.get("SSR_PORT", "443"))

    if not host:
        print("ERROR: SSR_HOST is not set. Copy .env.example to .env and fill in credentials.")
        sys.exit(1)

    client = SSRClient(host, username, password, verify_ssl=verify_ssl, port=port)

    print("Discovering context...")
    try:
        ctx = await discover_context(client)
    except Exception as e:
        print(f"ERROR: {e}")
        await client.close()
        sys.exit(1)

    print(f"\n{ctx.describe()}\n")
    print(f"Running {len(_TESTS)} tests:\n")

    counts, failures = await run_tests(client, ctx)

    print(f"\n{'─' * 48}")
    print(f"  Passed : {counts['pass']}")
    print(f"  Failed : {counts['fail']}")
    print(f"  Skipped: {counts['skip']}")
    print(f"{'─' * 48}")

    if failures:
        print("\nFailed tests — include the block below when reporting bugs or asking for fixes:\n")
        print(json.dumps({"context": ctx.as_dict(), "failures": failures}, indent=2))

    await client.close()
    sys.exit(1 if counts["fail"] else 0)


if __name__ == "__main__":
    asyncio.run(main())
