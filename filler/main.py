"""
Jeffway EasyBOP API  --  port 8000
===================================

All automation endpoints in a single FastAPI app.

Swagger UI: http://173.212.233.153:8000/docs

Endpoints
---------
System
  GET  /health

Auth
  POST /login

Excel exports (existing, for n8n)
  POST /download-works-template
  POST /download-works-template/file
  GET  /files/reactive-works-instructions-template
  POST /download-works-list-export-stage6
  POST /download-works-list-export-stage6/file
  POST /download-scheduling-register-stage6
  POST /download-scheduling-register-stage6/file
  POST /compare-next-appointment-stage6
  POST /resolve-works-id-stage6
  POST /update-works-next-appointment-stage6
  POST /stage5/stalled-appointment-check
  POST /stage7/add-variation
  POST /stage7/add-variation-and-tasks
  POST /stage7/create-tasks
  POST /stage7/scan-ready-for-handover
  POST /stage7/set-handover-date
  POST /stage7/export-job-documents
  POST /stage7/mark-job-complete
  POST /stage7/zip-files
  POST /stage7/add-email-review-task
  POST /stage7/create-handover-task
  POST /create-handover-task-stage7
  POST /download-boq-template
  POST /download-boq-template/file
  GET  /files/boq-import-template

Form Filler
  POST /fill-work-order
  POST /fill-work-order/from-n8n

Stage 1 Import (download + upload xlsx)
  POST /download-excel-files
  POST /download-wi-xlsx
  POST /download-boq-xlsx
  POST /upload-works-file
  POST /upload-boq-file
  POST /insert-boq-item

WI MSG Upload (BCC .msg files to EasyBOP)
  POST /wi-msg/upload-batch
  POST /wi-msg/upload

Stage 6 (operative job sheets, BOQ, job report)
  POST /extract-operative-job-sheets
  POST /works-index
  POST /fetch-boq-for-corf
  POST /fetch-boq-batch
  POST /boq-pool/reset
  POST /generate-job-report-pdf
  POST /generate-daily-exception-report-pdf

Run:
    sudo /root/projects/jeffway/deploy/jeffway-api.sh restart
"""

import asyncio
import importlib.util
import logging
import re
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Optional, Tuple

_FILLER_DIR = Path(__file__).resolve().parent
_JEFFWAY_ROOT = _FILLER_DIR.parent

# Stage 6 folder has been referred to both as "Stage6" and "Stage 6" — resolve whichever exists.
_STAGE6_DIR = next(
    (_JEFFWAY_ROOT / _n for _n in ("Stage6", "Stage 6") if (_JEFFWAY_ROOT / _n).is_dir()),
    _JEFFWAY_ROOT / "Stage6",
)

if str(_FILLER_DIR) not in sys.path:
    sys.path.insert(0, str(_FILLER_DIR))
if str(_JEFFWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(_JEFFWAY_ROOT))

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, Browser, BrowserContext, Error as PlaywrightError

from config import settings
from automation import login as _easybop_login
from ip_allowlist import IPAllowlistMiddleware, parse_allowed_ips

from work_inst_template.download_works_template import download_works_instruction_template
from work_inst_template.download_works_list_export import download_works_list_export
from work_inst_template.download_scheduling_register import download_scheduling_register_export
from work_inst_template.update_works_next_appointment import (
    update_next_appointment_on_page,
    _norm_corf,
)
from boq_template.download_boq_template import download_boq_import_template

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("easybop.api")

# ──────────────────────────────────────────────────────────────────
# Browser lifecycle  (shared across all endpoints)
# ──────────────────────────────────────────────────────────────────
_browser: Optional[Browser] = None
_context: Optional[BrowserContext] = None
_playwright: Any = None
_corf_works_meta_cache: Dict[str, List[Dict[str, str]]] = {}
_corf_works_meta_ts: float = 0.0
_CORF_WORKS_META_TTL: float = 3600.0
_corf_works_meta_lock: Optional[asyncio.Lock] = None
_session_path = Path(settings.session_file)
if not _session_path.is_absolute():
    _session_path = _FILLER_DIR / _session_path


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _browser, _context, _playwright
    _playwright = await async_playwright().start()
    launch_kwargs: Dict[str, Any] = {
        "headless": settings.headless,
        "args": ["--window-size=1400,900"],
    }
    if settings.slow_mo_ms > 0:
        launch_kwargs["slow_mo"] = settings.slow_mo_ms
    _browser = await _playwright.chromium.launch(**launch_kwargs)
    if _session_path.exists():
        _context = await _browser.new_context(storage_state=str(_session_path))
        logger.info("Restored EasyBOP session from %s", _session_path)
    else:
        _context = await _browser.new_context()
        logger.info("No saved session — call POST /login first")
    yield
    if _context is not None:
        await _context.close()
        _context = None
    if _browser is not None:
        await _browser.close()
        _browser = None
    if _playwright is not None:
        await _playwright.stop()
        _playwright = None



def _browser_connected() -> bool:
    return _browser is not None and _browser.is_connected()


async def _start_browser() -> None:
    global _browser, _context, _playwright
    if _browser is not None and _browser.is_connected():
        return
    if _context is not None:
        try:
            await _context.close()
        except Exception:
            pass
        _context = None
    if _browser is not None:
        try:
            await _browser.close()
        except Exception:
            pass
        _browser = None
    if _playwright is None:
        _playwright = await async_playwright().start()
    launch_kwargs: Dict[str, Any] = {
        "headless": settings.headless,
        "args": ["--window-size=1400,900"],
    }
    if settings.slow_mo_ms > 0:
        launch_kwargs["slow_mo"] = settings.slow_mo_ms
    _browser = await _playwright.chromium.launch(**launch_kwargs)
    if _session_path.exists():
        _context = await _browser.new_context(storage_state=str(_session_path))
        logger.info("Browser restarted — restored session from %s", _session_path)
    else:
        _context = await _browser.new_context()
        logger.info("Browser restarted — no saved session")


def _require_browser() -> None:
    if _browser is None or not _browser.is_connected():
        raise HTTPException(status_code=503, detail="Browser not running — restart the server")


