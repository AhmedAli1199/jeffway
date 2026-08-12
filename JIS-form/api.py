from __future__ import annotations

import base64
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi import APIRouter, HTTPException
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


_config = _load_local_module("_easybop_jis_config", "config.py")
_sheet_loader = _load_local_module("_easybop_jis_sheet_loader", "sheet_loader.py")
sys.modules["sheet_loader"] = _sheet_loader
_appointment_loader = _load_local_module("_easybop_jis_appointment_loader", "appointment_loader.py")
sys.modules["appointment_loader"] = _appointment_loader
_appointment_automation = _load_local_module(
    "_easybop_jis_appointment_automation", "appointment_automation.py"
)
sys.modules["appointment_automation"] = _appointment_automation
_automation = _load_local_module("_easybop_jis_automation", "automation.py")
sys.modules["automation"] = _automation
try:
    _task_automation = _load_local_module("_easybop_jis_task_automation", "task_automation.py")
finally:
    sys.modules.pop("automation", None)

settings = _config.settings
fill_jis_planned_works = _automation.fill_jis_planned_works
add_qs_review_task_from_row = _task_automation.add_qs_review_task_from_row
load_jis_row_for_example = _sheet_loader.load_jis_row_for_example
load_jis_row_from_google_url = _sheet_loader.load_jis_row_from_google_url
load_jis_rows_from_google_url = _sheet_loader.load_jis_rows_from_google_url
parse_uprn_from_q12 = _appointment_loader.parse_uprn_from_q12
load_appointment_row_from_google_url = _appointment_loader.load_appointment_row_from_google_url

logger = logging.getLogger("easybop.jis.api")

_APPOINTMENT_SHEET_KEYS = frozenset(
    {
        "UPRN",
        "7.1",
        "7.2",
        "8.1",
        "8.2",
        "9.1",
        "9.2",
        "10.1",
        "10.2",
        "11.1",
        "11.2",
        "Trade_1",
        "Hours_1",
        "Notes_1",
        "Trade_2",
        "Hours_2",
        "Notes_2",
        "Trade_3",
        "Hours_3",
        "Notes_3",
        "Trade_4",
        "Hours_4",
        "Notes_4",
        "Trade_5",
        "Hours_5",
        "Notes_5",
    }
)


class FillJisRequest(BaseModel):
    google_sheet_url: Optional[str] = Field(
        None,
        description=(
            "Full Google Sheets URL including gid= for the tab (e.g. SOR-Codes-Template). "
            "Exports via CSV; spreadsheet must allow link-based viewing. Defaults to JIS_GOOGLE_SHEET_URL."
        ),
    )
    excel_path: Optional[str] = Field(
        None,
        description="Path to SOR-Codes-Template.xlsx when not using Google Sheets (set JIS_GOOGLE_SHEET_URL= to disable default Sheet URL).",
    )
    sheet_name: str = Field("Sheet3", description="Worksheet name for local xlsx only (default Sheet3).")
    example_number: int = Field(3, description="Value in the Example # column (e.g. 3).")
    contract_id: Optional[str] = Field(
        None,
        description="contract_id for index.php (default from JIS_CONTRACT_ID / 321129).",
    )
    submit: bool = Field(False, description="If true, click the first form submit on the page.")
    open_job_information_sheet: bool = Field(
        True,
        description=(
            "After Q1.3 search: click work-instruction pre-item dialog, then open "
            "'Job information sheet' smart form (works_ajax_openSmartForm)."
        ),
    )
    smart_form_job_info_id: Optional[str] = Field(
        None,
        description="data-smart-form-id for Job information sheet (default JIS_SMART_FORM_JOB_INFO_ID / 17799).",
    )
    pre_item_item_id: Optional[str] = Field(
        None,
        description=(
            "EasyBOP list-row item_id inside data-item-args for the JIS 'Click on me' link "
            "(e.g. 27059). Overrides sheet column / JIS_PRE_ITEM_ITEM_ID."
        ),
    )
    pre_item_works_id: Optional[str] = Field(
        None,
        description="Optional works_id filter for the same link (data-item-args).",
    )
    pre_item_item_type: Optional[str] = Field(
        None,
        description="Optional item_type filter (default from JIS_PRE_ITEM_ITEM_TYPE, usually wi_pw). Pass '' to disable.",
    )
    keep_page_open: Optional[bool] = Field(
        None,
        description="Leave browser tab open after fill; default from EASYBOP_KEEP_PAGE_OPEN.",
    )
    skip_questions: List[str] = Field(
        default_factory=list,
        description=(
            "Optional list of JIS question keys to skip during fill "
            "(e.g. ['Q5.1', 'Q5.2'])."
        ),
    )
    row_data: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Optional inline Q1.1–Q5.2 values (from n8n). May include nested "
            "appointment_row for Q7.1–Q11.2. Q1.2 is used to pick the correct search result."
        ),
    )
    return_to_index_after: bool = Field(
        False,
        description=(
            "After fill, navigate back to planned works index and clear #search_dwelling "
            "(for manual multi-row testing in one browser session)."
        ),
    )
    fill_appointments: bool = Field(
        True,
        description="After Q1–Q5 fill, fill Q7.1–Q11.2 appointment blocks from appointment sheet row.",
    )
    appointment_google_sheet_url: Optional[str] = Field(
        None,
        description=(
            "Google Sheet tab with appointment columns (gid 1360938164). "
            "Row matched by UPRN from Q1.2 or inline appointment_row / Trade_1 columns."
        ),
    )
    appointment_row: Optional[Dict[str, Optional[str]]] = Field(
        None,
        description="Optional inline appointment row (Trade_1, Hours_1, Notes_1, 7.2, …).",
    )


