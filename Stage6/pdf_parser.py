"""
Parse Operative job sheet PDF text (from pdfplumber) into structured Q fields.
"""

from __future__ import annotations

import base64
import hashlib
import io
import re
from typing import Any, Dict, List, Optional, Tuple

import pdfplumber

_FOOTER_RE = re.compile(
    r"JeffWay Construction Ltd.*?VAT Registration No\.\s*\d+",
    re.DOTALL | re.IGNORECASE,
)
_CHECKMARK_RE = re.compile(r"[\uf00c\u2713\u2611✓]")
_SECTION_RE = re.compile(r"^\d+\.\s+[A-Za-z].*$", re.MULTILINE)
_Q_MARKER_RE = re.compile(r"^Q(\d+)\.(\d+)\.\s*(.*)$", re.MULTILINE)
_SCALAR_LABELS = frozenset(
    {
        "address",
        "date",
        "job details",
        "employee name",
        "employee signature",
        "notes, if applicable",
    }
)
_CORF_RE = re.compile(r":\s*(\d{6,})\s*$")
_CORF_FROM_Q11_RE = re.compile(r"-(\d+/\d+)\s*$")


def extract_corf_from_address(address: Optional[str]) -> Optional[str]:
    """
  Extract Client Order Reference from Q1.1 Address tail, e.g.
  '... BS10 7QL-63699/2' -> '63699/2'.
  """
    s = str(address or "").strip()
    if not s:
        return None
    m = _CORF_FROM_Q11_RE.search(s)
    return m.group(1) if m else None


def _clean_pdf_text(text: str) -> str:
    cleaned = _FOOTER_RE.sub("", text or "")
    cleaned = _CHECKMARK_RE.sub("", cleaned)
    cleaned = re.sub(r"\r\n?", "\n", cleaned)
    if "Pictures Appendix" in cleaned:
        cleaned = cleaned.split("Pictures Appendix", 1)[0]
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _split_q_blocks(text: str) -> List[Dict[str, str]]:
    lines = text.split("\n")
    blocks: List[Dict[str, str]] = []
    current: Optional[Dict[str, str]] = None

    for line in lines:
        m = _Q_MARKER_RE.match(line)
        if m:
            if current:
                blocks.append(current)
            current = {
                "section": m.group(1),
                "number": m.group(2),
                "label": m.group(3).strip(),
                "body": "",
            }
            continue

        if current is None:
            continue

        if _SECTION_RE.match(line.strip()):
            continue

        if current["body"]:
            current["body"] += "\n"
        current["body"] += line

    if current:
        blocks.append(current)
    return blocks


def _split_label_and_answer(label: str, body: str) -> tuple[str, str]:
    label = label.strip()
    body = body.strip()
    if not body:
        return label, ""

    label_lower = label.lower()
    if label_lower in _SCALAR_LABELS:
        return label, body

    if "daily jobs table" in label_lower:
        return label, body

    label_lines = [label] if label else []
    body_lines = [ln.strip() for ln in body.split("\n") if ln.strip()]

    while body_lines:
        if re.match(r"^(Yes|No|Not Answered|No Signature)\b", body_lines[0], re.I):
            break
        if re.match(r"^Description\b", body_lines[0], re.I):
            break

        current = "\n".join(label_lines).strip()
        next_line = body_lines[0]
        if current.rstrip().endswith("?"):
            break
        if next_line[:1].islower():
            label_lines.append(body_lines.pop(0))
            continue
        break

    return "\n".join(label_lines).strip(), "\n".join(body_lines).strip()


