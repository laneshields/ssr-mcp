import ipaddress
import json

from ssr_mcp.core import mcp, _RO, get_client


@mcp.tool(annotations=_RO)
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


_SERVICE_DETAIL_FIELDS = frozenset({"access", "transport", "serviceRoutes"})


@mcp.tool(annotations=_RO)
async def list_services(
    router: str,
    node: str | None = None,
    filter: str | None = None,
    detail: bool = False,
) -> str:
    """List configured services with path status and service configuration.

    Use this as the first step when the destination is described by name
    rather than IP (e.g. "the internet", "corporate VPN", "the file server").
    Find the most likely matching service name, then pass it to
    list_service_paths to check whether that service's paths are up.

    By default (detail=False) returns a compact summary per service:
    name, enabled state, type, route type, service policy, prefixes, and
    up/down path counts. Use detail=True only when you need to inspect
    tenant access lists (allowed/denied), port/protocol transport rules,
    or individual service route names — these fields are large and rarely
    needed for initial triage.

    Args:
        router: Router name (required).
        node:   (optional) Limit to a specific node within that router.
        filter: (optional) Filter expression using the same syntax as
                get_sessions. Known filterable field: service_name.
                Examples:
                  '"service_name"="internet"'   - exact match
                  '"service_name"~"lane"'        - contains
        detail: When True, include access lists, transport rules, and service
                route detail. Default False.
    """
    data = await get_client().get_services(router, node, filter)

    if detail:
        return json.dumps(data, indent=2)

    # Strip large config-only fields from each service entry
    for router_node in data.get("data", {}).get("allRouters", {}).get("nodes", []):
        for node_entry in router_node.get("nodes", {}).get("nodes", []):
            node_entry["serviceInfo"] = [
                {k: v for k, v in svc.items() if k not in _SERVICE_DETAIL_FIELDS}
                for svc in node_entry.get("serviceInfo", [])
            ]
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_RO)
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


@mcp.tool(annotations=_RO)
async def get_tenant_membership(
    router: str,
    node: str,
    summarize: bool = True,
    include_global: bool = False,
    tenant: str | None = None,
    network_interface: str | None = None,
    device_interface: str | None = None,
    vlan: int | None = None,
    source_ip: str | None = None,
) -> str:
    """Show tenant membership rules for a node and look up how traffic is
    classified — step 1 of SSR traffic processing (see the begin_query guidance
    traffic-flow section).

    **Modes:**

    - No filter params: list or summarize all membership entries.
    - tenant=: show interfaces and prefixes assigned to that tenant.
    - network_interface= (or device_interface= + vlan=): show all tenants and
      prefixes on that interface.
    - interface + source_ip=: LPM lookup — returns the specific tenant that
      would classify traffic from source_ip arriving on that interface.

    summarize=True (default): groups entries by tenant name listing their
    assigned interfaces and prefixes. Pass summarize=False for a flat list
    of individual entries (projected to tenant, interface, prefix only).

    <invalidTenant> entries are always suppressed. <global> entries are
    suppressed by default (set include_global=True to include them in list
    and summarize modes). For source_ip lookups, <global> is always considered
    as the fallback match regardless of include_global.

    Context: router

    Args:
        router:            Router name (required).
        node:              Node name (required).
        summarize:         True (default): group by tenant. False: flat entry list.
        include_global:    Include <global> tenant entries in list/summarize output.
                           Default False.
        tenant:            (optional) Filter to a specific tenant name.
        network_interface: (optional) Filter by network-interface (logical) name.
        device_interface:  (optional) Filter by device-interface (physical) name.
        vlan:              (optional) VLAN tag; combined with device_interface.
        source_ip:         (optional) Source IP for LPM tenant classification lookup.
                           Requires network_interface or device_interface.
    """
    if source_ip and not network_interface and not device_interface:
        return json.dumps({"error": "source_ip requires network_interface or device_interface"}, indent=2)

    entries = await get_client().get_tenant_members(router, node)

    # Strip <invalidTenant> always
    entries = [e for e in entries if e.get("tenantName") != "<invalidTenant>"]

    # Apply interface filters
    if network_interface:
        entries = [e for e in entries if e.get("networkInterfaceName") == network_interface]
    elif device_interface:
        entries = [e for e in entries if e.get("devicePortName") == device_interface]
        if vlan is not None:
            entries = [e for e in entries if e.get("vlan") == vlan]

    # Apply tenant filter
    if tenant:
        entries = [e for e in entries if e.get("tenantName") == tenant]

    # LPM lookup
    if source_ip:
        try:
            src = ipaddress.ip_address(source_ip)
        except ValueError:
            return json.dumps({"error": f"Invalid source_ip: {source_ip}"}, indent=2)

        # Always include <global> entries in LPM matching as fallback
        candidates = entries  # <invalidTenant> already stripped above
        best_prefix_len = -1
        best_match: dict | None = None
        for e in candidates:
            prefix_str = e.get("sourceIpPrefix")
            if not prefix_str:
                continue
            try:
                net = ipaddress.ip_network(prefix_str, strict=False)
                if src in net and net.prefixlen > best_prefix_len:
                    best_prefix_len = net.prefixlen
                    best_match = e
            except ValueError:
                continue

        if best_match:
            iface = best_match.get("networkInterfaceName") or best_match.get("devicePortName")
            return json.dumps(
                {
                    "match": {
                        "tenant": best_match.get("tenantName"),
                        "prefix": best_match.get("sourceIpPrefix"),
                        "interface": iface,
                    }
                },
                indent=2,
            )
        return json.dumps(
            {
                "match": None,
                "message": (
                    f"No tenant prefix covers {source_ip} on this interface"
                    " — traffic would be assigned to <global> or dropped."
                ),
            },
            indent=2,
        )

    # Suppress <global> for list/summarize unless requested
    if not include_global:
        entries = [e for e in entries if e.get("tenantName") != "<global>"]

    if summarize:
        by_tenant: dict[str, dict] = {}
        for e in entries:
            name = e.get("tenantName", "unknown")
            if name not in by_tenant:
                by_tenant[name] = {"interfaces": [], "prefixes": []}
            iface = e.get("networkInterfaceName") or e.get("devicePortName")
            if iface and iface not in by_tenant[name]["interfaces"]:
                by_tenant[name]["interfaces"].append(iface)
            pfx = e.get("sourceIpPrefix")
            if pfx and pfx not in by_tenant[name]["prefixes"]:
                by_tenant[name]["prefixes"].append(pfx)
        return json.dumps({"total": len(entries), "by_tenant": by_tenant}, indent=2)

    # Raw mode: project to 3 fields only
    slim = [
        {
            "tenant": e.get("tenantName"),
            "interface": e.get("networkInterfaceName") or e.get("devicePortName"),
            "prefix": e.get("sourceIpPrefix"),
        }
        for e in entries
    ]
    return json.dumps({"count": len(slim), "entries": slim}, indent=2)
