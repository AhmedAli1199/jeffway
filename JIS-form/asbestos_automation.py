"""
EasyBOP Asbestos Scraper — part of the JIS-form automation suite.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, List, Optional

from playwright.async_api import Page

log = logging.getLogger("easybop.asbestos")

REPORT_URL = "https://easybop.co.uk/a_planned_works/z_works_reports/pre_works.php?contract_id={contract_id}"
ASBESTOS_NOTES_COL = "list_wi_pw_24818_notes"

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

async def _wait_for_grid(page: Page, timeout_ms: int) -> None:
    log.info("asbestos: waiting for jqGrid...")
    # Loading overlay
    try:
        await page.wait_for_function(
            "() => { const el = document.getElementById('load_list'); return !el || el.style.display === 'none' || !el.offsetParent; }",
            timeout=timeout_ms,
        )
    except Exception:
        pass

    # Data rows
    try:
        await page.wait_for_selector("tr.jqgrow", state="attached", timeout=timeout_ms)
    except Exception as e:
        raise RuntimeError(f"jqGrid rows never appeared within {timeout_ms}ms") from e
    
    await asyncio.sleep(1.0)

async def scrape_missing_asbestos_reports(
    page: Page,
    contract_id: str,
    login_func: Callable[[Page, str, str], Awaitable[bool]],
    username: Optional[str],
    password: Optional[str],
    timeout_ms: int = 30000,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> List[dict]:
    """
    Navigate to pre_works report and scrape rows with missing asbestos (red cells).
    """
    url = REPORT_URL.format(contract_id=contract_id)
    log.info("asbestos: navigating to %s", url)
    await page.goto(url, wait_until="domcontentloaded")

    if await _page_looks_like_login(page):
        if not username or not password:
            raise RuntimeError("Login required but credentials missing")
        log.info("asbestos: session expired, re-logging")
        if not await login_func(page, username, password):
            raise RuntimeError("EasyBOP re-login failed")
        if after_relogin:
            await after_relogin()
        await page.goto(url, wait_until="domcontentloaded")

    await _wait_for_grid(page, timeout_ms)

    # Scrape missing reports (red cells)
    results = await page.evaluate(
        """
        (asbestosCol) => {
            const found = [];
            const rows = document.querySelectorAll('tr.jqgrow');
            rows.forEach(row => {
                const notesCell = row.querySelector(`td[aria-describedby="${asbestosCol}"]`);
                if (notesCell && notesCell.classList.contains('jqgrid_bg_red')) {
                    const addrCell = row.querySelector('td[aria-describedby="list_address"]');
                    const uprnCell = row.querySelector('td[aria-describedby="list_property_ref"]');
                    found.push({
                        row_id: row.id || null,
                        uprn: uprnCell ? (uprnCell.getAttribute('title') || uprnCell.innerText || '').trim() : null,
                        address: addrCell ? (addrCell.getAttribute('title') || addrCell.innerText || '').trim() : null,
                    });
                }
            });
            return found;
        }
        """,
        ASBESTOS_NOTES_COL,
    )
    return results