class FillJisBatchRequest(BaseModel):
    rows: List[Dict[str, Any]] = Field(
        ...,
        description=(
            "List of JIS row dicts (Q1.1–Q5.2 keys). Each must include Q1.3. "
            "May include nested appointment_row (Trade_1, Hours_1, 7.2, …) from n8n."
        ),
    )
    contract_id: Optional[str] = Field(None, description="contract_id for index.php (default 321129).")
    submit: bool = Field(True, description="If true, click Save on each row's form (toolbar Save → Complete → block Save).")
    open_job_information_sheet: bool = Field(True, description="Open Job information sheet smart form per row.")
    smart_form_job_info_id: Optional[str] = Field(None, description="Smart form id override per row.")
    pre_item_item_id: Optional[str] = Field(None, description="pre_item item_id override.")
    pre_item_works_id: Optional[str] = Field(None, description="pre_item works_id override.")
    pre_item_item_type: Optional[str] = Field(None, description="pre_item item_type override.")
    keep_page_open: Optional[bool] = Field(
        None,
        description="Leave browser open after entire batch (default false for batch).",
    )
    skip_questions: List[str] = Field(default_factory=list, description="e.g. ['Q5.1','Q5.2']")
    return_to_index_between_rows: bool = Field(
        True,
        description=(
            "After each row (except when batch fails hard), return to planned works index "
            "before the next Q1.3 search. Ignored when rows include works_id (direct WI navigation)."
        ),
    )
    use_works_id_navigation: bool = Field(
        False,
        description=(
            "When true (or when any row has works_id), navigate to "
            "works_details.php?tab_nav_item=wi per row instead of index search."
        ),
    )
    fill_appointments: bool = Field(
        True,
        description="After Q1–Q5 per row, fill Q7.1–Q11.2 from appointment data.",
    )
    appointment_google_sheet_url: Optional[str] = Field(
        None,
        description="Appointment tab URL for lookup when rows lack Trade_1 / appointment_row.",
    )


class FillJisBatchRowResult(BaseModel):
    index: int
    q13: Optional[str] = None
    success: bool
    message: str
    fields_filled: int = 0
    appointment_slots_filled: int = 0
    warnings: List[str] = Field(default_factory=list)
    row_preview: Dict[str, Any] = Field(default_factory=dict)


class FillJisResponse(BaseModel):
    success: bool
    message: str
    fields_filled: int = 0
    warnings: List[str] = Field(default_factory=list)
    smart_form_url: Optional[str] = Field(
        None,
        description="Detected EasyBOP SmartForm URL (/SmartForms/...) used for Q fills when Job information sheet opens.",
    )
    row_preview: Dict[str, Any] = Field(default_factory=dict)
    screenshot_b64: Optional[str] = Field(
        None,
        description="PNG screenshot of the page where filling ran (usually the SmartForm tab).",
    )


