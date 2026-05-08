FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

# Install dependencies into the project virtualenv (cached layer)
COPY pyproject.toml uv.lock LICENSE ./
RUN uv sync --frozen --no-install-project

# Copy source and install the project itself
COPY ssr_mcp/ ./ssr_mcp/
RUN uv sync --frozen

# Run as non-root
RUN useradd -r -s /sbin/nologin app \
    && chown -R app:app /app \
    && mkdir -p /var/log/ssr-mcp \
    && chown app:app /var/log/ssr-mcp
USER app

EXPOSE 8000

# Credentials and transport are supplied at runtime via environment variables.
# Example:
#   docker run -e SSR_HOST=... -e SSR_USERNAME=... -e SSR_PASSWORD=... \
#              -e SSR_MCP_TRANSPORT=streamable-http \
#              -e SSR_MCP_HOST=0.0.0.0 \
#              -e SSR_MCP_AUTH_TOKEN=... \
#              -p 8000:8000 ssr-mcp
ENV SSR_MCP_TRANSPORT=streamable-http \
    SSR_MCP_HOST=0.0.0.0 \
    SSR_MCP_PORT=8000 \
    SSR_MCP_LOG_FILE=/var/log/ssr-mcp/tool_calls.jsonl

CMD [".venv/bin/ssr-mcp"]
