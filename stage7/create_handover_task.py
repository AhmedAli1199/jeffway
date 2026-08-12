"""
Create EasyBOP planned-works task: Handover needed → Shannon Slade.

Opens works_details.php?tab_nav_item=tasks by works_id, adds task with:
- Issued To: Shannon Slade: JWC - Administrator: VOIDS & RR
- Category: Handover needed (213)
- Priority: High
- Complete by: tomorrow (DD/MM/YYYY)
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, timedelta
from typing import Any, Awaitable, Callable, Dict, Optional

from playwright.async_api import Page

log = logging.getLogger("stage7.handover_task")

WORKS_TASKS_URL = (
    "https://easybop.co.uk/a_planned_works/z_works/works_details.php"
    "?works_id={works_id}&contract_id={contract_id}&tab_nav_item=tasks"
)

CATEGORY_VALUE = "213"
CATEGORY_LABEL = "Handover needed"
ISSUED_TO_QUERY = "Shannon"
ISSUED_TO_LABEL = "Shannon Slade: JWC - Administrator: VOIDS & RR"
PRIORITY_VALUE = "high"
PRIORITY_LABEL = "High"

_SHANNON_RE = re.compile(r"shannon", re.I)
_HANDOVER_CATEGORY_RE = re.compile(r"handover\s+needed", re.I)

_PAGE_STATE_JS = """() => {
  const lb = document.querySelector('#login_box');
  if (lb && lb.offsetParent !== null) return 'login';
  const add = document.querySelector("button[id^='btn_add_task']");
  if (add) return 'tasks';
  const tab = document.querySelector("[data-tab-nav-item='tasks']");
  if (tab) return 'tasks_tab';
  const h1 = document.querySelector('h1');
  const h1txt = ((h1 && h1.textContent) || '').toLowerCase();
  if (h1txt.includes('easybop') && h1txt.includes('login')) return 'login';
  const body = (document.body && document.body.innerText) || '';
  if (body.includes('EasyBOP Login')) return 'login';
  return null;
}"""


def tomorrow_dd_mm_yyyy() -> str:
    return (date.today() + timedelta(days=1)).strftime("%d/%m/%Y")


def default_description(*, corf: str = "", smart_form_name: str = "") -> str:
    parts = ["Handover needed — job completed on OJS; Ready To Hand over not set."]
    if corf:
        parts.append(f"CORF: {corf}.")
    if smart_form_name:
        parts.append(smart_form_name)
    return " ".join(parts)


async def _on_login_screen(page: Page) -> bool:
    try:
        lb = page.locator("#login_box")
        if await lb.count() > 0 and await lb.is_visible():
            return True
    except Exception:
        pass
    if "easybop login" in (await page.title() or "").lower():
        return True
    return False


async def _wait_for_page_state(page: Page, timeout_ms: int) -> Optional[str]:
    try:
        handle = await page.wait_for_function(_PAGE_STATE_JS, timeout=timeout_ms)
        return await handle.json_value()
    except Exception:
        return None


async def _ensure_tasks_page(
    page: Page,
    *,
    works_id: str,
    contract_id: str,
    username: str,
    password: str,
    login: Callable[[Page, str, str], Awaitable[bool]],
    timeout_ms: int,
) -> None:
    url = WORKS_TASKS_URL.format(works_id=works_id, contract_id=contract_id)
    for attempt in range(2):
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        state = await _wait_for_page_state(page, timeout_ms)
        if state == "login":
            log.info("handover task: login (works_id=%s attempt=%s)", works_id, attempt + 1)
            ok = await login(page, username, password)
            if not ok:
                raise RuntimeError("EasyBOP login failed")
            continue
        if state in ("tasks", "tasks_tab"):
            if state == "tasks_tab":
                tab = page.locator("[data-tab-nav-item='tasks']").first
                await tab.wait_for(state="visible", timeout=timeout_ms)
                await tab.click()
                await asyncio.sleep(0.4)
            add_btn = page.locator("button[id^='btn_add_task']").first
            await add_btn.wait_for(state="visible", timeout=timeout_ms)
            return
        if await _on_login_screen(page):
            ok = await login(page, username, password)
            if not ok:
                raise RuntimeError("EasyBOP login failed")
            continue
        break
    if await _on_login_screen(page):
        raise RuntimeError("Still on login after sign-in")
    add_btn = page.locator("button[id^='btn_add_task']").first
    if await add_btn.count() == 0:
        raise RuntimeError(f"Add Task button not found for works_id={works_id}")


async def _tasks_grid_has_handover_task(page: Page, timeout_ms: int) -> bool:
    """True when tasks grid already has Handover needed for Shannon."""
    table = page.locator("table[id^='list_tasks']")
    try:
        await table.first.wait_for(state="attached", timeout=min(8_000, timeout_ms))
    except Exception:
        return False

    await asyncio.sleep(0.4)
    rows = page.locator("table[id^='list_tasks'] tr.jqgrow")
    n = await rows.count()
    for i in range(n):
        row = rows.nth(i)
        try:
            row_text = (await row.inner_text() or "").strip()
            if _HANDOVER_CATEGORY_RE.search(row_text) and _SHANNON_RE.search(row_text):
                log.info("handover task: existing task found in grid")
                return True
        except Exception:
            continue
    return False


async def _fill_issued_to(page: Page, timeout_ms: int) -> None:
    field = page.locator("#issued_to_contact_name")
    await field.wait_for(state="visible", timeout=timeout_ms)
    await field.click()
    await field.fill("")
    await field.type(ISSUED_TO_QUERY, delay=60)
    ac_item = page.locator(".ui-autocomplete:visible .ui-menu-item").filter(
        has_text=ISSUED_TO_LABEL
    )
    await ac_item.first.wait_for(state="visible", timeout=10_000)
    await ac_item.first.click()
    log.info("handover task: Issued To set to %r", ISSUED_TO_LABEL)


async def _click_save_changes(page: Page, timeout_ms: int) -> None:
    save_btn = page.locator("#btn_dialog_save_changes")
    if await save_btn.count() == 0 or not await save_btn.first.is_visible():
        save_btn = page.locator(".ui-dialog:visible button").filter(has_text="Save Changes")
    await save_btn.first.wait_for(state="visible", timeout=timeout_ms)
    await save_btn.first.click()
    log.info("handover task: clicked Save Changes")
    try:
        await page.wait_for_selector(".ui-dialog:visible", state="hidden", timeout=10_000)
    except Exception:
        log.warning("handover task: dialog still visible after save")


async def _add_handover_task_in_dialog(
    page: Page,
    *,
    description: str,
    complete_by_date: str,
    timeout_ms: int,
) -> None:
    add_btn = page.locator("button[id^='btn_add_task']").first
    await add_btn.wait_for(state="visible", timeout=timeout_ms)
    await add_btn.click()
    log.info("handover task: clicked Add Task")

    dialog = page.locator(".ui-dialog:visible, [role='dialog']:visible")
    await dialog.first.wait_for(state="visible", timeout=8_000)

    category = page.locator("#category_id").first
    await category.wait_for(state="visible", timeout=timeout_ms)
    await category.select_option(value=CATEGORY_VALUE)
    log.info("handover task: category %r (value=%s)", CATEGORY_LABEL, CATEGORY_VALUE)

    desc = page.locator("#description").first
    await desc.wait_for(state="visible", timeout=timeout_ms)
    await desc.fill(description)

    await _fill_issued_to(page, timeout_ms)

    priority = page.locator("#priority").first
    await priority.wait_for(state="visible", timeout=timeout_ms)
    try:
        await priority.select_option(value=PRIORITY_VALUE)
    except Exception:
        await priority.select_option(label=PRIORITY_LABEL)
    log.info("handover task: priority High")

    date_field = page.locator("#date_for_completion").first
    await date_field.wait_for(state="visible", timeout=timeout_ms)
    await date_field.click()
    await date_field.fill(complete_by_date)
    await date_field.press("Tab")
    log.info("handover task: complete by %r", complete_by_date)

    await asyncio.sleep(0.4)
    await _click_save_changes(page, timeout_ms)


async def create_handover_task_on_page(
    page: Page,
    *,
    works_id: str,
    contract_id: str = "321129",
    description: str = "",
    complete_by_date: str = "",
    client_order_reference: str = "",
    smart_form_name: str = "",
    username: str = "",
    password: str = "",
    login: Callable[[Page, str, str], Awaitable[bool]],
    timeout_ms: int = 60_000,
    skip_if_exists: bool = True,
) -> Dict[str, Any]:
    """Create Handover needed task on one job's Tasks tab."""
    wid = str(works_id).strip()
    if not wid:
        raise ValueError("works_id is required")

    due = (complete_by_date or "").strip() or tomorrow_dd_mm_yyyy()
    desc = (description or "").strip() or default_description(
        corf=client_order_reference,
        smart_form_name=smart_form_name,
    )
    result: Dict[str, Any] = {
        "works_id": wid,
        "client_order_reference": client_order_reference or None,
        "smart_form_name": smart_form_name or None,
        "description": desc,
        "complete_by_date": due,
        "success": False,
        "already_exists": False,
        "url": WORKS_TASKS_URL.format(works_id=wid, contract_id=contract_id),
    }

    await _ensure_tasks_page(
        page,
        works_id=wid,
        contract_id=contract_id,
        username=username,
        password=password,
        login=login,
        timeout_ms=timeout_ms,
    )

    if skip_if_exists and await _tasks_grid_has_handover_task(page, timeout_ms):
        result["already_exists"] = True
        result["success"] = True
        result["message"] = "Handover task for Shannon already exists — skipped"
        return result

    await _add_handover_task_in_dialog(page, description=desc, complete_by_date=due, timeout_ms=timeout_ms)
    result["success"] = True
    result["message"] = (
        f"Task created (Handover needed → {ISSUED_TO_LABEL}, priority High, due {due})"
    )
    return result
