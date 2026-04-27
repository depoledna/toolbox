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

import glob
import shutil
import tempfile

from library.chrome import (
    alloc_port, free_port, cdp_ready, resolve_cdp_ws_url, launch_chrome,
    kill_chrome, kill_chrome_async,
    clone_profile, hide_chrome_pid, show_chrome_pid,
)
from tools._session import get_client_session_id

log = logging.getLogger(__name__)

# --- Config ---

PROFILE_DIR = os.environ.get("CHROME_PROFILE_DIR", os.path.join(str(Path.home()), ".chrome-profile"))
_MAX_SNAPSHOT_CHARS = 50_000
_TEXT_TRUNCATE = 80
_IDLE_TTL = 180  # 3 min
_CLEANUP_INTERVAL = 60
_STALE_PROFILE_AGE = 2 * 24 * 3600  # 2 days
_FS_SWEEP_INTERVAL = 3600  # at most once an hour

_INTERACTIVE_ROLES = frozenset({
    "link", "button", "textbox", "checkbox", "radio", "combobox",
    "menuitem", "tab", "switch", "slider", "searchbox", "option",
    "menuitemcheckbox", "menuitemradio", "spinbutton", "treeitem",
})

# Captures: 1=prefix ("  - "), 2=role, 3=optional quoted name, 4=trailing rest.
# Used by both _process_snapshot (needs all 4) and _fold_repeated (needs 2 + 3 presence).
_LINE_RE = re.compile(r'^(\s*-\s+)(\w+)(?:\s+"([^"]*)")?(.*)$')
_NAME_RE = re.compile(r'^[a-zA-Z0-9_-]{1,32}$')

_OVERLAY_JS = """
(({prompt, timeout}) => {
  if (document.getElementById('__claude_surface__')) return;
  const host = document.createElement('div');
  host.id = '__claude_surface__';
  host.style.cssText = 'position:fixed;top:16px;right:16px;z-index:2147483647;';
  const root = host.attachShadow({mode: 'closed'});
  root.innerHTML = `<div style="font:14px/1.4 -apple-system,sans-serif;background:#1f2937;color:#fff;padding:12px 16px;border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,.3);max-width:320px;">
    <div style="font-weight:600;margin-bottom:6px;">Claude is waiting</div>
    <div id="p" style="margin-bottom:8px;opacity:.85;"></div>
    <div style="display:flex;gap:8px;align-items:center;">
      <button id="b" style="background:#10b981;color:#fff;border:0;padding:8px 14px;border-radius:6px;cursor:pointer;font-weight:600;">Done</button>
      <span id="t" style="opacity:.6;font-variant-numeric:tabular-nums;"></span>
    </div>
  </div>`;
  root.getElementById('p').textContent = prompt;
  document.documentElement.appendChild(host);
  const t = root.getElementById('t');
  let left = timeout;
  const tick = () => { t.textContent = left + 's'; if (--left < 0) clearInterval(iv); };
  tick(); const iv = setInterval(tick, 1000);
  root.getElementById('b').onclick = () => {
    const m = document.createElement('div');
    m.id = '__claude_surface_done_marker__';
    m.style.display = 'none';
    document.body.appendChild(m);
    root.getElementById('b').textContent = 'Sent';
  };
})
"""


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
    cdp: Any = None
    window_id: int = 0
    profile_dir: str = ""


_sessions: dict[str, _BrowserState] = {}
_lock = threading.Lock()
_last_cleanup: float = 0.0
_last_fs_sweep: float = 0.0


def _get_or_create_state(session_id: str) -> _BrowserState:
    global _last_cleanup, _last_fs_sweep
    now = time.time()
    if now - _last_cleanup > _CLEANUP_INTERVAL:
        _last_cleanup = now
        _schedule_cleanup()
    if now - _last_fs_sweep > _FS_SWEEP_INTERVAL:
        _last_fs_sweep = now
        _sweep_stale_profiles()
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


