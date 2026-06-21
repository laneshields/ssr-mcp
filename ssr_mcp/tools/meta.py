import json
import re
from datetime import datetime, timezone

from ssr_mcp.core import mcp, _RO, _LW, _GUIDANCE, _TOPICS, _LOG_PATH


_TRIAGE_CATEGORIES: dict[str, dict] = {
    "reachability": {
        "keywords": [
            "can't reach", "unreachable", "blocked", "not accessible",
            "not working", "cannot connect", "dropped", "no route",
        ],
        "workflow": [
            "1. If destination is an app name (Teams, Zoom, etc.) and has_http_https is enabled: "
            "get_app_id_cache to resolve IPs/ports, then proceed.",
            "2. If source/dest known → fib_lookup; no result = FIB miss (tenant/service config problem).",
            "3. FIB result, 0 next-hops → get_dropped_packets to confirm; check service/path config.",
            "4. FIB result, 1+ next-hops → get_sessions to confirm session; "
            "if session exists check list_service_paths / list_peer_paths.",
            "5. No source/dest specifics → get_dropped_packets unfiltered first.",
        ],
        "tools": ["fib_lookup", "get_dropped_packets", "get_sessions", "list_service_paths",
                  "list_peer_paths", "get_app_id_cache"],
        "clarifying_questions": [
            "Do you have a source IP or hostname?",
            "What destination are they trying to reach?",
            "Is traffic completely absent or intermittently failing?",
        ],
    },
    "performance": {
        "keywords": [
            "slow", "latency", "high rtt", "poor performance", "lag",
            "sluggish", "degraded", "timeout",
        ],
        "workflow": [
            "1. If destination is an app name and has_http_https is enabled: "
            "get_app_id_cache to resolve IPs, then proceed.",
            "2. get_application_traffic(view='tcp_health') — find apps with elevated retransmissions/RTT.",
            "3. list_peer_paths — check SVR path latency/loss/MOS; confirm the affected "
            "service actually routes through the degraded peer before citing it as a cause.",
            "4. get_application_traffic(view='clients', application=<name>) — confirm which clients are affected.",
            "5. query_metrics on interface bandwidth — rule out local congestion.",
        ],
        "tools": ["get_application_traffic", "list_peer_paths", "query_metrics", "get_app_id_cache"],
        "clarifying_questions": [
            "Is this for a specific application (e.g. Teams, Zoom)?",
            "Is it affecting all sites or one router?",
        ],
    },
    "health": {
        "keywords": [
            "alarm", "down", "unhealthy", "not healthy", "health check",
            "offline", "status", "node", "ha", "failover", "redundancy",
        ],
        "workflow": [
            "1. get_conductor_summary (conductor mode) for authority-wide overview.",
            "2. get_router_health for a specific router → alarms, node state, processes, utilization.",
            "3. Drill into any 'High' state with query_metrics to confirm sustained vs transient spike.",
        ],
        "tools": ["get_conductor_summary", "get_router_health", "get_alarms", "query_metrics"],
        "clarifying_questions": ["Is this a specific router or the whole network?"],
    },
    "bgp": {
        "keywords": [
            "bgp", "border gateway", "peer", "neighbor", "advertised", "received",
        ],
        "workflow": [
            "1. get_bgp_summary — neighbor state counts.",
            "2. get_bgp_neighbors — per-neighbor detail; check Established vs other states.",
            "3. get_bgp_received_routes / get_bgp_advertised_routes for a specific neighbor.",
            "4. get_rib — verify routes are installed.",
        ],
        "tools": ["get_bgp_summary", "get_bgp_neighbors", "get_bgp_received_routes",
                  "get_bgp_advertised_routes", "get_rib"],
        "clarifying_questions": [
            "Do you have a specific BGP neighbor IP?",
            "Are routes missing or are neighbors down?",
        ],
    },
    "capacity": {
        "keywords": [
            "cpu", "memory", "utilization", "overloaded", "high load", "resource",
            "exhausted", "full", "session count", "session table", "session limit",
        ],
        "workflow": [
            "1. get_node_utilization — CPU, memory, disk.",
            "2. get_session_processor_utilization — service area thread load.",
            "3. query_metrics with counter=False; compare current vs avg to confirm sustained vs spike.",
            "4. get_capacity — session/flow table headroom.",
        ],
        "tools": ["get_node_utilization", "get_session_processor_utilization",
                  "query_metrics", "get_capacity"],
        "clarifying_questions": ["Which router is showing high load?"],
    },
    "idp": {
        "keywords": [
            "idp", "intrusion", "attack", "threat", "malicious", "blocked by",
            "ids", "antivirus", "anti-virus", "signature", "exploit", "vulnerability",
        ],
        "workflow": [
            "1. get_idp_status — engine state, container health, SPU utilization.",
            "2. get_security_events — recent IDP hits, grouped by attack type.",
            "3. Correlate with get_sessions to find associated flows.",
            "4. If legitimate traffic is being blocked: cross-reference with get_dropped_packets.",
        ],
        "tools": ["get_idp_status", "get_security_events", "get_sessions", "get_dropped_packets"],
        "clarifying_questions": [
            "Is IDP blocking legitimate traffic or are you investigating an attack?",
        ],
    },
    "sessions": {
        "keywords": ["session", "flow", "nat", "forwarding"],
        "workflow": [
            "1. get_sessions (with source/dest filter if known).",
            "2. get_session on a specific UUID for full detail.",
            "3. trace_session in conductor mode to find all legs of an SVR session by UUID.",
        ],
        "tools": ["get_sessions", "get_session", "trace_session"],
        "clarifying_questions": [
            "Do you have a source IP, destination, or session UUID?",
        ],
    },
    "discovery": {
        "keywords": [
            "what routers", "explore", "what services", "topology", "what sites",
            "inventory", "overview", "top applications", "what applications",
        ],
        "workflow": [
            "1. list_routers — enumerate all routers.",
            "2. get_router_health per router for quick status.",
            "3. list_services / list_peer_paths for service and path inventory.",
            "4. get_application_traffic / get_application_names for app inventory.",
        ],
        "tools": ["list_routers", "get_router_health", "list_services",
                  "list_peer_paths", "get_application_traffic", "get_application_names"],
        "clarifying_questions": [],
    },
}


