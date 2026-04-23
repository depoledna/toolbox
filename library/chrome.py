"""
Shared Chrome/CDP utilities for browser automation.

Used by tools/browser.py and library/apple_ads.py.
"""

import asyncio
import json
import os
import subprocess
import tempfile
import urllib.request

CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CDP_PORT_BASE = 9240

_port_lock = asyncio.Lock()
_used_ports: set[int] = set()


async def alloc_port() -> int:
    """Allocate a free CDP debugging port."""
    async with _port_lock:
        port = CDP_PORT_BASE
        while port in _used_ports:
            port += 1
        _used_ports.add(port)
        return port


async def free_port(port: int) -> None:
    """Release a CDP port."""
    async with _port_lock:
        _used_ports.discard(port)


def cdp_ready(port: int) -> bool:
    """Check if Chrome's CDP endpoint is responding."""
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
        return True
    except Exception:
        return False


def resolve_cdp_ws_url(port: int) -> str:
    """Get the WebSocket CDP URL from Chrome's debug endpoint."""
    resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5)
    data = json.loads(resp.read())
    return data["webSocketDebuggerUrl"]


def _port_in_use(port: int) -> bool:
    """Return True if something is already listening on the port."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except (ConnectionRefusedError, OSError):
            return False


def _profile_locked(profile_dir: str) -> bool:
    """Detect Chrome's SingletonLock indicating another instance owns the profile."""
    return os.path.exists(os.path.join(profile_dir, "SingletonLock"))


async def launch_chrome(
    port: int,
    profile_dir: str,
    headless: bool = False,
    extra_args: list[str] | None = None,
) -> subprocess.Popen:
    """Launch Chrome with CDP debugging and wait until ready.

    On failure, raises RuntimeError with actionable details: missing binary,
    port already in use, profile locked by another instance, or Chrome's own
    stderr output if it crashed during startup.
    """
    # Pre-flight diagnostics so we fail with a useful message instead of a bare timeout.
    if not os.path.exists(CHROME_PATH):
        raise RuntimeError(f"Chrome binary not found at {CHROME_PATH}")
    if _port_in_use(port):
        raise RuntimeError(
            f"Port {port} is already in use — another process is listening there. "
            "Close stray Chrome instances or free the port."
        )
    if _profile_locked(profile_dir):
        raise RuntimeError(
            f"Chrome profile at {profile_dir} is locked (SingletonLock present). "
            "Another Chrome instance is using this profile, or a previous session "
            "didn't clean up. Quit Chrome or remove the lock file and retry."
        )

    args = [
        CHROME_PATH,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if headless:
        args.extend(["--headless=new", "--disable-gpu"])
    if extra_args:
        args.extend(extra_args)

    # Capture stderr so we can surface Chrome's actual startup errors.
    stderr_file = tempfile.NamedTemporaryFile(
        prefix=f"chrome_stderr_{port}_", suffix=".log", delete=False
    )
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=stderr_file)

    loop = asyncio.get_running_loop()
    try:
        for _ in range(30):
            # If Chrome died during startup, don't keep polling — surface the error now.
            if proc.poll() is not None:
                stderr_file.close()
                tail = _read_tail(stderr_file.name)
                raise RuntimeError(
                    f"Chrome exited with code {proc.returncode} during startup on port {port}. "
                    f"stderr: {tail or '(empty)'}"
                )
            if await loop.run_in_executor(None, cdp_ready, port):
                stderr_file.close()
                _safe_unlink(stderr_file.name)
                return proc
            await asyncio.sleep(0.3)

        # Timed out waiting for CDP. Kill and surface whatever stderr we captured.
        proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        stderr_file.close()
        tail = _read_tail(stderr_file.name)
        raise RuntimeError(
            f"Chrome did not start on port {port} within 9s. "
            f"stderr: {tail or '(empty)'}"
        )
    finally:
        _safe_unlink(stderr_file.name)


def _read_tail(path: str, max_bytes: int = 2000) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def kill_chrome(proc: subprocess.Popen | None) -> None:
    """Terminate a Chrome process gracefully, then force-kill."""
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


async def kill_chrome_async(proc: subprocess.Popen | None) -> None:
    """Non-blocking terminate: SIGTERM, 10×0.2s poll, SIGKILL.

    Prefer over `kill_chrome` inside async code — avoids blocking the event
    loop for up to 5 seconds if Chrome is slow to exit.
    """
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    for _ in range(10):
        if proc.poll() is not None:
            return
        await asyncio.sleep(0.2)
    proc.kill()
    proc.wait()


async def kill_all_chrome() -> None:
    """Kill every Chrome instance via pkill. Used before visible-auth flows."""
    p = await asyncio.create_subprocess_exec(
        "pkill", "-f", "Google Chrome",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await p.wait()
    await asyncio.sleep(1)
