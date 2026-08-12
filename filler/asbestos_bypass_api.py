"""
POST /asbestos-bypass/batch — bypass Global: Asbestos Survey (item 24818) on EasyBOP.
"""
from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path
from typing import Any, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from playwright.async_api import BrowserContext
from pydantic import BaseModel, Field, ValidationError

_here = Path(__file__).resolve().parent


def _load_local(name: str, filename: str):
    p = _here / filename
    spec = importlib.util.spec_from_file_location(name, p)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {filename} from {_here}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_bypass_mod = _load_local("_bypass_asbestos", "bypass_asbestos_survey.py")
bypass_asbestos_batch = _bypass_mod.bypass_asbestos_batch

logger = logging.getLogger("easybop.asbestos_bypass_api")


class BypassAsbestosItem(BaseModel):
    corf: str = Field(..., description="Client Order Reference e.g. '98050/2'")
    works_id: str = Field(..., description="EasyBOP works_id for direct WI navigation")


class BypassAsbestosBatchRequest(BaseModel):
    items: List[BypassAsbestosItem] = Field(..., description="CORFs to bypass in one browser session")
    contract_id: str = Field("321129", description="EasyBOP contract ID")
    timeout_ms: int = Field(45_000, description="Playwright timeout per action (ms)")


class BypassAsbestosItemResult(BaseModel):
    success: bool
    bypassed: bool = False
    already_bypassed: bool = False
    works_id: Optional[str] = None
    corf: str = ""
    message: str = ""
    error: Optional[str] = None


class BypassAsbestosBatchResponse(BaseModel):
    success: bool
    total: int = 0
    bypassed: int = 0
    already_bypassed: int = 0
    errors: int = 0
    results: List[BypassAsbestosItemResult] = Field(default_factory=list)


def _parse_batch_payload(raw: bytes) -> BypassAsbestosBatchRequest:
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
                detail="JSON body is a quoted string, not an object",
            ) from exc
    try:
        return BypassAsbestosBatchRequest.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc


def _batch_response(batch: dict) -> BypassAsbestosBatchResponse:
    results = batch.get("results") or []
    return BypassAsbestosBatchResponse(
        success=bool(batch.get("success")),
        total=batch.get("total", len(results)),
        bypassed=batch.get("bypassed", 0),
        already_bypassed=batch.get("already_bypassed", 0),
        errors=batch.get("errors", 0),
        results=[BypassAsbestosItemResult(**r) for r in results],
    )


async def _safe_save_session(save_session) -> None:
    if save_session is None:
        return
    try:
        await save_session()
    except Exception:
        logger.exception("Could not save EasyBOP session after asbestos bypass batch")


def create_router(
    *,
    get_context,
    require_browser,
    save_session=None,
    start_browser=None,
    browser_connected=None,
) -> APIRouter:
    router = APIRouter(prefix="/asbestos-bypass", tags=["Asbestos Bypass"])

    @router.post(
        "/batch",
        response_model=BypassAsbestosBatchResponse,
        summary="Bypass asbestos survey for multiple works in one browser session",
    )
    async def bypass_asbestos_batch_endpoint(request: Request) -> BypassAsbestosBatchResponse:
        req = _parse_batch_payload(await request.body())

        if not req.items:
            return BypassAsbestosBatchResponse(success=True, total=0, results=[])

        decoded = [
            {"corf": str(i.corf).strip(), "works_id": str(i.works_id).strip()}
            for i in req.items
        ]

        async def ensure_browser() -> BypassAsbestosBatchResponse | None:
            connected = browser_connected() if browser_connected else True
            if connected:
                return None
            if start_browser is not None:
                try:
                    await start_browser()
                    logger.info("Browser restarted for asbestos-bypass batch")
                except Exception:
                    logger.exception("Failed to restart browser")
            connected = browser_connected() if browser_connected else False
            if not connected:
                return BypassAsbestosBatchResponse(
                    success=False,
                    total=len(decoded),
                    errors=len(decoded),
                    results=[
                        BypassAsbestosItemResult(
                            success=False,
                            corf=d["corf"],
                            works_id=d["works_id"],
                            error="Browser not running — restart the server",
                            message="Browser not running — restart the server",
                        )
                        for d in decoded
                    ],
                )
            return None

        browser_err = await ensure_browser()
        if browser_err is not None:
            return JSONResponse(status_code=200, content=browser_err.model_dump())

        try:
            require_browser()
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            return JSONResponse(
                status_code=200,
                content=BypassAsbestosBatchResponse(
                    success=False,
                    total=len(decoded),
                    errors=len(decoded),
                    results=[
                        BypassAsbestosItemResult(
                            success=False,
                            corf=d["corf"],
                            works_id=d["works_id"],
                            error=detail,
                            message=detail,
                        )
                        for d in decoded
                    ],
                ).model_dump(),
            )

        ctx: BrowserContext = get_context()
        batch: dict | None = None

        page = await ctx.new_page()
        try:
            batch = await bypass_asbestos_batch(
                page,
                ctx,
                items=decoded,
                contract_id=req.contract_id,
                timeout_ms=req.timeout_ms,
            )
            await _safe_save_session(save_session)
            resp = _batch_response(batch)
            return JSONResponse(status_code=200, content=resp.model_dump())
        except Exception as exc:
            logger.exception("asbestos-bypass batch failed")
            if batch:
                resp = _batch_response(batch)
                return JSONResponse(status_code=200, content=resp.model_dump())
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        finally:
            try:
                await page.close()
            except Exception:
                pass

    return router
