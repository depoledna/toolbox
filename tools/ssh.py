import asyncio
import base64
import json
import logging
import os
import re
import shlex
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from socket import timeout as socket_timeout
from typing import Optional

import paramiko

from tools._session import get_client_session_id

# --- Config ---
_project_root = Path(__file__).parent.parent
_settings_path = _project_root / "settings.json"

_PROMPT_MARKER = "__MCP_END__"
_SUDO_PATTERN = re.compile(r"\[sudo\] password for \w+:|Password:|authenticate\] Password:")
_INPUT_PROMPT_PATTERN = re.compile(
    r"(\[Y/n\]|\[y/N\]|\(yes/no\)|\(y/n\)|password\s*:|Continue\?\s|Enter .*:|Press .* to continue)",
    re.IGNORECASE | re.MULTILINE,
)
_INPUT_SILENCE_WINDOW = 3.0  # seconds of no output after pattern match

_ANSI_ESCAPE = re.compile(r"""
    \x1b       # ESC character
    (?:
        \[ [?]? [0-9;]* [a-zA-Z] # CSI sequences: ESC[...letter (incl. private mode ESC[?...)
    |   \] [^\x07\x1b]* (?:\x07|\x1b\\)  # OSC sequences: ESC]...BEL or ESC]...ST
    |   \( [A-Z]                 # Character set: ESC(X
    |   [>=78DEHM]               # Other: ESC> ESC= ESC7 ESC8 ESCD ESCE ESCH ESCM
    )
""", re.VERBOSE)


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return _ANSI_ESCAPE.sub("", text)


def _load_settings():
    """Load settings fresh from disk (called on each tool invocation)."""
    if _settings_path.exists():
        with open(_settings_path) as f:
            return json.load(f)
    return {}


def _get_servers():
    return _load_settings().get("SSH_SERVERS", {})


def _get_timeout_hours():
    return _load_settings().get("SSH_CONNECTION_TIMEOUT_HOURS", 6)


def _get_max_output_chars():
    return _load_settings().get("SSH_MAX_OUTPUT_CHARS", 100000)


_OUTPUT_DIR = "~/mcp_output"
_CLEANUP_DAYS = 5
_TAIL_LINES = 200
_IDLE_TTL = 3600  # disconnect sessions idle for 1 hour
_CLEANUP_INTERVAL = 300


# --- Per-Session State ---

@dataclass
class _SSHState:
    client: Optional[paramiko.SSHClient] = None
    channel: Optional[paramiko.Channel] = None
    cwd: Optional[str] = None
    current_alias: Optional[str] = None
    connection_time: Optional[float] = None
    sudo_password: Optional[str] = None
    waiting_for_input: bool = False
    session_log: Optional[str] = None
    last_command: Optional[str] = None
    last_used: float = field(default_factory=time.time)
    # Background reader — persistent across polls, captures channel output
    _bg_reader_thread: Optional[threading.Thread] = None
    _bg_reader_stop: Optional[threading.Event] = None
    _bg_reader_buffer: str = ""
    _bg_reader_prompt_found: bool = False
    _bg_reader_input_detected: bool = False
    _bg_reader_output_snapshot: str = ""  # tracks what was returned to agent last
    _bg_reader_lock: threading.Lock = field(default_factory=threading.Lock)


_sessions: dict[str, _SSHState] = {}
_lock = threading.Lock()
_last_cleanup: float = 0.0


def _get_or_create_state(session_id: str) -> _SSHState:
    """Get or create SSH state for a session."""
    global _last_cleanup

    now = time.time()
    if now - _last_cleanup > _CLEANUP_INTERVAL:
        _last_cleanup = now
        _cleanup_stale_sessions()

    with _lock:
        state = _sessions.get(session_id)
        if state is None:
            state = _SSHState()
            _sessions[session_id] = state
        state.last_used = now
        return state


def _cleanup_stale_sessions() -> None:
    """Disconnect SSH sessions idle for > _IDLE_TTL."""
    now = time.time()
    to_remove: list[tuple[str, _SSHState]] = []
    to_reset: list[_SSHState] = []
    with _lock:
        for sid, state in _sessions.items():
            if now - state.last_used > _IDLE_TTL:
                to_remove.append((sid, state))
            elif state.client is not None:
                transport = state.client.get_transport()
                if transport is None or not transport.is_active():
                    to_reset.append(state)
        for sid, _ in to_remove:
            del _sessions[sid]

    for _, state in to_remove:
        _disconnect(state)

    for state in to_reset:
        _stop_background_reader(state)
        if state.channel:
            try:
                state.channel.close()
            except Exception:
                pass
            state.channel = None
        if state.client:
            try:
                state.client.close()
            except Exception:
                pass
            state.client = None


# --- Connection Helpers ---

def _init_session_log(state: _SSHState) -> None:
    """Create ~/mcp_output/ on remote and set session_log path for this session."""
    if state.client is None:
        return
    try:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        remote_dir = _OUTPUT_DIR
        state.client.exec_command(f"mkdir -p {remote_dir}")
        state.client.exec_command(
            f"find {remote_dir} -name '*.log' -mtime +{_CLEANUP_DAYS} -delete 2>/dev/null"
        )
        state.session_log = f"{remote_dir}/session_{timestamp}.log"
        state.client.exec_command(
            f"echo '=== MCP SSH Session {timestamp} ===' > {state.session_log}"
        )
        logging.info(f"Session log initialized: {state.session_log}")
    except Exception as e:
        logging.warning(f"Failed to init session log: {e}")
        state.session_log = None


