#!/bin/sh
# Fix log directory ownership when a volume is mounted over it at runtime.
chown app:app /var/log/ssr-mcp 2>/dev/null || true
exec runuser -u app -- "$@"
