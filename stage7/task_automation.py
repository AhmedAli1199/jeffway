"""
Stage 7 — Task Automation

After a variation is added, creates one task on the same job:

  Task 1 — QS Review
    Category : "QS review for variations"
    Description: SOR code + short description + worker's original note
    Issued To: Charlie Higginson
    Priority : Medium
    Due      : today
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, timedelta
from typing import Any, Awaitable, Callable, Optional

from playwright.async_api import Page

log = logging.getLogger("stage7.task")

JOBS_INDEX_URL = "https://easybop.co.uk/a_planned_works/contracts/index.php"
LOGIN_URL = "https://easybop.co.uk/"
_RE_WORKS_DETAILS = re.compile(r"works_details\.php", re.I)
_NETWORKIDLE_MS = 8_000

# Issued-to search strings and the expected autocomplete label
_QS_ISSUED_TO_QUERY = "Charlie"
_QS_ISSUED_TO_LABEL = "Charlie Higginson"

_HANDOVER_ISSUED_TO_QUERY = "Shannon"
_HANDOVER_ISSUED_TO_LABEL = "Shannon Slade"


# ---------------------------------------------------------------------------
# Date utilities
# ---------------------------------------------------------------------------

def _today_dd_mm_yyyy() -> str:
    return date.today().strftime("%d/%m/%Y")


def _next_working_day_dd_mm_yyyy() -> str:
    """Next Mon–Fri date after today, formatted DD/MM/YYYY."""
    d = date.today() + timedelta(days=1)
    while d.weekday() >= 5:  # 5=Saturday, 6=Sunday
        d += timedelta(days=1)
    return d.strftime("%d/%m/%Y")


# ---------------------------------------------------------------------------
# Navigation helpers (same pattern as variation_automation.py)
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
        log.info("task: login success — %s", page.url)
        return True
    except Exception as exc:
        log.exception("task: login failed: %s", exc)
        return False


async def _ensure_jobs_index(
    page: Page,
    username: str,
    password: str,
    after_relogin: Optional[Callable[[], Awaitable[None]]],
) -> None:
    await _goto_safe(page, JOBS_INDEX_URL)
    log.info("task: landed — url=%s  title=%r", page.url, await page.title())

    if await _page_is_login(page):
        log.info("task: session expired — re-authenticating")
        if not await _login(page, username, password):
            raise RuntimeError("EasyBOP login failed — check credentials")
        if after_relogin:
            await after_relogin()
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        log.info("task: post-login settle (7 s)")
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
    log.info("task: searching for address %r", address)
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
                f"Page closed while searching for {address!r} — session may have expired"
            ) from exc
        raise RuntimeError(
            f"No active job rows found for address {address!r}: {exc}"
        ) from exc

    first_row = active_row.first
    works_id = (await first_row.get_attribute("data-works_id") or "").strip()
    contract_id = (await first_row.get_attribute("data-contract_id") or "").strip()
    log.info("task: row found — works_id=%s contract_id=%s", works_id, contract_id)
    await first_row.click()

    try:
        await page.wait_for_url(_RE_WORKS_DETAILS, timeout=timeout_ms)
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except Exception as exc:
        raise RuntimeError(
            f"works_details.php did not load for {address!r}: {exc}"
        ) from exc

    await asyncio.sleep(0.8)
    return {"works_id": works_id, "contract_id": contract_id}


# ---------------------------------------------------------------------------
# Task dialog helpers
# ---------------------------------------------------------------------------

async def _open_tasks_tab(page: Page, timeout_ms: int) -> None:
    tab = page.locator("[data-tab-nav-item='tasks']")
    await tab.first.wait_for(state="visible", timeout=timeout_ms)
    await tab.first.click()
    await asyncio.sleep(0.5)
    log.info("task: Tasks tab opened")


async def _click_add_task(page: Page, timeout_ms: int) -> None:
    btn = page.locator("button[id^='btn_add_task']")
    await btn.first.wait_for(state="visible", timeout=timeout_ms)
    await btn.first.click()
    dialog = page.locator(".ui-dialog:visible, [role='dialog']:visible")
    await dialog.first.wait_for(state="visible", timeout=8_000)
    log.info("task: Add Task dialog opened")


async def _select_category(page: Page, label: str, timeout_ms: int) -> None:
    cat = page.locator("#category_id")
    await cat.first.wait_for(state="visible", timeout=timeout_ms)

    # Try exact match first
    try:
        await cat.first.select_option(label=label)
        log.info("task: category set to %r (exact match)", label)
        return
    except Exception:
        pass

    # Fall back to case-insensitive match and log what's available
    options = await cat.first.locator("option").all()
    label_lower = label.lower()
    available = []
    for opt in options:
        text = (await opt.inner_text()).strip()
        available.append(text)
        if text.lower() == label_lower:
            value = await opt.get_attribute("value")
            await cat.first.select_option(value=value)
            log.info("task: category set to %r (case-insensitive match for %r)", text, label)
            return

    raise RuntimeError(
        f"Category {label!r} not found in #category_id. "
        f"Available options: {available}"
    )


async def _fill_description(page: Page, text: str) -> None:
    desc = page.locator("#description")
    await desc.first.wait_for(state="visible", timeout=5_000)
    await desc.first.fill(text)
    log.info("task: description filled")


async def _fill_issued_to(
    page: Page,
    query: str,
    label: str,
    timeout_ms: int,
) -> None:
    field = page.locator("#issued_to_contact_name")
    await field.wait_for(state="visible", timeout=timeout_ms)
    await field.click()
    await field.fill("")
    await field.type(query, delay=60)
    ac = page.locator(".ui-autocomplete:visible .ui-menu-item").filter(has_text=label)
    await ac.first.wait_for(state="visible", timeout=10_000)
    await ac.first.click()
    log.info("task: Issued To set to %r", label)


async def _select_priority(page: Page, label: str, timeout_ms: int) -> None:
    for sel in (
        "#priority",
        "#task_priority",
        "select[name='priority']",
        "select[name='task_priority']",
    ):
        loc = page.locator(sel)
        if await loc.count() == 0:
            continue
        try:
            await loc.first.wait_for(state="visible", timeout=3_000)
            await loc.first.select_option(label=label)
            log.info("task: priority set to %r via %s", label, sel)
            return
        except Exception:
            continue
    raise RuntimeError(f"Could not set priority to {label!r} — select not found")


async def _fill_due_date(page: Page, date_str: str) -> None:
    field = page.locator("#date_for_completion")
    await field.wait_for(state="visible", timeout=5_000)
    await field.click()
    await field.fill(date_str)
    await field.press("Tab")
    log.info("task: due date set to %r", date_str)


async def _save_task(page: Page, timeout_ms: int) -> None:
    btn = page.locator("#btn_dialog_save_changes")
    if await btn.count() == 0 or not await btn.first.is_visible():
        btn = page.locator(".ui-dialog:visible button").filter(has_text="Save Changes")
    await btn.first.wait_for(state="visible", timeout=timeout_ms)
    await btn.first.click()
    log.info("task: Save Changes clicked")
    try:
        await page.wait_for_selector(".ui-dialog:visible", state="hidden", timeout=10_000)
        log.info("task: dialog closed after save")
    except Exception:
        log.warning("task: dialog still visible after save — proceeding anyway")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_WORKS_DETAILS_BASE = "https://easybop.co.uk/a_planned_works/z_works/works_details.php"


async def create_tasks_on_current_page(
    page: Page,
    *,
    works_id: str,
    contract_id: str,
    sor_code: str,
    sor_description: str,
    worker_description: str,
    timeout_ms: int = 30_000,
) -> dict[str, Any]:
    """
    Navigate directly to the Tasks tab using known works_id/contract_id
    (no re-search needed), then create both tasks back-to-back.

    Called from the combined endpoint after variation_automation closes JW Rates.
    """
    today = _today_dd_mm_yyyy()

    tasks_url = (
        f"{_WORKS_DETAILS_BASE}"
        f"?works_id={works_id}&contract_id={contract_id}&tab_nav_item=tasks"
    )
    log.info("task: navigating directly to Tasks tab — %s", tasks_url)
    await _goto_safe(page, tasks_url)

    qs_description = (
        f"Variation added: SOR {sor_code} — {sor_description}\n"
        f"Worker noted: \"{worker_description}\"\n"
        "Please review this variation before confirming handover."
    )

    # ── Task 1: QS Review ────────────────────────────────────────────────────
    log.info("task: creating QS review task")
    await _click_add_task(page, timeout_ms)
    await _select_category(page, "QS review for variations", timeout_ms)
    await _fill_description(page, qs_description)
    await _fill_issued_to(page, _QS_ISSUED_TO_QUERY, _QS_ISSUED_TO_LABEL, timeout_ms)
    await _select_priority(page, "Medium", timeout_ms)
    await _fill_due_date(page, today)
    await asyncio.sleep(0.3)
    await _save_task(page, timeout_ms)
    log.info("task: QS review task saved")

    return {
        "success": True,
        "works_id": works_id,
        "qs_task": {
            "category": "QS review for variations",
            "issued_to": _QS_ISSUED_TO_LABEL,
            "priority": "Medium",
            "due": today,
            "sor_code": sor_code,
        },
        "message": f"Created QS review task (SOR {sor_code}, due {today})",
    }


async def add_email_review_task(
    page: Page,
    *,
    works_id: str,
    contract_id: str,
    address: str,
    order_ref: str,
    email_subject: str,
    timeout_ms: int = 30_000,
) -> dict[str, Any]:
    """
    Navigate directly to the Tasks tab using known works_id/contract_id and
    create a single task notifying Shannon that the handover email draft is ready.

    Category : "Handover needed"
    Issued To: Shannon Slade
    Priority : High
    Due      : next working day
    """
    next_wd = _next_working_day_dd_mm_yyyy()

    description = (
        f"Handover email for {address} has been drafted and is ready in the "
        f"Outlook Drafts folder (BCCorders inbox).\n"
        f"Subject: {email_subject}\n"
        f"Please review the email and attachments before sending.\n"
        f"Once sent, please update the handover date within Works UDF."
    )

    tasks_url = (
        f"{_WORKS_DETAILS_BASE}"
        f"?works_id={works_id}&contract_id={contract_id}&tab_nav_item=tasks"
    )
    log.info("task: navigating to Tasks tab for email review task — %s", tasks_url)
    await _goto_safe(page, tasks_url)

    await _click_add_task(page, timeout_ms)
    await _select_category(page, "Handover needed", timeout_ms)
    await _fill_description(page, description)
    await _fill_issued_to(
        page, _HANDOVER_ISSUED_TO_QUERY, _HANDOVER_ISSUED_TO_LABEL, timeout_ms
    )
    await _select_priority(page, "High", timeout_ms)
    await _fill_due_date(page, next_wd)
    await asyncio.sleep(0.3)
    await _save_task(page, timeout_ms)
    log.info("task: email review task saved for works_id=%s, due %s", works_id, next_wd)

    return {
        "success": True,
        "works_id": works_id,
        "task": {
            "category": "Handover needed",
            "issued_to": _HANDOVER_ISSUED_TO_LABEL,
            "priority": "High",
            "due": next_wd,
            "description": description,
        },
        "message": (
            f"Email review task created for {address!r} — "
            f"Shannon Slade notified, due {next_wd}"
        ),
    }


async def add_variation_tasks(
    page: Page,
    *,
    address: str,
    sor_code: str,
    sor_description: str,
    worker_description: str,
    username: str,
    password: str,
    timeout_ms: int = 30_000,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> dict[str, Any]:
    """
    Navigate once to the job, then create both tasks back-to-back:

      1. QS review task  (Charlie Higginson, Medium, due today)
      2. Handover task   (Shannon Slade, High, due next working day)

    Returns a dict with success flag and details of both tasks.
    """
    log.info(
        "task: add_variation_tasks — address=%r sor_code=%s", address, sor_code
    )

    await _ensure_jobs_index(page, username, password, after_relogin)
    ids = await _navigate_to_job(page, address, timeout_ms)

    today = _today_dd_mm_yyyy()

    qs_description = (
        f"Variation added: SOR {sor_code} — {sor_description}\n"
        f"Worker noted: \"{worker_description}\"\n"
        "Please review this variation before confirming handover."
    )

    # ── Task 1: QS Review ────────────────────────────────────────────────────
    log.info("task: creating QS review task")
    await _open_tasks_tab(page, timeout_ms)
    await _click_add_task(page, timeout_ms)
    await _select_category(page, "QS review for variations", timeout_ms)
    await _fill_description(page, qs_description)
    await _fill_issued_to(page, _QS_ISSUED_TO_QUERY, _QS_ISSUED_TO_LABEL, timeout_ms)
    await _select_priority(page, "Medium", timeout_ms)
    await _fill_due_date(page, today)
    await asyncio.sleep(0.3)
    await _save_task(page, timeout_ms)
    log.info("task: QS review task saved")

    return {
        "success": True,
        "address": address,
        "works_id": ids["works_id"],
        "contract_id": ids["contract_id"],
        "qs_task": {
            "category": "QS review for variations",
            "issued_to": _QS_ISSUED_TO_LABEL,
            "priority": "Medium",
            "due": today,
            "sor_code": sor_code,
        },
        "message": (
            f"Created QS review task for {address!r}: "
            f"SOR {sor_code}, due {today}"
        ),
    }