def _log_to_remote(state: _SSHState, command: str, output: str, exit_code: int) -> None:
    """Append command, output, and exit code to the remote session log."""
    if state.client is None or state.session_log is None:
        return
    try:
        timestamp = time.strftime("%H:%M:%S")
        log_entry = f"[{timestamp}] $ {command}\nexit: {exit_code}\n---\n"
        safe_output = output.replace("'", "'\\''")
        state.client.exec_command(
            f"cat >> {state.session_log} << 'MCPLOGEOF'\n{log_entry}{safe_output}\n===\nMCPLOGEOF"
        )
    except Exception as e:
        logging.warning(f"Failed to log to remote: {e}")


def _establish_connection(state: _SSHState, alias: str, cfg: dict, retries: int = 3) -> None:
    """Create SSH client connection with retries."""
    host = cfg["host"]
    user = cfg["user"]
    port = cfg.get("port", 22)
    password = cfg.get("password")
    key_file = cfg.get("key_file")

    if key_file:
        key_file = os.path.expanduser(key_file)

    last_error = None
    for attempt in range(1, retries + 1):
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            connect_kwargs = {
                "hostname": host,
                "username": user,
                "port": port,
                "timeout": 15,
            }
            if key_file:
                connect_kwargs["key_filename"] = key_file
            elif password:
                connect_kwargs["password"] = password

            client.connect(**connect_kwargs)

            state.client = client
            state.current_alias = alias
            state.connection_time = time.time()
            logging.info(f"SSH connected to {alias} ({host}) on attempt {attempt}")
            return
        except Exception as e:
            last_error = e
            logging.warning(f"SSH connection attempt {attempt}/{retries} to {alias} failed: {e}")
            if attempt < retries:
                time.sleep(1)

    raise ConnectionError(f"Failed to connect to {alias} after {retries} attempts: {last_error}")


def _open_shell(state: _SSHState) -> None:
    """Open an interactive shell channel and set up prompt detection.

    Bootstraps a clean bash with marker PS1 atomically in a single line.
    Previously this was two stages (exec bash, then send init_cmd after a
    fixed 0.3s sleep). On slower hosts (e.g. macOS bash 3.2), the sleep
    was insufficient and the init_cmd arrived before bash had finished
    replacing the outer shell, leaving the session wedged in PS2 mode
    with PS1 never applied. Sending everything in one line eliminates
    that race window entirely — the outer shell reads the whole line
    before exec'ing, so there is no partial-input window.
    """
    state.channel = state.client.invoke_shell(term="dumb", width=200, height=50)
    state.channel.settimeout(0.1)

    # Configure shell for reliable prompt detection.
    #
    # Critical: the marker value (_PROMPT_MARKER) MUST NOT appear literally in
    # the command text. On PTYs where echo is briefly still on (e.g. bash 3.2
    # on macOS before `stty -echo` has taken effect), the kernel echoes the
    # init line into the output stream. If that echoed line contains the
    # marker, _read_until_prompt will sync on the echo rather than the real
    # prompt — leaving PS1 effectively unset and the shell wedged.
    #
    # We rebuild the marker at runtime via printf, so the command text only
    # ever contains "__MCP%sEND__" (which does NOT match _PROMPT_MARKER).
    #
    # We wrap everything in a single `exec bash -c '<init>; exec bash'`
    # bootstrap. The outer shell parses the full line before exec'ing, so
    # there's no race. The inner `-c` bash runs the init, exports PS1 /
    # PS2 / TERM, sets stty, then exec-replaces itself with an interactive
    # bash that inherits all the exports (and whose first prompt is our
    # marker).
    marker_expr = "\"$(printf '__MCP%sEND__' '_')\""
    init_cmd = (
        "stty -echo -icanon 2>/dev/null; "
        "unset PROMPT_COMMAND; "
        "export PS2=''; "
        "export TERM=dumb; "
        f"export PS1={marker_expr}; "
        "exec bash --norc --noprofile --noediting"
    )
    bootstrap = f"exec bash --norc --noprofile --noediting -c {shlex.quote(init_cmd)}\n"
    state.channel.sendall(bootstrap)

    # Read until we see the prompt marker (shell is ready). Give the
    # remote side up to 15s to finish exec'ing + printing the first
    # prompt — slower than strictly necessary on fast hosts, but
    # covers macOS bash 3.2 / loaded machines without flakiness.
    _, completed = _read_until_prompt(state, timeout=15)
    if not completed:
        raise RuntimeError("Shell initialization timed out — prompt marker never received")

    state.waiting_for_input = False
    _init_session_log(state)


