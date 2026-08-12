"""
EasyBOP — Upload asbestos PDF to a work order.

For a given CORF (Client Order Reference):
  1. Navigate to the planned-works index.
  2. Search for the CORF via #search_dwelling (autocomplete).
  3. On the work-order detail page, click **Global: Asbestos Survey** row
     (not Global: Works Order Form) — [data-action="pw_works_show_pre_item_dialog"].
  4. In the popup, set the PDF on the hidden file input.
  5. Click "Save Changes" (#btn_dialog_save_changes).
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

from playwright.async_api import Page

log = logging.getLogger("asbestos.upload_pdf")

LOGIN_URL = "https://easybop.co.uk/"
INDEX_URL = (
    "https://easybop.co.uk/a_planned_works/z_works/index.php?contract_id={contract_id}"
)


def works_wi_url(works_id: str, contract_id: str) -> str:
    wid = str(works_id or "").strip()
    cid = (contract_id or "321129").strip()
    return (
        "https://easybop.co.uk/a_planned_works/z_works/works_details.php"
        f"?works_id={wid}&contract_id={cid}&tab_nav_item=wi"
    )

# EasyBOP pre-item rows on work-order page (contract 321129)
ASBESTOS_SURVEY_ITEM_ID = "24818"
WORKS_ORDER_FORM_ITEM_ID = "24819"


def _parse_data_item_args(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    s = html.unescape(raw.strip())
    try:
        data = json.loads(s)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


async def _page_looks_like_login(page: Page) -> bool:
    try:
        lb = page.locator("#login_box")
        if await lb.count() > 0 and await lb.first.is_visible():
            return True
        if "login" in (await page.title() or "").lower():
            return True
        return await page.get_by_text("EasyBOP Login", exact=False).count() > 0
    except Exception:
        return False


async def _login(page: Page, username: str, password: str) -> bool:
    log.info("login: opening %s", LOGIN_URL)
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")

    if await page.locator("a[href*='logout'], a[href*='log_out'], a[href*='sign_out']").count() > 0:
        log.info("login: already authenticated")
        return True

    box = page.locator("#login_box")
    try:
        await box.wait_for(state="visible", timeout=12_000)
    except Exception as e:
        log.error("login: login_box not visible: %s", e)
        return False

    try:
        await box.locator("input[type='text']").first.fill(username)
        await box.locator("input[type='password']").first.fill(password)
        await page.locator("#btn_login").click()
        await page.wait_for_function(
            "() => { const o=document.querySelector('a[href*=\"logout\"],a[href*=\"log_out\"]'); "
            "const lb=document.querySelector('#login_box'); "
            "return o!==null||(lb&&lb.offsetParent===null); }",
            timeout=20_000,
        )
        log.info("login: success url=%s", page.url)
        return True
    except Exception as e:
        log.exception("login: failed: %s", e)
        return False


async def _ensure_index_ready(
    page: Page,
    contract_id: str,
    username: Optional[str],
    password: Optional[str],
    timeout_ms: int,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> None:
    url = INDEX_URL.format(contract_id=contract_id)
    log.info("upload_pdf: navigate -> %s", url)
    await page.goto(url, wait_until="domcontentloaded")

    try:
        await page.wait_for_selector("#search_dwelling", state="visible", timeout=timeout_ms)
        if not await _page_looks_like_login(page):
            log.info("upload_pdf: index ready")
            return
    except Exception as e:
        log.warning("upload_pdf: search_dwelling not ready: %s", e)

    if await _page_looks_like_login(page):
        if not username or not password:
            raise RuntimeError("Login required but credentials not set in .env")
        log.info("upload_pdf: session expired — re-logging in")
        if not await _login(page, username, password):
            raise RuntimeError("EasyBOP re-login failed")
        if after_relogin:
            await after_relogin()
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_selector("#search_dwelling", state="visible", timeout=timeout_ms)
        if await _page_looks_like_login(page):
            raise RuntimeError("Still on login wall after re-login — check credentials")


async def _navigate_to_works_wi_page(
    page: Page,
    works_id: str,
    contract_id: str,
    timeout_ms: int,
    username: Optional[str],
    password: Optional[str],
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> None:
    """Open works_details WI tab by works_id (skip index CORF search)."""
    wid = str(works_id or "").strip()
    if not wid:
        raise ValueError("works_id is required for direct WI navigation")
    url = works_wi_url(wid, contract_id)
    log.info("upload_pdf: navigate works WI direct -> %s", url)
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

    if await _page_looks_like_login(page):
        if not username or not password:
            raise RuntimeError("Login required but credentials not set in .env")
        log.info("upload_pdf: session expired — re-logging in")
        if not await _login(page, username, password):
            raise RuntimeError("EasyBOP re-login failed")
        if after_relogin:
            await after_relogin()
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

    if await _page_looks_like_login(page):
        raise RuntimeError(f"Still on login wall for works_id={wid}")

    await _wait_pre_item_links(page, timeout_ms)
    log.info("upload_pdf: WI page loaded for works_id=%s (url=%s)", wid, page.url)


async def _search_by_corf(page: Page, corf: str, timeout_ms: int) -> None:
    """Type the CORF into #search_dwelling and pick the first autocomplete match."""
    log.info("upload_pdf: searching for CORF: %r", corf)

    field = page.locator("#search_dwelling")
    await field.wait_for(state="visible", timeout=timeout_ms)
    await field.click()
    await field.fill("")
    await field.type(corf, delay=60)

    try:
        ac = page.locator(".ui-autocomplete:visible li.ui-menu-item")
        await ac.first.wait_for(state="visible", timeout=10_000)
        suggestion_text = await ac.first.inner_text()
        log.info("upload_pdf: autocomplete suggestion: %r", suggestion_text.strip())
        await ac.first.click()
    except Exception as e:
        log.warning("upload_pdf: autocomplete not found — pressing Enter: %s", e)
        await field.press("Enter")

    try:
        await page.wait_for_load_state("load", timeout=min(12_000, timeout_ms))
    except Exception as e:
        log.warning("upload_pdf: post-search load wait: %s", e)

    await _wait_pre_item_links(page, timeout_ms)
    log.info("upload_pdf: work-order page loaded for CORF %r (url=%s)", corf, page.url)


