"""Build Stage 6 job status PDF report from Operative Job Sheet + OJS BOQ rows."""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from report_branding import PDF_HEADER_STYLES, pdf_footer_html, pdf_header_html
from typing import Any, Dict, List, Optional, Tuple


def _norm(v: Any) -> str:
    return str(v if v is not None else "").strip()


def _smart_form_name(row: Dict[str, Any]) -> str:
    return _norm(
        row.get("SmartForm Name")
        or row.get("smart_form_name")
        or row.get("form_name")
        or row.get("SmartForm name")
    )


def _corf_from_name(name: str) -> str:
    m = re.search(r":\s*(\d{4,})\s*$", name)
    return m.group(1) if m else ""


def _address_from_name(name: str) -> str:
    parts = name.split(":")
    return parts[0].strip() if parts else name


def _parse_hours(raw: Any) -> float:
    s = _norm(raw)
    if not s:
        return 0.0
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    return float(m.group(1)) if m else 0.0


def _parse_smv_hours(row: Dict[str, Any]) -> Optional[float]:
    h = row.get("Job SMV Allowed Hours") or row.get("job_smv_allowed_hours")
    if h is not None and _norm(h):
        n = _parse_hours(h)
        return n if n > 0 else None
    mins = row.get("Job SMV Allowed Minutes") or row.get("job_smv_allowed_minutes")
    if mins is not None and _norm(mins):
        n = _parse_hours(mins)
        return round(n / 60, 2) if n > 0 else None
    return None


def _job_key(row: Dict[str, Any]) -> str:
    sfn = _smart_form_name(row)
    if sfn:
        return sfn.upper()
    corf = _norm(row.get("CORF") or row.get("corf"))
    return f"CORF:{corf}" if corf else ""


def _is_header(row: Dict[str, Any]) -> bool:
    return bool(re.match(r"^smartform name$", _smart_form_name(row), re.I))


def _is_real_boq_row(row: Dict[str, Any]) -> bool:
    rt = _norm(row.get("Row Type") or row.get("Row_Type") or row.get("row_type"))
    if rt == "sor_line":
        return True
    sor = _norm(row.get("SOR Code") or row.get("sor_code"))
    return bool(sor and not re.match(r"^(no_sor_lines|fetch_error)$", sor, re.I))


def _status_norm(raw: Any) -> str:
    s = _norm(raw).lower()
    if s.startswith("comp"):
        return "completed"
    if s.startswith("incomp") or s == "incomplete":
        return "incomplete"
    return ""


def _time_vs_smv_label(spent: float, allowed: Optional[float], existing: str) -> str:
    if _norm(existing):
        return _norm(existing)
    if allowed is None or allowed <= 0:
        return "SMV unknown"
    if spent <= allowed * 0.5:
        return "Under SMV"
    if spent <= allowed * 1.25:
        return "Within allowance"
    return "Over SMV"


def _parse_yes_no(raw: Any) -> bool:
    return _norm(raw).lower() in ("yes", "true", "1")


def _ready_to_handover(row: Dict[str, Any]) -> bool:
    """True if EasyBOP/sheet Ready To Handover column is Yes."""
    for key, val in (row or {}).items():
        if re.match(r"^ready\s*to\s*hand", str(key), re.I):
            return _parse_yes_no(val)
    return _parse_yes_no(
        row.get("Ready To Hand over")
        or row.get("Ready To Handover")
        or row.get("ready_to_handover")
    )


# BCC Tracker Status labels (EasyBOP Works UDF) — report mirrors these exactly.
TRACKER_WIP = "WIP"
TRACKER_AWAITING_NEXT_APPOINTMENT = "Awaiting next appointment"
TRACKER_MANAGER_REVIEW = "Manager review needed"
TRACKER_AWAITING_HANDOVER = "Awaiting handover info"
SECTION_OJS_AWAITING_EASYBOP = "OJS scope complete — awaiting EasyBOP date"


