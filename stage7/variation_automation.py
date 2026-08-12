"""
Stage 7 — Variation Automation  (Phase 1)

Navigate from the EasyBOP jobs index to the Add New Variation dialog.

Flow:
  1. Go to https://easybop.co.uk/a_planned_works/contracts/index.php
  2. Wait for networkidle so all JS redirects/session checks settle.
  3. If redirected to login: authenticate, re-navigate.
  4. Type address into #quick_search_address, wait for results table.
  5. Skip disabled rows (tr.disabled); click first active row (tr.wi_row).
     Read data-works_id and data-contract_id from the row before clicking.
  6. Wait for works_details.php to load.
  7. Navigate directly to Variations tab via URL (&tab_nav_item=variations).
  8. Click #btn_add_new_variation.
  9. Wait 3 seconds — Phase 1 checkpoint.

Session states handled gracefully:
  - Valid cached session  -> land on contracts index, search immediately.
  - Expired session       -> redirected to login, re-authenticate, retry.
  - Post-login redirect   -> wait for networkidle before re-navigating.
  - Page closes mid-wait  -> TargetClosedError caught, reported clearly.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Awaitable, Callable, Optional

from playwright.async_api import Page


def _is_target_closed(exc: Exception) -> bool:
    """Return True for any Playwright 'target/page/browser closed' error."""
    msg = str(exc).lower()
    return (
        "target closed" in msg
        or "target page" in msg
        or "browser has been closed" in msg
        or "context or browser has been closed" in msg
    )

log = logging.getLogger("stage7.variation")

JOBS_INDEX_URL     = "https://easybop.co.uk/a_planned_works/contracts/index.php"
LOGIN_URL          = "https://easybop.co.uk/"
WORKS_DETAILS_BASE = "https://easybop.co.uk/a_planned_works/z_works/works_details.php"

_RE_WORKS_DETAILS = re.compile(r"works_details\.php", re.I)

_NETWORKIDLE_MS = 8_000   # max wait for network to go idle after navigation


# ---------------------------------------------------------------------------
# Navigation helpers
# ---------------------------------------------------------------------------

async def _goto_safe(page: Page, url: str) -> None:
    """
    Navigate to url, retry once on ERR_ABORTED, then wait for networkidle.
    networkidle ensures any JS redirects / session checks have fully settled
    before the caller touches any DOM elements.
    """
    try:
        await page.goto(url, wait_until="domcontentloaded")
    except Exception as exc:
        if "ERR_ABORTED" not in str(exc):
            raise
        log.warning("variation: ERR_ABORTED on goto %s — waiting 2s then retrying", url)
        await asyncio.sleep(2.0)
        await page.goto(url, wait_until="domcontentloaded")

    # Let all in-flight network requests (including JS-initiated redirects) settle.
    try:
        await page.wait_for_load_state("networkidle", timeout=_NETWORKIDLE_MS)
    except Exception:
        # Timeout is OK — networkidle can be slow on pages with websockets/polling.
        pass


# ---------------------------------------------------------------------------
# Login helpers
# ---------------------------------------------------------------------------

async def _page_is_login(page: Page) -> bool:
    try:
        if await page.locator("#login_box").count() > 0:
            if await page.locator("#login_box").first.is_visible():
                return True
        return "login" in (await page.title() or "").lower()
    except Exception:
        return False


async def _login(page: Page, username: str, password: str) -> bool:
    log.info("login: navigating to %s", LOGIN_URL)
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")

    # Already authenticated — logout link present
    if await page.locator("a[href*='logout']").count() > 0:
        log.info("login: already authenticated (logout link found)")
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
        log.info("login: success — url=%s", page.url)
        return True
    except Exception as exc:
        log.exception("login: failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Ensure we are on the jobs index, handling all session states
# ---------------------------------------------------------------------------

async def _ensure_jobs_index(
    page: Page,
    username: str,
    password: str,
    after_relogin: Optional[Callable[[], Awaitable[None]]],
) -> None:
    """
    Navigate to contracts/index.php and handle every auth state:

      1. Session valid   -- land directly on the search page, done.
      2. Session expired -- redirected to login, authenticate, re-navigate.
      3. Post-login      -- wait for networkidle before returning.

    After this returns the page is on contracts/index.php with
    #quick_search_address present, or a RuntimeError is raised.
    """
    await _goto_safe(page, JOBS_INDEX_URL)
    log.info(
        "variation: landed after JOBS_INDEX goto — url=%s  title=%r",
        page.url, await page.title(),
    )

    # Expired session: redirected to login wall
    if await _page_is_login(page):
        log.info("variation: session expired — re-authenticating")
        if not await _login(page, username, password):
            raise RuntimeError("EasyBOP login failed — check credentials in .env")
        if after_relogin:
            await after_relogin()
        # Dynamic: let any post-login redirects/XHRs finish first
        log.info("variation: waiting for post-login network to settle...")
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        # Fixed: EasyBOP needs ~7 s for server-side session to fully establish
        log.info("variation: post-login settle (7 s)")
        await asyncio.sleep(7.0)
        await _goto_safe(page, JOBS_INDEX_URL)
        log.info("variation: post-login nav — url=%s", page.url)
        if await _page_is_login(page):
            raise RuntimeError(
                "Still on login page after re-authentication. "
                "Login may have failed silently — check server logs."
            )

    # Verify the search field is actually present
    qsa_count = await page.locator("#quick_search_address").count()
    log.info(
        "variation: #quick_search_address count=%d  url=%s",
        qsa_count, page.url,
    )
    if qsa_count == 0:
        raise RuntimeError(
            f"Search field #quick_search_address not found after navigating to "
            f"contracts index. Current page: {page.url!r}. "
            "Check whether contracts/index.php redirected elsewhere."
        )


# ---------------------------------------------------------------------------
# Search and click the first active job row
# ---------------------------------------------------------------------------

async def _search_and_click_job_row(
    page: Page,
    address: str,
    timeout_ms: int,
) -> dict[str, str]:
    """
    Type address into #quick_search_address (already confirmed present).
    Wait for results table, skip disabled rows (tr.disabled),
    click the first active row (tr.wi_row).
    Reads data-works_id and data-contract_id BEFORE clicking.
    """
    log.info("variation: typing address %r into #quick_search_address", address)

    try:
        search = page.locator("#quick_search_address")
        await search.wait_for(state="visible", timeout=10_000)
        await search.click()
        await search.fill("")
        await search.type(address, delay=60)

        # Give the live search time to run and render results
        await page.wait_for_timeout(1_200)

        active_row = page.locator("table.nice_table.clickable tr.wi_row")
        try:
            await active_row.first.wait_for(state="visible", timeout=timeout_ms)
        except Exception as exc:
            if _is_target_closed(exc):
                raise  # re-raise so the outer except catches it for a clean message
            raise RuntimeError(
                f"No active job rows found for address {address!r}. "
                f"The job may be disabled/archived, or the address doesn't match. "
                f"Current url: {page.url!r} — inner error: {exc}"
            ) from exc

        first_row   = active_row.first
        works_id    = (await first_row.get_attribute("data-works_id")   or "").strip()
        contract_id = (await first_row.get_attribute("data-contract_id") or "").strip()

        if not works_id:
            raise RuntimeError(
                f"Could not read data-works_id from the first active row for {address!r}"
            )

        log.info(
            "variation: found active row — works_id=%s contract_id=%s — clicking",
            works_id, contract_id,
        )
        await first_row.click()

    except Exception as exc:
        if _is_target_closed(exc):
            raise RuntimeError(
                f"Page was closed unexpectedly while searching for address {address!r}. "
                f"This usually means contracts/index.php redirected the page away "
                f"(e.g. session expired mid-request, or JavaScript navigated elsewhere). "
                f"Try the request again — if it fails consistently, enable EASYBOP_KEEP_PAGE_OPEN "
                f"and check where the browser ends up after navigating to contracts/index.php. "
                f"Inner error: {exc}"
            ) from exc
        raise

    # Wait for navigation to works_details.php
    try:
        await page.wait_for_url(_RE_WORKS_DETAILS, timeout=timeout_ms)
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except Exception as exc:
        raise RuntimeError(
            f"works_details.php did not load after clicking row for {address!r}: {exc}"
        ) from exc

    log.info("variation: works_details loaded — %s", page.url)
    await asyncio.sleep(0.8)

    return {"works_id": works_id, "contract_id": contract_id}


# ---------------------------------------------------------------------------
# Navigate to Variations tab
# ---------------------------------------------------------------------------

async def _open_variations_tab(
    page: Page,
    works_id: str,
    contract_id: str,
    timeout_ms: int,
) -> str:
    """
    Navigate directly to the Variations tab by appending &tab_nav_item=variations.
    Returns the full URL used.
    """
    variations_url = (
        f"{WORKS_DETAILS_BASE}"
        f"?works_id={works_id}&contract_id={contract_id}&tab_nav_item=variations"
    )
    log.info("variation: navigating to Variations tab — %s", variations_url)
    await page.goto(variations_url, wait_until="domcontentloaded")
    await asyncio.sleep(1.0)

    btn = page.locator("#btn_add_new_variation")
    try:
        await btn.wait_for(state="visible", timeout=timeout_ms)
        log.info("variation: #btn_add_new_variation is visible")
    except Exception as exc:
        raise RuntimeError(
            f"Add New Variation button not found after navigating to Variations tab: {exc}"
        ) from exc

    return variations_url


# ---------------------------------------------------------------------------
# Fill the Add New Variation form
# ---------------------------------------------------------------------------

async def _fill_variation_form(
    page: Page,
    *,
    days: int,
    subject: str,
    timeout_ms: int,
) -> None:
    """
    Fill the Add New Variation dialog that is already open:

      1. Select "Additional works item" from #request_id
      2. Fill #days
      3. Fill #subject
      4. Read UPRN from the page → fill #purchase_order_number
      5. Tick #pricing_required checkbox
      6. Wait 4 s (required before JW Rates button appears)
      7. Click JW Rates button
      8. Wait 3 s (Phase 2 checkpoint)
    """
    # 1. Select "Additional works item" (value="321")
    log.info("variation: selecting 'Additional works item' from #request_id")
    request_sel = page.locator("#request_id")
    await request_sel.wait_for(state="visible", timeout=timeout_ms)
    await request_sel.select_option(value="321")

    # 2. Fill days
    log.info("variation: filling #days = %s", days)
    await page.locator("#days").fill(str(days))

    # 3. Fill subject
    log.info("variation: filling #subject = %r", subject)
    await page.locator("#subject").fill(subject)

    # 4. Read UPRN from the page and use it as the purchase order number.
    #    The UPRN lives in a .box_title_item div outside the form modal.
    uprn_value = ""
    try:
        uprn_div = page.locator(".box_title_item").filter(has_text="UPRN:")
        uprn_text = await uprn_div.first.inner_text()   # e.g. "UPRN: 1283085"
        m = re.search(r"\d+", uprn_text)
        uprn_value = m.group(0) if m else ""
    except Exception:
        log.warning("variation: could not read UPRN from page — purchase_order_number left blank")
    log.info("variation: UPRN=%r → #purchase_order_number", uprn_value)
    await page.locator("#purchase_order_number").fill(uprn_value)

    # 5. Tick pricing_required checkbox
    checkbox = page.locator("#pricing_required")
    if not await checkbox.is_checked():
        await checkbox.click()
    log.info("variation: #pricing_required ticked")

    # 6. Wait 4 s — required for the BOQ/JW Rates section to appear after ticking pricing
    log.info("variation: waiting 4 s for BOQ section to appear")
    await asyncio.sleep(2.0)

    # 7. Click JW Rates button
    jw_rates = page.locator("div.boq_button[data-boq-name='JW Rates']")
    try:
        await jw_rates.wait_for(state="visible", timeout=timeout_ms)
    except Exception as exc:
        raise RuntimeError(
            f"JW Rates button not visible after ticking pricing_required. "
            f"The BOQ section may not have loaded. Inner error: {exc}"
        ) from exc
    await jw_rates.click()
    log.info("variation: clicked JW Rates — waiting for JW Rates dialog #boq_search")

    search, _ = await _jw_rates_search_box(page, timeout_ms)
    await asyncio.sleep(0.6)
    log.info("variation: JW Rates dialog open (#boq_search visible and scoped)")


# ---------------------------------------------------------------------------
# Search and fill a SOR item inside the open JW Rates dialog
# ---------------------------------------------------------------------------


def _jw_rates_dialog(page: Page):
    """Visible JW Rates picker — contains #boq_search and Continue (#btn_close)."""
    return page.locator(".ui-dialog:visible").filter(has=page.locator("#boq_search"))


