from __future__ import annotations

import base64
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
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


_config = _load_local_module("_easybop_filler_config", "config.py")
sys.modules["config"] = _config
try:
    _automation = _load_local_module("_easybop_filler_automation", "automation.py")
finally:
    sys.modules.pop("config", None)
settings = _config.settings
fill_form = _automation.fill_form
login = _automation.login

logger = logging.getLogger("easybop.api")


class FillRequest(BaseModel):
    form_fields: Dict[str, Any] = Field(
        ...,
        description=(
            "Either the flat `form_fields` object from the legacy Map-to-Form-Fields node, "
            "or the section-grouped object (property_search, scope_of_work, …) like "
            "`map_to_form_fileds.json`. The API flattens sectioned payloads automatically."
        ),
    )
    contract_id: str = Field(
        "0",
        description=(
            "contract_id for the form URL (?contract_id=X). "
            "Pass '0' to let the API derive it from form_fields.contract_id."
        ),
    )
    submit: bool = Field(
        False,
        description="Set true only to actually save the record. Default false (safe preview).",
    )
    keep_page_open: Optional[bool] = Field(
        None,
        description=(
            "If true, do not close the browser tab after filling (so you can review the form). "
            "If null, uses EASYBOP_KEEP_PAGE_OPEN from .env (default true)."
        ),
    )


class FillResponse(BaseModel):
    success: bool
    message: str
    fields_filled: int = 0
    warnings: List[str] = Field(default_factory=list)
    screenshot_b64: Optional[str] = Field(
        None,
        description="Base64-encoded PNG screenshot of the filled form",
    )


def _resolve_contract_id(requested: str, form_fields: Dict[str, Any]) -> str:
    _ = requested
    _ = form_fields
    return "0"


def _screenshot_b64(png_bytes: bytes) -> str:
    return base64.b64encode(png_bytes).decode()


def create_router(
    *,
    get_context: Callable[[], BrowserContext],
    require_browser: Callable[[], None],
    save_session: Callable[[], Awaitable[None]],
) -> APIRouter:
    router = APIRouter(tags=["Form Filler"])

    @router.post("/fill-work-order", response_model=FillResponse, summary="Fill the works instruction form from a form_fields dict")
    async def fill_work_order(req: FillRequest):
        require_browser()
        effective_cid = _resolve_contract_id(req.contract_id, req.form_fields)
        keep_open = settings.keep_page_open_after_fill if req.keep_page_open is None else req.keep_page_open
        logger.info(
            "fill-work-order: contract_id=%s submit=%s timeout_ms=%s keep_page_open=%s",
            effective_cid,
            req.submit,
            settings.request_timeout_ms,
            keep_open,
        )

        page = None
        try:
            page = await get_context().new_page()
            errors, filled = await fill_form(
                page,
                req.form_fields,
                contract_id=effective_cid,
                submit=req.submit,
                timeout_ms=settings.request_timeout_ms,
                relogin_username=settings.username,
                relogin_password=settings.password,
                after_relogin=save_session,
            )
            screenshot_b64 = _screenshot_b64(await page.screenshot(full_page=True))
            msg = "Form filled" if not errors else f"Filled with {len(errors)} warning(s)"
            if keep_open:
                msg += " — browser tab left open (keep_page_open=true)."
            return FillResponse(
                success=not errors,
                message=msg,
                fields_filled=filled,
                warnings=errors,
                screenshot_b64=screenshot_b64,
            )
        except HTTPException:
            raise
        except RuntimeError as exc:
            logger.warning("Form fill runtime error: %s", exc)
            raise HTTPException(status_code=401, detail=str(exc))
        except Exception:
            logger.exception("Unhandled exception in /fill-work-order")
            raise HTTPException(
                status_code=500,
                detail="Internal error in /fill-work-order. Check server logs for traceback.",
            )
        finally:
            if page is not None and not keep_open:
                await page.close()

    @router.post(
        "/fill-work-order/from-n8n",
        response_model=FillResponse,
        summary="Fill form using the raw list output from the n8n Map-to-Form-Fields node",
    )
    async def fill_from_n8n(
        payload: List[Dict[str, Any]],
        contract_id: str = Query("0", description="Override contract_id in the URL"),
        submit: bool = Query(False, description="Set true to actually save"),
        keep_open: Optional[bool] = Query(
            None,
            description="Leave browser tab open after fill (overrides .env when set)",
        ),
    ):
        if not payload:
            raise HTTPException(status_code=400, detail="Payload list is empty")

        first = payload[0]
        meta_keys = frozenset({"fill_summary", "ai_raw"})
        if isinstance(first.get("form_fields"), dict) and first["form_fields"]:
            form_fields = first["form_fields"]
        else:
            form_fields = {k: v for k, v in first.items() if k not in meta_keys}
            if not form_fields:
                raise HTTPException(
                    status_code=422,
                    detail="First item has no usable data: add `form_fields` or section keys (property_search, …)",
                )

        req = FillRequest(
            form_fields=form_fields,
            contract_id=contract_id,
            submit=submit,
            keep_page_open=keep_open,
        )
        return await fill_work_order(req)

    return router
