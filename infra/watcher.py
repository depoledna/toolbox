"""Watch feedback.json and spawn Claude to fix bugs / implement approved features."""

import fcntl
import json
import logging
import os
import re
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
FEEDBACK_FILE = PROJECT_ROOT / "feedback.json"
FEEDBACK_LOCK = PROJECT_ROOT / "feedback.json.lock"
SETTINGS_FILE = PROJECT_ROOT / "settings.json"
DONE_DIR = PROJECT_ROOT / "feedback_done"
POLL_INTERVAL = 5
BUDGETS = {"bug": "1", "feature_request": "3", "improvement": "2"}
TIMEOUTS = {"bug": 600, "feature_request": 600, "improvement": 600}

MCP_CONFIG = json.dumps({
    "mcpServers": {
        "toolbox": {"type": "http", "url": "http://localhost:11000/mcp"},
        "pentest": {"type": "http", "url": "http://localhost:11001/mcp"},
    }
})

logging.basicConfig(
    format="[watcher] %(asctime)s %(message)s",
    level=logging.INFO,
    datefmt="%H:%M:%S",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_feedback() -> dict:
    """Load feedback.json with cross-process lock."""
    with open(FEEDBACK_LOCK, "w") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            return json.loads(FEEDBACK_FILE.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {"next_id": 1, "feedbacks": {}}


def _save_feedback(data: dict) -> None:
    """Atomically write feedback.json with cross-process lock."""
    with open(FEEDBACK_LOCK, "w") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        tmp = FEEDBACK_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        tmp.replace(FEEDBACK_FILE)


def _is_actionable(fb: dict) -> bool:
    """Check if a feedback item should be processed."""
    status = fb.get("status")
    if status in ("approved", "reopened"):
        return True
    if status == "open" and fb.get("type") == "bug" and not fb.get("attempts"):
        return True
    return False


def _archive_log(fb_id: str, stdout: str) -> Path:
    """Save agent log to feedback_done/, numbering by attempt."""
    DONE_DIR.mkdir(exist_ok=True)
    existing = sorted(DONE_DIR.glob(f"{fb_id}*.jsonl"))
    if not existing:
        log_path = DONE_DIR / f"{fb_id}.jsonl"
    else:
        log_path = DONE_DIR / f"{fb_id}_{len(existing) + 1}.jsonl"
    log_path.write_text(stdout)
    return log_path


def _extract_result_text(stdout: str) -> str:
    """Extract the result text from stream-json output."""
    for line in reversed(stdout.strip().splitlines()):
        try:
            msg = json.loads(line)
            if msg.get("type") == "result":
                return msg.get("result", "")[:1000]
        except json.JSONDecodeError:
            continue
    return ""


def _extract_approach_test(text: str) -> tuple[str, str]:
    """Extract APPROACH: and TEST: lines from fixer output."""
    approach = ""
    test = ""
    for line in text.splitlines():
        m = re.match(r"^APPROACH:\s*(.+)", line, re.IGNORECASE)
        if m:
            approach = m.group(1).strip()
        m = re.match(r"^TEST:\s*(.+)", line, re.IGNORECASE)
        if m:
            test = m.group(1).strip()
    return approach, test


def _format_previous_attempts(fb: dict) -> str:
    """Format previous attempts for the fixer prompt."""
    attempts = fb.get("attempts", [])
    if not attempts:
        return "None"
    lines = []
    for i, a in enumerate(attempts, 1):
        outcome = a.get("outcome", "unknown")
        lines.append(f"Attempt #{i}: {outcome}")
        if a.get("approach"):
            lines.append(f"  Approach: {a['approach']}")
        if a.get("test_result"):
            lines.append(f"  Test: {a['test_result']}")
    return "\n".join(lines)


def _build_prompt(fb: dict) -> str:
    """Build a self-contained prompt for the fixer agent."""
    fb_id = fb["id"]
    fb_type = fb["type"]
    prev = _format_previous_attempts(fb)

    return (
        f'<task id="{fb_id}" type="{fb_type}">\n'
        f"<title>{fb['title']}</title>\n"
        f"<description>{fb['description']}</description>\n"
        f"<context>{json.dumps(fb.get('context', {}))}</context>\n"
        f"<previous_attempts>\n{prev}\n</previous_attempts>\n"
        f"</task>\n\n"
        f"Fix this issue. At the end of your work, output EXACTLY these two lines:\n"
        f"APPROACH: <what you changed and why>\n"
        f"TEST: <what you tested and the result>\n"
    )


def _process(fb: dict) -> None:
    """Spawn Claude fixer for a single feedback item."""
    fb_id = fb["id"]
    fb_type = fb["type"]
    logging.info(f"Processing {fb_id}: {fb['title']}")

    # Record attempt start
    attempt = {"started_at": _now(), "outcome": "in_progress", "approach": None, "test_result": None}
    data = _load_feedback()
    stored = data["feedbacks"].get(fb_id)
    if not stored:
        return
    stored.setdefault("attempts", []).append(attempt)
    data["feedbacks"][fb_id] = {**stored, "status": "in_progress", "updated_at": _now()}
    _save_feedback(data)

    prompt = _build_prompt(fb)
    budget = BUDGETS.get(fb_type, "2")
    timeout = TIMEOUTS.get(fb_type, 600)

    cmd = [
        "claude", "-p", "--verbose",
        "--agent", "feedback-fixer",
        "--output-format", "stream-json",
        "--mcp-config", MCP_CONFIG,
        "--strict-mcp-config",
        "--permission-mode", "bypassPermissions",
        "--disallowedTools", "mcp__toolbox__feedback",
        "--no-session-persistence",
        "--max-budget-usd", budget,
        prompt,
    ]

    stdout = ""
    outcome = "failed"
    approach = ""
    test_result = ""

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(PROJECT_ROOT),
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                proc.wait()
            outcome = "timeout"
            logging.error(f"Timeout {fb_id}")
        else:
            result_text = _extract_result_text(stdout)
            approach, test_result = _extract_approach_test(result_text)

            if proc.returncode == 0:
                outcome = "resolved"
                logging.info(f"Resolved {fb_id}")
            else:
                outcome = "failed"
                logging.error(f"Failed {fb_id}: exit {proc.returncode}")

    except Exception as e:
        outcome = "error"
        logging.error(f"Error {fb_id}: {e}")

    # Archive log
    log_path = _archive_log(fb_id, stdout)

    # Update attempt and status
    finished = _now()
    data = _load_feedback()
    stored = data["feedbacks"].get(fb_id)
    if stored:
        attempts = stored.get("attempts", [])
        if attempts:
            attempts[-1] = {
                **attempts[-1],
                "finished_at": finished,
                "outcome": outcome,
                "approach": approach or None,
                "test_result": test_result or None,
                "log": str(log_path.relative_to(PROJECT_ROOT)),
            }

        if outcome == "resolved":
            notes = f"Approach: {approach}\nTest: {test_result}" if approach else f"Log: {log_path.name}"
            data["feedbacks"][fb_id] = {**stored, "status": "resolved", "updated_at": finished, "resolution_notes": notes, "attempts": attempts}
        else:
            notes = f"Auto-fix {outcome}. Log: {log_path.name}"
            data["feedbacks"][fb_id] = {**stored, "status": "open", "updated_at": finished, "resolution_notes": notes, "attempts": attempts}

        _save_feedback(data)


def _is_enabled() -> bool:
    """Check if feedback agent is enabled in settings.json."""
    try:
        settings = json.loads(SETTINGS_FILE.read_text())
        return settings.get("feedback_agent", True)
    except (FileNotFoundError, json.JSONDecodeError):
        return True


def _scan_and_process() -> None:
    """Scan feedback.json for actionable items and process them."""
    try:
        data = json.loads(FEEDBACK_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return

    for fb in data["feedbacks"].values():
        if _is_actionable(fb):
            _process(fb)


def main() -> None:
    DONE_DIR.mkdir(exist_ok=True)
    logging.info("Feedback watcher started")

    while True:
        if _is_enabled():
            _scan_and_process()
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