_FRACTION_HOURS_RE = re.compile(
    r"^(?:(?P<whole>\d+)\s+)?(?P<num>\d+)\s*/\s*(?P<den>\d+)\s*(?P<hours>hours?)?\s*$",
    re.I,
)
_DECIMAL_HOURS_RE = re.compile(r"^(?P<val>\d+(?:\.\d+)?)\s*(?P<hours>hours?)?\s*$", re.I)
_TRAILING_FRACTION_HOURS_RE = re.compile(
    r"^(?P<desc>.+?)\s+(?P<whole>\d+)\s+(?P<num>\d+)\s*/\s*(?P<den>\d+)\s*(?:hours?)?\s*$",
    re.I | re.S,
)
_TRAILING_DECIMAL_HOURS_RE = re.compile(
    r"^(?P<desc>.+?)\s+(?P<val>\d+(?:\.\d+)?)\s*(?:hours?)?\s*$",
    re.I | re.S,
)

_PRICE_LINE_RE = _DECIMAL_HOURS_RE
_PRICE_PREFIX_RE = re.compile(
    r"^(\d+(?:\.\d+)?(?:\s*hours?)?|\d+\s+\d+\s*/\s*\d+\s*(?:hours?)?)(?:\s{2,}|\t+)(.+)$",
    re.I,
)
_PRICE_SUFFIX_RE = re.compile(
    r"^(.*)(?:\s{2,}|\t+)(\d+(?:\.\d+)?(?:\s*hours?)?|\d+\s+\d+\s*/\s*\d+\s*(?:hours?)?)\s*$",
    re.I,
)

_WORD_ONES = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_WORD_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}


def _word_group_to_number(text: str) -> Optional[float]:
    """Parse a small cardinal number from digits or words (e.g. forty five -> 45)."""
    cleaned = re.sub(r"\s+", " ", str(text or "").strip().lower()).replace("-", " ")
    if not cleaned:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", cleaned):
        return float(cleaned)
    cleaned = re.sub(r"^(?:a|an)\s+", "", cleaned)
    if cleaned in _WORD_ONES:
        return float(_WORD_ONES[cleaned])
    if cleaned in _WORD_TENS:
        return float(_WORD_TENS[cleaned])
    for tens_word, tens_val in _WORD_TENS.items():
        prefix = f"{tens_word} "
        if cleaned.startswith(prefix):
            rest = cleaned[len(prefix) :].strip()
            if rest in _WORD_ONES:
                return float(tens_val + _WORD_ONES[rest])
    return None


def _parse_prose_hours(text: str) -> Optional[str]:
    """
    Parse spoken durations such as 'one hour forty five minutes' or
    'two and a half hours' into decimal hours.
    """
    raw = re.sub(r"\s+", " ", str(text or "").strip().lower()).rstrip(".,;")
    if not raw:
        return None

    m = re.fullmatch(r"(?P<n>.+?)\s+and\s+a\s+half\s+hours?", raw, flags=re.I)
    if m:
        base = _word_group_to_number(m.group("n"))
        if base is not None:
            return _format_hours_decimal(base + 0.5)

    if re.fullmatch(r"half\s+an?\s+hour", raw, flags=re.I):
        return _format_hours_decimal(0.5)

    m = re.fullmatch(
        r"(?P<hours>.+?)\s+hours?\s+(?P<mins>.+?)\s+minutes?",
        raw,
        flags=re.I,
    )
    if m:
        hours = _word_group_to_number(m.group("hours"))
        mins = _word_group_to_number(m.group("mins"))
        if hours is not None and mins is not None:
            return _format_hours_decimal(hours + mins / 60.0)

    m = re.fullmatch(r"(?P<hours>.+?)\s+hours?", raw, flags=re.I)
    if m:
        hours = _word_group_to_number(m.group("hours"))
        if hours is not None:
            return _format_hours_decimal(hours)

    m = re.fullmatch(r"(?P<mins>.+?)\s+minutes?", raw, flags=re.I)
    if m:
        mins = _word_group_to_number(m.group("mins"))
        if mins is not None:
            return _format_hours_decimal(mins / 60.0)

    return None


def _peel_trailing_prose_hours(text: str) -> Tuple[str, str]:
    """Try to split trailing spoken duration from the end of a description line."""
    raw = str(text or "").strip()
    if not raw:
        return "", ""
    words = raw.split()
    for size in range(min(8, len(words)), 0, -1):
        tail = " ".join(words[-size:])
        price = _parse_prose_hours(tail)
        if price:
            desc = " ".join(words[:-size]).strip().rstrip(".,;-")
            if desc:
                return desc, price
    return raw, ""


