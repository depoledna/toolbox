"""Full bash tool with server-side risk classification and execution logging."""

import asyncio
import json
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

from tools._bash_patterns import classify

_LOG_PATH = Path.home() / "Dev" / "mcp" / "logs" / "bash.log"


def _log(command, risk, reason, action, exit_code=None, duration_ms=None, cwd=None):
    """Append a JSONL entry to the bash execution log."""
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "command": command[:500],
        "risk": risk,
        "reason": reason,
        "action": action,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "cwd": cwd,
    })
    with open(_LOG_PATH, "a") as f:
        f.write(entry + "\n")


async def _read_startup(proc, seconds=3):
    """Read initial output from a backgrounded process, then return."""
    chunks = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=remaining)
            if not chunk:
                break
            chunks.append(chunk.decode("utf-8", errors="replace"))
        except asyncio.TimeoutError:
            break
    return "".join(chunks).strip()


async def bash(
    command: str,
    cwd: str = "",
    timeout: int = 120,
) -> str:
    """Run a shell command with full write access and server-side risk classification.

    Use for commands that modify files: git commit, mkdir, npm build, file creation, etc.
    Do NOT use for read-only operations — use read_only_bash instead (kernel-enforced sandbox).

    Risk levels (classified server-side, deterministic):
      low: runs normally (mkdir, git commit, builds, file writes)
      medium: runs with warning prefix (git reset --hard, package installs, recursive deletes)
      high: blocked (git push, rm -rf /, disk ops) — use the main Bash tool with human approval

    All executions are logged to ~/Dev/mcp/logs/bash.log. Background commands (ending with &)
    return after 3s of startup output.

    Args:
        command: Shell command to execute.
        cwd: Working directory. Empty = current directory.
        timeout: Max seconds (default 120, max 600).

    Returns:
        Command output with [exit CODE] suffix. Output truncated to last 100K chars if exceeded.
        Medium-risk commands include a [MEDIUM: reason] prefix. High-risk commands return BLOCKED.
    """
    if not command.strip():
        return "Error: command must not be empty"

    level, reason = classify(command)

    if level == "high":
        _log(command, level, reason, "blocked", cwd=cwd or None)
        return f"BLOCKED: {reason}. Use the main Bash tool with human approval."

    work_dir = Path(cwd).expanduser().resolve() if cwd else None
    if work_dir and not work_dir.is_dir():
        return f"Error: working directory does not exist: {work_dir}"
    work_dir = str(work_dir) if work_dir else None
    effective_timeout = min(timeout, 600)
    is_background = command.rstrip().endswith("&")
    start = time.monotonic()

    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", "-c", command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=work_dir,
            start_new_session=True,
        )

        if is_background:
            output = await _read_startup(proc, seconds=3)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            _log(command, level, reason, "executed", duration_ms=elapsed_ms, cwd=work_dir)
            return f"{output}\n[backgrounded]" if output else "[backgrounded]"

        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=effective_timeout)
        output = stdout.decode("utf-8", errors="replace").strip()

        max_chars = 100_000
        if len(output) > max_chars:
            output = (
                f"[OUTPUT TRUNCATED - showing last {max_chars:,} of {len(output):,} chars]\n\n"
                + output[-max_chars:]
            )

        result = f"{output}\n[exit {proc.returncode}]" if output else f"[exit {proc.returncode}]"

        if level == "medium":
            result = f"[MEDIUM: {reason}]\n{result}"

        elapsed_ms = int((time.monotonic() - start) * 1000)
        _log(command, level, reason, "executed", proc.returncode, elapsed_ms, work_dir)
        return result

    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()
        elapsed_ms = int((time.monotonic() - start) * 1000)
        _log(command, level, reason, "timed_out", duration_ms=elapsed_ms, cwd=work_dir)
        return f"[TIMEOUT after {effective_timeout}s — process killed]"
    except Exception as e:
        return f"Error: {e}"
