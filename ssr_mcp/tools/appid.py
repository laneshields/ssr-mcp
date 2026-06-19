import asyncio
import json
from typing import Literal

from ssr_mcp.core import mcp, _RO, get_client


@mcp.tool(annotations=_RO)
async def get_application_names(
    router: str,
    node: str,
    limit: int | None = None,
) -> str:
    """List named applications with their active session count and number of
    IP tuples resolved for each application name.

    Requires: app-id with 'module' mode — check app_id.has_module in get_router_info.

    For routers with http/https app-id mode, use get_app_id_cache(summarize=True)
    for an equivalent view from HTTP/HTTPS inspection.

    Args:
        router: Router name (required).
        node:   Node name (required).
        limit:  Max entries to return. Omit to return all entries.
    """
    entries = await get_client().get_application_names(router, node, limit)
    return json.dumps({"count": len(entries), "applications": entries}, indent=2)


@mcp.tool(annotations=_RO)
async def get_web_filtering_info(router: str, node: str) -> str:
    """Get web filtering state and the full list of categories for a router node.

    Requires: app-id with 'http' or 'https' mode — check app_id.has_http_https in get_router_info.

    Returns both the enabled/disabled state and the list of known categories in a
    single call.

    Args:
        router: Router name (required).
        node:   Node name (required).
    """
    data = await get_client().get_web_filtering_info(router, node)
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_RO)
async def app_id_address_lookup(
    router: str,
    node: str,
    ip: str,
    port: int,
    protocol: str,
) -> str:
    """Look up the application classification for a destination IP, port, and protocol.
    Returns the application, category, and domain/URL that app-id would resolve.

    Requires: app-id enabled — check app_id.enabled in get_router_info.

    Cache miss behaviour: if the destination has not been seen before, the lookup
    will return no result but will trigger the app-id engine to classify it. Call
    this tool a second time for the same destination and the result should be
    populated from the newly created cache entry.

    Args:
        router:   Router name (required).
        node:     Node name (required).
        ip:       Destination IP address.
        port:     Destination port number.
        protocol: Protocol — 'tcp' or 'udp'.
    """
    data = await get_client().app_id_address_lookup(router, node, ip, port, protocol)
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_RO)
async def app_id_domain_lookup(
    router: str,
    node: str,
    domain: str,
) -> str:
    """Look up the application classification for a domain name or URL.
    Returns the application and category that app-id would resolve.

    Requires: app-id with 'http' or 'https' mode — check app_id.has_http_https in get_router_info.

    Accepts a bare domain name or a full URL:
      domain='www.youtube.com'
      domain='http://192.168.1.5/index.html'

    Cache miss behaviour: if the domain has not been seen before, the lookup
    will return no result but will trigger the app-id engine to classify it. Call
    this tool a second time for the same domain and the result should be
    populated from the newly created cache entry.

    Args:
        router: Router name (required).
        node:   Node name (required).
        domain: Domain name or full URL to look up.
    """
    data = await get_client().app_id_domain_lookup(router, node, domain)
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_RO)
async def get_app_id_cache(
    router: str,
    node: str | None = None,
    application: str | None = None,
    cache: str = "address",
    limit: int = 500,
    summarize: bool = True,
) -> str:
    """Get the application identification cache — application names classified
    by app-id based on traffic through the router.

    Requires: app-id with 'http' or 'https' mode — check app_id.has_http_https in get_router_info.
    For module-mode routers use get_application_names instead.

    Default (summarize=True): returns unique application names merged across
    both the address cache (IP+port+protocol → app) and the domain cache
    (domain name → app), sorted by frequency. Use this to discover what
    applications the router has seen.

    Pass application='Teams' to return raw address-cache entries for a specific
    application — the dest IP, port, and protocol values can be fed into
    fib_lookup to trace connectivity. If no entries are returned, the
    application may not be in the sampled window; try increasing limit, or
    use get_dropped_packets if connectivity is completely broken.

    The address cache may contain tens of thousands of entries on an active
    router; limit=500 is a sample. When summarizing, both address and domain
    caches are each sampled up to this limit.

    Args:
        router:      Router name (required).
        node:        (optional) Limit to a specific node.
        application: (optional) Case-insensitive substring filter. When set,
                     returns raw cache entries matching this application name.
                     Use cache='domain' to search domain entries instead of
                     address entries.
        cache:       Cache type for raw or application-filtered queries.
                     Default 'address' (IP+port+protocol entries).
                     'domain' — resolved domain names.
                     'url'    — full URLs (often unclassified; limited value).
                     Ignored when summarize=True and application is not set.
        limit:       Max entries to fetch per cache. Default 500.
        summarize:   When True (default), return unique application names merged
                     across address and domain caches. When False, return raw
                     entries from the specified cache type.
    """
    client = get_client()
    actual_limit = limit if limit > 0 else None

    if application is not None:
        entries = await client.get_app_id_cache(router, node, cache, actual_limit)
        filtered = [
            e for e in entries
            if application.lower() in (e.get("application") or "").lower()
        ]
        return json.dumps(
            {
                "application_filter": application,
                "cache": cache,
                "entries_sampled": len(entries),
                "count": len(filtered),
                "entries": filtered,
            },
            indent=2,
        )

    if not summarize:
        entries = await client.get_app_id_cache(router, node, cache, actual_limit)
        return json.dumps({"count": len(entries), "app_id_cache": entries}, indent=2)

    addr_entries, domain_entries = await asyncio.gather(
        client.get_app_id_cache(router, node, "address", actual_limit),
        client.get_app_id_cache(router, node, "domain", actual_limit),
    )

    counts: dict[tuple, int] = {}
    for e in addr_entries + domain_entries:
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
            "address_entries_sampled": len(addr_entries),
            "domain_entries_sampled": len(domain_entries),
            "application_count": len(applications),
            "applications": applications,
        },
        indent=2,
    )