async def _wait_pre_item_links(page: Page, timeout_ms: int) -> None:
    """Wait until work-order pre-item links are on the page (after CORF search)."""
    if await _page_looks_like_login(page):
        raise RuntimeError("EasyBOP session expired on work-order page — call POST /login and retry")

    await page.wait_for_selector(
        'a[data-action="pw_works_show_pre_item_dialog"]',
        state="visible",
        timeout=timeout_ms,
    )
    try:
        await page.wait_for_selector("table.works_table", state="attached", timeout=min(10_000, timeout_ms))
    except Exception:
        pass
    await asyncio.sleep(0.6)


_ASBESTOS_SURVEY_ROW = re.compile(r"asbestos\s+survey", re.I)
_WORKS_ORDER_ROW = re.compile(r"works\s+order\s+form", re.I)


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


async def _close_dialog(page: Page, timeout_ms: int = 2_000) -> None:
    """Best-effort close of the visible EasyBOP dialog — never blocks the batch."""
    close_btn = page.locator(".ui-dialog:visible .ui-dialog-titlebar-close")
    try:
        if await close_btn.count() > 0:
            await close_btn.first.click(timeout=timeout_ms)
            await asyncio.sleep(0.3)
            return
    except Exception as e:
        log.debug("upload_pdf: close button click failed: %s", e)
    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.2)
    except Exception:
        pass


async def _goto_index(
    page: Page,
    contract_id: str,
    timeout_ms: int,
) -> None:
    url = INDEX_URL.format(contract_id=contract_id)
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_selector("#search_dwelling", state="visible", timeout=timeout_ms)
    await asyncio.sleep(0.3)


async def _dialog_has_existing_file(page: Page) -> bool:
    """True when the visible upload popup already lists a file."""
    selectors = [
        ".ui-dialog:visible table.works_table tr.row",
        ".ui-dialog:visible table tr.row",
        ".ui-dialog:visible a[href*='.pdf']",
        ".ui-dialog:visible a[href*='download_file.php']",
    ]
    for sel in selectors:
        loc = page.locator(sel)
        if await loc.count() > 0:
            return True
    return False


def _empty_result(corf: str, pdf_filename: str, *, works_id: Optional[str] = None) -> dict:
    return {
        "corf": corf,
        "works_id": works_id,
        "pdf_filename": pdf_filename,
        "success": False,
        "uploaded": False,
        "already_uploaded": False,
        "message": "",
        "error": None,
    }


