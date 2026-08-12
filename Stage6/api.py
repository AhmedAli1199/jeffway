from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from playwright.async_api import BrowserContext
from pydantic import BaseModel, Field

_here = Path(__file__).resolve().parent


def _load_local_module(name: str, filename: str):
    path = _here / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load {filename} from {_here}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_automation = _load_local_module("_stage6_automation", "automation.py")
_config = _load_local_module("_stage6_config", "config.py")
_pdf_parser = _load_local_module("_stage6_pdf_parser", "pdf_parser.py")
_job_report = _load_local_module("_stage6_job_report", "job_report.py")
_daily_exception_report = _load_local_module(
    "_stage6_daily_exception_report",
    "daily_exception_report.py",
)
_weekly_performance_report = _load_local_module(
    "_stage6_weekly_performance_report",
    "weekly_performance_report.py",
)
_daily_issue_reconcile = _load_local_module(
    "_stage6_daily_issue_reconcile",
    "daily_issue_reconcile.py",
)

extract_operative_job_sheets = _automation.extract_operative_job_sheets
scrape_boq_for_corf = _automation.scrape_boq_for_corf
scrape_boq_batch = _automation.scrape_boq_batch
scrape_works_index = _automation.scrape_works_index
_automation_module = _automation  # used for cache invalidation
parse_operative_job_sheet_text = _pdf_parser.parse_operative_job_sheet_text
settings = _config.settings
resolve_session_path = _config.resolve_session_path

logger = logging.getLogger("stage6.api")

# Persistent browser-tab pool for BOQ batch scraping.
# Tabs are opened on first use and reused across successive /fetch-boq-batch calls,
# avoiding the overhead of opening/closing 3 tabs per batch.
_boq_page_pool: List = []


class ExtractOjsRequest(BaseModel):
    limit: Optional[int] = Field(
        None,
        ge=1,
        description="Max number of completed Operative job sheets to extract (after grid filter).",
    )
    smart_form_ids: Optional[List[str]] = Field(
        None,
        description=(
            "Optional list of smart_form_x_id values to extract directly "
            "(skips register grid scrape). Useful for re-processing one form."
        ),
    )
    skip_smart_form_ids: Optional[List[str]] = Field(
        None,
        description="smart_form_x_id values already on Google Sheet — skip PDF download.",
    )
    skip_entries: Optional[List["OjsSkipEntry"]] = Field(
        None,
        description=(
            "Rows already on Google Sheet — matched by SmartForm Name + Date Created. "
            "n8n should send these from the Operative Job Sheet tab before calling extract."
        ),
    )
    force_extract_smart_form_ids: Optional[List[str]] = Field(
        None,
        description=(
            "Re-download PDFs for these smart_form_x_id values even when they appear in "
            "skip_smart_form_ids — used for photo backfill on existing sheet rows."
        ),
    )
    max_form_age_weeks: Optional[int] = Field(
        None,
        ge=0,
        description=(
            "Only extract forms whose EasyBOP Date Created is within this many weeks "
            "(Europe/London). Omit or set 0 to disable the age filter."
        ),
    )
    days_to_look_back: Optional[int] = Field(
        None,
        ge=0,
        description=(
            "Only extract forms whose EasyBOP Date Created is within this many days "
            "(Europe/London): 0 = today only, 1 = today + yesterday. Omit to disable "
            "the day-scope filter. Used by the Daily Form Report 'days to look back'."
        ),
    )
    only_complete: bool = Field(
        False,
        description=(
            "When true, extract only forms whose Status is exactly 'Complete' and drop "
            "'Part Complete' rows. Default false keeps existing Complete + Part Complete."
        ),
    )


class OjsSkipEntry(BaseModel):
    smart_form_name: str = Field(..., description="SmartForm Name column from sheet")
    date_created: str = Field(..., description="Date Created column from sheet")


class OjsParsedFields(BaseModel):
    template_title: Optional[str] = None
    job_header: Optional[str] = None
    corf: Optional[str] = None
    fields: Dict[str, Any] = Field(default_factory=dict)
    labeled_fields: Dict[str, Any] = Field(
        default_factory=dict,
        description="Sheet columns like Q1.1 Address mapped to answer values.",
    )
    photos: Dict[str, Any] = Field(
        default_factory=dict,
        description="Extracted appendix photos grouped as completed_work and variations.",
    )


