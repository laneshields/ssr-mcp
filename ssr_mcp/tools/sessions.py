import json

from ssr_mcp.core import mcp, _RO, get_client


@mcp.tool(annotations=_RO)
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


@mcp.tool(annotations=_RO)
async def trace_session(session_uuid: str) -> str:
    """Trace all legs of an SVR session across the network by UUID.

    SVR (Secure Vector Routing) sessions appear on multiple routers — at
    minimum the ingress router (originating the SVR tunnel) and the egress
    router (terminating it). This tool finds every flow leg associated with
    the UUID so you can see the full end-to-end path.

    Obtain a session UUID first via get_sessions on the router where you
    expect the session to originate, then call this tool to trace it.

    Each result includes _router and _node so you can see which router and
    node each leg is on.

    Context: conductor only — requires visibility into all managed routers.

    Args:
        session_uuid: Session UUID to trace (required). Obtain from get_sessions.

    Note: routers that are offline or unreachable return a connectivity error
    rather than an empty result. These are reported in unreachable_routers.
    """
    result = await get_client().find_sessions(f'"sessionUuid"="{session_uuid}"', 10)
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


@mcp.tool(annotations=_RO)
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
    use get_application_traffic (requires app-id http/https mode).

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


@mcp.tool(annotations=_RO)
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
