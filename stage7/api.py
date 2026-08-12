from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, HTTPException
from playwright.async_api import BrowserContext
from pydantic import BaseModel, Field

_here = Path(__file__).resolve().parent


def _load_local(name: str, filename: str):
    path = _here / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load {filename} from {_here}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_config = _load_local("_s7_config", "config.py")
_variation_auto = _load_local("_s7_variation_automation", "variation_automation.py")
_task_auto = _load_local("_s7_task_automation", "task_automation.py")
_jobs_scanner   = _load_local("_s7_jobs_scanner",   "jobs_scanner.py")
_export_auto    = _load_local("_s7_export_auto",    "export_automation.py")
_udf_auto       = _load_local("_s7_udf_auto",       "udf_automation.py")
_zip_helper       = _load_local("_s7_zip_helper",       "zip_helper.py")
_completion_auto  = _load_local("_s7_completion_auto",  "completion_automation.py")
_handover_task    = _load_local("_s7_handover_task",    "create_handover_task.py")

navigate_to_add_variation = _variation_auto.navigate_to_add_variation
add_variation_tasks          = _task_auto.add_variation_tasks
create_tasks_on_current_page = _task_auto.create_tasks_on_current_page
add_email_review_task        = _task_auto.add_email_review_task
create_handover_task_on_page = _handover_task.create_handover_task_on_page
_easybop_login               = _task_auto._login
scan_ready_for_handover = _jobs_scanner.scan_ready_for_handover
export_job_documents    = _export_auto.export_job_documents
set_handover_date       = _udf_auto.set_handover_date
build_zip_base64        = _zip_helper.build_zip_base64
mark_job_complete       = _completion_auto.mark_job_complete
settings = _config.settings
resolve_session_path = _config.resolve_session_path

logger = logging.getLogger("stage7.api")


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class AddVariationRequest(BaseModel):
    address: str = Field(
        "5 KEBLE AVENUE",
        min_length=1,
        description=(
            "Property address to search on the EasyBOP jobs index. "
            "Matches against the live search field — use the address as shown in EasyBOP "
            "(e.g. '5 KEBLE AVENUE').\n\n"
            "**Swagger test body:**\n"
            "```json\n"
            "{\"address\": \"5 KEBLE AVENUE\", \"days\": 2, \"subject\": \"Additional works required\"}\n"
            "```"
        ),
    )
    days: int = Field(
        2,
        ge=1,
        description="Number of additional days requested for the variation.",
    )
    subject: str = Field(
        "Additional works required",
        min_length=1,
        description="Subject / title of the variation (filled into the Subject field).",
    )
    sor_ref: str = Field(
        3009,
        min_length=1,
        description="SOR reference number to search in the JW Rates BOQ (e.g. '102731').",
    )
    quantity: float = Field(
        1.0,
        gt=0,
        description="Quantity to set for the matched SOR item in the JW Rates BOQ.",
    )
    keep_page_open: Optional[bool] = Field(
        None,
        description="Leave the browser tab open after completion (default from EASYBOP_KEEP_PAGE_OPEN in .env).",
    )


class AddVariationResponse(BaseModel):
    success: bool
    works_id: str = ""
    contract_id: str = ""
    variations_url: str = ""
    address: str = ""
    message: str


class ZipFileItem(BaseModel):
    filename: str = Field(..., min_length=1, description="Filename including extension (e.g. 'photo1.jpg').")
    base64: str = Field(..., min_length=1, description="Base64-encoded file content.")


class ZipFilesRequest(BaseModel):
    files: list[ZipFileItem] = Field(..., min_length=1, description="Files to include in the ZIP.")
    zip_name: str = Field("photos.zip", description="Name for the output ZIP file.")


class ZipFilesResponse(BaseModel):
    success: bool
    zip_filename: str = ""
    zip_base64: str = ""
    file_count: int = 0
    message: str = ""


class SetHandoverDateRequest(BaseModel):
    works_id: str = Field(..., min_length=1, description="EasyBOP works ID.")
    contract_id: str = Field(..., min_length=1, description="EasyBOP contract ID.")
    handover_date: str = Field(
        ...,
        min_length=1,
        description="Handover date in DD/MM/YYYY format (e.g. '23/06/2026').",
    )
    keep_page_open: Optional[bool] = Field(None)


