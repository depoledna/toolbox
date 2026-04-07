# MCP Toolbox

Two MCP servers from a single codebase: **toolbox** (general-purpose dev tools) and **pentest** (security testing tools). Built for [Claude Code](https://claude.ai/code) and any MCP-compatible client.

## Architecture

```
Claude Code ──► proxy :11000 ──► fastmcp --reload server.py      (toolbox)
             ──► proxy :11001 ──► fastmcp --reload server_pentest.py  (pentest)
```

**Proxy** (`:11000`, `:11001`) — lightweight HTTP reverse-proxy. Stays up permanently so the MCP connection is never dropped, even when the backend restarts.

**Backend** (`:8765`, `:8766`) — FastMCP server with `--reload`. Edit a tool file, save, and the next call uses the new code. The reload supervisor restarts the backend behind the proxy — no reconnect needed.

**When you do need to reconnect:** MCP clients cache the tool list (names, descriptions, parameters) at init. If you add a new tool, rename one, or change its signature, reconnect the MCP client (`/mcp` in Claude Code) to re-fetch the schema. Changes to tool *implementation* are picked up automatically.

**Isolated environments** — the server runs in `.venv/`, REPL user code runs in a separate `repl_venv/` so package installs don't affect the server.

## Quick Start

```bash
git clone https://github.com/depoledna/toolbox.git mcp && cd mcp
uv venv && uv pip install -r pyproject.toml
cp .env.example .env  # add your API keys
./scripts/run_server.sh
```

## Client Setup

### Claude Code

Config: `~/.claude.json` (global) or `.mcp.json` (per-project)

```json
{
  "mcpServers": {
    "toolbox": { "type": "http", "url": "http://localhost:11000/mcp" },
    "pentest": { "type": "http", "url": "http://localhost:11001/mcp" }
  }
}
```

### VS Code — GitHub Copilot

Config: `.vscode/mcp.json` in your workspace

```json
{
  "servers": {
    "toolbox": { "type": "http", "url": "http://localhost:11000/mcp" },
    "pentest": { "type": "http", "url": "http://localhost:11001/mcp" }
  }
}
```

### Cline

Config: `~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json`

```json
{
  "mcpServers": {
    "toolbox": { "url": "http://localhost:11000/mcp" },
    "pentest": { "url": "http://localhost:11001/mcp" }
  }
}
```

### Gemini CLI (stdio)

Gemini does not support HTTP transport yet. Use stdio, which runs the server as a subprocess:

Config: `~/.gemini/settings.json`

```json
{
  "mcpServers": {
    "toolbox": {
      "command": "/path/to/mcp/.venv/bin/fastmcp",
      "args": ["run", "server.py:mcp"],
      "cwd": "/path/to/mcp"
    }
  }
}
```

Stdio bypasses the proxy — no hot-reload, tool file changes require a client restart.

## Tools

### Toolbox Server

**`python_repl`** — persistent Python environment. Variables survive across calls, `await` works natively. Pre-loaded: pandas, numpy, os, sys, json. Use `install_package` to add dependencies. The REPL has access to `library.*` utilities — run `library.man()` to discover available functions.

**`bash`** — shell with server-side risk classification. *Low* (mkdir, git commit) runs normally, *medium* (git reset, package installs) runs with a warning, *high* (git push, rm -rf /) is blocked — must go through the host CLI with human approval. All executions logged to `logs/bash.log`.

**`read_only_bash`** — kernel-enforced read-only shell via macOS `sandbox-exec`. Filesystem writes blocked at the OS level. Use for safe exploration: ls, grep, git log, etc.

**`browser`** — headless Chrome via Playwright. Single tool, action routing: `go`, `click`, `type`, `scroll`, `press`, `eval`, `screenshot`, `close`. Returns ARIA snapshots with `[ref=eN]` element references. For complex pages (iframes, shadow DOM), use AI-powered `act` (natural language) and `extract` (structured data) via Stagehand.

**`ssh`** — persistent interactive shell on remote servers. Actions: `servers`, `connect`, `exec`, `rsync`. Handles CWD tracking, sudo prompts, stalled command detection, auto-reconnect. Each session is logged on the remote host.

**`docs`** — library documentation lookup via Context7. `resolve` finds a library by name, `query` fetches docs. Plain names like "react" are auto-resolved.

**`feedback`** — file bugs, feature requests, and improvements against the toolbox. See [Feedback Pipeline](#feedback-pipeline) below.

### Pentest Server

**`nmap_scan`** — port scanning, service detection, OS fingerprinting. **`nuclei_scan`** — template-based vulnerability scanning. **`nikto_scan`** — web server misconfiguration checks. **`sqlmap_scan`** — SQL injection testing. **`ffuf_fuzz`** / **`gobuster_scan`** / **`feroxbuster_scan`** — content discovery and fuzzing. **`hydra_attack`** — credential brute-force. **`testssl_scan`** — TLS/SSL analysis. Plus: subfinder, dnsx, httpx, whatweb, wafw00f, katana, gau, dalfox, tshark, wpscan.

All pentest tools return structured JSON output and enforce timeouts.

### Library Functions

Not MCP tools — importable in the REPL via `from library import ...`. Run `library.man()` for the full reference.

- `generate_image` / `edit_image` / `generate_icon` — image generation via OpenRouter
- `testflight` — build, archive, and upload to TestFlight in one call
- `asc_api` — authenticated App Store Connect API calls
- `apple_ads_keywords` — keyword research with competitor analysis (headless Chrome)
- `blob_list` / `blob_put` / `blob_get` / `blob_delete` — Vercel Blob storage
- `parse_nmap` / `categorize_hosts` / `pentest_report` — pentest analysis utilities

## Adding Tools

Create a file in `tools/` (or `tools/pentest/`) with a public async function:

```python
async def my_tool(param: str) -> str:
    """Docstring becomes the tool description."""
    return "result"
```

Implementation changes are live on save. New tools or signature changes need an MCP reconnect. Prefix helpers with `_` to exclude them from auto-registration.

## Feedback Pipeline

Agents file feedback when they hit broken tools or need missing features. A watcher auto-fixes bugs and implements approved features.

```
Bug ──► feedback(action="create", type="bug")
           └── watcher picks up → fixer agent → fix → test → resolved

Feature ──► feedback(action="create", type="feature_request")
               └── user approves → watcher picks up → same flow

Improvement ──► feedback(action="create", type="improvement")
                   └── same as feature: approve to trigger
```

The **watcher** (`infra/watcher.py`) polls `feedback.json` every 5s and spawns a guardrailed fixer agent that can only modify `tools/`, `tools/pentest/`, and `library/`. Budget: $1/bug, $3/feature. Each attempt is tracked with approach, test result, and outcome. Failed items can be reopened with new context.

Set `"feedback_agent": false` in `settings.json` to disable. See [`docs/feedback_pipeline.md`](docs/feedback_pipeline.md) for the full spec.

## Logging

- **`logs/bash.log`** — JSONL: every bash command with timestamp, risk level, action (executed/blocked/timed_out), exit code, duration, cwd.
- **SSH session logs** — per-connection log on the remote host (`~/mcp_output/session_*.log`) with commands, output, and exit codes. Auto-cleaned after 7 days.

The `logs/` directory is gitignored.

## Configuration

| File | Purpose |
|------|---------|
| `.env` | API keys (OPENROUTER_KEY, CONTEXT7_API_KEY, etc.) |
| `settings.json` | SSH servers, feedback agent toggle |

Both gitignored. See `.env.example` for required variables.

## License

MIT
