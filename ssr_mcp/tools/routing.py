import collections
import json
import re

from ssr_mcp.core import mcp, _RO, get_client


@mcp.tool(annotations=_RO)
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
          or get_tenant_membership).

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


@mcp.tool(annotations=_RO)
async def get_fib(
    router: str,
    node: str,
    summarize: bool = True,
    vrf: str | None = None,
    ip_prefix: str | None = None,
    tenant: str | None = None,
    service: str | None = None,
    limit: int | None = None,
) -> str:
    """Get the Forwarding Information Base (FIB) for a node — the resolved
    set of prefixes and next-hops the dataplane will actually use.

    By default returns a summary (total entry count plus breakdowns by service
    and by tenant). Pass summarize=False to retrieve raw entries with optional
    filtering.

    Filters (vrf, ip_prefix, tenant, service) can be combined freely. vrf and
    ip_prefix are applied server-side; tenant and service are applied
    client-side after pagination.

    Args:
        router:    Router name (required).
        node:      Node name within that router (required).
        summarize: When True (default), return counts by service and tenant.
                   When False, return raw FIB entries.
        vrf:       (optional) Filter by VRF name.
        ip_prefix: (optional) Filter by IP prefix, e.g. '10.0.0.0/8'.
        tenant:    (optional) Filter entries to a specific tenant name.
        service:   (optional) Filter entries to a specific service name.
        limit:     Max raw entries to return (summarize=False only).
    """
    entries = await get_client().get_fib(
        router, node,
        limit=None if summarize else limit,
        vrf=vrf,
        ip_prefix=ip_prefix,
        tenant=tenant,
        service=service,
    )
    if summarize:
        by_service = dict(collections.Counter(e.get("service", "") for e in entries).most_common())
        by_tenant = dict(collections.Counter(e.get("tenant", "") for e in entries).most_common())
        return json.dumps({"total": len(entries), "by_service": by_service, "by_tenant": by_tenant}, indent=2)
    return json.dumps({"count": len(entries), "fib": entries}, indent=2)


# SSR automatically creates two KNI (kernel network interface) devices that
# bridge the Linux OS networking stack to the SSR forwarding plane.  They
# carry fixed global IDs and appear in RIB next-hops but are not listed in
# get_network_interfaces.
_KNI_GIID_TO_NAME: dict[str, str] = {
    "4294967294": "kni254",  # IPv4 management KNI (all SSR versions)
    "4294967293": "kni253",  # IPv6 management KNI (SSR 7.0+)
}


async def _build_iface_map(router: str) -> tuple[dict[str, str], dict[str, str]]:
    """Return (giid_number→name, name→giid_string) for a router.

    Combines the hardcoded kni254/kni253 entries with live results from
    get_network_interfaces.  Either dict may be incomplete if the API call
    fails; callers should fall back to the raw giid string on a miss.
    """
    giid_to_name: dict[str, str] = dict(_KNI_GIID_TO_NAME)
    name_to_giid: dict[str, str] = {v: f"g{k}" for k, v in _KNI_GIID_TO_NAME.items()}
    try:
        raw = await get_client().get_network_interfaces(router)
        dev_ifaces = (
            raw.get("data", {})
               .get("allRouters", {})
               .get("nodes", [{}])[0]
               .get("nodes", {})
               .get("nodes", [{}])[0]
               .get("deviceInterfaces", {})
               .get("nodes", [])
        )
        for di in dev_ifaces:
            for ni in di.get("networkInterfaces", {}).get("nodes", []):
                gid = ni.get("globalId")
                name = ni.get("name")
                if gid is not None and name:
                    giid_to_name[str(gid)] = name
                    name_to_giid[name] = f"g{gid}"
    except Exception:
        pass
    return giid_to_name, name_to_giid


def _resolve_iface_name(name: str, giid_to_name: dict[str, str]) -> str:
    """Resolve a giid string (e.g. 'g11') to its network-interface name.
    Non-giid names (e.g. 'lo0', 'kni254') are returned unchanged.
    """
    if not name:
        return name
    m = re.match(r"^g(\d+)$", name)
    if m:
        return giid_to_name.get(m.group(1), name)
    return name


def _resolve_entries_ifaces(entries: list, giid_to_name: dict[str, str]) -> list:
    """Replace raw giid interfaceNames in a list of RIB entries in-place."""
    resolved = []
    for entry in entries:
        nhs = entry.get("nextHops")
        if not nhs:
            resolved.append(entry)
            continue
        new_nhs = []
        for nh in nhs:
            iface = nh.get("interfaceName")
            if iface:
                nh = {**nh, "interfaceName": _resolve_iface_name(iface, giid_to_name)}
            new_nhs.append(nh)
        resolved.append({**entry, "nextHops": new_nhs})
    return resolved


