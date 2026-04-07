# MCP Toolbox

A collection of MCP tools for Agents. extensible Python REPL, read-only shell access, browser automation, SSH, basic pentest tools. Works with MCP-compatible client.

## How It Works

Tool files can be edited while the server is running without an agent loosing a connection (new signatures require mcp reload as most CLIs cache them). This is ensured by a proxy layer between the agent and the MCP. Agent basically just sees the proxy.

```
MCP Client ──► proxy :11000 ──► toolbox mcp
           ──► proxy :11001 ──► pentest mcp
```

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
    "toolbox": { "type": "http", "url": "http://localhost:11000/mcp" },
    "pentest": { "type": "http", "url": "http://localhost:11001/mcp" }
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
    "toolbox": { "type": "http", "url": "http://localhost:11000/mcp" },
    "pentest": { "type": "http", "url": "http://localhost:11001/mcp" }
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
    "toolbox": { "url": "http://localhost:11000/mcp" },
    "pentest": { "url": "http://localhost:11001/mcp" }
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

> Stdio runs the server as a subprocess, bypassing the proxy. No hot-reload — tool file changes require a client restart.
</details>

## Tools

### Toolbox

| Tool | Description |
|------|-------------|
| **`repl`** | Persistent Python environment with action routing. `run` executes code (variables persist, `await` works natively), `install` adds packages via UV, `vars` lists defined variables, `clear` resets the namespace. Has access to `library.*` utilities — run `library.man()` to discover them. |
| **`read_only_bash`** | Kernel-enforced read-only shell via macOS `sandbox-exec`. Filesystem writes blocked at the OS level. Safe for exploration: ls, grep, git log, etc. |
| **`browser`** | Chrome via Playwright (non healdess so it can avoid bot detection). Actions: `go`, `click`, `type`, `scroll`, `press`, `eval`, `screenshot`, `close`. Returns ARIA snapshots with `[ref=eN]` element references. Supports AI-powered `act` and `extract` via Stagehand for complex pages. |
| **`ssh`** | Persistent interactive shell on remote servers. Actions: `servers`, `connect`, `exec`, `rsync`. Handles CWD tracking, sudo prompts, stalled command detection, and auto-reconnect. |
| **`docs`** | Library documentation lookup via Context7. `resolve` finds a library by name, `query` fetches docs. |
| **`feedback`** | File bugs, feature requests, and improvements against the toolbox. Bugs get processed automatically but another Agent. See [Feedback Pipeline](#feedback-pipeline). |

### Pentest

| Tool | Description |
|------|-------------|
| **`nmap_scan`** | Port scanning, service detection, OS fingerprinting |
| **`nuclei_scan`** | Template-based vulnerability scanning |
| **`nikto_scan`** | Web server misconfiguration checks |
| **`sqlmap_scan`** | SQL injection testing |
| **`ffuf_fuzz`** | Web fuzzing and content discovery |
| **`gobuster_scan`** | Directory and DNS brute-forcing |
| **`feroxbuster_scan`** | Recursive content discovery |
| **`hydra_attack`** | Credential brute-force testing |
| **`testssl_scan`** | TLS/SSL configuration analysis |
| **`subfinder_enum`** | Subdomain enumeration |
| **`dnsx_resolve`** | DNS resolution and probing |
| **`httpx_probe`** | HTTP probing and tech detection |
| **`whatweb_scan`** | Web technology fingerprinting |
| **`wafw00f_detect`** | WAF detection |
| **`katana_crawl`** | Web crawling |
| **`gau_urls`** | Known URL discovery from public sources |
| **`dalfox_xss`** | XSS vulnerability scanning |
| **`tshark_capture`** | Packet capture and analysis |
| **`wpscan_scan`** | WordPress vulnerability scanning |

### Library

Not MCP tools — importable in the REPL via `from library import ...`. Run `library.man()` for the full reference.

| Function | Description |
|----------|-------------|
| `generate_image` / `edit_image` / `generate_icon` | Image generation and editing via OpenRouter |
| `testflight` | Build, archive, and upload to TestFlight in one call |
| `asc_api` | Authenticated App Store Connect API calls |
| `apple_ads_keywords` | Keyword research with competitor analysis (headless Chrome) |
| `blob_list` / `blob_put` / `blob_get` / `blob_delete` | Vercel Blob storage |
| `parse_nmap` / `categorize_hosts` / `pentest_report` | Pentest analysis utilities |

## Feedback Pipeline

Agents file feedback when they hit broken tools or need missing features. A watcher auto-fixes bugs and implements approved features.

Bugs get autoresolved, features require human-in-the-loop.

```
Bug          ──► feedback(action="create", type="bug")
                    └── watcher ──► fixer agent ──► fix ──► test ──► resolved

Feature      ──► feedback(action="create", type="feature_request")
                    └── user approves ──► watcher ──► same flow

Improvement  ──► feedback(action="create", type="improvement")
                    └── user approves ──► watcher ──► same flow
```

The **watcher** (`infra/watcher.py`) polls `feedback.json` every 5s and spawns a guardrailed fixer agent that can only modify `tools/`, `tools/pentest/`, and `library/`. Budget: $1 per bug, $3 per feature. Each attempt is tracked with approach, test result, and outcome.

To disable set `"feedback_agent": false` in `settings.json` to disable. See [`docs/feedback_pipeline.md`](docs/feedback_pipeline.md) for the full spec.

## Configuration

| File | Purpose |
|------|---------|
| `.env` | API keys (`OPENROUTER_KEY`, `CONTEXT7_API_KEY`, etc.) |
| `settings.json` | SSH servers, feedback agent toggle |

Both files are gitignored. See [`.env.example`](.env.example) for all available variables.

### Logging

| Log | Contents |
|-----|----------|
| Remote: `~/mcp_output/session_*.log` | SSH session logs — commands, output, exit codes. Auto-cleaned after 7 days |

## License

MIT