class OjsExtractItem(BaseModel):
    smart_form_x_id: str
    pdf_url: str
    grid: Dict[str, Any] = Field(default_factory=dict)
    parsed: OjsParsedFields
    success: bool = True


class OjsExtractError(BaseModel):
    smart_form_x_id: str
    grid: Dict[str, Any] = Field(default_factory=dict)
    error: str


class ExtractOjsResponse(BaseModel):
    success: bool
    partial: bool = False
    total_grid_matched: int = 0
    total_skipped_existing: int = 0
    total_skipped_non_bcc: int = 0
    total_skipped_too_old: int = 0
    total_skipped_out_of_scope: int = 0
    total_skipped_part_complete: int = 0
    total_grid_rows: int
    total_extracted: int
    total_errors: int
    results: List[OjsExtractItem]
    errors: List[OjsExtractError]
    message: str


class FetchBoqRequest(BaseModel):
    address: str = Field(..., min_length=1, description="Property address to search on EasyBOP (e.g. 5 KEBLE AVENUE)")
    smart_form_name: Optional[str] = None
    uprn: Optional[str] = None
    timeout_ms: int = Field(30_000, description="Playwright timeout in milliseconds")


class BoqSorLine(BaseModel):
    sor_code: str
    description: str
    quantity: float
    unit: str
    rate: str
    total: str
    is_variation: bool
    section: Optional[str] = ""
    notes: Optional[str] = ""
    variation_label: Optional[str] = ""
    detailed_description: Optional[str] = ""


class FetchBoqResponse(BaseModel):
    success: bool
    address: str
    smart_form_name: Optional[str] = None
    uprn: Optional[str] = None
    works_id: str
    boq_url: str
    sor_count: int
    sor_lines: List[BoqSorLine]
    boq_grand_total: str = ""
    message: str


class WorksIndexRequest(BaseModel):
    timeout_ms: int = Field(90_000, description="Playwright timeout in milliseconds (index may have many pages)")


class WorksIndexItem(BaseModel):
    works_id: str
    address: str = ""
    property_ref: str = ""
    client_order_reference: str = ""
    our_order_reference: str = ""
    scope_of_work: str = ""
    physical_works_status: str = ""


class WorksIndexResponse(BaseModel):
    success: bool
    total: int
    items: List[WorksIndexItem]
    message: str


class BoqBatchItem(BaseModel):
    address: Optional[str] = None
    smart_form_name: Optional[str] = None
    uprn: Optional[str] = None
    works_id: Optional[str] = Field(
        None,
        description="EasyBOP works_id (from /works-index). When supplied the BOQ page is "
                    "opened directly — no address search needed (much faster).",
    )
    use_address_search: Optional[bool] = Field(
        False,
        description="When true, search by address on WI quick-search page instead of direct works_id.",
    )


class FetchBoqBatchRequest(BaseModel):
    items: List[BoqBatchItem] = Field(
        ...,
        min_length=1,
        max_length=3,
        description="Up to 3 jobs per batch — processed in parallel (one tab each).",
    )
    timeout_ms: int = Field(55_000, description="Playwright timeout in milliseconds per step")
    concurrency: int = Field(
        3,
        ge=1,
        le=3,
        description="How many jobs to scrape at once in parallel (max 3).",
    )


class BoqBatchItemResult(BaseModel):
    success: bool
    address: str = ""
    smart_form_name: Optional[str] = None
    uprn: Optional[str] = None
    works_id: str = ""
    boq_url: str = ""
    sor_count: int = 0
    sor_lines: List[BoqSorLine] = Field(default_factory=list)
    boq_grand_total: str = ""
    error: Optional[str] = None
    skip_boq_checked_true: bool = False
    reason: Optional[str] = None


class FetchBoqBatchResponse(BaseModel):
    success: bool
    total: int
    fetched: int
    errors: int
    interrupted: bool = False
    error: Optional[str] = None
    results: List[BoqBatchItemResult]
    message: str


