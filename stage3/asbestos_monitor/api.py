from __future__ import annotations

import base64
import importlib.util
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, List, Optional

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from playwright.async_api import BrowserContext
from pydantic import BaseModel, Field, ValidationError

_here = Path(__file__).resolve().parent


def _load_local_module(name: str, filename: str):
    path = _here / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load {filename} from {_here}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_automation = _load_local_module("_asbestos_monitor_automation", "automation.py")
_config = _load_local_module("_asbestos_monitor_config", "config.py")

_unsuspend_automation = _load_local_module("_asbestos_unsuspend_automation", "unsuspend_automation.py")
_suspend_automation = _load_local_module("_asbestos_suspend_automation", "suspend_automation.py")
_bulk_note_automation = _load_local_module("_asbestos_bulk_note_automation", "bulk_note_automation.py")
_task_automation = _load_local_module("_asbestos_task_automation", "task_automation.py")
_upload_pdf_automation = _load_local_module("_asbestos_upload_pdf_automation", "upload_pdf_automation.py")

scrape_missing_asbestos = _automation.scrape_missing_asbestos
scrape_ready_for_unsuspend = _automation.scrape_ready_for_unsuspend

unsuspend_addresses = _unsuspend_automation.unsuspend_addresses
unsuspend_corfs = _unsuspend_automation.unsuspend_corfs
suspend_addresses = _suspend_automation.suspend_addresses
bulk_add_notes = _bulk_note_automation.bulk_add_notes
add_asbestos_tasks = _task_automation.add_asbestos_tasks
upload_asbestos_pdf = _upload_pdf_automation.upload_asbestos_pdf
upload_asbestos_pdf_batch = _upload_pdf_automation.upload_asbestos_pdf_batch
settings = _config.settings
resolve_session_path = _config.resolve_session_path

logger = logging.getLogger("asbestos.api")


# ---------------------------------------------------------------------------
# Fetch-missing models (existing)
# ---------------------------------------------------------------------------

class MissingAsbestosItem(BaseModel):
    row_id: Optional[str] = None
    uprn: Optional[str] = None
    address: Optional[str] = None
    corf: Optional[str] = Field(
        None,
        description="Client Order Reference (e.g. 38865/1) from pre_works grid",
    )


class FetchMissingRequest(BaseModel):
    contract_id: Optional[str] = Field(
        None,
        description="EasyBOP contract_id (default from ASB_CONTRACT_ID / JIS_CONTRACT_ID / 321129 in .env)",
    )


class FetchMissingResponse(BaseModel):
    success: bool
    total: int
    contract_id: str
    properties: List[MissingAsbestosItem]
    message: str


# ---------------------------------------------------------------------------
# Unsuspend models
# ---------------------------------------------------------------------------

class UnsuspendRequest(BaseModel):
    corfs: List[str] = Field(
        default_factory=list,
        description=(
            "Preferred: Client Order References to match on the works index grid "
            "(column `list_client_order_reference`). Paginates through all jqGrid pages."
        ),
    )
    addresses: List[str] = Field(
        default_factory=list,
        description=(
            "Legacy: addresses to match on page 1 only. "
            "Ignored when `corfs` is non-empty."
        ),
    )
    contract_id: Optional[str] = Field(
        None,
        description="EasyBOP contract_id (defaults to ASB_CONTRACT_ID / 321129 in .env)",
    )
    keep_page_open: Optional[bool] = Field(
        None,
        description="If true, leave the browser tab open after completion.",
    )


def _unsuspend_match_lists(req: UnsuspendRequest) -> tuple[List[str], List[str]]:
    corfs = [str(c).strip() for c in (req.corfs or []) if c and str(c).strip()]
    addresses = [str(a).strip() for a in (req.addresses or []) if a and str(a).strip()]
    if not corfs and not addresses:
        raise HTTPException(
            status_code=422,
            detail="Provide at least one CORF in `corfs` or address in `addresses`.",
        )
    return corfs, addresses


class UnsuspendResponse(BaseModel):
    total_requested: int
    total_checked: int
    checked: List[str]
    not_found: List[str]
    contract_id: str
    success: bool
    message: str


# ---------------------------------------------------------------------------
# Suspend models
# ---------------------------------------------------------------------------

