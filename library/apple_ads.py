"""
Apple Search Ads keyword research via headless Chrome.

Navigates Apple Search Ads campaign setup, searches for keyword
recommendations, and returns results with popularity scores.

Auth is handled automatically:
- First run: saves session cookies after manual Apple 2FA login
- Subsequent runs: fully headless using saved cookies
- Expired session: auto-detects, launches visible Chrome for re-login

Supports parallel batch queries with isolated Chrome sessions:
    results = apple_ads_keywords(["ai chat", "fitness", "coin flip"])

Usage:
    results = apple_ads_keywords("ai chat")
    results = apple_ads_keywords(["ai chat", "fitness"], top_n=10)
"""

import asyncio
import os
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import quote_plus

CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CAMPAIGN_URL = os.environ.get("APPLE_ADS_CAMPAIGN_URL", "")
APP_NAME = os.environ.get("APPLE_ADS_APP_NAME", "")
BID = "1"
CDP_PORT_BASE = 9224
CDP_PORT_VISIBLE = 9223
PROFILE_DIR = os.environ.get("CHROME_PROFILE_DIR", os.path.join(str(Path.home()), ".chrome-profile"))
STATE_FILE = os.environ.get("CHROME_STATE_FILE", os.path.join(str(Path.home()), ".chrome-profile", "storage_state.json"))
LOGIN_TIMEOUT = 120
MAX_PARALLEL = 5

_port_lock = asyncio.Lock()
_used_ports: set[int] = set()

_APP_DETAIL_JS = """() => {
    const r = { name: '', developer: '', rating: null, ratingCount: '', lastUpdate: '', version: '' };

    const h1 = document.querySelector('h1');
    if (h1) r.name = h1.textContent.trim();

    const h2s = document.querySelectorAll('h2');
    for (const h2 of h2s) {
        const text = h2.textContent.trim();
        if (text.startsWith('More by ')) {
            r.developer = text.replace('More by ', '');
            break;
        }
    }
    if (!r.developer) {
        const dts = document.querySelectorAll('dt');
        for (const dt of dts) {
            if (dt.textContent.trim() === 'Seller') {
                const dd = dt.nextElementSibling;
                if (dd) r.developer = dd.textContent.trim();
                break;
            }
        }
    }

    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
        const t = walker.currentNode.textContent.trim();
        const el = walker.currentNode.parentElement;
        const rect = el.getBoundingClientRect();

        if (t === 'out of 5' && rect.y > 800) {
            const parent = el.parentElement;
            for (const d of parent.querySelectorAll('div, span')) {
                const v = parseFloat(d.textContent.trim());
                if (v > 0 && v <= 5 && d.textContent.trim().length < 4) {
                    r.rating = v;
                    break;
                }
            }
        }

        if (t.includes('Ratings') && !t.includes('&') && rect.y > 800)
            r.ratingCount = t;

        if (t.startsWith('Version '))
            r.version = t.replace('Version ', '');
    }

    const timeEl = document.querySelector('time');
    if (timeEl) r.lastUpdate = timeEl.textContent.trim();

    return r;
}"""


# ── Port allocation ──────────────────────────────────────────


async def _alloc_port() -> int:
    async with _port_lock:
        port = CDP_PORT_BASE
        while port in _used_ports:
            port += 1
        _used_ports.add(port)
        return port


async def _free_port(port: int):
    async with _port_lock:
        _used_ports.discard(port)


# ── Chrome lifecycle ─────────────────────────────────────────


def _cdp_ready(port: int) -> bool:
    try:
        urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=3)
        return True
    except Exception:
        return False


