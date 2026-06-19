from ssr_mcp.core import mcp


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
| Application name (e.g. "Teams", "Zoom") and `has_http_https` is true | `get_app_id_cache(application='<name>')` → extract dest IP, port, protocol for FIB lookup; if no entries returned fall back to `get_dropped_packets` filtered by the known source IP |
| Application name and `has_http_https` is false | App-id not active — ask user for destination IP and port |
| Domain name and `has_http_https` is true | `app_id_domain_lookup` |
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
IP is not classified into the expected tenant. Check `get_tenant_membership` for the
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
            get_application_traffic results.
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
        "In Step 5, filter `get_application_traffic` by this name. If it does not "
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
- `app_id.has_http_https` — determines whether `get_application_traffic` is available.

## Step 3 — SSR health snapshot

Call **in parallel** (router + node):
- `get_session_processor_utilization` — service area CPU and thread state
- `get_capacity` — FIB, flow table, action pool utilization
- `get_node_utilization` — CPU, memory, disk
- `get_resource_allocation` — core assignment (identifies IDP-dedicated cores)

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

Call `get_application_traffic(view='tcp_health', window_minutes=30)` (router + node).
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
  issue; drill into that app with `get_application_traffic(view='clients')` (see below).
- **All applications elevated** → link-wide or SSR-wide problem; check peer path
  metrics in Step 6 and MTU/MSS in Step 7.
- **Retransmissions low but TTFP/RTT elevated uniformly** → latency rather than
  loss; check peer path latency in Step 6.
- **Drops in Step 4 correlate with retransmissions** → SSR is the common thread;
  note source NAT / waypoint exhaustion if drops are from many clients.

**Drill-down for a suspect application:**
Once `get_application_traffic(view='tcp_health')` identifies an application with
elevated signals, call `get_application_traffic(view='clients', application=<name>)`
(router + node, `window_minutes=30`) for per-client detail. The application filter
keeps the response small.

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

Call `get_application_traffic(view='tcp_health', application=<name>)` (router + node,
`window_minutes=30`) to check TCP health for that app, then follow with
`get_application_traffic(view='clients', application=<name>)` for per-client detail.
Apply the same flow-direction and interface-correlation analysis described above.

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
- `traffic_classes` from `get_application_traffic` (`view='top'`) shows which
  SSR queue slots this router's traffic is using. All traffic in `low` only means
  no TE classification is active in the service config — contention still affects
  all traffic equally rather than prioritizing by class.

**Service path linkage:**
Call `list_service_paths` filtered to the service names identified in the
drill-down results (the `services` field from `get_application_traffic`). Use
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
