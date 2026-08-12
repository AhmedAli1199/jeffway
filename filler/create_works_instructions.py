"""
Create Works Instructions on EasyBOP via works_details.php form (one CORF per page load).

Used by Stage 1 n8n: POST /create-works-instructions with Work Inst Import rows.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from playwright.async_api import Page, async_playwright

from automation import (
    _fill_mapped_keys,
    _fill_one,
    _fill_rows_by_label,
    _fill_search_property,
    _fill_sow,
    login,
    page_looks_like_login_screen,
)
from config import settings
from upload_wi_fill import resolve_session_path

log = logging.getLogger("create_works_instructions")

WORKS_DETAILS_URL = (
    "https://easybop.co.uk/a_planned_works/z_works/works_details.php?contract_id={contract_id}"
)

# Type of Work Code (sheet) → scope_of_works_ids[] value (form.html)
SOW_CODE_TO_ID: Dict[str, str] = {
    "DR": "80025",
    "DRT": "84559",
    "ER": "77631",
    "EW": "77634",
    "RR": "77633",
    "UR": "77632",
}

KEYS_ORDER_INFO: Tuple[str, ...] = (
    "client_order_reference",
    "issued_by_name",
)

KEYS_PROPERTY: Tuple[str, ...] = (
    "property_ref",
    "alt_property_ref",
    "resident_type",
    "occupancy_type",
    "n_bedrooms",
    "property_type",
    "vulnerability_id",
    "zone_name",
    "client_estate_reference",
    "client_property_status",
    "year_built",
)

KEYS_ADDRESS: Tuple[str, ...] = (
    "address_1",
    "address_2",
    "address_3",
    "address_4",
    "town",
    "county",
    "postcode",
)

KEYS_RESIDENT: Tuple[str, ...] = (
    "resident_title",
    "resident_f_name",
    "resident_l_name",
    "resident_mobile",
    "resident_tel",
    "resident_email",
)

KEYS_ALT_ADDRESS: Tuple[str, ...] = (
    "alt_address_1",
    "alt_address_2",
    "alt_address_3",
    "alt_address_4",
    "alt_town",
    "alt_county",
    "alt_postcode",
    "alt_country",
)

_DATETIME_RE = re.compile(
    r"^(\d{1,2})/(\d{1,2})/(\d{2,4})\s+(\d{1,2}):(\d{2})(?::\d{2})?$"
)
_BCC_SURVEYOR_RE = re.compile(r"BCC\s*Surveyor", re.IGNORECASE)

# Sheet column aliases (flexible headers)
_COL = {
    "type_of_work": ("Type of Work Code", "Type of Work", "SOW Code"),
    "description": ("Description of Works", "Description of Work", "description_of_works"),
    "corf": ("Client Order Reference", "Client Order Ref", "Client Order Ref."),
    "issued_by": ("Issued By", "issued_by_name"),
    "datetime": ("Date Time Received (DD/MM/YYYY HH:MM)", "Date Time Received", "Date Received"),
    "uprn": ("UPRN", "property_ref"),
    "uprn_alt": ("UPRN Alternative", "alt_property_ref"),
    "resident_type": ("Resident Type (Leaseholder,Tenant)", "Resident Type", "resident_type"),
    "occupancy": ("Occupancy (Occupied,Void)", "Occupancy", "occupancy_type"),
    "bedrooms": ("No. of Bedrooms", "n_bedrooms"),
    "property_type": ("Property Type", "property_type"),
    "zone": ("Zone", "zone_name"),
    "estate_ref": ("Client Estate Reference", "client_estate_reference"),
    "prop_status": ("Client Property Status", "client_property_status"),
    "year_built": ("Year of Build", "Year Of Build", "year_built"),
    "resident_title": ("Resident Title", "resident_title"),
    "resident_fn": ("Resident First Name", "resident_f_name"),
    "resident_ln": ("Resident Last Name", "resident_l_name"),
    "resident_mobile": ("Resident Mobile", "resident_mobile"),
    "resident_tel": ("Resident Tel", "Resident Telephone", "resident_tel"),
    "resident_email": ("Resident Email", "resident_email"),
    "addr1": ("Address 1", "address_1"),
    "addr2": ("Address 2", "address_2"),
    "addr3": ("Address 3", "address_3"),
    "addr4": ("Address 4", "address_4"),
    "town": ("town", "Town"),
    "county": ("County", "county"),
    "postcode": ("Postcode", "postcode"),
    "surveyor": ("Surveyor", "BCC Surveyor"),
    "vuln_code": ("Vulnerability Code", "vulnerability_id"),
}


def _col(row: Dict[str, Any], key: str) -> Optional[str]:
    for name in _COL.get(key, (key,)):
        v = row.get(name)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def _s(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _norm_select_token(v: Any) -> Optional[str]:
    s = _s(v)
    if not s:
        return None
    s = s.split("(")[0].split(",")[0].strip()
    return s.lower() if s else None


def _parse_received_datetime(raw: Any) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    s = _s(raw)
    if not s:
        return None, None, None
    m = _DATETIME_RE.match(s)
    if not m:
        return s, None, None
    day, month, year, hour, minute = m.groups()
    if len(year) == 2:
        year = f"20{year}"
    date_part = f"{int(day):02d}/{int(month):02d}/{year}"
    return date_part, f"{int(hour):02d}", f"{int(minute):02d}"


def sheet_row_to_form_fields(row: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Map Work Inst Import sheet row → #works_form field ids (flexible column names)."""
    sow_code = (_col(row, "type_of_work") or "RR").upper()
    sow_id = SOW_CODE_TO_ID.get(sow_code)
    if not sow_id:
        for token, sid in SOW_CODE_TO_ID.items():
            if token in sow_code:
                sow_id = sid
                break
    if not sow_id:
        sow_id = SOW_CODE_TO_ID["RR"]

    date_received, hour, minute = _parse_received_datetime(_col(row, "datetime"))
    uprn = _col(row, "uprn")

    fields: Dict[str, Optional[str]] = {
        "scope_of_works_ids[]": sow_id,
        "description_of_works": _col(row, "description"),
        "client_order_reference": _col(row, "corf"),
        "issued_by_name": _col(row, "issued_by"),
        "date_received": date_received,
        "date_received_hour": hour,
        "date_received_minute": minute,
        "search_property": uprn,
        "property_ref": uprn,
        "alt_property_ref": _col(row, "uprn_alt"),
        "resident_type": _norm_select_token(_col(row, "resident_type")),
        "occupancy_type": _norm_select_token(_col(row, "occupancy")),
        "n_bedrooms": _col(row, "bedrooms"),
        "property_type": _col(row, "property_type"),
        "zone_name": _col(row, "zone"),
        "client_estate_reference": _col(row, "estate_ref"),
        "client_property_status": _col(row, "prop_status"),
        "year_built": _col(row, "year_built"),
        "resident_title": _col(row, "resident_title"),
        "resident_f_name": _col(row, "resident_fn"),
        "resident_l_name": _col(row, "resident_ln"),
        "resident_mobile": _col(row, "resident_mobile"),
        "resident_tel": _col(row, "resident_tel"),
        "resident_email": _col(row, "resident_email"),
        "address_1": _col(row, "addr1"),
        "address_2": _col(row, "addr2"),
        "address_3": _col(row, "addr3"),
        "address_4": _col(row, "addr4"),
        "town": _col(row, "town"),
        "county": _col(row, "county"),
        "postcode": _col(row, "postcode"),
        "alt_address_1": _s(row.get("Alternative Address 1")),
        "alt_address_2": _s(row.get("Alternative Address 2")),
        "alt_address_3": _s(row.get("Alternative Address 3")),
        "alt_address_4": _s(row.get("Alternative Address 4")),
        "alt_town": _s(row.get("Alternative Town")),
        "alt_county": _s(row.get("Alternative County")),
        "alt_postcode": _s(row.get("Alternative Postcode")),
        "alt_country": _s(row.get("Alternative Country")),
        "udf_pw_c_works_instructions_5065": _col(row, "surveyor"),
    }

    vuln_code = _col(row, "vuln_code")
    if vuln_code:
        fields["vulnerability_id"] = vuln_code

    return {k: v for k, v in fields.items() if v is not None}


