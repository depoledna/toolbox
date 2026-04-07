"""Library documentation lookup via Context7."""

import os
import re

import httpx

_API_BASE = "https://context7.com/api"
_API_KEY = os.environ.get("CONTEXT7_API_KEY", "")
_VERSION = "1.0.0"
_ACTIONS = ("resolve", "query")

_HEADERS = {
    "X-Context7-Source": "mcp-server",
    "X-Context7-Server-Version": _VERSION,
    "X-Context7-Transport": "custom",
    **({"Authorization": f"Bearer {_API_KEY}"} if _API_KEY else {}),
}


async def docs(action: str, library: str = "", query: str = "") -> str:
    """Look up library, framework, or SDK documentation via Context7.

    Use before web search when researching any library or framework — covers API syntax,
    configuration, version migration, CLI usage, and more. Not for general programming
    concepts, code review, or business logic questions.

    Actions:
      resolve — search for a library by name, returns matching IDs
      query   — fetch documentation (auto-resolves name if library doesn't start with /)

    Args:
        action: "resolve" or "query"
        library: Library name to search (resolve), or Context7 ID like "/facebook/react" (query).
                 Plain names are auto-resolved in query mode.
        query: Natural language question about the library (query only)

    Returns:
        resolve: markdown list of matching libraries with IDs (use ID in query action).
        query: raw documentation text relevant to the question.
    """
    if action not in _ACTIONS:
        return f"Unknown action '{action}'. Use: {', '.join(_ACTIONS)}"

    if action == "resolve":
        return await _resolve(library)
    return await _query(library, query)


async def _resolve(library: str) -> str:
    if not library.strip():
        return "Error: library required"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{_API_BASE}/v2/libs/search",
            params={"query": library, "libraryName": library},
            headers=_HEADERS,
        )
        resp.raise_for_status()
        data = resp.json()

    results = data.get("results", [])
    if not results:
        return f"No libraries found for '{library}'."

    return "\n".join(
        f"- **{r.get('title', '')}** (`{r.get('id', '')}`): {r.get('description', '')}"
        for r in results[:10]
    )


async def _query(library: str, query: str) -> str:
    if not library.strip():
        return "Error: library required"
    if not query.strip():
        return "Error: query required"

    # Auto-resolve if library doesn't look like a Context7 ID
    library_id = library
    if not library.startswith("/"):
        resolve_result = await _resolve(library)
        match = re.search(r"`(/[^`]+)`", resolve_result)
        if not match:
            return f"Could not auto-resolve '{library}'. Try action='resolve' to find the correct ID."
        library_id = match.group(1)

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(
            f"{_API_BASE}/v2/context",
            params={"query": query, "libraryId": library_id},
            headers=_HEADERS,
        )
        resp.raise_for_status()
        text = resp.text

    if not text:
        return "No documentation found. Try action='resolve' to verify the library ID."

    return text
