# MCP Toolbox

Two MCP servers from a single codebase: **toolbox** (core) and **pentest** (security tools).

## Tech Stack

- **FastMCP** with `--reload` (auto-restart on tool file changes)
- **UV** for package management
- **Python 3.12**

## Architecture

```
                        toolbox (core)
Claude Code ─SSE:11000─▶ infra/proxy.py ─HTTP:8765─▶ fastmcp --reload server.py
                          (repl, ssh)

                        pentest (security)
Claude Code ─SSE:11001─▶ infra/proxy_pentest.py ─HTTP:8766─▶ fastmcp --reload server_pentest.py
                          (nmap, nikto, nuclei, sqlmap, ffuf, etc.)
```

- **Proxies** (`:11000`, `:11001`) — stable front-ends, survive backend restarts
- **Backends** (`:8765`, `:8766`) — auto-reload on tool file changes

## Project Structure

```
mcp/
├── server.py              # Toolbox: auto-discovers tools/*.py (top-level only)
├── server_pentest.py      # Pentest: auto-discovers tools/pentest/*.py
├── infra/
│   ├── proxy.py           # Toolbox proxy on :11000
│   ├── proxy_pentest.py   # Pentest proxy on :11001
│   ├── repl_worker.py     # Isolated REPL worker process
│   └── watcher.py         # Feedback pipeline watcher
├── scripts/
│   ├── run_server.sh      # Launch all 5 processes
│   ├── reload.sh          # Restart backends (proxies stay up)
│   └── setup_pentest.sh   # Install pentest CLI tools
├── docs/
│   └── feedback_pipeline.md  # Feedback pipeline documentation
├── tools/
│   ├── repl.py            # Python REPL: run, install, vars, clear
│   ├── read_only_bash.py  # Sandboxed shell (kernel-enforced read-only)
│   ├── browser.py         # Browser automation (Playwright + Chrome CDP)
│   ├── ssh.py             # SSH connect/exec on remote servers
│   ├── feedback.py        # Feedback tracking (bugs, features, improvements)
│   └── pentest/           # Security tools (separate MCP server)
│       ├── nmap.py
│       ├── nikto.py
│       ├── nuclei.py
│       └── ...
├── library/
│   ├── generate_image.py  # OpenRouter image generation
│   ├── list_packages.py   # List installed packages
│   ├── vercel_blob.py     # Vercel Blob storage helpers
│   └── xcode.py           # Xcode/TestFlight deployment helper
├── settings.json          # SSH server config (gitignored)
├── .venv/                 # Server environment
├── repl_venv/             # REPL environment (user packages)
├── .env                   # API keys (OPENROUTER_KEY)
└── com.mcp.toolbox.plist  # launchd service (KeepAlive + RunAtLoad)
```

## Hot Reload

**Automatic** — edit any file in `tools/` or `tools/pentest/`, save, next call uses new code.

No restart, no `/mcp` reconnect needed.

**Backend restart only needed for:**
- Changes to `server.py` or `server_pentest.py`

```bash
./scripts/reload.sh              # restart both backends
./scripts/reload.sh toolbox      # restart toolbox only
./scripts/reload.sh pentest      # restart pentest only
```

## Adding New Tools

### Core tool (toolbox server)
Create `tools/new_tool.py` with a public async function:
```python
async def new_tool(param: str) -> str:
    """Docstring becomes the tool description."""
    return "result"
```

### Pentest tool (pentest server)
Create `tools/pentest/new_tool.py` with a public async function.

Convention: prefix private helpers with `_` to exclude them from registration.

## MCP Servers

| Server | Port | Proxy | Tools |
|--------|------|-------|-------|
| toolbox | 8765 | 11000 | repl, read_only_bash, browser, ssh, docs, feedback |
| pentest | 8766 | 11001 | nmap, nikto, nuclei, sqlmap, ffuf, etc. |

## Architecture Details