class AddTaskRequest(BaseModel):
    google_sheet_url: Optional[str] = Field(
        None,
        description="Google Sheet URL (with gid) to load row values from.",
    )
    excel_path: Optional[str] = Field(
        None,
        description="Local xlsx path when not using Google Sheet.",
    )
    sheet_name: str = Field("Sheet3", description="Worksheet name for local xlsx.")
    example_number: int = Field(5, description="Example # row to load.")
    contract_id: Optional[str] = Field(
        None,
        description="contract_id for index.php (default from JIS_CONTRACT_ID / 321129).",
    )
    keep_page_open: Optional[bool] = Field(
        None,
        description="Leave browser tab open after task preparation; default from EASYBOP_KEEP_PAGE_OPEN.",
    )
    q13_search_text: Optional[str] = Field(
        None,
        description="Optional override for Q1.3 (dwelling/order search). If set with q52_description, sheet loading is skipped.",
    )
    q52_description: Optional[str] = Field(
        None,
        description="Optional override for task description. If set with q13_search_text, sheet loading is skipped.",
    )
    qs_task_description: Optional[str] = Field(
        None,
        description="Optional explicit QS-Task description override (preferred over q52_description).",
    )


class AddTaskResponse(BaseModel):
    success: bool
    message: str
    category: str
    row_preview: Dict[str, Optional[str]] = Field(default_factory=dict)
    screenshot_b64: Optional[str] = None


class FillJisBatchResponse(BaseModel):
    success: bool
    message: str
    rows_processed: int = 0
    results: List[FillJisBatchRowResult] = Field(default_factory=list)


def _normalize_row_values(raw: Dict[str, object]) -> Dict[str, object]:
    out: Dict[str, object] = {}
    for k, v in raw.items():
        if k == "appointment_row" and isinstance(v, dict):
            out[k] = {
                sk: (str(sv).strip() if sv is not None else None)
                for sk, sv in v.items()
            }
        elif v is not None and not isinstance(v, (dict, list)):
            out[k] = str(v).strip()
        elif v is not None:
            out[k] = v
    return out


def _inline_appointment_row(row: Dict[str, Any]) -> Optional[Dict[str, Optional[str]]]:
    nested = row.get("appointment_row")
    if isinstance(nested, dict) and any(str(v or "").strip() for v in nested.values()):
        return {k: (str(v).strip() if v is not None else None) for k, v in nested.items()}

    appt: Dict[str, Optional[str]] = {}
    for k, v in row.items():
        if k in _APPOINTMENT_SHEET_KEYS and v is not None and str(v).strip():
            appt[k] = str(v).strip()
    if (appt.get("Trade_1") or "").strip():
        return appt
    return None


def _resolve_appointment_row(
    row: Dict[str, Any],
    *,
    appointment_google_sheet_url: Optional[str],
) -> Optional[Dict[str, Optional[str]]]:
    inline = _inline_appointment_row(row)
    if inline:
        return inline
    gurl = (appointment_google_sheet_url or settings.jis_appointment_google_sheet_url or "").strip()
    if not gurl:
        return None
    uprn = str(row.get("UPRN") or parse_uprn_from_q12(str(row.get("Q1.2") or "")) or "").strip()
    job_no = str(row.get("Q1.3") or "").strip()
    if not uprn and not job_no:
        return None
    try:
        return load_appointment_row_from_google_url(gurl, uprn=uprn or None, job_no=job_no or None)
    except Exception as e:
        logger.warning("appointment sheet lookup failed: %s", e)
        return None


def _jis_q_fill_order(skip_questions: List[str]) -> List[str]:
    default_q_order = [
        "Q1.1",
        "Q1.2",
        "Q1.3",
        "Q1.4",
        "Q1.5",
        "Q1.6",
        "Q2.1",
        "Q2.2",
        "Q3.1",
        "Q3.2",
        "Q4.1",
        "Q4.2",
        "Q5.1",
        "Q5.2",
    ]
    skip_set = {"Q5.1", "Q5.2"}
    skip_set.update({q.strip().upper() for q in skip_questions if isinstance(q, str) and q.strip()})
    return [q for q in default_q_order if q.upper() not in skip_set]