async def _fill_vulnerability_by_code(page: Page, code: str, errors: List[str]) -> bool:
    sel = page.locator("#vulnerability_id")
    if await sel.count() == 0:
        return False
    try:
        await sel.select_option(label=re.compile(re.escape(code), re.I))
        return True
    except Exception:
        pass
    try:
        opts = await sel.locator("option").all()
        for opt in opts:
            text = (await opt.inner_text() or "").strip()
            if text.upper().startswith(code.upper()):
                val = await opt.get_attribute("value")
                if val:
                    await sel.select_option(value=val)
                    return True
    except Exception as exc:
        errors.append(f"[Property Details] vulnerability_id → {exc}")
    return False


async def _wait_for_order_information(page: Page, timeout_ms: int = 10_000) -> None:
    await page.wait_for_function(
        """() => {
          const el = document.getElementById('order_information');
          if (!el) return false;
          const st = window.getComputedStyle(el);
          return st.display !== 'none' && st.visibility !== 'hidden' && el.offsetParent !== null;
        }""",
        timeout=timeout_ms,
    )


async def _fill_order_information(
    page: Page,
    fields: Dict[str, Optional[str]],
    errors: List[str],
) -> int:
    filled = 0
    for key in KEYS_ORDER_INFO:
        val = fields.get(key)
        if not val:
            continue
        err = await _fill_one(page, key, val)
        if err:
            errors.append(f"[Order Information] {err}")
        else:
            filled += 1

    if fields.get("date_received"):
        err = await _fill_one(page, "date_received", fields["date_received"])
        if err:
            errors.append(f"[Order Information] {err}")
        else:
            filled += 1
    if fields.get("date_received_hour"):
        try:
            await page.locator("#date_received_hour").select_option(value=fields["date_received_hour"])
            filled += 1
        except Exception as exc:
            errors.append(f"[Order Information] date_received_hour → {exc}")
    if fields.get("date_received_minute"):
        try:
            await page.locator("#date_received_minute").select_option(value=fields["date_received_minute"])
            filled += 1
        except Exception as exc:
            errors.append(f"[Order Information] date_received_minute → {exc}")

    return filled


