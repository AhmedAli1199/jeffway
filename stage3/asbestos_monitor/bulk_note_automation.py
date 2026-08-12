"""
EasyBOP Bulk Note Automation  (bulk_notes.php).

Flow
----
1. Navigate to bulk_notes.php?contract_id=<contract_id>.
2. Wait for the jqGrid to load.
3. Use JS to tick the checkbox for each address that matches a grid row
   (same fuzzy-match strategy as suspend_automation.py).
4. Click #btn_addnotes ("Add Notes to Selected Works").
5. In the popup dialog:
   a. Select the requested category from #category_id (or equivalent select).
   b. Fill the note text into #notes (or equivalent textarea).
   c. Click #btn_save_changes ("Add Note").
   d. Accept native browser confirm ("Save changes?" — OK).
   e. Wait for save to finish (dynamic poll).
   f. Click #btn_dialog_cancel (Close).
6. Return a summary dict.

Supported modes
---------------
  "no_asbestos"     → category value 1716  ("No Asbestos with order")
  "report_ordered"  → category value 1758  ("Asbestos Report Ordered")
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, List, Literal, Optional

from playwright.async_api import Dialog, Page

log = logging.getLogger("asbestos.bulk_note")

LOGIN_URL   = "https://easybop.co.uk/"
BULK_URL    = "https://easybop.co.uk/a_planned_works/z_works/bulk_notes.php?contract_id={contract_id}"

# Category options
CATEGORY_NO_ASBESTOS   = "1716"   # "No Asbestos with order"
CATEGORY_REPORT_ORDERED = "1758"  # "Asbestos Report Ordered"

NoteMode = Literal["no_asbestos", "report_ordered"]

_DIALOG_HANDLER_ATTR = "_easybop_dialog_accept_wired"


def _ensure_browser_dialog_auto_accept(page: Page) -> None:
    """Register persistent handler for window.confirm() — same as WI / suspend upload."""
    if getattr(page, _DIALOG_HANDLER_ATTR, False):
        return

    async def _accept_native_dialog(dialog: Dialog) -> None:
        log.info('bulk_note: native browser dialog "%s" — clicking OK', dialog.message)
        await dialog.accept()

    page.on("dialog", _accept_native_dialog)
    setattr(page, _DIALOG_HANDLER_ATTR, True)
    log.info("bulk_note: registered browser dialog auto-accept handler")


# ---------------------------------------------------------------------------
# Login helpers (consistent with other automations in this package)
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
# Navigate to bulk_notes.php
# ---------------------------------------------------------------------------

async def _ensure_bulk_page_ready(
    page: Page,
    contract_id: str,
    username: Optional[str],
    password: Optional[str],
    timeout_ms: int,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> None:
    url = BULK_URL.format(contract_id=contract_id)
    log.info("bulk_note: navigate -> %s", url)
    await page.goto(url, wait_until="domcontentloaded")

    if await _page_looks_like_login(page):
        if not username or not password:
            raise RuntimeError("Login required but credentials not set in .env")
        log.info("bulk_note: session expired — re-logging in")
        if not await _login(page, username, password):
            raise RuntimeError("EasyBOP re-login failed")
        if after_relogin:
            await after_relogin()
        await page.goto(url, wait_until="domcontentloaded")

    if await _page_looks_like_login(page):
        raise RuntimeError("Still on login wall after re-login — check credentials")


# ---------------------------------------------------------------------------
# Wait for jqGrid rows
# ---------------------------------------------------------------------------

async def _wait_for_grid(page: Page, timeout_ms: int) -> None:
    log.info("bulk_note: waiting for jqGrid...")
    try:
        await page.wait_for_function(
            "() => {"
            "  const el = document.getElementById('load_list');"
            "  return !el || el.style.display === 'none' || !el.offsetParent;"
            "}",
            timeout=timeout_ms,
        )
    except Exception as e:
        log.warning("bulk_note: grid overlay timed out: %s", e)

    try:
        await page.wait_for_selector("tr.jqgrow", state="attached", timeout=timeout_ms)
        log.info("bulk_note: grid rows found")
    except Exception as e:
        raise RuntimeError(f"jqGrid rows never appeared within {timeout_ms}ms: {e}") from e

    await asyncio.sleep(1.0)


# ---------------------------------------------------------------------------
# JS: tick checkboxes for matching addresses  (same as suspend_automation)
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
                results[match] = true;
            }
        }
    });

    return results;
}
"""

