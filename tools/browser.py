"""
Browser automation via Playwright + Stagehand + Chrome CDP.

Playwright: deterministic ops (goto, scroll, eval, screenshot, ARIA snapshot).
Stagehand: AI-powered ops (act, extract) — traverses iframes, shadow DOM, consent dialogs.

Session persists across calls. Chrome profile reused for cookie persistence.
"""

import asyncio
import json
import logging
import os
import re
import subprocess
from pathlib import Path
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import async_playwright
from stagehand import AsyncStagehand

from library.chrome import alloc_port, free_port, cdp_ready, resolve_cdp_ws_url, launch_chrome, kill_chrome
from tools._session import get_client_session_id

log = logging.getLogger(__name__)

# --- Config ---

PROFILE_DIR = os.environ.get("CHROME_PROFILE_DIR", os.path.join(str(Path.home()), ".chrome-profile"))
_MAX_SNAPSHOT_CHARS = 50_000
_TEXT_TRUNCATE = 80
_IDLE_TTL = 1800  # 30 min
_CLEANUP_INTERVAL = 300

_INTERACTIVE_ROLES = frozenset({
    "link", "button", "textbox", "checkbox", "radio", "combobox",
    "menuitem", "tab", "switch", "slider", "searchbox", "option",
    "menuitemcheckbox", "menuitemradio", "spinbutton", "treeitem",
})

_ROLE_RE = re.compile(r'^(\s*-\s+)(\w+)(.*)')
_ROLE_NAME_RE = re.compile(r'^\s*-\s+(\w+)(?:\s+"([^"]*)")?')


# --- Session State ---

@dataclass
class _BrowserState:
    chrome_proc: subprocess.Popen | None = None
    playwright: Any = None
    browser_conn: Any = None
    context: Any = None
    page: Any = None
    cdp_port: int = 0
    ref_map: dict[str, dict] = field(default_factory=dict)
    last_used: float = field(default_factory=time.time)
    stagehand: Any = None
    stagehand_session_id: str = ""


_sessions: dict[str, _BrowserState] = {}
_lock = threading.Lock()
_last_cleanup: float = 0.0


def _get_or_create_state(session_id: str) -> _BrowserState:
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup > _CLEANUP_INTERVAL:
        _last_cleanup = now
        _schedule_cleanup()
    with _lock:
        state = _sessions.get(session_id)
        if state is None:
            state = _BrowserState()
            _sessions[session_id] = state
        state.last_used = now
        return state


def _schedule_cleanup():
    stale: list[tuple[str, _BrowserState]] = []
    now = time.time()
    with _lock:
        for sid, state in list(_sessions.items()):
            if now - state.last_used > _IDLE_TTL:
                stale.append((sid, state))
        for sid, _ in stale:
            del _sessions[sid]
    for _, state in stale:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_shutdown(state))
        except RuntimeError:
            kill_chrome(state.chrome_proc)


# --- Browser Lifecycle ---

async def _launch(state: _BrowserState) -> None:
    """Launch Chrome, connect Playwright + Stagehand."""
    state.cdp_port = await alloc_port()
    state.chrome_proc = await launch_chrome(state.cdp_port, PROFILE_DIR)

    # Playwright
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{state.cdp_port}")
    state.playwright = pw
    state.browser_conn = browser
    state.context = browser.contexts[0] if browser.contexts else await browser.new_context()
    state.page = state.context.pages[0] if state.context.pages else await state.context.new_page()

    # Stagehand (local mode, same Chrome via CDP WebSocket)
    model_key = os.getenv("OPENROUTER_KEY", "")
    os.environ["OPENAI_BASE_URL"] = "https://openrouter.ai/api/v1"
    cdp_ws_url = resolve_cdp_ws_url(state.cdp_port)
    state.stagehand = AsyncStagehand(
        server="local",
        model_api_key=model_key,
        local_openai_api_key=model_key,
        browserbase_api_key="local",
        browserbase_project_id="local",
        local_ready_timeout_s=30.0,
    )
    session = await state.stagehand.sessions.start(
        model_name="openai/gpt-5.4-nano",
        browser={"type": "local", "launchOptions": {"cdpUrl": cdp_ws_url}},
    )
    state.stagehand_session_id = getattr(getattr(session, "data", None), "session_id", None) or session.id


