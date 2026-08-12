"""
EasyBOP Asbestos Monitor — automation layer.

Navigates to the pre_works planned works report page, waits for the jqGrid
table to load, then scrapes every row where the
'Global Asbestos Survey Notes' cell (aria-describedby='list_wi_pw_24818_notes')
has class 'jqgrid_bg_red', indicating the asbestos report is MISSING.

Returns a list of dicts: {uprn, address, row_id, corf}
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, List, Optional

from playwright.async_api import Page

log = logging.getLogger("asbestos.monitor")

LOGIN_URL = "https://easybop.co.uk/"
REPORT_URL = "https://easybop.co.uk/a_planned_works/z_works_reports/pre_works.php?contract_id={contract_id}"

# The aria-describedby value for the "Global Asbestos Survey Notes" column
ASBESTOS_NOTES_COL = "list_wi_pw_24818_notes"


# ---------------------------------------------------------------------------
# Login helpers (identical pattern to JIS-form)
# ---------------------------------------------------------------------------

async def _shows_login_wall(page: Page) -> bool:
    try:
        lb = page.locator("#login_box")
        return await lb.count() > 0 and await lb.first.is_visible()
    except Exception:
        return False


async def _page_looks_like_login(page: Page) -> bool:
    try:
        if await _shows_login_wall(page):
            return True
        if "login" in (await page.title() or "").lower():
            return True
        return await page.get_by_text("EasyBOP Login", exact=False).count() > 0
    except Exception:
        return False


async def login(page: Page, username: str, password: str) -> bool:
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
# Navigate to the report and handle re-login
# ---------------------------------------------------------------------------

async def _navigate_to_report(
    page: Page,
    contract_id: str,
    username: Optional[str],
    password: Optional[str],
    timeout_ms: int,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> None:
    url = REPORT_URL.format(contract_id=contract_id)
    log.info("navigate: -> %s", url)
    await page.goto(url, wait_until="domcontentloaded")

    if await _page_looks_like_login(page):
        if not username or not password:
            raise RuntimeError("Login required but credentials not set in .env")
        log.info("navigate: session expired — re-logging in")
        if not await login(page, username, password):
            raise RuntimeError("EasyBOP re-login failed")
        if after_relogin:
            await after_relogin()
        await page.goto(url, wait_until="domcontentloaded")

    if await _page_looks_like_login(page):
        raise RuntimeError("Still on login wall after re-login — check credentials")


# ---------------------------------------------------------------------------
# Wait for the jqGrid table to finish loading
# ---------------------------------------------------------------------------

async def _wait_for_grid(page: Page, timeout_ms: int) -> None:
    """
    Wait until:
    1. The loading overlay disappears (#load_list is hidden)
    2. At least one data row (tr.jqgrow) is visible in the grid
    """
    log.info("grid: waiting for jqGrid to load...")

    # Wait for loading overlay to go away
    try:
        await page.wait_for_function(
            "() => {"
            "  const el = document.getElementById('load_list');"
            "  return !el || el.style.display === 'none' || !el.offsetParent;"
            "}",
            timeout=timeout_ms,
        )
        log.info("grid: loading overlay gone")
    except Exception as e:
        log.warning("grid: loading overlay wait timed out: %s", e)

    # Wait for actual data rows
    try:
        await page.wait_for_selector("tr.jqgrow", state="attached", timeout=timeout_ms)
        log.info("grid: jqgrow rows found")
    except Exception as e:
        raise RuntimeError(f"jqGrid rows never appeared within {timeout_ms}ms: {e}") from e

    # Short settle for any lazy cells
    await asyncio.sleep(1.0)


async def _wait_for_grid_overlay(page: Page, timeout_ms: int) -> None:
    try:
        await page.wait_for_function(
            "() => {"
            "  const el = document.getElementById('load_list');"
            "  return !el || el.style.display === 'none' || !el.offsetParent;"
            "}",
            timeout=min(20_000, timeout_ms),
        )
    except Exception as e:
        log.warning("grid: overlay wait timed out after page change: %s", e)
    await asyncio.sleep(0.4)


async def _next_pre_works_page_enabled(page: Page) -> bool:
    return bool(
        await page.evaluate(
            """() => {
                const nextTd = document.getElementById('next_list_pager');
                if (!nextTd) return false;
                return !nextTd.classList.contains('ui-state-disabled');
            }"""
        )
    )


async def _go_to_next_pre_works_page(page: Page, *, timeout_ms: int = 15_000) -> bool:
    if not await _next_pre_works_page_enabled(page):
        return False

    info_before: Any = await page.evaluate(
        """() => {
            const inp = document.querySelector(
                '#pg_list_pager input.ui-pg-input, #pager_list input.ui-pg-input, input.ui-pg-input'
            );
            const currentPage = inp ? (parseInt(inp.value, 10) || 1) : 1;
            const totalPagesEl = document.querySelector(
                '#sp_1_list_pager, #sp_1_pager_list, span[id^="sp_1_"]'
            );
            const totalPages = totalPagesEl
                ? (parseInt(totalPagesEl.textContent || '1', 10) || 1)
                : 1;
            return { currentPage, totalPages };
        }"""
    ) or {}
    current = int(info_before.get("currentPage") or 1)

    first_id_before = await page.evaluate(
        """() => {
            const tr = document.querySelector('table.ui-jqgrid-btable tbody tr[id], tr.jqgrow[id]');
            return tr && /^\\d+$/.test(tr.id) ? tr.id : '';
        }"""
    )

    next_td = page.locator("#next_list_pager")
    icon = next_td.locator(".ui-icon-seek-next")
    if await icon.count() > 0:
        await icon.click()
    else:
        await next_td.click()

    try:
        await page.wait_for_function(
            f"""() => {{
                const inp = document.querySelector(
                    '#pg_list_pager input.ui-pg-input, #pager_list input.ui-pg-input, input.ui-pg-input'
                );
                const pageNum = inp ? (parseInt(inp.value, 10) || 0) : 0;
                if (pageNum > {current}) return true;
                const tr = document.querySelector('table.ui-jqgrid-btable tbody tr[id], tr.jqgrow[id]');
                const rid = tr && /^\\d+$/.test(tr.id) ? tr.id : '';
                return rid && rid !== '{first_id_before}';
            }}""",
            timeout=timeout_ms,
        )
    except Exception:
        pass

    await _wait_for_grid_overlay(page, timeout_ms)
    return True


_SCRAPE_READY_FOR_UNSUSPEND_JS = """
([asbestosCol, statusCol, statusNeedle]) => {
    const found = [];
    const needle = String(statusNeedle || '').toLowerCase();
    const rows = document.querySelectorAll('tr.jqgrow');
    rows.forEach(row => {
        const notesCell = row.querySelector(
            `td[aria-describedby="${asbestosCol}"]`
        );
        if (!notesCell || !notesCell.classList.contains('jqgrid_bg_green')) {
            return;
        }
        const statusCell = row.querySelector(
            `td[aria-describedby="${statusCol}"]`
        );
        const adminStatus = statusCell
            ? (statusCell.getAttribute('title') || statusCell.innerText || '').trim()
            : '';
        if (!adminStatus.toLowerCase().includes(needle)) {
            return;
        }
        const addrCell = row.querySelector('td[aria-describedby="list_address"]');
        const uprnCell = row.querySelector('td[aria-describedby="list_property_ref"]');
        const corfCell = row.querySelector('td[aria-describedby="list_client_order_reference"]');
        found.push({
            row_id: row.id || null,
            uprn: uprnCell
                ? (uprnCell.getAttribute('title') || uprnCell.innerText || '').trim()
                : null,
            address: addrCell
                ? (addrCell.getAttribute('title') || addrCell.innerText || '').trim()
                : null,
            corf: corfCell
                ? (corfCell.getAttribute('title') || corfCell.innerText || '').trim()
                : null,
            admin_status: adminStatus,
        });
    });
    return found;
}
"""


# ---------------------------------------------------------------------------
# Scrape missing asbestos rows
# ---------------------------------------------------------------------------

async def scrape_missing_asbestos(
    page: Page,
    contract_id: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout_ms: int = 30_000,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> List[dict]:
    """
    Navigate to the pre_works report, wait for the grid to load, then return
    a list of {uprn, address, row_id, corf} for every row where the
    'Global Asbestos Survey Notes' column cell is red (jqgrid_bg_red).
    """
    await _navigate_to_report(page, contract_id, username, password, timeout_ms, after_relogin)
    await _wait_for_grid(page, timeout_ms)

    # Run the scraper via JS — mirrors exactly what we tested in the console
    results: List[Any] = await page.evaluate(
        """
        (asbestosCol) => {
            const found = [];
            const rows = document.querySelectorAll('tr.jqgrow');
            rows.forEach(row => {
                const notesCell = row.querySelector(
                    `td[aria-describedby="${asbestosCol}"]`
                );
                if (notesCell && notesCell.classList.contains('jqgrid_bg_red')) {
                    const addrCell  = row.querySelector('td[aria-describedby="list_address"]');
                    const uprnCell  = row.querySelector('td[aria-describedby="list_property_ref"]');
                    const corfCell  = row.querySelector('td[aria-describedby="list_client_order_reference"]');
                    found.push({
                        row_id:  row.id || null,
                        uprn:    uprnCell  ? (uprnCell.getAttribute('title')  || uprnCell.innerText || '').trim() : null,
                        address: addrCell  ? (addrCell.getAttribute('title')  || addrCell.innerText || '').trim() : null,
                        corf:    corfCell  ? (corfCell.getAttribute('title')  || corfCell.innerText || '').trim() : null,
                    });
                }
            });
            return found;
        }
        """,
        ASBESTOS_NOTES_COL,
    )

    log.info("scrape: found %d properties with missing asbestos report", len(results))
    return results


# Administrative Status column on the pre_works grid
WORKS_STATUS_COL = "list_works_status"
SUSPENDED_ASBESTOS_STATUS = "suspended: awaiting asbestos info"


async def scrape_ready_for_unsuspend(
    page: Page,
    contract_id: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout_ms: int = 30_000,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
    max_pages: int = 200,
) -> List[dict]:
    """
    Navigate to the pre_works report, paginate via #next_list_pager, and return
    a list of {uprn, address, row_id, corf} for every row where the
    'Global Asbestos Survey Notes' column is green (jqgrid_bg_green) and
    Administrative Status is 'Suspended: Awaiting asbestos info'.
    """
    await _navigate_to_report(page, contract_id, username, password, timeout_ms, after_relogin)
    await _wait_for_grid(page, timeout_ms)

    scrape_args = [ASBESTOS_NOTES_COL, WORKS_STATUS_COL, SUSPENDED_ASBESTOS_STATUS]
    by_row_id: dict[str, dict] = {}
    pages_scraped = 0

    for page_num in range(1, max_pages + 1):
        page_rows: List[Any] = await page.evaluate(_SCRAPE_READY_FOR_UNSUSPEND_JS, scrape_args)
        pages_scraped = page_num
        for row in page_rows:
            row_id = str(row.get("row_id") or "").strip()
            if row_id:
                by_row_id[row_id] = row
            else:
                key = f"{row.get('corf')}::{row.get('address')}"
                by_row_id[key] = row

        log.info(
            "unsuspend scrape: page %d — %d match(es) on page (%d unique so far)",
            page_num,
            len(page_rows),
            len(by_row_id),
        )

        if not await _next_pre_works_page_enabled(page):
            break
        if not await _go_to_next_pre_works_page(page, timeout_ms=min(20_000, timeout_ms)):
            log.warning("unsuspend scrape: next page click failed on page %d", page_num)
            break

    results = list(by_row_id.values())
    log.info(
        "scrape: found %d properties ready for un-suspend across %d page(s)",
        len(results),
        pages_scraped,
    )
    return results
