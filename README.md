# MCP Toolbox

Two MCP servers from a single codebase: **toolbox** (general-purpose dev tools) and **pentest** (security testing tools). Built for [Claude Code](https://claude.ai/code).

## What's Inside

**Toolbox server** — Python REPL with persistent state, shell execution (sandboxed read-only + full-access with risk classification), browser automation (Playwright + Stagehand), SSH, library docs lookup, feedback tracking with auto-fix pipeline.

**Pentest server** — Nmap, Nikto, Nuclei, SQLMap, FFuf, Hydra, and more, wrapped as MCP tools with structured output.

## Architecture

```
                      toolbox (core)
Claude Code ─SSE:11000─▶ proxy.py ─HTTP:8765─▶ fastmcp --reload server.py

                      pentest (security)
Claude Code ─SSE:11001─▶ proxy_pentest.py ─HTTP:8766─▶ fastmcp --reload server_pentest.py
```

- **Proxies** — stable SSE front-ends that survive backend restarts
- **Backends** — auto-reload on tool file changes (edit, save, next call uses new code)
- **Isolated envs** — server runs in `.venv/`, REPL user code runs in `repl_venv/`

## Quick Start

```bash
# Clone and set up
git clone https://github.com/depoledna/toolbox.git mcp
cd mcp
uv venv && uv pip install -r pyproject.toml

# Configure environment
cp .env.example .env  # add your API keys

# Start servers
./scripts/run_server.sh
```

Then add to your Claude Code MCP config:
```json
{
  "mcpServers": {
    "toolbox": { "url": "http://localhost:11000/sse" },
    "pentest": { "url": "http://localhost:11001/sse" }
  }
}
```

## Adding Tools

Create a file in `tools/` (or `tools/pentest/` for security tools) with a public async function:

```python
async def my_tool(param: str) -> str:
    """Docstring becomes the tool description."""
    return "result"
```

Save — it's live immediately. Prefix helpers with `_` to exclude them from registration.

## Configuration

| File | Purpose |
|------|---------|
| `.env` | API keys (OPENROUTER_KEY, CONTEXT7_API_KEY, etc.) |
| `settings.json` | SSH server config (alias, host, user, auth) |

Both are gitignored. See `.env.example` for required variables.

## License

MIT
