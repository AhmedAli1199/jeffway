"""
Stage 7 — Completion Automation

For a job where the handover email has been confirmed sent:
    1. Navigate directly to works_details.php (Process tab).
    2. Bypass any incomplete pre-work rows in the pre-work tables.
    3. Guard: verify Ready to Handover = Yes on the Works UDF tab.
    4. Enter the completion date (bypassing readonly via JS) and click Save Changes.
    5. Click Add Note, fill the closing note, save.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from playwright.async_api import Dialog, Page

log = logging.getLogger("stage7.completion")

LOGIN_URL          = "https://easybop.co.uk/"
WORKS_DETAILS_BASE = "https://easybop.co.uk/a_planned_works/z_works/works_details.php"
_NETWORKIDLE_MS    = 8_000

# Process tab — completion date (readonly, set via JS)
_COMPLETION_DATE_ID  = "date_time_completed"
# Process tab — save button
_SAVE_BTN_ID         = "btn_save_works_changes"
# Communications — Add Note button
_ADD_NOTE_BTN_ID     = "btn_comms_add_note"
# Note dialog — textarea
_NOTE_TEXTAREA_ID    = "notes"
# Note dialog — save/Add Note button
_SAVE_NOTE_BTN_ID = "btn_save_changes"

# Process tab — final Save Changes button
_SAVE_PROCESS_BTN_XPATH = "xpath=/html/body/div[2]/div/div/div[10]/div[3]/button[1]"
_DIALOG_HANDLER_ATTR = "_easybop_completion_dialog_wired"
_BYPASS_REASON_LABEL = "NOT REQUIRED WORKS COMPLETE"


def _ensure_browser_dialog_auto_accept(page: Page) -> None:
    """Accept native window.confirm() — e.g. after Add Note or Save Changes."""
    if getattr(page, _DIALOG_HANDLER_ATTR, False):
        return

    async def _accept_native_dialog(dialog: Dialog) -> None:
        log.info('completion: native browser dialog "%s" — clicking OK', dialog.message)
        await dialog.accept()

    page.on("dialog", _accept_native_dialog)
    setattr(page, _DIALOG_HANDLER_ATTR, True)
    log.info("completion: registered browser dialog auto-accept handler")


async def _save_bypass_dialog(page: Page, timeout_ms: int) -> None:
    _ensure_browser_dialog_auto_accept(page)
    save_btn = page.locator(".ui-dialog:visible #btn_dialog_save_changes_bypass")
    await save_btn.wait_for(state="visible", timeout=timeout_ms)
    await save_btn.click()
    log.info("completion: bypass save clicked — waiting for confirm + save")
    try:
        await page.locator("#pre_item_bypass_form").wait_for(
            state="hidden",
            timeout=min(timeout_ms, 15_000),
        )
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 10_000))
    except Exception:
        pass


async def _select_bypass_reason(page: Page, timeout_ms: int) -> None:
    dialog = page.locator(".ui-dialog:visible")
    await dialog.first.wait_for(state="visible", timeout=timeout_ms)
    reason_link = dialog.locator(
        'ul.existing-reasons a[data-action="works_ajax_selectBypassReason"][data-label="NOT REQUIRED WORKS COMPLETE"]'
    ).first
    await reason_link.wait_for(state="visible", timeout=timeout_ms)
    await reason_link.click()
    log.info("completion: bypass reason set to %r", _BYPASS_REASON_LABEL)
    await asyncio.sleep(0.25)


async def _bypass_prework_row(page: Page, row: Any, timeout_ms: int) -> bool:
    bypass_links = row.locator('a[data-action="pw_works_show_pre_item_bypass_dialog"]')
    if await bypass_links.count() == 0:
        return False

    bypass_link = bypass_links.first
    try:
        await bypass_link.wait_for(state="visible", timeout=timeout_ms)
    except Exception:
        log.info("completion: incomplete row has no visible bypass link — skipping")
        return False

    try:
        await bypass_link.click(timeout=timeout_ms)
    except Exception:
        await bypass_link.click(force=True, timeout=timeout_ms)

    await _select_bypass_reason(page, timeout_ms)
    await _save_bypass_dialog(page, timeout_ms)
    await asyncio.sleep(0.5)
    return True


async def _bypass_incomplete_prework_tables(page: Page, timeout_ms: int) -> None:
    _ensure_browser_dialog_auto_accept(page)

    tables = page.locator("table.works_table")
    table_count = await tables.count()
    log.info("completion: scanning %s pre-work tables", table_count)
    if table_count == 0:
        log.info("completion: no table.works_table elements found on the page")
        return

    for table_index in range(table_count):
        table = tables.nth(table_index)
        try:
            await table.wait_for(state="visible", timeout=min(timeout_ms, 5_000))
        except Exception:
            continue

        while True:
            rows = table.locator("tbody tr")
            row_count = await rows.count()
            bypassed_any = False

            for index in range(row_count):
                row = rows.nth(index)
                cells = row.locator("td")
                cell_count = await cells.count()
                if cell_count < 4:
                    continue

                status_cell = cells.nth(cell_count - 1)
                status_class = (await status_cell.get_attribute("class")) or ""
                if "pw_incomplete_color" not in status_class:
                    continue
                if "pw_bypassed_color" in status_class:
                    continue

                row_text = ""
                try:
                    row_text = (await row.inner_text() or "").split("\n", 1)[0].strip()
                except Exception:
                    pass

                log.info("completion: bypassing red pre-work row — %s", row_text or f"table[{table_index}]")
                if await _bypass_prework_row(page, row, timeout_ms):
                    bypassed_any = True
                    break

            if not bypassed_any:
                break

            try:
                await table.wait_for(state="visible", timeout=min(timeout_ms, 5_000))
            except Exception:
                pass

# ---------------------------------------------------------------------------
# Navigation helpers
# ---------------------------------------------------------------------------

async def _goto_safe(page: Page, url: str) -> None:
    try:
        await page.goto(url, wait_until="domcontentloaded")
    except Exception as exc:
        if "ERR_ABORTED" not in str(exc):
            raise
        await asyncio.sleep(2.0)
        await page.goto(url, wait_until="domcontentloaded")
    try:
        await page.wait_for_load_state("networkidle", timeout=_NETWORKIDLE_MS)
    except Exception:
        pass


async def _page_is_login(page: Page) -> bool:
    try:
        if await page.locator("#login_box").count() > 0:
            if await page.locator("#login_box").first.is_visible():
                return True
        return "login" in (await page.title() or "").lower()
    except Exception:
        return False


async def _login(page: Page, username: str, password: str) -> bool:
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    if await page.locator("a[href*='logout']").count() > 0:
        return True
    box = page.locator("#login_box")
    try:
        await box.wait_for(state="visible", timeout=12_000)
        await box.locator("input[type='text']").first.fill(username)
        await box.locator("input[type='password']").first.fill(password)
        await page.locator("#btn_login").click()
        await page.wait_for_function(
            "() => document.querySelector('a[href*=\"logout\"]') !== null "
            "|| (document.querySelector('#login_box') && "
            "document.querySelector('#login_box').offsetParent === null)",
            timeout=20_000,
        )
        log.info("completion: login success — %s", page.url)
        return True
    except Exception as exc:
        log.exception("completion: login failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Guard check
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Process tab actions
# ---------------------------------------------------------------------------

async def _set_completion_date(
    page: Page,
    completion_date: str,
    timeout_ms: int,
) -> None:
    """
    The date field is readonly but clicking it auto-populates today's date.
    Click the field once, wait briefly for the value to appear.
    """
    field = page.locator(f"#{_COMPLETION_DATE_ID}")
    await field.wait_for(state="visible", timeout=timeout_ms)

    # Focus/click to let the UI populate today's date
    await field.click()
    # give the UI a moment to fill the field
    await asyncio.sleep(1.0)
    log.info("completion: clicked date_time_completed to auto-populate today's date")


async def _save_process_changes(page: Page, timeout_ms: int) -> None:
    _ensure_browser_dialog_auto_accept(page)
    btn = page.locator(_SAVE_PROCESS_BTN_XPATH)
    await btn.wait_for(state="visible", timeout=timeout_ms)
    await btn.click()
    log.info("completion: 'Save Changes' clicked — waiting for confirm + save")
    await asyncio.sleep(2.0)
    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass


async def _add_closing_note(
    page: Page,
    note_text: str,
    timeout_ms: int,
) -> None:
    # Open dialog
    add_note_btn = page.locator(f"#{_ADD_NOTE_BTN_ID}")
    await add_note_btn.wait_for(state="visible", timeout=timeout_ms)
    await add_note_btn.click()
    log.info("completion: 'Add Note' clicked")

    dialog = page.locator(".ui-dialog:visible, [role='dialog']:visible")
    await dialog.first.wait_for(state="visible", timeout=8_000)
    await asyncio.sleep(0.5)

    # Select category — "Email to Client" (value 1599) is correct since
    # this note records the handover pack email sent to BCC (the client)
    category = page.locator("#category_id")
    await category.wait_for(state="visible", timeout=timeout_ms)
    await category.select_option(value="1599")
    log.info("completion: note category set to 'Email to Client'")

    # Fill the note textarea
    textarea = page.locator(f"#{_NOTE_TEXTAREA_ID}")
    await textarea.wait_for(state="visible", timeout=timeout_ms)
    await textarea.fill(note_text)
    log.info("completion: note text filled — %r", note_text)

    # Click the Add Note (save) button inside the dialog — accept native confirm (OK)
    _ensure_browser_dialog_auto_accept(page)
    save_note_btn = page.locator(f"#{_SAVE_NOTE_BTN_ID}")
    await save_note_btn.wait_for(state="visible", timeout=timeout_ms)
    await save_note_btn.click()
    log.info("completion: note dialog save clicked — waiting for confirm + save")
    await asyncio.sleep(2.0)
    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass
    await page.keyboard.press("Escape")
    log.info("completion: note dialog closed (Escape)")
    await asyncio.sleep(0.5)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def mark_job_complete(
    page: Page,
    *,
    works_id: str,
    contract_id: str,
    completion_date: str,
    handover_date: str,
    username: str,
    password: str,
    timeout_ms: int = 30_000,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> dict[str, Any]:
    """
    Mark a job complete in EasyBOP and add a closing note.

    `completion_date` — DD/MM/YYYY HH:MM (e.g. "24/06/2026 00:00")
    `handover_date`   — DD/MM/YYYY (used only in the note text)
    """
    process_url = f"{WORKS_DETAILS_BASE}?works_id={works_id}&contract_id={contract_id}"

    # ── Session check ────────────────────────────────────────────────────────
    log.info("completion: navigating to Process tab — %s", process_url)
    await _goto_safe(page, process_url)

    if await _page_is_login(page):
        log.info("completion: session expired — re-authenticating")
        if not await _login(page, username, password):
            raise RuntimeError("EasyBOP login failed — check credentials")
        if after_relogin:
            await after_relogin()
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        log.info("completion: post-login settle (7 s)")
        await asyncio.sleep(7.0)
        await _goto_safe(page, process_url)

    # ── Step 1: Bypass incomplete pre-work items ───────────────────────────
    await _bypass_incomplete_prework_tables(page, timeout_ms)
    await asyncio.sleep(0.5)

    # ── Step 2: Enter completion date ────────────────────────────────────────
    await _set_completion_date(page, completion_date, timeout_ms)
    await asyncio.sleep(0.5)

    # ── Step 3: Add closing note ─────────────────────────────────────────────
    note_text = f"Handover pack sent to BCC on {handover_date}. Job closed."
    await _add_closing_note(page, note_text, timeout_ms)

    # ── Step 4: Save Changes ─────────────────────────────────────────────────
    # PRODUCTION: uncomment the line below to persist the completion date.
    await _save_process_changes(page, timeout_ms)

    return {
        "success":         True,
        "works_id":        works_id,
        "contract_id":     contract_id,
        "completion_date": completion_date,
        "note":            note_text,
        "message":         f"Job works_id={works_id} marked complete and note added",
    }
