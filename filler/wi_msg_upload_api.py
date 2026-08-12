"""
POST /wi-msg/upload-batch — upload .msg files to EasyBOP in one browser session.

Registered as a router in api/runtime.py alongside the other service routers.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import logging
from pathlib import Path
from typing import Any, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from playwright.async_api import BrowserContext
from pydantic import BaseModel, Field, ValidationError

from wi_msg_checkpoint import (
    clear_checkpoint,
    merge_results_with_checkpoint,
    summarize_results,
)

_here = Path(__file__).resolve().parent


def _load_local(name: str, filename: str):
    p = _here / filename
    spec = importlib.util.spec_from_file_location(name, p)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {filename} from {_here}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_upload_mod = _load_local("_wi_msg_upload", "upload_wi_msg.py")
upload_msg_batch = _upload_mod.upload_msg_batch
upload_msg_for_corf = _upload_mod.upload_msg_for_corf

logger = logging.getLogger("easybop.wi_msg_upload_api")


class UploadWiMsgItem(BaseModel):
    corf: str = Field(..., description="Client Order Reference e.g. '34101/1'")
    file_name: str = Field(..., description=".msg file name")
    msg_base64: str = Field(..., description="Base64-encoded .msg file bytes")
    works_id: Optional[str] = Field(
        None,
        description="EasyBOP works_id — when set, navigates directly to works WI page",
    )


class UploadWiMsgBatchRequest(BaseModel):
    items: List[UploadWiMsgItem] = Field(..., description="CORFs to process in one browser session")
    contract_id: str = Field("321129", description="EasyBOP contract ID")
    timeout_ms: int = Field(45_000, description="Playwright timeout per action (ms)")
    use_works_id_navigation: bool = Field(
        False,
        description="Skip index search; use works_id on each item for direct WI navigation",
    )


class UploadWiMsgItemResult(BaseModel):
    success: bool
    uploaded: bool = False
    already_uploaded: bool = False
    works_id: Optional[str] = None
    corf: str = ""
    file_name: str = ""
    message: str = ""
    error: Optional[str] = None


class UploadWiMsgBatchResponse(BaseModel):
    success: bool
    total: int = 0
    uploaded: int = 0
    already_uploaded: int = 0
    errors: int = 0
    results: List[UploadWiMsgItemResult] = Field(default_factory=list)


class UploadWiMsgRequest(BaseModel):
    corf: str = Field(..., description="Client Order Reference e.g. '34101/1'")
    file_name: str = Field(..., description=".msg file name")
    msg_base64: str = Field(..., description="Base64-encoded .msg file bytes")
    contract_id: str = Field("321129", description="EasyBOP contract ID")
    timeout_ms: int = Field(45_000, description="Playwright timeout per action (ms)")


class UploadWiMsgResponse(BaseModel):
    success: bool
    uploaded: bool = False
    already_uploaded: bool = False
    works_id: Optional[str] = None
    corf: str = ""
    file_name: str = ""
    message: str = ""
    error: Optional[str] = None


_OLE_MAGIC = b"\xd0\xcf\x11\xe0"
_MIN_MSG_BYTES = 256
_MIME_MARKERS = (
    b"received:", b"from:", b"return-path:", b"mime-version:",
    b"delivered-to:", b"subject:", b"date:", b"content-type:",
)


def _is_ole_msg(msg_bytes: bytes) -> bool:
    return len(msg_bytes) >= 8 and msg_bytes[:4] == _OLE_MAGIC


def _is_mime_email(msg_bytes: bytes) -> bool:
    head = msg_bytes[:4096].lower()
    for marker in _MIME_MARKERS:
        if head.startswith(marker) or (b"\n" + marker) in head or (b"\r\n" + marker) in head:
            return True
    return False


def _validate_msg_bytes(msg_bytes: bytes, corf: str) -> None:
    if len(msg_bytes) < _MIN_MSG_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"msg file too small for CORF {corf} ({len(msg_bytes)} bytes)",
        )
    if not (_is_ole_msg(msg_bytes) or _is_mime_email(msg_bytes)):
        preview = msg_bytes[:32].decode("latin-1", errors="replace")
        raise HTTPException(
            status_code=400,
            detail=(
                f"msg file for CORF {corf} is not OLE .msg or MIME email "
                f"({len(msg_bytes)} bytes). Preview: {preview!r}"
            ),
        )


def _parse_batch_payload(raw: bytes) -> UploadWiMsgBatchRequest:
    """Parse JSON body from n8n (handles accidental double-encoding)."""
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
        return UploadWiMsgBatchRequest.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc


def _decode_items(items: List[UploadWiMsgItem]) -> list:
    decoded = []
    for item in items:
        try:
            msg_bytes = base64.b64decode(item.msg_base64)
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid base64 for CORF {item.corf}: {exc}",
            )
        if len(msg_bytes) == 0:
            raise HTTPException(
                status_code=400,
                detail=f"msg_base64 decoded to 0 bytes for CORF {item.corf}",
            )
        _validate_msg_bytes(msg_bytes, item.corf)
        wid = str(item.works_id or "").strip() or None
        decoded.append({
            "corf": item.corf,
            "file_name": item.file_name,
            "msg_bytes": msg_bytes,
            "works_id": wid,
        })
    return decoded


def _batch_error_results(decoded: list, msg: str) -> UploadWiMsgBatchResponse:
    """Browser/request failure — return checkpoint successes only (never blanket errors)."""
    merged = merge_results_with_checkpoint(decoded, [])
    summary = summarize_results(merged, len(decoded))
    if merged:
        logger.warning(
            "Batch unavailable (%s) — returning %d checkpoint result(s) for sheet update",
            msg,
            len(merged),
        )
    else:
        logger.warning("Batch unavailable (%s) — no checkpoint results to return", msg)
    return UploadWiMsgBatchResponse(**summary)


def _batch_response(decoded: list, batch: dict | None) -> UploadWiMsgBatchResponse:
    merged = merge_results_with_checkpoint(decoded, (batch or {}).get("results"))
    summary = summarize_results(merged, len(decoded))
    if len(merged) >= len(decoded) and summary["errors"] == 0:
        clear_checkpoint()
    return UploadWiMsgBatchResponse(**summary)


async def _safe_save_session(save_session) -> None:
    if save_session is None:
        return
    try:
        await save_session()
    except Exception:
        logger.exception("Could not save EasyBOP session after batch — results still returned")


def create_router(
    *,
    get_context,
    require_browser,
    save_session=None,
    start_browser=None,
    browser_connected=None,
) -> APIRouter:
    router = APIRouter(prefix="/wi-msg", tags=["WI MSG Upload"])

    @router.post(
        "/upload-batch",
        response_model=UploadWiMsgBatchResponse,
        summary="Upload multiple .msg files in one browser session",
        description=(
            "Processes all CORFs in a single browser tab. "
            "After each CORF it returns to the works index before starting the next."
        ),
    )
    async def upload_wi_msg_batch(request: Request) -> UploadWiMsgBatchResponse:
        req = _parse_batch_payload(await request.body())

        if not req.items:
            return UploadWiMsgBatchResponse(success=True, total=0, results=[])

        decoded = _decode_items(req.items)

        async def ensure_browser() -> UploadWiMsgBatchResponse | None:
            connected = browser_connected() if browser_connected else True
            if connected:
                return None
            if start_browser is not None:
                try:
                    await start_browser()
                    logger.info("Browser restarted for upload-batch")
                except Exception:
                    logger.exception("Failed to restart browser")
            connected = browser_connected() if browser_connected else False
            if not connected:
                return _batch_error_results(
                    decoded, "Browser not running — restart the server"
                )
            return None

        browser_err = await ensure_browser()
        if browser_err is not None:
            return JSONResponse(status_code=200, content=browser_err.model_dump())

        try:
            require_browser()
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            resp = _batch_error_results(decoded, detail)
            return JSONResponse(status_code=200, content=resp.model_dump())

        ctx: BrowserContext = get_context()
        batch: dict | None = None

        page = await ctx.new_page()
        try:
            try:
                batch = await upload_msg_batch(
                    page,
                    ctx,
                    items=decoded,
                    contract_id=req.contract_id,
                    timeout_ms=req.timeout_ms,
                    use_works_id_navigation=req.use_works_id_navigation,
                )
            except Exception as exc:
                logger.exception("upload_wi_msg_batch unhandled error")
                if batch is None:
                    batch = {"results": []}

            await _safe_save_session(save_session)
            resp = _batch_response(decoded, batch)
            return JSONResponse(status_code=200, content=resp.model_dump())
        except Exception as exc:
            logger.exception("upload-batch failed after partial progress")
            resp = _batch_response(decoded, batch)
            if resp.results:
                return JSONResponse(status_code=200, content=resp.model_dump())
            resp = _batch_error_results(decoded, str(exc))
            return JSONResponse(status_code=200, content=resp.model_dump())
        finally:
            try:
                await page.close()
            except Exception:
                pass

    @router.post(
        "/upload",
        response_model=UploadWiMsgResponse,
        summary="Upload one .msg file (legacy single-item endpoint)",
    )
    async def upload_wi_msg(req: UploadWiMsgRequest) -> UploadWiMsgResponse:
        require_browser()
        ctx: BrowserContext = get_context()
        decoded = _decode_items([
            UploadWiMsgItem(corf=req.corf, file_name=req.file_name, msg_base64=req.msg_base64)
        ])

        page = await ctx.new_page()
        try:
            batch = await upload_msg_batch(
                page,
                ctx,
                items=decoded,
                contract_id=req.contract_id,
                timeout_ms=req.timeout_ms,
            )
            result = batch["results"][0] if batch["results"] else {}
            success = bool(result.get("uploaded") or result.get("already_uploaded"))
            if success and save_session is not None:
                await save_session()
            return UploadWiMsgResponse(success=success, **result)
        except Exception as exc:
            logger.exception("upload_wi_msg unhandled error for CORF %s", req.corf)
            raise HTTPException(status_code=500, detail=str(exc))
        finally:
            await page.close()

    return router
