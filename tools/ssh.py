import asyncio
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


_STALL_TIMEOUT = 15
_OUTPUT_DIR = "~/mcp_output"
_CLEANUP_DAYS = 5
_TAIL_LINES = 200
_IDLE_TTL = 3600  # disconnect sessions idle for 1 hour
_CLEANUP_INTERVAL = 300


def _get_stall_timeout():
    return _load_settings().get("SSH_STALL_TIMEOUT", _STALL_TIMEOUT)


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
    # Background reader — captures channel output between ssh_exec calls
    _bg_reader_thread: Optional[threading.Thread] = None
    _bg_reader_stop: Optional[threading.Event] = None
    _bg_reader_buffer: str = ""
    _bg_reader_prompt_found: bool = False
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
    """Open an interactive shell channel and set up prompt detection."""
    state.channel = state.client.invoke_shell(term="dumb", width=200, height=50)
    state.channel.settimeout(0.1)

    # Switch to a clean bash (avoids zsh prompt issues, oh-my-zsh, etc.)
    state.channel.sendall("exec bash --norc --noprofile --noediting\n")
    time.sleep(0.3)

    # Drain any startup output
    try:
        while state.channel.recv_ready():
            state.channel.recv(65536)
    except Exception:
        pass

    # Configure shell for reliable prompt detection
    init_commands = [
        "stty -echo -icanon",
        "unset PROMPT_COMMAND",
        f"export PS1='{_PROMPT_MARKER}\n'",
        "export PS2=''",
        "export TERM=dumb",
    ]
    for cmd in init_commands:
        state.channel.sendall(cmd + "\n")
        time.sleep(0.1)

    # Read until we see the prompt marker (shell is ready)
    _, completed, _ = _read_until_prompt(state, timeout=10, stall_timeout=0)
    if not completed:
        raise RuntimeError("Shell initialization timed out — prompt marker never received")

    state.waiting_for_input = False
    _init_session_log(state)


def _read_until_prompt(
    state: _SSHState, timeout: int = 30, responses: dict = None, stall_timeout: int = None
) -> tuple[str, bool, str]:
    """
    Read channel output until prompt marker appears, stall detected, or timeout.

    Handles sudo password prompts and custom response patterns.

    Returns:
        (output, completed, stall_reason) — completed is True if prompt marker was found.
        stall_reason is "silent" if no output for stall_timeout, or "timeout" if
        hard timeout hit. Empty string when completed is True.
    """
    if stall_timeout is None:
        stall_timeout = _get_stall_timeout()

    buffer = ""
    start = time.time()
    last_data_time = time.time()
    sudo_sent = False

    while time.time() - start < timeout:
        try:
            chunk = state.channel.recv(65536).decode("utf-8", errors="replace")
            if chunk:
                buffer += chunk
                last_data_time = time.time()

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

        # Stall: no output for stall_timeout seconds
        if stall_timeout > 0 and (time.time() - last_data_time) >= stall_timeout:
            if _PROMPT_MARKER not in buffer:
                return _strip_ansi(buffer), False, "silent"

        time.sleep(0.05)
    else:
        return _strip_ansi(buffer) + f"\n[TIMEOUT after {timeout}s - command may still be running]", False, "timeout"

    idx = buffer.find(_PROMPT_MARKER)
    if idx != -1:
        buffer = buffer[:idx]

    return _strip_ansi(buffer), True, ""


# --- Background Reader ---

def _start_background_reader(state: _SSHState) -> None:
    """Start a daemon thread to buffer channel output between ssh_exec calls."""
    if state._bg_reader_thread is not None:
        _stop_background_reader(state)

    stop_event = threading.Event()
    state._bg_reader_stop = stop_event
    state._bg_reader_buffer = ""
    state._bg_reader_prompt_found = False

    channel = state.channel
    client = state.client
    session_log = state.session_log
    last_command = state.last_command
    lock = state._bg_reader_lock

    def _reader_loop():
        last_log_time = time.time()
        logged_up_to = 0
        while not stop_event.is_set():
            try:
                chunk = channel.recv(65536).decode("utf-8", errors="replace")
                if chunk:
                    with lock:
                        state._bg_reader_buffer += chunk
                        if _PROMPT_MARKER in state._bg_reader_buffer:
                            state._bg_reader_prompt_found = True
                            return
            except socket_timeout:
                pass
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
        _read_until_prompt(state, timeout=5, stall_timeout=0)
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
    exit_output, _, _ = _read_until_prompt(state, timeout=5, stall_timeout=0)
    exit_code_str = exit_output.strip().split("\n")[-1].strip()
    try:
        exit_code = int(exit_code_str)
    except ValueError:
        exit_code = -1

    # Update CWD
    state.channel.sendall("pwd\n")
    pwd_output, _, _ = _read_until_prompt(state, timeout=5, stall_timeout=0)
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


def _format_waiting_output(state: _SSHState, output: str, command: str) -> str:
    """Format output for a command that hasn't completed yet."""
    state.waiting_for_input = True
    state.last_command = command
    output = _strip_command_echo(output, command)
    output = _truncate_output(output)

    # Extract last few lines for context
    last_lines = ""
    if output:
        tail = [l for l in output.strip().split("\n") if l.strip()]
        last_lines = "\n".join(tail[-5:])

    parts = []
    status = f"[running | cwd: {state.cwd}]"
    if state.session_log:
        status = f"[running | cwd: {state.cwd} | log: {state.session_log}]"
    parts.append(status)
    if last_lines:
        parts.append(f"[last output]\n{last_lines}")

    parts.append("[hint: call exec again to check progress, send input as 'command', or use force=True to abort]")

    # Start background reader to capture output between polls
    _start_background_reader(state)

    return "\n".join(parts)


