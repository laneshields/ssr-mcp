# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
uv sync

# Run the MCP server (stdio transport, for use with Claude Desktop / MCP clients)
uv run ssr-mcp

# Run directly via Python
uv run python -m ssr_mcp.server

# Inspect available tools via MCP dev UI
uv run mcp dev ssr_mcp/server.py

# Validate all tools against a live SSR instance
uv run python tests/validate_tools.py
```

## Validation script

`tests/validate_tools.py` runs every SSRClient method directly against a real SSR
instance — no MCP host required. It reads credentials from `.env` and auto-discovers
the connection mode, SSR software versions, and enabled features (app-id, IDP) before
running tests. Tests that don't apply to the current setup (conductor-only tools on a
direct router connection, app-id tools when app-id is disabled, etc.) are skipped
rather than failed.

Set `SSR_TEST_ROUTER` in `.env` to pin a specific managed router for conductor-mode
tests. If unset, the script discovers connected managed routers and prompts for a
selection with a suggested default.

When tests fail, the script prints a JSON block containing the full test context
(connection mode, conductor version, router version, feature flags) alongside each
error. **Always include this block when asking Claude Code to investigate or fix a
failure** — it prevents version-specific assumptions and ensures fixes are valid
across conductor, managed-router, and cloud-managed deployments.

## Configuration

Copy `.env.example` to `.env` and fill in credentials before running:

```
SSR_HOST=<conductor-ip-or-hostname>
SSR_USERNAME=admin
SSR_PASSWORD=<password>
SSR_VERIFY_SSL=false   # set false for self-signed certs (common in SSR deployments)
SSR_PORT=443           # override if conductor listens on a non-standard HTTPS port
```

**Credential management**: credentials must live in `.env` only. Do **not** put them in the Claude Desktop `env` block (`claude_desktop_config.json`) or the Claude Code MCP user config (`~/.claude.json` env block) — those values are injected as process environment variables before the server starts and will silently override `.env`. The server uses `load_dotenv(override=True)` with an explicit path so `.env` always wins, but the cleanest setup is to keep credentials out of those config files entirely.

## HTTP transport (remote server)

By default the server runs over `stdio`. To run as a remote HTTP server, set `SSR_MCP_TRANSPORT` to `sse` or `streamable-http` (preferred — MCP spec 2025-03-26):

```
SSR_MCP_TRANSPORT=streamable-http
SSR_MCP_HOST=0.0.0.0      # bind address (default 127.0.0.1)
SSR_MCP_PORT=8000          # listen port (default 8000)
SSR_MCP_AUTH_TOKEN=<secret> # bearer token; omit to disable auth
```

Connect Claude Code or Claude Desktop to `http://<host>:8000/mcp` (streamable-http) or `http://<host>:8000/sse` (sse).

**Auth**: when `SSR_MCP_AUTH_TOKEN` is set and transport is not `stdio`, every HTTP request must carry `Authorization: Bearer <token>`. Requests without a valid token are rejected. Auth is silently skipped for `stdio` even if the env var is set.

**Credentials in remote mode**: the `.env` file (or plain environment variables) stays on the server. `load_dotenv(override=True)` wins over injected vars when `.env` exists; if `.env` is absent (e.g., a container that sets vars directly), `load_dotenv` is a no-op and the container env vars are used as-is.

## Connection modes

`SSR_HOST` can point to a conductor or directly to a router. Call
`get_connection_info` at the start of a session — it queries `GET /api/v1/system`
and returns an `isConductor` / `isManaged` / `isManagedByCloud` breakdown.

| mode | Description |
|---|---|
| `conductor` | SSR_HOST is a conductor. All managed routers accessible by name. |
| `router-managed` | Connected directly to a conductor-managed router (not via conductor). |
| `router-cloud` | Mist/cloud-managed router. No on-premises conductor. |
| `router-standalone` | Standalone router with no management plane. |

**Conductor mode**: all managed routers accessible via `router:` parameter;
conductor-specific tools available (`get_assets`, `find_sessions`); tools with
optional `router:` scope authority-wide when omitted.

**All router modes**: only the local device is accessible; use the `router` and
`node` names from `get_connection_info`; conductor-only tools unavailable; tools
with optional `router:` scope to the local device when omitted.