def _read_until_prompt(
    state: _SSHState, timeout: int = 30, responses: dict = None
) -> tuple[str, bool]:
    """
    Read channel output until prompt marker appears or hard timeout hits.

    Used for internal reads (shell init, exit code, pwd). User commands use the
    persistent background reader + _poll_reader instead.

    Returns:
        (output, completed) — completed is True if prompt marker was found.
    """

    buffer = ""
    start = time.time()
    sudo_sent = False

    while time.time() - start < timeout:
        try:
            chunk = state.channel.recv(65536).decode("utf-8", errors="replace")
            if chunk:
                buffer += chunk

                clean = _strip_ansi(buffer)

                if not sudo_sent and state.sudo_password and _SUDO_PATTERN.search(clean):
                    state.channel.sendall(state.sudo_password + "\n")
                    sudo_sent = True

                if responses:
                    for pattern, response in list(responses.items()):
                        if pattern.lower() in clean.lower():
                            state.channel.sendall(response + "\n")
                            responses.pop(pattern)

                if _PROMPT_MARKER in buffer:
                    break
        except socket_timeout:
            pass

        time.sleep(0.05)
    else:
        return _strip_ansi(buffer) + f"\n[TIMEOUT after {timeout}s - command may still be running]", False

    idx = buffer.find(_PROMPT_MARKER)
    if idx != -1:
        buffer = buffer[:idx]

    return _strip_ansi(buffer), True


# --- Background Reader ---

def _start_background_reader(state: _SSHState, responses: dict = None) -> None:
    """Start a persistent daemon thread to read channel output.

    The reader stays alive across polls — it is only stopped when the command
    completes (prompt marker found) or is force-aborted. Handles sudo passwords,
    auto-responses, and input prompt detection inside the thread.
    """
    if state._bg_reader_thread is not None:
        _stop_background_reader(state)

    stop_event = threading.Event()
    state._bg_reader_stop = stop_event
    state._bg_reader_buffer = ""
    state._bg_reader_prompt_found = False
    state._bg_reader_input_detected = False
    state._bg_reader_output_snapshot = ""

    channel = state.channel
    client = state.client
    session_log = state.session_log
    last_command = state.last_command
    sudo_password = state.sudo_password
    lock = state._bg_reader_lock
    # Copy responses so only the reader thread mutates it
    resp_map = dict(responses) if responses else {}

    def _reader_loop():
        last_log_time = time.time()
        logged_up_to = 0
        sudo_sent = False
        input_pattern_time = 0.0  # reader-thread-local, no lock needed

        while not stop_event.is_set():
            try:
                chunk = channel.recv(65536).decode("utf-8", errors="replace")
                if chunk:
                    with lock:
                        prev_len = len(state._bg_reader_buffer)
                        state._bg_reader_buffer += chunk
                        # Reset input detection on new data — the silence window restarts
                        state._bg_reader_input_detected = False
                        input_pattern_time = 0.0

                        if _PROMPT_MARKER in chunk or (
                            prev_len > 0 and _PROMPT_MARKER in state._bg_reader_buffer[max(0, prev_len - len(_PROMPT_MARKER)):]
                        ):
                            state._bg_reader_prompt_found = True
                            return

                    # Only scan the tail for pattern matching (avoid O(n^2))
                    scan_start = max(0, prev_len - 200)
                    clean_tail = _strip_ansi(state._bg_reader_buffer[scan_start:])

                    # Sudo handling (sendall is thread-safe on paramiko channels)
                    if not sudo_sent and sudo_password and _SUDO_PATTERN.search(clean_tail):
                        channel.sendall(sudo_password + "\n")
                        sudo_sent = True

                    # Auto-responses
                    if resp_map:
                        for pattern in list(resp_map):
                            if pattern.lower() in clean_tail.lower():
                                channel.sendall(resp_map.pop(pattern) + "\n")

                    # Check for input prompt pattern — mark time, wait for silence
                    if _INPUT_PROMPT_PATTERN.search(clean_tail):
                        input_pattern_time = time.time()

            except socket_timeout:
                # No data available — check if silence window elapsed for input detection
                if input_pattern_time > 0 and (time.time() - input_pattern_time) >= _INPUT_SILENCE_WINDOW:
                    with lock:
                        if not state._bg_reader_prompt_found:
                            state._bg_reader_input_detected = True
                    input_pattern_time = 0.0  # don't re-trigger until new data

            except Exception as e:
                logging.debug(f"Background reader exited: {e}")
                return

            # Log intermediate output to remote session log every 30s
            now = time.time()
            if now - last_log_time >= 30 and session_log and client:
                with lock:
                    new_output = state._bg_reader_buffer[logged_up_to:]
                    current_len = len(state._bg_reader_buffer)
                if new_output.strip():
                    try:
                        safe = _strip_ansi(new_output).replace("'", "'\\''")
                        ts = time.strftime("%H:%M:%S")
                        client.exec_command(
                            f"cat >> {session_log} << 'MCPLOGEOF'\n"
                            f"[{ts}] (running: {last_command})\n{safe}\nMCPLOGEOF"
                        )
                        logged_up_to = current_len
                    except Exception:
                        pass
                last_log_time = now

    thread = threading.Thread(target=_reader_loop, daemon=True)
    thread.start()
    state._bg_reader_thread = thread