class SetHandoverDateResponse(BaseModel):
    success: bool
    works_id: str = ""
    contract_id: str = ""
    handover_date: str = ""
    message: str = ""


class MarkJobCompleteRequest(BaseModel):
    works_id: str = Field(..., min_length=1, description="EasyBOP works ID.")
    contract_id: str = Field(..., min_length=1, description="EasyBOP contract ID.")
    completion_date: str = Field(
        ...,
        min_length=1,
        description="Completion date in DD/MM/YYYY HH:MM format (e.g. '24/06/2026 00:00').",
    )
    handover_date: str = Field(
        ...,
        min_length=1,
        description="Handover date in DD/MM/YYYY format — used only in the note text.",
    )
    keep_page_open: Optional[bool] = Field(None)


class MarkJobCompleteResponse(BaseModel):
    success: bool
    works_id: str = ""
    contract_id: str = ""
    completion_date: str = ""
    note: str = ""
    message: str = ""


class EmailReviewTaskRequest(BaseModel):
    works_id: str = Field(
        ...,
        min_length=1,
        description="EasyBOP works ID — returned by the export-job-documents call.",
    )
    contract_id: str = Field(
        ...,
        min_length=1,
        description="EasyBOP contract ID — returned by the export-job-documents call.",
    )
    address: str = Field(
        ...,
        min_length=1,
        description="Property address — used in the task description body.",
    )
    order_ref: str = Field(
        "",
        description="Our order reference (e.g. '36/1635') — used in the task description.",
    )
    email_subject: str = Field(
        ...,
        min_length=1,
        description="The exact subject line of the drafted email, included in the task note.",
    )
    keep_page_open: Optional[bool] = Field(None, description="Leave browser tab open after completion.")


class EmailReviewTaskResponse(BaseModel):
    success: bool
    works_id: str = ""
    task: dict = {}
    message: str = ""


class CreateHandoverTaskRequest(BaseModel):
    works_id: str = Field(..., min_length=1, description="EasyBOP works ID.")
    contract_id: str = Field("321129", min_length=1, description="EasyBOP contract ID.")
    client_order_reference: str = Field("", description="CORF / client order reference.")
    smart_form_name: str = Field("", description="OJS SmartForm Name for task description.")
    description: str = Field("", description="Optional task description override.")
    complete_by_date: str = Field(
        "",
        description="Complete-by date DD/MM/YYYY (default: tomorrow).",
    )
    skip_if_exists: bool = Field(
        True,
        description="Skip creation when Shannon already has a Handover needed task.",
    )
    keep_page_open: Optional[bool] = Field(None)


class CreateHandoverTaskResponse(BaseModel):
    success: bool = False
    already_exists: bool = False
    works_id: str = ""
    client_order_reference: Optional[str] = None
    smart_form_name: Optional[str] = None
    description: str = ""
    complete_by_date: str = ""
    message: str = ""
    url: str = ""


class ExportJobDocumentsRequest(BaseModel):
    address: str = Field(
        "5 KEBLE AVENUE",
        min_length=1,
        description="Property address to search in EasyBOP.",
    )
    keep_page_open: Optional[bool] = Field(None, description="Leave browser tab open after completion.")


class ExportJobDocumentsResponse(BaseModel):
    success: bool
    works_id: str = ""
    contract_id: str = ""
    address: str = ""
    zip_filename: str = ""
    zip_base64: str = ""
    excel_filename: str = ""
    excel_base64: str = ""
    message: str = ""


class ScanReadyForHandoverRequest(BaseModel):
    contract_id: str = Field(
        "321129",
        min_length=1,
        description="EasyBOP contract ID to scan (default: 321129).",
    )
    keep_page_open: Optional[bool] = Field(None, description="Leave browser tab open after scan.")


class ScanReadyForHandoverResponse(BaseModel):
    success: bool
    contract_id: str = ""
    total_scanned: int = 0
    ready_count: int = 0
    ready_jobs: list = []
    message: str = ""


