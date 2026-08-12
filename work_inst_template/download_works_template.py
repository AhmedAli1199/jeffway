"""
Navigate to EasyBOP import works page and download the
"Download Template (Including Works Instructions)" spreadsheet into this folder.

Uses the same BrowserContext session as the FastAPI app (cookies / storage_state).
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional, Tuple

from playwright.async_api import BrowserContext, Download, Page

log = logging.getLogger("easybop.work_inst_template")

TEMPLATE_BUTTON_ID = "btn_works_template_wi"
TEMPLATE_BUTTON_SELECTOR = f"#{TEMPLATE_BUTTON_ID}"
TEMPLATE_BUTTON_TEXT = "Download Template (Including Works Instructions)"
# Must match `REACTIVE_WORKS_TEMPLATE_XLSX` in filler/main.py (GET serve path for n8n).
CANONICAL_TEMPLATE_FILENAME = "Reactive-Works-Instructions-Template.xlsx"
DEBUG_SCREENSHOT_PREFIX = "debug-works-template"

_BUTTON_READY_JS = """() => {
  const btn = document.getElementById('btn_works_template_wi');
  if (!btn) return false;
  const style = window.getComputedStyle(btn);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
    return false;
  }
  const rect = btn.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return false;
  return !btn.disabled && !btn.classList.contains('ui-state-disabled');
}"""

# Wait until login wall OR import button is in the DOM (not URL — index.php?r=... lies).
_PAGE_STATE_JS = """() => {
  const lb = document.querySelector('#login_box');
  if (lb && lb.offsetParent !== null) return 'login';
  if (document.getElementById('btn_works_template_wi')) return 'import';
  const h1 = document.querySelector('h1');
  const h1txt = ((h1 && h1.textContent) || '').toLowerCase();
  if (h1txt.includes('easybop') && h1txt.includes('login')) return 'login';
  const body = (document.body && document.body.innerText) || '';
  if (body.includes('EasyBOP Login')) return 'login';
  return null;
}"""


def default_output_dir() -> Path:
    return Path(__file__).resolve().parent


async def _on_login_screen(page: Page) -> bool:
    try:
        lb = page.locator("#login_box")
        if await lb.count() > 0 and await lb.first.is_visible():
            return True
    except Exception:
        pass
    if "easybop login" in (await page.title() or "").lower():
        return True
    return await page.get_by_text("EasyBOP Login", exact=False).count() > 0


async def _save_debug_screenshot(page: Page, out_dir: Path, label: str) -> Optional[Path]:
    """Save screenshot to work_inst_template/ for debugging failed runs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"{DEBUG_SCREENSHOT_PREFIX}-{label}-{ts}.png"
    try:
        cdp = await page.context.new_cdp_session(page)
        shot = await cdp.send("Page.captureScreenshot", {"format": "png", "fromSurface": True})
        path.write_bytes(base64.b64decode(shot["data"]))
    except Exception as cdp_exc:
        log.warning("works template: CDP screenshot failed (%s), trying page.screenshot", cdp_exc)
        try:
            await page.screenshot(
                path=str(path),
                full_page=True,
                timeout=8000,
                animations="disabled",
            )
        except Exception as shot_exc:
            log.warning("works template: could not save debug screenshot: %s", shot_exc)
            return None

    log.error(
        "works template: debug screenshot saved %s (url=%s title=%r)",
        path,
        page.url,
        await page.title(),
    )
    return path


async def _navigate_to_import_works(page: Page, url: str, timeout_ms: int) -> None:
    """EasyBOP often never fires domcontentloaded/load; use commit."""
    log.info("works template: navigating to %s", url)
    try:
        await page.goto(url, wait_until="commit", timeout=timeout_ms)
    except Exception as exc:
        log.warning("works template: commit navigation issue (%s), retrying with load", exc)
        try:
            await page.goto(url, wait_until="load", timeout=timeout_ms)
        except Exception as exc2:
            log.warning("works template: load navigation issue (%s)", exc2)
    log.info("works template: navigation committed url=%s", page.url)


async def _wait_for_login_or_import(page: Page, timeout_ms: int) -> str:
    """Poll DOM until login wall or download button appears."""
    log.info("works template: waiting for login screen or import page content")
    handle = await page.wait_for_function(_PAGE_STATE_JS, timeout=timeout_ms)
    state = await handle.json_value()
    log.info(
        "works template: page state=%r url=%s title=%r",
        state,
        page.url,
        await page.title(),
    )
    return state