def _parse_rib_summary(text: str) -> dict:
    result: dict = {}
    current_af: str | None = None
    current_vrf: str | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("IPv6 Address Family"):
            current_af = "ipv6"
        elif line.startswith("IP Address Family"):
            current_af = "ipv4"
        elif line.startswith("Route Source") and current_af:
            m = re.search(r'\(vrf (\S+)\)', line)
            current_vrf = m.group(1) if m else "default"
            result.setdefault(current_vrf, {}).setdefault(current_af, {})
        elif line.startswith("---") or line.startswith("Totals") or not current_vrf:
            continue
        else:
            parts = line.split()
            if len(parts) >= 2:
                try:
                    result[current_vrf][current_af][parts[0]] = {
                        "routes": int(parts[1]),
                        "fib": int(parts[2]) if len(parts) >= 3 else 0,
                    }
                except (ValueError, IndexError):
                    pass
    return result


@mcp.tool(annotations=_RO)
async def get_rib(
    router: str,
    summarize: bool = True,
    vrf: str | None = None,
    ip: str | None = None,
    next_hop: str | None = None,
    filter: str | None = None,
    sub_command: str | None = None,
    limit: int | None = None,
) -> str:
    """Get routes from the Routing Information Base (RIB) for a router.

    Five modes by parameter:
      - default (summarize=True): route counts by protocol per VRF, all address
        families. One fast call, safe on large BGP tables.
      - ip set: all RIB entries for a prefix, or longest-prefix match for a host
        address. Overrides summarize.
      - next_hop="*": enumerate unique next-hops (interfaces, gateways,
        blackhole) with prefix count and list each, sorted by count descending.
      - next_hop=<value>: prefixes routing via "blackhole", an IP gateway, or an
        interface name (friendly or raw giid, e.g. "ge-0-1" or "g10").
      - summarize=False: raw entries with vrf/filter/sub_command/limit controls.

    nextHops[].interfaceName values are auto-resolved from raw giid to the
    friendly name. For giid/KNI resolution and routing-engine interfaces (lo0,
    BGP-over-SVR loopbacks), call get_guidance(topic="rib").

    Args:
        router:      Router name (required).
        summarize:   Return a route-count summary when True (default).
                     Set False to retrieve raw entries.
        vrf:         (optional) Filter by VRF name (all modes).
        ip:          (optional) Prefix or host address for lookup/LPM.
                     Overrides summarize.
        next_hop:    (optional) "*" for next-hop overview; a specific
                     next-hop value to filter to. Overrides summarize.
        filter:      (optional) Protocol filter string (raw mode only).
        sub_command: (optional) RibSubCommand value (raw mode only).
        limit:       (optional) Max raw entries to return (raw mode only).
    """
    client = get_client()

    if ip is not None:
        entries = await client.get_rib(router, vrf=vrf, ip=ip)
        giid_to_name, _ = await _build_iface_map(router)
        entries = _resolve_entries_ifaces(entries, giid_to_name)
        return json.dumps({"prefix": ip, "count": len(entries), "routes": entries}, indent=2)

    if next_hop == "*":
        giid_to_name, _ = await _build_iface_map(router)
        entries = await client.get_rib(router, vrf=vrf)
        entries = _resolve_entries_ifaces(entries, giid_to_name)
        groups: dict[str, dict] = {}
        for e in entries:
            seen: set[str] = set()
            for nh in (e.get("nextHops") or []):
                if nh.get("blackhole"):
                    key, nh_type = "blackhole", "blackhole"
                elif nh.get("ip"):
                    key, nh_type = nh["ip"], "ip"
                elif nh.get("interfaceName"):
                    key, nh_type = nh["interfaceName"], "interface"
                else:
                    continue
                if key not in seen:
                    seen.add(key)
                    if key not in groups:
                        groups[key] = {"next_hop": key, "type": nh_type, "count": 0, "prefixes": set()}
                    groups[key]["count"] += 1
                    if e.get("prefix"):
                        groups[key]["prefixes"].add(e["prefix"])
        result = sorted(groups.values(), key=lambda x: x["count"], reverse=True)
        for g in result:
            g["prefixes"] = sorted(g["prefixes"])
        return json.dumps({"next_hops": result}, indent=2)

    if next_hop is not None:
        giid_to_name, name_to_giid = await _build_iface_map(router)
        # Accept either the friendly name ("ge-0-1") or raw giid ("g10") as input
        client_next_hop = name_to_giid.get(next_hop, next_hop)
        entries = await client.get_rib(router, vrf=vrf, next_hop=client_next_hop)
        entries = _resolve_entries_ifaces(entries, giid_to_name)
        resolved_nh = _resolve_iface_name(client_next_hop, giid_to_name)
        if resolved_nh.lower() == "blackhole":
            nh_type = "blackhole"
        elif re.match(r"^\d{1,3}(\.\d{1,3}){3}$", resolved_nh) or ":" in resolved_nh:
            nh_type = "ip"
        else:
            nh_type = "interface"
        prefixes = sorted({e["prefix"] for e in entries if e.get("prefix")})
        return json.dumps(
            {"next_hop": resolved_nh, "type": nh_type, "count": len(entries), "prefixes": prefixes},
            indent=2,
        )

    if summarize:
        raw = await client.get_rib_summary(router)
        parsed = _parse_rib_summary(raw.get("data", ""))
        if vrf:
            parsed = {vrf: parsed[vrf]} if vrf in parsed else {}
        return json.dumps({"vrfs": parsed}, indent=2)

    entries = await client.get_rib(router, vrf=vrf, filter=filter, sub_command=sub_command, limit=limit)
    giid_to_name, _ = await _build_iface_map(router)
    entries = _resolve_entries_ifaces(entries, giid_to_name)
    return json.dumps({"count": len(entries), "rib": entries}, indent=2)