def _stop_background_reader(state: _SSHState) -> tuple[str, bool]:
    """Stop background reader and return (buffered_output, prompt_found)."""
    if state._bg_reader_thread is None:
        return "", False

    if state._bg_reader_stop is not None:
        state._bg_reader_stop.set()
    if state._bg_reader_thread.is_alive():
        state._bg_reader_thread.join(timeout=2.0)
        if state._bg_reader_thread.is_alive():
            logging.warning("Background reader did not stop in time")

    with state._bg_reader_lock:
        buffered = state._bg_reader_buffer
        prompt_found = state._bg_reader_prompt_found

    # Strip prompt marker if present, always strip ANSI
    if prompt_found:
        idx = buffered.find(_PROMPT_MARKER)
        if idx != -1:
            buffered = buffered[:idx]
    buffered = _strip_ansi(buffered)

    state._bg_reader_thread = None
    state._bg_reader_stop = None
    state._bg_reader_buffer = ""
    state._bg_reader_prompt_found = False
    state._bg_reader_input_detected = False
    state._bg_reader_output_snapshot = ""

    return buffered, prompt_found


def _get_valid_channel(state: _SSHState):
    """Return an active shell channel, reconnecting if stale."""
    if state.current_alias is None:
        raise ConnectionError("No SSH connection. Use action='connect' first.")

    if state.client is None:
        logging.info(f"SSH client to {state.current_alias} is gone, auto-reconnecting.")
        _reconnect(state)
        return state.channel

    elapsed_hours = (time.time() - state.connection_time) / 3600
    if elapsed_hours > _get_timeout_hours():
        logging.info(f"SSH connection to {state.current_alias} timed out, reconnecting.")
        _reconnect(state)
        return state.channel

    transport = state.client.get_transport()
    if transport is None or not transport.is_active():
        logging.info(f"SSH transport to {state.current_alias} is stale, reconnecting.")
        _reconnect(state)
        return state.channel

    if state.channel is None or state.channel.closed:
        logging.info("SSH shell channel closed, reopening.")
        _open_shell(state)

    return state.channel


def _reconnect(state: _SSHState) -> None:
    """Reconnect to the current server and reopen shell, preserving CWD."""
    alias = state.current_alias
    previous_cwd = state.cwd
    cfg = _get_servers()[alias]
    _disconnect(state)
    _establish_connection(state, alias, cfg)
    _open_shell(state)
    state.sudo_password = cfg.get("sudo_password")
    if previous_cwd:
        state.channel.sendall(f"cd {shlex.quote(previous_cwd)} 2>/dev/null\n")
        _read_until_prompt(state, timeout=5)
        state.cwd = previous_cwd


def _disconnect(state: _SSHState) -> None:
    """Close shell channel and SSH connection."""
    _stop_background_reader(state)

    if state.channel:
        try:
            state.channel.close()
        except Exception:
            pass
        state.channel = None

    if state.client:
        try:
            state.client.close()
        except Exception:
            pass
        state.client = None

    state.current_alias = None
    state.connection_time = None
    state.sudo_password = None
    state.waiting_for_input = False
    state.session_log = None
    state.last_command = None


# --- Output Helpers ---

def _wrap_for_bash_c(command: str) -> str:
    """Base64-wrap a command so shell quoting/newlines/nested `$()` can't break it.

    Returns an `eval "$(echo B64 | base64 --decode)"` payload. Preserves `$?`
    transparently (eval's exit status is the last command's exit), and runs in
    the caller's shell context so `cd` and env mutations persist.
    """
    encoded = base64.b64encode(command.encode("utf-8")).decode("ascii")
    return f'eval "$(echo {encoded} | base64 --decode)"'


def _wrap_multiline(command: str) -> str:
    """Wrap multi-line commands so the interactive shell executes them atomically.

    In an interactive shell, each complete top-level statement causes bash to
    print PS1. A user command with internal newlines after a heredoc terminator
    (e.g. `cat > foo <<EOF...EOF\\necho done`) is parsed as two statements —
    bash prints PS1 after the first completes, which the prompt-marker detector
    sees as "command finished" and tears down the reader early.
    """
    if "\n" not in command:
        return command
    return _wrap_for_bash_c(command)


def _strip_command_echo(output: str, command: str) -> str:
    """Strip command echo (first line) and sudo prompts from output."""
    lines = output.split("\n")
    if lines and command in lines[0]:
        lines = lines[1:]
    lines = [l for l in lines if not _SUDO_PATTERN.search(l)]
    return "\n".join(lines).strip()


def _clean_output(text: str) -> str:
    """Collapse carriage-return overwrites into final visible lines."""
    result = []
    for line in text.split("\n"):
        line = line.rstrip("\r")  # Handle \r\n line endings
        if "\r" in line:
            # Embedded \r = cursor overwrite (e.g. progress bars)
            parts = line.split("\r")
            line = parts[-1]
        if line.strip():
            result.append(line)
    return "\n".join(result)


def _tail(text: str, n: int) -> str:
    """Return last n lines of text."""
    if not text:
        return text
    lines = text.split("\n")
    if len(lines) <= n:
        return text
    return f"[... {len(lines) - n} lines trimmed, full output in session log ...]\n" + "\n".join(lines[-n:])


def _truncate_output(output: str) -> str:
    """Clean, tail, and truncate output."""
    output = _clean_output(output)
    output = _tail(output, _TAIL_LINES)
    max_chars = _get_max_output_chars()
    if len(output) > max_chars:
        total = len(output)
        output = output[-max_chars:]
        output = f"[OUTPUT TRUNCATED - showing last {max_chars:,} of {total:,} chars]\n\n" + output
    return output


