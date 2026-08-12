"""
Stage 5 — Stalled appointment check (direct from EasyBOP).

1. Scheduling Register: rows with Appointment Status = "Appointment Started"
2. SmartForms register: Operative Job Sheet forms (all statuses)
3. For each started appointment with no matching Complete OJS form (operative + job + date):
   - If same operative booked to same job the next day → multi-day (OK)
   - Else → needs review
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger("stage5.stalled_check")

_ROOT = Path(__file__).resolve().parent.parent
for _candidate in ("Stage6", "Stage 6"):
    _stage6 = _ROOT / _candidate
    if _stage6.is_dir() and str(_stage6) not in sys.path:
        sys.path.insert(0, str(_stage6))

from compare_next_appointment import (  # noqa: E402
    SCHED_COL_ALIASES,
    _cell,
    _find_col,
    _parse_date,
    _read_xlsx_rows,
)
from month_filter import months_for_lookback, select_easybop_month  # noqa: E402

STARTED_STATUS = "appointment started"
COMPLETE_STATUS = "complete"

_SCrape_OJS_JS = """
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
        if (!/:\\s*BCC\\s+Respon(?:se|sive)\\s+Repairs\\b/i.test(formName)) return;

        rows.push({
            smart_form_x_id: row.id || null,
            template_name: templateName,
            status,
            form_name: formName,
            date_created: cells.list_date_created || null,
            date_completed: cells.list_date_completed || null,
            created_by: cells.list_created_by || null,
        });
    });
    return rows;
}
"""


def _norm_name(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _norm_address(value: Any) -> str:
    s = re.sub(r"[^\w\s]", " ", str(value or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def _address_from_form_name(name: str) -> str:
    # OJS form name looks like "<address>: BCC Response Repairs: <bcc works number>".
    # The trailing number is the BCC works order number, NOT the CORF — it never appears
    # in the Scheduling Register, so it cannot be used to match. The address prefix is the
    # only field the SmartForms register grid shares with the Scheduling Register.
    parts = str(name or "").split(":")
    return _norm_address(parts[0]) if parts else _norm_address(name)


def _job_key_from_sched(
    row: Dict[str, Any],
    headers: List[str],
    cols: Dict[str, Optional[int]],
) -> str:
    # Address is the only key shared with the SmartForms/OJS register grid (which exposes
    # the property address but not the CORF), so both sides key on the normalised address.
    addr = _norm_address(_cell(row, cols.get("address"), headers) or row.get("Address"))
    return f"ADDR:{addr}" if addr else ""


def _job_key_from_ojs(row: Dict[str, Any]) -> str:
    return f"ADDR:{_address_from_form_name(row.get('form_name') or '')}"


def _norm_status(status: str) -> str:
    s = re.sub(r"\s+", " ", str(status or "").strip().lower())
    s = re.sub(r"[–—−]", "-", s)
    return re.sub(r"\s*-\s*", " - ", s)


def _find_operative_col(headers: List[str]) -> Optional[int]:
    for alias in ("operative name", "operative", "staff name", "allocated operative"):
        idx = _find_col(headers, (alias,))
        if idx is not None:
            return idx
    return None


def _operative_name(row: Dict[str, Any], headers: List[str], cols: Dict[str, Optional[int]]) -> str:
    idx = cols.get("operative_name")
    if idx is not None:
        return str(_cell(row, idx, headers)).strip()
    return str(row.get("Operative Name") or "").strip()


def _started_rows(
    sched_rows: List[Dict[str, Any]],
    sched_headers: List[str],
    *,
    as_of: date,
    lookback_days: int,
) -> List[Dict[str, Any]]:
    cols = {k: _find_col(sched_headers, v) for k, v in SCHED_COL_ALIASES.items()}
    cols["operative_name"] = _find_operative_col(sched_headers)
    earliest = as_of - timedelta(days=max(lookback_days - 1, 0))
    seen: set[Tuple[str, str, str]] = set()
    out: List[Dict[str, Any]] = []

    for row in sched_rows:
        status = _norm_status(_cell(row, cols.get("status"), sched_headers))
        if status != STARTED_STATUS:
            continue
        operative = _operative_name(row, sched_headers, cols)
        if not operative:
            continue
        appt_raw = _cell(row, cols.get("appointment_date"), sched_headers) or row.get("Appointment Date") or ""
        appt_date = _parse_date(appt_raw)
        if appt_date is None:
            continue
        if appt_date > as_of or appt_date < earliest:
            continue
        job_key = _job_key_from_sched(row, sched_headers, cols)
        if not job_key:
            continue
        dedupe = (_norm_name(operative), job_key, appt_date.isoformat())
        if dedupe in seen:
            continue
        seen.add(dedupe)
        out.append(
            {
                "operative_name": operative,
                "appointment_date": appt_date.isoformat(),
                "appointment_date_raw": str(appt_raw).strip(),
                "address": _cell(row, cols.get("address"), sched_headers) or row.get("Address") or "",
                "client_reference": _cell(row, cols.get("client_order_reference"), sched_headers)
                or row.get("Client Reference")
                or "",
                "our_reference": str(row.get("Our Reference") or "").strip(),
                "description": str(row.get("Description") or row.get("Description of Work") or "").strip(),
                "job_key": job_key,
                "appointment_status": _cell(row, cols.get("status"), sched_headers),
            }
        )
    return out


def _ojs_date_matches(appt_date: date, ojs_row: Dict[str, Any]) -> bool:
    for field in ("date_created", "date_completed"):
        d = _parse_date(ojs_row.get(field))
        if d is None:
            continue
        if d == appt_date:
            return True
        if d == appt_date + timedelta(days=1):
            return True
    return False


def _has_complete_ojs(
    operative: str,
    job_key: str,
    appt_date: date,
    ojs_rows: List[Dict[str, Any]],
) -> bool:
    op = _norm_name(operative)
    for row in ojs_rows:
        if _norm_status(row.get("status") or "") != COMPLETE_STATUS:
            continue
        if _job_key_from_ojs(row) != job_key:
            continue
        # Operative must match when the OJS form records who created it. ~13% of Complete
        # forms have a blank "Created By" on the register grid; for those we still accept the
        # match on job + date so we don't raise a false "stalled" alert for completed work.
        row_op = _norm_name(row.get("created_by"))
        if row_op and op and row_op != op:
            continue
        if _ojs_date_matches(appt_date, row):
            return True
    return False


def _booked_next_day(
    operative: str,
    job_key: str,
    appt_date: date,
    sched_rows: List[Dict[str, Any]],
    sched_headers: List[str],
) -> Optional[Dict[str, Any]]:
    cols = {k: _find_col(sched_headers, v) for k, v in SCHED_COL_ALIASES.items()}
    cols["operative_name"] = _find_operative_col(sched_headers)
    next_day = appt_date + timedelta(days=1)
    op = _norm_name(operative)
    for row in sched_rows:
        row_op = _operative_name(row, sched_headers, cols)
        if _norm_name(row_op) != op:
            continue
        row_key = _job_key_from_sched(row, sched_headers, cols)
        if row_key != job_key:
            continue
        row_date = _parse_date(
            _cell(row, cols.get("appointment_date"), sched_headers) or row.get("Appointment Date")
        )
        if row_date != next_day:
            continue
        status_raw = str(_cell(row, cols.get("status"), sched_headers) or row.get("Appointment Status") or "").strip()
        status = _norm_status(status_raw)
        if status in ("n/a", ""):
            continue
        return {
            "next_appointment_date": next_day.isoformat(),
            "next_appointment_date_raw": str(
                _cell(row, cols.get("appointment_date"), sched_headers) or row.get("Appointment Date") or ""
            ).strip(),
            "next_appointment_status": status_raw,
        }
    return None


def compute_stalled_appointments(
    sched_rows: List[Dict[str, Any]],
    sched_headers: List[str],
    ojs_rows: List[Dict[str, Any]],
    *,
    as_of: Optional[date] = None,
    lookback_days: int = 14,
) -> Dict[str, Any]:
    as_of = as_of or date.today()
    started = _started_rows(sched_rows, sched_headers, as_of=as_of, lookback_days=lookback_days)

    with_complete: List[Dict[str, Any]] = []
    returning_next_day: List[Dict[str, Any]] = []
    needs_review: List[Dict[str, Any]] = []

    for item in started:
        appt_date = _parse_date(item["appointment_date"])
        if appt_date is None:
            continue
        if _has_complete_ojs(item["operative_name"], item["job_key"], appt_date, ojs_rows):
            with_complete.append({**item, "result": "complete_ojs_form_found"})
            continue

        nxt = _booked_next_day(
            item["operative_name"], item["job_key"], appt_date, sched_rows, sched_headers
        )
        if nxt:
            returning_next_day.append(
                {
                    **item,
                    "result": "returning_next_day",
                    "message": (
                        f"{item['operative_name']} at {item['address']} on {item['appointment_date_raw']} "
                        f"shows as Started with no Complete OJS form, but is booked again on "
                        f"{nxt['next_appointment_date_raw']} (multi-day job)."
                    ),
                    **nxt,
                }
            )
        else:
            needs_review.append(
                {
                    **item,
                    "result": "needs_review",
                    "message": (
                        f"Appointment for {item['operative_name']} at {item['address']} on "
                        f"{item['appointment_date_raw']} shows as Started but not Completed. "
                        f"Operative is not scheduled to return. Please review."
                    ),
                }
            )

    return {
        "as_of": as_of.isoformat(),
        "lookback_days": lookback_days,
        "months_checked": [],
        "scheduling_rows": len(sched_rows),
        "ojs_register_rows": len(ojs_rows),
        "started_appointments_checked": len(started),
        "with_complete_ojs_form": with_complete,
        "returning_next_day": returning_next_day,
        "needs_review": needs_review,
        "summary": {
            "started_count": len(started),
            "complete_ojs_count": len(with_complete),
            "returning_next_day_count": len(returning_next_day),
            "needs_review_count": len(needs_review),
            "email_count": len(returning_next_day) + len(needs_review),
        },
    }


def build_stalled_email_html(result: Dict[str, Any]) -> Tuple[str, str]:
    """Return (subject, html) for Gmail — covers returning next day AND needs review."""
    summary = result.get("summary") or {}
    returning = result.get("returning_next_day") or []
    review = result.get("needs_review") or []
    months = result.get("months_checked") or []
    as_of = result.get("as_of") or ""
    lookback = result.get("lookback_days") or ""

    def esc(t: Any) -> str:
        return (
            str(t if t is not None else "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    def row_cells(item: Dict[str, Any]) -> str:
        return (
            f"<tr>"
            f"<td>{esc(item.get('operative_name'))}</td>"
            f"<td>{esc(item.get('address'))}</td>"
            f"<td>{esc(item.get('appointment_date_raw'))}</td>"
            f"<td>{esc(item.get('client_reference') or item.get('our_reference'))}</td>"
            f"</tr>"
        )

    returning_rows = "".join(row_cells(r) for r in returning) or (
        "<tr><td colspan='4' style='color:#888;font-style:italic;'>None</td></tr>"
    )
    review_rows = "".join(row_cells(r) for r in review) or (
        "<tr><td colspan='4' style='color:#888;font-style:italic;'>None</td></tr>"
    )

    returning_msgs = "".join(
        f"<li style='margin-bottom:8px;'>{esc(r.get('message'))}</li>" for r in returning
    )
    review_msgs = "".join(
        f"<li style='margin-bottom:8px;color:#c0392b;'>{esc(r.get('message'))}</li>" for r in review
    )

    subject = (
        f"Stage 5 Stalled Appointments — {summary.get('needs_review_count', 0)} review, "
        f"{summary.get('returning_next_day_count', 0)} returning next day ({as_of})"
    )

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;font-family:Arial,sans-serif;background:#f4f4f4;">
<table width="100%" style="background:#f4f4f4;padding:20px;"><tr><td align="center">
<table width="720" style="background:#fff;border-radius:8px;overflow:hidden;">
<tr><td style="background:#111;color:#fff;padding:20px 24px;">
  <h2 style="margin:0 0 6px;font-size:20px;">Stage 5 — Stalled Appointment Check</h2>
  <p style="margin:0;opacity:0.85;font-size:13px;">As of {esc(as_of)} · lookback {esc(lookback)} days · months: {esc(', '.join(months) or '—')}</p>
</td></tr>
<tr><td style="padding:20px 24px;">
  <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:20px;">
    <tr>
      <td style="background:#f7f7f7;border:1px solid #e8e8e8;border-radius:8px;padding:14px;text-align:center;width:20%;">
        <div style="font-size:24px;font-weight:700;">{summary.get('started_count', 0)}</div>
        <div style="font-size:11px;color:#666;text-transform:uppercase;">Started checked</div>
      </td>
      <td width="8"></td>
      <td style="background:#fff8e1;border:1px solid #ffe082;border-radius:8px;padding:14px;text-align:center;width:20%;">
        <div style="font-size:24px;font-weight:700;color:#f57f17;">{summary.get('returning_next_day_count', 0)}</div>
        <div style="font-size:11px;color:#666;text-transform:uppercase;">Returning next day</div>
      </td>
      <td width="8"></td>
      <td style="background:#ffebee;border:1px solid #f5c6c6;border-radius:8px;padding:14px;text-align:center;width:20%;">
        <div style="font-size:24px;font-weight:700;color:#c0392b;">{summary.get('needs_review_count', 0)}</div>
        <div style="font-size:11px;color:#666;text-transform:uppercase;">Needs review</div>
      </td>
      <td width="8"></td>
      <td style="background:#e8f5e9;border:1px solid #c8e6c9;border-radius:8px;padding:14px;text-align:center;width:20%;">
        <div style="font-size:24px;font-weight:700;color:#2e7d32;">{summary.get('complete_ojs_count', 0)}</div>
        <div style="font-size:11px;color:#666;text-transform:uppercase;">Complete OJS (skipped)</div>
      </td>
    </tr>
  </table>

  <h3 style="margin:24px 0 10px;color:#f57f17;border-bottom:2px solid #ffe082;padding-bottom:6px;">
    Returning next day — multi-day job (OK)
  </h3>
  <table width="100%" cellpadding="8" cellspacing="0" style="border-collapse:collapse;font-size:12px;margin-bottom:10px;">
    <tr style="background:#fff8e1;">
      <th align="left">Operative</th><th align="left">Address</th><th align="left">Appointment</th><th align="left">Ref</th>
    </tr>
    {returning_rows}
  </table>
  <ul style="font-size:12px;color:#444;padding-left:18px;margin:0 0 20px;">{returning_msgs}</ul>

  <h3 style="margin:24px 0 10px;color:#c0392b;border-bottom:2px solid #f5c6c6;padding-bottom:6px;">
    Needs review — not scheduled to return
  </h3>
  <table width="100%" cellpadding="8" cellspacing="0" style="border-collapse:collapse;font-size:12px;margin-bottom:10px;">
    <tr style="background:#ffebee;">
      <th align="left">Operative</th><th align="left">Address</th><th align="left">Appointment</th><th align="left">Ref</th>
    </tr>
    {review_rows}
  </table>
  <ul style="font-size:12px;padding-left:18px;margin:0;">{review_msgs}</ul>
</td></tr>
<tr><td style="background:#f9f9f9;padding:14px;text-align:center;font-size:11px;color:#777;">
  Jeffway Group · Stage 5 stalled appointment automation · EasyBOP direct (no sheets)
</td></tr>
</table></td></tr></table></body></html>"""

    return subject, html


