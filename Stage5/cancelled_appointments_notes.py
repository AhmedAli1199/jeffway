"""
Stage 5 — Process Cancelled Appointments Notes Automation
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Dialog, Page

log = logging.getLogger("stage5.cancelled_notes")

_DIALOG_HANDLER_ATTR = "_easybop_dialog_accept_wired"

_PAGE_STATE_JS = """() => {
  const lb = document.querySelector('#login_box');
  if (lb && lb.offsetParent !== null) return 'login';
  if (document.querySelector('label[for="cancelled_2"]')) return 'index_page';
  const h1 = document.querySelector('h1');
  const h1txt = ((h1 && h1.textContent) || '').toLowerCase();
  if (h1txt.includes('easybop') && h1txt.includes('login')) return 'login';
  const body = (document.body && document.body.innerText) || '';
  if (body.includes('EasyBOP Login')) return 'login';
  return null;
}"""


def _ensure_browser_dialog_auto_accept(page: Page) -> None:
    """Register persistent handler for window.confirm() — click OK."""
    if getattr(page, _DIALOG_HANDLER_ATTR, False):
        return

    async def _accept_native_dialog(dialog: Dialog) -> None:
        log.info('cancelled_notes: native browser dialog "%s" — clicking OK', dialog.message)
        await dialog.accept()

    page.on("dialog", _accept_native_dialog)
    setattr(page, _DIALOG_HANDLER_ATTR, True)
    log.info("cancelled_notes: registered browser dialog auto-accept handler")


async def _navigate(page: Page, url: str, timeout_ms: int) -> None:
    log.info("cancelled_notes: navigating to %s", url)
    try:
        await page.goto(url, wait_until="commit", timeout=timeout_ms)
    except Exception as exc:
        log.warning("cancelled_notes: commit nav issue (%s), retry load", exc)
        try:
            await page.goto(url, wait_until="load", timeout=timeout_ms)
        except Exception as exc2:
            log.warning("cancelled_notes: load nav issue (%s)", exc2)


async def _ensure_authenticated(
    page: Page,
    *,
    context: BrowserContext,
    url: str,
    login: Callable[[Page, str, str], Awaitable[bool]],
    username: str,
    password: str,
    session_path: Path,
    timeout_ms: int,
) -> None:
    for attempt in range(2):
        await _navigate(page, url, timeout_ms)
        try:
            handle = await page.wait_for_function(_PAGE_STATE_JS, timeout=timeout_ms)
            state = await handle.json_value()
        except Exception as exc:
            log.warning("cancelled_notes: wait for page state timed out (%s)", exc)
            state = None

        log.info("cancelled_notes: state=%r url=%s", state, page.url)

        if state == "index_page":
            return
        if state == "login":
            log.info("cancelled_notes: login required (attempt %s)", attempt + 1)
            ok = await login(page, username, password)
            if not ok:
                raise RuntimeError("EasyBOP login failed")
            await context.storage_state(path=str(session_path))
            continue
        # Fallback check if it looks like the index page even if the label wasn't ready
        if "index.php" in page.url:
            return
        raise RuntimeError(f"Unexpected page state: {state!r}")


async def wait_for_grid_to_update_to_cancelled(page: Page, timeout_ms: int = 20_000) -> None:
    """Wait until all rows in the jqGrid have status containing 'cancelled' (or grid is empty)."""
    # Sleep 2.0 seconds first to allow the AJAX request to initiate and clear the grid.
    await asyncio.sleep(2.0)
    try:
        await page.wait_for_function(
            """() => {
                const loadEl = document.getElementById('load_list');
                const loading = loadEl && loadEl.offsetParent !== null && loadEl.style.display !== 'none';
                if (loading) return false;

                const rows = document.querySelectorAll('tr.jqgrow');
                if (rows.length === 0) {
                    // Check if the paging info is loaded and confirms no records
                    const info = document.querySelector('.ui-paging-info');
                    const infoText = info ? info.innerText.toLowerCase() : '';
                    if (infoText.includes('no records') || infoText.includes('0 of 0') || infoText.includes('0 - 0')) {
                        return true;
                    }
                    // Fallback to true if paging info is not found or empty
                    return !info;
                }

                for (const row of rows) {
                    const statusCell = row.querySelector('td[aria-describedby="list_works_status"]');
                    const status = statusCell ? (statusCell.getAttribute('title') || statusCell.innerText || '').toLowerCase() : '';
                    if (status && !status.includes('cancelled')) {
                        return false;
                    }
                }
                return true;
            }""",
            timeout=timeout_ms,
        )
    except Exception as e:
        log.warning("cancelled_notes: waiting for grid update to Cancelled timed out or failed: %s", e)
    await asyncio.sleep(1.0)


async def _select_task_category(page: Page, label: str, timeout_ms: int) -> None:
    cat = page.locator("#category_id")
    await cat.first.wait_for(state="visible", timeout=timeout_ms)
    try:
        await cat.first.select_option(label=label)
        log.info("task: category set to %r (exact)", label)
        return
    except Exception:
        pass
    
    options = await cat.first.locator("option").all()
    label_lower = label.lower()
    for opt in options:
        text = (await opt.inner_text()).strip()
        if label_lower in text.lower():
            value = await opt.get_attribute("value")
            await cat.first.select_option(value=value)
            log.info("task: category set to %r (partial match for %r)", text, label)
            return
    log.warning("task: category %r not found, selecting first option", label)


async def _select_task_priority(page: Page, label: str, timeout_ms: int) -> None:
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
            log.info("task: priority set to %r", label)
            return
        except Exception:
            continue
    log.warning("task: priority select element not found")


async def _fill_task_issued_to(
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


async def _fill_task_due_date(page: Page, date_str: str) -> None:
    field = page.locator("#date_for_completion")
    await field.wait_for(state="visible", timeout=5_000)
    await field.click()
    await field.fill(date_str)
    await field.press("Tab")
    log.info("task: due date set to %r", date_str)


async def _save_task_changes(page: Page, timeout_ms: int) -> None:
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
        pass


async def run_cancelled_appointments_notes(
    page: Page,
    *,
    contract_id: str,
    username: str,
    password: str,
    timeout_ms: int,
    login_fn: Callable[[Page, str, str], Awaitable[bool]],
    session_path: Path,
    context: BrowserContext,
) -> Dict[str, Any]:
    """
    Core routine to find all cancelled appointments for the given contract,
    navigate to each job's notes tab, add a No access note with timestamp,
    and optionally create a task if it's a plastering job with a next appointment.
    """
    index_url = f"https://easybop.co.uk/a_planned_works/z_works/index.php?contract_id={contract_id}"

    # 1. Authenticate & Navigate to Index
    await _ensure_authenticated(
        page,
        context=context,
        url=index_url,
        login=login_fn,
        username=username,
        password=password,
        session_path=session_path,
        timeout_ms=timeout_ms,
    )

    # 2. Click "Cancelled" status checkbox/label
    log.info("cancelled_notes: checking the Cancelled button/label")
    try:
        await page.locator('#cancelled_2').check(force=True, timeout=5000)
    except Exception:
        try:
            await page.locator('label[for="cancelled_2"]').click(timeout=5000)
        except Exception:
            pass

    # Dispatch events via JS to make sure jQuery UI updates the grid
    await page.evaluate(
        """() => {
            const input = document.getElementById('cancelled_2');
            if (input) {
                input.checked = true;
                input.dispatchEvent(new Event('change', { bubbles: true }));
                input.dispatchEvent(new Event('click', { bubbles: true }));
            }
        }"""
    )

    # 3. Wait for the network to idle and the grid to update
    try:
        await page.wait_for_load_state("networkidle", timeout=3000)
    except Exception:
        pass
    await wait_for_grid_to_update_to_cancelled(page, timeout_ms)

    # 4. Extract all jobs information from tr.jqgrow rows
    jobs_info = await page.evaluate(
        """() => {
            const rows = document.querySelectorAll('tr.jqgrow');
            return Array.from(rows).map(row => {
                const descCell = row.querySelector('td[aria-describedby="list_description_of_works"]');
                const apptDateCell = row.querySelector('td[aria-describedby="list_appointment_date_formatted"]');
                const udf5088Cell = row.querySelector('td[aria-describedby="list_udf_5088"]');
                
                const description = descCell ? (descCell.getAttribute('title') || descCell.innerText || '').trim() : '';
                const apptDate = apptDateCell ? (apptDateCell.getAttribute('title') || apptDateCell.innerText || '').trim() : '';
                const udf5088 = udf5088Cell ? (udf5088Cell.getAttribute('title') || udf5088Cell.innerText || '').trim() : '';
                
                return {
                    works_id: row.id,
                    description,
                    appt_date: apptDate,
                    udf_5088: udf5088
                };
            }).filter(item => !!item.works_id);
        }"""
    )
    log.info("cancelled_notes: found %d jobs on index page", len(jobs_info))

    if not jobs_info:
        return {
            "success": True,
            "processed_count": 0,
            "processed_jobs": [],
            "notes_added": [],
            "tasks_created": [],
            "errors": [],
            "message": "No cancelled appointments found for this contract."
        }

    notes_added = []
    tasks_created = []
    skipped_jobs = []
    errors = []

    # 5. Process each job
    for job in jobs_info:
        works_id = job["works_id"]
        description = job["description"]
        appt_date = job["appt_date"]
        udf_5088 = job["udf_5088"]
        
        # Check description: contains "Plaster", "Plastr", "plastering"
        is_plaster = bool(re.search(r'plaster|plastr', description, re.IGNORECASE))
        
        # Check next appointment: td has a date
        date_pattern = r'\d+/\d+/\d+|\d+-\d+-\d+'
        has_appt_date = bool(re.search(date_pattern, appt_date)) or bool(re.search(date_pattern, udf_5088))
        
        should_add_task = is_plaster and has_appt_date
        
        log.info(
            "cancelled_notes: works_id=%s, is_plaster=%s, has_appt_date=%s -> should_add_task=%s",
            works_id, is_plaster, has_appt_date, should_add_task
        )
        
        try:
            log.info("cancelled_notes: processing works_id=%s", works_id)
            # Generate dynamic note timestamp for this job
            current_dt_str = datetime.now().strftime("%d/%m/%Y %H:%M")
            note_text = f"No access — {current_dt_str}"
            
            # Construct the notes tab URL directly for direct navigation
            notes_url = f"https://easybop.co.uk/a_planned_works/z_works/works_details.php?works_id={works_id}&contract_id={contract_id}&tab_nav_item=notes"
            await page.goto(notes_url, timeout=timeout_ms)
            
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            except Exception:
                pass

            # Wait for grid loading
            await asyncio.sleep(1.0)

            # Check if a "No Access" category note already exists in the table
            has_no_access_note = await page.evaluate(
                """() => {
                    const rows = document.querySelectorAll('table[id="list"] tr.jqgrow');
                    for (const row of rows) {
                        const catCell = row.querySelector('td[aria-describedby="list_cat.category_name"]');
                        const catText = catCell ? (catCell.getAttribute('title') || catCell.innerText || '').toLowerCase() : '';
                        if (catText.includes('no access')) {
                            return true;
                        }
                    }
                    return false;
                }"""
            )
            
            if has_no_access_note:
                log.info("cancelled_notes: job works_id=%s already has a 'No Access' note, skipping", works_id)
                skipped_jobs.append(works_id)
                continue

            # Click "Add New Note" button
            add_note_btn = page.locator("#btn_add_note_dialog")
            await add_note_btn.wait_for(state="visible", timeout=10_000)
            await add_note_btn.click()
            log.info("cancelled_notes: clicked 'Add New Note'")

            # Wait for the popup dialog
            dialog = page.locator(".ui-dialog:visible, [role='dialog']:visible")
            await dialog.first.wait_for(state="visible", timeout=8_000)
            await asyncio.sleep(0.5)

            # Dynamically identify and select the "No access" category
            category_select = page.locator("#category_id")
            await category_select.wait_for(state="visible", timeout=timeout_ms)
            
            option_val = await page.evaluate(
                """() => {
                    const select = document.getElementById('category_id');
                    if (!select) return null;
                    for (const opt of select.options) {
                        if (opt.textContent.toLowerCase().includes('no access')) {
                            return opt.value;
                        }
                    }
                    return null;
                }"""
            )
            if option_val:
                await category_select.select_option(value=option_val)
                log.info("cancelled_notes: selected category 'No access' (value=%s)", option_val)
            else:
                await category_select.select_option(label="No access")
                log.info("cancelled_notes: selected category 'No access' via label fallback")

            # Fill description: "No access — [date] [time]"
            textarea = page.locator("#notes")
            await textarea.wait_for(state="visible", timeout=timeout_ms)
            await textarea.fill(note_text)
            log.info("cancelled_notes: filled note description: %r", note_text)

            # Click Save Changes inside dialog & Accept confirm alert
            _ensure_browser_dialog_auto_accept(page)
            save_btn = page.locator("#btn_save_changes")
            await save_btn.wait_for(state="visible", timeout=timeout_ms)
            await save_btn.click()
            log.info("cancelled_notes: clicked Save Changes button")

            # Wait for save completion & dismiss note dialog via Escape
            await asyncio.sleep(2.0)
            await page.keyboard.press("Escape")
            try:
                await page.wait_for_selector(".ui-dialog:visible", state="hidden", timeout=5_000)
            except Exception:
                pass
            await asyncio.sleep(0.5)

            notes_added.append({
                "works_id": works_id,
                "note_text": note_text,
                "success": True
            })
            log.info("cancelled_notes: successfully added note for works_id=%s", works_id)
            
            if should_add_task:
                # Navigate to Tasks tab URL
                tasks_url = f"https://easybop.co.uk/a_planned_works/z_works/works_details.php?works_id={works_id}&contract_id={contract_id}&tab_nav_item=tasks"
                log.info("cancelled_notes: navigating to tasks tab %s", tasks_url)
                await page.goto(tasks_url, timeout=timeout_ms)
                
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
                except Exception:
                    pass
                    
                # Click "Add Task" button
                add_task_btn = page.locator("button[id^='btn_add_task']").first
                await add_task_btn.wait_for(state="visible", timeout=timeout_ms)
                await add_task_btn.click()
                log.info("cancelled_notes: clicked 'Add Task'")
                
                # Wait for dialog
                dialog = page.locator(".ui-dialog:visible, [role='dialog']:visible")
                await dialog.first.wait_for(state="visible", timeout=8_000)
                await asyncio.sleep(0.5)
                
                # Select category
                await _select_task_category(page, "Next appointment needs updating", timeout_ms)
                
                # Fill description
                task_desc = f"Plastering job cancelled. Next appointment needs updating.\nOriginal job description: {description}"
                desc_field = page.locator("#description").first
                await desc_field.fill(task_desc)
                
                # Fill Issued To, Priority, Due Date (tomorrow)
                await _fill_task_issued_to(page, "Charlie", "Charlie Higginson", timeout_ms)
                await _select_task_priority(page, "High", timeout_ms)
                
                tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%d/%m/%Y")
                await _fill_task_due_date(page, tomorrow_str)
                
                await asyncio.sleep(0.4)
                await _save_task_changes(page, timeout_ms)
                
                tasks_created.append({
                    "works_id": works_id,
                    "issued_to": "Charlie Higginson",
                    "priority": "High",
                    "due_date": tomorrow_str,
                    "success": True
                })
                log.info("cancelled_notes: task created successfully for works_id=%s", works_id)

        except Exception as e:
            log.exception("cancelled_notes: failed to process works_id=%s", works_id)
            errors.append({
                "works_id": works_id,
                "error": str(e)
            })

    # Return to the planned works index page when done
    try:
        await page.goto(index_url, timeout=timeout_ms)
        await page.locator('label[for="cancelled_2"]').wait_for(state="visible", timeout=timeout_ms)
    except Exception:
        pass

    success = len(errors) == 0
    message = f"Processed {len(notes_added)} cancelled appointments. Added {len(tasks_created)} task(s). Skipped {len(skipped_jobs)} job(s)."
    if errors:
        message += f" Had errors on {len(errors)} job(s)."

    return {
        "success": success,
        "processed_count": len(jobs_info),
        "processed_jobs": [j["works_id"] for j in jobs_info],
        "notes_added": notes_added,
        "tasks_created": tasks_created,
        "skipped_jobs": skipped_jobs,
        "errors": errors,
        "message": message
    }