def _finalize_output(state: _SSHState, output: str, command: str) -> str:
    """Process completed command: get exit code, CWD, log, format result."""
    state.waiting_for_input = False
    output = _strip_command_echo(output, command)

    # Get exit code
    state.channel.sendall("echo $?\n")
    exit_output, _ = _read_until_prompt(state, timeout=5)
    exit_code_str = exit_output.strip().split("\n")[-1].strip()
    try:
        exit_code = int(exit_code_str)
    except ValueError:
        exit_code = -1

    # Update CWD
    state.channel.sendall("pwd\n")
    pwd_output, _ = _read_until_prompt(state, timeout=5)
    new_cwd = pwd_output.strip().split("\n")[-1].strip()
    if new_cwd and new_cwd.startswith("/"):
        state.cwd = new_cwd

    # Log raw output to remote before truncation
    _log_to_remote(state, command, output, exit_code)

    output = _truncate_output(output)

    parts = []
    if output:
        parts.append(output)
    status = f"[exit {exit_code} | cwd: {state.cwd}]"
    if state.session_log:
        status = f"[exit {exit_code} | cwd: {state.cwd} | log: {state.session_log}]"
    parts.append(status)
    return "\n".join(parts)


def _poll_reader(state: _SSHState, timeout: int, command: str) -> str:
    """Poll the persistent background reader until completion, input prompt, or timeout."""
    start = time.time()

    while time.time() - start < timeout:
        # Check prompt/input state FIRST. The reader thread sets
        # prompt_found=True and then exits (line 404 in _start_background_reader),
        # so by the time this loop notices, thread.is_alive() is already False.
        # If we checked thread-dead before prompt_found we'd misreport every
        # successful command as "connection lost".
        with state._bg_reader_lock:
            prompt_found = state._bg_reader_prompt_found
            input_detected = state._bg_reader_input_detected

        if prompt_found:
            output, _ = _stop_background_reader(state)
            return _finalize_output(state, output, command)

        # Check if reader thread died without finding the prompt (real drop)
        if state._bg_reader_thread is not None and not state._bg_reader_thread.is_alive():
            output, _ = _stop_background_reader(state)
            state.waiting_for_input = False
            if output.strip():
                return f"{_truncate_output(output)}\n[connection lost during command — use force=True or reconnect]"
            return "[connection lost during command — use force=True or reconnect]"

        if input_detected:
            with state._bg_reader_lock:
                snapshot = state._bg_reader_buffer
                state._bg_reader_input_detected = False  # reset for next detection
            return _format_pending(
                state, snapshot, command,
                label="waiting_for_input",
                hint="[hint: send your response as command, e.g. exec(command='Y')]",
                tail_n=10,
            )

        time.sleep(0.5)

    # Timeout — reader keeps running, return current status
    with state._bg_reader_lock:
        snapshot = state._bg_reader_buffer
        new_output = snapshot[len(state._bg_reader_output_snapshot):]
        state._bg_reader_output_snapshot = snapshot
    return _format_pending(
        state, new_output, command,
        label="running",
        hint="[hint: call exec again to check progress, send input as 'command', or use force=True to abort]",
        tail_n=5,
        output_prefix="[new output]\n",
    )


def _format_pending(
    state: _SSHState, output: str, command: str,
    *, label: str, hint: str, tail_n: int, output_prefix: str = "",
) -> str:
    """Format a "command in progress" status — either awaiting input or still running."""
    state.waiting_for_input = True
    state.last_command = command
    output = _truncate_output(_strip_command_echo(output, command))

    tail_lines = ""
    if output.strip():
        lines = [l for l in output.strip().split("\n") if l.strip()]
        tail_lines = "\n".join(lines[-tail_n:])

    status = f"[{label} | cwd: {state.cwd}"
    if state.session_log:
        status += f" | log: {state.session_log}"
    status += "]"

    parts = [status]
    if tail_lines:
        parts.append(f"{output_prefix}{tail_lines}" if output_prefix else tail_lines)
    parts.append(hint)
    return "\n".join(parts)


# --- Actions ---

_ACTIONS = ("servers", "connect", "exec", "rsync", "jobs")


