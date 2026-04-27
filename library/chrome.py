"""
Shared Chrome/CDP utilities for browser automation.

Used by tools/browser.py and library/apple_ads.py.
"""

import asyncio
import glob
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request

CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_APP = "/Applications/Google Chrome.app"
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


def _fetch_cdp_version(port: int, timeout: float) -> dict | None:
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout)
        return json.loads(resp.read())
    except Exception:
        return None


def cdp_ready(port: int) -> bool:
    """Check if Chrome's CDP endpoint is responding."""
    return _fetch_cdp_version(port, 2) is not None


def resolve_cdp_ws_url(port: int) -> str:
    """Get the WebSocket CDP URL from Chrome's debug endpoint."""
    data = _fetch_cdp_version(port, 5)
    if data is None:
        raise RuntimeError(f"CDP not reachable on port {port}")
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


class _OpenLaunchedChrome:
    """Popen-compatible wrapper for a Chrome launched via `open -na`.

    `open -n` doesn't return the spawned PID directly, so we resolve it after
    CDP is ready by pgrep'ing for the unique --remote-debugging-port flag.
    """

    def __init__(self, pid: int):
        self.pid = pid
        self._returncode: int | None = None

    def poll(self) -> int | None:
        if self._returncode is not None:
            return self._returncode
        try:
            os.kill(self.pid, 0)
            return None
        except ProcessLookupError:
            self._returncode = -1
            return -1

    def terminate(self) -> None:
        try:
            os.kill(self.pid, 15)  # SIGTERM
        except ProcessLookupError:
            pass

    def kill(self) -> None:
        try:
            os.kill(self.pid, 9)  # SIGKILL
        except ProcessLookupError:
            pass

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else (time.monotonic() + timeout)
        while True:
            rc = self.poll()
            if rc is not None:
                return rc
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(cmd="chrome", timeout=timeout)
            time.sleep(0.05)


def _find_chrome_pid_by_port(port: int) -> int | None:
    """Find the parent Chrome browser PID listening on the given CDP port."""
    try:
        # `--` separator: macOS pgrep treats leading `--` in the pattern as an
        # unknown option otherwise.
        r = subprocess.run(
            ["pgrep", "-f", "--", f"--remote-debugging-port={port}"],
            capture_output=True, text=True, timeout=2,
        )
    except Exception:
        return None
    pids = [int(p) for p in r.stdout.split() if p.strip().isdigit()]
    if not pids:
        return None
    # The parent (browser) process has the lowest PID; renderers are spawned later.
    return min(pids)


async def launch_chrome(
    port: int,
    profile_dir: str,
    headless: bool = False,
    extra_args: list[str] | None = None,
):
    """Launch Chrome with CDP debugging and wait until ready.

    For headed launches, uses `open -na "Google Chrome.app"` so each instance
    registers as a fully-separate LaunchServices app — required for `osascript`
    `set visible to false` to actually hide the window. Direct subprocess.Popen
    on the binary leaves secondary instances as "partial" apps that silently
    refuse System Events visibility verbs.

    Headless launches keep using Popen since they have no window to hide.

    Returns a Popen-compatible object (real Popen for headless, wrapper for
    headed). On failure, raises RuntimeError with actionable details.
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

    chrome_args = [
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if headless:
        chrome_args.extend(["--headless=new", "--disable-gpu"])
    if extra_args:
        chrome_args.extend(extra_args)

    if headless:
        # Headless mode: direct Popen is fine (no window to hide via LaunchServices).
        return await _launch_via_popen(port, chrome_args)
    else:
        # Headed mode: must go through `open -na` for hide-by-PID to work.
        return await _launch_via_open(port, chrome_args)


async def _launch_via_popen(port: int, chrome_args: list[str]):
    """Direct binary launch — used for headless mode."""
    stderr_file = tempfile.NamedTemporaryFile(
        prefix=f"chrome_stderr_{port}_", suffix=".log", delete=False
    )
    proc = subprocess.Popen(
        [CHROME_PATH, *chrome_args],
        stdout=subprocess.DEVNULL, stderr=stderr_file,
    )
    loop = asyncio.get_running_loop()
    try:
        for _ in range(30):
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


async def _launch_via_open(port: int, chrome_args: list[str]):
    """Launch via `open -na` — required for headed mode so hide-by-PID works."""
    open_proc = subprocess.run(
        ["open", "-na", CHROME_APP, "--args", *chrome_args],
        capture_output=True, text=True, timeout=10,
    )
    if open_proc.returncode != 0:
        raise RuntimeError(
            f"`open -na Google Chrome` failed (rc={open_proc.returncode}): "
            f"{open_proc.stderr.strip() or '(empty)'}"
        )
    loop = asyncio.get_running_loop()
    for _ in range(30):
        if await loop.run_in_executor(None, cdp_ready, port):
            pid = _find_chrome_pid_by_port(port)
            if pid is None:
                raise RuntimeError(
                    f"Chrome started on port {port} but no PID found via pgrep — "
                    "race or pgrep failure?"
                )
            return _OpenLaunchedChrome(pid)
        await asyncio.sleep(0.3)
    raise RuntimeError(
        f"Chrome did not start on port {port} within 9s after `open -na`."
    )


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


def clone_profile(src: str) -> str:
    """Clone a Chrome profile into a fresh tempdir and strip lock files.

    Uses macOS `cp -cR` (APFS clonefile, copy-on-write) for near-instant duplication.
    Returns the new profile dir path. Caller owns cleanup (shutil.rmtree).
    """
    dst = tempfile.mkdtemp(prefix="chrome-")
    if os.path.isdir(src):
        # cp -cR: APFS clonefile (COW). Trailing /. copies contents into dst,
        # not src itself as a subdir.
        subprocess.run(["cp", "-cR", f"{src}/.", dst], check=False)
    # Strip per-instance locks so the cloned profile isn't lock-held.
    for pattern in ("Singleton*", "lockfile", "CrashpadMetrics-active.pma", "*.lock"):
        for f in glob.glob(os.path.join(dst, pattern)):
            try:
                os.unlink(f)
            except OSError:
                pass
    return dst


async def hide_chrome_pid(pid: int) -> None:
    """Hide a Chrome instance via Cocoa NSRunningApplication.hide().

    This is the proper macOS API (what Cmd+H invokes). osascript `set visible
    to false` only works on the FIRST Chrome instance — silently no-ops on
    secondary instances. NSRunningApplication.hide() works for all instances.
    """
    await asyncio.get_running_loop().run_in_executor(None, _hide_pid_sync, pid)


async def show_chrome_pid(pid: int) -> None:
    """Unhide a Chrome instance and bring it to the foreground."""
    await asyncio.get_running_loop().run_in_executor(None, _show_pid_sync, pid)


def _hide_pid_sync(pid: int) -> None:
    try:
        from AppKit import NSRunningApplication
    except ImportError:
        return
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app:
        app.hide()


def _show_pid_sync(pid: int) -> None:
    try:
        from AppKit import NSRunningApplication, NSApplicationActivateIgnoringOtherApps
    except ImportError:
        return
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app:
        app.unhide()
        app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
