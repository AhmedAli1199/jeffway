"""Weekly operative form-quality performance report."""

from __future__ import annotations

import html
import importlib.util
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_here = Path(__file__).resolve().parent


def _load_local_module(name: str, filename: str):
    path = _here / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load {filename} from {_here}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_fiv = _load_local_module("_stage6_form_issue_validation", "form_issue_validation.py")
_daily_exception_report = _load_local_module(
    "_stage6_daily_exception_report",
    "daily_exception_report.py",
)

_branding = _load_local_module("_stage6_report_branding", "report_branding.py")
PDF_HEADER_STYLES = _branding.PDF_HEADER_STYLES
pdf_footer_html = _branding.pdf_footer_html
pdf_header_html = _branding.pdf_header_html

find_ojs_row = _fiv.find_ojs_row
index_ojs_rows = _fiv.index_ojs_rows
issue_still_open = _fiv.issue_still_open
validate_ojs_row = _fiv.validate_ojs_row
_extract_first_date = _fiv._extract_first_date
_norm = _fiv._norm
_text = _fiv._text

try:
    from zoneinfo import ZoneInfo

    _UK = ZoneInfo("Europe/London")
except Exception:
    _UK = timezone.utc

_DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")


def _esc(value: Any) -> str:
    return html.escape(_text(value))


def _parse_uk_date(value: Any) -> Optional[datetime]:
    m = _DATE_RE.search(_text(value))
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return datetime(year, month, day, tzinfo=_UK)
    except ValueError:
        return None


def _uk_today() -> datetime:
    return datetime.now(_UK)


def _format_uk_date(dt: datetime) -> str:
    return dt.strftime("%d/%m/%Y")


def _week_window(week_days: int = 7) -> Tuple[datetime, datetime, str]:
    end = _uk_today().replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=max(week_days - 1, 0))
    label = f"{_format_uk_date(start)} – {_format_uk_date(end)}"
    return start, end, label


def _in_window(value: Any, start: datetime, end: datetime) -> bool:
    dt = _parse_uk_date(value)
    if not dt:
        return False
    return start.date() <= dt.date() <= end.date()


def _issue_key_from_row(row: Dict[str, Any]) -> str:
    return _text(row.get("issue_key") or row.get("Issue Key"))


def _status(row: Dict[str, Any]) -> str:
    return _norm(row.get("status") or row.get("Status"))


def collect_refresh_ids(
    issue_cases: List[Dict[str, Any]],
    *,
    week_days: int = 7,
) -> List[str]:
    """smart_form_x_id values to re-download from EasyBOP before weekly reconcile."""
    start, end, _ = _week_window(week_days)
    ids: set[str] = set()
    for raw in issue_cases:
        if not isinstance(raw, dict):
            continue
        first_detected = _text(
            raw.get("first_detected_date") or raw.get("First Detected Date")
        )
        in_week = _in_window(first_detected, start, end)
        is_open = _status(raw) == "open"
        if not in_week and not is_open:
            continue
        xid = _text(raw.get("smart_form_x_id") or raw.get("SmartForm X ID"))
        if xid:
            ids.add(xid)
    return sorted(ids)


def _payload_from_reconciled_rows(
    reconciled: List[Dict[str, Any]],
    *,
    week_label: str,
    week_days: int,
    report_generated_at: datetime,
    missing_ojs: Optional[List[Dict[str, Any]]] = None,
    sheet_updates: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    week_issues = [r for r in reconciled if r["in_report_week"]]
    still_open_week = [r for r in week_issues if r["still_open"]]
    resolved_week = [r for r in week_issues if not r["still_open"]]
    still_open_carry = [
        r for r in reconciled if r["still_open"] and not r["in_report_week"]
    ]

    op_stats: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {
            "raised": 0,
            "resolved": 0,
            "still_open": 0,
            "still_open_carry": 0,
        }
    )
    for row in week_issues:
        op = row["operative"] or "Unknown operative"
        op_stats[op]["raised"] += 1
        if row["still_open"]:
            op_stats[op]["still_open"] += 1
        else:
            op_stats[op]["resolved"] += 1
    for row in still_open_carry:
        op = row["operative"] or "Unknown operative"
        op_stats[op]["still_open_carry"] += 1

    operative_rows = []
    for operative, stats in sorted(
        op_stats.items(), key=lambda x: (-x[1]["raised"], x[0])
    ):
        raised = stats["raised"]
        resolved = stats["resolved"]
        rate = round((resolved / raised) * 100) if raised else 100
        operative_rows.append(
            {
                "operative": operative,
                "issues_raised": raised,
                "issues_resolved": resolved,
                "issues_still_open": stats["still_open"],
                "older_still_open": stats["still_open_carry"],
                "resolution_rate_pct": rate,
            }
        )

    missing_ojs = missing_ojs or []
    sheet_updates = sheet_updates or []

    return {
        "report_type": "weekly_form_performance_report",
        "report_generated_at": report_generated_at.isoformat(),
        "week_window_label": week_label,
        "week_days": week_days,
        "summary": {
            "issues_in_week": len(week_issues),
            "resolved_in_week": len(resolved_week),
            "still_open_in_week": len(still_open_week),
            "older_still_open": len(still_open_carry),
            "missing_ojs_rows": len(missing_ojs),
            "sheet_updates_count": len(sheet_updates),
            "affected_operatives": len(operative_rows),
        },
        "operative_stats": operative_rows,
        "week_issues": week_issues,
        "still_open_carry": still_open_carry,
        "missing_ojs": missing_ojs,
        "sheet_updates": sheet_updates,
    }


