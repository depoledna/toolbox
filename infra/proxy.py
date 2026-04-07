"""
MCP Proxy — stable front-end for Claude Code.

Forwards all tool/resource/prompt requests to the backend server.
Each request creates a fresh session, so backend restarts are transparent.

Injects X-Client-Session-Id header so backend tools can isolate
per-client state (REPL workers, SSH connections).
"""

import signal
import sys

import anyio
import uvicorn
from starlette.types import ASGIApp, Receive, Scope, Send

from fastmcp.server import create_proxy

BACKEND_URL = "http://localhost:8765/mcp"

_MCP_SESSION_HEADER = b"mcp-session-id"
_CLIENT_SESSION_HEADER = b"x-client-session-id"


class InjectClientSessionId:
    """ASGI middleware: copy Mcp-Session-Id -> X-Client-Session-Id.

    FastMCP's get_http_headers() strips mcp-session-id before forwarding
    to the backend. This middleware copies the value into a custom header
    that survives the strip, so backend tools can identify the client.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = list(scope.get("headers", []))
            session_id = None
            for name, value in headers:
                if name == _MCP_SESSION_HEADER:
                    session_id = value
                    break
            if session_id is not None:
                headers = [
                    (n, v) for n, v in headers if n != _CLIENT_SESSION_HEADER
                ]
                headers.append((_CLIENT_SESSION_HEADER, session_id))
                scope = {**scope, "headers": headers}
        await self.app(scope, receive, send)


proxy = create_proxy(BACKEND_URL, name="Toolbox Proxy")


async def _serve() -> None:
    app = proxy.http_app(path="/mcp")
    wrapped = InjectClientSessionId(app)

    async with proxy._lifespan_manager():
        config = uvicorn.Config(
            wrapped,
            host="localhost",
            port=11000,
            log_level="debug",
            lifespan="on",
            timeout_graceful_shutdown=0,
        )
        server = uvicorn.Server(config)
        await server.serve()


def main():
    def _handle_sig(sig, frame):  # noqa: ARG001
        print("\n[proxy] Received signal, shutting down proxy.")
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    anyio.run(_serve)


if __name__ == "__main__":
    main()
