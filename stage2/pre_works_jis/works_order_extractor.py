"""
works_order_extractor.py
========================
Parse a Works Order .msg (email) or PDF file and use GPT-4.1-mini to
extract all JIS form fields (Q1.1–Q5.2) and appointment/BOQ rows.

This mirrors the logic in jis_mapper.json → "AI Extract JIS and SOR".

Public API
----------
extract_from_msg_bytes(file_bytes, filename, *, openai_api_key, asbestos_summary="", asbestos_items="")
    Main entry point. Returns a dict with keys:
        jis_fields      dict  (Q1.1..Q5.2, QS-Task, Asbestos Status)
        appointment_row dict  (UPRN, Trade_1..Trade_5, Hours, Notes, 7.1..11.2)
        raw_text        str   (full email body used for extraction)
        metadata        dict  (subject, from, received, found_sors, etc.)
"""

from __future__ import annotations

import email
import email.policy
import html as html_lib
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("pre_works_jis.extractor")

# ── JSON schema for the AI response (same as jis_mapper.json) ────────────────
_JIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "Q1_1_start_within", "Q1_2_address", "Q1_3_job_no",
        "Q1_4_manager", "Q1_5_bcc_surveyor", "Q1_6_work_description",
        "Q2_1_asbestos_survey_required", "Q2_2_asbestos_survey_on_easybop",
        "Q3_1_scaffold_required", "Q3_2_scaffold_details",
        "Q4_1_asbestos_removal_or_shadow_vac_before_start", "Q4_2_details_asbestos_removal",
        "Q5_1_add_codes", "Q5_2_omit_codes",
        "reasoning", "reason_2_1", "reason_4_1", "reason_5",
        "appointment_trade_1", "appointment_hours_1", "appointment_notes_1",
        "appointment_trade_2", "appointment_hours_2", "appointment_notes_2",
        "appointment_trade_3", "appointment_hours_3", "appointment_notes_3",
        "appointment_trade_4", "appointment_hours_4", "appointment_notes_4",
        "appointment_trade_5", "appointment_hours_5", "appointment_notes_5",
        "appointment_7_1", "appointment_7_2",
        "appointment_8_1", "appointment_8_2",
        "appointment_9_1", "appointment_9_2",
        "appointment_10_1", "appointment_10_2",
        "appointment_11_1", "appointment_11_2",
        "uprn",
    ],
    "properties": {
        "Q1_1_start_within":    {"type": ["string", "null"]},
        "Q1_2_address":         {"type": ["string", "null"]},
        "Q1_3_job_no":          {"type": ["string", "null"]},
        "Q1_4_manager":         {"type": ["string", "null"]},
        "Q1_5_bcc_surveyor":    {"type": ["string", "null"]},
        "Q1_6_work_description":{"type": ["string", "null"]},
        "Q2_1_asbestos_survey_required":           {"type": ["string", "null"]},
        "Q2_2_asbestos_survey_on_easybop":         {"type": ["string", "null"]},
        "Q3_1_scaffold_required":                  {"type": ["string", "null"]},
        "Q3_2_scaffold_details": {
            "type": ["string", "null"],
            "enum": [
                "Terrace", "Semi-detached", "Detached", "Flat", "Bungalow",
                "Front", "Rear",
                "Left side (looking at front door)", "Right side (looking at front door)",
                None,
            ],
        },
        "Q4_1_asbestos_removal_or_shadow_vac_before_start": {"type": ["string", "null"]},
        "Q4_2_details_asbestos_removal": {"type": ["string", "null"]},
        "Q5_1_add_codes": {"type": ["array", "null"], "items": {"type": "string"}},
        "Q5_2_omit_codes": {"type": ["array", "null"], "items": {"type": "string"}},
        "reasoning":   {"type": ["string", "null"]},
        "reason_2_1":  {"type": ["string", "null"]},
        "reason_4_1":  {"type": ["string", "null"]},
        "reason_5":    {"type": ["string", "null"]},
        "uprn":        {"type": ["string", "null"]},
        # Appointment / BOQ slots
        "appointment_7_1":    {"type": ["string", "null"]},
        "appointment_7_2":    {"type": ["string", "null"]},
        "appointment_trade_1": {"type": ["string", "null"]},
        "appointment_hours_1": {"type": ["string", "null"]},
        "appointment_notes_1": {"type": ["string", "null"]},
        "appointment_8_1":    {"type": ["string", "null"]},
        "appointment_8_2":    {"type": ["string", "null"]},
        "appointment_trade_2": {"type": ["string", "null"]},
        "appointment_hours_2": {"type": ["string", "null"]},
        "appointment_notes_2": {"type": ["string", "null"]},
        "appointment_9_1":    {"type": ["string", "null"]},
        "appointment_9_2":    {"type": ["string", "null"]},
        "appointment_trade_3": {"type": ["string", "null"]},
        "appointment_hours_3": {"type": ["string", "null"]},
        "appointment_notes_3": {"type": ["string", "null"]},
        "appointment_10_1":   {"type": ["string", "null"]},
        "appointment_10_2":   {"type": ["string", "null"]},
        "appointment_trade_4": {"type": ["string", "null"]},
        "appointment_hours_4": {"type": ["string", "null"]},
        "appointment_notes_4": {"type": ["string", "null"]},
        "appointment_11_1":   {"type": ["string", "null"]},
        "appointment_11_2":   {"type": ["string", "null"]},
        "appointment_trade_5": {"type": ["string", "null"]},
        "appointment_hours_5": {"type": ["string", "null"]},
        "appointment_notes_5": {"type": ["string", "null"]},
    },
}