async def fill_works_form_page(
    page: Page,
    fields: Dict[str, Optional[str]],
    *,
    contract_id: str,
    timeout_ms: int,
) -> Tuple[List[str], int]:
    """Fill #works_form on the current page (does not submit)."""
    errors: List[str] = []
    filled = 0

    await reset_works_form_page(page, contract_id=contract_id, timeout_ms=timeout_ms)

    uprn = fields.get("search_property") or fields.get("property_ref")
    if uprn:
        await _fill_search_property(page, uprn)
        filled += 1
        await asyncio.sleep(0.5)

    sow_val = fields.get("scope_of_works_ids[]")
    if sow_val:
        await _fill_sow(page, sow_val)
        filled += 1
        try:
            await _wait_for_order_information(page)
        except Exception as exc:
            errors.append(f"Order Information section did not appear after SOW select: {exc}")

    desc = fields.get("description_of_works")
    if desc:
        err = await _fill_one(page, "description_of_works", desc)
        if err:
            errors.append(f"[Description] {err}")
        else:
            filled += 1

    filled += await _fill_order_information(page, fields, errors)

    prop_fields = dict(fields)
    vuln_code = prop_fields.pop("vulnerability_id", None)
    filled += await _fill_mapped_keys(page, prop_fields, KEYS_PROPERTY, "Property Details", errors)
    if vuln_code:
        if await _fill_vulnerability_by_code(page, vuln_code, errors):
            filled += 1
    filled += await _fill_mapped_keys(page, fields, KEYS_ADDRESS, "Address", errors)
    filled += await _fill_mapped_keys(page, fields, KEYS_RESIDENT, "Resident Details", errors)
    filled += await _fill_mapped_keys(page, fields, KEYS_ALT_ADDRESS, "Alternative Address", errors)

    surveyor = fields.get("udf_pw_c_works_instructions_5065")
    if surveyor:
        filled += await _fill_rows_by_label(
            page, _BCC_SURVEYOR_RE, surveyor, "Surveyor", errors
        )

    return errors, filled


