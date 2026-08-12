"""
Upload .msg files to EasyBOP pre-work "Global: Works Order Form" for multiple CORFs.

Uses one browser tab for the whole batch:
  index → CORF search → open dialog → check/upload → back to index → next CORF
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
from wi_msg_checkpoint import clear_checkpoint, persist_success  # noqa: E402

log = logging.getLogger("easybop.wi_msg_upload")

CONTRACT_ID = "321129"
WORKS_ORDER_FORM_ITEM_ID = "24819"
_BASE = "https://easybop.co.uk"
_INDEX_URL = f"{_BASE}/a_planned_works/z_works/index.php"
_WORKS_ORDER_ROW = re.compile(r"works\s+order\s+form", re.I)


def _index_url(contract_id: str) -> str:
    return f"{_INDEX_URL}?contract_id={contract_id}"


def works_wi_url(works_id: str, contract_id: str) -> str:
    """WI tab on works details — hosts the pre-works table with item_id links."""
    wid = str(works_id or "").strip()
    cid = (contract_id or CONTRACT_ID).strip()
    return (
        f"{_BASE}/a_planned_works/z_works/works_details.php"
        f"?works_id={wid}&contract_id={cid}&tab_nav_item=wi"
    )


def _parse_data_item_args(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _empty_result(corf: str, file_name: str) -> dict:
    return {
        "success": False,
        "uploaded": False,
        "already_uploaded": False,
        "works_id": None,
        "corf": corf,
        "file_name": file_name,
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

    log.info("Login screen detected — signing in")
    ok = await login(page, username, password)
    if not ok:
        raise RuntimeError("EasyBOP login failed — check username/password in .env")

    sp = resolve_session_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    await context.storage_state(path=str(sp))
    log.info("Session saved to %s", sp)


async def _goto_index(
    page: Page,
    contract_id: str,
    timeout_ms: int,
    *,
    context: Optional[BrowserContext] = None,
) -> None:
    """Open works index; log in first if session expired."""
    url = _index_url(contract_id)
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        await page.wait_for_load_state("networkidle", timeout=min(20_000, timeout_ms))
    except Exception:
        pass

    if context is not None and await _page_looks_like_login(page):
        await _ensure_logged_in(page, context)
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            await page.wait_for_load_state("networkidle", timeout=min(20_000, timeout_ms))
        except Exception:
            pass

    await page.wait_for_selector("#search_dwelling", timeout=timeout_ms)


async def _navigate_to_works_wi_page(
    page: Page,
    works_id: str,
    contract_id: str,
    timeout_ms: int,
    *,
    context: Optional[BrowserContext] = None,
) -> None:
    wid = str(works_id or "").strip()
    if not wid:
        raise ValueError("works_id is required for direct WI navigation")

    await _close_dialog(page)

    url = works_wi_url(wid, contract_id)
    log.info("wi_msg_upload: navigate works WI direct -> %s", url)
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        await page.wait_for_load_state("networkidle", timeout=min(20_000, timeout_ms))
    except Exception:
        pass

    if context is not None and await _page_looks_like_login(page):
        await _ensure_logged_in(page, context)
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            await page.wait_for_load_state("networkidle", timeout=min(20_000, timeout_ms))
        except Exception:
            pass

    if await _page_looks_like_login(page):
        raise RuntimeError("EasyBOP session expired on works WI page — call POST /login and retry")

    await _wait_pre_item_links(page, timeout_ms)
    log.info("wi_msg_upload: WI page loaded for works_id=%s (url=%s)", wid, page.url)


async def _wait_pre_item_links(page: Page, timeout_ms: int) -> None:
    if await _page_looks_like_login(page):
        raise RuntimeError("EasyBOP session expired on work-order page — call POST /login and retry")

    try:
        await page.wait_for_selector("table.works_table", state="attached", timeout=timeout_ms)
    except Exception:
        pass

    links = page.locator('a[data-action="pw_works_show_pre_item_dialog"]')
    await links.first.wait_for(state="attached", timeout=timeout_ms)
    await asyncio.sleep(0.6)


async def _scan_pre_item_rows(page: Page) -> List[Dict[str, Any]]:
    links = page.locator('a[data-action="pw_works_show_pre_item_dialog"]')
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
        try:
            row = link.locator("xpath=ancestor::tr[1]")
            if await row.count() > 0:
                row_text = (await row.first.inner_text() or "").strip()
        except Exception:
            pass
        rows.append(
            {
                "index": i,
                "visible": visible,
                "item_id": item_id,
                "row_text": row_text,
                "link": link,
            }
        )
    return rows


async def _click_works_order_dialog(page: Page, timeout_ms: int) -> None:
    """Open Global: Works Order Form (item_id 24819), not asbestos."""
    await _wait_pre_item_links(page, timeout_ms)
    scanned = await _scan_pre_item_rows(page)
    visible_rows = [r for r in scanned if r["visible"]]
    if not visible_rows:
        visible_rows = scanned
    log.info(
        "wi_msg_upload: found %s pre_item link(s): %s",
        len(scanned),
        [
            {"item_id": r["item_id"], "row": (r["row_text"] or "")[:70]}
            for r in scanned
        ],
    )

    chosen = None
    for row in visible_rows:
        if row["item_id"] == WORKS_ORDER_FORM_ITEM_ID:
            chosen = row["link"]
            break

    if chosen is None:
        for row in visible_rows:
            if _WORKS_ORDER_ROW.search(row["row_text"] or ""):
                chosen = row["link"]
                break

    if chosen is None:
        raise RuntimeError(
            "Could not find 'Global: Works Order Form' pre-work action link (item_id 24819)"
        )

    try:
        await chosen.click(timeout=min(15_000, timeout_ms))
    except Exception:
        await chosen.click(force=True, timeout=min(15_000, timeout_ms))


async def _close_dialog(page: Page, timeout_ms: int = 2_000) -> None:
    """Best-effort close of visible EasyBOP dialog — never block the batch."""
    close_btn = page.locator(".ui-dialog:visible .ui-dialog-titlebar-close")
    try:
        if await close_btn.count() > 0:
            await close_btn.first.click(timeout=timeout_ms)
            await asyncio.sleep(0.3)
            return
    except Exception as exc:
        log.debug("wi_msg_upload: close button click failed: %s", exc)
    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.2)
    except Exception:
        pass


async def _dialog_has_existing_file(page: Page) -> bool:
    """True when the visible upload popup already lists a .msg file."""
    selectors = [
        ".ui-dialog:visible table.works_table tr.row",
        ".ui-dialog:visible table tr.row",
        ".ui-dialog:visible a[href*='.msg']",
        ".ui-dialog:visible a[href*='download_file.php']",
    ]
    for sel in selectors:
        loc = page.locator(sel)
        if await loc.count() > 0:
            return True
    return False


async def _process_one_corf(
    page: Page,
    *,
    corf: str,
    msg_bytes: bytes,
    file_name: str,
    contract_id: str,
    timeout_ms: int,
    context: Optional[BrowserContext] = None,
    works_id: Optional[str] = None,
) -> dict:
    """Process one CORF — index search unless works_id is provided."""
    wid = str(works_id or "").strip() or None
    result = _empty_result(corf, file_name)
    if wid:
        result["works_id"] = wid

    if wid:
        try:
            await _navigate_to_works_wi_page(
                page, wid, contract_id, timeout_ms, context=context
            )
        except Exception as exc:
            result["error"] = str(exc)
            result["message"] = result["error"]
            return result
        log.info("CORF %s → works_id=%s (direct navigation)", corf, wid)
    else:
        search = page.locator("#search_dwelling")
        await search.click()
        await search.fill("")
        await asyncio.sleep(0.2)
        await search.type(corf, delay=60)

        try:
            await page.wait_for_selector(
                ".ui-autocomplete .ui-menu-item:visible",
                timeout=12_000,
            )
        except Exception:
            result["error"] = f"No autocomplete results for CORF '{corf}'"
            result["message"] = result["error"]
            return result

        await page.locator(".ui-autocomplete .ui-menu-item:visible").first.click()

        try:
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            pass

        current_url = page.url
        m = re.search(r"works_id=(\d+)", current_url)
        if not m:
            result["error"] = (
                f"After selecting CORF '{corf}', did not land on a works-detail page "
                f"(URL: {current_url})"
            )
            result["message"] = result["error"]
            return result

        result["works_id"] = m.group(1)
        log.info("CORF %s → works_id=%s", corf, result["works_id"])

    try:
        await _click_works_order_dialog(page, timeout_ms)
    except Exception as exc:
        result["error"] = str(exc)
        result["message"] = result["error"]
        return result

    try:
        await page.wait_for_selector(".ui-dialog:visible", timeout=12_000)
    except Exception:
        result["error"] = "Pre-work dialog did not open after clicking 'Click on me'"
        result["message"] = result["error"]
        return result

    await asyncio.sleep(0.6)

    if await _dialog_has_existing_file(page):
        result["already_uploaded"] = True
        result["success"] = True
        result["message"] = f"File already present for CORF {corf} — skipping upload"
        log.info(result["message"])
        await _close_dialog(page)
        if not wid:
            await _goto_index(page, contract_id, timeout_ms, context=context)
        return result

    file_input = page.locator(".ui-dialog:visible input[type='file']").first
    try:
        await file_input.wait_for(timeout=8_000)
    except Exception:
        result["error"] = "File input not found inside dialog"
        result["message"] = result["error"]
        await _close_dialog(page)
        return result

    await file_input.set_input_files([
        {
            "name": file_name,
            "mimeType": "application/vnd.ms-outlook",
            "buffer": msg_bytes,
        }
    ])

    log.info("File input set: %s (%d bytes)", file_name, len(msg_bytes))
    await asyncio.sleep(1.0)

    async def _accept_dialog(dialog) -> None:
        log.info("Browser dialog: %s → accepting", dialog.message)
        await dialog.accept()

    page.once("dialog", _accept_dialog)

    await page.locator(".ui-dialog:visible #btn_dialog_save_changes").click()

    try:
        await page.wait_for_function(
            """() => {
                const rows = document.querySelectorAll('.ui-dialog tr.row');
                const dlg  = document.querySelector('.ui-dialog:not([style*="display: none"])');
                return rows.length > 0 || !dlg;
            }""",
            timeout=30_000,
        )
    except Exception:
        log.warning("Timed out waiting for post-save state for CORF %s", corf)

    after_count = await page.locator(".ui-dialog tr.row").count()
    dialog_open = await page.locator(".ui-dialog:visible").count()

    if after_count > 0 or dialog_open == 0:
        result["uploaded"] = True
        result["success"] = True
        result["message"] = (
            f"Successfully uploaded {file_name} to EasyBOP for CORF {corf} "
            f"(works_id={result['works_id']})"
        )
        log.info(result["message"])
    else:
        result["error"] = (
            "Upload attempted but could not confirm success "
            "(dialog still open, no file row found)"
        )
        result["message"] = result["error"]

    await _close_dialog(page)
    return result


async def upload_msg_batch(
    page: Page,
    context: BrowserContext,
    *,
    items: List[Dict[str, Any]],
    contract_id: str = CONTRACT_ID,
    timeout_ms: int = 30_000,
    use_works_id_navigation: bool = False,
) -> dict:
    """
    Process all CORFs in one browser tab.

    Each item: { corf, file_name, msg_bytes }
    Returns summary + per-item results for sheet updates.
    """
    results: List[dict] = []
    uploaded = 0
    already_uploaded = 0
    errors = 0

    if not items:
        return {
            "success": True,
            "total": 0,
            "uploaded": 0,
            "already_uploaded": 0,
            "errors": 0,
            "results": [],
        }

    batch_uses_works_id = use_works_id_navigation or any(
        str(item.get("works_id") or "").strip() for item in items
    )
    log.info(
        "Starting batch upload for %d CORF(s) (works_id_nav=%s)",
        len(items),
        batch_uses_works_id,
    )

    try:
        if not batch_uses_works_id:
            await _goto_index(page, contract_id, timeout_ms, context=context)
    except Exception as exc:
        if not await _page_looks_like_login(page):
            raise
        log.warning("Index load failed on login screen (%s) — attempting auth", exc)
        await _ensure_logged_in(page, context)
        if not batch_uses_works_id:
            await _goto_index(page, contract_id, timeout_ms, context=context)

    if await _page_looks_like_login(page):
        err = "Still on login page after auth attempt"
        for item in items:
            r = _empty_result(item["corf"], item["file_name"])
            r["error"] = err
            r["message"] = err
            results.append(r)
            errors += 1
        return {
            "success": False,
            "total": len(items),
            "uploaded": 0,
            "already_uploaded": 0,
            "errors": errors,
            "results": results,
        }

    try:
        for idx, item in enumerate(items):
            corf = item["corf"]
            file_name = item["file_name"]
            msg_bytes = item["msg_bytes"]

            log.info("Batch [%d/%d] CORF=%s file=%s", idx + 1, len(items), corf, file_name)

            wid = str(item.get("works_id") or "").strip() or None

            if idx > 0 and not batch_uses_works_id:
                await _goto_index(page, contract_id, timeout_ms, context=context)

            try:
                result = await _process_one_corf(
                    page,
                    corf=corf,
                    msg_bytes=msg_bytes,
                    file_name=file_name,
                    contract_id=contract_id,
                    timeout_ms=timeout_ms,
                    context=context,
                    works_id=wid,
                )
            except Exception as exc:
                log.exception("Error processing CORF %s", corf)
                result = _empty_result(corf, file_name)
                if wid:
                    result["works_id"] = wid
                result["error"] = str(exc)
                result["message"] = f"Unhandled error: {exc}"

            if result.get("uploaded"):
                uploaded += 1
            elif result.get("already_uploaded"):
                already_uploaded += 1
            elif not result.get("success"):
                errors += 1

            results.append(result)
            if result.get("uploaded") or result.get("already_uploaded"):
                persist_success(result)
    except Exception as exc:
        log.exception(
            "Batch aborted after %d/%d CORF(s) — unprocessed CORFs omitted from response",
            len(results),
            len(items),
        )

    if len(results) >= len(items) and errors == 0:
        clear_checkpoint()

    return {
        "success": errors == 0 and len(results) >= len(items),
        "total": len(items),
        "uploaded": uploaded,
        "already_uploaded": already_uploaded,
        "errors": errors,
        "results": results,
    }


async def upload_msg_for_corf(
    page: Page,
    context: BrowserContext,
    *,
    corf: str,
    msg_bytes: bytes,
    file_name: str,
    contract_id: str = CONTRACT_ID,
    timeout_ms: int = 30_000,
) -> dict:
    """Single-item wrapper — kept for backward compatibility."""
    batch = await upload_msg_batch(
        page,
        context,
        items=[{"corf": corf, "file_name": file_name, "msg_bytes": msg_bytes}],
        contract_id=contract_id,
        timeout_ms=timeout_ms,
    )
    return batch["results"][0] if batch["results"] else _empty_result(corf, file_name)