_SYSTEM_PROMPT = """\
You are a UK construction / repairs assistant for Jeffway JIS (Job Instruction Sheet) forms.

You receive the plain text of a BCC Works Order email (or .msg file).
Extract all fields for the Jeffway JIS form AND the appointment/BOQ slots.

Rules:
- Return ONLY valid JSON matching the schema. No markdown, no extra text.

## Fields to extract:

Q1_1_start_within: Extract ONLY the number of days from the Priority field (e.g., if '30 Days', output '30').
Q1_2_address: Full address and UPRN. Format: "WO_REF-ADDRESS - UPRN"  e.g. "99847/1-14 MILES ROAD BRISTOL - 100000"
Q1_3_job_no: Order reference / Job No (e.g. "99847/1").
Q1_4_manager: Leave null.
Q1_5_bcc_surveyor: Name of the BCC Surveyor / Owner of WO.
Q1_6_work_description: The works description / scope.
uprn: Just the numeric UPRN (e.g. "100000").

Q2_1_asbestos_survey_required: Always output "yes".
Q2_2_asbestos_survey_on_easybop:
- If asbestos survey is on file (ASBESTOS_REPORT provided): "Yes — survey on file. [summary]"
- Otherwise: "To be confirmed — asbestos report not yet linked to this order."

Q3_1_scaffold_required: "Yes" / "No" / "To be confirmed" based on scope of works.
Q3_2_scaffold_details: One of: Terrace, Semi-detached, Detached, Flat, Bungalow, Front, Rear,
  "Left side (looking at front door)", "Right side (looking at front door)", or null.

Q4_1_asbestos_removal_or_shadow_vac_before_start: "Yes" or "No" only.
Q4_2_details_asbestos_removal: Details about asbestos / shadow vac requirement.

Q5_1_add_codes: SOR codes to ADD that are missing from the order. If nothing to add: ["Not Answered"].
Q5_2_omit_codes: SOR codes to OMIT. If nothing to omit: ["Not Answered"].
reasoning: Explain Q5.1/Q5.2 decisions only.
reason_2_1: Reasoning for Q2.1.
reason_4_1: Reasoning for Q4.1.
reason_5: Reasoning for SOR add/omit decisions.

## Appointment / BOQ slots (from the SOR/BOQ table in the works order):
For each trade / line of work extract up to 5 appointment slots:
  appointment_7_1  = Date of appointment (first visit) — use the order's start date / target completion date if given
  appointment_7_2  = "yes" if there will be another appointment after slot 1, else "no"
  appointment_trade_1 = Trade for visit 1 (e.g. "Plasterer", "Electrician", "Multi-trade")
  appointment_hours_1 = Estimated hours (number, e.g. "4")
  appointment_notes_1 = Brief note about the work in this slot (SOR description or scope)
  (repeat pattern for slots 2–5 as needed based on distinct trades/visits)

If all work can be done in one visit: fill slot 1 only, set appointment_7_2 = "no".
Leave unused slots as null.
"""