class AddVariationAndTasksRequest(BaseModel):
    address: str = Field("5 KEBLE AVENUE", min_length=1, description="Property address to search in EasyBOP.")
    days: int = Field(2, ge=1, description="Number of additional days requested.")
    subject: str = Field("Additional works required", min_length=1, description="Subject / title of the variation.")
    sor_ref: str = Field("318105", min_length=1, description="SOR Document Code to search in JW Rates BOQ.")
    quantity: float = Field(1.0, gt=0, description="Quantity for the SOR item.")
    sor_description: str = Field(
        "WINDOW:RENEW TRICKLE VENT TO PVCU",
        min_length=1,
        description="Short description of the matched SOR item — used in the QS task body.",
    )
    worker_description: str = Field(
        "Replaced broken trickle vent in the toilet upstairs",
        min_length=1,
        description="Original Q2.7 free-text from the operative — used in the QS task body.",
    )
    keep_page_open: Optional[bool] = Field(None, description="Leave browser tab open after completion.")


class AddVariationAndTasksResponse(BaseModel):
    success: bool
    address: str = ""
    works_id: str = ""
    contract_id: str = ""
    variations_url: str = ""
    qs_task: dict = {}
    handover_task: dict = {}
    message: str


class CreateTasksRequest(BaseModel):
    address: str = Field(
        "5 KEBLE AVENUE",
        min_length=1,
        description="Property address — same value used for the add-variation call.",
    )
    sor_code: str = Field(
        "318105",
        min_length=1,
        description="Document Code returned by the AI SOR matcher (e.g. '318105').",
    )
    sor_description: str = Field(
        "WINDOW:RENEW TRICKLE VENT TO PVCU",
        min_length=1,
        description="Short description of the matched SOR item.",
    )
    worker_description: str = Field(
        "Replaced broken trickle vent in the toilet upstairs",
        min_length=1,
        description="Original Q2.7 free-text from the operative's job sheet.",
    )
    keep_page_open: Optional[bool] = Field(
        None,
        description="Leave the browser tab open after completion (default from .env).",
    )


class CreateTasksResponse(BaseModel):
    success: bool
    address: str = ""
    works_id: str = ""
    contract_id: str = ""
    qs_task: dict = {}
    handover_task: dict = {}
    message: str


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------

async def create_handover_task_handler(
    req: CreateHandoverTaskRequest,
    *,
    get_context: Callable[[], BrowserContext],
    require_browser: Callable[[], None],
    save_session: Callable[[], Awaitable[None]],
) -> CreateHandoverTaskResponse:
    require_browser()
    context = get_context()
    page = await context.new_page()
    keep_open = req.keep_page_open
    if keep_open is None:
        keep_open = settings.keep_page_open_after_fill

    try:
        result: dict[str, Any] = await create_handover_task_on_page(
            page,
            works_id=req.works_id,
            contract_id=req.contract_id,
            description=req.description,
            complete_by_date=req.complete_by_date,
            client_order_reference=req.client_order_reference,
            smart_form_name=req.smart_form_name,
            username=settings.username,
            password=settings.password,
            login=_easybop_login,
            timeout_ms=settings.request_timeout_ms,
            skip_if_exists=req.skip_if_exists,
        )

        if result.get("success"):
            await save_session()

        return CreateHandoverTaskResponse(**result)

    except RuntimeError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        logger.exception("stage7/create-handover-task")
        raise HTTPException(status_code=500, detail="Internal error — see server logs.")
    finally:
        if not keep_open:
            await page.close()
        else:
            logger.info("stage7/create-handover-task: keep_page_open=true — tab left open")


