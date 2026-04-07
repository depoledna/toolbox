"""Feedback tracking for the MCP toolbox.

Agents file feedback when they hit broken tools, need missing features,
or have improvement ideas. Humans review and act on them.
"""

import fcntl
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

_FILE = Path(__file__).parent.parent / "feedback.json"
_LOCK_FILE = Path(__file__).parent.parent / "feedback.json.lock"
_ACTIONS = ("create", "list", "get", "update")
_TYPES = ("bug", "feature_request", "improvement")
_STATUSES = ("open", "in_progress", "resolved", "rejected", "approved", "reopened")
_lock = threading.Lock()


def _load() -> dict:
    try:
        return json.loads(_FILE.read_text())
    except FileNotFoundError:
        return {"next_id": 1, "feedbacks": {}}
    except json.JSONDecodeError:
        _FILE.rename(_FILE.with_suffix(".json.bak"))
        return {"next_id": 1, "feedbacks": {}}


def _save(data: dict) -> None:
    with open(_LOCK_FILE, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        tmp = _FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        tmp.replace(_FILE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def feedback(
    action: str,
    title: str = "",
    description: str = "",
    type: str = "",
    context: str = "",
    id: str = "",
    status: str = "",
    resolution_notes: str = "",
) -> str:
    """Track bugs, feature requests, and improvements for MCP tools and library functions.

    Use this when a tool errors out, returns wrong results, or you need a capability
    that doesn't exist yet. Don't silently work around broken tools — leave a trail.

    Actions:
      create — file new feedback (requires title, description, type)
      list   — show all items, optionally filtered by status
      get    — full details for one item by id
      update — change status of an item by id (requires id, status)

    Args:
        action: "create", "list", "get", or "update"
        title: Short summary (create only)
        description: What happened or what you need (create only, or when reopening)
        type: "bug", "feature_request", or "improvement" (create only)
        context: Optional JSON with details, e.g. '{"tool": "ssh", "error": "timeout"}'
        id: Feedback ID, e.g. "FB-001" (get/update)
        status: New status (update only): open, in_progress, resolved, rejected, approved, reopened
        resolution_notes: Optional notes when resolving/rejecting (update only)
    """
    if action not in _ACTIONS:
        return f"Unknown action '{action}'. Use: {', '.join(_ACTIONS)}"

    if action == "create":
        return _create(title, description, type, context)
    if action == "list":
        return _list(status)
    if action == "get":
        return _get(id)
    return _update(id, status, resolution_notes, description)


def _create(title: str, description: str, type: str, context: str) -> str:
    if not title.strip():
        return "Error: title required"
    if not description.strip():
        return "Error: description required"
    if type not in _TYPES:
        return f"Error: type must be one of {_TYPES}"

    ctx = {}
    if context:
        try:
            ctx = json.loads(context)
        except json.JSONDecodeError:
            return "Error: context must be valid JSON"

    now = _now()
    with _lock:
        data = _load()
        fb_id = f"FB-{data['next_id']:03d}"
        data["next_id"] += 1
        data["feedbacks"][fb_id] = {
            "id": fb_id,
            "title": title,
            "description": description,
            "type": type,
            "status": "open",
            "context": ctx,
            "created_at": now,
            "updated_at": now,
            "resolution_notes": None,
            "attempts": [],
        }
        try:
            _save(data)
        except OSError as e:
            return f"Error saving: {e}"

    return f"Created {fb_id}: {title}"


def _list(status_filter: str) -> str:
    if status_filter and status_filter not in _STATUSES:
        return f"Error: status must be one of {_STATUSES}"

    with _lock:
        data = _load()

    items = sorted(data["feedbacks"].values(), key=lambda fb: fb["created_at"], reverse=True)
    if status_filter:
        items = [fb for fb in items if fb["status"] == status_filter]

    if not items:
        return "No feedback items found."

    return "\n".join(f"{fb['id']}  [{fb['type']}/{fb['status']}]  {fb['title']}" for fb in items)


def _get(fb_id: str) -> str:
    if not fb_id:
        return "Error: id required"

    with _lock:
        data = _load()
    fb = data["feedbacks"].get(fb_id.upper())
    if not fb:
        return f"{fb_id} not found."

    lines = [
        f"{fb['id']}  [{fb['type']}/{fb['status']}]  {fb['title']}",
        f"Created: {fb['created_at']}  Updated: {fb['updated_at']}",
        "",
        fb["description"],
    ]
    if fb.get("context"):
        lines += ["", f"Context: {json.dumps(fb['context'])}"]
    if fb.get("resolution_notes"):
        lines += ["", f"Resolution: {fb['resolution_notes']}"]
    if fb.get("attempts"):
        lines += ["", f"Attempts ({len(fb['attempts'])}):"]
        for i, a in enumerate(fb["attempts"], 1):
            lines.append(f"  #{i} [{a.get('outcome', '?')}] {a.get('started_at', '?')}")
            if a.get("approach"):
                lines.append(f"     {a['approach']}")
    return "\n".join(lines)


def _update(fb_id: str, status: str, notes: str, description: str) -> str:
    if not fb_id:
        return "Error: id required"
    if status not in _STATUSES:
        return f"Error: status must be one of {_STATUSES}"

    fb_id = fb_id.upper()
    with _lock:
        data = _load()
        fb = data["feedbacks"].get(fb_id)
        if not fb:
            return f"{fb_id} not found."

        data["feedbacks"][fb_id] = {
            **fb,
            "status": status,
            "updated_at": _now(),
            "resolution_notes": notes or fb.get("resolution_notes"),
            **({"description": description} if description else {}),
        }

        try:
            _save(data)
        except OSError as e:
            return f"Error saving: {e}"

    return f"Updated {fb_id}: status → {status}"