class JobReportRequest(BaseModel):
    ojs_rows: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="All rows from Operative Job Sheet (including Job Status columns if present).",
    )
    boq_rows: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="All rows from OJS BOQ & SMV tab.",
    )


class DailyFormReportSummary(BaseModel):
    new_issue_count: int = Field(default=0, ge=0)
    existing_open_count: int = Field(default=0, ge=0)
    total_issue_count: int = Field(default=0, ge=0)
    affected_operatives: int = Field(default=0, ge=0)
    affected_forms: int = Field(default=0, ge=0)
    access_issue_forms: int = Field(default=0, ge=0)


class DailyFormIssue(BaseModel):
    issue_key: str = ""
    corf: str = ""
    smart_form_name: str = ""
    form_date: str = ""
    smart_form_x_id: str = ""
    pdf_url: str = ""
    issue_type: str = ""
    issue_detail: str = ""
    issue_summary: str = ""
    issue_resolved: str = ""
    access_issue: bool = False
    access_issue_reason: str = ""
    is_new: bool = False


class DailyFormOperativeGroup(BaseModel):
    operative: str
    issue_count: int = Field(default=0, ge=0)
    forms: List[DailyFormIssue] = Field(default_factory=list)


class DailyFormReportRequest(BaseModel):
    report_type: Literal["daily_form_report"] = "daily_form_report"
    report_generated_at: datetime
    report_date: str
    summary: DailyFormReportSummary
    operative_groups: List[DailyFormOperativeGroup] = Field(
        default_factory=list,
    )


class WeeklyFormIssuesRequest(BaseModel):
    issue_cases: List[Dict[str, Any]] = Field(default_factory=list)
    ojs_rows: List[Dict[str, Any]] = Field(default_factory=list)
    week_days: int = Field(default=7, ge=1, le=31)
    refresh_from_easybop: bool = Field(
        default=True,
        description=(
            "Re-download PDFs from EasyBOP for open/in-scope Form Issue Cases "
            "and merge fresh Q fields into ojs_rows before reconcile."
        ),
    )


class WeeklySheetSummaryRequest(BaseModel):
    issue_cases: List[Dict[str, Any]] = Field(default_factory=list)
    week_days: int = Field(default=7, ge=1, le=31)


class DailyIssueReconcileRequest(BaseModel):
    issue_cases: List[Dict[str, Any]] = Field(default_factory=list)
    extract_results: List[Dict[str, Any]] = Field(default_factory=list)


class WeeklyFormPerformanceReportRequest(BaseModel):
    report_type: Literal["weekly_form_performance_report"] = (
        "weekly_form_performance_report"
    )
    report_generated_at: datetime
    week_window_label: str = ""
    week_days: int = Field(default=7, ge=1, le=31)
    summary: Dict[str, Any] = Field(default_factory=dict)
    operative_stats: List[Dict[str, Any]] = Field(default_factory=list)
    week_issues: List[Dict[str, Any]] = Field(default_factory=list)
    still_open_carry: List[Dict[str, Any]] = Field(default_factory=list)
    missing_ojs: List[Dict[str, Any]] = Field(default_factory=list)


ExtractOjsRequest.model_rebuild()