async def _ensure_ojs_register(
    page: "Page",
    *,
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout_ms: int = 30_000,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> None:
    from automation import (  # noqa: WPS433
        REGISTER_URL,
        _shows_login_wall,
        _wait_for_grid,
        login,
    )

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
    await _wait_for_grid(page, timeout_ms)


async def scrape_ojs_register_month(
    page: "Page",
    year: int,
    month: int,
    *,
    timeout_ms: int = 30_000,
    max_pages: int = 50,
) -> List[Dict[str, Any]]:
    from automation import _go_to_next_grid_page  # noqa: WPS433

    selected = await select_easybop_month(page, year, month, timeout_ms=timeout_ms)
    if not selected:
        log.warning("ojs register: month %s-%02d filter not applied", year, month)

    rows: List[Dict[str, Any]] = []
    for page_no in range(max_pages):
        batch = await page.evaluate(_SCrape_OJS_JS)
        rows.extend(batch)
        log.info("ojs register %s-%02d page %s: %s rows", year, month, page_no + 1, len(batch))
        if not await _go_to_next_grid_page(page):
            break
    return rows


async def scrape_ojs_register(
    page: "Page",
    *,
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout_ms: int = 30_000,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
    max_pages: int = 50,
    as_of: Optional[date] = None,
    lookback_days: int = 14,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    await _ensure_ojs_register(
        page,
        username=username,
        password=password,
        timeout_ms=timeout_ms,
        after_relogin=after_relogin,
    )

    as_of = as_of or date.today()
    month_list = months_for_lookback(as_of, lookback_days)
    month_labels = [f"{y}-{m:02d}" for y, m in month_list]
    seen_ids: set[str] = set()
    all_rows: List[Dict[str, Any]] = []

    for year, month in month_list:
        batch = await scrape_ojs_register_month(
            page, year, month, timeout_ms=timeout_ms, max_pages=max_pages
        )
        new_count = 0
        for row in batch:
            sfid = str(row.get("smart_form_x_id") or "").strip()
            if sfid and sfid in seen_ids:
                continue
            if sfid:
                seen_ids.add(sfid)
            all_rows.append(row)
            new_count += 1
        log.info(
            "ojs register month %s-%02d: %s scraped, %s new (total %s)",
            year,
            month,
            len(batch),
            new_count,
            len(all_rows),
        )

    return all_rows, month_labels


async def merge_scheduling_exports(paths: List[Path]) -> Tuple[List[str], List[Dict[str, Any]]]:
    headers: List[str] = []
    rows: List[Dict[str, Any]] = []
    for path in paths:
        h, part = _read_xlsx_rows(path)
        if h and not headers:
            headers = h
        rows.extend(part)
    return headers, rows


async def run_stalled_check(
    page: "Page",
    scheduling_paths: List[Path],
    *,
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout_ms: int = 30_000,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
    as_of: Optional[date] = None,
    lookback_days: int = 14,
    months_checked: Optional[List[str]] = None,
) -> Dict[str, Any]:
    sched_headers, sched_rows = await merge_scheduling_exports(scheduling_paths)
    ojs_rows, ojs_months = await scrape_ojs_register(
        page,
        username=username,
        password=password,
        timeout_ms=timeout_ms,
        after_relogin=after_relogin,
        as_of=as_of,
        lookback_days=lookback_days,
    )
    result = compute_stalled_appointments(
        sched_rows,
        sched_headers,
        ojs_rows,
        as_of=as_of,
        lookback_days=lookback_days,
    )
    result["months_checked"] = months_checked or ojs_months
    subject, html = build_stalled_email_html(result)
    result["email_subject"] = subject
    result["email_html"] = html
    return result