def create_router(
    *,
    get_context: Callable[[], BrowserContext],
    require_browser: Callable[[], None],
    save_session: Callable[[], Awaitable[None]],
) -> APIRouter:
    router = APIRouter(prefix="/stage7", tags=["Stage 7"])

    @router.post(
        "/add-variation",
        response_model=AddVariationResponse,
        summary="Navigate to Add New Variation dialog on EasyBOP (Phase 1)",
        description=(
            "**Phase 1 — navigation checkpoint.**\n\n"
            "1. Opens the EasyBOP jobs index.\n"
            "2. Searches by `address` using `#quick_search_address`.\n"
            "3. Skips disabled rows; clicks the first active row (`tr.wi_row`).\n"
            "4. Navigates to the Variations tab (`&tab_nav_item=variations`).\n"
            "5. Clicks **Add New Variation** (`#btn_add_new_variation`).\n"
            "6. Waits 3 seconds (Phase 1 stop — dialog is open for inspection).\n\n"
            "Returns `works_id`, `contract_id`, and `variations_url` for use in subsequent phases."
        ),
    )
    async def add_variation_endpoint(req: AddVariationRequest):
        require_browser()
        context = get_context()
        page = await context.new_page()
        keep_open = req.keep_page_open
        if keep_open is None:
            keep_open = settings.keep_page_open_after_fill

        try:
            async def _save() -> None:
                session_path = resolve_session_path()
                session_path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(session_path))

            result: dict[str, Any] = await navigate_to_add_variation(
                page,
                address=req.address,
                days=req.days,
                subject=req.subject,
                sor_ref=req.sor_ref,
                quantity=req.quantity,
                username=settings.username,
                password=settings.password,
                timeout_ms=settings.request_timeout_ms,
                after_relogin=_save,
            )

            if result.get("success"):
                await _save()

            return AddVariationResponse(**result)

        except RuntimeError as exc:
            raise HTTPException(status_code=401, detail=str(exc))
        except Exception:
            logger.exception("stage7/add-variation")
            raise HTTPException(status_code=500, detail="Internal error — see server logs.")
        finally:
            if not keep_open:
                await page.close()
            else:
                logger.info("stage7/add-variation: keep_page_open=true — tab left open")

    @router.post(
        "/create-tasks",
        response_model=CreateTasksResponse,
        summary="Create QS review and handover tasks on an EasyBOP job",
        description=(
            "Navigates to the job by address, then creates two tasks in sequence:\n\n"
            "1. **QS review task** — Category: *QS review for variations*, "
            "Issued To: Charlie Higginson, Priority: Medium, Due: today.\n"
            "2. **Handover task** — Category: *Handover Needed*, "
            "Issued To: Shannon Slade, Priority: High, Due: next working day.\n\n"
            "Call this endpoint after `/stage7/add-variation` has completed successfully."
        ),
    )
    async def create_tasks_endpoint(req: CreateTasksRequest):
        require_browser()
        context = get_context()
        page = await context.new_page()
        keep_open = req.keep_page_open
        if keep_open is None:
            keep_open = settings.keep_page_open_after_fill

        try:
            async def _save() -> None:
                session_path = resolve_session_path()
                session_path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(session_path))

            result: dict[str, Any] = await add_variation_tasks(
                page,
                address=req.address,
                sor_code=req.sor_code,
                sor_description=req.sor_description,
                worker_description=req.worker_description,
                username=settings.username,
                password=settings.password,
                timeout_ms=settings.request_timeout_ms,
                after_relogin=_save,
            )

            if result.get("success"):
                await _save()

            return CreateTasksResponse(**result)

        except RuntimeError as exc:
            raise HTTPException(status_code=401, detail=str(exc))
        except Exception:
            logger.exception("stage7/create-tasks")
            raise HTTPException(status_code=500, detail="Internal error — see server logs.")
        finally:
            if not keep_open:
                await page.close()
            else:
                logger.info("stage7/create-tasks: keep_page_open=true — tab left open")

    @router.post(
        "/add-variation-and-tasks",
        response_model=AddVariationAndTasksResponse,
        summary="Add variation + create QS and handover tasks in one call",
        description=(
            "Single combined endpoint for the full Stage 7 flow:\n\n"
            "1. Searches the job by `address` and navigates to the Variations tab.\n"
            "2. Fills the Add New Variation form (type, days, subject, UPRN, pricing).\n"
            "3. Opens JW Rates, finds `sor_ref`, sets `quantity`, clicks **Continue**.\n"
            "4. Clicks **Add Variation** (`#btn_add_variation`) and accepts the confirm dialog.\n"
            "5. Clicks **Save Variation** (`#btn_save_changes`) and accepts the confirm dialog.\n"
            "6. Navigates to the Tasks tab.\n"
            "7. Creates **QS review task** → Charlie Higginson, Medium, due today.\n\n"
            "Handover task is created separately via the handover_task n8n workflow."
            "Call this instead of `/add-variation` + `/create-tasks` separately."
        ),
    )
    async def add_variation_and_tasks_endpoint(req: AddVariationAndTasksRequest):
        require_browser()
        context = get_context()
        page = await context.new_page()
        keep_open = req.keep_page_open
        if keep_open is None:
            keep_open = settings.keep_page_open_after_fill

        try:
            async def _save() -> None:
                session_path = resolve_session_path()
                session_path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(session_path))

            # Step 1–4: add the variation and fill JW Rates qty
            variation_result: dict[str, Any] = await navigate_to_add_variation(
                page,
                address=req.address,
                days=req.days,
                subject=req.subject,
                sor_ref=req.sor_ref,
                quantity=req.quantity,
                username=settings.username,
                password=settings.password,
                timeout_ms=settings.request_timeout_ms,
                after_relogin=_save,
            )

            works_id = variation_result["works_id"]
            contract_id = variation_result["contract_id"]

            # Steps 5–7: navigate to Tasks tab and create both tasks
            tasks_result: dict[str, Any] = await create_tasks_on_current_page(
                page,
                works_id=works_id,
                contract_id=contract_id,
                sor_code=req.sor_ref,
                sor_description=req.sor_description,
                worker_description=req.worker_description,
                timeout_ms=settings.request_timeout_ms,
            )

            await _save()

            return AddVariationAndTasksResponse(
                success=True,
                address=req.address,
                works_id=works_id,
                contract_id=contract_id,
                variations_url=variation_result.get("variations_url", ""),
                qs_task=tasks_result.get("qs_task", {}),
                handover_task=tasks_result.get("handover_task", {}),
                message=tasks_result.get("message", "Variation and QS review task completed."),
            )

        except RuntimeError as exc:
            raise HTTPException(status_code=401, detail=str(exc))
        except Exception:
            logger.exception("stage7/add-variation-and-tasks")
            raise HTTPException(status_code=500, detail="Internal error — see server logs.")
        finally:
            if not keep_open:
                await page.close()
            else:
                logger.info("stage7/add-variation-and-tasks: keep_page_open=true — tab left open")

    @router.post(
        "/scan-ready-for-handover",
        response_model=ScanReadyForHandoverResponse,
        summary="Scan EasyBOP works index and return jobs where Ready to Handover = Yes",
        description=(
            "Navigates to the planned-works index for the given `contract_id`, "
            "reads every row across all pages, and returns the subset where the "
            "**Ready to Handover** column (`udf_5087`) is `Yes`.\n\n"
            "Call this on a schedule from n8n. Each returned job in `ready_jobs` "
            "contains: `works_id`, `address`, `description`, `property_ref`, "
            "`our_order_ref`, `physical_status`, `job_value`, and all other columns."
        ),
    )
    async def scan_ready_for_handover_endpoint(req: ScanReadyForHandoverRequest):
        require_browser()
        context = get_context()
        page = await context.new_page()
        keep_open = req.keep_page_open
        if keep_open is None:
            keep_open = settings.keep_page_open_after_fill

        try:
            async def _save() -> None:
                session_path = resolve_session_path()
                session_path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(session_path))

            result: dict[str, Any] = await scan_ready_for_handover(
                page,
                contract_id=req.contract_id,
                username=settings.username,
                password=settings.password,
                timeout_ms=settings.request_timeout_ms,
                after_relogin=_save,
            )

            if result.get("success"):
                await _save()

            return ScanReadyForHandoverResponse(
                **result,
                message=(
                    f"Scanned {result['total_scanned']} jobs in contract {req.contract_id}. "
                    f"Found {result['ready_count']} ready for handover."
                ),
            )

        except RuntimeError as exc:
            raise HTTPException(status_code=401, detail=str(exc))
        except Exception:
            logger.exception("stage7/scan-ready-for-handover")
            raise HTTPException(status_code=500, detail="Internal error — see server logs.")
        finally:
            if not keep_open:
                await page.close()
            else:
                logger.info("stage7/scan-ready-for-handover: keep_page_open=true — tab left open")

    @router.post(
        "/export-job-documents",
        response_model=ExportJobDocumentsResponse,
        summary="Download Document Pack ZIP and BOQ Excel for a job",
        description=(
            "For the job matching `address`:\n\n"
            "1. Clicks **Export Document Pack**, ticks the completion forms checkbox, "
            "downloads the ZIP.\n"
            "2. Navigates to the BOQ tab, opens **View Summary**, exports the Excel.\n\n"
            "Both files are returned as base64-encoded strings (`zip_base64`, `excel_base64`) "
            "so n8n can attach them to a Microsoft Graph API draft email via "
            "`POST /v1.0/users/{userId}/messages/{id}/attachments`."
        ),
    )
    async def export_job_documents_endpoint(req: ExportJobDocumentsRequest):
        require_browser()
        context = get_context()
        page = await context.new_page()
        keep_open = req.keep_page_open
        if keep_open is None:
            keep_open = settings.keep_page_open_after_fill

        try:
            async def _save() -> None:
                session_path = resolve_session_path()
                session_path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(session_path))

            result: dict[str, Any] = await export_job_documents(
                page,
                address=req.address,
                username=settings.username,
                password=settings.password,
                timeout_ms=settings.request_timeout_ms,
                after_relogin=_save,
            )

            if result.get("success"):
                await _save()

            return ExportJobDocumentsResponse(**result)

        except RuntimeError as exc:
            raise HTTPException(status_code=401, detail=str(exc))
        except Exception:
            logger.exception("stage7/export-job-documents")
            raise HTTPException(status_code=500, detail="Internal error — see server logs.")
        finally:
            if not keep_open:
                await page.close()
            else:
                logger.info("stage7/export-job-documents: keep_page_open=true — tab left open")

    @router.post(
        "/zip-files",
        response_model=ZipFilesResponse,
        summary="ZIP a list of base64 files and return the archive as base64",
        description=(
            "Accepts an array of `{filename, base64}` objects, creates a ZIP in memory, "
            "and returns the archive as a base64 string ready to attach to a Graph API email.\n\n"
            "**No browser required.** Call this after downloading SharePoint photos via Graph API "
            "in n8n, passing each photo as base64. Attach `zip_base64` to the draft email "
            "alongside the document pack and BOQ Excel."
        ),
    )
    async def zip_files_endpoint(req: ZipFilesRequest):
        try:
            result = build_zip_base64(
                files=[f.model_dump() for f in req.files],
                zip_name=req.zip_name,
            )
            return ZipFilesResponse(**result)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception:
            logger.exception("stage7/zip-files")
            raise HTTPException(status_code=500, detail="Internal error — see server logs.")

    @router.post(
        "/set-handover-date",
        response_model=SetHandoverDateResponse,
        summary="Write the handover date into Works UDF for a job",
        description=(
            "Navigates directly to the Works UDF tab using `works_id` + `contract_id`, "
            "fills `udf_pw_works_5069` with `handover_date` (DD/MM/YYYY), "
            "and clicks **Save Works UDF Data**.\n\n"
            "Triggered by the n8n polling workflow when it detects the handover "
            "email has been sent from the BCCorders inbox."
        ),
    )
    async def set_handover_date_endpoint(req: SetHandoverDateRequest):
        require_browser()
        context = get_context()
        page = await context.new_page()
        keep_open = req.keep_page_open
        if keep_open is None:
            keep_open = settings.keep_page_open_after_fill

        try:
            async def _save() -> None:
                session_path = resolve_session_path()
                session_path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(session_path))

            result: dict[str, Any] = await set_handover_date(
                page,
                works_id=req.works_id,
                contract_id=req.contract_id,
                handover_date=req.handover_date,
                username=settings.username,
                password=settings.password,
                timeout_ms=settings.request_timeout_ms,
                after_relogin=_save,
            )

            if result.get("success"):
                await _save()

            return SetHandoverDateResponse(**result)

        except RuntimeError as exc:
            raise HTTPException(status_code=401, detail=str(exc))
        except Exception:
            logger.exception("stage7/set-handover-date")
            raise HTTPException(status_code=500, detail="Internal error — see server logs.")
        finally:
            if not keep_open:
                await page.close()
            else:
                logger.info("stage7/set-handover-date: keep_page_open=true — tab left open")

    @router.post(
        "/mark-job-complete",
        response_model=MarkJobCompleteResponse,
        summary="Enter completion date and add closing note on the Process tab",
        description=(
            "Navigates directly to `works_details.php` (Process tab) using "
            "`works_id` + `contract_id`, verifies **Ready to Handover = Yes** "
            "on the Works UDF tab, then:\n\n"
            "1. Bypasses the `readonly` attribute on `date_time_completed` via JS "
            "and sets the completion date.\n"
            "2. Clicks **Save Changes**.\n"
            "3. Clicks **Add Note**, fills the closing note "
            "(`Handover pack sent to BCC on [date]. Job closed.`), and saves.\n\n"
            "Returns `success=false` without making any changes if Ready to Handover "
            "is not Yes — safe to call from a polling workflow."
        ),
    )
    async def mark_job_complete_endpoint(req: MarkJobCompleteRequest):
        require_browser()
        context = get_context()
        page = await context.new_page()
        keep_open = req.keep_page_open
        if keep_open is None:
            keep_open = settings.keep_page_open_after_fill

        try:
            async def _save() -> None:
                session_path = resolve_session_path()
                session_path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(session_path))

            result: dict[str, Any] = await mark_job_complete(
                page,
                works_id=req.works_id,
                contract_id=req.contract_id,
                completion_date=req.completion_date,
                handover_date=req.handover_date,
                username=settings.username,
                password=settings.password,
                timeout_ms=settings.request_timeout_ms,
                after_relogin=_save,
            )

            if result.get("success"):
                await _save()

            return MarkJobCompleteResponse(**result)

        except RuntimeError as exc:
            raise HTTPException(status_code=401, detail=str(exc))
        except Exception:
            logger.exception("stage7/mark-job-complete")
            raise HTTPException(status_code=500, detail="Internal error — see server logs.")
        finally:
            if not keep_open:
                await page.close()
            else:
                logger.info("stage7/mark-job-complete: keep_page_open=true — tab left open")

    @router.post(
        "/add-email-review-task",
        response_model=EmailReviewTaskResponse,
        summary="Create EasyBOP task notifying Shannon the handover email draft is ready",
        description=(
            "Navigates directly to the Tasks tab using `works_id` + `contract_id` "
            "(no address re-search needed) and creates a task:\n\n"
            "- **Category**: Handover needed\n"
            "- **Issued To**: Shannon Slade\n"
            "- **Priority**: High\n"
            "- **Due**: next working day\n"
            "- **Description**: includes address, email subject, and reminder to update "
            "the handover date in Works UDF after sending.\n\n"
            "Call this after `/stage7/export-job-documents` and the Graph API draft creation."
        ),
    )
    async def add_email_review_task_endpoint(req: EmailReviewTaskRequest):
        require_browser()
        context = get_context()
        page = await context.new_page()
        keep_open = req.keep_page_open
        if keep_open is None:
            keep_open = settings.keep_page_open_after_fill

        try:
            result: dict[str, Any] = await add_email_review_task(
                page,
                works_id=req.works_id,
                contract_id=req.contract_id,
                address=req.address,
                order_ref=req.order_ref,
                email_subject=req.email_subject,
                timeout_ms=settings.request_timeout_ms,
            )

            if result.get("success"):
                session_path = resolve_session_path()
                session_path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(session_path))

            return EmailReviewTaskResponse(**result)

        except RuntimeError as exc:
            raise HTTPException(status_code=401, detail=str(exc))
        except Exception:
            logger.exception("stage7/add-email-review-task")
            raise HTTPException(status_code=500, detail="Internal error — see server logs.")
        finally:
            if not keep_open:
                await page.close()
            else:
                logger.info("stage7/add-email-review-task: keep_page_open=true — tab left open")

    @router.post(
        "/create-handover-task",
        response_model=CreateHandoverTaskResponse,
        summary="Create Handover needed task for Shannon on a completed OJS job",
        description=(
            "Opens the job **Tasks** tab and creates:\n\n"
            "- **Category:** Handover needed\n"
            "- **Issued To:** Shannon Slade\n"
            "- **Priority:** High\n"
            "- **Complete by:** tomorrow (DD/MM/YYYY)\n\n"
            "Used by the `handover_task` n8n workflow after `resolve-works-id-stage6`.\n"
            "Alias route: `POST /create-handover-task-stage7`."
        ),
    )
    async def create_handover_task_endpoint(req: CreateHandoverTaskRequest):
        return await create_handover_task_handler(
            req,
            get_context=get_context,
            require_browser=require_browser,
            save_session=save_session,
        )

    return router