async def click_create_works_instruction(page: Page, timeout_ms: int) -> None:
    btn = page.locator("#btn_create_works_instruction")
    await btn.wait_for(state="visible", timeout=timeout_ms)
    await page.wait_for_function(
        """() => {
          const btn = document.getElementById('btn_create_works_instruction');
          return btn && !btn.disabled && !btn.classList.contains('ui-state-disabled');
        }""",
        timeout=timeout_ms,
    )
    log.info("Clicking Create Works Instruction")
    await btn.click()
    await asyncio.sleep(2)
    try:
        await page.wait_for_load_state("load", timeout=min(60_000, timeout_ms))
    except Exception:
        pass
    await asyncio.sleep(2)


async def reset_works_form_page(page: Page, *, contract_id: str, timeout_ms: int) -> None:
    """Navigate to blank works_details.php (required before each CORF)."""
    url = WORKS_DETAILS_URL.format(contract_id=contract_id)
    log.info("Loading fresh form: %s", url)
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_selector("#works_form", state="attached", timeout=timeout_ms)
    if await page_looks_like_login_screen(page):
        raise RuntimeError("EasyBOP login required — call POST /login first")
    # Ensure create button is on a new blank form
    await page.wait_for_selector("#btn_create_works_instruction", state="attached", timeout=timeout_ms)


async def create_one_works_instruction(
    page: Page,
    row: Dict[str, Any],
    *,
    contract_id: str,
    timeout_ms: int,
    row_number: Optional[int] = None,
    skip_final_reset: bool = False,
) -> Dict[str, Any]:
    corf = _col(row, "corf") or "?"
    row_number = row_number if row_number is not None else row.get("row_number")
    fields = sheet_row_to_form_fields(row)
    log.info(
        "Creating WI row=%s CORF=%s UPRN=%s SOW=%s",
        row_number,
        corf,
        fields.get("property_ref"),
        fields.get("scope_of_works_ids[]"),
    )

    if corf == "?":
        return {
            "success": False,
            "client_order_reference": corf,
            "row_number": row_number,
            "errors": ["Missing Client Order Reference"],
            "message": "Missing Client Order Reference",
        }
    if not fields.get("property_ref"):
        return {
            "success": False,
            "client_order_reference": corf,
            "row_number": row_number,
            "errors": ["Missing UPRN"],
            "message": "Missing UPRN",
        }

    result: Dict[str, Any] = {}
    try:
        errors, filled = await fill_works_form_page(
            page, fields, contract_id=contract_id, timeout_ms=timeout_ms
        )
        if errors:
            log.warning("CORF %s fill warnings (%s): %s", corf, len(errors), errors[:5])

        await click_create_works_instruction(page, timeout_ms)
        result = {
            "success": True,
            "client_order_reference": corf,
            "row_number": row_number,
            "fields_filled": filled,
            "warnings": errors,
            "url": page.url,
            "message": "Create Works Instruction clicked",
        }
    except Exception as exc:
        log.exception("create_one_works_instruction CORF=%s failed", corf)
        result = {
            "success": False,
            "client_order_reference": corf,
            "row_number": row_number,
            "errors": [str(exc)],
            "message": str(exc),
        }
    finally:
        if not skip_final_reset:
            try:
                await reset_works_form_page(page, contract_id=contract_id, timeout_ms=timeout_ms)
            except Exception as reset_exc:
                log.warning("Post-create form reset failed for CORF %s: %s", corf, reset_exc)
                if not result.get("success"):
                    result.setdefault("errors", []).append(f"form reset: {reset_exc}")

    return result


def _row_number_sync(row: Dict[str, Any]) -> Optional[int]:
    rn = row.get("row_number")
    if rn is None:
        return None
    try:
        return int(rn)
    except (TypeError, ValueError):
        return None