async def _ensure_authenticated_on_import_page(
    page: Page,
    *,
    context: BrowserContext,
    url: str,
    login: Callable[[Page, str, str], Awaitable[bool]],
    username: str,
    password: str,
    session_path: Path,
    timeout_ms: int,
) -> None:
    """Navigate to import works; re-login automatically if session expired."""
    for attempt in range(2):
        await _navigate_to_import_works(page, url, timeout_ms)
        state = await _wait_for_login_or_import(page, timeout_ms)

        if state == "import":
            return

        if state == "login":
            log.info("works template: session expired — signing in (attempt %s)", attempt + 1)
            ok = await login(page, username, password)
            if not ok:
                raise RuntimeError("EasyBOP login failed")
            await context.storage_state(path=str(session_path))
            continue

        raise RuntimeError(f"Unexpected page state after navigation: {state!r}")

    if await _on_login_screen(page):
        raise RuntimeError("Still on login after sign-in")


async def _wait_for_template_button(page: Page, timeout_ms: int):
    log.info("works template: waiting for %s", TEMPLATE_BUTTON_SELECTOR)
    await page.wait_for_selector(TEMPLATE_BUTTON_SELECTOR, state="attached", timeout=timeout_ms)
    await page.wait_for_function(_BUTTON_READY_JS, timeout=timeout_ms)

    btn = page.locator(TEMPLATE_BUTTON_SELECTOR).first
    if await btn.count() == 0:
        btn = page.locator(
            f"button:has-text('{TEMPLATE_BUTTON_TEXT}')"
        ).first
    if await btn.count() == 0:
        raise RuntimeError(
            f"Download button not found: {TEMPLATE_BUTTON_SELECTOR!r} "
            f"or text {TEMPLATE_BUTTON_TEXT!r}"
        )

    await btn.wait_for(state="visible", timeout=timeout_ms)
    await btn.scroll_into_view_if_needed(timeout=timeout_ms)
    log.info("works template: download button ready")
    return btn


async def _click_template_button(page: Page, btn, timeout_ms: int) -> Download:
    async with page.expect_download(timeout=timeout_ms) as download_info:
        try:
            await btn.click(timeout=timeout_ms)
        except Exception as exc:
            log.warning("works template: Playwright click failed (%s), using JS click", exc)
            clicked = await page.evaluate(
                """() => {
                  const btn = document.getElementById('btn_works_template_wi');
                  if (!btn) return false;
                  btn.click();
                  return true;
                }"""
            )
            if not clicked:
                raise RuntimeError(
                    f"Could not click download button: {TEMPLATE_BUTTON_SELECTOR}"
                ) from exc
    return await download_info.value


async def download_works_instruction_template(
    page: Page,
    *,
    context: BrowserContext,
    base_url: str,
    contract_id: str,
    login: Callable[[Page, str, str], Awaitable[bool]],
    username: str,
    password: str,
    session_path: Path,
    timeout_ms: int,
    output_dir: Optional[Path] = None,
) -> Tuple[Path, str]:
    """
    Open import_works.php, ensure session (login + save storage if needed),
    click #btn_works_template_wi, save download to output_dir.

    Returns (absolute_path_saved, final_page_url).
    """
    out = output_dir or default_output_dir()
    out.mkdir(parents=True, exist_ok=True)

    url = f"{base_url.rstrip('/')}/a_planned_works/z_works/import_works.php?contract_id={contract_id}"
    try:
        await _ensure_authenticated_on_import_page(
            page,
            context=context,
            url=url,
            login=login,
            username=username,
            password=password,
            session_path=session_path,
            timeout_ms=timeout_ms,
        )

        btn = await _wait_for_template_button(page, timeout_ms)
        download = await _click_template_button(page, btn, timeout_ms)

        dest = out / CANONICAL_TEMPLATE_FILENAME
        await download.save_as(str(dest))
        size = dest.stat().st_size
        log.info("works template: saved %s (%s bytes)", dest, size)
        return dest.resolve(), page.url
    except Exception as exc:
        label = type(exc).__name__.lower().replace("error", "")
        await _save_debug_screenshot(page, out, label or "error")
        raise
