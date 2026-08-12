"""Build Stage 6 daily exception PDF report from n8n-processed payload.

The n8n "Build Daily Form Issue Cases" node posts a payload shaped like::

    {
      "report_type": "daily_form_report",
      "report_generated_at": "<iso datetime>",
      "report_date": "DD/MM/YYYY",
      "summary": {
        "new_issue_count": int,
        "affected_operatives": int,
        "affected_forms": int,
        "access_issue_forms": int
      },
      "operative_groups": [
        {
          "operative": "<name>",
          "issue_count": int,
          "forms": [
            {
              "issue_key", "corf", "smart_form_name", "form_date",
              "smart_form_x_id", "pdf_url", "issue_type", "issue_detail",
              "issue_summary", "access_issue", "access_issue_reason"
            }
          ]
        }
      ]
    }
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from report_branding import PDF_HEADER_STYLES, pdf_footer_html, pdf_header_html
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _esc(value: Any) -> str:
    return html.escape(_text(value))


def _safe_url(value: Any) -> str:
    url = _text(value)
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return html.escape(url, quote=True)


def _parse_report_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        text = _text(value)
        if not text:
            return datetime.now(timezone.utc)
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            result = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(
                "report_generated_at must be a valid ISO date."
            ) from exc

    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------

def _groups(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    groups = payload.get("operative_groups") or []
    return [g for g in groups if isinstance(g, dict)]


def _group_forms(group: Dict[str, Any]) -> List[Dict[str, Any]]:
    forms = group.get("forms") or []
    return [f for f in forms if isinstance(f, dict)]


def _form_fingerprint(form: Dict[str, Any]) -> str:
    fid = _text(form.get("smart_form_x_id"))
    if fid:
        return f"id:{fid}"
    return f"corf:{_text(form.get('corf'))}|{_text(form.get('form_date'))}"


def _compute_meta(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Recompute headline counters from operative_groups and summary."""
    groups = _groups(payload)
    summary = payload.get("summary") or {}

    new_issue_count = 0
    existing_open_count = 0
    affected_operatives = 0
    unique_forms: set = set()
    access_forms: set = set()

    for group in groups:
        forms = _group_forms(group)
        if not forms:
            continue
        affected_operatives += 1
        for form in forms:
            if form.get("is_new") is True:
                new_issue_count += 1
            else:
                existing_open_count += 1
            fp = _form_fingerprint(form)
            unique_forms.add(fp)
            if form.get("access_issue") is True:
                access_forms.add(fp)

    total_issue_count = new_issue_count + existing_open_count
    if summary.get("total_issue_count") is not None:
        total_issue_count = max(
            total_issue_count,
            int(summary.get("total_issue_count") or 0),
        )

    return {
        "new_issue_count": new_issue_count,
        "existing_open_count": existing_open_count,
        "total_issue_count": total_issue_count,
        "affected_operatives": affected_operatives,
        "affected_forms": len(unique_forms),
        "access_issue_forms": len(access_forms),
    }


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

_STYLES = """
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: Arial, Helvetica, sans-serif;
  color: #17212b;
  font-size: 12px;
  line-height: 1.45;
}
.header {
  border-bottom: 3px solid #145c63;
  padding-bottom: 12px;
  margin-bottom: 18px;
}
.header h1 {
  margin: 0 0 4px;
  font-size: 22px;
  color: #145c63;
}
.header .meta {
  color: #56616b;
  font-size: 11px;
}
.cards {
  display: flex;
  gap: 10px;
  margin-bottom: 22px;
}
.card {
  flex: 1;
  border: 1px solid #dce3e8;
  border-radius: 8px;
  padding: 12px 14px;
  background: #f7fafb;
}
.card .value {
  font-size: 24px;
  font-weight: 700;
  color: #145c63;
}
.card .label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  color: #56616b;
}
.card.access .value { color: #b23c17; }
.op-group {
  margin-bottom: 20px;
  page-break-inside: avoid;
}
.op-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: #145c63;
  color: #fff;
  padding: 8px 12px;
  border-radius: 6px 6px 0 0;
  font-size: 13px;
  font-weight: 700;
}
.op-head .count {
  background: rgba(255, 255, 255, 0.2);
  padding: 2px 9px;
  border-radius: 10px;
  font-size: 11px;
}
table {
  width: 100%;
  border-collapse: collapse;
  border: 1px solid #dce3e8;
  border-top: none;
}
th, td {
  text-align: left;
  padding: 7px 10px;
  border-bottom: 1px solid #eef2f4;
  vertical-align: top;
}
th {
  background: #eef4f5;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.3px;
  color: #56616b;
}
td .form-name { font-weight: 600; }
td .corf { color: #56616b; font-size: 10px; }
.tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 10px;
  font-weight: 600;
  white-space: nowrap;
}
.tag.issue { background: #fbe9e2; color: #b23c17; }
.tag.photo { background: #fff2d6; color: #9a6b00; }
.tag.access { background: #fde2e2; color: #b3261e; }
.tag.new { background: #e3f2fd; color: #1565c0; }
.tag.carry { background: #eef4f5; color: #56616b; }
.access-note {
  margin-top: 4px;
  color: #b3261e;
  font-size: 10px;
}
a { color: #145c63; }
.empty {
  border: 1px dashed #cbd6dc;
  border-radius: 8px;
  padding: 40px 20px;
  text-align: center;
  color: #56616b;
  background: #f7fafb;
}
.empty .big { font-size: 18px; color: #2f8f7a; font-weight: 700; }
""" + PDF_HEADER_STYLES