_USER_PROMPT_TEMPLATE = """\
Process this BCC works order and extract all fields.

Subject: {subject}
From: {from_addr}
Received: {received}

Full works order text:
{body_text}

=====
ASBESTOS_REPORT (parsed from SharePoint PDF):
Report linked: {asbestos_linked}
Summary: {asbestos_summary}
ACM Register items:
{asbestos_items}
"""


# ── Email / MSG parsing ───────────────────────────────────────────────────────

def _extract_from_eml(raw: bytes) -> Tuple[str, str, str, str]:
    """Return (subject, from_addr, received, body_text) from raw .eml bytes."""
    try:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
    except Exception:
        msg = email.message_from_bytes(raw)

    subject   = str(msg.get("Subject", "")).strip()
    from_addr = str(msg.get("From", "")).strip()
    received  = str(msg.get("Date", "")).strip()

    body_parts: List[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body_parts.append(payload.decode("utf-8", errors="replace"))
            elif ct == "text/html" and not body_parts:
                payload = part.get_payload(decode=True)
                if payload:
                    body_parts.append(_html_to_plain(payload))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body_parts.append(payload.decode("utf-8", errors="replace"))

    body_text = _clean_body_text("\n".join(body_parts))
    return subject, from_addr, received, body_text


_OLE_MAGIC = b"\xd0\xcf\x11\xe0"


def _is_ole_msg(raw: bytes) -> bool:
    return len(raw) >= 4 and raw[:4] == _OLE_MAGIC


def _as_text(val) -> str:
    if val is None:
        return ""
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return str(val).strip()


def _strip_leading_css(text: str) -> str:
    """Remove LibreOffice / HTML-export CSS blocks that leak into plain text."""
    out = text
    for _ in range(40):
        nxt = re.sub(
            r"^\s*(?:@page|(?:p|pre|a|body|table|td|th|span|div|h\d)(?:[:\.#][a-z0-9_-]+)*)\s*\{[^}]*\}\s*",
            "",
            out,
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if nxt == out:
            break
        out = nxt
    return out


def _normalize_plain_text(text: str) -> str:
    text = html_lib.unescape(text or "")
    text = text.replace("\xa0", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


def _html_to_plain(html) -> str:
    text = _as_text(html)
    if not text:
        return ""
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", "\n", text)
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", "\n", text)
    text = re.sub(r"(?is)<head[^>]*>.*?</head>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|td|th|li|h[1-6]|table|pre|section|article)>", "\n", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = _strip_leading_css(text)
    return _normalize_plain_text(text)


def _clean_body_text(text: str) -> str:
    raw = _as_text(text)
    if not raw:
        return ""
    if "<" in raw and ">" in raw:
        plain = _html_to_plain(raw)
    else:
        plain = _normalize_plain_text(_strip_leading_css(raw))
    return plain


def _body_quality_score(text: str) -> int:
    if not text:
        return 0
    score = min(len(text), 8000)
    if re.search(r"works\s+order", text, re.IGNORECASE):
        score += 2000
    if re.search(r"\b\d{1,3}[A-Z]{2}\d{4,6}\b", text, re.IGNORECASE):
        score += 1000
    if re.search(r"\bUPRN\b", text, re.IGNORECASE):
        score += 500
    if text.lstrip().startswith("@page"):
        score -= 3000
    if re.search(r"(?m)^\s*p\.(?:western|cjk|ctl)\s*\{", text):
        score -= 1500
    return score


def _choose_best_body(*candidates: str) -> str:
    cleaned = [_clean_body_text(c) for c in candidates if c]
    cleaned = [c for c in cleaned if c]
    if not cleaned:
        return ""
    return max(cleaned, key=_body_quality_score)


def _parse_with_extract_msg(raw: bytes) -> Tuple[str, str, str, str]:
    import io

    import extract_msg  # optional dependency

    m = extract_msg.Message(io.BytesIO(raw))
    subject = _as_text(m.subject)
    from_addr = _as_text(m.sender)
    received = _as_text(m.date)
    body_text = _choose_best_body(m.body, m.htmlBody)
    return subject, from_addr, received, body_text


def _extract_from_msg_bytes(raw: bytes, filename: str) -> Tuple[str, str, str, str]:
    """
    Try extract-msg library first (proper Outlook .msg format),
    fall back to treating as raw email / plain text.
    """
    fn_lower = (filename or "").lower()
    is_outlook_msg = fn_lower.endswith(".msg") or _is_ole_msg(raw)

    if is_outlook_msg:
        try:
            return _parse_with_extract_msg(raw)
        except ImportError:
            logger.warning("extract_msg not installed — cannot parse Outlook .msg")
            if _is_ole_msg(raw):
                raise RuntimeError(
                    "extract_msg is required to parse Outlook .msg files but is not installed"
                )
        except Exception as e:
            logger.warning("extract_msg failed (%s)", e)
            if _is_ole_msg(raw):
                raise RuntimeError(f"Could not parse Outlook .msg: {e}") from e

    return _extract_from_eml(raw)


def _parse_priority_days(text: str) -> Optional[str]:
    m = re.search(r"(\d+)\s*(?:\w+\s+)?days?", text, re.IGNORECASE)
    if m:
        return m.group(1)
    m2 = re.search(r"Priority[:\s]*(\d+)", text, re.IGNORECASE)
    return m2.group(1) if m2 else None


def _format_q12(wo_ref: str, address: str, uprn: str) -> str:
    parts = [p.strip() for p in address.split(",")]
    street = parts[0] if parts else address
    locality = (parts[1] if len(parts) > 1 else "").upper()
    addr_local = (street + " " + locality).strip().upper()
    left = "-".join(filter(None, [wo_ref, addr_local]))
    return (left + (" - " + uprn if uprn else "")).strip()


# ── OpenAI call ───────────────────────────────────────────────────────────────

def _call_openai(
    user_prompt: str,
    *,
    api_key: str,
    model: str = "gpt-4.1-mini",
    temperature: float = 0.1,
) -> Dict[str, Any]:
    from openai import OpenAI  # import here so module loads even without openai installed

    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=model,
        temperature=temperature,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "jis_extraction",
                "strict": True,
                "schema": _JIS_SCHEMA,
            },
        },
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ],
    )
    raw = resp.choices[0].message.content or "{}"
    return json.loads(raw)


