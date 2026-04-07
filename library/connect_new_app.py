"""
Create a new app in App Store Connect via browser automation.

Auth is handled automatically using the same Chrome profile as apple_ads:
- First run or expired session: opens visible Chrome for manual Apple login
- Subsequent runs: fully headless using saved cookies

Usage:
    from library import connect_new_app
    result = connect_new_app("My App", sku="my-app-001", project="/path/to/App.xcodeproj")
    result = connect_new_app("My App", "dp.My-App", "my-app-001")
"""
import asyncio
import os
import re
import subprocess
import urllib.request
from pathlib import Path

CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PROFILE_DIR = os.environ.get("CHROME_PROFILE_DIR", os.path.join(str(Path.home()), ".chrome-profile"))
STATE_FILE = os.environ.get("CHROME_STATE_FILE", os.path.join(str(Path.home()), ".chrome-profile", "storage_state.json"))
ASC_URL = "https://appstoreconnect.apple.com/apps"
IDENTIFIERS_URL = "https://developer.apple.com/account/resources/identifiers/list"
CDP_PORT = 9232
LOGIN_TIMEOUT = 120

PLATFORM_MAP = {
    "ios": "platformsById.IOS",
    "macos": "platformsById.MAC_OS",
    "tvos": "platformsById.TV_OS",
    "visionos": "platformsById.VISION_OS",
}


def _read_bundle_id(project: str) -> str | None:
    """Extract PRODUCT_BUNDLE_IDENTIFIER from .xcodeproj/project.pbxproj."""
    pbxproj = Path(project) / "project.pbxproj"
    if not pbxproj.exists():
        return None
    content = pbxproj.read_text()
    matches = re.findall(r'PRODUCT_BUNDLE_IDENTIFIER\s*=\s*"?([^";]+)"?\s*;', content)
    # Filter out test targets (contain "Tests" or "UITests")
    app_ids = [m for m in matches if "Test" not in m]
    return app_ids[0] if app_ids else (matches[0] if matches else None)


def _generate_sku(name: str) -> str:
    """Generate a SKU from app name: 'Arcana Calendar' → 'arcana-calendar'."""
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')


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