async def _shutdown(state: _BrowserState) -> None:
    for close_fn in [
        lambda: state.stagehand.sessions.end(state.stagehand_session_id) if state.stagehand and state.stagehand_session_id else None,
        lambda: state.stagehand.close() if state.stagehand else None,
        lambda: state.browser_conn.close() if state.browser_conn else None,
        lambda: state.playwright.stop() if state.playwright else None,
    ]:
        try:
            coro = close_fn()
            if coro:
                await coro
        except Exception:
            pass
    kill_chrome(state.chrome_proc)
    if state.cdp_port:
        await free_port(state.cdp_port)
    state.chrome_proc = None
    state.playwright = None
    state.browser_conn = None
    state.context = None
    state.page = None
    state.cdp_port = 0
    state.ref_map.clear()
    state.stagehand = None
    state.stagehand_session_id = ""


def _ensure_page(state: _BrowserState) -> None:
    if not state.page:
        raise RuntimeError("No browser open. Use action='go' with a URL first.")


# --- ARIA Snapshot Processing ---

def _process_snapshot(raw: str, ref_map: dict[str, dict]) -> str:
    """Add [ref=eN] tags to interactive elements, strip URLs, truncate, fold."""
    ref_map.clear()
    result: list[str] = []
    counter = 0
    nth_tracker: dict[tuple[str, str], int] = {}

    for line in raw.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("- /url:") or stripped.startswith("/url:"):
            continue

        m = _ROLE_RE.match(line)
        if m:
            prefix, role, rest = m.group(1), m.group(2), m.group(3)
            name_m = re.search(r'"([^"]*)"', rest)
            name = name_m.group(1) if name_m else ""

            if len(name) > _TEXT_TRUNCATE:
                rest = rest.replace(f'"{name}"', f'"{name[:_TEXT_TRUNCATE]}..."', 1)

            if role in _INTERACTIVE_ROLES:
                counter += 1
                ref_id = f"e{counter}"
                nth = nth_tracker.get((role, name), 0)
                nth_tracker[(role, name)] = nth + 1
                ref_map[ref_id] = {"role": role, "name": name, "nth": nth}
                if rest.rstrip().endswith(":"):
                    rest = rest.rstrip()[:-1] + f" [ref={ref_id}]:"
                else:
                    rest += f" [ref={ref_id}]"

            line = prefix + role + rest

        result.append(line)

    result = _fold_repeated(result)
    body = "\n".join(result)

    # Prune refs that were folded away
    visible = set(re.findall(r'\[ref=(e\d+)\]', body))
    for r in [r for r in ref_map if r not in visible]:
        del ref_map[r]

    if len(body) > _MAX_SNAPSHOT_CHARS:
        body = body[:_MAX_SNAPSHOT_CHARS] + "\n... [snapshot truncated]"
    return body


def _fold_repeated(lines: list[str]) -> list[str]:
    """Fold 4+ consecutive unnamed siblings with the same role."""
    if len(lines) < 5:
        return lines

    output: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        indent = len(line) - len(line.lstrip())
        m = _ROLE_NAME_RE.match(line)
        sig = "" if not m or m.group(2) else f"{m.group(1)}:{line.rstrip().endswith(':')}"

        if not sig:
            output.append(line)
            i += 1
            continue

        # Find extent of consecutive siblings with same signature
        j = i + 1
        while j < len(lines):
            ji = len(lines[j]) - len(lines[j].lstrip())
            if ji < indent:
                break
            if ji == indent:
                mj = _ROLE_NAME_RE.match(lines[j])
                jsig = "" if not mj or mj.group(2) else f"{mj.group(1)}:{lines[j].rstrip().endswith(':')}"
                if jsig != sig:
                    break
            j += 1

        siblings = [k for k in range(i, j) if len(lines[k]) - len(lines[k].lstrip()) == indent]
        if len(siblings) > 3:
            first_end = siblings[1] if len(siblings) > 1 else j
            output.extend(lines[i:first_end])
            role_label = sig.split(":")[0]
            output.append(" " * indent + f"- ... {len(siblings) - 1} more {role_label} items")
            i = j
        else:
            output.append(line)
            i += 1

    return output