class SuspendRequest(BaseModel):
    addresses: List[str] = Field(
        ...,
        min_length=1,
        description=(
            "List of addresses to bulk suspend. Each address will be matched against rows "
            "in the planned-works grid, its checkbox will be ticked, then the "
            "**Bulk Suspend Works** button is clicked and 'Awaiting asbestos info' is selected.\n\n"
            "**Swagger test body:**\n"
            "`{\"addresses\": [\"71 Ashton Rise, Ashton Vale\"]}`"
        ),
    )
    contract_id: Optional[str] = Field(
        None,
        description="EasyBOP contract_id (defaults to ASB_CONTRACT_ID / 321129 in .env)",
    )
    keep_page_open: Optional[bool] = Field(
        None,
        description="If true, leave the browser tab open after completion.",
    )


class SuspendResponse(BaseModel):
    total_requested: int
    total_checked: int
    checked: List[str]
    not_found: List[str]
    contract_id: str
    success: bool
    message: str


# ---------------------------------------------------------------------------
# Bulk-note models (bulk_notes.php)
# ---------------------------------------------------------------------------

class BulkAddNotesRequest(BaseModel):
    corfs: List[str] = Field(
        default_factory=list,
        description=(
            "Preferred: Client Order References to match on bulk_notes.php "
            "(column Client Order Reference). One CORF per job even when multiple UPRNs exist."
        ),
    )
    addresses: List[str] = Field(
        default_factory=list,
        description=(
            "Legacy: addresses to match against the bulk_notes.php grid. "
            "Ignored when `corfs` is non-empty."
        ),
    )
    contract_id: Optional[str] = Field(
        None,
        description="EasyBOP contract_id (defaults to ASB_CONTRACT_ID / 321129 in .env)",
    )
    note_text: Optional[str] = Field(
        None,
        description="Custom note body. Leave blank to use the mode-appropriate default.",
    )
    keep_page_open: Optional[bool] = Field(
        None,
        description="If true, leave the browser tab open after completion.",
    )


def _bulk_notes_match_lists(req: BulkAddNotesRequest) -> tuple[List[str], List[str]]:
    corfs = [str(c).strip() for c in (req.corfs or []) if c and str(c).strip()]
    addresses = [str(a).strip() for a in (req.addresses or []) if a and str(a).strip()]
    if not corfs and not addresses:
        raise HTTPException(
            status_code=400,
            detail="Provide at least one CORF in `corfs` or address in `addresses`.",
        )
    return corfs, addresses


class BulkAddNotesResponse(BaseModel):
    total_requested: int
    total_checked: int
    checked: List[str]
    not_found: List[str]
    contract_id: str
    success: bool
    message: str

# ---------------------------------------------------------------------------
# Add-tasks models
# ---------------------------------------------------------------------------

class AddTaskItem(BaseModel):
    works_id: Optional[str] = Field(
        None,
        description="EasyBOP works_id — navigates directly to works_details.php?tab_nav_item=tasks",
    )
    address: str = Field("", description="Address (for logging / fallback if works_id missing)")
    uprn: Optional[str] = Field(None, description="UPRN for result matching in n8n")


class AddTasksRequest(BaseModel):
    addresses: List[str] = Field(
        default_factory=list,
        description="Legacy: list of addresses to search on the index",
    )
    items: List[AddTaskItem] = Field(
        default_factory=list,
        description=(
            "Preferred: list of {works_id, address, uprn}. "
            "When works_id is set, opens "
            "works_details.php?works_id=…&tab_nav_item=tasks directly."
        ),
    )
    contract_id: Optional[str] = Field(
        None,
        description="EasyBOP contract_id (default from ASB_CONTRACT_ID / .env)",
    )
    description: str = Field(
        "Asbestos survey required before works commence.",
        description="Text to enter in the task description textarea.",
    )
    complete_by_date: str = Field(
        "",
        description="Complete By Date (DD/MM/YYYY). Defaults to today if empty.",
    )
    keep_page_open: Optional[bool] = Field(
        None,
        description="If true, do not close the browser tab after completion.",
    )