def _issue_tag(issue_type: str) -> str:
    lowered = issue_type.lower()
    cls = "photo" if "photo" in lowered else "issue"
    label = _esc(issue_type) or "Issue"
    return f'<span class="tag {cls}">{label}</span>'


def _form_row(form: Dict[str, Any]) -> str:
    name = _esc(form.get("smart_form_name")) or "Operative Job Sheet"
    corf = _esc(form.get("corf"))
    corf_html = f'<div class="corf">CORF {corf}</div>' if corf else ""

    date = _esc(form.get("form_date"))

    issue_type = _text(form.get("issue_type"))
    detail = _esc(
        form.get("issue_detail") or form.get("issue_summary")
    ) or "&mdash;"
    resolved = _esc(form.get("issue_resolved"))
    resolved_html = (
        f'<div class="meta">Resolved: {resolved}</div>'
        if resolved
        else ""
    )

    status_tag = (
        '<span class="tag new">New today</span>'
        if form.get("is_new") is True
        else '<span class="tag carry">Still open</span>'
    )

    access_html = ""
    if form.get("access_issue") is True:
        reason = _esc(form.get("access_issue_reason"))
        reason_html = (
            f'<div class="access-note">{reason}</div>' if reason else ""
        )
        access_html = f'<span class="tag access">Access issue</span>{reason_html}'

    url = _safe_url(form.get("pdf_url"))
    link_html = f'<a href="{url}">View form</a>' if url else "&mdash;"

    return (
        "<tr>"
        f'<td><div class="form-name">{name}</div>{corf_html}</td>'
        f"<td>{date or '&mdash;'}</td>"
        f"<td>{status_tag}<br>{_issue_tag(issue_type)}</td>"
        f"<td>{detail}{resolved_html}</td>"
        f"<td>{access_html or '&mdash;'}</td>"
        f"<td>{link_html}</td>"
        "</tr>"
    )


def _operative_group_html(group: Dict[str, Any]) -> str:
    forms = _group_forms(group)
    if not forms:
        return ""

    operative = _esc(group.get("operative")) or "Unknown operative"
    rows = "".join(_form_row(form) for form in forms)

    return (
        '<div class="op-group">'
        '<div class="op-head">'
        f"<span>{operative}</span>"
        f'<span class="count">{len(forms)} issue'
        f"{'' if len(forms) == 1 else 's'}</span>"
        "</div>"
        "<table>"
        "<thead><tr>"
        "<th>Form</th><th>Date</th><th>Issue</th>"
        "<th>Details</th><th>Access</th><th>Link</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
        "</div>"
    )


def build_daily_exception_report_html(
    payload: Dict[str, Any],
    report_dt: datetime,
) -> str:
    meta = _compute_meta(payload)
    groups = _groups(payload)

    report_date = _esc(payload.get("report_date")) or report_dt.strftime(
        "%d/%m/%Y"
    )
    generated = report_dt.strftime("%d %b %Y, %H:%M UTC")

    cards = (
        '<div class="cards">'
        f'<div class="card"><div class="value">{meta["new_issue_count"]}'
        '</div><div class="label">New today</div></div>'
        f'<div class="card"><div class="value">{meta["existing_open_count"]}'
        '</div><div class="label">Still open</div></div>'
        f'<div class="card"><div class="value">{meta["total_issue_count"]}'
        '</div><div class="label">Total in report</div></div>'
        f'<div class="card"><div class="value">{meta["affected_operatives"]}'
        '</div><div class="label">Operatives</div></div>'
        f'<div class="card access"><div class="value">'
        f'{meta["access_issue_forms"]}'
        '</div><div class="label">Access issues</div></div>'
        "</div>"
    )

    body_groups = "".join(_operative_group_html(g) for g in groups)
    if not body_groups:
        body_groups = (
            '<div class="empty">'
            '<div class="big">No form issues in scope</div>'
            "<div>All operative job sheets in scope passed validation "
            "and there are no open cases on the tracker sheet.</div>"
            "</div>"
        )

    header = pdf_header_html(
        "Daily Form Quality Report",
        f"Report date {report_date} · Generated {generated}",
    )

    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>{_STYLES}</style></head><body>"
        f"{header}"
        f"{cards}"
        f"{body_groups}"
        + pdf_footer_html("Stage 6 daily form quality automation")
        + "</body></html>"
    )


# ---------------------------------------------------------------------------
# PDF rendering
# ---------------------------------------------------------------------------

def html_to_pdf_bytes(html_content: str) -> bytes:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(html_content, wait_until="networkidle")
            pdf = page.pdf(
                format="A4",
                print_background=True,
                margin={
                    "top": "12mm",
                    "right": "12mm",
                    "bottom": "12mm",
                    "left": "12mm",
                },
            )
            return pdf
        finally:
            browser.close()


def build_daily_exception_report_pdf(
    payload: Dict[str, Any],
) -> Tuple[bytes, Dict[str, Any]]:
    report_dt = _parse_report_datetime(payload.get("report_generated_at"))

    html_content = build_daily_exception_report_html(payload, report_dt)
    pdf_bytes = html_to_pdf_bytes(html_content)

    meta = _compute_meta(payload)
    meta["date_slug"] = report_dt.strftime("%Y-%m-%d")
    return pdf_bytes, meta
