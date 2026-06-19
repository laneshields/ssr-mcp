import asyncio
import json
from datetime import datetime, timedelta, timezone

from ssr_mcp.core import mcp, _RO, get_client


@mcp.tool(annotations=_RO)
async def get_connection_info() -> str:
    """Identify the device SSR_HOST is connected to and determine which tools
    are applicable. Call this first in any session.

    Returns mode, router name, node name, role, software version, and alarm
    counts for the connected device.

    Modes:
      conductor       — SSR_HOST is a conductor. All managed routers are
                        accessible by name via the router: parameter.
                        Conductor-specific tools (get_assets, trace_session)
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


@mcp.tool(annotations=_RO)
async def list_routers() -> str:
    """List all routers managed by the conductor, or the local router when
    connected directly to a standalone router.

    Context: conductor — returns all managed routers.
             standalone router — returns only the local device.

    Use get_connection_info for richer context including the connection mode.
    """
    routers = await get_client().get_routers()
    return json.dumps(routers, indent=2)


@mcp.tool(annotations=_RO)
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


@mcp.tool(annotations=_RO)
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


@mcp.tool(annotations=_RO)
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


@mcp.tool(annotations=_RO)
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
      has_module     — get_application_names,
                       app_id_address_lookup
      has_http_https — get_app_id_cache, get_application_traffic,
                       get_web_filtering_info,
                       app_id_address_lookup, app_id_domain_lookup

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
            "has_module": has_all or "module" in modes,
            "has_http_https": has_all or "http" in modes or "https" in modes,
        }

    return json.dumps({"nodes": nodes, "app_id": app_id}, indent=2)


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


@mcp.tool(annotations=_RO)
async def get_software_version() -> str:
    """Get detailed software version information for the connected device,
    including build metadata beyond what get_connection_info provides.

    Call this when you need more version detail than the version string in
    get_connection_info. For managed routers, use get_router_info which returns
    per-node software versions.

    Context: any — always returns the version of the connected device.
    """
    data = await get_client().get_software_version()
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_RO)
async def get_session_processor_utilization(router: str, node: str) -> str:
    """Get CPU utilization of the service area (session processor) threads.

    Args:
        router: Router name (required).
        node:   Node name (required).
    """
    data = await get_client().get_session_processor_utilization(router, node)
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_RO)
async def get_resource_allocation(router: str, node: str) -> str:
    """Get forwarding core and hugepage memory allocation for a node.

    Args:
        router: Router name (required).
        node:   Node name (required).
    """
    data = await get_client().get_resource_allocation(router, node)
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_RO)
async def get_events(
    router: str,
    from_time: str | None = None,
    to_time: str | None = None,
    event_types: list[str] | None = None,
    subtype: str | None = None,
    limit: int | None = None,
    summarize: bool = True,
) -> str:
    """Get the event (audit log) history for a router.

    Defaults to the last 24 hours when from_time is not provided. Pass
    from_time explicitly to extend or narrow the window.

    summarize=True (default): returns event counts grouped by type plus the
    time period. Use this for a quick overview of activity. Pass summarize=False
    to get individual event records.

    Args:
        router:      Router name (required).
        from_time:   Start of time range in ISO 8601, e.g. '2026-05-01T00:00:00Z'.
                     Defaults to 24 hours ago.
        to_time:     End of time range in ISO 8601. Defaults to now.
        event_types: (optional) List of AuditLogType values to filter by.
        subtype:     (optional) Event subtype to filter by.
        limit:       Max events to return. Omit to return all events in range.
                     Applies in both summarize modes.
        summarize:   True (default): counts by event type. False: raw event records.
    """
    if from_time is None:
        from_time = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    events = await get_client().get_events(router, from_time, to_time, event_types, subtype, limit)
    if summarize:
        by_type: dict[str, int] = {}
        for e in events:
            by_type[e.get("type", "UNKNOWN")] = by_type.get(e.get("type", "UNKNOWN"), 0) + 1
        return json.dumps(
            {"count": len(events), "period": {"from": from_time, "to": to_time}, "by_type": by_type},
            indent=2,
        )
    return json.dumps({"count": len(events), "events": events}, indent=2)


@mcp.tool(annotations=_RO)
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


@mcp.tool(annotations=_RO)
async def get_system_processes(
    router: str | None = None,
    node: str | None = None,
) -> str:
    """Get the status of all major SSR processes on a node.

    Call this when get_system_state or get_router_health indicates a node is
    not fully RUNNING — it identifies which specific processes are down. Also
    use this to check HA leader/standby status for redundancy-capable processes
    (e.g. to confirm which node is active after a failover).

    Context: any — omit router to query the connected device itself.

    Args:
        router: (optional) Limit to a specific router.
        node:   (optional) Limit to a specific node within that router.
    """
    data = await get_client().get_system_processes(router, node)
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_RO)
async def get_platform(
    router: str | None = None,
    node: str | None = None,
    summary: bool = True,
) -> str:
    """Get hardware platform information for nodes — CPU, memory, disks,
    OS, and vendor/product details.

    By default returns summary mode (omits NIC/deviceInterfaces inventory,
    which can be very large on multi-NIC systems). Pass summary=False to
    include full NIC detail (manufacturer, driver, PCI address, MAC, firmware).

    Context: any — omit router to query the connected device itself.

    Args:
        router:  (optional) Limit to a specific router.
        node:    (optional) Limit to a specific node within that router.
        summary: (optional) When True (default), omit deviceInterfaces detail.
    """
    data = await get_client().get_platform(router, node, summary)
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_RO)
async def get_system_services(
    router: str | None = None,
    node: str | None = None,
) -> str:
    """Get the status of systemd services used alongside the SSR application.

    Use this only to check OS-level service status (e.g. authy, salt-minion,
    or other systemd services). For SSR process health, get_router_health
    includes SSR process state and get_system_processes gives per-process detail.

    Context: any — omit router to query the connected device itself.

    Args:
        router: (optional) Limit to a specific router.
        node:   (optional) Limit to a specific node within that router.
    """
    data = await get_client().get_system_services(router, node)
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_RO)
async def get_system_connectivity(
    router: str | None = None,
    node: str | None = None,
) -> str:
    """Get management connectivity status between nodes and the conductor,
    or between nodes in an HA router pair.

    Use this only to diagnose management plane or HA node-to-node connectivity
    issues specifically. get_router_health's overall_status already covers node
    online/offline state — call this only when you need to understand which
    specific connectivity links are degraded.

    Context: any — omit router to query the connected device itself.

    Args:
        router: (optional) Limit to a specific router.
        node:   (optional) Limit to a specific node within that router.
    """
    data = await get_client().get_system_connectivity(router, node)
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_RO)
async def get_system_state(
    router: str | None = None,
    node: str | None = None,
) -> str:
    """Get basic system state for routers/nodes — status, uptime, role,
    software version, and active alarm count.

    Call this for a quick node status check. If any node returns a status other
    than RUNNING, follow up with get_system_processes to identify which specific
    processes are not in their expected state. For a full health summary
    including alarms and utilization, use get_router_health instead.

    Context: any — omit router to query the connected device itself.
             On a conductor, omitting router returns state for all managed
             routers.

    Args:
        router: (optional) Limit to a specific router.
        node:   (optional) Limit to a specific node within that router.
    """
    data = await get_client().get_system_state(router, node)
    return json.dumps(data, indent=2)


@mcp.tool(annotations=_RO)
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


@mcp.tool(annotations=_RO)
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


@mcp.tool(annotations=_RO)
async def get_running_config(
    router: str | None = None,
    subtree: str | None = None,
) -> str:
    """Fetch the running configuration.

    WARNING: Without a router filter this returns the full authority
    configuration, which can be very large on conductors managing many
    routers. Use router or subtree to limit the response whenever possible.

    Context: any — without router, returns the full authority config (conductor)
             or the local device config (standalone router). With router,
             returns that router's config subtree. With subtree, fetches an
             arbitrary config path (e.g. "authority/router/boston-01/service").

    Args:
        router: (optional) Limit to this router's config subtree.
        subtree: (optional) Fetch a specific config path, e.g.
            "authority/router/boston-01/service". When provided, takes
            precedence over router.
    """
    config = await get_client().get_running_config(router, subtree)
    return json.dumps(config, indent=2)


@mcp.tool(annotations=_RO)
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


@mcp.tool(annotations=_RO)
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


@mcp.tool(annotations=_RO)
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
