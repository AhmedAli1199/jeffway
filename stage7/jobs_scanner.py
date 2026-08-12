"""
Stage 7 — Jobs Scanner

Scans the EasyBOP planned-works index for a given contract and returns
every job where the "Ready to Handover" column (udf_5087) equals "Yes".

Used by n8n to detect jobs that need the Stage 7 variation + task flow.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from playwright.async_api import Page

log = logging.getLogger("stage7.scanner")

LOGIN_URL = "https://easybop.co.uk/"
WORKS_INDEX_URL = (
    "https://easybop.co.uk/a_planned_works/z_works/index.php?contract_id={contract_id}"
)

_TABLE_TBODY_XPATH = (
    "xpath=/html/body/div[2]/div/div/form/div[3]/div/div[3]/div[3]/div/table/tbody"
)

_NETWORKIDLE_MS = 8_000

# aria-describedby value → output field name
_COLUMNS: dict[str, str] = {
    "list_date_received_formatted":              "date_received",
    "list_description_of_works":                 "description",
    "list_appointment_date_formatted":           "appointment_date",
    "list_scope_of_work":                        "scope_of_work",
    "list_property_ref":                         "property_ref",
    "list_address":                              "address",
    "list_client_order_reference":               "client_order_ref",
    "list_physical_works_status":                "physical_status",
    "list_works_financial_status":               "financial_status",
    "list_our_order_reference":                  "our_order_ref",
    "list_date_time_started_formatted":          "date_started",
    "list_date_time_completed_formatted":        "date_completed",
    "list_time_allowed_label":                   "time_allowed",
    "list_recorded_by_name":                     "recorded_by",
    "list_date_latest_completion_allowed_formatted": "latest_completion_date",
    "list_in_time":                              "in_time",
    "list_udf_5069":                             "udf_5069",
    "list_udf_5087":                             "ready_to_handover",
    "list_internal_notes":                       "internal_notes",
    "list_job_value":                            "job_value",
}


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
        log.info("scanner: login success — %s", page.url)
        return True
    except Exception as exc:
        log.exception("scanner: login failed: %s", exc)
        return False


async def _ensure_index(
    page: Page,
    url: str,
    username: str,
    password: str,
    after_relogin: Optional[Callable[[], Awaitable[None]]],
) -> None:
    await _goto_safe(page, url)
    log.info("scanner: landed — url=%s  title=%r", page.url, await page.title())

    if await _page_is_login(page):
        log.info("scanner: session expired — re-authenticating")
        if not await _login(page, username, password):
            raise RuntimeError("EasyBOP login failed — check credentials")
        if after_relogin:
            await after_relogin()
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        log.info("scanner: post-login settle (7 s)")
        await asyncio.sleep(7.0)
        await _goto_safe(page, url)


# ---------------------------------------------------------------------------
# Table reading — JavaScript evaluation (single browser call per page)
# ---------------------------------------------------------------------------

# Injected into the browser to read all jqgrow rows at once.
# Returns [{_works_id, list_address, list_udf_5087, ...}, ...] with raw aria keys.
_JS_READ_ROWS = """
() => {
    const rows = Array.from(document.querySelectorAll('tr.jqgrow'));
    return rows.map(row => {
        const entry = { _works_id: row.id };
        row.querySelectorAll('td[aria-describedby]').forEach(td => {
            const col = td.getAttribute('aria-describedby');
            entry[col] = (td.getAttribute('title') || '').trim();
        });
        return entry;
    });
}
"""


def _map_raw_row(raw: dict) -> dict[str, Any]:
    """Convert raw JS output (aria-describedby keys) to named fields."""
    row: dict[str, Any] = {"works_id": raw.get("_works_id", "")}
    for aria_desc, field_name in _COLUMNS.items():
        row[field_name] = raw.get(aria_desc, "")
    return row


async def _read_current_page_rows(page: Page) -> list[dict[str, Any]]:
    """
    Extract all jqgrow rows on the current page via a single JS call.
    Waits for at least one row to be visible first.
    """
    # Wait for jqGrid to finish loading rows
    tbody = page.locator(_TABLE_TBODY_XPATH)
    try:
        await tbody.locator("tr.jqgrow").first.wait_for(state="visible", timeout=15_000)
    except Exception:
        log.warning("scanner: no rows visible on this page")
        return []

    raw_rows: list[dict] = await page.evaluate(_JS_READ_ROWS)
    log.info("scanner: JS extracted %d rows from current page", len(raw_rows))
    return [_map_raw_row(r) for r in raw_rows]


async def _scan_all_pages(page: Page) -> list[dict[str, Any]]:
    """Paginate through all jqGrid pages and collect every row."""
    all_rows: list[dict[str, Any]] = []
    page_num = 1

    while True:
        log.info("scanner: reading table page %d", page_num)
        rows = await _read_current_page_rows(page)
        all_rows.extend(rows)
        log.info("scanner: cumulative rows so far: %d", len(all_rows))

        # jqGrid next-page button has id starting with "next_"
        # and carries ui-state-disabled class on the last page
        next_btn = page.locator("td[id^='next_']").first
        if await next_btn.count() == 0:
            log.info("scanner: no next-page button — single page")
            break

        cls = (await next_btn.get_attribute("class") or "")
        if "ui-state-disabled" in cls:
            log.info("scanner: last page reached after page %d", page_num)
            break

        # Record first row ID so we can detect when the new page loads
        first_id_before = all_rows[-len(rows)]["works_id"] if rows else ""

        log.info("scanner: clicking Next Page")
        await next_btn.click()

        # Wait until jqGrid swaps in a new first row
        if first_id_before:
            try:
                await page.wait_for_function(
                    f"() => {{ "
                    f"  const first = document.querySelector('tr.jqgrow'); "
                    f"  return first && first.id !== '{first_id_before}'; "
                    f"}}",
                    timeout=15_000,
                )
            except Exception:
                await asyncio.sleep(2.0)
        else:
            await asyncio.sleep(2.0)

        page_num += 1

    return all_rows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def scan_ready_for_handover(
    page: Page,
    *,
    contract_id: str = "321129",
    username: str,
    password: str,
    timeout_ms: int = 30_000,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> dict[str, Any]:
    """
    Navigate to the works index for `contract_id`, read every row across
    all pages, and return those where ready_to_handover == "Yes".

    Response shape:
      {
        success: bool,
        contract_id: str,
        total_scanned: int,
        ready_count: int,
        ready_jobs: [ { works_id, address, description, ... }, ... ]
      }
    """
    url = WORKS_INDEX_URL.format(contract_id=contract_id)
    log.info("scanner: scanning contract_id=%s — %s", contract_id, url)

    await _ensure_index(page, url, username, password, after_relogin)

    all_rows = await _scan_all_pages(page)

    ready_jobs = [
        r for r in all_rows
        if r.get("ready_to_handover", "").strip().lower() == "yes"
    ]

    log.info(
        "scanner: done — total=%d  ready_to_handover=Yes: %d",
        len(all_rows), len(ready_jobs),
    )

    return {
        "success": True,
        "contract_id": contract_id,
        "total_scanned": len(all_rows),
        "ready_count": len(ready_jobs),
        "ready_jobs": ready_jobs,
    }
