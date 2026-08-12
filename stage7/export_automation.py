"""
Stage 7 — Export Automation

For a given job (searched by address):
  1. Navigate to works_details.php (Process tab — default on load).
  2. Click "Export Document Pack", tick the completion forms checkbox,
     download the ZIP, close the dialog.
  3. Navigate to BOQ tab, open View Summary, export the Excel file.
  4. Return both files as base64 strings so n8n can attach them to a
     Microsoft Graph API draft email.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import tempfile
from typing import Any, Awaitable, Callable, Optional

from playwright.async_api import Page

log = logging.getLogger("stage7.export")

LOGIN_URL          = "https://easybop.co.uk/"
JOBS_INDEX_URL     = "https://easybop.co.uk/a_planned_works/contracts/index.php"
WORKS_DETAILS_BASE = "https://easybop.co.uk/a_planned_works/z_works/works_details.php"

_RE_WORKS_DETAILS = re.compile(r"works_details\.php", re.I)
_NETWORKIDLE_MS   = 8_000

# Checkbox that selects "Post Completion" / operative completion forms in the export pack.
# The value 13356 is the document template ID — consistent across all jobs.
_COMPLETION_CHECKBOX_ID = "item-post_completion-13356"
_COMPLETION_CHECKBOX_XPATH = (
    "xpath=/html/body/div[16]/div[2]/div/div/div/div[2]/div/ul/li[3]/div[2]/div[13]/div/input"
)

# Export Document Pack button
_BTN_EXPORT_PACK_XPATH = "xpath=/html/body/div[2]/div/div/div[10]/div[3]/button[6]"

# Download Document Pack Files button (inside dialog)
_BTN_DOWNLOAD_PACK_XPATH = "xpath=/html/body/div[16]/div[3]/div/button[5]"

# Close dialog button
_BTN_CLOSE_DIALOG_XPATH = "xpath=/html/body/div[16]/div[3]/div/button[6]"

# BOQ tab: View Summary button
_BTN_VIEW_SUMMARY_XPATH = "xpath=/html/body/div[2]/div/div/div[10]/div[3]/form/div/button[2]"

# BOQ summary dialog: Export button
_BTN_EXPORT_EXCEL_XPATH = "xpath=/html/body/div[14]/div[3]/div/button[2]"


# ---------------------------------------------------------------------------
# Navigation helpers (same pattern as rest of Stage 7)
# ---------------------------------------------------------------------------

def _is_target_closed(exc: Exception) -> bool:
    msg = str(exc).lower()
    return (
        "target closed" in msg
        or "target page" in msg
        or "browser has been closed" in msg
        or "context or browser has been closed" in msg
    )


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
        log.info("export: login success — %s", page.url)
        return True
    except Exception as exc:
        log.exception("export: login failed: %s", exc)
        return False


async def _ensure_jobs_index(
    page: Page,
    username: str,
    password: str,
    after_relogin: Optional[Callable[[], Awaitable[None]]],
) -> None:
    await _goto_safe(page, JOBS_INDEX_URL)
    log.info("export: landed — url=%s  title=%r", page.url, await page.title())

    if await _page_is_login(page):
        log.info("export: session expired — re-authenticating")
        if not await _login(page, username, password):
            raise RuntimeError("EasyBOP login failed — check credentials")
        if after_relogin:
            await after_relogin()
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        log.info("export: post-login settle (7 s)")
        await asyncio.sleep(7.0)
        await _goto_safe(page, JOBS_INDEX_URL)

    if await page.locator("#quick_search_address").count() == 0:
        raise RuntimeError(
            f"#quick_search_address not found — current page: {page.url!r}"
        )


async def _navigate_to_job(
    page: Page,
    address: str,
    timeout_ms: int,
) -> dict[str, str]:
    """Search by address, click first active row, wait for works_details.php."""
    log.info("export: searching for address %r", address)
    search = page.locator("#quick_search_address")
    await search.wait_for(state="visible", timeout=10_000)
    await search.click()
    await search.fill("")
    await search.type(address, delay=60)
    await page.wait_for_timeout(1_200)

    active_row = page.locator("table.nice_table.clickable tr.wi_row")
    try:
        await active_row.first.wait_for(state="visible", timeout=timeout_ms)
    except Exception as exc:
        if _is_target_closed(exc):
            raise RuntimeError(
                f"Page closed while searching for {address!r}"
            ) from exc
        raise RuntimeError(
            f"No active job rows found for address {address!r}: {exc}"
        ) from exc

    first_row   = active_row.first
    works_id    = (await first_row.get_attribute("data-works_id")    or "").strip()
    contract_id = (await first_row.get_attribute("data-contract_id") or "").strip()
    log.info("export: row found — works_id=%s contract_id=%s", works_id, contract_id)
    await first_row.click()

    try:
        await page.wait_for_url(_RE_WORKS_DETAILS, timeout=timeout_ms)
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except Exception as exc:
        raise RuntimeError(
            f"works_details.php did not load for {address!r}: {exc}"
        ) from exc

    await asyncio.sleep(1.0)
    return {"works_id": works_id, "contract_id": contract_id}


# ---------------------------------------------------------------------------
# Download helper
# ---------------------------------------------------------------------------

async def _capture_download(page: Page, trigger_fn) -> tuple[str, str]:
    """
    Trigger a browser download via trigger_fn, wait for it to complete,
    read the file and return (suggested_filename, base64_content).
    """
    tmp_path = None
    try:
        async with page.expect_download(timeout=60_000) as dl_info:
            await trigger_fn()
        download = await dl_info.value
        filename = download.suggested_filename or "download"
        log.info("export: download received — %r", filename)

        # Save to a temp file so we can read the bytes
        suffix = os.path.splitext(filename)[1] or ""
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        os.close(tmp_fd)

        await download.save_as(tmp_path)

        with open(tmp_path, "rb") as fh:
            content_b64 = base64.b64encode(fh.read()).decode("utf-8")

        log.info("export: encoded %r — base64 length=%d", filename, len(content_b64))
        return filename, content_b64

    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Export Document Pack (ZIP)
# ---------------------------------------------------------------------------

async def _export_document_pack(
    page: Page,
    timeout_ms: int,
) -> tuple[str, str]:
    """
    Click Export Document Pack, tick the completion forms checkbox,
    download the ZIP, close the dialog.
    Returns (filename, base64_content).
    """
    # Click Export Document Pack button
    btn = page.locator(_BTN_EXPORT_PACK_XPATH)
    await btn.wait_for(state="visible", timeout=timeout_ms)
    await btn.click()
    log.info("export: clicked 'Export Document Pack'")
    await asyncio.sleep(2.0)

    # Tick the completion forms checkbox
    # Try by ID first (more robust), fall back to absolute XPath
    checkbox = page.locator(f"#{_COMPLETION_CHECKBOX_ID}")
    if await checkbox.count() == 0:
        checkbox = page.locator(_COMPLETION_CHECKBOX_XPATH)
    await checkbox.wait_for(state="visible", timeout=timeout_ms)
    if not await checkbox.is_checked():
        await checkbox.click()
    log.info("export: completion forms checkbox ticked")

    # Download the ZIP
    download_btn = page.locator(_BTN_DOWNLOAD_PACK_XPATH)
    await download_btn.wait_for(state="visible", timeout=timeout_ms)

    filename, b64 = await _capture_download(
        page,
        trigger_fn=lambda: download_btn.click(),
    )

    # Close the dialog
    close_btn = page.locator(_BTN_CLOSE_DIALOG_XPATH)
    try:
        await close_btn.wait_for(state="visible", timeout=10_000)
        await close_btn.click()
        log.info("export: document pack dialog closed")
    except Exception:
        log.warning("export: close button not found — pressing Escape")
        await page.keyboard.press("Escape")

    await asyncio.sleep(1.0)
    return filename, b64


# ---------------------------------------------------------------------------
# BOQ Export (Excel)
# ---------------------------------------------------------------------------

async def _export_boq_excel(
    page: Page,
    works_id: str,
    contract_id: str,
    timeout_ms: int,
) -> tuple[str, str]:
    """
    Navigate to the BOQ tab, open View Summary, export the Excel file.
    Returns (filename, base64_content).
    """
    boq_url = (
        f"{WORKS_DETAILS_BASE}"
        f"?works_id={works_id}&contract_id={contract_id}&tab_nav_item=boq"
    )
    log.info("export: navigating to BOQ tab — %s", boq_url)
    await _goto_safe(page, boq_url)

    # Click View Summary
    view_summary_btn = page.locator(_BTN_VIEW_SUMMARY_XPATH)
    await view_summary_btn.wait_for(state="visible", timeout=timeout_ms)
    await view_summary_btn.click()
    log.info("export: clicked 'View Summary'")
    await asyncio.sleep(1.5)

    # Click Export (downloads Excel)
    export_btn = page.locator(_BTN_EXPORT_EXCEL_XPATH)
    await export_btn.wait_for(state="visible", timeout=timeout_ms)

    filename, b64 = await _capture_download(
        page,
        trigger_fn=lambda: export_btn.click(),
    )

    log.info("export: BOQ Excel exported — %r", filename)
    return filename, b64


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def export_job_documents(
    page: Page,
    *,
    address: str,
    username: str,
    password: str,
    timeout_ms: int = 30_000,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> dict[str, Any]:
    """
    Navigate to the job, download the Document Pack ZIP and the BOQ Excel,
    and return both as base64-encoded strings.

    Response shape:
      {
        success: bool,
        works_id: str,
        contract_id: str,
        address: str,
        zip_filename: str,
        zip_base64: str,       ← full base64 of the ZIP file
        excel_filename: str,
        excel_base64: str,     ← full base64 of the Excel file
        message: str,
      }
    """
    log.info("export: starting for address=%r", address)

    await _ensure_jobs_index(page, username, password, after_relogin)
    ids = await _navigate_to_job(page, address, timeout_ms)
    works_id    = ids["works_id"]
    contract_id = ids["contract_id"]

    # 1 — Document Pack ZIP
    zip_filename, zip_b64 = await _export_document_pack(page, timeout_ms)

    # 2 — BOQ Excel
    excel_filename, excel_b64 = await _export_boq_excel(
        page, works_id, contract_id, timeout_ms
    )

    return {
        "success":        True,
        "works_id":       works_id,
        "contract_id":    contract_id,
        "address":        address,
        "zip_filename":   zip_filename,
        "zip_base64":     zip_b64,
        "excel_filename": excel_filename,
        "excel_base64":   excel_b64,
        "message": (
            f"Exported document pack ({zip_filename}) and "
            f"BOQ summary ({excel_filename}) for works_id={works_id}"
        ),
    }
