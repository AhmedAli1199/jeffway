"""
Stage 7 — Works UDF Automation

Updates the handover date field on the Works UDF tab for a given job.
Called by the n8n polling workflow when it detects the handover email
has been sent from the BCCorders inbox.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from playwright.async_api import Dialog, Page

log = logging.getLogger("stage7.udf")

LOGIN_URL          = "https://easybop.co.uk/"
WORKS_DETAILS_BASE = "https://easybop.co.uk/a_planned_works/z_works/works_details.php"
_NETWORKIDLE_MS    = 8_000

_HANDOVER_DATE_ID   = "udf_pw_works_5069"
_SAVE_BTN_ID        = "btn_save_works_udfs"
_DIALOG_HANDLER_ATTR = "_easybop_udf_dialog_wired"


def _ensure_browser_dialog_auto_accept(page: Page) -> None:
    """Accept native window.confirm() — e.g. 'Save Works UDF Data?' after save button."""
    if getattr(page, _DIALOG_HANDLER_ATTR, False):
        return

    async def _accept_native_dialog(dialog: Dialog) -> None:
        log.info('udf: native browser dialog "%s" — clicking OK', dialog.message)
        await dialog.accept()

    page.on("dialog", _accept_native_dialog)
    setattr(page, _DIALOG_HANDLER_ATTR, True)
    log.info("udf: registered browser dialog auto-accept handler")


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
        log.info("udf: login success — %s", page.url)
        return True
    except Exception as exc:
        log.exception("udf: login failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def set_handover_date(
    page: Page,
    *,
    works_id: str,
    contract_id: str,
    handover_date: str,
    username: str,
    password: str,
    timeout_ms: int = 30_000,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> dict[str, Any]:
    """
    Navigate to the Works UDF tab, fill the handover date, and save.

    `handover_date` must be DD/MM/YYYY (e.g. '23/06/2026').
    """
    udf_url = (
        f"{WORKS_DETAILS_BASE}"
        f"?works_id={works_id}&contract_id={contract_id}&tab_nav_item=works_udf"
    )
    log.info("udf: navigating to Works UDF tab — %s", udf_url)
    await _goto_safe(page, udf_url)

    # Handle session expiry
    if await _page_is_login(page):
        log.info("udf: session expired — re-authenticating")
        if not await _login(page, username, password):
            raise RuntimeError("EasyBOP login failed — check credentials")
        if after_relogin:
            await after_relogin()
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        log.info("udf: post-login settle (7 s)")
        await asyncio.sleep(7.0)
        await _goto_safe(page, udf_url)

    # Fill handover date
    date_field = page.locator(f"#{_HANDOVER_DATE_ID}")
    await date_field.wait_for(state="visible", timeout=timeout_ms)
    await date_field.click(click_count=3)   # select any existing value
    await date_field.fill(handover_date)
    await date_field.press("Tab")     # trigger datepicker onchange
    log.info("udf: handover date set to %r", handover_date)

    await asyncio.sleep(0.5)

    # Save — native confirm "Save Works UDF Data?" is auto-accepted via page.on("dialog")
    _ensure_browser_dialog_auto_accept(page)
    save_btn = page.locator(f"#{_SAVE_BTN_ID}")
    await save_btn.wait_for(state="visible", timeout=timeout_ms)
    await save_btn.click()
    log.info("udf: 'Save Works UDF Data' clicked — waiting for confirm + save")
    await asyncio.sleep(2.0)
    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass

    return {
        "success":        True,
        "works_id":       works_id,
        "contract_id":    contract_id,
        "handover_date":  handover_date,
        "message": f"Handover date set to {handover_date} for works_id={works_id}",
    }
