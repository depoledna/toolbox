"""
Shared Chrome/CDP utilities for browser automation.

Used by tools/browser.py and library/apple_ads.py.
"""

import asyncio
import json
import subprocess
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


async def launch_chrome(
    port: int,
    profile_dir: str,
    headless: bool = False,
    extra_args: list[str] | None = None,
) -> subprocess.Popen:
    """Launch Chrome with CDP debugging and wait until ready."""
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

    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    loop = asyncio.get_running_loop()
    for _ in range(30):
        if await loop.run_in_executor(None, cdp_ready, port):
            return proc
        await asyncio.sleep(0.3)

    proc.kill()
    raise RuntimeError(f"Chrome did not start on port {port}")


def kill_chrome(proc: subprocess.Popen | None) -> None:
    """Terminate a Chrome process gracefully, then force-kill."""
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
