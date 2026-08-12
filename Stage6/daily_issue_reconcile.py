"""Reconcile open Form Issue Cases against fresh EasyBOP OJS extracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from form_issue_validation import (
    FIELDS,
    _extract_first_date,
    _text,
    find_ojs_row,
    index_ojs_rows,
    issue_text_still_open,
    tracked_issue_messages,
)

try:
    _UK = ZoneInfo("Europe/London")
except Exception:
    _UK = ZoneInfo("UTC")

Q21_PRICE_COL = "Q2.1 Daily Jobs Table Price/Hours"


def _norm_status(value: Any) -> str:
    return _text(value).lower()


def _issue_key(case: Dict[str, Any]) -> str:
    return _text(case.get("issue_key") or case.get("Issue Key"))


def _uk_today() -> str:
    return datetime.now(_UK).strftime("%d/%m/%Y")


def _corf_from_address(address: Any) -> str:
    import re

    m = re.search(r"-(\d+/\d+)\s*$", _text(address))
    return m.group(1) if m else ""


def _photo_value(parsed: Dict[str, Any], key: str) -> str:
    photos = parsed.get("photos") or {}
    items = photos.get(key) or []
    return "Yes" if isinstance(items, list) and len(items) > 0 else "Not Answered"


def _merge_labeled_fields(target: Dict[str, Any], parsed: Dict[str, Any]) -> None:
    labeled = parsed.get("labeled_fields") or {}
    for col, val in labeled.items():
        if val is None:
            continue
        if isinstance(val, (list, dict)):
            text = str(val).strip()
            if text and text not in {"[]", "{}"}:
                target[col] = text
            continue
        text = str(val).strip()
        if col == Q21_PRICE_COL or text:
            target[col] = text


def ojs_rows_from_extract_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        grid = item.get("grid") or {}
        cells = grid.get("cells") or {}
        parsed = item.get("parsed") or {}
        row: Dict[str, Any] = {
            "smart_form_x_id": _text(item.get("smart_form_x_id")),
            "SmartForm Name": _text(
                grid.get("form_name")
                or cells.get("list_form_name")
                or parsed.get("job_header")
            ),
            "Date Created": _text(grid.get("date_created") or cells.get("list_date_created")),
            "Created By": _text(grid.get("created_by") or cells.get("list_created_by")),
        }
        _merge_labeled_fields(row, parsed)
        row[FIELDS["completedPhotos"]] = _photo_value(parsed, "completed_work")
        row[FIELDS["variationPhotos"]] = _photo_value(parsed, "variations")
        row["CORF"] = _corf_from_address(row.get(FIELDS["address"])) or _text(parsed.get("corf"))
        rows.append(row)
    return rows


def reconcile_open_issue_cases(
    issue_cases: List[Dict[str, Any]],
    extract_results: List[Dict[str, Any]],
    *,
    report_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Compare open issue cases to fresh EasyBOP rows and build sheet updates."""
    report_date = report_date or _uk_today()
    ojs_rows = ojs_rows_from_extract_results(extract_results)
    ojs_index = index_ojs_rows(ojs_rows)

    updates: List[Dict[str, Any]] = []
    diagnostics = {
        "open_cases": 0,
        "matched": 0,
        "unmatched": 0,
        "resolved_fully": 0,
        "partially_resolved": 0,
        "refetched": len(extract_results),
        "unchanged": 0,
    }

    for case in issue_cases:
        if not isinstance(case, dict):
            continue
        if _norm_status(case.get("status") or case.get("Status")) != "open":
            continue

        issue_key = _issue_key(case)
        if not issue_key:
            continue
        diagnostics["open_cases"] += 1

        smart_form_x_id = _text(case.get("smart_form_x_id") or case.get("SmartForm X ID"))
        ojs_row = find_ojs_row(case, ojs_index) if smart_form_x_id else None
        if smart_form_x_id and (
            not ojs_row or _text(ojs_row.get("smart_form_x_id")) != smart_form_x_id
        ):
            diagnostics["unmatched"] += 1
            continue
        if not ojs_row:
            diagnostics["unmatched"] += 1
            continue
        diagnostics["matched"] += 1

        issue_type = _text(case.get("issue_type") or case.get("Issue Type"))
        tracked = tracked_issue_messages(case)
        if not tracked:
            continue

        still_open = [
            msg
            for msg in tracked
            if issue_text_still_open(msg, issue_type, ojs_row)
        ]
        resolved = [msg for msg in tracked if msg not in still_open]

        new_summary = "; ".join(still_open)
        new_resolved = "; ".join(resolved)
        old_summary = _text(case.get("issue_summary") or case.get("Issue Summary"))
        old_resolved = _text(case.get("issue_resolved") or case.get("Issue Resolved"))
        all_resolved = len(still_open) == 0

        if new_summary == old_summary and new_resolved == old_resolved:
            diagnostics["unchanged"] += 1
            continue

        if all_resolved:
            diagnostics["resolved_fully"] += 1
        else:
            diagnostics["partially_resolved"] += 1

        updates.append(
            {
                "issue_key": issue_key,
                "status": "Resolved" if all_resolved else "Open",
                "issue_summary": new_summary,
                "issue_resolved": new_resolved,
                "resolved_date": (
                    report_date
                    if all_resolved
                    else _text(case.get("resolved_date") or case.get("Resolved Date"))
                ),
                "last_seen_date": report_date,
            }
        )

    return {
        "sheet_updates": updates,
        "diagnostics": diagnostics,
    }
