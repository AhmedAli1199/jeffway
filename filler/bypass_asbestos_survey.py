"""
Bypass Global: Asbestos Survey on EasyBOP works WI page (item_id 24818).

Flow per works_id:
  works_details.php?tab_nav_item=wi → Bypass link → reason → save → accept alert
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.async_api import BrowserContext, Page

_here = Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

from config import settings  # noqa: E402
from upload_wi_fill import _page_looks_like_login, login, resolve_session_path  # noqa: E402
from upload_wi_msg import (  # noqa: E402
    _close_dialog,
    _navigate_to_works_wi_page,
    _parse_data_item_args,
    works_wi_url,
)

log = logging.getLogger("easybop.asbestos_bypass")

CONTRACT_ID = "321129"
ASBESTOS_SURVEY_ITEM_ID = "24818"
BYPASS_REASON = "Not Required"
_ASBESTOS_ROW = re.compile(r"asbestos\s+survey", re.I)


def _empty_result(corf: str, *, works_id: Optional[str] = None) -> dict:
    return {
        "success": False,
        "bypassed": False,
        "already_bypassed": False,
        "works_id": works_id,
        "corf": corf,
        "message": "",
        "error": None,
    }


async def _ensure_logged_in(page: Page, context: BrowserContext) -> None:
    if not await _page_looks_like_login(page):
        return
    username = settings.username.strip()
    password = settings.password.strip()
    if not username or not password:
        raise RuntimeError("Login required — set username/password in .env")
    ok = await login(page, username, password)
    if not ok:
        raise RuntimeError("EasyBOP login failed")
    sp = resolve_session_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    await context.storage_state(path=str(sp))


async def _wait_works_table(page: Page, timeout_ms: int) -> None:
    if await _page_looks_like_login(page):
        raise RuntimeError("EasyBOP session expired — call POST /login and retry")
    await page.wait_for_selector("table.works_table", state="attached", timeout=timeout_ms)
    links = page.locator('a[data-action="pw_works_show_pre_item_bypass_dialog"]')
    await links.first.wait_for(state="attached", timeout=timeout_ms)
    await asyncio.sleep(0.5)


async def _scan_bypass_rows(page: Page) -> List[Dict[str, Any]]:
    links = page.locator('a[data-action="pw_works_show_pre_item_bypass_dialog"]')
    n = await links.count()
    rows: List[Dict[str, Any]] = []
    for i in range(n):
        link = links.nth(i)
        try:
            visible = await link.is_visible()
        except Exception:
            visible = False
        raw = await link.get_attribute("data-item-args")
        obj = _parse_data_item_args(raw)
        item_id = str(obj.get("item_id")).strip() if obj and obj.get("item_id") is not None else None
        row_text = ""
        status_class = ""
        try:
            row = link.locator("xpath=ancestor::tr[1]")
            if await row.count() > 0:
                row_text = (await row.first.inner_text() or "").strip()
                status_td = row.locator("td").last
                if await status_td.count() > 0:
                    cls = await status_td.first.get_attribute("class") or ""
                    status_class = cls
        except Exception:
            pass
        rows.append(
            {
                "index": i,
                "visible": visible,
                "item_id": item_id,
                "row_text": row_text,
                "status_class": status_class,
                "link": link,
            }
        )
    return rows


async def _asbestos_row_complete(page: Page) -> bool:
    """True when Global Asbestos Survey row looks complete/bypassed already."""
    scanned = await _scan_bypass_rows(page)
    for row in scanned:
        if row["item_id"] == ASBESTOS_SURVEY_ITEM_ID:
            return "pw_complete_color" in str(row.get("status_class") or "")
        if _ASBESTOS_ROW.search(row.get("row_text") or ""):
            return "pw_complete_color" in str(row.get("status_class") or "")
    return False


async def _click_asbestos_bypass_link(page: Page, timeout_ms: int) -> None:
    await _wait_works_table(page, timeout_ms)
    scanned = await _scan_bypass_rows(page)
    visible_rows = [r for r in scanned if r["visible"]] or scanned
    log.info(
        "asbestos_bypass: bypass links=%s %s",
        len(scanned),
        [{"item_id": r["item_id"], "row": (r["row_text"] or "")[:60]} for r in scanned],
    )

    chosen = None
    for row in visible_rows:
        if row["item_id"] == ASBESTOS_SURVEY_ITEM_ID:
            chosen = row["link"]
            break
    if chosen is None:
        for row in visible_rows:
            if _ASBESTOS_ROW.search(row.get("row_text") or ""):
                chosen = row["link"]
                break
    if chosen is None:
        raise RuntimeError(
            "Could not find Bypass link for Global: Asbestos Survey (item_id 24818)"
        )

    try:
        await chosen.click(timeout=min(15_000, timeout_ms))
    except Exception:
        await chosen.click(force=True, timeout=min(15_000, timeout_ms))


async def _fill_and_submit_bypass_dialog(page: Page, timeout_ms: int) -> None:
    await page.wait_for_selector(".ui-dialog:visible", timeout=min(12_000, timeout_ms))
    reason = page.locator(".ui-dialog:visible #bypass_reason")
    await reason.wait_for(state="visible", timeout=min(10_000, timeout_ms))
    await reason.click()
    await reason.fill("")
    await reason.type(BYPASS_REASON, delay=30)
    try:
        await reason.fill(BYPASS_REASON)
    except Exception:
        pass

    dialog_messages: List[str] = []

    async def _on_dialog(dialog) -> None:
        msg = dialog.message or ""
        dialog_messages.append(msg)
        log.info("asbestos_bypass: browser dialog %r → accept", msg)
        await dialog.accept()

    page.once("dialog", _on_dialog)

    save_btn = page.locator(".ui-dialog:visible #btn_dialog_save_changes_bypass")
    await save_btn.wait_for(state="visible", timeout=min(10_000, timeout_ms))
    await save_btn.click(timeout=min(15_000, timeout_ms))

    try:
        await page.wait_for_function(
            """() => {
                const dlg = document.querySelector('.ui-dialog:visible');
                const row = document.querySelector('table.works_table tr td.pw_complete_color');
                return !dlg || !!row;
            }""",
            timeout=min(30_000, timeout_ms),
        )
    except Exception:
        await asyncio.sleep(1.0)


async def bypass_asbestos_for_works(
    page: Page,
    *,
    corf: str,
    works_id: str,
    contract_id: str = CONTRACT_ID,
    timeout_ms: int = 45_000,
    context: Optional[BrowserContext] = None,
) -> dict:
    wid = str(works_id or "").strip()
    result = _empty_result(corf, works_id=wid)
    if not wid:
        result["error"] = "works_id is required"
        result["message"] = result["error"]
        return result

    try:
        await _navigate_to_works_wi_page(
            page, wid, contract_id, timeout_ms, context=context
        )
    except Exception as exc:
        result["error"] = str(exc)
        result["message"] = result["error"]
        return result

    if await _asbestos_row_complete(page):
        result["already_bypassed"] = True
        result["success"] = True
        result["message"] = f"Asbestos survey already complete/bypassed for CORF {corf}"
        log.info(result["message"])
        return result

    try:
        await _click_asbestos_bypass_link(page, timeout_ms)
        await _fill_and_submit_bypass_dialog(page, timeout_ms)
    except Exception as exc:
        result["error"] = str(exc)
        result["message"] = str(exc)
        await _close_dialog(page)
        return result

    await _close_dialog(page)

    if await _asbestos_row_complete(page):
        result["bypassed"] = True
        result["success"] = True
        result["message"] = (
            f"Bypassed asbestos survey for CORF {corf} (works_id={wid}, reason={BYPASS_REASON!r})"
        )
    else:
        result["success"] = True
        result["bypassed"] = True
        result["message"] = (
            f"Bypass submitted for CORF {corf} (works_id={wid}) — could not re-verify row colour"
        )
    log.info(result["message"])
    return result


async def bypass_asbestos_batch(
    page: Page,
    context: BrowserContext,
    *,
    items: List[Dict[str, Any]],
    contract_id: str = CONTRACT_ID,
    timeout_ms: int = 45_000,
) -> dict:
    results: List[dict] = []
    bypassed = 0
    already = 0
    errors = 0

    if not items:
        return {
            "success": True,
            "total": 0,
            "bypassed": 0,
            "already_bypassed": 0,
            "errors": 0,
            "results": [],
        }

    log.info("asbestos_bypass: batch start (%d item(s))", len(items))

    for idx, item in enumerate(items):
        corf = str(item.get("corf") or "").strip()
        wid = str(item.get("works_id") or "").strip()
        log.info("asbestos_bypass [%d/%d] CORF=%s works_id=%s", idx + 1, len(items), corf, wid)
        try:
            result = await bypass_asbestos_for_works(
                page,
                corf=corf,
                works_id=wid,
                contract_id=contract_id,
                timeout_ms=timeout_ms,
                context=context,
            )
        except Exception as exc:
            log.exception("asbestos_bypass unhandled CORF %s", corf)
            result = _empty_result(corf, works_id=wid or None)
            result["error"] = str(exc)
            result["message"] = f"Unhandled error: {exc}"

        if result.get("bypassed"):
            bypassed += 1
        elif result.get("already_bypassed"):
            already += 1
        elif not result.get("success"):
            errors += 1
        results.append(result)

    return {
        "success": errors == 0,
        "total": len(items),
        "bypassed": bypassed,
        "already_bypassed": already,
        "errors": errors,
        "results": results,
    }
