"""
asbestos_pdf_parser.py
======================
Extract text and ACM register lines from asbestos survey PDFs (EasyBOP upload).
Used by the filler API and pre-works JIS pipeline.
"""

from __future__ import annotations

import io
import re
from typing import Any, Dict, List

import pdfplumber

_REGISTER_HINT = re.compile(
    r"\b(NAD|P\b|S0[1-3]|PTCA|No Access|Negative|Presumed|Asbestos Identified)\b",
    re.I,
)
_LOCATION_HINT = re.compile(
    r"(kitchen|bathroom|bedroom|hall|loft|garage|cupboard|landing|external|stairs|cellar)",
    re.I,
)
_ASBESTOS_KEYWORD = re.compile(r"asbestos|ACM|NAD|PTCA", re.I)


def parse_asbestos_pdf_bytes(
    pdf_bytes: bytes,
    *,
    address: str = "",
    uprn: str = "",
) -> Dict[str, Any]:
    pages_text: List[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            if t.strip():
                pages_text.append(t)

    full_text = "\n\n".join(pages_text).strip()
    if not full_text:
        return {
            "asbestos_status": False,
            "asbestos_summary": "",
            "asbestos_items": "",
            "page_count": len(pages_text),
            "error": "no_text_extracted",
        }

    items = _extract_register_items(full_text)
    summary = _build_summary(full_text, items, address=address, uprn=uprn)
    status = _infer_status(full_text, items)

    return {
        "asbestos_status": status,
        "asbestos_summary": summary,
        "asbestos_items": items,
        "page_count": len(pages_text),
        "text_length": len(full_text),
    }


def _extract_register_items(text: str) -> str:
    lines: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or len(line) < 8:
            continue
        if _REGISTER_HINT.search(line) and (
            _LOCATION_HINT.search(line) or len(line) > 25
        ):
            lines.append(line)

    if not lines:
        for raw in text.splitlines():
            line = raw.strip()
            if _ASBESTOS_KEYWORD.search(line) and len(line) > 10:
                lines.append(line)

    seen: set[str] = set()
    out: List[str] = []
    for line in lines:
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    return "\n".join(out[:40])


def _build_summary(text: str, items: str, *, address: str, uprn: str) -> str:
    head = re.sub(r"\s+", " ", text[:2500]).strip()
    parts: List[str] = []
    if address:
        parts.append(f"Property: {address}")
    if uprn:
        parts.append(f"UPRN: {uprn}")
    if items:
        n = len([l for l in items.splitlines() if l.strip()])
        parts.append(f"{n} ACM register line(s) parsed")
    if re.search(r"no asbestos|nad only|no acm", head, re.I):
        parts.append("No ACM identified in accessed areas")
    elif re.search(r"asbestos|acm|ptca|presumed", head, re.I):
        parts.append("Asbestos/ACM references found")
    if head:
        parts.append(head[:400])
    return ". ".join(parts)


def _infer_status(text: str, items: str) -> str:
    blob = (text + "\n" + items).lower()
    if re.search(r"\b(ptca|presumed|s0[1-3]|asbestos identified|strongly presumed)\b", blob):
        return "PTCA/ACM"
    if re.search(r"\b(nad|negative|not identified|negatively sampled)\b", blob):
        return "NAD"
    if re.search(r"\bno access|exercise caution\b", blob):
        return "No Access"
    if items.strip():
        return "Report on file"
    return "Report on file"