async def ssh(
    action: str,
    server: str = "",
    command: str = "",
    timeout: int = 0,
    responses: str = "",
    force: bool = False,
    background: bool = False,
    source: str = "",
    destination: str = "",
    direction: str = "upload",
    exclude: str = "",
    delete: bool = False,
    dry_run: bool = False,
    extra_flags: str = "",
) -> str:
    """Run SSH operations on remote servers configured in settings.json.

    Persistent interactive shell with CWD tracking, sudo handling, and session logging.
    Commands wait the full requested timeout — no early stall interrupts. Interactive
    prompts ([Y/n], password:, etc.) are auto-detected and surfaced immediately.

    Actions:
      servers — list configured SSH servers (no params needed)
      connect — establish SSH connection (requires server)
      exec    — execute command (requires command, or empty to poll running command)
      jobs    — list/inspect/kill background jobs (no args=list, command=ID for details)
      rsync   — transfer files via rsync (requires source, destination)

    Args:
        action: "servers", "connect", "exec", "jobs", or "rsync"
        server: Server alias from settings.json, e.g. "HOME" (connect only)
        command: Shell command to execute, input for running command, or job ID (exec/jobs)
        timeout: Seconds before timeout. 0 = action default (exec: 30, rsync: 300)
        responses: JSON dict of {"prompt_pattern": "response"} for interactive prompts (exec only)
        force: If True, abort running command (exec) or kill background job (jobs)
        background: If True, run command in background with output to log file (exec only)
        source: Source path (rsync only). Plain path — do NOT prefix with "ALIAS:" or "host:".
            For direction="upload" this is local; for "download" this is remote (absolute).
        destination: Destination path (rsync only). Plain path — do NOT prefix with "ALIAS:".
            For direction="upload" this is remote (absolute, e.g. "/Users/x/dir/"); for
            "download" this is local. SSH routing is implicit via the active connect session.
        direction: "upload" (local→remote) or "download" (remote→local). Default "upload".
        exclude: Comma-separated exclude patterns (rsync only)
        delete: Delete files at destination not at source (rsync only)
        dry_run: Show what would transfer without doing it (rsync only)
        extra_flags: Additional rsync flags (rsync only)
    """
    if action not in _ACTIONS:
        return f"Unknown action '{action}'. Use: {', '.join(_ACTIONS)}"

    if action == "servers":
        return await _servers()
    if action == "connect":
        return await _connect(server)
    if action == "exec":
        effective_timeout = timeout if timeout > 0 else 30
        return await _exec(command, effective_timeout, responses, force, background)
    if action == "jobs":
        return await _jobs(command, force)
    return await _rsync(
        source, destination, direction, exclude, delete, dry_run, extra_flags,
        timeout if timeout > 0 else 300,
    )


async def _servers() -> str:
    servers = _get_servers()
    if not servers:
        return "No SSH servers configured. Add them to settings.json."

    lines = ["Available SSH servers:"]
    for alias, cfg in servers.items():
        host = cfg.get("host", "")
        user = cfg.get("user", "")
        port = cfg.get("port", 22)
        lines.append(f"  {alias}: {user}@{host}:{port}")

    return "\n".join(lines)


async def _connect(server: str) -> str:
    if not server.strip():
        return "Error: server required"

    session_id = get_client_session_id()
    state = _get_or_create_state(session_id)

    servers = _get_servers()
    alias_upper = server.upper()
    if alias_upper not in servers:
        available = ", ".join(servers.keys())
        return f"Unknown server '{server}'. Available: {available}"

    cfg = servers[alias_upper]

    _disconnect(state)
    try:
        _establish_connection(state, alias_upper, cfg)
        _open_shell(state)
    except Exception as e:
        return f"Connection failed: {e}"

    state.sudo_password = cfg.get("sudo_password")

    # Get initial CWD
    state.channel.sendall("pwd\n")
    output, _ = _read_until_prompt(state, timeout=5)
    state.cwd = output.strip().split("\n")[-1].strip() or f"/home/{cfg.get('user', '')}"

    log_msg = f" Session log: {state.session_log}" if state.session_log else ""
    return f"Connected to {alias_upper} ({cfg['host']}). CWD: {state.cwd}.{log_msg}"


def _exec_background(state: _SSHState, command: str) -> str:
    """Launch command as a background job on the remote server."""
    output_dir = _OUTPUT_DIR

    # Generate job ID
    state.channel.sendall("date +%s%N | tail -c 8\n")
    id_output, _ = _read_until_prompt(state, timeout=5)
    job_id = id_output.strip().split("\n")[-1].strip()
    if not job_id or not job_id.isdigit():
        job_id = str(int(time.time() * 1000))[-7:]

    started = time.strftime("%Y-%m-%d %H:%M:%S")
    # Base64 everywhere — robust to nested quotes, $(), and multi-line commands.
    inner = _wrap_for_bash_c(command)
    meta_json = json.dumps({"command": command, "started": started})
    meta_b64 = base64.b64encode(meta_json.encode()).decode()

    launch_script = (
        f"mkdir -p {output_dir} && "
        f"echo '{meta_b64}' | base64 -d > {output_dir}/job_{job_id}.meta && "
        f"setsid bash -c '{inner}; echo $? > {output_dir}/job_{job_id}.exit' "
        f"> {output_dir}/job_{job_id}.log 2>&1 & "
        f"echo $! > {output_dir}/job_{job_id}.pid && "
        f"echo 'JOB_STARTED:{job_id}'"
    )

    state.channel.sendall(launch_script + "\n")
    output, completed = _read_until_prompt(state, timeout=10)

    if f"JOB_STARTED:{job_id}" in output:
        return (
            f"[background job {job_id} started]\n"
            f"Command: {command}\n"
            f"Log: {output_dir}/job_{job_id}.log\n"
            f"[hint: use action='jobs' to list, action='jobs' command='{job_id}' for output]"
        )

    return f"[failed to start background job]\n{_strip_ansi(output)}"


