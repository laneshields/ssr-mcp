import json

from ssr_mcp.core import mcp, _RO, get_client


@mcp.tool(annotations=_RO)
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


def _extract_dhcp_servers(data: dict) -> list[dict]:
    result = []
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

                    statuses = {}
                    for item in dhcp_server:
                        for key in ("kea-service-target-status", "kea-status", "kea-ctrl-status"):
                            if key in item:
                                vals = item[key]
                                statuses[key] = vals[0] if isinstance(vals, list) and vals else vals

                    ha = next(
                        (item["ha-heartbeat"] for item in dhcp_server if "ha-heartbeat" in item),
                        None,
                    )

                    metrics_raw = next(
                        (item["metrics"] for item in dhcp_server if "metrics" in item),
                        [],
                    )
                    metrics = {k: v for m in metrics_raw for k, v in m.items()}

                    subnets_raw = next(
                        (item["subnets"] for item in dhcp_server if "subnets" in item),
                        [],
                    )
                    subnets = []
                    for idx, subnet_wrapper in enumerate(subnets_raw, start=1):
                        subnet_data = subnet_wrapper.get("subnet", {})
                        assigned = metrics.get(f"subnet[{idx}].assigned-addresses", subnet_data.get("current-lease-count"))
                        total = metrics.get(f"subnet[{idx}].total-addresses")
                        utilization_pct = round(assigned / total * 100, 1) if total and assigned is not None else None
                        subnets.append({
                            "subnet": subnet_data.get("subnet"),
                            "assigned_addresses": assigned,
                            "total_addresses": total,
                            "utilization_pct": utilization_pct,
                        })

                    entry: dict = {
                        "interface": net_iface.get("name"),
                        "kea_service_target_status": statuses.get("kea-service-target-status"),
                        "kea_status": statuses.get("kea-status"),
                        "kea_ctrl_status": statuses.get("kea-ctrl-status"),
                        "subnets": subnets,
                    }
                    if ha:
                        entry["ha_role"] = ha.get("role")
                        entry["ha_state"] = ha.get("state")
                    result.append(entry)
    return result


@mcp.tool(annotations=_RO)
async def get_dhcp_servers(
    router: str | None = None,
    node: str | None = None,
    network_interface: str | None = None,
) -> str:
    """Get DHCP server health summary for interfaces configured as DHCP servers.

    Returns per-interface Kea service status, per-subnet address pool utilization
    (assigned vs total addresses), and HA role/state when HA is configured.

    Use this to answer "is my DHCP server running?" or "is this subnet running
    out of addresses?". For individual lease records use get_dhcp_leases instead.

    Args:
        router:            (optional) Limit to a specific router.
        node:              (optional) Limit to a specific node.
        network_interface: (optional) Limit to a specific interface by name.
    """
    data = await get_client().get_network_interface_applications(router, node, network_interface)
    servers = _extract_dhcp_servers(data)
    return json.dumps({"dhcp_server_count": len(servers), "interfaces": servers}, indent=2)


@mcp.tool(annotations=_RO)
async def get_dhcp_leases(
    router: str | None = None,
    node: str | None = None,
    network_interface: str | None = None,
    summarize: bool = True,
) -> str:
    """Get active DHCP leases across all DHCP-server interfaces on a router.

    summarize=True (default): returns per-interface and per-subnet lease counts
    only — no individual lease records. Use this to see how many clients are
    on each subnet. Pass summarize=False to get full lease records (IP, hostname,
    MAC, last-seen time, lease lifetime).

    Use this to answer questions like "what devices are on the network?",
    "what IP did host X get?", or "how many clients are connected to home_lan?".

    Args:
        router:            (optional) Limit to a specific router.
        node:              (optional) Limit to a specific node.
        network_interface: (optional) Limit to a single interface by name,
                           e.g. 'home_lan'. Strongly recommended when you
                           know which interface to inspect.
        summarize:         True (default): counts only. False: full lease records.
    """
    data = await get_client().get_network_interface_applications(router, node, network_interface)
    interfaces = _extract_dhcp_leases(data)
    total = sum(i["lease_count"] for i in interfaces)
    if summarize:
        slim = [{"interface": i["interface"], "subnet": i["subnet"], "lease_count": i["lease_count"]} for i in interfaces]
        return json.dumps({"total_leases": total, "interfaces": slim}, indent=2)
    return json.dumps({"total_leases": total, "interfaces": interfaces}, indent=2)


@mcp.tool(annotations=_RO)
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


@mcp.tool(annotations=_RO)
async def get_arp(
    router: str,
    node: str,
    limit: int | None = None,
    summarize: bool = True,
) -> str:
    """Get the ARP table for a node.

    summarize=True (default): returns total entry count grouped by device
    interface with per-state breakdowns (REACHABLE, STALE, etc.). Pass
    summarize=False to get individual ARP records.

    Args:
        router:    Router name (required).
        node:      Node name (required).
        limit:     Max entries to return. Omit to return all entries.
        summarize: True (default): counts by interface and state. False: raw ARP records.
    """
    entries = await get_client().get_arp(router, node, limit)
    if summarize:
        by_iface: dict[str, dict] = {}
        for e in entries:
            iface = e.get("deviceInterface", "unknown")
            state = e.get("state", "UNKNOWN")
            if iface not in by_iface:
                by_iface[iface] = {"count": 0, "states": {}}
            by_iface[iface]["count"] += 1
            by_iface[iface]["states"][state] = by_iface[iface]["states"].get(state, 0) + 1
        return json.dumps({"total": len(entries), "by_interface": by_iface}, indent=2)
    return json.dumps({"count": len(entries), "arp": entries}, indent=2)


@mcp.tool(annotations=_RO)
async def get_vrfs(router: str, limit: int | None = None) -> str:
    """Get VRFs configured on a router.

    Args:
        router: Router name (required).
        limit:  Max entries to return. Omit to return all VRFs.
    """
    entries = await get_client().get_vrfs(router, limit)
    return json.dumps({"count": len(entries), "vrfs": entries}, indent=2)