# Nexthop-level metric accumulator keys (intermediate totals used to compute averages).
_APP_TCP_KEYS = (
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
# failed_sessions is at the client level (dropped before nexthop assignment),
# not the nexthop level where it is always 0. Tracked separately.
_APP_CLIENT_KEYS = ("failed_sessions",)


def _collect_raw_app_client_stats(
    buckets: list,
    application: str | None = None,
    client_ip: str | None = None,
) -> tuple[dict, dict, dict]:
    """Scan application series buckets into per-(app, client) deduped stats.

    Returns:
        dedup: dict[(app_name, addr)] -> {rx, tx, _APP_TCP_KEYS..., _APP_CLIENT_KEYS...}
        app_meta: dict[app_name] -> {type, category, tenants, services, next_hop_types,
                                      svr_peers, traffic_classes}
        client_meta: dict[addr] -> {tenant, network_interface, applications, services}

    Metrics live at the nextHopInterface level (without roll-up-metrics). The dedup
    dict uses max() per (app, addr) across bucket appearances to handle IDP service
    function chaining (same client IP appearing under two ingressIntf values) without
    double-counting. The true dedup key is clientIp + networkInterface; deduping by
    address alone would merge a legitimately multi-homed client.
    """
    dedup: dict[tuple[str, str], dict] = {}
    app_meta: dict[str, dict] = {}
    client_meta: dict[str, dict] = {}

    for bucket in buckets:
        for entry in bucket.get("value", []):
            name = entry.get("name", "unknown")
            if application and application.lower() not in name.lower():
                continue

            if name not in app_meta:
                app_meta[name] = {
                    "type": entry.get("type"),
                    "category": entry.get("category"),
                    "tenants": set(),
                    "services": set(),
                    "next_hop_types": set(),
                    "svr_peers": set(),
                    "traffic_classes": set(),
                }

            for client in entry.get("clients", []):
                addr = client.get("address", "unknown")
                if client_ip and client_ip.lower() not in addr.lower():
                    continue

                if addr not in client_meta:
                    client_meta[addr] = {
                        "tenant": client.get("tenant"),
                        "network_interface": client.get("networkInterface"),
                        "applications": set(),
                        "services": set(),
                    }
                client_meta[addr]["applications"].add(name)

                if client.get("tenant"):
                    app_meta[name]["tenants"].add(client["tenant"])
                for svc in client.get("services") or []:
                    svc_name = svc if isinstance(svc, str) else svc.get("name", str(svc))
                    app_meta[name]["services"].add(svc_name)
                    client_meta[addr]["services"].add(svc_name)

                client_vals: dict[str, int] = {
                    "failed_sessions": client.get("failedSessions") or 0,
                }

                nh_rx = nh_tx = 0
                tcp: dict[str, int] = {k: 0 for k in _APP_TCP_KEYS}

                for nh in client.get("nextHopInterface") or []:
                    if nh.get("type"):
                        app_meta[name]["next_hop_types"].add(nh["type"])
                    if nh.get("type") == "INTER_ROUTER" and nh.get("peerName"):
                        app_meta[name]["svr_peers"].add(nh["peerName"])
                    if nh.get("trafficClass"):
                        app_meta[name]["traffic_classes"].add(nh["trafficClass"])
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
                    ttfp = nh.get("timeToFirstDataPacketMs")
                    if isinstance(ttfp, dict):
                        for proto in ("TCP", "TLS"):
                            if proto in ttfp:
                                tcp["ttfp_total_ms"] += ttfp[proto].get("total") or 0
                                tcp["ttfp_count"] += ttfp[proto].get("count") or 0
                    fwd_rtt = nh.get("fwdTcpAckRttMs")
                    if isinstance(fwd_rtt, dict) and "TCP" in fwd_rtt:
                        tcp["fwd_rtt_total_ms"] += fwd_rtt["TCP"].get("total") or 0
                        tcp["fwd_rtt_count"] += fwd_rtt["TCP"].get("count") or 0
                    rev_rtt = nh.get("revTcpAckRttMs")
                    if isinstance(rev_rtt, dict) and "TCP" in rev_rtt:
                        tcp["rev_rtt_total_ms"] += rev_rtt["TCP"].get("total") or 0
                        tcp["rev_rtt_count"] += rev_rtt["TCP"].get("count") or 0

                key = (name, addr)
                if key not in dedup:
                    dedup[key] = {"rx": nh_rx, "tx": nh_tx, **tcp, **client_vals}
                else:
                    s = dedup[key]
                    s["rx"] = max(s["rx"], nh_rx)
                    s["tx"] = max(s["tx"], nh_tx)
                    for k in _APP_TCP_KEYS:
                        s[k] = max(s[k], tcp[k])
                    for k in _APP_CLIENT_KEYS:
                        s[k] = max(s[k], client_vals[k])

    return dedup, app_meta, client_meta


def _compute_tcp_averages(tcp_totals: dict) -> dict:
    """Compute retransmission percentages and average RTT/TTFP from raw totals."""
    total_pkts = tcp_totals["rx_packets"] + tcp_totals["tx_packets"]
    return {
        "tcp_retrans_from_server_pct": round(100 * tcp_totals["tcp_retrans_from_server"] / total_pkts, 2) if total_pkts else None,
        "tcp_retrans_from_client_pct": round(100 * tcp_totals["tcp_retrans_from_client"] / total_pkts, 2) if total_pkts else None,
        "avg_tcp_connection_ms": round(tcp_totals["ttfp_total_ms"] / tcp_totals["ttfp_count"]) if tcp_totals["ttfp_count"] else None,
        "avg_fwd_rtt_ms": round(tcp_totals["fwd_rtt_total_ms"] / tcp_totals["fwd_rtt_count"]) if tcp_totals["fwd_rtt_count"] else None,
        "avg_rev_rtt_ms": round(tcp_totals["rev_rtt_total_ms"] / tcp_totals["rev_rtt_count"]) if tcp_totals["rev_rtt_count"] else None,
    }


def _summarize_app_series(
    buckets: list,
    application: str | None = None,
    client_ip: str | None = None,
) -> list:
    """Aggregate application series buckets into a per-application summary.

    Localization semantics for TCP fields:
      tcp_retrans_from_server + dup_acks_fwd high → WAN downlink loss (server→client)
      tcp_retrans_from_client + dup_acks_rev high → LAN or uplink loss (client→server)
      ssr_retrans_to_* non-zero                   → SSR itself is retransmitting
      avg_fwd_rtt_ms high                         → WAN latency (client→server RTT)
      avg_rev_rtt_ms high                         → reverse-path latency (server→client RTT)
    """
    dedup, app_meta, _ = _collect_raw_app_client_stats(buckets, application, client_ip)

    app_clients: dict[str, list[dict]] = {name: [] for name in app_meta}
    app_addrs: dict[str, set[str]] = {name: set() for name in app_meta}
    for (app_name, addr), stats in dedup.items():
        app_clients[app_name].append(stats)
        app_addrs[app_name].add(addr)

    result = []
    all_keys = _APP_TCP_KEYS + _APP_CLIENT_KEYS
    for app_name, meta in app_meta.items():
        client_stats = app_clients[app_name]
        if not client_stats:
            continue
        tcp_totals = {k: sum(s[k] for s in client_stats) for k in all_keys}
        total_rx = sum(s["rx"] for s in client_stats)
        total_tx = sum(s["tx"] for s in client_stats)
        avgs = _compute_tcp_averages(tcp_totals)

        result.append({
            "name": app_name,
            "type": meta["type"],
            "category": meta["category"],
            "active_sessions": tcp_totals["active_sessions"],
            "new_sessions": tcp_totals["new_sessions"],
            "failed_sessions": tcp_totals["failed_sessions"],
            "unique_clients": len(app_addrs[app_name]),
            "clients": sorted(app_addrs[app_name]),
            "tenants": sorted(meta["tenants"]),
            "services": sorted(meta["services"]),
            "next_hop_types": sorted(meta["next_hop_types"]),
            "svr_peers": sorted(meta["svr_peers"]),
            "traffic_classes": sorted(meta["traffic_classes"]),
            "rx_bytes": total_rx,
            "tx_bytes": total_tx,
            "rx_packets": tcp_totals["rx_packets"],
            "tx_packets": tcp_totals["tx_packets"],
            "tcp_retrans_from_server": tcp_totals["tcp_retrans_from_server"],
            "tcp_retrans_from_server_pct": avgs["tcp_retrans_from_server_pct"],
            "tcp_retrans_from_client": tcp_totals["tcp_retrans_from_client"],
            "tcp_retrans_from_client_pct": avgs["tcp_retrans_from_client_pct"],
            "ssr_retrans_to_client": tcp_totals["ssr_retrans_to_client"],
            "ssr_retrans_to_server": tcp_totals["ssr_retrans_to_server"],
            "dup_acks_fwd": tcp_totals["dup_acks_fwd"],
            "dup_acks_rev": tcp_totals["dup_acks_rev"],
            "out_of_window_fwd": tcp_totals["out_of_window_fwd"],
            "out_of_window_rev": tcp_totals["out_of_window_rev"],
            "tcp_resets": tcp_totals["tcp_resets"],
            "avg_tcp_connection_ms": avgs["avg_tcp_connection_ms"],
            "avg_fwd_rtt_ms": avgs["avg_fwd_rtt_ms"],
            "avg_rev_rtt_ms": avgs["avg_rev_rtt_ms"],
        })

    return sorted(result, key=lambda x: x["rx_bytes"] + x["tx_bytes"], reverse=True)


def _summarize_app_series_by_client(
    buckets: list,
    application: str | None = None,
    client_ip: str | None = None,
) -> list:
    """Aggregate application series buckets into a per-client-IP summary.

    Inverts the grouping of _summarize_app_series: groups by client address across
    all matching applications. Use to find which clients are driving traffic or TCP
    health issues, optionally scoped to a specific application.
    """
    dedup, _, client_meta = _collect_raw_app_client_stats(buckets, application, client_ip)

    addr_entries: dict[str, list[dict]] = {addr: [] for addr in client_meta}
    for (_, addr), stats in dedup.items():
        if addr in addr_entries:
            addr_entries[addr].append(stats)

    result = []
    all_keys = _APP_TCP_KEYS + _APP_CLIENT_KEYS
    for addr, meta in client_meta.items():
        client_stats = addr_entries[addr]
        if not client_stats:
            continue
        tcp_totals = {k: sum(s[k] for s in client_stats) for k in all_keys}
        total_rx = sum(s["rx"] for s in client_stats)
        total_tx = sum(s["tx"] for s in client_stats)
        avgs = _compute_tcp_averages(tcp_totals)

        result.append({
            "client_ip": addr,
            "tenant": meta["tenant"],
            "network_interface": meta["network_interface"],
            "applications": sorted(meta["applications"]),
            "services": sorted(meta["services"]),
            "rx_bytes": total_rx,
            "tx_bytes": total_tx,
            "active_sessions": tcp_totals["active_sessions"],
            "new_sessions": tcp_totals["new_sessions"],
            "failed_sessions": tcp_totals["failed_sessions"],
            "rx_packets": tcp_totals["rx_packets"],
            "tx_packets": tcp_totals["tx_packets"],
            "tcp_retrans_from_server": tcp_totals["tcp_retrans_from_server"],
            "tcp_retrans_from_server_pct": avgs["tcp_retrans_from_server_pct"],
            "tcp_retrans_from_client": tcp_totals["tcp_retrans_from_client"],
            "tcp_retrans_from_client_pct": avgs["tcp_retrans_from_client_pct"],
            "ssr_retrans_to_client": tcp_totals["ssr_retrans_to_client"],
            "ssr_retrans_to_server": tcp_totals["ssr_retrans_to_server"],
            "dup_acks_fwd": tcp_totals["dup_acks_fwd"],
            "dup_acks_rev": tcp_totals["dup_acks_rev"],
            "out_of_window_fwd": tcp_totals["out_of_window_fwd"],
            "out_of_window_rev": tcp_totals["out_of_window_rev"],
            "tcp_resets": tcp_totals["tcp_resets"],
            "avg_tcp_connection_ms": avgs["avg_tcp_connection_ms"],
            "avg_fwd_rtt_ms": avgs["avg_fwd_rtt_ms"],
            "avg_rev_rtt_ms": avgs["avg_rev_rtt_ms"],
        })

    return sorted(result, key=lambda x: x["rx_bytes"] + x["tx_bytes"], reverse=True)


@mcp.tool(annotations=_RO)
async def get_application_traffic(
    router: str,
    node: str,
    view: Literal["top", "tcp_health", "clients"] = "top",
    application: str | None = None,
    client_ip: str | None = None,
    top_n: int | None = None,
    min_sessions: int = 5,
    window_minutes: int = 30,
) -> str:
    """Get per-application or per-client traffic data for a router node.

    Requires: app-id with 'http' or 'https' mode — check app_id.has_http_https in get_router_info.

    Views:
      top        — Top applications by traffic volume. Returns: name, bytes, sessions,
                   services, next-hop types, SVR peers. Use for "what's using my bandwidth?"
      tcp_health — Applications sorted by worst retransmission rate. Returns TCP health
                   signals (retransmissions, dup ACKs, out-of-window, resets, RTT, TTFP).
                   Use when traffic is reported slow, laggy, or lossy.
      clients    — Per-client-IP summary. Returns each unique source IP with its traffic
                   and TCP health metrics, across all applications or scoped to one app.
                   Use for "which clients are driving this problem?"

    Filters (can be combined):
      application — case-insensitive substring match on application name.
      client_ip   — substring match on client IP address.

    TCP health interpretation (tcp_health and clients views):
      tcp_retrans_from_server_pct high → WAN downlink loss (server→client direction)
      tcp_retrans_from_client_pct high → LAN or uplink loss (client→server direction)
      ssr_retrans_to_* non-zero        → SSR is the bottleneck (rare)
      avg_fwd_rtt_ms high              → WAN latency to server
      avg_rev_rtt_ms high              → reverse-path latency
      avg_tcp_connection_ms            → mean time-to-first-data-packet (TCP + TLS); biased
                                         toward surviving connections under high-loss conditions.

    services: SSR service name(s) handling this application or client — use with
    list_service_paths to check path health and SVR vs. IP forwarding.
    next_hop_types: PUBLIC = plain IP forwarding; INTER_ROUTER = SVR peer path.
    svr_peers: peer router names for SVR paths — use with list_peer_paths.

    Args:
        router:         Router name (required).
        node:           Node name (required).
        view:           'top' (default), 'tcp_health', or 'clients'.
        application:    Case-insensitive substring filter on application name.
        client_ip:      Substring filter on client IP address.
        top_n:          Max entries to return. Default: 10 for top/clients, 20 for tcp_health.
        min_sessions:   tcp_health view: minimum new+active sessions to include an app. Default 5.
        window_minutes: Time window to query. Default 30 minutes.
    """
    buckets = await get_client().get_application_series(router, node, window_minutes)

    if view == "clients":
        clients_list = _summarize_app_series_by_client(buckets, application, client_ip)
        n = top_n if top_n is not None else 10
        total = len(clients_list)
        clients_list = clients_list[:n]
        return json.dumps(
            {
                "window_minutes": window_minutes,
                "view": "clients",
                "client_count": len(clients_list),
                "total_clients": total,
                "clients": clients_list,
            },
            indent=2,
        )

    summary = _summarize_app_series(buckets, application, client_ip)

    if view == "tcp_health":
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
        n = top_n if top_n is not None else 20
        total = len(active)
        active = active[:n]

        return json.dumps(
            {
                "window_minutes": window_minutes,
                "view": "tcp_health",
                "application_count": len(active),
                "total_applications": total,
                "applications": [
                    {
                        "name": app["name"],
                        "category": app["category"],
                        "services": app["services"],
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

    # view == "top"
    n = top_n if top_n is not None else 10
    total = len(summary)
    top = summary[:n]

    return json.dumps(
        {
            "window_minutes": window_minutes,
            "view": "top",
            "application_count": len(top),
            "total_applications": total,
            "applications": [
                {
                    "name": app["name"],
                    "category": app["category"],
                    "rx_bytes": app["rx_bytes"],
                    "tx_bytes": app["tx_bytes"],
                    "active_sessions": app["active_sessions"],
                    "new_sessions": app["new_sessions"],
                    "unique_clients": app["unique_clients"],
                    "services": app["services"],
                    "next_hop_types": app["next_hop_types"],
                    "svr_peers": app["svr_peers"],
                }
                for app in top
            ],
        },
        indent=2,
    )