async def _kill_proc(proc: subprocess.Popen | None):
    """Terminate a specific Chrome process by PID."""
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
    """Kill all Chrome instances (used for auth login only)."""
    p = await asyncio.create_subprocess_exec(
        "pkill", "-f", "Google Chrome",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await p.wait()
    await asyncio.sleep(1)


async def _launch_chrome(port: int, profile_dir: str, headless: bool = True) -> subprocess.Popen:
    args = [
        CHROME_PATH,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
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


# ── Auth ─────────────────────────────────────────────────────


async def _is_authenticated(page) -> bool:
    try:
        body = await page.evaluate(
            "() => document.body?.innerText?.substring(0, 500) || ''"
        )
        return any(s in body for s in ["Create Campaign", "Campaign Settings", "All Campaigns"])
    except Exception:
        # Navigation during check (e.g. post-login redirect) — treat as authenticated
        return True


async def _do_visible_login() -> bool:
    """Launch visible Chrome, wait for manual login, save state."""
    from playwright.async_api import async_playwright

    await _kill_all_chrome()
    proc = await _launch_chrome(CDP_PORT_VISIBLE, PROFILE_DIR, headless=False)

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(f"http://localhost:{CDP_PORT_VISIBLE}")
        ctx = browser.contexts[0]

        page = None
        for p in ctx.pages:
            if "app-ads.apple.com" in p.url:
                page = p
                break
        if not page:
            page = await ctx.new_page()
            await page.goto(CAMPAIGN_URL, wait_until="networkidle", timeout=30000)

        print("[Auth] Please log in to Apple in the browser window...")
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
        await _kill_all_chrome()  # ensure no visible Chrome lingers


_auth_lock = asyncio.Lock()


async def _ensure_auth():
    """Re-login if needed. Only called when a session detects expired auth."""
    async with _auth_lock:
        # Double-check: another session might have already re-authed
        if os.path.exists(STATE_FILE):
            # Quick probe with a temp session
            port = await _alloc_port()
            profile_dir = tempfile.mkdtemp(prefix="chrome_auth_")
            try:
                proc = await _launch_chrome(port, profile_dir, headless=True)
                from playwright.async_api import async_playwright
                pw = await async_playwright().start()
                browser = await pw.chromium.connect_over_cdp(f"http://localhost:{port}")
                ctx = await browser.new_context(storage_state=STATE_FILE)
                page = await ctx.new_page()
                await page.goto(CAMPAIGN_URL, wait_until="networkidle", timeout=30000)
                authed = await _is_authenticated(page)
                await ctx.close()
                await pw.stop()
                await _kill_proc(proc)
                if authed:
                    return
            except Exception:
                pass
            finally:
                await _free_port(port)
                shutil.rmtree(profile_dir, ignore_errors=True)

        success = await _do_visible_login()
        if not success:
            raise RuntimeError(f"Manual login timed out after {LOGIN_TIMEOUT}s. Run again and log in.")


# ── Session management ───────────────────────────────────────


async def _create_session() -> dict:
    """Create an isolated headless Chrome session with temp profile + shared auth."""
    from playwright.async_api import async_playwright

    if not os.path.exists(STATE_FILE):
        await _ensure_auth()

    port = await _alloc_port()
    profile_dir = tempfile.mkdtemp(prefix="chrome_ads_")
    proc = None
    try:
        proc = await _launch_chrome(port, profile_dir, headless=True)

        pw = await async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(f"http://localhost:{port}")
        ctx = await browser.new_context(storage_state=STATE_FILE)
        page = await ctx.new_page()
        await page.goto(CAMPAIGN_URL, wait_until="networkidle", timeout=30000)

        if not await _is_authenticated(page):
            # Auth expired — clean up this session, re-login, retry
            await ctx.close()
            await pw.stop()
            await _kill_proc(proc)
            await _free_port(port)
            shutil.rmtree(profile_dir, ignore_errors=True)

            await _ensure_auth()

            # Retry with fresh session
            port = await _alloc_port()
            profile_dir = tempfile.mkdtemp(prefix="chrome_ads_")
            proc = await _launch_chrome(port, profile_dir, headless=True)
            pw = await async_playwright().start()
            browser = await pw.chromium.connect_over_cdp(f"http://localhost:{port}")
            ctx = await browser.new_context(storage_state=STATE_FILE)
            page = await ctx.new_page()
            await page.goto(CAMPAIGN_URL, wait_until="networkidle", timeout=30000)

        return {"pw": pw, "ctx": ctx, "page": page, "proc": proc, "port": port, "profile_dir": profile_dir}
    except Exception:
        if proc:
            await _kill_proc(proc)
        await _free_port(port)
        shutil.rmtree(profile_dir, ignore_errors=True)
        raise


async def _destroy_session(session: dict):
    """Clean up a Chrome session."""
    try:
        await session["ctx"].close()
    except Exception:
        pass
    try:
        await session["pw"].stop()
    except Exception:
        pass
    await _kill_proc(session["proc"])
    await _free_port(session["port"])
    shutil.rmtree(session["profile_dir"], ignore_errors=True)


# ── Campaign flow ────────────────────────────────────────────


async def _run_campaign_flow(page, app_name: str, country: str, bid: str):
    """Navigate through campaign setup to reach the Add Keywords screen."""
    # Type app name (page already at CAMPAIGN_URL from session creation)
    inp = page.locator('input.form-input__target[placeholder*="app name"]')
    await inp.click()
    await inp.type(app_name, delay=15)

    # Wait for dropdown, then select app
    await page.wait_for_selector('li.menu__item', state='visible', timeout=5000)
    await page.locator(f'li.menu__item:has-text("{app_name}")').first.click()
    await asyncio.sleep(2)

    # Select Search Results placement (shadow DOM radio)
    await page.evaluate("""() => {
        for (const sel of document.querySelectorAll('apui-wc-selector')) {
            if (sel.shadowRoot) {
                const r = sel.shadowRoot.querySelector('#APPSTORE_SEARCH_RESULTS');
                if (r) { r.click(); return; }
            }
        }
    }""")
    await asyncio.sleep(1)

    # Continue (1)
    await page.locator('apui-wc-button:has-text("Continue")').click()
    country_input = page.locator('input[placeholder="Enter country or region"]')
    await country_input.wait_for(state='visible', timeout=10000)

    # Type country
    await country_input.scroll_into_view_if_needed()
    await country_input.click(timeout=10000)
    await country_input.type(country, delay=15)
    await page.wait_for_selector(f'li:has-text("{country}")', state='visible', timeout=5000)
    await page.locator(f'li:has-text("{country}")').first.click()
    await asyncio.sleep(1)

    # Continue (2) — reveals full campaign form
    await page.locator('apui-wc-button:has-text("Continue")').click()
    await asyncio.sleep(3)

    # Select Manage Bids (shadow DOM radio) — reveals bid input
    await page.evaluate("""() => {
        for (const sel of document.querySelectorAll('apui-wc-selector')) {
            if (sel.shadowRoot) {
                const r = sel.shadowRoot.querySelector('#MANUAL_CPT');
                if (r) { r.click(); return; }
            }
        }
    }""")
    await asyncio.sleep(2)

    # Set CPT Bid
    bid_input = page.locator('input[name="apui-wc-input-9"]')
    await bid_input.scroll_into_view_if_needed()
    await bid_input.click()
    await bid_input.fill(bid)
    await asyncio.sleep(1)

    # Add Keywords — opens keyword research modal
    add_kw = page.locator('text=Add Keywords to an Ad Group')
    await add_kw.scroll_into_view_if_needed()
    await add_kw.click()
    await page.locator('neo-keyword-popularity input[type="text"]').first.wait_for(
        state='visible', timeout=10000
    )


# ── Data extraction ──────────────────────────────────────────


async def _get_suggested_bid(page) -> str:
    """Extract the Suggested Max CPT Bid from the Add Keywords modal header."""
    bid = await page.evaluate(r"""() => {
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        let foundLabel = false;
        while (walker.nextNode()) {
            const t = walker.currentNode.textContent.trim();
            if (t === 'Default Max CPT Bid:') {
                foundLabel = true;
                continue;
            }
            if (foundLabel) {
                const match = t.match(/[€$£¥][\d.,]+/);
                if (match) return match[0];
                if (t.length > 0) foundLabel = false;
            }
        }
        const allEls = document.querySelectorAll('span');
        for (const el of allEls) {
            const t = el.textContent.trim();
            const match = t.match(/^[€$£¥][\d.,]+$/);
            if (match) {
                const prev = el.previousElementSibling || el.parentElement;
                if (prev && prev.textContent.includes('Default Max CPT Bid')) {
                    return match[0];
                }
            }
        }
        return null;
    }""")
    return bid


async def _search_and_extract(page, keyword: str) -> dict:
    """Search a related keyword and extract recommendations + suggested bid."""
    search = page.locator('neo-keyword-popularity input[type="text"]').first
    await asyncio.sleep(2)  # widget needs time to become interactive after modal opens
    await search.click()
    await search.fill(keyword)
    await search.press('Enter')
    await asyncio.sleep(3)

    keywords = await page.evaluate("""() => {
        const container = document.querySelector('neo-keyword-popularity');
        if (!container) return [];
        const items = container.querySelectorAll('.kp-list-item');
        const results = [];
        items.forEach(item => {
            const title = item.querySelector('.kp-list-item__title')?.textContent?.trim();
            const dots = item.querySelector('apui-wc-popularity-dots');
            const popularity = dots ? parseInt(dots.getAttribute('value') || '0') : null;
            if (title) results.push({ keyword: title, popularity });
        });
        return results;
    }""")

    suggested_bid = await _get_suggested_bid(page)

    return {"keywords": keywords, "suggested_bid": suggested_bid}


async def _fetch_app_detail(ctx, app_url: str) -> dict:
    """Fetch details for a single App Store listing in its own page."""
    page = await ctx.new_page()
    try:
        await page.goto(app_url, wait_until="networkidle", timeout=20000)
        return await page.evaluate(_APP_DETAIL_JS)
    except Exception:
        return {"name": "", "developer": "", "rating": None, "ratingCount": "", "lastUpdate": "", "version": ""}
    finally:
        await page.close()


async def _appstore_competitors(ctx, keyword: str, top_n: int = 5, max_concurrent: int = 3) -> dict:
    """Search App Store for keyword and extract top N app details in parallel."""
    search_page = await ctx.new_page()
    try:
        url = f"https://apps.apple.com/us/iphone/search?term={quote_plus(keyword)}"
        await search_page.goto(url, wait_until="networkidle", timeout=30000)
        try:
            await search_page.wait_for_selector('a[href*="/app/"]', state='visible', timeout=5000)
        except Exception:
            return {"total_results": 0, "apps": []}

        app_urls = await search_page.evaluate("""() => {
            const results = [];
            const seen = new Set();
            document.querySelectorAll('a[href*="/app/"]').forEach(el => {
                const href = el.href;
                if (seen.has(href)) return;
                seen.add(href);
                const rect = el.getBoundingClientRect();
                if (rect.width > 50 && rect.height > 50) results.push(href);
            });
            return results;
        }""")
    finally:
        await search_page.close()

    total_results = len(app_urls)

    sem = asyncio.Semaphore(max_concurrent)

    async def _fetch_with_limit(app_url: str) -> dict:
        async with sem:
            return await _fetch_app_detail(ctx, app_url)

    apps = await asyncio.gather(*[_fetch_with_limit(u) for u in app_urls[:top_n]])

    return {"total_results": total_results, "apps": list(apps)}


# ── Single keyword flow ──────────────────────────────────────


async def _run_single_keyword(keyword: str, country: str, top_n: int) -> dict:
    """Run keyword research in its own isolated Chrome session."""
    session = await _create_session()
    try:
        await _run_campaign_flow(session["page"], APP_NAME, country, BID)

        ads_data, competitors = await asyncio.gather(
            _search_and_extract(session["page"], keyword),
            _appstore_competitors(session["ctx"], keyword, top_n),
        )

        return {**ads_data, "competitors": competitors}
    finally:
        await _destroy_session(session)


# ── Batch orchestration ──────────────────────────────────────


async def _batch_keywords_async(
    keywords: list[str],
    country: str,
    top_n: int,
) -> list[dict]:
    """Run multiple keyword searches in parallel, each in its own Chrome session."""
    sem = asyncio.Semaphore(MAX_PARALLEL)

    async def _run_with_limit(kw: str) -> dict:
        async with sem:
            return await _run_single_keyword(kw, country, top_n)

    return list(await asyncio.gather(*[_run_with_limit(kw) for kw in keywords]))


async def _single_keyword_async(
    keyword: str,
    country: str,
    top_n: int,
) -> dict:
    return await _run_single_keyword(keyword, country, top_n)


# ── Formatting ───────────────────────────────────────────────


def _format_results(keyword: str, data: dict) -> str:
    """Format combined keyword + competitor data for LLM consumption."""
    keywords = data.get("keywords", [])
    suggested_bid = data.get("suggested_bid")
    competitors = data.get("competitors", {})
    comp_apps = competitors.get("apps", [])
    total_apps = competitors.get("total_results", 0)

    lines = []

    lines.append(f"Apple Ads keyword research for '{keyword}'")
    lines.append(f"Keywords: {len(keywords)} | Suggested CPT Bid: {suggested_bid or 'N/A'}")
    lines.append("")

    by_pop = {}
    for item in keywords:
        by_pop.setdefault(item["popularity"], []).append(item["keyword"])

    for pop in sorted(by_pop.keys(), reverse=True):
        kws = by_pop[pop]
        dots = '●' * pop + '○' * (5 - pop)
        lines.append(f"{dots} ({pop}/5) — {len(kws)} keywords:")
        lines.append(", ".join(kws))
        lines.append("")

    if comp_apps:
        lines.append(f"App Store competition: {total_apps} apps found, top {len(comp_apps)}:")
        for i, app in enumerate(comp_apps, 1):
            rating = f"{app['rating']}/5" if app['rating'] else "N/A"
            lines.append(
                f"  {i}. {app['name']}"
                f" — {app['developer'] or '?'}"
                f" | {rating} ({app['ratingCount']})"
                f" | Updated: {app['lastUpdate']} v{app['version']}"
            )

        ratings = [a['rating'] for a in comp_apps if a['rating']]
        if ratings:
            avg = sum(ratings) / len(ratings)
            weak = sum(1 for r in ratings if r <= 3.5)
            lines.append(f"\n  Avg rating: {avg:.1f}/5 | Weak (≤3.5★): {weak}/{len(ratings)}")

    return "\n".join(lines).rstrip()


# ── Public API ───────────────────────────────────────────────


def apple_ads_keywords(
    keyword: str | list[str],
    country: str = "United States",
    top_n: int = 5,
    raw: bool = False,
) -> str | dict | list:
    """Search Apple Ads keywords + App Store competitor analysis.

    Supports single keyword or batch (parallel) mode with isolated Chrome sessions:
        apple_ads_keywords("ai chat")
        apple_ads_keywords(["ai chat", "fitness", "coin flip"])

    Args:
        keyword: Single keyword or list of keywords to search.
        country: Country/region (default: "United States")
        top_n: Number of top App Store competitors to analyze (default: 5)
        raw: If True, return raw dict(s).

    Returns:
        Single: formatted report or raw dict.
        Batch: list of formatted reports or list of raw dicts.

    Usage:
        apple_ads_keywords("ai chat")
        apple_ads_keywords("coin flip", top_n=10)
        apple_ads_keywords(["ai chat", "fitness", "games"])
    """
    if isinstance(keyword, list):
        results = asyncio.run(_batch_keywords_async(keyword, country, top_n))
        if raw:
            return results
        return [_format_results(kw, data) for kw, data in zip(keyword, results)]

    data = asyncio.run(_single_keyword_async(keyword, country, top_n))
    if raw:
        return data
    return _format_results(keyword, data)