def summarize_weekly_from_issue_cases(
    issue_cases: List[Dict[str, Any]],
    *,
    week_days: int = 7,
    report_generated_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build weekly performance payload from Form Issue Cases sheet only.

    Trusts Status / Resolved Date / Issue Resolved maintained by the daily
    exception report — no EasyBOP re-fetch or OJS re-validation.
    """
    report_generated_at = report_generated_at or datetime.now(timezone.utc)
    start, end, week_label = _week_window(week_days)
    reconciled: List[Dict[str, Any]] = []

    for raw in issue_cases:
        if not isinstance(raw, dict):
            continue
        issue_key = _issue_key_from_row(raw)
        if not issue_key:
            continue

        first_detected = _text(
            raw.get("first_detected_date") or raw.get("First Detected Date")
        )
        still_open = _status(raw) == "open"
        in_week = _in_window(first_detected, start, end)

        if not in_week and not still_open:
            continue

        operative = _text(raw.get("operative") or raw.get("Created By"))
        issue_type = _text(raw.get("issue_type") or raw.get("Issue Type"))
        issue_detail = _text(
            raw.get("issue_detail")
            or raw.get("Issue Detail")
            or raw.get("Issue Summary")
            or raw.get("issue_summary")
        )
        issue_resolved = _text(
            raw.get("issue_resolved") or raw.get("Issue Resolved")
        )
        resolved_date = _text(
            raw.get("resolved_date") or raw.get("Resolved Date")
        )
        resolution_note = _text(
            raw.get("resolution_note") or raw.get("Resolution Note")
        )
        if still_open:
            resolution_note = resolution_note or "Still open on Form Issue Cases"
        elif issue_resolved:
            resolution_note = resolution_note or f"Resolved: {issue_resolved}"
        else:
            resolution_note = resolution_note or "Marked resolved on Form Issue Cases"

        reconciled.append(
            {
                "issue_key": issue_key,
                "operative": operative,
                "corf": _text(raw.get("corf") or raw.get("CORF")),
                "smart_form_x_id": _text(
                    raw.get("smart_form_x_id") or raw.get("SmartForm X ID")
                ),
                "smart_form_name": _text(
                    raw.get("smart_form_name")
                    or raw.get("SmartForm Name")
                    or raw.get("SmartForm Template Name")
                ),
                "issue_type": issue_type,
                "issue_detail": issue_detail,
                "first_detected_date": first_detected,
                "in_report_week": in_week,
                "status": "Open" if still_open else "Resolved",
                "still_open": still_open,
                "resolved_date": resolved_date,
                "resolution_note": resolution_note,
            }
        )

    return _payload_from_reconciled_rows(
        reconciled,
        week_label=week_label,
        week_days=week_days,
        report_generated_at=report_generated_at,
    )


def reconcile_weekly_form_issues(
    issue_cases: List[Dict[str, Any]],
    ojs_rows: List[Dict[str, Any]],
    *,
    ojs_rows_before: Optional[List[Dict[str, Any]]] = None,
    refreshed_ids: Optional[set[str]] = None,
    week_days: int = 7,
    report_generated_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    report_generated_at = report_generated_at or datetime.now(timezone.utc)
    start, end, week_label = _week_window(week_days)
    today_uk = _format_uk_date(_uk_today())
    ojs_index = index_ojs_rows(ojs_rows)
    ojs_index_before = (
        index_ojs_rows(ojs_rows_before) if ojs_rows_before is not None else None
    )
    refreshed_ids = refreshed_ids or set()

    sheet_updates: List[Dict[str, Any]] = []
    reconciled: List[Dict[str, Any]] = []
    missing_ojs: List[Dict[str, Any]] = []

    for raw in issue_cases:
        if not isinstance(raw, dict):
            continue
        issue_key = _issue_key_from_row(raw)
        if not issue_key:
            continue

        first_detected = _text(
            raw.get("first_detected_date") or raw.get("First Detected Date")
        )
        status = _status(raw)
        in_week = _in_window(first_detected, start, end)
        is_open = status == "open"

        if not in_week and not is_open:
            continue
        if not in_week and is_open:
            # still open from before this week — include in outstanding only
            pass

        operative = _text(raw.get("operative") or raw.get("Created By"))
        issue_type = _text(raw.get("issue_type") or raw.get("Issue Type"))
        issue_detail = _text(raw.get("issue_detail") or raw.get("Issue Detail"))

        ojs_row = find_ojs_row(raw, ojs_index)
        if not ojs_row:
            missing_ojs.append(
                {
                    "issue_key": issue_key,
                    "operative": operative,
                    "smart_form_x_id": _text(
                        raw.get("smart_form_x_id") or raw.get("SmartForm X ID")
                    ),
                    "corf": _text(raw.get("corf") or raw.get("CORF")),
                    "issue_type": issue_type,
                    "issue_detail": issue_detail,
                }
            )
            still_open = True
            resolution_note = "Operative Job Sheet row not found"
        else:
            validation = validate_ojs_row(ojs_row)
            still_open = issue_still_open(issue_type, issue_detail, validation)
            xid = _text(
                raw.get("smart_form_x_id") or raw.get("SmartForm X ID")
            )
            if (
                xid in refreshed_ids
                and ojs_index_before is not None
                and still_open is False
            ):
                row_before = find_ojs_row(raw, ojs_index_before)
                if row_before:
                    validation_before = validate_ojs_row(row_before)
                    if issue_still_open(
                        issue_type, issue_detail, validation_before
                    ):
                        still_open = True
                        resolution_note = (
                            "Issue still present on OJS sheet before refresh; "
                            "not auto-resolved from EasyBOP"
                        )
                    else:
                        resolution_note = (
                            "Issue no longer detected after EasyBOP refresh"
                        )
                else:
                    resolution_note = (
                        "Issue no longer detected on latest OJS row"
                    )
            elif still_open:
                resolution_note = "Issue still present on latest OJS row"
            else:
                resolution_note = "Issue no longer detected on OJS row"

        new_status = "Open" if still_open else "Resolved"
        resolved_date = (
            ""
            if still_open
            else _text(raw.get("resolved_date") or raw.get("Resolved Date"))
            or today_uk
        )

        sheet_updates.append(
            {
                "issue_key": issue_key,
                "Status": new_status,
                "Last Seen Date": today_uk,
                "Resolved Date": resolved_date,
                "Resolution Note": resolution_note,
            }
        )

        reconciled.append(
            {
                "issue_key": issue_key,
                "operative": operative,
                "corf": _text(raw.get("corf") or raw.get("CORF")),
                "smart_form_x_id": _text(
                    raw.get("smart_form_x_id") or raw.get("SmartForm X ID")
                ),
                "smart_form_name": _text(
                    raw.get("smart_form_name")
                    or raw.get("SmartForm Name")
                    or raw.get("SmartForm Template Name")
                ),
                "issue_type": issue_type,
                "issue_detail": issue_detail,
                "first_detected_date": first_detected,
                "in_report_week": in_week,
                "status": new_status,
                "still_open": still_open,
                "resolved_date": resolved_date,
                "resolution_note": resolution_note,
            }
        )

    return _payload_from_reconciled_rows(
        reconciled,
        week_label=week_label,
        week_days=week_days,
        report_generated_at=report_generated_at,
        missing_ojs=missing_ojs,
        sheet_updates=sheet_updates,
    )


def build_weekly_performance_report_html(payload: Dict[str, Any]) -> str:
    summary = payload["summary"]
    week_label = _esc(payload.get("week_window_label"))
    ops = payload.get("operative_stats") or []
    still_open = [r for r in payload.get("week_issues") or [] if r.get("still_open")]
    carry = payload.get("still_open_carry") or []

    op_rows = ""
    for row in ops:
        rate = row.get("resolution_rate_pct", 0)
        rate_cls = "ok" if rate >= 80 else ("warn" if rate >= 50 else "danger")
        op_rows += f"""
        <tr>
          <td>{_esc(row.get('operative'))}</td>
          <td class="num">{row.get('issues_raised', 0)}</td>
          <td class="num ok">{row.get('issues_resolved', 0)}</td>
          <td class="num danger">{row.get('issues_still_open', 0)}</td>
          <td class="num">{row.get('older_still_open', 0)}</td>
          <td class="num {rate_cls}">{rate}%</td>
        </tr>"""

    def issue_cards(rows: List[Dict[str, Any]]) -> str:
        if not rows:
            return '<p class="muted">None</p>'
        out = ""
        for row in rows:
            out += f"""
            <div class="card">
              <div class="card-title">{_esc(row.get('smart_form_name') or row.get('corf'))}</div>
              <div class="meta">{_esc(row.get('operative'))} · CORF {_esc(row.get('corf'))}</div>
              <div class="issue">{_esc(row.get('issue_type'))}: {_esc(row.get('issue_detail'))}</div>
              <div class="meta">First detected {_esc(row.get('first_detected_date'))}</div>
            </div>"""
        return out

    header = pdf_header_html(
        "Weekly Form Quality Performance",
        f"Week {week_label} (Europe/London)",
    )

    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
@page {{ margin: 14mm; }}
body {{ font-family: Arial, Helvetica, sans-serif; color: #17212b; font-size: 11px; }}
{PDF_HEADER_STYLES}
.stats {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 18px; }}
.stat {{ flex: 1; min-width: 110px; background: #f7fafb; border: 1px solid #dce3e8; border-radius: 8px; padding: 12px; }}
.stat .num {{ font-size: 24px; font-weight: 700; color: #145c63; }}
.stat .lbl {{ font-size: 10px; color: #56616b; text-transform: uppercase; }}
.stat.danger .num {{ color: #c0392b; }}
.stat.ok .num {{ color: #2e7d32; }}
h2 {{ font-size: 14px; margin: 20px 0 10px; color: #145c63; border-bottom: 2px solid #145c63; padding-bottom: 6px; }}
table {{ width: 100%; border-collapse: collapse; margin-bottom: 16px; }}
th, td {{ border-bottom: 1px solid #eef2f4; padding: 8px; text-align: left; }}
th {{ background: #eef4f5; font-size: 10px; text-transform: uppercase; color: #56616b; }}
td.num {{ text-align: center; font-weight: 600; }}
td.ok {{ color: #2e7d32; }}
td.warn {{ color: #e65100; }}
td.danger {{ color: #c0392b; }}
.card {{ border: 1px solid #dce3e8; border-radius: 8px; padding: 12px; margin-bottom: 8px; }}
.card-title {{ font-weight: 700; margin-bottom: 4px; }}
.meta {{ color: #56616b; font-size: 10px; }}
.issue {{ margin: 6px 0; }}
.muted {{ color: #888; }}
</style></head><body>
{header}
<div class="stats">
  <div class="stat"><div class="num">{summary.get('issues_in_week', 0)}</div><div class="lbl">Issues raised</div></div>
  <div class="stat ok"><div class="num">{summary.get('resolved_in_week', 0)}</div><div class="lbl">Resolved</div></div>
  <div class="stat danger"><div class="num">{summary.get('still_open_in_week', 0)}</div><div class="lbl">Still open (this week)</div></div>
  <div class="stat"><div class="num">{summary.get('older_still_open', 0)}</div><div class="lbl">Older still open</div></div>
</div>
<h2>Operative summary</h2>
<table>
  <thead><tr>
    <th>Operative</th><th>Raised</th><th>Resolved</th><th>Still open</th><th>Older open</th><th>Resolution %</th>
  </tr></thead>
  <tbody>{op_rows}</tbody>
</table>
<h2>Still open — raised this week ({len(still_open)})</h2>
{issue_cards(still_open)}
<h2>Still open — raised before this week ({len(carry)})</h2>
{issue_cards(carry)}
{pdf_footer_html("Stage 6 weekly form performance automation")}
</body></html>"""


def html_to_pdf_bytes(html_content: str) -> bytes:
    return _daily_exception_report.html_to_pdf_bytes(html_content)


def build_weekly_performance_report_pdf(
    payload: Dict[str, Any],
) -> Tuple[bytes, Dict[str, Any]]:
    if payload.get("report_type") not in (None, "weekly_form_performance_report"):
        raise ValueError("report_type must be weekly_form_performance_report.")

    html_content = build_weekly_performance_report_html(payload)
    pdf_bytes = html_to_pdf_bytes(html_content)
    summary = payload.get("summary") or {}
    slug = _uk_today().strftime("%Y-%m-%d")
    return pdf_bytes, {
        "date_slug": slug,
        "issues_in_week": summary.get("issues_in_week", 0),
        "resolved_in_week": summary.get("resolved_in_week", 0),
        "still_open_in_week": summary.get("still_open_in_week", 0),
    }