**Tool context tags** in docstrings:
- `Context: conductor only` — not available when connected directly to a router
- `Context: router` — targets a specific router (via conductor or directly)
- `Context: any` — works in all modes; behaviour when `router` is omitted differs as above

## Architecture

Two files make up the entire package:

**`ssr_mcp/client.py` — `SSRClient`**
Async httpx client targeting the SSR REST and GraphQL APIs. Handles JWT authentication lazily (login deferred until first request) and retries automatically on 401 by re-authenticating once. All REST calls go through `_get()`; GraphQL calls go through `_graphql()`. GraphQL queries are stored as class-level string constants directly above the method that uses them. Paginated endpoints loop internally at `page_size=1000` using either cursor-based (`pageInfo.endCursor`) or offset-based pagination — callers never see pages.

Accepts `port: int = 443` in `__init__`; base URL is `https://{host}:{port}`.

**`ssr_mcp/server.py` — FastMCP tool definitions**
Thin `@mcp.tool()` wrappers around every `SSRClient` method. Each tool serialises the result to JSON and returns it as a string. The client singleton is created lazily via `get_client()` on first tool call so the server starts cleanly even without `.env` credentials. Tool docstrings double as the MCP tool descriptions seen by Claude — keep them accurate and include all `Args:` entries.

`load_dotenv` is called with the explicit path `Path(__file__).parent.parent / ".env"` and `override=True` so the repo's `.env` always takes precedence over any env vars injected by the MCP host.

## Tool-call logging

Every tool call is appended as a JSON line to `~/.ssr-mcp/tool_calls.jsonl` (override with `SSR_MCP_LOG_FILE`). Two record types are written:

- `type: "query"` — logged by `begin_query` at the start of each user request; contains the LLM's summary of the user's question.
- `type: "tool_call"` — one record per tool invocation; contains tool name, arguments, and response character count.

`begin_query` must be called first on every user request so that query records and the tool calls they trigger can be correlated by timestamp in the log.

**Docker default:** the Dockerfile sets `SSR_MCP_LOG_FILE=/var/log/ssr-mcp/tool_calls.jsonl`. Mount a volume at `/var/log/ssr-mcp` to persist logs across container restarts:

```
docker run ... -v /host/path/logs:/var/log/ssr-mcp ssr-mcp
```

Docker users should substitute `/var/log/ssr-mcp/tool_calls.jsonl` (or the host path of the mounted volume) for `~/.ssr-mcp/tool_calls.jsonl` in the examples below.

```bash
# Which tools are called most often
jq -r '.tool' ~/.ssr-mcp/tool_calls.jsonl | sort | uniq -c | sort -rn

# Largest responses by tool — useful for spotting token-expensive calls
jq -r '[.tool, .response_chars] | @tsv' ~/.ssr-mcp/tool_calls.jsonl | sort -t$'\t' -k2 -rn | head -20

# What arguments are being passed to a specific tool (e.g. get_sessions)
jq 'select(.tool == "get_sessions") | .args' ~/.ssr-mcp/tool_calls.jsonl

# Tools called in the last 24 hours
jq -r 'select(.ts > (now - 86400 | todate)) | .tool' ~/.ssr-mcp/tool_calls.jsonl | sort | uniq -c | sort -rn

# Average response size per tool
jq -r '[.tool, .response_chars] | @tsv' ~/.ssr-mcp/tool_calls.jsonl \
  | awk -F'\t' '{sum[$1]+=$2; count[$1]++} END {for (t in sum) printf "%d\t%s\n", sum[t]/count[t], t}' \
  | sort -rn

# Show each query alongside the tools called to answer it (correlate by timestamp order)
jq -r 'if .type == "query" then "\n--- \(.ts) \(.question)" else "  \(.tool)" end' ~/.ssr-mcp/tool_calls.jsonl
```

## BGP tools

BGP data comes from the FRR-backed JSON REST endpoints under `/api/v1/router/{router}/routing/bgp/`. These return structured JSON (not CLI text) and are the same source used by the SSR management GUI.

| Tool | Endpoint | Notes |
|---|---|---|
| `get_bgp_summary` | `GET /routing/bgp/summary` | Neighbor state counts; `address_family=all` by default |
| `get_bgp_neighbors` | `GET /routing/bgp/neighbors` | Per-neighbor detail; optional `neighbor` filter |
| `get_bgp_received_routes` | `GET /routing/bgp/neighbors/received-routes` | Routes received from a specific neighbor |
| `get_bgp_advertised_routes` | `GET /routing/bgp/neighbors/advertised-routes` | Routes advertised to a specific neighbor |
| `get_bgp_routes` | `GET /routing/bgp` | Full BGP RIB (all prefixes across all neighbors) |