async def _jw_rates_search_box(page: Page, timeout_ms: int):
    """#boq_search inside the visible JW Rates dialog (avoids duplicate ids on the page)."""
    jw_dialog = _jw_rates_dialog(page)
    await jw_dialog.wait_for(state="visible", timeout=timeout_ms)
    search = jw_dialog.locator("#boq_search").first
    await search.wait_for(state="visible", timeout=timeout_ms)
    try:
        await search.wait_for(state="editable", timeout=5_000)
    except Exception:
        log.warning("variation: #boq_search not editable yet — will retry click")
    return search, jw_dialog


async def _fill_jw_rates_item(
    page: Page,
    *,
    sor_ref: str,
    quantity: float,
    timeout_ms: int,
) -> None:
    """
    Inside the open JW Rates dialog:

      1. Type sor_ref into #boq_search and wait for tr.search-result to filter.
      2. Find the visible row whose td.item_ref matches sor_ref.
      3. Fill input.qty (click, clear, type, Tab) — same as insert_boq_item.py.
    """
    ref_str = str(sor_ref).strip()
    log.info("variation: searching JW Rates #boq_search for sor_ref=%r", ref_str)

    search, jw_dialog = await _jw_rates_search_box(page, timeout_ms)
    await search.scroll_into_view_if_needed()
    await search.click(timeout=timeout_ms)
    await search.fill("")
    try:
        await search.press_sequentially(ref_str, delay=60, timeout=timeout_ms)
    except Exception as exc:
        log.warning(
            "variation: press_sequentially failed (%s) — filling via JS input event",
            exc,
        )
        await search.evaluate(
            """(el, ref) => {
                el.focus();
                el.value = ref;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('keyup', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            ref_str,
        )
    log.info("variation: typed %r into #boq_search — waiting for search results", ref_str)

    matched_row = None
    deadline = time.monotonic() + min(timeout_ms, 20_000) / 1000
    while time.monotonic() < deadline:
        rows = jw_dialog.locator("tr.search-result")
        count = await rows.count()
        for i in range(count):
            row = rows.nth(i)
            if not await row.is_visible():
                continue
            ref_cell = row.locator("td.item_ref")
            if await ref_cell.count() == 0:
                continue
            cell_text = (await ref_cell.text_content() or "").strip()
            if cell_text == ref_str:
                matched_row = row
                break
        if matched_row is not None:
            break
        await asyncio.sleep(0.3)

    if matched_row is None:
        visible_refs: list[str] = []
        rows = jw_dialog.locator("tr.search-result")
        for i in range(min(10, await rows.count())):
            row = rows.nth(i)
            if not await row.is_visible():
                continue
            ref_cell = row.locator("td.item_ref")
            if await ref_cell.count():
                visible_refs.append((await ref_cell.text_content() or "").strip())
        raise RuntimeError(
            f"SOR ref {ref_str!r} not found in JW Rates after #boq_search. "
            f"Visible refs: {visible_refs}"
        )

    log.info("variation: matched JW Rates row for sor_ref=%r", ref_str)

    qty_input = matched_row.locator("input.qty")
    await qty_input.wait_for(state="visible", timeout=timeout_ms)
    await qty_input.click()
    await qty_input.fill("")
    qty_text = str(int(quantity)) if float(quantity).is_integer() else str(quantity)
    try:
        await qty_input.press_sequentially(qty_text, delay=30, timeout=timeout_ms)
    except Exception:
        await qty_input.fill(qty_text)
    await qty_input.press("Tab")
    await asyncio.sleep(0.5)

    qty_actual = (await qty_input.input_value() or "").strip()
    log.info("variation: qty set to %s (input.qty field shows %r)", qty_text, qty_actual)

    if not qty_actual or float(qty_actual) == 0:
        raise RuntimeError(
            f"JW Rates qty still 0 after fill for sor_ref={ref_str!r} — "
            f"check input.qty selector"
        )


async def _continue_jw_rates_dialog(page: Page, timeout_ms: int) -> None:
    """Click Continue in the JW Rates picker (#btn_close), not Escape."""
    jw_dialog = _jw_rates_dialog(page)
    if await jw_dialog.count() == 0:
        continue_btn = page.locator("#btn_close")
    else:
        continue_btn = jw_dialog.locator("#btn_close").first

    await continue_btn.wait_for(state="visible", timeout=timeout_ms)
    await page.wait_for_function(
        """() => {
          const dlg = document.querySelector('.ui-dialog:not([style*="display: none"]) #boq_search');
          const btn = document.getElementById('btn_close');
          return btn && !btn.disabled && !btn.classList.contains('ui-state-disabled');
        }""",
        timeout=timeout_ms,
    )
    log.info("variation: clicking Continue (#btn_close)")
    await continue_btn.click()
    await page.wait_for_selector("#boq_search", state="hidden", timeout=timeout_ms)
    await asyncio.sleep(1.5)
    log.info("variation: JW Rates dialog closed after Continue")


async def _wait_for_staged_boq_line(
    page: Page,
    *,
    sor_ref: str,
    quantity: float,
    timeout_ms: int,
) -> None:
    """
    After JW Rates Continue, the picked SOR must appear in #boq_table as a
    staged row (class new_item) before Add Variation is clicked.
    """
    ref = str(sor_ref).strip()
    log.info(
        "variation: waiting for #boq_table staged line sor_ref=%r qty=%s",
        ref, quantity,
    )

    row = page.locator("#boq_table tr.works_boq_item").filter(has_text=ref)
    try:
        await row.first.wait_for(state="visible", timeout=timeout_ms)
    except Exception as exc:
        raise RuntimeError(
            f"BOQ line for SOR {ref!r} not visible in #boq_table after Continue. "
            f"The quantity may not have been applied — cannot click Add Variation."
        ) from exc

    new_row = page.locator("#boq_table tr.works_boq_item.new_item").filter(has_text=ref)
    if await new_row.count() > 0 and await new_row.first.is_visible():
        log.info("variation: #boq_table new_item row visible for sor_ref=%r", ref)
    else:
        log.info("variation: #boq_table works_boq_item row visible for sor_ref=%r", ref)

    qty_text = str(int(quantity)) if float(quantity).is_integer() else str(quantity)
    qty_input = row.first.locator("input.item_quantity")
    if await qty_input.count() > 0:
        actual = (await qty_input.first.input_value() or "").strip()
        if actual and float(actual) != float(quantity):
            log.warning(
                "variation: #boq_table qty mismatch — expected %s, field shows %r",
                qty_text, actual,
            )


def _wire_dialog_auto_accept(page: Page) -> None:
    async def _on_dialog(dialog) -> None:
        log.info("variation: browser dialog %r — clicking OK", dialog.message or "")
        await dialog.accept()

    page.on("dialog", _on_dialog)


async def _add_and_save_variation(
    page: Page,
    *,
    sor_ref: str,
    quantity: float,
    timeout_ms: int,
) -> None:
    """
    After JW Rates Continue and #boq_table shows the staged line:
      1. Click Add Variation (#btn_add_variation) → accept "Add Variation?" confirm.
      2. Click Save Variation (#btn_save_changes) → accept "Save Variation?" confirm.
    """
    _wire_dialog_auto_accept(page)

    add_btn = page.locator("#btn_add_variation").first
    await add_btn.wait_for(state="visible", timeout=timeout_ms)
    log.info("variation: clicking Add Variation (#btn_add_variation)")
    await add_btn.click()
    await asyncio.sleep(2.0)

    # Form re-renders after Add Variation confirm — locate Save on the full page.
    save_btn = page.locator("#btn_save_changes").filter(
        has_text=re.compile(r"save\s+variation", re.I)
    )
    if await save_btn.count() == 0:
        save_btn = page.locator("#btn_save_changes")
    await save_btn.first.wait_for(state="visible", timeout=timeout_ms)
    log.info("variation: clicking Save Variation (#btn_save_changes)")
    await save_btn.first.click()

    # Allow save + confirm + page refresh.
    await asyncio.sleep(3.0)


async def _verify_variation_on_tab(
    page: Page,
    *,
    works_id: str,
    contract_id: str,
    sor_ref: str,
    subject: str,
    timeout_ms: int,
) -> None:
    """Reload Variations tab and confirm the saved variation is listed."""
    ref = str(sor_ref).strip()
    subj = str(subject).strip()
    variations_url = (
        f"{WORKS_DETAILS_BASE}"
        f"?works_id={works_id}&contract_id={contract_id}&tab_nav_item=variations"
    )
    log.info("variation: verifying saved variation on tab — %s", variations_url)
    await page.goto(variations_url, wait_until="domcontentloaded")
    await asyncio.sleep(2.0)

    deadline = time.monotonic() + (min(timeout_ms, 15_000) / 1000)
    while time.monotonic() < deadline:
        body = (await page.inner_text("body")) or ""
        if subj and subj.lower() in body.lower():
            log.info("variation: confirmed subject %r on Variations tab", subj)
            return
        if ref and ref in body:
            log.info("variation: confirmed SOR %r on Variations tab", ref)
            return
        await asyncio.sleep(0.5)

    raise RuntimeError(
        f"Saved variation not found on Variations tab "
        f"(subject={subj!r}, sor={ref!r}, works_id={works_id})"
    )


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

async def navigate_to_add_variation(
    page: Page,
    *,
    address: str,
    days: int = 2,
    subject: str = "",
    sor_ref: str = "",
    quantity: float = 1.0,
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout_ms: int = 30_000,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> dict[str, Any]:
    """
    Navigate to the Add New Variation dialog, fill the form, and add a BOQ item.

    Returns a result dict:
      {success, works_id, contract_id, variations_url, address, message}
    """
    log.info(
        "navigate_to_add_variation: address=%r days=%s subject=%r sor_ref=%r qty=%s timeout_ms=%d",
        address, days, subject, sor_ref, quantity, timeout_ms,
    )

    # Step 1 — navigate to jobs index, handle all session states
    await _ensure_jobs_index(page, username or "", password or "", after_relogin)

    # Steps 2+3 — search address, click first active row
    row_info    = await _search_and_click_job_row(page, address, timeout_ms)
    works_id    = row_info["works_id"]
    contract_id = row_info["contract_id"]

    # Step 4 — open Variations tab
    variations_url = await _open_variations_tab(page, works_id, contract_id, timeout_ms)

    # Step 5 — click Add New Variation, let dialog animate open
    await page.locator("#btn_add_new_variation").first.click()
    log.info("variation: clicked Add New Variation — waiting 1.5 s for dialog")
    await asyncio.sleep(1.5)

    # Step 6 — fill the variation form, tick pricing, open JW Rates dialog
    await _fill_variation_form(page, days=days, subject=subject, timeout_ms=timeout_ms)

    # Step 7 — search JW Rates BOQ and set quantity for the SOR item
    await _fill_jw_rates_item(page, sor_ref=sor_ref, quantity=quantity, timeout_ms=timeout_ms)

    # Step 8 — Continue out of JW Rates picker
    await _continue_jw_rates_dialog(page, timeout_ms)

    # Step 9 — staged line must appear in #boq_table before Add Variation
    await _wait_for_staged_boq_line(
        page, sor_ref=sor_ref, quantity=quantity, timeout_ms=timeout_ms
    )

    # Step 10 — Add Variation + Save Variation (with browser confirms)
    await _add_and_save_variation(
        page, sor_ref=sor_ref, quantity=quantity, timeout_ms=timeout_ms
    )

    # Step 11 — confirm variation persisted on the Variations tab
    await _verify_variation_on_tab(
        page,
        works_id=works_id,
        contract_id=contract_id,
        sor_ref=sor_ref,
        subject=subject,
        timeout_ms=timeout_ms,
    )

    return {
        "success": True,
        "works_id": works_id,
        "contract_id": contract_id,
        "variations_url": variations_url,
        "address": address,
        "message": (
            f"Variation saved: SOR {sor_ref!r} qty={quantity} on works_id={works_id}."
        ),
    }