- **Auto-discovery** — `server.py` scans `tools/*.py`, `server_pentest.py` scans `tools/pentest/*.py`
- **Isolated environments** — Server in `.venv/`, REPL in `repl_venv/`
- **Worker process** — `repl` spawns `infra/repl_worker.py` subprocess
- **Persistent state** — Worker maintains namespace across calls
- **Dynamic config** — SSH reads `settings.json` fresh on each call (no restart for config changes)
- **Reload supervisor** — `fastmcp --reload` spawns a `--stateless --no-reload` child worker; parent watches files and restarts the child on change. Two processes per backend is expected (~37M reloader + ~110M worker each)

## Browser

Headless Chrome automation via Playwright CDP. Single tool with action routing.

```
browser(action="go", url="https://example.com")   → ARIA snapshot with refs
browser(action="click", ref="e5")                  → updated snapshot
browser(action="type", ref="e3", text="hello")     → updated snapshot
browser(action="select", ref="e8", value="opt2")   → updated snapshot
browser(action="scroll", direction="down")          → updated snapshot
browser(action="press", key="Enter")               → updated snapshot
browser(action="back")                             → updated snapshot
browser(action="eval", script="document.title")    → JS result
browser(action="screenshot")                       → /tmp path
browser(action="close")                            → frees resources
```

- Returns ARIA accessibility tree with `[ref=eN]` tags on interactive elements
- Refs are valid until next snapshot — use them for click/type/select
- Session persists across calls (Chrome stays open, 30 min idle timeout)
- Uses Chrome profile from `CHROME_PROFILE_DIR` env var (default: `~/.chrome-profile`)

## Read-Only Bash

Kernel-enforced read-only shell via macOS `sandbox-exec`. Filesystem writes are blocked at the OS level — commands that attempt to write get "Operation not permitted".

- Writes to `/tmp` allowed (needed for pipes and temp files)
- Setuid binaries (e.g. `ps`) blocked by sandbox — expected behavior
- Good for: `ls`, `cat`, `grep`, `find`, `df`, `uname`, `git log`, `whoami`, etc.

## REPL

Actions: `run`, `install`, `vars`, `clear`. Pre-loaded: pandas (pd), numpy (np), os, sys, json.

```
repl(action="run", code="1 + 1")                         → execute code
repl(action="install", package="httpx")                   → install package
repl(action="vars")                                       → list defined variables
repl(action="clear")                                      → reset namespace
```

## SSH

Configured in `settings.json` (gitignored). Key-based auth recommended.

Add servers to `settings.json` with alias, host, user, and optional sudo_password.

## Docs (Context7)

Library documentation lookup via `tools/context7.py`. Single tool with action routing.

```
docs(action="resolve", library="react")                              → matching library IDs
docs(action="query", library="/facebook/react", query="useEffect")   → documentation
docs(action="query", library="react", query="useEffect")             → auto-resolves name, then fetches docs
```

## Library Functions

Not MCP tools — import in repl:

```python
from library.generate_image import generate_image
from library.edit_image import edit_image
from library.list_packages import list_packages
from library.vercel_blob import blob_list, blob_put, blob_get, blob_delete, blob_head
from library.xcode import testflight
```

## Feedback

When an `mcp__toolbox__*` or `mcp__pentest__*` tool errors out, returns wrong results, or you need a capability that doesn't exist yet — file feedback immediately. Don't silently work around it.

| Situation | Action |
|-----------|--------|
| Tool breaks or returns wrong output | `feedback(action="create", type="bug", ...)` |
| You need a tool/function that doesn't exist | `feedback(action="create", type="feature_request", ...)` |
| Tool works but could be better | `feedback(action="create", type="improvement", ...)` |

Bugs are auto-fixed. Features and improvements require user approval first.

Use `feedback(action="list")` and `feedback(action="get", id=...)` to check existing items before filing duplicates.