def _format_hours_decimal(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _format_price_hours(num: str, hours_word: str = "") -> str:
    """Legacy helper — prefer parse_hours_value for new parsing."""
    parsed = parse_hours_value(str(num or "").strip())
    if parsed:
        return parsed
    num = str(num or "").strip()
    hours_word = str(hours_word or "").strip()
    if not num:
        return ""
    return f"{num} {hours_word}".strip() if hours_word else num


def parse_hours_value(text: str) -> Optional[str]:
    """
    Parse a Price/Hours cell value into a sheet-friendly decimal string.
    Supports: 4, 4.5, 4 hours, 4 1/2, 4 1/2 hours, and spoken durations
    (e.g. 'one hour forty five minutes', 'two and a half hours').
    """
    raw = re.sub(r"\s+", " ", str(text or "").strip())
    if not raw:
        return None

    m = _FRACTION_HOURS_RE.match(raw)
    if m:
        whole = int(m.group("whole") or 0)
        num = int(m.group("num"))
        den = int(m.group("den"))
        if den == 0:
            return None
        return _format_hours_decimal(whole + num / den)

    m = _DECIMAL_HOURS_RE.match(raw)
    if m:
        try:
            return _format_hours_decimal(float(m.group("val")))
        except ValueError:
            return None

    return _parse_prose_hours(raw)


def _split_trailing_hours(text: str) -> Tuple[str, str]:
    """Peel trailing hours (incl. fractions) off a description line."""
    raw = str(text or "").strip()
    if not raw:
        return "", ""

    whole = parse_hours_value(raw)
    if whole:
        return "", whole

    m = _TRAILING_FRACTION_HOURS_RE.match(raw)
    if m:
        den = int(m.group("den"))
        if den:
            val = _format_hours_decimal(int(m.group("whole")) + int(m.group("num")) / den)
            return m.group("desc").strip(), val

    m = _TRAILING_DECIMAL_HOURS_RE.match(raw)
    if m:
        desc = m.group("desc").strip()
        val = parse_hours_value(m.group("val"))
        if val and not re.search(r"\bBCC\s*$", desc, re.I):
            try:
                if float(val) > 24 and "hour" not in raw.lower():
                    return raw, ""
            except ValueError:
                pass
            return desc, val

    split_desc, split_price = _peel_trailing_prose_hours(raw)
    if split_price:
        return split_desc, split_price

    return raw, ""


def _parse_price_line(line: str) -> Optional[str]:
    """Return Price/Hours value when the whole line is only hours."""
    return parse_hours_value(str(line or "").strip())


def _parse_price_token(token: str) -> str:
    token = str(token or "").strip()
    parsed = parse_hours_value(token)
    if parsed:
        return parsed
    return token


def _strip_daily_jobs_header(lines: List[str]) -> List[str]:
    if not lines:
        return lines
    if not re.match(r"^Description\b", lines[0], re.I):
        return lines
    remainder = re.sub(r"^Description\s*", "", lines[0], flags=re.I)
    remainder = re.sub(r"Price/?Hours\s*", "", remainder, flags=re.I).strip()
    return ([remainder] if remainder else []) + lines[1:]


def _group_words_into_lines(
    words: List[Dict[str, Any]],
    *,
    y_tol: float = 3.0,
) -> List[List[Dict[str, Any]]]:
    if not words:
        return []
    ordered = sorted(words, key=lambda w: (float(w.get("top", 0)), float(w.get("x0", 0))))
    lines: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    current_top: Optional[float] = None

    for word in ordered:
        top = float(word.get("top", 0))
        if current_top is None or abs(top - current_top) <= y_tol:
            current.append(word)
            if current_top is None:
                current_top = top
        else:
            lines.append(current)
            current = [word]
            current_top = top
    if current:
        lines.append(current)
    return lines


def _line_text(words: List[Dict[str, Any]]) -> str:
    return " ".join(
        str(w.get("text") or "").strip()
        for w in sorted(words, key=lambda w: float(w.get("x0", 0)))
        if str(w.get("text") or "").strip()
    ).strip()


def _collect_q21_words(pdf_bytes: bytes) -> List[Dict[str, Any]]:
    """Words between the Q2.1 marker and Q2.2 on the first page that contains Q2.1."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            words = page.extract_words() or []
            if not any("Q2.1" in str(w.get("text") or "") for w in words):
                continue
            q21_words: List[Dict[str, Any]] = []
            in_block = False
            for word in sorted(words, key=lambda w: (float(w.get("top", 0)), float(w.get("x0", 0)))):
                text = str(word.get("text") or "")
                if "Q2.1" in text:
                    in_block = True
                    continue
                if in_block and "Q2.2" in text:
                    break
                if in_block:
                    q21_words.append(word)
            if q21_words:
                return q21_words
    return []


def _is_valid_price_hours(price_text: str, desc_on_same_line: str = "") -> bool:
    price_text = str(price_text or "").strip()
    if not parse_hours_value(price_text):
        return False
    if re.search(r"\bBCC\b", desc_on_same_line, re.I) and re.fullmatch(r"\d+(?:\.\d+)?", price_text):
        return False
    return True


def extract_q21_table_from_pdf(pdf_bytes: bytes) -> Optional[Dict[str, str]]:
    """
    Read Q2.1 Description and Price/Hours from PDF word positions.

    EasyBOP PDFs render a 2-column table, but plain text extraction merges the
    columns. Price/Hours words sit far to the right (x0 ~ 298+) while
    description words stay on the left.
    """
    words = _collect_q21_words(pdf_bytes)
    if not words:
        return None

    desc_header_x: Optional[float] = None
    price_header_x: Optional[float] = None
    header_bottom = 0.0
    for word in words:
        text = str(word.get("text") or "")
        x0 = float(word.get("x0", 0))
        top = float(word.get("top", 0))
        if text == "Description":
            desc_header_x = x0
            header_bottom = max(header_bottom, top)
        elif text in {"Price/Hours", "Price", "Hours"} or "Price/Hours" in text:
            price_header_x = x0
            header_bottom = max(header_bottom, top)

    if price_header_x is None:
        return None

    if desc_header_x is None:
        desc_header_x = 26.0
    # Price/Hours data sits ~x0 298; description can wrap to ~x0 283 on long lines.
    split_x = max((desc_header_x + price_header_x) / 2.0, price_header_x - 115.0)

    body_words = [
        w for w in words
        if float(w.get("top", 0)) > header_bottom + 1.5
        and str(w.get("text") or "") not in {"Description", "Price/Hours", "Price", "Hours"}
    ]
    if not body_words:
        return {"description": "", "price_hours": ""}

    desc_lines: List[str] = []
    price_hours = ""

    for line_words in _group_words_into_lines(body_words):
        desc_words = [w for w in line_words if float(w.get("x0", 0)) < split_x]
        price_words = [w for w in line_words if float(w.get("x0", 0)) >= split_x]
        desc_text = _line_text(desc_words)
        price_text = parse_hours_value(_line_text(price_words)) or ""

        if price_text and _is_valid_price_hours(price_text, desc_text) and not price_hours:
            price_hours = price_text
            if desc_text:
                desc_lines.append(desc_text)
            continue

        merged = _line_text(line_words)
        if merged:
            split_desc, split_price = _split_trailing_hours(merged)
            if split_price and not price_hours:
                price_hours = split_price
            if split_desc:
                desc_lines.append(split_desc)
            elif not split_price:
                desc_lines.append(merged)

    return {
        "description": "\n".join(desc_lines).strip(),
        "price_hours": price_hours.strip(),
    }


def _merge_q21_tables(primary: Dict[str, str], fallback: Dict[str, str]) -> Dict[str, str]:
    desc = str(primary.get("description") or "").strip()
    price = str(primary.get("price_hours") or "").strip()
    if not desc:
        desc = str(fallback.get("description") or "").strip()
    if not price:
        price = str(fallback.get("price_hours") or "").strip()

    if not price and desc:
        lines = [ln.strip() for ln in desc.split("\n") if ln.strip()]
        for idx in (0, -1):
            if not lines:
                break
            split_desc, split_price = _split_trailing_hours(lines[idx])
            if split_price:
                price = split_price
                if split_desc:
                    lines[idx] = split_desc
                else:
                    lines.pop(idx)
                desc = "\n".join(lines).strip()
                break

    return {"description": desc, "price_hours": price}


def _split_price_from_first_line(line: str) -> Tuple[str, str]:
    """
    Fallback when PDF positions are unavailable: peel a trailing hours value off
    the first line only (e.g. 'Gloss both sides of front door 2').
    """
    raw = str(line or "").strip()
    if not raw:
        return "", ""

    whole = _parse_price_line(raw)
    if whole:
        return "", whole

    split_desc, split_price = _split_trailing_hours(raw)
    if split_price:
        return split_desc, split_price

    m = re.match(
        r"^(?P<desc>.+?)\s+(?P<price>\d+(?:\.\d+)?)(?:\s*(?P<hours>hours?))?\s*$",
        raw,
        re.I,
    )
    if not m:
        return raw, ""

    desc = m.group("desc").strip()
    price = _format_price_hours(m.group("price"), m.group("hours") or "")

    if re.search(r"\bBCC\s*$", desc, re.I):
        return raw, ""
    if re.search(r"\b(?:bedroom|room)\s*$", desc, re.I) and m.group("price").isdigit():
        if int(m.group("price")) <= 9:
            return raw, ""

    try:
        if float(m.group("price")) > 24 and not m.group("hours"):
            return raw, ""
    except ValueError:
        return raw, ""

    return desc, price


def _strip_orphan_table_headers(lines: List[str]) -> List[str]:
    out: List[str] = []
    for line in lines:
        if re.match(r"^Description\s*$", line, re.I):
            continue
        if re.match(r"^Price/?Hours\s*$", line, re.I):
            continue
        out.append(line)
    return out


def _parse_daily_jobs_table(answer: str) -> Dict[str, str]:
    """
    Parse Q2.1 Daily Jobs Table into description text and a single Price/Hours value.

    PDF text layout varies:
    - Price/Hours on its own line before or after the description block
    - Price/Hours in a separate column (gap / tab) on the first description line
  - Trailing standalone number (e.g. 5) after all description lines
    """
    lines = [ln.strip() for ln in answer.split("\n") if ln.strip()]
    if not lines:
        return {"description": "", "price_hours": ""}

    lines = _strip_daily_jobs_header(lines)
    lines = _strip_orphan_table_headers(lines)

    price_hours = ""

    if lines:
        first_desc, first_price = _split_price_from_first_line(lines[0])
        if first_price:
            price_hours = first_price
            lines = ([first_desc] if first_desc else []) + lines[1:]

    # Leading Price/Hours line (common when PDF column order is price then description)
    while lines and (leading := _parse_price_line(lines[0])):
        price_hours = leading
        lines.pop(0)

    # Trailing Price/Hours line
    while lines and (trailing := _parse_price_line(lines[-1])):
        price_hours = trailing or price_hours
        lines.pop()

    desc_parts: List[str] = []
    for line in lines:
        prefix = _PRICE_PREFIX_RE.match(line)
        if prefix:
            if not price_hours:
                price_hours = _parse_price_token(prefix.group(1))
            rest = prefix.group(2).strip()
            if rest:
                desc_parts.append(rest)
            continue

        suffix = _PRICE_SUFFIX_RE.match(line)
        if suffix and suffix.group(1).strip():
            desc_parts.append(suffix.group(1).strip())
            if not price_hours:
                price_hours = _parse_price_token(suffix.group(2))
            continue

        desc_parts.append(line)

    return {
        "description": "\n".join(desc_parts).strip(),
        "price_hours": price_hours.strip(),
    }


def _sheet_column_name(key: str, label: str) -> str:
    short = re.sub(r"\s+", " ", (label or "").split("\n")[0].strip())
    if not short:
        return key
    return f"{key} {short}"


def _apply_photo_yes_no_fields(
    questions: Dict[str, Dict[str, Any]],
    labeled_fields: Dict[str, Any],
    photos: Dict[str, List[Dict[str, str]]],
) -> None:
    """Photo questions are image attachments — set Yes/No from appendix extraction."""
    completed = photos.get("completed_work") or []
    variations = photos.get("variations") or []
    q23_label = str((questions.get("Q2.3") or {}).get("label") or "Please provide photos of completed work")
    q26_label = str((questions.get("Q2.6") or {}).get("label") or "Please provide photos of variations")
    labeled_fields[_sheet_column_name("Q2.3", q23_label)] = "Yes" if completed else "No"
    labeled_fields[_sheet_column_name("Q2.6", q26_label)] = "Yes" if variations else "No"


def _build_labeled_fields(
    questions: Dict[str, Dict[str, Any]],
    flat: Dict[str, Any],
) -> Dict[str, Any]:
    labeled: Dict[str, Any] = {}
    for key, entry in questions.items():
        col = _sheet_column_name(key, str(entry.get("label") or ""))
        if key == "Q2.1":
            table = flat.get("Q2.1_table") or {}
            desc = str(table.get("description") or "").strip()
            price = str(table.get("price_hours") or "").strip()
            labeled[col] = desc
            labeled[f"{col} Price/Hours"] = price
            continue
        value = flat.get(key)
        if value is not None and str(value).strip() != "":
            labeled[col] = value
    return labeled


def _is_footer_banner_image(img: Dict[str, Any]) -> bool:
    return img.get("height", 0) < 120 and img.get("width", 0) > 400


def _image_mime_and_ext(data: bytes) -> Tuple[str, str]:
    if data[:2] == b"\xff\xd8":
        return "image/jpeg", "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "png"
    return "image/jpeg", "jpg"


def _appendix_photo_section(page_text: str, q24_variation: bool) -> Optional[str]:
    text = page_text or ""
    if "Q2.6" in text:
        return "variations"
    if "Q2.3" in text:
        return "completed_work"
    if q24_variation and "Q2.4" in text and "variation" in text.lower():
        return "variations"
    return None


def extract_ojs_photos_from_pdf(
    pdf_bytes: bytes,
    *,
    q24_variation: bool = False,
    file_prefix: str = "",
) -> Dict[str, List[Dict[str, str]]]:
    """
    Extract completed-work and variation photos from the Pictures Appendix.

    Photos are grouped using appendix section markers (Q2.3 / Q2.6 / Q2.4).
    """
    photos: Dict[str, List[Dict[str, str]]] = {
        "completed_work": [],
        "variations": [],
    }
    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
        return photos

    prefix = re.sub(r"[^\w.-]+", "_", str(file_prefix or "").strip()).strip("_") or "photo"

    position_seen: set[Tuple[int, int, int, int, int]] = set()
    content_seen: set[str] = set()
    in_appendix = False
    section: Optional[str] = None

    try:
        doc_ctx = pdfplumber.open(io.BytesIO(pdf_bytes))
    except Exception:
        return photos

    with doc_ctx as doc:
        for page_index, page in enumerate(doc.pages):
            page_text = page.extract_text() or ""
            if "Pictures Appendix" in page_text:
                in_appendix = True

            page_section = _appendix_photo_section(page_text, q24_variation)
            if page_section:
                section = page_section

            if not in_appendix or section not in photos:
                continue

            for img in page.images:
                if _is_footer_banner_image(img):
                    continue

                pos_key = (
                    page_index,
                    round(img.get("x0", 0)),
                    round(img.get("y0", 0)),
                    round(img.get("width", 0)),
                    round(img.get("height", 0)),
                )
                if pos_key in position_seen:
                    continue
                position_seen.add(pos_key)

                stream = img.get("stream")
                if stream is None:
                    continue
                try:
                    data = stream.get_data()
                except Exception:
                    continue
                if not data or len(data) < 256:
                    continue

                digest = hashlib.md5(data).hexdigest()
                if digest in content_seen:
                    continue
                content_seen.add(digest)

                mime_type, ext = _image_mime_and_ext(data)
                if section == "completed_work":
                    idx = len(photos["completed_work"]) + 1
                    filename = f"{prefix}_completed_{idx:02d}.{ext}"
                else:
                    idx = len(photos["variations"]) + 1
                    filename = f"{prefix}_variation_{idx:02d}.{ext}"

                photos[section].append(
                    {
                        "filename": filename,
                        "mime_type": mime_type,
                        "content_base64": base64.b64encode(data).decode("ascii"),
                    }
                )

    return photos


def _extract_header_fields(text: str) -> Dict[str, Optional[str]]:
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not lines:
        return {"template_title": None, "job_header": None, "corf": None}

    template_title = lines[0]
    job_header = lines[1] if len(lines) > 1 else None
    corf = None
    if job_header:
        m = _CORF_RE.search(job_header)
        if m:
            corf = m.group(1)
    return {"template_title": template_title, "job_header": job_header, "corf": corf}


def parse_operative_job_sheet_text(
    text: str,
    pdf_bytes: Optional[bytes] = None,
    smart_form_x_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Turn raw PDF text into header fields, per-question metadata, and flat sheet fields.

    When pdf_bytes is supplied, Q2.1 Description / Price/Hours are read from PDF
    word positions (reliable 2-column layout). Plain-text parsing is used as fallback.
    """
    cleaned = _clean_pdf_text(text)
    header = _extract_header_fields(cleaned)
    blocks = _split_q_blocks(cleaned)

    questions: Dict[str, Dict[str, Any]] = {}
    flat: Dict[str, Any] = {}

    for block in blocks:
        key = f"Q{block['section']}.{block['number']}"
        if key in questions:
            continue

        label, answer = _split_label_and_answer(block["label"], block["body"])
        entry: Dict[str, Any] = {"label": label, "answer": answer}

        if key == "Q2.1":
            text_table = _parse_daily_jobs_table(answer)
            pdf_table = extract_q21_table_from_pdf(pdf_bytes) if pdf_bytes else None
            if pdf_table:
                table = _merge_q21_tables(pdf_table, text_table)
            else:
                table = text_table
            entry["daily_jobs_table"] = table
            flat["Q2.1_table"] = table
            flat["Q2.1_description"] = table.get("description", "")
            flat["Q2.1_price_hours"] = table.get("price_hours", "")
        else:
            flat[key] = answer.strip()

        questions[key] = entry

    labeled_fields = _build_labeled_fields(questions, flat)

    q11_address = str(flat.get("Q1.1") or labeled_fields.get("Q1.1 Address") or "").strip()
    corf = extract_corf_from_address(q11_address)
    if corf:
        header["corf"] = corf

    photos: Dict[str, List[Dict[str, str]]] = {
        "completed_work": [],
        "variations": [],
    }
    if pdf_bytes:
        q24_answer = str(flat.get("Q2.4") or "").strip().lower()
        q24_variation = q24_answer in {"yes", "y", "true"}
        file_prefix = str(header.get("corf") or smart_form_x_id or "").strip()
        photos = extract_ojs_photos_from_pdf(
            pdf_bytes,
            q24_variation=q24_variation,
            file_prefix=file_prefix,
        )
    _apply_photo_yes_no_fields(questions, labeled_fields, photos)

    return {
        "template_title": header.get("template_title"),
        "job_header": header.get("job_header"),
        "corf": header.get("corf"),
        "questions": questions,
        "fields": flat,
        "labeled_fields": labeled_fields,
        "photos": photos,
        "pdf_text": cleaned,
    }
