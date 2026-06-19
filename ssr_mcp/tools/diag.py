import json

from ssr_mcp.core import mcp, _RO, get_client


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


@mcp.tool(annotations=_RO)
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


def _format_bps(bps: float) -> str:
    if bps >= 1_000_000_000:
        return f"{bps / 1_000_000_000:.1f} Gbps"
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.1f} Mbps"
    if bps >= 1_000:
        return f"{bps / 1_000:.1f} Kbps"
    return f"{bps:.0f} bps"


@mcp.tool(annotations=_RO)
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


@mcp.tool(annotations=_RO)
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


@mcp.tool(annotations=_RO)
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


@mcp.tool(annotations=_RO)
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
