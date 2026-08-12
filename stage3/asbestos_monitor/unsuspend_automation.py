"""
EasyBOP Bulk Un-suspend Automation.

For each CORF (Client Order Reference) supplied:
  1. Navigate to the planned-works index (z_works/index.php).
  2. Wait for jqGrid to load.
  3. Paginate via #next_list_pager — tick checkbox per matching CORF row.
  4. Click #btn_bulk_unsuspend ("Bulk Un-suspend Works").
  5. Accept the native browser confirm dialog ("Un-Suspend Works?" — OK).

Returns:
  { total_requested, total_checked, checked, not_found, success, message }
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, List, Optional

from playwright.async_api import Dialog, Page

log = logging.getLogger("asbestos.unsuspend")

LOGIN_URL = "https://easybop.co.uk/"
INDEX_URL = "https://easybop.co.uk/a_planned_works/z_works/index.php?contract_id={contract_id}"

_DIALOG_HANDLER_ATTR = "_easybop_unsuspend_dialog_wired"
DEFAULT_MAX_PAGES = 200


def _ensure_browser_dialog_auto_accept(page: Page) -> None:
    """Accept native window.confirm() — e.g. 'Un-Suspend Works?' after bulk button."""
    if getattr(page, _DIALOG_HANDLER_ATTR, False):
        return

    async def _accept_native_dialog(dialog: Dialog) -> None:
        log.info('unsuspend: native browser dialog "%s" — clicking OK', dialog.message)
        await dialog.accept()

    page.on("dialog", _accept_native_dialog)
    setattr(page, _DIALOG_HANDLER_ATTR, True)
    log.info("unsuspend: registered browser dialog auto-accept handler")


def _log_corf_match_summary(
    requested: List[str],
    checked: List[str],
    not_found: List[str],
) -> None:
    log.info("unsuspend: CORF match summary")
    log.info("  requested: %d", len(requested))
    log.info("  checked:   %d", len(checked))
    log.info("  not found: %d", len(not_found))
    if checked:
        log.info("  checked CORFs:")
        for index, corf in enumerate(checked, start=1):
            log.info("    %3d. %s", index, corf)
    if not_found:
        log.warning("  not found in grid:")
        for index, corf in enumerate(not_found, start=1):
            log.warning("    %3d. %s", index, corf)


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


async def _ensure_index_ready(
    page: Page,
    contract_id: str,
    username: Optional[str],
    password: Optional[str],
    timeout_ms: int,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> None:
    url = INDEX_URL.format(contract_id=contract_id)
    log.info("unsuspend: navigate -> %s", url)
    await page.goto(url, wait_until="domcontentloaded")

    if await _page_looks_like_login(page):
        if not username or not password:
            raise RuntimeError("Login required but credentials not set in .env")
        log.info("unsuspend: session expired — re-logging in")
        if not await _login(page, username, password):
            raise RuntimeError("EasyBOP re-login failed")
        if after_relogin:
            await after_relogin()
        await page.goto(url, wait_until="domcontentloaded")

    if await _page_looks_like_login(page):
        raise RuntimeError("Still on login wall after re-login — check credentials")


async def _wait_for_grid(page: Page, timeout_ms: int) -> None:
    log.info("unsuspend: waiting for jqGrid...")
    try:
        await page.wait_for_function(
            "() => {"
            "  const el = document.getElementById('load_list');"
            "  return !el || el.style.display === 'none' || !el.offsetParent;"
            "}",
            timeout=timeout_ms,
        )
    except Exception as e:
        log.warning("unsuspend: grid overlay timed out: %s", e)

    try:
        await page.wait_for_selector("tr.jqgrow", state="attached", timeout=timeout_ms)
        log.info("unsuspend: grid rows found")
    except Exception as e:
        raise RuntimeError(f"jqGrid rows never appeared within {timeout_ms}ms: {e}") from e

    await asyncio.sleep(0.8)


async def _wait_for_grid_overlay(page: Page, timeout_ms: int) -> None:
    try:
        await page.wait_for_function(
            "() => {"
            "  const el = document.getElementById('load_list');"
            "  return !el || el.style.display === 'none' || !el.offsetParent;"
            "}",
            timeout=min(20_000, timeout_ms),
        )
    except Exception:
        pass
    await asyncio.sleep(0.35)


async def _next_page_enabled(page: Page) -> bool:
    return bool(
        await page.evaluate(
            """() => {
                const nextTd = document.getElementById('next_list_pager');
                if (!nextTd) return false;
                return !nextTd.classList.contains('ui-state-disabled');
            }"""
        )
    )


async def _go_to_next_page(page: Page, *, timeout_ms: int = 15_000) -> bool:
    if not await _next_page_enabled(page):
        return False

    info_before: Any = await page.evaluate(
        """() => {
            const inp = document.querySelector(
                '#pg_list_pager input.ui-pg-input, #pager_list input.ui-pg-input, input.ui-pg-input'
            );
            return { currentPage: inp ? (parseInt(inp.value, 10) || 1) : 1 };
        }"""
    ) or {}
    current = int(info_before.get("currentPage") or 1)

    first_id_before = await page.evaluate(
        """() => {
            const tr = document.querySelector('tr.jqgrow[id]');
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
                const tr = document.querySelector('tr.jqgrow[id]');
                const rid = tr && /^\\d+$/.test(tr.id) ? tr.id : '';
                return rid && rid !== '{first_id_before}';
            }}""",
            timeout=timeout_ms,
        )
    except Exception:
        pass

    await _wait_for_grid_overlay(page, timeout_ms)
    return True


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

    const foundOnPage = [];
    const cells = document.querySelectorAll('td[aria-describedby="list_client_order_reference"]');
    cells.forEach(cell => {
        const cellCorf = normCorf(cell.getAttribute('title') || cell.textContent || '');
        if (!cellCorf) return;

        const match = corfs.find(c => normCorf(c) === cellCorf);
        if (!match) return;

        const row = cell.closest('tr');
        if (!row) return;
        const cb = row.querySelector('input[type="checkbox"].cbox');
        if (cb && !cb.checked) {
            cb.click();
        }
        foundOnPage.push(match);
    });

    return foundOnPage;
}
"""


