import asyncio
import os
import signal
from pathlib import Path

_SANDBOX_PROFILE = (
    "(version 1)"
    "(allow default)"
    "(deny file-write*)"
    '(allow file-write* (subpath "/private/tmp"))'
    '(allow file-write* (subpath "/private/var/tmp"))'
    '(allow file-write* (subpath "/private/var/folders"))'
    '(allow file-write* (subpath "/dev"))'
)


async def read_only_bash(command: str, cwd: str = "", timeout: int = 120) -> str:
    """Run a shell command in a kernel-enforced read-only sandbox (macOS sandbox-exec).

    Use for all read-only operations: ls, cat, grep, find, df, git log/diff/status, etc.
    Filesystem writes are blocked at the OS level — commands that attempt to write get
    "Operation not permitted". Writes to /tmp are allowed (needed by pipes and temp files).

    Do NOT use for commands that need write access — use the host CLI's Bash tool instead.
    Note: setuid binaries (e.g. ps) are blocked by the sandbox — this is expected.

    Args:
        command: Shell command to execute
        cwd: Working directory. Empty = current directory.
        timeout: Max seconds (default 120, max 600).

    Returns:
        Combined stdout/stderr with [exit CODE] suffix. Output truncated to last 100K chars.
    """
    if not command.strip():
        return "Error: command must not be empty"

    work_dir = str(Path(cwd).expanduser().resolve()) if cwd else None
    effective_timeout = min(timeout, 600)

    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/sandbox-exec", "-p", _SANDBOX_PROFILE,
            "/bin/bash", "-c", command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=work_dir,
            start_new_session=True,
        )

        timed_out = False
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=effective_timeout)
        except asyncio.TimeoutError:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, _ = await proc.communicate()

        output = stdout.decode("utf-8", errors="replace").strip()

        max_chars = 100_000
        if len(output) > max_chars:
            output = (
                f"[OUTPUT TRUNCATED - showing last {max_chars:,} of {len(output):,} chars]\n\n"
                + output[-max_chars:]
            )

        if timed_out:
            suffix = f"[TIMEOUT after {effective_timeout}s — process killed]"
            return f"{output}\n{suffix}" if output else suffix

        return f"{output}\n[exit {proc.returncode}]" if output else f"[exit {proc.returncode}]"

    except Exception as e:
        return f"Error: {e}"
