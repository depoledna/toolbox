# Feedback Pipeline

Agents file bugs and feature requests via MCP tools. The watcher detects changes in `feedback.json` and spawns a Claude agent to fix/implement them.

## Flow

```
Agent hits a problem
  │
  ├── Bug ──► feedback(action="create", type="bug") ──► feedback.json (status: open)
  │                                                          │
  │                                            watcher detects new bug (open + no attempts)
  │                                                          │
  │                                            sets in_progress, appends attempt
  │                                            spawns claude --agent feedback-fixer
  │                                                          │
  │                                            fix → restart backend → test via MCP
  │                                                          │
  │                                            resolved ──► feedback.json + feedback_done/FB-xxx.jsonl
  │
  ├── Feature ──► feedback(action="create", type="feature_request") ──► feedback.json
  │                                                          │
  │                                            user reviews, then:
  │                                            feedback(action="update", id=..., status="approved")
  │                                                          │
  │                                            watcher detects "approved" status
  │                                            (same flow as bugs)
  │
  ├── Improvement ──► feedback(action="create", type="improvement") ──► feedback.json
  │                                                          │
  │                                            same as feature: approve to trigger
  │
  └── Reopen ──► feedback(action="update", id=..., status="reopened", description="new context")
                                                          │
                                            watcher detects "reopened" status
                                            enriches prompt with previous attempts
                                            (same processing flow)
```

## Components

| Component | File | Role |
|-----------|------|------|
| Feedback tools | `tools/feedback.py` | CRUD for feedback items. Only writes to `feedback.json`. |
| Watcher | `infra/watcher.py` | Polls `feedback.json` every 5s. Detects actionable items. Spawns Claude CLI. Tracks attempts. Archives logs. |
| Fixer agent | `~/.claude/agents/feedback-fixer.md` | Guardrailed agent: can only touch `tools/` and `library/`. Fixes bug or implements feature, restarts backend, tests via live MCP. Outputs APPROACH/TEST lines. |
| Done archive | `feedback_done/FB-xxx.jsonl` | Full stream-json trace of every fixer agent run. Numbered per attempt. |

## Actionable Statuses

The watcher processes items with these conditions:
- **New bug**: `type == "bug"` AND `status == "open"` AND no previous `attempts`
- **Approved**: `status == "approved"` (feature requests or improvements after user review)
- **Reopened**: `status == "reopened"` (retry with new context, previous attempts included in prompt)

## MCP Tool

Single tool with action routing:

```
feedback(action="create", title=..., description=..., type=...)   → file new item
feedback(action="list")                                           → all items
feedback(action="list", status="open")                            → filtered
feedback(action="get", id="FB-001")                               → full details
feedback(action="update", id="FB-001", status="approved")         → change status
```

## Attempt Tracking

Each feedback item has an `attempts` array. The watcher manages this automatically:

```json
{
  "started_at": "2026-03-14T09:57:09+00:00",
  "finished_at": "2026-03-14T10:02:09+00:00",
  "outcome": "resolved",
  "approach": "Fixed quote escaping in ssh_exec by using shlex.quote()",
  "test_result": "python3 -c 'print(1+1)' via ssh_exec returned '2' successfully",
  "log": "feedback_done/FB-002.jsonl"
}
```

Outcomes: `resolved`, `failed`, `timeout`, `error`

## Fixer Agent Guardrails

- Only modifies files in `tools/` and `library/`
- Never touches infrastructure (server.py, infra/proxy.py, scripts/run_server.sh, etc.)
- Never deletes tool files or changes existing function signatures
- Never adds pip dependencies
- Max 3 fix attempts per run, then gives up
- `--disallowedTools` prevents calling feedback_create or feedback_update
- `--strict-mcp-config` limits to the toolbox MCP server only
- `--max-budget-usd` capped at $1 (bugs) / $3 (features)
- Process group kill on timeout (no orphan processes)
- Must output `APPROACH:` and `TEST:` lines — watcher parses these for the attempt record
- Never calls `scripts/reload.sh` — backends auto-reload on tool file changes via `--reload`

## On/Off Switch

Set `"feedback_agent": false` in `settings.json` to disable processing. The watcher stays alive but skips scanning. Set back to `true` (or remove the key) to re-enable. No restart needed.

## File Locking

`infra/watcher.py` and `tools/feedback.py` both use `fcntl.flock` on `feedback.json.lock` for cross-process safety when writing to `feedback.json`.

## Who Files Feedback

Global CLAUDE.md makes feedback mandatory for all sessions encountering MCP tool or library function issues.

## Reviewing Feedback

```
feedback(action="list", status="open")
feedback(action="get", id="FB-004")
feedback(action="update", id="FB-005", status="approved")
feedback(action="update", id="FB-002", status="reopened", description="Try shlex.quote()")
feedback(action="update", id="FB-005", status="rejected", resolution_notes="not needed")
```

## Gitignored

`feedback.json`, `feedback.json.lock`, `feedback_queue/`, `feedback_logs/`, `feedback_done/` are all gitignored — runtime data only.
