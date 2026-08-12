"""
Stage 7 — ZIP Helper

Receives a list of files (filename + base64 content) and returns a single
ZIP archive as base64. No browser or Playwright involved — pure stdlib.

Used by n8n after it fetches SharePoint photos via Graph API, so the photos
can be attached as a single ZIP to the handover draft email.
"""

from __future__ import annotations

import base64
import io
import logging
import zipfile

log = logging.getLogger("stage7.zip")


def build_zip_base64(
    files: list[dict],
    zip_name: str = "photos.zip",
) -> dict:
    """
    files: [{"filename": "photo1.jpg", "base64": "<b64 string>"}, ...]

    Returns:
      {
        success: bool,
        zip_filename: str,
        zip_base64: str,
        file_count: int,
        message: str,
      }
    """
    if not files:
        raise ValueError("No files provided to zip")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in files:
            filename = item.get("filename", "file")
            raw = base64.b64decode(item["base64"])
            zf.writestr(filename, raw)
            log.info("zip: added %r (%d bytes)", filename, len(raw))

    buf.seek(0)
    zip_b64 = base64.b64encode(buf.read()).decode("utf-8")
    log.info("zip: created %r — %d files, base64 length=%d", zip_name, len(files), len(zip_b64))

    return {
        "success":      True,
        "zip_filename": zip_name,
        "zip_base64":   zip_b64,
        "file_count":   len(files),
        "message":      f"Zipped {len(files)} file(s) into {zip_name}",
    }