# ── Main public function ──────────────────────────────────────────────────────

def extract_from_msg_bytes(
    file_bytes: bytes,
    filename: str,
    *,
    openai_api_key: str,
    asbestos_summary: str = "",
    asbestos_items: str = "",
    model: str = "gpt-4.1-mini",
) -> Dict[str, Any]:
    """
    Parse the .msg / .eml file and ask GPT to extract JIS + appointment fields.

    Returns:
        {
            "jis_fields":      {...},   # Q1.1 → Q5.2 + QS-Task + Asbestos Status
            "appointment_row": {...},   # UPRN, 7.1, 7.2, Trade_1, Hours_1, Notes_1, ...
            "raw_text":        str,
            "metadata":        {"subject", "from", "received"},
        }
    """
    subject, from_addr, received, body_text = _extract_from_msg_bytes(file_bytes, filename)

    asbestos_linked = "Yes" if (asbestos_summary or asbestos_items) else "No"
    user_prompt = _USER_PROMPT_TEMPLATE.format(
        subject=subject,
        from_addr=from_addr,
        received=received,
        body_text=body_text[:12000],  # trim very long emails
        asbestos_linked=asbestos_linked,
        asbestos_summary=asbestos_summary or "(none)",
        asbestos_items=asbestos_items or "(no items parsed)",
    )

    logger.info("Calling OpenAI %s for %s (body_len=%s)", model, filename, len(body_text))
    ai = _call_openai(user_prompt, api_key=openai_api_key, model=model)

    # ── Build JIS fields dict (matches jis tab columns) ──────────────────────
    uprn     = (ai.get("uprn") or "").strip()
    q13      = (ai.get("Q1_3_job_no") or "").strip()
    q12_addr = (ai.get("Q1_2_address") or "").strip()

    add_list  = ai.get("Q5_1_add_codes") or ["Not Answered"]
    omit_list = ai.get("Q5_2_omit_codes") or ["Not Answered"]
    q51 = ", ".join(str(c) for c in add_list if c) or "Not Answered"
    q52 = ", ".join(str(c) for c in omit_list if c) or "Not Answered"

    reason5 = (ai.get("reason_5") or "").strip()
    add_text  = ", ".join(str(c) for c in add_list if c) or "none"
    omit_text = ", ".join(str(c) for c in omit_list if c) or "none"
    qs_task = (
        f"Added SORs: {add_text}. Omitted SORs: {omit_text}. "
        f"Why: {reason5 or 'Closest-match and scope-based review from AI.'}"
    )

    q32_raw = ai.get("Q3_2_scaffold_details")
    q32 = (
        q32_raw[0] if isinstance(q32_raw, list) and q32_raw else
        str(q32_raw).strip() if q32_raw else ""
    )

    jis_fields: Dict[str, Any] = {
        "Q1.1": (ai.get("Q1_1_start_within") or "").strip(),
        "Q1.2": q12_addr,
        "Q1.3": q13,
        "Q1.4": "AI",
        "Q1.5": (ai.get("Q1_5_bcc_surveyor") or "").strip(),
        "Q1.6": (ai.get("Q1_6_work_description") or "").strip(),
        "Q2.1": (ai.get("Q2_1_asbestos_survey_required") or "yes").strip(),
        "Q2.2": (ai.get("Q2_2_asbestos_survey_on_easybop") or "").strip(),
        "Q3.1": (ai.get("Q3_1_scaffold_required") or "").strip(),
        "Q3.2": q32,
        "Q4.1": (ai.get("Q4_1_asbestos_removal_or_shadow_vac_before_start") or "").strip(),
        "Q4.2": (ai.get("Q4_2_details_asbestos_removal") or "").strip(),
        "Q5.1": q51,
        "Q5.2": q52,
        "QS-Task":        qs_task,
        "Asbestos Status": "",
        "jis_uploaded":   False,
    }

    # ── Build appointment row ────────────────────────────────────────────────
    appointment_row: Dict[str, Any] = {"UPRN": uprn}
    for slot in range(1, 6):
        q_a = f"7" if slot == 1 else str(6 + slot)
        q_b = str(6 + slot)

        prefix = f"appointment_"
        s = str(slot)
        appointment_row[f"{6+slot}.1" if slot > 1 else "7.1"] = (ai.get(f"{prefix}{6+slot}_1") or ai.get(f"{prefix}7_1") if slot == 1 else ai.get(f"{prefix}{6+slot}_1") or "")
        appointment_row[f"{6+slot}.2" if slot > 1 else "7.2"] = (ai.get(f"{prefix}{6+slot}_2") or ai.get(f"{prefix}7_2") if slot == 1 else ai.get(f"{prefix}{6+slot}_2") or "")
        appointment_row[f"Trade_{s}"] = (ai.get(f"{prefix}trade_{s}") or "").strip()
        appointment_row[f"Hours_{s}"] = (ai.get(f"{prefix}hours_{s}") or "").strip()
        appointment_row[f"Notes_{s}"] = (ai.get(f"{prefix}notes_{s}") or "").strip()

    # Clean appointment row keys
    appt_clean: Dict[str, Any] = {}
    for i in range(1, 6):
        q1 = f"{6+i}.1"
        q2 = f"{6+i}.2"
        if i == 1:
            q1, q2 = "7.1", "7.2"
        appt_clean[q1] = (ai.get(f"appointment_{6+i}_1") or (ai.get("appointment_7_1") if i == 1 else "") or "").strip()
        appt_clean[q2] = (ai.get(f"appointment_{6+i}_2") or (ai.get("appointment_7_2") if i == 1 else "") or "").strip()
        appt_clean[f"Trade_{i}"] = (ai.get(f"appointment_trade_{i}") or "").strip()
        appt_clean[f"Hours_{i}"] = (ai.get(f"appointment_hours_{i}") or "").strip()
        appt_clean[f"Notes_{i}"] = (ai.get(f"appointment_notes_{i}") or "").strip()
    appt_clean["UPRN"] = uprn

    return {
        "jis_fields":      jis_fields,
        "appointment_row": appt_clean,
        "raw_text":        body_text,
        "metadata": {
            "subject":  subject,
            "from":     from_addr,
            "received": received,
            "ai_raw":   ai,
        },
    }


def parse_msg_text_only(file_bytes: bytes, filename: str) -> Dict[str, Any]:
    """Parse .msg / .eml bytes to email metadata and plain body text (no GPT)."""
    subject, from_addr, received, body_text = _extract_from_msg_bytes(file_bytes, filename)
    body_plain = _clean_body_text(body_text)
    return {
        "subject": subject,
        "from_addr": from_addr,
        "received": received,
        "body_text": body_plain[:12000],
        "body_plain_text": body_plain[:12000],
        "full_body_length": len(body_plain),
        "filename": filename,
    }