async def _take_snapshot(state: _BrowserState) -> str:
    try:
        raw = await state.page.locator(":root").aria_snapshot()
    except Exception as e:
        log.warning(f"ARIA snapshot failed: {e}")
        return f"Page: (snapshot failed: {type(e).__name__})\nURL: {state.page.url}"

    if not raw or not raw.strip():
        return f"Page: (empty)\nURL: {state.page.url}"

    body = _process_snapshot(raw, state.ref_map)
    title = await state.page.title()
    return f"Page: {title}\nURL: {state.page.url}\n{body}"


# --- Ref Resolution ---

async def _resolve_ref(state: _BrowserState, ref_id: str):
    info = state.ref_map.get(ref_id)
    if not info:
        raise ValueError(
            f"ref '{ref_id}' not found ({len(state.ref_map)} refs available). "
            "Refs expire after each snapshot. Use action='go' to refresh."
        )

    role, name, nth = info["role"], info["name"], info.get("nth", 0)

    # Try exact match first, then fuzzy
    for exact in (True, False):
        locator = state.page.get_by_role(role, name=name, exact=exact)
        count = await locator.count()
        if count > 0:
            break
    else:
        raise ValueError(
            f"Element for ref '{ref_id}' ({role} \"{name}\") not found. "
            "Page may have changed."
        )

    if count > 1 and nth < count:
        return locator.nth(nth)
    if count == 1 and nth > 0:
        raise ValueError(
            f"Element for ref '{ref_id}' expected nth={nth} but only 1 match. Page changed."
        )
    return locator


# --- Main Tool ---

async def browser(
    action: str,
    url: str = "",
    ref: str = "",
    text: str = "",
    value: str = "",
    key: str = "",
    script: str = "",
    direction: str = "down",
    zoom: float = 1.0,
    snapshot: str = "auto",
) -> str:
    """
    Automate a browser. Returns ARIA accessibility snapshot with element refs.

    Actions:
      go       — Navigate to URL. Launches browser on first call.
      click    — Click element by ref (from snapshot).
      type     — Type text into element by ref. Clears existing value first.
      select   — Select dropdown option by ref and value.
      scroll   — Scroll page up or down.
      press    — Press keyboard key (Enter, Tab, Escape, ArrowDown, etc.).
      back     — Navigate back.
      forward  — Navigate forward.
      eval     — Run JavaScript and return result.
      screenshot — Save screenshot to /tmp, return path. Combine with zoom for scaled capture.
      refresh  — Reload the current page.
      act      — AI-powered action via natural language (e.g. "click the consent button").
                  Handles iframes, shadow DOM, dynamic content automatically. Uses LLM.
      extract  — AI-powered structured data extraction. Pass instruction in text,
                  JSON schema in value. Uses LLM.
      close    — Close browser and free resources.

    Every action except eval/screenshot/extract/close returns an ARIA snapshot.
    Interactive elements have [ref=eN] tags — use these refs in subsequent calls.
    For elements inside iframes or shadow DOM that refs can't reach, use act instead.

    Args:
        action: One of: go, click, type, select, scroll, press, back, forward, refresh, eval, screenshot, act, extract, close
        url: Target URL (for action=go)
        ref: Element reference from snapshot, e.g. "e5" (for click/type/select)
        text: Text to type (for type), natural language instruction (for act/extract)
        value: Option value (for select), JSON schema string (for extract)
        key: Key name to press (for press) — Enter, Tab, Escape, ArrowDown, etc.
        script: JavaScript to evaluate (for eval)
        direction: Scroll direction: "up" or "down" (for scroll, default "down")
        zoom: Zoom level as float (1.0 = 100%, 1.5 = 150%, 0.5 = 50%). Applied via CSS zoom before action.
        snapshot: Response mode: "auto" (include snapshot), "none" (skip snapshot)

    Returns:
        ARIA snapshot with element refs, or action-specific result
    """
    session_id = get_client_session_id()
    state = _get_or_create_state(session_id)

    try:
        return await asyncio.wait_for(
            _run_action(state, action, url, ref, text, value, key, script, direction, zoom, snapshot),
            timeout=60,
        )
    except asyncio.TimeoutError:
        return "Error: timeout after 60s"
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        log.exception("browser tool error")
        return f"Error: {type(e).__name__}: {e}"


