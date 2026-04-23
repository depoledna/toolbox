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
from pathlib import Path

from .chrome import kill_chrome_async, launch_chrome

PROFILE_DIR = os.environ.get("CHROME_PROFILE_DIR", os.path.join(str(Path.home()), ".chrome-profile"))
STATE_FILE = os.environ.get("CHROME_STATE_FILE", os.path.join(str(Path.home()), ".chrome-profile", "storage_state.json"))
CDP_PORT = 9233
LOGIN_TIMEOUT = 120


async def _set_privacy(app_id: str) -> dict:
    from playwright.async_api import async_playwright

    proc = await launch_chrome(CDP_PORT, PROFILE_DIR, headless=True)

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
                await kill_chrome_async(proc)
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
        await kill_chrome_async(proc)

        return {"success": True, "app_id": app_id, "collects_data": False, "already_set": False}

    except Exception:
        await pw.stop()
        await kill_chrome_async(proc)
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