def create_router(
    *,
    get_context: Callable[[], BrowserContext],
    require_browser: Callable[[], None],
    save_session: Callable[[], Awaitable[None]],
) -> APIRouter:
    router = APIRouter(tags=["JIS"])

    @router.post("/fill-jis-from-sheet", response_model=FillJisResponse)
    async def fill_jis_from_sheet(req: FillJisRequest):
        require_browser()
        gurl = (req.google_sheet_url or settings.jis_google_sheet_url or "").strip()
        raw_path = (req.excel_path or settings.jis_excel_path or "").strip()

        try:
            if req.row_data and isinstance(req.row_data, dict) and any(
                str(v or "").strip() for v in req.row_data.values()
            ):
                row = _normalize_row_values(req.row_data)
            elif gurl:
                row = load_jis_row_from_google_url(gurl, req.example_number)
            elif raw_path:
                excel = Path(raw_path)
                if not excel.is_absolute():
                    excel = (Path.cwd() / excel).resolve()
                row = load_jis_row_for_example(excel, req.sheet_name, req.example_number)
            else:
                raise HTTPException(
                    status_code=400,
                    detail="Set google_sheet_url (or JIS_GOOGLE_SHEET_URL), or excel_path / JIS_EXCEL_PATH for a local xlsx.",
                )
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except (KeyError, LookupError, ValueError) as e:
            raise HTTPException(status_code=422, detail=str(e))

        cid = (req.contract_id or settings.jis_contract_id).strip()
        eff_item = (
            req.pre_item_item_id
            or row.get("pre_item_item_id")
            or settings.jis_pre_item_item_id
            or ""
        ).strip() or None
        eff_works = (
            req.pre_item_works_id or row.get("pre_item_works_id") or settings.jis_pre_item_works_id or ""
        ).strip() or None
        if req.pre_item_item_type is not None:
            eff_type = req.pre_item_item_type.strip() or None
        else:
            eff_type = settings.jis_pre_item_item_type
        keep_open = settings.keep_page_open_after_fill if req.keep_page_open is None else req.keep_page_open
        q_fill_order = _jis_q_fill_order(req.skip_questions)
        appt_row = req.appointment_row if req.appointment_row else None
        if not appt_row and req.fill_appointments:
            appt_row = _resolve_appointment_row(
                row,
                appointment_google_sheet_url=req.appointment_google_sheet_url,
            )
        page = None
        try:
            page = await get_context().new_page()
            errors, filled, appt_filled, smart_form_url, fill_page = await fill_jis_planned_works(
                page,
                row,
                contract_id=cid,
                smart_form_job_info_id=(
                    req.smart_form_job_info_id or settings.jis_smart_form_job_info_id
                ).strip(),
                open_job_information_sheet=req.open_job_information_sheet,
                pre_item_item_id=eff_item,
                pre_item_works_id=eff_works,
                pre_item_item_type=eff_type,
                submit=req.submit,
                timeout_ms=settings.request_timeout_ms,
                relogin_username=settings.username,
                relogin_password=settings.password,
                after_relogin=save_session,
                q_fill_order=q_fill_order,
                return_to_index_after=req.return_to_index_after,
                fill_appointments=req.fill_appointments and bool(appt_row),
                appointment_row=appt_row,
            )
            screenshot_b64 = base64.b64encode(await fill_page.screenshot(full_page=True)).decode()
            msg = "JIS filled" if not errors else f"JIS filled with {len(errors)} warning(s)"
            if appt_filled:
                msg += f" ({appt_filled} appointment slot(s))"
            if keep_open:
                msg += " — tab left open."
            return FillJisResponse(
                success=(filled + appt_filled) > 0,
                message=msg,
                fields_filled=filled + appt_filled,
                warnings=errors,
                smart_form_url=smart_form_url,
                row_preview=row,
                screenshot_b64=screenshot_b64,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=401, detail=str(e))
        except Exception:
            logger.exception("fill-jis-from-sheet")
            raise HTTPException(status_code=500, detail="Internal error — see server logs.")
        finally:
            if page is not None and not keep_open:
                await page.close()

    @router.post("/fill-jis-batch", response_model=FillJisBatchResponse)
    async def fill_jis_batch(req: FillJisBatchRequest):
        require_browser()
        if not req.rows:
            raise HTTPException(status_code=400, detail="rows must be a non-empty list")

        cid = (req.contract_id or settings.jis_contract_id).strip()
        logger.info("fill-jis-batch: rows=%s submit=%s", len(req.rows), req.submit)
        keep_open = False if req.keep_page_open is None else req.keep_page_open
        q_fill_order = _jis_q_fill_order(req.skip_questions)
        eff_item = (req.pre_item_item_id or settings.jis_pre_item_item_id or "").strip() or None
        eff_works = (req.pre_item_works_id or settings.jis_pre_item_works_id or "").strip() or None
        if req.pre_item_item_type is not None:
            eff_type = req.pre_item_item_type.strip() or None
        else:
            eff_type = settings.jis_pre_item_item_type

        results: List[FillJisBatchRowResult] = []
        page = None
        batch_uses_works_id = req.use_works_id_navigation or any(
            str((r or {}).get("works_id") or "").strip() for r in req.rows
        )
        try:
            page = await get_context().new_page()
            for idx, raw_row in enumerate(req.rows):
                row = _normalize_row_values(raw_row or {})
                q13 = (row.get("Q1.3") or "").strip()
                row_works_id = str(row.get("works_id") or "").strip() or None
                if not q13:
                    results.append(
                        FillJisBatchRowResult(
                            index=idx,
                            q13=None,
                            success=False,
                            message="Skipped: Q1.3 empty",
                            row_preview=row,
                        )
                    )
                    continue

                appt_row = None
                if req.fill_appointments:
                    appt_row = _resolve_appointment_row(
                        row,
                        appointment_google_sheet_url=req.appointment_google_sheet_url,
                    )
                try:
                    fill_kwargs = dict(
                        contract_id=cid,
                        smart_form_job_info_id=(
                            req.smart_form_job_info_id or settings.jis_smart_form_job_info_id
                        ).strip(),
                        open_job_information_sheet=req.open_job_information_sheet,
                        pre_item_item_id=eff_item,
                        pre_item_works_id=row_works_id or eff_works,
                        pre_item_item_type=eff_type,
                        works_id=row_works_id,
                        submit=req.submit,
                        timeout_ms=settings.request_timeout_ms,
                        relogin_username=settings.username,
                        relogin_password=settings.password,
                        after_relogin=save_session,
                        q_fill_order=q_fill_order,
                        return_to_index_after=False,
                        fill_appointments=req.fill_appointments and bool(appt_row),
                        appointment_row=appt_row,
                    )
                    errors, filled, appt_filled, _sf, _fp = None, 0, 0, None, page
                    for attempt in range(2):
                        try:
                            errors, filled, appt_filled, _sf, _fp = await fill_jis_planned_works(
                                page, row, **fill_kwargs
                            )
                            break
                        except RuntimeError as e:
                            expired = _automation.url_indicates_session_expired(
                                page.url or ""
                            ) or await _automation.page_looks_like_login_screen(page)
                            if attempt == 0 and expired:
                                logger.warning(
                                    "fill-jis-batch: session expired (%s) — re-login, retry row %s",
                                    page.url,
                                    idx,
                                )
                                if row_works_id:
                                    await _automation.navigate_to_works_wi_page(
                                        page,
                                        row_works_id,
                                        cid,
                                        settings.request_timeout_ms,
                                        relogin_username=settings.username,
                                        relogin_password=settings.password,
                                        after_relogin=save_session,
                                    )
                                else:
                                    await _automation.ensure_jis_index_ready(
                                        page,
                                        cid,
                                        settings.username,
                                        settings.password,
                                        settings.request_timeout_ms,
                                        after_relogin=save_session,
                                    )
                                continue
                            raise
                    if (
                        req.return_to_index_between_rows
                        and not batch_uses_works_id
                        and idx < len(req.rows) - 1
                    ):
                        page = await _automation.return_to_planned_works_index(
                            page,
                            cid,
                            settings.request_timeout_ms,
                            relogin_username=settings.username,
                            relogin_password=settings.password,
                            after_relogin=save_session,
                        )
                    msg = "JIS filled" if not errors else f"JIS filled with {len(errors)} warning(s)"
                    if appt_filled:
                        msg += f" ({appt_filled} appointment slot(s))"
                    fields_total = filled + appt_filled
                    results.append(
                        FillJisBatchRowResult(
                            index=idx,
                            q13=q13,
                            success=fields_total > 0,
                            message=msg,
                            fields_filled=fields_total,
                            appointment_slots_filled=appt_filled,
                            warnings=errors,
                            row_preview=row,
                        )
                    )
                except (ValueError, RuntimeError) as e:
                    results.append(
                        FillJisBatchRowResult(
                            index=idx,
                            q13=q13,
                            success=False,
                            message=str(e),
                            row_preview=row,
                        )
                    )
                    if req.return_to_index_between_rows and not batch_uses_works_id:
                        try:
                            page = await _automation.return_to_planned_works_index(
                                page,
                                cid,
                                settings.request_timeout_ms,
                                relogin_username=settings.username,
                                relogin_password=settings.password,
                                after_relogin=save_session,
                            )
                        except Exception:
                            logger.exception("fill-jis-batch: return_to_index after row error")

            ok_count = sum(1 for r in results if r.success)
            all_ok = ok_count == len(results) and len(results) > 0
            return FillJisBatchResponse(
                success=all_ok,
                message=f"Batch complete: {ok_count}/{len(results)} row(s) OK",
                rows_processed=len(results),
                results=results,
            )
        except Exception as e:
            logger.exception("fill-jis-batch")
            if results:
                ok_count = sum(1 for r in results if r.success)
                return FillJisBatchResponse(
                    success=False,
                    message=(
                        f"Batch aborted: {ok_count}/{len(results)} row(s) OK before error — {e}"
                    ),
                    rows_processed=len(results),
                    results=results,
                )
            raise HTTPException(status_code=500, detail="Internal error — see server logs.")
        finally:
            if page is not None and not keep_open:
                await page.close()

    @router.post("/add-variation-task-from-sheet", response_model=AddTaskResponse)
    async def add_variation_task_from_sheet(req: AddTaskRequest):
        require_browser()
        row: Dict[str, Optional[str]] = {}
        cid = (req.contract_id or settings.jis_contract_id).strip()
        q13 = (req.q13_search_text or "").strip()
        task_description = (req.qs_task_description or req.q52_description or "").strip()

        if not (q13 and task_description):
            gurl = (req.google_sheet_url or settings.jis_google_sheet_url or "").strip()
            raw_path = (req.excel_path or settings.jis_excel_path or "").strip()
            try:
                if gurl:
                    row = load_jis_row_from_google_url(gurl, req.example_number)
                elif raw_path:
                    excel = Path(raw_path)
                    if not excel.is_absolute():
                        excel = (Path.cwd() / excel).resolve()
                    row = load_jis_row_for_example(excel, req.sheet_name, req.example_number)
                else:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "Provide q13_search_text + q52_description directly, or set google_sheet_url "
                            "(or JIS_GOOGLE_SHEET_URL), or excel_path / JIS_EXCEL_PATH."
                        ),
                    )
            except FileNotFoundError as e:
                raise HTTPException(status_code=404, detail=str(e))
            except (KeyError, LookupError, ValueError) as e:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Could not load example row: {e}. "
                        "Tip: pass q13_search_text and q52_description directly in this endpoint body."
                    ),
                )

            if not q13:
                q13 = (row.get("Q1.3") or "").strip()
            if not task_description:
                task_description = (row.get("QS-Task") or "").strip()
                if not task_description:
                    task_description = (row.get("Q5.1") or "").strip()
                if not task_description:
                    task_description = (row.get("Q5.2") or "").strip()
        keep_open = settings.keep_page_open_after_fill if req.keep_page_open is None else req.keep_page_open

        page = None
        try:
            page = await get_context().new_page()
            q12_val = (row.get("Q1.2") or "").strip() or None
            q12_hint = _automation.parse_address_from_q12(q12_val)
            await add_qs_review_task_from_row(
                page,
                contract_id=cid,
                q13_search_text=q13,
                q52_description=task_description,
                q12_address_hint=q12_hint or None,
                q12_raw=q12_val,
                timeout_ms=settings.request_timeout_ms,
                relogin_username=settings.username,
                relogin_password=settings.password,
                after_relogin=save_session,
            )

            screenshot_b64 = base64.b64encode(await page.screenshot(full_page=True)).decode()
            msg = "Task popup prepared: category selected and Q5.2 description filled."
            if keep_open:
                msg += " — tab left open."
            return AddTaskResponse(
                success=True,
                message=msg,
                category="QS review for variations",
                row_preview=row or {"Q1.3": q13, "Q5.1": task_description},
                screenshot_b64=screenshot_b64,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=401, detail=str(e))
        except Exception:
            logger.exception("add-variation-task-from-sheet")
            raise HTTPException(status_code=500, detail="Internal error — see server logs.")
        finally:
            if page is not None and not keep_open:
                await page.close()

    return router
