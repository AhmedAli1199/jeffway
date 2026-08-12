"""Stage 5 API — stalled appointment check."""

from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, HTTPException
from playwright.async_api import BrowserContext
from pydantic import BaseModel, Field

_here = Path(__file__).resolve().parent
_root = _here.parent

if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

from month_filter import months_for_lookback  # noqa: E402
from stalled_check import run_stalled_check  # noqa: E402
from cancelled_appointments_notes import run_cancelled_appointments_notes  # noqa: E402

logger = logging.getLogger("stage5.api")


class StalledCheckRequest(BaseModel):
    contract_id: str = Field("321129", description="EasyBOP contract_id")
    lookback_days: int = Field(14, ge=1, le=90, description="Days back to check started appointments")
    output_dir: Optional[str] = Field(None, description="Directory for scheduling register xlsx downloads")
    keep_page_open: Optional[bool] = Field(None, description="Leave Playwright page open after run")
    as_of: Optional[str] = Field(
        None,
        description="Override check date (YYYY-MM-DD). Defaults to today (Europe/London).",
    )


class CancelledAppointmentsNotesRequest(BaseModel):
    contract_id: str = Field("321129", description="EasyBOP contract_id")
    keep_page_open: Optional[bool] = Field(None, description="Leave Playwright page open after run")


def create_router(
    *,
    get_context: Callable[[], BrowserContext],
    require_browser: Callable[[], None],
    download_scheduling_export: Callable[..., Any],
    login_fn: Any,
    username: str,
    password: str,
    timeout_ms: int,
    easybop_base_url: str,
    session_path: Path,
    default_keep_page_open: bool,
) -> APIRouter:
    router = APIRouter(prefix="/stage5", tags=["Stage 5"])

    @router.post(
        "/stalled-appointment-check",
        summary="Check stalled appointments (Scheduling Register vs OJS register)",
        description=(
            "Downloads the Scheduling Register from EasyBOP **for each month in the lookback window**, "
            "scrapes the Operative Job Sheet register for the same months, and flags appointments that "
            "show **Appointment Started** with no matching **Complete** OJS form for the same "
            "operative + job + date."
        ),
    )
    async def stalled_appointment_check(req: StalledCheckRequest):
        require_browser()
        context = get_context()

        as_of: Optional[date] = None
        if req.as_of:
            try:
                as_of = date.fromisoformat(req.as_of.strip()[:10])
            except ValueError as e:
                raise HTTPException(status_code=422, detail=f"Invalid as_of date: {req.as_of}") from e
        as_of = as_of or date.today()

        keep = default_keep_page_open if req.keep_page_open is None else req.keep_page_open
        out_dir = Path(req.output_dir).expanduser() if req.output_dir else None
        month_list = months_for_lookback(as_of, req.lookback_days)
        month_labels = [f"{y}-{m:02d}" for y, m in month_list]

        sched_page = await context.new_page()
        sched_paths: list[Path] = []
        try:
            for year, month in month_list:
                sched_path, sched_url = await download_scheduling_export(
                    sched_page,
                    context=context,
                    base_url=easybop_base_url,
                    contract_id=req.contract_id,
                    login=login_fn,
                    username=username,
                    password=password,
                    session_path=session_path,
                    timeout_ms=timeout_ms,
                    output_dir=out_dir,
                    report_year=year,
                    report_month=month,
                )
                sched_paths.append(Path(sched_path))
                logger.info(
                    "stalled-check: scheduling %s-%02d saved %s (%s)",
                    year,
                    month,
                    sched_path,
                    sched_url,
                )
        finally:
            if not keep:
                await sched_page.close()

        ojs_page = await context.new_page()
        try:
            result = await run_stalled_check(
                ojs_page,
                sched_paths,
                username=username,
                password=password,
                timeout_ms=timeout_ms,
                as_of=as_of,
                lookback_days=req.lookback_days,
                months_checked=month_labels,
            )
            return {
                "success": True,
                "scheduling_files": [str(p) for p in sched_paths],
                **result,
            }
        except RuntimeError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except Exception:
            logger.exception("stalled-appointment-check")
            raise HTTPException(status_code=500, detail="Failed — see server logs.")
        finally:
            if not keep:
                await ojs_page.close()

    @router.post(
        "/cancelled-appointments-notes",
        summary="Process cancelled appointments and add No access notes",
        description=(
            "Navigates to the planned works index, filters by cancelled status, "
            "and adds a 'No access' note to each cancelled job details notes tab."
        ),
    )
    async def cancelled_appointments_notes(req: CancelledAppointmentsNotesRequest):
        require_browser()
        context = get_context()

        keep = default_keep_page_open if req.keep_page_open is None else req.keep_page_open

        page = await context.new_page()
        try:
            result = await run_cancelled_appointments_notes(
                page,
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
            logger.exception("cancelled-appointments-notes")
            raise HTTPException(status_code=500, detail="Failed — see server logs.")
        finally:
            if not keep:
                await page.close()

    return router