def _sweep_stale_profiles() -> None:
    """Cross-session housekeeping: remove chrome-* tempdirs older than 2 days.

    Covers the gap left by the 3-min idle TTL: server crashes/restarts/idle
    leave temp profiles + orphan Chromes behind otherwise. Kills any Chrome
    process whose --user-data-dir matches a stale dir before deleting it.
    """
    now = time.time()
    pattern = os.path.join(tempfile.gettempdir(), "chrome-*")
    for path in glob.glob(pattern):
        try:
            if not os.path.isdir(path):
                continue
            if now - os.path.getmtime(path) < _STALE_PROFILE_AGE:
                continue
            # Kill any Chrome bound to this profile dir.
            try:
                subprocess.run(
                    ["pkill", "-9", "-f", "--", f"--user-data-dir={path}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=5,
                )
            except Exception:
                pass
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass


# Run once on module load so server restarts don't accumulate orphan profiles.
_sweep_stale_profiles()


# --- Browser Lifecycle ---

async def _launch(state: _BrowserState) -> None:
    """Launch Chrome, connect Playwright + Stagehand.

    On any failure, releases the CDP port and kills Chrome so that subsequent
    retries don't leak ports (previously caused escalating 9241/9242/9243 errors).
    """
    state.cdp_port = await alloc_port()
    try:
        # Each named session gets its own profile clone so parallel Chromes don't
        # collide on the SingletonLock and each agent has isolated cookies/storage.
        if not state.profile_dir:
            state.profile_dir = clone_profile(PROFILE_DIR)
        state.chrome_proc = await launch_chrome(state.cdp_port, state.profile_dir)

        # Playwright
        pw = await async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{state.cdp_port}")
        state.playwright = pw
        state.browser_conn = browser
        state.context = browser.contexts[0] if browser.contexts else await browser.new_context()
        state.page = state.context.pages[0] if state.context.pages else await state.context.new_page()

        # CDP session for window state control. Stored once; reused by `surface`.
        state.cdp = await state.context.new_cdp_session(state.page)
        win = await state.cdp.send("Browser.getWindowForTarget")
        state.window_id = win["windowId"]

        # Pin window to primary-display coords BEFORE hiding. Chrome's default
        # placement can land on a secondary monitor (e.g. Y=-1028 on a stacked
        # display setup); this guarantees `surface` later unhides on the user's
        # main screen. Never reposition while hidden — deadlocks Playwright's CDP.
        await state.cdp.send("Browser.setWindowBounds", {
            "windowId": state.window_id,
            "bounds": {"left": 200, "top": 100, "width": 1200, "height": 900, "windowState": "normal"},
        })

        # Hide before any Stagehand interaction. Stagehand's session.start
        # was found to refocus Chrome and break hide on 2nd+ instances; we
        # now defer Stagehand init until first act/extract call.
        if os.getenv("BROWSER_VISIBLE", "") != "1":
            await _set_window_state(state, "minimized")
    except Exception:
        # Clean up partial launch so the next attempt gets a fresh port + clean state.
        await _shutdown(state)
        raise


def _reset_state(state: _BrowserState) -> None:
    state.chrome_proc = None
    state.playwright = None
    state.browser_conn = None
    state.context = None
    state.page = None
    state.cdp = None
    state.stagehand = None
    state.cdp_port = 0
    state.window_id = 0
    state.stagehand_session_id = ""
    state.profile_dir = ""
    state.ref_map.clear()


async def _shutdown(state: _BrowserState) -> None:
    """Tear down all browser resources. Safe to call on partially-initialized state."""
    for close_fn in [
        lambda: state.stagehand.sessions.end(state.stagehand_session_id) if state.stagehand and state.stagehand_session_id else None,
        lambda: state.stagehand.close() if state.stagehand else None,
        lambda: state.cdp.detach() if state.cdp else None,
        lambda: state.browser_conn.close() if state.browser_conn else None,
        lambda: state.playwright.stop() if state.playwright else None,
    ]:
        try:
            coro = close_fn()
            if coro:
                await coro
        except Exception:
            pass
    await kill_chrome_async(state.chrome_proc)
    if state.cdp_port:
        await free_port(state.cdp_port)
    if state.profile_dir:
        shutil.rmtree(state.profile_dir, ignore_errors=True)
    _reset_state(state)


def _ensure_page(state: _BrowserState) -> None:
    if not state.page or state.page.is_closed():
        raise RuntimeError("No browser open. Use action='go' with a URL first.")


def _require(value: Any, message: str) -> None:
    if not value:
        raise ValueError(message)


async def _ensure_stagehand(state: _BrowserState) -> None:
    """Lazily initialize Stagehand on first act/extract call.

    Stagehand's `sessions.start` re-focuses Chrome and breaks the hidden state
    on 2nd+ launched instances, so we defer it out of the launch path.
    """
    if state.stagehand and state.stagehand_session_id:
        return
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
    state.stagehand_session_id = (
        getattr(getattr(session, "data", None), "session_id", None) or session.id
    )


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

        m = _LINE_RE.match(line)
        if m:
            prefix, role, name, rest = m.group(1), m.group(2), m.group(3) or "", m.group(4)
            name_present = m.group(3) is not None
            display_name = name[:_TEXT_TRUNCATE] + "..." if len(name) > _TEXT_TRUNCATE else name

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

            name_part = f' "{display_name}"' if name_present else ""
            line = prefix + role + name_part + rest

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
        m = _LINE_RE.match(line)
        sig = "" if not m or m.group(3) else f"{m.group(2)}:{line.rstrip().endswith(':')}"

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
                mj = _LINE_RE.match(lines[j])
                jsig = "" if not mj or mj.group(3) else f"{mj.group(2)}:{lines[j].rstrip().endswith(':')}"
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


# --- Window State + Surface ---

async def _set_window_state(state: _BrowserState, window_state: str) -> None:
    """Hide or show this Chrome instance via macOS app-level hide on its PID.

    `window_state="minimized"` hides the app (Cmd+H equivalent — Stage Manager
    respects this; CDP `windowState: minimized` does NOT and the window stays
    visible on multi-display setups).
    `window_state="normal"` unhides and brings the app to the foreground.

    NEVER call CDP `Browser.setWindowBounds` after hiding — it deadlocks
    Playwright's CDP socket. Position bounds before the first hide only.
    """
    if not state.chrome_proc:
        return
    pid = state.chrome_proc.pid
    if window_state == "minimized":
        await hide_chrome_pid(pid)
    elif window_state == "normal":
        await show_chrome_pid(pid)




async def _capture_state(page: Any) -> dict:
    """Snapshot lightweight page identity for before/after diffing."""
    try:
        title = await page.title()
    except Exception:
        title = ""
    return {"url": page.url, "title": title}


def _format_surface_diff(
    before: dict, after: dict, ended_by: str, elapsed_s: int, snapshot_text: str
) -> str:
    """Render the surface-action result for the agent.

    Inputs:
      before/after: {"url": str, "title": str}
      ended_by:     "user_done" | "navigation" | "page_closed" | "timeout"
      elapsed_s:    seconds the user spent in the surfaced window
      snapshot_text: full ARIA snapshot of the post-interaction page
                     (or "(page closed)" if the user closed the window)

    Goal: give the agent enough signal to reason about what happened
    without burying the lede. The agent reads top-down — most decision-
    relevant facts first, then detail.
    """
    header = f"[surface ended_by={ended_by} elapsed={elapsed_s}s]"
    if ended_by == "page_closed":
        return f"{header}\n(page closed)"
    diffs = []
    if before["url"] != after["url"]:
        diffs.append(f"URL: {before['url']} → {after['url']}")
    if before["title"] != after["title"]:
        diffs.append(f"Title: {before['title']} → {after['title']}")
    if not diffs:
        diffs.append("(no URL or title change)")
    return f"{header}\n" + "\n".join(diffs) + f"\n\n{snapshot_text}"


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
    name: str = "",
    url: str = "",
    ref: str = "",
    text: str = "",
    value: str = "",
    key: str = "",
    script: str = "",
    direction: str = "down",
    zoom: float = 1.0,
    snapshot: str = "auto",
    timeout: int = 120,
) -> str:
    """
    Automate a browser. Returns ARIA accessibility snapshot with element refs.

    Each named session gets its own isolated Chrome (own profile, own page, own refs).
    Pick a task-specific name like "checkout-flow" or "alice-login" so parallel agents
    don't collide. The same name across calls reuses the same browser.

    Windows launch hidden by default — set BROWSER_VISIBLE=1 to keep them visible.
    Use action='surface' when the user must interact manually (login, captcha, etc.).

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
      surface  — Unhide the window, show a "Done" overlay with the prompt in `text`,
                  and wait up to `timeout` seconds for one of: Done click, navigation,
                  or window close. Re-hides on exit and returns a state diff
                  (URL/title before→after + post-interaction ARIA snapshot).
      close    — Close browser and free resources.

    Every action except eval/screenshot/extract/close returns an ARIA snapshot.
    Interactive elements have [ref=eN] tags — use these refs in subsequent calls.
    For elements inside iframes or shadow DOM that refs can't reach, use act instead.

    Args:
        action: One of: go, click, type, select, scroll, press, back, forward, refresh, eval, screenshot, act, extract, surface, close
        name: Required. Unique session label (1-32 chars: a-z, A-Z, 0-9, _, -).
              Pick something task-specific so parallel agents don't collide.
        url: Target URL (for action=go)
        ref: Element reference from snapshot, e.g. "e5" (for click/type/select)
        text: Text to type (for type), natural language instruction (for act/extract),
              prompt shown to the user (for surface)
        value: Option value (for select), JSON schema string (for extract)
        key: Key name to press (for press) — Enter, Tab, Escape, ArrowDown, etc.
        script: JavaScript to evaluate (for eval)
        direction: Scroll direction: "up" or "down" (for scroll, default "down")
        zoom: Zoom level as float (1.0 = 100%, 1.5 = 150%, 0.5 = 50%). Applied via CSS zoom before action.
        snapshot: Response mode: "auto" (include snapshot), "none" (skip snapshot)
        timeout: Surface wait timeout in seconds (default 120). Only used by action='surface'.

    Returns:
        ARIA snapshot with element refs, or action-specific result
    """
    if not _NAME_RE.match(name):
        return (
            "Error: 'name' parameter required. Pick a unique task-specific identifier "
            "(alphanumeric, hyphens, underscores; 1-32 chars). "
            "Example: name='checkout-flow'."
        )
    session_id = get_client_session_id()
    state = _get_or_create_state(f"{session_id}::{name}")

    # `surface` runs for minutes — bypass the 60s wrapper. Its own `timeout` param caps it.
    # surface manages its own show/hide; never auto-rehide here.
    if action == "surface":
        try:
            return await _run_action(state, action, url, ref, text, value, key, script, direction, zoom, snapshot, timeout)
        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            log.exception("browser tool error")
            return f"Error: {type(e).__name__}: {e}"

    try:
        result = await asyncio.wait_for(
            _run_action(state, action, url, ref, text, value, key, script, direction, zoom, snapshot, timeout),
            timeout=60,
        )
    except asyncio.TimeoutError:
        return "Error: timeout after 60s"
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        log.exception("browser tool error")
        return f"Error: {type(e).__name__}: {e}"

    # Re-hide after every action. CDP calls (page.goto, click, etc.) and
    # Stagehand can re-show Chrome at any point during a tool call. The 2nd+
    # launched Chrome instances are especially prone to staying visible after
    # launch even when the initial hide fired. Cheapest robust fix is to
    # re-assert hide on every action that's not surface or close.
    if action != "close" and os.getenv("BROWSER_VISIBLE", "") != "1" and state.chrome_proc:
        await _set_window_state(state, "minimized")

    return result