class AddTaskResult(BaseModel):
    address: str = ""
    works_id: Optional[str] = None
    uprn: Optional[str] = None
    success: bool
    message: str
    already_created: bool = False
    tasks_count_before: Optional[int] = Field(
        None,
        description="Tasks counter shown on the tab before opening the dialog.",
    )
    tasks_count_after: Optional[int] = Field(
        None,
        description="Tasks counter shown on the tab after saving the task.",
    )


class AddTasksResponse(BaseModel):
    success: bool = True
    total_processed: int
    total_success: int
    total_failed: int
    contract_id: str
    results: List[AddTaskResult]


def _tasks_error_results(addresses: List[str], contract_id: str, msg: str) -> AddTasksResponse:
    results = [
        AddTaskResult(address=addr, success=False, message=msg)
        for addr in addresses
    ]
    return AddTasksResponse(
        success=False,
        total_processed=len(results),
        total_success=0,
        total_failed=len(results),
        contract_id=contract_id,
        results=results,
    )


# ---------------------------------------------------------------------------
# Upload-PDF response model
# ---------------------------------------------------------------------------

class UploadPdfResponse(BaseModel):
    corf: str
    pdf_filename: str
    success: bool
    uploaded: bool = False
    already_uploaded: bool = False
    message: str
    error: Optional[str] = None
    works_id: Optional[str] = None


class UploadPdfBatchItem(BaseModel):
    corf: str = Field(..., description="Client Order Reference")
    pdf_filename: str = Field(..., description="PDF file name")
    pdf_base64: str = Field(..., description="Base64-encoded PDF bytes")
    works_id: Optional[str] = Field(
        None, description="EasyBOP works_id — opens works_details WI tab directly"
    )


class UploadPdfBatchRequest(BaseModel):
    items: List[UploadPdfBatchItem] = Field(..., description="CORFs to process in one browser session")
    contract_id: str = Field("321129", description="EasyBOP contract ID")
    timeout_ms: int = Field(45_000, description="Playwright timeout per action (ms)")
    use_works_id_navigation: bool = Field(
        False,
        description="When true (or when any item has works_id), navigate via works_details.php per row",
    )


class UploadPdfBatchResponse(BaseModel):
    success: bool
    total: int = 0
    uploaded: int = 0
    already_uploaded: int = 0
    errors: int = 0
    results: List[UploadPdfResponse] = Field(default_factory=list)


def _parse_pdf_batch_payload(raw: bytes) -> UploadPdfBatchRequest:
    if not raw:
        raise HTTPException(status_code=400, detail="Empty request body")
    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc}") from exc
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=422,
                detail="JSON body is a quoted string, not an object — check n8n POST body settings",
            ) from exc
    try:
        return UploadPdfBatchRequest.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc


def _decode_pdf_items(items: List[UploadPdfBatchItem]) -> list:
    decoded = []
    for item in items:
        try:
            pdf_bytes = base64.b64decode(item.pdf_base64)
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid base64 for CORF {item.corf}: {exc}",
            )
        if len(pdf_bytes) == 0:
            raise HTTPException(
                status_code=400,
                detail=f"pdf_base64 decoded to 0 bytes for CORF {item.corf}",
            )
        if not pdf_bytes.startswith(b"%PDF"):
            raise HTTPException(
                status_code=400,
                detail=f"File for CORF {item.corf} is not a PDF ({len(pdf_bytes)} bytes)",
            )
        decoded.append({
            "corf": item.corf,
            "pdf_filename": item.pdf_filename,
            "pdf_bytes": pdf_bytes,
            "works_id": str(item.works_id or "").strip() or None,
        })
    return decoded


def _pdf_batch_error_results(decoded: list, msg: str) -> UploadPdfBatchResponse:
    return UploadPdfBatchResponse(
        success=False,
        total=len(decoded),
        uploaded=0,
        already_uploaded=0,
        errors=len(decoded),
        results=[
            UploadPdfResponse(
                success=False,
                corf=item["corf"],
                pdf_filename=item["pdf_filename"],
                error=msg,
                message=msg,
            )
            for item in decoded
        ],
    )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------

