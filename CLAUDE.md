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
```

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

Every tool call is appended as a JSON line to `~/.ssr-mcp/tool_calls.jsonl` (override with `SSR_MCP_LOG_FILE`). Each record contains the UTC timestamp, tool name, arguments, and response character count.

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

## Adding a new tool

1. Add a method to `SSRClient` in `client.py`.
2. Add a `@mcp.tool()` async function in `server.py` that calls the new method and returns `json.dumps(result, indent=2)`.

The MCP server entry point is `ssr_mcp/server.py:main()`, registered as the `ssr-mcp` script in `pyproject.toml`.