def _classify_query(question: str) -> dict:
    q = question.lower()
    scores = {
        cat: sum(
            1 for kw in data["keywords"]
            if re.search(r"\b" + re.escape(kw), q)
        )
        for cat, data in _TRIAGE_CATEGORIES.items()
    }
    best = max(scores, key=scores.get)
    hits = scores[best]
    if hits == 0:
        return {"category": "general", "confidence": "low"}
    data = _TRIAGE_CATEGORIES[best]
    return {
        "category": best,
        "confidence": "high" if hits >= 2 else "medium",
        "recommended_workflow": data["workflow"],
        "tools_in_scope": data["tools"],
        "clarifying_questions": data["clarifying_questions"],
    }


@mcp.tool(annotations=_LW)
async def begin_query(question: str) -> str:
    """Log the user's question and return operational guidance.

    Call this FIRST at the start of every user request, before calling any
    other tools. Pass a concise restatement of what the user is asking.

    Returns a triage block (when the query matches a known category) followed
    by full operational guidance covering session startup rules, connection
    modes, the SSR traffic flow model, connectivity and slow-traffic
    troubleshooting decision trees, and metric interpretation. Read it before
    proceeding — it determines which tools to call and in what order.

    Args:
        question: A one- or two-sentence summary of what the user is asking.
    """
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": "query",
            "question": question,
        }
        with _LOG_PATH.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass

    triage = _classify_query(question)
    if triage["confidence"] == "low":
        return _GUIDANCE

    triage_block = (
        "## Triage\n\n"
        f"**Category:** {triage['category']}  \n"
        f"**Confidence:** {triage['confidence']}\n\n"
        "**Recommended workflow:**\n"
        + "\n".join(f"- {step}" for step in triage["recommended_workflow"])
        + "\n\n**Tools in scope:** "
        + ", ".join(f"`{t}`" for t in triage["tools_in_scope"])
    )
    if triage["clarifying_questions"]:
        triage_block += (
            "\n\n**Clarifying questions to ask if needed:**\n"
            + "\n".join(f"- {q}" for q in triage["clarifying_questions"])
        )
    triage_block += "\n\n---\n\n"
    return triage_block + _GUIDANCE


@mcp.tool(annotations=_RO)
async def get_guidance(topic: str | None = None) -> str:
    """Return operational guidance for using this MCP server effectively.

    With no topic, returns the same general guidance `begin_query` returns.
    Call this only to re-read it mid-session without logging a new query.

    With a topic, returns a deep tool-specific reference. Some tool docstrings
    point here for detail kept out of their always-loaded description.

    Args:
        topic: (optional) A reference topic. Known topics: 'rib'. An unknown
               topic returns the list of available topics.
    """
    if topic is None:
        return _GUIDANCE
    if topic in _TOPICS:
        return _TOPICS[topic]
    return f"Unknown topic '{topic}'. Available topics: {', '.join(sorted(_TOPICS))}."


@mcp.tool(annotations=_LW)
async def report_issue(
    tool: str,
    observation: str,
    data: str | None = None,
) -> str:
    """Report a suspected bug or data anomaly encountered during a tool call.

    Call this whenever a tool result looks wrong or inconsistent — for example,
    a summary returning "unknown" for fields that should have values, a result
    that contradicts another tool's output, or a response shape that doesn't
    match the documented format.

    Reports are written to the same log file as tool calls and query records so
    they can be correlated with the session that triggered them.

    Args:
        tool:        Name of the tool that returned the suspicious result.
        observation: Concise description of what was unexpected and why.
        data:        Optional snippet of the suspicious result (JSON string or
                     short excerpt) to attach for context.
    """
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record: dict = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": "issue",
            "tool": tool,
            "observation": observation,
        }
        if data is not None:
            record["data"] = data
        with _LOG_PATH.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass
    return "Issue logged. Thank you — this helps improve the tools."


@mcp.tool(annotations=_LW)
async def report_feedback(
    complaint: str,
    what_went_wrong: str,
    context: str,
) -> str:
    """Log user dissatisfaction with a result during a troubleshooting session.

    Call this when the user indicates the answer or analysis was wrong,
    incomplete, or unhelpful — for example: "that's not right", "you missed X",
    "I expected Y", "that doesn't make sense". Do not wait for the user to ask
    you to log it; call it proactively as soon as dissatisfaction is clear.

    Args:
        complaint:      What the user said or expressed, quoted or closely
                        paraphrased.
        what_went_wrong: Your honest self-assessment of the error — which step
                         failed, what assumption was wrong, or what you should
                         have done differently.
        context:        A brief summary of the interaction at the point of
                        complaint: which tools were called, what they returned,
                        and what you reported to the user. Include key values
                        (router name, tool names, result snippets) so the record
                        is useful without replaying the full conversation.
    """
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": "feedback",
            "complaint": complaint,
            "what_went_wrong": what_went_wrong,
            "context": context,
        }
        with _LOG_PATH.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass
    return "Feedback logged. Thank you — this helps improve the tools."