@mcp.tool(annotations=_RO)
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


@mcp.tool(annotations=_RO)
async def get_bgp_routes(
    router: str,
    vrf: str = "default",
    address_family: str = "ipv4",
    prefix: str | None = None,
    limit: int = 100,
) -> str:
    """Get the BGP routing table — equivalent to 'show bgp'.

    Returns prefixes with all candidate paths. Each path includes:
    bestpath flag, selection reason, AS path, origin, metric, weight,
    peer ID, and nexthops with hostnames.

    Context: router

    Args:
        router:         Router name (required).
        vrf:            VRF name. Default 'default'.
        address_family: Address family. Default 'ipv4'. Also: 'ipv6'.
        prefix:         (optional) Filter to prefixes containing this string,
                        e.g. "192.168" or "10.0.0.0/8".
        limit:          Max prefixes to return. Default 100. Use 0 for no limit.
    """
    result = await get_client().get_bgp_routes(router, vrf, address_family, prefix, limit)
    return json.dumps(result, indent=2)


@mcp.tool(annotations=_RO)
async def get_bgp_advertised_routes(
    router: str,
    neighbor: str,
    vrf: str = "default",
    address_family: str = "ipv4",
    prefix: str | None = None,
    summarize: bool = True,
    limit: int | None = None,
) -> str:
    """Get BGP routes advertised to a specific neighbor — equivalent to
    'show bgp neighbors <neighbor> advertised-routes'.

    summarize=True (default): returns total route count and prefix counts
    per unique next-hop. Use this when the neighbor has a large route table
    (e.g. a full internet feed). Pass summarize=False for raw route records;
    combine with prefix= or limit= to keep the response manageable.

    Context: router

    Args:
        router:         Router name (required).
        neighbor:       Neighbor IP address (required).
        vrf:            VRF name. Default 'default'.
        address_family: Address family. Default 'ipv4'. Also: 'ipv6'.
        prefix:         (optional) Filter to prefixes containing this string.
        summarize:      True (default): counts by next-hop. False: raw route records.
        limit:          (optional) Max routes to return in summarize=False mode.
    """
    result = await get_client().get_bgp_advertised_routes(
        router, neighbor, vrf, address_family, prefix, limit if not summarize else None
    )
    if summarize:
        routes = result.get("routes", [])
        next_hops: dict[str, int] = {}
        for r in routes:
            nh = r.get("nexthop") or r.get("nextHop") or "unknown"
            next_hops[nh] = next_hops.get(nh, 0) + 1
        return json.dumps({"total_routes": len(routes), "next_hops": next_hops}, indent=2)
    return json.dumps(result, indent=2)


@mcp.tool(annotations=_RO)
async def get_bgp_received_routes(
    router: str,
    neighbor: str,
    vrf: str = "default",
    address_family: str = "ipv4",
    prefix: str | None = None,
    summarize: bool = True,
    limit: int | None = None,
) -> str:
    """Get BGP routes received from a specific neighbor — equivalent to
    'show bgp neighbors <neighbor> received-routes'.

    summarize=True (default): returns total route count and prefix counts
    per unique next-hop. Use this when the neighbor has a large route table
    (e.g. a full internet feed). Pass summarize=False for raw route records;
    combine with prefix= or limit= to keep the response manageable.

    Context: router

    Args:
        router:         Router name (required).
        neighbor:       Neighbor IP address (required).
        vrf:            VRF name. Default 'default'.
        address_family: Address family. Default 'ipv4'. Also: 'ipv6'.
        prefix:         (optional) Filter to prefixes containing this string.
        summarize:      True (default): counts by next-hop. False: raw route records.
        limit:          (optional) Max routes to return in summarize=False mode.
    """
    result = await get_client().get_bgp_received_routes(
        router, neighbor, vrf, address_family, prefix, limit if not summarize else None
    )
    if summarize:
        routes = result.get("routes", [])
        next_hops: dict[str, int] = {}
        for r in routes:
            nh = r.get("nexthop") or r.get("nextHop") or "unknown"
            next_hops[nh] = next_hops.get(nh, 0) + 1
        return json.dumps({"total_routes": len(routes), "next_hops": next_hops}, indent=2)
    return json.dumps(result, indent=2)


@mcp.tool(annotations=_RO)
async def get_bgp_neighbors(
    router: str,
    vrf: str = "default",
    address_family: str = "ipv4",
    neighbor: str | None = None,
) -> str:
    """Get detailed BGP neighbor information — equivalent to 'show bgp neighbors'.

    Call get_bgp_summary first — it shows neighbor states across all address
    families in a compact view. Call this tool only when you need per-neighbor
    detail: capabilities, reset reason, RTT, route-map info, or to distinguish
    SVR vs IP transport neighbors.

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
