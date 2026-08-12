"""
Update EasyBOP planned-works UDF field "Next appointment date" (udf_pw_works_5088).

Opens works_details.php?tab_nav_item=works_udf per job, fills the date field,
clicks **Save Works UDF Data**, and accepts the browser confirm dialog.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from playwright.async_api import BrowserContext, Page

log = logging.getLogger("easybop.update_works_next_appointment")

WORKS_UDF_URL = (
    "https://easybop.co.uk/a_planned_works/z_works/works_details.php"
    "?works_id={works_id}&contract_id={contract_id}&tab_nav_item=works_udf"
)
NEXT_APPOINTMENT_FIELD_ID = "udf_pw_works_5088"
SAVE_UDF_BUTTON_ID = "btn_save_works_udfs"

_PAGE_STATE_JS = """() => {
  const lb = document.querySelector('#login_box');
  if (lb && lb.offsetParent !== null) return 'login';
  const el = document.getElementById('udf_pw_works_5088');
  if (el) return 'udf';
  const h1 = document.querySelector('h1');
  const h1txt = ((h1 && h1.textContent) || '').toLowerCase();
  if (h1txt.includes('easybop') && h1txt.includes('login')) return 'login';
  const body = (document.body && document.body.innerText) || '';
  if (body.includes('EasyBOP Login')) return 'login';
  return null;
}"""


def _norm_corf(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().upper())


def _parse_iso_date(value: str) -> Optional[date]:
    s = str(value or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    return None


def format_easybop_date(value: Any) -> str:
    """EasyBOP UDF datepicker expects DD/MM/YYYY."""
    if value is None:
        return ""
    if isinstance(value, date):
        return value.strftime("%d/%m/%Y")
    if isinstance(value, datetime):
        return value.date().strftime("%d/%m/%Y")
    s = str(value).strip()
    if not s:
        return ""
    parsed = _parse_iso_date(s)
    if parsed:
        return parsed.strftime("%d/%m/%Y")
    return s


def build_corf_to_works_id_map(rows: List[Dict[str, Any]]) -> Dict[str, str]:
    """Map normalised Client Order Reference -> works_id (last row wins)."""
    out: Dict[str, str] = {}
    for row in rows:
        corf = _norm_corf(str(row.get("client_order_reference") or ""))
        wid = str(row.get("works_id") or "").strip()
        if corf and wid:
            out[corf] = wid
    return out


async def _on_login_screen(page: Page) -> bool:
    try:
        lb = page.locator("#login_box")
        if await lb.count() > 0 and await lb.is_visible():
            return True
    except Exception:
        pass
    if "easybop login" in (await page.title() or "").lower():
        return True
    return False


async def _wait_for_login_or_udf(page: Page, timeout_ms: int) -> Optional[str]:
    try:
        handle = await page.wait_for_function(_PAGE_STATE_JS, timeout=timeout_ms)
        return await handle.json_value()
    except Exception:
        return None


async def _ensure_udf_page(
    page: Page,
    *,
    works_id: str,
    contract_id: str,
    username: str,
    password: str,
    login: Callable[[Page, str, str], Awaitable[bool]],
    timeout_ms: int,
) -> None:
    url = WORKS_UDF_URL.format(works_id=works_id, contract_id=contract_id)
    for attempt in range(2):
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        state = await _wait_for_login_or_udf(page, timeout_ms)
        if state == "login":
            log.info("next appt update: login (works_id=%s attempt=%s)", works_id, attempt + 1)
            ok = await login(page, username, password)
            if not ok:
                raise RuntimeError("EasyBOP login failed")
            continue
        if state == "udf":
            return
        if await _on_login_screen(page):
            ok = await login(page, username, password)
            if not ok:
                raise RuntimeError("EasyBOP login failed")
            continue
        break
    if await _on_login_screen(page):
        raise RuntimeError("Still on login after sign-in")
    field = page.locator(f"#{NEXT_APPOINTMENT_FIELD_ID}")
    if await field.count() == 0:
        raise RuntimeError(f"#{NEXT_APPOINTMENT_FIELD_ID} not found on works_id={works_id}")


async def _set_next_appointment_date(page: Page, display_date: str) -> str:
    """Fill Next appointment date and fire change events so EasyBOP marks the UDF dirty."""
    field_id = NEXT_APPOINTMENT_FIELD_ID
    field = page.locator(f"#{field_id}")
    await field.click(click_count=3)
    await field.fill(display_date)
    await field.press("Tab")
    await page.evaluate(
        """([id, dateStr]) => {
            const el = document.getElementById(id);
            if (!el) return;
            if (el.value !== dateStr) el.value = dateStr;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        [field_id, display_date],
    )
    await page.wait_for_timeout(400)
    return (await field.input_value()).strip()


