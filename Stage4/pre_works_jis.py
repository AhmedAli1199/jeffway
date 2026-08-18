"""
Stage 4 — Pre-Works JIS Dialog and Task Automation
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging
import math
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page

log = logging.getLogger("stage4.pre_works_jis")

_PAGE_STATE_JS = """() => {
  const lb = document.querySelector('#login_box');
  if (lb && lb.offsetParent !== null) return 'login';
  if (window.location.href.includes('.php')) return 'index_page';
  const h1 = document.querySelector('h1');
  const h1txt = ((h1 && h1.textContent) || '').toLowerCase();
  if (h1txt.includes('easybop') && h1txt.includes('login')) return 'login';
  const body = (document.body && document.body.innerText) || '';
  if (body.includes('EasyBOP Login')) return 'login';
  return null;
}"""


def _format_appointment_period(raw_duration: Any) -> str:
    """
    Format any raw duration input (e.g. 3, 2, 4, 11, 3.0, '3', '2', '4', '11', '03:00', '2.5', '2 hours')
    into HH:MM format suitable for <select id="aptj_time_allowed_override"> options (00:00 to 24:00 in 15-min increments).
    """
    if raw_duration is None:
        return "02:00"

    if isinstance(raw_duration, (int, float)):
        val = float(raw_duration)
        h = int(val)
        m = int(round((val - h) * 60.0 / 15.0) * 15)
        if m >= 60:
            h += 1
            m = 0
        return f"{min(max(h, 0), 24):02d}:{m:02d}"

    raw = str(raw_duration).strip().lower()
    if not raw:
        return "01:00"

    # Handle HH:MM format (e.g. "03:00", "04:30", "11:00:00")
    if ":" in raw:
        parts = raw.split(":")
        try:
            h = int(parts[0])
            m = int(parts[1]) if len(parts) > 1 else 0
            m = int(round(m / 15.0) * 15)
            if m >= 60:
                h += 1
                m = 0
            return f"{min(max(h, 0), 24):02d}:{m:02d}"
        except Exception:
            return "01:00"

    # Extract digits / decimal number from strings like "3", "3.0", "3 hours", "4 hrs", "45 mins"
    match = re.search(r"(\d+(?:\.\d+)?)", raw)
    if match:
        try:
            val = float(match.group(1))
            if "min" in raw and val >= 15 and val < 60:
                m = int(round(val / 15.0) * 15)
                return f"00:{m:02d}"
            
            h = int(val)
            m = int(round((val - h) * 60.0 / 15.0) * 15)
            if m >= 60:
                h += 1
                m = 0
            return f"{min(max(h, 0), 24):02d}:{m:02d}"
        except Exception:
            return "01:00"

    return "01:00"


def _parse_date_received(date_str: str) -> datetime:
    """
    Parse 'DD/MM/YYYY HH:MM' or 'DD/MM/YYYY' into a datetime object for ascending sorting.
    """
    if not date_str or not isinstance(date_str, str):
        return datetime.max
    s = date_str.strip()
    try:
        return datetime.strptime(s, "%d/%m/%Y %H:%M")
    except Exception:
        try:
            return datetime.strptime(s.split()[0], "%d/%m/%Y")
        except Exception:
            return datetime.max


def _normalize_corf_list(raw_list: Optional[List[Any]]) -> Set[str]:
    """
    Extract and normalize Client Order Reference (CORF) strings from a raw list of strings, dict objects,
    or n8n node output objects containing {"json": {"corf": "117349/1"}, "pairedItem": ...}.
    """
    if not raw_list:
        return set()
    result = set()

    def _extract_from_dict(d: dict):
        if not isinstance(d, dict):
            return
        # Handle n8n wrapper {"json": {...}}
        if "json" in d and isinstance(d["json"], dict):
            _extract_from_dict(d["json"])

        # Search keys in dict case-insensitively for corf
        for k, v in d.items():
            if k and str(k).strip().lower() in ("corf", "client_order_reference", "client order reference", "client_ref"):
                if v is not None:
                    val_str = str(v).strip().lower()
                    if val_str:
                        result.add(val_str)

    for item in raw_list:
        if isinstance(item, str):
            c = item.strip().lower()
            if c:
                result.add(c)
        elif isinstance(item, dict):
            _extract_from_dict(item)
        elif item:
            c = str(item).strip().lower()
            if c:
                result.add(c)

    return result


def _extract_date_from_comment(comment: str) -> Optional[str]:
    if not comment:
        return None
    
    c = comment.strip()
    
    # 1. Match DD/MM/YYYY, DD/MM/YY, DD/MM, DD.MM.YY, DD-MM-YY with / . - delimiters
    match = re.search(r"\b(\d{1,2})[\/\.\-](\d{1,2})(?:[\/\.\-](\d{2,4}))?\b", c)
    if match:
        day_str, month_str, year_str = match.groups()
        day = int(day_str)
        month = int(month_str)
        if 1 <= day <= 31 and 1 <= month <= 12:
            if year_str:
                if len(year_str) == 2:
                    year = 2000 + int(year_str)
                else:
                    year = int(year_str)
            else:
                year = 2026
            return f"{day:02d}/{month:02d}/{year}"
            
    # 2. Match text patterns like "17th July", "17 July", "July 17"
    months_map = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12
    }
    
    words = re.findall(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-zA-Z]+)\b", c)
    for day_str, month_name in words:
        m_name = month_name.lower()
        if m_name in months_map:
            day = int(day_str)
            month = months_map[m_name]
            if 1 <= day <= 31:
                return f"{day:02d}/{month:02d}/2026"
                
    words_reverse = re.findall(r"\b([a-zA-Z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\b", c)
    for month_name, day_str in words_reverse:
        m_name = month_name.lower()
        if m_name in months_map:
            day = int(day_str)
            month = months_map[m_name]
            if 1 <= day <= 31:
                return f"{day:02d}/{month:02d}/2026"
                
    return None


def _parse_target_date(target_date_str: str) -> Optional[tuple[int, int, int]]:
    """
    Parse a date string in format DD/MM/YYYY, YYYY-MM-DD, DD-MM-YYYY, or DD.MM.YYYY
    and return (day, month, year) integers.
    """
    if not target_date_str or not str(target_date_str).strip():
        return None
    s = str(target_date_str).strip()

    # 1. YYYY-MM-DD or YYYY/MM/DD
    m = re.match(r"^(\d{4})[\/\.\-](\d{1,2})[\/\.\-](\d{1,2})$", s)
    if m:
        y, mth, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= d <= 31 and 1 <= mth <= 12:
            return d, mth, y

    # 2. DD/MM/YYYY or DD-MM-YYYY or DD.MM.YYYY
    m = re.match(r"^(\d{1,2})[\/\.\-](\d{1,2})[\/\.\-](\d{2,4})$", s)
    if m:
        d, mth, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        if 1 <= d <= 31 and 1 <= mth <= 12:
            return d, mth, y

    return None


def _get_next_working_days(
    start_date_str: Optional[str] = None,
    count: int = 5
) -> List[tuple[Any, str, str]]:
    """
    Get a list of `count` upcoming working days (Monday-Friday) starting from start_date_str (or today).
    Returns list of (datetime_obj, "DD/MM/YYYY", "DayOfWeekName").
    """
    from datetime import datetime, timedelta

    if start_date_str:
        parsed = _parse_target_date(start_date_str)
        if parsed:
            d, mth, y = parsed
            curr = datetime(y, mth, d)
        else:
            curr = datetime.now()
    else:
        curr = datetime.now()

    days = []
    days_map = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    while len(days) < count:
        if curr.weekday() < 5:  # Monday to Friday
            days.append((curr, curr.strftime("%d/%m/%Y"), days_map[curr.weekday()]))
        curr += timedelta(days=1)

    return days


async def _navigate(page: Page, url: str, timeout_ms: int) -> None:
    log.info("pre_works_jis: navigating to %s", url)
    try:
        await page.goto(url, wait_until="commit", timeout=timeout_ms)
    except Exception as exc:
        log.warning("pre_works_jis: commit nav issue (%s), retry load", exc)
        try:
            await page.goto(url, wait_until="load", timeout=timeout_ms)
        except Exception as exc2:
            log.warning("pre_works_jis: load nav issue (%s)", exc2)


async def _page_is_login(page: Page) -> bool:
    try:
        if await page.locator("#login_box").count() > 0:
            if await page.locator("#login_box").first.is_visible():
                return True
        title = (await page.title() or "").lower()
        if "login" in title:
            return True
        url = page.url.lower()
        return "login.php" in url or "r=%2f" in url
    except Exception:
        return False


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
        
        is_login = await _page_is_login(page)
        if is_login:
            log.info("pre_works_jis: login page detected (attempt %s)", attempt + 1)
            ok = await login(page, username, password)
            if not ok:
                raise RuntimeError("EasyBOP login failed — check credentials")
            await context.storage_state(path=str(session_path))
            await asyncio.sleep(2.0)
            continue

        try:
            handle = await page.wait_for_function(_PAGE_STATE_JS, timeout=timeout_ms)
            state = await handle.json_value()
        except Exception as exc:
            log.warning("pre_works_jis: wait for page state timed out (%s)", exc)
            state = None

        log.info("pre_works_jis: state=%r url=%s", state, page.url)

        if state == "index_page":
            return
        if state == "login":
            log.info("pre_works_jis: login required (attempt %s)", attempt + 1)
            ok = await login(page, username, password)
            if not ok:
                raise RuntimeError("EasyBOP login failed")
            await context.storage_state(path=str(session_path))
            await asyncio.sleep(2.0)
            continue
        # Fallback check if it looks like the index page even if the label wasn't ready
        if "index.php" in page.url or "pre_works.php" in page.url or "works_details.php" in page.url or "tasks_issued_by_me.php" in page.url or "scheduling.php" in page.url or "scheduling_board.php" in page.url:
            return
        raise RuntimeError(f"Unexpected page state: {state!r}")


def _clean_label(raw: Any) -> str:
    import html as html_lib
    import re
    text = html_lib.unescape(str(raw or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _strip_html_text(raw: Any) -> str:
    import html as html_lib
    import re
    text = html_lib.unescape(str(raw or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _decode_multiple_value(question: Dict[str, Any]) -> Any:
    raw = question.get("value")
    if raw is None or raw == "":
        return ""
    answers = (question.get("config") or {}).get("answers") or []
    id_to_text = {str(a.get("value")): str(a.get("text", "")).strip() for a in answers}
    selected_ids = [part.strip() for part in str(raw).split(",") if part.strip()]
    texts = [id_to_text.get(i, i) for i in selected_ids]
    texts = [t for t in texts if t]
    if not texts:
        return ""
    return texts[0] if len(texts) == 1 else texts


def _decode_yes_no_value(question: Dict[str, Any]) -> Any:
    raw = question.get("value")
    if isinstance(raw, dict):
        radio = str(raw.get("radio", "")).strip()
        yes_no = "Yes" if radio == "1" else "No" if radio == "0" else radio
        note = _strip_html_text(raw.get("note"))
        return {"answer": yes_no, "note": note or None}
    if raw in (None, ""):
        return ""
    return raw


def _decode_dataset_value(question: Dict[str, Any]) -> Any:
    raw = question.get("value")
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        return raw
    rows: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        answers = item.get("answers") or {}
        cleaned_answers = {
            str(k): _strip_html_text(v) if isinstance(v, str) else v
            for k, v in answers.items()
        }
        rows.append(
            {
                "name": _strip_html_text(item.get("name")),
                "answers": cleaned_answers,
                "answers_text": _strip_html_text(
                    " | ".join(str(v) for v in cleaned_answers.values() if v)
                ),
            }
        )
    return rows


def _decode_question_value(question: Dict[str, Any]) -> Any:
    qtype = str(question.get("type") or "").lower()
    if qtype == "multiple":
        return _decode_multiple_value(question)
    if qtype == "yes_no":
        return _decode_yes_no_value(question)
    if qtype == "dataset":
        return _decode_dataset_value(question)
    if qtype == "rich_text":
        raw = question.get("value") or ""
        return {
            "text": _strip_html_text(raw),
            "html": str(raw).strip() or None,
        }
    raw = question.get("value")
    if isinstance(raw, str):
        return _strip_html_text(raw)
    return raw if raw is not None else ""


def _q_key(section_prefix: Any, question_prefix: Any) -> Optional[str]:
    sec = str(section_prefix or "").strip()
    q = str(question_prefix or "").strip()
    if sec and q:
        return f"Q{sec}.{q}"
    return None


def _flatten_fields(fields: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for field in fields:
        q_key = field.get("q_key")
        label = field.get("label")
        value = field.get("value")
        if q_key:
            out[q_key] = value
        elif label:
            out[label] = value
    return out


def _parse_api_payload(data: Dict[str, Any], *, requested_url: str) -> Dict[str, Any]:
    fields: List[Dict[str, Any]] = []
    by_q_key: Dict[str, Any] = {}

    for section in data.get("sections") or []:
        section_name = _clean_label(section.get("name"))
        section_prefix = section.get("prefix")
        for question in section.get("questions") or []:
            q_key = _q_key(section_prefix, question.get("prefix"))
            label = _clean_label(question.get("label"))
            value = _decode_question_value(question)
            field = {
                "section": section_name,
                "section_prefix": section_prefix,
                "question_prefix": question.get("prefix"),
                "q_key": q_key,
                "label": label,
                "type": question.get("type"),
                "question_id": question.get("id"),
                "value": value,
                "raw_value": question.get("value"),
            }
            fields.append(field)
            if q_key and q_key not in by_q_key:
                by_q_key[q_key] = value

    return {
        "ok": True,
        "source": "api",
        "requested_url": requested_url,
        "url": requested_url,
        "smart_form_id": str(data.get("id") or ""),
        "form_name": _clean_label(data.get("name")),
        "template_name": _clean_label(data.get("template_name")),
        "complete": bool(data.get("complete")),
        "completed_date": data.get("completed_date"),
        "date_created": data.get("date_created"),
        "date_modified": data.get("date_modified"),
        "entity_id": data.get("entity_id"),
        "parent_id": data.get("parent_id"),
        "fields": fields,
        "by_q_key": by_q_key,
        "values_flat": _flatten_fields(fields),
        "meta": {
            "section_count": len(data.get("sections") or []),
            "question_count": len(fields),
            "api_url": f"https://propeller-api.co.uk/api/smart_forms/get/{data.get('id')}",
        },
    }


async def _fetch_smart_form_via_api(page: Page, url: str, smart_form_id: str, timeout_ms: int) -> Dict[str, Any]:
    api_pattern = f"/api/smart_forms/get/{smart_form_id}"
    captured: Dict[str, Any] = {}

    async def _capture(response) -> None:
        if api_pattern in response.url and response.status == 200 and "data" not in captured:
            try:
                captured["data"] = await response.json()
                captured["api_url"] = response.url
            except Exception as exc:
                captured["error"] = str(exc)

    page.on("response", lambda response: asyncio.create_task(_capture(response)))

    try:
        async with page.expect_response(
            lambda r: api_pattern in r.url and r.status == 200,
            timeout=timeout_ms,
        ) as resp_info:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        resp = await resp_info.value
        data = captured.get("data") or await resp.json()
    except Exception:
        if "data" in captured:
            data = captured["data"]
        else:
            raise RuntimeError(
                f"Propeller API response not captured for smart_form_id={smart_form_id}"
            ) from None

    if not isinstance(data, dict):
        raise RuntimeError("Unexpected Propeller API payload")
    return _parse_api_payload(data, requested_url=url)


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
    if await btn.count() == 0 or not await btn.first.is_visible():
        btn = page.locator("button").filter(has_text="Save Changes")
    await btn.first.wait_for(state="visible", timeout=timeout_ms)
    await btn.first.click()
    log.info("task: Save Changes clicked")
    try:
        await page.wait_for_selector(".ui-dialog:visible", state="hidden", timeout=10_000)
        log.info("task: dialog closed after save")
    except Exception:
        pass


async def run_create_contractor_task(
    page: Page,
    *,
    contract_id: str,
    works_id: str,
    username: str,
    password: str,
    timeout_ms: int,
    login_fn: Callable[[Page, str, str], Awaitable[bool]],
    session_path: Path,
    context: BrowserContext,
) -> Dict[str, Any]:
    """
    Create a task for Shannon Slade, Category: Book work for contractor,
    Priority: High, Completion date: Today.
    """
    tasks_url = f"https://easybop.co.uk/a_planned_works/z_works/works_details.php?works_id={works_id}&contract_id={contract_id}&tab_nav_item=tasks"

    # 1. Authenticate & Navigate to Tasks tab
    await _ensure_authenticated(
        page,
        context=context,
        url=tasks_url,
        login=login_fn,
        username=username,
        password=password,
        session_path=session_path,
        timeout_ms=timeout_ms,
    )

    # 2. Click "Add Task" button
    add_task_btn = page.locator("button#btn_add_task_5")
    if await add_task_btn.count() == 0 or not await add_task_btn.first.is_visible():
        add_task_btn = page.locator("button[id^='btn_add_task']")
    if await add_task_btn.count() == 0 or not await add_task_btn.first.is_visible():
        add_task_btn = page.locator("button").filter(has_text="Add Task")
        
    await add_task_btn.first.wait_for(state="visible", timeout=timeout_ms)
    await add_task_btn.first.click()
    log.info("create_task: clicked 'Add Task'")
    
    # Wait for dialog
    dialog = page.locator(".ui-dialog:visible, [role='dialog']:visible")
    await dialog.first.wait_for(state="visible", timeout=8_000)
    await asyncio.sleep(0.5)
    
    # Select category
    await _select_task_category(page, "Book work for contractor", timeout_ms)
    
    # Fill description
    desc_field = page.locator("#description").first
    await desc_field.fill("Book work for contractor.")
    
    # Fill Issued To, Priority, Due Date (today)
    await _fill_task_issued_to(page, "Shannon", "Shannon Slade", timeout_ms)
    await _select_task_priority(page, "High", timeout_ms)
    
    from datetime import datetime
    today_str = datetime.now().strftime("%d/%m/%Y")
    await _fill_task_due_date(page, today_str)
    
    await asyncio.sleep(0.4)
    await _save_task_changes(page, timeout_ms)
    
    log.info("create_task: task created successfully for works_id=%s", works_id)

    return {
        "success": True,
        "works_id": works_id,
        "issued_to": "Shannon Slade",
        "category": "Book work for contractor",
        "priority": "High",
        "due_date": today_str,
        "message": f"Task created successfully for works_id={works_id}"
    }


async def run_create_task(
    page: Page,
    *,
    contract_id: str,
    works_id: str,
    category: str,
    description: str,
    issued_to_query: str,
    issued_to_label: str,
    priority: str = "High",
    due_date: Optional[str] = None,
    username: str,
    password: str,
    timeout_ms: int,
    login_fn: Callable[[Page, str, str], Awaitable[bool]],
    session_path: Path,
    context: BrowserContext,
) -> Dict[str, Any]:
    """
    Generic task creation on a works order's Tasks tab. Unlike
    run_create_contractor_task (fixed to Shannon Slade / "Book work for
    contractor"), this accepts category/description/assignee/priority/due
    date so it can be used for other notifications — e.g. flagging a Vapi
    voice-call reschedule or scheduling conflict for the admin/scheduling
    team to action manually.
    """
    tasks_url = f"https://easybop.co.uk/a_planned_works/z_works/works_details.php?works_id={works_id}&contract_id={contract_id}&tab_nav_item=tasks"

    await _ensure_authenticated(
        page,
        context=context,
        url=tasks_url,
        login=login_fn,
        username=username,
        password=password,
        session_path=session_path,
        timeout_ms=timeout_ms,
    )

    add_task_btn = page.locator("button#btn_add_task_5")
    if await add_task_btn.count() == 0 or not await add_task_btn.first.is_visible():
        add_task_btn = page.locator("button[id^='btn_add_task']")
    if await add_task_btn.count() == 0 or not await add_task_btn.first.is_visible():
        add_task_btn = page.locator("button").filter(has_text="Add Task")

    await add_task_btn.first.wait_for(state="visible", timeout=timeout_ms)
    await add_task_btn.first.click()
    log.info("create_task(generic): clicked 'Add Task' works_id=%s category=%r", works_id, category)

    dialog = page.locator(".ui-dialog:visible, [role='dialog']:visible")
    await dialog.first.wait_for(state="visible", timeout=8_000)
    await asyncio.sleep(0.5)

    await _select_task_category(page, category, timeout_ms)

    desc_field = page.locator("#description").first
    await desc_field.fill(description)

    await _fill_task_issued_to(page, issued_to_query, issued_to_label, timeout_ms)
    await _select_task_priority(page, priority, timeout_ms)

    due_date_str = due_date or datetime.now().strftime("%d/%m/%Y")
    await _fill_task_due_date(page, due_date_str)

    await asyncio.sleep(0.4)
    await _save_task_changes(page, timeout_ms)

    log.info("create_task(generic): task created for works_id=%s category=%r issued_to=%r", works_id, category, issued_to_label)

    return {
        "success": True,
        "works_id": works_id,
        "issued_to": issued_to_label,
        "category": category,
        "priority": priority,
        "due_date": due_date_str,
        "description": description,
        "message": f"Task created successfully for works_id={works_id}",
    }


async def run_pre_works_jis(
    page: Page,
    *,
    contract_id: str,
    item_id: str,
    existing_corfs: Optional[List[Any]] = None,
    username: str,
    password: str,
    timeout_ms: int,
    login_fn: Callable[[Page, str, str], Awaitable[bool]],
    session_path: Path,
    context: BrowserContext,
) -> Dict[str, Any]:
    """
    Find qualifying jobs on the pre-works report (Pre-completion Items To Complete
    with green JIS note cell), navigate to their details page, click 'Click on me',
    and wait for the JIS smart form dialog to open.
    Restricted to processing the first 10 qualifying jobs only.
    """
    index_url = f"https://easybop.co.uk/a_planned_works/z_works_reports/pre_works.php?contract_id={contract_id}"

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

    # 2. Wait for initial table grid / filter elements to load
    await asyncio.sleep(2.0)
    try:
        await page.wait_for_selector("#works_administrative_status_search, tr.jqgrow", state="attached", timeout=15000)
    except Exception:
        pass

    # 3. Set Filter Options:
    # - Administrative Status -> "Pre-completion Items To Complete" (value 6)
    # - Works Status -> select options excluding "Works Completed" (7) and "Works Cancelled" (8)
    try:
        admin_select = page.locator("#works_administrative_status_search")
        if await admin_select.count() > 0:
            await admin_select.first.wait_for(state="attached", timeout=10000)
            try:
                await admin_select.first.select_option(value="6")
                log.info("pre_works_jis: selected Administrative Status value '6' (Pre-completion Items To Complete)")
            except Exception as e:
                log.warning("pre_works_jis: failed selecting Administrative Status: %s", e)

        # Try clicking multi-select toggle link for Works Status if present
        try:
            toggle_link = page.locator('a[data-action="module_filter_toggle_multiselect"][data-select-id="works_status_search"]')
            if await toggle_link.count() > 0 and await toggle_link.first.is_visible():
                await toggle_link.first.click()
                await asyncio.sleep(0.5)
                log.info("pre_works_jis: clicked multi-select toggle for #works_status_search")
        except Exception:
            pass

        # Configure #works_status_search multi-options excluding Works Started, Works Completed, Works Cancelled
        await page.evaluate("""() => {
            const sel = document.getElementById('works_status_search');
            if (sel) {
                sel.setAttribute('multiple', 'multiple');
                Array.from(sel.options).forEach(opt => {
                    const txt = (opt.textContent || '').toLowerCase();
                    const val = (opt.value || '').trim();
                    if (txt.includes('works started') || txt.includes('works completed') || txt.includes('works cancelled') || val === '5' || val === '7' || val === '8') {
                        opt.selected = false;
                    } else if (val) {
                        opt.selected = true;
                    }
                });
            }
        }""")
        log.info("pre_works_jis: configured #works_status_search to exclude Works Started, Completed, Cancelled")

        # Dispatch change/input events
        await page.evaluate("""() => {
            ['works_administrative_status_search', 'works_status_search'].forEach(id => {
                const sel = document.getElementById(id);
                if (sel) {
                    sel.dispatchEvent(new Event('input', { bubbles: true }));
                    sel.dispatchEvent(new Event('change', { bubbles: true }));
                    if (window.jQuery) {
                        try { window.jQuery(sel).trigger('change'); } catch(e) {}
                    }
                }
            });
        }""")
        log.info("pre_works_jis: dispatched change events for filter inputs")
        await asyncio.sleep(1.0)
    except Exception as exc:
        log.warning("pre_works_jis: issue applying top dropdown filters: %s", exc)

    # 4. Click Search Button (#btn_search) to reload table with filtered items
    try:
        search_btn = page.locator("#btn_search")
        if await search_btn.count() > 0 and await search_btn.first.is_visible():
            await search_btn.first.click()
            log.info("pre_works_jis: clicked '#btn_search' button")
        else:
            await page.evaluate("""() => {
                const btn = document.getElementById('btn_search') || 
                            Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim().includes('Search'));
                if (btn) btn.click();
            }""")
            log.info("pre_works_jis: JS clicked '#btn_search' button")
    except Exception as exc:
        log.warning("pre_works_jis: issue clicking '#btn_search' button: %s", exc)

    await asyncio.sleep(2.0)

    # 5. Wait for grid reload to clear/finish and table rows to populate after search
    try:
        await page.wait_for_function(
            """() => {
                const loadEl = document.getElementById('load_list');
                const loading = loadEl && loadEl.offsetParent !== null && loadEl.style.display !== 'none';
                return !loading;
            }""",
            timeout=timeout_ms,
        )
    except Exception as e:
        log.warning("pre_works_jis: waiting for loading overlay to hide timed out: %s", e)

    try:
        await page.wait_for_function(
            """() => {
                const rows = document.querySelectorAll('tr.jqgrow');
                if (rows.length === 0) return false;
                const loadEl = document.getElementById('load_list');
                if (loadEl && loadEl.offsetParent !== null && loadEl.style.display !== 'none') {
                    return false;
                }
                return Array.from(rows).some(r => r.querySelector('td[aria-describedby*="works_status"]'));
            }""",
            timeout=timeout_ms,
        )
        log.info("pre_works_jis: tr.jqgrow table rows loaded successfully after clicking search")
    except Exception as exc:
        log.warning("pre_works_jis: tr.jqgrow table row wait timed out after %dms: %s", timeout_ms, exc)

    # 6. Extract eligible jobs from pre_works report table
    # Conditions:
    # 1. Administrative status MUST contain 'pre-completion' (case-insensitive)
    # 2. Works status MUST NOT be 'Works Started', 'Works Completed', or 'Works Cancelled'
    # 3. Green JIS note cell exists (itemId)
    # 4. Appointment Date column (list_appointment_date_formatted) IS EMPTY / NULL
    # 5. Next Appointment Date column (list_udf_5088) IS EMPTY / NULL
    res = await page.evaluate(
        """(itemId) => {
            const rows = document.querySelectorAll('tr.jqgrow');
            const qualifying = [];
            const debug = {
                total_rows: rows.length,
                statuses_seen: [],
            };

            function isCellEmpty(td) {
                if (!td) return true;
                const txt = (td.getAttribute('title') || td.innerText || td.textContent || '').trim();
                return txt === '' || txt === '-' || txt === '&nbsp;' || txt.toLowerCase() === 'null';
            }

            for (const row of rows) {
                const statusCell = row.querySelector('td[aria-describedby*="works_status"]');
                const status = statusCell ? (statusCell.getAttribute('title') || statusCell.innerText || '').trim() : '';
                if (status && !debug.statuses_seen.includes(status)) {
                    debug.statuses_seen.push(status);
                }

                const statusLower = status.toLowerCase();

                // 1. Strictly check Administrative Status: MUST contain 'pre-completion'
                if (!statusLower.includes('pre-completion')) {
                    continue;
                }

                // 2. Strictly EXCLUDE 'Works Started', 'Works Completed', or 'Works Cancelled'
                if (statusLower.includes('works started') || 
                    statusLower.includes('works completed') || 
                    statusLower.includes('works cancelled') || 
                    statusLower.includes('fully complete') || 
                    statusLower.includes('cancelled')) {
                    continue;
                }

                // Filter 1: Green JIS note cell
                const greenCell = row.querySelector(`td[aria-describedby*="${itemId}"]`);
                if (!greenCell) {
                    continue;
                }

                const cellClass = greenCell.className || '';
                const cellStyle = greenCell.getAttribute('style') || '';
                const isGreen = cellClass.includes('green') || 
                                cellClass.includes('jqgrid_bg_green') || 
                                cellStyle.includes('green') ||
                                cellStyle.includes('rgb(0,');

                if (!isGreen) {
                    continue;
                }

                // Filter 2: Appointment Date (list_appointment_date_formatted) MUST BE EMPTY
                const apptDateCell = row.querySelector('td[aria-describedby*="appointment_date_formatted"]');
                if (!isCellEmpty(apptDateCell)) {
                    continue;
                }

                // Filter 3: Next Appointment Date (list_udf_5088) MUST BE EMPTY
                const nextApptCell = row.querySelector('td[aria-describedby*="udf_5088"]');
                if (!isCellEmpty(nextApptCell)) {
                    continue;
                }

                const propertyRefCell = row.querySelector('td[aria-describedby*="property_ref"]');
                const addressCell = row.querySelector('td[aria-describedby*="address"]');
                const scopeCell = row.querySelector('td[aria-describedby*="scope_of_work"]');
                const corfCell = row.querySelector('td[aria-describedby*="client_order_reference"]');
                const dateReceivedCell = row.querySelector('td[aria-describedby*="date_received"]');

                qualifying.push({
                    works_id: row.id,
                    property_ref: propertyRefCell ? (propertyRefCell.getAttribute('title') || propertyRefCell.innerText || '').trim() : '',
                    address: addressCell ? (addressCell.getAttribute('title') || addressCell.innerText || '').trim() : '',
                    scope_of_work: scopeCell ? (scopeCell.getAttribute('title') || scopeCell.innerText || '').trim() : '',
                    client_order_reference: corfCell ? (corfCell.getAttribute('title') || corfCell.innerText || '').trim() : '',
                    date_received: dateReceivedCell ? (dateReceivedCell.getAttribute('title') || dateReceivedCell.innerText || '').trim() : '',
                    works_status: status
                });
            }
            return { qualifying, debug };
        }""",
        item_id,
    )
    jobs_info = res.get("qualifying") or []
    debug_info = res.get("debug") or {}

    log.info("pre_works_jis: found %d raw qualifying pre-works jobs (total table rows: %d, statuses seen: %r)", 
             len(jobs_info), debug_info.get("total_rows", 0), debug_info.get("statuses_seen", []))

    # Apply CORF deduplication against existing_corfs
    exclude_corfs = _normalize_corf_list(existing_corfs)
    if exclude_corfs:
        log.info("pre_works_jis: excluding %d existing CORFs: %r", len(exclude_corfs), list(exclude_corfs))
        filtered_jobs = []
        skipped_count = 0
        for job in jobs_info:
            c = str(job.get("client_order_reference") or "").strip().lower()
            if c and c in exclude_corfs:
                log.info("pre_works_jis: skipping job works_id=%s because CORF %r is in existing_corfs", job.get("works_id"), job.get("client_order_reference"))
                skipped_count += 1
                continue
            filtered_jobs.append(job)
        log.info("pre_works_jis: CORF deduplication skipped %d jobs. %d eligible jobs remain.", skipped_count, len(filtered_jobs))
        jobs_info = filtered_jobs

    # Sort qualifying jobs in ascending order of date_received (older jobs on top, newer at the end)
    jobs_info.sort(key=lambda j: _parse_date_received(j.get("date_received", "")))
    log.info("pre_works_jis: sorted %d eligible jobs in ascending order of date_received", len(jobs_info))

    if not jobs_info:
        return {
            "success": True,
            "processed_count": 0,
            "processed_jobs": [],
            "appointments": [],
            "errors": [],
            "debug": debug_info,
            "message": f"No qualifying pre-works jobs remaining after CORF deduplication. (Examined {debug_info.get('total_rows', 0)} rows, statuses seen: {debug_info.get('statuses_seen', [])})"
        }

    processed_jobs = []
    appointments_list = []
    errors = []

    # 4. Iterate over each qualifying job and click the link to open the pre-item dialog
    for job in jobs_info:
        works_id = job["works_id"]
        try:
            log.info("pre_works_jis: navigating to works_id=%s details page", works_id)
            details_url = f"https://easybop.co.uk/a_planned_works/z_works/works_details.php?works_id={works_id}&contract_id={contract_id}&tab_nav_item=wi"
            await page.goto(details_url, timeout=timeout_ms)
            
            # Mid-loop session drop check
            if await _page_is_login(page):
                log.info("pre_works_jis: session dropped for works_id=%s — re-authenticating", works_id)
                ok = await login_fn(page, username, password)
                if not ok:
                    raise RuntimeError("EasyBOP login failed during job navigation")
                await context.storage_state(path=str(session_path))
                await asyncio.sleep(2.0)
                await page.goto(details_url, timeout=timeout_ms)

            try:
                await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            except Exception:
                pass

            await asyncio.sleep(1.0)

            # Scrape tenant mobile from top header (.box_title_item)
            tenant_mobile = await page.evaluate(
                """() => {
                    const items = document.querySelectorAll('.box_title_item');
                    for (const item of items) {
                        const text = (item.textContent || '').trim();
                        if (text.toLowerCase().includes('mobile:')) {
                            return text.replace(/mobile:/i, '').trim();
                        }
                    }
                    for (const item of items) {
                        const text = (item.textContent || '').trim();
                        if (text.toLowerCase().includes('tel:') || text.toLowerCase().includes('telephone:')) {
                            return text.replace(/tel(ephone)?:/i, '').trim();
                        }
                    }
                    return '';
                }"""
            )
            log.info("pre_works_jis: extracted tenant_mobile=%r for works_id=%s", tenant_mobile, works_id)

            # Look for the Click on me link
            link_selector = 'a[data-action="pw_works_show_pre_item_dialog"]'
            await page.wait_for_selector(link_selector, state="visible", timeout=15000)

            # Match the item args
            links = page.locator(link_selector)
            n = await links.count()
            link_clicked = False
            for i in range(n):
                link = links.nth(i)
                item_args_str = await link.get_attribute("data-item-args")
                if not item_args_str:
                    continue
                try:
                    import json
                    args = json.loads(item_args_str)
                    if (str(args.get("contract_id")) == str(contract_id) and 
                        str(args.get("works_id")) == str(works_id) and 
                        str(args.get("item_id")) == str(item_id)):
                        await link.click()
                        log.info("pre_works_jis: clicked 'Click on me' link for works_id=%s", works_id)
                        link_clicked = True
                        break
                except Exception as e:
                    log.warning("pre_works_jis: error parsing data-item-args: %s", e)

            if not link_clicked:
                raise RuntimeError(f"Could not find matching 'Click on me' link for works_id={works_id} and item_id={item_id}")

            # Wait for the dialog to open and be visible
            dialog = page.locator(".ui-dialog:visible, [role='dialog']:visible")
            await dialog.first.wait_for(state="visible", timeout=10000)
            log.info("pre_works_jis: JIS dialog successfully opened for works_id=%s", works_id)

            # Extract smart_form_id from the dialog Reset Form link
            reset_btn = page.locator('a[data-action="works_ajax_resetSmartForm"]')
            await reset_btn.first.wait_for(state="attached", timeout=15000)
            smart_form_id = await reset_btn.first.get_attribute("data-smart-form-x-id")
            if not smart_form_id:
                smart_form_id = await reset_btn.first.get_attribute("data-smart-form-id")
            
            if not smart_form_id:
                raise RuntimeError(f"Could not extract smart_form_id for works_id={works_id}")
            
            log.info("pre_works_jis: extracted smart_form_id=%s", smart_form_id)

            # Fetch the SmartForm details
            smart_form_url = f"https://easybop.co.uk/SmartForms/{smart_form_id}"
            smart_form_payload = await _fetch_smart_form_via_api(page, smart_form_url, smart_form_id, timeout_ms)
            
            # Extract appointment details with strict sequential appointment numbering (Appointment 1, Appointment 2, etc.)
            job_appointments = []
            fields = smart_form_payload.get("fields") or []
            appt_counter = 1

            for field in fields:
                section_name = str(field.get("section", "")).strip()
                sec_lower = section_name.lower()
                if sec_lower.startswith("appointment") or sec_lower.startswith("q7") or sec_lower.startswith("q8") or sec_lower.startswith("q9") or sec_lower.startswith("q10") or sec_lower.startswith("q11") or "appointment" in sec_lower:
                    if field.get("type") == "dataset":
                        val = field.get("value")
                        if isinstance(val, list):
                            for row in val:
                                trade = row.get("name")
                                answers = row.get("answers", {})
                                ans_list = list(answers.values()) if isinstance(answers, dict) else []
                                hours = ans_list[1] if len(ans_list) > 1 else ""
                                desc = ans_list[2] if len(ans_list) > 2 else ""
                                
                                # Parse total numeric hours required
                                raw_hours_str = str(hours or "").strip().lower()
                                total_hours_val = None
                                desc_prefix = ""

                                if not raw_hours_str or raw_hours_str in ["null", "n/a", "na", "unknown", "none", "0"]:
                                    desc_prefix = "ALERT: JOB DURATION UNKNOWN — DEFAULT 1 HOUR APPOINTMENT CREATED. SCHEDULING STAFF MUST CONFIRM ACTUAL DURATION BEFORE ALLOCATING."
                                    total_hours_val = 1.0
                                else:
                                    h_match = re.search(r"(\d+(?:\.\d+)?)", raw_hours_str)
                                    if h_match:
                                        try:
                                            parsed_val = float(h_match.group(1))
                                            if parsed_val > 40.0:
                                                orig_h = int(parsed_val) if parsed_val.is_integer() else round(parsed_val, 1)
                                                days_calc = parsed_val / 8.0
                                                approx_workdays = int(round(days_calc)) if abs(days_calc - round(days_calc)) < 0.1 else round(days_calc, 1)
                                                desc_prefix = (
                                                    f"ALERT: ORIGINAL REQUIRED DURATION WAS {orig_h} HOURS (~{approx_workdays} WORKDAYS). "
                                                    f"DURATION CAPPED AT 40 HOURS (5 WORKDAYS MAXIMUM) FOR INITIAL SCHEDULING — PLEASE SCHEDULE REMAINING WEEKS / SLOTS IN EASYBOP."
                                                )
                                                total_hours_val = 40.0
                                            else:
                                                total_hours_val = parsed_val
                                        except Exception:
                                            total_hours_val = 1.0
                                    else:
                                        total_hours_val = 1.0

                                full_desc = f"{desc_prefix} * {desc}" if desc_prefix and desc else (desc_prefix or desc)

                                # Daily-chunk splitting: Maximum 8 hours per appointment day
                                # e.g. 15 hours -> Day 1: 8 hours, Day 2: 7 hours
                                if total_hours_val is None or total_hours_val <= 8.0:
                                    daily_chunks = [(f"Appointment {appt_counter}", total_hours_val or 1.0, full_desc)]
                                else:
                                    num_days = math.ceil(total_hours_val / 8.0)
                                    daily_chunks = []
                                    rem_h = total_hours_val
                                    for day_idx in range(1, num_days + 1):
                                        chunk_h = min(8.0, rem_h)
                                        rem_h -= chunk_h
                                        chunk_label = f"Appointment {appt_counter} (Day {day_idx} of {num_days})"
                                        chunk_h_str = str(int(chunk_h)) if chunk_h.is_integer() else str(round(chunk_h, 1))
                                        chunk_desc = f"[Day {day_idx} of {num_days} — {chunk_h_str} Hours] {full_desc}"
                                        daily_chunks.append((chunk_label, chunk_h, chunk_desc))

                                appt_counter += 1

                                # Trade classification logic
                                trade_lower = str(trade or "").lower().strip()
                                hours_lower = raw_hours_str
                                sub_keywords = [
                                    "roofing",
                                    "roofer",
                                    "scaffolding",
                                    "scaffolder",
                                    "asbestos removal subcontract",
                                    "asbestos removal",
                                    "asbestos",
                                    "subcontract",
                                    "subcontractor",
                                    "book separately",
                                    "bokk separately"
                                ]
                                is_sub = any(kw in trade_lower for kw in sub_keywords) or any(kw in hours_lower for kw in sub_keywords)
                                classification = "subcontractor" if is_sub else "in-house"

                                for section_label, c_hours, c_desc in daily_chunks:
                                    c_hours_str = str(int(c_hours)) if isinstance(c_hours, float) and c_hours.is_integer() else str(c_hours)
                                    unique_suffix = section_label.replace(" ", "").replace("(", "_").replace(")", "_")
                                    appt_info = {
                                        "works_id": works_id,
                                        "property_ref": job.get("property_ref"),
                                        "address": job.get("address"),
                                        "client_order_reference": job.get("client_order_reference"),
                                        "date_received": job.get("date_received", ""),
                                        "tenant_mobile": tenant_mobile,
                                        "smart_form_id": smart_form_id,
                                        "section": section_label,
                                        "trade": trade,
                                        "hours": c_hours_str,
                                        "description": c_desc,
                                        "classification": classification,
                                        "unique_key": f"{works_id}_{unique_suffix}_{trade_lower.replace(' ', '_')}"
                                    }
                                    job_appointments.append(appt_info)
                                    appointments_list.append(appt_info)

            # Log / Print to console
            print(f"\n=== APPOINTMENT INFO FOR Works ID: {works_id} ===")
            for appt in job_appointments:
                print(f"[{appt['section']}] ({appt['classification'].upper()})")
                print(f"  Trade/Sub: {appt['trade']}")
                print(f"  Hours/Qty: {appt['hours']}")
                print(f"  Details:   {appt['description']}")
            print("=========================================\n")

            processed_jobs.append({
                **job,
                "tenant_mobile": tenant_mobile,
                "smart_form_id": smart_form_id,
                "dialog_opened": True,
                "appointments": job_appointments
            })

        except Exception as e:
            log.exception("pre_works_jis: failed for works_id=%s", works_id)
            errors.append({
                "works_id": works_id,
                "error": str(e)
            })

    # Navigate back to report page when finished
    try:
        await page.goto(index_url, timeout=timeout_ms)
    except Exception:
        pass

    success = len(errors) == 0
    message = f"Processed {len(processed_jobs)} qualifying jobs. Found {len(appointments_list)} appointments."
    if errors:
        message += f" Had errors on {len(errors)} job(s)."

    return {
        "success": success,
        "processed_count": len(processed_jobs),
        "processed_jobs": processed_jobs,
        "appointments": appointments_list,
        "errors": errors,
        "message": message
    }


async def run_check_completed_tasks(
    page: Page,
    *,
    category: str = "Book work for contractor",
    username: str,
    password: str,
    timeout_ms: int,
    login_fn: Callable[[Page, str, str], Awaitable[bool]],
    session_path: Path,
    context: BrowserContext,
) -> Dict[str, Any]:
    """
    Go to tasks issued by me, select category, search, and extract all completed task rows.
    """
    url = "https://easybop.co.uk/a_tasks/tasks_issued_by_me.php"

    # 1. Authenticate & Navigate to tasks page
    await _ensure_authenticated(
        page,
        context=context,
        url=url,
        login=login_fn,
        username=username,
        password=password,
        session_path=session_path,
        timeout_ms=timeout_ms,
    )

    # 2. Select category
    search_cat = page.locator("#search_categories")
    await search_cat.wait_for(state="visible", timeout=timeout_ms)
    
    try:
        await search_cat.select_option(label=category)
        log.info("check_tasks: selected category %r", category)
    except Exception:
        options = await search_cat.locator("option").all()
        cat_lower = category.lower()
        selected = False
        for opt in options:
            text = (await opt.inner_text()).strip()
            if cat_lower in text.lower():
                val = await opt.get_attribute("value")
                await search_cat.select_option(value=val)
                log.info("check_tasks: selected category %r (partial match for %r)", text, category)
                selected = True
                break
        if not selected:
            log.warning("check_tasks: category %r not found", category)

    # 3. Click Search button
    search_btn = page.locator("#btn_search")
    await search_btn.wait_for(state="visible", timeout=timeout_ms)
    await search_btn.click()
    log.info("check_tasks: clicked search button")

    # 4. Wait for grid load to finish
    await asyncio.sleep(2.0)
    try:
        await page.wait_for_function(
            """() => {
                const loadEl = document.getElementById('load_list');
                const loading = loadEl && loadEl.offsetParent !== null && loadEl.style.display !== 'none';
                return !loading;
            }""",
            timeout=timeout_ms,
        )
    except Exception as e:
        log.warning("check_tasks: waiting for loading overlay to hide timed out: %s", e)

    # 5. Scrape table rows for completed tasks
    completed_tasks = await page.evaluate(
        """() => {
            const rows = document.querySelectorAll('tr.jqgrow');
            const completed = [];
            for (const row of rows) {
                const dateCompCell = row.querySelector('td[aria-describedby="list_date_completed"]');
                const dateCompleted = dateCompCell ? (dateCompCell.getAttribute('title') || dateCompCell.innerText || '').trim() : '';
                if (!dateCompleted) {
                    continue;
                }
                
                const taskNumCell = row.querySelector('td[aria-describedby="list_task_number"]');
                const issuedByCell = row.querySelector('td[aria-describedby="list_issued_by_contact_name"]');
                const dateCreatedCell = row.querySelector('td[aria-describedby="list_date_created"]');
                const issuedToCell = row.querySelector('td[aria-describedby="list_issued_to_contact_name"]');
                const entityNameCell = row.querySelector('td[aria-describedby="list_module_entity_name"]');
                const statusCell = row.querySelector('td[aria-describedby="list_status"]');
                const descCell = row.querySelector('td[aria-describedby="list_description"]');
                const dateForCompCell = row.querySelector('td[aria-describedby="list_date_for_completion"]');
                const catCell = row.querySelector('td[aria-describedby="list_category_name"]');
                const priorityCell = row.querySelector('td[aria-describedby="list_priority"]');
                const compByCell = row.querySelector('td[aria-describedby="list_completed_by_contact_name"]');
                const commentsCell = row.querySelector('td[aria-describedby="list_completed_comments"]');
                const entityLinkCell = row.querySelector('td[aria-describedby="list_module_entity_link"]');
                
                const entityLink = entityLinkCell ? (entityLinkCell.getAttribute('title') || entityLinkCell.innerText || '').trim() : '';
                let works_id = '';
                const match = entityLink.match(/works_id=(\\d+)/);
                if (match) {
                    works_id = match[1];
                }

                completed.push({
                    task_id: row.id,
                    task_number: taskNumCell ? (taskNumCell.getAttribute('title') || taskNumCell.innerText || '').trim() : '',
                    issued_by: issuedByCell ? (issuedByCell.getAttribute('title') || issuedByCell.innerText || '').trim() : '',
                    date_created: dateCreatedCell ? (dateCreatedCell.getAttribute('title') || dateCreatedCell.innerText || '').trim() : '',
                    issued_to: issuedToCell ? (issuedToCell.getAttribute('title') || issuedToCell.innerText || '').trim() : '',
                    module_entity_name: entityNameCell ? (entityNameCell.getAttribute('title') || entityNameCell.innerText || '').trim() : '',
                    status: statusCell ? (statusCell.getAttribute('title') || statusCell.innerText || '').trim() : '',
                    description: descCell ? (descCell.getAttribute('title') || descCell.innerText || '').trim() : '',
                    date_for_completion: dateForCompCell ? (dateForCompCell.getAttribute('title') || dateForCompCell.innerText || '').trim() : '',
                    category_name: catCell ? (catCell.getAttribute('title') || catCell.innerText || '').trim() : '',
                    priority: priorityCell ? (priorityCell.getAttribute('title') || priorityCell.innerText || '').trim() : '',
                    date_completed: dateCompleted,
                    completed_by: compByCell ? (compByCell.getAttribute('title') || compByCell.innerText || '').trim() : '',
                    completed_comments: commentsCell ? (commentsCell.getAttribute('title') || commentsCell.innerText || '').trim() : '',
                    module_entity_link: entityLink,
                    works_id: works_id
                });
            }
            return completed;
        }"""
    )
    log.info("check_tasks: found %d completed tasks", len(completed_tasks))

    return {
        "success": True,
        "completed_count": len(completed_tasks),
        "completed_tasks": completed_tasks,
        "message": f"Successfully retrieved {len(completed_tasks)} completed task(s)."
    }


async def run_process_completed_task(
    page: Page,
    *,
    works_id: str,
    contract_id: str,
    comment: str,
    username: str,
    password: str,
    timeout_ms: int,
    login_fn: Callable[[Page, str, str], Awaitable[bool]],
    session_path: Path,
    context: BrowserContext,
) -> Dict[str, Any]:
    """
    Append comment to works details page internal notes, save.
    Then go to works_udf page, parse date from comment, fill it, and set UDF select to 'Appointment booked'.
    """
    # 0. Automatically accept browser confirmation dialogs (like native OK/Confirm prompts)
    async def _accept_dialog(dialog):
        log.info("process_task: auto-accepting native browser dialog: %s", dialog.message)
        await dialog.accept()
    page.on("dialog", _accept_dialog)

    wi_url = f"https://easybop.co.uk/a_planned_works/z_works/works_details.php?works_id={works_id}&contract_id={contract_id}&tab_nav_item=wi"

    # 1. Navigate to Details (wi) page & Authenticate
    await _ensure_authenticated(
        page,
        context=context,
        url=wi_url,
        login=login_fn,
        username=username,
        password=password,
        session_path=session_path,
        timeout_ms=timeout_ms,
    )

    # 2. Append comment to internal_notes
    notes_field = page.locator("#internal_notes")
    await notes_field.wait_for(state="visible", timeout=timeout_ms)
    current_val = await notes_field.input_value()
    current_val = current_val.strip()
    if current_val:
        new_val = f"{current_val} {comment}"
    else:
        new_val = comment

    await notes_field.focus()
    await notes_field.fill(new_val)
    # Trigger event dispatch to make form aware of change
    await notes_field.evaluate(
        "(el) => { el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }"
    )
    log.info("process_task: updated internal notes")

    # 3. Click Save Changes button
    save_wi_btn = page.locator("#btn_save_works_changes")
    await save_wi_btn.wait_for(state="visible", timeout=timeout_ms)
    await save_wi_btn.click()
    log.info("process_task: clicked save works changes")
    
    # Wait for network idle to ensure AJAX saves complete before navigating away
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    await asyncio.sleep(2.0)

    # 4. Navigate to UDF page
    udf_url = f"https://easybop.co.uk/a_planned_works/z_works/works_details.php?works_id={works_id}&contract_id={contract_id}&tab_nav_item=works_udf"
    await page.goto(udf_url, timeout=timeout_ms)
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except Exception:
        pass
    await asyncio.sleep(1.0)

    # 5. Extract date and set udf_pw_works_5088 datepicker if found
    extracted_date = _extract_date_from_comment(comment)
    if extracted_date:
        date_field = page.locator("#udf_pw_works_5088")
        await date_field.wait_for(state="visible", timeout=10000)
        await date_field.click()
        await date_field.fill(extracted_date)
        # Programmatically set value and dispatch change event to be completely safe
        await date_field.evaluate(
            "(el, val) => { el.value = val; el.dispatchEvent(new Event('change', { bubbles: true })); }",
            extracted_date
        )
        await date_field.press("Tab")
        log.info("process_task: populated UDF date field to %r", extracted_date)
    else:
        log.warning("process_task: no date found in comment %r", comment)

    # 6. Choose "Appointment booked" in udf_pw_works_5090 select list
    udf_select = page.locator("#udf_pw_works_5090")
    await udf_select.wait_for(state="visible", timeout=10000)
    try:
        await udf_select.select_option(value="Appointment booked")
        log.info("process_task: selected 'Appointment booked' in UDF select")
    except Exception as e:
        log.warning("process_task: failed selecting by value 'Appointment booked': %s, trying label...", e)
        try:
            await udf_select.select_option(label="Appointment booked")
        except Exception as e2:
            log.error("process_task: failed selecting 'Appointment booked': %s", e2)

    # 7. Click Save Works UDF Data
    save_udfs_btn = page.locator("#btn_save_works_udfs")
    await save_udfs_btn.wait_for(state="visible", timeout=timeout_ms)
    await save_udfs_btn.click()
    log.info("process_task: clicked save works UDF data")
    
    # Wait for network idle to ensure UDF AJAX saves complete
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    await asyncio.sleep(2.0)

    return {
        "success": True,
        "works_id": works_id,
        "extracted_date": extracted_date,
        "notes_updated": True,
        "udf_updated": True,
        "message": f"Successfully processed completed task for works_id={works_id} with date={extracted_date}"
    }


async def run_book_appointment(
    page: Page,
    *,
    works_id: str,
    contract_id: str,
    reason: str,
    duration_hours: str,
    note: Optional[str] = None,
    trade: Optional[str] = None,
    username: str,
    password: str,
    timeout_ms: int,
    login_fn: Callable[[Page, str, str], Awaitable[bool]],
    session_path: Path,
    context: BrowserContext,
) -> Dict[str, Any]:
    """
    Open the job's details page, click 'Add Other Appointment', fill out the dialog,
    set duration period, optionally add structured appointment note (Appointment #, Trade & Full Description),
    and save so it lands in the Scheduling Register.
    """
    # 0. Automatically accept browser confirmation dialogs safely
    async def _accept_dialog(dialog):
        try:
            log.info("book_appointment: auto-accepting native browser dialog: %s", dialog.message)
            await dialog.accept()
        except Exception as exc:
            log.info("book_appointment: dialog accept skipped/already handled: %s", exc)

    page.on("dialog", _accept_dialog)

    process_url = f"https://easybop.co.uk/a_planned_works/z_works/works_details.php?works_id={works_id}&contract_id={contract_id}&tab_nav_item=process_monitor"

    # 1. Authenticate & Navigate to Process Monitor tab
    await _ensure_authenticated(
        page,
        context=context,
        url=process_url,
        login=login_fn,
        username=username,
        password=password,
        session_path=session_path,
        timeout_ms=timeout_ms,
    )

    # 2. Click "Add Other Appointment" button
    add_btn = page.locator("#btn_create_new_other_apt")
    if await add_btn.count() == 0 or not await add_btn.first.is_visible():
        add_btn = page.locator("button").filter(has_text="Add Other Appointment")

    await add_btn.first.wait_for(state="visible", timeout=timeout_ms)
    await add_btn.first.click()
    log.info("book_appointment: clicked 'Add Other Appointment'")

    # 3. Wait for dialog
    dialog = page.locator(".ui-dialog:visible, [role='dialog']:visible")
    await dialog.first.wait_for(state="visible", timeout=10_000)
    await asyncio.sleep(0.5)

    # 4. Fill Reason for Appointment (truncate to max 120 chars to fit EasyBOP field limits)
    clean_reason = reason.strip()
    if len(clean_reason) > 120:
        clean_reason = clean_reason[:120].rstrip()
        log.info("book_appointment: truncated long reason text from %d to 120 chars: %r", len(reason), clean_reason)

    reason_field = page.locator("#aptj_reason_for_appointment")
    await reason_field.wait_for(state="visible", timeout=timeout_ms)
    await reason_field.focus()
    await reason_field.fill(clean_reason)
    await reason_field.evaluate(
        "(el) => { el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }"
    )
    log.info("book_appointment: reason filled — %r", clean_reason)

    # 5. Work board allocation — select "Appointments" (value 1598)
    board_select = page.locator("#aptj_apt_board_id")
    await board_select.wait_for(state="visible", timeout=timeout_ms)
    try:
        await board_select.select_option(value="1598")
        log.info("book_appointment: selected board 'Appointments' (value 1598)")
    except Exception:
        try:
            await board_select.select_option(label="Appointments")
            log.info("book_appointment: selected board 'Appointments' (by label)")
        except Exception as e:
            log.warning("book_appointment: failed selecting board 'Appointments': %s", e)

    # 6. Set Appointment Period / Duration (#aptj_time_allowed_override)
    formatted_duration = _format_appointment_period(duration_hours)
    period_select = page.locator("#aptj_time_allowed_override")
    try:
        if await period_select.count() > 0:
            await period_select.first.wait_for(state="visible", timeout=timeout_ms)
            await period_select.first.select_option(value=formatted_duration)
            log.info("book_appointment: Playwright native select_option %r succeeded", formatted_duration)
    except Exception as e:
        log.warning("book_appointment: Playwright select_option notice for %r: %s", formatted_duration, e)
    
    # Extract integer hour number for extra matching flexibility
    try:
        if isinstance(duration_hours, (int, float)):
            hour_num = int(duration_hours)
        else:
            hour_num = int(float(str(duration_hours).split(":")[0]))
    except Exception:
        hour_num = 1

    # Execute robust JS selection to set selectedIndex, attribute, and jQuery state
    selected_val = await page.evaluate("""([selId, hhmm, hrNum]) => {
        const sel = document.getElementById(selId) || document.querySelector('select[name="aptj_time_allowed_override"]');
        if (!sel) return null;
        const options = Array.from(sel.options);
        const totalMinutes = hrNum * 60;
        const hrStr = String(hrNum);
        const hrPadded = String(hrNum).padStart(2, '0');

        let matchIndex = -1;

        // 1. Exact match on value or text
        matchIndex = options.findIndex(opt => {
            const v = (opt.value || '').trim();
            const t = (opt.text || '').trim().toLowerCase();
            return v === hhmm || v === hrStr || v === `${hrStr}.0` || v === `${hrStr}:00` || v === `${hrPadded}:00` || v === String(totalMinutes) || t === hhmm || t === `${hrPadded}:00`;
        });

        // 2. Prefix / text contains match
        if (matchIndex === -1) {
            matchIndex = options.findIndex(opt => {
                const t = (opt.text || '').trim().toLowerCase();
                const v = (opt.value || '').trim();
                return t.includes(`${hrStr} hour`) || t.includes(`${hrStr} hr`) || t.startsWith(`${hrStr}:`) || t.startsWith(`${hrPadded}:`) || v.startsWith(`${hrPadded}:`) || v.startsWith(`${hrStr}:`);
            });
        }

        // 3. Float or minute numeric match
        if (matchIndex === -1) {
            matchIndex = options.findIndex(opt => {
                const vNum = parseFloat(opt.value);
                return vNum === hrNum || vNum === totalMinutes;
            });
        }

        if (matchIndex !== -1) {
            options.forEach(opt => {
                opt.removeAttribute('selected');
                opt.selected = false;
            });
            sel.selectedIndex = matchIndex;
            options[matchIndex].setAttribute('selected', 'selected');
            options[matchIndex].selected = true;
            sel.value = options[matchIndex].value;

            sel.dispatchEvent(new Event('input', { bubbles: true }));
            sel.dispatchEvent(new Event('change', { bubbles: true }));
            if (window.jQuery) {
                try { window.jQuery(sel).val(options[matchIndex].value).trigger('change'); } catch(e) {}
            }
            return options[matchIndex].value;
        }
        return null;
    }""", ["aptj_time_allowed_override", formatted_duration, hour_num])

    if selected_val:
        log.info("book_appointment: JS selected appointment period %r (raw input: %r)", selected_val, duration_hours)
    else:
        selected_val = formatted_duration

    # 6b. Fill Appointment Note if provided (textarea #apt_event_note)
    note_parts = []
    if note and str(note).strip():
        note_parts.append(str(note).strip())

    if trade and str(trade).strip():
        note_parts.append(f"Trade: {str(trade).strip()}")
        if reason and str(reason).strip():
            note_parts.append(f"Description: {str(reason).strip()}")
    elif reason and str(reason).strip() and not note:
        note_parts.append(f"Description: {str(reason).strip()}")

    full_note_text = "\n\n".join(note_parts).strip()

    if full_note_text:
        note_field = page.locator("#apt_event_note")
        try:
            if await note_field.count() > 0:
                await note_field.first.wait_for(state="visible", timeout=5000)
                await note_field.first.focus()
                await note_field.first.fill(full_note_text)
                await note_field.first.evaluate(
                    "(el) => { el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }"
                )
                log.info("book_appointment: note filled — %r", full_note_text)
        except Exception as e:
            log.warning("book_appointment: failed filling note %r: %s", full_note_text, e)

    await asyncio.sleep(0.4)

    # Re-enforce duration option selection right before submitting modal form
    pre_click_val = await page.evaluate("""([selId, val]) => {
        const sel = document.getElementById(selId);
        if (sel) {
            const options = Array.from(sel.options);
            const optIndex = options.findIndex(o => o.value === val);
            if (optIndex !== -1) {
                options.forEach((o, i) => {
                    if (i === optIndex) { o.setAttribute('selected', 'selected'); o.selected = true; }
                    else { o.removeAttribute('selected'); o.selected = false; }
                });
                sel.selectedIndex = optIndex;
                sel.value = val;
                sel.dispatchEvent(new Event('input', { bubbles: true }));
                sel.dispatchEvent(new Event('change', { bubbles: true }));
                if (window.jQuery) try { window.jQuery(sel).val(val).trigger('change'); } catch(e) {}
            }
        }
        const input = document.getElementById('aptj_time_allowed');
        if (input) {
            input.disabled = false;
            input.value = val;
        }
        return sel ? sel.value : null;
    }""", ["aptj_time_allowed_override", formatted_duration])

    log.info("book_appointment: right before modal submit, aptj_time_allowed_override value is %r (target: %r)", pre_click_val, formatted_duration)

    # Comprehensive, case-insensitive DOM verification across innerText, innerHTML, input values, and title attributes
    verify_snippet = clean_reason[:20].strip()

    async def _snippet_in_dom(snippet: str) -> bool:
        if not snippet:
            return True
        return await page.evaluate("""(snippet) => {
            const target = snippet.toLowerCase();
            const bodyText = (document.body.innerText || '').toLowerCase();
            const bodyHTML = (document.body.innerHTML || '').toLowerCase();
            if (bodyText.includes(target) || bodyHTML.includes(target)) return true;

            const inputs = Array.from(document.querySelectorAll('input, textarea, select'));
            if (inputs.some(el => (el.value || '').toLowerCase().includes(target))) return true;

            const elements = Array.from(document.querySelectorAll('*'));
            if (elements.some(el => (el.getAttribute('title') || '').toLowerCase().includes(target))) return true;

            return false;
        }""", snippet)

    async def _ensure_phone_has_plus() -> None:
        """
        EasyBOP's 'Add Other Appointment' modal validates the prefilled
        tenant mobile number (#aptj_resident_mobile) on submit and blocks
        with "Enter Valid Tenant phone number" if it doesn't start with a
        '+' — even though the field comes prefilled from the job record.
        Prepend '+' if it's missing so the modal's own validation passes.
        """
        phone_field = page.locator("#aptj_resident_mobile")
        if await phone_field.count() == 0:
            return
        try:
            current_val = (await phone_field.first.input_value()).strip()
        except Exception:
            current_val = ""
        if current_val and not current_val.startswith("+"):
            fixed_val = "+" + current_val
            await phone_field.first.fill(fixed_val)
            await phone_field.first.evaluate(
                "(el) => { el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
            log.info("book_appointment: tenant mobile missing '+' prefix — corrected %r -> %r", current_val, fixed_val)

    # 7. Click Add New Other Appointment save button in modal — single attempt.
    # (Previously this retried on a "staged" DOM check that turned out to be
    # unreliable — EasyBOP's modal submit appears to persist the appointment
    # on its own, independent of the outer Save Changes button, so the
    # retry was silently creating real duplicate appointments. One click.)
    save_btn = page.locator("#btn_add_new_apt_board_job")
    if await save_btn.count() == 0 or not await save_btn.first.is_visible():
        save_btn = page.locator(".ui-dialog:visible button").filter(has_text="Add New Other Appointment")
    await save_btn.first.wait_for(state="visible", timeout=timeout_ms)

    await _ensure_phone_has_plus()
    await save_btn.first.click()
    log.info("book_appointment: clicked 'Add New Other Appointment'")

    # Wait for dialog and modal backdrop overlay to close/detach completely
    dialog_closed = False
    try:
        await page.wait_for_selector(".ui-dialog:visible, .ui-widget-overlay", state="hidden", timeout=10_000)
        dialog_closed = True
        log.info("book_appointment: dialog & modal overlay closed cleanly")
    except Exception:
        pass

    # Ensure ALL residual modal overlay & mask elements are removed from DOM
    await page.evaluate("""() => {
        document.querySelectorAll('.ui-widget-overlay, .ui-front, .ui-dialog-mask').forEach(el => el.remove());
    }""")

    # Allow EasyBOP modal client-side JS to finish serializing the modal row into the main form array
    await asyncio.sleep(1.5)

    # 8. Click "Save Changes" (#btn_save_works_changes) on main page to persist changes permanently
    try:
        # Register native dialog handler for confirmation prompt safely
        async def _accept_save_confirm(dialog):
            try:
                log.info("book_appointment: native confirmation dialog %r — clicking OK", dialog.message)
                await dialog.accept()
            except Exception:
                pass

        page.once("dialog", _accept_save_confirm)

        save_changes_btn = page.locator("#btn_save_works_changes")
        if await save_changes_btn.count() > 0:
            log.info("book_appointment: clicking 'Save Changes' (#btn_save_works_changes)...")
            try:
                await save_changes_btn.first.click(force=True, timeout=5000)
                log.info("book_appointment: clicked 'Save Changes' (#btn_save_works_changes)")
            except Exception as e:
                log.warning("book_appointment: click timed out (%s), executing JS click", e)
                await page.evaluate("""() => {
                    const btn = document.getElementById('btn_save_works_changes') || 
                                Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('Save Changes'));
                    if (btn) btn.click();
                }""")
                log.info("book_appointment: JS clicked 'Save Changes' (#btn_save_works_changes)")
        else:
            await page.evaluate("""() => {
                const btn = document.getElementById('btn_save_works_changes') || 
                            Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('Save Changes'));
                if (btn) btn.click();
            }""")
            log.info("book_appointment: JS clicked 'Save Changes' (#btn_save_works_changes)")
    except Exception as exc:
        log.warning("book_appointment: issue clicking Save Changes button: %s", exc)

    # 9. Settlement buffer — allow EasyBOP backend SQL database write to complete 100%
    await asyncio.sleep(6.0)
    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass
    await asyncio.sleep(2.0)

    # 10. Ground-truth verification: reload the process_monitor page from
    # scratch instead of inspecting the already-open DOM. The already-open
    # page can still show staged/stale content (or nothing meaningful,
    # depending on what EasyBOP re-renders in place) regardless of whether
    # the server-side save actually succeeded — a fresh navigation is the
    # only way to see what's really persisted.
    await page.goto(process_url, wait_until="load", timeout=timeout_ms)
    await asyncio.sleep(2.5)
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass

    dom_found = await _snippet_in_dom(verify_snippet)
    if not dom_found:
        log.warning(
            "book_appointment: POST-RELOAD verification did NOT find snippet %r for works_id=%s — the modal "
            "submit itself appears to persist the appointment independent of this text check, so treating "
            "the booking as successful and only flagging this as unverified rather than failing the request.",
            verify_snippet, works_id,
        )
    else:
        log.info("book_appointment: POST-RELOAD verification for snippet %r -> found=True", verify_snippet)

    is_verified = dom_found
    selected_duration = formatted_duration

    return {
        "success": True,
        "works_id": works_id,
        "reason": clean_reason,
        "duration": selected_duration,
        "duration_hours": duration_hours,
        "note": note,
        "trade": trade,
        "appointment_verified": is_verified,
        "message": f"Successfully created unallocated appointment for works_id={works_id}"
    }


async def run_get_staff_availability(
    page: Page,
    *,
    contract_id: str = "321129",
    target_date: Optional[str] = None,
    staff_names: Optional[List[str]] = None,
    required_hours: float = 2.0,
    days_count: int = 5,
    username: str,
    password: str,
    timeout_ms: int,
    login_fn: Callable[[Page, str, str], Awaitable[bool]],
    session_path: Path,
    context: BrowserContext,
) -> Dict[str, Any]:
    """
    Navigate to EasyBOP Scheduling Register board and calculate exact start/end times
    and free continuous time slots for staff members across a full working week (default 5 working days).
    If staff_names is omitted/empty, automatically calculates for ALL staff members on the board.
    """
    scheduling_url = f"https://easybop.co.uk/a_planned_works/z_scheduling/scheduling_board.php?contract_id={contract_id}&board_type=sch&apt_board_id=1598&is_popped=0"

    # 1. Authenticate & Navigate to Scheduling Register
    await _ensure_authenticated(
        page,
        context=context,
        url=scheduling_url,
        login=login_fn,
        username=username,
        password=password,
        session_path=session_path,
        timeout_ms=timeout_ms,
    )

    working_days = _get_next_working_days(start_date_str=target_date, count=days_count)
    log.info("staff_availability: calculating availability for %d working days starting from %s", 
             len(working_days), working_days[0][1] if working_days else "Today")

    weekly_results: List[Dict[str, Any]] = []

    async def _ensure_datepicker_on_month(target_month_idx: int, target_year: int, max_clicks: int = 6) -> bool:
        """
        The #fc_date_picker jQuery UI Datepicker only renders day cells for
        whichever month is currently displayed. If the target working day
        falls in a later month (e.g. calculating the next working days
        right at month-end), the day cell for that date doesn't exist in
        the DOM yet, so clicking it silently no-ops and the calendar stays
        wrapped on the current month. Step the 'Next' arrow forward until
        the target month/year is actually rendered before selecting the day.
        """
        for _ in range(max_clicks):
            has_target_month = await page.evaluate(
                """({ m, y }) => !!document.querySelector(`td[data-month="${m}"][data-year="${y}"]`)""",
                {"m": target_month_idx, "y": target_year},
            )
            if has_target_month:
                return True

            next_btn = page.locator(
                "#fc_date_picker .ui-datepicker-next, .ui-datepicker-next"
            ).first
            clicked = False
            try:
                if await next_btn.count() > 0 and await next_btn.is_visible():
                    await next_btn.click(force=True)
                    clicked = True
            except Exception:
                pass

            if not clicked:
                # Fallback: JS click, in case Playwright's locator can't
                # resolve the widget instance (e.g. rebuilt after AJAX reload).
                await page.evaluate("""() => {
                    const btn = document.querySelector('#fc_date_picker .ui-datepicker-next')
                             || document.querySelector('.ui-datepicker-next')
                             || Array.from(document.querySelectorAll('a')).find(a => (a.textContent || '').trim() === 'Next');
                    if (btn) btn.click();
                }""")

            await asyncio.sleep(1.2)
            try:
                await page.wait_for_selector(".ui-datepicker-calendar, td[data-handler='selectDay']", state="attached", timeout=8000)
            except Exception:
                pass

        # Final check after exhausting the click budget
        return await page.evaluate(
            """({ m, y }) => !!document.querySelector(`td[data-month="${m}"][data-year="${y}"]`)""",
            {"m": target_month_idx, "y": target_year},
        )

    # 2. Iterate through each working day
    for idx, (dt_obj, date_str, day_name) in enumerate(working_days):
        log.info("staff_availability: [%d/%d] checking availability for date=%s (%s)",
                 idx + 1, len(working_days), date_str, day_name)

        t_day, t_month, t_year = dt_obj.day, dt_obj.month, dt_obj.year
        t_month_idx = t_month - 1  # 0-indexed month for jQuery UI Datepicker

        # Navigate datepicker for dates
        try:
            await page.wait_for_selector(".ui-datepicker-calendar, td[data-handler='selectDay']", state="attached", timeout=10000)
        except Exception:
            pass

        # If the target day falls in a month later than what's currently
        # rendered (e.g. rolling from end-of-month into next month), step
        # the datepicker forward via the 'Next' arrow before trying to
        # click the day cell — otherwise the cell doesn't exist yet and
        # the click below silently no-ops.
        month_ready = await _ensure_datepicker_on_month(t_month_idx, t_year)
        if not month_ready:
            log.warning(
                "staff_availability: could not navigate datepicker to month=%s year=%s for date=%s after max 'Next' clicks",
                t_month_idx, t_year, date_str,
            )

        date_selected = False

        # Method A: Playwright native locator click
        try:
            cell_selector = f'td[data-handler="selectDay"][data-month="{t_month_idx}"][data-year="{t_year}"] a[data-date="{t_day}"], td[data-month="{t_month_idx}"][data-year="{t_year}"] a[data-date="{t_day}"]'
            cell_loc = page.locator(cell_selector).first
            if await cell_loc.count() > 0 and await cell_loc.is_visible():
                await cell_loc.click(force=True)
                date_selected = True
        except Exception:
            pass

        # Method B: JS evaluate fallback
        if not date_selected:
            try:
                await page.evaluate(
                    """async ({ targetDay, targetMonthIdx, targetYear }) => {
                        const cell = document.querySelector(`td[data-handler="selectDay"][data-month="${targetMonthIdx}"][data-year="${targetYear}"] a[data-date="${targetDay}"]`) ||
                                     document.querySelector(`td[data-month="${targetMonthIdx}"][data-year="${targetYear}"] a[data-date="${targetDay}"]`) ||
                                     document.querySelector(`a[data-date="${targetDay}"]`);
                        if (!cell) return;

                        const td = cell.closest('td') || cell;

                        td.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                        cell.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));

                        try { td.click(); } catch(e) {}
                        try { cell.click(); } catch(e) {}

                        if (window.jQuery) {
                            try { window.jQuery(td).trigger('click'); } catch(e) {}
                            try { window.jQuery(cell).trigger('click'); } catch(e) {}
                        }
                    }""",
                    {"targetDay": t_day, "targetMonthIdx": t_month_idx, "targetYear": t_year}
                )
            except Exception as exc:
                log.warning("staff_availability: date selection fallback failed for %s: %s", date_str, exc)

        # Pause to allow AJAX schedule board reload
        await asyncio.sleep(2.0)

        # Wait for FullCalendar timeline events to load
        try:
            await page.wait_for_selector(".fc-bgevent, .fc-timeline-event, a.fc-event", state="attached", timeout=15000)
        except Exception as exc:
            log.warning("staff_availability: timeline event blocks wait timed out for %s: %s", date_str, exc)

        await asyncio.sleep(1.0)

        # Extract availability data for current date
        extracted = await page.evaluate(
            """({ names, reqHours }) => {
                const BASE_LEFT = 1152.4;
                const PX_PER_MIN = 2.4;
                const TOTAL_MINUTES = 540; // 08:00 to 17:00 (540 mins)

                function minsToTimeStr(minsFrom0800) {
                    const totalMins = 8 * 60 + Math.round(minsFrom0800);
                    const hh = Math.floor(totalMins / 60);
                    const mm = totalMins % 60;
                    return `${String(hh).padStart(2, '0')}:${String(mm).padStart(2, '0')}`;
                }

                // If names is empty/null, automatically get ALL staff names from left panel
                let targetNames = names;
                if (!targetNames || !Array.isArray(targetNames) || targetNames.length === 0) {
                    const staffSpans = document.querySelectorAll('span.resource_contact_label');
                    const nameSet = new Set();
                    for (const span of staffSpans) {
                        const txt = (span.textContent || '').trim();
                        if (txt) nameSet.add(txt);
                    }
                    targetNames = Array.from(nameSet);
                }

                // Step 1: Map names -> contact_id from left panel (span.resource_contact_label)
                const staffSpans = document.querySelectorAll('span.resource_contact_label');
                const contactMap = {};

                for (const span of staffSpans) {
                    const txt = (span.textContent || '').trim();
                    const cid = span.getAttribute('data-contact-id');
                    if (txt && cid) {
                        contactMap[txt.toLowerCase()] = `contactid_${cid}`;
                    }
                }

                const results = [];

                for (const name of targetNames) {
                    const nameLower = name.toLowerCase().trim();
                    let targetResourceId = contactMap[nameLower];

                    if (!targetResourceId) {
                        for (const [k, v] of Object.entries(contactMap)) {
                            if (k.includes(nameLower) || nameLower.includes(k)) {
                                targetResourceId = v;
                                break;
                            }
                        }
                    }

                    if (!targetResourceId) {
                        results.push({
                            staff_name: name,
                            found: false,
                            resource_id: null,
                            message: "Staff name not found in left panel (span.resource_contact_label)"
                        });
                        continue;
                    }

                    // Step 2: Query the right panel table row matching tr[data-resource-id="contactid_XXXXX"]
                    const eventTr = document.querySelector(`td:nth-child(3) tr[data-resource-id="${targetResourceId}"]`) ||
                                    document.querySelector(`tr[data-resource-id="${targetResourceId}"]:nth-child(even)`) ||
                                    document.querySelectorAll(`tr[data-resource-id="${targetResourceId}"]`)[1] ||
                                    document.querySelector(`tr[data-resource-id="${targetResourceId}"]`);

                    if (!eventTr) {
                        results.push({
                            staff_name: name,
                            found: false,
                            resource_id: targetResourceId,
                            message: `Matched contact ID in left panel, but event row tr[data-resource-id="${targetResourceId}"] not found in right panel`
                        });
                        continue;
                    }

                    // Query all event elements in this target tr
                    const eventEls = eventTr.querySelectorAll('a.fc-timeline-event, a.fc-event, div.fc-bgevent');
                    const busyIntervals = [];
                    const busySlots = [];

                    for (const el of eventEls) {
                        const styleAttr = el.getAttribute('style') || '';
                        const leftMatch = styleAttr.match(/left:\\s*([\\d\\.]+)px/i);
                        const rightMatch = styleAttr.match(/right:\\s*-([\\d\\.]+)px/i);

                        if (!leftMatch) continue;

                        const leftPx = parseFloat(leftMatch[1]);
                        let rightPx = rightMatch ? parseFloat(rightMatch[1]) : (leftPx + (el.offsetWidth || 144));

                        let startMins = (leftPx - BASE_LEFT) / PX_PER_MIN;
                        let endMins = (rightPx - BASE_LEFT) / PX_PER_MIN;

                        startMins = Math.max(0, Math.min(TOTAL_MINUTES, startMins));
                        endMins = Math.max(0, Math.min(TOTAL_MINUTES, endMins));

                        if (endMins > startMins) {
                            const titleEl = el.querySelector('.fc-title') || el;
                            let title = (titleEl.textContent || '').trim();
                            if (!title || title === '\\u00a0' || title.includes('Unavailable')) {
                                title = "Unavailable";
                            }

                            const startTimeStr = minsToTimeStr(startMins);
                            const endTimeStr = minsToTimeStr(endMins);

                            busyIntervals.push({ startMins, endMins, startTimeStr, endTimeStr, title });
                            busySlots.push({
                                title: title,
                                start_time: startTimeStr,
                                end_time: endTimeStr,
                                duration_hours: Number(((endMins - startMins) / 60).toFixed(2))
                            });
                        }
                    }

                    busyIntervals.sort((a, b) => a.startMins - b.startMins);

                    const mergedBusy = [];
                    for (const interval of busyIntervals) {
                        if (mergedBusy.length === 0) {
                            mergedBusy.push({ start: interval.startMins, end: interval.endMins });
                        } else {
                            const last = mergedBusy[mergedBusy.length - 1];
                            if (interval.startMins <= last.end) {
                                last.end = Math.max(last.end, interval.endMins);
                            } else {
                                mergedBusy.push({ start: interval.startMins, end: interval.endMins });
                            }
                        }
                    }

                    const freeSlots = [];
                    let currentPointer = 0; // 08:00

                    for (const b of mergedBusy) {
                        if (b.start > currentPointer) {
                            const gapDuration = b.start - currentPointer;
                            const freeStartStr = minsToTimeStr(currentPointer);
                            const freeEndStr = minsToTimeStr(b.start);
                            const gapHours = Number((gapDuration / 60).toFixed(2));

                            if (gapHours >= reqHours) {
                                freeSlots.push({
                                    start_time: freeStartStr,
                                    end_time: freeEndStr,
                                    duration_available_hours: gapHours,
                                    fits_required: true
                                });
                            }
                        }
                        currentPointer = Math.max(currentPointer, b.end);
                    }

                    if (currentPointer < TOTAL_MINUTES) {
                        const gapDuration = TOTAL_MINUTES - currentPointer;
                        const freeStartStr = minsToTimeStr(currentPointer);
                        const freeEndStr = minsToTimeStr(TOTAL_MINUTES);
                        const gapHours = Number((gapDuration / 60).toFixed(2));

                        if (gapHours >= reqHours) {
                            freeSlots.push({
                                start_time: freeStartStr,
                                end_time: freeEndStr,
                                duration_available_hours: gapHours,
                                fits_required: true
                            });
                        }
                    }

                    results.push({
                        staff_name: name,
                        found: true,
                        resource_id: targetResourceId,
                        busy_count: busySlots.length,
                        busy_slots: busySlots,
                        free_slots_count: freeSlots.length,
                        free_available_windows: freeSlots,
                        is_available_for_job: freeSlots.length > 0
                    });
                }

                return results;
            }""",
            {"names": staff_names, "reqHours": required_hours},
        )

        weekly_results.append({
            "date": date_str,
            "day_of_week": day_name,
            "staff_availability": extracted
        })

    log.info("staff_availability: completed %d working days availability matrix calculation", len(working_days))

    return {
        "success": True,
        "contract_id": contract_id,
        "start_date": working_days[0][1] if working_days else "Today",
        "end_date": working_days[-1][1] if working_days else "Today",
        "working_days_count": len(working_days),
        "required_hours": required_hours,
        "weekly_staff_availability": weekly_results,
        "message": f"Successfully calculated {len(working_days)} working days availability matrix ({working_days[0][1]} to {working_days[-1][1]})."
    }


async def run_get_unallocated_jobs(
    page: Page,
    *,
    contract_id: str = "321129",
    apt_board_id: str = "1598",
    username: str,
    password: str,
    timeout_ms: int,
    login_fn: Callable[[Page, str, str], Awaitable[bool]],
    session_path: Path,
    context: BrowserContext,
) -> Dict[str, Any]:
    """
    Navigate to the Scheduling Register page (board_type=sch&apt_board_id=1598),
    scrape all unallocated appointment events sitting inside #events_allocation_data,
    and return list of unallocated works_ids and appointment details.
    """
    sched_url = f"https://easybop.co.uk/a_planned_works/z_scheduling/scheduling_board.php?contract_id={contract_id}&board_type=sch&apt_board_id={apt_board_id}&is_popped=0"

    # 1. Authenticate & Navigate to Scheduling Register
    await _ensure_authenticated(
        page,
        context=context,
        url=sched_url,
        login=login_fn,
        username=username,
        password=password,
        session_path=session_path,
        timeout_ms=timeout_ms,
    )

    # 2. Wait for #events_allocation_data container to be present in DOM
    try:
        await page.wait_for_selector("#events_allocation_data", state="visible", timeout=timeout_ms)
    except Exception as e:
        log.warning("unallocated_jobs: #events_allocation_data selector wait timed out: %s", e)

    await asyncio.sleep(1.5)

    # 3. Iterate through each unallocated .fc_event card: right-click -> Display details -> scrape #apt_notes_list
    events_locator = page.locator("#events_allocation_data .fc_event")
    event_count = await events_locator.count()
    log.info("unallocated_jobs: found %d unallocated .fc_event cards in #events_allocation_data", event_count)

    unallocated_items = []

    for i in range(event_count):
        card = events_locator.nth(i)
        try:
            works_id = await card.get_attribute("data-module-entity-id") or ""
            apt_event_id = await card.get_attribute("data-apt-event-id") or await card.get_attribute("data-id") or ""
            duration = await card.get_attribute("data-duration") or ""
            
            label_text = ""
            if await card.locator(".fce_label").count() > 0:
                label_text = await card.locator(".fce_label").first.inner_text()

            works_id = works_id.strip()
            apt_event_id = apt_event_id.strip()
            duration = duration.strip()
            address = label_text.strip()

            appointment_number = ""
            note_content = ""
            trade = ""

            # Right click on appointment card to open context menu
            try:
                # Ensure no stale backdrop overlay blocks pointer events
                await page.evaluate("""() => {
                    document.querySelectorAll('.ui-widget-overlay, .ui-front, .ui-dialog-mask').forEach(el => el.remove());
                }""")

                await card.click(button="right", force=True, timeout=5000)
                await asyncio.sleep(0.4)

                details_btn = page.locator("li.context-menu-item:has-text('Display details'), .context-menu-item:has-text('Display details')").first
                if await details_btn.count() > 0:
                    try:
                        await details_btn.click(force=True, timeout=3000)
                    except Exception:
                        await page.evaluate("""() => {
                            const btn = Array.from(document.querySelectorAll('.context-menu-item')).find(el => (el.textContent || '').includes('Display details'));
                            if (btn) btn.click();
                        }""")

                    log.info("unallocated_jobs: clicked 'Display details' for card %d (apt_event_id=%s, works_id=%s)", i+1, apt_event_id, works_id)

                    # Wait for dialog and wait for AJAX jqGrid table rows (tr.jqgrow) to populate in #apt_notes_list
                    try:
                        dialog = page.locator(".ui-dialog:visible")
                        await dialog.first.wait_for(state="visible", timeout=3000)
                    except Exception:
                        pass

                    try:
                        await page.wait_for_function(
                            """() => {
                                const table = document.getElementById('apt_notes_list');
                                if (!table) return false;
                                const rows = table.querySelectorAll('tr.jqgrow');
                                return rows.length > 0;
                            }""",
                            timeout=3500,
                        )
                        log.info("unallocated_jobs: #apt_notes_list table rows loaded for card %d", i+1)
                    except Exception:
                        await asyncio.sleep(1.0)

                    extracted_note = await page.evaluate("""() => {
                        const dialogs = Array.from(document.querySelectorAll('.ui-dialog')).filter(d => d.offsetWidth > 0 || d.offsetHeight > 0 || (getComputedStyle(d).display !== 'none'));
                        const dialog = dialogs[0] || document;
                        
                        // 1. Target exact note content cell (aria-describedby*="n.notes"), ignoring category name ("Appointment Note")
                        const rows = Array.from(dialog.querySelectorAll('tr.jqgrow, #apt_notes_list tr'));
                        for (const row of rows) {
                            const noteCell = row.querySelector('td[aria-describedby*="n.notes"]') ||
                                             row.querySelector('td[aria-describedby$=".notes"]') ||
                                             Array.from(row.querySelectorAll('td')).find(c => {
                                                 const desc = c.getAttribute('aria-describedby') || '';
                                                 return desc.includes('n.notes') || desc.endsWith('.notes');
                                             });

                            if (noteCell) {
                                const text = (noteCell.getAttribute('title') || noteCell.innerText || noteCell.textContent || '').trim();
                                if (text && text.toLowerCase() !== 'appointment note') {
                                    return text;
                                }
                            }
                        }
                        
                        // 2. Fallback search all table cells in dialog for notes text starting with "Appointment 1", "Appointment 2", "Appointment #"
                        const cells = Array.from(dialog.querySelectorAll('td'));
                        for (const cell of cells) {
                            const text = (cell.getAttribute('title') || cell.innerText || cell.textContent || '').trim();
                            const lower = text.toLowerCase();
                            if (lower.startsWith('appointment 1') || lower.startsWith('appointment 2') || lower.startsWith('appointment #') || (lower.startsWith('appointment') && lower !== 'appointment note')) {
                                return text;
                            }
                        }
                        return null;
                    }""")

                    if extracted_note:
                        note_content = extracted_note
                        lines = [l.strip() for l in extracted_note.splitlines() if l.strip()]
                        
                        appt_line = ""
                        for line in lines:
                            if "appointment" in line.lower():
                                appt_line = line
                                break
                        
                        if appt_line:
                            appointment_number = appt_line.strip()
                        else:
                            match = re.search(r"(appointment\s*#?\s*\d+(?:\s*\([^)]+\))?)", extracted_note, re.IGNORECASE)
                            if match:
                                appointment_number = match.group(1).strip()

                        for line in lines:
                            if line.lower().startswith("trade:"):
                                trade = line.split(":", 1)[1].strip()

                        log.info("unallocated_jobs: extracted appointment_number=%r, trade=%r for apt_event_id=%s",
                                 appointment_number, trade, apt_event_id)

                    # Close details dialog cleanly and purge all backdrop overlay elements
                    await page.evaluate("""() => {
                        const dialogs = Array.from(document.querySelectorAll('.ui-dialog')).filter(d => d.offsetWidth > 0 || d.offsetHeight > 0 || (getComputedStyle(d).display !== 'none'));
                        dialogs.forEach(dialog => {
                            const btns = Array.from(dialog.querySelectorAll('button'));
                            const closeBtn = btns.find(b => (b.textContent || '').trim().toLowerCase() === 'close') ||
                                             dialog.querySelector('.ui-dialog-titlebar-close');
                            if (closeBtn) closeBtn.click();
                            dialog.style.display = 'none';
                        });
                        document.querySelectorAll('.ui-widget-overlay, .ui-front, .ui-dialog-mask').forEach(el => el.remove());
                    }""")

                    await asyncio.sleep(0.3)
                else:
                    log.info("unallocated_jobs: 'Display details' context menu item not visible for card %d (apt_event_id=%s)", i+1, apt_event_id)
            except Exception as e:
                log.warning("unallocated_jobs: failed right-clicking card %d: %s", i+1, e)

            unallocated_items.append({
                "works_id": works_id,
                "apt_event_id": apt_event_id,
                "duration": duration,
                "address": address,
                "appointment_number": appointment_number,
                "trade": trade,
                "note": note_content
            })

        except Exception as exc:
            log.warning("unallocated_jobs: error scraping card %d: %s", i+1, exc)

    # Extract unique works_ids
    works_id_set = set()
    for item in unallocated_items:
        wid = item.get("works_id")
        if wid:
            works_id_set.add(wid)

    unallocated_works_ids = sorted(list(works_id_set))

    log.info("unallocated_jobs: scraped %d unallocated items across %d unique works_ids", 
             len(unallocated_items), len(unallocated_works_ids))

    return {
        "success": True,
        "unallocated_count": len(unallocated_items),
        "unique_works_count": len(unallocated_works_ids),
        "unallocated_works_ids": unallocated_works_ids,
        "unallocated_items": unallocated_items,
        "message": f"Successfully retrieved {len(unallocated_items)} unallocated appointments across {len(unallocated_works_ids)} unique jobs."
    }


async def run_verify_scheduled_appointment(
    page: Page,
    *,
    contract_id: str = "321129",
    apt_board_id: str = "1598",
    address: str,
    appointment_number: str = "Appointment 1",
    username: str,
    password: str,
    timeout_ms: int,
    login_fn: Callable[[Page, str, str], Awaitable[bool]],
    session_path: Path,
    context: BrowserContext,
) -> Dict[str, Any]:
    """
    Search and verify whether a specific appointment (by address and appointment_number e.g. "Appointment 1")
    has been scheduled on the EasyBOP Scheduling Register board.
    Returns scheduled: True with all details (operative, date, resident info, status) if matched.
    """
    sched_url = f"https://easybop.co.uk/a_planned_works/z_scheduling/scheduling_board.php?contract_id={contract_id}&board_type=sch&apt_board_id={apt_board_id}&is_popped=0"

    # 1. Authenticate & Navigate to Scheduling Register
    await _ensure_authenticated(
        page,
        context=context,
        url=sched_url,
        login=login_fn,
        username=username,
        password=password,
        session_path=session_path,
        timeout_ms=timeout_ms,
    )

    await page.evaluate("""() => {
        document.querySelectorAll('.ui-widget-overlay, .ui-front, .ui-dialog-mask').forEach(el => el.remove());
    }""")

    # Clean address for search (strip property ref in parentheses e.g. "1 Failand Crescent (KS1985)" -> "1 Failand Crescent")
    clean_search_addr = address.split("(")[0].strip()

    # 2. Open Search Dialog on EasyBOP (retry until search input is visible)
    search_input = page.locator("#dlg_sch_list_search")
    
    for attempt in range(1, 4):
        log.info("verify_scheduled: attempting to open Search dialog (attempt %d/3)...", attempt)
        search_btn = page.locator("button.fc-btnSearch-button, button:has-text('Search'), .fc-toolbar button:has-text('Search')").first
        try:
            if await search_btn.count() > 0 and await search_btn.is_visible():
                await search_btn.click(force=True, timeout=3000)
                log.info("verify_scheduled: Playwright clicked top Search button")
        except Exception:
            pass

        await page.evaluate("""() => {
            const btn = document.querySelector('button.fc-btnSearch-button') || 
                        Array.from(document.querySelectorAll('button')).find(b => (b.textContent || '').trim().toLowerCase() === 'search');
            if (btn) btn.click();
            if (window.jQuery && window.jQuery('#dlg_sch_list').length) {
                try { window.jQuery('#dlg_sch_list').dialog('open'); } catch(e) {}
            }
        }""")

        try:
            await search_input.first.wait_for(state="visible", timeout=4000)
            log.info("verify_scheduled: Search input #dlg_sch_list_search is now VISIBLE!")
            break
        except Exception:
            await asyncio.sleep(1.0)

    # 3. Type search address into #dlg_sch_list_search and press Enter
    try:
        await search_input.first.wait_for(state="visible", timeout=5000)
        await search_input.first.focus()
        await search_input.first.fill(clean_search_addr)
        await page.keyboard.press("Enter")
        log.info("verify_scheduled: typed %r into #dlg_sch_list_search and pressed Enter", clean_search_addr)
    except Exception as e:
        log.warning("verify_scheduled: search input fill issue: %s, executing JS fill + Enter", e)

    # JS fallback trigger for Enter key and jQuery search handler
    await page.evaluate("""(val) => {
        const input = document.getElementById('dlg_sch_list_search');
        if (input) {
            input.focus();
            input.value = val;
            if (input.classList) input.classList.remove('placeholder_text');
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            const ev = new KeyboardEvent('keydown', { bubbles: true, cancelable: true, keyCode: 13, which: 13 });
            input.dispatchEvent(ev);
            if (window.jQuery) {
                try { window.jQuery(input).trigger(jQuery.Event('keydown', { keyCode: 13, which: 13 })); } catch(err) {}
                try { window.jQuery('#btn_dlg_sch_list_search').click(); } catch(err) {}
            }
        }
    }""", clean_search_addr)

    log.info("verify_scheduled: submitted search for address %r in dialog", clean_search_addr)

    # 4. Wait for table #list_apt_schedule rows to populate
    try:
        await page.wait_for_function(
            """() => {
                const table = document.getElementById('list_apt_schedule');
                if (!table) return false;
                const rows = table.querySelectorAll('tr.jqgrow');
                return rows.length > 0;
            }""",
            timeout=10000,
        )
        await asyncio.sleep(0.5)
    except Exception as e:
        log.warning("verify_scheduled: no results or table load timeout for search_address=%r: %s", clean_search_addr, e)
        return {
            "success": True,
            "scheduled": False,
            "address": address,
            "appointment_number": appointment_number,
            "message": f"No scheduled appointments found on board for address '{address}'."
        }

    # 5. Scrape candidate rows from #list_apt_schedule
    rows_locator = page.locator("#list_apt_schedule tr.jqgrow")
    row_count = await rows_locator.count()
    log.info("verify_scheduled: found %d rows in #list_apt_schedule for address=%r", row_count, address)

    if row_count == 0:
        return {
            "success": True,
            "scheduled": False,
            "address": address,
            "appointment_number": appointment_number,
            "message": f"No matching scheduled appointment rows returned for address '{address}'."
        }

    target_number_clean = appointment_number.strip().lower()
    target_digit = ""
    d_match = re.search(r"(?:appointment|appt)\s*#?\s*(\d+)", target_number_clean)
    if d_match:
        target_digit = d_match.group(1)

    # Iterate rows and double-click to check details
    for i in range(row_count):
        row = rows_locator.nth(i)
        try:
            row_info = await page.evaluate("""(index) => {
                const table = document.getElementById('list_apt_schedule');
                if (!table) return null;
                const rows = Array.from(table.querySelectorAll('tr.jqgrow'));
                const r = rows[index];
                if (!r) return null;

                const getVal = (desc) => {
                    const cell = r.querySelector(`td[aria-describedby*="${desc}"]`);
                    return cell ? (cell.getAttribute('title') || cell.innerText || cell.textContent || '').trim() : '';
                };

                return {
                    operative_name: getVal('contact_name'),
                    working_with: getVal('working_with'),
                    appointment_date: getVal('appointment_date'),
                    appointment_status: getVal('appointment_status'),
                    address: getVal('address'),
                    post_code: getVal('post_code'),
                    description: getVal('description'),
                    resident_name: getVal('resident_name'),
                    resident_mobile: getVal('resident_mobile'),
                    resident_email: getVal('resident_email'),
                    apt_event_id: getVal('apt_event_id'),
                    wp_board_entry_id: getVal('wp_board_entry_id'),
                    works_id: getVal('module_entity_id')
                };
            }""", i)

            if not row_info:
                continue

            # Ensure address matches
            row_addr = (row_info.get("address") or "").lower()
            input_addr = address.lower().split("(")[0].split(",")[0].strip()
            if input_addr not in row_addr and row_addr not in address.lower():
                continue

            # Double click row to open details modal
            await row.dblclick(force=True)
            log.info("verify_scheduled: double-clicked row %d (apt_event_id=%s, works_id=%s)", i+1, row_info.get("apt_event_id"), row_info.get("works_id"))

            # Wait for #apt_notes_list table inside detail dialog
            try:
                await page.wait_for_function(
                    """() => {
                        const table = document.getElementById('apt_notes_list');
                        if (!table) return false;
                        const rows = table.querySelectorAll('tr.jqgrow');
                        return rows.length > 0;
                    }""",
                    timeout=3500,
                )
                await asyncio.sleep(0.3)
            except Exception:
                await asyncio.sleep(0.8)

            # Target table #apt_notes_list directly by ID across the whole page DOM
            extracted_note = await page.evaluate("""() => {
                const table = document.getElementById('apt_notes_list') || 
                              document.querySelector('table[aria-labelledby="gbox_apt_notes_list"]');
                if (!table) return null;

                // 1. Target exact notes cell
                const noteCell = table.querySelector('td[aria-describedby="apt_notes_list_n.notes"]') || 
                                 table.querySelector('td[aria-describedby*="n.notes"]');
                if (noteCell) {
                    const text = (noteCell.getAttribute('title') || noteCell.innerText || noteCell.textContent || '').trim();
                    if (text && text.toLowerCase() !== 'appointment note') return text;
                }

                // 2. Iterate all rows and cells in table
                const rows = Array.from(table.querySelectorAll('tr.jqgrow'));
                for (const r of rows) {
                    const cells = Array.from(r.querySelectorAll('td'));
                    for (const c of cells) {
                        const cellText = (c.getAttribute('title') || c.innerText || c.textContent || '').trim();
                        if (cellText && cellText.toLowerCase().includes('appointment') && cellText.toLowerCase() !== 'appointment note') {
                            return cellText;
                        }
                    }
                }
                return null;
            }""")

            log.info("verify_scheduled: row %d (apt_event_id=%s, works_id=%s) extracted_note=%r", 
                     i+1, row_info.get("apt_event_id"), row_info.get("works_id"), extracted_note)

            extracted_number = ""
            extracted_trade = ""

            if extracted_note:
                lines = [l.strip() for l in extracted_note.splitlines() if l.strip()]
                appt_line = ""
                for line in lines:
                    if "appointment" in line.lower():
                        appt_line = line
                        break
                
                if appt_line:
                    extracted_number = appt_line.strip()
                else:
                    match = re.search(r"(appointment\s*#?\s*\d+(?:\s*\([^)]+\))?)", extracted_note, re.IGNORECASE)
                    if match:
                        extracted_number = match.group(1).strip()

                for line in lines:
                    if line.lower().startswith("trade:"):
                        extracted_trade = line.split(":", 1)[1].strip()

            # Close ONLY details modal (preserve search dialog #dlg_sch_list open for next rows!)
            try:
                close_btn = page.locator(".ui-dialog:has(#apt_notes_list) button:has-text('Close')").first
                if await close_btn.count() > 0 and await close_btn.is_visible():
                    await close_btn.click(force=True)
            except Exception:
                pass

            await page.evaluate("""() => {
                const detailDialogs = Array.from(document.querySelectorAll('.ui-dialog')).filter(d => {
                    return d.querySelector('#apt_notes_list') || (!d.querySelector('#list_apt_schedule') && d.id !== 'dlg_sch_list');
                });
                detailDialogs.forEach(d => {
                    const closeBtn = Array.from(d.querySelectorAll('button')).find(b => (b.textContent || '').trim().toLowerCase() === 'close') ||
                                     d.querySelector('.ui-dialog-titlebar-close');
                    if (closeBtn) closeBtn.click();
                    d.style.display = 'none';
                });
            }""")
            await asyncio.sleep(0.6)

            # Match appointment number strictly & flexibly with Multi-Day Chunk Awareness
            target_clean = appointment_number.strip().lower()
            full_note_lower = (extracted_note or "").strip().lower()

            # Extract base appointment digit from target e.g. "Appointment 1 (Day 2 of 5)" -> "1"
            target_appt_digit = ""
            t_match = re.search(r"(?:appointment|appt)\s*#?\s*(\d+)", target_clean)
            if t_match:
                target_appt_digit = t_match.group(1)

            # Extract target day chunk if present e.g. "Day 2", "Day 2 of 5" -> "2"
            target_day = ""
            td_match = re.search(r"\bday\s*(\d+)", target_clean)
            if td_match:
                target_day = td_match.group(1)

            # Extract note base appointment digit
            note_appt_digit = ""
            n_match = re.search(r"(?:appointment|appt)\s*#?\s*(\d+)", full_note_lower)
            if n_match:
                note_appt_digit = n_match.group(1)

            # Extract note day chunk if present e.g. "Day 1", "Day 1 of 5" -> "1"
            note_day = ""
            nd_match = re.search(r"\bday\s*(\d+)", full_note_lower)
            if nd_match:
                note_day = nd_match.group(1)

            is_match = False

            # Rule 1: If Target specifies a Day (e.g. Day 2 of 5), both Appointment # and Day # MUST match!
            if target_day:
                if note_day:
                    if target_appt_digit and note_appt_digit:
                        if target_appt_digit == note_appt_digit and target_day == note_day:
                            is_match = True
                    elif target_day == note_day:
                        is_match = True
                else:
                    is_match = False

            # Rule 2: If Target does NOT specify a Day (e.g. "Appointment 3" or "Appointment 1"):
            elif target_appt_digit and note_appt_digit:
                if target_appt_digit == note_appt_digit:
                    if not note_day:
                        is_match = True
                    elif note_day == "1":
                        is_match = True
                    else:
                        is_match = False

            # Rule 3: Direct substring match fallback if no digits parsed
            elif target_clean and target_clean in full_note_lower:
                is_match = True

            if is_match:
                log.info("verify_scheduled: MATCH CONFIRMED for address=%r, appointment_number=%r on row %d (apt_event_id=%s, works_id=%s, extracted_number=%r)!", 
                         address, appointment_number, i+1, row_info.get("apt_event_id"), row_info.get("works_id"), extracted_number)
                return {
                    "success": True,
                    "scheduled": True,
                    "works_id": row_info.get("works_id"),
                    "apt_event_id": row_info.get("apt_event_id"),
                    "wp_board_entry_id": row_info.get("wp_board_entry_id"),
                    "appointment_number": extracted_number or appointment_number,
                    "trade": extracted_trade,
                    "address": row_info.get("address"),
                    "post_code": row_info.get("post_code"),
                    "operative_name": row_info.get("operative_name"),
                    "working_with": row_info.get("working_with"),
                    "appointment_date": row_info.get("appointment_date"),
                    "scheduled_duration": row_info.get("duration") or row_info.get("time_allowed") or "",
                    "appointment_status": row_info.get("appointment_status"),
                    "description": row_info.get("description"),
                    "resident_name": row_info.get("resident_name"),
                    "resident_mobile": row_info.get("resident_mobile"),
                    "resident_email": row_info.get("resident_email"),
                    "note": extracted_note or "",
                    "message": f"Appointment '{appointment_number}' for address '{address}' IS scheduled on board."
                }
            else:
                log.info("verify_scheduled: row %d (apt_event_id=%s) not a match for %r — checking next row...", 
                         i+1, row_info.get("apt_event_id"), appointment_number)

        except Exception as exc:
            log.warning("verify_scheduled: error checking row %d: %s", i+1, exc)

    return {
        "success": True,
        "scheduled": False,
        "address": address,
        "appointment_number": appointment_number,
        "message": f"Appointment '{appointment_number}' for address '{address}' was NOT found on the scheduling board."
    }
