"""Per-item feedback event log. Plain-text, append-only, one file per FB-xxx."""

from datetime import datetime, timezone
from pathlib import Path

_DIR = Path(__file__).parent.parent / "feedback_events"


def emit(fb_id: str, event: str, summary: str = "", details: dict | None = None) -> None:
    """Append one event to feedback_events/FB-xxx.log. Never raises.

    Line format:
        [<ISO8601>] <event> — <summary>
            key1: value1     (only if details provided)
            key2: value2
    """
    try:
        _DIR.mkdir(exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = f"[{ts}] {event}"
        if summary:
            line += f" — {summary}"
        if details:
            for k, v in details.items():
                line += f"\n    {k}: {v}"
        with open(_DIR / f"{fb_id}.log", "a") as fh:
            fh.write(line + "\n")
    except (OSError, TypeError, ValueError):
        pass