async def _read_next_appointment_date(
    page: Page,
    *,
    works_id: str,
    contract_id: str,
    username: str,
    password: str,
    login: Callable[[Page, str, str], Awaitable[bool]],
    timeout_ms: int,
) -> str:
    url = WORKS_UDF_URL.format(works_id=works_id, contract_id=contract_id)
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    await _ensure_udf_page(
        page,
        works_id=str(works_id),
        contract_id=contract_id,
        username=username,
        password=password,
        login=login,
        timeout_ms=timeout_ms,
    )
    field = page.locator(f"#{NEXT_APPOINTMENT_FIELD_ID}")
    await field.wait_for(state="visible", timeout=min(15_000, timeout_ms))
    verified = (await field.input_value()).strip()
    if not verified:
        verified = str(
            await page.evaluate(
                f"() => (document.getElementById({NEXT_APPOINTMENT_FIELD_ID!r}) || {{}}).value || ''"
            )
        ).strip()
    return verified


async def _click_save_works_udfs(page: Page, *, timeout_ms: int) -> bool:
    """Click Save Works UDF Data (#btn_save_works_udfs) and accept confirm('Save Works UDF Data?')."""
    save_btn = page.locator(f"#{SAVE_UDF_BUTTON_ID}")
    await save_btn.wait_for(state="visible", timeout=min(15_000, timeout_ms))
    if await save_btn.count() == 0:
        log.warning("save: #%s not found", SAVE_UDF_BUTTON_ID)
        return False

    async def _accept_dialog(dialog) -> None:
        log.info("save: browser dialog type=%s message=%r", dialog.type, dialog.message)
        await dialog.accept()

    # Register handler BEFORE click — click blocks until the dialog is accepted.
    page.once("dialog", _accept_dialog)

    click_timeout = min(15_000, timeout_ms)
    try:
        await save_btn.click(timeout=click_timeout)
    except Exception as click_exc:
        log.warning("save: Playwright click failed (%s), trying JS click", click_exc)
        page.once("dialog", _accept_dialog)
        await page.evaluate(
            f"() => document.getElementById({SAVE_UDF_BUTTON_ID!r})?.click()"
        )

    try:
        await page.wait_for_load_state("networkidle", timeout=min(15_000, timeout_ms))
    except Exception:
        await page.wait_for_timeout(2000)
    log.info("save: clicked #%s and accepted confirm", SAVE_UDF_BUTTON_ID)
    return True


