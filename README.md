# MCP Toolbox

An MCP toolbox for coding agents: a persistent Python REPL, kernel-sandboxed shell, browser automation, and SSH. All hot-reloadable while the server runs. Includes a feedback loop where the agent files bugs against its own tools and a bounded fixer agent auto-resolves them.

## Highlights

- **Hot-reloadable tools** — edit any tool file; the next agent call uses the new code without a client reconnect.
- **Self-healing feedback loop** — agents file bugs they hit, and a guardrailed fixer agent auto-resolves them.
- **Kernel-sandboxed shell** — read-only enforcement via macOS `sandbox-exec`; filesystem writes are blocked at the OS level. Used for permissionless reads.
- **Parallel browser sessions** — each agent claims a `name=`; gets its own hidden Chrome with an isolated profile clone. `surface` unhides on demand when the user must intervene (login, captcha, etc.).
- **Stable proxy layer** — backend restarts are invisible to clients. No dropped sessions, no mid-task reconnects.

## Quick Start

```bash
git clone https://github.com/depoledna/toolbox.git mcp && cd mcp
uv venv && uv pip install -r pyproject.toml
cp .env.example .env   # add your API keys
./scripts/run_server.sh
```

## Client Setup

<details>
<summary><strong>Claude Code</strong></summary>

Config: `~/.claude.json` (global) or `.mcp.json` (per-project)

```json
{
  "mcpServers": {
    "toolbox": { "type": "http", "url": "http://localhost:11000/mcp" }
  }
}
```
</details>

<details>
<summary><strong>VS Code — GitHub Copilot</strong></summary>

Config: `.vscode/mcp.json` in your workspace

```json
{
  "servers": {
    "toolbox": { "type": "http", "url": "http://localhost:11000/mcp" }
  }
}
```
</details>

<details>
<summary><strong>Cline</strong></summary>

Config: `~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json`

```json
{
  "mcpServers": {
    "toolbox": { "url": "http://localhost:11000/mcp" }
  }
}
```
</details>

<details>
<summary><strong>Gemini CLI</strong> (stdio — no HTTP support yet)</summary>

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

> Stdio runs the server as a subprocess, bypassing the proxy. No hot reload — tool file changes require a client restart.
</details>

## Architecture

The toolbox server sits behind a stable proxy so backend reloads never surface to the client.

```
Client ─SSE:11000─▶ proxy ─HTTP:8765─▶ fastmcp --reload ──► tools/*.py
```

**Why a proxy?** `fastmcp --reload` restarts the backend worker whenever a tool file changes. Without the proxy, the client would see every reload as a dropped connection. The proxy terminates the client session and forwards to whichever backend worker is currently up — so you can edit tool files mid-conversation without breaking the agent's session. Only changes to tool *signatures* require an `/mcp` reload on the client, since most clients cache them.

**Process layout**
- Server code runs in `.venv/`
- The REPL runs in a separate subprocess against `repl_venv/`, so user `pip install`s never contaminate the server environment
- SSH, browser, and REPL state persist across tool calls; only a backend restart clears them
- Two processes per backend is expected: the reload supervisor (~37 MB) and the worker (~110 MB)

## Feedback Pipeline

Agents file feedback when they hit a broken tool or need a missing capability. A watcher polls the feedback file and spawns a bounded fixer agent.

Bugs are auto-resolved. Features and improvements require human approval first.

```
Bug          ──► feedback(action="create", type="bug")
                    └── watcher ──► fixer agent ──► fix ──► test ──► resolved

Feature      ──► feedback(action="create", type="feature_request")
                    └── user approves ──► watcher ──► same flow

Improvement  ──► feedback(action="create", type="improvement")
                    └── user approves ──► watcher ──► same flow
```

**Guardrails**
- The fixer can only modify `tools/` and `library/`
- Budget: $1 per bug, $3 per feature — attempts halt at the cap
- Each attempt logs its approach, test result, and outcome, so retries don't repeat the same failed fix

To disable, set `"feedback_agent": false` in `settings.json`. See [`docs/feedback_pipeline.md`](docs/feedback_pipeline.md) for the full spec.

## Tool Reference

### Toolbox

| Tool | Description |
|------|-------------|
| **`repl`** | Persistent Python environment with action routing. `run` executes code (variables persist, `await` works natively), `install` adds packages via UV, `vars` lists defined variables, `clear` resets the namespace. Has access to `library.*` utilities — run `library.man()` to discover them. |
| **`read_only_bash`** | Kernel-enforced read-only shell via macOS `sandbox-exec`. Filesystem writes blocked at the OS level. Safe for exploration: `ls`, `grep`, `git log`, etc. |
| **`browser`** | Chrome via Playwright, one isolated browser per `name=` (required on every call). Actions: `go`, `click`, `type`, `select`, `scroll`, `press`, `back`, `forward`, `refresh`, `eval`, `screenshot`, `act`, `extract`, `surface`, `close`. Returns ARIA snapshots with `[ref=eN]` element references. Windows launch hidden (macOS app-hide via `NSRunningApplication`); `surface` unhides on the user's primary display with a "Done" overlay and re-hides when finished. AI-powered `act` / `extract` via Stagehand for iframes and shadow DOM. |
| **`ssh`** | Persistent interactive shell on remote servers. Actions: `servers`, `connect`, `exec`, `rsync`. Handles CWD tracking, sudo prompts, stalled command detection, and auto-reconnect. |
| **`docs`** | Library documentation lookup via Context7. `resolve` finds a library by name, `query` fetches docs. |
| **`feedback`** | File bugs, feature requests, and improvements against the toolbox. Bugs are resolved automatically by a fixer agent; features and improvements require human approval first. See [Feedback Pipeline](#feedback-pipeline). |

### Library

Not MCP tools — importable in the REPL via `from library import ...`. Run `library.man()` for the full reference.

| Function | Description |
|----------|-------------|
| `generate_image` / `edit_image` / `generate_icon` | Image generation and editing via OpenRouter |
| `testflight` | Build, archive, and upload to TestFlight in one call |
| `asc_api` | Authenticated App Store Connect API calls |
| `apple_ads_keywords` | Keyword research with competitor analysis (headless Chrome) |
| `blob_list` / `blob_put` / `blob_get` / `blob_delete` | Vercel Blob storage |

## Configuration

| File | Purpose |
|------|---------|
| `.env` | API keys (`OPENROUTER_KEY`, `CONTEXT7_API_KEY`, etc.) |
| `settings.json` | SSH servers, feedback agent toggle |

Both files are gitignored. See [`.env.example`](.env.example) for all available variables.

SSH sessions are logged remotely to `~/mcp_output/session_*.log` — commands, output, exit codes. Auto-cleaned after 7 days.

## License

MIT