async def _jobs(command: str, force: bool) -> str:
    """List, inspect, or kill background jobs."""
    session_id = get_client_session_id()
    state = _get_or_create_state(session_id)

    try:
        _get_valid_channel(state)
    except ConnectionError as e:
        return str(e)

    output_dir = _OUTPUT_DIR

    if not command.strip():
        # List all jobs
        list_cmd = (
            f"for meta in {output_dir}/job_*.meta 2>/dev/null; do "
            f"[ -f \"$meta\" ] || continue; "
            f"jid=$(echo \"$meta\" | grep -o 'job_[0-9]*' | cut -d_ -f2); "
            f"pid=$(cat {output_dir}/job_${{jid}}.pid 2>/dev/null); "
            f"if [ -f {output_dir}/job_${{jid}}.exit ]; then "
            f"exit_code=$(cat {output_dir}/job_${{jid}}.exit); "
            f"status=\"done (exit $exit_code)\"; "
            f"elif kill -0 $pid 2>/dev/null; then "
            f"status=\"running (pid $pid)\"; "
            f"else "
            f"status=\"dead (no exit file)\"; "
            f"fi; "
            f"cmd=$(cat \"$meta\" 2>/dev/null); "
            f"echo \"[$jid] $status | $cmd\"; "
            f"done"
        )
        state.channel.sendall(list_cmd + "\n")
        output, _ = _read_until_prompt(state, timeout=10)
        output = _strip_command_echo(output, list_cmd)
        output = _strip_ansi(output).strip()

        if not output:
            return "No background jobs found."

        # Cleanup old job files
        state.channel.sendall(
            f"find {output_dir} -name 'job_*' -mtime +{_CLEANUP_DAYS} -delete 2>/dev/null\n"
        )
        _read_until_prompt(state, timeout=5)

        return f"Background jobs:\n{output}"

    job_id = command.strip()
    if not job_id.isdigit():
        return "Error: job ID must be numeric (e.g. '1234567')"

    if force:
        # Kill job by process group
        kill_cmd = (
            f"pid=$(cat {output_dir}/job_{job_id}.pid 2>/dev/null) && "
            f"kill -TERM -- -$pid 2>/dev/null || kill -TERM $pid 2>/dev/null && "
            f"echo 'KILLED' || echo 'NOT_FOUND'"
        )
        state.channel.sendall(kill_cmd + "\n")
        output, _ = _read_until_prompt(state, timeout=5)
        if "KILLED" in output:
            return f"[job {job_id} killed]"
        return f"[job {job_id} not found or already dead]"

    # Tail job output
    tail_cmd = f"tail -n {_TAIL_LINES} {output_dir}/job_{job_id}.log 2>/dev/null"
    state.channel.sendall(tail_cmd + "\n")
    output, _ = _read_until_prompt(state, timeout=10)
    output = _strip_command_echo(output, tail_cmd)
    output = _strip_ansi(output).strip()

    # Check status
    status_cmd = (
        f"if [ -f {output_dir}/job_{job_id}.exit ]; then "
        f"echo \"STATUS:done:$(cat {output_dir}/job_{job_id}.exit)\"; "
        f"elif [ -f {output_dir}/job_{job_id}.pid ] && kill -0 $(cat {output_dir}/job_{job_id}.pid) 2>/dev/null; then "
        f"echo \"STATUS:running\"; "
        f"else echo \"STATUS:dead\"; fi"
    )
    state.channel.sendall(status_cmd + "\n")
    status_output, _ = _read_until_prompt(state, timeout=5)
    status_lines = [l for l in status_output.split("\n") if l.startswith("STATUS:")]

    status_str = "unknown"
    if status_lines:
        parts = status_lines[0].split(":")
        if parts[1] == "done":
            status_str = f"completed (exit {parts[2] if len(parts) > 2 else '?'})"
        elif parts[1] == "running":
            status_str = "running"
        else:
            status_str = "dead (no exit code)"

    result_parts = [f"[job {job_id} | {status_str}]"]
    if output:
        result_parts.append(output)
    else:
        result_parts.append("(no output yet)")

    return "\n".join(result_parts)


def _abort_current_command(state: _SSHState) -> str:
    """Send escalating signals to abort the current command."""
    _stop_background_reader(state)
    state.waiting_for_input = False
    state.last_command = None

    for signal_char, delay in [("\x03", 0.5), ("\x03", 0.5), ("\x1c", 0.5)]:
        state.channel.sendall(signal_char)
        time.sleep(delay)
        try:
            while state.channel.recv_ready():
                state.channel.recv(65536)
        except Exception:
            pass

    state.channel.sendall("\n")
    _, prompt_returned = _read_until_prompt(state, timeout=5)
    if not prompt_returned:
        logging.warning("Force-abandon: signals failed, reopening shell")
        try:
            _open_shell(state)
            return f"[force-abandon: had to reset shell | cwd: {state.cwd}]"
        except Exception as e:
            return f"[force-abandon failed: {e}. Use action='connect' to reconnect.]"

    return f"[command aborted | cwd: {state.cwd}]"