_CHECK_CORF_JS = """
(corfs) => {
    function normCorf(v) {
        const raw = String(v ?? '').trim();
        if (!raw) return '';
        const t = raw.toLowerCase().replace(/\\s+/g, '');
        const slash = t.match(/^(\\d+)\\/(\\d+)$/);
        if (slash) return parseInt(slash[1], 10) + '/' + parseInt(slash[2], 10);
        const digits = t.replace(/\\D/g, '');
        if (digits.length >= 4) return String(parseInt(digits, 10));
        return t;
    }

    const results = {};
    corfs.forEach(c => { results[c] = false; });

    const cells = document.querySelectorAll('td[aria-describedby="list_client_order_reference"]');
    cells.forEach(cell => {
        const cellCorf = normCorf(cell.getAttribute('title') || cell.textContent || '');
        if (!cellCorf) return;

        const match = corfs.find(c => normCorf(c) === cellCorf);
        if (match) {
            const row = cell.closest('tr');
            if (!row) return;
            const cb = row.querySelector('input[type="checkbox"].cbox');
            if (cb && !cb.checked) {
                cb.click();
                results[match] = true;
            } else if (cb && cb.checked) {
                results[match] = true;
            }
        }
    });

    return results;
}
"""


async def _wait_for_note_save_settled(page: Page, timeout_ms: int) -> None:
    """Poll until jqGrid load overlay clears after Add Note."""
    deadline = time.monotonic() + (timeout_ms / 1000)
    poll_interval_s = 0.4
    poll_n = 0

    log.info("bulk_note: waiting for note save to settle (up to %ss)…", timeout_ms // 1000)

    while time.monotonic() < deadline:
        poll_n += 1
        grid_loading = await page.evaluate(
            """() => {
              const loadEl = document.getElementById('load_list');
              return !!(loadEl && loadEl.offsetParent !== null
                && loadEl.style.display !== 'none');
            }"""
        )
        if not grid_loading:
            log.info("bulk_note: grid idle after %d poll(s)", poll_n)
            return
        if poll_n == 1 or poll_n % 10 == 0:
            log.info("bulk_note: grid still loading…")
        await asyncio.sleep(poll_interval_s)

    log.warning("bulk_note: grid still loading after %sms — continuing", timeout_ms)


async def _dismiss_note_dialog(page: Page) -> None:
    """Close Add Note dialog — overlay often blocks the Close button after save."""
    try:
        await page.wait_for_function(
            """() => {
              const ov = document.querySelector('.ui-widget-overlay.ui-front');
              return !ov || ov.offsetParent === null;
            }""",
            timeout=12_000,
        )
    except Exception:
        pass

    if await page.locator(".ui-dialog:visible").count() == 0:
        log.info("bulk_note: dialog already dismissed")
        return

    close_btn = page.locator("#btn_dialog_cancel").or_(
        page.locator(".ui-dialog:visible button").filter(has_text="Close")
    )
    for label, force in (("normal", False), ("force", True)):
        try:
            if await close_btn.count() == 0:
                break
            await close_btn.first.click(force=force, timeout=3_000)
            log.info("bulk_note: clicked Close (%s)", label)
            break
        except Exception as e:
            if force:
                log.warning("bulk_note: could not click Close: %s", e)
                try:
                    await page.keyboard.press("Escape")
                    log.info("bulk_note: sent Escape to dismiss dialog")
                except Exception:
                    pass

    try:
        await page.wait_for_selector(".ui-dialog:visible", state="hidden", timeout=4_000)
        log.info("bulk_note: dialog dismissed")
    except Exception:
        log.warning("bulk_note: dialog still visible after dismiss attempt, proceeding anyway")


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

async def bulk_add_notes(
    page: Page,
    contract_id: str,
    addresses: Optional[List[str]] = None,
    corfs: Optional[List[str]] = None,
    mode: NoteMode = "no_asbestos",
    note_text: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout_ms: int = 30_000,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> dict:
    """
    Navigate to bulk_notes.php, tick checkboxes for the supplied CORFs (preferred)
    or addresses, click 'Add Notes to Selected Works', fill in the note dialog,
    wait for save, then close.

    Parameters
    ----------
    mode       : "no_asbestos"    → category 1716 ("No Asbestos with order")
                 "report_ordered" → category 1758 ("Asbestos Report Ordered")
    note_text  : Custom note body.  Defaults to mode-appropriate text if omitted.

    Returns
    -------
    dict with keys: total_requested, total_checked, checked, not_found, success, message
    """
    corf_list = [str(c).strip() for c in (corfs or []) if c and str(c).strip()]
    addr_list = [a.strip() for a in (addresses or []) if a and a.strip()]
    use_corf = bool(corf_list)
    match_keys = corf_list if use_corf else addr_list

    if not match_keys:
        return {
            "total_requested": 0,
            "total_checked": 0,
            "not_found": [],
            "checked": [],
            "success": False,
            "message": "No valid CORFs or addresses provided.",
        }

    # Resolve category
    if mode == "report_ordered":
        category_value = CATEGORY_REPORT_ORDERED
        category_label = "Asbestos Report Ordered"
        default_note   = "Asbestos report has been ordered — works to remain on hold until survey is received."
    else:
        category_value = CATEGORY_NO_ASBESTOS
        category_label = "No Asbestos with order"
        default_note   = "Asbestos report missing — no asbestos confirmed with order."

    note_body = note_text if note_text else default_note

    log.info(
        "bulk_note: mode=%s  category=%s (%s)  match_by=%s  count=%d",
        mode, category_value, category_label, "corf" if use_corf else "address", len(match_keys),
    )

    # Accept "Save changes?" after Add Note (must register before any clicks)
    _ensure_browser_dialog_auto_accept(page)

    # ── Step 1: navigate ──────────────────────────────────────────────────
    await _ensure_bulk_page_ready(page, contract_id, username, password, timeout_ms, after_relogin)

    # ── Step 2: wait for grid ─────────────────────────────────────────────
    await _wait_for_grid(page, timeout_ms)

    # ── Step 3: check checkboxes ──────────────────────────────────────────
    log.info("bulk_note: matching %d %s against grid rows", len(match_keys), "CORFs" if use_corf else "addresses")
    check_js = _CHECK_CORF_JS if use_corf else _CHECK_JS
    check_results: dict = await page.evaluate(check_js, match_keys)

    checked   = [k for k, ok in check_results.items() if ok]
    not_found = [k for k, ok in check_results.items() if not ok]

    log.info("bulk_note: checked=%d  not_found=%d", len(checked), len(not_found))
    for key in not_found:
        log.warning("bulk_note: %s not found in grid: %r", "CORF" if use_corf else "address", key)

    if not checked:
        return {
            "total_requested": len(match_keys),
            "total_checked": 0,
            "not_found": not_found,
            "checked": checked,
            "success": False,
            "message": f"None of the provided {'CORFs' if use_corf else 'addresses'} were found in the grid.",
        }

    await asyncio.sleep(0.5)

    # ── Step 4: click "Add Notes to Selected Works" ───────────────────────
    try:
        add_btn = page.locator("#btn_addnotes")
        await add_btn.wait_for(state="visible", timeout=10_000)
        await add_btn.click()
        log.info("bulk_note: clicked #btn_addnotes")
    except Exception as e:
        return {
            "total_requested": len(match_keys),
            "total_checked": len(checked),
            "not_found": not_found,
            "checked": checked,
            "success": False,
            "message": f"Could not click 'Add Notes to Selected Works' button: {e}",
        }

    # ── Step 5: handle the popup dialog ──────────────────────────────────
    try:
        # Wait for the dialog
        dialog = page.locator(".ui-dialog:visible, [role='dialog']:visible")
        await dialog.first.wait_for(state="visible", timeout=8_000)
        log.info("bulk_note: dialog appeared")

        # Select category
        cat_select = page.locator("select").filter(
            has=page.locator(f"option[value='{category_value}']")
        ).first
        await cat_select.wait_for(state="visible", timeout=5_000)
        await cat_select.select_option(value=category_value)
        log.info("bulk_note: selected category '%s' (value=%s)", category_label, category_value)

        # Fill note text
        notes_area = page.locator("#notes")
        await notes_area.wait_for(state="visible", timeout=5_000)
        await notes_area.fill(note_body)
        log.info("bulk_note: note text filled")

        # Save: green "Add Note" button (#btn_save_changes)
        save_note_btn = page.locator("#btn_save_changes")
        if await save_note_btn.count() == 0:
            save_note_btn = page.locator(
                ".ui-dialog:visible button.green, .ui-dialog:visible button"
            ).filter(has_text="Add Note")
        await save_note_btn.first.wait_for(state="visible", timeout=5_000)
        await save_note_btn.first.scroll_into_view_if_needed()
        await asyncio.sleep(0.2)
        try:
            await save_note_btn.first.click(timeout=5_000)
            log.info("bulk_note: clicked #btn_save_changes (Add Note)")
        except Exception:
            log.warning("bulk_note: normal click failed — retrying Add Note with force")
            await save_note_btn.first.click(force=True, timeout=5_000)
            log.info("bulk_note: force-clicked #btn_save_changes (Add Note)")

        await asyncio.sleep(0.5)
        await _wait_for_note_save_settled(page, timeout_ms=max(timeout_ms, 30_000))
        await _dismiss_note_dialog(page)

    except Exception as e:
        return {
            "total_requested": len(match_keys),
            "total_checked": len(checked),
            "not_found": not_found,
            "checked": checked,
            "success": False,
            "message": f"Dialog handling failed: {e}",
        }

    await asyncio.sleep(1.0)

    msg = (
        f"Bulk notes completed. "
        f"Added '{category_label}' note to {len(checked)}/{len(match_keys)} rows. "
        + (
            f"Not found on page: {len(not_found)} {'CORF(s)' if use_corf else 'address(es)'}."
            if not_found
            else "All rows found."
        )
    )
    log.info("bulk_note: %s", msg)

    return {
        "total_requested": len(match_keys),
        "total_checked": len(checked),
        "not_found": not_found,
        "checked": checked,
        "success": True,
        "message": msg,
    }