async def _run_action(
    state: _BrowserState, action: str, url: str, ref: str, text: str,
    value: str, key: str, script: str, direction: str, zoom: float,
    snapshot: str, timeout: int = 120,
) -> str:
    # Apply zoom if non-default
    if zoom != 1.0 and state.page:
        await state.page.evaluate(f"document.body.style.zoom = '{zoom}'")

    if action == "go":
        _require(url, "url is required for action='go'")
        if not state.page or state.page.is_closed():
            if state.page:
                # Closed-page carcass from a prior page_close — tear down the
                # whole stack (Chrome, Playwright, CDP) so the fresh _launch
                # doesn't collide on CDP port or leak state.
                await _shutdown(state)
            await _launch(state)
        await state.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await state.page.wait_for_timeout(500)

    elif action == "click":
        _ensure_page(state)
        _require(ref, "ref is required for action='click'")
        await (await _resolve_ref(state, ref)).click(timeout=5000)
        await state.page.wait_for_timeout(300)

    elif action == "type":
        _ensure_page(state)
        _require(ref, "ref is required for action='type'")
        await (await _resolve_ref(state, ref)).fill(text, timeout=5000)

    elif action == "select":
        _ensure_page(state)
        _require(ref, "ref is required for action='select'")
        await (await _resolve_ref(state, ref)).select_option(value, timeout=5000)

    elif action == "scroll":
        _ensure_page(state)
        await state.page.mouse.wheel(0, -500 if direction == "up" else 500)
        await state.page.wait_for_timeout(300)

    elif action == "press":
        _ensure_page(state)
        _require(key, "key is required for action='press'")
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
        _require(script, "script is required for action='eval'")
        result = await state.page.evaluate(script)
        return f"Result: {json.dumps(result, default=str, ensure_ascii=False)}"

    elif action == "screenshot":
        _ensure_page(state)
        path = f"/tmp/browser_{int(time.time())}_{os.getpid()}.png"
        await state.page.screenshot(path=path, full_page=False)
        return f"Screenshot saved: {path}"

    elif action == "act":
        _ensure_page(state)
        _require(text, "text is required for action='act'")
        await _ensure_stagehand(state)
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
        _require(text, "text is required for action='extract'")
        await _ensure_stagehand(state)
        schema = json.loads(value) if value else {"type": "object"}
        result = await state.stagehand.sessions.extract(
            state.stagehand_session_id, instruction=text, schema=schema, timeout=30.0,
        )
        r = getattr(getattr(result, "data", None), "result", None)
        return f"Result: {json.dumps(r, default=str, ensure_ascii=False)}" if r else f"Result: {result}"

    elif action == "surface":
        _ensure_page(state)
        before = await _capture_state(state.page)
        hide_after = os.getenv("BROWSER_VISIBLE", "") != "1"
        started = time.time()
        await _set_window_state(state, "normal")

        nav_event = asyncio.Event()
        close_event = asyncio.Event()

        # Use page.on() rather than page.wait_for_event() to avoid Playwright's
        # default 30s timeout phantom-firing as a fake signal. Predicate lets
        # us filter iframe nav noise.
        main_frame = state.page.main_frame

        def _on_framenavigated(frame):
            if frame == main_frame:
                nav_event.set()

        def _on_page_close(_=None):
            close_event.set()

        state.page.on("framenavigated", _on_framenavigated)
        state.page.on("close", _on_page_close)

        await state.page.evaluate(_OVERLAY_JS, {"prompt": text or "Click Done when finished", "timeout": timeout})

        # Done signal: the overlay button injects #__claude_surface_done_marker__
        # into the body. We wait for that selector with timeout=0 (disabled).
        # This sidesteps expose_function's "doesn't apply to already-loaded
        # pages" quirk — DOM mutation signals work on any loaded page.
        async def _wait_for_done_marker():
            try:
                await state.page.wait_for_selector(
                    "#__claude_surface_done_marker__", state="attached", timeout=0,
                )
            except Exception:
                # Page closed or similar — let close_event or timeout win.
                await asyncio.Event().wait()

        done_task    = asyncio.create_task(_wait_for_done_marker())
        nav_task     = asyncio.create_task(nav_event.wait())
        close_task   = asyncio.create_task(close_event.wait())
        timeout_task = asyncio.create_task(asyncio.sleep(timeout))
        pending = {nav_task, close_task, done_task, timeout_task}

        try:
            finished, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in pending:
                t.cancel()
            try: state.page.remove_listener("framenavigated", _on_framenavigated)
            except Exception: pass
            try: state.page.remove_listener("close", _on_page_close)
            except Exception: pass

        if done_task in finished:    ended_by = "user_done"
        elif nav_task in finished:   ended_by = "navigation"
        elif close_task in finished: ended_by = "page_closed"
        else:                        ended_by = "timeout"

        elapsed = int(time.time() - started)

        if ended_by != "page_closed":
            try:
                await state.page.evaluate(
                    "document.getElementById('__claude_surface__')?.remove();"
                    "document.getElementById('__claude_surface_done_marker__')?.remove();"
                )
            except Exception:
                pass
            if hide_after:
                await _set_window_state(state, "minimized")
            after = await _capture_state(state.page)
            snapshot_text = await _take_snapshot(state)
        else:
            after = before
            snapshot_text = "(page closed)"

        return _format_surface_diff(before, after, ended_by, elapsed, snapshot_text)

    elif action == "close":
        await _shutdown(state)
        return "Browser closed."

    else:
        return f"Error: unknown action '{action}'. Valid: go, click, type, select, scroll, press, back, forward, refresh, eval, screenshot, act, extract, surface, close"

    if snapshot != "none":
        return await _take_snapshot(state)
    return "OK"