async def _kill_all_chrome():
    p = await asyncio.create_subprocess_exec(
        "pkill", "-f", "Google Chrome",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await p.wait()
    await asyncio.sleep(1)


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


async def _is_authenticated(page) -> bool:
    url = page.url
    return "login" not in url and "authResult" not in url


async def _do_visible_login() -> bool:
    from playwright.async_api import async_playwright

    await _kill_all_chrome()
    proc = await _launch_chrome(CDP_PORT, headless=False)

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(f"http://localhost:{CDP_PORT}")
        ctx = browser.contexts[0]

        # Reuse existing ASC tab if one exists, otherwise open new
        page = None
        for p in ctx.pages:
            if "appstoreconnect" in p.url:
                page = p
                break
        if not page:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(ASC_URL, wait_until="networkidle", timeout=30000)

        print("[Auth] Please log in to App Store Connect in the browser window...")
        elapsed = 0
        while elapsed < LOGIN_TIMEOUT:
            if await _is_authenticated(page):
                await ctx.storage_state(path=STATE_FILE)
                print("[Auth] Logged in! Session saved.")
                return True
            await asyncio.sleep(5)
            elapsed += 5
            remaining = LOGIN_TIMEOUT - elapsed
            if remaining > 0 and remaining % 15 == 0:
                print(f"[Auth] Waiting... {remaining}s remaining")

        print("[Auth] Login timeout expired.")
        return False
    finally:
        await pw.stop()
        await _kill_proc(proc)
        await _kill_all_chrome()


async def _find_bundle_id_option(page, bundle_id: str) -> str | None:
    """Check if bundle_id exists in the New App dialog's dropdown. Returns matched value or None."""
    return await page.evaluate(f"""() => {{
        const sel = document.querySelector('#bundleId');
        if (!sel) return null;
        for (const opt of sel.options) {{
            if (opt.value === '{bundle_id}' || opt.value.includes('{bundle_id}') || opt.textContent.includes('{bundle_id}')) {{
                return opt.value;
            }}
        }}
        return null;
    }}""")


async def _register_bundle_id(page, bundle_id: str, description: str):
    """Register a new Bundle ID on developer.apple.com.

    Must navigate from the identifiers list page (direct URL hits team selection error).
    Flow: list → click + → select App IDs → Continue → select App → Continue → fill form → Continue → Register
    """
    # Step 1: Go to identifiers list
    await page.goto(IDENTIFIERS_URL, wait_until="networkidle", timeout=30000)
    await asyncio.sleep(3)

    # Check auth on developer portal
    if "signin" in page.url:
        raise RuntimeError("Not authenticated on developer.apple.com — login required")

    # Step 2: Click + to add new identifier
    await page.click('a[aria-label="Add new identifier"]')
    await asyncio.sleep(3)

    # Step 3: Select "App IDs" → Continue
    await page.check('#bundleId', force=True)
    await page.click('#action-continue')
    await asyncio.sleep(3)

    # Step 4: Select "App" (not App Clip) → Continue
    await page.check('#bundle', force=True)
    await page.click('#action-continue')
    await asyncio.sleep(5)

    # Step 5: Wait for form to render, then fill
    await page.wait_for_selector('#description', timeout=10000)
    await page.fill('#description', description)
    await page.check('#explicit', force=True)
    await page.fill('#identifier', bundle_id)

    # Step 6: Click Continue
    continue_btn = page.locator('button#action-save, button#action-continue, button:has-text("Continue")')
    await continue_btn.first.click()
    await asyncio.sleep(5)

    # Step 7: Confirmation page — click Register
    register_btn = page.locator('button#action-save, button:has-text("Register")')
    await register_btn.first.click()
    await asyncio.sleep(5)

    # Verify success — page redirects to list or shows identifier details
    url = page.url
    body = await page.evaluate("() => document.body?.innerText || ''")
    if "identifiers" in url.lower() or bundle_id in body or "Registration complete" in body:
        return  # Success — landed on list or detail page
    if "already exists" in body.lower() or "already in use" in body.lower():
        return  # Already registered, that's fine
    raise RuntimeError(f"Bundle ID registration may have failed: {body[:200]}")


async def _create_app(
    name: str,
    bundle_id: str,
    sku: str,
    platforms: list[str],
    language: str,
    user_access: str,
) -> dict:
    from playwright.async_api import async_playwright
    import os

    # Launch headless Chrome
    proc = await _launch_chrome(CDP_PORT, headless=True)

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(f"http://localhost:{CDP_PORT}")

        # Load saved auth if available
        if os.path.exists(STATE_FILE):
            ctx = await browser.new_context(storage_state=STATE_FILE)
        else:
            ctx = await browser.new_context()

        page = await ctx.new_page()
        await page.goto(ASC_URL, wait_until="networkidle", timeout=30000)

        # Check auth
        if not await _is_authenticated(page):
            await ctx.close()
            await pw.stop()
            await _kill_proc(proc)

            success = await _do_visible_login()
            if not success:
                raise RuntimeError(f"Manual login timed out after {LOGIN_TIMEOUT}s")

            # Retry headless
            proc = await _launch_chrome(CDP_PORT, headless=True)
            pw = await async_playwright().start()
            browser = await pw.chromium.connect_over_cdp(f"http://localhost:{CDP_PORT}")
            ctx = await browser.new_context(storage_state=STATE_FILE)
            page = await ctx.new_page()
            await page.goto(ASC_URL, wait_until="networkidle", timeout=30000)

        # Open New App dialog
        await page.click('button[aria-label="New App"]')
        await asyncio.sleep(1)
        await page.click('text="New App"')
        await asyncio.sleep(2)

        # Check dialog opened
        dialog = await page.evaluate(
            "() => { const d = document.querySelector('[role=\"dialog\"]'); return d ? d.innerText.substring(0, 50) : ''; }"
        )
        if "New App" not in dialog:
            raise RuntimeError("Failed to open New App dialog")

        # Fill platforms
        for p in platforms:
            field_name = PLATFORM_MAP.get(p.lower().strip())
            if field_name:
                await page.locator(f'input[name="{field_name}"]').check(force=True)

        # Fill text fields
        await page.fill('#name', name[:30])
        await page.select_option('#primaryLocale', language)

        # Bundle ID: check if it exists in dropdown, register if not
        bundle_id_registered = False
        matched = await _find_bundle_id_option(page, bundle_id)

        if not matched:
            # Close dialog, register bundle ID, come back
            await page.click('text="Cancel"')
            await asyncio.sleep(1)

            await _register_bundle_id(page, bundle_id, name)
            bundle_id_registered = True

            # Navigate back to ASC and reopen dialog
            await page.goto(ASC_URL, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(2)
            await page.click('button[aria-label="New App"]')
            await asyncio.sleep(1)
            await page.click('text="New App"')
            await asyncio.sleep(2)

            # Re-fill fields that were lost
            for p in platforms:
                field_name = PLATFORM_MAP.get(p.lower().strip())
                if field_name:
                    await page.locator(f'input[name="{field_name}"]').check(force=True)
            await page.fill('#name', name[:30])
            await page.select_option('#primaryLocale', language)

            matched = await _find_bundle_id_option(page, bundle_id)
            if not matched:
                raise RuntimeError(f"Bundle ID '{bundle_id}' was registered but not found in ASC dropdown")

        await page.select_option('#bundleId', matched)
        await page.fill('#sku', sku)

        # User access
        access_id = '#userAccessFull' if user_access == 'full' else '#userAccessLimited'
        await page.check(access_id, force=True)

        # Click Create
        create_btn = page.locator('[role="dialog"] button:has-text("Create")')
        await create_btn.click()
        await asyncio.sleep(5)

        # Check for validation errors
        error = await page.evaluate("""() => {
            const d = document.querySelector('[role="dialog"]');
            if (!d) return '';
            const errs = d.querySelectorAll('[class*="error"], [class*="Error"], [role="alert"]');
            return Array.from(errs).map(e => e.textContent.trim()).join('; ');
        }""")

        # Dialog still open = validation error
        dialog_still = await page.evaluate(
            "() => !!document.querySelector('[role=\"dialog\"]')"
        )
        if dialog_still:
            dialog_text = await page.evaluate(
                "() => document.querySelector('[role=\"dialog\"]')?.innerText?.substring(0, 500) || ''"
            )
            raise RuntimeError(f"App creation failed. Dialog: {dialog_text}")

        # Extract app ID — click the new app to get its URL with ID
        current_url = page.url
        app_id_match = re.search(r'/apps/(\d+)', current_url)
        app_id = app_id_match.group(1) if app_id_match else None

        if not app_id:
            # Page stayed on /apps — click the new app to get its ID
            await page.goto(ASC_URL, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(2)
            try:
                await page.click(f'text="{name}"', timeout=5000)
                await asyncio.sleep(3)
                app_id_match = re.search(r'/apps/(\d+)', page.url)
                app_id = app_id_match.group(1) if app_id_match else None
            except Exception:
                pass

        # Save updated cookies
        await ctx.storage_state(path=STATE_FILE)

        await ctx.close()
        await pw.stop()
        await _kill_proc(proc)

        return {
            "app_id": app_id,
            "name": name,
            "platforms": platforms,
            "bundle_id": bundle_id,
            "sku": sku,
            "url": f"https://appstoreconnect.apple.com/apps/{app_id}" if app_id else current_url,
            "bundle_id_registered": bundle_id_registered,
        }

    except Exception:
        await pw.stop()
        await _kill_proc(proc)
        raise


def connect_new_app(
    name: str,
    bundle_id: str = "",
    sku: str = "",
    platform: str = "ios",
    language: str = "en-US",
    user_access: str = "full",
    project: str = "",
) -> dict:
    """Create a new app in App Store Connect.

    Bundle ID and SKU are auto-derived if not provided:
    - project given → reads PRODUCT_BUNDLE_IDENTIFIER from .xcodeproj
    - no project → generates from name (e.g., "My App" → "dp.My-App")
    - SKU auto-generated from name (e.g., "My App" → "my-app")

    If the bundle ID doesn't exist on developer.apple.com, it's registered automatically.

    connect_new_app("My App", project="/path/to/MyApp.xcodeproj")    → reads bundle ID from project
    connect_new_app("My App", "dp.My-App", "my-app-001")             → explicit bundle ID + SKU
    connect_new_app("My App")                                         → auto-generates everything

    Args:
        name: App name (max 30 chars, must be unique on App Store)
        bundle_id: Bundle ID (optional — read from project or auto-generated)
        sku: Unique identifier (optional — auto-generated from name)
        platform: "ios", "macos", "tvos", "visionos" or comma-separated combo
        language: Primary language code (default "en-US")
        user_access: "full" or "limited" (default "full")
        project: Path to .xcodeproj (optional — used to read bundle ID)

    Returns:
        dict with app_id, name, platforms, bundle_id, sku, url, bundle_id_registered
    """
    platforms = [p.strip() for p in platform.split(",")]

    for p in platforms:
        if p not in PLATFORM_MAP:
            raise ValueError(f"Unknown platform '{p}'. Use: {', '.join(PLATFORM_MAP)}")

    if len(name) > 30:
        raise ValueError(f"Name must be ≤30 chars, got {len(name)}")

    if user_access not in ("full", "limited"):
        raise ValueError("user_access must be 'full' or 'limited'")

    # Resolve bundle_id
    if not bundle_id and project:
        bundle_id = _read_bundle_id(project)
        if not bundle_id:
            raise ValueError(f"Could not read PRODUCT_BUNDLE_IDENTIFIER from {project}")
    if not bundle_id:
        slug = re.sub(r'[^a-zA-Z0-9]+', '-', name).strip('-')
        bundle_id = f"dp.{slug}"

    # Resolve SKU
    if not sku:
        sku = _generate_sku(name)

    return asyncio.run(_create_app(name, bundle_id, sku, platforms, language, user_access))