async def update_next_appointment_on_page(
    page: Page,
    *,
    works_id: str,
    next_appointment_date: str,
    contract_id: str = "321129",
    username: str = "",
    password: str = "",
    login: Callable[[Page, str, str], Awaitable[bool]],
    timeout_ms: int = 60_000,
    click_save: bool = True,
    client_order_reference: str = "",
    address: str = "",
    current_next_appointment_date: str = "",
) -> Dict[str, Any]:
    """Fill Next appointment date on one job UDF tab. Returns result dict."""
    display_date = format_easybop_date(next_appointment_date)
    corf = str(client_order_reference or "").strip()
    addr = str(address or "").strip()
    current_raw = str(current_next_appointment_date or "").strip()
    result: Dict[str, Any] = {
        "works_id": str(works_id),
        "client_order_reference": corf,
        "address": addr,
        "current_next_appointment_date": current_raw or None,
        "next_appointment_date": display_date,
        "success": False,
        "saved": False,
        "url": WORKS_UDF_URL.format(works_id=works_id, contract_id=contract_id),
    }
    log.info(
        "next appt update START works_id=%s corf=%s address=%s current=%r -> %s save=%s",
        works_id,
        corf or "-",
        (addr[:50] + "…") if len(addr) > 50 else (addr or "-"),
        current_raw or None,
        display_date,
        click_save,
    )
    try:
        await _ensure_udf_page(
            page,
            works_id=str(works_id),
            contract_id=contract_id,
            username=username,
            password=password,
            login=login,
            timeout_ms=timeout_ms,
        )
        field = page.locator(f"#{NEXT_APPOINTMENT_FIELD_ID}")
        after = await _set_next_appointment_date(page, display_date)
        result["field_value_after"] = after
        result["success"] = after == display_date
        if click_save:
            await page.wait_for_timeout(500)
            try:
                clicked = await _click_save_works_udfs(page, timeout_ms=timeout_ms)
                verified = await _read_next_appointment_date(
                    page,
                    works_id=str(works_id),
                    contract_id=contract_id,
                    username=username,
                    password=password,
                    login=login,
                    timeout_ms=timeout_ms,
                )
                result["field_value_verified"] = verified
                result["saved"] = clicked and verified == display_date
                result["success"] = verified == display_date
                if clicked and not result["saved"]:
                    result["save_error"] = (
                        f"Save clicked but reload shows {verified!r}, expected {display_date!r}"
                    )
                    log.warning(
                        "save: verify failed works_id=%s expected=%r got=%r",
                        works_id,
                        display_date,
                        verified,
                    )
            except Exception as save_exc:
                result["save_error"] = str(save_exc)
                log.warning("save: failed works_id=%s: %s", works_id, save_exc)
        log.info(
            "next appt update DONE works_id=%s corf=%s ok=%s field_after=%r verified=%r saved=%s",
            works_id,
            corf or "-",
            result["success"],
            after,
            result.get("field_value_verified"),
            result.get("saved"),
        )
    except Exception as exc:
        result["error"] = str(exc)
        log.warning(
            "next appt update FAIL works_id=%s corf=%s address=%s: %s",
            works_id,
            corf or "-",
            addr or "-",
            exc,
        )
    return result


async def update_next_appointment_batch(
    context: BrowserContext,
    items: List[Dict[str, Any]],
    *,
    contract_id: str = "321129",
    username: str = "",
    password: str = "",
    login: Callable[[Page, str, str], Awaitable[bool]],
    timeout_ms: int = 60_000,
    click_save: bool = True,
    reuse_page: bool = True,
) -> Dict[str, Any]:
    """
    Update Next appointment date for many jobs sequentially.

    Each item: {works_id, next_appointment_date, client_order_reference?}.
    """
    results: List[Dict[str, Any]] = []
    page: Optional[Page] = None
    try:
        if reuse_page:
            page = await context.new_page()
        for item in items:
            wid = str(item.get("works_id") or "").strip()
            if not wid:
                results.append({
                    "success": False,
                    "error": "missing works_id",
                    "client_order_reference": item.get("client_order_reference"),
                })
                continue
            if not reuse_page:
                page = await context.new_page()
            assert page is not None
            row = await update_next_appointment_on_page(
                page,
                works_id=wid,
                next_appointment_date=str(item.get("next_appointment_date") or ""),
                contract_id=contract_id,
                username=username,
                password=password,
                login=login,
                timeout_ms=timeout_ms,
                click_save=click_save,
                client_order_reference=str(item.get("client_order_reference") or ""),
                address=str(item.get("address") or ""),
                current_next_appointment_date=str(
                    item.get("current_next_appointment_date") or ""
                ),
            )
            results.append(row)
            if not reuse_page and page:
                await page.close()
                page = None
    finally:
        if reuse_page and page and not page.is_closed():
            await page.close()

    ok = sum(1 for r in results if r.get("success"))
    err = len(results) - ok
    return {
        "success": ok > 0 and err == 0,
        "total": len(results),
        "updated": ok,
        "errors": err,
        "click_save": click_save,
        "results": results,
        "message": f"Filled Next appointment date on {ok}/{len(results)} job(s)"
        + (f" ({err} error(s))" if err else "")
        + (" — Save not clicked" if not click_save else " — Saved"),
    }