async def _click_pre_item_dialog(page: Page, timeout_ms: int) -> None:
    """Click pw_works_show_pre_item_dialog on Global: Asbestos Survey (item_id 24818), not Works Order Form."""
    await _wait_pre_item_links(page, timeout_ms)
    scanned = await _scan_pre_item_rows(page)
    visible_rows = [r for r in scanned if r["visible"]]
    log.info(
        "upload_pdf: found %s pre_item link(s): %s",
        len(scanned),
        [
            {"item_id": r["item_id"], "row": (r["row_text"] or "")[:70]}
            for r in scanned
        ],
    )

    chosen: Optional[Any] = None
    reason = ""

    for row in visible_rows:
        if row["item_id"] == ASBESTOS_SURVEY_ITEM_ID:
            chosen = row["link"]
            reason = f"item_id={ASBESTOS_SURVEY_ITEM_ID}"
            break

    if chosen is None:
        for row in visible_rows:
            if row["item_id"] == WORKS_ORDER_FORM_ITEM_ID:
                continue
            text = row["row_text"] or ""
            if _ASBESTOS_SURVEY_ROW.search(text) and not _WORKS_ORDER_ROW.search(text):
                chosen = row["link"]
                reason = f"row label: {text.split(chr(10))[0][:60]!r}"
                break

    if chosen is None:
        for row in visible_rows:
            if row["item_id"] != WORKS_ORDER_FORM_ITEM_ID:
                text = row["row_text"] or ""
                if _ASBESTOS_SURVEY_ROW.search(text):
                    chosen = row["link"]
                    reason = f"row label (fallback): {text.split(chr(10))[0][:60]!r}"
                    break

    if chosen is None:
        raise RuntimeError(
            "Could not find Global: Asbestos Survey pre_item link "
            f"(expected item_id={ASBESTOS_SURVEY_ITEM_ID}). "
            f"Visible rows: {[{'item_id': r['item_id'], 'row': (r['row_text'] or '')[:80]} for r in visible_rows]}"
        )

    try:
        await chosen.click(timeout=timeout_ms)
        log.info("upload_pdf: clicked Asbestos Survey pre_item_dialog (%s)", reason)
    except Exception as e:
        raise RuntimeError(
            f"Could not click Asbestos Survey pw_works_show_pre_item_dialog: {e}"
        ) from e

    await asyncio.sleep(1.0)

    dialog = page.locator(".ui-dialog:visible, [role='dialog']:visible")
    try:
        await dialog.first.wait_for(state="visible", timeout=8_000)
        log.info("upload_pdf: dialog opened")
    except Exception as e:
        raise RuntimeError(f"Pre-item dialog did not appear: {e}") from e


async def _upload_pdf_in_dialog(
    page: Page,
    pdf_bytes: bytes,
    pdf_filename: str,
    timeout_ms: int,
) -> None:
    """Set the PDF on the file input inside the dialog, then click Save Changes."""
    file_input = page.locator("input[type='file'][name='file']")
    try:
        await file_input.set_input_files(
            {
                "name": pdf_filename,
                "mimeType": "application/pdf",
                "buffer": pdf_bytes,
            }
        )
        log.info("upload_pdf: PDF set on file input: %s (%d bytes)", pdf_filename, len(pdf_bytes))
    except Exception as e:
        raise RuntimeError(f"Could not set PDF on file input: {e}") from e

    await asyncio.sleep(1.0)

    save_btn = page.locator("#btn_dialog_save_changes")
    try:
        await save_btn.wait_for(state="visible", timeout=timeout_ms)
        await save_btn.click()
        log.info("upload_pdf: clicked Save Changes")
    except Exception as e:
        raise RuntimeError(f"Could not click Save Changes: {e}") from e

    await asyncio.sleep(2.0)

    try:
        await page.wait_for_selector(".ui-dialog:visible", state="hidden", timeout=10_000)
        log.info("upload_pdf: dialog closed after save")
    except Exception:
        log.warning("upload_pdf: dialog still visible after save — proceeding")


async def _process_one_corf(
    page: Page,
    *,
    corf: str,
    pdf_bytes: bytes,
    pdf_filename: str,
    contract_id: str,
    username: Optional[str],
    password: Optional[str],
    timeout_ms: int,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
    on_index: bool = False,
    works_id: Optional[str] = None,
) -> dict:
    """Process one upload — CORF index search or direct works_id WI navigation."""
    wid = str(works_id or "").strip() or None
    result = _empty_result(corf, pdf_filename, works_id=wid)

    if wid:
        try:
            await _navigate_to_works_wi_page(
                page,
                wid,
                contract_id,
                timeout_ms,
                username,
                password,
                after_relogin,
            )
        except Exception as exc:
            result["error"] = str(exc)
            result["message"] = str(exc)
            return result
    else:
        if not on_index:
            await _ensure_index_ready(
                page, contract_id, username, password, timeout_ms, after_relogin
            )
        try:
            await _search_by_corf(page, corf, timeout_ms)
        except Exception as exc:
            result["error"] = str(exc)
            result["message"] = str(exc)
            return result

    try:
        await _click_pre_item_dialog(page, timeout_ms)
    except Exception as exc:
        result["error"] = str(exc)
        result["message"] = str(exc)
        return result

    await asyncio.sleep(0.6)

    if await _dialog_has_existing_file(page):
        result["already_uploaded"] = True
        result["success"] = True
        result["message"] = f"PDF already present for CORF {corf} — skipping upload"
        log.info(result["message"])
        if on_index and not wid:
            try:
                await _goto_index(page, contract_id, timeout_ms)
            except Exception as e:
                log.warning("upload_pdf: goto index after skip failed: %s", e)
        return result

    try:
        await _upload_pdf_in_dialog(page, pdf_bytes, pdf_filename, timeout_ms)
    except Exception as exc:
        result["error"] = str(exc)
        result["message"] = str(exc)
        await _close_dialog(page)
        return result

    result["uploaded"] = True
    result["success"] = True
    result["message"] = f"Uploaded {pdf_filename} for CORF {corf}"
    return result