def _is_browser_closed_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return (
        "target page, context or browser has been closed" in msg
        or "browser has been closed" in msg
        or "target closed" in msg
        or isinstance(exc, PlaywrightError) and "closed" in msg
    )


async def _ensure_browser() -> None:
    """Start or restart the shared Playwright browser if disconnected."""
    if _browser is not None and _browser.is_connected() and _context is not None:
        return
    logger.warning("Browser not connected — restarting Playwright")
    await _start_browser()


async def _save_session() -> None:
    _require_browser()
    _session_path.parent.mkdir(parents=True, exist_ok=True)
    await _context.storage_state(path=str(_session_path))


# ──────────────────────────────────────────────────────────────────
# App definition
# ──────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Jeffway EasyBOP API",
    description=(
        "Playwright-powered automation API for EasyBOP.\n\n"
        "**Typical flow:** `POST /login` once, then use any endpoint below.\n\n"
        "- **Excel exports:** download WI/BOQ templates from EasyBOP (for n8n)\n"
        "- **Form Filler:** fill Works Instruction forms from flat/n8n payloads\n"
        "- **Stage 1 Import:** download Google Sheet xlsx + upload to EasyBOP\n"
        "- **WI MSG Upload:** upload .msg files to Works Order pre-work dialog\n"
        "- **Stage 6:** extract operative job sheets, scrape works index, fetch BOQ/SOR, generate job report PDF\n"
        "- **Stage 5:** stalled appointment check (scheduling register vs OJS register)\n"
        "- **JIS:** fill JIS forms in batch\n"
        "- **Asbestos:** upload asbestos PDFs and monitor tasks\n"
    ),
    version="2.0.0",
    lifespan=lifespan,
)

_allowed_ips = parse_allowed_ips(settings.api_allowed_ips)
if _allowed_ips is not None:
    app.add_middleware(IPAllowlistMiddleware, allowed_ips=_allowed_ips)
    logger.info("API IP allowlist enabled: %s", ", ".join(sorted(_allowed_ips)))
else:
    logger.warning("API IP allowlist disabled (API_ALLOWED_IPS=*)")


# ──────────────────────────────────────────────────────────────────
# System
# ──────────────────────────────────────────────────────────────────
@app.get("/health", tags=["System"], summary="Liveness check")
async def health():
    return {
        "status": "ok",
        "browser_running": _browser is not None and _browser.is_connected(),
        "session_saved": _session_path.exists(),
        "session_path": str(_session_path),
        "headless": settings.headless,
        "port": settings.api_port,
    }


# ──────────────────────────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────────────────────────
@app.post("/login", tags=["Auth"], summary="Authenticate with EasyBOP and save session")
async def do_login():
    _require_browser()
    page = await _context.new_page()
    try:
        ok = await _easybop_login(page, settings.username, settings.password)
        if not ok:
            raise HTTPException(status_code=401, detail="EasyBOP login failed — check credentials in .env")
        await _save_session()
        return {"success": True, "message": "Logged in and session saved", "session_path": str(_session_path)}
    except HTTPException:
        raise
    except Exception:
        logger.exception("login")
        raise HTTPException(status_code=500, detail="Unexpected error in /login — see server logs.")
    finally:
        await page.close()


# ──────────────────────────────────────────────────────────────────
# Excel exports  (template downloads — unchanged from v1)
# ──────────────────────────────────────────────────────────────────
_WI_TEMPLATE_XLSX = _JEFFWAY_ROOT / "work_inst_template" / "Reactive-Works-Instructions-Template.xlsx"
_WI_LIST_EXPORT_XLSX = _JEFFWAY_ROOT / "work_inst_template" / "Works-List-Export.xlsx"
_BOQ_TEMPLATE_XLSX = _JEFFWAY_ROOT / "boq_template" / "BOQ-Import-Template.xlsx"