All BGP tools accept `router`, `vrf` (default `"default"`), and `address_family` (default `"ipv4"` or `"all"` depending on tool) parameters.

## Session startup for router-specific work

When working with a specific router, call `get_router_info` first. It returns the
node names needed by most router-targeted tools, the software version, and whether
application identification is enabled and which modes are active (`has_module`,
`has_http_https`). This avoids repeated discovery calls during the session.

`get_router_health` is separate — use it for triage ("is this router healthy?"),
not as a session initializer. Node names from `get_router_info` are required by
most tools; do not guess them.

## SSR traffic flow

Understanding how packets are processed helps interpret tool output correctly.

**Non-SVR traffic (standard forwarding):**

1. **Tenant classification** — on ingress, the source IP and ingress interface are
   matched against tenant membership rules (`list_tenant_members`) to assign a tenant.

2. **FIB lookup** — the tenant, destination IP, port, and protocol are looked up in
   the FIB. There is either 0 or 1 match.
   - **No match (FIB miss):** the SSR sends an ICMP network unreachable reply. The
     packet is silently dropped and **does not appear in `get_dropped_packets`**.
     `fib_lookup` returning no result confirms a FIB miss. Root cause is typically
     a missing service or incorrect tenant configuration.
   - **Match:** the entry identifies the service and provides 0 or more next-hops.

3. **Next-hop check:**
   - **0 next-hops:** traffic cannot be forwarded. **Appears in `get_dropped_packets`.**
   - **1+ next-hops:** packet is sent to the service area for session processing.

4. **Service area processing** — the session is established or fails. Failures
   **appear in `get_dropped_packets`**.

5. **Session established but traffic still fails** — if `get_dropped_packets` is
   empty and `fib_lookup` shows a valid match with next-hops but traffic is broken,
   the problem may be outside the SSR. Check `list_service_paths` and
   `list_peer_paths`; if those are healthy the fault is likely external.

## Connectivity troubleshooting

**Decision tree:**

- No specifics on source/destination → `get_dropped_packets` unfiltered
- Service area CPU high with no explanation → `get_dropped_packets` (flood of unmatched traffic)
- Have source + destination details → `fib_lookup`
  - No result → FIB miss → tenant or service config problem (not in dropped packets)
  - Result, 0 next-hops → `get_dropped_packets` to confirm + check service/path config
  - Result, 1+ next-hops → `get_sessions` to confirm session; if session exists check `list_service_paths` / `list_peer_paths`
- `get_dropped_packets` empty + valid FIB match + sessions establishing + traffic still broken → problem likely outside SSR

**Resolving a vague source:**

- Hostname/device description → `get_dhcp_leases` (by hostname) or `get_arp`
- Physical interface name → `get_device_interfaces`; if single network interface on it, use that name directly
- Network interface name → match directly in `get_network_interfaces`
- Source IP, directly connected → match subnet against ethernet-type interfaces in `get_network_interfaces`
- Source IP, off-network → `get_rib` LPM → next-hop `interfaceName` is a giid (e.g. `g12`) → match number against `globalId` in `get_network_interfaces`

**Resolving a vague destination:**

- Named service ("internet", "corporate-vpn") → `list_services` then `list_service_paths`
- Application name ("Teams", "Zoom"):
  - `has_http_https` enabled → `get_app_id_cache` (address cache, `summarize=False`) filtered by application name → dest IP/port/protocol → `fib_lookup`
  - Completely broken (cache may be empty) → `get_dropped_packets`
  - App-id not enabled → ask user for IP/port
- Domain name → `app_id_lookup` domain mode (requires `has_http_https`)
- All else fails → `get_dropped_packets`

## Adding a new tool

1. Add a method to `SSRClient` in `client.py`.
2. Add a `@mcp.tool()` async function in `server.py` that calls the new method and returns `json.dumps(result, indent=2)`.

The MCP server entry point is `ssr_mcp/server.py:main()`, registered as the `ssr-mcp` script in `pyproject.toml`.
