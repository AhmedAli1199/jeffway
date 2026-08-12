"""Persist per-CORF WI msg upload results so a crashed batch can still update the sheet."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger("easybop.wi_msg_checkpoint")

_CHECKPOINT_PATH = Path(__file__).resolve().parent / "wi_msg_batch_checkpoint.json"


def _norm_corf(corf: Any) -> str:
    return str(corf or "").strip()


def _load_raw() -> Dict[str, Any]:
    if not _CHECKPOINT_PATH.is_file():
        return {"results": {}}
    try:
        data = json.loads(_CHECKPOINT_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("results"), dict):
            return data
    except Exception as exc:
        log.warning("Could not read checkpoint %s: %s", _CHECKPOINT_PATH, exc)
    return {"results": {}}


def persist_success(result: dict) -> None:
    """Save uploaded / already-on-EasyBOP result keyed by CORF."""
    if not (result.get("uploaded") or result.get("already_uploaded")):
        return
    corf = _norm_corf(result.get("corf"))
    if not corf:
        return
    data = _load_raw()
    results: Dict[str, Any] = data.setdefault("results", {})
    results[corf] = {
        "success": True,
        "uploaded": bool(result.get("uploaded")),
        "already_uploaded": bool(result.get("already_uploaded")),
        "works_id": result.get("works_id"),
        "corf": corf,
        "file_name": result.get("file_name") or "",
        "message": result.get("message") or "",
        "error": None,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    _CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CHECKPOINT_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    log.info("Checkpoint saved CORF=%s uploaded=%s already=%s", corf, result.get("uploaded"), result.get("already_uploaded"))


def clear_checkpoint() -> None:
    if _CHECKPOINT_PATH.is_file():
        _CHECKPOINT_PATH.unlink(missing_ok=True)
        log.info("Checkpoint cleared")


def checkpoint_results_for_corfs(corfs: List[str]) -> List[dict]:
    """Return saved successes for the given CORFs (newest checkpoint wins)."""
    data = _load_raw()
    stored: Dict[str, Any] = data.get("results") or {}
    out: List[dict] = []
    for corf in corfs:
        key = _norm_corf(corf)
        hit = stored.get(key)
        if hit and (hit.get("uploaded") or hit.get("already_uploaded")):
            out.append(dict(hit))
    return out


def merge_results_with_checkpoint(
    decoded: list,
    batch_results: List[dict] | None,
) -> List[dict]:
    """Combine live batch results with checkpoint; unprocessed CORFs are omitted."""
    by_corf: Dict[str, dict] = {}
    for r in batch_results or []:
        if r.get("uploaded") or r.get("already_uploaded"):
            by_corf[_norm_corf(r.get("corf"))] = r
        elif r.get("error") and not (r.get("uploaded") or r.get("already_uploaded")):
            # Explicit per-item failure from this run
            by_corf[_norm_corf(r.get("corf"))] = r

    for item in decoded:
        key = _norm_corf(item.get("corf"))
        if key in by_corf:
            continue
        for ck in checkpoint_results_for_corfs([key]):
            by_corf[key] = ck

    ordered: List[dict] = []
    seen = set()
    for item in decoded:
        key = _norm_corf(item.get("corf"))
        if not key or key in seen:
            continue
        seen.add(key)
        if key in by_corf:
            ordered.append(by_corf[key])
    return ordered


def summarize_results(results: List[dict], total: int) -> dict:
    uploaded = sum(1 for r in results if r.get("uploaded"))
    already = sum(1 for r in results if r.get("already_uploaded"))
    errors = sum(
        1 for r in results
        if not (r.get("uploaded") or r.get("already_uploaded"))
    )
    return {
        "success": errors == 0 and len(results) >= total,
        "total": total,
        "uploaded": uploaded,
        "already_uploaded": already,
        "errors": errors,
        "results": results,
    }