async def upload_asbestos_pdf_batch(
    page: Page,
    *,
    items: List[Dict[str, Any]],
    contract_id: str = "321129",
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout_ms: int = 30_000,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
    use_works_id_navigation: bool = False,
) -> dict:
    """
    Process all CORFs in one browser tab.

    Each item: { corf, pdf_filename, pdf_bytes }
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
        "Starting asbestos PDF batch upload for %d item(s) (works_id_nav=%s)",
        len(items),
        batch_uses_works_id,
    )

    if not batch_uses_works_id:
        try:
            await _ensure_index_ready(
                page, contract_id, username, password, timeout_ms, after_relogin
            )
        except Exception as exc:
            err = str(exc)
            for item in items:
                r = _empty_result(
                    item["corf"],
                    item["pdf_filename"],
                    works_id=str(item.get("works_id") or "").strip() or None,
                )
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
            pdf_filename = item["pdf_filename"]
            pdf_bytes = item["pdf_bytes"]
            works_id = str(item.get("works_id") or "").strip() or None

            log.info(
                "Batch [%d/%d] CORF=%s works_id=%s file=%s",
                idx + 1,
                len(items),
                corf,
                works_id or "-",
                pdf_filename,
            )

            if idx > 0 and not batch_uses_works_id:
                await _goto_index(page, contract_id, timeout_ms)

            try:
                result = await _process_one_corf(
                    page,
                    corf=corf,
                    pdf_bytes=pdf_bytes,
                    pdf_filename=pdf_filename,
                    contract_id=contract_id,
                    username=username,
                    password=password,
                    timeout_ms=timeout_ms,
                    after_relogin=after_relogin,
                    on_index=not batch_uses_works_id,
                    works_id=works_id,
                )
            except Exception as exc:
                log.warning("Error processing CORF %s: %s", corf, exc)
                result = _empty_result(corf, pdf_filename, works_id=works_id)
                result["error"] = str(exc)
                result["message"] = f"Unhandled error: {exc}"

            if result.get("uploaded"):
                uploaded += 1
            elif result.get("already_uploaded"):
                already_uploaded += 1
            elif not result.get("success"):
                errors += 1

            results.append(result)
    except Exception as exc:
        log.exception("Batch aborted after %d/%d CORF(s)", len(results), len(items))
        while len(results) < len(items):
            item = items[len(results)]
            r = _empty_result(
                item["corf"],
                item["pdf_filename"],
                works_id=str(item.get("works_id") or "").strip() or None,
            )
            r["error"] = f"Batch aborted: {exc}"
            r["message"] = r["error"]
            results.append(r)
            errors += 1

    return {
        "success": errors == 0,
        "total": len(items),
        "uploaded": uploaded,
        "already_uploaded": already_uploaded,
        "errors": errors,
        "results": results,
    }


async def upload_asbestos_pdf(
    page: Page,
    corf: str,
    pdf_bytes: bytes,
    pdf_filename: str = "asbestos_report.pdf",
    contract_id: str = "321129",
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout_ms: int = 30_000,
    after_relogin: Optional[Callable[[], Awaitable[None]]] = None,
) -> dict:
    """Single-item wrapper — kept for multipart endpoint."""
    batch = await upload_asbestos_pdf_batch(
        page,
        items=[{"corf": corf, "pdf_filename": pdf_filename, "pdf_bytes": pdf_bytes}],
        contract_id=contract_id,
        username=username,
        password=password,
        timeout_ms=timeout_ms,
        after_relogin=after_relogin,
    )
    return batch["results"][0] if batch["results"] else _empty_result(corf, pdf_filename)
