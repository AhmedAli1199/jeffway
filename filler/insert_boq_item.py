"""
Insert a single BOQ line into EasyBOP manually.

Flow:
  1. Navigate to Works Instruction index for the contract.
  2. Search by Works Instruction Address → pick matching dropdown item.
  3. Navigate to the BOQ tab.
  4. Open "JW Rates" BOQ panel.
  5. Search for BOQ Item Ref → fill Quantity from request (Google Sheet).
  6. Click Continue (#btn_close) → Save changes and Set Status → ACE Agreed → OK confirm.

Used by POST /insert-boq-item (stage1_import_api.py).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.async_api import BrowserContext, Page

_here = Path(__file__).resolve().parent
_repo = _here.parent
_SCREENSHOTS_DIR = _here / "screenshots"
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

from config import settings  # noqa: E402
from upload_wi_fill import _page_looks_like_login, login, resolve_session_path  # noqa: E402

log = logging.getLogger("easybop.insert_boq_item")

WI_INDEX_URL = "https://easybop.co.uk/a_planned_works/z_works/index.php?contract_id={contract_id}"
LOGIN_URL = "https://easybop.co.uk/"


async def _save_step_screenshot(page: Page, label: str, boq_item_ref: str) -> str:
    """Save PNG under filler/screenshots/ for debugging insert-boq-item steps."""
    _SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_ref = re.sub(r"[^\w.-]+", "_", str(boq_item_ref).strip())[:40] or "ref"
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = _SCREENSHOTS_DIR / f"insert-boq-{label}-{safe_ref}-{ts}.png"
    try:
        cdp = await page.context.new_cdp_session(page)
        shot = await cdp.send("Page.captureScreenshot", {"format": "png", "fromSurface": True})
        path.write_bytes(base64.b64decode(shot["data"]))
    except Exception as exc:
        log.warning("CDP screenshot failed (%s), using page.screenshot", exc)
        await page.screenshot(path=str(path), full_page=True, timeout=10_000, animations="disabled")
    log.info("Screenshot saved: %s", path)
    return str(path.resolve())


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

async def _ensure_logged_in(page: Page, context: BrowserContext) -> None:
    if await _page_looks_like_login(page):
        log.info("Login screen detected — signing in")
        ok = await login(page, settings.username.strip(), settings.password.strip())
        if not ok:
            raise RuntimeError("EasyBOP login failed")
        sp = resolve_session_path()
        sp.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(sp))
        log.info("Session saved to %s", sp)


# ---------------------------------------------------------------------------
# Step helpers
# ---------------------------------------------------------------------------

async def _search_and_select_wi(
    page: Page,
    address: str,
    timeout_ms: int,
) -> str:
    """
    Type the address into #search_dwelling, wait for autocomplete dropdown,
    click the best-matching item.  Returns the full text of the item clicked.
    """
    search = page.locator("#search_dwelling")
    await search.wait_for(state="visible", timeout=timeout_ms)
    await search.click()
    await search.fill("")
    await search.type(address, delay=60)

    dropdown = page.locator("ul.ui-autocomplete .ui-menu-item-wrapper")
    log.info("Waiting for autocomplete dropdown…")
    await dropdown.first.wait_for(state="visible", timeout=timeout_ms)
    await page.wait_for_timeout(400)

    # Prefer item whose text contains the address (case-insensitive);
    # fall back to first item if none match.
    addr_lower = address.strip().lower()
    count = await dropdown.count()
    chosen_idx = 0
    for i in range(count):
        text = (await dropdown.nth(i).text_content() or "").strip().lower()
        if addr_lower in text:
            chosen_idx = i
            break

    item_text = (await dropdown.nth(chosen_idx).text_content() or "").strip()
    log.info("Selecting dropdown item [%d/%d]: %s", chosen_idx + 1, count, item_text[:80])
    await dropdown.nth(chosen_idx).click()

    # Wait for navigation to the work instruction detail page.
    await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    await page.wait_for_timeout(800)
    log.info("WI page loaded: %s", page.url)
    return item_text


async def _open_boq_tab(page: Page, timeout_ms: int) -> None:
    """Click the BOQ tab and wait for its content to activate."""
    boq_tab = page.locator("div[data-tab-nav-item='boq']")
    await boq_tab.wait_for(state="visible", timeout=timeout_ms)
    log.info("Clicking BOQ tab")
    await boq_tab.click()
    # Wait for BOQ buttons to appear
    await page.wait_for_selector("div.boq_button", state="visible", timeout=timeout_ms)
    await page.wait_for_timeout(600)


async def _open_jw_rates_panel(page: Page, timeout_ms: int) -> None:
    """Click the 'JW Rates' BOQ panel button and wait for the dialog."""
    jw_btn = page.locator("div.boq_button[data-boq-name='JW Rates']")
    await jw_btn.wait_for(state="visible", timeout=timeout_ms)
    log.info("Clicking JW Rates button")
    await jw_btn.click()

    # Wait for the search box inside the popup dialog
    boq_search = page.locator("#boq_search")
    log.info("Waiting for BOQ item search dialog…")
    await boq_search.wait_for(state="visible", timeout=timeout_ms)
    await page.wait_for_timeout(600)
    log.info("BOQ dialog open")


async def _find_and_fill_boq_item(
    page: Page,
    boq_item_ref: str,
    quantity: float,
    timeout_ms: int,
) -> dict:
    """
    Search for boq_item_ref inside the open dialog, fill quantity, return info dict.
    """
    boq_search = page.locator("#boq_search")
    await boq_search.fill("")
    await boq_search.type(str(boq_item_ref), delay=60)
    log.info("Typed BOQ ref '%s' into search", boq_item_ref)

    # Wait for table to filter — at least one visible row whose item_ref matches
    ref_str = str(boq_item_ref).strip()

    async def _wait_for_results():
        for _ in range(40):
            rows = page.locator("tr.search-result")
            count = await rows.count()
            for i in range(count):
                ref_cell = rows.nth(i).locator("td.item_ref")
                if await ref_cell.count() == 0:
                    continue
                cell_text = (await ref_cell.text_content() or "").strip()
                if cell_text == ref_str:
                    return i
            await page.wait_for_timeout(300)
        return None

    matched_idx = await _wait_for_results()
    if matched_idx is None:
        # Grab whatever is visible for debugging
        visible_refs = []
        rows = page.locator("tr.search-result")
        for i in range(min(5, await rows.count())):
            rc = rows.nth(i).locator("td.item_ref")
            if await rc.count():
                visible_refs.append((await rc.text_content() or "").strip())
        raise RuntimeError(
            f"BOQ Item Ref '{boq_item_ref}' not found in search results. "
            f"Visible refs: {visible_refs}"
        )

    matched_row = page.locator("tr.search-result").nth(matched_idx)
    item_name = (await matched_row.locator("td.element_name").text_content() or "").strip()
    description = (await matched_row.locator("td.description").text_content() or "").strip()
    log.info("Found row [%d]: name=%s desc=%s", matched_idx, item_name, description)

    qty_input = matched_row.locator("input.qty")
    await qty_input.wait_for(state="visible", timeout=timeout_ms)
    await qty_input.click()
    await qty_input.fill("")
    qty_text = str(int(quantity)) if float(quantity).is_integer() else str(quantity)
    await qty_input.type(qty_text, delay=30)
    await qty_input.press("Tab")
    await page.wait_for_timeout(400)
    qty_actual = (await qty_input.input_value() or "").strip()
    log.info("Filled quantity from sheet: %s (field now shows: %r)", qty_text, qty_actual)

    screenshot_after_qty = await _save_step_screenshot(page, "after-qty", ref_str)

    return {
        "item_name": item_name,
        "description": description,
        "boq_item_ref": ref_str,
        "quantity_entered": quantity,
        "quantity_in_field": qty_actual,
        "screenshot_after_qty": screenshot_after_qty,
    }


async def _continue_boq_dialog(page: Page, timeout_ms: int) -> None:
    """Click Continue in the JW Rates dialog (not the titlebar X)."""
    continue_btn = page.locator("#btn_close")
    await continue_btn.wait_for(state="visible", timeout=timeout_ms)
    await page.wait_for_function(
        """() => {
          const btn = document.getElementById('btn_close');
          return btn && !btn.disabled && !btn.classList.contains('ui-state-disabled');
        }""",
        timeout=timeout_ms,
    )
    log.info("Clicking Continue (#btn_close)")
    await continue_btn.click()
    await page.wait_for_selector("#boq_search", state="hidden", timeout=timeout_ms)
    await page.wait_for_timeout(500)
    log.info("BOQ dialog closed after Continue")


async def _save_and_set_ace_agreed(page: Page, timeout_ms: int, *, boq_item_ref: str) -> dict:
    """
    Save changes and Set Status → ACE Agreed → accept native 'Save Changes?' confirm.
    """
    dialog_info = {"appeared": False, "message": ""}

    async def _on_dialog(dialog) -> None:
        dialog_info["appeared"] = True
        dialog_info["message"] = dialog.message or ""
        log.info("Browser confirm dialog: %r — clicking OK", dialog_info["message"])
        await dialog.accept()

    page.on("dialog", _on_dialog)

    save_btn = page.locator("#btn_save_and_mark_as_agreed")
    await save_btn.wait_for(state="visible", timeout=timeout_ms)
    await page.wait_for_function(
        """() => {
          const btn = document.getElementById('btn_save_and_mark_as_agreed');
          return btn && !btn.disabled && !btn.classList.contains('ui-state-disabled');
        }""",
        timeout=timeout_ms,
    )
    log.info("Clicking Save changes and Set Status (#btn_save_and_mark_as_agreed)")
    await save_btn.click()

    ace_item = page.locator("li.context-menu-item").filter(
        has_text=re.compile(r"ACE\s+Agreed", re.I)
    )
    await ace_item.first.wait_for(state="visible", timeout=timeout_ms)
    log.info("Selecting ACE Agreed from context menu")
    await ace_item.first.click()

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not dialog_info["appeared"]:
        await page.wait_for_timeout(200)

    if not dialog_info["appeared"]:
        log.warning("No native confirm dialog after ACE Agreed")

    await page.wait_for_timeout(1500)
    screenshot_after_save = await _save_step_screenshot(page, "after-save-ok", boq_item_ref)

    return {
        "save_clicked": True,
        "ace_agreed_selected": True,
        "confirm_dialog_appeared": dialog_info["appeared"],
        "confirm_dialog_message": dialog_info["message"] or None,
        "screenshot_after_save_ok": screenshot_after_save,
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def insert_boq_item(
    page: Page,
    context: BrowserContext,
    *,
    works_instruction_address: str,
    boq_item_ref: str,
    quantity: float,
    contract_id: str = "321129",
    timeout_ms: Optional[int] = None,
) -> dict:
    """
    Full flow: open WI index → search address → BOQ tab → JW Rates panel →
    search ref → fill qty → Continue → Save changes → ACE Agreed → confirm OK.

    Returns result dict with success info.
    """
    t = timeout_ms or max(settings.request_timeout_ms, 30_000)
    url = WI_INDEX_URL.format(contract_id=contract_id)

    log.info(
        "insert_boq_item: address=%r ref=%r qty=%s contract=%s",
        works_instruction_address,
        boq_item_ref,
        quantity,
        contract_id,
    )

    await page.goto(url, wait_until="domcontentloaded", timeout=t)
    try:
        await page.wait_for_load_state("networkidle", timeout=min(20_000, t))
    except Exception:
        pass

    await _ensure_logged_in(page, context)

    if await _page_looks_like_login(page):
        raise RuntimeError("Still on login page after auth attempt")

    # Re-navigate after potential login redirect
    if "index.php" not in page.url:
        await page.goto(url, wait_until="domcontentloaded", timeout=t)
        await page.wait_for_timeout(800)

    dropdown_text = await _search_and_select_wi(page, works_instruction_address, t)
    await _open_boq_tab(page, t)
    await _open_jw_rates_panel(page, t)
    item_info = await _find_and_fill_boq_item(page, boq_item_ref, quantity, t)
    await _continue_boq_dialog(page, t)
    save_info = await _save_and_set_ace_agreed(page, t, boq_item_ref=boq_item_ref)

    return {
        "success": True,
        "works_instruction_address": works_instruction_address,
        "dropdown_selected": dropdown_text,
        "final_url": page.url,
        **item_info,
        **save_info,
        "message": "BOQ item inserted, quantity set, saved with ACE Agreed",
    }


# ---------------------------------------------------------------------------
# Standalone runner (headed, for manual testing)
# ---------------------------------------------------------------------------

async def _run_standalone(
    address: str,
    boq_item_ref: str,
    quantity: float,
    contract_id: str,
) -> None:
    from playwright.async_api import async_playwright

    sp = resolve_session_path()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            slow_mo=settings.slow_mo_ms or 80,
            args=["--window-size=1400,900"],
        )
        ctx_kwargs = {}
        if sp.exists():
            ctx_kwargs["storage_state"] = str(sp)
        context = await browser.new_context(**ctx_kwargs)
        page = await context.new_page()
        try:
            result = await insert_boq_item(
                page, context,
                works_instruction_address=address,
                boq_item_ref=boq_item_ref,
                quantity=quantity,
                contract_id=contract_id,
            )
            print("OK:", result)
            print("Browser open 60s for inspection…")
            await asyncio.sleep(60)
        except Exception as exc:
            print("FAILED:", exc)
            print("Browser open 120s for inspection…")
            await asyncio.sleep(120)
            raise
        finally:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    parser = argparse.ArgumentParser(description="Manually insert a BOQ item into EasyBOP")
    parser.add_argument("--address", required=True, help="Works Instruction Address")
    parser.add_argument("--ref", required=True, help="BOQ Item Ref. (e.g. 240251)")
    parser.add_argument("--qty", type=float, default=1.0, help="Quantity")
    parser.add_argument("--contract", default="321129", help="EasyBOP contract_id")
    args = parser.parse_args()

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(_run_standalone(args.address, args.ref, args.qty, args.contract))