async def _exec(command: str, timeout: int, responses: str, force: bool, background: bool) -> str:
    session_id = get_client_session_id()
    state = _get_or_create_state(session_id)

    try:
        _get_valid_channel(state)
    except ConnectionError as e:
        return str(e)

    # Parse responses
    response_map = {}
    if responses:
        try:
            response_map = json.loads(responses)
        except json.JSONDecodeError:
            return "Error: 'responses' must be valid JSON, e.g. '{\"prompt\": \"answer\"}'"

    # --- Background execution ---
    if background:
        if not command:
            return "Error: command required for background execution"
        return _exec_background(state, command)

    # --- Force-abort running command ---
    if force:
        if state.waiting_for_input or state._bg_reader_thread is not None:
            abort_msg = _abort_current_command(state)
            if not command:
                return abort_msg
            # Fall through to execute the new command after abort
        elif not command:
            return "Nothing running to abort."

    # --- Send input / poll running command ---
    if state.waiting_for_input:
        if state._bg_reader_thread is None or not state._bg_reader_thread.is_alive():
            # Reader died — reset state
            _stop_background_reader(state)
            state.waiting_for_input = False
        else:
            if command:
                # Send input while reader keeps running (full-duplex safe)
                state.channel.sendall(command + "\n")
            return _poll_reader(state, timeout, state.last_command or command)

    # --- New command ---
    if not command:
        return "No command running and no command provided."

    state.channel.sendall(_wrap_multiline(command) + "\n")
    state.last_command = command
    _start_background_reader(state, responses=response_map)
    return _poll_reader(state, timeout, command)


async def _rsync(
    source: str,
    destination: str,
    direction: str,
    exclude: str,
    delete: bool,
    dry_run: bool,
    extra_flags: str,
    timeout: int,
) -> str:
    session_id = get_client_session_id()
    state = _get_or_create_state(session_id)

    if not state.current_alias:
        return "No SSH connection. Use action='connect' first."

    if not source.strip() or not destination.strip():
        return "source and destination must not be empty"

    if source.startswith("-") or destination.startswith("-"):
        return "source and destination must not start with '-'"

    direction = direction.lower()
    if direction not in ("upload", "download"):
        return "direction must be 'upload' or 'download'"

    # Reject "ALIAS:path" or "host:path" — SSH routing is implicit via the active
    # connect session. The remote side is determined by direction (upload → dest
    # is remote, download → source is remote). Paths must be plain (local absolute
    # or relative paths for the local side; absolute remote paths for the remote side).
    _alias_prefix = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*:")
    remote_label = "destination" if direction == "upload" else "source"
    for label, val in (("source", source), ("destination", destination)):
        if _alias_prefix.match(val):
            prefix = val.split(":", 1)[0]
            return (
                f"{label}='{val}' looks like a host/alias-prefixed path ('{prefix}:...'). "
                f"rsync routing is implicit via the active SSH connection "
                f"(currently: {state.current_alias}). Pass a plain path instead — "
                f"for direction='{direction}', {remote_label} is the remote path "
                f"(absolute, e.g. '/srv/data/') and the other side is local."
            )

    servers = _get_servers()
    cfg = servers.get(state.current_alias)
    if not cfg:
        return f"Server config for '{state.current_alias}' not found."

    key_file = cfg.get("key_file")
    if not key_file:
        return "rsync requires key-based auth. No key_file configured for this server."

    effective_timeout = min(timeout, 600)

    # Build SSH command for rsync -e
    key_path = os.path.expanduser(key_file)
    port = cfg.get("port", 22)
    ssh_cmd = f"ssh -i {shlex.quote(key_path)} -p {port} -o StrictHostKeyChecking=accept-new"

    user = cfg["user"]
    host = cfg["host"]
    remote_prefix = f"{user}@{host}:"

    # Build rsync args
    cmd = ["rsync", "-avz", "-e", ssh_cmd]

    if delete:
        cmd.append("--delete")
    if dry_run:
        cmd.append("--dry-run")

    if exclude:
        for pattern in exclude.split(","):
            pattern = pattern.strip()
            if pattern:
                if pattern.startswith("-"):
                    return f"Exclude pattern must not start with '-': {pattern}"
                cmd.extend(["--exclude", pattern])

    if extra_flags:
        allowed = {
            "--progress", "--partial", "--compress", "--stats",
            "--human-readable", "--itemize-changes", "--checksum",
            "--ignore-existing", "--update", "--no-perms",
            "--no-owner", "--no-group", "--chmod", "--inplace",
            "--append", "--append-verify", "--size-only",
        }
        for flag in extra_flags.split():
            flag_name = flag.split("=")[0]
            if flag_name not in allowed:
                return f"Disallowed rsync flag: {flag_name}. Allowed: {', '.join(sorted(allowed))}"
            cmd.append(flag)

    # Separate flags from paths
    cmd.append("--")

    if direction == "upload":
        cmd.append(os.path.expanduser(source))
        cmd.append(f"{remote_prefix}{destination}")
    else:
        cmd.append(f"{remote_prefix}{source}")
        cmd.append(os.path.expanduser(destination))

    logging.info(f"rsync {direction}: {source} -> {destination} ({state.current_alias})")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=effective_timeout)
        output = stdout.decode("utf-8", errors="replace")
        output = _truncate_output(output)
        return f"{output}\n[exit {proc.returncode}]"
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return f"[TIMEOUT after {effective_timeout}s — rsync process killed]"
    except FileNotFoundError:
        return "rsync not found. Install rsync and try again."
    except Exception as e:
        return f"rsync failed: {e}"