def _is_ojs_scope_complete(job: Dict[str, Any]) -> bool:
    return job.get("job_status") == "completed" and not _norm(job.get("completed_date"))


def tracker_status_for_job(job: Dict[str, Any]) -> str:
    """Single BCC Tracker Status value for a job (priority order)."""
    if _is_ojs_scope_complete(job):
        return SECTION_OJS_AWAITING_EASYBOP
    if job.get("job_status") == "completed":
        return TRACKER_AWAITING_HANDOVER
    jt = _norm(job.get("jeopardy_type"))
    if jt == "next_appointment_needed":
        return TRACKER_AWAITING_NEXT_APPOINTMENT
    if jt == "manager_review":
        return TRACKER_MANAGER_REVIEW
    if job.get("in_jeopardy"):
        return TRACKER_AWAITING_NEXT_APPOINTMENT
    if _norm(job.get("appointment_date")):
        return TRACKER_WIP
    return ""


def _jeopardy_type_label(jeopardy_type: str) -> str:
    if jeopardy_type == "next_appointment_needed":
        return TRACKER_AWAITING_NEXT_APPOINTMENT
    if jeopardy_type == "manager_review":
        return TRACKER_MANAGER_REVIEW
    return TRACKER_AWAITING_NEXT_APPOINTMENT