async def _wait_for_unsuspend_settled(page: Page, timeout_ms: int) -> None:
    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        grid_loading = await page.evaluate(
            """() => {
              const loadEl = document.getElementById('load_list');
              return !!(loadEl && loadEl.offsetParent !== null
                && loadEl.style.display !== 'none');
            }"""
        )
        if not grid_loading:
            return
        await asyncio.sleep(0.4)
    log.warning("unsuspend: grid still loading after bulk un-suspend — continuing")


async def _find_and_check_corfs_across_pages(
    page: Page,
    corfs: List[str],
    *,
    timeout_ms: int,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> tuple[List[str], List[str]]:
    """Paginate the works index grid and tick checkboxes for each CORF."""
    remaining = list(corfs)
    checked: List[str] = []
    pages_scraped = 0

    for page_num in range(1, max_pages + 1):
        pages_scraped = page_num
        if not remaining:
            break

        found_on_page: List[str] = await page.evaluate(_CHECK_CORF_JS, remaining)
        if found_on_page:
            for corf in found_on_page:
                if corf not in checked:
                    checked.append(corf)
            remaining = [c for c in remaining if c not in checked]
            log.info(
                "unsuspend: page %d — checked %d CORF(s) (%d remaining)",
                page_num,
                len(found_on_page),
                len(remaining),
            )

        if not remaining:
            break

        if not await _next_page_enabled(page):
            break
        if not await _go_to_next_page(page, timeout_ms=min(20_000, timeout_ms)):
            log.warning("unsuspend: next page click failed on page %d", page_num)
            break

    log.info(
        "unsuspend: searched %d page(s); checked %d/%d CORF(s)",
        pages_scraped,
        len(checked),
        len(corfs),
    )
    not_found = [c for c in corfs if c not in checked]
    return checked, not_found


async def unsuspend_corfs(
    page: Page,
    corfs: List[str],
    contract_id: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout_ms: int = 30_000,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> dict:
    corfs = [c.strip() for c in corfs if c and str(c).strip()]
    if not corfs:
        return {
            "total_requested": 0,
            "total_checked": 0,
            "not_found": [],
            "checked": [],
            "success": False,
            "message": "No valid CORFs provided.",
        }

    log.info("unsuspend: processing %d CORF(s)", len(corfs))
    _ensure_browser_dialog_auto_accept(page)

    await _ensure_index_ready(page, contract_id, username, password, timeout_ms, after_relogin)
    await _wait_for_grid(page, timeout_ms)

    checked, not_found = await _find_and_check_corfs_across_pages(
        page,
        corfs,
        timeout_ms=timeout_ms,
        max_pages=max_pages,
    )

    _log_corf_match_summary(corfs, checked, not_found)

    if not checked:
        return {
            "total_requested": len(corfs),
            "total_checked": 0,
            "not_found": not_found,
            "checked": checked,
            "success": False,
            "message": "None of the provided CORFs were found in the works index grid.",
        }

    await asyncio.sleep(0.5)

    try:
        unsuspend_btn = page.locator("#btn_bulk_unsuspend")
        await unsuspend_btn.wait_for(state="visible", timeout=10_000)
        await unsuspend_btn.scroll_into_view_if_needed()
        await unsuspend_btn.click()
        log.info("unsuspend: clicked #btn_bulk_unsuspend (%d row(s) selected)", len(checked))
    except Exception as e:
        return {
            "total_requested": len(corfs),
            "total_checked": len(checked),
            "not_found": not_found,
            "checked": checked,
            "success": False,
            "message": f"Could not click Bulk Un-suspend Works button: {e}",
        }

    await asyncio.sleep(0.5)
    await _wait_for_unsuspend_settled(page, timeout_ms=max(timeout_ms, 30_000))

    msg = (
        f"Bulk un-suspend completed. "
        f"Checked {len(checked)}/{len(corfs)} CORF(s). "
        + (f"Not found in grid: {len(not_found)} CORF(s)." if not_found else "All CORFs found.")
    )
    log.info("unsuspend: %s", msg)

    return {
        "total_requested": len(corfs),
        "total_checked": len(checked),
        "not_found": not_found,
        "checked": checked,
        "success": True,
        "message": msg,
    }


# Legacy entry point — still accepts addresses but prefer unsuspend_corfs / corfs API field.
_CHECK_ADDRESS_JS = """
(addresses) => {
    const results = {};
    addresses.forEach(a => { results[a] = false; });
    const cells = document.querySelectorAll('td[aria-describedby="list_address"]');
    cells.forEach(cell => {
        const cellAddr = (cell.getAttribute('title') || cell.textContent || '').trim();
        if (!cellAddr) return;
        const match = addresses.find(a => {
            const cleanA = a.toLowerCase().trim();
            const cleanCell = cellAddr.toLowerCase();
            return cleanA.length > 0 &&
                   (cleanCell.includes(cleanA) || cleanA.includes(cleanCell));
        });
        if (match) {
            const row = cell.closest('tr');
            if (!row) return;
            const cb = row.querySelector('input[type="checkbox"].cbox');
            if (cb && !cb.checked) cb.click();
            results[match] = true;
        }
    });
    return results;
}
"""


async def unsuspend_addresses(
    page: Page,
    addresses: List[str],
    contract_id: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout_ms: int = 30_000,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> dict:
    """Legacy: match by address on page 1 only. Use unsuspend_corfs instead."""
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

    log.warning("unsuspend_addresses is deprecated — use corfs for paginated CORF matching")
    _ensure_browser_dialog_auto_accept(page)
    await _ensure_index_ready(page, contract_id, username, password, timeout_ms, after_relogin)
    await _wait_for_grid(page, timeout_ms)

    check_results: dict = await page.evaluate(_CHECK_ADDRESS_JS, addresses)
    checked = [a for a, ok in check_results.items() if ok]
    not_found = [a for a, ok in check_results.items() if not ok]

    if not checked:
        return {
            "total_requested": len(addresses),
            "total_checked": 0,
            "not_found": not_found,
            "checked": checked,
            "success": False,
            "message": "None of the provided addresses were found in the grid.",
        }

    unsuspend_btn = page.locator("#btn_bulk_unsuspend")
    await unsuspend_btn.wait_for(state="visible", timeout=10_000)
    await unsuspend_btn.click()
    await asyncio.sleep(0.5)
    await _wait_for_unsuspend_settled(page, timeout_ms=max(timeout_ms, 30_000))

    return {
        "total_requested": len(addresses),
        "total_checked": len(checked),
        "not_found": not_found,
        "checked": checked,
        "success": True,
        "message": f"Bulk un-suspend completed. Checked {len(checked)}/{len(addresses)} addresses.",
    }
