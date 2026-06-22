# SSR MCP Server

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server that gives Claude AI hands-on access to [Juniper Session Smart Router (SSR)](https://www.juniper.net/us/en/products/routers/session-smart-router.html) networks. Instead of copying and pasting CLI output, you can ask Claude to investigate issues, correlate data across routers, and walk through problems with you in plain English — while it queries your actual network in real time.

## What it can do

Once connected, Claude can work with your SSR network across 50+ tools covering:

| Category | Capabilities |
|---|---|
| **Topology** | List routers, nodes, interfaces, peer paths, services, tenants |
| **Sessions** | Search and inspect active forwarding sessions and flow detail |
| **BGP** | Summary, per-neighbor state, RIB, received/advertised routes |
| **Routing** | FIB lookup, VRFs, RIB, service paths |
| **Health** | Alarms, router health, system state, dropped packets, events |
| **Performance** | Node utilization, capacity, top sources, time-series metrics |
| **Platform** | Software version, system processes, services, ARP, DHCP leases |
| **Application ID** | App classification cache, categories, FIB lookups |
| **Security / IDP** | IDP engine and cSRX health, SPU utilization, per-event security audit log |
| **Diagnostics** | Ping, running config, web filtering state |

Claude understands the difference between connecting to a **conductor** (where all managed routers are accessible by name) and connecting **directly to a router** — and adjusts which tools are available accordingly.

## Requirements

- Python 3.10 or newer
- [`uv`](https://docs.astral.sh/uv/) package manager
- Network access to your SSR conductor or router
- Claude Desktop or Claude Code

## Installation

```bash
# Install uv if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and install
git clone https://github.com/laneshields/ssr-mcp.git
cd ssr-mcp
uv sync
```

## Configuration

Copy the example env file and fill in your credentials:

```bash
cp .env.example .env
```

Edit `.env`:

```bash
# Required — your conductor IP/hostname or a direct router address
SSR_HOST=192.168.1.1
SSR_USERNAME=admin
SSR_PASSWORD=your-password

# Set false for self-signed certificates (common in SSR deployments)
SSR_VERIFY_SSL=false

# Override if your conductor listens on a non-standard HTTPS port
# SSR_PORT=49000
```

> **Keep credentials in `.env` only.** Do not put them in the Claude Desktop or Claude Code config files — those inject env vars that can silently override `.env`.

## Connecting to Claude

### Claude Desktop

Add the server to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "SSR": {
      "command": "uv",
      "args": ["--directory", "/path/to/ssr-mcp", "run", "ssr-mcp"]
    }
  }
}
```

Restart Claude Desktop. You should see the SSR tools available in the tools panel.

### Claude Code

```bash
claude mcp add SSR -- uv --directory /path/to/ssr-mcp run ssr-mcp
```

Or add manually to `~/.claude.json` under `mcpServers` with the same `command`/`args` structure as above.

## Running as a remote server

If your SSR conductor is only reachable from a specific machine (a jump host, server in the same network, etc.), you can run the MCP server there and have Claude connect to it over HTTP.

Set these additional variables in `.env`:

```bash
SSR_MCP_TRANSPORT=streamable-http
SSR_MCP_HOST=0.0.0.0
SSR_MCP_PORT=8000
SSR_MCP_AUTH_TOKEN=<a long random secret>   # generate with: openssl rand -hex 32
```

Start the server:

```bash
uv run ssr-mcp
```

Then point Claude at it. In Claude Code (`~/.claude.json`):

```json
{
  "mcpServers": {
    "SSR": {
      "type": "http",
      "url": "http://<server-ip>:8000/mcp",
      "headers": {
        "Authorization": "Bearer <your-token>"
      }
    }
  }
}
```

### Docker

Build and run the container (the HTTP transport is the default inside Docker):

```bash
docker build -t ssr-mcp .

docker run -d --name ssr-mcp \
  -e SSR_HOST=<conductor-ip> \
  -e SSR_USERNAME=admin \
  -e SSR_PASSWORD=<password> \
  -e SSR_VERIFY_SSL=false \
  -e SSR_MCP_AUTH_TOKEN=$(openssl rand -hex 32) \
  -p 8000:8000 \
  --restart unless-stopped \
  ssr-mcp
```

### Linux systemd service

```ini
# /etc/systemd/system/ssr-mcp.service
[Unit]
Description=SSR MCP Server
After=network.target

[Service]
Type=simple
User=<youruser>
WorkingDirectory=/path/to/ssr-mcp
ExecStart=/home/<youruser>/.local/bin/uv run ssr-mcp
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ssr-mcp
```

## Getting started with Claude

### Guided workflows (prompts)

The server includes built-in prompts that walk Claude through multi-step investigations automatically. Invoke them by name:

| Prompt | What it does |
|---|---|
| `health_check` | Conductor-wide triage (unreachable routers, alarms, system state) or single-router deep dive (software, connectivity, resources, IDP). |
| `troubleshoot_traffic` | Step-by-step connectivity troubleshooting. Accepts optional `router`, `source`, and `destination` to skip the discovery steps. |
| `troubleshoot_slow_traffic` | Latency and throughput investigation using TCP health signals, retransmissions, and RTT per application. |
| `explore` | Open-ended discovery walkthrough — summarises the state of a router or the whole authority. |

Examples:

> *"Run the health_check prompt."*

> *"Run the troubleshoot_traffic prompt for router BOS1, source 10.0.1.5, destination Teams."*

### Freeform questions

For ad-hoc investigation, start by orienting Claude:

> *"Call get_connection_info and tell me what you're connected to."*

This tells Claude whether it's talking to a conductor or a router directly, and gives it the router/node names to use in subsequent calls. From there you can ask things like:

- *"Are there any active alarms across the network?"*
- *"Show me all BGP neighbors on REM1 and their current state."*
- *"Find any sessions with packet loss in the last hour."*
- *"What peer paths are currently down or degraded?"*
- *"Walk me through what happened to the BFD sessions at 2pm."*

## Development

```bash
# Run tests / inspect tools interactively
uv run mcp dev ssr_mcp/server.py

# Tool calls are logged to ~/.ssr-mcp/tool_calls.jsonl
# Most-called tools:
jq -r '.tool' ~/.ssr-mcp/tool_calls.jsonl | sort | uniq -c | sort -rn
```

See [`CLAUDE.md`](CLAUDE.md) for architecture details and guidance on adding new tools.