# --- Actions ---

_ACTIONS = ("servers", "connect", "exec", "rsync")


async def ssh(
    action: str,
    server: str = "",
    command: str = "",
    timeout: int = 0,
    responses: str = "",
    force: bool = False,
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
    Handles stalled commands, auto-reconnects on timeout, and supports file transfer via rsync.

    Actions:
      servers — list configured SSH servers (no params needed)
      connect — establish SSH connection (requires server)
      exec    — execute command on connected server (requires command, or empty to poll)
      rsync   — transfer files via rsync (requires source, destination)

    Args:
        action: "servers", "connect", "exec", or "rsync"
        server: Server alias from settings.json, e.g. "HOME" (connect only)
        command: Shell command to execute, or input for stalled command (exec only)
        timeout: Seconds before timeout. 0 = action default (exec: 30, rsync: 300)
        responses: JSON dict of {"prompt_pattern": "response"} for interactive prompts (exec only)
        force: If True, send Ctrl+C to abandon stalled command first (exec only)
        source: Source path (rsync only)
        destination: Destination path (rsync only)
        direction: "upload" or "download" (rsync only, default "upload")
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
        return await _exec(command, effective_timeout, responses, force)
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
        return f"Unknown server '{server_alias}'. Available: {available}"

    cfg = servers[alias_upper]

    _disconnect(state)
    try:
        _establish_connection(state, alias_upper, cfg)
        _open_shell(state)
    except (ConnectionError, Exception) as e:
        return f"Connection failed: {e}"

    state.sudo_password = cfg.get("sudo_password")

    # Get initial CWD
    state.channel.sendall("pwd\n")
    output, _, _ = _read_until_prompt(state, timeout=5, stall_timeout=0)
    state.cwd = output.strip().split("\n")[-1].strip() or f"/home/{cfg.get('user', '')}"

    log_msg = f" Session log: {state.session_log}" if state.session_log else ""
    return f"Connected to {alias_upper} ({cfg['host']}). CWD: {state.cwd}.{log_msg}"


async def _exec(command: str, timeout: int, responses: str, force: bool) -> str:
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

    effective_timeout = min(timeout, 300)

    # --- Branch 1: Resume stalled command ---
    if state.waiting_for_input and not force:
        original_cmd = state.last_command or command

        # Harvest output captured by background reader between polls
        bg_output, bg_prompt_found = _stop_background_reader(state)

        if bg_prompt_found:
            # Command completed while we were away
            return _finalize_output(state, bg_output, original_cmd)

        if command:
            # Sending input — wait for response
            state.channel.sendall(command + "\n")
            output, completed, stall_reason = _read_until_prompt(
                state, timeout=effective_timeout, responses=response_map
            )
            combined = bg_output + output
            if completed:
                return _finalize_output(state, combined, original_cmd)
            return _format_waiting_output(state, combined, original_cmd)

        # Polling — wait up to timeout for command to complete
        output, completed, stall_reason = _read_until_prompt(
            state, timeout=effective_timeout, stall_timeout=effective_timeout
        )
        combined = bg_output + output
        if completed:
            return _finalize_output(state, combined, original_cmd)
        return _format_waiting_output(state, combined, original_cmd)

    # --- Branch 2: Force-abandon stalled command ---
    if force and state.waiting_for_input:
        _stop_background_reader(state)
        state.waiting_for_input = False
        # Try escalating signals: Ctrl+C, then Ctrl+C again, then Ctrl+\
        for signal, delay in [("\x03", 0.5), ("\x03", 0.5), ("\x1c", 0.5)]:
            state.channel.sendall(signal)
            time.sleep(delay)
            # Drain any output
            try:
                while state.channel.recv_ready():
                    state.channel.recv(65536)
            except Exception:
                pass
        # Now check if we got back to prompt
        state.channel.sendall("\n")
        _, prompt_returned, _ = _read_until_prompt(state, timeout=5, stall_timeout=0)
        if not prompt_returned:
            # Last resort: close channel and reopen shell
            logging.warning("Force-abandon: signals failed, reopening shell")
            try:
                _open_shell(state)
                return f"[force-abandon: had to reset shell | cwd: {state.cwd}]"
            except Exception as e:
                return f"[force-abandon failed: {e}. Use action='connect' to reconnect.]"

    # --- Branch 3: Execute new command ---
    state.channel.sendall(command + "\n")

    output, completed, stall_reason = _read_until_prompt(
        state, timeout=effective_timeout, responses=response_map
    )

    if completed:
        return _finalize_output(state, output, command)
    return _format_waiting_output(state, output, command)


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

    servers = _get_servers()
    cfg = servers.get(state.current_alias)
    if not cfg:
        return f"Server config for '{state.current_alias}' not found."

    key_file = cfg.get("key_file")
    if not key_file:
        return "rsync requires key-based auth. No key_file configured for this server."

    direction = direction.lower()
    if direction not in ("upload", "download"):
        return "direction must be 'upload' or 'download'"

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
