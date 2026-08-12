"""
EasyBOP Stage 6 — scrape completed Operative job sheets from SmartForms register
and extract Q-field data from each PDF preview.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pdfplumber
from playwright.async_api import BrowserContext, Page

_here = Path(__file__).resolve().parent


def _load_local_module(name: str, filename: str):
    path = _here / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load {filename} from {_here}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_config = _load_local_module("_stage6_automation_config", "config.py")
_pdf_parser = _load_local_module("_stage6_automation_pdf_parser", "pdf_parser.py")

settings = _config.settings
parse_operative_job_sheet_text = _pdf_parser.parse_operative_job_sheet_text

log = logging.getLogger("stage6.ojs")

LOGIN_URL = "https://easybop.co.uk/"
REGISTER_URL = settings.register_url
PDF_PREVIEW_URL = settings.pdf_preview_url
OPERATIVE_TEMPLATE_RE = re.compile(r"operative\s+job\s+sheet", re.IGNORECASE)
# SmartForm Name must include ": BCC Response Repairs" (e.g. "Address: BCC Response Repairs: 1298631").
BCC_RESPONSE_REPAIRS_RE = re.compile(
    r":\s*BCC\s+Respon(?:se|sive)\s+Repairs\b",
    re.IGNORECASE,
)
# EasyBOP register statuses we extract (Complete + Part Complete operative job sheets).
OPERATIVE_EXTRACTABLE_STATUS_RE = re.compile(r"^(?:complete|part complete)$", re.IGNORECASE)


def _row_form_name(row: Dict[str, Any]) -> str:
    cells = row.get("cells") or {}
    return str(row.get("form_name") or cells.get("list_form_name") or "").strip()


def is_bcc_response_repairs_form(name: str) -> bool:
    return bool(BCC_RESPONSE_REPAIRS_RE.search(str(name or "").strip()))


def _filter_bcc_response_repairs_rows(
    grid_rows: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], int]:
    """Keep only Operative forms whose SmartForm Name is BCC Response Repairs."""
    kept: List[Dict[str, Any]] = []
    skipped = 0
    for row in grid_rows:
        name = _row_form_name(row)
        if name and not is_bcc_response_repairs_form(name):
            skipped += 1
            continue
        kept.append(row)
    return kept, skipped


def _norm_dup_key(name: str, date_created: str) -> str:
    n = re.sub(r"\s+", " ", str(name or "").strip().lower())
    d = str(date_created or "").strip()
    return f"{n}::{d}"


def _row_dup_key(row: Dict[str, Any]) -> Optional[str]:
    cells = row.get("cells") or {}
    name = row.get("form_name") or cells.get("list_form_name") or ""
    date_created = row.get("date_created") or cells.get("list_date_created") or ""
    if not name or not date_created:
        return None
    return _norm_dup_key(name, date_created)


def _filter_new_grid_rows(
    grid_rows: List[Dict[str, Any]],
    *,
    skip_smart_form_ids: Optional[set[str]] = None,
    skip_name_date_keys: Optional[set[str]] = None,
) -> tuple[List[Dict[str, Any]], int]:
    """Return grid rows not already on the sheet.

    Skip by ``smart_form_x_id`` when present. Name+date is only used for
    legacy rows that have no EasyBOP id (multiple visits same day get unique ids).
    """
    skip_ids = skip_smart_form_ids or set()
    skip_keys = skip_name_date_keys or set()
    if not skip_ids and not skip_keys:
        return grid_rows, 0

    kept: List[Dict[str, Any]] = []
    skipped = 0
    for row in grid_rows:
        sfid = str(row.get("smart_form_x_id") or "").strip()
        dup_key = _row_dup_key(row)
        if sfid and sfid in skip_ids:
            skipped += 1
            continue
        if not sfid and dup_key and dup_key in skip_keys:
            skipped += 1
            continue
        kept.append(row)

    return kept, skipped


def _row_date_created_value(row: Dict[str, Any]) -> str:
    cells = row.get("cells") or {}
    return str(row.get("date_created") or cells.get("list_date_created") or "").strip()


def _parse_uk_date_only(value: str) -> Optional[date]:
    match = re.search(r"(\d{2})/(\d{2})/(\d{4})", str(value or ""))
    if not match:
        return None
    day, month, year = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _filter_grid_rows_by_max_age(
    grid_rows: List[Dict[str, Any]],
    *,
    max_form_age_weeks: Optional[int],
) -> Tuple[List[Dict[str, Any]], int]:
    """Keep rows whose Date Created is within the last N weeks (Europe/London)."""
    if not max_form_age_weeks or max_form_age_weeks <= 0:
        return grid_rows, 0

    today = datetime.now(ZoneInfo("Europe/London")).date()
    cutoff = today - timedelta(weeks=max_form_age_weeks)
    kept: List[Dict[str, Any]] = []
    skipped = 0
    for row in grid_rows:
        parsed = _parse_uk_date_only(_row_date_created_value(row))
        if parsed is not None and parsed < cutoff:
            skipped += 1
            continue
        kept.append(row)
    if skipped:
        log.info(
            "grid filter: skipped %s row(s) older than %s weeks (before %s)",
            skipped,
            max_form_age_weeks,
            cutoff.strftime("%d/%m/%Y"),
        )
    return kept, skipped


def _filter_grid_rows_by_days_lookback(
    grid_rows: List[Dict[str, Any]],
    *,
    days_to_look_back: Optional[int],
) -> Tuple[List[Dict[str, Any]], int]:
    """Keep rows whose Date Created falls within the last N days (Europe/London).

    ``days_to_look_back`` semantics match the n8n "days to look back" Set node:
    ``0`` = today only, ``1`` = today + yesterday, etc. ``None`` disables the
    filter (used for backfill / no date scope). Rows with an unparseable
    Date Created are kept so they are never silently dropped.
    """
    if days_to_look_back is None:
        return grid_rows, 0
    days = max(0, int(days_to_look_back))

    today = datetime.now(ZoneInfo("Europe/London")).date()
    cutoff = today - timedelta(days=days)
    kept: List[Dict[str, Any]] = []
    skipped = 0
    for row in grid_rows:
        parsed = _parse_uk_date_only(_row_date_created_value(row))
        if parsed is not None and (parsed < cutoff or parsed > today):
            skipped += 1
            continue
        kept.append(row)
    if skipped:
        log.info(
            "grid filter: skipped %s row(s) outside %s-day look-back (before %s)",
            skipped,
            days,
            cutoff.strftime("%d/%m/%Y"),
        )
    return kept, skipped


def _row_status_value(row: Dict[str, Any]) -> str:
    cells = row.get("cells") or {}
    return str(row.get("status") or cells.get("list_status") or "").strip()


def _filter_grid_rows_complete_only(
    grid_rows: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], int]:
    """Keep only rows whose Status is exactly ``Complete`` (drop ``Part Complete``)."""
    kept: List[Dict[str, Any]] = []
    skipped = 0
    for row in grid_rows:
        status = _row_status_value(row).lower()
        # Rows with no status value are kept (e.g. direct smart_form_ids path).
        if status and status != "complete":
            skipped += 1
            continue
        kept.append(row)
    if skipped:
        log.info(
            "grid filter: skipped %s Part Complete row(s) — only_complete=True",
            skipped,
        )
    return kept, skipped


async def _shows_login_wall(page: Page) -> bool:
    """True when the page is showing the EasyBOP login screen."""
    try:
        lb = page.locator("#login_box")
        if await lb.count() > 0 and await lb.first.is_visible():
            return True
        if "login" in (await page.title() or "").lower():
            return True
        h = page.locator("h1").first
        if await h.count() > 0:
            txt = (await h.inner_text() or "").lower()
            if "easybop" in txt and "login" in txt:
                return True
        return await page.get_by_text("EasyBOP Login", exact=False).count() > 0
    except Exception:
        return False


async def _login_if_needed(
    page: Page,
    *,
    username: Optional[str],
    password: Optional[str],
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
    label: str = "boq",
) -> None:
    if not await _shows_login_wall(page):
        return
    if not username or not password:
        raise RuntimeError("EasyBOP session expired — set username/password in .env or call POST /login")
    log.info("%s: login screen detected — signing in", label)
    if not await login(page, username, password):
        raise RuntimeError("EasyBOP login failed — check username/password in .env")
    if after_relogin:
        await after_relogin()


async def login(page: Page, username: str, password: str) -> bool:
    if not username or not password:
        log.error("login: missing username/password in .env")
        return False

    if not await _shows_login_wall(page):
        log.info("login: opening %s", LOGIN_URL)
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    else:
        log.info("login: login screen already visible — filling credentials")

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
        user_input = box.locator(
            "input[type='text'], input[name='username'], input[name='user']"
        ).first
        pass_input = box.locator("input[type='password']").first
        await user_input.fill(username)
        await pass_input.fill(password)
        await page.locator("#btn_login, button:has-text('Login')").first.click()
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


async def _select_register_month_for_today(page: Page, *, timeout_ms: int) -> None:
    """Select the EasyBOP register month radio matching today (Europe/London)."""
    today = datetime.now(ZoneInfo("Europe/London")).date()
    month_index = today.month - 1  # Jan=0 .. Dec=11 (search_month_0 .. search_month_11)
    target_id = f"search_month_{month_index}"

    try:
        await page.wait_for_selector("#search_months input[name='search_month']", timeout=timeout_ms)
    except Exception as e:
        log.warning("register month filter: month radios not found: %s", e)
        return

    # EasyBOP applies the saved month via jQuery UI ~1s after load. The raw HTML
    # can briefly show a different month as checked, so wait for the UI to settle.
    try:
        await page.wait_for_selector(
            "#search_months label.ui-checkboxradio-checked, #search_months label.ui-state-active",
            timeout=timeout_ms,
        )
    except Exception:
        pass
    await asyncio.sleep(1.5)

    state = await page.evaluate(
        """(targetId) => {
            const target = document.getElementById(targetId);
            const current = document.querySelector(
                "#search_months input[name='search_month']:checked"
            );
            if (!target) return { found: false };
            return {
                found: true,
                alreadySelected: Boolean(current && current.id === targetId),
                currentId: current ? current.id : null,
                dataYear: target.getAttribute("data-year") || "",
            };
        }""",
        target_id,
    )
    if not state.get("found"):
        log.warning("register month filter: %s not found", target_id)
        return

    month_label = today.strftime("%b")
    if state.get("alreadySelected"):
        log.info(
            "register month filter: already on %s %s",
            month_label,
            state.get("dataYear") or today.year,
        )
        return

    log.info(
        "register month filter: selecting %s %s (%s, was %s)",
        month_label,
        today.year,
        target_id,
        state.get("currentId") or "none",
    )
    label = page.locator(f'label[for="{target_id}"]')
    if await label.count() > 0:
        await label.click()
    else:
        clicked = await page.evaluate(
            """(targetId) => {
                const label = document.querySelector(`label[for="${targetId}"]`);
                if (!label) return false;
                label.click();
                return true;
            }""",
            target_id,
        )
        if not clicked:
            log.warning("register month filter: could not click label for %s", target_id)
            return

    try:
        await page.wait_for_function(
            f"() => document.querySelector('#search_months input[name=\"search_month\"]:checked')?.id === '{target_id}'",
            timeout=timeout_ms,
        )
    except Exception as e:
        log.warning("register month filter: month did not switch to %s: %s", target_id, e)
        return

    await asyncio.sleep(0.3)
    try:
        await page.wait_for_function(
            "() => { const el = document.getElementById('load_list'); "
            "return !el || el.style.display === 'none' || !el.offsetParent; }",
            timeout=timeout_ms,
        )
    except Exception as e:
        log.warning("register month filter: grid reload wait: %s", e)
    await asyncio.sleep(1.0)


async def _ensure_register_ready(
    page: Page,
    username: Optional[str],
    password: Optional[str],
    timeout_ms: int,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> None:
    log.info("navigate: -> %s", REGISTER_URL)
    await page.goto(REGISTER_URL, wait_until="domcontentloaded")

    if await _shows_login_wall(page):
        if not username or not password:
            raise RuntimeError("Login required but credentials not set in .env")
        log.info("navigate: session expired — re-logging in")
        if not await login(page, username, password):
            raise RuntimeError("EasyBOP re-login failed")
        if after_relogin:
            await after_relogin()
        await page.goto(REGISTER_URL, wait_until="domcontentloaded")

    if await _shows_login_wall(page):
        raise RuntimeError("Still on login wall after re-login — check credentials")

    await _select_register_month_for_today(page, timeout_ms=timeout_ms)
    await _wait_for_grid(page, timeout_ms)


async def _wait_for_grid(page: Page, timeout_ms: int) -> None:
    try:
        await page.wait_for_function(
            "() => {"
            "  const el = document.getElementById('load_list');"
            "  return !el || el.style.display === 'none' || !el.offsetParent;"
            "}",
            timeout=timeout_ms,
        )
    except Exception as e:
        log.warning("grid: loading overlay wait timed out: %s", e)

    try:
        await page.wait_for_selector("tr.jqgrow", state="attached", timeout=timeout_ms)
    except Exception as e:
        raise RuntimeError(f"jqGrid rows never appeared within {timeout_ms}ms: {e}") from e

    await asyncio.sleep(1.0)


async def _scrape_operative_rows_on_page(page: Page) -> List[Dict[str, Any]]:
    return await page.evaluate(
        """
        () => {
            const rows = [];
            document.querySelectorAll('tr.jqgrow').forEach(row => {
                const templateCell = row.querySelector('td[aria-describedby="list_template_name"]');
                const statusCell = row.querySelector('td[aria-describedby="list_status"]');
                const templateName = templateCell
                    ? (templateCell.getAttribute('title') || templateCell.innerText || '').trim()
                    : '';
                const status = statusCell
                    ? (statusCell.getAttribute('title') || statusCell.innerText || '').trim()
                    : '';

                const cells = {};
                row.querySelectorAll('td[role="gridcell"]').forEach(td => {
                    const key = td.getAttribute('aria-describedby') || 'unknown';
                    cells[key] = (td.getAttribute('title') || td.innerText || '').trim();
                });

                const formName = (cells.list_form_name || '').trim();
                const templateLooksOjs = /operative\\s+job\\s+sheet/i.test(templateName)
                    && !/^none$/i.test(templateName);
                const nameLooksOjs = /operative\\s+job\\s+sheet/i.test(formName);
                if (!templateLooksOjs && !nameLooksOjs) return;
                if (!/^(?:complete|part complete)$/i.test(status)) return;

                if (!/:\\s*BCC\\s+Respon(?:se|sive)\\s+Repairs\\b/i.test(formName)) return;

                rows.push({
                    smart_form_x_id: row.id || null,
                    template_name: templateName,
                    status,
                    form_name: cells.list_form_name || null,
                    date_created: cells.list_date_created || null,
                    date_completed: cells.list_date_completed || null,
                    time_completed: cells.list_time_completed || null,
                    created_by: cells.list_created_by || null,
                    module_name: cells.list_module_name || null,
                    cells,
                });
            });
            return rows;
        }
        """
    )


async def _go_to_next_grid_page(page: Page) -> bool:
    """Click jqGrid next-page if enabled. Returns True when a new page loaded."""
    clicked = await page.evaluate(
        """
        () => {
            const next = document.querySelector(
                '#next_list_pager, #next_list, #next_pager, td.ui-pg-button .ui-icon-seek-next, .ui-pg-button.ui-icon-seek-next'
            );
            if (!next) return false;
            const parent = next.closest ? next.closest('.ui-pg-button') : null;
            const disabled = next.classList.contains('ui-state-disabled')
                || (parent && parent.classList.contains('ui-state-disabled'))
                || next.getAttribute('disabled') !== null;
            if (disabled) return false;
            next.click();
            return true;
        }
        """
    )
    if not clicked:
        return False
    await asyncio.sleep(1.5)
    try:
        await page.wait_for_function(
            "() => { const el = document.getElementById('load_list'); "
            "return !el || el.style.display === 'none' || !el.offsetParent; }",
            timeout=15_000,
        )
    except Exception:
        pass
    await asyncio.sleep(0.5)
    return True


async def _wait_for_works_index_grid_ready(page: Page, timeout_ms: int) -> None:
    """Wait until jqGrid has at least one data row (works_id numeric tr id)."""
    await page.wait_for_function(
        """() => {
            let n = 0;
            document.querySelectorAll('table#list tbody tr[role="row"]').forEach((tr) => {
                if (tr.id && /^\\d+$/.test(tr.id)) n++;
            });
            return n > 0;
        }""",
        timeout=min(60_000, timeout_ms),
    )


async def _next_works_index_page_enabled(page: Page) -> bool:
    """True when jqGrid next-page control exists and is not disabled."""
    return bool(
        await page.evaluate(
            """() => {
                const nextTd = document.getElementById('next_list_pager');
                if (!nextTd) return false;
                return !nextTd.classList.contains('ui-state-disabled');
            }"""
        )
    )


async def _go_to_next_works_index_page(page: Page, *, timeout_ms: int = 15_000) -> bool:
    """Click #next_list_pager and wait for the grid page to advance."""
    if not await _next_works_index_page_enabled(page):
        return False

    info_before: Dict[str, Any] = await page.evaluate(_WORKS_INDEX_PAGE_INFO_JS)
    current = int(info_before.get("currentPage") or 1)

    first_id_before = await page.evaluate(
        "() => { const tr = document.querySelector('table#list tbody tr[role=\"row\"][id]'); "
        "return tr && /^\\d+$/.test(tr.id) ? tr.id : ''; }"
    )

    next_td = page.locator("#next_list_pager")
    if await next_td.count() == 0:
        return False
    classes = await next_td.get_attribute("class") or ""
    if "ui-state-disabled" in classes:
        return False

    icon = next_td.locator(".ui-icon-seek-next")
    if await icon.count() > 0:
        await icon.click()
    else:
        await next_td.click()

    try:
        await page.wait_for_function(
            f"() => {{ const inp = document.querySelector('#pager_list input.ui-pg-input, #pg_list_pager input.ui-pg-input'); "
            f"const pageNum = inp ? (parseInt(inp.value, 10) || 0) : 0; "
            f"if (pageNum > {current}) return true; "
            f"const tr = document.querySelector('table#list tbody tr[role=\"row\"][id]'); "
            f"const wid = tr && /^\\d+$/.test(tr.id) ? tr.id : ''; "
            f"return wid && wid !== '{first_id_before}'; }}",
            timeout=timeout_ms,
        )
    except Exception:
        pass

    try:
        await page.wait_for_function(
            "() => { const el = document.getElementById('load_list'); "
            "return !el || el.style.display === 'none' || !el.offsetParent; }",
            timeout=timeout_ms,
        )
    except Exception:
        pass
    await asyncio.sleep(0.5)
    return True


