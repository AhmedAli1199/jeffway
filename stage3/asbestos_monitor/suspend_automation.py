"""
EasyBOP Bulk Suspend Automation.

For each address supplied:
  1. Navigate to the planned-works index.
  2. Wait for jqGrid to load all rows.
  3. Use JS to check the checkbox for each matching address row.
  4. Click #btn_bulk_suspend ("Bulk Suspend Works").
  5. In the popup select reason: "Awaiting asbestos info" (value=1466).
  6. Click #btn_dialog_suspend_works (Suspend).
  7. Accept the browser confirm dialog ("Suspend Works?" — OK).

Returns:
  { total_requested, total_checked, checked, not_found, success, message }
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, List, Optional

from playwright.async_api import Dialog, Page

log = logging.getLogger("asbestos.suspend")


def _log_address_match_summary(
    requested: List[str],
    checked: List[str],
    not_found: List[str],
) -> None:
    log.info("suspend: address match summary")
    log.info("  requested: %d", len(requested))
    log.info("  checked:   %d", len(checked))
    log.info("  not found: %d", len(not_found))
    if checked:
        log.info("  checked addresses:")
        for index, address in enumerate(checked, start=1):
            log.info("    %3d. %s", index, address)
    if not_found:
        log.warning("  not found on page:")
        for index, address in enumerate(not_found, start=1):
            log.warning("    %3d. %s", index, address)


LOGIN_URL  = "https://easybop.co.uk/"
INDEX_URL  = "https://easybop.co.uk/a_planned_works/z_works/index.php?contract_id={contract_id}"

SUSPEND_REASON_VALUE = "1466"
SUSPEND_REASON_LABEL = "Awaiting asbestos info"

_DIALOG_HANDLER_ATTR = "_easybop_dialog_accept_wired"


def _ensure_browser_dialog_auto_accept(page: Page) -> None:
    """
    Register a persistent dialog handler (same as WI upload / BOQ upload).
    Must be set before any click that triggers window.confirm().
    """
    if getattr(page, _DIALOG_HANDLER_ATTR, False):
        return

    async def _accept_native_dialog(dialog: Dialog) -> None:
        log.info('suspend: native browser dialog "%s" — clicking OK', dialog.message)
        await dialog.accept()

    page.on("dialog", _accept_native_dialog)
    setattr(page, _DIALOG_HANDLER_ATTR, True)
    log.info("suspend: registered persistent browser dialog auto-accept handler")


async def _wait_for_suspend_ui_settled(page: Page, timeout_ms: int) -> None:
    """Poll until suspend dialog is gone and jqGrid is not showing load overlay."""
    deadline = time.monotonic() + (timeout_ms / 1000)
    poll_interval_s = 0.4
    poll_n = 0

    log.info("suspend: waiting for post-suspend UI to settle (up to %ss)…", timeout_ms // 1000)

    while time.monotonic() < deadline:
        poll_n += 1
        status = await page.evaluate(
            """() => {
              const uiDialog = document.querySelector('.ui-dialog:not([style*="display: none"])');
              const uiVisible = !!(uiDialog && uiDialog.offsetParent !== null);
              const loadEl = document.getElementById('load_list');
              const gridLoading = !!(loadEl && loadEl.offsetParent !== null
                && loadEl.style.display !== 'none');
              return { uiVisible, gridLoading };
            }"""
        )
        ui_visible = bool(status.get("uiVisible"))
        grid_loading = bool(status.get("gridLoading"))

        if not ui_visible and not grid_loading:
            log.info("suspend: UI settled after %d poll(s)", poll_n)
            return

        if poll_n == 1 or poll_n % 10 == 0:
            log.info(
                "suspend: still waiting — ui_dialog=%s grid_loading=%s",
                ui_visible,
                grid_loading,
            )
        await asyncio.sleep(poll_interval_s)

    log.warning("suspend: UI did not fully settle within %sms — continuing", timeout_ms)


# ---------------------------------------------------------------------------
# Login helpers
# ---------------------------------------------------------------------------

async def _page_looks_like_login(page: Page) -> bool:
    try:
        lb = page.locator("#login_box")
        if await lb.count() > 0 and await lb.first.is_visible():
            return True
        if "login" in (await page.title() or "").lower():
            return True
        return await page.get_by_text("EasyBOP Login", exact=False).count() > 0
    except Exception:
        return False


async def _login(page: Page, username: str, password: str) -> bool:
    log.info("login: opening %s", LOGIN_URL)
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")

    if await page.locator("a[href*='logout'], a[href*='log_out'], a[href*='sign_out']").count() > 0:
        log.info("login: already authenticated")
        return True

    box = page.locator("#login_box")
    try:
        await box.wait_for(state="visible", timeout=12_000)
    except Exception as e:
        log.error("login: login_box not visible: %s", e)
        return False

    try:
        await box.locator("input[type='text']").first.fill(username)
        await box.locator("input[type='password']").first.fill(password)
        await page.locator("#btn_login").click()
        await page.wait_for_function(
            "() => { const o=document.querySelector('a[href*=\"logout\"],a[href*=\"log_out\"]'); "
            "const lb=document.querySelector('#login_box'); "
            "return o!==null||(lb&&lb.offsetParent===null); }",
            timeout=20_000,
        )
        log.info("login: success url=%s", page.url)
        return True
    except Exception as e:
        log.exception("login: failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Navigate to index
# ---------------------------------------------------------------------------

async def _ensure_index_ready(
    page: Page,
    contract_id: str,
    username: Optional[str],
    password: Optional[str],
    timeout_ms: int,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> None:
    url = INDEX_URL.format(contract_id=contract_id)
    log.info("suspend: navigate -> %s", url)
    await page.goto(url, wait_until="domcontentloaded")

    if await _page_looks_like_login(page):
        if not username or not password:
            raise RuntimeError("Login required but credentials not set in .env")
        log.info("suspend: session expired — re-logging in")
        if not await _login(page, username, password):
            raise RuntimeError("EasyBOP re-login failed")
        if after_relogin:
            await after_relogin()
        await page.goto(url, wait_until="domcontentloaded")

    if await _page_looks_like_login(page):
        raise RuntimeError("Still on login wall after re-login — check credentials")


# ---------------------------------------------------------------------------
# Wait for jqGrid
# ---------------------------------------------------------------------------

async def _wait_for_grid(page: Page, timeout_ms: int) -> None:
    log.info("suspend: waiting for jqGrid...")
    try:
        await page.wait_for_function(
            "() => {"
            "  const el = document.getElementById('load_list');"
            "  return !el || el.style.display === 'none' || !el.offsetParent;"
            "}",
            timeout=timeout_ms,
        )
    except Exception as e:
        log.warning("suspend: grid overlay timed out: %s", e)

    try:
        await page.wait_for_selector("tr.jqgrow", state="attached", timeout=timeout_ms)
        log.info("suspend: grid rows found")
    except Exception as e:
        raise RuntimeError(f"jqGrid rows never appeared within {timeout_ms}ms: {e}") from e

    await asyncio.sleep(1.0)


# ---------------------------------------------------------------------------
# JS: check checkboxes for matching addresses
# ---------------------------------------------------------------------------

_CHECK_JS = """
(addresses) => {
    const results = {};
    addresses.forEach(a => { results[a] = false; });

    const cells = document.querySelectorAll('td[aria-describedby="list_address"]');
    cells.forEach(cell => {
        const cellAddr = (cell.getAttribute('title') || cell.textContent || '').trim();
        if (!cellAddr) return;

        const match = addresses.find(a => {
            const cleanA    = a.toLowerCase().trim();
            const cleanCell = cellAddr.toLowerCase();
            return cleanA.length > 0 &&
                   (cleanCell.includes(cleanA) || cleanA.includes(cleanCell));
        });

        if (match) {
            const row = cell.closest('tr');
            if (!row) return;
            const cb = row.querySelector('input[type="checkbox"].cbox');
            if (cb && !cb.checked) {
                cb.click();
                results[match] = true;
            } else if (cb && cb.checked) {
                results[match] = true; // already checked
            }
        }
    });

    return results;
}
"""


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

async def suspend_addresses(
    page: Page,
    addresses: List[str],
    contract_id: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout_ms: int = 30_000,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> dict:
    """
    1. Navigate to the planned-works index.
    2. Wait for jqGrid to load.
    3. Check checkboxes for each matching address.
    4. Click #btn_bulk_suspend.
    5. In the popup select "Awaiting asbestos info" (value=1466).
    6. Click #btn_dialog_suspend_works and accept the browser confirm (OK).
    """
    addresses = [a.strip() for a in addresses if a and a.strip()]
    if not addresses:
        return {
            "total_requested": 0,
            "total_checked": 0,
            "not_found": [],
            "checked": [],
            "success": False,
            "message": "No valid addresses provided.",
        }

    log.info("suspend: processing %d addresses", len(addresses))

    # Accept native confirm() dialogs whenever they appear (WI upload pattern)
    _ensure_browser_dialog_auto_accept(page)

    # ── Step 1: navigate ──────────────────────────────────────────────────
    await _ensure_index_ready(page, contract_id, username, password, timeout_ms, after_relogin)

    # ── Step 2: wait for grid ─────────────────────────────────────────────
    await _wait_for_grid(page, timeout_ms)

    # ── Step 3: check checkboxes ──────────────────────────────────────────
    log.info("suspend: matching %d addresses against grid rows", len(addresses))
    check_results: dict = await page.evaluate(_CHECK_JS, addresses)

    checked   = [a for a, ok in check_results.items() if ok]
    not_found = [a for a, ok in check_results.items() if not ok]

    _log_address_match_summary(addresses, checked, not_found)

    if not checked:
        return {
            "total_requested": len(addresses),
            "total_checked": 0,
            "not_found": not_found,
            "checked": checked,
            "success": False,
            "message": "None of the provided addresses were found in the grid.",
        }

    await asyncio.sleep(0.5)

    # ── Step 4: click Bulk Suspend button ────────────────────────────────
    try:
        suspend_btn = page.locator("#btn_bulk_suspend")
        await suspend_btn.wait_for(state="visible", timeout=10_000)
        await suspend_btn.click()
        log.info("suspend: clicked #btn_bulk_suspend")
    except Exception as e:
        return {
            "total_requested": len(addresses),
            "total_checked": len(checked),
            "not_found": not_found,
            "checked": checked,
            "success": False,
            "message": f"Could not click Bulk Suspend button: {e}",
        }

    # ── Step 5: handle the popup ─────────────────────────────────────────
    try:
        # Wait for the dialog to appear
        dialog = page.locator(".ui-dialog:visible, [role='dialog']:visible")
        await dialog.first.wait_for(state="visible", timeout=8_000)
        log.info("suspend: popup appeared")

        # Find the suspend reason select and choose "Awaiting asbestos info"
        reason_select = page.locator("select").filter(
            has=page.locator(f"option[value='{SUSPEND_REASON_VALUE}']")
        ).first
        await reason_select.wait_for(state="visible", timeout=5_000)
        await reason_select.select_option(value=SUSPEND_REASON_VALUE)
        log.info("suspend: selected reason '%s' (value=%s)", SUSPEND_REASON_LABEL, SUSPEND_REASON_VALUE)

        suspend_confirm_btn = page.locator("#btn_dialog_suspend_works")
        await suspend_confirm_btn.wait_for(state="visible", timeout=5_000)
        await suspend_confirm_btn.scroll_into_view_if_needed()
        await asyncio.sleep(0.3)

        # Click Suspend — page.on("dialog") accepts "Suspend Works?" when it appears
        try:
            await suspend_confirm_btn.click(timeout=5_000)
            log.info("suspend: clicked #btn_dialog_suspend_works")
        except Exception:
            log.warning("suspend: normal click failed — retrying with force")
            await suspend_confirm_btn.click(force=True, timeout=5_000)
            log.info("suspend: force-clicked #btn_dialog_suspend_works")

        # Brief pause so the dialog handler can run if confirm appeared
        await asyncio.sleep(0.5)
        await _wait_for_suspend_ui_settled(page, timeout_ms=max(timeout_ms, 30_000))

    except Exception as e:
        return {
            "total_requested": len(addresses),
            "total_checked": len(checked),
            "not_found": not_found,
            "checked": checked,
            "success": False,
            "message": f"Popup handling failed: {e}",
        }

    await asyncio.sleep(1.0)

    log.info("suspend: bulk suspend completed")
    log.info("  reason: %s (value=%s)", SUSPEND_REASON_LABEL, SUSPEND_REASON_VALUE)
    log.info("  checked: %d / %d", len(checked), len(addresses))
    if not_found:
        log.info("  not found: %d", len(not_found))

    msg = (
        f"Bulk suspend completed. "
        f"Checked {len(checked)}/{len(addresses)} rows with reason '{SUSPEND_REASON_LABEL}'. "
        + (f"Not found on page: {len(not_found)} address(es)." if not_found else "All addresses found.")
    )

    return {
        "total_requested": len(addresses),
        "total_checked": len(checked),
        "not_found": not_found,
        "checked": checked,
        "success": True,
        "message": msg,
    }
