"""
EasyBOP Asbestos Task Automation.

For each address supplied:
  1. Navigate to the planned-works index (or reuse the already-loaded page).
  2. Type the address into #search_dwelling and select the autocomplete suggestion.
  3. Wait for the work-order detail page to finish loading.
  4. Click the "Tasks" tab (data-tab-nav-item="tasks").
  5. Click "Add Task" (#btn_add_task_5).
  6. In the popup: select task type "Asbestos survey needed" (value 205).
  7. Fill in the description textarea (#description).
  8. Issued To: type "Cybix" and pick "Cybix AI: Demo" from autocomplete.
  9. Set priority to Medium.
 10. Fill Complete By Date (#date_for_completion) with today's date (DD/MM/YYYY).
 11. Click Save Changes to create the task.
 12. Move on to the next address.

Returns a list of result dicts per address:
  { address, success, message, tasks_count_before, tasks_count_after }
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date
from typing import Any, Awaitable, Callable, List, Optional

from playwright.async_api import Page

log = logging.getLogger("asbestos.task")

LOGIN_URL = "https://easybop.co.uk/"
INDEX_URL = "https://easybop.co.uk/a_planned_works/z_works/index.php?contract_id={contract_id}"
WORKS_TASKS_URL = (
    "https://easybop.co.uk/a_planned_works/z_works/works_details.php"
    "?works_id={works_id}&contract_id={contract_id}&tab_nav_item=tasks"
)

# Task type "Asbestos survey needed"
TASK_TYPE_VALUE = "205"
TASK_TYPE_LABEL = "Asbestos survey needed"

ISSUED_TO_QUERY = "Cybix"
ISSUED_TO_LABEL = "Cybix AI: Demo"
TASK_PRIORITY_LABEL = "Medium"
_CYBIX_ISSUED_TO = re.compile(r"cybix", re.I)


def _browser_closed(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return (
        "target page, context or browser has been closed" in msg
        or "browser has been closed" in msg
        or "target closed" in msg
    )


def _task_result(address: str, works_id: str = "") -> dict[str, Any]:
    return {
        "address": address,
        "works_id": works_id or None,
        "success": False,
        "already_created": False,
        "message": "",
    }


def _normalize_work_items(
    addresses: Optional[List[str]] = None,
    items: Optional[List[dict]] = None,
) -> List[dict[str, str]]:
    """Build [{works_id, address}] from legacy addresses or new items list."""
    out: List[dict[str, str]] = []
    if items:
        for raw in items:
            if not isinstance(raw, dict):
                continue
            wid = str(raw.get("works_id") or "").strip()
            addr = str(raw.get("address") or "").strip()
            if wid or addr:
                out.append({"works_id": wid, "address": addr})
        return out
    for addr in addresses or []:
        a = str(addr or "").strip()
        if a:
            out.append({"works_id": "", "address": a})
    return out


def _finish_partial_batch(
    results: List[dict],
    addresses: List[str],
    msg: str,
) -> List[dict]:
    """Append error rows for unprocessed addresses and return partial batch."""
    while len(results) < len(addresses):
        addr = str(addresses[len(results)]).strip()
        r = _task_result(addr or str(addresses[len(results)]))
        r["message"] = msg
        results.append(r)
    return results


def _today_dd_mm_yyyy() -> str:
    return date.today().strftime("%d/%m/%Y")


# ---------------------------------------------------------------------------
# Login helpers (consistent with existing asbestos automation pattern)
# ---------------------------------------------------------------------------

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
# Ensure the index page with #search_dwelling is ready
# ---------------------------------------------------------------------------

async def _ensure_index_ready(
    page: Page,
    contract_id: str,
    username: Optional[str],
    password: Optional[str],
    timeout_ms: int,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> None:
    url = INDEX_URL.format(contract_id=contract_id)
    log.info("task: navigate -> %s", url)
    await page.goto(url, wait_until="domcontentloaded")

    try:
        await page.wait_for_selector("#search_dwelling", state="visible", timeout=timeout_ms)
        if not await _page_looks_like_login(page):
            log.info("task: index ready (search_dwelling visible)")
            return
    except Exception as e:
        log.warning("task: search_dwelling not ready: %s", e)

    if await _page_looks_like_login(page):
        if not username or not password:
            raise RuntimeError("Login required but credentials not set in .env")
        log.info("task: session expired — re-logging in")
        if not await login(page, username, password):
            raise RuntimeError("EasyBOP re-login failed")
        if after_relogin:
            await after_relogin()
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_selector("#search_dwelling", state="visible", timeout=timeout_ms)
        if await _page_looks_like_login(page):
            raise RuntimeError("Still on login wall after re-login — check credentials")


# ---------------------------------------------------------------------------
# Navigate back to index (between addresses) without full re-navigate if possible
# ---------------------------------------------------------------------------

async def _return_to_index(
    page: Page,
    contract_id: str,
    timeout_ms: int,
) -> None:
    """Go back to the index so we can search the next address."""
    url = INDEX_URL.format(contract_id=contract_id)
    current = (page.url or "").split("?")[0].rstrip("/")
    index_base = url.split("?")[0].rstrip("/")

    if current == index_base:
        try:
            await page.wait_for_selector("#search_dwelling", state="visible", timeout=5_000)
            return
        except Exception:
            pass

    log.info("task: returning to index -> %s", url)
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_selector("#search_dwelling", state="visible", timeout=timeout_ms)


# ---------------------------------------------------------------------------
# Search for an address and navigate into the work order
# ---------------------------------------------------------------------------

async def _navigate_to_works_tasks_by_id(
    page: Page,
    works_id: str,
    contract_id: str,
    timeout_ms: int,
    *,
    username: Optional[str] = None,
    password: Optional[str] = None,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> None:
    wid = str(works_id or "").strip()
    if not wid:
        raise ValueError("works_id is required for direct tasks navigation")
    url = WORKS_TASKS_URL.format(works_id=wid, contract_id=contract_id)
    log.info("task: navigate direct -> %s", url)
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        await page.wait_for_load_state("networkidle", timeout=min(20_000, timeout_ms))
    except Exception:
        pass
    if await _page_looks_like_login(page):
        if not username or not password:
            raise RuntimeError("Login required but credentials not set in .env")
        if not await login(page, username, password):
            raise RuntimeError("EasyBOP re-login failed")
        if after_relogin:
            await after_relogin()
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    if await _page_looks_like_login(page):
        raise RuntimeError("Still on login page after re-login")
    await page.wait_for_selector(
        "[data-tab-nav-item='tasks']", state="visible", timeout=timeout_ms
    )
    log.info("task: works_id=%s tasks tab ready", wid)
    await asyncio.sleep(0.5)


async def _search_and_open_work_order(
    page: Page,
    address: str,
    timeout_ms: int,
) -> None:
    """
    Type the address into #search_dwelling (index) or #quick_search_address (detail),
    pick the first autocomplete suggestion, and wait for the work-order detail to load.
    """
    log.info("task: searching for address: %r", address)

    search_dwelling = page.locator("#search_dwelling")
    quick_search = page.locator("#quick_search_address")

    is_quick_search = False
    if await quick_search.count() > 0 and await quick_search.is_visible():
        field = quick_search
        is_quick_search = True
        log.info("task: using #quick_search_address (on detail page)")
    else:
        field = search_dwelling
        await field.wait_for(state="visible", timeout=timeout_ms)
        log.info("task: using #search_dwelling (on index page)")

    await field.click()
    await field.fill("")
    await field.type(address, delay=60)

    if is_quick_search:
        try:
            row = page.locator("tr.wi_row:visible")
            await row.first.wait_for(state="visible", timeout=10_000)
            log.info("task: found quick search row result, clicking it")
            await row.first.click()
        except Exception as e:
            log.warning("task: quick search row not clicked — trying Enter: %s", e)
            await field.press("Enter")
    else:
        try:
            ac = page.locator(".ui-autocomplete:visible li.ui-menu-item")
            await ac.first.wait_for(state="visible", timeout=10_000)
            first_suggestion = ac.first
            suggestion_text = await first_suggestion.inner_text()
            log.info("task: autocomplete first suggestion: %r", suggestion_text.strip())
            await first_suggestion.click()
        except Exception as e:
            log.warning("task: autocomplete suggestion not clicked — trying Enter: %s", e)
            await field.press("Enter")

    # Wait for the detail page to load — the Tasks tab is a reliable indicator
    try:
        await page.wait_for_selector(
            "[data-tab-nav-item='tasks']", state="visible", timeout=timeout_ms
        )
        log.info("task: work-order detail loaded (Tasks tab visible)")
    except Exception as e:
        raise RuntimeError(
            f"Work-order detail did not load for address {address!r} — Tasks tab not found: {e}"
        ) from e

    # Brief settle for dynamic tab counters to update
    await asyncio.sleep(0.8)


# ---------------------------------------------------------------------------
# Read the current tasks count from the tab counter badge
# ---------------------------------------------------------------------------

async def _get_tasks_count(page: Page) -> Optional[int]:
    try:
        counter = page.locator("#tab_nav_item_tasks_counter")
        if await counter.count() > 0:
            txt = (await counter.first.inner_text() or "").strip()
            return int(txt) if txt.isdigit() else None
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Open the Tasks tab
# ---------------------------------------------------------------------------

async def _click_tasks_tab(page: Page, timeout_ms: int) -> None:
    tab = page.locator("[data-tab-nav-item='tasks']")
    try:
        await tab.first.wait_for(state="visible", timeout=timeout_ms)
        await tab.first.click()
        log.info("task: Tasks tab clicked")
    except Exception as e:
        raise RuntimeError(f"Could not click Tasks tab: {e}") from e

    await asyncio.sleep(0.5)


async def _tasks_grid_has_cybix_task(page: Page, timeout_ms: int) -> bool:
    """True when the tasks grid already has a row with Issued To = Cybix AI."""
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
        issued_to = row.locator("[aria-describedby*='issued_to_contact_name']")
        try:
            if await issued_to.count() == 0:
                continue
            text = (await issued_to.first.inner_text() or "").strip()
            if _CYBIX_ISSUED_TO.search(text):
                log.info("task: existing Cybix task found in grid (Issued To=%r)", text)
                return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# Task dialog helpers
# ---------------------------------------------------------------------------

async def _fill_issued_to(
    page: Page,
    query: str,
    option_label: str,
    timeout_ms: int,
) -> None:
    field = page.locator("#issued_to_contact_name")
    try:
        await field.wait_for(state="visible", timeout=timeout_ms)
        await field.click()
        await field.fill("")
        await field.type(query, delay=60)
        log.info("task: typed Issued To query %r", query)

        ac_item = page.locator(".ui-autocomplete:visible .ui-menu-item").filter(
            has_text=option_label
        )
        await ac_item.first.wait_for(state="visible", timeout=10_000)
        await ac_item.first.click()
        log.info("task: selected Issued To %r", option_label)
    except Exception as e:
        raise RuntimeError(
            f"Could not set Issued To ({query!r} -> {option_label!r}): {e}"
        ) from e


async def _select_priority_medium(page: Page, timeout_ms: int) -> None:
    selectors = [
        "#priority",
        "#task_priority",
        "select[name='priority']",
        "select[name='task_priority']",
    ]
    for sel in selectors:
        loc = page.locator(sel)
        if await loc.count() == 0:
            continue
        try:
            await loc.first.wait_for(state="visible", timeout=3_000)
            await loc.first.select_option(label=TASK_PRIORITY_LABEL)
            log.info("task: priority set to %r via %s", TASK_PRIORITY_LABEL, sel)
            return
        except Exception:
            try:
                await loc.first.select_option(value=TASK_PRIORITY_LABEL)
                log.info("task: priority set to %r (value) via %s", TASK_PRIORITY_LABEL, sel)
                return
            except Exception:
                continue

    priority_select = page.locator(".ui-dialog:visible select").filter(
        has=page.locator("option", has_text=TASK_PRIORITY_LABEL)
    )
    try:
        await priority_select.first.wait_for(state="visible", timeout=timeout_ms)
        await priority_select.first.select_option(label=TASK_PRIORITY_LABEL)
        log.info("task: priority set to %r (dialog select fallback)", TASK_PRIORITY_LABEL)
    except Exception as e:
        raise RuntimeError(f"Could not select priority {TASK_PRIORITY_LABEL!r}: {e}") from e


async def _click_save_changes(page: Page, timeout_ms: int) -> None:
    save_btn = page.locator("#btn_dialog_save_changes")
    if await save_btn.count() == 0 or not await save_btn.first.is_visible():
        save_btn = page.locator(".ui-dialog:visible button").filter(has_text="Save Changes")
    try:
        await save_btn.first.wait_for(state="visible", timeout=timeout_ms)
        await save_btn.first.click()
        log.info("task: clicked Save Changes")
    except Exception as e:
        raise RuntimeError(f"Could not click Save Changes: {e}") from e

    try:
        await page.wait_for_selector(".ui-dialog:visible", state="hidden", timeout=10_000)
        log.info("task: dialog closed after save")
    except Exception:
        log.warning("task: dialog still visible after save — proceeding anyway")


# ---------------------------------------------------------------------------
# Click "Add Task", fill the form, and save
# ---------------------------------------------------------------------------

async def _add_task_in_dialog(
    page: Page,
    description: str,
    complete_by_date: str,
    timeout_ms: int,
) -> None:
    """
    Click Add Task, select 'Asbestos survey needed' (205), fill description,
    Issued To, priority, complete-by date, then Save Changes.
    """
    if not complete_by_date.strip():
        complete_by_date = _today_dd_mm_yyyy()
    # Click "Add Task" — the button id ends with _5 (works for contract 5)
    # We use a broader selector in case the contract suffix differs
    add_btn = page.locator("button[id^='btn_add_task']")
    try:
        await add_btn.first.wait_for(state="visible", timeout=timeout_ms)
        await add_btn.first.click()
        log.info("task: clicked 'Add Task'")
    except Exception as e:
        raise RuntimeError(f"Could not click Add Task button: {e}") from e

    # Wait for the dialog/form to appear
    dialog = page.locator(".ui-dialog:visible, [role='dialog']:visible")
    try:
        await dialog.first.wait_for(state="visible", timeout=8_000)
        log.info("task: Add Task dialog visible")
    except Exception as e:
        raise RuntimeError(f"Add Task dialog did not appear: {e}") from e

    # Select task type: "Asbestos survey needed" (value="205")
    # The select is usually the first <select> inside the task form
    type_select = page.locator("select").filter(has=page.locator(f"option[value='{TASK_TYPE_VALUE}']"))
    try:
        await type_select.first.wait_for(state="visible", timeout=5_000)
        await type_select.first.select_option(value=TASK_TYPE_VALUE)
        log.info("task: selected task type '%s' (value=%s)", TASK_TYPE_LABEL, TASK_TYPE_VALUE)
    except Exception as e:
        raise RuntimeError(f"Could not select task type: {e}") from e

    # Fill in the description textarea (#description)
    desc_area = page.locator("#description")
    try:
        await desc_area.wait_for(state="visible", timeout=5_000)
        await desc_area.fill(description)
        log.info("task: description filled")
    except Exception as e:
        raise RuntimeError(f"Could not fill description textarea: {e}") from e

    await _fill_issued_to(page, ISSUED_TO_QUERY, ISSUED_TO_LABEL, timeout_ms)
    await _select_priority_medium(page, timeout_ms)

    # Fill in the Complete By Date (#date_for_completion)
    date_field = page.locator("#date_for_completion")
    try:
        await date_field.wait_for(state="visible", timeout=5_000)
        await date_field.click()
        await date_field.fill(complete_by_date)
        await date_field.press("Tab")
        log.info("task: date_for_completion filled with %r", complete_by_date)
    except Exception as e:
        raise RuntimeError(f"Could not fill date_for_completion: {e}") from e

    await asyncio.sleep(0.5)
    await _click_save_changes(page, timeout_ms)


# ---------------------------------------------------------------------------
# Main public function: add tasks for a list of addresses
# ---------------------------------------------------------------------------

async def add_asbestos_tasks(
    page: Page,
    addresses: Optional[List[str]] = None,
    items: Optional[List[dict]] = None,
    contract_id: str = "",
    description: str = "Asbestos survey required before works commence.",
    complete_by_date: str = "",
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout_ms: int = 30_000,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
    use_works_id_navigation: bool = False,
) -> List[dict]:
    """
    For each work item (works_id and/or address):
      - Navigate via works_details.php?tab_nav_item=tasks when works_id is set,
        otherwise search by address on the planned-works index.
      - Open the Add Task dialog, select "Asbestos survey needed" (205), fill form, save.

    Returns a list of result dicts, one per item.
    """
    work_items = _normalize_work_items(addresses, items)
    results: List[dict] = []
    batch_uses_works_id = use_works_id_navigation or any(
        str(it.get("works_id") or "").strip() for it in work_items
    )

    if not batch_uses_works_id:
        try:
            await _ensure_index_ready(
                page, contract_id, username, password, timeout_ms, after_relogin
            )
        except Exception as exc:
            err = str(exc)
            log.exception("task: could not load index — aborting batch")
            return [
                {**_task_result(it["address"], it.get("works_id", "")), "message": err}
                for it in work_items
            ]

    try:
        for i, item in enumerate(work_items):
            address = str(item.get("address") or "").strip()
            works_id = str(item.get("works_id") or "").strip()
            label = works_id or address
            if not label:
                r = _task_result(address, works_id)
                r["message"] = "Empty works_id/address — skipped"
                results.append(r)
                continue

            log.info(
                "task: processing %d/%d: works_id=%r address=%r",
                i + 1,
                len(work_items),
                works_id or None,
                address or None,
            )
            result = _task_result(address, works_id)

            try:
                if works_id:
                    await _navigate_to_works_tasks_by_id(
                        page,
                        works_id,
                        contract_id,
                        timeout_ms,
                        username=username,
                        password=password,
                        after_relogin=after_relogin,
                    )
                else:
                    if i > 0:
                        quick_search = page.locator("#quick_search_address")
                        if await quick_search.count() == 0 or not await quick_search.is_visible():
                            await _return_to_index(page, contract_id, timeout_ms)
                    await _search_and_open_work_order(page, address, timeout_ms)
                    await _click_tasks_tab(page, timeout_ms)

                tasks_before = await _get_tasks_count(page)
                result["tasks_count_before"] = tasks_before

                if await _tasks_grid_has_cybix_task(page, timeout_ms):
                    result["already_created"] = True
                    result["success"] = True
                    result["message"] = (
                        f"Task already exists (Issued To: Cybix AI) for "
                        f"works_id={works_id!r} address={address!r} — skipping"
                    )
                    log.info("task: SKIP works_id=%r — Cybix task already on grid", works_id or address)
                    results.append(result)
                    continue

                task_complete_by = complete_by_date.strip() or _today_dd_mm_yyyy()

                # Open Add Task dialog, fill, and save
                await _add_task_in_dialog(page, description, task_complete_by, timeout_ms)

                await asyncio.sleep(0.8)
                tasks_after = await _get_tasks_count(page)
                result["tasks_count_after"] = tasks_after

                result["success"] = True
                result["message"] = (
                    f"Task saved (Issued To: {ISSUED_TO_LABEL}, priority: {TASK_PRIORITY_LABEL}, "
                    f"due: {task_complete_by}). "
                    f"Tasks count: {tasks_before} -> {tasks_after}"
                )
                log.info("task: SUCCESS for %r — %s", address, result["message"])

            except Exception as exc:
                result["success"] = False
                result["message"] = str(exc)
                log.exception("task: FAILED works_id=%r address=%r: %s", works_id, address, exc)
                results.append(result)

                if _browser_closed(exc):
                    log.error(
                        "task: browser closed after %d/%d — returning partial results",
                        len(results),
                        len(work_items),
                    )
                    return _finish_partial_batch(
                        results,
                        [it.get("address") or it.get("works_id") or "" for it in work_items],
                        "Browser closed — not processed",
                    )

                if not batch_uses_works_id:
                    try:
                        url = INDEX_URL.format(contract_id=contract_id)
                        await page.goto(url, wait_until="domcontentloaded")
                        await page.wait_for_selector("#search_dwelling", state="visible", timeout=10_000)
                    except Exception as nav_exc:
                        log.warning("task: recovery navigation failed: %s", nav_exc)
                        if _browser_closed(nav_exc):
                            return _finish_partial_batch(
                                results,
                                [it.get("address") or it.get("works_id") or "" for it in work_items],
                                "Browser closed — not processed",
                            )
                continue

            results.append(result)
    except Exception as exc:
        log.exception("task: batch aborted after %d/%d item(s)", len(results), len(work_items))
        addr_list = [it.get("address") or it.get("works_id") or "" for it in work_items]
        if _browser_closed(exc):
            return _finish_partial_batch(results, addr_list, "Browser closed — not processed")
        return _finish_partial_batch(results, addr_list, f"Batch aborted: {exc}")

    return results