async def scrape_completed_operative_job_sheets(
    page: Page,
    *,
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout_ms: int = 30_000,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
    max_pages: int = 50,
) -> tuple[List[Dict[str, Any]], int]:
    await _ensure_register_ready(page, username, password, timeout_ms, after_relogin)

    seen_ids: set[str] = set()
    all_rows: List[Dict[str, Any]] = []

    for page_no in range(max_pages):
        batch = await _scrape_operative_rows_on_page(page)
        new_count = 0
        for row in batch:
            sfid = str(row.get("smart_form_x_id") or "").strip()
            if not sfid or sfid in seen_ids:
                continue
            seen_ids.add(sfid)
            all_rows.append(row)
            new_count += 1

        log.info(
            "grid page %s: found %s operative extractable rows (%s new, %s total)",
            page_no + 1,
            len(batch),
            new_count,
            len(all_rows),
        )

        if not await _go_to_next_grid_page(page):
            break

    log.info("grid scrape complete: %s operative extractable row(s) total", len(all_rows))
    filtered, skipped_non_bcc = _filter_bcc_response_repairs_rows(all_rows)
    if skipped_non_bcc:
        log.info(
            "grid scrape: skipped %s non-BCC Response Repairs form(s)",
            skipped_non_bcc,
        )
    return filtered, skipped_non_bcc