def create_router(
    *,
    get_context: Callable[[], BrowserContext],
    require_browser: Callable[[], None],
) -> APIRouter:
    router = APIRouter(tags=["Asbestos"])

    # ------------------------------------------------------------------
    # Existing endpoint: fetch missing asbestos rows from the report grid
    # ------------------------------------------------------------------

    @router.post("/fetch-missing-asbestos", response_model=FetchMissingResponse)
    async def fetch_missing_asbestos(req: Optional[FetchMissingRequest] = None):
        require_browser()
        req = req or FetchMissingRequest()
        cid = (req.contract_id or settings.contract_id).strip()
        context = get_context()

        page = await context.new_page()
        try:
            async def _save_session() -> None:
                session_path = resolve_session_path()
                session_path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(session_path))

            results = await scrape_missing_asbestos(
                page,
                contract_id=cid,
                username=settings.username,
                password=settings.password,
                timeout_ms=settings.request_timeout_ms,
                after_relogin=_save_session,
            )

            items = [MissingAsbestosItem(**r) for r in results]
            return FetchMissingResponse(
                success=True,
                total=len(items),
                contract_id=cid,
                properties=items,
                message=f"Found {len(items)} propert{'y' if len(items) == 1 else 'ies'} with missing asbestos report.",
            )
        except RuntimeError as e:
            raise HTTPException(status_code=401, detail=str(e))
        except Exception:
            logger.exception("fetch-missing-asbestos")
            raise HTTPException(status_code=500, detail="Internal error — see server logs.")
        finally:
            keep_open = getattr(settings, "keep_page_open_after_fill", True)
            if not keep_open:
                await page.close()
            else:
                logger.info("fetch-missing: keep_page_open is true, leaving tab open.")



    # ------------------------------------------------------------------
    # Fetch rows ready for un-suspend (green asbestos notes + suspended status)
    # ------------------------------------------------------------------

    @router.post("/fetch-ready-for-unsuspend", response_model=FetchMissingResponse)
    async def fetch_ready_for_unsuspend(req: Optional[FetchMissingRequest] = None):
        require_browser()
        req = req or FetchMissingRequest()
        cid = (req.contract_id or settings.contract_id).strip()
        context = get_context()

        page = await context.new_page()
        try:
            async def _save_session() -> None:
                session_path = resolve_session_path()
                session_path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(session_path))

            results = await scrape_ready_for_unsuspend(
                page,
                contract_id=cid,
                username=settings.username,
                password=settings.password,
                timeout_ms=settings.request_timeout_ms,
                after_relogin=_save_session,
            )

            items = [MissingAsbestosItem(**r) for r in results]
            return FetchMissingResponse(
                success=True,
                total=len(items),
                contract_id=cid,
                properties=items,
                message=(
                    f"Found {len(items)} propert{'y' if len(items) == 1 else 'ies'} "
                    "with green asbestos notes and suspended admin status."
                ),
            )
        except RuntimeError as e:
            raise HTTPException(status_code=401, detail=str(e))
        except Exception:
            logger.exception("fetch-ready-for-unsuspend")
            raise HTTPException(status_code=500, detail="Internal error — see server logs.")
        finally:
            keep_open = getattr(settings, "keep_page_open_after_fill", True)
            if not keep_open:
                await page.close()
            else:
                logger.info("fetch-ready-for-unsuspend: keep_page_open is true, leaving tab open.")

    # ------------------------------------------------------------------
    # New endpoint: unsuspend planned works for a list of addresses
    # ------------------------------------------------------------------

    @router.post(
        "/unsuspend-works",
        response_model=UnsuspendResponse,
        summary="Bulk un-suspend planned works by CORF on EasyBOP",
        description=(
            "Navigates to `z_works/index.php`, paginates the jqGrid via `#next_list_pager`, "
            "ticks each row matching **Client Order Reference** (`corfs`), clicks "
            "**Bulk Un-suspend Works** (`#btn_bulk_unsuspend`), and accepts the native "
            "**Un-Suspend Works?** confirm dialog.\n\n"
            "**Swagger test body:**\n"
            "```json\n"
            '{"corfs": ["121044/1", "99847/1"]}\n'
            "```"
        ),
    )
    async def unsuspend_works_endpoint(req: UnsuspendRequest):
        require_browser()
        cid = (req.contract_id or settings.contract_id).strip()
        corfs, addresses = _unsuspend_match_lists(req)
        context = get_context()

        page = await context.new_page()
        try:
            async def _save_session() -> None:
                session_path = resolve_session_path()
                session_path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(session_path))

            if corfs:
                result: dict = await unsuspend_corfs(
                    page,
                    corfs=corfs,
                    contract_id=cid,
                    username=settings.username,
                    password=settings.password,
                    timeout_ms=settings.request_timeout_ms,
                    after_relogin=_save_session,
                )
            else:
                result = await unsuspend_addresses(
                    page,
                    addresses=addresses,
                    contract_id=cid,
                    username=settings.username,
                    password=settings.password,
                    timeout_ms=settings.request_timeout_ms,
                    after_relogin=_save_session,
                )

            return UnsuspendResponse(
                total_requested=result["total_requested"],
                total_checked=result["total_checked"],
                checked=result["checked"],
                not_found=result["not_found"],
                contract_id=cid,
                success=result["success"],
                message=result["message"],
            )

        except RuntimeError as e:
            raise HTTPException(status_code=401, detail=str(e))
        except Exception:
            logger.exception("unsuspend-works")
            raise HTTPException(status_code=500, detail="Internal error — see server logs.")
        finally:
            keep_open = req.keep_page_open
            if keep_open is None:
                keep_open = getattr(settings, "keep_page_open_after_fill", True)
            if not keep_open:
                await page.close()
            else:
                logger.info("unsuspend: keep_page_open is true, leaving tab open.")

    # ------------------------------------------------------------------
    # New endpoint: suspend planned works for a list of addresses
    # ------------------------------------------------------------------

    @router.post(
        "/suspend-works",
        response_model=SuspendResponse,
        summary="Bulk suspend planned works by address on EasyBOP",
        description=(
            "Navigates to the planned-works index, checks the checkbox for each matching address, "
            "clicks **Bulk Suspend Works** (`#btn_bulk_suspend`), selects reason 1466 ('Awaiting asbestos info'), "
            "clicks **Suspend** (`#btn_dialog_suspend_works`), and accepts the browser **Suspend Works?** confirm.\n\n"
            "**Swagger test body:**\n"
            "```json\n"
            '{"addresses": ["71 Ashton Rise, Ashton Vale"]}\n'
            "```"
        ),
    )
    async def suspend_works_endpoint(req: SuspendRequest):
        require_browser()
        cid = (req.contract_id or settings.contract_id).strip()
        context = get_context()

        page = await context.new_page()
        try:
            async def _save_session() -> None:
                session_path = resolve_session_path()
                session_path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(session_path))

            result: dict = await suspend_addresses(
                page,
                addresses=req.addresses,
                contract_id=cid,
                username=settings.username,
                password=settings.password,
                timeout_ms=settings.request_timeout_ms,
                after_relogin=_save_session,
            )

            return SuspendResponse(
                total_requested=result["total_requested"],
                total_checked=result["total_checked"],
                checked=result["checked"],
                not_found=result["not_found"],
                contract_id=cid,
                success=result["success"],
                message=result["message"],
            )

        except RuntimeError as e:
            raise HTTPException(status_code=401, detail=str(e))
        except Exception:
            logger.exception("suspend-works")
            raise HTTPException(status_code=500, detail="Internal error — see server logs.")
        finally:
            keep_open = req.keep_page_open
            if keep_open is None:
                keep_open = getattr(settings, "keep_page_open_after_fill", True)
            if not keep_open:
                await page.close()
            else:
                logger.info("suspend: keep_page_open is true, leaving tab open.")

    # ------------------------------------------------------------------
    # New endpoint: bulk add "No Asbestos with order" notes (bulk_notes.php)
    # ------------------------------------------------------------------

    @router.post(
        "/bulk-add-notes",
        response_model=BulkAddNotesResponse,
        summary="Bulk add 'No Asbestos with order' notes via bulk_notes.php",
        description=(
            "Navigates to `bulk_notes.php`, ticks rows by **Client Order Reference** (`corfs`) "
            "or legacy address match, clicks **Add Notes to Selected Works**, selects category "
            "**No Asbestos with order** (1716), fills the note text, clicks **Add Note** "
            "(`#btn_save_changes`), accepts the browser **Save changes?** confirm, then closes.\n\n"
            "**Swagger test body:**\n"
            "```json\n"
            '{"corfs": ["38865/1"]}\n'
            "```"
        ),
    )
    async def bulk_add_notes_no_asbestos_endpoint(req: BulkAddNotesRequest):
        require_browser()
        cid = (req.contract_id or settings.contract_id).strip()
        corfs, addresses = _bulk_notes_match_lists(req)
        context = get_context()

        page = await context.new_page()
        try:
            async def _save_session() -> None:
                session_path = resolve_session_path()
                session_path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(session_path))

            result: dict = await bulk_add_notes(
                page,
                corfs=corfs or None,
                addresses=addresses or None,
                contract_id=cid,
                mode="no_asbestos",
                note_text=req.note_text,
                username=settings.username,
                password=settings.password,
                timeout_ms=settings.request_timeout_ms,
                after_relogin=_save_session,
            )

            return BulkAddNotesResponse(
                total_requested=result["total_requested"],
                total_checked=result["total_checked"],
                checked=result["checked"],
                not_found=result["not_found"],
                contract_id=cid,
                success=result["success"],
                message=result["message"],
            )

        except RuntimeError as e:
            raise HTTPException(status_code=401, detail=str(e))
        except Exception:
            logger.exception("bulk-add-notes")
            raise HTTPException(status_code=500, detail="Internal error — see server logs.")
        finally:
            keep_open = req.keep_page_open
            if keep_open is None:
                keep_open = getattr(settings, "keep_page_open_after_fill", True)
            if not keep_open:
                await page.close()
            else:
                logger.info("bulk-add-notes: keep_page_open is true, leaving tab open.")

    # ------------------------------------------------------------------
    # New endpoint: bulk add "Asbestos Report Ordered" notes (bulk_notes.php)
    # ------------------------------------------------------------------

    @router.post(
        "/bulk-add-notes-report-ordered",
        response_model=BulkAddNotesResponse,
        summary="Bulk add 'Asbestos Report Ordered' notes via bulk_notes.php",
        description=(
            "Same as `/bulk-add-notes` but selects category "
            "**Asbestos Report Ordered** (1758), clicks **Add Note** (`#btn_save_changes`), "
            "accepts **Save changes?**, then closes. "
            "Use this after a survey has been commissioned to update the works record.\n\n"
            "**Swagger test body:**\n"
            "```json\n"
            '{"corfs": ["38865/1"]}\n'
            "```"
        ),
    )
    async def bulk_add_notes_report_ordered_endpoint(req: BulkAddNotesRequest):
        require_browser()
        cid = (req.contract_id or settings.contract_id).strip()
        corfs, addresses = _bulk_notes_match_lists(req)
        context = get_context()

        page = await context.new_page()
        try:
            async def _save_session() -> None:
                session_path = resolve_session_path()
                session_path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(session_path))

            result: dict = await bulk_add_notes(
                page,
                corfs=corfs or None,
                addresses=addresses or None,
                contract_id=cid,
                mode="report_ordered",
                note_text=req.note_text,
                username=settings.username,
                password=settings.password,
                timeout_ms=settings.request_timeout_ms,
                after_relogin=_save_session,
            )

            return BulkAddNotesResponse(
                total_requested=result["total_requested"],
                total_checked=result["total_checked"],
                checked=result["checked"],
                not_found=result["not_found"],
                contract_id=cid,
                success=result["success"],
                message=result["message"],
            )

        except RuntimeError as e:
            raise HTTPException(status_code=401, detail=str(e))
        except Exception:
            logger.exception("bulk-add-notes-report-ordered")
            raise HTTPException(status_code=500, detail="Internal error — see server logs.")
        finally:
            keep_open = req.keep_page_open
            if keep_open is None:
                keep_open = getattr(settings, "keep_page_open_after_fill", True)
            if not keep_open:
                await page.close()
            else:
                logger.info("bulk-add-notes-report-ordered: keep_page_open is true, leaving tab open.")

    # ------------------------------------------------------------------
    # New endpoint: add "Asbestos survey needed" task for a list of addresses
    # ------------------------------------------------------------------

    @router.post(
        "/add-asbestos-tasks",
        response_model=AddTasksResponse,
        summary="Open 'Asbestos survey needed' task dialog per address on EasyBOP",
        description=(
            "For each item (prefer `items` with works_id) or legacy `addresses` list: "
            "opens the work order **Tasks** tab, clicks **Add Task**, selects "
            "**Asbestos survey needed** (205), fills description, Issued To, priority, "
            "complete-by date, then **Save Changes**.\n\n"
            "With `works_id`, navigates to "
            "`works_details.php?works_id=…&tab_nav_item=tasks` (no address search).\n\n"
            "**Swagger test body:**\n"
            "```json\n"
            '{"items": [{"works_id": "926607", "address": "18 Gatehouse Close", "uprn": "1280315"}]}\n'
            "```"
        ),
    )
    async def add_asbestos_tasks_endpoint(req: AddTasksRequest):
        cid = (req.contract_id or settings.contract_id).strip()
        items = [it.model_dump() for it in (req.items or []) if it]
        addresses = [a.strip() for a in (req.addresses or []) if a and str(a).strip()]

        if not items and not addresses:
            return AddTasksResponse(
                success=True,
                total_processed=0,
                total_success=0,
                total_failed=0,
                contract_id=cid,
                results=[],
            )

        label_list = items or [{"address": a} for a in addresses]

        try:
            require_browser()
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            return _tasks_error_results(
                [str(x.get("address") or x.get("works_id") or "") for x in label_list],
                cid,
                detail,
            )

        context = get_context()
        page = await context.new_page()
        try:
            async def _save_session() -> None:
                session_path = resolve_session_path()
                session_path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(session_path))

            try:
                raw_results: List[dict] = await add_asbestos_tasks(
                    page,
                    addresses=addresses if not items else None,
                    items=items if items else None,
                    contract_id=cid,
                    description=req.description,
                    complete_by_date=req.complete_by_date,
                    username=settings.username,
                    password=settings.password,
                    timeout_ms=settings.request_timeout_ms,
                    after_relogin=_save_session,
                    use_works_id_navigation=bool(items),
                )
            except RuntimeError as e:
                return _tasks_error_results(
                    [str(x.get("address") or x.get("works_id") or "") for x in label_list],
                    cid,
                    str(e),
                )
            except Exception as exc:
                logger.exception("add-asbestos-tasks unhandled error")
                return _tasks_error_results(
                    [str(x.get("address") or x.get("works_id") or "") for x in label_list],
                    cid,
                    str(exc),
                )

            try:
                if any(r.get("success") for r in raw_results):
                    await _save_session()
            except Exception:
                logger.warning("add-asbestos-tasks: session save failed after batch", exc_info=True)

            uprn_by_wid = {
                str(it.works_id).strip(): str(it.uprn or "").strip()
                for it in (req.items or [])
                if str(it.works_id or "").strip()
            }
            uprn_by_addr = {
                str(it.address or "").strip().lower(): str(it.uprn or "").strip()
                for it in (req.items or [])
                if str(it.address or "").strip()
            }
            merged: List[dict] = []
            for r in raw_results:
                row = dict(r)
                if not row.get("uprn"):
                    wid = str(row.get("works_id") or "").strip()
                    addr = str(row.get("address") or "").strip().lower()
                    row["uprn"] = uprn_by_wid.get(wid) or uprn_by_addr.get(addr) or None
                merged.append(row)

            result_models = [AddTaskResult(**r) for r in merged]
            n_ok = sum(1 for r in result_models if r.success)
            n_fail = len(result_models) - n_ok

            return AddTasksResponse(
                success=n_fail == 0,
                total_processed=len(result_models),
                total_success=n_ok,
                total_failed=n_fail,
                contract_id=cid,
                results=result_models,
            )
        finally:
            keep_open = req.keep_page_open
            if keep_open is None:
                keep_open = getattr(settings, "keep_page_open_after_fill", True)
            if not keep_open:
                await page.close()
            else:
                logger.info("add-asbestos-tasks: keep_page_open is true, leaving tab open.")

    # ------------------------------------------------------------------
    # New endpoint: upload asbestos PDF to a work order by CORF
    # ------------------------------------------------------------------

    @router.post(
        "/upload-asbestos-pdf",
        response_model=UploadPdfResponse,
        summary="Upload an asbestos PDF to a work order on EasyBOP by CORF",
        description=(
            "Receives a PDF as the raw request body and uploads it to the work order "
            "matching the given CORF on EasyBOP.\n\n"
            "1. Navigates to the planned-works index.\n"
            "2. Searches for the CORF via `#search_dwelling`.\n"
            "3. Clicks **Global: Asbestos Survey** row `pw_works_show_pre_item_dialog` (not Works Order Form).\n"
            "4. Sets the PDF on the file input in the popup.\n"
            "5. Clicks **Save Changes**.\n\n"
            "**n8n usage:** send PDF as binary body with query params `?corf=12345&filename=report.pdf`"
        ),
    )
    async def upload_asbestos_pdf_endpoint(
        corf: str = Query(..., description="Client Order Reference"),
        filename: str = Query("asbestos_report.pdf", description="PDF filename"),
        contract_id: Optional[str] = Query(None, description="EasyBOP contract_id"),
        keep_page_open: Optional[bool] = Query(None),
        data: UploadFile = File(..., description="PDF file to upload"),
    ):
        require_browser()
        cid = (contract_id or settings.contract_id).strip()
        context = get_context()

        pdf_bytes = await data.read()
        if data.filename and filename == "asbestos_report.pdf":
            filename = data.filename

        if not pdf_bytes:
            raise HTTPException(status_code=400, detail="No PDF provided — upload a file")

        page = await context.new_page()
        try:
            async def _save_session() -> None:
                session_path = resolve_session_path()
                session_path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(session_path))

            result = await upload_asbestos_pdf(
                page,
                corf=corf,
                pdf_bytes=pdf_bytes,
                pdf_filename=filename,
                contract_id=cid,
                username=settings.username,
                password=settings.password,
                timeout_ms=settings.request_timeout_ms,
                after_relogin=_save_session,
            )

            ok = bool(result.get("success"))
            if ok:
                await _save_session()
            return UploadPdfResponse(**result)

        except RuntimeError as e:
            raise HTTPException(status_code=401, detail=str(e))
        except Exception:
            logger.exception("upload-asbestos-pdf")
            raise HTTPException(status_code=500, detail="Internal error — see server logs.")
        finally:
            keep_open = keep_page_open
            if keep_open is None:
                keep_open = getattr(settings, "keep_page_open_after_fill", True)
            if not keep_open:
                await page.close()
            else:
                logger.info("upload-asbestos-pdf: keep_page_open is true, leaving tab open.")

    @router.post(
        "/upload-asbestos-pdf-batch",
        response_model=UploadPdfBatchResponse,
        summary="Upload multiple asbestos PDFs in one browser session",
        description=(
            "Processes all CORFs in a single browser tab. "
            "After each CORF it returns to the works index before starting the next. "
            "Returns HTTP 200 with per-item results even on partial failure."
        ),
    )
    async def upload_asbestos_pdf_batch_endpoint(request: Request) -> UploadPdfBatchResponse:
        req = _parse_pdf_batch_payload(await request.body())

        if not req.items:
            return UploadPdfBatchResponse(success=True, total=0, results=[])

        decoded = _decode_pdf_items(req.items)

        try:
            require_browser()
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            return _pdf_batch_error_results(decoded, detail)

        context = get_context()
        cid = (req.contract_id or settings.contract_id).strip()

        page = await context.new_page()
        try:
            async def _save_session() -> None:
                session_path = resolve_session_path()
                session_path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(session_path))

            try:
                batch = await upload_asbestos_pdf_batch(
                    page,
                    items=decoded,
                    contract_id=cid,
                    username=settings.username,
                    password=settings.password,
                    timeout_ms=req.timeout_ms,
                    after_relogin=_save_session,
                    use_works_id_navigation=req.use_works_id_navigation,
                )
            except Exception as exc:
                logger.exception("upload-asbestos-pdf-batch unhandled error")
                err = str(exc)
                batch = {
                    "success": False,
                    "total": len(decoded),
                    "uploaded": 0,
                    "already_uploaded": 0,
                    "errors": len(decoded),
                    "results": [
                        {
                            "success": False,
                            "corf": item["corf"],
                            "pdf_filename": item["pdf_filename"],
                            "error": err,
                            "message": err,
                        }
                        for item in decoded
                    ],
                }

            if batch.get("uploaded", 0) + batch.get("already_uploaded", 0) > 0:
                await _save_session()

            return UploadPdfBatchResponse(**batch)
        finally:
            await page.close()

    return router