def _json_safe_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _json_safe_fields(fields: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {str(k): _json_safe_value(v) for k, v in (fields or {}).items()}


def _json_safe_grid(grid: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    safe: Dict[str, Any] = {}
    for k, v in (grid or {}).items():
        if isinstance(v, dict):
            safe[str(k)] = {str(ck): _json_safe_value(cv) for ck, cv in v.items()}
        else:
            safe[str(k)] = _json_safe_value(v)
    return safe


def _build_response(
    payload: Dict[str, Any],
    *,
    partial: bool = False,
    warning: Optional[str] = None,
) -> ExtractOjsResponse:
    raw_results = payload.get("results") or []
    raw_errors = payload.get("errors") or []

    results: List[OjsExtractItem] = []
    build_errors: List[str] = []

    for item in raw_results:
        try:
            parsed = item.get("parsed") or {}
            results.append(
                OjsExtractItem(
                    smart_form_x_id=str(item.get("smart_form_x_id") or ""),
                    pdf_url=str(item.get("pdf_url") or ""),
                    grid=_json_safe_grid(item.get("grid")),
                    parsed=OjsParsedFields(
                        template_title=parsed.get("template_title"),
                        job_header=parsed.get("job_header"),
                        corf=parsed.get("corf"),
                        fields=_json_safe_fields(parsed.get("fields")),
                        labeled_fields=_json_safe_fields(parsed.get("labeled_fields")),
                        photos=parsed.get("photos") or {},
                    ),
                    success=True,
                )
            )
        except Exception as e:
            sfid = str(item.get("smart_form_x_id") or "unknown")
            build_errors.append(f"{sfid}: {e}")
            logger.warning("extract-operative-job-sheets: skip result %s: %s", sfid, e)

    errors: List[OjsExtractError] = []
    for item in raw_errors:
        try:
            errors.append(
                OjsExtractError(
                    smart_form_x_id=str(item.get("smart_form_x_id") or ""),
                    grid=_json_safe_grid(item.get("grid")),
                    error=str(item.get("error") or "unknown error"),
                )
            )
        except Exception as e:
            logger.warning("extract-operative-job-sheets: skip error row: %s", e)

    for note in build_errors:
        errors.append(
            OjsExtractError(
                smart_form_x_id="response_build",
                grid={},
                error=note,
            )
        )

    extracted = len(results)
    err_count = len(errors)
    is_partial = partial or bool(build_errors) or bool(warning)

    parts = [f"Extracted {extracted} Operative job sheet(s)"]
    skipped_existing = int(payload.get("total_skipped_existing") or 0)
    skipped_non_bcc = int(payload.get("total_skipped_non_bcc") or 0)
    skipped_too_old = int(payload.get("total_skipped_too_old") or 0)
    skipped_out_of_scope = int(payload.get("total_skipped_out_of_scope") or 0)
    skipped_part_complete = int(payload.get("total_skipped_part_complete") or 0)
    grid_matched = int(payload.get("total_grid_matched") or 0)
    if grid_matched:
        parts.append(f"{grid_matched} on EasyBOP grid")
    if skipped_existing:
        parts.append(f"{skipped_existing} skipped (already on sheet)")
    if skipped_non_bcc:
        parts.append(f"{skipped_non_bcc} skipped (not BCC Response Repairs)")
    if skipped_too_old:
        parts.append(f"{skipped_too_old} skipped (older than max age)")
    if skipped_out_of_scope:
        parts.append(f"{skipped_out_of_scope} skipped (outside day look-back)")
    if skipped_part_complete:
        parts.append(f"{skipped_part_complete} skipped (Part Complete)")
    if err_count:
        parts.append(f"{err_count} error(s)")
    if warning:
        parts.append(warning)
    if is_partial:
        parts.append("partial response")

    return ExtractOjsResponse(
        success=extracted > 0 or (err_count == 0 and skipped_existing >= 0),
        partial=is_partial,
        total_grid_matched=grid_matched,
        total_skipped_existing=skipped_existing,
        total_skipped_non_bcc=skipped_non_bcc,
        total_skipped_too_old=skipped_too_old,
        total_skipped_out_of_scope=skipped_out_of_scope,
        total_skipped_part_complete=skipped_part_complete,
        total_grid_rows=int(payload.get("total_grid_rows") or 0),
        total_extracted=extracted,
        total_errors=err_count,
        results=results,
        errors=errors,
        message="; ".join(parts) + ".",
    )


def create_router(
    *,
    get_context: Callable[[], BrowserContext],
    require_browser: Callable[[], None],
    save_session: Callable[[], Awaitable[None]],
) -> APIRouter:
    router = APIRouter(tags=["Stage 6"])

    @router.post(
        "/extract-operative-job-sheets",
        response_model=ExtractOjsResponse,
        summary="Extract Operative job sheet PDFs (Complete + Part Complete) from EasyBOP",
        description=(
            "Opens **SmartForms register** (`/a_smart_forms/register.php`), selects the **current month** "
            "(Europe/London) on `#search_months`, then finds rows where:\n"
            "- **Template** contains `Operative job sheet`\n"
            "- **Status** is `Complete` or `Part Complete`\n"
            "- **SmartForm Name** contains `: BCC Response Repairs` "
            "(e.g. `Address: BCC Response Repairs: 1298631`)\n\n"
            "For each row, downloads the PDF preview "
            "(`preview_form_pdf.php?smart_form_x_id=...`) using the existing Playwright session, "
            "then parses Q fields (Q1.1 Address, Q1.2 Date, Q2.1 daily jobs table, etc.).\n\n"
            "Pass `skip_smart_form_ids` and/or `skip_entries` (SmartForm Name + Date Created) "
            "from Google Sheets so PDFs are **not** re-downloaded for rows already on the sheet.\n\n"
            "Use `force_extract_smart_form_ids` to re-download specific rows (e.g. photo backfill) "
            "even when they are in the skip list.\n\n"
            "Returns **partial results** when some rows fail or response shaping hits issues — "
            "n8n can still append successful `results` to Google Sheets."
        ),
    )
    async def extract_operative_job_sheets_endpoint(req: Optional[ExtractOjsRequest] = None):
        require_browser()
        req = req or ExtractOjsRequest()
        context = get_context()
        page = await context.new_page()
        payload: Optional[Dict[str, Any]] = None

        try:
            logger.info(
                "extract-operative-job-sheets: start limit=%s skip_ids=%s skip_entries=%s "
                "max_form_age_weeks=%s days_to_look_back=%s only_complete=%s",
                req.limit,
                len(req.skip_smart_form_ids or []),
                len(req.skip_entries or []),
                req.max_form_age_weeks,
                req.days_to_look_back,
                req.only_complete,
            )
            skip_entries = None
            if req.skip_entries:
                skip_entries = [
                    {
                        "smart_form_name": e.smart_form_name,
                        "date_created": e.date_created,
                    }
                    for e in req.skip_entries
                ]
            try:
                payload = await extract_operative_job_sheets(
                    page,
                    context,
                    username=settings.username,
                    password=settings.password,
                    timeout_ms=settings.request_timeout_ms,
                    after_relogin=save_session,
                    limit=req.limit,
                    smart_form_ids=req.smart_form_ids,
                    skip_smart_form_ids=req.skip_smart_form_ids,
                    skip_entries=skip_entries,
                    force_extract_smart_form_ids=req.force_extract_smart_form_ids,
                    max_form_age_weeks=req.max_form_age_weeks,
                    days_to_look_back=req.days_to_look_back,
                    only_complete=req.only_complete,
                )
            except RuntimeError as e:
                logger.warning("extract-operative-job-sheets: auth/runtime error: %s", e)
                raise HTTPException(status_code=401, detail=str(e)) from e
            except Exception as e:
                if payload and (payload.get("results") or payload.get("errors")):
                    logger.exception(
                        "extract-operative-job-sheets: extraction error after partial progress"
                    )
                    return _build_response(
                        payload,
                        partial=True,
                        warning=f"Extraction interrupted: {e}",
                    )
                logger.exception("extract-operative-job-sheets")
                raise HTTPException(
                    status_code=500,
                    detail="Internal error — see server logs.",
                ) from e

            try:
                return _build_response(payload)
            except Exception as e:
                logger.exception("extract-operative-job-sheets: response build failed")
                return _build_response(
                    payload,
                    partial=True,
                    warning=f"Response build issue: {e}",
                )
        finally:
            if not settings.keep_page_open_after_fill:
                await page.close()
            else:
                extracted = (payload or {}).get("total_extracted", "?")
                errs = (payload or {}).get("total_errors", "?")
                logger.info(
                    "extract-operative-job-sheets: done — extracted=%s errors=%s (keep_page_open)",
                    extracted,
                    errs,
                )

    # ------------------------------------------------------------------
    # POST /works-index
    # ------------------------------------------------------------------

    @router.post(
        "/works-index",
        response_model=WorksIndexResponse,
        summary="Scrape all works from the EasyBOP planned-works index",
        description=(
            "Navigates to the planned-works index page (`index.php?contract_id=321129`) "
            "and scrapes every row in the jqgrid (with pagination).\n\n"
            "Returns `works_id` + `address` for every job — pass `works_id` into "
            "`/fetch-boq-batch` items to skip address search and go directly to each BOQ page."
        ),
    )
    async def works_index_endpoint(req: Optional[WorksIndexRequest] = None):
        require_browser()
        req = req or WorksIndexRequest()
        context = get_context()
        try:
            rows = await scrape_works_index(
                context,
                username=settings.username,
                password=settings.password,
                timeout_ms=req.timeout_ms,
                after_relogin=save_session,
            )
        except RuntimeError as e:
            raise HTTPException(status_code=401, detail=str(e)) from e
        except Exception as e:
            logger.exception("works-index: scrape failed")
            raise HTTPException(status_code=500, detail=str(e)) from e

        items = [
            WorksIndexItem(
                works_id=str(r.get("works_id") or ""),
                address=str(r.get("address") or ""),
                property_ref=str(r.get("property_ref") or ""),
                client_order_reference=str(r.get("client_order_reference") or ""),
                our_order_reference=str(r.get("our_order_reference") or ""),
                scope_of_work=str(r.get("scope_of_work") or ""),
                physical_works_status=str(r.get("physical_works_status") or ""),
            )
            for r in rows
            if r.get("works_id")
        ]
        return WorksIndexResponse(
            success=True,
            total=len(items),
            items=items,
            message=f"Scraped {len(items)} works from EasyBOP planned-works index.",
        )

    # ------------------------------------------------------------------
    # POST /fetch-boq-for-corf
    # ------------------------------------------------------------------

    @router.post(
        "/fetch-boq-for-corf",
        response_model=FetchBoqResponse,
        summary="Fetch BOQ/SOR lines for a property address from EasyBOP",
        description=(
            "Searches EasyBOP WI tab by **property address** (`#quick_search_address`), "
            "navigates to the BOQ tab, and scrapes all SOR lines including variations.\n\n"
            "Legacy path name kept for n8n compatibility — send `address`, not CORF/UPRN."
        ),
    )
    async def fetch_boq_for_corf_endpoint(req: FetchBoqRequest):
        require_browser()
        context = get_context()
        page = await context.new_page()
        try:
            result = await scrape_boq_for_corf(
                page,
                req.address,
                smart_form_name=req.smart_form_name or "",
                uprn=req.uprn or "",
                username=settings.username,
                password=settings.password,
                timeout_ms=req.timeout_ms,
                after_relogin=save_session,
            )
            return FetchBoqResponse(
                success=True,
                address=result["address"],
                smart_form_name=result.get("smart_form_name") or req.smart_form_name,
                uprn=result.get("uprn") or req.uprn,
                works_id=result["works_id"],
                boq_url=result["boq_url"],
                sor_count=result["sor_count"],
                sor_lines=[BoqSorLine(**ln) for ln in result["sor_lines"]],
                boq_grand_total=str(result.get("boq_grand_total") or ""),
                message=f"Fetched {result['sor_count']} SOR line(s) for {req.address}.",
            )
        except RuntimeError as e:
            raise HTTPException(status_code=401, detail=str(e)) from e
        except Exception as e:
            logger.exception("fetch-boq-for-corf: address=%s", req.address)
            raise HTTPException(status_code=500, detail=str(e)) from e
        finally:
            await page.close()

    @router.post(
        "/fetch-boq-batch",
        response_model=FetchBoqBatchResponse,
        summary="Fetch BOQ/SOR lines for up to 3 jobs in parallel",
        description=(
            "Processes up to **3 jobs in parallel** (one Playwright tab per job):\n"
            "WI quick-search → job name check → BOQ tab → scrape.\n\n"
            "Always returns per-job `results` — partial success is preserved even when "
            "some jobs fail or the batch is interrupted."
        ),
    )
    async def fetch_boq_batch_endpoint(req: FetchBoqBatchRequest):
        require_browser()
        context = get_context()
        payload: Dict[str, Any] = {
            "success": False,
            "total": len(req.items),
            "fetched": 0,
            "errors": len(req.items),
            "interrupted": True,
            "error": "unknown",
            "results": [],
        }
        try:
            payload = await scrape_boq_batch(
                context,
                [
                    {
                        "address": item.address or "",
                        "smart_form_name": item.smart_form_name or "",
                        "uprn": item.uprn or "",
                        "works_id": item.works_id or "",
                        "use_address_search": bool(item.use_address_search),
                    }
                    for item in req.items
                ],
                username=settings.username,
                password=settings.password,
                timeout_ms=req.timeout_ms,
                after_relogin=save_session,
                concurrency=req.concurrency,
                persistent_pages=_boq_page_pool,
            )
        except RuntimeError as e:
            raise HTTPException(status_code=401, detail=str(e)) from e
        except Exception as e:
            logger.exception("fetch-boq-batch")
            if not payload.get("results"):
                raise HTTPException(status_code=500, detail=str(e)) from e
            payload["interrupted"] = True
            payload["error"] = str(e)

        results = [
            BoqBatchItemResult(
                success=bool(r.get("success")),
                address=str(r.get("address") or ""),
                smart_form_name=r.get("smart_form_name"),
                uprn=r.get("uprn"),
                works_id=str(r.get("works_id") or ""),
                boq_url=str(r.get("boq_url") or ""),
                sor_count=int(r.get("sor_count") or 0),
                sor_lines=[BoqSorLine(**ln) for ln in (r.get("sor_lines") or [])],
                boq_grand_total=str(r.get("boq_grand_total") or ""),
                error=r.get("error"),
                skip_boq_checked_true=bool(r.get("skip_boq_checked_true")),
                reason=r.get("reason"),
            )
            for r in payload.get("results") or []
        ]
        total = int(payload.get("total") or len(req.items))
        fetched = int(payload.get("fetched") or 0)
        err_count = int(payload.get("errors") or 0)
        interrupted = bool(payload.get("interrupted"))
        partial_note = " (partial — some jobs may be missing)" if interrupted else ""
        return FetchBoqBatchResponse(
            success=bool(payload.get("success")),
            total=total,
            fetched=fetched,
            errors=err_count,
            interrupted=interrupted,
            error=payload.get("error"),
            results=results,
            message=(
                f"Fetched BOQ for {fetched}/{total} job(s)"
                + (f" ({err_count} error(s))" if err_count else "")
                + partial_note
                + "."
            ),
        )

    # ------------------------------------------------------------------
    # POST /boq-pool/reset
    # ------------------------------------------------------------------

    @router.post(
        "/boq-pool/reset",
        summary="Close persistent BOQ tabs and clear the works-index cache",
        description=(
            "Closes every tab in the persistent BOQ page pool, empties it, and "
            "clears the cached works-index address map so it is re-fetched on the "
            "next batch call.  Call this after a workflow run completes, or if "
            "tabs appear stuck."
        ),
    )
    async def boq_pool_reset_endpoint():
        closed = 0
        for p in list(_boq_page_pool):
            if not p.is_closed():
                try:
                    await p.close()
                    closed += 1
                except Exception:
                    pass
        _boq_page_pool.clear()
        # Also clear the works-index cache so a fresh fetch happens next run
        _automation_module._works_index_addr_map = {}
        _automation_module._works_index_map_ts = 0.0
        logger.info("boq-pool/reset: closed %d tab(s), works-index cache cleared", closed)
        return {"ok": True, "closed": closed, "message": f"Closed {closed} tab(s) and cleared works-index cache."}

    # ------------------------------------------------------------------
    # POST /generate-job-report-pdf
    # ------------------------------------------------------------------

    @router.post(
        "/generate-job-report-pdf",
        summary="Generate Stage 6 job status PDF report",
        description=(
            "Accepts Operative Job Sheet + OJS BOQ & SMV rows from n8n, aggregates time per job "
            "(operative breakdown, SMV comparison, jobs in jeopardy), and returns a PDF."
        ),
        responses={
            200: {
                "content": {"application/pdf": {}},
                "description": "Job status report PDF",
            }
        },
    )
    async def generate_job_report_pdf_endpoint(req: JobReportRequest):
        try:
            pdf_bytes, meta = await asyncio.to_thread(
                _job_report.build_job_report_pdf_from_sheets,
                req.ojs_rows,
                req.boq_rows,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.exception("generate-job-report-pdf")
            raise HTTPException(status_code=500, detail=str(e)) from e

        filename = f"job-status-report-{meta['date_slug']}.pdf"
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Report-Total-Jobs": str(meta["total_jobs"]),
                "X-Report-In-Progress": str(meta["in_progress_count"]),
                "X-Report-Completed": str(meta["completed_count"]),
                "X-Report-Jeopardy": str(meta["jeopardy_count"]),
            },
        )

    @router.post(
        "/generate-daily-exception-report-pdf",
        response_class=Response,
    )
    async def generate_daily_exception_report_pdf(
        req: DailyFormReportRequest,
    ):
        try:
            pdf_bytes, meta = await asyncio.to_thread(
                _daily_exception_report.build_daily_exception_report_pdf,
                req.model_dump(mode="python"),
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=str(exc),
            ) from exc
        except Exception as exc:
            logger.exception(
                "Daily exception PDF generation failed",
            )

            raise HTTPException(
                status_code=500,
                detail="Daily exception PDF generation failed.",
            ) from exc

        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": (
                    'attachment; '
                    f'filename="daily-form-report-{req.report_date}.pdf"'
                ),
                "X-Report-New-Issues": str(
                    meta["new_issue_count"],
                ),
                "X-Report-Affected-Operatives": str(
                    meta["affected_operatives"],
                ),
                "X-Report-Affected-Forms": str(
                    meta["affected_forms"],
                ),
            },
        )

    @router.post(
        "/reconcile-daily-form-issue-cases",
        summary="Reconcile open Form Issue Cases against fresh EasyBOP extracts",
    )
    async def reconcile_daily_form_issue_cases(req: DailyIssueReconcileRequest):
        try:
            return await asyncio.to_thread(
                _daily_issue_reconcile.reconcile_open_issue_cases,
                req.issue_cases,
                req.extract_results,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("reconcile-daily-form-issue-cases")
            raise HTTPException(
                status_code=500,
                detail="Daily issue case reconciliation failed.",
            ) from exc

    @router.post("/reconcile-weekly-form-issues")
    async def reconcile_weekly_form_issues(req: WeeklyFormIssuesRequest):
        try:
            ojs_rows = [dict(r) for r in req.ojs_rows if isinstance(r, dict)]
            ojs_rows_before = [dict(r) for r in ojs_rows]
            refresh_ids: set[str] = set()
            if req.refresh_from_easybop:
                refresh_ids = set(
                    _weekly_performance_report.collect_refresh_ids(
                        req.issue_cases,
                        week_days=req.week_days,
                    )
                )
            return await asyncio.to_thread(
                _weekly_performance_report.reconcile_weekly_form_issues,
                req.issue_cases,
                ojs_rows,
                ojs_rows_before=ojs_rows_before,
                refreshed_ids=refresh_ids,
                week_days=req.week_days,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("reconcile-weekly-form-issues")
            raise HTTPException(
                status_code=500,
                detail="Weekly form issue reconciliation failed.",
            ) from exc

    @router.post(
        "/weekly-performance-from-sheet",
        summary="Summarize weekly operative performance from Form Issue Cases only",
    )
    async def weekly_performance_from_sheet(req: WeeklySheetSummaryRequest):
        try:
            return await asyncio.to_thread(
                _weekly_performance_report.summarize_weekly_from_issue_cases,
                req.issue_cases,
                week_days=req.week_days,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("weekly-performance-from-sheet")
            raise HTTPException(
                status_code=500,
                detail="Weekly sheet summary failed.",
            ) from exc

    @router.post(
        "/generate-weekly-performance-report-pdf",
        response_class=Response,
    )
    async def generate_weekly_performance_report_pdf(
        req: WeeklyFormPerformanceReportRequest,
    ):
        try:
            pdf_bytes, meta = await asyncio.to_thread(
                _weekly_performance_report.build_weekly_performance_report_pdf,
                req.model_dump(mode="python"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("generate-weekly-performance-report-pdf")
            raise HTTPException(
                status_code=500,
                detail="Weekly performance PDF generation failed.",
            ) from exc

        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": (
                    'attachment; '
                    f'filename="weekly-form-performance-{meta["date_slug"]}.pdf"'
                ),
                "X-Report-Issues-In-Week": str(meta["issues_in_week"]),
                "X-Report-Resolved-In-Week": str(meta["resolved_in_week"]),
                "X-Report-Still-Open-In-Week": str(
                    meta["still_open_in_week"],
                ),
            },
        )

    return router