def _pdf_text_from_bytes(pdf_bytes: bytes) -> str:
    chunks: List[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for pg in pdf.pages:
            chunks.append(pg.extract_text() or "")
    return "\n".join(chunks)


async def _download_pdf(context: BrowserContext, smart_form_x_id: str, timeout_ms: int) -> bytes:
    url = PDF_PREVIEW_URL.format(smart_form_x_id=smart_form_x_id)
    log.info("pdf download: GET %s", url)
    resp = await context.request.get(url, timeout=timeout_ms)
    if resp.status != 200:
        raise RuntimeError(f"PDF download failed ({resp.status}) for smart_form_x_id={smart_form_x_id}")
    body = await resp.body()
    if not body.startswith(b"%PDF"):
        raise RuntimeError(f"Response is not a PDF for smart_form_x_id={smart_form_x_id}")
    return body


async def extract_operative_job_sheets(
    page: Page,
    context: BrowserContext,
    *,
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout_ms: int = 30_000,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
    limit: Optional[int] = None,
    smart_form_ids: Optional[List[str]] = None,
    skip_smart_form_ids: Optional[List[str]] = None,
    skip_entries: Optional[List[Dict[str, str]]] = None,
    force_extract_smart_form_ids: Optional[List[str]] = None,
    max_form_age_weeks: Optional[int] = None,
    days_to_look_back: Optional[int] = None,
    only_complete: bool = False,
) -> Dict[str, Any]:
    """
    Scrape register grid for Operative job sheets with status Complete or
    Part Complete, download each PDF, and parse Q fields. Rows matching
    skip_smart_form_ids or skip_entries (SmartForm Name + Date Created) are
    not downloaded unless listed in force_extract_smart_form_ids (photo backfill).

    ``days_to_look_back`` restricts the grid to forms whose Date Created is
    within the last N days (Europe/London; 0 = today only). ``only_complete``
    drops ``Part Complete`` rows so only fully Complete forms are extracted.
    Both are applied *before* PDF download to avoid wasted downloads.
    """
    skip_ids = {str(x).strip() for x in (skip_smart_form_ids or []) if str(x).strip()}
    skip_keys: set[str] = set()
    for entry in skip_entries or []:
        name = entry.get("smart_form_name") or entry.get("form_name") or ""
        date_created = entry.get("date_created") or ""
        if name and date_created:
            skip_keys.add(_norm_dup_key(name, date_created))

    total_grid_matched = 0
    total_skipped_existing = 0
    total_skipped_non_bcc = 0
    total_skipped_too_old = 0
    total_skipped_out_of_scope = 0
    total_skipped_part_complete = 0
    all_grid_rows: List[Dict[str, Any]] = []
    force_ids = {
        str(x).strip() for x in (force_extract_smart_form_ids or []) if str(x).strip()
    }

    if smart_form_ids:
        grid_rows = [
            {"smart_form_x_id": str(sfid).strip(), "template_name": None, "status": "Complete"}
            for sfid in smart_form_ids
            if str(sfid).strip()
        ]
        total_grid_matched = len(grid_rows)
        grid_rows, total_skipped_existing = _filter_new_grid_rows(
            grid_rows,
            skip_smart_form_ids=skip_ids,
            skip_name_date_keys=skip_keys,
        )
    else:
        all_grid_rows, scraped_skipped_non_bcc = await scrape_completed_operative_job_sheets(
            page,
            username=username,
            password=password,
            timeout_ms=timeout_ms,
            after_relogin=after_relogin,
        )
        total_skipped_non_bcc += scraped_skipped_non_bcc
        if only_complete:
            all_grid_rows, total_skipped_part_complete = _filter_grid_rows_complete_only(
                all_grid_rows,
            )
        all_grid_rows, total_skipped_out_of_scope = _filter_grid_rows_by_days_lookback(
            all_grid_rows,
            days_to_look_back=days_to_look_back,
        )
        all_grid_rows, total_skipped_too_old = _filter_grid_rows_by_max_age(
            all_grid_rows,
            max_form_age_weeks=max_form_age_weeks,
        )
        total_grid_matched = len(all_grid_rows)
        grid_rows, total_skipped_existing = _filter_new_grid_rows(
            all_grid_rows,
            skip_smart_form_ids=skip_ids,
            skip_name_date_keys=skip_keys,
        )
        log.info(
            "grid filter: %s matched on EasyBOP, %s already on sheet, %s new to extract",
            total_grid_matched,
            total_skipped_existing,
            len(grid_rows),
        )

    if force_ids:
        by_id = {
            str(r.get("smart_form_x_id") or "").strip(): r
            for r in all_grid_rows
            if str(r.get("smart_form_x_id") or "").strip()
        }
        kept_ids = {str(r.get("smart_form_x_id") or "").strip() for r in grid_rows}
        added_force = 0
        for fid in force_ids:
            if fid in kept_ids:
                continue
            grid_rows.append(by_id.get(fid) or {"smart_form_x_id": fid, "status": "Complete"})
            kept_ids.add(fid)
            added_force += 1
        if added_force:
            log.info("grid filter: force re-extract %s row(s) for photo backfill", added_force)

    grid_rows, skipped_non_bcc_pre = _filter_bcc_response_repairs_rows(grid_rows)
    total_skipped_non_bcc += skipped_non_bcc_pre
    if skipped_non_bcc_pre:
        log.info(
            "grid filter: skipped %s row(s) — SmartForm Name is not BCC Response Repairs",
            skipped_non_bcc_pre,
        )

    if limit is not None and limit > 0:
        grid_rows = grid_rows[:limit]

    total = len(grid_rows)
    if total == 0:
        log.info(
            "pdf extract: nothing new — %s on grid, %s skipped as existing",
            total_grid_matched,
            total_skipped_existing,
        )
        return {
            "total_grid_matched": total_grid_matched,
            "total_skipped_existing": total_skipped_existing,
            "total_skipped_non_bcc": total_skipped_non_bcc,
            "total_skipped_too_old": total_skipped_too_old,
            "total_skipped_out_of_scope": total_skipped_out_of_scope,
            "total_skipped_part_complete": total_skipped_part_complete,
            "total_grid_rows": 0,
            "total_extracted": 0,
            "total_errors": 0,
            "results": [],
            "errors": [],
        }

    log.info("pdf extract: starting %s operative job sheet(s)", total)

    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    for idx, row in enumerate(grid_rows, start=1):
        sfid = str(row.get("smart_form_x_id") or "").strip()
        if not sfid:
            continue
        form_name = _row_form_name(row) or sfid
        if _row_form_name(row) and not is_bcc_response_repairs_form(form_name):
            total_skipped_non_bcc += 1
            log.info(
                "pdf extract: %s/%s skip smart_form_x_id=%s — not BCC Response Repairs (%s)",
                idx,
                total,
                sfid,
                form_name,
            )
            continue
        log.info("pdf extract: %s/%s smart_form_x_id=%s (%s)", idx, total, sfid, form_name)
        try:
            pdf_bytes = await _download_pdf(context, sfid, timeout_ms)
            log.info("pdf extract: %s/%s downloaded %s bytes", idx, total, len(pdf_bytes))
            parsed = parse_operative_job_sheet_text(
                _pdf_text_from_bytes(pdf_bytes),
                pdf_bytes,
                smart_form_x_id=sfid,
            )
            header_name = str(parsed.get("job_header") or form_name or "").strip()
            if not is_bcc_response_repairs_form(header_name):
                total_skipped_non_bcc += 1
                log.info(
                    "pdf extract: %s/%s skip smart_form_x_id=%s — parsed header not BCC Response Repairs (%s)",
                    idx,
                    total,
                    sfid,
                    header_name,
                )
                continue
            log.info(
                "pdf extract: %s/%s parsed corf=%s fields=%s",
                idx,
                total,
                parsed.get("corf"),
                len((parsed.get("fields") or {})),
            )
            results.append(
                {
                    "smart_form_x_id": sfid,
                    "pdf_url": PDF_PREVIEW_URL.format(smart_form_x_id=sfid),
                    "grid": row,
                    "parsed": parsed,
                    "success": True,
                }
            )
        except Exception as e:
            log.warning("pdf extract: %s/%s failed for %s: %s", idx, total, sfid, e)
            errors.append({"smart_form_x_id": sfid, "grid": row, "error": str(e)})

    log.info(
        "pdf extract: finished — %s ok, %s error(s) of %s row(s)",
        len(results),
        len(errors),
        total,
    )

    return {
        "total_grid_matched": total_grid_matched,
        "total_skipped_existing": total_skipped_existing,
        "total_skipped_non_bcc": total_skipped_non_bcc,
        "total_skipped_too_old": total_skipped_too_old,
        "total_skipped_out_of_scope": total_skipped_out_of_scope,
        "total_skipped_part_complete": total_skipped_part_complete,
        "total_grid_rows": len(grid_rows),
        "total_extracted": len(results),
        "total_errors": len(errors),
        "results": results,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# BOQ scraper — fetch SOR lines (including variations) for a CORF
# ---------------------------------------------------------------------------

BOQ_TAB_URL = "https://easybop.co.uk/a_planned_works/z_works/works_details.php?works_id={works_id}&contract_id={contract_id}&tab_nav_item=boq"

_BOQ_SCRAPE_JS = """
() => {
    function cellText(td) {
        return (td && (td.innerText || td.textContent) || '').replace(/\\s+/g, ' ').trim();
    }

    function parseQuantity(tr) {
        const input = tr.querySelector(
            'input.item_quantity, input[id*="boq_quantity"], input[id*="variation_"][id*="boq_quantity"]'
        );
        if (input && input.value != null) {
            const v = String(input.value).trim();
            if (v) return v;
        }
        const qtyTd = tr.querySelector('td.qty');
        if (qtyTd) return cellText(qtyTd);
        const tds = tr.querySelectorAll('td');
        return tds.length > 6 ? cellText(tds[6]) : '';
    }

    function scrapeRow(tr, isVariation, variationLabel) {
        if (!tr || tr.id === 'grand_total' || tr.classList.contains('header_row')) return null;

        const tds = tr.querySelectorAll('td');
        if (tds.length < 8) return null;

        const section = cellText(tds[0]);
        const reference = cellText(tds[1]);
        const description = cellText(tds[2]);
        const detailedDescription = cellText(tds[3]);
        const location = cellText(tds[4]);
        const notes = cellText(tds[5]);
        const quantity = parseQuantity(tr);
        const unit = cellText(tds[7]);
        const rateTd = tr.querySelector('td.rate');
        const totalTd = tr.querySelector('td.total');
        const rate = rateTd ? cellText(rateTd) : cellText(tds[8]);
        const total = totalTd ? cellText(totalTd) : cellText(tds[9]);

        if (!reference || /^reference$/i.test(reference)) return null;
        if (!/\\d/.test(reference)) return null;

        return {
            section,
            sor_code: reference,
            description,
            detailed_description: detailedDescription,
            location,
            notes,
            quantity,
            unit,
            rate,
            total,
            is_variation: !!isVariation,
            variation_label: variationLabel || '',
        };
    }

    function tableAfterHeading(h3) {
        let el = h3.nextElementSibling;
        while (el) {
            if (el.tagName === 'TABLE') return el;
            if (el.tagName === 'H3') break;
            el = el.nextElementSibling;
        }
        return null;
    }

    const rows = [];
    const form = document.querySelector('#form_boq_table') || document;

    form.querySelectorAll('#boq_table tr.works_boq_item').forEach((tr) => {
        const row = scrapeRow(tr, false, '');
        if (row) rows.push(row);
    });

    form.querySelectorAll('h3.pw_h3_left').forEach((h3) => {
        const label = cellText(h3);
        if (!/variation/i.test(label)) return;
        const table = tableAfterHeading(h3);
        if (!table) return;
        table.querySelectorAll('tr.variation_boq_item').forEach((tr) => {
            const row = scrapeRow(tr, true, label);
            if (row) rows.push(row);
        });
    });

    function parseGrandTotal(root) {
        const tr = root.querySelector('#grand_total, tr#grand_total');
        if (!tr) return '';
        const tds = tr.querySelectorAll('td');
        for (let i = tds.length - 1; i >= 0; i--) {
            const txt = cellText(tds[i]);
            if (txt && /[\\d.]/.test(txt) && !/^total:?$/i.test(txt)) return txt;
        }
        return '';
    }

    return {
        lines: rows,
        grand_total: parseGrandTotal(form),
    };
}
"""


def _parse_boq_quantity(raw: Any) -> float:
    s = str(raw or "").strip()
    if not s:
        return 1.0
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return 1.0
    try:
        return float(m.group(0))
    except ValueError:
        return 1.0


def _parse_boq_money(raw: Any) -> Optional[float]:
    s = str(raw or "").strip()
    if not s:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _sum_boq_line_totals(lines: List[Dict[str, Any]]) -> str:
    total = 0.0
    found = False
    for line in lines:
        amount = _parse_boq_money(line.get("total"))
        if amount is None:
            continue
        qty = _parse_boq_quantity(line.get("quantity"))
        signed = -abs(amount) if qty < 0 else amount
        total += signed
        found = True
    if not found:
        return ""
    return f"{total:.2f}"


def _resolve_boq_grand_total(raw: Any, clean_lines: List[Dict[str, Any]]) -> str:
    if isinstance(raw, dict):
        grand_total = str(raw.get("grand_total") or "").strip()
        lines = raw.get("lines") or []
    else:
        grand_total = ""
        lines = raw if isinstance(raw, list) else []

    if grand_total:
        return grand_total

    summed = _sum_boq_line_totals(clean_lines or _normalize_scraped_boq_lines(lines))
    return summed
    s = str(raw or "").strip()
    if not s:
        return 1.0
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return 1.0
    try:
        return float(m.group(0))
    except ValueError:
        return 1.0


def _is_valid_boq_reference(code: str) -> bool:
    code = str(code or "").strip()
    if not code or re.match(r"^reference$", code, re.I):
        return False
    return bool(re.search(r"\d", code))


def _normalize_scraped_boq_lines(raw_lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    clean_lines: List[Dict[str, Any]] = []
    for line in raw_lines:
        code = str(line.get("sor_code") or "").strip()
        if not _is_valid_boq_reference(code):
            continue
        clean_lines.append({
            "sor_code": code,
            "description": str(line.get("description") or "").strip(),
            "quantity": _parse_boq_quantity(line.get("quantity")),
            "unit": str(line.get("unit") or "").strip(),
            "rate": str(line.get("rate") or "").strip(),
            "total": str(line.get("total") or "").strip(),
            "is_variation": bool(line.get("is_variation")),
            "section": str(line.get("section") or "").strip(),
            "notes": str(line.get("notes") or "").strip(),
            "variation_label": str(line.get("variation_label") or "").strip(),
            "detailed_description": str(line.get("detailed_description") or "").strip(),
        })
    return clean_lines


REQUIRED_JOB_NAME = "BCC Response Repairs"
WORKS_ID_MISSING_REASON = "works_id is required — address-search path removed"
CONTRACT_ID = "321129"
_WORKS_ID_RE = re.compile(r"works_id=(\d+)", re.IGNORECASE)
BOQ_SEARCH_WORKS_ID = "938256"
BOQ_SEARCH_URL = (
    "https://easybop.co.uk/a_planned_works/z_works/works_details.php"
    "?works_id={search_works_id}&contract_id={contract_id}&tab_nav_item=wi"
)

WORKS_INDEX_URL = (
    "https://easybop.co.uk/a_planned_works/z_works/index.php"
    "?contract_id={contract_id}"
)

# JavaScript to extract all rows from the jqgrid on the works index page.
# Each <tr role="row" id="WORKS_ID"> holds cells with aria-describedby="list_COLUMN".
_WORKS_INDEX_ROWS_JS = r"""
() => {
    const rows = [];
    document.querySelectorAll('table#list tbody tr[role="row"]').forEach((tr) => {
        const wid = tr.id;
        if (!wid || !/^\d+$/.test(wid)) return;
        const cellMap = {};
        tr.querySelectorAll('td[aria-describedby]').forEach((td) => {
            const key = (td.getAttribute('aria-describedby') || '').replace(/^list_/, '');
            cellMap[key] = (td.getAttribute('title') || td.innerText || td.textContent || '').trim();
        });
        rows.push({
            works_id:              wid,
            address:               cellMap['address'] || '',
            property_ref:          cellMap['property_ref'] || '',
            client_order_reference: cellMap['client_order_reference'] || '',
            our_order_reference:   cellMap['our_order_reference'] || '',
            scope_of_work:         cellMap['scope_of_work'] || '',
            physical_works_status: cellMap['physical_works_status'] || '',
        });
    });
    return rows;
}
"""

# Returns current page number, total pages, and whether a next-page button exists.
_WORKS_INDEX_PAGE_INFO_JS = r"""
() => {
    const inp = document.querySelector(
        '#pager_list input.ui-pg-input, #pg_list_pager input.ui-pg-input, input.ui-pg-input'
    );
    const tot = document.querySelector(
        '#sp_1_list_pager, #sp_1_pager_list, span[id^="sp_1_"]'
    );
    const nextTd = document.getElementById('next_list_pager');
    const currentPage = inp ? (parseInt(inp.value, 10) || 1) : 1;
    const totalPages = tot ? (parseInt((tot.textContent || '').trim(), 10) || 1) : 1;
    const hasNext = nextTd
        ? !nextTd.classList.contains('ui-state-disabled')
        : false;
    return { currentPage, totalPages, hasNext };
}
"""

_WORKS_INDEX_ROW_COUNT_JS = r"""
() => {
    let n = 0;
    document.querySelectorAll('table#list tbody tr[role="row"]').forEach((tr) => {
        if (tr.id && /^\d+$/.test(tr.id)) n++;
    });
    return n;
}
"""

def parse_address_from_smart_form_name(smart_form_name: str) -> str:
    """e.g. '5 KEBLE AVENUE: BCC Response Repairs: 1283085' -> '5 KEBLE AVENUE'."""
    raw = str(smart_form_name or "").strip()
    if not raw:
        return ""
    return raw.split(":")[0].strip()


def parse_uprn_from_smart_form_name(smart_form_name: str) -> str:
    """Trailing numeric segment after colons is usually the UPRN."""
    raw = str(smart_form_name or "").strip()
    if not raw:
        return ""
    tail = raw.split(":")[-1].strip()
    return tail if tail.isdigit() and len(tail) >= 5 else ""


_LIST_WORKS_TABLE_ROWS_JS = """
() => {
    const rows = [];
    document.querySelectorAll('table.nice_table.clickable tbody tr[data-works_id]').forEach((tr) => {
        const tds = tr.querySelectorAll('td');
        rows.push({
            works_id: tr.getAttribute('data-works_id') || '',
            job_name: (tds[1] && (tds[1].innerText || tds[1].textContent) || '').replace(/\\s+/g, ' ').trim(),
            address: (tds[6] && (tds[6].innerText || tds[6].textContent) || '').replace(/\\s+/g, ' ').trim(),
            disabled: tr.classList.contains('disabled'),
        });
    });
    return rows;
}
"""


_LIST_WI_ROWS_JS = """
() => {
    const rows = [];
    document.querySelectorAll('tr.wi_row').forEach((tr, index) => {
        const tds = tr.querySelectorAll('td');
        rows.push({
            index,
            works_id: tr.getAttribute('data-works_id') || '',
            job_name: (tds[1] && (tds[1].innerText || tds[1].textContent) || '').replace(/\\s+/g, ' ').trim(),
            address: (tds[6] && (tds[6].innerText || tds[6].textContent) || '').replace(/\\s+/g, ' ').trim(),
            disabled: tr.classList.contains('disabled'),
        });
    });
    return rows;
}
"""


class BoqWrongJobNameSkip(Exception):
    """Address matched but job name is not BCC Response Repairs."""

    def __init__(self, found_job_names: List[str], required: str = REQUIRED_JOB_NAME):
        self.found_job_names = found_job_names
        self.required_job_name = required
        names = ", ".join(found_job_names) if found_job_names else "(none)"
        super().__init__(
            f"Job name is not {required!r} — found: {names}. Skipping BOQ fetch."
        )


def _norm_job_name(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _job_names_match(required: str, actual: str) -> bool:
    return _norm_job_name(required) == _norm_job_name(actual)


def _required_job_name(*, smart_form_name: str = "", override: str = "") -> str:
    if str(override or "").strip():
        return str(override).strip()
    return REQUIRED_JOB_NAME


def _boq_search_url() -> str:
    return BOQ_SEARCH_URL.format(
        search_works_id=BOQ_SEARCH_WORKS_ID,
        contract_id=CONTRACT_ID,
    )


def _on_boq_search_page(page: Page) -> bool:
    url = page.url or ""
    return (
        "works_details.php" in url
        and "tab_nav_item=wi" in url
        and f"contract_id={CONTRACT_ID}" in url
    )


async def _goto_boq_search_page(
    page: Page,
    *,
    timeout_ms: int = 30_000,
    username: Optional[str] = None,
    password: Optional[str] = None,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> None:
    """Open WI tab works-details page with #quick_search_address; log in if needed."""
    search_url = _boq_search_url()
    await page.goto(search_url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        await page.wait_for_load_state("networkidle", timeout=min(20_000, timeout_ms))
    except Exception:
        pass

    await _login_if_needed(
        page,
        username=username,
        password=password,
        after_relogin=after_relogin,
        label="boq search",
    )

    if await _shows_login_wall(page):
        raise RuntimeError("Still on login page after auth attempt — check credentials")

    if not _on_boq_search_page(page):
        await page.goto(search_url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            await page.wait_for_load_state("networkidle", timeout=min(20_000, timeout_ms))
        except Exception:
            pass

    try:
        await page.wait_for_selector("#quick_search_address", state="visible", timeout=timeout_ms)
    except Exception as e:
        if await _shows_login_wall(page):
            raise RuntimeError(
                "EasyBOP session expired — login required but #quick_search_address not available"
            ) from e
        raise


def _addr_matches(search_label: str, row_addr: str) -> bool:
    a = search_label.strip().lower()
    b = row_addr.strip().lower()
    if not a or not b:
        return False
    a_parts = a.split()
    b_parts = b.split()
    if a_parts and b_parts and a_parts[0] != b_parts[0]:
        return False
    return a in b or b in a


async def _wait_works_detail_after_search(
    page: Page,
    search_label: str,
    *,
    timeout_ms: int = 30_000,
) -> str:
    t = min(timeout_ms, 30_000)
    try:
        await page.wait_for_load_state("networkidle", timeout=t)
    except Exception:
        try:
            await page.wait_for_load_state("load", timeout=min(12_000, t))
        except Exception:
            pass

    try:
        await page.wait_for_function(
            "() => /works_id=\\d+/i.test(window.location.href)",
            timeout=min(15_000, t),
        )
    except Exception:
        pass

    try:
        await page.wait_for_selector(
            'a[data-action="pw_works_show_pre_item_dialog"]',
            state="visible",
            timeout=min(12_000, t),
        )
    except Exception as e:
        log.warning(
            "boq: pre_item links not visible after search %r: %s",
            search_label,
            e,
        )

    await asyncio.sleep(0.6)

    m = _WORKS_ID_RE.search(page.url or "")
    if not m:
        raise RuntimeError(
            f"Address {search_label!r}: did not land on works-detail page (URL: {page.url})"
        )
    return m.group(1)


async def _pick_works_row_after_address_search(
    page: Page,
    *,
    required_job_name: str,
    search_label: str,
    timeout_ms: int = 30_000,
) -> str:
    t = min(timeout_ms, 30_000)
    try:
        await page.wait_for_selector(
            "table.nice_table.clickable tbody tr[data-works_id], tr.wi_row",
            state="attached",
            timeout=min(10_000, t),
        )
    except Exception:
        pass
    await asyncio.sleep(0.5)

    rows: List[Dict[str, Any]] = await page.evaluate(_LIST_WORKS_TABLE_ROWS_JS)
    if rows:
        addr_matches = [
            r for r in rows
            if not r.get("disabled") and _addr_matches(search_label, r.get("address", ""))
        ]
        if not addr_matches:
            addr_matches = [r for r in rows if not r.get("disabled")]

        job_matches = [
            r for r in addr_matches
            if _job_names_match(required_job_name, r.get("job_name", ""))
        ]
        if not job_matches:
            found_names = [str(r.get("job_name") or "").strip() for r in addr_matches if r.get("job_name")]
            log.info(
                "boq: address %r — no row with job name %r (found: %s)",
                search_label,
                required_job_name,
                found_names,
            )
            raise BoqWrongJobNameSkip(found_names, required_job_name)

        works_id = str(job_matches[0].get("works_id") or "").strip()
        if not works_id:
            raise RuntimeError(f"Matched job row missing works_id for {search_label!r}")

        log.info(
            "boq: selecting works_id=%s job_name=%r for address %r",
            works_id,
            job_matches[0].get("job_name"),
            search_label,
        )
        await page.locator(
            f'table.nice_table.clickable tbody tr[data-works_id="{works_id}"]'
        ).first.click()
        return await _wait_works_detail_after_search(page, search_label, timeout_ms=timeout_ms)

    wi_rows: List[Dict[str, Any]] = await page.evaluate(_LIST_WI_ROWS_JS)
    if wi_rows:
        addr_matches = [
            r for r in wi_rows
            if not r.get("disabled") and _addr_matches(search_label, r.get("address", ""))
        ]
        if not addr_matches:
            addr_matches = [r for r in wi_rows if not r.get("disabled")]

        job_matches = [
            r for r in addr_matches
            if _job_names_match(required_job_name, r.get("job_name", ""))
        ]
        if not job_matches:
            found_names = [str(r.get("job_name") or "").strip() for r in addr_matches if r.get("job_name")]
            log.info(
                "boq: address %r — no wi_row with job name %r (found: %s)",
                search_label,
                required_job_name,
                found_names,
            )
            raise BoqWrongJobNameSkip(found_names or ["(no job name found)"], required_job_name)

        row_index = int(job_matches[0].get("index") or 0)
        log.info(
            "boq: selecting wi_row[%d] job_name=%r address=%r for search %r",
            row_index,
            job_matches[0].get("job_name"),
            job_matches[0].get("address"),
            search_label,
        )
        await page.locator("tr.wi_row").nth(row_index).click()
        return await _wait_works_detail_after_search(page, search_label, timeout_ms=timeout_ms)

    m = _WORKS_ID_RE.search(page.url or "")
    if m and m.group(1) != BOQ_SEARCH_WORKS_ID:
        return m.group(1)

    raise RuntimeError(
        f"Address {search_label!r}: no results found after quick search"
    )


async def _search_address_get_works_id(
    page: Page,
    address: str,
    *,
    timeout_ms: int = 30_000,
    assume_on_index: bool = False,
    username: Optional[str] = None,
    password: Optional[str] = None,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
    smart_form_name: str = "",
    required_job_name: str = "",
) -> str:
    """Type property address into #quick_search_address (WI tab), pick row with required job name."""
    if not assume_on_index and not _on_boq_search_page(page):
        await page.goto(_boq_search_url(), wait_until="domcontentloaded")

    await _login_if_needed(
        page,
        username=username,
        password=password,
        after_relogin=after_relogin,
        label="boq search",
    )
    if await _shows_login_wall(page):
        raise RuntimeError("EasyBOP session expired — login required")

    try:
        await page.wait_for_selector("#quick_search_address", state="visible", timeout=timeout_ms)
    except Exception:
        await _goto_boq_search_page(
            page,
            timeout_ms=timeout_ms,
            username=username,
            password=password,
            after_relogin=after_relogin,
        )

    address = str(address).strip()
    if not address:
        raise ValueError("address is required")

    job_name_required = _required_job_name(
        smart_form_name=smart_form_name,
        override=required_job_name,
    )

    log.info(
        "boq: quick-search by address %r (required job name %r)",
        address,
        job_name_required,
    )

    field = page.locator("#quick_search_address")
    await field.wait_for(state="visible", timeout=timeout_ms)
    await field.click()
    await field.fill("")
    await asyncio.sleep(0.2)
    await field.type(address, delay=60)

    result_timeout = min(12_000, timeout_ms)
    try:
        await page.wait_for_selector(
            "table.nice_table.clickable tbody tr[data-works_id], tr.wi_row",
            state="attached",
            timeout=result_timeout,
        )
    except Exception:
        pass
    await asyncio.sleep(0.5)

    return await _pick_works_row_after_address_search(
        page,
        required_job_name=job_name_required,
        search_label=address,
        timeout_ms=timeout_ms,
    )


async def _scrape_boq_via_address_search(
    page: Page,
    *,
    address: str,
    smart_form_name: str = "",
    uprn: str = "",
    timeout_ms: int = 30_000,
    username: Optional[str] = None,
    password: Optional[str] = None,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> Dict[str, Any]:
    """Search by address on WI quick-search page, then scrape BOQ."""
    await _goto_boq_search_page(
        page,
        timeout_ms=timeout_ms,
        username=username,
        password=password,
        after_relogin=after_relogin,
    )
    works_id = await _search_address_get_works_id(
        page,
        address,
        timeout_ms=timeout_ms,
        assume_on_index=True,
        username=username,
        password=password,
        after_relogin=after_relogin,
        smart_form_name=smart_form_name,
    )
    return await _scrape_boq_lines_on_page(
        page,
        address=address,
        smart_form_name=smart_form_name,
        uprn=uprn,
        works_id=works_id,
        timeout_ms=timeout_ms,
    )


async def _scrape_boq_lines_on_page(
    page: Page,
    *,
    address: str,
    smart_form_name: str,
    uprn: str,
    works_id: str,
    timeout_ms: int = 30_000,
) -> Dict[str, Any]:
    """On a works-detail page, open BOQ tab and scrape SOR lines."""
    boq_url = BOQ_TAB_URL.format(works_id=works_id, contract_id=CONTRACT_ID)
    try:
        boq_tab = page.locator("div[data-tab-nav-item='boq']")
        await boq_tab.wait_for(state="visible", timeout=min(10_000, timeout_ms))
        log.info("boq fetch: clicking BOQ tab for %r", address)
        await boq_tab.click()
        try:
            await page.wait_for_selector(
                "#form_boq_table tr.works_boq_item, #boq_table tr.works_boq_item",
                state="attached",
                timeout=min(12_000, timeout_ms),
            )
        except Exception:
            pass
        await asyncio.sleep(0.6)
    except Exception as e:
        log.info("boq fetch: BOQ tab click failed (%s) — navigating to %s", e, boq_url)
        await page.goto(boq_url, wait_until="domcontentloaded")
        try:
            await page.wait_for_selector(
                "#form_boq_table tr.works_boq_item, #boq_table tr.works_boq_item",
                state="attached",
                timeout=min(12_000, timeout_ms),
            )
        except Exception:
            pass
        await asyncio.sleep(1.0)

    sor_raw: Any = await page.evaluate(_BOQ_SCRAPE_JS)
    if isinstance(sor_raw, dict):
        sor_lines = sor_raw.get("lines") or []
    elif isinstance(sor_raw, list):
        sor_lines = sor_raw
    else:
        sor_lines = []

    log.info(
        "boq fetch: address=%r works_id=%s scraped %s row(s)",
        address,
        works_id,
        len(sor_lines),
    )

    clean_lines = _normalize_scraped_boq_lines(sor_lines)
    boq_grand_total = _resolve_boq_grand_total(sor_raw, clean_lines)

    return {
        "address": address,
        "smart_form_name": smart_form_name,
        "uprn": uprn,
        "works_id": works_id,
        "boq_url": boq_url,
        "sor_lines": clean_lines,
        "sor_count": len(clean_lines),
        "boq_grand_total": boq_grand_total,
    }


async def scrape_boq_for_corf(
    page: Page,
    corf: str,
    *,
    smart_form_name: str = "",
    uprn: str = "",
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout_ms: int = 30_000,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> Dict[str, Any]:
    """Resolve works_id from the index cache then navigate directly to the BOQ tab."""
    address = str(corf or smart_form_name or "").strip()
    if not address:
        raise ValueError("corf/address is required")

    addr_map = await _get_or_refresh_works_index_map(
        page.context,
        username=username,
        password=password,
        after_relogin=after_relogin,
        timeout_ms=timeout_ms,
    )
    works_id = addr_map.get(address.lower(), "")
    if not works_id:
        raise RuntimeError(
            f"scrape_boq_for_corf: could not resolve works_id for address {address!r}"
        )

    return await _scrape_boq_direct(
        page,
        works_id=works_id,
        address=address,
        smart_form_name=smart_form_name,
        uprn=uprn,
        username=username,
        password=password,
        timeout_ms=timeout_ms,
        after_relogin=after_relogin,
    )


async def scrape_works_index(
    context: BrowserContext,
    *,
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout_ms: int = 90_000,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
    max_pages: int = 200,
) -> List[Dict[str, Any]]:
    """Scrape all works from the planned-works index jqgrid (with pagination).

    Returns a list of dicts: {works_id, address, property_ref,
    client_order_reference, our_order_reference, scope_of_work,
    physical_works_status}.
    """
    index_url = WORKS_INDEX_URL.format(contract_id=CONTRACT_ID)
    page = await context.new_page()
    try:
        await page.goto(index_url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            await page.wait_for_load_state("networkidle", timeout=min(20_000, timeout_ms))
        except Exception:
            pass

        await _login_if_needed(
            page,
            username=username,
            password=password,
            after_relogin=after_relogin,
            label="works index",
        )
        if await _shows_login_wall(page):
            raise RuntimeError("Works index: still on login page after auth")

        # Wait for jqgrid to populate
        try:
            await page.wait_for_selector(
                'table#list tbody tr[role="row"]',
                state="attached",
                timeout=min(30_000, timeout_ms),
            )
            await _wait_for_works_index_grid_ready(page, timeout_ms)
        except Exception as e:
            log.warning("works index: jqgrid rows not found: %s", e)
            if page.is_closed():
                raise RuntimeError(
                    "Works index: browser page closed before grid loaded (API may be restarting)"
                ) from e

        all_rows: List[Dict[str, Any]] = []
        pages_scraped = 0

        for page_num in range(1, max_pages + 1):
            if page.is_closed():
                raise RuntimeError("Works index: browser page closed during scrape")
            await asyncio.sleep(0.4)
            rows: List[Dict[str, Any]] = await page.evaluate(_WORKS_INDEX_ROWS_JS)
            all_rows.extend(rows)
            pages_scraped = page_num

            info: Dict[str, Any] = await page.evaluate(_WORKS_INDEX_PAGE_INFO_JS)
            current = int(info.get("currentPage") or 1)
            total = int(info.get("totalPages") or 1)
            log.info(
                "works index: page %d/%d — %d row(s) on page (total so far: %d)",
                current,
                total,
                len(rows),
                len(all_rows),
            )

            if not await _next_works_index_page_enabled(page):
                break

            try:
                if not await _go_to_next_works_index_page(page, timeout_ms=min(20_000, timeout_ms)):
                    log.warning("works index: next page click failed on page %d/%d", current, total)
                    break
                await _wait_for_works_index_grid_ready(page, timeout_ms)
            except Exception as nav_err:
                log.warning("works index: could not advance past page %d: %s", current, nav_err)
                break

        log.info(
            "works index: scraped %d total row(s) across %d page(s)",
            len(all_rows),
            pages_scraped,
        )
        return all_rows
    finally:
        await page.close()


async def _scrape_boq_direct(
    page: Page,
    *,
    works_id: str,
    address: str,
    smart_form_name: str = "",
    uprn: str = "",
    timeout_ms: int = 30_000,
    username: Optional[str] = None,
    password: Optional[str] = None,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> Dict[str, Any]:
    """Navigate directly to the BOQ tab by works_id — no address search needed."""
    boq_url = BOQ_TAB_URL.format(works_id=works_id, contract_id=CONTRACT_ID)
    log.info("boq direct: works_id=%s address=%r", works_id, address)
    await page.goto(boq_url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        await page.wait_for_load_state("networkidle", timeout=min(15_000, timeout_ms))
    except Exception:
        pass

    await _login_if_needed(
        page,
        username=username,
        password=password,
        after_relogin=after_relogin,
        label=f"boq direct works_id={works_id}",
    )
    if await _shows_login_wall(page):
        raise RuntimeError(f"BOQ direct: login required for works_id={works_id}")

    try:
        await page.wait_for_selector(
            "#form_boq_table tr.works_boq_item, #boq_table tr.works_boq_item",
            state="attached",
            timeout=min(12_000, timeout_ms),
        )
    except Exception:
        pass
    await asyncio.sleep(0.5)

    return await _scrape_boq_lines_on_page(
        page,
        address=address,
        smart_form_name=smart_form_name,
        uprn=uprn,
        works_id=works_id,
        timeout_ms=timeout_ms,
    )


def _normalize_boq_batch_item(item: Dict[str, Any]) -> Dict[str, Any]:
    smart_form_name = str(item.get("smart_form_name") or "").strip()
    address = str(item.get("address") or "").strip()
    if not address and smart_form_name:
        address = parse_address_from_smart_form_name(smart_form_name)
    uprn = str(item.get("uprn") or "").strip()
    if not uprn and smart_form_name:
        uprn = parse_uprn_from_smart_form_name(smart_form_name)
    return {
        "address": address,
        "smart_form_name": smart_form_name,
        "uprn": uprn,
        "works_id": str(item.get("works_id") or "").strip(),
        "use_address_search": bool(item.get("use_address_search")),
    }


import time as _time_mod

# ── Works-index address→works_id cache ──────────────────────────────────────
# Fetched once per process (or when stale) and used to resolve works_id for
# any batch item that doesn't already have one — works with both old and new
# n8n workflows without requiring extra API calls from n8n.

_works_index_addr_map: Dict[str, str] = {}  # address.lower() -> works_id
_works_index_map_ts: float = 0.0
_WORKS_INDEX_CACHE_TTL: float = 3600.0  # refresh after 1 hour
_works_index_map_lock: Optional[asyncio.Lock] = None


def _get_works_index_lock() -> asyncio.Lock:
    global _works_index_map_lock
    if _works_index_map_lock is None:
        _works_index_map_lock = asyncio.Lock()
    return _works_index_map_lock


async def _get_or_refresh_works_index_map(
    context: BrowserContext,
    *,
    username: Optional[str],
    password: Optional[str],
    timeout_ms: int,
    after_relogin: Optional[Callable[[], Awaitable[None]]],
) -> Dict[str, str]:
    """Return the cached address→works_id map, refreshing if stale."""
    global _works_index_addr_map, _works_index_map_ts
    lock = _get_works_index_lock()
    async with lock:
        age = _time_mod.monotonic() - _works_index_map_ts
        if _works_index_addr_map and age < _WORKS_INDEX_CACHE_TTL:
            log.info(
                "works index map: cache hit (age=%.0fs, %d entries)",
                age, len(_works_index_addr_map),
            )
            return _works_index_addr_map

        log.info("works index map: fetching (cache age=%.0fs)", age)
        rows = await scrape_works_index(
            context,
            username=username,
            password=password,
            timeout_ms=min(timeout_ms, 120_000),
            after_relogin=after_relogin,
        )
        m: Dict[str, str] = {}
        for r in rows:
            addr = str(r.get("address") or "").strip().lower()
            wid = str(r.get("works_id") or "").strip()
            if addr and wid:
                m[addr] = wid
        _works_index_addr_map = m
        _works_index_map_ts = _time_mod.monotonic()
        log.info("works index map: cached %d entries", len(m))
        return m


def _boq_batch_error_result(item: Dict[str, Any], error: str) -> Dict[str, Any]:
    norm = _normalize_boq_batch_item(item)
    return {
        "success": False,
        "address": norm["address"],
        "smart_form_name": norm["smart_form_name"],
        "uprn": norm["uprn"],
        "works_id": "",
        "boq_url": "",
        "sor_count": 0,
        "sor_lines": [],
        "boq_grand_total": "",
        "error": error,
        "skip_boq_checked_true": False,
        "reason": None,
    }


async def _scrape_boq_job_on_page(
    page: "Page",
    item: Dict[str, Any],
    *,
    username: Optional[str],
    password: Optional[str],
    timeout_ms: int,
    after_relogin: Optional[Callable[[], Awaitable[None]]],
) -> Dict[str, Any]:
    """Scrape one job on an *already-open* Playwright page (does not create or close it).

    Fast path: ``works_id`` set → direct BOQ URL.
    Address-search path: ``use_address_search`` true (rows marked missing works_id).
    """
    norm = _normalize_boq_batch_item(item)
    address = norm["address"]
    smart_form_name = norm["smart_form_name"]
    uprn = norm["uprn"]
    direct_works_id = norm["works_id"]
    use_address_search = norm["use_address_search"]

    if not address:
        return _boq_batch_error_result(item, "address is required")

    if not direct_works_id and not use_address_search:
        log.info(
            "boq batch: no works_id for %r — falling back to address-search",
            address,
        )
        use_address_search = True

    try:
        if use_address_search:
            log.info("boq batch: address-search path for %r", address)
            payload = await _scrape_boq_via_address_search(
                page,
                address=address,
                smart_form_name=smart_form_name,
                uprn=uprn,
                timeout_ms=timeout_ms,
                username=username,
                password=password,
                after_relogin=after_relogin,
            )
        else:
            payload = await _scrape_boq_direct(
                page,
                works_id=direct_works_id,
                address=address,
                smart_form_name=smart_form_name,
                uprn=uprn,
                timeout_ms=timeout_ms,
                username=username,
                password=password,
                after_relogin=after_relogin,
            )
        return {
            "success": True,
            "address": payload["address"],
            "smart_form_name": smart_form_name or payload.get("smart_form_name", ""),
            "uprn": uprn or payload.get("uprn", ""),
            "works_id": payload["works_id"],
            "boq_url": payload["boq_url"],
            "sor_count": payload["sor_count"],
            "sor_lines": payload["sor_lines"],
            "boq_grand_total": payload.get("boq_grand_total", ""),
            "error": None,
            "skip_boq_checked_true": False,
            "reason": None,
        }
    except BoqWrongJobNameSkip as exc:
        actual_name = exc.found_job_names[0] if exc.found_job_names else "unknown"
        reason = f"Job name: {actual_name}"
        log.info("boq batch: address %r skipped — %s", address, reason)
        return {
            "success": False,
            "address": address,
            "smart_form_name": smart_form_name,
            "uprn": uprn,
            "works_id": "",
            "boq_url": "",
            "sor_count": 0,
            "sor_lines": [],
            "boq_grand_total": "",
            "error": str(exc),
            "skip_boq_checked_true": False,
            "reason": reason,
        }
    except Exception as exc:
        log.warning("boq batch: address %r works_id=%r failed: %s", address, direct_works_id, exc)
        result = _boq_batch_error_result(item, str(exc))
        result["address"] = address or result["address"]
        result["skip_boq_checked_true"] = False
        return result


async def _scrape_boq_job_worker(
    context: BrowserContext,
    item: Dict[str, Any],
    *,
    username: Optional[str],
    password: Optional[str],
    timeout_ms: int,
    after_relogin: Optional[Callable[[], Awaitable[None]]],
) -> Dict[str, Any]:
    """Open a fresh tab, scrape one job, close the tab. Legacy wrapper."""
    page = await context.new_page()
    try:
        return await _scrape_boq_job_on_page(
            page, item,
            username=username, password=password,
            timeout_ms=timeout_ms, after_relogin=after_relogin,
        )
    finally:
        await page.close()


async def scrape_boq_batch(
    context: BrowserContext,
    items: List[Dict[str, Any]],
    *,
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout_ms: int = 30_000,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
    concurrency: int = 3,
    persistent_pages: Optional[List] = None,
) -> Dict[str, Any]:
    """Fetch BOQ for up to ``concurrency`` jobs in parallel (one tab each).

    ``persistent_pages`` — pass a module-level list to reuse browser tabs
    across successive batch calls.  Pages in the list are reused (and kept
    open); the list is grown/healed automatically if pages have been closed.
    When ``None`` a temporary pool is created and closed at the end (legacy
    behaviour, e.g. from the single-item endpoint).
    """
    results: List[Dict[str, Any]] = []
    fetched = 0
    errors = 0
    interrupted = False
    interrupt_error: Optional[str] = None

    if not items:
        return {
            "success": True,
            "total": 0,
            "fetched": 0,
            "errors": 0,
            "interrupted": False,
            "results": [],
        }

    wave_size = max(1, min(int(concurrency or 3), 3, len(items)))
    owns_pool = persistent_pages is None  # True → we created the pool; we close it

    # ── Build / heal the page pool ──────────────────────────────────────────
    if owns_pool:
        pool: List = []
    else:
        pool = persistent_pages  # type: ignore[assignment]

    # Remove any dead tabs
    closed_indices = [i for i, p in enumerate(pool) if p.is_closed()]
    for i in reversed(closed_indices):
        pool.pop(i)

    # Grow the pool to wave_size
    while len(pool) < wave_size:
        pool.append(await context.new_page())
        log.info("boq batch: opened new pool tab (pool size now %d)", len(pool))

    log.info(
        "boq batch: starting %d job(s), %d tab(s), owns_pool=%s",
        len(items),
        wave_size,
        owns_pool,
    )

    # ── Auto-resolve missing works_ids from the cached index ─────────────────
    # Items from the old n8n workflow (or if /works-index failed) arrive
    # without works_id.  Resolve them now so the fast-path is always used.
    items_without_id = [
        i for i in items
        if not str(i.get("works_id") or "").strip()
        and not bool(i.get("use_address_search"))
    ]
    if items_without_id:
        try:
            index_map = await _get_or_refresh_works_index_map(
                context,
                username=username,
                password=password,
                timeout_ms=timeout_ms,
                after_relogin=after_relogin,
            )
            resolved = 0
            for item in items:
                if str(item.get("works_id") or "").strip():
                    continue
                if bool(item.get("use_address_search")):
                    continue
                addr_lo = str(item.get("address") or "").strip().lower()
                wid = index_map.get(addr_lo, "")
                if wid:
                    item["works_id"] = wid
                    resolved += 1
                    log.info(
                        "boq batch: resolved works_id=%s for address %r",
                        wid, item.get("address"),
                    )
            log.info(
                "boq batch: auto-resolved %d/%d works_id(s) from index",
                resolved, len(items_without_id),
            )
        except Exception as resolve_err:
            log.warning(
                "boq batch: works index auto-resolve failed (%s) — "
                "items without works_id will return an error",
                resolve_err,
            )

    # ── Process waves using the pool ─────────────────────────────────────────
    try:
        for wave_start in range(0, len(items), wave_size):
            wave = items[wave_start : wave_start + wave_size]
            wave_no = wave_start // wave_size + 1
            wave_total = (len(items) + wave_size - 1) // wave_size
            wave_pages = pool[: len(wave)]
            log.info(
                "boq batch: wave %d/%d — %d job(s) across %d tab(s)",
                wave_no,
                wave_total,
                len(wave),
                len(wave_pages),
            )
            try:
                wave_results = await asyncio.gather(
                    *[
                        _scrape_boq_job_on_page(
                            page,
                            item,
                            username=username,
                            password=password,
                            timeout_ms=timeout_ms,
                            after_relogin=after_relogin,
                        )
                        for page, item in zip(wave_pages, wave)
                    ],
                    return_exceptions=True,
                )
            except Exception as exc:
                interrupted = True
                interrupt_error = str(exc)
                log.error("boq batch: wave %d gather failed: %s", wave_no, exc)
                for item in wave:
                    results.append(_boq_batch_error_result(item, str(exc)))
                    errors += 1
                break

            for item, outcome in zip(wave, wave_results):
                if isinstance(outcome, Exception):
                    results.append(_boq_batch_error_result(item, str(outcome)))
                    errors += 1
                    continue
                results.append(outcome)
                if outcome.get("success"):
                    fetched += 1
                else:
                    errors += 1
    finally:
        if owns_pool:
            for p in pool:
                if not p.is_closed():
                    await p.close()
        # When using a persistent pool, leave tabs open for the next call.

    return {
        "success": fetched > 0 and not interrupted,
        "total": len(items),
        "fetched": fetched,
        "errors": errors,
        "interrupted": interrupted,
        "error": interrupt_error,
        "results": results,
    }