def aggregate_jobs(
    ojs_rows: List[Dict[str, Any]],
    boq_rows: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    jobs: Dict[str, Dict[str, Any]] = {}

    for row in ojs_rows or []:
        if _is_header(row):
            continue
        key = _job_key(row)
        if not key:
            continue
        if key not in jobs:
            name = _smart_form_name(row)
            jobs[key] = {
                "job_key": key,
                "smart_form_name": name,
                "corf": _norm(row.get("CORF") or row.get("corf")) or _corf_from_name(name),
                "address": _norm(row.get("Q1.1 Address") or row.get("Q1.1_address"))
                or _address_from_name(name),
                "visits": [],
            }
        jobs[key]["visits"].append(row)

    boq_rows = boq_rows or []
    smv_from_boq: Dict[str, float] = {}
    for row in boq_rows:
        if _is_header(row) or not _is_real_boq_row(row):
            continue
        key = _job_key(row)
        if not key:
            continue
        jm = row.get("Job Estimated Minutes") or row.get("job_estimated_minutes")
        if jm is not None and _norm(jm):
            n = _parse_hours(jm)
            if n > 0:
                smv_from_boq[key] = round(n / 60, 2)
        jh = row.get("Job Estimated Hours") or row.get("job_estimated_hours")
        if jh is not None and _norm(jh):
            n = _parse_hours(jh)
            if n > 0:
                smv_from_boq[key] = n

    out: List[Dict[str, Any]] = []
    for key, job in jobs.items():
        visits = job["visits"]
        if not visits:
            continue

        spent = None
        breakdown = ""
        for v in visits:
            jts = _norm(v.get("Job Time Spent Hours") or v.get("job_time_spent_hours"))
            if jts:
                spent = _parse_hours(jts)
            ob = _norm(v.get("Operative Time Breakdown") or v.get("operative_time_breakdown"))
            if ob:
                breakdown = ob

        if spent is None:
            spent = 0.0
            by_op: Dict[str, float] = {}
            for v in visits:
                h = _parse_hours(
                    v.get("Q2.1 Daily Jobs Table Price/Hours")
                    or v.get("Visit Hours")
                    or v.get("visit_hours")
                )
                spent += h
                op = _norm(v.get("Created By") or v.get("Q4.1 Employee Name") or "Unknown")
                by_op[op] = by_op.get(op, 0.0) + h
            breakdown = "; ".join(f"{name}: {round(h, 2)}h" for name, h in sorted(by_op.items()))

        spent = round(spent, 2)

        status = ""
        notes = ""
        in_jeopardy = False
        jeopardy_type = ""
        jeopardy_reason = ""
        time_existing = ""
        smv_allowed = None
        appointment_date = ""
        next_appointment_date = ""
        completed_date = ""
        ready_to_handover = False
        for v in visits:
            s = _status_norm(v.get("Job Status") or v.get("job_status"))
            if s:
                status = s
            cd = _norm(v.get("Completed") or v.get("completed") or v.get("completed_date"))
            if cd:
                completed_date = cd
            ad = _norm(v.get("Appointment Date") or v.get("appointment_date"))
            if ad:
                appointment_date = ad
            nad = _norm(v.get("Next appointment date") or v.get("next_appointment_date"))
            if nad:
                next_appointment_date = nad
            n = _norm(v.get("Job Status Notes") or v.get("job_status_notes"))
            if n:
                notes = n
            if _parse_yes_no(v.get("In Jeopardy") or v.get("in_jeopardy")):
                in_jeopardy = True
            jt = _norm(v.get("Jeopardy Type") or v.get("jeopardy_type"))
            if jt:
                jeopardy_type = jt
            jr = _norm(v.get("Jeopardy Reason") or v.get("jeopardy_reason"))
            if jr:
                jeopardy_reason = jr
            t = _norm(v.get("Time vs SMV") or v.get("time_vs_smv"))
            if t:
                time_existing = t
            sh = _parse_smv_hours(v)
            if sh is not None:
                smv_allowed = sh
            if _ready_to_handover(v):
                ready_to_handover = True

        if smv_allowed is None:
            smv_allowed = smv_from_boq.get(key)

        if completed_date and status != "completed":
            status = "completed"
        if not status:
            status = "incomplete"

        time_label = _time_vs_smv_label(spent, smv_allowed, time_existing)

        ojs_scope_complete = status == "completed" and not completed_date

        out.append(
            {
                "job_key": key,
                "smart_form_name": job["smart_form_name"],
                "corf": job["corf"],
                "address": job["address"],
                "job_status": status,
                "time_spent_hours": spent,
                "smv_allowed_hours": smv_allowed,
                "operative_breakdown": breakdown,
                "time_vs_smv": time_label,
                "visit_count": len(visits),
                "job_status_notes": notes,
                "in_jeopardy": in_jeopardy,
                "jeopardy_type": jeopardy_type,
                "jeopardy_reason": jeopardy_reason,
                "appointment_date": appointment_date,
                "next_appointment_date": next_appointment_date,
                "completed_date": completed_date,
                "ojs_scope_complete": ojs_scope_complete,
                "ready_to_handover": ready_to_handover,
            }
        )

    out.sort(key=lambda j: (j["job_status"] != "incomplete", j.get("corf") or j["smart_form_name"]))
    return out


def _esc(t: Any) -> str:
    return html.escape(_norm(t))


def _status_badge(job: Dict[str, Any]) -> str:
    label = tracker_status_for_job(job) or "—"
    if label == SECTION_OJS_AWAITING_EASYBOP:
        return f'<span class="badge ojs">{_esc(label)}</span>'
    if label == TRACKER_AWAITING_HANDOVER:
        return f'<span class="badge ok">{_esc(label)}</span>'
    if label in (TRACKER_AWAITING_NEXT_APPOINTMENT, TRACKER_MANAGER_REVIEW):
        return f'<span class="badge danger">{_esc(label)}</span>'
    return f'<span class="badge warn">{_esc(label)}</span>'


def _job_card(job: Dict[str, Any], accent: str) -> str:
    smv = job.get("smv_allowed_hours")
    smv_txt = f"{smv}h" if smv is not None else "—"
    pct = ""
    if smv and smv > 0:
        pct_val = round((job["time_spent_hours"] / smv) * 100)
        pct = f" ({pct_val}% of SMV)"

    jeopardy_badge = ""
    if job.get("in_jeopardy"):
        label = _jeopardy_type_label(job.get("jeopardy_type", ""))
        jeopardy_badge = (
            f'<span class="badge danger">{_esc(label)}</span>'
            f'<p class="jeopardy-reason">{_esc(job.get("jeopardy_reason"))}</p>'
        )

    sched_rows = ""
    if job.get("appointment_date"):
        sched_rows += f'<tr><th>Appointment date</th><td>{_esc(job.get("appointment_date"))}</td></tr>'
    if job.get("next_appointment_date"):
        sched_rows += f'<tr><th>Next appointment</th><td>{_esc(job.get("next_appointment_date"))}</td></tr>'
    if job.get("completed_date"):
        sched_rows += f'<tr><th>Completed</th><td>{_esc(job.get("completed_date"))}</td></tr>'
    elif job.get("job_status") == "completed":
        sched_rows += '<tr><th>EasyBOP completed</th><td><strong>Not set</strong></td></tr>'

    return f"""
    <div class="job-card" style="border-left-color:{accent}">
      <div class="job-head">
        <div>
          <div class="job-title">{_esc(job.get("address") or job.get("smart_form_name"))}</div>
          <div class="job-sub">Job ref: <strong>{_esc(job.get("corf") or "—")}</strong>
            · Visits: {job.get("visit_count", 0)}</div>
        </div>
        {_status_badge(job)}
      </div>
      {jeopardy_badge}
      <table class="mini">
        <tr><th>Time spent</th><td><strong>{job.get("time_spent_hours", 0)}h</strong>{_esc(pct)}</td></tr>
        <tr><th>SMV allowed</th><td>{_esc(smv_txt)}</td></tr>
        <tr><th>Time vs SMV</th><td>{_esc(job.get("time_vs_smv"))}</td></tr>
        <tr><th>Operatives</th><td>{_esc(job.get("operative_breakdown") or "—")}</td></tr>
        {sched_rows}
      </table>
      {f'<p class="notes"><strong>Notes:</strong> {_esc(job.get("job_status_notes"))}</p>' if job.get("job_status_notes") else ''}
    </div>
    """


def _jeopardy_section(
    title: str,
    lead: str,
    items: List[Dict[str, Any]],
    accent: str,
    box_class: str = "jeopardy-box",
) -> str:
    if not items:
        return ""
    cards = "".join(_job_card(j, accent) for j in items)
    return f"""
    <section class="{box_class}">
      <h2>{_esc(title)} <span class="count">({len(items)})</span></h2>
      <p class="lead">{_esc(lead)}</p>
      {cards}
    </section>
    """


def build_report_html(jobs: List[Dict[str, Any]], report_dt: Optional[datetime] = None) -> str:
    report_dt = report_dt or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        local_dt = report_dt.astimezone(ZoneInfo("Europe/London"))
    except Exception:
        local_dt = report_dt
    date_str = local_dt.strftime("%d %B %Y %H:%M") + " (UK)"

    next_appt_jeopardy = [j for j in jobs if j.get("jeopardy_type") == "next_appointment_needed"]
    manager_jeopardy = [j for j in jobs if j.get("jeopardy_type") == "manager_review"]
    other_jeopardy = [
        j for j in jobs
        if j.get("in_jeopardy") and j.get("jeopardy_type") not in ("next_appointment_needed", "manager_review")
    ]
    wip = [
        j for j in jobs
        if j.get("job_status") != "completed"
        and not j.get("in_jeopardy")
        and _norm(j.get("appointment_date"))
    ]
    completed_all = [j for j in jobs if j.get("job_status") == "completed"]
    ojs_scope_complete = [j for j in completed_all if j.get("ojs_scope_complete")]
    awaiting_handover = [
        j for j in completed_all
        if not j.get("ojs_scope_complete") and not j.get("ready_to_handover")
    ]
    ready_handover_done = [
        j for j in completed_all
        if not j.get("ojs_scope_complete") and j.get("ready_to_handover")
    ]

    def section(title: str, items: List[Dict[str, Any]], accent: str, empty: str) -> str:
        if not items:
            return f'<h2>{_esc(title)}</h2><p class="empty">{_esc(empty)}</p>'
        cards = "".join(_job_card(j, accent) for j in items)
        return f'<h2>{_esc(title)} <span class="count">({len(items)})</span></h2>{cards}'

    jeopardy_html = (
        _jeopardy_section(
            TRACKER_AWAITING_NEXT_APPOINTMENT,
            "No next appointment booked or date is in the past — cancel, complete, or rebook.",
            next_appt_jeopardy + other_jeopardy,
            "#c0392b",
        )
        + _jeopardy_section(
            TRACKER_MANAGER_REVIEW,
            "Time spent exceeds SMV allowance — manager review required.",
            manager_jeopardy,
            "#d35400",
        )
    )

    # "Total jobs" reflects only the jobs shown in the stat boxes below, so the
    # boxes always sum to the total. Jobs with no bucket (e.g. ready-to-handover
    # completed, or open with no appointment) are excluded from this count.
    shown_total = (
        len(wip)
        + len(next_appt_jeopardy)
        + len(other_jeopardy)
        + len(manager_jeopardy)
        + len(ojs_scope_complete)
        + len(awaiting_handover)
    )

    header = pdf_header_html(
        "Stage 6 — WIP Report",
        f"BCC Tracker Status summary · Generated {_esc(date_str)}",
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
  @page {{ margin: 18mm 14mm; }}
  body {{ font-family: Arial, Helvetica, sans-serif; color: #17212b; font-size: 11px; line-height: 1.45; margin: 0; }}
  {PDF_HEADER_STYLES}
  .stats {{ display: flex; gap: 12px; margin-bottom: 22px; flex-wrap: wrap; }}
  .stat {{ flex: 1; min-width: 100px; background: #f7fafb; border-radius: 8px; padding: 14px 16px;
    border: 1px solid #dce3e8; }}
  .stat .num {{ font-size: 26px; font-weight: 700; color: #145c63; }}
  .stat .lbl {{ font-size: 11px; color: #56616b; text-transform: uppercase; letter-spacing: 0.04em; }}
  .stat.danger .num {{ color: #c0392b; }}
  h2 {{ font-size: 16px; margin: 24px 0 12px; color: #145c63; border-bottom: 2px solid #145c63;
    padding-bottom: 6px; }}
  h2 .count {{ font-weight: 400; color: #56616b; font-size: 14px; }}
  .job-card {{ background: #fff; border: 1px solid #dce3e8; border-left: 4px solid #145c63;
    border-radius: 6px; padding: 14px 16px; margin-bottom: 12px; page-break-inside: avoid; }}
  .job-head {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }}
  .job-title {{ font-size: 13px; font-weight: 600; }}
  .job-sub {{ font-size: 10px; color: #56616b; margin-top: 4px; }}
  .badge {{ display: inline-block; padding: 3px 10px; border-radius: 12px; font-size: 10px;
    font-weight: 600; text-transform: uppercase; }}
  .badge.ok {{ background: #e8f5e9; color: #2e7d32; }}
  .badge.ojs {{ background: #fff8e1; color: #f57f17; }}
  .badge.warn {{ background: #fff3e0; color: #e65100; }}
  .badge.danger {{ background: #ffebee; color: #c62828; margin-top: 8px; }}
  .jeopardy-reason {{ font-size: 10px; color: #c0392b; margin: 6px 0 0; }}
  table.mini {{ width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 10px; }}
  table.mini th {{ text-align: left; color: #56616b; width: 110px; padding: 3px 8px 3px 0; font-weight: 600; }}
  table.mini td {{ padding: 3px 0; }}
  .notes {{ margin: 10px 0 0; font-size: 10px; color: #444; }}
  .jeopardy-box {{ background: #fff8f8; border: 1px solid #f5c6c6; border-radius: 8px;
    padding: 16px 18px; margin-bottom: 24px; }}
  .ojs-box {{ background: #fffdf5; border: 1px solid #ffe082; border-radius: 8px;
    padding: 16px 18px; margin-bottom: 24px; }}
  .lead {{ margin: 0 0 12px; color: #56616b; }}
  .empty {{ color: #888; font-style: italic; }}
</style></head><body>
  {header}
  <div class="stats">
    <div class="stat"><div class="num">{shown_total}</div><div class="lbl">Total jobs</div></div>
    <div class="stat"><div class="num">{len(wip)}</div><div class="lbl">{_esc(TRACKER_WIP)}</div></div>
    <div class="stat danger"><div class="num">{len(next_appt_jeopardy) + len(other_jeopardy)}</div><div class="lbl">{_esc(TRACKER_AWAITING_NEXT_APPOINTMENT)}</div></div>
    <div class="stat danger"><div class="num">{len(manager_jeopardy)}</div><div class="lbl">{_esc(TRACKER_MANAGER_REVIEW)}</div></div>
    <div class="stat"><div class="num">{len(ojs_scope_complete)}</div><div class="lbl">{_esc(SECTION_OJS_AWAITING_EASYBOP)}</div></div>
    <div class="stat"><div class="num">{len(awaiting_handover)}</div><div class="lbl">{_esc(TRACKER_AWAITING_HANDOVER)}</div></div>
  </div>
  {jeopardy_html}
  {section(TRACKER_WIP, wip, "#e65100", f"No jobs with tracker status {TRACKER_WIP}.")}
  {_jeopardy_section(SECTION_OJS_AWAITING_EASYBOP, "BOQ scope covered in operative forms — set works completed date in EasyBOP.", ojs_scope_complete, "#f57f17", "ojs-box")}
  {section(TRACKER_AWAITING_HANDOVER, awaiting_handover, "#2e7d32", f"No jobs with tracker status {TRACKER_AWAITING_HANDOVER}." + (f" ({len(ready_handover_done)} ready to handover — excluded.)" if ready_handover_done else ""))}
  {pdf_footer_html("Stage 6 WIP report automation")}
</body></html>"""


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
                margin={"top": "12mm", "right": "12mm", "bottom": "12mm", "left": "12mm"},
            )
            return pdf
        finally:
            browser.close()


def build_job_report_pdf_from_sheets(
    ojs_rows: List[Dict[str, Any]],
    boq_rows: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[bytes, Dict[str, Any]]:
    jobs = aggregate_jobs(ojs_rows, boq_rows)
    if not jobs:
        raise ValueError("No jobs found in sheet data — run Job Status Check first.")

    report_dt = datetime.now(timezone.utc)
    html_content = build_report_html(jobs, report_dt)
    pdf_bytes = html_to_pdf_bytes(html_content)

    wip = sum(
        1
        for j in jobs
        if j.get("job_status") != "completed"
        and not j.get("in_jeopardy")
        and _norm(j.get("appointment_date"))
    )
    completed = sum(1 for j in jobs if j.get("job_status") == "completed")
    ojs_scope_count = sum(1 for j in jobs if j.get("ojs_scope_complete"))
    handover_count = sum(
        1
        for j in jobs
        if j.get("job_status") == "completed"
        and not j.get("ojs_scope_complete")
        and not j.get("ready_to_handover")
    )
    ready_handover_excluded = sum(
        1
        for j in jobs
        if j.get("job_status") == "completed"
        and not j.get("ojs_scope_complete")
        and j.get("ready_to_handover")
    )
    next_appt = sum(1 for j in jobs if j.get("jeopardy_type") == "next_appointment_needed")
    manager = sum(1 for j in jobs if j.get("jeopardy_type") == "manager_review")
    jeopardy = sum(1 for j in jobs if j.get("in_jeopardy"))
    date_slug = report_dt.strftime("%Y-%m-%d")

    meta = {
        "date_slug": date_slug,
        "total_jobs": len(jobs),
        "wip_count": wip,
        "in_progress_count": wip,
        "completed_count": completed,
        "awaiting_handover_count": handover_count,
        "ready_to_handover_excluded_count": ready_handover_excluded,
        "ojs_scope_complete_count": ojs_scope_count,
        "jeopardy_count": jeopardy,
        "next_appointment_needed_count": next_appt,
        "manager_review_count": manager,
    }
    return pdf_bytes, meta
