import io
import json
import os
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from tools._session import get_client_session_id

_project_root = Path(__file__).parent.parent
_DEFAULT_TIMEOUT = 60
_MAX_NONCE_RETRIES = 20  # max lines to skip looking for our nonce
_IDLE_TTL = 3600  # kill workers idle for 1 hour
_CLEANUP_INTERVAL = 300  # check every 5 minutes


def _clean_terminal_output(text: str) -> str:
    """Simulate terminal \\r behavior to collapse progress bar output."""
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        if "\r" in line:
            segments = line.split("\r")
            last = ""
            for seg in segments:
                if seg.strip():
                    last = seg
            line = last
        cleaned.append(line)
    deduped = []
    prev = None
    for line in cleaned:
        if line != prev or not line.strip():
            deduped.append(line)
        prev = line
    return "\n".join(deduped)


@dataclass
class _WorkerState:
    worker: subprocess.Popen
    response_read: io.TextIOWrapper
    last_used: float = field(default_factory=time.time)


_workers: dict[str, _WorkerState] = {}
_lock = threading.Lock()
_last_cleanup: float = 0.0


def _create_worker() -> _WorkerState:
    """Spawn a fresh REPL worker subprocess."""
    r, w = os.pipe()

    repl_python = _project_root / "repl_venv" / "bin" / "python"
    worker_script = _project_root / "infra" / "repl_worker.py"

    env = os.environ.copy()
    env["_REPL_RESPONSE_FD"] = str(w)

    worker = subprocess.Popen(
        [str(repl_python), str(worker_script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=str(_project_root),
        pass_fds=(w,),
        env=env,
    )

    os.close(w)
    response_read = os.fdopen(r, "r")

    ready = response_read.readline()
    status = json.loads(ready)
    if status.get("status") != "ready":
        raise RuntimeError(f"Worker failed to start: {status}")

    state = _WorkerState(worker=worker, response_read=response_read)

    # Pre-load standard modules and library into the namespace
    init_code = (
        "import os, sys, json\n"
        "sys.path.insert(0, os.getcwd())\n"
        "import pandas as pd, numpy as np\n"
        "import library\n"
    )
    nonce = secrets.token_hex(16)
    cmd = json.dumps({"type": "execute", "code": init_code, "nonce": nonce})
    state.worker.stdin.write(cmd + "\n")
    state.worker.stdin.flush()
    _read_response_with_nonce(state.response_read, nonce, timeout=30)

    return state


def _kill_worker_state(state: _WorkerState) -> None:
    """Kill a worker given its state object."""
    try:
        state.worker.kill()
        state.worker.wait(timeout=5)
    except Exception:
        pass
    try:
        state.response_read.close()
    except Exception:
        pass


def _cleanup_stale_workers() -> None:
    """Kill workers that haven't been used for _IDLE_TTL seconds."""
    now = time.time()
    to_remove: list[tuple[str, _WorkerState]] = []
    with _lock:
        for sid, state in _workers.items():
            if now - state.last_used > _IDLE_TTL or state.worker.poll() is not None:
                to_remove.append((sid, state))
        for sid, _ in to_remove:
            del _workers[sid]

    for _, state in to_remove:
        _kill_worker_state(state)


def _get_worker(session_id: str) -> _WorkerState:
    """Get or start the REPL worker for a specific session."""
    global _last_cleanup

    now = time.time()
    if now - _last_cleanup > _CLEANUP_INTERVAL:
        _last_cleanup = now
        _cleanup_stale_workers()

    dead_state = None
    with _lock:
        state = _workers.get(session_id)
        if state is not None and state.worker.poll() is None:
            state.last_used = now
            return state
        # Remove dead worker if present
        if state is not None:
            _workers.pop(session_id, None)
            dead_state = state

    # Kill outside lock to avoid blocking other sessions
    if dead_state is not None:
        _kill_worker_state(dead_state)

    new_state = _create_worker()

    with _lock:
        # Race check: another thread may have created one
        existing = _workers.get(session_id)
        if existing is not None and existing.worker.poll() is None:
            _kill_worker_state(new_state)
            existing.last_used = time.time()
            return existing
        _workers[session_id] = new_state
        return new_state


def _kill_worker(session_id: str) -> None:
    """Kill the worker for a specific session."""
    with _lock:
        state = _workers.pop(session_id, None)
    if state is not None:
        _kill_worker_state(state)


def _readline_with_timeout(response_read: io.TextIOWrapper, timeout: int) -> str | None:
    """Read a line from the response pipe with a timeout. Returns None on timeout."""
    result = [None]

    def _read():
        try:
            result[0] = response_read.readline()
        except Exception:
            result[0] = None

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        return None
    return result[0]


def _read_response_with_nonce(
    response_read: io.TextIOWrapper, nonce: str, timeout: int
) -> dict | None:
    """Read lines until we find one with matching nonce, or timeout."""
    for _ in range(_MAX_NONCE_RETRIES):
        line = _readline_with_timeout(response_read, timeout)
        if line is None:
            return None  # timeout
        if not line:
            return None  # pipe closed / worker died

        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue  # garbage line, skip

        if data.get("nonce") == nonce:
            return data
        # Wrong nonce — injected by user code, discard and read next

    return None  # too many bad lines


async def python_repl(code: str, timeout: int = _DEFAULT_TIMEOUT) -> str:
    """Execute Python code in a persistent environment.

    Variables persist across calls. Supports sync and async code (use `await` directly).
    Pre-loaded: pandas (pd), numpy (np), os, sys, json, and library.* utilities.
    Use install_package first if you need a package that isn't available.

    Do NOT use for long-running daemons or GUI operations — the worker is killed on timeout.
    Worker state is lost if it crashes or times out (auto-respawns fresh on next call).

    Special commands: %vars (show variables), %clear (reset namespace).

    Args:
        code: Python code to execute
        timeout: Max execution time in seconds (default 60). Worker is killed on timeout.

    Returns:
        stdout/stderr output, last expression result, or error traceback.
    """
    session_id = get_client_session_id()

    try:
        state = _get_worker(session_id)
        nonce = secrets.token_hex(16)

        cmd = json.dumps({"type": "execute", "code": code, "nonce": nonce})
        state.worker.stdin.write(cmd + "\n")
        state.worker.stdin.flush()

        response = _read_response_with_nonce(state.response_read, nonce, timeout)

        if response is None:
            _kill_worker(session_id)
            return f"Error: execution timed out or worker crashed after {timeout}s. Worker restarted (state lost)."

        if "error" in response:
            return f"Error: {response['error']}"

        output = response.get("output", "No output")
        return _clean_terminal_output(output)

    except Exception as e:
        _kill_worker(session_id)
        return f"REPL error: {e}"
