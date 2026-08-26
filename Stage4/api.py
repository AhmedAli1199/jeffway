"""
Stage 4 API router.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from playwright.async_api import BrowserContext

_here = Path(__file__).resolve().parent


def _load_local_module(name: str, filename: str):
    path = _here / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load {filename} from {_here}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_pre_works_jis = _load_local_module("_pre_works_jis", "pre_works_jis.py")

run_pre_works_jis = _pre_works_jis.run_pre_works_jis
run_create_contractor_task = _pre_works_jis.run_create_contractor_task
run_create_task = _pre_works_jis.run_create_task
run_add_internal_note = _pre_works_jis.run_add_internal_note
run_add_works_note = _pre_works_jis.run_add_works_note
run_get_job_contact_info = _pre_works_jis.run_get_job_contact_info
run_check_completed_tasks = _pre_works_jis.run_check_completed_tasks
run_process_completed_task = _pre_works_jis.run_process_completed_task
run_book_appointment = _pre_works_jis.run_book_appointment
run_get_staff_availability = _pre_works_jis.run_get_staff_availability
run_get_unallocated_jobs = _pre_works_jis.run_get_unallocated_jobs
run_verify_scheduled_appointment = _pre_works_jis.run_verify_scheduled_appointment

logger = logging.getLogger("easybop.api")
_book_appointment_lock = asyncio.Lock()


class VerifyScheduledAppointmentRequest(BaseModel):
    contract_id: str = Field("321129", description="EasyBOP contract_id")
    apt_board_id: str = Field("1598", description="Scheduling board ID")
    address: str = Field(..., description="Property address to search (e.g. '8 Hersey Gardens')")
    appointment_number: str = Field("Appointment 1", description="Target appointment number to match (e.g. 'Appointment 1')")
    keep_page_open: Optional[bool] = Field(None, description="Leave Playwright page open after run")


class PreWorksJisRequest(BaseModel):
    contract_id: str = Field("321129", description="EasyBOP contract_id")
    item_id: str = Field("27059", description="JIS item ID (wi_pw UDF column)")
    existing_corfs: Optional[List[Any]] = Field(
        default=None,
        description="Optional list of already booked Client Order References (CORF) or sheet rows to exclude from qualifying jobs"
    )
    date_received_after: Optional[str] = Field(
        default=None,
        description="Optional date DD/MM/YYYY — only include jobs whose Date Received is strictly after this date. Independent of existing_corfs; either or both filters can be used."
    )
    keep_page_open: Optional[bool] = Field(None, description="Leave Playwright page open after run")


class CreateContractorTaskRequest(BaseModel):
    works_id: str = Field(..., description="EasyBOP works_id of the job")
    contract_id: str = Field("321129", description="EasyBOP contract_id")
    keep_page_open: Optional[bool] = Field(None, description="Leave Playwright page open after run")


class CreateTaskRequest(BaseModel):
    works_id: str = Field(..., description="EasyBOP works_id of the job")
    contract_id: str = Field("321129", description="EasyBOP contract_id")
    category: str = Field(..., description="Task category e.g. 'General', 'Book work for contractor'")
    description: str = Field(..., description="Task description / comment text")
    issued_to_query: str = Field(..., description="Autocomplete search text for Issued To, e.g. 'Shannon'")
    issued_to_label: str = Field(..., description="Full name to match in the autocomplete dropdown, e.g. 'Shannon Slade'")
    priority: str = Field("High", description="Task priority e.g. High, Medium, Low")
    due_date: Optional[str] = Field(None, description="Due date DD/MM/YYYY, defaults to today")
    keep_page_open: Optional[bool] = Field(None, description="Leave Playwright page open after run")


class GetJobContactInfoRequest(BaseModel):
    works_id: str = Field(..., description="EasyBOP works_id of the job")
    contract_id: str = Field("321129", description="EasyBOP contract_id")
    keep_page_open: Optional[bool] = Field(None, description="Leave Playwright page open after run")


class AddWorksNoteRequest(BaseModel):
    works_id: str = Field(..., description="EasyBOP works_id of the job")
    contract_id: str = Field("321129", description="EasyBOP contract_id")
    note_text: str = Field(..., description="Text for the note's free-text field")
    category_label: Optional[str] = Field("Telephone Call", description="Category dropdown label, e.g. 'Telephone Call', 'Telephone Call No Answer', 'Voicemail left', 'Appointment Made'")
    date_of: Optional[str] = Field(None, description="Date DD/MM/YYYY — leave unset to keep the dialog's prefilled today's date")
    time_issued: Optional[str] = Field(None, description="Time HH:MM — leave unset to keep the dialog's prefilled current time")
    rlo_visit: Optional[bool] = Field(None, description="RLO Visit Yes/No — leave unset to keep the dialog's default (No)")
    keep_page_open: Optional[bool] = Field(None, description="Leave Playwright page open after run")


class AddInternalNoteRequest(BaseModel):
    works_id: str = Field(..., description="EasyBOP works_id of the job")
    contract_id: str = Field("321129", description="EasyBOP contract_id")
    note: str = Field(..., description="Text to append to the internal notes field, e.g. 'Vapi call attempt 1 placed 18/08/2026 15:02 — call_id: 8f3a2b1c-...'")
    keep_page_open: Optional[bool] = Field(None, description="Leave Playwright page open after run")


class CheckCompletedTasksRequest(BaseModel):
    category: str = Field("Book work for contractor", description="Task category name to filter by")
    keep_page_open: Optional[bool] = Field(None, description="Leave Playwright page open after run")


class ProcessCompletedTaskRequest(BaseModel):
    works_id: str = Field(..., description="EasyBOP works_id of the job")
    contract_id: str = Field("321129", description="EasyBOP contract_id")
    comment: str = Field(..., description="The comment text from the completed task")
    keep_page_open: Optional[bool] = Field(None, description="Leave Playwright page open after run")


class BookAppointmentRequest(BaseModel):
    works_id: str = Field(..., description="EasyBOP works_id of the job")
    contract_id: str = Field("321129", description="EasyBOP contract_id")
    reason: str = Field(..., description="Reason for appointment / Trade description")
    duration_hours: Any = Field(2.0, description="Duration hours e.g. 3, 2, 4, 11, '3', '03:00'")
    note: Optional[str] = Field(None, description="Optional appointment note e.g. 'Appointment # 1' or 'Appointment # 2'")
    trade: Optional[str] = Field(None, description="Optional trade type e.g. 'multi/trade', 'electrical'")
    keep_page_open: Optional[bool] = Field(None, description="Leave Playwright page open after run")


class StaffAvailabilityRequest(BaseModel):
    contract_id: str = Field("321129", description="EasyBOP contract_id")
    target_date: Optional[str] = Field(None, description="Start date DD/MM/YYYY (defaults to today)")
    staff_names: Optional[List[str]] = Field(
        default=None,
        description="Optional list of staff names to check availability for (defaults to ALL staff if empty/omitted)"
    )
    required_hours: float = Field(2.0, description="Required appointment duration in hours")
    days_count: int = Field(5, description="Number of working days to check (default 5)")
    keep_page_open: Optional[bool] = Field(None, description="Leave Playwright page open after run")


class UnallocatedJobsRequest(BaseModel):
    contract_id: str = Field("321129", description="EasyBOP contract_id")
    apt_board_id: str = Field("1598", description="Appointments board ID (default 1598)")
    keep_page_open: Optional[bool] = Field(None, description="Leave Playwright page open after run")


def create_router(
    *,
    get_context: Callable[[], BrowserContext],
    require_browser: Callable[[], None],
    login_fn: Any,
    username: str,
    password: str,
    timeout_ms: int,
    easybop_base_url: str = "https://easybop.co.uk",
    session_path: Any,
    default_keep_page_open: bool = False,
) -> APIRouter:
    router = APIRouter(prefix="/stage4", tags=["Stage 4 — Pre-Works JIS Automation"])

    @router.post(
        "/qualifying-jobs",
        summary="Fetch qualifying pre-works jobs and extract JIS appointment info",
        description=(
            "Navigates to pre_works report, filters jobs with status 'Pre-completion Items To Complete' "
            "and green JIS cell, extracts smart_form_id and JIS dataset fields, classifies appointments "
            "as subcontractor or in-house, and returns tenant mobile number alongside appointment objects."
        ),
    )
    async def pre_works_jis(req: PreWorksJisRequest):
        require_browser()
        context = get_context()

        keep = default_keep_page_open if req.keep_page_open is None else req.keep_page_open

        page = await context.new_page()
        try:
            result = await run_pre_works_jis(
                page,
                contract_id=req.contract_id,
                item_id=req.item_id,
                existing_corfs=req.existing_corfs,
                date_received_after=req.date_received_after,
                username=username,
                password=password,
                timeout_ms=timeout_ms,
                login_fn=login_fn,
                session_path=session_path,
                context=context,
            )
            return {
                "success": result.get("success", False),
                **result,
            }
        except RuntimeError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except Exception:
            logger.exception("stage4-qualifying-jobs")
            raise HTTPException(status_code=500, detail="Failed — see server logs.")
        finally:
            if not keep:
                await page.close()

    @router.post(
        "/create-contractor-task",
        summary="Create a high priority task for Shannon Slade",
        description=(
            "Navigates to the job's Tasks tab and creates a High priority task for Shannon Slade "
            "under category 'Book work for contractor' with due date set to today."
        ),
    )
    async def create_contractor_task(req: CreateContractorTaskRequest):
        require_browser()
        context = get_context()

        keep = default_keep_page_open if req.keep_page_open is None else req.keep_page_open

        page = await context.new_page()
        try:
            result = await run_create_contractor_task(
                page,
                contract_id=req.contract_id,
                works_id=req.works_id,
                username=username,
                password=password,
                timeout_ms=timeout_ms,
                login_fn=login_fn,
                session_path=session_path,
                context=context,
            )
            return {
                "success": result.get("success", False),
                **result,
            }
        except RuntimeError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except Exception:
            logger.exception("stage4-create-contractor-task")
            raise HTTPException(status_code=500, detail="Failed — see server logs.")
        finally:
            if not keep:
                await page.close()

    @router.post(
        "/create-task",
        summary="Create a task on a job's Tasks tab with a custom category, assignee, and description",
        description=(
            "Generic version of /create-contractor-task. Use for notifying the scheduling/admin "
            "team about voice-call outcomes — e.g. an appointment the tenant rescheduled, a "
            "scheduling conflict where no offered slot worked for the tenant, or a refusal — "
            "where the fixed 'Book work for contractor' / Shannon Slade task doesn't apply."
        ),
    )
    async def create_task(req: CreateTaskRequest):
        require_browser()
        context = get_context()

        keep = default_keep_page_open if req.keep_page_open is None else req.keep_page_open

        page = await context.new_page()
        try:
            result = await run_create_task(
                page,
                contract_id=req.contract_id,
                works_id=req.works_id,
                category=req.category,
                description=req.description,
                issued_to_query=req.issued_to_query,
                issued_to_label=req.issued_to_label,
                priority=req.priority,
                due_date=req.due_date,
                username=username,
                password=password,
                timeout_ms=timeout_ms,
                login_fn=login_fn,
                session_path=session_path,
                context=context,
            )
            return {
                "success": result.get("success", False),
                **result,
            }
        except RuntimeError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except Exception:
            logger.exception("stage4-create-task")
            raise HTTPException(status_code=500, detail="Failed — see server logs.")
        finally:
            if not keep:
                await page.close()

    @router.post(
        "/job-contact-info",
        summary="Fetch tenant mobile/email + raw header text for a works order",
        description=(
            "Navigates to the works Details tab and scrapes tenant mobile/email from the .box_title_item "
            "header, for use by SMS/voice notification steps that need contact details not otherwise "
            "available in the calling n8n workflow. Also returns page_h1/header_text as raw fields since "
            "tenant name/address aren't consistently labelled — inspect these to refine parsing if needed."
        ),
    )
    async def job_contact_info(req: GetJobContactInfoRequest):
        require_browser()
        context = get_context()

        keep = default_keep_page_open if req.keep_page_open is None else req.keep_page_open

        page = await context.new_page()
        try:
            result = await run_get_job_contact_info(
                page,
                works_id=req.works_id,
                contract_id=req.contract_id,
                username=username,
                password=password,
                timeout_ms=timeout_ms,
                login_fn=login_fn,
                session_path=session_path,
                context=context,
            )
            return {
                "success": result.get("success", False),
                **result,
            }
        except RuntimeError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except Exception:
            logger.exception("stage4-job-contact-info")
            raise HTTPException(status_code=500, detail="Failed — see server logs.")
        finally:
            if not keep:
                await page.close()

    @router.post(
        "/add-works-note",
        summary="Add a formal Note entry via the Notes tab's 'Add New Note' dialog",
        description=(
            "Navigates to the works Notes tab, opens the 'Add New Note' dialog, sets category/date/time/"
            "RLO fields as provided, fills the free-text note, and saves. Distinct from /add-internal-note, "
            "which appends to the plain internal_notes textarea on the Details tab instead."
        ),
    )
    async def add_works_note(req: AddWorksNoteRequest):
        require_browser()
        context = get_context()

        keep = default_keep_page_open if req.keep_page_open is None else req.keep_page_open

        page = await context.new_page()
        try:
            result = await run_add_works_note(
                page,
                works_id=req.works_id,
                contract_id=req.contract_id,
                note_text=req.note_text,
                category_label=req.category_label,
                date_of=req.date_of,
                time_issued=req.time_issued,
                rlo_visit=req.rlo_visit,
                username=username,
                password=password,
                timeout_ms=timeout_ms,
                login_fn=login_fn,
                session_path=session_path,
                context=context,
            )
            return {
                "success": result.get("success", False),
                **result,
            }
        except RuntimeError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except Exception:
            logger.exception("stage4-add-works-note")
            raise HTTPException(status_code=500, detail="Failed — see server logs.")
        finally:
            if not keep:
                await page.close()

    @router.post(
        "/add-internal-note",
        summary="Append a line to a job's internal notes (Details tab) and save",
        description=(
            "Navigates to the works Details tab, appends the given text to the internal_notes field, "
            "and saves. Use this to log Vapi voice-call attempts and call IDs against the job so the "
            "team can later pull a transcript/recording from Vapi by call_id."
        ),
    )
    async def add_internal_note(req: AddInternalNoteRequest):
        require_browser()
        context = get_context()

        keep = default_keep_page_open if req.keep_page_open is None else req.keep_page_open

        page = await context.new_page()
        try:
            result = await run_add_internal_note(
                page,
                works_id=req.works_id,
                contract_id=req.contract_id,
                note=req.note,
                username=username,
                password=password,
                timeout_ms=timeout_ms,
                login_fn=login_fn,
                session_path=session_path,
                context=context,
            )
            return {
                "success": result.get("success", False),
                **result,
            }
        except RuntimeError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except Exception:
            logger.exception("stage4-add-internal-note")
            raise HTTPException(status_code=500, detail="Failed — see server logs.")
        finally:
            if not keep:
                await page.close()

    @router.post(
        "/check-completed-tasks",
        summary="Check tasks issued by me for completed tasks by category",
        description=(
            "Navigates to tasks_issued_by_me.php, filters by category ('Book work for contractor'), "
            "searches, and extracts all completed task details including completed comments and works_id."
        ),
    )
    async def check_completed_tasks(req: CheckCompletedTasksRequest):
        require_browser()
        context = get_context()

        keep = default_keep_page_open if req.keep_page_open is None else req.keep_page_open

        page = await context.new_page()
        try:
            result = await run_check_completed_tasks(
                page,
                category=req.category,
                username=username,
                password=password,
                timeout_ms=timeout_ms,
                login_fn=login_fn,
                session_path=session_path,
                context=context,
            )
            return {
                "success": result.get("success", False),
                **result,
            }
        except RuntimeError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except Exception:
            logger.exception("stage4-check-completed-tasks")
            raise HTTPException(status_code=500, detail="Failed — see server logs.")
        finally:
            if not keep:
                await page.close()

    @router.post(
        "/process-completed-task",
        summary="Process a completed task comment, update notes and UDFs",
        description=(
            "Navigates to the works details tab, appends the comment to internal notes, and saves. "
            "Then navigates to the works UDF tab, extracts date component from the comment, enters it "
            "into the date input field, and sets the multi-select dropdown to 'Appointment booked'."
        ),
    )
    async def process_completed_task(req: ProcessCompletedTaskRequest):
        require_browser()
        context = get_context()

        keep = default_keep_page_open if req.keep_page_open is None else req.keep_page_open

        page = await context.new_page()
        try:
            result = await run_process_completed_task(
                page,
                works_id=req.works_id,
                contract_id=req.contract_id,
                comment=req.comment,
                username=username,
                password=password,
                timeout_ms=timeout_ms,
                login_fn=login_fn,
                session_path=session_path,
                context=context,
            )
            return {
                "success": result.get("success", False),
                **result,
            }
        except RuntimeError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except Exception:
            logger.exception("stage4-process-completed-task")
            raise HTTPException(status_code=500, detail="Failed — see server logs.")
        finally:
            if not keep:
                await page.close()

    @router.post(
        "/book-appointment",
        summary="Create an unallocated appointment on EasyBOP (lands in Jobs to allocate)",
        description=(
            "Navigates to process monitor tab, clicks 'Add Other Appointment', fills out reason and "
            "appointment period duration (e.g. '02:00', '04:00'), selects workboard 'Appointments', "
            "and saves so the job appears in the Scheduling Register 'Jobs to allocate' list."
        ),
    )
    async def book_appointment(req: BookAppointmentRequest):
        require_browser()
        context = get_context()

        keep = default_keep_page_open if req.keep_page_open is None else req.keep_page_open

        async with _book_appointment_lock:
            page = await context.new_page()
            try:
                result = await run_book_appointment(
                    page,
                    works_id=req.works_id,
                    contract_id=req.contract_id,
                    reason=req.reason,
                    duration_hours=req.duration_hours,
                    note=req.note,
                    trade=req.trade,
                    username=username,
                    password=password,
                    timeout_ms=timeout_ms,
                    login_fn=login_fn,
                    session_path=session_path,
                    context=context,
                )
                return {
                    "success": result.get("success", False),
                    **result,
                }
            except RuntimeError as e:
                raise HTTPException(status_code=422, detail=str(e)) from e
            except Exception:
                logger.exception("stage4-book-appointment")
                raise HTTPException(status_code=500, detail="Failed — see server logs.")
            finally:
                if not keep:
                    await page.close()

    @router.post(
        "/staff-availability",
        summary="Get staff member timeline availability from the Scheduling Register",
        description=(
            "Navigates to the Scheduling Register board (scheduling.php) and inspects FullCalendar "
            "timeline rows to extract busy event blocks and availability for a list of staff names."
        ),
    )
    async def staff_availability(req: StaffAvailabilityRequest):
        require_browser()
        context = get_context()

        keep = default_keep_page_open if req.keep_page_open is None else req.keep_page_open

        page = await context.new_page()
        try:
            result = await run_get_staff_availability(
                page,
                contract_id=req.contract_id,
                target_date=req.target_date,
                staff_names=req.staff_names,
                required_hours=req.required_hours,
                days_count=req.days_count,
                username=username,
                password=password,
                timeout_ms=timeout_ms,
                login_fn=login_fn,
                session_path=session_path,
                context=context,
            )
            return {
                "success": result.get("success", False),
                **result,
            }
        except RuntimeError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except Exception:
            logger.exception("stage4-staff-availability")
            raise HTTPException(status_code=500, detail="Failed — see server logs.")
        finally:
            if not keep:
                await page.close()

    @router.post(
        "/unallocated-jobs",
        summary="Scrape unallocated jobs/appointments from Scheduling Register board",
        description=(
            "Navigates to the EasyBOP Scheduling Register board (board_type=sch&apt_board_id=1598) "
            "and scrapes all currently unallocated appointment items sitting in the 'Jobs to allocate' side panel."
        ),
    )
    async def unallocated_jobs(req: UnallocatedJobsRequest):
        require_browser()
        context = get_context()

        keep = default_keep_page_open if req.keep_page_open is None else req.keep_page_open

        page = await context.new_page()
        try:
            result = await run_get_unallocated_jobs(
                page,
                contract_id=req.contract_id,
                apt_board_id=req.apt_board_id,
                username=username,
                password=password,
                timeout_ms=timeout_ms,
                login_fn=login_fn,
                session_path=session_path,
                context=context,
            )
            return {
                "success": result.get("success", False),
                **result,
            }
        except RuntimeError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except Exception:
            logger.exception("stage4-unallocated-jobs")
            raise HTTPException(status_code=500, detail="Failed — see server logs.")
        finally:
            if not keep:
                await page.close()

    @router.post(
        "/verify-scheduled-appointment",
        summary="Search and verify whether an appointment is scheduled on the board",
        description=(
            "Searches the EasyBOP Scheduling Register board by address, iterates matching rows, "
            "double-clicks each row to inspect note details, and verifies whether the target "
            "appointment_number (e.g. 'Appointment 1') is scheduled on staff calendar."
        ),
    )
    async def verify_scheduled_appointment(req: VerifyScheduledAppointmentRequest):
        require_browser()
        context = get_context()

        keep = default_keep_page_open if req.keep_page_open is None else req.keep_page_open

        page = await context.new_page()
        try:
            result = await run_verify_scheduled_appointment(
                page,
                contract_id=req.contract_id,
                apt_board_id=req.apt_board_id,
                address=req.address,
                appointment_number=req.appointment_number,
                username=username,
                password=password,
                timeout_ms=timeout_ms,
                login_fn=login_fn,
                session_path=session_path,
                context=context,
            )
            return {
                "success": result.get("success", False),
                **result,
            }
        except RuntimeError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except Exception:
            logger.exception("stage4-verify-scheduled-appointment")
            raise HTTPException(status_code=500, detail="Failed — see server logs.")
        finally:
            if not keep:
                await page.close()

    return router