async def _process_one_row_parallel(
    pw: Any,
    index: int,
    row: Dict[str, Any],
    *,
    total: int,
    contract_id: str,
    headless: bool,
    session_path: Any,
    username: str,
    password: str,
    timeout_ms: int,
    semaphore: asyncio.Semaphore,
    session_lock: asyncio.Lock,
) -> Tuple[int, Dict[str, Any]]:
    """Run one CORF in its own browser (up to max_concurrent at a time)."""
    corf = _col(row, "corf") or "?"
    rn = _row_number_sync(row)

    async with semaphore:
        log.info("Batch %s/%s starting (browser slot)", index + 1, total)
        browser = None
        try:
            browser = await pw.chromium.launch(
                headless=headless,
                slow_mo=settings.slow_mo_ms or 0,
                args=["--window-size=1400,900"],
            )
            ctx_kwargs: Dict[str, Any] = {}
            async with session_lock:
                if session_path.exists():
                    ctx_kwargs["storage_state"] = str(session_path)
            context = await browser.new_context(**ctx_kwargs)
            page = await context.new_page()

            async def _accept_dialog(dialog) -> None:
                log.info('Dialog: "%s" — accept', dialog.message)
                await dialog.accept()

            page.on("dialog", _accept_dialog)

            init_url = WORKS_DETAILS_URL.format(contract_id=contract_id)
            await page.goto(init_url, wait_until="domcontentloaded")
            if await page_looks_like_login_screen(page):
                async with session_lock:
                    if await page_looks_like_login_screen(page):
                        if not username or not password:
                            raise RuntimeError(
                                "Login required — set credentials in .env or call POST /login"
                            )
                        if not await login(page, username, password):
                            raise RuntimeError("EasyBOP login failed")
                        session_path.parent.mkdir(parents=True, exist_ok=True)
                        await context.storage_state(path=str(session_path))

            result = await create_one_works_instruction(
                page,
                row,
                contract_id=contract_id,
                timeout_ms=timeout_ms,
                row_number=rn,
                skip_final_reset=True,
            )
            async with session_lock:
                session_path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(session_path))
            log.info(
                "Batch %s/%s CORF %s → %s",
                index + 1,
                total,
                result.get("client_order_reference"),
                "OK" if result.get("success") else result.get("message"),
            )
            return index, result
        except Exception as exc:
            log.exception("Batch row %s CORF=%s unhandled error", index, corf)
            result = {
                "success": False,
                "client_order_reference": corf,
                "row_number": rn,
                "errors": [str(exc)],
                "message": str(exc),
            }
            log.info(
                "Batch %s/%s CORF %s → %s",
                index + 1,
                total,
                corf,
                result.get("message"),
            )
            return index, result
        finally:
            if browser is not None:
                await browser.close()


async def create_works_instructions_batch(
    rows: List[Dict[str, Any]],
    *,
    contract_id: str = "321129",
    headless: bool = False,
    max_concurrent: Optional[int] = None,
) -> Dict[str, Any]:
    if not rows:
        raise ValueError("No rows to create")

    session_path = resolve_session_path()
    username = settings.username
    password = settings.password
    timeout_ms = max(settings.request_timeout_ms, 60_000)
    workers = max(1, min(3, max_concurrent or settings.wi_create_max_concurrent))
    total = len(rows)
    log.info(
        "Creating %s WI(s) with up to %s parallel browser(s)",
        total,
        workers,
    )

    semaphore = asyncio.Semaphore(workers)
    session_lock = asyncio.Lock()

    async with async_playwright() as pw:
        tasks = [
            _process_one_row_parallel(
                pw,
                i,
                row,
                total=total,
                contract_id=contract_id,
                headless=headless,
                session_path=session_path,
                username=username,
                password=password,
                timeout_ms=timeout_ms,
                semaphore=semaphore,
                session_lock=session_lock,
            )
            for i, row in enumerate(rows)
        ]
        pairs = await asyncio.gather(*tasks)

    pairs.sort(key=lambda p: p[0])
    results = [p[1] for p in pairs]

    ok = sum(1 for r in results if r.get("success"))
    failed = len(results) - ok
    return {
        "success": ok > 0,
        "all_created": failed == 0,
        "contract_id": contract_id,
        "total": len(results),
        "created_count": ok,
        "failed_count": failed,
        "max_concurrent": workers,
        "results": results,
        "message": f"Created {ok}/{len(results)} works instruction(s)"
        + (f" ({failed} failed)" if failed else ""),
    }
