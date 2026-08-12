"""Validate Operative Job Sheet rows — shared by daily and weekly form-issue flows."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

FIELDS = {
    "address": "Q1.1 Address",
    "date": "Q1.2 Date",
    "jobDetails": "Q1.3 Job details",
    "asbestos": (
        "Q1.4 Have you read and understood the asbestos report for the property "
        "or checked the status with a Manager or Supervisor before"
    ),
    "dailyJobs": "Q2.1 Daily Jobs Table",
    "dailyHours": "Q2.1 Daily Jobs Table Price/Hours",
    "completedInFull": "Q2.2 Were you able to complete your work in full?",
    "completedPhotos": "Q2.3 Please provide photos of completed work",
    "variations": "Q2.4 Did you complete any additonal work or variations?",
    "incompleteReason": "Q2.5 Please explain why you could not complete your job",
    "variationPhotos": "Q2.6 Please provide photos of variations",
    "variationDetails": (
        "Q2.7 Please provide some detail on the variations? Task, QTY, Location. "
        "e.g. skimmed extra 3 sqm in lounge."
    ),
    "outstandingWorks": (
        "Q2.8 Please provide details of any outstanding works you are aware of "
        "for this property"
    ),
    "notes": "Q3.1 Notes, if applicable",
    "employeeName": "Q4.1 Employee Name",
    "employeeSignature": "Q4.2 Employee Signature",
}

VALIDATE_OUTSTANDING_WORKS = True
_DATE_RE = re.compile(r"\d{2}/\d{2}/\d{4}")
_HOURS_RE = re.compile(r"^\d+([.,]\d+)?$")
_HOURS_TEXT_RE = re.compile(r"\d+\s*h(?:ours?)?", re.I)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _norm(value: Any) -> str:
    return _text(value).lower()


def _is_missing(value: Any) -> bool:
    n = _norm(value)
    return n == "" or n == "not answered"


def _is_not_answered(value: Any) -> bool:
    return _norm(value) == "not answered"


def _has_value(value: Any) -> bool:
    return not _is_missing(value)


def _extract_first_date(value: Any) -> str:
    m = _DATE_RE.search(_text(value))
    return m.group(0) if m else ""


def _extract_corf(row: Dict[str, Any]) -> str:
    direct = _text(row.get("CORF") or row.get("corf") or row.get("CORF Number"))
    if direct:
        return direct
    source = _text(
        row.get("SmartForm Name") or row.get("Job Header") or row.get("Job Summary")
    )
    m = re.search(r":\s*(\d+)\s*$", source)
    return m.group(1) if m else ""


def _is_truthy_flag(value: Any) -> bool:
    return _norm(value) in {
        "true",
        "yes",
        "1",
        "access issue",
        "access issue detected",
    }


def get_access_issue(row: Dict[str, Any]) -> bool:
    explicit = (
        row.get("Access Issue")
        or row.get("Access Issue Detected")
        or row.get("Has Access Issue")
        or row.get("access_issue")
    )
    if explicit is not None and _text(explicit) != "":
        return _is_truthy_flag(explicit)
    return "access issue" in _norm(row.get(FIELDS["notes"]))


def get_access_issue_reason(row: Dict[str, Any]) -> str:
    if row.get("access_issue_reason") is not None:
        return _text(row.get("access_issue_reason"))
    return _text(row.get("Access Issue Reason"))


def _has_valid_daily_hours(value: Any) -> bool:
    text = _text(value)
    if not text:
        return False
    if _HOURS_RE.match(text):
        return True
    return bool(_HOURS_TEXT_RE.search(text))


def validate_incorrect_form_issues(row: Dict[str, Any]) -> List[str]:
    issues: List[str] = []

    required = [
        (FIELDS["address"], "Address missing"),
        (FIELDS["date"], "Form date missing"),
        (FIELDS["jobDetails"], "Job details missing"),
        (FIELDS["asbestos"], "Asbestos confirmation missing"),
        (FIELDS["dailyJobs"], "Daily Jobs description missing"),
        (FIELDS["completedInFull"], "Work completed in full answer missing"),
        (FIELDS["variations"], "Additional work/variation answer missing"),
        (FIELDS["employeeName"], "Employee name missing"),
    ]
    for field, message in required:
        if _is_missing(row.get(field)):
            issues.append(message)

    if _has_value(row.get(FIELDS["dailyJobs"])) and not _has_valid_daily_hours(
        row.get(FIELDS["dailyHours"])
    ):
        issues.append("Daily Jobs hours missing")

    completed = _norm(row.get(FIELDS["completedInFull"]))
    variation = _norm(row.get(FIELDS["variations"]))

    if completed == "no" and _is_missing(row.get(FIELDS["incompleteReason"])):
        issues.append("Reason job was not completed is missing")

    if (
        VALIDATE_OUTSTANDING_WORKS
        and completed == "no"
        and _is_missing(row.get(FIELDS["outstandingWorks"]))
    ):
        issues.append("Outstanding works details are missing")

    if variation == "yes" and _is_missing(row.get(FIELDS["variationDetails"])):
        issues.append("Variation details are missing")

    return issues


def validate_missing_photo_issues(row: Dict[str, Any]) -> List[str]:
    issues: List[str] = []
    daily_jobs = _has_value(row.get(FIELDS["dailyJobs"]))
    variation = _norm(row.get(FIELDS["variations"]))

    if daily_jobs and _is_not_answered(row.get(FIELDS["completedPhotos"])):
        issues.append("Completed-work photos missing")

    if variation == "yes" and _is_not_answered(row.get(FIELDS["variationPhotos"])):
        issues.append("Variation photos missing")

    return issues


def validate_ojs_row(row: Dict[str, Any]) -> Dict[str, Any]:
    incorrect = validate_incorrect_form_issues(row)
    missing_photos = validate_missing_photo_issues(row)
    return {
        "incorrect_issues": incorrect,
        "missing_photo_issues": missing_photos,
        "access_issue": get_access_issue(row),
        "access_issue_reason": get_access_issue_reason(row),
        "corf": _extract_corf(row),
        "smart_form_x_id": _text(row.get("smart_form_x_id")),
        "smart_form_name": _text(
            row.get("SmartForm Name") or row.get("Job Header") or row.get("Job Summary")
        ),
        "form_date": _extract_first_date(row.get(FIELDS["date"]))
        or _extract_first_date(row.get("Date Created")),
        "operative": _text(row.get(FIELDS["employeeName"]))
        or _text(row.get("Created By"))
        or _text(row.get("Operative")),
    }


def issue_still_open(
    issue_type: str,
    issue_detail: str,
    validation: Dict[str, Any],
) -> bool:
    detail = _text(issue_detail)
    itype = _text(issue_type)
    if itype == "Incorrect form":
        issues = validation.get("incorrect_issues", [])
        if not detail or ";" in detail:
            return bool(issues)
        return detail in issues
    if itype == "Missing photos":
        issues = validation.get("missing_photo_issues", [])
        if not detail or ";" in detail:
            return bool(issues)
        return detail in issues
    if itype.lower().startswith("access"):
        return bool(validation.get("access_issue"))
    return True


def issue_text_still_open(
    issue_text: str,
    issue_type: str,
    row: Dict[str, Any],
) -> bool:
    """Return True when a single tracked issue message is still present on the form."""
    text = _text(issue_text)
    if not text:
        return False

    validation = validate_ojs_row(row)
    if issue_still_open(issue_type, text, validation):
        return True

    # Hours cannot be marked resolved while the jobs table is still empty — the
    # validator skips the hours check when Q2.1 has no content.
    if text == "Daily Jobs hours missing":
        if _is_missing(row.get(FIELDS["dailyJobs"])):
            return True
        if _has_value(row.get(FIELDS["dailyJobs"])) and not _has_valid_daily_hours(
            row.get(FIELDS["dailyHours"])
        ):
            return True
        return False

    return False


def tracked_issue_messages(case: Dict[str, Any]) -> List[str]:
    """Union of open + resolved issue messages already tracked for this case."""
    parts: List[str] = []
    for key in (
        "issue_summary",
        "Issue Summary",
        "issue_resolved",
        "Issue Resolved",
        "issue_detail",
        "Issue Detail",
    ):
        raw = _text(case.get(key))
        if not raw:
            continue
        for part in raw.split(";"):
            cleaned = part.strip()
            if cleaned and cleaned not in parts:
                parts.append(cleaned)
    return parts


def index_ojs_rows(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    by_corfx: Dict[str, Dict[str, Any]] = {}

    for row in rows:
        if not isinstance(row, dict):
            continue
        xid = _text(row.get("smart_form_x_id"))
        if xid:
            by_id[xid] = row
        corf = _extract_corf(row)
        date_created = _extract_first_date(row.get("Date Created"))
        if corf:
            by_corfx[f"{corf}|{date_created}"] = row
            if corf not in by_corfx:
                by_corfx[corf] = row

    return {"by_id": by_id, "by_corfx": by_corfx}


def find_ojs_row(
    issue: Dict[str, Any],
    index: Dict[str, Dict[str, Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    xid = _text(issue.get("smart_form_x_id") or issue.get("SmartForm X ID"))
    if xid and xid in index["by_id"]:
        return index["by_id"][xid]

    corf = _text(issue.get("corf") or issue.get("CORF"))
    date_created = _extract_first_date(
        issue.get("date_created") or issue.get("Date Created")
    )
    if corf and f"{corf}|{date_created}" in index["by_corfx"]:
        return index["by_corfx"][f"{corf}|{date_created}"]
    if corf and corf in index["by_corfx"]:
        return index["by_corfx"][corf]
    return None