def _latest_xlsx(canonical: Path) -> Optional[Path]:
    if canonical.is_file():
        return canonical
    candidates = sorted(canonical.parent.glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


class DownloadWorksTemplateRequest(BaseModel):
    contract_id: str = "321129"
    output_dir: Optional[str] = None
    keep_page_open: Optional[bool] = None


class DownloadBoqTemplateRequest(BaseModel):
    contract_id: str = "321129"
    output_dir: Optional[str] = None
    keep_page_open: Optional[bool] = None


class DownloadWorksListExportRequest(BaseModel):
    contract_id: str = "321129"
    output_dir: Optional[str] = None
    keep_page_open: Optional[bool] = None


class DownloadSchedulingRegisterRequest(BaseModel):
    contract_id: str = "321129"
    output_dir: Optional[str] = None
    keep_page_open: Optional[bool] = None


class NextAppointmentUpdateItem(BaseModel):
    works_id: str = Field(..., min_length=1)
    next_appointment_date: str = Field(..., description="ISO (YYYY-MM-DD) or DD/MM/YYYY")
    client_order_reference: Optional[str] = None
    address: Optional[str] = None
    current_next_appointment_date: Optional[str] = None


class ResolveWorksIdRequest(BaseModel):
    contract_id: str = "321129"
    client_order_reference: Optional[str] = Field(
        None, description="Single CORF to resolve (use in n8n loop)"
    )
    client_order_references: Optional[list[str]] = Field(
        None, description="Batch resolve multiple CORFs in one call"
    )
    address: Optional[str] = Field(
        None,
        description="Property address to disambiguate duplicate CORFs (e.g. multiple TBC rows)",
    )
    force_refresh: bool = Field(
        False,
        description="Re-scrape planned-works index (all pages) instead of using cache",
    )


class UpdateWorksNextAppointmentRequest(BaseModel):
    works_id: str = Field(..., min_length=1)
    next_appointment_date: str = Field(..., description="ISO (YYYY-MM-DD) or DD/MM/YYYY")
    contract_id: str = "321129"
    client_order_reference: Optional[str] = None
    address: Optional[str] = None
    current_next_appointment_date: Optional[str] = None
    click_save: bool = Field(True, description="Click Save Works UDF Data and accept confirm dialog")


async def _do_wi_template(req: DownloadWorksTemplateRequest) -> Tuple[Path, str]:
    _require_browser()
    out_dir = Path(req.output_dir).expanduser() if req.output_dir else None
    keep = settings.keep_page_open_after_fill if req.keep_page_open is None else req.keep_page_open
    page = await _context.new_page()
    try:
        saved, url = await download_works_instruction_template(
            page, context=_context, base_url=settings.easybop_base_url,
            contract_id=req.contract_id, login=_easybop_login,
            username=settings.username, password=settings.password,
            session_path=_session_path, timeout_ms=settings.request_timeout_ms,
            output_dir=out_dir,
        )
        return saved, url
    finally:
        if not keep:
            await page.close()


async def _do_works_list_export(req: DownloadWorksListExportRequest) -> Tuple[Path, str]:
    _require_browser()
    out_dir = Path(req.output_dir).expanduser() if req.output_dir else None
    keep = settings.keep_page_open_after_fill if req.keep_page_open is None else req.keep_page_open
    page = await _context.new_page()
    try:
        saved, url = await download_works_list_export(
            page, context=_context, base_url=settings.easybop_base_url,
            contract_id=req.contract_id, login=_easybop_login,
            username=settings.username, password=settings.password,
            session_path=_session_path, timeout_ms=settings.request_timeout_ms,
            output_dir=out_dir,
        )
        return saved, url
    finally:
        if not keep:
            await page.close()


async def _do_scheduling_register_export(req: DownloadSchedulingRegisterRequest) -> Tuple[Path, str]:
    _require_browser()
    out_dir = Path(req.output_dir).expanduser() if req.output_dir else None
    keep = settings.keep_page_open_after_fill if req.keep_page_open is None else req.keep_page_open
    page = await _context.new_page()
    try:
        saved, url = await download_scheduling_register_export(
            page, context=_context, base_url=settings.easybop_base_url,
            contract_id=req.contract_id, login=_easybop_login,
            username=settings.username, password=settings.password,
            session_path=_session_path, timeout_ms=settings.request_timeout_ms,
            output_dir=out_dir,
        )
        return saved, url
    finally:
        if not keep:
            await page.close()


async def _do_boq_template(req: DownloadBoqTemplateRequest) -> Tuple[Path, str]:
    _require_browser()
    out_dir = Path(req.output_dir).expanduser() if req.output_dir else None
    keep = settings.keep_page_open_after_fill if req.keep_page_open is None else req.keep_page_open
    page = await _context.new_page()
    try:
        saved, url = await download_boq_import_template(
            page, context=_context, base_url=settings.easybop_base_url,
            contract_id=req.contract_id, login=_easybop_login,
            username=settings.username, password=settings.password,
            session_path=_session_path, timeout_ms=settings.request_timeout_ms,
            output_dir=out_dir,
        )
        return saved, url
    finally:
        if not keep:
            await page.close()


@app.post("/download-works-template", tags=["Excel exports"],
          summary="Save WI template xlsx to work_inst_template/")
async def download_wi_template_ep(req: DownloadWorksTemplateRequest):
    try:
        saved, url = await _do_wi_template(req)
        return {"success": True, "file_path": str(saved), "landed_url": url}
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception:
        logger.exception("download-works-template")
        raise HTTPException(status_code=500, detail="Failed — see server logs.")


@app.post("/download-works-template/file", tags=["Excel exports"],
          summary="Download WI template and return xlsx bytes (for n8n HTTP node)")
async def download_wi_template_file(req: DownloadWorksTemplateRequest):
    try:
        saved, _ = await _do_wi_template(req)
        return FileResponse(saved, filename=saved.name,
                            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception:
        logger.exception("download-works-template/file")
        raise HTTPException(status_code=500, detail="Failed — see server logs.")


@app.get("/files/reactive-works-instructions-template", tags=["Excel exports"],
         summary="Serve last downloaded WI template (GET for n8n)")
async def serve_wi_template():
    p = _latest_xlsx(_WI_TEMPLATE_XLSX)
    if not p:
        raise HTTPException(status_code=404, detail="Run POST /download-works-template first.")
    return FileResponse(p, filename=p.name,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.post("/download-works-list-export-stage6", tags=["Excel exports"],
          summary="Save z_works Export list xlsx (Stage 6)")
async def download_works_list_export_ep(req: DownloadWorksListExportRequest):
    try:
        saved, url = await _do_works_list_export(req)
        return {"success": True, "file_path": str(saved), "landed_url": url}
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception:
        logger.exception("download-works-list-export-stage6")
        raise HTTPException(status_code=500, detail="Failed — see server logs.")


@app.post("/download-works-list-export-stage6/file", tags=["Excel exports"],
          summary="z_works Export button → xlsx bytes for n8n (Stage 6)")
async def download_works_list_export_file(req: DownloadWorksListExportRequest):
    try:
        saved, _ = await _do_works_list_export(req)
        return FileResponse(saved, filename=saved.name,
                            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception:
        logger.exception("download-works-list-export-stage6/file")
        raise HTTPException(status_code=500, detail="Failed — see server logs.")


@app.post("/download-scheduling-register-stage6", tags=["Excel exports"],
          summary="Save Scheduling Register export xlsx (Stage 6)")
async def download_scheduling_register_ep(req: DownloadSchedulingRegisterRequest):
    try:
        saved, url = await _do_scheduling_register_export(req)
        return {"success": True, "file_path": str(saved), "landed_url": url}
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception:
        logger.exception("download-scheduling-register-stage6")
        raise HTTPException(status_code=500, detail="Failed — see server logs.")


@app.post("/download-scheduling-register-stage6/file", tags=["Excel exports"],
          summary="Scheduling Register #btn_export_list → xlsx bytes for n8n")
async def download_scheduling_register_file(req: DownloadSchedulingRegisterRequest):
    try:
        saved, _ = await _do_scheduling_register_export(req)
        return FileResponse(saved, filename=saved.name,
                            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception:
        logger.exception("download-scheduling-register-stage6/file")
        raise HTTPException(status_code=500, detail="Failed — see server logs.")


@app.post("/compare-next-appointment-stage6", tags=["Excel exports"],
          summary="Compare scheduling register vs works list — Next appointment diffs")
async def compare_next_appointment_stage6(req: DownloadWorksListExportRequest):
    """Download both exports fresh, match by Client Order Reference / Client Reference."""
    try:
        sched_path, _ = await _do_scheduling_register_export(
            DownloadSchedulingRegisterRequest(
                contract_id=req.contract_id,
                output_dir=req.output_dir,
                keep_page_open=req.keep_page_open,
            )
        )
        wi_path, _ = await _do_works_list_export(req)
        stage6 = _STAGE6_DIR
        if str(stage6) not in sys.path:
            sys.path.insert(0, str(stage6))
        from compare_next_appointment import compare_next_appointment

        result = compare_next_appointment(sched_path, wi_path)
        return {"success": True, **result}
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception:
        logger.exception("compare-next-appointment-stage6")
        raise HTTPException(status_code=500, detail="Failed — see server logs.")


def _load_stage6_scrape_works_index():
    stage6 = _STAGE6_DIR
    if str(stage6) not in sys.path:
        sys.path.insert(0, str(stage6))
    from automation import scrape_works_index  # noqa: WPS433

    return scrape_works_index


def _get_corf_meta_lock() -> asyncio.Lock:
    global _corf_works_meta_lock
    if _corf_works_meta_lock is None:
        _corf_works_meta_lock = asyncio.Lock()
    return _corf_works_meta_lock


def _norm_address(addr: str) -> str:
    s = re.sub(r"[^\w\s]", " ", str(addr or "").lower())
    return re.sub(r"\s+", " ", s).strip()


_ADDR_STOP_WORDS = frozenset(
    {"bk", "flat", "floor", "first", "second", "the", "and", "st", "rd", "ave", "avenue"}
)


def _address_match_score(want: str, have: str) -> int:
    """Higher score = better address match. 0 means no meaningful match."""
    a = _norm_address(want)
    b = _norm_address(have)
    if not a or not b:
        return 0
    if a == b:
        return 100
    if a in b or b in a:
        return 85
    ta = {t for t in a.split() if t not in _ADDR_STOP_WORDS and (len(t) > 1 or t.isdigit())}
    tb = {t for t in b.split() if t not in _ADDR_STOP_WORDS and (len(t) > 1 or t.isdigit())}
    if not ta or not tb:
        return 0
    overlap = len(ta & tb)
    if overlap == 0:
        return 0
    return 40 + overlap * 15


def _corf_lookup_keys(raw: str) -> list[str]:
    """Normalised CORF plus base form without /variation suffix."""
    n = _norm_corf(raw)
    if not n:
        return []
    keys = [n]
    if "/" in n:
        base = n.split("/", 1)[0]
        if base and base not in keys:
            keys.append(base)
    return keys


def _resolve_match_method(raw: str, hit: Dict[str, str], *, ambiguous: bool = False) -> str:
    """Classify whether lookup matched client_order_reference or property_ref (UPRN)."""
    nraw = _norm_corf(raw)
    hit_corf = _norm_corf(hit.get("client_order_reference", ""))
    hit_prop = _norm_corf(hit.get("property_ref", ""))
    if nraw and nraw == hit_prop and nraw != hit_corf:
        return "property_ref"
    if ambiguous:
        return "corf_ambiguous"
    return "corf"


def _lookup_corf_meta(
    meta: Dict[str, List[Dict[str, str]]],
    raw: str,
    *,
    address: str = "",
) -> tuple[Dict[str, str], str]:
    """Return (entry, match_method). match_method: corf | corf+address | corf+address_best."""
    candidates: List[Dict[str, str]] = []
    for key in _corf_lookup_keys(raw):
        candidates.extend(meta.get(key, []))
    if not candidates:
        return {}, "not_found"

    # De-dupe by works_id while preserving order
    seen: set[str] = set()
    unique: List[Dict[str, str]] = []
    for row in candidates:
        wid = str(row.get("works_id") or "").strip()
        if not wid or wid in seen:
            continue
        seen.add(wid)
        unique.append(row)
    candidates = unique

    if len(candidates) == 1:
        return candidates[0], _resolve_match_method(raw, candidates[0])

    addr = str(address or "").strip()
    if addr:
        scored = [(c, _address_match_score(addr, c.get("address", ""))) for c in candidates]
        scored.sort(key=lambda x: x[1], reverse=True)
        best, best_score = scored[0]
        if best_score >= 40:
            method = _resolve_match_method(raw, best)
            if method == "corf":
                return best, "corf+address"
            return best, method
        if scored[1][1] == best_score:
            return candidates[0], _resolve_match_method(raw, candidates[0], ambiguous=True)
        method = _resolve_match_method(raw, best)
        if method == "corf":
            return best, "corf+address_best"
        return best, method

    return candidates[0], _resolve_match_method(raw, candidates[0], ambiguous=True)


async def _corf_to_works_id_map(*, force_refresh: bool = False) -> Dict[str, List[Dict[str, str]]]:
    global _corf_works_meta_cache, _corf_works_meta_ts
    lock = _get_corf_meta_lock()

    async with lock:
        age = time.monotonic() - _corf_works_meta_ts
        if _corf_works_meta_cache and age < _CORF_WORKS_META_TTL and not force_refresh:
            logger.info(
                "resolve-works-id: cache hit (%s CORFs, age=%.0fs)",
                len(_corf_works_meta_cache),
                age,
            )
            return _corf_works_meta_cache

        scrape_works_index = _load_stage6_scrape_works_index()
        logger.info("resolve-works-id: scraping planned-works index for CORF map")
        index_rows: List[Dict[str, Any]] = []
        last_err: Optional[BaseException] = None
        for attempt in range(2):
            try:
                await _ensure_browser()
                index_rows = await scrape_works_index(
                    _context,
                    username=settings.username,
                    password=settings.password,
                    timeout_ms=settings.request_timeout_ms,
                    after_relogin=_save_session,
                )
                break
            except Exception as exc:
                last_err = exc
                if attempt == 0 and _is_browser_closed_error(exc):
                    logger.warning(
                        "resolve-works-id: browser closed during index scrape — retrying once"
                    )
                    await _start_browser()
                    continue
                raise
        if last_err is not None and not index_rows:
            raise last_err
        meta: Dict[str, List[Dict[str, str]]] = {}
        for row in index_rows:
            corf_raw = str(row.get("client_order_reference") or "").strip()
            wid = str(row.get("works_id") or "").strip()
            if not corf_raw or not wid:
                continue
            entry = {
                "works_id": wid,
                "address": str(row.get("address") or ""),
                "property_ref": str(row.get("property_ref") or ""),
                "client_order_reference": corf_raw,
            }
            keys: set[str] = set()
            for key in _corf_lookup_keys(corf_raw):
                keys.add(key)
            prop_key = _norm_corf(entry["property_ref"])
            if prop_key:
                keys.add(prop_key)
            for key in keys:
                meta.setdefault(key, []).append(entry)
        _corf_works_meta_cache = meta
        _corf_works_meta_ts = time.monotonic()
        logger.info(
            "resolve-works-id: index scraped rows=%s corf_map=%s",
            len(index_rows),
            len(meta),
        )
        return meta


@app.post("/resolve-works-id-stage6", tags=["Excel exports"],
          summary="Resolve EasyBOP works_id from Client Order Reference")
async def resolve_works_id_stage6(req: ResolveWorksIdRequest):
    """Scrape planned-works index and look up works_id by CORF (and address when duplicates exist)."""
    await _ensure_browser()
    corfs: list[str] = []
    if req.client_order_reference:
        corfs.append(req.client_order_reference.strip())
    if req.client_order_references:
        corfs.extend(c.strip() for c in req.client_order_references if c and c.strip())
    if not corfs:
        raise HTTPException(status_code=400, detail="Provide client_order_reference or client_order_references")

    resolve_address = str(req.address or "").strip()

    try:
        meta = await _corf_to_works_id_map(force_refresh=req.force_refresh)
        if not req.force_refresh:
            missing = [
                raw for raw in corfs
                if not _lookup_corf_meta(meta, raw, address=resolve_address)[0]
            ]
            if missing:
                logger.info(
                    "resolve-works-id: %s CORF(s) not in cache — re-scraping index (all pages)",
                    len(missing),
                )
                meta = await _corf_to_works_id_map(force_refresh=True)
        results: list[Dict[str, Any]] = []
        for raw in corfs:
            hit, match_method = _lookup_corf_meta(
                meta, raw, address=resolve_address if len(corfs) == 1 else ""
            )
            wid = hit.get("works_id", "")
            found = bool(wid)
            row = {
                "client_order_reference": raw,
                "works_id": wid,
                "address": hit.get("address", ""),
                "property_ref": hit.get("property_ref", ""),
                "found": found,
                "match_method": match_method,
                "resolve_address": resolve_address or None,
            }
            if not found:
                row["error"] = "works_id not found on EasyBOP planned-works index"
            elif match_method == "corf_ambiguous":
                row["warning"] = (
                    "Multiple jobs share this CORF — pass address to disambiguate"
                )
            results.append(row)
            logger.info(
                "resolve-works-id: corf=%s works_id=%s found=%s match=%s address=%s resolve_addr=%s",
                raw,
                wid or "-",
                found,
                match_method,
                (row["address"][:40] + "…") if len(row["address"]) > 40 else row["address"] or "-",
                (resolve_address[:40] + "…") if len(resolve_address) > 40 else resolve_address or "-",
            )

        found_count = sum(1 for r in results if r.get("found"))
        single = len(results) == 1
        payload: Dict[str, Any] = {
            "success": found_count == len(results),
            "total": len(results),
            "found_count": found_count,
            "results": results,
            "message": f"Resolved works_id for {found_count}/{len(results)} CORF(s).",
        }
        if single:
            payload.update(results[0])
        return payload
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as exc:
        if _is_browser_closed_error(exc):
            raise HTTPException(
                status_code=503,
                detail="Browser closed during resolve (API may be restarting) — retry the workflow",
            ) from exc
        logger.exception("resolve-works-id-stage6")
        raise HTTPException(status_code=500, detail="Failed — see server logs.")


@app.post("/update-works-next-appointment-stage6", tags=["Excel exports"],
          summary="Fill Next appointment date UDF on one EasyBOP job and save")
async def update_works_next_appointment_stage6(req: UpdateWorksNextAppointmentRequest):
    """
    Open ``works_details.php?tab_nav_item=works_udf`` and fill **#udf_pw_works_5088**.

    Call ``POST /resolve-works-id-stage6`` first to obtain ``works_id``.
    """
    await _ensure_browser()
    page = await _context.new_page()
    try:
        result = await update_next_appointment_on_page(
            page,
            works_id=req.works_id,
            next_appointment_date=req.next_appointment_date,
            contract_id=req.contract_id,
            username=settings.username,
            password=settings.password,
            login=_easybop_login,
            timeout_ms=settings.request_timeout_ms,
            click_save=req.click_save,
            client_order_reference=req.client_order_reference or "",
            address=req.address or "",
            current_next_appointment_date=req.current_next_appointment_date or "",
        )
        return {
            "success": bool(
                result.get("success") and (not req.click_save or result.get("saved"))
            ),
            **result,
            "message": (
                f"works_id={req.works_id} corf={req.client_order_reference or '-'} "
                f"filled={result.get('next_appointment_date')} "
                f"verified={result.get('field_value_verified', result.get('field_value_after'))} "
                f"ok={result.get('success')} saved={result.get('saved')}"
            ),
        }
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception:
        logger.exception(
            "update-works-next-appointment-stage6 works_id=%s corf=%s",
            req.works_id,
            req.client_order_reference,
        )
        raise HTTPException(status_code=500, detail="Failed — see server logs.")
    finally:
        await page.close()


@app.post("/download-boq-template", tags=["Excel exports"],
          summary="Save BOQ template xlsx to boq_template/")
async def download_boq_template_ep(req: DownloadBoqTemplateRequest):
    try:
        saved, url = await _do_boq_template(req)
        return {"success": True, "file_path": str(saved), "landed_url": url}
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception:
        logger.exception("download-boq-template")
        raise HTTPException(status_code=500, detail="Failed — see server logs.")


@app.post("/download-boq-template/file", tags=["Excel exports"],
          summary="Download BOQ template and return xlsx bytes (for n8n HTTP node)")
async def download_boq_template_file(req: DownloadBoqTemplateRequest):
    try:
        saved, _ = await _do_boq_template(req)
        return FileResponse(saved, filename=saved.name,
                            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception:
        logger.exception("download-boq-template/file")
        raise HTTPException(status_code=500, detail="Failed — see server logs.")


@app.get("/files/boq-import-template", tags=["Excel exports"],
         summary="Serve last downloaded BOQ template (GET for n8n)")
async def serve_boq_template():
    p = _latest_xlsx(_BOQ_TEMPLATE_XLSX)
    if not p:
        raise HTTPException(status_code=404, detail="Run POST /download-boq-template first.")
    return FileResponse(p, filename=p.name,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ──────────────────────────────────────────────────────────────────
# Dynamic module loader with Pydantic model rebuild
# ──────────────────────────────────────────────────────────────────
def _load_path(name: str, path: Path) -> ModuleType:
    """Load a .py file as a module and rebuild its Pydantic models."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    ns = dict(vars(mod))
    for val in ns.values():
        if isinstance(val, type) and issubclass(val, BaseModel) and val is not BaseModel:
            try:
                val.model_rebuild(_types_namespace=ns)
            except Exception:
                pass
    return mod


def _load(name: str, filename: str) -> ModuleType:
    return _load_path(name, _FILLER_DIR / filename)


# Form Filler  (api.py)
_filler_api = _load("_filler_api", "api.py")
app.include_router(
    _filler_api.create_router(
        get_context=lambda: _context,
        require_browser=_require_browser,
        save_session=_save_session,
    )
)

# Stage 1 Import  (stage1_import_api.py)
_stage1_api = _load("_stage1_import_api", "stage1_import_api.py")
app.include_router(_stage1_api.create_router(headless=settings.headless))

# WI MSG Upload  (wi_msg_upload_api.py)
_wi_msg_api = _load("_wi_msg_api", "wi_msg_upload_api.py")
app.include_router(
    _wi_msg_api.create_router(
        get_context=lambda: _context,
        require_browser=_require_browser,
        save_session=_save_session,
        start_browser=_start_browser,
        browser_connected=_browser_connected,
    )
)

# Asbestos Bypass  (asbestos_bypass_api.py)
_asbestos_bypass_api = _load("_asbestos_bypass_api", "asbestos_bypass_api.py")
app.include_router(
    _asbestos_bypass_api.create_router(
        get_context=lambda: _context,
        require_browser=_require_browser,
        save_session=_save_session,
        start_browser=_start_browser,
        browser_connected=_browser_connected,
    )
)

# JIS  (JIS-form/api.py)
_jis_api = _load_path("_jis_api", _JEFFWAY_ROOT / "JIS-form" / "api.py")
app.include_router(
    _jis_api.create_router(
        get_context=lambda: _context,
        require_browser=_require_browser,
        save_session=_save_session,
    )
)

# Stage 3 asbestos monitor (stage3/asbestos_monitor/api.py)
_asbestos_api = _load_path("_asbestos_api", _JEFFWAY_ROOT / "stage3" / "asbestos_monitor" / "api.py")
app.include_router(
    _asbestos_api.create_router(
        get_context=lambda: _context,
        require_browser=_require_browser,
    )
)

# Stage 4  (Stage4/api.py)
_stage4_api = _load_path("_stage4_api", _JEFFWAY_ROOT / "Stage4" / "api.py")
app.include_router(
    _stage4_api.create_router(
        get_context=lambda: _context,
        require_browser=_require_browser,
        login_fn=_easybop_login,
        username=settings.username,
        password=settings.password,
        timeout_ms=settings.request_timeout_ms,
        easybop_base_url=settings.easybop_base_url,
        session_path=_session_path,
        default_keep_page_open=settings.keep_page_open_after_fill,
    )
)

# Stage 5  (Stage5/api.py)
_stage5_api = _load_path("_stage5_api", _JEFFWAY_ROOT / "Stage5" / "api.py")
app.include_router(
    _stage5_api.create_router(
        get_context=lambda: _context,
        require_browser=_require_browser,
        download_scheduling_export=download_scheduling_register_export,
        login_fn=_easybop_login,
        username=settings.username,
        password=settings.password,
        timeout_ms=settings.request_timeout_ms,
        easybop_base_url=settings.easybop_base_url,
        session_path=_session_path,
        default_keep_page_open=settings.keep_page_open_after_fill,
    )
)

# Stage 6  (Stage6/api.py)
_stage6_api = _load_path("_stage6_api", _STAGE6_DIR / "api.py")
app.include_router(
    _stage6_api.create_router(
        get_context=lambda: _context,
        require_browser=_require_browser,
        save_session=_save_session,
    )
)

# Stage 7  (stage7/api.py)
_stage7_api = _load_path("_stage7_api", _JEFFWAY_ROOT / "stage7" / "api.py")
app.include_router(
    _stage7_api.create_router(
        get_context=lambda: _context,
        require_browser=_require_browser,
        save_session=_save_session,
    )
)


@app.post(
    "/create-handover-task-stage7",
    tags=["Stage 7"],
    response_model=_stage7_api.CreateHandoverTaskResponse,
    summary="Create Handover needed task (n8n alias)",
    description="Alias for `POST /stage7/create-handover-task` — used by handover_task.json.",
)
async def create_handover_task_stage7(req: _stage7_api.CreateHandoverTaskRequest):
    await _ensure_browser()
    return await _stage7_api.create_handover_task_handler(
        req,
        get_context=lambda: _context,
        require_browser=_require_browser,
        save_session=_save_session,
    )


# ──────────────────────────────────────────────────────────────────
# Pre-Works JIS Auto-Fill  (stage2/pre_works_jis/orchestrator.py)
# ──────────────────────────────────────────────────────────────────
class PreWorksJisScanRequest(BaseModel):
    contract_id: str = Field("321129", description="EasyBOP contract_id")
    dry_run: bool = Field(
        False,
        description=(
            "If true, parse the pre-works table and identify qualifying jobs "
            "but do NOT download files or write to Google Sheets."
        ),
    )
    limit: Optional[int] = Field(
        None,
        description="Max number of jobs to process (useful for testing — e.g. limit=2).",
    )
    openai_api_key: Optional[str] = Field(
        None,
        description="Override OpenAI API key (falls back to OPENAI_API_KEY in .env).",
    )
    sheets_api_key: Optional[str] = Field(
        None,
        description="Google Sheets API key for direct append (falls back to GOOGLE_SHEETS_API_KEY in .env).",
    )
    n8n_append_jis_url: Optional[str] = Field(
        None,
        description="n8n webhook URL that appends a row to the jis tab (alternative to Sheets API key).",
    )
    n8n_append_appt_url: Optional[str] = Field(
        None,
        description="n8n webhook URL that appends a row to the appointment tab.",
    )


@app.post(
    "/pre-works/scan-and-queue-jis",
    tags=["Pre-Works JIS"],
    summary=(
        "Scan EasyBOP Pre Works report → extract JIS fields via GPT → "
        "write to Google Sheets jis + appointment tabs → jis_filler picks them up"
    ),
)
async def pre_works_scan_and_queue_jis(req: PreWorksJisScanRequest):
    """
    Full pipeline:

    1. Navigate to `pre_works.php?contract_id=...` via Playwright (renders jqGrid)
    2. Parse the table — find rows where asbestos=green, JIS=red, WO=green
    3. Download each Works Order .msg file (authenticated HTTP session)
    4. Call GPT-4.1-mini to extract JIS Q1.1–Q5.2 + appointment slots
    5. Check for duplicates in the jis tab
    6. Append new rows to jis tab (jis_uploaded=false) + appointment tab
    7. Return a JSON summary

    After this runs, trigger `jis_filler` (schedule or webhook) to fill EasyBOP.
    """
    import asyncio as _asyncio
    import sys as _sys

    _stage2_dir = _JEFFWAY_ROOT / "stage2"
    for p in [str(_JEFFWAY_ROOT), str(_stage2_dir)]:
        if p not in _sys.path:
            _sys.path.insert(0, p)

    try:
        from stage2.pre_works_jis.orchestrator import run_pipeline  # type: ignore
        from stage2.pre_works_jis.msg_downloader import fetch_pre_works_table_html_playwright  # type: ignore
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="stage2.pre_works_jis module not found — check server logs."
        )

    try:
        # Step 1: Render the jqGrid table via Playwright (async, runs on event loop)
        _require_browser()
        logger.info("pre-works: fetching jqGrid HTML via Playwright (contract_id=%s)", req.contract_id)
        html = await fetch_pre_works_table_html_playwright(
            _context,
            req.contract_id,
            timeout_ms=settings.request_timeout_ms * 2,
        )
        logger.info("pre-works: rendered HTML %s chars", len(html))

        # Steps 2–6: CPU-bound pipeline (GPT + HTTP downloads), run in thread
        result = await _asyncio.get_event_loop().run_in_executor(
            None,
            lambda: run_pipeline(
                contract_id=req.contract_id,
                openai_api_key=req.openai_api_key or None,
                dry_run=req.dry_run,
                html_override=html,
                limit=req.limit,
                sheets_api_key=req.sheets_api_key or None,
                n8n_append_jis_url=req.n8n_append_jis_url or None,
                n8n_append_appt_url=req.n8n_append_appt_url or None,
            ),
        )
        return {
            "success": result["failed"] == 0,
            **result,
        }
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception:
        logger.exception("pre-works/scan-and-queue-jis")
        raise HTTPException(status_code=500, detail="Failed — see server logs.")



# ──────────────────────────────────────────────────────────────────
# Pre-Works: fetch rendered jqGrid HTML (for n8n workflow)
# ──────────────────────────────────────────────────────────────────
class FetchPreWorksTableRequest(BaseModel):
    contract_id: str = Field("321129", description="EasyBOP contract_id")
    timeout_ms:  int  = Field(120000,  description="Playwright wait timeout (ms) per step")
    max_pages:   int  = Field(200,     description="Max jqGrid pages to scrape via #next_list_pager")


@app.post(
    "/pre-works/fetch-table-html",
    tags=["Pre-Works JIS"],
    summary="Render pre_works.php via Playwright and return jqGrid HTML for n8n parsing",
)
async def pre_works_fetch_table_html(req: FetchPreWorksTableRequest):
    """
    Navigates to pre_works.php in the shared Playwright browser, waits for
    the jqGrid to render, then returns the HTML so the n8n workflow can
    parse it with a Code node (no server-side Python parsing needed).
    """
    import sys as _sys
    _stage2_dir = _JEFFWAY_ROOT / "stage2"
    if str(_JEFFWAY_ROOT) not in _sys.path:
        _sys.path.insert(0, str(_JEFFWAY_ROOT))

    try:
        from stage2.pre_works_jis.msg_downloader import fetch_pre_works_table_html_playwright_meta  # type: ignore
    except ImportError:
        raise HTTPException(status_code=500, detail="pre_works_jis module not found")

    try:
        _require_browser()
        result = await fetch_pre_works_table_html_playwright_meta(
            _context,
            req.contract_id,
            timeout_ms=req.timeout_ms,
            max_pages=req.max_pages,
        )
        html = result.get("html") or ""
        return {
            "success": True,
            "html": html,
            "length": len(html),
            "contract_id": req.contract_id,
            "pages_scraped": result.get("pages_scraped", 0),
            "rows_total": result.get("rows_total", 0),
            "records_total": result.get("records_total"),
        }
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception:
        logger.exception("pre-works/fetch-table-html")
        raise HTTPException(status_code=500, detail="Failed — see server logs.")


class ParseAsbestosPdfRequest(BaseModel):
    pdf_base64: str = Field(..., description="Raw PDF bytes, base64-encoded")
    address: str = Field("", description="Property address (for summary)")
    uprn: str = Field("", description="UPRN (for summary)")


@app.post(
    "/pre-works/parse-asbestos-pdf",
    tags=["Pre-Works JIS"],
    summary="Extract ACM register text from an asbestos survey PDF",
)
async def pre_works_parse_asbestos_pdf(req: ParseAsbestosPdfRequest):
    """Parse asbestos survey PDF text for use in JIS + appointment AI prompts."""
    import base64 as _b64
    import sys as _sys

    _stage2_dir = _JEFFWAY_ROOT / "stage2"
    for p in [str(_JEFFWAY_ROOT), str(_stage2_dir)]:
        if p not in _sys.path:
            _sys.path.insert(0, p)

    try:
        from stage2.pre_works_jis.asbestos_pdf_parser import parse_asbestos_pdf_bytes  # type: ignore
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="stage2.pre_works_jis.asbestos_pdf_parser not found — check server logs.",
        )

    try:
        b64 = (req.pdf_base64 or "").strip()
        if b64.startswith("data:") and "," in b64:
            b64 = b64.split(",", 1)[1]
        raw = _b64.b64decode(b64, validate=False)
        if not raw:
            raise HTTPException(status_code=422, detail="pdf_base64 decoded to empty bytes")
        result = parse_asbestos_pdf_bytes(raw, address=req.address, uprn=req.uprn)
        return {"success": True, **result}
    except HTTPException:
        raise
    except Exception:
        logger.exception("pre-works/parse-asbestos-pdf")
        raise HTTPException(status_code=500, detail="Failed — see server logs.")


class ExtractMsgTextRequest(BaseModel):
    msg_base64: str = Field(..., description="Raw .msg / .eml bytes, base64-encoded")
    filename: str = Field("works_order.msg", description="Original filename hint for parser")


class ExtractMsgTextBatchItem(BaseModel):
    corf: str = Field(..., description="Client order reference for result matching")
    msg_base64: str = Field(..., description="Base64-encoded .msg / .eml bytes")
    filename: str = Field("works_order.msg", description="Original filename hint")


class ExtractMsgTextBatchRequest(BaseModel):
    items: List[ExtractMsgTextBatchItem] = Field(default_factory=list)


def _decode_msg_bytes(msg_base64: str, *, corf: str = "") -> bytes:
    import base64 as _b64

    b64 = (msg_base64 or "").strip()
    if b64.startswith("data:") and "," in b64:
        b64 = b64.split(",", 1)[1]
    try:
        raw = _b64.b64decode(b64, validate=False)
    except Exception as exc:
        label = f" for CORF {corf}" if corf else ""
        raise HTTPException(status_code=400, detail=f"Invalid base64{label}: {exc}") from exc
    if not raw:
        label = f" for CORF {corf}" if corf else ""
        raise HTTPException(status_code=422, detail=f"msg_base64 decoded to empty bytes{label}")
    head = raw[:512].lower()
    if head.startswith(b"<!doctype") or head.startswith(b"<html") or b"please log in" in head:
        label = f" for CORF {corf}" if corf else ""
        raise HTTPException(
            status_code=422,
            detail=f"EasyBOP returned HTML (session expired?){label}",
        )
    return raw


def _parse_msg_text_result(raw: bytes, filename: str) -> dict:
    import sys as _sys

    _stage2_dir = _JEFFWAY_ROOT / "stage2"
    for p in [str(_JEFFWAY_ROOT), str(_stage2_dir)]:
        if p not in _sys.path:
            _sys.path.insert(0, p)

    try:
        from stage2.pre_works_jis.works_order_extractor import parse_msg_text_only  # type: ignore
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="stage2.pre_works_jis.works_order_extractor not found — check server logs.",
        )

    result = parse_msg_text_only(raw, filename or "works_order.msg")
    body = (result.get("body_text") or "").strip()
    if not body:
        raise HTTPException(status_code=422, detail="Could not extract text from works order file")
    if raw[:4] == b"\xd0\xcf\x11\xe0" and not (result.get("subject") or "").strip():
        raise HTTPException(
            status_code=422,
            detail=(
                "Outlook .msg was not parsed — msg_base64 may be truncated "
                "(check n8n POST body uses JSON.stringify, not {{ }} templates)"
            ),
        )
    return {"success": True, **result}


@app.post(
    "/pre-works/extract-msg-text",
    tags=["Pre-Works JIS"],
    summary="Parse Outlook .msg / .eml file to plain text for JIS AI extraction",
)
async def pre_works_extract_msg_text(req: ExtractMsgTextRequest):
    """Extract subject, from, date, and body from a BCC works order .msg file."""
    try:
        raw = _decode_msg_bytes(req.msg_base64)
        return _parse_msg_text_result(raw, req.filename or "works_order.msg")
    except HTTPException:
        raise
    except Exception:
        logger.exception("pre-works/extract-msg-text")
        raise HTTPException(status_code=500, detail="Failed — see server logs.")


@app.post(
    "/pre-works/extract-msg-text-batch",
    tags=["Pre-Works JIS"],
    summary="Parse multiple .msg files in one request (sequential, single HTTP round-trip)",
)
async def pre_works_extract_msg_text_batch(req: ExtractMsgTextBatchRequest):
    """Parse many works-order .msg files without n8n firing one HTTP call per item."""
    items = req.items or []
    if not items:
        return {"success": True, "total": 0, "parsed": 0, "errors": 0, "results": []}

    results: List[dict] = []
    parsed = 0
    errors = 0

    for item in items:
        corf = str(item.corf or "").strip()
        filename = item.filename or "works_order.msg"
        try:
            raw = _decode_msg_bytes(item.msg_base64, corf=corf)
            parsed_item = _parse_msg_text_result(raw, filename)
            results.append({"corf": corf, "filename": filename, **parsed_item})
            parsed += 1
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            results.append(
                {
                    "corf": corf,
                    "filename": filename,
                    "success": False,
                    "error": detail,
                }
            )
            errors += 1
        except Exception as exc:
            logger.exception("extract-msg-text-batch CORF %s", corf)
            results.append(
                {
                    "corf": corf,
                    "filename": filename,
                    "success": False,
                    "error": str(exc),
                }
            )
            errors += 1

    return {
        "success": errors == 0,
        "total": len(items),
        "parsed": parsed,
        "errors": errors,
        "results": results,
    }


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.api_port,
        reload=settings.reload_on_change,
        loop="asyncio",
    )


