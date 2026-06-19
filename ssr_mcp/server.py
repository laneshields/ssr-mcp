from ssr_mcp.core import mcp, _TRANSPORT, _HOST, _PORT, _AUTH_TOKEN

import ssr_mcp.tools.meta  # noqa: F401
import ssr_mcp.tools.system  # noqa: F401
import ssr_mcp.tools.network  # noqa: F401
import ssr_mcp.tools.sessions  # noqa: F401
import ssr_mcp.tools.appid  # noqa: F401
import ssr_mcp.tools.routing  # noqa: F401
import ssr_mcp.tools.services  # noqa: F401
import ssr_mcp.tools.diag  # noqa: F401
import ssr_mcp.prompts  # noqa: F401
import ssr_mcp.resources  # noqa: F401


class _BearerAuthMiddleware:
    """Pure-ASGI bearer token gate. Passes lifespan/websocket scopes through unchanged."""

    def __init__(self, app: object, token: str) -> None:
        self._app = app
        self._token = token

    async def __call__(self, scope: dict, receive: object, send: object) -> None:
        if scope["type"] == "http":
            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            auth = headers.get(b"authorization", b"").decode()
            if not (auth.startswith("Bearer ") and auth[7:] == self._token):
                await self._reject(scope, send)
                return
        await self._app(scope, receive, send)

    @staticmethod
    async def _reject(scope: dict, send: object) -> None:
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [[b"www-authenticate", b"Bearer"], [b"content-length", b"12"]],
        })
        await send({"type": "http.response.body", "body": b"Unauthorized"})


def main() -> None:
    if _TRANSPORT == "stdio":
        mcp.run(transport="stdio")
        return

    import anyio
    import uvicorn

    async def _serve() -> None:
        if _TRANSPORT == "streamable-http":
            app: object = mcp.streamable_http_app()
        else:
            app = mcp.sse_app()
        if _AUTH_TOKEN:
            app = _BearerAuthMiddleware(app, _AUTH_TOKEN)
        config = uvicorn.Config(app, host=_HOST, port=_PORT)
        await uvicorn.Server(config).serve()

    anyio.run(_serve)


if __name__ == "__main__":
    main()
