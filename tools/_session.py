"""Per-session isolation support. Extracts client session ID from HTTP headers."""

_DEFAULT_SESSION = "__default__"
_HEADER_NAME = "x-client-session-id"


def get_client_session_id() -> str:
    """Return the client session ID from the current request, or a default.

    The proxy copies the MCP Mcp-Session-Id header into X-Client-Session-Id
    before FastMCP's get_http_headers() strips the original. Backend tools
    call this to key per-session state (REPL workers, SSH connections).
    """
    try:
        from fastmcp.server.http import _current_http_request

        request = _current_http_request.get()
        if request is not None:
            sid = request.headers.get(_HEADER_NAME)
            if sid:
                return sid
    except Exception:
        pass
    return _DEFAULT_SESSION
