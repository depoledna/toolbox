# MCP Toolbox

Single MCP server: core dev tools (repl, ssh, browser, docs, feedback).

## Tech Stack

- **FastMCP** with `--reload` (auto-restart on tool file changes)
- **UV** for package management
- **Python 3.12**

## Architecture

```
Claude Code ─SSE:11000─▶ infra/proxy.py ─HTTP:8765─▶ fastmcp --reload server.py
                          (repl, ssh, browser, docs, feedback)
```

- **Proxy** (`:11000`) — stable front-end, survives backend restarts
- **Backend** (`:8765`) — auto-reloads on tool file changes

## Project Structure

```
mcp/
├── server.py              # Auto-discovers tools/*.py (top-level only)
├── infra/
│   ├── proxy.py           # Stable proxy on :11000
│   ├── repl_worker.py     # Isolated REPL worker process
│   └── watcher.py         # Feedback pipeline watcher
├── scripts/
│   ├── run_server.sh      # Launch backend + proxy + watcher
│   └── reload.sh          # Restart backend (proxy stays up)
├── docs/
│   └── feedback_pipeline.md  # Feedback pipeline documentation
├── tools/
│   ├── repl.py            # Python REPL: run, install, vars, clear
│   ├── read_only_bash.py  # Sandboxed shell (kernel-enforced read-only)
│   ├── browser.py         # Browser automation (Playwright + Chrome CDP)
│   ├── ssh.py             # SSH connect/exec on remote servers
│   └── feedback.py        # Feedback tracking (bugs, features, improvements)
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

**Automatic** — edit any file in `tools/`, save, next call uses new code.

No restart, no `/mcp` reconnect needed.

**Backend restart only needed for changes to `server.py`:**

```bash
./scripts/reload.sh
```

## Adding New Tools

Create `tools/new_tool.py` with a public async function:
```python
async def new_tool(param: str) -> str:
    """Docstring becomes the tool description."""
    return "result"
```

Convention: prefix private helpers with `_` to exclude them from registration.

## MCP Server

| Server | Port | Proxy | Tools |
|--------|------|-------|-------|
| toolbox | 8765 | 11000 | repl, read_only_bash, browser, ssh, docs, feedback |

## Architecture Details

- **Auto-discovery** — `server.py` scans `tools/*.py`
- **Isolated environments** — Server in `.venv/`, REPL in `repl_venv/`
- **Worker process** — `repl` spawns `infra/repl_worker.py` subprocess
- **Persistent state** — Worker maintains namespace across calls
- **Dynamic config** — SSH reads `settings.json` fresh on each call (no restart for config changes)
- **Reload supervisor** — `fastmcp --reload` spawns a `--stateless --no-reload` child worker; parent watches files and restarts the child on change. Two processes are expected (~37M reloader + ~110M worker)

## Browser

Hidden Chrome automation via Playwright CDP. Single tool with action routing.

**Every call requires `name=`** — a task-specific session label. Each name gets its
own isolated Chrome (own profile clone, own page, own refs). Parallel agents pick
distinct names so they don't collide. Same name reuses the same browser.

```
browser(action="go",   name="alice", url="https://example.com")  → ARIA snapshot
browser(action="click", name="alice", ref="e5")                   → updated snapshot
browser(action="type",  name="alice", ref="e3", text="hello")     → updated snapshot
browser(action="select", name="alice", ref="e8", value="opt2")    → updated snapshot
browser(action="scroll", name="alice", direction="down")           → updated snapshot
browser(action="press",  name="alice", key="Enter")                → updated snapshot
browser(action="back",   name="alice")                             → updated snapshot
browser(action="eval",   name="alice", script="document.title")    → JS result
browser(action="screenshot", name="alice")                          → /tmp path
browser(action="surface", name="alice", text="please log in")       → unhide + wait
browser(action="close",   name="alice")                             → frees resources
```

- `name`: 1-32 chars, `[a-zA-Z0-9_-]`. Required on every call.
- Returns ARIA accessibility tree with `[ref=eN]` tags on interactive elements
- Refs are valid until next snapshot — use them for click/type/select
- Session persists across calls (Chrome stays open, 3 min idle timeout)
- Windows launch hidden by default (macOS app-hide; Stage Manager respects this).
  Set `BROWSER_VISIBLE=1` to keep them visible.
- `surface` unhides the named lane on the user's primary display, shows a Done
  overlay with `text` as prompt, and re-hides on completion.
- Profile cloned per session from `CHROME_PROFILE_DIR` (default `~/.chrome-profile`).
  Cookies/logins propagate from the base profile but each lane is isolated thereafter.
- 2-day autoclean removes any orphaned profile dirs left by crashes/restarts.

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

```
ssh(action="connect", server="HOME")                         → establish connection
ssh(action="exec", command="ls -la", timeout=30)             → run command (waits full timeout, no stall)
ssh(action="exec", command="Y")                              → send input to waiting command
ssh(action="exec", command="make build", background=True)    → run in background, returns job ID
ssh(action="jobs")                                           → list background jobs
ssh(action="jobs", command="1234567")                        → tail output of job
ssh(action="jobs", command="1234567", force=True)            → kill background job
ssh(action="exec", force=True)                               → abort running command
```

- **Interactive commands** wait the full timeout. Input prompts (`[Y/n]`, `password:`, etc.) are auto-detected and surfaced immediately — no polling loop needed.
- **Background jobs** (`background=True`) run server-side with output in `~/mcp_output/job_*.log`. Survive connection drops and MCP restarts. Use `action="jobs"` to manage.
- **Auto-responses**: pass `responses='{"Continue?": "Y"}'` to auto-answer known prompts.

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

When an `mcp__toolbox__*` tool errors out, returns wrong results, or you need a capability that doesn't exist yet — file feedback immediately. Don't silently work around it.

| Situation | Action |
|-----------|--------|
| Tool breaks or returns wrong output | `feedback(action="create", type="bug", ...)` |
| You need a tool/function that doesn't exist | `feedback(action="create", type="feature_request", ...)` |
| Tool works but could be better | `feedback(action="create", type="improvement", ...)` |

Bugs are auto-fixed. Features and improvements require user approval first.

Use `feedback(action="list")` and `feedback(action="get", id=...)` to check existing items before filing duplicates.
