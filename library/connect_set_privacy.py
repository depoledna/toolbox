"""
Set App Privacy declaration in App Store Connect via browser automation.

No API exists for this — browser automation is the only way.
Uses the same Chrome profile and auth as connect_new_app.

Usage:
    from library import connect_set_privacy
    connect_set_privacy("<APP_ID>")
"""
import asyncio
import os
import subprocess
import urllib.request
from pathlib import Path

CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PROFILE_DIR = os.environ.get("CHROME_PROFILE_DIR", os.path.join(str(Path.home()), ".chrome-profile"))
STATE_FILE = os.environ.get("CHROME_STATE_FILE", os.path.join(str(Path.home()), ".chrome-profile", "storage_state.json"))
CDP_PORT = 9233
LOGIN_TIMEOUT = 120


def _cdp_ready(port: int) -> bool:
    try:
        urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=3)
        return True
    except Exception:
        return False


async def _kill_proc(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    for _ in range(10):
        if proc.poll() is not None:
            return
        await asyncio.sleep(0.2)
    proc.kill()
    proc.wait()


async def _launch_chrome(port: int, headless: bool = True) -> subprocess.Popen:
    args = [
        CHROME_PATH,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run", "--no-default-browser-check",
    ]
    if headless:
        args.extend(["--headless=new", "--disable-gpu"])

    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    loop = asyncio.get_event_loop()
    for _ in range(20):
        if await loop.run_in_executor(None, _cdp_ready, port):
            return proc
        await asyncio.sleep(0.25)

    proc.kill()
    raise RuntimeError(f"Chrome did not start on port {port}")


async def _set_privacy(app_id: str) -> dict:
    from playwright.async_api import async_playwright

    proc = await _launch_chrome(CDP_PORT, headless=True)

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(f"http://localhost:{CDP_PORT}")

        if os.path.exists(STATE_FILE):
            ctx = await browser.new_context(storage_state=STATE_FILE)
        else:
            ctx = await browser.new_context()

        page = await ctx.new_page()
        privacy_url = f"https://appstoreconnect.apple.com/apps/{app_id}/distribution/privacy"
        await page.goto(privacy_url, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(5)

        # Check auth
        if "login" in page.url or "authResult" in page.url:
            raise RuntimeError("Not authenticated — run connect_new_app first to trigger login")

        # Check if already configured (no "Get Started" button)
        get_started = page.locator('button:has-text("Get Started")')
        if await get_started.count() == 0:
            body = await page.evaluate("() => document.body?.innerText || ''")
            if "do not collect data" in body.lower() or "no data collected" in body.lower():
                await ctx.close()
                await pw.stop()
                await _kill_proc(proc)
                return {"success": True, "app_id": app_id, "collects_data": False, "already_set": True}

        # Click Get Started
        await get_started.click()
        await asyncio.sleep(3)

        # Select "No, we do not collect data"
        no_collect = page.locator('#CONFIRM_COLLECT_DATA_radio_false')
        await no_collect.click(force=True)
        await asyncio.sleep(1)

        # Click Save
        save_btn = page.locator('button:has-text("Save")')
        await save_btn.click()
        await asyncio.sleep(3)

        # Click Publish
        publish_btn = page.locator('button:has-text("Publish")')
        if await publish_btn.count() > 0:
            await publish_btn.click()
            await asyncio.sleep(3)

        # Save cookies
        await ctx.storage_state(path=STATE_FILE)

        await ctx.close()
        await pw.stop()
        await _kill_proc(proc)

        return {"success": True, "app_id": app_id, "collects_data": False, "already_set": False}

    except Exception:
        await pw.stop()
        await _kill_proc(proc)
        raise


def connect_set_privacy(app_id: str, collects_data: bool = False) -> dict:
    """Set App Privacy declaration in App Store Connect (browser automation).

    connect_set_privacy("<APP_ID>")   → declares no data collected, publishes

    Args:
        app_id: App Store Connect app ID
        collects_data: False (default) = "No, we do not collect data"

    Returns:
        dict with success, app_id, collects_data, already_set
    """
    if collects_data:
        raise NotImplementedError("collects_data=True requires specifying data types — not yet supported")

    return asyncio.run(_set_privacy(app_id))