async def _run_action(
    state: _BrowserState, action: str, url: str, ref: str, text: str,
    value: str, key: str, script: str, direction: str, zoom: float,
    snapshot: str,
) -> str:
    # Apply zoom if non-default
    if zoom != 1.0 and state.page:
        await state.page.evaluate(f"document.body.style.zoom = '{zoom}'")

    if action == "go":
        if not url:
            return "Error: url is required for action='go'"
        if not state.page:
            await _launch(state)
        await state.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await state.page.wait_for_timeout(500)

    elif action == "click":
        _ensure_page(state)
        if not ref:
            return "Error: ref is required for action='click'"
        await (await _resolve_ref(state, ref)).click(timeout=5000)
        await state.page.wait_for_timeout(300)

    elif action == "type":
        _ensure_page(state)
        if not ref:
            return "Error: ref is required for action='type'"
        await (await _resolve_ref(state, ref)).fill(text, timeout=5000)

    elif action == "select":
        _ensure_page(state)
        if not ref:
            return "Error: ref is required for action='select'"
        await (await _resolve_ref(state, ref)).select_option(value, timeout=5000)

    elif action == "scroll":
        _ensure_page(state)
        await state.page.mouse.wheel(0, -500 if direction == "up" else 500)
        await state.page.wait_for_timeout(300)

    elif action == "press":
        _ensure_page(state)
        if not key:
            return "Error: key is required for action='press'"
        await state.page.keyboard.press(key)
        await state.page.wait_for_timeout(200)

    elif action == "back":
        _ensure_page(state)
        await state.page.go_back(timeout=10000)
        await state.page.wait_for_timeout(500)

    elif action == "forward":
        _ensure_page(state)
        await state.page.go_forward(timeout=10000)
        await state.page.wait_for_timeout(500)

    elif action == "refresh":
        _ensure_page(state)
        await state.page.reload(wait_until="domcontentloaded", timeout=30000)
        await state.page.wait_for_timeout(500)

    elif action == "eval":
        _ensure_page(state)
        if not script:
            return "Error: script is required for action='eval'"
        result = await state.page.evaluate(script)
        return f"Result: {json.dumps(result, default=str, ensure_ascii=False)}"

    elif action == "screenshot":
        _ensure_page(state)
        path = f"/tmp/browser_{int(time.time())}_{os.getpid()}.png"
        await state.page.screenshot(path=path, full_page=False)
        return f"Screenshot saved: {path}"

    elif action == "act":
        _ensure_page(state)
        if not text:
            return "Error: text is required for action='act'"
        result = await state.stagehand.sessions.act(
            state.stagehand_session_id, input=text, timeout=30.0,
        )
        r = getattr(getattr(result, "data", None), "result", None)
        msg = f"success={r.success}, message={r.message}" if r and hasattr(r, "success") else str(result)
        if snapshot != "none":
            return f"[act: {msg}]\n\n{await _take_snapshot(state)}"
        return f"[act: {msg}]"

    elif action == "extract":
        _ensure_page(state)
        if not text:
            return "Error: text is required for action='extract'"
        schema = json.loads(value) if value else {"type": "object"}
        result = await state.stagehand.sessions.extract(
            state.stagehand_session_id, instruction=text, schema=schema, timeout=30.0,
        )
        r = getattr(getattr(result, "data", None), "result", None)
        return f"Result: {json.dumps(r, default=str, ensure_ascii=False)}" if r else f"Result: {result}"

    elif action == "close":
        await _shutdown(state)
        return "Browser closed."

    else:
        return f"Error: unknown action '{action}'. Valid: go, click, type, select, scroll, press, back, forward, refresh, eval, screenshot, act, extract, close"

    if snapshot != "none":
        return await _take_snapshot(state)
    return "OK"
